"""P0 账号安全回归：管理员初始化原子限流、恢复码生成需当前密码二次校验。"""
import threading

from fastapi.testclient import TestClient
import pytest


def _init_admin_body(token="wrong-token-2026", username="初始管理员"):
    return {"username": username, "password": "admin-pass-123", "token": token}


@pytest.fixture
def anon(isolated_db, monkeypatch):
    monkeypatch.setenv("STUDY_ADMIN_INIT_TOKEN", "operator-token-2026")
    from app.main import app

    with TestClient(app) as value:
        yield value


def test_init_admin_rate_limit_is_atomic(anon):
    """串行 10 次错误口令后第 11 次必须 429（检查与占用在同一写事务内）。"""
    for _ in range(10):
        assert anon.post("/api/auth/init-admin", json=_init_admin_body()).status_code == 403
    limited = anon.post("/api/auth/init-admin", json=_init_admin_body())
    assert limited.status_code == 429, limited.text


def test_concurrent_rate_reserve_cannot_exceed_limit(isolated_db):
    """并发预约不能突破 10 次上限（旧 _rate_guard+_rate_record 会进入 12 次核验）。"""
    import threading

    from fastapi import HTTPException

    from app import auth, db

    db.init_db()
    key = auth._rate_key("init-admin", "127.0.0.1")
    results = []
    lock = threading.Lock()

    def attempt():
        status = "reserved"
        try:
            auth._rate_reserve("reset", key, 10)
        except HTTPException as exc:
            status = exc.status_code
        with lock:
            results.append(status)

    threads = [threading.Thread(target=attempt) for _ in range(14)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert results.count("reserved") == 10, results
    assert results.count(429) == 4, results


def test_recovery_codes_require_current_password(client):
    # 缺少当前密码 / 密码错误都不能导出恢复码。
    assert client.post("/api/auth/recovery-codes").status_code == 422
    wrong = client.post("/api/auth/recovery-codes", json={"current_password": "wrong-password-999"})
    assert wrong.status_code == 401
    okay = client.post("/api/auth/recovery-codes", json={"current_password": "test-password-123"})
    assert okay.status_code == 200
    assert len(okay.json()["codes"]) == 8
