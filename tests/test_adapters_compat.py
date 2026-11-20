"""协议适配层兼容：URL 拼接、鉴权/Header、max_tokens 互切、reasoning 分流、记账与 caps。"""
import asyncio
import json

import httpx
import pytest

from app import ai, db
from app.ai import adapters, netguard, providers
from test_ai import save_provider

KEY = "sk-adapters-unit-test-secret"
_REAL_ASYNC_CLIENT = httpx.AsyncClient


def provider_dict(**changes):
    payload = {
        "kind": "openai", "name": "mock",
        "base_url": "https://relay.example.test/v1",
        "encrypted_key": ai._cipher().encrypt(KEY.encode()).decode(),
    }
    payload.update(changes)
    return payload


def mock_client(monkeypatch, handler):
    monkeypatch.setattr(
        ai.httpx, "AsyncClient",
        lambda **kw: _REAL_ASYNC_CLIENT(transport=httpx.MockTransport(handler), **kw),
    )
    # 兼容层单测不联网：跳过域名解析策略，直接使用 mock transport。
    monkeypatch.setattr(ai, "_validate_url", lambda value, **kw: value)


def sse_response(frames: list[str]) -> httpx.Response:
    content = "".join(f"data: {frame}\n\n" for frame in frames) + "data: [DONE]\n\n"
    return httpx.Response(200, content=content.encode(), headers={"content-type": "text/event-stream"})


# ---------------------------------------------------------------------------
# URL 拼接 / 协议端点
# ---------------------------------------------------------------------------

def test_prepare_request_never_duplicates_endpoint():
    openai_request = adapters._prepare_request(
        provider_dict(base_url="https://relay.example.test/v1/chat/completions"),
        "gpt-4o", "hi", api_key="k",
    )
    assert openai_request.endpoint == "https://relay.example.test/v1/chat/completions"

    anthropic_request = adapters._prepare_request(
        provider_dict(kind="anthropic", base_url="https://relay.example.test/v1/messages"),
        "claude-3-5-sonnet", "hi", api_key="k",
    )
    assert anthropic_request.endpoint == "https://relay.example.test/v1/messages"


def test_azure_openai_endpoint_keeps_api_version():
    prepared = adapters._prepare_request(
        provider_dict(
            kind="azure_openai", auth_mode="api-key",
            base_url="https://school.openai.azure.com/openai/deployments/gpt4o",
            compat_json=json.dumps({"api_version": "2024-10-21"}),
        ),
        "gpt-4o", "hi", api_key="azure-key",
    )
    assert prepared.endpoint.startswith(
        "https://school.openai.azure.com/openai/deployments/gpt4o/chat/completions"
    )
    assert "api-version=2024-10-21" in prepared.endpoint
    assert prepared.headers["api-key"] == "azure-key"
    assert "Authorization" not in prepared.headers


# ---------------------------------------------------------------------------
# 鉴权 / Header
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("kind,auth_mode,expected", [
    ("openai", "bearer", {"Authorization": "Bearer k-123"}),
    ("anthropic", "x-api-key", {"x-api-key": "k-123", "anthropic-version": "2023-06-01"}),
    ("azure_openai", "api-key", {"api-key": "k-123"}),
])
def test_auth_headers_by_mode(kind, auth_mode, expected):
    prepared = adapters._prepare_request(
        provider_dict(kind=kind, auth_mode=auth_mode), "model-x", "hi", api_key="k-123",
    )
    for name, value in expected.items():
        assert prepared.headers[name] == value


def test_query_key_and_none_auth_modes():
    gemini = adapters._prepare_request(
        provider_dict(kind="gemini_native", auth_mode="query_key",
                      base_url="https://generativelanguage.googleapis.com/v1beta"),
        "gemini-2.0-flash", "hi", api_key="gemini-key",
    )
    assert "key=gemini-key" in gemini.endpoint
    assert "x-api-key" not in gemini.headers and "Authorization" not in gemini.headers

    none = adapters._prepare_request(
        provider_dict(base_url="http://127.0.0.1:11434/v1", auth_mode="none"),
        "llama3", "hi", api_key="",
    )
    assert "Authorization" not in none.headers and "key=" not in none.endpoint


def test_extra_headers_applied_but_system_headers_protected():
    prepared = adapters._prepare_request(
        provider_dict(), "model-x", "hi", api_key="k",
        extra_headers={
            "HTTP-Referer": "https://zhixu.test",
            "X-Title": "知序",
            "Host": "evil.example",
            "Connection": "close",
        },
    )
    assert prepared.headers["HTTP-Referer"] == "https://zhixu.test"
    title = prepared.headers["X-Title"]
    assert title == "知序" or title == "知序".encode("utf-8")
    assert "Host" not in prepared.headers
    assert "Connection" not in prepared.headers


def test_saved_extra_headers_sent_on_call(client, monkeypatch):
    saved = save_provider(
        client, extra_headers={"HTTP-Referer": "https://zhixu.test", "X-Title": "知序"},
    )
    user_id = client.get("/api/auth/me").json()["id"]
    provider = providers._owned("ai_providers", saved["id"], user_id)
    captured = {}

    def handler(request):
        captured["headers"] = dict(request.headers)
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "带自定义头"}, "finish_reason": "stop"}],
            "usage": {"total_tokens": 1},
        })

    mock_client(monkeypatch, handler)
    result = asyncio.run(adapters._call_model(provider, "model-a", "hi"))
    assert result["content"] == "带自定义头"
    assert captured["headers"]["http-referer"] == "https://zhixu.test"
    assert captured["headers"]["x-title"] == "知序"


# ---------------------------------------------------------------------------
# max_tokens ↔ max_completion_tokens
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("model,compat,expected_param", [
    ("gpt-4o", {}, "max_tokens"),
    ("deepseek-chat", {}, "max_tokens"),
    ("o3-mini", {}, "max_completion_tokens"),
    ("o4-mini", {}, "max_completion_tokens"),
    ("gpt-5", {}, "max_completion_tokens"),
    ("o3-mini", {"max_tokens_param": "max_tokens"}, "max_tokens"),
    ("gpt-4o", {"max_tokens_param": "max_completion_tokens"}, "max_completion_tokens"),
])
def test_max_tokens_param_selection(model, compat, expected_param):
    prepared = adapters._prepare_request(
        provider_dict(), model, "hi", api_key="k", compat=compat,
    )
    assert expected_param in prepared.body
    assert ("max_tokens" if expected_param != "max_tokens" else "max_completion_tokens") not in prepared.body


def test_400_auto_switches_to_max_completion_tokens(monkeypatch):
    seen = []

    def handler(request):
        body = json.loads(request.content)
        seen.append(body)
        if "max_tokens" in body:
            return httpx.Response(400, json={"error": {"message": (
                "Unsupported parameter: 'max_tokens' is not supported with this model. "
                "Use 'max_completion_tokens' instead."
            )}})
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "切换成功"}, "finish_reason": "stop"}],
            "usage": {"total_tokens": 2},
        })

    mock_client(monkeypatch, handler)
    result = asyncio.run(adapters._call_model(provider_dict(), "test-model", "hi"))
    assert result["content"] == "切换成功"
    assert "max_tokens" in seen[0]
    assert "max_completion_tokens" in seen[1]


def test_400_auto_switches_back_to_max_tokens(monkeypatch):
    seen = []

    def handler(request):
        body = json.loads(request.content)
        seen.append(body)
        if "max_completion_tokens" in body:
            return httpx.Response(400, json={"error": {"message": (
                "max_completion_tokens is not supported by this gateway; use max_tokens"
            )}})
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "反向切换"}, "finish_reason": "stop"}],
            "usage": {"total_tokens": 2},
        })

    mock_client(monkeypatch, handler)
    provider = provider_dict(compat_json=json.dumps({"max_tokens_param": "max_completion_tokens"}))
    result = asyncio.run(adapters._call_model(provider, "gpt-4o", "hi"))
    assert result["content"] == "反向切换"
    assert "max_completion_tokens" in seen[0]
    assert "max_tokens" in seen[1]


# ---------------------------------------------------------------------------
# reasoning_content / thinking_delta 分流
# ---------------------------------------------------------------------------

def test_stream_reasoning_content_separated_from_answer(monkeypatch):
    def handler(request):
        return sse_response([
            '{"choices":[{"delta":{"reasoning_content":"先思考"}}]}',
            '{"choices":[{"delta":{"reasoning":"再想想"}}]}',
            '{"choices":[{"delta":{"content":"答案"}}]}',
            '{"choices":[{"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":3,"completion_tokens":4}}',
        ])

    mock_client(monkeypatch, handler)
    deltas, thoughts = [], []

    async def on_delta(text):
        deltas.append(text)

    async def on_thinking(text):
        thoughts.append(text)

    result = asyncio.run(adapters._call_model(
        provider_dict(), "deepseek-reasoner", "hi",
        on_delta=on_delta, on_thinking=on_thinking,
    ))
    assert result["content"] == "答案"
    assert result["thinking"] == "先思考再想想"
    assert result["thinking_chars"] == 6
    assert deltas == ["答案"]
    assert thoughts == ["先思考", "再想想"]
    assert "先思考" not in result["content"]


def test_anthropic_thinking_delta_not_in_content(monkeypatch):
    def handler(request):
        return sse_response([
            '{"type":"content_block_delta","delta":{"type":"thinking_delta","thinking":"推理中"}}',
            '{"type":"content_block_delta","delta":{"type":"text_delta","text":"结论"}}',
            '{"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":2}}',
        ])

    mock_client(monkeypatch, handler)
    deltas, thoughts = [], []

    async def on_delta(text):
        deltas.append(text)

    async def on_thinking(text):
        thoughts.append(text)

    result = asyncio.run(adapters._call_model(
        provider_dict(kind="anthropic"), "claude-3-5-sonnet", "hi",
        on_delta=on_delta, on_thinking=on_thinking,
    ))
    assert result["content"] == "结论"
    assert result["thinking"] == "推理中"
    assert deltas == ["结论"] and thoughts == ["推理中"]


def test_non_stream_reasoning_content_parsed(monkeypatch):
    def handler(request):
        return httpx.Response(200, json={
            "choices": [{
                "message": {"content": "最终答案", "reasoning_content": "内部推理"},
                "finish_reason": "stop",
            }],
            "usage": {"total_tokens": 3},
        })

    mock_client(monkeypatch, handler)
    result = asyncio.run(adapters._call_model(provider_dict(), "deepseek-reasoner", "hi"))
    assert result["content"] == "最终答案"
    assert result["thinking"] == "内部推理"
    assert result["thinking_chars"] == 4


def test_anthropic_non_stream_thinking_blocks(monkeypatch):
    def handler(request):
        return httpx.Response(200, json={
            "content": [
                {"type": "thinking", "thinking": "内部推理"},
                {"type": "text", "text": "可见答案"},
            ],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 1, "output_tokens": 2},
        })

    mock_client(monkeypatch, handler)
    result = asyncio.run(adapters._call_model(
        provider_dict(kind="anthropic"), "claude-3-5-sonnet", "hi",
    ))
    assert result["content"] == "可见答案"
    assert result["thinking"] == "内部推理"


# ---------------------------------------------------------------------------
# 内嵌错误帧 / 用量记账
# ---------------------------------------------------------------------------

def test_sse_embedded_error_frame_raises_clear_error(monkeypatch):
    def handler(request):
        return sse_response([
            '{"error":{"message":"quota exhausted for sk-embedded-secret-1234"}}',
        ])

    mock_client(monkeypatch, handler)

    async def on_delta(text):
        raise AssertionError("不应收到正文增量")

    with pytest.raises(ValueError) as excinfo:
        asyncio.run(adapters._call_model(provider_dict(), "test-model", "hi", on_delta=on_delta))
    message = str(excinfo.value)
    assert "模型服务返回错误" in message or "quota" in message
    assert "sk-embedded-secret-1234" not in message


def test_usage_recorded_before_empty_content_error(client, monkeypatch):
    user_id = client.get("/api/auth/me").json()["id"]

    def handler(request):
        return httpx.Response(200, json={
            "choices": [{"message": {"content": ""}, "finish_reason": "length"}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 7},
        })

    mock_client(monkeypatch, handler)
    with pytest.raises(ValueError):
        asyncio.run(adapters._call_model(provider_dict(), "deepseek-chat", "hi", user_id=user_id))
    with db.connection() as con:
        row = con.execute(
            "SELECT input_tokens, output_tokens, job_id FROM ai_usage "
            "WHERE user_id=? ORDER BY id DESC LIMIT 1",
            (user_id,),
        ).fetchone()
    assert row is not None
    assert row["input_tokens"] == 5 and row["output_tokens"] == 7


# ---------------------------------------------------------------------------
# caps 生效
# ---------------------------------------------------------------------------

def _saved_provider_row(client, monkeypatch):
    saved = save_provider(client, models=["vision-model"])
    user_id = client.get("/api/auth/me").json()["id"]
    row = providers._owned("ai_providers", saved["id"], user_id)
    model_row = saved["model_details"][0]
    return row, model_row["id"]


def test_caps_stream_false_forces_non_stream(client, monkeypatch):
    provider, model_row_id = _saved_provider_row(client, monkeypatch)
    assert client.patch(
        f"/api/models/{model_row_id}", json={"caps": {"stream": False}},
    ).status_code == 200
    captured = {}

    def handler(request):
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "非流式返回"}, "finish_reason": "stop"}],
            "usage": {"total_tokens": 1},
        })

    mock_client(monkeypatch, handler)
    deltas = []

    async def on_delta(text):
        deltas.append(text)

    result = asyncio.run(adapters._call_model(provider, "vision-model", "hi", on_delta=on_delta))
    assert result["content"] == "非流式返回"
    assert "stream" not in captured["body"]
    assert deltas == []


def test_caps_json_false_skips_response_format(client, monkeypatch):
    provider, model_row_id = _saved_provider_row(client, monkeypatch)
    assert client.patch(
        f"/api/models/{model_row_id}", json={"caps": {"json": False}},
    ).status_code == 200
    captured = {}

    def handler(request):
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "纯文本"}, "finish_reason": "stop"}],
            "usage": {"total_tokens": 1},
        })

    mock_client(monkeypatch, handler)
    result = asyncio.run(adapters._call_model(provider, "vision-model", "hi", json_output=True))
    assert result["content"] == "纯文本"
    assert "response_format" not in captured["body"]


def test_caps_vision_false_rejects_images_with_chinese_message(client, monkeypatch):
    provider, model_row_id = _saved_provider_row(client, monkeypatch)
    assert client.patch(
        f"/api/models/{model_row_id}", json={"caps": {"vision": False}},
    ).status_code == 200
    with pytest.raises(ValueError) as excinfo:
        asyncio.run(adapters._call_model(
            provider, "vision-model", "看图", images=[{"mime": "image/png", "data": "AAAA"}],
        ))
    assert "视觉" in str(excinfo.value)


# ---------------------------------------------------------------------------
# 能力推断规则 / 身份与同底座模型
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("model,vision,reasoning", [
    ("o3-mini", True, True),
    ("o1", True, True),
    ("o4-mini", True, True),
    ("gpt-4o", True, False),
    ("gpt-5", True, False),
    ("claude-3-haiku", True, False),
    ("claude-3-5-sonnet", True, True),
    ("gemini-2.0-flash", True, False),
    ("gemini-2.5-pro", True, True),
    ("glm-4-flash", False, False),
    ("glm-z1-air", False, True),
    ("kimi-k2-thinking", False, True),
    ("deepseek-r1", False, True),
    ("deepseek-chat", False, False),
])
def test_infer_capabilities_rules(model, vision, reasoning):
    caps = ai.infer_capabilities(model)
    assert caps["vision"] is vision
    assert caps["reasoning"] is reasoning


def test_identity_distinguishes_port_and_path_scope():
    base = {"base_url": "https://127.0.0.1:8000/v1"}
    other_port = {"base_url": "https://127.0.0.1:8001/v1"}
    other_path = {"base_url": "https://127.0.0.1:8000/v2"}
    same = {"base_url": "https://127.0.0.1:8000/v1"}
    assert providers._identity(base, "m") != providers._identity(other_port, "m")
    assert providers._identity(base, "m") != providers._identity(other_path, "m")
    assert providers._identity(base, "m") == providers._identity(same, "m")


@pytest.mark.parametrize("raw,family", [
    ("openai/gpt-4o", "gpt-4o"),
    ("gpt-4o:free", "gpt-4o"),
    ("gpt-4o:nitro", "gpt-4o"),
    ("Pro/deepseek-ai/DeepSeek-V3", "deepseek-v3"),
    ("google/gemini-2.5-pro", "gemini-2.5-pro"),
    ("anthropic/claude-3-5-sonnet", "claude-3-5-sonnet"),
    ("gpt-4o-2024-11-20", "gpt-4o"),
    ("models/gemini-2.0-flash", "gemini-2.0-flash"),
])
def test_canonical_model_family(raw, family):
    assert providers._canonical_model_family(raw) == family


def test_cross_relay_same_model_marker():
    official = {"base_url": "https://api.openai.com/v1"}
    relay = {"base_url": "https://openrouter.ai/api/v1"}
    assert providers.cross_relay_same_model(official, "gpt-4o", relay, "openai/gpt-4o") is True
    # 同域名同模型不算跨中转站，但仍是同底座
    assert providers.cross_relay_same_model(official, "gpt-4o", official, "gpt-4o") is False
    risk = providers.identity_risk(official, "gpt-4o", relay, "openai/gpt-4o")
    assert risk["same_model_family"] is True
    assert risk["cross_relay_same_model"] is True
    assert "同一底座模型" in risk["notice"]
    assert providers.cross_relay_same_model(official, "gpt-4o", relay, "claude-3-5-sonnet") is False


# ---------------------------------------------------------------------------
# Gemini 原生协议
# ---------------------------------------------------------------------------

def test_gemini_native_generate_content(monkeypatch):
    captured = {}

    def handler(request):
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={
            "candidates": [{
                "content": {"parts": [{"text": "原生返回"}]},
                "finishReason": "STOP",
            }],
            "usageMetadata": {"promptTokenCount": 2, "candidatesTokenCount": 3, "totalTokenCount": 5},
        })

    mock_client(monkeypatch, handler)
    provider = provider_dict(
        kind="gemini_native", auth_mode="query_key",
        base_url="https://generativelanguage.googleapis.com/v1beta",
    )
    result = asyncio.run(adapters._call_model(provider, "gemini-2.0-flash", "你好"))
    assert result["content"] == "原生返回"
    assert result["usage"]["prompt_tokens"] == 2
    assert ":generateContent" in captured["url"]
    assert "key=" + KEY in captured["url"]
    assert captured["body"]["generationConfig"]["maxOutputTokens"] == 5000


def test_gemini_native_stream_sse(monkeypatch):
    def handler(request):
        return sse_response([
            '{"candidates":[{"content":{"parts":[{"text":"原生"}]}}]}',
            '{"candidates":[{"content":{"parts":[{"text":"流式"}]}}]}',
            '{"candidates":[{"finishReason":"STOP"}],"usageMetadata":{"promptTokenCount":1,"candidatesTokenCount":2}}',
        ])

    mock_client(monkeypatch, handler)
    deltas = []

    async def on_delta(text):
        deltas.append(text)

    provider = provider_dict(
        kind="gemini_native", auth_mode="query_key",
        base_url="https://generativelanguage.googleapis.com/v1beta",
    )
    result = asyncio.run(adapters._call_model(
        provider, "gemini-2.0-flash", "你好", on_delta=on_delta,
    ))
    assert result["content"] == "原生流式"
    assert deltas == ["原生", "流式"]


def test_local_policy_has_no_transport_override():
    assert "transport" not in netguard.client_kwargs("http://127.0.0.1:11434/v1/chat/completions")
