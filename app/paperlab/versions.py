"""个人收录版本与「有新版」（Phase 1 剩余 · 方案 §6.2/§6.3）。

背景与边界：

- 从 r5 起，用户把只读题库题目收录到「我的题库」时**固定当时版本**：题面、选项、答案、
  解析、共用材料、可合法访问的配图与题库 ``bank_version`` / ``content_revision`` /
  ``answer_revision`` 一并快照。个人快照默认私有（``status=draft``），不自动公开，
  原题核验/练习状态记录在来源元数据里而不是伪造；
- 题库更新后已收录内容继续指向原收藏版本；``GET .../source-status`` 报告
  ``has_new_version`` 与差异字段，用户显式 ``update-from-source {confirm:true}`` 后
  用新版题库内容生成**新版本**，历史版本行与旧练习继续绑定旧内容；
- 来源下架 / 受限：``source_available=false``，不拉取、不回显受限正文；
- ``pl_question_versions`` 是不可变历史（无修改入口）：创建、编辑、收录、题库更新各写一行，
  内容以 ``content_sha256`` 指纹标识；读取侧按需为老数据补一条当前快照版本（只追加）。

只读题库通过 ``practice.load_question`` 读取（与题库详情同一投影），不写题库；个人数据
全部走应用库 ``pl_questions`` / ``pl_question_versions``。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from .. import db as app_db
from ..auth import current_user
from ..db import connection, utcnow, write_lock
from . import db as paperlab_db
from . import schema
from .db import json_dump, json_load

router = APIRouter()

SOURCE_BANK_PREFIX = "bank:"
MAX_COLLECT_QID = 300
MAX_VERSIONS_LIMIT = 200
DEFAULT_VERSIONS_LIMIT = 50
ORIGINS = ("create", "edit", "collect", "source_update", "snapshot")

# 题库题型 → 个人题库题型（material_group 等上下文条目不可单独收录，见 _live_block）。
_BANK_TYPE_MAP = {
    "single_choice": "SINGLE",
    "multi_choice": "MULTIPLE",
    "fill_blank": "BLANK",
    "short_answer": "SUBJECTIVE",
    "sub_short_answer": "SUBJECTIVE",
    "calculation": "SUBJECTIVE",
    "essay": "SUBJECTIVE",
    "unknown": "SUBJECTIVE",
}

# 题库图片是只读发布包素材，不能直接变成个人资产；只提示、不伪造引用。
_IMAGE_WARNING = (
    "题库原题含 {count} 张配图（只读题库素材，未复制为个人资产）；"
    "可在题库本题查看原图"
)


class SourceUnavailable(Exception):
    """来源题库不可用（下架 / 受限 / 题库暂不可读 / 题目内容不完整）。"""

    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason
        self.message = message


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------

def _text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def content_sha256(content) -> str:
    """内容指纹：归一化 JSON（键排序、紧凑分隔）的 SHA-256。"""
    payload = content if isinstance(content, (dict, list)) else {}
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _asset_refs(content) -> list[str]:
    if not isinstance(content, dict):
        return []
    images = content.get("images")
    if not isinstance(images, list):
        return []
    refs: list[str] = []
    for image in images:
        if not isinstance(image, dict):
            continue
        ref = _text(image.get("asset"))
        if ref and ref not in refs:
            refs.append(ref)
    return refs


def _content_summary(content) -> dict:
    content = content if isinstance(content, dict) else {}
    answers = [str(item).strip() for item in (content.get("answers") or []) if str(item).strip()]
    has_answer = bool(answers)
    if not has_answer and isinstance(content.get("parts"), list):
        for part in content["parts"]:
            part_answers = part.get("answers") if isinstance(part, dict) else None
            if isinstance(part_answers, list) and any(str(item).strip() for item in part_answers):
                has_answer = True
                break
    stem = _text(content.get("stem"))
    return {
        "stem_preview": stem[:80] + ("…" if len(stem) > 80 else ""),
        "answer_preview": "；".join(answers)[:80],
        "has_answer": has_answer,
        "option_count": len(content.get("options") or []),
        "image_count": len(content.get("images") or []),
    }


def _blocks_text(blocks) -> str:
    parts: list[str] = []
    if not isinstance(blocks, list):
        return ""
    for block in blocks:
        if not isinstance(block, dict):
            continue
        text = _text(block.get("text") or block.get("latex"))
        if text:
            parts.append(text)
    return "\n".join(parts).strip()


# ---------------------------------------------------------------------------
# 来源元数据
# ---------------------------------------------------------------------------

def _question_source(row) -> dict:
    """从 pl_questions 行解析来源元数据（兼容老行：无 source_* 列时回退 source 列）。"""
    row = dict(row or {})
    source_ref = _text(row.get("source_ref"))
    qid = _text(row.get("source_question_id"))
    if not qid and source_ref.startswith(SOURCE_BANK_PREFIX):
        qid = source_ref[len(SOURCE_BANK_PREFIX):]
    kind = _text(row.get("source_kind"))
    if not kind:
        kind = "bank" if source_ref.startswith(SOURCE_BANK_PREFIX) else _text(row.get("source")) or "manual"
    meta = json_load(row.get("source_meta_json"), {})
    if not isinstance(meta, dict):
        meta = {}
    warnings = meta.get("warnings")
    return {
        "source_kind": kind,
        "source_ref": source_ref,
        "source_question_id": qid,
        "source_bank_version": _text(row.get("source_bank_version")),
        "source_content_revision": _text(row.get("source_content_revision")),
        "source_answer_revision": _text(row.get("source_answer_revision")),
        "collected_at": _text(row.get("collected_at")) or None,
        "content_sha256": _text(row.get("content_sha256")),
        "warnings": [str(item) for item in warnings if str(item).strip()] if isinstance(warnings, list) else [],
        "meta": meta,
    }


def _revision_label(bank_version: str, content_revision: str, answer_revision: str,
                    fallback: str = "") -> str:
    if content_revision or answer_revision:
        return "|".join(part for part in (content_revision, answer_revision) if part)
    if bank_version:
        return bank_version
    return fallback


def _live_revisions(live: dict) -> dict:
    live = live if isinstance(live, dict) else {}
    return {
        "bank_version": _text(live.get("bank_version")),
        "content_revision": _text(live.get("content_revision")),
        "answer_revision": _text(live.get("answer_revision")),
    }


# ---------------------------------------------------------------------------
# 只读题库读取（不写题库）
# ---------------------------------------------------------------------------

def _load_live(qid: str) -> dict:
    """用题库详情同源投影读取当前题；不存在/不可用统一转 SourceUnavailable。"""
    from .. import practice

    try:
        live = practice.load_question(qid)
    except HTTPException as exc:
        if int(exc.status_code or 0) == 404:
            raise SourceUnavailable("missing", "题库中未找到该题（可能已下架或编号无效）") from None
        raise SourceUnavailable("unavailable", "题库暂不可用，请稍后重试") from None
    except SourceUnavailable:
        raise
    except Exception:  # noqa: BLE001 - 题库读取异常一律降级，不外抛内部细节
        raise SourceUnavailable("unavailable", "题库暂不可用，请稍后重试") from None
    if not isinstance(live, dict) or not _text(live.get("qid")):
        raise SourceUnavailable("unavailable", "题库返回的题目缺少编号")
    return live


def _live_block(live: dict) -> tuple[str, str] | None:
    """当前题是否不可收录/更新：返回 (reason, message)。"""
    if not isinstance(live, dict):
        return "unavailable", "题库暂不可用，请稍后重试"
    if live.get("quarantined"):
        return "restricted", "该题当前在数据复核/隔离中，暂不可收录或更新"
    if not live.get("practice_allowed"):
        status = _text(live.get("practice_status"))
        if status == "context":
            return "context", "该题只是题组共用材料，不能单独收录；请收录对应小题"
        return "restricted", "该题当前不可用（可能已下架或受限）"
    return None


def _http_for_source_error(exc: SourceUnavailable) -> HTTPException:
    code = {
        "missing": 404,
        "restricted": 409,
        "context": 422,
        "invalid": 422,
    }.get(exc.reason, 503)
    return HTTPException(code, exc.message)


def _bank_probe(qids: list[str]) -> dict[str, dict] | None:
    """批量读取题库当前 revision（只读 SQLite；不可读返回 None）。"""
    wanted = [qid for qid in qids if qid]
    if not wanted:
        return {}
    path = Path(app_db.BANK_DIR) / "题库.db"
    if not path.is_file():
        return None
    try:
        con = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=10)
    except sqlite3.Error:
        return None
    con.row_factory = sqlite3.Row
    try:
        columns = {str(row[1]) for row in con.execute("PRAGMA table_info(questions)")}
        if "qid" not in columns:
            return None
        select = ["qid"]
        for column in ("type", "bank_version", "content_revision", "answer_revision",
                       "practice_status", "grading_status"):
            if column in columns:
                select.append(column)
        marks = ",".join("?" for _ in wanted)
        rows = con.execute(
            "SELECT " + ", ".join(select) + " FROM questions WHERE qid IN (" + marks + ")",
            tuple(wanted),
        ).fetchall()
        return {_text(row["qid"]): dict(row) for row in rows if _text(row["qid"])}
    except sqlite3.Error:
        return None
    finally:
        con.close()


# ---------------------------------------------------------------------------
# 题库题 → 个人题快照（只使用可公开访问的题面/答案信息）
# ---------------------------------------------------------------------------

def _normalize_bank_options(raw) -> list[dict]:
    options: list[dict] = []
    for index, item in enumerate(raw if isinstance(raw, list) else []):
        if isinstance(item, dict):
            label = _text(item.get("label")).upper()
            text = _text(item.get("text"))
        else:
            label, text = "", _text(item)
        if not text:
            continue
        if not label:
            label = chr(ord("A") + index) if index < 26 else str(index + 1)
        options.append({"label": label[:4], "text": text})
    return options[:12]


def _choice_answers(raw_answer: str, labels: set[str]) -> tuple[list[str], str]:
    """把题库 ``answer_text``（如 A / CD / A、C）映射成选项标号；无法映射返回原文。"""
    text = _text(raw_answer)
    if not text:
        return [], ""
    compact = "".join(ch for ch in text.upper() if not ch.isspace() and ch not in "，,、;；/")
    if compact and all(ch in labels for ch in compact):
        ordered: list[str] = []
        for ch in compact:
            if ch not in ordered:
                ordered.append(ch)
        return ordered, ""
    hit = sorted({ch for ch in compact if ch in labels})
    if hit:
        return hit, ""
    return [], text


def _build_content(live: dict) -> tuple[str, dict, list[str]]:
    """把题库投影转成个人题 content（stem/options/answers/explanation/共用材料）。"""
    warnings: list[str] = []
    raw_type = _text(live.get("type")).lower()
    qtype = _BANK_TYPE_MAP.get(raw_type, "SUBJECTIVE")
    stem = _text(live.get("stem_plain"))
    if not stem:
        stem = _blocks_text(live.get("stem_blocks"))
    options = _normalize_bank_options(live.get("options"))
    try:
        options = schema.normalize_options(options)
    except ValueError:
        options = []
    if qtype in ("SINGLE", "MULTIPLE") and not options:
        warnings.append("题库原题为选择题但缺少选项，已按主观题收录")
        qtype = "SUBJECTIVE"
    labels = {str(option.get("label") or "") for option in options}
    raw_answer = _text(live.get("answer_text"))
    unresolved = ""
    if qtype in ("SINGLE", "MULTIPLE"):
        answers, unresolved = _choice_answers(raw_answer, labels)
        if qtype == "SINGLE" and len(answers) > 1:
            answers = answers[:1]
        if not answers and raw_answer:
            warnings.append("题库原题答案无法自动对应选项标号，已保留原文待人工确认")
    else:
        answers = [raw_answer] if raw_answer else []
    explanation = _text(live.get("solution"))
    note = _text(live.get("answer_note"))
    if unresolved:
        prefix = f"【题库原答案】{unresolved}"
        explanation = prefix + ("\n" + explanation if explanation else "")
    if note:
        explanation = (note + ("\n" + explanation if explanation else "")).strip()
    group = live.get("group") if isinstance(live.get("group"), dict) else {}
    material = _text(group.get("material_text")) or _blocks_text(live.get("shared_blocks"))
    parent_stem = _text(group.get("parent_stem"))
    content: dict = {
        "stem": stem,
        "options": options,
        "answers": answers[:8],
        "explanation": explanation,
        "rubric": "",
        "parts": [],
        "images": [],
    }
    if material:
        content["material"] = material
    if parent_stem and parent_stem != stem:
        content["parent_stem"] = parent_stem
    image_count = len(live.get("images") or []) if isinstance(live.get("images"), list) else 0
    material_images = group.get("material_images")
    if isinstance(material_images, list):
        image_count += len(material_images)
    if image_count:
        warnings.append(_IMAGE_WARNING.format(count=image_count))
    return qtype, content, warnings


def build_snapshot(live: dict) -> dict:
    """构造可写入个人库的题目快照；内容不合法时抛 SourceUnavailable('invalid')。"""
    qid = _text(live.get("qid"))
    if not qid:
        raise SourceUnavailable("unavailable", "题库返回的题目缺少编号")
    qtype, content, warnings = _build_content(live)
    try:
        normalized = schema.normalize_question_content(content, qtype)
    except ValueError as exc:
        raise SourceUnavailable("invalid", f"题库题目内容不完整，暂不能收录：{exc}") from None
    group = live.get("group") if isinstance(live.get("group"), dict) else {}
    revisions = _live_revisions(live)
    source = {
        "ref": SOURCE_BANK_PREFIX + qid,
        "qid": qid,
        "bank_version": revisions["bank_version"],
        "content_revision": revisions["content_revision"],
        "answer_revision": revisions["answer_revision"],
        "practice_status": _text(live.get("practice_status")) or "practice",
        "grading_status": _text(live.get("grading_status")) or "auto",
        "number_label": _text(live.get("number_label")),
        "paper_name": _text(live.get("paper_name")),
        "year": live.get("year"),
        "province": _text(live.get("province")),
        "group_id": _text(group.get("group_id")),
    }
    return {
        "qid": qid,
        "qtype": qtype,
        "subject": _text(live.get("subject"))[:40],
        "grade": "",
        "difficulty": None,
        "knowledge_tags": [],
        "content": normalized,
        "warnings": warnings,
        "source": source,
    }


# ---------------------------------------------------------------------------
# 版本行：不可变历史（只追加）
# ---------------------------------------------------------------------------

def snapshot_from_row(row) -> dict:
    """把 pl_questions 行转成版本快照（供 create/patch 写入版本行）。"""
    row = dict(row or {})
    content = json_load(row.get("content_json"), {})
    if not isinstance(content, dict):
        content = {}
    tags = json_load(row.get("knowledge_tags_json"), [])
    if not isinstance(tags, list):
        tags = []
    return {
        "question_id": int(row.get("id") or 0),
        "user_id": int(row.get("user_id") or 0),
        "version": int(row.get("version") or 1),
        "qtype": _text(row.get("type")) or "SUBJECTIVE",
        "subject": _text(row.get("subject")),
        "grade": _text(row.get("grade")),
        "difficulty": row.get("difficulty"),
        "tags": [str(tag) for tag in tags if str(tag).strip()],
        "content": content,
        "sha": content_sha256(content),
        "assets": _asset_refs(content),
        "source_ref": _text(row.get("source_ref")),
        "source_question_id": _text(row.get("source_question_id")),
        "source_bank_version": _text(row.get("source_bank_version")),
        "source_content_revision": _text(row.get("source_content_revision")),
        "source_answer_revision": _text(row.get("source_answer_revision")),
    }


_VERSION_INSERT_COLUMNS = (
    "question_id", "user_id", "version", "origin", "type", "subject", "grade",
    "difficulty", "knowledge_tags_json", "content_json", "content_sha256",
    "asset_refs_json", "source_ref", "source_question_id", "source_bank_version",
    "source_content_revision", "source_answer_revision", "created_at",
)


def _insert_version(con, snapshot: dict, *, version: int, origin: str,
                    created_at: str | None = None) -> None:
    """写一行不可变版本（同版本已存在时忽略，保证幂等/并发安全）。"""
    values = (
        int(snapshot["question_id"]), int(snapshot["user_id"]), int(version),
        origin if origin in ORIGINS else "snapshot",
        snapshot["qtype"], snapshot["subject"], snapshot["grade"], snapshot["difficulty"],
        json_dump(snapshot["tags"]), json_dump(snapshot["content"]), snapshot["sha"],
        json_dump(snapshot["assets"]), snapshot["source_ref"],
        snapshot["source_question_id"], snapshot["source_bank_version"],
        snapshot["source_content_revision"], snapshot["source_answer_revision"],
        created_at or utcnow(),
    )
    placeholders = ",".join("?" for _ in _VERSION_INSERT_COLUMNS)
    con.execute(
        "INSERT INTO pl_question_versions(" + ",".join(_VERSION_INSERT_COLUMNS) + ") "
        "VALUES (" + placeholders + ") ON CONFLICT(question_id,version) DO NOTHING",
        values,
    )


def record_version(con, snapshot: dict, *, version: int, origin: str,
                   created_at: str | None = None) -> None:
    """在调用方事务内追加版本行（创建/编辑/收录/题库更新共用入口）。"""
    _insert_version(con, snapshot, version=version, origin=origin, created_at=created_at)


def _latest_version(con, question_id: int) -> dict | None:
    row = con.execute(
        "SELECT * FROM pl_question_versions WHERE question_id=? ORDER BY version DESC LIMIT 1",
        (int(question_id),),
    ).fetchone()
    if row is None:
        return None
    return dict(row)


def _latest_version_read(question_id: int) -> dict | None:
    with connection() as con:
        return _latest_version(con, question_id)


def _ensure_row_in_tx(con, row) -> None:
    """老数据/旁路写入（导入、AI 补全）在首次读取时补一条当前快照版本（只追加）。"""
    snapshot = snapshot_from_row(row)
    if not snapshot["question_id"] or not snapshot["user_id"]:
        return
    latest = _latest_version(con, snapshot["question_id"])
    if latest is not None and _text(latest.get("content_sha256")) == snapshot["sha"]:
        return
    next_version = max(
        int(snapshot["version"]),
        (int(latest.get("version") or 0) + 1) if latest is not None else 1,
    )
    _insert_version(con, snapshot, version=next_version, origin="snapshot")
    # 顺带回填内容指纹（老行 migration 前为空）。
    if not _text(row.get("content_sha256")):
        con.execute(
            "UPDATE pl_questions SET content_sha256=? WHERE id=? AND (content_sha256 IS NULL OR content_sha256='')",
            (snapshot["sha"], snapshot["question_id"]),
        )


def ensure_history(question_row) -> None:
    """确保题目至少有一条版本行、且最新版本与当前内容一致（只追加，不改内容）。"""
    snapshot = snapshot_from_row(question_row)
    if not snapshot["question_id"]:
        return
    latest = _latest_version_read(snapshot["question_id"])
    if latest is not None and _text(latest.get("content_sha256")) == snapshot["sha"]:
        return
    with connection() as con:
        write_lock(con, "paperlab-question")
        fresh = con.execute(
            "SELECT * FROM pl_questions WHERE id=? AND user_id=? AND deleted_at IS NULL",
            (snapshot["question_id"], snapshot["user_id"]),
        ).fetchone()
        if fresh is not None:
            _ensure_row_in_tx(con, dict(fresh))


def ensure_history_in_tx(con, question_row) -> None:
    """在调用方事务内补齐历史快照（编辑题目前保证旧内容可追溯）。"""
    _ensure_row_in_tx(con, question_row)


# ---------------------------------------------------------------------------
# 有新版判定 / 差异摘要
# ---------------------------------------------------------------------------

def _source_changed(source: dict, live: dict, live_sha: str) -> bool:
    """是否题库已有新版：revision 为权威口径；旧库无 revision 时退化为内容指纹比较。"""
    current = _live_revisions(live)
    old_content = _text(source.get("source_content_revision"))
    old_answer = _text(source.get("source_answer_revision"))
    new_content = current["content_revision"]
    new_answer = current["answer_revision"]
    revisions_known = bool(old_content or old_answer) and bool(new_content or new_answer)
    if revisions_known:
        # r5 起题库带内容/答案 revision：只按它判定，避免应用侧快照归一化升级造成误报。
        return old_content != new_content or old_answer != new_answer
    old_sha = _text(source.get("content_sha256"))
    if old_sha and live_sha:
        return old_sha != live_sha
    old_bank = _text(source.get("source_bank_version"))
    if old_bank and current["bank_version"]:
        return old_bank != current["bank_version"]
    return False


def _parts_signature(raw) -> str:
    if not isinstance(raw, list):
        return "[]"
    return json.dumps(raw, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _diff_snapshots(old: dict, new: dict) -> dict:
    old = old if isinstance(old, dict) else {}
    new = new if isinstance(new, dict) else {}
    changed: list[str] = []

    def _text_changed(field: str) -> bool:
        return schema.normalize_compare_text(old.get(field)) != schema.normalize_compare_text(new.get(field))

    if _text_changed("stem"):
        changed.append("题干")
    if schema._option_signature(old.get("options")) != schema._option_signature(new.get("options")):
        changed.append("选项")
    if schema._answer_signature(old.get("answers")) != schema._answer_signature(new.get("answers")):
        changed.append("答案")
    if _parts_signature(old.get("parts")) != _parts_signature(new.get("parts")):
        changed.append("子题")
    if _text_changed("explanation"):
        changed.append("解析")
    if _text_changed("material"):
        changed.append("共用材料")
    if _text_changed("parent_stem"):
        changed.append("题组题干")
    if _text_changed("rubric"):
        changed.append("评分标准")
    if _asset_refs(old) != _asset_refs(new):
        changed.append("配图")
    try:
        similarity = schema.stem_similarity(old.get("stem"), new.get("stem"))
    except Exception:  # noqa: BLE001 - 差异摘要失败不影响主流程
        similarity = None
    return {"changed": changed, "field_count": len(changed), "stem_similarity": similarity}


def _local_edited(question_id: int) -> bool:
    latest = _latest_version_read(int(question_id))
    return bool(latest) and _text(latest.get("origin")) in ("edit",)


def source_status(question_row) -> dict:
    """GET .../source-status 的响应体：只报告版本/可用性/差异字段，不回显题库受限正文。"""
    row = dict(question_row or {})
    source = _question_source(row)
    question_id = int(row.get("id") or 0)
    payload: dict[str, Any] = {
        "question_id": question_id,
        "version": int(row.get("version") or 1),
        "source_kind": source["source_kind"],
        "source_ref": source["source_ref"],
        "source_question_id": source["source_question_id"],
        "collected_at": source["collected_at"],
        "collected_revision": _revision_label(
            source["source_bank_version"], source["source_content_revision"],
            source["source_answer_revision"], source["content_sha256"],
        ),
        "collected_bank_version": source["source_bank_version"],
        "collected_content_revision": source["source_content_revision"],
        "collected_answer_revision": source["source_answer_revision"],
        "collected_content_sha256": source["content_sha256"],
        "current_revision": None,
        "current_bank_version": None,
        "current_content_revision": None,
        "current_answer_revision": None,
        "source_available": None,
        "source_restricted": False,
        "has_new_version": False,
        "local_edited": _local_edited(question_id) if question_id else False,
        "diff": None,
        "notice": "",
        "warnings": source["warnings"],
    }
    if source["source_kind"] != "bank" or not source["source_question_id"]:
        payload["notice"] = "该题不是题库收录题，没有题库来源版本。"
        return payload
    qid = source["source_question_id"]
    try:
        live = _load_live(qid)
    except SourceUnavailable as exc:
        payload["source_available"] = False
        payload["notice"] = "来源已从题库下架或暂不可用；已收录版本保持收藏时的内容。" \
            if exc.reason == "missing" else exc.message
        return payload
    block = _live_block(live)
    if block:
        payload["source_available"] = False
        payload["source_restricted"] = True
        payload["notice"] = block[1]
        return payload
    try:
        snapshot = build_snapshot(live)
    except SourceUnavailable as exc:
        payload["source_available"] = False
        payload["notice"] = exc.message
        return payload
    live_sha = content_sha256(snapshot["content"])
    current = _live_revisions(live)
    payload.update({
        "source_available": True,
        "current_revision": _revision_label(
            current["bank_version"], current["content_revision"],
            current["answer_revision"], live_sha,
        ),
        "current_bank_version": current["bank_version"],
        "current_content_revision": current["content_revision"],
        "current_answer_revision": current["answer_revision"],
    })
    changed = _source_changed(source, live, live_sha)
    payload["has_new_version"] = bool(changed)
    if changed:
        old_content = json_load(row.get("content_json"), {})
        payload["diff"] = _diff_snapshots(
            old_content if isinstance(old_content, dict) else {}, snapshot["content"],
        )
        payload["notice"] = "题库已有新版；收藏版仍固定为收录时的内容，可选择沿用或收录新版。"
    else:
        payload["notice"] = "已是最新版本。"
    return payload


def _batch_changed(item: dict, live: dict) -> bool | None:
    """列表批量判定：只比较轻量 revision 字段（不加载完整题面）。"""
    current = _live_revisions(live)
    old_content = _text(item.get("source_content_revision"))
    old_answer = _text(item.get("source_answer_revision"))
    new_content = current["content_revision"]
    new_answer = current["answer_revision"]
    if (old_content or old_answer) and (new_content or new_answer):
        return old_content != new_content or old_answer != new_answer
    old_bank = _text(item.get("source_bank_version"))
    if old_bank and current["bank_version"]:
        return old_bank != current["bank_version"]
    return None


def annotate_source_flags(items) -> list[dict]:
    """给列表/详情返回的 public_question 批量填充 ``has_new_version`` / ``source_available``。"""
    if not isinstance(items, list) or not items:
        return items
    bank_items = [
        item for item in items
        if isinstance(item, dict) and item.get("source_kind") == "bank" and item.get("source_question_id")
    ]
    for item in items:
        if not isinstance(item, dict):
            continue
        if item.get("source_kind") != "bank":
            item["has_new_version"] = False
            item["source_available"] = None
        else:
            item.setdefault("has_new_version", None)
            item.setdefault("source_available", False)
    if not bank_items:
        return items
    qids = [str(item["source_question_id"]) for item in bank_items]
    probe = _bank_probe(qids)
    for item in bank_items:
        qid = str(item["source_question_id"])
        if probe is None:
            item["has_new_version"] = None
            item["source_available"] = None
            continue
        live = probe.get(qid)
        if live is None:
            item["source_available"] = False
            item["has_new_version"] = False
            continue
        item["source_available"] = True
        item["has_new_version"] = _batch_changed(item, live)
    return items


# ---------------------------------------------------------------------------
# 收录 / 版本 / 更新接口
# ---------------------------------------------------------------------------

class CollectInput(BaseModel):
    qid: str = Field(min_length=1, max_length=MAX_COLLECT_QID)


class UpdateFromSourceInput(BaseModel):
    confirm: bool = False
    expected_version: int | None = Field(default=None, ge=1)


def _find_collected(user_id: int, source_ref: str) -> dict | None:
    with connection() as con:
        row = con.execute(
            "SELECT * FROM pl_questions WHERE user_id=? AND deleted_at IS NULL AND source_ref=? "
            "ORDER BY id DESC LIMIT 1",
            (int(user_id), source_ref),
        ).fetchone()
    return dict(row) if row is not None else None


_COLLECT_INSERT_COLUMNS = (
    "user_id", "source", "type", "subject", "grade", "difficulty",
    "knowledge_tags_json", "content_json", "status", "verify_status", "version",
    "source_ref", "source_kind", "source_question_id", "source_bank_version",
    "source_content_revision", "source_answer_revision", "source_meta_json",
    "collected_at", "content_sha256", "created_at", "updated_at",
)


def _collect_existing_payload(row: dict) -> dict:
    question = paperlab_db.public_question(row)
    annotate_source_flags([question])
    status = source_status(row)
    notice = "该题已收录过，返回已有条目（固定为收录时版本）。"
    if status.get("has_new_version"):
        notice += "题库已有新版，可查看差异后选择收录新版。"
    return {
        "collected": True,
        "already_collected": True,
        "question": question,
        "question_id": int(row["id"]),
        "version": int(row.get("version") or 1),
        "has_new_version": bool(status.get("has_new_version")),
        "source_available": status.get("source_available"),
        "warnings": question.get("collect_warnings") or [],
        "notice": notice,
    }


@router.post("/questions/collect")
def collect_question(data: CollectInput, user=Depends(current_user)):
    """把只读题库题收录到「我的题库」：固定当前版本，同版本重复收录复用条目。"""
    qid = schema.normalize_bank_qid(data.qid)
    if not qid:
        raise HTTPException(422, "题目编号格式不正确")
    source_ref = SOURCE_BANK_PREFIX + qid
    existing = _find_collected(user["id"], source_ref)
    if existing is not None:
        return _collect_existing_payload(existing)
    try:
        live = _load_live(qid)
    except SourceUnavailable as exc:
        raise _http_for_source_error(exc) from None
    block = _live_block(live)
    if block:
        code = 422 if block[0] == "context" else 409
        raise HTTPException(code, block[1])
    try:
        snapshot = build_snapshot(live)
    except SourceUnavailable as exc:
        raise _http_for_source_error(exc) from None
    content_json = json_dump(snapshot["content"])
    digest = content_sha256(snapshot["content"])
    now = utcnow()
    source_meta_json = json_dump({
        "warnings": snapshot["warnings"],
        "license": "题库原版权说明见题库发布包；个人快照默认私有，不自动公开",
        "original": snapshot["source"],
        "collected_by": "versions.collect",
    })
    created_row: dict | None = None
    with connection() as con:
        write_lock(con, "paperlab-question")
        # 双重检查：并发/重复点击时复用已有条目，不产生重复收藏。
        row = con.execute(
            "SELECT * FROM pl_questions WHERE user_id=? AND deleted_at IS NULL AND source_ref=? "
            "ORDER BY id DESC LIMIT 1",
            (user["id"], source_ref),
        ).fetchone()
        if row is not None:
            existing = dict(row)
        else:
            values = (
                user["id"], "bank", snapshot["qtype"], snapshot["subject"],
                snapshot["grade"], snapshot["difficulty"], json_dump(snapshot["knowledge_tags"]),
                content_json, "draft", "pending", 1, source_ref, "bank", qid,
                snapshot["source"]["bank_version"], snapshot["source"]["content_revision"],
                snapshot["source"]["answer_revision"], source_meta_json, now, digest, now, now,
            )
            placeholders = ",".join("?" for _ in _COLLECT_INSERT_COLUMNS)
            question_id = int(con.execute(
                "INSERT INTO pl_questions(" + ",".join(_COLLECT_INSERT_COLUMNS) + ") "
                "VALUES (" + placeholders + ")",
                values,
            ).lastrowid)
            fresh = con.execute(
                "SELECT * FROM pl_questions WHERE id=?", (question_id,),
            ).fetchone()
            fresh_row = dict(fresh) if fresh is not None else {}
            if fresh_row:
                record_version(
                    con, snapshot_from_row(fresh_row), version=1, origin="collect",
                    created_at=now,
                )
                created_row = fresh_row
    if existing is not None:
        return _collect_existing_payload(existing)
    if created_row is None:
        raise HTTPException(503, "收录失败，请稍后重试")
    question = paperlab_db.public_question(created_row)
    annotate_source_flags([question])
    return {
        "collected": True,
        "already_collected": False,
        "question": question,
        "question_id": int(created_row["id"]),
        "version": int(created_row.get("version") or 1),
        "has_new_version": False,
        "source_available": True,
        "source": snapshot["source"],
        "warnings": snapshot["warnings"],
        "notice": "已收录到我的题库并固定当前题库版本；个人快照默认私有，不会自动公开。",
    }


def _public_version(row: dict, *, current_sha: str, current_version: int | None) -> dict:
    content = json_load(row.get("content_json"), {})
    if not isinstance(content, dict):
        content = {}
    version = int(row.get("version") or 1)
    return {
        "version": version,
        "origin": _text(row.get("origin")) or "snapshot",
        "type": _text(row.get("type")) or "SUBJECTIVE",
        "subject": _text(row.get("subject")),
        "grade": _text(row.get("grade")),
        "knowledge_tags": json_load(row.get("knowledge_tags_json"), []) or [],
        "difficulty": row.get("difficulty"),
        "content": content,
        "content_sha256": _text(row.get("content_sha256")),
        "asset_refs": json_load(row.get("asset_refs_json"), []) or [],
        "source_ref": _text(row.get("source_ref")),
        "source_question_id": _text(row.get("source_question_id")),
        "source_bank_version": _text(row.get("source_bank_version")),
        "source_content_revision": _text(row.get("source_content_revision")),
        "source_answer_revision": _text(row.get("source_answer_revision")),
        "created_at": _text(row.get("created_at")),
        "summary": _content_summary(content),
        "is_current": (
            _text(row.get("content_sha256")) == current_sha and version == current_version
        ),
    }


@router.get("/questions/{question_id}/versions")
def list_question_versions(
    question_id: int,
    limit: int = Query(DEFAULT_VERSIONS_LIMIT, ge=1, le=MAX_VERSIONS_LIMIT),
    user=Depends(current_user),
):
    """版本历史（不可变、只读）：收录/创建/编辑/题库更新各一版，含当前内容标记。"""
    row = paperlab_db.fetch_question(question_id, user["id"])
    try:
        ensure_history(row)
    except Exception:  # noqa: BLE001 - 历史补写失败不阻断读取
        pass
    snapshot = snapshot_from_row(row)
    with connection() as con:
        total = int(con.execute(
            "SELECT COUNT(*) FROM pl_question_versions WHERE question_id=? AND user_id=?",
            (question_id, user["id"]),
        ).fetchone()[0])
        rows = con.execute(
            "SELECT * FROM pl_question_versions WHERE question_id=? AND user_id=? "
            "ORDER BY version DESC LIMIT ?",
            (question_id, user["id"], limit),
        ).fetchall()
    version_rows = [dict(item) for item in rows]
    current_candidates = [
        int(item.get("version") or 0) for item in version_rows
        if _text(item.get("content_sha256")) == snapshot["sha"]
    ]
    current_version = max(current_candidates) if current_candidates else None
    items = [
        _public_version(item, current_sha=snapshot["sha"], current_version=current_version)
        for item in version_rows
    ]
    return {
        "question_id": int(question_id),
        "current_version": int(row.get("version") or 1),
        "content_sha256": snapshot["sha"],
        "total": total,
        "items": items,
        "notice": "版本内容只读：用于对照与追溯，不能修改历史版本。",
    }


@router.get("/questions/{question_id}/source-status")
def question_source_status(question_id: int, user=Depends(current_user)):
    """题库来源状态：是否下架/受限、是否有新版、收藏版本号与当前版本号、差异字段。"""
    row = paperlab_db.fetch_question(question_id, user["id"])
    return source_status(row)


@router.post("/questions/{question_id}/update-from-source")
def update_from_source(
    question_id: int,
    data: UpdateFromSourceInput,
    user=Depends(current_user),
):
    """显式确认后用题库新版内容创建新版本；历史版本与旧练习继续绑定旧内容。"""
    row = paperlab_db.fetch_question(question_id, user["id"])
    source = _question_source(row)
    if source["source_kind"] != "bank" or not source["source_question_id"]:
        raise HTTPException(422, "该题不是题库收录题，无法从题库更新")
    if not data.confirm:
        raise HTTPException(422, "请确认后再收录新版")
    status = source_status(row)
    current_question = paperlab_db.public_question(row)
    annotate_source_flags([current_question])
    if not status.get("source_available"):
        return {
            "updated": False,
            "source_available": False,
            "source_restricted": bool(status.get("source_restricted")),
            "has_new_version": False,
            "question": current_question,
            "notice": status.get("notice") or "来源已从题库下架或受限，保留已收录版本。",
        }
    if not status.get("has_new_version"):
        return {
            "updated": False,
            "source_available": True,
            "has_new_version": False,
            "question": current_question,
            "notice": "已是最新版本，无需更新。",
        }
    qid = source["source_question_id"]
    try:
        live = _load_live(qid)
    except SourceUnavailable as exc:
        raise _http_for_source_error(exc) from None
    block = _live_block(live)
    if block:
        code = 422 if block[0] == "context" else 409
        raise HTTPException(code, block[1])
    try:
        snapshot = build_snapshot(live)
    except SourceUnavailable as exc:
        raise _http_for_source_error(exc) from None
    digest = content_sha256(snapshot["content"])
    current_version = int(row.get("version") or 1)
    if data.expected_version is not None and int(data.expected_version) != current_version:
        raise HTTPException(409, "题目已被修改（版本冲突），请刷新后重试")
    new_version = current_version + 1
    now = utcnow()
    source_meta_json = json_dump({
        "warnings": snapshot["warnings"],
        "license": "题库原版权说明见题库发布包；个人快照默认私有，不自动公开",
        "original": snapshot["source"],
        "collected_by": "versions.update_from_source",
    })
    with connection() as con:
        write_lock(con, "paperlab-question")
        fresh = con.execute(
            "SELECT * FROM pl_questions WHERE id=? AND user_id=? AND deleted_at IS NULL",
            (question_id, user["id"]),
        ).fetchone()
        if fresh is None:
            raise HTTPException(404, "未找到该题目")
        _ensure_row_in_tx(con, dict(fresh))
        params = (
            json_dump(snapshot["content"]), snapshot["qtype"], snapshot["subject"],
            snapshot["source"]["bank_version"], snapshot["source"]["content_revision"],
            snapshot["source"]["answer_revision"], source_meta_json, now, digest,
            new_version, now, question_id, user["id"], current_version,
        )
        changed = con.execute(
            "UPDATE pl_questions SET content_json=?, type=?, subject=?, "
            "source_bank_version=?, source_content_revision=?, source_answer_revision=?, "
            "source_meta_json=?, collected_at=?, content_sha256=?, version=?, updated_at=? "
            "WHERE id=? AND user_id=? AND version=? AND deleted_at IS NULL",
            params,
        ).rowcount
        if not changed:
            raise HTTPException(409, "题目已被修改（版本冲突），请刷新后重试")
        after = con.execute("SELECT * FROM pl_questions WHERE id=?", (question_id,)).fetchone()
        if after is not None:
            record_version(
                con, snapshot_from_row(dict(after)), version=new_version,
                origin="source_update", created_at=now,
            )
    updated = paperlab_db.public_question(paperlab_db.fetch_question(question_id, user["id"]))
    annotate_source_flags([updated])
    return {
        "updated": True,
        "question": updated,
        "question_id": int(question_id),
        "previous_version": current_version,
        "version": new_version,
        "source": snapshot["source"],
        "diff": status.get("diff"),
        "warnings": snapshot["warnings"],
        "notice": "已用题库新版创建新版本；历史版本与已开始的练习继续绑定旧内容。",
    }


__all__ = [
    "SourceUnavailable",
    "annotate_source_flags",
    "build_snapshot",
    "collect_question",
    "content_sha256",
    "ensure_history",
    "ensure_history_in_tx",
    "list_question_versions",
    "question_source_status",
    "record_version",
    "router",
    "snapshot_from_row",
    "source_status",
    "update_from_source",
]
