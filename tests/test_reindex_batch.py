"""资料向量批量重建（POST /api/documents/reindex-batch）。"""
import json


def _upload(client, text: str, name: str = "批量向量.md") -> dict:
    response = client.post("/api/documents", files={
        "file": (name, text.encode("utf-8"), "text/markdown"),
    })
    assert response.status_code == 200, response.text
    return response.json()


def _fake_vectors(chunks):
    return [
        (chunk["chunk_id"], "test-model@local", json.dumps([0.1, 0.2, 0.3]))
        for chunk in chunks
    ]


def _patch_embeddings(monkeypatch):
    from app.ai import notes_agent

    monkeypatch.setattr(notes_agent, "embedding_config", lambda *args, **kwargs: object())

    async def vectors(chunks, **kwargs):
        return _fake_vectors(chunks)

    monkeypatch.setattr(notes_agent, "document_vectors", vectors)


def test_batch_rebuilds_only_stale_documents(client, monkeypatch):
    from app import db

    _patch_embeddings(monkeypatch)
    first = _upload(client, "# 第一章\n函数定义域。", "一.md")
    second = _upload(client, "# 第二章\n概率初步。", "二.md")
    with db.connection() as con:
        con.execute(
            "UPDATE ai_documents SET vector_status='stale', vector_digest=NULL WHERE id=?",
            (first["id"],),
        )

    response = client.post("/api/documents/reindex-batch", json={"mode": "stale"})
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["attempted"] == 1
    assert payload["ready"] == 1
    assert payload["failed"] == []

    items = {item["id"]: item for item in client.get("/api/documents").json()["items"]}
    assert items[first["id"]]["index_status"] == "ready"
    assert items[second["id"]]["index_status"] == "ready"


def test_batch_all_and_document_filter(client, monkeypatch):
    _patch_embeddings(monkeypatch)
    first = _upload(client, "# 甲\n内容甲。", "甲.md")
    second = _upload(client, "# 乙\n内容乙。", "乙.md")

    response = client.post("/api/documents/reindex-batch", json={
        "mode": "all", "document_ids": [second["id"]],
    })
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["attempted"] == 1
    assert payload["ready"] == 1
    assert first["id"] != second["id"]


def test_batch_rejects_unknown_document(client, monkeypatch):
    _patch_embeddings(monkeypatch)
    response = client.post("/api/documents/reindex-batch", json={
        "mode": "all", "document_ids": [999999],
    })
    assert response.status_code == 404
    assert "999999" in response.json()["detail"]


def test_batch_without_embedding_skips(client, monkeypatch):
    from app.ai import notes_agent

    monkeypatch.setattr(notes_agent, "embedding_config", lambda *args, **kwargs: None)
    _upload(client, "# 本地\n本地检索内容。", "本地.md")
    response = client.post("/api/documents/reindex-batch", json={"mode": "all"})
    assert response.status_code == 200
    payload = response.json()
    assert payload["attempted"] == 0
    assert payload["skipped"] >= 1
    assert "Embedding" in payload["notice"]


def test_batch_rejects_unknown_mode(client, monkeypatch):
    _patch_embeddings(monkeypatch)
    response = client.post("/api/documents/reindex-batch", json={"mode": "everything"})
    assert response.status_code == 422
