"""P1 任务状态与幂等：取消请求、需处理、产物按 job_id 只写一份。"""
import json

from app import db, practice
from app.ai import jobs as ai_jobs
from app.ai.verify import _save_explanation


def _bank_question():
    with practice._bank() as con:
        ids = [r[0] for r in con.execute("SELECT qid FROM questions ORDER BY year DESC LIMIT 50")]
    for qid in ids:
        question = practice.load_question(qid)
        if question["has_answer"] and not question["quarantined"]:
            return question
    raise AssertionError("No suitable source question")


def _insert_job(user_id: int, status: str, *, checkpoint: str | None = None) -> int:
    now = db.utcnow()
    with db.connection() as con:
        return con.execute(
            "INSERT INTO ai_jobs(user_id,task,status,stage,request_json,partial_content,cancelled,"
            "created_at,updated_at,dispatch_token,checkpoint_json) VALUES (?,?,?,?,?,'',0,?,?,?,?)",
            (
                user_id, "explain", status, status,
                json.dumps({"task": "explain", "provider_id": 1, "model": "m"}),
                now, now, "test-token", checkpoint,
            ),
        ).lastrowid


class _FakeTask:
    """模拟本进程内正在运行的任务，用来观察取消请求状态。"""

    def done(self):
        return False

    def cancel(self):
        return None


def test_cancel_queued_job_settles_immediately(client):
    user_id = client.get("/api/auth/me").json()["id"]
    job_id = _insert_job(user_id, "queued")
    response = client.post(f"/api/ai/jobs/{job_id}/cancel")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "cancelled"
    assert body["finished_at"]


def test_cancel_awaiting_outline_job_settles_immediately(client):
    """等待人工确认提纲的任务没有执行进程在跑，取消必须直接落定。

    回归：曾把 awaiting_outline 当终态返回 200 却不改状态，任务永久占坑且无法释放。
    """
    user_id = client.get("/api/auth/me").json()["id"]
    job_id = _insert_job(user_id, "awaiting_outline")
    response = client.post(f"/api/ai/jobs/{job_id}/cancel")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "cancelled"
    assert body["cancelled"] == 1
    assert body["finished_at"]


def test_cancel_running_job_goes_through_cancel_request(client):
    user_id = client.get("/api/auth/me").json()["id"]
    job_id = _insert_job(user_id, "running")
    ai_jobs._running_by_id[job_id] = _FakeTask()
    try:
        response = client.post(f"/api/ai/jobs/{job_id}/cancel")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "cancel_requested"
        assert body["cancelled"] == 1
        assert body["cancel_requested_at"]
        assert body["finished_at"] is None
    finally:
        ai_jobs._running_by_id.pop(job_id, None)
    # 执行进程确认后落定为已取消
    assert ai_jobs._finalize_cancel(job_id) is True
    settled = client.get(f"/api/ai/jobs/{job_id}").json()
    assert settled["status"] == "cancelled"
    assert settled["finished_at"]
    # 已完成任务再次取消不改变状态
    assert client.post("/api/ai/jobs/{}/cancel".format(job_id)).json()["status"] == "cancelled"


def test_failed_job_with_checkpoint_needs_attention(client):
    user_id = client.get("/api/auth/me").json()["id"]
    job_id = _insert_job(user_id, "failed", checkpoint=json.dumps({"stage": "outline"}))
    listing = client.get("/api/ai/jobs").json()["items"]
    item = next(x for x in listing if x["id"] == job_id)
    assert item["has_checkpoint"] is True
    assert item["needs_attention"] is True
    # 没有检查点的失败任务只显示未完成，不标需处理
    plain_id = _insert_job(user_id, "failed")
    plain = next(x for x in client.get("/api/ai/jobs").json()["items"] if x["id"] == plain_id)
    assert plain["needs_attention"] is False


def test_explanation_product_is_idempotent_per_job(client):
    user_id = client.get("/api/auth/me").json()["id"]
    question = _bank_question()
    first = _save_explanation(
        user_id, question["qid"], None, "detailed",
        {"content": "第一版讲解", "verification": {"status": "agreed"}, "calls": [{"model": "m"}]},
        job_id=9001,
    )
    second = _save_explanation(
        user_id, question["qid"], None, "detailed",
        {"content": "重试后的讲解", "verification": {"status": "agreed"}, "calls": [{"model": "m"}]},
        job_id=9001,
    )
    assert first and second == first
    with db.connection() as con:
        rows = con.execute(
            "SELECT content_json FROM ai_explanations WHERE job_id=?", (9001,)
        ).fetchall()
    assert len(rows) == 1
    assert json.loads(rows[0]["content_json"])["content"] == "重试后的讲解"
    # 不同任务产生独立产物
    third = _save_explanation(user_id, question["qid"], None, "detailed", {"content": "另一任务"}, job_id=9002)
    assert third and third != first


def test_job_input_reference_records_bank_context(client):
    question = _bank_question()
    data = ai_jobs.JobInput(
        task="explain", qid=question["qid"], provider_id=1, model="m",
    )
    reference = ai_jobs._input_reference(data, "prompt-test")
    assert reference["qid"] == question["qid"]
    assert reference["prompt_version"] == "prompt-test"
    assert reference["task"] == "explain"
    assert "bank_version" in reference and "practice_status" in reference
