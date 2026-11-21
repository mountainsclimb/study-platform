"""广场收藏：收藏/取消收藏、我的收藏列表与「撤回通知收藏者」。

规则（方案 §5、§7）：

* 只有**当前公开可见**（社区待核验 / 社区核验通过 / 平台精选且发布指针活跃）的题目
  可收藏；草稿、初审未通过、已撤回、有争议一律 409；
* 收藏记录只存用户与投稿的内部关联，列表按**当前**投稿状态实时投影，撤回后仍可
  看到「已撤回」状态与提示，但不再标记为可用；
* 撤回（作者撤回、编辑撤回、抽检严重问题自动撤回）在事务内通知收藏者，
  通知不包含作者/编辑身份。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from ..auth import current_user
from ..db import connection, utcnow, write_lock
from . import db, figures, notify, service

router = APIRouter()

MAX_PAGE_SIZE = 100


# ---------------------------------------------------------------------------
# 读取辅助
# ---------------------------------------------------------------------------

def is_favorited(con, user_id, submission_id) -> bool:
    try:
        uid = int(user_id)
        sid = int(submission_id)
    except (TypeError, ValueError):
        return False
    if uid <= 0 or sid <= 0:
        return False
    row = con.execute(
        "SELECT 1 FROM plaza_favorites WHERE user_id=? AND submission_id=? LIMIT 1",
        (uid, sid),
    ).fetchone()
    return row is not None


def favorite_count(con, submission_id) -> int:
    try:
        sid = int(submission_id)
    except (TypeError, ValueError):
        return 0
    row = con.execute(
        "SELECT COUNT(*) FROM plaza_favorites WHERE submission_id=?", (sid,),
    ).fetchone()
    return int(row[0] or 0)


def favoriter_ids(con, submission_id) -> list[int]:
    try:
        sid = int(submission_id)
    except (TypeError, ValueError):
        return []
    rows = con.execute(
        "SELECT user_id FROM plaza_favorites WHERE submission_id=? ORDER BY id",
        (sid,),
    ).fetchall()
    return [int(item[0]) for item in rows]


def notify_favoriters(con, submission: dict, kind: str, title: str, body: str = "", *,
                      exclude=()) -> int:
    """通知收藏者（排除作者本人等 ``exclude``）；不泄露作者/处理人身份。"""
    if not isinstance(submission, dict):
        return 0
    submitter = int(submission.get("author_id") or 0)
    excluded = {submitter, *{int(item) for item in exclude if item}}
    return notify.notify_many(
        con, favoriter_ids(con, submission.get("id")),
        kind, title, body,
        submission_id=submission.get("id"),
        public_id=str(submission.get("public_id") or ""),
        link=notify.public_link(str(submission.get("public_id") or "")),
        exclude=excluded,
    )


# ---------------------------------------------------------------------------
# 序列化
# ---------------------------------------------------------------------------

def _available(con, submission: dict) -> bool:
    if not isinstance(submission, dict):
        return False
    if str(submission.get("status") or "") not in db.PUBLIC_STATUSES:
        return False
    return figures.submission_publicly_visible(con, submission)


def favorite_item(con, favorite: dict) -> dict:
    submission = db.load_submission(con, int(favorite.get("submission_id") or 0))
    if submission is None:
        submission = {}
    snapshot = db.json_load(submission.get("snapshot_json"), {})
    if not isinstance(snapshot, dict):
        snapshot = {}
    status = str(submission.get("status") or "")
    public_id = str(submission.get("public_id") or favorite.get("public_id") or "")
    available = _available(con, submission)
    return {
        "id": int(favorite.get("id") or 0),
        "submission_id": int(favorite.get("submission_id") or 0),
        "public_id": public_id or None,
        "status": status,
        "status_label": db.STATUS_LABELS.get(status, status),
        "review_status": db.public_review_status(status),
        "available": available,
        "favorited_at": str(favorite.get("created_at") or ""),
        "subject": snapshot.get("subject") or "",
        "type": snapshot.get("type") or "",
        "grade": snapshot.get("grade") or "",
        "difficulty": snapshot.get("difficulty"),
        "stem_preview": service.stem_preview(snapshot),
        "source": "featured" if status == "published" else "community",
        "featured": status == "published",
        "link": notify.public_link(public_id),
        "notice": service.status_notice(status),
    }


# ---------------------------------------------------------------------------
# 接口
# ---------------------------------------------------------------------------

@router.post("/questions/{public_id}/favorite")
def add_favorite(public_id: str, user=Depends(current_user)):
    """收藏公开题（幂等）；不可见/已撤回 409。"""
    db.ensure_tables()
    user_id = int(user["id"])
    with connection() as con:
        write_lock(con, "plaza-write")
        submission = db.load_submission_by_public_id(con, public_id)
        if submission is None:
            raise HTTPException(404, "未找到该公开题目")
        if not _available(con, submission):
            raise HTTPException(409, {
                "message": "该题当前不可收藏（可能已撤回、有争议或尚未公开）",
                "status": str(submission.get("status") or ""),
                "status_label": db.STATUS_LABELS.get(
                    str(submission.get("status") or ""), "",
                ),
            })
        existing = con.execute(
            "SELECT id FROM plaza_favorites WHERE user_id=? AND submission_id=?",
            (user_id, int(submission["id"])),
        ).fetchone()
        if existing is not None:
            return {
                "favorited": True,
                "idempotent": True,
                "public_id": str(submission.get("public_id") or public_id),
                "favorite_id": int(dict(existing).get("id") or 0),
                "favorite_count": favorite_count(con, int(submission["id"])),
                "message": "已收藏过该题，无需重复收藏",
            }
        favorite_id = db.insert_row(con, "plaza_favorites", {
            "user_id": user_id,
            "submission_id": int(submission["id"]),
            "public_id": str(submission.get("public_id") or public_id),
            "created_at": utcnow(),
        })
        count = favorite_count(con, int(submission["id"]))
    return {
        "favorited": True,
        "idempotent": False,
        "public_id": str(submission.get("public_id") or public_id),
        "favorite_id": favorite_id,
        "favorite_count": count,
        "message": "已加入收藏；题目撤回时会通知你",
    }


@router.delete("/questions/{public_id}/favorite")
def remove_favorite(public_id: str, user=Depends(current_user)):
    """取消收藏（幂等）；题目已撤回也允许取消。"""
    db.ensure_tables()
    user_id = int(user["id"])
    with connection() as con:
        write_lock(con, "plaza-write")
        submission = db.load_submission_by_public_id(con, public_id)
        if submission is None:
            raise HTTPException(404, "未找到该公开题目")
        removed = con.execute(
            "DELETE FROM plaza_favorites WHERE user_id=? AND submission_id=?",
            (user_id, int(submission["id"])),
        ).rowcount
        count = favorite_count(con, int(submission["id"]))
    return {
        "favorited": False,
        "idempotent": not bool(removed),
        "public_id": str(submission.get("public_id") or public_id),
        "favorite_count": count,
        "message": "已取消收藏" if removed else "尚未收藏该题",
    }


@router.get("/favorites")
def list_favorites(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=MAX_PAGE_SIZE),
    user=Depends(current_user),
):
    """我的收藏：按当前投稿状态实时投影（撤回题保留状态提示、available=false）。"""
    db.ensure_tables()
    user_id = int(user["id"])
    with connection() as con:
        total = int(con.execute(
            "SELECT COUNT(*) FROM plaza_favorites WHERE user_id=?", (user_id,),
        ).fetchone()[0])
        rows = con.execute(
            "SELECT * FROM plaza_favorites WHERE user_id=? ORDER BY id DESC "
            "LIMIT ? OFFSET ?",
            (user_id, page_size, (page - 1) * page_size),
        ).fetchall()
        items = [favorite_item(con, dict(row)) for row in rows]
    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


__all__ = [
    "add_favorite",
    "favorite_count",
    "favorite_item",
    "favoriter_ids",
    "is_favorited",
    "list_favorites",
    "notify_favoriters",
    "remove_favorite",
]
