"""Provider connections, Fernet encryption, model capabilities, task defaults, usage & allowlist.

自定义厂商支持：
* 分层 URL 策略（``local_open`` / ``public_https`` / ``allowlist``，见 :mod:`app.ai.netguard`）；
* 鉴权方式 ``auth_mode``、自定义请求头（加密存储）与协议兼容微调 ``compat_json``；
* 「保存前」探测接口 ``fetch-models-preview`` / ``probe``，无需先落库即可拉模型、测连通；
* 账号级 Embedding 配置（``ai_embeddings_config``）与向量接口自测。
"""
from datetime import datetime, timedelta, timezone
import ipaddress
import json
import os
import re
import tempfile
from urllib.parse import parse_qsl, urlsplit

from cryptography.fernet import Fernet, InvalidToken
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .. import db
from ..auth import current_user, require_admin

router = APIRouter(prefix="/api", tags=["AI providers"])

PRESETS = [
    {"name": "OpenAI", "kind": "openai", "base_url": "https://api.openai.com/v1", "models": []},
    {"name": "DeepSeek", "kind": "openai", "base_url": "https://api.deepseek.com/v1", "models": []},
    {"name": "通义千问", "kind": "openai", "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1", "models": []},
    {"name": "硅基流动", "kind": "openai", "base_url": "https://api.siliconflow.cn/v1", "models": []},
    {"name": "智谱 GLM", "kind": "openai", "base_url": "https://open.bigmodel.cn/api/paas/v4", "models": []},
    {"name": "月之暗面 Kimi", "kind": "openai", "base_url": "https://api.moonshot.cn/v1", "models": []},
    {"name": "火山方舟（豆包）", "kind": "openai", "base_url": "https://ark.cn-beijing.volces.com/api/v3", "models": []},
    {"name": "OpenRouter", "kind": "openai", "base_url": "https://openrouter.ai/api/v1", "models": []},
    {"name": "Claude", "kind": "anthropic", "base_url": "https://api.anthropic.com/v1", "models": []},
    {"name": "Gemini", "kind": "gemini", "base_url": "https://generativelanguage.googleapis.com/v1beta/openai", "models": []},
]

PROVIDER_KINDS = ("openai", "anthropic", "gemini", "gemini_native", "azure_openai")
_KIND_PATTERN = "^(openai|anthropic|gemini|gemini_native|azure_openai)$"
AUTH_MODES = ("auto", "bearer", "x-api-key", "api-key", "query_key", "none")
_AUTH_PATTERN = "^(auto|bearer|x-api-key|api-key|query_key|none)$"

MAX_EXTRA_HEADERS = 8
MAX_HEADER_NAME = 64
MAX_HEADER_VALUE = 1024
FORBIDDEN_HEADERS = frozenset({
    "host", "content-length", "connection", "cookie", "transfer-encoding",
    "upgrade", "proxy-authorization", "te", "trailer",
})
_HEADER_NAME_RE = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")

MAX_COMPAT_TIMEOUT = 600
MIN_COMPAT_TIMEOUT = 15
MAX_TOKENS_PARAMS = ("auto", "max_tokens", "max_completion_tokens")

# 「保存前」探测接口的用户级频控：每分钟 10 次，防止被当作 SSRF 扫描器。
PROBE_RATE_LIMIT = 10
PROBE_RATE_WINDOW_SECONDS = 60

# ---- 内置能力推断 ---------------------------------------------------------
# 规则顺序即优先级：更具体/更新的规则必须在前，o1/o3/o4-mini 使用词边界匹配，
# 避免被 gpt-4o 等更泛的规则吞掉；claude-haiku / gemini-flash / glm-flash 不误标
# reasoning，kimi-k2-thinking 不误标 vision。
MODEL_CAP_RULES = [
    (("o1", "o3", "o4-mini", "o4"), {
        "vision": True, "json": True, "reasoning": True, "stream": True, "context": 200000,
    }),
    (("gpt-4o", "gpt-4.1", "gpt-4.5", "gpt-5", "gpt-4-turbo"), {
        "vision": True, "json": True, "reasoning": False, "stream": True, "context": 128000,
    }),
    (("claude-3-haiku", "claude-3.5-haiku", "claude-haiku"), {
        "vision": True, "json": True, "reasoning": False, "stream": True, "context": 200000,
    }),
    (("claude-3", "claude-4", "claude-sonnet", "claude-opus"), {
        "vision": True, "json": True, "reasoning": True, "stream": True, "context": 200000,
    }),
    (("gemini-flash", "gemini-1.5-flash", "gemini-2.0-flash", "gemini-2.5-flash",
      "gemini-3-flash", "gemini-2.0-flash-lite"), {
        "vision": True, "json": True, "reasoning": False, "stream": True, "context": 1000000,
    }),
    (("gemini-1.5", "gemini-2", "gemini-3", "gemini-4"), {
        "vision": True, "json": True, "reasoning": True, "stream": True, "context": 1000000,
    }),
    (("deepseek-reasoner", "deepseek-r1"), {
        "vision": False, "json": True, "reasoning": True, "stream": True, "context": 64000,
    }),
    (("deepseek-chat", "deepseek-v3"), {
        "vision": False, "json": True, "reasoning": False, "stream": True, "context": 64000,
    }),
    (("qwen-vl", "qwen2-vl", "qwen2.5-vl", "qvq"), {
        "vision": True, "json": True, "reasoning": False, "stream": True, "context": 128000,
    }),
    (("qwen",), {
        "vision": False, "json": True, "reasoning": False, "stream": True, "context": 128000,
    }),
    (("glm-4v", "glm-4.5v", "glm-4.6v"), {
        "vision": True, "json": True, "reasoning": False, "stream": True, "context": 128000,
    }),
    (("glm-4-flash", "glm-4.5-flash", "glm-flash"), {
        "vision": False, "json": True, "reasoning": False, "stream": True, "context": 128000,
    }),
    (("glm-z1",), {
        "vision": False, "json": True, "reasoning": True, "stream": True, "context": 128000,
    }),
    (("glm-4", "glm-4.5", "glm-4.6"), {
        "vision": False, "json": True, "reasoning": True, "stream": True, "context": 128000,
    }),
    (("kimi-k2-thinking", "kimi-thinking"), {
        "vision": False, "json": True, "reasoning": True, "stream": True, "context": 128000,
    }),
    (("moonshot", "kimi"), {
        "vision": False, "json": True, "reasoning": False, "stream": True, "context": 128000,
    }),
    (("doubao-vision", "doubao-1-5-vision", "seed-1-6", "doubao-seed"), {
        "vision": True, "json": True, "reasoning": True, "stream": True, "context": 128000,
    }),
]

# 这些短 token 必须按词边界匹配（否则 gpt-4o 里的 "o1"/"o4" 会被误命中）。
_BOUNDARY_TOKENS = frozenset({"o1", "o3", "o4", "o4-mini"})
_TOKEN_PATTERNS = {
    token: re.compile(rf"(?<![a-z0-9]){re.escape(token)}(?![a-z0-9])")
    for token in _BOUNDARY_TOKENS
}
_VISION_MARKERS = ("vision", "-vl", "vl-", "4v", "omni", "multimodal", "image")
_REASONING_MARKERS = ("reasoner", "-r1", "thinking", "qwq", "deepseek-r1")


def _rule_hit(low: str, pattern: str) -> bool:
    token_pattern = _TOKEN_PATTERNS.get(pattern)
    if token_pattern is not None:
        return bool(token_pattern.search(low))
    return pattern in low


def infer_capabilities(model_id: str) -> dict:
    """按内置启发式表推断模型能力；用户可在 ai_models.caps_json 覆盖。"""
    low = str(model_id or "").strip().lower()
    caps = {"vision": False, "json": True, "reasoning": False, "stream": True, "context": 64000}
    for patterns, spec in MODEL_CAP_RULES:
        if any(_rule_hit(low, pattern) for pattern in patterns):
            caps = dict(spec)
            break
    if any(marker in low for marker in _VISION_MARKERS):
        caps["vision"] = True
    if any(marker in low for marker in _REASONING_MARKERS) or any(
        pattern.search(low) for pattern in _TOKEN_PATTERNS.values()
    ):
        caps["reasoning"] = True
    # flash 系列的通用兜底：gemini-*-flash / glm-*-flash 不按推理模型处理。
    if caps.get("reasoning") and "flash" in low and not any(
        marker in low for marker in ("thinking", "reasoner")
    ):
        caps["reasoning"] = False
    return caps


def _json(value):
    return json.dumps(value, ensure_ascii=False)


def _cipher():
    path = db.DATA_DIR / "api-key.key"
    try:
        return Fernet(path.read_bytes())
    except FileNotFoundError:
        path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        candidate = stream.name
        stream.write(Fernet.generate_key())
    try:
        # Publish the fully written key without replacing a key another process created.
        os.link(candidate, path)
    except FileExistsError:
        pass
    finally:
        os.unlink(candidate)
    return Fernet(path.read_bytes())


def _decrypt_key(encrypted) -> str:
    if not str(encrypted or "").strip():
        return ""
    try:
        return _cipher().decrypt(str(encrypted or "").encode()).decode()
    except InvalidToken:
        raise HTTPException(422, "API 密钥无法解密，请重新保存连接") from None


def _owned(table, resource_id, user_id):
    if table not in {"ai_providers", "ai_uploads", "ai_documents", "ai_notes", "ai_jobs", "ai_models", "prompt_templates"}:
        raise ValueError("invalid table")
    with db.connection() as con:
        row = con.execute(f"SELECT * FROM {table} WHERE id=? AND user_id=?", (resource_id, user_id)).fetchone()
    if row is None:
        raise HTTPException(404, "未找到该记录")
    return dict(row)


def _row_get(row, key, default=None):
    try:
        value = row[key]
    except (KeyError, IndexError, TypeError):
        return default
    return default if value is None else value


def _normalize_allowed_entry(raw: str) -> str:
    entry = str(raw or "").strip().lower()
    if not entry:
        return ""
    if "://" in entry:
        try:
            entry = (urlsplit(entry).hostname or "").lower()
        except ValueError:
            return ""
    entry = entry.split("/", 1)[0].strip().rstrip(".")
    return entry


def _allowed_hostnames():
    """预设厂商 + 环境变量（支持 ``*.example.com`` 通配符）+ 数据库白名单。"""
    hosts = set()
    for preset in PRESETS:
        host = urlsplit(preset["base_url"]).hostname
        if host:
            hosts.add(host.lower())
    for raw in os.environ.get("STUDY_AI_ALLOWED_HOSTS", "").split(","):
        entry = _normalize_allowed_entry(raw)
        if entry:
            hosts.add(entry)
    try:
        with db.connection() as con:
            for row in con.execute("SELECT hostname FROM ai_allowed_hosts"):
                entry = _normalize_allowed_entry(row[0])
                if entry:
                    hosts.add(entry)
    except Exception:
        pass
    return hosts


def _validate_url(value, *, resolve=False):
    """按 STUDY_AI_URL_POLICY 校验并返回规范化地址；非法地址 422。"""
    from . import netguard

    policy = netguard.url_policy()
    allowed = _allowed_hostnames() if policy == "allowlist" else None
    return netguard.validate_base_url(
        value, resolve=resolve, policy=policy, allowed_hosts=allowed,
    )


# ---------------------------------------------------------------------------
# 鉴权 / 请求头 / 兼容微调
# ---------------------------------------------------------------------------

def _normalize_auth_mode(value, *, default="auto") -> str:
    mode = str(value or "").strip().lower() or default
    if mode not in AUTH_MODES:
        raise HTTPException(422, "不支持的鉴权方式，可选 auto/bearer/x-api-key/api-key/query_key/none")
    return mode


def resolve_auth_mode(provider) -> str:
    """按 auth_mode 显式设置或协议默认值决定实际鉴权方式。"""
    mode = str(_row_get(provider, "auth_mode", "auto") or "auto").strip().lower()
    if mode not in AUTH_MODES:
        mode = "auto"
    if mode != "auto":
        return mode
    kind = str(_row_get(provider, "kind", "openai") or "openai").strip().lower()
    if kind == "anthropic":
        return "x-api-key"
    if kind == "azure_openai":
        return "api-key"
    if kind == "gemini_native":
        return "query_key"
    return "bearer"


def _normalize_extra_headers(value) -> dict[str, str]:
    """校验自定义请求头：最多 8 条、长度受限、禁止覆盖 Host/Content-Length 等。"""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise HTTPException(422, "自定义请求头必须是键值对象")
    if len(value) > MAX_EXTRA_HEADERS:
        raise HTTPException(422, f"自定义请求头最多 {MAX_EXTRA_HEADERS} 条")
    headers: dict[str, str] = {}
    seen: set[str] = set()
    for raw_name, raw_value in value.items():
        name = str(raw_name or "").strip()
        if not name or len(name) > MAX_HEADER_NAME or not _HEADER_NAME_RE.match(name):
            raise HTTPException(422, f"请求头名称无效：{name[:32]}")
        lowered = name.lower()
        if lowered in FORBIDDEN_HEADERS:
            raise HTTPException(422, f"请求头 {name} 由系统管理，不允许自定义")
        if lowered in seen:
            raise HTTPException(422, f"请求头 {name} 重复")
        seen.add(lowered)
        if isinstance(raw_value, (dict, list, tuple)):
            raise HTTPException(422, f"请求头 {name} 的值必须是字符串")
        text = str(raw_value if raw_value is not None else "").strip()
        if "\n" in text or "\r" in text:
            raise HTTPException(422, f"请求头 {name} 的值不能包含换行")
        if len(text) > MAX_HEADER_VALUE:
            raise HTTPException(422, f"请求头 {name} 的值过长")
        headers[name] = text
    return headers


def _encrypt_extra_headers(headers: dict[str, str]) -> str:
    if not headers:
        return ""
    payload = _json(headers).encode("utf-8")
    return _cipher().encrypt(payload).decode()


def provider_extra_headers(provider) -> dict[str, str]:
    """读取并解密连接的自定义请求头（无则空 dict）。"""
    encrypted = str(_row_get(provider, "extra_headers_enc", "") or "")
    if not encrypted:
        return {}
    try:
        payload = _cipher().decrypt(encrypted.encode()).decode()
        parsed = json.loads(payload)
    except (InvalidToken, ValueError):
        return {}
    if not isinstance(parsed, dict):
        return {}
    return {str(k): str(v) for k, v in parsed.items()}


def normalize_compat(value) -> dict:
    """校验并规范化连接级协议兼容选项。"""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise HTTPException(422, "协议兼容选项必须是对象")
    compat: dict = {}
    for key, raw in value.items():
        name = str(key or "").strip()
        if name == "max_tokens_param":
            param = str(raw or "auto").strip().lower()
            if param not in MAX_TOKENS_PARAMS:
                raise HTTPException(422, "max_tokens_param 只支持 auto/max_tokens/max_completion_tokens")
            compat["max_tokens_param"] = param
        elif name == "timeout_seconds":
            if isinstance(raw, bool) or not isinstance(raw, (int, float)):
                raise HTTPException(422, "timeout_seconds 必须是 15–600 之间的数字")
            seconds = int(raw)
            if not MIN_COMPAT_TIMEOUT <= seconds <= MAX_COMPAT_TIMEOUT:
                raise HTTPException(422, "timeout_seconds 必须在 15–600 秒之间")
            compat["timeout_seconds"] = seconds
        elif name == "use_proxy_env":
            if not isinstance(raw, bool):
                raise HTTPException(422, "use_proxy_env 必须是布尔值")
            compat["use_proxy_env"] = raw
        elif name == "api_version":
            version = str(raw or "").strip()
            if len(version) > 80 or any(character in version for character in "\r\n\t "):
                raise HTTPException(422, "api-version 格式无效")
            if version:
                compat["api_version"] = version
        elif name == "provider_kind":
            kind = str(raw or "").strip()
            if len(kind) > 40:
                raise HTTPException(422, "provider_kind 过长")
            if kind:
                compat["provider_kind"] = kind
        elif name == "strip_reasoning_for_budget":
            if not isinstance(raw, bool):
                raise HTTPException(422, "strip_reasoning_for_budget 必须是布尔值")
            compat["strip_reasoning_for_budget"] = raw
    return compat


def provider_compat(provider) -> dict:
    raw = _row_get(provider, "compat_json", "{}")
    if isinstance(raw, dict):
        return normalize_compat(raw) if raw else {}
    try:
        parsed = json.loads(str(raw or "{}"))
    except ValueError:
        return {}
    if not isinstance(parsed, dict):
        return {}
    try:
        return normalize_compat(parsed)
    except HTTPException:
        return {}


def _provider_use_proxy(provider) -> bool:
    from . import netguard

    compat = provider_compat(provider)
    return bool(compat.get("use_proxy_env")) and netguard.url_policy() == "local_open"


def _encrypt_key(key: str) -> tuple[str, str]:
    if not key:
        return "", "未设置（无鉴权）"
    encrypted = _cipher().encrypt(key.encode("utf-8")).decode()
    return encrypted, ("已加密 · ****" + key[-4:] if len(key) > 8 else "已加密")


def _clean_api_key(value, auth_mode: str, *, required: bool) -> str:
    raw = "" if value is None else str(value)
    if "\n" in raw or "\r" in raw:
        raise HTTPException(422, "API Key 不能包含换行")
    key = raw.strip()
    if len(key) > 1000:
        raise HTTPException(422, "API Key 过长")
    if not key and required and auth_mode != "none":
        raise HTTPException(
            422,
            "API Key 不能为空；本地无鉴权服务请在高级选项把鉴权方式显式设为「无鉴权（none）」",
        )
    return key


# ---------------------------------------------------------------------------
# 模型同步 / 连接公开视图 / 身份
# ---------------------------------------------------------------------------

def _sync_provider_models(con, provider_id: int, user_id: int, models: list[str]):
    existing = {
        r["model_id"]: dict(r)
        for r in con.execute("SELECT * FROM ai_models WHERE provider_id=? AND user_id=?", (provider_id, user_id))
    }
    keep = set(models)
    for old_id in list(existing.keys()):
        if old_id not in keep:
            con.execute("DELETE FROM ai_models WHERE provider_id=? AND user_id=? AND model_id=?", (provider_id, user_id, old_id))
    for m in models:
        if m not in existing:
            con.execute(
                "INSERT INTO ai_models(provider_id,user_id,model_id,display_name,caps_json,enabled) VALUES (?,?,?,?,?,1)",
                (provider_id, user_id, m, m, _json(infer_capabilities(m))),
            )


def _provider_public(row, model_rows=None):
    models = json.loads(_row_get(row, "models_json", "[]") or "[]")
    if model_rows is None:
        with db.connection() as con:
            rows = con.execute(
                "SELECT * FROM ai_models WHERE provider_id=? AND user_id=? ORDER BY id",
                (row["id"], row["user_id"]),
            ).fetchall()
            if not rows and models:
                _sync_provider_models(con, row["id"], row["user_id"], models)
                rows = con.execute(
                    "SELECT * FROM ai_models WHERE provider_id=? AND user_id=? ORDER BY id",
                    (row["id"], row["user_id"]),
                ).fetchall()
            model_rows = [dict(r) for r in rows]
    meta_by_id = {m["model_id"]: m for m in model_rows}
    model_details = []
    for m in models:
        rec = meta_by_id.get(m)
        if rec:
            model_details.append({
                "id": rec["id"],
                "model_id": m,
                "display_name": rec["display_name"] or m,
                "caps": json.loads(rec["caps_json"] or "{}") or infer_capabilities(m),
                "input_price": rec["input_price"],
                "output_price": rec["output_price"],
                "enabled": bool(rec["enabled"]),
            })
        else:
            model_details.append({
                "id": None, "model_id": m, "display_name": m,
                "caps": infer_capabilities(m), "input_price": None, "output_price": None, "enabled": True,
            })
    try:
        header_names = sorted(provider_extra_headers(row).keys())
    except HTTPException:
        header_names = []
    encrypted = str(_row_get(row, "encrypted_key", "") or "")
    key_hint = str(_row_get(row, "key_hint", "") or "")
    return {
        "id": row["id"], "name": row["name"], "kind": row["kind"],
        "base_url": row["base_url"], "key_hint": key_hint,
        "models": models, "model_details": model_details,
        "auth_mode": str(_row_get(row, "auth_mode", "auto") or "auto"),
        "extra_header_names": header_names,
        "has_api_key": bool(encrypted),
        "compat": provider_compat(row),
    }


def _selected_provider(user_id, provider_id, model):
    row = _owned("ai_providers", provider_id, user_id)
    if model not in json.loads(row["models_json"]):
        raise HTTPException(422, "该模型未包含在所选连接中，请先在设置中添加")
    return row


def _identity(provider, model):
    """连接身份：同域 + 同路径前缀 + 同模型才算「同一调用方」。"""
    base = str(_row_get(provider, "base_url", "") or "")
    host = ""
    path_scope = ""
    port = None
    try:
        parts = urlsplit(base)
        host = (parts.hostname or "").lower()
        port = parts.port
        path_scope = "/".join(segment for segment in (parts.path or "").lower().split("/") if segment)
    except ValueError:
        host = base.strip().lower()
    netloc = f"{host}:{port}" if port else host
    return (netloc, path_scope, str(model or "").strip().lower())


_MODEL_ROUTE_PREFIXES = (
    "openai", "anthropic", "google", "deepseek", "deepseek-ai", "meta-llama", "meta",
    "qwen", "alibaba", "mistralai", "mistral", "x-ai", "xai", "moonshotai", "moonshot",
    "z-ai", "zhipuai", "zhipu", "thudm", "nvidia", "microsoft", "cohere", "ai21",
    "amazon", "together", "togethercomputer", "openrouter", "perplexity", "baidu",
    "bytedance", "doubao", "minimax", "01-ai", "internlm", "models", "pro",
)
_MODEL_TAG_SUFFIX_RE = re.compile(
    r":(?:free|nitro|latest|online|extended|thinking|beta|preview|fp8|fp16)$",
    re.IGNORECASE,
)
_MODEL_DATE_SUFFIX_RE = re.compile(r"-(?:20\d{2}-\d{2}-\d{2}|\d{8})$")


def _canonical_model_family(model) -> str:
    """剥离聚合路由前缀与 ``:free``/``:nitro`` 等后缀，得到跨中转站的同族模型名。"""
    value = str(model or "").strip().lower()
    if not value:
        return ""
    while True:
        stripped = _MODEL_TAG_SUFFIX_RE.sub("", value)
        if stripped == value:
            break
        value = stripped
    changed = True
    while changed:
        changed = False
        for prefix in _MODEL_ROUTE_PREFIXES:
            marker = prefix + "/"
            if value.startswith(marker):
                value = value[len(marker):]
                changed = True
                break
    value = _MODEL_DATE_SUFFIX_RE.sub("", value)
    if value.endswith("-latest"):
        value = value[:-len("-latest")]
    return value.strip().strip("/")


def cross_relay_same_model(provider_a, model_a, provider_b, model_b) -> bool:
    """两个连接是否在调用同一底座模型（同族模型 + 不同域名/端口）。"""
    family_a = _canonical_model_family(model_a)
    family_b = _canonical_model_family(model_b)
    if not family_a or family_a != family_b:
        return False
    return _identity(provider_a, model_a)[0] != _identity(provider_b, model_b)[0]


def identity_risk(provider_a, model_a, provider_b, model_b) -> dict:
    """交叉核验身份风险：供 verify 层降级标注使用（本模块只产出标记）。"""
    family_a = _canonical_model_family(model_a)
    family_b = _canonical_model_family(model_b)
    same_family = bool(family_a) and family_a == family_b
    same_endpoint = _identity(provider_a, model_a) == _identity(provider_b, model_b)
    cross_relay = same_family and not same_endpoint and (
        _identity(provider_a, model_a)[0] != _identity(provider_b, model_b)[0]
    )
    notice = ""
    if cross_relay:
        notice = (
            "两个连接调用了同一底座模型（可能只是不同中转站），"
            "不计入高可信独立交叉核验"
        )
    return {
        "same_model_family": same_family,
        "same_endpoint": same_endpoint,
        "cross_relay_same_model": cross_relay,
        "notice": notice,
    }


# ---------------------------------------------------------------------------
# 「保存前」探测：限流与上下文
# ---------------------------------------------------------------------------

def _probe_rate_limit(user_id: int, scope: str) -> None:
    """用户维度原子限流：每分钟 PROBE_RATE_LIMIT 次，防止探测接口被当扫描器。"""
    now = datetime.now(timezone.utc)
    cutoff = (now - timedelta(seconds=PROBE_RATE_WINDOW_SECONDS)).isoformat()
    bucket = f"{scope}:{user_id}"
    with db.connection() as con:
        db.write_lock(con, "auth-rate")
        con.execute("DELETE FROM auth_rate_events WHERE at < ?", ((now - timedelta(days=1)).isoformat(),))
        count = con.execute(
            "SELECT COUNT(*) FROM auth_rate_events WHERE bucket=? AND at>=?", (bucket, cutoff),
        ).fetchone()[0]
        if count >= PROBE_RATE_LIMIT:
            raise HTTPException(429, "探测请求过于频繁（每分钟最多 10 次），请稍后再试")
        con.execute(
            "INSERT INTO auth_rate_events(bucket,kind,at) VALUES (?,?,?)",
            (bucket, scope, now.isoformat()),
        )


def _preview_context(data, user) -> dict:
    """解析「保存前」探测入参：地址/请求头用表单最新值，编辑态可复用已存 Key。"""
    from . import netguard

    normalization_notes = list(netguard.normalize_base_url(data.base_url).get("notes") or [])
    url = _validate_url(data.base_url)
    auth_mode = _normalize_auth_mode(data.auth_mode)
    extra_headers = _normalize_extra_headers(data.extra_headers)
    compat = normalize_compat(data.compat)
    if data.api_key is not None:
        api_key = _clean_api_key(data.api_key, auth_mode, required=False)
    elif data.provider_id:
        row = _owned("ai_providers", data.provider_id, user["id"])
        api_key = _decrypt_key(_row_get(row, "encrypted_key", ""))
    else:
        api_key = ""
    return {
        "base_url": url,
        "kind": data.kind,
        "auth_mode": auth_mode,
        "api_key": api_key,
        "extra_headers": extra_headers,
        "compat": compat,
        "provider_id": data.provider_id,
        "notes": normalization_notes,
    }


def _models_endpoint(base_url: str, kind: str, compat: dict) -> tuple[str, dict]:
    """模型列表端点：OpenAI 兼容 / Gemini 原生 / Azure。"""
    from . import netguard

    if kind == "gemini_native":
        return netguard.join_endpoint(base_url, "/models"), {}
    endpoint = netguard.join_endpoint(base_url, "/models")
    params = {}
    if kind == "azure_openai":
        api_version = compat.get("api_version")
        if api_version and not any(key == "api-version" for key, _ in parse_qsl(urlsplit(endpoint).query)):
            params["api-version"] = api_version
    return endpoint, params


def _model_ids_from_payload(payload) -> list[str]:
    """解析 OpenAI ``data[].id`` / Gemini ``models[].name`` / Ollama ``models[].name``。"""
    if isinstance(payload, dict):
        raw = payload.get("data")
        if not isinstance(raw, list):
            raw = payload.get("models")
        if not isinstance(raw, list):
            raw = payload.get("result")
    elif isinstance(payload, list):
        raw = payload
    else:
        raw = []
    ids: list[str] = []
    seen: set[str] = set()
    for item in raw or []:
        if isinstance(item, str):
            model_id = item
        elif isinstance(item, dict):
            model_id = item.get("id") or item.get("name") or item.get("model")
        else:
            model_id = None
        if not isinstance(model_id, str):
            continue
        model_id = model_id.strip()
        if model_id.startswith("models/"):
            model_id = model_id[len("models/"):]
        if not model_id or len(model_id) > 160 or "\n" in model_id:
            continue
        if model_id not in seen:
            seen.add(model_id)
            ids.append(model_id)
    return ids


def _caps_map(model_ids: list[str], user_id: int | None = None, provider_id: int | None = None) -> dict:
    caps_map = {model_id: infer_capabilities(model_id) for model_id in model_ids}
    if not user_id or not provider_id:
        return caps_map
    try:
        with db.connection() as con:
            rows = con.execute(
                "SELECT model_id, caps_json FROM ai_models WHERE provider_id=? AND user_id=?",
                (provider_id, user_id),
            ).fetchall()
    except Exception:
        return caps_map
    for row in rows:
        try:
            overrides = json.loads(row["caps_json"] or "{}")
        except ValueError:
            continue
        if not isinstance(overrides, dict):
            continue
        caps = caps_map.get(row["model_id"])
        if caps is None:
            continue
        for key in ("vision", "json", "reasoning", "stream"):
            if isinstance(overrides.get(key), bool):
                caps[key] = overrides[key]
    return caps_map


async def _request_models_listing(ctx: dict, user_id: int) -> dict:
    """真实请求厂商模型列表；兼容 OpenAI / Gemini 原生 / Ollama 原生回退。"""
    from . import httpx as ai_httpx, netguard
    from .adapters import auth_fields, apply_extra_headers

    kind = ctx["kind"]
    compat = ctx["compat"]
    endpoint, params = _models_endpoint(ctx["base_url"], kind, compat)
    headers, query = auth_fields(kind, ctx["auth_mode"], ctx["api_key"])
    apply_extra_headers(headers, ctx["extra_headers"])
    query.update(params)
    if query:
        endpoint = netguard.join_endpoint(endpoint, "", params=query)
    notes = []
    resolved_endpoint = netguard.mask_url(endpoint)
    use_proxy = bool(compat.get("use_proxy_env")) and netguard.url_policy() == "local_open"
    raw_payload = None
    status = None
    error_text = ""
    try:
        kwargs = netguard.client_kwargs(
            endpoint, timeout_seconds=25.0, connect_timeout=10.0, use_proxy_env=use_proxy,
        )
        async with ai_httpx.AsyncClient(**kwargs) as client:
            response = await client.get(endpoint, headers=headers)
            status = response.status_code
            if 200 <= status < 300:
                raw_payload = response.json()
                # Gemini 原生 /models 默认分页：跟随 nextPageToken 拉全量，不截断。
                if kind == "gemini_native" and isinstance(raw_payload, dict) and raw_payload.get("nextPageToken"):
                    merged = _model_ids_from_payload(raw_payload)
                    page_token = raw_payload.get("nextPageToken")
                    pages = 1
                    while page_token and pages < 20:
                        page_url = netguard.join_endpoint(endpoint, "", params={"pageToken": page_token})
                        page_response = await client.get(page_url, headers=headers)
                        if not 200 <= page_response.status_code < 300:
                            break
                        page_payload = page_response.json()
                        for model_id in _model_ids_from_payload(page_payload):
                            if model_id not in merged:
                                merged.append(model_id)
                        page_token = (
                            page_payload.get("nextPageToken") if isinstance(page_payload, dict) else None
                        )
                        pages += 1
                    raw_payload = {"models": [{"name": model_id} for model_id in merged]}
            else:
                error_text = netguard.mask_secrets(
                    response.text[:800] if hasattr(response, "text") else "",
                )
    except Exception as exc:  # noqa: BLE001 - 网络异常统一转为可读诊断
        error_text = netguard.mask_secrets(f"{type(exc).__name__}: {exc}")[:500]
    model_ids = _model_ids_from_payload(raw_payload)
    # Ollama 原生回退：本地服务通常没有 /v1/models，但提供 /api/tags。
    if not model_ids and status not in (200, 201) and netguard.url_policy() == "local_open":
        origin = urlsplit(ctx["base_url"])
        origin_url = f"{origin.scheme}://{origin.netloc}"
        if netguard.is_local_host(origin.hostname or ""):
            tags_endpoint = netguard.join_endpoint(origin_url, "/api/tags")
            try:
                kwargs = netguard.client_kwargs(tags_endpoint, timeout_seconds=10.0, connect_timeout=5.0)
                async with ai_httpx.AsyncClient(**kwargs) as client:
                    response = await client.get(tags_endpoint)
                    if 200 <= response.status_code < 300:
                        tags_payload = response.json()
                        model_ids = _model_ids_from_payload(tags_payload)
                        if model_ids:
                            resolved_endpoint = netguard.mask_url(tags_endpoint)
                            status = response.status_code
                            error_text = ""
                            notes.append("已改用 Ollama 原生 /api/tags 拉取本地模型列表")
            except Exception:  # noqa: BLE001 - 回退失败保留原始诊断
                pass
    ok = bool(model_ids) or (status is not None and 200 <= status < 300)
    caps_map = _caps_map(model_ids, user_id, ctx.get("provider_id"))
    diagnosis = ""
    if not ok:
        diagnosis = _diagnose_http(status, error_text, path="/models")
    return {
        "ok": ok,
        "normalized_base_url": ctx["base_url"],
        "resolved_endpoint": resolved_endpoint,
        "models": [{"model_id": model_id, "caps": caps_map[model_id]} for model_id in model_ids],
        "model_ids": list(model_ids),
        "count": len(model_ids),
        "capabilities": caps_map,
        "http_status": status,
        "diagnosis": diagnosis,
        "notes": list(ctx.get("notes") or []) + notes,
    }


def _diagnose_http(status, error_text: str, *, path: str) -> str:
    """把上游 HTTP 错误翻译成可执行的中文诊断。"""
    if status is None:
        return f"无法连接模型服务：{error_text or '网络不可达，请检查地址、端口与网络'}"
    if status == 401:
        return "鉴权失败（401）：API Key 无效或未填写，请检查密钥与鉴权方式"
    if status == 403:
        return "权限不足（403）：密钥无权访问该模型/接口，或该模型未开通"
    if status == 404:
        return (
            f"接口不存在（404）：请检查 base_url 是否正确（例如 OpenAI 兼容服务通常需要 /v1），"
            f"系统会自动请求 {{base}}{path}"
        )
    if status == 400:
        return f"参数不兼容（400）：可能是模型 ID 拼写错误或该模型不支持当前参数。{error_text}"
    if status == 422:
        return f"请求参数被厂商拒绝（422）：{error_text}"
    if status == 429:
        return "厂商限流（429）：请求过于频繁或额度不足，请稍后再试"
    if 500 <= status < 600:
        return f"厂商服务端错误（{status}）：请稍后重试或更换模型"
    return f"模型服务返回 HTTP {status}：{error_text}"


async def _probe_upstream(ctx: dict, data, user) -> dict:
    """发送最小探测请求，返回连通/鉴权/模型名诊断（不落库、不产生任务记录）。"""
    import time

    from . import httpx as ai_httpx, netguard
    from .adapters import MAX_RESPONSE_BYTES, _extract_non_stream, _model_caps, _prepare_request

    provider = {
        "id": ctx.get("provider_id"),
        "kind": ctx["kind"],
        "name": "保存前探测",
        "base_url": ctx["base_url"],
        "auth_mode": ctx["auth_mode"],
        "user_id": user["id"],
    }
    caps = _model_caps(provider, data.model) if ctx.get("provider_id") else infer_capabilities(data.model)
    prepared = _prepare_request(
        provider, data.model, "只回复 OK", api_key=ctx["api_key"],
        json_output=False, token_limit=16, stream_mode=False, caps=caps,
        extra_headers=ctx["extra_headers"], compat=ctx["compat"],
    )
    endpoint = prepared.endpoint
    timeout_seconds = float(ctx["compat"].get("timeout_seconds") or 30)
    use_proxy = bool(ctx["compat"].get("use_proxy_env")) and netguard.url_policy() == "local_open"
    request_url = netguard.mask_url(endpoint)
    started = time.monotonic()
    status = None
    error_text = ""
    body_bytes = b""
    try:
        kwargs = netguard.client_kwargs(
            endpoint, timeout_seconds=timeout_seconds,
            connect_timeout=min(15.0, timeout_seconds), use_proxy_env=use_proxy,
        )
        async with ai_httpx.AsyncClient(**kwargs) as client:
            async with client.stream(
                "POST", endpoint, headers=prepared.headers, json=prepared.body,
            ) as response:
                status = response.status_code
                buffer = bytearray()
                async for piece in response.aiter_bytes():
                    buffer.extend(piece)
                    if len(buffer) > MAX_RESPONSE_BYTES:
                        break
                body_bytes = bytes(buffer)
    except ai_httpx.TimeoutException:
        return _probe_result(
            False, request_url, ctx, data.model, status=None, latency_ms=_elapsed_ms(started),
            diagnosis="连接超时：请检查地址/端口是否可达、是否需要代理，或适当调大超时秒数",
        )
    except Exception as exc:  # noqa: BLE001 - 网络异常统一转为诊断
        return _probe_result(
            False, request_url, ctx, data.model, status=None, latency_ms=_elapsed_ms(started),
            diagnosis=f"无法连接模型服务：{netguard.mask_secrets(f'{type(exc).__name__}: {exc}')[:300]}",
        )
    latency_ms = _elapsed_ms(started)
    if status is not None and 200 <= status < 300:
        content_preview = ""
        usage: dict = {}
        try:
            payload = json.loads(body_bytes)
            text, usage, _truncated, _thinking = _extract_non_stream(payload, ctx["kind"])
            content_preview = (text or "").strip()[:200]
        except Exception:  # noqa: BLE001 - 探测只关心连通性，响应解析失败不影响结论
            content_preview = netguard.mask_secrets(body_bytes.decode("utf-8", "ignore"))[:200]
        return _probe_result(
            True, request_url, ctx, data.model, status=status, latency_ms=latency_ms,
            diagnosis="连接正常：地址、鉴权与模型名可用", usage=usage,
            content_preview=content_preview,
        )
    error_text = netguard.mask_secrets(body_bytes.decode("utf-8", "ignore"))[:500]
    return _probe_result(
        False, request_url, ctx, data.model, status=status, latency_ms=latency_ms,
        diagnosis=_diagnose_http(status, error_text, path="/chat/completions"),
        content_preview=error_text[:200],
    )


def _elapsed_ms(started: float) -> int:
    import time

    return max(0, int((time.monotonic() - started) * 1000))


def _probe_result(
    ok: bool,
    request_url: str,
    ctx: dict,
    model: str,
    *,
    status,
    latency_ms: int,
    diagnosis: str,
    usage: dict | None = None,
    content_preview: str = "",
) -> dict:
    return {
        "ok": bool(ok),
        "http_status": status,
        "latency_ms": latency_ms,
        "diagnosis": diagnosis,
        "request_url": request_url,
        "normalized_base_url": ctx["base_url"],
        "resolved_endpoint": request_url,
        "kind": ctx["kind"],
        "model": model,
        "usage": usage or {},
        "content_preview": content_preview,
        "notes": list(ctx.get("notes") or []),
    }


# ---------------------------------------------------------------------------
# 请求模型
# ---------------------------------------------------------------------------

class ProviderInput(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    kind: str = Field(pattern=_KIND_PATTERN)
    base_url: str = Field(min_length=1, max_length=1000)
    api_key: str | None = Field(default=None, max_length=1000)
    models: list[str] = Field(min_length=1, max_length=200)
    auth_mode: str | None = Field(default=None, pattern=_AUTH_PATTERN)
    extra_headers: dict[str, str] | None = None
    compat: dict | None = None


class ProviderPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    kind: str | None = Field(default=None, pattern=_KIND_PATTERN)
    base_url: str | None = Field(default=None, min_length=1, max_length=1000)
    api_key: str | None = Field(default=None, max_length=1000)
    models: list[str] | None = Field(default=None, min_length=1, max_length=200)
    auth_mode: str | None = Field(default=None, pattern=_AUTH_PATTERN)
    extra_headers: dict[str, str] | None = None
    compat: dict | None = None


class ModelInput(BaseModel):
    model: str = Field(min_length=1, max_length=160)


class PreviewInput(BaseModel):
    kind: str = Field(pattern=_KIND_PATTERN)
    base_url: str = Field(min_length=1, max_length=1000)
    api_key: str | None = Field(default=None, max_length=1000)
    provider_id: int | None = None
    auth_mode: str | None = Field(default=None, pattern=_AUTH_PATTERN)
    extra_headers: dict[str, str] | None = None
    compat: dict | None = None


class ProbeInput(PreviewInput):
    model: str = Field(min_length=1, max_length=200)


class ModelMetaPatch(BaseModel):
    display_name: str | None = Field(default=None, max_length=120)
    caps: dict | None = None
    input_price: float | None = Field(default=None, ge=0)
    output_price: float | None = Field(default=None, ge=0)
    enabled: bool | None = None


class TaskDefaultInput(BaseModel):
    task: str = Field(pattern="^(explain|grade|ocr|notes|tagging|judge|upload)$")
    primary_provider_id: int | None = None
    primary_model: str | None = Field(default=None, max_length=160)
    verify_provider_id: int | None = None
    verify_model: str | None = Field(default=None, max_length=160)
    judge_provider_id: int | None = None
    judge_model: str | None = Field(default=None, max_length=160)
    mode: str = Field(default="fast", pattern="^(fast|accurate)$")


class AllowedHostInput(BaseModel):
    hostname: str = Field(min_length=3, max_length=200)


class EmbeddingConfigInput(BaseModel):
    mode: str = Field(pattern="^(off|inherit|custom)$")
    provider_id: int | None = None
    base_url: str | None = Field(default=None, max_length=1000)
    model: str | None = Field(default=None, max_length=200)
    api_key: str | None = Field(default=None, max_length=1000)


class EmbeddingTestInput(BaseModel):
    mode: str | None = Field(default=None, pattern="^(off|inherit|custom)$")
    provider_id: int | None = None
    base_url: str | None = Field(default=None, max_length=1000)
    model: str | None = Field(default=None, max_length=200)
    api_key: str | None = Field(default=None, max_length=1000)
    text: str | None = Field(default=None, max_length=2000)


# ---------------------------------------------------------------------------
# 连接 CRUD
# ---------------------------------------------------------------------------

@router.get("/providers")
def providers(user=Depends(current_user)):
    from . import netguard

    with db.connection() as con:
        rows = con.execute("SELECT * FROM ai_providers WHERE user_id=? ORDER BY id", (user["id"],)).fetchall()
        m_rows = con.execute("SELECT * FROM ai_models WHERE user_id=? ORDER BY id", (user["id"],)).fetchall()
    by_provider: dict[int, list[dict]] = {}
    for mr in m_rows:
        by_provider.setdefault(mr["provider_id"], []).append(dict(mr))
    items = []
    for r in rows:
        mr_list = by_provider.get(r["id"])
        items.append(_provider_public(r, mr_list))
    return {
        "items": items,
        "presets": PRESETS,
        "url_policy": netguard.url_policy(),
        "public_mode": netguard.url_policy() != "local_open",
    }


@router.post("/providers")
def create_provider(data: ProviderInput, user=Depends(current_user)):
    from . import _validate_url as validate_url

    url = validate_url(data.base_url)
    models = list(dict.fromkeys(m.strip() for m in data.models if m.strip()))
    if not models or any(len(m) > 160 or "\n" in m for m in models):
        raise HTTPException(422, "请填写有效的模型 ID")
    auth_mode = _normalize_auth_mode(data.auth_mode)
    key = _clean_api_key(data.api_key, auth_mode, required=True)
    extra_headers = _normalize_extra_headers(data.extra_headers)
    compat = normalize_compat(data.compat)
    encrypted, hint = _encrypt_key(key)
    with db.connection() as con:
        if con.execute("SELECT COUNT(*) FROM ai_providers WHERE user_id=?", (user["id"],)).fetchone()[0] >= 20:
            raise HTTPException(422, "每个账号最多保存 20 个连接")
        item_id = con.execute(
            "INSERT INTO ai_providers(user_id,name,kind,base_url,encrypted_key,key_hint,models_json,"
            "auth_mode,extra_headers_enc,compat_json,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                user["id"], data.name.strip(), data.kind, url, encrypted, hint, _json(models),
                auth_mode, _encrypt_extra_headers(extra_headers), _json(compat), db.utcnow(),
            ),
        ).lastrowid
        _sync_provider_models(con, item_id, user["id"], models)
    return _provider_public(_owned("ai_providers", item_id, user["id"]))


@router.patch("/providers/{provider_id}")
def update_provider(provider_id: int, data: ProviderPatch, user=Depends(current_user)):
    from . import _validate_url as validate_url

    row = _owned("ai_providers", provider_id, user["id"])
    name = data.name.strip() if data.name is not None else row["name"]
    kind = data.kind if data.kind is not None else row["kind"]
    url = validate_url(data.base_url) if data.base_url is not None else row["base_url"]
    if data.models is not None:
        models = list(dict.fromkeys(m.strip() for m in data.models if m.strip()))
        if not models or any(len(m) > 160 or "\n" in m for m in models):
            raise HTTPException(422, "请填写有效的模型 ID")
    else:
        models = json.loads(row["models_json"])
    auth_mode = (
        _normalize_auth_mode(data.auth_mode) if data.auth_mode is not None
        else str(_row_get(row, "auth_mode", "auto") or "auto")
    )
    encrypted = _row_get(row, "encrypted_key", "")
    key_hint = _row_get(row, "key_hint", "")
    if data.api_key is not None:
        key = _clean_api_key(data.api_key, auth_mode, required=True)
        encrypted, key_hint = _encrypt_key(key)
    elif auth_mode != "none":
        # 未重填 Key 但把鉴权方式从 none 改回带鉴权：要求已存 Key 非空。
        try:
            existing_key = _cipher().decrypt(str(encrypted or "").encode()).decode()
        except InvalidToken:
            raise HTTPException(422, "API 密钥无法解密，请重新保存连接") from None
        if not existing_key.strip():
            raise HTTPException(422, "当前连接没有已保存的 API Key，请填写后再切换鉴权方式")
    extra_headers_enc = _row_get(row, "extra_headers_enc", "")
    if data.extra_headers is not None:
        extra_headers_enc = _encrypt_extra_headers(_normalize_extra_headers(data.extra_headers))
    compat_json = _row_get(row, "compat_json", "{}")
    if data.compat is not None:
        compat_json = _json(normalize_compat(data.compat))
    with db.connection() as con:
        con.execute(
            "UPDATE ai_providers SET name=?,kind=?,base_url=?,encrypted_key=?,key_hint=?,models_json=?,"
            "auth_mode=?,extra_headers_enc=?,compat_json=? WHERE id=? AND user_id=?",
            (
                name, kind, url, encrypted, key_hint, _json(models),
                auth_mode, extra_headers_enc, compat_json, provider_id, user["id"],
            ),
        )
        _sync_provider_models(con, provider_id, user["id"], models)
    return _provider_public(_owned("ai_providers", provider_id, user["id"]))


@router.delete("/providers/{provider_id}")
def delete_provider(provider_id: int, user=Depends(current_user)):
    _owned("ai_providers", provider_id, user["id"])
    with db.connection() as con:
        # 事务内清理任务默认模型引用，杜绝删除连接后的脏默认模型。
        for prefix in ("primary", "verify", "judge"):
            con.execute(
                f"UPDATE ai_task_defaults SET {prefix}_provider_id=NULL, {prefix}_model=NULL "
                f"WHERE user_id=? AND {prefix}_provider_id=?",
                (user["id"], provider_id),
            )
        con.execute("DELETE FROM ai_models WHERE provider_id=? AND user_id=?", (provider_id, user["id"]))
        con.execute("DELETE FROM ai_providers WHERE id=? AND user_id=?", (provider_id, user["id"]))
        try:
            con.execute(
                "UPDATE ai_embeddings_config SET mode='off', provider_id=NULL, updated_at=? "
                "WHERE user_id=? AND provider_id=?",
                (db.utcnow(), user["id"], provider_id),
            )
        except Exception:
            pass
    return {"deleted": True}


@router.post("/providers/{provider_id}/test")
async def test_provider(provider_id: int, data: ModelInput, user=Depends(current_user)):
    from . import _call_model

    provider = _selected_provider(user["id"], provider_id, data.model)
    try:
        result = await _call_model(provider, data.model, "请只回复：连接正常", token_limit=1024)
    except (ValueError, HTTPException) as exc:
        raise HTTPException(422, exc.detail if isinstance(exc, HTTPException) else str(exc)) from None
    notice = "此测试已向你的模型服务发送一次请求，可能产生费用"
    if result.get("notice"):
        # 推理模型自动放宽输出预算的提示要一起返回给设置页，避免用户误以为 1024 token 足够。
        notice = result["notice"] + " " + notice
    return {
        "ok": True, "content": result["content"], "usage": result["usage"],
        "token_limit": result.get("token_limit"), "notice": notice,
    }


@router.get("/providers/{provider_id}/models")
async def list_provider_models(provider_id: int, user=Depends(current_user)):
    """只读模型列表端点（等价旧 POST /fetch-models；编辑弹窗已改用保存前预览接口）。"""
    return await fetch_provider_models(provider_id, user)


@router.post("/providers/{provider_id}/fetch-models")
async def fetch_provider_models(provider_id: int, user=Depends(current_user)):
    """Explicit user-triggered call to vendor GET /models endpoint（已保存连接）。"""
    from . import _validate_url as validate_url

    provider = _owned("ai_providers", provider_id, user["id"])
    base = validate_url(provider["base_url"], resolve=True)
    api_key = _decrypt_key(_row_get(provider, "encrypted_key", ""))
    ctx = {
        "base_url": base,
        "kind": str(_row_get(provider, "kind", "openai") or "openai"),
        "auth_mode": resolve_auth_mode(provider),
        "api_key": api_key,
        "extra_headers": provider_extra_headers(provider),
        "compat": provider_compat(provider),
        "provider_id": provider_id,
    }
    try:
        result = await _request_models_listing(ctx, user["id"])
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(422, "无法拉取模型列表，请检查网络或手动填写模型 ID") from None
    if not result["models"]:
        diagnosis = result.get("diagnosis") or "厂商 /models 未返回模型，请手动填写模型 ID"
        raise HTTPException(422, diagnosis)
    return {
        "models": result["models"],
        "count": result["count"],
        "notice": "已向厂商 /models 真实查询，勾选后点击保存即可加入连接",
    }


@router.post("/providers/fetch-models-preview")
async def fetch_models_preview(data: PreviewInput, user=Depends(current_user)):
    """保存前拉取模型列表：无需落库，地址/Header 用表单最新值，可复用已存 Key。"""
    _probe_rate_limit(user["id"], "ai-models-preview")
    ctx = _preview_context(data, user)
    return await _request_models_listing(ctx, user["id"])


@router.post("/providers/probe")
async def probe_provider(data: ProbeInput, user=Depends(current_user)):
    """保存前最小探测：验证连通性、鉴权与模型名，返回耗时与结构化诊断。"""
    _probe_rate_limit(user["id"], "ai-probe")
    ctx = _preview_context(data, user)
    return await _probe_upstream(ctx, data, user)


# ---------------------------------------------------------------------------
# 模型元数据
# ---------------------------------------------------------------------------

@router.patch("/models/{model_row_id}")
def update_model_meta(model_row_id: int, data: ModelMetaPatch, user=Depends(current_user)):
    row = _owned("ai_models", model_row_id, user["id"])
    caps = json.loads(row["caps_json"] or "{}")
    if data.caps is not None:
        caps.update({k: v for k, v in data.caps.items() if k in ("vision", "json", "reasoning", "stream", "context")})
    with db.connection() as con:
        con.execute(
            "UPDATE ai_models SET display_name=?, caps_json=?, input_price=?, output_price=?, enabled=? "
            "WHERE id=? AND user_id=?",
            (
                data.display_name if data.display_name is not None else row["display_name"],
                _json(caps),
                data.input_price if data.input_price is not None else row["input_price"],
                data.output_price if data.output_price is not None else row["output_price"],
                int(data.enabled) if data.enabled is not None else row["enabled"],
                model_row_id, user["id"],
            ),
        )
    updated = _owned("ai_models", model_row_id, user["id"])
    return {
        "id": updated["id"], "model_id": updated["model_id"], "display_name": updated["display_name"],
        "caps": json.loads(updated["caps_json"] or "{}"),
        "input_price": updated["input_price"], "output_price": updated["output_price"],
        "enabled": bool(updated["enabled"]),
    }


# ---------------------------------------------------------------------------
# 任务默认模型
# ---------------------------------------------------------------------------

@router.get("/ai/task-defaults")
def get_task_defaults(user=Depends(current_user)):
    with db.connection() as con:
        rows = [dict(r) for r in con.execute("SELECT * FROM ai_task_defaults WHERE user_id=?", (user["id"],)).fetchall()]
    return {"items": {r["task"]: r for r in rows}}


@router.put("/ai/task-defaults")
def set_task_default(data: TaskDefaultInput, user=Depends(current_user)):
    for pid, m in (
        (data.primary_provider_id, data.primary_model),
        (data.verify_provider_id, data.verify_model),
        (data.judge_provider_id, data.judge_model),
    ):
        if pid and m:
            _selected_provider(user["id"], pid, m)
    with db.connection() as con:
        con.execute(
            "INSERT INTO ai_task_defaults(user_id,task,primary_provider_id,primary_model,verify_provider_id,"
            "verify_model,judge_provider_id,judge_model,mode,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(user_id,task) DO UPDATE SET primary_provider_id=excluded.primary_provider_id,"
            "primary_model=excluded.primary_model,verify_provider_id=excluded.verify_provider_id,"
            "verify_model=excluded.verify_model,judge_provider_id=excluded.judge_provider_id,"
            "judge_model=excluded.judge_model,mode=excluded.mode,updated_at=excluded.updated_at",
            (
                user["id"], data.task, data.primary_provider_id, data.primary_model,
                data.verify_provider_id, data.verify_model, data.judge_provider_id,
                data.judge_model, data.mode, db.utcnow(),
            ),
        )
    return data.model_dump()


# ---------------------------------------------------------------------------
# Embedding 配置（账号级）
# ---------------------------------------------------------------------------

def _embedding_public(user_id: int) -> dict:
    from .embeddings import embedding_config, env_embedding_config, read_embedding_config

    row = read_embedding_config(user_id) or {}
    mode = str(row.get("mode") or "off")
    env_configured = env_embedding_config() is not None
    config = embedding_config(user_id)
    compatible = []
    try:
        with db.connection() as con:
            rows = con.execute(
                "SELECT id,name,kind,models_json FROM ai_providers WHERE user_id=? ORDER BY id",
                (user_id,),
            ).fetchall()
    except Exception:
        rows = []
    for item in rows:
        if item["kind"] in ("openai", "gemini", "azure_openai"):
            compatible.append({
                "id": item["id"], "name": item["name"], "kind": item["kind"],
                "models": json.loads(item["models_json"] or "[]"),
            })
    from . import netguard

    return {
        "mode": mode,
        "provider_id": row.get("provider_id"),
        "base_url": row.get("base_url") or "",
        "model": row.get("model") or "",
        "has_api_key": bool(row.get("api_key_enc")),
        "key_hint": "已加密" if row.get("api_key_enc") else "",
        "updated_at": row.get("updated_at"),
        "env_configured": env_configured,
        "effective_source": config.source if config else ("off" if mode == "off" and row else "none"),
        "effective": {
            "base_url": netguard.mask_url(config.base_url) if config else "",
            "model": config.model if config else "",
            "endpoint": netguard.mask_url(config.endpoint) if config else "",
            "ready": config is not None,
        },
        "compatible_providers": compatible,
    }


@router.get("/providers/embeddings-config")
def get_embeddings_config(user=Depends(current_user)):
    return _embedding_public(user["id"])


@router.put("/providers/embeddings-config")
def put_embeddings_config(data: EmbeddingConfigInput, user=Depends(current_user)):
    from . import netguard
    from .embeddings import read_embedding_config, save_embedding_config

    existing = read_embedding_config(user["id"]) or {}
    mode = data.mode
    provider_id = None
    base_url = ""
    model = ""
    api_key_enc = existing.get("api_key_enc") or ""
    if mode == "inherit":
        if not data.provider_id:
            raise HTTPException(422, "复用已有连接时请选择 provider_id")
        provider = _owned("ai_providers", data.provider_id, user["id"])
        if provider["kind"] not in ("openai", "gemini", "azure_openai"):
            raise HTTPException(422, "该连接不是 OpenAI 兼容协议，无法用于 Embedding")
        if not str(data.model or "").strip():
            raise HTTPException(422, "复用已有连接时请填写 Embedding 模型名，例如 text-embedding-3-small")
        provider_id = data.provider_id
        base_url = ""
        model = str(data.model).strip()
        api_key_enc = ""  # inherit 直接复用 provider 的加密 Key，不重复保存
    elif mode == "custom":
        if not str(data.base_url or "").strip() or not str(data.model or "").strip():
            raise HTTPException(422, "自定义 Embedding 服务需要填写地址与模型名")
        base_url = _validate_url(data.base_url)
        model = str(data.model).strip()
        if data.api_key is not None:
            key = _clean_api_key(data.api_key, "bearer", required=False)
            api_key_enc = _cipher().encrypt(key.encode()).decode() if key else ""
    else:  # off
        base_url = ""
        model = ""
        api_key_enc = ""
    save_embedding_config(
        user["id"], mode=mode, provider_id=provider_id, base_url=base_url,
        model=model, api_key_enc=api_key_enc,
    )
    return _embedding_public(user["id"])


@router.post("/providers/embeddings-test")
async def test_embeddings(data: EmbeddingTestInput, user=Depends(current_user)):
    """用当前（可临时覆盖）配置真实发一条短文本，验证维度与连通性。"""
    from .embeddings import test_embedding_config

    _probe_rate_limit(user["id"], "ai-embedding-test")
    payload = data.model_dump(exclude_none=True)
    for key in ("mode", "provider_id", "base_url", "model", "api_key"):
        if key in payload and key != "mode" and payload.get(key) == "":
            payload[key] = None
    mode = payload.get("mode")
    if mode and mode != "off":
        # 与 PUT 相同的前置校验，避免把探测接口当成任意 URL 的 SSRF 跳板。
        if mode == "custom" and payload.get("base_url"):
            payload["base_url"] = _validate_url(payload["base_url"])
        if mode == "inherit" and payload.get("provider_id"):
            _owned("ai_providers", payload["provider_id"], user["id"])
    return await test_embedding_config(user["id"], **payload)


# ---------------------------------------------------------------------------
# 用量
# ---------------------------------------------------------------------------

def record_usage(user_id: int | None, job_id: int | None, provider_id: int | None, model: str, usage: dict):
    if not user_id or not usage:
        return
    in_tok = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
    out_tok = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
    if in_tok == 0 and out_tok == 0 and usage.get("total_tokens"):
        in_tok = int(usage["total_tokens"])
    try:
        with db.connection() as con:
            con.execute(
                "INSERT INTO ai_usage(user_id,job_id,provider_id,model,input_tokens,output_tokens,created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (user_id, job_id, provider_id, model, in_tok, out_tok, db.utcnow()),
            )
    except Exception:
        pass


@router.get("/ai/usage")
def usage_summary(days: int = 30, user=Depends(current_user)):
    days = max(1, min(days, 365))
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with db.connection() as con:
        by_model = [
            dict(r) for r in con.execute(
                "SELECT u.provider_id, u.model, COUNT(*) AS calls, "
                "SUM(u.input_tokens) AS input_tokens, SUM(u.output_tokens) AS output_tokens, "
                "m.input_price, m.output_price "
                "FROM ai_usage u LEFT JOIN ai_models m ON m.user_id=u.user_id AND m.provider_id=u.provider_id AND m.model_id=u.model "
                "WHERE u.user_id=? AND u.created_at >= ? "
                "GROUP BY u.provider_id, u.model, m.input_price, m.output_price "
                "ORDER BY (SUM(u.input_tokens)+SUM(u.output_tokens)) DESC",
                (user["id"], cutoff),
            ).fetchall()
        ]
        by_day = [
            dict(r) for r in con.execute(
                "SELECT SUBSTR(created_at, 1, 10) AS day, COUNT(*) AS calls, "
                "SUM(input_tokens) AS input_tokens, SUM(output_tokens) AS output_tokens "
                "FROM ai_usage WHERE user_id=? AND created_at >= ? "
                "GROUP BY SUBSTR(created_at, 1, 10) ORDER BY day DESC",
                (user["id"], cutoff),
            ).fetchall()
        ]
    for item in by_model:
        ip, op = item.get("input_price"), item.get("output_price")
        if ip is not None and op is not None:
            # Prices are per 1M tokens
            cost = (item["input_tokens"] / 1_000_000.0) * ip + (item["output_tokens"] / 1_000_000.0) * op
            item["estimated_cost"] = round(cost, 4)
        else:
            item["estimated_cost"] = None
    return {
        "days": days,
        "by_model": by_model,
        "by_day": by_day,
        "notice": "费用仅在用户自行填写单价（元/百万 tokens）后估算；未填单价的模型显示未知。",
    }


# ---------------------------------------------------------------------------
# 管理员白名单
# ---------------------------------------------------------------------------

@router.get("/admin/allowed-hosts")
def list_allowed_hosts(user=Depends(require_admin)):
    with db.connection() as con:
        custom = [dict(r) for r in con.execute("SELECT * FROM ai_allowed_hosts ORDER BY hostname").fetchall()]
    preset_hosts = sorted({urlsplit(p["base_url"]).hostname.lower() for p in PRESETS})
    env_hosts = sorted({
        _normalize_allowed_entry(h) for h in os.environ.get("STUDY_AI_ALLOWED_HOSTS", "").split(",")
        if _normalize_allowed_entry(h)
    })
    return {"presets": preset_hosts, "env": env_hosts, "custom": custom}


def _valid_allowed_pattern(host: str) -> bool:
    if not host or len(host) > 200 or " " in host or ":" in host or "//" in host:
        return False
    candidate = host[2:] if host.startswith("*.") else host
    if not candidate or "." not in candidate:
        return False
    labels = candidate.split(".")
    return all(label and _HEADER_NAME_RE.match(label) for label in labels)


@router.post("/admin/allowed-hosts")
def add_allowed_host(data: AllowedHostInput, user=Depends(require_admin)):
    host = _normalize_allowed_entry(data.hostname)
    if not _valid_allowed_pattern(host):
        raise HTTPException(422, "请仅输入有效域名（支持 *.example.com 通配符），例如 api.example.com")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise HTTPException(422, "不允许添加 IP 地址")
    if host in ("localhost", "localhost.localdomain") or host.endswith(".localhost"):
        raise HTTPException(422, "不允许添加本地域名")
    with db.connection() as con:
        con.execute(
            "INSERT INTO ai_allowed_hosts(hostname, added_by, created_at) VALUES (?,?,?) "
            "ON CONFLICT(hostname) DO NOTHING",
            (host, user["id"], db.utcnow()),
        )
    return {"hostname": host, "added": True}


@router.delete("/admin/allowed-hosts/{hostname}")
def remove_allowed_host(hostname: str, user=Depends(require_admin)):
    with db.connection() as con:
        con.execute("DELETE FROM ai_allowed_hosts WHERE hostname=?", (_normalize_allowed_entry(hostname),))
    return {"deleted": True}


__all__ = [
    "PRESETS", "PROVIDER_KINDS", "AUTH_MODES", "MODEL_CAP_RULES", "infer_capabilities",
    "normalize_compat", "provider_compat", "provider_extra_headers", "resolve_auth_mode",
    "_allowed_hostnames", "_canonical_model_family", "_cipher", "_identity", "_json",
    "_owned", "_selected_provider", "_validate_url", "cross_relay_same_model",
    "identity_risk", "record_usage", "router",
]
