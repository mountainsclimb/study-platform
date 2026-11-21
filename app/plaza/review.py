"""广场评审端接口：编辑初审、按学科评审队列、独立评审与阈值自动流转。

安全与公平性规则（方案 §5）：

* 作者回避：作者不能评审自己的投稿，编辑不能初审自己的投稿；
* 一人一版本一票：``UNIQUE(submission_id, version, reviewer_id)``，重复提交返回 409；
* 投票隐藏：评审员提交自己的独立评审之前，看不到已有投票与意见；
* 冲突声明：``conflict_declared=True`` 的票留档但不计入独立票；
* 阈值自动流转：同版 ≥5 位独立评审、≥4 通过、其中 ≥2 位学科复核员通过且无 reject
  阻断 → ``verified``；学科复核员 reject → ``disputed``；七天未凑齐保持当前状态。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from ..db import connection, utcnow, write_lock
from . import db, figures, notify, service

router = APIRouter()

MAX_PAGE_SIZE = 50


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


def _review_payload(item: dict) -> dict:
    return {
        "id": int(item.get("id") or 0),
        "verdict": str(item.get("verdict") or ""),
        "reviewer_role": str(item.get("reviewer_role") or ""),
        "reasons": str(item.get("reasons") or ""),
        "independent_answer": str(item.get("independent_answer") or ""),
        "conflict_declared": bool(item.get("conflict_declared")),
        "created_at": str(item.get("created_at") or ""),
    }


def _register_figures(con, row: dict) -> dict:
    """登记公开衍生图；任何异常都降级为空摘要（配图问题不阻断初审/发布）。"""
    try:
        return figures.ensure_public_figures(con, row)
    except Exception:  # noqa: BLE001 - 图片登记失败不能回滚审核状态流转
        return {
            "version": int((row or {}).get("current_version") or 1),
            "total": 0, "public": 0, "unavailable": 0,
            "items": [], "notices": [],
            "figure_notice": "部分配图暂不可公开查看", "blocking": False,
        }


def _figure_event_payload(summary: dict | None) -> dict:
    if not summary:
        return {}
    return {
        "total": int(summary.get("total") or 0),
        "public": int(summary.get("public") or 0),
        "unavailable": int(summary.get("unavailable") or 0),
        "figure_notice": str(summary.get("figure_notice") or ""),
    }


# ---------------------------------------------------------------------------
# 编辑初审
# ---------------------------------------------------------------------------

class InitialReviewInput(BaseModel):
    version: int = Field(ge=1)
    decision: str = Field(pattern="^(accept|reject)$")
    reason: str = Field(default="", max_length=2000)
    source_checked: bool = False
    privacy_checked: bool = False
    completeness_checked: bool = False
    # 兼容编辑台另一套字段命名（source_ok/privacy_ok/integrity_ok），语义与上面三项一致。
    source_ok: bool | None = None
    privacy_ok: bool | None = None
    integrity_ok: bool | None = None

    def checked_flags(self) -> dict[str, bool]:
        source = bool(self.source_checked or self.source_ok)
        privacy = bool(self.privacy_checked or self.privacy_ok)
        completeness = bool(self.completeness_checked or self.integrity_ok)
        return {
            "source_checked": source,
            "privacy_checked": privacy,
            "completeness_checked": completeness,
        }


@router.post("/submissions/{submission_id}/initial-review")
def initial_review(submission_id: int, data: InitialReviewInput,
                   user=Depends(service.require_editor)):
    """编辑初审来源/隐私/完整性：通过 → 社区待核验（7 天评审窗口）；拒绝 → 退回。"""
    db.ensure_tables()
    with connection() as con:
        write_lock(con, "plaza-write")
        row = db.load_submission(con, submission_id)
        if row is None:
            raise HTTPException(404, "未找到该投稿")
        if int(row["author_id"]) == int(user["id"]):
            raise HTTPException(403, "编辑回避：不能初审本人投稿")
        status = str(row.get("status") or "")
        if status != "pending_initial":
            raise HTTPException(409, {
                "message": "仅待初审投稿可做初审",
                "status": status,
                "status_label": db.STATUS_LABELS.get(status, status),
            })
        if int(data.version) != int(row.get("current_version") or 1):
            raise HTTPException(409, "初审只针对当前版本，请刷新后重试")
        if data.decision == "reject":
            reason = data.reason.strip()
            if len(reason) < 2:
                raise HTTPException(422, "退回投稿必须填写原因（便于作者修订）")
            changed = con.execute(
                "UPDATE plaza_submissions SET status='rejected', updated_at=? "
                "WHERE id=? AND status='pending_initial' AND version=?",
                (utcnow(), submission_id, int(row.get("version") or 1)),
            ).rowcount
            if not changed:
                raise HTTPException(409, "投稿已被其他操作修改（状态冲突），请刷新后重试")
            db.record_event(
                con, submission_id, int(user["id"]), "initial_review_rejected",
                "pending_initial", "rejected", reason=reason,
                payload={"version": int(row.get("version") or 1)},
            )
            notify.notify(
                con, int(row["author_id"]), "initial_review_rejected",
                "初审未通过，可修订后重新送审",
                f"编辑初审未通过。原因：{reason}；修订内容或来源声明后可改版重新送审。",
                submission_id=int(submission_id),
                public_id=str(row.get("public_id") or ""),
                link=notify.submission_link(int(submission_id)),
                payload={"version": int(row.get("version") or 1)},
            )
            figures_summary: dict | None = None
        else:
            flags = data.checked_flags()
            missing = [key for key, checked in flags.items() if not checked]
            if missing:
                raise HTTPException(422, {
                    "message": "初审项未完成：需逐项确认来源、隐私与完整性",
                    "missing": missing,
                })
            if not bool(row.get("license_confirmed")):
                raise HTTPException(422, "来源与授权声明未确认，不能进入社区核验")
            checks = _checks_for(row)
            blocking = service.blocking_checks(checks)
            if blocking:
                raise HTTPException(422, {
                    "message": "投稿仍存在阻断项，不能进入社区核验",
                    "blocking": [check["key"] for check in blocking],
                })
            deadline = db.iso_in_days(db.DEFAULT_REVIEW_DAYS)
            # 初审通过即进入「社区待核验」，广场可浏览：同时分配公开题号并登记公开衍生图。
            public_id = str(row.get("public_id") or "") or db.new_public_id()
            changed = con.execute(
                "UPDATE plaza_submissions SET status='community_review', public_id=?, "
                "review_deadline_at=?, updated_at=? "
                "WHERE id=? AND status='pending_initial' AND version=?",
                (public_id, deadline, utcnow(), submission_id,
                 int(row.get("version") or 1)),
            ).rowcount
            if not changed:
                raise HTTPException(409, "投稿已被其他操作修改（状态冲突），请刷新后重试")
            updated = db.load_submission(con, submission_id)
            figures_summary = _register_figures(con, updated)
            db.record_event(
                con, submission_id, int(user["id"]), "initial_review_accepted",
                "pending_initial", "community_review",
                payload={
                    "version": int(row.get("version") or 1),
                    "deadline": deadline,
                    "public_id": public_id,
                    "checked": {
                        "source": True, "privacy": True, "completeness": True,
                    },
                    "figures": _figure_event_payload(figures_summary),
                },
            )
            notify.notify(
                con, int(row["author_id"]), "initial_review_approved",
                "初审通过，进入社区核验",
                "投稿已通过编辑初审并进入社区待核验；评审窗口 7 天，结果会在通知中心通知你。",
                submission_id=int(submission_id), public_id=public_id,
                link=notify.public_link(public_id),
                payload={"version": int(row.get("version") or 1),
                         "deadline": deadline},
            )
        updated = db.load_submission(con, submission_id)
        payload = service.submission_detail(con, updated, user)
    result = {"submission": payload}
    if figures_summary is not None:
        result["figures"] = figures.summary_for_editor(figures_summary)
    return result


# ---------------------------------------------------------------------------
# 评审队列（按学科分派、作者回避、隐藏他人投票）
# ---------------------------------------------------------------------------

@router.get("/review/queue")
def review_queue(
    subject: str | None = Query(default=None, max_length=40),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=MAX_PAGE_SIZE),
    user=Depends(service.require_reviewer),
):
    db.ensure_tables()
    user_id = int(user["id"])
    is_admin = str(user.get("role") or "") == "admin"
    with connection() as con:
        names = db.role_names(con, user_id)
        scope_subjects: set[str] | None
        if is_admin or ("editor" in names and not names.intersection(db.REVIEWER_ROLES)):
            scope_subjects = None  # 管理员/编辑不受学科分派限制
        else:
            scope_subjects = db.reviewer_subjects(con, user_id)
        requested = (subject or "").strip()
        if requested:
            if scope_subjects is not None and requested not in scope_subjects:
                raise HTTPException(403, "该学科不在你的评审授权范围内")
            allowed = {requested}
        else:
            allowed = scope_subjects
        where = ["status='community_review'", "author_id<>?"]
        params: list = [user_id]
        if allowed is not None:
            if not allowed:
                return {"items": [], "total": 0, "page": page, "page_size": page_size}
            placeholders = ", ".join("?" for _ in allowed)
            where.append(f"subject IN ({placeholders})")
            params.extend(sorted(allowed))
        where.append(
            "NOT EXISTS (SELECT 1 FROM plaza_reviews r WHERE r.submission_id=plaza_submissions.id "
            "AND r.version=plaza_submissions.current_version AND r.reviewer_id=?)"
        )
        params.append(user_id)
        clause = " AND ".join(where)
        total = int(con.execute(
            f"SELECT COUNT(*) FROM plaza_submissions WHERE {clause}", tuple(params)
        ).fetchone()[0])
        rows = con.execute(
            f"SELECT * FROM plaza_submissions WHERE {clause} "
            "ORDER BY review_deadline_at ASC, id ASC LIMIT ? OFFSET ?",
            (*params, page_size, (page - 1) * page_size),
        ).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            snapshot = db.json_load(item.get("snapshot_json"), {})
            if not isinstance(snapshot, dict):
                snapshot = {}
            items.append({
                "submission_id": int(item["id"]),
                "version": int(item.get("current_version") or 1),
                "subject": item.get("subject") or "",
                "type": item.get("type") or "",
                "grade": item.get("grade") or "",
                "difficulty": snapshot.get("difficulty"),
                "knowledge_tags": list(snapshot.get("knowledge_tags") or []),
                "stem_preview": service.stem_preview(snapshot, 160),
                "review_deadline_at": item.get("review_deadline_at"),
                "deadline_passed": service.deadline_passed(item),
                "review_hidden": True,
                "notice": "先独立作答或说明论证再提交评审；提交前看不到他人投票与意见。",
            })
    return {"items": items, "total": total, "page": page, "page_size": page_size}


# ---------------------------------------------------------------------------
# 我的评审历史（评审台「我的评审」）
# ---------------------------------------------------------------------------

@router.get("/review/mine")
def my_reviews(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=MAX_PAGE_SIZE),
    user=Depends(service.require_reviewer),
):
    """我的评审历史：本人提交过的结构化评审与对应投稿当前状态（作者回避，不含他人票）。"""
    db.ensure_tables()
    user_id = int(user["id"])
    with connection() as con:
        total = int(con.execute(
            "SELECT COUNT(*) FROM plaza_reviews WHERE reviewer_id=?", (user_id,)
        ).fetchone()[0])
        rows = con.execute(
            "SELECT r.id, r.submission_id, r.version, r.verdict, r.reasons, "
            "r.independent_answer, r.reviewer_role, r.conflict_declared, r.created_at, "
            "s.public_id, s.status, s.subject, s.type, s.grade, s.snapshot_json, "
            "s.current_version "
            "FROM plaza_reviews r JOIN plaza_submissions s ON s.id=r.submission_id "
            "WHERE r.reviewer_id=? ORDER BY r.id DESC LIMIT ? OFFSET ?",
            (user_id, page_size, (page - 1) * page_size),
        ).fetchall()
        items = []
        for raw in rows:
            item = dict(raw)
            snapshot = db.json_load(item.get("snapshot_json"), {})
            if not isinstance(snapshot, dict):
                snapshot = {}
            status = str(item.get("status") or "")
            review_version = int(item.get("version") or 1)
            current_version = int(item.get("current_version") or 1)
            items.append({
                "review_id": int(item.get("id") or 0),
                "submission_id": int(item.get("submission_id") or 0),
                "public_id": item.get("public_id"),
                "version": review_version,
                "current_version": current_version,
                "is_current_version": review_version == current_version,
                "verdict": str(item.get("verdict") or ""),
                "reviewer_role": str(item.get("reviewer_role") or ""),
                "reasons": str(item.get("reasons") or ""),
                "independent_answer": str(item.get("independent_answer") or ""),
                "conflict_declared": bool(item.get("conflict_declared")),
                "status": status,
                "status_label": db.STATUS_LABELS.get(status, status),
                "subject": item.get("subject") or "",
                "type": item.get("type") or "",
                "grade": item.get("grade") or "",
                "stem_preview": service.stem_preview(snapshot, 160),
                "created_at": str(item.get("created_at") or ""),
                "reviewed_at": str(item.get("created_at") or ""),
            })
    return {"items": items, "total": total, "page": page, "page_size": page_size}

class ReviewInput(BaseModel):
    version: int = Field(ge=1)
    verdict: str = Field(pattern="^(approve|revise|reject|unsure)$")
    reasons: str = Field(default="", max_length=4000)
    independent_answer: str = Field(default="", max_length=8000)
    conflict_declared: bool = False


@router.post("/submissions/{submission_id}/reviews", status_code=201)
def submit_review(submission_id: int, data: ReviewInput,
                  user=Depends(service.require_reviewer)):
    """提交结构化评审：一人一版本一票；达到阈值时自动流转。"""
    db.ensure_tables()
    user_id = int(user["id"])
    with connection() as con:
        write_lock(con, "plaza-write")
        row = db.load_submission(con, submission_id)
        if row is None:
            raise HTTPException(404, "未找到该投稿")
        if int(row["author_id"]) == user_id:
            raise HTTPException(403, "作者回避：不能评审自己的投稿")
        status = str(row.get("status") or "")
        if status != "community_review":
            raise HTTPException(409, {
                "message": "当前状态不可评审",
                "status": status,
                "status_label": db.STATUS_LABELS.get(status, status),
            })
        version = int(row.get("current_version") or 1)
        if int(data.version) != version:
            raise HTTPException(409, "评审只针对当前版本；旧版本投票已留档且不再计入")
        is_admin = str(user.get("role") or "") == "admin"
        names = db.role_names(con, user_id)
        subject = str(row.get("subject") or "")
        subject_reviewer = any(
            str(item.get("role") or "") == "subject_reviewer"
            and str(item.get("subject") or "") == subject
            for item in db.active_roles(con, user_id)
        )
        if is_admin and not subject_reviewer:
            # 管理员没有学科复核资格时不冒充学科复核票。
            subjects = db.reviewer_subjects(con, user_id)
            subject_reviewer = subject in subjects
        if not is_admin and "editor" not in names:
            if subject not in db.reviewer_subjects(con, user_id):
                raise HTTPException(403, "该题不在你的学科评审授权范围内")
        existing = con.execute(
            "SELECT * FROM plaza_reviews WHERE submission_id=? AND version=? AND reviewer_id=?",
            (submission_id, version, user_id),
        ).fetchone()
        if existing is not None:
            raise HTTPException(409, {
                "message": "你已评审过该版本投稿（一人一版本一票）",
                "review_id": int(dict(existing).get("id") or 0),
                "review": _review_payload(dict(existing)),
            })
        reasons = data.reasons.strip()
        independent_answer = data.independent_answer.strip()
        if data.verdict in ("revise", "reject", "unsure") and len(reasons) < 2:
            raise HTTPException(422, "需修订 / 拒绝 / 无法判断必须填写简短原因")
        if data.verdict == "approve" and len(independent_answer) < 2:
            raise HTTPException(422, "通过结论需先独立作答或说明论证")
        reviewer_role = "subject_reviewer" if subject_reviewer else "reviewer"
        review_id = db.insert_row(con, "plaza_reviews", {
            "submission_id": int(submission_id),
            "version": version,
            "reviewer_id": user_id,
            "reviewer_role": reviewer_role,
            "verdict": data.verdict,
            "reasons": reasons[:4000],
            "independent_answer": independent_answer[:8000],
            "conflict_declared": 1 if data.conflict_declared else 0,
            "created_at": utcnow(),
        })
        db.record_event(
            con, submission_id, user_id, "review_submitted", status, status,
            payload={
                "version": version,
                "verdict": data.verdict,
                "reviewer_role": reviewer_role,
                "conflict_declared": bool(data.conflict_declared),
            },
        )
        summary = service.review_summary(con, row)
        transition = ""
        if not data.conflict_declared and summary["blocking"] and reviewer_role == "subject_reviewer":
            changed = con.execute(
                "UPDATE plaza_submissions SET status='disputed', updated_at=? "
                "WHERE id=? AND status='community_review' AND version=?",
                (utcnow(), submission_id, version),
            ).rowcount
            if changed:
                transition = "disputed"
                db.record_event(
                    con, submission_id, user_id, "disputed", status, "disputed",
                    reason=reasons[:2000],
                    payload={"version": version, "review_id": review_id,
                             "review_summary": summary},
                )
                notify.notify(
                    con, int(row["author_id"]), "submission_disputed",
                    "投稿被提出争议，暂停公开展示",
                    "社区评审提出有效的答案争议或内容问题，题目已暂停公开展示；"
                    "请修订后重新送审，处理进展在投稿台查看。",
                    submission_id=int(submission_id),
                    public_id=str(row.get("public_id") or ""),
                    link=notify.submission_link(int(submission_id)),
                    payload={"version": version,
                             "reasons": reasons[:500]},
                )
        elif not data.conflict_declared and summary["threshold_met"]:
            public_id = str(row.get("public_id") or "") or db.new_public_id()
            changed = con.execute(
                "UPDATE plaza_submissions SET status='verified', public_id=?, updated_at=? "
                "WHERE id=? AND status='community_review' AND version=?",
                (public_id, utcnow(), submission_id, version),
            ).rowcount
            if changed:
                transition = "verified"
                fresh = db.load_submission(con, submission_id)
                # 公开衍生图在初审时已登记；这里再兜底一次（幂等），兼容历史数据。
                figure_summary = _register_figures(con, fresh)
                db.record_event(
                    con, submission_id, user_id, "threshold_met", status, "verified",
                    payload={"version": version, "public_id": public_id,
                             "review_summary": summary,
                             "figures": _figure_event_payload(figure_summary)},
                )
                notify.notify(
                    con, int(row["author_id"]), "submission_verified",
                    "社区核验通过，等待平台签发",
                    "该题已由社区评审核验通过（达到阈值），等待平台编辑签发精选；"
                    "可以在广场查看当前状态。",
                    submission_id=int(submission_id), public_id=public_id,
                    link=notify.public_link(public_id),
                    payload={"version": version, "review_summary": summary},
                )
                # 参与独立评审的评审员本人得到核验结论（作者回避，不泄露其他人身份）。
                reviewer_ids = [
                    int(item[0]) for item in con.execute(
                        "SELECT DISTINCT reviewer_id FROM plaza_reviews "
                        "WHERE submission_id=? AND version=? AND conflict_declared=0",
                        (submission_id, version),
                    ).fetchall()
                ]
                notify.notify_many(
                    con, reviewer_ids, "submission_verified",
                    "你评审的题目已通过社区核验",
                    "你参与独立评审的题目已达到核验阈值并通过，感谢你的评审。",
                    submission_id=int(submission_id), public_id=public_id,
                    link=notify.review_link(),
                    payload={"version": version},
                    exclude=(int(row["author_id"]),),
                )
        review = con.execute("SELECT * FROM plaza_reviews WHERE id=?", (review_id,)).fetchone()
        updated = db.load_submission(con, submission_id)
        payload = {
            "review": _review_payload(dict(review)),
            "review_summary": summary,
            "transition": transition or "pending",
            "submission": {
                "id": int(updated["id"]),
                "status": str(updated.get("status") or ""),
                "status_label": db.STATUS_LABELS.get(str(updated.get("status") or ""), ""),
                "version": int(updated.get("current_version") or 1),
                "public_id": updated.get("public_id"),
            },
        }
    return payload
