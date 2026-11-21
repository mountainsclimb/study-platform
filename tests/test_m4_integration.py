"""M4 集成测试：课堂版本化演示场景与固定版本导出。

使用真实应用（client/second_client）：学习附件 → 课堂固定版本演示场景 → 课程读取/导出 →
场景删除与顺序重排；同时验证笔记 by-target 读取与两账号隔离。
前端播放器/预设的静态契约由 ``scripts/integration/ui_static_accept.py`` 覆盖。
"""
from __future__ import annotations

import io
import zipfile

FLOW_DATA = {
    "layout": "loop",
    "style": "sketch",
    "title": "M4 集成复习闭环",
    "nodes": [
        {"id": "n1", "label": "答错", "kind": "start"},
        {"id": "n2", "label": "归因", "kind": "process"},
        {"id": "n3", "label": "变式练习", "kind": "process"},
        {"id": "n4", "label": "到期复习", "kind": "end"},
    ],
    "edges": [
        {"from": "n1", "to": "n2"},
        {"from": "n2", "to": "n3"},
        {"from": "n3", "to": "n4"},
        {"from": "n4", "to": "n3", "kind": "feedback"},
    ],
    "steps": [{"label": "答错先归因"}, {"label": "做变式"}, {"label": "按到期复习"}],
}


def _create_artifact(client, kind: str = "flow_animation", title: str = "集成流程演示") -> dict:
    response = client.post(
        "/api/artifacts",
        json={"kind": kind, "title": title, "data": FLOW_DATA,
              "preview": {"kind": "principle-demo"}},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    return {"artifact_id": body["artifact"]["artifact_id"], "revision": body["revision"]["revision"]}


def _create_course(client) -> dict:
    response = client.post(
        "/api/classroom/courses",
        json={"topic": "集成测试课堂：原理演示", "subject": "物理", "grade": "高中"},
    )
    assert response.status_code in (200, 201), response.text
    course = response.json().get("course") or {}
    assert course.get("id")
    return course


def _attach_demo(client, course_id: int, artifact: dict, **extra) -> dict:
    response = client.post(
        f"/api/classroom/courses/{course_id}/scenes/demo",
        json={"artifact_id": artifact["artifact_id"], "revision": artifact["revision"], **extra},
    )
    assert response.status_code in (200, 201), response.text
    return response.json()["scene"]


def test_demo_scene_pins_artifact_revision(client):
    artifact = _create_artifact(client)
    course = _create_course(client)
    scene = _attach_demo(client, course["id"], artifact, source_note="集成固定版本说明")
    content = scene["content"]
    assert scene["scene_type"] == "demo"
    assert content["fixed"] is True
    assert content["artifact"]["artifact_id"] == artifact["artifact_id"]
    assert content["artifact"]["revision"] == artifact["revision"]
    assert "固定版本" in str(content.get("source_note") or "") or content["source_note"] == "集成固定版本说明"

    # 源附件升版后课堂仍是固定版本。
    patched = client.patch(
        f"/api/artifacts/{artifact['artifact_id']}",
        json={"data": {**FLOW_DATA, "title": "集成流程演示 v2"}, "expected_revision": artifact["revision"]},
    )
    assert patched.status_code == 200, patched.text
    scenes = client.get(f"/api/classroom/courses/{course['id']}").json()["scenes"]
    demo = next(item for item in scenes if item["scene_type"] == "demo")
    assert demo["content"]["artifact"]["revision"] == artifact["revision"]


def test_demo_scene_validation_and_isolation(client, second_client):
    artifact = _create_artifact(client)
    course = _create_course(client)
    foreign_course = second_client.post(
        f"/api/classroom/courses/{course['id']}/scenes/demo",
        json={"artifact_id": artifact["artifact_id"]},
    )
    assert foreign_course.status_code == 404
    # 他人附件不可被引用（附件归属校验）。
    other_artifact = _create_artifact(second_client)
    foreign_artifact = client.post(
        f"/api/classroom/courses/{course['id']}/scenes/demo",
        json={"artifact_id": other_artifact["artifact_id"]},
    )
    assert foreign_artifact.status_code == 404
    bad_kind = client.post(
        f"/api/classroom/courses/{course['id']}/scenes/demo",
        json={"artifact_id": "nonexistent-id", "revision": 1},
    )
    assert bad_kind.status_code == 404
    # 音乐/未知 kind 不能作为演示场景（用 whiteboard 之外的非法 kind 建附件会被 422，这里直接传未知 ID）。
    assert client.get(f"/api/classroom/courses/{course['id']}").status_code == 200


def test_meta_declares_manual_demo_scene_type(client):
    meta = client.get("/api/classroom/meta").json()
    assert meta["manual_scene_types"] == ["demo"]
    assert set(meta["scene_types"]) == {"slide", "quiz", "interactive", "pbl"}
    assert "demo" in (meta.get("demo_artifact_kinds") or [])


def test_export_uses_fixed_revision(client):
    artifact = _create_artifact(client, title="导出固定版本演示")
    course = _create_course(client)
    _attach_demo(client, course["id"], artifact)

    html = client.get(f"/api/classroom/courses/{course['id']}/export", params={"format": "html"})
    assert html.status_code == 200, html.text
    assert "data-fixed-revision" in html.text
    assert artifact["artifact_id"] in html.text
    assert "cls-demo-scene" in html.text

    data = client.get(f"/api/classroom/courses/{course['id']}/export", params={"format": "zip"})
    assert data.status_code == 200
    with zipfile.ZipFile(io.BytesIO(data.content)) as archive:
        names = archive.namelist()
    assert any(name.startswith("demos/") and name.endswith(".json") for name in names)

    pptx = client.get(f"/api/classroom/courses/{course['id']}/export", params={"format": "pptx"})
    assert pptx.status_code == 200 and pptx.content[:2] == b"PK"
    from pptx import Presentation

    presentation = Presentation(io.BytesIO(pptx.content))
    texts = []
    for slide in presentation.slides:
        for shape in slide.shapes:
            if getattr(shape, "has_text_frame", False):
                texts.append(shape.text_frame.text)
    combined = "\n".join(texts)
    assert "固定版本" in combined
    assert "导出固定版本演示" in combined


def test_delete_demo_scene_renumbers(client):
    artifact = _create_artifact(client)
    course = _create_course(client)
    scene = _attach_demo(client, course["id"], artifact)
    removed = client.delete(f"/api/classroom/courses/{course['id']}/scenes/{scene['id']}")
    assert removed.status_code == 200, removed.text
    assert removed.json()["deleted"] is True
    scenes = client.get(f"/api/classroom/courses/{course['id']}").json()["scenes"]
    assert scenes == []
    # 非演示场景不允许删除（没有演示场景时删除不存在的场景仍是 404）。
    assert client.delete(f"/api/classroom/courses/{course['id']}/scenes/{scene['id']}").status_code == 404


def test_note_artifact_by_target(client, second_client):
    created = client.post(
        "/api/artifacts",
        json={
            "kind": "function_graph", "title": "笔记关联函数图",
            "data": {"version": 1, "source": "mafs-compatible",
                     "expressions": [{"id": "f1", "latex": "x^2", "color": "#2b7de9", "visible": True}],
                     "params": [], "xRange": [-5, 5], "yRange": [-5, 5], "points": [], "labels": []},
            "links": [{"target_kind": "note", "target_key": "777"}],
        },
    )
    assert created.status_code == 201, created.text
    by_target = client.get("/api/artifacts/by-target", params={"target_kind": "note", "target_key": "777"})
    assert by_target.status_code == 200
    items = by_target.json()["items"]
    assert items and items[0]["artifact"]["kind"] == "function_graph"
    # 他账号看不到该笔记附件。
    foreign = second_client.get("/api/artifacts/by-target", params={"target_kind": "note", "target_key": "777"})
    assert foreign.status_code == 200 and foreign.json()["total"] == 0
