"""真实本地语音模型 smoke（阶段 4-5）：默认跳过，部署方预置模型后自动生效。

需要同时满足：
* ``STUDY_LOCAL_SPEECH=on`` 且 ``STUDY_LOCAL_SPEECH_DIR`` 下有可用 TTS/ASR 模型；
* sherpa-onnx 已安装（``pip install -r requirements-oss.txt``）。
缺少任一条件都 skip，不把「假引擎通过」当作真实可用。
"""
from __future__ import annotations

import pytest

from app.ai import speech_local


def _speech_ready() -> bool:
    return bool(speech_local.tts_available() and speech_local.asr_available())


@pytest.mark.skipif(not _speech_ready(), reason="本地 TTS/ASR 模型未预置或未开启")
def test_real_tts_to_asr_round_trip():
    wav, mime = speech_local.synthesize("知序语音自检。", speed=1.0)
    assert wav and mime == "audio/wav"
    result = speech_local.transcribe(wav)
    assert isinstance(result.get("text"), str)


@pytest.mark.skipif(not speech_local.tts_available(), reason="本地 TTS 模型未预置或未开启")
def test_real_tts_returns_24k_or_16k_pcm_wav():
    wav, mime = speech_local.synthesize("语音压缩自检。", speed=1.0)
    assert mime == "audio/wav"
    assert wav[:4] == b"RIFF"
    assert len(wav) > 1000
