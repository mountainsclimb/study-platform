"""MiniMax 媒体适配单元测试：TTS（/t2a_v2 hex 音频）、ASR（/speech_to_text multipart）、图片（/image_generation）。

全部 mock，不联网；用 asyncio.run 驱动（仓库默认禁用 pytest-asyncio 插件）。
"""
import asyncio
import base64
import io

import pytest

from app.ai import audio as ai_audio
from app.classroom import images as cls_images


class _FakeResponse:
    def __init__(self, *, status_code=200, payload=None, content=b"", headers=None):
        self.status_code = status_code
        self._payload = payload
        self.content = content
        self.headers = {"content-type": "application/json", **(headers or {})}

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class _FakeClient:
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


def _provider(base_url="https://api.minimaxi.com/v1"):
    from app.ai.providers import _encrypt_key

    encrypted, hint = _encrypt_key("sk-api-test-key")
    return {
        "id": 2, "name": "MiniMax", "kind": "openai", "base_url": base_url,
        "encrypted_key": encrypted, "key_hint": hint, "models_json": "[]",
        "auth_mode": "auto", "extra_headers_enc": "", "compat_json": "{}",
    }


def _png_bytes() -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (8, 8), "white").save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture(autouse=True)
def _patch(monkeypatch):
    from app.ai import audio as mod

    _FakeClient.calls.clear()
    _FakeClient.responses.clear()
    monkeypatch.setattr(mod, "_client", lambda provider, timeout: (provider["base_url"], {}))
    monkeypatch.setattr(mod.ai_httpx, "AsyncClient", _FakeClient)
    # 图片模块内部延迟导入 netguard/providers；统一走假客户端并跳过地址解析
    monkeypatch.setattr("app.ai.providers._validate_url", lambda value, resolve=False: value)
    monkeypatch.setattr("app.ai.netguard.client_kwargs", lambda endpoint, timeout_seconds=60: {})
    yield
    _FakeClient.calls.clear()
    _FakeClient.responses.clear()


def test_minimax_detection():
    assert ai_audio.is_minimax_provider({"base_url": "https://api.minimaxi.com/v1"}) is True
    assert ai_audio.is_minimax_provider({"base_url": "https://api.minimax.chat/v1"}) is True
    assert ai_audio.is_minimax_provider({"base_url": "https://api.xiaomimimo.com/v1"}) is False


def test_minimax_tts_decodes_hex_and_defaults_model_and_voice():
    mp3 = b"ID3" + b"\x00" * 64
    _FakeClient.responses.append(_FakeResponse(payload={
        "data": {"audio": mp3.hex()}, "base_resp": {"status_code": 0, "status_msg": "success"},
    }))
    payload, mime = asyncio.run(ai_audio.synthesize(
        _provider(), "mimo-v2.5-tts", "同学们好。", voice="茉莉", fmt="mp3",
    ))
    assert payload == mp3
    assert mime == "audio/mpeg"
    call = _FakeClient.calls[0]
    assert call["url"].endswith("/t2a_v2")
    assert call["json"]["model"] == "speech-2.8-turbo"  # 非 speech 模型自动落到默认 TTS 模型
    assert call["json"]["voice_setting"]["voice_id"] == "female-shaonv"  # MiMo 音色在 MiniMax 上退回默认
    assert call["json"]["audio_setting"]["format"] == "mp3"
    assert call["json"]["stream"] is False


def test_minimax_tts_keeps_native_voice_and_model():
    _FakeClient.responses.append(_FakeResponse(payload={
        "data": {"audio": b"ID3x".hex()}, "base_resp": {"status_code": 0},
    }))
    asyncio.run(ai_audio.synthesize(
        _provider(), "speech-2.8-turbo", "你好", voice="female-shaonv",
    ))
    body = _FakeClient.calls[0]["json"]
    assert body["model"] == "speech-2.8-turbo"
    assert body["voice_setting"]["voice_id"] == "female-shaonv"


def test_minimax_tts_base_resp_error_is_value_error():
    _FakeClient.responses.append(_FakeResponse(payload={
        "data": None, "base_resp": {"status_code": 1008, "status_msg": "insufficient balance"},
    }))
    with pytest.raises(ValueError) as exc:
        asyncio.run(ai_audio.synthesize(_provider(), "speech-2.8-turbo", "你好"))
    assert "balance" in str(exc.value)


def test_minimax_asr_uses_multipart_and_default_model():
    _FakeClient.responses.append(_FakeResponse(payload={"text": "光合作用的原料是水和二氧化碳。", "duration": 2.4}))
    result = asyncio.run(ai_audio.transcribe(_provider(), "speech-2.8-turbo", b"ID3abc", "audio/mpeg"))
    assert result["text"] == "光合作用的原料是水和二氧化碳。"
    assert result["model"] == "asr-1.0"
    call = _FakeClient.calls[0]
    assert call["url"].endswith("/speech_to_text")
    assert call["data"] == {"model": "asr-1.0"}
    assert "file" in call["files"]


def test_minimax_image_returns_decoded_bytes():
    png = _png_bytes()
    _FakeClient.responses.append(_FakeResponse(payload={
        "id": "x", "data": {"image_base64": [base64.b64encode(png).decode()]},
        "base_resp": {"status_code": 0, "status_msg": "success"},
    }))
    data, mime = asyncio.run(cls_images._request_image(_provider(), "image-01", "教室插画"))
    assert data == png
    assert mime == "image/png"
    call = _FakeClient.calls[0]
    assert call["url"].endswith("/image_generation")
    assert call["json"]["model"] == "image-01"
    assert call["json"]["response_format"] == "base64"
    assert call["json"]["aspect_ratio"] == "1:1"


def test_minimax_image_model_normalization_and_error():
    _FakeClient.responses.append(_FakeResponse(payload={
        "data": {"image_base64": []}, "base_resp": {"status_code": 2013, "status_msg": "unsupported model: H3"},
    }))
    with pytest.raises(ValueError) as exc:
        asyncio.run(cls_images._request_image(_provider(), "MiniMax-H3-Image", "x"))
    assert "unsupported" in str(exc.value)
    assert _FakeClient.calls[0]["json"]["model"] == "image-01"


def test_minimax_image_accepts_urls_fallback(monkeypatch):
    png = _png_bytes()

    async def fake_download(url):
        return png

    monkeypatch.setattr(cls_images, "_download_image", fake_download)
    _FakeClient.responses.append(_FakeResponse(payload={
        "data": {"image_urls": ["https://example.com/a.png"]}, "base_resp": {"status_code": 0},
    }))
    data, mime = asyncio.run(cls_images._request_image(_provider(), "image-01-live", "x"))
    assert data == png and mime == "image/png"
