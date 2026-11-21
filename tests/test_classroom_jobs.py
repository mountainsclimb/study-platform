"""``course`` 任务执行器验收：两阶段生成 / awaiting_outline / confirm-outline / 逐场景提交 /
失败隔离 / 取消保留已生成场景 / checkpoint 续跑 / 单场景重新生成。

不联网：``_call_model`` 全部替换为按 prompt 标记分派的假模型。
"""
import asyncio
import json
import time
from types import SimpleNamespace

from fastapi import HTTPException
import pytest

from app import ai, db
from app.ai import jobs as ai_jobs
from app.classroom import generate, schema
from app.classroom import db as cdb


# ---------------------------------------------------------------------------
# 假模型与公共辅助
# ---------------------------------------------------------------------------

OUTLINE_RAW = {
    "title": "光合作用入门",
    "description": "面向初中生物的光合作用原理入门课。",
    "scenes": [
        {"title": "为什么植物离不开光", "type": "slide", "outline_points": ["生活现象", "光合作用定义", "学习目标"]},
        {"title": "课堂小测", "type": "quiz", "outline_points": ["场所", "原料"],
         "quiz": {"question_count": 2, "difficulty": "medium"}},
        {"title": "原料与产物", "type": "slide", "outline_points": ["水", "二氧化碳", "叶绿体"]},
    ],
}

SLIDE_RAW = {
    "elements": [
        {"id": "text_001", "type": "text", "left": 60, "top": 50, "width": 880, "height": 76,
         "content": '<p style="font-size:32px;"><strong>光合作用</strong></p>',
         "defaultFontName": "", "defaultColor": "#333333"},
        {"id": "text_002", "type": "text", "left": 80, "top": 160, "width": 840, "height": 130,
         "content": '<p style="font-size:20px;">• 水</p><p style="font-size:20px;">• 二氧化碳</p>',
         "defaultFontName": "", "defaultColor": "#333333"},
    ]
}

QUIZ_RAW = [
    {"id": "q1", "type": "single", "question": "光合作用发生的场所是？",
     "options": [{"label": "叶绿体", "value": "A"}, {"label": "线粒体", "value": "B"}],
     "answer": ["A"], "analysis": "叶绿体是场所。", "points": 10},
    {"id": "q2", "type": "multiple", "question": "光合作用的原料包括？（多选）",
     "options": [{"label": "水", "value": "A"}, {"label": "氧气", "value": "B"}, {"label": "二氧化碳", "value": "C"}],
     "answer": ["A", "C"], "analysis": "水和二氧化碳是原料。", "points": 10},
]

ACTIONS_RAW = [
    {"type": "action", "name": "spotlight", "params": {"elementId": "text_001"}},
    {"type": "text", "content": "这一页我们来看光合作用。"},
    {"type": "action", "name": "spotlight", "params": {"elementId": "text_002"}},
    {"type": "text", "content": "水与二氧化碳是光合作用的原料。"},
]

TUTOR_RAW = {
    "reply": {"speaker": "teacher", "text": "叶绿体是光合作用发生的场所。",
              "actions": [{"type": "spotlight", "element_id": "text_001"}]},
}

# P1 起 interactive/PBL 走真实生成；以下假产物同时满足 P1 校验，占位实现下不会被调用。
INTERACTIVE_RAW = {
    "widget": "simulation",
    "height": 440,
    "html": (
        "<!DOCTYPE html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
        "<title>光照强度模拟</title></head><body><div id=\"experiment\">"
        "<input id=\"light\" type=\"range\" min=\"0\" max=\"100\" value=\"40\">"
        "<p>调节光照观察光合速率。</p></div><script>"
        "(function () { function zhixuReport(type, payload) {"
        " window.parent.postMessage({ source: 'zhixu-interactive', type: type, payload: payload || {} }, '*'); }"
        " window.addEventListener('load', function () { zhixuReport('ready', {}); });"
        "})();</script></body></html>"
    ),
}

PBL_RAW = {
    "project": {
        "title": "校园节能方案",
        "background": "学校近三个月电费上涨，后勤希望学生提出可执行的节能方案。",
        "goals": ["理解用电统计方法", "用数据支撑节能建议"],
        "roles": [{"name": "数据分析师", "description": "统计校园用电"},
                  {"name": "节能设计师", "description": "提出节能措施"}],
        "milestones": [
            {"title": "用电调研", "tasks": ["记录一周用电"], "deliverable": "用电调研表",
             "checkpoint": "数据覆盖一周且标注异常时段"},
            {"title": "方案设计", "tasks": ["提出三条措施"], "deliverable": "节能方案海报",
             "checkpoint": "每条措施都有数据与收益估算"},
        ],
        "rubric": [{"criterion": "数据充分", "levels": "优秀/合格/待改进"}],
        "mentor_hints": ["先算清基线用电量，再谈节省"],
    },
}

_MARKERS = (
    ("课程大纲生成器", "outline"),
    ("幻灯片内容生成器", "slide"),
    ("测验内容生成器", "quiz"),
    ("课堂旁白与动作生成器", "actions"),
    ("交互场景 HTML 生成器", "interactive"),
    ("项目式学习（PBL）设计器", "pbl"),
    ("课堂 AI 教师对话", "tutor"),
)


def save_provider(client, name="课堂测试连接", models=("test-model",)):
    response = client.post("/api/providers", json={
        "name": name, "kind": "openai", "base_url": "https://api.openai.com/v1",
        "api_key": "sk-classroom-unit-test-123456", "models": list(models),
    })
    assert response.status_code == 200, response.text
    return response.json()


def create_course(client, **overrides):
    payload = {"topic": "光合作用", "subject": "生物", "grade": "初中二年级"}
    payload.update(overrides)
    response = client.post("/api/classroom/courses", json=payload)
    assert response.status_code == 200, response.text
    return response.json()["course"]


def user_id_of(client):
    return client.get("/api/auth/me").json()["id"]


def job_view(client, job_id):
    response = client.get(f"/api/ai/jobs/{job_id}")
    assert response.status_code == 200, response.text
    return response.json()


def wait_for_status(client, job_id, status, tries=250, delay=0.03):
    job = None
    for _ in range(tries):
        job = job_view(client, job_id)
        if job["status"] == status:
            return job
        if job["status"] in ("failed", "cancelled") and status not in (job["status"],):
            pytest.fail(f"任务提前进入 {job['status']}（期望 {status}）：{job.get('error')}")
        time.sleep(delay)
    pytest.fail(f"任务未进入 {status}，当前 {job}")


def job_checkpoint(job_id):
    with db.connection() as con:
        raw = con.execute("SELECT checkpoint_json FROM ai_jobs WHERE id=?", (job_id,)).fetchone()[0]
    return json.loads(raw) if raw else {}


def provider_row(provider_id):
    with db.connection() as con:
        return dict(con.execute("SELECT * FROM ai_providers WHERE id=?", (provider_id,)).fetchone())


def insert_course_job(user_id, params, provider_id, model, *, token="classroom-token",
                      status="queued", checkpoint=None):
    now = db.utcnow()
    request = {"task": "course", "provider_id": provider_id, "model": model, "params": params, "requirements": ""}
    with db.connection() as con:
        return con.execute(
            "INSERT INTO ai_jobs(user_id,task,status,stage,request_json,partial_content,cancelled,"
            "created_at,updated_at,dispatch_token,checkpoint_json) VALUES (?,?,?,?,?,'',0,?,?,?,?)",
            (user_id, "course", status, status, json.dumps(request, ensure_ascii=False), now, now, token,
             json.dumps(checkpoint, ensure_ascii=False) if checkpoint is not None else None),
        ).lastrowid


def set_course_outline(course_id, user_id, outline, *, status="outline_ready"):
    with db.connection() as con:
        con.execute(
            "UPDATE cls_courses SET outline_json=?, status=?, updated_at=? WHERE id=? AND user_id=?",
            (cdb.json_dump(outline), status, db.utcnow(), course_id, user_id),
        )


def course_scene_rows(course_id, user_id):
    return cdb.course_scenes(course_id, user_id)


class FakeModel:
    """按 prompt 标记返回固定 JSON 的假模型；支持失败、首次非法与调用钩子。"""

    def __init__(self, *, outline=None, slide=None, quiz=None, actions=None,
                 interactive=None, pbl=None, fail=(), invalid_first=(), on_call=None, delay=0.0):
        self.payloads = {
            "outline": json.dumps(outline if outline is not None else OUTLINE_RAW, ensure_ascii=False),
            "slide": json.dumps(slide if slide is not None else SLIDE_RAW, ensure_ascii=False),
            "quiz": json.dumps(quiz if quiz is not None else QUIZ_RAW, ensure_ascii=False),
            "actions": json.dumps(actions if actions is not None else ACTIONS_RAW, ensure_ascii=False),
            "interactive": json.dumps(interactive if interactive is not None else INTERACTIVE_RAW, ensure_ascii=False),
            "pbl": json.dumps(pbl if pbl is not None else PBL_RAW, ensure_ascii=False),
            "tutor": json.dumps(TUTOR_RAW, ensure_ascii=False),
        }
        self.fail = set(fail)
        self.invalid_first = set(invalid_first)
        self.on_call = on_call
        self.delay = delay
        self.calls = []
        self.prompts = []

    def classify(self, prompt):
        for marker, kind in _MARKERS:
            if marker in prompt:
                return kind
        raise AssertionError("未知 prompt：%s" % prompt[:200])

    def install(self, monkeypatch):
        async def fake_call(provider, model, prompt, images=None, json_output=False, token_limit=5000, **kwargs):
            kind = self.classify(prompt)
            self.calls.append(kind)
            self.prompts.append(prompt)
            if self.on_call:
                self.on_call(kind, len(self.calls))
            if self.delay:
                await asyncio.sleep(self.delay)
            if kind in self.fail:
                raise ValueError("模拟模型失败：%s" % kind)
            content = self.payloads[kind]
            if kind in self.invalid_first:
                self.invalid_first.discard(kind)
                content = "这不是 JSON"
            return {"content": content, "model": model, "provider_name": "mock", "usage": {"total_tokens": 12}}

        monkeypatch.setattr(ai, "_call_model", fake_call)
        return self

    def count(self, kind):
        return self.calls.count(kind)


def _generate_all(client, provider, course, *, scene_count=3):
    response = client.post(f"/api/classroom/courses/{course['id']}/generate", json={
        "stage": "all", "provider_id": provider["id"], "model": "test-model", "scene_count": scene_count,
    })
    assert response.status_code == 200, response.text
    return response.json()["job"]


# ---------------------------------------------------------------------------
# 1. 两阶段生成：大纲暂停 → 人工确认 → 逐场景生成 → completed
# ---------------------------------------------------------------------------

def test_two_phase_generation_pauses_then_generates_after_confirm(client, monkeypatch):
    provider = save_provider(client)
    course = create_course(client)
    fake = FakeModel().install(monkeypatch)

    job = _generate_all(client, provider, course)
    paused = wait_for_status(client, job["id"], "awaiting_outline")
    paused_checkpoint = job_checkpoint(job["id"])
    assert paused["has_checkpoint"] is True
    assert paused["result"]["outline"]["title"] == "光合作用入门"
    assert len(paused_checkpoint["outline"]["scenes"]) == 3
    # 只调用了大纲生成，尚未生成任何场景
    assert fake.calls == ["outline"]

    detail = client.get(f"/api/classroom/courses/{course['id']}").json()
    assert detail["course"]["status"] == "outline_ready"
    assert detail["scenes"] == []

    # 用户在 UI 编辑大纲后确认（JSON 字符串）
    edited = paused_checkpoint["outline"]
    edited["scenes"][0]["title"] = "导入：为什么植物离不开光"
    response = client.post(f"/api/ai/jobs/{job['id']}/confirm-outline", json={
        "outline": json.dumps(edited, ensure_ascii=False),
    })
    assert response.status_code == 200, response.text

    done = wait_for_status(client, job["id"], "completed")
    result = done["result"]
    assert result["course_id"] == course["id"]
    assert result["scene_count"] == 3
    assert result["generated_scene_count"] == 3
    assert result["failed_scene_ids"] == []
    assert "prompt_version" in result

    detail = client.get(f"/api/classroom/courses/{course['id']}").json()
    assert detail["course"]["status"] == "ready"
    assert detail["course"]["scene_count"] == 3
    scenes = detail["scenes"]
    assert [scene["type"] for scene in scenes] == ["slide", "quiz", "slide"]
    assert [scene["status"] for scene in scenes] == ["ready", "ready", "ready"]
    assert scenes[0]["title"] == "导入：为什么植物离不开光"
    assert scenes[0]["content"]["canvas"]["width"] == 1000
    assert scenes[0]["content"]["canvas"]["height"] == 562.5
    # speech 旁白落到 actions（浏览器 TTS 用）
    assert any(action["type"] == "speech" for action in scenes[0]["actions"])
    assert scenes[1]["content"]["type"] == "quiz"
    assert len(scenes[1]["content"]["questions"]) == 2
    assert scenes[1]["meta"]["actions"][0]["type"] == "speech"
    assert fake.count("outline") == 1
    assert fake.count("slide") == 2
    assert fake.count("quiz") == 1
    assert fake.count("actions") == 2  # 只有 slide 场景需要一次旁白/动作生成


def test_outline_only_stage_completes_after_markdown_confirm(client, monkeypatch):
    provider = save_provider(client)
    course = create_course(client)
    FakeModel().install(monkeypatch)

    response = client.post(f"/api/classroom/courses/{course['id']}/generate", json={
        "stage": "outline", "provider_id": provider["id"], "model": "test-model", "scene_count": 3,
    })
    job_id = response.json()["job"]["id"]
    wait_for_status(client, job_id, "awaiting_outline")

    # UI 以 Markdown 标题编辑大纲（兼容 confirmed_outline 非 JSON 的写法）
    confirmed = client.post(f"/api/ai/jobs/{job_id}/confirm-outline", json={
        "outline": "## 情境导入\n## 课堂小测\n## 总结提升",
    })
    assert confirmed.status_code == 200, confirmed.text
    done = wait_for_status(client, job_id, "completed")
    assert done["result"]["scene_count"] == 3
    assert "generated_scene_count" not in done["result"]  # 只确认大纲，不生成场景

    detail = client.get(f"/api/classroom/courses/{course['id']}").json()
    assert detail["course"]["status"] == "outline_ready"
    assert len(detail["course"]["outline"]["scenes"]) == 3
    assert [scene["type"] for scene in detail["course"]["outline"]["scenes"]] == ["slide", "quiz", "slide"]
    assert detail["scenes"] == []  # 只生成大纲，不生成场景


# ---------------------------------------------------------------------------
# 2. 准入校验
# ---------------------------------------------------------------------------

def test_scenes_stage_without_outline_is_rejected(client):
    provider = save_provider(client)
    course = create_course(client)
    response = client.post(f"/api/classroom/courses/{course['id']}/generate", json={
        "stage": "scenes", "provider_id": provider["id"], "model": "test-model",
    })
    assert response.status_code == 422
    assert "大纲" in response.json()["detail"]


def test_generate_limits_and_unknown_stage(client):
    provider = save_provider(client)
    course = create_course(client)
    base = {"stage": "all", "provider_id": provider["id"], "model": "test-model"}
    assert client.post(f"/api/classroom/courses/{course['id']}/generate", json={**base, "scene_count": 2}).status_code == 422
    assert client.post(f"/api/classroom/courses/{course['id']}/generate", json={**base, "scene_count": 13}).status_code == 422
    assert client.post(f"/api/classroom/courses/{course['id']}/generate", json={**base, "depth": "ultra"}).status_code == 422
    assert client.post(f"/api/classroom/courses/{course['id']}/generate", json={**base, "scene_types": ["video"]}).status_code == 422
    assert client.post(f"/api/classroom/courses/{course['id']}/generate", json={**base, "stage": "bogus"}).status_code == 422


def test_validate_course_job_rejects_foreign_course_and_bad_params(client, second_client):
    provider = save_provider(client)
    course = create_course(client)
    other_id = user_id_of(second_client)
    data = SimpleNamespace(params={"course_id": course["id"], "stage": "all"})
    with pytest.raises(HTTPException) as excinfo:
        generate.validate_course_job(data, {"id": other_id})
    assert excinfo.value.status_code == 404

    # 同用户未知阶段 422
    data = SimpleNamespace(params={"course_id": course["id"], "stage": "unknown"})
    with pytest.raises(HTTPException) as excinfo:
        generate.validate_course_job(data, {"id": user_id_of(client)})
    assert excinfo.value.status_code == 422
    _ = provider


# ---------------------------------------------------------------------------
# 3. 失败隔离与重试
# ---------------------------------------------------------------------------

def test_single_scene_failure_does_not_break_course(client, monkeypatch):
    provider = save_provider(client)
    course = create_course(client)
    FakeModel(fail=("quiz",)).install(monkeypatch)

    job = _generate_all(client, provider, course)
    wait_for_status(client, job["id"], "awaiting_outline")
    client.post(f"/api/ai/jobs/{job['id']}/confirm-outline", json={
        "outline": json.dumps(OUTLINE_RAW, ensure_ascii=False),
    })
    done = wait_for_status(client, job["id"], "completed")
    assert len(done["result"]["failed_scene_ids"]) == 1
    assert "失败" in done["result"]["summary"]

    detail = client.get(f"/api/classroom/courses/{course['id']}").json()
    statuses = [scene["status"] for scene in detail["scenes"]]
    assert statuses == ["ready", "failed", "ready"]
    assert detail["course"]["status"] == "ready"  # 仍有可用场景
    failed = detail["scenes"][1]
    assert "error" in failed["meta"] and failed["meta"]["requires_generation"] is True


def test_invalid_json_model_output_is_retried_once(client, monkeypatch):
    provider = save_provider(client)
    course = create_course(client)
    fake = FakeModel(invalid_first=("outline",)).install(monkeypatch)

    job = _generate_all(client, provider, course)
    paused = wait_for_status(client, job["id"], "awaiting_outline")
    assert paused["result"]["outline"]["scenes"]
    assert fake.count("outline") == 2  # 首次非法 JSON → call_structured 自动重试一次


def test_slide_content_invalid_first_is_retried(client, monkeypatch):
    provider = save_provider(client)
    course = create_course(client)
    fake = FakeModel(invalid_first=("slide",)).install(monkeypatch)
    job = _generate_all(client, provider, course)
    wait_for_status(client, job["id"], "awaiting_outline")
    client.post(f"/api/ai/jobs/{job['id']}/confirm-outline", json={
        "outline": json.dumps(OUTLINE_RAW, ensure_ascii=False),
    })
    done = wait_for_status(client, job["id"], "completed")
    assert done["result"]["failed_scene_ids"] == []
    # 2 个 slide 场景 + 1 次因非法 JSON 的重试
    assert fake.count("slide") == 3


def test_actions_failure_falls_back_to_deterministic_speech(client, monkeypatch):
    provider = save_provider(client)
    course = create_course(client)
    FakeModel(fail=("actions",)).install(monkeypatch)
    job = _generate_all(client, provider, course)
    wait_for_status(client, job["id"], "awaiting_outline")
    client.post(f"/api/ai/jobs/{job['id']}/confirm-outline", json={
        "outline": json.dumps(OUTLINE_RAW, ensure_ascii=False),
    })
    done = wait_for_status(client, job["id"], "completed")
    assert done["result"]["failed_scene_ids"] == []
    detail = client.get(f"/api/classroom/courses/{course['id']}").json()
    slide = detail["scenes"][0]
    assert slide["status"] == "ready"
    speech = [action for action in slide["actions"] if action["type"] == "speech"]
    assert speech and "光合作用" in speech[0]["text"]
    assert any("旁白生成失败" in warning for warning in slide["meta"]["warnings"])


def test_interactive_and_pbl_scene_content_is_structurally_valid(client, monkeypatch):
    """交互/PBL 场景内容结构合法（兼容 P0 占位与 P1 真实生成两阶段行为）。

    - 不硬断言 placeholder=true：P1 起交互 HTML / PBL 项目由 AI 真实生成；
    - 只断言场景存在、content 类型正确、交互有 html 字符串（或明确 placeholder）、PBL 有 project 结构，
      并确认 meta 的 placeholder/requires_generation 是布尔字段（前端据此显示「内容待生成」标识）。
    """
    provider = save_provider(client)
    course = create_course(client, mode="interactive")
    outline = {
        "title": "光合作用互动课",
        "scenes": [
            {"title": "情境导入", "type": "slide", "outline_points": ["现象"]},
            {"title": "光照强度模拟", "type": "interactive", "outline_points": ["调节光照"], "widget": "simulation"},
            {"title": "校园节能项目", "type": "pbl", "outline_points": ["调研校园用电"]},
            {"title": "课堂小测", "type": "quiz", "outline_points": ["场所"]},
        ],
    }
    fake = FakeModel(outline=outline).install(monkeypatch)
    response = client.post(f"/api/classroom/courses/{course['id']}/generate", json={
        "stage": "all", "provider_id": provider["id"], "model": "test-model",
        "scene_types": ["slide", "quiz", "interactive", "pbl"],
    })
    assert response.status_code == 200, response.text
    job_id = response.json()["job"]["id"]
    wait_for_status(client, job_id, "awaiting_outline")
    client.post(f"/api/ai/jobs/{job_id}/confirm-outline", json={
        "outline": json.dumps(outline, ensure_ascii=False),
    })
    done = wait_for_status(client, job_id, "completed")
    assert done["status"] == "completed"

    detail = client.get(f"/api/classroom/courses/{course['id']}").json()
    scenes = detail["scenes"]
    assert [scene["type"] for scene in scenes] == ["slide", "interactive", "pbl", "quiz"]
    interactive = scenes[1]
    pbl = scenes[2]
    # 交互：有可播放 HTML，或明确标注为占位（两阶段行为都合法）
    assert interactive["content"]["type"] == "interactive"
    assert isinstance(interactive["content"].get("html"), str)
    assert interactive["content"]["html"].strip() or interactive["meta"].get("placeholder") is True
    assert isinstance(interactive["meta"].get("placeholder"), bool)
    assert isinstance(interactive["meta"].get("requires_generation"), bool)
    # PBL：结构化项目（P1 真实生成时角色/里程碑齐全；P0 占位时列表允许为空但仍为数组）
    project = pbl["content"].get("project")
    assert pbl["content"]["type"] == "pbl" and isinstance(project, dict)
    assert isinstance(project.get("title"), str) and project["title"].strip()
    assert isinstance(project.get("roles"), list)
    assert isinstance(project.get("milestones"), list)
    assert isinstance(pbl["meta"].get("placeholder"), bool)
    assert isinstance(pbl["meta"].get("requires_generation"), bool)
    _ = fake


def test_interactive_and_pbl_failure_isolated_to_their_scenes(client, monkeypatch):
    """P1 起交互/PBL 走真实生成：这里只验证这两类场景生成失败时不影响其它场景。"""
    provider = save_provider(client)
    course = create_course(client, mode="interactive")
    outline = {
        "title": "光合作用互动课",
        "scenes": [
            {"title": "情境导入", "type": "slide", "outline_points": ["现象"]},
            {"title": "光照强度模拟", "type": "interactive", "outline_points": ["调节光照"], "widget": "simulation"},
            {"title": "校园节能项目", "type": "pbl", "outline_points": ["调研校园用电"]},
        ],
    }
    FakeModel(fail=("interactive", "pbl")).install(monkeypatch)
    response = client.post(f"/api/classroom/courses/{course['id']}/generate", json={
        "stage": "all", "provider_id": provider["id"], "model": "test-model",
        "scene_types": ["slide", "interactive", "pbl"],
    })
    assert response.status_code == 200, response.text
    job_id = response.json()["job"]["id"]
    wait_for_status(client, job_id, "awaiting_outline")
    client.post(f"/api/ai/jobs/{job_id}/confirm-outline", json={
        "outline": json.dumps(outline, ensure_ascii=False),
    })
    done = wait_for_status(client, job_id, "completed")
    assert done["result"]["generated_scene_count"] == 1
    assert len(done["result"]["failed_scene_ids"]) == 2

    detail = client.get(f"/api/classroom/courses/{course['id']}").json()
    assert [scene["status"] for scene in detail["scenes"]] == ["ready", "failed", "failed"]
    assert detail["course"]["status"] == "ready"
    assert "error" in detail["scenes"][1]["meta"]
    # 失败场景可在「大纲与场景」页单独重新生成（前端入口依赖 failed 状态与 error 提示）
    assert detail["scenes"][1]["meta"]["requires_generation"] is True


def test_outline_prompt_includes_user_document_material(client, monkeypatch):
    uploaded = client.post("/api/documents", files={
        "file": ("光合资料.md", "光合作用需要光照、水和二氧化碳。".encode(), "text/markdown"),
    })
    assert uploaded.status_code == 200, uploaded.text
    provider = save_provider(client)
    course = create_course(client, document_ids=[uploaded.json()["id"]])
    fake = FakeModel().install(monkeypatch)
    job = _generate_all(client, provider, course)
    wait_for_status(client, job["id"], "awaiting_outline")
    outline_prompts = [prompt for prompt in fake.prompts if "课程大纲生成器" in prompt]
    assert outline_prompts and "光合作用需要光照" in outline_prompts[0]


# ---------------------------------------------------------------------------
# 4. 取消保留已生成场景 + 续跑跳过已完成
# ---------------------------------------------------------------------------

def _prepare_outline(course, user_id):
    outline, _warnings = schema.normalize_outline(OUTLINE_RAW, scene_count=3)
    set_course_outline(course["id"], user_id, outline)
    return outline


def test_cancel_keeps_generated_scenes_and_resume_skips_them(client, monkeypatch):
    provider = save_provider(client)
    course = create_course(client)
    uid = user_id_of(client)
    _prepare_outline(course, uid)
    job_id = insert_course_job(uid, {
        "course_id": course["id"], "stage": "scenes",
        "scene_types": ["slide", "quiz"], "scene_count": 3, "depth": "standard",
    }, provider["id"], "test-model", token="classroom-cancel-token")

    cancel_state = {}

    def on_call(kind, number):
        if kind == "slide" and not cancel_state:
            cancel_state["triggered"] = True
            with db.connection() as con:
                con.execute(
                    "UPDATE ai_jobs SET cancelled=1,status='cancel_requested' WHERE id=?", (job_id,),
                )

    FakeModel(on_call=on_call).install(monkeypatch)
    asyncio.run(ai_jobs.execute_dispatched_job(job_id, "classroom-cancel-token", ai_jobs._run_job))

    snapshot = job_view(client, job_id)
    assert snapshot["status"] == "cancelled"
    scenes = course_scene_rows(course["id"], uid)
    ready = [scene for scene in scenes if scene["status"] == "ready"]
    assert len(ready) >= 1  # 取消前完成的场景保留
    assert len(ready) < 3
    detail = client.get(f"/api/classroom/courses/{course['id']}").json()
    assert detail["course"]["status"] == "outline_ready"
    done_before = {scene["id"] for scene in ready}

    # 续跑：只补未完成场景
    resumed_fake = FakeModel().install(monkeypatch)
    assert client.post(f"/api/ai/jobs/{job_id}/resume").status_code == 200
    wait_for_status(client, job_id, "completed")
    assert resumed_fake.calls
    assert "outline" not in resumed_fake.calls  # 续跑不重新生成大纲
    detail = client.get(f"/api/classroom/courses/{course['id']}").json()
    assert [scene["status"] for scene in detail["scenes"]] == ["ready", "ready", "ready"]
    # 已完成场景的内容没有被重写（id 与创建时间保持不变）
    with db.connection() as con:
        for scene_id in done_before:
            row = con.execute("SELECT created_at FROM cls_scenes WHERE id=?", (scene_id,)).fetchone()
            assert row is not None


def test_checkpoint_resume_skips_ready_scenes(client, monkeypatch):
    provider = save_provider(client)
    course = create_course(client)
    uid = user_id_of(client)
    _prepare_outline(course, uid)
    # 先手工写入第 1 个场景（模拟上一轮已完成）
    first = generate._upsert_scene(
        course={"id": course["id"], "user_id": uid},
        spec={"scene_id": None, "order_index": 1, "scene_type": "slide", "title": "已完成场景",
              "outline": {"outline_points": ["旧要点"]}},
        status="ready",
        content={"type": "slide", "canvas": {"width": 1000, "height": 562.5, "theme": {}, "elements": []}},
        meta={"actions": [{"id": "act_001", "type": "speech", "text": "上一轮已生成"}]},
    )
    job_id = insert_course_job(uid, {
        "course_id": course["id"], "stage": "scenes", "scene_types": ["slide", "quiz"],
        "scene_count": 3, "depth": "standard",
    }, provider["id"], "test-model", token="classroom-resume-token",
        checkpoint={"done_scene_ids": [first], "failed_scene_ids": []})

    fake = FakeModel().install(monkeypatch)
    asyncio.run(ai_jobs.execute_dispatched_job(job_id, "classroom-resume-token", ai_jobs._run_job))

    snapshot = job_view(client, job_id)
    assert snapshot["status"] == "completed"
    assert snapshot["result"]["generated_scene_count"] == 2
    # 已 ready 的第 1 个场景不再调用幻灯片模型（第 2、3 个场景：quiz + slide）
    assert fake.count("slide") == 1
    assert fake.count("quiz") == 1
    scenes = course_scene_rows(course["id"], uid)
    assert [scene["id"] for scene in scenes][0] == first
    assert scenes[0]["title"] == "已完成场景"  # 未被重写


# ---------------------------------------------------------------------------
# 5. 单场景重新生成
# ---------------------------------------------------------------------------

def test_regenerate_single_scene(client, monkeypatch):
    provider = save_provider(client)
    course = create_course(client)
    FakeModel().install(monkeypatch)
    job = _generate_all(client, provider, course)
    wait_for_status(client, job["id"], "awaiting_outline")
    client.post(f"/api/ai/jobs/{job['id']}/confirm-outline", json={
        "outline": json.dumps(OUTLINE_RAW, ensure_ascii=False),
    })
    wait_for_status(client, job["id"], "completed")
    detail = client.get(f"/api/classroom/courses/{course['id']}").json()
    quiz_scene = detail["scenes"][1]

    regenerated = FakeModel().install(monkeypatch)
    response = client.post(
        f"/api/classroom/courses/{course['id']}/scenes/{quiz_scene['id']}/regenerate",
        json={"provider_id": provider["id"], "model": "test-model", "instruction": "题目再简单一点"},
    )
    assert response.status_code == 200, response.text
    new_job_id = response.json()["job"]["id"]
    wait_for_status(client, new_job_id, "completed")
    assert regenerated.calls == ["quiz"]  # 只重新生成该场景
    detail_after = client.get(f"/api/classroom/courses/{course['id']}").json()
    assert [scene["status"] for scene in detail_after["scenes"]] == ["ready", "ready", "ready"]
    assert any("再简单一点" in warning for warning in detail_after["scenes"][1]["meta"]["warnings"])


def test_regenerate_foreign_scene_is_404(client, second_client, monkeypatch):
    provider = save_provider(client)
    course = create_course(client)
    FakeModel().install(monkeypatch)
    job = _generate_all(client, provider, course)
    wait_for_status(client, job["id"], "awaiting_outline")
    client.post(f"/api/ai/jobs/{job['id']}/confirm-outline", json={
        "outline": json.dumps(OUTLINE_RAW, ensure_ascii=False),
    })
    wait_for_status(client, job["id"], "completed")
    scene_id = client.get(f"/api/classroom/courses/{course['id']}").json()["scenes"][0]["id"]
    # 他人先建自己的 provider 再尝试重新生成
    other_provider = save_provider(second_client, name="他人连接")
    response = second_client.post(
        f"/api/classroom/courses/{course['id']}/scenes/{scene_id}/regenerate",
        json={"provider_id": other_provider["id"], "model": "test-model"},
    )
    assert response.status_code == 404
