"""试卷工坊 · AI 补全（paper-enrich）P0 验收：

任务准入（归属/上限/未知任务）、逐题补全写回 content_json、已核验题目跳过、
非法输出重试一次后隔离失败、MIXED 子题结构保持、取消保留已应用结果。
全部 mock AI（``app.ai._call_model``），不联网。
"""
import asyncio
import json

from fastapi.testclient import TestClient
import pytest

from app import ai, db
from app.ai import jobs as ai_jobs
from app.main import app
from app.paperlab import enrich

ENRICH_MODEL = "test-model"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def save_provider(client, name="AI 补全测试连接"):
    response = client.post("/api/providers", json={
        "name": name, "kind": "openai", "base_url": "https://api.openai.com/v1",
        "api_key": "sk-paperlab-enrich-unit-test",
        "models": [ENRICH_MODEL, "vision-model"],
    })
    assert response.status_code == 200, response.text
    return response.json()


def create_question(client, **overrides):
    payload = {
        "type": "SUBJECTIVE",
        "subject": "数学",
        "grade": "高中一年级",
        "difficulty": 0.5,
        "knowledge_tags": [],
        "content": {"stem": "证明函数 $f(x)=x^2$ 在 $[0,+\\infty)$ 上单调递增。"},
    }
    payload.update(overrides)
    response = client.post("/api/paperlab/questions", json=payload)
    assert response.status_code == 200, response.text
    return response.json()["question"]


def create_single(client, **overrides):
    content = {
        "stem": "已知 $f(x)=x^2$，则 $f(2)$ 等于？",
        "options": [{"label": "A", "text": "2"}, {"label": "B", "text": "4"}],
    }
    content.update(overrides.pop("content", {}))
    payload = {
        "type": "SINGLE", "subject": "数学", "grade": "高中一年级",
        "knowledge_tags": ["函数"], "content": content,
    }
    payload.update(overrides)
    return create_question(client, **payload)


def get_question(client, question_id):
    response = client.get(f"/api/paperlab/questions/{question_id}")
    assert response.status_code == 200, response.text
    return response.json()["question"]


def user_id(client):
    return client.get("/api/auth/me").json()["id"]


def insert_enrich_job(client, question_ids, provider_id=None):
    uid = user_id(client)
    provider_id = provider_id or save_provider(client)["id"]
    request = {
        "task": "paper-enrich", "provider_id": provider_id, "model": ENRICH_MODEL,
        "params": {"question_ids": list(question_ids)},
    }
    now = db.utcnow()
    with db.connection() as con:
        job_id = con.execute(
            "INSERT INTO ai_jobs(user_id,task,status,stage,request_json,partial_content,"
            "cancelled,created_at,updated_at,dispatch_token) VALUES (?,?,?,?,?,'',0,?,?,?)",
            (uid, "paper-enrich", "queued", "queued",
             json.dumps(request, ensure_ascii=False), now, now, "enrich-token"),
        ).lastrowid
    provider = ai._selected_provider(uid, provider_id, ENRICH_MODEL)
    return job_id, ai_jobs.JobInput(**request), provider


def run_enrich_job(job_id, uid, data, provider):
    return asyncio.run(enrich.run_enrich_job(job_id, uid, data, provider))


def job_status(job_id):
    with db.connection() as con:
        return con.execute("SELECT status FROM ai_jobs WHERE id=?", (job_id,)).fetchone()[0]


def enrich_payload(*, answers=("标准答案",), explanation="推导过程完整，结论正确。",
                   rubric="", tags=("函数",), parts=None):
    return {
        "answers": list(answers),
        "explanation": explanation,
        "rubric": rubric,
        "knowledge_tags": list(tags),
        "parts": list(parts or []),
    }


class FakeEnrich:
    def __init__(self, responses=None, on_call=None):
        self.responses = list(responses or [])
        self.on_call = on_call
        self.calls = []

    async def call(self, provider, model, prompt, images=None, json_output=False,
                   token_limit=5000, **kwargs):
        index = len(self.calls)
        self.calls.append({"prompt": prompt, "images": images})
        if self.on_call:
            self.on_call(index, prompt)
        if self.responses:
            item = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        else:
            item = enrich_payload()
        if isinstance(item, Exception):
            raise item
        if callable(item):
            item = item(prompt, images)
        content = item if isinstance(item, str) else json.dumps(item, ensure_ascii=False)
        return {"content": content, "model": model, "provider_name": "mock", "usage": {}}

    def install(self, monkeypatch):
        monkeypatch.setattr(ai, "_call_model", self.call)
        return self


async def no_dispatch(job_id, token, runner):
    return None


def patch_dispatch(monkeypatch):
    monkeypatch.setattr(ai_jobs, "_dispatch", no_dispatch)


# ---------------------------------------------------------------------------
# 1. 任务准入（HTTP）
# ---------------------------------------------------------------------------

def test_enrich_creates_paper_enrich_job(client, monkeypatch):
    patch_dispatch(monkeypatch)
    provider = save_provider(client)
    question = create_question(client)
    response = client.post("/api/paperlab/questions/enrich", json={
        "question_ids": [question["id"]], "provider_id": provider["id"], "model": ENRICH_MODEL,
    })
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["job"]["task"] == "paper-enrich"
    assert payload["job"]["status"] == "queued"
    assert payload["question_ids"] == [question["id"]]
    with db.connection() as con:
        row = con.execute(
            "SELECT request_json FROM ai_jobs WHERE id=?", (payload["job"]["id"],)
        ).fetchone()
    request = json.loads(row["request_json"])
    assert request["params"]["question_ids"] == [question["id"]]


def test_enrich_rejects_non_owned_question_and_missing_provider(client, second_client, monkeypatch):
    patch_dispatch(monkeypatch)
    provider = save_provider(client)
    other = create_question(second_client)
    foreign = client.post("/api/paperlab/questions/enrich", json={
        "question_ids": [other["id"]], "provider_id": provider["id"], "model": ENRICH_MODEL,
    })
    assert foreign.status_code == 404
    missing_provider = client.post("/api/paperlab/questions/enrich", json={
        "question_ids": [1], "model": ENRICH_MODEL,
    })
    assert missing_provider.status_code == 422
    bad_provider = client.post("/api/paperlab/questions/enrich", json={
        "question_ids": [1], "provider_id": 999, "model": ENRICH_MODEL,
    })
    assert bad_provider.status_code in {404, 422}


def test_enrich_rejects_empty_and_oversized_batches(client, monkeypatch):
    patch_dispatch(monkeypatch)
    provider = save_provider(client)
    question = create_question(client)
    empty = client.post("/api/paperlab/questions/enrich", json={
        "question_ids": [], "provider_id": provider["id"], "model": ENRICH_MODEL,
    })
    assert empty.status_code == 422
    too_many = client.post("/api/paperlab/questions/enrich", json={
        "question_ids": list(range(1, 52)), "provider_id": provider["id"],
        "model": ENRICH_MODEL,
    })
    assert too_many.status_code == 422


def test_enrich_unknown_task_without_question_ids_is_rejected(client, monkeypatch):
    patch_dispatch(monkeypatch)
    provider = save_provider(client)
    response = client.post("/api/ai/jobs", json={
        "task": "paper-enrich", "provider_id": provider["id"], "model": ENRICH_MODEL,
    })
    assert response.status_code == 422
    assert "补全" in response.json()["detail"]
    assert create_question(client)["id"] >= 1


# ---------------------------------------------------------------------------
# 2. 执行器：成功补全
# ---------------------------------------------------------------------------

def test_executor_applies_enrichment_and_marks_pending(client, monkeypatch):
    question = create_question(client)
    fake = FakeEnrich([enrich_payload(
        answers=["利用导数 $f'(x)=2x\\ge 0$ 可证。"],
        explanation="对 $f(x)=x^2$ 求导，在 $[0,+\\infty)$ 上 $f'(x)\\ge 0$。",
        rubric="求导正确得 3 分，结论正确得 2 分。",
        tags=["函数", "单调性"],
    )]).install(monkeypatch)
    job_id, data, provider = insert_enrich_job(client, [question["id"]])
    result = run_enrich_job(job_id, user_id(client), data, provider)

    assert result["enriched_count"] == 1
    assert result["failed"] == []
    assert result["done_ids"] == [question["id"]]
    updated = get_question(client, question["id"])
    assert updated["version"] == 2
    assert updated["verify_status"] == "pending"
    assert updated["ai_job_id"] == job_id
    assert updated["content"]["answers"] == ["利用导数 $f'(x)=2x\\ge 0$ 可证。"]
    assert updated["content"]["rubric"].startswith("求导正确")
    assert updated["content"]["ai"]["job_id"] == job_id
    assert updated["content"]["ai"]["model"] == ENRICH_MODEL
    assert updated["knowledge_tags"] == ["函数", "单调性"]


def test_executor_skips_verified_question(client, monkeypatch):
    verified = create_single(client, content={"answers": ["B"]})
    verified = client.post(
        f"/api/paperlab/questions/{verified['id']}/verify", json={"status": "verified"},
    ).json()["question"]
    assert verified["version"] == 2
    plain = create_question(client)
    fake = FakeEnrich([enrich_payload(answers=["新答案"], rubric="按点给分。")]).install(monkeypatch)
    job_id, data, provider = insert_enrich_job(client, [verified["id"], plain["id"]])
    result = run_enrich_job(job_id, user_id(client), data, provider)

    assert len(fake.calls) == 1, "已核验题目不应调用模型"
    assert result["skipped"] == [{"question_id": verified["id"], "reason": "已人工核验，AI 不覆盖"}]
    assert result["enriched_count"] == 1
    still_verified = get_question(client, verified["id"])
    assert still_verified["verify_status"] == "verified"
    assert still_verified["version"] == 2
    assert still_verified["content"]["answers"] == ["B"]


# ---------------------------------------------------------------------------
# 3. 执行器：非法输出重试 / 单题隔离
# ---------------------------------------------------------------------------

def test_executor_retries_invalid_json_once_then_fails_isolated(client, monkeypatch):
    question = create_question(client)
    fake = FakeEnrich(["not-a-json", "still-not-json"]).install(monkeypatch)
    job_id, data, provider = insert_enrich_job(client, [question["id"]])
    result = run_enrich_job(job_id, user_id(client), data, provider)

    assert len(fake.calls) == 2
    assert "格式修正" in fake.calls[1]["prompt"]
    assert result["enriched_count"] == 0
    assert result["failed"][0]["question_id"] == question["id"]
    unchanged = get_question(client, question["id"])
    assert unchanged["version"] == 1
    assert unchanged["content"]["answers"] == []


def test_executor_keeps_other_questions_when_one_fails(client, monkeypatch):
    bad = create_single(client)
    good = create_question(client)
    # 第 1 题：选择题答案不在选项 label（两次都非法）；第 2 题：正常补全
    invalid = enrich_payload(answers=["Z"], rubric="")
    fake = FakeEnrich([invalid, invalid, enrich_payload(
        answers=["单调递增"], rubric="按步骤给分。",
    )]).install(monkeypatch)
    job_id, data, provider = insert_enrich_job(client, [bad["id"], good["id"]])
    result = run_enrich_job(job_id, user_id(client), data, provider)

    assert result["enriched_count"] == 1
    assert result["done_ids"] == [good["id"]]
    assert result["failed"][0]["question_id"] == bad["id"]
    assert "选项" in result["failed"][0]["error"]
    assert get_question(client, bad["id"])["version"] == 1
    assert get_question(client, good["id"])["version"] == 2


def test_executor_requires_rubric_for_subjective(client, monkeypatch):
    question = create_question(client)
    fake = FakeEnrich([enrich_payload(rubric=""), enrich_payload(rubric="")]).install(monkeypatch)
    job_id, data, provider = insert_enrich_job(client, [question["id"]])
    result = run_enrich_job(job_id, user_id(client), data, provider)
    assert result["enriched_count"] == 0
    assert "评分标准" in result["failed"][0]["error"]


# ---------------------------------------------------------------------------
# 4. MIXED 子题结构保持
# ---------------------------------------------------------------------------

def test_executor_preserves_mixed_parts_structure(client, monkeypatch):
    mixed = create_question(client, type="MIXED", knowledge_tags=["函数"], content={
        "stem": "阅读题目，完成下列各小题。",
        "parts": [
            {
                "index": 1, "type": "SINGLE", "stem": "(1) 求 $f(1)$。",
                "options": [{"label": "A", "text": "0"}, {"label": "B", "text": "1"}],
                "answers": ["A"],
            },
            {
                "index": 2, "type": "SUBJECTIVE", "stem": "(2) 说明理由。",
                "answers": ["原答案"],
                "rubric": "原评分标准",
            },
        ],
    })
    fake = FakeEnrich([{
        "answers": [],
        "explanation": "整题解析：先求值再说明理由。",
        "rubric": "",
        "knowledge_tags": ["分段函数"],
        "parts": [
            {"index": 1, "answers": ["B"], "explanation": "子题一解析。", "rubric": ""},
            {"index": 2, "answers": ["因为 $f$ 单调。"], "explanation": "子题二解析。",
             "rubric": "理由正确得 3 分。"},
        ],
    }]).install(monkeypatch)
    job_id, data, provider = insert_enrich_job(client, [mixed["id"]])
    result = run_enrich_job(job_id, user_id(client), data, provider)
    assert result["enriched_count"] == 1, result

    updated = get_question(client, mixed["id"])
    content = updated["content"]
    assert content["stem"] == "阅读题目，完成下列各小题。"
    assert len(content["parts"]) == 2
    first, second = content["parts"]
    # 结构与题型保持
    assert first["type"] == "SINGLE"
    assert first["stem"] == "(1) 求 $f(1)$。"
    assert first["options"] == [{"label": "A", "text": "0"}, {"label": "B", "text": "1"}]
    assert first["answers"] == ["B"]
    assert first["explanation"] == "子题一解析。"
    assert second["type"] == "SUBJECTIVE"
    assert second["answers"] == ["因为 $f$ 单调。"]
    assert second["rubric"] == "理由正确得 3 分。"
    assert content["explanation"].startswith("整题解析")
    assert "分段函数" in updated["knowledge_tags"]


# ---------------------------------------------------------------------------
# 5. 取消
# ---------------------------------------------------------------------------

def test_executor_cancel_keeps_applied_questions(client, monkeypatch):
    first = create_question(client, content={"stem": "第一道主观题。"})
    second = create_question(client, content={"stem": "第二道主观题。"})
    job_id, data, provider = insert_enrich_job(client, [first["id"], second["id"]])

    def cancel_after_first(index, prompt):
        if index == 0:
            with db.connection() as con:
                con.execute("UPDATE ai_jobs SET cancelled=1 WHERE id=?", (job_id,))

    fake = FakeEnrich([enrich_payload(rubric="按点给分。")], on_call=cancel_after_first).install(monkeypatch)
    result = run_enrich_job(job_id, user_id(client), data, provider)

    assert result["cancelled"] is True
    assert result["enriched_count"] == 1
    assert job_status(job_id) == "cancelled"
    assert get_question(client, first["id"])["version"] == 2
    assert get_question(client, second["id"])["version"] == 1
    assert len(fake.calls) == 1


# ---------------------------------------------------------------------------
# 6. 登录
# ---------------------------------------------------------------------------

def test_enrich_requires_login(client):
    anonymous = TestClient(app)
    try:
        assert anonymous.post("/api/paperlab/questions/enrich", json={
            "question_ids": [1], "provider_id": 1, "model": ENRICH_MODEL,
        }).status_code == 401
    finally:
        anonymous.close()
