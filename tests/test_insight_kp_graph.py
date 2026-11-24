"""知识图谱接口验收（方案 §6.6）：响应结构、共现/从属边、边数上限、降级与用户隔离。"""
from __future__ import annotations

import pytest

from app import db, insight, practice


def _user_id(client) -> int:
    response = client.get("/api/auth/me")
    assert response.status_code == 200, response.text
    return response.json()["id"]


def _bank_qids(subject: str, count: int = 4) -> list[str]:
    with practice._bank() as con:
        rows = con.execute(
            "SELECT qid FROM questions WHERE subject=? ORDER BY qid LIMIT ?", (subject, count)
        ).fetchall()
    assert len(rows) >= count, f"题库中 {subject} 题目不足"
    return [row[0] for row in rows]


def _kp_ids(subject: str, count: int = 2) -> list[int]:
    with db.connection() as con:
        rows = con.execute(
            "SELECT id FROM knowledge_points WHERE subject=? ORDER BY id LIMIT ?",
            (subject, count),
        ).fetchall()
    assert len(rows) >= count, f"知识点种子中 {subject} 数量不足"
    return [row[0] for row in rows]


def _link_kp(qid: str, kp_id: int, weight: float = 1.0) -> None:
    with db.connection() as con:
        con.execute(
            "INSERT INTO question_kp(qid,kp_id,weight,source,created_at) VALUES(?,?,?,'admin',?)",
            (qid, kp_id, weight, db.utcnow()),
        )


def _insert_attempt(user_id: int, qid: str, result: str) -> None:
    with db.connection() as con:
        con.execute(
            "INSERT INTO attempts(user_id,qid,answer_text,result,grade_source,created_at) "
            "VALUES(?,?,?,?,?,?)",
            (user_id, qid, "A", result, "reference", db.utcnow()),
        )


def _seed_graph(client) -> dict:
    uid = _user_id(client)
    math = _bank_qids("数学", 4)
    kp_a, kp_b = _kp_ids("数学", 2)
    _link_kp(math[0], kp_a)
    _link_kp(math[1], kp_a)
    _link_kp(math[0], kp_b)
    _link_kp(math[2], kp_b)
    _link_kp(math[3], kp_b)
    for qid, result in (
        (math[0], "correct"), (math[1], "incorrect"),
        (math[2], "correct"), (math[3], "correct"),
    ):
        _insert_attempt(uid, qid, result)
    return {"math": math, "kp_a": kp_a, "kp_b": kp_b}


def test_kp_graph_empty_and_unknown_subject(client):
    response = client.get("/api/insight/kp-graph")
    assert response.status_code == 200, response.text
    body = response.json()
    assert isinstance(body["nodes"], list)
    assert isinstance(body["edges"], list)
    assert body["subject"] is None
    if body["available"] is False:
        assert body["note"]

    unknown = client.get("/api/insight/kp-graph", params={"subject": "不存在的学科"}).json()
    assert unknown["available"] is False
    assert unknown["nodes"] == [] and unknown["edges"] == []
    assert "知识点" in unknown["note"]


def test_kp_graph_nodes_edges_and_weak_flags(client):
    seeded = _seed_graph(client)
    body = client.get("/api/insight/kp-graph", params={"subject": "数学"}).json()
    assert body["available"] is True, body
    assert body["subject"] == "数学"

    nodes = {node["kp_id"]: node for node in body["nodes"]}
    assert seeded["kp_a"] in nodes and seeded["kp_b"] in nodes
    node_a = nodes[seeded["kp_a"]]
    node_b = nodes[seeded["kp_b"]]
    for node in (node_a, node_b):
        for field in ("title", "level", "path", "attempts", "accuracy", "weak", "weight"):
            assert field in node
    assert node_a["attempts"] == 2 and node_a["weak"] is True
    assert node_a["accuracy"] == 50
    assert node_b["attempts"] == 3 and node_b["weak"] is False
    assert node_b["accuracy"] == 100

    cooccur = [
        edge for edge in body["edges"]
        if edge["type"] == "cooccur"
        and {edge["source"], edge["target"]} == {seeded["kp_a"], seeded["kp_b"]}
    ]
    assert cooccur, body["edges"]
    assert cooccur[0]["weight"] == 1
    # 从属边存在时字段结构一致（种子是否有父子关系取决于导入数据）。
    for edge in body["edges"]:
        assert edge["type"] in ("cooccur", "hierarchy")
        assert isinstance(edge["source"], int) and isinstance(edge["target"], int)


def test_kp_graph_edge_cap(client, monkeypatch):
    _seed_graph(client)
    monkeypatch.setattr(insight, "KP_GRAPH_MAX_EDGES", 1)
    body = client.get("/api/insight/kp-graph", params={"subject": "数学"}).json()
    assert len(body["edges"]) <= 1


def test_kp_graph_isolated_between_users(client, second_client):
    _seed_graph(client)
    mine = client.get("/api/insight/kp-graph", params={"subject": "数学"}).json()
    assert mine["available"] is True
    assert any(node["attempts"] > 0 for node in mine["nodes"])

    other = second_client.get("/api/insight/kp-graph", params={"subject": "数学"}).json()
    assert other["available"] is True
    assert all(node["attempts"] == 0 for node in other["nodes"])


@pytest.mark.parametrize("days", [1, 90, 400])
def test_kp_graph_days_window(client, days):
    _seed_graph(client)
    body = client.get("/api/insight/kp-graph", params={"subject": "数学", "days": days}).json()
    assert body["available"] is True
    assert body["days"] == days
