"""本地重排薄封装（§5）：开关判断、异常吞掉、绝不改变原排序结构。

调用方：
* ``search_vector.rank_chunks_hybrid(..., rerank=rerank.rerank)``：混合得分前 20 条送重排；
* ``practice.search_similar_questions()``：候选题干送重排，条目增加 ``rerank_score``。

重排在 ``STUDY_RERANK=on`` 且 fastembed/模型目录可用时生效；任何失败由调用方捕获后
沿用原排序，不产生 500。
"""
from __future__ import annotations

from . import embeddings_local

__all__ = ["available", "rerank", "score_texts", "MAX_TEXTS", "MAX_TEXT_CHARS"]

MAX_TEXTS = 20
MAX_TEXT_CHARS = 512


def available() -> bool:
    return embeddings_local.rerank_available()


def rerank(query: str, texts: list[str]) -> list[float]:
    """同步重排；异常向上抛，由调用方决定回退（本函数不吞异常）。"""
    values = [str(text or "")[:MAX_TEXT_CHARS] for text in (texts or [])]
    if not values:
        return []
    return embeddings_local.rerank(str(query or "")[:MAX_TEXT_CHARS], values)


def score_texts(query: str, texts: list[str]) -> list[float]:
    """带开关与长度限制的便捷入口：不可用时返回空列表（调用方据此跳过）。"""
    if not available():
        return []
    return rerank(query, list(texts or [])[:MAX_TEXTS])
