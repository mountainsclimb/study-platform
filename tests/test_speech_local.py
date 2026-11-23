"""离线语音模块（app/ai/speech_local.py）验收。

全程假引擎，不下载模型。覆盖方案 §4：
* 未安装 / 关闭 / 模型目录缺失：available 为假，合成/识别抛 SpeechUnavailable；
* 假引擎合成返回 WAV；非 WAV、非 16k、非单声道、超长输入被拒；
* 初始化失败可 reset 重试（状态缓存清理）；
* 状态字段与音色列表。
"""
from io import BytesIO
import math
import wave

import pytest

from app.ai import speech_local


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    speech_local.reset_engine()
    for name in (
        "STUDY_LOCAL_SPEECH", "STUDY_LOCAL_SPEECH_DIR", "STUDY_LOCAL_TTS_MODEL",
        "STUDY_LOCAL_ASR_MODEL", "STUDY_LOCAL_TTS_VOICE",
        "STUDY_LOCAL_ASR_MAX_SECONDS",
    ):
        monkeypatch.delenv(name, raising=False)
    yield
    speech_local.reset_engine()


class FakeAudio:
    def __init__(self, samples=None, sample_rate=16000):
        self.samples = [0.1, -0.1, 0.2] if samples is None else samples
        self.sample_rate = sample_rate


class FakeTts:
    num_speakers = 3

    def __init__(self):
        self.calls = []

    def generate(self, text, sid=0, speed=1.0):
        self.calls.append((text, int(sid), float(speed)))
        return FakeAudio()


class FakeResult:
    text = "你好，这是一段测试"


class FakeStream:
    result = FakeResult()

    def accept_waveform(self, rate, samples):
        self.rate = rate
        self.samples = samples


class FakeRecognizer:
    def __init__(self):
        self.stream = FakeStream()
        self.decoded = []

    def create_stream(self):
        return self.stream

    def decode_stream(self, stream):
        self.decoded = True


def fake_recognizer():
    return FakeRecognizer()


def wav_bytes(*, rate=16000, channels=1, width=2, seconds=0.5, value=0.1):
    count = max(1, int(rate * seconds))
    pcm = b"".join(
        int(max(-1, min(1, value)) * 32767).to_bytes(2, "little", signed=True)
        for _ in range(count)
    )
    buffer = BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(width)
        handle.setframerate(rate)
        handle.writeframes(pcm * channels)
    return buffer.getvalue()


def install_speech(monkeypatch, root):
    tts_dir = root / "kokoro-multi-lang-v1_1"
    asr_dir = root / "paraformer-zh"
    tts_dir.mkdir(parents=True, exist_ok=True)
    asr_dir.mkdir(parents=True, exist_ok=True)
    for name in ("model.onnx", "tokens.txt", "voices.bin"):
        (tts_dir / name).write_bytes(b"fake")
    for name in ("model.onnx", "tokens.txt"):
        (asr_dir / name).write_bytes(b"fake")
    tts = FakeTts()
    asr = fake_recognizer()
    monkeypatch.setenv("STUDY_LOCAL_SPEECH", "on")
    monkeypatch.setenv("STUDY_LOCAL_SPEECH_DIR", str(root))
    monkeypatch.setattr(speech_local, "_module_available", lambda: True)
    monkeypatch.setattr(speech_local, "_get_tts_engine", lambda: tts)
    monkeypatch.setattr(speech_local, "_get_asr_engine", lambda: asr)
    return tts, asr


def test_off_and_uninstalled_paths_are_unavailable(monkeypatch):
    assert speech_local.tts_available() is False
    assert speech_local.asr_available() is False
    with pytest.raises(speech_local.SpeechUnavailable):
        speech_local.synthesize("旁白")
    with pytest.raises(speech_local.SpeechUnavailable):
        speech_local.transcribe(wav_bytes())

    monkeypatch.setenv("STUDY_LOCAL_SPEECH", "on")
    monkeypatch.setattr(speech_local, "_module_available", lambda: False)
    assert speech_local.tts_available() is False
    assert speech_local.asr_available() is False
    status = speech_local.engine_status()
    assert status["mode"] == "on" and status["available"] is False
    assert "sherpa-onnx" in status["notice"]


def test_model_directory_missing_marks_unavailable(monkeypatch, tmp_path):
    monkeypatch.setenv("STUDY_LOCAL_SPEECH", "on")
    monkeypatch.setattr(speech_local, "_module_available", lambda: True)
    monkeypatch.setenv("STUDY_LOCAL_SPEECH_DIR", str(tmp_path / "missing"))
    assert speech_local.tts_available() is False
    assert speech_local.asr_available() is False


def test_synthesize_returns_16k_wav_and_voices(monkeypatch, tmp_path):
    tts, _ = install_speech(monkeypatch, tmp_path)
    audio, mime = speech_local.synthesize("第一句旁白。", voice="1", speed=1.2)
    assert mime == "audio/wav"
    with wave.open(BytesIO(audio), "rb") as handle:
        assert handle.getnchannels() == 1
        assert handle.getsampwidth() == 2
        assert handle.getframerate() == 16000
        assert handle.getnframes() == 3
    assert tts.calls[-1][0] == "第一句旁白。"
    assert tts.calls[-1][1] == 1
    assert math.isclose(tts.calls[-1][2], 1.2)
    voices = speech_local.list_voices()
    assert [item["voice_id"] for item in voices] == ["0", "1", "2"]
    assert voices[0]["default"] is True


def test_transcribe_accepts_valid_wav(monkeypatch, tmp_path):
    _, asr = install_speech(monkeypatch, tmp_path)
    result = speech_local.transcribe(wav_bytes(seconds=0.2))
    assert result["text"] == "你好，这是一段测试"
    assert result["words"] == []
    assert asr.stream.rate == 16000
    assert len(asr.stream.samples) == 3200


@pytest.mark.parametrize("kwargs", [
    {"rate": 8000},
    {"channels": 2},
    {"width": 1},
])
def test_transcribe_rejects_non_16k_mono_pcm16(monkeypatch, tmp_path, kwargs):
    install_speech(monkeypatch, tmp_path)
    with pytest.raises(ValueError) as error:
        speech_local.transcribe(wav_bytes(**kwargs))
    assert "16" in str(error.value) or "单声道" in str(error.value) or "PCM" in str(error.value)


def test_transcribe_rejects_non_wav_and_overlong(monkeypatch, tmp_path):
    install_speech(monkeypatch, tmp_path)
    with pytest.raises(ValueError):
        speech_local.transcribe(b"not a wav")

    monkeypatch.setenv("STUDY_LOCAL_ASR_MAX_SECONDS", "1")
    with pytest.raises(ValueError) as error:
        speech_local.transcribe(wav_bytes(seconds=2))
    assert "最长" in str(error.value)


def test_max_asr_seconds_is_bounded(monkeypatch):
    assert speech_local.max_asr_seconds() == 60
    monkeypatch.setenv("STUDY_LOCAL_ASR_MAX_SECONDS", "0")
    assert speech_local.max_asr_seconds() == 1
    monkeypatch.setenv("STUDY_LOCAL_ASR_MAX_SECONDS", "9999")
    assert speech_local.max_asr_seconds() == 600


def test_engine_status_and_reset(monkeypatch, tmp_path):
    install_speech(monkeypatch, tmp_path)
    status = speech_local.engine_status()
    assert status["tts_available"] is True
    assert status["asr_available"] is True
    assert status["tts_model_label"] == "local:kokoro-multi-lang-v1_1"
    assert status["asr_model_label"] == "local:paraformer-zh"

    monkeypatch.setattr(speech_local, "_tts_error", "模拟初始化失败")
    assert speech_local.tts_available() is False
    speech_local.reset_engine()
    assert speech_local.tts_available() is True


def test_synthesize_rejects_empty_and_overlong(monkeypatch, tmp_path):
    install_speech(monkeypatch, tmp_path)
    with pytest.raises(speech_local.SpeechError):
        speech_local.synthesize("")
    with pytest.raises(speech_local.SpeechError):
        speech_local.synthesize("长" * (speech_local.MAX_TTS_CHARS + 1))
