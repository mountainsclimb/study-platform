"""专注计时（番茄钟）会话与统计的接口测试。"""
from datetime import datetime, timedelta, timezone


def _local_date(days_ago: int = 0) -> str:
    return (datetime.now().astimezone() - timedelta(days=days_ago)).date().isoformat()


def _payload(**overrides):
    now = datetime.now(timezone.utc)
    started = int((now - timedelta(minutes=26)).timestamp() * 1000)
    base = {
        "client_id": "test-client-0001",
        "mode": "focus",
        "planned_minutes": 25,
        "elapsed_seconds": 1500,
        "status": "completed",
        "task_label": "导数专题",
        "local_date": _local_date(),
        "started_ms": started,
        "ended_ms": started + 25 * 60 * 1000,
    }
    base.update(overrides)
    return base


def test_focus_session_roundtrip(client):
    create = client.post("/api/focus/sessions", json=_payload())
    assert create.status_code == 200, create.text
    body = create.json()
    assert body["created"] is True
    item = body["item"]
    assert item["mode"] == "focus"
    assert item["task_label"] == "导数专题"
    assert item["local_date"] == _local_date()

    # 同一个 client_id 重复提交（pagehide 兜底、网络重试）不重复计数。
    again = client.post("/api/focus/sessions", json=_payload())
    assert again.status_code == 200
    assert again.json()["created"] is False
    assert again.json()["item"]["id"] == item["id"]

    listing = client.get("/api/focus/sessions?days=7").json()
    assert listing["total"] == 1
    assert listing["items"][0]["id"] == item["id"]

    summary = client.get(f"/api/focus/summary?days=7&today={_local_date()}").json()
    assert summary["today_minutes"] == 25
    assert summary["week_minutes"] == 25
    assert summary["total_minutes"] == 25
    assert summary["total_sessions"] == 1
    assert summary["streak_days"] == 1
    assert len(summary["by_day"]) == 7
    assert summary["last_session"]["id"] == item["id"]

    removed = client.delete(f"/api/focus/sessions/{item['id']}")
    assert removed.status_code == 200
    assert client.get("/api/focus/summary?days=7").json()["total_sessions"] == 0


def test_focus_summary_is_zero_when_empty(client):
    summary = client.get("/api/focus/summary?days=7").json()
    assert summary["today_minutes"] == 0
    assert summary["week_minutes"] == 0
    assert summary["streak_days"] == 0
    assert summary["last_session"] is None
    assert all(item["minutes"] == 0 for item in summary["by_day"])


def test_focus_breaks_do_not_count_as_focus_time(client):
    client.post("/api/focus/sessions", json=_payload(client_id="break-client-01", mode="short_break",
                                                    planned_minutes=5, elapsed_seconds=300))
    summary = client.get("/api/focus/summary?days=7").json()
    assert summary["today_minutes"] == 0
    assert summary["total_sessions"] == 0


def test_focus_streak_counts_consecutive_days(client):
    # 历史日期不能通过接口回填（接口只接受今天/昨天），这里直接写库构造连续三天记录。
    from app import db

    me = client.get("/api/auth/me").json()
    user_id = (me.get("user") or me)["id"]
    now = db.utcnow()
    with db.connection() as con:
        for offset in range(3):
            con.execute(
                "INSERT INTO focus_sessions "
                "(user_id, client_id, mode, planned_minutes, elapsed_seconds, status, task_label, "
                " local_date, started_at, ended_at, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (user_id, f"seed-client-0{offset}", "focus", 25, 1500, "completed", "",
                 _local_date(offset), now, now, now, now),
            )
    summary = client.get(f"/api/focus/summary?days=7&today={_local_date()}").json()
    assert summary["streak_days"] == 3
    assert summary["streak_includes_today"] is True
    assert summary["week_minutes"] == 75


def test_focus_rejects_bad_payloads(client):
    assert client.post("/api/focus/sessions", json=_payload(status="done")).status_code == 422
    assert client.post("/api/focus/sessions", json=_payload(mode="nap")).status_code == 422
    assert client.post("/api/focus/sessions", json=_payload(planned_minutes=0)).status_code == 422
    assert client.post("/api/focus/sessions", json=_payload(elapsed_seconds=99999)).status_code == 422
    assert client.post("/api/focus/sessions", json=_payload(client_id="bad id!")).status_code == 422
    assert client.post("/api/focus/sessions", json=_payload(local_date="2020-01-01")).status_code == 422


def test_focus_sessions_are_isolated_per_user(client, second_client):
    created = client.post("/api/focus/sessions", json=_payload()).json()["item"]
    other_list = second_client.get("/api/focus/sessions?days=7").json()
    assert other_list["total"] == 0
    assert second_client.delete(f"/api/focus/sessions/{created['id']}").status_code == 404
    assert second_client.get("/api/focus/summary?days=7").json()["total_sessions"] == 0
    # 原账号的数据不受影响。
    assert client.get("/api/focus/summary?days=7").json()["total_sessions"] == 1
