"""小纸「分步提示」只读工具（``step_hint``）测试（本轮 M6）。

覆盖：
1. 工具出现在 ``GET /api/assistant/tools``，``side_effect`` / ``requires_confirmation`` / 参数契约正确；
2. 会话已存在时直接读取，不触发创建，且返回的锁定步骤**天然无正文**（递归断言，不允许补全）；
3. 无会话时调用 ``guided_hint.create_hint`` 幂等创建，并只回传公开视图 ``hint + steps``；
4. ``ref`` 缺失 / 超长 → 中文 ``ToolError``；题目不可用、隔离、格式错误、未知异常 → 统一中文
   ``ToolError``（不向模型抛 500）；
5. 系统提示包含分步提示规则（只引用已解锁步骤、不得编造锁定步骤、完整解析需提交尝试）。

AI 不参与本文件：只测服务端工具边界与文案，``guided_hint`` 的读/建行为全部 monkeypatch，
不访问真实题库、不联网。
"""
import pytest
from fastapi import HTTPException

from app import assistant as assistant_module


# ---------------------------------------------------------------------------
# 夹具 / 辅助
# ---------------------------------------------------------------------------

def _user_id(client) -> int:
    response = client.get("/api/auth/me")
    assert response.status_code == 200, response.text
    return response.json()["id"]


def _hint_view(question_ref_value: str = "bank:bank-q1") -> dict:
    """按 ``app/guided_hint.py::_hint_view`` 的真实口径造一份会话视图。

    锁定步骤（``locked: true``）只有 position/kind/title/requires_attempt，**没有 body**；
    这里刻意不伪造正文，测试工具层不得自行补全。
    """
    return {
        "hint": {
            "hint_id": 7,
            "question_ref": question_ref_value,
            "status": "active",
            "progress": {"total": 4, "unlocked": 2, "next_position": 2, "done": False},
            "source_note": (
                "基于原题题面与解析的规则分步提示（本轮未接 AI 生成）；完整解析仅在提交尝试后解锁。"
            ),
            "created_at": "2026-11-22T00:00:00+00:00",
            "updated_at": "2026-11-22T00:00:00+00:00",
        },
        "steps": [
            {
                "position": 0,
                "kind": "focus",
                "title": "审题：题目到底在问什么",
                "body": "题干片段\n先圈出已知条件与所求，再决定用哪个知识点。",
                "requires_attempt": False,
                "attempted": True,
                "unlocked_at": "2026-11-22T00:00:00+00:00",
            },
            {
                "position": 1,
                "kind": "concept",
                "title": "相关知识点",
                "body": "本题关联知识点：函数。先回忆定义/定理与适用条件，再回到题目。",
                "requires_attempt": True,
                "attempted": True,
                "unlocked_at": "2026-11-22T00:00:00+00:00",
            },
            {
                "position": 2,
                "kind": "first_step",
                "title": "第一步思路（来自原题解析首步）",
                "locked": True,
                "requires_attempt": True,
            },
            {
                "position": 3,
                "kind": "solution",
                "title": "完整解析与参考答案",
                "locked": True,
                "requires_attempt": True,
            },
        ],
    }


def _assert_locked_steps_have_no_body(node, path: str = "data") -> None:
    """递归断言：任何 ``locked: true`` 的节点都不得带 body / 正文类字段（不得补全锁定内容）。"""
    if isinstance(node, dict):
        if node.get("locked") is True:
            for forbidden in ("body", "solution", "answer", "answer_text", "content", "text"):
                assert forbidden not in node, "%s 的锁定步骤泄露了 %s" % (path, forbidden)
        for key, value in node.items():
            _assert_locked_steps_have_no_body(value, "%s.%s" % (path, key))
    elif isinstance(node, list):
        for index, item in enumerate(node):
            _assert_locked_steps_have_no_body(item, "%s[%s]" % (path, index))


def _raise(exc):
    def _raiser(*_args, **_kwargs):
        raise exc

    return _raiser


# ---------------------------------------------------------------------------
# 工具能力边界
# ---------------------------------------------------------------------------

def test_step_hint_exposed_in_tools_endpoint(client):
    response = client.get("/api/assistant/tools")
    assert response.status_code == 200, response.text
    body = response.json()
    by_name = {item["name"]: item for item in body["items"]}

    spec = by_name["step_hint"]
    assert spec["side_effect"] is False
    assert spec["requires_confirmation"] is False
    assert "分步提示" in spec["description"]
    params = {item["name"]: item for item in spec["params"]}
    assert set(params) == {"ref"}
    assert params["ref"]["type"] == "string"
    assert params["ref"]["required"] is True
    assert "bank:" in params["ref"]["description"]

    # 只读工具：不进入写操作白名单，且必须登记在只读处理器表里。
    assert "step_hint" not in body["write_tools"]
    assert "step_hint" in assistant_module.READ_TOOL_HANDLERS
    assert assistant_module.TOOL_BY_NAME["step_hint"]["side_effect"] is False
    # 既有工具列表保持完整（最小回归）。
    assert {"read_question", "web_search", "compose_paper"} <= set(by_name)


# ---------------------------------------------------------------------------
# 读取已有会话：只回传公开视图，锁定步骤不带正文
# ---------------------------------------------------------------------------

def test_step_hint_reads_existing_session_without_body_for_locked_steps(client, monkeypatch):
    from app import guided_hint

    user_id = _user_id(client)
    view = _hint_view()
    reads: list[tuple[int, str]] = []

    def fake_get_hint_by_ref(uid, ref):
        reads.append((uid, ref))
        return view

    monkeypatch.setattr(guided_hint, "get_hint_by_ref", fake_get_hint_by_ref)
    monkeypatch.setattr(
        guided_hint, "create_hint", _raise(AssertionError("已有会话时不应触发创建")),
    )

    result = assistant_module._tool_step_hint({"id": user_id}, {"ref": "  bank:bank-q1  "})

    assert reads == [(user_id, "bank:bank-q1")]
    assert result["ok"] is True
    assert result["tool"] == "step_hint"
    assert "2/4" in result["summary"]
    assert "提交尝试" in result["summary"]
    assert result["refs"] == [{"kind": "question", "ref": "bank:bank-q1"}]
    assert set(result["data"]) == {"hint", "steps"}
    assert result["data"]["hint"]["hint_id"] == 7
    # 工具不得裁剪/补全/改写服务端视图：原样回传。
    assert result["data"]["steps"] == view["steps"]
    # 递归断言：锁定步骤只保留标题，任何层级都没有 body 等正文。
    _assert_locked_steps_have_no_body(result)
    locked = [step for step in result["data"]["steps"] if step.get("locked")]
    assert [step["position"] for step in locked] == [2, 3]
    assert all("body" not in step for step in locked)
    assert "完整解析与参考答案" in [step["title"] for step in locked]


def test_step_hint_all_unlocked_summary_and_locked_absent(client, monkeypatch):
    from app import guided_hint

    user_id = _user_id(client)
    view = _hint_view()
    view["hint"]["progress"] = {"total": 4, "unlocked": 4, "next_position": None, "done": True}
    monkeypatch.setattr(guided_hint, "get_hint_by_ref", lambda uid, ref: view)
    monkeypatch.setattr(
        guided_hint, "create_hint", _raise(AssertionError("已有会话时不应触发创建")),
    )

    result = assistant_module._tool_step_hint({"id": user_id}, {"ref": "bank:bank-q1"})
    assert "4/4" in result["summary"]
    assert "完整解析" in result["summary"]
    assert "提交尝试" not in result["summary"]


# ---------------------------------------------------------------------------
# 无会话：幂等创建
# ---------------------------------------------------------------------------

def test_step_hint_creates_session_when_missing(client, monkeypatch):
    from app import guided_hint

    user_id = _user_id(client)
    created: list[tuple[int, str]] = []

    def fake_create_hint(uid, ref):
        created.append((uid, ref))
        view = _hint_view(question_ref_value=ref)
        view["replayed"] = False  # create_hint 的返回会带 replayed，工具不应把它塞给模型
        return view

    monkeypatch.setattr(
        guided_hint,
        "get_hint_by_ref",
        _raise(HTTPException(404, "该题还没有分步提示，请先创建")),
    )
    monkeypatch.setattr(guided_hint, "create_hint", fake_create_hint)

    result = assistant_module._tool_step_hint({"id": user_id}, {"ref": "app:12"})

    assert created == [(user_id, "app:12")]
    assert result["ok"] is True
    assert result["data"]["hint"]["question_ref"] == "app:12"
    assert result["refs"] == [{"kind": "question", "ref": "app:12"}]
    # 只回传公开视图 hint + steps，不泄露创建期元数据（replayed 等）。
    assert set(result["data"]) == {"hint", "steps"}
    _assert_locked_steps_have_no_body(result)


# ---------------------------------------------------------------------------
# 参数错误与异常 → 中文 ToolError（不 500）
# ---------------------------------------------------------------------------

def test_step_hint_requires_ref_and_rejects_overlong_ref(client):
    user_id = _user_id(client)
    with pytest.raises(assistant_module.ToolError) as missing:
        assistant_module._tool_step_hint({"id": user_id}, {})
    assert "ref" in str(missing.value)
    assert missing.value.status == 422

    with pytest.raises(assistant_module.ToolError) as too_long:
        assistant_module._tool_step_hint({"id": user_id}, {"ref": "x" * 161})
    assert "过长" in str(too_long.value)


def test_step_hint_turns_guided_hint_errors_into_chinese_tool_error(client, monkeypatch):
    from app import guided_hint

    user_id = _user_id(client)

    # 1) 已有会话读取时的参数/状态错误：保留原状态码，文案包上中文前缀。
    monkeypatch.setattr(
        guided_hint,
        "get_hint_by_ref",
        _raise(HTTPException(422, "题目引用格式不正确：题目引用不能为空")),
    )
    with pytest.raises(assistant_module.ToolError) as invalid_ref:
        assistant_module._tool_step_hint({"id": user_id}, {"ref": "??"})
    assert str(invalid_ref.value) == (
        "该题暂时无法生成逐步提示：题目引用格式不正确：题目引用不能为空"
    )
    assert invalid_ref.value.status == 422

    # 2) 无会话 → 创建失败（题目被隔离/不可练习）：409 转中文，不抛 HTTPException。
    monkeypatch.setattr(
        guided_hint,
        "get_hint_by_ref",
        _raise(HTTPException(404, "该题还没有分步提示，请先创建")),
    )
    monkeypatch.setattr(
        guided_hint,
        "create_hint",
        _raise(HTTPException(409, "该题当前不可用于练习（材料缺失或混入答案），暂不能生成分步提示")),
    )
    with pytest.raises(assistant_module.ToolError) as quarantined:
        assistant_module._tool_step_hint({"id": user_id}, {"ref": "bank:bank-q1"})
    assert str(quarantined.value).startswith("该题暂时无法生成逐步提示：")
    assert "不可用于练习" in str(quarantined.value)
    assert quarantined.value.status == 409

    # 3) 未知服务端异常：统一中文兜底，不把 500/堆栈抛给模型。
    monkeypatch.setattr(
        guided_hint,
        "get_hint_by_ref",
        _raise(RuntimeError("数据库连接中断")),
    )
    with pytest.raises(assistant_module.ToolError) as broken:
        assistant_module._tool_step_hint({"id": user_id}, {"ref": "bank:bank-q1"})
    assert str(broken.value) == "该题暂时无法生成逐步提示：服务暂时不可用，请稍后重试"


# ---------------------------------------------------------------------------
# 系统提示：分步提示规则（锁定步骤不得编造，完整解析需提交尝试）
# ---------------------------------------------------------------------------

def test_system_prompt_contains_step_hint_rule(client):
    prompt = assistant_module._system_prompt(
        {"username": "测试学生", "role": "student"},
        {"model": "test-model", "title": ""},
    )
    assert "step_hint" in prompt
    assert "分步提示（step_hint）只引用工具返回的已解锁步骤" in prompt
    assert "不得编造、补全或推测其正文" in prompt
    assert "完整解析须提示学生在题目页逐级提交尝试后" in prompt
    # 既有安全规则与输出协议不被破坏。
    assert "【安全与权限规则】" in prompt
    assert "【输出协议（必须严格遵守）】" in prompt
