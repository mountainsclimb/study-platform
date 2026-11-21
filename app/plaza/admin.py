"""广场运营后台接口：编辑台投稿队列、身份认证审核队列与审核动作。

权限（方案 §5「页面和权限」）：仅平台编辑（``plaza_roles.role='editor'``）与系统管理员；
普通评审员、作者与其他登录用户一律 403。列表对操作者可见作者/申请人账号用于处理，
但公开接口不返回任何内部 ``user_id``/用户名/评审人身份；本模块也不返回证据存储路径。
"""
from __future__ import annotations

import hashlib

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from ..db import connection, utcnow, write_lock
from . import db, service

router = APIRouter()

MAX_PAGE_SIZE = 100

#: 编辑台默认待处理状态（初审、社区核验、待签发、发布中）。
DEFAULT_QUEUE_STATUSES = (
    "pending_initial",
    "community_review",
    "verified",
    "pending_release",
)

_IDENTITY_QUEUE_FILTERS = ("pending", "approved", "rejected", "expired", "expiring", "all")
_IDENTITY_EXPIRING_DAYS = 30


def _author_display(author_id: int) -> str:
    """脱敏作者展示名：不可反推、不暴露内部 user_id 与用户名。"""
    digest = hashlib.sha256(f"plaza-author:{int(author_id)}".encode("utf-8")).hexdigest()[:6]
    return f"投稿者·{digest}"


def _checks_for(row: dict) -> list[dict]:
    snapshot = db.json_load(row.get("snapshot_json"), {})
    if not isinstance(snapshot, dict):
        snapshot = {}
    source = db.json_load(row.get("source_json"), {})
    if not isinstance(source, dict):
        source = {}
    image_ok, image_hint = service.image_check(
        int(row["author_id"]), snapshot.get("content") or {}
    )
    return service.snapshot_checks(
        snapshot, source, bool(row.get("license_confirmed")), image_ok, image_hint
    )


def _parse_statuses(raw: str | None) -> list[str]:
    text = str(raw or "").strip()
    if not text:
        return list(DEFAULT_QUEUE_STATUSES)
    values: list[str] = []
    for part in text.replace("，", ",").split(","):
        value = part.strip()
        if not value:
            continue
        if value not in db.SUBMISSION_STATUSES:
            raise HTTPException(422, {
                "message": "未知的投稿状态筛选项",
                "value": value,
                "allowed": list(db.SUBMISSION_STATUSES),
            })
        if value not in values:
            values.append(value)
    if not values:
        raise HTTPException(422, "至少指定一个投稿状态")
    return values


@router.get("/admin/submissions")
def admin_submission_queue(
    status: str | None = Query(default=None, max_length=400),
    subject: str | None = Query(default=None, max_length=40),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=MAX_PAGE_SIZE),
    user=Depends(service.require_plaza_role("editor")),
):
    """编辑台投稿队列：按状态（逗号多选）/学科筛选；作者以脱敏名展示。

    默认返回待处理集合：待初审、社区待核验、社区核验通过、待平台发布。
    不返回作者内部 user_id/用户名，也不返回评审人身份。
    """
    db.ensure_tables()
    statuses = _parse_statuses(status)
    placeholders = ", ".join("?" for _ in statuses)
    where = [f"status IN ({placeholders})"]
    params: list = list(statuses)
    if subject and subject.strip():
        where.append("subject=?")
        params.append(subject.strip())
    clause = " AND ".join(where)
    with connection() as con:
        total = int(con.execute(
            f"SELECT COUNT(*) FROM plaza_submissions WHERE {clause}", tuple(params)
        ).fetchone()[0])
        rows = con.execute(
            f"SELECT * FROM plaza_submissions WHERE {clause} "
            "ORDER BY updated_at DESC, id DESC LIMIT ? OFFSET ?",
            (*params, page_size, (page - 1) * page_size),
        ).fetchall()
        items = [_admin_item(con, dict(row)) for row in rows]
    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
        "status_filter": statuses,
    }


def _admin_item(con, row: dict) -> dict:
    snapshot = db.json_load(row.get("snapshot_json"), {})
    if not isinstance(snapshot, dict):
        snapshot = {}
    status = str(row.get("status") or "")
    summary = service.review_summary(con, row)
    blocking = service.blocking_checks(_checks_for(row))
    submitted = con.execute(
        "SELECT created_at FROM plaza_events WHERE submission_id=? "
        "AND action IN ('submitted','revised') ORDER BY id DESC LIMIT 1",
        (int(row["id"]),),
    ).fetchone()
    return {
        "id": int(row["id"]),
        "public_id": row.get("public_id"),
        "status": status,
        "status_label": db.STATUS_LABELS.get(status, status),
        "review_status": db.public_review_status(status),
        "subject": row.get("subject") or snapshot.get("subject") or "",
        "type": row.get("type") or snapshot.get("type") or "",
        "grade": row.get("grade") or snapshot.get("grade") or "",
        "stem_preview": service.stem_preview(snapshot, 160),
        "version": int(row.get("version") or 1),
        "current_version": int(row.get("current_version") or 1),
        # 脱敏作者展示：不可反推，公开接口不会出现该字段。
        "author_display": _author_display(int(row["author_id"])),
        "submitted_at": str(dict(submitted).get("created_at") if submitted else "") or
                        str(row.get("updated_at") or ""),
        "review": {
            "independent_reviews": int(summary.get("independent_reviews") or 0),
            "approves": int(summary.get("approves") or 0),
            "subject_reviewer_approves": int(summary.get("subject_reviewer_approves") or 0),
            "threshold_met": bool(summary.get("threshold_met")),
            "thresholds": {
                "min_reviews": db.THRESHOLD_MIN_REVIEWS,
                "min_approves": db.THRESHOLD_MIN_APPROVES,
                "min_subject_reviewer_approves": db.THRESHOLD_MIN_SUBJECT_APPROVES,
            },
        },
        # 兼容编辑台另一套读取（review_summary.thresholds）：内容与 review 一致。
        "review_summary": {
            "independent_reviews": int(summary.get("independent_reviews") or 0),
            "approves": int(summary.get("approves") or 0),
            "subject_reviewer_approves": int(summary.get("subject_reviewer_approves") or 0),
            "threshold_met": bool(summary.get("threshold_met")),
            "hidden": False,
            "thresholds": {
                "min_reviews": db.THRESHOLD_MIN_REVIEWS,
                "min_approves": db.THRESHOLD_MIN_APPROVES,
                "min_subject_reviewer_approves": db.THRESHOLD_MIN_SUBJECT_APPROVES,
            },
        },
        "deadline_passed": service.deadline_passed(row),
        "blocking_count": len(blocking),
        "blocking": [check["key"] for check in blocking],
        "updated_at": str(row.get("updated_at") or ""),
    }


# ---------------------------------------------------------------------------
# 身份认证审核（方案 §5：单独申请、最少量证明、仅授权人员查看、到期复核）
# ---------------------------------------------------------------------------

class IdentityReviewInput(BaseModel):
    decision: str = Field(pattern="^(approve|reject)$")
    reason: str = Field(default="", max_length=2000)
    expires_at: str | None = Field(default=None, max_length=40)


def _identity_status_sql(filter_name: str, now: str) -> tuple[str, list]:
    if filter_name == "all":
        return "", []
    if filter_name == "pending":
        return "i.status='pending'", []
    if filter_name == "rejected":
        return "i.status='rejected'", []
    if filter_name == "approved":
        return "i.status='approved' AND (i.expires_at IS NULL OR i.expires_at>?)", [now]
    if filter_name == "expired":
        return "i.status IN ('expired','approved') AND i.expires_at IS NOT NULL AND i.expires_at<=?", [now]
    # expiring：已通过且 30 天内到期
    from datetime import datetime, timedelta, timezone
    horizon = (datetime.now(timezone.utc) + timedelta(days=_IDENTITY_EXPIRING_DAYS)).isoformat()
    return "i.status='approved' AND i.expires_at IS NOT NULL AND i.expires_at>? AND i.expires_at<=?", [now, horizon]


def _identity_item(row: dict, username: str = "") -> dict:
    item = dict(row or {})
    effective = db.identity_effective_status(item)
    expires_at = item.get("expires_at")
    return {
        "id": int(item.get("id") or 0),
        "user": {"id": int(item.get("user_id") or 0), "username": str(username or "")},
        "kind": str(item.get("kind") or ""),
        "subject": str(item.get("subject") or ""),
        "evidence": str(item.get("evidence") or ""),
        "status": effective,
        "stored_status": str(item.get("status") or ""),
        "applied_at": str(item.get("applied_at") or ""),
        "reviewed_at": item.get("reviewed_at"),
        "reviewed_by": item.get("reviewed_by"),
        "expires_at": expires_at,
        "expired": effective == "expired",
        "review_due": bool(expires_at) and effective == "approved",
    }


@router.get("/admin/identity/queue")
def identity_queue(
    status: str = Query(default="pending", pattern="^(pending|approved|rejected|expired|expiring|all)$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=MAX_PAGE_SIZE),
    user=Depends(service.require_plaza_role("editor")),
):
    """身份认证审核队列（仅编辑/管理员；证明材料仅供授权人员查看）。"""
    db.ensure_tables()
    if status not in _IDENTITY_QUEUE_FILTERS:
        raise HTTPException(422, "未知的身份审核队列状态")
    now = utcnow()
    where_sql, params = _identity_status_sql(status, now)
    clause = f"WHERE {where_sql}" if where_sql else ""
    with connection() as con:
        total = int(con.execute(
            f"SELECT COUNT(*) FROM plaza_identity i {clause}", tuple(params)
        ).fetchone()[0])
        rows = con.execute(
            "SELECT i.*, u.username FROM plaza_identity i "
            "LEFT JOIN users u ON u.id=i.user_id "
            f"{clause} ORDER BY i.applied_at ASC, i.id ASC LIMIT ? OFFSET ?",
            (*params, page_size, (page - 1) * page_size),
        ).fetchall()
        items = [_identity_item(dict(row), dict(row).get("username") or "") for row in rows]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@router.post("/admin/identity/{identity_id}/review")
def review_identity(
    identity_id: int,
    data: IdentityReviewInput,
    user=Depends(service.require_plaza_role("editor")),
):
    """审核身份申请：approve/reject + 原因，写广场事件审计；通过可设到期复核时间。"""
    db.ensure_tables()
    user_id = int(user["id"])
    reason = data.reason.strip()
    if data.decision == "reject" and len(reason) < 2:
        raise HTTPException(422, "驳回身份申请必须填写原因")
    try:
        expires_at = db.normalize_timestamp(data.expires_at)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    if expires_at is not None and expires_at <= utcnow():
        raise HTTPException(422, "到期复核时间必须晚于当前时间")
    now = utcnow()
    target_status = "approved" if data.decision == "approve" else "rejected"
    new_expires = expires_at or (db.iso_in_days(365) if data.decision == "approve" else None)
    with connection() as con:
        write_lock(con, "plaza-write")
        raw = con.execute("SELECT * FROM plaza_identity WHERE id=?", (identity_id,)).fetchone()
        if raw is None:
            raise HTTPException(404, "未找到该身份申请")
        row = dict(raw)
        current = str(row.get("status") or "")
        if current != "pending":
            raise HTTPException(409, {
                "message": "该身份申请已处理，不能重复审核",
                "status": db.identity_effective_status(row),
            })
        changed = con.execute(
            "UPDATE plaza_identity SET status=?, reviewed_by=?, reviewed_at=?, expires_at=? "
            "WHERE id=? AND status='pending'",
            (target_status, user_id, now, new_expires, identity_id),
        ).rowcount
        if not changed:
            raise HTTPException(409, "身份申请已被其他操作处理，请刷新后重试")
        db.record_event(
            con, None, user_id, f"identity_{target_status}", "pending", target_status,
            reason=reason,
            payload={
                "identity_id": int(identity_id),
                "user_id": int(row.get("user_id") or 0),
                "kind": str(row.get("kind") or ""),
                "subject": str(row.get("subject") or ""),
                "expires_at": new_expires,
            },
        )
        fresh = con.execute("SELECT * FROM plaza_identity WHERE id=?", (identity_id,)).fetchone()
        fresh_row = dict(fresh) if fresh is not None else {}
        target_user = db.find_user(con, user_id=int(fresh_row.get("user_id") or 0)) or {}
        payload = {
            "identity": _identity_item(fresh_row, str(target_user.get("username") or "")),
            "action": target_status,
            "reviewed_by": user_id,
            "reviewed_at": now,
            "idempotent": False,
            "message": "身份申请已通过" if target_status == "approved" else "身份申请已驳回",
        }
    return payload


__all__ = ["router"]
