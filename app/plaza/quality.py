"""广场质量抽检：抽检记录、待抽检队列与严重问题自动撤回。

方案 §8 阶段 3「上线抽检、通知及回滚」：

* 试点首批对正式发布题做 **100% 独立抽检**（``QUALITY_SAMPLING_MODE=initial_100``），
  稳定后可由运营改为常规抽检 10%、高风险 100%；
* 抽检结论 ``pass`` / ``issue``；``issue`` 必须给严重度与问题清单；
* 抽检分两个阶段共用同一接口：``post_publish``（已发布，既有口径，``high`` 自动撤回）
  与 ``pre_publish``（``verified`` / ``pending_release``，签发前抽检——抽检门禁要求
  通过后才能签发，见 :mod:`app.plaza.gate`）；``community_review`` 等其它状态仍 409；
* ``high`` 严重问题（严重错题、隐私/版权阻断）**自动撤回**当前发布（复用
  :func:`app.plaza.release.withdraw_active_release`，同一事务写发布指针、投稿状态、
  ``plaza_events`` 与通知作者/收藏者），并返回 ``auto_withdrawn=true``；
* 同一版本可多次抽检（记录只增不删），签发门禁按**最新一次**记录判定，避免歧义；
* 抽检员回避本人投稿；队列只对编辑/管理员开放，抽检员身份脱敏展示。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from ..auth import require_admin
from ..db import connection, utcnow, write_lock
from . import db, favorites, gate, notify, release, service

router = APIRouter()

MAX_PAGE_SIZE = 100
_MAX_FINDINGS = 20
_FINDING_LIMIT = 500
_SEVERITY_LABELS = {"low": "低", "medium": "中", "high": "高"}


# ---------------------------------------------------------------------------
# 序列化
# ---------------------------------------------------------------------------

def check_public(row: dict) -> dict:
    findings = db.json_load(row.get("findings_json"), [])
    if not isinstance(findings, list):
        findings = []
    release_id = row.get("release_id")
    return {
        "id": int(row.get("id") or 0),
        "submission_id": int(row.get("submission_id") or 0),
        "public_id": str(row.get("public_id") or "") or None,
        "version": int(row.get("version") or 1),
        "release_id": release_id,
        "phase": "pre_publish" if not release_id else "post_publish",
        "decision": str(row.get("decision") or ""),
        "severity": str(row.get("severity") or ""),
        "severity_label": _SEVERITY_LABELS.get(str(row.get("severity") or ""), ""),
        "findings": [str(item) for item in findings],
        "note": str(row.get("note") or ""),
        "auto_withdrawn": bool(row.get("auto_withdrawn")),
        "checker_display": db.masked_display("抽检员", row.get("checker_id")),
        "created_at": str(row.get("created_at") or ""),
    }


def _check_status(latest: dict | None) -> str:
    if not latest:
        return "pending"
    return "passed" if str(latest.get("decision") or "") == "pass" else "issue"


def _latest_checks(con) -> dict[int, dict]:
    rows = con.execute("SELECT * FROM plaza_quality_checks ORDER BY id ASC").fetchall()
    latest: dict[int, dict] = {}
    for raw in rows:
        item = dict(raw)
        latest[int(item["submission_id"])] = item
    return latest


def _auto_withdrawn_ids(con) -> set[int]:
    rows = con.execute(
        "SELECT DISTINCT submission_id FROM plaza_quality_checks WHERE auto_withdrawn=1",
    ).fetchall()
    return {int(item[0]) for item in rows}


def _published_times(con) -> dict[int, str]:
    rows = con.execute(
        "SELECT submission_id, published_at FROM plaza_releases ORDER BY id ASC",
    ).fetchall()
    times: dict[int, str] = {}
    for row in rows:
        times[int(row[0])] = str(row[1] or "")
    return times


def _checks_counts(con) -> dict[int, int]:
    rows = con.execute(
        "SELECT submission_id, COUNT(*) FROM plaza_quality_checks GROUP BY submission_id",
    ).fetchall()
    return {int(item[0]): int(item[1] or 0) for item in rows}


# ---------------------------------------------------------------------------
# 待抽检 / 已抽检队列
# ---------------------------------------------------------------------------

@router.get("/admin/quality/queue")
def quality_queue(
    status: str = Query(default="all", max_length=20),
    subject: str | None = Query(default=None, max_length=40),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=MAX_PAGE_SIZE),
    user=Depends(service.require_plaza_role("editor")),
):
    """抽检队列：``pending`` 待抽检（已发布但无抽检记录）/ ``checked`` 已抽检。

    ``passed`` / ``issues`` 按最新一次抽检结论过滤；``all`` 返回并集。
    ``sampling`` 给出首批 100% 抽检的覆盖率提示。
    """
    db.ensure_tables()
    if status not in db.QUALITY_QUEUE_STATUSES:
        raise HTTPException(422, {
            "message": "未知的抽检队列状态",
            "allowed": list(db.QUALITY_QUEUE_STATUSES),
        })
    subject_filter = (subject or "").strip()
    with connection() as con:
        rows = con.execute(
            "SELECT * FROM plaza_submissions WHERE status='published' "
            "OR EXISTS (SELECT 1 FROM plaza_quality_checks q "
            "WHERE q.submission_id=plaza_submissions.id) "
            "ORDER BY updated_at DESC, id DESC",
        ).fetchall()
        latest = _latest_checks(con)
        auto_ids = _auto_withdrawn_ids(con)
        published_times = _published_times(con)
        checks_counts = _checks_counts(con)
        items: list[dict] = []
        for raw in rows:
            row = dict(raw)
            subject_value = str(row.get("subject") or "") or str(
                (db.json_load(row.get("snapshot_json"), {}) or {}).get("subject") or ""
            )
            if subject_filter and subject_value != subject_filter:
                continue
            snapshot = db.json_load(row.get("snapshot_json"), {})
            if not isinstance(snapshot, dict):
                snapshot = {}
            current = latest.get(int(row["id"]))
            current_status = _check_status(current)
            if current is not None:
                publish_phase = "pre_publish" if not current.get("release_id") else "post_publish"
            elif str(row.get("status") or "") == "published":
                publish_phase = "post_publish"
            else:
                publish_phase = ""
            items.append({
                "submission_id": int(row["id"]),
                "public_id": row.get("public_id"),
                "version": int(row.get("current_version") or 1),
                "subject": subject_value,
                "type": row.get("type") or snapshot.get("type") or "",
                "grade": row.get("grade") or snapshot.get("grade") or "",
                "stem_preview": service.stem_preview(snapshot, 160),
                "status": str(row.get("status") or ""),
                "status_label": db.STATUS_LABELS.get(str(row.get("status") or ""), ""),
                "published_at": published_times.get(int(row["id"]), ""),
                "check_status": current_status,
                "publish_phase": publish_phase,
                "latest_check": check_public(current) if current else None,
                "checks_count": checks_counts.get(int(row["id"]), 0),
                "auto_withdrawn": int(row["id"]) in auto_ids,
                "updated_at": str(row.get("updated_at") or ""),
            })
        counts = {
            "pending": 0, "passed": 0, "issues": 0, "checked": 0, "all": len(items),
        }
        for item in items:
            if item["check_status"] == "pending":
                counts["pending"] += 1
            else:
                counts["checked"] += 1
                if item["check_status"] == "passed":
                    counts["passed"] += 1
                else:
                    counts["issues"] += 1
        if status == "pending":
            filtered = [item for item in items if item["check_status"] == "pending"]
        elif status == "passed":
            filtered = [item for item in items if item["check_status"] == "passed"]
        elif status == "issues":
            filtered = [item for item in items if item["check_status"] == "issue"]
        elif status == "checked":
            filtered = [item for item in items if item["check_status"] != "pending"]
        else:
            filtered = items
        filtered.sort(
            key=lambda item: (
                0 if item["check_status"] == "pending" else 1,
                item["updated_at"],
            ),
        )
        total = len(filtered)
        page_items = filtered[(page - 1) * page_size: page * page_size]
        # 首批 100% 抽检覆盖率只统计「已签发过」的题（含被抽检自动撤回的题）：
        # 签发前抽检（pre_publish）的待发布题不计入发布基数，避免覆盖率虚低。
        sampling_items = [
            item for item in items
            if item["status"] == "published" or item["publish_phase"] == "post_publish"
        ]
        sampling_pending = sum(
            1 for item in sampling_items if item["check_status"] == "pending"
        )
        sampling_checked = len(sampling_items) - sampling_pending
        published_total = len(sampling_items)
        sampling = {
            "mode": db.QUALITY_SAMPLING_MODE,
            "required": bool(db.QUALITY_SAMPLING_REQUIRED),
            "label": "试点首批 100% 独立抽检",
            "published_total": published_total,
            "checked_total": sampling_checked,
            "pending_total": sampling_pending,
            "coverage_rate": round(sampling_checked / published_total, 4)
            if published_total else 0.0,
        }
        gate_state = gate.state(con)
    return {
        "items": page_items,
        "total": total,
        "page": page,
        "page_size": page_size,
        "status_filter": status,
        "counts": counts,
        "sampling": sampling,
        "gate": gate_state,
    }


# ---------------------------------------------------------------------------
# 记录抽检结论
# ---------------------------------------------------------------------------

class QualityCheckInput(BaseModel):
    submission_id: int | None = Field(default=None, ge=1)
    public_id: str | None = Field(default=None, max_length=80)
    decision: str = Field(pattern="^(pass|issue)$")
    severity: str | None = Field(default=None, pattern="^(low|medium|high)$")
    findings: list[str] | None = Field(default=None, max_length=_MAX_FINDINGS)
    note: str = Field(default="", max_length=2000)


def _normalize_findings(raw) -> list[str]:
    values: list[str] = []
    for item in raw or ():
        text = str(item or "").strip()
        if not text:
            continue
        values.append(text[:_FINDING_LIMIT])
        if len(values) >= _MAX_FINDINGS:
            break
    return values


@router.post("/admin/quality/checks", status_code=201)
def create_quality_check(data: QualityCheckInput,
                         user=Depends(service.require_plaza_role("editor"))):
    """记录抽检结论；``issue`` 必填严重度与问题清单，发布后 ``high`` 自动撤回。

    抽检阶段（同一接口）：``post_publish``=已发布且活跃发布（既有口径，``high``
    自动撤回）；``pre_publish``=``verified`` / ``pending_release`` 的签发前抽检，
    通过（pass）后编辑才能签发（见 :mod:`app.plaza.gate`）；其它状态 409。
    ``community_review`` 仍不可抽检（保持既有语义）。
    """
    db.ensure_tables()
    user_id = int(user["id"])
    with connection() as con:
        write_lock(con, "plaza-write")
        if data.submission_id is not None:
            submission = db.load_submission(con, int(data.submission_id))
        elif data.public_id and data.public_id.strip():
            submission = db.load_submission_by_public_id(con, data.public_id.strip())
        else:
            raise HTTPException(422, {
                "message": "请提供 submission_id 或 public_id",
                "field": "submission_id|public_id",
            })
        if submission is None:
            raise HTTPException(404, "未找到该投稿")
        if data.public_id and data.submission_id is not None and \
                str(submission.get("public_id") or "") != data.public_id.strip():
            raise HTTPException(422, "public_id 与 submission_id 不匹配")
        if int(submission["author_id"]) == user_id:
            raise HTTPException(403, "抽检回避：不能抽检本人投稿")
        status = str(submission.get("status") or "")
        release_row = con.execute(
            "SELECT * FROM plaza_releases WHERE submission_id=? AND status='active' "
            "ORDER BY id DESC LIMIT 1",
            (int(submission["id"]),),
        ).fetchone()
        if status == "published" and release_row is not None:
            phase = "post_publish"
        elif status in db.QUALITY_PRE_PUBLISH_STATUSES and release_row is None:
            # 签发前抽检：社区核验通过/待平台发布，尚未有活跃发布。
            phase = "pre_publish"
        else:
            raise HTTPException(409, {
                "message": (
                    "仅已发布（有活跃发布）的题目可做发布后抽检，"
                    "或社区核验通过（待签发）的题目可做签发前抽检"
                ),
                "status": status,
                "status_label": db.STATUS_LABELS.get(status, status),
            })
        severity = (data.severity or "").strip()
        findings = _normalize_findings(data.findings)
        if data.decision == "issue":
            if not severity:
                severity = "medium"
            if not findings:
                raise HTTPException(422, {
                    "message": "抽检发现问题必须填写 findings（问题清单）",
                    "field": "findings",
                })
        else:
            severity = ""
            findings = []
        version = int(submission.get("current_version") or 1)
        public_id = str(submission.get("public_id") or "")
        release_id = int(release_row["id"]) if release_row is not None else None
        check_id = db.insert_row(con, "plaza_quality_checks", {
            "submission_id": int(submission["id"]),
            "version": version,
            "release_id": release_id,
            "public_id": public_id,
            "checker_id": user_id,
            "decision": data.decision,
            "severity": severity,
            "findings_json": db.json_dump(findings),
            "note": data.note.strip()[:2000],
            "auto_withdrawn": 0,
            "created_at": utcnow(),
        })
        event_action = "quality_checked" if data.decision == "pass" else "quality_issue"
        db.record_event(
            con, int(submission["id"]), user_id, event_action, status, status,
            reason=data.note.strip()[:2000],
            payload={
                "quality_check_id": check_id,
                "decision": data.decision,
                "severity": severity,
                "findings": findings,
                "version": version,
                "public_id": public_id,
                "phase": phase,
            },
        )
        auto_withdrawn = False
        withdraw_payload = None
        notified = {"author": False, "favoriters": 0}
        if (data.decision == "issue"
                and severity in db.QUALITY_AUTO_WITHDRAW_SEVERITIES
                and phase == "post_publish" and release_row is not None):
            withdraw_payload = release.withdraw_active_release(
                con, dict(release_row), public_id=public_id, actor_id=user_id,
                reason=(
                    f"抽检发现{_SEVERITY_LABELS.get(severity, severity)}严重问题，自动撤回"
                    + (f"：{findings[0]}" if findings else "")
                ),
                mode="emergency",
                event_action="quality_withdrawn",
                extra_payload={
                    "quality_check_id": check_id,
                    "severity": severity,
                    "findings": findings,
                },
                author_kind="quality_withdrawn",
                author_title="抽检发现严重问题，题目已被撤回",
                author_body=(
                    "平台抽检发现该题存在严重问题，已自动撤回公开发布。"
                    "问题：" + "；".join(findings[:3])
                ),
            )
            con.execute(
                "UPDATE plaza_quality_checks SET auto_withdrawn=1 WHERE id=?",
                (check_id,),
            )
            auto_withdrawn = True
            notified = {
                "author": True,
                "favoriters": len(favorites.favoriter_ids(con, int(submission["id"]))),
            }
        elif data.decision == "issue":
            notified["author"] = bool(notify.notify(
                con, int(submission["author_id"]), "quality_issue",
                f"抽检发现问题（严重度：{_SEVERITY_LABELS.get(severity, severity)}）",
                (
                    "平台抽检发现该题存在问题，请尽快在投稿台查看并修订。"
                    if phase == "post_publish" else
                    "平台签发前抽检发现该题存在问题，已暂缓签发，请修订后重新送审。"
                ) + "问题：" + "；".join(findings[:3]),
                submission_id=int(submission["id"]), public_id=public_id,
                link=notify.submission_link(int(submission["id"])),
                payload={"quality_check_id": check_id, "severity": severity,
                         "findings": findings, "version": version,
                         "phase": phase},
            ))
        fresh_submission = db.load_submission(con, int(submission["id"])) or submission
        fresh_check = con.execute(
            "SELECT * FROM plaza_quality_checks WHERE id=?", (check_id,),
        ).fetchone()
        gate_info = gate.evaluate(con, fresh_submission, version)
        response_payload = {
            "check": check_public(dict(fresh_check)) if fresh_check else {"id": check_id},
            "submission": {
                "id": int(fresh_submission["id"]),
                "status": str(fresh_submission.get("status") or ""),
                "status_label": db.STATUS_LABELS.get(
                    str(fresh_submission.get("status") or ""), "",
                ),
                "version": int(fresh_submission.get("current_version") or 1),
                "public_id": fresh_submission.get("public_id"),
                "public_visible": str(fresh_submission.get("status") or "")
                in db.PUBLIC_STATUSES,
            },
            "phase": phase,
            "auto_withdrawn": auto_withdrawn,
            "withdraw": withdraw_payload,
            "notified": notified,
            "gate": {
                "mode": gate_info["mode"],
                "mode_label": gate_info["mode_label"],
                "mode_source": gate_info["mode_source"],
                "armed": bool(gate_info["armed"]),
                "enforced": bool(gate_info["enforced"]),
                "required": bool(gate_info["required"]),
                "blocking": bool(gate_info.get("blocking")),
                # 签发前抽检通过后即可签发；发布后抽检不改变发布状态。
                "publish_ready": bool(
                    phase == "pre_publish" and not gate_info.get("blocking")
                ),
                "guidance": str(gate_info.get("guidance") or ""),
                "override_guidance": gate.OVERRIDE_GUIDANCE,
            },
            "message": (
                "抽检发现严重问题，已自动撤回该题发布并通知作者与收藏者"
                if auto_withdrawn else
                (
                    "签发前抽检已记录，该版本已满足抽检门禁，可签发"
                    if phase == "pre_publish" and data.decision == "pass" else
                    "抽检结论已记录" if data.decision == "pass"
                    else "抽检问题已记录，已通知作者"
                )
            ),
        }
    return response_payload


# ---------------------------------------------------------------------------
# 抽检门禁：状态查询与管理员设置（模式 DB 设置 > 环境变量 > 默认 initial_100）
# ---------------------------------------------------------------------------

@router.get("/admin/quality/gate")
def get_quality_gate(user=Depends(service.require_plaza_role("editor"))):
    """抽检门禁当前状态（模式/是否已激活/试点窗口/审计计数/最近判定）。"""
    db.ensure_tables()
    with connection() as con:
        return gate.state(con)


class QualityGateUpdateInput(BaseModel):
    mode: str | None = Field(default=None, pattern="^(off|initial_100|always)$")
    armed: bool | None = None
    pilot_limit: int | None = Field(default=None, ge=0, le=1000000)
    reset: bool = False


@router.put("/admin/quality/gate")
def update_quality_gate(data: QualityGateUpdateInput, user=Depends(require_admin)):
    """管理员修改抽检门禁：``{mode?, armed?, pilot_limit?, reset?}``，立即生效。"""
    db.ensure_tables()
    try:
        return gate.update_settings(
            mode=data.mode, armed=data.armed,
            pilot_limit=data.pilot_limit, reset=bool(data.reset),
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None


__all__ = [
    "check_public",
    "create_quality_check",
    "get_quality_gate",
    "quality_queue",
    "update_quality_gate",
]
