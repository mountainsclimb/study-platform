"""带引文的专题报告（服务端规则汇编，**非 AI 生成**；总方案 §5.3「后续功能：专题调研」/ §7 M6）。

本模块是 M6 的**专题报告**服务端模块：把本人已有的资料片段、笔记摘录、学习附件与题库候选题
按固定规则汇编成一份带真实出处的 Markdown 报告，保存为可下载、可改标题/正文、可版本化删除的
规整数据。报告里的每一处引用都来自数据库中的真实行（``ai_documents`` / ``ai_notes`` /
``doc_annotations`` / ``study_artifacts`` / 题库检索结果），**不调用任何 AI 模型**，不编造资料、
页码、摘录或题目；数据缺失时只写「未检索到」而不是补写内容。``generator`` 固定为 ``'rules'``，
报告正文也显式注明「规则汇编，非 AI 生成」。

## 报告结构（``content_md``）

1. 标题（默认「专题报告 · <topic>」）、生成时间与「规则汇编」声明；
2. ``## 一、主题与范围``：主题、四类来源数量（资料 / 笔记 / 知识点 / 学习附件）、生成时间；
3. ``## 二、资料依据``：每份资料最多 2 段**包含主题关键字**的分块原文（ASCII 大小写不敏感），
   格式 ``> <分块前 200 字>`` + ``（来源：《<title>》第 <page> 页 · chunk <chunk_id> · 文档版本 <doc_version>）``；
   没有命中时改用资料开头 2 段，并注明「未检索到与主题直接相关的段落，以下为资料开头摘录」；
4. ``## 三、笔记摘录``：每篇笔记标题与内容前 200 字；再调用 :func:`app.doc_location.list_annotations`
   取该笔记最多 5 条原文摘录，格式 ``> <quote>`` + ``（来源：《<文档标题>》第 N 页 · 摘录 #id）``；
   没有摘录就不写，绝不虚构；
5. ``## 四、学习附件``：每份附件的标题、``kind``、固定版本 ``vN`` 与「可在知序中按该版本打开」；
   只引用 ``artifact_id + revision``，不内联附件内容或 base64；
6. ``## 五、相关练习``：调用 :func:`app.practice.search_similar_questions` 取最多 5 道候选题，
   逐条列出题干前 100 字与题目引用；注明「候选题为题库检索结果，非正式知识点标签」；
7. ``## 六、学习证据``：调用 :func:`app.study_evidence.summary` 的近 90 天统计，**只取真实字段**
   （有效证据、已判定、独立自评、缺失提示/揭晓信息等），并注明样本是否充足；
8. ``## 七、口径与局限``：规则汇编、非 AI 生成；引用随资料版本 / 摘录删除可能失效；原文只摘录不改写。

文档版本沿用 :func:`app.doc_location.document_version` 的「内容摘要即版本」口径（分块 JSON 的
sha256 前 16 位），与资料定位/摘录模块一致，便于前端与用户对照同一版本。

## ``sources`` 列表

真实来源清单，元素形如
``{kind: 'document'|'note'|'annotation'|'artifact'|'question', id, title, ref?, page?, chunk_id?, quote?, version?, note?}``；
``kind='document'`` 按**被引用的分块**逐条记录（同一资料最多 2 条），``kind='annotation'`` 指向
具体摘录，``kind='artifact'`` 的 ``version`` 是生成时固定的附件版本号，``kind='question'`` 的 ``ref``
是统一题目引用（``bank:<qid>`` 等）。来源列表与 ``content_md`` 一一对应，不包含未真实读取到的对象。

## 错误口径（detail 全部中文）

* ``404``：专题报告不存在或不属于当前用户（同一文案，不泄露存在性）；资料 / 笔记 / 学习附件不存在、
  已删除或属于他人。归属校验一律「不存在或无权访问」，不透露其他账号是否有该资源；
* ``409``：``update_report`` 的 ``expected_version`` 与当前版本不一致（乐观锁冲突，含 ``0`` 这种
  过期版本号），不写入任何内容；
* ``422``：主题长度越界、四类来源全空、每类超过 10 个、个人报告数达上限、知识点不存在、标题 /
  正文超长或正文不是文本、``expected_version`` 非法、请求体结构非法等参数与归属校验失败；
* 本模块不引入 503：题库 / 学习证据 / 摘录读取失败时在报告正文里如实降级说明，不把内部数据问题
  当成本模块的错误码。资料 ``chunks_json`` 结构损坏也只降级为「该资料暂不可引用」，不抛 500。

## 存储与并发

* 表 ``topic_reports`` 只在 :func:`init_topic_reports` 里用 ``con.executescript`` 幂等创建；主键已在
  ``app/db.py::_ID_TABLES`` 登记，PostgreSQL 下 ``INSERT`` 自动带 ``RETURNING id``，与 SQLite 的
  ``lastrowid`` 口径一致。
* 只通过 ``app.db.connection()`` 访问应用库；JSON 列存 TEXT（``ensure_ascii=False, sort_keys=True``
  稳定序列化）；SQL 占位符统一 ``?``；时间统一 :func:`app.db.utcnow` 的 UTC ISO-8601 字符串，
  不使用 SQLite 专有语法，双引擎行为一致。
* 所有写路径先取 ``db.write_lock(con, "topic-report")`` 串行化；``create_report`` 先在锁外完成
  只读校验与规则汇编（避免长事务），再在写锁内复查报告数量并插入。

## 接线方式

应用启动时调用 :func:`init_topic_reports`，并把 :data:`router` 挂到主应用：
``from . import topic_report; topic_report.init_topic_reports(); app.include_router(topic_report.router)``。
"""
from __future__ import annotations

from contextlib import contextmanager
import json
import re

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel

from . import db, doc_location, practice, question_ref, study_artifact, study_evidence
from .auth import current_user

router = APIRouter(prefix="/api/topic-reports", tags=["Topic reports"])

# ---------------------------------------------------------------------------
# 常量与中文口径
# ---------------------------------------------------------------------------

#: 报告生成器标记：固定为规则汇编，绝不冒充 AI 生成。
GENERATOR = "rules"
GENERATOR_LABEL = "规则汇编"
GENERATOR_NOTE = (
    "本报告由规则汇编生成（检索资料片段、笔记摘录、学习附件与相关练习），不含 AI 生成内容。"
)
STATUS_READY = "ready"

#: 主题长度（按去除首尾空白后的字符数计）。
TOPIC_MIN_LENGTH = 2
TOPIC_MAX_LENGTH = 200
#: 每类来源最多 10 个（资料 / 笔记 / 知识点 / 学习附件各自计数）。
MAX_SOURCES_PER_KIND = 10
#: 单个用户最多保存的报告数。
MAX_REPORTS_PER_USER = 50
#: 标题上限与正文上限（正文按 UTF-8 字节计）。
MAX_TITLE_LENGTH = 120
MAX_CONTENT_BYTES = 200 * 1024
#: 列表默认 / 最大返回条数。
DEFAULT_LIMIT = 20
MAX_LIMIT = 100
#: 引用摘录长度与每题最多摘录条数。
CHUNK_EXCERPT_CHARS = 200
NOTE_EXCERPT_CHARS = 200
QUESTION_STEM_CHARS = 100
MAX_ANNOTATIONS_PER_NOTE = 5
QUESTION_LIMIT = 5
#: 学习证据统计窗口（天）与报告级「样本充足」门槛（有效证据条数）。
EVIDENCE_DAYS = 90
EVIDENCE_MIN_EFFECTIVE = 5

NO_SOURCES_DETAIL = "请至少选择一份资料、笔记、知识点或学习附件"
LIST_NOTE = (
    "专题报告是规则汇编快照：引用来自生成时的资料版本、摘录与附件版本，"
    "资料更新、摘录删除或附件改版后旧引用需重新生成核对。"
)
LIMIT_DETAIL_TEMPLATE = "专题报告数量已达上限（%s 份），请先删除不再需要的报告后再创建"
TOPIC_FALLBACK_EXCERPT_NOTE = "未检索到与主题直接相关的段落，以下为资料开头摘录"
CORRUPT_CHUNKS_NOTE = "该资料的分块数据无法解析，暂不能引用原文片段（可重新上传或校对后再生成）。"
EMPTY_CHUNKS_NOTE = "该资料暂无可摘录的文字。"
NO_QUESTIONS_NOTE = "未检索到相关候选题。"
QUESTION_CALIBER_NOTE = "候选题为题库检索结果，非正式知识点标签。"
QUESTION_UNAVAILABLE_NOTE = "题库暂不可用，未检索到候选题（不影响报告其余部分）。"

CALIBER = {
    "generator": "报告由服务端规则汇编生成（generator=rules），不含 AI 生成内容。",
    "citations": "引用来自真实资料分块、笔记摘录、学习附件版本与题库检索结果；原文只摘录不改写。",
    "freshness": "资料版本按 contents digest 计算；资料更新、摘录删除或附件改版后旧引用可能失效。",
    "questions": "候选题为题库检索结果，非正式知识点标签，也不是正式推荐。",
    "evidence": "学习证据只统计 study_evidence 的真实字段；样本不足时不推断掌握程度。",
}

# ---------------------------------------------------------------------------
# 表结构 / 初始化
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS topic_reports(
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL,
    topic TEXT NOT NULL,
    title TEXT NOT NULL,
    generator TEXT NOT NULL DEFAULT 'rules',
    content_md TEXT NOT NULL,
    sources_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'ready',
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_topic_reports_user
    ON topic_reports(user_id, updated_at);
"""

#: 进程内就绪标记（数据目录 + 存储引擎）：未经 lifespan 直接调用时也保证表存在。
_ready_key: tuple[str, bool] | None = None


def _schema_key() -> tuple[str, bool]:
    return (str(db.DATA_DIR), db.postgres_enabled())


def init_topic_reports() -> None:
    """幂等建表：应用启动时调用（不写进 ``app/db.py``），重复调用不报错。"""
    global _ready_key
    with db.connection() as con:
        db.write_lock(con, "topic-report-schema")
        con.executescript(_SCHEMA)
    _ready_key = _schema_key()


def _ensure_ready() -> None:
    """懒初始化：直接调用本模块函数（未经 lifespan）时也保证 ``topic_reports`` 存在。"""
    if _ready_key != _schema_key():
        init_topic_reports()


# ---------------------------------------------------------------------------
# 连接与写锁
# ---------------------------------------------------------------------------


def _lock_write(con) -> None:
    """在写事务内取跨进程命名写锁；外层事务已持锁时（SQLite 不可嵌套 BEGIN IMMEDIATE）复用。"""
    if getattr(con, "in_transaction", False):
        return
    db.write_lock(con, "topic-report")


@contextmanager
def _write_connection(con=None):
    """取得写连接：``con`` 为空时自建并提交，否则复用调用方事务（由调用方提交）。"""
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
# 行对象 / JSON / 文本小工具
# ---------------------------------------------------------------------------


def _row_dict(row) -> dict:
    """把 sqlite3.Row / PostgreSQL 伪行 / dict 统一转成 dict（两种行都支持 keys()）。"""
    if isinstance(row, dict):
        return dict(row)
    return {key: row[key] for key in row.keys()}


def _parse_sources(raw) -> list[dict]:
    """解析 ``sources_json``：缺失 / 损坏 / 非数组一律降级为空列表，读取路径不 500。"""
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", "ignore")
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return []
        try:
            raw = json.loads(text)
        except ValueError:
            return []
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict)]


def _dump_sources(sources: list[dict]) -> str:
    """稳定序列化来源列表（键排序 + 不转义中文），同输入必得同文本。"""
    return json.dumps(sources, ensure_ascii=False, sort_keys=True)


def _collapse_whitespace(text) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _excerpt(text, limit: int) -> str:
    """折叠空白后取前 ``limit`` 个字符（引用行必须单行，避免破坏 Markdown 引用块）。"""
    return _collapse_whitespace(text)[: max(0, int(limit))]


def _page_number(value) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


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


def _validate_id(value, label: str = "报告") -> int:
    """校验报告 / 资料 / 笔记 / 知识点编号（>=1）。"""
    if isinstance(value, bool) or value is None:
        raise HTTPException(422, "%s编号不正确" % label)
    if isinstance(value, float):
        if not value.is_integer():
            raise HTTPException(422, "%s编号不正确" % label)
        value = int(value)
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise HTTPException(422, "%s编号不正确" % label) from None
    if number < 1:
        raise HTTPException(422, "%s编号不正确" % label)
    return number


def _validate_topic(topic) -> str:
    if not isinstance(topic, str):
        raise HTTPException(422, "主题必须是文本")
    value = topic.strip()
    if len(value) < TOPIC_MIN_LENGTH or len(value) > TOPIC_MAX_LENGTH:
        raise HTTPException(
            422,
            "主题需在 %s 到 %s 个字符之间（当前 %s 个字符）"
            % (TOPIC_MIN_LENGTH, TOPIC_MAX_LENGTH, len(value)),
        )
    return value


def _default_title(topic: str) -> str:
    """默认标题「专题报告 · <topic>」；主题极长时截断到标题上限内。"""
    return ("专题报告 · %s" % topic)[:MAX_TITLE_LENGTH]


def _validate_title(title, *, topic: str | None = None) -> str:
    """标题校验：``None`` 用默认标题；显式标题去空白后 1..120 字。"""
    if title is None:
        return _default_title(topic or "")
    if not isinstance(title, str):
        raise HTTPException(422, "标题必须是文本")
    value = title.strip()
    if not value:
        raise HTTPException(422, "标题不能为空")
    if len(value) > MAX_TITLE_LENGTH:
        raise HTTPException(422, "标题最多 %s 个字符" % MAX_TITLE_LENGTH)
    return value


def _validate_content_md(content_md) -> str:
    """正文校验：必须是非空文本，UTF-8 编码后不超过 200 KB。"""
    if not isinstance(content_md, str):
        raise HTTPException(422, "报告正文必须是文本")
    if not content_md.strip():
        raise HTTPException(422, "报告正文不能为空")
    size = len(content_md.encode("utf-8"))
    if size > MAX_CONTENT_BYTES:
        raise HTTPException(
            422,
            "报告正文超过 200 KB 上限（当前 %s 字节），请精简后再保存" % size,
        )
    return content_md


def _validate_expected_version(value) -> int:
    """乐观锁版本号：允许 ``0``（必然过期 → 由调用方返回 409），负数 / 非整数 → 422。"""
    if isinstance(value, bool) or value is None:
        raise HTTPException(422, "版本号必须是整数")
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise HTTPException(422, "版本号必须是整数") from None
    if number < 0:
        raise HTTPException(422, "版本号不能为负数")
    return number


def _normalize_limit(limit) -> int:
    """列表条数：非法值退回默认，超界裁剪到 1..100（只影响展示，不作为 422）。"""
    if isinstance(limit, bool):
        return DEFAULT_LIMIT
    try:
        value = int(limit)
    except (TypeError, ValueError):
        return DEFAULT_LIMIT
    return max(1, min(MAX_LIMIT, value))


def _normalize_id_list(values, label: str) -> list[int]:
    """编号数组校验：去重保序、每类最多 10 个；结构非法 / 编号非法 → 422。"""
    if values is None:
        return []
    if isinstance(values, (str, bytes)) or not isinstance(values, (list, tuple)):
        raise HTTPException(422, "%s编号必须是数组" % label)
    ordered: list[int] = []
    for item in values:
        value = _validate_id(item, label)
        if value not in ordered:
            ordered.append(value)
    if len(ordered) > MAX_SOURCES_PER_KIND:
        raise HTTPException(
            422, "每类来源最多 %s 个（%s当前 %s 个）" % (MAX_SOURCES_PER_KIND, label, len(ordered))
        )
    return ordered


def _normalize_artifact_ids(values) -> list[str]:
    """学习附件公开 ID 数组：非空字符串、去重保序、最多 10 个；结构非法 → 422。"""
    if values is None:
        return []
    if isinstance(values, (str, bytes)) or not isinstance(values, (list, tuple)):
        raise HTTPException(422, "附件编号必须是数组")
    ordered: list[str] = []
    for item in values:
        if not isinstance(item, str) or not item.strip():
            raise HTTPException(422, "附件编号不正确")
        value = item.strip()
        if value not in ordered:
            ordered.append(value)
    if len(ordered) > MAX_SOURCES_PER_KIND:
        raise HTTPException(
            422, "每类来源最多 %s 个（附件当前 %s 个）" % (MAX_SOURCES_PER_KIND, len(ordered))
        )
    return ordered


# ---------------------------------------------------------------------------
# 只读加载：资料 / 笔记 / 知识点 / 附件 / 摘录 / 候选题 / 学习证据
# ---------------------------------------------------------------------------


def _parse_chunks_json(raw) -> tuple[list[dict], bool]:
    """解析 ``chunks_json``：返回 ``(归一化分块, 是否结构损坏)``。

    分块元素口径与 :mod:`app.doc_location` 一致（``page/chunk_id/heading_path/text``）；
    结构损坏只标记，不抛 503——报告降级为「暂不可引用」，其余来源照常汇编。
    """
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", "ignore")
    if raw is None:
        return [], False
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return [], False
        try:
            raw = json.loads(text)
        except ValueError:
            return [], True
    if not isinstance(raw, list):
        return [], True
    chunks: list[dict] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        chunks.append({
            "page": _page_number(item.get("page")),
            "chunk_id": str(item.get("chunk_id") or "").strip(),
            "heading_path": str(item.get("heading_path") or "").strip(),
            "text": str(item.get("text") or ""),
        })
    return chunks, False


def _load_documents(user_id: int, document_ids: list[int]) -> list[dict]:
    """按输入顺序读取本人资料（id/title/分块/内容摘要版本）；任一不存在或非本人 → 404。"""
    if not document_ids:
        return []
    marks = ",".join("?" for _ in document_ids)
    with db.connection() as con:
        rows = con.execute(
            "SELECT id, title, pages, chunks_json FROM ai_documents "
            "WHERE user_id=? AND id IN (%s)" % marks,
            (user_id, *document_ids),
        ).fetchall()
    by_id = {int(row["id"]): _row_dict(row) for row in rows}
    missing = [document_id for document_id in document_ids if document_id not in by_id]
    if missing:
        raise HTTPException(404, "资料不存在或无权访问")
    documents: list[dict] = []
    for document_id in document_ids:
        row = by_id[document_id]
        chunks, corrupt = _parse_chunks_json(row.get("chunks_json"))
        try:
            version = doc_location.document_version(row.get("chunks_json"))
        except Exception:  # noqa: BLE001 - 版本摘要失败不能拖垮整份报告
            version = ""
        documents.append({
            "id": int(row["id"]),
            "title": str(row.get("title") or ""),
            "pages": _page_number(row.get("pages")),
            "chunks": chunks,
            "corrupt": corrupt,
            "version": version,
        })
    return documents


def _load_notes(user_id: int, note_ids: list[int]) -> list[dict]:
    """按输入顺序读取本人笔记（id/title/content）；任一不存在或非本人 → 404。"""
    if not note_ids:
        return []
    marks = ",".join("?" for _ in note_ids)
    with db.connection() as con:
        rows = con.execute(
            "SELECT id, title, content FROM ai_notes "
            "WHERE user_id=? AND id IN (%s)" % marks,
            (user_id, *note_ids),
        ).fetchall()
    by_id = {int(row["id"]): _row_dict(row) for row in rows}
    if any(note_id not in by_id for note_id in note_ids):
        raise HTTPException(404, "笔记不存在或无权访问")
    return [
        {
            "id": int(by_id[note_id]["id"]),
            "title": str(by_id[note_id].get("title") or ""),
            "content": str(by_id[note_id].get("content") or ""),
        }
        for note_id in note_ids
    ]


def _load_knowledge_points(kp_ids: list[int]) -> list[dict]:
    """按输入顺序读取知识点；任一不存在 → 422（知识点必须真实存在，不猜测）。"""
    if not kp_ids:
        return []
    marks = ",".join("?" for _ in kp_ids)
    with db.connection() as con:
        rows = con.execute(
            "SELECT id, name, subject FROM knowledge_points WHERE id IN (%s)" % marks,
            tuple(kp_ids),
        ).fetchall()
    by_id = {int(row["id"]): _row_dict(row) for row in rows}
    missing = [kp_id for kp_id in kp_ids if kp_id not in by_id]
    if missing:
        raise HTTPException(
            422,
            "知识点不存在：%s（请刷新知识点列表后重试）"
            % "、".join(str(value) for value in missing),
        )
    return [
        {
            "id": int(by_id[kp_id]["id"]),
            "name": str(by_id[kp_id].get("name") or ""),
            "subject": str(by_id[kp_id].get("subject") or ""),
        }
        for kp_id in kp_ids
    ]


def _load_artifacts(user_id: int, artifact_ids: list[str]) -> list[dict]:
    """按输入顺序读取本人学习附件的当前固定版本；不存在 / 已删除 / 非本人 → 404。

    只取元数据（标题、kind、版本号），**不读取也不内联 ``data``/``preview`` 内容或 base64**。
    """
    artifacts: list[dict] = []
    for artifact_id in artifact_ids:
        payload = study_artifact.get_artifact(user_id, artifact_id)
        artifact = payload.get("artifact") or {}
        revision = payload.get("revision") or {}
        artifacts.append({
            "artifact_id": str(artifact.get("artifact_id") or artifact_id),
            "title": str(artifact.get("title") or ""),
            "kind": str(artifact.get("kind") or ""),
            "revision": int(revision.get("revision") or artifact.get("current_revision") or 1),
        })
    return artifacts


def _document_titles(user_id: int, document_ids) -> dict[int, str]:
    """注解引用的资料标题（只读本人资料；已删除/他人的资料 title 为 ``None``，由调用方兜底）。"""
    wanted = sorted({int(value) for value in document_ids})
    if not wanted:
        return {}
    marks = ",".join("?" for _ in wanted)
    with db.connection() as con:
        rows = con.execute(
            "SELECT id, title FROM ai_documents WHERE user_id=? AND id IN (%s)" % marks,
            (user_id, *wanted),
        ).fetchall()
    return {int(row["id"]): str(row["title"] or "") for row in rows}


def _load_note_annotations(user_id: int, notes: list[dict]) -> tuple[dict[int, list[dict]], dict[int, str]]:
    """读取每篇笔记的原文摘录（最多 5 条/篇）与相关资料标题。

    复用 :func:`app.doc_location.list_annotations` 的归属与失效校验：真实存在但已失效的摘录照常
    返回并带 ``invalid_reason``，本模块在正文中如实标注「该摘录已失效」而不是丢掉或改写。
    读取异常按「暂无摘录」降级，不影响报告其余部分。
    """
    annotations: dict[int, list[dict]] = {}
    document_ids: set[int] = set()
    for note in notes:
        try:
            payload = doc_location.list_annotations(
                user_id, note_id=note["id"], limit=MAX_ANNOTATIONS_PER_NOTE
            )
        except Exception:  # noqa: BLE001 - 摘录读取失败降级为空，报告其余部分照常
            payload = {}
        items = [item for item in (payload.get("items") or []) if isinstance(item, dict)]
        items = items[:MAX_ANNOTATIONS_PER_NOTE]
        annotations[note["id"]] = items
        for item in items:
            document_id = _page_number(item.get("document_id"))
            if document_id:
                document_ids.add(document_id)
    return annotations, _document_titles(user_id, document_ids)


def _search_questions(topic: str) -> tuple[list[dict], str]:
    """调用题库相似题检索取最多 5 道候选题；题库不可用 / 检索失败时降级为空并给出中文说明。"""
    try:
        results = practice.search_similar_questions(topic, limit=QUESTION_LIMIT)
    except HTTPException:
        return [], QUESTION_UNAVAILABLE_NOTE
    except Exception:  # noqa: BLE001 - 检索失败不拖垮报告，正文如实说明
        return [], QUESTION_UNAVAILABLE_NOTE
    questions: list[dict] = []
    for raw in results or []:
        if not isinstance(raw, dict):
            continue
        qid = str(raw.get("qid") or "").strip()
        if not qid:
            continue
        try:
            ref = question_ref.normalize(qid)
            label = question_ref.source_label(ref)
        except question_ref.QuestionRefError:
            ref = qid
            label = "题库"
        questions.append({
            "qid": qid,
            "ref": ref,
            "source_label": label,
            "stem": _excerpt(raw.get("stem_plain") or "", QUESTION_STEM_CHARS) or "（题干为空）",
        })
        if len(questions) >= QUESTION_LIMIT:
            break
    return questions, ("" if questions else NO_QUESTIONS_NOTE)


def _evidence_stats(user_id: int) -> dict | None:
    """学习证据近 90 天统计（只取 :func:`app.study_evidence.summary` 的真实字段）；失败返回 None。"""
    try:
        payload = study_evidence.summary(user_id, days=EVIDENCE_DAYS)
    except Exception:  # noqa: BLE001 - 证据读取失败降级说明，报告不因此失败
        return None
    by_verdict = payload.get("by_verdict") or {}
    by_kind = payload.get("by_kind") or {}
    by_self_report = payload.get("by_self_report") or {}
    try:
        effective_count = int(payload.get("effective_count") or 0)
        revised_count = int(payload.get("revised_count") or 0)
        missing_context_count = int(payload.get("missing_context_count") or 0)
        window_days = int(payload.get("window_days") or EVIDENCE_DAYS)
    except (TypeError, ValueError):
        return None
    return {
        "effective_count": max(0, effective_count),
        "revised_count": max(0, revised_count),
        "missing_context_count": max(0, missing_context_count),
        "window_days": max(1, window_days),
        "correct": int(by_verdict.get("correct") or 0),
        "incorrect": int(by_verdict.get("incorrect") or 0),
        "pending": int(by_verdict.get("pending") or 0),
        "unknown": int(by_verdict.get("unknown") or 0),
        "self_report_events": int(by_kind.get("self_report") or 0),
        "independent_self_report": int(by_self_report.get("independent") or 0),
    }


# ---------------------------------------------------------------------------
# 规则汇编：关键字命中、分块选择、Markdown 组装
# ---------------------------------------------------------------------------


def _fold_ascii(text) -> str:
    """仅把 ASCII 字母 A–Z 折叠为小写；中文与其他字符保持原样（与资料定位口径一致）。"""
    return "".join(
        chr(ord(character) + 32) if "A" <= character <= "Z" else character
        for character in str(text or "")
    )


def _topic_keywords(topic: str) -> list[str]:
    """由主题派生检索关键字：保留完整主题，并补充空白 / 标点切分后长度 ≥2 的片段。"""
    pieces = re.split(r"[\s,，、;；:：/|·。.!！?？()（）\[\]【】\"'“”]+", topic)
    keywords = [str(topic or "").strip()] + [piece.strip() for piece in pieces if piece.strip()]
    ordered: list[str] = []
    for keyword in keywords:
        if len(keyword) >= 2 and keyword not in ordered:
            ordered.append(keyword)
    return ordered


def _select_chunks(chunks: list[dict], topic: str) -> dict:
    """选择引用分块：优先命中主题关键字的前 2 段，否则取资料开头 2 段（标记回退）。"""
    text_chunks = [chunk for chunk in chunks if chunk["text"].strip()]
    keywords = [_fold_ascii(keyword) for keyword in _topic_keywords(topic)]
    if keywords:
        matched = [
            chunk for chunk in text_chunks
            if any(keyword in _fold_ascii(chunk["text"]) for keyword in keywords)
        ]
    else:  # pragma: no cover - 主题至少 2 字，关键字列表不会为空
        matched = []
    if matched:
        return {"chunks": matched[:2], "matched": True}
    return {"chunks": text_chunks[:2], "matched": False}


def _document_source(doc: dict, chunk: dict | None, excerpt: str) -> dict:
    """资料分块来源条目（同一资料最多 2 条；无分块时只记录资料本身）。"""
    source = {
        "kind": "document",
        "id": doc["id"],
        "title": doc["title"],
        "ref": "ai_documents:%s" % doc["id"],
        "version": doc["version"],
    }
    if chunk is not None:
        source["page"] = chunk["page"]
        source["chunk_id"] = chunk["chunk_id"]
        source["quote"] = excerpt
    return source


def _annotation_source(annotation: dict, title: str, quote: str) -> dict:
    """摘录来源条目：指向具体摘录与所属资料页。"""
    source = {
        "kind": "annotation",
        "id": int(annotation["annotation_id"]),
        "title": title,
        "ref": "doc_annotations:%s" % int(annotation["annotation_id"]),
        "page": _page_number(annotation.get("page")),
        "quote": quote,
    }
    chunk_id = str(annotation.get("chunk_id") or "").strip()
    if chunk_id:
        source["chunk_id"] = chunk_id
    note_id = annotation.get("note_id")
    if note_id is not None:
        source["note"] = "笔记 #%s" % int(note_id)
    return source


def _artifact_source(artifact: dict) -> dict:
    """学习附件来源条目：固定版本号，不内联内容。"""
    return {
        "kind": "artifact",
        "id": artifact["artifact_id"],
        "title": artifact["title"],
        "ref": "study_artifacts:%s" % artifact["artifact_id"],
        "version": int(artifact["revision"]),
        "note": artifact["kind"],
    }


def _question_source(question: dict) -> dict:
    """候选题来源条目：统一题目引用 + 来源标签。"""
    return {
        "kind": "question",
        "id": question["qid"],
        "title": question["stem"],
        "ref": question["ref"],
        "note": question["source_label"],
    }


def _evidence_lines(evidence: dict | None) -> list[str]:
    """学习证据小节正文（只写真实字段；样本不足时明确不推断掌握程度）。"""
    if evidence is None:
        return [
            "- 学习证据：暂不可用（读取失败），本报告不据此推断掌握程度。",
            "",
        ]
    sufficient = evidence["effective_count"] >= EVIDENCE_MIN_EFFECTIVE
    if sufficient:
        sample_line = (
            "- 样本是否充足：样本充足（近 %s 天有效证据 %s 条，报告级门槛 %s 条）；"
            "仍只作事实陈述，不代表掌握结论。"
            % (evidence["window_days"], evidence["effective_count"], EVIDENCE_MIN_EFFECTIVE)
        )
    else:
        sample_line = (
            "- 样本是否充足：样本不足（近 %s 天有效证据只有 %s 条，报告级门槛 %s 条）；"
            "报告不据此推断掌握程度。"
            % (evidence["window_days"], evidence["effective_count"], EVIDENCE_MIN_EFFECTIVE)
        )
    return [
        "- 统计窗口：近 %s 天" % evidence["window_days"],
        "- 有效证据：%s 条；修订前旧版本 %s 条（不计入有效证据）"
        % (evidence["effective_count"], evidence["revised_count"]),
        "- 已判定作答：正确 %s / 错误 %s / 待核验 %s / 未知 %s"
        % (
            evidence["correct"], evidence["incorrect"],
            evidence["pending"], evidence["unknown"],
        ),
        "- 学生自评：%s 条（其中独立自评 %s 条）；学生自评不计入正确率"
        % (evidence["self_report_events"], evidence["independent_self_report"]),
        "- 提示/揭晓信息缺失：%s 条（缺失信息按 unknown 处理，不推断为独立作答）"
        % evidence["missing_context_count"],
        sample_line,
        "",
    ]


def _build_snapshot(
    *,
    topic: str,
    title: str,
    documents: list[dict],
    notes: list[dict],
    kps: list[dict],
    artifacts: list[dict],
    annotations: dict[int, list[dict]],
    document_titles: dict[int, str],
    questions: list[dict],
    question_note: str,
    evidence: dict | None,
    generated_at: str,
) -> dict:
    """按固定规则把真实来源组装成 ``content_md`` 与 ``sources``（同输入同输出）。"""
    sources: list[dict] = []
    lines: list[str] = [
        "# %s" % title,
        "",
        "生成时间：%s（UTC）" % generated_at,
        GENERATOR_NOTE,
        "",
        "## 一、主题与范围",
        "",
        "- 主题：%s" % topic,
        "- 来源数量：资料 %s / 笔记 %s / 知识点 %s / 附件 %s"
        % (len(documents), len(notes), len(kps), len(artifacts)),
    ]
    if kps:
        lines.append(
            "- 关联知识点：%s"
            % "、".join("%s（#%s）" % (kp["name"], kp["id"]) for kp in kps)
        )
    lines += [
        "- 生成时间：%s（UTC）" % generated_at,
        "- 学习证据窗口：近 %s 天" % EVIDENCE_DAYS,
        "",
    ]

    # 二、资料依据：每份资料最多 2 段真实分块，带页码 / chunk / 文档版本。
    lines += ["## 二、资料依据", ""]
    if not documents:
        lines += ["（本报告未选择资料。）", ""]
    for doc in documents:
        lines += ["### 资料 #%s：《%s》" % (doc["id"], doc["title"]), ""]
        if doc["corrupt"]:
            lines += [CORRUPT_CHUNKS_NOTE, ""]
            sources.append(_document_source(doc, None, ""))
            continue
        selection = _select_chunks(doc["chunks"], topic)
        if not selection["chunks"]:
            lines += [EMPTY_CHUNKS_NOTE, ""]
            sources.append(_document_source(doc, None, ""))
            continue
        for chunk in selection["chunks"]:
            excerpt = _excerpt(chunk["text"], CHUNK_EXCERPT_CHARS)
            lines.append("> %s" % excerpt)
            lines.append(
                "（来源：《%s》第 %s 页 · chunk %s · 文档版本 %s）"
                % (doc["title"], chunk["page"], chunk["chunk_id"], doc["version"])
            )
            lines.append("")
            sources.append(_document_source(doc, chunk, excerpt))
        if not selection["matched"]:
            lines += ["（说明）%s。" % TOPIC_FALLBACK_EXCERPT_NOTE, ""]

    # 三、笔记摘录：标题 + 内容前 200 字 + 该笔记的原文摘录（最多 5 条）。
    lines += ["## 三、笔记摘录", ""]
    if not notes:
        lines += ["（本报告未选择笔记。）", ""]
    for note in notes:
        note_excerpt = _excerpt(note["content"], NOTE_EXCERPT_CHARS)
        lines += [
            "### 笔记 #%s · %s" % (note["id"], note["title"]),
            "",
            "> %s" % note_excerpt,
            "（笔记 #%s）" % note["id"],
            "",
        ]
        sources.append({
            "kind": "note",
            "id": note["id"],
            "title": note["title"],
            "ref": "ai_notes:%s" % note["id"],
            "quote": note_excerpt,
        })
        for annotation in annotations.get(note["id"], []):
            quote = _excerpt(annotation.get("quote"), doc_location.MAX_QUOTE_LENGTH)
            if not quote:
                continue
            document_id = _page_number(annotation.get("document_id"))
            doc_title = document_titles.get(document_id) or "资料已删除或无权访问"
            page = _page_number(annotation.get("page"))
            annotation_id = int(annotation.get("annotation_id"))
            lines.append("> %s" % quote)
            lines.append("（来源：《%s》第 %s 页 · 摘录 #%s）" % (doc_title, page, annotation_id))
            if not annotation.get("valid", True) and annotation.get("invalid_reason"):
                lines.append("（该摘录已失效：%s；引用可能不准确。）" % annotation["invalid_reason"])
            lines.append("")
            sources.append(_annotation_source(annotation, doc_title, quote))

    # 四、学习附件：标题 / kind / 固定版本；只引用版本，不内联内容。
    lines += ["## 四、学习附件", ""]
    if not artifacts:
        lines += ["（本报告未选择学习附件。）", ""]
    for artifact in artifacts:
        revision = int(artifact["revision"] or 1)
        lines += [
            "### %s（v%s）" % (artifact["title"], revision),
            "",
            "- 类型：%s" % (artifact["kind"] or "unknown"),
            "- 固定版本：v%s" % revision,
            "- 可在知序中按该版本打开（附件 %s · v%s）；本报告不内联附件内容或 base64。"
            % (artifact["artifact_id"], revision),
            "",
        ]
        sources.append(_artifact_source(artifact))

    # 五、相关练习：题库相似题检索结果（非正式知识点标签）。
    lines += ["## 五、相关练习", ""]
    if questions:
        for question in questions:
            lines.append(
                "- %s（引用：%s · %s）"
                % (question["stem"], question["ref"], question["source_label"])
            )
            sources.append(_question_source(question))
        lines.append("")
    else:
        lines += [question_note or NO_QUESTIONS_NOTE, ""]
    lines += [QUESTION_CALIBER_NOTE, ""]

    # 六、学习证据：只写 study_evidence.summary 的真实字段并注明样本是否充足。
    lines += ["## 六、学习证据", ""]
    lines += _evidence_lines(evidence)

    # 七、口径与局限。
    lines += [
        "## 七、口径与局限",
        "",
        "- 本报告为**规则汇编，非 AI 生成**：只检索已有资料片段、笔记摘录、学习附件与题库候选题，"
        "不调用 AI 模型、不生成新内容（generator=%s）。" % GENERATOR,
        "- 引用可能失效：资料更新后文档版本（内容摘要）变化、摘录被删除或资料移出本人账号时，"
        "对应引用需重新核对或重新生成报告。",
        "- 资料片段仅按分块原文摘录（最多 200 字），不改写、不补写，也不内联附件二进制或 base64 内容。",
        "- 候选题为题库检索结果，非正式知识点标签；学习证据只统计真实字段，样本不足时不推断掌握程度。",
        "",
    ]
    return {"content_md": "\n".join(lines).strip() + "\n", "sources": sources}


# ---------------------------------------------------------------------------
# 行投影与读取
# ---------------------------------------------------------------------------


def _report_public(row) -> dict:
    """报告摘要（列表与创建 / 读取 / 更新共用同一口径，不暴露 user_id 与正文）。"""
    return {
        "report_id": int(row["id"]),
        "topic": str(row["topic"] or ""),
        "title": str(row["title"] or ""),
        "generator": str(row["generator"] or GENERATOR),
        "status": str(row["status"] or STATUS_READY),
        "version": int(row["version"] or 1),
        "created_at": str(row["created_at"] or ""),
        "updated_at": str(row["updated_at"] or ""),
    }


def _load_report(con, user_id: int, report_id: int):
    """读取本人报告行；不存在或属于他人 → 同一个 404（不泄露存在性）。"""
    row = con.execute(
        "SELECT * FROM topic_reports WHERE id=? AND user_id=?",
        (int(report_id), int(user_id)),
    ).fetchone()
    if row is None:
        raise HTTPException(404, "专题报告不存在或无权访问")
    return row


def _detail(row) -> dict:
    """详情投影：``{report, content_md, sources}``（创建 / 读取 / 更新 / 复读共用）。"""
    return {
        "report": _report_public(row),
        "content_md": str(row["content_md"] or ""),
        "sources": _parse_sources(row["sources_json"]),
    }


def _count_reports(con, user_id: int) -> int:
    return int(
        con.execute(
            "SELECT COUNT(*) FROM topic_reports WHERE user_id=?", (int(user_id),)
        ).fetchone()[0]
    )


# ---------------------------------------------------------------------------
# 公开函数：创建 / 列表 / 详情 / 更新 / 删除 / Markdown
# ---------------------------------------------------------------------------


def _limit_detail() -> str:
    """报告数量上限的中文 detail（按当前常量动态生成，便于测试调小上限）。"""
    return LIMIT_DETAIL_TEMPLATE % MAX_REPORTS_PER_USER


def create_report(
    user_id,
    *,
    topic,
    document_ids=None,
    note_ids=None,
    kp_ids=None,
    artifact_ids=None,
    title=None,
    con=None,
) -> dict:
    """按规则汇编生成一份带引文的专题报告并保存（**非 AI 生成**）。

    参数：
        user_id: 当前用户 id。
        topic: 主题（2–200 字，必填）。
        document_ids: 本人资料 id 列表（≤10）。
        note_ids: 本人笔记 id 列表（≤10）。
        kp_ids: 知识点 id 列表（≤10，必须真实存在，仅用于范围说明与计数）。
        artifact_ids: 学习附件公开 ``artifact_id`` 列表（≤10，固定引用当前版本）。
        title: 可选标题（≤120 字）；缺省「专题报告 · <topic>」。
        con: 可选连接；传入时只在当前连接执行最终写入，由调用方负责提交。

    四类来源（资料 / 笔记 / 知识点 / 学习附件）至少一个非空，否则 422；资料 / 笔记 / 附件不存在
    或非本人 → 404，知识点不存在 → 422；个人报告数达 :data:`MAX_REPORTS_PER_USER` → 422。
    返回 ``{report, content_md, sources}``。
    """
    _ensure_ready()
    owner = _validate_user_id(user_id)
    topic_value = _validate_topic(topic)
    title_value = _validate_title(title, topic=topic_value)
    document_values = _normalize_id_list(document_ids, "资料")
    note_values = _normalize_id_list(note_ids, "笔记")
    kp_values = _normalize_id_list(kp_ids, "知识点")
    artifact_values = _normalize_artifact_ids(artifact_ids)
    if not (document_values or note_values or kp_values or artifact_values):
        raise HTTPException(422, NO_SOURCES_DETAIL)

    # 上限早检查：报告已满时不做检索与汇编，避免无谓的题库检索成本。
    with _read_connection(None) as read_con:
        if _count_reports(read_con, owner) >= MAX_REPORTS_PER_USER:
            raise HTTPException(422, _limit_detail())

    documents = _load_documents(owner, document_values)
    notes = _load_notes(owner, note_values)
    kps = _load_knowledge_points(kp_values)
    artifacts = _load_artifacts(owner, artifact_values)
    annotations, document_titles = _load_note_annotations(owner, notes)
    questions, question_note = _search_questions(topic_value)
    evidence = _evidence_stats(owner)
    generated_at = db.utcnow()
    snapshot = _build_snapshot(
        topic=topic_value,
        title=title_value,
        documents=documents,
        notes=notes,
        kps=kps,
        artifacts=artifacts,
        annotations=annotations,
        document_titles=document_titles,
        questions=questions,
        question_note=question_note,
        evidence=evidence,
        generated_at=generated_at,
    )
    sources_json = _dump_sources(snapshot["sources"])

    with _write_connection(con) as write_con:
        # 写锁内复查上限（并发创建同账号报告时不能突破上限）。
        if _count_reports(write_con, owner) >= MAX_REPORTS_PER_USER:
            raise HTTPException(422, _limit_detail())
        cursor = write_con.execute(
            "INSERT INTO topic_reports("
            "user_id, topic, title, generator, content_md, sources_json, status, version, "
            "created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                owner, topic_value, title_value, GENERATOR, snapshot["content_md"],
                sources_json, STATUS_READY, 1, generated_at, generated_at,
            ),
        )
        report_id = cursor.lastrowid
        if report_id is None:  # pragma: no cover - 双引擎都应返回 id
            raise RuntimeError("专题报告写入失败：未取得新报告编号")
        row = _load_report(write_con, owner, int(report_id))
    return _detail(row)


def list_reports(user_id, *, limit: int = DEFAULT_LIMIT, con=None) -> dict:
    """列出本人专题报告（最近更新在前）。

    返回 ``{items, total, limit, note}``；``total`` 不受 ``limit`` 截断；``limit`` 非法值退回默认、
    超界裁剪到 1..100。摘要项含 ``report_id/topic/title/generator/status/version/时间``。
    """
    _ensure_ready()
    owner = _validate_user_id(user_id)
    size = _normalize_limit(limit)
    with _read_connection(con) as read_con:
        total = _count_reports(read_con, owner)
        rows = read_con.execute(
            "SELECT * FROM topic_reports WHERE user_id=? "
            "ORDER BY updated_at DESC, id DESC LIMIT ?",
            (owner, size),
        ).fetchall()
    return {
        "items": [_report_public(row) for row in rows],
        "total": total,
        "limit": size,
        "note": LIST_NOTE,
    }


def get_report(user_id, report_id, con=None) -> dict:
    """读取单份报告详情（``{report, content_md, sources}``）；不存在或非本人 → 404。"""
    _ensure_ready()
    owner = _validate_user_id(user_id)
    report_value = _validate_id(report_id, "报告")
    with _read_connection(con) as read_con:
        row = _load_report(read_con, owner, report_value)
    return _detail(row)


def update_report(
    user_id,
    report_id,
    *,
    title=None,
    content_md=None,
    expected_version=None,
    con=None,
) -> dict:
    """更新报告标题 / 正文（CAS 乐观锁）：``version`` +1、``updated_at`` 刷新。

    * 至少提供 ``title`` 或 ``content_md`` 之一，否则 422；
    * ``title`` ≤120 字、``content_md`` ≤200 KB（UTF-8）且非空，否则 422；
    * ``expected_version`` 与当前版本不一致（含已过期的 ``0``）→ 409，不写入任何内容；
    * 报告不存在或非本人 → 404。
    返回更新后的 ``{report, content_md, sources}``（``sources`` 保持生成时快照）。
    """
    _ensure_ready()
    owner = _validate_user_id(user_id)
    report_value = _validate_id(report_id, "报告")
    if title is None and content_md is None:
        raise HTTPException(422, "请至少提供标题 title 或正文 content_md 之一")
    title_value = None if title is None else _validate_title(title)
    content_value = None if content_md is None else _validate_content_md(content_md)
    expected = (
        None if expected_version is None else _validate_expected_version(expected_version)
    )
    with _write_connection(con) as write_con:
        row = _load_report(write_con, owner, report_value)
        current_version = int(row["version"] or 1)
        if expected is not None and expected != current_version:
            raise HTTPException(
                409,
                "专题报告已在其他位置更新（当前版本 v%s，提交版本 v%s），请刷新后重试"
                % (current_version, expected),
            )
        now = db.utcnow()
        write_con.execute(
            "UPDATE topic_reports SET title=?, content_md=?, version=?, updated_at=? "
            "WHERE id=? AND user_id=?",
            (
                row["title"] if title_value is None else title_value,
                row["content_md"] if content_value is None else content_value,
                current_version + 1,
                now,
                report_value,
                owner,
            ),
        )
        fresh = _load_report(write_con, owner, report_value)
    return _detail(fresh)


def delete_report(user_id, report_id, con=None) -> dict:
    """删除本人报告；不存在或非本人 → 404。返回 ``{deleted: true, report_id}``。"""
    _ensure_ready()
    owner = _validate_user_id(user_id)
    report_value = _validate_id(report_id, "报告")
    with _write_connection(con) as write_con:
        _load_report(write_con, owner, report_value)
        write_con.execute(
            "DELETE FROM topic_reports WHERE id=? AND user_id=?",
            (report_value, owner),
        )
    return {"deleted": True, "report_id": report_value}


_FILENAME_FORBIDDEN = re.compile(r'[\\/:*?"<>|\x00-\x1f\x7f]+')


def _safe_filename_part(text, limit: int = 40) -> str:
    """清洗文件名片段：替换路径分隔符与非法字符、折叠空白、截断，保证不能逃出目录。"""
    value = _FILENAME_FORBIDDEN.sub("_", str(text or ""))
    value = re.sub(r"\s+", " ", value).strip(" .")
    value = "".join(character for character in value if character not in "\\/:*?\"<>|")
    if len(value) > limit:
        value = value[:limit].rstrip(" .")
    return value or "未命名专题"


def _markdown_filename(report: dict) -> str:
    """下载文件名：``专题报告-<safe topic>-v<version>.md``（中文安全、无路径分隔符）。"""
    topic_part = _safe_filename_part(report.get("topic"), 40)
    version = max(1, int(report.get("version") or 1))
    return "专题报告-%s-v%s.md" % (topic_part, version)


def report_markdown(user_id, report_id, con=None) -> tuple[str, str]:
    """导出报告 Markdown：返回 ``(filename, markdown_text)``，内容与详情接口的 ``content_md`` 一致。

    文件名形如 ``专题报告-<safe topic>-v<version>.md``（非法文件名字符替换为 ``_``，中文保留）；
    报告不存在或非本人 → 404。
    """
    detail = get_report(user_id, report_id, con=con)
    return _markdown_filename(detail["report"]), detail["content_md"]


# ---------------------------------------------------------------------------
# HTTP 请求模型
# ---------------------------------------------------------------------------


class CreateReportInput(BaseModel):
    """``POST /api/topic-reports`` 请求体：主题必填，四类来源至少一个非空。"""

    topic: str
    title: str | None = None
    document_ids: list[int] | None = None
    note_ids: list[int] | None = None
    kp_ids: list[int] | None = None
    artifact_ids: list[str] | None = None


class UpdateReportInput(BaseModel):
    """``PATCH /api/topic-reports/{report_id}`` 请求体：标题 / 正文至少一个。"""

    title: str | None = None
    content_md: str | None = None
    expected_version: int | None = None


# ---------------------------------------------------------------------------
# HTTP 路由（全部 Depends(current_user)；静态路径在前，动态路径在后）
# ---------------------------------------------------------------------------


@router.post("", status_code=201)
def create_report_route(data: CreateReportInput, user=Depends(current_user)):
    """按规则汇编创建带引文的专题报告（非 AI 生成）；四类来源至少一个非空。"""
    return create_report(
        user["id"],
        topic=data.topic,
        document_ids=data.document_ids,
        note_ids=data.note_ids,
        kp_ids=data.kp_ids,
        artifact_ids=data.artifact_ids,
        title=data.title,
    )


@router.get("")
def list_reports_route(
    limit: int = Query(default=DEFAULT_LIMIT),
    user=Depends(current_user),
):
    """本人专题报告列表（最近更新在前，默认 20 条）。"""
    return list_reports(user["id"], limit=limit)


@router.get("/{report_id}/markdown")
def report_markdown_route(report_id: int, user=Depends(current_user)):
    """下载报告 Markdown（``text/markdown``），中文文件名按 RFC 5987 编码。"""
    from urllib.parse import quote

    filename, markdown = report_markdown(user["id"], report_id)
    match = re.search(r"-v(\d+)\.md$", filename)
    ascii_fallback = "topic-report-v%s.md" % (match.group(1) if match else "1")
    headers = {
        "Content-Disposition": (
            'attachment; filename="%s"; filename*=UTF-8\'\'%s'
            % (ascii_fallback, quote(filename, safe=""))
        ),
        "Cache-Control": "no-store",
    }
    return Response(
        content=markdown,
        media_type="text/markdown; charset=utf-8",
        headers=headers,
    )


@router.get("/{report_id}")
def get_report_route(report_id: int, user=Depends(current_user)):
    """单份报告详情：``{report, content_md, sources}``；他人报告 404。"""
    return get_report(user["id"], report_id)


@router.patch("/{report_id}")
def update_report_route(
    report_id: int,
    data: UpdateReportInput,
    user=Depends(current_user),
):
    """更新报告标题 / 正文（``expected_version`` 做 CAS，冲突 409）。"""
    return update_report(
        user["id"],
        report_id,
        title=data.title,
        content_md=data.content_md,
        expected_version=data.expected_version,
    )


@router.delete("/{report_id}")
def delete_report_route(report_id: int, user=Depends(current_user)):
    """删除本人报告；不存在或非本人 404。"""
    return delete_report(user["id"], report_id)


__all__ = [
    "CALIBER",
    "CHUNK_EXCERPT_CHARS",
    "DEFAULT_LIMIT",
    "EVIDENCE_DAYS",
    "EVIDENCE_MIN_EFFECTIVE",
    "GENERATOR",
    "GENERATOR_LABEL",
    "GENERATOR_NOTE",
    "LIST_NOTE",
    "MAX_ANNOTATIONS_PER_NOTE",
    "MAX_CONTENT_BYTES",
    "MAX_LIMIT",
    "MAX_REPORTS_PER_USER",
    "MAX_SOURCES_PER_KIND",
    "MAX_TITLE_LENGTH",
    "NO_SOURCES_DETAIL",
    "QUESTION_CALIBER_NOTE",
    "QUESTION_LIMIT",
    "TOPIC_FALLBACK_EXCERPT_NOTE",
    "TOPIC_MAX_LENGTH",
    "TOPIC_MIN_LENGTH",
    "create_report",
    "delete_report",
    "get_report",
    "init_topic_reports",
    "list_reports",
    "report_markdown",
    "router",
    "update_report",
]
