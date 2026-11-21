"""AI 课堂视频导出（P5）：离线播放器逐场景截图 → ffmpeg 合成 H.264 MP4（可选旁白音轨）。

契约见 ``INTEGRATION-CONTRACT.md`` §4.1（导出）、task-40 与 task-44；设计要点：

- **两种模式**：``static``（默认，逐场景静帧 + 旁白音轨，向后兼容）与 ``animated``
  （逐帧动画：按 fps 采样 ``window.__zhixuCapture.seekMs(t)`` 截图，场景之间用 ffmpeg
  ``xfade`` 做 ``fade`` 转场；``transition='none'`` 时直接拼接）；
- **截帧**：服务端用 Playwright 打开 ``exports.build_offline_html`` 生成的离线单文件播放器
  （``page.set_content``），等待 ``window.__zhixuCapture.ready`` 后逐场景调用
  ``gotoScene(index)`` 并在固定 1280×720 视口截图；每个场景至少 1 帧；
- **动画时间轴**：``animated`` 模式下每个场景帧数 = ceil(时长 × fps)（单场景 ≤300 帧），
  逐帧调用 ``seekMs(t)`` 把聚光灯/激光/高亮/白板/字幕确定性推进到 t 毫秒；场景时长优先取
  旁白音频时长（ffprobe），无旁白时用播放器 ``sceneDurationMs(i)`` 的 actions 估算（≥1.5s）；
- **音画**：课程 ``config.narration`` 里有旁白的场景使用旁白音频作为该场景音频（不足补静音），
  无旁白场景补静音轨，保证转场/拼接的片段结构一致；``animated`` 模式始终输出 AAC 音轨
  （无旁白时为静音），浏览器可直接播放；
- **合成**：static 模式每个场景编码为独立 mp4 片段 → concat demuxer ``-c copy`` 合并；
  animated 模式每个场景先编码 JPEG 序列 + 音轨为片段，``fade`` 转场用 ``xfade`` + ``acrossfade``
  一次重编码合成（转场时长自动收敛到最短场景的一半以内），``none`` 时仍走 concat；
  均为 1280×720、yuv420p、faststart；产物走 ``storage.save_file`` + ``cls_assets``（kind=``video``），
  下载地址 ``/api/classroom/assets/{id}``；
- **任务 ``course-video``**：SSE 中文阶段「渲染第 n/N 场景 · 第 k/m 帧」「合成视频（转场）」等，
  写 checkpoint；取消时删除半成品，已完成场景的帧保留，续跑按「场景内容摘要 + 旁白音频摘要 + fps」
  复用帧（内容/音频变化则重截）；取消/失败不落库任何视频素材；
- **降级**：``animated`` 模式下若离线播放器缺少 ``seekMs``/``sceneDurationMs``（老模板/老课程 HTML）
  或首次调用失败 → 自动回退 static，任务结果标注 ``mode_used='static'`` 与 ``fallback_reason``；
- **限额**：场景 ≤12、总时长 ≤10 分钟、产物 ≤200MB；帧率 5–24（默认 12）；
  animated 单场景 ≤300 帧、总帧数 ≤6000；转场 100–1000ms（默认 400ms fade）；
  无 Playwright/ffmpeg 时 ``503`` + 中文原因；
- **路由**：``POST /courses/{id}/video``（创建任务）、``GET /courses/{id}/video``（最近产物/能力/任务）、
  ``GET /courses/{id}/export?format=mp4``（下载最近一次成功产物，未生成 409）。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import os
import shutil
import time
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field

from .. import storage
from ..auth import current_user
from . import db as cdb
from . import exports as cexports
from . import tts

router = APIRouter()
log = logging.getLogger("app.classroom.video")

TASK_NAME = "course-video"
TASK_LABEL = "课堂视频导出"

#: 帧率（输出视频恒定帧率；静帧场景不受影响，主要影响播放器兼容性与体积）。
DEFAULT_FPS = 12
MIN_FPS = 5
MAX_FPS = 24
#: 单次导出的场景数上限（课程超过时需要先删减场景或分段导出）。
MAX_SCENES = 12
#: 总时长上限：10 分钟（= 600 秒）。
MAX_VIDEO_SECONDS = 600.0
#: 产物体积上限：200MB。
MAX_VIDEO_BYTES = 200 * 1024 * 1024
#: 无旁白场景的默认静帧时长（秒）。
DEFAULT_SCENE_SECONDS = 3.0
MIN_SCENE_SECONDS = 0.5
#: 视频画布（与离线播放器 1000×562.5 画布同为 16:9，缩放 1.28 后正好铺满）。
VIDEO_WIDTH = 1280
VIDEO_HEIGHT = 720
#: 视频素材与既有课堂素材共用目录（``cls_assets.storage_name`` + ``storage``）。
VIDEO_FOLDER = cdb.ASSET_FOLDER
VIDEO_KIND = "video"
VIDEO_MIME = "video/mp4"
#: 切场后等待布局/字体稳定的毫秒数（截图前）。
CAPTURE_SETTLE_MS = int(os.environ.get("STUDY_VIDEO_CAPTURE_SETTLE_MS", "120") or 120)
#: 未完成任务的工作目录保留天数（续跑需要已完成帧；超期清理避免磁盘累积）。
WORK_DIR_TTL_SECONDS = 14 * 24 * 3600
FFMPEG_TIMEOUT_SECONDS = int(os.environ.get("STUDY_VIDEO_FFMPEG_TIMEOUT_SECONDS", "600") or 600)
FFPROBE_TIMEOUT_SECONDS = 30
#: 视频模式：``static``（逐场景静帧，默认、向后兼容）与 ``animated``（逐帧动画 + 场景转场）。
VIDEO_MODE_STATIC = "static"
VIDEO_MODE_ANIMATED = "animated"
VIDEO_MODES = (VIDEO_MODE_STATIC, VIDEO_MODE_ANIMATED)
DEFAULT_VIDEO_MODE = VIDEO_MODE_STATIC
#: 场景转场：``none``（直接拼接）与 ``fade``（ffmpeg xfade 交叉淡化，默认）。
TRANSITION_NONE = "none"
TRANSITION_FADE = "fade"
TRANSITIONS = (TRANSITION_NONE, TRANSITION_FADE)
DEFAULT_TRANSITION = TRANSITION_FADE
DEFAULT_TRANSITION_MS = 400
MIN_TRANSITION_MS = 100
MAX_TRANSITION_MS = 1000
#: animated 限额：单场景 ≤300 帧、总帧数 ≤6000（总时长仍沿用 600 秒上限）。
MAX_ANIMATED_FRAMES_PER_SCENE = 300
MAX_ANIMATED_FRAMES_TOTAL = 6000
#: 逐帧截图使用 JPEG（比 PNG 快且体积小，ffmpeg image2 直接读序列）。
ANIMATED_FRAME_SUFFIX = ".jpg"
ANIMATED_FRAME_QUALITY = int(os.environ.get("STUDY_VIDEO_FRAME_QUALITY", "85") or 85)
ANIMATED_FRAMES_DIR = "frames"
#: 动画时间轴估算参数（与 ``exports.py`` 内联 JS 的 ``actionDurationMs/sceneDurationMs`` 保持一致；
#: 两边任何改动都要同步，否则 route 准入估算与播放器逐帧采样会不一致）。
MIN_SCENE_MS = 1500
SPEECH_BASE_MS = 800
SPEECH_PER_CHAR_MS = 250
SPEECH_MIN_MS = 1500
VISUAL_ACTION_MS = 700
WB_OP_MS = 260
WB_MIN_MS = 700
WB_MAX_MS = 3000
AUDIO_ACTION_MS = 1000
#: 逐帧截图后等待一次 rAF（保证样式/绘制提交）再截图；``STUDY_VIDEO_ANIM_RAF=0`` 可关闭。
ANIMATED_SETTLE_RAF = str(os.environ.get("STUDY_VIDEO_ANIM_RAF", "1") or "1").strip().lower() not in (
    "0", "off", "false", "no",
)


# ---------------------------------------------------------------------------
# 延迟引用 app.ai.jobs（测试可 monkeypatch，也避免包初始化循环）
# ---------------------------------------------------------------------------

def _ai_jobs():
    from ..ai import jobs

    return jobs


def _db():
    from .. import db

    return db


def _render_module():
    from ..ai import render as ai_render

    return ai_render


def _publish(job_id: int, event: str, payload: dict) -> None:
    try:
        _ai_jobs().publish_event(job_id, event, payload)
    except Exception:  # SSE/Redis 推送失败不影响已落盘产物
        pass


def _job_status_update(job_id: int, status: str, stage: str) -> None:
    try:
        _ai_jobs().update_job(job_id, status, stage)
    except Exception:
        pass


def _checkpoint_get(job_id: int) -> dict:
    try:
        value = _ai_jobs()._job_get_checkpoint(job_id)
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


def _checkpoint_set(job_id: int, checkpoint: dict) -> None:
    try:
        _ai_jobs()._job_set_checkpoint(job_id, checkpoint)
    except Exception:
        pass


def _checkpoint_force(job_id: int, checkpoint: dict) -> None:
    """取消/中断后强制落库检查点：``_job_set_checkpoint`` 带 cancelled 过滤会跳过写入。"""
    db = _db()
    try:
        with db.connection() as con:
            db.write_lock(con, "ai-job-checkpoint")
            con.execute(
                "UPDATE ai_jobs SET checkpoint_json=?, updated_at=? WHERE id=?",
                (cdb.json_dump(checkpoint), db.utcnow(), job_id),
            )
    except Exception:
        log.warning("Could not force checkpoint for video job %s", job_id, exc_info=True)


def _cancelled(job_id: int) -> bool:
    try:
        return bool(_ai_jobs().is_job_cancelled(job_id))
    except Exception:
        return False


def _params(data) -> dict:
    params = getattr(data, "params", None)
    return dict(params) if isinstance(params, dict) else {}


def _int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# 模式 / 转场参数与逐帧动画时间轴（与 exports.py 内联 JS 同步）
# ---------------------------------------------------------------------------

def _resolve_video_mode(value) -> str:
    """解析视频模式（默认 static，保持向后兼容）；非法值抛中文 ``ValueError``。"""
    text = str(value or "").strip().lower() or DEFAULT_VIDEO_MODE
    if text not in VIDEO_MODES:
        raise ValueError("视频模式只能是 static（静态画面）或 animated（逐帧动画）")
    return text


def _resolve_transition(value) -> str:
    """解析场景转场（默认 fade）；非法值抛中文 ``ValueError``。"""
    text = str(value or "").strip().lower() or DEFAULT_TRANSITION
    if text not in TRANSITIONS:
        raise ValueError("场景转场只能是 none（直接拼接）或 fade（交叉淡化）")
    return text


def _resolve_transition_ms(value) -> int:
    """解析转场时长（毫秒，100–1000，默认 400）；非法值抛中文 ``ValueError``。"""
    if value is None or value == "":
        return DEFAULT_TRANSITION_MS
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ValueError("场景转场时长必须是 %d–%d 毫秒的整数" % (
            MIN_TRANSITION_MS, MAX_TRANSITION_MS)) from None
    if not MIN_TRANSITION_MS <= number <= MAX_TRANSITION_MS:
        raise ValueError("场景转场时长必须在 %d–%d 毫秒之间" % (
            MIN_TRANSITION_MS, MAX_TRANSITION_MS))
    return number


def _scene_actions(scene: dict) -> list[dict]:
    """读取场景 ``meta.actions``（离线播放器逐帧动画时间轴的输入）。"""
    meta = cdb.json_load(scene.get("meta_json"), {})
    actions = meta.get("actions") if isinstance(meta, dict) else None
    if not isinstance(actions, list):
        return []
    return [action for action in actions if isinstance(action, dict)]


def _action_duration_ms(action: dict) -> int:
    """单个动作占用的动画时长（毫秒）；与 ``exports.py`` 内联 JS 的 ``actionDurationMs`` 同步。"""
    action_type = str((action or {}).get("type") or "")
    if action_type == "speech":
        text = str((action or {}).get("text") or "")
        return max(SPEECH_MIN_MS, SPEECH_BASE_MS + len(text) * SPEECH_PER_CHAR_MS)
    if action_type in ("spotlight", "laser", "highlight"):
        return VISUAL_ACTION_MS
    if action_type == "whiteboard":
        ops = (action or {}).get("ops")
        count = len(ops) if isinstance(ops, list) else 0
        return max(WB_MIN_MS, min(WB_MAX_MS, count * WB_OP_MS))
    if action_type == "play_audio":
        return AUDIO_ACTION_MS
    return 0


def estimate_scene_duration_ms(scene: dict) -> int:
    """无旁白场景的逐帧动画时长估算（毫秒，最少 1500ms）。

    与离线播放器 ``window.__zhixuCapture.sceneDurationMs(i)`` 使用同一套参数；route 准入
    先用它估算帧数/总帧数并给出 422，任务内再用播放器真实返回值二次校验。
    """
    total = 0
    for action in _scene_actions(scene):
        total += _action_duration_ms(action)
    return max(MIN_SCENE_MS, total)


def animated_frame_times(duration_ms: int, fps: int) -> list[int]:
    """场景时长 → 逐帧采样时间（毫秒，从 0 开始，步长 1000/fps；帧数 = ceil(时长×fps)）。"""
    duration = max(1, _int(duration_ms))
    rate = max(1, _int(fps, DEFAULT_FPS))
    count = max(1, int(math.ceil(duration / 1000.0 * rate)))
    return [int(round(index * 1000.0 / rate)) for index in range(count)]


def animated_frame_count(duration_ms: int, fps: int) -> int:
    return len(animated_frame_times(duration_ms, fps))


def animated_frame_plan(durations_seconds: list[float], fps: int) -> list[int]:
    """按场景时长计算每场景帧数并校验限额；超限抛中文 ``ValueError``（route 会转 422）。"""
    frames: list[int] = []
    for position, duration in enumerate(durations_seconds or [], 1):
        duration_ms = max(int(MIN_SCENE_SECONDS * 1000), int(round(float(duration or 0) * 1000)))
        count = animated_frame_count(duration_ms, fps)
        if count > MAX_ANIMATED_FRAMES_PER_SCENE:
            raise ValueError(
                "第 %d 个场景需要 %d 帧，超过逐帧动画单场景上限 %d 帧："
                "请降低帧率（当前 %d）或缩短旁白后重试"
                % (position, count, MAX_ANIMATED_FRAMES_PER_SCENE, _int(fps, DEFAULT_FPS))
            )
        frames.append(count)
    total = sum(frames)
    if total > MAX_ANIMATED_FRAMES_TOTAL:
        raise ValueError(
            "逐帧动画总帧数 %d 超过上限 %d 帧：请降低帧率、减少场景或缩短旁白后重试"
            % (total, MAX_ANIMATED_FRAMES_TOTAL)
        )
    return frames


# ---------------------------------------------------------------------------
# 能力探测与限额
# ---------------------------------------------------------------------------

def video_limits() -> dict:
    return {
        "max_scenes": MAX_SCENES,
        "max_seconds": MAX_VIDEO_SECONDS,
        "max_bytes": MAX_VIDEO_BYTES,
        "fps_default": DEFAULT_FPS,
        "fps_min": MIN_FPS,
        "fps_max": MAX_FPS,
        "silent_scene_seconds": DEFAULT_SCENE_SECONDS,
        "width": VIDEO_WIDTH,
        "height": VIDEO_HEIGHT,
        "modes": list(VIDEO_MODES),
        "mode_default": DEFAULT_VIDEO_MODE,
        "transitions": list(TRANSITIONS),
        "transition_default": DEFAULT_TRANSITION,
        "transition_ms_default": DEFAULT_TRANSITION_MS,
        "transition_ms_min": MIN_TRANSITION_MS,
        "transition_ms_max": MAX_TRANSITION_MS,
        "max_frames_per_scene": MAX_ANIMATED_FRAMES_PER_SCENE,
        "max_frames_total": MAX_ANIMATED_FRAMES_TOTAL,
        "animated_audio": "aac",
    }


def ffmpeg_info() -> dict:
    """ffmpeg / ffprobe 可用性（不实际执行）。"""
    ffmpeg = shutil.which("ffmpeg") or ""
    ffprobe = shutil.which("ffprobe") or ""
    if not ffmpeg:
        return {
            "available": False,
            "path": "",
            "ffprobe": ffprobe,
            "detail": "未检测到 ffmpeg（系统 PATH），无法合成 MP4 视频",
        }
    detail = "可用：%s" % Path(ffmpeg).name
    if not ffprobe:
        detail += "（缺少 ffprobe，旁白时长按文本估算）"
    return {"available": True, "path": ffmpeg, "ffprobe": ffprobe, "detail": detail}


def video_capabilities() -> dict:
    """视频导出能力：Playwright 渲染 + ffmpeg 缺一不可。"""
    render_info = _render_module().render_available()
    ff = ffmpeg_info()
    render_ok = bool(render_info.get("available"))
    available = render_ok and bool(ff.get("available"))
    problems: list[str] = []
    if not render_ok:
        problems.append("服务端浏览器渲染不可用（%s）" % (render_info.get("detail") or "未安装 Playwright"))
    if not ff.get("available"):
        problems.append(str(ff.get("detail") or "未检测到 ffmpeg"))
    detail = (
        "可用：逐场景截图 + ffmpeg 合成 H.264 MP4（%d×%d；static 静帧 / animated 逐帧 + xfade 转场）"
        % (VIDEO_WIDTH, VIDEO_HEIGHT)
        if available
        else "；".join(problems) + "。可改用 PPTX / 离线 HTML 导出。"
    )
    return {
        "available": available,
        "detail": detail,
        "render": render_info,
        "ffmpeg": ff,
        "limits": video_limits(),
    }


def export_capabilities() -> dict:
    """导出对话框用：PDF（只需渲染）与 MP4（渲染 + ffmpeg）能力。"""
    render_info = _render_module().render_available()
    pdf_ok = bool(render_info.get("available"))
    return {
        "pdf": {
            "available": pdf_ok,
            "detail": (
                "可用：服务端渲染 A4 横向 PDF 讲义"
                if pdf_ok
                else "服务端 PDF 导出不可用：%s。可改用「导出离线 HTML」后在浏览器中打印为 PDF。"
                % (render_info.get("detail") or "浏览器渲染不可用")
            ),
        },
        "video": video_capabilities(),
        "limits": video_limits(),
    }


# ---------------------------------------------------------------------------
# 工作目录 / 场景摘要 / 旁白
# ---------------------------------------------------------------------------

def _work_root() -> Path:
    return Path(_db().DATA_DIR) / "classroom" / "video-work"


def _job_work_dir(job_id) -> Path:
    return _work_root() / ("job-%s" % job_id)


def _ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def _cleanup_stale_work_dirs(exclude: Path | None = None) -> None:
    """清理超过保留期的未完成任务工作目录（最佳努力，失败不影响导出）。"""
    root = _work_root()
    if not root.is_dir():
        return
    cutoff = time.time() - WORK_DIR_TTL_SECONDS
    try:
        exclude_resolved = exclude.resolve() if exclude is not None else None
        children = list(root.iterdir())
    except OSError:
        return
    for child in children:
        try:
            if not child.is_dir():
                continue
            if exclude_resolved is not None and child.resolve() == exclude_resolved:
                continue
            if child.stat().st_mtime < cutoff:
                shutil.rmtree(child, ignore_errors=True)
        except OSError:
            continue


def scene_digest(scene: dict) -> str:
    """场景内容摘要：内容/旁白动作/标题任一变更都会让 checkpoint 里的旧帧失效。"""
    payload = {
        "type": scene.get("scene_type") or "",
        "title": scene.get("title") or "",
        "content": scene.get("content_json") or "",
        "meta": scene.get("meta_json") or "",
    }
    return hashlib.sha256(cdb.json_dump(payload).encode("utf-8")).hexdigest()


def _course_config(course: dict) -> dict:
    config = cdb.json_load((course or {}).get("config_json"), {})
    return config if isinstance(config, dict) else {}


def _narration_map(course: dict) -> dict[int, dict]:
    """读取课程 ``config.narration``：``{scene_id(int): {asset_id,url,chars,...}}``。"""
    raw = _course_config(course).get("narration")
    if not isinstance(raw, dict):
        return {}
    result: dict[int, dict] = {}
    for key, value in raw.items():
        scene_id = _int(key)
        if scene_id > 0 and isinstance(value, dict):
            result[scene_id] = dict(value)
    return result


def _scene_speech_text(scene: dict) -> str:
    try:
        from .narration import scene_speech_text

        return scene_speech_text(scene)
    except Exception:
        return ""


def _estimate_scene_seconds(scene: dict, entry: dict | None) -> float:
    """旁白时长估算（ffprobe 不可用/失败时兜底）：中文旁白约 4 字/秒 + 0.8 秒收尾。"""
    chars = _int((entry or {}).get("chars")) or len(_scene_speech_text(scene))
    if chars <= 0:
        return DEFAULT_SCENE_SECONDS
    return max(DEFAULT_SCENE_SECONDS, chars / 4.0 + 0.8)


def _estimate_seconds(scenes: list[dict], narration: dict[int, dict]) -> float:
    """任务准入用的总时长粗估（仅用于提前拒绝明显超限的课程）。"""
    total = 0.0
    for scene in scenes:
        entry = narration.get(int(scene["id"]))
        if entry:
            total += _estimate_scene_seconds(scene, entry)
        else:
            total += DEFAULT_SCENE_SECONDS
    return round(total, 3)


def _audio_file_suffix(mime: str) -> str:
    return cexports._asset_suffix(mime or "audio/mpeg")


def _extract_narration_audio(user_id: int, scenes: list[dict], narration: dict[int, dict],
                             work_dir: Path) -> dict[int, Path]:
    """把旁白音频素材复制到工作目录（读失败/素材丢失的场景当作无旁白，记 warning）。"""
    paths: dict[int, Path] = {}
    for position, scene in enumerate(scenes, 1):
        scene_id = int(scene["id"])
        entry = narration.get(scene_id)
        if not entry:
            continue
        asset_id = _int(entry.get("asset_id"))
        if asset_id <= 0:
            continue
        try:
            row = cdb.owned_asset(asset_id, user_id)
            data = storage.read_file(tts.TTS_FOLDER, row["storage_name"])
        except Exception:
            log.warning("Narration asset %s for scene %s unavailable", asset_id, scene_id)
            continue
        if not data:
            continue
        path = work_dir / ("audio-%03d%s" % (position, _audio_file_suffix(row.get("mime_type") or "")))
        try:
            path.write_bytes(data)
        except OSError:
            continue
        paths[scene_id] = path
    return paths


# ---------------------------------------------------------------------------
# 截帧（Playwright）与 ffmpeg 合成
# ---------------------------------------------------------------------------

async def _capture_scene_pngs(html: str, scene_count: int, *, indices=None,
                              width: int = VIDEO_WIDTH, height: int = VIDEO_HEIGHT,
                              on_frame=None, is_cancelled=None) -> dict[int, bytes]:
    """打开离线播放器并逐场景截图：``{index: png_bytes}``。

    ``indices`` 为空时截取全部场景（续跑时只补缺失帧）；``on_frame(index, png)`` 用于
    落盘 + 更新 checkpoint（可为协程），``is_cancelled()`` 在每帧之间检查取消。
    """
    ai_render = _render_module()
    info = ai_render.render_available()
    if not info.get("available"):
        raise ai_render.RenderUnavailable(info.get("detail") or "浏览器渲染不可用")
    wanted = sorted({
        int(index) for index in (indices if indices is not None else range(scene_count))
        if 0 <= int(index) < int(scene_count)
    })
    frames: dict[int, bytes] = {}
    if not wanted:
        return frames
    timeout = int(getattr(ai_render, "RENDER_TIMEOUT_MS", 45000) or 45000)
    from playwright.async_api import async_playwright

    try:
        async with async_playwright() as playwright:
            browser = await ai_render._launch(playwright)
            try:
                page = await browser.new_page(viewport={"width": int(width), "height": int(height)})
                await page.set_content(html, wait_until="load", timeout=timeout)
                await page.wait_for_function(
                    "() => window.__zhixuCapture && window.__zhixuCapture.ready === true",
                    timeout=timeout,
                )
                total = _int(await page.evaluate("window.__zhixuCapture.sceneCount"))
                usable = [index for index in wanted if index < total]
                if not usable:
                    raise ai_render.RenderUnavailable("离线播放器没有可截图的场景")
                try:
                    await page.evaluate(
                        "() => (document.fonts && document.fonts.ready) ? document.fonts.ready.then(() => true) : true"
                    )
                except Exception:  # 字体就绪等待失败不影响截图
                    pass
                for index in usable:
                    if is_cancelled is not None and is_cancelled():
                        raise asyncio.CancelledError()
                    await page.evaluate("(index) => window.__zhixuCapture.gotoScene(index)", index)
                    try:
                        await page.wait_for_function(
                            "(index) => window.__zhixuCapture.current === index",
                            arg=index, timeout=5000,
                        )
                    except Exception:  # 切场标记未及时更新时仍等待固定时间后截图
                        pass
                    await page.wait_for_timeout(max(0, CAPTURE_SETTLE_MS))
                    png = await page.screenshot(type="png")
                    if not png or not png.startswith(b"\x89PNG"):
                        raise ai_render.RenderUnavailable("场景截图失败（返回内容不是 PNG）")
                    frames[index] = png
                    if on_frame is not None:
                        result = on_frame(index, png)
                        if asyncio.iscoroutine(result):
                            await result
                return frames
            finally:
                await browser.close()
    except (ai_render.RenderUnavailable, asyncio.CancelledError):
        raise
    except Exception as exc:  # noqa: BLE001 - 浏览器/页面异常统一转可读错误
        log.warning("video capture failed: %s", type(exc).__name__)
        raise ai_render.RenderUnavailable("视频截帧失败（%s）" % type(exc).__name__) from exc


# ---------------------------------------------------------------------------
# 逐帧动画截帧（animated 模式）：seekMs(t) 采样 + checkpoint 按场景复用
# ---------------------------------------------------------------------------

class AnimatedUnavailable(RuntimeError):
    """离线播放器不支持逐帧动画（缺少 seekMs/sceneDurationMs 或首次调用失败）。

    仅用于「seek 接口不可用」的降级路径：调用方应回退 static 并在结果里记录
    ``mode_used='static'`` 与 ``fallback_reason``；其它渲染错误仍按 ``RenderUnavailable`` 处理。
    """


#: 探测离线播放器 capture API（供 animated 模式决定是否降级）。
_ANIMATED_CAPS_JS = """
() => {
  var capture = window.__zhixuCapture || null;
  return {
    present: !!capture,
    seek: !!(capture && typeof capture.seekMs === 'function'),
    duration: !!(capture && typeof capture.sceneDurationMs === 'function'),
    sceneCount: capture && typeof capture.sceneCount === 'number' ? capture.sceneCount : 0
  };
}
"""

#: 逐帧推进：seekMs(t) → 等一次 rAF（样式/绘制提交）→ 返回当前时间。
_ANIMATED_SEEK_JS = """
(t) => {
  var capture = window.__zhixuCapture;
  capture.seekMs(t);
  return new Promise(function (resolve) {
    if (typeof requestAnimationFrame !== 'function') { resolve(t); return; }
    requestAnimationFrame(function () { resolve(t); });
  });
}
"""


def _reusable_frame_set(entry, directory: Path, *, frame_count: int, duration_ms: int, fps: int,
                        digest: str, audio_digest: str) -> bool:
    """checkpoint 场景帧集是否可复用：内容/旁白/时长/帧率/帧数一致且帧文件完整。"""
    if not isinstance(entry, dict):
        return False
    if _int(entry.get("frame_count")) != int(frame_count):
        return False
    if _int(entry.get("duration_ms")) != int(duration_ms):
        return False
    if _int(entry.get("fps")) != int(fps):
        return False
    if str(entry.get("digest") or "") != str(digest):
        return False
    if str(entry.get("audio_digest") or "") != str(audio_digest):
        return False
    if int(entry.get("bytes_total") or 0) <= 0:
        return False
    try:
        if not Path(directory).is_dir():
            return False
        files = sorted(Path(directory).glob("*" + ANIMATED_FRAME_SUFFIX))
    except OSError:
        return False
    if len(files) != int(frame_count):
        return False
    total = 0
    for path in files:
        try:
            total += path.stat().st_size
        except OSError:
            return False
    return total > 0


async def _animated_scene_frames(page, index: int, *, duration_ms: int, fps: int,
                                 image_dir: Path, on_frame=None, is_cancelled=None) -> int:
    """在已打开的离线播放器页面上按 fps 采样 ``seekMs(t)`` 并逐帧截图写盘（页面级函数，可单测）。

    - 帧数 = ``ceil(duration_ms × fps / 1000)``，时间点从 0 到 (帧数-1)×1000/fps；
    - 截图 JPEG 写入 ``image_dir/%05d.jpg``（ffmpeg image2 直接读序列）；
    - ``on_frame(index, frame_no, frame_total)`` 用于进度回调；``is_cancelled()`` 帧间检查取消；
    - 首次 ``seekMs`` 调用失败 → 抛 :class:`AnimatedUnavailable`（触发 static 降级）；
      之后失败 → ``RenderUnavailable``（真实渲染错误，不做降级）。
    """
    frames_dir = _ensure_dir(Path(image_dir))
    times = animated_frame_times(duration_ms, fps)
    total = len(times)
    seek_ok = False
    written = 0
    for frame_no, time_ms in enumerate(times):
        if is_cancelled is not None and is_cancelled():
            raise asyncio.CancelledError()
        try:
            await page.evaluate(_ANIMATED_SEEK_JS, time_ms)
            seek_ok = True
        except Exception as exc:  # noqa: BLE001 - 区分「接口不可用」与后续渲染错误
            if not seek_ok:
                raise AnimatedUnavailable(
                    "离线播放器逐帧动画接口调用失败（%s），已降级为静态画面" % type(exc).__name__
                ) from exc
            ai_render = _render_module()
            raise ai_render.RenderUnavailable(
                "逐帧动画 seekMs 调用失败（%s）" % type(exc).__name__
            ) from exc
        png = await page.screenshot(type="jpeg", quality=ANIMATED_FRAME_QUALITY)
        if not png or not png.startswith(b"\xff\xd8"):
            ai_render = _render_module()
            raise ai_render.RenderUnavailable("逐帧截图失败（返回内容不是 JPEG）")
        (frames_dir / ("%05d%s" % (frame_no, ANIMATED_FRAME_SUFFIX))).write_bytes(png)
        written += 1
        if on_frame is not None:
            result = on_frame(index, frame_no + 1, total)
            if asyncio.iscoroutine(result):
                await result
    return written


async def _capture_animated_frames(html: str, scene_labels: list[str], *, fps: int,
                                   durations_ms: dict[int, int] | None = None,
                                   work_dir: Path,
                                   scene_digests: list[str] | None = None,
                                   audio_digests: list[str] | None = None,
                                   resume_entries: dict[int, dict] | None = None,
                                   on_frame=None, on_scene_done=None, is_cancelled=None,
                                   width: int = VIDEO_WIDTH,
                                   height: int = VIDEO_HEIGHT) -> dict[int, dict]:
    """逐场景逐帧动画截图：``{index: {dir, frame_count, duration_ms, reused, ...}}``。

    - 打开离线播放器后先探测 ``seekMs/sceneDurationMs``；缺失（旧模板/旧课程 HTML）→
      抛 :class:`AnimatedUnavailable`，由 ``render_course_video`` 自动降级 static；
    - ``durations_ms`` 里已给定时长的场景（有旁白，以音频为时间基准）直接使用，其余用页面
      ``sceneDurationMs(i)`` 估算（最少 1500ms）；
    - ``resume_entries``（checkpoint ``anim.scenes``）按场景摘要/旁白摘要/时长/帧数校验后复用
      已完成帧目录，不再重截；
    - 取消时抛 ``asyncio.CancelledError``，未完成场景的帧目录由调用方清理。
    """
    ai_render = _render_module()
    info = ai_render.render_available()
    if not info.get("available"):
        raise ai_render.RenderUnavailable(info.get("detail") or "浏览器渲染不可用")
    labels = [str(item) for item in (scene_labels or [])]
    scene_count = len(labels)
    if scene_count <= 0:
        raise ai_render.RenderUnavailable("离线播放器没有可截图的场景")
    known = {}
    for key, value in (durations_ms or {}).items():
        known[int(key)] = max(int(MIN_SCENE_SECONDS * 1000), _int(value))
    digests = list(scene_digests or [""] * scene_count)
    audios = list(audio_digests or [""] * scene_count)
    resume = resume_entries if isinstance(resume_entries, dict) else {}
    timeout = int(getattr(ai_render, "RENDER_TIMEOUT_MS", 45000) or 45000)
    from playwright.async_api import async_playwright

    try:
        async with async_playwright() as playwright:
            browser = await ai_render._launch(playwright)
            try:
                page = await browser.new_page(viewport={"width": int(width), "height": int(height)})
                await page.set_content(html, wait_until="load", timeout=timeout)
                await page.wait_for_function(
                    "() => window.__zhixuCapture && window.__zhixuCapture.ready === true",
                    timeout=timeout,
                )
                try:
                    await page.evaluate(
                        "() => (document.fonts && document.fonts.ready) ? document.fonts.ready.then(() => true) : true"
                    )
                except Exception:  # 字体就绪等待失败不影响截图
                    pass
                caps = await page.evaluate(_ANIMATED_CAPS_JS) or {}
                if not caps.get("seek") or not caps.get("duration"):
                    missing = []
                    if not caps.get("present"):
                        missing.append("window.__zhixuCapture")
                    else:
                        if not caps.get("seek"):
                            missing.append("seekMs")
                        if not caps.get("duration"):
                            missing.append("sceneDurationMs")
                    raise AnimatedUnavailable(
                        "离线播放器缺少逐帧动画接口（%s），已降级为静态画面" % "、".join(missing)
                    )
                total_scenes = _int(caps.get("sceneCount"), scene_count) or scene_count
                scene_count = max(0, min(scene_count, total_scenes, len(labels)))
                if scene_count <= 0:
                    raise ai_render.RenderUnavailable("离线播放器没有可截图的场景")
                # 1) 场景时长：旁白场景用音频时长；其余用播放器 actions 时间轴估算
                durations: dict[int, int] = {}
                for index in range(scene_count):
                    if known.get(index, 0) > 0:
                        durations[index] = known[index]
                        continue
                    try:
                        value = _int(await page.evaluate(
                            "(index) => window.__zhixuCapture.sceneDurationMs(index)", index,
                        ))
                    except Exception:  # noqa: BLE001 - 估算失败回退 3 秒静帧时长
                        value = 0
                    durations[index] = max(MIN_SCENE_MS, value or int(DEFAULT_SCENE_SECONDS * 1000))
                # 2) 帧数计划 + 限额（播放器真实时长可能略不同于 route 估算，这里二次校验）
                frame_counts: dict[int, int] = {}
                for index in range(scene_count):
                    count = animated_frame_count(durations[index], fps)
                    if count > MAX_ANIMATED_FRAMES_PER_SCENE:
                        raise ValueError(
                            "第 %d 个场景需要 %d 帧，超过逐帧动画单场景上限 %d 帧："
                            "请降低帧率或缩短旁白后重试"
                            % (index + 1, count, MAX_ANIMATED_FRAMES_PER_SCENE)
                        )
                    frame_counts[index] = count
                if sum(frame_counts.values()) > MAX_ANIMATED_FRAMES_TOTAL:
                    raise ValueError(
                        "逐帧动画总帧数 %d 超过上限 %d 帧：请降低帧率、减少场景或缩短旁白后重试"
                        % (sum(frame_counts.values()), MAX_ANIMATED_FRAMES_TOTAL)
                    )
                # 3) 逐场景截图（checkpoint 命中则复用已完成帧目录）
                frames: dict[int, dict] = {}
                for index in range(scene_count):
                    if is_cancelled is not None and is_cancelled():
                        raise asyncio.CancelledError()
                    directory = Path(work_dir) / ANIMATED_FRAMES_DIR / ("s%03d" % index)
                    count = frame_counts[index]
                    digest = str(digests[index] if index < len(digests) else "")
                    audio_digest = str(audios[index] if index < len(audios) else "")
                    entry = {
                        "name": directory.name,
                        "dir": directory,
                        "frame_count": count,
                        "duration_ms": durations[index],
                        "fps": int(fps),
                        "digest": digest,
                        "audio_digest": audio_digest,
                        "reused": False,
                    }
                    if _reusable_frame_set(
                        resume.get(index), directory,
                        frame_count=count, duration_ms=durations[index], fps=fps,
                        digest=digest, audio_digest=audio_digest,
                    ):
                        entry["reused"] = True
                        entry["bytes_total"] = _int((resume.get(index) or {}).get("bytes_total"))
                        frames[index] = entry
                        if on_scene_done is not None:
                            result = on_scene_done(index, entry)
                            if asyncio.iscoroutine(result):
                                await result
                        continue
                    await page.evaluate("(index) => window.__zhixuCapture.gotoScene(index)", index)
                    try:
                        await page.wait_for_function(
                            "(index) => window.__zhixuCapture.current === index",
                            arg=index, timeout=5000,
                        )
                    except Exception:  # 切场标记未及时更新时仍等待固定时间后截图
                        pass
                    await page.wait_for_timeout(max(0, CAPTURE_SETTLE_MS))
                    if directory.exists():
                        shutil.rmtree(directory, ignore_errors=True)
                    written = await _animated_scene_frames(
                        page, index, duration_ms=durations[index], fps=fps,
                        image_dir=directory, on_frame=on_frame, is_cancelled=is_cancelled,
                    )
                    if written != count:
                        raise ai_render.RenderUnavailable(
                            "第 %d 个场景只写出 %d/%d 帧，无法合成视频"
                            % (index + 1, written, count)
                        )
                    try:
                        entry["bytes_total"] = sum(
                            path.stat().st_size for path in directory.glob("*" + ANIMATED_FRAME_SUFFIX)
                        )
                    except OSError:
                        entry["bytes_total"] = 0
                    frames[index] = entry
                    if on_scene_done is not None:
                        result = on_scene_done(index, entry)
                        if asyncio.iscoroutine(result):
                            await result
                return frames
            finally:
                await browser.close()
    except (AnimatedUnavailable, asyncio.CancelledError, ValueError):
        raise
    except ai_render.RenderUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001 - 浏览器/页面异常统一转可读错误
        log.warning("animated video capture failed: %s", type(exc).__name__)
        raise ai_render.RenderUnavailable("逐帧动画截帧失败（%s）" % type(exc).__name__) from exc


async def _probe_duration(path: Path | str) -> float | None:
    """用 ffprobe 读取媒体时长（秒）；不可用/失败返回 None（回退文本估算）。"""
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return None
    target = Path(path)
    if not target.is_file() or target.stat().st_size <= 0:
        return None
    args = [
        ffprobe, "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", str(target),
    ]
    try:
        process = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, _stderr = await asyncio.wait_for(
                process.communicate(), timeout=FFPROBE_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            return None
    except OSError:
        return None
    text = (stdout or b"").decode("utf-8", "replace").strip()
    try:
        value = float(text)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


async def _run_ffmpeg(args: list[str], *, timeout: float | None = None) -> None:
    """执行 ffmpeg（失败/超时抛中文 ``ValueError``，由任务框架展示给用户）。"""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise ValueError("未检测到 ffmpeg，无法合成 MP4 视频")
    command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", *[str(item) for item in args]]
    try:
        process = await asyncio.create_subprocess_exec(
            *command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        raise ValueError("无法启动 ffmpeg：%s" % type(exc).__name__) from exc
    limit = float(timeout or FFMPEG_TIMEOUT_SECONDS)
    try:
        _stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=limit)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        raise ValueError("ffmpeg 执行超时（%d 秒），已中止视频合成" % int(limit)) from None
    if process.returncode != 0:
        tail = [
            line.strip()
            for line in (stderr or b"").decode("utf-8", "replace").splitlines()
            if line.strip()
        ][-3:]
        raise ValueError("ffmpeg 合成失败：%s" % (" / ".join(tail) or ("退出码 %s" % process.returncode)))


def segment_ffmpeg_args(*, frame_path: Path | str, segment_path: Path | str, duration: float,
                        fps: int, audio_path: Path | str | None = None) -> list[str]:
    """单个场景片段：静帧 + （旁白音频或静音轨）→ H.264/AAC mp4。"""
    safe_duration = max(MIN_SCENE_SECONDS, float(duration or 0))
    args = ["-loop", "1", "-framerate", str(int(fps)), "-i", str(frame_path)]
    if audio_path:
        args.extend(["-i", str(audio_path)])
    else:
        args.extend(["-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100"])
    args.extend([
        "-map", "0:v:0", "-map", "1:a:0",
        "-c:v", "libx264", "-preset", "veryfast", "-tune", "stillimage",
        "-pix_fmt", "yuv420p", "-r", str(int(fps)),
        "-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "2",
        "-t", "%.3f" % safe_duration,
    ])
    if audio_path:
        # 旁白音频长度即场景时长：-shortest 保证音画同时结束（与 -t 双保险）。
        args.append("-shortest")
    args.extend(["-movflags", "+faststart", str(segment_path)])
    return args


def video_only_segment_ffmpeg_args(*, frame_path: Path | str, segment_path: Path | str,
                                   duration: float, fps: int) -> list[str]:
    """无任何旁白时的纯视频片段（不带音轨）。"""
    safe_duration = max(MIN_SCENE_SECONDS, float(duration or 0))
    return [
        "-loop", "1", "-framerate", str(int(fps)), "-i", str(frame_path),
        "-t", "%.3f" % safe_duration,
        "-c:v", "libx264", "-preset", "veryfast", "-tune", "stillimage",
        "-pix_fmt", "yuv420p", "-r", str(int(fps)),
        "-movflags", "+faststart",
        str(segment_path),
    ]


def concat_ffmpeg_args(*, concat_path: Path | str, output_path: Path | str) -> list[str]:
    return [
        "-f", "concat", "-safe", "0", "-i", str(concat_path),
        "-c", "copy", "-movflags", "+faststart", str(output_path),
    ]


# ---------------------------------------------------------------------------
# animated：逐帧片段与场景转场（ffmpeg xfade / acrossfade）
# ---------------------------------------------------------------------------

def animated_segment_ffmpeg_args(*, frame_dir: Path | str, segment_path: Path | str,
                                 duration: float, fps: int,
                                 audio_path: Path | str | None = None) -> list[str]:
    """单个场景的逐帧片段：JPEG 序列（``00000.jpg``…）+（旁白音频或静音）→ H.264/AAC mp4。

    ``duration`` 取 ``帧数 / fps``，与 xfade 计算使用的片段时长严格一致；音频用 ``apad``
    补齐（旁白不足时补静音）后再按 ``-t`` 截断，保证音画同长。
    """
    safe_duration = max(MIN_SCENE_SECONDS, float(duration or 0))
    pattern = str(Path(frame_dir) / ("%05d" + ANIMATED_FRAME_SUFFIX))
    args = ["-framerate", str(int(fps)), "-start_number", "0", "-i", pattern]
    if audio_path:
        args.extend(["-i", str(audio_path)])
    else:
        args.extend(["-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100"])
    args.extend([
        "-map", "0:v:0", "-map", "1:a:0",
        "-c:v", "libx264", "-preset", "veryfast", "-tune", "animation",
        "-pix_fmt", "yuv420p", "-r", str(int(fps)),
        "-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "2",
        "-af", "apad", "-t", "%.3f" % safe_duration,
        "-movflags", "+faststart", str(segment_path),
    ])
    return args


def xfade_offsets(durations: list[float], transition_seconds: float) -> list[float]:
    """链式 xfade 每段的 offset：第 k 段（k 从 1 计）= 前 k 段时长和 − k × 转场时长。"""
    offsets: list[float] = []
    cumulative = 0.0
    for index, duration in enumerate(durations or []):
        if index:
            offsets.append(round(cumulative - index * float(transition_seconds), 6))
        cumulative += float(duration or 0)
    return offsets


def effective_transition_seconds(durations: list[float], transition_ms: int) -> float:
    """实际转场时长：不超过最短场景的一半（避免 xfade 窗口重叠导致命令失败）。"""
    requested = max(0.0, float(_int(transition_ms)) / 1000.0)
    if len(durations or []) < 2 or requested <= 0:
        return 0.0
    shortest = min(float(item or 0) for item in durations)
    return round(min(requested, max(0.0, shortest * 0.5)), 3)


def xfade_ffmpeg_args(*, segment_paths: list[Path | str], durations: list[float],
                      transition_seconds: float, output_path: Path | str,
                      fps: int, transition: str = TRANSITION_FADE) -> list[str]:
    """多场景片段 → xfade 视频转场 + acrossfade 音频转场 → 单个 H.264/AAC mp4。"""
    paths = [str(Path(item)) for item in (segment_paths or [])]
    if len(paths) < 2:
        raise ValueError("xfade 至少需要 2 个场景片段")
    transition_name = str(transition or TRANSITION_FADE).strip().lower()
    if transition_name not in TRANSITIONS or transition_name == TRANSITION_NONE:
        transition_name = TRANSITION_FADE
    filters: list[str] = []
    offsets = xfade_offsets([float(item or 0) for item in durations], transition_seconds)
    video_prev = "[0:v]"
    for index, offset in enumerate(offsets, start=1):
        video_out = "[v%d]" % index
        filters.append(
            "%s[%d:v]xfade=transition=%s:duration=%.3f:offset=%.3f%s"
            % (video_prev, index, transition_name, float(transition_seconds), float(offset), video_out)
        )
        video_prev = video_out
    audio_prev = "[0:a]"
    for index in range(1, len(paths)):
        audio_out = "[a%d]" % index
        filters.append(
            "%s[%d:a]acrossfade=d=%.3f:c1=tri:c2=tri%s"
            % (audio_prev, index, float(transition_seconds), audio_out)
        )
        audio_prev = audio_out
    inputs: list[str] = []
    for path in paths:
        inputs.extend(["-i", path])
    return [
        *inputs,
        "-filter_complex", ";".join(filters),
        "-map", video_prev, "-map", audio_prev,
        "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", "-r", str(int(fps)),
        "-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "2",
        "-movflags", "+faststart", str(output_path),
    ]


def _write_concat_file(concat_path: Path, segments: list[Path]) -> None:
    lines: list[str] = []
    for segment in segments:
        rendered = str(Path(segment).resolve()).replace("\\", "/").replace("'", "'\\''")
        lines.append("file '%s'" % rendered)
    concat_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _cleanup_partial_files(work_dir: Path) -> None:
    """删除半成品（片段 / 合并中的 mp4 / concat 清单），已完成帧保留供续跑复用。"""
    for pattern in ("seg-*.mp4", "anim-seg-*.mp4", "course.mp4", "segments.txt"):
        for path in work_dir.glob(pattern):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                continue


def _cleanup_animated_partial(work_dir: Path, checkpoint: dict | None = None) -> None:
    """取消/失败：删除未完成场景的帧目录与半成品片段；已完成场景帧保留供 checkpoint 复用。"""
    completed: set[str] = set()
    cp = checkpoint if isinstance(checkpoint, dict) else {}
    anim = cp.get("anim") if isinstance(cp.get("anim"), dict) else {}
    scenes = anim.get("scenes") if isinstance(anim.get("scenes"), dict) else {}
    for entry in scenes.values():
        if isinstance(entry, dict) and entry.get("name"):
            completed.add(str(entry["name"]))
    frames_root = Path(work_dir) / ANIMATED_FRAMES_DIR
    try:
        if frames_root.is_dir():
            for child in frames_root.iterdir():
                if not child.is_dir() or child.name in completed:
                    continue
                shutil.rmtree(child, ignore_errors=True)
    except OSError:
        pass
    _cleanup_partial_files(Path(work_dir))


# ---------------------------------------------------------------------------
# 素材落库
# ---------------------------------------------------------------------------

def _asset_public(row) -> dict:
    meta = cdb.json_load(row.get("meta_json"), {})
    return {
        "id": int(row["id"]),
        "kind": row["kind"],
        "mime_type": row["mime_type"],
        "url": "/api/classroom/assets/%d" % int(row["id"]),
        "meta": meta if isinstance(meta, dict) else {},
        "created_at": row["created_at"],
    }


def _latest_video_asset(course_id: int, user_id: int) -> dict | None:
    with _db().connection() as con:
        row = con.execute(
            "SELECT * FROM cls_assets WHERE user_id=? AND course_id=? AND kind=? ORDER BY id DESC LIMIT 1",
            (user_id, int(course_id), VIDEO_KIND),
        ).fetchone()
    return dict(row) if row else None


def _save_video_asset(*, user_id: int, course: dict, data: bytes, duration_seconds: float,
                      fps: int, scene_count: int, has_audio: bool,
                      narrated_scene_count: int, truncated: bool,
                      mode: str = VIDEO_MODE_STATIC,
                      transition: str = TRANSITION_NONE,
                      transition_ms: int = 0,
                      frame_count: int = 0,
                      fallback_reason: str = "") -> dict:
    """视频产物按 sha256 内容寻址落库（同用户同视频只存一份）。"""
    db = _db()
    digest = hashlib.sha256(data).hexdigest()
    with db.connection() as con:
        existing = con.execute(
            "SELECT * FROM cls_assets WHERE user_id=? AND kind=? AND sha256=? ORDER BY id LIMIT 1",
            (user_id, VIDEO_KIND, digest),
        ).fetchone()
    if existing is not None:
        return dict(existing)
    storage_name = "video-%s.mp4" % digest[:32]
    storage.save_file(VIDEO_FOLDER, storage_name, data, VIDEO_MIME)
    meta = {
        "course_id": int(course["id"]),
        "course_title": course.get("title") or "",
        "fps": int(fps),
        "scene_count": int(scene_count),
        "duration_seconds": round(float(duration_seconds), 3),
        "width": VIDEO_WIDTH,
        "height": VIDEO_HEIGHT,
        "has_audio": bool(has_audio),
        "narrated_scene_count": int(narrated_scene_count),
        "size": len(data),
        "truncated": bool(truncated),
        "mode": str(mode),
        "animated": str(mode) == VIDEO_MODE_ANIMATED,
        "frame_count": int(frame_count),
        "transition": str(transition),
        "transition_ms": int(transition_ms),
        "fallback_reason": str(fallback_reason or ""),
        "source": "classroom-video",
        "encoder": "ffmpeg/libx264",
    }
    now = db.utcnow()
    with db.connection() as con:
        asset_id = con.execute(
            "INSERT INTO cls_assets(user_id,course_id,kind,storage_name,mime_type,sha256,meta_json,created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (user_id, int(course["id"]), VIDEO_KIND, storage_name, VIDEO_MIME, digest,
             cdb.json_dump(meta), now),
        ).lastrowid
        row = con.execute("SELECT * FROM cls_assets WHERE id=?", (asset_id,)).fetchone()
    return dict(row)


# ---------------------------------------------------------------------------
# 视频渲染主流程（任务执行器与测试共用）
# ---------------------------------------------------------------------------

def _animated_audio_digest(path: Path | None) -> str:
    """旁白音频摘要（animated checkpoint 帧集复用判据；无旁白记为 ``none``）。"""
    if path is None:
        return "none"
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return "unreadable"


async def render_course_video(course_id: int, user_id: int, *, fps: int = DEFAULT_FPS,
                              max_scenes: int = MAX_SCENES, out_format: str = "mp4",
                              mode: str = DEFAULT_VIDEO_MODE,
                              transition: str = DEFAULT_TRANSITION,
                              transition_ms: int | None = None,
                              job_id: int | None = None, checkpoint: dict | None = None,
                              on_stage=None, on_checkpoint=None,
                              is_cancelled=None) -> dict:
    """把课程渲染为 MP4 教学视频并落库为 ``cls_assets`` 素材。

    ``mode='static'``（默认，向后兼容）逐场景静帧 + 旁白音轨；``mode='animated'`` 按 fps
    逐帧采样 ``seekMs(t)`` 截图并用 ffmpeg ``xfade`` 做场景转场；``transition`` 取
    ``none|fade``，``transition_ms`` 取 100–1000（默认 400ms）。
    ``animated`` 下若离线播放器缺少 seek 接口 → 自动降级 static 并记录 ``fallback_reason``。

    返回 ``{asset, asset_id, url, mime_type, duration_seconds, scene_count, fps, size_bytes,
    has_audio, narrated_scene_count, reused_frames, captured_frames, truncated, warnings,
    mode, mode_used, transition, transition_ms, transition_effective_ms, frame_count,
    fallback_reason, summary}``。取消（``asyncio.CancelledError``）时删除半成品与未完成
    场景帧、保留已完成帧；其余异常同样保留已完成帧。
    """
    fps = _int(fps, DEFAULT_FPS)
    max_scenes = _int(max_scenes, MAX_SCENES)
    requested_mode = _resolve_video_mode(mode)
    transition = _resolve_transition(transition)
    transition_ms = _resolve_transition_ms(transition_ms)
    mode_used = requested_mode
    fallback_reason = ""
    animated = requested_mode == VIDEO_MODE_ANIMATED
    if str(out_format or "").strip().lower() != "mp4":
        raise ValueError("视频导出只支持 mp4 格式")
    if not MIN_FPS <= fps <= MAX_FPS:
        raise ValueError("视频帧率必须在 %d–%d 之间" % (MIN_FPS, MAX_FPS))
    if not 1 <= max_scenes <= MAX_SCENES:
        raise ValueError("单次视频导出最多 %d 个场景" % MAX_SCENES)
    course = cdb.find_course(int(course_id), int(user_id))
    if course is None:
        raise ValueError("课程不存在或无权访问")
    all_scenes = cdb.course_scenes(int(course_id), int(user_id))
    if not all_scenes:
        raise ValueError("课程还没有场景，无法导出视频")
    scenes = all_scenes[:max_scenes]
    truncated = len(all_scenes) > len(scenes)

    capabilities = video_capabilities()
    if not capabilities["available"]:
        raise ValueError("视频导出不可用：%s" % capabilities["detail"])

    async def stage(text: str, done: int, total: int, **extra) -> None:
        if on_stage is None:
            return
        result = on_stage(text, int(done), int(total), **extra)
        if asyncio.iscoroutine(result):
            await result

    await stage("准备导出视频", 0, len(scenes))
    _cleanup_stale_work_dirs()
    if job_id is not None:
        work_dir = _ensure_dir(_job_work_dir(job_id))
    else:
        work_dir = _ensure_dir(_work_root() / ("render-%s" % uuid.uuid4().hex[:12]))

    frames: dict[int, Path] = {}
    reused_frames = 0
    captured_frames = 0
    frame_count = 0
    transition_effective_ms = 0
    segment_durations: list[float] = []
    total_seconds = 0.0
    cp = checkpoint if isinstance(checkpoint, dict) else {}
    try:
        # ---- checkpoint 续跑：场景清单/帧率一致且场景内容摘要未变时复用已完成静帧 ----
        cp_scene_ids = [_int(item) for item in (cp.get("scene_ids") or [])]
        resume_ok = (
            _int(cp.get("course_id")) == int(course_id)
            and _int(cp.get("fps")) == fps
            and cp_scene_ids == [int(scene["id"]) for scene in scenes]
        )
        if (not animated) and resume_ok and isinstance(cp.get("frames"), dict):
            for index, scene in enumerate(scenes):
                entry = cp["frames"].get(str(int(scene["id"])))
                if not isinstance(entry, dict):
                    continue
                path = work_dir / str(entry.get("name") or "")
                if not path.is_file():
                    continue
                if str(entry.get("digest") or "") != scene_digest(scene):
                    continue
                expected_bytes = _int(entry.get("bytes"))
                if expected_bytes and path.stat().st_size != expected_bytes:
                    continue
                frames[index] = path
                reused_frames += 1
        if not isinstance(cp.get("frames"), dict):
            cp["frames"] = {}
        cp.update({
            "course_id": int(course_id),
            "fps": fps,
            "max_scenes": max_scenes,
            "scene_ids": [int(scene["id"]) for scene in scenes],
            "work_dir": work_dir.name,
            "mode": requested_mode,
        })
        anim_cp = cp.get("anim") if isinstance(cp.get("anim"), dict) else {}
        if not isinstance(anim_cp.get("scenes"), dict):
            anim_cp["scenes"] = {}
        anim_cp["fps"] = fps
        cp["anim"] = anim_cp

        # ---- 1) 旁白音频 → 每场景时长（animated 旁白场景以音频为时间基准）----
        narration = _narration_map(course)
        audio_paths = _extract_narration_audio(int(user_id), scenes, narration, work_dir)
        durations: dict[int, float] = {}
        warnings: list[str] = list(cp.get("warnings") or [])
        for position, scene in enumerate(scenes, 1):
            scene_id = int(scene["id"])
            audio_path = audio_paths.get(scene_id)
            if audio_path is not None:
                probed = await _probe_duration(audio_path)
                if probed is None:
                    probed = _estimate_scene_seconds(scene, narration.get(scene_id))
                    message = "场景《%s》旁白音频时长读取失败，已按文本估算" % (scene.get("title") or position)
                    if message not in warnings:
                        warnings.append(message)
                durations[scene_id] = max(MIN_SCENE_SECONDS, float(probed))
            else:
                durations[scene_id] = DEFAULT_SCENE_SECONDS
        total_seconds = round(sum(durations.values()), 3)
        if total_seconds > MAX_VIDEO_SECONDS:
            raise ValueError(
                "视频总时长约 %d 秒，超过 %d 分钟上限：请减少场景或缩短旁白后重试"
                % (int(total_seconds), int(MAX_VIDEO_SECONDS // 60))
            )
        has_audio = bool(audio_paths)
        narrated_scene_count = len(audio_paths)
        cp["warnings"] = warnings
        if on_checkpoint is not None:
            on_checkpoint(cp)
        await stage("视频时长约 %d 秒，开始渲染画面" % int(round(total_seconds)), 0, len(scenes))

        # ---- 构建离线单文件播放器（static 只补缺失帧时用；animated 逐帧截图一直用）----
        snapshot = cexports.build_snapshot(
            course, cexports._inline_scene_images(scenes, int(user_id)), for_html=True,
        )
        snapshot_scenes = snapshot.get("scenes") if isinstance(snapshot.get("scenes"), list) else []
        for position, scene in enumerate(scenes):
            if position >= len(snapshot_scenes):
                break
            scene_id = int(scene["id"])
            if scene_id in audio_paths:
                # 旁白场景以音频时长为时间基准（播放器 sceneDurationMs 优先返回该值）
                snapshot_scenes[position]["durationMs"] = int(round(durations[scene_id] * 1000))
        html_text = cexports.build_offline_html(snapshot, None)

        # ---- 2) animated：逐场景逐帧截图（checkpoint 按场景复用已完成帧）----
        animation: dict[int, dict] = {}
        if animated:
            planned_frames_total = 0
            for index, scene in enumerate(scenes):
                scene_id = int(scene["id"])
                duration_ms = (
                    int(round(durations[scene_id] * 1000))
                    if scene_id in audio_paths else estimate_scene_duration_ms(scene)
                )
                planned_frames_total += animated_frame_count(duration_ms, fps)

            async def frame_progress(index: int, frame_no: int, frame_total: int) -> None:
                nonlocal captured_frames
                captured_frames += 1
                await stage(
                    "渲染第 %d/%d 场景 · 第 %d/%d 帧"
                    % (index + 1, len(scenes), frame_no, frame_total),
                    captured_frames, planned_frames_total or len(scenes),
                    scene_id=int(scenes[index]["id"]),
                )

            async def scene_done(index: int, info: dict) -> None:
                nonlocal reused_frames, frame_count
                scene = scenes[index]
                scene_id = int(scene["id"])
                current = cp.get("anim")
                if not isinstance(current, dict):
                    current = {"scenes": {}}
                if not isinstance(current.get("scenes"), dict):
                    current["scenes"] = {}
                current["scenes"][str(scene_id)] = {
                    "name": str(info.get("name") or Path(info.get("dir") or "").name),
                    "frame_count": _int(info.get("frame_count")),
                    "duration_ms": _int(info.get("duration_ms")),
                    "fps": fps,
                    "digest": str(info.get("digest") or ""),
                    "audio_digest": str(info.get("audio_digest") or ""),
                    "bytes_total": _int(info.get("bytes_total")),
                    "reused": bool(info.get("reused")),
                }
                current.update({"fps": fps, "transition": transition, "transition_ms": transition_ms})
                cp["anim"] = current
                frame_count += _int(info.get("frame_count"))
                if info.get("reused"):
                    reused_frames += _int(info.get("frame_count"))
                if on_checkpoint is not None:
                    on_checkpoint(cp)

            try:
                await stage("开始渲染逐帧动画", 0, len(scenes))
                resume_entries: dict[int, dict] = {}
                if resume_ok and _int(anim_cp.get("fps")) == fps:
                    anim_scenes = anim_cp.get("scenes") if isinstance(anim_cp.get("scenes"), dict) else {}
                    for index, scene in enumerate(scenes):
                        entry = anim_scenes.get(str(int(scene["id"])))
                        if isinstance(entry, dict):
                            resume_entries[index] = entry
                animation = await _capture_animated_frames(
                    html_text,
                    [str(scene.get("title") or ("场景 %d" % (index + 1)))
                     for index, scene in enumerate(scenes)],
                    fps=fps,
                    durations_ms={
                        index: int(round(durations[int(scene["id"])] * 1000))
                        for index, scene in enumerate(scenes)
                        if int(scene["id"]) in audio_paths
                    },
                    work_dir=work_dir,
                    scene_digests=[scene_digest(scene) for scene in scenes],
                    audio_digests=[
                        _animated_audio_digest(audio_paths.get(int(scene["id"]))) for scene in scenes
                    ],
                    resume_entries=resume_entries,
                    on_frame=frame_progress,
                    on_scene_done=scene_done,
                    is_cancelled=is_cancelled,
                )
            except AnimatedUnavailable as exc:
                # seek 接口不可用（旧模板/旧课程 HTML）→ 自动回退 static，并在结果里标注
                animated = False
                mode_used = VIDEO_MODE_STATIC
                fallback_reason = str(exc)
                if fallback_reason not in warnings:
                    warnings.append(fallback_reason)
                cp["warnings"] = warnings
                cp["mode_used"] = mode_used
                cp["fallback_reason"] = fallback_reason
                if on_checkpoint is not None:
                    on_checkpoint(cp)
                await stage("逐帧动画不可用，已降级为静态画面", 0, len(scenes))
            except Exception as exc:  # noqa: BLE001 - 与 static 路径一致的中文错误映射
                if isinstance(exc, asyncio.CancelledError):
                    raise
                render_unavailable = getattr(_render_module(), "RenderUnavailable", RuntimeError)
                if isinstance(exc, render_unavailable):
                    raise ValueError("视频渲染不可用：%s" % exc) from exc
                raise

            if animated:
                # ---- 2.1) 每场景：JPEG 序列 + 音轨 → 片段；fade 转场用 xfade 一次合成 ----
                segments: list[Path] = []
                for index, scene in enumerate(scenes):
                    if is_cancelled is not None and is_cancelled():
                        raise asyncio.CancelledError()
                    scene_id = int(scene["id"])
                    info = animation.get(index)
                    if not isinstance(info, dict):
                        raise ValueError("第 %d 个场景没有成功截帧，无法合成视频" % (index + 1))
                    segment_path = work_dir / ("anim-seg-%03d.mp4" % index)
                    seconds = max(MIN_SCENE_SECONDS, _int(info.get("frame_count")) / float(fps))
                    await stage(
                        "合成视频片段 %d/%d：%s" % (index + 1, len(scenes), scene.get("title") or ""),
                        index, len(scenes), scene_id=scene_id,
                    )
                    await _run_ffmpeg(animated_segment_ffmpeg_args(
                        frame_dir=info["dir"], segment_path=segment_path,
                        duration=seconds, fps=fps, audio_path=audio_paths.get(scene_id),
                    ))
                    if not segment_path.is_file() or segment_path.stat().st_size <= 0:
                        raise ValueError("ffmpeg 未生成第 %d 个视频片段" % (index + 1))
                    segments.append(segment_path)
                    segment_durations.append(round(seconds, 3))
                if is_cancelled is not None and is_cancelled():
                    raise asyncio.CancelledError()
                output_path = work_dir / "course.mp4"
                transition_effective_ms = int(round(
                    effective_transition_seconds(segment_durations, transition_ms) * 1000
                ))
                if transition == TRANSITION_FADE and len(segments) > 1 and transition_effective_ms > 0:
                    await stage(
                        "合成视频（转场）：%d 个场景 · fade %d ms"
                        % (len(segments), transition_effective_ms),
                        len(scenes), len(scenes),
                    )
                    await _run_ffmpeg(xfade_ffmpeg_args(
                        segment_paths=segments, durations=segment_durations,
                        transition_seconds=transition_effective_ms / 1000.0,
                        output_path=output_path, fps=fps, transition=transition,
                    ))
                else:
                    transition_effective_ms = 0
                    concat_path = work_dir / "segments.txt"
                    _write_concat_file(concat_path, segments)
                    await stage("合并视频片段", len(scenes), len(scenes))
                    await _run_ffmpeg(concat_ffmpeg_args(
                        concat_path=concat_path, output_path=output_path,
                    ))
                if not output_path.is_file() or output_path.stat().st_size <= 0:
                    raise ValueError("ffmpeg 未生成视频文件")
                total_seconds = round(
                    sum(segment_durations)
                    - max(0, len(segment_durations) - 1) * transition_effective_ms / 1000.0,
                    3,
                )

        # ---- 3) static（默认原路径）：逐场景静帧 → 片段 → concat 合并 ----
        if not animated:
            missing = [index for index in range(len(scenes)) if index not in frames]
            if missing:
                async def save_frame(index: int, png: bytes) -> None:
                    nonlocal captured_frames
                    scene = scenes[index]
                    name = "frame-%03d.png" % index
                    path = work_dir / name
                    path.write_bytes(png)
                    cp["frames"][str(int(scene["id"]))] = {
                        "name": name,
                        "digest": scene_digest(scene),
                        "bytes": len(png),
                    }
                    captured_frames += 1
                    if on_checkpoint is not None:
                        on_checkpoint(cp)
                    await stage(
                        "渲染第 %d/%d 场景：%s" % (index + 1, len(scenes), scene.get("title") or ""),
                        index + 1, len(scenes), scene_id=int(scene["id"]),
                    )

                try:
                    captured = await _capture_scene_pngs(
                        html_text, len(scenes), indices=missing,
                        on_frame=save_frame, is_cancelled=is_cancelled,
                    )
                except Exception as exc:
                    if isinstance(exc, asyncio.CancelledError):
                        raise
                    render_unavailable = getattr(_render_module(), "RenderUnavailable", RuntimeError)
                    if isinstance(exc, render_unavailable):
                        raise ValueError("视频渲染不可用：%s" % exc) from exc
                    raise
                for index in captured:
                    frames[index] = work_dir / ("frame-%03d.png" % index)
                if is_cancelled is not None and is_cancelled():
                    raise asyncio.CancelledError()
            for index in range(len(scenes)):
                if index not in frames:
                    raise ValueError("第 %d 个场景没有成功截图，无法合成视频" % (index + 1))
            if is_cancelled is not None and is_cancelled():
                raise asyncio.CancelledError()

            segments = []
            for index, scene in enumerate(scenes):
                if is_cancelled is not None and is_cancelled():
                    raise asyncio.CancelledError()
                scene_id = int(scene["id"])
                segment_path = work_dir / ("seg-%03d.mp4" % index)
                duration = durations.get(scene_id, DEFAULT_SCENE_SECONDS)
                await stage(
                    "合成视频片段 %d/%d：%s" % (index + 1, len(scenes), scene.get("title") or ""),
                    index, len(scenes), scene_id=scene_id,
                )
                if has_audio:
                    args = segment_ffmpeg_args(
                        frame_path=frames[index], segment_path=segment_path,
                        duration=duration, fps=fps, audio_path=audio_paths.get(scene_id),
                    )
                else:
                    args = video_only_segment_ffmpeg_args(
                        frame_path=frames[index], segment_path=segment_path,
                        duration=duration, fps=fps,
                    )
                await _run_ffmpeg(args)
                if not segment_path.is_file() or segment_path.stat().st_size <= 0:
                    raise ValueError("ffmpeg 未生成第 %d 个视频片段" % (index + 1))
                segments.append(segment_path)
            if is_cancelled is not None and is_cancelled():
                raise asyncio.CancelledError()

            concat_path = work_dir / "segments.txt"
            _write_concat_file(concat_path, segments)
            output_path = work_dir / "course.mp4"
            await stage("合并视频片段", len(scenes), len(scenes))
            await _run_ffmpeg(concat_ffmpeg_args(concat_path=concat_path, output_path=output_path))
            if not output_path.is_file() or output_path.stat().st_size <= 0:
                raise ValueError("ffmpeg 未生成视频文件")

        # ---- 4) 体积/取消校验 → 落库为素材 ----
        size_bytes = output_path.stat().st_size
        if size_bytes > MAX_VIDEO_BYTES:
            output_path.unlink(missing_ok=True)
            raise ValueError(
                "视频体积 %.1f MB 超过 %d MB 上限：请减少场景或缩短旁白后重试"
                % (size_bytes / (1024 * 1024), MAX_VIDEO_BYTES // (1024 * 1024))
            )
        if is_cancelled is not None and is_cancelled():
            raise asyncio.CancelledError()
        output_audio = has_audio or mode_used == VIDEO_MODE_ANIMATED
        duration_seconds = total_seconds
        await stage("保存视频素材", len(scenes), len(scenes))
        data = output_path.read_bytes()
        probed_total = await _probe_duration(output_path)
        if probed_total:
            duration_seconds = round(float(probed_total), 3)
        transition_used = (
            transition
            if mode_used == VIDEO_MODE_ANIMATED and transition_effective_ms > 0
            else TRANSITION_NONE
        )
        asset = _save_video_asset(
            user_id=int(user_id), course=course, data=data,
            duration_seconds=duration_seconds, fps=fps, scene_count=len(scenes),
            has_audio=output_audio, narrated_scene_count=narrated_scene_count,
            truncated=truncated, mode=mode_used, transition=transition_used,
            transition_ms=transition_effective_ms if mode_used == VIDEO_MODE_ANIMATED else 0,
            frame_count=frame_count, fallback_reason=fallback_reason,
        )
        cp["final_asset_id"] = int(asset["id"])
        cp["completed"] = True
        cp["mode_used"] = mode_used
        cp["fallback_reason"] = fallback_reason
        if on_checkpoint is not None:
            on_checkpoint(cp)
        public_asset = _asset_public(asset)
        summary = "视频导出完成：%d 个场景、约 %d 秒、%.1f MB" % (
            len(scenes), int(round(duration_seconds)), size_bytes / (1024 * 1024),
        )
        if mode_used == VIDEO_MODE_ANIMATED:
            summary += "；逐帧动画 %d 帧" % frame_count
            if transition_used == TRANSITION_FADE and transition_effective_ms > 0:
                summary += "、fade 转场 %dms" % transition_effective_ms
        if fallback_reason:
            summary += "；逐帧动画不可用，已降级为静态画面"
        if reused_frames:
            summary += "（复用 %d 张已完成帧）" % reused_frames
        if truncated:
            summary += "；课程共 %d 个场景，本次仅导出前 %d 个" % (len(all_scenes), len(scenes))
        if output_audio:
            if narrated_scene_count:
                summary += "；%d 个场景带旁白音轨" % narrated_scene_count
            else:
                summary += "；音轨为静音（本课程未生成旁白）"
        if warnings:
            summary += "；%d 条提示" % len(warnings)
        shutil.rmtree(work_dir, ignore_errors=True)
        return {
            "asset": public_asset,
            "asset_id": int(asset["id"]),
            "url": public_asset["url"],
            "mime_type": VIDEO_MIME,
            "duration_seconds": duration_seconds,
            "scene_count": len(scenes),
            "fps": fps,
            "size_bytes": size_bytes,
            "has_audio": output_audio,
            "narrated_scene_count": narrated_scene_count,
            "reused_frames": reused_frames,
            "captured_frames": captured_frames,
            "truncated": truncated,
            "warnings": warnings,
            "mode": requested_mode,
            "mode_used": mode_used,
            "animated": mode_used == VIDEO_MODE_ANIMATED,
            "transition": transition_used,
            "transition_requested": transition,
            "transition_ms": (
                transition_effective_ms if mode_used == VIDEO_MODE_ANIMATED else 0
            ),
            "transition_ms_requested": transition_ms,
            "frame_count": frame_count,
            "fallback_reason": fallback_reason,
            "summary": summary,
        }
    except asyncio.CancelledError:
        _cleanup_partial_files(work_dir)
        _cleanup_animated_partial(work_dir, cp)
        if on_checkpoint is not None:
            try:
                on_checkpoint(cp)
            except Exception:
                pass
        raise
    except Exception:
        _cleanup_partial_files(work_dir)
        _cleanup_animated_partial(work_dir, cp)
        raise
    finally:
        if job_id is None:
            shutil.rmtree(work_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# 任务执行器 / 准入校验
# ---------------------------------------------------------------------------

def _job_connection(user_id: int) -> dict:
    """视频导出不调用模型；任务系统要求 JobInput 带 provider/model，这里取用户任一连接作为载体。"""
    db = _db()
    with db.connection() as con:
        rows = [
            dict(row) for row in con.execute(
                "SELECT * FROM ai_providers WHERE user_id=? ORDER BY id", (int(user_id),),
            ).fetchall()
        ]
    for provider in rows:
        models = [
            str(item) for item in (cdb.json_load(provider.get("models_json"), []) or [])
            if str(item).strip()
        ]
        if models:
            return {"provider_id": int(provider["id"]), "model": models[0]}
    raise HTTPException(
        422,
        "视频导出使用后台任务执行：请先在「模型设置」保存任意一个 AI 连接"
        "（导出过程不会调用模型，仅作为任务载体）",
    )


def _running_video_job_count(course_id: int, user_id: int) -> int:
    from ..ai.jobs import TERMINAL_STATES

    placeholders = ",".join("?" for _ in TERMINAL_STATES)
    with _db().connection() as con:
        rows = con.execute(
            "SELECT request_json FROM ai_jobs WHERE user_id=? AND task=? AND status NOT IN (%s)"
            % placeholders,
            (int(user_id), TASK_NAME, *TERMINAL_STATES),
        ).fetchall()
    count = 0
    for row in rows:
        try:
            request = json.loads(row["request_json"] or "{}")
        except (TypeError, ValueError):
            continue
        params = request.get("params") or {}
        if isinstance(params, dict) and _int(params.get("course_id")) == int(course_id):
            count += 1
    return count


def _validate_video_params(params: dict, user: dict) -> dict:
    """POST 路由与任务准入共用的参数/前置校验；返回解析后的参数与课程。"""
    course_id = _int(params.get("course_id"))
    if not course_id:
        raise HTTPException(422, "缺少课程参数 course_id")
    course = cdb.find_course(course_id, user["id"])
    if course is None:
        raise HTTPException(404, "未找到该课程")
    scenes = cdb.course_scenes(course_id, user["id"])
    if not scenes:
        raise HTTPException(422, "课程还没有场景，无法导出视频")
    if len(scenes) > MAX_SCENES:
        raise HTTPException(
            422,
            "课程有 %d 个场景，超过视频导出上限 %d 个：请先删减场景或分段导出"
            % (len(scenes), MAX_SCENES),
        )
    fps = _int(params.get("fps"), DEFAULT_FPS)
    if not MIN_FPS <= fps <= MAX_FPS:
        raise HTTPException(422, "视频帧率必须在 %d–%d 之间" % (MIN_FPS, MAX_FPS))
    max_scenes = _int(params.get("max_scenes"), MAX_SCENES)
    if not 1 <= max_scenes <= MAX_SCENES:
        raise HTTPException(422, "单次视频导出最多 %d 个场景" % MAX_SCENES)
    # mode / transition / transition_ms：手工解析以保证 422 为中文说明（Pydantic 默认信息为英文）。
    try:
        mode = _resolve_video_mode(params.get("mode"))
        transition = _resolve_transition(params.get("transition"))
        transition_ms = _resolve_transition_ms(params.get("transition_ms"))
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    capabilities = video_capabilities()
    if not capabilities["available"]:
        raise HTTPException(503, "视频导出不可用：%s" % capabilities["detail"])
    narration = _narration_map(course)
    plan_scenes = scenes[:max_scenes]
    if mode == VIDEO_MODE_ANIMATED:
        # animated：按播放器时间轴估算每场景时长 → 先校验单场景/总帧数限额（任务内还会二次校验）。
        planned_seconds: list[float] = []
        for scene in plan_scenes:
            entry = narration.get(int(scene["id"]))
            if entry is not None:
                planned_seconds.append(_estimate_scene_seconds(scene, entry))
            else:
                planned_seconds.append(estimate_scene_duration_ms(scene) / 1000.0)
        try:
            animated_frame_plan(planned_seconds, fps)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        estimate = round(sum(planned_seconds), 3)
    else:
        estimate = _estimate_seconds(plan_scenes, narration)
    if estimate > MAX_VIDEO_SECONDS:
        raise HTTPException(
            422,
            "预计视频总时长约 %d 秒，超过 %d 分钟上限：请减少场景或缩短旁白后重试"
            % (int(estimate), int(MAX_VIDEO_SECONDS // 60)),
        )
    if _running_video_job_count(course_id, user["id"]) > 0:
        raise HTTPException(409, "该课程已有视频导出任务在运行，请等待完成或取消后再试")
    return {
        "course_id": course_id,
        "course": course,
        "scenes": scenes,
        "fps": fps,
        "max_scenes": max_scenes,
        "mode": mode,
        "transition": transition,
        "transition_ms": transition_ms,
    }


def validate_video_job(data, user) -> None:
    """``course-video`` 任务准入校验（POST /api/ai/jobs 与 POST /courses/{id}/video 共用）。"""
    _validate_video_params(_params(data), user)


async def run_video_job(job_id, user_id, data, provider, verifier) -> dict:
    """``course-video`` 执行器：截帧（static 静帧 / animated 逐帧）→ 片段合成 → 转场/拼接 → 素材落库。"""
    params = _params(data)
    course_id = _int(params.get("course_id"))
    if not course_id:
        raise ValueError("缺少课程参数 course_id")
    fps = _int(params.get("fps"), DEFAULT_FPS)
    max_scenes = _int(params.get("max_scenes"), MAX_SCENES)
    mode = _resolve_video_mode(params.get("mode"))
    transition = _resolve_transition(params.get("transition"))
    transition_ms = _resolve_transition_ms(params.get("transition_ms"))
    checkpoint = _checkpoint_get(job_id)
    _job_status_update(job_id, "running", "准备导出视频")
    _publish(job_id, "stage", {"status": "running", "stage": "准备导出视频", "done": 0, "total": 0})

    async def on_stage(stage: str, done: int, total: int, **extra) -> None:
        if not _cancelled(job_id):
            _job_status_update(job_id, "running", stage)
        payload = {"status": "running", "stage": stage, "done": int(done), "total": int(total)}
        payload.update(extra)
        _publish(job_id, "stage", payload)

    def on_checkpoint(value: dict) -> None:
        _checkpoint_set(job_id, value)

    def is_cancelled() -> bool:
        return _cancelled(job_id)

    try:
        result = await render_course_video(
            int(course_id), int(user_id), fps=fps, max_scenes=max_scenes,
            mode=mode, transition=transition, transition_ms=transition_ms,
            job_id=job_id, checkpoint=checkpoint,
            on_stage=on_stage, on_checkpoint=on_checkpoint, is_cancelled=is_cancelled,
        )
    except asyncio.CancelledError:
        _checkpoint_force(job_id, checkpoint if isinstance(checkpoint, dict) else {})
        raise
    if _cancelled(job_id):
        _checkpoint_force(job_id, checkpoint if isinstance(checkpoint, dict) else {})
        raise asyncio.CancelledError()
    _publish(job_id, "stage", {
        "status": "running", "stage": "视频导出完成",
        "done": result.get("scene_count"), "total": result.get("scene_count"),
    })
    return result


def register() -> None:
    """向任务注册表登记 ``course-video``（由 ``__init__.register_jobs()`` 调用）。"""
    from ..ai.jobs import register_task_handler

    register_task_handler(
        TASK_NAME, run_video_job, validate_video_job, label=TASK_LABEL,
    )


def init_video_storage() -> None:
    """启动时准备工作目录（幂等；素材目录由 ``init_classroom`` 统一创建）。"""
    try:
        _ensure_dir(_work_root())
    except OSError:
        log.warning("Could not create classroom video work dir", exc_info=True)


# ---------------------------------------------------------------------------
# HTTP 路由：创建任务 / 查询最近产物 / 下载 mp4
# ---------------------------------------------------------------------------

class VideoInput(BaseModel):
    """``POST /courses/{id}/video`` 请求体。

    - ``mode``：``static``（默认，静帧）/ ``animated``（逐帧动画）；
    - ``transition``：``none`` / ``fade``（默认）；``transition_ms``：100–1000（默认 400）；
    - ``fps`` / ``max_scenes`` 沿用既有约束；mode/transition 的非法值由路由返回 422 中文说明。
    """

    fps: int | None = Field(default=None, ge=MIN_FPS, le=MAX_FPS)
    max_scenes: int | None = Field(default=None, ge=1, le=MAX_SCENES)
    mode: str | None = None
    transition: str | None = None
    transition_ms: int | None = None


@router.post("/courses/{course_id}/video")
async def start_video(course_id: int, data: VideoInput | None = None,
                      user=Depends(current_user)):
    """创建课堂视频导出任务 → ``{job, course_id, scene_count, fps, mode, transition, ...}``。"""
    payload = data or VideoInput()
    params: dict = {"course_id": int(course_id)}
    if payload.fps is not None:
        params["fps"] = int(payload.fps)
    if payload.max_scenes is not None:
        params["max_scenes"] = int(payload.max_scenes)
    if payload.mode is not None:
        params["mode"] = payload.mode
    if payload.transition is not None:
        params["transition"] = payload.transition
    if payload.transition_ms is not None:
        params["transition_ms"] = int(payload.transition_ms)
    resolved = _validate_video_params(params, user)
    # 未显式传参时也把解析后的默认值写进任务参数，便于任务卡/续跑稳定复现。
    params.setdefault("mode", resolved["mode"])
    params.setdefault("transition", resolved["transition"])
    params.setdefault("transition_ms", resolved["transition_ms"])
    connection = _job_connection(user["id"])
    from ..ai.jobs import JobInput, submit_job

    job_input = JobInput(
        task=TASK_NAME,
        provider_id=int(connection["provider_id"]),
        model=str(connection["model"]),
        params=params,
    )
    job = await submit_job(user, job_input)
    scenes = resolved["scenes"]
    return {
        "job": job,
        "course_id": int(course_id),
        "scene_count": len(scenes),
        "render_scene_count": min(len(scenes), resolved["max_scenes"]),
        "fps": resolved["fps"],
        "mode": resolved["mode"],
        "transition": resolved["transition"],
        "transition_ms": resolved["transition_ms"],
        "capabilities": video_capabilities(),
        "limits": video_limits(),
    }


def _latest_video_job(course_id: int, user_id: int) -> dict | None:
    """该课程最近一次 ``course-video`` 任务摘要（供导出对话框显示进度/失败原因）。"""
    with _db().connection() as con:
        rows = [
            dict(row) for row in con.execute(
                "SELECT * FROM ai_jobs WHERE user_id=? AND task=? ORDER BY id DESC LIMIT 20",
                (int(user_id), TASK_NAME),
            ).fetchall()
        ]
    for row in rows:
        try:
            request = json.loads(row.get("request_json") or "{}")
        except (TypeError, ValueError):
            continue
        params = request.get("params") or {}
        if not isinstance(params, dict) or _int(params.get("course_id")) != int(course_id):
            continue
        result = None
        if row.get("result_json"):
            try:
                result = json.loads(row["result_json"])
            except (TypeError, ValueError):
                result = None
        excerpt = None
        if isinstance(result, dict):
            excerpt = {
                key: result.get(key)
                for key in ("asset_id", "url", "scene_count", "fps", "duration_seconds",
                            "size_bytes", "mode", "mode_used", "animated", "transition",
                            "transition_ms", "frame_count", "fallback_reason", "summary")
                if key in result
            }
        return {
            "id": int(row["id"]),
            "status": row.get("status") or "",
            "stage": row.get("stage") or "",
            "error": row.get("error") or "",
            "created_at": row.get("created_at") or "",
            "updated_at": row.get("updated_at") or "",
            "has_checkpoint": bool(row.get("checkpoint_json")),
            "result": excerpt,
        }
    return None


@router.get("/courses/{course_id}/video")
def video_status(course_id: int, user=Depends(current_user)):
    """课程视频状态：导出能力 + 限额 + 最近一次成功产物 + 最近一次任务。"""
    course = cdb.find_course(int(course_id), user["id"])
    if course is None:
        raise HTTPException(404, "未找到该课程")
    row = _latest_video_asset(int(course_id), user["id"])
    return {
        "course_id": int(course_id),
        "capabilities": export_capabilities(),
        "limits": video_limits(),
        "asset": _asset_public(row) if row else None,
        "latest_job": _latest_video_job(int(course_id), user["id"]),
        "scene_count": len(cdb.course_scenes(int(course_id), user["id"])),
    }


def mp4_export_response(course: dict, user_id: int) -> Response:
    """``GET /courses/{id}/export?format=mp4``：下载最近一次成功生成的视频素材。"""
    course_id = int(course["id"])
    row = _latest_video_asset(course_id, int(user_id))
    if row is None:
        raise HTTPException(
            409,
            "该课程还没有生成 MP4 视频：请先在「导出课堂」中选择「生成 MP4 视频」，"
            "等任务完成后再下载",
        )
    try:
        data = storage.read_file(VIDEO_FOLDER, row["storage_name"])
    except HTTPException:
        raise HTTPException(503, "视频文件已丢失，请重新生成视频") from None
    if not data:
        raise HTTPException(503, "视频文件已丢失，请重新生成视频")
    meta = cdb.json_load(row.get("meta_json"), {})
    meta = meta if isinstance(meta, dict) else {}
    filename = cexports.export_filename(course, "mp4")
    headers = cexports._file_headers(filename, extra={
        "X-Zhixu-Asset-Id": str(int(row["id"])),
        "X-Zhixu-Video-Duration": "%.1f" % float(meta.get("duration_seconds") or 0),
        "X-Zhixu-Video-Scenes": str(meta.get("scene_count") or 0),
        "X-Zhixu-Video-Fps": str(meta.get("fps") or DEFAULT_FPS),
    })
    return Response(content=data, media_type=VIDEO_MIME, headers=headers)


__all__ = [
    "ANIMATED_FRAME_QUALITY", "ANIMATED_FRAME_SUFFIX", "ANIMATED_FRAMES_DIR",
    "AnimatedUnavailable", "DEFAULT_FPS", "DEFAULT_SCENE_SECONDS", "DEFAULT_TRANSITION",
    "DEFAULT_TRANSITION_MS", "DEFAULT_VIDEO_MODE", "MAX_ANIMATED_FRAMES_PER_SCENE",
    "MAX_ANIMATED_FRAMES_TOTAL", "MAX_FPS", "MAX_SCENES", "MAX_TRANSITION_MS",
    "MAX_VIDEO_BYTES", "MAX_VIDEO_SECONDS", "MIN_FPS", "MIN_TRANSITION_MS", "MIN_SCENE_MS",
    "TASK_LABEL", "TASK_NAME", "TRANSITION_FADE", "TRANSITION_NONE",
    "TRANSITIONS", "VIDEO_HEIGHT", "VIDEO_KIND", "VIDEO_MIME", "VIDEO_MODES",
    "VIDEO_MODE_ANIMATED", "VIDEO_MODE_STATIC", "VIDEO_WIDTH",
    "animated_frame_count", "animated_frame_plan", "animated_frame_times",
    "animated_segment_ffmpeg_args", "concat_ffmpeg_args", "effective_transition_seconds",
    "estimate_scene_duration_ms", "export_capabilities", "ffmpeg_info", "init_video_storage",
    "mp4_export_response", "register", "render_course_video", "router", "run_video_job",
    "scene_digest", "segment_ffmpeg_args", "start_video", "validate_video_job",
    "video_capabilities", "video_limits", "video_only_segment_ffmpeg_args", "video_status",
    "xfade_ffmpeg_args", "xfade_offsets",
]
