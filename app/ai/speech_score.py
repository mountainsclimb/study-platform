"""本地语音的文本评分：听写拼写与跟读转写共用同一套规范化与相似度口径。

方案阶段 4-3 / 4-5：跟读只用 ASR 转写文本与原文的**文本相似度**做反馈，不做发音
评测；词汇听写沿用原来的编辑距离评分。两者都只返回可解释的分数与规范化文本，
不产生「像发音评分」的结论，也不伪造分数。
"""
from __future__ import annotations

import re
import unicodedata

from ..text_similarity import ratio

__all__ = [
    "READING_NOTE",
    "levenshtein",
    "normalize_spelling",
    "normalize_spoken",
    "score_attempt",
    "score_reading",
]

READING_NOTE = "评分只反映识别转写与原文的文本相似度，不代表发音质量；可重试后再次比较。"

_WORD_NORMALIZE_RE = re.compile(r"[^a-z0-9]+")
#: 跟读/朗读比对：保留多语言字母数字与 CJK，去掉空白和标点。
_SPOKEN_NORMALIZE_RE = re.compile(r"[^\w㐀-䶿一-鿿]+", re.UNICODE)
_WS_RE = re.compile(r"\s+")


def normalize_spelling(value: str) -> str:
    """小写并去掉空格/连字符/标点，用于听写比对（ice cream ≈ icecream）。"""
    text = str(value or "").strip().lower()
    text = text.replace("’", "'").replace("‘", "'")
    return _WORD_NORMALIZE_RE.sub("", text)


def normalize_spoken(value: str) -> str:
    """跟读比对规范化：NFKC、小写、去空白与标点，只保留文字与数字。"""
    text = unicodedata.normalize("NFKC", str(value or "")).lower()
    text = _SPOKEN_NORMALIZE_RE.sub("", text)
    return _WS_RE.sub("", text)


def levenshtein(left: str, right: str) -> int:
    """标准编辑距离（插入/删除/替换各计 1）。"""
    if left == right:
        return 0
    if not left:
        return len(right)
    if not right:
        return len(left)
    previous = list(range(len(right) + 1))
    for i, char_left in enumerate(left, start=1):
        current = [i]
        for j, char_right in enumerate(right, start=1):
            cost = 0 if char_left == char_right else 1
            current.append(min(
                previous[j] + 1,
                current[j - 1] + 1,
                previous[j - 1] + cost,
            ))
        previous = current
    return previous[-1]


def score_attempt(expected: str, transcript: str) -> dict:
    """听写评分：规范化后按编辑距离；correct 以 80 分为线，完全一致必对。

    返回 ``{correct, score, distance, expected_normalized, transcript_normalized}``；
    空转写记 0 分。评分只反映听写转写与目标拼写的接近程度，不代表发音质量。
    """
    expected_norm = normalize_spelling(expected)
    transcript_norm = normalize_spelling(transcript)
    if not expected_norm:
        return {
            "correct": False, "score": 0, "distance": 0,
            "expected_normalized": "", "transcript_normalized": transcript_norm,
            "reason": "目标单词为空",
        }
    if not transcript_norm:
        return {
            "correct": False, "score": 0, "distance": len(expected_norm),
            "expected_normalized": expected_norm, "transcript_normalized": "",
            "reason": "没有识别到文本",
        }
    distance = levenshtein(expected_norm, transcript_norm)
    longest = max(len(expected_norm), len(transcript_norm)) or 1
    score = max(0, min(100, round(100 * (1 - distance / longest))))
    return {
        "correct": distance == 0 or (score >= 80 and len(expected_norm) >= 4),
        "score": score,
        "distance": distance,
        "expected_normalized": expected_norm,
        "transcript_normalized": transcript_norm,
        "reason": "",
    }


def score_reading(expected: str, transcript: str, *, threshold: float = 0.8) -> dict:
    """跟读评分：规范化文本相似度（0–100），80 分为线；不评价发音。

    ``similar`` 只表示转写文本与原文接近，足以判断「是否大致读对」；空转写记 0 分。
    返回 ``{similar, score, ratio, expected_normalized, transcript_normalized, note}``。
    """
    expected_norm = normalize_spoken(expected)
    transcript_norm = normalize_spoken(transcript)
    threshold = max(0.0, min(1.0, float(threshold)))
    if not expected_norm:
        return {
            "similar": False, "score": 0, "ratio": 0.0,
            "expected_normalized": "", "transcript_normalized": transcript_norm,
            "reason": "原文为空", "note": READING_NOTE,
        }
    if not transcript_norm:
        return {
            "similar": False, "score": 0, "ratio": 0.0,
            "expected_normalized": expected_norm, "transcript_normalized": "",
            "reason": "没有识别到文本", "note": READING_NOTE,
        }
    matched = max(0.0, min(1.0, float(ratio(expected_norm, transcript_norm))))
    score = max(0, min(100, round(matched * 100)))
    similar = expected_norm == transcript_norm or (
        matched >= threshold and len(expected_norm) >= 4
    )
    return {
        "similar": bool(similar),
        "score": score,
        "ratio": round(matched, 4),
        "expected_normalized": expected_norm,
        "transcript_normalized": transcript_norm,
        "reason": "",
        "note": READING_NOTE,
    }
