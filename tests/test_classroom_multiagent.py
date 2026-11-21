"""AI 课堂 P1 多智能体课堂验收：角色名册 / 多角色讨论 / 学生插话 / 增强白板动作 / 归属与上限。

不联网：所有模型调用都 monkeypatch ``app.ai._call_model``（``call_structured`` 延迟引用它），
覆盖严格 JSON 校验、失败重试、动作白名单、0-1 白板坐标、cls_turns 持久化与用户归属。
"""
import json

from fastapi.testclient import TestClient
import pytest

from app import db
from app.classroom import db as cdb
from app.classroom import multiagent as ma
from app.classroom.schema import ClassroomSchemaError


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------

ROSTER_PAYLOAD = {
    "teacher": {"name": "林老师", "persona": "善于用追问推进推导，讲题时先让学生猜再纠正。"},
    "classmates": [
        {"name": "小舟", "persona": "好奇心强，常问“为什么”，但计算容易粗心。"},
        {"name": "小雨", "persona": "喜欢先记结论，需要老师带着看推导过程。"},
        {"name": "阿哲", "persona": "愿意主动举例，偶尔把概念张冠李戴。"},
    ],
}

SLIDE_CONTENT = {
    "type": "slide", "schemaVersion": 1,
    "canvas": {"width": 1000, "height": 562.5, "theme": {}, "elements": [
        {"id": "text_001", "type": "text", "left": 60, "top": 50, "width": 880, "height": 76,
         "content": "<p>光合作用</p>"},
        {"id": "latex_001", "type": "latex", "left": 660, "top": 250, "width": 240, "height": 80,
         "latex": "6CO_2+6H_2O\\rightarrow C_6H_{12}O_6+6O_2"},
        {"id": "chart_001", "type": "chart", "left": 660, "top": 150, "width": 280, "height": 200,
         "chartType": "line", "data": {"labels": [], "legends": [], "series": []}},
    ]},
}


class FakeModel:
    """按顺序返回 payload；payload 可为 dict/list（自动 JSON 化）或字符串。"""

    def __init__(self, payloads=None, *, fail=False):
        self.payloads = list(payloads or [])
        self.calls = []
        self.fail = fail

    async def __call__(self, provider, model, prompt, images=None, json_output=False,
                       token_limit=5000, **kwargs):
        self.calls.append({
            "prompt": prompt, "model": model, "json_output": json_output,
            "token_limit": token_limit,
        })
        if self.fail:
            raise ValueError("模型服务不可用")
        content = self.payloads.pop(0) if self.payloads else "{}"
        if isinstance(content, (dict, list)):
            content = json.dumps(content, ensure_ascii=False)
        return {
            "content": content, "model": model, "provider_name": "mock",
            "usage": {"prompt_tokens": 12, "completion_tokens": 34, "total_tokens": 46},
        }


def install_model(monkeypatch, *payloads, fail=False) -> FakeModel:
    fake = FakeModel(payloads, fail=fail)
    monkeypatch.setattr("app.ai._call_model", fake)
    return fake


def save_provider(client, name="多智能体测试连接", models=("test-model",)):
    response = client.post("/api/providers", json={
        "name": name, "kind": "openai", "base_url": "https://api.openai.com/v1",
        "api_key": "sk-multiagent-unit-test", "models": list(models),
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


def make_classroom(client, monkeypatch=None, *, scenes=1, roster=True, provider=True):
    """建课 + 场景 + 会话；默认配好模型连接与角色名册，返回 (course, scenes, session, uid)。"""
    if provider:
        save_provider(client)
    course = create_course(client)
    uid = user_id_of(client)
    scene_ids = []
    for index in range(scenes):
        content = dict(SLIDE_CONTENT)
        content["canvas"] = dict(SLIDE_CONTENT["canvas"])
        scene_ids.append(insert_scene(
            course["id"], uid, "slide", index + 1,
            "原料与产物" if index == 0 else "影响光合速率的因素",
            content, meta={"digest": "场景《原料与产物》（幻灯片）元素 text_001、latex_001"},
        ))
    session = client.post(f"/api/classroom/courses/{course['id']}/sessions",
                          json={"scene_id": scene_ids[0]}).json()["session"]
    if roster:
        fake = install_model(monkeypatch, ROSTER_PAYLOAD)
        response = client.post(f"/api/classroom/courses/{course['id']}/agents/generate", json={})
        assert response.status_code == 200, response.text
        assert fake.calls
    return course, scene_ids, session, uid


def discussion_payload(*turns, scene_hint=None):
    payload = {"turns": list(turns)}
    if scene_hint is not None:
        payload["scene_hint"] = scene_hint
    return payload


def turn(speaker_id="teacher_1", text="我们来看这一页。", actions=None):
    return {"speaker_id": speaker_id, "text": text, "actions": actions or []}


def course_config(client, course_id):
    detail = client.get(f"/api/classroom/courses/{course_id}")
    assert detail.status_code == 200
    return detail.json()["course"]["config"]


# ---------------------------------------------------------------------------
# 1. 鉴权与归属
# ---------------------------------------------------------------------------

def test_agents_and_multiagent_endpoints_require_login(client):
    from app.main import app

    course = create_course(client)
    anon = TestClient(app)
    endpoints = [
        ("get", f"/api/classroom/courses/{course['id']}/agents"),
        ("post", f"/api/classroom/courses/{course['id']}/agents/generate"),
        ("patch", f"/api/classroom/courses/{course['id']}/agents"),
        ("post", "/api/classroom/sessions/1/discussion"),
        ("post", "/api/classroom/sessions/1/interject"),
    ]
    for method, path in endpoints:
        kwargs = {"json": {}} if method in ("post", "patch") else {}
        response = getattr(anon, method)(path, **kwargs)
        assert response.status_code == 401, (method, path, response.text)


def test_foreign_course_agents_and_session_are_404(client, second_client):
    save_provider(client)
    course = create_course(client)
    uid = user_id_of(client)
    scene_id = insert_scene(course["id"], uid, "slide", 1, "第一页", SLIDE_CONTENT)
    session = client.post(f"/api/classroom/courses/{course['id']}/sessions").json()["session"]

    assert second_client.get(f"/api/classroom/courses/{course['id']}/agents").status_code == 404
    assert second_client.post(f"/api/classroom/courses/{course['id']}/agents/generate",
                              json={}).status_code == 404
    assert second_client.patch(f"/api/classroom/courses/{course['id']}/agents",
                               json={"clear": True}).status_code == 404
    assert second_client.post(f"/api/classroom/sessions/{session['id']}/discussion", json={
        "scene_id": scene_id, "mode": "discussion",
    }).status_code == 404
    assert second_client.post(f"/api/classroom/sessions/{session['id']}/interject", json={
        "message": "你好",
    }).status_code == 404


# ---------------------------------------------------------------------------
# 2. 角色名册：生成 / 读取 / 编辑
# ---------------------------------------------------------------------------

def test_generate_agents_no_provider_is_422(client):
    course = create_course(client)
    response = client.post(f"/api/classroom/courses/{course['id']}/agents/generate", json={})
    assert response.status_code == 422
    assert "模型设置" in response.json()["detail"]


def test_generate_agents_returns_roster_and_persists(client, monkeypatch):
    save_provider(client)
    course = create_course(client)
    fake = install_model(monkeypatch, ROSTER_PAYLOAD)

    response = client.post(f"/api/classroom/courses/{course['id']}/agents/generate",
                           json={"classmate_count": 3})
    assert response.status_code == 200, response.text
    body = response.json()
    roster = body["roster"]
    assert roster["teacher"]["name"] == "林老师"
    assert roster["teacher"]["role"] == "teacher"
    assert roster["teacher"]["id"] == "teacher_1"
    assert roster["teacher"]["priority"] == 10
    assert [item["id"] for item in roster["classmates"]] == ["student_1", "student_2", "student_3"]
    assert roster["classmates"][0]["color"] == "#10b981"
    assert body["has_roster"] is True
    assert body["usage"]["total_tokens"] == 46
    assert body["provider_name"] == "mock"
    assert body["prompt_version"] == ma.PROMPT_VERSION
    assert fake.calls and "课堂角色名册生成" in fake.calls[0]["prompt"]
    assert fake.calls[0]["json_output"] is True

    # 落库到 config_json.agents / agents_meta，且不破坏既有 config 字段
    config = course_config(client, course["id"])
    assert config["agents"]["teacher"]["name"] == "林老师"
    assert config["agents_meta"]["prompt_version"] == ma.PROMPT_VERSION
    assert "document_ids" in config

    # GET 读回
    got = client.get(f"/api/classroom/courses/{course['id']}/agents").json()
    assert got["has_roster"] is True
    assert [item["id"] for item in got["agents"]] == ["teacher_1", "student_1", "student_2", "student_3"]
    assert got["generated_at"]
    assert got["limits"]["agents_max"] == 6


def test_generate_agents_reads_empty_roster_first(client):
    course = create_course(client)
    body = client.get(f"/api/classroom/courses/{course['id']}/agents").json()
    assert body["has_roster"] is False
    assert body["roster"] == {"teacher": None, "classmates": []}
    assert body["agents"] == []


def test_generate_agents_invalid_json_retries_once_then_succeeds(client, monkeypatch):
    save_provider(client)
    course = create_course(client)
    fake = install_model(monkeypatch, "这不是 JSON", ROSTER_PAYLOAD)

    response = client.post(f"/api/classroom/courses/{course['id']}/agents/generate", json={})
    assert response.status_code == 200, response.text
    assert len(fake.calls) == 2
    assert "格式修正" in fake.calls[1]["prompt"]
    assert response.json()["has_roster"] is True


def test_generate_agents_invalid_roster_twice_is_503_and_keeps_config(client, monkeypatch):
    save_provider(client)
    course = create_course(client)
    two_teachers = {
        "teacher": {"name": "老师甲", "persona": "第一老师，讲得很细。"},
        "classmates": [{"name": "老师乙", "persona": "冒充同学的第二老师。", "role": "teacher"}],
    }
    fake = install_model(monkeypatch, two_teachers, two_teachers)

    response = client.post(f"/api/classroom/courses/{course['id']}/agents/generate", json={})
    assert response.status_code == 503
    assert len(fake.calls) == 2
    assert "agents" not in course_config(client, course["id"])


def test_patch_agents_full_replace_partial_and_clear(client, monkeypatch):
    course, _scenes, _session, _uid = make_classroom(client, monkeypatch)

    # 完整替换
    response = client.patch(f"/api/classroom/courses/{course['id']}/agents", json={
        "roster": {
            "teacher": {"id": "teacher_1", "name": "王老师", "persona": "重视动手验证，先做再总结。"},
            "classmates": [
                {"id": "student_1", "name": "小明", "persona": "喜欢动手，但结论写得慢。"},
                {"id": "student_2", "name": "小雅", "persona": "逻辑清晰，喜欢追问为什么。"},
            ],
        },
    })
    assert response.status_code == 200, response.text
    roster = response.json()["roster"]
    assert roster["teacher"]["name"] == "王老师"
    assert [item["name"] for item in roster["classmates"]] == ["小明", "小雅"]

    # 局部编辑老师名字：persona 保留
    response = client.patch(f"/api/classroom/courses/{course['id']}/agents", json={
        "teacher": {"id": "teacher_1", "name": "李老师"},
    })
    assert response.status_code == 200, response.text
    teacher = response.json()["roster"]["teacher"]
    assert teacher["name"] == "李老师"
    assert teacher["persona"] == "重视动手验证，先做再总结。"

    # 局部编辑同学 personaa
    response = client.patch(f"/api/classroom/courses/{course['id']}/agents", json={
        "classmates": [{"id": "student_2", "persona": "逻辑清晰，会把推导步骤写在草稿上。"}],
    })
    assert response.status_code == 200
    assert response.json()["roster"]["classmates"][1]["persona"].startswith("逻辑清晰")

    # 未知角色 id / 空编辑 → 422
    assert client.patch(f"/api/classroom/courses/{course['id']}/agents", json={
        "teacher": {"id": "teacher_404", "name": "赵老师"},
    }).status_code == 422
    assert client.patch(f"/api/classroom/courses/{course['id']}/agents", json={}).status_code == 422

    # 完整替换里出现 2 名老师 → 422
    assert client.patch(f"/api/classroom/courses/{course['id']}/agents", json={
        "roster": {
            "teacher": {"id": "teacher_1", "name": "王老师", "persona": "老师一。"},
            "classmates": [{"id": "student_1", "name": "老师二", "role": "teacher", "persona": "老师二。"}],
        },
    }).status_code == 422

    # 清空
    response = client.patch(f"/api/classroom/courses/{course['id']}/agents", json={"clear": True})
    assert response.status_code == 200
    assert response.json()["has_roster"] is False
    assert "agents" not in course_config(client, course["id"])


def test_patch_agents_validation_and_limits(client, monkeypatch):
    course, _scenes, _session, _uid = make_classroom(client, monkeypatch)
    # 名册超过 5 名同学 → 截断并告警
    classmates = [{"name": "同学%d" % index, "persona": "第 %d 位同学的人设说明。" % index}
                  for index in range(1, 8)]
    response = client.patch(f"/api/classroom/courses/{course['id']}/agents", json={
        "roster": {"teacher": {"id": "teacher_1", "name": "林老师", "persona": "引导型老师。"},
                   "classmates": classmates},
    })
    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body["roster"]["classmates"]) == ma.MAX_CLASSMATES
    assert any("上限" in item for item in body["warnings"])

    # 名字超长 / persona 过短 → Pydantic 422
    assert client.patch(f"/api/classroom/courses/{course['id']}/agents", json={
        "teacher": {"id": "teacher_1", "name": "名" * 30},
    }).status_code == 422
    assert client.patch(f"/api/classroom/courses/{course['id']}/agents", json={
        "teacher": {"id": "teacher_1", "persona": "短"},
    }).status_code == 422


# ---------------------------------------------------------------------------
# 3. 多角色讨论
# ---------------------------------------------------------------------------

def test_discussion_requires_provider_and_roster(client, monkeypatch):
    course = create_course(client)
    uid = user_id_of(client)
    scene_id = insert_scene(course["id"], uid, "slide", 1, "第一页", SLIDE_CONTENT)
    session = client.post(f"/api/classroom/courses/{course['id']}/sessions").json()["session"]

    # 无模型连接 → 422 模型设置
    response = client.post(f"/api/classroom/sessions/{session['id']}/discussion", json={
        "scene_id": scene_id, "mode": "discussion",
    })
    assert response.status_code == 422
    assert "模型设置" in response.json()["detail"]

    # 有连接但无名册 → 422 引导生成名册
    save_provider(client)
    install_model(monkeypatch, ROSTER_PAYLOAD)
    response = client.post(f"/api/classroom/sessions/{session['id']}/discussion", json={
        "scene_id": scene_id, "mode": "discussion",
    })
    assert response.status_code == 422
    assert "角色名册" in response.json()["detail"]

    # 生成名册后可用
    assert client.post(f"/api/classroom/courses/{course['id']}/agents/generate",
                       json={}).status_code == 200
    install_model(monkeypatch, discussion_payload(turn(), turn("student_1", "为什么需要水？")))
    response = client.post(f"/api/classroom/sessions/{session['id']}/discussion", json={
        "scene_id": scene_id, "mode": "qa",
    })
    assert response.status_code == 200, response.text


def test_discussion_returns_enriched_turns_actions_and_persists(client, monkeypatch):
    course, (first, _second), session, uid = make_classroom(client, monkeypatch, scenes=2)
    script = discussion_payload(
        turn("teacher_1", "这一页我们看原料与产物。", [
            {"type": "spotlight", "element_id": "text_001"},
            {"type": "点歌"},                                   # 未知动作 → 丢弃
            {"type": "spotlight", "element_id": "nope_999"},    # 不存在的元素 → 丢弃
        ]),
        turn("student_1", "老师，水为什么算原料？", [
            {"type": "whiteboard", "ops": [
                {"kind": "rect", "x": 0.06, "y": 0.18, "width": 0.22, "height": 0.15},
                {"kind": "text", "x": 0.08, "y": 0.23, "content": "光反应", "font_size": 20},
                {"kind": "arrow", "x1": 0.28, "y1": 0.2, "x2": 0.38, "y2": 0.2},
            ]},
        ]),
        turn("teacher_1", "水在光反应里被分解，提供氢和电子。", [
            {"type": "laser", "element_id": "latex_001"},
        ]),
    )
    fake = install_model(monkeypatch, script)

    response = client.post(f"/api/classroom/sessions/{session['id']}/discussion", json={
        "scene_id": first, "mode": "discussion", "topic": "原料与产物",
        "rounds": 2, "user_message": "老师，这页主要讲什么？",
    })
    assert response.status_code == 200, response.text
    body = response.json()
    turns = body["turns"]
    assert len(turns) == 3
    assert [item["speaker_name"] for item in turns] == ["林老师", "小舟", "林老师"]
    assert [item["role"] for item in turns] == ["teacher", "student", "teacher"]
    assert turns[0]["color"] == "#3b82f6" and turns[1]["color"] == "#10b981"
    # 动作白名单：spotlight 只留下合法元素
    assert [action["type"] for action in turns[0]["actions"]] == ["spotlight"]
    assert turns[0]["actions"][0]["element_id"] == "text_001"
    # 增强白板：coord_space unit + 3 个合法 op
    whiteboard = turns[1]["actions"][0]
    assert whiteboard["type"] == "whiteboard"
    assert whiteboard["coord_space"] == "unit"
    assert [op["kind"] for op in whiteboard["ops"]] == ["rect", "text", "arrow"]
    assert 0 <= whiteboard["ops"][0]["x"] <= 1
    assert body["mode"] == "discussion" and body["rounds"] == 2
    assert body["usage"]["total_tokens"] == 46
    assert body["user_turn_id"] is not None
    assert len(body["turn_ids"]) == 3
    assert any("点歌" in item for item in body["warnings"])
    assert body["prompt_version"] == ma.PROMPT_VERSION

    # prompt 注入：课程/场景/名册/元素 id/学生发言
    prompt = fake.calls[0]["prompt"]
    for piece in ("多智能体课堂脚本生成", "林老师", "小舟", "text_001", "这页主要讲什么", "原料与产物"):
        assert piece in prompt, piece
    assert fake.calls[0]["token_limit"] == 2500

    # cls_turns 持久化：user + teacher/student 角色、speaker 名、actions
    detail = client.get(f"/api/classroom/sessions/{session['id']}").json()
    stored = detail["turns"]
    assert [row["role"] for row in stored] == ["user", "teacher", "student", "teacher"]
    assert [row["speaker"] for row in stored] == ["student", "林老师", "小舟", "林老师"]
    assert stored[0]["content"] == "老师，这页主要讲什么？"
    assert stored[2]["actions"][0]["coord_space"] == "unit"
    assert [row["id"] for row in stored[1:]] == body["turn_ids"]


def test_discussion_truncates_turns_and_applies_scene_hint(client, monkeypatch):
    course, (first, second), session, _uid = make_classroom(client, monkeypatch, scenes=2)
    many = discussion_payload(
        *[turn("teacher_1" if index % 2 == 0 else "student_1", "第 %d 段发言。" % index)
          for index in range(9)],
        scene_hint={"index": 1, "reason": "下一页讲影响因素"},
    )
    install_model(monkeypatch, many)
    response = client.post(f"/api/classroom/sessions/{session['id']}/discussion", json={
        "scene_id": first, "mode": "lecture", "rounds": 3,
    })
    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body["turns"]) == ma.MAX_DISCUSSION_TURNS
    assert any("上限" in item for item in body["warnings"])
    assert body["scene_hint"]["index"] == 1
    # scene_hint 已由服务端切换会话当前场景
    assert client.get(f"/api/classroom/sessions/{session['id']}").json()["session"]["current_scene_id"] == second
    assert second != first


def test_discussion_mode_and_rounds_validation(client, monkeypatch):
    course, (first, _second), session, uid = make_classroom(client, monkeypatch, scenes=2)
    install_model(monkeypatch, discussion_payload(turn(), turn("student_1", "补充。")))

    response = client.post(f"/api/classroom/sessions/{session['id']}/discussion", json={
        "scene_id": first, "mode": "debate",
    })
    assert response.status_code == 422
    assert client.post(f"/api/classroom/sessions/{session['id']}/discussion", json={
        "scene_id": first, "rounds": 0,
    }).status_code == 422
    assert client.post(f"/api/classroom/sessions/{session['id']}/discussion", json={
        "scene_id": first, "rounds": 4,
    }).status_code == 422

    other_course = create_course(client, topic="细胞结构")
    other_scene = insert_scene(other_course["id"], uid, "slide", 1, "别的课", SLIDE_CONTENT)
    assert client.post(f"/api/classroom/sessions/{session['id']}/discussion", json={
        "scene_id": other_scene,
    }).status_code == 404
    assert client.post(f"/api/classroom/sessions/{session['id']}/discussion", json={
        "scene_id": 99999,
    }).status_code == 404


def test_discussion_json_retry_then_success_and_failure_no_persist(client, monkeypatch):
    course, (first,), session, _uid = make_classroom(client, monkeypatch, scenes=1)

    # 第一次非法 JSON，第二次合法 → 成功，且失败那次不落库
    fake = install_model(monkeypatch, "```json 不是合法 JSON ```",
                         discussion_payload(turn(), turn("student_1", "我有个问题。")))
    response = client.post(f"/api/classroom/sessions/{session['id']}/discussion", json={
        "scene_id": first, "mode": "qa",
    })
    assert response.status_code == 200, response.text
    assert len(fake.calls) == 2
    assert "格式修正" in fake.calls[1]["prompt"]
    assert len(response.json()["turns"]) == 2

    # 两次非法 → 503，且不写半成品 turns
    second_session = client.post(f"/api/classroom/courses/{course['id']}/sessions").json()["session"]
    fake = install_model(monkeypatch, "not json", "still not json")
    response = client.post(f"/api/classroom/sessions/{second_session['id']}/discussion", json={
        "scene_id": first, "mode": "discussion",
    })
    assert response.status_code == 503
    assert len(fake.calls) == 2
    assert client.get(f"/api/classroom/sessions/{second_session['id']}").json()["turns"] == []


def test_discussion_unknown_speaker_retries_then_503(client, monkeypatch):
    course, (first,), session, _uid = make_classroom(client, monkeypatch, scenes=1)
    unknown = discussion_payload(turn("teacher_404", "我是名册外的角色。"))
    fake = install_model(monkeypatch, unknown, unknown)
    response = client.post(f"/api/classroom/sessions/{session['id']}/discussion", json={
        "scene_id": first, "mode": "discussion",
    })
    assert response.status_code == 503
    assert len(fake.calls) == 2
    assert client.get(f"/api/classroom/sessions/{session['id']}").json()["turns"] == []


def test_discussion_model_failure_is_503_and_no_turns(client, monkeypatch):
    course, (first,), session, _uid = make_classroom(client, monkeypatch, scenes=1)
    install_model(monkeypatch, fail=True)
    response = client.post(f"/api/classroom/sessions/{session['id']}/discussion", json={
        "scene_id": first,
    })
    assert response.status_code == 503
    assert client.get(f"/api/classroom/sessions/{session['id']}").json()["turns"] == []


def test_discussion_whiteboard_ops_limits_in_response(client, monkeypatch):
    course, (first,), session, _uid = make_classroom(client, monkeypatch, scenes=1)
    ops = [{"kind": "text", "x": 0.1, "y": 0.1, "content": "关键点"} for _ in range(45)]
    ops[0] = {"kind": "text", "x": 2, "y": -1, "content": "字" * 300, "font_size": 200}
    ops[1] = {"kind": "plot", "x": 0.1, "y": 0.1}                       # 未知类型
    ops[2] = {"kind": "path", "d": "<script>alert(1)</script>", "x": 0.1, "y": 0.1}
    ops[3] = {"kind": "formula", "x": 0.1, "y": 0.2, "latex": "$E=mc^2$"}
    script = discussion_payload(turn("teacher_1", "我们用白板梳理一下。", [
        {"type": "whiteboard", "ops": ops},
    ]))
    install_model(monkeypatch, script)
    response = client.post(f"/api/classroom/sessions/{session['id']}/discussion", json={
        "scene_id": first, "mode": "discussion",
    })
    assert response.status_code == 200, response.text
    callback_ops = response.json()["turns"][0]["actions"][0]["ops"]
    # 45 个 op 只处理前 40 个，其中 2 个非法（plot / 恶意 path）被丢弃 → 38 个
    assert len(callback_ops) == ma.MAX_WHITEBOARD_OPS - 2
    assert any("上限" in item for item in response.json()["warnings"])
    kinds = {op["kind"] for op in callback_ops}
    assert kinds <= set(ma.WHITEBOARD_KINDS)
    text_op = callback_ops[0]
    assert text_op["x"] == 1.0 and text_op["y"] == 0.0          # 越界坐标收敛
    assert len(text_op["content"]) == ma.MAX_WB_TEXT_CHARS      # 文字截断
    assert text_op["font_size"] == 72                           # 字号收敛
    formulas = [op for op in callback_ops if op["kind"] == "formula"]
    assert formulas and formulas[0]["latex"] == "E=mc^2"        # 去掉 $ 包裹
    assert all("script" not in op.get("d", "") for op in callback_ops)


# ---------------------------------------------------------------------------
# 4. 学生插话
# ---------------------------------------------------------------------------

def test_interject_returns_at_most_two_turns_and_persists(client, monkeypatch):
    course, (first,), session, _uid = make_classroom(client, monkeypatch, scenes=1)
    payload = discussion_payload(
        turn("teacher_1", "你问到了关键：光照不足时光反应先受限。", [
            {"type": "spotlight", "element_id": "chart_001"},
        ]),
        turn("student_3", "那温度过高呢？", []),
        turn("teacher_1", "这一段应被截断。", []),
    )
    fake = install_model(monkeypatch, payload)
    response = client.post(f"/api/classroom/sessions/{session['id']}/interject", json={
        "scene_id": first, "message": "那如果光照不足会怎样？",
    })
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["mode"] == "interject"
    assert len(body["turns"]) == ma.MAX_INTERJECT_TURNS
    assert any("上限" in item for item in body["warnings"])
    assert body["user_turn_id"] is not None
    assert len(body["turn_ids"]) == 2
    assert "插话" in fake.calls[0]["prompt"]
    assert "那如果光照不足会怎样" in fake.calls[0]["prompt"]
    assert fake.calls[0]["token_limit"] == 1500

    stored = client.get(f"/api/classroom/sessions/{session['id']}").json()["turns"]
    assert [row["role"] for row in stored] == ["user", "teacher", "student"]
    assert stored[0]["content"] == "那如果光照不足会怎样？"
    assert stored[1]["actions"][0]["type"] == "spotlight"


def test_interject_validation_and_no_provider(client, monkeypatch):
    course = create_course(client)
    uid = user_id_of(client)
    scene_id = insert_scene(course["id"], uid, "slide", 1, "第一页", SLIDE_CONTENT)
    session = client.post(f"/api/classroom/courses/{course['id']}/sessions").json()["session"]

    # 空消息 → Pydantic 422
    assert client.post(f"/api/classroom/sessions/{session['id']}/interject", json={
        "message": "",
    }).status_code == 422
    # 未配置连接 → 422 模型设置
    response = client.post(f"/api/classroom/sessions/{session['id']}/interject", json={
        "scene_id": scene_id, "message": "老师我有个问题",
    })
    assert response.status_code == 422
    assert "模型设置" in response.json()["detail"]


# ---------------------------------------------------------------------------
# 5. 白板动作与名册校验（模块级单元测试）
# ---------------------------------------------------------------------------

def test_whiteboard_ops_all_kinds_and_clamping():
    ops, warnings = ma.normalize_whiteboard_ops([
        {"kind": "text", "x": 0.1, "y": 0.2, "content": "关键词", "font_size": 24},
        {"kind": "formula", "x": 0.1, "y": 0.3, "width": 0.4, "latex": "$a^2+b^2=c^2$"},
        {"kind": "line", "x1": 0.1, "y1": 0.5, "x2": 0.6, "y2": 0.5},
        {"kind": "arrow", "x1": 0.1, "y1": 0.6, "x2": 0.6, "y2": 0.6, "width": 99},
        {"kind": "rect", "x": 0.6, "y": 0.1, "width": 0.2, "height": 0.1, "fill": "#ff0000"},
        {"kind": "circle", "x": 0.7, "y": 0.3, "width": 0.2, "height": 0.2},
        {"kind": "path", "x": 0.1, "y": 0.7, "width": 0.2, "height": 0.2,
         "d": "M0 0 L1 1", "view_box": [1, 1]},
        {"kind": "text", "x": 9, "y": -3, "content": "越界"},
        {"kind": "text", "content": "缺坐标但有默认值"},
        {"kind": "text"},                                             # 缺内容 → 丢弃
        {"kind": "公式?"},                                             # 未知 → 丢弃
        {"kind": "line", "x1": 0.1, "y1": 0.1, "x2": 0.1, "y2": 0.1},  # 起终点重合 → 丢弃
        {"kind": "path", "x": 0, "y": 0, "d": "M0 0 L1 1 <script>"},   # 非法字符 → 丢弃
        {"kind": "path", "x": 0, "y": 0, "d": "L0 0"},                 # 非 M 开头 → 丢弃
    ])
    kinds = [op["kind"] for op in ops]
    assert kinds == ["text", "formula", "line", "arrow", "rect", "circle", "path", "text", "text"]
    assert ops[1]["latex"] == "a^2+b^2=c^2"
    assert ops[3]["width"] == 8
    assert ops[7]["x"] == 1.0 and ops[7]["y"] == 0.0
    assert len(warnings) >= 4


def test_whiteboard_ops_limits_and_examples():
    ops, warnings = ma.normalize_whiteboard_ops([{"kind": "text", "content": "x"} for _ in range(50)])
    assert len(ops) == ma.MAX_WHITEBOARD_OPS
    assert any("上限" in item for item in warnings)
    long_text, _ = ma.normalize_whiteboard_ops(
        [{"kind": "text", "content": "字" * (ma.MAX_WB_TEXT_CHARS + 50)}])
    assert len(long_text[0]["content"]) == ma.MAX_WB_TEXT_CHARS

    for name, example in ma.WHITEBOARD_EXAMPLES.items():
        assert 0 < len(example) <= ma.MAX_WHITEBOARD_OPS, name
        normalized, warnings = ma.normalize_whiteboard_ops(example)
        assert warnings == [], (name, warnings)
        assert len(normalized) == len(example), name
        again, _ = ma.normalize_whiteboard_ops(normalized)
        assert again == normalized, name
    sample = ma.sample_discussion_actions()
    assert sample["whiteboard"]["coord_space"] == "unit"
    assert sample["spotlight"]["type"] == "spotlight"


def test_multiagent_actions_whitelist_element_and_envelope():
    actions, warnings = ma.normalize_multiagent_actions([
        {"type": "spotlight", "element_id": "text_001"},
        {"type": "action", "name": "laser", "params": {"element_id": "latex_001"}},  # 上游信封
        {"type": "speech", "text": "台词应写在 text 字段"},
        {"type": "speech"},                                   # 缺文本
        {"type": "spotlight", "element_id": "missing_001"},   # 元素不存在
        {"type": "highlight", "element_id": "text_001"},
        {"type": "next_scene"},
        {"type": "show_slide", "index": 2},
        {"type": "play_audio"},                               # 缺 asset/url
        {"type": "unknown_action"},
        {"type": "whiteboard", "ops": [{"kind": "arrow", "x1": 0.1, "y1": 0.1, "x2": 0.5, "y2": 0.5}]},
    ], element_ids={"text_001", "latex_001"}, max_actions=12)
    types = [action["type"] for action in actions]
    assert types == ["spotlight", "laser", "highlight", "next_scene", "show_slide", "whiteboard"]
    assert [action["id"] for action in actions] == ["act_%03d" % index for index in range(1, 7)]
    assert any("speech" in item for item in warnings)
    assert any("missing_001" in item for item in warnings)
    assert any("未知" in item for item in warnings)

    # 单段动作数量上限
    limited, warnings = ma.normalize_multiagent_actions(
        [{"type": "laser", "element_id": "text_001"} for _ in range(10)],
        element_ids={"text_001"},
    )
    assert len(limited) == ma.MAX_ACTIONS_PER_TURN
    assert any("动作数量超过上限" in item for item in warnings)


def test_roster_normalization_rules():
    roster, warnings = ma.normalize_agent_roster(ROSTER_PAYLOAD)
    assert roster["teacher"]["role"] == "teacher"
    assert len(roster["classmates"]) == 3
    assert roster["classmates"][0]["role"] == "student"

    # 扁平 agents 列表 + assistant 角色 + 重复 id + 非法颜色/优先级
    flat, warnings = ma.normalize_agent_roster({"agents": [
        {"id": "t", "role": "teacher", "name": "林老师", "persona": "引导型老师，先问后讲。"},
        {"id": "student_1", "role": "assistant", "name": "助教小北", "persona": "负责补充例子。"},
        {"id": "student_1", "role": "student", "name": "小舟", "persona": "爱提问的同学。",
         "color": "not-a-color", "priority": 99},
    ]})
    assert flat["teacher"]["id"] == "t"
    assert flat["classmates"][0]["role"] == "assistant"
    assert flat["classmates"][0]["priority"] == 7
    assert flat["classmates"][1]["id"] != "student_1"
    assert flat["classmates"][1]["color"] in ma.AGENT_COLOR_PALETTE
    assert flat["classmates"][1]["priority"] == 5
    assert warnings

    for bad in (
        {"classmates": [{"name": "小舟", "persona": "同学。"}]},                       # 缺老师
        {"teacher": {"name": "林老师", "persona": "老师。"}, "classmates": []},        # 缺同学
        {"teacher": {"name": "", "persona": "老师。"},
         "classmates": [{"name": "小舟", "persona": "同学。"}]},                        # 空名字
        {"teacher": {"name": "林老师", "persona": ""},
         "classmates": [{"name": "小舟", "persona": "同学。"}]},                        # 空人设
        {"teacher": {"name": "林老师", "persona": "老师。"},
         "classmates": [{"name": "小舟", "persona": "同学。", "role": "teacher"}]},      # 第二个老师
        "不是 JSON 对象",                                                              # 非法 JSON
    ):
        with pytest.raises(ClassroomSchemaError):
            ma.normalize_agent_roster(bad)


def test_discussion_script_normalization_unit():
    roster, _ = ma.normalize_agent_roster(ROSTER_PAYLOAD)
    # speaker 名回退匹配 + 台词截断 + 越界 scene_hint 丢弃
    script, warnings = ma.normalize_discussion_script({
        "turns": [{"speaker_name": "小舟", "text": "字" * (ma.MAX_TURN_TEXT_CHARS + 20)}],
        "scene_hint": {"index": 9, "reason": "不存在"},
    }, roster=roster, element_ids=set(), scene_count=2)
    assert script["turns"][0]["speaker_id"] == "student_1"
    assert len(script["turns"][0]["text"]) == ma.MAX_TURN_TEXT_CHARS
    assert script["scene_hint"] is None
    assert any("截断" in item for item in warnings)
    # 空 turns / 非对象 / 非法 JSON
    for bad in ("nope", {}, {"turns": []}, {"turns": "x"}, [{"speaker_id": "teacher_1", "text": "x"}]):
        with pytest.raises(ClassroomSchemaError):
            ma.normalize_discussion_script(bad, roster=roster)
    # 名册外角色 / 空台词
    with pytest.raises(ClassroomSchemaError):
        ma.normalize_discussion_script(
            {"turns": [{"speaker_id": "ghost", "text": "台词"}]}, roster=roster)
    with pytest.raises(ClassroomSchemaError):
        ma.normalize_discussion_script(
            {"turns": [{"speaker_id": "teacher_1", "text": ""}]}, roster=roster)


def test_roster_config_helpers_and_merge_patch():
    roster, _ = ma.normalize_agent_roster(ROSTER_PAYLOAD)
    config = ma.with_roster_config({"document_ids": [3], "depth": "deep"}, roster,
                                   model="m1", provider_name="连接A")
    assert config["agents"]["teacher"]["name"] == "林老师"
    assert config["document_ids"] == [3] and config["depth"] == "deep"
    assert config["agents_meta"]["model"] == "m1"
    assert ma.roster_from_config(config)["teacher"]["name"] == "林老师"
    assert ma.roster_from_config({"agents": {"broken": True}}) == ma.empty_roster()
    assert ma.roster_from_config(None) == ma.empty_roster()

    edited, warnings = ma.merge_roster_patch(roster, teacher_patch={"id": "teacher_1", "name": "李老师"},
                                            classmate_patches=[{"id": "student_1", "color": "#123456"}])
    assert edited["teacher"]["name"] == "李老师"
    assert edited["classmates"][0]["color"] == "#123456"
    with pytest.raises(ClassroomSchemaError):
        ma.merge_roster_patch(roster, teacher_patch={"id": "ghost", "name": "x"})
    cleared = ma.without_roster_config(config)
    assert ma.CONFIG_KEY not in cleared and ma.CONFIG_META_KEY not in cleared
    assert not ma.has_roster(ma.empty_roster())
    _ = warnings


# ---------------------------------------------------------------------------
# 6. /meta 增量字段
# ---------------------------------------------------------------------------

def test_meta_exposes_multiagent_capabilities(client):
    body = client.get("/api/classroom/meta").json()
    assert body["multiagent_prompt_version"] == ma.PROMPT_VERSION
    assert {"teacher", "assistant", "student"} == {item["id"] for item in body["agent_roles"]}
    assert [item["id"] for item in body["discussion_modes"]] == ["discussion", "qa", "lecture"]
    limits = body["limits"]
    assert limits["agents_max"] == ma.MAX_AGENTS
    assert limits["classmates_max"] == ma.MAX_CLASSMATES
    assert limits["discussion_turns_max"] == ma.MAX_DISCUSSION_TURNS
    assert limits["interject_turns_max"] == ma.MAX_INTERJECT_TURNS
    assert limits["discussion_rounds_max"] == ma.MAX_ROUNDS
    assert limits["whiteboard_ops_max"] == ma.MAX_WHITEBOARD_OPS
    assert limits["whiteboard_text_max"] == ma.MAX_WB_TEXT_CHARS
    assert limits["whiteboard_kinds"] == list(ma.WHITEBOARD_KINDS)
    # 既有字段保持
    assert limits["scene_count_max"] == 12 and body["prompt_version"].startswith("classroom-")
