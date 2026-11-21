"""AI 课堂 P1/P2 增量验收：深度交互模式生成 / 课程任务自动生成名册 / 可选配图（image slot）。

不联网：文本模型全部 monkeypatch ``app.ai._call_model``；图片接口 monkeypatch
``app.classroom.images._request_image``（不访问外网）。覆盖 interactive 场景数量约束、
slide「动手试一试」提示块、名册自动生成成功/失败不阻塞/幂等、ZIP 导出导入携带名册与图片、
配图成功入 cls_assets 与元素引用、无配置不生成、显式关闭、失败降级、取消与续跑。
"""
import asyncio
import io
import json
import time
import zipfile

from fastapi.testclient import TestClient
import pytest

from app import ai, db
from app.ai import jobs as ai_jobs
from app.classroom import db as cdb
from app.classroom import exports, generate, images, multiagent, prompts, schema, tts


# ---------------------------------------------------------------------------
# 假模型 / 假图片接口
# ---------------------------------------------------------------------------

OUTLINE_RAW = {
    "title": "光合作用入门",
    "description": "面向初中生物的光合作用原理入门课。",
    "scenes": [
        {"title": "为什么植物离不开光", "type": "slide", "outline_points": ["生活现象", "学习目标"]},
        {"title": "课堂小测", "type": "quiz", "outline_points": ["场所", "原料"],
         "quiz": {"question_count": 2, "difficulty": "medium"}},
        {"title": "原料与产物", "type": "slide", "outline_points": ["水是原料", "二氧化碳是原料"]},
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
     "options": [{"label": "水", "value": "A"}, {"label": "氧气", "value": "B"},
                 {"label": "二氧化碳", "value": "C"}],
     "answer": ["A", "C"], "analysis": "水和二氧化碳是原料。", "points": 10},
]

ACTIONS_RAW = [
    {"type": "action", "name": "spotlight", "params": {"elementId": "text_001"}},
    {"type": "text", "content": "这一页我们来看光合作用。"},
    {"type": "action", "name": "spotlight", "params": {"elementId": "text_002"}},
    {"type": "text", "content": "水与二氧化碳是光合作用的原料。"},
]

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

ROSTER_RAW = {
    "teacher": {"name": "林老师", "persona": "善于用追问推进推导，讲题时先让学生猜再纠正。"},
    "classmates": [
        {"name": "小舟", "persona": "好奇心强，常问“为什么”，但计算容易粗心。"},
        {"name": "小雨", "persona": "喜欢先记结论，需要老师带着看推导过程。"},
    ],
}

#: 1×1 PNG（真实图片字节；用于配图成功路径与内容寻址去重）。
TINY_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d4944415478da63f8ff1f040005fe02fea72d1e1b0000000049454e44ae426082"
)

_MARKERS = (
    ("课程大纲生成器", "outline"),
    ("幻灯片内容生成器", "slide"),
    ("测验内容生成器", "quiz"),
    ("课堂旁白与动作生成器", "actions"),
    ("交互场景 HTML 生成器", "interactive"),
    ("项目式学习（PBL）设计器", "pbl"),
    ("课堂角色名册生成", "roster"),
)


class FakeModel:
    """按 prompt 标记返回固定 JSON；支持失败/取消钩子。"""

    def __init__(self, *, outline=None, slide=None, quiz=None, actions=None,
                 interactive=None, roster=None, fail=(), on_call=None):
        self.payloads = {
            "outline": json.dumps(outline if outline is not None else OUTLINE_RAW, ensure_ascii=False),
            "slide": json.dumps(slide if slide is not None else SLIDE_RAW, ensure_ascii=False),
            "quiz": json.dumps(quiz if quiz is not None else QUIZ_RAW, ensure_ascii=False),
            "actions": json.dumps(actions if actions is not None else ACTIONS_RAW, ensure_ascii=False),
            "interactive": json.dumps(interactive if interactive is not None else INTERACTIVE_RAW,
                                      ensure_ascii=False),
            "roster": json.dumps(roster if roster is not None else ROSTER_RAW, ensure_ascii=False),
        }
        self.fail = set(fail)
        self.on_call = on_call
        self.calls = []
        self.prompts = []

    def classify(self, prompt):
        for marker, kind in _MARKERS:
            if marker in prompt:
                return kind
        raise AssertionError("未知 prompt：%s" % prompt[:200])

    def count(self, kind):
        return self.calls.count(kind)

    def prompts_of(self, kind):
        return [prompt for prompt, call_kind in zip(self.prompts, self.calls) if call_kind == kind]

    def install(self, monkeypatch):
        async def fake_call(provider, model, prompt, images_=None, json_output=False,
                            token_limit=5000, **kwargs):
            kind = self.classify(prompt)
            self.calls.append(kind)
            self.prompts.append(prompt)
            if self.on_call:
                self.on_call(kind, len(self.calls))
            if kind in self.fail:
                raise ValueError("模拟模型失败：%s" % kind)
            return {"content": self.payloads[kind], "model": model,
                    "provider_name": "mock", "usage": {"total_tokens": 12}}

        monkeypatch.setattr(ai, "_call_model", fake_call)
        return self


class FakeImage:
    """假图片接口：记录调用、可返回固定 PNG 或抛错、可做取消钩子。"""

    def __init__(self, payload=None, *, fail=False, on_call=None):
        self.payload = payload if payload is not None else TINY_PNG
        self.fail = fail
        self.on_call = on_call
        self.calls = []

    async def __call__(self, provider, model, prompt, *, size=images.DEFAULT_SIZE):
        self.calls.append({"model": model, "prompt": prompt, "size": size})
        if self.on_call:
            self.on_call(len(self.calls))
        if self.fail:
            raise ValueError("模拟图片服务不可用")
        return self.payload, "image/png"

    def install(self, monkeypatch):
        monkeypatch.setattr(images, "_request_image", self)
        return self


def save_provider(client, name="课堂增量测试连接", models=("test-model",)):
    response = client.post("/api/providers", json={
        "name": name, "kind": "openai", "base_url": "https://api.openai.com/v1",
        "api_key": "sk-classroom-plus-unit-test", "models": list(models),
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


def wait_for_status(client, job_id, status, tries=400, delay=0.02):
    job = None
    for _ in range(tries):
        job = job_view(client, job_id)
        if job["status"] == status:
            return job
        if job["status"] in ("failed", "cancelled") and status not in (job["status"],):
            pytest.fail(f"任务提前进入 {job['status']}（期望 {status}）：{job.get('error')}")
        time.sleep(delay)
    pytest.fail(f"任务未进入 {status}，当前 {job}")


def start_generate(client, provider, course, **overrides):
    payload = {
        "stage": "all", "provider_id": provider["id"], "model": "test-model",
        "scene_count": 3,
    }
    payload.update(overrides)
    response = client.post(f"/api/classroom/courses/{course['id']}/generate", json=payload)
    assert response.status_code == 200, response.text
    return response.json()["job"]


def confirm_outline(client, job_id):
    paused = job_view(client, job_id)
    outline = paused["result"]["outline"]
    response = client.post(f"/api/ai/jobs/{job_id}/confirm-outline", json={
        "outline": json.dumps(outline, ensure_ascii=False),
    })
    assert response.status_code == 200, response.text
    return outline


def run_generate_all(client, provider, course, **overrides):
    job = start_generate(client, provider, course, **overrides)
    wait_for_status(client, job["id"], "awaiting_outline")
    confirm_outline(client, job["id"])
    return wait_for_status(client, job["id"], "completed")


def course_detail(client, course_id):
    response = client.get(f"/api/classroom/courses/{course_id}")
    assert response.status_code == 200, response.text
    return response.json()


def scene_of_type(detail, scene_type, index=0):
    items = [scene for scene in detail["scenes"] if scene["type"] == scene_type]
    assert len(items) > index, [scene["type"] for scene in detail["scenes"]]
    return items[index]


def image_elements(scene):
    elements = ((scene.get("content") or {}).get("canvas") or {}).get("elements") or []
    return [element for element in elements if element.get("type") == "image"]


def image_assets(course_id=None, user_id=None):
    with db.connection() as con:
        sql = "SELECT * FROM cls_assets WHERE kind='image'"
        params = []
        if course_id is not None:
            sql += " AND course_id=?"
            params.append(course_id)
        if user_id is not None:
            sql += " AND user_id=?"
            params.append(user_id)
        return [dict(row) for row in con.execute(sql + " ORDER BY id", tuple(params)).fetchall()]


def insert_course_job(user_id, params, provider_id, model, *, token="classroom-plus-token"):
    now = db.utcnow()
    request = {"task": "course", "provider_id": provider_id, "model": model, "params": params,
               "requirements": ""}
    with db.connection() as con:
        return con.execute(
            "INSERT INTO ai_jobs(user_id,task,status,stage,request_json,partial_content,cancelled,"
            "created_at,updated_at,dispatch_token,checkpoint_json) VALUES (?,?,?,?,?,'',0,?,?,?,NULL)",
            (user_id, "course", "queued", "queued", json.dumps(request, ensure_ascii=False), now, now, token),
        ).lastrowid


def set_course_outline(course_id, user_id, outline, *, status="outline_ready"):
    with db.connection() as con:
        con.execute(
            "UPDATE cls_courses SET outline_json=?, status=?, updated_at=? WHERE id=? AND user_id=?",
            (cdb.json_dump(outline), status, db.utcnow(), course_id, user_id),
        )


# ---------------------------------------------------------------------------
# 1. 深度交互模式：大纲约束 / widget / slide 提示块
# ---------------------------------------------------------------------------

def test_interactive_mode_outline_clips_extra_interactive_scenes(client, monkeypatch):
    provider = save_provider(client)
    course = create_course(client, mode="interactive")
    outline = {
        "title": "光合作用互动课",
        "scenes": [
            {"title": "光照模拟", "type": "interactive", "outline_points": ["调节光照"],
             "widget": "simulation"},
            {"title": "概念关系图", "type": "interactive", "outline_points": ["梳理概念"],
             "widget": "mindmap"},
            {"title": "分类挑战", "type": "interactive", "outline_points": ["拖动分类"],
             "widget": "game"},
            {"title": "总结提升", "type": "slide", "outline_points": ["回顾"]},
        ],
    }
    fake = FakeModel(outline=outline).install(monkeypatch)
    job = start_generate(client, provider, course, scene_count=4,
                         scene_types=["slide", "quiz", "interactive"])
    paused = wait_for_status(client, job["id"], "awaiting_outline")
    result_outline = paused["result"]["outline"]
    types = [scene["type"] for scene in result_outline["scenes"]]
    assert types == ["interactive", "interactive", "slide", "slide"]
    assert result_outline.get("interactive_mode") is True
    assert any("最多 2 个 interactive" in item for item in result_outline["warnings"])
    # 大纲 prompt 走「交互优先」分支（widget 选择表 + 1-2 个要求）
    outline_prompt = fake.prompts_of("outline")[0]
    assert "interactive-first" in outline_prompt and "widget 选择表" in outline_prompt
    assert "至少 1 个、最多 2 个" in outline_prompt
    # 落库到课程大纲
    detail = course_detail(client, course["id"])
    assert [scene["type"] for scene in detail["course"]["outline"]["scenes"]] == types


def test_interactive_mode_widget_default_and_missing_warning(client, monkeypatch):
    provider = save_provider(client)
    course = create_course(client, mode="interactive")
    outline = {
        "title": "光合作用互动课",
        "scenes": [
            {"title": "光照模拟", "type": "interactive", "outline_points": ["调节光照"]},
            {"title": "概念梳理", "type": "slide", "outline_points": ["回顾概念"]},
            {"title": "总结", "type": "slide", "outline_points": ["回顾"]},
        ],
    }
    FakeModel(outline=outline).install(monkeypatch)
    job = start_generate(client, provider, course, scene_count=3,
                         scene_types=["slide", "interactive"])
    paused = wait_for_status(client, job["id"], "awaiting_outline")
    scenes = paused["result"]["outline"]["scenes"]
    assert scenes[0]["type"] == "interactive" and scenes[0]["widget"] == "simulation"
    assert any("缺少 widget" in item for item in paused["result"]["outline"]["warnings"])


def test_interactive_mode_zero_interactive_warns(client, monkeypatch):
    provider = save_provider(client)
    course = create_course(client, mode="interactive")
    outline = {
        "title": "只有讲解的互动课",
        "scenes": [
            {"title": "导入", "type": "slide", "outline_points": ["现象"]},
            {"title": "展开", "type": "slide", "outline_points": ["概念"]},
            {"title": "总结", "type": "slide", "outline_points": ["回顾"]},
        ],
    }
    FakeModel(outline=outline).install(monkeypatch)
    job = start_generate(client, provider, course, scene_count=3, scene_types=["slide", "interactive"])
    paused = wait_for_status(client, job["id"], "awaiting_outline")
    assert any("建议至少 1 个 interactive" in item
               for item in paused["result"]["outline"]["warnings"])


def test_interactive_mode_slide_hint_and_course_flags(client, monkeypatch):
    provider = save_provider(client)
    course = create_course(client, mode="interactive")
    outline = {
        "title": "光合作用互动课",
        "scenes": [
            {"title": "情境导入", "type": "slide", "outline_points": ["生活现象", "学习目标"]},
            {"title": "光照强度模拟", "type": "interactive", "outline_points": ["调节光照"],
             "widget": "simulation"},
            {"title": "总结提升", "type": "slide", "outline_points": ["知识回顾"]},
        ],
    }
    fake = FakeModel(outline=outline).install(monkeypatch)
    done = run_generate_all(client, provider, course, scene_count=3,
                            scene_types=["slide", "interactive"])
    assert done["status"] == "completed"
    detail = course_detail(client, course["id"])
    assert detail["course"]["interactive_mode"] is True
    assert detail["course"]["config"].get("interactive_mode") is True
    listing = client.get("/api/classroom/courses").json()
    assert listing["items"][0]["interactive_mode"] is True
    meta = client.get("/api/classroom/meta").json()
    assert meta["interactive_mode"]["enabled"] is True
    assert meta["interactive_mode"]["interactive_scenes_max"] == generate.MAX_INTERACTIVE_SCENES

    slide = scene_of_type(detail, "slide")
    hint = slide["content"].get("interactive_hint")
    assert isinstance(hint, dict) and hint.get("element_id", "").startswith("hint_")
    assert "动手试一试" in hint["text"]
    assert hint["element_id"] in [element["id"] for element in slide["content"]["canvas"]["elements"]]
    assert slide["meta"].get("interactive_hint", {}).get("element_id") == hint["element_id"]
    assert any("动手试一试" in item for item in slide["meta"]["warnings"])
    # slide prompt 包含提示块要求
    slide_prompts = fake.prompts_of("slide")
    assert slide_prompts and all("动手试一试" in prompt for prompt in slide_prompts)
    # 交互场景照常真实生成（有 HTML、非占位）
    interactive_scene = scene_of_type(detail, "interactive")
    assert interactive_scene["content"]["html"].strip()
    assert interactive_scene["content"]["widget"] == "simulation"


def test_standard_mode_has_no_interactive_hint(client, monkeypatch):
    provider = save_provider(client)
    course = create_course(client)
    fake = FakeModel().install(monkeypatch)
    run_generate_all(client, provider, course, scene_count=3, scene_types=["slide", "quiz"])
    detail = course_detail(client, course["id"])
    assert detail["course"]["interactive_mode"] is False
    slide = scene_of_type(detail, "slide")
    assert "interactive_hint" not in slide["content"]
    assert not any("动手试一试" in prompt for prompt in fake.prompts_of("slide"))
    assert client.get("/api/classroom/meta").json()["interactive_mode"]["enabled"] is True


# ---------------------------------------------------------------------------
# 2. 课程任务自动生成角色名册
# ---------------------------------------------------------------------------

def test_course_job_auto_generates_roster(client, monkeypatch):
    provider = save_provider(client)
    course = create_course(client)
    fake = FakeModel().install(monkeypatch)
    done = run_generate_all(client, provider, course, scene_count=3, scene_types=["slide", "quiz"])
    result = done["result"]
    assert result["roster_generated"] is True
    assert result["roster_agent_count"] == 3
    assert result["roster_warnings"] == []
    assert fake.count("roster") == 1
    agents = client.get(f"/api/classroom/courses/{course['id']}/agents").json()
    assert agents["has_roster"] is True
    assert [agent["name"] for agent in agents["agents"]] == ["林老师", "小舟", "小雨"]
    config = course_detail(client, course["id"])["course"]["config"]
    assert config["agents_meta"]["prompt_version"] == multiagent.PROMPT_VERSION
    roster_prompt = fake.prompts_of("roster")[0]
    assert "课堂角色名册生成" in roster_prompt and "persona" in roster_prompt


def test_course_job_roster_failure_does_not_block(client, monkeypatch):
    provider = save_provider(client)
    course = create_course(client)
    FakeModel(fail=("roster",)).install(monkeypatch)
    done = run_generate_all(client, provider, course, scene_count=3, scene_types=["slide", "quiz"])
    result = done["result"]
    assert result["roster_generated"] is False
    assert result["roster_warnings"]
    assert "角色名册自动生成失败" in result["summary"]
    detail = course_detail(client, course["id"])
    assert [scene["status"] for scene in detail["scenes"]] == ["ready"] * 3
    assert client.get(f"/api/classroom/courses/{course['id']}/agents").json()["has_roster"] is False

    # 失败后用户仍可手动生成名册
    FakeModel().install(monkeypatch)
    response = client.post(f"/api/classroom/courses/{course['id']}/agents/generate", json={})
    assert response.status_code == 200
    assert response.json()["has_roster"] is True


def test_course_job_roster_is_idempotent(client, monkeypatch):
    provider = save_provider(client)
    course = create_course(client)
    FakeModel().install(monkeypatch)
    generated = client.post(f"/api/classroom/courses/{course['id']}/agents/generate", json={})
    assert generated.status_code == 200
    first_teacher = generated.json()["roster"]["teacher"]["name"]
    # 生成任务里的另一个名册 payload 不应被使用（已有名册 → 跳过自动生成）
    changed = {
        "teacher": {"name": "另一位老师", "persona": "不应该出现在这里。"},
        "classmates": [{"name": "同学甲", "persona": "也不应出现。"}],
    }
    fake = FakeModel(roster=changed).install(monkeypatch)
    run_generate_all(client, provider, course, scene_count=3, scene_types=["slide", "quiz"])
    assert fake.count("roster") == 0
    agents = client.get(f"/api/classroom/courses/{course['id']}/agents").json()
    assert agents["roster"]["teacher"]["name"] == first_teacher


def test_roster_export_import_roundtrip(client, monkeypatch):
    provider = save_provider(client)
    course = create_course(client)
    FakeModel().install(monkeypatch)
    assert client.post(f"/api/classroom/courses/{course['id']}/agents/generate", json={}).status_code == 200
    uid = user_id_of(client)
    from app.classroom import interactive
    slide_content, _ = schema.normalize_slide_content(SLIDE_RAW)
    now = db.utcnow()
    with db.connection() as con:
        con.execute(
            "INSERT INTO cls_scenes(course_id,user_id,scene_type,order_index,title,content_json,meta_json,"
            "status,created_at,updated_at) VALUES (?,?,?,?,?,?,?,'ready',?,?)",
            (course["id"], uid, "slide", 1, "光合作用", cdb.json_dump(slide_content),
             cdb.json_dump({"actions": [{"id": "act_001", "type": "speech", "text": "开场"}]}), now, now),
        )
    exported = client.get(f"/api/classroom/courses/{course['id']}/export", params={"format": "zip"})
    assert exported.status_code == 200, exported.text
    with zipfile.ZipFile(io.BytesIO(exported.content)) as archive:
        manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
    assert manifest["agents"]["teacher"]["name"] == "林老师"
    assert manifest["agentsPromptVersion"] == multiagent.PROMPT_VERSION
    assert "agents" in manifest["capabilities"]

    imported = client.post("/api/classroom/import", files={
        "file": ("课程.zip", exported.content, "application/zip"),
    })
    assert imported.status_code == 200, imported.text
    new_id = imported.json()["course"]["id"]
    agents = client.get(f"/api/classroom/courses/{new_id}/agents").json()
    assert agents["has_roster"] is True
    assert agents["roster"]["teacher"]["name"] == "林老师"
    assert [agent["name"] for agent in agents["roster"]["classmates"]] == ["小舟", "小雨"]
    config = imported.json()["course"]["config"]
    assert config["agents_meta"]["prompt_version"] == multiagent.PROMPT_VERSION
    assert config["agents_meta"]["imported"] is True


# ---------------------------------------------------------------------------
# 3. 可选配图（image slot）
# ---------------------------------------------------------------------------

def test_image_generation_success_assets_elements_and_dedupe(client, monkeypatch):
    provider = save_provider(client, models=("test-model", "gpt-image-1"))
    course = create_course(client)
    FakeModel().install(monkeypatch)
    fake_image = FakeImage().install(monkeypatch)

    done = run_generate_all(client, provider, course, scene_count=3, scene_types=["slide", "quiz"],
                            images=True, images_per_scene=2)
    assert done["status"] == "completed"
    assert len(fake_image.calls) == 4  # 2 张 slide × 2 张配图
    for call in fake_image.calls:
        assert call["model"] == "gpt-image-1"
        assert "禁止" in call["prompt"] and "文字" in call["prompt"]

    detail = course_detail(client, course["id"])
    slide = scene_of_type(detail, "slide")
    elements = image_elements(slide)
    assert len(elements) == 2
    assert all(element["src"].startswith("/api/classroom/assets/") for element in elements)
    assert all(element.get("asset_id") for element in elements)
    assert len(slide["content"]["image_assets"]) == 2
    assert [item["element_id"] for item in slide["content"]["image_assets"]] == \
           [element["id"] for element in elements]
    assert len(slide["meta"]["images"]) == 2
    # 同字节内容寻址去重：本测所有配图字节一致 → cls_assets 只有 1 行
    assets = image_assets(course["id"])
    assert len(assets) == 1 and assets[0]["mime_type"] == "image/png"
    asset_id = int(assets[0]["id"])
    for element in elements:
        assert int(element["asset_id"]) == asset_id
    served = client.get(f"/api/classroom/assets/{asset_id}")
    assert served.status_code == 200
    assert served.headers["content-type"].startswith("image/png")
    # /meta 暴露可用配图连接
    meta = client.get("/api/classroom/meta").json()
    assert meta["images"]["enabled"] is True
    assert meta["images"]["model"] == "gpt-image-1"
    assert meta["limits"]["images_per_scene_max"] == images.MAX_IMAGES_PER_SCENE


def test_image_generation_auto_without_capable_model(client, monkeypatch):
    provider = save_provider(client)  # 只有 test-model，无图片能力
    course = create_course(client)
    FakeModel().install(monkeypatch)
    fake_image = FakeImage().install(monkeypatch)
    done = run_generate_all(client, provider, course, scene_count=3, scene_types=["slide", "quiz"])
    assert done["status"] == "completed"
    assert fake_image.calls == []
    detail = course_detail(client, course["id"])
    slide = scene_of_type(detail, "slide")
    assert image_elements(slide) == []
    assert image_assets(course["id"]) == []
    assert not any("配图" in item for item in slide["meta"]["warnings"])
    assert client.get("/api/classroom/meta").json()["images"]["enabled"] is False


def test_image_generation_explicit_off_and_admission_validation(client, second_client, monkeypatch):
    provider = save_provider(client, models=("test-model", "gpt-image-1"))
    course = create_course(client)
    FakeModel().install(monkeypatch)
    fake_image = FakeImage().install(monkeypatch)
    done = run_generate_all(client, provider, course, scene_count=3, scene_types=["slide", "quiz"],
                            images=False)
    assert done["status"] == "completed"
    assert fake_image.calls == []
    assert image_elements(scene_of_type(course_detail(client, course["id"]), "slide")) == []

    # 显式开启但该用户没有可用图片连接 → 422 引导（用另一个只配置纯文本连接的用户验证）
    plain_provider = save_provider(second_client, name="纯文本连接")
    plain_course = create_course(second_client, topic="细胞结构")
    response = second_client.post(f"/api/classroom/courses/{plain_course['id']}/generate", json={
        "stage": "all", "provider_id": plain_provider["id"], "model": "test-model", "images": True,
    })
    assert response.status_code == 422
    assert "图片生成" in response.json()["detail"]
    # 每场景配图数量越界 / 只给 provider 不给 model → 422
    base = {"stage": "all", "provider_id": provider["id"], "model": "test-model"}
    assert client.post(f"/api/classroom/courses/{course['id']}/generate",
                       json={**base, "images_per_scene": 3}).status_code == 422
    assert client.post(f"/api/classroom/courses/{course['id']}/generate",
                       json={**base, "image_provider_id": provider["id"]}).status_code == 422


def test_image_generation_failure_degrades_without_blocking(client, monkeypatch):
    provider = save_provider(client, models=("test-model", "gpt-image-1"))
    course = create_course(client)
    FakeModel().install(monkeypatch)
    FakeImage(fail=True).install(monkeypatch)
    done = run_generate_all(client, provider, course, scene_count=3, scene_types=["slide", "quiz"],
                            images=True)
    assert done["status"] == "completed"
    detail = course_detail(client, course["id"])
    slide = scene_of_type(detail, "slide")
    assert image_elements(slide) == []
    assert image_assets(course["id"]) == []
    assert any("配图生成失败" in item for item in slide["meta"]["warnings"])
    assert slide["status"] == "ready"


def test_zip_roundtrip_remaps_image_assets(client, monkeypatch):
    provider = save_provider(client, models=("test-model", "gpt-image-1"))
    course = create_course(client)
    FakeModel().install(monkeypatch)
    FakeImage().install(monkeypatch)
    run_generate_all(client, provider, course, scene_count=3, scene_types=["slide", "quiz"],
                     images=True, images_per_scene=1)
    detail = course_detail(client, course["id"])
    slide = scene_of_type(detail, "slide")
    original_element = image_elements(slide)[0]

    exported = client.get(f"/api/classroom/courses/{course['id']}/export", params={"format": "zip"})
    assert exported.status_code == 200, exported.text
    with zipfile.ZipFile(io.BytesIO(exported.content)) as archive:
        manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
        names = set(archive.namelist())
        image_res = next(item for item in manifest["resources"] if item["kind"] == "image")
        assert image_res["assetId"] == int(original_element["asset_id"])
        assert image_res["path"] in names
        # ZIP 里场景仍以 asset 引用（导出时不内联，减少重复）
        scene_json = json.loads(archive.read("scenes/scene_001.json").decode("utf-8"))
        zip_image = [element for element in scene_json["content"]["canvas"]["elements"]
                     if element.get("type") == "image"][0]
        assert zip_image["src"].startswith("/api/classroom/assets/")

    imported = client.post("/api/classroom/import", files={
        "file": ("课程.zip", exported.content, "application/zip"),
    })
    assert imported.status_code == 200, imported.text
    new_id = imported.json()["course"]["id"]
    imported_detail = course_detail(client, new_id)
    imported_image = image_elements(scene_of_type(imported_detail, "slide"))[0]
    assert imported_image["asset_id"] != original_element["asset_id"]
    assert imported_image["src"] == "/api/classroom/assets/%d" % int(imported_image["asset_id"])
    served = client.get(imported_image["src"])
    assert served.status_code == 200
    # 离线 HTML 导出把配图内联成 data URI（可离线打开）
    html = client.get(f"/api/classroom/courses/{course['id']}/export", params={"format": "html"})
    assert html.status_code == 200
    assert "data:image/png;base64," in html.text


def test_image_cancel_keeps_progress_and_resume(client, monkeypatch):
    provider = save_provider(client, models=("test-model", "gpt-image-1"))
    course = create_course(client)
    uid = user_id_of(client)
    outline, _warnings = schema.normalize_outline(OUTLINE_RAW, scene_count=3)
    set_course_outline(course["id"], uid, outline)
    job_id = insert_course_job(uid, {
        "course_id": course["id"], "stage": "scenes", "scene_types": ["slide", "quiz"],
        "scene_count": 3, "depth": "standard",
        "images": True, "images_per_scene": 1,
        "image_provider_id": provider["id"], "image_model": "gpt-image-1",
    }, provider["id"], "test-model", token="classroom-plus-cancel-token")

    cancel_state = {}

    def on_image(number):
        if not cancel_state:
            cancel_state["triggered"] = True
            with db.connection() as con:
                con.execute(
                    "UPDATE ai_jobs SET cancelled=1,status='cancel_requested' WHERE id=?", (job_id,),
                )

    FakeModel().install(monkeypatch)
    FakeImage(on_call=on_image).install(monkeypatch)
    asyncio.run(ai_jobs.execute_dispatched_job(job_id, "classroom-plus-cancel-token", ai_jobs._run_job))

    snapshot = job_view(client, job_id)
    assert snapshot["status"] == "cancelled"
    scenes = cdb.course_scenes(course["id"], uid)
    ready = [scene for scene in scenes if scene["status"] == "ready"]
    assert 1 <= len(ready) < 3
    # 取消前已生成场景的配图保留
    assert image_assets(course["id"])

    # 续跑：只补未完成场景，全部 ready
    FakeModel().install(monkeypatch)
    FakeImage().install(monkeypatch)
    assert client.post(f"/api/ai/jobs/{job_id}/resume").status_code == 200
    done = wait_for_status(client, job_id, "completed")
    assert done["status"] == "completed"
    assert [scene["status"] for scene in cdb.course_scenes(course["id"], uid)] == ["ready"] * 3


# ---------------------------------------------------------------------------
# 4. prompt 分支单元测试
# ---------------------------------------------------------------------------

def test_prompt_branches_for_interactive_and_images():
    interactive_prompt = prompts.build_outline_prompt(topic="光合作用", mode="interactive", scene_count=6)
    standard_prompt = prompts.build_outline_prompt(topic="光合作用", mode="standard", scene_count=6)
    # 交互优先分支：1-2 个 interactive + widget 选择表 + 动手提示要求
    for marker in ("interactive-first", "widget 选择表", "至少 1 个、最多 2 个", "动手试一试"):
        assert marker in interactive_prompt, marker
    assert "interactive-first" not in standard_prompt
    assert "widget 选择表" not in standard_prompt
    # slide 提示块只在与交互模式一起时出现
    hint_prompt = prompts.build_slide_prompt(
        scene_title="原料", outline_points=["水是原料"], interactive_hint=True)
    plain_prompt = prompts.build_slide_prompt(scene_title="原料", outline_points=["水是原料"])
    assert "动手试一试" in hint_prompt and "hint_001" in hint_prompt
    assert "动手试一试" not in plain_prompt
    # 配图 prompt：教学示意 + 无文字/真人/实景限制
    image_prompt = prompts.build_image_prompt(
        scene_title="光照与光合速率", outline_points=["调节光照观察速率"],
        course_title="光合作用入门", subject="生物", grade="初二",
    )
    assert "教学配图" in image_prompt and "禁止" in image_prompt
    assert "文字" in image_prompt and "真实人物照片" in image_prompt
    # prompt 版本已升级，便于追溯 AI 产物
    assert prompts.PROMPT_VERSION == "classroom-2026-11-21-v2"
