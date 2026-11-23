"""Hybrid lexical + char n-gram TF-IDF vector retriever with optional dense embedding cosine scoring."""
from collections import Counter
import json
import math
import re
from typing import TypeGuard

from . import db

_TOKEN_RX = re.compile(r"[\u4e00-\u9fff]+|[A-Za-z0-9_]+")

#: \u91cd\u6392\u53ea\u53d6\u6df7\u5408\u5f97\u5206\u524d N \u6761\uff0c\u63a7\u5236 CPU \u63a8\u7406\u65f6\u5ef6\uff08\u00a75\uff09\u3002
RERANK_POOL = 20
RERANK_TEXT_CHARS = 512


def _normalize_rerank_scores(scores: list[float]) -> list[float]:
    """\u91cd\u6392\u5f97\u5206\u5f52\u4e00\u5230 0\u20131\uff08min-max\uff09\uff1b\u5168\u90e8\u76f8\u7b49\u65f6\u6309 0.5 \u5904\u7406\u3002"""
    values = [float(score) for score in scores]
    if not values:
        return []
    low, high = min(values), max(values)
    if high - low <= 1e-9:
        return [0.5 for _ in values]
    return [(value - low) / (high - low) for value in values]


def _extract_features(text: str) -> Counter:
    """Extract mixed word tokens and Chinese 2/3-gram character shingles for lightweight vectorization."""
    feats: Counter = Counter()
    for seg in _TOKEN_RX.findall((text or "").lower()):
        if seg.isascii():
            if len(seg) >= 2:
                feats[seg] += 1
        else:
            n = len(seg)
            if n == 1:
                feats[seg] += 1
            else:
                for i in range(n - 1):
                    feats[seg[i:i + 2]] += 1
                for i in range(n - 2):
                    feats[seg[i:i + 3]] += 1
    return feats


def cosine_dense(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    na = math.hypot(*a)
    nb = math.hypot(*b)
    if not na or not nb:
        return 0.0
    return sum((x / na) * (y / nb) for x, y in zip(a, b))


def valid_embedding(vector: object) -> TypeGuard[list[float]]:
    if not isinstance(vector, list) or not vector or any(type(x) not in (int, float) for x in vector):
        return False
    try:
        norm = math.hypot(*vector)
        return math.isfinite(norm) and norm > 0
    except OverflowError:
        return False


def load_chunk_vectors(chunks: list[dict], model: str) -> dict[tuple[int, str], list[float]]:
    keys = {(int(c["document_id"]), c["chunk_id"]) for c in chunks if c.get("document_id") and c.get("chunk_id")}
    if not keys:
        return {}
    doc_ids = sorted({document_id for document_id, _ in keys})
    marks = ",".join("?" for _ in doc_ids)
    dense_vectors = {}
    with db.connection() as con:
        for row in con.execute(
            f"SELECT document_id, chunk_id, vector_json FROM chunk_embeddings "
            f"WHERE model=? AND document_id IN ({marks})",
            [model, *doc_ids],
        ):
            key = (row["document_id"], row["chunk_id"])
            if key not in keys:
                continue
            try:
                value = row["vector_json"]
                vec = json.loads(value) if isinstance(value, str) else value
            except ValueError:
                continue
            if valid_embedding(vec):
                dense_vectors[key] = vec
    return dense_vectors


def rank_chunks_hybrid(
    chunks: list[dict],
    query_text: str,
    *,
    top_k: int = 6,
    query_vector: list[float] | None = None,
    embedding_model: str | None = None,
    dense_vectors: dict[tuple[int, str], list[float]] | None = None,
    rerank=None,
) -> list[dict]:
    """Rank document chunks using TF-IDF n-gram cosine similarity + heading boost (+ optional dense vectors).

    ``rerank`` 为可选可调用对象 ``(query, texts) -> list[float]``：启用时取混合得分前
    ``RERANK_POOL`` 条送重排，最终按 ``0.3 × 混合得分 + 0.7 × 重排得分`` 重排；
    返回的块结构与未启用时完全一致（不附带得分）。重排失败时沿用原排序。
    """
    if not chunks or not (query_text or "").strip():
        return chunks[:top_k]
    if len(chunks) <= top_k and not query_vector and rerank is None:
        return list(chunks)

    # Build corpus IDF over provided chunks
    doc_feats = []
    df: Counter = Counter()
    for c in chunks:
        heading = c.get("heading_path") or ""
        body = c.get("text") or ""
        cf = _extract_features(body)
        # Boost heading tokens 2x
        for tok, cnt in _extract_features(heading).items():
            cf[tok] += cnt * 2
        doc_feats.append(cf)
        for tok in cf:
            df[tok] += 1

    n_docs = len(chunks)
    idf = {tok: math.log((n_docs + 1) / (freq + 1)) + 1.0 for tok, freq in df.items()}

    q_feats = _extract_features(query_text)
    q_vec = {tok: cnt * idf.get(tok, 1.0) for tok, cnt in q_feats.items()}
    q_norm = math.sqrt(sum(v * v for v in q_vec.values())) or 1.0

    if query_vector and embedding_model and dense_vectors is None:
        dense_vectors = load_chunk_vectors(chunks, embedding_model)

    scored = []
    for idx, (c, cf) in enumerate(zip(chunks, doc_feats)):
        dot = 0.0
        d_norm_sq = 0.0
        for tok, cnt in cf.items():
            w = cnt * idf.get(tok, 1.0)
            d_norm_sq += w * w
            if tok in q_vec:
                dot += w * q_vec[tok]
        sparse_sim = dot / (q_norm * (math.sqrt(d_norm_sq) or 1.0))

        dense_sim = 0.0
        if dense_vectors and query_vector and c.get("document_id") and c.get("chunk_id"):
            vec = dense_vectors.get((int(c["document_id"]), c["chunk_id"]))
            if vec:
                dense_sim = max(0.0, cosine_dense(query_vector, vec))

        final_score = (0.45 * sparse_sim + 0.55 * dense_sim) if dense_sim > 0 else sparse_sim
        scored.append((final_score, idx, c))

    scored.sort(key=lambda x: (-x[0], x[1]))
    if rerank is not None and scored:
        pool = scored[:min(RERANK_POOL, len(scored))]
        try:
            texts = [
                ("\n".join(filter(None, [c.get("heading_path"), c.get("text")])))[:RERANK_TEXT_CHARS]
                for _, _, c in pool
            ]
            scores = list(rerank(query_text, texts) or [])
        except Exception:  # 重排只是增强：失败沿用原排序
            scores = []
        if len(scores) == len(pool):
            normalized = _normalize_rerank_scores(scores)
            reranked = [
                (0.3 * max(0.0, min(1.0, base)) + 0.7 * score, idx, chunk)
                for (base, idx, chunk), score in zip(pool, normalized)
            ]
            reranked.sort(key=lambda x: (-x[0], x[1]))
            scored = reranked + scored[len(pool):]
    picked = [c for s, _, c in scored[:top_k] if s > 0]
    return picked or chunks[:top_k]


def text_vector_similarity(text_a: str, text_b: str) -> float:
    """Normalized cosine similarity on char 2/3-gram + ASCII token bags."""
    fa = _extract_features(text_a)
    fb = _extract_features(text_b)
    if not fa or not fb:
        return 0.0
    common = set(fa) & set(fb)
    dot = sum(fa[k] * fb[k] for k in common)
    na = math.sqrt(sum(v * v for v in fa.values()))
    nb = math.sqrt(sum(v * v for v in fb.values()))
    return round(dot / (na * nb), 4) if na and nb else 0.0
