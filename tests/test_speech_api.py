"""通用语音 API（阶段 4-1/4-3/4-5）：朗读、转写、跟读与降级口径。"""
from __future__ import annotations

import pytest

from app import speech_api
from app.ai import audio_compress, speech_local
from app.classroom import tts


@pytest.fixture(autouse=True)
def _clean_speech(monkeypatch):
    speech_api._LOCAL_CACHE.clear()
    speech_local.reset_engine()
    # 单元测试不依赖本机是否安装 ffmpeg：统一按「不可用」验证回退 WAV。
    monkeypatch.setattr(audio_compress, "ffmpeg_path", lambda: None)
    yield
    speech_local.reset_engine()
    speech_api._LOCAL_CACHE.clear()


def _enable_local(monkeypatch, *, tts_ok=True, asr_ok=True):
    monkeypatch.setattr(speech_local, "tts_available", lambda: tts_ok)
    monkeypatch.setattr(speech_local, "asr_available", lambda: asr_ok)
    monkeypatch.setattr(speech_local, "model_label", lambda: "local:test-tts")
    monkeypatch.setattr(speech_local, "asr_model_label", lambda: "local:test-asr")
    monkeypatch.setattr(speech_local, "normalize_voice", lambda voice: str(voice or "0"))
    monkeypatch.setattr(speech_local, "max_asr_seconds", lambda: 60)


def test_info_reports_local_tts_and_asr(client, monkeypatch):
    _enable_local(monkeypatch)
    body = client.get("/api/speech/info").json()
    assert body["tts"]["available"] is True
    assert body["tts"]["source"] == "local"
    assert body["tts"]["model"] == "local:test-tts"
    assert body["tts"]["browser_fallback"] is False
    assert body["asr"]["available"] is True
    assert body["asr"]["source"] == "local"
    assert body["asr"]["max_seconds"] == 60


def test_info_degrades_to_browser_without_engines(client, monkeypatch):
    _enable_local(monkeypatch, tts_ok=False, asr_ok=False)
    body = client.get("/api/speech/info").json()
    assert body["tts"]["available"] is False
    assert body["tts"]["source"] is None
    assert body["tts"]["browser_fallback"] is True
    assert "浏览器朗读" in body["tts"]["notice"]
    assert body["asr"]["available"] is False


def test_local_tts_returns_audio_and_caches(client, monkeypatch):
    _enable_local(monkeypatch)
    calls = []

    def fake_synthesize(text, voice=None, speed=None):
        calls.append((text, voice, speed))
        return b"RIFF-local", "audio/wav"

    monkeypatch.setattr(speech_local, "synthesize", fake_synthesize)
    first = client.post("/api/speech/tts", json={"text": "第一段朗读", "voice": "1", "speed": 1.2})
    assert first.status_code == 200, first.text
    assert first.content == b"RIFF-local"
    assert first.headers["content-type"].startswith("audio/wav")
    assert first.headers["x-speech-source"] == "local"
    assert first.headers["x-speech-format"] == "wav"
    assert first.headers["x-speech-voice"] == "1"

    second = client.post("/api/speech/tts", json={"text": "第一段朗读", "voice": "1", "speed": 1.2})
    assert second.status_code == 200 and second.content == b"RIFF-local"
    assert len(calls) == 1  # 相同文本/音色/速度命中进程内缓存

    other = client.post("/api/speech/tts", json={"text": "第一段朗读", "voice": "2"})
    assert other.status_code == 200
    assert len(calls) == 2


def test_tts_503_when_no_server_engine(client, monkeypatch):
    _enable_local(monkeypatch, tts_ok=False, asr_ok=False)
    response = client.post("/api/speech/tts", json={"text": "请朗读"})
    assert response.status_code == 503
    assert "浏览器朗读" in response.json()["detail"]


def test_provider_tts_is_used_when_configured(client, monkeypatch):
    _enable_local(monkeypatch, tts_ok=False)
    provider = {"id": 7, "name": "测试语音", "kind": "openai"}
    monkeypatch.setattr(tts, "tts_candidates", lambda user_id: [{"provider": provider, "model": "tts-1"}])
    monkeypatch.setattr(tts, "normalize_voice_for", lambda provider, voice: voice or "alloy")
    seen = {}

    async def fake_provider(provider, model, text, *, voice=None, speed=None):
        seen.update({"provider": provider, "model": model, "text": text, "voice": voice})
        return b"ID3-provider", "audio/mpeg"

    monkeypatch.setattr(tts, "synthesize_provider", fake_provider)
    response = client.post("/api/speech/tts", json={"text": "厂商朗读", "voice": "nova"})
    assert response.status_code == 200, response.text
    assert response.content == b"ID3-provider"
    assert response.headers["x-speech-source"] == "provider"
    assert response.headers["x-speech-format"] == "mpeg"
    assert seen == {"provider": provider, "model": "tts-1", "text": "厂商朗读", "voice": "nova"}


def test_provider_errors_map_to_502(client, monkeypatch):
    _enable_local(monkeypatch, tts_ok=False)
    provider = {"id": 7, "name": "测试语音", "kind": "openai"}
    monkeypatch.setattr(tts, "tts_candidates", lambda user_id: [{"provider": provider, "model": "tts-1"}])
    monkeypatch.setattr(tts, "normalize_voice_for", lambda provider, voice: "alloy")

    async def boom(*args, **kwargs):
        raise RuntimeError("upstream")

    monkeypatch.setattr(tts, "synthesize_provider", boom)
    response = client.post("/api/speech/tts", json={"text": "厂商朗读"})
    assert response.status_code == 502
    assert "语音合成服务暂不可用" in response.json()["detail"]


def test_transcribe_and_shadowing_use_local_asr(client, monkeypatch):
    _enable_local(monkeypatch)
    monkeypatch.setattr(
        speech_local, "transcribe",
        lambda payload: {"text": "春眠不觉晓处处闻啼鸟", "words": []},
    )
    files = {"file": ("recording.wav", b"RIFF-fake", "audio/wav")}
    transcribed = client.post("/api/speech/transcribe", files=files)
    assert transcribed.status_code == 200, transcribed.text
    assert transcribed.json()["text"] == "春眠不觉晓处处闻啼鸟"
    assert transcribed.json()["source"] == "local"

    files = {"file": ("recording.wav", b"RIFF-fake", "audio/wav")}
    checked = client.post(
        "/api/speech/shadowing/check",
        data={"expected": "春眠不觉晓，处处闻啼鸟。"},
        files=files,
    )
    assert checked.status_code == 200, checked.text
    body = checked.json()
    assert body["similar"] is True
    assert body["score"] == 100
    assert body["transcript"] == "春眠不觉晓处处闻啼鸟"
    assert "不代表发音质量" in body["note"]


def test_shadowing_marks_empty_and_short_mismatch(client, monkeypatch):
    _enable_local(monkeypatch)
    monkeypatch.setattr(speech_local, "transcribe", lambda payload: {"text": "", "words": []})
    files = {"file": ("recording.wav", b"RIFF-fake", "audio/wav")}
    empty = client.post(
        "/api/speech/shadowing/check", data={"expected": "春眠不觉晓"}, files=files,
    ).json()
    assert empty["similar"] is False and empty["score"] == 0
    assert empty["reason"] == "没有识别到文本"

    monkeypatch.setattr(speech_local, "transcribe", lambda payload: {"text": "cut", "words": []})
    files = {"file": ("recording.wav", b"RIFF-fake", "audio/wav")}
    short = client.post(
        "/api/speech/shadowing/check", data={"expected": "cat"}, files=files,
    ).json()
    assert short["similar"] is False
    assert 0 < short["score"] < 80


def test_transcribe_unavailable_is_503(client, monkeypatch):
    _enable_local(monkeypatch, asr_ok=False)
    files = {"file": ("recording.wav", b"RIFF-fake", "audio/wav")}
    response = client.post("/api/speech/transcribe", files=files)
    assert response.status_code == 503
    assert "语音识别不可用" in response.json()["detail"]


def test_empty_and_oversized_tts_text_rejected(client, monkeypatch):
    _enable_local(monkeypatch)
    assert client.post("/api/speech/tts", json={"text": "   "}).status_code == 422
    assert client.post("/api/speech/tts", json={"text": "字" * 2001}).status_code == 422


def test_speech_endpoints_require_login(client):
    from fastapi.testclient import TestClient

    from app.main import app

    anonymous = TestClient(app)
    assert anonymous.get("/api/speech/info").status_code == 401
    assert anonymous.post("/api/speech/tts", json={"text": "朗读"}).status_code == 401
    files = {"file": ("recording.wav", b"RIFF-fake", "audio/wav")}
    assert anonymous.post("/api/speech/transcribe", files=files).status_code == 401
    assert anonymous.post(
        "/api/speech/shadowing/check", data={"expected": "原文"}, files=files,
    ).status_code == 401
