"""广场通知中心：站内通知表、事件钩子与个人通知读取/已读接口。

规则（方案 §5、§8 阶段 3「上线抽检、通知及回滚」）：

* 通知只发给**相关用户**：投稿作者、收藏者、评审员本人、反馈人；同一事件不重复
  发给同一用户（按 user_id 去重）；
* 通知正文只写事件与处理结论，不写他人 ``user_id``/用户名/评审意见原文，避免
  通过通知间接泄露身份；
* 通知与状态更新、事件链写在同一事务（调用方已持有 ``plaza-write`` 写锁）：
  状态成功落库即通知，失败一起回滚；
* 读取接口只返回本人通知；``ids`` 中属于他人的通知被忽略，不报错也不泄露存在性。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from ..auth import current_user
from ..db import connection, utcnow, write_lock
from . import db

router = APIRouter()

MAX_PAGE_SIZE = 100
MAX_READ_IDS = 200


# ---------------------------------------------------------------------------
# 前端跳转链接（hash 路由；后端只是提示，前端可自行决定跳转目标）
# ---------------------------------------------------------------------------

def public_link(public_id: str) -> str:
    text = str(public_id or "").strip()
    return f"#/plaza?id={text}" if text else "#/plaza"


def submission_link(submission_id) -> str:
    try:
        return f"#/submission?id={int(submission_id)}"
    except (TypeError, ValueError):
        return "#/submission"


def review_link() -> str:
    return "#/review"


def admin_link(section: str = "ops") -> str:
    return f"#/plaza-admin?section={str(section or 'ops')}"


# ---------------------------------------------------------------------------
# 写通知（调用方持有写事务）
# ---------------------------------------------------------------------------

def _int_or_none(value):
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def notify(con, user_id, kind: str, title: str, body: str = "", *,
           submission_id=None, public_id: str = "", link: str = "",
           payload: dict | None = None) -> int | None:
    """给单个用户写一条通知；``user_id`` 无效时静默跳过（不阻断主流程）。"""
    uid = _int_or_none(user_id)
    if uid is None:
        return None
    return db.insert_row(con, "plaza_notifications", {
        "user_id": uid,
        "kind": str(kind or "")[:60],
        "title": str(title or "")[:200],
        "body": str(body or "")[:2000],
        "submission_id": _int_or_none(submission_id),
        "public_id": str(public_id or "")[:80],
        "link": str(link or "")[:300],
        "payload_json": db.json_dump(payload or {}),
        "read_at": None,
        "created_at": utcnow(),
    })


def notify_many(con, user_ids, kind: str, title: str, body: str = "", *,
                submission_id=None, public_id: str = "", link: str = "",
                payload: dict | None = None, exclude=()) -> int:
    """给多个用户写同一条通知（按 user_id 去重，跳过无效/排除用户）。"""
    excluded = {_int_or_none(item) for item in exclude}
    sent = 0
    seen: set[int] = set()
    for raw in user_ids or ():
        uid = _int_or_none(raw)
        if uid is None or uid in seen or uid in excluded:
            continue
        seen.add(uid)
        notify(
            con, uid, kind, title, body,
            submission_id=submission_id, public_id=public_id, link=link,
            payload=payload,
        )
        sent += 1
    return sent


def notification_public(item: dict) -> dict:
    payload = db.json_load(item.get("payload_json"), {})
    read_at = item.get("read_at")
    return {
        "id": int(item.get("id") or 0),
        "kind": str(item.get("kind") or ""),
        "title": str(item.get("title") or ""),
        "body": str(item.get("body") or ""),
        "submission_id": item.get("submission_id"),
        "public_id": str(item.get("public_id") or "") or None,
        "link": str(item.get("link") or ""),
        "payload": payload if isinstance(payload, dict) else {},
        "read": read_at is not None,
        "read_at": read_at,
        "created_at": str(item.get("created_at") or ""),
    }


def unread_count(con, user_id) -> int:
    uid = _int_or_none(user_id)
    if uid is None:
        return 0
    row = con.execute(
        "SELECT COUNT(*) FROM plaza_notifications WHERE user_id=? AND read_at IS NULL",
        (uid,),
    ).fetchone()
    return int(row[0] or 0)


# ---------------------------------------------------------------------------
# 个人通知接口
# ---------------------------------------------------------------------------

@router.get("/notifications")
def list_notifications(
    unread_only: bool = Query(default=False),
    kind: str | None = Query(default=None, max_length=60),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=MAX_PAGE_SIZE),
    user=Depends(current_user),
):
    """我的通知列表（只含本人通知）；``unread_count`` 为本人全部未读数。"""
    db.ensure_tables()
    user_id = int(user["id"])
    where = ["user_id=?"]
    params: list = [user_id]
    if unread_only:
        where.append("read_at IS NULL")
    if kind:
        where.append("kind=?")
        params.append(kind.strip())
    clause = " AND ".join(where)
    with connection() as con:
        total = int(con.execute(
            f"SELECT COUNT(*) FROM plaza_notifications WHERE {clause}", tuple(params)
        ).fetchone()[0])
        rows = con.execute(
            f"SELECT * FROM plaza_notifications WHERE {clause} "
            "ORDER BY id DESC LIMIT ? OFFSET ?",
            (*params, page_size, (page - 1) * page_size),
        ).fetchall()
        items = [notification_public(dict(row)) for row in rows]
        unread = unread_count(con, user_id)
    return {
        "items": items,
        "total": total,
        "unread_count": unread,
        "page": page,
        "page_size": page_size,
    }


class ReadInput(BaseModel):
    ids: list[int] | None = Field(default=None, max_length=MAX_READ_IDS)
    all: bool | None = None


@router.post("/notifications/read")
def read_notifications(data: ReadInput, user=Depends(current_user)):
    """标记本人通知为已读：``{ids:[...]}`` 或 ``{all:true}``；他人通知被忽略。"""
    db.ensure_tables()
    user_id = int(user["id"])
    now = utcnow()
    with connection() as con:
        write_lock(con, "plaza-write")
        if data.all:
            updated = con.execute(
                "UPDATE plaza_notifications SET read_at=? "
                "WHERE user_id=? AND read_at IS NULL",
                (now, user_id),
            ).rowcount
        elif data.ids:
            ids = sorted({int(item) for item in data.ids if int(item) > 0})
            if not ids:
                raise _invalid_ids()
            placeholders = ", ".join("?" for _ in ids)
            updated = con.execute(
                f"UPDATE plaza_notifications SET read_at=? "
                f"WHERE user_id=? AND read_at IS NULL AND id IN ({placeholders})",
                (now, user_id, *ids),
            ).rowcount
        else:
            raise _invalid_ids()
        updated = int(updated or 0)
        unread = unread_count(con, user_id)
    return {
        "updated": updated,
        "unread_count": unread,
        "message": "已全部标记为已读" if data.all else f"已标记 {updated} 条通知为已读",
    }


def _invalid_ids():
    return HTTPException(422, {
        "message": "请提供要标记为已读的通知 ids，或使用 all:true 全部标记",
        "field": "ids|all",
    })


__all__ = [
    "admin_link",
    "list_notifications",
    "notify",
    "notify_many",
    "notification_public",
    "public_link",
    "read_notifications",
    "review_link",
    "submission_link",
    "unread_count",
]
