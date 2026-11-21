"""逐步辅导服务端状态机（``app/guided_hint.py``）的隔离测试。

本文件不依赖 ``app.main``：用本地 ``FastAPI()`` 只挂载 ``guided_hint.router``，通过
``app.dependency_overrides[current_user]`` 注入/切换当前账号，并用 ``isolated_db`` 把应用库
指到 ``tmp_path``（不读写真实应用库）。题库只读：多数用例 monkeypatch
``app.practice.load_question_ref`` 返回构造题目（题干/解析/隔离状态可控），另有一条用例
直接读真实只读题库，验证与 ``app/practice.py`` 的实际接线口径。

覆盖：幂等建表；创建步骤与 ``source_note``（focus 立即解锁、body 不含答案）；幂等创建
``replayed``；锁定步骤响应递归无 ``body``；未提交尝试时 advance 只提示不解锁且可重复调用；
提交尝试后 advance 解锁下一步；重复尝试幂等；未解锁位置 / 空文本 / 超长文本 422；
全部解锁后 ``completed``；跨用户 404 与两账号隔离；非法引用 422；隔离题 409、缺题干 422、
无解析时步骤数减少且不报错；知识点名称来自 ``question_kp JOIN knowledge_points`` 且最多 5 个；
公开函数直接调用与 ``con`` 参数复用调用方事务。
"""
import re

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app import db, guided_hint, practice
from app.auth import current_user

USER_A = {"id": 901, "username": "逐步提示甲", "role": "student"}
USER_B = {"id": 902, "username": "逐步提示乙", "role": "student"}
BASE = "/api/guided-hints"

#: 未解锁步骤视图的精确字段（无 body / attempted / unlocked_at）。
STEP_KEYS_LOCKED = {"position", "kind", "title", "locked", "requires_attempt"}
#: 已解锁步骤视图的精确字段。
STEP_KEYS_UNLOCKED = {
    "position", "kind", "title", "body", "requires_attempt", "attempted", "unlocked_at",
}

STEM = "已知函数 f(x)=x^2+1，求 f(2) 的值，并说明所用的代入方法。"
SOLUTION = "解析：把 x=2 代入函数解析式 f(x)=x^2+1。\n得到 f(2)=5，所以函数值为 5。\n答案：5"
ANSWER = "5"


# ---------------------------------------------------------------------------
# 夹具与工具
# ---------------------------------------------------------------------------


@pytest.fixture
def hint_env(isolated_db):
    """隔离应用库 + 基础表 + 逐步辅导三张表（重复建表幂等）。"""
    db.init_db()
    guided_hint.init_guided_hints()
    return isolated_db


def _make_client(holder) -> TestClient:
    """只挂逐步辅导 router 的小应用；当前用户由 ``holder["user"]`` 决定。"""
    app = FastAPI()
    app.include_router(guided_hint.router)
    app.dependency_overrides[current_user] = lambda: holder["user"]
    return TestClient(app)


@pytest.fixture
def hint_api(hint_env):
    """返回 ``(TestClient, holder)``；``holder["user"]`` 可在测试内切换账号。"""
    holder = {"user": dict(USER_A)}
    client = _make_client(holder)
    try:
        yield client, holder
    finally:
        client.close()


def _switch(holder, user) -> None:
    holder["user"] = dict(user)


def _make_question(qid="math-step-1", **overrides) -> dict:
    question = {
        "qid": qid,
        "ref": f"bank:{qid}",
        "subject": "数学",
        "stem_plain": STEM,
        "solution": SOLUTION,
        "answer_text": ANSWER,
        "has_answer": True,
        "has_solution": True,
        "quarantined": False,
        "practice_allowed": True,
    }
    question.update(overrides)
    return question


def _patch_loader(monkeypatch, question) -> dict:
    """把题库读取替换成构造题目；返回调用记录（断言传入的 ref / user_id）。"""
    calls: dict = {}

    def fake_load(ref, user_id):
        calls["ref"] = ref
        calls["user_id"] = user_id
        if callable(question):
            return question(ref, user_id)
        return dict(question)

    monkeypatch.setattr(practice, "load_question_ref", fake_load)
    return calls


def _patch_loader_error(monkeypatch, error: HTTPException) -> None:
    def fake_load(ref, user_id):
        raise error

    monkeypatch.setattr(practice, "load_question_ref", fake_load)


def _create(client, ref="math-step-1"):
    return client.post(BASE, json={"ref": ref})


def _created(client, ref="math-step-1") -> dict:
    response = _create(client, ref)
    assert response.status_code == 200, response.text
    return response.json()


def _walk(node):
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk(item)


def _assert_locked_steps_have_no_body(payload) -> None:
    """递归断言：锁定步骤只含位置/类型/标题，绝无 body 等正文内容。"""
    for node in _walk(payload):
        if node.get("locked") is True:
            assert "body" not in node
            assert set(node) == STEP_KEYS_LOCKED


def _assert_chinese_detail(response) -> None:
    detail = response.json().get("detail")
    assert isinstance(detail, str) and detail
    assert re.search(r"[\u4e00-\u9fff]", detail)


def _attempt(client, hint_id, position, text):
    return client.post(f"{BASE}/{hint_id}/attempt", json={"step_position": position, "text": text})


def _advance(client, hint_id, **body):
    return client.post(f"{BASE}/{hint_id}/advance", json=body)


def _db_rows(sql, params=()):
    with db.connection() as con:
        return con.execute(sql, params).fetchall()


def returned_body(step: dict, payload: dict) -> str:
    """测试辅助：锁定步骤的 body 不在响应里，只能从数据库读取后断言生成内容。"""
    assert step.get("locked") is True
    rows = _db_rows(
        "SELECT body FROM guided_steps WHERE hint_id=? AND position=?",
        (payload["hint"]["hint_id"], step["position"]),
    )
    assert rows, "步骤应已落库"
    return str(rows[0][0])


# ---------------------------------------------------------------------------
# 建表
# ---------------------------------------------------------------------------


def test_init_guided_hints_is_idempotent(hint_env):
    """重复 init 不报错，三张表与关键列齐全。"""
    guided_hint.init_guided_hints()
    guided_hint.init_guided_hints()
    with db.connection() as con:
        tables = db.table_names(con)
        assert {"guided_hints", "guided_steps", "guided_attempts"} <= tables
        hint_columns = {row[1] for row in con.execute("PRAGMA table_info(guided_hints)")}
        step_columns = {row[1] for row in con.execute("PRAGMA table_info(guided_steps)")}
        attempt_columns = {row[1] for row in con.execute("PRAGMA table_info(guided_attempts)")}
    assert {
        "id", "user_id", "question_ref", "storage_ref", "status", "source_note",
        "created_at", "updated_at",
    } <= hint_columns
    assert {
        "id", "hint_id", "position", "kind", "title", "body", "requires_attempt",
        "unlocked_at", "created_at",
    } <= step_columns
    assert {"id", "hint_id", "user_id", "step_position", "text", "created_at"} <= attempt_columns


# ---------------------------------------------------------------------------
# 创建：步骤生成口径
# ---------------------------------------------------------------------------


def test_create_generates_rule_steps_and_source_note(hint_api, monkeypatch):
    client, _holder = hint_api
    calls = _patch_loader(monkeypatch, _make_question())
    payload = _created(client)

    hint = payload["hint"]
    assert payload["replayed"] is False
    assert hint["question_ref"] == "bank:math-step-1"
    assert hint["status"] == "active"
    assert hint["source_note"] == guided_hint.SOURCE_NOTE
    assert "规则分步提示" in hint["source_note"] and "未接 AI" in hint["source_note"]
    assert hint["progress"] == {
        "total": 4, "unlocked": 1, "next_position": 1, "done": False,
    }
    # 调用方传的是本人 user_id 与规范化引用。
    assert calls == {"ref": "bank:math-step-1", "user_id": USER_A["id"]}

    steps = payload["steps"]
    assert [step["position"] for step in steps] == [0, 1, 2, 3]
    assert [step["kind"] for step in steps] == ["focus", "concept", "first_step", "solution"]
    assert [step["title"] for step in steps] == [
        "审题：题目到底在问什么",
        "相关知识点",
        "第一步思路（来自原题解析首步）",
        "完整解析与参考答案",
    ]

    focus = steps[0]
    assert focus["requires_attempt"] is False
    assert focus["attempted"] is False
    assert focus["unlocked_at"]
    assert focus["body"].startswith(STEM[:20])
    assert "先圈出已知条件与所求，再决定用哪个知识点。" in focus["body"]
    # 审题提示只能来自题面，不能提前泄露答案。
    assert ANSWER not in focus["body"]

    # 未解锁步骤的 body 不在响应里，只能从库里核对生成内容。
    # 无知识点关联时如实说明样本不足，不编造知识点。
    concept_body = returned_body(steps[1], payload)
    assert "题库暂无本题的知识点关联（样本不足）" in concept_body
    assert steps[1]["requires_attempt"] is True

    # 首步提示剔除了「解析：/答案：」标记行，只保留推导首步。
    first_step_body = returned_body(steps[2], payload)
    assert "该步来自原题解析的首步，仍需自己完成后续推导。" in first_step_body
    assert "把 x=2 代入" not in first_step_body
    assert "答案：" not in first_step_body

    # 完整解析附带参考答案，且它在初始响应里必须锁定（无 body）。
    assert "参考答案：" + ANSWER in returned_body(steps[3], payload)
    assert steps[3]["locked"] is True
    _assert_locked_steps_have_no_body(payload)

    # 单条读取与按引用读取返回同一份安全视图（锁定步骤同样无 body）。
    detail = client.get(f"{BASE}/{hint['hint_id']}")
    assert detail.status_code == 200, detail.text
    assert detail.json()["hint"]["hint_id"] == hint["hint_id"]
    _assert_locked_steps_have_no_body(detail.json())
    by_ref = client.get(f"{BASE}/by-ref", params={"ref": "math-step-1"})
    assert by_ref.status_code == 200, by_ref.text
    assert by_ref.json()["steps"] == detail.json()["steps"]


def test_create_is_idempotent_and_replayed(hint_api, monkeypatch):
    client, _holder = hint_api
    _patch_loader(monkeypatch, _make_question())
    first = _created(client)
    second = _created(client)

    assert second["replayed"] is True
    assert second["hint"]["hint_id"] == first["hint"]["hint_id"]
    assert len(second["steps"]) == len(first["steps"])
    hint_id = first["hint"]["hint_id"]
    assert len(_db_rows("SELECT id FROM guided_hints WHERE user_id=?", (USER_A["id"],))) == 1
    assert len(_db_rows("SELECT id FROM guided_steps WHERE hint_id=?", (hint_id,))) == 4

    by_ref = client.get(f"{BASE}/by-ref", params={"ref": "bank:math-step-1"})
    assert by_ref.status_code == 200, by_ref.text
    assert by_ref.json()["hint"]["hint_id"] == hint_id
    # 裸 qid 与 bank: 前缀归一到同一会话（storage_ref 口径）。
    prefixed = _created(client, ref="bank:math-step-1")
    assert prefixed["replayed"] is True
    assert prefixed["hint"]["hint_id"] == hint_id
    # 列表读取同样只给锁定步骤的安全视图。
    listed = client.get(BASE).json()
    assert listed["total"] == 1
    assert listed["items"][0]["hint"]["hint_id"] == hint_id
    _assert_locked_steps_have_no_body(listed)


# ---------------------------------------------------------------------------
# 解锁状态机
# ---------------------------------------------------------------------------


def test_advance_without_attempt_requires_attempt_and_does_not_unlock(hint_api, monkeypatch):
    client, _holder = hint_api
    _patch_loader(monkeypatch, _make_question())
    hint_id = _created(client)["hint"]["hint_id"]

    first = _advance(client, hint_id)
    assert first.status_code == 200, first.text
    payload = first.json()
    assert payload["requires_attempt"] is True
    assert payload["advanced"] is False
    assert payload["completed"] is False
    assert payload["unlocked"] is None
    assert isinstance(payload["message"], str) and payload["message"]
    assert payload["hint"]["progress"]["unlocked"] == 1
    assert payload["steps"][1]["locked"] is True
    _assert_locked_steps_have_no_body(payload)

    # 未提交尝试时重复调用：不报错、不重复解锁（幂等）。
    repeated = _advance(client, hint_id).json()
    assert repeated["requires_attempt"] is True
    assert repeated["hint"]["progress"]["unlocked"] == 1
    assert len(_db_rows(
        "SELECT id FROM guided_steps WHERE hint_id=? AND unlocked_at IS NOT NULL", (hint_id,)
    )) == 1


def test_attempt_then_advance_unlocks_next_step(hint_api, monkeypatch):
    client, _holder = hint_api
    _patch_loader(monkeypatch, _make_question())
    hint_id = _created(client)["hint"]["hint_id"]

    attempt = _attempt(client, hint_id, 0, "先把 x=2 代入函数解析式。")
    assert attempt.status_code == 200, attempt.text
    body = attempt.json()
    assert body["replayed"] is False
    assert body["attempt"] == {
        "attempt_id": body["attempt"]["attempt_id"],
        "step_position": 0,
        "text": "先把 x=2 代入函数解析式。",
        "created_at": body["attempt"]["created_at"],
    }
    assert body["steps"][0]["attempted"] is True
    _assert_locked_steps_have_no_body(body)

    advanced = _advance(client, hint_id).json()
    assert advanced["advanced"] is True
    assert advanced["advanced_position"] == 1
    assert advanced["requires_attempt"] is False
    assert advanced["hint"]["progress"]["unlocked"] == 2
    assert advanced["unlocked"]["position"] == 1
    assert advanced["unlocked"]["kind"] == "concept"
    assert advanced["unlocked"]["body"] == advanced["steps"][1]["body"]
    _assert_locked_steps_have_no_body(advanced)
    fetched = client.get(f"{BASE}/{hint_id}").json()
    assert fetched["hint"]["progress"]["unlocked"] == 2
    _assert_locked_steps_have_no_body(fetched)
    concept = advanced["steps"][1]
    assert "locked" not in concept
    assert set(concept) == STEP_KEYS_UNLOCKED
    assert concept["body"]
    assert concept["unlocked_at"]
    assert advanced["steps"][2]["locked"] is True
    _assert_locked_steps_have_no_body(advanced)


def test_advance_can_submit_attempt_in_one_call(hint_api, monkeypatch):
    client, _holder = hint_api
    _patch_loader(monkeypatch, _make_question())
    hint_id = _created(client)["hint"]["hint_id"]

    payload = _advance(client, hint_id, attempt="一次调用里提交尝试并解锁。").json()
    assert payload["advanced"] is True
    assert payload["advanced_position"] == 1
    assert payload["unlocked"]["position"] == 1
    assert payload["attempt"]["step_position"] == 0
    assert payload["attempt"]["text"] == "一次调用里提交尝试并解锁。"
    assert payload["replayed"] is False
    assert len(_db_rows("SELECT id FROM guided_attempts WHERE hint_id=?", (hint_id,))) == 1


def test_app_ref_keeps_prefixed_storage_key(hint_api, monkeypatch):
    """个人题引用（app:<id>）带前缀写库，question_ref 保存规范化引用。"""
    client, _holder = hint_api
    calls = _patch_loader(monkeypatch, _make_question(qid="app:12"))
    payload = _created(client, ref="app:12")
    assert payload["hint"]["question_ref"] == "app:12"
    assert calls["ref"] == "app:12"
    rows = _db_rows(
        "SELECT question_ref, storage_ref FROM guided_hints WHERE id=?",
        (payload["hint"]["hint_id"],),
    )
    assert (rows[0][0], rows[0][1]) == ("app:12", "app:12")
    assert _created(client, ref="app:12")["replayed"] is True
    # app 引用必须是正整数 id。
    assert _create(client, ref="app:0").status_code == 422


def test_advance_body_is_optional_and_invalid_attempt_is_rejected(hint_api, monkeypatch):
    client, _holder = hint_api
    _patch_loader(monkeypatch, _make_question())
    hint_id = _created(client)["hint"]["hint_id"]

    # advance 的请求体可选：不带 body 时按「未提交尝试」处理。
    bare = client.post(f"{BASE}/{hint_id}/advance")
    assert bare.status_code == 200, bare.text
    assert bare.json()["requires_attempt"] is True
    assert bare.json()["advanced"] is False

    # advance 附带非法尝试文本：先校验再写，422 且不产生尝试记录。
    for text in ("", "   ", "长" * (guided_hint.MAX_ATTEMPT_LENGTH + 1)):
        response = _advance(client, hint_id, attempt=text)
        assert response.status_code == 422, text
        _assert_chinese_detail(response)
    assert len(_db_rows("SELECT id FROM guided_attempts WHERE hint_id=?", (hint_id,))) == 0

    # attempt 接口缺少 body 同样 422。
    assert client.post(f"{BASE}/{hint_id}/attempt").status_code == 422


def test_duplicate_attempt_is_replayed(hint_api, monkeypatch):
    client, _holder = hint_api
    _patch_loader(monkeypatch, _make_question())
    hint_id = _created(client)["hint"]["hint_id"]

    first = _attempt(client, hint_id, 0, "  第一次思路  ").json()
    # 文本首尾空白被规范化掉后再落库。
    assert first["attempt"]["text"] == "第一次思路"
    second_response = _attempt(client, hint_id, 0, "第二次思路")
    assert second_response.status_code == 200, second_response.text
    second = second_response.json()
    assert second["replayed"] is True
    assert second["attempt"]["attempt_id"] == first["attempt"]["attempt_id"]
    # 重复提交不覆盖首次尝试。
    assert second["attempt"]["text"] == "第一次思路"
    assert len(_db_rows("SELECT id FROM guided_attempts WHERE hint_id=?", (hint_id,))) == 1


def test_attempt_on_locked_or_missing_step_is_422(hint_api, monkeypatch):
    client, _holder = hint_api
    _patch_loader(monkeypatch, _make_question())
    hint_id = _created(client)["hint"]["hint_id"]

    locked = _attempt(client, hint_id, 1, "还没解锁就写")
    assert locked.status_code == 422
    _assert_chinese_detail(locked)
    assert "尚未解锁" in locked.json()["detail"]

    missing = _attempt(client, hint_id, 9, "不存在的步骤")
    assert missing.status_code == 422
    assert "步骤位置不存在" in missing.json()["detail"]

    negative = _attempt(client, hint_id, -1, "负数位置")
    assert negative.status_code == 422
    assert len(_db_rows("SELECT id FROM guided_attempts WHERE hint_id=?", (hint_id,))) == 0


def test_attempt_text_validation(hint_api, monkeypatch):
    client, _holder = hint_api
    _patch_loader(monkeypatch, _make_question())
    hint_id = _created(client)["hint"]["hint_id"]

    for text in ("", "   "):
        response = _attempt(client, hint_id, 0, text)
        assert response.status_code == 422
        _assert_chinese_detail(response)
        assert "尝试内容不能为空" in response.json()["detail"]

    too_long = _attempt(client, hint_id, 0, "长" * (guided_hint.MAX_ATTEMPT_LENGTH + 1))
    assert too_long.status_code == 422
    assert "最多 2000 字" in too_long.json()["detail"]

    # 缺少 text 字段同样 422，且不落库。
    missing = client.post(f"{BASE}/{hint_id}/attempt", json={"step_position": 0})
    assert missing.status_code == 422
    assert len(_db_rows("SELECT id FROM guided_attempts WHERE hint_id=?", (hint_id,))) == 0

    # 边界：恰好 2000 字允许提交。
    accepted = _attempt(client, hint_id, 0, "边" * guided_hint.MAX_ATTEMPT_LENGTH)
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["attempt"]["text"] == "边" * guided_hint.MAX_ATTEMPT_LENGTH


def test_full_unlock_sets_completed(hint_api, monkeypatch):
    client, _holder = hint_api
    _patch_loader(monkeypatch, _make_question())
    hint_id = _created(client)["hint"]["hint_id"]

    for position in range(3):
        attempt = _attempt(client, hint_id, position, f"第 {position} 步的尝试。")
        assert attempt.status_code == 200, attempt.text
        payload = _advance(client, hint_id).json()
        assert payload["advanced"] is True
        assert payload["hint"]["progress"]["unlocked"] == position + 2
        _assert_locked_steps_have_no_body(payload)

    assert payload["completed"] is True
    hint = payload["hint"]
    assert hint["status"] == "completed"
    assert hint["progress"] == {"total": 4, "unlocked": 4, "next_position": None, "done": True}
    # 最后解锁的是完整解析步骤，advance 直接返回该步视图。
    assert payload["unlocked"]["position"] == 3
    assert payload["unlocked"]["kind"] == "solution"
    # 解锁完毕后再查：完整解析可见。
    assert payload["steps"][3]["kind"] == "solution"
    assert "参考答案：" + ANSWER in payload["steps"][3]["body"]

    # 已全部解锁后重复 advance：返回 completed，不报错、不重复写入。
    again = _advance(client, hint_id)
    assert again.status_code == 200, again.text
    assert again.json()["completed"] is True
    assert again.json()["advanced"] is False
    assert client.get(f"{BASE}/{hint_id}").json()["hint"]["status"] == "completed"


# ---------------------------------------------------------------------------
# 错误口径与题目状态
# ---------------------------------------------------------------------------


def test_other_user_gets_404_and_accounts_are_isolated(hint_api, monkeypatch):
    client, holder = hint_api
    _patch_loader(monkeypatch, _make_question())
    created = _created(client)
    hint_id = created["hint"]["hint_id"]
    _attempt(client, hint_id, 0, "甲同学的尝试")
    assert _advance(client, hint_id).json()["advanced"] is True

    _switch(holder, USER_B)
    assert client.get(f"{BASE}/{hint_id}").status_code == 404
    assert client.get(f"{BASE}/by-ref", params={"ref": "math-step-1"}).status_code == 404
    assert _advance(client, hint_id).status_code == 404
    assert _attempt(client, hint_id, 0, "乙同学不该写进别人的会话").status_code == 404
    listed = client.get(BASE).json()
    assert listed["total"] == 0 and listed["items"] == []

    # 不存在与不属于本人是同一 404 文案（不泄露存在性）。
    unknown = client.get(f"{BASE}/987654321")
    assert unknown.status_code == 404
    assert unknown.json()["detail"] == client.get(f"{BASE}/{hint_id}").json()["detail"]

    # 同一道题两个账号各有一份独立会话，互不影响。
    other = _created(client)
    assert other["hint"]["hint_id"] != hint_id
    assert other["hint"]["progress"]["unlocked"] == 1
    assert other["steps"][0]["attempted"] is False

    _switch(holder, USER_A)
    mine = client.get(f"{BASE}/{hint_id}").json()
    assert mine["hint"]["progress"]["unlocked"] == 2
    assert mine["steps"][0]["attempted"] is True
    assert mine["hint"]["hint_id"] == hint_id


def test_invalid_ref_is_422(hint_api, monkeypatch):
    client, _holder = hint_api
    _patch_loader(monkeypatch, _make_question())

    for ref in ("", "   ", "has space", "x" * (guided_hint.question_ref.MAX_REF_LENGTH + 1)):
        response = client.post(BASE, json={"ref": ref})
        assert response.status_code == 422, ref
        _assert_chinese_detail(response)
    # 缺少 ref 字段、以及 by-ref 的非法引用同样 422。
    assert client.post(BASE, json={}).status_code == 422
    assert client.get(f"{BASE}/by-ref", params={"ref": "bad ref"}).status_code == 422


def test_quarantined_and_empty_stem_are_rejected(hint_api, monkeypatch):
    client, _holder = hint_api
    _patch_loader(monkeypatch, _make_question(quarantined=True, practice_allowed=False))
    quarantined = _create(client)
    assert quarantined.status_code == 409
    _assert_chinese_detail(quarantined)
    assert "不可用于练习" in quarantined.json()["detail"]

    _patch_loader(monkeypatch, _make_question(practice_allowed=False))
    assert _create(client).status_code == 409

    _patch_loader(monkeypatch, _make_question(stem_plain=""))
    empty_stem = _create(client)
    assert empty_stem.status_code == 422
    assert "缺少题干" in empty_stem.json()["detail"]

    _patch_loader_error(monkeypatch, HTTPException(404, "题目不存在"))
    assert _create(client).status_code == 404
    _patch_loader_error(monkeypatch, HTTPException(503, "未找到题库，请检查 STUDY_BANK_DIR 配置"))
    unavailable = _create(client)
    assert unavailable.status_code == 503
    _assert_chinese_detail(unavailable)

    # 以上失败都不应留下会话。
    assert _db_rows("SELECT id FROM guided_hints WHERE user_id=?", (USER_A["id"],)) == []


def test_question_without_solution_generates_fewer_steps(hint_api, monkeypatch):
    client, _holder = hint_api
    _patch_loader(monkeypatch, _make_question(solution="", has_solution=False))
    payload = _created(client, ref="math-no-solution")

    hint = payload["hint"]
    kinds = [step["kind"] for step in payload["steps"]]
    assert hint["progress"]["total"] == 3
    assert len(payload["steps"]) == 3
    assert kinds == ["focus", "concept", "concept"]
    assert "first_step" not in kinds and "solution" not in kinds
    assert "本题题库未附解析" in returned_body(payload["steps"][2], payload)
    _assert_locked_steps_have_no_body(payload)

    # 没有解析也能正常走完解锁流程（不报错）。
    for position in range(2):
        assert _attempt(client, hint["hint_id"], position, f"自写推导第 {position} 步").status_code == 200
        advanced = _advance(client, hint["hint_id"]).json()
    assert advanced["completed"] is True
    assert advanced["hint"]["status"] == "completed"


def test_knowledge_points_are_listed_and_limited(hint_api, monkeypatch):
    client, _holder = hint_api
    qid = "math-kp-question"
    _patch_loader(monkeypatch, _make_question(qid=qid))

    names = ["函数定义", "代入法", "二次函数", "符号运算", "方程思想", "排除法"]
    kp_ids = []
    with db.connection() as con:
        for name in names:
            cursor = con.execute(
                "INSERT INTO knowledge_points(subject, name) VALUES (?, ?)", ("数学", name),
            )
            kp_ids.append(int(cursor.lastrowid))
        for index, kp_id in enumerate(kp_ids):
            con.execute(
                "INSERT INTO question_kp(qid, kp_id, weight, source, created_at) "
                "VALUES (?,?,?,?,?)",
                (qid, kp_id, float(len(kp_ids) - index), "admin", db.utcnow()),
            )

    payload = _created(client, ref=qid)
    concept = payload["steps"][1]
    assert concept["kind"] == "concept" and concept["locked"] is True
    concept_body = returned_body(concept, payload)
    assert "本题关联知识点：" in concept_body
    listed = concept_body.split("本题关联知识点：", 1)[1].split("。", 1)[0].split("、")
    assert listed == names[: guided_hint.MAX_KP_NAMES]
    assert "排除法" not in concept_body


# ---------------------------------------------------------------------------
# 公开函数与 con 语义
# ---------------------------------------------------------------------------


def test_public_functions_reuse_caller_transaction(hint_env, monkeypatch):
    """``con`` 传入时只在调用方事务里执行：提交前其他连接不可见，提交后可见。"""
    _patch_loader(monkeypatch, _make_question())
    with db.connection() as con:
        created = guided_hint.create_hint(USER_A["id"], "math-con-1", con=con)
        hint_id = created["hint"]["hint_id"]
        assert created["replayed"] is False
        assert con.execute("SELECT COUNT(*) FROM guided_hints").fetchone()[0] == 1
        assert guided_hint.create_hint(USER_A["id"], "math-con-1", con=con)["replayed"] is True

    assert guided_hint.get_hint(USER_A["id"], hint_id)["hint"]["hint_id"] == hint_id
    assert guided_hint.get_hint_by_ref(USER_A["id"], "bank:math-con-1")["hint"]["hint_id"] == hint_id
    advanced = guided_hint.advance_hint(USER_A["id"], hint_id, attempt="函数里提交尝试")
    assert advanced["advanced"] is True
    attempt = guided_hint.record_attempt(
        USER_A["id"], hint_id, step_position=1, text="第二步尝试",
    )
    assert attempt["attempt"]["step_position"] == 1
    assert guided_hint.list_hints(USER_A["id"], limit=5)["total"] == 1
    with pytest.raises(HTTPException) as excinfo:
        guided_hint.record_attempt(USER_A["id"], hint_id, step_position=3, text="尚未解锁")
    assert excinfo.value.status_code == 422


def test_routes_require_login(hint_env):
    app = FastAPI()
    app.include_router(guided_hint.router)
    client = TestClient(app)
    try:
        response = client.post(BASE, json={"ref": "math-step-1"})
        assert response.status_code == 401
        assert response.json()["detail"] == "请先登录"
    finally:
        client.close()


# ---------------------------------------------------------------------------
# 真实只读题库接线
# ---------------------------------------------------------------------------


def _real_bank_question() -> dict | None:
    """从只读题库取一道可练习、有解析、题干非空的数学题（供真实接线用例）。"""
    from app.practice import _bank, load_question

    try:
        with _bank() as con:
            columns = {row[1] for row in con.execute("PRAGMA table_info(questions)")}
            if "solution" not in columns:
                return None
            rows = con.execute(
                "SELECT qid FROM questions WHERE subject=? "
                "AND LENGTH(COALESCE(solution,''))>=120 LIMIT 40",
                ("数学",),
            ).fetchall()
    except Exception:  # noqa: BLE001 - 题库缺失/不可读时跳过该用例
        return None
    for row in rows:
        try:
            question = load_question(row[0])
        except Exception:  # noqa: BLE001 - 单题读取失败换下一道
            continue
        stem = str(question.get("stem_plain") or "").strip()
        solution = str(question.get("solution") or "").strip()
        answer = str(question.get("answer_text") or "").strip()
        if not question.get("practice_allowed") or not stem or not solution:
            continue
        if answer and answer in stem[:300]:
            continue
        return question
    return None


def test_real_bank_question_is_wired_to_practice(hint_env):
    """真实题库只读接线：规范化引用 / storage_ref 口径与 practice.load_question_ref 一致。"""
    question = _real_bank_question()
    if question is None:
        pytest.skip("当前只读题库没有可用于逐步提示的数学题")
    holder = {"user": dict(USER_A)}
    client = _make_client(holder)
    try:
        response = client.post(BASE, json={"ref": question["qid"]})
        assert response.status_code == 200, response.text
        payload = response.json()
        hint = payload["hint"]
        assert hint["question_ref"] == "bank:" + question["qid"]
        assert hint["source_note"] == guided_hint.SOURCE_NOTE
        assert payload["steps"][0]["kind"] == "focus"
        assert payload["steps"][0]["body"].startswith(question["stem_plain"][:40])
        answer = str(question.get("answer_text") or "").strip()
        if answer:
            assert answer not in payload["steps"][0]["body"]
        kinds = [step["kind"] for step in payload["steps"]]
        assert kinds[:2] == ["focus", "concept"]
        # 有解析时必然生成完整解析步骤（首步摘录可能为空，但解析步骤一定在）。
        assert "solution" in kinds
        rows = _db_rows(
            "SELECT question_ref, storage_ref FROM guided_hints WHERE id=?",
            (hint["hint_id"],),
        )
        assert rows[0][0] == "bank:" + question["qid"]
        assert rows[0][1] == question["qid"]
    finally:
        client.close()
