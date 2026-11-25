"""P1-1：引擎重置的跨进程语义（世代号传播）。"""
import pytest

from app import engine_api
from app.ai import engine_reset


@pytest.fixture
def fresh_generation(isolated_db):
    from app import db

    db.init_db()
    engine_reset.forget_local_state()
    yield
    engine_reset.forget_local_state()


def test_mark_reset_writes_generation_and_resets_local(isolated_db, fresh_generation, monkeypatch):
    calls = []
    monkeypatch.setattr(engine_reset, "_reset_local", lambda engine: calls.append(engine))
    marked = engine_reset.mark_reset("speech")
    assert marked["engine"] == "speech"
    assert marked["generation"]
    assert calls == ["speech"]
    assert engine_reset.generation("speech") == marked["generation"]
    # 世代号落在 app_settings，跨进程可见。
    assert _stored("speech") == marked["generation"]


def test_apply_pending_follows_other_process_reset(isolated_db, fresh_generation, monkeypatch):
    calls = []
    monkeypatch.setattr(engine_reset, "_reset_local", lambda engine: calls.append(engine))
    # 模拟「API 进程发起重置」：直接写 DB，本进程（Worker）尚未应用。
    _write_stored("formula", "2026-01-01T00:00:00+00:00")
    assert calls == []
    reset = engine_reset.apply_pending()
    assert reset == ["formula"]
    assert calls == ["formula"]
    assert engine_reset.generation("formula") == "2026-01-01T00:00:00+00:00"
    # 世代号未变 → 不重复重置。
    assert engine_reset.apply_pending() == []
    assert calls == ["formula"]


def test_apply_pending_picks_up_newer_generation(isolated_db, fresh_generation, monkeypatch):
    calls = []
    monkeypatch.setattr(engine_reset, "_reset_local", lambda engine: calls.append(engine))
    _write_stored("table", "2026-01-01T00:00:00+00:00")
    engine_reset.apply_pending()
    _write_stored("table", "2026-01-02T00:00:00+00:00")
    assert engine_reset.apply_pending() == ["table"]
    assert calls == ["table", "table"]
    assert engine_reset.generation("table") == "2026-01-02T00:00:00+00:00"


def test_apply_pending_survives_db_failure(isolated_db, fresh_generation, monkeypatch):
    def boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(engine_reset, "_stored_generations", boom)
    assert engine_reset.apply_pending() == []


def test_unknown_engine_rejected(isolated_db, fresh_generation):
    with pytest.raises(ValueError):
        engine_reset.mark_reset("chem")


def test_reset_endpoint_reports_scope(client):
    response = client.post("/api/engines/speech/reset")
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["reset"]["engine"] == "speech"
    assert payload["reset"]["scope"] == "api-process-immediately,worker-on-next-task"
    assert "Worker" in payload["reset"]["note"]


def test_reset_endpoint_crosses_process_boundary(client):
    """API 重置后，模拟的 Worker 进程（清空进程内记录）能读到并应用世代号。"""
    engine_reset.forget_local_state()
    assert client.post("/api/engines/embeddings/reset").status_code == 200
    stamp = _stored("embeddings")
    assert stamp
    calls = []

    from app.ai import engine_reset as fresh

    fresh.forget_local_state()
    original = fresh._reset_local
    fresh._reset_local = lambda engine: calls.append(engine)
    try:
        assert fresh.apply_pending() == ["embeddings"]
    finally:
        fresh._reset_local = original
    assert calls == ["embeddings"]
    assert fresh.generation("embeddings") == stamp


def _stored(engine: str) -> str:
    from app import db

    return db.get_setting("engine.reset." + engine, "") or ""


def _write_stored(engine: str, value: str) -> None:
    from app import db

    db.set_setting("engine.reset." + engine, value)
