"""自定义 API 地址/厂商：URL 策略、规范化、鉴权/Header/兼容模型、保存前探测与 Embedding 配置。"""
import asyncio
import json

import httpcore
import httpx
from fastapi import HTTPException
import pytest

from app import ai, db
from app.ai import netguard

_REAL_ASYNC_CLIENT = httpx.AsyncClient


def mock_upstream(monkeypatch, handler, module=None):
    """把 ai/embeddings 的 httpx.AsyncClient 替换为 MockTransport（离线、零配额）。"""
    target = module or ai.httpx
    monkeypatch.setattr(
        target, "AsyncClient",
        lambda **kw: _REAL_ASYNC_CLIENT(transport=httpx.MockTransport(handler), **kw),
    )


def save_provider(client, **changes):
    payload = {
        "name": "自定义连接", "kind": "openai", "base_url": "https://relay.example.test/v1",
        "api_key": "sk-custom-unit-test-secret", "models": ["model-a"],
    }
    payload.update(changes)
    return client.post("/api/providers", json=payload)


# ---------------------------------------------------------------------------
# A. URL 策略与规范化
# ---------------------------------------------------------------------------

def test_url_policy_defaults_and_invalid_fallback(monkeypatch):
    monkeypatch.delenv("STUDY_PUBLIC_MODE", raising=False)
    monkeypatch.delenv("STUDY_AI_URL_POLICY", raising=False)
    assert netguard.url_policy() == "local_open"
    monkeypatch.setenv("STUDY_PUBLIC_MODE", "1")
    assert netguard.url_policy() == "public_https"
    monkeypatch.setenv("STUDY_AI_URL_POLICY", "allowlist")
    assert netguard.url_policy() == "allowlist"
    monkeypatch.setenv("STUDY_AI_URL_POLICY", "not-a-policy")
    assert netguard.url_policy() == "public_https"
    monkeypatch.delenv("STUDY_PUBLIC_MODE")
    assert netguard.url_policy() == "local_open"


def test_normalize_strips_pasted_endpoint_and_keeps_query():
    result = netguard.normalize_base_url(" https://relay.example.test/v1/chat/completions/ ")
    assert result["normalized"] == "https://relay.example.test/v1"
    assert any("chat/completions" in note for note in result["notes"])

    result = netguard.normalize_base_url("relay.example.test/v1/models")
    assert result["normalized"] == "https://relay.example.test/v1"
    assert any("https" in note for note in result["notes"])

    # 本地地址未写协议时补 http://
    result = netguard.normalize_base_url("127.0.0.1:11434/v1")
    assert result["normalized"] == "http://127.0.0.1:11434/v1"

    # Azure 依赖 query，必须保留
    result = netguard.normalize_base_url(
        "https://school.openai.azure.com/openai/deployments/gpt4o/chat/completions?api-version=2024-10-21"
    )
    assert result["normalized"] == (
        "https://school.openai.azure.com/openai/deployments/gpt4o?api-version=2024-10-21"
    )

    # Gemini 原生整段 URL 回到 base
    result = netguard.normalize_base_url(
        "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent"
    )
    assert result["normalized"] == "https://generativelanguage.googleapis.com/v1beta"


@pytest.mark.parametrize("policy", ["local_open", "public_https", "allowlist"])
@pytest.mark.parametrize("url", [
    "http://169.254.169.254/latest/meta-data",
    "http://[fd00:ec2::254]/",
    "https://metadata.google.internal/computeMetadata/v1",
])
def test_metadata_always_blocked(monkeypatch, policy, url):
    monkeypatch.setenv("STUDY_AI_URL_POLICY", policy)
    with pytest.raises(HTTPException) as excinfo:
        netguard.validate_base_url(url)
    assert excinfo.value.status_code == 422


def test_local_open_allows_local_models_and_open_key(client, monkeypatch):
    monkeypatch.setenv("STUDY_AI_URL_POLICY", "local_open")
    saved = save_provider(
        client, name="本地 Ollama", base_url="127.0.0.1:11434/v1",
        api_key="", auth_mode="none", models=["llama3:8b"],
    )
    assert saved.status_code == 200, saved.text
    body = saved.json()
    assert body["base_url"] == "http://127.0.0.1:11434/v1"
    assert body["auth_mode"] == "none"
    assert body["has_api_key"] is False

    # 局域网 + 自定义端口 + 查询参数在本地模式放行
    for url in (
        "http://192.168.1.20:8000/v1",
        "https://relay.example.test:8443/v1",
        "https://relay.example.test/v1?api-version=2024-10-21",
    ):
        response = save_provider(client, name="自定义", base_url=url, models=["m"])
        assert response.status_code == 200, (url, response.text)


def test_local_open_requires_explicit_none_for_empty_key(client, monkeypatch):
    monkeypatch.setenv("STUDY_AI_URL_POLICY", "local_open")
    response = save_provider(client, name="空 Key", base_url="http://127.0.0.1:11434/v1",
                             api_key="", models=["llama3"])
    assert response.status_code == 422
    assert "无鉴权" in response.json()["detail"]


def test_public_https_rejects_http_private_and_metadata(monkeypatch):
    monkeypatch.setenv("STUDY_AI_URL_POLICY", "public_https")
    with pytest.raises(HTTPException):
        netguard.validate_base_url("http://api.openai.com/v1")
    monkeypatch.setattr(
        ai.socket, "getaddrinfo",
        lambda *a, **kw: [(2, 1, 6, "", ("10.0.0.8", 443))],
    )
    with pytest.raises(HTTPException):
        netguard.validate_base_url("https://relay.example.test/v1", resolve=True)
    # 回环/链路本地也拒绝
    monkeypatch.setattr(
        ai.socket, "getaddrinfo",
        lambda *a, **kw: [(2, 1, 6, "", ("127.0.0.1", 443))],
    )
    with pytest.raises(HTTPException):
        netguard.validate_base_url("https://relay.example.test/v1", resolve=True)


def test_public_https_allows_custom_port_and_safe_query(monkeypatch):
    monkeypatch.setenv("STUDY_AI_URL_POLICY", "public_https")
    monkeypatch.setattr(
        ai.socket, "getaddrinfo",
        lambda *a, **kw: [(2, 1, 6, "", ("93.184.216.34", 443))],
    )
    assert netguard.validate_base_url("https://relay.example.test:8443/v1", resolve=True)
    assert netguard.validate_base_url(
        "https://school.openai.azure.com/openai/deployments/gpt4o?api-version=2024-10-21",
        resolve=True,
    )
    # 密钥型 query 必须拒绝，避免 Key 泄露到日志
    with pytest.raises(HTTPException):
        netguard.validate_base_url("https://relay.example.test/v1?key=sk-secret", resolve=True)


def test_allowlist_env_wildcard_and_suggestion(monkeypatch):
    monkeypatch.setenv("STUDY_AI_URL_POLICY", "allowlist")
    monkeypatch.setenv("STUDY_AI_ALLOWED_HOSTS", "*.openai.azure.com,gateway.my-school.edu.cn")
    monkeypatch.setattr(
        ai.socket, "getaddrinfo",
        lambda *a, **kw: [(2, 1, 6, "", ("93.184.216.34", 443))],
    )
    assert ai._validate_url(
        "https://school.openai.azure.com/openai/deployments/gpt4o?api-version=2024-10-21",
        resolve=True,
    )
    assert ai._validate_url("https://gateway.my-school.edu.cn/v1", resolve=True)
    with pytest.raises(HTTPException) as excinfo:
        ai._validate_url("https://new-relay.example.test/v1")
    detail = excinfo.value.detail
    assert detail["code"] == "url_not_allowed"
    assert detail["suggested_allowlist_entry"] == "new-relay.example.test"


def test_ip_pinning_transport_binds_verified_ips(monkeypatch):
    transport = netguard.pinned_transport("relay.example.test", 443, ["93.184.216.34", "93.184.216.35"])
    backend = transport.network_backend
    assert backend.host == "relay.example.test"
    assert backend.next_target() == "93.184.216.34"
    assert backend.next_target() == "93.184.216.35"
    with pytest.raises(ValueError):
        netguard.pinned_transport("relay.example.test", 443, [])

    monkeypatch.setenv("STUDY_AI_URL_POLICY", "public_https")
    monkeypatch.setattr(
        ai.socket, "getaddrinfo",
        lambda *a, **kw: [(2, 1, 6, "", ("93.184.216.34", 443))],
    )
    kwargs = netguard.client_kwargs("https://relay.example.test/v1/chat/completions")
    assert "transport" in kwargs and kwargs["trust_env"] is False
    # 私网解析必须拒绝，禁止回退到「不 Pinning 直连」
    monkeypatch.setattr(
        ai.socket, "getaddrinfo",
        lambda *a, **kw: [(2, 1, 6, "", ("10.0.0.8", 443))],
    )
    with pytest.raises(HTTPException):
        netguard.client_kwargs("https://relay.example.test/v1/chat/completions")

    monkeypatch.setenv("STUDY_AI_URL_POLICY", "local_open")
    assert "transport" not in netguard.client_kwargs("http://127.0.0.1:11434/v1/chat/completions")


def test_pinned_transport_keeps_sni_and_connects_to_verified_ip(monkeypatch):
    captured = {}

    async def fake_handle(self, request):
        captured["sni"] = request.extensions.get("sni_hostname")
        return httpx.Response(200, json={})

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", fake_handle)
    transport = netguard.pinned_transport("relay.example.test", 443, ["93.184.216.34"])
    asyncio.run(transport.handle_async_request(
        httpx.Request("GET", "https://relay.example.test/v1/models"),
    ))
    # HTTPS 证书与 SNI 仍然按原始域名校验
    assert captured["sni"] == "relay.example.test"

    connects = []

    async def fake_connect(self, host, port, timeout=None, local_address=None, socket_options=None):
        connects.append((host, port))
        return object()

    monkeypatch.setattr(httpcore.AnyIOBackend, "connect_tcp", fake_connect)
    backend = netguard.PinnedNetworkBackend("relay.example.test", ["93.184.216.34"])
    asyncio.run(backend.connect_tcp("relay.example.test", 443))
    # 实际 TCP 目标被替换成已校验的 IP
    assert connects == [("93.184.216.34", 443)]


# ---------------------------------------------------------------------------
# B. 数据模型：auth_mode / 自定义 Header / compat / 删除清理
# ---------------------------------------------------------------------------

def test_provider_columns_and_public_view_hides_secrets(client):
    response = save_provider(
        client,
        auth_mode="bearer",
        extra_headers={"HTTP-Referer": "https://zhixu.test", "X-Title": "知序"},
        compat={
            "max_tokens_param": "max_completion_tokens",
            "timeout_seconds": 45,
            "use_proxy_env": True,
            "api_version": "2024-10-21",
            "provider_kind": "openai",
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["auth_mode"] == "bearer"
    assert sorted(body["extra_header_names"]) == ["HTTP-Referer", "X-Title"]
    assert body["compat"]["timeout_seconds"] == 45
    assert body["compat"]["max_tokens_param"] == "max_completion_tokens"
    serialized = json.dumps(body, ensure_ascii=False)
    assert "https://zhixu.test" not in serialized
    assert body["key_hint"].endswith("cret")
    assert "sk-custom-unit-test-secret" not in serialized
    with db.connection() as con:
        row = con.execute(
            "SELECT auth_mode, extra_headers_enc, compat_json FROM ai_providers WHERE id=?",
            (body["id"],),
        ).fetchone()
    assert row["auth_mode"] == "bearer"
    assert row["extra_headers_enc"] and "HTTP-Referer" not in row["extra_headers_enc"]
    assert json.loads(row["compat_json"])["timeout_seconds"] == 45

    with db.connection() as con:
        columns = {r[1] for r in con.execute('PRAGMA table_info("ai_providers")')}
    assert {"auth_mode", "extra_headers_enc", "compat_json"} <= columns
    with db.connection() as con:
        table = con.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='ai_embeddings_config'"
        ).fetchone()[0]
    assert table == 1


@pytest.mark.parametrize("field,value", [
    ("extra_headers", {"Host": "evil.example"}),
    ("extra_headers", {"Content-Length": "0"}),
    ("extra_headers", {f"X-H{i}": "1" for i in range(9)}),
    ("compat", {"timeout_seconds": 5}),
    ("compat", {"timeout_seconds": 601}),
    ("compat", {"max_tokens_param": "bogus"}),
    ("compat", {"use_proxy_env": "yes"}),
    ("kind", "not-a-kind"),
    ("auth_mode", "cookie"),
])
def test_invalid_connection_options_rejected(client, field, value):
    response = save_provider(client, **{field: value})
    assert response.status_code == 422


def test_short_or_local_key_allowed_but_never_returned(client):
    response = save_provider(client, api_key="ollama", base_url="http://127.0.0.1:11434/v1")
    assert response.status_code == 200, response.text
    assert "ollama" not in json.dumps(response.json(), ensure_ascii=False)


@pytest.mark.parametrize("kind,base_url", [
    ("openai", "https://relay.example.test/v1"),
    ("anthropic", "https://relay.example.test/v1"),
    ("gemini", "https://generativelanguage.googleapis.com/v1beta/openai"),
    ("gemini_native", "https://generativelanguage.googleapis.com/v1beta"),
    ("azure_openai", "https://school.openai.azure.com/openai/deployments/gpt4o?api-version=2024-10-21"),
])
def test_provider_kinds_accepted(client, kind, base_url):
    response = save_provider(client, kind=kind, base_url=base_url, models=["test-model"])
    assert response.status_code == 200, response.text
    assert response.json()["kind"] == kind


def test_delete_provider_clears_task_defaults(client):
    provider = save_provider(client, models=["model-a", "model-b"]).json()
    defaults = {
        "task": "grade",
        "primary_provider_id": provider["id"], "primary_model": "model-a",
        "verify_provider_id": provider["id"], "verify_model": "model-a",
        "judge_provider_id": provider["id"], "judge_model": "model-b",
    }
    assert client.put("/api/ai/task-defaults", json=defaults).status_code == 200
    assert client.delete(f"/api/providers/{provider['id']}").status_code == 200
    row = client.get("/api/ai/task-defaults").json()["items"]["grade"]
    assert row["primary_provider_id"] is None and row["primary_model"] is None
    assert row["verify_provider_id"] is None and row["verify_model"] is None
    assert row["judge_provider_id"] is None and row["judge_model"] is None


# ---------------------------------------------------------------------------
# C1. 保存前拉取模型列表
# ---------------------------------------------------------------------------

def test_fetch_models_preview_openai_compatible_full_list(client, monkeypatch):
    captured = {}
    models = [{"id": f"relay-model-{index:03d}"} for index in range(120)]

    def handler(request):
        captured["url"] = str(request.url)
        captured["authorization"] = request.headers.get("authorization")
        return httpx.Response(200, json={"data": models})

    mock_upstream(monkeypatch, handler)
    response = client.post("/api/providers/fetch-models-preview", json={
        "kind": "openai",
        "base_url": "https://relay.example.test/v1/chat/completions",
        "api_key": "sk-preview-secret",
    })
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True
    assert body["normalized_base_url"] == "https://relay.example.test/v1"
    assert captured["url"] == "https://relay.example.test/v1/models"
    assert captured["authorization"] == "Bearer sk-preview-secret"
    # 全量返回，不允许静默截断（旧实现截断 80 条）
    assert body["count"] == 120
    assert len(body["models"]) == 120
    assert all(item["caps"] for item in body["models"])
    assert "sk-preview-secret" not in json.dumps(body, ensure_ascii=False)


def test_fetch_models_preview_gemini_native(client, monkeypatch):
    captured = {}

    def handler(request):
        captured["url"] = str(request.url)
        return httpx.Response(200, json={
            "models": [
                {"name": "models/gemini-2.0-flash"},
                {"name": "models/gemini-2.5-pro"},
            ],
        })

    mock_upstream(monkeypatch, handler)
    response = client.post("/api/providers/fetch-models-preview", json={
        "kind": "gemini_native",
        "base_url": "https://generativelanguage.googleapis.com/v1beta",
        "api_key": "gemini-secret-key",
    })
    assert response.status_code == 200, response.text
    body = response.json()
    assert [item["model_id"] for item in body["models"]] == ["gemini-2.0-flash", "gemini-2.5-pro"]
    assert ":generateContent" not in captured["url"]
    assert "key=gemini-secret-key" in captured["url"]
    assert "gemini-secret-key" not in json.dumps(body, ensure_ascii=False)


def test_fetch_models_preview_ollama_native_fallback(client, monkeypatch):
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if request.url.path == "/v1/models":
            return httpx.Response(404, text="not found")
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "llama3:8b"}, {"name": "qwen2.5:7b"}]})
        return httpx.Response(500)

    mock_upstream(monkeypatch, handler)
    response = client.post("/api/providers/fetch-models-preview", json={
        "kind": "openai",
        "base_url": "http://127.0.0.1:11434/v1",
        "auth_mode": "none",
    })
    assert response.status_code == 200, response.text
    body = response.json()
    assert calls[:2] == ["/v1/models", "/api/tags"]
    assert [item["model_id"] for item in body["models"]] == ["llama3:8b", "qwen2.5:7b"]
    assert any("Ollama" in note for note in body["notes"])


def test_fetch_models_preview_reuses_saved_key_for_edit_mode(client, monkeypatch):
    provider = save_provider(client, api_key="sk-saved-secret-value").json()
    captured = {}

    def handler(request):
        captured["authorization"] = request.headers.get("authorization")
        captured["url"] = str(request.url)
        return httpx.Response(200, json={"data": [{"id": "new-model"}]})

    mock_upstream(monkeypatch, handler)
    response = client.post("/api/providers/fetch-models-preview", json={
        "kind": "openai",
        "base_url": "https://new-relay.example.test/v2",
        "provider_id": provider["id"],
    })
    assert response.status_code == 200, response.text
    # Key 复用已存密文，但地址一律使用表单最新值
    assert captured["authorization"] == "Bearer sk-saved-secret-value"
    assert captured["url"] == "https://new-relay.example.test/v2/models"
    assert response.json()["normalized_base_url"] == "https://new-relay.example.test/v2"


def test_fetch_models_preview_rate_limited(client, monkeypatch):
    def handler(request):
        return httpx.Response(200, json={"data": [{"id": "m"}]})

    mock_upstream(monkeypatch, handler)
    payload = {"kind": "openai", "base_url": "https://relay.example.test/v1", "api_key": "sk-rate"}
    for _ in range(10):
        assert client.post("/api/providers/fetch-models-preview", json=payload).status_code == 200
    limited = client.post("/api/providers/fetch-models-preview", json=payload)
    assert limited.status_code == 429


# ---------------------------------------------------------------------------
# C2. 保存前探测
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("kind,base_url,model,path_fragment", [
    ("openai", "https://relay.example.test/v1", "gpt-4o-mini", "/v1/chat/completions"),
    ("anthropic", "https://api.anthropic.com/v1", "claude-3-5-sonnet", "/v1/messages"),
    ("gemini_native", "https://generativelanguage.googleapis.com/v1beta", "gemini-2.0-flash", ":generateContent"),
    ("azure_openai", "https://school.openai.azure.com/openai/deployments/gpt4o", "gpt-4o", "/chat/completions"),
])
def test_probe_minimal_request_success(client, monkeypatch, kind, base_url, model, path_fragment):
    captured = {}

    def handler(request):
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        if kind == "anthropic":
            return httpx.Response(200, json={
                "content": [{"type": "text", "text": "OK"}], "stop_reason": "end_turn",
                "usage": {"input_tokens": 1, "output_tokens": 1},
            })
        if kind == "gemini_native":
            return httpx.Response(200, json={
                "candidates": [{"content": {"parts": [{"text": "OK"}]}, "finishReason": "STOP"}],
                "usageMetadata": {"promptTokenCount": 1, "candidatesTokenCount": 1},
            })
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "OK"}, "finish_reason": "stop"}],
            "usage": {"total_tokens": 2},
        })

    mock_upstream(monkeypatch, handler)
    response = client.post("/api/providers/probe", json={
        "kind": kind, "base_url": base_url, "api_key": "sk-probe-secret",
        "model": model, "compat": {"api_version": "2024-10-21"},
    })
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True
    assert body["http_status"] == 200
    assert isinstance(body["latency_ms"], int) and body["latency_ms"] >= 0
    assert path_fragment in captured["url"]
    if kind == "gemini_native":
        assert captured["body"]["generationConfig"]["maxOutputTokens"] == 16
        assert "key=sk-probe-secret" in captured["url"]
    elif kind == "anthropic":
        assert captured["body"]["max_tokens"] == 16
        assert captured["body"]["messages"][0]["content"][0]["text"]
    else:
        assert captured["body"]["max_tokens"] == 16
    assert "sk-probe-secret" not in json.dumps(body, ensure_ascii=False)


@pytest.mark.parametrize("status,message,fragment", [
    (401, "Incorrect API key provided: sk-bad-secret", "401"),
    (403, "model not allowed", "403"),
    (404, "not found", "404"),
    (400, "invalid model name", "400"),
    (429, "rate limit", "429"),
])
def test_probe_diagnosis_mapped_and_masked(client, monkeypatch, status, message, fragment):
    def handler(request):
        return httpx.Response(status, json={"error": {"message": message}})

    mock_upstream(monkeypatch, handler)
    response = client.post("/api/providers/probe", json={
        "kind": "openai", "base_url": "https://relay.example.test/v1",
        "api_key": "sk-bad-secret", "model": "test-model",
    })
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is False
    assert body["http_status"] == status
    assert fragment in body["diagnosis"]
    serialized = json.dumps(body, ensure_ascii=False)
    assert "sk-bad-secret" not in serialized


def test_probe_timeout_diagnosis(client, monkeypatch):
    def handler(request):
        raise httpx.TimeoutException("timed out")

    mock_upstream(monkeypatch, handler)
    response = client.post("/api/providers/probe", json={
        "kind": "openai", "base_url": "https://relay.example.test/v1",
        "api_key": "sk-timeout", "model": "test-model",
    })
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is False
    assert "超时" in body["diagnosis"]


def test_probe_gemini_native_error_frame_masked(client, monkeypatch):
    def handler(request):
        return httpx.Response(200, json={
            "candidates": [],
            "promptFeedback": {"blockReason": "SAFETY"},
        })

    mock_upstream(monkeypatch, handler)
    response = client.post("/api/providers/probe", json={
        "kind": "gemini_native",
        "base_url": "https://generativelanguage.googleapis.com/v1beta",
        "api_key": "gemini-secret", "model": "gemini-2.0-flash",
    })
    assert response.status_code == 200, response.text
    assert response.json()["ok"] is True  # HTTP 200 即视为连通，内容拦截不影响连通性判断


def test_probe_reuses_saved_key_but_uses_form_headers(client, monkeypatch):
    provider = save_provider(
        client, extra_headers={"HTTP-Referer": "https://saved.example"},
    ).json()
    captured = {}

    def handler(request):
        captured["authorization"] = request.headers.get("authorization")
        captured["http_referer"] = request.headers.get("http-referer")
        captured["x_trace"] = request.headers.get("x-trace")
        return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})

    mock_upstream(monkeypatch, handler)
    response = client.post("/api/providers/probe", json={
        "kind": "openai",
        "base_url": "https://form-latest.example.test/v3",
        "provider_id": provider["id"],
        "model": "model-a",
        "extra_headers": {"X-Trace": "trace-1"},
    })
    assert response.status_code == 200, response.text
    assert response.json()["ok"] is True
    assert captured["authorization"] == "Bearer sk-custom-unit-test-secret"
    # 探测/预览的 Header 一律取表单最新值，不回放已存连接的旧 Header
    assert captured["x_trace"] == "trace-1"
    assert captured["http_referer"] is None


def test_probe_rate_limited(client, monkeypatch):
    def handler(request):
        return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})

    mock_upstream(monkeypatch, handler)
    payload = {"kind": "openai", "base_url": "https://relay.example.test/v1",
               "api_key": "sk-rate", "model": "m"}
    for _ in range(10):
        assert client.post("/api/providers/probe", json=payload).status_code == 200
    assert client.post("/api/providers/probe", json=payload).status_code == 429


# ---------------------------------------------------------------------------
# E. Embedding 配置
# ---------------------------------------------------------------------------

def _clear_embedding_env(monkeypatch):
    for name in ("STUDY_EMBEDDING_BASE_URL", "STUDY_EMBEDDING_MODEL", "STUDY_EMBEDDING_API_KEY"):
        monkeypatch.delenv(name, raising=False)


def test_embedding_config_crud_and_env_fallback(client, monkeypatch):
    _clear_embedding_env(monkeypatch)
    empty = client.get("/api/providers/embeddings-config").json()
    assert empty["mode"] == "off"
    assert empty["env_configured"] is False
    assert empty["effective"]["ready"] is False

    created = client.put("/api/providers/embeddings-config", json={
        "mode": "custom", "base_url": "https://vectors.example.test/v1",
        "model": "text-embedding-3-small", "api_key": "sk-embedding-secret-value",
    })
    assert created.status_code == 200, created.text
    body = created.json()
    assert body["mode"] == "custom" and body["has_api_key"] is True
    assert body["effective"]["ready"] is True
    assert body["effective_source"] == "custom"
    assert "sk-embedding-secret-value" not in json.dumps(body, ensure_ascii=False)
    user_id = client.get("/api/auth/me").json()["id"]
    with db.connection() as con:
        row = con.execute(
            "SELECT api_key_enc FROM ai_embeddings_config WHERE user_id=?", (user_id,),
        ).fetchone()
    assert row["api_key_enc"] and "sk-embedding-secret-value" not in row["api_key_enc"]

    # 环境变量作为默认回退（未落库账号配置时）
    _clear_embedding_env(monkeypatch)
    client.put("/api/providers/embeddings-config", json={"mode": "off"})
    monkeypatch.setenv("STUDY_EMBEDDING_BASE_URL", "https://env-vectors.example.test/v1")
    monkeypatch.setenv("STUDY_EMBEDDING_MODEL", "env-embedding-model")
    off_view = client.get("/api/providers/embeddings-config").json()
    assert off_view["env_configured"] is True
    assert off_view["effective"]["ready"] is False  # off 显式关闭优先于环境变量
    monkeypatch.delenv("STUDY_EMBEDDING_BASE_URL")
    monkeypatch.delenv("STUDY_EMBEDDING_MODEL")


def test_embedding_config_inherit_and_delete_cleanup(client, monkeypatch):
    _clear_embedding_env(monkeypatch)
    provider = save_provider(client, models=["text-embedding-3-small"]).json()
    response = client.put("/api/providers/embeddings-config", json={
        "mode": "inherit", "provider_id": provider["id"], "model": "text-embedding-3-small",
    })
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["mode"] == "inherit"
    assert body["effective_source"] == "inherit"
    assert body["effective"]["model"] == "text-embedding-3-small"
    assert body["has_api_key"] is False

    # 复用的是 provider 的加密 Key：真实请求必须带出该 Key
    captured = {}

    def handler(request):
        captured["authorization"] = request.headers.get("authorization")
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [0.1, 0.2]}]})

    from app.ai import embeddings

    mock_upstream(monkeypatch, handler, module=embeddings.httpx)
    tested = client.post("/api/providers/embeddings-test", json={})
    assert tested.status_code == 200, tested.text
    assert tested.json()["ok"] is True
    assert tested.json()["dimension"] == 2
    assert captured["authorization"] == "Bearer sk-custom-unit-test-secret"

    assert client.delete(f"/api/providers/{provider['id']}").status_code == 200
    degraded = client.get("/api/providers/embeddings-config").json()
    assert degraded["mode"] == "off"


def test_embedding_test_reports_dimension_and_masked_error(client, monkeypatch):
    _clear_embedding_env(monkeypatch)
    client.put("/api/providers/embeddings-config", json={
        "mode": "custom", "base_url": "https://vectors.example.test/v1",
        "model": "text-embedding-3-small", "api_key": "sk-embedding-secret",
    })
    from app.ai import embeddings

    def ok_handler(request):
        return httpx.Response(200, json={"data": [
            {"index": 0, "embedding": [0.1, 0.2, 0.3]},
        ]})

    mock_upstream(monkeypatch, ok_handler, module=embeddings.httpx)
    ok_body = client.post("/api/providers/embeddings-test", json={}).json()
    assert ok_body["ok"] is True
    assert ok_body["dimension"] == 3
    assert ok_body["latency_ms"] is not None

    def fail_handler(request):
        return httpx.Response(401, json={"error": {"message": "bad key sk-embedding-secret"}})

    mock_upstream(monkeypatch, fail_handler, module=embeddings.httpx)
    failed = client.post("/api/providers/embeddings-test", json={}).json()
    assert failed["ok"] is False
    assert failed["dimension"] is None
    assert "401" in failed["diagnosis"]
    assert "sk-embedding-secret" not in json.dumps(failed, ensure_ascii=False)


def test_embedding_config_rejects_bad_input(client, monkeypatch):
    _clear_embedding_env(monkeypatch)
    assert client.put("/api/providers/embeddings-config", json={
        "mode": "custom", "base_url": "http://169.254.169.254/v1", "model": "m",
    }).status_code == 422
    assert client.put("/api/providers/embeddings-config", json={
        "mode": "inherit", "provider_id": 9999, "model": "m",
    }).status_code == 404
    assert client.put("/api/providers/embeddings-config", json={
        "mode": "custom", "base_url": "https://vectors.example.test/v1",
    }).status_code == 422
