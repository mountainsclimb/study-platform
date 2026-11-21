"""广场角色/身份接口：我的能力、身份申请、管理员角色授权与撤销。

身份（teacher / student_teacher / student / contributor）仅用于展示与运营审核，
不参与权限判断；评审/编辑权限一律以 ``plaza_roles`` 的学科授权为准（方案 §5）。
角色授权保留到期时间与授权人，撤销采用置 ``active=0`` 留档，不做物理删除。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field

from ..auth import current_user, optional_user, require_admin
from ..db import connection, utcnow, write_lock
from . import db

router = APIRouter()


def _identity_public(row) -> dict | None:
    item = db._row_dict(row)
    if not item:
        return None
    stored_status = str(item.get("status") or "")
    return {
        "id": int(item.get("id") or 0),
        "kind": str(item.get("kind") or ""),
        "subject": str(item.get("subject") or ""),
        "evidence": str(item.get("evidence") or ""),
        # 已通过但超过到期时间按「已到期」展示，行本身保留供审计与复核。
        "status": db.identity_effective_status(item),
        "stored_status": stored_status,
        "applied_at": str(item.get("applied_at") or ""),
        "reviewed_at": item.get("reviewed_at"),
        "expires_at": item.get("expires_at"),
    }


def _role_public(item: dict) -> dict:
    return {
        "role": str(item.get("role") or ""),
        "subject": str(item.get("subject") or ""),
        "granted_at": str(item.get("granted_at") or ""),
        "expires_at": item.get("expires_at"),
    }


@router.get("/me")
def plaza_me(user=Depends(optional_user)):
    """登录后返回能力/角色/身份摘要；匿名返回 ``authenticated=false``（不 401）。"""
    if user is None:
        return {
            "authenticated": False,
            "user": None,
            "roles": [],
            "identity": None,
            "capabilities": {
                "read_public": True,
                "submit": False,
                "review": False,
                "initial_review": False,
                "publish": False,
                "grant_roles": False,
                "admin": False,
                "notifications": False,
                "favorites": False,
                "feedback": False,
                "appeals": False,
                "quality": False,
                "ops": False,
                "metrics": False,
                "rollback": False,
            },
            "unread_notifications": 0,
            "stats": {"submissions": 0, "favorites": 0, "unread_notifications": 0},
        }
    db.ensure_tables()
    user_id = int(user["id"])
    is_admin = str(user.get("role") or "") == "admin"
    with connection() as con:
        roles = db.active_roles(con, user_id)
        identity = con.execute(
            "SELECT * FROM plaza_identity WHERE user_id=? ORDER BY id DESC LIMIT 1",
            (user_id,),
        ).fetchone()
        submissions = int(con.execute(
            "SELECT COUNT(*) FROM plaza_submissions WHERE author_id=?", (user_id,)
        ).fetchone()[0])
        favorite_total = int(con.execute(
            "SELECT COUNT(*) FROM plaza_favorites WHERE user_id=?", (user_id,)
        ).fetchone()[0])
        unread = int(con.execute(
            "SELECT COUNT(*) FROM plaza_notifications "
            "WHERE user_id=? AND read_at IS NULL",
            (user_id,),
        ).fetchone()[0])
    names = {str(item.get("role") or "") for item in roles}
    can_edit = is_admin or "editor" in names
    can_review = is_admin or bool(names.intersection(db.REVIEWER_ROLES)) or can_edit
    return {
        "authenticated": True,
        "user": {"id": user_id, "username": user.get("username"), "role": user.get("role")},
        "roles": [_role_public(item) for item in roles],
        "identity": _identity_public(identity),
        "capabilities": {
            "read_public": True,
            "submit": True,
            "review": can_review,
            "initial_review": can_edit,
            "publish": can_edit,
            "grant_roles": is_admin,
            "admin": is_admin,
            # Phase 3 质量运营能力（前端据此显示通知铃铛/收藏/反馈入口与编辑台分区）。
            "notifications": True,
            "favorites": True,
            "feedback": True,
            "appeals": True,
            "quality": can_edit,
            "ops": can_edit,
            "metrics": can_edit,
            "rollback": can_edit,
        },
        "unread_notifications": unread,
        "stats": {
            "submissions": submissions,
            "favorites": favorite_total,
            "unread_notifications": unread,
        },
    }


# ---------------------------------------------------------------------------
# 身份申请
# ---------------------------------------------------------------------------

class IdentityApplyInput(BaseModel):
    kind: str = Field(min_length=2, max_length=40)
    subject: str = Field(default="", max_length=40)
    evidence: str = Field(min_length=2, max_length=2000)


@router.post("/identity/apply", status_code=201)
def apply_identity(data: IdentityApplyInput, response: Response, user=Depends(current_user)):
    """提交身份认证申请（最少量证明，仅授权人员查看；可撤回与到期复核由后台处理）。"""
    db.ensure_tables()
    kind = data.kind.strip()
    if kind not in db.IDENTITY_KINDS:
        raise HTTPException(422, "身份类型只能是 teacher / student_teacher / student / contributor")
    subject = data.subject.strip()
    evidence = data.evidence.strip()
    now = utcnow()
    with connection() as con:
        write_lock(con, "plaza-write")
        existing = con.execute(
            "SELECT * FROM plaza_identity WHERE user_id=? AND kind=? AND subject=? AND status='pending' "
            "ORDER BY id DESC LIMIT 1",
            (int(user["id"]), kind, subject),
        ).fetchone()
        if existing is not None:
            response.status_code = 200
            return {
                "identity": _identity_public(existing),
                "idempotent": True,
                "message": "相同身份申请正在审核中，无需重复提交",
            }
        identity_id = db.insert_row(con, "plaza_identity", {
            "user_id": int(user["id"]),
            "kind": kind,
            "subject": subject,
            "evidence": evidence,
            "status": "pending",
            "applied_at": now,
            "reviewed_by": None,
            "reviewed_at": None,
            "expires_at": None,
        })
        row = con.execute("SELECT * FROM plaza_identity WHERE id=?", (identity_id,)).fetchone()
        payload = {
            "identity": _identity_public(row),
            "idempotent": False,
            "message": "身份申请已提交；审核结果仅本人与授权运营可见。",
        }
    return payload


# ---------------------------------------------------------------------------
# 管理员：角色授权 / 撤销（带到期）
# ---------------------------------------------------------------------------

class RoleGrantInput(BaseModel):
    # ``action`` 是正式口径；编辑台另一套写法用 ``active`` 布尔表达授权/撤销，这里兼容。
    action: str | None = Field(default=None, pattern="^(grant|revoke)$")
    active: bool | None = None
    role: str = Field(pattern="^(reviewer|subject_reviewer|editor)$")
    subject: str = Field(default="", max_length=40)
    username: str | None = Field(default=None, max_length=40)
    user_id: int | None = Field(default=None, ge=1)
    expires_at: str | None = Field(default=None, max_length=40)

    def resolved_action(self) -> str:
        if self.action:
            return self.action
        if self.active is None:
            raise ValueError("缺少 action（grant/revoke）或 active（true/false）")
        return "grant" if self.active else "revoke"


@router.post("/admin/roles")
def manage_roles(data: RoleGrantInput, user=Depends(require_admin)):
    """管理员授权/撤销评审与编辑角色；评审角色必须绑定学科，可带到期时间。"""
    db.ensure_tables()
    try:
        action = data.resolved_action()
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    subject = data.subject.strip()
    try:
        expires_at = db.normalize_timestamp(data.expires_at)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    if action == "grant" and data.role in db.REVIEWER_ROLES and not subject:
        raise HTTPException(422, "评审/学科复核角色必须指定授权学科")
    with connection() as con:
        write_lock(con, "plaza-write")
        target = db.find_user(con, user_id=data.user_id, username=data.username)
        if target is None:
            raise HTTPException(404, "未找到该用户（username 或 user_id 至少要有一个能匹配）")
        target_id = int(target["id"])
        if action == "grant":
            if data.role == "editor":
                subject = ""
            con.execute(
                "UPDATE plaza_roles SET active=0 WHERE user_id=? AND role=? AND subject=?",
                (target_id, data.role, subject),
            )
            role_id = db.insert_row(con, "plaza_roles", {
                "user_id": target_id,
                "role": data.role,
                "subject": subject,
                "granted_by": int(user["id"]),
                "granted_at": utcnow(),
                "expires_at": expires_at,
                "active": 1,
            })
            row = con.execute("SELECT * FROM plaza_roles WHERE id=?", (role_id,)).fetchone()
            roles = db.active_roles(con, target_id)
            return {
                "action": "grant",
                "role": _role_public(dict(row)),
                "role_id": role_id,
                "user": {"id": target_id, "username": target.get("username")},
                "roles": [_role_public(item) for item in roles],
            }
        where = ["user_id=?", "role=?", "active=1"]
        params: list = [target_id, data.role]
        if subject:
            where.append("subject=?")
            params.append(subject)
        rows = con.execute(
            f"SELECT id FROM plaza_roles WHERE {' AND '.join(where)}", tuple(params)
        ).fetchall()
        if not rows:
            raise HTTPException(404, "该用户没有可撤销的对应角色")
        con.execute(
            f"UPDATE plaza_roles SET active=0 WHERE {' AND '.join(where)}", tuple(params)
        )
        roles = db.active_roles(con, target_id)
        return {
            "action": "revoke",
            "revoked": len(rows),
            "user": {"id": target_id, "username": target.get("username")},
            "roles": [_role_public(item) for item in roles],
        }
