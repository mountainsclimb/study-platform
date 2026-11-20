"""P2 公网账号策略：受控的一次性管理员初始化、注册开关与跨进程限流。"""
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def anon(isolated_db):
    from app.main import app
    # 公网模式下会话 Cookie 强制 Secure，因此测试客户端必须走 https（等价于 Caddy 终止 TLS）。
    with TestClient(app, base_url="https://testserver") as value:
        yield value


def test_public_mode_defaults_to_closed_registration(anon, monkeypatch):
    monkeypatch.setenv("STUDY_PUBLIC_MODE", "1")
    monkeypatch.delenv("STUDY_REGISTRATION_MODE", raising=False)
    monkeypatch.delenv("STUDY_ADMIN_INIT_TOKEN", raising=False)

    policy = anon.get("/api/auth/registration-policy").json()
    assert policy["registration_open"] is False
    assert policy["public_mode"] is True
    assert policy["admin_ready"] is False
    assert policy["admin_init_available"] is False

    blocked = anon.post("/api/auth/register", json={"username": "公网用户", "password": "public-pass-123"})
    assert blocked.status_code == 403
    assert "注册暂未开放" in blocked.json()["detail"]

    # 没有配置初始化口令时不能创建管理员
    missing_token = anon.post("/api/auth/init-admin", json={
        "username": "初始管理员", "password": "admin-pass-123", "token": "configured-token-123",
    })
    assert missing_token.status_code == 503


def test_admin_init_requires_token_and_runs_once(anon, monkeypatch):
    monkeypatch.setenv("STUDY_PUBLIC_MODE", "1")
    monkeypatch.delenv("STUDY_REGISTRATION_MODE", raising=False)
    monkeypatch.setenv("STUDY_ADMIN_INIT_TOKEN", "operator-token-2026")

    policy = anon.get("/api/auth/registration-policy").json()
    assert policy["admin_init_available"] is True

    wrong = anon.post("/api/auth/init-admin", json={
        "username": "初始管理员", "password": "admin-pass-123", "token": "wrong-token-2026",
    })
    assert wrong.status_code == 403

    created = anon.post("/api/auth/init-admin", json={
        "username": "初始管理员", "password": "admin-pass-123", "token": "operator-token-2026",
    })
    assert created.status_code == 200, created.text
    assert created.json()["role"] == "admin"
    assert anon.get("/api/auth/me").json()["role"] == "admin"

    # 只能成功一次
    again = anon.post("/api/auth/init-admin", json={
        "username": "第二个管理员", "password": "admin-pass-456", "token": "operator-token-2026",
    })
    assert again.status_code == 409

    # 公网模式下注册仍关闭，直到运营显式开放
    assert anon.post("/api/auth/register", json={"username": "公网用户", "password": "public-pass-123"}).status_code == 403
    opened = anon.post("/api/auth/admin/registration", json={"mode": "open"})
    assert opened.status_code == 200
    student = anon.post("/api/auth/register", json={"username": "公网用户", "password": "public-pass-123"})
    assert student.status_code == 200
    assert student.json()["role"] == "student"

    # 注册接口会切换到新账号会话；重新以管理员身份登录后继续调整策略
    assert anon.post("/api/auth/login", json={
        "username": "初始管理员", "password": "admin-pass-123",
    }).status_code == 200
    closed = anon.post("/api/auth/admin/registration", json={"mode": "closed"})
    assert closed.status_code == 200
    assert anon.get("/api/auth/registration-policy").json()["registration_open"] is False
    assert anon.post("/api/auth/register", json={"username": "后来者", "password": "later-pass-123"}).status_code == 403


def test_student_cannot_change_registration_policy(anon):
    assert anon.post("/api/auth/register", json={"username": "本地管理员", "password": "admin-pass-123"}).status_code == 200
    assert anon.post("/api/auth/register", json={"username": "普通学生", "password": "student-pass-123"}).status_code == 200
    assert anon.get("/api/auth/admin/registration").status_code == 403
    assert anon.post("/api/auth/admin/registration", json={"mode": "closed"}).status_code == 403


def test_rate_limit_is_shared_between_web_processes(isolated_db):
    from app.main import app
    with TestClient(app) as first, TestClient(app) as second:
        assert first.post("/api/auth/register", json={
            "username": "限流管理员", "password": "admin-pass-123",
        }).status_code == 200
        for _ in range(10):
            assert first.post("/api/auth/login", json={
                "username": "限流管理员", "password": "wrong-password-123",
            }).status_code == 401
        # 另一个 TestClient 相当于另一个 Web 进程，共享同一个限流窗口
        assert second.post("/api/auth/login", json={
            "username": "限流管理员", "password": "wrong-password-123",
        }).status_code == 429


def test_password_reset_is_rate_limited(isolated_db):
    from app.main import app
    with TestClient(app) as client:
        assert client.post("/api/auth/register", json={
            "username": "重置管理员", "password": "admin-pass-123",
        }).status_code == 200
        for _ in range(10):
            assert client.post("/api/auth/reset-password", json={
                "username": "重置管理员", "recovery_code": "ABCD-1234-EF56", "new_password": "new-pass-123",
            }).status_code == 401
        limited = client.post("/api/auth/reset-password", json={
            "username": "重置管理员", "recovery_code": "ABCD-1234-EF56", "new_password": "new-pass-123",
        })
        assert limited.status_code == 429


def test_concurrent_login_attempts_cannot_exceed_limit(isolated_db, monkeypatch):
    """并发登录不能绕过 10 次上限：检查与占用必须在同一把写锁内完成。"""
    import threading

    from app.main import app
    from fastapi.testclient import TestClient

    with TestClient(app) as client:
        assert client.post("/api/auth/register", json={
            "username": "并发限流", "password": "admin-pass-123",
        }).status_code == 200

    results = []
    lock = threading.Lock()

    def attempt():
        with TestClient(app) as worker:
            response = worker.post("/api/auth/login", json={
                "username": "并发限流", "password": "wrong-password-123",
            })
            with lock:
                results.append(response.status_code)

    threads = [threading.Thread(target=attempt) for _ in range(14)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert results.count(401) == 10, results
    assert results.count(429) == 4, results
