"""广场作者端接口：投稿创建、预览校验、送审、改版、撤回与我的投稿。

作者端只处理「个人题 → 不可变投稿快照」的固定版本链路；每次内容或声明实质变化都
创建新版本（``version``/``current_version`` 同步递增，历史版本留档），避免旧评审票
被悄悄复用。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from ..auth import current_user
from ..db import connection, utcnow, write_lock
from . import db, favorites, notify, service

router = APIRouter()

MAX_PAGE_SIZE = 50


def _author_row(con, submission_id: int, user: dict) -> dict:
    row = db.load_submission(con, submission_id)
    if row is None or int(row["author_id"]) != int(user["id"]):
        raise HTTPException(404, "未找到该投稿")
    return row


def _confirmed_source(source: dict, user_id: int) -> dict:
    value = dict(source)
    value["confirmed_by_user_id"] = int(user_id)
    value["confirmed_at"] = utcnow()
    return value


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


def _response(con, row: dict, user: dict, **extra) -> dict:
    checks = _checks_for(row)
    payload = {
        "submission": service.submission_detail(con, row, user),
        "checks": service.checks_payload(checks),
    }
    payload.update(extra)
    return payload


def _apply_version(con, row: dict, snapshot: dict, source: dict, license_confirmed: bool,
                   *, status: str, version: int, user_id: int, action: str,
                   event_reason: str = "", payload: dict | None = None) -> dict:
    """把新版本写入主表 + 版本留档 + 事件（调用方已持有 plaza 写锁与 CAS 预期）。"""
    fingerprint = service.snapshot_fingerprint(snapshot)
    now = utcnow()
    changed = con.execute(
        "UPDATE plaza_submissions SET version=?, current_version=?, snapshot_json=?, "
        "source_json=?, fingerprint=?, license_confirmed=?, status=?, "
        "review_deadline_at=NULL, updated_at=? "
        "WHERE id=? AND author_id=? AND status=? AND version=?",
        (
            int(version), int(version), db.json_dump(snapshot), db.json_dump(source),
            fingerprint, 1 if license_confirmed else 0, status, now,
            int(row["id"]), int(user_id), str(row.get("status") or ""),
            int(row.get("version") or 1),
        ),
    ).rowcount
    if not changed:
        raise HTTPException(409, "投稿已被其他操作修改（版本冲突），请刷新后重试")
    db.insert_row(con, "plaza_submission_versions", {
        "submission_id": int(row["id"]),
        "version": int(version),
        "snapshot_json": db.json_dump(snapshot),
        "source_json": db.json_dump(source),
        "fingerprint": fingerprint,
        "license_confirmed": 1 if license_confirmed else 0,
        "created_by": int(user_id),
        "created_at": now,
    })
    db.record_event(
        con, int(row["id"]), int(user_id), action,
        str(row.get("status") or ""), status,
        reason=event_reason, payload=payload or {},
    )
    return db.load_submission(con, int(row["id"])) or row


# ---------------------------------------------------------------------------
# 创建 / 预览
# ---------------------------------------------------------------------------

class SubmissionCreateInput(BaseModel):
    question_id: int = Field(ge=1)
    source: dict = Field(default_factory=dict)
    license_confirmed: bool = False


@router.post("/submissions", status_code=201)
def create_submission(data: SubmissionCreateInput, user=Depends(current_user)):
    """从个人题生成不可变快照并创建投稿草稿，返回预览与逐项校验结果。"""
    db.ensure_tables()
    question = db.load_own_question(data.question_id, user["id"])
    if question is None:
        raise HTTPException(404, "未找到可投稿的个人题目")
    snapshot = service.build_snapshot(question)
    fingerprint = service.snapshot_fingerprint(snapshot)
    source = _confirmed_source(service.normalize_source(data.source), user["id"])
    now = utcnow()
    with connection() as con:
        write_lock(con, "plaza-write")
        submission_id = db.insert_row(con, "plaza_submissions", {
            "author_id": int(user["id"]),
            "question_id": int(question["id"]),
            "version": 1,
            "current_version": 1,
            "subject": str(snapshot.get("subject") or ""),
            "type": str(snapshot.get("type") or ""),
            "grade": str(snapshot.get("grade") or ""),
            "snapshot_json": db.json_dump(snapshot),
            "source_json": db.json_dump(source),
            "fingerprint": fingerprint,
            "license_confirmed": 1 if data.license_confirmed else 0,
            "status": "draft",
            "public_id": None,
            "review_deadline_at": None,
            "created_at": now,
            "updated_at": now,
        })
        db.insert_row(con, "plaza_submission_versions", {
            "submission_id": submission_id,
            "version": 1,
            "snapshot_json": db.json_dump(snapshot),
            "source_json": db.json_dump(source),
            "fingerprint": fingerprint,
            "license_confirmed": 1 if data.license_confirmed else 0,
            "created_by": int(user["id"]),
            "created_at": now,
        })
        db.record_event(
            con, submission_id, int(user["id"]), "created", "", "draft",
            payload={"version": 1, "question_id": int(question["id"])},
        )
        row = db.load_submission(con, submission_id)
        content = snapshot.get("content") if isinstance(snapshot.get("content"), dict) else {}
        payload = _response(con, row, user, preview={
            "subject": snapshot.get("subject") or "",
            "type": snapshot.get("type") or "",
            "grade": snapshot.get("grade") or "",
            "stem_preview": service.stem_preview(snapshot),
            "answer_count": len(content.get("answers") or []),
            "image_count": len(content.get("images") or []),
            "source_declaration": service.public_source(source),
            "notice": "投稿将固定当前个人题版本；公开前请核对来源与授权声明。",
        })
    return payload


# ---------------------------------------------------------------------------
# 我的投稿 / 详情
# ---------------------------------------------------------------------------

@router.get("/submissions/mine")
def my_submissions(
    status: str | None = Query(default=None, max_length=20),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=MAX_PAGE_SIZE),
    user=Depends(current_user),
):
    db.ensure_tables()
    where = ["author_id=?"]
    params: list = [int(user["id"])]
    if status:
        if status not in db.SUBMISSION_STATUSES:
            raise HTTPException(422, "未知的投稿状态")
        where.append("status=?")
        params.append(status)
    clause = " AND ".join(where)
    with connection() as con:
        total = int(con.execute(
            f"SELECT COUNT(*) FROM plaza_submissions WHERE {clause}", tuple(params)
        ).fetchone()[0])
        rows = con.execute(
            f"SELECT * FROM plaza_submissions WHERE {clause} ORDER BY updated_at DESC, id DESC "
            "LIMIT ? OFFSET ?",
            (*params, page_size, (page - 1) * page_size),
        ).fetchall()
        items = [service.author_list_item(con, dict(row)) for row in rows]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@router.get("/submissions/{submission_id}")
def get_submission(submission_id: int, user=Depends(current_user)):
    """作者/评审/编辑可见的投稿详情；按视角隐藏他人投票与内部信息。"""
    db.ensure_tables()
    with connection() as con:
        row = db.load_submission(con, submission_id)
        if row is None:
            raise HTTPException(404, "未找到该投稿")
        if not service.can_view_submission(con, row, user):
            raise HTTPException(403, "无权查看该投稿")
        payload = _response(con, row, user)
    return payload


# ---------------------------------------------------------------------------
# 草稿修改（创建新版本）
# ---------------------------------------------------------------------------

class SubmissionPatchInput(BaseModel):
    version: int = Field(ge=1)
    source: dict | None = None
    license_confirmed: bool | None = None


@router.patch("/submissions/{submission_id}")
def patch_submission(submission_id: int, data: SubmissionPatchInput, user=Depends(current_user)):
    """仅 ``draft`` 可改；内容或声明实质变化时创建新版本并留档旧版本。"""
    db.ensure_tables()
    with connection() as con:
        write_lock(con, "plaza-write")
        row = _author_row(con, submission_id, user)
        if str(row.get("status") or "") != "draft":
            raise HTTPException(409, "仅草稿可修改；已送审内容请使用改版接口创建新版本")
        if int(data.version) != int(row.get("version") or 1):
            raise HTTPException(409, "投稿已被修改（版本冲突），请刷新后重试")
        old_source = db.json_load(row.get("source_json"), {})
        if not isinstance(old_source, dict):
            old_source = {}
        question = db.load_own_question(int(row["question_id"]), user["id"])
        if question is None:
            raise HTTPException(404, "原始个人题目已删除，无法更新投稿")
        snapshot = service.build_snapshot(question)
        new_source = _confirmed_source(
            service.normalize_source(data.source or {}, existing=old_source), user["id"]
        )
        license_confirmed = (
            bool(data.license_confirmed) if data.license_confirmed is not None
            else bool(row.get("license_confirmed"))
        )
        old_plain_source = service.normalize_source({}, existing=old_source)
        new_plain_source = service.normalize_source({}, existing=new_source)
        fingerprint = service.snapshot_fingerprint(snapshot)
        changed_content = fingerprint != str(row.get("fingerprint") or "")
        changed_source = new_plain_source != old_plain_source
        changed_license = license_confirmed != bool(row.get("license_confirmed"))
        if not (changed_content or changed_source or changed_license):
            return _response(con, row, user, changed=False,
                             message="内容与当前版本一致，未创建新版本")
        new_version = db.next_version(con, int(row["id"]), row)
        updated = _apply_version(
            con, row, snapshot, new_source, license_confirmed,
            status="draft", version=new_version, user_id=int(user["id"]),
            action="updated",
            payload={
                "from_version": int(row.get("version") or 1),
                "to_version": new_version,
                "content_changed": changed_content,
                "source_changed": changed_source,
                "license_confirmed": license_confirmed,
            },
        )
        payload = _response(con, updated, user, changed=True, message="已创建新版本")
    return payload


# ---------------------------------------------------------------------------
# 送审 / 改版 / 撤回
# ---------------------------------------------------------------------------

class SubmitInput(BaseModel):
    version: int = Field(ge=1)
    confirm_preview: bool = False
    license_confirmed: bool | None = None


@router.post("/submissions/{submission_id}/submit")
def submit_submission(submission_id: int, data: SubmitInput, user=Depends(current_user)):
    """送审：校验预览确认 + 来源/授权声明完整 + 无阻断项，流转到「待初审」。"""
    db.ensure_tables()
    with connection() as con:
        write_lock(con, "plaza-write")
        row = _author_row(con, submission_id, user)
        status = str(row.get("status") or "")
        if status != "draft":
            message = {
                "pending_initial": "投稿已在待初审，请勿重复提交",
                "community_review": "投稿已在社区待核验",
                "verified": "投稿已通过社区核验",
                "pending_release": "投稿待平台发布",
                "published": "投稿已发布",
                "withdrawn": "投稿已撤回",
                "disputed": "投稿有争议，请先改版",
                "rejected": "初审未通过，请使用改版接口修改后重新提交",
            }.get(status, "当前状态不可提交")
            raise HTTPException(409, message)
        if int(data.version) != int(row.get("version") or 1):
            raise HTTPException(409, "投稿已被修改（版本冲突），请刷新后重试")
        license_confirmed = (
            bool(data.license_confirmed) if data.license_confirmed is not None
            else bool(row.get("license_confirmed"))
        )
        source = db.json_load(row.get("source_json"), {})
        if not isinstance(source, dict):
            source = {}
        checks = _checks_for({**row, "license_confirmed": 1 if license_confirmed else 0})
        blocking = service.blocking_checks(checks)
        if not data.confirm_preview:
            raise HTTPException(422, {
                "message": "请先确认投稿预览（将公开的题干、答案、解析与来源声明）",
                "checklist": [check["key"] for check in checks],
            })
        if blocking:
            raise HTTPException(422, {
                "message": "投稿校验未通过，请先补全阻断项",
                "blocking": [check["key"] for check in blocking],
                "hints": [check["hint"] for check in blocking],
            })
        new_source = _confirmed_source(service.normalize_source({}, existing=source), user["id"])
        now = utcnow()
        changed = con.execute(
            "UPDATE plaza_submissions SET status=?, license_confirmed=?, source_json=?, "
            "updated_at=? WHERE id=? AND author_id=? AND status='draft' AND version=?",
            ("pending_initial", 1, db.json_dump(new_source), now, submission_id,
             int(user["id"]), int(row.get("version") or 1)),
        ).rowcount
        if not changed:
            raise HTTPException(409, "投稿已被其他操作修改，请刷新后重试")
        db.record_event(
            con, submission_id, int(user["id"]), "submitted", "draft", "pending_initial",
            payload={"version": int(row.get("version") or 1)},
        )
        notify.notify(
            con, int(row["author_id"]), "submission_submitted",
            "投稿已送审",
            "投稿已提交，等待平台编辑初审；初审结果会在通知中心通知你。",
            submission_id=submission_id,
            public_id=str(row.get("public_id") or ""),
            link=notify.submission_link(submission_id),
            payload={"version": int(row.get("version") or 1)},
        )
        updated = db.load_submission(con, submission_id)
        payload = _response(con, updated, user)
    return payload


class ReviseInput(BaseModel):
    version: int = Field(ge=1)
    source: dict | None = None
    license_confirmed: bool | None = None


@router.post("/submissions/{submission_id}/revise")
def revise_submission(submission_id: int, data: ReviseInput, user=Depends(current_user)):
    """送审后改版：创建新版本并回到待评（旧版本评审票留档、不再计入阈值）。"""
    db.ensure_tables()
    with connection() as con:
        write_lock(con, "plaza-write")
        row = _author_row(con, submission_id, user)
        status = str(row.get("status") or "")
        if status not in db.REVISABLE_STATUSES:
            raise HTTPException(409, "当前状态不可改版；请先撤回或等待审核结论")
        if int(data.version) != int(row.get("version") or 1):
            raise HTTPException(409, "投稿已被修改（版本冲突），请刷新后重试")
        question = db.load_own_question(int(row["question_id"]), user["id"])
        if question is None:
            raise HTTPException(404, "原始个人题目已删除，无法改版")
        snapshot = service.build_snapshot(question)
        old_source = db.json_load(row.get("source_json"), {})
        if not isinstance(old_source, dict):
            old_source = {}
        new_source = _confirmed_source(
            service.normalize_source(data.source or {}, existing=old_source), user["id"]
        )
        license_confirmed = (
            bool(data.license_confirmed) if data.license_confirmed is not None
            else bool(row.get("license_confirmed"))
        )
        fingerprint = service.snapshot_fingerprint(snapshot)
        changed_content = fingerprint != str(row.get("fingerprint") or "")
        changed_source = service.normalize_source({}, existing=old_source) != \
            service.normalize_source({}, existing=new_source)
        changed_license = license_confirmed != bool(row.get("license_confirmed"))
        if not (changed_content or changed_source or changed_license):
            raise HTTPException(422, {
                "message": "内容与当前版本一致，请先修改个人题或声明后再创建新版本",
            })
        new_version = db.next_version(con, int(row["id"]), row)
        new_status = "draft" if status == "rejected" else "pending_initial"
        updated = _apply_version(
            con, row, snapshot, new_source, license_confirmed,
            status=new_status, version=new_version, user_id=int(user["id"]),
            action="revised",
            payload={
                "from_version": int(row.get("version") or 1),
                "to_version": new_version,
                "previous_status": status,
                "content_changed": changed_content,
                "source_changed": changed_source,
                "notice": "新版本从待评开始，旧版本评审票留档但不计入阈值。",
            },
        )
        payload = _response(con, updated, user, changed=True)
        notify.notify(
            con, int(row["author_id"]), "submission_revised",
            "投稿已创建新版本",
            "新版本从待评开始，旧版本评审票留档但不计入阈值。",
            submission_id=int(submission_id),
            public_id=str(updated.get("public_id") or ""),
            link=notify.submission_link(int(submission_id)),
            payload={"version": int(updated.get("current_version") or new_version)},
        )
    return payload


class WithdrawInput(BaseModel):
    reason: str = Field(default="", max_length=2000)


@router.post("/submissions/{submission_id}/withdraw")
def withdraw_submission(submission_id: int, data: WithdrawInput | None = None,
                        user=Depends(current_user)):
    """作者撤回：未发布前可主动撤回；已发布题目必须走发布端整条链撤回。"""
    db.ensure_tables()
    reason = (data.reason if data is not None else "") or ""
    with connection() as con:
        write_lock(con, "plaza-write")
        row = _author_row(con, submission_id, user)
        status = str(row.get("status") or "")
        if status == "withdrawn":
            raise HTTPException(409, "投稿已撤回，请勿重复操作")
        if status == "published":
            raise HTTPException(409, "已发布题目请由平台编辑撤回（发布端整条链）")
        if status not in db.WITHDRAWABLE_STATUSES:
            raise HTTPException(409, "当前状态不可撤回")
        changed = con.execute(
            "UPDATE plaza_submissions SET status='withdrawn', updated_at=? "
            "WHERE id=? AND author_id=? AND status=? AND version=?",
            (utcnow(), submission_id, int(user["id"]), status,
             int(row.get("version") or 1)),
        ).rowcount
        if not changed:
            raise HTTPException(409, "投稿已被其他操作修改，请刷新后重试")
        db.record_event(
            con, submission_id, int(user["id"]), "author_withdrawn", status, "withdrawn",
            reason=reason, payload={"version": int(row.get("version") or 1)},
        )
        if str(row.get("public_id") or "") and status in db.PUBLIC_STATUSES:
            # 作者撤回公开稿件：收藏者需要知道该题不再公开（同一事务内通知）。
            favorites.notify_favoriters(
                con, row, "favorite_withdrawn",
                "你收藏的题目已被作者撤回",
                "你收藏的题目已被作者撤回，公开内容不再展示；收藏记录仍保留。",
            )
        updated = db.load_submission(con, submission_id)
        payload = _response(con, updated, user)
    return payload
