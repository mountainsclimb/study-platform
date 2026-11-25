"""学习证据共用模块（整合方案 §4「作答与学习证据」/ §8 工单 C）。

练习、整卷练习、扫描、课堂小测、复习评分与学生自评产生的作答/评阅事件，统一写入
``study_evidence`` 单一事件表，供学情、掌握路径、复习与错题读取同一套确定证据；
原始记录仍由各业务模块（practice / paperlab / classroom / review）自己维护，本模块只做
「同一份作答只产生一条有效证据」的共享投影。

口径（同时写入各接口响应的 ``caliber`` / ``note``，前端与 AI 提示沿用同一套说明）：

* **有效证据**：``valid=1`` 的最新版本。查询与统计一律只看有效版本，同一份作答不会被
  重复计数。
* **重判（修订）**：不新增有效证据，而是插入一条新版本行（``evidence_version`` 递增），
  旧版本标记 ``valid=0`` 并用 ``superseded_by`` 指向新版本；修订前后有效证据条数不变。
  统计里的 ``revised_count`` 是窗口内被替换的旧版本行数，不进入各类有效计数。
* **学生自评**：``self_report``（none / independent / assisted / unknown）是学生自己的
  判断（如「这题我是独立做出来的」），独立保存，**不等于核验结果**，不覆盖判分，也不
  并入正确率、``verdict`` 计数与独立作答样本。
* **提示与揭晓**：``prompt_state`` 记录作答前是否看到提示（none / hint / step / unknown），
  ``reveal_state`` 记录作答时答案是否已揭晓（before / after / unknown）。旧判分缺少这些
  信息时一律记 ``unknown``，**不得伪造**为「独立作答」。
* **判分来源**：``grade_source``（reference / ai_cross_verified / manual / pending / self）
  与 ``verdict``（correct / incorrect / pending / unknown）共同推导 ``confidence``
  （verified / pending / unknown）：只有 reference / ai_cross_verified / manual 的已判对错
  才是 verified，自评与缺失信息是 unknown。
* **幂等**：``(user_id, event_key)`` 唯一，重复写入直接返回已有事件，不插入第二行。
  ``event_key`` 缺省由来源键生成（见 :func:`record_event`）；修订版本在库内的键形如
  ``<基础键>#v<版本>``，调用方始终用基础键读取与修订即可。
* **时间**：``occurred_at`` 是事件实际发生时间，``created_at`` 是入库时间，统一用
  :func:`app.db.utcnow` 的 ISO-8601 UTC 字符串；统计窗口按 ``occurred_at`` 截取。

存储约定：建表只在 :func:`init_study_evidence` 里幂等完成（由 ``app.main.lifespan`` 调用），
不写进 ``app/db.py``；只依赖 ``app.db`` 的连接与 ``write_lock``，SQL 占位符为 ``?``，
不用 SQLite 专有语法，SQLite / PostgreSQL 双引擎行为一致。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import re

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from . import db, question_ref
from .auth import current_user

router = APIRouter(prefix="/api/evidence", tags=["Study evidence"])

# ---------------------------------------------------------------------------
# 枚举与常量
# ---------------------------------------------------------------------------

KINDS = (
    "attempt", "paper_attempt", "paper_scan", "classroom_quiz", "review_rating", "self_report",
)
RECORD_KINDS = (
    "attempts", "pl_paper_attempts", "paper_scan", "cls_quiz_attempts", "review_cards", "self",
)
VERDICTS = ("correct", "incorrect", "pending", "unknown")
GRADE_SOURCES = ("reference", "ai_cross_verified", "manual", "pending", "self")
CONFIDENCES = ("verified", "pending", "unknown")
PROMPT_STATES = ("none", "hint", "step", "unknown")
REVEAL_STATES = ("before", "after", "unknown")
SELF_REPORTS = ("none", "independent", "assisted", "unknown")

#: 阶段 5-3：功能使用事件（只记成功使用与耗时，不记朗读文本/公式/音频）。
FEATURES = ("demo", "formula", "speech")
FEATURE_ACTIONS = ("success",)
MAX_FEATURE_SOURCE = 40
MAX_FEATURE_SUBJECT = 40
MAX_FEATURE_DURATION_MS = 3_600_000
FEATURE_USAGE_NOTE = (
    "只统计成功使用次数与耗时；不记录朗读文本、公式内容、音频或文件名。"
)

#: 只有这些判分来源的「已判对错」才算核验通过（verified）；自评/缺失信息不算。
VERIFIED_GRADE_SOURCES = frozenset({"reference", "ai_cross_verified", "manual"})
#: 视为「独立作答」的提示/揭晓状态；其余（hint / step / after / unknown）不算独立。
INDEPENDENT_PROMPT = "none"
INDEPENDENT_REVEAL = "before"

DEFAULT_DAYS = 90
MAX_DAYS = 3650
#: 同一题「独立且核验」的有效作答达到该条数才认为样本充足（方案 §5.2：两道不同题独立答对
#: 作为示例达标规则的第一档；这里按同一题目的证据样本计）。
SAMPLE_MIN_INDEPENDENT = 2
#: 单题证据事件最多回传条数。
QUESTION_EVENT_LIMIT = 20
MAX_EVENT_KEY_LENGTH = 512
MAX_SELF_REPORT_NOTE = 500
MAX_REF_LENGTH = question_ref.MAX_REF_LENGTH

CALIBER = {
    "effective": "有效证据 = valid=1 的最新版本；重判不新增有效证据，只把旧版本标记为失效"
                 "（valid=0）并用 superseded_by 指向新版本。",
    "revision": "修订（重判）插入新版本行、evidence_version 递增；窗口内被替换的旧版本行计入 "
                "revised_count，不进入各类有效证据计数。",
    "self_report": "学生自评（self_report）独立保存，不等于核验结果：单题统计不并入 counts"
                   "（正确/错误/待核验），汇总里仍按事件计数，但自评事件的 verdict 只是学生自己的"
                   "结论，计算正确率时必须剔除 record_kind=self 的事件。",
    "prompt_reveal": "prompt_state 表示作答前是否看到提示，reveal_state 表示作答时答案是否已揭晓；"
                     "缺信息一律记 unknown，不伪造为独立作答。",
    "independent": "独立且核验的作答 = confidence=verified 且（self_report=independent，或"
                   "提示/揭晓明确为 none/before）；同一题至少 %s 条才视为样本充足。"
                   % SAMPLE_MIN_INDEPENDENT,
    "window": "统计窗口按 occurred_at（UTC ISO-8601）截取，默认近 90 天；不同 kind 的计数相互独立。",
    "unknown_verdict": "verdict=unknown 计入 total，但不计入 correct / incorrect / pending。",
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS study_evidence (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL,
    event_key TEXT NOT NULL,
    kind TEXT NOT NULL,
    question_ref TEXT,
    record_kind TEXT NOT NULL,
    record_id INTEGER,
    item_key TEXT,
    verdict TEXT NOT NULL,
    grade_source TEXT NOT NULL,
    confidence TEXT NOT NULL,
    prompt_state TEXT NOT NULL,
    reveal_state TEXT NOT NULL,
    self_report TEXT NOT NULL,
    evidence_version INTEGER NOT NULL DEFAULT 1,
    superseded_by INTEGER,
    valid INTEGER NOT NULL DEFAULT 1,
    occurred_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    meta_json TEXT,
    UNIQUE(user_id, event_key)
);
CREATE INDEX IF NOT EXISTS study_evidence_user_time ON study_evidence(user_id, occurred_at);
CREATE INDEX IF NOT EXISTS study_evidence_user_valid ON study_evidence(user_id, valid);
CREATE INDEX IF NOT EXISTS study_evidence_question ON study_evidence(user_id, question_ref, valid);
CREATE TABLE IF NOT EXISTS feature_events (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL,
    feature TEXT NOT NULL,
    action TEXT NOT NULL,
    source TEXT,
    subject TEXT,
    duration_ms INTEGER,
    occurred_at TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS feature_events_user_time ON feature_events(user_id, occurred_at);
CREATE INDEX IF NOT EXISTS feature_events_user_feature ON feature_events(user_id, feature);
"""

_VERSION_SUFFIX = re.compile(r"#v(\d+)$")
_ready_key: tuple[str, bool] | None = None


# ---------------------------------------------------------------------------
# 建表 / 就绪
# ---------------------------------------------------------------------------

def _schema_key() -> tuple[str, bool]:
    return (str(db.DATA_DIR), db.postgres_enabled())


def init_study_evidence() -> None:
    """幂等建表：由 `app.main.lifespan` 在启动时调用（不写进 app/db.py）。"""
    global _ready_key
    with db.connection() as con:
        db.write_lock(con, "study-evidence-schema")
        con.executescript(_SCHEMA)
    _ready_key = _schema_key()


def _ensure_ready() -> None:
    """懒初始化：直接调用本模块函数（未经 lifespan）时也保证表存在。"""
    if _ready_key != _schema_key():
        init_study_evidence()


# ---------------------------------------------------------------------------
# 校验与转换
# ---------------------------------------------------------------------------

def _enum(value, allowed: tuple[str, ...], label: str) -> str:
    text = str(value or "").strip()
    if text not in allowed:
        raise ValueError(f"{label}只能是 {' / '.join(allowed)}")
    return text


def _optional_int(value, label: str) -> int | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{label} 必须是整数") from None


def _derive_confidence(verdict: str, grade_source: str) -> str:
    """由判分结论与判分来源推导置信级别；自评与缺失信息一律 unknown，不伪造成 verified。"""
    if verdict == "pending" or grade_source == "pending":
        return "pending"
    if verdict in ("correct", "incorrect") and grade_source in VERIFIED_GRADE_SOURCES:
        return "verified"
    return "unknown"


def _normalize_question_ref(value) -> str | None:
    """统一引用规范化（bank:<qid> / app:<id> / official:<id>）；非法引用抛 QuestionRefError。"""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return question_ref.parse(text).ref


def _normalize_occurred(value) -> str:
    """occurred_at 规范化为带 UTC 时区的 ISO-8601 字符串，便于按字符串窗口比较。"""
    if value is None:
        return db.utcnow()
    text = str(value).strip()
    if not text:
        raise ValueError("occurred_at 不能为空")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        raise ValueError("occurred_at 必须是 ISO-8601 时间（UTC）") from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def _meta_json(meta) -> str | None:
    if meta is None:
        return None
    if not isinstance(meta, dict):
        raise ValueError("meta 必须是 JSON 对象")
    try:
        return json.dumps(meta, ensure_ascii=False)
    except (TypeError, ValueError):
        raise ValueError("meta 必须是可 JSON 序列化的对象") from None


def _like_escape(text: str) -> str:
    return str(text).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _base_key(event_key) -> str:
    """去掉修订版本后缀 ``#v<n>``，得到稳定的基础键。"""
    return _VERSION_SUFFIX.sub("", str(event_key or "").strip())


def _versioned_key(base: str, version: int) -> str:
    return f"{base}#v{int(version)}"


def _parse_generated_key(base: str) -> tuple[str, str, int | None, str | None]:
    """从缺省生成的事件键中恢复 kind / record_kind / record_id / item_key（尽力而为）。"""
    parts = base.split(":")
    kind = parts[0] if parts and parts[0] in KINDS else "attempt"
    record_kind = parts[1] if len(parts) > 1 and parts[1] in RECORD_KINDS else "attempts"
    record_id = None
    item_key = None
    if len(parts) > 2:
        try:
            record_id = int(parts[2])
        except (TypeError, ValueError):
            record_id = None
    if len(parts) > 3:
        item_key = ":".join(parts[3:])
        if item_key == "-":
            item_key = None
    return kind, record_kind, record_id, item_key


# ---------------------------------------------------------------------------
# 行读取 / 写入
# ---------------------------------------------------------------------------

def _public(row) -> dict:
    try:
        meta = json.loads(row["meta_json"]) if row["meta_json"] else None
    except (TypeError, ValueError):
        meta = None
    return {
        "id": int(row["id"]),
        "event_key": str(row["event_key"] or ""),
        "kind": str(row["kind"] or ""),
        "question_ref": row["question_ref"],
        "record_kind": str(row["record_kind"] or ""),
        "record_id": row["record_id"],
        "item_key": row["item_key"],
        "verdict": str(row["verdict"] or ""),
        "grade_source": str(row["grade_source"] or ""),
        "confidence": str(row["confidence"] or ""),
        "prompt_state": str(row["prompt_state"] or ""),
        "reveal_state": str(row["reveal_state"] or ""),
        "self_report": str(row["self_report"] or ""),
        "evidence_version": int(row["evidence_version"] or 1),
        "valid": int(row["valid"] or 0),
        "occurred_at": str(row["occurred_at"] or ""),
        "created_at": str(row["created_at"] or ""),
        "meta": meta,
    }


_INSERT_SQL = (
    "INSERT INTO study_evidence(user_id, event_key, kind, question_ref, record_kind, record_id, "
    "item_key, verdict, grade_source, confidence, prompt_state, reveal_state, self_report, "
    "evidence_version, superseded_by, valid, occurred_at, created_at, meta_json) "
    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
    "ON CONFLICT(user_id, event_key) DO NOTHING"
)


def _insert_row(con, payload: dict) -> int | None:
    """插入事件行；唯一键冲突时返回 None（不抛错，由调用方读取已有行）。"""
    cursor = con.execute(_INSERT_SQL, (
        payload["user_id"], payload["event_key"], payload["kind"], payload["question_ref"],
        payload["record_kind"], payload["record_id"], payload["item_key"], payload["verdict"],
        payload["grade_source"], payload["confidence"], payload["prompt_state"],
        payload["reveal_state"], payload["self_report"], payload["evidence_version"],
        payload["superseded_by"], payload["valid"], payload["occurred_at"],
        payload["created_at"], payload["meta_json"],
    ))
    if db.postgres_enabled():
        # PostgreSQL 的 lastrowid 由 app.db 的 INSERT ... RETURNING id 兼容层填充；
        # 冲突跳过（DO NOTHING）时为 None。
        return int(cursor.lastrowid) if cursor.lastrowid is not None else None
    # SQLite 的 lastrowid 在 DO NOTHING 时不会清空，改用 rowcount 判断是否真的插入。
    if cursor.rowcount == 1:
        return int(cursor.lastrowid)
    return None


def _row_by_id(con, event_id: int):
    return con.execute("SELECT * FROM study_evidence WHERE id=?", (int(event_id),)).fetchone()


def _row_by_key(con, user_id: int, event_key: str):
    return con.execute(
        "SELECT * FROM study_evidence WHERE user_id=? AND event_key=?",
        (int(user_id), str(event_key)),
    ).fetchone()


def _row_active(con, user_id: int, base_key: str):
    """取该基础键当前有效的最新版本（valid=1）。"""
    pattern = _like_escape(base_key) + "#v%"
    return con.execute(
        "SELECT * FROM study_evidence WHERE user_id=? AND valid=1 "
        "AND (event_key=? OR event_key LIKE ? ESCAPE '\\') "
        "ORDER BY evidence_version DESC, id DESC LIMIT 1",
        (int(user_id), base_key, pattern),
    ).fetchone()


def _record_with(con, payload: dict) -> dict:
    """在给定连接内幂等写入事件并返回公开 dict（不 commit、不开事务）。"""
    new_id = _insert_row(con, payload)
    if new_id is not None:
        row = _row_by_id(con, new_id)
    else:
        # 已存在：优先返回该基础键当前有效的最新版本（重判后仍是同一份作答的证据）。
        row = _row_active(con, payload["user_id"], _base_key(payload["event_key"]))
        if row is None:
            row = _row_by_key(con, payload["user_id"], payload["event_key"])
    if row is None:
        raise RuntimeError("学习证据写入失败：事件已存在但读取不到")
    return _public(row)


# ---------------------------------------------------------------------------
# 写入入口（供 practice / paperlab / classroom / review 接线）
# ---------------------------------------------------------------------------

def record_event(
    user_id: int,
    *,
    kind: str,
    record_kind: str,
    verdict: str,
    question_ref: str | None = None,
    record_id: int | None = None,
    item_key: str | None = None,
    grade_source: str = "reference",
    prompt_state: str = "unknown",
    reveal_state: str = "unknown",
    self_report: str = "none",
    event_key: str | None = None,
    occurred_at: str | None = None,
    meta: dict | None = None,
    con=None,
) -> dict:
    """记录一条学习证据（幂等）。

    ``event_key`` 缺省按 ``f"{kind}:{record_kind}:{record_id}:{item_key or '-'}"`` 生成；
    同一用户同一 ``event_key`` 已存在时直接返回已有事件（修订后返回当前有效版本），
    不会插入第二行。``con`` 传入时只在当前连接执行：不 commit、不自行开启事务，
    便于与作答主流程同事务提交或回滚；此时调用方应保证启动时已调用
    :func:`init_study_evidence`。
    """
    kind = _enum(kind, KINDS, "事件类型 kind")
    record_kind = _enum(record_kind, RECORD_KINDS, "记录类型 record_kind")
    verdict = _enum(verdict, VERDICTS, "判分结论 verdict")
    grade_source = _enum(grade_source, GRADE_SOURCES, "判分来源 grade_source")
    prompt_state = _enum(prompt_state, PROMPT_STATES, "提示状态 prompt_state")
    reveal_state = _enum(reveal_state, REVEAL_STATES, "揭晓状态 reveal_state")
    self_report = _enum(self_report, SELF_REPORTS, "学生自评 self_report")
    user_id = _optional_int(user_id, "user_id")
    if user_id is None:
        raise ValueError("user_id 不能为空")
    record_id = _optional_int(record_id, "record_id")
    key = str(event_key or "").strip() or f"{kind}:{record_kind}:{record_id}:{item_key or '-'}"
    if len(key) > MAX_EVENT_KEY_LENGTH:
        raise ValueError("event_key 过长")
    payload = {
        "user_id": user_id,
        "event_key": key,
        "kind": kind,
        "question_ref": _normalize_question_ref(question_ref),
        "record_kind": record_kind,
        "record_id": record_id,
        "item_key": None if item_key is None else str(item_key).strip() or None,
        "verdict": verdict,
        "grade_source": grade_source,
        "confidence": _derive_confidence(verdict, grade_source),
        "prompt_state": prompt_state,
        "reveal_state": reveal_state,
        "self_report": self_report,
        "evidence_version": 1,
        "superseded_by": None,
        "valid": 1,
        "occurred_at": _normalize_occurred(occurred_at),
        "created_at": db.utcnow(),
        "meta_json": _meta_json(meta),
    }
    if con is not None:
        return _record_with(con, payload)
    _ensure_ready()
    with db.connection() as own:
        return _record_with(own, payload)


def record_attempt_evidence(
    user_id: int,
    *,
    ref,
    attempt_id: int,
    verdict: str,
    grade_source: str,
    con=None,
    **kwargs,
) -> dict:
    """单题作答证据：kind=attempt、record_kind=attempts、record_id=attempt_id。

    ``ref`` 允许裸 qid（旧库 bank 键）、``bank:<qid>``、``app:<id>``、``official:<id>``，
    写入前统一规范化为 ``app/question_ref.py`` 的引用格式。``confidence`` 由
    verdict / grade_source 推导（reference / ai_cross_verified / manual + 已判对错 → verified；
    pending → pending；自评/缺失 → unknown）。
    """
    return record_event(
        user_id,
        kind="attempt",
        record_kind="attempts",
        record_id=attempt_id,
        verdict=verdict,
        grade_source=grade_source,
        question_ref=ref,
        con=con,
        **kwargs,
    )


def attempt_event_key(attempt_id: int) -> str:
    """单题作答证据的缺省幂等键，与 :func:`record_event` 的生成规则完全一致。"""
    return f"attempt:attempts:{int(attempt_id)}:-"


def _build_changes(
    *,
    verdict=None,
    grade_source=None,
    prompt_state=None,
    reveal_state=None,
    self_report=None,
    meta=None,
    occurred_at=None,
) -> dict:
    """把「可选修订字段」校验成内部变更集；None 表示沿用旧值。"""
    return {
        "verdict": _enum(verdict, VERDICTS, "判分结论 verdict") if verdict is not None else None,
        "grade_source": (
            _enum(grade_source, GRADE_SOURCES, "判分来源 grade_source")
            if grade_source is not None else None
        ),
        "prompt_state": (
            _enum(prompt_state, PROMPT_STATES, "提示状态 prompt_state")
            if prompt_state is not None else None
        ),
        "reveal_state": (
            _enum(reveal_state, REVEAL_STATES, "揭晓状态 reveal_state")
            if reveal_state is not None else None
        ),
        "self_report": (
            _enum(self_report, SELF_REPORTS, "学生自评 self_report")
            if self_report is not None else None
        ),
        "meta_json": _meta_json(meta) if meta is not None else None,
        "occurred_at": _normalize_occurred(occurred_at) if occurred_at is not None else None,
    }


def _fallback_payload(user_id: int, base_key: str, changes: dict, fallback: dict | None) -> dict:
    """修订时没有有效事件：按传入键补一条 v1 证据（尽力从缺省键恢复来源字段）。"""
    kind, record_kind, record_id, item_key = _parse_generated_key(base_key)
    question_ref = None
    if fallback:
        kind = fallback.get("kind") or kind
        record_kind = fallback.get("record_kind") or record_kind
        record_id = fallback.get("record_id", record_id)
        item_key = fallback.get("item_key", item_key)
        question_ref = fallback.get("question_ref")
    verdict = changes["verdict"] or "unknown"
    grade_source = changes["grade_source"] or "reference"
    return {
        "user_id": user_id,
        "event_key": base_key,
        "kind": kind,
        "question_ref": _normalize_question_ref(question_ref),
        "record_kind": record_kind,
        "record_id": record_id,
        "item_key": item_key,
        "verdict": verdict,
        "grade_source": grade_source,
        "confidence": _derive_confidence(verdict, grade_source),
        "prompt_state": changes["prompt_state"] or "unknown",
        "reveal_state": changes["reveal_state"] or "unknown",
        "self_report": changes["self_report"] or "none",
        "evidence_version": 1,
        "superseded_by": None,
        "valid": 1,
        "occurred_at": changes["occurred_at"] or db.utcnow(),
        "created_at": db.utcnow(),
        "meta_json": changes["meta_json"],
    }


def _revise_with(con, user_id: int, base_key: str, changes: dict, fallback: dict | None) -> dict:
    """在给定连接内完成一次版本修订（不 commit、不开事务）。"""
    for _attempt in range(3):
        row = _row_active(con, user_id, base_key)
        if row is None:
            return _record_with(con, _fallback_payload(user_id, base_key, changes, fallback))
        old = _public(row)
        version = int(old["evidence_version"] or 1) + 1
        verdict = changes["verdict"] or old["verdict"]
        grade_source = changes["grade_source"] or old["grade_source"]
        payload = {
            "user_id": user_id,
            "event_key": _versioned_key(base_key, version),
            "kind": old["kind"],
            "question_ref": old["question_ref"],
            "record_kind": old["record_kind"],
            "record_id": old["record_id"],
            "item_key": old["item_key"],
            "verdict": verdict,
            "grade_source": grade_source,
            "confidence": _derive_confidence(verdict, grade_source),
            "prompt_state": changes["prompt_state"] or old["prompt_state"],
            "reveal_state": changes["reveal_state"] or old["reveal_state"],
            "self_report": changes["self_report"] or old["self_report"],
            "evidence_version": version,
            "superseded_by": None,
            "valid": 1,
            "occurred_at": changes["occurred_at"] or old["occurred_at"],
            "created_at": db.utcnow(),
            "meta_json": (
                changes["meta_json"] if changes["meta_json"] is not None
                else _meta_json(old["meta"])
            ),
        }
        new_id = _insert_row(con, payload)
        if new_id is None:
            # 并发修订抢先占用了这个版本号：重新读取有效版本后重试。
            continue
        pattern = _like_escape(base_key) + "#v%"
        con.execute(
            "UPDATE study_evidence SET valid=0, superseded_by=? "
            "WHERE user_id=? AND valid=1 AND id<>? "
            "AND (event_key=? OR event_key LIKE ? ESCAPE '\\')",
            (new_id, user_id, new_id, base_key, pattern),
        )
        return _public(_row_by_id(con, new_id))
    raise RuntimeError("修订学习证据失败：并发冲突，请重试")


def _revise(
    user_id: int,
    event_key: str,
    *,
    changes: dict,
    con=None,
    fallback: dict | None = None,
) -> dict:
    base_key = _base_key(event_key)
    if not base_key:
        raise ValueError("event_key 不能为空")
    user_id = _optional_int(user_id, "user_id")
    if user_id is None:
        raise ValueError("user_id 不能为空")
    if con is not None:
        return _revise_with(con, user_id, base_key, changes, fallback)
    _ensure_ready()
    with db.connection() as own:
        # 读旧版本 + 写新版本必须串行化，避免并发修订产生两条有效证据。
        db.write_lock(own, "study-evidence")
        return _revise_with(own, user_id, base_key, changes, fallback)


def revise_event(
    user_id: int,
    event_key: str,
    *,
    verdict: str | None = None,
    grade_source: str | None = None,
    prompt_state: str | None = None,
    reveal_state: str | None = None,
    self_report: str | None = None,
    meta: dict | None = None,
    occurred_at: str | None = None,
    con=None,
) -> dict:
    """修订（重判）同一份证据：插入新版本行，旧版本标失效并指向新版本。

    未提供的字段沿用旧值；没有有效事件时退化为 :func:`record_event`（使用传入 event_key）。
    ``con`` 传入时只在当前连接执行：不 commit、不自行开启事务。
    """
    changes = _build_changes(
        verdict=verdict,
        grade_source=grade_source,
        prompt_state=prompt_state,
        reveal_state=reveal_state,
        self_report=self_report,
        meta=meta,
        occurred_at=occurred_at,
    )
    return _revise(user_id, event_key, changes=changes, con=con)


def revise_attempt_evidence(
    user_id: int,
    *,
    attempt_id: int,
    verdict: str,
    grade_source: str,
    con=None,
    **kwargs,
) -> dict:
    """按作答 ID 重判同一份证据；没有有效事件时退化为首次记录。

    ``kwargs`` 可带 ``ref``（退化时写入题目引用）以及 prompt_state / reveal_state /
    self_report / meta / occurred_at（修订时更新对应字段）。
    """
    ref = kwargs.pop("ref", None)
    kwargs.pop("event_key", None)
    changes = _build_changes(verdict=verdict, grade_source=grade_source, **kwargs)
    fallback = {
        "kind": "attempt",
        "record_kind": "attempts",
        "record_id": int(attempt_id),
        "item_key": None,
        "question_ref": ref,
    }
    return _revise(
        user_id, attempt_event_key(attempt_id), changes=changes, con=con, fallback=fallback,
    )


# ---------------------------------------------------------------------------
# 查询
# ---------------------------------------------------------------------------

def active_event(user_id: int, event_key: str) -> dict | None:
    """读取某基础键当前有效的证据版本；不存在返回 None（不泄露其他账号数据）。"""
    base_key = _base_key(event_key)
    if not base_key:
        return None
    user_id = _optional_int(user_id, "user_id")
    if user_id is None:
        return None
    _ensure_ready()
    with db.connection() as con:
        row = _row_active(con, user_id, base_key)
    return _public(row) if row is not None else None


def _is_independent_verified(row: dict) -> bool:
    """独立且核验：置信 verified，且自评为 independent 或提示/揭晓明确（none/before）。"""
    if row["confidence"] != "verified":
        return False
    return bool(
        row["self_report"] == "independent"
        or (row["prompt_state"] == INDEPENDENT_PROMPT and row["reveal_state"] == INDEPENDENT_REVEAL)
    )


def _question_evidence_note(events: list[dict], verification: list[dict],
                            independent_count: int, sample_sufficient: bool) -> str:
    if not events:
        return "该题暂无本人学习证据；完成一次作答或核验后才会出现在这里。"
    if not verification:
        return "目前只有学生自评（self_report），尚无核验作答，不能据此判断掌握。"
    if sample_sufficient:
        return "已有 %s 条独立且核验的有效作答，样本充足。" % independent_count
    unknown_context = sum(
        1 for item in verification
        if item["prompt_state"] == "unknown" or item["reveal_state"] == "unknown"
    )
    note = "样本不足：独立且核验的有效作答只有 %s 条（至少需要 %s 条）。" % (
        independent_count, SAMPLE_MIN_INDEPENDENT)
    if unknown_context:
        note += ("其中 %s 条缺少提示/揭晓信息，按 unknown 处理，不推断为独立作答。"
                 % unknown_context)
    return note


def _question_evidence_with(con, user_id: int, parsed: question_ref.QuestionRef,
                            raw_ref: str) -> dict:
    normalized = parsed.ref
    rows = con.execute(
        "SELECT * FROM study_evidence WHERE user_id=? AND question_ref=? AND valid=1 "
        "ORDER BY occurred_at DESC, id DESC",
        (int(user_id), normalized),
    ).fetchall()
    events = [_public(row) for row in rows[:QUESTION_EVENT_LIMIT]]
    verification = [
        _public(row) for row in rows
        # 学生自评是独立证据，不并入核验统计（口径见 CALIBER["self_report"]）。
        if str(row["record_kind"] or "") != "self"
    ]
    counts = {"total": len(verification), "correct": 0, "incorrect": 0, "pending": 0}
    for item in verification:
        if item["verdict"] in ("correct", "incorrect", "pending"):
            counts[item["verdict"]] += 1
    independent_count = sum(1 for item in verification if _is_independent_verified(item))
    sample_sufficient = independent_count >= SAMPLE_MIN_INDEPENDENT
    first_independent_correct = any(
        item["verdict"] == "correct" and _is_independent_verified(item) for item in verification
    )
    return {
        "ref": raw_ref,
        "question_ref": normalized,
        "counts": counts,
        "effective": verification[0] if verification else None,
        "events": events,
        "first_independent_correct": bool(first_independent_correct),
        "sample_sufficient": bool(sample_sufficient),
        "independent_verified_count": independent_count,
        "note": _question_evidence_note(events, verification, independent_count, sample_sufficient),
    }


def question_evidence(user_id: int, ref, *, con=None) -> dict:
    """单题学习证据：有效版本计数、最新有效事件、最近事件与独立作答样本判断。

    ``counts``（total / correct / incorrect / pending）只统计核验类证据，学生自评不并入；
    ``events`` 是最近 20 条有效版本事件（含自评，可按 ``record_kind``/``self_report`` 区分）。
    非法题目引用抛 :class:`app.question_ref.QuestionRefError`（路由转 422）。
    """
    parsed = question_ref.parse(ref)
    raw_ref = str(ref).strip()
    user_id = _optional_int(user_id, "user_id")
    if user_id is None:
        raise ValueError("user_id 不能为空")
    if con is not None:
        return _question_evidence_with(con, user_id, parsed, raw_ref)
    _ensure_ready()
    with db.connection() as own:
        return _question_evidence_with(own, user_id, parsed, raw_ref)


def _clamp_days(days) -> int:
    try:
        value = int(days)
    except (TypeError, ValueError):
        return DEFAULT_DAYS
    return max(1, min(MAX_DAYS, value))


def _window_start(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def _empty_counts(allowed: tuple[str, ...]) -> dict:
    return {value: 0 for value in allowed}


def _question_source(value) -> str:
    source = str(value or "").partition(":")[0]
    return source if source in question_ref.SOURCES else "none"


def _summary_with(con, user_id: int, days: int) -> dict:
    start = _window_start(days)
    rows = con.execute(
        "SELECT kind, record_kind, grade_source, verdict, prompt_state, reveal_state, "
        "self_report, question_ref, valid FROM study_evidence "
        "WHERE user_id=? AND occurred_at>=? ORDER BY id",
        (int(user_id), start),
    ).fetchall()
    by_kind = _empty_counts(KINDS)
    by_record_kind = _empty_counts(RECORD_KINDS)
    by_source = _empty_counts(GRADE_SOURCES)
    by_verdict = _empty_counts(VERDICTS)
    by_prompt_state = _empty_counts(PROMPT_STATES)
    by_reveal_state = _empty_counts(REVEAL_STATES)
    by_self_report = _empty_counts(SELF_REPORTS)
    by_question_source = {source: 0 for source in (*question_ref.SOURCES, "none")}
    effective_count = 0
    revised_count = 0
    missing_context_count = 0
    for row in rows:
        if int(row["valid"] or 0) != 1:
            revised_count += 1
            continue
        effective_count += 1
        by_kind[str(row["kind"] or "")] = by_kind.get(str(row["kind"] or ""), 0) + 1
        by_record_kind[str(row["record_kind"] or "")] = (
            by_record_kind.get(str(row["record_kind"] or ""), 0) + 1
        )
        by_source[str(row["grade_source"] or "")] = (
            by_source.get(str(row["grade_source"] or ""), 0) + 1
        )
        by_verdict[str(row["verdict"] or "")] = by_verdict.get(str(row["verdict"] or ""), 0) + 1
        by_prompt_state[str(row["prompt_state"] or "")] = (
            by_prompt_state.get(str(row["prompt_state"] or ""), 0) + 1
        )
        by_reveal_state[str(row["reveal_state"] or "")] = (
            by_reveal_state.get(str(row["reveal_state"] or ""), 0) + 1
        )
        by_self_report[str(row["self_report"] or "")] = (
            by_self_report.get(str(row["self_report"] or ""), 0) + 1
        )
        source = _question_source(row["question_ref"])
        by_question_source[source] = by_question_source.get(source, 0) + 1
        if str(row["prompt_state"] or "") == "unknown" or str(row["reveal_state"] or "") == "unknown":
            missing_context_count += 1
    return {
        "effective_count": effective_count,
        "revised_count": revised_count,
        "window_days": days,
        "window_start": start,
        "generated_at": db.utcnow(),
        "by_kind": by_kind,
        "by_record_kind": by_record_kind,
        "by_source": by_source,
        "by_verdict": by_verdict,
        "by_prompt_state": by_prompt_state,
        "by_reveal_state": by_reveal_state,
        "by_self_report": by_self_report,
        "by_question_source": by_question_source,
        "missing_context_count": missing_context_count,
        "caliber": dict(CALIBER),
    }


def summary(user_id: int, *, days: int = DEFAULT_DAYS, con=None) -> dict:
    """本人近 N 天学习证据汇总：按各维度计数 + 有效/修订条数 + 口径说明。

    只统计 ``occurred_at`` 落在窗口内的行；各类计数只计有效版本，``revised_count`` 是被
    替换的旧版本行数；学生自评以 ``by_kind=self_report`` / ``by_self_report`` 单列，其
    ``verdict`` 是学生自己的结论而不是核验结果，计算正确率时应剔除。
    """
    days = _clamp_days(days)
    user_id = _optional_int(user_id, "user_id")
    if user_id is None:
        raise ValueError("user_id 不能为空")
    if con is not None:
        return _summary_with(con, user_id, days)
    _ensure_ready()
    with db.connection() as own:
        return _summary_with(own, user_id, days)


# ---------------------------------------------------------------------------
# 功能使用事件（阶段 5-3）：演示 / 公式 / 语音的成功使用与耗时
# ---------------------------------------------------------------------------

_SOURCE_RE = re.compile(r"^[A-Za-z0-9_.-]*$")


def _feature_public(row) -> dict:
    return {
        "id": int(row["id"]),
        "feature": str(row["feature"] or ""),
        "action": str(row["action"] or ""),
        "source": row["source"],
        "subject": row["subject"],
        "duration_ms": row["duration_ms"],
        "occurred_at": row["occurred_at"],
        "created_at": row["created_at"],
    }


def record_feature_event(
    user_id: int,
    *,
    feature: str,
    action: str = "success",
    source: str | None = None,
    subject: str | None = None,
    duration_ms: int | None = None,
    con=None,
) -> dict:
    """写入一条成功使用事件；只接受白名单功能与 ``action=success``。

    不接收/不保存朗读文本、公式内容、音频或文件名；``source`` 只允许字母数字与
    ``_ . -``，``subject`` 仅保留限长文本，``duration_ms`` 限制 0–3600000。
    """
    feature = _enum(feature, FEATURES, "功能类型 feature")
    action = _enum(action, FEATURE_ACTIONS, "事件动作 action（只记录成功使用）")
    user_id = _optional_int(user_id, "user_id")
    if user_id is None:
        raise ValueError("user_id 不能为空")
    source_text = str(source or "").strip()
    if len(source_text) > MAX_FEATURE_SOURCE or not _SOURCE_RE.fullmatch(source_text):
        raise ValueError("来源标识只能包含字母、数字、点、下划线或连字符，且不超过 40 字")
    subject_text = str(subject or "").strip()
    if len(subject_text) > MAX_FEATURE_SUBJECT:
        raise ValueError("学科不能超过 40 字")
    if duration_ms is None:
        duration_value = None
    else:
        try:
            duration_value = int(duration_ms)
        except (TypeError, ValueError):
            raise ValueError("耗时必须是整数毫秒") from None
        if duration_value < 0 or duration_value > MAX_FEATURE_DURATION_MS:
            raise ValueError("耗时必须在 0–3600000 毫秒之间")
    now = db.utcnow()
    payload = (
        int(user_id), feature, action, source_text or None, subject_text or None,
        duration_value, now, now,
    )
    if con is not None:
        event_id = con.execute(
            "INSERT INTO feature_events(user_id,feature,action,source,subject,duration_ms,"
            "occurred_at,created_at) VALUES (?,?,?,?,?,?,?,?)",
            payload,
        ).lastrowid
        return _feature_public(con.execute(
            "SELECT * FROM feature_events WHERE id=?", (event_id,),
        ).fetchone())
    _ensure_ready()
    with db.connection() as own:
        event_id = own.execute(
            "INSERT INTO feature_events(user_id,feature,action,source,subject,duration_ms,"
            "occurred_at,created_at) VALUES (?,?,?,?,?,?,?,?)",
            payload,
        ).lastrowid
        return _feature_public(own.execute(
            "SELECT * FROM feature_events WHERE id=?", (event_id,),
        ).fetchone())


def _feature_usage_with(con, user_id: int, days: int) -> dict:
    start = _window_start(days)
    rows = con.execute(
        "SELECT feature, COUNT(*) AS count, COALESCE(SUM(duration_ms),0) AS total_duration_ms, "
        "MAX(occurred_at) AS last_used_at FROM feature_events "
        "WHERE user_id=? AND occurred_at>=? GROUP BY feature",
        (int(user_id), start),
    ).fetchall()
    items = {
        feature: {
            "feature": feature, "count": 0, "total_duration_ms": 0, "last_used_at": None,
        }
        for feature in FEATURES
    }
    for row in rows:
        name = str(row["feature"] or "")
        if name not in items:
            continue
        items[name] = {
            "feature": name,
            "count": int(row["count"] or 0),
            "total_duration_ms": int(row["total_duration_ms"] or 0),
            "last_used_at": row["last_used_at"],
        }
    ordered = [items[feature] for feature in FEATURES]
    return {
        "window_days": days,
        "window_start": start,
        "total": sum(item["count"] for item in ordered),
        "items": ordered,
        "note": FEATURE_USAGE_NOTE,
    }


def feature_usage(user_id: int, *, days: int = DEFAULT_DAYS, con=None) -> dict:
    """近 N 天三类功能使用次数与累计耗时（只统计成功使用）。"""
    days = _clamp_days(days)
    user_id = _optional_int(user_id, "user_id")
    if user_id is None:
        raise ValueError("user_id 不能为空")
    if con is not None:
        return _feature_usage_with(con, user_id, days)
    _ensure_ready()
    with db.connection() as own:
        return _feature_usage_with(own, user_id, days)


# ---------------------------------------------------------------------------
# HTTP 路由
# ---------------------------------------------------------------------------

class SelfReportInput(BaseModel):
    ref: str = Field(min_length=1, max_length=MAX_REF_LENGTH)
    verdict: str = Field(min_length=1, max_length=20)
    note: str | None = Field(default=None, max_length=MAX_SELF_REPORT_NOTE)


class FeatureEventInput(BaseModel):
    """功能使用事件：白名单字段，额外字段（如朗读文本）直接 422。"""

    model_config = ConfigDict(extra="forbid")

    feature: str = Field(min_length=1, max_length=20)
    action: str = Field(default="success", max_length=20)
    source: str | None = Field(default=None, max_length=MAX_FEATURE_SOURCE)
    subject: str | None = Field(default=None, max_length=MAX_FEATURE_SUBJECT)
    duration_ms: int | None = Field(
        default=None, ge=0, le=MAX_FEATURE_DURATION_MS,
    )


def _normalize_ref_or_422(value) -> question_ref.QuestionRef:
    try:
        return question_ref.parse(value)
    except question_ref.QuestionRefError as exc:
        raise HTTPException(422, f"题目引用格式不正确：{exc}") from exc


@router.get("/summary")
def evidence_summary(
    days: int = Query(DEFAULT_DAYS, ge=1, le=MAX_DAYS),
    user=Depends(current_user),
):
    """本人学习证据汇总（默认近 90 天）：各维度计数、有效/修订条数与口径。"""
    return summary(user["id"], days=days)


@router.get("/question/{ref:path}")
def evidence_question(ref: str, user=Depends(current_user)):
    """单题学习证据：只回本人提交/核验留下的证据，不回显题干、答案或解析内容。"""
    parsed = _normalize_ref_or_422(ref)
    return question_evidence(user["id"], parsed.ref)


@router.post("/self-report")
def submit_self_report(data: SelfReportInput, user=Depends(current_user)):
    """提交学生自评（正确/错误）：独立保存为 kind=self_report，不覆盖、不改动核验证据。"""
    if data.verdict not in ("correct", "incorrect"):
        raise HTTPException(422, "自评结论只能是 correct 或 incorrect")
    parsed = _normalize_ref_or_422(data.ref)
    note = (data.note or "").strip() or None
    token = hashlib.sha256((note or "").encode("utf-8")).hexdigest()[:16]
    # 同一 ref + verdict + note 复用同一 event_key，重复提交只保留一条（幂等）。
    event = record_event(
        user["id"],
        kind="self_report",
        record_kind="self",
        verdict=data.verdict,
        grade_source="self",
        question_ref=parsed.ref,
        self_report="independent",
        event_key=f"self_report:{parsed.ref}:{data.verdict}:{token}",
        meta={"note": note} if note else None,
    )
    return event


@router.post("/feature-event")
def submit_feature_event(data: FeatureEventInput, user=Depends(current_user)):
    """记录一次成功使用（演示/公式/语音）；额外内容字段直接 422，不静默落库。"""
    try:
        event = record_feature_event(
            user["id"],
            feature=data.feature,
            action=data.action,
            source=data.source,
            subject=data.subject,
            duration_ms=data.duration_ms,
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return {"ok": True, "event": event}


@router.get("/feature-usage")
def feature_usage_summary(
    days: int = Query(DEFAULT_DAYS, ge=1, le=MAX_DAYS),
    user=Depends(current_user),
):
    """本人近 N 天三类功能使用次数与累计耗时（只统计成功使用）。"""
    return feature_usage(user["id"], days=days)


__all__ = [
    "CALIBER",
    "CONFIDENCES",
    "DEFAULT_DAYS",
    "FEATURES",
    "FEATURE_ACTIONS",
    "FEATURE_USAGE_NOTE",
    "GRADE_SOURCES",
    "KINDS",
    "MAX_DAYS",
    "PROMPT_STATES",
    "QUESTION_EVENT_LIMIT",
    "RECORD_KINDS",
    "REVEAL_STATES",
    "SAMPLE_MIN_INDEPENDENT",
    "SELF_REPORTS",
    "VERDICTS",
    "active_event",
    "attempt_event_key",
    "feature_usage",
    "init_study_evidence",
    "question_evidence",
    "record_attempt_evidence",
    "record_event",
    "record_feature_event",
    "revise_attempt_evidence",
    "revise_event",
    "router",
    "summary",
]
