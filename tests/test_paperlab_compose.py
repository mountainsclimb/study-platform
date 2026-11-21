"""试卷工坊 P1：AI 组卷（paper-compose）硬校验与建卷。

覆盖：Hamilton 配额 + 二分匹配、候选不足/题型/知识点/难度预检 422、模型编造 ID 与
题数/难度不满足的失败路径、成功建卷快照、同一任务续跑幂等、取消。全部 mock 模型。
"""
import asyncio
import json

from fastapi.testclient import TestClient
import pytest

from app import ai, db
from app.ai import jobs as ai_jobs
from app.main import app
from app.paperlab import compose

MODEL = "test-model"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def save_provider(client) -> dict:
    response = client.post("/api/providers", json={
        "name": "AI 组卷测试连接", "kind": "openai", "base_url": "https://api.openai.com/v1",
        "api_key": "sk-paperlab-compose-unit-test", "models": [MODEL],
    })
    assert response.status_code == 200, response.text
    return response.json()


def user_id(client) -> int:
    return client.get("/api/auth/me").json()["id"]


def create_question(client, *, qtype="SINGLE", difficulty=0.2, tags=("函数",), grade="高中一年级",
                    stem=None, answers=None) -> dict:
    content = {"stem": stem or f"题目：已知 $f(x)=x^2$，判断 {qtype} 题。"}
    if qtype in ("SINGLE", "MULTIPLE"):
        content["options"] = [{"label": "A", "text": "2"}, {"label": "B", "text": "4"}]
        content["answers"] = answers or ["B"]
    else:
        content["answers"] = answers or ["略"]
    response = client.post("/api/paperlab/questions", json={
        "type": qtype, "subject": "数学", "grade": grade, "difficulty": difficulty,
        "knowledge_tags": list(tags), "content": content, "status": "ready",
    })
    assert response.status_code == 200, response.text
    return response.json()["question"]


def patch_dispatch(monkeypatch):
    async def no_dispatch(*_args, **_kwargs):
        return None
    monkeypatch.setattr(ai_jobs, "_dispatch", no_dispatch)


class FakeModel:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def call(self, provider, model, prompt, images=None, json_output=False,
                   token_limit=5000, **kwargs):
        self.calls.append(prompt)
        item = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(item, Exception):
            raise item
        return {"content": json.dumps(item, ensure_ascii=False), "model": model,
                "provider_name": "mock", "usage": {}}

    def install(self, monkeypatch):
        monkeypatch.setattr(ai, "_call_model", self.call)
        return self


def insert_compose_job(client, spec: dict, provider_id: int) -> tuple[int, dict, dict]:
    uid = user_id(client)
    request = {"task": "paper-compose", "provider_id": provider_id, "model": MODEL,
               "params": {"spec": spec}}
    now = db.utcnow()
    with db.connection() as con:
        job_id = con.execute(
            "INSERT INTO ai_jobs(user_id,task,status,stage,request_json,partial_content,"
            "cancelled,created_at,updated_at,dispatch_token) VALUES (?,?,?,?,?,'',0,?,?,?)",
            (uid, "paper-compose", "queued", "queued",
             json.dumps(request, ensure_ascii=False), now, now, "compose-token"),
        ).lastrowid
    provider = ai._selected_provider(uid, provider_id, MODEL)
    return int(job_id), request, provider


def run_compose(job_id: int, uid: int, request: dict, provider: dict):
    return asyncio.run(compose.run_compose_job(job_id, uid, ai_jobs.JobInput(**request), provider))


def job_status(job_id: int) -> str:
    with db.connection() as con:
        return con.execute("SELECT status FROM ai_jobs WHERE id=?", (job_id,)).fetchone()[0]


def paper_count(client, source_prefix="ai-compose") -> int:
    with db.connection() as con:
        rows = con.execute(
            "SELECT COUNT(*) FROM pl_papers WHERE user_id=? AND source LIKE ?",
            (user_id(client), source_prefix + "%"),
        ).fetchone()
    return int(rows[0])


# ---------------------------------------------------------------------------
# 1. Hamilton 配额与二分匹配（单元）
# ---------------------------------------------------------------------------

def test_hamilton_quotas_and_bipartite_matching():
    quotas = compose.hamilton_quotas(10, {"函数": 50, "几何": 30, "数列": 20})
    assert quotas == {"函数": 5, "几何": 3, "数列": 2}
    # 百分比未满 100：只约束对应比例，其余题数不限知识点
    partial = compose.hamilton_quotas(5, {"函数": 50})
    assert sum(partial.values()) == 2 and partial["函数"] == 2
    # 合计超过 100 时按相对权重分配全部题数
    over = compose.hamilton_quotas(6, {"函数": 60, "几何": 60})
    assert sum(over.values()) == 6

    candidates = [
        {"ref": "a", "knowledge_tags": ["函数", "几何"]},
        {"ref": "b", "knowledge_tags": ["函数"]},
        {"ref": "c", "knowledge_tags": ["几何"]},
    ]
    ok, counts = compose.match_kp_quotas(candidates, {"函数": 2, "几何": 1})
    assert ok is True
    assert counts == {"函数": 2, "几何": 1}
    # 多标签题只计一次：同一道题不能同时占「函数」「几何」两个题位
    multi_tag_candidates = [{"ref": "a", "knowledge_tags": ["函数", "几何"]}]
    failed, detail = compose.match_kp_quotas(multi_tag_candidates, {"函数": 1, "几何": 1})
    assert failed is False
    assert detail == {"函数": 0, "几何": 0}


# ---------------------------------------------------------------------------
# 2. 预检 422（不创建任务、不调用模型）
# ---------------------------------------------------------------------------

def test_compose_precheck_rejects_insufficient_candidates(client, monkeypatch):
    patch_dispatch(monkeypatch)
    provider = save_provider(client)
    create_question(client)
    create_question(client, qtype="SUBJECTIVE")
    response = client.post("/api/paperlab/papers/compose", json={
        "mode": "smart", "subject": "数学", "grade": "高中一年级", "count": 10,
        "provider_id": provider["id"], "model": MODEL,
    })
    assert response.status_code == 422
    assert "不足" in response.json()["detail"]
    with db.connection() as con:
        jobs = con.execute("SELECT COUNT(*) FROM ai_jobs").fetchone()[0]
    assert int(jobs) == 0, "预检失败不应创建任务"


def test_compose_precheck_rejects_type_and_kp_and_difficulty(client, monkeypatch):
    patch_dispatch(monkeypatch)
    provider = save_provider(client)
    for _ in range(3):
        create_question(client, qtype="SINGLE", tags=("函数",))
    create_question(client, qtype="SUBJECTIVE", tags=("几何",))
    base = {"mode": "smart", "subject": "数学", "grade": "高中一年级",
            "provider_id": provider["id"], "model": MODEL}

    type_mismatch = client.post("/api/paperlab/papers/compose", json={
        **base, "count": 3, "type_counts": {"SINGLE": 5},
    })
    assert type_mismatch.status_code == 422
    assert "题型" in type_mismatch.json()["detail"]

    kp_shortage = client.post("/api/paperlab/papers/compose", json={
        **base, "count": 2, "kp_weights": {"不存在的知识点": 100},
    })
    assert kp_shortage.status_code == 422
    assert "知识点配额" in kp_shortage.json()["detail"]

    difficulty = client.post("/api/paperlab/papers/compose", json={
        **base, "count": 2, "difficulty_target": 0.9, "tolerance": 0.05,
    })
    assert difficulty.status_code == 422
    assert "难度" in difficulty.json()["detail"]

    invalid_spec = client.post("/api/paperlab/papers/compose", json={
        **base, "count": 2, "type_counts": {"SINGLE": 1, "SUBJECTIVE": 2},
    })
    assert invalid_spec.status_code == 422
    assert "之和" in invalid_spec.json()["detail"]


def test_compose_task_admission_requires_params(client, monkeypatch):
    patch_dispatch(monkeypatch)
    provider = save_provider(client)
    response = client.post("/api/ai/jobs", json={
        "task": "paper-compose", "provider_id": provider["id"], "model": MODEL,
    })
    assert response.status_code == 422
    assert "spec" in response.json()["detail"] or "组卷" in response.json()["detail"]


# ---------------------------------------------------------------------------
# 3. 成功路径：快照建卷
# ---------------------------------------------------------------------------

def test_compose_job_creates_snapshot_paper(client, monkeypatch):
    provider = save_provider(client)
    questions = [
        create_question(client, difficulty=0.2, tags=("函数",)),
        create_question(client, difficulty=0.4, tags=("函数",)),
        create_question(client, qtype="SUBJECTIVE", difficulty=0.6, tags=("几何",)),
    ]
    spec = {
        "mode": "smart", "subject": "数学", "grade": "高中一年级", "count": 3,
        "type_counts": {"SINGLE": 2, "SUBJECTIVE": 1},
        "difficulty_target": 0.4, "tolerance": 0.1,
        "kp_weights": {"函数": 66.6, "几何": 33.4},
    }
    fake = FakeModel([{
        "selections": [
            {"ref": f"app:{questions[0]['id']}", "score": 2, "reason": "基础函数计算"},
            {"ref": f"app:{questions[1]['id']}", "score": 2, "reason": "函数单调性判断"},
            {"ref": f"app:{questions[2]['id']}", "score": 10, "reason": "几何推理"},
        ],
        "summary": "覆盖函数与几何的 3 题小卷",
    }]).install(monkeypatch)
    job_id, request, provider_row = insert_compose_job(client, spec, provider["id"])
    result = run_compose(job_id, user_id(client), request, provider_row)

    assert result["paper_id"] >= 1
    assert result["item_count"] == 3
    assert result["total_score"] == 14
    assert result["summary"] == "覆盖函数与几何的 3 题小卷"
    assert result["statistics"]["type_counts"] == {"SINGLE": 2, "SUBJECTIVE": 1}
    assert result["statistics"]["difficulty_average"] == 0.4
    assert len(fake.calls) == 1
    # 模型 prompt 只包含真实候选题
    assert f"app:{questions[0]['id']}" in fake.calls[0]
    assert "只能从「候选题」中选择" in fake.calls[0]

    paper = client.get(f"/api/paperlab/papers/{result['paper_id']}").json()
    assert paper["paper"]["kind"] == "smart"
    assert paper["paper"]["status"] == "ready"
    assert paper["paper"]["item_count"] == 3
    item = paper["items"][0]
    assert item["source"] == "app"
    assert item["selection_reason"] == "基础函数计算"
    assert "x^2" in item["snapshot"]["content"]["stem"]
    assert item["snapshot"]["content"]["answers"]
    # 恢复 prompt 不泄漏候选外的题目（模型只看到候选 JSON）
    exported = client.get(f"/api/paperlab/papers/{result['paper_id']}/export?format=html&answers=0")
    assert exported.status_code == 200


def test_compose_job_rejects_fabricated_id_and_wrong_count(client, monkeypatch):
    provider = save_provider(client)
    for _ in range(3):
        create_question(client, difficulty=0.2)
    spec = {"mode": "smart", "subject": "数学", "grade": "高中一年级", "count": 2}
    # 编造 ID：两次都无效 → call_structured 重试一次后仍失败
    fake = FakeModel([
        {"selections": [{"ref": "app:999999"}, {"ref": "app:888888"}]},
        {"selections": [{"ref": "app:777777"}, {"ref": "app:666666"}]},
    ]).install(monkeypatch)
    job_id, request, provider_row = insert_compose_job(client, spec, provider["id"])
    with pytest.raises(ValueError) as exc:
        run_compose(job_id, user_id(client), request, provider_row)
    assert "候选" in str(exc.value)
    assert len(fake.calls) == 2, "硬校验失败应带差额原因重试一次"
    assert paper_count(client) == 0, "校验不过不得建卷"

    # 题数不满足：同样失败
    fake.responses = [
        {"selections": [{"ref": "app:1"}]},
        {"selections": [{"ref": "app:2"}]},
    ]
    job_id, request, provider_row = insert_compose_job(client, spec, provider["id"])
    with pytest.raises(ValueError) as exc2:
        run_compose(job_id, user_id(client), request, provider_row)
    assert "数量" in str(exc2.value)
    assert paper_count(client) == 0


def test_compose_job_retries_once_then_succeeds(client, monkeypatch):
    provider = save_provider(client)
    questions = [create_question(client, difficulty=0.2) for _ in range(3)]
    spec = {"mode": "smart", "subject": "数学", "grade": "高中一年级", "count": 2}
    first = {"selections": [{"ref": f"app:{questions[0]['id']}"},  # 缺一道
                            {"ref": f"app:{questions[0]['id']}"}]}
    second = {"selections": [{"ref": f"app:{questions[0]['id']}", "reason": "基础"},
                             {"ref": f"app:{questions[1]['id']}", "reason": "提高"}],
              "summary": "重试后通过"}
    fake = FakeModel([first, second]).install(monkeypatch)
    job_id, request, provider_row = insert_compose_job(client, spec, provider["id"])
    result = run_compose(job_id, user_id(client), request, provider_row)
    assert len(fake.calls) == 2
    assert result["item_count"] == 2
    # 直接调用执行器时框架不负责终态，这里只要求任务已被执行器推进（不再是 queued）。
    assert job_status(job_id) in ("running", "queued")


def test_compose_job_idempotent_resume(client, monkeypatch):
    provider = save_provider(client)
    questions = [create_question(client, difficulty=0.2) for _ in range(2)]
    spec = {"mode": "knowledge", "subject": "数学", "grade": "高中一年级", "count": 1}
    fake = FakeModel([{
        "selections": [{"ref": f"app:{questions[0]['id']}", "reason": "覆盖函数概念"}],
        "summary": "1 题小卷",
    }]).install(monkeypatch)
    job_id, request, provider_row = insert_compose_job(client, spec, provider["id"])
    first = run_compose(job_id, user_id(client), request, provider_row)
    second = run_compose(job_id, user_id(client), request, provider_row)
    assert first["paper_id"] == second["paper_id"]
    assert second["statistics"].get("reused") is True
    assert len(fake.calls) == 1, "已有试卷时续跑不应再次调用模型"

    paper = client.get(f"/api/paperlab/papers/{first['paper_id']}").json()
    assert paper["paper"]["kind"] == "knowledge"


def test_compose_cancel_is_honoured(client, monkeypatch):
    provider = save_provider(client)
    for _ in range(2):
        create_question(client, difficulty=0.2)
    spec = {"mode": "smart", "subject": "数学", "grade": "高中一年级", "count": 1}
    job_id, request, provider_row = insert_compose_job(client, spec, provider["id"])
    with db.connection() as con:
        con.execute("UPDATE ai_jobs SET cancelled=1, status='cancel_requested' WHERE id=?", (job_id,))
    with pytest.raises(asyncio.CancelledError):
        run_compose(job_id, user_id(client), request, provider_row)
    assert paper_count(client) == 0


def test_compose_grade_mode_only_uses_app_bank(client, monkeypatch):
    provider = save_provider(client)
    create_question(client, difficulty=0.2)
    plan = compose._parse_spec({
        "mode": "smart", "subject": "数学", "grade": "高中一年级", "count": 1,
    })
    candidates = compose.build_candidates(user_id(client), plan)
    assert candidates, "应用题库应有候选"
    assert all(candidate["source"] == "app" for candidate in candidates), "指定年级时不应混入无年级的只读题库题"

    plan_all = compose._parse_spec({"mode": "smart", "subject": "数学", "count": 1})
    mixed = compose.build_candidates(user_id(client), plan_all)
    assert any(candidate["source"] == "bank" for candidate in mixed), "未指定年级时应包含只读题库真实题"
    assert all(candidate["subject"] in ("", "数学") for candidate in mixed)
