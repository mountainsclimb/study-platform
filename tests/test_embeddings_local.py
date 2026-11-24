"""本地向量（fastembed，app/ai/embeddings_local.py）与 Embedding 配置验收。

全程假引擎，不下载模型。覆盖方案 §5：
* 未安装 / 关闭两条路径：``embedding_config()`` 为 None，``document_vectors()`` 为空；
* 本地模式：配置来源 local、存储标签与 HTTP 模型不同，``_request_embeddings()`` 从未调用；
* 模型目录缺失：状态如实为不可用；
* 测试接口：本地模式返回维度/耗时；
* ``/providers/embeddings-config`` 的 local_available / mode=local 保存。
"""
import asyncio
import json

import pytest

from app import db
from app.ai import embeddings, embeddings_local


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    embeddings_local.reset_engine()
    for name in (
        "STUDY_EMBEDDING_MODE", "STUDY_RERANK", "STUDY_EMBEDDING_BASE_URL",
        "STUDY_EMBEDDING_MODEL", "STUDY_EMBEDDING_API_KEY",
        "STUDY_LOCAL_EMBEDDING_DIR", "STUDY_LOCAL_RERANK_DIR",
    ):
        monkeypatch.delenv(name, raising=False)
    yield
    embeddings_local.reset_engine()


class FakeEmbedEngine:
    def __init__(self, dimension=2):
        self.dimension = dimension
        self.calls = []

    def embed(self, texts):
        self.calls.append(list(texts))
        return [[1.0, 0.0, 0.0][: self.dimension] for _ in texts]


class FakeRerankEngine:
    def __init__(self, scores=None):
        self.scores = scores or [0.9, 0.1]
        self.calls = []

    def rerank(self, query, documents):
        self.calls.append((query, list(documents)))
        return self.scores[: len(documents)]


def install_local(monkeypatch, root, *, dimension=2):
    embed_dir = root / "models" / "embeddings"
    rerank_dir = root / "models" / "rerank"
    embed_dir.mkdir(parents=True, exist_ok=True)
    rerank_dir.mkdir(parents=True, exist_ok=True)
    for directory, model_file in ((embed_dir, "model_optimized.onnx"), (rerank_dir, "model.onnx")):
        (directory / model_file).write_bytes(b"fake")
        (directory / "config.json").write_text("{}", encoding="utf-8")
        (directory / "tokenizer.json").write_text("{}", encoding="utf-8")
    engine = FakeEmbedEngine(dimension)
    rerank_engine = FakeRerankEngine()
    monkeypatch.setenv("STUDY_EMBEDDING_MODE", "local")
    monkeypatch.setenv("STUDY_LOCAL_EMBEDDING_DIR", str(embed_dir))
    monkeypatch.setenv("STUDY_LOCAL_RERANK_DIR", str(rerank_dir))
    monkeypatch.setattr(embeddings_local, "_module_available", lambda: True)
    monkeypatch.setattr(embeddings_local, "_get_engine", lambda: engine)
    monkeypatch.setattr(embeddings_local, "_get_rerank_engine", lambda: rerank_engine)
    return engine, rerank_engine


def test_off_and_uninstalled_paths_return_none(monkeypatch):
    assert embeddings.embedding_config() is None
    assert embeddings_local.local_available() is False
    assert asyncio.run(embeddings.document_vectors([{"chunk_id": "c1", "text": "内容"}])) == []

    monkeypatch.setenv("STUDY_EMBEDDING_MODE", "local")
    monkeypatch.setattr(embeddings_local, "_module_available", lambda: False)
    assert embeddings.embedding_config() is None
    status = embeddings_local.engine_status()
    assert status["mode"] == "local" and status["available"] is False
    assert "fastembed" in status["notice"]


def test_model_dir_rejects_unrelated_onnx(tmp_path):
    directory = tmp_path / "embeddings"
    directory.mkdir()
    (directory / "unrelated.onnx").write_bytes(b"fake")
    (directory / "config.json").write_text("{}", encoding="utf-8")
    (directory / "tokenizer.json").write_text("{}", encoding="utf-8")
    resolved, missing = embeddings_local.resolve_model_dir(
        directory, embeddings_local.embedding_spec()
    )
    assert resolved is None
    assert missing == ["model_optimized.onnx", "config.json", "tokenizer.json"]

    (directory / "model_optimized.onnx").write_bytes(b"fake")
    resolved, missing = embeddings_local.resolve_model_dir(
        directory, embeddings_local.embedding_spec()
    )
    assert resolved is not None
    assert resolved.root == directory.resolve()
    assert resolved.model_file == "model_optimized.onnx"
    assert missing == []


def test_model_dir_reports_each_missing_companion_file(tmp_path):
    directory = tmp_path / "embeddings"
    directory.mkdir()
    (directory / "model_optimized.onnx").write_bytes(b"fake")
    resolved, missing = embeddings_local.resolve_model_dir(
        directory, embeddings_local.embedding_spec()
    )
    assert resolved is None
    assert missing == ["config.json", "tokenizer.json"]


def test_hf_cache_snapshot_layout_resolves(tmp_path):
    snapshot = (
        tmp_path / "embeddings" / "models--Qdrant--bge-small-zh-v1.5"
        / "snapshots" / "abc123"
    )
    snapshot.mkdir(parents=True)
    (snapshot / "model_optimized.onnx").write_bytes(b"fake")
    (snapshot / "config.json").write_text("{}", encoding="utf-8")
    (snapshot / "tokenizer.json").write_text("{}", encoding="utf-8")
    resolved, missing = embeddings_local.resolve_model_dir(
        tmp_path / "embeddings", embeddings_local.embedding_spec()
    )
    assert resolved is not None
    assert resolved.root == snapshot.resolve()
    assert missing == []


def test_model_name_subdirectory_resolves(tmp_path):
    root = tmp_path / "embeddings" / "bge-small-zh-v1.5"
    root.mkdir(parents=True)
    (root / "model_optimized.onnx").write_bytes(b"fake")
    (root / "config.json").write_text("{}", encoding="utf-8")
    (root / "tokenizer.json").write_text("{}", encoding="utf-8")
    resolved, _ = embeddings_local.resolve_model_dir(
        tmp_path / "embeddings", embeddings_local.embedding_spec()
    )
    assert resolved is not None and resolved.root == root.resolve()


def test_engine_kwargs_uses_specific_model_path_when_supported(tmp_path):
    class Modern:
        def __init__(self, model_name=None, specific_model_path=None, threads=1):
            pass

    class Legacy:
        def __init__(self, model_name=None, cache_dir=None, local_files_only=False, threads=1):
            pass

    resolved = embeddings_local.ResolvedModel(tmp_path, "model_optimized.onnx")
    modern = embeddings_local._engine_kwargs(Modern, "BAAI/bge-small-zh-v1.5", resolved)
    assert modern["specific_model_path"] == str(tmp_path)
    assert "cache_dir" not in modern
    legacy = embeddings_local._engine_kwargs(Legacy, "BAAI/bge-small-zh-v1.5", resolved)
    assert legacy["cache_dir"] == str(tmp_path)
    assert legacy["local_files_only"] is True


def test_engine_error_names_missing_files(monkeypatch, tmp_path):
    monkeypatch.setenv("STUDY_EMBEDDING_MODE", "local")
    monkeypatch.setattr(embeddings_local, "_module_available", lambda: True)
    monkeypatch.setattr(embeddings_local, "embedding_dir", lambda: tmp_path / "missing")
    with pytest.raises(embeddings_local.LocalEmbeddingUnavailable) as excinfo:
        embeddings_local.embed(["内容"])
    message = str(excinfo.value)
    assert "model_optimized.onnx" in message
    assert "Qdrant/bge-small-zh-v1.5" in message


def test_model_dir_missing_marks_unavailable(monkeypatch, tmp_path):
    monkeypatch.setenv("STUDY_EMBEDDING_MODE", "local")
    monkeypatch.setattr(embeddings_local, "_module_available", lambda: True)
    monkeypatch.setattr(embeddings_local, "embedding_dir", lambda: tmp_path / "missing")
    assert embeddings_local.local_available() is False
    assert embeddings.embedding_config() is None
    with pytest.raises(embeddings_local.LocalEmbeddingUnavailable):
        embeddings_local.embed(["内容"])


def test_local_config_and_vectors_never_call_http(monkeypatch, tmp_path):
    engine, _ = install_local(monkeypatch, tmp_path)

    async def unexpected(*args, **kwargs):
        pytest.fail("本地模式不得调用 HTTP embedding")

    monkeypatch.setattr(embeddings, "_request_embeddings", unexpected)
    config = embeddings.embedding_config()
    assert config is not None
    assert config.source == "local"
    assert config.endpoint == "local://" + embeddings_local.EMBEDDING_MODEL_NAME
    assert config.storage_model.startswith(embeddings_local.EMBEDDING_MODEL_NAME + "@")

    chunks = [
        {"chunk_id": "c1", "text": "函数定义域", "heading_path": "第一章"},
        {"chunk_id": "c2", "text": "概率初步", "heading_path": "第二章"},
    ]
    vectors = asyncio.run(embeddings.document_vectors(chunks))
    assert [item[0] for item in vectors] == ["c1", "c2"]
    assert all(item[1] == config.storage_model for item in vectors)
    assert all(json.loads(item[2]) == [1.0, 0.0] for item in vectors)
    assert engine.calls == [["第一章\n函数定义域", "第二章\n概率初步"]]
    assert embeddings_local.dimension() == 2


def test_local_vector_label_differs_from_http_model(monkeypatch, tmp_path):
    local_engine, _ = install_local(monkeypatch, tmp_path)
    local = embeddings.embedding_config()
    assert local is not None

    monkeypatch.delenv("STUDY_EMBEDDING_MODE")
    monkeypatch.setenv("STUDY_EMBEDDING_BASE_URL", "https://vectors.example.test/v1")
    monkeypatch.setenv("STUDY_EMBEDDING_MODEL", "text-embedding-test")
    http = embeddings.embedding_config()
    assert http is not None and http.source == "env"
    assert local.storage_model != http.storage_model
    assert local_engine.calls == []


def test_test_embedding_config_uses_local_engine(monkeypatch, tmp_path):
    install_local(monkeypatch, tmp_path)
    result = asyncio.run(embeddings.test_embedding_config(1, mode="local"))
    assert result["ok"] is True
    assert result["dimension"] == 2
    assert result["source"] == "local"
    assert result["endpoint"].startswith("local://")
    assert result["latency_ms"] >= 0


def test_embedding_status_reports_local(monkeypatch, tmp_path):
    install_local(monkeypatch, tmp_path)
    status = embeddings.embedding_status()
    assert status["configured"] is True
    assert status["source"] == "local"
    assert status["local_available"] is True
    assert status["endpoint"].startswith("local://")
    assert status["detail"] == "本地语义检索已配置"


def test_api_saves_and_reports_local_mode(client, monkeypatch):
    engine, _ = install_local(monkeypatch, db.DATA_DIR)

    response = client.put("/api/providers/embeddings-config", json={"mode": "local"})
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["mode"] == "local"
    assert payload["local_available"] is True
    assert payload["effective_source"] == "local"
    assert payload["effective"]["ready"] is True
    assert payload["effective"]["endpoint"].startswith("local://")
    assert payload["local_model"] == embeddings_local.EMBEDDING_MODEL_NAME

    fetched = client.get("/api/providers/embeddings-config").json()
    assert fetched["mode"] == "local"
    assert fetched["local_available"] is True

    tested = client.post("/api/providers/embeddings-test", json={"mode": "local"})
    assert tested.status_code == 200, tested.text
    assert tested.json()["ok"] is True
    assert engine.calls


def test_account_local_mode_works_without_env(client, monkeypatch):
    """账号级 local 模式不依赖 STUDY_EMBEDDING_MODE（环境变量只决定默认回退）。"""
    engine, _ = install_local(monkeypatch, db.DATA_DIR)
    monkeypatch.delenv("STUDY_EMBEDDING_MODE")
    assert embeddings.embedding_config() is None  # 尚未选择账号模式

    response = client.put("/api/providers/embeddings-config", json={"mode": "local"})
    assert response.status_code == 200, response.text
    assert response.json()["effective_source"] == "local"
    assert response.json()["local_available"] is True
    user_id = client.get("/api/auth/me").json()["id"]
    assert embeddings.embedding_config(user_id).source == "local"

    tested = client.post("/api/providers/embeddings-test", json={"mode": "local"})
    assert tested.status_code == 200, tested.text
    assert tested.json()["ok"] is True
    assert engine.calls


def test_api_rejects_local_when_unavailable(client, monkeypatch):
    monkeypatch.setenv("STUDY_EMBEDDING_MODE", "local")
    monkeypatch.setattr(embeddings_local, "_module_available", lambda: False)
    response = client.put("/api/providers/embeddings-config", json={"mode": "local"})
    assert response.status_code == 422
    assert "本地向量" in response.json()["detail"]


def test_upload_with_local_vectors_stores_chunks(client, monkeypatch):
    install_local(monkeypatch, db.DATA_DIR)
    response = client.post("/api/documents", files={
        "file": ("本地资料.txt", "函数定义域与值域".encode(), "text/plain"),
    })
    assert response.status_code == 200, response.text
    document = response.json()
    with db.connection() as con:
        rows = con.execute(
            "SELECT chunk_id, model FROM chunk_embeddings WHERE document_id=?",
            (document["id"],),
        ).fetchall()
    assert rows
    assert all(row["model"].startswith(embeddings_local.EMBEDDING_MODEL_NAME + "@") for row in rows)
