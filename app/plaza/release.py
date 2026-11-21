"""广场发布端接口：编辑签发精选、撤回整条链、发布历史与事件链。

发布/撤回都要求：

* 只允许平台编辑或管理员签发；签发者回避本人投稿与本人承担过复核的题；
* 发布只在 ``verified`` / ``pending_release``（评审阈值已达成）上进行；
* 发布与撤回在同一事务内写发布指针与事件，重复请求返回幂等结果；
* 撤回立即影响公开读取（公开接口以 ``status`` / 活跃发布指针为准）。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from ..auth import current_user
from ..db import connection, utcnow, write_lock
from . import db, favorites, figures, gate, notify, service

router = APIRouter()

MAX_PAGE_SIZE = 100


def _release_public(con, release: dict) -> dict:
    submission = db.load_submission(con, int(release["submission_id"])) or {}
    snapshot = db.json_load(submission.get("snapshot_json"), {})
    if not isinstance(snapshot, dict):
        snapshot = {}
    return {
        "id": int(release["id"]),
        "public_id": str(release.get("public_id") or ""),
        "submission_id": int(release["submission_id"]),
        "version": int(release.get("version") or 1),
        "status": str(release.get("status") or ""),
        "subject": snapshot.get("subject") or "",
        "type": snapshot.get("type") or "",
        "grade": snapshot.get("grade") or "",
        "published_by": release.get("published_by"),
        "published_at": release.get("published_at"),
        "withdrawn_at": release.get("withdrawn_at"),
        "withdraw_mode": str(release.get("withdraw_mode") or ""),
        "reason": str(release.get("reason") or ""),
    }


def _ensure_figures(con, row: dict) -> dict:
    """发布前登记/刷新公开衍生图；异常降级为空摘要（配图问题不阻断签发）。"""
    try:
        return figures.ensure_public_figures(con, row)
    except Exception:  # noqa: BLE001 - 图片登记失败不能回滚发布
        return {
            "version": int((row or {}).get("current_version") or 1),
            "total": 0, "public": 0, "unavailable": 0, "items": [],
            "notices": [], "figure_notice": "部分配图暂不可公开查看", "blocking": False,
        }


# ---------------------------------------------------------------------------
# 撤回内部函数（编辑撤回、抽检严重问题自动撤回共用同一事务链）
# ---------------------------------------------------------------------------

def withdraw_active_release(
    con, release: dict, *, public_id: str, actor_id: int, reason: str,
    mode: str = "normal", event_action: str = "release_withdrawn",
    extra_payload: dict | None = None,
    author_kind: str = "release_withdrawn",
    author_title: str = "", author_body: str = "",
) -> dict:
    """撤回一条活跃发布（调用方已持有 ``plaza-write`` 写锁）。

    同一事务内：发布指针置 withdrawn、投稿状态置 withdrawn、写 ``plaza_events``
    审计事件、通知作者与收藏者。重复/并发撤回由 CAS 保证只成功一次。
    """
    now = utcnow()
    reason = str(reason or "").strip()
    changed = con.execute(
        "UPDATE plaza_releases SET status='withdrawn', active_public_id=NULL, "
        "withdrawn_at=?, withdraw_mode=?, reason=? WHERE id=? AND status='active'",
        (now, str(mode or "normal"), reason, int(release["id"])),
    ).rowcount
    if not changed:
        raise HTTPException(409, "发布记录已被其他操作修改，请刷新后重试")
    con.execute(
        "UPDATE plaza_submissions SET status='withdrawn', updated_at=? "
        "WHERE public_id=? AND status='published'",
        (now, str(public_id or "")),
    )
    submission = db.load_submission(con, int(release["submission_id"]))
    version = int(release.get("version") or 1)
    db.record_event(
        con, int(release["submission_id"]), int(actor_id), str(event_action),
        "published", "withdrawn", reason=reason,
        payload={
            "release_id": int(release["id"]),
            "public_id": str(public_id or ""),
            "version": version,
            "mode": str(mode or "normal"),
            **(extra_payload or {}),
        },
    )
    author_notified = False
    if isinstance(submission, dict):
        author_id = int(submission.get("author_id") or 0)
        author_notified = bool(notify.notify(
            con, author_id, author_kind,
            author_title or "发布撤回通知",
            author_body or (
                f"你的投稿已被平台撤回，公开内容不再展示。原因：{reason}"
            ),
            submission_id=int(submission["id"]),
            public_id=str(public_id or ""),
            link=notify.submission_link(int(submission["id"])),
            payload={"release_id": int(release["id"]), "mode": str(mode or "normal"),
                     "reason": reason, "version": version},
        ))
        favorites.notify_favoriters(
            con, submission, "favorite_withdrawn",
            "你收藏的题目已撤回",
            "你收藏的题目已被平台撤回，公开内容不再展示；历史作答与收藏记录仍保留。",
        )
    return {
        "public_id": str(public_id or ""),
        "status": "withdrawn",
        "mode": str(mode or "normal"),
        "withdrawn_at": now,
        "reason": reason,
        "idempotent": False,
        "notified": {"author": author_notified},
    }


# ---------------------------------------------------------------------------
# 签发精选
# ---------------------------------------------------------------------------

class PublishInput(BaseModel):
    version: int = Field(ge=1)
    reason: str = Field(default="", max_length=2000)
    #: 抽检门禁显式 override（仅编辑/管理员可用；必须带 ``override_reason``）。
    quality_gate_override: bool = False
    override_reason: str = Field(default="", max_length=2000)


@router.post("/submissions/{submission_id}/publish")
def publish_submission(submission_id: int, data: PublishInput,
                       user=Depends(service.require_editor)):
    """对指定版本签发精选：写活跃发布指针 + ``publication_event``（同事务）。

    签发前校验抽检门禁（:mod:`app.plaza.gate`）：试点期内该版本必须有独立通过
    （pass）的抽检记录，否则 409 并返回 ``guidance``；编辑可用
    ``quality_gate_override`` + ``override_reason`` 显式放行（写事件与门禁审计，
    指标可查）。发布后抽检 ``high`` 自动撤回的发布可走
    ``POST /releases/{public_id}/restore`` 一键恢复。
    """
    db.ensure_tables()
    user_id = int(user["id"])
    try:
        with connection() as con:
            write_lock(con, "plaza-write")
            row = db.load_submission(con, submission_id)
            if row is None:
                raise HTTPException(404, "未找到该投稿")
            if int(row["author_id"]) == user_id:
                raise HTTPException(403, "签发者回避：不能签发本人投稿")
            reviewed = con.execute(
                "SELECT id FROM plaza_reviews WHERE submission_id=? AND reviewer_id=? LIMIT 1",
                (submission_id, user_id),
            ).fetchone()
            if reviewed is not None:
                raise HTTPException(403, "签发者回避：不能签发本人承担过复核的题目")
            status = str(row.get("status") or "")
            version = int(row.get("current_version") or 1)
            if int(data.version) != version:
                raise HTTPException(409, "签发只针对当前版本；请刷新后重试")
            public_id = str(row.get("public_id") or "")
            if status == "published":
                active = con.execute(
                    "SELECT * FROM plaza_releases WHERE public_id=? AND status='active'",
                    (public_id,),
                ).fetchone()
                if active is not None:
                    figure_summary = _ensure_figures(con, row)
                    gate_cfg = gate.config(con)
                    return {
                        "release": _release_public(con, dict(active)),
                        "submission": {
                            "id": int(row["id"]),
                            "status": "published",
                            "version": version,
                            "public_id": public_id,
                        },
                        "figures": figures.summary_for_editor(figure_summary),
                        "idempotent": True,
                        "quality_gate": {
                            "mode": gate_cfg["mode"],
                            "mode_label": gate_cfg["mode_label"],
                            "armed": bool(gate_cfg["armed"]),
                            "enforced": bool(gate_cfg["enforced"]),
                            "required": bool(gate_cfg["required"]),
                            "blocking": False,
                            "override": False,
                            "message": "该版本已处于发布状态，无需重复签发",
                        },
                        "message": "该版本已处于发布状态，无需重复签发",
                    }
                raise HTTPException(409, "投稿处于已发布但无活跃发布记录的不一致状态，请联系管理员")
            if status not in ("verified", "pending_release"):
                raise HTTPException(409, {
                    "message": "仅社区核验通过（待平台发布）的投稿可签发",
                    "status": status,
                    "status_label": db.STATUS_LABELS.get(status, status),
                })
            if not bool(row.get("license_confirmed")):
                raise HTTPException(409, "来源与授权声明未确认，不能签发")
            summary = service.review_summary(con, row)
            if not summary["threshold_met"]:
                raise HTTPException(409, {
                    "message": "评审阈值未达成，不能签发精选",
                    "review": summary,
                })
            # ---- 抽检门禁（WS28）：试点期内该版本须有独立通过抽检记录 ----
            gate_info = gate.evaluate(con, row, version)
            override_reason = data.override_reason.strip()
            gate_override = False
            if gate_info.get("blocking"):
                if not data.quality_gate_override:
                    raise gate.QualityGateBlocked(
                        int(submission_id), version, gate_info,
                        actor_id=user_id, reason="未通过独立抽检，签发被门禁阻断",
                    )
                if len(override_reason) < 2:
                    raise HTTPException(422, {
                        "message": "抽检门禁 override 必须填写 override_reason（至少 2 个字）",
                        "field": "override_reason",
                        "guidance": gate.OVERRIDE_GUIDANCE,
                    })
                gate_override = True
            if not public_id:
                public_id = db.new_public_id()
            now = utcnow()
            # 门禁审计与 override 事件先写（与签发同事务；签发失败一起回滚）。
            if gate_override:
                db.record_event(
                    con, submission_id, user_id, "quality_gate_override", status, "published",
                    reason=override_reason,
                    payload={
                        "mode": gate_info.get("mode"),
                        "version": version,
                        "latest_check": gate_info.get("latest_check"),
                        "guidance": gate_info.get("guidance") or gate.OVERRIDE_GUIDANCE,
                    },
                )
                gate.record_outcome(
                    con, submission_id, version, str(gate_info.get("mode") or ""),
                    "override", user_id, override_reason,
                )
            elif gate_info.get("enforced") and gate_info.get("required"):
                gate.record_outcome(
                    con, submission_id, version, str(gate_info.get("mode") or ""),
                    "passed", user_id, "该版本已通过独立抽检",
                )
            changed = con.execute(
                "UPDATE plaza_submissions SET status='published', public_id=?, updated_at=? "
                "WHERE id=? AND version=? AND status IN ('verified','pending_release')",
                (public_id, now, submission_id, version),
            ).rowcount
            if not changed:
                raise HTTPException(409, "投稿已被其他操作修改（状态冲突），请刷新后重试")
            release_id = db.insert_row(con, "plaza_releases", {
                "submission_id": int(submission_id),
                "version": version,
                "public_id": public_id,
                "status": "active",
                "active_public_id": public_id,
                "published_by": user_id,
                "published_at": now,
                "withdrawn_at": None,
                "withdraw_mode": "",
                "reason": "",
            })
            published_row = db.load_submission(con, submission_id)
            figure_summary = _ensure_figures(con, published_row or row)
            db.record_event(
                con, submission_id, user_id, "published", status, "published",
                reason=data.reason.strip(),
                payload={
                    "release_id": release_id,
                    "public_id": public_id,
                    "version": version,
                    "review_summary": summary,
                    "quality_gate": {
                        "mode": gate_info.get("mode"),
                        "armed": bool(gate_info.get("armed")),
                        "required": bool(gate_info.get("required")),
                        "override": gate_override,
                        "override_reason": override_reason if gate_override else "",
                        "pass_check_id": (gate_info.get("pass_check") or {}).get("id"),
                    },
                    "figures": {
                        "total": int(figure_summary.get("total") or 0),
                        "public": int(figure_summary.get("public") or 0),
                        "unavailable": int(figure_summary.get("unavailable") or 0),
                        "figure_notice": str(figure_summary.get("figure_notice") or ""),
                    },
                },
            )
            release = con.execute("SELECT * FROM plaza_releases WHERE id=?", (release_id,)).fetchone()
            notify.notify(
                con, int(row["author_id"]), "submission_published",
                "投稿已发布为平台精选",
                "你的投稿已由平台编辑签发为精选，公开详情可在广场查看。",
                submission_id=int(submission_id), public_id=public_id,
                link=notify.public_link(public_id),
                payload={"release_id": release_id, "version": version,
                         "public_url": f"/api/plaza/questions/{public_id}"},
            )
            payload = {
                "release": _release_public(con, dict(release)),
                "submission": {
                    "id": int(submission_id),
                    "status": "published",
                    "version": version,
                    "public_id": public_id,
                },
                "figures": figures.summary_for_editor(figure_summary),
                "idempotent": False,
                "public_url": f"/api/plaza/questions/{public_id}",
                "quality_gate": {
                    "mode": gate_info.get("mode"),
                    "mode_label": gate_info.get("mode_label"),
                    "mode_source": gate_info.get("mode_source"),
                    "armed": bool(gate_info.get("armed")),
                    "enforced": bool(gate_info.get("enforced")),
                    "required": bool(gate_info.get("required")),
                    "window_open": bool(gate_info.get("window_open")),
                    "blocking": False,
                    "satisfied": bool(gate_info.get("satisfied")),
                    "override": gate_override,
                    "override_reason": override_reason if gate_override else "",
                    "latest_check": gate_info.get("latest_check"),
                    "pass_check": gate_info.get("pass_check"),
                    "guidance": gate_info.get("guidance") or "",
                    "message": str(gate_info.get("message") or ""),
                },
            }
    except gate.QualityGateBlocked as blocked:
        # 被阻断的签发事务已回滚：审计记录用独立事务落库后再返回 409。
        gate.record_blocked(blocked)
        raise HTTPException(409, blocked.detail()) from None
    return payload


# ---------------------------------------------------------------------------
# 撤回整条链
# ---------------------------------------------------------------------------

class WithdrawReleaseInput(BaseModel):
    reason: str = Field(min_length=2, max_length=2000)
    mode: str = Field(default="normal", pattern="^(normal|emergency)$")


@router.post("/releases/{public_id}/withdraw")
def withdraw_release(public_id: str, data: WithdrawReleaseInput,
                     user=Depends(service.require_editor)):
    """撤回发布：隐藏公开入口、写事件、保留历史（重复撤回返回幂等结果）。"""
    db.ensure_tables()
    user_id = int(user["id"])
    with connection() as con:
        write_lock(con, "plaza-write")
        release = con.execute(
            "SELECT * FROM plaza_releases WHERE public_id=? AND status='active'",
            (public_id,),
        ).fetchone()
        if release is None:
            any_release = con.execute(
                "SELECT * FROM plaza_releases WHERE public_id=? ORDER BY id DESC LIMIT 1",
                (public_id,),
            ).fetchone()
            if any_release is None:
                raise HTTPException(404, "未找到该发布记录")
            existing = dict(any_release)
            if str(existing.get("status") or "") == "withdrawn":
                return {
                    "public_id": public_id,
                    "status": "withdrawn",
                    "mode": str(existing.get("withdraw_mode") or ""),
                    "withdrawn_at": existing.get("withdrawn_at"),
                    "reason": str(existing.get("reason") or ""),
                    "idempotent": True,
                    "message": "该发布已撤回，无需重复操作",
                }
            raise HTTPException(409, "发布记录状态异常，请联系管理员")
        payload = withdraw_active_release(
            con, dict(release), public_id=public_id, actor_id=user_id,
            reason=data.reason.strip(), mode=data.mode,
        )
    return payload


# ---------------------------------------------------------------------------
# 回滚：撤销当前发布并恢复上一已发布版本（方案 §8 阶段 3「上线回滚」）
# ---------------------------------------------------------------------------

class RollbackInput(BaseModel):
    reason: str = Field(min_length=2, max_length=2000)


@router.post("/submissions/{submission_id}/rollback")
def rollback_submission(submission_id: int, data: RollbackInput,
                        user=Depends(service.require_editor)):
    """回滚发布：撤回当前活跃发布，恢复该投稿上一已发布版本为 active。

    * 只有平台编辑/管理员可操作，且编辑回避本人投稿；
    * 当前没有活跃发布、或没有更早的已撤回历史发布 → 409；
    * 同一事务内切换发布指针、恢复该版本快照、重登记公开图、写事件并通知作者/收藏者。
    """
    db.ensure_tables()
    user_id = int(user["id"])
    reason = data.reason.strip()
    if len(reason) < 2:
        raise HTTPException(422, "回滚必须填写原因")
    with connection() as con:
        write_lock(con, "plaza-write")
        row = db.load_submission(con, submission_id)
        if row is None:
            raise HTTPException(404, "未找到该投稿")
        if int(row["author_id"]) == user_id:
            raise HTTPException(403, "回滚回避：不能回滚本人投稿")
        active_row = con.execute(
            "SELECT * FROM plaza_releases WHERE submission_id=? AND status='active' "
            "ORDER BY id DESC LIMIT 1",
            (int(submission_id),),
        ).fetchone()
        if active_row is None:
            raise HTTPException(409, {
                "message": "当前没有活跃发布，无法回滚",
                "submission_id": int(submission_id),
                "status": str(row.get("status") or ""),
            })
        active = dict(active_row)
        previous_row = con.execute(
            "SELECT * FROM plaza_releases WHERE submission_id=? AND status='withdrawn' "
            "AND id<? ORDER BY id DESC LIMIT 1",
            (int(submission_id), int(active["id"])),
        ).fetchone()
        if previous_row is None:
            raise HTTPException(409, {
                "message": "没有可恢复的历史发布版本，无法回滚",
                "submission_id": int(submission_id),
                "active_public_id": str(active.get("public_id") or ""),
            })
        previous = dict(previous_row)
        restored_version = int(previous.get("version") or 1)
        if restored_version >= int(active.get("version") or 1):
            raise HTTPException(409, "历史发布版本不早于当前版本，无法回滚")
        version_row = db.latest_version_snapshot(con, int(submission_id), restored_version)
        if version_row is None:
            raise HTTPException(409, "历史版本快照缺失，无法回滚")
        now = utcnow()
        changed = con.execute(
            "UPDATE plaza_releases SET status='withdrawn', active_public_id=NULL, "
            "withdrawn_at=?, withdraw_mode='rollback', reason=? "
            "WHERE id=? AND status='active'",
            (now, reason, int(active["id"])),
        ).rowcount
        if not changed:
            raise HTTPException(409, "发布记录已被其他操作修改，请刷新后重试")
        restored_public_id = str(previous.get("public_id") or "")
        changed = con.execute(
            "UPDATE plaza_releases SET status='active', active_public_id=?, "
            "withdrawn_at=NULL, withdraw_mode='', reason='' "
            "WHERE id=? AND status='withdrawn'",
            (restored_public_id, int(previous["id"])),
        ).rowcount
        if not changed:
            raise HTTPException(409, "历史发布记录状态异常，无法恢复")
        snapshot = db.json_load(version_row.get("snapshot_json"), {})
        if not isinstance(snapshot, dict):
            snapshot = {}
        source = db.json_load(version_row.get("source_json"), {})
        if not isinstance(source, dict):
            source = {}
        changed = con.execute(
            "UPDATE plaza_submissions SET version=?, current_version=?, snapshot_json=?, "
            "source_json=?, fingerprint=?, license_confirmed=?, status='published', "
            "public_id=?, updated_at=? WHERE id=? AND status='published' AND version=?",
            (
                restored_version, restored_version, db.json_dump(snapshot),
                db.json_dump(source),
                str(version_row.get("fingerprint") or ""),
                int(version_row.get("license_confirmed") or 0),
                restored_public_id, now, int(submission_id),
                int(active.get("version") or 1),
            ),
        ).rowcount
        if not changed:
            raise HTTPException(409, "投稿已被其他操作修改，请刷新后重试")
        refreshed = db.load_submission(con, int(submission_id))
        figure_summary = _ensure_figures(con, refreshed)
        restored_row = con.execute(
            "SELECT * FROM plaza_releases WHERE id=?", (int(previous["id"]),),
        ).fetchone()
        restored_release = _release_public(
            con, dict(restored_row) if restored_row is not None else dict(previous),
        )
        db.record_event(
            con, int(submission_id), user_id, "release_rolled_back",
            "published", "published", reason=reason,
            payload={
                "withdrawn_release_id": int(active["id"]),
                "restored_release_id": int(previous["id"]),
                "from_version": int(active.get("version") or 1),
                "to_version": restored_version,
                "public_id": restored_public_id,
                "figures": {
                    "total": int(figure_summary.get("total") or 0),
                    "public": int(figure_summary.get("public") or 0),
                    "unavailable": int(figure_summary.get("unavailable") or 0),
                },
            },
        )
        notify.notify(
            con, int(row["author_id"]), "release_rolled_back",
            "发布已回滚到上一版本",
            "平台编辑已将你的题目回滚到上一已发布版本，公开内容恢复为该版本。",
            submission_id=int(submission_id), public_id=restored_public_id,
            link=notify.public_link(restored_public_id),
            payload={"restored_version": restored_version,
                     "withdrawn_version": int(active.get("version") or 1),
                     "reason": reason},
        )
        favoriters = favorites.notify_favoriters(
            con, refreshed, "favorite_restored",
            "你收藏的题目已恢复上一发布版本",
            "你收藏的题目已回滚到上一已发布版本，公开内容恢复为该版本。",
        )
        payload = {
            "submission": {
                "id": int(submission_id),
                "status": "published",
                "status_label": db.STATUS_LABELS["published"],
                "version": restored_version,
                "current_version": restored_version,
                "public_id": restored_public_id,
                "public_visible": True,
            },
            "release": restored_release,
            "restored_release": restored_release,
            "withdrawn_release": {
                "public_id": str(active.get("public_id") or ""),
                "version": int(active.get("version") or 1),
                "withdrawn_at": now,
                "mode": "rollback",
                "reason": reason,
            },
            "restored_version": restored_version,
            "notified": {"author": True, "favoriters": int(favoriters)},
            "figures": figures.summary_for_editor(figure_summary),
            "public_url": f"/api/plaza/questions/{restored_public_id}",
            "idempotent": False,
            "message": f"已回滚到版本 {restored_version}，当前发布指针已恢复",
        }
    return payload


# ---------------------------------------------------------------------------
# 一键恢复：抽检 high 自动撤回 / 编辑撤回后恢复公开发布（WS28）
# ---------------------------------------------------------------------------

class RestoreReleaseInput(BaseModel):
    reason: str = Field(min_length=2, max_length=2000)
    #: 必须显式确认「问题已处理」（谁基于什么理由恢复，写入审计事件与通知）。
    confirm_resolved: bool = False


@router.post("/releases/{public_id}/restore")
def restore_release(public_id: str, data: RestoreReleaseInput,
                    user=Depends(service.require_editor)):
    """一键恢复被抽检 ``high`` 自动撤回或编辑撤回的发布（同事务恢复整条链）。

    * 仅编辑/管理员；编辑回避本人投稿；
    * 仅 ``withdrawn`` 且由「抽检 high 自动撤回」或「编辑撤回」产生的发布；
      回滚产生（``withdraw_mode='rollback'``）的不支持恢复；
    * 恢复条件：``confirm_resolved=true`` + 明确 reason（问题已处理声明）；
    * 同一投稿已有活跃发布或已有更新版本时 409（先重新送审/签发，避免内容回退）；
    * 同事务恢复发布指针、投稿状态与版本快照、公开衍生图，写 ``release_restored``
      事件，通知作者与收藏者，并把恢复操作人/理由留在事件 payload 中。
    """
    db.ensure_tables()
    user_id = int(user["id"])
    with connection() as con:
        write_lock(con, "plaza-write")
        release_rows = [
            dict(row) for row in con.execute(
                "SELECT * FROM plaza_releases WHERE public_id=? ORDER BY id DESC",
                (public_id,),
            ).fetchall()
        ]
        if not release_rows:
            raise HTTPException(404, "未找到该发布记录")
        withdrawn_rows = [
            item for item in release_rows
            if str(item.get("status") or "") == "withdrawn"
        ]
        active_rows = [
            item for item in release_rows
            if str(item.get("status") or "") == "active"
        ]
        if not withdrawn_rows:
            if active_rows:
                return {
                    "release": _release_public(con, active_rows[0]),
                    "restored_version": int(active_rows[0].get("version") or 1),
                    "idempotent": True,
                    "message": "该发布已处于活跃状态，无需重复恢复",
                }
            raise HTTPException(409, {
                "message": "发布记录状态异常，无法恢复",
                "status": str(release_rows[0].get("status") or ""),
            })
        # 默认恢复「该公开题最近一次被撤回的发布」；若同一题已有更新版本活跃，
        # 下方 active 检查会 409（避免把已上线的更新版本回退成旧版本内容）。
        release = withdrawn_rows[0]
        submission = db.load_submission(con, int(release["submission_id"]))
        if submission is None:
            raise HTTPException(409, "发布记录对应的投稿不存在")
        if int(submission["author_id"]) == user_id:
            raise HTTPException(403, "恢复回避：不能恢复本人投稿")
        withdraw_mode = str(release.get("withdraw_mode") or "")
        if withdraw_mode == "rollback":
            raise HTTPException(409, {
                "message": "回滚产生的撤回记录不支持一键恢复；如需恢复旧版本内容请重新送审并签发",
                "withdraw_mode": withdraw_mode,
            })
        quality_check = con.execute(
            "SELECT * FROM plaza_quality_checks WHERE release_id=? AND auto_withdrawn=1 "
            "ORDER BY id DESC LIMIT 1",
            (int(release["id"]),),
        ).fetchone()
        auto_withdraw = quality_check is not None
        editor_withdraw = (
            withdraw_mode in ("normal", "emergency")
            and bool(str(release.get("withdrawn_at") or ""))
        )
        if not (auto_withdraw or editor_withdraw):
            raise HTTPException(409, {
                "message": "该发布不是抽检自动撤回或编辑撤回，不支持一键恢复",
                "submission_id": int(submission["id"]),
                "withdraw_mode": withdraw_mode,
            })
        if not data.confirm_resolved:
            raise HTTPException(422, {
                "message": "恢复前必须确认问题已处理（confirm_resolved=true）并填写理由",
                "field": "confirm_resolved",
                "guidance": (
                    "抽检 high 自动撤回或编辑撤回的发布，只有问题已处理/已修订后"
                    "才能恢复公开发布；恢复操作人与理由会写入审计事件。"
                ),
            })
        reason = data.reason.strip()
        if len(reason) < 2:
            raise HTTPException(422, "恢复必须填写理由（至少 2 个字）")
        active_row = con.execute(
            "SELECT * FROM plaza_releases WHERE submission_id=? AND status='active' "
            "ORDER BY id DESC LIMIT 1",
            (int(submission["id"]),),
        ).fetchone()
        if active_row is not None:
            active = dict(active_row)
            raise HTTPException(409, {
                "message": "该投稿已有更新版本的活跃发布，不能恢复旧发布；请重新送审并签发新版本",
                "active_public_id": str(active.get("public_id") or ""),
                "active_version": int(active.get("version") or 1),
                "restore_version": int(release.get("version") or 1),
            })
        restored_version = int(release.get("version") or 1)
        current_version = int(submission.get("current_version") or 1)
        if current_version != restored_version:
            raise HTTPException(409, {
                "message": "投稿已有更新版本，恢复旧发布会造成内容回退；请重新送审并签发新版本",
                "current_version": current_version,
                "restore_version": restored_version,
            })
        if str(submission.get("status") or "") != "withdrawn":
            raise HTTPException(409, {
                "message": "投稿当前状态不是已撤回，无法恢复旧发布",
                "status": str(submission.get("status") or ""),
                "status_label": db.STATUS_LABELS.get(
                    str(submission.get("status") or ""), "",
                ),
            })
        version_row = db.latest_version_snapshot(con, int(submission["id"]), restored_version)
        if version_row is None:
            raise HTTPException(409, "历史版本快照缺失，无法恢复发布")
        now = utcnow()
        restored_public_id = str(release.get("public_id") or public_id)
        changed = con.execute(
            "UPDATE plaza_releases SET status='active', active_public_id=?, "
            "withdrawn_at=NULL, withdraw_mode='', reason='' "
            "WHERE id=? AND status='withdrawn'",
            (restored_public_id, int(release["id"])),
        ).rowcount
        if not changed:
            raise HTTPException(409, "发布记录已被其他操作修改，请刷新后重试")
        snapshot = db.json_load(version_row.get("snapshot_json"), {})
        if not isinstance(snapshot, dict):
            snapshot = {}
        source = db.json_load(version_row.get("source_json"), {})
        if not isinstance(source, dict):
            source = {}
        changed = con.execute(
            "UPDATE plaza_submissions SET version=?, current_version=?, snapshot_json=?, "
            "source_json=?, fingerprint=?, license_confirmed=?, status='published', "
            "public_id=?, updated_at=? WHERE id=? AND status='withdrawn' AND version=?",
            (
                restored_version, restored_version, db.json_dump(snapshot),
                db.json_dump(source),
                str(version_row.get("fingerprint") or ""),
                int(version_row.get("license_confirmed") or 0),
                restored_public_id, now, int(submission["id"]), restored_version,
            ),
        ).rowcount
        if not changed:
            raise HTTPException(409, "投稿已被其他操作修改，请刷新后重试")
        refreshed = db.load_submission(con, int(submission["id"])) or submission
        figure_summary = _ensure_figures(con, refreshed)
        restored_row = con.execute(
            "SELECT * FROM plaza_releases WHERE id=?", (int(release["id"]),),
        ).fetchone()
        restored_release = _release_public(
            con, dict(restored_row) if restored_row is not None else release,
        )
        restore_source = "quality_auto_withdraw" if auto_withdraw else "editor_withdraw"
        previous_withdraw = {
            "mode": withdraw_mode,
            "reason": str(release.get("reason") or ""),
            "withdrawn_at": release.get("withdrawn_at"),
        }
        db.record_event(
            con, int(submission["id"]), user_id, "release_restored",
            "withdrawn", "published", reason=reason,
            payload={
                "release_id": int(release["id"]),
                "public_id": restored_public_id,
                "version": restored_version,
                "source": restore_source,
                "quality_check_id": (
                    int(dict(quality_check)["id"]) if quality_check is not None else None
                ),
                "previous_withdraw": previous_withdraw,
                "confirm_resolved": True,
                "figures": {
                    "total": int(figure_summary.get("total") or 0),
                    "public": int(figure_summary.get("public") or 0),
                    "unavailable": int(figure_summary.get("unavailable") or 0),
                },
            },
        )
        notify.notify(
            con, int(submission["author_id"]), "release_restored",
            "发布已恢复公开",
            "平台编辑已恢复该题的公开发布（此前撤回的问题已处理）。",
            submission_id=int(submission["id"]), public_id=restored_public_id,
            link=notify.public_link(restored_public_id),
            payload={"release_id": int(release["id"]), "version": restored_version,
                     "reason": reason, "source": restore_source},
        )
        favoriters = favorites.notify_favoriters(
            con, refreshed, "favorite_restored",
            "你收藏的题目已恢复公开",
            "你收藏的题目已恢复公开发布，可继续查看；历史作答记录保留。",
        )
        quality_check_summary = None
        if quality_check is not None:
            check_item = dict(quality_check)
            quality_check_summary = {
                "id": int(check_item.get("id") or 0),
                "decision": str(check_item.get("decision") or ""),
                "severity": str(check_item.get("severity") or ""),
                "version": int(check_item.get("version") or 0),
                "findings": db.json_load(check_item.get("findings_json"), []) or [],
                "note": str(check_item.get("note") or ""),
            }
        payload = {
            "submission": {
                "id": int(submission["id"]),
                "status": "published",
                "status_label": db.STATUS_LABELS["published"],
                "version": restored_version,
                "current_version": restored_version,
                "public_id": restored_public_id,
                "public_visible": True,
            },
            "release": restored_release,
            "restored_release": restored_release,
            "restored_version": restored_version,
            "restore_source": restore_source,
            "previous_withdraw": previous_withdraw,
            "quality_check": quality_check_summary,
            "notified": {"author": True, "favoriters": int(favoriters)},
            "figures": figures.summary_for_editor(figure_summary),
            "public_url": f"/api/plaza/questions/{restored_public_id}",
            "idempotent": False,
            "message": f"已恢复版本 {restored_version} 的公开发布",
        }
    return payload


# ---------------------------------------------------------------------------
# 发布历史 / 事件链
# ---------------------------------------------------------------------------

@router.get("/releases")
def list_releases(
    status: str | None = Query(default=None, pattern="^(active|withdrawn)$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=MAX_PAGE_SIZE),
    user=Depends(service.require_editor),
):
    db.ensure_tables()
    where: list[str] = []
    params: list = []
    if status:
        where.append("status=?")
        params.append(status)
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    with connection() as con:
        total = int(con.execute(
            f"SELECT COUNT(*) FROM plaza_releases{clause}", tuple(params)
        ).fetchone()[0])
        rows = con.execute(
            f"SELECT * FROM plaza_releases{clause} ORDER BY id DESC LIMIT ? OFFSET ?",
            (*params, page_size, (page - 1) * page_size),
        ).fetchall()
        items = [_release_public(con, dict(row)) for row in rows]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@router.get("/submissions/{submission_id}/events")
def submission_events(submission_id: int, user=Depends(current_user)):
    """事件链：作者看节点、编辑/管理员看完整审计字段；评审员无权查看。"""
    db.ensure_tables()
    user_id = int(user["id"])
    is_admin = str(user.get("role") or "") == "admin"
    with connection() as con:
        row = db.load_submission(con, submission_id)
        if row is None:
            raise HTTPException(404, "未找到该投稿")
        names = db.role_names(con, user_id)
        is_author = int(row["author_id"]) == user_id
        is_editor = is_admin or "editor" in names
        if not (is_author or is_editor):
            raise HTTPException(403, "仅作者、编辑或管理员可查看投稿事件链")
        rows = con.execute(
            "SELECT * FROM plaza_events WHERE submission_id=? ORDER BY id",
            (submission_id,),
        ).fetchall()
        items = []
        for raw in rows:
            item = dict(raw)
            payload = db.json_load(item.get("payload_json"), {})
            entry = {
                "action": str(item.get("action") or ""),
                "from_status": str(item.get("from_status") or ""),
                "to_status": str(item.get("to_status") or ""),
                "reason": str(item.get("reason") or ""),
                "payload": payload if isinstance(payload, dict) else {},
                "created_at": str(item.get("created_at") or ""),
            }
            if is_editor:
                entry["actor_id"] = item.get("actor_id")
                entry["id"] = int(item.get("id") or 0)
            items.append(entry)
        submission_status = str(row.get("status") or "")
    return {
        "submission_id": int(submission_id),
        "status": submission_status,
        "status_label": db.STATUS_LABELS.get(submission_status, submission_status),
        "items": items,
    }
