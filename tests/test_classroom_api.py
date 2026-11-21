"""AI 课堂 API 验收：鉴权 / 归属 / 上限 / CRUD / 会话进度 / 测验判分 / TTS 分层 / 素材与交互 HTML。

不联网：TTS 合成与 tutor 模型调用全部用 monkeypatch 假实现。
"""
import asyncio
import json

from fastapi.testclient import TestClient
import pytest

from app import db
from app.classroom import db as cdb
from app.classroom import schema


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------

def save_provider(client, name="课堂测试连接", models=("test-model",)):
    response = client.post("/api/providers", json={
        "name": name, "kind": "openai", "base_url": "https://api.openai.com/v1",
        "api_key": "sk-classroom-api-unit-test", "models": list(models),
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


def insert_scene(course_id, user_id, scene_type, order_index, title, content,
                 *, meta=None, status="ready"):
    now = db.utcnow()
    with db.connection() as con:
        return con.execute(
            "INSERT INTO cls_scenes(course_id,user_id,scene_type,order_index,title,content_json,meta_json,status,"
            "created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (course_id, user_id, scene_type, order_index, title, cdb.json_dump(content),
             cdb.json_dump(meta or {}), status, now, now),
        ).lastrowid


QUIZ_RAW = {"questions": [
    {"id": "q1", "type": "single", "question": "光合作用发生的场所是？",
     "options": [{"label": "叶绿体", "value": "A"}, {"label": "线粒体", "value": "B"}],
     "answer": ["A"], "analysis": "叶绿体是场所。", "points": 10},
    {"id": "q2", "type": "multiple", "question": "光合作用的原料包括？（多选）",
     "options": [{"label": "水", "value": "A"}, {"label": "氧气", "value": "B"}, {"label": "二氧化碳", "value": "C"}],
     "answer": ["A", "C"], "analysis": "水和二氧化碳是原料。", "points": 10},
    {"id": "q3", "type": "short_answer", "question": "简述光合作用的意义。",
     "commentPrompt": "评分标准：要点完整", "analysis": "参考要点。", "points": 20},
]}

SLIDE_CONTENT = {
    "type": "slide", "schemaVersion": 1,
    "canvas": {"width": 1000, "height": 562.5, "theme": {},
               "elements": [{"id": "text_001", "type": "text", "left": 60, "top": 50,
                             "width": 880, "height": 76, "content": "<p>光合作用</p>"}]},
}

INTERACTIVE_CONTENT = {
    "type": "interactive", "widget": "simulation", "height": 480,
    "html": "<!DOCTYPE html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\"></head>"
            "<body><div id=\"experiment\">模拟占位</div></body></html>",
}


# ---------------------------------------------------------------------------
# 1. 鉴权与归属
# ---------------------------------------------------------------------------

def test_endpoints_require_login(client):
    from app.main import app

    anon = TestClient(app)
    endpoints = [
        ("get", "/api/classroom/meta"),
        ("get", "/api/classroom/courses"),
        ("post", "/api/classroom/courses"),
        ("get", "/api/classroom/sessions/1"),
        ("get", "/api/classroom/assets/1"),
    ]
    for method, path in endpoints:
        kwargs = {"json": {"topic": "测试主题"}} if method == "post" else {}
        response = getattr(anon, method)(path, **kwargs)
        assert response.status_code == 401, (method, path, response.text)


def test_foreign_course_and_session_are_404(client, second_client):
    provider = save_provider(client)
    course = create_course(client)
    detail = client.get(f"/api/classroom/courses/{course['id']}").json()
    _ = detail
    uid = user_id_of(client)
    scene_id = insert_scene(course["id"], uid, "slide", 1, "他人场景", SLIDE_CONTENT)
    session = client.post(f"/api/classroom/courses/{course['id']}/sessions").json()["session"]

    assert second_client.get(f"/api/classroom/courses/{course['id']}").status_code == 404
    assert second_client.patch(f"/api/classroom/courses/{course['id']}", json={"title": "x"}).status_code == 404
    assert second_client.delete(f"/api/classroom/courses/{course['id']}").status_code == 404
    assert second_client.get(f"/api/classroom/sessions/{session['id']}").status_code == 404
    assert second_client.patch(f"/api/classroom/sessions/{session['id']}", json={"progress": {}}).status_code == 404
    assert second_client.get(f"/api/classroom/scenes/{scene_id}/html").status_code == 404
    # 他人的连接也不能给自己的课程发起生成（课程不属于他 → 404）
    other_provider = save_provider(second_client, name="他人连接")
    response = second_client.post(f"/api/classroom/courses/{course['id']}/generate", json={
        "stage": "outline", "provider_id": other_provider["id"], "model": "test-model",
    })
    assert response.status_code == 404
    _ = provider


# ---------------------------------------------------------------------------
# 2. 元信息 / 课程 CRUD
# ---------------------------------------------------------------------------

def test_meta_shape(client):
    body = client.get("/api/classroom/meta").json()
    assert body["enabled"] is True
    assert set(body["scene_types"]) == {"slide", "quiz", "interactive", "pbl"}
    assert body["scene_type_labels"]["slide"] == "幻灯片"
    assert body["modes"] == ["standard", "interactive"]
    assert body["limits"]["scene_count_min"] == 3
    assert body["limits"]["scene_count_max"] == 12
    assert "provider" in body["tts"]
    assert body["prompt_version"].startswith("classroom-")


def test_course_crud_lifecycle(client):
    # 主题太短 / 模式非法 → 422
    assert client.post("/api/classroom/courses", json={"topic": "课"}).status_code == 422
    assert client.post("/api/classroom/courses", json={"topic": "光合作用", "mode": "magic"}).status_code == 422

    course = create_course(client, title="光合作用入门", requirements="重点是原理")
    assert course["status"] == "draft"
    assert course["title"] == "光合作用入门"
    assert course["mode"] == "standard"

    listing = client.get("/api/classroom/courses").json()
    assert listing["total"] == 1
    assert listing["items"][0]["id"] == course["id"]
    assert listing["items"][0]["scene_count"] == 0

    detail = client.get(f"/api/classroom/courses/{course['id']}").json()
    assert detail["course"]["outline"] == {}
    assert detail["scenes"] == []

    patched = client.patch(f"/api/classroom/courses/{course['id']}", json={
        "title": "光合作用 · 入门", "description": "适合初中生物的入门课", "grade": "初中二年级",
    })
    assert patched.status_code == 200
    assert patched.json()["course"]["title"] == "光合作用 · 入门"
    assert patched.json()["course"]["description"] == "适合初中生物的入门课"

    assert client.delete(f"/api/classroom/courses/{course['id']}").status_code == 200
    assert client.get(f"/api/classroom/courses/{course['id']}").status_code == 404
    assert client.get("/api/classroom/courses").json()["total"] == 0


def test_course_document_ids_must_be_owned(client, second_client):
    uploaded = client.post("/api/documents", files={
        "file": ("资料.md", "光合作用资料正文".encode(), "text/markdown"),
    })
    assert uploaded.status_code == 200, uploaded.text
    doc_id = uploaded.json()["id"]
    course = create_course(client, document_ids=[doc_id])
    assert course["config"]["document_ids"] == [doc_id]

    foreign_doc = second_client.post("/api/documents", files={
        "file": ("他人资料.md", "他人资料".encode(), "text/markdown"),
    }).json()["id"]
    response = client.post("/api/classroom/courses", json={
        "topic": "光合作用", "document_ids": [foreign_doc],
    })
    assert response.status_code == 404


def test_delete_course_with_running_job_is_409(client):
    course = create_course(client)
    uid = user_id_of(client)
    now = db.utcnow()
    with db.connection() as con:
        con.execute(
            "INSERT INTO ai_jobs(user_id,task,status,stage,request_json,partial_content,cancelled,"
            "created_at,updated_at) VALUES (?,?,?,?,?,'',0,?,?)",
            (uid, "course", "running", "running",
             json.dumps({"task": "course", "provider_id": 1, "model": "m",
                         "params": {"course_id": course["id"]}}, ensure_ascii=False), now, now),
        )
    response = client.delete(f"/api/classroom/courses/{course['id']}")
    assert response.status_code == 409


# ---------------------------------------------------------------------------
# 3. 生成准入
# ---------------------------------------------------------------------------

def test_generate_without_provider_is_422(client):
    course = create_course(client)
    for payload in (
        {"stage": "all", "provider_id": 1, "model": "test-model"},
        {"stage": "outline", "provider_id": 1, "model": "test-model"},
    ):
        response = client.post(f"/api/classroom/courses/{course['id']}/generate", json=payload)
        assert response.status_code == 422
        assert "模型设置" in response.json()["detail"]


def test_generate_with_model_not_in_provider_is_422(client):
    course = create_course(client)
    provider = save_provider(client)
    response = client.post(f"/api/classroom/courses/{course['id']}/generate", json={
        "stage": "all", "provider_id": provider["id"], "model": "not-in-connection",
    })
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# 4. 会话与进度
# ---------------------------------------------------------------------------

def test_session_create_get_patch_progress(client):
    course = create_course(client)
    uid = user_id_of(client)
    first = insert_scene(course["id"], uid, "slide", 1, "第一页", SLIDE_CONTENT)
    second = insert_scene(course["id"], uid, "quiz", 2, "小测",
                          schema.normalize_quiz_content(QUIZ_RAW)[0])

    created = client.post(f"/api/classroom/courses/{course['id']}/sessions")
    assert created.status_code == 200
    session = created.json()["session"]
    assert session["course_id"] == course["id"]
    assert session["current_scene_id"] in (None, first)  # 没有场景时为空；有场景时默认第一页

    # 显式从第一页开始
    session = client.post(f"/api/classroom/courses/{course['id']}/sessions",
                          json={"scene_id": first}).json()["session"]
    assert session["current_scene_id"] == first

    progress = {"scene_index": 1, "action_index": 3, "consumed": ["discussion"]}
    patched = client.patch(f"/api/classroom/sessions/{session['id']}", json={
        "scene_id": second, "progress": progress,
    })
    assert patched.status_code == 200
    assert patched.json()["session"]["progress"] == progress

    detail = client.get(f"/api/classroom/sessions/{session['id']}").json()
    assert detail["session"]["current_scene_id"] == second
    assert detail["progress"] == progress
    assert [scene["id"] for scene in detail["scenes"]] == [first, second]
    assert detail["course"]["id"] == course["id"]

    # 进度数据过大 / 场景不属于本课程 → 422 / 404
    assert client.patch(f"/api/classroom/sessions/{session['id']}", json={
        "progress": {"blob": "x" * (20 * 1024)},
    }).status_code == 422
    assert client.patch(f"/api/classroom/sessions/{session['id']}", json={"scene_id": 9999}).status_code == 404


# ---------------------------------------------------------------------------
# 5. 测验判分
# ---------------------------------------------------------------------------

def test_quiz_grading_objective_and_short_answer(client):
    course = create_course(client)
    uid = user_id_of(client)
    quiz_content, _ = schema.normalize_quiz_content(QUIZ_RAW)
    quiz_id = insert_scene(course["id"], uid, "quiz", 1, "课堂小测", quiz_content,
                           meta={"actions": [{"id": "act_001", "type": "speech", "text": "先做题"}]})
    slide_id = insert_scene(course["id"], uid, "slide", 2, "讲解", SLIDE_CONTENT)
    session = client.post(f"/api/classroom/courses/{course['id']}/sessions").json()["session"]

    response = client.post(f"/api/classroom/sessions/{session['id']}/quiz", json={
        "scene_id": quiz_id,
        "answers": [
            {"question_id": "q1", "values": ["A"]},          # 正确
            {"question_id": "q2", "values": ["A"]},          # 少选 → 错误
            {"question_id": "q3", "values": ["为生物提供有机物和氧气"]},  # 主观题 → needs_review
        ],
    })
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["score"] == 10
    assert body["max_score"] == 40
    assert body["correct_count"] == 1
    assert body["needs_review_count"] == 1
    by_id = {item["question_id"]: item for item in body["detail"]}
    assert by_id["q1"]["correct"] is True and by_id["q1"]["earned"] == 10
    assert by_id["q2"]["correct"] is False and by_id["q2"]["correct_answer"] == ["A", "C"]
    assert by_id["q3"]["needs_review"] is True and by_id["q3"]["earned"] == 0
    assert by_id["q1"]["analysis"]

    with db.connection() as con:
        attempt = con.execute("SELECT * FROM cls_quiz_attempts WHERE id=?", (body["attempt_id"],)).fetchone()
    assert attempt is not None and attempt["score"] == 10

    # 非测验场景 / 未作答 / 不存在的场景 → 422 / 404
    assert client.post(f"/api/classroom/sessions/{session['id']}/quiz", json={
        "scene_id": slide_id, "answers": [{"question_id": "q1", "values": ["A"]}],
    }).status_code == 422
    assert client.post(f"/api/classroom/sessions/{session['id']}/quiz", json={
        "scene_id": quiz_id, "answers": [],
    }).status_code == 422
    assert client.post(f"/api/classroom/sessions/{session['id']}/quiz", json={
        "scene_id": 9999, "answers": [{"question_id": "q1", "values": ["A"]}],
    }).status_code == 404


# ---------------------------------------------------------------------------
# 6. TTS 分层与素材
# ---------------------------------------------------------------------------

def test_tts_without_provider_falls_back_to_browser(client):
    course = create_course(client)
    response = client.post(f"/api/classroom/courses/{course['id']}/tts", json={"text": "大家好，我们开始上课。"})
    assert response.status_code == 200
    body = response.json()
    assert body["fallback"] == "browser"
    assert body["course_id"] == course["id"]


def test_tts_synthesis_saves_asset_and_serves_with_etag(client, second_client, monkeypatch):
    from app.classroom import tts as classroom_tts

    provider = save_provider(client, models=("tts-1",))
    course = create_course(client)
    calls = []

    async def fake_speech(provider_row, model, text, *, voice=None, speed=None):
        calls.append({"model": model, "text": text, "voice": voice, "speed": speed})
        return b"ID3-classroom-fake-mp3-bytes", "audio/mpeg"

    monkeypatch.setattr(classroom_tts, "_request_speech", fake_speech)
    meta = client.get("/api/classroom/meta").json()
    assert meta["tts"]["provider"] == provider["name"]

    response = client.post(f"/api/classroom/courses/{course['id']}/tts", json={
        "text": "光合作用发生在叶绿体。", "voice": "alloy", "speed": 1.1,
    })
    assert response.status_code == 200, response.text
    body = response.json()
    assert "asset" in body and body["asset"]["url"].startswith("/api/classroom/assets/")
    assert body["provider"] == provider["name"]
    assert calls and calls[0]["text"] == "光合作用发生在叶绿体。"

    asset_url = body["asset"]["url"]
    fetched = client.get(asset_url)
    assert fetched.status_code == 200
    assert fetched.headers["content-type"].startswith("audio/mpeg")
    assert "private" in fetched.headers["cache-control"]
    etag = fetched.headers["etag"]
    assert etag
    not_modified = client.get(asset_url, headers={"If-None-Match": etag})
    assert not_modified.status_code == 304
    # 他人不能读自己的素材
    assert second_client.get(asset_url).status_code == 404


def test_tts_failure_falls_back_instead_of_500(client, monkeypatch):
    from app.classroom import tts as classroom_tts

    save_provider(client, models=("tts-1",))
    course = create_course(client)

    async def broken(*args, **kwargs):
        raise ValueError("上游语音服务不可用")

    monkeypatch.setattr(classroom_tts, "_request_speech", broken)
    response = client.post(f"/api/classroom/courses/{course['id']}/tts", json={"text": "测试"})
    assert response.status_code == 200
    body = response.json()
    assert body["fallback"] == "browser"
    assert "语音合成失败" in body["error"]


# ---------------------------------------------------------------------------
# 7. 交互场景 HTML（独立 CSP 沙箱）
# ---------------------------------------------------------------------------

def test_interactive_scene_html_route_sets_csp(client, second_client):
    course = create_course(client)
    uid = user_id_of(client)
    scene_id = insert_scene(course["id"], uid, "interactive", 1, "虚拟实验", INTERACTIVE_CONTENT)
    slide_id = insert_scene(course["id"], uid, "slide", 2, "讲解", SLIDE_CONTENT)

    response = client.get(f"/api/classroom/scenes/{scene_id}/html")
    assert response.status_code == 200
    assert "模拟占位" in response.text
    csp = response.headers["content-security-policy"]
    assert "default-src 'none'" in csp
    assert "connect-src 'none'" in csp
    assert response.headers["x-frame-options"] == "SAMEORIGIN"

    assert client.get(f"/api/classroom/scenes/{slide_id}/html").status_code == 404
    assert second_client.get(f"/api/classroom/scenes/{scene_id}/html").status_code == 404


# ---------------------------------------------------------------------------
# 8. 课堂同步对话（tutor）
# ---------------------------------------------------------------------------

def _install_tutor_model(monkeypatch, payload, *, fail=False):
    calls = []

    async def fake_call(provider, model, prompt, images=None, json_output=False, token_limit=5000, **kwargs):
        calls.append({"prompt": prompt, "model": model})
        if fail:
            raise ValueError("模型服务不可用")
        return {"content": json.dumps(payload, ensure_ascii=False), "model": model,
                "provider_name": "mock", "usage": {"total_tokens": 20}}

    monkeypatch.setattr("app.ai._call_model", fake_call)
    return calls


def test_tutor_sync_reply_actions_validated_and_turns_persisted(client, monkeypatch):
    provider = save_provider(client)
    course = create_course(client)
    uid = user_id_of(client)
    scene_id = insert_scene(course["id"], uid, "slide", 1, "原料与产物", SLIDE_CONTENT,
                            meta={"actions": [], "digest": "元素 text_001：光合作用"})
    session = client.post(f"/api/classroom/courses/{course['id']}/sessions").json()["session"]
    calls = _install_tutor_model(monkeypatch, {
        "reply": {"speaker": "teacher", "text": "叶绿体是光合作用的场所。", "actions": [
            {"type": "spotlight", "element_id": "text_001"},
            {"type": "点歌"},  # 未知动作会被拒绝
        ]},
        "scene_hint": {"index": 0, "reason": "回到本页"},
    })

    response = client.post(f"/api/classroom/sessions/{session['id']}/tutor", json={
        "scene_id": scene_id, "message": "为什么需要光？", "mode": "qa",
    })
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["reply"]["speaker"] == "teacher"
    assert body["reply"]["text"]
    assert [action["type"] for action in body["reply"]["actions"]] == ["spotlight"]
    assert body["scene_hint"]["index"] == 0
    assert calls and "课堂 AI 教师对话" in calls[0]["prompt"]
    assert "为什么需要光" in calls[0]["prompt"]
    _ = provider

    turns = client.get(f"/api/classroom/sessions/{session['id']}").json()["turns"]
    assert [turn["role"] for turn in turns] == ["user", "assistant"]
    assert turns[0]["content"] == "为什么需要光？"
    assert turns[1]["actions"][0]["type"] == "spotlight"


def test_tutor_requires_provider_and_model_failure_is_503(client, monkeypatch):
    course = create_course(client)
    uid = user_id_of(client)
    scene_id = insert_scene(course["id"], uid, "slide", 1, "第一页", SLIDE_CONTENT)
    session = client.post(f"/api/classroom/courses/{course['id']}/sessions").json()["session"]

    # 没有配置连接 → 422
    response = client.post(f"/api/classroom/sessions/{session['id']}/tutor", json={
        "scene_id": scene_id, "message": "你好", "mode": "qa",
    })
    assert response.status_code == 422
    assert "模型设置" in response.json()["detail"]

    save_provider(client)
    _install_tutor_model(monkeypatch, {}, fail=True)
    response = client.post(f"/api/classroom/sessions/{session['id']}/tutor", json={
        "scene_id": scene_id, "message": "你好", "mode": "qa",
    })
    assert response.status_code == 503

    # 未知对话模式 422
    response = client.post(f"/api/classroom/sessions/{session['id']}/tutor", json={
        "scene_id": scene_id, "message": "你好", "mode": "debate",
    })
    assert response.status_code == 422
