"""互动讲义（handout）模块：结构化小节 + 固定版本引用 + 单文件 HTML / PDF 导出。

定位
----
本模块实现整合方案 §4「学习附件」、§5.3「后续功能」、§5.4「导出」与 §7 M6 中的
**互动讲义最小版**：把一门课程的大纲场景、若干笔记的原文摘录、若干学习附件（按创建时
**固定版本**）、若干知识点对应的练习候选题，以及近 90 天学习证据，按**固定规则**汇总成
可保存、可列表、可删除、可导出的结构化小节（``sections``）。全流程不调用任何 AI 模型，
**规则汇编，非 AI 生成**；来源数据不可用时如实留空或注明，绝不虚构内容。

小节口径（固定顺序，缺省来源的节不出现）
----------------------------------------
1. ``course_outline``：课程标题 + ``cls_scenes`` 场景（按 ``order_index``，标题、``scene_type``
   中文名、``meta.actions`` 中 speech 摘要前 120 字，最多 20 项）。
2. ``artifact``：每份学习附件一个 item，写明 kind 中文名、固定版本 ``vN`` 与来源说明；
   ``preview.png`` 是合法 ``data:image/png;base64,`` 时内嵌为 ``preview_data_uri``，单份讲义
   累计不超过 300KB，超出按「预览过大已省略」注明。
3. ``excerpt``：对每个笔记调用 :func:`app.doc_location.list_annotations`，最多 10 条
   ``quotes``（``text``=原文摘录，``source``=``《文档标题》第 N 页 · 摘录 #id``）；无摘录时
   只写说明，不虚构。
4. ``practice``：用知识点名称调用 :func:`app.practice.search_similar_questions` 检索候选题，
   item 为 ``{title: 题干前 100 字, ref: qid, detail: 来源：…}``，并注明候选题不是正式标签。
5. ``evidence``：:func:`app.study_evidence.summary` 的关键真实字段（有效事件、独立且核验的
   作答数、样本是否充足）写成段落；无证据时显示「样本不足」，不推断掌握程度。
6. ``meta``：「规则汇编，非 AI 生成」+ 引用局限 + 固定版本说明 + generator。

错误口径（detail 全部中文）
---------------------------
* 404：讲义、课程、笔记、附件不存在或不属于当前用户（不泄露他人资源是否存在）；
* 422：空来源、参数非法、笔记/附件/知识点超过 10 个、个人讲义超过 50 份、知识点不存在；
* 503：PDF 渲染依赖不可用（Playwright 缺失/无浏览器），引导改用 HTML 导出或浏览器打印。

存储与导出
----------
* 只通过 :mod:`app.db` 的 ``connection()`` 访问应用库（不缓存 ``DATA_DIR``），时间用
  ``db.utcnow()``，JSON 列存 TEXT，SQL 占位符统一 ``?``，不使用 SQLite 专有语法，SQLite 与
  PostgreSQL 行为一致；所有写路径先取 ``db.write_lock(con, "handout")`` 串行化。
* ``handouts`` 表已登记 ``app/db.py::_ID_TABLES``（PostgreSQL 下 INSERT 自动 ``RETURNING id``）。
* 导出读取的是**创建时落库的小节快照**，不会回读课程/附件最新版本，因此源数据之后升版或
  删除不会改动已保存的讲义；导出 HTML 为自包含单文件（无外链、无 ``<script>``，所有用户
  内容 ``html.escape``，图片仅允许 PNG data URI）。

接线方式：应用启动时调用 :func:`init_handouts`，并把 :data:`router` 挂到主应用
（``app.include_router(handout.router)``）。
"""
from __future__ import annotations

from contextlib import contextmanager
import html as html_lib
import json
import re
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel

from . import db, doc_location, practice, study_artifact, study_evidence
from .auth import current_user
from .classroom import db as classroom_db
from .classroom import schema as classroom_schema

router = APIRouter(prefix="/api/handouts", tags=["Interactive handouts"])

#: 生成方式：规则汇编（本模块不调用 AI 模型）。
GENERATOR = "rules"
#: 小节固定顺序（缺省来源的节不出现，但相对顺序不变）。
SECTION_ORDER = ("course_outline", "artifact", "excerpt", "practice", "evidence", "meta")

#: 单类来源最多可选数量；个人讲义最多保存数量。
MAX_SOURCES_PER_KIND = 10
MAX_HANDOUTS_PER_USER = 50
MAX_TITLE_LENGTH = 120
#: 单节数量上限（真实数据截断，并在段落中注明被省略的数量）。
MAX_SCENES_PER_SECTION = 20
MAX_ARTIFACTS_PER_SECTION = 6
MAX_QUOTES_PER_HANDOUT = 10
MAX_PRACTICE_ITEMS = 10
#: 每个知识点向题库检索的候选条数（与 ``practice.search_similar_questions`` 默认值一致）。
MAX_PRACTICE_PER_KEYWORD = 5
#: 单份讲义内嵌 PNG 预览累计上限（超出后省略并注明）。
MAX_PREVIEW_TOTAL_BYTES = 300 * 1024
#: 学习证据统计窗口（天）。
EVIDENCE_DAYS = 90
#: 列表默认 / 最大返回条数。
DEFAULT_LIST_LIMIT = 20
MAX_LIST_LIMIT = 100
#: 课程场景旁白摘要、候选题题干摘要的最大字符数。
SPEECH_PREVIEW_CHARS = 120
QUESTION_TITLE_CHARS = 100

#: 只允许内嵌合法 PNG data URI（正则校验后再插入 ``src``，其余一律省略）。
PNG_DATA_URI_PREFIX = "data:image/png;base64,"
_PNG_DATA_URI_RE = re.compile(r"^data:image/png;base64,[A-Za-z0-9+/]+={0,2}$")

#: 学习附件类型中文名（与 ``app.classroom.schema.DEMO_ARTIFACT_KIND_LABELS`` 同口径并补齐
#: 讲义可引用的全部类型；未知类型回退原值）。
ARTIFACT_KIND_LABELS = {
    "function_graph": "函数图",
    "whiteboard": "解题白板",
    "reading_excerpt": "阅读摘录",
    "flow_animation": "流程动画",
    "isometric": "等距结构",
    "science_demo": "科学演示",
    "demo_3d": "3D 演示",
    "mech_3d": "机械 3D",
    "factory_sim": "工厂仿真",
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS handouts(
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL,
    title TEXT NOT NULL,
    course_id INTEGER,
    note_ids_json TEXT NOT NULL,
    artifact_ids_json TEXT NOT NULL,
    question_refs_json TEXT NOT NULL,
    sections_json TEXT NOT NULL,
    generator TEXT NOT NULL DEFAULT 'rules',
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_handouts_user ON handouts(user_id, updated_at);
"""

#: 进程内就绪标记（数据目录 + 存储引擎）：直接调用本模块函数（未经 lifespan）时懒建表。
_ready_key: tuple[str, bool] | None = None


# ---------------------------------------------------------------------------
# 建表 / 初始化
# ---------------------------------------------------------------------------


def _schema_key() -> tuple[str, bool]:
    return (str(db.DATA_DIR), db.postgres_enabled())


def init_handouts() -> None:
    """应用启动时调用：幂等创建 ``handouts`` 表与索引（SQLite / PostgreSQL 同一套 DDL）。"""
    global _ready_key
    with db.connection() as con:
        db.write_lock(con, "handout")
        con.executescript(_SCHEMA)
    _ready_key = _schema_key()


def _ensure_ready() -> None:
    """懒初始化：未经 lifespan 直接调用本模块函数时也保证表存在（建表仍只在本模块 init 完成）。"""
    if _ready_key != _schema_key():
        init_handouts()


# ---------------------------------------------------------------------------
# 连接与写锁
# ---------------------------------------------------------------------------


def _lock_write(con) -> None:
    """在写事务内取跨进程命名写锁；外层事务已持锁时（SQLite 不可嵌套 BEGIN IMMEDIATE）复用。"""
    if getattr(con, "in_transaction", False):
        return
    db.write_lock(con, "handout")


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
    """取得读连接：``con`` 为空时自建只读连接，否则复用调用方连接。"""
    if con is not None:
        yield con
        return
    with db.connection() as own:
        yield own


# ---------------------------------------------------------------------------
# 校验与序列化
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


def _validate_id_value(value, label: str) -> int:
    """校验课程 / 讲义 / 知识点等整数标识（>=1）；非法抛 422 中文提示。"""
    if isinstance(value, bool):
        raise HTTPException(422, f"{label}编号不正确")
    if isinstance(value, float):
        if not value.is_integer():
            raise HTTPException(422, f"{label}编号不正确")
        value = int(value)
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise HTTPException(422, f"{label}编号不正确") from None
    if number < 1:
        raise HTTPException(422, f"{label}编号不正确")
    return number


def _validate_title(title) -> str:
    if not isinstance(title, str):
        raise HTTPException(422, "标题不正确")
    value = title.strip()
    if not value:
        raise HTTPException(422, "标题不能为空")
    if len(value) > MAX_TITLE_LENGTH:
        raise HTTPException(422, f"标题最多 {MAX_TITLE_LENGTH} 个字符")
    return value


def _validate_int_list(value, label: str) -> list[int]:
    """校验笔记 / 知识点编号数组：去重保序、每项 >=1、最多 10 个（422 中文）。"""
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        raise HTTPException(422, f"{label}参数必须是数组")
    items: list[int] = []
    seen: set[int] = set()
    for entry in value:
        number = _validate_id_value(entry, label)
        if number not in seen:
            seen.add(number)
            items.append(number)
    if len(items) > MAX_SOURCES_PER_KIND:
        raise HTTPException(422, f"{label}最多选择 {MAX_SOURCES_PER_KIND} 个")
    return items


def _validate_artifact_id_list(value) -> list[str]:
    """校验学习附件 ID 数组（公开字符串 ID）：去重保序、非空、最多 10 个（422 中文）。"""
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        raise HTTPException(422, "附件参数必须是数组")
    items: list[str] = []
    seen: set[str] = set()
    for entry in value:
        if not isinstance(entry, str):
            raise HTTPException(422, "附件编号不正确")
        text = entry.strip()
        if not text:
            raise HTTPException(422, "附件编号不正确")
        if text not in seen:
            seen.add(text)
            items.append(text)
    if len(items) > MAX_SOURCES_PER_KIND:
        raise HTTPException(422, f"附件最多选择 {MAX_SOURCES_PER_KIND} 个")
    return items


def _validate_limit(limit) -> int:
    if isinstance(limit, bool):
        raise HTTPException(422, f"每页数量需在 1–{MAX_LIST_LIMIT} 之间")
    try:
        value = int(limit)
    except (TypeError, ValueError):
        raise HTTPException(422, f"每页数量需在 1–{MAX_LIST_LIMIT} 之间") from None
    if value < 1 or value > MAX_LIST_LIMIT:
        raise HTTPException(422, f"每页数量需在 1–{MAX_LIST_LIMIT} 之间")
    return value


def _dump_json(value) -> str:
    """稳定序列化：键排序、紧凑分隔符、不转义中文（落库与回读同一口径）。"""
    try:
        return json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
    except (TypeError, ValueError):
        raise HTTPException(422, "讲义内容包含无法序列化的数据") from None


def _parse_json(text, default):
    if text is None or text == "":
        return default
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return default


def _collapse(text) -> str:
    """折叠空白，单行展示。"""
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _clip(text, limit: int) -> str:
    """折叠空白后按字符数截断（“前 N 字”口径，不额外添加省略号）。"""
    return _collapse(text)[:limit]


# ---------------------------------------------------------------------------
# 数据读取（只读其它模块的表）
# ---------------------------------------------------------------------------


def _row_dict(row) -> dict:
    """把 sqlite3.Row / PostgreSQL 伪行统一转成 dict。"""
    if isinstance(row, dict):
        return dict(row)
    return {key: row[key] for key in row.keys()}


def _load_notes(con, user_id: int, note_ids: list[int]) -> list[dict]:
    """按传入顺序加载本人笔记标题；缺失或不属于本人一律 404「笔记不存在或无权访问」。"""
    if not note_ids:
        return []
    placeholders = ",".join("?" for _ in note_ids)
    rows = con.execute(
        f"SELECT id, title FROM ai_notes WHERE user_id=? AND id IN ({placeholders})",
        (user_id, *note_ids),
    ).fetchall()
    by_id = {int(_row_dict(row)["id"]): _row_dict(row) for row in rows}
    if len(by_id) != len(note_ids):
        raise HTTPException(404, "笔记不存在或无权访问")
    return [by_id[note_id] for note_id in note_ids]


def _load_knowledge_points(con, kp_ids: list[int]) -> list[dict]:
    """按传入顺序加载知识点；不存在（或题库结构不可用）一律 422。"""
    if not kp_ids:
        return []
    placeholders = ",".join("?" for _ in kp_ids)
    try:
        rows = con.execute(
            f"SELECT id, subject, name FROM knowledge_points WHERE id IN ({placeholders})",
            tuple(kp_ids),
        ).fetchall()
    except Exception:  # noqa: BLE001 - 知识点表缺失/结构异常按参数不可用处理
        raise HTTPException(422, "知识点不存在或不可用") from None
    by_id = {int(_row_dict(row)["id"]): _row_dict(row) for row in rows}
    if len(by_id) != len(kp_ids):
        raise HTTPException(422, "知识点不存在或不可用")
    return [by_id[kp_id] for kp_id in kp_ids]


def _document_titles(con, user_id: int, document_ids: set[int]) -> dict[int, str]:
    """读取资料标题（``ai_documents`` 只读）；已删除/无权访问的资料不返回。"""
    if not document_ids:
        return {}
    ids = sorted(document_ids)
    placeholders = ",".join("?" for _ in ids)
    try:
        rows = con.execute(
            f"SELECT id, title FROM ai_documents WHERE user_id=? AND id IN ({placeholders})",
            (user_id, *ids),
        ).fetchall()
    except Exception:  # noqa: BLE001 - 资料表缺失时按「未知资料」降级，不阻断讲义生成
        return {}
    return {int(_row_dict(row)["id"]): str(_row_dict(row).get("title") or "") for row in rows}


def _scene_speech(scene: dict) -> str:
    """取场景 ``meta.actions`` 里的 speech 旁白并摘要前 120 字（缺失返回空串）。"""
    meta = _parse_json(scene.get("meta_json"), {})
    actions = meta.get("actions") if isinstance(meta, dict) else None
    speech = ""
    if isinstance(actions, list):
        speech = classroom_schema.speech_text_of(
            [action for action in actions if isinstance(action, dict)]
        )
    if not speech:
        content = _parse_json(scene.get("content_json"), {})
        if isinstance(content, dict) and isinstance(content.get("speech"), str):
            speech = content["speech"]
    return _clip(speech, SPEECH_PREVIEW_CHARS)


def _question_source_label(item: dict) -> str:
    """候选题来源展示文案：学科 · 年份 · 地区/卷名（缺字段时如实省略）。"""
    parts: list[str] = []
    subject = _collapse(item.get("subject"))
    if subject:
        parts.append(subject)
    year = item.get("year")
    if year not in (None, ""):
        parts.append(f"{year} 年")
    province = _collapse(item.get("province"))
    if province:
        parts.append(province)
    paper_name = _collapse(item.get("paper_name"))
    if paper_name:
        parts.append(paper_name)
    return " · ".join(parts) if parts else "题库候选题"


# ---------------------------------------------------------------------------
# 小节构造（全部来自真实行，失败时如实降级）
# ---------------------------------------------------------------------------


def _course_outline_section(course: dict, scenes: list[dict]) -> dict:
    """小节 1：课程标题 + 场景列表（标题 / 中文类型 / speech 摘要，最多 20 项）。"""
    scene_items: list[dict] = []
    for scene in scenes[:MAX_SCENES_PER_SECTION]:
        row = _row_dict(scene)
        scene_type = str(row.get("scene_type") or "")
        type_label = classroom_schema.SCENE_TYPE_LABELS.get(scene_type, scene_type or "场景")
        speech = _scene_speech(row)
        detail = type_label + (f" · 旁白：{speech}" if speech else "")
        scene_items.append(
            {
                "title": _collapse(row.get("title")) or "未命名场景",
                "detail": detail,
                "ref": f"scene:{int(row.get('id') or 0)}",
            }
        )
    paragraphs = [
        f"课程：{_collapse(course.get('title')) or '未命名课程'}",
        f"场景数：{len(scenes)} 个。",
    ]
    if len(scenes) > MAX_SCENES_PER_SECTION:
        paragraphs.append(
            f"场景较多，本节仅列前 {MAX_SCENES_PER_SECTION} 个（共 {len(scenes)} 个）。"
        )
    if not scenes:
        paragraphs.append("该课程暂无场景。")
    return {
        "id": "course_outline",
        "kind": "course_outline",
        "title": "课程大纲",
        "paragraphs": paragraphs,
        "quotes": [],
        "items": scene_items,
    }


def _artifact_section(con, user_id: int, artifact_ids: list[str]) -> dict:
    """小节 2：学习附件（固定版本 + 来源说明 + 受限额约束的 PNG 预览）。"""
    paragraphs: list[str] = []
    items: list[dict] = []
    omitted_notes: list[str] = []
    used_bytes = 0
    for index, artifact_id in enumerate(artifact_ids):
        if index >= MAX_ARTIFACTS_PER_SECTION:
            paragraphs.append(
                f"附件较多，本节仅列前 {MAX_ARTIFACTS_PER_SECTION} 份"
                f"（共 {len(artifact_ids)} 份）。"
            )
            break
        bundle = study_artifact.get_artifact(user_id, artifact_id, con=con)
        artifact = bundle["artifact"]
        revision = bundle["revision"]
        fixed_revision = int(revision["revision"])
        kind = str(artifact.get("kind") or "")
        kind_label = ARTIFACT_KIND_LABELS.get(kind, kind or "学习附件")
        source_ref = _collapse(artifact.get("source_ref")) or "学习附件库"
        title = _collapse(artifact.get("title")) or "未命名附件"
        item = {
            "title": title,
            "detail": f"{kind_label} · 固定版本 v{fixed_revision} · 来源：{source_ref}",
            "ref": f"artifact:{artifact.get('artifact_id')}@{fixed_revision}",
        }
        preview = revision.get("preview")
        png = preview.get("png") if isinstance(preview, dict) else None
        if isinstance(png, str) and png:
            if not _PNG_DATA_URI_RE.match(png):
                omitted_notes.append(f"《{title}》预览格式无法内嵌，已省略")
            else:
                size = len(png.encode("utf-8"))
                if used_bytes + size <= MAX_PREVIEW_TOTAL_BYTES:
                    used_bytes += size
                    item["preview_data_uri"] = png
                else:
                    omitted_notes.append(f"《{title}》预览过大已省略")
        items.append(item)
    paragraphs.insert(
        0, f"共 {len(items)} 份学习附件，均按创建时固定版本引用（源附件升版不改变本讲义）。"
    )
    paragraphs.extend(omitted_notes)
    if omitted_notes:
        paragraphs.append(
            f"单份讲义内嵌预览累计上限 {MAX_PREVIEW_TOTAL_BYTES // 1024}KB，"
            "被省略的预览仍可在学习附件页查看原始版本。"
        )
    return {
        "id": "artifact",
        "kind": "artifact",
        "title": "学习附件",
        "paragraphs": paragraphs,
        "quotes": [],
        "items": items,
    }


def _excerpt_section(con, user_id: int, notes: list[dict]) -> dict:
    """小节 3：笔记原文摘录（最多 10 条 quotes，source 带文档标题与页码）。"""
    paragraphs: list[str] = []
    if notes:
        note_titles = "、".join(f"《{_collapse(note.get('title')) or '未命名笔记'}》" for note in notes)
        paragraphs.append(f"所选笔记：{note_titles}")
    annotations: list[dict] = []
    for note in notes:
        note_id = int(note["id"])
        result = doc_location.list_annotations(
            user_id,
            note_id=note_id,
            limit=MAX_QUOTES_PER_HANDOUT,
            con=con,
        )
        for item in result.get("items") or []:
            if isinstance(item, dict):
                annotations.append(item)
    document_ids = {
        int(item.get("document_id"))
        for item in annotations
        if item.get("document_id") is not None
    }
    titles = _document_titles(con, user_id, document_ids)
    picked = annotations[:MAX_QUOTES_PER_HANDOUT]
    quotes: list[dict] = []
    invalid_count = 0
    for annotation in picked:
        document_id = annotation.get("document_id")
        title = titles.get(int(document_id)) if document_id is not None else ""
        title = _collapse(title) or "未知资料"
        page = annotation.get("page") or 0
        annotation_id = annotation.get("annotation_id") or 0
        quotes.append(
            {
                "text": str(annotation.get("quote") or ""),
                "source": f"《{title}》第 {page} 页 · 摘录 #{annotation_id}",
            }
        )
        if not annotation.get("valid"):
            invalid_count += 1
    if len(annotations) > len(picked):
        paragraphs.append(
            f"按笔记与时间顺序仅引用前 {MAX_QUOTES_PER_HANDOUT} 条摘录"
            f"（共 {len(annotations)} 条）。"
        )
    if quotes:
        paragraphs.append(
            f"共引用 {len(quotes)} 条原文摘录；摘录文本与页码为创建时快照，"
            "资料更新后引用可能失效。"
        )
    else:
        paragraphs.append("所选笔记没有可引用的原文摘录，本节不虚构摘录内容。")
    if invalid_count:
        paragraphs.append(
            f"其中 {invalid_count} 条摘录在当前资料版本中已失效，引用可能失效。"
        )
    return {
        "id": "excerpt",
        "kind": "excerpt",
        "title": "原文摘录",
        "paragraphs": paragraphs,
        "quotes": quotes,
        "items": [],
    }


def _practice_section(con, kp_ids: list[int]) -> tuple[dict, list[str]]:
    """小节 4：按知识点名称检索练习候选题（不是正式标签，明确注明）。"""
    knowledge_points = _load_knowledge_points(con, kp_ids)
    keywords = [
        _collapse(point.get("name"))
        for point in knowledge_points
        if _collapse(point.get("name"))
    ]
    paragraphs = ["检索关键词：" + "、".join(keywords) if keywords else "未提供可用知识点名称。"]
    items: list[dict] = []
    question_refs: list[str] = []
    seen: set[str] = set()
    search_failed = False
    truncated = False
    for keyword in keywords:
        try:
            candidates = practice.search_similar_questions(
                keyword, limit=MAX_PRACTICE_PER_KEYWORD
            )
        except HTTPException:
            search_failed = True
            continue
        except Exception:  # noqa: BLE001 - 题库不可用时如实降级，不伪造候选题
            search_failed = True
            continue
        for candidate in candidates or []:
            if not isinstance(candidate, dict):
                continue
            qid = _collapse(candidate.get("qid"))
            if not qid or qid in seen:
                continue
            seen.add(qid)
            question_refs.append(qid)
            items.append(
                {
                    "title": _clip(candidate.get("stem_plain"), QUESTION_TITLE_CHARS),
                    "ref": qid,
                    "detail": "来源：" + _question_source_label(candidate),
                }
            )
            if len(items) >= MAX_PRACTICE_ITEMS:
                truncated = True
                break
        if len(items) >= MAX_PRACTICE_ITEMS:
            break
    if search_failed:
        if items:
            paragraphs.append("部分知识点的题库检索暂不可用，候选题目可能不完整。")
        else:
            paragraphs.append("题库检索暂不可用，未能生成候选题，请稍后在练习页重试。")
    if truncated:
        paragraphs.append(f"候选题目较多，本节仅列前 {MAX_PRACTICE_ITEMS} 条。")
    paragraphs.append(
        "候选题来自题库相似度检索，仅作练习候选，属于非正式标签；"
        "请以题目详解与教师讲解为准。"
    )
    if not items and not search_failed:
        paragraphs.append("本次未检索到候选题，可在练习页按知识点继续筛选。")
    return (
        {
            "id": "practice",
            "kind": "practice",
            "title": "练习候选题",
            "paragraphs": paragraphs,
            "quotes": [],
            "items": items,
        },
        question_refs,
    )


def _independent_verified_count(con, user_id: int, window_start: str) -> int:
    """统计窗口内「独立且核验」的有效证据条数（口径与 ``study_evidence.CALIBER`` 一致）。

    ``summary`` 只给各类边际计数，没有独立作答的交集字段，因此按同一口径在证据表上做一次
    只读统计：``confidence=verified`` 且（自评为 independent，或提示/揭晓明确 none/before）。
    查询失败时返回 0（保守口径，不推断为独立作答）。
    """
    if not window_start:
        return 0
    try:
        row = con.execute(
            "SELECT COUNT(*) FROM study_evidence WHERE user_id=? AND occurred_at>=? "
            "AND valid=1 AND confidence='verified' "
            "AND (self_report='independent' "
            "OR (prompt_state='none' AND reveal_state='before'))",
            (user_id, window_start),
        ).fetchone()
        return int(row[0]) if row is not None else 0
    except Exception:  # noqa: BLE001 - 证据表缺失/结构差异时按样本不足处理
        return 0


def _evidence_section(con, user_id: int) -> dict:
    """小节 5：学习证据关键真实字段（有效事件 / 独立作答 / 样本是否充足）。"""
    try:
        summary = study_evidence.summary(user_id, days=EVIDENCE_DAYS, con=con)
    except Exception:  # noqa: BLE001 - 调用方连接上表尚未就绪时退回懒初始化连接
        try:
            summary = study_evidence.summary(user_id, days=EVIDENCE_DAYS)
        except Exception:  # noqa: BLE001 - 证据模块不可用时如实降级，不阻断讲义生成
            return {
                "id": "evidence",
                "kind": "evidence",
                "title": "学习证据",
                "paragraphs": ["学习证据暂不可用，本节不展示统计数据。"],
                "quotes": [],
                "items": [],
            }
    effective = int(summary.get("effective_count") or 0)
    revised = int(summary.get("revised_count") or 0)
    window_start = str(summary.get("window_start") or "")[:10]
    by_verdict = summary.get("by_verdict") or {}
    correct = int(by_verdict.get("correct") or 0)
    incorrect = int(by_verdict.get("incorrect") or 0)
    pending = int(by_verdict.get("pending") or 0)
    independent = _independent_verified_count(con, user_id, str(summary.get("window_start") or ""))
    threshold = int(study_evidence.SAMPLE_MIN_INDEPENDENT)
    paragraphs = [
        f"统计窗口：近 {EVIDENCE_DAYS} 天（自 {window_start or '未知日期'} 起），"
        f"有效事件 {effective} 条；另有 {revised} 条旧版本因重判失效，未计入。"
    ]
    if effective:
        paragraphs.append(
            f"核验作答：正确 {correct} 条、错误 {incorrect} 条、待核验 {pending} 条；"
            f"独立作答（独立且核验）{independent} 条。"
        )
    else:
        paragraphs.append(
            "近期暂无有效学习证据：先完成一次作答或复习评分后再生成讲义，本节不推断掌握程度。"
        )
    if independent >= threshold:
        paragraphs.append(
            f"样本判断（样本是否充足）：独立且核验的有效作答达到 {threshold} 条门槛，"
            "可结合学情页细项参考。"
        )
    else:
        paragraphs.append(
            f"样本判断（样本是否充足）：独立且核验的有效作答不足 {threshold} 条，样本不足，"
            "本讲义不推断掌握程度。"
        )
    return {
        "id": "evidence",
        "kind": "evidence",
        "title": "学习证据",
        "paragraphs": paragraphs,
        "quotes": [],
        "items": [],
    }


def _meta_section() -> dict:
    """小节 6：生成方式与引用局限声明（固定文案，不含 AI 生成内容）。"""
    return {
        "id": "meta",
        "kind": "meta",
        "title": "生成说明",
        "paragraphs": [
            "规则汇编，非 AI 生成（不含 AI 生成内容）：本讲义由服务端按固定规则汇总课程场景、"
            "笔记原文摘录、学习附件、练习候选题与学习证据，未调用任何 AI 模型。",
            "引用随资料/附件版本或删除可能失效；课程场景、笔记摘录与学习证据为创建时的快照。",
            "附件按创建时固定版本引用：源附件后续升版或删除不会改动本讲义已保存的小节内容。",
            f"generator：{GENERATOR}（规则汇编）。",
        ],
        "quotes": [],
        "items": [],
    }


# ---------------------------------------------------------------------------
# 行投影
# ---------------------------------------------------------------------------


def _load_sections(row) -> list[dict]:
    """解析落库的小节 JSON；损坏时返回空列表（不抛 500）。"""
    sections = _parse_json(_row_dict(row).get("sections_json"), [])
    return [item for item in sections if isinstance(item, dict)] if isinstance(sections, list) else []


def _handout_detail(row) -> dict:
    """讲义详情投影（不含来源 id 列表与落库 JSON）。"""
    data = _row_dict(row)
    course_id = data.get("course_id")
    return {
        "handout_id": int(data["id"]),
        "title": str(data.get("title") or ""),
        "course_id": int(course_id) if course_id is not None else None,
        "version": int(data.get("version") or 1),
        "generator": str(data.get("generator") or GENERATOR),
        "status": "ready",
        "created_at": str(data.get("created_at") or ""),
        "updated_at": str(data.get("updated_at") or ""),
    }


def _handout_summary(row) -> dict:
    """讲义列表摘要：含小节数，不含 sections 内容。"""
    data = _row_dict(row)
    course_id = data.get("course_id")
    return {
        "handout_id": int(data["id"]),
        "title": str(data.get("title") or ""),
        "course_id": int(course_id) if course_id is not None else None,
        "version": int(data.get("version") or 1),
        "section_count": len(_load_sections(data)),
        "created_at": str(data.get("created_at") or ""),
        "updated_at": str(data.get("updated_at") or ""),
    }


def _owned_row(con, user_id: int, handout_id: int):
    """读取本人讲义行；不存在或属于他人返回同一个 404（不泄露存在性）。"""
    row = con.execute(
        "SELECT * FROM handouts WHERE id=? AND user_id=?", (handout_id, user_id)
    ).fetchone()
    if row is None:
        raise HTTPException(404, "讲义不存在或无权访问")
    return row


# ---------------------------------------------------------------------------
# 公开函数：创建 / 列表 / 详情 / 删除
# ---------------------------------------------------------------------------


def create_handout(
    user_id,
    *,
    title=None,
    course_id=None,
    note_ids=None,
    artifact_ids=None,
    kp_ids=None,
    con=None,
) -> dict:
    """创建一份互动讲义并落库固定小节快照，返回 ``{handout, sections}``。

    * 至少一个来源（课程 / 笔记 / 学习附件 / 知识点），否则 422；每类最多 10 个；
    * ``title`` 省略或为空时默认 ``互动讲义 · <课程标题或日期>``，最长 120 字符；
    * 课程走 :func:`app.classroom.db.owned_course`，笔记按本人 ``ai_notes`` 校验，学习附件走
      :func:`app.study_artifact.get_artifact`（他人/不存在/已删除统一 404）；知识点不存在 422；
    * 个人讲义最多 :data:`MAX_HANDOUTS_PER_USER` 份，超出 422；
    * 本节内容全部来自真实行：场景、摘录、附件固定版本、候选题、学习证据，缺数据时如实注明。
    """
    _ensure_ready()
    uid = _validate_user_id(user_id)
    title_value = None
    if title is not None and not (isinstance(title, str) and not title.strip()):
        title_value = _validate_title(title)
    course_value = None if course_id is None else _validate_id_value(course_id, "课程")
    note_values = _validate_int_list(note_ids, "笔记")
    artifact_values = _validate_artifact_id_list(artifact_ids)
    kp_values = _validate_int_list(kp_ids, "知识点")
    if not (course_value is not None or note_values or artifact_values or kp_values):
        raise HTTPException(
            422, "请至少选择一门课程、一篇笔记、一份学习附件或一个知识点"
        )
    now = db.utcnow()
    with _read_connection(con) as read_con:
        course = None
        if course_value is not None:
            course = classroom_db.owned_course(course_value, uid)
        notes = _load_notes(read_con, uid, note_values)
        sections: list[dict] = []
        if course is not None:
            scenes = classroom_db.course_scenes(int(course["id"]), uid)
            sections.append(_course_outline_section(course, scenes))
        if artifact_values:
            sections.append(_artifact_section(read_con, uid, artifact_values))
        if note_values:
            sections.append(_excerpt_section(read_con, uid, notes))
        question_refs: list[str] = []
        if kp_values:
            practice_section, question_refs = _practice_section(read_con, kp_values)
            sections.append(practice_section)
        sections.append(_evidence_section(read_con, uid))
        sections.append(_meta_section())
        if title_value is None:
            if course is not None:
                title_value = _clip(
                    f"互动讲义 · {_collapse(course.get('title')) or '未命名课程'}",
                    MAX_TITLE_LENGTH,
                )
            else:
                title_value = f"互动讲义 · {now[:10]}"
        sections_json = _dump_json(sections)
        note_ids_json = _dump_json(note_values)
        artifact_ids_json = _dump_json(artifact_values)
        question_refs_json = _dump_json(question_refs)
    with _write_connection(con) as active:
        total = int(
            active.execute(
                "SELECT COUNT(*) FROM handouts WHERE user_id=?", (uid,)
            ).fetchone()[0]
        )
        if total >= MAX_HANDOUTS_PER_USER:
            raise HTTPException(
                422,
                f"个人讲义最多保存 {MAX_HANDOUTS_PER_USER} 份，请先删除不再需要的讲义",
            )
        cursor = active.execute(
            "INSERT INTO handouts(user_id, title, course_id, note_ids_json, artifact_ids_json, "
            "question_refs_json, sections_json, generator, version, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                uid,
                title_value,
                course_value,
                note_ids_json,
                artifact_ids_json,
                question_refs_json,
                sections_json,
                GENERATOR,
                1,
                now,
                now,
            ),
        )
        handout_pk = cursor.lastrowid
        if handout_pk is None:  # pragma: no cover - 双引擎正常路径都会回填主键
            raise HTTPException(503, "讲义保存失败，请稍后重试")
        row = _owned_row(active, uid, int(handout_pk))
    return {"handout": _handout_detail(row), "sections": _load_sections(row)}


def list_handouts(user_id, *, limit=DEFAULT_LIST_LIMIT, con=None) -> dict:
    """分页列出本人讲义摘要（按更新时间倒序），返回 ``{items, total, limit}``。"""
    _ensure_ready()
    uid = _validate_user_id(user_id)
    limit_value = _validate_limit(limit)
    with _read_connection(con) as active:
        total = int(
            active.execute(
                "SELECT COUNT(*) FROM handouts WHERE user_id=?", (uid,)
            ).fetchone()[0]
        )
        rows = active.execute(
            "SELECT * FROM handouts WHERE user_id=? ORDER BY updated_at DESC, id DESC LIMIT ?",
            (uid, limit_value),
        ).fetchall()
    return {
        "items": [_handout_summary(row) for row in rows],
        "total": total,
        "limit": limit_value,
    }


def get_handout(user_id, handout_id, con=None) -> dict:
    """读取本人讲义详情与完整小节；不存在的讲义或不属于本人一律 404。"""
    _ensure_ready()
    uid = _validate_user_id(user_id)
    hid = _validate_id_value(handout_id, "讲义")
    with _read_connection(con) as active:
        row = _owned_row(active, uid, hid)
        sections = _load_sections(row)
    return {"handout": _handout_detail(row), "sections": sections}


def delete_handout(user_id, handout_id, con=None) -> dict:
    """删除本人讲义；不存在或属于他人返回 404（不泄露存在性）。"""
    _ensure_ready()
    uid = _validate_user_id(user_id)
    hid = _validate_id_value(handout_id, "讲义")
    with _write_connection(con) as active:
        _owned_row(active, uid, hid)
        active.execute("DELETE FROM handouts WHERE id=? AND user_id=?", (hid, uid))
    return {"deleted": True, "handout_id": hid}


# ---------------------------------------------------------------------------
# 导出：自包含 HTML / 服务端 PDF
# ---------------------------------------------------------------------------


def _esc(value) -> str:
    """转义所有会写入 HTML 的用户内容（属性值也转义引号）。"""
    return html_lib.escape("" if value is None else str(value), quote=True)


_HTML_STYLE = """
:root{color-scheme:light}
*{box-sizing:border-box}
body{margin:0;padding:24px;background:#f5f7fb;color:#1f2937;
  font-family:system-ui,"Microsoft YaHei","PingFang SC","Noto Sans CJK SC",sans-serif;
  font-size:15px;line-height:1.75}
.handout{max-width:920px;margin:0 auto;background:#ffffff;padding:32px 38px;border-radius:10px;
  box-shadow:0 1px 4px rgba(15,23,42,.08)}
.handout-header{border-bottom:2px solid #e5e7eb;padding-bottom:14px;margin-bottom:18px}
.handout-header h1{margin:0 0 6px;font-size:26px;line-height:1.35;color:#111827}
.handout-meta{margin:0;color:#6b7280;font-size:13px}
.handout-section{margin:22px 0 26px;page-break-inside:avoid}
.handout-section h2{margin:0 0 10px;font-size:19px;color:#1d4ed8;
  border-left:4px solid #1d4ed8;padding-left:10px}
.handout-section p{margin:6px 0}
blockquote.quote{margin:10px 0;padding:8px 14px;background:#f8fafc;
  border-left:3px solid #94a3b8;border-radius:4px}
blockquote.quote p{margin:0;white-space:pre-wrap}
blockquote.quote footer{margin-top:4px;color:#64748b;font-size:12.5px}
ul.items{list-style:none;margin:8px 0;padding:0}
ul.items li{border:1px solid #e5e7eb;border-radius:8px;padding:10px 14px;margin:8px 0;
  background:#fbfdff}
ul.items .item-title{margin:0;font-weight:600;color:#111827}
ul.items .item-detail{margin:2px 0 0;color:#475569;font-size:14px;white-space:pre-wrap}
ul.items .item-ref{margin:2px 0 0;color:#94a3b8;font-size:12px;word-break:break-all}
ul.items figure{margin:8px 0 0}
ul.items img{max-width:100%;height:auto;border:1px solid #e2e8f0;border-radius:6px}
.handout-declaration{margin-top:28px;padding-top:14px;border-top:1px dashed #cbd5e1;
  color:#475569;font-size:13.5px}
.handout-declaration h2{margin:0 0 8px;font-size:15px;color:#334155}
@media print{body{background:#fff;padding:0}.handout{box-shadow:none;border-radius:0;
  max-width:none;padding:10mm}.handout-section{page-break-inside:avoid}}
"""

_DECLARATION_PARAGRAPHS = (
    "本讲义由规则汇编生成，非 AI 生成；内容来自课程场景、笔记原文摘录、学习附件与学习证据的真实记录。",
    "引用随资料/附件版本或删除可能失效；附件按创建时固定版本引用，课程场景、笔记摘录与学习证据为创建时的快照。",
    "导出文件为自包含单文件：不加载外部样式、字体、脚本或图片，内嵌预览仅为本讲义保存时的 PNG data URI。",
)


def _item_html(item: dict) -> str:
    """单个 items 项 → HTML（标题 / 说明 / 引用 / 合法 PNG data URI 预览）。"""
    parts = ['<li class="item">']
    title = _collapse(item.get("title"))
    if title:
        parts.append(f'<p class="item-title">{_esc(title)}</p>')
    detail = str(item.get("detail") or "")
    if detail:
        parts.append(f'<p class="item-detail">{_esc(detail)}</p>')
    ref = str(item.get("ref") or "")
    if ref:
        parts.append(f'<p class="item-ref">引用：{_esc(ref)}</p>')
    preview = item.get("preview_data_uri")
    if isinstance(preview, str) and _PNG_DATA_URI_RE.match(preview):
        parts.append(
            '<figure><img src="%s" alt="%s" loading="lazy"></figure>'
            % (_esc(preview), _esc("附件预览"))
        )
    parts.append("</li>")
    return "".join(parts)


def _section_html(section: dict) -> str:
    """单个小节 → HTML；用户内容全部转义，图片只接受合法 PNG data URI。"""
    section_id = _collapse(section.get("id")) or "section"
    kind = _collapse(section.get("kind")) or section_id
    title = _collapse(section.get("title")) or kind
    parts = [
        f'<section class="handout-section" id="section-{_esc(section_id)}" '
        f'data-kind="{_esc(kind)}">',
        f"<h2>{_esc(title)}</h2>",
    ]
    paragraphs = section.get("paragraphs")
    if isinstance(paragraphs, list):
        for paragraph in paragraphs:
            parts.append(f"<p>{_esc(paragraph)}</p>")
    quotes = section.get("quotes")
    if isinstance(quotes, list):
        for quote in quotes:
            if not isinstance(quote, dict):
                continue
            parts.append(
                '<blockquote class="quote"><p>%s</p><footer>%s</footer></blockquote>'
                % (_esc(quote.get("text")), _esc(quote.get("source")))
            )
    items = section.get("items")
    if isinstance(items, list):
        rendered = [_item_html(item) for item in items if isinstance(item, dict)]
        if rendered:
            parts.append('<ul class="items">' + "".join(rendered) + "</ul>")
    parts.append("</section>")
    return "".join(parts)


def _declaration_html() -> str:
    """文末「规则汇编 · 引用局限」声明（文档级固定文案，不含用户内容）。"""
    parts = ['<footer class="handout-declaration"><h2>规则汇编 · 引用局限</h2>']
    for paragraph in _DECLARATION_PARAGRAPHS:
        parts.append(f"<p>{_esc(paragraph)}</p>")
    parts.append(
        '<p class="generator">generator：%s（规则汇编，非 AI 生成）</p>'
        % _esc(GENERATOR)
    )
    parts.append("</footer>")
    return "".join(parts)


def _render_html(handout: dict, sections: list[dict]) -> str:
    """讲义 → 自包含单文件 HTML（``<meta charset>`` + 内联 CSS，无外链、无脚本）。"""
    title = _esc(handout.get("title"))
    handout_id = int(handout.get("handout_id") or 0)
    version = int(handout.get("version") or 1)
    generator = _esc(handout.get("generator") or GENERATOR)
    parts = [
        "<!DOCTYPE html>",
        '<html lang="zh-CN">',
        "<head>",
        '<meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f"<title>{title}</title>",
        f"<style>{_HTML_STYLE}</style>",
        "</head>",
        "<body>",
        f'<article class="handout" data-handout-id="{handout_id}" '
        f'data-version="{version}" data-generator="{generator}">',
        '<header class="handout-header">',
        f"<h1>{title}</h1>",
        f'<p class="handout-meta">互动讲义 · 版本 v{version} · '
        "规则汇编（非 AI 生成）</p>",
        "</header>",
    ]
    for section in sections:
        if isinstance(section, dict):
            parts.append(_section_html(section))
    parts.append(_declaration_html())
    parts.extend(["</article>", "</body>", "</html>"])
    return "".join(parts)


def handout_html(user_id, handout_id, con=None) -> str:
    """导出本人讲义为**自包含单文件 HTML** 字符串（无外链、无脚本、用户内容全部转义）。

    讲义不存在或不属于当前用户返回 404；导出内容为创建时落库的小节快照，不重新回读源数据。
    """
    detail = get_handout(user_id, handout_id, con=con)
    return _render_html(detail["handout"], detail["sections"])


def _render_module():
    """延迟导入服务端渲染模块。

    与课堂导出（``app/classroom/exports.py``）同做法：只有真正导出 PDF 时才加载
    ``app.ai.render``，主应用启动与 HTML 导出都不引入 Playwright 相关依赖；测试对
    ``app.ai.render.render_available`` / ``html_to_pdf`` 的 monkeypatch 仍然生效。
    """
    from .ai import render

    return render


async def handout_pdf(user_id, handout_id, con=None) -> bytes:
    """导出本人讲义为 PDF 字节：先探测渲染能力，不可用按既有口径 503。

    ``app.ai.render.render_available()`` 不可用时抛 HTTPException(503) 中文提示，引导改用
    HTML 导出/浏览器打印；可用时 ``await app.ai.render.html_to_pdf(html)`` 生成 PDF 并校验
    返回内容以 ``%PDF`` 开头。测试通过 monkeypatch 替换 ``render.render_available`` 与
    ``render.html_to_pdf`` 即可覆盖两条分支。
    """
    html = handout_html(user_id, handout_id, con=con)
    render = _render_module()
    availability = render.render_available()
    if not availability.get("available"):
        raise HTTPException(503, "服务器 PDF 渲染不可用，请改用 HTML 导出/浏览器打印")
    try:
        data = await render.html_to_pdf(html)
    except render.RenderUnavailable:
        raise HTTPException(
            503, "服务器 PDF 渲染不可用，请改用 HTML 导出/浏览器打印"
        ) from None
    except HTTPException:
        raise
    except Exception:  # noqa: BLE001 - 渲染异常统一按依赖不可用口径返回
        raise HTTPException(
            503, "服务器 PDF 渲染不可用，请改用 HTML 导出/浏览器打印"
        ) from None
    if not isinstance(data, (bytes, bytearray)) or not bytes(data).startswith(b"%PDF"):
        raise HTTPException(503, "PDF 渲染结果不正确，请改用 HTML 导出/浏览器打印")
    return bytes(data)


def _attachment_disposition(handout: dict, extension: str) -> str:
    """附件下载头：ASCII 安全名 + RFC 5987 中文文件名（浏览器优先使用后者）。"""
    handout_id = int(handout.get("handout_id") or 0)
    title = _collapse(handout.get("title")) or f"handout-{handout_id}"
    ascii_name = f"handout-{handout_id}.{extension}"
    encoded = quote(f"{title}.{extension}", safe="")
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{encoded}"


# ---------------------------------------------------------------------------
# HTTP 请求模型与路由
# ---------------------------------------------------------------------------


class CreateHandoutInput(BaseModel):
    """创建讲义请求体：至少一类来源。

    字段先收为 ``Any``，再统一由 :func:`create_handout` 做中文校验：这样类型错误、空来源、
    超限等参数问题都返回中文 ``detail``，不会落到框架默认的英文校验错误。
    """

    title: Any = None
    course_id: Any = None
    note_ids: Any = None
    artifact_ids: Any = None
    kp_ids: Any = None


@router.post("", status_code=201)
def route_create_handout(body: CreateHandoutInput, user=Depends(current_user)):
    """创建互动讲义（规则汇编，非 AI 生成），返回 ``{handout, sections}``。"""
    return create_handout(
        user["id"],
        title=body.title,
        course_id=body.course_id,
        note_ids=body.note_ids,
        artifact_ids=body.artifact_ids,
        kp_ids=body.kp_ids,
    )


@router.get("")
def route_list_handouts(
    limit: int = Query(default=DEFAULT_LIST_LIMIT),
    user=Depends(current_user),
):
    """列出我的互动讲义摘要（按更新时间倒序）。"""
    return list_handouts(user["id"], limit=limit)


@router.get("/{handout_id}/export")
async def route_export_handout(
    handout_id: int,
    format: str = Query(default="html"),
    user=Depends(current_user),
):
    """导出讲义：``format=html``（默认）返回自包含 HTML，``format=pdf`` 返回服务端 PDF。"""
    export_format = str(format or "").strip().lower()
    detail = get_handout(user["id"], handout_id)["handout"]
    if export_format == "html":
        content = handout_html(user["id"], handout_id)
        return Response(
            content=content,
            media_type="text/html; charset=utf-8",
            headers={"Content-Disposition": _attachment_disposition(detail, "html")},
        )
    if export_format == "pdf":
        data = await handout_pdf(user["id"], handout_id)
        return Response(
            content=data,
            media_type="application/pdf",
            headers={"Content-Disposition": _attachment_disposition(detail, "pdf")},
        )
    raise HTTPException(422, "导出格式不正确，可选：html、pdf")


@router.get("/{handout_id}")
def route_get_handout(handout_id: int, user=Depends(current_user)):
    """读取本人讲义详情与完整小节；他人或不存在的讲义返回 404。"""
    return get_handout(user["id"], handout_id)


@router.delete("/{handout_id}")
def route_delete_handout(handout_id: int, user=Depends(current_user)):
    """删除本人讲义。"""
    return delete_handout(user["id"], handout_id)


__all__ = [
    "ARTIFACT_KIND_LABELS",
    "DEFAULT_LIST_LIMIT",
    "EVIDENCE_DAYS",
    "GENERATOR",
    "MAX_ARTIFACTS_PER_SECTION",
    "MAX_HANDOUTS_PER_USER",
    "MAX_LIST_LIMIT",
    "MAX_PRACTICE_ITEMS",
    "MAX_PREVIEW_TOTAL_BYTES",
    "MAX_QUOTES_PER_HANDOUT",
    "MAX_SCENES_PER_SECTION",
    "MAX_SOURCES_PER_KIND",
    "MAX_TITLE_LENGTH",
    "PNG_DATA_URI_PREFIX",
    "SECTION_ORDER",
    "create_handout",
    "delete_handout",
    "get_handout",
    "handout_html",
    "handout_pdf",
    "init_handouts",
    "list_handouts",
    "router",
]
