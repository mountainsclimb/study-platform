"""合成音频压缩（阶段 4-4）：ffmpeg 可用时转码，失败/缺失一律回退 WAV。"""
from __future__ import annotations

from app.ai import audio_compress


def _wav() -> bytes:
    return b"RIFF\x00\x00\x00\x00WAVEfmt "


def test_without_ffmpeg_keeps_wav(monkeypatch):
    monkeypatch.setattr(audio_compress, "ffmpeg_path", lambda: None)
    audio, mime, fmt = audio_compress.compress_speech(_wav())
    assert audio == _wav()
    assert mime == "audio/wav"
    assert fmt == "wav"


def test_compresses_wav_to_mp3(monkeypatch):
    monkeypatch.delenv("STUDY_TTS_FORMAT", raising=False)
    monkeypatch.setattr(audio_compress, "ffmpeg_path", lambda: "ffmpeg")
    monkeypatch.setattr(
        audio_compress, "_run_ffmpeg",
        lambda executable, args, payload: b"ID3-mp3-bytes",
    )
    audio, mime, fmt = audio_compress.compress_speech(_wav())
    assert audio == b"ID3-mp3-bytes"
    assert mime == "audio/mpeg"
    assert fmt == "mp3"


def test_opus_format_and_failure_fallback(monkeypatch):
    monkeypatch.setenv("STUDY_TTS_FORMAT", "opus")
    monkeypatch.setattr(audio_compress, "ffmpeg_path", lambda: "ffmpeg")
    monkeypatch.setattr(
        audio_compress, "_run_ffmpeg",
        lambda executable, args, payload: b"OggS-opus",
    )
    audio, mime, fmt = audio_compress.compress_speech(_wav())
    assert audio == b"OggS-opus" and mime == "audio/ogg" and fmt == "opus"

    monkeypatch.setattr(audio_compress, "_run_ffmpeg", lambda *args: None)
    audio, mime, fmt = audio_compress.compress_speech(_wav())
    assert audio == _wav() and mime == "audio/wav" and fmt == "wav"


def test_wav_switch_and_non_wav_passthrough(monkeypatch):
    monkeypatch.setenv("STUDY_TTS_FORMAT", "wav")
    monkeypatch.setattr(audio_compress, "ffmpeg_path", lambda: "ffmpeg")
    audio, mime, fmt = audio_compress.compress_speech(_wav())
    assert audio == _wav() and fmt == "wav"

    monkeypatch.delenv("STUDY_TTS_FORMAT", raising=False)
    audio, mime, fmt = audio_compress.compress_speech(b"MP3", "audio/mpeg")
    assert audio == b"MP3" and mime == "audio/mpeg" and fmt == "mpeg"


def test_status_reports_format_and_binary(monkeypatch):
    monkeypatch.setenv("STUDY_TTS_FORMAT", "mp3")
    monkeypatch.setattr(audio_compress, "ffmpeg_path", lambda: "/usr/bin/ffmpeg")
    status = audio_compress.status()
    assert status["format"] == "mp3"
    assert status["available"] is True
    assert status["ffmpeg"] == "/usr/bin/ffmpeg"

    monkeypatch.setattr(audio_compress, "ffmpeg_path", lambda: None)
    assert audio_compress.status()["available"] is False
