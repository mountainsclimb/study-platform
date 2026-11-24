"""合成音频压缩：本地 TTS 的 WAV 优先转 MP3/Opus，ffmpeg 不可用时保留 WAV。

方案阶段 4-4：课堂旁白、词汇听写、跟读反馈等所有本地合成音频都经过这里，缓存键由
调用方带上格式与音色；**任何失败都不影响主流程**，调用方拿到的仍是可播放音频。

* 开关：``STUDY_TTS_FORMAT=mp3|opus|wav``（默认 ``mp3``）；
* 可执行文件：``STUDY_FFMPEG`` 显式指定，否则查 ``PATH`` 中的 ``ffmpeg``；
* 输入只接受 ``audio/wav``，其它 MIME 原样返回（厂商 TTS 已是压缩格式）；
* 输出失败/超时/空数据一律回退原始 WAV，并返回 ``"wav"``，不伪造成功。
"""
from __future__ import annotations

import os
import shutil
import subprocess
from typing import Final

__all__ = [
    "DEFAULT_FORMAT",
    "FFMPEG_TIMEOUT_SECONDS",
    "FORMATS",
    "compress_speech",
    "ffmpeg_path",
    "speech_format",
    "status",
]

DEFAULT_FORMAT: Final = "mp3"
FFMPEG_TIMEOUT_SECONDS: Final = 30
_FORMAT_ARGS: Final[dict[str, tuple[str, list[str]]]] = {
    "mp3": ("audio/mpeg", ["-f", "mp3", "-codec:a", "libmp3lame",
                           "-b:a", "64k", "-ac", "1", "-ar", "24000"]),
    "opus": ("audio/ogg", ["-f", "ogg", "-codec:a", "libopus",
                           "-b:a", "32k", "-ac", "1", "-ar", "24000"]),
}
FORMATS: Final = frozenset({"mp3", "opus", "wav"})


def speech_format() -> str:
    """目标格式；非法值回退 ``DEFAULT_FORMAT``，``wav`` 表示不压缩。"""
    raw = str(os.environ.get("STUDY_TTS_FORMAT", "") or "").strip().lower()
    if not raw:
        return DEFAULT_FORMAT
    return raw if raw in FORMATS else DEFAULT_FORMAT


def ffmpeg_path() -> str | None:
    """ffmpeg 可执行文件：优先 ``STUDY_FFMPEG``，否则查 ``PATH``；没有返回 None。"""
    explicit = str(os.environ.get("STUDY_FFMPEG", "") or "").strip()
    if explicit:
        return explicit
    return shutil.which("ffmpeg")


def _run_ffmpeg(executable: str, args: list[str], payload: bytes) -> bytes | None:
    """同步调用 ffmpeg（stdin → stdout）；失败/超时/空输出返回 None，由调用方回退。"""
    command = [executable, "-hide_banner", "-loglevel", "error", "-i", "pipe:0",
               *args, "pipe:1"]
    try:
        completed = subprocess.run(
            command,
            input=payload,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=FFMPEG_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0 or not completed.stdout:
        return None
    return completed.stdout


def compress_speech(audio: bytes, mime: str = "audio/wav", *, fmt: str | None = None) -> tuple[bytes, str, str]:
    """压缩语音音频，返回 ``(数据, MIME, 实际格式)``。

    ``fmt`` 未指定时读 ``STUDY_TTS_FORMAT``；只对 WAV 输入生效。任何失败都返回原始
    WAV、``audio/wav``、``"wav"``，绝不返回不可播放的空数据。
    """
    target = str(fmt or speech_format()).strip().lower()
    if target not in FORMATS:
        target = DEFAULT_FORMAT
    content_type = str(mime or "").split(";")[0].strip().lower()
    if target == "wav" or not audio:
        return audio, content_type or "audio/wav", "wav"
    if content_type != "audio/wav":
        suffix = content_type.split("/")[-1].split("+")[0] or "original"
        return audio, content_type, suffix
    executable = ffmpeg_path()
    if not executable:
        return audio, "audio/wav", "wav"
    output_mime, args = _FORMAT_ARGS[target]
    converted = _run_ffmpeg(executable, args, audio)
    if not converted:
        return audio, "audio/wav", "wav"
    return converted, output_mime, target


def status() -> dict:
    """给设置页/环境自检用的只读状态（不执行 ffmpeg）。"""
    return {
        "format": speech_format(),
        "ffmpeg": ffmpeg_path(),
        "available": bool(ffmpeg_path()) and speech_format() != "wav",
        "timeout_seconds": FFMPEG_TIMEOUT_SECONDS,
    }
