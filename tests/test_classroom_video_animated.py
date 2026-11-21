"""P5 增强验收：课堂视频逐帧动画（animated）+ 场景转场（ffmpeg xfade）。

离线单测：渲染截图与 ffmpeg 子进程全部用 monkeypatch 假实现，覆盖
- 逐帧时间轴/帧数计算（animated_frame_times / estimate_scene_duration_ms，与播放器 JS 同参数）；
- seekMs 调用序列（页面级 `_animated_scene_frames` 用假 page 验证 0,1/fps,… 升序采样与取消）；
- xfade / acrossfade 参数与 offset 链（xfade_offsets / xfade_ffmpeg_args / animated_segment_ffmpeg_args）；
- 任务成功路径（mode/帧数/转场/SSE 阶段/素材 meta）、transition=none 走 concat；
- 降级：seek 接口不可用 → static 回退 + `mode_used='static'` / `fallback_reason`；
- 限额：单场景 ≤300 帧、总帧数 ≤6000（route 422 中文 / helper 抛错）；
- 取消清理未完成场景帧 + checkpoint 按场景复用；checkpoint 判据纯函数。
真实冒烟：造 1 门含 speech+spotlight 的短课 → animated 生成 3–6 秒 h264+aac 视频，
ffprobe 断言后复制到 `.omx/tmp/video-anim-smoke.mp4`（缺引擎自动 skip）。
"""
import asyncio
import base64
import hashlib
import inspect
import json
import shutil
import subprocess
import time
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from app import db
from app.ai import jobs as ai_jobs
from app.classroom import db as cdb
from app.classroom import video

PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)
JPEG_BYTES = b"\xff\xd8\xff\xe0FAKE-JPEG-FRAME"

SLIDE_CONTENT = {
    "type": "slide",
    "canvas": {
        "width": 1000, "height": 562.5, "theme": {},
        "elements": [
            {"id": "t1", "type": "text", "left": 60, "top": 50, "width": 880, "height": 80,
             "text": "光合作用", "fontSize": 32, "color": "#333333"},
            {"id": "t2", "type": "text", "left": 60, "top": 220, "width": 880, "height": 120,
             "text": "叶绿体是光合作用的场所", "fontSize": 26, "color": "#333333"},
        ],
    },
}
SHORT_SLIDE_CONTENT = {
    "type": "slide",
    "canvas": {
        "width": 1000, "height": 562.5, "theme": {},
        "elements": [
            {"id": "t1", "type": "text", "left": 100, "top": 80, "width": 800, "height": 100,
             "text": "焦点", "fontSize": 36, "color": "#1f2937"},
        ],
    },
}
#: speech 11 字 → 800 + 11×250 = 3550ms；spotlight 700ms → 场景 4250ms（12fps → 51 帧）。
ANIM_ACTIONS = [
    {"id": "a1", "type": "speech", "text": "这一页我们看光合作用。"},
    {"id": "a2", "type": "spotlight", "element_id": "t1", "dim_opacity": 0.5},
]
SPEECH_ONLY_ACTIONS = [{"id": "b1", "type": "speech", "text": "这一页我们看光合作用。"}]


# ---------------------------------------------------------------------------
# 公共辅助（与 tests/test_classroom_video.py 同一套建库/任务工具）
# ---------------------------------------------------------------------------

def save_provider(client, name="视频动画测试连接", models=("test-model",)):
    response = client.post("/api/providers", json={
        "name": name, "kind": "openai", "base_url": "https://api.openai.com/v1",
        "api_key": "sk-classroom-video-anim-test-123456", "models": list(models),
    })
    assert response.status_code == 200, response.text
    return response.json()


def create_course(client, **overrides):
    payload = {"topic": "光合作用", "subject": "生物", "grade": "初中二年级"}
    payload.update(overrides)
    response = client.post("/api/classroom/courses", json=payload)
    assert response.status_code == 200, response.text
    return response.json()["course"]


def user_id_of(client):
    return client.get("/api/auth/me").json()["id"]


def insert_scene(course_id, user_id, scene_type, order_index, title, content, *, meta=None):
    now = db.utcnow()
    with db.connection() as con:
        return con.execute(
            "INSERT INTO cls_scenes(course_id,user_id,scene_type,order_index,title,content_json,meta_json,status,"
            "created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (course_id, user_id, scene_type, order_index, title, cdb.json_dump(content),
             cdb.json_dump(meta or {"actions": ANIM_ACTIONS}), "ready", now, now),
        ).lastrowid


def setup_animated_course(client, *, scene_count=2, actions=None, content=None):
    course = create_course(client)
    uid = user_id_of(client)
    for index in range(1, scene_count + 1):
        insert_scene(
            course["id"], uid, "slide", index, "场景 %d" % index,
            content or SLIDE_CONTENT, meta={"actions": actions or ANIM_ACTIONS},
        )
    return course, uid


def job_view(client, job_id):
    response = client.get(f"/api/ai/jobs/{job_id}")
    assert response.status_code == 200, response.text
    return response.json()


def wait_for_status(client, job_id, status, tries=400, delay=0.03):
    job = None
    for _ in range(tries):
        job = job_view(client, job_id)
        if job["status"] == status:
            return job
        if job["status"] in ("failed", "cancelled") and status not in (job["status"],):
            pytest.fail("任务提前进入 %s（期望 %s）：%s" % (job["status"], status, job.get("error")))
        time.sleep(delay)
    pytest.fail("任务未进入 %s，当前 %s" % (status, job))


def job_checkpoint(job_id):
    with db.connection() as con:
        raw = con.execute("SELECT checkpoint_json FROM ai_jobs WHERE id=?", (job_id,)).fetchone()[0]
    return json.loads(raw) if raw else {}


def insert_video_job(user_id, params, provider_id, model, *, token="video-anim-token",
                     status="queued", checkpoint=None):
    now = db.utcnow()
    request = {"task": video.TASK_NAME, "provider_id": provider_id, "model": model,
               "params": params, "requirements": ""}
    with db.connection() as con:
        return con.execute(
            "INSERT INTO ai_jobs(user_id,task,status,stage,request_json,partial_content,cancelled,"
            "created_at,updated_at,dispatch_token,checkpoint_json) VALUES (?,?,?,?,?,'',0,?,?,?,?)",
            (user_id, video.TASK_NAME, status, status, json.dumps(request, ensure_ascii=False),
             now, now, token,
             json.dumps(checkpoint, ensure_ascii=False) if checkpoint is not None else None),
        ).lastrowid


def run_job_sync(job_id, token="video-anim-token"):
    asyncio.run(ai_jobs.execute_dispatched_job(job_id, token, ai_jobs._run_job))


async def _maybe_await(value):
    if inspect.isawaitable(value):
        await value


async def _noop_ffmpeg(args, *, timeout=None):
    target = Path(str(args[-1]))
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.name.startswith("anim-seg-") or target.name.startswith("seg-"):
        target.write_bytes(b"FAKE-SEGMENT-" + target.name.encode())
    else:
        target.write_bytes(b"FAKE-ANIMATED-MP4" * 64)


async def _fake_probe(path):
    text = str(path)
    if "course.mp4" in text:
        return 4.5
    return None


_AVAILABLE = {"available": True, "detail": "测试可用", "render": {"available": True},
              "ffmpeg": {"available": True}, "limits": video.video_limits()}


@pytest.fixture
def video_ready(monkeypatch):
    """让视频能力探测在单测中恒为可用（动画截帧/ffmpeg 均 mock）。"""
    monkeypatch.setattr(video, "video_capabilities", lambda: dict(_AVAILABLE, limits=video.video_limits()))
    return True


def fake_static_capture_factory():
    state = {"calls": []}

    async def fake_capture(html, scene_count, *, indices=None, width=video.VIDEO_WIDTH,
                           height=video.VIDEO_HEIGHT, on_frame=None, is_cancelled=None):
        wanted = list(indices if indices is not None else range(scene_count))
        state["calls"].append(wanted)
        frames = {}
        for index in wanted:
            frames[index] = PNG_BYTES
            if on_frame is not None:
                await _maybe_await(on_frame(index, PNG_BYTES))
        return frames

    fake_capture.state = state
    return fake_capture


def fake_analytical_capture_factory(*, frame_counts, cancel_job_id=None, cancel_after_scene=None,
                                    resume_frames=True):
    """假逐帧截帧：写 ``frames/sNNN/00000.jpg…``，按场景回调 checkpoint；可模拟取消。"""
    state = {"calls": [], "resume_entries": {}, "written": {}}

    async def fake_capture(html, scene_labels, *, fps, durations_ms=None, work_dir,
                           scene_digests=None, audio_digests=None, resume_entries=None,
                           on_frame=None, on_scene_done=None, is_cancelled=None,
                           width=video.VIDEO_WIDTH, height=video.VIDEO_HEIGHT):
        state["calls"].append(list(scene_labels))
        state["resume_entries"] = dict(resume_entries or {})
        result = {}
        for index in range(len(scene_labels)):
            if cancel_after_scene is not None and cancel_job_id is not None \
                    and index > cancel_after_scene:
                with db.connection() as con:
                    con.execute(
                        "UPDATE ai_jobs SET cancelled=1,status='cancel_requested' WHERE id=?",
                        (cancel_job_id,),
                    )
                raise asyncio.CancelledError()
            count = int(frame_counts[index])
            directory = Path(work_dir) / video.ANIMATED_FRAMES_DIR / ("s%03d" % index)
            reused = bool(resume_frames and index in state["resume_entries"])
            if not reused:
                directory.mkdir(parents=True, exist_ok=True)
                for frame in range(count):
                    (directory / ("%05d.jpg" % frame)).write_bytes(JPEG_BYTES)
                    if on_frame is not None:
                        await _maybe_await(on_frame(index, frame + 1, count))
                state["written"][index] = count
            info = {
                "name": directory.name,
                "dir": directory,
                "frame_count": count,
                "duration_ms": int(round(count / float(fps) * 1000)),
                "fps": fps,
                "digest": (scene_digests or [""] * len(scene_labels))[index],
                "audio_digest": (audio_digests or [""] * len(scene_labels))[index],
                "bytes_total": count * len(JPEG_BYTES),
                "reused": reused,
            }
            result[index] = info
            if on_scene_done is not None:
                await _maybe_await(on_scene_done(index, info))
            if cancel_after_scene is not None and cancel_job_id is not None \
                    and index >= cancel_after_scene:
                with db.connection() as con:
                    con.execute(
                        "UPDATE ai_jobs SET cancelled=1,status='cancel_requested' WHERE id=?",
                        (cancel_job_id,),
                    )
                raise asyncio.CancelledError()
        return result

    fake_capture.state = state
    return fake_capture


# ---------------------------------------------------------------------------
# 1. 导出模板 capture API（seekMs / sceneDurationMs）与正常播放隔离
# ---------------------------------------------------------------------------

def test_offline_html_exposes_animated_capture_api(client):
    course, _uid = setup_animated_course(client, scene_count=1)
    response = client.get(f"/api/classroom/courses/{course['id']}/export?format=html")
    assert response.status_code == 200, response.text
    html_text = response.text
    # 新增逐帧动画接口
    assert "seekMs" in html_text and "sceneDurationMs" in html_text
    assert "gotoScene" in html_text and "gotoScene(index)" in html_text
    assert "actionDurationMs" in html_text and "applyCaptureState" in html_text
    # 效果层与字幕样式
    assert "fx-whiteboard" in html_text and "fx-spot" in html_text and "fx-laser" in html_text
    assert "capture-subtitle" in html_text
    # 正常播放器元素/捕获模式样式仍在（不影响既有离线播放）
    assert 'id="prev"' in html_text and 'id="next"' in html_text
    assert 'id="scene-stage"' in html_text and "body.capture" in html_text
    assert "window.__zhixuCapture" in html_text


# ---------------------------------------------------------------------------
# 2. 帧数 / 时间轴 / 估算（与 exports.py 内联 JS 同一套参数）
# ---------------------------------------------------------------------------

def test_animated_frame_times_and_counts():
    assert video.animated_frame_times(2000, 10) == list(range(0, 2000, 100))
    assert video.animated_frame_count(2000, 10) == 20
    assert video.animated_frame_count(5650, 8) == 46  # ceil(5.65×8)
    assert video.animated_frame_count(1, 5) == 1
    assert video.animated_frame_times(5650, 8)[-1] == 5625
    assert video.animated_frame_plan([2.0, 3.0], 10) == [20, 30]
    with pytest.raises(ValueError) as excinfo:
        video.animated_frame_plan([30.0], 12)  # 30s × 12fps = 360 > 300
    assert "单场景上限" in str(excinfo.value) and "300" in str(excinfo.value)
    with pytest.raises(ValueError) as excinfo:
        video.animated_frame_plan([10.0] * 121, 5)  # 每场景 50 帧 ≤300，总数 6050 > 6000
    assert "总帧数" in str(excinfo.value) and "6000" in str(excinfo.value)


def test_estimate_scene_duration_ms_matches_capture_timeline():
    def scene(actions):
        return {"meta_json": json.dumps({"actions": actions}, ensure_ascii=False)}

    # speech 11 字 = 800 + 11×250 = 3550ms；spotlight 700ms
    assert video.estimate_scene_duration_ms(scene(ANIM_ACTIONS)) == 4250
    assert video.estimate_scene_duration_ms(scene([])) == video.MIN_SCENE_MS
    assert video.estimate_scene_duration_ms(scene([{"type": "laser", "element_id": "t1"}])) == 1500
    whiteboard = {"type": "whiteboard", "ops": [{"kind": "line"}] * 20}  # 20×260=5200 → 上限 3000
    assert video.estimate_scene_duration_ms(scene([whiteboard])) == 3000
    assert video.estimate_scene_duration_ms(scene([{"type": "play_audio", "asset_id": 1}])) == 1500
    assert video.estimate_scene_duration_ms({"meta_json": "{}"}) == video.MIN_SCENE_MS


# ---------------------------------------------------------------------------
# 3. xfade / 逐帧片段 ffmpeg 参数
# ---------------------------------------------------------------------------

def test_xfade_arguments_and_offsets():
    assert video.xfade_offsets([2.0, 2.0, 2.0], 0.4) == [1.6, 3.2]
    assert video.effective_transition_seconds([2.0, 2.0], 400) == 0.4
    assert video.effective_transition_seconds([0.5, 2.0], 400) == 0.25  # 收敛到最短场景一半
    assert video.effective_transition_seconds([2.0], 400) == 0.0
    args = video.xfade_ffmpeg_args(
        segment_paths=["a.mp4", "b.mp4", "c.mp4"], durations=[2.0, 2.0, 2.0],
        transition_seconds=0.4, output_path="out.mp4", fps=12, transition="fade",
    )
    assert args.count("-i") == 3
    filter_complex = args[args.index("-filter_complex") + 1]
    assert "[0:v][1:v]xfade=transition=fade:duration=0.400:offset=1.600[v1]" in filter_complex
    assert "[v1][2:v]xfade=transition=fade:duration=0.400:offset=3.200[v2]" in filter_complex
    assert "[0:a][1:a]acrossfade=d=0.400:c1=tri:c2=tri[a1]" in filter_complex
    assert "[a1][2:a]acrossfade=d=0.400:c1=tri:c2=tri[a2]" in filter_complex
    assert args[args.index("-map") + 1] == "[v2]"
    assert "libx264" in args and "aac" in args and args[-1] == "out.mp4"
    with pytest.raises(ValueError):
        video.xfade_ffmpeg_args(
            segment_paths=["a.mp4"], durations=[2.0], transition_seconds=0.4,
            output_path="out.mp4", fps=12,
        )


def test_animated_segment_ffmpeg_args_reads_jpeg_sequence():
    args = video.animated_segment_ffmpeg_args(
        frame_dir=Path("frames/s000"), segment_path=Path("anim-seg-000.mp4"),
        duration=2.125, fps=8, audio_path=Path("audio-001.mp3"),
    )
    assert "-framerate" in args and args[args.index("-framerate") + 1] == "8"
    assert args[args.index("-start_number") + 1] == "0"
    pattern = args[args.index("-i") + 1]
    assert pattern.endswith("%05d.jpg") and "s000" in pattern
    assert "apad" in args  # 旁白不足补静音
    assert "libx264" in args and "aac" in args and "-map" in args
    assert args[args.index("-t") + 1] == "2.125"
    silent = video.animated_segment_ffmpeg_args(
        frame_dir=Path("frames/s001"), segment_path=Path("anim-seg-001.mp4"),
        duration=3.0, fps=8,
    )
    assert "anullsrc=channel_layout=stereo:sample_rate=44100" in silent


# ---------------------------------------------------------------------------
# 4. seekMs 调用序列（页面级截帧函数，用假 page 验证）
# ---------------------------------------------------------------------------

class FakePage:
    def __init__(self):
        self.seeks = []
        self.screenshots = 0

    async def evaluate(self, script, arg=None):
        assert "seekMs" in script
        self.seeks.append(arg)
        return arg

    async def screenshot(self, *, type="png", quality=None):
        assert type == "jpeg"
        self.screenshots += 1
        return JPEG_BYTES


def test_animated_scene_frames_seek_sequence(tmp_path):
    page = FakePage()
    frames_dir = tmp_path / "frames" / "s000"
    written = asyncio.run(video._animated_scene_frames(
        page, 0, duration_ms=2100, fps=10, image_dir=frames_dir,
    ))
    assert written == 21
    assert page.seeks == list(range(0, 2100, 100))  # 0,100,…,2000ms 升序
    files = sorted(frames_dir.glob("*.jpg"))
    assert len(files) == 21 and files[0].name == "00000.jpg" and files[-1].name == "00020.jpg"
    assert files[0].read_bytes().startswith(b"\xff\xd8")


def test_animated_scene_frames_cancel_stops_early(tmp_path):
    page = FakePage()
    frames_dir = tmp_path / "frames" / "s000"
    state = {"count": 0}

    def is_cancelled():
        state["count"] += 1
        return state["count"] > 3

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(video._animated_scene_frames(
            page, 0, duration_ms=2100, fps=10, image_dir=frames_dir,
            is_cancelled=is_cancelled,
        ))
    assert len(list(frames_dir.glob("*.jpg"))) == 3
    assert page.seeks == [0, 100, 200]


# ---------------------------------------------------------------------------
# 5. 任务成功路径：animated + fade 转场
# ---------------------------------------------------------------------------

def test_animated_render_success_with_fade_transition(client, monkeypatch, video_ready):
    course, uid = setup_animated_course(client, scene_count=2)
    monkeypatch.setattr(video, "_capture_animated_frames",
                        fake_analytical_capture_factory(frame_counts=[51, 51]))
    ffmpeg_calls = []

    async def fake_ffmpeg(args, *, timeout=None):
        ffmpeg_calls.append([str(item) for item in args])
        await _noop_ffmpeg(args)

    monkeypatch.setattr(video, "_run_ffmpeg", fake_ffmpeg)
    monkeypatch.setattr(video, "_probe_duration", _fake_probe)
    stages = []

    async def on_stage(text, done, total, **extra):
        stages.append((text, int(done), int(total)))

    started = time.time()
    result = asyncio.run(video.render_course_video(
        course["id"], uid, fps=12, mode="animated", transition="fade", transition_ms=400,
        on_stage=on_stage,
    ))
    elapsed = time.time() - started
    assert result["mode"] == "animated" and result["mode_used"] == "animated"
    assert result["animated"] is True and result["fallback_reason"] == ""
    assert result["frame_count"] == 102 and result["captured_frames"] == 102
    assert result["transition"] == "fade" and result["transition_ms"] == 400
    assert result["duration_seconds"] == 4.5
    assert result["has_audio"] is True  # animated 恒带 AAC 音轨（无旁白为静音）
    assert result["asset_id"] and result["url"].startswith("/api/classroom/assets/")
    assert "逐帧动画 102 帧" in result["summary"] and "fade 转场 400ms" in result["summary"]
    # 2 个逐帧片段 + 1 次 xfade 合成
    assert len(ffmpeg_calls) == 3
    first_segment, second_segment, xfade_args = ffmpeg_calls
    assert "libx264" in first_segment and "%05d.jpg" in " ".join(first_segment)
    assert "4.250" in first_segment  # 第 1 段时长 = 51 帧 / 12fps = 4.25s
    assert "xfade=transition=fade" in xfade_args[xfade_args.index("-filter_complex") + 1]
    assert "acrossfade" in xfade_args[xfade_args.index("-filter_complex") + 1]
    # SSE 中文阶段：逐帧进度 + 合成视频（转场）
    texts = [item[0] for item in stages]
    assert any(text.startswith("渲染第 1/2 场景 · 第 1/51 帧") for text in texts)
    assert any(text.startswith("渲染第 2/2 场景 · 第 51/51 帧") for text in texts)
    assert any(text.startswith("合成视频（转场）") and "fade 400 ms" in text for text in texts)
    assert elapsed < 10.0
    # 素材 meta 记录 mode/帧数/转场
    with db.connection() as con:
        row = dict(con.execute("SELECT * FROM cls_assets WHERE id=?", (result["asset_id"],)).fetchone())
    meta = cdb.json_load(row["meta_json"], {})
    assert meta["mode"] == "animated" and meta["animated"] is True
    assert meta["frame_count"] == 102 and meta["transition"] == "fade" and meta["transition_ms"] == 400
    # 下载最近产物
    download = client.get(f"/api/classroom/courses/{course['id']}/export?format=mp4")
    assert download.status_code == 200 and download.content.startswith(b"FAKE-ANIMATED-MP4")


def test_animated_transition_none_uses_concat(client, monkeypatch, video_ready):
    course, uid = setup_animated_course(client, scene_count=2)
    monkeypatch.setattr(video, "_capture_animated_frames",
                        fake_analytical_capture_factory(frame_counts=[24, 30]))
    ffmpeg_calls = []

    async def fake_ffmpeg(args, *, timeout=None):
        ffmpeg_calls.append([str(item) for item in args])
        await _noop_ffmpeg(args)

    monkeypatch.setattr(video, "_run_ffmpeg", fake_ffmpeg)
    monkeypatch.setattr(video, "_probe_duration", _fake_probe)
    result = asyncio.run(video.render_course_video(
        course["id"], uid, fps=8, mode="animated", transition="none",
    ))
    assert result["mode_used"] == "animated" and result["transition"] == "none"
    assert result["transition_ms"] == 0 and result["frame_count"] == 54
    assert len(ffmpeg_calls) == 3  # 2 片段 + concat
    concat_args = ffmpeg_calls[-1]
    assert "concat" in concat_args and "copy" in concat_args
    assert not any("xfade" in item for item in concat_args)


# ---------------------------------------------------------------------------
# 6. 降级：seek 接口不可用 → static
# ---------------------------------------------------------------------------

def test_animated_falls_back_to_static_when_seek_unavailable(client, monkeypatch, video_ready):
    course, uid = setup_animated_course(client, scene_count=2)
    static_capture = fake_static_capture_factory()
    ffmpeg_calls = []

    async def fake_ffmpeg(args, *, timeout=None):
        ffmpeg_calls.append([str(item) for item in args])
        await _noop_ffmpeg(args)

    async def unavailable_capture(*_args, **_kwargs):
        raise video.AnimatedUnavailable(
            "离线播放器缺少逐帧动画接口（seekMs），已降级为静态画面"
        )

    monkeypatch.setattr(video, "_capture_animated_frames", unavailable_capture)
    monkeypatch.setattr(video, "_capture_scene_pngs", static_capture)
    monkeypatch.setattr(video, "_run_ffmpeg", fake_ffmpeg)
    monkeypatch.setattr(video, "_probe_duration", _fake_probe)
    stages = []

    async def on_stage(text, done, total, **extra):
        stages.append(text)

    result = asyncio.run(video.render_course_video(
        course["id"], uid, fps=8, mode="animated", transition="fade",
        on_stage=on_stage,
    ))
    assert result["mode"] == "animated" and result["mode_used"] == "static"
    assert result["animated"] is False
    assert "seekMs" in result["fallback_reason"]
    assert result["frame_count"] == 0 and result["transition"] == "none"
    assert result["has_audio"] is False  # 无旁白 static 仍为纯视频
    assert "降级为静态画面" in result["summary"]
    assert static_capture.state["calls"] == [[0, 1]]
    assert len(ffmpeg_calls) == 3  # 2 静帧片段 + concat
    assert any("降级为静态画面" in text for text in stages)
    with db.connection() as con:
        row = dict(con.execute("SELECT * FROM cls_assets WHERE id=?", (result["asset_id"],)).fetchone())
    meta = cdb.json_load(row["meta_json"], {})
    assert meta["mode"] == "static" and meta["animated"] is False
    assert "seekMs" in meta["fallback_reason"]
    # 降级后仍可正常下载
    download = client.get(f"/api/classroom/courses/{course['id']}/export?format=mp4")
    assert download.status_code == 200 and download.content.startswith(b"FAKE-ANIMATED-MP4")


# ---------------------------------------------------------------------------
# 7. 路由参数（Pydantic body 扩展 / 422 中文 / limits）
# ---------------------------------------------------------------------------

def test_animated_route_accepts_mode_and_rejects_invalid_values(client, video_ready):
    course, _uid = setup_animated_course(client, scene_count=1)
    save_provider(client)
    url = f"/api/classroom/courses/{course['id']}/video"
    invalid_mode = client.post(url, json={"mode": "super"})
    assert invalid_mode.status_code == 422
    assert "视频模式" in invalid_mode.json()["detail"]
    invalid_transition = client.post(url, json={"mode": "animated", "transition": "spin"})
    assert invalid_transition.status_code == 422
    assert "场景转场" in invalid_transition.json()["detail"]
    assert client.post(url, json={"mode": "animated", "transition_ms": 50}).status_code == 422
    too_slow = client.post(url, json={"mode": "animated", "transition_ms": 1200})
    assert too_slow.status_code == 422 and "转场时长" in too_slow.json()["detail"]

    response = client.post(url, json={
        "mode": "animated", "fps": 10, "transition": "fade", "transition_ms": 600,
    })
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["mode"] == "animated" and body["transition"] == "fade" and body["transition_ms"] == 600
    request = body["job"]["request"]
    assert request["params"]["mode"] == "animated"
    assert request["params"]["transition_ms"] == 600
    status_payload = client.get(url).json()
    limits = status_payload["limits"]
    assert limits["modes"] == ["static", "animated"]
    assert limits["transitions"] == ["none", "fade"]
    assert limits["max_frames_per_scene"] == 300
    assert limits["max_frames_total"] == 6000
    assert limits["transition_ms_min"] == 100 and limits["transition_ms_max"] == 1000


def test_animated_route_rejects_scene_over_frame_limit(client, video_ready):
    long_speech = [{"id": "s1", "type": "speech", "text": "很长的旁白" * 40}]  # 200 字 → 50.8s
    course, _uid = setup_animated_course(client, scene_count=1, actions=long_speech)
    save_provider(client)
    response = client.post(
        f"/api/classroom/courses/{course['id']}/video", json={"mode": "animated", "fps": 12},
    )
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert "单场景上限" in detail and "300" in detail


# ---------------------------------------------------------------------------
# 8. 取消清理 + checkpoint 按场景复用
# ---------------------------------------------------------------------------

def test_animated_cancel_cleans_incomplete_scene_frames_then_resumes(client, monkeypatch, video_ready):
    course, uid = setup_animated_course(client, scene_count=2)
    provider = save_provider(client)
    job_id = insert_video_job(
        uid, {"course_id": course["id"], "fps": 8, "mode": "animated",
              "transition": "fade", "transition_ms": 400},
        provider["id"], "test-model", token="video-anim-cancel-token",
    )
    first_capture = fake_analytical_capture_factory(
        frame_counts=[24, 30], cancel_job_id=job_id, cancel_after_scene=0,
    )
    monkeypatch.setattr(video, "_capture_animated_frames", first_capture)
    monkeypatch.setattr(video, "_run_ffmpeg", _noop_ffmpeg)
    monkeypatch.setattr(video, "_probe_duration", _fake_probe)

    run_job_sync(job_id, "video-anim-cancel-token")
    job = job_view(client, job_id)
    assert job["status"] == "cancelled"
    checkpoint = job_checkpoint(job_id)
    anim = checkpoint.get("anim") or {}
    scene_id = cdb.course_scenes(course["id"], uid)[0]["id"]
    assert str(scene_id) in (anim.get("scenes") or {})
    work_dir = Path(db.DATA_DIR) / "classroom" / "video-work" / ("job-%d" % job_id)
    completed_dir = work_dir / video.ANIMATED_FRAMES_DIR / "s000"
    assert completed_dir.is_dir() and len(list(completed_dir.glob("*.jpg"))) == 24
    assert not (work_dir / video.ANIMATED_FRAMES_DIR / "s001").exists()  # 未完成场景帧已清理
    assert not list(work_dir.glob("anim-seg-*.mp4")) and not (work_dir / "course.mp4").exists()
    with db.connection() as con:
        assert con.execute(
            "SELECT COUNT(*) FROM cls_assets WHERE user_id=? AND kind='video'", (uid,),
        ).fetchone()[0] == 0

    # 续跑：第 1 个场景的 24 帧直接复用（fake 通过 resume_entries 判断），只补第 2 个场景
    resume_capture = fake_analytical_capture_factory(frame_counts=[24, 30])
    monkeypatch.setattr(video, "_capture_animated_frames", resume_capture)
    ffmpeg_calls = []

    async def fake_ffmpeg(args, *, timeout=None):
        ffmpeg_calls.append([str(item) for item in args])
        await _noop_ffmpeg(args)

    monkeypatch.setattr(video, "_run_ffmpeg", fake_ffmpeg)
    resumed = client.post(f"/api/ai/jobs/{job_id}/resume")
    assert resumed.status_code == 200, resumed.text
    done = wait_for_status(client, job_id, "completed")
    result = done["result"]
    assert result["mode_used"] == "animated"
    assert resume_capture.state["resume_entries"] and 0 in resume_capture.state["resume_entries"]
    assert result["reused_frames"] == 24 and result["captured_frames"] == 30
    assert result["frame_count"] == 54
    assert len(ffmpeg_calls) == 3  # 2 片段 + xfade
    # 成功落库后工作目录清理
    assert not work_dir.exists()


def test_reusable_frame_set_validation(tmp_path):
    directory = tmp_path / "frames" / "s000"
    directory.mkdir(parents=True)
    for frame in range(3):
        (directory / ("%05d.jpg" % frame)).write_bytes(JPEG_BYTES)
    entry = {
        "name": "s000", "frame_count": 3, "duration_ms": 2000, "fps": 10,
        "digest": "d1", "audio_digest": "none", "bytes_total": 3 * len(JPEG_BYTES),
    }
    assert video._reusable_frame_set(
        entry, directory, frame_count=3, duration_ms=2000, fps=10, digest="d1", audio_digest="none",
    ) is True
    assert video._reusable_frame_set(
        entry, directory, frame_count=3, duration_ms=2000, fps=10, digest="d2", audio_digest="none",
    ) is False  # 场景内容变化
    assert video._reusable_frame_set(
        entry, directory, frame_count=3, duration_ms=2000, fps=10, digest="d1", audio_digest="audio-x",
    ) is False  # 旁白音频变化
    assert video._reusable_frame_set(
        entry, directory, frame_count=4, duration_ms=2000, fps=10, digest="d1", audio_digest="none",
    ) is False  # 帧数变化
    assert video._reusable_frame_set(
        None, directory, frame_count=3, duration_ms=2000, fps=10, digest="d1", audio_digest="none",
    ) is False
    (directory / "00002.jpg").unlink()
    assert video._reusable_frame_set(
        entry, directory, frame_count=3, duration_ms=2000, fps=10, digest="d1", audio_digest="none",
    ) is False  # 帧文件缺失


# ---------------------------------------------------------------------------
# 9. 真实渲染冒烟（Playwright + ffmpeg；缺引擎自动 skip，产物写 .omx/tmp）
# ---------------------------------------------------------------------------

def _real_environment_ready() -> bool:
    try:
        from app.ai import render as ai_render

        return bool(
            ai_render.render_available().get("available")
            and shutil.which("ffmpeg")
            and shutil.which("ffprobe")
        )
    except Exception:  # noqa: BLE001
        return False


REAL_ENV = _real_environment_ready()
SMOKE_OUT = Path(__file__).resolve().parents[1] / ".omx" / "tmp"


@pytest.mark.skipif(not REAL_ENV, reason="需要 Playwright 渲染引擎与 ffmpeg/ffprobe")
def test_real_animated_render_generates_short_mp4_with_transition(client):
    """真实冒烟：speech+spotlight 短课 → animated 生成约 3.7 秒 H.264+AAC（xfade 转场）。"""
    course = create_course(client, topic="逐帧动画冒烟")
    uid = user_id_of(client)
    actions_one = [
        {"id": "a1", "type": "speech", "text": "看这里。"},  # 4 字 → 1800ms
        {"id": "a2", "type": "spotlight", "element_id": "t1", "dim_opacity": 0.5},
    ]
    actions_two = [{"id": "b1", "type": "speech", "text": "结束。"}]  # 3 字 → 1550ms
    insert_scene(course["id"], uid, "slide", 1, "聚光灯", SHORT_SLIDE_CONTENT,
                 meta={"actions": actions_one})
    insert_scene(course["id"], uid, "slide", 2, "总结",
                 {"type": "slide", "canvas": {"width": 1000, "height": 562.5, "theme": {}, "elements": [
                     {"id": "t1", "type": "text", "left": 100, "top": 80, "width": 800, "height": 100,
                      "text": "总结", "fontSize": 36, "color": "#1f2937"},
                 ]}}, meta={"actions": actions_two})
    save_provider(client)
    started = time.time()
    try:
        result = asyncio.run(video.render_course_video(
            course["id"], uid, fps=8, mode="animated", transition="fade", transition_ms=400,
        ))
    except Exception as exc:  # noqa: BLE001 - 本机浏览器不可用时按 skip 处理
        if "渲染" in str(exc) or "ffmpeg" in str(exc):
            pytest.skip("真实渲染环境不可用：%s" % str(exc)[:160])
        raise
    elapsed = time.time() - started
    assert result["mode_used"] == "animated" and result["fallback_reason"] == ""
    assert result["frame_count"] > 0 and result["size_bytes"] > 1000
    assert result["transition"] == "fade" and result["transition_ms"] == 400
    with db.connection() as con:
        row = dict(con.execute("SELECT * FROM cls_assets WHERE id=?", (result["asset_id"],)).fetchone())
    from app import storage

    source = Path(storage.local_path(video.VIDEO_FOLDER, row["storage_name"]))
    assert source.is_file()
    probed = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries",
         "format=duration:stream=codec_name,codec_type,width,height", "-of", "json", str(source)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    assert probed.returncode == 0, probed.stderr
    info = json.loads(probed.stdout)
    streams = {stream["codec_type"]: stream for stream in info["streams"]}
    assert streams["video"]["codec_name"] == "h264"
    assert streams["video"]["width"] == video.VIDEO_WIDTH
    assert streams["video"]["height"] == video.VIDEO_HEIGHT
    assert streams["audio"]["codec_name"] == "aac"  # animated 恒带 AAC 音轨
    duration = float(info["format"]["duration"])
    assert 3.0 <= duration <= 6.0, "animated 冒烟视频时长应 3–6 秒，实际 %.3f" % duration
    SMOKE_OUT.mkdir(parents=True, exist_ok=True)
    target = SMOKE_OUT / "video-anim-smoke.mp4"
    shutil.copyfile(source, target)
    assert target.stat().st_size == result["size_bytes"]
    # 生成耗时（供验收报告记录；真实浏览器截图受机器影响，只做宽松上限）
    assert elapsed < 120.0
    print("animated 冒烟：%.3f 秒生成 %.3f 秒视频（%d 帧，%d 字节）"
          % (elapsed, duration, result["frame_count"], result["size_bytes"]))
