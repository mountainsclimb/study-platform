"""零厂商配置下的本地旁白 / 本地 ASR 接入验收（方案 §4）。

全程假本地引擎，不加载 sherpa-onnx 模型。覆盖：
* `/classroom/meta`、`/classroom/voices`、`/classroom/asr/info` 的本地能力形状；
* `tts.synthesize` 本地分支落 `cls_assets`（provider=local）；
* 本地合成失败回退浏览器；
* `POST /classroom/asr` 本地分支返回文本，非 WAV 返回 422；
* 旁白准入 `_resolve_tts_candidate` 本地候选（provider_id=0）不再 422；
* `_selected_provider(allow_local=True)` 哨兵与越界模型校验。
"""
import asyncio
from io import BytesIO
import types
import wave

from fastapi import HTTPException
import pytest

from app import db
from app.ai import providers, speech_local
from app.classroom import db as cdb
from app.classroom import narration, tts


def user_id_of(client):
    return client.get("/api/auth/me").json()["id"]


def create_course(client, **overrides):
    payload = {"topic": "光合作用", "subject": "生物", "grade": "初中二年级"}
    payload.update(overrides)
    response = client.post("/api/classroom/courses", json=payload)
    assert response.status_code == 200, response.text
    return response.json()["course"]


def insert_scene(course_id, user_id, content, meta):
    now = db.utcnow()
    with db.connection() as con:
        return con.execute(
            "INSERT INTO cls_scenes(course_id,user_id,scene_type,order_index,title,content_json,meta_json,status,"
            "created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (course_id, user_id, "explain", 0, "场景 1", cdb.json_dump(content),
             cdb.json_dump(meta), "ready", now, now),
        ).lastrowid


def wav_bytes(seconds=0.2, rate=16000):
    count = max(1, int(rate * seconds))
    pcm = b"\x10\x00" * count
    buffer = BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(pcm)
    return buffer.getvalue()


@pytest.fixture(autouse=True)
def _local_available(monkeypatch):
    speech_local.reset_engine()
    monkeypatch.setattr(speech_local, "tts_available", lambda: True)
    monkeypatch.setattr(speech_local, "asr_available", lambda: True)
    monkeypatch.setattr(speech_local, "model_label", lambda: "local:test-tts")
    monkeypatch.setattr(speech_local, "asr_model_label", lambda: "local:test-asr")
    monkeypatch.setattr(speech_local, "normalize_voice", lambda voice: str(voice or "0"))
    monkeypatch.setattr(
        speech_local, "synthesize",
        lambda text, voice=None, speed=None: (wav_bytes(), "audio/wav"),
    )
    monkeypatch.setattr(
        speech_local, "transcribe",
        lambda payload: {"text": "本地识别文本", "words": []},
    )
    monkeypatch.setattr(
        speech_local, "list_voices",
        lambda: [
            {"voice_id": "0", "name": "本地 · 音色 1", "lang": "zh-CN", "default": True},
            {"voice_id": "1", "name": "本地 · 音色 2", "lang": "zh-CN", "default": False},
        ],
    )
    yield
    speech_local.reset_engine()


def test_meta_and_voice_catalog_expose_local(client):
    uid = user_id_of(client)
    info = tts.tts_provider_info(uid)
    assert info["provider"] == "local"
    assert info["model"] == "local:test-tts"
    assert info["provider_id"] is None

    meta = client.get("/api/classroom/meta").json()
    assert meta["tts"]["provider"] == "local"
    assert meta["asr"]["available"] is True
    assert meta["asr"]["provider"] == "local"

    catalog = asyncio.run(tts.list_voices(uid))
    assert catalog["total"] == 2
    assert {item["provider"] for item in catalog["items"]} == {"本地语音"}
    assert all(item["provider_id"] is None for item in catalog["items"])
    assert catalog["providers"][0]["source"] == "local"

    http_catalog = client.get("/api/classroom/voices").json()
    assert http_catalog["total"] == 2
    assert http_catalog["items"][0]["source"] == "local"
    filtered = client.get("/api/classroom/voices", params={"provider_id": 0}).json()
    assert filtered["total"] == 2
    assert filtered["items"][0]["provider_id"] is None


def test_local_tts_saves_asset(client):
    uid = user_id_of(client)
    course = create_course(client)
    result = asyncio.run(tts.synthesize(
        user_id=uid, course_id=course["id"], text="第一句旁白。", voice="0",
    ))
    assert result["provider"] == "local"
    assert result["model"] == "local:test-tts"
    assert result["voice"] == "0"
    assert result["asset"]["mime_type"] == "audio/wav"
    with db.connection() as con:
        row = con.execute(
            "SELECT kind, mime_type FROM cls_assets WHERE id=?", (result["asset"]["id"],),
        ).fetchone()
    assert dict(row) == {"kind": tts.TTS_KIND, "mime_type": "audio/wav"}


def test_local_tts_failure_falls_back_to_browser(client, monkeypatch):
    uid = user_id_of(client)
    course = create_course(client)

    def broken(*args, **kwargs):
        raise RuntimeError("模拟本地合成失败")

    monkeypatch.setattr(speech_local, "synthesize", broken)
    result = asyncio.run(tts.synthesize(
        user_id=uid, course_id=course["id"], text="旁白", voice="0",
    ))
    assert result["fallback"] == "browser"
    assert "本地语音合成失败" in result["error"]


def test_asr_info_and_local_transcription(client):
    info = client.get("/api/classroom/asr/info").json()
    assert info["available"] is True
    assert info["provider"] == "local"
    assert info["provider_id"] is None
    assert info["model"] == "local:test-asr"
    assert info["max_seconds"] == 60

    for extra in ({}, {"model": "local:test-asr"}, {"provider_id": "0", "model": "local:test-asr"}):
        response = client.post("/api/classroom/asr",
            files={"file": ("voice.wav", wav_bytes(), "audio/wav")}, data=extra)
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["text"] == "本地识别文本"
        assert payload["model"] == "local:test-asr"
        assert payload["usage"] is None

    rejected = client.post("/api/classroom/asr", files={
        "file": ("voice.webm", b"not a wav", "audio/webm"),
    })
    # 假引擎不做格式校验；用真实 speech_local 校验非 WAV 的 422 口径。
    assert rejected.status_code == 200


def test_local_asr_rejects_non_wav_via_real_validator(client, monkeypatch):
    def real_transcribe(payload):
        from app.ai import speech_local as module

        return module._read_wav(payload) and {"text": "ok", "words": []}

    monkeypatch.setattr(speech_local, "transcribe", real_transcribe)
    with monkeypatch.context() as patch:
        patch.setattr(speech_local, "_module_available", lambda: True)
        patch.setenv("STUDY_LOCAL_SPEECH", "on")
        response = client.post("/api/classroom/asr", files={
            "file": ("voice.webm", b"not a wav", "audio/webm"),
        })
    assert response.status_code == 422
    assert "WAV" in response.json()["detail"]


def test_narration_local_candidate_and_validation(client):
    uid = user_id_of(client)
    user = client.get("/api/auth/me").json()
    course = create_course(client)
    insert_scene(
        course["id"], uid,
        {"title": "光合作用"},
        {"actions": [{"type": "speech", "text": "第一句旁白。"}]},
    )

    for provider_id, model in ((None, None), (None, "local:test-tts"), (0, "local:test-tts")):
        candidate = narration._resolve_tts_candidate(uid, provider_id=provider_id, model=model)
        assert candidate["provider"]["id"] == 0
        assert candidate["provider"]["kind"] == "local"
        assert candidate["model"] == "local:test-tts"

    with pytest.raises(HTTPException) as error:
        narration._resolve_tts_candidate(uid, provider_id=999)
    assert error.value.status_code == 422
    with pytest.raises(HTTPException):
        narration._resolve_tts_candidate(uid, model="vendor-model")

    data = types.SimpleNamespace(
        params={"course_id": course["id"]}, provider_id=0, model="local:test-tts",
    )
    narration.validate_narration_job(data, user)


def test_selected_provider_local_sentinel(client):
    uid = user_id_of(client)
    provider = providers._selected_provider(uid, 0, "local:test-tts", allow_local=True)
    assert provider["id"] == 0 and provider["kind"] == "local"
    with pytest.raises(HTTPException) as error:
        providers._selected_provider(uid, 0, "local:other", allow_local=True)
    assert error.value.status_code == 422
    with pytest.raises(HTTPException):
        providers._selected_provider(uid, 0, "local:test-tts", allow_local=False)
