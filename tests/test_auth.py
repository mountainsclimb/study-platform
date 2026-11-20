from fastapi.testclient import TestClient


def test_first_account_is_admin_and_second_is_student(client, second_client):
    assert client.get("/api/auth/me").json()["role"] == "admin"
    assert second_client.get("/api/auth/me").json()["role"] == "student"
    assert second_client.get("/api/admin/overview").status_code == 403
    assert client.get("/api/admin/overview").status_code == 200


def test_passwords_and_session_tokens_are_not_stored_in_plaintext(client):
    from app.db import connection
    with connection() as con:
        user = con.execute("SELECT * FROM users").fetchone()
        session = con.execute("SELECT * FROM sessions").fetchone()
    assert "test-password" not in user["password_hash"]
    assert user["password_hash"].startswith("scrypt$")
    assert session["token_hash"] != client.cookies.get("study_session")
    me = client.get("/api/auth/me")
    assert "password" not in me.text
    assert me.headers["cache-control"] == "no-store"


def test_logout_invalidates_session_and_old_cookie(client):
    cookie = client.cookies.get("study_session")
    assert client.post("/api/auth/logout").status_code == 200
    assert client.get("/api/auth/me").status_code == 401
    client.cookies.set("study_session", cookie)
    assert client.get("/api/auth/me").status_code == 401


def test_login_and_duplicate_username(client):
    client.post("/api/auth/logout")
    assert client.post("/api/auth/login", json={"username": "测试管理员", "password": "wrong-password"}).status_code == 401
    result = client.post("/api/auth/login", json={"username": "测试管理员", "password": "test-password-123"})
    assert result.status_code == 200
    assert client.post("/api/auth/register", json={"username": "测试管理员", "password": "new-password-123"}).status_code == 409


def test_cross_site_mutations_rejected(client):
    response = client.post("/api/auth/logout", headers={"origin": "https://foreign.example"})
    assert response.status_code == 403
    assert client.get("/api/auth/me").status_code == 200


def test_unauthenticated_requests_are_rejected(client):
    from app.main import app
    with TestClient(app) as anonymous:
        for path in ("/api/questions", "/api/wrongbook", "/api/providers", "/api/notes", "/api/documents"):
            assert anonymous.get(path).status_code == 401, path


def test_health_and_same_origin_headers(client):
    response = client.get("/api/health")
    assert response.json()["status"] == "ok"
    assert response.json()["bank_available"] is True
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "object-src 'none'" in response.headers["content-security-policy"]


def test_unknown_host_is_rejected(client):
    assert client.get("/api/health", headers={"host": "rebind.attacker.example"}).status_code == 400


def test_login_rate_limit_normalizes_whitespace(client):
    from app.auth import _failures
    _failures.clear()
    for index in range(10):
        result = client.post("/api/auth/login", json={
            "username": " " * index + "测试管理员", "password": "wrong-password-123"
        })
        assert result.status_code == 401
    assert client.post("/api/auth/login", json={
        "username": " 测试管理员 ", "password": "wrong-password-123"
    }).status_code == 429
    _failures.clear()


def test_login_limit_is_shared_across_web_processes(client):
    from app import auth
    assert client.post("/api/auth/logout").status_code == 200
    for _ in range(10):
        result = client.post("/api/auth/login", json={
            "username": "测试管理员", "password": "wrong-password-123",
        })
        assert result.status_code == 401
    # 清空进程内桶后仍然封禁：限流状态保存在数据库，两个 Web 进程共享同一窗口。
    auth._failures.clear()
    auth._ip_failures.clear()
    assert client.post("/api/auth/login", json={
        "username": "测试管理员", "password": "test-password-123",
    }).status_code == 429
    # 其他账号不受影响（同一 IP 计数尚未达到 IP 上限）
    assert client.post("/api/auth/login", json={
        "username": "另一个账号", "password": "wrong-password-123",
    }).status_code == 401
