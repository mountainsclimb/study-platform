"""WS27 运维监控看板：``GET /api/admin/monitor`` 五组只读检查、权限、短缓存与降级。

方案 §7「分发与运维」要求监控作业失败、数据库等待、OCR 延迟、渲染队列、违规举报与撤回传播。
本文件不联网、不启动真实 Worker/浏览器，直接在隔离应用库中造数据后断言看板输出。

覆盖：
1. 权限：管理员 200、普通用户 403、匿名 401、部署探针口令（``X-Health-Token``）只读可读；
2. 结构：``{ok, status, generated_at, build, checks{tasks,ai,media,data,plaza}}`` 与阈值/缓存元信息；
3. tasks：按 kind 的排队/运行/失败率/超时/中断回收、最老排队（含租约与超时阈值）；
4. ai：用量账本聚合（token/费用/失败率）、OCR P50/P95、失败率告警、账本不可用退回任务表；
5. media：课堂视频/旁白队列与失败、素材/课程状态、导出能力不可用时的排队告警；
6. data：应用库大小/WAL、题库版本与可读性、最近备份元数据与过期告警；
7. plaza：撤回传播抽样（撤回后仍公开 → error）、发布指针异常、正常发布无告警、反馈积压；
8. 工程约束：单检查抛错只标 error 不 500、短缓存与 refresh 绕过、源码只读（无写 SQL）、
   不泄露密钥/宿主绝对路径、路由注册且不影响既有管理接口。
"""
from __future__ import annotations

import json
from pathlib import Path
import os
import re
import time
import zipfile

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app import db, monitor
from app.main import app

ROOT = Path(__file__).resolve().parents[1]
MONITOR_PATH = "/api/admin/monitor"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _user(client) -> dict:
    response = client.get("/api/auth/me")
    assert response.status_code == 200, response.text
    return response.json()


def _iso(offset_seconds: float = 0) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=offset_seconds)).isoformat()


def _insert_job(
    user_id: int,
    *,
    task: str = "explain",
    status: str = "completed",
    created_at: str | None = None,
    updated_at: str | None = None,
    started_at: str | None = None,
    finished_at: str | None = None,
    heartbeat_at: str | None = None,
) -> int:
    now = db.utcnow()
    with db.connection() as con:
        return con.execute(
            "INSERT INTO ai_jobs(user_id,task,status,stage,request_json,result_json,error,"
            "cancelled,created_at,updated_at,started_at,finished_at,heartbeat_at,retry_count,dispatch_token) "
            "VALUES (?,?,?,?,?,NULL,'',0,?,?,?,?,?,0,NULL)",
            (
                user_id, task, status, status, json.dumps({"task": task}),
                created_at or now, updated_at or now, started_at, finished_at, heartbeat_at,
            ),
        ).lastrowid


def _insert_usage(
    user_id: int,
    job_id: int,
    *,
    kind: str = "ocr",
    status: str = "completed",
    duration_ms: int = 1000,
    tokens_in: int = 10,
    tokens_out: int = 20,
    cost_micro: int | None = None,
    created_at: str | None = None,
) -> None:
    with db.connection() as con:
        con.execute(
            "INSERT INTO ai_job_usage(user_id,job_id,kind,provider_id,model,status,duration_ms,"
            "retries,tokens_in,tokens_out,tokens_estimate,cost_micro,created_at,meta_json) "
            "VALUES (?,?,?,NULL,'test-model',?,?,0,?,?,0,?,?,'{}')",
            (
                user_id, job_id, kind, status, duration_ms,
                tokens_in, tokens_out, cost_micro, created_at or db.utcnow(),
            ),
        )


def _monitor(client, *, refresh: bool = True) -> dict:
    suffix = "?refresh=1" if refresh else ""
    response = client.get(MONITOR_PATH + suffix)
    assert response.status_code == 200, response.text
    return response.json()


@pytest.fixture(autouse=True)
def _reset_monitor_cache():
    monitor.reset_cache()
    yield
    monitor.reset_cache()


# ---------------------------------------------------------------------------
# 广场测试数据（真实广场表结构，只读监控不应依赖广场业务函数）
# ---------------------------------------------------------------------------

def _plaza_tables() -> None:
    from app.plaza import db as plaza_db

    plaza_db.ensure_tables()


def _insert_submission(user_id: int, *, status: str, public_id: str) -> int:
    now = db.utcnow()
    with db.connection() as con:
        return con.execute(
            "INSERT INTO plaza_submissions(author_id,version,current_version,subject,type,grade,"
            "snapshot_json,source_json,fingerprint,license_confirmed,status,public_id,"
            "created_at,updated_at) VALUES (?,1,1,'数学','SINGLE','高一','{}','{}','',0,?,?,?,?)",
            (user_id, status, public_id, now, now),
        ).lastrowid

def _insert_release(
    submission_id: int,
    *,
    public_id: str,
    status: str,
    active_public_id: str | None = None,
    withdrawn_at: str | None = None,
) -> int:
    now = db.utcnow()
    with db.connection() as con:
        return con.execute(
            "INSERT INTO plaza_releases(submission_id,version,public_id,status,active_public_id,"
            "published_by,published_at,withdrawn_at,withdraw_mode,reason) "
            "VALUES (?,1,?,?,?,NULL,?,?,'normal','')",
            (submission_id, public_id, status, active_public_id, now, withdrawn_at),
        ).lastrowid


def _insert_feedback(user_id: int, submission_id: int, public_id: str, table: str) -> None:
    now = db.utcnow()
    with db.connection() as con:
        if table == "plaza_appeals":
            con.execute(
                'INSERT INTO "plaza_appeals"(submission_id,user_id,reason,status,created_at,updated_at) '
                "VALUES (?,?,?,?,?,?)",
                (submission_id, user_id, "说明问题", "pending", now, now),
            )
        elif table == "plaza_reports":
            con.execute(
                'INSERT INTO "plaza_reports"(submission_id,public_id,user_id,reason,detail,status,'
                "created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
                (submission_id, public_id, user_id, "other", "说明问题", "pending", now, now),
            )
        else:
            con.execute(
                'INSERT INTO "plaza_issues"(submission_id,public_id,user_id,field,description,status,'
                "created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
                (submission_id, public_id, user_id, "other", "说明问题", "pending", now, now),
            )


# ---------------------------------------------------------------------------
# 1. 权限
# ---------------------------------------------------------------------------

def test_monitor_requires_admin(client, second_client):
    assert second_client.get(MONITOR_PATH).status_code == 403
    anon = TestClient(app)
    try:
        assert anon.get(MONITOR_PATH).status_code == 401
    finally:
        anon.close()


def test_monitor_health_token_probe_can_read(client, monkeypatch):
    monkeypatch.setenv("STUDY_HEALTH_TOKEN", "probe-secret-123")
    probe = TestClient(app)
    try:
        assert probe.get(MONITOR_PATH).status_code == 401
        assert probe.get(
            MONITOR_PATH + "?refresh=1", headers={"X-Health-Token": "wrong-token"},
        ).status_code == 401
        response = probe.get(
            MONITOR_PATH + "?refresh=1", headers={"X-Health-Token": "probe-secret-123"},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert set(body["checks"]) == set(monitor.CHECK_ORDER)
    finally:
        probe.close()


# ---------------------------------------------------------------------------
# 2. 结构与构建元信息
# ---------------------------------------------------------------------------

def test_monitor_structure_and_build_metadata(client):
    from app import main

    body = _monitor(client)
    assert body["ok"] is True, body["checks"]
    assert body["status"] == "ok"
    assert set(body["checks"]) == set(monitor.CHECK_ORDER) == {"tasks", "ai", "media", "data", "plaza"}
    for name, check in body["checks"].items():
        assert check["status"] in {"ok", "warn", "error"}, name
        assert isinstance(check["detail"], str) and check["detail"]
        assert isinstance(check["alerts"], list)
    generated = datetime.fromisoformat(body["generated_at"])
    assert generated.tzinfo is not None
    assert body["build"]["build_id"] == main.BUILD_ID
    assert body["build"]["version"] == main.VERSION
    assert body["build"]["bank_version"]
    assert body["window_days"] == monitor.DEFAULT_AI_WINDOW_DAYS
    assert isinstance(body["duration_ms"], float) and body["duration_ms"] >= 0
    assert body["cache"] == {"ttl_seconds": monitor.DEFAULT_CACHE_TTL_SECONDS,
                             "cached": False, "age_seconds": 0.0}
    thresholds = body["thresholds"]
    assert thresholds["lease_seconds"] >= 1
    assert thresholds["job_timeout_seconds"] >= 1
    assert thresholds["queue_warn_seconds"] < thresholds["queue_error_seconds"]
    assert thresholds["db_wait_warn_ms"] > 0


def test_monitor_ai_window_is_configurable(client, monkeypatch):
    monkeypatch.setenv("STUDY_MONITOR_AI_WINDOW_DAYS", "3")
    monitor.reset_cache()
    body = _monitor(client)
    assert body["window_days"] == 3
    assert body["checks"]["ai"]["window_days"] == 3
    assert body["checks"]["tasks"]["window_days"] == 3


# ---------------------------------------------------------------------------
# 3. tasks
# ---------------------------------------------------------------------------

def test_tasks_check_reports_queue_failures_and_reclaim(client):
    user_id = _user(client)["id"]
    _insert_job(user_id, task="explain", status="queued", created_at=_iso(-400))
    _insert_job(
        user_id, task="explain", status="running",
        created_at=_iso(-40), started_at=_iso(-30), heartbeat_at=_iso(-5),
    )
    for _ in range(2):
        _insert_job(user_id, task="ocr", status="failed", finished_at=_iso(-10))
    for _ in range(2):
        _insert_job(user_id, task="ocr", status="completed", finished_at=_iso(-10))
    _insert_job(user_id, task="grade", status="interrupted", finished_at=_iso(-20))

    tasks = _monitor(client)["checks"]["tasks"]
    assert tasks["totals"]["queued"] == 1
    assert tasks["totals"]["running"] == 1
    assert tasks["totals"]["failed"] == 2
    assert tasks["reclaimed"] == 1
    assert tasks["oldest_queued_seconds"] >= 350
    assert tasks["oldest_queued_kind"] == "explain"
    assert tasks["failure_rate"] >= 0.25
    assert tasks["status"] in {"warn", "error"}
    kinds = {row["kind"]: row for row in tasks["by_kind"]}
    assert kinds["explain"]["queued"] == 1
    assert kinds["explain"]["running"] == 1
    assert kinds["ocr"]["failed"] == 2 and kinds["ocr"]["completed"] == 2
    assert kinds["grade"]["interrupted"] == 1
    assert any("排队" in alert or "失败率" in alert for alert in tasks["alerts"])


def test_tasks_check_flags_timeout_and_stale_running(client, monkeypatch):
    monkeypatch.setenv("STUDY_MONITOR_JOB_TIMEOUT_SECONDS", "60")
    monkeypatch.setenv("STUDY_MONITOR_LEASE_SECONDS", "30")
    monitor.reset_cache()
    user_id = _user(client)["id"]
    _insert_job(
        user_id, task="course-video", status="running",
        created_at=_iso(-7200), started_at=_iso(-7200), heartbeat_at=None,
    )
    tasks = _monitor(client)["checks"]["tasks"]
    assert tasks["timed_out"] == 1
    assert tasks["stale_running"] == 1
    assert tasks["status"] == "warn"
    assert any("超时" in alert for alert in tasks["alerts"])


# ---------------------------------------------------------------------------
# 4. ai
# ---------------------------------------------------------------------------

def test_ai_check_aggregates_ledger_and_ocr_percentiles(client):
    user_id = _user(client)["id"]
    jobs = [_insert_job(user_id, task="ocr", status="completed") for _ in range(3)]
    _insert_usage(user_id, jobs[0], kind="ocr", duration_ms=1000,
                  tokens_in=100, tokens_out=50, cost_micro=2000)
    _insert_usage(user_id, jobs[1], kind="ocr", duration_ms=2000,
                  tokens_in=200, tokens_out=100, cost_micro=4000)
    _insert_usage(user_id, jobs[2], kind="ocr", duration_ms=3000,
                  tokens_in=0, tokens_out=0, cost_micro=None)

    ai = _monitor(client)["checks"]["ai"]
    assert ai["source"] == "ai_job_usage"
    totals = ai["totals"]
    assert totals["jobs"] == 3
    assert totals["tokens_in"] == 300
    assert totals["tokens_out"] == 150
    assert totals["tokens_total"] == 450
    assert totals["cost_micro"] == 6000
    assert totals["cost_covered_jobs"] == 2
    assert totals["cost_unpriced_jobs"] == 1
    assert ai["ocr"]["jobs"] == 3
    assert ai["ocr"]["duration_p50_ms"] == 2000
    assert ai["ocr"]["duration_p95_ms"] >= 2800
    by_kind = {row["kind"]: row for row in ai["by_kind"]}
    assert by_kind["ocr"]["jobs"] == 3
    assert by_kind["ocr"]["label"]


def test_ai_check_reports_failure_rate(client):
    user_id = _user(client)["id"]
    jobs = [_insert_job(user_id, task="explain", status="failed") for _ in range(2)]
    jobs += [_insert_job(user_id, task="explain", status="completed") for _ in range(2)]
    for job in jobs:
        _insert_usage(user_id, job, kind="explain", status="failed" if job in jobs[:2] else "completed")
    ai = _monitor(client)["checks"]["ai"]
    assert ai["totals"]["jobs"] == 4
    assert ai["totals"]["failed"] == 2
    assert ai["totals"]["failure_rate"] == 0.5
    assert ai["status"] == "warn"
    assert any("失败率" in alert for alert in ai["alerts"])


def test_ai_check_falls_back_to_job_table(client, monkeypatch):
    def _boom(*args, **kwargs):
        raise RuntimeError("ledger unavailable")

    monkeypatch.setattr(monitor, "_ledger_rows", _boom)
    user_id = _user(client)["id"]
    _insert_job(
        user_id, task="explain", status="completed",
        created_at=_iso(-20), started_at=_iso(-10), finished_at=_iso(-5),
    )
    ai = _monitor(client)["checks"]["ai"]
    assert ai["source"] == "ai_jobs"
    assert ai["status"] == "warn"
    assert ai["totals"]["jobs"] == 1
    assert any("账本" in alert for alert in ai["alerts"])


def test_ai_check_warns_on_slow_ocr(client):
    user_id = _user(client)["id"]
    for duration in (130_000, 140_000, 150_000):
        job_id = _insert_job(user_id, task="ocr", status="completed")
        _insert_usage(user_id, job_id, kind="ocr", duration_ms=duration)
    ai = _monitor(client)["checks"]["ai"]
    assert ai["ocr"]["duration_p95_ms"] >= monitor.OCR_P95_WARN_MS
    assert ai["status"] == "warn"
    assert any("OCR" in alert for alert in ai["alerts"])


# ---------------------------------------------------------------------------
# 5. media
# ---------------------------------------------------------------------------

def test_media_check_reports_classroom_queues_and_assets(client):
    user_id = _user(client)["id"]
    _insert_job(user_id, task="course-video", status="queued", created_at=_iso(-200))
    _insert_job(user_id, task="course-video", status="failed", finished_at=_iso(-60))
    _insert_job(user_id, task="course-narration", status="completed", finished_at=_iso(-60))
    now = db.utcnow()
    with db.connection() as con:
        course_id = con.execute(
            "INSERT INTO cls_courses(user_id,title,topic,status,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?)",
            (user_id, "课程", "主题", "failed", now, now),
        ).lastrowid
        for kind, name in (("video", "v.mp4"), ("tts", "a.mp3")):
            con.execute(
                "INSERT INTO cls_assets(user_id,course_id,kind,storage_name,mime_type,sha256,"
                "meta_json,created_at) VALUES (?,?,?,?,?,?,?,?)",
                (user_id, course_id, kind, name, "application/octet-stream", "", "{}", now),
            )

    media = _monitor(client)["checks"]["media"]
    assert media["totals"]["queued"] == 1
    assert media["totals"]["failed"] == 1
    assert media["oldest_queued_seconds"] >= 150
    assert media["status"] in {"warn", "error"}
    kinds = {row["kind"]: row for row in media["by_kind"]}
    assert kinds["course-video"]["queued"] == 1
    assert kinds["course-video"]["failed"] == 1
    assert kinds["course-narration"]["completed"] == 1
    assert media["assets"]["by_kind"].get("video") == 1
    assert media["assets"]["by_kind"].get("tts") == 1
    assert media["courses"].get("failed") == 1
    assert set(media["capabilities"]) >= {"render", "ffmpeg", "pdf", "video"}
    assert isinstance(media["capabilities"]["video"]["available"], bool)


def test_media_queue_warns_when_video_capability_unavailable(client, monkeypatch):
    monkeypatch.setattr(monitor, "_media_capabilities", lambda: {
        "render": {"available": False, "detail": "渲染不可用"},
        "ffmpeg": {"available": False, "detail": "未检测到 ffmpeg"},
        "pdf": {"available": False, "detail": "PDF 不可用"},
        "video": {"available": False, "detail": "视频不可用"},
    })
    user_id = _user(client)["id"]
    _insert_job(user_id, task="course-video", status="queued", created_at=_iso(-5))
    media = _monitor(client)["checks"]["media"]
    assert media["capabilities"]["video"]["available"] is False
    assert media["status"] == "warn"
    assert any("能力不可用" in alert for alert in media["alerts"])


# ---------------------------------------------------------------------------
# 6. data
# ---------------------------------------------------------------------------

def test_data_check_reports_database_bank_and_backup(client):
    data = _monitor(client)["checks"]["data"]
    assert data["engine"] == "sqlite"
    assert data["database"]["readable"] is True
    assert data["database"]["size_bytes"] > 0
    assert data["database"]["wal_bytes"] is None or data["database"]["wal_bytes"] >= 0
    assert data["database"]["table_count"] > 0
    assert data["bank"]["present"] is True
    assert data["bank"]["readable"] is True
    assert data["bank"]["questions"] > 0
    assert data["bank"]["version"]
    assert data["backups"]["count"] == 0
    assert data["status"] == "ok", data


def test_data_check_reads_latest_backup_manifest(client):
    backup_dir = Path(db.DATA_DIR) / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    archive = backup_dir / "zhixu-backup-20260101-000000-000000.zip"
    manifest = {
        "created_at": db.utcnow(),
        "file_count": 3,
        "bank_version": db.bank_version_hint(),
        "format_version": 2,
        "key_included": False,
        "files": {"app.sqlite3": {"size": 1, "sha256": "0" * 64}},
    }
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False))
        zf.writestr("app.sqlite3", b"snapshot")

    data = _monitor(client)["checks"]["data"]
    backups = data["backups"]
    assert backups["count"] == 1
    latest = backups["latest"]
    assert latest["name"] == archive.name
    assert latest["manifest_ok"] is True
    assert latest["file_count"] == 3
    assert latest["bank_version"] == db.bank_version_hint()
    assert latest["age_seconds"] < 120
    assert data["status"] == "ok", data


def test_data_check_warns_on_stale_backup(client):
    backup_dir = Path(db.DATA_DIR) / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    archive = backup_dir / "zhixu-backup-20251101-000000-000000.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("manifest.json", json.dumps({"created_at": db.utcnow(), "files": {}}))
    stale = time.time() - 72 * 3600
    os.utime(archive, (stale, stale))

    data = _monitor(client)["checks"]["data"]
    assert data["status"] == "warn"
    assert any("备份" in alert for alert in data["alerts"])


# ---------------------------------------------------------------------------
# 7. plaza
# ---------------------------------------------------------------------------

def test_plaza_check_disabled_without_tables(client):
    plaza = _monitor(client)["checks"]["plaza"]
    assert plaza["status"] == "ok"
    assert plaza["initialized"] is False
    assert plaza["feedback"]["total_pending"] == 0
    assert plaza["withdrawn"]["checked"] == 0


def test_plaza_withdrawn_propagation_flags_leak(client):
    _plaza_tables()
    user_id = _user(client)["id"]
    # 正常撤回：投稿与发布指针都已是 withdrawn。
    good_public_id = "plz-" + "a" * 20
    good = _insert_submission(user_id, status="withdrawn", public_id=good_public_id)
    _insert_release(good, public_id=good_public_id, status="withdrawn", withdrawn_at=db.utcnow())
    # 异常撤回传播：发布指针已 withdrawn，但投稿状态仍是 published（公开读仍可见）。
    leaked_public_id = "plz-" + "b" * 20
    bad = _insert_submission(user_id, status="published", public_id=leaked_public_id)
    _insert_release(bad, public_id=leaked_public_id, status="withdrawn", withdrawn_at=db.utcnow())

    body = _monitor(client)
    plaza = body["checks"]["plaza"]
    withdrawn = plaza["withdrawn"]
    assert withdrawn["checked"] == 2
    assert withdrawn["leaked"] == 1
    assert withdrawn["leaked_items"][0]["public_id"] == leaked_public_id
    assert plaza["status"] == "error"
    assert any("撤回传播" in alert for alert in plaza["alerts"])
    assert body["ok"] is False and body["status"] == "error"


def test_plaza_republished_public_id_is_not_reported_as_leak(client):
    """同一 public_id 撤回后重新签发 active 新版：旧撤回记录不能误报成撤回传播泄露。"""
    _plaza_tables()
    user_id = _user(client)["id"]
    public_id = "plz-" + "f" * 20
    submission = _insert_submission(user_id, status="published", public_id=public_id)
    _insert_release(submission, public_id=public_id, status="withdrawn", withdrawn_at=db.utcnow())
    _insert_release(submission, public_id=public_id, status="active", active_public_id=public_id)

    plaza = _monitor(client)["checks"]["plaza"]
    assert plaza["withdrawn"]["checked"] >= 1
    assert plaza["withdrawn"]["leaked"] == 0
    assert plaza["withdrawn"]["orphan_active"] == 0
    assert plaza["status"] == "ok", plaza


def test_plaza_orphan_active_pointer_is_error(client):
    _plaza_tables()
    user_id = _user(client)["id"]
    public_id = "plz-" + "c" * 20
    submission = _insert_submission(user_id, status="withdrawn", public_id=public_id)
    _insert_release(submission, public_id=public_id, status="active", active_public_id=public_id)

    plaza = _monitor(client)["checks"]["plaza"]
    assert plaza["withdrawn"]["orphan_active"] == 1
    assert plaza["withdrawn"]["orphan_active_items"][0]["public_id"] == public_id
    assert plaza["status"] == "error"
    assert any("发布指针" in alert for alert in plaza["alerts"])


def test_plaza_healthy_release_is_ok(client):
    _plaza_tables()
    user_id = _user(client)["id"]
    public_id = "plz-" + "d" * 20
    submission = _insert_submission(user_id, status="published", public_id=public_id)
    _insert_release(submission, public_id=public_id, status="active", active_public_id=public_id)

    plaza = _monitor(client)["checks"]["plaza"]
    assert plaza["initialized"] is True
    assert plaza["releases"]["active"] == 1
    assert plaza["withdrawn"]["leaked"] == 0
    assert plaza["withdrawn"]["orphan_active"] == 0
    assert plaza["withdrawn"]["published_without_active"] == 0
    assert plaza["status"] == "ok", plaza


def test_plaza_feedback_backlog_warns(client, monkeypatch):
    monkeypatch.setenv("STUDY_MONITOR_FEEDBACK_WARN_PENDING", "2")
    monitor.reset_cache()
    _plaza_tables()
    user_id = _user(client)["id"]
    public_id = "plz-" + "e" * 20
    submission = _insert_submission(user_id, status="community_review", public_id=public_id)
    for table in ("plaza_issues", "plaza_reports", "plaza_appeals"):
        _insert_feedback(user_id, submission, public_id, table)

    plaza = _monitor(client)["checks"]["plaza"]
    assert plaza["feedback"]["issues"] == 1
    assert plaza["feedback"]["reports"] == 1
    assert plaza["feedback"]["appeals"] == 1
    assert plaza["feedback"]["total_pending"] == 3
    assert plaza["status"] == "warn"
    assert any("反馈" in alert for alert in plaza["alerts"])


# ---------------------------------------------------------------------------
# 8. 工程约束：降级 / 缓存 / 只读 / 不泄露
# ---------------------------------------------------------------------------

def test_single_check_failure_degrades_without_500(client, monkeypatch):
    def _boom(ctx):
        raise RuntimeError("data check exploded")

    monkeypatch.setitem(monitor._CHECK_FUNCS, "data", _boom)
    response = client.get(MONITOR_PATH + "?refresh=1")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["checks"]["data"]["status"] == "error"
    assert body["checks"]["data"]["detail"]
    assert body["ok"] is False and body["status"] == "error"
    assert "data" in body["degraded"]
    for name in ("tasks", "ai", "media", "plaza"):
        assert body["checks"][name]["status"] in {"ok", "warn"}


def test_monitor_short_cache_and_refresh(client):
    user_id = _user(client)["id"]
    first = _monitor(client)
    assert first["cache"]["cached"] is False
    cached = client.get(MONITOR_PATH).json()
    assert cached["cache"]["cached"] is True
    assert cached["generated_at"] == first["generated_at"]
    assert cached["cache"]["age_seconds"] >= 0

    # 缓存期内新增任务：普通请求仍返回缓存结果。
    _insert_job(user_id, task="explain", status="queued", created_at=_iso(-5))
    still_cached = client.get(MONITOR_PATH).json()
    assert still_cached["checks"]["tasks"]["totals"]["queued"] == \
        first["checks"]["tasks"]["totals"]["queued"]
    # refresh=1 绕过缓存，立即反映新任务。
    refreshed = client.get(MONITOR_PATH + "?refresh=1").json()
    assert refreshed["cache"]["cached"] is False
    assert refreshed["checks"]["tasks"]["totals"]["queued"] == \
        first["checks"]["tasks"]["totals"]["queued"] + 1


def test_monitor_cache_can_be_disabled(client, monkeypatch):
    monkeypatch.setenv("STUDY_MONITOR_CACHE_SECONDS", "0")
    monitor.reset_cache()
    first = client.get(MONITOR_PATH).json()
    second = client.get(MONITOR_PATH).json()
    assert first["cache"]["cached"] is False
    assert second["cache"]["cached"] is False
    assert second["cache"]["ttl_seconds"] == 0


def test_monitor_does_not_leak_secrets_or_host_paths(client, monkeypatch):
    api_key = "sk-test-monitor-secret-123456"
    probe_token = "probe-token-monitor-secret"
    monkeypatch.setenv("OPENAI_API_KEY", api_key)
    monkeypatch.setenv("STUDY_HEALTH_TOKEN", probe_token)
    text = client.get(MONITOR_PATH + "?refresh=1").text
    assert api_key not in text
    assert probe_token not in text
    assert str(db.DATA_DIR) not in text
    assert "api-key.key" not in text


def test_monitor_source_is_read_only_and_portable():
    source = (ROOT / "app" / "monitor.py").read_text(encoding="utf-8")
    upper = source.upper()
    for keyword in ("INSERT INTO", "UPDATE ", "DELETE FROM", "DROP TABLE",
                    "CREATE TABLE", "ALTER TABLE", "BEGIN IMMEDIATE", "COMMIT"):
        assert keyword not in upper, f"监控模块不应包含写 SQL：{keyword}"
    # 监控必须走 db 封装并探测表存在（SQLite / PostgreSQL 双兼容）。
    assert "db.connection(" in source
    assert "db.table_names(" in source
    for sqlite_only in ("julianday(", "strftime(", "sqlite_master"):
        assert sqlite_only not in source, f"监控 SQL 不应依赖 SQLite 专有函数：{sqlite_only}"


def test_monitor_route_registered_and_admin_endpoints_intact(client):
    paths = client.get("/openapi.json").json()["paths"]
    assert MONITOR_PATH in paths
    assert "get" in paths[MONITOR_PATH]
    overview = client.get("/api/admin/overview")
    assert overview.status_code == 200
    assert overview.json()["users"] >= 1
    ready = client.get("/api/health/ready")
    assert ready.status_code == 200
    assert ready.json()["checks"]["app_db"]["ok"] is True


# ---------------------------------------------------------------------------
# 9. 管理台前端契约（静态，不启动浏览器）
# ---------------------------------------------------------------------------

ADMIN_JS = ROOT / "web" / "js" / "pages" / "admin.js"
ZH_LOCALE = ROOT / "web" / "locales" / "zh-CN.json"
EN_LOCALE = ROOT / "web" / "locales" / "en-US.json"


def test_admin_monitor_card_contract():
    source = ADMIN_JS.read_text(encoding="utf-8")
    assert "function adminMonitorCard()" in source, "管理台缺少运维监控卡片"
    assert "/admin/monitor" in source
    assert "refresh=1" in source, "刷新按钮应绕过服务端短缓存"
    assert "adminMonitorCard()," in source, "运维监控卡片未接入管理台分区"
    # 失败降级：接口不可用时只提示并给重试，不抛异常。
    assert "admin.monitor.unavailable" in source
    assert "admin.monitor.reload" in source
    # 五组检查分组标签与告警色（分组名通过模板字符串取键，先断言分组清单）。
    assert "MONITOR_GROUP_KEYS = ['tasks', 'ai', 'media', 'data', 'plaza']" in source
    assert "admin.monitor.group.${key}" in source
    for status in ("ok", "warn", "error"):
        assert f"admin.monitor.status.{status}" in source
    assert "settings-usage-warn" in source or "notice(" in source


def test_admin_monitor_locale_keys_aligned():
    zh = json.loads(ZH_LOCALE.read_text(encoding="utf-8"))
    en = json.loads(EN_LOCALE.read_text(encoding="utf-8"))
    zh_keys = {key for key in zh if key.startswith("admin.monitor.")}
    en_keys = {key for key in en if key.startswith("admin.monitor.")}
    assert len(zh_keys) >= 50, f"admin.monitor.* 键过少：{len(zh_keys)}"
    assert zh_keys == en_keys, f"中英文键不一致：{sorted(zh_keys ^ en_keys)}"
    for key in sorted(zh_keys):
        assert str(zh[key]).strip(), f"中文空值：{key}"
        assert str(en[key]).strip(), f"英文空值：{key}"
    # admin.js 里直接引用的字面量键必须都在两种语言中。
    source = ADMIN_JS.read_text(encoding="utf-8")
    literals = {
        key for key in re.findall(r"t\('([^']+)'", source)
        if key.startswith("admin.monitor.")
    }
    assert literals, "admin.js 未引用 admin.monitor.* 字面量键"
    missing = sorted(key for key in literals if key not in zh or key not in en)
    assert not missing, f"前端键缺失：{missing}"
