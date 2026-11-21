"""试卷工坊 · 整卷练习（个人自测）：作答、保存草稿、提交判分、错题联动。

设计要点（个人自测语义，不含作业的发布/收交/班级）：
- ``pl_paper_attempts``：一次整卷练习；创建时在**同一写事务**内把逐题快照冻结到
  ``pl_attempt_items``（题目引用、``snapshot_json``、``score/max_score``、配图引用、版式位置），
  同时把试卷元数据存进 ``paper_snapshot_json``。之后改题/删题/重排/删卷都不影响已开始的作答；
  历史作答没有冻结行时回退现读当前试卷（``frozen=false`` + 「历史题面不可追溯」提示），不 500。
- ``pl_paper_answers``：逐题作答与判分结果（``UNIQUE(attempt_id,item_position)``，PATCH 幂等）；
  题序与分值一律取自冻结行，重新提交/补答只更新作答行。
- 判分：单选/多选按快照选项 label 精确比对；填空按归一化文本比对（支持「；/ 或」多答案）；
  主观题与含主观子题的混合题标记 ``pending``（不硬判、不计分），结果页展示参考答案供自测核对。
  判分使用的参考答案与分值同样来自冻结快照。
- 错题联动：``bank:qid`` 来源的题（客观判错或主观待核验）写入既有 ``wrong_questions``（复用其表结构、
  ON CONFLICT 累加），并同步 ``mastery`` 降为「未掌握」；``app:ID`` 应用题库题目只在本次练习结果中记录
  （应用题库题没有只读题库 qid，错题本按 qid 索引不适用），在结果里明确提示。
- 删卷保留历史：``pl_paper_attempts.paper_id`` 不再挂 ``ON DELETE CASCADE``（旧库由
  ``ensure_tables()`` 做一次等价迁移），删卷后练习记录仍可回看，卷面用冻结快照/元数据补齐。

避免新增主键表登记（``app/db.py::_ID_TABLES`` 为 Lead 独占文件）：本模块插入后不使用 ``lastrowid``，
改用同一写事务内的 ``SELECT id`` 取主键，SQLite / PostgreSQL 语义一致。
"""
from __future__ import annotations

import base64
import json
import re
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field

from .. import question_ref
from ..auth import current_user
from ..db import connection, ensure_column, postgres_enabled, utcnow, write_lock
from . import papers
from .db import json_dump, json_load

router = APIRouter()

CHOICE_TYPES = ("SINGLE", "MULTIPLE")
TYPE_LABELS = {
    "SINGLE": "单选题", "MULTIPLE": "多选题", "BLANK": "填空题",
    "SUBJECTIVE": "主观题", "MIXED": "混合题",
}
#: 平台精选题的审核状态文案（与广场公开页/练习来源徽标同口径）。
_REVIEW_STATUS_LABELS = {
    "community": "社区待核验",
    "verified": "社区核验通过",
    "featured": "平台精选",
    "withdrawn": "已撤回",
    "disputed": "有争议或待修订",
}
MAX_ANSWER_BYTES = 16000
MAX_ATTEMPT_ANSWERS = 200


# ---------------------------------------------------------------------------
# 表（幂等；在 papers.ensure_tables 之后调用）
# ---------------------------------------------------------------------------

# 作答记录主表：``paper_id`` 故意不挂 ``ON DELETE CASCADE``——删卷后练习记录仍要可回看，
# 卷面由 ``pl_attempt_items`` 冻结快照与 ``paper_snapshot_json`` 补齐。旧库在
# ``ensure_tables()`` 里做一次等价迁移（SQLite 重建表 / PostgreSQL 删外键约束）。
_ATTEMPT_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS pl_paper_attempts (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    paper_id INTEGER NOT NULL,
    paper_version INTEGER NOT NULL DEFAULT 1,
    paper_snapshot_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'in_progress',
    answers_json TEXT NOT NULL DEFAULT '{}',
    score REAL NOT NULL DEFAULT 0,
    max_score REAL NOT NULL DEFAULT 0,
    detail_json TEXT NOT NULL DEFAULT '{}',
    started_at TEXT NOT NULL,
    submitted_at TEXT,
    created_at TEXT NOT NULL
);
"""

_SCHEMA = _ATTEMPT_TABLE_SQL + """
CREATE INDEX IF NOT EXISTS pl_paper_attempts_user ON pl_paper_attempts(user_id, paper_id, id);
CREATE INDEX IF NOT EXISTS pl_paper_attempts_paper ON pl_paper_attempts(paper_id, id);

CREATE TABLE IF NOT EXISTS pl_attempt_items (
    id INTEGER PRIMARY KEY,
    attempt_id INTEGER NOT NULL REFERENCES pl_paper_attempts(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL,
    item_position INTEGER NOT NULL,
    paper_item_id INTEGER,
    question_ref TEXT NOT NULL DEFAULT '',
    section TEXT NOT NULL DEFAULT '',
    snapshot_json TEXT NOT NULL DEFAULT '{}',
    score REAL NOT NULL DEFAULT 0,
    max_score REAL NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    UNIQUE(attempt_id, item_position)
);
CREATE INDEX IF NOT EXISTS pl_attempt_items_attempt ON pl_attempt_items(attempt_id, item_position);
CREATE INDEX IF NOT EXISTS pl_attempt_items_user ON pl_attempt_items(user_id);

CREATE TABLE IF NOT EXISTS pl_paper_answers (
    id INTEGER PRIMARY KEY,
    attempt_id INTEGER NOT NULL REFERENCES pl_paper_attempts(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL,
    item_position INTEGER NOT NULL,
    question_ref TEXT NOT NULL DEFAULT '',
    answer_json TEXT NOT NULL DEFAULT 'null',
    result TEXT NOT NULL DEFAULT 'pending',
    score REAL NOT NULL DEFAULT 0,
    max_score REAL NOT NULL DEFAULT 0,
    detail_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    UNIQUE(attempt_id, item_position)
);
CREATE INDEX IF NOT EXISTS pl_paper_answers_attempt ON pl_paper_answers(attempt_id, item_position);
CREATE INDEX IF NOT EXISTS pl_paper_answers_user ON pl_paper_answers(user_id);
"""

# 旧库增量列：作答冻结的试卷元数据（新库已在 CREATE TABLE 里；老库用 ensure_column 补齐）。
_ATTEMPT_EXTRA_COLUMNS = (
    ("paper_snapshot_json", "TEXT NOT NULL DEFAULT '{}'"),
)

_ATTEMPT_COLUMNS = (
    "id", "user_id", "paper_id", "paper_version", "paper_snapshot_json", "status",
    "answers_json", "score", "max_score", "detail_json", "started_at", "submitted_at", "created_at",
)


def _migrate_attempt_paper_fk(con) -> None:
    """移除旧库作答表到 ``pl_papers`` 的 ``ON DELETE CASCADE``（删卷不再连带删除练习记录）。

    - SQLite 不支持 DROP CONSTRAINT，按官方 12 步流程重建表（foreign_keys=OFF 下复制/改名）；
    - PostgreSQL 直接按系统目录找到外键约束并删除；
    - 幂等：新库或已迁移库不存在该外键时什么都不做。
    """
    # 旧库先补齐新增列，下面重建表时按完整列清单复制（SQLite / PostgreSQL 都需要）。
    for column, definition in _ATTEMPT_EXTRA_COLUMNS:
        ensure_column(con, "pl_paper_attempts", column, definition)
    if postgres_enabled():
        row = con.execute(
            "SELECT c.conname FROM pg_constraint c JOIN pg_class t ON t.oid=c.conrelid "
            "WHERE t.relname='pl_paper_attempts' AND c.contype='f' "
            "AND t.relnamespace=current_schema()::regnamespace "
            "AND pg_get_constraintdef(c.oid) LIKE '%REFERENCES pl_papers%'"
        ).fetchone()
        if row is not None:
            name = str(row[0]).replace('"', '""')
            con.execute(f'ALTER TABLE pl_paper_attempts DROP CONSTRAINT IF EXISTS "{name}"')
        return
    fks = con.execute("PRAGMA foreign_key_list(pl_paper_attempts)").fetchall()
    cascade = any(
        str(fk["table"]).lower() == "pl_papers" and str(fk["on_delete"]).upper() == "CASCADE"
        for fk in fks
    )
    if not cascade:
        return
    new_table_sql = _ATTEMPT_TABLE_SQL.replace(
        "pl_paper_attempts", "pl_paper_attempts_v2", 1,
    )
    columns = ", ".join(_ATTEMPT_COLUMNS)
    con.execute("PRAGMA foreign_keys=OFF")
    try:
        con.execute("BEGIN")
        con.execute(new_table_sql)
        con.execute(
            f"INSERT INTO pl_paper_attempts_v2({columns}) SELECT {columns} FROM pl_paper_attempts"
        )
        con.execute("DROP TABLE pl_paper_attempts")
        con.execute("ALTER TABLE pl_paper_attempts_v2 RENAME TO pl_paper_attempts")
        con.execute("COMMIT")
    except Exception:
        try:
            con.execute("ROLLBACK")
        except Exception:  # noqa: BLE001 - BEGIN 失败时无事务可回滚
            pass
        raise
    finally:
        con.execute("PRAGMA foreign_keys=ON")
    # 重建后索引随旧表丢弃，按完整 schema 幂等补回（顺带补建 pl_attempt_items）。
    con.executescript(_SCHEMA)


def ensure_tables() -> None:
    with connection() as con:
        con.executescript(_SCHEMA)
        _migrate_attempt_paper_fk(con)


# ---------------------------------------------------------------------------
# 通用工具
# ---------------------------------------------------------------------------

def _row_dict(row) -> dict:
    return dict(row) if row is not None else {}


def _paper_row(paper_id: int, user_id: int) -> dict:
    return papers.fetch_paper(paper_id, user_id)


def _items(paper_id: int, user_id: int) -> list[dict]:
    return papers.list_items(paper_id, user_id)


def _snapshot(item: dict) -> dict:
    snapshot = json_load(item.get("snapshot_json"), {})
    return snapshot if isinstance(snapshot, dict) else {}


def _content(snapshot: dict) -> dict:
    content = snapshot.get("content")
    return content if isinstance(content, dict) else {}


def _option_labels(snapshot: dict) -> set[str]:
    content = _content(snapshot)
    labels = set()
    for option in content.get("options") or []:
        if isinstance(option, dict) and option.get("label"):
            labels.add(str(option["label"]).strip().upper())
    return labels


def _reference_choice_labels(snapshot: dict) -> list[str]:
    """从快照里提取选择题标准答案 label（app 快照是 label 数组；只读题库快照是 answer_text）。"""
    content = _content(snapshot)
    labels = _option_labels(snapshot)
    found: list[str] = []
    for value in content.get("answers") or []:
        found.extend(_labels_from_value(value))
    if not found:
        found.extend(_labels_from_value(content.get("answer_text")))
    result = []
    for label in found:
        if label in labels and label not in result:
            result.append(label)
    return result


def _labels_from_value(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        labels = []
        for item in value:
            labels.extend(_labels_from_value(item))
        return labels
    text = str(value).strip().upper()
    if not text:
        return []
    if text.startswith("$") or len(text) > 8:
        return []
    letters = re.findall(r"[A-Z]", text)
    return letters if letters else []


def _submitted_choice(answer) -> set[str]:
    values = []
    if isinstance(answer, (list, tuple, set)):
        for item in answer:
            values.extend(_labels_from_value(item))
    elif isinstance(answer, dict):
        for key in ("value", "values", "labels", "answer"):
            if key in answer:
                return _submitted_choice(answer[key])
    else:
        values = _labels_from_value(answer)
    return {value for value in values}


_MATH_REPLACEMENTS = (
    (r"\\dfrac", r"\\frac"), (r"\\tfrac", r"\\frac"), (r"\\left", ""), (r"\\right", ""),
    (r"\\!", ""), (r"\\,", ""), (r"\\;", ""), (r"\\:", ""), (r"\\ ", ""),
    (r"\\cdot", "*"), (r"\\times", "*"), (r"\\div", "/"),
    (r"\\pi", "π"), (r"\\sqrt", "√"),
)


def _normalize_text(value) -> str:
    text = str(value if value is not None else "").strip()
    if not text:
        return ""
    text = text.replace("$", "")
    for pattern, replacement in _MATH_REPLACEMENTS:
        text = re.sub(pattern, replacement, text)
    text = text.replace("　", " ")
    text = re.sub(r"[\s\u00a0]+", "", text)
    text = text.replace("（", "(").replace("）", ")").replace("，", ",").replace("。", ".")
    text = text.replace("；", ";").replace("、", ",")
    return text.casefold()


def _submitted_text(answer) -> str:
    if answer is None:
        return ""
    if isinstance(answer, str):
        return answer.strip()
    if isinstance(answer, (list, tuple)):
        return "；".join(str(item).strip() for item in answer if str(item).strip())
    if isinstance(answer, dict):
        for key in ("value", "text", "answer", "answers", "values"):
            if key in answer:
                return _submitted_text(answer[key])
    return str(answer).strip()


def _blank_references(snapshot: dict) -> list[list[str]]:
    """填空/文本参考答案的候选组：每项是「一个空」的可接受答案列表。"""
    content = _content(snapshot)
    raw_values: list[str] = []
    for value in content.get("answers") or []:
        text = _submitted_text(value)
        if text:
            raw_values.append(text)
    if not raw_values:
        answer_text = _submitted_text(content.get("answer_text"))
        if answer_text:
            raw_values.append(answer_text)
    groups: list[list[str]] = []
    for raw in raw_values:
        alternatives = [part.strip() for part in re.split(r"[;；]|(?:\s+或\s+)|(?:\s*\|\s*)", raw) if part.strip()]
        groups.append(alternatives or [raw])
    return groups


def _judge_blank(snapshot: dict, answer) -> tuple[str, str]:
    groups = _blank_references(snapshot)
    submitted = answer
    submitted_values: list[str]
    if isinstance(submitted, (list, tuple)):
        submitted_values = [str(item) for item in submitted]
    else:
        text = _submitted_text(submitted)
        if text and ("；" in text or "\n" in text):
            submitted_values = [part for part in re.split(r"[;；\n]", text)]
        else:
            submitted_values = [text]
    submitted_values = [value for value in submitted_values if str(value).strip()]
    if not submitted_values:
        return "incorrect", "未作答"
    if not groups:
        return "pending", "快照缺少参考答案，标记待核验"
    if len(groups) == 1:
        targets = {_normalize_text(value) for value in groups[0]}
        normalized = {_normalize_text(value) for value in submitted_values}
        if normalized & targets:
            return "correct", ""
        # 多个空被合写成一个字符串时，逐空拼接后比较
        joined_reference = {_normalize_text("".join(group)) for group in groups}
        if _normalize_text("".join(submitted_values)) in joined_reference:
            return "correct", ""
        return "incorrect", "答案与参考答案不一致"
    if len(submitted_values) != len(groups):
        return "incorrect", f"参考答案为 {len(groups)} 个空，本次提交 {len(submitted_values)} 个答案"
    for index, group in enumerate(groups):
        targets = {_normalize_text(value) for value in group}
        if _normalize_text(submitted_values[index]) not in targets:
            return "incorrect", f"第 {index + 1} 空答案不正确"
    return "correct", ""


def _answer_for_part(answer, index: int):
    if isinstance(answer, dict):
        parts = answer.get("parts")
        if isinstance(parts, dict):
            return parts.get(str(index), parts.get(index))
        if isinstance(parts, list):
            for part in parts:
                if isinstance(part, dict) and int(part.get("index") or 0) == int(index):
                    return part.get("answer", part.get("value"))
            if len(parts) >= index:
                entry = parts[index - 1]
                if isinstance(entry, dict):
                    return entry.get("answer", entry.get("value"))
                return entry
    if isinstance(answer, (list, tuple)) and len(answer) >= index:
        entry = answer[index - 1]
        if isinstance(entry, dict):
            return entry.get("answer", entry.get("value"))
        return entry
    return None


def _reference_payload(snapshot: dict) -> dict:
    content = _content(snapshot)
    answers = content.get("answers")
    if not isinstance(answers, list):
        answers = []
    answer_text = _submitted_text(content.get("answer_text"))
    parts = []
    for index, part in enumerate(content.get("parts") or [], start=1):
        if not isinstance(part, dict):
            continue
        part_answers = part.get("answers") if isinstance(part.get("answers"), list) else []
        parts.append({
            "index": int(part.get("index") or index),
            "type": part.get("type") or "SUBJECTIVE",
            "stem": str(part.get("stem") or ""),
            "answers": [str(item) for item in part_answers if str(item).strip()],
            "explanation": str(part.get("explanation") or ""),
            "rubric": str(part.get("rubric") or ""),
        })
    return {
        "answers": [str(item) for item in answers if str(item).strip()],
        "answer_text": answer_text,
        "explanation": str(content.get("explanation") or ""),
        "rubric": str(content.get("rubric") or ""),
        "parts": parts,
    }


def _judge_item(item: dict, answer) -> dict:
    """单题判分：返回 {judgement, score, max_score, detail}。"""
    snapshot = _snapshot(item)
    qtype = str(snapshot.get("type") or "SUBJECTIVE").upper()
    max_score = round(float(item.get("score") or 0), 2)
    detail: dict = {
        "judgement": "pending",
        "score": 0.0,
        "max_score": max_score,
        "reason": "",
        "reference": _reference_payload(snapshot),
    }
    if qtype in CHOICE_TYPES:
        reference = _reference_choice_labels(snapshot)
        submitted = _submitted_choice(answer)
        if not reference:
            detail.update({"judgement": "pending", "reason": "题目快照缺少可判分的选项答案，标记待核验"})
        elif not submitted:
            detail.update({"judgement": "incorrect", "reason": "未作答"})
        elif submitted == set(reference):
            detail.update({"judgement": "correct", "score": max_score})
        else:
            detail.update({"judgement": "incorrect", "reason": "选择与参考答案不一致"})
        return detail
    if qtype == "BLANK":
        judgement, reason = _judge_blank(snapshot, answer)
        detail.update({
            "judgement": judgement,
            "score": max_score if judgement == "correct" else 0.0,
            "reason": reason,
        })
        return detail
    if qtype == "MIXED":
        parts = [part for part in (_content(snapshot).get("parts") or []) if isinstance(part, dict)]
        part_details = []
        has_incorrect = False
        has_pending = False
        all_choice = True
        for index, part in enumerate(parts, start=1):
            part_index = int(part.get("index") or index)
            part_type = str(part.get("type") or "SUBJECTIVE").upper()
            part_answer = _answer_for_part(answer, part_index)
            part_reference = _reference_payload({"content": part})
            if part_type in CHOICE_TYPES:
                labels = _option_labels(part)
                reference = [label for label in _reference_choice_labels_from_part(part) if label in labels]
                submitted = _submitted_choice(part_answer)
                if not reference:
                    judgement, reason = "pending", "子题缺少可判分答案"
                elif not submitted:
                    judgement, reason = "incorrect", "子题未作答"
                elif submitted == set(reference):
                    judgement, reason = "correct", ""
                else:
                    judgement, reason = "incorrect", "子题选择与参考答案不一致"
            else:
                all_choice = False
                judgement, reason = "pending", "主观子题需人工核对"
            if judgement == "incorrect":
                has_incorrect = True
            if judgement == "pending":
                has_pending = True
            part_details.append({
                "index": part_index, "type": part_type, "judgement": judgement,
                "reason": reason, "reference": part_reference,
            })
        if not parts:
            detail.update({"judgement": "pending", "reason": "混合题缺少子题快照，标记待核验"})
        elif has_incorrect:
            detail.update({"judgement": "incorrect", "reason": "存在答错的子题", "parts": part_details})
        elif has_pending:
            detail.update({"judgement": "pending", "reason": "含主观子题，需人工核对", "parts": part_details})
        elif all_choice:
            detail.update({"judgement": "correct", "score": max_score, "parts": part_details})
        else:
            detail.update({"judgement": "pending", "parts": part_details})
        return detail
    detail.update({"judgement": "pending", "reason": "主观题不自动判分，标记待核验；请对照参考答案自测"})
    return detail


def _reference_choice_labels_from_part(part: dict) -> list[str]:
    found: list[str] = []
    for value in part.get("answers") or []:
        found.extend(_labels_from_value(value))
    return found


# ---------------------------------------------------------------------------
# 错题联动（bank:qid 与 app:ID 都写入既有 wrong_questions；app 引用由练习侧统一复习队列读取）
# ---------------------------------------------------------------------------

def _wrong_reason(result_detail: dict) -> str:
    return "整卷练习答错" if result_detail.get("judgement") == "incorrect" else "整卷练习待核验"


def _record_wrong(con, user_id: int, qid: str, judgement: str, entry_key: str | None = None) -> bool:
    """把错题写入既有 ``wrong_questions``（结构同 app/practice.py 的错题记录）。

    ``entry_key`` 为幂等键（整卷逐题 = ``paper:<attempt_id>:<item_position>``）：
    同一键只入账一次，重复提交/重试不会重复累加 failures 与掌握度。唯一性由
    ``submission_ledger`` 主键保证；未提供 ``entry_key`` 时保持旧的非幂等语义。
    """
    now = utcnow()
    due = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    reason = "整卷练习答错" if judgement == "incorrect" else "整卷练习待核验"
    if entry_key is not None:
        try:
            claimed = con.execute(
                "INSERT INTO submission_ledger(user_id,kind,ledger_key,qid,attempt_id,created_at) "
                "VALUES(?,?,?,?,NULL,?) ON CONFLICT(user_id,kind,ledger_key) DO NOTHING",
                (int(user_id), "paper_item", str(entry_key), str(qid), now),
            ).rowcount == 1
        except Exception:  # noqa: BLE001 - 旧库未建台账表时降级为旧语义，不阻塞判分
            claimed = True
        if not claimed:
            return False
    try:
        con.execute(
            "INSERT INTO wrong_questions(user_id,qid,reason,failures,streak,next_review_at,added_at,"
            "last_result,interval_days,review_count,archived,last_wrong_at,error_tags_json,"
            "last_mastery_push_attempt) "
            "VALUES(?,?,?,1,0,?,?,?,1,0,0,?,'[]',NULL) "
            "ON CONFLICT(user_id,qid) DO UPDATE SET failures=wrong_questions.failures+1,"
            "streak=0,next_review_at=excluded.next_review_at,last_result=excluded.last_result,"
            "interval_days=1,archived=0,last_wrong_at=excluded.last_wrong_at,"
            "reason=excluded.reason,last_mastery_push_attempt=NULL",
            (user_id, qid, reason, due, now, judgement, now),
        )
        if judgement == "incorrect":
            con.execute(
                "INSERT INTO mastery(user_id,qid,level,updated_at) VALUES(?,?,'未掌握',?) "
                "ON CONFLICT(user_id,qid) DO UPDATE SET level='未掌握',updated_at=excluded.updated_at",
                (user_id, qid, now),
            )
        return True
    except Exception:  # 题库记录已不存在等异常不阻塞提交（结果里仍会记录）
        if entry_key is not None:
            try:
                con.execute(
                    "DELETE FROM submission_ledger WHERE user_id=? AND kind=? AND ledger_key=?",
                    (int(user_id), "paper_item", str(entry_key)),
                )
            except Exception:  # noqa: BLE001 - 回收失败不影响主流程
                pass
        return False


# ---------------------------------------------------------------------------
# 序列化（练习卷面不泄露答案；提交后才返回参考解析）
# ---------------------------------------------------------------------------

def _practice_item(item: dict, index: int) -> dict:
    snapshot = _snapshot(item)
    content = _content(snapshot)
    payload_content = {
        "stem": content.get("stem") or "",
        "options": content.get("options") or [],
        "images": content.get("images") or [],
        "material": content.get("material"),
        "parts": [],
    }
    for part_index, part in enumerate(content.get("parts") or [], start=1):
        if not isinstance(part, dict):
            continue
        payload_content["parts"].append({
            "index": int(part.get("index") or part_index),
            "type": part.get("type") or "SUBJECTIVE",
            "stem": part.get("stem") or "",
            "options": part.get("options") or [],
        })
    tags = snapshot.get("knowledge_tags")
    raw_ref = str(item.get("question_ref") or "")
    try:
        normalized_ref = question_ref.normalize(raw_ref) if raw_ref else ""
    except question_ref.QuestionRefError:
        normalized_ref = raw_ref
    source = str(snapshot.get("source") or "app")
    review_status = str(snapshot.get("review_status") or "")
    return {
        "id": int(item.get("id") or 0),
        "item_position": int(index) + 1,
        "question_ref": raw_ref,
        "ref": normalized_ref,
        "source": source,
        "source_label": question_ref.SOURCE_LABELS.get(source, source),
        # 平台精选来源标注（bank/app 为空；回看/结果页据此显示来源徽标与公开题号）。
        "public_id": str(snapshot.get("public_id") or ""),
        "review_status": review_status,
        "review_status_label": str(snapshot.get("review_status_label") or "")
        or _REVIEW_STATUS_LABELS.get(review_status, ""),
        "published_version": snapshot.get("published_version"),
        "featured": bool(snapshot.get("featured")),
        "source_notice": str(snapshot.get("notice") or ""),
        "source_declaration": snapshot.get("source_declaration") or {},
        "type": str(snapshot.get("type") or "SUBJECTIVE").upper(),
        "type_label": TYPE_LABELS.get(str(snapshot.get("type") or "").upper(), "题目"),
        "subject": snapshot.get("subject") or "",
        "difficulty": snapshot.get("difficulty"),
        "knowledge_tags": list(tags) if isinstance(tags, list) else [],
        "score": float(item.get("score") or 0),
        "max_score": float(item.get("score") or 0),
        "section": item.get("section") or "",
        "content": payload_content,
    }


def _attempt_public(row: dict, *, item_count: int | None = None,
                    frozen: bool = True, paper_deleted: bool = False) -> dict:
    detail = json_load(row.get("detail_json"), {})
    detail = detail if isinstance(detail, dict) else {}
    summary = detail.get("summary") if isinstance(detail.get("summary"), dict) else {}
    return {
        "id": int(row["id"]),
        "paper_id": int(row["paper_id"]),
        "paper_version": int(row.get("paper_version") or 1),
        "frozen": bool(frozen),
        "paper_deleted": bool(paper_deleted),
        "status": row.get("status") or "in_progress",
        "score": round(float(row.get("score") or 0), 2),
        "max_score": round(float(row.get("max_score") or 0), 2),
        "item_count": int(item_count if item_count is not None else summary.get("item_count") or 0),
        "correct_count": int(summary.get("correct_count") or 0),
        "incorrect_count": int(summary.get("incorrect_count") or 0),
        "pending_count": int(summary.get("pending_count") or 0),
        "started_at": row.get("started_at") or "",
        "submitted_at": row.get("submitted_at"),
    }


def _saved_answers(con, attempt_id: int) -> dict[int, object]:
    rows = con.execute(
        "SELECT item_position, answer_json FROM pl_paper_answers WHERE attempt_id=?", (attempt_id,)
    ).fetchall()
    return {int(row["item_position"]): json_load(row["answer_json"], None) for row in rows}


def _answer_rows(con, attempt_id: int) -> dict[int, dict]:
    rows = con.execute(
        "SELECT * FROM pl_paper_answers WHERE attempt_id=? ORDER BY item_position", (attempt_id,)
    ).fetchall()
    return {int(row["item_position"]): _row_dict(row) for row in rows}


# ---------------------------------------------------------------------------
# 作答侧读取：冻结快照优先，旧作答回退现读
# ---------------------------------------------------------------------------

def _frozen_item_rows(con, attempt_id: int) -> list[dict]:
    """读取一次作答的逐题冻结快照，形状与 ``papers.list_items`` 的行兼容。"""
    rows = con.execute(
        "SELECT * FROM pl_attempt_items WHERE attempt_id=? ORDER BY item_position",
        (attempt_id,),
    ).fetchall()
    items = []
    for raw in rows:
        row = _row_dict(raw)
        position = int(row.get("item_position") or 0)
        items.append({
            "id": int(row.get("paper_item_id") or 0),
            "item_position": position,
            "position": position - 1,
            "question_ref": row.get("question_ref") or "",
            "section": row.get("section") or "",
            "snapshot_json": row.get("snapshot_json") or "{}",
            "score": float(row.get("score") or 0),
            "max_score": float(row.get("max_score") if row.get("max_score") is not None
                               else row.get("score") or 0),
        })
    return items


def _attempt_items(attempt: dict) -> tuple[list[dict], bool]:
    """作答侧题目：优先冻结快照；无冻结行（旧作答）回退现读当前试卷并返回 ``frozen=False``。"""
    attempt_id = int(attempt["id"])
    with connection() as con:
        frozen = _frozen_item_rows(con, attempt_id)
    if frozen:
        return frozen, True
    paper_id = int(attempt["paper_id"])
    user_id = int(attempt["user_id"])
    if papers.find_paper(paper_id, user_id) is None:
        return [], False
    return papers.list_items(paper_id, user_id), False


def _paper_snapshot(paper: dict) -> dict:
    """作答开始时冻结的试卷元数据（用于删卷后的历史回看）。"""
    layout = json_load(paper.get("layout_json"), {})
    return {
        "title": paper.get("title") or "",
        "kind": paper.get("kind") or "manual",
        "subject": paper.get("subject") or "",
        "grade": paper.get("grade") or "",
        "instructions": paper.get("instructions") or "",
        "layout": layout if isinstance(layout, dict) else {},
        "status": paper.get("status") or "draft",
        "published_at": paper.get("published_at"),
        "source": paper.get("source") or "manual",
        "version": int(paper.get("version") or 1),
        "created_at": paper.get("created_at") or "",
        "updated_at": paper.get("updated_at") or "",
    }


def _attempt_paper(attempt: dict) -> tuple[dict, bool]:
    """作答对应的试卷：现卷存在时读现卷；试卷已删除时用开始时冻结的元数据补齐。"""
    paper_id = int(attempt["paper_id"])
    user_id = int(attempt["user_id"])
    paper = papers.find_paper(paper_id, user_id)
    if paper is not None:
        return paper, False
    snapshot = json_load(attempt.get("paper_snapshot_json"), {})
    snapshot = snapshot if isinstance(snapshot, dict) else {}
    layout = snapshot.get("layout") if isinstance(snapshot.get("layout"), dict) else {}
    return {
        "id": paper_id,
        "user_id": user_id,
        "title": str(snapshot.get("title") or "已删除的试卷"),
        "kind": str(snapshot.get("kind") or "manual"),
        "subject": str(snapshot.get("subject") or ""),
        "grade": str(snapshot.get("grade") or ""),
        "instructions": str(snapshot.get("instructions") or ""),
        "layout_json": json_dump(layout),
        "status": str(snapshot.get("status") or "draft"),
        "published_at": snapshot.get("published_at"),
        "source": str(snapshot.get("source") or "manual"),
        "version": int(snapshot.get("version") or attempt.get("paper_version") or 1),
        "created_at": str(snapshot.get("created_at") or attempt.get("created_at") or ""),
        "updated_at": str(snapshot.get("updated_at") or ""),
    }, True


def _frozen_counts(con, attempt_ids: list[int]) -> dict[int, int]:
    ids = [int(value) for value in attempt_ids if int(value or 0)]
    if not ids:
        return {}
    placeholders = ",".join("?" for _ in ids)
    rows = con.execute(
        f"SELECT attempt_id, COUNT(*) FROM pl_attempt_items WHERE attempt_id IN ({placeholders}) "
        "GROUP BY attempt_id",
        tuple(ids),
    ).fetchall()
    return {int(row[0]): int(row[1]) for row in rows}


def _attempt_notice(frozen: bool, paper_deleted: bool) -> str:
    """作答侧提示：正常冻结时为空（前端显示固定文案），旧作答/删卷时说明可追溯性。"""
    if frozen and not paper_deleted:
        return ""
    if frozen:
        return "试卷已删除，本次练习仍按开始时的冻结题面展示；作答、判分与历史回看不受影响。"
    if paper_deleted:
        return "该练习创建于题面冻结之前，且试卷已被删除，历史题面不可追溯。"
    return "该练习创建于题面冻结之前，历史题面不可追溯；以下按试卷当前内容展示，修改试卷会影响回看。"


def _attempt_payload(attempt: dict, paper: dict, items: list[dict], *,
                     answers: dict | None = None, answer_rows: dict | None = None,
                     frozen: bool = True, paper_deleted: bool = False) -> dict:
    submitted = (attempt.get("status") or "") == "submitted"
    saved = answers or {}
    rows = answer_rows or {}
    detail = json_load(attempt.get("detail_json"), {})
    detail = detail if isinstance(detail, dict) else {}
    detail_items = {}
    for entry in detail.get("items") or []:
        if isinstance(entry, dict):
            detail_items[int(entry.get("item_position") or 0)] = entry
    payload_items = []
    for index, item in enumerate(items):
        position = int(index) + 1
        practice_item = _practice_item(item, index)
        practice_item["answer"] = saved.get(position)
        if submitted:
            union = detail_items.get(position) or {}
            practice_item["judgement"] = union.get("judgement") or "pending"
            practice_item["score"] = union.get("score") or 0.0
            practice_item["feedback"] = union.get("reason") or ""
            practice_item["reference"] = union.get("reference") or _reference_payload(_snapshot(item))
            part_judgements = {
                int(part.get("index") or 0): part for part in (union.get("parts") or [])
                if isinstance(part, dict)
            }
            for part in practice_item["content"]["parts"]:
                judged_part = part_judgements.get(int(part.get("index") or 0))
                if judged_part:
                    part["judgement"] = judged_part.get("judgement") or "pending"
                    part["feedback"] = judged_part.get("reason") or ""
                    part["reference"] = judged_part.get("reference") or {}
        payload_items.append(practice_item)
    payload = {
        "attempt": _attempt_public(attempt, item_count=len(items),
                                   frozen=frozen, paper_deleted=paper_deleted),
        "paper": papers.public_paper(paper, item_count=len(items),
                                     total_score=round(sum(float(i.get("score") or 0) for i in items), 2)),
        "items": payload_items,
        "frozen": bool(frozen),
        "paper_deleted": bool(paper_deleted),
        "notice": _attempt_notice(bool(frozen), bool(paper_deleted)),
    }
    if paper_deleted:
        payload["paper"]["deleted"] = True
    if submitted:
        payload["result"] = {
            "summary": detail.get("summary") or {},
            "wrong_added": detail.get("wrong_added") or [],
            "app_bank_unlinked": detail.get("app_bank_unlinked") or [],
            "app_wrong_added": detail.get("app_wrong_added") or [],
            "official_bank_unlinked": detail.get("official_bank_unlinked") or [],
            "official_wrong_added": detail.get("official_wrong_added") or [],
            "notice": detail.get("notice") or "",
        }
    return payload


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

class AnswerPatchInput(BaseModel):
    item_position: int = Field(ge=1, le=MAX_ATTEMPT_ANSWERS)
    answer: object = None


class SubmitInput(BaseModel):
    answers: list[dict] | None = Field(default=None, max_length=MAX_ATTEMPT_ANSWERS)


def _owned_attempt(attempt_id: int, user_id: int) -> dict:
    with connection() as con:
        row = con.execute(
            "SELECT * FROM pl_paper_attempts WHERE id=? AND user_id=?", (attempt_id, user_id)
        ).fetchone()
    if row is None:
        raise HTTPException(404, "未找到该练习记录")
    return _row_dict(row)


def _validate_answer_size(answer) -> None:
    try:
        encoded = json_dump(answer)
    except Exception:
        raise HTTPException(422, "作答内容无法保存为 JSON") from None
    if len(encoded.encode("utf-8")) > MAX_ANSWER_BYTES:
        raise HTTPException(413, "作答内容过长，请精简后再保存")


def _save_answer(con, attempt: dict, position: int, answer) -> None:
    _validate_answer_size(answer)
    now = utcnow()
    written = json_dump(answer)
    con.execute(
        "INSERT INTO pl_paper_answers(attempt_id,user_id,item_position,question_ref,answer_json,"
        "result,score,max_score,detail_json,created_at) "
        "VALUES(?,?,?,?,?,'pending',0,0,'{}',?) "
        "ON CONFLICT(attempt_id,item_position) DO UPDATE SET "
        "answer_json=excluded.answer_json, result='pending', score=0, detail_json='{}', created_at=excluded.created_at",
        (int(attempt["id"]), int(attempt["user_id"]), int(position), "", written, now),
    )


def _sync_answers_json(con, attempt_id: int) -> None:
    rows = con.execute(
        "SELECT item_position, answer_json FROM pl_paper_answers WHERE attempt_id=?", (attempt_id,)
    ).fetchall()
    mapping = {str(int(row["item_position"])): json_load(row["answer_json"], None) for row in rows}
    con.execute(
        "UPDATE pl_paper_attempts SET answers_json=? WHERE id=?", (json_dump(mapping), attempt_id)
    )


def _insert_frozen_items(con, attempt_id: int, user_id: int, items: list[dict], now: str) -> None:
    """把 ``papers.list_items`` 行冻结进 ``pl_attempt_items``（调用方负责事务与顺序）。"""
    for index, item in enumerate(items, start=1):
        con.execute(
            "INSERT INTO pl_attempt_items(attempt_id,user_id,item_position,paper_item_id,"
            "question_ref,section,snapshot_json,score,max_score,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                attempt_id, int(user_id), index, int(item.get("id") or 0),
                item.get("question_ref") or "", item.get("section") or "",
                item.get("snapshot_json") or "{}",
                float(item.get("score") or 0), float(item.get("score") or 0), now,
            ),
        )


@router.post("/papers/{paper_id}/attempts")
def create_attempt(paper_id: int, user=Depends(current_user)):
    """创建一次整卷练习（个人自测；不要求试卷已发布，草稿/待发布/已发布均可练）。"""
    paper = _paper_row(paper_id, user["id"])
    items = _items(paper_id, user["id"])
    if not items:
        raise HTTPException(422, "试卷还没有题目，不能开始整卷练习")
    usable = [item for item in items if str(_snapshot(item).get("content", {}).get("stem") or "").strip()
              or (_content(_snapshot(item)).get("parts"))]
    if not usable:
        raise HTTPException(422, "试卷题目快照不可用，请重新建卷后再练习")
    now = utcnow()
    with connection() as con:
        write_lock(con, "paperlab-practice")
        con.execute(
            "INSERT INTO pl_paper_attempts(user_id,paper_id,paper_version,paper_snapshot_json,"
            "status,answers_json,score,max_score,detail_json,started_at,submitted_at,created_at) "
            "VALUES(?,?,?,?,'in_progress','{}',0,?, '{}',?,NULL,?)",
            (
                user["id"], paper_id, int(paper.get("version") or 1),
                json_dump(_paper_snapshot(paper)),
                round(sum(float(item.get("score") or 0) for item in items), 2),
                now, now,
            ),
        )
        attempt_id = int(con.execute(
            "SELECT id FROM pl_paper_attempts WHERE user_id=? AND paper_id=? ORDER BY id DESC LIMIT 1",
            (user["id"], paper_id),
        ).fetchone()[0])
        # 同一写事务内冻结逐题快照：题目引用、snapshot_json、分值、题序与配图引用都固定下来。
        _insert_frozen_items(con, attempt_id, int(user["id"]), items, now)
    attempt = _owned_attempt(attempt_id, user["id"])
    frozen_items, frozen = _attempt_items(attempt)
    return _attempt_payload(attempt, paper, frozen_items, answers={}, frozen=frozen)


@router.get("/papers/{paper_id}/attempts")
def list_attempts(paper_id: int, user=Depends(current_user)):
    """某张试卷的练习历史（本人；含得分/状态/时间）。

    试卷已删除时仍返回本人的历史练习（冻结快照可回看），不因删卷 404；完全无记录才 404。
    """
    paper = papers.find_paper(paper_id, user["id"])
    live_count = len(papers.list_items(paper_id, user["id"])) if paper is not None else 0
    with connection() as con:
        rows = con.execute(
            "SELECT * FROM pl_paper_attempts WHERE paper_id=? AND user_id=? "
            "ORDER BY COALESCE(submitted_at, started_at) DESC, id DESC LIMIT 50",
            (paper_id, user["id"]),
        ).fetchall()
        if paper is None and not rows:
            raise HTTPException(404, "未找到该试卷")
        frozen = _frozen_counts(con, [int(row["id"]) for row in rows])
    entries = []
    for row in rows:
        attempt_id = int(row["id"])
        has_frozen = attempt_id in frozen
        item_count = frozen.get(attempt_id, live_count if paper is not None else 0)
        entries.append(_attempt_public(
            _row_dict(row), item_count=item_count,
            frozen=has_frozen, paper_deleted=paper is None,
        ))
    return {"items": entries, "total": len(entries), "paper_id": paper_id}


@router.get("/attempts")
def list_attempts_by_query(paper_id: int, user=Depends(current_user)):
    return list_attempts(paper_id, user)


def _official_snapshot_image_response(image: dict) -> Response:
    """平台精选快照配图：只服务仍被公开稿件当前版本引用的公开衍生图（撤回立即 404）。"""
    public_asset_id = str(image.get("public_asset_id") or image.get("asset") or "").strip()
    if not public_asset_id:
        raise HTTPException(404, "该题配图暂不可用")
    try:
        from ..plaza import figures as plaza_figures

        with connection() as con:
            row = plaza_figures.authorize_public_asset(con, public_asset_id)
    except HTTPException:
        raise
    except Exception:  # noqa: BLE001 - 广场图授权不可用时按配图不可用降级，不阻断回看
        row = None
    if row is None:
        raise HTTPException(404, "该题配图暂不可用或已随题目撤回")
    return plaza_figures.serve_public_image(row)


def _snapshot_image_response(snapshot: dict, image_index: int) -> Response:
    """把快照里的第 ``image_index`` 张配图读成图片响应（路径安全由 exporters 内部校验）。"""
    content = _content(snapshot)
    images = content.get("images") if isinstance(content.get("images"), list) else []
    if image_index < 0 or image_index >= len(images):
        raise HTTPException(404, "该题没有这张配图")
    image = images[image_index]
    if not isinstance(image, dict):
        raise HTTPException(404, "该题没有这张配图")
    if str(image.get("kind") or "") == "official" or image.get("public_asset_id"):
        return _official_snapshot_image_response(image)
    from .exporters import _image_data_url

    data_url = _image_data_url(image)
    if not data_url:
        raise HTTPException(404, "该题配图暂不可用")
    try:
        header, encoded = data_url.split(",", 1)
        mime = header.split(";")[0].split(":", 1)[1]
        raw = base64.b64decode(encoded)
    except (ValueError, IndexError):
        raise HTTPException(404, "该题配图暂不可用") from None
    return Response(content=raw, media_type=mime,
                    headers={"Cache-Control": "private, max-age=3600"})


@router.get("/papers/{paper_id}/items/{item_position}/images/{image_index}")
def frozen_paper_image(paper_id: int, item_position: int, image_index: int,
                       user=Depends(current_user)):
    """试卷快照配图（本人校验）：冻结在试卷里的题库配图/公式图，不经过练习解锁门控。

    试卷详情与整卷练习都读建卷快照；只读题库的 ``/api/bank-image`` 按「是否已作答解锁」授权，
    直接用于历史试卷会 403。这里按试卷归属授权并只读取快照内记录的图片（路径安全由
    ``exporters._image_data_url`` 内部校验），供前端展示冻结卷面配图。
    """
    _paper_row(paper_id, user["id"])
    items = _items(paper_id, user["id"])
    if item_position < 1 or item_position > len(items):
        raise HTTPException(404, "题目不存在")
    return _snapshot_image_response(_snapshot(items[item_position - 1]), image_index)


@router.get("/attempts/{attempt_id}/items/{item_position}/images/{image_index}")
def frozen_attempt_image(attempt_id: int, item_position: int, image_index: int,
                         user=Depends(current_user)):
    """作答侧冻结配图：按开始练习时的逐题快照读图，试卷改题/重排/删除后仍指向原图。"""
    attempt = _owned_attempt(attempt_id, user["id"])
    items, _frozen = _attempt_items(attempt)
    if item_position < 1 or item_position > len(items):
        raise HTTPException(404, "题目不存在")
    return _snapshot_image_response(_snapshot(items[item_position - 1]), image_index)


@router.get("/attempts/{attempt_id}")
def get_attempt(attempt_id: int, user=Depends(current_user)):
    attempt = _owned_attempt(attempt_id, user["id"])
    paper, paper_deleted = _attempt_paper(attempt)
    items, frozen = _attempt_items(attempt)
    with connection() as con:
        answers = _saved_answers(con, attempt_id)
        rows = _answer_rows(con, attempt_id)
    return _attempt_payload(attempt, paper, items, answers=answers, answer_rows=rows,
                            frozen=frozen, paper_deleted=paper_deleted)


@router.patch("/attempts/{attempt_id}/answers")
def patch_answer(attempt_id: int, data: AnswerPatchInput, user=Depends(current_user)):
    """逐题保存作答（幂等 upsert；已提交后 409）。题目序号与范围取自冻结快照。"""
    attempt = _owned_attempt(attempt_id, user["id"])
    if attempt.get("status") == "submitted":
        raise HTTPException(409, "该练习已提交，不能再修改作答；如需重练请新建一次练习")
    items, frozen = _attempt_items(attempt)
    _paper, paper_deleted = _attempt_paper(attempt)
    positions = {index + 1 for index, item in enumerate(items)}
    if data.item_position not in positions:
        raise HTTPException(422, "题目序号不在本试卷范围内")
    with connection() as con:
        write_lock(con, "paperlab-practice")
        fresh = con.execute(
            "SELECT status FROM pl_paper_attempts WHERE id=? AND user_id=?", (attempt_id, user["id"])
        ).fetchone()
        if fresh is None:
            raise HTTPException(404, "未找到该练习记录")
        if fresh["status"] == "submitted":
            raise HTTPException(409, "该练习已提交，不能再修改作答；如需重练请新建一次练习")
        _save_answer(con, attempt, data.item_position, data.answer)
        _sync_answers_json(con, attempt_id)
    attempt = _owned_attempt(attempt_id, user["id"])
    return {
        "saved": True,
        "attempt": _attempt_public(attempt, item_count=len(items), frozen=frozen,
                                   paper_deleted=paper_deleted),
        "frozen": bool(frozen),
        "item_position": data.item_position,
        "answer": data.answer,
    }


@router.post("/attempts/{attempt_id}/submit")
def submit_attempt(attempt_id: int, data: SubmitInput | None = None, user=Depends(current_user)):
    """提交判分：客观题自动判分、主观题 pending；错题联动既有错题本（bank 来源）。

    判分使用的参考答案、分值与题序一律取自开始练习时冻结的逐题快照；补答只更新作答行。
    """
    attempt = _owned_attempt(attempt_id, user["id"])
    if attempt.get("status") == "submitted":
        raise HTTPException(409, "该练习已经提交过，不能重复提交；可查看结果或新建练习")
    paper, paper_deleted = _attempt_paper(attempt)
    items, frozen = _attempt_items(attempt)
    if not items:
        raise HTTPException(422, "试卷还没有题目，不能提交")
    payload_answers = {}
    for entry in (data.answers if data and data.answers else []):
        if not isinstance(entry, dict):
            continue
        position = int(entry.get("item_position") or entry.get("position") or 0)
        if position:
            payload_answers[position] = entry.get("answer", entry.get("value"))
    now = utcnow()
    wrong_added: list[dict] = []
    app_unlinked: list[dict] = []
    app_wrong_added: list[dict] = []
    official_unlinked: list[dict] = []
    official_wrong_added: list[dict] = []
    result_items: list[dict] = []
    score = 0.0
    max_score = 0.0
    correct_count = incorrect_count = pending_count = 0
    with connection() as con:
        write_lock(con, "paperlab-practice")
        fresh = con.execute(
            "SELECT status FROM pl_paper_attempts WHERE id=? AND user_id=?", (attempt_id, user["id"])
        ).fetchone()
        if fresh is None:
            raise HTTPException(404, "未找到该练习记录")
        if fresh["status"] == "submitted":
            raise HTTPException(409, "该练习已经提交过，不能重复提交；可查看结果或新建练习")
        answers = _saved_answers(con, attempt_id)
        answers.update(payload_answers)
        for position, value in payload_answers.items():
            _save_answer(con, attempt, position, value)
        for index, item in enumerate(items):
            position = int(index) + 1
            answer = answers.get(position)
            judged = _judge_item(item, answer)
            snapshot = _snapshot(item)
            ref = str(item.get("question_ref") or "")
            max_item = round(float(item.get("score") or 0), 2)
            max_score += max_item
            if judged["judgement"] == "correct":
                score += float(judged["score"] or 0)
                correct_count += 1
            elif judged["judgement"] == "incorrect":
                incorrect_count += 1
            else:
                pending_count += 1
            entry = {
                "item_position": position,
                "question_ref": ref,
                "ref": ref,
                "source": str(snapshot.get("source") or "app"),
                "type": str(snapshot.get("type") or "SUBJECTIVE").upper(),
                "judgement": judged["judgement"],
                "score": round(float(judged["score"] or 0), 2),
                "max_score": max_item,
                "submitted": answer,
                "reason": judged.get("reason") or "",
                "reference": judged.get("reference") or {},
            }
            if judged.get("parts"):
                entry["parts"] = judged["parts"]
            result_items.append(entry)
            con.execute(
                "UPDATE pl_paper_answers SET question_ref=?, result=?, score=?, max_score=?, detail_json=? "
                "WHERE attempt_id=? AND item_position=?",
                (
                    ref, judged["judgement"], float(judged["score"] or 0), max_item,
                    json_dump({**judged, "submitted": answer}), attempt_id, position,
                ),
            )
            if judged["judgement"] in ("incorrect", "pending"):
                # 整卷逐题入账幂等键：同一 attempt 的同一题位只入账一次，
                # 与普通作答（kind=attempt）互不干扰。
                entry_key = question_ref.paper_item_key(attempt_id, position)
                if ref.startswith("bank:"):
                    qid = ref.split(":", 1)[1].strip()
                    if qid and _record_wrong(con, user["id"], qid, judged["judgement"], entry_key=entry_key):
                        wrong_added.append({
                            "item_position": position, "qid": qid,
                            "judgement": judged["judgement"],
                            "reason": _wrong_reason(judged),
                        })
                elif ref.startswith("app:"):
                    linked = _record_wrong(
                        con, user["id"], ref, judged["judgement"], entry_key=entry_key,
                    )
                    app_unlinked.append({
                        "item_position": position, "question_ref": ref,
                        "ref": ref, "source": "app",
                        "judgement": judged["judgement"],
                        "linked": bool(linked),
                        "reason": (
                            "已加入统一错题本（我的题库）"
                            if linked else "应用题库题目已记录在本次练习结果中（错题本未写入）"
                        ),
                    })
                    if linked:
                        app_wrong_added.append({
                            "item_position": position, "qid": ref, "ref": ref,
                            "judgement": judged["judgement"],
                            "reason": _wrong_reason(judged),
                        })
                elif ref.startswith("official:"):
                    # 平台精选题：写入统一错题本（qid=official:<public_id>），
                    # 之后由练习侧错题本/复习队列按查询时可见性展示（撤回后仅保留历史）。
                    linked = _record_wrong(
                        con, user["id"], ref, judged["judgement"], entry_key=entry_key,
                    )
                    official_unlinked.append({
                        "item_position": position, "question_ref": ref,
                        "ref": ref, "source": "official",
                        "judgement": judged["judgement"],
                        "linked": bool(linked),
                        "reason": (
                            "已加入统一错题本（平台精选）"
                            if linked else "平台精选题已记录在本次练习结果中（错题本未写入）"
                        ),
                    })
                    if linked:
                        official_wrong_added.append({
                            "item_position": position, "qid": ref, "ref": ref,
                            "judgement": judged["judgement"],
                            "reason": _wrong_reason(judged),
                        })
        summary = {
            "item_count": len(items),
            "correct_count": correct_count,
            "incorrect_count": incorrect_count,
            "pending_count": pending_count,
            "score": round(score, 2),
            "max_score": round(max_score, 2),
            "auto_graded": correct_count + incorrect_count,
        }
        detail = {
            "summary": summary,
            "items": result_items,
            "wrong_added": wrong_added,
            "app_bank_unlinked": app_unlinked,
            "app_wrong_added": app_wrong_added,
            "official_bank_unlinked": official_unlinked,
            "official_wrong_added": official_wrong_added,
            "notice": (
                f"客观题自动判分：正确 {correct_count} 题、错误 {incorrect_count} 题；"
                f"主观题待核验 {pending_count} 题（不硬判，可对照参考答案自测）。"
                + (f"错题已加入错题本 {len(wrong_added)} 题。" if wrong_added else "")
                + (f"我的题库题 {len(app_wrong_added)} 题已加入统一错题本。" if app_wrong_added else "")
                + (f"我的题库题 {len(app_unlinked) - len(app_wrong_added)} 题已记录在本次练习结果中。"
                   if len(app_unlinked) > len(app_wrong_added) else "")
                + (f"平台精选题 {len(official_wrong_added)} 题已加入统一错题本。"
                   if official_wrong_added else "")
                + (f"平台精选题 {len(official_unlinked) - len(official_wrong_added)} 题已记录在本次练习结果中。"
                   if len(official_unlinked) > len(official_wrong_added) else "")
            ),
        }
        con.execute(
            "UPDATE pl_paper_attempts SET status='submitted', score=?, max_score=?, detail_json=?, "
            "submitted_at=?, answers_json=? WHERE id=?",
            (
                round(score, 2), round(max_score, 2), json_dump(detail), now,
                json_dump({str(int(row["item_position"])): json_load(row["answer_json"], None)
                           for row in con.execute(
                               "SELECT item_position, answer_json FROM pl_paper_answers WHERE attempt_id=?",
                               (attempt_id,)).fetchall()}),
                attempt_id,
            ),
        )
    attempt = _owned_attempt(attempt_id, user["id"])
    payload = _attempt_payload(attempt, paper, items, answers=answers,
                               frozen=frozen, paper_deleted=paper_deleted)
    payload["result"] = {
        "summary": detail["summary"],
        "wrong_added": detail["wrong_added"],
        "app_bank_unlinked": detail["app_bank_unlinked"],
        "app_wrong_added": detail.get("app_wrong_added") or [],
        "official_bank_unlinked": detail.get("official_bank_unlinked") or [],
        "official_wrong_added": detail.get("official_wrong_added") or [],
        "notice": detail["notice"],
    }
    return payload


__all__ = ["router", "ensure_tables"]
