"""广场质量指标：题目覆盖、可判分率、图可用率、评审时长、抽检问题率与反馈积压。

口径（方案 §7「搜索与题库质量」、§8「面向发布跟踪五项指标」）：

* **可判分率**：公开可见题（社区待核验/已核验/精选）中，快照含参考答案（含子题）
  的占比——与练习门禁「可自动判分」同口径的展示版；
* **图可用率**：公开可见题当前版本的配图引用中，已登记且可匿名读取的公开衍生图
  占比（``plaza_public_assets.status='active'`` 且 ``public_asset_id`` 非空）；
* **评审时长**：``initial_review_accepted``（材料齐备）→ ``threshold_met``（社区核验
  通过）的中位小时数；另给 ``submitted``→``published`` 的签发时长；
* **抽检问题率**：最新抽检记录中 ``issue`` 占比与 ``high`` 严重问题数；首批 100%
  抽检覆盖率单独给出；
* **抽检门禁（WS28）**：模式（off/initial_100/always）、是否已激活（armed）、试点
  窗口计数、阻断/override/通过次数与最近判定；
* **一键恢复 / 批量处理 / 通知**：``release_restored`` 事件数、编辑台批量处理量与
  跳过数（``plaza_ops_batches``）、站内通知未读总量（admin 聚合口径）。

所有字段集中在本模块计算，编辑台/前端只展示不重算；指标为运营口径，不做个性化。
"""
from __future__ import annotations

import statistics
from datetime import datetime

from fastapi import APIRouter, Depends

from ..db import connection, utcnow
from . import db, gate, service

router = APIRouter()


def _rate(numerator: int, denominator: int) -> float:
    if not denominator:
        return 0.0
    return round(float(numerator) / float(denominator), 4)


def _parse_time(value) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed


def _hours(start, end) -> float | None:
    begin = _parse_time(start)
    finish = _parse_time(end)
    if begin is None or finish is None:
        return None
    seconds = (finish - begin).total_seconds()
    if seconds < 0:
        return None
    return round(seconds / 3600.0, 3)


def _has_answer(snapshot: dict) -> bool:
    content = snapshot.get("content") if isinstance(snapshot, dict) else None
    if not isinstance(content, dict):
        return False
    if content.get("answers"):
        return True
    for part in content.get("parts") or []:
        if isinstance(part, dict) and part.get("answers"):
            return True
    return False


def _image_count(snapshot: dict) -> int:
    content = snapshot.get("content") if isinstance(snapshot, dict) else None
    images = content.get("images") if isinstance(content, dict) else None
    return len(images) if isinstance(images, list) else 0


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    return round(float(statistics.median(values)), 2)


@router.get("/admin/metrics")
def admin_metrics(user=Depends(service.require_plaza_role("editor"))):
    """广场质量指标（仅编辑/管理员）：覆盖/可判分/图可用/评审时长/抽检/反馈/撤回。"""
    db.ensure_tables()
    with connection() as con:
        submissions = [dict(row) for row in con.execute(
            "SELECT * FROM plaza_submissions ORDER BY id",
        ).fetchall()]
        releases = [dict(row) for row in con.execute(
            "SELECT status, submission_id FROM plaza_releases",
        ).fetchall()]
        checks = [dict(row) for row in con.execute(
            "SELECT submission_id, version, release_id, decision, severity, auto_withdrawn "
            "FROM plaza_quality_checks",
        ).fetchall()]
        events = [dict(row) for row in con.execute(
            "SELECT submission_id, action, created_at FROM plaza_events ORDER BY id",
        ).fetchall()]
        asset_rows = [dict(row) for row in con.execute(
            "SELECT submission_id, version, status, public_asset_id "
            "FROM plaza_public_assets",
        ).fetchall()]
        gate_state = gate.state(con)
        batch_rows = [dict(row) for row in con.execute(
            "SELECT kind, action, requested, updated, skipped FROM plaza_ops_batches",
        ).fetchall()]
        notification_total = int(con.execute(
            "SELECT COUNT(*) FROM plaza_notifications",
        ).fetchone()[0])
        notification_unread = int(con.execute(
            "SELECT COUNT(*) FROM plaza_notifications WHERE read_at IS NULL",
        ).fetchone()[0])
        feedback_tables = {
            "issues": "plaza_issues",
            "reports": "plaza_reports",
            "appeals": "plaza_appeals",
        }
        feedback: dict[str, dict] = {}
        for key, table in feedback_tables.items():
            total = int(con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            unresolved = int(con.execute(
                f"SELECT COUNT(*) FROM {table} WHERE status='pending'",
            ).fetchone()[0])
            feedback[key] = {"total": total, "unresolved": unresolved}

    # ---- 题量与学科/题型分布 -------------------------------------------------
    status_counts = {status: 0 for status in db.SUBMISSION_STATUSES}
    by_subject: dict[str, dict] = {}
    by_type: dict[str, dict] = {}
    public_rows: list[dict] = []
    for row in submissions:
        status = str(row.get("status") or "")
        if status in status_counts:
            status_counts[status] += 1
        snapshot = db.json_load(row.get("snapshot_json"), {})
        if not isinstance(snapshot, dict):
            snapshot = {}
        subject = str(row.get("subject") or snapshot.get("subject") or "").strip() or "未标注"
        qtype = str(row.get("type") or snapshot.get("type") or "").strip() or "未标注"
        for bucket, key in ((by_subject, subject), (by_type, qtype)):
            entry = bucket.setdefault(key, {
                "count": 0, "published": 0, "verified": 0, "community_review": 0,
            })
            entry["count"] += 1
            if status in ("published", "verified", "community_review"):
                entry[status] += 1
        if status in db.PUBLIC_STATUSES and row.get("public_id"):
            public_rows.append({**row, "_snapshot": snapshot})

    # ---- 可判分率 / 图可用率 -------------------------------------------------
    scorable = sum(1 for row in public_rows if _has_answer(row["_snapshot"]))
    image_refs = sum(_image_count(row["_snapshot"]) for row in public_rows)
    public_ids = {int(row["id"]) for row in public_rows}
    current_versions = {
        int(row["id"]): int(row.get("current_version") or 1) for row in public_rows
    }
    public_images = sum(
        1 for asset in asset_rows
        if int(asset.get("submission_id") or 0) in public_ids
        and str(asset.get("status") or "") == "active"
        and str(asset.get("public_asset_id") or "").strip()
        and int(asset.get("version") or 1)
        == current_versions.get(int(asset.get("submission_id") or 0))
    )

    # ---- 评审时长 -----------------------------------------------------------
    event_times: dict[int, dict[str, str]] = {}
    for event in events:
        submission_id = int(event.get("submission_id") or 0)
        if not submission_id:
            continue
        slot = event_times.setdefault(submission_id, {})
        action = str(event.get("action") or "")
        slot[action] = str(event.get("created_at") or "")

    review_hours: list[float] = []
    publish_hours: list[float] = []
    for submission_id, slot in event_times.items():
        accepted = _hours(
            slot.get("initial_review_accepted"), slot.get("threshold_met"),
        )
        if accepted is not None:
            review_hours.append(accepted)
        submitted = _hours(slot.get("submitted"), slot.get("published"))
        if submitted is not None:
            publish_hours.append(submitted)

    # ---- 抽检 ---------------------------------------------------------------
    checks_total = len(checks)
    passed = sum(1 for item in checks if str(item.get("decision") or "") == "pass")
    issues = sum(1 for item in checks if str(item.get("decision") or "") == "issue")
    high_issues = sum(
        1 for item in checks
        if str(item.get("decision") or "") == "issue"
        and str(item.get("severity") or "") == "high"
    )
    post_publish_checks = [
        item for item in checks if item.get("release_id") is not None
    ]
    pre_publish_checks = checks_total - len(post_publish_checks)
    published_ids = {
        int(row.get("id") or 0) for row in submissions
        if str(row.get("status") or "") == "published"
    }
    post_check_submissions = {
        int(item.get("submission_id") or 0) for item in post_publish_checks
    }
    all_check_submissions = {
        int(item.get("submission_id") or 0) for item in checks
    }
    # 抽检只发生在已发布题：抽检后被撤回的题仍计入「首批发布题」基数，
    # 这样 100% 抽检覆盖率不会因严重问题自动撤回而虚降；签发前抽检
    # （pre_publish，release_id 为空）只在签发后计入覆盖率。
    checked_submissions = post_check_submissions | (all_check_submissions & published_ids)
    withdrawn_after_check = len(checked_submissions - published_ids)
    published_count = len(published_ids) + withdrawn_after_check
    pending_checks = max(0, len(published_ids - checked_submissions))
    sampling = {
        "mode": db.QUALITY_SAMPLING_MODE,
        "required": bool(db.QUALITY_SAMPLING_REQUIRED),
        "published_total": published_count,
        "checked_total": len(checked_submissions),
        "pending_total": pending_checks,
        "coverage_rate": _rate(len(checked_submissions), published_count),
    }
    gate_summary = {
        "mode": gate_state["mode"],
        "mode_label": gate_state["mode_label"],
        "mode_source": gate_state["mode_source"],
        "armed": bool(gate_state["armed"]),
        "armed_source": gate_state["armed_source"],
        "enforced": bool(gate_state["enforced"]),
        "required": bool(gate_state["required"]),
        "window_open": bool(gate_state["window_open"]),
        "pilot_limit": int(gate_state["pilot_limit"]),
        "pilot_remaining": gate_state["pilot_remaining"],
        "published_total": int(gate_state["published_total"]),
        "checks_total": int(gate_state["checks_total"]),
        "blocked_total": int(gate_state["blocked_total"]),
        "override_total": int(gate_state["override_total"]),
        "passed_total": int(gate_state["passed_total"]),
        "evaluated_total": int(gate_state["evaluated_total"]),
        "by_mode": dict(gate_state.get("by_mode") or {}),
        "notice": str(gate_state.get("notice") or ""),
    }

    # ---- 撤回 / 回滚 / 一键恢复 ---------------------------------------------
    releases_withdrawn = sum(
        1 for item in releases if str(item.get("status") or "") == "withdrawn"
    )
    rollbacks = sum(
        1 for item in events if str(item.get("action") or "") == "release_rolled_back"
    )
    restored = sum(
        1 for item in events if str(item.get("action") or "") == "release_restored"
    )

    # ---- 批量处理 / 通知（WS28） --------------------------------------------
    batch_by_kind: dict[str, dict] = {}
    for item in batch_rows:
        key = str(item.get("kind") or "")
        slot = batch_by_kind.setdefault(
            key, {"batches": 0, "requested": 0, "updated": 0, "skipped": 0},
        )
        slot["batches"] += 1
        slot["requested"] += int(item.get("requested") or 0)
        slot["updated"] += int(item.get("updated") or 0)
        slot["skipped"] += int(item.get("skipped") or 0)
    batch_totals = {
        "batches": len(batch_rows),
        "requested": sum(int(item.get("requested") or 0) for item in batch_rows),
        "updated_total": sum(int(item.get("updated") or 0) for item in batch_rows),
        "skipped_total": sum(int(item.get("skipped") or 0) for item in batch_rows),
        "by_kind": [
            {"kind": key, **value}
            for key, value in sorted(batch_by_kind.items())
        ],
    }
    notification_summary = {
        "total": int(notification_total),
        "unread_total": int(notification_unread),
        "read_total": max(0, int(notification_total) - int(notification_unread)),
    }

    caliber = {
        "submissions": "广场投稿总数（全部状态）；public_visible 为匿名可读状态数",
        "by_subject": "按学科分组的投稿数及公开状态分布（published/verified/community_review）",
        "by_type": "按题型分组的投稿数及公开状态分布",
        "scorable": "公开可见题中快照含参考答案（含子题答案）的占比",
        "figures": "公开可见题当前版本配图引用中，可匿名读取的公开衍生图占比",
        "review_duration": (
            "initial_review_accepted（材料齐备）→ threshold_met（社区核验通过）的"
            "中位小时数；submitted_to_published 为送审→签发的中位小时数"
        ),
        "quality": (
            "抽检记录口径：issue_rate=问题记录/抽检记录；high_issue_rate=高风险/"
            "抽检记录；sampling 为试点首批 100% 抽检覆盖率；gate 为抽检门禁"
            "（模式/激活/试点窗口/阻断/override/通过次数）"
        ),
        "feedback": "纠错/举报/申诉总数与未处理（pending）数",
        "withdrawals": "发布撤回数、投稿撤回数、回滚次数与一键恢复次数（事件链口径）",
        "ops": "编辑台批量处理批次/请求/成功/跳过数（plaza_ops_batches 口径）",
        "notifications": "站内通知总量与未读总量（admin 聚合口径，不含用户身份）",
    }

    return {
        "generated_at": utcnow(),
        "totals": {
            "submissions": len(submissions),
            "public_visible": len(public_rows),
            "published": status_counts["published"],
            "verified": status_counts["verified"],
            "community_review": status_counts["community_review"],
            "withdrawn": status_counts["withdrawn"],
            "rejected": status_counts["rejected"],
            "by_status": status_counts,
        },
        "by_subject": [
            {"subject": key, **value}
            for key, value in sorted(
                by_subject.items(), key=lambda item: (-item[1]["count"], item[0]),
            )
        ],
        "by_type": [
            {"type": key, **value}
            for key, value in sorted(
                by_type.items(), key=lambda item: (-item[1]["count"], item[0]),
            )
        ],
        "scorable": {
            "total": len(public_rows),
            "scorable": scorable,
            "rate": _rate(scorable, len(public_rows)),
        },
        "figures": {
            "total_images": image_refs,
            "public_images": public_images,
            "unavailable": max(0, image_refs - public_images),
            "coverage_rate": _rate(public_images, image_refs),
        },
        "review_duration": {
            "samples": len(review_hours),
            "median_hours": _median(review_hours),
            "submitted_to_published": {
                "samples": len(publish_hours),
                "median_hours": _median(publish_hours),
            },
        },
        "quality": {
            "checks_total": checks_total,
            "passed": passed,
            "issues": issues,
            "high_issues": high_issues,
            "issue_rate": _rate(issues, checks_total),
            "high_issue_rate": _rate(high_issues, checks_total),
            "pending_checks": pending_checks,
            "pre_publish_checks": pre_publish_checks,
            "post_publish_checks": len(post_publish_checks),
            "gate": gate_summary,
            "sampling": sampling,
        },
        "feedback": {
            "issues_total": feedback["issues"]["total"],
            "reports_total": feedback["reports"]["total"],
            "appeals_total": feedback["appeals"]["total"],
            "issues_unresolved": feedback["issues"]["unresolved"],
            "reports_unresolved": feedback["reports"]["unresolved"],
            "appeals_unresolved": feedback["appeals"]["unresolved"],
        },
        "withdrawals": {
            "releases_withdrawn": releases_withdrawn,
            "submissions_withdrawn": status_counts["withdrawn"],
            "rollbacks": rollbacks,
            "restorations": restored,
        },
        "restorations": {
            "total": restored,
            "latest_event": next(
                (
                    str(item.get("created_at") or "")
                    for item in reversed(events)
                    if str(item.get("action") or "") == "release_restored"
                ),
                "",
            ),
        },
        "ops": {
            "batch": batch_totals,
        },
        "notifications": notification_summary,
        "caliber": caliber,
    }


__all__ = ["admin_metrics"]
