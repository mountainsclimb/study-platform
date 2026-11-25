"""试卷工坊：应用内可写题库 ``pl_questions`` 的检索 / CRUD / 版本 CAS / 核验。

与只读高考题库并存（契约 §4.2）：本模块只写应用题库，不触碰 ``practice`` 的只读库。
检索关键词用 LIKE 过滤 ``content_json``（题干摘要等）与知识点 JSON，不依赖 FTS5。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from .. import storage
from ..auth import current_user
from ..db import connection, utcnow, write_lock
from . import assets, db, schema, versions
from .db import (
    ASSET_FOLDER,
    MAX_PAGE_SIZE,
    MAX_TAGS,
    QUESTION_STATUSES,
    VERIFY_STATUSES,
    json_dump,
    json_load,
)

router = APIRouter()


# ---------------------------------------------------------------------------
# meta
# ---------------------------------------------------------------------------

def _meta_payload() -> dict:
    return {
        "enabled": True,
        "subjects": list(db.SUBJECTS),
        "grades": list(db.GRADES),
        "qtypes": [{"value": value, "label": label} for value, label in db.Q_TYPES.items()],
        "difficulties": [
            {"value": 0.2, "label": "简单"},
            {"value": 0.5, "label": "中等"},
            {"value": 0.8, "label": "困难"},
        ],
        "limits": {
            "upload_mb": db.MAX_FILE_BYTES // (1024 * 1024),
            "import_pages_max": db.MAX_IMPORT_PAGES,
            "import_files_max": db.MAX_IMPORT_FILES,
            "import_items_max": db.MAX_IMPORT_ITEMS,
            "enrich_questions_max": db.MAX_ENRICH_QUESTIONS,
            "page_size_max": MAX_PAGE_SIZE,
        },
        "capabilities": {
            "vision": True,
            "pdf": True,
            "docx": True,
            "ai_enrich": True,
            "papers": True,
            "answer_sheet": True,
        },
        "prompt_version": _prompt_version(),
    }


def _prompt_version() -> str:
    from .prompts import PROMPT_VERSION
    return PROMPT_VERSION


@router.get("/meta")
def paperlab_meta(user=Depends(current_user)):
    return _meta_payload()


# ---------------------------------------------------------------------------
# 题库查询
# ---------------------------------------------------------------------------

def _like_pattern(keyword: str) -> str:
    escaped = (
        keyword.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    )
    return f"%{escaped}%"


@router.get("/questions")
def list_questions(
    subject: str | None = Query(default=None, max_length=40),
    grade: str | None = Query(default=None, max_length=40),
    qtype: str | None = Query(default=None, max_length=20),
    type: str | None = Query(default=None, max_length=20),
    keyword: str | None = Query(default=None, max_length=120),
    status: str | None = Query(default=None, max_length=20),
    verify_status: str | None = Query(default=None, max_length=20),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=MAX_PAGE_SIZE),
    user=Depends(current_user),
):
    where = ["user_id=?", "deleted_at IS NULL"]
    params: list = [user["id"]]
    if subject:
        where.append("subject=?")
        params.append(subject.strip())
    if grade:
        where.append("grade=?")
        params.append(grade.strip())
    raw_type = (qtype or type or "").strip()
    if raw_type:
        normalized = schema.normalize_qtype(raw_type)
        if normalized is None:
            raise HTTPException(422, "未知的题型筛选")
        where.append("type=?")
        params.append(normalized)
    if keyword and keyword.strip():
        pattern = _like_pattern(keyword.strip())
        where.append("(content_json LIKE ? ESCAPE '\\' OR knowledge_tags_json LIKE ? ESCAPE '\\')")
        params.extend([pattern, pattern])
    if status:
        if status not in QUESTION_STATUSES:
            raise HTTPException(422, "未知的题目状态")
        where.append("status=?")
        params.append(status)
    if verify_status:
        if verify_status not in VERIFY_STATUSES:
            raise HTTPException(422, "未知的核验状态")
        where.append("verify_status=?")
        params.append(verify_status)
    clause = " AND ".join(where)
    with connection() as con:
        total = int(con.execute(
            f"SELECT COUNT(*) FROM pl_questions WHERE {clause}", tuple(params)
        ).fetchone()[0])
        rows = con.execute(
            f"SELECT * FROM pl_questions WHERE {clause} ORDER BY updated_at DESC, id DESC "
            "LIMIT ? OFFSET ?",
            (*params, page_size, (page - 1) * page_size),
        ).fetchall()
    items = [db.public_question(row) for row in rows]
    # 列表批量标注「有新版」/来源可用性（一次只读探针，不做逐题请求；versions.py 实现）。
    versions.annotate_source_flags(items)
    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# ---------------------------------------------------------------------------
# 新建 / 详情 / 修改 / 删除
# ---------------------------------------------------------------------------

class QuestionCreateInput(BaseModel):
    type: str = Field(default="SUBJECTIVE", max_length=20)
    subject: str = Field(min_length=1, max_length=40)
    grade: str = Field(default="", max_length=40)
    difficulty: float | None = Field(default=None, ge=0, le=1)
    knowledge_tags: list[str] = Field(default_factory=list, max_length=MAX_TAGS)
    content: dict
    status: str = Field(default="draft", max_length=20)


def _normalize_content(content: dict, qtype: str) -> dict:
    try:
        return schema.normalize_question_content(content, qtype)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None


def _clean_tags(raw) -> list[str]:
    return schema.normalize_tags(raw, limit=MAX_TAGS)


@router.post("/questions")
def create_question(data: QuestionCreateInput, user=Depends(current_user)):
    qtype = schema.normalize_qtype(data.type)
    if qtype is None:
        raise HTTPException(422, "未知的题型")
    if data.status not in QUESTION_STATUSES:
        raise HTTPException(422, "未知的题目状态")
    content = _normalize_content(data.content, qtype)
    # P0：配图引用必须可归属（本人签发的 asset / 已迁移的旧文件名），拒绝他人资产。
    assets.validate_content_image_refs(user["id"], content)
    tags = _clean_tags(data.knowledge_tags)
    digest = versions.content_sha256(content)
    now = utcnow()
    with connection() as con:
        write_lock(con, "paperlab-question")
        question_id = int(con.execute(
            "INSERT INTO pl_questions(user_id,source,type,subject,grade,difficulty,"
            "knowledge_tags_json,content_json,status,verify_status,version,ai_job_id,"
            "ai_model,prompt_version,source_kind,content_sha256,created_at,updated_at,deleted_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,NULL,'','','manual',?,?,?,NULL)",
            (
                user["id"], "manual", qtype, data.subject.strip(),
                (data.grade or "").strip(), data.difficulty,
                json_dump(tags), json_dump(content),
                data.status, "pending", 1, digest, now, now,
            ),
        ).lastrowid)
        # Phase 1 剩余：创建即固定版本 1（不可变内容版本，供编辑/收录历史对照）。
        fresh = con.execute(
            "SELECT * FROM pl_questions WHERE id=?", (question_id,),
        ).fetchone()
        if fresh is not None:
            versions.record_version(
                con, versions.snapshot_from_row(dict(fresh)), version=1, origin="create",
                created_at=now,
            )
    question = db.public_question(db.fetch_question(question_id, user["id"]))
    versions.annotate_source_flags([question])
    return {"question": question}


@router.get("/questions/{question_id}")
def get_question(question_id: int, user=Depends(current_user)):
    question = db.public_question(db.fetch_question(question_id, user["id"]))
    versions.annotate_source_flags([question])
    return {"question": question}


class QuestionPatchInput(BaseModel):
    version: int = Field(ge=1)
    type: str | None = Field(default=None, max_length=20)
    subject: str | None = Field(default=None, min_length=1, max_length=40)
    grade: str | None = Field(default=None, max_length=40)
    difficulty: float | None = Field(default=None, ge=0, le=1)
    knowledge_tags: list[str] | None = Field(default=None, max_length=MAX_TAGS)
    content: dict | None = None
    status: str | None = Field(default=None, max_length=20)


@router.patch("/questions/{question_id}")
def patch_question(question_id: int, data: QuestionPatchInput, user=Depends(current_user)):
    """修改题目；``version`` 做乐观锁 CAS，冲突返回 409 而不是静默覆盖。

    Phase 1 剩余：每次修改成功后追加一条不可变版本行（``origin='edit'``）；
    修改前若历史缺失（老数据/导入旁路写入），先补一条当前快照，保证旧内容可追溯。
    """
    with connection() as con:
        write_lock(con, "paperlab-question")
        row = con.execute(
            "SELECT * FROM pl_questions WHERE id=? AND user_id=? AND deleted_at IS NULL",
            (question_id, user["id"]),
        ).fetchone()
        if row is None:
            raise HTTPException(404, "未找到该题目")
        current_version = int(row["version"] or 1)
        if int(data.version) != current_version:
            raise HTTPException(409, "题目已被修改（版本冲突），请刷新后重试")
        versions.ensure_history_in_tx(con, dict(row))
        sets: list[str] = []
        params: list = []
        qtype = row["type"] or "SUBJECTIVE"
        if data.type is not None:
            normalized = schema.normalize_qtype(data.type)
            if normalized is None:
                raise HTTPException(422, "未知的题型")
            qtype = normalized
            sets.append("type=?")
            params.append(normalized)
        existing_content = json_load(row["content_json"], {})
        if not isinstance(existing_content, dict):
            existing_content = {}
        if data.content is not None:
            merged = dict(existing_content)
            merged.update(data.content)
        else:
            merged = existing_content
        final_content = existing_content
        if data.content is not None or data.type is not None:
            normalized_content = _normalize_content(merged, qtype)
            assets.validate_content_image_refs(
                user["id"], normalized_content, existing_content=existing_content,
            )
            sets.append("content_json=?")
            params.append(json_dump(normalized_content))
            final_content = normalized_content
        if data.subject is not None:
            sets.append("subject=?")
            params.append(data.subject.strip())
        if data.grade is not None:
            sets.append("grade=?")
            params.append((data.grade or "").strip())
        if data.difficulty is not None:
            sets.append("difficulty=?")
            params.append(data.difficulty)
        if data.knowledge_tags is not None:
            sets.append("knowledge_tags_json=?")
            params.append(json_dump(_clean_tags(data.knowledge_tags)))
        if data.status is not None:
            if data.status not in QUESTION_STATUSES:
                raise HTTPException(422, "未知的题目状态")
            sets.append("status=?")
            params.append(data.status)
        if not sets:
            raise HTTPException(422, "没有需要修改的字段")
        # 内容指纹始终跟随最终内容（即使本次只改标签/状态，也保持列与内容一致）。
        sets.append("content_sha256=?")
        params.append(versions.content_sha256(final_content))
        sets.append("version=?")
        params.append(current_version + 1)
        sets.append("updated_at=?")
        params.append(utcnow())
        params.extend([question_id, user["id"], current_version])
        changed = con.execute(
            "UPDATE pl_questions SET " + ", ".join(sets) +
            " WHERE id=? AND user_id=? AND version=? AND deleted_at IS NULL",
            tuple(params),
        ).rowcount
        if not changed:
            raise HTTPException(409, "题目已被修改（版本冲突），请刷新后重试")
        # Phase 1 剩余：修改成功后追加不可变版本（旧版本行保持不变）。
        after = con.execute(
            "SELECT * FROM pl_questions WHERE id=?", (question_id,),
        ).fetchone()
        if after is not None:
            versions.record_version(
                con, versions.snapshot_from_row(dict(after)),
                version=current_version + 1, origin="edit",
            )
    question = db.public_question(db.fetch_question(question_id, user["id"]))
    versions.annotate_source_flags([question])
    return {"question": question}


@router.delete("/questions/{question_id}")
def delete_question(question_id: int, user=Depends(current_user)):
    with connection() as con:
        write_lock(con, "paperlab-question")
        row = con.execute(
            "SELECT id FROM pl_questions WHERE id=? AND user_id=? AND deleted_at IS NULL",
            (question_id, user["id"]),
        ).fetchone()
        if row is None:
            raise HTTPException(404, "未找到该题目")
        con.execute(
            "UPDATE pl_questions SET deleted_at=?, status='archived', version=version+1, "
            "updated_at=? WHERE id=? AND user_id=?",
            (utcnow(), utcnow(), question_id, user["id"]),
        )
    return {"deleted": True, "question_id": question_id}


# ---------------------------------------------------------------------------
# 核验（verified 后 AI 补全不得覆盖）
# ---------------------------------------------------------------------------

_VERIFY_ALIASES = {
    "verified": "verified", "ready": "verified", "核准": "verified", "通过": "verified",
    "rejected": "rejected", "驳回": "rejected", "拒绝": "rejected",
    "pending": "pending", "draft": "pending", "待核验": "pending", "取消核验": "pending",
}


class VerifyInput(BaseModel):
    status: str = Field(max_length=20)


def _content_has_answer(content: dict) -> bool:
    answers = content.get("answers")
    if isinstance(answers, list) and any(str(item).strip() for item in answers):
        return True
    parts = content.get("parts")
    if isinstance(parts, list):
        for part in parts:
            part_answers = part.get("answers") if isinstance(part, dict) else None
            if isinstance(part_answers, list) and any(str(item).strip() for item in part_answers):
                return True
    return False


@router.post("/questions/{question_id}/verify")
def verify_question(question_id: int, data: VerifyInput, user=Depends(current_user)):
    verify_status = _VERIFY_ALIASES.get((data.status or "").strip())
    if verify_status is None:
        raise HTTPException(422, "核验状态只能是 verified / rejected / pending")
    question = db.fetch_question(question_id, user["id"])
    content = json_load(question.get("content_json"), {})
    if not isinstance(content, dict):
        content = {}
    if verify_status == "verified" and not _content_has_answer(content):
        raise HTTPException(422, "题目还没有答案，请先补全或手工填写后再核验")
    status = {"verified": "ready", "rejected": "draft", "pending": "draft"}[verify_status]
    with connection() as con:
        write_lock(con, "paperlab-question")
        changed = con.execute(
            "UPDATE pl_questions SET verify_status=?, status=?, version=version+1, updated_at=? "
            "WHERE id=? AND user_id=? AND deleted_at IS NULL",
            (verify_status, status, utcnow(), question_id, user["id"]),
        ).rowcount
    if not changed:
        raise HTTPException(404, "未找到该题目")
    return {"question": db.public_question(db.fetch_question(question_id, user["id"]))}


# ---------------------------------------------------------------------------
# 题目配图（识别时按 bbox 裁切，存 paperlab/assets）
# ---------------------------------------------------------------------------

@router.get("/questions/{question_id}/images/{image_index}")
def question_image(question_id: int, image_index: int, user=Depends(current_user)):
    """题目配图文件流（P0：独立的所有权校验）。

    同时校验：题目属于当前用户且未删除（``fetch_question`` 的 404）、配图引用来自题目
    当前 content、资产管理行存在且可用、资产属于当前用户（或已公开的平台精选衍生图）。
    任何一项不满足统一 404，不泄露他人资产是否存在性。
    """
    question = db.fetch_question(question_id, user["id"])
    content = json_load(question.get("content_json"), {})
    images = content.get("images") if isinstance(content, dict) else None
    if not isinstance(images, list) or image_index < 0 or image_index >= len(images):
        raise HTTPException(404, "该题没有这张配图")
    image = images[image_index] if isinstance(images[image_index], dict) else {}
    asset_ref = str(image.get("asset") or "").strip()
    if not asset_ref:
        raise HTTPException(404, "该题配图尚未裁切")
    asset = assets.resolve_asset_for_read(asset_ref, user["id"])
    if not asset:
        raise HTTPException(404, "该题配图不可用")
    return storage.file_response(
        str(asset.get("folder") or ASSET_FOLDER),
        str(asset.get("storage_ref") or ""),
        media_type=str(asset.get("media_type") or "image/png"),
        headers={"Cache-Control": "private, max-age=3600"},
    )


# 个人收录版本与「有新版」（§6.2/§6.3，已实现于 versions.py）。
# 路由前缀与 bank 相同（都挂在 /api/paperlab 下），路径均为 /questions/{id}/... 的具体子路径，
# 与上面 GET /questions/{id} 不冲突；放在文件末尾注册，保持既有路由匹配顺序不变。
router.include_router(versions.router)

