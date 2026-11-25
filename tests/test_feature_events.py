"""学习事件（阶段 5-3）：只记成功使用、字段白名单、聚合与隐私边界。"""
from __future__ import annotations

from app import db, study_evidence


def _post(client, **payload):
    body = {"feature": "demo", "action": "success", **payload}
    return client.post("/api/evidence/feature-event", json=body)


def test_feature_event_records_only_success_and_is_separate_from_evidence(client):
    response = _post(client, source="geometry_demo", duration_ms=1200, subject="物理")
    assert response.status_code == 200, response.text
    event = response.json()["event"]
    assert event["feature"] == "demo"
    assert event["source"] == "geometry_demo"
    assert event["duration_ms"] == 1200
    with db.connection() as con:
        assert con.execute("SELECT COUNT(*) FROM feature_events").fetchone()[0] == 1
        assert con.execute("SELECT COUNT(*) FROM study_evidence").fetchone()[0] == 0
    summary = client.get("/api/evidence/summary").json()
    assert summary["effective_count"] == 0


def test_feature_event_rejects_invalid_feature_action_and_content(client):
    assert _post(client, feature="chat").status_code == 422
    assert _post(client, action="open").status_code == 422
    assert _post(client, source="bad source!").status_code == 422
    assert _post(client, duration_ms=-1).status_code == 422
    assert _post(client, duration_ms=3_600_001).status_code == 422
    # 额外字段（可能夹带朗读文本/公式/音频）直接拒绝，不静默落库。
    assert _post(client, text="secret 朗读正文").status_code == 422
    with db.connection() as con:
        assert con.execute("SELECT COUNT(*) FROM feature_events").fetchone()[0] == 0


def test_feature_usage_aggregates_by_feature_and_isolates_users(client, second_client):
    assert _post(client, feature="demo", source="timeline", duration_ms=500).status_code == 200
    assert _post(client, feature="demo", source="geometry_demo", duration_ms=700).status_code == 200
    assert _post(client, feature="formula", source="chem_balance", duration_ms=300).status_code == 200
    assert _post(client, feature="speech", source="read_aloud", duration_ms=200).status_code == 200

    body = client.get("/api/evidence/feature-usage?days=90").json()
    items = {item["feature"]: item for item in body["items"]}
    assert set(items) == {"demo", "formula", "speech"}
    assert body["total"] == 4
    assert items["demo"]["count"] == 2
    assert items["demo"]["total_duration_ms"] == 1200
    assert items["formula"]["count"] == 1
    assert items["speech"]["count"] == 1
    assert "不记录" in body["note"]

    empty = second_client.get("/api/evidence/feature-usage?days=90").json()
    assert empty["total"] == 0
    assert {item["count"] for item in empty["items"]} == {0}


def test_insight_report_includes_feature_usage(client):
    assert _post(client, feature="speech", source="shadowing", duration_ms=900).status_code == 200
    report = client.get("/api/insight/report").json()
    usage = report["feature_usage"]
    assert usage["total"] == 1
    speech = next(item for item in usage["items"] if item["feature"] == "speech")
    assert speech["count"] == 1 and speech["total_duration_ms"] == 900


def test_feature_table_has_no_content_columns(client):
    with db.connection() as con:
        columns = {row[1] for row in con.execute("PRAGMA table_info(feature_events)")}
    assert columns == {
        "id", "user_id", "feature", "action", "source", "subject",
        "duration_ms", "occurred_at", "created_at",
    }
    assert "text" not in columns and "content" not in columns and "audio" not in columns


def test_record_feature_event_validates_at_module_level(isolated_db, client):
    study_evidence.init_study_evidence()
    with db.connection() as con:
        assert "feature_events" in db.table_names(con)
    event = study_evidence.record_feature_event(
        1, feature="formula", source="formula_editor", duration_ms=10,
    )
    assert event["feature"] == "formula"
    usage = study_evidence.feature_usage(1, days=90)
    assert usage["total"] == 1
