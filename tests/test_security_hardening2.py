"""§2.3/§2.4 安全加固回归：就绪信息脱敏、认证限流、Redis TLS/ACL、CSRF、缓存头与构建号。

对应《知序下一轮优化方案》：
* §2.3-2 Windows Zip-Slip（见 test_ops_backup_safety.py）
* §2.3-3 未鉴权就绪泄露 / 认证限流补洞 / Redis TLS+ACL / CSRF 规范化
* §2.4-2 bank-image 缓存头与 ETag
* §2.4-3 构建号补充 web/styles-pages.css 与提示词 YAML
"""
import json
import threading
import time

from fastapi import HTTPException
from fastapi.testclient import TestClient
import pytest

from app import auth, db, main
from bank_fixture import drawn_image_bytes, make_bank, question_row
import sqlite3


# ---------------------------------------------------------------------------
# 1. /api/health/ready：匿名只返回精简结构，管理员 / 探针口令才拿到完整详情
# ---------------------------------------------------------------------------

PUBLIC_READY_KEYS = {
    "status", "version", "build_id", "study_ready", "ai_ready",
    "degraded", "affected_features", "checks",
}


def test_ready_anonymous_view_hides_host_details(client, isolated_db, monkeypatch):
    from app.main import app

    with TestClient(app) as anon:
        monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:1/0")
        response = anon.get("/api/health/ready")
        assert response.status_code == 200
        body = response.json()
    assert set(body) == PUBLIC_READY_KEYS, body.keys()
    assert set(body["checks"]) == {"app_db", "bank", "storage", "redis", "worker"}
    for name, check in body["checks"].items():
        # 只暴露 ok 布尔值：不含 detail / engine / questions / bank_dir / configured
        assert set(check) == {"ok"}, (name, check)
        assert isinstance(check["ok"], bool)
    assert body["checks"]["redis"]["ok"] is False
    assert "redis" in body["degraded"]
    text = json.dumps(body, ensure_ascii=False)
    assert "bank_dir" not in text
    assert "questions" not in text
    assert str(db.BANK_DIR) not in text
    assert str(db.DATA_DIR) not in text


def test_ready_admin_full_details(client):
    body = client.get("/api/health/ready").json()
    assert body["checks"]["bank"]["questions"] > 0
    assert "bank_dir" in body["checks"]["bank"]
    assert body["checks"]["app_db"]["engine"]
    assert body["bank_version"] == body["checks"]["bank"]["bank_version"]
    assert body["version_label"].startswith(body["version"] + "+")
    assert body["generated_at"]


def test_ready_health_token_and_wrong_token(client, monkeypatch):
    from app.main import app

    monkeypatch.setenv("STUDY_HEALTH_TOKEN", "probe-token-2026")
    with TestClient(app) as anon:
        full = anon.get("/api/health/ready", headers={"X-Health-Token": "probe-token-2026"})
        assert full.status_code == 200
        assert full.json()["checks"]["bank"]["questions"] > 0
        assert "bank_dir" in full.json()["checks"]["bank"]

        wrong = anon.get("/api/health/ready", headers={"X-Health-Token": "wrong-token-2026"})
        assert wrong.status_code == 200
        assert "questions" not in wrong.json()["checks"]["bank"]

        # 未配置口令时任何值都不算通过
        monkeypatch.delenv("STUDY_HEALTH_TOKEN")
        missing_config = anon.get("/api/health/ready", headers={"X-Health-Token": "probe-token-2026"})
        assert "questions" not in missing_config.json()["checks"]["bank"]


def test_ready_student_session_gets_minimal_view(client, second_client):
    body = second_client.get("/api/health/ready").json()
    assert set(body["checks"]["bank"]) == {"ok"}
    assert "questions" not in body


# ---------------------------------------------------------------------------
# 2. 认证限流补洞与用户名枚举防护
# ---------------------------------------------------------------------------

def test_change_password_and_recovery_codes_are_rate_limited(client):
    for _ in range(10):
        response = client.post("/api/auth/change-password", json={
            "current_password": "wrong-password-999", "new_password": "new-password-123",
        })
        assert response.status_code == 401, response.text
    limited = client.post("/api/auth/change-password", json={
        "current_password": "wrong-password-999", "new_password": "new-password-123",
    })
    assert limited.status_code == 429, limited.text

    # 恢复码是独立账号维度桶：不会与改密共享配额，但同样有上限。
    for _ in range(10):
        assert client.post("/api/auth/recovery-codes", json={
            "current_password": "wrong-password-999",
        }).status_code == 401
    assert client.post("/api/auth/recovery-codes", json={
        "current_password": "wrong-password-999",
    }).status_code == 429


def test_change_password_rate_bucket_is_atomic(isolated_db):
    """新限流作用域同样走 _rate_reserve：并发预约不能突破 10 次。"""
    db.init_db()
    bucket = auth._rate_key("change-password", 1)
    results = []
    lock = threading.Lock()

    def attempt():
        status = "reserved"
        try:
            auth._rate_reserve("change-password", bucket, 10)
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


def test_login_username_global_limit_spans_ips(isolated_db):
    """同一用户名跨 IP 的失败登录总量 15 分钟 20 次，超出后 429（IP 维度仍然保留）。"""
    from app.main import app

    with TestClient(app, client=("10.1.0.1", 5000)) as first:
        assert first.post("/api/auth/register", json={
            "username": "跨IP目标", "password": "admin-pass-123",
        }).status_code == 200
    for ip in ("10.1.0.2", "10.1.0.3"):
        with TestClient(app, client=(ip, 5000)) as worker:
            for _ in range(10):
                assert worker.post("/api/auth/login", json={
                    "username": "跨IP目标", "password": "wrong-password-123",
                }).status_code == 401
    with TestClient(app, client=("10.1.0.4", 5000)) as fresh:
        blocked = fresh.post("/api/auth/login", json={
            "username": "跨IP目标", "password": "wrong-password-123",
        })
        assert blocked.status_code == 429, blocked.text


def test_reset_password_username_global_limit_spans_ips(isolated_db):
    from app.main import app

    with TestClient(app) as setup:
        assert setup.post("/api/auth/register", json={
            "username": "重置目标账号", "password": "admin-pass-123",
        }).status_code == 200
    for ip in ("10.2.0.1", "10.2.0.2"):
        with TestClient(app, client=(ip, 5000)) as worker:
            for _ in range(10):
                assert worker.post("/api/auth/reset-password", json={
                    "username": "重置目标账号", "recovery_code": "ABCD-1234-EF56",
                    "new_password": "brand-new-pass-123",
                }).status_code == 401
    with TestClient(app, client=("10.2.0.3", 5000)) as fresh:
        assert fresh.post("/api/auth/reset-password", json={
            "username": "重置目标账号", "recovery_code": "ABCD-1234-EF56",
            "new_password": "brand-new-pass-123",
        }).status_code == 429


def test_missing_username_burns_constant_time_scrypt(client):
    """用户名不存在时也执行一次 scrypt 假核验，耗时与真实用户接近（防时序枚举）。"""

    def median_login_latency(username):
        samples = []
        for _ in range(3):
            started = time.perf_counter()
            response = client.post("/api/auth/login", json={
                "username": username, "password": "wrong-password-123",
            })
            samples.append(time.perf_counter() - started)
            assert response.status_code == 401
        return sorted(samples)[1]

    existing = median_login_latency("测试管理员")
    missing = median_login_latency("不存在的账号xyz")
    # scrypt(n=16384) 单次约数十毫秒；两条路径都必须真的算了哈希。
    assert existing >= 0.01, existing
    assert missing >= 0.01, missing
    assert 0.25 <= missing / existing <= 4.0, (existing, missing)


def test_public_mode_forces_secure_cookie(isolated_db, monkeypatch):
    from app.main import app

    monkeypatch.setenv("STUDY_PUBLIC_MODE", "1")
    monkeypatch.setenv("STUDY_ADMIN_INIT_TOKEN", "operator-token-2026")
    monkeypatch.delenv("STUDY_REGISTRATION_MODE", raising=False)
    # Secure Cookie 只会在 https 请求上回传：测试客户端模拟 Caddy 终止 TLS。
    with TestClient(app, base_url="https://testserver") as anon:
        created = anon.post("/api/auth/init-admin", json={
            "username": "公网管理员", "password": "admin-pass-123",
            "token": "operator-token-2026",
        })
        assert created.status_code == 200, created.text
        cookie = created.headers["set-cookie"]
        assert "Secure" in cookie
        assert "HttpOnly" in cookie
        assert "SameSite=strict" in cookie
        assert anon.get("/api/auth/me").status_code == 200


# ---------------------------------------------------------------------------
# 3. Redis 就绪检查：RESP over TLS、URL 编码密码与 ACL
# ---------------------------------------------------------------------------

class _FakeSocket:
    def __init__(self, replies):
        self.replies = list(replies)
        self.sent = []
        self.timeout = None
        self.closed = False

    def settimeout(self, value):
        self.timeout = value

    def sendall(self, data):
        self.sent.append(data)

    def recv(self, size):
        if self.replies:
            return self.replies.pop(0)
        return b""

    def close(self):
        self.closed = True


def _decode_resp_stream(payload: bytes) -> list[list[str]]:
    lines = payload.split(b"\r\n")
    commands: list[list[str]] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        index += 1
        if not line:
            continue
        assert line.startswith(b"*"), line
        count = int(line[1:])
        args = []
        for _ in range(count):
            length_line = lines[index]
            index += 1
            assert length_line.startswith(b"$"), length_line
            args.append(lines[index].decode("utf-8"))
            index += 1
        commands.append(args)
    return commands


def test_redis_url_parsing_supports_tls_acl_and_encoded_password():
    target = main._redis_target("rediss://ops:p%40ss%3Aword@redis.internal:6380/3?socket_timeout=4.5")
    assert target["tls"] is True
    assert target["username"] == "ops"
    assert target["password"] == "p@ss:word"
    assert target["host"] == "redis.internal"
    assert target["port"] == 6380
    assert target["db"] == 3
    assert target["timeout"] == 4.5
    assert main._redis_commands(target) == [
        ("AUTH", "ops", "p@ss:word"), ("SELECT", "3"), ("PING",),
    ]

    plain = main._redis_target("redis://:s3cret@10.0.0.5:6379/0")
    assert plain["tls"] is False
    assert plain["username"] is None
    assert plain["password"] == "s3cret"
    assert main._redis_commands(plain) == [("AUTH", "s3cret"), ("PING",)]

    no_auth = main._redis_target("redis://localhost:6379/0")
    assert main._redis_commands(no_auth) == [("PING",)]

    with pytest.raises(ValueError):
        main._redis_target("http://localhost:6379")
    with pytest.raises(ValueError):
        main._redis_target("redis://only-user@localhost:6379/0")


def test_redis_probe_uses_tls_acl_and_expected_command_sequence(monkeypatch):
    fake = _FakeSocket([b"+OK\r\n", b"+OK\r\n", b"+PONG\r\n"])
    seen = {}

    def fake_connect(address, timeout=None):
        seen["address"] = address
        seen["timeout"] = timeout
        return fake

    class FakeSSLContext:
        def __init__(self):
            self.server_hostnames = []

        def wrap_socket(self, sock, server_hostname=None):
            self.server_hostnames.append(server_hostname)
            return sock

    context = FakeSSLContext()
    monkeypatch.setattr(main.socket, "create_connection", fake_connect)
    monkeypatch.setattr(main.ssl, "create_default_context", lambda: context)

    result = main._redis_ping(main._redis_target("rediss://ops:pass@redis.internal:6380/2"))
    assert result["ok"] is True
    assert result["tls"] is True
    assert result["auth"] is True
    assert result["db"] == 2
    assert seen["address"] == ("redis.internal", 6380)
    assert seen["timeout"] == 2.0
    assert context.server_hostnames == ["redis.internal"]
    assert _decode_resp_stream(b"".join(fake.sent)) == [
        ["AUTH", "ops", "pass"], ["SELECT", "2"], ["PING"],
    ]
    assert fake.closed is True


def test_check_redis_reports_acl_failure_and_plain_ping(monkeypatch):
    monkeypatch.setenv("REDIS_URL", "redis://:bad-pass@redis.internal:6379/0")
    fake = _FakeSocket([b"-WRONGPASS invalid username-password pair\r\n"])
    monkeypatch.setattr(main.socket, "create_connection", lambda address, timeout=None: fake)
    failed = main._check_redis()
    assert failed["configured"] is True
    assert failed["ok"] is False
    assert "认证" in failed["detail"]

    plain = _FakeSocket([b"+PONG\r\n"])
    monkeypatch.delenv("REDIS_URL")
    monkeypatch.setenv("REDIS_URL", "redis://redis.internal:6379/0")
    monkeypatch.setattr(main.socket, "create_connection", lambda address, timeout=None: plain)
    ok = main._check_redis()
    assert ok["ok"] is True
    assert ok["auth"] is False
    # 未配置密码时不应发送 AUTH
    assert _decode_resp_stream(b"".join(plain.sent)) == [["PING"]]


# ---------------------------------------------------------------------------
# 4. CSRF Origin 规范化与错误体脱敏
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("origin,host,scheme,expected", [
    ("https://a.com:443", "a.com", "https", True),
    ("https://a.com", "a.com:443", "https", True),
    ("https://A.COM", "a.com", "https", True),
    ("http://a.com:80", "a.com", "http", True),
    ("http://a.com", "a.com:80", "http", True),
    # Host 无端口时，跨协议但仍是该协议默认端口的同源也兼容（旧行为不因大小写/端口写法被误伤）。
    ("https://a.com:443", "a.com", "http", True),
    ("http://a.com:80", "a.com", "https", True),
    ("https://a.com:8443", "a.com", "https", False),
    ("https://a.com", "a.com:80", "https", False),
    ("https://foreign.example:443", "a.com", "https", False),
    ("ftp://a.com", "a.com", "http", False),
])
def test_origin_matches_host_default_ports(origin, host, scheme, expected):
    assert main._origin_matches_host(origin, host, scheme) is expected


def test_csrf_origin_accepts_normalized_same_origin(client):
    for origin in (
        "http://testserver:80",
        "http://TESTSERVER",
        "HTTP://TestServer",
        "https://testserver:443",
    ):
        response = client.post(
            "/api/auth/login",
            json={"username": "测试管理员", "password": "wrong-password-123"},
            headers={"Origin": origin},
        )
        assert response.status_code != 403, (origin, response.text)
    for origin in ("http://testserver:8080", "https://testserver:8443", "https://foreign.example:443"):
        response = client.post(
            "/api/auth/login",
            json={"username": "测试管理员", "password": "wrong-password-123"},
            headers={"Origin": origin},
        )
        assert response.status_code == 403, (origin, response.text)


def test_redact_secrets_helper():
    text = main._redact_secrets(
        "upstream error: sk-abcdef1234567890 and Authorization: Bearer abcdefghijklmnopq"
    )
    assert "abcdef1234567890" not in text
    assert "Bearer ***" in text
    assert "sk-***" in text
    # 普通中文报错不受影响
    assert main._redact_secrets("题库版本不匹配") == "题库版本不匹配"


def test_http_error_bodies_are_redacted(client, monkeypatch):
    from app import ops

    def boom(*args, **kwargs):
        raise HTTPException(409, "上游返回：sk-abcdef1234567890 Bearer abcdefghijklmnop")

    monkeypatch.setattr(ops, "create_backup", boom)
    response = client.post("/api/admin/backup")
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert "abcdef1234567890" not in detail
    assert "Bearer ***" in detail
    assert "sk-***" in detail


# ---------------------------------------------------------------------------
# 5. 缓存头：bank-image private + ETag/304，普通 API 保持 no-store
# ---------------------------------------------------------------------------

@pytest.fixture
def image_bank(tmp_path, monkeypatch):
    bank = make_bank(tmp_path / "bank", [question_row("docA:1", "函数图象题")])
    images = bank / "数学" / "images"
    images.mkdir(parents=True, exist_ok=True)
    image = images / "pic.png"
    image.write_bytes(drawn_image_bytes())
    con = sqlite3.connect(str(bank / "题库.db"))
    try:
        con.execute("CREATE TABLE images (subject TEXT, file TEXT)")
        con.execute("INSERT INTO images(subject,file) VALUES ('数学','images/pic.png')")
        con.commit()
    finally:
        con.close()
    monkeypatch.setattr(db, "BANK_DIR", bank)
    return bank, image


def test_bank_image_private_cache_and_etag_304(client, image_bank):
    params = {"subject": "数学", "file": "images/pic.png"}
    first = client.get("/api/bank-image", params=params)
    assert first.status_code == 200, first.text
    assert "private" in first.headers["cache-control"]
    assert "max-age=3600" in first.headers["cache-control"]
    etag = first.headers.get("etag")
    assert etag

    cached = client.get("/api/bank-image", params=params, headers={"If-None-Match": etag})
    assert cached.status_code == 304
    assert cached.headers["etag"] == etag
    assert "private" in cached.headers["cache-control"]
    assert cached.content == b""

    stale = client.get("/api/bank-image", params=params, headers={"If-None-Match": '"deadbeef-1"'})
    assert stale.status_code == 200
    assert stale.headers["etag"] == etag
    # 普通 API 仍保持 no-store（缓存头不被 bank-image 影响）
    assert client.get("/api/health").headers["cache-control"] == "no-store"
    assert client.get("/api/health/ready").headers["cache-control"] == "no-store"


def test_bank_image_etag_changes_with_file_content(client, image_bank):
    _, image = image_bank
    params = {"subject": "数学", "file": "images/pic.png"}
    before = client.get("/api/bank-image", params=params).headers["etag"]
    image.write_bytes(drawn_image_bytes(size=(260, 260)))
    after = client.get("/api/bank-image", params=params).headers["etag"]
    assert after and after != before


def test_bank_image_still_requires_authentication(client, image_bank):
    from app.main import app

    params = {"subject": "数学", "file": "images/pic.png"}
    with TestClient(app) as anon:
        assert anon.get("/api/bank-image", params=params).status_code == 401


# ---------------------------------------------------------------------------
# 6. 构建号：补 web/styles-pages.css 与提示词 YAML
# ---------------------------------------------------------------------------

def test_build_id_covers_styles_pages_and_prompt_yaml(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    (root / "app" / "ai" / "prompts").mkdir(parents=True)
    (root / "web" / "js").mkdir(parents=True)
    (root / "app" / "main.py").write_text("# app", encoding="utf-8")
    (root / "app" / "ai" / "prompts" / "grid.yaml").write_text("a: 1\n", encoding="utf-8")
    (root / "web" / "js" / "main.js").write_text("// js\n", encoding="utf-8")
    (root / "web" / "styles.css").write_text("body{}\n", encoding="utf-8")
    (root / "web" / "styles-pages.css").write_text(".a{}\n", encoding="utf-8")
    (root / "web" / "index.html").write_text("<html></html>\n", encoding="utf-8")

    monkeypatch.delenv("STUDY_BUILD_ID", raising=False)
    monkeypatch.setattr(main, "__file__", str(root / "app" / "main.py"))
    base = main._compute_build_id()
    assert len(base) == 12

    (root / "web" / "styles-pages.css").write_text(".a{color:red}\n", encoding="utf-8")
    styles_changed = main._compute_build_id()
    assert styles_changed != base

    (root / "app" / "ai" / "prompts" / "grid.yaml").write_text("a: 2\n", encoding="utf-8")
    prompt_changed = main._compute_build_id()
    assert prompt_changed not in (base, styles_changed)

    monkeypatch.setenv("STUDY_BUILD_ID", "commit-abc123")
    assert main._compute_build_id() == "commit-abc123"


def test_current_build_id_is_stable_and_nonempty(monkeypatch):
    monkeypatch.delenv("STUDY_BUILD_ID", raising=False)
    assert len(main.BUILD_ID) == 12
    assert main.BUILD_ID == main.BUILD_ID.lower()
    # 内容指纹为 12 位十六进制（工作区可能被并行开发修改，因此不与导入期 BUILD_ID 比较）。
    current = main._compute_build_id()
    assert len(current) == 12
    assert all(character in "0123456789abcdef" for character in current)
