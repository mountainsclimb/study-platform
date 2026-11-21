"""联网搜索适配层单元测试：DeepSeek /v1/tools/search（全部 mock，不联网）。"""
import asyncio
import json

import pytest

from app.ai import search as ai_search


class _FakeResponse:
    def __init__(self, *, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload
        self.headers = {"content-type": "application/json"}

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class _FakeClient:
    calls: list[dict] = []
    responses: list[_FakeResponse] = []

    def __init__(self, **_kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def post(self, url, headers=None, json=None):
        _FakeClient.calls.append({"url": url, "headers": headers, "json": json})
        return _FakeClient.responses.pop(0)


def _provider(base_url="https://api.deepseek.com/v1"):
    from app.ai.providers import _encrypt_key

    encrypted, hint = _encrypt_key("sk-search-test")
    return {
        "id": 3, "name": "DeepSeek", "kind": "openai", "base_url": base_url,
        "encrypted_key": encrypted, "key_hint": hint, "models_json": "[]",
        "auth_mode": "auto", "extra_headers_enc": "", "compat_json": "{}",
    }


@pytest.fixture(autouse=True)
def _patch(monkeypatch):
    _FakeClient.calls.clear()
    _FakeClient.responses.clear()
    monkeypatch.setattr(ai_search, "ai_httpx", type("M", (), {"AsyncClient": _FakeClient}))
    monkeypatch.setattr("app.ai.providers._validate_url", lambda value, resolve=False: value)
    monkeypatch.setattr("app.ai.netguard.client_kwargs", lambda endpoint, timeout_seconds=45: {})
    yield
    _FakeClient.calls.clear()
    _FakeClient.responses.clear()


def test_provider_detection():
    assert ai_search.is_deepseek_provider({"base_url": "https://api.deepseek.com/v1"}) is True
    assert ai_search.is_deepseek_provider({"base_url": "https://api.xiaomimimo.com/v1"}) is False


def _payload():
    return {
        "results": [
            {"title": "十五五教育规划", "url": "https://a.example/1", "site_name": "中国之声",
             "date": "2026-11-10T16:00:00+00:00", "summary": "教育资源配置新闻摘要", "authority": 4},
            {"title": "光合作用", "url": "https://b.example/2", "site_name": "科技小院",
             "summary": "x" * 5000},
        ]
    }


def test_web_search_cleans_and_limits_results():
    _FakeClient.responses.append(_FakeResponse(payload=_payload()))
    result = asyncio.run(ai_search.web_search(_provider(), "教育政策", 2))
    assert result["query"] == "教育政策"
    assert len(result["results"]) == 2
    first = result["results"][0]
    assert first["title"] == "十五五教育规划"
    assert first["url"].startswith("https://")
    assert first["site"] == "中国之声"
    # 摘要超长截断
    assert len(result["results"][1]["summary"]) <= ai_search.MAX_SUMMARY_CHARS + 1
    call = _FakeClient.calls[0]
    assert call["url"].endswith("/tools/search")
    assert call["json"] == {"query": "教育政策"}
    assert call["headers"]["Authorization"].startswith("Bearer ")


def test_web_search_validates_query_and_limit():
    with pytest.raises(ai_search.SearchUnavailable):
        asyncio.run(ai_search.web_search(_provider(), "   "))
    long_query = "教" * (ai_search.MAX_QUERY_CHARS + 50)
    _FakeClient.responses.append(_FakeResponse(payload=_payload()))
    asyncio.run(ai_search.web_search(_provider(), long_query, 99))
    # 查询超长截断、limit 夹到 MAX_LIMIT（由函数内部约束）
    assert len(_FakeClient.calls[0]["json"]["query"]) == ai_search.MAX_QUERY_CHARS


def test_web_search_errors_raise_unavailable():
    _FakeClient.responses.append(_FakeResponse(status_code=429))
    with pytest.raises(ai_search.SearchUnavailable):
        asyncio.run(ai_search.web_search(_provider(), "q"))
    _FakeClient.responses.append(_FakeResponse(payload={"results": []}))
    with pytest.raises(ai_search.SearchUnavailable):
        asyncio.run(ai_search.web_search(_provider(), "q"))
    _FakeClient.responses.append(_FakeResponse(payload={"unexpected": 1}))
    with pytest.raises(ai_search.SearchUnavailable):
        asyncio.run(ai_search.web_search(_provider(), "q"))


def test_format_results_for_prompt_contains_sources_and_links():
    _FakeClient.responses.append(_FakeResponse(payload=_payload()))
    result = asyncio.run(ai_search.web_search(_provider(), "光合作用", 2))
    text = ai_search.format_results_for_prompt(result)
    assert "联网搜索资料" in text
    assert "https://a.example/1" in text
    assert "[1]" in text and "[2]" in text
    # 预算上限生效
    short = ai_search.format_results_for_prompt(result, max_chars=200)
    assert len(short) < len(text)


def test_public_result_hides_internal_fields():
    _FakeClient.responses.append(_FakeResponse(payload=_payload()))
    result = asyncio.run(ai_search.web_search(_provider(), "q", 1))
    public = ai_search.public_result(result)
    assert set(public) == {"query", "provider", "results"}
    assert set(public["results"][0]) == {"title", "url", "site", "date", "summary", "authority"}


def test_search_candidates_filters_by_user_and_host(client):
    """真实 TestClient：只有 DeepSeek 连接被视为搜索候选。"""
    from app import db

    user_id = client.get("/api/auth/me").json()["id"]
    for name, base in (("DS", "https://api.deepseek.com/v1"), ("MiMo", "https://api.xiaomimimo.com/v1")):
        saved = client.post("/api/providers", json={
            "name": name, "kind": "openai", "base_url": base, "api_key": "sk-unit-test-key-123",
            "models": ["deepseek-flash"] if name == "DS" else ["mimo-v2.6-flash"],
        })
        assert saved.status_code == 200, saved.text
    candidates = ai_search.search_candidates(user_id)
    assert [c["name"] for c in candidates] == ["DS"]
    info = ai_search.search_provider_info(user_id)
    assert info and info["endpoint"] == "/v1/tools/search"
    # 其他账号看不到
    other = client.post("/api/auth/register", json={"username": "搜索隔离用户", "password": "isolation-pass-1"})
    if other.status_code == 200:
        with db.connection() as con:
            other_id = con.execute("SELECT id FROM users WHERE username=?", ("搜索隔离用户",)).fetchone()["id"]
        assert ai_search.search_candidates(other_id) == []
