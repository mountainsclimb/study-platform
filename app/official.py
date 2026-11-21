"""平台精选（``official:<public_id>``）读取适配层（方案 §6 统一题目引用 + §8 阶段 3）。

把广场/评审链路产出的**公开题目**接入统一题目引用与学习链路（检索、练习、错题、复习、
工坊组卷）：引用格式 ``official:<public_id>``，其中 ``public_id`` 是广场对外公开题号
（``plz-`` + 20 位随机 hex，与内部自增 id 解耦）。

可见性（与 ``app/plaza/db.py`` 同一口径，**查询时判定**、不做进程内缓存）：

* ``community_review``（初审通过、社区核验中）、``verified``（社区核验通过）、
  ``published``（平台精选）且 ``public_id`` 非空时可读；其中 ``published`` 还要求存在
  对应的 **active** ``plaza_releases`` 发布指针（签发后的撤回立即失效）；
* ``draft/pending_initial/disputed/withdrawn/rejected/pending_release`` 等一律不可读：
  detail / 作答 / 组卷快照统一 404，不向前端回显任何受限正文；
* 撤回/争议后公开图同样立即失效（公开图接口按当前状态实时授权）。

本模块只读广场数据（``plaza_submissions/plaza_releases/plaza_public_assets`` 与快照 JSON），
复用 ``app.plaza.service`` 的公开投影（``public_content`` / ``public_source``）与
``app.plaza.figures`` 的公开图映射，因此与广场公开详情页字段口径一致；不写入广场表、
不写 ``app/plaza/**``（该目录由并行工作流维护）。

对外只暴露三个读取函数：

* :func:`load_official_question`：按 public_id 读公开题，返回与 ``practice.load_question``
  同形的题目字典（``qid=official:<public_id>``）；不可见统一 404。
* :func:`search_official`：公开题列表 + total（LIKE 快照 JSON；供 ``/api/questions`` 的
  ``source=official|all`` 使用）。
* :func:`inspect_official`：只返回可见性/状态摘要（不回显正文），供错题卡/复习队列在
  题目被撤回后显示「内容已撤回」提示并保留历史成绩。
"""
from __future__ import annotations

import re

from fastapi import HTTPException

from . import question_ref
from .db import connection

SOURCE = question_ref.SOURCE_OFFICIAL
SOURCE_LABEL = question_ref.SOURCE_LABELS[SOURCE]

#: 平台精选题在练习侧的状态提示（社区待核验/已核验/精选各自的说明）。
NOTICE_COMMUNITY_REVIEW = (
    "该题已通过初审，正在社区核验（社区待核验）；内容可能继续修订，答案与解析仅供自测参考。"
)
NOTICE_VERIFIED = "该题由社区评审核验通过，尚未经平台编辑签发精选；答案与解析仅供自测参考。"
NOTICE_PUBLISHED = "该题已由平台编辑签发为平台精选，答案与解析可在作答后查看。"
NOTICE_WITHDRAWN = "该题已撤回，内容不再展示；历史作答、错题与成绩记录保留。"
NOTICE_DISPUTED = "该题有争议或待修订，已暂停展示内容；历史作答、错题与成绩记录保留。"
NOTICE_UNAVAILABLE = (
    "该平台精选题目当前不可见（未发布、已撤回或已下架）；历史作答、错题与成绩记录保留。"
)

STATUS_NOTICES = {
    "community_review": NOTICE_COMMUNITY_REVIEW,
    "verified": NOTICE_VERIFIED,
    "published": NOTICE_PUBLISHED,
    "withdrawn": NOTICE_WITHDRAWN,
    "disputed": NOTICE_DISPUTED,
}

#: 快照题型（工坊题型）→ 练习侧题型（与 ``practice.APP_PRACTICE_TYPE_MAP`` 同口径）。
_PRACTICE_TYPE_MAP = {
    "SINGLE": "single_choice",
    "MULTIPLE": "multi_choice",
    "BLANK": "fill_blank",
    "SUBJECTIVE": "short_answer",
    "MIXED": "material_group",
}
_PRACTICE_TYPE_LABELS = {
    "single_choice": "单选题",
    "multi_choice": "多选题",
    "fill_blank": "填空题",
    "short_answer": "简答题",
    "material_group": "材料题",
}
#: 题库题型 → 工坊题型（``/api/questions?source=official&type=`` 同时接受两种口径）。
_BANK_TYPE_TO_PAPERLAB = {
    "single_choice": "SINGLE",
    "multi_choice": "MULTIPLE",
    "fill_blank": "BLANK",
    "calculation": "SUBJECTIVE",
    "short_answer": "SUBJECTIVE",
    "sub_short_answer": "SUBJECTIVE",
    "essay": "SUBJECTIVE",
    "material_group": "MIXED",
    "unknown": "SUBJECTIVE",
}
_REVIEW_STATUS_LABELS = {
    "community": "社区待核验",
    "verified": "社区核验通过",
    "featured": "平台精选",
    "withdrawn": "已撤回",
    "disputed": "有争议或待修订",
}

# public_id 由广场生成（``plz-`` + hex），但测试/历史数据可能使用其它安全字符；
# 这里只做「可安全放进 URL 与引用」的宽松校验，非法值按不存在处理（404，不用 422 泄露格式）。
_PUBLIC_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,119}$")


# ---------------------------------------------------------------------------
# 广场模块（延迟导入：practice 先于 plaza 初始化，避免模块级循环依赖）
# ---------------------------------------------------------------------------

def _plaza_modules():
    from .plaza import db as plaza_db
    from .plaza import figures, service
    return plaza_db, service, figures


def _plaza_db():
    plaza_db, _service, _figures = _plaza_modules()
    return plaza_db


def ensure_tables() -> None:
    """确保广场表存在（official 读取可能发生在广场接口首次调用之前）。"""
    _plaza_db().ensure_tables()


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------

def _row_dict(row):
    if row is None:
        return None
    try:
        return dict(row)
    except Exception:  # noqa: BLE001 - 行对象不可解析按不存在处理
        return None


def normalize_public_id(value) -> str:
    """规范化/校验公开题号；非法返回空串（调用方按不存在处理）。"""
    text = str(value or "").strip()
    if not text or not _PUBLIC_ID_RE.match(text):
        return ""
    return text


def ref_of(public_id) -> str:
    return f"{SOURCE}:{public_id}"


def _split_csv(value) -> list[str]:
    if not value:
        return []
    return [part.strip() for part in str(value).split(",") if part.strip()]


def _like_pattern(keyword: str) -> str:
    escaped = keyword.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _normalize_type_filter(value) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    from .paperlab import schema as paperlab_schema

    normalized = paperlab_schema.normalize_qtype(text)
    if normalized:
        return normalized
    return _BANK_TYPE_TO_PAPERLAB.get(text.lower())


def _active_release(con, row: dict) -> dict | None:
    """当前 active 发布指针（``published`` 状态只有在签发指针仍 active 时公开可读）。"""
    public_id = str(row.get("public_id") or "")
    if not public_id:
        return None
    found = con.execute(
        "SELECT * FROM plaza_releases WHERE submission_id=? AND status='active' "
        "AND public_id=? ORDER BY id DESC LIMIT 1",
        (int(row["id"]), public_id),
    ).fetchone()
    return _row_dict(found)


def _is_visible(con, row: dict | None, plaza_db) -> bool:
    if not row:
        return False
    status = str(row.get("status") or "")
    if status not in plaza_db.PUBLIC_STATUSES:
        return False
    if not str(row.get("public_id") or "").strip():
        return False
    if status == "published":
        return _active_release(con, row) is not None
    return True


def _status_notice(status: str) -> str:
    return STATUS_NOTICES.get(str(status or ""), NOTICE_UNAVAILABLE)


def review_status_label(review_status: str) -> str:
    return _REVIEW_STATUS_LABELS.get(str(review_status or ""), "")


# ---------------------------------------------------------------------------
# 公开题投影（与 practice.load_question 同形）
# ---------------------------------------------------------------------------

def _public_images(snapshot_content: dict, public_content: dict) -> list[dict]:
    """公开配图列表：只输出公开衍生图直链（可撤回），不可公开的图只留原因。

    与广场公开详情一致：``content.images`` 的序号与快照配图序号一一对应，
    撤回/争议后 ``/api/plaza/questions/{public_id}/images/{index}`` 立即 404。
    """
    raw_images = snapshot_content.get("images")
    raw_images = raw_images if isinstance(raw_images, list) else []
    public_images = {
        int(entry.get("index")): entry
        for entry in (public_content.get("images") or [])
        if isinstance(entry, dict) and entry.get("index") is not None
    }
    images: list[dict] = []
    for index, raw in enumerate(raw_images):
        if not isinstance(raw, dict):
            continue
        mapped = public_images.get(index) or {}
        accessible = bool(mapped.get("accessible"))
        url = ""
        if accessible:
            url = str(mapped.get("question_image_url") or mapped.get("url") or "")
        images.append({
            "index": index,
            "role": str(raw.get("role") or "figure"),
            "alt": str(raw.get("alt") or ""),
            "accessible": accessible and bool(url),
            "url": url,
            "asset": str(mapped.get("asset_id") or ""),
            "media_type": str(mapped.get("media_type") or ""),
            "reason": "" if accessible else str(mapped.get("reason") or "unavailable"),
        })
    return images


def _project(con, row: dict, plaza_db) -> dict:
    """把广场投稿行投影成与 ``practice.load_question`` 同形的题字典。"""
    _db, service, figures = _plaza_modules()
    public_id = str(row.get("public_id") or "")
    snapshot = plaza_db.json_load(row.get("snapshot_json"), {})
    if not isinstance(snapshot, dict):
        snapshot = {}
    source_declaration = plaza_db.json_load(row.get("source_json"), {})
    if not isinstance(source_declaration, dict):
        source_declaration = {}
    image_map = figures.public_figure_map(con, row)
    content = service.public_content(
        snapshot.get("content") or {}, image_map=image_map, public_id=public_id,
    )
    status = str(row.get("status") or "")
    release = _active_release(con, row)
    published_version = int(
        (release or {}).get("version") or row.get("current_version") or row.get("version") or 1
    )
    raw_type = str(snapshot.get("type") or "SUBJECTIVE").upper()
    practice_type = _PRACTICE_TYPE_MAP.get(raw_type, "short_answer")
    options = [
        {"label": str(option.get("label") or ""), "text": str(option.get("text") or "")}
        for option in (content.get("options") or []) if isinstance(option, dict)
    ]
    answers = [str(answer) for answer in (content.get("answers") or []) if str(answer).strip()]
    parts = [
        part for part in (content.get("parts") or []) if isinstance(part, dict)
    ]
    part_answers = [
        str(answer) for part in parts
        for answer in (part.get("answers") or []) if str(answer).strip()
    ]
    stem = str(content.get("stem") or "")
    explanation = str(content.get("explanation") or "")
    rubric = str(content.get("rubric") or "")
    images = _public_images(
        snapshot.get("content") if isinstance(snapshot.get("content"), dict) else {},
        content,
    )
    objective = raw_type in ("SINGLE", "MULTIPLE") and bool(options)
    auto_gradeable = (objective or raw_type == "BLANK") and bool(answers)
    review_status = plaza_db.public_review_status(status)
    answer_text = "；".join(answers) if answers else "；".join(part_answers)
    qid = ref_of(public_id)
    content_revision = str(published_version)
    practice_status = "practice"
    grading_status = "auto" if auto_gradeable else "pending"
    knowledge_tags = [
        str(tag) for tag in (snapshot.get("knowledge_tags") or []) if str(tag).strip()
    ]
    return {
        "qid": qid,
        "ref": qid,
        "source": SOURCE,
        "source_label": SOURCE_LABEL,
        "subject": str(snapshot.get("subject") or row.get("subject") or ""),
        "grade": str(snapshot.get("grade") or ""),
        "year": None,
        "province": "",
        "paper_name": "",
        "paper_type": "official",
        "type": practice_type,
        "type_label": _PRACTICE_TYPE_LABELS.get(practice_type, practice_type),
        "number_label": None,
        "group_id": None,
        "group_role": None,
        "stem_plain": stem,
        "stem_blocks": [{"kind": "text", "text": stem}] if stem else [],
        "options": options,
        "options_blocks": [],
        "images": images,
        "objective": objective,
        "has_answer": bool(answers or part_answers),
        "has_solution": bool(explanation.strip() or rubric.strip()
                             or any(str(part.get("explanation") or "").strip() for part in parts)),
        "quality_flags": [],
        "grading_flags": [],
        "data_quality_flags": [],
        "practice_allowed": True,
        "quarantined": False,
        "quarantine_reason": None,
        "practice_status": practice_status,
        "grading_status": grading_status,
        "bank_version": "",
        "content_revision": content_revision,
        "answer_revision": content_revision,
        "parent_source_question_id": None,
        "group_kind": None,
        "shared_blocks": [],
        "member_index": [],
        "group": None,
        "answer": {
            "text": answer_text,
            "kind": "reference",
            "note": rubric or None,
            "solution": explanation,
        },
        "answer_text": answer_text,
        "solution": explanation,
        "answer_blocks": [],
        "solution_blocks": [],
        "knowledge_tags": knowledge_tags,
        "parts": parts,
        # 内部字段：不进 ``_public_question`` 白名单，仅供作答判分复用个人题的判分工具。
        "app_content": {
            "answers": answers,
            "options": options,
            "explanation": explanation,
            "rubric": rubric,
        },
        "app_type": raw_type,
        # 平台精选公开元数据：详情/列表/错题卡显示来源、审核状态与公开题号。
        "public_id": public_id,
        "official_status": status,
        "status_label": plaza_db.STATUS_LABELS.get(status, status),
        "review_status": review_status,
        "review_status_label": review_status_label(review_status),
        "published_version": published_version,
        "published_at": str((release or {}).get("published_at") or ""),
        "featured": status == "published",
        "community_pending": status == "community_review",
        "notice": _status_notice(status),
        "source_declaration": service.public_source(source_declaration),
        "figure_notice": figures.figure_notice_from_map(image_map),
        "difficulty": snapshot.get("difficulty"),
        "difficulty_source": "平台精选",
        "ai_difficulty": None,
        "ai_difficulty_score": None,
        "stat_accuracy": None,
        "stat_attempts": 0,
        "unavailable": False,
        "withdrawn": False,
    }


# ---------------------------------------------------------------------------
# 读取接口
# ---------------------------------------------------------------------------

def load_official_question(public_id) -> dict:
    """按公开题号读取平台精选题；不存在或当前不可见统一 404。"""
    normalized = normalize_public_id(public_id)
    if not normalized:
        raise HTTPException(404, "未找到该平台精选题目")
    ensure_tables()
    plaza_db, _service, _figures = _plaza_modules()
    with connection() as con:
        row = _row_dict(con.execute(
            "SELECT * FROM plaza_submissions WHERE public_id=?", (normalized,)
        ).fetchone())
        if row is None or not _is_visible(con, row, plaza_db):
            raise HTTPException(404, "该平台精选题目不存在或已不可见")
        return _project(con, row, plaza_db)


def inspect_official(public_id) -> dict:
    """可见性摘要（不回显正文）：供错题卡/复习队列在撤回后保留历史记录。

    返回 ``{public_id, exists, visible, status, status_label, review_status,
    review_status_label, featured, community_pending, notice}``。
    """
    normalized = normalize_public_id(public_id)
    info = {
        "public_id": normalized or str(public_id or ""),
        "exists": False,
        "visible": False,
        "status": "",
        "status_label": "",
        "review_status": "",
        "review_status_label": "",
        "featured": False,
        "community_pending": False,
        "notice": NOTICE_UNAVAILABLE,
    }
    if not normalized:
        return info
    plaza_db, _service, _figures = _plaza_modules()
    try:
        ensure_tables()
        with connection() as con:
            row = _row_dict(con.execute(
                "SELECT * FROM plaza_submissions WHERE public_id=?", (normalized,)
            ).fetchone())
            if row is None:
                return info
            status = str(row.get("status") or "")
            review_status = plaza_db.public_review_status(status)
            info.update({
                "exists": True,
                "status": status,
                "status_label": plaza_db.STATUS_LABELS.get(status, status),
                "featured": status == "published",
                "community_pending": status == "community_review",
                "notice": _status_notice(status),
            })
            if _is_visible(con, row, plaza_db):
                info["visible"] = True
                info["review_status"] = review_status
            elif status == "withdrawn":
                info["review_status"] = "withdrawn"
                info["notice"] = NOTICE_WITHDRAWN
            elif status == "disputed":
                info["review_status"] = "disputed"
                info["notice"] = NOTICE_DISPUTED
            info["review_status_label"] = review_status_label(info["review_status"])
            return info
    except HTTPException:
        raise
    except Exception:  # noqa: BLE001 - 广场表不可读时按不可见处理，不阻断错题/复习列表
        _log_fallback_failure()
        return info


def _log_fallback_failure() -> None:
    try:
        import logging

        logging.getLogger("zhixu.official").warning(
            "平台精选可见性检查失败，按不可见降级", exc_info=True,
        )
    except Exception:  # noqa: BLE001 - 日志失败不影响主流程
        pass


def search_official(keyword=None, subject=None, type=None, limit: int = 20,
                    offset: int = 0) -> dict:
    """公开题列表 + total（LIKE 快照 JSON；学科/题型/关键词过滤与可见性同查询时判定）。

    ``type`` 同时接受工坊题型（``SINGLE`` 等）与练习侧题型（``single_choice`` 等）。
    """
    plaza_db, _service, _figures = _plaza_modules()
    ensure_tables()
    try:
        limit = max(0, min(int(limit or 0), 100))
    except (TypeError, ValueError):
        limit = 20
    try:
        offset = max(0, int(offset or 0))
    except (TypeError, ValueError):
        offset = 0
    where: list[str] = []
    params: list = []
    marks = ",".join("?" for _ in plaza_db.PUBLIC_STATUSES)
    where.append(f"status IN ({marks})")
    params.extend(plaza_db.PUBLIC_STATUSES)
    where.append("public_id IS NOT NULL AND public_id <> ''")
    # published 只有在 active 发布指针仍存在时公开可读（签发后撤回立即从检索退出）。
    where.append(
        "(status <> 'published' OR EXISTS (SELECT 1 FROM plaza_releases r "
        "WHERE r.submission_id = plaza_submissions.id AND r.status='active' "
        "AND r.public_id = plaza_submissions.public_id))"
    )
    subjects = _split_csv(subject)
    if subjects:
        where.append("subject IN (" + ",".join("?" for _ in subjects) + ")")
        params.extend(subjects)
    raw_types = _split_csv(type)
    if raw_types:
        normalized_types: list[str] = []
        for raw in raw_types:
            normalized = _normalize_type_filter(raw)
            if normalized is None:
                raise HTTPException(422, "未知的题型筛选")
            if normalized not in normalized_types:
                normalized_types.append(normalized)
        where.append("type IN (" + ",".join("?" for _ in normalized_types) + ")")
        params.extend(normalized_types)
    if keyword and str(keyword).strip():
        where.append("snapshot_json LIKE ? ESCAPE '\\'")
        params.append(_like_pattern(str(keyword).strip()[:120]))
    clause = " AND ".join(where)
    order = (
        "ORDER BY CASE status WHEN 'published' THEN 0 WHEN 'verified' THEN 1 ELSE 2 END, "
        "updated_at DESC, id DESC"
    )
    with connection() as con:
        total = int(con.execute(
            f"SELECT COUNT(*) FROM plaza_submissions WHERE {clause}", tuple(params)
        ).fetchone()[0])
        items: list[dict] = []
        if limit:
            rows = con.execute(
                f"SELECT * FROM plaza_submissions WHERE {clause} {order} LIMIT ? OFFSET ?",
                (*params, limit, offset),
            ).fetchall()
            items = [
                _project(con, row_dict, plaza_db)
                for row_dict in (_row_dict(row) for row in rows)
                if row_dict is not None
            ]
    return {"items": items, "total": total}


__all__ = [
    "NOTICE_COMMUNITY_REVIEW",
    "NOTICE_DISPUTED",
    "NOTICE_PUBLISHED",
    "NOTICE_UNAVAILABLE",
    "NOTICE_VERIFIED",
    "NOTICE_WITHDRAWN",
    "SOURCE",
    "SOURCE_LABEL",
    "STATUS_NOTICES",
    "ensure_tables",
    "inspect_official",
    "load_official_question",
    "normalize_public_id",
    "ref_of",
    "review_status_label",
    "search_official",
]
