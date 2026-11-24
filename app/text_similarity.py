"""文本相似度：优先 RapidFuzz，未安装时用标准库 difflib 兜底。

用途：题库近似查重、无向量时的相似题兜底、错题变式候选粗排。返回值统一为
``0.0–1.0``，调用方不需要知道后端是 RapidFuzz 还是 difflib。

RapidFuzz 是可选运行依赖（``requirements-search.txt``，MIT），未安装不影响主流程。
"""
from __future__ import annotations

from difflib import SequenceMatcher
from functools import lru_cache
import re
import unicodedata

__all__ = ["backend", "normalize_text", "ratio", "is_near_duplicate"]


def normalize_text(text: str) -> str:
    """NFKC、去首尾空白、折叠连续空白；不做去标点（由调用方决定）。"""
    value = unicodedata.normalize("NFKC", str(text or ""))
    return re.sub(r"\s+", " ", value).strip()


@lru_cache(maxsize=1)
def backend() -> str:
    try:
        import rapidfuzz  # noqa: F401

        return "rapidfuzz"
    except Exception:
        return "difflib"


def ratio(left: str, right: str) -> float:
    """两段文本的相似度（0–1）；空文本与任意文本相似度为 0。"""
    a = normalize_text(left)
    b = normalize_text(right)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if backend() == "rapidfuzz":
        from rapidfuzz import fuzz

        return max(0.0, min(1.0, float(fuzz.ratio(a, b)) / 100.0))
    return max(0.0, min(1.0, SequenceMatcher(None, a, b).ratio()))


def is_near_duplicate(left: str, right: str, *, threshold: float = 0.92) -> bool:
    """相似度达到阈值即视为近似重复（只用于人工复核，不自动删题）。"""
    threshold = max(0.0, min(1.0, float(threshold)))
    return ratio(left, right) >= threshold
