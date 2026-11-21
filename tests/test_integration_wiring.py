"""整合工程中央接线回归：扩展任务注册表、JobInput.params、未知任务拒绝。

这些是 OpenMAIC / Oh My Paper 等功能模块接入知序任务体系的公共通道，
任何一处回归都会让全部扩展任务（course / paper-import / paper-enrich / insight-report /
assistant-turn …）失效，因此单独托管在集成层测试里。
"""
import time

import pytest
from fastapi import HTTPException

from app.ai import jobs
from app.ai.jobs import EXTRA_TASKS, JobInput, register_task_handler
from test_ai import save_provider


def _wait_job(client, job_id: int, timeout: float = 8.0) -> dict:
    deadline = time.time() + timeout
    body = None
    while time.time() < deadline:
        body = client.get(f"/api/ai/jobs/{job_id}").json()
        if body["status"] in ("completed", "failed", "interrupted", "cancelled"):
            return body
        time.sleep(0.05)
    raise AssertionError(f"job did not settle: {body}")


def test_job_input_accepts_extension_task_and_params():
    async def handler(*_args):
        return {}

    register_task_handler("demo-shape", handler)
    try:
        data = JobInput(task="demo-shape", provider_id=1, model="m", params={"course_id": 3, "stage": "outline"})
        assert data.task == "demo-shape"
        assert data.params == {"course_id": 3, "stage": "outline"}
    finally:
        EXTRA_TASKS.pop("demo-shape", None)
    # 任务名形态仍受约束，避免把任意字符串塞进任务表
    with pytest.raises(Exception):
        JobInput(task="Bad Task", provider_id=1, model="m")
    with pytest.raises(Exception):
        JobInput(task="UPPER", provider_id=1, model="m")
    # 未注册的任务在解析阶段就被拒绝（与旧基线 test_job_ocr_dispatch 的 bogus 断言一致）
    with pytest.raises(Exception):
        JobInput(task="bogus", provider_id=1, model="m")


def test_register_task_handler_rejects_builtin_and_bad_handler():
    with pytest.raises(ValueError):
        register_task_handler("explain", lambda *a: None)
    with pytest.raises(ValueError):
        register_task_handler("demo-bad", None)


def test_extension_task_runs_end_to_end(client):
    seen = {}

    async def handler(job_id, user_id, data, provider, verifier):
        seen.update({"job_id": job_id, "user_id": user_id, "params": data.params})
        jobs.update_job(job_id, "running", "演示阶段")
        return {"summary": "扩展任务完成", "echo": data.params}

    def validate(data, user):
        if not (data.params or {}).get("ok"):
            raise HTTPException(422, "演示任务缺少 ok 参数")

    register_task_handler("demo-echo", handler, validate)
    try:
        provider = save_provider(client)
        # 校验器拒绝：明确 422 中文错误
        rejected = client.post("/api/ai/jobs", json={
            "task": "demo-echo", "provider_id": provider["id"], "model": "test-model", "params": {"ok": False},
        })
        assert rejected.status_code == 422
        assert "ok" in rejected.json()["detail"]

        created = client.post("/api/ai/jobs", json={
            "task": "demo-echo", "provider_id": provider["id"], "model": "test-model",
            "params": {"ok": True, "course_id": 7},
        })
        assert created.status_code == 200, created.text
        job = _wait_job(client, created.json()["id"])
        assert job["status"] == "completed"
        assert job["result"]["summary"] == "扩展任务完成"
        assert job["result"]["echo"] == {"ok": True, "course_id": 7}
        assert seen["params"] == {"ok": True, "course_id": 7}
        # 扩展任务结果同样带 prompt_version / models / input_ref
        assert job["result"]["prompt_version"]
        assert job["result"]["models"][0]["model"] == "test-model"
        assert job["result"]["input_ref"]["task"] == "demo-echo"
    finally:
        EXTRA_TASKS.pop("demo-echo", None)


def test_unknown_task_is_rejected(client):
    provider = save_provider(client)
    response = client.post("/api/ai/jobs", json={
        "task": "no-such-task", "provider_id": provider["id"], "model": "test-model",
    })
    assert response.status_code == 422
    assert "未知任务" in response.text


def test_oversized_params_rejected(client):
    provider = save_provider(client)
    response = client.post("/api/ai/jobs", json={
        "task": "explain", "provider_id": provider["id"], "model": "test-model",
        "params": {"blob": "x" * 20000},
    })
    assert response.status_code == 422
    assert "参数过大" in response.json()["detail"]
