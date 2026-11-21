"""知识点掌握路径（总方案 §3 数学主流程第 3 步、§5.2「学情」、§5.3「掌握路径」）。

本模块是 M3 的**掌握路径**服务端模块：从学情 / 错题 / 手动指定的知识点发起一条路径，
每个知识点生成「练习 → 概念复述」两个节点；节点完成条件全部由服务端重算，只来自
**明确学习证据**，没有数据时显示「样本不足」，接口不猜测掌握程度、不伪造完成。

## 节点与完成条件

* ``practice``（练习节点）：该知识点关联题中**至少 2 道不同题**满足
  :mod:`app.study_evidence` 的有效证据口径——``valid=1``、``verdict='correct'``、
  ``confidence='verified'`` 且独立作答（``self_report='independent'`` 或
  ``prompt_state='none'``）。同一道题重复正确只算 1 道；带提示（``hint`` / ``step``）、
  揭晓后（``reveal_state='after'``）以及学生自评 ``assisted`` 的作答不计。
* ``concept``（概念复述节点）：学生用自己的话复述 20–2000 字。复述在证据里独立标记
  ``self_report='independent'``，是**学生自评**，不冒充核验结果，也不并入练习节点的
  独立正确题数。

## 口径与诚实性

* 完成条件只能由服务端重算：``refresh_practice_node`` 查学习证据，``restate_path_node``
  记录学生自评；客户端无法直接把节点置为 ``done``。
* ``sample_status`` 由知识点**当前**关联的题目数决定：≥2 → ``sufficient``，否则
  ``insufficient``（读取时实时计算，创建时写入快照；题库补打标后不会一直显示样本不足）。
  路径级为「所有知识点都充足」才 ``sufficient``，否则顶层给出 ``sample_note``
  「部分知识点题目样本不足，完成条件可能无法自动核验」。
* 自动选薄弱知识点：优先读 ``study_evidence`` 的有效已判定作答（``verdict`` 为
  correct / incorrect 且 ``confidence='verified'``），该知识点正确率 < 70% 判为薄弱，
  按正确率升序取最多 3 个；某知识点完全没有证据时回退读旧 ``attempts``
  （``result != 'pending'``）的已判定作答，避免旧用户自动生成失效，也避免同一份作答
  被两处重复计数。两类数据都没有 → 422
  「暂无知识点关联或有效作答（样本不足），无法自动生成掌握路径；请显式指定知识点」。
* 跨模块只读：``question_kp`` / ``knowledge_points`` 是练习模块维护的知识点关系，
  ``attempts`` 是练习模块的旧作答记录，``study_evidence`` 是学习证据共用表。本模块只做
  只读查询（与 :mod:`app.insight` 读取这些表的做法一致），不修改它们的行；掌握路径
  自己的三张表由 :func:`init_mastery_path` 幂等创建。

## 存储与并发

* 表 ``mastery_paths`` / ``mastery_path_nodes`` / ``mastery_path_events`` 只在
  :func:`init_mastery_path` 用 ``executescript`` 幂等创建；主键已在
  ``app/db.py::_ID_TABLES`` 登记，SQLite / PostgreSQL 都能通过 ``cursor.lastrowid``
  拿到新 id。
* 行内 JSON 一律存 TEXT，SQL 占位符 ``?``，时间统一为 :func:`app.db.utcnow` 的 UTC
  ISO-8601 字符串（可直接按字符串比较/排序），不使用 SQLite 专有语法，双引擎行为一致。
* 写事务统一 ``db.write_lock(con, "mastery-path")`` 串行化；路径 ``version`` 每次进度
  重算 +1，供客户端做变更检测。

## 错误口径（detail 均为中文）

* ``404``：路径不存在或不属于当前用户（不泄露存在性）、节点不属于该路径；
* ``409``：路径已归档，仍在尝试提交复述 / 刷新完成条件；
* ``422``：知识点不存在、一次超过 8 个知识点、路径数达上限 50、复述字数越界、
  节点类型与操作不符、状态/来源枚举非法等参数错误。
"""
from __future__ import annotations

from contextlib import contextmanager
import json

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel

from . import db, question_ref
from .auth import current_user

router = APIRouter(prefix="/api/mastery-paths", tags=["Mastery path"])

# ---------------------------------------------------------------------------
# 枚举与常量
# ---------------------------------------------------------------------------

NODE_KINDS = ("practice", "concept")
NODE_STATUSES = ("pending", "in_progress", "done")
PATH_STATUSES = ("active", "completed", "archived")
SOURCE_KINDS = ("manual", "wrongbook", "insight")

#: 一个知识点固定生成两个节点：先练习、后概念复述。
NODE_PLAN = ("practice", "concept")

DEFAULT_LIMIT = 20
MAX_LIMIT = 100
#: 单用户未归档掌握路径数量上限（归档不占额度）。
MAX_PATHS = 50
#: 一次最多显式指定 8 个知识点。
MAX_KP_IDS = 8
#: 自动生成时最多选 3 个薄弱知识点。
AUTO_WEAK_LIMIT = 3
#: 与学情同一薄弱阈值：正确率 < 70% 判为薄弱（OMP accuracy < 0.7）。
WEAK_ACCURACY = 70.0

#: 练习节点完成条件：两道**不同**题独立答对。
PRACTICE_REQUIRED = 2
#: 概念复述字数范围。
CONCEPT_MIN_CHARS = 20
CONCEPT_MAX_CHARS = 2000
#: 知识点关联题目数达到该值才算样本充足。
SAMPLE_MIN_QUESTIONS = 2
#: get_path 返回的最近事件条数上限。
EVENT_LIMIT = 10

SAMPLE_NOTE = "部分知识点题目样本不足，完成条件可能无法自动核验"
INDEPENDENT_NOTE = "提示/揭晓后的作答不计"
#: 路径事件类型 → 中文说明（get_path 的 recent_events 直接展示）。
_EVENT_NOTES = {
    "created": "已创建掌握路径",
    "practice_refreshed": "已按学习证据刷新练习节点",
    "concept_restated": "已提交概念复述（学生自评）",
    "path_completed": "路径已完成",
    "path_reopened": "路径回到进行中（证据发生变化）",
    "archived": "路径已归档",
}
AUTO_EMPTY_DETAIL = (
    "暂无知识点关联或有效作答（样本不足），无法自动生成掌握路径；请显式指定知识点"
)
AUTO_WEAK_EMPTY_DETAIL = (
    "暂无薄弱知识点（已判定作答正确率均不低于 70%），无法自动生成掌握路径；请显式指定知识点"
)

CALIBER = {
    "practice": (
        "练习节点完成条件：该知识点关联题中至少 2 道不同题满足 study_evidence 有效证据"
        "（valid=1、verdict=correct、confidence=verified）且独立作答"
        "（self_report=independent 或 prompt_state=none）；同一题重复正确只算 1 道，"
        "带提示、揭晓后或自评 assisted 的作答不计。"
    ),
    "concept": (
        "概念复述节点完成条件：学生提交 20–2000 字复述，记为 self_report=independent 的"
        "学生自评证据，只代表本人声明，不是核验结果。"
    ),
    "sample": (
        "sample_status 由该知识点当前关联的题目数决定：≥2 → sufficient，否则 insufficient；"
        "样本不足时完成条件可能无法自动核验，接口不猜测掌握程度。"
    ),
    "auto": (
        "自动生成优先使用学习证据里的有效已判定作答计算知识点正确率，<70% 判为薄弱；"
        "某知识点没有任何证据时才回退旧 attempts 的已判定作答，避免重复计数。"
    ),
    "evidence": "完成条件只来自明确学习证据；没有有效作答时节点保持 pending、匹配题目为空。",
}

# ---------------------------------------------------------------------------
# 表结构 / 初始化
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS mastery_paths (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL,
    title TEXT NOT NULL,
    subject TEXT NOT NULL DEFAULT '',
    source_kind TEXT NOT NULL DEFAULT 'manual',
    status TEXT NOT NULL DEFAULT 'active',
    sample_status TEXT NOT NULL DEFAULT 'unknown',
    nodes_total INTEGER NOT NULL DEFAULT 0,
    nodes_done INTEGER NOT NULL DEFAULT 0,
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT
);
CREATE INDEX IF NOT EXISTS mastery_paths_user_status
    ON mastery_paths(user_id, status, updated_at);

CREATE TABLE IF NOT EXISTS mastery_path_nodes (
    id INTEGER PRIMARY KEY,
    path_id INTEGER NOT NULL,
    position INTEGER NOT NULL,
    kp_id INTEGER,
    kp_name TEXT NOT NULL,
    title TEXT NOT NULL,
    kind TEXT NOT NULL,
    requirements_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    evidence_json TEXT,
    completed_at TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(path_id, position)
);
CREATE INDEX IF NOT EXISTS mastery_path_nodes_path ON mastery_path_nodes(path_id, position);

CREATE TABLE IF NOT EXISTS mastery_path_events (
    id INTEGER PRIMARY KEY,
    path_id INTEGER NOT NULL,
    node_id INTEGER,
    kind TEXT NOT NULL,
    payload_json TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS mastery_path_events_path ON mastery_path_events(path_id, id);
"""

_ready_key: tuple[str, bool] | None = None


def _schema_key() -> tuple[str, bool]:
    return (str(db.DATA_DIR), db.postgres_enabled())


def init_mastery_path() -> None:
    """幂等建表：由 ``app.main.lifespan`` 启动时调用（不写进 ``app/db.py``）。

    只建本模块自己的三张表，不碰学习证据、知识点或作答表；重复调用不报错。
    """
    global _ready_key
    with db.connection() as con:
        db.write_lock(con, "mastery-path-schema")
        con.executescript(_SCHEMA)
    _ready_key = _schema_key()


def _ensure_ready() -> None:
    """懒初始化：直接调用本模块函数（未经 lifespan）时也保证表存在。"""
    if _ready_key != _schema_key():
        init_mastery_path()


# ---------------------------------------------------------------------------
# 通用工具：连接、JSON、枚举与整数校验
# ---------------------------------------------------------------------------


@contextmanager
def _use(con):
    """读取用连接：未传入时自建（db.connection），传入时复用调用方事务。"""
    if con is not None:
        yield con
        return
    with db.connection() as owned:
        yield owned


@contextmanager
def _write(con):
    """写入用连接：未传入时自建并加 ``mastery-path`` 写锁；传入时由调用方管理事务。"""
    if con is not None:
        yield con
        return
    with db.connection() as owned:
        db.write_lock(owned, "mastery-path")
        yield owned


def _load_json(raw) -> dict:
    """JSON TEXT → dict；缺失/损坏/非对象一律返回空 dict（读取路径不因脏数据 500）。"""
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _dump_json(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _chunks(values, size: int = 300):
    values = list(values)
    for start in range(0, len(values), size):
        yield values[start:start + size]


def _enum_value(value, allowed: tuple[str, ...], label: str) -> str:
    """枚举参数校验：非法值抛 422（中文 detail）。"""
    text = str(value or "").strip()
    if text not in allowed:
        raise HTTPException(422, "%s只能是 %s" % (label, " / ".join(allowed)))
    return text


def _int_value(value, label: str) -> int:
    """整数参数校验：非法值抛 422 而不是 500。"""
    if isinstance(value, bool) or value is None:
        raise HTTPException(422, "%s必须是整数" % label)
    try:
        parsed = int(str(value).strip()) if isinstance(value, str) else int(value)
    except (TypeError, ValueError):
        raise HTTPException(422, "%s必须是整数" % label) from None
    if parsed <= 0:
        raise HTTPException(422, "%s必须是正整数" % label)
    return parsed


def _normalize_source_kind(value) -> str:
    text = str(value or "").strip() or "manual"
    return _enum_value(text, SOURCE_KINDS, "路径来源 source_kind")


def _normalize_subject(value) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise HTTPException(422, "学科 subject 必须是文本")
    text = value.strip()
    if not text:
        return None
    if len(text) > 40:
        raise HTTPException(422, "学科名称过长（最多 40 字）")
    return text


def _normalize_title(value) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise HTTPException(422, "标题 title 必须是文本")
    text = value.strip()
    if not text:
        return None
    if len(text) > 120:
        raise HTTPException(422, "标题过长（最多 120 字）")
    return text


def _normalize_kp_ids(kp_ids) -> list[int] | None:
    """显式知识点列表：去重保序、≤8 个、必须是正整数；空数组视为未指定。"""
    if kp_ids is None:
        return None
    if isinstance(kp_ids, (str, bytes)) or not isinstance(kp_ids, (list, tuple)):
        raise HTTPException(422, "kp_ids 必须是知识点 ID 数组")
    ordered: list[int] = []
    for item in kp_ids:
        kp_id = _int_value(item, "知识点 ID ")
        if kp_id not in ordered:
            ordered.append(kp_id)
    if len(ordered) > MAX_KP_IDS:
        raise HTTPException(422, "一次最多指定 %s 个知识点" % MAX_KP_IDS)
    return ordered or None


# ---------------------------------------------------------------------------
# 行读取与公开投影
# ---------------------------------------------------------------------------


def _load_path(con, user_id: int, path_id: int):
    """读取当前用户的路径行；不存在或非本人 → 404（不泄露存在性）。"""
    row = con.execute(
        "SELECT * FROM mastery_paths WHERE id=? AND user_id=?",
        (int(path_id), int(user_id)),
    ).fetchone()
    if row is None:
        raise HTTPException(404, "掌握路径不存在")
    return row


def _load_node(con, path_id: int, node_id: int):
    """读取路径内的节点行；不存在 → 404。"""
    row = con.execute(
        "SELECT * FROM mastery_path_nodes WHERE id=? AND path_id=?",
        (int(node_id), int(path_id)),
    ).fetchone()
    if row is None:
        raise HTTPException(404, "掌握路径节点不存在")
    return row


def _path_nodes(con, path_id: int) -> list:
    return con.execute(
        "SELECT * FROM mastery_path_nodes WHERE path_id=? ORDER BY position ASC, id ASC",
        (int(path_id),),
    ).fetchall()


def _percent(nodes_done: int, nodes_total: int) -> int:
    if not nodes_total:
        return 0
    return int(round(100.0 * nodes_done / nodes_total))


def _progress(nodes_total: int, nodes_done: int) -> dict:
    return {
        "nodes_total": int(nodes_total),
        "nodes_done": int(nodes_done),
        "percent": _percent(nodes_done, nodes_total),
    }


def _path_summary(row, sample_status: str | None = None) -> dict:
    """路径摘要（list_paths 与 get_path 共用）。"""
    nodes_total = int(row["nodes_total"] or 0)
    nodes_done = int(row["nodes_done"] or 0)
    return {
        "path_id": int(row["id"]),
        "title": str(row["title"] or ""),
        "subject": str(row["subject"] or ""),
        "status": str(row["status"] or "active"),
        "source_kind": str(row["source_kind"] or "manual"),
        "sample_status": str(sample_status or row["sample_status"] or "unknown"),
        "progress": _progress(nodes_total, nodes_done),
        "version": int(row["version"] or 1),
        "created_at": str(row["created_at"] or ""),
        "updated_at": str(row["updated_at"] or ""),
        "completed_at": row["completed_at"],
    }


def _node_public(row, sample_status: str) -> dict:
    """节点公开投影：完成条件、证据与知识点级样本状态。"""
    return {
        "node_id": int(row["id"]),
        "path_id": int(row["path_id"]),
        "position": int(row["position"] or 0),
        "kp_id": row["kp_id"],
        "kp_name": str(row["kp_name"] or ""),
        "title": str(row["title"] or ""),
        "kind": str(row["kind"] or ""),
        "status": str(row["status"] or "pending"),
        "requirements": _load_json(row["requirements_json"]),
        "evidence": _load_json(row["evidence_json"]),
        "sample_status": sample_status,
        "completed_at": row["completed_at"],
        "created_at": str(row["created_at"] or ""),
    }


def _event_public(row) -> dict:
    kind = str(row["kind"] or "")
    return {
        "event_id": int(row["id"]),
        "kind": kind,
        # 事件类型与中文说明：前端「最近事件」直接展示，不必再按 kind 翻译。
        "event_type": kind,
        "note": _EVENT_NOTES.get(kind, kind),
        "node_id": row["node_id"],
        "payload": _load_json(row["payload_json"]),
        "created_at": str(row["created_at"] or ""),
    }


def _sample_status(linked_count: int) -> str:
    return "sufficient" if int(linked_count or 0) >= SAMPLE_MIN_QUESTIONS else "insufficient"


def _sample_note(sample_status: str) -> str | None:
    return SAMPLE_NOTE if sample_status == "insufficient" else None


# ---------------------------------------------------------------------------
# 跨模块只读查询：知识点关联题、学习证据、旧作答
# ---------------------------------------------------------------------------


def _kp_meta_map(con, kp_ids) -> dict[int, dict]:
    """知识点元数据（只读 knowledge_points）；已删除的知识点不会出现。"""
    wanted = [int(value) for value in dict.fromkeys(kp_ids)]
    result: dict[int, dict] = {}
    for chunk in _chunks(wanted):
        marks = ",".join("?" for _ in chunk)
        rows = con.execute(
            "SELECT id, subject, parent_id, name, level FROM knowledge_points "
            "WHERE id IN (%s)" % marks,
            tuple(chunk),
        ).fetchall()
        for row in rows:
            result[int(row["id"])] = {
                "kp_id": int(row["id"]),
                "subject": str(row["subject"] or ""),
                "parent_id": row["parent_id"],
                "name": str(row["name"] or ""),
                "level": row["level"],
            }
    return result


def _fetch_kps(con, kp_ids) -> list[dict]:
    """按给定顺序返回知识点元数据；任一不存在 → 422（指定知识点必须真实存在）。"""
    meta = _kp_meta_map(con, kp_ids)
    missing = [int(kp_id) for kp_id in kp_ids if int(kp_id) not in meta]
    if missing:
        raise HTTPException(
            422,
            "知识点不存在：%s（请刷新知识点列表后重试）"
            % "、".join(str(value) for value in missing),
        )
    return [meta[int(kp_id)] for kp_id in kp_ids]


def _kp_ids_for_qids(con, qids) -> dict[str, list[int]]:
    """qid → [kp_id]（只读 question_kp；供自动选薄弱与证据匹配使用）。"""
    wanted = [str(qid) for qid in dict.fromkeys(qids) if str(qid or "").strip()]
    result: dict[str, list[int]] = {}
    for chunk in _chunks(wanted):
        marks = ",".join("?" for _ in chunk)
        rows = con.execute(
            "SELECT qid, kp_id FROM question_kp WHERE qid IN (%s)" % marks,
            tuple(chunk),
        ).fetchall()
        for row in rows:
            result.setdefault(str(row["qid"]), []).append(int(row["kp_id"]))
    return result


def _linked_qids(con, kp_ids) -> dict[int, list[str]]:
    """知识点 → 当前关联题目 qid 列表（只读 question_kp）。"""
    wanted = [int(value) for value in dict.fromkeys(kp_ids)]
    result: dict[int, list[str]] = {kp_id: [] for kp_id in wanted}
    for chunk in _chunks(wanted):
        marks = ",".join("?" for _ in chunk)
        rows = con.execute(
            "SELECT kp_id, qid FROM question_kp WHERE kp_id IN (%s) "
            "ORDER BY kp_id ASC, qid ASC" % marks,
            tuple(chunk),
        ).fetchall()
        for row in rows:
            kp_id = int(row["kp_id"])
            qid = str(row["qid"] or "").strip()
            if qid and kp_id in result:
                result[kp_id].append(qid)
    return result


def _linked_refs(qids) -> dict[str, str]:
    """关联题 qid → (规范化引用, 写库 qid)；非法引用跳过（不因脏关联 500）。"""
    refs: dict[str, str] = {}
    for qid in qids:
        text = str(qid or "").strip()
        if not text:
            continue
        try:
            parsed = question_ref.parse(text)
        except question_ref.QuestionRefError:
            continue
        refs[parsed.ref] = parsed.storage_key
    return refs


def _is_independent_correct(row) -> bool:
    """独立且核验的判对证据（练习节点完成条件的最小单元）。

    口径（与 :data:`CALIBER["practice"]` 一致）：
    ``valid=1``、``verdict='correct'``、``confidence='verified'``，且
    ``self_report='independent'`` 或 ``prompt_state='none'``；提示（hint/step）、
    揭晓后（reveal_state='after'）与学生自评 assisted 一律不计。
    """
    if int(row["valid"] or 0) != 1:
        return False
    if str(row["verdict"] or "") != "correct":
        return False
    if str(row["confidence"] or "") != "verified":
        return False
    self_report = str(row["self_report"] or "none")
    prompt_state = str(row["prompt_state"] or "unknown")
    reveal_state = str(row["reveal_state"] or "unknown")
    if self_report == "assisted":
        return False
    if prompt_state in ("hint", "step"):
        return False
    if reveal_state == "after":
        return False
    return self_report == "independent" or prompt_state == "none"


def _matched_independent_questions(con, user_id: int, linked_qids) -> list[str]:
    """该知识点下「独立且核验答对」的**不同题**写库 qid（按首次答对时间排序）。

    只读 ``study_evidence``（学习证据共用表）；同一道题无论答对几次都只算 1 道。
    """
    refs = _linked_refs(linked_qids)
    if not refs:
        return []
    rows = []
    for chunk in _chunks(sorted(refs)):
        marks = ",".join("?" for _ in chunk)
        rows.extend(con.execute(
            "SELECT id, question_ref, occurred_at, prompt_state, reveal_state, "
            "self_report, verdict, confidence, valid FROM study_evidence "
            "WHERE user_id=? AND valid=1 AND verdict='correct' AND confidence='verified' "
            "AND question_ref IN (%s)" % marks,
            (int(user_id), *chunk),
        ).fetchall())
    rows.sort(key=lambda row: (str(row["occurred_at"] or ""), int(row["id"])))
    matched: list[str] = []
    seen: set[str] = set()
    for row in rows:
        ref = str(row["question_ref"] or "")
        if not ref or ref in seen:
            continue
        if not _is_independent_correct(row):
            continue
        seen.add(ref)
        matched.append(refs.get(ref) or ref)
    return matched


def _select_weak_kps(con, user_id: int, subject: str | None) -> list[dict]:
    """自动选最多 3 个薄弱知识点（无数据 → 422，不猜测）。

    优先按学习证据统计各知识点正确率（有效已判定作答）；某知识点完全没有证据时回退
    旧 ``attempts``，保证旧用户也能自动生成，同时避免同一份作答被两处重复计数。
    """
    stats: dict[int, dict] = {}
    evidence_rows = con.execute(
        "SELECT question_ref, verdict FROM study_evidence "
        "WHERE user_id=? AND valid=1 AND confidence='verified' "
        "AND verdict IN ('correct','incorrect')",
        (int(user_id),),
    ).fetchall()
    graded_refs: list[tuple[set[str], str]] = []
    qids_to_resolve: set[str] = set()
    for row in evidence_rows:
        raw_ref = str(row["question_ref"] or "").strip()
        if not raw_ref:
            continue
        try:
            parsed = question_ref.parse(raw_ref)
        except question_ref.QuestionRefError:
            continue
        candidates = {parsed.ref, parsed.storage_key}
        graded_refs.append((candidates, str(row["verdict"] or "")))
        qids_to_resolve.update(candidates)
    kp_by_qid = _kp_ids_for_qids(con, qids_to_resolve)
    for candidates, verdict in graded_refs:
        hits: set[int] = set()
        for qid in candidates:
            for kp_id in kp_by_qid.get(qid, ()):
                hits.add(int(kp_id))
        for kp_id in hits:
            bucket = stats.setdefault(kp_id, {"graded": 0, "correct": 0})
            bucket["graded"] += 1
            if verdict == "correct":
                bucket["correct"] += 1

    evidence_kp_ids = set(stats)
    # 旧 ``attempts`` 表由练习模块创建：仅在表存在时做回退查询（精简部署/旧库不因此 500）。
    attempt_rows = []
    if "attempts" in db.table_names(con):
        attempt_rows = con.execute(
            "SELECT qid, result FROM attempts "
            "WHERE user_id=? AND result IN ('correct','incorrect')",
            (int(user_id),),
        ).fetchall()
    attempt_qids = {str(row["qid"]) for row in attempt_rows if row["qid"]}
    attempt_kp = _kp_ids_for_qids(con, attempt_qids)
    attempt_stats: dict[int, dict] = {}
    for row in attempt_rows:
        qid = str(row["qid"] or "")
        for kp_id in {int(value) for value in attempt_kp.get(qid, ())}:
            if kp_id in evidence_kp_ids:
                continue  # 该知识点已有学习证据：只用证据口径，旧作答不重复计入
            bucket = attempt_stats.setdefault(kp_id, {"graded": 0, "correct": 0})
            bucket["graded"] += 1
            if str(row["result"] or "") == "correct":
                bucket["correct"] += 1
    for kp_id, bucket in attempt_stats.items():
        stats.setdefault(kp_id, bucket)

    if not stats:
        raise HTTPException(422, AUTO_EMPTY_DETAIL)

    meta = _kp_meta_map(con, list(stats))
    candidates: list[tuple[float, int, int, dict]] = []
    for kp_id, bucket in stats.items():
        kp = meta.get(int(kp_id))
        graded = int(bucket["graded"] or 0)
        if kp is None or graded <= 0:
            continue
        if subject and kp["subject"] != subject:
            continue
        accuracy = 100.0 * int(bucket["correct"] or 0) / graded
        if accuracy < WEAK_ACCURACY:
            candidates.append((accuracy, -graded, int(kp_id), kp))
    if not candidates:
        raise HTTPException(422, AUTO_WEAK_EMPTY_DETAIL)
    candidates.sort(key=lambda item: (item[0], item[1], item[2]))
    return [item[3] for item in candidates[:AUTO_WEAK_LIMIT]]


# ---------------------------------------------------------------------------
# 样本状态与路径详情
# ---------------------------------------------------------------------------


def _path_detail(con, path_row) -> dict:
    """读取路径的完整投影：摘要（含实时样本状态）、节点、进度与样本提示。"""
    nodes = _path_nodes(con, int(path_row["id"]))
    kp_ids = list(dict.fromkeys(
        int(node["kp_id"]) for node in nodes if node["kp_id"] is not None
    ))
    linked = _linked_qids(con, kp_ids)
    counts = {kp_id: len(linked.get(kp_id) or []) for kp_id in kp_ids}
    node_payloads = []
    for node in nodes:
        kp_id = node["kp_id"]
        sample_status = (
            _sample_status(counts.get(int(kp_id), 0)) if kp_id is not None else "insufficient"
        )
        node_payloads.append(_node_public(node, sample_status))
    if counts:
        sample_status = (
            "sufficient"
            if all(value >= SAMPLE_MIN_QUESTIONS for value in counts.values())
            else "insufficient"
        )
    else:
        sample_status = str(path_row["sample_status"] or "unknown")
    summary = _path_summary(path_row, sample_status)
    return {
        "path": summary,
        "nodes": node_payloads,
        "progress": dict(summary["progress"]),
        "sample_note": _sample_note(sample_status),
    }


def _path_sample_status_map(con, rows) -> dict[int, str]:
    """批量计算路径级样本状态（list_paths 用，避免逐条查询）。"""
    path_ids = [int(row["id"]) for row in rows]
    if not path_ids:
        return {}
    node_rows = []
    for chunk in _chunks(path_ids):
        marks = ",".join("?" for _ in chunk)
        node_rows.extend(con.execute(
            "SELECT path_id, kp_id FROM mastery_path_nodes WHERE path_id IN (%s)" % marks,
            tuple(chunk),
        ).fetchall())
    kp_by_path: dict[int, set[int]] = {}
    for row in node_rows:
        if row["kp_id"] is None:
            continue
        kp_by_path.setdefault(int(row["path_id"]), set()).add(int(row["kp_id"]))
    all_kp_ids = sorted({kp_id for values in kp_by_path.values() for kp_id in values})
    linked = _linked_qids(con, all_kp_ids)
    result: dict[int, str] = {}
    for row in rows:
        path_id = int(row["id"])
        kp_ids = sorted(kp_by_path.get(path_id, set()))
        if not kp_ids:
            result[path_id] = str(row["sample_status"] or "unknown")
            continue
        result[path_id] = (
            "sufficient"
            if all(len(linked.get(kp_id) or []) >= SAMPLE_MIN_QUESTIONS for kp_id in kp_ids)
            else "insufficient"
        )
    return result


# ---------------------------------------------------------------------------
# 写入工具：路径 / 节点 / 事件 / 进度重算
# ---------------------------------------------------------------------------


def _insert_path(
    con,
    *,
    user_id: int,
    title: str,
    subject: str,
    source_kind: str,
    sample_status: str,
    nodes_total: int,
    now: str,
) -> int:
    cursor = con.execute(
        "INSERT INTO mastery_paths(user_id, title, subject, source_kind, status, "
        "sample_status, nodes_total, nodes_done, version, created_at, updated_at, "
        "completed_at) VALUES(?,?,?,?, 'active', ?, ?, 0, 1, ?, ?, NULL)",
        (user_id, title, subject, source_kind, sample_status, nodes_total, now, now),
    )
    path_id = cursor.lastrowid
    if path_id is None:  # pragma: no cover - 双引擎都应返回 id
        raise RuntimeError("掌握路径写入失败：未取得新路径编号")
    return int(path_id)


def _insert_node(con, *, path_id: int, position: int, kp: dict, kind: str, now: str) -> int:
    kp_name = str(kp["name"] or "")
    if kind == "practice":
        title = "独立答对 %s 道不同题（%s）" % (PRACTICE_REQUIRED, kp_name)
        requirements = {"kind": "two_independent_correct", "count": PRACTICE_REQUIRED}
    else:
        title = "用自己的话复述该知识点并说明一道例题"
        requirements = {"kind": "restate", "min_chars": CONCEPT_MIN_CHARS}
    cursor = con.execute(
        "INSERT INTO mastery_path_nodes(path_id, position, kp_id, kp_name, title, kind, "
        "requirements_json, status, evidence_json, completed_at, created_at) "
        "VALUES(?,?,?,?,?,?,?, 'pending', NULL, NULL, ?)",
        (
            int(path_id), int(position), kp["kp_id"], kp_name, title, kind,
            _dump_json(requirements), now,
        ),
    )
    node_id = cursor.lastrowid
    if node_id is None:  # pragma: no cover - 双引擎都应返回 id
        raise RuntimeError("掌握路径节点写入失败：未取得新节点编号")
    return int(node_id)


def _insert_event(con, path_id: int, node_id, kind: str, payload: dict | None, now: str) -> int:
    cursor = con.execute(
        "INSERT INTO mastery_path_events(path_id, node_id, kind, payload_json, created_at) "
        "VALUES(?,?,?,?,?)",
        (
            int(path_id),
            None if node_id is None else int(node_id),
            str(kind),
            None if payload is None else _dump_json(payload),
            now,
        ),
    )
    event_id = cursor.lastrowid
    if event_id is None:  # pragma: no cover - 双引擎都应返回 id
        raise RuntimeError("掌握路径事件写入失败：未取得新事件编号")
    return int(event_id)


def _recompute_progress(con, path_row):
    """重算路径进度：nodes_done、status、completed_at，``version`` 每次 +1。

    * 节点全部 done → ``completed`` 并写入 ``completed_at``（已有完成时间保持不变）；
    * 否则回到 ``active``（例如证据被修订后练习节点重新变回 pending）；
    * ``archived`` 路径保持归档状态，不因节点变化改状态。
    """
    path_id = int(path_row["id"])
    counts = con.execute(
        "SELECT COUNT(*) AS total, "
        "SUM(CASE WHEN status='done' THEN 1 ELSE 0 END) AS done "
        "FROM mastery_path_nodes WHERE path_id=?",
        (path_id,),
    ).fetchone()
    nodes_total = int(counts["total"] or 0)
    nodes_done = int(counts["done"] or 0)
    now = db.utcnow()
    current_status = str(path_row["status"] or "active")
    completed_at = path_row["completed_at"]
    if current_status == "archived":
        new_status = "archived"
    elif nodes_total > 0 and nodes_done >= nodes_total:
        new_status = "completed"
        if not completed_at:
            completed_at = now
    else:
        new_status = "active"
        completed_at = None
    version = int(path_row["version"] or 1) + 1
    con.execute(
        "UPDATE mastery_paths SET status=?, nodes_total=?, nodes_done=?, completed_at=?, "
        "version=?, updated_at=? WHERE id=?",
        (new_status, nodes_total, nodes_done, completed_at, version, now, path_id),
    )
    if new_status != current_status:
        if new_status == "completed":
            _insert_event(con, path_id, None, "path_completed",
                          {"nodes_total": nodes_total}, now)
        elif new_status == "active":
            _insert_event(con, path_id, None, "path_reopened",
                          {"nodes_total": nodes_total, "nodes_done": nodes_done}, now)
    return con.execute("SELECT * FROM mastery_paths WHERE id=?", (path_id,)).fetchone()


def _find_active_path_by_kp_set(con, user_id: int, kp_ids) -> object | None:
    """同一用户相同知识点集合的 active 路径（幂等重放用）；按 id 倒序取最近一条。"""
    wanted = sorted({int(value) for value in kp_ids})
    rows = con.execute(
        "SELECT * FROM mastery_paths WHERE user_id=? AND status='active' ORDER BY id DESC",
        (int(user_id),),
    ).fetchall()
    if not rows:
        return None
    node_rows = []
    for chunk in _chunks([int(row["id"]) for row in rows]):
        marks = ",".join("?" for _ in chunk)
        node_rows.extend(con.execute(
            "SELECT path_id, kp_id FROM mastery_path_nodes "
            "WHERE path_id IN (%s) AND kp_id IS NOT NULL" % marks,
            tuple(chunk),
        ).fetchall())
    kp_sets: dict[int, set[int]] = {}
    for row in node_rows:
        kp_sets.setdefault(int(row["path_id"]), set()).add(int(row["kp_id"]))
    for row in rows:
        if sorted(kp_sets.get(int(row["id"]), set())) == wanted:
            return row
    return None


def _node_result(con, path_id: int, node_id: int) -> dict:
    """一次节点写入后的统一响应体 ``{node, path, progress}``。"""
    path_row = con.execute(
        "SELECT * FROM mastery_paths WHERE id=?", (int(path_id),)
    ).fetchone()
    detail = _path_detail(con, path_row)
    node = next(
        (item for item in detail["nodes"] if item["node_id"] == int(node_id)), None
    )
    return {"node": node, "path": detail["path"], "progress": detail["progress"]}


# ---------------------------------------------------------------------------
# 公开函数：创建 / 列表 / 详情
# ---------------------------------------------------------------------------


def _default_title(kps: list[dict], subject: str) -> str:
    names = [str(kp["name"] or "").strip() for kp in kps if str(kp["name"] or "").strip()]
    if subject:
        title = "掌握路径 · %s" % subject
    elif len(names) == 1:
        title = "掌握路径 · %s" % names[0]
    elif len(names) == 2:
        title = "掌握路径 · %s、%s" % (names[0], names[1])
    elif names:
        title = "掌握路径 · %s 等 %s 个知识点" % (names[0], len(names))
    else:  # pragma: no cover - 知识点名称在库中为 NOT NULL
        title = "掌握路径"
    return title[:120]


def _create_with(con, user_id: int, *, subject, kp_ids, title, source_kind) -> dict:
    source = _normalize_source_kind(source_kind)
    subject_value = _normalize_subject(subject)
    title_value = _normalize_title(title)
    explicit = _normalize_kp_ids(kp_ids)

    if explicit is not None:
        kps = _fetch_kps(con, explicit)
    else:
        kps = _select_weak_kps(con, user_id, subject_value)
        if not kps:  # pragma: no cover - _select_weak_kps 无候选时已抛 422
            raise HTTPException(422, AUTO_EMPTY_DETAIL)
    kp_id_list = [int(kp["kp_id"]) for kp in kps]

    existing = _find_active_path_by_kp_set(con, user_id, kp_id_list)
    if existing is not None:
        detail = _path_detail(con, existing)
        return {
            "path": detail["path"],
            "nodes": detail["nodes"],
            "progress": detail["progress"],
            "sample_note": detail["sample_note"],
            "replayed": True,
            "note": "同一知识点组合已有进行中的掌握路径，直接返回已有路径（幂等重放）。",
        }

    active_count = con.execute(
        "SELECT COUNT(*) FROM mastery_paths WHERE user_id=? AND status <> 'archived'",
        (int(user_id),),
    ).fetchone()[0]
    if int(active_count or 0) >= MAX_PATHS:
        raise HTTPException(
            422,
            "掌握路径数量已达上限（%s 条），请先归档已有路径后再创建" % MAX_PATHS,
        )

    if not subject_value:
        subjects = sorted({kp["subject"] for kp in kps if kp["subject"]})
        subject_value = subjects[0] if len(subjects) == 1 else ""
    if not title_value:
        title_value = _default_title(kps, subject_value or "")

    linked = _linked_qids(con, kp_id_list)
    sample_status = (
        "sufficient"
        if all(
            len(linked.get(kp_id) or []) >= SAMPLE_MIN_QUESTIONS for kp_id in kp_id_list
        )
        else "insufficient"
    )
    nodes_total = len(kp_id_list) * len(NODE_PLAN)
    now = db.utcnow()
    path_id = _insert_path(
        con,
        user_id=user_id,
        title=title_value or _default_title(kps, subject_value or ""),
        subject=subject_value or "",
        source_kind=source,
        sample_status=sample_status,
        nodes_total=nodes_total,
        now=now,
    )
    position = 0
    for kp in kps:
        for kind in NODE_PLAN:
            _insert_node(
                con, path_id=path_id, position=position, kp=kp, kind=kind, now=now
            )
            position += 1
    _insert_event(con, path_id, None, "created", {
        "kp_ids": kp_id_list,
        "source_kind": source,
        "nodes_total": nodes_total,
        "sample_status": sample_status,
    }, now)
    path_row = con.execute(
        "SELECT * FROM mastery_paths WHERE id=?", (path_id,)
    ).fetchone()
    detail = _path_detail(con, path_row)
    return {
        "path": detail["path"],
        "nodes": detail["nodes"],
        "progress": detail["progress"],
        "sample_note": detail["sample_note"],
        "replayed": False,
        "note": "掌握路径已创建：每个知识点包含练习与概念复述两个节点，完成条件由学习证据重算。",
    }


def create_path(
    user_id: int,
    *,
    subject=None,
    kp_ids=None,
    title=None,
    source_kind: str = "manual",
    con=None,
) -> dict:
    """创建掌握路径（未指定知识点时自动选最多 3 个薄弱知识点）。

    :param user_id: 当前用户 id。
    :param subject: 可选学科；未给定时由知识点推导（单一学科才落学科名）。
    :param kp_ids: 可选知识点 id 列表（≤8，自动去重）；不存在 → 422。
    :param title: 可选标题；缺省「掌握路径 · <学科/知识点概括>」。
    :param source_kind: 来源标记 ``manual`` / ``wrongbook`` / ``insight``，只记录不改变规则。
    :param con: 可选连接；传入时只在当前事务执行，不 commit、不加锁。
    :return: ``{path, nodes, progress, sample_note, replayed, note}``；命中相同知识点
        集合的进行中路径时 ``replayed=True`` 并返回已有路径（不新增行）。
    """
    _ensure_ready()
    owner = _int_value(user_id, "用户 id")
    with _write(con) as dbcon:
        return _create_with(
            dbcon,
            owner,
            subject=subject,
            kp_ids=kp_ids,
            title=title,
            source_kind=source_kind,
        )


def list_paths(user_id: int, *, status=None, limit: int = DEFAULT_LIMIT, con=None) -> dict:
    """当前用户的掌握路径列表（按更新时间倒序，默认 20 条）。

    :param status: 可选路径状态过滤（``active`` / ``completed`` / ``archived``）。
    :param limit: 返回条数 1..100；``total`` 是满足条件的总数（不受 limit 截断）。
    :return: ``{items, total, limit, status, note}``；每个 item 含
        ``path_id/title/subject/status/progress/sample_status/created_at/updated_at``。
    """
    _ensure_ready()
    owner = _int_value(user_id, "用户 id")
    wanted_status = None
    if status is not None and str(status).strip():
        wanted_status = _enum_value(status, PATH_STATUSES, "路径状态 status")
    try:
        limit_value = int(limit) if not isinstance(limit, bool) else DEFAULT_LIMIT
    except (TypeError, ValueError):
        limit_value = DEFAULT_LIMIT
    limit_value = max(1, min(MAX_LIMIT, limit_value))

    with _use(con) as dbcon:
        clauses = ["user_id=?"]
        params: list = [owner]
        if wanted_status:
            clauses.append("status=?")
            params.append(wanted_status)
        where = " AND ".join(clauses)
        total = dbcon.execute(
            "SELECT COUNT(*) FROM mastery_paths WHERE %s" % where, tuple(params)
        ).fetchone()[0]
        rows = dbcon.execute(
            "SELECT * FROM mastery_paths WHERE %s ORDER BY updated_at DESC, id DESC LIMIT ?"
            % where,
            (*params, limit_value),
        ).fetchall()
        sample_map = _path_sample_status_map(dbcon, rows)
        items = [
            _path_summary(row, sample_map.get(int(row["id"])))
            for row in rows
        ]
    return {
        "items": items,
        "total": int(total or 0),
        "limit": limit_value,
        "status": wanted_status,
        "note": (
            "掌握路径只由学习证据推进：练习节点要求两道不同题独立答对，概念复述是"
            "学生自评；没有有效证据时节点保持 pending。"
        ),
    }


def get_path(user_id: int, path_id: int, con=None) -> dict:
    """读取单条掌握路径详情（本人或 404）。

    :return: ``{path, nodes, progress, sample_note, recent_events, caliber}``；
        节点含 ``status/requirements/evidence/sample_status/completed_at``，
        ``sample_status`` 是知识点级（按当前关联题目数实时计算）。
    """
    _ensure_ready()
    owner = _int_value(user_id, "用户 id")
    path_value = _int_value(path_id, "掌握路径编号")
    with _use(con) as dbcon:
        path_row = _load_path(dbcon, owner, path_value)
        detail = _path_detail(dbcon, path_row)
        events = dbcon.execute(
            "SELECT * FROM mastery_path_events WHERE path_id=? ORDER BY id DESC LIMIT ?",
            (path_value, EVENT_LIMIT),
        ).fetchall()
    return {
        "path": detail["path"],
        "nodes": detail["nodes"],
        "progress": detail["progress"],
        "sample_note": detail["sample_note"],
        "recent_events": [_event_public(row) for row in events],
        "caliber": dict(CALIBER),
    }


# ---------------------------------------------------------------------------
# 公开函数：节点写入（概念复述 / 练习证据刷新）与归档
# ---------------------------------------------------------------------------


def _normalize_restate_text(text) -> tuple[str, int]:
    """概念复述文本校验：20–2000 字（按去除首尾空白后的字符数计），否则 422。"""
    if not isinstance(text, str):
        raise HTTPException(422, "复述内容 text 必须是文本")
    stripped = text.strip()
    chars = len(stripped)
    if chars < CONCEPT_MIN_CHARS:
        raise HTTPException(
            422,
            "复述内容太短：至少 %s 字（当前 %s 字）" % (CONCEPT_MIN_CHARS, chars),
        )
    if chars > CONCEPT_MAX_CHARS:
        raise HTTPException(
            422,
            "复述内容过长：最多 %s 字（当前 %s 字）" % (CONCEPT_MAX_CHARS, chars),
        )
    return stripped, chars


def _restate_with(con, user_id: int, path_id: int, node_id: int, text) -> dict:
    stripped, chars = _normalize_restate_text(text)
    path_row = _load_path(con, user_id, path_id)
    if str(path_row["status"] or "") == "archived":
        raise HTTPException(409, "掌握路径已归档，不能继续提交概念复述")
    node = _load_node(con, path_id, node_id)
    kind = str(node["kind"] or "")
    if kind != "concept":
        raise HTTPException(
            422, "只有概念复述节点可以提交复述（当前节点类型：%s）" % (kind or "unknown")
        )
    now = db.utcnow()
    evidence = {
        "kind": "restate",
        "text": stripped,
        "chars": chars,
        "self_report": "independent",
        "submitted_at": now,
    }
    con.execute(
        "UPDATE mastery_path_nodes SET status='done', evidence_json=?, completed_at=? "
        "WHERE id=? AND path_id=?",
        (_dump_json(evidence), now, int(node_id), int(path_id)),
    )
    _insert_event(con, path_id, node_id, "concept_restated", {
        "chars": chars,
        "self_report": "independent",
        "note": "学生自评，不等于核验结果",
    }, now)
    _recompute_progress(con, path_row)
    return _node_result(con, path_id, node_id)


def restate_path_node(user_id: int, path_id: int, node_id: int, *, text, con=None) -> dict:
    """提交概念复述（学生自评）：20–2000 字，写入节点 done 与自评证据。

    复述是 ``self_report='independent'`` 的学生自评，独立标记保存，**不冒充核验结果**；
    只有 ``kind='concept'`` 节点可提交，路径已归档 → 409，节点不属于该路径 → 404。
    """
    _ensure_ready()
    owner = _int_value(user_id, "用户 id")
    path_value = _int_value(path_id, "掌握路径编号")
    node_value = _int_value(node_id, "掌握路径节点编号")
    with _write(con) as dbcon:
        return _restate_with(dbcon, owner, path_value, node_value, text)


def _refresh_with(con, user_id: int, path_id: int, node_id: int) -> dict:
    path_row = _load_path(con, user_id, path_id)
    if str(path_row["status"] or "") == "archived":
        raise HTTPException(409, "掌握路径已归档，不能继续刷新完成条件")
    node = _load_node(con, path_id, node_id)
    kind = str(node["kind"] or "")
    if kind != "practice":
        raise HTTPException(
            422, "只有练习节点可以刷新完成条件（当前节点类型：%s）" % (kind or "unknown")
        )
    now = db.utcnow()
    kp_id = int(node["kp_id"]) if node["kp_id"] is not None else None
    linked = _linked_qids(con, [kp_id]) if kp_id is not None else {}
    matched = (
        _matched_independent_questions(con, user_id, linked.get(kp_id) or [])
        if kp_id is not None else []
    )
    matched_count = len(matched)
    missing = max(0, PRACTICE_REQUIRED - matched_count)
    done = matched_count >= PRACTICE_REQUIRED
    evidence = {
        "kind": "two_independent_correct",
        # ``distinct_questions`` 是已匹配「不同题」数量（前端读数用），
        # ``matched_questions`` 是具体写库 qid 列表，两者口径一致。
        "distinct_questions": matched_count,
        "matched_questions": matched,
        "missing": missing,
        "checked_at": now,
        "note": INDEPENDENT_NOTE,
    }
    con.execute(
        "UPDATE mastery_path_nodes SET status=?, evidence_json=?, completed_at=? "
        "WHERE id=? AND path_id=?",
        (
            "done" if done else "pending",
            _dump_json(evidence),
            now if done else None,
            int(node_id),
            int(path_id),
        ),
    )
    _insert_event(con, path_id, node_id, "practice_refreshed", {
        "matched_questions": matched,
        "matched_count": matched_count,
        "missing": missing,
        "status": "done" if done else "pending",
        "note": INDEPENDENT_NOTE,
    }, now)
    _recompute_progress(con, path_row)
    return _node_result(con, path_id, node_id)


def refresh_practice_node(user_id: int, path_id: int, node_id: int, con=None) -> dict:
    """按学习证据重算练习节点：该知识点 ≥2 道不同题独立核验答对才 done。

    只认 :mod:`app.study_evidence` 的有效证据（见 :func:`_is_independent_correct`）；
    不足 2 道时节点保持 ``pending``，``evidence_json`` 记录已匹配题目、还差几道与
    口径说明「提示/揭晓后的作答不计」。每次刷新都写事件并重算路径进度。
    """
    _ensure_ready()
    owner = _int_value(user_id, "用户 id")
    path_value = _int_value(path_id, "掌握路径编号")
    node_value = _int_value(node_id, "掌握路径节点编号")
    with _write(con) as dbcon:
        return _refresh_with(dbcon, owner, path_value, node_value)


def _archive_with(con, user_id: int, path_id: int) -> dict:
    path_row = _load_path(con, user_id, path_id)
    now = db.utcnow()
    if str(path_row["status"] or "") != "archived":
        previous = str(path_row["status"] or "active")
        con.execute(
            "UPDATE mastery_paths SET status='archived', updated_at=?, version=? WHERE id=?",
            (now, int(path_row["version"] or 1) + 1, int(path_id)),
        )
        _insert_event(con, path_id, None, "archived", {"previous_status": previous}, now)
    fresh = con.execute(
        "SELECT * FROM mastery_paths WHERE id=?", (int(path_id),)
    ).fetchone()
    detail = _path_detail(con, fresh)
    return {"path": detail["path"]}


def archive_path(user_id: int, path_id: int, con=None) -> dict:
    """归档掌握路径（幂等）：归档后不再计入 50 条上限，也不能继续提交节点。

    已有节点与证据全部保留；重复归档返回同一路径，不重复写事件。
    """
    _ensure_ready()
    owner = _int_value(user_id, "用户 id")
    path_value = _int_value(path_id, "掌握路径编号")
    with _write(con) as dbcon:
        return _archive_with(dbcon, owner, path_value)


# ---------------------------------------------------------------------------
# HTTP 路由（静态路径在前，动态路径在后）
# ---------------------------------------------------------------------------


class CreatePathInput(BaseModel):
    subject: str | None = None
    kp_ids: list[int] | None = None
    title: str | None = None
    source_kind: str = "manual"


class RestateInput(BaseModel):
    text: str


@router.post("")
def create_path_route(
    data: CreatePathInput,
    response: Response,
    user=Depends(current_user),
):
    """创建掌握路径；新建返回 201，命中相同知识点集合的进行中路径返回 200（replayed）。"""
    payload = create_path(
        user["id"],
        subject=data.subject,
        kp_ids=data.kp_ids,
        title=data.title,
        source_kind=data.source_kind,
    )
    response.status_code = 200 if payload.get("replayed") else 201
    return payload


@router.get("")
def list_paths_route(
    status: str | None = None,
    limit: int = DEFAULT_LIMIT,
    user=Depends(current_user),
):
    """本人掌握路径列表（默认 20 条，按更新时间倒序；可按状态过滤）。"""
    return list_paths(user["id"], status=status, limit=limit)


@router.get("/{path_id}")
def get_path_route(path_id: str, user=Depends(current_user)):
    """单条掌握路径详情：节点完成条件、证据、样本状态与最近事件。"""
    return get_path(user["id"], _int_value(path_id, "掌握路径编号"))


@router.post("/{path_id}/nodes/{node_id}/restate")
def restate_node_route(
    path_id: str,
    node_id: str,
    data: RestateInput,
    user=Depends(current_user),
):
    """提交概念复述（学生自评，20–2000 字）；只有 concept 节点可提交。"""
    return restate_path_node(
        user["id"],
        _int_value(path_id, "掌握路径编号"),
        _int_value(node_id, "掌握路径节点编号"),
        text=data.text,
    )


@router.post("/{path_id}/nodes/{node_id}/refresh")
def refresh_node_route(path_id: str, node_id: str, user=Depends(current_user)):
    """按学习证据刷新练习节点完成条件（≥2 道不同题独立核验答对才 done）。"""
    return refresh_practice_node(
        user["id"],
        _int_value(path_id, "掌握路径编号"),
        _int_value(node_id, "掌握路径节点编号"),
    )


@router.post("/{path_id}/archive")
def archive_route(path_id: str, user=Depends(current_user)):
    """归档掌握路径（幂等）；归档后不能再提交节点。"""
    return archive_path(user["id"], _int_value(path_id, "掌握路径编号"))


__all__ = [
    "AUTO_EMPTY_DETAIL",
    "AUTO_WEAK_EMPTY_DETAIL",
    "CALIBER",
    "CONCEPT_MAX_CHARS",
    "CONCEPT_MIN_CHARS",
    "DEFAULT_LIMIT",
    "EVENT_LIMIT",
    "MAX_KP_IDS",
    "MAX_LIMIT",
    "MAX_PATHS",
    "NODE_KINDS",
    "NODE_PLAN",
    "NODE_STATUSES",
    "PATH_STATUSES",
    "PRACTICE_REQUIRED",
    "SAMPLE_MIN_QUESTIONS",
    "SAMPLE_NOTE",
    "SOURCE_KINDS",
    "WEAK_ACCURACY",
    "archive_path",
    "create_path",
    "get_path",
    "init_mastery_path",
    "list_paths",
    "refresh_practice_node",
    "restate_path_node",
    "router",
]
