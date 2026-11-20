"""P0/P1 AI 链路加固回归测试。

覆盖：交叉验证与讲解模式解耦、SymPy DoS 预算与可强杀子进程、笔记 Agent 的 L0
系统提示词/续跑清理、全链路 job_id 记账、答案比较口径、裁判题图与互审复判、
适配层降级、向量状态、Redis 队列租约/分布式取消、拍题压图与打标预估。

全部离线：模型调用一律 monkeypatch `ai._call_model`，不联网、不消耗额度。
"""
import asyncio
from io import BytesIO
import json
import time

import httpx
from PIL import Image
import pytest

from app import ai, db
from app.ai import adapters, jobs, notes_agent, upload_pipeline, verify
from test_ai import save_provider


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------

def _structured(value, kind="choice", steps_content="推导"):
    return json.dumps({
        "final_answer": {"kind": kind, "value": value},
        "steps": [{"no": 1, "title": "分析", "content": steps_content, "score_point": True}],
        "knowledge_points": ["测试知识点"], "method": "通法", "assumptions": [], "uncertain": [],
    }, ensure_ascii=False)


def _insert_job(user_id, status="running", *, token="tok-test", request=None):
    now = db.utcnow()
    with db.connection() as con:
        return con.execute(
            "INSERT INTO ai_jobs(user_id,task,status,stage,request_json,partial_content,cancelled,"
            "created_at,updated_at,dispatch_token,heartbeat_at) VALUES (?,?,?,?,?,'',0,?,?,?,?)",
            (
                user_id, "explain", status, status,
                json.dumps(request or {"task": "explain", "provider_id": 1, "model": "m"}),
                now, now, token,
                now if status == "running" and token else None,
            ),
        ).lastrowid


# ---------------------------------------------------------------------------
# 1. 交叉验证 vs 讲解模式解耦
# ---------------------------------------------------------------------------

def test_modes_decoupled_from_structured_solve(client, monkeypatch):
    calls = []

    async def fake_call(provider, model, prompt, images=None, json_output=False, **kwargs):
        calls.append({"model": model, "prompt": prompt, "images": images, **kwargs})
        if "请独立求解本题" in prompt:
            return {"content": _structured("A"), "model": model,
                    "provider_name": "mock", "usage": {"total_tokens": 4}}
        if "【启发引导】" in prompt:
            return {"content": "HINTS-BODY", "model": model,
                    "provider_name": "mock", "usage": {"total_tokens": 4}}
        if "【标准步骤" in prompt:
            return {"content": "STEPS-BODY", "model": model,
                    "provider_name": "mock", "usage": {"total_tokens": 4}}
        return {"content": "OTHER-BODY", "model": model,
                "provider_name": "mock", "usage": {"total_tokens": 4}}

    monkeypatch.setattr(ai, "_call_model", fake_call)
    monkeypatch.setattr(ai, "_bank_evidence", lambda *a: (
        {"题干": "题干", "选项": [], "原卷参考答案": "A", "原卷解析": "", "材料": None,
         "学科": "数学", "题型": "单选题"}, [],
    ))
    data = ai.JobInput(
        task="explain", qid="q-mode-test", provider_id=1, model="model-a",
        cross_verify=True, verify_provider_id=2, verify_model="model-b",
        structured_verify=True, modes=["hints", "standard_steps"],
    )
    result = asyncio.run(ai._explain_job(1, data, {"id": 1, "name": "p"}, {"id": 2, "name": "v"},
                                         job_id=321))

    # 讲解正文 = 两个模式各自的产物；结构化 JSON 结果绝不能冒充正文。
    assert [c["mode"] for c in result["mode_cards"]] == ["hints", "standard_steps"]
    assert result["mode_cards"][0]["content"] == "HINTS-BODY"
    assert result["mode_cards"][1]["content"] == "STEPS-BODY"
    assert "HINTS-BODY" in result["content"] and "STEPS-BODY" in result["content"]
    assert "final_answer" not in result["content"]

    # 结构化独立求解只用于核验证据。
    structured_calls = [c for c in calls if "请独立求解本题" in c["prompt"]]
    assert len(structured_calls) == 2  # A/B 双模型各一次
    assert all(c["prompt"].count("final_answer") for c in structured_calls)
    independent = result["verification"]["independent_results"]
    assert [item["source"] for item in independent] == ["structured_solve", "structured_solve"]

    # hints / standard_steps 的硬约束必须出现在各自提示词里。
    hints_prompt = next(c["prompt"] for c in calls if "【启发引导】" in c["prompt"])
    steps_prompt = next(c["prompt"] for c in calls if "【标准步骤" in c["prompt"])
    assert "绝对不要直接给出最终答案" in hints_prompt
    assert "严禁给出最终答案" in hints_prompt
    assert "采分点" in steps_prompt
    # 全链路 job_id / user_id 透传
    assert all(c.get("job_id") == 321 and c.get("user_id") == 1 for c in calls)


def test_unstructured_cross_verify_reuses_primary_mode_body(client, monkeypatch):
    """未开启结构化求解时，A 的模式讲解正文可直接复用，额外模式仍分别生成。"""
    calls = []

    async def fake_call(provider, model, prompt, images=None, json_output=False, **kwargs):
        calls.append(prompt)
        if "【详细解析】" in prompt:
            return {"content": "DETAIL-BODY", "model": model, "provider_name": "mock", "usage": {}}
        if "【变式练习】" in prompt:
            return {"content": "VARIANTS-BODY", "model": model, "provider_name": "mock", "usage": {}}
        return {"content": "B-BODY", "model": model, "provider_name": "mock", "usage": {}}

    monkeypatch.setattr(ai, "_call_model", fake_call)
    monkeypatch.setattr(ai, "_bank_evidence", lambda *a: (
        {"题干": "题干", "原卷参考答案": "", "原卷解析": "", "选项": [], "学科": "数学",
         "题型": "计算题", "材料": None}, [],
    ))
    data = ai.JobInput(
        task="explain", qid="q-mode-test-2", provider_id=1, model="model-a",
        cross_verify=True, verify_provider_id=2, verify_model="model-b",
        modes=["detailed", "variants"],
    )
    result = asyncio.run(ai._explain_job(1, data, {"id": 1}, {"id": 2}))
    assert [c["mode"] for c in result["mode_cards"]] == ["detailed", "variants"]
    assert result["mode_cards"][0]["content"] == "DETAIL-BODY"
    assert result["mode_cards"][1]["content"] == "VARIANTS-BODY"
    assert result["verification"]["status"] == "partial"  # 文本题解无法程序化核验


# ---------------------------------------------------------------------------
# 2. SymPy DoS 预算与可强杀子进程
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("value", [
    "9**9**9", "10**100000", "factorial(100000)", "9**9**9**9",
    # 嵌套炸弹：内层幂/阶乘本身已经天文数字，必须先于 parse_expr 被拦下
    "2**(2**1000)", "factorial(factorial(9))",
])
def test_expression_bombs_rejected_within_budget(value):
    started = time.perf_counter()
    result = verify.compare_answers(
        {"kind": "expression", "value": value}, {"kind": "expression", "value": "1"},
    )
    assert time.perf_counter() - started < 2.0
    assert result["comparable"] is False
    assert result["agreed"] is None
    assert result["method"].startswith("rejected")


def test_power_tower_depth_and_factorial_budget_are_configurable(monkeypatch):
    monkeypatch.setenv("STUDY_VERIFY_MAX_FACTORIAL", "10")
    result = verify.compare_answers(
        {"kind": "expression", "value": "factorial(50)"}, {"kind": "expression", "value": "1"},
    )
    assert result["comparable"] is False and "factorial_too_large" in result["method"]
    monkeypatch.setenv("STUDY_VERIFY_MAX_EXPONENT", "50")
    result = verify.compare_answers(
        {"kind": "expression", "value": "2**100"}, {"kind": "expression", "value": "1"},
    )
    assert result["comparable"] is False and "exponent_too_large" in result["method"]


def test_simplify_runs_in_killable_subprocess(monkeypatch, tmp_path):
    """硬超时必须真的杀死子进程，而不是线程 join 后继续跑。"""
    sleeper = tmp_path / "slow_worker.py"
    sleeper.write_text("import time\ntime.sleep(30)\n", encoding="utf-8")
    monkeypatch.setattr(verify, "_SYMPY_WORKER", sleeper)
    started = time.perf_counter()
    verdict, reason = verify._sympy_compare_in_subprocess("Symbol('x')", "Symbol('x')", 200)
    assert time.perf_counter() - started < 5.0
    assert verdict is None and reason == "timeout"


def test_compare_answers_runs_off_event_loop(client, monkeypatch):
    """异步链路里 simplify 比对必须通过线程池调用，不能阻塞事件循环。"""
    to_thread_calls = []
    real_to_thread = asyncio.to_thread

    async def spy_to_thread(func, *args, **kwargs):
        to_thread_calls.append(getattr(func, "__name__", str(func)))
        return await real_to_thread(func, *args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", spy_to_thread)
    monkeypatch.setattr(ai, "_bank_evidence", lambda *a: (
        {"题干": "题干", "原卷参考答案": "A", "原卷解析": "", "选项": [], "学科": "数学",
         "题型": "单选题", "材料": None}, [],
    ))

    async def fake_call(provider, model, prompt, images=None, json_output=False, **kwargs):
        return {"content": _structured("A"), "model": model, "provider_name": "mock", "usage": {}}

    monkeypatch.setattr(ai, "_call_model", fake_call)
    data = ai.JobInput(
        task="explain", qid="q-async", provider_id=1, model="model-a",
        cross_verify=True, verify_provider_id=2, verify_model="model-b", structured_verify=True,
    )
    asyncio.run(ai._explain_job(1, data, {"id": 1}, {"id": 2}))
    assert "compare_answers" in to_thread_calls


# ---------------------------------------------------------------------------
# 3. 笔记 Agent：L0 系统提示词 / 去重 / 结构化摘要 / 续跑清理
# ---------------------------------------------------------------------------

def _note_doc(client, text="# 章节\n函数定义域是自变量取值范围。\n单调性用导数判断。"):
    response = client.post("/api/documents", files={
        "file": ("笔记资料.md", text.encode("utf-8"), "text/markdown"),
    })
    assert response.status_code == 200, response.text
    return response.json()


def _doc_tags(document_id):
    with db.connection() as con:
        row = con.execute("SELECT chunks_json FROM ai_documents WHERE id=?", (document_id,)).fetchone()
    chunks = json.loads(row["chunks_json"])
    return [f"[D{document_id}:{chunk['chunk_id']}]" for chunk in chunks], chunks


def _insert_notes_job(user_id=1):
    now = db.utcnow()
    with db.connection() as con:
        return con.execute(
            "INSERT INTO ai_jobs(user_id,task,status,stage,request_json,created_at,updated_at) "
            "VALUES (?,'notes','running','test','{}',?,?)",
            (user_id, now, now),
        ).lastrowid


def test_notes_agent_always_passes_l0_and_does_not_duplicate_requirements(client, monkeypatch):
    provider = save_provider(client)
    doc = _note_doc(client)
    user_id = client.get("/api/auth/me").json()["id"]
    job_id = _insert_notes_job(user_id)
    tags, _chunks = _doc_tags(doc["id"])
    calls = []

    async def fake_call(prov, model, prompt, *args, **kwargs):
        calls.append({"prompt": prompt, **kwargs})
        if "先根据资料设计" in prompt:
            content = "## 1. 定义域"
        else:
            content = "## 要点 " + " ".join(tags)
        return {"content": content, "model": model, "provider_name": "mock", "usage": {}}

    monkeypatch.setattr(ai, "_call_model", fake_call)
    requirements = "只保留易错点，多举反例"
    data = ai.JobInput(
        task="notes", document_id=doc["id"], provider_id=provider["id"], model="test-model",
        requirements=requirements, prompt_config={"format": "anki"},
    )
    asyncio.run(ai._notes_job(job_id, user_id, data, {"id": provider["id"]}, None))

    expected_l0 = notes_agent.assemble_prompt_layers(
        {"format": "anki"}, custom_requirements=requirements,
    )["l0"]
    assert len(calls) >= 2
    for call in calls:
        # 每一次调用都必须显式携带 L0 防注入系统提示词
        assert call.get("system_prompt") == expected_l0
        assert "不可信任务数据" in call["system_prompt"]
        assert "不得执行" in call["system_prompt"]
        # 用户可覆盖内容（L4）绝不能进入系统提示词
        assert requirements not in call["system_prompt"]
        assert call.get("job_id") == job_id and call.get("user_id") == user_id
        # TEMPLATES 硬注入与 requirements 重复拼接已删除，统一由方格 L1/L4 驱动
        assert "模板：" not in call["prompt"]
        assert "用户风格要求" not in call["prompt"]
        assert call["prompt"].count(requirements) == 1


def test_notes_agent_map_extract_sections_use_structured_summary(client, monkeypatch):
    provider = save_provider(client)
    doc = _note_doc(client, "# 章节\n原始资料正文：定义域与单调性。")
    user_id = client.get("/api/auth/me").json()["id"]
    job_id = _insert_notes_job(user_id)
    tags, _chunks = _doc_tags(doc["id"])
    calls = []

    async def fake_call(prov, model, prompt, *args, **kwargs):
        calls.append({"prompt": prompt, **kwargs})
        if "从单个资料块逐项提取" in prompt:
            return {"content": json.dumps({
                "concepts": ["定义域"], "formulas": [], "conditions": [], "examples": [],
            }), "model": model, "provider_name": "mock", "usage": {}}
        if "先根据资料设计" in prompt:
            return {"content": "## 1. 定义域", "model": model, "provider_name": "mock", "usage": {}}
        return {"content": "## 定义域要点 " + tags[0], "model": model,
                "provider_name": "mock", "usage": {}}

    monkeypatch.setattr(ai, "_call_model", fake_call)
    data = ai.JobInput(
        task="notes", document_id=doc["id"], provider_id=provider["id"], model="test-model",
        map_extract=True,
    )
    result = asyncio.run(ai._notes_job(job_id, user_id, data, {"id": provider["id"]}, None))
    assert result["coverage"]["map_extracted_chunks"] == len(tags)

    extract_calls = [c for c in calls if "从单个资料块逐项提取" in c["prompt"]]
    assert extract_calls and extract_calls[0]["system_prompt"] == notes_agent.assemble_prompt_layers(
        {}, custom_requirements=""
    )["l0"]
    gen_prompts = [c["prompt"] for c in calls if "从单个资料块逐项提取" not in c["prompt"]]
    assert gen_prompts
    # 分节/成文使用 map_extract 的结构化摘要，而不是回原文做 top_k 检索
    assert any("【概念】定义域" in prompt for prompt in gen_prompts)
    for prompt in gen_prompts:
        assert "原始资料正文：定义域与单调性" not in prompt


def test_notes_agent_clears_bad_sections_on_citation_failure(client, monkeypatch):
    provider = save_provider(client)
    doc = _note_doc(client)
    user_id = client.get("/api/auth/me").json()["id"]
    job_id = _insert_notes_job(user_id)
    tags, _chunks = _doc_tags(doc["id"])
    bad_tag = "[D999999:p9-c9]"
    state = {"run": 0, "calls": 0}
    threadpool_calls = []
    real_run_in_threadpool = notes_agent.run_in_threadpool

    async def spy_run_in_threadpool(func, *args, **kwargs):
        threadpool_calls.append(getattr(func, "__name__", str(func)))
        return await real_run_in_threadpool(func, *args, **kwargs)

    monkeypatch.setattr(notes_agent, "run_in_threadpool", spy_run_in_threadpool)
    jobs._job_set_checkpoint(job_id, {
        "confirmed_outline": "## 1. 定义域\n## 2. 单调性",
        "confirmed_sections": ["定义域", "单调性"],
    })

    async def fake_call(prov, model, prompt, *args, **kwargs):
        state["calls"] += 1
        if "第 1 节" in prompt:
            tag = bad_tag if state["run"] == 0 else tags[0]
        else:
            tag = tags[0]
        return {"content": f"## 小节 {tag}", "model": model, "provider_name": "mock", "usage": {}}

    monkeypatch.setattr(ai, "_call_model", fake_call)
    data = ai.JobInput(task="notes", document_id=doc["id"], provider_id=provider["id"],
                       model="test-model")

    with pytest.raises(ValueError) as error:
        asyncio.run(ai._notes_job(job_id, user_id, data, {"id": provider["id"]}, None))
    assert "引用" in str(error.value)
    # 坏分节必须从 checkpoint 清除，好分节保留
    with db.connection() as con:
        checkpoint = json.loads(con.execute(
            "SELECT checkpoint_json FROM ai_jobs WHERE id=?", (job_id,)
        ).fetchone()["checkpoint_json"])
    assert set(checkpoint["generated_sections"].keys()) == {"1"}
    assert bad_tag not in json.dumps(checkpoint["generated_sections"], ensure_ascii=False)
    # checkpoint 写入走线程池（不在协程里同步 SQLite）
    assert "_job_set_checkpoint" in threadpool_calls

    state["run"] = 1
    before = state["calls"]
    result = asyncio.run(ai._notes_job(job_id, user_id, data, {"id": provider["id"]}, None))
    assert state["calls"] - before == 1  # 只重新生成被清除的那一节
    assert result["note_id"]
    note = client.get(f"/api/notes/{result['note_id']}").json()
    assert bad_tag not in note["content"]


# ---------------------------------------------------------------------------
# 4. 全链路 job_id / user_id 记账
# ---------------------------------------------------------------------------

def test_call_structured_forwards_user_and_job_id(monkeypatch):
    captured = {}

    async def fake_call(provider, model, prompt, images=None, json_output=False, **kwargs):
        captured.update(kwargs)
        return {"content": json.dumps({"ok": True}), "model": model,
                "provider_name": "mock", "usage": {}}

    monkeypatch.setattr(ai, "_call_model", fake_call)
    parsed, _call = asyncio.run(ai.call_structured(
        {"id": 1}, "m", "prompt", lambda value: value, user_id=7, job_id=99,
    ))
    assert parsed == {"ok": True}
    assert captured["user_id"] == 7 and captured["job_id"] == 99


def test_grade_job_forwards_job_id(client, monkeypatch):
    from app import practice

    captured = []

    async def fake_call(provider, model, prompt, images=None, json_output=False, **kwargs):
        captured.append(kwargs)
        if "共同评分标准" in prompt:
            return {"content": json.dumps({"max_score": 100, "criteria": [
                {"label": "结论", "possible": 40, "expected": "正确"},
                {"label": "推导", "possible": 60, "expected": "有效"},
            ]}), "model": model, "provider_name": "mock", "usage": {}}
        return {"content": json.dumps({
            "is_correct": True, "score": 100, "max_score": 100,
            "steps": [{"label": "结论", "earned": 40, "possible": 40, "feedback": ""},
                      {"label": "推导", "earned": 60, "possible": 60, "feedback": ""}],
            "feedback": "ok",
        }), "model": model, "provider_name": "mock", "usage": {}}

    monkeypatch.setattr(ai, "_call_model", fake_call)
    monkeypatch.setattr(ai, "_bank_evidence", lambda *a: ({"原卷参考答案": "答案"}, []))
    monkeypatch.setattr(practice, "latest_attempt", lambda *a: {"id": 7, "answer_text": "答案"})
    monkeypatch.setattr(practice, "get_attempt_image", lambda *a: None)
    monkeypatch.setattr(practice, "set_ai_grade", lambda *a: None)
    data = ai.JobInput(task="grade", qid="q", attempt_id=7, provider_id=1, model="one",
                       cross_verify=True, verify_provider_id=2, verify_model="two")
    asyncio.run(ai._grade_job(1, data, {"id": 1}, {"id": 2}, job_id=456))
    assert captured and all(kw.get("job_id") == 456 and kw.get("user_id") == 1 for kw in captured)


def test_recognize_job_forwards_job_id(client, monkeypatch):
    provider = save_provider(client)
    uploaded = client.post("/api/uploads/question", files={
        "file": ("题目.txt", "求导".encode(), "text/plain"),
    }).json()
    user_id = client.get("/api/auth/me").json()["id"]
    captured = []

    async def fake_call(prov, model, prompt, images=None, json_output=False, **kwargs):
        captured.append(kwargs)
        return {"content": json.dumps({
            "subject": "数学", "type": "计算题", "stem": "求导", "options": [],
            "conditions": [], "uncertain": [],
        }), "model": model, "provider_name": "mock", "usage": {}}

    monkeypatch.setattr(ai, "_call_model", fake_call)
    data = ai.JobInput(task="recognize", upload_id=uploaded["id"],
                       provider_id=provider["id"], model="test-model",
                       cross_verify=True, verify_provider_id=provider["id"],
                       verify_model="verify-model")
    result = asyncio.run(ai._recognize_job(
        user_id, data, {"id": provider["id"]}, {"id": provider["id"]}, job_id=789,
    ))
    assert captured and all(kw.get("job_id") == 789 and kw.get("user_id") == user_id for kw in captured)
    # 双模型识别差异字段必须原样返回给前端
    assert "secondary_recognized" in result and "differences" in result


def test_usage_ledger_records_the_task_job_id(client, monkeypatch):
    """真实适配器落账：ai_usage.job_id 必须对得上具体任务。"""
    user_id = client.get("/api/auth/me").json()["id"]

    def handler(request):
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "账本测试"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 3},
        })

    monkeypatch.setattr(ai.httpx, "AsyncClient", _mock_transport(handler))
    monkeypatch.setattr(ai, "_validate_url", lambda value, **kw: value)
    provider = {
        "kind": "openai", "name": "ledger", "base_url": "https://api.deepseek.com/v1",
        "encrypted_key": ai._cipher().encrypt(b"test-secret-key").decode(),
    }
    asyncio.run(adapters._call_model(
        provider, "deepseek-chat", "hi", user_id=user_id, job_id=2024,
    ))
    with db.connection() as con:
        row = con.execute(
            "SELECT job_id, input_tokens, output_tokens FROM ai_usage WHERE user_id=? ORDER BY id DESC LIMIT 1",
            (user_id,),
        ).fetchone()
    assert row["job_id"] == 2024
    assert row["input_tokens"] == 12 and row["output_tokens"] == 3


# ---------------------------------------------------------------------------
# 5. 比较逻辑口径
# ---------------------------------------------------------------------------

def test_relative_equal_uses_pure_relative_error_for_tiny_values():
    result = verify.compare_answers(
        {"kind": "number", "value": "1.8e-10"}, {"kind": "number", "value": "5e-10"},
    )
    assert result["comparable"] is True and result["agreed"] is False
    close = verify.compare_answers(
        {"kind": "number", "value": "1.8e-10"}, {"kind": "number", "value": "1.80000001e-10"},
    )
    assert close["agreed"] is True
    zero = verify.compare_answers(
        {"kind": "number", "value": 0}, {"kind": "number", "value": "1e-8"},
    )
    assert zero["agreed"] is True  # 零值用绝对误差


def test_letters_are_not_choice_sets():
    assert verify._classify_kind("text", "text", "a", "b") == "expression"
    # 纯字母代数答案按表达式比较，不再按选项集合
    same = verify.compare_answers({"kind": "text", "value": "a"}, {"kind": "text", "value": "a"})
    assert same["method"] == "sympy"
    different = verify.compare_answers({"kind": "text", "value": "AB"}, {"kind": "text", "value": "BA"})
    assert different.get("method") != "choice_set"
    assert different["agreed"] is not True
    # 双方都声明 choice 才走选项集合
    choice = verify.compare_answers({"kind": "choice", "value": "AB"}, {"kind": "choice", "value": "B,A"})
    assert choice["agreed"] is True and choice["method"] == "choice_set"


def test_literal_equal_is_downgraded_and_not_share_eligible(client, monkeypatch):
    literal = verify.compare_answers(
        {"kind": "expression", "value": "AB"}, {"kind": "expression", "value": "AB"},
    )
    assert literal["agreed"] is True
    assert literal["method"] == "literal_equal_unverified"
    assert literal["warning"] == "literal_equal_unverified"
    assert literal["verified"] is False

    async def fake_call(provider, model, prompt, images=None, json_output=False, **kwargs):
        return {"content": _structured("AB", kind="expression"), "model": model,
                "provider_name": "mock", "usage": {}}

    monkeypatch.setattr(ai, "_call_model", fake_call)
    monkeypatch.setattr(ai, "_bank_evidence", lambda *a: (
        {"题干": "题干", "选项": [], "原卷参考答案": "AB", "原卷解析": "",
         "材料": None, "学科": "数学", "题型": "填空题"}, [],
    ))
    data = ai.JobInput(
        task="explain", qid="q-literal", provider_id=1, model="model-a",
        cross_verify=True, verify_provider_id=2, verify_model="model-b", structured_verify=True,
    )
    result = asyncio.run(ai._explain_job(1, data, {"id": 1}, {"id": 2}))
    verification = result["verification"]
    assert verification["warning"] == "literal_equal_unverified"
    assert verification["confidence"] == "low"
    with db.connection() as con:
        row = con.execute(
            "SELECT share_eligible FROM ai_explanations WHERE qid=?", ("q-literal",),
        ).fetchone()
    assert row["share_eligible"] == 0


# ---------------------------------------------------------------------------
# 6. 裁判题图 / 原卷冲突降级 / 互审复判
# ---------------------------------------------------------------------------

def test_judge_receives_images_and_conflict_downgrades(client, monkeypatch):
    captured = []
    image_part = {"mime": "image/png", "data": "AAAA"}

    async def fake_call(provider, model, prompt, images=None, json_output=False, **kwargs):
        captured.append({"model": model, "prompt": prompt, "images": images})
        if "请独立求解本题" in prompt:
            value = "A" if model == "model-a" else "B"
            return {"content": _structured(value), "model": model,
                    "provider_name": "mock", "usage": {}}
        if "你是独立裁判" in prompt:
            return {"content": json.dumps({
                "agreed": True, "diverge_at_step": None, "which_is_right": "unsure",
                "reason": "看起来一致", "confidence": "high", "summary": "一致", "differences": [],
            }), "model": model, "provider_name": "mock", "usage": {}}
        return {"content": "详细解析正文", "model": model, "provider_name": "mock", "usage": {}}

    monkeypatch.setattr(ai, "_call_model", fake_call)
    monkeypatch.setattr(ai, "_bank_evidence", lambda *a: (
        {"题干": "题干", "选项": [], "原卷参考答案": "C", "原卷解析": "",
         "材料": None, "学科": "数学", "题型": "单选题"}, [image_part],
    ))
    data = ai.JobInput(
        task="explain", qid="q-judge", provider_id=1, model="model-a",
        cross_verify=True, verify_provider_id=2, verify_model="model-b",
        judge_provider_id=3, judge_model="model-c", structured_verify=True,
    )
    result = asyncio.run(ai._explain_job(
        1, data, {"id": 1}, {"id": 2}, judge={"id": 3}, judge_model="model-c", job_id=11,
    ))
    judge_calls = [c for c in captured if "你是独立裁判" in c["prompt"]]
    assert judge_calls and judge_calls[0]["images"] == [image_part]
    verification = result["verification"]
    # 双解程序化不一致（A vs B），裁判却说一致 → 必须降级标注
    assert verification["status"] == "agreed"
    assert verification["confidence"] == "low"
    assert verification["reference_conflict"] is False
    assert verification["warning"] == "programmatic_disagreed"


def test_judge_agreement_with_reference_conflict_is_flagged(client, monkeypatch):
    async def fake_call(provider, model, prompt, images=None, json_output=False, **kwargs):
        if "请独立求解本题" in prompt:
            return {"content": _structured("A"), "model": model,
                    "provider_name": "mock", "usage": {}}
        return {"content": "详细解析正文", "model": model, "provider_name": "mock", "usage": {}}

    monkeypatch.setattr(ai, "_call_model", fake_call)
    # 双解一致为 A，但原卷答案是 C → 参考冲突
    monkeypatch.setattr(ai, "_bank_evidence", lambda *a: (
        {"题干": "题干", "选项": [], "原卷参考答案": "C", "原卷解析": "",
         "材料": None, "学科": "数学", "题型": "单选题"}, [],
    ))
    data = ai.JobInput(
        task="explain", qid="q-ref-conflict", provider_id=1, model="model-a",
        cross_verify=True, verify_provider_id=2, verify_model="model-b", structured_verify=True,
    )
    result = asyncio.run(ai._explain_job(1, data, {"id": 1}, {"id": 2}))
    verification = result["verification"]
    assert verification["status"] == "agreed"
    assert verification["reference_conflict"] is True
    assert verification["confidence"] == "medium"
    with db.connection() as con:
        row = con.execute(
            "SELECT share_eligible FROM ai_explanations WHERE qid=?", ("q-ref-conflict",),
        ).fetchone()
    assert row["share_eligible"] == 0


def test_peer_review_recheck_updates_status_and_confidence(client, monkeypatch):
    async def fake_call(provider, model, prompt, images=None, json_output=False, **kwargs):
        if "请独立求解本题" in prompt:
            value = "A" if model == "model-a" else "B"
            return {"content": _structured(value), "model": model,
                    "provider_name": "mock", "usage": {}}
        if "你是独立裁判" in prompt:
            return {"content": json.dumps({
                "agreed": False, "diverge_at_step": 1, "which_is_right": "A",
                "reason": "B 第 1 步符号错误", "confidence": "high",
                "summary": "符号分歧", "differences": ["第1步"],
            }), "model": model, "provider_name": "mock", "usage": {}}
        if "另一位独立解题者给出了不同解答" in prompt:
            # 互审后 B 修正为 A：修正后程序化比对应重新通过
            return {"content": _structured("A"), "model": model,
                    "provider_name": "mock", "usage": {}}
        return {"content": "详细解析正文", "model": model, "provider_name": "mock", "usage": {}}

    monkeypatch.setattr(ai, "_call_model", fake_call)
    monkeypatch.setattr(ai, "_bank_evidence", lambda *a: (
        {"题干": "题干", "选项": [], "原卷参考答案": "A", "原卷解析": "",
         "材料": None, "学科": "数学", "题型": "单选题"}, [],
    ))
    data = ai.JobInput(
        task="explain", qid="q-peer", provider_id=1, model="model-a",
        cross_verify=True, verify_provider_id=2, verify_model="model-b",
        judge_provider_id=3, judge_model="model-c", structured_verify=True, peer_review=True,
    )
    result = asyncio.run(ai._explain_job(
        1, data, {"id": 1}, {"id": 2}, judge={"id": 3}, judge_model="model-c",
    ))
    verification = result["verification"]
    assert verification["peer_review"]["recheck"]["agreed"] is True
    assert verification["status"] == "agreed"
    assert verification["confidence"] == "medium"
    assert verification["programmatic"]["matches_reference"] is True


# ---------------------------------------------------------------------------
# 7. 适配层降级
# ---------------------------------------------------------------------------

def test_stream_retry_does_not_duplicate_deltas():
    async def run():
        collected = []

        async def on_delta(text):
            collected.append(text)

        gate = adapters._DeltaGate(on_delta)
        for piece in ("abc", "def"):
            await gate.push(piece)
        # 网络中断后重试：厂商从头重放同样的前缀
        gate.start_attempt()
        for piece in ("a", "b", "c", "d", "e", "f", "g"):
            await gate.push(piece)
        return "".join(collected)

    assert asyncio.run(run()) == "abcdefg"


def _mock_transport(handler):
    original = httpx.AsyncClient

    def factory(**kwargs):
        return original(transport=httpx.MockTransport(handler), **kwargs)

    return factory


def test_400_on_optional_params_stripped_and_retried(monkeypatch):
    requests = []

    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        if "stream_options" in body:
            return httpx.Response(400, json={
                "error": {"message": "Unsupported parameter: stream_options"},
            })
        content = (
            b'data: {"choices":[{"delta":{"content":"ok"},"finish_reason":null}]}\n\n'
            b'data: [DONE]\n\n'
        )
        return httpx.Response(200, content=content,
                              headers={"content-type": "text/event-stream"})

    monkeypatch.setattr(ai.httpx, "AsyncClient", _mock_transport(handler))
    monkeypatch.setattr(ai, "_validate_url", lambda value, **kw: value)
    provider = {
        "kind": "openai", "name": "mock", "base_url": "https://gateway.example/v1",
        "encrypted_key": ai._cipher().encrypt(b"test-secret-key").decode(),
    }
    deltas = []

    async def on_delta(text):
        deltas.append(text)

    result = asyncio.run(adapters._call_model(provider, "test-model", "hi", on_delta=on_delta))
    assert result["content"] == "ok"
    assert "stream_options" in requests[0]
    assert "stream_options" not in requests[1]
    assert "".join(deltas) == "ok"


def test_reasoning_model_token_floor_and_notice(monkeypatch):
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "思考完成"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        })

    monkeypatch.setattr(ai.httpx, "AsyncClient", _mock_transport(handler))
    monkeypatch.setattr(ai, "_validate_url", lambda value, **kw: value)
    key = ai._cipher().encrypt(b"test-secret-key").decode()
    reasoning = {"kind": "openai", "name": "r1", "base_url": "https://api.deepseek.com/v1",
                 "encrypted_key": key}
    result = asyncio.run(adapters._call_model(reasoning, "deepseek-r1", "hi", token_limit=1024))
    assert bodies[0]["max_tokens"] >= adapters.REASONING_TOKEN_FLOOR
    assert "推理模型" in result.get("notice", "")
    # 非推理模型保持用户设定的 token 预算
    chat = {"kind": "openai", "name": "chat", "base_url": "https://api.deepseek.com/v1",
            "encrypted_key": key}
    asyncio.run(adapters._call_model(chat, "deepseek-chat", "hi", token_limit=1024))
    assert bodies[1]["max_tokens"] == 1024


# ---------------------------------------------------------------------------
# 8. 向量状态不得假 ready
# ---------------------------------------------------------------------------

def test_vector_failure_marks_stale(client, monkeypatch):
    doc = _note_doc(client, "# 章节\n需要向量化的内容。")
    user_id = client.get("/api/auth/me").json()["id"]
    monkeypatch.setattr(notes_agent, "embedding_config", lambda *args, **kwargs: object())
    monkeypatch.setattr(notes_agent, "document_vectors", lambda chunks, **kwargs: _async_value([]))
    with db.connection() as con:
        con.execute("UPDATE ai_documents SET vector_status='ready', vector_digest='old' WHERE id=?",
                    (doc["id"],))
    result = asyncio.run(notes_agent._save_ocr_pages(
        doc["id"], user_id, [{"page": 1, "text": "# 章节\n改写后的内容。"}], manual=True,
    ))
    assert result["index_status"] == "stale"
    with db.connection() as con:
        row = con.execute("SELECT vector_status, vector_digest FROM ai_documents WHERE id=?",
                          (doc["id"],)).fetchone()
    assert row["vector_status"] == "stale" and row["vector_digest"] is None


def test_reindex_failure_marks_stale(client, monkeypatch):
    doc = _note_doc(client, "# 章节\n需要重建索引的内容。")
    monkeypatch.setattr(notes_agent, "embedding_config", lambda *args, **kwargs: object())
    monkeypatch.setattr(notes_agent, "document_vectors", lambda chunks, **kwargs: _async_value([]))
    response = client.post(f"/api/documents/{doc['id']}/reindex")
    assert response.status_code == 200, response.text
    assert response.json()["index_status"] == "stale"
    item = next(row for row in client.get("/api/documents").json()["items"] if row["id"] == doc["id"])
    assert item["index_status"] == "stale"


def _async_value(value):
    async def _inner():
        return value
    return _inner()


# ---------------------------------------------------------------------------
# 9. 队列租约 / 回收 / 分布式取消
# ---------------------------------------------------------------------------

def test_stale_lease_reaped_but_live_worker_kept(client):
    user_id = client.get("/api/auth/me").json()["id"]
    with db.connection() as con:
        stale = con.execute(
            "INSERT INTO ai_jobs(user_id,task,status,stage,request_json,partial_content,cancelled,"
            "created_at,updated_at,dispatch_token,heartbeat_at) VALUES (?,'explain','running','running','{}','',0,?,?,?,NULL)",
            (user_id, db.utcnow(), db.utcnow(), "stale-token"),
        ).lastrowid
        live = con.execute(
            "INSERT INTO ai_jobs(user_id,task,status,stage,request_json,partial_content,cancelled,"
            "created_at,updated_at,dispatch_token,heartbeat_at) VALUES (?,'explain','running','running','{}','',0,?,?,?,?)",
            (user_id, db.utcnow(), db.utcnow(), "live-token", db.utcnow()),
        ).lastrowid
        queued_token = con.execute(
            "INSERT INTO ai_jobs(user_id,task,status,stage,request_json,partial_content,cancelled,"
            "created_at,updated_at,dispatch_token) VALUES (?,'explain','queued','queued','{}','',0,?,?,?)",
            (user_id, db.utcnow(), db.utcnow(), "queued-token"),
        ).lastrowid
    result = jobs.reap_stale_jobs(lease_seconds=60, redis_mode=True)
    assert result["running"] >= 1
    with db.connection() as con:
        statuses = {
            row["id"]: row["status"]
            for row in con.execute("SELECT id,status FROM ai_jobs WHERE id IN (?,?,?)",
                                   (stale, live, queued_token)).fetchall()
        }
    assert statuses[stale] == "interrupted"
    assert statuses[live] == "running"
    # Redis 模式下已提交派发的 queued 任务必须保留，交给 recover_queued_jobs 重放
    assert statuses[queued_token] == "queued"
    # 本地模式没有 Worker 消费 queued 任务 → 中断
    jobs.reap_stale_jobs(lease_seconds=60, redis_mode=False)
    with db.connection() as con:
        assert con.execute("SELECT status FROM ai_jobs WHERE id=?", (queued_token,)).fetchone()[0] == "interrupted"


def test_init_ai_does_not_skip_token_jobs_on_restart(client, monkeypatch):
    user_id = client.get("/api/auth/me").json()["id"]
    with db.connection() as con:
        running = con.execute(
            "INSERT INTO ai_jobs(user_id,task,status,stage,request_json,partial_content,cancelled,"
            "created_at,updated_at,dispatch_token,heartbeat_at) VALUES (?,'explain','running','running','{}','',0,?,?,?,NULL)",
            (user_id, db.utcnow(), db.utcnow(), "old-worker-token"),
        ).lastrowid
        queued = con.execute(
            "INSERT INTO ai_jobs(user_id,task,status,stage,request_json,partial_content,cancelled,"
            "created_at,updated_at,dispatch_token) VALUES (?,'explain','queued','queued','{}','',0,?,?,?)",
            (user_id, db.utcnow(), db.utcnow(), "committed-token"),
        ).lastrowid
    monkeypatch.setenv("REDIS_URL", "redis://unused:6379/0")
    ai.init_ai()
    with db.connection() as con:
        statuses = {
            row["id"]: row["status"]
            for row in con.execute("SELECT id,status FROM ai_jobs WHERE id IN (?,?)",
                                   (running, queued)).fetchall()
        }
    # 心跳超期的 running 任务（旧 Worker 宕机）被回收，不再永久锁死 MAX_ACTIVE
    assert statuses[running] == "interrupted"
    # 已提交派发的 queued 任务保留，由重放循环重新入队
    assert statuses[queued] == "queued"


def test_distributed_cancel_waits_for_worker(client, monkeypatch):
    user_id = client.get("/api/auth/me").json()["id"]
    job_id = _insert_job(user_id, "running", token="remote-token")
    monkeypatch.setattr(jobs, "redis_url", lambda: "redis://unused:6379/0")
    response = client.post(f"/api/ai/jobs/{job_id}/cancel")
    assert response.status_code == 200
    body = response.json()
    # Web 进程没有任务句柄：不能直接宣布取消完成，必须等 Worker 轮询落定
    assert body["status"] == "cancel_requested"
    assert body["cancelled"] == 1
    assert body["finished_at"] is None
    # Worker 确认后由 _finalize_cancel 落定
    assert jobs._finalize_cancel(job_id) is True
    assert client.get(f"/api/ai/jobs/{job_id}").json()["status"] == "cancelled"


def test_execute_dispatched_job_writes_lease_heartbeat(client, monkeypatch):
    user_id = client.get("/api/auth/me").json()["id"]
    job_id = _insert_job(user_id, "queued", token="lease-token")
    assert jobs.touch_job_heartbeat(job_id, "wrong-token") is False

    async def runner(queued_id, owner_id, data):
        jobs._job_update(queued_id, "completed", "completed", result={"content": "ok"})

    asyncio.run(jobs.execute_dispatched_job(job_id, "lease-token", runner))
    with db.connection() as con:
        row = con.execute("SELECT status, heartbeat_at FROM ai_jobs WHERE id=?", (job_id,)).fetchone()
    assert row["status"] == "completed"
    assert row["heartbeat_at"]


# ---------------------------------------------------------------------------
# 10. 拍题上传与打标预估
# ---------------------------------------------------------------------------

def test_estimate_tagging_matches_micro_batches(client):
    response = client.post("/api/admin/tagging/estimate", json={"subject": "物理", "limit": 10})
    assert response.status_code == 200, response.text
    data = response.json()
    expected = (data["will_process"] + 4) // 5
    assert data["estimated_calls"] == expected
    assert data["batches_of_5"] == expected
    assert data["questions_per_call"] == 5
    assert "每 5 题" in data["notice"]


def test_upload_normalizes_exif_and_compresses_large_image():
    from PIL import Image as PILImage

    # EXIF Orientation=6 的手机照片：必须转正
    source = PILImage.new("RGB", (600, 1200), "white")
    exif = source.getexif()
    exif[274] = 6
    buffer = BytesIO()
    source.save(buffer, "JPEG", exif=exif)
    normalized, mime, suffix = upload_pipeline._normalize_question_image(buffer.getvalue())
    assert mime == "image/jpeg" and suffix == ".jpg"
    with PILImage.open(BytesIO(normalized)) as image:
        assert image.size == (1200, 600)

    # 超过 2048px 的长边需要压缩
    large = PILImage.new("RGB", (3000, 1000), "white")
    buffer = BytesIO()
    large.save(buffer, "PNG")
    normalized, mime, suffix = upload_pipeline._normalize_question_image(buffer.getvalue())
    assert mime == "image/png"
    with PILImage.open(BytesIO(normalized)) as image:
        assert max(image.size) == 2048

    # 小图无 EXIF 时保留原始字节（不重复编码）
    tiny = PILImage.new("RGB", (20, 20), "white")
    buffer = BytesIO()
    tiny.save(buffer, "PNG")
    raw = buffer.getvalue()
    assert upload_pipeline._normalize_question_image(raw)[0] == raw


def test_upload_endpoint_serves_exif_normalized_preview(client):
    from PIL import Image as PILImage

    source = PILImage.new("RGB", (600, 1200), "white")
    exif = source.getexif()
    exif[274] = 6
    buffer = BytesIO()
    source.save(buffer, "JPEG", exif=exif)
    response = client.post("/api/uploads/question", files={
        "file": ("手机照片.jpg", buffer.getvalue(), "image/jpeg"),
    })
    assert response.status_code == 200, response.text
    preview = client.get(response.json()["preview_url"])
    assert preview.status_code == 200
    with PILImage.open(BytesIO(preview.content)) as image:
        assert image.size == (1200, 600)


def test_upload_rejects_total_payload_over_40mb(client, monkeypatch):
    big = b"x" * (12 * 1024 * 1024)

    async def fake_read_upload(_file):
        return "题目.png", big

    monkeypatch.setattr(upload_pipeline, "_read_upload", fake_read_upload)
    monkeypatch.setattr(upload_pipeline, "_valid_image", lambda raw: ("image/png", ".png"))
    monkeypatch.setattr(upload_pipeline, "_normalize_question_image", lambda raw: (raw, "image/png", ".png"))
    files = [("files", (f"p{index}.png", b"x", "image/png")) for index in range(4)]
    response = client.post("/api/uploads/question", files=files)
    assert response.status_code == 422
    assert "40 MB" in response.json()["detail"]


def test_upload_rejects_invalid_image_still(client):
    response = client.post("/api/uploads/question", files={
        "file": ("假.png", b"not an image", "image/png"),
    })
    assert response.status_code == 422
