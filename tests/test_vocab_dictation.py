"""词汇听写验收（批次六后续）：编辑距离评分、TTS/ASR 降级、卡片隔离与答案揭示。"""
from __future__ import annotations

import pytest

from app import vocab_dictation
from app.ai import speech_local


def _create_card(client, word="abandon"):
    response = client.post("/api/vocab/cards", json={"word": word})
    assert response.status_code == 200, response.text
    return response.json()["card"]


def test_dictation_scoring_rules():
    exact = vocab_dictation.score_attempt("abandon", "Abandon.")
    assert exact["correct"] is True and exact["score"] == 100
    assert exact["distance"] == 0

    one_edit = vocab_dictation.score_attempt("abandon", "abandan")
    assert one_edit["distance"] == 1
    assert one_edit["score"] >= 80 and one_edit["correct"] is True

    short_wrong = vocab_dictation.score_attempt("cat", "cut")
    assert short_wrong["correct"] is False and short_wrong["score"] < 80

    spaced = vocab_dictation.score_attempt("ice cream", "icecream")
    assert spaced["correct"] is True and spaced["score"] == 100

    empty = vocab_dictation.score_attempt("abandon", "")
    assert empty["correct"] is False and empty["score"] == 0


def test_dictation_status_degrades_when_speech_unavailable(client, monkeypatch):
    monkeypatch.setattr(vocab_dictation.speech_local, "tts_available", lambda: False)
    monkeypatch.setattr(vocab_dictation.speech_local, "asr_available", lambda: False)
    body = client.get("/api/vocab/dictation/status").json()
    assert body["available"] is False
    assert body["tts_available"] is False and body["asr_available"] is False
    assert body["note"]
    assert client.get("/api/vocab/dictation/next").status_code == 503


def test_dictation_next_and_answer(client, monkeypatch):
    card = _create_card(client, "abandon")
    monkeypatch.setattr(vocab_dictation.speech_local, "tts_available", lambda: True)
    monkeypatch.setattr(vocab_dictation.speech_local, "asr_available", lambda: True)
    monkeypatch.setattr(
        vocab_dictation.speech_local, "synthesize",
        lambda *args, **kwargs: (b"RIFF-test-wav", "audio/wav"),
    )
    monkeypatch.setattr(
        vocab_dictation.speech_local, "transcribe",
        lambda payload: {"text": "abandon", "words": []},
    )
    vocab_dictation._synthesize_word.cache_clear()

    item = client.get("/api/vocab/dictation/next").json()
    assert item["available"] is True and item["done"] is False
    assert item["card_id"] == card["card_id"]
    assert item["audio"].startswith("data:audio/wav;base64,")
    assert "word" not in item  # 不把答案单词返回给前端
    assert item["meaning"]

    answer = client.post(
        "/api/vocab/dictation/answer",
        data={"card_id": str(card["card_id"])},
        files={"file": ("answer.wav", b"RIFF-test-wav", "audio/wav")},
    )
    assert answer.status_code == 200, answer.text
    body = answer.json()
    assert body["correct"] is True
    assert body["score"] == 100
    assert body["expected"] == "abandon"
    assert body["transcript"] == "abandon"
    assert body["translation"]

    exhausted = client.get(
        "/api/vocab/dictation/next", params={"exclude": str(card["card_id"])},
    ).json()
    assert exhausted["done"] is True


def test_dictation_requires_ownership_and_asr(client, second_client, monkeypatch):
    card = _create_card(client)
    monkeypatch.setattr(vocab_dictation.speech_local, "tts_available", lambda: True)
    monkeypatch.setattr(vocab_dictation.speech_local, "asr_available", lambda: True)
    other = second_client.post(
        "/api/vocab/dictation/answer",
        data={"card_id": str(card["card_id"])},
        files={"file": ("answer.wav", b"RIFF", "audio/wav")},
    )
    assert other.status_code == 404

    monkeypatch.setattr(vocab_dictation.speech_local, "asr_available", lambda: False)
    blocked = client.post(
        "/api/vocab/dictation/answer",
        data={"card_id": str(card["card_id"])},
        files={"file": ("answer.wav", b"RIFF", "audio/wav")},
    )
    assert blocked.status_code == 503


def test_dictation_rejects_bad_audio(client, monkeypatch):
    card = _create_card(client)
    monkeypatch.setattr(vocab_dictation.speech_local, "asr_available", lambda: True)

    empty = client.post(
        "/api/vocab/dictation/answer",
        data={"card_id": str(card["card_id"])},
        files={"file": ("answer.wav", b"", "audio/wav")},
    )
    assert empty.status_code == 422

    non_audio = client.post(
        "/api/vocab/dictation/answer",
        data={"card_id": str(card["card_id"])},
        files={"file": ("answer.txt", b"hello", "text/plain")},
    )
    assert non_audio.status_code == 422
