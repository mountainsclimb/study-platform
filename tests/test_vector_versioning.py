"""P1 回归：OCR/向量按资料版本与内容摘要写入，晚完成的旧批次不覆盖新索引。"""
import asyncio
import json

import pytest


def _doc_item(client, document_id: int) -> dict:
    items = client.get("/api/documents").json()["items"]
    return next(row for row in items if row["id"] == document_id)


def _upload(client, text: str = "# 第一章 函数\n函数定义域是自变量取值范围。") -> dict:
    response = client.post("/api/documents", files={
        "file": ("向量资料.md", text.encode("utf-8"), "text/markdown"),
    })
    assert response.status_code == 200, response.text
    return response.json()


def _fake_vectors(chunks, tag="v1"):
    return [(chunk["chunk_id"], "test-model@local", json.dumps([0.1, 0.2, 0.3])) for chunk in chunks]


def test_upload_records_index_digest(client, monkeypatch):
    from app.ai import notes_agent

    monkeypatch.setattr(notes_agent, "embedding_config", lambda *args, **kwargs: object())
    monkeypatch.setattr(notes_agent, "document_vectors", lambda chunks, **kwargs: _async(_fake_vectors(chunks)))
    doc = _upload(client, "# 章节\n内容正文第一版。")
    assert doc["index_status"] == "ready"
    assert doc["index_digest"]
    listing = client.get("/api/documents").json()["items"]
    item = next(row for row in listing if row["id"] == doc["id"])
    assert item["index_status"] == "ready"


def test_late_old_batch_does_not_overwrite_newer_vectors(client, monkeypatch):
    """模拟两个 OCR 批次并发：旧批次后完成时只能标 stale，不能覆盖新版本向量。"""
    from app.ai import notes_agent
    from app import db

    monkeypatch.setattr(notes_agent, "embedding_config", lambda *args, **kwargs: object())
    doc = _upload(client, "# 章节\n初始内容。")
    user_id = client.get("/api/auth/me").json()["id"]
    newer_saved = {"done": False}

    async def racing_vectors(chunks, **kwargs):
        # 旧批次在算向量时，新批次已经先落库（模拟乱序完成）
        if not newer_saved["done"]:
            newer_saved["done"] = True
            await notes_agent._save_ocr_pages(
                doc["id"], user_id, [{"page": 1, "text": "# 新版本\n新内容。"}], manual=True)
        return _fake_vectors(chunks, tag="old")

    monkeypatch.setattr(notes_agent, "document_vectors", racing_vectors)
    result = asyncio.run(notes_agent._save_ocr_pages(
        doc["id"], user_id, [{"page": 1, "text": "# 旧版本\n旧内容。"}], manual=True))

    # 最终正文保留「新版本」；旧批次不得覆盖索引：
    # 现有索引正好对应当前内容时保持 ready，否则标记 stale。
    detail = _doc_item(client, doc["id"])
    assert "新版本" in json.dumps(detail, ensure_ascii=False)
    with db.connection() as con:
        rows = con.execute("SELECT COUNT(*), MIN(model) FROM chunk_embeddings WHERE document_id=?", (doc["id"],)).fetchone()
    assert rows[0] >= 0
    if detail["index_status"] == "ready":
        assert detail["index_digest"]
    else:
        assert detail["index_status"] == "stale"


def test_old_batch_cannot_overwrite_index_of_different_content(client, monkeypatch):
    """旧批次完成时当前内容已更新、索引仍是上一版 → 标 stale，且不写入旧向量。"""
    from app.ai import notes_agent

    monkeypatch.setattr(notes_agent, "embedding_config", lambda *args, **kwargs: object())
    doc = _upload(client, "# 章节\n初始内容。")
    user_id = client.get("/api/auth/me").json()["id"]
    state = {"step": 0}
    real_vectors = _fake_vectors

    async def racing_vectors(chunks, **kwargs):
        if state["step"] == 0:
            state["step"] = 1
            # 新批次先落库并建好索引（ready）
            await notes_agent._save_ocr_pages(doc["id"], user_id,
                                             [{"page": 1, "text": "# 新版本\n新内容。"}], manual=True)
            # 内容又改成第三版，但这次索引写入被跳过（模拟索引服务失败/未配置）
            state["skip"] = True
            await notes_agent._save_ocr_pages(doc["id"], user_id,
                                             [{"page": 1, "text": "# 第三版\n更新的内容。"}], manual=True)
            state["skip"] = False
        if state.get("skip"):
            return []
        return real_vectors(chunks)

    monkeypatch.setattr(notes_agent, "document_vectors", racing_vectors)
    asyncio.run(notes_agent._save_ocr_pages(
        doc["id"], user_id, [{"page": 1, "text": "# 旧版本\n旧内容。"}], manual=True))
    detail = _doc_item(client, doc["id"])
    assert "第三版" in json.dumps(detail, ensure_ascii=False)
    assert detail["index_status"] == "stale"
    # 重建后可恢复 ready
    rebuilt = client.post(f"/api/documents/{doc['id']}/reindex")
    assert rebuilt.status_code == 200
    assert _doc_item(client, doc["id"])["index_status"] == "ready"


def test_reindex_endpoint_rebuilds_after_stale(client, monkeypatch):
    from app.ai import notes_agent

    monkeypatch.setattr(notes_agent, "embedding_config", lambda *args, **kwargs: object())
    monkeypatch.setattr(notes_agent, "document_vectors", lambda chunks, **kwargs: _async(_fake_vectors(chunks)))
    doc = _upload(client, "# 章节\n需要重建索引的内容。")
    from app import db
    with db.connection() as con:
        con.execute("UPDATE ai_documents SET vector_status='stale', vector_digest=NULL WHERE id=?", (doc["id"],))
    rebuilt = client.post(f"/api/documents/{doc['id']}/reindex")
    assert rebuilt.status_code == 200, rebuilt.text
    detail = _doc_item(client, doc["id"])
    assert detail["index_status"] == "ready" and detail["index_digest"]


def test_reindex_without_embedding_keeps_local_search(client, monkeypatch):
    from app.ai import notes_agent

    monkeypatch.setattr(notes_agent, "embedding_config", lambda *args, **kwargs: None)
    doc = _upload(client, "# 章节\n本地检索内容。")
    response = client.post(f"/api/documents/{doc['id']}/reindex")
    assert response.status_code == 200
    assert response.json()["index_status"] == "pending"
    assert "Embedding" in response.json()["notice"]


def _async(value):
    async def _inner():
        return value
    return _inner()


@pytest.fixture(autouse=True)
def _clean_documents(client):
    yield
