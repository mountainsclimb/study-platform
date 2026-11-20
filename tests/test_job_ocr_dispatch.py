"""P1 OCR 任务派发契约：JobInput 扩展、submit_job 抽取后的准入回归、_ocr_job 分支。"""
import asyncio
from io import BytesIO
import json
import sys
import types

from fastapi import HTTPException
import pytest

from app import db, practice
from app.ai import jobs
from test_ai import save_provider


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _user(client):
    return client.get("/api/auth/me").json()


def _upload(client, name="题目.txt", content=b"1+1=?", media="text/plain"):
    response = client.post("/api/uploads/question", files={"file": (name, content, media)})
    assert response.status_code == 200, response.text
    return response.json()


def _scanned_pdf():
    from pypdf import PdfWriter
    writer = PdfWriter()
    writer.add_blank_page(width=600, height=800)
    raw = BytesIO()
    writer.write(raw)
    return raw.getvalue()


def _scanned_document(client):
    response = client.post(
        "/api/documents", files={"file": ("扫描.pdf", _scanned_pdf(), "application/pdf")}
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "needs_ocr"
    return response.json()


def _text_document(client):
    response = client.post(
        "/api/documents", files={"file": ("资料.txt", "定义和公式".encode(), "text/plain")}
    )
    assert response.status_code == 200, response.text
    return response.json()


def _capture_dispatch(monkeypatch):
    dispatched = []

    async def fake_dispatch(job_id, token, runner):
        dispatched.append((job_id, token))

    monkeypatch.setattr(jobs, "_dispatch", fake_dispatch)
    return dispatched


def _insert_job(user_id, *, status="running", task="explain", request=None, token="job-token"):
    with db.connection() as con:
        now = db.utcnow()
        return con.execute(
            "INSERT INTO ai_jobs(user_id,task,status,stage,request_json,partial_content,cancelled,"
            "created_at,updated_at,dispatch_token) VALUES (?,?,?,?,?,'',0,?,?,?)",
            (
                user_id, task, status, status,
                json.dumps(request or {"task": task, "provider_id": 1, "model": "m"}),
                now, now, token,
            ),
        ).lastrowid


def _ocr_job_input(provider_id, document_id, **changes):
    payload = {
        "task": "ocr", "provider_id": provider_id, "model": "test-model",
        "document_id": document_id, "page_from": 1, "page_to": 1,
    }
    payload.update(changes)
    return jobs.JobInput(**payload)


@pytest.fixture(autouse=True)
def _clean_stream_state():
    jobs._stream_buffers.clear()
    jobs._stream_queues.clear()
    yield
    jobs._stream_buffers.clear()
    jobs._stream_queues.clear()


# ---------------------------------------------------------------------------
# 1. JobInput：ocr 任务与页码字段
# ---------------------------------------------------------------------------

def test_job_input_accepts_ocr_task_and_page_range():
    data = jobs.JobInput(
        task="ocr", provider_id=1, model="m", document_id=7, page_from=2, page_to=9,
    )
    assert (data.task, data.document_id, data.page_from, data.page_to) == ("ocr", 7, 2, 9)
    assert data.model_dump()["page_from"] == 2 and data.model_dump()["page_to"] == 9
    default = jobs.JobInput(task="ocr", provider_id=1, model="m", document_id=7)
    assert default.page_from == 1
    assert default.page_to is None


@pytest.mark.parametrize("changes", [
    {"page_from": 0},
    {"page_to": 0},
    {"task": "bogus"},
])
def test_job_input_rejects_invalid_ocr_fields(changes):
    with pytest.raises(ValueError):
        jobs.JobInput(**{"task": "ocr", "provider_id": 1, "model": "m", "document_id": 7, **changes})


# ---------------------------------------------------------------------------
# 2. submit_job：配额 / 24h / 权限 / 去重规则回归
# ---------------------------------------------------------------------------

def test_submit_job_enforces_per_user_quota_from_env(client, monkeypatch):
    monkeypatch.setenv("STUDY_AI_MAX_ACTIVE_PER_USER", "1")
    dispatched = _capture_dispatch(monkeypatch)
    provider = save_provider(client)
    upload = _upload(client)
    user = _user(client)
    data = jobs.JobInput(
        task="explain", upload_id=upload["id"], provider_id=provider["id"], model="test-model",
    )

    async def scenario():
        first = await jobs.submit_job(user, data)
        with pytest.raises(HTTPException) as exc:
            await jobs.submit_job(user, data)
        return first, exc.value

    job, exc = asyncio.run(scenario())
    assert job["status"] == "queued"
    assert "dispatch_token" not in job
    assert job["request"]["task"] == "explain"
    assert exc.status_code == 429
    assert "1 个任务" in exc.detail
    assert len(dispatched) == 1  # 被配额拒绝的任务不会派发


def test_cancel_requested_jobs_still_count_toward_quota(client, monkeypatch):
    monkeypatch.setenv("STUDY_AI_MAX_ACTIVE_PER_USER", "1")
    _capture_dispatch(monkeypatch)
    user = _user(client)
    _insert_job(user["id"], status="cancel_requested", token="cancel-token")
    provider = save_provider(client)
    upload = _upload(client)

    async def scenario():
        with pytest.raises(HTTPException) as exc:
            await jobs.submit_job(
                user,
                jobs.JobInput(task="explain", upload_id=upload["id"], provider_id=provider["id"], model="test-model"),
            )
        return exc.value

    assert asyncio.run(scenario()).status_code == 429


def test_submit_job_enforces_global_concurrency_from_env(client, second_client, monkeypatch):
    monkeypatch.setenv("STUDY_AI_GLOBAL_CONCURRENCY", "1")
    _capture_dispatch(monkeypatch)
    provider_a = save_provider(client)
    upload_a = _upload(client)
    asyncio.run(jobs.submit_job(
        _user(client),
        jobs.JobInput(task="explain", upload_id=upload_a["id"], provider_id=provider_a["id"], model="test-model"),
    ))
    provider_b = save_provider(second_client)
    upload_b = _upload(second_client)

    async def scenario():
        with pytest.raises(HTTPException) as exc:
            await jobs.submit_job(
                _user(second_client),
                jobs.JobInput(task="explain", upload_id=upload_b["id"], provider_id=provider_b["id"], model="test-model"),
            )
        return exc.value

    exc = asyncio.run(scenario())
    assert exc.status_code == 429
    assert "同时运行" in exc.detail


def test_submit_job_enforces_daily_limit(client, monkeypatch):
    _capture_dispatch(monkeypatch)
    user = _user(client)
    provider = save_provider(client)
    upload = _upload(client)
    now = db.utcnow()
    with db.connection() as con:
        con.executemany(
            "INSERT INTO ai_jobs(user_id,task,status,stage,request_json,partial_content,cancelled,"
            "created_at,updated_at,dispatch_token) VALUES (?,?,?,?,?,'',0,?,?,NULL)",
            [(user["id"], "explain", "completed", "completed", "{}", now, now)] * jobs.DAILY_JOB_LIMIT,
        )

    async def scenario():
        with pytest.raises(HTTPException) as exc:
            await jobs.submit_job(
                user,
                jobs.JobInput(task="explain", upload_id=upload["id"], provider_id=provider["id"], model="test-model"),
            )
        return exc.value

    exc = asyncio.run(scenario())
    assert exc.status_code == 429
    assert "24 小时" in exc.detail


def test_submit_job_keeps_grade_dedupe(client, monkeypatch):
    provider = save_provider(client)
    _capture_dispatch(monkeypatch)
    monkeypatch.setattr(practice, "ensure_unlocked", lambda *a: None)
    monkeypatch.setattr(practice, "load_question", lambda *a: {})
    monkeypatch.setattr(
        practice, "latest_attempt", lambda *a: {"id": 7, "result": "pending", "grade_source": "reference"},
    )
    payload = {
        "task": "grade", "qid": "qid", "attempt_id": 7,
        "provider_id": provider["id"], "model": "test-model",
        "cross_verify": True, "verify_provider_id": provider["id"], "verify_model": "verify-model",
    }
    first = client.post("/api/ai/jobs", json=payload)
    second = client.post("/api/ai/jobs", json=payload)
    assert first.status_code == 200, first.text
    assert first.json()["status"] == "queued"
    assert second.status_code == 409
    assert "进行中" in second.json()["detail"]


def test_submit_job_keeps_permission_and_ownership_checks(client, second_client, monkeypatch):
    _capture_dispatch(monkeypatch)
    provider = save_provider(client)
    document = _text_document(client)
    student = _user(second_client)
    student_provider = save_provider(second_client)

    async def scenario():
        with pytest.raises(HTTPException) as tagging_exc:
            await jobs.submit_job(
                student,
                jobs.JobInput(task="tagging", subject="数学", provider_id=student_provider["id"], model="test-model"),
            )
        with pytest.raises(HTTPException) as ocr_exc:
            await jobs.submit_job(
                student,
                jobs.JobInput(task="ocr", document_id=document["id"], provider_id=student_provider["id"], model="test-model"),
            )
        return tagging_exc.value, ocr_exc.value

    tagging, ocr = asyncio.run(scenario())
    assert tagging.status_code == 403
    assert "管理员" in tagging.detail
    assert ocr.status_code == 404  # 他人资料不可 OCR


# ---------------------------------------------------------------------------
# 3. OCR 派发：参数校验 + 任务落库 + _run_job 分支
# ---------------------------------------------------------------------------

def test_submit_job_dispatches_ocr_with_page_range(client, monkeypatch):
    dispatched = _capture_dispatch(monkeypatch)
    provider = save_provider(client)
    document = _scanned_document(client)
    user = _user(client)

    async def scenario():
        return await jobs.submit_job(user, _ocr_job_input(provider["id"], document["id"], page_from=1, page_to=1))

    job = asyncio.run(scenario())
    assert job["task"] == "ocr"
    assert job["status"] == "queued"
    assert job["request"]["task"] == "ocr"
    assert job["request"]["document_id"] == document["id"]
    assert job["request"]["page_from"] == 1
    assert job["request"]["page_to"] == 1
    assert "dispatch_token" not in job
    assert [item[0] for item in dispatched] == [job["id"]]


def test_submit_job_rejects_invalid_ocr_requests(client, monkeypatch):
    _capture_dispatch(monkeypatch)
    provider = save_provider(client)
    scanned = _scanned_document(client)
    text_doc = _text_document(client)
    user = _user(client)

    async def scenario():
        errors = {}
        for name, payload in {
            "missing_document": jobs.JobInput(task="ocr", provider_id=provider["id"], model="test-model"),
            "not_pdf": _ocr_job_input(provider["id"], text_doc["id"]),
            "page_from_beyond": _ocr_job_input(provider["id"], scanned["id"], page_from=5, page_to=5),
            "page_to_before_from": _ocr_job_input(provider["id"], scanned["id"], page_from=3, page_to=2),
        }.items():
            with pytest.raises(HTTPException) as exc:
                await jobs.submit_job(user, payload)
            errors[name] = exc.value.status_code
        return errors

    errors = asyncio.run(scenario())
    assert errors == {
        "missing_document": 422,
        "not_pdf": 422,
        "page_from_beyond": 422,
        "page_to_before_from": 422,
    }


def _fake_notes_module(ocr_job):
    module = types.ModuleType("app.ai.notes_agent")
    if ocr_job is not None:
        module._ocr_job = ocr_job
    return module


def test_run_job_dispatches_to_ocr_executor(client, monkeypatch):
    provider = save_provider(client)
    user = _user(client)
    calls = []

    async def fake_ocr_job(job_id, user_id, data, provider_row):
        calls.append((job_id, user_id, data.task, data.page_from, data.page_to, provider_row["id"]))
        jobs.publish_event(job_id, "stage", {"status": "running", "stage": "ocr-1/2"})
        return {"document_id": data.document_id, "ocr_pages": [{"page": 1, "text": "识别结果"}]}

    monkeypatch.setitem(sys.modules, "app.ai.notes_agent", _fake_notes_module(fake_ocr_job))
    data = _ocr_job_input(provider["id"], 5, page_from=1, page_to=2)
    job_id = _insert_job(user["id"], status="queued", task="ocr", request=data.model_dump(), token="ocr-token")

    asyncio.run(jobs._run_job(job_id, user["id"], data))

    assert calls == [(job_id, user["id"], "ocr", 1, 2, provider["id"])]
    job = client.get(f"/api/ai/jobs/{job_id}").json()
    assert job["status"] == "completed"
    assert job["result"]["ocr_pages"] == [{"page": 1, "text": "识别结果"}]
    assert job["result"]["prompt_version"]
    assert job["result"]["models"] == [{"provider_id": provider["id"], "model": "test-model"}]
    assert job["finished_at"]


def test_run_job_reports_clear_error_when_ocr_executor_missing(client, monkeypatch):
    provider = save_provider(client)
    user = _user(client)
    monkeypatch.setitem(sys.modules, "app.ai.notes_agent", _fake_notes_module(None))
    data = _ocr_job_input(provider["id"], 5)
    job_id = _insert_job(user["id"], status="queued", task="ocr", request=data.model_dump(), token="ocr-token")

    asyncio.run(jobs._run_job(job_id, user["id"], data))

    job = client.get(f"/api/ai/jobs/{job_id}").json()
    assert job["status"] == "failed"
    assert job["error"] == "OCR 任务执行器尚未就绪"
    assert job["finished_at"]


# ---------------------------------------------------------------------------
# 4. 供 notes_agent OCR 执行器使用的公共包装
# ---------------------------------------------------------------------------

def test_update_job_writes_stage_and_finishes_cancelled(client):
    user = _user(client)
    job_id = _insert_job(user["id"], status="running")

    jobs.update_job(job_id, "running", "ocr-2/5")
    with db.connection() as con:
        row = con.execute("SELECT status, stage, finished_at FROM ai_jobs WHERE id=?", (job_id,)).fetchone()
    assert (row["status"], row["stage"], row["finished_at"]) == ("running", "ocr-2/5", None)

    # stage 为空时回退用 status，终态写 finished_at
    jobs.update_job(job_id, "cancelled")
    with db.connection() as con:
        row = con.execute(
            "SELECT status, stage, cancelled, finished_at FROM ai_jobs WHERE id=?", (job_id,)
        ).fetchone()
    assert row["status"] == "cancelled"
    assert row["stage"] == "cancelled"
    assert row["finished_at"]


def test_ocr_executor_contract_publish_event_and_is_job_cancelled(client, monkeypatch):
    monkeypatch.delenv("REDIS_URL", raising=False)
    user = _user(client)
    job_id = _insert_job(user["id"], status="running")
    queue = asyncio.Queue(maxsize=256)
    jobs._stream_queues[job_id] = [queue]

    jobs.update_job(job_id, "running", "ocr-1/2")
    jobs.publish_event(job_id, "delta", {"text": "第 1 页文本"})
    first = queue.get_nowait()
    second = queue.get_nowait()
    assert first["event"] == "stage" and first["data"]["stage"] == "ocr-1/2"
    assert second == {"event": "delta", "data": {"text": "第 1 页文本"}}
    assert jobs.is_job_cancelled(job_id) is False

    with db.connection() as con:
        con.execute("UPDATE ai_jobs SET cancelled=1, status='cancel_requested' WHERE id=?", (job_id,))
    assert jobs.is_job_cancelled(job_id) is True


def test_submit_job_is_the_route_implementation(client, monkeypatch):
    """端点改为调用 submit_job，HTTP 行为不变（返回公开任务对象、无 dispatch_token）。"""
    _capture_dispatch(monkeypatch)
    provider = save_provider(client)
    upload = _upload(client)
    response = client.post("/api/ai/jobs", json={
        "task": "explain", "upload_id": upload["id"],
        "provider_id": provider["id"], "model": "test-model",
    })
    assert response.status_code == 200, response.text
    job = response.json()
    assert job["status"] == "queued"
    assert "dispatch_token" not in job
    assert job["request"]["upload_id"] == upload["id"]
