"""通用语音 API（方案阶段 4-1 / 4-3 / 4-4 / 4-5）。

把课堂命名空间里的朗读/识别能力抽成跨场景接口：

* ``GET  /api/speech/info``：当前账号的合成/识别能力、来源、上限与中文说明；
* ``POST /api/speech/tts``：文本 → 音频字节（本地 WAV 经 MP3/Opus 压缩，失败回退 WAV）；
  没有服务端语音时返回 503 中文原因，前端据此回退浏览器 ``speechSynthesis``；
* ``POST /api/speech/transcribe``：音频 → 文本（本地模型优先，其次已配置的云端 ASR）；
* ``POST /api/speech/shadowing/check``：跟读检查，只给文本相似度，明确不是发音评测。

课堂旧接口（``/api/classroom/tts``、``/api/classroom/asr``）保持兼容；本模块不写
``cls_assets``，朗读请求不落库，音频正文与公式内容均不进入学习事件。
"""
from __future__ import annotations

import asyncio
from collections import OrderedDict
import re
import threading
import time

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field

from .ai import asr_providers, audio_compress, speech_local
from .ai.speech_score import score_reading
from .auth import current_user

router = APIRouter(prefix="/api/speech", tags=["Speech"])

MAX_ASR_BYTES = 25 * 1024 * 1024
MAX_EXPECTED_CHARS = 2000
MAX_TRANSCRIPT_CHARS = 2000
_LOCAL_CACHE_MAX = 16
_LOCAL_CACHE_MAX_TEXT = 600
_LOCAL_CACHE_LOCK = threading.Lock()
_LOCAL_CACHE: "OrderedDict[tuple, tuple[bytes, str, str]]" = OrderedDict()


class TtsInput(BaseModel):
    text: str = Field(min_length=1, max_length=2000)
    voice: str | None = Field(default=None, max_length=60)
    speed: float | None = Field(default=None, ge=0.5, le=2.0)
    provider_id: int | None = None
    model: str | None = Field(default=None, max_length=160)


def _classroom_tts():
    # 延迟导入：课堂包初始化会注册大量路由/任务，避免模块导入期循环依赖。
    from .classroom import tts

    return tts


def _cache_key(text: str, voice: str, speed: float) -> tuple:
    return (
        text, voice, round(float(speed), 2), speech_local.model_label(),
        audio_compress.speech_format(),
    )


def _cache_get(key: tuple) -> tuple[bytes, str, str] | None:
    with _LOCAL_CACHE_LOCK:
        value = _LOCAL_CACHE.get(key)
        if value is None:
            return None
        _LOCAL_CACHE.move_to_end(key)
        return value


def _cache_put(key: tuple, value: tuple[bytes, str, str]) -> None:
    with _LOCAL_CACHE_LOCK:
        _LOCAL_CACHE[key] = value
        _LOCAL_CACHE.move_to_end(key)
        while len(_LOCAL_CACHE) > _LOCAL_CACHE_MAX:
            _LOCAL_CACHE.popitem(last=False)


def _local_synthesize_cached(text: str, voice: str, speed: float) -> tuple[bytes, str, str]:
    """本地合成 + 压缩；短文本走进程内 LRU（键含音色与目标格式）。"""
    key = _cache_key(text, voice, speed)
    hit = _cache_get(key)
    if hit is not None:
        return hit
    audio, mime = speech_local.synthesize(text, voice, speed)
    audio, mime, audio_format = audio_compress.compress_speech(audio, mime)
    result = (audio, mime, audio_format)
    if len(text) <= _LOCAL_CACHE_MAX_TEXT:
        _cache_put(key, result)
    return result


def _tts_info(user_id: int) -> dict:
    from .classroom.tts import tts_provider_info

    info = tts_provider_info(user_id)
    local_ready = bool(speech_local.tts_available())
    if info and str(info.get("provider") or "").lower() == "local":
        source = "local"
    elif info:
        source = "provider"
    else:
        source = None
    notice = "服务端朗读可用：音频按需合成，不保存朗读正文。"
    if source == "local":
        notice = "本地朗读可用：合成在本机完成，音频经过压缩后返回，不保存朗读正文。"
    elif source is None:
        notice = "服务端朗读不可用；前端将回退浏览器朗读（若浏览器支持）。"
    return {
        "available": bool(info),
        "source": source,
        "provider": (info or {}).get("provider"),
        "provider_id": (info or {}).get("provider_id"),
        "model": (info or {}).get("model"),
        "local_available": local_ready,
        "max_chars": speech_local.MAX_TTS_CHARS if source == "local" else 2000,
        "speed_range": [0.5, 2.0],
        "compression": {
            "format": audio_compress.speech_format(),
            "ffmpeg_available": bool(audio_compress.ffmpeg_path()),
        },
        "notice": notice,
        "browser_fallback": source is None,
    }


def _asr_info(user_id: int) -> dict:
    candidates = asr_providers.asr_candidates(user_id)
    if candidates:
        first = candidates[0]
        return {
            "available": True,
            "source": "provider",
            "provider": first["provider"].get("name") or "",
            "model": first["model"],
            "max_seconds": None,
            "notice": "音频会上传到已配置的语音识别连接；本机不保存录音。",
        }
    if speech_local.asr_available():
        return {
            "available": True,
            "source": "local",
            "provider": "本地语音",
            "model": speech_local.asr_model_label(),
            "max_seconds": speech_local.max_asr_seconds(),
            "notice": "本地识别只接受 16k 单声道 PCM16 WAV；录音只在本机处理。",
        }
    return {
        "available": False,
        "source": None,
        "provider": None,
        "model": None,
        "max_seconds": speech_local.max_asr_seconds(),
        "notice": "语音识别不可用：本地模型未就绪，且没有配置云端语音识别连接。",
    }


@router.get("/info")
def speech_info(user=Depends(current_user)):
    """跨场景朗读/跟读能力探测（不触发模型加载，不返回密钥与目录）。"""
    return {
        "tts": _tts_info(user["id"]),
        "asr": _asr_info(user["id"]),
        "limits": {
            "max_tts_chars": speech_local.MAX_TTS_CHARS,
            "max_expected_chars": MAX_EXPECTED_CHARS,
            "max_audio_bytes": MAX_ASR_BYTES,
        },
    }


@router.post("/tts")
async def synthesize_speech(data: TtsInput, user=Depends(current_user)):
    """通用朗读：返回音频字节；没有服务端 TTS 时 503（前端回退浏览器朗读）。"""
    text = str(data.text or "").strip()
    if not text:
        raise HTTPException(422, "朗读文本为空")
    if len(text) > speech_local.MAX_TTS_CHARS:
        raise HTTPException(422, "单次朗读文本不能超过 %d 字" % speech_local.MAX_TTS_CHARS)
    tts = _classroom_tts()
    candidate = tts.select_candidate(user["id"], data.provider_id)
    if data.provider_id and candidate is None:
        raise HTTPException(404, "未找到该语音合成连接")
    started = time.monotonic()
    if candidate is None:
        if data.model and str(data.model).strip() != speech_local.model_label():
            raise HTTPException(422, "所选语音模型未配置可用的合成连接")
        if not speech_local.tts_available():
            raise HTTPException(503, "本地语音合成不可用；请改用浏览器朗读或配置语音合成连接")
        voice = speech_local.normalize_voice(data.voice)
        speed = max(0.5, min(2.0, float(data.speed))) if data.speed is not None else 1.0
        try:
            audio, mime, audio_format = await asyncio.wait_for(
                asyncio.to_thread(_local_synthesize_cached, text, voice, speed),
                timeout=speech_local.SYNTH_TIMEOUT_SECONDS,
            )
        except speech_local.SpeechUnavailable as exc:
            raise HTTPException(503, str(exc)) from exc
        except Exception as exc:  # noqa: BLE001 - 本地引擎异常统一中文降级
            raise HTTPException(502, "本地语音合成失败（%s）" % type(exc).__name__) from exc
        source = "local"
        model = speech_local.model_label()
    else:
        provider = candidate["provider"]
        model = str(data.model or candidate["model"])
        voice = tts.normalize_voice_for(provider, str(data.voice).strip() if data.voice else None)
        speed = max(0.5, min(2.0, float(data.speed))) if data.speed is not None else 1.0
        try:
            audio, mime = await tts.synthesize_provider(
                provider, model, text, voice=voice, speed=speed,
            )
        except Exception as exc:  # noqa: BLE001 - 上游异常统一可读失败
            raise HTTPException(502, "语音合成服务暂不可用（%s）" % type(exc).__name__) from exc
        audio_format = (mime or "").split("/")[-1].split(";")[0].strip() or "unknown"
        source = "provider"
    safe_voice = re.sub(r"[^\x20-\x7e]", "", str(voice or ""))[:60]
    headers = {
        "Cache-Control": "private, max-age=3600",
        "X-Speech-Source": source,
        "X-Speech-Format": audio_format,
        "X-Speech-Elapsed-Ms": str(int((time.monotonic() - started) * 1000)),
    }
    if safe_voice:
        headers["X-Speech-Voice"] = safe_voice
    return Response(content=audio, media_type=mime or "application/octet-stream", headers=headers)


async def _transcribe_audio(
    *, user_id: int, payload: bytes, mime: str,
    provider_id: int | None = None, model: str | None = None,
) -> dict:
    candidates = asr_providers.asr_candidates(user_id)
    local_selector = (
        (not provider_id or int(provider_id) == 0)
        and (not model or str(model).strip() == speech_local.asr_model_label())
    )
    if local_selector and not candidates and speech_local.asr_available():
        try:
            result = await asyncio.wait_for(
                asyncio.to_thread(speech_local.transcribe, payload),
                timeout=speech_local.TRANSCRIBE_TIMEOUT_SECONDS,
            )
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(502, "本地语音识别失败（%s）" % type(exc).__name__) from exc
        return {
            "text": str(result.get("text") or "").strip(),
            "model": speech_local.asr_model_label(),
            "source": "local",
            "usage": None,
        }
    if not candidates:
        raise HTTPException(
            503,
            "语音识别不可用：本地模型未就绪，且没有配置云端语音识别连接",
        )
    try:
        candidate = asr_providers.resolve_asr_candidate(user_id, provider_id, model)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    from .ai import audio as ai_audio

    try:
        result = await ai_audio.transcribe(
            candidate["provider"], candidate["model"], payload, mime,
        )
    except ValueError as exc:
        raise HTTPException(502, "语音识别失败：%s" % exc) from exc
    except Exception as exc:  # noqa: BLE001 - 上游异常统一可读失败
        raise HTTPException(502, "语音识别服务暂不可用（%s）" % type(exc).__name__) from exc
    return {
        "text": str(result.get("text") or "").strip(),
        "model": result.get("model") or candidate["model"],
        "source": "provider",
        "usage": result.get("usage") or {},
    }


def _read_audio_or_422(payload: bytes, mime: str) -> None:
    if not payload:
        raise HTTPException(422, "录音文件为空")
    if len(payload) > MAX_ASR_BYTES:
        raise HTTPException(413, "录音文件不能超过 25MB")
    if mime and not mime.startswith("audio/") and mime != "application/octet-stream":
        raise HTTPException(422, "请上传音频文件（WAV / webm / mp3 / m4a）")


@router.post("/transcribe")
async def transcribe_speech(
    file: UploadFile = File(...),
    provider_id: int | None = Form(None),
    model: str | None = Form(None),
    user=Depends(current_user),
):
    """通用语音转写：本地模型优先，其次已配置的云端 ASR；不保存录音。"""
    payload = await file.read(MAX_ASR_BYTES + 1)
    mime = (file.content_type or "").split(";")[0].strip().lower() or "audio/wav"
    _read_audio_or_422(payload, mime)
    result = await _transcribe_audio(
        user_id=user["id"], payload=payload, mime=mime,
        provider_id=provider_id, model=model,
    )
    return {**result, "filename": file.filename}


@router.post("/shadowing/check")
async def shadowing_check(
    expected: str = Form(..., min_length=1, max_length=MAX_EXPECTED_CHARS),
    file: UploadFile = File(...),
    provider_id: int | None = Form(None),
    model: str | None = Form(None),
    user=Depends(current_user),
):
    """跟读检查：识别转写后只给文本相似度与可重试反馈，不评价发音。"""
    payload = await file.read(MAX_ASR_BYTES + 1)
    mime = (file.content_type or "").split(";")[0].strip().lower() or "audio/wav"
    _read_audio_or_422(payload, mime)
    result = await _transcribe_audio(
        user_id=user["id"], payload=payload, mime=mime,
        provider_id=provider_id, model=model,
    )
    transcript = str(result.get("text") or "").strip()[:MAX_TRANSCRIPT_CHARS]
    scored = score_reading(expected, transcript)
    return {
        "similar": scored["similar"],
        "score": scored["score"],
        "ratio": scored["ratio"],
        "transcript": transcript,
        "expected": expected,
        "reason": scored["reason"],
        "note": scored["note"],
        "source": result["source"],
        "model": result["model"],
        "usage": result.get("usage"),
    }
