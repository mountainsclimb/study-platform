"""可选引擎启动预热（app/ai/warmup.py）验收。"""
import threading

import pytest

from app.ai import embeddings_local, formula_ocr, speech_local, table_structure, warmup


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name, _ in warmup.SWITCHES:
        monkeypatch.delenv(name, raising=False)
    embeddings_local.reset_engine()
    formula_ocr.reset_engine()
    table_structure.reset_engine()
    speech_local.reset_engine()
    yield
    embeddings_local.reset_engine()
    formula_ocr.reset_engine()
    table_structure.reset_engine()
    speech_local.reset_engine()


def test_warmup_all_collects_success_and_failure(monkeypatch):
    def broken():
        raise RuntimeError("模拟预热失败")

    monkeypatch.setattr(
        warmup, "_targets",
        lambda: (("ok", lambda: {"loaded": True}), ("broken", broken)),
    )
    results = warmup.warmup_all()
    assert results["ok"]["loaded"] is True
    assert "RuntimeError" in results["broken"]["error"]


def test_start_background_warmup_skips_when_all_switches_off():
    assert warmup.any_enabled() is False
    assert warmup.start_background_warmup() is None


def test_start_background_warmup_runs_once(monkeypatch):
    monkeypatch.setenv("STUDY_TABLE_OCR", "on")
    called = threading.Event()

    def target():
        called.set()
        return {"loaded": True}

    monkeypatch.setattr(warmup, "_targets", lambda: (("fake", target),))
    thread = warmup.start_background_warmup()
    assert thread is not None
    thread.join(timeout=2.0)
    assert called.is_set()


def test_engine_warmups_skip_when_switches_off():
    for result in (
        embeddings_local.warmup(),
        formula_ocr.warmup(),
        table_structure.warmup(),
        speech_local.warmup(),
    ):
        assert result.get("loaded") is not True
        assert result.get("skipped")
