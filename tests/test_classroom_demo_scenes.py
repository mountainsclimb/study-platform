"""M4 验收：课堂「版本化学习附件演示场景」（demo）+ 固定版本导出。

覆盖：

1. 学习附件 → ``POST /courses/{id}/scenes/demo`` 插入固定版本演示场景（当前版本 / 指定旧版本），
   ``GET /courses/{id}`` 能读回完整附件快照，``GET /meta`` 只追加 ``manual_scene_types`` /
   ``demo_artifact_kinds`` / ``limits.demo_content_bytes`` 且既有 ``scene_types`` 仍是 4 类；
2. 错误口径：他人课程 / 他人附件 / 不存在附件 404，非法附件类型与超限内容 422，
   生成任务运行中与场景上限 409，AI 生成流程不会被分配 demo；
3. ``DELETE /courses/{id}/scenes/{scene_id}`` 仅演示场景可删、删除后 order_index 连续重排；
4. 固定版本导出：PPTX 一页「固定版本」说明（含海报）、离线 HTML / 打印讲义（PDF 渲染入口）
   含 ``data-fixed-revision`` 与 ``data-artifact-id`` 的静态片段、ZIP 含 ``demos/`` JSON；
5. schema ``normalize_demo_content`` 的限额与中文错误口径。

全部离线：不调用模型与网络，导出走 TestClient + 临时数据目录（``client`` 夹具）。
"""
from __future__ import annotations

import base64
import io
import json
import zipfile

import pytest

from app import db
from app.classroom import exports, schema
from app.classroom import db as cdb


# 1×1 透明 PNG（与 tests/test_classroom_export.py 相同的合法最小海报）。
TINY_PNG = (
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)

FLOW_DATA = {
    "layout": "loop",
    "style": "sketch",
    "title": "复习闭环流程",
    "nodes": [
        {"id": "n1", "label": "答错", "kind": "start"},
        {"id": "n2", "label": "归因", "kind": "process"},
        {"id": "n3", "label": "到期复习", "kind": "end"},
    ],
    "edges": [{"from": "n1", "to": "n2"}, {"from": "n2", "to": "n3"}],
    "steps": [{"label": "答错先归因"}, {"label": "做变式"}, {"label": "按到期复习"}],
}

FUNCTION_DATA = {
    "version": 1,
    "source": "mafs-compatible",
    "expressions": [{"id": "f1", "latex": "x^2", "color": "#2b7de9", "visible": True}],
    "params": [],
    "xRange": [-5, 5],
    "yRange": [-5, 5],
    "points": [],
    "labels": [],
}


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------

def user_id_of(client):
    return client.get("/api/auth/me").json()["id"]


def save_provider(client, name="演示场景测试连接", models=("test-model",)):
    response = client.post("/api/providers", json={
        "name": name, "kind": "openai", "base_url": "https://api.openai.com/v1",
        "api_key": "sk-classroom-demo-unit-test", "models": list(models),
    })
    assert response.status_code == 200, response.text
    return response.json()


def create_course(client, **overrides):
    payload = {"topic": "原理演示课堂", "subject": "物理", "grade": "高中"}
    payload.update(overrides)
    response = client.post("/api/classroom/courses", json=payload)
    assert response.status_code == 200, response.text
    return response.json()["course"]


def create_artifact(client, *, kind="flow_animation", title="流程演示附件",
                    data=None, preview=None):
    payload = {"kind": kind, "title": title, "data": data if data is not None else FLOW_DATA}
    if preview is not None:
        # 允许直接传 PNG data URI（课堂 `preview.png` 口径）或完整 preview 对象。
        payload["preview"] = {"png": preview} if isinstance(preview, str) else preview
    response = client.post("/api/artifacts", json=payload)
    assert response.status_code == 201, response.text
    body = response.json()
    return {
        "artifact_id": body["artifact"]["artifact_id"],
        "revision": body["revision"]["revision"],
        "artifact": body["artifact"],
    }


def insert_scene(course_id, user_id, scene_type, order_index, title, content,
                 *, meta=None, status="ready"):
    now = db.utcnow()
    with db.connection() as con:
        return con.execute(
            "INSERT INTO cls_scenes(course_id,user_id,scene_type,order_index,title,content_json,"
            "meta_json,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (course_id, user_id, scene_type, order_index, title, cdb.json_dump(content),
             cdb.json_dump(meta or {}), status, now, now),
        ).lastrowid


def attach_demo(client, course_id, artifact, **payload):
    body = {"artifact_id": artifact["artifact_id"], **payload}
    return client.post(f"/api/classroom/courses/{course_id}/scenes/demo", json=body)


def course_scenes(client, course_id):
    response = client.get(f"/api/classroom/courses/{course_id}")
    assert response.status_code == 200, response.text
    return response.json()["scenes"]


def demo_scene_of(scenes):
    demo = next((item for item in scenes if item["scene_type"] == "demo"), None)
    assert demo is not None, scenes
    return demo


def insert_running_course_job(course_id, user_id):
    """构造一条 running course 任务（与 tests/test_classroom_api.py 同口径）。"""
    now = db.utcnow()
    with db.connection() as con:
        return con.execute(
            "INSERT INTO ai_jobs(user_id,task,status,stage,request_json,partial_content,cancelled,"
            "created_at,updated_at) VALUES (?,?,?,?,?,'',0,?,?)",
            (user_id, "course", "running", "running",
             json.dumps({"task": "course", "provider_id": 1, "model": "m",
                         "params": {"course_id": course_id}}, ensure_ascii=False), now, now),
        ).lastrowid


def _all_pptx_text(prs):
    texts = []
    for slide in prs.slides:
        for shape in slide.shapes:
            if getattr(shape, "has_text_frame", False):
                texts.append(shape.text_frame.text)
            if getattr(shape, "has_table", False):
                for row in shape.table.rows:
                    for cell in row.cells:
                        texts.append(cell.text)
    return "\n".join(texts)


def _pptx_notes(prs):
    return "\n".join(
        slide.notes_slide.notes_text_frame.text for slide in prs.slides
        if getattr(slide, "has_notes_slide", False)
    )


def _offline_snapshot(html: str) -> dict:
    raw = html.split('<script id="course-data" type="application/json">', 1)[1]
    raw = raw.split("</script>", 1)[0]
    return json.loads(raw.replace("<\\/", "</"))


# ---------------------------------------------------------------------------
# 1. /meta 能力位 + schema 白名单
# ---------------------------------------------------------------------------

def test_meta_declares_manual_demo_scene_type_without_changing_generation_types(client):
    meta = client.get("/api/classroom/meta").json()
    # 手工类型独立声明，AI 生成类型保持既有 4 类（test_classroom_api.py 明确断言）。
    assert meta["manual_scene_types"] == ["demo"]
    assert set(meta["scene_types"]) == {"slide", "quiz", "interactive", "pbl"}
    assert meta["scene_type_labels"]["demo"] == "原理演示"
    # 演示可用的附件类型（按场景类型分组，key 含 demo；前端据此过滤附件选择器）。
    assert "demo" in meta["demo_artifact_kinds"]
    assert meta["demo_artifact_kinds"]["demo"] == list(schema.DEMO_ARTIFACT_KINDS)
    assert meta["limits"]["demo_content_bytes"] == schema.MAX_DEMO_CONTENT_BYTES

    # schema 层：既有常量不变，新增独立常量；AI 生成类型归一化不会产出 demo。
    assert schema.SCENE_TYPES == ("slide", "quiz", "interactive", "pbl")
    assert schema.MANUAL_SCENE_TYPES == ("demo",)
    assert schema.ALL_SCENE_TYPES == schema.SCENE_TYPES + ("demo",)
    assert schema.normalize_scene_type("原理演示") == "demo"
    assert schema.normalize_scene_types(["demo", "slide", "quiz"]) == ["slide", "quiz"]


# ---------------------------------------------------------------------------
# 2. 插入（当前版本）与读回
# ---------------------------------------------------------------------------

def test_insert_demo_scene_freezes_current_revision_and_reads_back(client):
    function_artifact = create_artifact(
        client, kind="function_graph", title="函数图演示", data=FUNCTION_DATA,
    )
    flow_artifact = create_artifact(client, kind="flow_animation", title="流程演示附件",
                                    preview=TINY_PNG)
    course = create_course(client)
    uid = user_id_of(client)
    insert_scene(course["id"], uid, "slide", 1, "引入", {
        "type": "slide",
        "canvas": {"width": 1000, "height": 562.5, "theme": {}, "elements": [
            {"id": "t1", "type": "text", "left": 60, "top": 60, "width": 800, "height": 80,
             "content": "<p>引入</p>"},
        ]},
    })

    first = attach_demo(client, course["id"], function_artifact,
                        source_note="课堂固定版本说明")
    assert first.status_code == 201, first.text
    first_body = first.json()
    scene = first_body["scene"]
    assert scene["scene_type"] == "demo"
    assert scene["type"] == "demo"
    assert scene["order_index"] == 2  # 追加到既有 slide 之后
    assert scene["status"] == "ready"
    assert scene["title"] == "函数图演示"
    content = scene["content"]
    assert content["type"] == "demo"
    assert content["fixed"] is True
    assert content["fixed_revision"] == function_artifact["revision"] == 1
    assert content["artifact"]["artifact_id"] == function_artifact["artifact_id"]
    assert content["artifact"]["revision"] == 1
    assert content["artifact"]["kind"] == "function_graph"
    assert content["artifact"]["data"] == FUNCTION_DATA
    assert content["source_note"] == "课堂固定版本说明"
    assert any("固定版本" in warning for warning in scene["meta"]["warnings"])
    assert first_body["course"] == {"id": course["id"], "scene_count": 2}

    second = attach_demo(client, course["id"], flow_artifact,
                         title="自定义演示标题", source_note="第二段说明")
    assert second.status_code == 201, second.text
    second_scene = second.json()["scene"]
    assert second_scene["order_index"] == 3
    assert second_scene["title"] == "自定义演示标题"
    assert second_scene["content"]["artifact"]["preview"]["png"] == TINY_PNG

    scenes = course_scenes(client, course["id"])
    assert [item["scene_type"] for item in scenes] == ["slide", "demo", "demo"]
    assert [item["order_index"] for item in scenes] == [1, 2, 3]
    demo = demo_scene_of(scenes)
    assert demo["content"] == scene["content"]  # GET /courses 读回完整快照
    # 课程列表/详情的 scene_count 同步
    course_row = next(item for item in client.get("/api/classroom/courses").json()["items"]
                      if item["id"] == course["id"])
    assert course_row["scene_count"] == 3
    # 演示场景不直出 HTML（安全口径，中文提示）。
    html_response = client.get(f"/api/classroom/scenes/{scene['id']}/html")
    assert html_response.status_code == 404
    assert "原理演示" in html_response.json()["detail"]


# ---------------------------------------------------------------------------
# 3. 指定旧版本固定 + 导出使用旧版本
# ---------------------------------------------------------------------------

def test_explicit_revision_freezes_old_version_and_exports_use_it(client):
    artifact = create_artifact(client, kind="flow_animation", title="固定版本演示",
                               preview=TINY_PNG)
    course = create_course(client)
    first = attach_demo(client, course["id"], artifact, revision=1,
                        source_note="按 v1 固定")
    assert first.status_code == 201, first.text
    scene = first.json()["scene"]
    assert scene["content"]["artifact"]["revision"] == 1
    assert scene["content"]["artifact"]["data"]["title"] == "复习闭环流程"

    # 源附件升到 v2：数据变化，但课堂场景仍是 v1 快照。
    upgraded = client.patch(f"/api/artifacts/{artifact['artifact_id']}", json={
        "data": {**FLOW_DATA, "title": "复习闭环流程 v2"},
        "expected_revision": 1,
    })
    assert upgraded.status_code == 200, upgraded.text
    assert upgraded.json()["revision"]["revision"] == 2
    demo = demo_scene_of(course_scenes(client, course["id"]))
    assert demo["content"]["artifact"]["revision"] == 1
    assert demo["content"]["artifact"]["data"]["title"] == "复习闭环流程"
    assert demo["content"]["fixed_revision"] == 1

    # 不传 revision 的新场景取当前 v2。
    current = attach_demo(client, course["id"], artifact)
    assert current.status_code == 201, current.text
    assert current.json()["scene"]["content"]["artifact"]["revision"] == 2

    # PPTX：demo 页文字含「固定版本 v1」，且导出走演示页而非占位页。
    from pptx import Presentation

    pptx_response = client.get(
        f"/api/classroom/courses/{course['id']}/export", params={"format": "pptx"},
    )
    assert pptx_response.status_code == 200, pptx_response.text
    prs = Presentation(io.BytesIO(pptx_response.content))
    assert len(prs.slides) == 3  # 标题页 + 2 个演示场景
    text = _all_pptx_text(prs)
    assert "固定版本 v1" in text
    assert "固定版本 v2" in text
    assert "原理演示 · 流程动画" in text
    assert "按 v1 固定" in text
    # v2 场景是 PATCH 后的新版本（未携带 preview）→ 走中文海报提示；v1 场景有 PNG 海报。
    assert "未附加海报图像" in text
    from pptx.enum.shapes import MSO_SHAPE_TYPE

    assert any(shape.shape_type == MSO_SHAPE_TYPE.PICTURE
               for slide in prs.slides for shape in slide.shapes)
    notes = _pptx_notes(prs)
    assert artifact["artifact_id"] in notes

    # 离线 HTML：静态片段固定 data-fixed-revision，内嵌课程 JSON 也是旧版本。
    html_response = client.get(
        f"/api/classroom/courses/{course['id']}/export", params={"format": "html"},
    )
    assert html_response.status_code == 200, html_response.text
    html = html_response.text
    assert 'data-fixed-revision="1"' in html
    assert f'data-artifact-id="{artifact["artifact_id"]}"' in html
    snapshot = _offline_snapshot(html)
    demo_scene = next(item for item in snapshot["scenes"] if item["type"] == "demo")
    assert demo_scene["content"]["fixed_revision"] == 1
    assert demo_scene["content"]["artifact"]["data"]["title"] == "复习闭环流程"


# ---------------------------------------------------------------------------
# 4. 错误口径（404 / 409 / 422）
# ---------------------------------------------------------------------------

def test_demo_insert_error_contract(client, second_client):
    artifact = create_artifact(client)
    course = create_course(client)

    # 他人课程 → 404
    foreign_artifact = create_artifact(second_client, title="他人附件")
    assert attach_demo(second_client, course["id"], artifact).status_code == 404
    # 他人附件 → 404（不泄露存在性）
    assert attach_demo(client, course["id"], foreign_artifact).status_code == 404
    # 附件不存在 → 404
    assert attach_demo(client, course["id"], {"artifact_id": "missing-artifact"}).status_code == 404
    # 附件 ID 格式非法 / 缺少参数 → 422
    assert attach_demo(client, course["id"], {"artifact_id": "bad id!"}).status_code == 422
    assert client.post(
        f"/api/classroom/courses/{course['id']}/scenes/demo", json={},
    ).status_code == 422
    # 版本号越界 → 422
    assert attach_demo(client, course["id"], artifact, revision=0).status_code == 422
    assert attach_demo(
        client, course["id"], artifact, revision=schema.MAX_DEMO_REVISION + 1,
    ).status_code == 422
    # 说明超长 → 422（Pydantic 上限 300）
    assert attach_demo(client, course["id"], artifact, source_note="说" * 301).status_code == 422

    # 附件类型存在但不在演示白名单 → 422
    reading = create_artifact(client, kind="reading_excerpt", title="阅读摘录",
                              data={"text": "摘录"})
    response = attach_demo(client, course["id"], reading)
    assert response.status_code == 422
    assert "不支持" in response.json()["detail"]

    # 附件 data 超过课堂侧 200KB 上限（附件本身允许 256KB）→ 422
    big = create_artifact(client, title="超大附件", data={"blob": "x" * (210 * 1024)})
    response = attach_demo(client, course["id"], big)
    assert response.status_code == 422
    assert "200KB" in response.json()["detail"]

    # 生成任务运行中 → 409
    uid = user_id_of(client)
    insert_running_course_job(course["id"], uid)
    response = attach_demo(client, course["id"], artifact)
    assert response.status_code == 409
    with db.connection() as con:
        con.execute("DELETE FROM ai_jobs WHERE task='course'")

    # 场景数量上限 → 409（monkeypatch 上限，避免建满 12 个场景）
    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setattr(schema, "MAX_SCENE_COUNT", 1)
        first = attach_demo(client, course["id"], artifact)
        assert first.status_code == 201, first.text
        response = attach_demo(client, course["id"], artifact)
        assert response.status_code == 409
        assert "上限" in response.json()["detail"]
    finally:
        monkeypatch.undo()

    # AI 生成流程仍以既有 4 类为白名单：显式要求 demo 生成 → 422。
    save_provider(client)
    response = client.post(f"/api/classroom/courses/{course['id']}/generate", json={
        "stage": "outline", "provider_id": 1, "model": "test-model",
        "scene_types": ["demo"],
    })
    assert response.status_code == 422
    assert "不支持" in response.json()["detail"]


# ---------------------------------------------------------------------------
# 5. 删除演示场景 + order_index 连续重排
# ---------------------------------------------------------------------------

def test_delete_demo_scene_renumbers_orders_and_guards(client, second_client):
    course = create_course(client)
    uid = user_id_of(client)
    slide = {
        "type": "slide",
        "canvas": {"width": 1000, "height": 562.5, "theme": {}, "elements": [
            {"id": "t1", "type": "text", "left": 60, "top": 60, "width": 800, "height": 80,
             "content": "<p>说明</p>"},
        ]},
    }
    insert_scene(course["id"], uid, "slide", 1, "第一页", slide)
    artifact = create_artifact(client)
    attached = attach_demo(client, course["id"], artifact)
    assert attached.status_code == 201, attached.text
    demo_id = attached.json()["scene"]["id"]
    insert_scene(course["id"], uid, "slide", 3, "第三页", slide)

    removed = client.delete(f"/api/classroom/courses/{course['id']}/scenes/{demo_id}")
    assert removed.status_code == 200, removed.text
    assert removed.json()["deleted"] is True
    assert removed.json()["scene_id"] == demo_id
    assert removed.json()["course"] == {"id": course["id"], "scene_count": 2}

    scenes = course_scenes(client, course["id"])
    assert [item["scene_type"] for item in scenes] == ["slide", "slide"]
    assert [item["order_index"] for item in scenes] == [0, 1]  # 连续重排 0..n-1

    # 删除后继续插入：order_index 追加到重排后的末尾。
    again = attach_demo(client, course["id"], artifact)
    assert again.status_code == 201, again.text
    assert again.json()["scene"]["order_index"] == 2

    # 非演示场景（slide）不允许删除 → 409；他人课程 → 404；不存在场景 → 404。
    slide_id = scenes[0]["id"]
    response = client.delete(f"/api/classroom/courses/{course['id']}/scenes/{slide_id}")
    assert response.status_code == 409
    assert "仅演示场景" in response.json()["detail"]
    assert second_client.delete(
        f"/api/classroom/courses/{course['id']}/scenes/{demo_id}",
    ).status_code == 404
    assert client.delete(
        f"/api/classroom/courses/{course['id']}/scenes/999999",
    ).status_code == 404


# ---------------------------------------------------------------------------
# 6. 固定版本导出：PPTX / 离线 HTML / 打印讲义（PDF 入口）/ ZIP
# ---------------------------------------------------------------------------

def test_exports_render_fixed_version_demo_scene(client):
    artifact = create_artifact(
        client, kind="flow_animation", title="导出固定版本演示", preview=TINY_PNG,
    )
    plain_artifact = create_artifact(
        client, kind="science_demo", title="无海报演示", data={"assumptions": ["理想条件"]},
    )
    course = create_course(client)
    uid = user_id_of(client)
    insert_scene(course["id"], uid, "slide", 1, "课程引入", {
        "type": "slide",
        "canvas": {"width": 1000, "height": 562.5, "theme": {}, "elements": [
            {"id": "t1", "type": "text", "left": 60, "top": 60, "width": 800, "height": 80,
             "content": "<p>引入</p>", "text": "引入", "fontSize": 20},
        ]},
    })
    assert attach_demo(client, course["id"], artifact,
                       source_note="导出固定版本说明").status_code == 201
    assert attach_demo(client, course["id"], plain_artifact).status_code == 201

    # ---- PPTX：标题页 + slide 1 页 + 2 个演示页；含固定版本/说明/海报，无 PNG 时给中文提示。
    from pptx import Presentation
    from pptx.enum.shapes import MSO_SHAPE_TYPE

    pptx_response = client.get(
        f"/api/classroom/courses/{course['id']}/export", params={"format": "pptx"},
    )
    assert pptx_response.status_code == 200, pptx_response.text
    assert pptx_response.content[:2] == b"PK"
    prs = Presentation(io.BytesIO(pptx_response.content))
    assert len(prs.slides) >= 4
    text = _all_pptx_text(prs)
    assert "固定版本 v1" in text
    assert "原理演示 · 流程动画" in text
    assert "导出固定版本说明" in text
    assert "导出固定版本演示" in text
    assert "未附加海报图像" in text  # 第二个演示无 preview.png
    assert "科学演示" in text
    pictures = [shape for slide in prs.slides for shape in slide.shapes
                if shape.shape_type == MSO_SHAPE_TYPE.PICTURE]
    assert pictures, "有 preview.png 的演示应插入海报图片"
    notes = _pptx_notes(prs)
    assert "固定版本 v1" in notes and artifact["artifact_id"] in notes

    # ---- 离线 HTML：静态演示片段（固定版本标识 + 内嵌快照 JSON）+ 播放器数据。
    html_response = client.get(
        f"/api/classroom/courses/{course['id']}/export", params={"format": "html"},
    )
    assert html_response.status_code == 200, html_response.text
    html = html_response.text
    assert 'class="cls-demo-scene"' in html
    assert 'data-scene-type="demo"' in html
    assert f'data-artifact-id="{artifact["artifact_id"]}"' in html
    assert 'data-fixed-revision="1"' in html
    assert 'class="cls-demo-data"' in html
    assert "data:image/png;base64," in html
    assert "未附加海报图像" in html
    snapshot = _offline_snapshot(html)
    demo = next(item for item in snapshot["scenes"] if item["type"] == "demo")
    assert demo["content"]["fixed"] is True
    assert demo["content"]["artifact"]["revision"] == 1
    assert demo["content"]["artifact"]["preview"]["png"] == TINY_PNG

    # ---- 打印讲义（PDF 渲染入口）：同一静态片段，无外链。
    scenes = cdb.course_scenes(course["id"], uid)
    print_html = exports.build_print_html(course, scenes)
    assert 'class="cls-demo-scene"' in print_html
    assert 'data-fixed-revision="1"' in print_html
    assert 'class="cls-demo-data"' in print_html
    assert "固定版本 v1" in print_html
    assert "http://" not in print_html and "https://" not in print_html
    # 服务端 PDF：渲染引擎可用则返回 PDF，不可用时按既有口径 503 中文引导。
    pdf_response = client.get(
        f"/api/classroom/courses/{course['id']}/export", params={"format": "pdf"},
    )
    assert pdf_response.status_code in (200, 503)
    if pdf_response.status_code == 200:
        assert pdf_response.content[:4] == b"%PDF"

    # ---- ZIP：scenes/*.json 保留完整快照，额外写 demos/ JSON + manifest 清单。
    zip_response = client.get(
        f"/api/classroom/courses/{course['id']}/export", params={"format": "zip"},
    )
    assert zip_response.status_code == 200, zip_response.text
    with zipfile.ZipFile(io.BytesIO(zip_response.content)) as archive:
        names = archive.namelist()
        demo_json = sorted(
            name for name in names if name.startswith("demos/") and name.endswith(".json")
        )
        demo_html = sorted(
            name for name in names if name.startswith("demos/") and name.endswith(".html")
        )
        assert len(demo_json) == 2 and len(demo_html) == 2
        assert demo_json[0].startswith("demos/scene-02-")
        demo_payload = json.loads(archive.read(demo_json[0]).decode("utf-8"))
        assert demo_payload["format"] == "zhixu-classroom-demo-snapshot"
        assert demo_payload["fixed"] is True
        assert demo_payload["fixedRevision"] == 1
        assert demo_payload["artifact"]["artifact_id"] == artifact["artifact_id"]
        assert "固定版本" in demo_payload["fixedNote"]
        standalone = archive.read(demo_html[0]).decode("utf-8")
        assert "data-offline-demo" in standalone
        assert "application/json" in standalone
        assert "http://" not in standalone and "https://" not in standalone
        assert 'src="http' not in standalone and "<link" not in standalone
        assert 'src="data:image/png;base64,' in standalone
        manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
        assert len(manifest["demoScenes"]) == 2
        assert all(entry.get("htmlPath") for entry in manifest["demoScenes"])
        assert "demo-scenes" in manifest["capabilities"]
        scene_snapshot = json.loads(archive.read("scenes/scene_002.json").decode("utf-8"))
        assert scene_snapshot["type"] == "demo"
        assert scene_snapshot["content"]["fixed_revision"] == 1
        assert scene_snapshot["content"]["artifact"]["artifact_id"] == artifact["artifact_id"]

    # ---- 教学资源包：内嵌 PPTX 同样按固定版本导出演示页，README 标注原理演示。
    package_response = client.get(
        f"/api/classroom/courses/{course['id']}/export", params={"format": "package"},
    )
    assert package_response.status_code == 200, package_response.text
    with zipfile.ZipFile(io.BytesIO(package_response.content)) as archive:
        package_names = set(archive.namelist())
        assert {"course.pptx", "course.json", "README.txt"} <= package_names
        package_pptx = Presentation(io.BytesIO(archive.read("course.pptx")))
        assert "固定版本 v1" in _all_pptx_text(package_pptx)
        readme = archive.read("README.txt").decode("utf-8")
        assert "原理演示" in readme
        assert "固定版本" in readme


# ---------------------------------------------------------------------------
# 7. schema.normalize_demo_content 限额与错误口径（直接单元覆盖）
# ---------------------------------------------------------------------------

def _raw_demo(**overrides):
    raw = {
        "artifact": {
            "artifact_id": "abcd1234efgh",
            "revision": 2,
            "kind": "flow_animation",
            "title": "流程演示",
            "data": {"steps": [{"label": "第一步"}]},
            "preview": {"kind": "principle-demo"},
        },
        "source_note": "课堂说明",
        "assumptions": ["假设一"],
        "step_count": 1,
        "fixed": True,
    }
    raw.update(overrides)
    return raw


def test_normalize_demo_content_ok_and_limits():
    content, warnings = schema.normalize_demo_content(_raw_demo())
    assert content["type"] == "demo"
    assert content["fixed"] is True
    assert content["fixed_revision"] == 2
    assert content["artifact"]["artifact_id"] == "abcd1234efgh"
    assert content["artifact"]["revision"] == 2
    assert content["artifact"]["preview"] == {"kind": "principle-demo"}
    assert content["step_count"] == 1
    assert warnings == [
        "演示为固定版本 v2：课堂与导出使用该版本，源附件更新不会自动改变",
    ]
    # normalize_scene_content 统一按类型分派（_CONTENT_NORMALIZERS["demo"] 已注册）。
    normalized, scene_warnings = schema.normalize_scene_content("demo", _raw_demo())
    assert normalized["type"] == "demo"
    assert scene_warnings == warnings

    bad_cases = [
        {"artifact": "not-a-dict"},
        {"artifact": {**_raw_demo()["artifact"], "artifact_id": "abc"}},
        {"artifact": {**_raw_demo()["artifact"], "artifact_id": "bad id!"}},
        {"artifact": {**_raw_demo()["artifact"], "artifact_id": "x" * 65}},
        {"artifact": {**_raw_demo()["artifact"], "revision": 0}},
        {"artifact": {**_raw_demo()["artifact"], "revision": schema.MAX_DEMO_REVISION + 1}},
        {"artifact": {**_raw_demo()["artifact"], "revision": "abc"}},
        {"artifact": {**_raw_demo()["artifact"], "revision": True}},
        {"artifact": {**_raw_demo()["artifact"], "kind": "reading_excerpt"}},
        {"artifact": {**_raw_demo()["artifact"], "title": ""}},
        {"artifact": {**_raw_demo()["artifact"], "title": "题" * 121}},
        {"artifact": {**_raw_demo()["artifact"], "data": ["not", "dict"]}},
        {"artifact": {**_raw_demo()["artifact"], "data": {"blob": "x" * (201 * 1024)}}},
        {"artifact": {**_raw_demo()["artifact"],
                      "preview": {"png": "data:image/jpeg;base64,AAAA"}}},
        {"artifact": {**_raw_demo()["artifact"],
                      "preview": {"png": "data:image/png;base64,不是base64"}}},
        {"artifact": {**_raw_demo()["artifact"], "preview": "not-a-dict"}},
        {"artifact": {**_raw_demo()["artifact"], "preview": {"svg": 123}}},
        {"artifact": {**_raw_demo()["artifact"], "preview": {"svg": "x" * (61 * 1024)}}},
        {"source_note": "说" * 301},
        {"assumptions": ["假设"] * (schema.MAX_DEMO_ASSUMPTIONS + 1)},
        {"assumptions": ["假设" * 200]},
        {"step_count": schema.MAX_DEMO_STEP_COUNT + 1},
        {"step_count": -1},
        {"step_count": "abc"},
    ]
    for overrides in bad_cases:
        raw = _raw_demo()
        raw.update(overrides)
        with pytest.raises(schema.ClassroomSchemaError):
            schema.normalize_demo_content(raw)

    # 部分字段合法但合并后整段 content 超过 260KB → 仍拒绝。
    big_png = (
        "data:image/png;base64,"
        + base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"0" * (190 * 1024)).decode("ascii")
    )
    oversized = _raw_demo()
    oversized["artifact"] = dict(oversized["artifact"])
    oversized["artifact"]["data"] = {"blob": "x" * (190 * 1024)}
    oversized["artifact"]["preview"] = {"png": big_png}
    with pytest.raises(schema.ClassroomSchemaError) as error:
        schema.normalize_demo_content(oversized)
    assert "260KB" in str(error.value)
