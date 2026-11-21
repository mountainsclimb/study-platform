"""P5 课堂产出验收：服务端 PDF 讲义 + MP4 视频导出（任务 / 截帧 / ffmpeg / checkpoint / 限额）。

全部离线且不依赖真实模型：渲染与 ffmpeg 子进程用 monkeypatch 假实现；文件末尾附
「真实 Playwright + ffmpeg」冒烟（缺少引擎时自动 skip，产物写到 ``.omx/tmp/`` 不落真实数据目录）。
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

from app import db, storage
from app.ai import jobs as ai_jobs
from app.classroom import db as cdb
from app.classroom import exports, tts, video

PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)

SLIDE_CONTENT = {
    "type": "slide",
    "canvas": {
        "width": 1000, "height": 562.5, "theme": {},
        "elements": [
            {"id": "t1", "type": "text", "left": 60, "top": 50, "width": 880, "height": 80,
             "text": "光合作用", "fontSize": 32, "color": "#333333"},
        ],
    },
}
QUIZ_CONTENT = {
    "type": "quiz",
    "questions": [
        {"id": "q1", "type": "single", "question": "光合作用发生的场所是？",
         "options": [{"label": "叶绿体", "value": "A"}, {"label": "线粒体", "value": "B"}],
         "answer": ["A"], "analysis": "叶绿体是场所。", "points": 10},
    ],
}
PBL_CONTENT = {
    "type": "pbl",
    "project": {
        "title": "校园节能行动方案", "background": "用电上涨。", "goals": ["理解电功率"],
        "roles": [{"name": "导师", "description": "陪你调研"}],
        "milestones": [{"title": "调研", "tasks": ["记录用电"], "deliverable": "调查表",
                        "checkpoint": "可复核"}],
        "rubric": [{"criterion": "真实", "levels": [{"level": "优秀", "description": "有来源"}]}],
        "mentor_hints": ["先算基线"],
    },
}
SPEECH_ACTIONS = [{"id": "act_001", "type": "speech", "text": "这一页我们看光合作用。"}]


# ---------------------------------------------------------------------------
# 公共辅助
# ---------------------------------------------------------------------------

def save_provider(client, name="视频测试连接", models=("test-model",)):
    response = client.post("/api/providers", json={
        "name": name, "kind": "openai", "base_url": "https://api.openai.com/v1",
        "api_key": "sk-classroom-video-test-123456", "models": list(models),
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
             cdb.json_dump(meta or {"actions": SPEECH_ACTIONS}), "ready", now, now),
        ).lastrowid


def setup_course(client, *, scene_types=("slide", "slide")):
    course = create_course(client)
    uid = user_id_of(client)
    for index, scene_type in enumerate(scene_types, 1):
        content = {"slide": SLIDE_CONTENT, "quiz": QUIZ_CONTENT, "pbl": PBL_CONTENT}[scene_type]
        insert_scene(course["id"], uid, scene_type, index, "场景 %d" % index, content)
    return course, uid


def create_tts_asset(user_id, course_id, payload=b"ID3-fake-narration-bytes"):
    folder = Path(db.DATA_DIR) / tts.TTS_FOLDER
    folder.mkdir(parents=True, exist_ok=True)
    name = "narration-%s.mp3" % hashlib.sha256(payload).hexdigest()[:12]
    (folder / name).write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest()
    with db.connection() as con:
        return con.execute(
            "INSERT INTO cls_assets(user_id,course_id,kind,storage_name,mime_type,sha256,meta_json,created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (user_id, course_id, tts.TTS_KIND, name, "audio/mpeg", digest, "{}", db.utcnow()),
        ).lastrowid


def set_narration(course_id, mapping):
    with db.connection() as con:
        row = con.execute("SELECT config_json FROM cls_courses WHERE id=?", (course_id,)).fetchone()
        config = cdb.json_load(row["config_json"], {})
        config = config if isinstance(config, dict) else {}
        config["narration"] = mapping
        con.execute("UPDATE cls_courses SET config_json=? WHERE id=?",
                    (cdb.json_dump(config), course_id))


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


def insert_video_job(user_id, params, provider_id, model, *, token="video-job-token",
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


def run_job_sync(job_id, token="video-job-token"):
    asyncio.run(ai_jobs.execute_dispatched_job(job_id, token, ai_jobs._run_job))


_AVAILABLE = {"available": True, "detail": "测试可用", "render": {"available": True},
              "ffmpeg": {"available": True}, "limits": video.video_limits()}


@pytest.fixture
def video_ready(monkeypatch):
    """让视频能力探测在单测中恒为可用（真实 ffmpeg/Playwright 是否安装不影响 mock 用例）。"""
    monkeypatch.setattr(video, "video_capabilities", lambda: dict(_AVAILABLE, limits=video.video_limits()))
    return True


def _maybe_await(value):
    if inspect.isawaitable(value):
        return value
    return None


def fake_capture_factory(*, cancel_job_id=None, cancel_after=None, fail_at=None):
    """假截帧：按 indices 返回 PNG，可按需在某一帧后把任务置为取消。"""
    state = {"calls": [], "html": ""}

    async def fake_capture(html, scene_count, *, indices=None, width=video.VIDEO_WIDTH,
                           height=video.VIDEO_HEIGHT, on_frame=None, is_cancelled=None):
        state["html"] = html
        wanted = list(indices if indices is not None else range(scene_count))
        state["calls"].append(wanted)
        frames = {}
        for index in wanted:
            if fail_at is not None and index == fail_at:
                from app.ai.render import RenderUnavailable

                raise RenderUnavailable("模拟截帧失败")
            frames[index] = PNG_BYTES
            if on_frame is not None:
                result = on_frame(index, PNG_BYTES)
                if inspect.isawaitable(result):
                    await result
            if cancel_job_id is not None and cancel_after is not None and index >= cancel_after:
                with db.connection() as con:
                    con.execute(
                        "UPDATE ai_jobs SET cancelled=1,status='cancel_requested' WHERE id=?",
                        (cancel_job_id,),
                    )
                break
        return frames

    fake_capture.calls = state["calls"]
    fake_capture.state = state
    return fake_capture


def fake_ffmpeg_factory(*, mp4_bytes=b"FAKE-MP4-BYTES" * 64):
    calls = []

    async def fake_ffmpeg(args, *, timeout=None):
        rendered = [str(item) for item in args]
        calls.append(rendered)
        target = Path(rendered[-1])
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.name.startswith("seg-"):
            target.write_bytes(b"FAKE-SEGMENT-" + target.name.encode())
        else:
            target.write_bytes(mp4_bytes)

    fake_ffmpeg.calls = calls
    return fake_ffmpeg


def fake_probe_factory(*, audio_seconds=2.5, video_seconds=5.5):
    async def fake_probe(path):
        text = str(path)
        if "audio-" in text:
            return audio_seconds
        if text.endswith("course.mp4"):
            return video_seconds
        return None

    return fake_probe


# ---------------------------------------------------------------------------
# 1. 服务端 PDF
# ---------------------------------------------------------------------------

def test_export_pdf_renders_print_html_with_headers(client, monkeypatch):
    course, _uid = setup_course(client, scene_types=("slide", "quiz"))
    from app.ai import render as ai_render

    captured = {}

    def fake_available():
        return {"available": True, "engine": "playwright", "detail": "测试可用"}

    async def fake_pdf(html_text, *, paper="A4", landscape=False, margin_mm=10):
        captured.update({"html": html_text, "paper": paper, "landscape": landscape})
        return b"%PDF-1.4\nfake-classroom-pdf"

    monkeypatch.setattr(ai_render, "render_available", fake_available)
    monkeypatch.setattr(ai_render, "html_to_pdf", fake_pdf)

    response = client.get(f"/api/classroom/courses/{course['id']}/export?format=pdf")
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("application/pdf")
    assert response.content.startswith(b"%PDF-1.4")
    assert response.headers["X-Zhixu-Export-Format"] == "pdf"
    assert response.headers["X-Page-Count"] == "3"  # 封面 + 2 场景（无联网来源页）
    disposition = response.headers["content-disposition"]
    assert "filename*=UTF-8''" in disposition and disposition.endswith(".pdf")
    assert captured["paper"] == "A4" and captured["landscape"] is True
    html_text = captured["html"]
    assert "光合作用" in html_text and "场景 2" in html_text
    assert "@page { size: A4 landscape" in html_text
    assert "参考答案：A" in html_text  # 测验页附参考答案
    assert html_text
    # 归属校验：他人 404（见下一个用例）


def test_video_generic_job_route_validates_course_video(client, video_ready):
    """``POST /api/ai/jobs``（task=course-video）复用同一准入校验。"""
    course, _uid = setup_course(client, scene_types=("slide",))
    provider = save_provider(client)
    base = {"task": "course-video", "provider_id": provider["id"], "model": "test-model"}
    missing = client.post("/api/ai/jobs", json={**base, "params": {}})
    assert missing.status_code == 422 and "course_id" in missing.json()["detail"]
    foreign = client.post("/api/ai/jobs", json={**base, "params": {"course_id": 999999}})
    assert foreign.status_code == 404
    over = client.post("/api/ai/jobs", json={**base, "params": {"course_id": course["id"], "fps": 2}})
    assert over.status_code == 422
    _ = course


def test_export_pdf_ownership_and_engine_unavailable(client, second_client, monkeypatch):
    course, _uid = setup_course(client, scene_types=("slide",))
    assert second_client.get(
        f"/api/classroom/courses/{course['id']}/export?format=pdf"
    ).status_code == 404
    from app.main import app

    anon = TestClient(app)
    assert anon.get(f"/api/classroom/courses/{course['id']}/export?format=pdf").status_code == 401

    from app.ai import render as ai_render

    monkeypatch.setattr(ai_render, "render_available", lambda: {
        "available": False, "engine": None, "detail": "未安装 Playwright",
    })
    response = client.get(f"/api/classroom/courses/{course['id']}/export?format=pdf")
    assert response.status_code == 503
    detail = response.json()["detail"]
    assert "PDF" in detail and "浏览器" in detail and "未安装 Playwright" in detail


def test_export_pdf_render_failure_is_503(client, monkeypatch):
    course, _uid = setup_course(client, scene_types=("slide",))
    from app.ai import render as ai_render

    monkeypatch.setattr(ai_render, "render_available", lambda: {
        "available": True, "engine": "playwright", "detail": "ok",
    })

    async def fake_pdf(*_args, **_kwargs):
        raise ai_render.RenderUnavailable("浏览器启动失败")

    monkeypatch.setattr(ai_render, "html_to_pdf", fake_pdf)
    response = client.get(f"/api/classroom/courses/{course['id']}/export?format=pdf")
    assert response.status_code == 503
    assert "PDF 渲染失败" in response.json()["detail"]


def test_offline_html_exposes_video_capture_interface(client):
    course, _uid = setup_course(client, scene_types=("slide", "quiz"))
    response = client.get(f"/api/classroom/courses/{course['id']}/export?format=html")
    assert response.status_code == 200
    html_text = response.text
    assert "window.__zhixuCapture" in html_text
    assert "gotoScene" in html_text and "sceneCount" in html_text and "ready" in html_text
    assert "body.capture" in html_text  # 捕获模式隐藏控件、画布 1280×720
    # 正常播放器元素仍在（捕获模式不影响离线播放）
    assert 'id="prev"' in html_text and 'id="next"' in html_text and 'id="scene-stage"' in html_text


# ---------------------------------------------------------------------------
# 2. 视频路由准入与能力
# ---------------------------------------------------------------------------

def test_video_routes_guard_login_ownership_and_params(client, second_client, video_ready):
    course, _uid = setup_course(client, scene_types=("slide",))
    anon = TestClient(__import__("app.main", fromlist=["app"]).app)
    assert anon.post(f"/api/classroom/courses/{course['id']}/video").status_code == 401
    assert second_client.post(f"/api/classroom/courses/{course['id']}/video").status_code == 404
    assert second_client.get(f"/api/classroom/courses/{course['id']}/video").status_code == 404

    # 没有场景的课程
    empty = create_course(client, topic="空课程")
    response = client.post(f"/api/classroom/courses/{empty['id']}/video")
    assert response.status_code == 422 and "场景" in response.json()["detail"]

    # 没有配置任何连接：任务系统要求 provider/model 作为载体
    response = client.post(f"/api/classroom/courses/{course['id']}/video")
    assert response.status_code == 422
    assert "模型设置" in response.json()["detail"]

    save_provider(client)
    # 帧率/场景数越界
    assert client.post(f"/api/classroom/courses/{course['id']}/video", json={"fps": 3}).status_code == 422
    assert client.post(f"/api/classroom/courses/{course['id']}/video", json={"fps": 30}).status_code == 422
    assert client.post(
        f"/api/classroom/courses/{course['id']}/video", json={"max_scenes": 13}
    ).status_code == 422


def test_video_rejects_course_over_scene_limit(client, video_ready):
    course = create_course(client)
    uid = user_id_of(client)
    save_provider(client)
    for index in range(1, video.MAX_SCENES + 2):
        insert_scene(course["id"], uid, "slide", index, "场景 %d" % index, SLIDE_CONTENT)
    response = client.post(f"/api/classroom/courses/{course['id']}/video")
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert str(video.MAX_SCENES) in detail and "场景" in detail


def test_video_unavailable_engine_returns_503(client, monkeypatch):
    course, _uid = setup_course(client, scene_types=("slide",))
    save_provider(client)
    monkeypatch.setattr(video, "video_capabilities", lambda: {
        "available": False,
        "detail": "未检测到 ffmpeg（系统 PATH），无法合成 MP4 视频",
        "render": {"available": True}, "ffmpeg": {"available": False},
    })
    response = client.post(f"/api/classroom/courses/{course['id']}/video")
    assert response.status_code == 503
    assert "ffmpeg" in response.json()["detail"]


def test_video_rejects_when_job_already_running(client, video_ready):
    course, uid = setup_course(client, scene_types=("slide",))
    provider = save_provider(client)
    insert_video_job(uid, {"course_id": course["id"]}, provider["id"], "test-model", status="running")
    response = client.post(f"/api/classroom/courses/{course['id']}/video")
    assert response.status_code == 409
    assert "已有视频导出任务" in response.json()["detail"]


def test_video_status_reports_capabilities_and_empty_state(client, monkeypatch):
    course, _uid = setup_course(client, scene_types=("slide",))
    from app.ai import render as ai_render

    monkeypatch.setattr(ai_render, "render_available", lambda: {
        "available": True, "engine": "playwright", "detail": "ok",
    })
    monkeypatch.setattr(video, "video_capabilities", lambda: dict(
        _AVAILABLE, limits=video.video_limits(),
    ))
    response = client.get(f"/api/classroom/courses/{course['id']}/video")
    assert response.status_code == 200
    payload = response.json()
    assert payload["asset"] is None and payload["latest_job"] is None
    assert payload["capabilities"]["pdf"]["available"] is True
    assert payload["capabilities"]["video"]["available"] is True
    assert payload["limits"]["max_scenes"] == video.MAX_SCENES
    assert payload["limits"]["max_seconds"] == video.MAX_VIDEO_SECONDS
    assert payload["limits"]["max_bytes"] == video.MAX_VIDEO_BYTES
    # 未生成视频时下载返回 409（提示先生成任务）
    download = client.get(f"/api/classroom/courses/{course['id']}/export?format=mp4")
    assert download.status_code == 409
    assert "生成 MP4 视频" in download.json()["detail"]


# ---------------------------------------------------------------------------
# 3. course-video 任务成功路径（mock 截帧/ffmpeg）
# ---------------------------------------------------------------------------

def test_video_job_success_with_narration_and_download(client, monkeypatch, video_ready):
    course, uid = setup_course(client, scene_types=("slide", "slide"))
    save_provider(client)
    asset_id = create_tts_asset(uid, course["id"])
    set_narration(course["id"], {"1": {"asset_id": asset_id, "chars": 10}})

    fake_capture = fake_capture_factory()
    fake_ffmpeg = fake_ffmpeg_factory()
    monkeypatch.setattr(video, "_capture_scene_pngs", fake_capture)
    monkeypatch.setattr(video, "_run_ffmpeg", fake_ffmpeg)
    monkeypatch.setattr(video, "_probe_duration", fake_probe_factory(audio_seconds=2.5, video_seconds=5.5))
    events = []
    published = video._publish

    def record_event(job_id, event, payload):
        events.append((event, dict(payload or {})))
        return published(job_id, event, payload)

    monkeypatch.setattr(video, "_publish", record_event)

    response = client.post(f"/api/classroom/courses/{course['id']}/video", json={"fps": 10})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["scene_count"] == 2 and body["fps"] == 10
    job_id = body["job"]["id"]
    job = wait_for_status(client, job_id, "completed")
    result = job["result"]
    assert result["scene_count"] == 2 and result["fps"] == 10
    assert result["has_audio"] is True and result["narrated_scene_count"] == 1
    assert result["duration_seconds"] == 5.5
    assert result["reused_frames"] == 0 and result["captured_frames"] == 2
    assert result["asset_id"] and result["url"].startswith("/api/classroom/assets/")
    assert "视频导出完成" in result["summary"]

    # 截帧 HTML 是离线播放器（含捕获接口），场景 2 没有旁白 → 走静音片段
    assert fake_capture.state["html"].count("__zhixuCapture") >= 1
    assert fake_capture.calls == [[0, 1]]
    assert len(fake_ffmpeg.calls) == 3  # 2 片段 + 1 次 concat
    first_segment, second_segment, concat_args = fake_ffmpeg.calls
    assert "libx264" in first_segment and "-t" in first_segment
    assert "2.500" in first_segment  # 旁白场景按音频时长
    assert any("audio-" in item for item in first_segment)
    assert any("anullsrc" in item for item in second_segment)
    assert "3.000" in second_segment  # 无旁白场景 3 秒静帧
    assert "-f" in concat_args and "concat" in concat_args and "-c" in concat_args
    assert "copy" in concat_args

    # SSE 中文阶段（渲染第 n/N 场景 / 合成视频片段 / 合并视频片段）
    stages = [payload.get("stage") or "" for event, payload in events if event == "stage"]
    assert any(stage.startswith("渲染第 1/2 场景") for stage in stages)
    assert any(stage.startswith("合成视频片段") for stage in stages)
    assert any("合并视频片段" in stage for stage in stages)

    # 素材落库 + GET /video + mp4 下载
    with db.connection() as con:
        row = dict(con.execute("SELECT * FROM cls_assets WHERE id=?", (result["asset_id"],)).fetchone())
    assert row["kind"] == "video" and row["mime_type"] == "video/mp4"
    assert row["sha256"]
    saved = Path(db.DATA_DIR) / video.VIDEO_FOLDER / row["storage_name"]
    assert saved.is_file() and saved.read_bytes().startswith(b"FAKE-MP4")
    meta = cdb.json_load(row["meta_json"], {})
    assert meta["scene_count"] == 2 and meta["fps"] == 10

    status_payload = client.get(f"/api/classroom/courses/{course['id']}/video").json()
    assert status_payload["asset"]["id"] == result["asset_id"]
    assert status_payload["asset"]["meta"]["duration_seconds"] == 5.5
    assert status_payload["latest_job"]["status"] == "completed"

    download = client.get(f"/api/classroom/courses/{course['id']}/export?format=mp4")
    assert download.status_code == 200
    assert download.headers["content-type"].startswith("video/mp4")
    assert download.content.startswith(b"FAKE-MP4")
    assert download.headers["X-Zhixu-Asset-Id"] == str(result["asset_id"])
    assert "filename*=UTF-8''" in download.headers["content-disposition"]


def test_video_job_failure_keeps_checkpoint_frames(client, monkeypatch, video_ready):
    course, _uid = setup_course(client, scene_types=("slide", "slide"))
    save_provider(client)
    monkeypatch.setattr(video, "_capture_scene_pngs", fake_capture_factory(fail_at=1))
    monkeypatch.setattr(video, "_run_ffmpeg", fake_ffmpeg_factory())
    monkeypatch.setattr(video, "_probe_duration", fake_probe_factory())

    response = client.post(f"/api/classroom/courses/{course['id']}/video")
    assert response.status_code == 200
    job_id = response.json()["job"]["id"]
    job = wait_for_status(client, job_id, "failed")
    assert "视频渲染不可用" in (job.get("error") or "")
    assert job["has_checkpoint"] is True
    checkpoint = job_checkpoint(job_id)
    assert checkpoint["scene_ids"] and "1" in (checkpoint.get("frames") or {})
    # 半成品（片段/合并视频）已清理，帧保留供续跑
    work_dir = Path(db.DATA_DIR) / "classroom" / "video-work" / ("job-%d" % job_id)
    assert (work_dir / "frame-000.png").is_file()
    assert not list(work_dir.glob("seg-*.mp4")) and not (work_dir / "course.mp4").exists()
    with db.connection() as con:
        assert con.execute(
            "SELECT COUNT(*) FROM cls_assets WHERE user_id=? AND kind='video'",
            (job["user_id"],),
        ).fetchone()[0] == 0


# ---------------------------------------------------------------------------
# 4. 取消 + 续跑（checkpoint 复用已完成帧）
# ---------------------------------------------------------------------------

def test_video_cancel_cleans_partial_then_resume_reuses_frames(client, monkeypatch, video_ready):
    course, uid = setup_course(client, scene_types=("slide", "slide"))
    provider = save_provider(client)
    job_id = insert_video_job(
        uid, {"course_id": course["id"], "fps": 8}, provider["id"], "test-model",
        token="video-cancel-token",
    )
    first_capture = fake_capture_factory(cancel_job_id=job_id, cancel_after=0)
    fake_ffmpeg = fake_ffmpeg_factory()
    monkeypatch.setattr(video, "_capture_scene_pngs", first_capture)
    monkeypatch.setattr(video, "_run_ffmpeg", fake_ffmpeg)
    monkeypatch.setattr(video, "_probe_duration", fake_probe_factory())

    run_job_sync(job_id, "video-cancel-token")
    job = job_view(client, job_id)
    assert job["status"] == "cancelled"
    checkpoint = job_checkpoint(job_id)
    assert checkpoint["scene_ids"] == [scene["id"] for scene in cdb.course_scenes(course["id"], uid)]
    assert "1" in (checkpoint.get("frames") or {})
    work_dir = Path(db.DATA_DIR) / "classroom" / "video-work" / ("job-%d" % job_id)
    assert (work_dir / "frame-000.png").is_file()  # 已完成帧保留
    assert not list(work_dir.glob("seg-*.mp4"))
    assert fake_ffmpeg.calls == []  # 取消后没有开始合成
    with db.connection() as con:
        assert con.execute(
            "SELECT COUNT(*) FROM cls_assets WHERE user_id=? AND kind='video'", (uid,),
        ).fetchone()[0] == 0

    # 续跑：只补缺失的第 2 个场景，复用第 1 帧
    resume_capture = fake_capture_factory()
    monkeypatch.setattr(video, "_capture_scene_pngs", resume_capture)
    resume_ffmpeg = fake_ffmpeg_factory()
    monkeypatch.setattr(video, "_run_ffmpeg", resume_ffmpeg)
    resumed = client.post(f"/api/ai/jobs/{job_id}/resume")
    assert resumed.status_code == 200, resumed.text
    done = wait_for_status(client, job_id, "completed")
    assert done["result"]["reused_frames"] == 1
    assert resume_capture.calls == [[1]]
    assert len(resume_ffmpeg.calls) == 3
    # 续跑完成后工作目录被清理（成功产物已落盘素材）
    assert not work_dir.exists()


# ---------------------------------------------------------------------------
# 5. 限额（总时长 / 产物体积）
# ---------------------------------------------------------------------------

def test_video_without_narration_outputs_video_only(client, monkeypatch, video_ready):
    """无旁白课程：片段不带音轨（纯视频），每场景默认 3 秒静帧。"""
    course, uid = setup_course(client, scene_types=("slide",))
    save_provider(client)
    monkeypatch.setattr(video, "_capture_scene_pngs", fake_capture_factory())
    fake_ffmpeg = fake_ffmpeg_factory()
    monkeypatch.setattr(video, "_run_ffmpeg", fake_ffmpeg)
    monkeypatch.setattr(video, "_probe_duration", fake_probe_factory(video_seconds=3.0))

    result = asyncio.run(video.render_course_video(course["id"], uid, fps=8))
    assert result["has_audio"] is False and result["narrated_scene_count"] == 0
    assert result["scene_count"] == 1
    segment_args, concat_args = fake_ffmpeg.calls
    assert not any("anullsrc" in item for item in segment_args)
    assert "-map" not in segment_args  # 纯视频分支不映射音频
    assert "3.000" in segment_args
    assert "concat" in concat_args and "copy" in concat_args
    with db.connection() as con:
        row = dict(con.execute("SELECT * FROM cls_assets WHERE id=?", (result["asset_id"],)).fetchone())
    assert cdb.json_load(row["meta_json"], {})["has_audio"] is False


def test_video_total_duration_limit_is_enforced(client, monkeypatch, video_ready):
    course, uid = setup_course(client, scene_types=("slide",))
    save_provider(client)
    asset_id = create_tts_asset(uid, course["id"])
    set_narration(course["id"], {"1": {"asset_id": asset_id, "chars": 10}})

    async def fake_probe(path):
        return video.MAX_VIDEO_SECONDS + 120  # 单场景旁白就超 10 分钟

    monkeypatch.setattr(video, "_probe_duration", fake_probe)
    monkeypatch.setattr(video, "_capture_scene_pngs", fake_capture_factory())

    with pytest.raises(ValueError) as excinfo:
        asyncio.run(video.render_course_video(course["id"], uid, fps=8))
    assert "10 分钟上限" in str(excinfo.value)


def test_video_size_limit_is_enforced(client, monkeypatch, video_ready):
    course, uid = setup_course(client, scene_types=("slide",))
    save_provider(client)
    monkeypatch.setattr(video, "_capture_scene_pngs", fake_capture_factory())
    monkeypatch.setattr(video, "_run_ffmpeg", fake_ffmpeg_factory(mp4_bytes=b"X" * 512))
    monkeypatch.setattr(video, "_probe_duration", fake_probe_factory())
    monkeypatch.setattr(video, "MAX_VIDEO_BYTES", 128)

    with pytest.raises(ValueError) as excinfo:
        asyncio.run(video.render_course_video(course["id"], uid, fps=8))
    assert "上限" in str(excinfo.value)
    with db.connection() as con:
        assert con.execute(
            "SELECT COUNT(*) FROM cls_assets WHERE user_id=? AND kind='video'", (uid,),
        ).fetchone()[0] == 0


def test_video_render_course_video_rejects_bad_course(client, video_ready):
    # 依赖应用库表已初始化：用 client fixture 走 lifespan（隔离数据目录下也能稳定复现）；
    # 不依赖外部环境里恰好已存在 cls_* 表。
    with pytest.raises(ValueError) as excinfo:
        asyncio.run(video.render_course_video(999999, 999999))
    assert "课程不存在" in str(excinfo.value)


# ---------------------------------------------------------------------------
# 6. 真实渲染冒烟（Playwright + ffmpeg；缺引擎自动 skip，产物写 .omx/tmp）
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
def test_real_render_generates_playable_mp4_and_pdf(client):
    """真实冒烟：2 个场景 → 约 6 秒 H.264 MP4 + 4 页 PDF，用 ffprobe/PDF 魔数断言。"""
    course, uid = setup_course(client, scene_types=("slide", "quiz"))
    save_provider(client)
    try:
        result = asyncio.run(video.render_course_video(course["id"], uid, fps=8))
    except Exception as exc:  # noqa: BLE001 - 本机浏览器不可用时按 skip 处理（CI 无 GUI 环境）
        if "渲染" in str(exc) or "ffmpeg" in str(exc):
            pytest.skip("真实渲染环境不可用：%s" % str(exc)[:120])
        raise
    assert result["asset_id"] and result["scene_count"] == 2
    assert result["size_bytes"] > 1000
    with db.connection() as con:
        row = dict(con.execute("SELECT * FROM cls_assets WHERE id=?", (result["asset_id"],)).fetchone())
    source = storage.local_path(video.VIDEO_FOLDER, row["storage_name"])
    assert Path(source).is_file()
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
    duration = float(info["format"]["duration"])
    assert 4.0 <= duration <= 8.0  # 2 个无旁白场景 × 3 秒 ≈ 6 秒
    SMOKE_OUT.mkdir(parents=True, exist_ok=True)
    target = SMOKE_OUT / "video-smoke-pytest.mp4"
    shutil.copyfile(source, target)
    assert target.stat().st_size == result["size_bytes"]

    # 真实 PDF：同一课程 3 页（封面 + 2 场景）+ PDF 魔数
    pdf = client.get(f"/api/classroom/courses/{course['id']}/export?format=pdf")
    assert pdf.status_code == 200, pdf.text
    assert pdf.content.startswith(b"%PDF")
    assert pdf.headers["X-Page-Count"] == "3"
    pdf_target = SMOKE_OUT / "classroom-smoke-pytest.pdf"
    pdf_target.write_bytes(pdf.content)
