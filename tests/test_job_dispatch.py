import asyncio
from concurrent.futures import ThreadPoolExecutor
import os
import signal

import pytest

from app import ai
from app.ai import jobs
from deploy.worker import run_ai_job_task
from test_ai import save_provider


def submit_job(client):
    provider = save_provider(client)
    upload = client.post(
        "/api/uploads/question",
        files={"file": ("question.txt", "求导".encode(), "text/plain")},
    ).json()
    return client.post("/api/ai/jobs", json={
        "task": "explain", "upload_id": upload["id"],
        "provider_id": provider["id"], "model": "test-model",
    })


def test_queue_failure_cancel_and_resume_invalidate_old_dispatch(client, monkeypatch):
    monkeypatch.setenv("REDIS_URL", "redis://unused:6379/0")
    dispatches = []

    async def unavailable(rows):
        raise ConnectionError("test queue unavailable")

    monkeypatch.setattr(jobs, "_enqueue_jobs", unavailable)
    assert submit_job(client).status_code == 503
    job = client.get("/api/ai/jobs").json()["items"][0]
    assert job["status"] == "failed"
    job_id = job["id"]

    async def capture(rows):
        dispatches.extend(rows)

    monkeypatch.setattr(jobs, "_enqueue_jobs", capture)
    route = f"/api/ai/jobs/{job_id}"
    assert client.post(f"{route}/resume").json()["status"] == "queued"
    old_dispatch = dispatches[-1]
    ai.init_ai()
    assert client.get(route).json()["status"] == "queued"
    assert client.post(f"{route}/cancel").json()["status"] == "cancelled"
    assert client.post(f"{route}/resume").json()["status"] == "queued"
    new_dispatch = dispatches[-1]
    assert old_dispatch != new_dispatch

    runs = []

    async def complete(queued_id, user_id, data):
        runs.append((queued_id, user_id, data.task))
        jobs._job_update(queued_id, "completed", "completed", result={"content": "done"})

    monkeypatch.setattr(ai, "_run_job", complete)
    asyncio.run(run_ai_job_task({"job_try": 1}, *old_dispatch))
    assert not runs
    asyncio.run(run_ai_job_task({"job_try": 1}, *new_dispatch))
    asyncio.run(run_ai_job_task({"job_try": 2}, *new_dispatch))
    assert runs == [(job_id, 1, "explain")]
    assert client.get(route).json()["status"] == "completed"


@pytest.mark.skipif(not os.getenv("STUDY_TEST_REDIS_URL"), reason="requires a dedicated Redis test instance")
def test_real_arq_dispatch_and_sse(client, monkeypatch):
    from arq.worker import Worker

    # The arq test worker's close helper expects this POSIX signal.
    monkeypatch.setattr(signal, "SIGUSR1", getattr(signal, "SIGUSR1", signal.SIGTERM), raising=False)
    monkeypatch.setenv("REDIS_URL", os.environ["STUDY_TEST_REDIS_URL"])

    async def fake_model(*args, **kwargs):
        on_delta = kwargs.get("on_delta")
        if on_delta:
            on_delta("正在求导")
        await asyncio.sleep(1.1)
        return {"content": "导数为 1", "model": "test-model", "provider_name": "mock", "usage": {}}

    monkeypatch.setattr(ai, "_call_model", fake_model)
    response = submit_job(client)
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["status"] == "queued"
    assert "dispatch_token" not in payload
    route = f"/api/ai/jobs/{payload['id']}"

    async def consume():
        worker = Worker(
            [run_ai_job_task], redis_settings=jobs.redis_settings(), queue_name=jobs.QUEUE_NAME,
            burst=True, handle_signals=False,
        )
        try:
            await worker.async_run()
        finally:
            await worker.close()

    with ThreadPoolExecutor(max_workers=1) as pool:
        task = pool.submit(asyncio.run, consume())
        events = client.get(f"{route}/stream").text
        task.result(timeout=10)
    assert "event: snapshot" in events
    assert "event: delta" in events
    assert "正在求导" in events
    assert "event: done" in events
    result = client.get(route).json()
    assert result["status"] == "completed", result
    assert "导数为 1" in result["result"]["content"]
