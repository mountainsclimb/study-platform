"""本地重排（app/ai/rerank.py + search_vector + 相似题）验收。

覆盖方案 §5：
* ``rank_chunks_hybrid`` 启用重排后的顺序符合 0.3 混合 + 0.7 重排；
* 返回结构与未启用时一致（不附带得分）；
* 重排抛错时结果与未启用时逐项相同；
* ``search_similar_questions()`` 条目带 ``rerank_score`` 且 ``similarity`` 不变；
* 开关关闭时 ``rerank.score_texts`` 返回空列表。
"""
import asyncio
import json

import pytest

from app import db, practice
from app.ai import embeddings_local, rerank
from app.search_vector import rank_chunks_hybrid
from bank_fixture import make_bank, question_row


def _clear_bank_caches():
    practice._sqlite_question_columns.cache_clear()
    practice._sqlite_group_columns.cache_clear()
    practice._source_groups.cache_clear()


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    embeddings_local.reset_engine()
    for name in ("STUDY_RERANK", "STUDY_EMBEDDING_MODE", "STUDY_LOCAL_RERANK_DIR"):
        monkeypatch.delenv(name, raising=False)
    yield
    embeddings_local.reset_engine()


def chunks():
    return [
        {"chunk_id": "c1", "document_id": 1, "heading_path": "甲", "text": "苹果 香蕉 水果"},
        {"chunk_id": "c2", "document_id": 1, "heading_path": "乙", "text": "苹果 苹果 苹果"},
        {"chunk_id": "c3", "document_id": 1, "heading_path": "丙", "text": "汽车 火车 飞机"},
        {"chunk_id": "c4", "document_id": 1, "heading_path": "丁", "text": "苹果 梨 桃"},
    ]


def test_rank_chunks_with_rerank_reorders_without_changing_shape(monkeypatch):
    base = rank_chunks_hybrid(chunks(), "苹果", top_k=2)
    calls = []

    def fake_rerank(query, texts):
        calls.append((query, list(texts)))
        return [1.0 if "汽车" in text else 0.0 for text in texts]

    reranked = rank_chunks_hybrid(chunks(), "苹果", top_k=2, rerank=fake_rerank)
    assert [chunk["chunk_id"] for chunk in reranked][0] == "c3"
    assert {key for chunk in reranked for key in chunk} == {
        key for chunk in base for key in chunk
    }
    assert all("rerank_score" not in chunk for chunk in reranked)
    assert calls and calls[0][0] == "苹果"
    assert len(calls[0][1]) == 4
    assert all(len(text) <= 512 for text in calls[0][1])


def test_rerank_failure_keeps_original_order():
    base = rank_chunks_hybrid(chunks(), "苹果", top_k=2)

    def broken(query, texts):
        raise RuntimeError("模拟重排失败")

    fallback = rank_chunks_hybrid(chunks(), "苹果", top_k=2, rerank=broken)
    assert [chunk["chunk_id"] for chunk in fallback] == [
        chunk["chunk_id"] for chunk in base
    ]


def test_rerank_is_skipped_when_all_scores_equal():
    base = rank_chunks_hybrid(chunks(), "苹果", top_k=2)
    same = rank_chunks_hybrid(chunks(), "苹果", top_k=2, rerank=lambda q, t: [0.5] * len(t))
    # 归一化后全部 0.5：顺序回到混合得分原序（第三块原本最低）。
    assert [chunk["chunk_id"] for chunk in same] == [
        chunk["chunk_id"] for chunk in base
    ]


def test_score_texts_returns_empty_when_disabled(monkeypatch):
    assert rerank.available() is False
    assert rerank.score_texts("苹果", ["苹果", "香蕉"]) == []


def test_score_texts_uses_local_engine(monkeypatch, tmp_path):
    directory = tmp_path / "rerank"
    directory.mkdir()
    (directory / "model.onnx").write_bytes(b"fake")
    monkeypatch.setenv("STUDY_RERANK", "on")
    monkeypatch.setattr(embeddings_local, "_module_available", lambda: True)
    monkeypatch.setattr(embeddings_local, "rerank_dir", lambda: directory)
    monkeypatch.setattr(
        embeddings_local, "_get_rerank_engine",
        lambda: type("Fake", (), {"rerank": lambda self, q, docs: [0.2 for _ in docs]})(),
    )
    assert rerank.available() is True
    assert rerank.score_texts("苹果", ["苹果", "香蕉"]) == [0.2, 0.2]


@pytest.fixture
def indexed_bank(tmp_path, monkeypatch):
    rows = [
        question_row("docR:1", "函数单调性的判断方法"),
        question_row("docR:2", "函数单调性的判断方法是什么"),
        question_row("docR:3", "集合运算与概率初步"),
    ]
    bank = make_bank(tmp_path / "bank", rows)
    monkeypatch.setattr(db, "BANK_DIR", bank)
    _clear_bank_caches()
    yield bank
    _clear_bank_caches()


def test_search_similar_questions_adds_rerank_score_and_keeps_similarity(
    client, indexed_bank, monkeypatch,
):
    client.get("/api/questions", params={"q": "函数"})  # 触发索引构建
    baseline = practice.search_similar_questions("函数单调性的判断方法", limit=5)
    assert baseline
    baseline_scores = {item["qid"]: item["similarity"] for item in baseline}

    monkeypatch.setattr(rerank, "available", lambda: True)
    monkeypatch.setattr(
        rerank, "rerank",
        lambda query, texts: [0.01 * index for index in range(len(texts))],
    )
    result = practice.search_similar_questions("函数单调性的判断方法", limit=5)
    assert result
    assert all("rerank_score" in item for item in result)
    assert all(isinstance(item["rerank_score"], float) for item in result)
    for item in result:
        assert item["similarity"] == baseline_scores.get(item["qid"], item["similarity"])
        assert item["exact_match"] is (item["similarity"] >= 0.65)
    # 重排得分升序：最后一条应排在最前。
    scores = [item["rerank_score"] for item in result]
    assert scores == sorted(scores, reverse=True)


def test_search_similar_questions_rerank_failure_keeps_order(
    client, indexed_bank, monkeypatch,
):
    client.get("/api/questions", params={"q": "函数"})
    baseline = practice.search_similar_questions("函数单调性的判断方法", limit=5)
    monkeypatch.setattr(rerank, "available", lambda: True)

    def broken(query, texts):
        raise RuntimeError("模拟失败")

    monkeypatch.setattr(rerank, "rerank", broken)
    result = practice.search_similar_questions("函数单调性的判断方法", limit=5)
    assert [item["qid"] for item in result] == [item["qid"] for item in baseline]
    assert all("rerank_score" not in item for item in result)
