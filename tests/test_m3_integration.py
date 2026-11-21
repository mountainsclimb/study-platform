"""M3 集成测试（第七轮）：资料原文定位与摘录、掌握路径、图形学习附件。

使用真实应用（client/second_client fixture）：验证主应用已挂载 M3 路由、权限隔离、
版本快照与证据驱动的完成条件。逐步提示的协议细节由 ``tests/test_guided_hint.py``
覆盖（本文件只验证真实应用下的路由可用性，模块落地后补充）。
"""
from __future__ import annotations

import secrets

import pytest

DOC_TEXT = (
    "第一章 函数与导数\n"
    "1.1 函数的概念\n"
    "知序集成测试定位短语M3XYZ 用于验证原文定位。\n"
    "1.2 函数的单调性\n"
    "若函数单调递增，则导数非负。\n"
)


def _upload_document(client) -> dict:
    response = client.post(
        "/api/documents",
        files={"file": ("m3-int.txt", DOC_TEXT.encode("utf-8"), "text/plain")},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body.get("id")
    return body


def test_document_locate_and_annotation_round_trip(client, second_client):
    document = _upload_document(client)
    document_id = document["id"]

    located = client.post(
        f"/api/documents/{document_id}/locate", json={"quote": "集成测试定位短语M3XYZ"}
    )
    assert located.status_code == 200, located.text
    body = located.json()
    assert "集成测试定位短语M3XYZ" in body["snippet"]
    assert body["page"] == 1 and body["chunk_id"]
    assert body["doc_version"]
    assert body["valid"] is True

    missing = client.post(f"/api/documents/{document_id}/locate", json={"quote": "不存在的摘录M3"})
    assert missing.status_code == 422

    created = client.post(
        "/api/doc-annotations",
        json={"document_id": document_id, "quote": "集成测试定位短语M3XYZ", "note": "集成测试摘录"},
    )
    assert created.status_code == 201, created.text
    annotation = created.json()["annotation"]
    assert annotation["valid"] is True and annotation["doc_version"]

    listing = client.get("/api/doc-annotations", params={"document_id": document_id})
    assert listing.status_code == 200
    items = listing.json()["items"]
    assert any(item["annotation_id"] == annotation["annotation_id"] for item in items)

    refreshed = client.post(f"/api/doc-annotations/{annotation['annotation_id']}/refresh")
    assert refreshed.status_code == 200 and refreshed.json()["valid"] is True

    # 两账号隔离：他人资料与摘录均不可见/不可定位。
    foreign_locate = second_client.post(
        f"/api/documents/{document_id}/locate", json={"quote": "集成测试定位短语M3XYZ"}
    )
    assert foreign_locate.status_code == 404
    foreign_list = second_client.get("/api/doc-annotations", params={"document_id": document_id})
    assert foreign_list.status_code == 200 and foreign_list.json()["items"] == []


def test_mastery_path_restate_and_evidence_gate(client, second_client):
    points = client.get("/api/knowledge-points", params={"subject": "数学"})
    assert points.status_code == 200
    kp_ids = [int(item["id"]) for item in points.json()["items"]][:1]
    if not kp_ids:
        pytest.skip("应用库没有数学知识点")

    created = client.post("/api/mastery-paths", json={"kp_ids": kp_ids, "source_kind": "manual"})
    assert created.status_code in (200, 201), created.text
    body = created.json()
    path_id = body["path"]["path_id"]
    nodes = body["nodes"]
    assert len(nodes) == 2 and {node["kind"] for node in nodes} == {"practice", "concept"}

    concept = next(node for node in nodes if node["kind"] == "concept")
    practice = next(node for node in nodes if node["kind"] == "practice")
    too_short = client.post(
        f"/api/mastery-paths/{path_id}/nodes/{concept['node_id']}/restate", json={"text": "短"}
    )
    assert too_short.status_code == 422
    restated = client.post(
        f"/api/mastery-paths/{path_id}/nodes/{concept['node_id']}/restate",
        json={"text": "单调性描述自变量增大时函数值的变化方向，可用导数符号判断。"},
    )
    assert restated.status_code == 200 and restated.json()["node"]["status"] == "done"

    refreshed = client.post(f"/api/mastery-paths/{path_id}/nodes/{practice['node_id']}/refresh")
    assert refreshed.status_code == 200
    node = refreshed.json()["node"]
    # 没有「两道不同题独立答对」证据时不得自动完成。
    assert node["status"] in ("pending", "in_progress")
    assert "提示/揭晓后的作答不计" in str((node.get("evidence") or {}).get("note", ""))

    detail = client.get(f"/api/mastery-paths/{path_id}")
    assert detail.status_code == 200 and detail.json()["progress"]["nodes_total"] == 2
    assert second_client.get(f"/api/mastery-paths/{path_id}").status_code == 404
    assert client.post(f"/api/mastery-paths/{path_id}/archive").status_code == 200


def _pick_bank_ref(client) -> str | None:
    response = client.get("/api/questions", params={"subject": "数学", "page": 1, "page_size": 10})
    if response.status_code != 200:
        return None
    for item in response.json().get("items") or []:
        return item.get("ref") or item.get("qid")
    return None


def test_graph_and_board_artifacts_are_reusable_targets(client):
    ref = _pick_bank_ref(client)
    graph = {
        "version": 1,
        "source": "mafs-compatible",
        "expressions": [{"id": "f1", "latex": "a*x^2+b*x+c", "color": "#2b7de9", "visible": True}],
        "params": [{"name": "a", "value": 1, "min": -5, "max": 5, "step": 0.1}],
        "xRange": [-6, 6],
        "yRange": [-8, 8],
        "points": [],
        "labels": [],
    }
    board = {
        "version": 1,
        "source": "excalidraw-compatible",
        "appState": {"viewBackgroundColor": "#ffffff"},
        "elements": [{"id": "e1", "type": "formula", "x": 10, "y": 10, "latex": "x^2", "fontSize": 20}],
    }
    links = [{"target_kind": "question", "target_key": ref}] if ref else []
    created = client.post(
        "/api/artifacts",
        json={
            "kind": "function_graph", "title": "集成测试函数图", "data": graph,
            "preview": {"kind": "function-graph", "expressions": [{"latex": "a*x^2+b*x+c"}]},
            "source_ref": ref, "source_kind": "question", "links": links,
        },
    )
    assert created.status_code == 201, created.text
    created_board = client.post(
        "/api/artifacts",
        json={
            "kind": "whiteboard", "title": "集成测试白板", "data": board,
            "preview": {"kind": "whiteboard-image", "png": ""},
            "source_ref": ref, "source_kind": "question", "links": links,
        },
    )
    assert created_board.status_code == 201, created_board.text
    if ref:
        by_target = client.get(
            "/api/artifacts/by-target", params={"target_kind": "question", "target_key": ref}
        )
        assert by_target.status_code == 200
        kinds = {(item.get("artifact") or {}).get("kind") for item in by_target.json()["items"]}
        assert {"function_graph", "whiteboard"} <= kinds


def test_m3_routes_are_mounted():
    from app.main import app

    paths = {getattr(route, "path", "") for route in app.routes}
    required = {
        "/api/guided-hints",
        "/api/guided-hints/by-ref",
        "/api/guided-hints/{hint_id}",
        "/api/guided-hints/{hint_id}/advance",
        "/api/guided-hints/{hint_id}/attempt",
        "/api/documents/{document_id}/locate",
        "/api/doc-annotations",
        "/api/doc-annotations/{annotation_id}/refresh",
        "/api/mastery-paths",
        "/api/mastery-paths/{path_id}",
        "/api/mastery-paths/{path_id}/nodes/{node_id}/restate",
    }
    missing = sorted(required - paths)
    assert not missing, f"未挂载路由: {missing}"


def test_guided_hint_unlock_protocol(client, monkeypatch):
    """逐步提示：锁定正文不下发；未提交尝试不能解锁；提交后逐级解锁。"""
    import app.practice as practice

    fake_question = {
        "qid": "docM3:1",
        "ref": "bank:docM3:1",
        "source": "bank",
        "subject": "数学",
        "stem_plain": "已知函数 f(x)=x^2+2x，求 f(1)。",
        "solution": "第一步：代入 x=1。第二步：f(1)=1+2=3。",
        "answer_text": "3",
        "options": [],
        "practice_allowed": True,
        "quarantined": False,
        "has_solution": True,
        "knowledge_points": [{"kp_id": 1, "title": "函数定义域与值域"}],
    }
    monkeypatch.setattr(practice, "load_question_ref", lambda ref, user_id: dict(fake_question))
    created = client.post("/api/guided-hints", json={"ref": "docM3:1"})
    assert created.status_code in (200, 201), created.text
    body = created.json()
    steps = body["steps"]
    assert len(steps) >= 2
    locked = [step for step in steps if step.get("locked")]
    assert locked and all(step.get("body") in (None, "") for step in locked)
    hint_id = body["hint"]["hint_id"]

    blocked = client.post(f"/api/guided-hints/{hint_id}/advance", json={})
    assert blocked.status_code == 200 and blocked.json()["requires_attempt"] is True
    attempted = client.post(
        f"/api/guided-hints/{hint_id}/attempt", json={"step_position": 0, "text": "先代入 x=1"}
    )
    assert attempted.status_code == 200
    advanced = client.post(f"/api/guided-hints/{hint_id}/advance", json={})
    assert advanced.status_code == 200
    assert advanced.json()["unlocked"]["position"] == 1
    assert advanced.json()["unlocked"]["body"]


def test_guided_hint_isolation(client, second_client, monkeypatch):
    import app.practice as practice

    monkeypatch.setattr(
        practice, "load_question_ref",
        lambda ref, user_id: {
            "qid": "docM3:2", "ref": "bank:docM3:2", "source": "bank", "subject": "数学",
            "stem_plain": "求 1+1。", "solution": "等于 2。", "answer_text": "2",
            "options": [], "practice_allowed": True, "quarantined": False,
            "has_solution": True, "knowledge_points": [],
        },
    )
    created = client.post("/api/guided-hints", json={"ref": "docM3:2"}).json()
    hint_id = created["hint"]["hint_id"]
    assert second_client.get(f"/api/guided-hints/{hint_id}").status_code == 404
