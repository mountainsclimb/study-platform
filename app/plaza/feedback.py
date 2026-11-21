"""广场反馈闭环：题目纠错、举报与作者申诉。

规则（方案 §5「投稿到官方发布」「文件、版权和未成年人」）：

* 任意登录用户可对**当前公开可见**的题目提交纠错/举报；草稿、已撤回、有争议一律
  409（不可反馈），未知公开题号 404；
* 作者只能对 ``rejected`` / ``withdrawn`` / ``disputed`` 的投稿发起申诉，他人投稿
  统一 404（不泄露存在性）；相同投稿已有待处理申诉时幂等返回既有申诉；
* 编辑/管理员处理（``resolve`` / ``reject`` + 理由）在同事务内更新状态、写
  ``plaza_events`` 审计事件并通知反馈人/作者；重复处理 409；
* 编辑端队列对反馈人做脱敏展示（``反馈者·<hash>``），不输出内部 user_id/用户名。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field

from ..auth import current_user
from ..db import connection, utcnow, write_lock
from . import db, notify, service

router = APIRouter()

MAX_PAGE_SIZE = 100

_ISSUE = "issue"
_REPORT = "report"
_APPEAL = "appeal"
_TABLES = {
    _ISSUE: "plaza_issues",
    _REPORT: "plaza_reports",
    _APPEAL: "plaza_appeals",
}
_QUESTION_KINDS = (_ISSUE, _REPORT)
_ID_KEY = {_ISSUE: "issue_id", _REPORT: "report_id", _APPEAL: "appeal_id"}
_KIND_LABELS = {_ISSUE: "纠错", _REPORT: "举报", _APPEAL: "申诉"}
_DISPLAY_PREFIX = {_ISSUE: "反馈者", _REPORT: "反馈者", _APPEAL: "申诉作者"}
_QUEUE_FILTERS = ("pending", "resolved", "rejected", "all")


# ---------------------------------------------------------------------------
# 读取辅助与序列化
# ---------------------------------------------------------------------------

def _load_feedback(con, kind: str, item_id: int) -> dict | None:
    row = con.execute(
        f"SELECT * FROM {_TABLES[kind]} WHERE id=?", (int(item_id),),
    ).fetchone()
    return db._row_dict(row)


def _question_summary(con, submission_id) -> dict:
    submission = db.load_submission(con, int(submission_id or 0))
    if submission is None:
        return {}
    snapshot = db.json_load(submission.get("snapshot_json"), {})
    if not isinstance(snapshot, dict):
        snapshot = {}
    status = str(submission.get("status") or "")
    return {
        "public_id": submission.get("public_id"),
        "status": status,
        "status_label": db.STATUS_LABELS.get(status, status),
        "subject": snapshot.get("subject") or "",
        "type": snapshot.get("type") or "",
        "grade": snapshot.get("grade") or "",
        "stem_preview": service.stem_preview(snapshot),
    }


def feedback_item(con, kind: str, row: dict) -> dict:
    """编辑端/作者端反馈序列化：反馈人脱敏，不含内部 user_id/用户名。"""
    item = {
        "id": int(row.get("id") or 0),
        "submission_id": int(row.get("submission_id") or 0),
        "public_id": str(row.get("public_id") or "") or None,
        "status": str(row.get("status") or ""),
        "status_label": db.feedback_status_label(row.get("status")),
        "resolution": str(row.get("resolution") or ""),
        "created_at": str(row.get("created_at") or ""),
        "updated_at": str(row.get("updated_at") or ""),
        "resolved_at": row.get("resolved_at"),
        "question": _question_summary(con, row.get("submission_id")),
    }
    display = db.masked_display(
        _DISPLAY_PREFIX[kind], row.get("user_id", row.get("author_id")),
    )
    if kind == _APPEAL:
        item["author_display"] = display
        item["reason"] = str(row.get("reason") or "")
    else:
        item["reporter_display"] = display
    if kind == _ISSUE:
        item["field"] = str(row.get("field") or "")
        item["description"] = str(row.get("description") or "")
    elif kind == _REPORT:
        item["reason"] = str(row.get("reason") or "")
        item["detail"] = str(row.get("detail") or "")
    return item


def _public_target(con, public_id: str) -> dict:
    submission = db.load_submission_by_public_id(con, public_id)
    if submission is None:
        raise HTTPException(404, "未找到该公开题目")
    status = str(submission.get("status") or "")
    if status not in db.PUBLIC_STATUSES:
        raise HTTPException(409, {
            "message": "该题当前不可反馈（可能已撤回、有争议或尚未公开）",
            "status": status,
            "status_label": db.STATUS_LABELS.get(status, status),
        })
    return submission


# ---------------------------------------------------------------------------
# 用户端：题目纠错 / 举报
# ---------------------------------------------------------------------------

class IssueInput(BaseModel):
    field: str = Field(default="other", max_length=20)
    description: str = Field(min_length=2, max_length=4000)


@router.post("/questions/{public_id}/issues", status_code=201)
def create_issue(public_id: str, data: IssueInput, user=Depends(current_user)):
    """提交题目纠错（字段 + 描述）；处理结果通过通知中心返回给反馈人。"""
    db.ensure_tables()
    field = data.field.strip() or "other"
    if field not in db.ISSUE_FIELDS:
        raise HTTPException(422, {
            "message": "未知的纠错字段",
            "allowed": list(db.ISSUE_FIELDS),
        })
    description = data.description.strip()
    if len(description) < 2:
        raise HTTPException(422, "请填写具体的问题描述（至少 2 个字）")
    user_id = int(user["id"])
    now = utcnow()
    with connection() as con:
        write_lock(con, "plaza-write")
        submission = _public_target(con, public_id)
        issue_id = db.insert_row(con, "plaza_issues", {
            "submission_id": int(submission["id"]),
            "public_id": str(submission.get("public_id") or public_id),
            "user_id": user_id,
            "field": field,
            "description": description[:4000],
            "status": "pending",
            "resolution": "",
            "handled_by": None,
            "created_at": now,
            "updated_at": now,
            "resolved_at": None,
        })
        db.record_event(
            con, int(submission["id"]), user_id, "issue_submitted", "", "",
            payload={
                "issue_id": issue_id,
                "public_id": str(submission.get("public_id") or public_id),
                "field": field,
            },
        )
        row = _load_feedback(con, _ISSUE, issue_id) or {}
        payload = {
            "issue": feedback_item(con, _ISSUE, row) if row else {"id": issue_id},
            "message": "纠错已提交，编辑处理后会在通知中心通知你",
        }
    return payload


class ReportInput(BaseModel):
    reason: str = Field(default="other", max_length=20)
    detail: str = Field(default="", max_length=4000)


@router.post("/questions/{public_id}/reports", status_code=201)
def create_report(public_id: str, data: ReportInput, user=Depends(current_user)):
    """提交举报（原因 + 补充说明）；处理结果通过通知中心返回给反馈人。"""
    db.ensure_tables()
    reason = data.reason.strip() or "other"
    if reason not in db.REPORT_REASONS:
        raise HTTPException(422, {
            "message": "未知的举报原因",
            "allowed": list(db.REPORT_REASONS),
        })
    user_id = int(user["id"])
    now = utcnow()
    with connection() as con:
        write_lock(con, "plaza-write")
        submission = _public_target(con, public_id)
        report_id = db.insert_row(con, "plaza_reports", {
            "submission_id": int(submission["id"]),
            "public_id": str(submission.get("public_id") or public_id),
            "user_id": user_id,
            "reason": reason,
            "detail": data.detail.strip()[:4000],
            "status": "pending",
            "resolution": "",
            "handled_by": None,
            "created_at": now,
            "updated_at": now,
            "resolved_at": None,
        })
        db.record_event(
            con, int(submission["id"]), user_id, "report_submitted", "", "",
            payload={
                "report_id": report_id,
                "public_id": str(submission.get("public_id") or public_id),
                "reason": reason,
            },
        )
        row = _load_feedback(con, _REPORT, report_id) or {}
        payload = {
            "report": feedback_item(con, _REPORT, row) if row else {"id": report_id},
            "message": "举报已提交，编辑处理后会在通知中心通知你",
        }
    return payload


# ---------------------------------------------------------------------------
# 作者端：申诉
# ---------------------------------------------------------------------------

class AppealInput(BaseModel):
    reason: str = Field(min_length=2, max_length=2000)


@router.post("/submissions/{submission_id}/appeals")
def create_appeal(
    submission_id: int, data: AppealInput, response: Response,
    user=Depends(current_user),
):
    """作者申诉：仅 rejected/withdrawn/disputed 可申诉；待处理申诉幂等返回。"""
    db.ensure_tables()
    user_id = int(user["id"])
    reason = data.reason.strip()
    if len(reason) < 2:
        raise HTTPException(422, "请填写申诉理由（至少 2 个字）")
    now = utcnow()
    with connection() as con:
        write_lock(con, "plaza-write")
        submission = db.load_submission(con, submission_id)
        if submission is None or int(submission["author_id"]) != user_id:
            raise HTTPException(404, "未找到该投稿")
        status = str(submission.get("status") or "")
        if status not in db.APPEALABLE_STATUSES:
            raise HTTPException(409, {
                "message": "当前状态不可申诉（仅初审未通过/已撤回/有争议可申诉）",
                "status": status,
                "status_label": db.STATUS_LABELS.get(status, status),
            })
        existing = con.execute(
            "SELECT * FROM plaza_appeals WHERE submission_id=? AND user_id=? "
            "AND status='pending' ORDER BY id DESC LIMIT 1",
            (int(submission_id), user_id),
        ).fetchone()
        if existing is not None:
            response.status_code = 200
            return {
                "appeal": feedback_item(con, _APPEAL, dict(existing)),
                "idempotent": True,
                "message": "已有待处理申诉，编辑处理后会在通知中心通知你",
            }
        appeal_id = db.insert_row(con, "plaza_appeals", {
            "submission_id": int(submission_id),
            "user_id": user_id,
            "reason": reason[:2000],
            "status": "pending",
            "resolution": "",
            "handled_by": None,
            "created_at": now,
            "updated_at": now,
            "resolved_at": None,
        })
        db.record_event(
            con, int(submission_id), user_id, "appeal_submitted", status, status,
            reason=reason[:2000],
            payload={"appeal_id": appeal_id, "status": status},
        )
        row = _load_feedback(con, _APPEAL, appeal_id) or {}
        payload = {
            "appeal": feedback_item(con, _APPEAL, row) if row else {"id": appeal_id},
            "idempotent": False,
            "message": "申诉已提交，编辑处理后会在通知中心通知你",
        }
    response.status_code = 201
    return payload


@router.get("/submissions/{submission_id}/appeals")
def list_submission_appeals(submission_id: int, user=Depends(current_user)):
    """投稿申诉记录：作者只看自己的申诉，编辑/管理员看全部（反馈人脱敏）。"""
    db.ensure_tables()
    user_id = int(user["id"])
    with connection() as con:
        submission = db.load_submission(con, submission_id)
        if submission is None:
            raise HTTPException(404, "未找到该投稿")
        names = db.role_names(con, user_id)
        is_editor = str(user.get("role") or "") == "admin" or "editor" in names
        if int(submission["author_id"]) != user_id and not is_editor:
            raise HTTPException(404, "未找到该投稿")
        where = "submission_id=?"
        params: list = [int(submission_id)]
        if not is_editor:
            where += " AND user_id=?"
            params.append(user_id)
        rows = con.execute(
            f"SELECT * FROM plaza_appeals WHERE {where} ORDER BY id DESC", tuple(params),
        ).fetchall()
        items = [feedback_item(con, _APPEAL, dict(row)) for row in rows]
    return {"items": items, "total": len(items), "submission_id": int(submission_id)}


# ---------------------------------------------------------------------------
# 编辑端队列
# ---------------------------------------------------------------------------

def _status_filter(raw: str | None) -> str:
    value = str(raw or "pending").strip()
    if value not in _QUEUE_FILTERS:
        raise HTTPException(422, {
            "message": "未知的反馈队列状态",
            "allowed": list(_QUEUE_FILTERS),
        })
    return value


def _queue_counts(con, kind: str) -> dict:
    rows = con.execute(
        f"SELECT status, COUNT(*) FROM {_TABLES[kind]} GROUP BY status",
    ).fetchall()
    counts = {status: 0 for status in db.FEEDBACK_STATUSES}
    for row in rows:
        key = str(row[0] or "")
        if key in counts:
            counts[key] = int(row[1] or 0)
    counts["all"] = sum(counts.values())
    return counts


def _queue(con, kind: str, status: str, page: int, page_size: int) -> dict:
    where = ""
    params: list = []
    if status != "all":
        where = " WHERE status=?"
        params.append(status)
    total = int(con.execute(
        f"SELECT COUNT(*) FROM {_TABLES[kind]}{where}", tuple(params),
    ).fetchone()[0])
    rows = con.execute(
        f"SELECT * FROM {_TABLES[kind]}{where} ORDER BY id DESC LIMIT ? OFFSET ?",
        (*params, page_size, (page - 1) * page_size),
    ).fetchall()
    items = [feedback_item(con, kind, dict(row)) for row in rows]
    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
        "status_filter": status,
        "counts": _queue_counts(con, kind),
    }


@router.get("/admin/issues")
def admin_issue_queue(
    status: str = Query(default="pending", max_length=20),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=MAX_PAGE_SIZE),
    user=Depends(service.require_plaza_role("editor")),
):
    """纠错队列（仅编辑/管理员）：反馈人脱敏，附题目摘要。"""
    db.ensure_tables()
    with connection() as con:
        return _queue(con, _ISSUE, _status_filter(status), page, page_size)


@router.get("/admin/reports")
def admin_report_queue(
    status: str = Query(default="pending", max_length=20),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=MAX_PAGE_SIZE),
    user=Depends(service.require_plaza_role("editor")),
):
    """举报队列（仅编辑/管理员）：反馈人脱敏，附题目摘要。"""
    db.ensure_tables()
    with connection() as con:
        return _queue(con, _REPORT, _status_filter(status), page, page_size)


@router.get("/admin/appeals")
def admin_appeal_queue(
    status: str = Query(default="pending", max_length=20),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=MAX_PAGE_SIZE),
    user=Depends(service.require_plaza_role("editor")),
):
    """申诉队列（仅编辑/管理员）：申诉作者脱敏，附题目摘要。"""
    db.ensure_tables()
    with connection() as con:
        return _queue(con, _APPEAL, _status_filter(status), page, page_size)


# ---------------------------------------------------------------------------
# 编辑端处理动作
# ---------------------------------------------------------------------------

class FeedbackHandleInput(BaseModel):
    action: str = Field(pattern="^(resolve|reject)$")
    reason: str = Field(default="", max_length=2000)


def _handle_feedback(con, kind: str, item_id: int, data: FeedbackHandleInput,
                     user: dict) -> dict:
    row = _load_feedback(con, kind, item_id)
    if row is None:
        raise HTTPException(404, f"未找到该{_KIND_LABELS[kind]}记录")
    current = str(row.get("status") or "")
    if current != "pending":
        raise HTTPException(409, {
            "message": f"该{_KIND_LABELS[kind]}已处理，不能重复处理",
            "status": current,
            "status_label": db.feedback_status_label(current),
        })
    target = "resolved" if data.action == "resolve" else "rejected"
    reason = data.reason.strip()
    if target == "rejected" and len(reason) < 2:
        raise HTTPException(422, "驳回必须填写原因（便于反馈人/作者理解）")
    now = utcnow()
    changed = con.execute(
        f"UPDATE {_TABLES[kind]} SET status=?, resolution=?, handled_by=?, "
        "updated_at=?, resolved_at=? WHERE id=? AND status='pending'",
        (target, reason[:2000], int(user["id"]), now, now, int(item_id)),
    ).rowcount
    if not changed:
        raise HTTPException(409, "该记录已被其他操作处理，请刷新后重试")
    submission_id = int(row.get("submission_id") or 0)
    public_id = str(row.get("public_id") or "")
    db.record_event(
        con, submission_id, int(user["id"]), f"{kind}_{target}", "pending", target,
        reason=reason[:2000],
        payload={
            _ID_KEY[kind]: int(item_id),
            "public_id": public_id,
            "resolution": reason[:2000],
        },
    )
    target_user = int(row.get("user_id") or 0)
    label = _KIND_LABELS[kind]
    title = f"{label}处理结果：{'已处理' if target == 'resolved' else '已驳回'}"
    body = f"你提交的{label}已由平台编辑处理。"
    if reason:
        body += f"处理说明：{reason[:500]}"
    link = notify.public_link(public_id) if public_id else notify.submission_link(submission_id)
    notify.notify(
        con, target_user, f"{kind}_{target}", title, body,
        submission_id=submission_id or None, public_id=public_id, link=link,
        payload={_ID_KEY[kind]: int(item_id), "status": target},
    )
    fresh = _load_feedback(con, kind, item_id) or row
    return {
        kind: feedback_item(con, kind, fresh),
        "action": data.action,
        "message": f"{label}已{'处理' if target == 'resolved' else '驳回'}",
    }


@router.patch("/admin/issues/{issue_id}")
def handle_issue(
    issue_id: int, data: FeedbackHandleInput,
    user=Depends(service.require_plaza_role("editor")),
):
    """处理纠错：resolve=已采纳 / reject=不采纳（驳回需理由）。"""
    db.ensure_tables()
    with connection() as con:
        write_lock(con, "plaza-write")
        return _handle_feedback(con, _ISSUE, issue_id, data, user)


@router.patch("/admin/reports/{report_id}")
def handle_report(
    report_id: int, data: FeedbackHandleInput,
    user=Depends(service.require_plaza_role("editor")),
):
    """处理举报：resolve=已处理 / reject=不成立（驳回需理由）。"""
    db.ensure_tables()
    with connection() as con:
        write_lock(con, "plaza-write")
        return _handle_feedback(con, _REPORT, report_id, data, user)


@router.patch("/admin/appeals/{appeal_id}")
def handle_appeal(
    appeal_id: int, data: FeedbackHandleInput,
    user=Depends(service.require_plaza_role("editor")),
):
    """处理作者申诉：resolve=受理 / reject=不受理（驳回需理由）。"""
    db.ensure_tables()
    with connection() as con:
        write_lock(con, "plaza-write")
        return _handle_feedback(con, _APPEAL, appeal_id, data, user)


__all__ = [
    "admin_appeal_queue",
    "admin_issue_queue",
    "admin_report_queue",
    "create_appeal",
    "create_issue",
    "create_report",
    "feedback_item",
    "handle_appeal",
    "handle_issue",
    "handle_report",
    "list_submission_appeals",
]
