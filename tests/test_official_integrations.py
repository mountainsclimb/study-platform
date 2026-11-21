"""WS26 · 平台精选接入助手/课堂 + official 学情口径 + 学段筛选（第四轮 Phase 3 收尾）。

覆盖：
- 助手：``search_official`` 只读检索（公开编号/来源/审核状态）、``read_question`` 接受
  ``official:<公开编号>``（答案门控/撤回清晰提示/非法引用 422）、system prompt 引用规则、
  refs 携带来源与公开编号、错题/统计带 official 口径、题库题读取向后兼容；
- 课堂：``source_ref=official:<公开编号>`` 建课（大纲 prompt 注入题面/答案要点、outline.sources
  记录公开编号/版本/来源声明、场景 prompt 带来源、导出 ZIP/HTML 保留来源标记、撤回后不可新建、
  已生成课程不被撤回影响）；
- 学情：``GET /api/insight/home`` 与 ``GET /api/stats`` 的 ``official`` 口径（撤回不计入），
  ``/api/stats`` 既有平铺 official_* 字段语义不变；
- 学段筛选：``/api/questions?grade=``（official 用快照 grade、bank 有 grade 列时生效/无列安全
  降级、非法/未知值不 422 并返回 grade_filter 说明）、catalog 的 grades/grade_stages 元数据、
  ``source=all`` 合并分页语义不变。

官方数据用 ``plaza_submissions/plaza_releases`` 最小种子写入；题库用 ``bank_fixture`` 自含最小库；
所有 AI 调用 mock，不联网、不访问真实模型。
"""
import io
import json
import sqlite3
import time
import zipfile

import pytest

from app import ai, db
from app import assistant as assistant_module
from app import practice as practice_module
from app.classroom import generate as classroom_generate
from app.classroom import prompts as classroom_prompts
from app.plaza import db as plaza_db
from test_ai import save_provider


# ---------------------------------------------------------------------------
# 官方题种子（与 tests/test_official_refs.py 同一最小结构）
# ---------------------------------------------------------------------------

def user_id(client) -> int:
    response = client.get("/api/auth/me")
    assert response.status_code == 200, response.text
    return response.json()["id"]


def official_snapshot(*, stem="已知 1+1=2，下列结论正确的是（  ）",
                      options=None, answers=("B",), explanation="基础加法。",
                      qtype="SINGLE", subject="数学", grade="高中一年级",
                      tags=("函数",), difficulty=0.2):
    return {
        "question_id": 1,
        "question_version": 1,
        "type": qtype,
        "subject": subject,
        "grade": grade,
        "difficulty": difficulty,
        "knowledge_tags": list(tags),
        "content": {
            "stem": stem,
            "options": options if options is not None else [
                {"label": "A", "text": "2"}, {"label": "B", "text": "3"},
            ],
            "answers": list(answers),
            "explanation": explanation,
            "rubric": "",
            "parts": [],
            "images": [],
        },
        "verify_status": "verified",
    }


def _source_json():
    return {
        "url": "https://example.com/ws26/exam",
        "original_author": "命题组",
        "license_basis": "原创并授权知序公开展示",
        "rights_scope": "允许公开题面、答案与解析；禁止商业再分发",
        "attribution": "署名：知序投稿作者",
        "reference": "2026 模拟卷",
        "page_no": "12",
        "question_no": "3",
    }


def seed_official(*, public_id="plz-test-1", status="published", release=True,
                  release_status="active", version=1, snapshot=None,
                  subject="数学", type="SINGLE", author_id=1):
    """直接写入最小广场数据（不依赖评审流程），返回 submission_id。"""
    from app.db import connection, utcnow

    plaza_db.ensure_tables()
    snapshot = snapshot if snapshot is not None else official_snapshot()
    now = utcnow()
    with connection() as con:
        db.write_lock(con, "plaza-write")
        submission_id = plaza_db.insert_row(con, "plaza_submissions", {
            "author_id": int(author_id),
            "question_id": None,
            "version": int(version),
            "current_version": int(version),
            "subject": subject,
            "type": type,
            "grade": str(snapshot.get("grade") or ""),
            "snapshot_json": plaza_db.json_dump(snapshot),
            "source_json": plaza_db.json_dump(_source_json()),
            "fingerprint": "f" * 8,
            "license_confirmed": 1,
            "status": status,
            "public_id": public_id,
            "review_deadline_at": None,
            "created_at": now,
            "updated_at": now,
        })
        if release:
            plaza_db.insert_row(con, "plaza_releases", {
                "submission_id": submission_id,
                "version": int(version),
                "public_id": public_id,
                "status": release_status,
                "active_public_id": public_id if release_status == "active" else None,
                "published_by": 1,
                "published_at": now,
                "withdrawn_at": None,
                "withdraw_mode": "",
                "reason": "",
            })
    return submission_id


def set_official_status(public_id: str, status: str, *, release_status=None):
    from app.db import connection, utcnow

    with connection() as con:
        db.write_lock(con, "plaza-write")
        con.execute("UPDATE plaza_submissions SET status=? WHERE public_id=?", (status, public_id))
        if release_status is not None:
            con.execute(
                "UPDATE plaza_releases SET status=?, active_public_id=NULL, withdrawn_at=? "
                "WHERE public_id=?",
                (release_status, utcnow(), public_id),
            )


def withdraw_official(public_id: str):
    set_official_status(public_id, "withdrawn", release_status="withdrawn")


def clear_bank_caches():
    practice_module._sqlite_question_columns.cache_clear()
    practice_module._sqlite_group_columns.cache_clear()
    practice_module._source_groups.cache_clear()
    practice_module._verified_index_key = None
    practice_module._verified_fts_key = None
    practice_module.invalidate_catalog_cache()


@pytest.fixture
def install_bank(tmp_path, monkeypatch):
    """安装自含最小只读题库（可选追加 grade 列模拟未来题库带年级字段）。"""
    from bank_fixture import make_bank

    sequence = {"n": 0}

    def install(rows=(), groups=(), grade_values=None):
        sequence["n"] += 1
        bank = make_bank(tmp_path / f"bank-{sequence['n']}", rows, groups)
        if grade_values:
            con = sqlite3.connect(str(bank / "题库.db"))
            try:
                con.execute("ALTER TABLE questions ADD COLUMN grade TEXT")
                for qid, grade in grade_values.items():
                    con.execute("UPDATE questions SET grade=? WHERE qid=?", (grade, qid))
                con.commit()
            finally:
                con.close()
        monkeypatch.setattr(db, "BANK_DIR", bank)
        clear_bank_caches()
        return bank

    yield install
    clear_bank_caches()


def bank_question(qid="docQ:1", **values):
    from bank_fixture import question_row

    values.setdefault("doc_id", qid.split(":", 1)[0])
    values.setdefault("options_json", '[{"label":"A","text":"2"},{"label":"B","text":"3"}]')
    values.setdefault("answer_text", "A")
    return question_row(qid, "已知 1+1=2，下列结论正确的是（  ）", **values)


# ---------------------------------------------------------------------------
# 助手假模型 / 辅助
# ---------------------------------------------------------------------------

def _create_conversation(client) -> dict:
    response = client.post("/api/assistant/conversations", json={})
    assert response.status_code == 200, response.text
    return response.json()["conversation"]


def _send(client, conversation_id: int, provider: dict, content: str = "帮我找一道平台精选题"):
    return client.post(
        "/api/assistant/conversations/%s/messages" % conversation_id,
        json={"content": content, "provider_id": provider["id"], "model": "test-model"},
    )


def _wait_job(client, job_id: int, timeout: float = 15.0) -> dict:
    deadline = time.time() + timeout
    body = None
    while time.time() < deadline:
        body = client.get("/api/ai/jobs/%s" % job_id).json()
        if body["status"] in ("completed", "failed", "interrupted", "cancelled"):
            return body
        time.sleep(0.05)
    raise AssertionError("任务未在 %ss 内结束: %s" % (timeout, body))


class AssistantFakeModel:
    """按顺序返回预置 JSON 决策；记录 prompt 供断言。"""

    def __init__(self, decisions):
        self.decisions = list(decisions)
        self.calls: list[dict] = []

    async def __call__(self, provider, model, prompt, images=None, json_output=False,
                       token_limit=5000, **kwargs):
        self.calls.append({"prompt": prompt, "system_prompt": kwargs.get("system_prompt")})
        content = self.decisions.pop(0) if self.decisions else {"answer": "默认回答"}
        if not isinstance(content, str):
            content = json.dumps(content, ensure_ascii=False)
        return {"content": content, "usage": {"total_tokens": 18}, "model": model}


# ---------------------------------------------------------------------------
# 课堂假模型 / 辅助
# ---------------------------------------------------------------------------

CLASSROOM_OUTLINE_RAW = {
    "title": "函数与平台精选题精讲",
    "description": "以平台精选题为素材的函数入门课。",
    "scenes": [
        {"title": "从精选例题出发", "type": "slide",
         "outline_points": ["题目情境", "已知条件", "求解目标"]},
        {"title": "课堂小测", "type": "quiz", "outline_points": ["函数定义", "代入计算"],
         "quiz": {"question_count": 2, "difficulty": "medium"}},
        {"title": "方法总结", "type": "slide", "outline_points": ["解题步骤", "易错点"]},
    ],
}

CLASSROOM_SLIDE_RAW = {
    "elements": [
        {"id": "text_001", "type": "text", "left": 60, "top": 50, "width": 880, "height": 76,
         "content": '<p style="font-size:32px;"><strong>精选例题</strong></p>',
         "defaultFontName": "", "defaultColor": "#333333"},
    ]
}

CLASSROOM_QUIZ_RAW = [
    {"id": "q1", "type": "single", "question": "已知 f(x)=x^2，f(2)=？",
     "options": [{"label": "4", "value": "A"}, {"label": "5", "value": "B"}],
     "answer": ["A"], "analysis": "代入得 4。", "points": 10},
]

CLASSROOM_ACTIONS_RAW = [
    {"type": "action", "name": "spotlight", "params": {"elementId": "text_001"}},
    {"type": "text", "content": "这一页我们从平台精选例题出发。"},
]

CLASSROOM_ROSTER_RAW = {
    "teacher": {"name": "林老师", "persona": "善于用追问推进推导。"},
    "classmates": [{"name": "小舟", "persona": "好奇心强，常问为什么。"}],
}

_CLASSROOM_MARKERS = (
    ("课程大纲生成器", "outline"),
    ("幻灯片内容生成器", "slide"),
    ("测验内容生成器", "quiz"),
    ("课堂旁白与动作生成器", "actions"),
    ("课堂角色名册生成", "roster"),
)


class ClassroomFakeModel:
    def __init__(self):
        self.payloads = {
            "outline": json.dumps(CLASSROOM_OUTLINE_RAW, ensure_ascii=False),
            "slide": json.dumps(CLASSROOM_SLIDE_RAW, ensure_ascii=False),
            "quiz": json.dumps(CLASSROOM_QUIZ_RAW, ensure_ascii=False),
            "actions": json.dumps(CLASSROOM_ACTIONS_RAW, ensure_ascii=False),
            "roster": json.dumps(CLASSROOM_ROSTER_RAW, ensure_ascii=False),
        }
        self.calls: list[str] = []
        self.prompts: list[str] = []

    def classify(self, prompt: str) -> str:
        for marker, kind in _CLASSROOM_MARKERS:
            if marker in prompt:
                return kind
        raise AssertionError("未知 prompt：%s" % prompt[:200])

    def install(self, monkeypatch):
        async def fake_call(provider, model, prompt, images_=None, json_output=False,
                            token_limit=5000, **kwargs):
            kind = self.classify(prompt)
            self.calls.append(kind)
            self.prompts.append(prompt)
            return {"content": self.payloads[kind], "model": model,
                    "provider_name": "mock", "usage": {"total_tokens": 12}}

        monkeypatch.setattr(ai, "_call_model", fake_call)
        return self

    def prompts_of(self, kind: str) -> list[str]:
        return [prompt for prompt, item in zip(self.prompts, self.calls) if item == kind]


def create_course(client, **overrides) -> dict:
    payload = {"topic": "函数的表示法", "subject": "数学", "grade": "高中一年级"}
    payload.update(overrides)
    response = client.post("/api/classroom/courses", json=payload)
    assert response.status_code == 200, response.text
    return response.json()["course"]


def start_generate(client, provider: dict, course: dict, **overrides) -> dict:
    payload = {
        "stage": "all", "provider_id": provider["id"], "model": "test-model",
        "scene_count": 3,
    }
    payload.update(overrides)
    response = client.post(f"/api/classroom/courses/{course['id']}/generate", json=payload)
    assert response.status_code == 200, response.text
    return response.json()["job"]


def wait_for_status(client, job_id: int, status: str, tries: int = 600, delay: float = 0.02) -> dict:
    job = None
    for _ in range(tries):
        job = client.get(f"/api/ai/jobs/{job_id}").json()
        if job["status"] == status:
            return job
        if job["status"] in ("failed", "cancelled"):
            pytest.fail(f"任务提前进入 {job['status']}（期望 {status}）：{job.get('error')}")
        time.sleep(delay)
    pytest.fail(f"任务未进入 {status}，当前 {job}")


def confirm_outline(client, job: dict) -> dict:
    outline = job["result"]["outline"]
    response = client.post(f"/api/ai/jobs/{job['id']}/confirm-outline", json={
        "outline": json.dumps(outline, ensure_ascii=False),
    })
    assert response.status_code == 200, response.text
    return outline


def start_official_course(client, monkeypatch, source_ref: str):
    """以平台精选题为素材启动课程生成，返回 (provider, course, fake, job, paused)。"""
    provider = save_provider(client)
    course = create_course(client)
    fake = ClassroomFakeModel().install(monkeypatch)
    job = start_generate(client, provider, course, source_ref=source_ref)
    paused = wait_for_status(client, job["id"], "awaiting_outline")
    return provider, course, fake, job, paused


# ---------------------------------------------------------------------------
# 1. 助手：official 检索工具
# ---------------------------------------------------------------------------

def test_assistant_search_official_returns_public_metadata(client):
    seed_official(
        public_id="plz-ai-math", subject="数学",
        snapshot=official_snapshot(stem="平台精选函数题", grade="高中一年级", tags=("函数",)),
    )
    seed_official(
        public_id="plz-ai-physics", subject="物理", type="MULTIPLE",
        snapshot=official_snapshot(
            stem="平台精选动量题", qtype="MULTIPLE", subject="物理", grade="初中二年级",
            options=[{"label": "A", "text": "甲"}, {"label": "B", "text": "乙"}],
            answers=["AB"], tags=("动量",),
        ),
    )
    uid = user_id(client)
    result = assistant_module._tool_search_official({"id": uid}, {"subject": "数学"})
    assert result["ok"] is True and result["tool"] == "search_official"
    assert result["data"]["total"] == 1
    item = result["data"]["items"][0]
    assert item["qid"] == "official:plz-ai-math"
    assert item["source"] == "official" and item["source_label"] == "平台精选"
    assert item["public_id"] == "plz-ai-math"
    assert item["review_status"] == "featured"
    assert item["review_status_label"] == "平台精选"
    assert item["knowledge_tags"] == ["函数"]
    # 不泄露内部字段
    for forbidden in ("author_id", "user_id", "question_id", "submission_id"):
        assert forbidden not in item
    ref = result["refs"][0]
    assert ref["source"] == "official" and ref["public_id"] == "plz-ai-math"
    # 学段 / 题型筛选
    by_grade = assistant_module._tool_search_official({"id": uid}, {"grade": "初中"})
    assert [entry["qid"] for entry in by_grade["data"]["items"]] == ["official:plz-ai-physics"]
    by_type = assistant_module._tool_search_official({"id": uid}, {"qtype": "MULTIPLE"})
    assert [entry["qid"] for entry in by_type["data"]["items"]] == ["official:plz-ai-physics"]
    # 无筛选条件时明确提示，而不是返回全量
    with pytest.raises(assistant_module.ToolError):
        assistant_module._tool_search_official({"id": uid}, {})


def test_assistant_search_official_excludes_withdrawn_and_disputed(client):
    seed_official(public_id="plz-ai-live", subject="数学")
    seed_official(public_id="plz-ai-gone", subject="数学")
    withdraw_official("plz-ai-gone")
    seed_official(public_id="plz-ai-dispute", subject="数学", status="disputed",
                  release_status="withdrawn")
    result = assistant_module._tool_search_official(
        {"id": user_id(client)}, {"subject": "数学"},
    )
    assert {entry["qid"] for entry in result["data"]["items"]} == {"official:plz-ai-live"}
    assert {ref["public_id"] for ref in result["refs"]} == {"plz-ai-live"}


# ---------------------------------------------------------------------------
# 2. 助手：read_question 接受 official 引用（门控/撤回/非法）
# ---------------------------------------------------------------------------

def test_assistant_read_official_answer_gated_by_attempt(client, second_client):
    seed_official(public_id="plz-ai-read")
    ref = "official:plz-ai-read"
    uid = user_id(client)
    locked = assistant_module._tool_read_question({"id": uid}, {"qid": ref})
    assert locked["data"]["source"] == "official"
    assert locked["data"]["source_label"] == "平台精选"
    assert locked["data"]["public_id"] == "plz-ai-read"
    assert locked["data"]["unlocked"] is False
    assert "answer" not in locked["data"] and "solution" not in locked["data"]
    assert any("未作答" in note for note in locked["data"]["notes"])
    assert any("平台精选" in note and "公开编号" in note for note in locked["data"]["notes"])

    submitted = client.post("/api/attempts", data={"qid": ref, "answer": "A"})
    assert submitted.status_code == 200, submitted.text
    unlocked = assistant_module._tool_read_question({"id": uid}, {"qid": ref})
    assert unlocked["data"]["unlocked"] is True
    assert unlocked["data"]["answer"] == "B"
    assert unlocked["data"]["review_status"] == "featured"
    # 他人不因本人作答解锁
    other = assistant_module._tool_read_question(
        {"id": user_id(second_client)}, {"qid": ref},
    )
    assert other["data"]["unlocked"] is False and "answer" not in other["data"]


def test_assistant_read_official_withdrawn_and_invalid_refs(client):
    seed_official(public_id="plz-ai-hidden")
    withdraw_official("plz-ai-hidden")
    uid = user_id(client)
    with pytest.raises(assistant_module.ToolError) as hidden:
        assistant_module._tool_read_question({"id": uid}, {"qid": "official:plz-ai-hidden"})
    assert hidden.value.status == 404
    assert "撤回" in str(hidden.value)
    assert "已知 1+1=2" not in str(hidden.value)

    with pytest.raises(assistant_module.ToolError) as missing:
        assistant_module._tool_read_question({"id": uid}, {"qid": "official:plz-not-exist"})
    assert missing.value.status == 404 and "未找到" in str(missing.value)

    with pytest.raises(assistant_module.ToolError) as invalid:
        assistant_module._tool_read_question({"id": uid}, {"qid": "official:"})
    assert invalid.value.status == 422 and "格式" in str(invalid.value)

    seed_official(public_id="plz-ai-disputed", status="disputed", release_status="withdrawn")
    with pytest.raises(assistant_module.ToolError) as disputed:
        assistant_module._tool_read_question({"id": uid}, {"qid": "official:plz-ai-disputed"})
    assert "争议" in str(disputed.value) or "暂停" in str(disputed.value)


def test_assistant_tools_prompt_and_refs_carry_official_source(client):
    body = client.get("/api/assistant/tools").json()
    by_name = {item["name"]: item for item in body["items"]}
    assert "search_official" in by_name
    assert by_name["search_official"]["side_effect"] is False
    assert "official" in by_name["read_question"]["description"]
    assert "平台精选" in body["scope_note"]
    system_prompt = assistant_module._system_prompt(
        {"username": "测试同学", "role": "user"}, {"model": "test-model"},
    )
    for marker in ("平台精选", "公开编号", "撤回", "search_official"):
        assert marker in system_prompt, marker

    bucket: list = []
    assistant_module._collect_refs(bucket, [{
        "kind": "question", "qid": "official:plz-x", "title": "平台精选题 · 数学",
        "source": "official", "source_label": "平台精选", "public_id": "plz-x",
        "review_status": "featured", "internal_author_id": 1,
    }])
    assert bucket[0]["source"] == "official"
    assert bucket[0]["public_id"] == "plz-x"
    assert "internal_author_id" not in bucket[0]


def test_assistant_agent_loop_official_search_end_to_end(client, monkeypatch):
    seed_official(public_id="plz-ai-loop", subject="数学")
    provider = save_provider(client)
    conversation = _create_conversation(client)
    fake = AssistantFakeModel([
        {"tool": "search_official", "args": {"subject": "数学"}},
        {"answer": "推荐平台精选 plz-ai-loop：这是一道函数基础题。"},
    ])
    monkeypatch.setattr(ai, "_call_model", fake)
    sent = _send(client, conversation["id"], provider)
    assert sent.status_code == 200, sent.text
    job = _wait_job(client, sent.json()["job"]["id"])
    assert job["status"] == "completed", job
    result = job["result"]
    assert result["tools"] == ["search_official"]
    assert result["refs"], result
    assert result["refs"][0]["public_id"] == "plz-ai-loop"
    assert result["refs"][0]["source"] == "official"
    detail = client.get("/api/assistant/conversations/%s" % conversation["id"]).json()
    tool_message = next(item for item in detail["messages"] if item["role"] == "tool")
    assert tool_message["tool_result"]["ok"] is True
    assert tool_message["tool_result"]["data"]["items"][0]["public_id"] == "plz-ai-loop"
    # 第二次模型调用能看到工具观察（公开编号回灌），system prompt 带引用规则
    assert "公开编号" in fake.calls[1]["prompt"]
    assert "平台精选" in fake.calls[0]["system_prompt"]


def test_assistant_wrongbook_and_stats_mark_official_source(client):
    seed_official(public_id="plz-ai-wrong")
    ref = "official:plz-ai-wrong"
    uid = user_id(client)
    assert client.post("/api/attempts", data={"qid": ref, "answer": "A"}).status_code == 200
    wrongs = assistant_module._tool_list_wrongs({"id": uid}, {"limit": 5})
    card = wrongs["data"]["items"][0]
    assert card["source"] == "official" and card["source_label"] == "平台精选"
    assert card["public_id"] == "plz-ai-wrong"
    stats = assistant_module._tool_get_stats({"id": uid}, {})
    official = stats["data"]["official"]
    assert official["available"] is True
    assert official["practiced"] == 1 and official["wrong_count"] == 1
    assert "平台精选已练 1 道" in stats["summary"]
    # 撤回后历史保留但不计入 official 口径（错题占位卡不回显正文）
    withdraw_official("plz-ai-wrong")
    stats_after = assistant_module._tool_get_stats({"id": uid}, {})
    assert stats_after["data"]["official"]["wrong_count"] == 0
    assert stats_after["data"]["official"]["attempts"] == 0
    wrongs_after = assistant_module._tool_list_wrongs({"id": uid}, {"limit": 5})
    assert wrongs_after["data"]["items"][0]["unavailable"] is True


def test_assistant_read_question_bank_still_works(client, install_bank):
    install_bank([bank_question()])
    result = assistant_module._tool_read_question({"id": user_id(client)}, {"qid": "docQ:1"})
    assert result["data"]["source"] == "bank"
    assert result["data"]["source_label"] == "高考题库"
    assert "public_id" not in result["data"]


# ---------------------------------------------------------------------------
# 3. 学段筛选：/api/questions?grade= 与 catalog 元数据
# ---------------------------------------------------------------------------

def test_questions_grade_filter_official_stage_and_exact(client):
    seed_official(public_id="plz-grade-math", subject="数学",
                  snapshot=official_snapshot(grade="高中一年级"))
    seed_official(public_id="plz-grade-physics", subject="物理", type="BLANK",
                  snapshot=official_snapshot(
                      grade="初中二年级", subject="物理", qtype="BLANK",
                      options=[], answers=["6"],
                  ))
    stage = client.get("/api/questions", params={"source": "official", "grade": "高中"}).json()
    assert stage["total"] == 1
    assert stage["items"][0]["qid"] == "official:plz-grade-math"
    assert stage["grade_filter"]["applied"] is True
    assert stage["grade_filter"]["official_applied"] is True
    exact = client.get(
        "/api/questions", params={"source": "official", "grade": "初中二年级"},
    ).json()
    assert [item["qid"] for item in exact["items"]] == ["official:plz-grade-physics"]
    both = client.get(
        "/api/questions", params={"source": "official", "grade": "高中一年级,初中二年级"},
    ).json()
    assert both["total"] == 2
    # 未知值安全降级：不 422、不过滤、返回 grade_filter 说明
    unknown = client.get(
        "/api/questions", params={"source": "official", "grade": "火星年级"},
    ).json()
    assert unknown["total"] == 2
    assert unknown["grade_filter"]["applied"] is False
    assert unknown["grade_filter"]["reason"]
    # 未传 grade 时不追加 grade_filter 字段（旧响应结构不变）
    plain = client.get("/api/questions", params={"source": "official"}).json()
    assert "grade_filter" not in plain


def test_catalog_includes_grade_filter_metadata(client, install_bank):
    install_bank([bank_question()])
    catalog = client.get("/api/catalog").json()
    assert len(catalog["grades"]) == 12
    assert catalog["grades"][0] == "小学一年级"
    assert catalog["grades"][-1] == "高中三年级"
    assert catalog["grade_stages"] == ["小学", "初中", "高中"]
    assert "学段" in catalog["grade_note"]


def test_questions_grade_filter_bank_with_grade_column(client, install_bank):
    install_bank(
        [bank_question(qid="g:1"), bank_question(qid="g:2")],
        grade_values={"g:1": "高中一年级", "g:2": "初中二年级"},
    )
    data = client.get("/api/questions", params={"grade": "高中"}).json()
    assert data["total"] == 1
    assert data["items"][0]["qid"] == "g:1"
    assert data["grade_filter"]["applied"] is True
    assert data["grade_filter"]["bank_applied"] is True
    exact = client.get("/api/questions", params={"grade": "初中二年级"}).json()
    assert [item["qid"] for item in exact["items"]] == ["g:2"]


def test_questions_grade_filter_degrades_without_bank_grade_column(client, install_bank):
    install_bank([bank_question(qid="ng:1"), bank_question(qid="ng:2")])
    data = client.get("/api/questions", params={"grade": "高中一年级"}).json()
    assert data["total"] == 2
    assert data["grade_filter"]["applied"] is False
    assert "没有年级字段" in data["grade_filter"]["reason"]


def test_questions_source_all_grade_filter_keeps_pagination(client, install_bank):
    install_bank(
        [bank_question(qid="ag:1"), bank_question(qid="ag:2"), bank_question(qid="ag:3")],
        grade_values={"ag:1": "高中一年级", "ag:2": "高中一年级", "ag:3": "初中二年级"},
    )
    seed_official(public_id="plz-ag-1", snapshot=official_snapshot(grade="高中一年级"))
    seed_official(public_id="plz-ag-2", snapshot=official_snapshot(grade="初中二年级"))
    first = client.get("/api/questions", params={
        "source": "all", "grade": "高中一年级", "page": 1, "page_size": 2,
    }).json()
    assert first["source"] == "all"
    assert first["bank_total"] == 2 and first["official_total"] == 1
    assert first["total"] == 3
    assert [item["qid"] for item in first["items"]] == ["ag:1", "ag:2"]
    assert first["grade_filter"]["bank_applied"] is True
    assert first["grade_filter"]["official_applied"] is True
    second = client.get("/api/questions", params={
        "source": "all", "grade": "高中一年级", "page": 2, "page_size": 2,
    }).json()
    assert [item["qid"] for item in second["items"]] == ["official:plz-ag-1"]


# ---------------------------------------------------------------------------
# 4. 学情 / 统计的 official 口径
# ---------------------------------------------------------------------------

def test_insight_home_official_caliber_and_withdrawal(client, install_bank):
    install_bank([bank_question()])
    seed_official(public_id="plz-home-1")
    seed_official(public_id="plz-home-2")
    home = client.get("/api/insight/home").json()
    official = home["official"]
    assert official["available"] is True
    assert official["total_questions"] == 2
    assert official["practice_pool"] == 2
    assert official["auto_gradable"] == 2
    assert home["bank"]["official_practice_pool"] == 2
    assert home["bank"]["official_auto_gradable"] == 2
    assert "official" in home["caliber"]

    assert client.post(
        "/api/attempts", data={"qid": "official:plz-home-1", "answer": "A"},
    ).status_code == 200
    home2 = client.get("/api/insight/home").json()["official"]
    assert home2["attempts"] == 1 and home2["practiced"] == 1
    assert home2["wrong_count"] == 1
    # 撤回未练过的题：总数下降，已练/错题不受影响
    withdraw_official("plz-home-2")
    home3 = client.get("/api/insight/home").json()["official"]
    assert home3["total_questions"] == 1
    assert home3["practiced"] == 1 and home3["wrong_count"] == 1
    # 撤回已练过的题：撤回题不计入 official 口径（历史记录仍保留在库中）
    withdraw_official("plz-home-1")
    home4 = client.get("/api/insight/home").json()["official"]
    assert home4["total_questions"] == 0
    assert home4["attempts"] == 0 and home4["practiced"] == 0
    assert home4["wrong_count"] == 0


def test_stats_official_block_excludes_withdrawn_legacy_fields_unchanged(client):
    seed_official(public_id="plz-stats-1")
    ref = "official:plz-stats-1"
    assert client.post("/api/attempts", data={"qid": ref, "answer": "A"}).status_code == 200
    stats = client.get("/api/stats").json()
    assert stats["official"]["available"] is True
    assert stats["official"]["attempts"] == 1
    assert stats["official"]["practiced"] == 1
    assert stats["official"]["wrong_count"] == 1
    # 既有平铺字段语义不变（历史口径）
    assert stats["official_attempts"] == 1
    assert stats["official_practiced"] == 1
    assert stats["official_wrong_count"] == 1
    withdraw_official("plz-stats-1")
    after = client.get("/api/stats").json()
    assert after["official"]["attempts"] == 0
    assert after["official"]["practiced"] == 0
    assert after["official"]["wrong_count"] == 0
    assert after["official_attempts"] == 1  # 既有字段保留历史语义
    assert after["official_practiced"] == 1
    assert after["official_wrong_count"] == 1


# ---------------------------------------------------------------------------
# 5. 课堂：以平台精选题为素材建课（来源记录 / 撤回 / 导出）
# ---------------------------------------------------------------------------

def test_classroom_seed_source_ref_validation(client):
    provider = save_provider(client)
    course = create_course(client)

    def submit(source_ref):
        return client.post(f"/api/classroom/courses/{course['id']}/generate", json={
            "stage": "all", "provider_id": provider["id"], "model": "test-model",
            "scene_count": 3, "source_ref": source_ref,
        })

    assert submit("bank:docQ:1").status_code == 422
    assert submit("official:").status_code == 422
    assert submit("official:plz-missing").status_code == 404
    seed_official(public_id="plz-withdrawn-course")
    withdraw_official("plz-withdrawn-course")
    withdrawn = submit("official:plz-withdrawn-course")
    assert withdrawn.status_code == 404
    assert "撤回" in withdrawn.text or "不可见" in withdrawn.text
    seed_official(public_id="plz-visible-course")
    assert submit("official:plz-visible-course").status_code == 200


def test_classroom_job_validate_accepts_seed_question_alias(client):
    """``/api/ai/jobs`` 直提参数兼容 ``source_ref`` 与 ``seed_question`` 两个键名。"""
    from types import SimpleNamespace

    from fastapi import HTTPException

    save_provider(client)
    seed_official(public_id="plz-alias-seed")
    course = create_course(client)
    user = {"id": user_id(client), "role": "user"}
    classroom_generate.validate_course_job(
        SimpleNamespace(params={
            "course_id": course["id"], "stage": "outline",
            "seed_question": "official:plz-alias-seed",
        }), user,
    )
    withdraw_official("plz-alias-seed")
    with pytest.raises(HTTPException) as excinfo:
        classroom_generate.validate_course_job(
            SimpleNamespace(params={
                "course_id": course["id"], "stage": "outline",
                "seed_question": "official:plz-alias-seed",
            }), user,
        )
    assert excinfo.value.status_code == 404


def test_classroom_official_seed_generates_and_records_sources(client, monkeypatch):
    seed_official(
        public_id="plz-course-1",
        snapshot=official_snapshot(
            stem="已知函数 f(x)=x^2，求 f(2)。", grade="高中一年级",
            answers=["A"], tags=("函数",),
        ),
    )
    _provider, course, fake, job, paused = start_official_course(
        client, monkeypatch, "official:plz-course-1",
    )
    result = paused["result"]
    assert result["official_source_count"] == 1
    assert result["official_sources"][0]["public_id"] == "plz-course-1"
    assert result["official_sources"][0]["published_version"] == 1
    assert result["official_sources"][0]["review_status"] == "featured"
    sources = result["outline"]["sources"]
    official_entries = [item for item in sources if item.get("kind") == "official"]
    assert len(official_entries) == 1
    entry = official_entries[0]
    assert entry["public_id"] == "plz-course-1"
    assert entry["published_version"] == 1
    assert entry["question_ref"] == "official:plz-course-1"
    assert entry["source_declaration"]["license_basis"] == "原创并授权知序公开展示"
    assert "公开编号 plz-course-1" in entry["title"]
    # 大纲 prompt 注入题面与答案要点，并带公开编号引用规则
    outline_prompt = fake.prompts_of("outline")[0]
    assert "已知函数 f(x)=x^2" in outline_prompt
    assert "公开编号 plz-course-1" in outline_prompt
    assert "平台精选题素材" in outline_prompt
    assert "不得编造公开编号" in outline_prompt
    # 内部 ID 不进入 prompt
    assert "author_id" not in outline_prompt and "submission_id" not in outline_prompt

    confirm_outline(client, paused)
    done = wait_for_status(client, job["id"], "completed")
    assert done["result"]["official_source_count"] == 1
    assert "平台精选 plz-course-1" in done["result"]["summary"]
    scene_prompts = [prompt for prompt in fake.prompts_of("slide")]
    scene_prompts += fake.prompts_of("quiz")
    assert scene_prompts
    for prompt in scene_prompts:
        assert "平台精选" in prompt and "plz-course-1" in prompt
    detail = client.get(f"/api/classroom/courses/{course['id']}").json()["course"]
    stored = [item for item in detail["outline"]["sources"] if item.get("kind") == "official"]
    assert stored and stored[0]["public_id"] == "plz-course-1"
    assert stored[0]["published_version"] == 1
    assert detail["config"]["seed_source"]["public_id"] == "plz-course-1"


def test_classroom_official_course_export_keeps_source_marker(client, monkeypatch):
    seed_official(
        public_id="plz-course-export",
        snapshot=official_snapshot(stem="已知函数 g(x)=x+1，求 g(1)。", grade="高中一年级"),
    )
    _provider, course, _fake, job, paused = start_official_course(
        client, monkeypatch, "official:plz-course-export",
    )
    confirm_outline(client, paused)
    wait_for_status(client, job["id"], "completed")

    zip_response = client.get(f"/api/classroom/courses/{course['id']}/export?format=zip")
    assert zip_response.status_code == 200, zip_response.text
    with zipfile.ZipFile(io.BytesIO(zip_response.content)) as archive:
        assert "sources.json" in archive.namelist()
        payload = json.loads(archive.read("sources.json").decode("utf-8"))
    official_entries = [item for item in payload["sources"] if item.get("kind") == "official"]
    assert official_entries, payload
    assert official_entries[0]["public_id"] == "plz-course-export"
    assert official_entries[0]["question_ref"] == "official:plz-course-export"
    assert "平台精选" in official_entries[0]["title"]

    html_response = client.get(f"/api/classroom/courses/{course['id']}/export?format=html")
    assert html_response.status_code == 200
    html = html_response.text
    assert "plz-course-export" in html
    assert "平台精选" in html


def test_classroom_withdrawn_seed_does_not_break_existing_course(client, monkeypatch):
    seed_official(public_id="plz-course-withdraw",
                  snapshot=official_snapshot(stem="已知函数 h(x)=2x，求 h(1)。"))
    _provider, course, _fake, job, paused = start_official_course(
        client, monkeypatch, "official:plz-course-withdraw",
    )
    # 大纲生成后题目被撤回：已开始的课程继续完成，来源标记保留
    withdraw_official("plz-course-withdraw")
    confirm_outline(client, paused)
    done = wait_for_status(client, job["id"], "completed")
    assert done["status"] == "completed"
    detail = client.get(f"/api/classroom/courses/{course['id']}").json()["course"]
    stored = [item for item in detail["outline"]["sources"] if item.get("kind") == "official"]
    assert stored and stored[0]["public_id"] == "plz-course-withdraw"
    # 撤回后不允许新课程引用
    provider = save_provider(client)
    new_course = create_course(client)
    response = client.post(f"/api/classroom/courses/{new_course['id']}/generate", json={
        "stage": "all", "provider_id": provider["id"], "model": "test-model",
        "scene_count": 3, "source_ref": "official:plz-course-withdraw",
    })
    assert response.status_code == 404
    # 任务参数校验同样失败（不留下新任务）
    with pytest.raises(Exception):
        classroom_generate.validate_course_job(
            type("Job", (), {"params": {
                "course_id": new_course["id"], "stage": "all",
                "source_ref": "official:plz-course-withdraw",
            }})(),
            {"id": user_id(client), "role": "user"},
        )


# ---------------------------------------------------------------------------
# 6. 课堂 prompt 构建与来源清洗（不依赖生成任务的单元级断言）
# ---------------------------------------------------------------------------

def test_classroom_official_source_helpers_prompt_and_cleaning():
    question = {
        "public_id": "plz-helpers-1", "subject": "数学", "grade": "高中一年级",
        "review_status": "featured", "review_status_label": "平台精选",
        "published_version": 3, "stem_plain": "已知函数 f(x)=x^2，求 f(2)。",
        "options": [{"label": "A", "text": "4"}, {"label": "B", "text": "5"}],
        "answer": {"text": "A"}, "solution": "f(2)=4。",
        "knowledge_tags": ["函数"],
        "source_declaration": {"original_author": "命题组", "license_basis": "原创授权"},
        "notice": "该题已由平台编辑签发为平台精选。",
    }
    material = classroom_generate.format_seed_question_material(question)
    assert "公开编号 plz-helpers-1" in material
    assert "已知函数 f(x)=x^2" in material
    assert "参考答案" in material
    entry = classroom_generate.official_source_entry(question, 2)
    assert entry["kind"] == "official" and entry["index"] == 2
    cleaned = classroom_generate.outline_sources({"sources": [entry]})
    assert cleaned[0]["public_id"] == "plz-helpers-1"
    assert cleaned[0]["published_version"] == 3
    assert cleaned[0]["question_ref"] == "official:plz-helpers-1"
    assert cleaned[0]["source_declaration"]["license_basis"] == "原创授权"
    prompt = classroom_prompts.build_outline_prompt(
        topic="函数", seed_question_text=material, scene_count=3,
    )
    assert "平台精选题素材" in prompt
    assert "公开编号 plz-helpers-1" in prompt
    formatted = classroom_generate.format_sources_for_prompt([entry])
    assert "平台精选" in formatted and "plz-helpers-1" in formatted
    assert classroom_generate.normalize_seed_source_ref("official:plz-helpers-1") == (
        "official:plz-helpers-1"
    )
    with pytest.raises(Exception):
        classroom_generate.normalize_seed_source_ref("bank:docQ:1")
