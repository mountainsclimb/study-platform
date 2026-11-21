"""资料原文定位与摘录（document location / annotation，整合方案 §4「资料定位」/ §5.3「原文伴读」）。

资料阅读页的「划线定位、保存摘录、回到原文」能力由本模块统一提供：小纸、笔记、掌握路径与
讲义都通过同一套读取/校验接口引用资料的某一段原文，而不是各自解析 ``chunks_json``。

数据口径
--------

* **资料与分块**：资料是 ``ai_documents`` 中的一行（本人可见）。可分块文本存放在
  ``chunks_json``（TEXT 列，JSON 数组），元素形如
  ``{"page": 3, "chunk_id": "p3-c1", "heading_path": "第一章 › 1.2", "text": "……"}``，
  由 ``app/ai/notes_agent`` 的 ``_structured_chunks`` 生成。本模块**只读**该表，不修改
  资料本身，也不新增文档版本列。
* **内容摘要即版本（doc_version）**：资料版本取
  ``sha256(json.dumps(chunks, ensure_ascii=False, sort_keys=True))[:16]``——同一份分块内容
  必得同一个摘要，分块内容一旦变化摘要即变（OCR 校对、重新分块都算新版本）。摘录只保存
  摘要快照，列表读取时实时比对当前摘要，因此不需要额外的资料版本列或版本表。
* **定位（locate）**：在当前版本内按固定优先级定位原文——先 ``chunk_id`` 精确命中；否则
  按 ``quote`` 做「空白折叠 + ASCII 大小写不敏感」匹配，取第一个命中的分块；只给
  ``page`` 时定位到该页第一个分块的开头。``start``/``end`` 是命中位置在**原始分块文本**
  中的下标（不是折叠后文本的下标），``match`` 区分 ``exact``（原文逐字相同）与
  ``normalized``（仅忽略空白/英文大小写后相同）；``snippet`` 是命中位置前后各
  ``context`` 个字符（``context`` 上限 300）并折叠空白后的预览。
* **摘录（annotation）**：保存 quote 快照、doc_version、heading_path、page/chunk_id，可选
  关联本人 ``ai_notes``。列表实时校验三条：资料仍在本人名下、当前 doc_version 与快照一致、
  当前版本仍能找到该摘录；不满足的条目照常返回并带中文 ``invalid_reason``（「资料已删除或
  无权访问」「资料已更新，摘录需重新定位」「当前版本中已找不到该摘录」），列表接口不会 500。
  ``refresh`` 在新的资料版本里按 quote 重新定位：成功则更新位置快照；找不到仍返回 200 和
  ``valid:false``，不把「引用失效」当成 404。

错误口径（detail 全部中文）
--------------------------

* 资料 / 摘录 / 笔记不存在或不属于当前用户 → 404「……不存在或无权访问」，同一文案不泄露
  其他账号资源是否存在；
* 参数非法（缺定位条件、页码越界、摘录过长、颜色非法等）或摘录在当前资料版本中找不到 → 422；
* 版本冲突：摘录定位之后、提交之前资料又被更新时，不落旧版本快照 → 409「资料在保存摘录期间
  发生变化，请重新定位后再保存」（读取路径的版本失效按规格用 ``valid:false`` 表达，不返回 409）；
* ``chunks_json`` 结构损坏（不是合法 JSON 数组）→ 503：这是服务端数据问题，不是调用方错误，
  其他读取方（如摘录列表）会捕获并降级为「摘录失效」，不会抛出 500；
* 本模块不引入新的限流面，因此不产生 429。

存储约束
--------

建表只在 :func:`init_doc_location` 内用 ``con.executescript`` 幂等完成；只通过 ``app.db``
的 ``connection()`` 访问应用库（不缓存 ``DATA_DIR``），时间统一用 ``db.utcnow()``，JSON 列
存 TEXT、SQL 占位符统一 ``?``，不使用 SQLite 专有函数/PRAGMA/RETURNING，SQLite 与
PostgreSQL 行为一致；所有写路径先取 ``db.write_lock(con, "doc-location")`` 串行化。
``doc_annotations`` 已登记 ``app/db.py::_ID_TABLES``，PostgreSQL 下 ``INSERT`` 自动带
``RETURNING id``，与 SQLite 的 ``lastrowid`` 口径一致。

接线方式：应用启动时调用 :func:`init_doc_location`，并把 :data:`router_docs`
（``/api/documents/{document_id}/locate``）与 :data:`router`（``/api/doc-annotations``）
挂到主应用。本模块依赖 AI 模块已建好 ``ai_documents`` / ``ai_notes`` 两张表（只读引用）。
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import re

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from . import db
from .auth import current_user

router_docs = APIRouter(prefix="/api/documents", tags=["Document location"])
router = APIRouter(prefix="/api/doc-annotations", tags=["Document annotations"])

#: 定位时允许的最大页码（与资料页数无关，仅拦截明显非法的输入）。
MAX_PAGE = 100_000
#: ``chunk_id`` 最大长度（``p{页}-c{序号}`` 格式远小于该值）。
MAX_CHUNK_ID_LENGTH = 64
#: 单条摘录文本的最大长度。
MAX_QUOTE_LENGTH = 500
#: 摘录备注最大长度。
MAX_NOTE_LENGTH = 1000
#: 单份资料最多保存的摘录条数。
MAX_ANNOTATIONS_PER_DOC = 200
#: 列表默认 / 最大返回条数。
DEFAULT_LIST_LIMIT = 100
MAX_LIST_LIMIT = 200
#: 定位预览默认上下文长度 / 上限。
DEFAULT_CONTEXT = 80
MAX_CONTEXT = 300
#: 摘录颜色白名单与默认颜色。
ANNOTATION_COLORS = ("yellow", "green", "blue", "pink")
DEFAULT_COLOR = "yellow"

#: 摘录失效原因（中文，直接下发前端）。
INVALID_REASON_MISSING = "资料已删除或无权访问"
INVALID_REASON_VERSION = "资料已更新，摘录需重新定位"
INVALID_REASON_QUOTE = "当前版本中已找不到该摘录"

#: 定位结果的可读提示（``notice``，正常时为空字符串）。
NOTICE_NORMALIZED = "已按忽略空白与英文大小写的方式匹配，返回资料中的原始文字。"
NOTICE_CHUNK_ANCHOR = "已定位到片段开头。"
NOTICE_PAGE_ANCHOR = "已定位到该页第一个片段的开头。"
NOTICE_CHUNK_FALLBACK = "原片段标识已失效，已按摘录重新定位。"
NOTICE_REFRESHED = "已按当前资料版本重新定位摘录。"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS doc_annotations(
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL,
    document_id INTEGER NOT NULL,
    note_id INTEGER,
    page INTEGER NOT NULL,
    chunk_id TEXT,
    heading_path TEXT NOT NULL DEFAULT '',
    quote TEXT NOT NULL,
    snippet TEXT NOT NULL DEFAULT '',
    color TEXT NOT NULL DEFAULT 'yellow',
    note TEXT NOT NULL DEFAULT '',
    doc_version TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_doc_annotations_user
    ON doc_annotations(user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_doc_annotations_document
    ON doc_annotations(user_id, document_id, created_at);
CREATE INDEX IF NOT EXISTS idx_doc_annotations_note
    ON doc_annotations(user_id, note_id);
"""

#: 进程内就绪标记（数据目录 + 存储引擎）：直接调用本模块函数（未经 lifespan）时懒建表。
_ready_key: tuple[str, bool] | None = None


# ---------------------------------------------------------------------------
# 建表 / 初始化
# ---------------------------------------------------------------------------


def _schema_key() -> tuple[str, bool]:
    return (str(db.DATA_DIR), db.postgres_enabled())


def init_doc_location() -> None:
    """应用启动时调用：幂等创建 ``doc_annotations`` 表与索引（SQLite / PostgreSQL 同一套 DDL）。"""
    global _ready_key
    with db.connection() as con:
        db.write_lock(con, "doc-location")
        con.executescript(_SCHEMA)
    _ready_key = _schema_key()


def _ensure_ready() -> None:
    """懒初始化：未经 lifespan 直接调用本模块函数时也保证表存在（建表仍只在 init 完成）。"""
    if _ready_key != _schema_key():
        init_doc_location()


# ---------------------------------------------------------------------------
# 连接与写锁
# ---------------------------------------------------------------------------


def _lock_write(con) -> None:
    """在写事务内取跨进程命名写锁；外层事务已持锁时（SQLite 不能嵌套 BEGIN IMMEDIATE）复用。"""
    if getattr(con, "in_transaction", False):
        return
    db.write_lock(con, "doc-location")


@contextmanager
def _write_connection(con=None):
    """取得写连接：``con`` 为空时自建并在退出时提交，否则复用调用方事务（由调用方提交）。"""
    if con is not None:
        _lock_write(con)
        yield con
        return
    with db.connection() as own:
        _lock_write(own)
        yield own


@contextmanager
def _read_connection(con=None):
    """取得读连接：``con`` 为空时自建，否则复用调用方连接。"""
    if con is not None:
        yield con
        return
    with db.connection() as own:
        yield own


# ---------------------------------------------------------------------------
# 参数校验（失败统一抛 422，detail 中文）
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


def _validate_id(value, label: str = "编号") -> int:
    """校验资料/摘录/笔记等整数标识（>=1）。"""
    if isinstance(value, bool):
        raise HTTPException(422, f"{label}不正确")
    if isinstance(value, float):
        if not value.is_integer():
            raise HTTPException(422, f"{label}不正确")
        value = int(value)
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise HTTPException(422, f"{label}不正确") from None
    if number < 1:
        raise HTTPException(422, f"{label}不正确")
    return number


def _validate_page(page) -> int | None:
    if page is None:
        return None
    if isinstance(page, bool):
        raise HTTPException(422, "页码不正确")
    if isinstance(page, float):
        if not page.is_integer():
            raise HTTPException(422, "页码不正确")
        page = int(page)
    try:
        value = int(page)
    except (TypeError, ValueError):
        raise HTTPException(422, "页码不正确") from None
    if not 1 <= value <= MAX_PAGE:
        raise HTTPException(422, f"页码需在 1 到 {MAX_PAGE} 之间")
    return value


def _validate_chunk_id(chunk_id) -> str | None:
    if chunk_id is None:
        return None
    if not isinstance(chunk_id, str):
        raise HTTPException(422, "片段标识不正确")
    value = chunk_id.strip()
    if not value:
        raise HTTPException(422, "片段标识不正确")
    if len(value) > MAX_CHUNK_ID_LENGTH:
        raise HTTPException(422, f"片段标识最多 {MAX_CHUNK_ID_LENGTH} 个字符")
    return value


def _validate_quote(quote) -> str:
    if not isinstance(quote, str):
        raise HTTPException(422, "摘录不能为空")
    value = quote.strip()
    if not value:
        raise HTTPException(422, "摘录不能为空")
    if len(value) > MAX_QUOTE_LENGTH:
        raise HTTPException(422, f"摘录最多 {MAX_QUOTE_LENGTH} 个字符")
    return value


def _validate_note(note) -> str:
    if note is None:
        return ""
    if not isinstance(note, str):
        raise HTTPException(422, "备注不正确")
    value = note.strip()
    if len(value) > MAX_NOTE_LENGTH:
        raise HTTPException(422, f"备注最多 {MAX_NOTE_LENGTH} 个字符")
    return value


def _validate_color(color) -> str:
    value = DEFAULT_COLOR if color is None else str(color).strip().lower()
    if value not in ANNOTATION_COLORS:
        raise HTTPException(422, "颜色不正确，可选：" + "、".join(ANNOTATION_COLORS))
    return value


def _validate_context(context) -> int:
    """上下文长度：非法值退回默认，超限截到 300（预览只影响展示，不作为 422 拒绝理由）。"""
    if isinstance(context, bool):
        return DEFAULT_CONTEXT
    try:
        value = int(context)
    except (TypeError, ValueError):
        return DEFAULT_CONTEXT
    return max(0, min(MAX_CONTEXT, value))


def _validate_limit(limit) -> int:
    if isinstance(limit, bool):
        raise HTTPException(422, f"数量需在 1 到 {MAX_LIST_LIMIT} 之间")
    try:
        value = int(limit)
    except (TypeError, ValueError):
        raise HTTPException(422, f"数量需在 1 到 {MAX_LIST_LIMIT} 之间") from None
    if not 1 <= value <= MAX_LIST_LIMIT:
        raise HTTPException(422, f"数量需在 1 到 {MAX_LIST_LIMIT} 之间")
    return value


# ---------------------------------------------------------------------------
# JSON / 行对象小工具
# ---------------------------------------------------------------------------


def _row_dict(row) -> dict:
    """把 sqlite3.Row / PostgreSQL 伪行 / dict 统一转成 dict（两种行都支持 keys()）。"""
    if isinstance(row, dict):
        return dict(row)
    return {key: row[key] for key in row.keys()}


def _parse_chunks_json(raw) -> list:
    """解析 ``chunks_json``：空值按空列表，非法 JSON / 非数组视为资料数据损坏（503）。"""
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", "ignore")
    if raw is None:
        return []
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return []
        try:
            data = json.loads(text)
        except ValueError:
            raise HTTPException(503, "资料内容暂不可用，请稍后重试") from None
    else:
        data = raw
    if not isinstance(data, list):
        raise HTTPException(503, "资料内容暂不可用，请稍后重试")
    return data


def _chunk_page(value) -> int:
    """分块页码归一化（缺失/非法按 0 处理，只用于定位过滤与展示）。"""
    if isinstance(value, bool):
        return 0
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _normalize_chunks(raw_chunks: list) -> list[dict]:
    """把 ``chunks_json`` 中的元素归一化为内部结构；非对象元素直接忽略。"""
    chunks: list[dict] = []
    for item in raw_chunks:
        if not isinstance(item, dict):
            continue
        chunks.append({
            "page": _chunk_page(item.get("page")),
            "chunk_id": str(item.get("chunk_id") or "").strip(),
            "heading_path": str(item.get("heading_path") or "").strip(),
            "text": str(item.get("text") or ""),
        })
    return chunks


def _collapse_whitespace(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _snippet(text: str, start: int, end: int, context: int) -> str:
    """命中位置前后各 ``context`` 个字符，折叠空白后作为预览。"""
    prefix = max(0, start - context)
    suffix = min(len(text), max(start, end) + context)
    return _collapse_whitespace(text[prefix:suffix])


# ---------------------------------------------------------------------------
# 内容摘要即版本
# ---------------------------------------------------------------------------


def document_version(chunks: list[dict] | str) -> str:
    """计算资料版本：``sha256(json.dumps(chunks, ensure_ascii=False, sort_keys=True))[:16]``。

    本轮不为 ``ai_documents`` 新增版本列，**内容摘要即版本**：同一份分块内容（与字典键顺序
    无关）必得同一摘要，分块内容一变摘要即变。传入 JSON 字符串时先解析（解析失败按空列表
    处理，返回空列表摘要）；调用方正常路径应传 :func:`_load_document` 解析后的数组。
    """
    if isinstance(chunks, (bytes, bytearray)):
        chunks = chunks.decode("utf-8", "ignore")
    if isinstance(chunks, str):
        text = chunks.strip()
        if not text:
            chunks = []
        else:
            try:
                chunks = json.loads(text)
            except ValueError:
                chunks = []
    if not isinstance(chunks, list):
        chunks = []
    try:
        serialized = json.dumps(chunks, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        serialized = json.dumps([], ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# 资料加载（只读 ai_documents）
# ---------------------------------------------------------------------------


def _load_document(user_id, document_id, con=None) -> dict:
    """加载当前用户的一份资料及其解析后的分块。

    * 只返回本人资料；资料不存在或属于他人一律 404「资料不存在或无权访问」（不泄露存在性）；
    * ``chunks_json`` 不是合法 JSON 数组时视为资料内容损坏，返回 503「资料内容暂不可用，请稍后
      重试」——结构损坏不是调用方错误，列表等读取方会捕获该异常并降级为「摘录失效」；
    * 返回 ``{"id", "user_id", "title", "pages", "status", "doc_version", "chunks"}``，其中
      ``doc_version`` 按 :func:`document_version` 由原始解析结果计算（内容摘要即版本）。
    """
    _ensure_ready()
    user_id = _validate_user_id(user_id)
    document_id = _validate_id(document_id, "资料参数")

    def _fetch(connection):
        return connection.execute(
            "SELECT id, user_id, title, pages, status, chunks_json "
            "FROM ai_documents WHERE id=? AND user_id=?",
            (document_id, user_id),
        ).fetchone()

    if con is None:
        with _read_connection() as own:
            row = _fetch(own)
    else:
        row = _fetch(con)
    if row is None:
        raise HTTPException(404, "资料不存在或无权访问")
    item = _row_dict(row)
    raw_chunks = _parse_chunks_json(item.get("chunks_json"))
    try:
        pages = max(0, int(item.get("pages") or 0))
    except (TypeError, ValueError):
        pages = 0
    return {
        "id": int(item.get("id")),
        "user_id": int(item.get("user_id") or 0),
        "title": str(item.get("title") or ""),
        "pages": pages,
        "status": str(item.get("status") or ""),
        "doc_version": document_version(raw_chunks),
        "chunks": _normalize_chunks(raw_chunks),
    }


# ---------------------------------------------------------------------------
# 摘录匹配：空白折叠 + ASCII 大小写不敏感，start/end 落在原始文本
# ---------------------------------------------------------------------------


def _normalize_with_positions(text: str) -> tuple[str, list[int]]:
    """返回 (折叠后文本, 折叠后每个字符在原文中的下标)。

    折叠规则：连续空白（含换行、制表符、全角空格）压成一个半角空格；仅 ASCII 字母 A–Z
    转为小写（非 ASCII 字符保持原样，避免中文/全角字母被误折叠）。
    """
    folded: list[str] = []
    positions: list[int] = []
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if char.isspace():
            end = index + 1
            while end < length and text[end].isspace():
                end += 1
            folded.append(" ")
            positions.append(index)
            index = end
            continue
        folded.append(chr(ord(char) + 32) if "A" <= char <= "Z" else char)
        positions.append(index)
        index += 1
    return "".join(folded), positions


def _match_quote_in_chunks(chunks: list[dict], quote: str):
    """在候选分块中按「空白折叠 + ASCII 大小写不敏感」找第一个命中。

    返回 ``(chunk, start, end, match)``；``start``/``end`` 是原始分块文本中的下标，
    ``match`` 为 ``exact``（原文逐字相同）或 ``normalized``（折叠后相同）。找不到返回 None。
    """
    normalized_quote, _ = _normalize_with_positions(quote)
    if not normalized_quote:
        return None
    for chunk in chunks:
        text = chunk["text"]
        if not text:
            continue
        normalized_text, positions = _normalize_with_positions(text)
        found = normalized_text.find(normalized_quote)
        if found < 0:
            continue
        start = positions[found]
        end = positions[found + len(normalized_quote) - 1] + 1
        matched = "exact" if text[start:end] == quote else "normalized"
        return chunk, start, end, matched
    return None


def _locate_payload(doc: dict, chunk: dict, start: int, end: int, match: str,
                    context: int, notice: str) -> dict:
    """组装定位结果（``start``/``end`` 为原始分块文本下标，``quote`` 为原文片段）。"""
    text = chunk["text"]
    located_quote = text[start:end] if end > start else ""
    return {
        "document_id": int(doc["id"]),
        "title": doc["title"],
        "doc_version": doc["doc_version"],
        "pages": doc["pages"],
        "page": chunk["page"],
        "chunk_id": chunk["chunk_id"],
        "heading_path": chunk["heading_path"],
        "quote": located_quote,
        "snippet": _snippet(text, start, end, context),
        "start": start,
        "end": end,
        "match": match,
        "valid": True,
        "notice": notice,
    }


# ---------------------------------------------------------------------------
# 定位
# ---------------------------------------------------------------------------


def locate(user_id, document_id, *, page=None, chunk_id=None, quote=None, context=DEFAULT_CONTEXT,
           con=None) -> dict:
    """在当前资料版本中定位一段原文（本人资料）。

    参数与匹配顺序：

    1. ``chunk_id`` 精确命中分块；若同时给 ``quote``，要求摘录也在该分块内（否则 422）。
       分块标识已失效且给了 ``quote`` 时，回退到全文按摘录定位（``notice`` 说明已重新定位）。
    2. ``quote``：在候选分块（给了 ``page`` 时只在该页内）按「空白折叠 + ASCII 大小写不敏感」
       找第一个命中，返回原文片段与原始偏移；找不到 → 422「在当前资料版本中未找到该摘录」。
    3. 只给 ``page``：定位该页第一个分块的开头（``start=end=0``、``quote=""``）。

    至少提供 ``page`` / ``chunk_id`` / ``quote`` 之一，否则 422。资料无任何可定位文本（如扫描件
    未 OCR）时 422「该资料还没有可定位的文本……」。返回字段见 :func:`_locate_payload`。
    """
    _ensure_ready()
    user_id = _validate_user_id(user_id)
    document_id = _validate_id(document_id, "资料参数")
    page = _validate_page(page)
    chunk_id = _validate_chunk_id(chunk_id)
    quote_value = _validate_quote(quote) if quote is not None else None
    if page is None and chunk_id is None and quote_value is None:
        raise HTTPException(422, "请提供 quote、chunk_id 或 page 之一")
    context = _validate_context(context)

    doc = _load_document(user_id, document_id, con=con)
    chunks = doc["chunks"]
    if not any(chunk["text"].strip() for chunk in chunks):
        raise HTTPException(422, "该资料还没有可定位的文本，请先完成文字识别或校对")

    if chunk_id is not None:
        target = next((chunk for chunk in chunks if chunk["chunk_id"] == chunk_id), None)
        if target is not None:
            if quote_value is None:
                return _locate_payload(doc, target, 0, 0, "exact", context, NOTICE_CHUNK_ANCHOR)
            hit = _match_quote_in_chunks([target], quote_value)
            if hit is None:
                raise HTTPException(422, "在当前资料版本中未找到该摘录")
            chunk, start, end, matched = hit
            notice = "" if matched == "exact" else NOTICE_NORMALIZED
            return _locate_payload(doc, chunk, start, end, matched, context, notice)
        if quote_value is None:
            raise HTTPException(422, "在当前资料版本中未找到该片段")
        candidates = [chunk for chunk in chunks if chunk["page"] == page] if page is not None else chunks
        hit = _match_quote_in_chunks(candidates, quote_value)
        if hit is None:
            raise HTTPException(422, "在当前资料版本中未找到该摘录")
        chunk, start, end, matched = hit
        return _locate_payload(doc, chunk, start, end, matched, context, NOTICE_CHUNK_FALLBACK)

    if quote_value is not None:
        candidates = [chunk for chunk in chunks if chunk["page"] == page] if page is not None else chunks
        hit = _match_quote_in_chunks(candidates, quote_value)
        if hit is None:
            raise HTTPException(422, "在当前资料版本中未找到该摘录")
        chunk, start, end, matched = hit
        notice = "" if matched == "exact" else NOTICE_NORMALIZED
        return _locate_payload(doc, chunk, start, end, matched, context, notice)

    candidates = [chunk for chunk in chunks if chunk["page"] == page]
    if not candidates:
        total_pages = doc["pages"] or max((chunk["page"] for chunk in chunks), default=0)
        raise HTTPException(
            422,
            f"第 {page} 页在当前资料版本中没有可定位的原文（资料共 {total_pages} 页）",
        )
    return _locate_payload(doc, candidates[0], 0, 0, "exact", context, NOTICE_PAGE_ANCHOR)


# ---------------------------------------------------------------------------
# 摘录：创建 / 列表 / 刷新 / 删除
# ---------------------------------------------------------------------------


def _annotation_public(row: dict, *, valid: bool, invalid_reason: str) -> dict:
    """摘录的公开结构（列表、创建、刷新共用同一口径）。"""
    data = _row_dict(row)
    note_id = data.get("note_id")
    return {
        "annotation_id": int(data["id"]),
        "document_id": int(data["document_id"]),
        "note_id": int(note_id) if note_id is not None else None,
        "page": int(data.get("page") or 0),
        "chunk_id": str(data.get("chunk_id") or ""),
        "heading_path": str(data.get("heading_path") or ""),
        "quote": str(data.get("quote") or ""),
        "snippet": str(data.get("snippet") or ""),
        "color": str(data.get("color") or DEFAULT_COLOR),
        "note": str(data.get("note") or ""),
        "doc_version": str(data.get("doc_version") or ""),
        "valid": bool(valid),
        "invalid_reason": invalid_reason,
        "created_at": str(data.get("created_at") or ""),
        "updated_at": str(data.get("updated_at") or ""),
    }


def _annotation_validity(user_id: int, annotation: dict, con) -> tuple[bool, str]:
    """实时校验一条摘录；任何异常都降级为「失效」，保证列表接口不 500。"""
    try:
        doc = _load_document(user_id, annotation.get("document_id"), con=con)
    except HTTPException as error:
        if error.status_code == 404:
            return False, INVALID_REASON_MISSING
        return False, INVALID_REASON_VERSION
    except Exception:  # noqa: BLE001 - 单条摘录校验失败不能拖垮整个列表
        return False, INVALID_REASON_VERSION
    try:
        hit = _match_quote_in_chunks(doc["chunks"], str(annotation.get("quote") or ""))
    except Exception:  # noqa: BLE001
        return False, INVALID_REASON_VERSION
    if hit is None:
        return False, INVALID_REASON_QUOTE
    if doc["doc_version"] != str(annotation.get("doc_version") or ""):
        return False, INVALID_REASON_VERSION
    return True, ""


def _note_owned(con, note_id: int, user_id: int) -> bool:
    """``note_id`` 必须指向本人 ``ai_notes``（缺失/他人统一按 404 处理）。"""
    row = con.execute(
        "SELECT id FROM ai_notes WHERE id=? AND user_id=?", (note_id, user_id)
    ).fetchone()
    return row is not None


def create_annotation(user_id, *, document_id, quote, note=None, page=None, chunk_id=None,
                      note_id=None, color=None, con=None) -> dict:
    """保存一条原文摘录：先按 quote 在当前版本定位，再落 quote / doc_version / heading_path 快照。

    * 定位失败（摘录在当前资料版本中找不到、资料无权访问）直接复用 :func:`locate` 的错误；
    * ``note_id`` 若传入必须是本人 ``ai_notes``，否则 404「笔记不存在或无权访问」；
    * ``color`` 白名单 ``yellow|green|blue|pink``（默认 ``yellow``），非法 422；
    * 单份资料最多 :data:`MAX_ANNOTATIONS_PER_DOC`（200）条，超限 422；
    * 定位之后、提交之前资料又被更新（PostgreSQL 下并发写无法靠 SQLite 的整库写锁拦住）时
      不在旧版本上落快照，返回 409「资料在保存摘录期间发生变化，请重新定位后再保存」；
    * 返回 ``{"annotation": {...}}``，字段与列表条目一致。
    """
    _ensure_ready()
    user_id = _validate_user_id(user_id)
    document_id = _validate_id(document_id, "资料参数")
    page = _validate_page(page)
    chunk_id = _validate_chunk_id(chunk_id)
    quote_value = _validate_quote(quote)
    note_value = _validate_note(note)
    color_value = _validate_color(color)
    note_id_value = _validate_id(note_id, "笔记参数") if note_id is not None else None

    with _write_connection(con) as write_con:
        located = locate(
            user_id, document_id, page=page, chunk_id=chunk_id, quote=quote_value, con=write_con,
        )
        # 409 版本冲突：PostgreSQL 下资料可能被其他进程在定位与提交之间更新，不能把旧版本
        # 快照写进摘录；重新读一次当前内容摘要比对，不一致就让调用方重新定位。
        if _load_document(user_id, document_id, con=write_con)["doc_version"] != located["doc_version"]:
            raise HTTPException(409, "资料在保存摘录期间发生变化，请重新定位后再保存")
        if note_id_value is not None and not _note_owned(write_con, note_id_value, user_id):
            raise HTTPException(404, "笔记不存在或无权访问")
        count = int(write_con.execute(
            "SELECT COUNT(*) FROM doc_annotations WHERE user_id=? AND document_id=?",
            (user_id, document_id),
        ).fetchone()[0])
        if count >= MAX_ANNOTATIONS_PER_DOC:
            raise HTTPException(
                422,
                f"单份资料的摘录最多 {MAX_ANNOTATIONS_PER_DOC} 条，请先清理不再需要的摘录",
            )
        now = db.utcnow()
        row = {
            "user_id": user_id,
            "document_id": located["document_id"],
            "note_id": note_id_value,
            "page": located["page"],
            "chunk_id": located["chunk_id"],
            "heading_path": located["heading_path"],
            "quote": located["quote"],
            "snippet": located["snippet"],
            "color": color_value,
            "note": note_value,
            "doc_version": located["doc_version"],
            "created_at": now,
            "updated_at": now,
        }
        annotation_id = write_con.execute(
            "INSERT INTO doc_annotations("
            "user_id, document_id, note_id, page, chunk_id, heading_path, quote, snippet, "
            "color, note, doc_version, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                row["user_id"], row["document_id"], row["note_id"], row["page"],
                row["chunk_id"], row["heading_path"], row["quote"], row["snippet"],
                row["color"], row["note"], row["doc_version"],
                row["created_at"], row["updated_at"],
            ),
        ).lastrowid
        if annotation_id is None:
            raise HTTPException(503, "摘录保存失败，请稍后重试")
        row["id"] = int(annotation_id)
    return {"annotation": _annotation_public(row, valid=True, invalid_reason="")}


def list_annotations(user_id, *, document_id=None, note_id=None, page=None, limit=DEFAULT_LIST_LIMIT,
                     con=None) -> dict:
    """按 ``created_at DESC`` 列出本人摘录（可按资料 / 笔记 / 页码过滤），并实时校验有效性。

    每条摘录在返回前用当前资料实时校验：资料仍在本人名下、当前 doc_version 与快照一致、
    当前版本仍能找到 quote；不满足的条目照常返回 ``valid:false`` + 中文 ``invalid_reason``，
    单条校验异常只降级该条，整个列表不会 500。返回 ``{"items": [...], "total": int}``
    （``total`` 是过滤后的总条数，``items`` 最多 ``limit`` 条）。
    """
    _ensure_ready()
    user_id = _validate_user_id(user_id)
    document_id = _validate_id(document_id, "资料参数") if document_id is not None else None
    note_id_value = _validate_id(note_id, "笔记参数") if note_id is not None else None
    page = _validate_page(page)
    limit = _validate_limit(limit)

    where = "user_id=?"
    params: list = [user_id]
    if document_id is not None:
        where += " AND document_id=?"
        params.append(document_id)
    if note_id_value is not None:
        where += " AND note_id=?"
        params.append(note_id_value)
    if page is not None:
        where += " AND page=?"
        params.append(page)

    with _read_connection(con) as read_con:
        total = int(read_con.execute(
            f"SELECT COUNT(*) FROM doc_annotations WHERE {where}", params
        ).fetchone()[0])
        rows = read_con.execute(
            f"SELECT * FROM doc_annotations WHERE {where} ORDER BY created_at DESC, id DESC LIMIT ?",
            (*params, limit),
        ).fetchall()
        items = []
        for row in rows:
            annotation = _row_dict(row)
            valid, reason = _annotation_validity(user_id, annotation, read_con)
            items.append(_annotation_public(annotation, valid=valid, invalid_reason=reason))
    return {"items": items, "total": total}


def refresh_annotation(user_id, annotation_id, con=None) -> dict:
    """在当前资料版本重新定位本人摘录；找到则更新位置快照，找不到仍返回 200 但 ``valid:false``。

    * 摘录不存在或属于他人 → 404「摘录不存在或无权访问」；
    * 资料已删除/无权访问 → 200, ``migrated:false, valid:false``，原因「资料已删除或无权访问」；
    * 当前版本找不到该摘录 → 200, ``migrated:false, valid:false``，原因「当前版本中已找不到该摘录」；
    * 找到 → 更新 ``doc_version/page/chunk_id/heading_path/snippet/updated_at``，
      ``migrated:true, valid:true``（即使版本未变化也重新核对并刷新 ``updated_at``）。
    返回 ``{"annotation", "migrated", "valid", "invalid_reason", "notice"}``。
    """
    _ensure_ready()
    user_id = _validate_user_id(user_id)
    annotation_id = _validate_id(annotation_id, "摘录参数")
    with _write_connection(con) as write_con:
        row = write_con.execute(
            "SELECT * FROM doc_annotations WHERE id=? AND user_id=?", (annotation_id, user_id)
        ).fetchone()
        if row is None:
            raise HTTPException(404, "摘录不存在或无权访问")
        annotation = _row_dict(row)
        valid = False
        migrated = False
        reason = ""
        notice = ""
        doc = None
        try:
            doc = _load_document(user_id, annotation.get("document_id"), con=write_con)
        except HTTPException as error:
            reason = (
                INVALID_REASON_MISSING if error.status_code == 404 else INVALID_REASON_VERSION
            )
        except Exception:  # noqa: BLE001 - 资料读取异常按「需重新定位」处理，不抛 500
            reason = INVALID_REASON_VERSION
        hit = None
        if doc is not None:
            try:
                hit = _match_quote_in_chunks(doc["chunks"], str(annotation.get("quote") or ""))
            except Exception:  # noqa: BLE001
                hit = None
            if hit is None:
                reason = INVALID_REASON_QUOTE
        if hit is not None:
            chunk, start, end, _matched = hit
            now = db.utcnow()
            snippet = _snippet(chunk["text"], start, end, DEFAULT_CONTEXT)
            write_con.execute(
                "UPDATE doc_annotations SET doc_version=?, page=?, chunk_id=?, heading_path=?, "
                "snippet=?, updated_at=? WHERE id=? AND user_id=?",
                (
                    doc["doc_version"], chunk["page"], chunk["chunk_id"], chunk["heading_path"],
                    snippet, now, annotation_id, user_id,
                ),
            )
            annotation.update({
                "doc_version": doc["doc_version"],
                "page": chunk["page"],
                "chunk_id": chunk["chunk_id"],
                "heading_path": chunk["heading_path"],
                "snippet": snippet,
                "updated_at": now,
            })
            valid = True
            migrated = True
            notice = NOTICE_REFRESHED
        else:
            notice = reason
    return {
        "annotation": _annotation_public(annotation, valid=valid, invalid_reason=reason),
        "migrated": migrated,
        "valid": valid,
        "invalid_reason": reason,
        "notice": notice,
    }


def delete_annotation(user_id, annotation_id, con=None) -> dict:
    """删除本人摘录；不存在或属于他人一律 404「摘录不存在或无权访问」。"""
    _ensure_ready()
    user_id = _validate_user_id(user_id)
    annotation_id = _validate_id(annotation_id, "摘录参数")
    with _write_connection(con) as write_con:
        row = write_con.execute(
            "SELECT id FROM doc_annotations WHERE id=? AND user_id=?", (annotation_id, user_id)
        ).fetchone()
        if row is None:
            raise HTTPException(404, "摘录不存在或无权访问")
        write_con.execute(
            "DELETE FROM doc_annotations WHERE id=? AND user_id=?", (annotation_id, user_id)
        )
    return {"deleted": True, "annotation_id": annotation_id}


# ---------------------------------------------------------------------------
# HTTP 路由（全部 Depends(current_user)）
# ---------------------------------------------------------------------------


class LocateInput(BaseModel):
    """``POST /api/documents/{document_id}/locate`` 请求体；``page/chunk_id/quote`` 至少一项。"""

    page: int | None = None
    chunk_id: str | None = None
    quote: str | None = None


class AnnotationInput(BaseModel):
    """``POST /api/doc-annotations`` 请求体。"""

    document_id: int
    quote: str
    note: str | None = None
    page: int | None = None
    chunk_id: str | None = None
    note_id: int | None = None
    color: str | None = None


@router_docs.post("/{document_id}/locate")
def locate_document(
    document_id: int,
    data: LocateInput | None = None,
    user=Depends(current_user),
):
    """按页码 / 片段标识 / 摘录在当前资料版本中定位原文（本人资料；跨用户 404）。"""
    payload = data or LocateInput()
    return locate(
        user["id"], document_id,
        page=payload.page, chunk_id=payload.chunk_id, quote=payload.quote,
    )


@router.post("", status_code=201)
def create_document_annotation(data: AnnotationInput, user=Depends(current_user)):
    """保存一条原文摘录（先按 quote 定位校验；定位失败 422，跨用户资料 404）。"""
    return create_annotation(
        user["id"],
        document_id=data.document_id,
        quote=data.quote,
        note=data.note,
        page=data.page,
        chunk_id=data.chunk_id,
        note_id=data.note_id,
        color=data.color,
    )


@router.get("")
def list_document_annotations(
    document_id: int | None = Query(default=None),
    note_id: int | None = Query(default=None),
    page: int | None = Query(default=None),
    limit: int = Query(default=DEFAULT_LIST_LIMIT),
    user=Depends(current_user),
):
    """列出本人摘录（按创建时间倒序，可按资料 / 笔记 / 页码过滤），失效条目照常返回。"""
    return list_annotations(
        user["id"],
        document_id=document_id, note_id=note_id, page=page, limit=limit,
    )


@router.post("/{annotation_id}/refresh")
def refresh_document_annotation(annotation_id: int, user=Depends(current_user)):
    """按当前资料版本重新定位本人摘录；找不到仍是 200，返回 ``valid:false`` 与中文原因。"""
    return refresh_annotation(user["id"], annotation_id)


@router.delete("/{annotation_id}")
def delete_document_annotation(annotation_id: int, user=Depends(current_user)):
    """删除本人摘录；不存在或属于他人 404。"""
    return delete_annotation(user["id"], annotation_id)


__all__ = [
    "router_docs", "router", "init_doc_location", "document_version", "locate",
    "create_annotation", "list_annotations", "refresh_annotation", "delete_annotation",
    "MAX_PAGE", "MAX_CHUNK_ID_LENGTH", "MAX_QUOTE_LENGTH", "MAX_NOTE_LENGTH",
    "MAX_ANNOTATIONS_PER_DOC", "MAX_LIST_LIMIT", "DEFAULT_LIST_LIMIT",
    "DEFAULT_CONTEXT", "MAX_CONTEXT", "ANNOTATION_COLORS", "DEFAULT_COLOR",
    "INVALID_REASON_MISSING", "INVALID_REASON_VERSION", "INVALID_REASON_QUOTE",
]
