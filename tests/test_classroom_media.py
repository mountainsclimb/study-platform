"""P4 课堂媒体验收：图片编辑（MiniMax subject_reference / OpenAI edits 回退 / 参考图压缩）、
音色列表（MiniMax /get_voice 缓存 + 静态回退 / 小米 9 音色 / OpenAI 默认）、批量旁白任务
``course-narration``（SSE 阶段、取消保留、checkpoint 续跑、幂等、配置合并、归属校验）。

不联网：图片生成与语音合成都用 monkeypatch 假实现；MiniMax ``/get_voice`` 用假 httpx 客户端。
"""
import asyncio
import hashlib
import io
import json
import time

from fastapi import HTTPException
from fastapi.testclient import TestClient
import pytest

from app import db, storage
from app.classroom import api as classroom_api
from app.classroom import db as cdb
from app.classroom import images as cls_images
from app.classroom import narration as cls_narration
from app.classroom import tts as cls_tts


# ---------------------------------------------------------------------------
# 辅助：假 httpx 客户端（覆盖 MiniMax /get_voice 与 /image_generation、OpenAI /images/edits）
# ---------------------------------------------------------------------------

class _FakeResponse:
    def __init__(self, *, status_code=200, payload=None, content=b"", headers=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.content = content
        self.headers = {"content-type": "application/json", **(headers or {})}
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class FakeHTTP:
    """按顺序返回预置响应并记录请求；支持 post 的 json/data/files 三种形态。"""

    def __init__(self):
        self.calls: list[dict] = []
        self.responses: list[_FakeResponse] = []

    def queue(self, response):
        self.responses.append(response)
        return self

    def install(self, monkeypatch):
        calls, responses = self.calls, self.responses

        class _Client:
            def __init__(self, **_kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_exc):
                return False

            async def post(self, url, headers=None, json=None, data=None, files=None):
                calls.append({"url": url, "headers": headers, "json": json,
                              "data": data, "files": files})
                if not responses:
                    raise AssertionError("没有预置的假 HTTP 响应：%s" % url)
                return responses.pop(0)

            async def get(self, url, **_kwargs):
                calls.append({"url": url, "method": "GET"})
                if not responses:
                    raise AssertionError("没有预置的假 HTTP 响应：%s" % url)
                return responses.pop(0)

        from app.ai import httpx as ai_httpx

        monkeypatch.setattr(ai_httpx, "AsyncClient", _Client)
        monkeypatch.setattr("app.ai.providers._validate_url", lambda value, resolve=False: value)
        monkeypatch.setattr("app.ai.netguard.client_kwargs",
                            lambda endpoint, timeout_seconds=60: {})
        return self


@pytest.fixture
def fake_http(monkeypatch):
    helper = FakeHTTP().install(monkeypatch)
    yield helper


# ---------------------------------------------------------------------------
# 辅助：账号 / 连接 / 课程 / 场景 / 任务
# ---------------------------------------------------------------------------

def save_provider(client, *, name="媒体测试连接", models=("test-model",),
                  kind="openai", base_url="https://api.openai.com/v1", api_key="sk-media-unit-test"):
    response = client.post("/api/providers", json={
        "name": name, "kind": kind, "base_url": base_url,
        "api_key": api_key, "models": list(models),
    })
    assert response.status_code == 200, response.text
    return response.json()


def save_tts_provider(client, *, name="测试语音连接", model="tts-1",
                      base_url="https://api.openai.com/v1"):
    return save_provider(client, name=name, models=(model,), base_url=base_url)


def save_mimo_provider(client, *, name="小米语音"):
    return save_provider(client, name=name, models=("mimo-v2.5-tts",),
                         base_url="https://api.xiaomimimo.com/v1")


def save_minimax_provider(client, *, name="MiniMax 语音"):
    return save_provider(client, name=name, models=("speech-2.8-turbo",),
                         base_url="https://api.minimaxi.com/v1")


def save_image_provider(client, *, name="测试图片连接", model="gpt-image-1"):
    return save_provider(client, name=name, models=(model,))


def create_course(client, **overrides):
    payload = {"topic": "光合作用", "subject": "生物", "grade": "初中二年级"}
    payload.update(overrides)
    response = client.post("/api/classroom/courses", json=payload)
    assert response.status_code == 200, response.text
    return response.json()["course"]


def user_id_of(client):
    return client.get("/api/auth/me").json()["id"]


def insert_scene(course_id, user_id, scene_type, order_index, title, content,
                 *, actions=None, status="ready"):
    now = db.utcnow()
    with db.connection() as con:
        return con.execute(
            "INSERT INTO cls_scenes(course_id,user_id,scene_type,order_index,title,content_json,meta_json,status,"
            "created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (course_id, user_id, scene_type, order_index, title,
             cdb.json_dump(content), cdb.json_dump({"actions": actions or []}), status, now, now),
        ).lastrowid


SLIDE_CONTENT = {
    "type": "slide", "schemaVersion": 1,
    "canvas": {"width": 1000, "height": 562.5, "theme": {},
               "elements": [{"id": "text_001", "type": "text", "left": 60, "top": 50,
                             "width": 880, "height": 76, "content": "<p>光合作用</p>"}]},
}


def speech_action(text, action_id="act_001"):
    return {"id": action_id, "type": "speech", "text": text}


def make_speech_course(client, texts):
    """建课程并为每个文本插一个含 speech 动作的 slide 场景，返回 (course, [scene_ids])。"""
    course = create_course(client)
    uid = user_id_of(client)
    scene_ids = []
    for index, text in enumerate(texts, 1):
        scene_ids.append(insert_scene(
            course["id"], uid, "slide", index, "第 %d 页" % index, SLIDE_CONTENT,
            actions=[speech_action("第 %d 段讲解：%s" % (index, text))],
        ))
    return course, scene_ids


def job_view(client, job_id):
    response = client.get(f"/api/ai/jobs/{job_id}")
    assert response.status_code == 200, response.text
    return response.json()


def wait_for_status(client, job_id, status, *, tries=300, delay=0.03):
    job = None
    for _ in range(tries):
        job = job_view(client, job_id)
        if job["status"] == status:
            return job
        if job["status"] in ("failed", "cancelled") and status not in (job["status"],):
            pytest.fail("任务提前进入 %s（期望 %s）：%s" % (job["status"], status, job.get("error")))
        time.sleep(delay)
    pytest.fail("任务未进入 %s，当前 %s" % (status, job))


def insert_narration_job(user_id, params, provider_id, model, *, token="narration-token",
                         status="queued", checkpoint=None):
    now = db.utcnow()
    request = {"task": "course-narration", "provider_id": provider_id, "model": model,
               "params": params}
    with db.connection() as con:
        return con.execute(
            "INSERT INTO ai_jobs(user_id,task,status,stage,request_json,partial_content,cancelled,"
            "created_at,updated_at,dispatch_token,checkpoint_json) VALUES (?,?,?,?,?,'',0,?,?,?,?)",
            (user_id, "course-narration", status, status,
             json.dumps(request, ensure_ascii=False), now, now, token,
             json.dumps(checkpoint, ensure_ascii=False) if checkpoint is not None else None),
        ).lastrowid


def run_narration_job_direct(job_id, token):
    from app.ai import jobs as ai_jobs

    asyncio.run(ai_jobs.execute_dispatched_job(job_id, token, ai_jobs._run_job))


def course_config(course_id, user_id):
    with db.connection() as con:
        row = con.execute(
            "SELECT config_json FROM cls_courses WHERE id=? AND user_id=?", (course_id, user_id),
        ).fetchone()
    return cdb.json_load(row["config_json"], {}) if row else {}


def narration_assets(user_id):
    with db.connection() as con:
        rows = con.execute(
            "SELECT * FROM cls_assets WHERE user_id=? AND kind='tts' ORDER BY id", (user_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def _png_bytes(width=16, height=16, color="white"):
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buffer, format="PNG")
    return buffer.getvalue()


def _jpeg_bytes():
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (8, 8), "white").save(buffer, format="JPEG")
    return buffer.getvalue()


def _provider_row(base_url="https://api.minimaxi.com/v1", name="MiniMax"):
    from app.ai.providers import _encrypt_key

    encrypted, hint = _encrypt_key("sk-api-test-key")
    return {
        "id": 2, "name": name, "kind": "openai", "base_url": base_url,
        "encrypted_key": encrypted, "key_hint": hint, "models_json": "[]",
        "auth_mode": "auto", "extra_headers_enc": "", "compat_json": "{}",
    }


def _minimax_image_payload(png):
    import base64

    return {
        "id": "x", "data": {"image_base64": [base64.b64encode(png).decode()]},
        "base_resp": {"status_code": 0, "status_msg": "success"},
    }


def _openai_image_payload(png):
    import base64

    return {"data": [{"b64_json": base64.b64encode(png).decode()}]}


# ---------------------------------------------------------------------------
# 1. 参考图压缩（Pillow：最长边 1600 / JPEG 85 / ≤8MB）
# ---------------------------------------------------------------------------

def test_compress_reference_image_resizes_and_converts_to_jpeg():
    from PIL import Image

    compressed, mime = cls_images.compress_reference_image(_png_bytes(2400, 1200))
    assert mime == "image/jpeg"
    assert compressed.startswith(b"\xff\xd8\xff")
    with Image.open(io.BytesIO(compressed)) as image:
        assert image.format == "JPEG"
        assert max(image.size) <= cls_images.REFERENCE_MAX_SIDE
        # 等比缩放：2400x1200 → 1600x800
        assert image.size == (1600, 800)


def test_compress_reference_image_keeps_small_images_and_flattens_alpha():
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGBA", (120, 60), (0, 0, 0, 0)).save(buffer, format="PNG")
    compressed, mime = cls_images.compress_reference_image(buffer.getvalue())
    assert mime == "image/jpeg"
    with Image.open(io.BytesIO(compressed)) as image:
        assert image.size == (120, 60)
        assert image.mode == "RGB"
        assert image.getpixel((0, 0)) == (255, 255, 255)


def test_compress_reference_image_rejects_invalid_and_oversized():
    with pytest.raises(ValueError) as exc:
        cls_images.compress_reference_image(b"this-is-not-an-image")
    assert "参考图无法识别" in str(exc.value)

    with pytest.raises(ValueError) as exc:
        cls_images.compress_reference_image(b"x" * (cls_images.MAX_REFERENCE_BYTES + 1))
    assert "参考图不能超过" in str(exc.value)


def test_generate_scene_image_with_reference_compresses_before_request(client, monkeypatch):
    course = create_course(client)
    uid = user_id_of(client)
    captured = {}

    async def fake_request(provider, model, prompt, *, size=cls_images.DEFAULT_SIZE,
                           reference=None, reference_mime="image/jpeg", warnings=None):
        captured.update({"reference": reference, "mime": reference_mime, "warnings": warnings})
        return _png_bytes(), "image/png"

    monkeypatch.setattr(cls_images, "_request_image", fake_request)
    payload = asyncio.run(cls_images.generate_scene_image(
        user_id=uid, course_id=course["id"], candidate={"provider": _provider_row(), "model": "image-01"},
        prompt="水彩风格", reference=_png_bytes(2400, 1200),
    ))
    assert captured["mime"] == "image/jpeg"
    assert captured["reference"].startswith(b"\xff\xd8\xff")
    assert payload["edit"] is True
    assert payload["warnings"] == []


# ---------------------------------------------------------------------------
# 2. 图片生成协议：MiniMax subject_reference / OpenAI edits（失败回退文生图）
# ---------------------------------------------------------------------------

def test_minimax_image_edit_sends_data_uri_subject_reference(fake_http):
    png = _png_bytes()
    fake_http.queue(_FakeResponse(payload=_minimax_image_payload(png)))
    reference = b"\xff\xd8\xffreference-bytes"
    data, mime = asyncio.run(cls_images._request_image(
        _provider_row(), "image-01", "把角色改成水彩风格",
        reference=reference, reference_mime="image/jpeg",
    ))
    assert data == png and mime == "image/png"
    call = fake_http.calls[0]
    assert call["url"].endswith("/image_generation")
    body = call["json"]
    assert body["model"] == "image-01"
    subject = body["subject_reference"]
    assert subject[0]["type"] == "character"
    assert subject[0]["image_file"].startswith("data:image/jpeg;base64,")
    # 纯 base64 会被上游当 URL 拒绝：必须带 data URI 前缀
    import base64

    assert subject[0]["image_file"].split(",", 1)[1] == base64.b64encode(reference).decode()


def test_openai_image_edit_success(fake_http):
    png = _png_bytes()
    fake_http.queue(_FakeResponse(payload=_openai_image_payload(png)))
    data, mime = asyncio.run(cls_images._request_image(
        _provider_row(base_url="https://api.openai.com/v1", name="OpenAI 图片"),
        "gpt-image-1", "改成油画风格", reference=_jpeg_bytes(),
    ))
    assert data == png and mime == "image/png"
    call = fake_http.calls[0]
    assert call["url"].endswith("/images/edits")
    assert call["data"]["model"] == "gpt-image-1"
    assert call["data"]["prompt"] == "改成油画风格"
    assert "image" in call["files"]


def test_openai_image_edit_failure_falls_back_to_generation_with_warning(fake_http):
    png = _png_bytes()
    fake_http.queue(_FakeResponse(status_code=400, text="unknown parameter: response_format"))
    fake_http.queue(_FakeResponse(payload=_openai_image_payload(png)))
    warnings: list[str] = []
    data, mime = asyncio.run(cls_images._request_image(
        _provider_row(base_url="https://api.openai.com/v1", name="OpenAI 图片"),
        "gpt-image-1", "改成油画风格", reference=_jpeg_bytes(), warnings=warnings,
    ))
    assert data == png and mime == "image/png"
    assert fake_http.calls[0]["url"].endswith("/images/edits")
    assert fake_http.calls[1]["url"].endswith("/images/generations")
    assert warnings and "回退为文生图" in warnings[0]


# ---------------------------------------------------------------------------
# 3. 图片 API：无连接 422 / 归属 / 参考图 422 / 成功落素材
# ---------------------------------------------------------------------------

def test_image_api_without_provider_is_422(client):
    course = create_course(client)
    response = client.post(f"/api/classroom/courses/{course['id']}/images",
                           data={"prompt": "画一张光合作用示意图"})
    assert response.status_code == 422
    assert "图片生成连接" in response.json()["detail"]


def test_image_api_requires_prompt(client):
    save_image_provider(client)
    course = create_course(client)
    response = client.post(f"/api/classroom/courses/{course['id']}/images", data={})
    assert response.status_code == 422


def test_image_api_rejects_invalid_reference_as_422(client):
    save_image_provider(client)
    course = create_course(client)
    response = client.post(
        f"/api/classroom/courses/{course['id']}/images",
        data={"prompt": "改成水彩风格"},
        files={"reference": ("bad.png", b"not-an-image", "image/png")},
    )
    assert response.status_code == 422
    assert "参考图无法识别" in response.json()["detail"]


def test_image_api_generates_asset_with_reference_and_records_config(client, second_client, fake_http):
    save_image_provider(client)
    course = create_course(client)
    uid = user_id_of(client)
    scene_id = insert_scene(course["id"], uid, "slide", 1, "第一页", SLIDE_CONTENT)
    png = _png_bytes()
    fake_http.queue(_FakeResponse(payload=_openai_image_payload(png)))

    response = client.post(
        f"/api/classroom/courses/{course['id']}/images",
        data={"prompt": "把讲台换成水彩风格", "scene_id": str(scene_id)},
        files={"reference": ("ref.png", _png_bytes(2400, 1200), "image/png")},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["edit"] is True
    assert body["scene_id"] == scene_id
    assert body["asset"]["url"] == "/api/classroom/assets/%d" % body["asset"]["id"]
    assert body["model"] == "gpt-image-1"

    fetched = client.get(body["asset"]["url"])
    assert fetched.status_code == 200
    assert fetched.headers["content-type"].startswith("image/png")
    assert second_client.get(body["asset"]["url"]).status_code == 404

    config = course_config(course["id"], uid)
    remembered = config.get("generated_images") or []
    assert remembered and remembered[0]["asset_id"] == body["asset"]["id"]
    assert remembered[0]["edit"] is True
    # 素材 meta 记录编辑标记，便于导出/追溯
    with db.connection() as con:
        row = con.execute("SELECT meta_json FROM cls_assets WHERE id=?",
                          (body["asset"]["id"],)).fetchone()
    meta = cdb.json_load(row["meta_json"], {})
    assert meta["edit"] is True and meta["scene_id"] == scene_id


def test_image_api_foreign_course_and_scene_are_404(client, second_client):
    save_image_provider(client)
    course = create_course(client)
    uid = user_id_of(client)
    scene_id = insert_scene(course["id"], uid, "slide", 1, "第一页", SLIDE_CONTENT)
    other_course = create_course(second_client, topic="他人课程")

    assert second_client.post(
        f"/api/classroom/courses/{course['id']}/images", data={"prompt": "画图"},
    ).status_code == 404
    # 场景不属于该课程 → 404（即使属于本人）
    response = client.post(
        f"/api/classroom/courses/{other_course['id']}/images",
        data={"prompt": "画图", "scene_id": str(scene_id)},
    )
    assert response.status_code == 404


def test_meta_images_exposes_reference_capability(client):
    meta = client.get("/api/classroom/meta").json()
    assert meta["images"]["enabled"] is False
    assert meta["images"]["supports_reference"] is True
    assert meta["images"]["reference"]["max_side"] == 1600
    assert meta["images"]["reference"]["max_bytes"] == cls_images.MAX_REFERENCE_BYTES

    save_image_provider(client)
    meta = client.get("/api/classroom/meta").json()
    assert meta["images"]["enabled"] is True
    assert meta["images"]["supports_reference"] is True
    assert meta["images"]["model"] == "gpt-image-1"


# ---------------------------------------------------------------------------
# 4. 音色列表：无连接 / MiMo 9 音色 / OpenAI 默认 / MiniMax API + 缓存 + 回退
# ---------------------------------------------------------------------------

def test_voices_without_tts_provider_is_empty_with_guidance(client):
    body = client.get("/api/classroom/voices").json()
    assert body["items"] == []
    assert body["tts"] is None
    assert any("语音合成连接" in item for item in body["warnings"])


def test_voices_mimo_returns_nine_static_voices_and_caches(client):
    cls_tts.clear_voice_cache()
    save_mimo_provider(client)
    first = client.get("/api/classroom/voices").json()
    assert first["total"] == 9
    assert first["cached"] is False
    assert {item["voice_id"] for item in first["items"]} == set(cls_tts.MIMO_VOICE_LABELS)
    assert all(item["source"] == "static" for item in first["items"])
    assert first["items"][0]["provider"] == "小米语音"
    assert first["items"][0]["model"] == "mimo-v2.5-tts"
    assert sum(1 for item in first["items"] if item["default"]) == 1
    assert [item["voice_id"] for item in first["items"] if item["default"]] == ["mimo_default"]

    second = client.get("/api/classroom/voices").json()
    assert second["cached"] is True
    assert second["total"] == 9


def test_voices_openai_compatible_returns_annotated_defaults(client):
    cls_tts.clear_voice_cache()
    save_tts_provider(client)
    body = client.get("/api/classroom/voices").json()
    ids = [item["voice_id"] for item in body["items"]]
    assert ids == list(cls_tts.OPENAI_STATIC_VOICES)
    assert body["providers"][0]["source"] == "static"
    default_items = [item for item in body["items"] if item["default"]]
    assert [item["voice_id"] for item in default_items] == ["alloy"]
    assert all(item["source"] == "static" for item in body["items"])


def test_voices_minimax_fetches_api_and_uses_cache(client, fake_http):
    cls_tts.clear_voice_cache()
    save_minimax_provider(client)
    fake_http.queue(_FakeResponse(payload={
        "voice_list": [
            {"voice_id": "female-shaonv", "voice_name": "少女音色"},
            {"voice_id": "my-cloned-voice", "voice_name": "我的克隆音色", "language": "zh-CN"},
        ],
        "base_resp": {"status_code": 0, "status_msg": "success"},
    }))
    first = client.get("/api/classroom/voices").json()
    assert first["total"] == 2
    assert first["providers"][0]["source"] == "api"
    assert [item["voice_id"] for item in first["items"]] == ["female-shaonv", "my-cloned-voice"]
    assert all(item["source"] == "api" for item in first["items"])
    assert len(fake_http.calls) == 1
    assert fake_http.calls[0]["url"].endswith("/get_voice")
    assert fake_http.calls[0]["json"] == {"voice_type": "all"}

    second = client.get("/api/classroom/voices").json()
    assert second["cached"] is True
    assert second["total"] == 2
    assert len(fake_http.calls) == 1  # 命中 10 分钟缓存，不再请求上游


def test_voices_minimax_failure_falls_back_to_static_list(client, fake_http):
    cls_tts.clear_voice_cache()
    save_minimax_provider(client)
    fake_http.queue(_FakeResponse(status_code=500, text="upstream error"))
    body = client.get("/api/classroom/voices").json()
    assert body["total"] == len(cls_tts.MINIMAX_STATIC_VOICES)
    assert body["providers"][0]["source"] == "static"
    assert all(item["source"] == "static" for item in body["items"])
    assert any("内置音色" in item for item in body["warnings"])
    assert any(item["voice_id"] == "female-shaonv" for item in body["items"])


def test_voice_cache_expires_after_ttl(client):
    cls_tts.clear_voice_cache()
    save_mimo_provider(client)
    assert client.get("/api/classroom/voices").json()["cached"] is False
    assert client.get("/api/classroom/voices").json()["cached"] is True
    for entry in cls_tts._VOICE_CACHE.values():
        entry["expires"] = 0
    refreshed = client.get("/api/classroom/voices").json()
    assert refreshed["cached"] is False
    assert refreshed["total"] == 9


def test_voices_unknown_provider_is_404(client):
    save_mimo_provider(client)
    assert client.get("/api/classroom/voices?provider_id=9999").status_code == 404


def test_tts_uses_course_config_voice(client, monkeypatch):
    provider = save_tts_provider(client)
    course = create_course(client)
    uid = user_id_of(client)
    with db.connection() as con:
        con.execute(
            "UPDATE cls_courses SET config_json=? WHERE id=? AND user_id=?",
            (cdb.json_dump({"voice": "nova"}), course["id"], uid),
        )
    calls = []

    async def fake_speech(provider_row, model, text, *, voice=None, speed=None):
        calls.append({"model": model, "text": text, "voice": voice})
        return b"ID3-config-voice", "audio/mpeg"

    monkeypatch.setattr(cls_tts, "_request_speech", fake_speech)
    response = client.post(f"/api/classroom/courses/{course['id']}/tts", json={"text": "上课啦"})
    assert response.status_code == 200, response.text
    assert calls and calls[0]["voice"] == "nova"
    assert response.json()["voice"] == "nova"
    _ = provider


# ---------------------------------------------------------------------------
# 5. 旁白任务：准入（无 provider / 无 speech / 归属 / 重复任务）
# ---------------------------------------------------------------------------

def test_narration_requires_tts_provider(client):
    save_provider(client, models=("test-model",))  # 只有聊天模型，不是 TTS
    course, _scene_ids = make_speech_course(client, ["光合作用发生在叶绿体。"])
    response = client.post(f"/api/classroom/courses/{course['id']}/narration", json={})
    assert response.status_code == 422
    assert "语音合成连接" in response.json()["detail"]

    # 走通用任务入口同样被准入拦截
    response = client.post("/api/ai/jobs", json={
        "task": "course-narration", "provider_id": 1, "model": "test-model",
        "params": {"course_id": course["id"]},
    })
    assert response.status_code == 422


def test_narration_requires_speech_text(client):
    save_tts_provider(client)
    course = create_course(client)
    uid = user_id_of(client)
    insert_scene(course["id"], uid, "slide", 1, "无旁白", SLIDE_CONTENT, actions=[])
    response = client.post(f"/api/classroom/courses/{course['id']}/narration", json={})
    assert response.status_code == 422
    assert "旁白文本" in response.json()["detail"]


def test_narration_foreign_course_is_404(client, second_client):
    save_tts_provider(client)
    course, _scene_ids = make_speech_course(client, ["第一段。"])
    assert second_client.post(
        f"/api/classroom/courses/{course['id']}/narration", json={},
    ).status_code == 404
    assert second_client.get(
        f"/api/classroom/courses/{course['id']}/narration",
    ).status_code == 404


def test_narration_scene_ids_validation(client):
    save_tts_provider(client)
    course, scene_ids = make_speech_course(client, ["第一段。", "第二段。"])
    response = client.post(f"/api/classroom/courses/{course['id']}/narration",
                           json={"scene_ids": [9999]})
    assert response.status_code == 404
    response = client.post(f"/api/classroom/courses/{course['id']}/narration",
                           json={"scene_ids": []})
    assert response.status_code == 422
    _ = scene_ids


def test_narration_duplicate_running_job_is_409(client):
    provider = save_tts_provider(client)
    course, _scene_ids = make_speech_course(client, ["第一段。"])
    uid = user_id_of(client)
    insert_narration_job(uid, {"course_id": course["id"]}, provider["id"], "tts-1",
                         status="running")
    response = client.post(f"/api/classroom/courses/{course['id']}/narration", json={})
    assert response.status_code == 409
    assert "旁白合成任务" in response.json()["detail"]


# ---------------------------------------------------------------------------
# 6. 旁白任务执行：成功 / SSE 阶段 / GET 查询 / 幂等
# ---------------------------------------------------------------------------

def _install_fake_tts(monkeypatch, *, fail_texts=(), hook=None):
    calls: list[dict] = []

    async def fake_speech(provider_row, model, text, *, voice=None, speed=None):
        calls.append({"model": model, "text": text, "voice": voice})
        if hook:
            hook(len(calls), text)
        for marker in fail_texts:
            if marker in text:
                raise ValueError("模拟语音服务失败")
        digest = hashlib.sha256(("%s|%s" % (voice, text)).encode("utf-8")).digest()[:24]
        return b"ID3" + digest, "audio/mpeg"

    monkeypatch.setattr(cls_tts, "_request_speech", fake_speech)
    return calls


def test_narration_job_success_and_get(client, monkeypatch):
    save_tts_provider(client)
    course, scene_ids = make_speech_course(client, ["光合作用发生在叶绿体。", "原料是水和二氧化碳。"])
    uid = user_id_of(client)
    calls = _install_fake_tts(monkeypatch)

    response = client.post(f"/api/classroom/courses/{course['id']}/narration",
                           json={"voice": "alloy"})
    assert response.status_code == 200, response.text
    created = response.json()
    assert created["course_id"] == course["id"]
    assert created["voice"] == "alloy"
    assert created["scene_total"] == 2
    job = wait_for_status(client, created["job"]["id"], "completed")
    result = job["result"]
    assert result["narrated_scene_count"] == 2
    assert result["synthesized_scene_count"] == 2
    assert result["reused_scene_count"] == 0
    assert result["failed_scene_ids"] == []
    assert result["voice"] == "alloy"
    assert result["provider"] == "测试语音连接"
    assert "旁白合成完成" in result["summary"]
    assert len(calls) == 2

    listing = client.get(f"/api/classroom/courses/{course['id']}/narration").json()
    assert listing["total"] == 2
    assert [item["scene_id"] for item in listing["items"]] == scene_ids
    assert listing["voice"] == "alloy"
    assert listing["total_chars"] == sum(len(calls[i]["text"]) for i in range(2))
    first = listing["items"][0]
    assert first["asset_id"] and first["url"] == "/api/classroom/assets/%d" % first["asset_id"]
    assert first["asset"]["mime_type"] == "audio/mpeg"
    assert first["chars"] == len(calls[0]["text"])
    assert first["missing_asset"] is False

    # 课程配置写入 narration 映射 + 音色；素材可播放（本人）
    config = course_config(course["id"], uid)
    assert config["voice"] == "alloy"
    assert set(config["narration"].keys()) == {str(sid) for sid in scene_ids}
    assert config["narration"][str(scene_ids[0])]["text_sha256"]
    assert client.get(first["url"]).status_code == 200
    assert len(narration_assets(uid)) == 2


def test_narration_second_run_reuses_assets_and_keeps_single_config(client, monkeypatch):
    save_tts_provider(client)
    course, scene_ids = make_speech_course(client, ["第一段讲解。", "第二段讲解。"])
    uid = user_id_of(client)
    calls = _install_fake_tts(monkeypatch)

    first = client.post(f"/api/classroom/courses/{course['id']}/narration", json={})
    assert first.status_code == 200
    wait_for_status(client, first.json()["job"]["id"], "completed")
    assets_before = narration_assets(uid)
    assert len(assets_before) == 2
    calls_before = len(calls)

    second = client.post(f"/api/classroom/courses/{course['id']}/narration", json={})
    assert second.status_code == 200
    job = wait_for_status(client, second.json()["job"]["id"], "completed")
    result = job["result"]
    assert result["reused_scene_count"] == 2
    assert result["synthesized_scene_count"] == 0
    # 幂等：没有重复调用语音服务、没有重复素材、配置仍只有每个场景一份
    assert len(calls) == calls_before
    assert len(narration_assets(uid)) == 2
    config = course_config(course["id"], uid)
    assert set(config["narration"].keys()) == {str(sid) for sid in scene_ids}


def test_narration_voice_change_regenerates(client, monkeypatch):
    save_tts_provider(client)
    course, _scene_ids = make_speech_course(client, ["第一段讲解。"])
    uid = user_id_of(client)
    calls = _install_fake_tts(monkeypatch)

    first = client.post(f"/api/classroom/courses/{course['id']}/narration", json={"voice": "alloy"})
    wait_for_status(client, first.json()["job"]["id"], "completed")
    assert course_config(course["id"], uid)["voice"] == "alloy"

    second = client.post(f"/api/classroom/courses/{course['id']}/narration", json={"voice": "nova"})
    job = wait_for_status(client, second.json()["job"]["id"], "completed")
    assert job["result"]["synthesized_scene_count"] == 1  # 音色变了 → 重新合成
    assert {call["voice"] for call in calls} == {"alloy", "nova"}
    assert course_config(course["id"], uid)["voice"] == "nova"


def test_narration_preserves_web_search_config(client, monkeypatch):
    save_tts_provider(client)
    course, _scene_ids = make_speech_course(client, ["第一段讲解。"])
    uid = user_id_of(client)
    _install_fake_tts(monkeypatch)
    with db.connection() as con:
        con.execute(
            "UPDATE cls_courses SET config_json=? WHERE id=? AND user_id=?",
            (cdb.json_dump({"web_search": {"enabled": True, "limit": 5}, "depth": "standard"}),
             course["id"], uid),
        )
    response = client.post(f"/api/classroom/courses/{course['id']}/narration", json={})
    wait_for_status(client, response.json()["job"]["id"], "completed")
    config = course_config(course["id"], uid)
    assert config["web_search"] == {"enabled": True, "limit": 5}
    assert config["depth"] == "standard"
    assert config["narration"]


def test_narration_scene_subset(client, monkeypatch):
    save_tts_provider(client)
    course, scene_ids = make_speech_course(client, ["第一段讲解。", "第二段讲解。"])
    calls = _install_fake_tts(monkeypatch)
    response = client.post(f"/api/classroom/courses/{course['id']}/narration",
                           json={"scene_ids": [scene_ids[1]]})
    assert response.status_code == 200, response.text
    job = wait_for_status(client, response.json()["job"]["id"], "completed")
    assert job["result"]["scene_count"] == 1
    assert len(calls) == 1
    listing = client.get(f"/api/classroom/courses/{course['id']}/narration").json()
    assert [item["scene_id"] for item in listing["items"]] == [scene_ids[1]]


def test_narration_truncates_overlong_speech(client, monkeypatch):
    save_tts_provider(client)
    course, scene_ids = make_speech_course(client, ["长" * 2600])
    calls = _install_fake_tts(monkeypatch)
    response = client.post(f"/api/classroom/courses/{course['id']}/narration", json={})
    assert response.status_code == 200, response.text
    job = wait_for_status(client, response.json()["job"]["id"], "completed")
    assert job["result"]["truncated_scene_ids"] == [scene_ids[0]]
    assert len(calls[0]["text"]) == cls_narration.MAX_SCENE_TEXT_CHARS
    assert any("已截断" in item for item in [job["result"]["summary"]])


def test_narration_partial_failure_isolated(client, monkeypatch):
    save_tts_provider(client)
    course, scene_ids = make_speech_course(client, ["正常的一段。", "这段会失败。"])
    _install_fake_tts(monkeypatch, fail_texts=("这段会失败",))
    response = client.post(f"/api/classroom/courses/{course['id']}/narration", json={})
    assert response.status_code == 200, response.text
    job = wait_for_status(client, response.json()["job"]["id"], "completed")
    result = job["result"]
    assert result["narrated_scene_count"] == 1
    assert result["synthesized_scene_count"] == 1
    assert result["failed_scene_ids"] == [scene_ids[1]]
    assert result["failures"][0]["scene_id"] == scene_ids[1]
    assert "失败" in result["summary"]
    # 失败场景不写配置、不伪造旁白
    listing = client.get(f"/api/classroom/courses/{course['id']}/narration").json()
    assert [item["scene_id"] for item in listing["items"]] == [scene_ids[0]]


def test_narration_all_failures_make_job_failed(client, monkeypatch):
    save_tts_provider(client)
    course, _scene_ids = make_speech_course(client, ["第一次会失败。", "第二次会失败。"])
    _install_fake_tts(monkeypatch, fail_texts=("会失败",))
    response = client.post(f"/api/classroom/courses/{course['id']}/narration", json={})
    assert response.status_code == 200, response.text
    job = wait_for_status(client, response.json()["job"]["id"], "failed")
    assert "旁白合成失败" in job["error"]
    assert client.get(f"/api/classroom/courses/{course['id']}/narration").json()["items"] == []


# ---------------------------------------------------------------------------
# 7. 旁白任务：取消保留 + checkpoint 续跑
# ---------------------------------------------------------------------------

def test_narration_cancel_keeps_synthesized_scenes_and_resume_skips_them(client, monkeypatch):
    provider = save_tts_provider(client)
    course, scene_ids = make_speech_course(client, ["第一页旁白。", "第二页旁白。", "第三页旁白。"])
    uid = user_id_of(client)
    job_id = insert_narration_job(
        uid, {"course_id": course["id"], "voice": "alloy"}, provider["id"], "tts-1",
        token="narration-cancel-token", status="queued",
    )
    cancel_state = {"triggered": False}

    def on_call(number, text):
        if number == 1 and not cancel_state["triggered"]:
            cancel_state["triggered"] = True
            with db.connection() as con:
                con.execute(
                    "UPDATE ai_jobs SET cancelled=1,status='cancel_requested' WHERE id=?", (job_id,),
                )

    calls = _install_fake_tts(monkeypatch, hook=on_call)
    run_narration_job_direct(job_id, "narration-cancel-token")

    snapshot = job_view(client, job_id)
    assert snapshot["status"] == "cancelled"
    assert snapshot["has_checkpoint"] is True
    listing = client.get(f"/api/classroom/courses/{course['id']}/narration").json()
    assert [item["scene_id"] for item in listing["items"]] == [scene_ids[0]]
    first_asset = listing["items"][0]["asset_id"]
    assert len(calls) == 1
    with db.connection() as con:
        raw = con.execute("SELECT checkpoint_json FROM ai_jobs WHERE id=?", (job_id,)).fetchone()[0]
    cp = json.loads(raw)
    assert cp["done_scene_ids"] == [scene_ids[0]]

    # 续跑：只补未完成的 2 个场景，第一个场景素材原样保留
    resume_fake_calls = _install_fake_tts(monkeypatch)
    assert client.post(f"/api/ai/jobs/{job_id}/resume").status_code == 200
    completed = wait_for_status(client, job_id, "completed")
    assert completed["result"]["synthesized_scene_count"] == 2
    assert completed["result"]["reused_scene_count"] == 1
    listing = client.get(f"/api/classroom/courses/{course['id']}/narration").json()
    assert [item["scene_id"] for item in listing["items"]] == scene_ids
    assert listing["items"][0]["asset_id"] == first_asset
    assert len(resume_fake_calls) == 2


def test_narration_checkpoint_resume_skips_done_scenes(client, monkeypatch):
    provider = save_tts_provider(client)
    course, scene_ids = make_speech_course(client, ["第一页旁白。", "第二页旁白。"])
    uid = user_id_of(client)
    # 先模拟上一轮已完成第 1 个场景（配置 + 素材 + checkpoint 都存在）
    _install_fake_tts(monkeypatch)
    first = client.post(f"/api/classroom/courses/{course['id']}/narration",
                        json={"scene_ids": [scene_ids[0]]})
    wait_for_status(client, first.json()["job"]["id"], "completed")
    listing = client.get(f"/api/classroom/courses/{course['id']}/narration").json()
    first_asset = listing["items"][0]["asset_id"]
    config = course_config(course["id"], uid)

    job_id = insert_narration_job(
        uid, {"course_id": course["id"], "voice": "alloy"}, provider["id"], "tts-1",
        token="narration-resume-token", status="queued",
        checkpoint={
            "course_id": course["id"], "done": config["narration"],
            "done_scene_ids": [scene_ids[0]], "failed_scene_ids": [],
            "voice": "alloy",
        },
    )
    resume_calls = _install_fake_tts(monkeypatch)
    run_narration_job_direct(job_id, "narration-resume-token")
    snapshot = job_view(client, job_id)
    assert snapshot["status"] == "completed"
    assert snapshot["result"]["reused_scene_count"] == 1
    assert snapshot["result"]["synthesized_scene_count"] == 1
    assert len(resume_calls) == 1
    listing = client.get(f"/api/classroom/courses/{course['id']}/narration").json()
    assert listing["total"] == 2
    assert listing["items"][0]["asset_id"] == first_asset


# ---------------------------------------------------------------------------
# 8. 归属与鉴权（旁白 / 音色 / 图片）
# ---------------------------------------------------------------------------

def test_media_endpoints_require_login():
    from app.main import app as fastapi_app

    anon = TestClient(fastapi_app)
    for method, path in (
        ("get", "/api/classroom/voices"),
        ("get", "/api/classroom/courses/1/narration"),
        ("post", "/api/classroom/courses/1/narration"),
        ("post", "/api/classroom/courses/1/images"),
    ):
        kwargs = {"json": {}} if method == "post" else {}
        response = getattr(anon, method)(path, **kwargs)
        assert response.status_code == 401, (method, path, response.status_code)

# ---------------------------------------------------------------------------
# 9. P1 素材类型控制、跨课程引用与安全回收
# ---------------------------------------------------------------------------

def _image_slide(asset_id):
    return {
        "type": "slide", "schemaVersion": 1,
        "canvas": {"width": 1000, "height": 562.5, "theme": {}, "elements": [
            {"id": "image_001", "type": "image", "left": 20, "top": 20, "width": 120,
             "height": 120, "asset_id": int(asset_id),
             "src": "/api/classroom/assets/%d" % int(asset_id), "alt": "配图"},
        ]},
    }


def _insert_raw_asset(user_id, course_id, payload, mime, *, kind="image", write_file=True):
    digest = hashlib.sha256(payload).hexdigest()
    name = "test-%s-%s.bin" % (kind, digest[:20])
    if write_file:
        storage.save_file(cdb.ASSET_FOLDER, name, payload, mime)
    now = db.utcnow()
    with db.connection() as con:
        asset_id = con.execute(
            "INSERT INTO cls_assets(user_id,course_id,kind,storage_name,mime_type,sha256,meta_json,created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (user_id, course_id, kind, name, mime, digest, cdb.json_dump({"test": True}), now),
        ).lastrowid
    return {"id": int(asset_id), "storage_name": name, "mime_type": mime, "sha256": digest}


def _asset_row(asset_id):
    with db.connection() as con:
        row = con.execute("SELECT * FROM cls_assets WHERE id=?", (asset_id,)).fetchone()
    return dict(row) if row else None


def _asset_path(storage_name):
    return storage.local_path(cdb.ASSET_FOLDER, storage_name)


def test_media_type_sniffing_and_import_classification():
    png = _png_bytes()
    assert cls_images.sniff_image_type(png) == "image/png"
    assert cls_images.sniff_media_type(b"<html><script>alert(1)</script></html>") == "text/html"
    assert cls_images.sniff_media_type(
        b'<svg xmlns="http://www.w3.org/2000/svg"><script>x</script></svg>') == "image/svg+xml"
    assert cls_images.sniff_media_type(b"ID3-fake-mp3") == "audio/mpeg"
    assert cls_images.sniff_image_type(b"<svg></svg>") == ""  # SVG 不是白名单图片

    plan = cls_images.asset_serving_plan({"id": 7, "kind": "image", "mime_type": "image/png"}, png)
    assert plan["inline"] is True and plan["media_type"] == "image/png"
    plan = cls_images.asset_serving_plan(
        {"id": 8, "kind": "image", "mime_type": "image/png"},
        b"<!DOCTYPE html><html><body><script>alert(1)</script></body></html>",
    )
    assert plan["inline"] is False
    assert plan["media_type"] == "application/octet-stream"
    assert "attachment" in plan["disposition"] and "asset-8" in plan["disposition"]
    plan = cls_images.asset_serving_plan(
        {"id": 9, "kind": "image", "mime_type": "image/svg+xml"},
        b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>',
    )
    assert plan["inline"] is False and plan["media_type"] == "application/octet-stream"

    classification = cls_images.classify_import_media("image/png", b"<svg><script>x</script></svg>")
    assert classification["inline_allowed"] is False
    assert classification["mime_type"] == "application/octet-stream"
    assert any("隔离下载" in item for item in classification["warnings"])
    classification = cls_images.classify_import_media("image/png", png)
    assert classification["inline_allowed"] is True
    assert classification["mime_type"] == "image/png"


def test_shared_image_asset_kept_until_last_course_deleted(client):
    uid = user_id_of(client)
    course_a = create_course(client, topic="课程甲")
    course_b = create_course(client, topic="课程乙")
    asset = cls_images.save_image_asset(uid, course_a["id"], _png_bytes(), "image/png")
    asset_id = int(asset["id"])
    url = "/api/classroom/assets/%d" % asset_id
    insert_scene(course_a["id"], uid, "slide", 1, "甲页", _image_slide(asset_id))
    insert_scene(course_b["id"], uid, "slide", 1, "乙页", _image_slide(asset_id))

    assert client.get(url).status_code == 200
    assert cls_images.asset_reference_counts(uid, [asset_id])[asset_id] == 2
    references = cls_images.asset_references(uid, [asset_id])[asset_id]
    assert {ref["course_id"] for ref in references} == {course_a["id"], course_b["id"]}

    first = client.delete(f"/api/classroom/courses/{course_a['id']}")
    assert first.status_code == 200
    audit = first.json()["assets"]
    assert audit["examined"] == 1
    assert audit["files_removed"] == 0
    assert [item["asset_id"] for item in audit["retained"]] == [asset_id]
    assert audit["retained"][0]["reassigned_to_course_id"] == course_b["id"]
    row = _asset_row(asset_id)
    assert row is not None and row["course_id"] == course_b["id"]
    assert _asset_path(asset["storage_name"]).is_file()
    assert client.get(url).status_code == 200  # 另一门课程仍可读
    assert client.get(f"/api/classroom/courses/{course_b['id']}").status_code == 200

    second = client.delete(f"/api/classroom/courses/{course_b['id']}")
    assert second.status_code == 200
    audit = second.json()["assets"]
    assert [item["asset_id"] for item in audit["reclaimed"]] == [asset_id]
    assert audit["files_removed"] == 1
    assert _asset_row(asset_id) is None
    assert not _asset_path(asset["storage_name"]).exists()
    missing = client.get(url)
    assert missing.status_code == 404
    assert "素材" in missing.json()["detail"]


def test_shared_tts_asset_survives_owner_delete_and_records_missing(client, monkeypatch):
    save_tts_provider(client)
    course_a, _scene_ids_a = make_speech_course(client, ["课程甲的旁白。"])
    course_b = create_course(client, topic="课程乙")
    uid = user_id_of(client)
    scene_b = insert_scene(course_b["id"], uid, "slide", 1, "乙页", SLIDE_CONTENT,
                           actions=[speech_action("课程乙的旁白。")])
    _install_fake_tts(monkeypatch)
    run = client.post(f"/api/classroom/courses/{course_a['id']}/narration", json={"voice": "alloy"})
    wait_for_status(client, run.json()["job"]["id"], "completed")
    listing_a = client.get(f"/api/classroom/courses/{course_a['id']}/narration").json()
    asset_id = listing_a["items"][0]["asset_id"]
    url = "/api/classroom/assets/%d" % asset_id

    # 模拟内容寻址复用：课程乙 config.narration 指向课程甲已合成的同一素材
    config_b = course_config(course_b["id"], uid)
    config_b["narration"] = {
        str(scene_b): {"asset_id": asset_id, "url": url, "voice": "alloy", "chars": 9,
                       "text_sha256": "test"},
    }
    with db.connection() as con:
        con.execute("UPDATE cls_courses SET config_json=? WHERE id=? AND user_id=?",
                    (cdb.json_dump(config_b), course_b["id"], uid))

    deleted = client.delete(f"/api/classroom/courses/{course_a['id']}")
    assert deleted.status_code == 200
    audit = deleted.json()["assets"]
    assert [item["asset_id"] for item in audit["retained"]] == [asset_id]
    assert client.get(url).status_code == 200
    listing_b = client.get(f"/api/classroom/courses/{course_b['id']}/narration").json()
    assert [item["asset_id"] for item in listing_b["items"]] == [asset_id]
    assert listing_b["items"][0]["missing_asset"] is False

    # 最后一门引用课程删除后才回收素材
    client.delete(f"/api/classroom/courses/{course_b['id']}")
    assert _asset_row(asset_id) is None
    assert client.get(url).status_code == 404


def test_asset_serving_isolates_executable_content_and_degrades_missing_file(client):
    uid = user_id_of(client)
    course = create_course(client)
    html = b"<!DOCTYPE html><html><body><script>window.top.location=1</script></body></html>"
    svg = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
    html_asset = _insert_raw_asset(uid, course["id"], html, "image/png", kind="image")
    svg_asset = _insert_raw_asset(uid, course["id"], svg, "image/svg+xml", kind="image")
    png_asset = _insert_raw_asset(uid, course["id"], _png_bytes(), "image/png", kind="image")

    for record, payload in ((html_asset, html), (svg_asset, svg)):
        response = client.get(f"/api/classroom/assets/{record['id']}")
        assert response.status_code == 200
        assert "attachment" in response.headers["content-disposition"]
        assert response.headers["content-type"].startswith("application/octet-stream")
        assert response.headers["x-content-type-options"] == "nosniff"
        assert "sandbox" in response.headers["content-security-policy"]
        assert response.content == payload

    inline = client.get(f"/api/classroom/assets/{png_asset['id']}")
    assert inline.status_code == 200
    assert inline.headers["content-type"].startswith("image/png")
    assert "content-disposition" not in inline.headers

    # 素材文件缺失：404 中文降级而不是 500
    orphan = _insert_raw_asset(uid, course["id"], b"ID3-orphan", "audio/mpeg",
                               kind="tts", write_file=False)
    broken = client.get(f"/api/classroom/assets/{orphan['id']}")
    assert broken.status_code == 404
    assert "素材文件缺失或已被回收" in broken.json()["detail"]


def test_course_detail_reports_missing_assets_instead_of_500(client):
    uid = user_id_of(client)
    course = create_course(client)
    insert_scene(course["id"], uid, "slide", 1, "缺图页", _image_slide(987654))
    response = client.get(f"/api/classroom/courses/{course['id']}")
    assert response.status_code == 200
    body = response.json()
    assert body["missing_asset_ids"] == [987654]
    assert any("素材" in item for item in body["warnings"])


def test_bounded_upload_probe_reads_only_limit_plus_one_bytes():
    class _CountingUpload:
        def __init__(self, payload):
            self.payload = payload
            self.reads = []

        async def read(self, size=-1):
            self.reads.append(size)
            return self.payload if size is None or size < 0 else self.payload[:size]

    upload = _CountingUpload(b"x" * 16)
    payload = asyncio.run(classroom_api._read_bounded_upload(
        upload, 16, empty_detail="空", too_large_detail="过大",
    ))
    assert payload == b"x" * 16
    assert upload.reads == [17]

    upload = _CountingUpload(b"x" * 200)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(classroom_api._read_bounded_upload(
            upload, 16, empty_detail="空", too_large_detail="过大",
        ))
    assert exc.value.status_code == 413 and "过大" in exc.value.detail
    assert upload.reads == [17]  # 只探测 N+1，不整读上传流


def test_reference_image_over_limit_is_413_without_whole_read(client, monkeypatch):
    save_image_provider(client)
    course = create_course(client)
    monkeypatch.setattr(classroom_api, "MAX_REFERENCE_IMAGE_BYTES", 32)
    response = client.post(
        f"/api/classroom/courses/{course['id']}/images",
        data={"prompt": "画一张光合作用示意图"},
        files={"reference": ("big.png", b"\x89PNG\r\n\x1a\n" + b"x" * 64, "image/png")},
    )
    assert response.status_code == 413
    assert "参考图不能超过" in response.json()["detail"]


def test_asr_audio_over_limit_is_413_without_whole_read(client, monkeypatch):
    monkeypatch.setattr(classroom_api, "MAX_ASR_BYTES", 32)
    response = client.post("/api/classroom/asr", files={
        "file": ("audio.webm", b"x" * 64, "audio/webm"),
    })
    assert response.status_code == 413
    assert "音频文件不能超过" in response.json()["detail"]


def test_image_request_rejects_non_image_upstream_payload(fake_http):
    import base64

    fake_http.queue(_FakeResponse(payload={
        "data": [{"b64_json": base64.b64encode(b"<html><script>x</script></html>").decode()}],
    }))
    with pytest.raises(ValueError) as exc:
        asyncio.run(cls_images._request_image(
            _provider_row(base_url="https://api.openai.com/v1", name="OpenAI 图片"),
            "gpt-image-1", "画一张光合作用示意图",
        ))
    assert "不是支持的图片格式" in str(exc.value)
