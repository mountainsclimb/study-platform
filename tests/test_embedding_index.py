import asyncio
import json

import httpx
import pytest

from app import db
from app.ai import embeddings


@pytest.fixture
def embedding_provider(monkeypatch):
    monkeypatch.setenv("STUDY_EMBEDDING_BASE_URL", "https://vectors.example.test/v1/")
    monkeypatch.setenv("STUDY_EMBEDDING_MODEL", "embedding-test")
    monkeypatch.setenv("STUDY_EMBEDDING_API_KEY", "mock-key")
    async_client = httpx.AsyncClient

    def mock(handler):
        transport = httpx.MockTransport(handler)
        monkeypatch.setattr(embeddings.httpx, "AsyncClient", lambda **kwargs: async_client(transport=transport, **kwargs))

    return mock


def _upload(client, text):
    response = client.post("/api/documents", files={"file": ("学习资料.txt", text.encode(), "text/plain")})
    assert response.status_code == 200, response.text
    return response.json()


def _vectors_for(document_id):
    with db.connection() as con:
        return con.execute(
            "SELECT chunk_id, model, vector_json FROM chunk_embeddings WHERE document_id=? ORDER BY chunk_id",
            (document_id,),
        ).fetchall()


def test_upload_batches_embedding_and_uses_same_model_for_query(client, monkeypatch, embedding_provider):
    requests = []

    def respond(request):
        assert str(request.url) == "https://vectors.example.test/v1/embeddings"
        assert request.headers["authorization"] == "Bearer mock-key"
        payload = json.loads(request.content)
        assert payload["model"] == "embedding-test" and payload["encoding_format"] == "float"
        texts = payload["input"]
        requests.append(texts)
        data = [
            {"index": i, "embedding": [0, 1] if text.startswith(("条目8", "语义查询")) else [1, 0]}
            for i, text in enumerate(texts)
        ]
        return httpx.Response(200, json={"data": data[::-1]})

    embedding_provider(respond)
    doc = _upload(client, "\n".join(f"条目{i} 学习资料" + "内容" * 600 for i in range(9)))
    assert doc["chunks"] == 9
    assert [len(batch) for batch in requests] == [8, 1]
    assert client.get("/api/documents").json()["embedding_enabled"] is True
    saved = _vectors_for(doc["id"])
    assert [(row["chunk_id"], json.loads(row["vector_json"])) for row in saved] == [
        (f"p1-c{i + 1}", [0, 1] if i == 8 else [1, 0]) for i in range(9)
    ]
    with db.connection() as con:
        raw_chunks = con.execute("SELECT chunks_json FROM ai_documents WHERE id=?", (doc["id"],)).fetchone()[0]
    chunks = [{"document_id": doc["id"], **chunk} for chunk in json.loads(raw_chunks)]

    matches = asyncio.run(embeddings.retrieve_document_chunks(chunks, "语义查询", 1))
    assert matches[0]["chunk_id"] == "p1-c9"
    assert requests[-1] == ["语义查询"]

    monkeypatch.setenv("STUDY_EMBEDDING_BASE_URL", "https://alternative.example.test/v1")
    matches = asyncio.run(embeddings.retrieve_document_chunks(chunks, "语义查询", 1))
    assert matches[0]["chunk_id"] == "p1-c1"
    assert len(requests) == 3


def test_ocr_replacement_clears_previous_vectors_on_provider_failure(client, second_client, embedding_provider):
    requests = []

    def respond(request):
        texts = json.loads(request.content)["input"]
        requests.extend(texts)
        if texts[0] == "供应商故障":
            return httpx.Response(429)
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [1, 0]}]})

    embedding_provider(respond)
    doc = _upload(client, "原始资料")
    path = f"/api/documents/{doc['id']}"
    assert len(_vectors_for(doc["id"])) == 1
    correction = {"pages": [{"page": 1, "text": "供应商故障"}]}
    assert second_client.patch(f"{path}/ocr-text", json=correction).status_code == 404
    assert requests == ["原始资料"]
    edited = client.patch(f"{path}/ocr-text", json=correction)
    assert edited.status_code == 200
    assert edited.json()["text_excerpt"] == "供应商故障"
    assert not _vectors_for(doc["id"])

    assert client.patch(f"{path}/ocr-text", json={"pages": [{"page": 1, "text": "校对后资料"}]}).status_code == 200
    assert len(_vectors_for(doc["id"])) == 1
    assert client.delete(path).status_code == 200
    assert not _vectors_for(doc["id"])


def test_upload_keeps_document_when_embedding_batches_have_different_dimensions(client, embedding_provider):
    calls = []

    def respond(request):
        texts = json.loads(request.content)["input"]
        calls.append(texts)
        vector = [1, 0] if len(calls) == 1 else [1, 0, 0]
        return httpx.Response(200, json={"data": [{"index": i, "embedding": vector} for i in range(len(texts))]})

    embedding_provider(respond)
    doc = _upload(client, "学习资料" * 4200)
    assert doc["status"] == "ready"
    assert [len(batch) for batch in calls] == [8, 1]
    assert not _vectors_for(doc["id"])


def test_upload_default_does_not_send_embedding_requests(client, monkeypatch):
    monkeypatch.delenv("STUDY_EMBEDDING_BASE_URL", raising=False)
    monkeypatch.delenv("STUDY_EMBEDDING_MODEL", raising=False)

    async def unexpected(*args):
        pytest.fail("unconfigured uploads must stay local")

    monkeypatch.setattr(embeddings, "_request_embeddings", unexpected)
    doc = _upload(client, "本地资料")
    assert doc["status"] == "ready"
    assert client.get("/api/documents").json()["embedding_enabled"] is False
    assert not _vectors_for(doc["id"])
