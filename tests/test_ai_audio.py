"""语音适配层单元测试：小米 MiMo 的 chat 复合模态 TTS/ASR 与 OpenAI 兼容路径（全部 mock，不联网）。

仓库测试默认 `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`（无 pytest-asyncio），因此用 `asyncio.run` 驱动协程。
"""
import asyncio
import base64
import io
import struct
import wave

import pytest

from app.ai import audio as ai_audio


class _FakeResponse:
    def __init__(self, *, status_code=200, payload=None, content=b"", headers=None, ctype="application/json"):
        self.status_code = status_code
        self._payload = payload
        self.content = content
        self.headers = {"content-type": ctype, **(headers or {})}

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class _FakeClient:
    """记录请求；按队列返回预设响应。"""

    calls: list[dict] = []
    responses: list[_FakeResponse] = []

    def __init__(self, **_kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def post(self, url, headers=None, json=None, files=None, data=None):
        _FakeClient.calls.append({"url": url, "headers": headers, "json": json, "files": files, "data": data})
        return _FakeClient.responses.pop(0)


def _provider(base_url="https://api.xiaomimimo.com/v1", key="sk-test-key", name="小米 MiMo"):
    from app.ai.providers import _encrypt_key

    encrypted, hint = _encrypt_key(key)
    return {
        "id": 1, "name": name, "kind": "openai", "base_url": base_url,
        "encrypted_key": encrypted, "key_hint": hint, "models_json": "[]",
        "auth_mode": "auto", "extra_headers_enc": "", "compat_json": "{}",
    }


@pytest.fixture(autouse=True)
def _patch(monkeypatch):
    from app.ai import audio as mod

    _FakeClient.calls.clear()
    _FakeClient.responses.clear()
    monkeypatch.setattr(mod, "_client", lambda provider, timeout: (provider["base_url"], {}))
    monkeypatch.setattr(mod.ai_httpx, "AsyncClient", _FakeClient)
    yield
    _FakeClient.calls.clear()
    _FakeClient.responses.clear()


def _wav_bytes() -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(16000)
        wf.writeframes(b"".join(struct.pack("<h", 0) for _ in range(1600)))
    return buf.getvalue()


def test_mimo_detection_and_model_markers():
    assert ai_audio.is_mimo_provider({"base_url": "https://api.xiaomimimo.com/v1"}) is True
    assert ai_audio.is_mimo_provider({"base_url": "https://api.deepseek.com/v1"}) is False
    assert ai_audio.is_tts_model("mimo-v2.5-tts") is True
    assert ai_audio.is_tts_model("speech-2.8-turbo") is True
    assert ai_audio.is_tts_model("mimo-v2.6-flash") is False
    assert ai_audio.is_asr_model("mimo-v2.5-asr") is True
    assert ai_audio.is_asr_model("deepseek-flash") is False


def test_mimo_tts_uses_chat_audio_modality_and_maps_unknown_voice():
    audio = _wav_bytes()
    _FakeClient.responses.append(_FakeResponse(payload={
        "choices": [{"message": {"audio": {"data": base64.b64encode(audio).decode()}, "content": ""}}],
    }))
    payload, mime = asyncio.run(ai_audio.synthesize(
        _provider(), "mimo-v2.5-tts", "同学们好，这一页讲光合作用。", voice="alloy", fmt="wav",
    ))
    assert payload == audio
    assert mime == "audio/wav"
    call = _FakeClient.calls[0]
    assert call["url"].endswith("/chat/completions")
    assert call["json"]["modalities"] == ["audio"]
    assert call["json"]["messages"] == [{"role": "assistant", "content": "同学们好，这一页讲光合作用。"}]
    # 非 MiMo 音色（alloy）自动落到默认音色，避免上游 400
    assert call["json"]["audio"]["voice"] == "mimo_default"
    assert call["json"]["audio"]["format"] == "wav"
    assert call["headers"]["Authorization"].startswith("Bearer ")


def test_mimo_tts_keeps_supported_voice():
    _FakeClient.responses.append(_FakeResponse(payload={
        "choices": [{"message": {"audio": {"data": base64.b64encode(_wav_bytes()).decode()}}}],
    }))
    asyncio.run(ai_audio.synthesize(_provider(), "mimo-v2.5-tts", "你好", voice="茉莉"))
    assert _FakeClient.calls[0]["json"]["audio"]["voice"] == "茉莉"


def test_openai_compatible_tts_returns_bytes():
    _FakeClient.responses.append(_FakeResponse(content=b"ID3fake-mp3", headers={"content-type": "audio/mpeg"}))
    payload, mime = asyncio.run(ai_audio.synthesize(
        _provider(base_url="https://api.openai.com/v1"), "tts-1", "hello", voice="alloy", fmt="mp3",
    ))
    assert payload == b"ID3fake-mp3"
    assert mime == "audio/mpeg"
    call = _FakeClient.calls[0]
    assert call["url"].endswith("/audio/speech")
    assert call["json"]["voice"] == "alloy"
    assert call["json"]["response_format"] == "mp3"


def test_mimo_asr_reads_transcript_from_message_content():
    _FakeClient.responses.append(_FakeResponse(payload={
        "choices": [{"message": {"content": "光合作用的原料是水和二氧化碳。"}}],
        "usage": {"total_tokens": 42},
    }))
    result = asyncio.run(ai_audio.transcribe(_provider(), "mimo-v2.5-asr", _wav_bytes(), "audio/wav"))
    assert result["text"] == "光合作用的原料是水和二氧化碳。"
    assert result["usage"]["total_tokens"] == 42
    body = _FakeClient.calls[0]["json"]
    parts = body["messages"][0]["content"]
    assert len(parts) == 1 and parts[0]["type"] == "input_audio"
    assert parts[0]["input_audio"]["format"] == "wav"
    assert body["messages"][0]["role"] == "user"


def test_openai_compatible_asr_posts_multipart():
    _FakeClient.responses.append(_FakeResponse(payload={"text": "hello world"}))
    result = asyncio.run(ai_audio.transcribe(
        _provider(base_url="https://api.openai.com/v1"), "whisper-1", b"RIFffake", "audio/wav",
    ))
    assert result["text"] == "hello world"
    call = _FakeClient.calls[0]
    assert call["url"].endswith("/audio/transcriptions")
    assert "file" in call["files"] and call["data"]["model"] == "whisper-1"


def test_errors_are_value_errors_not_silent():
    _FakeClient.responses.append(_FakeResponse(status_code=401, payload={"error": "bad key"}))
    with pytest.raises(ValueError):
        asyncio.run(ai_audio.synthesize(_provider(), "mimo-v2.5-tts", "你好"))
    _FakeClient.responses.append(_FakeResponse(payload={"choices": [{"message": {"audio": {}}}]}))
    with pytest.raises(ValueError):
        asyncio.run(ai_audio.synthesize(_provider(), "mimo-v2.5-tts", "你好"))
    with pytest.raises(ValueError):
        asyncio.run(ai_audio.synthesize(_provider(), "mimo-v2.5-tts", "  "))
    with pytest.raises(ValueError):
        asyncio.run(ai_audio.transcribe(_provider(), "mimo-v2.5-asr", b"", "audio/wav"))
