"""课堂测验判分：客观题纯 Python 判分，简答题标记人工/AI 复核（不硬判）。

对齐 recon §5.5：按 option ``value`` 精确比较；兼容旧数据里把选项文本当答案的写法
（label 与 value 精确且唯一时才解析）。主观题只返回 ``needs_review``，不猜分。
"""
from __future__ import annotations

from . import schema


def _given_values(raw) -> list[str]:
    if isinstance(raw, str):
        return [raw]
    if not isinstance(raw, (list, tuple)):
        return []
    return [item for item in raw if isinstance(item, (str, int, float)) and str(item).strip()]


def grade_quiz(content: dict, answers: list[dict]) -> dict:
    """对一份测验内容判分。

    - single：作答 value 集合与答案集合完全相等；
    - multiple：集合相等（多选、少选、错选都算错）；
    - short_answer：返回 ``needs_review``，得分记 0（满分计入 max_score，交由 AI/人工复核）。
    """
    questions = content.get("questions") if isinstance(content, dict) else []
    if not isinstance(questions, list):
        questions = []
    submitted: dict[str, list[str]] = {}
    for item in answers or []:
        if not isinstance(item, dict):
            continue
        qid = str(item.get("question_id") or item.get("id") or "").strip()
        if not qid:
            continue
        submitted[qid] = _given_values(item.get("values") if "values" in item else item.get("answer"))
    detail: list[dict] = []
    score = 0.0
    max_score = 0.0
    correct_count = 0
    needs_review = 0
    for question in questions:
        qid = str(question.get("id") or "")
        qtype = question.get("type") or "single"
        points = round(schema._num(question.get("points"), schema.DEFAULT_QUIZ_POINTS, 1.0, schema.MAX_POINTS), 2)
        max_score += points
        given = submitted.get(qid, [])
        if qtype == "short_answer":
            needs_review += 1
            detail.append({
                "question_id": qid,
                "type": "short_answer",
                "question": question.get("question") or "",
                "given": " ".join(str(item) for item in given)[:4000],
                "correct": None,
                "needs_review": True,
                "correct_answer": [],
                "points": points,
                "earned": 0.0,
                "analysis": question.get("analysis") or "",
                "comment_prompt": question.get("commentPrompt") or "",
            })
            continue
        options = question.get("options") if isinstance(question.get("options"), list) else []
        correct = schema._normalize_answers(question.get("answer"), options)
        normalized_given = schema._normalize_answers(given, options)
        is_correct = bool(normalized_given) and set(normalized_given) == set(correct)
        if is_correct:
            score += points
            correct_count += 1
        detail.append({
            "question_id": qid,
            "type": qtype,
            "question": question.get("question") or "",
            "given": normalized_given,
            "correct": is_correct,
            "needs_review": False,
            "correct_answer": correct,
            "points": points,
            "earned": points if is_correct else 0.0,
            "analysis": question.get("analysis") or "",
        })
    return {
        "score": round(score, 2),
        "max_score": round(max_score, 2),
        "correct_count": correct_count,
        "question_count": len(questions),
        "needs_review_count": needs_review,
        "detail": detail,
    }


__all__ = ["grade_quiz"]
