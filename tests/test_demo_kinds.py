"""演示附件类型验收（批次五起；覆盖 molecule_3d / physics_sandbox）。

覆盖：
1. `POST /api/artifacts` 接受 `molecule_3d`，data/preview 往返一致；
2. 插入课堂固定版本演示场景后可读回附件快照；
3. `GET /api/classroom/meta` 的 demo_artifact_kinds 包含 molecule_3d；
4. `physics_sandbox` 附件往返、课堂固定版本与离线仅海报导出；
5. 课堂插入端的结构校验：刚体/约束上限、形状白名单、数值与约束索引非法时 422。
"""
from __future__ import annotations

from app.classroom import schema


TINY_PNG = (
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)

MOLECULE_DATA = {"smiles": "O", "label": "水分子", "style": "stick"}
PHYSICS_DATA = {
    "gravity": {"x": 0, "y": 1},
    "bodies": [
        {"id": "anchor", "shape": "rect", "x": 320, "y": 42, "width": 16, "height": 16, "static": True},
        {"id": "bob", "shape": "circle", "x": 320, "y": 190, "radius": 24, "restitution": 0.15},
    ],
    "constraints": [{"bodyA": 0, "bodyB": 1, "stiffness": 0.02, "length": 120, "damping": 0.02}],
}


def create_course(client):
    response = client.post("/api/classroom/courses", json={
        "topic": "分子结构", "subject": "化学", "grade": "高中",
    })
    assert response.status_code == 200, response.text
    return response.json()["course"]


def test_molecule_artifact_roundtrip_and_demo_scene(client):
    response = client.post("/api/artifacts", json={
        "kind": "molecule_3d",
        "title": "水分子",
        "data": MOLECULE_DATA,
        "preview": {"kind": "molecule-3d", "smiles": "O", "style": "stick", "png": TINY_PNG},
    })
    assert response.status_code == 201, response.text
    body = response.json()
    artifact = body["artifact"]
    artifact_id = artifact["artifact_id"]
    assert artifact["kind"] == "molecule_3d"
    assert body["revision"]["data"] == MOLECULE_DATA
    assert body["revision"]["preview"]["smiles"] == "O"

    detail = client.get(f"/api/artifacts/{artifact_id}")
    assert detail.status_code == 200, detail.text
    assert detail.json()["revision"]["data"] == MOLECULE_DATA

    course = create_course(client)
    attached = client.post(f"/api/classroom/courses/{course['id']}/scenes/demo", json={
        "artifact_id": artifact_id,
    })
    assert attached.status_code == 201, attached.text
    scenes = client.get(f"/api/classroom/courses/{course['id']}").json()["scenes"]
    demo = next(scene for scene in scenes if scene["scene_type"] == "demo")
    assert demo["content"]["artifact"]["kind"] == "molecule_3d"
    assert demo["content"]["artifact"]["data"] == MOLECULE_DATA

    html = client.get(
        f"/api/classroom/courses/{course['id']}/export", params={"format": "html"},
    )
    assert html.status_code == 200, html.text
    page = html.text
    assert 'data-offline-demo="molecule_3d"' in page
    assert 'data-offline-demo-mounted="1"' not in page
    assert "分子模型" in page
    assert "data:image/png;base64," in page


def test_meta_declares_molecule_demo_kind(client):
    meta = client.get("/api/classroom/meta").json()
    assert "molecule_3d" in schema.DEMO_ARTIFACT_KINDS
    assert meta["demo_artifact_kinds"]["demo"] == list(schema.DEMO_ARTIFACT_KINDS)
    assert schema.DEMO_ARTIFACT_KIND_LABELS["molecule_3d"] == "分子模型"


def _sandbox_artifact(client, data):
    """创建 physics_sandbox 附件（附件端只限字节，结构校验在课堂插入时）。"""
    response = client.post("/api/artifacts", json={
        "kind": "physics_sandbox",
        "title": "结构校验沙盒",
        "data": data,
        "preview": {"kind": "physics-sandbox", **data, "png": TINY_PNG},
    })
    assert response.status_code == 201, response.text
    return response.json()["artifact"]["artifact_id"]


def test_physics_sandbox_demo_scene_rejects_invalid_structure(client):
    course = create_course(client)

    def attach(data):
        return client.post(f"/api/classroom/courses/{course['id']}/scenes/demo", json={
            "artifact_id": _sandbox_artifact(client, data),
        })

    # 刚体超过 60 上限。
    overflow = {
        "gravity": {"x": 0, "y": 1},
        "bodies": [dict(PHYSICS_DATA["bodies"][1], id=f"b{index}") for index in range(61)],
        "constraints": [],
    }
    response = attach(overflow)
    assert response.status_code == 422, response.text
    assert "60" in response.text

    # 形状不在白名单。
    bad_shape = {
        "gravity": {"x": 0, "y": 1},
        "bodies": [dict(PHYSICS_DATA["bodies"][0], shape="triangle")],
        "constraints": [],
    }
    response = attach(bad_shape)
    assert response.status_code == 422, response.text
    assert "rect/circle/slope" in response.text

    # 数值字段非法。
    bad_number = {
        "gravity": {"x": 0, "y": 1},
        "bodies": [dict(PHYSICS_DATA["bodies"][0], x="abc")],
        "constraints": [],
    }
    response = attach(bad_number)
    assert response.status_code == 422, response.text
    assert "有限数值" in response.text

    # 约束引用越界。
    bad_reference = {
        "gravity": {"x": 0, "y": 1},
        "bodies": [dict(PHYSICS_DATA["bodies"][1])],
        "constraints": [{"bodyA": 0, "bodyB": 3, "stiffness": 0.02, "length": 80}],
    }
    response = attach(bad_reference)
    assert response.status_code == 422, response.text
    assert "超出刚体索引范围" in response.text


def test_physics_sandbox_artifact_and_demo_scene(client):
    response = client.post("/api/artifacts", json={
        "kind": "physics_sandbox",
        "title": "弹簧振子",
        "data": PHYSICS_DATA,
        "preview": {"kind": "physics-sandbox", **PHYSICS_DATA, "png": TINY_PNG},
    })
    assert response.status_code == 201, response.text
    body = response.json()
    artifact_id = body["artifact"]["artifact_id"]
    assert body["artifact"]["kind"] == "physics_sandbox"
    assert body["revision"]["data"] == PHYSICS_DATA

    course = create_course(client)
    attached = client.post(f"/api/classroom/courses/{course['id']}/scenes/demo", json={
        "artifact_id": artifact_id,
    })
    assert attached.status_code == 201, attached.text
    scenes = client.get(f"/api/classroom/courses/{course['id']}").json()["scenes"]
    demo = next(scene for scene in scenes if scene["scene_type"] == "demo")
    assert demo["content"]["artifact"]["kind"] == "physics_sandbox"

    html = client.get(
        f"/api/classroom/courses/{course['id']}/export", params={"format": "html"},
    )
    assert html.status_code == 200, html.text
    assert 'data-offline-demo="physics_sandbox"' in html.text
    assert 'data-offline-demo-mounted="1"' not in html.text
    assert "物理沙盒" in html.text
    assert "data:image/png;base64," in html.text
