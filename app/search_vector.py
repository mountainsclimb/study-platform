"""Hybrid lexical + char n-gram TF-IDF vector retriever with optional dense embedding cosine scoring."""
from collections import Counter
import json
import math
import re
from typing import TypeGuard

from . import db

_TOKEN_RX = re.compile(r"[\u4e00-\u9fff]+|[A-Za-z0-9_]+")


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
) -> list[dict]:
    """Rank document chunks using TF-IDF n-gram cosine similarity + heading boost (+ optional dense vectors)."""
    if not chunks or not (query_text or "").strip():
        return chunks[:top_k]
    if len(chunks) <= top_k and not query_vector:
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
