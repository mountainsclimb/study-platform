"""逐步辅导（小纸/题目「按步骤提示」）服务端状态机（整合方案 §3「数学主流程」/ §5.3「逐步辅导」）。

一道题在本模块里是一份**分步提示会话**（``guided_hints`` + ``guided_steps`` +
``guided_attempts``）：先给审题提示，学生提交尝试后才逐步解锁知识点、原题解析首步与
完整解析。**未解锁步骤的正文绝不会出现在任何响应里**，完整解析只在提交尝试后可见，
服务端不把「前端隐藏」当作安全边界。

步骤生成口径（规则版，来源如实标注，不伪造 AI）
------------------------------------------------

创建会话时按 ``app/practice.py::load_question_ref`` 读到的题目数据规则生成：

1. ``focus``「审题：题目到底在问什么」：题干前 300 字 + 审题指令，**不含答案**；
   ``requires_attempt=0``，即创建后立刻解锁。
2. ``concept``「相关知识点」：``question_kp JOIN knowledge_points`` 取最多 5 个知识点名称，
   拼成「本题关联知识点：…」；没有关联时如实说明「样本不足」，请学生先写下自己想到的
   知识点与公式（不编造知识点）。
3. ``first_step``「第一步思路（来自原题解析首步）」：仅在题目有 ``solution`` 时生成；去掉
   以「答案 / 【答案】 / 解： / 【解析】 / 解析：」开头的行后取前 1–2 句（≤300 字），并注明
   该步来自原题解析首步、后续推导仍需自己完成。
4. ``solution``「完整解析与参考答案」：原题解析全文（有 ``answer_text`` 时附「参考答案：…」），
   并且必须完成前面步骤的尝试后才解锁。
5. 题目没有 ``solution`` 时只生成 ``focus`` + ``concept`` + 一条「本题题库未附解析」的
   ``concept`` 兜底，``progress.total`` 如实返回实际步骤数。

会话的 ``source_note`` 固定说明「基于原题题面与解析的规则分步提示（本轮未接 AI 生成）」，
学情/前端与导出使用同一句来源说明，不把它包装成 AI 生成内容。

解锁状态机
----------

* 创建后 position 0 立即解锁（``unlocked_at`` 非空）；其余步骤 ``unlocked_at`` 为空。
* ``advance``：从**最后一个已解锁步骤**开始，若该位置已有学生尝试，则最多解锁下一个位置；
  没有尝试记录时返回 ``requires_attempt=true`` 与中文提示，不解锁、不报错（可重复调用）。
* ``attempt``：只能在**已解锁**位置提交（否则 422），文本 1–2000 字；
  ``(hint_id, step_position)`` 唯一，重复提交返回已有尝试并标记 ``replayed=true``。
* 全部步骤解锁后会话状态置为 ``completed``，``progress.done=true``、``next_position=null``。
* 读取接口统一步骤视图：已解锁返回
  ``{position,kind,title,body,requires_attempt,attempted,unlocked_at}``；
  未解锁只返回 ``{position,kind,title,locked:true,requires_attempt:true}``（**无 body**）。

错误口径（detail 全部中文）
---------------------------

* 401：未登录（由 ``app.auth.current_user`` 统一给出）；
* 404：题目/会话不存在，或会话不属于当前用户（两者文案相同，不泄露存在性）；
* 409：题目处于隔离/不可练习状态等状态冲突；
* 422：题目引用非法、步骤位置未解锁、尝试为空或超长等参数问题；
* 503：只读题库等依赖不可用（由 ``app/practice.py`` 原样上抛）。

存储与兼容
----------

建表只在 :func:`init_guided_hints` 里用 ``con.executescript`` 幂等完成（由 ``app.main.lifespan``
调用），不写进 ``app/db.py``；只依赖 ``app.db`` 的 ``connection`` / ``write_lock`` / ``utcnow``，
JSON 列存 TEXT，SQL 占位符统一 ``?``，不使用 SQLite 专有函数、PRAGMA 或 RETURNING，因此
SQLite 与 PostgreSQL 行为一致；所有写路径先取 :func:`app.db.write_lock` 的 ``"guided-hint"``
命名写锁串行化，并发创建同题会话/重复提交尝试由唯一约束兜底。

接线方式：应用启动时调用 :func:`init_guided_hints`，并把 :data:`router` 挂到主应用
（``app.include_router(guided_hint.router)``）；前端按 ``GET /api/guided-hints/by-ref`` 取
当前题的会话，按 ``POST /{hint_id}/attempt`` + ``POST /{hint_id}/advance`` 推进。
"""
from __future__ import annotations

from contextlib import contextmanager
import re

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from . import db, practice, question_ref
from .auth import current_user

router = APIRouter(prefix="/api/guided-hints", tags=["Guided hints"])

# ---------------------------------------------------------------------------
# 枚举与限额
# ---------------------------------------------------------------------------

#: 步骤类型（与 ``guided_steps.kind`` 的取值一致）。
STEP_KINDS = ("focus", "concept", "first_step", "solution")
#: 会话状态（与 ``guided_hints.status`` 的取值一致）。
HINT_STATUSES = ("active", "completed")
STATUS_ACTIVE = "active"
STATUS_COMPLETED = "completed"

#: 会话来源说明：明确标注规则生成，本轮不接 AI，避免把规则文案包装成模型输出。
SOURCE_NOTE = (
    "基于原题题面与解析的规则分步提示（本轮未接 AI 生成）；"
    "完整解析仅在提交尝试后解锁。"
)

#: 单条尝试文本长度上限（1–2000 字）。
MAX_ATTEMPT_LENGTH = 2000
#: ``focus`` 步骤引用的题干字数上限。
MAX_FOCUS_STEM = 300
#: ``first_step`` 摘录字数上限。
MAX_FIRST_STEP = 300
#: ``concept`` 步骤引用的知识点个数上限。
MAX_KP_NAMES = 5
DEFAULT_LIMIT = 20
MAX_LIMIT = 100

_FOCUS_TITLE = "审题：题目到底在问什么"
_FOCUS_HINT = "先圈出已知条件与所求，再决定用哪个知识点。"
_CONCEPT_TITLE = "相关知识点"
_CONCEPT_HINT = "先回忆定义/定理与适用条件，再回到题目。"
_NO_KP_BODY = "题库暂无本题的知识点关联（样本不足）；先写下你想到的知识点与公式。"
_FIRST_STEP_TITLE = "第一步思路（来自原题解析首步）"
_FIRST_STEP_HINT = "该步来自原题解析的首步，仍需自己完成后续推导。"
_SOLUTION_TITLE = "完整解析与参考答案"
_NO_SOLUTION_TITLE = "本题暂无解析：先写下自己的推导"
_NO_SOLUTION_BODY = "本题题库未附解析；可保存自己的推导作为学习附件，再对照知识点自查。"

#: 原题解析中应剔除的行首标记（答案/解析本身不能作为「第一步思路」提前泄露）。
_ANALYSIS_LINE_PREFIXES = ("答案", "【答案】", "解：", "【解析】", "解析：")
#: 中文句末标点：用于把解析切成 1–2 句。
_SENTENCE_SPLIT = re.compile(r"(?<=[。！？!?；;])")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS guided_hints(
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL,
    question_ref TEXT NOT NULL,
    storage_ref TEXT NOT NULL,
    status TEXT NOT NULL,
    source_note TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(user_id, storage_ref));
CREATE TABLE IF NOT EXISTS guided_steps(
    id INTEGER PRIMARY KEY,
    hint_id INTEGER NOT NULL,
    position INTEGER NOT NULL,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    requires_attempt INTEGER NOT NULL DEFAULT 1,
    unlocked_at TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(hint_id, position));
CREATE TABLE IF NOT EXISTS guided_attempts(
    id INTEGER PRIMARY KEY,
    hint_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    step_position INTEGER NOT NULL,
    text TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(hint_id, step_position));
CREATE INDEX IF NOT EXISTS idx_guided_hints_user
    ON guided_hints(user_id, updated_at);
"""

#: 进程内就绪标记（DATA_DIR + 存储引擎）：未经 lifespan 直接调用公开函数时懒建表。
_ready_key: tuple[str, bool] | None = None


# ---------------------------------------------------------------------------
# 建表 / 就绪
# ---------------------------------------------------------------------------


def _schema_key() -> tuple[str, bool]:
    return (str(db.DATA_DIR), db.postgres_enabled())


def init_guided_hints() -> None:
    """应用启动时调用：幂等创建逐步辅导三张表与索引（SQLite / PostgreSQL 同一套 DDL）。"""
    global _ready_key
    with db.connection() as con:
        db.write_lock(con, "guided-hint")
        con.executescript(_SCHEMA)
    _ready_key = _schema_key()


def _ensure_ready() -> None:
    """懒初始化：未经 lifespan 直接调用公开函数时也保证表存在（建表仍只在 init 完成）。"""
    if _ready_key != _schema_key():
        init_guided_hints()


# ---------------------------------------------------------------------------
# 连接与写锁
# ---------------------------------------------------------------------------


def _lock_write(con) -> None:
    """在写事务内取跨进程命名写锁；外层事务已持锁时（SQLite 不可嵌套 BEGIN IMMEDIATE）复用。"""
    if getattr(con, "in_transaction", False):
        return
    db.write_lock(con, "guided-hint")


@contextmanager
def _write_connection(con=None):
    """取得写连接：``con`` 为空时自建并提交，否则复用调用方事务（由调用方负责提交）。"""
    if con is not None:
        _lock_write(con)
        yield con
        return
    with db.connection() as own:
        _lock_write(own)
        yield own


@contextmanager
def _read_connection(con=None):
    """取得读连接：``con`` 为空时自建连接，否则复用调用方连接。"""
    if con is not None:
        yield con
        return
    with db.connection() as own:
        yield own


# ---------------------------------------------------------------------------
# 校验
# ---------------------------------------------------------------------------


def _validate_user_id(user_id) -> int:
    if isinstance(user_id, bool):
        raise HTTPException(422, "用户标识不正确")
    try:
        value = int(user_id)
    except (TypeError, ValueError):
        raise HTTPException(422, "用户标识不正确") from None
    if value < 1:
        raise HTTPException(422, "用户标识不正确")
    return value


def _validate_hint_id(hint_id) -> int:
    """会话编号：非法或不存在都按 404 处理，不向前端泄露存在性。"""
    if isinstance(hint_id, bool):
        raise HTTPException(404, "分步提示不存在或无权访问")
    try:
        value = int(hint_id)
    except (TypeError, ValueError):
        raise HTTPException(404, "分步提示不存在或无权访问") from None
    if value < 1:
        raise HTTPException(404, "分步提示不存在或无权访问")
    return value


def _validate_limit(limit) -> int:
    try:
        value = int(limit)
    except (TypeError, ValueError):
        raise HTTPException(422, "limit 必须是 1–100 的整数") from None
    if value < 1 or value > MAX_LIMIT:
        raise HTTPException(422, "limit 必须是 1–100 的整数")
    return value


def _validate_step_position(value) -> int:
    if value is None:
        raise HTTPException(422, "步骤位置不能为空")
    if isinstance(value, bool):
        raise HTTPException(422, "步骤位置必须是整数")
    try:
        position = int(value)
    except (TypeError, ValueError):
        raise HTTPException(422, "步骤位置必须是整数") from None
    if position < 0:
        raise HTTPException(422, "步骤位置必须是从 0 开始的整数")
    return position


def _validate_attempt_text(value) -> str:
    """尝试文本：去掉首尾空白后必须 1–2000 字（中文提示，供前端直接展示）。"""
    if value is None:
        raise HTTPException(422, "尝试内容不能为空")
    if not isinstance(value, str):
        raise HTTPException(422, "尝试内容必须是文本")
    text = value.strip()
    if not text:
        raise HTTPException(422, "尝试内容不能为空")
    if len(text) > MAX_ATTEMPT_LENGTH:
        raise HTTPException(422, "尝试内容最多 2000 字")
    return text


def _parse_ref(value) -> question_ref.QuestionRef:
    """解析统一题目引用；格式非法统一 422（不把 QuestionRefError 变成 500）。"""
    try:
        return question_ref.parse(value)
    except question_ref.QuestionRefError as exc:
        raise HTTPException(422, f"题目引用格式不正确：{exc}") from None


def _text(value) -> str:
    """取去空白文本；None / 非字符串统一转字符串，空值返回空串。"""
    if value is None:
        return ""
    return str(value).strip()


# ---------------------------------------------------------------------------
# 步骤生成（规则版）
# ---------------------------------------------------------------------------


def _first_step_excerpt(solution: str) -> str:
    """从原题解析里取「首步」：剔除答案/解析标记行后取前 1–2 句（≤300 字）。

    解析首行常是「解析：…」这类整段说明，直接引用会把后续推导一并泄出；这里只保留
    非标记行，并按句号切出至多两句，仍无法清洗时返回空串（调用方跳过该步骤）。
    """
    lines: list[str] = []
    for raw_line in str(solution or "").splitlines():
        cleaned = raw_line.strip()
        if not cleaned:
            continue
        if cleaned.startswith(_ANALYSIS_LINE_PREFIXES):
            continue
        lines.append(cleaned)
    text = " ".join(lines).strip()
    if not text:
        return ""
    sentences = [part.strip() for part in _SENTENCE_SPLIT.split(text) if part.strip()]
    if not sentences:
        return ""
    excerpt = sentences[0][:MAX_FIRST_STEP]
    if (
        len(excerpt) < MAX_FIRST_STEP
        and len(sentences) > 1
        and len(excerpt) + len(sentences[1]) <= MAX_FIRST_STEP
    ):
        excerpt += sentences[1]
    return excerpt[:MAX_FIRST_STEP]


def _knowledge_point_names(storage_ref: str, con=None) -> list[str]:
    """按 ``question_kp JOIN knowledge_points`` 取本题关联知识点名称（最多 5 个）。

    题库/应用库的知识点关联是可选数据：表缺失或查询异常时降级为「无关联」，不因为
    知识点表不可用而让创建会话 500。
    """
    try:
        with _read_connection(con) as active:
            rows = active.execute(
                "SELECT kp.name FROM question_kp qk "
                "JOIN knowledge_points kp ON kp.id = qk.kp_id "
                "WHERE qk.qid=? ORDER BY qk.weight DESC, kp.id LIMIT ?",
                (str(storage_ref), MAX_KP_NAMES),
            ).fetchall()
    except HTTPException:
        raise
    except Exception:  # noqa: BLE001 - 知识点表缺失/查询失败按「无关联」降级
        return []
    names: list[str] = []
    for row in rows:
        name = str(row[0] or "").strip()
        if name and name not in names:
            names.append(name)
        if len(names) >= MAX_KP_NAMES:
            break
    return names


def _plan_steps(question: dict, storage_ref: str, con=None) -> list[dict]:
    """按题目数据生成步骤计划（未落库）：``[{position,kind,title,body,requires_attempt}]``。

    题目不可练习/被隔离 → 409；题干缺失 → 422；没有解析时按实际生成 3 步（不报错）。
    """
    if question.get("quarantined") or question.get("practice_allowed") is False:
        raise HTTPException(409, "该题当前不可用于练习（材料缺失或混入答案），暂不能生成分步提示")
    stem = _text(question.get("stem_plain")) or _text(question.get("stem"))
    if not stem:
        raise HTTPException(422, "该题缺少题干，暂无法生成分步提示")
    solution = _text(question.get("solution"))
    answer_text = _text(question.get("answer_text"))

    steps: list[dict] = [
        {
            "position": 0,
            "kind": "focus",
            "title": _FOCUS_TITLE,
            "body": stem[:MAX_FOCUS_STEM] + "\n" + _FOCUS_HINT,
            "requires_attempt": 0,
        },
        {
            "position": 1,
            "kind": "concept",
            "title": _CONCEPT_TITLE,
            "body": "",
            "requires_attempt": 1,
        },
    ]
    names = _knowledge_point_names(storage_ref, con=con)
    if names:
        steps[1]["body"] = "本题关联知识点：%s。%s" % ("、".join(names), _CONCEPT_HINT)
    else:
        steps[1]["body"] = _NO_KP_BODY

    if solution:
        excerpt = _first_step_excerpt(solution)
        if excerpt:
            steps.append({
                "position": len(steps),
                "kind": "first_step",
                "title": _FIRST_STEP_TITLE,
                "body": excerpt + "\n" + _FIRST_STEP_HINT,
                "requires_attempt": 1,
            })
        body = solution
        if answer_text:
            body = solution + "\n参考答案：" + answer_text
        steps.append({
            "position": len(steps),
            "kind": "solution",
            "title": _SOLUTION_TITLE,
            "body": body,
            "requires_attempt": 1,
        })
    else:
        steps.append({
            "position": len(steps),
            "kind": "concept",
            "title": _NO_SOLUTION_TITLE,
            "body": _NO_SOLUTION_BODY,
            "requires_attempt": 1,
        })
    return steps


# ---------------------------------------------------------------------------
# 行读取与视图
# ---------------------------------------------------------------------------


def _hint_row_by_id(con, user_id: int, hint_id: int):
    return con.execute(
        "SELECT * FROM guided_hints WHERE id=? AND user_id=?", (int(hint_id), int(user_id))
    ).fetchone()


def _hint_row_by_storage(con, user_id: int, storage_ref: str):
    return con.execute(
        "SELECT * FROM guided_hints WHERE user_id=? AND storage_ref=?",
        (int(user_id), str(storage_ref)),
    ).fetchone()


def _require_hint(con, user_id: int, hint_id: int):
    """读取本人会话；不存在与不属于当前用户同一 404 文案（不泄露存在性）。"""
    row = _hint_row_by_id(con, user_id, hint_id)
    if row is None:
        raise HTTPException(404, "分步提示不存在或无权访问")
    return row


def _step_rows(con, hint_id: int) -> list:
    return con.execute(
        "SELECT * FROM guided_steps WHERE hint_id=? ORDER BY position, id", (int(hint_id),)
    ).fetchall()


def _attempted_positions(con, hint_id: int, user_id: int) -> set[int]:
    rows = con.execute(
        "SELECT step_position FROM guided_attempts WHERE hint_id=? AND user_id=?",
        (int(hint_id), int(user_id)),
    ).fetchall()
    return {int(row[0]) for row in rows}


def _attempt_row(con, hint_id: int, step_position: int):
    return con.execute(
        "SELECT * FROM guided_attempts WHERE hint_id=? AND step_position=?",
        (int(hint_id), int(step_position)),
    ).fetchone()


def _attempt_dict(row) -> dict:
    return {
        "attempt_id": int(row["id"]),
        "step_position": int(row["step_position"]),
        "text": str(row["text"] or ""),
        "created_at": str(row["created_at"] or ""),
    }


def _step_view(row, attempted: bool) -> dict:
    """步骤视图：未解锁步骤只返回位置/类型/标题，绝不带 ``body`` 等正文信息。"""
    position = int(row["position"])
    kind = str(row["kind"] or "")
    title = str(row["title"] or "")
    unlocked_at = str(row["unlocked_at"] or "")
    if not unlocked_at:
        return {
            "position": position,
            "kind": kind,
            "title": title,
            "locked": True,
            "requires_attempt": True,
        }
    return {
        "position": position,
        "kind": kind,
        "title": title,
        "body": str(row["body"] or ""),
        "requires_attempt": bool(int(row["requires_attempt"] or 0)),
        "attempted": bool(attempted),
        "unlocked_at": unlocked_at,
    }


def _hint_view(con, hint_row) -> dict:
    """会话视图：``{hint:{...progress...}, steps:[step_view...]}``。"""
    hint_id = int(hint_row["id"])
    user_id = int(hint_row["user_id"])
    step_rows = _step_rows(con, hint_id)
    attempted = _attempted_positions(con, hint_id, user_id)
    steps = [_step_view(row, int(row["position"]) in attempted) for row in step_rows]
    total = len(steps)
    unlocked = sum(1 for step in steps if not step.get("locked"))
    done = bool(total) and unlocked >= total
    next_position = None if done or not total else unlocked
    return {
        "hint": {
            "hint_id": hint_id,
            "question_ref": str(hint_row["question_ref"] or ""),
            "status": STATUS_COMPLETED if done else STATUS_ACTIVE,
            "progress": {
                "total": total,
                "unlocked": unlocked,
                "next_position": next_position,
                "done": done,
            },
            "source_note": str(hint_row["source_note"] or ""),
            "created_at": str(hint_row["created_at"] or ""),
            "updated_at": str(hint_row["updated_at"] or ""),
        },
        "steps": steps,
    }


def _touch_hint(con, hint_id: int, now: str) -> None:
    con.execute("UPDATE guided_hints SET updated_at=? WHERE id=?", (now, int(hint_id)))


def _insert_attempt(con, hint_id: int, user_id: int, step_position: int, text: str, now: str):
    """写入步骤尝试；``(hint_id, step_position)`` 已存在时返回已有记录（``replayed=True``）。

    调用方必须已持有 ``"guided-hint"`` 命名写锁（本函数也只在写事务内调用），因此
    「先读后写」不会产生第二条尝试；表上的唯一约束只是最后的数据库兜底。
    """
    existing = _attempt_row(con, hint_id, step_position)
    if existing is not None:
        return _attempt_dict(existing), True
    cursor = con.execute(
        "INSERT INTO guided_attempts(hint_id, user_id, step_position, text, created_at) "
        "VALUES (?,?,?,?,?)",
        (int(hint_id), int(user_id), int(step_position), str(text), str(now)),
    )
    row = con.execute(
        "SELECT * FROM guided_attempts WHERE id=?", (int(cursor.lastrowid),)
    ).fetchone()
    if row is None:  # pragma: no cover - 正常写入后必然可读回
        raise HTTPException(503, "尝试记录写入失败，请稍后重试")
    return _attempt_dict(row), False


# ---------------------------------------------------------------------------
# 公开函数：创建 / 读取 / 推进 / 提交尝试
# ---------------------------------------------------------------------------


def create_hint(user_id, ref, con=None) -> dict:
    """创建（或复用）某道题的分步提示会话，返回会话视图 + ``replayed``。

    同一用户同一题（按 ``storage_ref`` 归一）重复创建时复用已有会话，``replayed=true``，
    不会重复生成步骤；``ref`` 支持裸 qid / ``bank:`` / ``app:`` / ``official:``。
    题目不存在 → 404，题目被隔离/不可练习 → 409，题干缺失或引用非法 → 422，
    只读题库不可用 → 503（``app/practice.py`` 原样上抛）。
    """
    _ensure_ready()
    uid = _validate_user_id(user_id)
    parsed = _parse_ref(ref)
    # 先读题、生成步骤计划，再进入写事务：不在持锁期间读题库/知识点，缩短写事务。
    question = practice.load_question_ref(parsed.ref, uid)
    steps = _plan_steps(question, parsed.storage_key, con=con)
    now = db.utcnow()
    with _write_connection(con) as active:
        existing = _hint_row_by_storage(active, uid, parsed.storage_key)
        if existing is not None:
            view = _hint_view(active, existing)
            view["replayed"] = True
            return view
        cursor = active.execute(
            "INSERT INTO guided_hints(user_id, question_ref, storage_ref, status, source_note, "
            "created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
            (uid, parsed.ref, parsed.storage_key, STATUS_ACTIVE, SOURCE_NOTE, now, now),
        )
        hint_id = int(cursor.lastrowid)
        for step in steps:
            active.execute(
                "INSERT INTO guided_steps(hint_id, position, kind, title, body, requires_attempt, "
                "unlocked_at, created_at) VALUES (?,?,?,?,?,?,?,?)",
                (
                    hint_id,
                    int(step["position"]),
                    str(step["kind"]),
                    str(step["title"]),
                    str(step["body"]),
                    int(step["requires_attempt"]),
                    now if int(step["position"]) == 0 else None,
                    now,
                ),
            )
        row = _hint_row_by_id(active, uid, hint_id)
        if row is None:  # pragma: no cover - 正常写入后必然可读回
            raise HTTPException(503, "分步提示创建失败，请稍后重试")
        view = _hint_view(active, row)
        view["replayed"] = False
        return view


def get_hint(user_id, hint_id, con=None) -> dict:
    """读取本人某个分步提示会话；不存在或非本人统一 404。"""
    _ensure_ready()
    uid = _validate_user_id(user_id)
    hid = _validate_hint_id(hint_id)
    with _read_connection(con) as active:
        return _hint_view(active, _require_hint(active, uid, hid))


def get_hint_by_ref(user_id, ref, con=None) -> dict:
    """按统一题目引用读取本人会话；未创建过该题会话 → 404。"""
    _ensure_ready()
    uid = _validate_user_id(user_id)
    parsed = _parse_ref(ref)
    with _read_connection(con) as active:
        row = _hint_row_by_storage(active, uid, parsed.storage_key)
        if row is None:
            raise HTTPException(404, "该题还没有分步提示，请先创建")
        return _hint_view(active, row)


def list_hints(user_id, *, limit=20, con=None) -> dict:
    """列出本人的分步提示会话（按最近更新倒序），返回 ``{items,total,limit}``。"""
    _ensure_ready()
    uid = _validate_user_id(user_id)
    size = _validate_limit(limit)
    with _read_connection(con) as active:
        total = int(active.execute(
            "SELECT COUNT(*) FROM guided_hints WHERE user_id=?", (uid,)
        ).fetchone()[0])
        rows = active.execute(
            "SELECT * FROM guided_hints WHERE user_id=? "
            "ORDER BY updated_at DESC, id DESC LIMIT ?",
            (uid, size),
        ).fetchall()
        items = [_hint_view(active, row) for row in rows]
    return {"items": items, "total": total, "limit": size}


def _advance_message(*, completed: bool, advanced: bool, requires_attempt: bool) -> str:
    if completed and advanced:
        return "已解锁最后一步：完整解析可以查看了，请先自行核对答案与推导。"
    if completed:
        return "全部步骤已解锁，可以查看完整解析并核对答案。"
    if requires_attempt:
        return "请先在当前已解锁步骤提交你的尝试（写下思路或答案），再解锁下一步。"
    return "已解锁下一步。"


def advance_hint(user_id, hint_id, *, attempt=None, con=None) -> dict:
    """推进解锁：当前末尾步骤有尝试才解锁下一个位置（最多一个）。

    ``attempt`` 可选：先按 ``attempt`` 的文本规则校验并提交到当前末尾步骤（等价一次
    attempt + advance），再执行推进；没有尝试时返回 ``requires_attempt=true`` 与中文提示，
    不解锁也不报错（可重复调用）。全部解锁后返回 ``completed=true``；``unlocked`` 是本轮
    新解锁的步骤视图（未解锁任何步骤时为 ``None``）。
    """
    _ensure_ready()
    uid = _validate_user_id(user_id)
    hid = _validate_hint_id(hint_id)
    text = None if attempt is None else _validate_attempt_text(attempt)
    with _write_connection(con) as active:
        row = _require_hint(active, uid, hid)
        now = db.utcnow()
        step_rows = _step_rows(active, hid)
        if not step_rows:  # pragma: no cover - 正常会话至少两步
            raise HTTPException(409, "分步提示步骤缺失，请重新创建")
        unlocked_rows = [item for item in step_rows if str(item["unlocked_at"] or "")]
        if not unlocked_rows:  # pragma: no cover - 创建时 position 0 必然已解锁
            raise HTTPException(409, "分步提示状态异常，请重新创建")
        last_position = int(unlocked_rows[-1]["position"])

        attempt_payload = None
        replayed = False
        if text is not None:
            attempt_payload, replayed = _insert_attempt(active, hid, uid, last_position, text, now)
            if not replayed:
                _touch_hint(active, hid, now)

        advanced = False
        advanced_position = None
        requires_attempt = False
        completed = len(unlocked_rows) >= len(step_rows)
        if not completed:
            if last_position in _attempted_positions(active, hid, uid):
                index = next(
                    i for i, item in enumerate(step_rows)
                    if int(item["position"]) == last_position
                )
                if index + 1 < len(step_rows):
                    next_row = step_rows[index + 1]
                    if not str(next_row["unlocked_at"] or ""):
                        active.execute(
                            "UPDATE guided_steps SET unlocked_at=? WHERE id=?",
                            (now, int(next_row["id"])),
                        )
                        advanced = True
                        advanced_position = int(next_row["position"])
                        _touch_hint(active, hid, now)
                step_rows = _step_rows(active, hid)
                completed = all(str(item["unlocked_at"] or "") for item in step_rows)
            else:
                requires_attempt = True

        if completed and str(row["status"] or "") != STATUS_COMPLETED:
            active.execute(
                "UPDATE guided_hints SET status=?, updated_at=? WHERE id=?",
                (STATUS_COMPLETED, now, hid),
            )

        refreshed = _require_hint(active, uid, hid)
        view = _hint_view(active, refreshed)
        # ``unlocked`` 是本轮新解锁的步骤视图（没有解锁任何步骤时为 None），
        # 便于前端/验收脚本直接渲染「刚解锁的那一步」，无需再按位置回查。
        unlocked = None
        if advanced_position is not None:
            unlocked = next(
                (step for step in view["steps"] if step["position"] == advanced_position), None,
            )
        view.update({
            "advanced": advanced,
            "advanced_position": advanced_position,
            "unlocked": unlocked,
            "completed": completed,
            "requires_attempt": requires_attempt,
            "attempt": attempt_payload,
            "replayed": replayed,
            "message": _advance_message(
                completed=completed, advanced=advanced, requires_attempt=requires_attempt,
            ),
        })
        return view


def record_attempt(user_id, hint_id, *, step_position, text, con=None) -> dict:
    """在**已解锁**步骤提交尝试；重复提交同一位置返回已有尝试（``replayed=true``）。

    位置属于其他会话/不存在 → 422「步骤位置不存在」；位置尚未解锁 → 422；
    文本为空或超过 2000 字 → 422。
    """
    _ensure_ready()
    uid = _validate_user_id(user_id)
    hid = _validate_hint_id(hint_id)
    position = _validate_step_position(step_position)
    body = _validate_attempt_text(text)
    with _write_connection(con) as active:
        _require_hint(active, uid, hid)
        step_row = active.execute(
            "SELECT * FROM guided_steps WHERE hint_id=? AND position=?", (hid, position)
        ).fetchone()
        if step_row is None:
            raise HTTPException(422, "步骤位置不存在")
        if not str(step_row["unlocked_at"] or ""):
            raise HTTPException(422, "该步骤尚未解锁，请先完成前面的步骤")
        now = db.utcnow()
        payload, replayed = _insert_attempt(active, hid, uid, position, body, now)
        if not replayed:
            _touch_hint(active, hid, now)
        view = _hint_view(active, _require_hint(active, uid, hid))
        view.update({"attempt": payload, "replayed": replayed})
        return view


# ---------------------------------------------------------------------------
# HTTP 路由
# ---------------------------------------------------------------------------


class CreateHintInput(BaseModel):
    """创建分步提示请求体：``ref`` 为统一题目引用（裸 qid 按题库题解析）。"""
    ref: str | None = None


class AdvanceHintInput(BaseModel):
    """推进请求体：``attempt`` 可选，表示先提交当前末尾步骤尝试再推进。"""
    attempt: str | None = None


class AttemptInput(BaseModel):
    """提交步骤尝试请求体：``step_position`` 从 0 开始，``text`` 为 1–2000 字。"""
    step_position: int | None = None
    text: str | None = None


@router.post("")
def route_create_hint(data: CreateHintInput | None = None, user=Depends(current_user)):
    """创建/复用某道题的分步提示会话（同用户同题幂等，``replayed`` 标记复用）。"""
    ref = None if data is None else data.ref
    return create_hint(user["id"], ref)


@router.get("")
def route_list_hints(
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    user=Depends(current_user),
):
    """列出本人的分步提示会话（最近更新在前，默认 20 条）。"""
    return list_hints(user["id"], limit=limit)


@router.get("/by-ref")
def route_hint_by_ref(
    ref: str = Query(default="", max_length=question_ref.MAX_REF_LENGTH),
    user=Depends(current_user),
):
    """按题目引用读取本人会话；未创建 → 404。静态路径必须先于 ``/{hint_id}`` 注册。"""
    return get_hint_by_ref(user["id"], ref)


@router.get("/{hint_id}")
def route_get_hint(hint_id: int, user=Depends(current_user)):
    """读取本人某个分步提示会话；不存在或非本人统一 404。"""
    return get_hint(user["id"], hint_id)


@router.post("/{hint_id}/advance")
def route_advance_hint(
    hint_id: int,
    data: AdvanceHintInput | None = None,
    user=Depends(current_user),
):
    """推进解锁：可带 ``attempt`` 先提交当前末尾步骤尝试；无尝试时提示先提交。"""
    attempt = None if data is None else data.attempt
    return advance_hint(user["id"], hint_id, attempt=attempt)


@router.post("/{hint_id}/attempt")
def route_record_attempt(
    hint_id: int,
    data: AttemptInput | None = None,
    user=Depends(current_user),
):
    """在已解锁步骤提交尝试（1–2000 字，重复提交返回已有记录）。"""
    body = data if data is not None else AttemptInput()
    return record_attempt(
        user["id"], hint_id, step_position=body.step_position, text=body.text,
    )


__all__ = [
    "HINT_STATUSES",
    "MAX_ATTEMPT_LENGTH",
    "MAX_LIMIT",
    "SOURCE_NOTE",
    "STATUS_ACTIVE",
    "STATUS_COMPLETED",
    "STEP_KINDS",
    "advance_hint",
    "create_hint",
    "get_hint",
    "get_hint_by_ref",
    "init_guided_hints",
    "list_hints",
    "record_attempt",
    "router",
]
