"""广场公开只读接口（匿名可访问，可选登录）。

公开可见性（方案 §5「投稿到官方发布」状态表）：

* ``community_review``（初审通过、社区核验进行中）：进入公开列表/详情，但显著标注
  ``review_status=community``/「社区待核验」，并附评审进度摘要；
* ``verified``：社区核验通过（``review_status=verified``）；
* ``published``：平台精选（``review_status=featured``）；
* ``disputed``：不公开结论（``visible=false`` + 暂停展示原因），列表不可见；
* ``withdrawn``：旧链接保留清晰撤回状态，不回显任何受限正文，列表不可见。

匿名读图只服务「当前 active release 或社区待核验快照引用 + 公开衍生资产」，
白名单内容类型内联返回；其余 404/415，不泄露存储路径、他人私有资产或评审人身份。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from ..auth import optional_user
from ..db import connection
from . import db, favorites, figures, service

router = APIRouter()

MAX_PAGE_SIZE = 50


def _escape_like(keyword: str) -> str:
    escaped = keyword.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


@router.get("/questions")
def list_public_questions(
    subject: str | None = Query(default=None, max_length=40),
    type: str | None = Query(default=None, max_length=20),
    qtype: str | None = Query(default=None, max_length=20),
    keyword: str | None = Query(default=None, max_length=120),
    source: str | None = Query(default=None, pattern="^(community|featured)$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=MAX_PAGE_SIZE),
    user=Depends(optional_user),
):
    """公开题列表：社区待核验/已核验/精选可见，支持学科/题型/关键词/来源与分页。"""
    db.ensure_tables()
    placeholders = ", ".join("?" for _ in db.PUBLIC_STATUSES)
    where = [f"status IN ({placeholders})", "public_id IS NOT NULL"]
    params: list = list(db.PUBLIC_STATUSES)
    if subject and subject.strip():
        where.append("subject=?")
        params.append(subject.strip())
    raw_type = (type or qtype or "").strip()
    if raw_type:
        from ..paperlab import schema as paperlab_schema
        normalized = paperlab_schema.normalize_qtype(raw_type)
        if normalized is None:
            raise HTTPException(422, "未知的题型筛选")
        where.append("type=?")
        params.append(normalized)
    if keyword and keyword.strip():
        pattern = _escape_like(keyword.strip())
        # 题干与知识点都在不可变快照 JSON 内；转义后 LIKE 在 SQLite/PostgreSQL 同语义。
        where.append("snapshot_json LIKE ? ESCAPE '\\'")
        params.append(pattern)
    if source:
        statuses = db.PUBLIC_SOURCES.get(source, ())
        source_placeholders = ", ".join("?" for _ in statuses)
        where.append(f"status IN ({source_placeholders})")
        params.extend(statuses)
    clause = " AND ".join(where)
    with connection() as con:
        total = int(con.execute(
            f"SELECT COUNT(*) FROM plaza_submissions WHERE {clause}", tuple(params)
        ).fetchone()[0])
        rows = con.execute(
            f"SELECT * FROM plaza_submissions WHERE {clause} "
            "ORDER BY CASE status WHEN 'published' THEN 0 WHEN 'verified' THEN 1 ELSE 2 END, "
            "updated_at DESC, id DESC LIMIT ? OFFSET ?",
            (*params, page_size, (page - 1) * page_size),
        ).fetchall()
        items = [service.public_list_item(con, dict(row)) for row in rows]
    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@router.get("/questions/{public_id}")
def get_public_question(public_id: str, user=Depends(optional_user)):
    """公开题详情：题面/选项/答案/解析/知识点/公开图/来源声明/审核状态与进度。"""
    db.ensure_tables()
    with connection() as con:
        row = db.load_submission_by_public_id(con, public_id)
        if row is None:
            raise HTTPException(404, "未找到该公开题目")
        status = str(row.get("status") or "")
        if status in db.PUBLIC_STATUSES:
            payload = service.public_payload(con, row)
        elif status == "withdrawn":
            # 撤回后旧链接保留清晰状态，但不回显任何受限正文。
            payload = {
                "public_id": str(row.get("public_id") or public_id),
                "status": "withdrawn",
                "status_label": db.STATUS_LABELS["withdrawn"],
                "review_status": "withdrawn",
                "visible": False,
                "featured": False,
                "notice": service.withdrawn_notice(),
                "reason": service.withdrawn_notice(),
            }
        elif status == "disputed":
            # 有争议/待修订：不公开结论，只说明暂停展示的原因口径。
            notice = service.disputed_notice()
            payload = {
                "public_id": str(row.get("public_id") or public_id),
                "status": "disputed",
                "status_label": db.STATUS_LABELS["disputed"],
                "review_status": "disputed",
                "visible": False,
                "featured": False,
                "notice": notice,
                "reason": notice,
            }
        else:
            raise HTTPException(404, "未找到该公开题目")
        # 当前登录用户的收藏状态（匿名 false）与公开收藏总数（聚合数，不含身份）。
        viewer_id = int(user["id"]) if user else 0
        payload["favorited"] = bool(
            viewer_id and favorites.is_favorited(con, viewer_id, int(row["id"]))
        )
        payload["favorite_count"] = favorites.favorite_count(con, int(row["id"]))
    payload["visible"] = bool(payload.get("visible"))
    return payload


# ---------------------------------------------------------------------------
# 匿名可读图（仅公开衍生图；白名单内容类型内联）
# ---------------------------------------------------------------------------

@router.get("/assets/{asset_id}")
def get_public_asset(asset_id: str):
    """公开衍生图直链：仅服务被当前公开稿件当前版本引用的公开副本。"""
    db.ensure_tables()
    with connection() as con:
        row = figures.authorize_public_asset(con, asset_id)
    if row is None:
        # 统一 404：不泄露资产是否存在、是否为他人私有资产或存储位置。
        raise HTTPException(404, "配图不可公开访问")
    return figures.serve_public_image(row)


@router.get("/questions/{public_id}/images/{index}")
def get_public_question_image(public_id: str, index: int):
    """按「公开题号 + 配图序号」读取公开衍生图；撤回/争议/私有配图一律 404。"""
    db.ensure_tables()
    with connection() as con:
        row = figures.authorize_question_image(con, public_id, index)
    if row is None:
        raise HTTPException(404, "配图不可公开访问")
    return figures.serve_public_image(row)
