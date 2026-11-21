"""课堂批量旁白合成（P4）：``course-narration`` 任务 + 旁白查询/发起路由。

设计要点（与 ``generate.py`` 的任务执行器规范一致）：

- 逐场景读取 ``meta.actions`` 中的 ``speech`` 文本，拼成一段旁白，调用
  :func:`app.classroom.tts.synthesize` 合成音频；音频按 sha256 内容寻址落 ``cls_assets``；
- 「场景 → 音频素材」写入课程 ``config_json.narration``（读-改-写加 ``db.write_lock`` 并重读，
  不会覆盖并发的 ``web_search`` 等其它配置）；
- SSE 阶段「合成旁白 第 n/m 场景」；每个场景结束后写 checkpoint，取消时保留已合成场景并
  强制落库检查点，续跑跳过已完成场景；
- 幂等：同一场景同一 speech 文本（且音色一致、素材仍在）已有旁白时直接复用，重复执行只保留
  一份配置、不会重复生成素材；
- 失败隔离：单个场景配音失败只记失败清单并继续，全部失败才让任务失败（绝不伪造旁白）。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ..auth import current_user
from . import db as cdb
from . import tts

router = APIRouter()
log = logging.getLogger("app.classroom.narration")

TASK_NAME = "course-narration"
TASK_LABEL = "课堂批量旁白"
#: 单场景旁白文本上限（与单次 TTS 上限一致；超出部分截断并记录，绝不静默丢弃）。
MAX_SCENE_TEXT_CHARS = tts.MAX_TTS_CHARS
#: 单次批量合成最多覆盖的场景数（防御异常参数）。
MAX_SCENES = 200


# ---------------------------------------------------------------------------
# 延迟引用 app.ai.jobs（测试可 monkeypatch，也避免包初始化循环）
# ---------------------------------------------------------------------------

def _ai_jobs():
    from ..ai import jobs

    return jobs


def _publish(job_id: int, event: str, payload: dict) -> None:
    try:
        _ai_jobs().publish_event(job_id, event, payload)
    except Exception:  # SSE/Redis 推送失败不影响已落库产物
        pass


def _job_status_update(job_id: int, status: str, stage: str) -> None:
    """更新任务状态/阶段（公共包装 ``update_job``，失败不影响旁白合成）。"""
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
    """取消后强制落库检查点：``_job_set_checkpoint`` 带 cancelled 过滤会跳过写入。"""
    from .. import db

    try:
        with db.connection() as con:
            db.write_lock(con, "ai-job-checkpoint")
            con.execute(
                "UPDATE ai_jobs SET checkpoint_json=?, updated_at=? WHERE id=?",
                (cdb.json_dump(checkpoint), db.utcnow(), job_id),
            )
    except Exception:
        log.warning("Could not force checkpoint for narration job %s", job_id, exc_info=True)


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
# 课程配置 / 场景旁白文本
# ---------------------------------------------------------------------------

def _course_config(course: dict) -> dict:
    config = cdb.json_load((course or {}).get("config_json"), {})
    return config if isinstance(config, dict) else {}


def _update_course_config(user_id: int, course_id: int, updates: dict) -> dict:
    """读-改-写课程配置（写锁 + 重读，避免覆盖并发的 web_search 等其它键）。"""
    from .. import db

    with db.connection() as con:
        db.write_lock(con, "classroom-course")
        row = con.execute(
            "SELECT config_json FROM cls_courses WHERE id=? AND user_id=?", (course_id, user_id),
        ).fetchone()
        if row is None:
            return {}
        config = cdb.json_load(row["config_json"], {})
        config = config if isinstance(config, dict) else {}
        config.update(updates or {})
        con.execute(
            "UPDATE cls_courses SET config_json=?, updated_at=? WHERE id=? AND user_id=?",
            (cdb.json_dump(config), db.utcnow(), course_id, user_id),
        )
    return config


def _narration_map(config: dict) -> dict:
    """读取 ``config.narration``：``{scene_id(str): {asset_id,url,voice,chars,text_sha256,...}}``。"""
    raw = (config or {}).get("narration")
    if not isinstance(raw, dict):
        return {}
    result: dict[str, dict] = {}
    for key, value in raw.items():
        scene_id = _int(key)
        if scene_id and isinstance(value, dict):
            result[str(scene_id)] = dict(value)
    return result


def _scene_actions(scene_row) -> list[dict]:
    meta = cdb.json_load((scene_row or {}).get("meta_json"), {})
    actions = meta.get("actions") if isinstance(meta, dict) else []
    return [dict(item) for item in actions if isinstance(item, dict)] if isinstance(actions, list) else []


def _join_speech_texts(texts) -> str:
    """把同一场景的多段 speech 拼成一段旁白（缺失句末标点补「。」，保持朗读自然）。"""
    pieces: list[str] = []
    for raw in texts:
        piece = str(raw or "").strip()
        if not piece:
            continue
        if not piece.endswith(("。", "！", "？", "…", "；", ".", "!", "?", ";", "，", ",")):
            piece += "。"
        pieces.append(piece)
    return "".join(pieces).strip()


def scene_speech_text(scene_row) -> str:
    """场景的旁白文本 = 按顺序拼接所有 ``speech`` 动作文本（无 speech 返回空串）。"""
    return _join_speech_texts(
        action.get("text") for action in _scene_actions(scene_row)
        if str(action.get("type") or "") == "speech"
    )


def _speech_text_hash(text: str) -> str:
    return hashlib.sha256(str(text or "").encode("utf-8")).hexdigest()


def _speech_scenes(course: dict) -> list[dict]:
    """返回可合成旁白的场景清单（按 order_index 排序；超长文本截断并标记）。"""
    targets: list[dict] = []
    for scene in cdb.course_scenes(course["id"], course.get("user_id")):
        text = scene_speech_text(scene)
        if not text:
            continue
        truncated = len(text) > MAX_SCENE_TEXT_CHARS
        if truncated:
            text = text[:MAX_SCENE_TEXT_CHARS]
        targets.append({"scene": scene, "text": text, "chars": len(text), "truncated": truncated})
    return targets


# ---------------------------------------------------------------------------
# 连接解析 / 幂等判断
# ---------------------------------------------------------------------------

def _resolve_tts_candidate(user_id: int, provider_id: int | None = None,
                           model: str | None = None) -> dict:
    """解析本次旁白使用的 TTS 连接；没有已配置的语音连接 → 422 中文引导。"""
    candidates = tts.tts_candidates(user_id)
    if not candidates:
        raise HTTPException(
            422,
            "还没有可用的语音合成连接：请在「模型设置」添加支持 TTS 的连接"
            "（小米 MiMo / MiniMax / OpenAI 兼容语音）后再合成旁白",
        )
    if provider_id:
        matches = [item for item in candidates if int(item["provider"]["id"]) == int(provider_id)]
        if not matches:
            raise HTTPException(404, "未找到该语音合成连接")
        candidate = matches[0]
        if model:
            models = [str(item) for item in cdb.json_load(candidate["provider"].get("models_json"), []) or []]
            if model not in models:
                raise HTTPException(422, "该连接没有配置这个语音模型")
            return {"provider": candidate["provider"], "model": str(model)}
        return candidate
    if model:
        for item in candidates:
            models = [str(entry) for entry in cdb.json_load(item["provider"].get("models_json"), []) or []]
            if model in models:
                return {"provider": item["provider"], "model": str(model)}
        raise HTTPException(422, "该语音模型未在已配置的连接中，请先在模型设置保存该模型")
    return candidates[0]


def _asset_alive(asset_id: int, user_id: int) -> bool:
    if not asset_id:
        return False
    from .. import db

    with db.connection() as con:
        row = con.execute(
            "SELECT 1 FROM cls_assets WHERE id=? AND user_id=? AND kind=?",
            (asset_id, user_id, tts.TTS_KIND),
        ).fetchone()
    return row is not None


def _entry_matches(entry, text_hash: str, voice: str) -> bool:
    """复用条件：同场景同 speech 文本 + 音色一致（未记录音色时视为一致，兼容旧配置）。"""
    if not isinstance(entry, dict):
        return False
    if str(entry.get("text_sha256") or "") != str(text_hash):
        return False
    stored_voice = str(entry.get("voice") or "").strip()
    if stored_voice and voice and stored_voice != voice:
        return False
    return bool(_int(entry.get("asset_id")) or str(entry.get("url") or ""))


def _running_narration_job_count(course_id: int, user_id: int) -> int:
    """统计该课程正在排队/运行中的旁白任务数（用于 409 与重复提交去重）。"""
    from ..ai.jobs import TERMINAL_STATES
    from .. import db

    placeholders = ",".join("?" for _ in TERMINAL_STATES)
    with db.connection() as con:
        rows = con.execute(
            "SELECT request_json FROM ai_jobs WHERE user_id=? AND task=? AND status NOT IN (%s)"
            % placeholders,
            (user_id, TASK_NAME, *TERMINAL_STATES),
        ).fetchall()
    count = 0
    for row in rows:
        try:
            request = json.loads(row["request_json"] or "{}")
        except (TypeError, ValueError):
            continue
        params = request.get("params") or {}
        if isinstance(params, dict) and _int(params.get("course_id")) == course_id:
            count += 1
    return count


# ---------------------------------------------------------------------------
# 任务执行器
# ---------------------------------------------------------------------------

async def run_narration_job(job_id, user_id, data, provider, verifier) -> dict:
    """``course-narration`` 执行器：逐场景 speech → TTS → 素材 + 课程配置。"""
    params = _params(data)
    course_id = _int(params.get("course_id"))
    if not course_id:
        raise ValueError("缺少课程参数 course_id")
    course = cdb.find_course(course_id, user_id)
    if course is None:
        raise ValueError("课程不存在或无权访问")
    config = _course_config(course)
    narration = _narration_map(config)
    voice_setting = str(params.get("voice") or config.get("voice") or "").strip()
    effective_voice = tts.normalize_voice_for(provider, voice_setting or None)

    targets = _speech_scenes(course)
    requested = params.get("scene_ids")
    if isinstance(requested, (list, tuple)) and requested:
        wanted = {_int(item) for item in requested}
        targets = [target for target in targets if int(target["scene"]["id"]) in wanted]
    total = len(targets)
    if not total:
        raise ValueError("该课程还没有可合成的旁白文本（场景 actions.speech 为空）")

    checkpoint = _checkpoint_get(job_id)
    done = dict(checkpoint.get("done") or {})
    done_ids = {_int(item) for item in (checkpoint.get("done_scene_ids") or [])}
    failed_ids = {_int(item) for item in (checkpoint.get("failed_scene_ids") or [])}
    reused = 0
    synthesized = 0
    failure_messages: list[dict] = []
    truncated_scene_ids: list[int] = []
    total_chars = 0
    provider_name = str(provider.get("name") or "") if provider else ""
    model_name = str(getattr(data, "model", "") or "")

    _job_status_update(job_id, "running", "准备合成本课旁白")
    _publish(job_id, "stage", {
        "status": "running", "stage": "准备合成本课旁白", "done": 0, "total": total,
        "voice": effective_voice,
    })

    def save_checkpoint() -> None:
        checkpoint.update({
            "course_id": course_id,
            "done": done,
            "done_scene_ids": sorted(done_ids),
            "failed_scene_ids": sorted(failed_ids),
            "voice": effective_voice,
            "provider_id": _int(getattr(data, "provider_id", 0)) or None,
            "model": model_name,
        })
        _checkpoint_set(job_id, checkpoint)

    for position, target in enumerate(targets, 1):
        if _cancelled(job_id):
            save_checkpoint()
            _checkpoint_force(job_id, checkpoint)
            raise asyncio.CancelledError()
        scene = target["scene"]
        scene_id = int(scene["id"])
        scene_key = str(scene_id)
        scene_title = str(scene.get("title") or "")
        text = target["text"]
        text_hash = _speech_text_hash(text)
        if target["truncated"]:
            truncated_scene_ids.append(scene_id)

        # 幂等 / 续跑：配置里已有同文本同音色且素材仍在 → 复用，不再调用语音服务。
        existing = narration.get(scene_key) or done.get(scene_key)
        if (_entry_matches(existing, text_hash, effective_voice)
                and _asset_alive(_int(existing.get("asset_id")), user_id)):
            entry = dict(existing)
            narration[scene_key] = entry
            done[scene_key] = entry
            done_ids.add(scene_id)
            failed_ids.discard(scene_id)
            reused += 1
            total_chars += _int(entry.get("chars"))
            save_checkpoint()
            _publish(job_id, "stage", {
                "status": "running",
                "stage": "复用第 %d/%d 个场景旁白：%s" % (position, total, scene_title),
                "done": position, "total": total, "scene_id": scene_id,
                "scene_title": scene_title, "reused": True,
            })
            continue

        stage_text = "合成旁白 第 %d/%d 场景：%s" % (position, total, scene_title)
        _job_status_update(job_id, "running", stage_text)
        _publish(job_id, "stage", {
            "status": "running", "stage": stage_text, "done": position - 1, "total": total,
            "scene_id": scene_id, "scene_title": scene_title,
        })
        result: dict
        try:
            result = await tts.synthesize(
                user_id=user_id, course_id=course_id, text=text,
                voice=effective_voice or None,
                provider_id=_int(getattr(data, "provider_id", 0)) or None,
                model=model_name or None,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # 单场景失败隔离：记录后继续下一个场景
            result = {"fallback": "browser", "error": "%s: %s" % (type(exc).__name__, str(exc)[:160])}
        asset = result.get("asset") if isinstance(result, dict) else None
        if not asset:
            message = str((result or {}).get("error") or "语音合成服务不可用")[:200]
            failed_ids.add(scene_id)
            failure_messages.append({"scene_id": scene_id, "title": scene_title, "error": message})
            save_checkpoint()
            _publish(job_id, "stage", {
                "status": "running",
                "stage": "第 %d/%d 个场景旁白合成失败：%s" % (position, total, scene_title),
                "done": position - 1, "total": total, "failed": True,
                "scene_id": scene_id, "scene_title": scene_title, "error": message,
            })
            continue

        entry = {
            "asset_id": _int(asset.get("id")),
            "url": str(asset.get("url") or ""),
            "voice": str(result.get("voice") or effective_voice or ""),
            "chars": len(text),
            "text_sha256": text_hash,
            "scene_title": scene_title,
            "provider": str(result.get("provider") or provider_name),
            "model": str(result.get("model") or model_name),
        }
        from .. import db as _db

        entry["updated_at"] = _db.utcnow()
        narration[scene_key] = entry
        done[scene_key] = entry
        done_ids.add(scene_id)
        failed_ids.discard(scene_id)
        synthesized += 1
        total_chars += len(text)
        _update_course_config(user_id, course_id, {
            "narration": narration,
            "narration_updated_at": entry["updated_at"],
            "narration_provider": entry["provider"],
            "narration_model": entry["model"],
            "voice": entry["voice"] or config.get("voice") or "",
        })
        save_checkpoint()
        _publish(job_id, "stage", {
            "status": "running",
            "stage": "已完成第 %d/%d 个场景旁白：%s" % (position, total, scene_title),
            "done": position, "total": total, "scene_id": scene_id,
            "scene_title": scene_title, "asset_id": entry["asset_id"],
        })

    if _cancelled(job_id):
        save_checkpoint()
        _checkpoint_force(job_id, checkpoint)
        raise asyncio.CancelledError()

    narrated_count = len(done_ids)
    if narrated_count == 0:
        first_error = failure_messages[0]["error"] if failure_messages else "语音服务不可用"
        raise ValueError("旁白合成失败：%s" % first_error)

    summary = "旁白合成完成：%d/%d 个场景有旁白（新合成 %d、复用 %d）" % (
        narrated_count, total, synthesized, reused,
    )
    if failed_ids:
        summary += "；%d 个场景失败，可重新发起补合成" % len(failed_ids)
    if truncated_scene_ids:
        summary += "；%d 个场景文本超过 %d 字已截断" % (len(truncated_scene_ids), MAX_SCENE_TEXT_CHARS)
    _publish(job_id, "stage", {
        "status": "running", "stage": "旁白合成完成", "done": total, "total": total,
    })
    return {
        "course_id": course_id,
        "course_title": course.get("title") or "",
        "scene_count": total,
        "narrated_scene_count": narrated_count,
        "synthesized_scene_count": synthesized,
        "reused_scene_count": reused,
        "failed_scene_ids": sorted(failed_ids),
        "failures": failure_messages,
        "truncated_scene_ids": truncated_scene_ids,
        "total_chars": total_chars,
        "voice": effective_voice,
        "provider": provider_name,
        "model": model_name,
        "summary": summary,
    }


# ---------------------------------------------------------------------------
# 准入校验（POST /api/ai/jobs 与 POST /courses/{id}/narration 共用）
# ---------------------------------------------------------------------------

def validate_narration_job(data, user) -> None:
    params = _params(data)
    course_id = _int(params.get("course_id"))
    if not course_id:
        raise HTTPException(422, "缺少课程参数 course_id")
    course = cdb.find_course(course_id, user["id"])
    if course is None:
        raise HTTPException(404, "未找到该课程")
    targets = _speech_scenes(course)
    if not targets:
        raise HTTPException(422, "该课程还没有可合成的旁白文本（场景 actions.speech 为空）")
    voice = str(params.get("voice") or "").strip()
    if len(voice) > 120:
        raise HTTPException(422, "音色标识过长（最多 120 字符）")
    _resolve_tts_candidate(user["id"], _int(getattr(data, "provider_id", 0)) or None,
                           str(getattr(data, "model", "") or "") or None)
    scene_ids = params.get("scene_ids")
    if scene_ids is not None:
        if not isinstance(scene_ids, (list, tuple)) or not scene_ids:
            raise HTTPException(422, "指定场景时 scene_ids 不能为空")
        if len(scene_ids) > MAX_SCENES:
            raise HTTPException(422, "一次最多合成 %d 个场景" % MAX_SCENES)
        all_scenes = {int(scene["id"]) for scene in cdb.course_scenes(course_id, user["id"])}
        speech_ids = {int(target["scene"]["id"]) for target in targets}
        for item in scene_ids:
            scene_id = _int(item)
            if scene_id not in all_scenes:
                raise HTTPException(404, "未找到要合成旁白的场景")
            if scene_id not in speech_ids:
                raise HTTPException(422, "所选场景没有可合成的旁白文本（actions.speech 为空）")
    if _running_narration_job_count(course_id, user["id"]) > 0:
        raise HTTPException(409, "该课程已有旁白合成任务在运行，请等待完成或取消后再试")


def register() -> None:
    """向任务注册表登记 ``course-narration``（由 ``__init__.register_jobs()`` 调用）。"""
    from ..ai.jobs import register_task_handler

    register_task_handler(
        TASK_NAME, run_narration_job, validate_narration_job, label=TASK_LABEL,
    )


# ---------------------------------------------------------------------------
# HTTP 路由：POST 发起批量旁白 / GET 查询已合成旁白
# ---------------------------------------------------------------------------

class NarrationInput(BaseModel):
    voice: str | None = Field(default=None, max_length=120)
    provider_id: int | None = None
    model: str | None = Field(default=None, max_length=160)
    scene_ids: list[int] | None = Field(default=None, max_length=MAX_SCENES)


def _job_input(**kwargs):
    from ..ai.jobs import JobInput

    return JobInput(**kwargs)


@router.post("/courses/{course_id}/narration")
async def start_narration(course_id: int, data: NarrationInput | None = None,
                          user=Depends(current_user)):
    """发起批量旁白任务 → ``{job, course_id, voice, provider, model, scene_total}``。"""
    course = cdb.find_course(course_id, user["id"])
    if course is None:
        raise HTTPException(404, "未找到该课程")
    payload = data or NarrationInput()
    config = _course_config(course)
    voice = str(payload.voice or config.get("voice") or "").strip() or None
    candidate = _resolve_tts_candidate(user["id"], payload.provider_id, payload.model)
    params: dict = {"course_id": course_id}
    if voice:
        params["voice"] = voice
    if payload.scene_ids is not None:
        if not payload.scene_ids:
            raise HTTPException(422, "指定场景时 scene_ids 不能为空")
        params["scene_ids"] = [int(item) for item in payload.scene_ids]
    job_input = _job_input(
        task=TASK_NAME,
        provider_id=int(candidate["provider"]["id"]),
        model=str(candidate["model"]),
        params=params,
    )
    from ..ai.jobs import submit_job

    job = await submit_job(user, job_input)
    if voice:
        # 记住用户选择的音色，下次旁白/单句 TTS 直接沿用（只更新 config.voice 一个键）。
        _update_course_config(user["id"], course_id, {"voice": voice})
    return {
        "job": job,
        "course_id": course_id,
        "voice": voice,
        "provider": candidate["provider"].get("name") or "",
        "model": candidate["model"],
        "scene_total": len(_speech_scenes(course)),
    }


@router.get("/courses/{course_id}/narration")
def get_narration(course_id: int, user=Depends(current_user)):
    """查询课程已合成的旁白 → ``{items:[{scene_id,asset...}], ...}``；无旁白时 ``items=[]``。"""
    from .. import db

    course = cdb.find_course(course_id, user["id"])
    if course is None:
        raise HTTPException(404, "未找到该课程")
    config = _course_config(course)
    narration = _narration_map(config)
    scenes = cdb.course_scenes(course_id, user["id"])
    with db.connection() as con:
        asset_rows = {
            int(row["id"]): dict(row)
            for row in con.execute(
                "SELECT * FROM cls_assets WHERE user_id=? AND kind=?", (user["id"], tts.TTS_KIND),
            ).fetchall()
        }
    items: list[dict] = []
    total_chars = 0
    for scene in scenes:
        entry = narration.get(str(scene["id"]))
        if not entry:
            continue
        asset_id = _int(entry.get("asset_id"))
        row = asset_rows.get(asset_id)
        asset = tts.asset_public(row) if row else None
        url = str(entry.get("url") or (asset or {}).get("url") or "")
        chars = _int(entry.get("chars"))
        total_chars += chars
        items.append({
            "scene_id": int(scene["id"]),
            "scene_title": str(scene.get("title") or entry.get("scene_title") or ""),
            "order_index": _int(scene.get("order_index")),
            "asset_id": asset_id,
            "url": url,
            "asset": asset,
            "voice": str(entry.get("voice") or ""),
            "chars": chars,
            "text_sha256": str(entry.get("text_sha256") or ""),
            "updated_at": str(entry.get("updated_at") or ""),
            "missing_asset": bool(asset_id and asset is None),
        })
    return {
        "course_id": course_id,
        "course_title": course.get("title") or "",
        "items": items,
        "total": len(items),
        "total_chars": total_chars,
        "voice": str(config.get("voice") or ""),
        "provider": str(config.get("narration_provider") or ""),
        "model": str(config.get("narration_model") or ""),
        "updated_at": str(config.get("narration_updated_at") or ""),
        "scene_count": len(scenes),
    }


__all__ = [
    "MAX_SCENE_TEXT_CHARS", "MAX_SCENES", "TASK_LABEL", "TASK_NAME",
    "get_narration", "register", "run_narration_job", "router", "scene_speech_text",
    "start_narration", "validate_narration_job",
]
