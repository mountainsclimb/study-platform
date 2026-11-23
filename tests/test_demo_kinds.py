"""演示附件类型验收（批次五起；当前覆盖 molecule_3d）。

覆盖：
1. `POST /api/artifacts` 接受 `molecule_3d`，data/preview 往返一致；
2. 插入课堂固定版本演示场景后可读回附件快照；
3. `GET /api/classroom/meta` 的 demo_artifact_kinds 包含 molecule_3d。
"""
from __future__ import annotations

from app.classroom import schema


TINY_PNG = (
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)

MOLECULE_DATA = {"smiles": "O", "label": "水分子", "style": "stick"}


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
