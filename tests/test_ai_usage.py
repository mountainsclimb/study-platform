"""WS23 AI 结果可追踪与配额/并发：``ai_job_usage`` 账本、按类型配额、全站并发与查询接口。

覆盖：
- 任务成功 / 失败 / 取消 / 中断统一入账，且 ``job_id`` 唯一（幂等）；
- token（逐次调用账本汇总）/ 耗时 / 重试 / 费用估算（无单价降级为空）；
- 入参只记分类（text/image/pdf/audio/none），不记录文件名、题干等用户内容；
- 按任务类型的每人每日配额与全站并发（拒绝 429 且不落库、失败不消耗配额、管理员可调）；
- 本人只能看自己的用量；全站汇总、P50/P95、筛选与分页仅管理员可用；
- 保留策略（按天清理）边界；并发计数在数据库异常时的内存兜底。
"""
from datetime import datetime, timedelta, timezone
from io import BytesIO
import json

from fastapi.testclient import TestClient
import pytest
from PIL import Image

from app import db
from app.ai import jobs as ai_jobs
from app.ai import usage
from app.main import app
from test_ai import save_provider


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _user(client) -> dict:
    return client.get("/api/auth/me").json()


def _utc(offset_seconds: float = 0) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=offset_seconds)).isoformat()


def _capture_dispatch(monkeypatch) -> list:
    dispatched = []

    async def fake_dispatch(job_id, token, runner):
        dispatched.append((job_id, token))

    monkeypatch.setattr(ai_jobs, "_dispatch", fake_dispatch)
    return dispatched


def _insert_job(user_id: int, *, task="explain", status="completed", request=None,
                result=None, started_at=None, finished_at=None, retry_count=0,
                created_at=None, updated_at=None):
    now = db.utcnow()
    payload = {"task": task, "provider_id": 1, "model": "test-model"}
    payload.update(request or {})
    with db.connection() as con:
        return con.execute(
            "INSERT INTO ai_jobs(user_id,task,status,stage,request_json,result_json,partial_content,"
            "cancelled,created_at,updated_at,started_at,finished_at,retry_count,dispatch_token) "
            "VALUES (?,?,?,?,?,?,'',0,?,?,?,?,?,NULL)",
            (
                user_id, task, status, status, json.dumps(payload),
                json.dumps(result) if result is not None else None,
                created_at or now, updated_at or now, started_at, finished_at, retry_count,
            ),
        ).lastrowid


def _insert_model_calls(job_id: int, calls):
    with db.connection() as con:
        for tokens_in, tokens_out in calls:
            con.execute(
                "INSERT INTO ai_usage(user_id,job_id,provider_id,model,input_tokens,output_tokens,created_at) "
                "VALUES (1,?,1,'test-model',?,?,?)",
                (job_id, tokens_in, tokens_out, db.utcnow()),
            )


def _insert_upload(user_id: int, filename="题目.txt", media="text/plain") -> int:
    with db.connection() as con:
        return con.execute(
            "INSERT INTO ai_uploads(user_id,filename,storage_name,media_type,text_content,created_at) "
            "VALUES (?,?,?,?,?,?)",
            (user_id, filename, filename, media, "", db.utcnow()),
        ).lastrowid


def _real_upload(client, name="题目.txt", content=b"1+1=?", media="text/plain") -> dict:
    response = client.post("/api/uploads/question", files={"file": (name, content, media)})
    assert response.status_code == 200, response.text
    return response.json()


def _image_upload(client) -> dict:
    image = BytesIO()
    Image.new("RGB", (24, 24), "white").save(image, "PNG")
    return _real_upload(client, "题目.png", image.getvalue(), "image/png")


def _scanned_document(client) -> dict:
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=600, height=800)
    raw = BytesIO()
    writer.write(raw)
    response = client.post("/api/documents", files={"file": ("扫描.pdf", raw.getvalue(), "application/pdf")})
    assert response.status_code == 200, response.text
    return response.json()


def _usage_row(job_id: int):
    with db.connection() as con:
        return con.execute("SELECT * FROM ai_job_usage WHERE job_id=?", (job_id,)).fetchone()


def _usage_count() -> int:
    with db.connection() as con:
        return con.execute("SELECT COUNT(*) FROM ai_job_usage").fetchone()[0]


def _job_count() -> int:
    with db.connection() as con:
        return con.execute("SELECT COUNT(*) FROM ai_jobs").fetchone()[0]


def _set_prices(user_id: int, provider_id: int, model: str, input_price, output_price):
    with db.connection() as con:
        con.execute(
            "UPDATE ai_models SET input_price=?, output_price=? "
            "WHERE user_id=? AND provider_id=? AND model_id=?",
            (input_price, output_price, user_id, provider_id, model),
        )


@pytest.fixture(autouse=True)
def _clean_usage_runtime():
    usage.reset_runtime_state()
    yield
    usage.reset_runtime_state()


# ---------------------------------------------------------------------------
# 1. 建表与生命周期入账
# ---------------------------------------------------------------------------

def test_usage_schema_created_and_job_unique(client):
    with db.connection() as con:
        tables = db.table_names(con)
        job_columns = {row[1] for row in con.execute("PRAGMA table_info(ai_jobs)")}
        indexes = {row[1] for row in con.execute("PRAGMA index_list(ai_job_usage)")}
    assert "ai_job_usage" in tables and "ai_quotas" in tables
    assert {"started_at", "retry_count"} <= job_columns
    assert "idx_ai_job_usage_job" in indexes


def test_completed_job_records_model_duration_tokens_and_retries(client):
    user = _user(client)
    job_id = _insert_job(
        user["id"], task="explain", status="completed",
        request={"provider_id": 1, "model": "test-model"},
        result={"prompt_version": "prompt-v9", "content": "讲解内容"},
        started_at="2026-11-21T10:00:00+00:00", finished_at="2026-11-21T10:00:02.500000+00:00",
        retry_count=2,
    )
    _insert_model_calls(job_id, [(100, 20), (50, 5)])
    assert usage.record_job_usage(job_id) is True
    row = _usage_row(job_id)
    assert row["kind"] == "explain"
    assert row["model"] == "test-model"
    assert row["provider_id"] == 1
    assert row["status"] == "completed"
    assert row["duration_ms"] == 2500
    assert row["retries"] == 2
    assert row["tokens_in"] == 150 and row["tokens_out"] == 25
    assert row["tokens_estimate"] == 0
    meta = json.loads(row["meta_json"])
    assert meta["input_kind"] == "text"
    assert meta["prompt_version"] == "prompt-v9"


def test_failed_job_is_recorded_but_does_not_consume_quota(client):
    user = _user(client)
    job_id = _insert_job(user["id"], status="failed")
    assert usage.record_job_usage(job_id) is True
    assert _usage_row(job_id)["status"] == "failed"
    with db.connection() as con:
        assert usage.daily_used(con, user["id"], "explain") == 0


def test_cancelled_job_via_finalize_cancel_is_recorded(client):
    user = _user(client)
    job_id = _insert_job(user["id"], status="running")
    assert ai_jobs._finalize_cancel(job_id) is True
    row = _usage_row(job_id)
    assert row is not None and row["status"] == "cancelled"
    with db.connection() as con:
        assert usage.daily_used(con, user["id"], "explain") == 0


def test_terminal_hooks_record_ledger_and_started_at(client):
    user = _user(client)
    job_id = _insert_job(user["id"], status="queued")
    ai_jobs.update_job(job_id, "running", "preparing")
    ai_jobs.update_job(job_id, "completed")
    with db.connection() as con:
        job = con.execute("SELECT started_at, finished_at FROM ai_jobs WHERE id=?", (job_id,)).fetchone()
    assert job["started_at"] and job["finished_at"]
    row = _usage_row(job_id)
    assert row is not None and row["status"] == "completed"
    assert row["duration_ms"] >= 0


def test_cancel_route_settles_cancelled_usage(client):
    user = _user(client)
    job_id = _insert_job(user["id"], status="queued")
    response = client.post(f"/api/ai/jobs/{job_id}/cancel")
    assert response.status_code == 200 and response.json()["status"] == "cancelled"
    row = _usage_row(job_id)
    assert row is not None and row["status"] == "cancelled"


def test_record_usage_is_idempotent_per_job(client):
    user = _user(client)
    job_id = _insert_job(user["id"], status="failed")
    assert usage.record_job_usage(job_id) is True
    assert usage.record_job_usage(job_id) is True
    assert _usage_count() == 1
    # 失败后续跑完成：同一 job 只保留一行，并更新为最终状态。
    with db.connection() as con:
        con.execute("UPDATE ai_jobs SET status='completed' WHERE id=?", (job_id,))
    assert usage.record_job_usage(job_id) is True
    assert _usage_count() == 1
    assert _usage_row(job_id)["status"] == "completed"


def test_record_usage_skips_non_terminal_job(client):
    user = _user(client)
    job_id = _insert_job(user["id"], status="running")
    assert usage.record_job_usage(job_id) is False
    assert _usage_row(job_id) is None


def test_record_usage_tolerates_missing_job_and_bad_request_json(client):
    assert usage.record_job_usage(999999) is False
    user = _user(client)
    with db.connection() as con:
        job_id = con.execute(
            "INSERT INTO ai_jobs(user_id,task,status,stage,request_json,partial_content,cancelled,"
            "created_at,updated_at,dispatch_token) VALUES (?,?,?,?,'not-json','',0,?,?,NULL)",
            (user["id"], "explain", "failed", "failed", db.utcnow(), db.utcnow()),
        ).lastrowid
    assert usage.record_job_usage(job_id) is True
    assert _usage_row(job_id)["model"] == ""


# ---------------------------------------------------------------------------
# 2. token / 费用 / 入参分类
# ---------------------------------------------------------------------------

def test_token_estimate_when_provider_reports_no_usage(client):
    user = _user(client)
    job_id = _insert_job(
        user["id"], status="completed",
        result={"content": "讲解正文" * 200},
        started_at=_utc(), finished_at=_utc(0.2),
    )
    assert usage.record_job_usage(job_id) is True
    row = _usage_row(job_id)
    assert row["tokens_estimate"] == 1
    assert row["tokens_in"] > 0 and row["tokens_out"] > 0
    assert row["cost_micro"] is None  # 未配置单价：不按 0 元编造费用


def test_cost_micro_estimated_from_model_prices(client):
    user = _user(client)
    provider = save_provider(client)
    _set_prices(user["id"], provider["id"], "test-model", 4.0, 8.0)
    job_id = _insert_job(
        user["id"], status="completed", request={"provider_id": provider["id"], "model": "test-model"},
    )
    _insert_model_calls(job_id, [(500_000, 250_000)])
    assert usage.record_job_usage(job_id) is True
    # 单价口径：元 / 百万 tokens，因此 cost_micro = 500000×4 + 250000×8 = 4,000,000 微元 = ¥4。
    assert _usage_row(job_id)["cost_micro"] == 4_000_000


def test_cost_micro_is_null_when_price_missing(client):
    user = _user(client)
    provider = save_provider(client)
    job_id = _insert_job(
        user["id"], status="completed", request={"provider_id": provider["id"], "model": "test-model"},
    )
    _insert_model_calls(job_id, [(10, 5)])
    assert usage.record_job_usage(job_id) is True
    assert _usage_row(job_id)["cost_micro"] is None
    response = client.get("/api/admin/ai/usage?days=7")
    assert response.status_code == 200
    totals = response.json()["totals"]
    assert totals["cost_micro"] is None
    assert totals["cost_unpriced_jobs"] >= 1


@pytest.mark.parametrize("case,expected", [
    ("text", "text"),
    ("image", "image"),
    ("pdf", "pdf"),
    ("audio", "audio"),
    ("none", "none"),
])
def test_input_classification_only_records_category(client, case, expected):
    user = _user(client)
    request = {"provider_id": 1, "model": "test-model"}
    if case == "text":
        request["upload_id"] = _real_upload(client, "题目.txt", b"1+1=?", "text/plain")["id"]
    elif case == "image":
        request["upload_id"] = _image_upload(client)["id"]
    elif case == "pdf":
        request["document_id"] = _scanned_document(client)["id"]
    elif case == "audio":
        request["upload_id"] = _insert_upload(user["id"], "语音.mp3", "audio/mpeg")
    job_id = _insert_job(user["id"], task="tagging" if case == "none" else "explain",
                         status="completed", request=request)
    assert usage.record_job_usage(job_id) is True
    meta = json.loads(_usage_row(job_id)["meta_json"])
    assert meta["input_kind"] == expected


def test_input_classification_does_not_store_private_content(client):
    user = _user(client)
    upload = _real_upload(client, "秘密题目.txt", "私密题干内容".encode(), "text/plain")
    job_id = _insert_job(
        user["id"], status="completed",
        request={"provider_id": 1, "model": "test-model", "upload_id": upload["id"],
                 "requirements": "请按私密要求讲解"},
    )
    assert usage.record_job_usage(job_id) is True
    meta_json = _usage_row(job_id)["meta_json"]
    assert "秘密题目" not in meta_json
    assert "私密题干内容" not in meta_json
    assert "私密要求" not in meta_json
    assert upload["id"] is not None
    meta = json.loads(meta_json)
    assert meta["input_kind"] == "text"
    assert set(meta) <= {
        "input_kind", "models", "cross_verify", "structured_verify", "peer_review",
        "source_mode", "explain_mode", "modes_count", "document_count",
        "requirements_present", "retries", "prompt_version",
    }


# ---------------------------------------------------------------------------
# 3. 本人用量 / 管理台用量 / 权限
# ---------------------------------------------------------------------------

def test_my_usage_only_contains_own_rows(client, second_client):
    admin = _user(client)
    student = _user(second_client)
    admin_job = _insert_job(admin["id"], task="explain")
    student_job = _insert_job(student["id"], task="grade")
    usage.record_job_usage(admin_job)
    usage.record_job_usage(student_job)
    body = second_client.get("/api/ai/usage/me?days=7").json()
    assert body["recent"] and {item["job_id"] for item in body["recent"]} == {student_job}
    assert all(item["user_id"] == student["id"] for item in body["recent"])
    assert body["today"]["jobs"] == 1
    # 单条账本同时暴露入参分类与 prompt 版本，便于追溯模型/版本。
    assert body["recent"][0]["input_kind"] in usage.INPUT_KINDS
    assert "prompt_version" in body["recent"][0]


def test_my_usage_quota_remaining_and_consumption(client, monkeypatch):
    _capture_dispatch(monkeypatch)
    provider = save_provider(client)
    upload = _real_upload(client)
    user = _user(client)
    response = client.post("/api/ai/jobs", json={
        "task": "explain", "upload_id": upload["id"],
        "provider_id": provider["id"], "model": "test-model",
    })
    assert response.status_code == 200, response.text
    body = client.get("/api/ai/usage/me?days=1").json()
    entry = next(item for item in body["quota"]["kinds"] if item["kind"] == "explain")
    assert entry["used_today"] == 1
    assert entry["daily_limit"] == usage.DEFAULT_DAILY_QUOTAS["explain"]
    assert entry["remaining_today"] == entry["daily_limit"] - 1
    assert body["quota"]["timezone"] == "UTC"


@pytest.mark.parametrize("days", [0, 400])
def test_my_usage_days_out_of_range(client, days):
    assert client.get(f"/api/ai/usage/me?days={days}").status_code == 422


def test_my_usage_requires_login(client):
    anon = TestClient(app)
    assert anon.get("/api/ai/usage/me").status_code == 401


def test_admin_usage_requires_admin(client, second_client):
    assert second_client.get("/api/admin/ai/usage").status_code == 403
    assert second_client.get("/api/admin/ai/quotas").status_code == 403
    anon = TestClient(app)
    assert anon.get("/api/admin/ai/usage").status_code == 401


def test_admin_usage_aggregates_failure_rate_and_percentiles(client):
    user = _user(client)
    durations = [100, 200, 300, 400, 500]
    for index, duration in enumerate(durations):
        job_id = _insert_job(
            user["id"], task="explain" if index % 2 == 0 else "grade", status="completed",
            started_at="2026-11-21T10:00:00+00:00",
            finished_at=(datetime(2026, 11, 21, 10, 0, tzinfo=timezone.utc)
                         + timedelta(milliseconds=duration)).isoformat(),
        )
        usage.record_job_usage(job_id)
    for _ in range(2):
        usage.record_job_usage(_insert_job(user["id"], status="failed"))
    usage.record_job_usage(_insert_job(user["id"], status="cancelled"))
    body = client.get("/api/admin/ai/usage?days=7").json()
    totals = body["totals"]
    assert totals["jobs"] == 8
    assert totals["succeeded"] == 5
    assert totals["failed"] == 2
    assert totals["cancelled"] == 1
    assert totals["failure_rate"] == pytest.approx(0.25)
    # durations = [100,200,300,400,500] → P50=300，P95=480（线性插值）。
    assert totals["duration_p50_ms"] == 300
    assert totals["duration_p95_ms"] == 480
    kinds = {item["kind"]: item for item in body["by_kind"]}
    # 失败/取消任务默认也是 explain 类型：3 个完成 + 2 失败 + 1 取消 = 6。
    assert kinds["explain"]["jobs"] == 6 and kinds["explain"]["failed"] == 2
    assert kinds["grade"]["jobs"] == 2 and kinds["grade"]["failed"] == 0
    assert body["by_model"][0]["model"] == "test-model"


def test_admin_usage_filters_and_pagination(client, second_client):
    admin = _user(client)
    student = _user(second_client)
    for _ in range(25):
        usage.record_job_usage(_insert_job(admin["id"], task="explain"))
    for _ in range(12):
        usage.record_job_usage(_insert_job(admin["id"], task="grade"))
    usage.record_job_usage(_insert_job(student["id"], task="explain"))
    body = client.get("/api/admin/ai/usage?days=7&kind=grade&page=1").json()
    assert body["totals"]["jobs"] == 12
    assert all(item["kind"] == "grade" for item in body["page"]["items"])
    by_student = client.get(f"/api/admin/ai/usage?days=7&user_id={student['id']}").json()
    assert by_student["totals"]["jobs"] == 1
    assert by_student["page"]["items"][0]["user_id"] == student["id"]
    first = client.get("/api/admin/ai/usage?days=7&kind=explain&page=1").json()
    second = client.get("/api/admin/ai/usage?days=7&kind=explain&page=2").json()
    assert first["page"]["total"] == 26 and first["page"]["has_more"] is True
    assert len(first["page"]["items"]) == usage.ADMIN_PAGE_SIZE
    assert second["page"]["has_more"] is False and len(second["page"]["items"]) == 26 - usage.ADMIN_PAGE_SIZE


# ---------------------------------------------------------------------------
# 4. 配额与并发
# ---------------------------------------------------------------------------

def test_admin_quota_management_flow(client):
    response = client.get("/api/admin/ai/quotas")
    assert response.status_code == 200
    entry = next(item for item in response.json()["items"] if item["kind"] == "explain")
    assert entry["daily_limit"] == usage.DEFAULT_DAILY_QUOTAS["explain"]
    assert entry["daily_source"] == "default"

    saved = client.post("/api/admin/ai/quotas", json={
        "kind": "explain", "daily_limit": 2, "concurrency_limit": 1,
    })
    assert saved.status_code == 200, saved.text
    assert saved.json()["daily_limit"] == 2 and saved.json()["daily_source"] == "db"
    after = client.get("/api/admin/ai/quotas").json()
    entry = next(item for item in after["items"] if item["kind"] == "explain")
    assert entry["daily_limit"] == 2 and entry["concurrency_limit"] == 1
    assert entry["daily_source"] == "db" and entry["concurrency_source"] == "db"

    reset = client.post("/api/admin/ai/quotas", json={"kind": "explain", "reset": True})
    assert reset.status_code == 200
    assert reset.json()["daily_source"] == "default"
    assert reset.json()["daily_limit"] == usage.DEFAULT_DAILY_QUOTAS["explain"]


@pytest.mark.parametrize("payload", [
    {"kind": "bogus-task", "daily_limit": 1},
    {"kind": "explain"},
    {"kind": "explain", "daily_limit": -1},
])
def test_admin_quota_rejects_invalid_payload(client, payload):
    assert client.post("/api/admin/ai/quotas", json=payload).status_code == 422


def test_quota_rejection_returns_429_and_does_not_persist_job(client, monkeypatch):
    _capture_dispatch(monkeypatch)
    assert client.post("/api/admin/ai/quotas", json={
        "kind": "explain", "daily_limit": 1, "concurrency_limit": 5,
    }).status_code == 200
    provider = save_provider(client)
    upload = _real_upload(client)
    payload = {"task": "explain", "upload_id": upload["id"],
               "provider_id": provider["id"], "model": "test-model"}
    first = client.post("/api/ai/jobs", json=payload)
    assert first.status_code == 200, first.text
    count_before = _job_count()
    second = client.post("/api/ai/jobs", json=payload)
    assert second.status_code == 429
    detail = second.json()["detail"]
    assert detail["code"] == "ai_quota_exceeded"
    assert detail["kind"] == "explain"
    assert "配额" in detail["message"]
    assert _job_count() == count_before
    assert _usage_count() == 0


def test_quota_env_override_limits_kind(client, monkeypatch):
    monkeypatch.setenv("STUDY_AI_QUOTA_EXPLAIN", "1")
    _capture_dispatch(monkeypatch)
    provider = save_provider(client)
    upload = _real_upload(client)
    payload = {"task": "explain", "upload_id": upload["id"],
               "provider_id": provider["id"], "model": "test-model"}
    assert client.post("/api/ai/jobs", json=payload).status_code == 200
    second = client.post("/api/ai/jobs", json=payload)
    assert second.status_code == 429
    assert "配额" in second.json()["detail"]["message"]
    with db.connection() as con:
        limit, source = usage.effective_daily_quota("explain", con)
    assert (limit, source) == (1, "env")


def test_failed_jobs_do_not_consume_daily_quota(client, monkeypatch):
    _capture_dispatch(monkeypatch)
    user = _user(client)
    assert client.post("/api/admin/ai/quotas", json={
        "kind": "explain", "daily_limit": 1,
    }).status_code == 200
    for _ in range(3):
        _insert_job(user["id"], status="failed")
    provider = save_provider(client)
    upload = _real_upload(client)
    response = client.post("/api/ai/jobs", json={
        "task": "explain", "upload_id": upload["id"],
        "provider_id": provider["id"], "model": "test-model",
    })
    assert response.status_code == 200, response.text


def test_concurrency_limit_blocks_new_job(client, monkeypatch):
    _capture_dispatch(monkeypatch)
    user = _user(client)
    assert client.post("/api/admin/ai/quotas", json={
        "kind": "explain", "daily_limit": 50, "concurrency_limit": 1,
    }).status_code == 200
    _insert_job(user["id"], status="running")
    provider = save_provider(client)
    upload = _real_upload(client)
    response = client.post("/api/ai/jobs", json={
        "task": "explain", "upload_id": upload["id"],
        "provider_id": provider["id"], "model": "test-model",
    })
    assert response.status_code == 429
    assert "并发" in response.json()["detail"]["message"]


def test_concurrency_memory_fallback_when_database_count_fails(client, monkeypatch):
    usage.note_job_active(901, "explain")
    usage.note_job_active(902, "explain")
    usage.note_job_active(903, "grade")

    def broken(con, kind):
        raise RuntimeError("db unavailable")

    monkeypatch.setattr(usage, "_count_active_jobs", broken)
    assert usage.current_concurrency("explain") == 2
    assert usage.current_concurrency("grade") == 1


def test_unconfigured_kind_is_not_blocked(client, caplog):
    user = _user(client)
    assert usage.effective_daily_quota("custom-extension-task") == (None, "unconfigured")
    with db.connection() as con:
        usage.check_new_job(con, user_id=user["id"], kind="custom-extension-task")


def test_resume_clears_old_ledger_and_records_retry(client, monkeypatch):
    _capture_dispatch(monkeypatch)
    user = _user(client)
    job_id = _insert_job(
        user["id"], status="failed",
        request={"provider_id": 1, "model": "test-model"},
    )
    assert usage.record_job_usage(job_id) is True
    assert _usage_row(job_id)["status"] == "failed"
    response = client.post(f"/api/ai/jobs/{job_id}/resume")
    assert response.status_code == 200, response.text
    assert _usage_row(job_id) is None  # 旧终态账本已清掉，等待新终态重建
    with db.connection() as con:
        assert con.execute("SELECT retry_count FROM ai_jobs WHERE id=?", (job_id,)).fetchone()[0] == 1
    ai_jobs.update_job(job_id, "completed")
    row = _usage_row(job_id)
    assert row["status"] == "completed" and row["retries"] == 1


# ---------------------------------------------------------------------------
# 5. 保留策略与默认值
# ---------------------------------------------------------------------------

def test_purge_retention_boundary(client):
    user = _user(client)
    old_job = _insert_job(user["id"], status="completed")
    recent_job = _insert_job(user["id"], status="completed")
    usage.record_job_usage(old_job)
    usage.record_job_usage(recent_job)
    old_created = (datetime.now(timezone.utc) - timedelta(days=400)).isoformat()
    with db.connection() as con:
        con.execute("UPDATE ai_job_usage SET created_at=? WHERE job_id=?", (old_created, old_job))
    removed = usage.purge_old_usage(180)
    assert removed == 1
    assert _usage_row(old_job) is None and _usage_row(recent_job) is not None
    assert usage.purge_old_usage(180) == 0
    # 0 表示永久保留：旧行也不会被删除。
    with db.connection() as con:
        con.execute("UPDATE ai_job_usage SET created_at=? WHERE job_id=?", (old_created, recent_job))
    assert usage.purge_old_usage(0) == 0
    assert _usage_row(recent_job) is not None


def test_default_quotas_are_conservative_and_configured(monkeypatch):
    assert set(usage.DEFAULT_DAILY_QUOTAS) >= {
        "explain", "grade", "notes", "recognize", "ocr", "course",
    }
    assert all(0 < limit <= 200 for limit in usage.DEFAULT_DAILY_QUOTAS.values())
    assert all(0 < limit <= 8 for limit in usage.DEFAULT_CONCURRENCY.values())
    monkeypatch.setenv("STUDY_AI_CONCURRENCY_OCR", "1")
    with db.connection() as con:
        assert usage.effective_concurrency("ocr", con) == (1, "env")
