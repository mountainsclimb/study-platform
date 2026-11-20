"""P2 存活与就绪：依赖逐个核对，并定位受影响功能。"""
from datetime import datetime, timedelta, timezone

from app import db


def test_liveness_and_ready_baseline(client):
    live = client.get("/api/health/live")
    assert live.status_code == 200
    assert live.json()["status"] == "ok"

    ready = client.get("/api/health/ready")
    assert ready.status_code == 200
    body = ready.json()
    assert body["status"] == "ok", body
    assert body["checks"]["app_db"]["ok"] is True
    assert body["checks"]["bank"]["ok"] is True
    assert body["checks"]["bank"]["questions"] > 0
    assert body["checks"]["storage"]["ok"] is True
    # 本地模式没有配置 Redis/Worker 时不判定为降级
    assert body["checks"]["redis"]["configured"] is False
    assert body["checks"]["worker"]["configured"] is False
    assert body["degraded"] == []


def test_missing_bank_degrades_question_features(client, monkeypatch, tmp_path):
    monkeypatch.setattr(db, "BANK_DIR", tmp_path / "missing-bank")
    body = client.get("/api/health/ready").json()
    assert body["status"] == "degraded"
    assert "bank" in body["degraded"]
    assert any("题目" in feature or "题库" in feature for feature in body["affected_features"])
    assert client.get("/api/health").json()["bank_available"] is False


def test_redis_and_worker_checks_point_to_ai_jobs(client, monkeypatch):
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:1/0")
    body = client.get("/api/health/ready").json()
    assert body["checks"]["redis"]["configured"] is True
    assert body["checks"]["redis"]["ok"] is False
    assert body["checks"]["worker"]["ok"] is False
    assert "redis" in body["degraded"] and "worker" in body["degraded"]
    assert any("AI 任务" in feature for feature in body["affected_features"])

    # 写入最近心跳后 Worker 恢复就绪
    with db.connection() as con:
        con.execute(
            "INSERT INTO service_heartbeat(name,beat_at,payload_json) VALUES ('worker',?,NULL) "
            "ON CONFLICT(name) DO UPDATE SET beat_at=excluded.beat_at",
            (db.utcnow(),),
        )
    recovered = client.get("/api/health/ready").json()
    assert recovered["checks"]["worker"]["ok"] is True
    assert recovered["checks"]["redis"]["ok"] is False  # Redis 仍不可用，整体保持降级
    assert recovered["status"] == "degraded"


def test_stale_worker_heartbeat_is_reported(client, monkeypatch):
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:1/0")
    stale = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    with db.connection() as con:
        con.execute(
            "INSERT INTO service_heartbeat(name,beat_at,payload_json) VALUES ('worker',?,NULL) "
            "ON CONFLICT(name) DO UPDATE SET beat_at=excluded.beat_at",
            (stale,),
        )
    body = client.get("/api/health/ready").json()
    worker = body["checks"]["worker"]
    assert worker["configured"] is True
    assert worker["ok"] is False
    assert worker["age_seconds"] >= 500


def test_expected_bank_version_mismatch_degrades(client, monkeypatch):
    """STUDY_EXPECTED_BANK_VERSION 与实读版本不一致时降级，避免新旧题库混用。"""
    actual = client.get("/api/health/ready").json()["checks"]["bank"]["bank_version"]
    monkeypatch.setenv("STUDY_EXPECTED_BANK_VERSION", "bank-does-not-exist-r99")
    body = client.get("/api/health/ready").json()
    bank = body["checks"]["bank"]
    assert bank["ok"] is False
    assert bank["expected_bank_version"] == "bank-does-not-exist-r99"
    assert body["status"] == "degraded"
    assert "bank" in body["degraded"]
    assert body["study_ready"] is False
    # 与实读版本一致时恢复就绪
    monkeypatch.setenv("STUDY_EXPECTED_BANK_VERSION", actual or "")
    restored = client.get("/api/health/ready").json()
    assert restored["checks"]["bank"]["ok"] is True


def test_redis_readiness_requires_protocol_ping(client, monkeypatch):
    """普通 TCP 监听器不能被判为 Redis 可用：必须完成 PING 握手。"""
    import socket
    import threading

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]
    stop = threading.Event()

    def serve():
        try:
            conn, _ = server.accept()
            stop.set()
            conn.sendall(b"-ERR unknown command\r\n")
            conn.close()
        except OSError:
            stop.set()

    threading.Thread(target=serve, daemon=True).start()
    monkeypatch.setenv("REDIS_URL", f"redis://127.0.0.1:{port}/0")
    try:
        body = client.get("/api/health/ready").json()
        assert body["checks"]["redis"]["ok"] is False
        assert "PING" in body["checks"]["redis"]["detail"] or "不可连接" in body["checks"]["redis"]["detail"]
    finally:
        stop.wait(timeout=2)
        server.close()
