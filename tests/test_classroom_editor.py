"""P6a 幻灯片结构化编辑器验收：内容 PATCH（version CAS）/ 校验 / 越界钳制 / 归属 / 场景复制。

覆盖 ``app/classroom/editor.py`` 的路由契约：
- ``GET   /api/classroom/courses/{id}/scenes/{sid}/content``
- ``PATCH /api/classroom/courses/{id}/scenes/{sid}/content`` body ``{version, content}``
- ``POST  /api/classroom/courses/{id}/scenes/{sid}/duplicate``

不联网：只建课程/场景数据，不调用模型。前端 ``node --check`` 在测试机上无 Node 时自动跳过。
"""
import json
import shutil
import subprocess
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from app import db
from app.classroom import db as cdb
from app.classroom import editor, schema

REPO_ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------

def user_id_of(client):
    return client.get("/api/auth/me").json()["id"]


def create_course(client, **overrides):
    payload = {"topic": "光合作用", "subject": "生物", "grade": "初中二年级"}
    payload.update(overrides)
    response = client.post("/api/classroom/courses", json=payload)
    assert response.status_code == 200, response.text
    return response.json()["course"]


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


def slide_content(*elements, theme=None):
    return {
        "type": "slide",
        "schemaVersion": 1,
        "canvas": {
            "width": 1000,
            "height": 562.5,
            "theme": theme or {},
            "elements": list(elements),
        },
    }


def text_element(element_id="text_001", content="<p>光合作用</p>", **geometry):
    element = {"id": element_id, "type": "text", "left": 60, "top": 50,
               "width": 880, "height": 76, "content": content}
    element.update(geometry)
    return element


def patch_url(course_id, scene_id):
    return "/api/classroom/courses/%d/scenes/%d/content" % (course_id, scene_id)


def duplicate_url(course_id, scene_id):
    return "/api/classroom/courses/%d/scenes/%d/duplicate" % (course_id, scene_id)


def scene_row(scene_id):
    with db.connection() as con:
        row = con.execute("SELECT * FROM cls_scenes WHERE id=?", (scene_id,)).fetchone()
    return dict(row)


def make_slide_scene(client):
    course = create_course(client)
    scene_id = insert_scene(course["id"], user_id_of(client), "slide", 1, "光合作用入门",
                            slide_content(text_element()))
    return course, scene_id


# ---------------------------------------------------------------------------
# 1. 鉴权 / 归属
# ---------------------------------------------------------------------------

def test_editor_endpoints_require_login(client):
    from app.main import app

    course, scene_id = make_slide_scene(client)
    anon = TestClient(app)
    assert anon.get(patch_url(course["id"], scene_id)).status_code == 401
    assert anon.patch(patch_url(course["id"], scene_id),
                      json={"version": 1, "content": slide_content(text_element())}).status_code == 401
    assert anon.post(duplicate_url(course["id"], scene_id)).status_code == 401


def test_foreign_scene_is_404(client, second_client):
    course, scene_id = make_slide_scene(client)
    assert second_client.get(patch_url(course["id"], scene_id)).status_code == 404
    assert second_client.patch(
        patch_url(course["id"], scene_id),
        json={"version": 1, "content": slide_content(text_element())},
    ).status_code == 404
    assert second_client.post(duplicate_url(course["id"], scene_id)).status_code == 404


def test_scene_must_belong_to_course(client):
    course, scene_id = make_slide_scene(client)
    other_course = create_course(client, topic="细胞呼吸")
    # 场景不属于该课程 → 404（不泄露存在性）
    assert client.get(patch_url(other_course["id"], scene_id)).status_code == 404
    assert client.patch(patch_url(other_course["id"], scene_id),
                        json={"version": 1, "content": slide_content(text_element())}).status_code == 404
    # 不存在的场景 → 404
    assert client.get(patch_url(course["id"], 999999)).status_code == 404


# ---------------------------------------------------------------------------
# 2. 读取内容 / 成功保存 / 版本递增
# ---------------------------------------------------------------------------

def test_get_scene_content_returns_version_and_dsl(client):
    course, scene_id = make_slide_scene(client)
    body = client.get(patch_url(course["id"], scene_id)).json()
    scene = body["scene"]
    assert scene["id"] == scene_id
    assert scene["scene_type"] == "slide"
    assert scene["version"] == 1
    assert scene["content"]["type"] == "slide"
    assert scene["content"]["canvas"]["elements"][0]["id"] == "text_001"


def test_patch_success_bumps_version_and_updates_same_source(client):
    course, scene_id = make_slide_scene(client)
    content = slide_content(text_element(content="<p>新的标题</p>", left=120, top=80))
    response = client.patch(patch_url(course["id"], scene_id), json={"version": 1, "content": content})
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["scene"]["id"] == scene_id
    assert data["scene"]["version"] == 2
    elements = data["scene"]["content"]["canvas"]["elements"]
    assert elements[0]["left"] == 120
    assert "新的标题" in elements[0]["content"]
    assert data["warnings"] == []

    row = scene_row(scene_id)
    assert int(row["version"]) == 2
    stored = cdb.json_load(row["content_json"], {})
    assert stored["canvas"]["elements"][0]["left"] == 120

    # 播放器/导出读取同一 content_json：课程详情立即可见新内容
    detail = client.get("/api/classroom/courses/%d" % course["id"]).json()
    scene = next(item for item in detail["scenes"] if item["id"] == scene_id)
    assert scene["content"]["canvas"]["elements"][0]["left"] == 120


def test_patch_version_increments_across_saves(client):
    course, scene_id = make_slide_scene(client)
    version = 1
    for index in range(3):
        content = slide_content(text_element(left=60 + index * 10))
        response = client.patch(patch_url(course["id"], scene_id),
                                json={"version": version, "content": content})
        assert response.status_code == 200, response.text
        version = response.json()["scene"]["version"]
    assert version == 4
    assert int(scene_row(scene_id)["version"]) == 4


# ---------------------------------------------------------------------------
# 3. version CAS 冲突
# ---------------------------------------------------------------------------

def test_patch_version_conflict_returns_current_version(client):
    course, scene_id = make_slide_scene(client)
    first = client.patch(patch_url(course["id"], scene_id),
                         json={"version": 1, "content": slide_content(text_element(left=100))})
    assert first.status_code == 200, first.text
    assert first.json()["scene"]["version"] == 2
    # 仍用旧 version=1 保存 → 409，detail 携带服务端当前版本
    stale = client.patch(patch_url(course["id"], scene_id),
                         json={"version": 1, "content": slide_content(text_element(left=200))})
    assert stale.status_code == 409, stale.text
    detail = stale.json()["detail"]
    assert isinstance(detail, dict)
    assert detail["version"] == 2
    assert "冲突" in detail["message"] or "修改" in detail["message"]
    # 内容未被旧版本覆盖
    stored = cdb.json_load(scene_row(scene_id)["content_json"], {})
    assert stored["canvas"]["elements"][0]["left"] == 100
    # 用最新 version 重试可成功
    retry = client.patch(patch_url(course["id"], scene_id),
                         json={"version": detail["version"], "content": slide_content(text_element(left=200))})
    assert retry.status_code == 200, retry.text
    assert retry.json()["scene"]["version"] == 3


def test_patch_version_must_be_positive(client):
    course, scene_id = make_slide_scene(client)
    response = client.patch(patch_url(course["id"], scene_id),
                            json={"version": 0, "content": slide_content(text_element())})
    assert response.status_code == 422
    assert client.patch(patch_url(course["id"], scene_id), json={"content": slide_content()}).status_code == 422


# ---------------------------------------------------------------------------
# 4. 只能编辑幻灯片
# ---------------------------------------------------------------------------

def test_patch_rejects_non_slide_scene(client):
    course = create_course(client)
    user_id = user_id_of(client)
    quiz_id = insert_scene(course["id"], user_id, "quiz", 1, "课堂小测",
                           {"type": "quiz", "questions": [{
                               "id": "q1", "type": "single", "question": "光合作用场所？",
                               "options": [{"label": "叶绿体", "value": "A"},
                                           {"label": "线粒体", "value": "B"}],
                               "answer": ["A"], "points": 5}]})
    response = client.patch(patch_url(course["id"], quiz_id),
                            json={"version": 1, "content": slide_content(text_element())})
    assert response.status_code == 422
    assert "只有幻灯片场景支持编辑" in response.json()["detail"]

    interactive_id = insert_scene(course["id"], user_id, "interactive", 2, "模拟", {
        "type": "interactive", "widget": "simulation", "height": 480,
        "html": "<!DOCTYPE html><html><body><div id=\"experiment\"></div></body></html>",
    })
    response = client.patch(patch_url(course["id"], interactive_id),
                            json={"version": 1, "content": slide_content(text_element())})
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# 5. 非法内容 422（中文原因）
# ---------------------------------------------------------------------------

def test_patch_rejects_invalid_content(client):
    course, scene_id = make_slide_scene(client)
    cases = [
        ({}, "elements"),                                      # 缺少元素
        ({"canvas": {"elements": []}}, "至少需要 1 个元素"),     # 空元素
        ("不是对象", "JSON 对象"),                               # content 非对象
        (slide_content({"id": "t1", "type": "unknown-widget", "content": "<p>x</p>"}), "类型不支持"),
        (slide_content(*[text_element("t%d" % i) for i in range(schema.MAX_ELEMENTS + 1)]),
         "最多 %d 个" % schema.MAX_ELEMENTS),
        ({"canvas": {"elements": [{"type": "image", "src": ""}]}}, "没有任何可用元素"),
        ({"canvas": {"elements": ["不是对象"]}}, "必须是 JSON 对象"),
        ({"canvas": {"elements": [{"content": "<p>缺 type</p>"}]}}, "缺少 type"),
    ]
    for content, expected in cases:
        response = client.patch(patch_url(course["id"], scene_id),
                                json={"version": 1, "content": content})
        assert response.status_code == 422, (content, response.text)
        detail = response.json()["detail"]
        text = detail if isinstance(detail, str) else json.dumps(detail, ensure_ascii=False)
        assert expected in text, (content, text)


def test_patch_missing_content_returns_422(client):
    course, scene_id = make_slide_scene(client)
    response = client.patch(patch_url(course["id"], scene_id), json={"version": 1})
    assert response.status_code == 422
    assert "JSON 对象" in response.json()["detail"]


# ---------------------------------------------------------------------------
# 6. 归一化：越界钳制 / HTML 净化 / 丢弃元素有 warnings
# ---------------------------------------------------------------------------

def test_patch_clamps_out_of_bounds_geometry(client):
    course, scene_id = make_slide_scene(client)
    out_of_bounds = text_element("text_oob", left=5000, top=-200, width=200, height=100,
                                 rotate=999)
    response = client.patch(patch_url(course["id"], scene_id),
                            json={"version": 1, "content": slide_content(out_of_bounds)})
    assert response.status_code == 200, response.text
    element = response.json()["scene"]["content"]["canvas"]["elements"][0]
    assert 0 <= element["left"] <= schema.CANVAS_WIDTH - element["width"]
    assert 0 <= element["top"] <= schema.CANVAS_HEIGHT - element["height"]
    assert -360 <= element["rotate"] <= 360


def test_patch_sanitizes_text_html(client):
    course, scene_id = make_slide_scene(client)
    dirty = ('<p style="font-size: 24px; color: #ff0000">安全文本</p>'
             '<script>alert(1)</script><img src=x onerror=alert(1)>')
    response = client.patch(patch_url(course["id"], scene_id),
                            json={"version": 1, "content": slide_content(text_element(content=dirty))})
    assert response.status_code == 200, response.text
    element = response.json()["scene"]["content"]["canvas"]["elements"][0]
    assert "<script" not in element["content"]
    assert "onerror" not in element["content"]
    assert "安全文本" in element["content"]
    assert element["fontSize"] == 24
    assert element["color"] == "#ff0000"


def test_patch_reports_dropped_elements_as_warnings(client):
    course, scene_id = make_slide_scene(client)
    content = slide_content(
        text_element(),
        {"id": "img_bad", "type": "image", "left": 10, "top": 10, "width": 100, "height": 100},
    )
    response = client.patch(patch_url(course["id"], scene_id),
                            json={"version": 1, "content": content})
    assert response.status_code == 200, response.text
    data = response.json()
    assert len(data["scene"]["content"]["canvas"]["elements"]) == 1
    assert any("图片" in warning for warning in data["warnings"])


# ---------------------------------------------------------------------------
# 7. 场景复制
# ---------------------------------------------------------------------------

def test_duplicate_scene_appends_and_returns_new_scene(client):
    course, scene_id = make_slide_scene(client)
    response = client.post(duplicate_url(course["id"], scene_id))
    assert response.status_code == 200, response.text
    data = response.json()
    new_scene = data["scene"]
    assert new_scene["id"] != scene_id
    assert new_scene["version"] == 1
    assert new_scene["order_index"] == 2
    assert new_scene["title"].endswith("（副本）")
    assert new_scene["content"]["canvas"]["elements"][0]["id"] == "text_001"
    assert data["course_id"] == course["id"]

    detail = client.get("/api/classroom/courses/%d" % course["id"]).json()
    assert len(detail["scenes"]) == 2
    assert detail["course"]["scene_count"] == 2
    # 副本可独立编辑（版本从 1 开始）
    patched = client.patch(patch_url(course["id"], new_scene["id"]),
                           json={"version": 1, "content": slide_content(text_element(left=222))})
    assert patched.status_code == 200, patched.text
    assert int(scene_row(new_scene["id"])["version"]) == 2
    # 原场景未受影响
    assert int(scene_row(scene_id)["version"]) == 1

    # 再复制一次：order 继续顺延
    again = client.post(duplicate_url(course["id"], scene_id))
    assert again.status_code == 200
    assert again.json()["scene"]["order_index"] == 3
    assert client.get("/api/classroom/courses/%d" % course["id"]).json()["course"]["scene_count"] == 3


def test_duplicate_missing_scene_is_404(client):
    course, _scene_id = make_slide_scene(client)
    assert client.post(duplicate_url(course["id"], 999999)).status_code == 404
    other_course = create_course(client, topic="细胞呼吸")
    assert client.post(duplicate_url(other_course["id"], 999999)).status_code == 404


# ---------------------------------------------------------------------------
# 8. 迁移幂等
# ---------------------------------------------------------------------------

def test_ensure_columns_is_idempotent(client):
    editor.ensure_columns()
    editor.ensure_columns()
    with db.connection() as con:
        columns = {row[1] for row in con.execute("PRAGMA table_info(cls_scenes)")}
    assert "version" in columns


# ---------------------------------------------------------------------------
# 9. 前端静态检查（有 Node 时）
# ---------------------------------------------------------------------------

NODE = shutil.which("node")
FRONTEND_FILES = (
    REPO_ROOT / "web" / "js" / "components" / "classroomEditor.js",
    REPO_ROOT / "web" / "js" / "pages" / "classroom.js",
)


@pytest.mark.skipif(NODE is None, reason="本机未安装 Node，跳过前端语法检查")
def test_editor_frontend_syntax():
    for path in FRONTEND_FILES:
        assert path.is_file(), path
        completed = subprocess.run(
            [NODE, "--check", str(path)],
            capture_output=True, text=True, timeout=60,
        )
        assert completed.returncode == 0, "%s\n%s" % (path, completed.stderr)
