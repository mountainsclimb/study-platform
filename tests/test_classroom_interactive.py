"""AI 课堂 P1：交互场景（HTML 生成/静态安全校验/沙箱 CSP/运行时事件）与 PBL 结构化生成验收。

不联网：``_call_model`` 全部替换为按 prompt 标记分派的假模型。
"""
import json
import time

from fastapi.testclient import TestClient
import pytest

from app import ai, db
from app.classroom import interactive, pbl, schema
from app.classroom import db as cdb


# ---------------------------------------------------------------------------
# 假模型与公共辅助
# ---------------------------------------------------------------------------

SAFE_HTML = (
    "<!DOCTYPE html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
    "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
    "<title>光照强度模拟</title><style>body{margin:0;padding:16px;font-family:system-ui;}"
    "#experiment{display:block}</style></head><body><div id=\"experiment\">"
    "<h1>光照强度模拟</h1><label>光照强度 <input id=\"light\" type=\"range\" min=\"0\" max=\"100\" value=\"50\"></label>"
    "<p>光合速率：<span id=\"rate\">0</span></p><canvas id=\"chart\" width=\"320\" height=\"160\"></canvas></div>"
    "<script>(function(){var input=document.getElementById('light');var rate=document.getElementById('rate');"
    "function update(){var value=Number(input.value);var speed=Math.round(value*0.6*10)/10;"
    "rate.textContent=String(speed);document.getElementById('chart').setAttribute('data-speed',String(speed));"
    "if(window.zhixuReport){window.zhixuReport('state',{summary:'光照强度 '+value+'，光合速率 '+speed,"
    "state:{light:value,rate:speed}});}}input.addEventListener('input',update);update();})();</script>"
    "<script>(function(){function zhixuReport(type,payload){try{window.parent.postMessage("
    "{source:'zhixu-interactive',type:type,payload:payload||{}},'*');}catch(error){}}"
    "window.zhixuReport=zhixuReport;window.addEventListener('error',function(event){"
    "zhixuReport('error',{message:String((event&&event.message)||'脚本错误')});});"
    "window.addEventListener('load',function(){zhixuReport('ready',{});});})();</script>"
    "</body></html>"
)

INTERACTIVE_RAW = {"widget": "simulation", "height": 480, "html": SAFE_HTML}

PBL_RAW = {
    "project": {
        "title": "校园节能行动方案",
        "background": "学校上月电费上涨，请你调查校园用电并提出一份可执行的节能方案。",
        "goals": ["理解电功率与电能的关系", "能用数据支撑一个节能决策", "体验从调研到方案的完整过程"],
        "roles": [{"name": "节能项目导师", "description": "陪你走完调研、方案与复盘，随时可以提问。"}],
        "milestones": [
            {
                "title": "调研校园用电",
                "description": "记录三个场景的用电情况并找出最值得改进的一处。",
                "tasks": ["列出校园里三类主要用电设备", "估算它们的日均耗电量并找出最大项"],
                "deliverable": "一张用电调查表（含三项数据与一个结论）",
                "checkpoint": "数据有来源、计算过程可复核，结论能指出最大耗电项",
            },
            {
                "title": "提出并验证节能方案",
                "description": "针对最大耗电项设计一个可执行方案，并说明预期节省。",
                "tasks": ["写出方案的具体做法", "估算实施后的日均节电量"],
                "deliverable": "一页节能方案（做法+预计节省）",
                "checkpoint": "方案可执行、估算有依据，并能说明取舍",
            },
        ],
        "rubric": [
            {"criterion": "数据真实性", "levels": [
                {"level": "优秀", "description": "数据有来源且计算无误"},
                {"level": "合格", "description": "数据基本合理"},
                {"level": "待改进", "description": "数据来源不明"},
            ]},
            {"criterion": "方案可行性", "levels": [{"level": "优秀", "description": "可落地且有取舍分析"}]},
        ],
        "mentor_hints": ["先想清楚一天里哪些设备一直在耗电？", "估算时可以用功率乘以使用时长。"],
    }
}

SLIDE_RAW = {"elements": [
    {"id": "text_001", "type": "text", "left": 60, "top": 50, "width": 880, "height": 76,
     "content": "<p style=\"font-size:32px\">本课回顾</p>", "defaultFontName": "", "defaultColor": "#333333"},
]}

OUTLINE_RAW = {
    "title": "光合作用互动课",
    "scenes": [
        {"title": "光照强度模拟", "type": "interactive", "outline_points": ["调节光照观察速率"], "widget": "simulation"},
        {"title": "校园节能项目", "type": "pbl", "outline_points": ["调研校园用电"]},
        {"title": "总结", "type": "slide", "outline_points": ["知识回顾"]},
    ],
}

TUTOR_RAW = {"reply": {"speaker": "teacher", "text": "你观察到光照越强，光合速率越高。", "actions": []}}

_MARKERS = (
    ("课程大纲生成器", "outline"),
    ("幻灯片内容生成器", "slide"),
    ("测验内容生成器", "quiz"),
    ("课堂旁白与动作生成器", "actions"),
    ("课堂 AI 教师对话", "tutor"),
    ("交互场景 HTML 生成器", "interactive"),
    ("项目式学习（PBL）设计器", "pbl"),
)


class FakeModel:
    """按 prompt 标记返回固定 JSON；交互/PBL 支持「首次非法、重试合法」的序列。"""

    def __init__(self, *, outline=None, slide=None, interactive_payloads=None, pbl_payloads=None):
        self.payloads = {
            "outline": json.dumps(outline if outline is not None else OUTLINE_RAW, ensure_ascii=False),
            "slide": json.dumps(slide if slide is not None else SLIDE_RAW, ensure_ascii=False),
            "quiz": json.dumps([], ensure_ascii=False),
            "actions": json.dumps([{"type": "text", "content": "我们来看这一页。"}], ensure_ascii=False),
            "tutor": json.dumps(TUTOR_RAW, ensure_ascii=False),
        }
        self.sequences = {
            "interactive": [json.dumps(item, ensure_ascii=False) for item in (interactive_payloads or [INTERACTIVE_RAW])],
            "pbl": [json.dumps(item, ensure_ascii=False) for item in (pbl_payloads or [PBL_RAW])],
        }
        self.calls = []
        self.prompts = []

    def classify(self, prompt: str) -> str:
        for marker, kind in _MARKERS:
            if marker in prompt:
                return kind
        raise AssertionError("未知 prompt：%s" % prompt[:200])

    def install(self, monkeypatch):
        async def fake_call(provider, model, prompt, images=None, json_output=False, token_limit=5000, **kwargs):
            kind = self.classify(prompt)
            self.calls.append(kind)
            self.prompts.append(prompt)
            sequence = self.sequences.get(kind)
            if sequence is not None and len(sequence) > 1:
                content = sequence.pop(0)
            elif sequence:
                content = sequence[0]
            else:
                content = self.payloads[kind]
            return {"content": content, "model": model, "provider_name": "mock",
                    "usage": {"total_tokens": 12}}

        monkeypatch.setattr(ai, "_call_model", fake_call)
        return self

    def count(self, kind: str) -> int:
        return self.calls.count(kind)


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
        if job["status"] in ("failed", "cancelled") and job["status"] != status:
            pytest.fail("任务提前进入 %s（期望 %s）：%s" % (job["status"], status, job.get("error")))
        time.sleep(delay)
    pytest.fail("任务未进入 %s，当前 %s" % (status, job))


def insert_scene(course_id, user_id, scene_type, order_index, title, content,
                 *, meta=None, status="ready"):
    now = db.utcnow()
    with db.connection() as con:
        return con.execute(
            "INSERT INTO cls_scenes(course_id,user_id,scene_type,order_index,title,content_json,meta_json,status,"
            "created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (course_id, user_id, scene_type, order_index, title, cdb.json_dump(content),
             cdb.json_dump(meta or {}), status, now, now),
        ).lastrowid


def generate_all(client, provider, course, *, scene_types, scene_count=3):
    response = client.post(f"/api/classroom/courses/{course['id']}/generate", json={
        "stage": "all", "provider_id": provider["id"], "model": "test-model",
        "scene_types": scene_types, "scene_count": scene_count,
    })
    assert response.status_code == 200, response.text
    return response.json()["job"]


def confirm_outline(client, job_id, outline=None):
    response = client.post(f"/api/ai/jobs/{job_id}/confirm-outline", json={
        "outline": json.dumps(outline if outline is not None else OUTLINE_RAW, ensure_ascii=False),
    })
    assert response.status_code == 200, response.text


# ---------------------------------------------------------------------------
# 1. 交互 HTML 静态安全校验
# ---------------------------------------------------------------------------

def _mutate(html: str, snippet: str) -> str:
    return html.replace("</body>", snippet + "</body>")


def test_static_check_accepts_safe_self_contained_html():
    problems = interactive.static_check_interactive_html(SAFE_HTML)
    assert problems == [], problems
    content, warnings = interactive.normalize_generated_interactive(
        {"widget": "simulation", "height": 520, "html": SAFE_HTML}, title="光照模拟", widget="simulation")
    assert content["type"] == "interactive"
    assert content["widget"] == "simulation"
    assert content["height"] == 520
    assert "zhixuReport" in content["html"]
    assert warnings == []


@pytest.mark.parametrize("snippet,reason", [
    ("<script src=\"https://cdn.example.com/lib.js\"></script>", "外网"),
    ("<img src=\"//cdn.example.com/a.png\">", "协议相对"),
    ("<script src=\"app.js\"></script>", "script src"),
    ("<style>@import url(https://fonts.example.com/x.css);</style>", "外网"),
    ("<script>fetch('/api/data');</script>", "fetch"),
    ("<script>var x=new XMLHttpRequest();</script>", "XMLHttpRequest"),
    ("<script>eval('1+1');</script>", "eval"),
    ("<script>var f=new Function('return 1');</script>", "new Function"),
    ("<script>var secret=parent.document.cookie;</script>", "父窗口"),
    ("<script>localStorage.setItem('token','x');</script>", "凭据"),
    ("<script>document.cookie='a=b';</script>", "Cookie"),
    ("<iframe src=\"about:blank\"></iframe>", "iframe"),
    ("<meta http-equiv=\"refresh\" content=\"0;url=/x\">", "meta refresh"),
])
def test_static_check_rejects_external_resources_and_dangerous_apis(snippet, reason):
    problems = interactive.static_check_interactive_html(_mutate(SAFE_HTML, snippet))
    assert problems, "应拒绝：%s" % snippet
    assert any(reason in item for item in problems), (reason, problems)


def test_static_check_requires_reporter_and_size_limit():
    no_reporter = SAFE_HTML.split("<script>(function(){function zhixuReport")[0] + "</body></html>"
    problems = interactive.static_check_interactive_html(no_reporter)
    assert any("上报" in item or "zhixuReport" in item for item in problems)

    huge = SAFE_HTML.replace("</body>", "<div>" + "x" * (interactive.MAX_INTERACTIVE_HTML_BYTES + 10) + "</div></body>")
    problems = interactive.static_check_interactive_html(huge)
    assert any("超过" in item for item in problems)

    with pytest.raises(schema.ClassroomSchemaError):
        interactive.normalize_generated_interactive(
            {"html": _mutate(SAFE_HTML, "<script>fetch('/x');</script>")}, title="坏页面", widget="simulation")


def test_interactive_fragment_is_wrapped_into_complete_document():
    fragment = ("<div id=\"experiment\"><button id=\"b\">点我</button></div>"
                "<script>document.getElementById('b').addEventListener('click',function(){"
                "window.zhixuReport('state',{summary:'点击一次',state:{clicks:1}});});</script>"
                "<script>(function(){window.zhixuReport=window.zhixuReport||function(t,p){"
                "window.parent.postMessage({source:'zhixu-interactive',type:t,payload:p},'*');};"
                "window.addEventListener('error',function(e){window.zhixuReport('error',{});});})();</script>")
    content, _ = interactive.normalize_generated_interactive(
        {"widget": "game", "html": fragment}, title="点击游戏", widget="game")
    assert content["html"].lower().startswith("<!doctype html>")
    assert content["widget"] == "game"


# ---------------------------------------------------------------------------
# 2. 交互场景真实生成 + 沙箱路由
# ---------------------------------------------------------------------------

def test_interactive_scene_generation_and_sandbox_route(client, second_client, monkeypatch):
    provider = save_provider(client)
    course = create_course(client, mode="interactive")
    fake = FakeModel().install(monkeypatch)
    job = generate_all(client, provider, course, scene_types=["slide", "interactive", "pbl"])
    wait_for_status(client, job["id"], "awaiting_outline")
    confirm_outline(client, job["id"])
    done = wait_for_status(client, job["id"], "completed")
    assert done["result"]["generated_scene_count"] == 3
    assert fake.count("interactive") == 1
    assert fake.count("pbl") == 1
    # 交互 prompt 带上了场景标题、交互类型与安全约束
    interactive_prompt = next(prompt for prompt in fake.prompts if "交互场景 HTML 生成器" in prompt)
    assert "光照强度模拟" in interactive_prompt and "simulation" in interactive_prompt
    assert "zhixuReport" in interactive_prompt and "600KB" in interactive_prompt

    detail = client.get(f"/api/classroom/courses/{course['id']}").json()
    scene = next(item for item in detail["scenes"] if item["type"] == "interactive")
    assert scene["status"] == "ready"
    assert scene["meta"]["placeholder"] is False
    assert scene["meta"]["requires_generation"] is False
    assert scene["meta"]["interactive_checked"] is True
    assert scene["meta"]["html_bytes"] > 0
    assert "zhixuReport" in scene["content"]["html"]
    assert scene["content"]["widget"] == "simulation"

    response = client.get(f"/api/classroom/scenes/{scene['id']}/html")
    assert response.status_code == 200, response.text
    assert "光照强度模拟" in response.text or "光" in response.text
    assert response.headers["content-type"].startswith("text/html")
    csp = response.headers["content-security-policy"]
    assert "default-src 'none'" in csp
    assert "script-src 'unsafe-inline'" in csp
    assert "style-src 'unsafe-inline'" in csp
    assert "img-src data: blob:" in csp and "font-src data:" in csp
    assert "connect-src 'none'" in csp
    assert "unsafe-eval" not in csp
    assert response.headers["x-frame-options"] == "SAMEORIGIN"
    assert interactive.IFRAME_SANDBOX == "allow-scripts"
    assert "allow-same-origin" not in interactive.IFRAME_SANDBOX

    slide_scene = next(item for item in detail["scenes"] if item["type"] == "slide")
    assert client.get(f"/api/classroom/scenes/{slide_scene['id']}/html").status_code == 404
    assert second_client.get(f"/api/classroom/scenes/{scene['id']}/html").status_code == 404
    from app.main import app

    anon = TestClient(app)
    assert anon.get(f"/api/classroom/scenes/{scene['id']}/html").status_code == 401


def test_interactive_generation_retries_once_then_succeeds(client, monkeypatch):
    provider = save_provider(client)
    course = create_course(client, mode="interactive")
    bad = {"widget": "simulation", "html": _mutate(SAFE_HTML, "<script>fetch('/x');</script>")}
    fake = FakeModel(interactive_payloads=[bad, INTERACTIVE_RAW]).install(monkeypatch)
    job = generate_all(client, provider, course, scene_types=["slide", "interactive", "pbl"])
    wait_for_status(client, job["id"], "awaiting_outline")
    confirm_outline(client, job["id"])
    done = wait_for_status(client, job["id"], "completed")
    assert done["result"]["failed_scene_ids"] == []
    assert fake.count("interactive") == 2  # 首次校验失败 → 带纠错提示重试一次
    detail = client.get(f"/api/classroom/courses/{course['id']}").json()
    scene = next(item for item in detail["scenes"] if item["type"] == "interactive")
    assert scene["status"] == "ready"
    assert any("格式修正" in prompt or "未通过校验" in prompt for prompt in fake.prompts)


def test_interactive_generation_failure_is_recorded_not_faked(client, monkeypatch):
    provider = save_provider(client)
    course = create_course(client, mode="interactive")
    bad = {"widget": "simulation", "html": _mutate(SAFE_HTML, "<script>eval('1+1');</script>")}
    fake = FakeModel(interactive_payloads=[bad, bad]).install(monkeypatch)
    job = generate_all(client, provider, course, scene_types=["slide", "interactive", "pbl"])
    wait_for_status(client, job["id"], "awaiting_outline")
    confirm_outline(client, job["id"])
    done = wait_for_status(client, job["id"], "completed")
    assert len(done["result"]["failed_scene_ids"]) == 1
    assert fake.count("interactive") == 2
    detail = client.get(f"/api/classroom/courses/{course['id']}").json()
    scene = next(item for item in detail["scenes"] if item["type"] == "interactive")
    assert scene["status"] == "failed"
    assert "安全校验" in scene["meta"].get("error", "")
    assert "eval" in scene["meta"].get("error", "")
    # 失败场景没有伪造内容，也没有可播放 HTML
    assert scene["content"] == {}
    assert client.get(f"/api/classroom/scenes/{scene['id']}/html").status_code == 404


# ---------------------------------------------------------------------------
# 3. PBL 结构化生成与校验
# ---------------------------------------------------------------------------

def test_pbl_normalizer_enforces_structure():
    content, warnings = pbl.normalize_pbl_content(PBL_RAW)
    project = content["project"]
    assert content["type"] == "pbl"
    assert project["title"] == "校园节能行动方案"
    assert len(project["goals"]) == 3
    assert project["milestones"][0]["deliverable"]
    assert project["milestones"][0]["checkpoint"]
    assert project["rubric"][0]["criterion"] == "数据真实性"
    assert project["mentor_hints"]
    _ = warnings

    with pytest.raises(schema.ClassroomSchemaError):
        pbl.normalize_pbl_content({"project": {"title": "缺字段", "background": "背景太短"}})
    missing_checkpoint = json.loads(json.dumps(PBL_RAW, ensure_ascii=False))
    missing_checkpoint["project"]["milestones"][0].pop("checkpoint")
    with pytest.raises(schema.ClassroomSchemaError) as excinfo:
        pbl.normalize_pbl_content(missing_checkpoint)
    assert "checkpoint" in str(excinfo.value)


def test_pbl_scene_generation_is_structured(client, monkeypatch):
    provider = save_provider(client)
    course = create_course(client, mode="interactive")
    fake = FakeModel().install(monkeypatch)
    job = generate_all(client, provider, course, scene_types=["slide", "interactive", "pbl"])
    wait_for_status(client, job["id"], "awaiting_outline")
    confirm_outline(client, job["id"])
    wait_for_status(client, job["id"], "completed")
    assert fake.count("pbl") == 1
    pbl_prompt = next(prompt for prompt in fake.prompts if "项目式学习（PBL）设计器" in prompt)
    assert "deliverable" in pbl_prompt and "checkpoint" in pbl_prompt and "mentor_hints" in pbl_prompt

    detail = client.get(f"/api/classroom/courses/{course['id']}").json()
    scene = next(item for item in detail["scenes"] if item["type"] == "pbl")
    assert scene["status"] == "ready"
    assert scene["meta"]["placeholder"] is False
    assert scene["meta"]["pbl"]["milestone_count"] == 2
    project = scene["content"]["project"]
    assert [milestone["title"] for milestone in project["milestones"]] == ["调研校园用电", "提出并验证节能方案"]
    assert project["milestones"][0]["checkpoint"].startswith("数据有来源")
    assert project["goals"] and project["rubric"] and project["mentor_hints"]
    # 旁白（非 slide 场景用确定性 fallback）仍然存在
    assert any(action["type"] == "speech" for action in scene["actions"])


def test_pbl_generation_failure_records_reason(client, monkeypatch):
    provider = save_provider(client)
    course = create_course(client, mode="interactive")
    bad = {"project": {"title": "只有标题", "background": "背景不足"}}
    fake = FakeModel(pbl_payloads=[bad, bad]).install(monkeypatch)
    job = generate_all(client, provider, course, scene_types=["slide", "interactive", "pbl"])
    wait_for_status(client, job["id"], "awaiting_outline")
    confirm_outline(client, job["id"])
    done = wait_for_status(client, job["id"], "completed")
    assert len(done["result"]["failed_scene_ids"]) == 1
    assert fake.count("pbl") == 2
    detail = client.get(f"/api/classroom/courses/{course['id']}").json()
    scene = next(item for item in detail["scenes"] if item["type"] == "pbl")
    assert scene["status"] == "failed"
    assert "PBL" in scene["meta"].get("error", "")


# ---------------------------------------------------------------------------
# 4. 运行时事件上报（白名单 / progress / 追问）
# ---------------------------------------------------------------------------

def _setup_interactive_session(client):
    course = create_course(client, mode="interactive")
    uid = user_id_of(client)
    scene_id = insert_scene(course["id"], uid, "interactive", 1, "光照强度模拟", {
        "type": "interactive", "widget": "simulation", "height": 480, "html": SAFE_HTML,
    }, meta={"actions": [{"id": "act_001", "type": "speech", "text": "拖动滑杆观察光合速率变化。"}]})
    session = client.post(f"/api/classroom/courses/{course['id']}/sessions").json()["session"]
    return course, scene_id, session


def test_interactive_event_state_is_stored_in_session_progress(client):
    _course, scene_id, session = _setup_interactive_session(client)
    response = client.post(f"/api/classroom/scenes/{scene_id}/interactive-event", json={
        "type": "state", "session_id": session["id"],
        "summary": "光照强度 80，光合速率 48",
        "state": {"light": 80, "rate": 48},
    })
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True and body["stored"] is True and body["type"] == "state"

    progress = client.get(f"/api/classroom/sessions/{session['id']}").json()["progress"]
    assert progress["interactive"]["scene_id"] == scene_id
    assert progress["interactive"]["summary"] == "光照强度 80，光合速率 48"
    assert progress["interactive"]["state"] == {"light": 80, "rate": 48}
    assert progress["interactive_events"][-1]["type"] == "state"


def test_interactive_status_events_are_whitelisted_and_logged(client, second_client):
    _course, scene_id, session = _setup_interactive_session(client)
    for event_type in ("ready", "error"):
        response = client.post(f"/api/classroom/scenes/{scene_id}/interactive-event", json={
            "type": event_type, "session_id": session["id"],
            "message": "初始化失败：canvas 不可用" if event_type == "error" else "",
        })
        assert response.status_code == 200, response.text
        assert response.json()["stored"] is True
    progress = client.get(f"/api/classroom/sessions/{session['id']}").json()["progress"]
    types = [item["type"] for item in progress["interactive_events"]]
    assert types[-2:] == ["ready", "error"]

    # 事件类型白名单
    bad = client.post(f"/api/classroom/scenes/{scene_id}/interactive-event", json={"type": "click"})
    assert bad.status_code == 422
    assert "error / ready / state" in bad.json()["detail"]
    # 状态过大
    huge = client.post(f"/api/classroom/scenes/{scene_id}/interactive-event", json={
        "type": "state", "session_id": session["id"], "state": {"blob": "x" * 9000},
    })
    assert huge.status_code == 422
    assert "过大" in huge.json()["detail"]
    # state 空 payload
    empty = client.post(f"/api/classroom/scenes/{scene_id}/interactive-event", json={
        "type": "state", "session_id": session["id"],
    })
    assert empty.status_code == 422
    # 他人会话/场景 → 404
    assert second_client.post(f"/api/classroom/scenes/{scene_id}/interactive-event", json={
        "type": "ready", "session_id": session["id"],
    }).status_code == 404
    # 不传 session_id 时事件可接受但不落库
    no_session = client.post(f"/api/classroom/scenes/{scene_id}/interactive-event", json={"type": "ready"})
    assert no_session.status_code == 200
    assert no_session.json()["stored"] is False


def test_interactive_event_rejects_non_interactive_scene(client):
    course = create_course(client)
    uid = user_id_of(client)
    scene_id = insert_scene(course["id"], uid, "slide", 1, "讲解", {"type": "slide", "canvas": {"elements": []}})
    response = client.post(f"/api/classroom/scenes/{scene_id}/interactive-event", json={"type": "ready"})
    assert response.status_code == 422
    assert "不是交互场景" in response.json()["detail"]


def test_tutor_uses_reported_interactive_state(client, monkeypatch):
    _course, scene_id, session = _setup_interactive_session(client)
    provider = save_provider(client)
    event = client.post(f"/api/classroom/scenes/{scene_id}/interactive-event", json={
        "type": "state", "session_id": session["id"],
        "summary": "学生把光照强度调到 80，光合速率 48",
        "state": {"light": 80, "rate": 48},
    })
    assert event.status_code == 200, event.text
    progress = client.get(f"/api/classroom/sessions/{session['id']}").json()["progress"]
    assert "光照强度" in (progress.get("interactive") or {}).get("summary", ""), progress
    captured = {}

    async def fake_call(provider_row, model, prompt, images=None, json_output=False, token_limit=5000, **kwargs):
        if "课堂 AI 教师对话" in prompt:
            captured["prompt"] = prompt
            return {"content": json.dumps(TUTOR_RAW, ensure_ascii=False), "model": model,
                    "provider_name": "mock", "usage": {"total_tokens": 8}}
        raise AssertionError("未预期的 prompt：%s" % prompt[:120])

    monkeypatch.setattr(ai, "_call_model", fake_call)
    response = client.post(f"/api/classroom/sessions/{session['id']}/tutor", json={
        "scene_id": scene_id, "message": "我刚才调高光照后速率为什么升得变慢了？",
        "mode": "qa", "provider_id": provider["id"], "model": "test-model",
    })
    assert response.status_code == 200, response.text
    assert "光照强度调到 80" in captured["prompt"]
    assert "光合速率 48" in captured["prompt"]
    assert "学生当前在交互页面的状态" in captured["prompt"]
    assert response.json()["reply"]["text"]


def test_interactive_event_requires_login():
    from app.main import app

    anon = TestClient(app)
    response = anon.post("/api/classroom/scenes/1/interactive-event", json={"type": "ready"})
    assert response.status_code == 401
