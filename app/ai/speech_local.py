"""离线语音（sherpa-onnx，可选依赖）：本地合成与识别，模型由部署方预置。

按《开源组件整合实施方案》§4 与 §1.1 落地：

* ``STUDY_LOCAL_SPEECH=on`` 才启用（默认关闭）；``STUDY_LOCAL_SPEECH_DIR`` 指定模型根目录
  （默认 ``<data>/models/speech``），其下按 ``STUDY_LOCAL_TTS_MODEL`` /
  ``STUDY_LOCAL_ASR_MODEL`` 放置模型目录。
* 合成用 Kokoro（默认 ``kokoro-multi-lang-v1_1``），识别用 Paraformer-zh
  （默认 ``paraformer-zh``）。权重不进仓库与镜像。
* 合成返回 16-bit PCM 单声道 WAV；识别只接受 **16k 单声道 PCM16 WAV**，超长拒绝，
  非 WAV 由调用方转 422 中文提示。
* 同步推理由调用方经 ``asyncio.to_thread`` 执行；进程级信号量限制并发。
* 模型目录/依赖缺失或初始化失败统一转 :class:`SpeechUnavailable`，调用方回退浏览器
  或厂商接口，不产生占位音频/文本。
"""
from __future__ import annotations

import importlib.util
from io import BytesIO
import os
from pathlib import Path
import threading
import time
import wave

from .. import db

__all__ = [
    "ENGINE_NAME",
    "DEFAULT_TTS_MODEL",
    "DEFAULT_ASR_MODEL",
    "MAX_TTS_CHARS",
    "SpeechError",
    "SpeechUnavailable",
    "mode",
    "speech_dir",
    "tts_model_name",
    "asr_model_name",
    "tts_model_dir",
    "asr_model_dir",
    "tts_available",
    "asr_available",
    "model_label",
    "asr_model_label",
    "normalize_voice",
    "default_voice",
    "list_voices",
    "synthesize",
    "transcribe",
    "max_asr_seconds",
    "engine_status",
    "warmup",
    "reset_engine",
    "SYNTH_TIMEOUT_SECONDS",
    "TRANSCRIBE_TIMEOUT_SECONDS",
]

ENGINE_NAME = "sherpa-onnx"
DEFAULT_TTS_MODEL = "kokoro-multi-lang-v1_1"
DEFAULT_ASR_MODEL = "paraformer-zh"
DEFAULT_ASR_MAX_SECONDS = 60
MAX_TTS_CHARS = 2000
MAX_WAV_BYTES = 25 * 1024 * 1024
SYNTH_TIMEOUT_SECONDS = 120
TRANSCRIBE_TIMEOUT_SECONDS = 120
NOTICE = (
    "离线语音由 sherpa-onnx 在本机 CPU 上运行；合成面向课堂旁白，"
    "识别只接受 16k 单声道 PCM16 WAV，结果仅供输入参考。"
)

_tts_engine = None
_tts_error: str | None = None
_asr_engine = None
_asr_error: str | None = None
_tts_lock = threading.Lock()
_asr_lock = threading.Lock()
_infer_slots = threading.BoundedSemaphore(2)


class SpeechError(RuntimeError):
    """离线语音异常基类。"""


class SpeechUnavailable(SpeechError):
    """开关关闭、依赖缺失、模型目录缺失或引擎初始化失败。"""


def mode() -> str:
    return "on" if os.environ.get("STUDY_LOCAL_SPEECH", "").strip().lower() == "on" else "off"


def speech_dir() -> Path:
    """模型根目录（调用时读取，兼容测试替换 db.DATA_DIR）。"""
    configured = os.environ.get("STUDY_LOCAL_SPEECH_DIR", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return Path(db.DATA_DIR) / "models" / "speech"


def tts_model_name() -> str:
    return os.environ.get("STUDY_LOCAL_TTS_MODEL", "").strip() or DEFAULT_TTS_MODEL


def asr_model_name() -> str:
    return os.environ.get("STUDY_LOCAL_ASR_MODEL", "").strip() or DEFAULT_ASR_MODEL


def max_asr_seconds() -> float:
    raw = os.environ.get("STUDY_LOCAL_ASR_MAX_SECONDS", "").strip()
    if not raw:
        return float(DEFAULT_ASR_MAX_SECONDS)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return float(DEFAULT_ASR_MAX_SECONDS)
    return min(600.0, max(1.0, value))


def _module_available() -> bool:
    try:
        return importlib.util.find_spec("sherpa_onnx") is not None
    except (ImportError, ValueError):
        return False


def _find_onnx(directory: Path) -> Path | None:
    for name in ("model.onnx", "model.int8.onnx"):
        candidate = directory / name
        if candidate.is_file():
            return candidate
    try:
        candidates = sorted(directory.glob("*.onnx"))
    except OSError:
        return None
    return candidates[0] if candidates else None


def _tts_files(directory: Path) -> dict | None:
    if not directory.is_dir():
        return None
    model = _find_onnx(directory)
    tokens = directory / "tokens.txt"
    voices = directory / "voices.bin"
    if model is None or not tokens.is_file() or not voices.is_file():
        return None
    lexicons = [
        path for path in (directory / "lexicon-us-en.txt", directory / "lexicon-zh.txt")
        if path.is_file()
    ]
    data_dir = directory / "espeak-ng-data"
    return {
        "model": model,
        "tokens": tokens,
        "voices": voices,
        "lexicons": lexicons,
        "data_dir": data_dir if data_dir.is_dir() else None,
    }


def _asr_files(directory: Path) -> dict | None:
    if not directory.is_dir():
        return None
    model = _find_onnx(directory)
    tokens = directory / "tokens.txt"
    if model is None or not tokens.is_file():
        return None
    return {"model": model, "tokens": tokens}


def tts_model_dir() -> Path:
    base = speech_dir()
    if _tts_files(base) is not None:
        return base
    return base / tts_model_name()


def asr_model_dir() -> Path:
    base = speech_dir()
    if _asr_files(base) is not None:
        return base
    return base / asr_model_name()


def _tts_ready() -> dict | None:
    return _tts_files(tts_model_dir())


def _asr_ready() -> dict | None:
    return _asr_files(asr_model_dir())


def tts_available() -> bool:
    if mode() != "on":
        return False
    if _tts_engine is not None:
        return True
    if _tts_error is not None:
        return False
    return _module_available() and _tts_ready() is not None


def asr_available() -> bool:
    if mode() != "on":
        return False
    if _asr_engine is not None:
        return True
    if _asr_error is not None:
        return False
    return _module_available() and _asr_ready() is not None


def model_label() -> str:
    return "local:" + tts_model_name()


def asr_model_label() -> str:
    return "local:" + asr_model_name()


def _unavailable_reason(kind: str) -> str:
    if mode() != "on":
        return "离线语音已关闭（STUDY_LOCAL_SPEECH=off）"
    if kind == "tts" and _tts_error is not None:
        return _tts_error
    if kind == "asr" and _asr_error is not None:
        return _asr_error
    return (
        "离线语音不可用：未安装 sherpa-onnx 或模型目录缺失"
        f"（{tts_model_dir() if kind == 'tts' else asr_model_dir()}）"
    )


def _get_tts_engine():
    global _tts_engine, _tts_error
    with _tts_lock:
        if _tts_engine is not None:
            return _tts_engine
        if _tts_error is not None:
            raise SpeechUnavailable(_tts_error)
        files = _tts_ready()
        if files is None:
            _tts_error = f"本地合成模型目录不可用：{tts_model_dir()}"
            raise SpeechUnavailable(_tts_error)
        try:
            import sherpa_onnx
        except Exception as error:
            _tts_error = f"本地合成依赖不可用（{type(error).__name__}）"
            raise SpeechUnavailable(_tts_error) from None
        try:
            kokoro = sherpa_onnx.OfflineTtsKokoroModelConfig(
                model=str(files["model"]),
                voices=str(files["voices"]),
                tokens=str(files["tokens"]),
                lexicon=",".join(str(path) for path in files["lexicons"]),
                data_dir=str(files["data_dir"]) if files["data_dir"] else "",
                lang="",
            )
            config = sherpa_onnx.OfflineTtsConfig(
                model=sherpa_onnx.OfflineTtsModelConfig(
                    kokoro=kokoro, num_threads=1, debug=False, provider="cpu",
                ),
                max_num_sentences=1,
                silence_scale=0.2,
            )
            engine = sherpa_onnx.OfflineTts(config)
        except Exception as error:
            _tts_error = f"本地合成引擎初始化失败（{type(error).__name__}）"
            raise SpeechUnavailable(_tts_error) from None
        _tts_engine = engine
        return _tts_engine


def _get_asr_engine():
    global _asr_engine, _asr_error
    with _asr_lock:
        if _asr_engine is not None:
            return _asr_engine
        if _asr_error is not None:
            raise SpeechUnavailable(_asr_error)
        files = _asr_ready()
        if files is None:
            _asr_error = f"本地识别模型目录不可用：{asr_model_dir()}"
            raise SpeechUnavailable(_asr_error)
        try:
            import sherpa_onnx
        except Exception as error:
            _asr_error = f"本地识别依赖不可用（{type(error).__name__}）"
            raise SpeechUnavailable(_asr_error) from None
        try:
            engine = sherpa_onnx.OfflineRecognizer.from_paraformer(
                paraformer=str(files["model"]),
                tokens=str(files["tokens"]),
                num_threads=1,
                sample_rate=16000,
                feature_dim=80,
                decoding_method="greedy_search",
                debug=False,
                provider="cpu",
            )
        except Exception as error:
            _asr_error = f"本地识别引擎初始化失败（{type(error).__name__}）"
            raise SpeechUnavailable(_asr_error) from None
        _asr_engine = engine
        return _asr_engine


def warmup() -> dict:
    """启动预热：只加载可用的合成/识别模型，不做推理。"""
    started = time.monotonic()
    base = {"engine": ENGINE_NAME, "tts_loaded": False, "asr_loaded": False}
    if mode() != "on":
        return {**base, "skipped": "STUDY_LOCAL_SPEECH 未开启", "elapsed_ms": 0}
    if not _module_available():
        return {**base, "skipped": "sherpa-onnx 未安装",
                "elapsed_ms": int((time.monotonic() - started) * 1000)}
    result = dict(base)
    if tts_available():
        try:
            _get_tts_engine()
            result["tts_loaded"] = True
        except Exception as error:
            result["tts_error"] = f"{type(error).__name__}: {error}"
    if asr_available():
        try:
            _get_asr_engine()
            result["asr_loaded"] = True
        except Exception as error:
            result["asr_error"] = f"{type(error).__name__}: {error}"
    result["elapsed_ms"] = int((time.monotonic() - started) * 1000)
    if not result["tts_loaded"] and not result["asr_loaded"]:
        result["skipped"] = _unavailable_reason("语音")
    return result


def reset_engine() -> None:
    global _tts_engine, _tts_error, _asr_engine, _asr_error
    with _tts_lock:
        _tts_engine = None
        _tts_error = None
    with _asr_lock:
        _asr_engine = None
        _asr_error = None


def default_voice() -> str:
    raw = os.environ.get("STUDY_LOCAL_TTS_VOICE", "").strip()
    return raw if raw.isdigit() else "0"


def normalize_voice(voice: str | int | None) -> str:
    """本地音色 ID 规范化：取模型首个音色作为兜底。"""
    value = str(voice if voice is not None else "").strip()
    if value.isdigit():
        return str(int(value))
    return default_voice()


def _speaker_count(engine) -> int:
    try:
        count = int(getattr(engine, "num_speakers", 0) or 0)
    except (TypeError, ValueError):
        count = 0
    return max(1, count)


def list_voices() -> list[dict]:
    """本地合成音色（加载模型读取 num_speakers；失败抛 SpeechUnavailable）。"""
    if not tts_available():
        raise SpeechUnavailable(_unavailable_reason("tts"))
    engine = _get_tts_engine()
    return [
        {
            "voice_id": str(index),
            "name": f"本地 · 音色 {index + 1}",
            "lang": "zh-CN",
            "default": index == 0,
        }
        for index in range(_speaker_count(engine))
    ]


def _wav_bytes(samples, sample_rate: int) -> bytes:
    import numpy as np

    array = np.asarray(samples, dtype=np.float32).reshape(-1)
    if array.size == 0:
        raise SpeechError("本地合成返回空音频")
    array = np.clip(array, -1.0, 1.0)
    pcm = (array * 32767.0).astype("<i2")
    buffer = BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(int(sample_rate))
        handle.writeframes(pcm.tobytes())
    return buffer.getvalue()


def synthesize(text: str, voice: str | int | None = None, speed: float | None = None) -> tuple[bytes, str]:
    """同步合成为 16-bit PCM 单声道 WAV，返回 ``(wav_bytes, "audio/wav")``。"""
    if not tts_available():
        raise SpeechUnavailable(_unavailable_reason("tts"))
    normalized = str(text or "").strip()
    if not normalized:
        raise SpeechError("旁白文本为空")
    if len(normalized) > MAX_TTS_CHARS:
        raise SpeechError(f"单次语音合成文本不能超过 {MAX_TTS_CHARS} 字")
    try:
        speed_value = max(0.5, min(2.0, float(speed))) if speed is not None else 1.0
    except (TypeError, ValueError):
        speed_value = 1.0
    with _infer_slots:
        engine = _get_tts_engine()
        sid = int(normalize_voice(voice))
        sid = min(max(0, sid), _speaker_count(engine) - 1)
        try:
            audio = engine.generate(normalized, sid=sid, speed=speed_value)
        except Exception as error:
            raise SpeechError(f"本地语音合成失败（{type(error).__name__}）") from None
    samples = getattr(audio, "samples", None)
    rate = int(getattr(audio, "sample_rate", 0) or 0)
    if samples is None or rate <= 0:
        raise SpeechError("本地合成没有返回有效音频")
    return _wav_bytes(samples, rate), "audio/wav"


def _read_wav(raw: bytes) -> tuple[object, int]:
    import numpy as np

    if not raw:
        raise ValueError("音频内容为空")
    if len(raw) > MAX_WAV_BYTES:
        raise ValueError("音频文件不能超过 25MB")
    try:
        with wave.open(BytesIO(raw), "rb") as handle:
            channels = handle.getnchannels()
            width = handle.getsampwidth()
            rate = handle.getframerate()
            frames = handle.getnframes()
            if channels != 1:
                raise ValueError("本地识别只接受单声道 WAV，请转换为 16k 单声道后重试")
            if width != 2:
                raise ValueError("本地识别只接受 16-bit PCM WAV，请转换后重试")
            if rate != 16000:
                raise ValueError("本地识别只接受 16000Hz 采样的 WAV，请重采样后重试")
            duration = frames / float(rate or 1)
            if duration > max_asr_seconds():
                raise ValueError(
                    f"本地识别单次最长 {max_asr_seconds():g} 秒，请分段录音后重试"
                )
            data = handle.readframes(frames)
    except ValueError:
        raise
    except (wave.Error, EOFError, OSError):
        raise ValueError("请上传 16k 单声道 PCM16 WAV 音频") from None
    if not data:
        raise ValueError("音频内容为空")
    samples = np.frombuffer(data, dtype="<i2").astype(np.float32) / 32768.0
    return samples, rate


def transcribe(wav_bytes: bytes) -> dict:
    """同步识别 16k 单声道 PCM16 WAV，返回 ``{"text", "words": []}``。"""
    if not asr_available():
        raise SpeechUnavailable(_unavailable_reason("asr"))
    started = time.monotonic()
    samples, rate = _read_wav(wav_bytes)
    with _infer_slots:
        engine = _get_asr_engine()
        try:
            stream = engine.create_stream()
            stream.accept_waveform(rate, samples)
            engine.decode_stream(stream)
            text = str(getattr(stream.result, "text", "") or "").strip()
        except Exception as error:
            raise SpeechError(f"本地语音识别失败（{type(error).__name__}）") from None
    transcribe.last_elapsed_ms = int((time.monotonic() - started) * 1000)
    return {"text": text, "words": []}


transcribe.last_elapsed_ms = 0


def engine_status() -> dict:
    tts_ok = tts_available()
    asr_ok = asr_available()
    if mode() != "on":
        notice = "离线语音已关闭（STUDY_LOCAL_SPEECH=off）；旁白回退浏览器朗读，语音输入回退厂商识别。"
    else:
        notice = NOTICE
        if not tts_ok:
            notice += " 本地合成不可用。"
        if not asr_ok:
            notice += " 本地识别不可用。"
    return {
        "engine": ENGINE_NAME,
        "mode": mode(),
        "available": tts_ok or asr_ok,
        "tts_available": tts_ok,
        "asr_available": asr_ok,
        "loaded": _tts_engine is not None,
        "asr_loaded": _asr_engine is not None,
        "tts_model": tts_model_name(),
        "asr_model": asr_model_name(),
        "tts_model_label": model_label(),
        "asr_model_label": asr_model_label(),
        "directories": {
            "speech": str(speech_dir()),
            "tts": str(tts_model_dir()),
            "asr": str(asr_model_dir()),
        },
        "limits": {
            "max_tts_chars": MAX_TTS_CHARS,
            "max_asr_seconds": max_asr_seconds(),
            "max_wav_bytes": MAX_WAV_BYTES,
            "concurrency": 2,
        },
        "notice": notice,
        "error": _tts_error,
        "asr_error": _asr_error,
    }
