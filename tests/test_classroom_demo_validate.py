"""课堂演示参数 schema 与归一化校验（阶段 3-4）。"""
from fastapi.testclient import TestClient

from app.main import app


def _content(kind="flow_animation", data=None):
    return {
        "artifact": {
            "artifact_id": "abcd1234efgh",
            "revision": 1,
            "kind": kind,
            "title": "流程演示",
            "data": data if data is not None else {"steps": [{"label": "第一步"}]},
            "preview": {"kind": "principle-demo"},
        },
        "source_note": "课堂说明",
        "assumptions": ["假设一"],
        "step_count": 1,
        "fixed": True,
    }


def test_demo_schema_lists_kinds_and_limits(client):
    response = client.get("/api/classroom/demo/schema")
    assert response.status_code == 200, response.text
    payload = response.json()
    kinds = {item["kind"] for item in payload["kinds"]}
    assert {"geometry_demo", "timeline", "molecule_3d", "physics_sandbox"} <= kinds
    assert payload["limits"]["data_bytes"] > 0
    assert "不接受任意 HTML" in payload["note"]


def test_validate_demo_accepts_and_normalizes(client):
    response = client.post("/api/classroom/demo/validate", json={"content": _content()})
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["ok"] is True
    assert payload["content"]["type"] == "demo"
    assert payload["content"]["fixed"] is True
    assert payload["content"]["artifact"]["artifact_id"] == "abcd1234efgh"
    assert payload["warnings"]


def test_validate_demo_rejects_unknown_kind(client):
    response = client.post(
        "/api/classroom/demo/validate",
        json={"content": _content(kind="raw_html")},
    )
    assert response.status_code == 422
    assert "演示附件类型仅支持" in response.json()["detail"]


def test_validate_demo_rejects_bad_timeline_dates(client):
    content = _content(
        kind="timeline",
        data={"preset_id": "modern-china-1840-1949", "events": [
            {"title": "事件", "start": "2020-13-01"},
        ]},
    )
    response = client.post("/api/classroom/demo/validate", json={"content": content})
    assert response.status_code == 422
    assert "开始日期不合法" in response.json()["detail"]


def test_validate_demo_requires_login():
    anonymous = TestClient(app)
    response = anonymous.post(
        "/api/classroom/demo/validate", json={"content": _content()},
    )
    assert response.status_code == 401
