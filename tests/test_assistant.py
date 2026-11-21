"""AI 助手（/api/assistant）测试：会话归属、任务准入、Agent 循环、协议重试、取消、工具权限与写操作确认。

AI 全部 mock（``monkeypatch.setattr(app.ai, "_call_model", fake)``），不访问外网；
每个用例使用 tests/conftest.py 的隔离数据目录与一次性账号。
"""
import asyncio
import json
import time

import pytest
from fastapi import HTTPException

from app import ai, db, practice
from app import assistant as assistant_module
from app.ai import jobs as ai_jobs
from test_ai import save_provider


# ---------------------------------------------------------------------------
# 夹具 / 辅助
# ---------------------------------------------------------------------------

def _user_id(client) -> int:
    response = client.get("/api/auth/me")
    assert response.status_code == 200, response.text
    return response.json()["id"]


def _bank_qids(subject: str, count: int = 2) -> list[str]:
    with practice._bank() as con:
        table = practice._bank_table(con, "questions")
        rows = con.execute(
            "SELECT qid FROM %s WHERE subject=? ORDER BY qid LIMIT ?" % table,
            (subject, count),
        ).fetchall()
    assert len(rows) >= count, "题库中 %s 题目不足" % subject
    return [row[0] for row in rows]


def _unlock(user_id: int, qid: str) -> None:
    with db.connection() as con:
        con.execute(
            "INSERT INTO attempts(user_id,qid,answer_text,result,grade_source,created_at) "
            "VALUES (?,?,?,?,?,?)",
            (user_id, qid, "A", "correct", "reference", db.utcnow()),
        )


def _insert_wrong(user_id: int, qid: str) -> None:
    now = db.utcnow()
    with db.connection() as con:
        con.execute(
            "INSERT INTO wrong_questions(user_id,qid,reason,error_tags_json,failures,streak,"
            "next_review_at,added_at,last_result,last_wrong_at,archived) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,0)",
            (user_id, qid, "答题错误", "[]", 1, 0, now, now, "incorrect", now),
        )


def _create_conversation(client, title: str | None = None) -> dict:
    payload = {"title": title} if title is not None else {}
    response = client.post("/api/assistant/conversations", json=payload)
    assert response.status_code == 200, response.text
    return response.json()["conversation"]


def _send(client, conversation_id: int, provider: dict, content: str = "帮我复盘错题", model: str = "test-model"):
    return client.post(
        "/api/assistant/conversations/%s/messages" % conversation_id,
        json={"content": content, "provider_id": provider["id"], "model": model},
    )


def _wait_job(client, job_id: int, timeout: float = 10.0) -> dict:
    deadline = time.time() + timeout
    body = None
    while time.time() < deadline:
        body = client.get("/api/ai/jobs/%s" % job_id).json()
        if body["status"] in ("completed", "failed", "interrupted", "cancelled"):
            return body
        time.sleep(0.05)
    raise AssertionError("任务未在 %ss 内结束: %s" % (timeout, body))


def _wait_status(client, conversation_id: int, statuses, timeout: float = 5.0) -> dict:
    deadline = time.time() + timeout
    body = None
    while time.time() < deadline:
        body = client.get("/api/assistant/conversations/%s" % conversation_id).json()
        if body["conversation"]["status"] in statuses:
            return body
        time.sleep(0.05)
    raise AssertionError("会话未进入 %s: %s" % (statuses, body))


class FakeModel:
    """按顺序返回预置 JSON 决策；记录每次调用的 prompt 供断言。"""

    def __init__(self, decisions, fallback: str | None = None):
        self.decisions = list(decisions)
        self.fallback = fallback
        self.calls: list[dict] = []

    async def __call__(self, provider, model, prompt, images=None, json_output=False,
                       token_limit=5000, **kwargs):
        self.calls.append(
            {
                "prompt": prompt,
                "json_output": json_output,
                "system_prompt": kwargs.get("system_prompt"),
                "model": model,
            }
        )
        if self.decisions:
            content = self.decisions.pop(0)
        elif self.fallback is not None:
            content = self.fallback
        else:
            content = json.dumps({"answer": "默认回答"}, ensure_ascii=False)
        if not isinstance(content, str):
            content = json.dumps(content, ensure_ascii=False)
        return {
            "content": content,
            "usage": {"prompt_tokens": 12, "completion_tokens": 6, "total_tokens": 18},
            "model": model,
        }


# ---------------------------------------------------------------------------
# 工具能力边界
# ---------------------------------------------------------------------------

def test_tools_endpoint_lists_whitelist_and_side_effects(client):
    response = client.get("/api/assistant/tools")
    assert response.status_code == 200, response.text
    body = response.json()
    by_name = {item["name"]: item for item in body["items"]}
    read_tools = {
        "search_source_bank", "read_question", "list_wrongs", "get_stats",
        "list_notes", "read_note", "list_knowledge_points", "list_mybank_questions",
    }
    write_tools = {"create_explain_job", "create_revision_paper", "compose_paper"}
    assert read_tools <= set(by_name)
    assert write_tools <= set(by_name)
    for name in read_tools:
        assert by_name[name]["side_effect"] is False
        assert by_name[name]["description"]
    for name in write_tools:
        assert by_name[name]["side_effect"] is True
        assert by_name[name]["requires_confirmation"] is True
    assert set(body["write_tools"]) == write_tools
    assert body["limits"]["max_steps"] == 6
    # 不暴露任意 HTTP / 命令 / 沙箱工具
    for name in by_name:
        assert not any(bad in name for bad in ("http", "request", "command", "shell", "sandbox", "file"))
    assert "不能访问外网" in body["scope_note"]


# ---------------------------------------------------------------------------
# 会话 CRUD 与归属
# ---------------------------------------------------------------------------

def test_conversation_crud_and_user_isolation(client, second_client):
    created = _create_conversation(client, "数学复习")
    assert created["status"] == "idle"
    conversation_id = created["id"]
    listing = client.get("/api/assistant/conversations").json()
    assert listing["total"] >= 1
    assert any(item["id"] == conversation_id for item in listing["items"])
    assert listing["items"][0]["message_count"] == 0

    # 他人不可见/不可改/不可删
    assert second_client.get("/api/assistant/conversations/%s" % conversation_id).status_code == 404
    assert second_client.patch(
        "/api/assistant/conversations/%s" % conversation_id, json={"title": "偷改名"}
    ).status_code == 404
    assert second_client.delete("/api/assistant/conversations/%s" % conversation_id).status_code == 404
    assert second_client.get("/api/assistant/conversations").json()["items"] == []

    renamed = client.patch(
        "/api/assistant/conversations/%s" % conversation_id, json={"title": "数学复习 · 二轮"}
    )
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["conversation"]["title"] == "数学复习 · 二轮"
    assert client.patch(
        "/api/assistant/conversations/%s" % conversation_id, json={"title": "   "}
    ).status_code == 422

    detail = client.get("/api/assistant/conversations/%s" % conversation_id).json()
    assert detail["messages"] == []
    assert detail["total"] == 0

    assert client.delete("/api/assistant/conversations/%s" % conversation_id).json()["deleted"] is True
    assert client.get("/api/assistant/conversations/%s" % conversation_id).status_code == 404


def test_message_pagination(client):
    provider = save_provider(client)
    conversation = _create_conversation(client)
    user_id = _user_id(client)
    for index in range(5):
        assistant_module._append_message(
            conversation["id"], user_id, "assistant",
            [{"type": "text", "text": "第%d条" % index}],
        )
    first = client.get(
        "/api/assistant/conversations/%s?page=1&page_size=2" % conversation["id"]
    ).json()
    assert [item["parts"][0]["text"] for item in first["messages"]] == ["第3条", "第4条"]
    assert first["total"] == 5 and first["has_more"] is True and first["page"] == 1
    oldest = client.get(
        "/api/assistant/conversations/%s?page=3&page_size=2" % conversation["id"]
    ).json()
    assert [item["parts"][0]["text"] for item in oldest["messages"]] == ["第0条"]
    assert oldest["has_more"] is False
    assert first["conversation"]["message_count"] == 5
    # 越界页返回空消息而不是 500
    empty = client.get(
        "/api/assistant/conversations/%s?page=9&page_size=2" % conversation["id"]
    ).json()
    assert empty["messages"] == []


# ---------------------------------------------------------------------------
# 消息 → 任务准入
# ---------------------------------------------------------------------------

def test_message_admission_checks_provider_ownership_and_content(client, second_client):
    conversation = _create_conversation(client)
    # 缺 provider_id：参数校验 422
    missing = client.post(
        "/api/assistant/conversations/%s/messages" % conversation["id"],
        json={"content": "你好", "model": "test-model"},
    )
    assert missing.status_code == 422
    # 未配置连接 / 连接不属于本人
    unconfigured = _send(client, conversation["id"], {"id": 9999})
    assert unconfigured.status_code in (403, 404, 422)
    provider = save_provider(client)
    other_provider = save_provider(second_client, name="他人连接")
    stolen = _send(client, conversation["id"], other_provider)
    assert stolen.status_code == 404
    # 空内容 422
    blank = client.post(
        "/api/assistant/conversations/%s/messages" % conversation["id"],
        json={"content": "   ", "provider_id": provider["id"], "model": "test-model"},
    )
    assert blank.status_code == 422
    # 他人会话：404（不泄露存在性）
    other_conversation = _create_conversation(second_client)
    foreign = client.post(
        "/api/assistant/conversations/%s/messages" % other_conversation["id"],
        json={"content": "你好", "provider_id": provider["id"], "model": "test-model"},
    )
    assert foreign.status_code == 404


def test_validate_rejects_foreign_conversation(client, second_client):
    provider = save_provider(client)
    other_conversation = _create_conversation(second_client)
    data = ai_jobs.JobInput(
        task="assistant-turn",
        provider_id=provider["id"],
        model="test-model",
        params={"conversation_id": other_conversation["id"], "message_id": 1},
    )
    with pytest.raises(HTTPException) as excinfo:
        assistant_module.validate_assistant_turn(
            data, {"id": _user_id(client), "role": "user"}
        )
    assert excinfo.value.status_code in (404, 422)


# ---------------------------------------------------------------------------
# Agent 循环：工具调用 → 观察 → 最终回答
# ---------------------------------------------------------------------------

def test_agent_loop_tool_then_answer(client, monkeypatch):
    provider = save_provider(client)
    conversation = _create_conversation(client)
    fake = FakeModel(
        [
            {"tool": "list_wrongs", "args": {"limit": 3}},
            {"answer": "## 复盘建议\n先复习错题本里的计算错误，再练 2 道同类题。"},
        ]
    )
    monkeypatch.setattr(ai, "_call_model", fake)
    sent = _send(client, conversation["id"], provider)
    assert sent.status_code == 200, sent.text
    body = sent.json()
    assert body["job"]["task"] == "assistant-turn"
    assert body["message"]["role"] == "user"
    job = _wait_job(client, body["job"]["id"])
    assert job["status"] == "completed", job
    result = job["result"]
    assert result["steps"] == 2
    assert result["tools"] == ["list_wrongs"]
    assert result["side_effect"] is False
    assert result["awaiting_confirmation"] is False
    assert result["usage"]["total_tokens"] == 36  # 两次模型调用的用量累计

    detail = _wait_status(client, conversation["id"], ("done",))
    assert detail["conversation"]["status"] == "done"
    assert detail["conversation"]["last_error"] is None
    roles = [item["role"] for item in detail["messages"]]
    assert roles == ["user", "tool", "assistant"]
    tool_message = detail["messages"][1]
    assert tool_message["tool_name"] == "list_wrongs"
    assert tool_message["tool_result"]["ok"] is True
    assistant_message = detail["messages"][2]
    assert "复盘建议" in assistant_message["parts"][0]["text"]

    # system prompt 含身份、权限与 JSON 协议；第二次调用把工具观察回灌给模型
    assert fake.calls[0]["json_output"] is True
    assert "知序助手" in fake.calls[0]["system_prompt"]
    assert "输出协议" in fake.calls[0]["system_prompt"]
    assert "list_wrongs" in fake.calls[1]["prompt"]
    assert "本轮工具观察" in fake.calls[1]["prompt"]


def test_protocol_violation_retries_once_then_succeeds(client, monkeypatch):
    provider = save_provider(client)
    conversation = _create_conversation(client)
    fake = FakeModel(["这不是 JSON", {"answer": "已理解你的问题。"}])
    monkeypatch.setattr(ai, "_call_model", fake)
    sent = _send(client, conversation["id"], provider)
    assert sent.status_code == 200, sent.text
    job = _wait_job(client, sent.json()["job"]["id"])
    assert job["status"] == "completed", job
    assert len(fake.calls) == 2
    assert "格式修正" in fake.calls[1]["prompt"]
    assert job["result"]["steps"] == 1


def test_protocol_violation_twice_fails_with_friendly_error(client, monkeypatch):
    provider = save_provider(client)
    conversation = _create_conversation(client)
    fake = FakeModel(["不是 JSON", "仍然不是 JSON"])
    monkeypatch.setattr(ai, "_call_model", fake)
    sent = _send(client, conversation["id"], provider)
    assert sent.status_code == 200, sent.text
    job = _wait_job(client, sent.json()["job"]["id"])
    assert job["status"] == "failed", job
    assert "格式" in (job["error"] or "")
    assert "Traceback" not in (job["error"] or "")
    detail = client.get("/api/assistant/conversations/%s" % conversation["id"]).json()
    assert detail["conversation"]["status"] == "failed"
    assert "格式" in (detail["conversation"]["last_error"] or "")
    # 失败不落 assistant 回答，只保留用户消息
    assert [item["role"] for item in detail["messages"]] == ["user"]


# ---------------------------------------------------------------------------
# 停止 / 取消
# ---------------------------------------------------------------------------

def test_stop_cancels_running_turn_and_blocks_parallel_message(client, monkeypatch):
    provider = save_provider(client)
    conversation = _create_conversation(client)

    async def slow_call(*_args, **_kwargs):
        await asyncio.sleep(30)
        return {"content": '{"answer":"太晚了"}'}

    monkeypatch.setattr(ai, "_call_model", slow_call)
    sent = _send(client, conversation["id"], provider)
    assert sent.status_code == 200, sent.text
    job_id = sent.json()["job"]["id"]

    # 等任务真正进入 running（期间会话状态是 queued/running）
    deadline = time.time() + 5
    while time.time() < deadline:
        status = client.get("/api/ai/jobs/%s" % job_id).json()["status"]
        if status == "running":
            break
        time.sleep(0.05)
    # 运行中不允许再发消息 / 删会话
    parallel = _send(client, conversation["id"], provider, content="再问一句")
    assert parallel.status_code == 409
    assert client.delete("/api/assistant/conversations/%s" % conversation["id"]).status_code == 409

    stopped = client.post("/api/assistant/conversations/%s/stop" % conversation["id"])
    assert stopped.status_code == 200, stopped.text
    assert stopped.json()["cancelled"] is True
    job = _wait_job(client, job_id)
    assert job["status"] == "cancelled", job
    detail = client.get("/api/assistant/conversations/%s" % conversation["id"]).json()
    assert detail["conversation"]["status"] == "idle"
    assert detail["conversation"]["pending_action"] is None
    system_messages = [item for item in detail["messages"] if item["role"] == "system"]
    assert any("停止" in item["parts"][0]["text"] for item in system_messages)

    # 没有进行中的任务时再停止：不报错，cancelled=false
    again = client.post("/api/assistant/conversations/%s/stop" % conversation["id"]).json()
    assert again["cancelled"] is False


# ---------------------------------------------------------------------------
# 工具权限：未解锁题不泄露答案、严格用户隔离
# ---------------------------------------------------------------------------

def test_read_question_gate_never_leaks_answer_before_attempt(client, second_client):
    user_id = _user_id(client)
    other_id = _user_id(second_client)
    qid = _bank_qids("数学", 2)[0]

    locked = assistant_module._tool_read_question({"id": user_id}, {"qid": qid})
    assert locked["ok"] is True
    assert locked["data"]["unlocked"] is False
    assert "answer" not in locked["data"] and "solution" not in locked["data"]
    assert any("未作答" in note for note in locked["data"]["notes"])

    _unlock(user_id, qid)
    unlocked = assistant_module._tool_read_question({"id": user_id}, {"qid": qid})
    assert unlocked["data"]["unlocked"] is True
    assert "answer" in unlocked["data"]

    # 其他账号不因他人作答而解锁
    other_view = assistant_module._tool_read_question({"id": other_id}, {"qid": qid})
    assert other_view["data"]["unlocked"] is False
    assert "answer" not in other_view["data"]

    # 不存在的题号：结构化 404（Agent 循环里会被转成工具错误给模型）
    with pytest.raises(HTTPException) as excinfo:
        assistant_module._tool_read_question({"id": user_id}, {"qid": "no-such-qid"})
    assert excinfo.value.status_code == 404


def test_wrongs_and_notes_are_isolated_per_user(client, second_client):
    user_id = _user_id(client)
    other_id = _user_id(second_client)
    qid = _bank_qids("物理", 2)[0]
    _insert_wrong(user_id, qid)

    own = assistant_module._tool_list_wrongs({"id": user_id}, {"limit": 5})
    assert any(item["qid"] == qid for item in own["data"]["items"])
    other = assistant_module._tool_list_wrongs({"id": other_id}, {"limit": 5})
    assert all(item["qid"] != qid for item in other["data"]["items"])
    assert other["data"]["total"] == 0

    now = db.utcnow()
    with db.connection() as con:
        private_note = con.execute(
            "INSERT INTO ai_notes(user_id,title,content,sources_json,origin,published,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (user_id, "私有笔记", "只有甲能看到的秘密内容", "[]", "private", 0, now, now),
        ).lastrowid
        official_note = con.execute(
            "INSERT INTO ai_notes(user_id,title,content,sources_json,origin,published,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (other_id, "官方清单", "官方公开内容", "[]", "official", 1, now, now),
        ).lastrowid
    assert "秘密内容" in assistant_module._tool_read_note({"id": user_id}, {"note_id": private_note})["data"]["content"]
    with pytest.raises(HTTPException):
        assistant_module._tool_read_note({"id": other_id}, {"note_id": private_note})
    # 官方已发布笔记本人与他人都可读
    assert "官方公开内容" in assistant_module._tool_read_note({"id": user_id}, {"note_id": official_note})["data"]["content"]

    listed = assistant_module._tool_list_notes({"id": user_id}, {"limit": 10})
    note_ids = {item["note_id"] for item in listed["data"]["items"]}
    assert private_note in note_ids and official_note in note_ids


def test_read_tools_reject_bad_arguments(client):
    user_id = _user_id(client)
    with pytest.raises(assistant_module.ToolError):
        assistant_module._tool_search_source_bank({"id": user_id}, {})
    with pytest.raises(assistant_module.ToolError):
        assistant_module._tool_read_question({"id": user_id}, {})
    stats = assistant_module._tool_get_stats({"id": user_id}, {})
    assert stats["ok"] is True
    assert "正确率" in stats["summary"]


# ---------------------------------------------------------------------------
# 写操作 HITL：只准备、要求确认、确认后才产生副作用
# ---------------------------------------------------------------------------

def _prepare_explain_write(client, monkeypatch):
    provider = save_provider(client)
    conversation = _create_conversation(client)
    user_id = _user_id(client)
    qid = _bank_qids("数学", 2)[0]
    _unlock(user_id, qid)
    fake = FakeModel([{"tool": "create_explain_job", "args": {"qid": qid}}], fallback="讲解正文")
    monkeypatch.setattr(ai, "_call_model", fake)
    sent = _send(client, conversation["id"], provider, content="给我讲讲这道题")
    assert sent.status_code == 200, sent.text
    job = _wait_job(client, sent.json()["job"]["id"])
    assert job["status"] == "completed", job
    detail = client.get("/api/assistant/conversations/%s" % conversation["id"]).json()
    return provider, conversation, qid, fake, job, detail


def test_write_tool_prepares_confirmation_without_side_effect(client, monkeypatch):
    provider, conversation, qid, fake, job, detail = _prepare_explain_write(client, monkeypatch)
    result = job["result"]
    assert result["awaiting_confirmation"] is True
    assert result["side_effect"] is True
    pending = result["pending_action"]
    assert pending["tool"] == "create_explain_job"
    assert pending["requires_confirmation"] is True and pending["side_effect"] is True
    assert pending["args"]["qid"] == qid
    assert detail["conversation"]["pending_action"]["id"] == pending["id"]
    assistant_message = detail["messages"][-1]
    assert assistant_message["role"] == "assistant"
    confirm_parts = [part for part in assistant_message["parts"] if part["type"] == "action_confirm"]
    assert confirm_parts and confirm_parts[0]["side_effect"] is True
    # 写操作只准备：本次只产生 assistant-turn 任务，没有真的创建 explain 任务
    job_tasks = [item["task"] for item in client.get("/api/ai/jobs").json()["items"]]
    assert "explain" not in job_tasks

    # 取消确认：不产生副作用
    cancelled = client.post(
        "/api/assistant/conversations/%s/confirm" % conversation["id"],
        json={"action_id": pending["id"], "confirm": False},
    )
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["confirmed"] is False
    assert cancelled.json()["side_effect"] is False
    after = client.get("/api/assistant/conversations/%s" % conversation["id"]).json()
    assert after["conversation"]["pending_action"] is None
    assert "explain" not in [item["task"] for item in client.get("/api/ai/jobs").json()["items"]]


def test_confirm_rejects_stale_action_id(client, monkeypatch):
    _provider, conversation, _qid, _fake, job, _detail = _prepare_explain_write(client, monkeypatch)
    pending = job["result"]["pending_action"]
    response = client.post(
        "/api/assistant/conversations/%s/confirm" % conversation["id"],
        json={"action_id": "stale-id", "confirm": True},
    )
    assert response.status_code == 409
    # 未确认的操作不能重复确认
    assert client.post(
        "/api/assistant/conversations/%s/confirm" % conversation["id"],
        json={"action_id": pending["id"], "confirm": False},
    ).status_code == 200
    assert client.post(
        "/api/assistant/conversations/%s/confirm" % conversation["id"],
        json={"action_id": pending["id"], "confirm": True},
    ).status_code == 409


def test_confirm_executes_write_action_creates_explain_job(client, monkeypatch):
    _provider, conversation, qid, _fake, job, _detail = _prepare_explain_write(client, monkeypatch)
    pending = job["result"]["pending_action"]
    response = client.post(
        "/api/assistant/conversations/%s/confirm" % conversation["id"],
        json={"action_id": pending["id"], "confirm": True},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["confirmed"] is True
    assert body["side_effect"] is True
    effect = body["effect"]
    assert effect["kind"] == "explain_job" and effect["job_id"]
    assert body["message"]["role"] == "assistant"
    side_effect_parts = [part for part in body["message"]["parts"] if part["type"] == "side_effect"]
    assert side_effect_parts and side_effect_parts[0]["side_effect"] is True
    explain_job = client.get("/api/ai/jobs/%s" % effect["job_id"]).json()
    assert explain_job["task"] == "explain"
    assert explain_job["request"]["qid"] == qid
    after = client.get("/api/assistant/conversations/%s" % conversation["id"]).json()
    assert after["conversation"]["pending_action"] is None


def test_paperlab_write_tools_are_guarded_until_ready(client, monkeypatch):
    provider = save_provider(client)
    conversation = _create_conversation(client)
    fake = FakeModel(
        [
            {"tool": "create_revision_paper", "args": {"mode": "replay"}},
            {"answer": "试卷工坊还没有就绪，我先把错题本入口给你。"},
        ]
    )
    monkeypatch.setattr(ai, "_call_model", fake)
    sent = _send(client, conversation["id"], provider, content="给我生成错题巩固卷")
    assert sent.status_code == 200, sent.text
    job = _wait_job(client, sent.json()["job"]["id"])
    assert job["status"] == "completed", job
    ready = assistant_module._paperlab_endpoint("/api/paperlab/revision") is not None
    if ready:
        # task-5 已合入：工具转为待确认写操作，同样不直接执行
        assert job["result"]["awaiting_confirmation"] is True
    else:
        assert job["result"]["awaiting_confirmation"] is False
        detail = client.get("/api/assistant/conversations/%s" % conversation["id"]).json()
        tool_messages = [item for item in detail["messages"] if item["role"] == "tool"]
        assert tool_messages
        tool_result = tool_messages[-1]["tool_result"]
        assert tool_result["ok"] is False
        assert tool_result.get("status") == 501
        assert "尚未就绪" in tool_result["error"]
        assert detail["messages"][-1]["role"] == "assistant"


# ---------------------------------------------------------------------------
# 试卷工坊桥接（task-5 合入后自动可用；未合入时相关用例跳过）
# ---------------------------------------------------------------------------

def test_paperlab_bridge_resolves_published_endpoints(client):
    revision = assistant_module._paperlab_endpoint("/api/paperlab/revision")
    compose = assistant_module._paperlab_endpoint("/api/paperlab/papers/compose")
    if revision is None or compose is None:
        pytest.skip("试卷工坊 P1（papers/revision）尚未合入")
    assert assistant_module._paperlab_body_model(revision) is not None
    assert assistant_module._paperlab_body_model(compose) is not None


def test_paperlab_write_tools_prepare_confirmation_with_real_endpoints(client):
    """桥接能按 task-5 的 Pydantic 请求体准备写操作，且仍不直接执行。"""
    user_id = _user_id(client)
    conversation = {"provider_id": 7, "model": "demo-model"}
    if assistant_module._paperlab_endpoint("/api/paperlab/revision") is None:
        pytest.skip("试卷工坊 P1 尚未合入")
    revision = assistant_module._prepare_paperlab_action(
        "create_revision_paper", {"id": user_id},
        {"mode": "replay", "count": 3}, conversation,
    )
    assert revision["requires_confirmation"] is True and revision["side_effect"] is True
    assert revision["pending"]["body"]["mode"] == "replay"
    assert revision["pending"]["body"]["provider_id"] == 7

    if assistant_module._paperlab_endpoint("/api/paperlab/papers/compose") is None:
        pytest.skip("试卷工坊组卷端点尚未合入")
    compose = assistant_module._prepare_paperlab_action(
        "compose_paper", {"id": user_id},
        {"subject": "数学", "mode": "smart", "count": 3}, conversation,
    )
    body = compose["pending"]["body"]
    assert body["subject"] == "数学" and body["mode"] == "smart"
    assert body["provider_id"] == 7 and body["model"] == "demo-model"
    assert "grade" not in body or isinstance(body["grade"], str)
