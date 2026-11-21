"""厂商语音能力适配：TTS（含小米 MiMo 的 chat 复合模态）与 ASR。

两套真实协议（均已实测）：

* **OpenAI 兼容**：``POST /audio/speech``（返回音频字节）与 ``POST /audio/transcriptions``（multipart，返回文本）；
* **小米 MiMo（xiaomimimo）**：走 ``POST /chat/completions``
  - TTS：``messages=[{"role":"assistant","content":文本}]`` + ``modalities=["audio"]``，
    可选 ``audio={"voice": 音色, "format": "wav"|"mp3"}``，音频在 ``choices[0].message.audio.data``（base64）。
    音色：``mimo_default / 冰糖 / 茉莉 / 苏打 / 白桦 / Mia / Chloe / Milo / Dean``。
  - ASR：``messages=[{"role":"user","content":[{"type":"input_audio","input_audio":{"data":b64,"format":fmt}}]}]``，
    转写文本在 ``choices[0].message.content``；请求里不能带文本 part（网关会拒绝）。

失败一律抛 ``ValueError``（调用方决定回退或报错），不在这里吞异常。
"""
from __future__ import annotations

import base64
import logging

from ..ai import httpx as ai_httpx

log = logging.getLogger("app.ai.audio")

MIMO_HOST_MARKER = "xiaomimimo"
MIMO_TTS_VOICES = (
    "mimo_default", "冰糖", "茉莉", "苏打", "白桦", "Mia", "Chloe", "Milo", "Dean",
)
DEFAULT_MIMO_VOICE = "mimo_default"
#: MiniMax（api.minimaxi.com / api.minimax.chat）。TTS 走 ``/t2a_v2``（返回 hex 音频），
#: ASR 走 ``/speech_to_text``（multipart，模型 ``asr-1.0``）。
MINIMAX_HOST_MARKERS = ("minimaxi.com", "minimax.chat", "minimax.io")
MINIMAX_DEFAULT_TTS_MODEL = "speech-2.8-turbo"
MINIMAX_DEFAULT_ASR_MODEL = "asr-1.0"
MINIMAX_DEFAULT_VOICE = "female-shaonv"
MAX_TTS_CHARS = 4000
TTS_TIMEOUT_SECONDS = 90
ASR_TIMEOUT_SECONDS = 120
_SUPPORTED_TTS_FORMATS = {"wav": "audio/wav", "mp3": "audio/mpeg"}


def is_mimo_provider(provider: dict) -> bool:
    """按 base_url 判断是否小米 MiMo 网关。"""
    base = str((provider or {}).get("base_url") or "").lower()
    return MIMO_HOST_MARKER in base


def is_minimax_provider(provider: dict) -> bool:
    """按 base_url 判断是否 MiniMax 网关。"""
    base = str((provider or {}).get("base_url") or "").lower()
    return any(marker in base for marker in MINIMAX_HOST_MARKERS)


def is_tts_model(model: str) -> bool:
    name = str(model or "").lower()
    return any(marker in name for marker in ("-tts", "tts-", "speech-", "audio-speech"))


def is_asr_model(model: str) -> bool:
    name = str(model or "").strip().lower()
    # ``asr-1.0``（MiniMax）没有连字符前后缀，单独用前缀匹配；其余按常见命名标记。
    if name.startswith(("asr", "whisper")):
        return True
    return any(marker in name for marker in ("-asr", "asr-", "whisper", "transcribe"))


def _auth_headers(provider: dict) -> dict:
    from ..ai.providers import _decrypt_key, provider_extra_headers

    headers: dict[str, str] = {}
    api_key = _decrypt_key((provider or {}).get("encrypted_key") or "")
    if api_key:
        headers["Authorization"] = "Bearer %s" % api_key
    try:
        headers.update(provider_extra_headers(provider) or {})
    except Exception:  # 自定义头非法时按无自定义头处理
        pass
    return headers


def _client(provider: dict, timeout: float):
    from ..ai import netguard
    from ..ai.providers import _validate_url

    base_url = _validate_url((provider or {}).get("base_url") or "", resolve=True)
    return base_url, netguard.client_kwargs(base_url, timeout_seconds=timeout)


def _normalize_voice(voice: str | None, *, mimo: bool, minimax: bool = False) -> str:
    candidate = str(voice or "").strip()
    if mimo:
        return candidate if candidate in MIMO_TTS_VOICES else DEFAULT_MIMO_VOICE
    if minimax:
        # MiniMax 音色是 ``female-shaonv`` 这类 voice_id；传入 MiMo/OpenAI 音色时退回默认女声。
        if not candidate or candidate in MIMO_TTS_VOICES or candidate in ("alloy", "nova", "shimmer", "echo", "fable", "onyx"):
            return MINIMAX_DEFAULT_VOICE
        return candidate
    return candidate or "alloy"


def _sse_audio_settings(audio_format: str) -> dict:
    return {
        "format": audio_format,
        "sample_rate": 32000,
        "bitrate": 128000,
        "channel": 1,
    }


async def _minimax_synthesize(client_kwargs: dict, headers: dict, base_url: str, model: str,
                              text: str, voice: str, audio_format: str) -> tuple[bytes, str]:
    """MiniMax ``/t2a_v2``：音频以 hex 字符串返回（``data.audio``）。"""
    from ..ai.netguard import join_endpoint

    endpoint = join_endpoint(base_url, "/t2a_v2")
    tts_model = model if str(model or "").lower().startswith("speech") else MINIMAX_DEFAULT_TTS_MODEL
    body = {
        "model": tts_model,
        "text": text,
        "stream": False,
        "voice_setting": {"voice_id": voice, "speed": 1.0, "vol": 1.0, "pitch": 0},
        "audio_setting": _sse_audio_settings(audio_format),
    }
    async with ai_httpx.AsyncClient(**client_kwargs) as client:
        response = await client.post(endpoint, headers={**headers, "Content-Type": "application/json"}, json=body)
    if not 200 <= response.status_code < 300:
        raise ValueError("语音服务返回 HTTP %s" % response.status_code)
    try:
        payload = response.json()
    except Exception as exc:  # noqa: BLE001
        raise ValueError("语音服务返回结构无法解析") from exc
    base_resp = (payload.get("base_resp") or {}) if isinstance(payload, dict) else {}
    if base_resp and int(base_resp.get("status_code") or 0) != 0:
        raise ValueError("语音服务错误：%s" % (base_resp.get("status_msg") or "未知错误"))
    encoded = str(((payload.get("data") or {}).get("audio")) or "").strip()
    if not encoded:
        raise ValueError("语音服务返回空音频")
    try:
        audio = bytes.fromhex(encoded)
    except ValueError as exc:
        raise ValueError("语音服务返回的音频不是合法 hex") from exc
    return audio, _SUPPORTED_TTS_FORMATS[audio_format]


def _normalize_format(fmt: str | None) -> str:
    value = str(fmt or "mp3").strip().lower()
    return value if value in _SUPPORTED_TTS_FORMATS else "mp3"


async def synthesize(provider: dict, model: str, text: str, *, voice: str | None = None,
                     fmt: str | None = "mp3", timeout: float = TTS_TIMEOUT_SECONDS) -> tuple[bytes, str]:
    """合成语音，返回 ``(audio_bytes, mime_type)``。

    - 小米 MiMo 的 TTS 模型走 chat 复合模态；
    - 其他厂商走 OpenAI 兼容 ``/audio/speech``。
    """
    content = str(text or "").strip()
    if not content:
        raise ValueError("语音合成文本为空")
    if len(content) > MAX_TTS_CHARS:
        raise ValueError("单次语音合成文本不能超过 %d 字" % MAX_TTS_CHARS)
    base_url, client_kwargs = _client(provider, timeout)
    headers = _auth_headers(provider)
    mimo = is_mimo_provider(provider)
    minimax = is_minimax_provider(provider)
    voice_name = _normalize_voice(voice, mimo=mimo, minimax=minimax)
    audio_format = _normalize_format(fmt)

    if minimax:
        return await _minimax_synthesize(
            client_kwargs, headers, base_url, model, content, voice_name, audio_format,
        )

    if mimo:
        from ..ai.netguard import join_endpoint

        endpoint = join_endpoint(base_url, "/chat/completions")
        body = {
            "model": model,
            "messages": [{"role": "assistant", "content": content}],
            "modalities": ["audio"],
            "audio": {"voice": voice_name, "format": audio_format},
        }
        async with ai_httpx.AsyncClient(**client_kwargs) as client:
            response = await client.post(endpoint, headers=headers, json=body)
        if not 200 <= response.status_code < 300:
            raise ValueError("语音服务返回 HTTP %s" % response.status_code)
        try:
            payload = response.json()
            message = (payload.get("choices") or [{}])[0].get("message") or {}
            encoded = ((message.get("audio") or {}).get("data")) or ""
            audio = base64.b64decode(encoded) if encoded else b""
        except Exception as exc:  # noqa: BLE001 - 上游结构异常统一按失败处理
            raise ValueError("语音服务返回结构无法解析") from exc
        if not audio:
            raise ValueError("语音服务返回空音频")
        return audio, _SUPPORTED_TTS_FORMATS[audio_format]

    from ..ai.netguard import join_endpoint

    endpoint = join_endpoint(base_url, "/audio/speech")
    headers = {**headers, "Content-Type": "application/json", "Accept": "audio/*"}
    body = {
        "model": model,
        "input": content,
        "voice": voice_name,
        "response_format": audio_format,
    }
    async with ai_httpx.AsyncClient(**client_kwargs) as client:
        response = await client.post(endpoint, headers=headers, json=body)
    if not 200 <= response.status_code < 300:
        raise ValueError("语音服务返回 HTTP %s" % response.status_code)
    audio = response.content
    if not audio:
        raise ValueError("语音服务返回空音频")
    mime = (response.headers.get("content-type") or _SUPPORTED_TTS_FORMATS[audio_format]).split(";")[0].strip()
    return audio, mime or _SUPPORTED_TTS_FORMATS[audio_format]


def _audio_format_of(mime: str) -> str:
    value = str(mime or "").lower()
    if "wav" in value or "wave" in value:
        return "wav"
    if "mp3" in value or "mpeg" in value:
        return "mp3"
    if "ogg" in value or "opus" in value:
        return "ogg"
    if "webm" in value:
        return "webm"
    if "m4a" in value or "mp4" in value or "aac" in value:
        return "m4a"
    return "wav"


async def transcribe(provider: dict, model: str, audio: bytes, mime: str = "audio/wav", *,
                     timeout: float = ASR_TIMEOUT_SECONDS) -> dict:
    """语音转写，返回 ``{"text": str, "usage": dict, "model": str}``。"""
    if not audio:
        raise ValueError("音频内容为空")
    if len(audio) > 25 * 1024 * 1024:
        raise ValueError("音频文件不能超过 25MB")
    base_url, client_kwargs = _client(provider, timeout)
    headers = _auth_headers(provider)
    audio_format = _audio_format_of(mime)

    if is_minimax_provider(provider):
        from ..ai.netguard import join_endpoint

        endpoint = join_endpoint(base_url, "/speech_to_text")
        asr_model = model if str(model or "").lower().startswith("asr") else MINIMAX_DEFAULT_ASR_MODEL
        ext = "mp3" if audio_format == "mp3" else audio_format
        files = {"file": ("audio.%s" % ext, audio, mime or "audio/wav")}
        async with ai_httpx.AsyncClient(**client_kwargs) as client:
            response = await client.post(endpoint, headers=headers, files=files, data={"model": asr_model})
        if not 200 <= response.status_code < 300:
            raise ValueError("语音识别服务返回 HTTP %s" % response.status_code)
        try:
            payload = response.json()
        except Exception as exc:  # noqa: BLE001
            raise ValueError("语音识别返回结构无法解析") from exc
        return {
            "text": str(payload.get("text") or "").strip(),
            "usage": {"duration": payload.get("duration")},
            "model": asr_model,
        }

    if is_mimo_provider(provider):
        from ..ai.netguard import join_endpoint

        endpoint = join_endpoint(base_url, "/chat/completions")
        body = {
            "model": model,
            "messages": [{
                "role": "user",
                "content": [{
                    "type": "input_audio",
                    "input_audio": {
                        "data": base64.b64encode(audio).decode("ascii"),
                        "format": audio_format,
                    },
                }],
            }],
        }
        async with ai_httpx.AsyncClient(**client_kwargs) as client:
            response = await client.post(endpoint, headers=headers, json=body)
        if not 200 <= response.status_code < 300:
            raise ValueError("语音识别服务返回 HTTP %s" % response.status_code)
        try:
            payload = response.json()
            message = (payload.get("choices") or [{}])[0].get("message") or {}
            text = str(message.get("content") or "").strip()
        except Exception as exc:  # noqa: BLE001
            raise ValueError("语音识别返回结构无法解析") from exc
        usage = payload.get("usage") if isinstance(payload, dict) else None
        return {"text": text, "usage": usage or {}, "model": model}

    from ..ai.netguard import join_endpoint

    endpoint = join_endpoint(base_url, "/audio/transcriptions")
    files = {"file": ("audio.%s" % audio_format, audio, mime or "audio/wav")}
    data = {"model": model}
    async with ai_httpx.AsyncClient(**client_kwargs) as client:
        response = await client.post(endpoint, headers=headers, files=files, data=data)
    if not 200 <= response.status_code < 300:
        raise ValueError("语音识别服务返回 HTTP %s" % response.status_code)
    try:
        payload = response.json()
    except Exception as exc:  # noqa: BLE001
        raise ValueError("语音识别返回结构无法解析") from exc
    return {
        "text": str(payload.get("text") or "").strip(),
        "usage": payload.get("usage") or {},
        "model": model,
    }


__all__ = [
    "MIMO_TTS_VOICES", "DEFAULT_MIMO_VOICE", "MAX_TTS_CHARS",
    "is_mimo_provider", "is_tts_model", "is_asr_model",
    "synthesize", "transcribe",
]
