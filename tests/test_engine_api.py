"""本地引擎总览与重置（阶段 5-1）：状态形状、路径脱敏、权限与预热状态。"""
from __future__ import annotations

import json

from app.ai import embeddings_local, speech_local, warmup


def test_engine_overview_contains_all_engines_for_admin(client):
    body = client.get("/api/engines").json()
    assert body["can_reset"] is True
    assert {engine["id"] for engine in body["engines"]} == {
        "speech", "embeddings", "formula", "table", "chem",
    }
    assert body["window"]["count"] == 5
    assert body["compression"]["format"] in {"mp3", "opus", "wav"}
    assert "started" in body["warmup"]
    for engine in body["engines"]:
        assert "available" in engine and "notice" in engine
        assert "checked_at" in engine


def test_non_admin_view_redacts_paths_but_admin_view_keeps_them(
    client, second_client, monkeypatch
):
    secret = "C:\\secret\\models\\speech"
    fake_speech = {
        "engine": "sherpa-onnx",
        "mode": "on",
        "available": False,
        "loaded": False,
        "notice": f"模型目录缺失（{secret}）",
        "error": f"FileNotFoundError: {secret}",
        "directories": {"speech": secret, "tts": secret + "\\tts"},
        "model_path": secret,
        "missing_files": ["a.onnx", "b.onnx"],
        "models": ["a.onnx"],
    }
    monkeypatch.setattr(speech_local, "engine_status", lambda: dict(fake_speech))
    monkeypatch.setattr(
        embeddings_local, "engine_status",
        lambda: {"engine": "fastembed", "available": False, "notice": "未安装",
                 "directories": {"embeddings": secret}, "missing_files": []},
    )

    normal = second_client.get("/api/engines").json()
    text = json.dumps(normal, ensure_ascii=False)
    assert "secret" not in text and "C:\\" not in text
    assert normal["can_reset"] is False
    speech = next(engine for engine in normal["engines"] if engine["id"] == "speech")
    assert "directories" not in speech and "model_path" not in speech
    assert speech["missing_count"] == 2 and speech["model_count"] == 1

    admin = client.get("/api/engines").json()
    admin_speech = next(engine for engine in admin["engines"] if engine["id"] == "speech")
    assert admin_speech["directories"]["speech"] == secret
    assert admin["can_reset"] is True


def test_reset_requires_admin_and_known_engine(client, second_client, monkeypatch):
    calls = []
    monkeypatch.setattr(speech_local, "reset_engine", lambda: calls.append("speech"))
    response = client.post("/api/engines/speech/reset")
    assert response.status_code == 200, response.text
    assert calls == ["speech"]
    assert response.json()["reset"]["engine"] == "speech"

    assert second_client.post("/api/engines/speech/reset").status_code == 403
    assert client.post("/api/engines/chem/reset").status_code == 422
    assert client.post("/api/engines/nope/reset").status_code == 404


def test_warmup_state_records_background_results(monkeypatch):
    import threading

    warmup.reset_state()
    monkeypatch.setenv("STUDY_TABLE_OCR", "on")
    called = threading.Event()

    def target():
        called.set()
        return {"loaded": True, "elapsed_ms": 3}

    monkeypatch.setattr(warmup, "_targets", lambda: (("fake", target),))
    thread = warmup.start_background_warmup()
    assert thread is not None
    thread.join(timeout=2.0)
    assert called.is_set()
    state = warmup.state()
    assert state["started"] is True
    assert state["finished_at"]
    assert state["results"]["fake"]["loaded"] is True
    warmup.reset_state()
    assert warmup.state()["started"] is False
