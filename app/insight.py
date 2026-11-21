"""学情分析（整合自 Oh My Paper 报告 + 小鹿老师学情分析）：个人学习报告与 AI 文字建议。

契约见 `INTEGRATION-CONTRACT.md` §4.3。数据实时聚合自 attempts / mastery /
wrong_questions / knowledge_points / question_kp / ai_jobs，以及整合模块的只读统计
（AI 课堂 ``cls_*``、整卷练习 ``pl_paper_attempts/pl_paper_answers``、我的题库
``pl_questions/pl_papers/pl_cards``、AI 助手 ``asst_*``；直接 SQL 只读，不新建业务表）。

统计口径（同时写入每个响应的 ``caliber`` 字段与前端说明）：

* **正确率 / 准确率**：``correct / graded``，其中 graded 指 ``attempts.result != 'pending'``
  的已判定作答；同一道题多次作答分别计入；单位是百分比（0–100，保留 1 位小数）。
* **掌握分布**：``mastery`` 表的**当前**等级（每用户每题一行），属于当前状态，
  不随时间窗口截断。
* **到期复习 / 错题数**：``wrong_questions`` 中未归档记录的当前数量，同样不随窗口截断。
* **知识点统计**：按 ``question_kp`` 关联逐题统计。一道题关联多个知识点时，该题的
  作答分别计入**每一个**关联知识点（知序的数据模型是 qid→kp 离散关联，不做 OMP
  的分数均摊）；口径在该字段的响应里注明。知识点掌握等级优先由关联题目的
  ``mastery`` 记录聚合，缺少掌握度记录时按关联题正确率推算。
* **薄弱阈值**：知识点正确率 < 70%（即 OMP 的 accuracy < 0.7）判为薄弱；
  ≥ 85% 且作答 ≥ 3 次判为优势知识点。
* **时间窗口**：作答、AI 任务、活动/趋势、错题分布按 ``days``（默认 90）窗口过滤；
  窗口按 UTC 自然日计算（与 ``db.utcnow()`` 的 ISO-8601 UTC 字符串一致）。
"""
import asyncio
import json
from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from . import db, home_stats, practice
from .auth import current_user

router = APIRouter(prefix="/api/insight", tags=["Learning insight"])

DEFAULT_DAYS = 90
MAX_DAYS = 3650
WEAK_ACCURACY = 70.0
STRONG_ACCURACY = 85.0
STRONG_MIN_ATTEMPTS = 3
MASTERY_LEVELS = ("未掌握", "初步掌握", "比较掌握", "熟练掌握")
_MASTERY_INDEX = {name: index for index, name in enumerate(MASTERY_LEVELS)}
UNKNOWN_SUBJECT = "未分类"
_MAX_LIST = 50
_MAX_RECOMMENDATIONS = 6

CALIBER = {
    "correct_rate": "正确率 = 已判定作答（attempts.result != 'pending'）中判对的占比，单位 %，保留 1 位小数；同一道题的多次作答分别计入。",
    "accuracy": "学科/知识点准确率均按每次作答的判题结果统计，不是去重后的题目正确率。",
    "mastery": "掌握分布来自 mastery 表的当前等级（未掌握/初步掌握/比较掌握/熟练掌握）；知识点掌握等级优先由关联题目的掌握度聚合，缺少掌握度记录时按正确率推算（≥90% 熟练 / ≥70% 比较 / ≥40% 初步 / 其余未掌握）。",
    "knowledge": "知识点统计按 question_kp 的题目关联逐题统计；一道题关联多个知识点时，该题作答分别计入每个知识点（不按题拆分）。暂无关联数据时返回空数组并说明。",
    "weak": "薄弱知识点阈值：正确率 < 70%；优势知识点：正确率 ≥ 85% 且作答 ≥ 3 次。",
    "wrong": "错题原因优先使用错题本里的人工错误标签（一题多标签时分别计入），没有标签时使用错题原因字段；错题分布按 days 窗口（last_wrong_at/added_at）过滤，而错题数、到期复习为当前状态总量。",
    "window": "作答、AI 任务、活动/趋势与错题分布按 days 时间窗口统计（默认近 90 天，UTC 自然日）；掌握分布、到期复习与错题数为当前状态，不随窗口截断。",
    "ai": "AI 学习建议只基于本接口返回的真实统计生成，显著标注 AI 生成；统计数字由代码计算，模型不得编造。",
    "integrations": (
        "整合模块统计为当前账号实时聚合：课堂=cls_courses/cls_scenes/cls_quiz_attempts，"
        "整卷练习=pl_paper_attempts（仅已提交）与 pl_paper_answers（剔除待核验的主观题），"
        "题库产出=pl_questions（未删除）/pl_papers/pl_cards，AI 助手=asst_conversations/asst_messages；"
        "均为当前总量，不随 days 窗口截断。"
    ),
    "papers": (
        "整卷练习：练习次数=已提交的整卷练习数；平均得分=各次 score/max_score 的百分比均值；"
        "正确率=已判定的整卷作答（result=correct/incorrect，不含 pending）中判对的占比；"
        "recent 为最近 5 次已提练习。"
    ),
    "activity_mix": "活动混合=近 days 天内各模块的真实活动计数：题库练习（attempts）、课堂小测（cls_quiz_attempts）、整卷练习（已提交的 pl_paper_attempts）、AI 助手（用户消息 asst_messages）。",
    "review_due": "『今日复习』以服务端四档复习卡（/api/review-cards）为准：due_now=已到期未归档卡，due_today=本 UTC 自然日结束前到期（含已逾期）；卡到期在同一事务镜像到旧错题到期字段，因此学情、首页、错题本与学习计划使用同一个日期。",
}


# ---------------------------------------------------------------------------
# 通用工具
# ---------------------------------------------------------------------------


def _clamp_days(days) -> int:
    try:
        value = int(days)
    except (TypeError, ValueError):
        return DEFAULT_DAYS
    return max(1, min(MAX_DAYS, value))


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return db.utcnow()


def _window_start_iso(days: int) -> str:
    return (_now() - timedelta(days=_clamp_days(days))).isoformat()


def _chunks(values, size: int = 900):
    for start in range(0, len(values), size):
        yield values[start:start + size]


def _percent(correct: int, graded: int) -> float:
    if not graded:
        return 0.0
    return round(100.0 * correct / graded, 1)


def _subject_type_map(qids) -> dict:
    """qid → {subject, type}，来自只读题库；题库不可用时返回空映射（降级为未分类）。"""
    wanted = [qid for qid in dict.fromkeys(q for q in qids if q)]
    result: dict[str, dict] = {}
    if not wanted:
        return result
    try:
        with practice._bank() as con:
            table = practice._bank_table(con, "questions")
            for chunk in _chunks(wanted):
                marks = ",".join("?" for _ in chunk)
                for row in con.execute(
                    f"SELECT qid, subject, type FROM {table} WHERE qid IN ({marks})", chunk
                ):
                    result[row[0]] = {"subject": row[1] or UNKNOWN_SUBJECT, "type": row[2] or ""}
    except HTTPException:
        # 题库缺失时学情分析仍可展示作答次数等不依赖学科的数据。
        return result
    except Exception:  # noqa: BLE001 - 只读题库异常不能让整个报告 500
        return result
    return result


def _type_label(type_value: str) -> str:
    return practice.TYPE_LABELS.get(type_value or "", type_value or "其他")


def _parse_iso(value) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


# ---------------------------------------------------------------------------
# 数据收集：一次查询、三个视图共用（overview / knowledge / mistakes / report）
# ---------------------------------------------------------------------------


def _collect(user_id: int, days: int, subject: str | None = None) -> dict:
    """读取当前用户在窗口内的原始记录并补上学科/题型标签（已按学科过滤）。"""
    days = _clamp_days(days)
    start = _window_start_iso(days)
    with db.connection() as con:
        attempt_rows = con.execute(
            "SELECT qid, result, created_at FROM attempts WHERE user_id=? AND created_at>=?",
            (user_id, start),
        ).fetchall()
        mastery_rows = con.execute(
            "SELECT qid, level, updated_at FROM mastery WHERE user_id=?", (user_id,)
        ).fetchall()
        wrong_rows = con.execute(
            "SELECT qid, reason, error_tags_json, failures, next_review_at, added_at, "
            "last_wrong_at, archived FROM wrong_questions WHERE user_id=?",
            (user_id,),
        ).fetchall()
        ai_jobs_count = con.execute(
            "SELECT COUNT(*) FROM ai_jobs WHERE user_id=? AND created_at>=?",
            (user_id, start),
        ).fetchone()[0]

    qids = [row["qid"] for row in attempt_rows]
    qids += [row["qid"] for row in mastery_rows]
    qids += [row["qid"] for row in wrong_rows]
    labels = _subject_type_map(qids)

    def _subject_of(qid: str) -> str:
        info = labels.get(qid)
        return (info or {}).get("subject") or UNKNOWN_SUBJECT

    attempts = []
    for row in attempt_rows:
        item = dict(row)
        item["subject"] = _subject_of(item["qid"])
        info = labels.get(item["qid"]) or {}
        item["type"] = info.get("type") or ""
        if subject and item["subject"] != subject:
            continue
        attempts.append(item)

    mastery = []
    for row in mastery_rows:
        item = dict(row)
        item["subject"] = _subject_of(item["qid"])
        if subject and item["subject"] != subject:
            continue
        mastery.append(item)

    wrongs = []
    for row in wrong_rows:
        item = dict(row)
        item["subject"] = _subject_of(item["qid"])
        info = labels.get(item["qid"]) or {}
        item["type"] = info.get("type") or ""
        try:
            item["error_tags"] = json.loads(item.get("error_tags_json") or "[]")
        except (TypeError, ValueError):
            item["error_tags"] = []
        if subject and item["subject"] != subject:
            continue
        wrongs.append(item)

    return {
        "user_id": user_id,
        "days": days,
        "start": start,
        "subject": subject or None,
        "attempts": attempts,
        "mastery": mastery,
        "wrongs": wrongs,
        "ai_jobs": int(ai_jobs_count or 0),
        "labels": labels,
    }


def _attempt_summary(attempts) -> dict:
    graded = [item for item in attempts if item.get("result") != "pending"]
    correct = sum(1 for item in graded if item["result"] == "correct")
    return {
        "attempts": len(attempts),
        "graded_attempts": len(graded),
        "correct_attempts": correct,
        "correct_rate": _percent(correct, len(graded)),
    }


def _mastery_counts(mastery) -> dict:
    counts = {level: 0 for level in MASTERY_LEVELS}
    for item in mastery:
        level = item.get("level")
        if level in counts:
            counts[level] += 1
    return counts


def _subjects_payload(data) -> list:
    grouped: dict[str, dict] = {}
    for item in data["attempts"]:
        bucket = grouped.setdefault(item["subject"], {"attempts": 0, "graded": 0, "correct": 0})
        bucket["attempts"] += 1
        if item["result"] != "pending":
            bucket["graded"] += 1
            if item["result"] == "correct":
                bucket["correct"] += 1
    mastery_by_subject: dict[str, dict] = {}
    for item in data["mastery"]:
        bucket = mastery_by_subject.setdefault(
            item["subject"], {level: 0 for level in MASTERY_LEVELS}
        )
        if item.get("level") in bucket:
            bucket[item["level"]] += 1
    items = []
    for name, bucket in grouped.items():
        items.append({
            "subject": name,
            "attempts": bucket["attempts"],
            "graded_attempts": bucket["graded"],
            "correct_rate": _percent(bucket["correct"], bucket["graded"]),
            "accuracy": _percent(bucket["correct"], bucket["graded"]),
            "mastery": mastery_by_subject.get(name, {level: 0 for level in MASTERY_LEVELS}),
        })
    items.sort(key=lambda item: (-item["attempts"], item["subject"]))
    return items


def _activity_payload(data) -> list:
    """近 days 天每日活动：按 UTC 日期分桶，补零，保证图表连续。"""
    days = data["days"]
    today = _now().date()
    first = today - timedelta(days=days - 1)
    buckets: dict[str, dict] = {}
    cursor = first
    while cursor <= today:
        buckets[cursor.isoformat()] = {"date": cursor.isoformat(), "attempts": 0, "correct": 0}
        cursor += timedelta(days=1)
    for item in data["attempts"]:
        key = str(item.get("created_at") or "")[:10]
        bucket = buckets.get(key)
        if bucket is None:
            continue
        bucket["attempts"] += 1
        if item.get("result") == "correct":
            bucket["correct"] += 1
    return list(buckets.values())


def _trend_payload(data) -> list:
    """按自然周（周一起）聚合作答与正确率。"""
    days = data["days"]
    today = _now().date()
    first = today - timedelta(days=days - 1)
    week_start = first - timedelta(days=first.weekday())
    buckets: dict[str, dict] = {}
    cursor = week_start
    last_week = today - timedelta(days=today.weekday())
    while cursor <= last_week:
        buckets[cursor.isoformat()] = {
            "week": cursor.isoformat(), "attempts": 0, "graded": 0, "correct": 0,
        }
        cursor += timedelta(days=7)
    for item in data["attempts"]:
        parsed = _parse_iso(item.get("created_at"))
        if parsed is None:
            continue
        monday = (parsed.date() - timedelta(days=parsed.date().weekday())).isoformat()
        bucket = buckets.get(monday)
        if bucket is None:
            continue
        bucket["attempts"] += 1
        if item["result"] != "pending":
            bucket["graded"] += 1
            if item["result"] == "correct":
                bucket["correct"] += 1
    return [
        {
            "week": bucket["week"],
            "attempts": bucket["attempts"],
            "accuracy": _percent(bucket["correct"], bucket["graded"]),
        }
        for bucket in buckets.values()
    ]


# ---------------------------------------------------------------------------
# 整合模块学习数据（课堂 / 整卷练习 / 题库 / AI 助手）
# 直接只读 SQL 聚合，不 import 其他模块，避免跨模块循环依赖；单表缺失时降级为 0。
# ---------------------------------------------------------------------------


def _safe_scalar(con, sql: str, params=(), default=0):
    try:
        row = con.execute(sql, params).fetchone()
    except Exception:  # noqa: BLE001 - 某模块表缺失不应让整个报告 500
        return default
    if row is None or row[0] is None:
        return default
    return row[0]


def _safe_rows(con, sql: str, params=()):
    try:
        return con.execute(sql, params).fetchall()
    except Exception:  # noqa: BLE001 - 单模块降级
        return []


def _percent_ratio(score: float, max_score: float) -> float:
    try:
        score = float(score or 0)
        max_score = float(max_score or 0)
    except (TypeError, ValueError):
        return 0.0
    if max_score <= 0:
        return 0.0
    return round(100.0 * score / max_score, 1)


def _integrations_payload(user_id: int) -> dict:
    """课堂/整卷练习/题库产出/AI 助手四块实时统计（当前总量，用户隔离）。"""
    classroom = {
        "courses": 0, "ready_courses": 0, "scenes_total": 0, "scenes_completed": 0,
        "quiz_attempts": 0, "quiz_accuracy": 0.0,
    }
    papers = {
        "practice_attempts": 0, "practice_avg_score": 0.0,
        "practice_accuracy": 0.0, "recent": [],
    }
    library = {"questions": 0, "verified": 0, "pending_review": 0, "papers": 0, "cards": 0}
    assistant = {"conversations": 0, "messages": 0}
    with db.connection() as con:
        try:
            classroom = {
                "courses": int(_safe_scalar(
                    con, "SELECT COUNT(*) FROM cls_courses WHERE user_id=?", (user_id,))),
                "ready_courses": int(_safe_scalar(
                    con,
                    "SELECT COUNT(*) FROM cls_courses WHERE user_id=? AND status='ready'",
                    (user_id,),
                )),
                "scenes_total": int(_safe_scalar(
                    con, "SELECT COUNT(*) FROM cls_scenes WHERE user_id=?", (user_id,))),
                "scenes_completed": int(_safe_scalar(
                    con,
                    "SELECT COUNT(*) FROM cls_scenes WHERE user_id=? AND status='ready'",
                    (user_id,),
                )),
                "quiz_attempts": int(_safe_scalar(
                    con, "SELECT COUNT(*) FROM cls_quiz_attempts WHERE user_id=?", (user_id,))),
                "quiz_accuracy": 0.0,
            }
            quiz_score = float(_safe_scalar(
                con, "SELECT COALESCE(SUM(score),0) FROM cls_quiz_attempts WHERE user_id=?",
                (user_id,)))
            quiz_max = float(_safe_scalar(
                con, "SELECT COALESCE(SUM(max_score),0) FROM cls_quiz_attempts WHERE user_id=?",
                (user_id,)))
            classroom["quiz_accuracy"] = _percent_ratio(quiz_score, quiz_max)
        except Exception:  # noqa: BLE001
            pass

        try:
            submitted = int(_safe_scalar(
                con,
                "SELECT COUNT(*) FROM pl_paper_attempts WHERE user_id=? AND status='submitted'",
                (user_id,),
            ))
            score_sum = float(_safe_scalar(
                con,
                "SELECT COALESCE(SUM(score),0) FROM pl_paper_attempts "
                "WHERE user_id=? AND status='submitted'",
                (user_id,),
            ))
            max_sum = float(_safe_scalar(
                con,
                "SELECT COALESCE(SUM(max_score),0) FROM pl_paper_attempts "
                "WHERE user_id=? AND status='submitted'",
                (user_id,),
            ))
            correct_answers = int(_safe_scalar(
                con,
                "SELECT COUNT(*) FROM pl_paper_answers WHERE user_id=? AND result='correct'",
                (user_id,),
            ))
            graded_answers = int(_safe_scalar(
                con,
                "SELECT COUNT(*) FROM pl_paper_answers WHERE user_id=? AND result IN ('correct','incorrect')",
                (user_id,),
            ))
            recent_rows = _safe_rows(
                con,
                "SELECT a.paper_id, a.score, a.max_score, a.submitted_at, p.title "
                "FROM pl_paper_attempts a LEFT JOIN pl_papers p ON p.id=a.paper_id "
                "WHERE a.user_id=? AND a.status='submitted' "
                "ORDER BY COALESCE(a.submitted_at, a.created_at) DESC, a.id DESC LIMIT 5",
                (user_id,),
            )
            recent = [
                {
                    "paper_title": str(row["title"] or "未命名试卷"),
                    "score": round(float(row["score"] or 0), 2),
                    "max_score": round(float(row["max_score"] or 0), 2),
                    "submitted_at": row["submitted_at"],
                }
                for row in recent_rows
            ]
            papers = {
                "practice_attempts": submitted,
                "practice_avg_score": _percent_ratio(score_sum, max_sum),
                "practice_accuracy": _percent(correct_answers, graded_answers),
                "recent": recent,
            }
        except Exception:  # noqa: BLE001
            pass

        try:
            library = {
                "questions": int(_safe_scalar(
                    con,
                    "SELECT COUNT(*) FROM pl_questions WHERE user_id=? AND deleted_at IS NULL",
                    (user_id,),
                )),
                "verified": int(_safe_scalar(
                    con,
                    "SELECT COUNT(*) FROM pl_questions WHERE user_id=? AND deleted_at IS NULL "
                    "AND verify_status='verified'",
                    (user_id,),
                )),
                "pending_review": int(_safe_scalar(
                    con,
                    "SELECT COUNT(*) FROM pl_questions WHERE user_id=? AND deleted_at IS NULL "
                    "AND verify_status='pending'",
                    (user_id,),
                )),
                "papers": int(_safe_scalar(
                    con, "SELECT COUNT(*) FROM pl_papers WHERE user_id=?", (user_id,))),
                "cards": int(_safe_scalar(
                    con, "SELECT COUNT(*) FROM pl_cards WHERE user_id=?", (user_id,))),
            }
        except Exception:  # noqa: BLE001
            pass

        try:
            assistant = {
                "conversations": int(_safe_scalar(
                    con, "SELECT COUNT(*) FROM asst_conversations WHERE user_id=?", (user_id,))),
                "messages": int(_safe_scalar(
                    con, "SELECT COUNT(*) FROM asst_messages WHERE user_id=?", (user_id,))),
            }
        except Exception:  # noqa: BLE001
            pass
    return {
        "classroom": classroom,
        "papers": papers,
        "library": library,
        "assistant": assistant,
    }


def _activity_mix_payload(user_id: int, days: int) -> dict:
    """近 days 天各模块真实活动计数（题库练习/课堂小测/整卷练习/AI 助手）。"""
    start = _window_start_iso(days)
    with db.connection() as con:
        practice_count = int(_safe_scalar(
            con, "SELECT COUNT(*) FROM attempts WHERE user_id=? AND created_at>=?",
            (user_id, start)))
        classroom_count = int(_safe_scalar(
            con, "SELECT COUNT(*) FROM cls_quiz_attempts WHERE user_id=? AND created_at>=?",
            (user_id, start)))
        paper_count = int(_safe_scalar(
            con,
            "SELECT COUNT(*) FROM pl_paper_attempts WHERE user_id=? AND status='submitted' "
            "AND COALESCE(submitted_at, created_at)>=?",
            (user_id, start),
        ))
        assistant_count = int(_safe_scalar(
            con,
            "SELECT COUNT(*) FROM asst_messages WHERE user_id=? AND role='user' AND created_at>=?",
            (user_id, start),
        ))
    items = [
        {"key": "practice", "label": "题库练习", "count": practice_count},
        {"key": "classroom", "label": "AI 课堂", "count": classroom_count},
        {"key": "paper_practice", "label": "整卷练习", "count": paper_count},
        {"key": "assistant", "label": "AI 助手", "count": assistant_count},
    ]
    return {
        "days": days,
        "total": sum(item["count"] for item in items),
        "items": items,
    }


def _integrations_has_data(integrations: dict) -> bool:
    """整合模块是否有可展示的活动（用于 has_data 与空态判断，不改变原有作答口径）。"""
    classroom = integrations.get("classroom") or {}
    papers = integrations.get("papers") or {}
    library = integrations.get("library") or {}
    assistant = integrations.get("assistant") or {}
    return bool(
        classroom.get("courses") or classroom.get("scenes_total") or classroom.get("quiz_attempts")
        or papers.get("practice_attempts")
        or library.get("questions") or library.get("papers") or library.get("cards")
        or assistant.get("conversations") or assistant.get("messages")
    )


def _overview_payload(data) -> dict:
    summary = _attempt_summary(data["attempts"])
    active_wrongs = [item for item in data["wrongs"] if not item.get("archived")]
    now = _now_iso()
    due = sum(1 for item in active_wrongs if str(item.get("next_review_at") or "") <= now)
    summary.update({
        "mastery_counts": _mastery_counts(data["mastery"]),
        "due_reviews": due,
        "wrong_count": len(active_wrongs),
        "ai_jobs": data["ai_jobs"],
        "window_days": data["days"],
        "window_start": data["start"],
    })
    integrations = _integrations_payload(data["user_id"])
    activity_mix = _activity_mix_payload(data["user_id"], data["days"])
    has_data = bool(
        summary["attempts"]
        or summary["wrong_count"]
        or any(summary["mastery_counts"].values())
        or _integrations_has_data(integrations)
    )
    return {
        "summary": summary,
        "subjects": _subjects_payload(data),
        "activity": _activity_payload(data),
        "trend": _trend_payload(data),
        "integrations": integrations,
        "activity_mix": activity_mix,
        "has_data": has_data,
        "caliber": CALIBER,
    }


# ---------------------------------------------------------------------------
# 知识点掌握
# ---------------------------------------------------------------------------


def _load_kp_maps() -> tuple[dict, dict]:
    """返回 (kp 元数据 map, qid → [kp_id...] map)。知识库不大，一次读全量。"""
    with db.connection() as con:
        kp_rows = con.execute(
            "SELECT id, subject, parent_id, name, level FROM knowledge_points"
        ).fetchall()
    kp_map = {
        row["id"]: {
            "id": row["id"],
            "subject": row["subject"],
            "parent_id": row["parent_id"],
            "name": row["name"],
            "level": row["level"],
        }
        for row in kp_rows
    }
    return kp_map, {}


def _kp_path(kp_map: dict, kp_id) -> str:
    parts: list[str] = []
    cursor = kp_map.get(kp_id)
    guard = 0
    while cursor is not None and guard < 8:
        parts.append(cursor["name"])
        cursor = kp_map.get(cursor.get("parent_id"))
        guard += 1
    return " / ".join(reversed(parts))


def _qid_kp_map(qids, subject: str | None = None) -> dict:
    wanted = [qid for qid in dict.fromkeys(q for q in qids if q)]
    result: dict[str, list[int]] = {}
    if not wanted:
        return result
    with db.connection() as con:
        for chunk in _chunks(wanted):
            marks = ",".join("?" for _ in chunk)
            rows = con.execute(
                "SELECT qk.qid, qk.kp_id FROM question_kp qk "
                f"WHERE qk.qid IN ({marks}) ORDER BY qk.weight DESC, qk.kp_id",
                chunk,
            ).fetchall()
            for row in rows:
                result.setdefault(row["qid"], []).append(row["kp_id"])
    if not subject:
        return result
    kp_map, _ = _load_kp_maps()
    allowed = set()
    for qid, ids in list(result.items()):
        keep = [kp_id for kp_id in ids if (kp_map.get(kp_id) or {}).get("subject") == subject]
        if keep:
            result[qid] = keep
        else:
            result.pop(qid, None)
    return result


def _knowledge_payload(data) -> dict:
    kp_map, _ = _load_kp_maps()
    attempts = data["attempts"]
    wrong_qids = {item["qid"] for item in data["wrongs"] if not item.get("archived")}
    qid_kps = _qid_kp_map([item["qid"] for item in attempts], data["subject"])

    mastery_by_qid: dict[str, list[str]] = {}
    for item in data["mastery"]:
        mastery_by_qid.setdefault(item["qid"], []).append(item.get("level") or "未掌握")

    stats: dict[int, dict] = {}
    for item in attempts:
        for kp_id in qid_kps.get(item["qid"], []):
            bucket = stats.setdefault(kp_id, {
                "kp_id": kp_id,
                "attempts": 0,
                "graded": 0,
                "correct": 0,
                "qids": set(),
                "wrong_qids": set(),
                "levels": [],
            })
            bucket["attempts"] += 1
            bucket["qids"].add(item["qid"])
            if item["result"] != "pending":
                bucket["graded"] += 1
                if item["result"] == "correct":
                    bucket["correct"] += 1
            if item["qid"] in wrong_qids:
                bucket["wrong_qids"].add(item["qid"])
            for level in mastery_by_qid.get(item["qid"], []):
                bucket["levels"].append(level)

    items = []
    for kp_id, bucket in stats.items():
        kp = kp_map.get(kp_id)
        if kp is None:
            continue
        accuracy = _percent(bucket["correct"], bucket["graded"])
        items.append({
            "kp_id": kp_id,
            "title": kp["name"],
            "path": _kp_path(kp_map, kp_id),
            "subject": kp["subject"],
            "level": kp["level"],
            "attempts": bucket["attempts"],
            "graded_attempts": bucket["graded"],
            "accuracy": accuracy,
            "question_count": len(bucket["qids"]),
            "wrong_count": len(bucket["wrong_qids"]),
            "mastery_level": _aggregate_mastery(bucket["levels"], accuracy),
            # 只有存在已判定作答时才能判断薄弱；全是待核验作答时不能判为薄弱。
            "weak": bucket["graded"] > 0 and accuracy < WEAK_ACCURACY,
        })
    items.sort(key=lambda item: (item["accuracy"], -item["attempts"], item["title"]))

    linked_attempts = sum(1 for item in attempts if qid_kps.get(item["qid"]))
    coverage = {
        "attempted_questions": len({item["qid"] for item in attempts}),
        "linked_attempts": linked_attempts,
        "knowledge_point_count": len({kp_id for ids in qid_kps.values() for kp_id in ids}),
    }
    note = None
    if not attempts:
        note = "所选时间范围内还没有作答记录，暂无可统计的知识点掌握。"
    elif not items:
        note = (
            "题库题目尚未关联知识点（question_kp 为空或无你练过的题目），暂时无法统计知识点掌握。"
            "管理员可在「内容管理」中标注知识点，或使用「知识点批量打标」任务建立关联。"
        )
    return {
        "items": items,
        "note": note,
        "coverage": coverage,
        "subject": data["subject"],
        "caliber": {
            "knowledge": CALIBER["knowledge"],
            "weak": CALIBER["weak"],
            "mastery": CALIBER["mastery"],
        },
    }


def _aggregate_mastery(levels, accuracy: float) -> str:
    if levels:
        index = round(sum(_MASTERY_INDEX.get(level, 0) for level in levels) / len(levels))
        return MASTERY_LEVELS[max(0, min(len(MASTERY_LEVELS) - 1, index))]
    if accuracy >= 90:
        return "熟练掌握"
    if accuracy >= WEAK_ACCURACY:
        return "比较掌握"
    if accuracy >= 40:
        return "初步掌握"
    return "未掌握"


def _knowledge_sections(data) -> dict:
    """报告用的强/弱/未开始知识点分组。"""
    knowledge = _knowledge_payload(data)
    items = knowledge["items"]
    strong = [
        _thin_kp(item) for item in items
        if item["accuracy"] >= STRONG_ACCURACY and item["graded_attempts"] >= STRONG_MIN_ATTEMPTS
    ][:10]
    weak = [_thin_kp(item) for item in items if item["weak"]][:10]
    not_started: list[dict] = []
    not_started_total = 0
    # 「未开始」= 有题目关联（question_kp）但在全部历史作答中从未练过的知识点；
    # 没有知识点关联数据时无法可靠判断，返回空数组避免把「未关联」误报成「未学习」。
    with db.connection() as con:
        linked_total = con.execute("SELECT COUNT(DISTINCT kp_id) FROM question_kp").fetchone()[0]
        started_qids = [row[0] for row in con.execute(
            "SELECT DISTINCT qid FROM attempts WHERE user_id=?", (data["user_id"],)
        )]
        kp_rows = con.execute(
            "SELECT id, subject, parent_id, name, level FROM knowledge_points"
        ).fetchall()
    kp_map = {
        row["id"]: {
            "id": row["id"], "subject": row["subject"], "parent_id": row["parent_id"],
            "name": row["name"], "level": row["level"],
        }
        for row in kp_rows
    }
    if linked_total:
        started_kp_ids: set[int] = set()
        for qid, ids in _qid_kp_map(started_qids, data["subject"]).items():
            started_kp_ids.update(ids)
        linked_kp_ids: set[int] = set()
        with db.connection() as con:
            for chunk in _chunks(list(kp_map.keys())):
                if not chunk:
                    continue
                marks = ",".join("?" for _ in chunk)
                for row in con.execute(
                    f"SELECT DISTINCT kp_id FROM question_kp WHERE kp_id IN ({marks})", chunk
                ):
                    linked_kp_ids.add(row[0])
        pending_ids = []
        for kp_id in linked_kp_ids:
            kp = kp_map.get(kp_id)
            if kp is None:
                continue
            if data["subject"] and kp["subject"] != data["subject"]:
                continue
            if kp_id in started_kp_ids:
                continue
            pending_ids.append(kp_id)
        pending_ids.sort(key=lambda kp_id: (kp_map[kp_id]["subject"], kp_map[kp_id]["name"]))
        not_started_total = len(pending_ids)
        not_started = [
            {
                "kp_id": kp_id,
                "title": kp_map[kp_id]["name"],
                "path": _kp_path(kp_map, kp_id),
                "subject": kp_map[kp_id]["subject"],
            }
            for kp_id in pending_ids[:_MAX_LIST]
        ]
    return {
        "strong": strong,
        "weak": weak,
        "not_started": not_started,
        "not_started_total": not_started_total,
        "items": items,
        "note": knowledge["note"],
        "coverage": knowledge["coverage"],
        "caliber": knowledge["caliber"],
    }


def _thin_kp(item: dict) -> dict:
    return {
        "kp_id": item["kp_id"],
        "title": item["title"],
        "path": item["path"],
        "subject": item["subject"],
        "attempts": item["attempts"],
        "graded_attempts": item.get("graded_attempts", item["attempts"]),
        "accuracy": item["accuracy"],
        "mastery_level": item["mastery_level"],
        "weak": item["weak"],
    }


# ---------------------------------------------------------------------------
# 错题分布
# ---------------------------------------------------------------------------


def _wrong_reason_labels(item: dict) -> list[str]:
    tags = [str(tag).strip() for tag in (item.get("error_tags") or []) if str(tag).strip()]
    if tags:
        return list(dict.fromkeys(tags))[:10]
    reason = str(item.get("reason") or "").strip()
    if reason and reason != "答题错误":
        return [reason]
    return ["未标注原因"]


def _mistakes_payload(data) -> dict:
    all_active = [item for item in data["wrongs"] if not item.get("archived")]
    window_start = data["start"]
    # 错题分布按窗口内的错题时间过滤；错题数/到期复习（overview summary）是当前状态总量。
    active = [
        item for item in all_active
        if str(item.get("last_wrong_at") or item.get("added_at") or "") >= window_start
    ]
    by_reason: dict[str, int] = {}
    for item in active:
        for label in _wrong_reason_labels(item):
            by_reason[label] = by_reason.get(label, 0) + 1
    by_type: dict[str, int] = {}
    for item in active:
        label = _type_label(item.get("type"))
        by_type[label] = by_type.get(label, 0) + 1

    qid_kps = _qid_kp_map([item["qid"] for item in active], data["subject"])
    kp_map, _ = _load_kp_maps()
    by_kp: dict[int, dict] = {}
    for item in active:
        for kp_id in qid_kps.get(item["qid"], []):
            bucket = by_kp.setdefault(kp_id, {"kp_id": kp_id, "count": 0})
            bucket["count"] += 1

    recent = []
    for item in active:
        occurred = item.get("last_wrong_at") or item.get("added_at")
        recent.append({
            "qid": item["qid"],
            "subject": item.get("subject") or UNKNOWN_SUBJECT,
            "type": _type_label(item.get("type")),
            "reason": "/".join(_wrong_reason_labels(item)),
            "occurred_at": occurred,
            "due": str(item.get("next_review_at") or "") <= _now_iso(),
            "failures": int(item.get("failures") or 0),
        })
    recent.sort(key=lambda item: str(item.get("occurred_at") or ""), reverse=True)

    kp_note = None
    if active and not by_kp:
        kp_note = "错题对应的题库题目尚未关联知识点，暂无法按知识点统计错题分布。"
    window_note = None
    if not active and all_active:
        window_note = "近 %s 天没有新增错题；错题本另有 %s 道更早的错题未计入本分布。" % (
            data["days"], len(all_active))

    return {
        "by_reason": [
            {"reason": label, "count": count}
            for label, count in sorted(by_reason.items(), key=lambda pair: (-pair[1], pair[0]))
        ],
        "by_type": [
            {"type": label, "count": count}
            for label, count in sorted(by_type.items(), key=lambda pair: (-pair[1], pair[0]))
        ],
        "by_kp": [
            {
                "kp": (kp_map.get(kp_id) or {}).get("name") or "#%s" % kp_id,
                "kp_id": kp_id,
                "path": _kp_path(kp_map, kp_id),
                "count": bucket["count"],
            }
            for kp_id, bucket in sorted(by_kp.items(), key=lambda pair: (-pair[1]["count"], pair[0]))
        ][:_MAX_LIST],
        "recent": recent[:20],
        "total": len(active),
        "window_days": data["days"],
        "active_total": len(all_active),
        "note": kp_note or window_note,
        "caliber": {"wrong": CALIBER["wrong"]},
    }


# ---------------------------------------------------------------------------
# 规则推荐（不调用模型）
# ---------------------------------------------------------------------------


def _integration_recommendations(overview: dict) -> list[dict]:
    """基于课堂/整卷练习/题库/助手的真实状态给出行动建议（不编造数据）。"""
    integrations = overview.get("integrations") or {}
    classroom = integrations.get("classroom") or {}
    papers = integrations.get("papers") or {}
    library = integrations.get("library") or {}
    assistant = integrations.get("assistant") or {}
    items: list[dict] = []
    courses = int(classroom.get("courses") or 0)
    ready_courses = int(classroom.get("ready_courses") or 0)
    scenes_total = int(classroom.get("scenes_total") or 0)
    scenes_completed = int(classroom.get("scenes_completed") or 0)
    if courses and (ready_courses < courses or scenes_completed < scenes_total):
        items.append({
            "title": "继续未完成的 AI 课堂",
            "reason": "你共有 %s 门 AI 课堂（%s 门已就绪），场景完成 %s/%s；继续上次课堂能保持学习连贯。" % (
                courses, ready_courses, scenes_completed, scenes_total),
            "count": max(0, courses - ready_courses) or max(0, scenes_total - scenes_completed),
            "action": "classroom",
        })
    if int(papers.get("practice_attempts") or 0) == 0 and int(library.get("papers") or 0) > 0:
        items.append({
            "title": "开始一次整卷练习",
            "reason": "你已有 %s 份试卷，但还没有提交过整卷练习；按考试节奏完成一份，能得到整卷得分与逐题错题记录。" % (
                int(library.get("papers") or 0)),
            "count": int(library.get("papers") or 0),
            "action": "paperlab",
        })
    pending_review = int(library.get("pending_review") or 0)
    if pending_review > 0:
        items.append({
            "title": "核验待处理的题目",
            "reason": "我的题库有 %s 道题仍待核验；先核对答案与解析，再用于组卷或整卷练习，避免把错误题目带进试卷。" % (
                pending_review),
            "count": pending_review,
            "action": "mybank",
        })
    if int(assistant.get("conversations") or 0) == 0 and (
        overview["summary"].get("attempts") or int(library.get("questions") or 0)
    ):
        items.append({
            "title": "用 AI 助手做一次复盘",
            "reason": "你还没有和 AI 助手对话过；可以把最近的错题或薄弱知识点交给它，它会基于你的真实数据给出下一步动作。",
            "count": 0,
            "action": "assistant",
        })
    return items


def _recommendations(data, overview: dict, knowledge: dict, mistakes: dict) -> list:
    items: list[dict] = []
    summary = overview["summary"]
    subject = data["subject"]
    integration_items = _integration_recommendations(overview)

    if not summary["attempts"] and not summary["wrong_count"]:
        if not integration_items:
            return [{
                "title": "从第一道题开始积累学情数据",
                "reason": "目前还没有作答、错题、课堂或整卷练习记录。先完成一次练习，学情报告与知识点分析才会出现真实数据。",
                "count": 0,
                "action": "practice",
            }]

    for kp in knowledge["weak"][:3]:
        items.append({
            "title": "专项突破：%s" % kp["title"],
            "reason": "近 %s 天该知识点正确率 %s%%，低于薄弱线 %s%%。" % (
                data["days"], kp["accuracy"], int(WEAK_ACCURACY)),
            "kp_ids": [kp["kp_id"]],
            "count": kp.get("graded_attempts", kp["attempts"]),
            "action": "practice",
        })

    if summary["due_reviews"]:
        items.append({
            "title": "复习到期的错题",
            "reason": "错题本里有 %s 道题已到复习时间，按复习计划过一遍能巩固记忆。" % summary["due_reviews"],
            "count": summary["due_reviews"],
            "action": "wrongbook",
        })

    for row in overview["subjects"][:3]:
        if row["graded_attempts"] >= 5 and row["accuracy"] < 60:
            items.append({
                "title": "加强 %s 练习" % row["subject"],
                "reason": "近 %s 天 %s 正确率 %s%%，低于 60%%，建议从基础题开始补齐。" % (
                    data["days"], row["subject"], row["accuracy"]),
                "count": row["graded_attempts"],
                "action": "practice",
            })
            break

    if mistakes["by_reason"]:
        top = mistakes["by_reason"][0]
        items.append({
            "title": "针对「%s」集中复盘" % top["reason"],
            "reason": "这是近 %s 天出现最多的错因（%s 道），复盘时优先检查这一类问题。" % (
                data["days"], top["count"]),
            "count": top["count"],
            "action": "wrongbook",
        })

    if knowledge["not_started"]:
        sample = knowledge["not_started"][0]
        items.append({
            "title": "预习尚未练习的知识点：%s" % sample["title"],
            "reason": "该知识点有题目关联，但你的历史作答中还没有练过；可从 1–2 道基础题开始。",
            "count": knowledge["not_started_total"],
            "action": "practice",
        })

    items.extend(integration_items)

    if not items:
        items.append({
            "title": "保持练习节奏",
            "reason": "当前没有明显薄弱项，继续按自己的节奏练习并按时复习错题即可。",
            "count": summary["attempts"],
            "action": "practice",
        })

    if subject:
        for item in items:
            item.setdefault("subject", subject)
    return items[:_MAX_RECOMMENDATIONS]


# ---------------------------------------------------------------------------
# 报告聚合
# ---------------------------------------------------------------------------


def _review_caliber(user_id: int) -> dict:
    """复习排程统一口径（M2）：读服务端四档复习卡，失败降级为旧错题口径。"""
    try:
        from . import study_review

        stats = study_review.stats(user_id)
        return {
            "available": True,
            "due_now": int(stats.get("due_now") or 0),
            "due_today": int(stats.get("due_today") or 0),
            "total": int(stats.get("total") or 0),
            "new": int(stats.get("new") or 0),
            "learning": int(stats.get("learning") or 0),
            "review": int(stats.get("review") or 0),
            "relearning": int(stats.get("relearning") or 0),
            "algorithm": stats.get("algorithm"),
            "algorithm_version": stats.get("algorithm_version"),
            "due_source": "review-cards",
            "caliber": CALIBER["review_due"],
            "note": "",
        }
    except Exception as error:  # noqa: BLE001 - 排程不可用不能拖垮学情其他统计
        return {
            "available": False,
            "due_now": None,
            "due_today": None,
            "total": None,
            "algorithm": None,
            "algorithm_version": None,
            "due_source": "legacy",
            "caliber": CALIBER["review_due"],
            "note": f"复习排程暂不可用，沿用旧错题到期口径（{type(error).__name__}）",
        }


def build_report(user_id: int, days: int = DEFAULT_DAYS, subject: str | None = None) -> dict:
    """构建个人学情报告（实时聚合；供 API 与 AI 建议任务复用）。"""
    days = _clamp_days(days)
    data = _collect(user_id, days, subject)
    overview = _overview_payload(data)
    knowledge = _knowledge_sections(data)
    mistakes = _mistakes_payload(data)
    recommendations = _recommendations(data, overview, knowledge, mistakes)
    return {
        "generated_at": _now_iso(),
        "range": {
            "days": days,
            "from": data["start"],
            "to": _now_iso(),
            "subject": subject or None,
        },
        "summary": overview["summary"],
        "subjects": overview["subjects"],
        "activity": overview["activity"],
        "trend": overview["trend"],
        "integrations": overview["integrations"],
        "activity_mix": overview["activity_mix"],
        "knowledge": knowledge,
        "mistakes": mistakes,
        "recommendations": recommendations,
        # M2：复习排程统一口径（与首页/错题/练习同一到期值）。
        "review": _review_caliber(user_id),
        "has_data": overview["has_data"],
        "caliber": CALIBER,
    }


# ---------------------------------------------------------------------------
# HTTP 路由
# ---------------------------------------------------------------------------


@router.get("/overview")
def overview(
    subject: str | None = Query(default=None, max_length=40),
    days: int = Query(default=DEFAULT_DAYS, ge=1, le=MAX_DAYS),
    user=Depends(current_user),
):
    """学情总览：摘要指标、分学科正确率、每日活动与周趋势（默认近 90 天）。"""
    payload = _overview_payload(_collect(user["id"], days, subject))
    # M2：总览也带复习排程统一口径，前端可与首页/错题本核对同一天。
    payload["review"] = _review_caliber(user["id"])
    return payload


@router.get("/knowledge")
def knowledge(
    subject: str | None = Query(default=None, max_length=40),
    days: int = Query(default=DEFAULT_DAYS, ge=1, le=MAX_DAYS),
    user=Depends(current_user),
):
    """知识点掌握：按 question_kp 关联统计，正确率升序（最弱在前），<70% 标为薄弱。"""
    return _knowledge_payload(_collect(user["id"], days, subject))


@router.get("/mistakes")
def mistakes(
    subject: str | None = Query(default=None, max_length=40),
    days: int = Query(default=DEFAULT_DAYS, ge=1, le=MAX_DAYS),
    user=Depends(current_user),
):
    """错题分布：按错因、题型、知识点聚合，并返回最近错题。"""
    return _mistakes_payload(_collect(user["id"], days, subject))


@router.get("/report")
def report(
    subject: str | None = Query(default=None, max_length=40),
    days: int = Query(default=DEFAULT_DAYS, ge=1, le=MAX_DAYS),
    user=Depends(current_user),
):
    """个人学情报告：摘要 + 学科 + 知识点强弱 + 错题分布 + 活动 + 规则建议。"""
    return build_report(user["id"], days, subject)


@router.get("/home")
def home(
    days: int = Query(default=DEFAULT_DAYS, ge=1, le=MAX_DAYS),
    user=Depends(current_user),
):
    """首页统计口径：题库可练/可自动判分/待核验 + 个人待复习、今日练习、首次正确率、薄弱知识点。

    与 `/api/insight/report`、`/api/practice/stats` 共用真实数据口径（见 app/home_stats.py）。
    """
    return home_stats.build_home(user["id"], days)


# ---------------------------------------------------------------------------
# AI 学习建议（任务 insight-report）
# ---------------------------------------------------------------------------


class NarrativeInput(BaseModel):
    provider_id: int = Field(ge=1)
    model: str = Field(min_length=1, max_length=160)
    subject: str | None = Field(default=None, max_length=40)
    # 与报告页的时间窗口保持一致；默认近 90 天。
    days: int = Field(default=DEFAULT_DAYS, ge=1, le=MAX_DAYS)


@router.post("/narrative")
async def narrative(data: NarrativeInput, user=Depends(current_user)):
    """创建 AI 学情建议任务（task=insight-report），返回与 POST /api/ai/jobs 一致的任务对象。"""
    from .ai.jobs import JobInput, submit_job

    job_input = JobInput(
        task="insight-report",
        provider_id=data.provider_id,
        model=data.model,
        subject=data.subject,
        params={"days": data.days, "subject": data.subject},
    )
    return await submit_job(user, job_input)


def _prompt_version() -> str:
    try:
        from .ai import PROMPT_VERSION
        return str(PROMPT_VERSION or "unknown")
    except Exception:  # noqa: BLE001 - 版本号缺失不应阻断任务
        return "unknown"


def validate_insight_job(data, user) -> None:
    """任务准入：连接归属、参数范围，以及「没有真实统计就不生成建议」的底线。"""
    from .ai.providers import _selected_provider

    # submit_job 已校验过一次，这里再显式复校验，防止绕过通用入口直接派发。
    _selected_provider(user["id"], data.provider_id, data.model)
    params = data.params or {}
    days = _clamp_days(params.get("days", DEFAULT_DAYS))
    subject = params.get("subject")
    if subject is not None:
        if not isinstance(subject, str) or len(subject) > 40:
            raise HTTPException(422, "学科参数不正确")
        subject = subject.strip() or None
    report_data = build_report(user["id"], days, subject)
    if not report_data["has_data"]:
        raise HTTPException(
            422,
            "还没有可分析的学习记录（至少完成一次作答或加入一道错题），暂不生成 AI 建议，以免编造内容。",
        )


async def _call_model(*args, **kwargs):
    """间接引用 AI 适配层：测试可 monkeypatch ``app.ai._call_model`` 或本模块函数。"""
    from . import ai as ai_module

    return await ai_module._call_model(*args, **kwargs)


def _statistics_snapshot(report: dict) -> dict:
    """送给模型的统计快照：只放真实聚合数字，避免把整份 90 天明细塞进 prompt。"""
    summary = report["summary"]
    knowledge = report["knowledge"]
    mistakes = report["mistakes"]
    activity = report["activity"]
    active_days = sum(1 for day in activity if day["attempts"])
    last_seven = activity[-7:] if activity else []
    return {
        "range": report["range"],
        "summary": {
            "attempts": summary["attempts"],
            "graded_attempts": summary["graded_attempts"],
            "correct_rate": summary["correct_rate"],
            "mastery_counts": summary["mastery_counts"],
            "due_reviews": summary["due_reviews"],
            "wrong_count": summary["wrong_count"],
            "ai_jobs": summary["ai_jobs"],
        },
        "subjects": [
            {
                "subject": row["subject"],
                "attempts": row["attempts"],
                "accuracy": row["accuracy"],
            }
            for row in report["subjects"]
        ],
        "knowledge": {
            "weak": [
                {"title": item["title"], "attempts": item["attempts"], "accuracy": item["accuracy"]}
                for item in knowledge["weak"][:5]
            ],
            "strong": [
                {"title": item["title"], "attempts": item["attempts"], "accuracy": item["accuracy"]}
                for item in knowledge["strong"][:5]
            ],
            "not_started": [item["title"] for item in knowledge["not_started"][:5]],
            "note": knowledge["note"],
        },
        "mistakes": {
            "total": mistakes["total"],
            "by_reason": mistakes["by_reason"][:5],
            "by_type": mistakes["by_type"][:5],
            "by_kp": [
                {"kp": row["kp"], "count": row["count"]}
                for row in mistakes["by_kp"][:5]
            ],
        },
        "activity": {
            "active_days": active_days,
            "last_7_days_attempts": sum(day["attempts"] for day in last_seven),
            "last_7_days_correct": sum(day["correct"] for day in last_seven),
        },
        "integrations": report.get("integrations") or {},
        "activity_mix": report.get("activity_mix") or {},
        "rule_recommendations": [
            {"title": item["title"], "reason": item["reason"]}
            for item in report["recommendations"]
        ],
    }


def _narrative_prompt(snapshot: dict) -> str:
    payload = json.dumps(snapshot, ensure_ascii=False, indent=2)
    return (
        "你是一位中学学科教研员，负责给一名学生撰写「学情分析文字建议」。\n\n"
        "【硬性规则】\n"
        "1. 只能使用下面给出的统计数据；不得编造任何数字、题目、知识点、排名或班级/学校对比"
        "（知序只有个人数据，没有班级维度）。\n"
        "2. 如果某项统计为空或为 0，直接写「暂无相关记录」，不要推测或补全。\n"
        "3. 正确率的单位是百分比（0–100）；薄弱线是正确率低于 70%；"
        "知识点统计按题目关联、一题多知识点时该题作答计入每个关联知识点。\n"
        "4. 统计快照中的 integrations（AI 课堂/整卷练习/题库产出/AI 助手）与 activity_mix "
        "是各整合模块的真实聚合；引用时必须与快照数字一致，为 0 就写暂无相关记录。\n"
        "5. 输出中文 Markdown，结构固定为四节：\n"
        "   ## 总体表现\n   ## 薄弱知识点\n   ## 错题与复习\n   ## 下一步建议\n"
        "6. 「下一步建议」给 3 条以内、具体可执行的动作；语气平实，不空泛鼓励；全文 300–700 字。\n"
        "7. 结尾不要重复统计表，不要输出 JSON，不要使用 emoji。\n\n"
        "【统计数据（来自学生真实作答/错题/掌握度记录）】\n"
        f"{payload}\n"
    )


async def run_insight_job(job_id, user_id, data, provider, verifier) -> dict:
    """执行 insight-report 任务：聚合真实统计 → 单次模型调用 → 标注 AI 生成的建议文本。"""
    from .ai.jobs import is_job_cancelled, update_job

    params = data.params or {}
    days = _clamp_days(params.get("days", DEFAULT_DAYS))
    subject = (params.get("subject") or None)
    if isinstance(subject, str):
        subject = subject.strip() or None

    update_job(job_id, "running", "insight-collect")
    report_data = build_report(user_id, days, subject)
    if is_job_cancelled(job_id):
        raise asyncio.CancelledError()
    snapshot = _statistics_snapshot(report_data)
    update_job(job_id, "running", "insight-write")

    call = await _call_model(
        provider,
        data.model,
        _narrative_prompt(snapshot),
        token_limit=2200,
        system_prompt=(
            "你是知序学习工作台的学情分析助手。只依据用户提供的真实统计数据写建议，"
            "禁止编造数字、排名或班级对比；所有结论必须能对应到给定统计。"
        ),
        user_id=user_id,
        job_id=job_id,
    )
    content = str((call or {}).get("content") or "").strip()
    if len(content) < 30:
        raise ValueError("模型未返回可用的学习建议文本，请重试或更换模型")
    if is_job_cancelled(job_id):
        raise asyncio.CancelledError()

    provider_id = None
    try:
        provider_id = dict(provider).get("id")
    except (TypeError, ValueError):
        provider_id = getattr(provider, "id", None)
    result = {
        "markdown": content,
        "statistics_snapshot": snapshot,
        "ai_generated": True,
        "ai_notice": "本建议由 AI 生成，仅基于本页真实作答、错题与掌握度统计，仅供参考；不构成排名或评价。",
        "generated_at": _now_iso(),
        "days": days,
        "subject": subject,
        "prompt_version": _prompt_version(),
        "usage": (call or {}).get("usage") or {},
        "calls": [{
            "provider_id": provider_id,
            "model": data.model,
            "usage": (call or {}).get("usage") or {},
        }],
    }
    return result


def init_insight() -> None:
    """幂等初始化：登记 AI 报告任务执行器（无自有表）。"""
    register_jobs()


def register_jobs() -> None:
    """向 `app.ai.jobs` 的任务注册表登记学情报告任务。"""
    from .ai.jobs import register_task_handler

    register_task_handler(
        "insight-report",
        run_insight_job,
        validate_insight_job,
        label="学情报告",
    )
