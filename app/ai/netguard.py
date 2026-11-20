"""AI 出站网络守卫：URL 规范化、分层策略、DNS 校验与 IP Pinning。

分层策略（`STUDY_AI_URL_POLICY`，缺省随 `STUDY_PUBLIC_MODE` 自动决策）：

* ``local_open``（本地模式默认）：放行任意 HTTP/HTTPS、自定义端口、回环/局域网与
  本地模型（Ollama / LM Studio / vLLM），允许 ``auth_mode=none`` 与空 Key；
  仅始终拦截云元数据地址。
* ``public_https``（公网模式默认）：仅允许 HTTPS，拒绝凭据/片段与密钥型 query，
  允许自定义端口与安全 query；DNS 解析后拒绝私网/回环/链路本地/元数据 IP，并在
  请求时用 IP Pinning + SNI 固定已校验地址，根治 DNS Rebinding。
* ``allowlist``：在 public_https 基础上再要求域名命中预设/环境变量（支持
  ``*.example.com`` 通配符）/数据库白名单。

无论哪种策略，云元数据地址（``169.254.169.254``、``fd00:ec2::254``、
``metadata.google.internal`` 等）始终禁止。
"""
from __future__ import annotations

from dataclasses import dataclass, field
import fnmatch
import ipaddress
import os
import re
import socket
from typing import Iterable, Mapping, Sequence
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from fastapi import HTTPException
import httpx

try:  # httpx 的运行时依赖，正常安装 httpx 即存在；缺失时退化为普通连接池。
    import httpcore
except ImportError:  # pragma: no cover - httpx 自带 httpcore
    httpcore = None

POLICIES = ("local_open", "public_https", "allowlist")
PUBLIC_POLICIES = ("public_https", "allowlist")

DEFAULT_PORTS = {"http": 80, "https": 443}

# 用户最常见的「把接口地址整段粘进来」的尾部路径；规范化时自动剥离。
# 注意只剥离端点本身，保留 base 前缀（例如 /v1、/compatible-mode/v1、Azure deployment 路径）。
_TAIL_SUFFIXES = (
    "/chat/completions",
    "/completions",
    "/responses",
    "/messages",
    "/embeddings",
    "/models",
    "/api/tags",
    "/api/generate",
    "/api/chat",
    ":streamgeneratecontent",
    ":generatecontent",
)
_GEMINI_MODEL_PATH_RE = re.compile(
    r"/models/[^/]+:(?:stream)?generatecontent$", re.IGNORECASE,
)

# 公网/白名单模式下不接受的密钥型 query 参数（避免 Key 泄露到 URL/日志）。
_SENSITIVE_QUERY_KEYS = frozenset({
    "key", "api_key", "apikey", "api-key", "token", "access_token", "access-token",
    "secret", "client_secret", "password", "passwd", "sig", "signature", "auth",
    "authorization",
})

# 云厂商元数据服务的固定地址：任何策略下都恒久禁止。
_METADATA_NETWORKS = tuple(
    ipaddress.ip_network(value)
    for value in (
        "169.254.169.254/32",      # AWS / GCP / Azure / OpenStack IMDS
        "169.254.170.2/32",        # AWS ECS task metadata
        "169.254.169.253/32",      # AWS VPC DNS
        "100.100.100.200/32",      # 阿里云 metadata
        "fd00:ec2::254/128",       # AWS IMDSv6
    )
)
_METADATA_HOSTNAMES = frozenset({
    "metadata.google.internal", "metadata.goog", "metadata.azure.com",
    "instance-data", "instance-data.ec2.internal", "metadata.tencent-cloud.com",
})

_HOST_RE = re.compile(r"^[a-z0-9]([a-z0-9._-]*[a-z0-9])?$", re.IGNORECASE)
_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://")
_IPV4_LITERAL_RE = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")

_WHITESPACE = " \t\r\n\u3000\u200b\ufeff"


def _public_mode() -> bool:
    return os.environ.get("STUDY_PUBLIC_MODE", "").strip().lower() in {"1", "true", "yes", "on"}


def url_policy() -> str:
    """当前 URL 放行策略；非法值回退为按运行模式推导的默认策略。"""
    configured = os.environ.get("STUDY_AI_URL_POLICY", "").strip().lower()
    if configured in POLICIES:
        return configured
    return "public_https" if _public_mode() else "local_open"


def requires_pinning(policy: str | None = None) -> bool:
    return (policy or url_policy()) in PUBLIC_POLICIES


# ---------------------------------------------------------------------------
# 地址判断
# ---------------------------------------------------------------------------

def _parse_ip(value: str):
    text = str(value or "").strip().strip("[]")
    if not text:
        return None
    try:
        ip = ipaddress.ip_address(text)
    except ValueError:
        return None
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    return ip


def is_metadata_ip(value) -> bool:
    """是否为云元数据服务的固定 IP（任何策略下都禁止）。"""
    ip = _parse_ip(str(value or ""))
    if ip is None:
        return False
    return any(ip in network for network in _METADATA_NETWORKS)


def _normalize_hostname(host: str) -> str:
    return (host or "").strip().strip("[]").rstrip(".").lower()


def is_metadata_host(host: str) -> bool:
    host = _normalize_hostname(host)
    if not host:
        return False
    if host in _METADATA_HOSTNAMES:
        return True
    return is_metadata_ip(host)


def is_local_host(host: str) -> bool:
    """回环 / 私网 / 链路本地 / 单机名（用于本地模式补 http:// 与 Ollama 回退判断）。"""
    host = _normalize_hostname(host)
    if not host:
        return False
    if host in {"localhost", "localhost.localdomain", "host.docker.internal",
                "gateway.docker.internal", "host.containers.internal"}:
        return True
    if host.endswith(".localhost") or host.endswith(".local") or host.endswith(".internal"):
        return True
    ip = _parse_ip(host)
    if ip is not None:
        return bool(ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_unspecified)
    return False


def _is_global_ip(value) -> bool:
    ip = _parse_ip(str(value or ""))
    if ip is None:
        return False
    if is_metadata_ip(ip):
        return False
    return bool(ip.is_global)


def host_allowed(host: str, allowed_hosts: Iterable[str] | None) -> bool:
    """白名单匹配：精确域名，或 ``*.example.com`` 形式的通配符。"""
    host = _normalize_hostname(host)
    if not host:
        return False
    for raw in allowed_hosts or ():
        pattern = _normalize_hostname(str(raw))
        if not pattern:
            continue
        if pattern == host:
            return True
        if pattern.startswith("*.") and fnmatch.fnmatchcase(host, pattern):
            return True
        if pattern.startswith(".") and host.endswith(pattern):
            return True
    return False


def suggested_allowlist_entry(host: str) -> str:
    return _normalize_hostname(host)


# ---------------------------------------------------------------------------
# 规范化
# ---------------------------------------------------------------------------

def _strip_tail_path(path: str) -> tuple[str, str | None]:
    """剥离误粘贴的接口尾部路径，返回 (新路径, 被剥离的后缀)。"""
    cleaned = path or ""
    removed = None
    changed = True
    while changed:
        changed = False
        trimmed = cleaned.rstrip("/")
        gemini_match = _GEMINI_MODEL_PATH_RE.search(trimmed)
        if gemini_match:
            removed = gemini_match.group(0) if removed is None else removed
            cleaned = trimmed[: gemini_match.start()]
            changed = True
            continue
        lowered = trimmed.lower()
        for suffix in _TAIL_SUFFIXES:
            if lowered.endswith(suffix):
                candidate = trimmed[: -len(suffix)]
                # 路径为空或只剩 "/" 时不保留末端斜杠
                if candidate in ("", "/"):
                    candidate = ""
                removed = trimmed[-len(suffix):] if removed is None else removed
                cleaned = candidate
                changed = True
                break
    cleaned = cleaned.rstrip("/")
    if cleaned in ("", "/"):
        cleaned = ""
    return cleaned, removed


def normalize_base_url(value) -> dict:
    """规范化用户填写的 API 地址。

    返回 ``{"normalized": str, "notes": [str, ...]}``；不抛异常（格式问题留给
    :func:`validate_base_url` 统一给出中文 422 提示）。
    """
    notes: list[str] = []
    raw = str(value or "")
    for character in _WHITESPACE:
        raw = raw.replace(character, " ")
    raw = raw.strip()
    if not raw:
        return {"normalized": "", "notes": notes}
    if raw.startswith("//"):
        # 协议相对地址（//relay.example/v1）按 https:// 处理。
        raw = "https:" + raw
    # 去掉用户复制的多协议前缀（https://https://...）。
    while _SCHEME_RE.match(raw) and raw.lower().count("://") > 1:
        _, _, rest = raw.partition("://")
        raw = rest
    if not _SCHEME_RE.match(raw):
        # 未写协议：本地地址补 http://，其余提示并补 https://。
        candidate = raw.split("/", 1)[0]
        host_part = candidate.rsplit("@", 1)[-1]
        if host_part.startswith("["):
            host_part = host_part[1:].split("]", 1)[0]
        else:
            host_part = host_part.split(":", 1)[0]
        if url_policy() == "local_open" and is_local_host(host_part):
            raw = "http://" + raw
            notes.append("已自动补全协议 http://（本地/局域网地址）")
        else:
            raw = "https://" + raw
            notes.append("已自动补全协议 https://，如厂商要求 http 请显式填写")
    try:
        parts = urlsplit(raw)
    except ValueError:
        return {"normalized": raw.rstrip("/"), "notes": notes}
    path, removed_suffix = _strip_tail_path(parts.path)
    if removed_suffix:
        notes.append(
            f"已自动移除误粘贴的接口路径 {removed_suffix}，最终请求地址会自动拼接该端点"
        )
    elif path != (parts.path or ""):
        notes.append("已移除地址尾部斜杠")
    # 保留 query（Azure api-version 等）与 base 前缀，仅规范尾部斜杠。
    normalized = urlunsplit((parts.scheme.lower(), parts.netloc, path, parts.query, parts.fragment))
    if normalized != raw.rstrip("/") and not removed_suffix:
        notes.append("已规范地址格式（去除尾部斜杠/多余空白）")
    return {"normalized": normalized, "notes": notes}


# ---------------------------------------------------------------------------
# DNS 解析
# ---------------------------------------------------------------------------

def resolve_host(host: str, port: int | None = None) -> tuple[str, ...]:
    """解析主机名到去重后的 IP 列表；解析失败抛 422。"""
    host = _normalize_hostname(host)
    if not host:
        raise HTTPException(422, "API 地址缺少主机名")
    literal = _parse_ip(host)
    if literal is not None:
        return (str(literal),)
    try:
        infos = socket.getaddrinfo(host, port or 443, type=socket.SOCK_STREAM)
    except OSError:
        raise HTTPException(422, "无法解析模型服务域名") from None
    addresses: list[str] = []
    for info in infos or ():
        try:
            raw = str(info[4][0]).split("%", 1)[0]
            ip = ipaddress.ip_address(raw)
        except (IndexError, TypeError, ValueError):
            continue
        text = str(ip)
        if text not in addresses:
            addresses.append(text)
    if not addresses:
        raise HTTPException(422, "无法解析模型服务域名")
    return tuple(addresses)


def _reject_non_public_ip(ip: str) -> None:
    if is_metadata_ip(ip):
        raise HTTPException(422, "禁止访问云元数据地址，请检查 API 地址")
    if not _is_global_ip(ip):
        raise HTTPException(422, "模型服务不能指向本地或私有网络（公网模式仅允许公网地址）")


# ---------------------------------------------------------------------------
# 校验
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ValidatedEndpoint:
    url: str
    host: str
    port: int
    scheme: str
    policy: str
    ips: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()

    @property
    def pinned(self) -> bool:
        return self.policy in PUBLIC_POLICIES and bool(self.ips)


def _validate_static(parts, *, policy: str, allowlist: Iterable[str] | None) -> None:
    scheme = (parts.scheme or "").lower()
    host = _normalize_hostname(parts.hostname or "")
    if not host:
        raise HTTPException(422, "API 地址缺少有效主机名")
    if parts.username is not None or parts.password is not None:
        raise HTTPException(422, "API 地址不能包含用户名或密码，请把凭据填写在 API Key 一栏")
    if parts.fragment:
        raise HTTPException(422, "API 地址不能包含 # 片段")
    if scheme not in ("http", "https"):
        raise HTTPException(422, "API 地址仅支持 http:// 或 https:// 协议")
    if is_metadata_host(host):
        raise HTTPException(422, "禁止访问云元数据地址，请检查 API 地址")
    if _parse_ip(host) is None and not _HOST_RE.match(host):
        raise HTTPException(422, "API 地址格式无效")
    if policy in PUBLIC_POLICIES:
        if scheme != "https":
            raise HTTPException(
                422,
                "公网模式仅允许 HTTPS 地址；本地/局域网模型请在本地模式下使用",
            )
        for key, _value in parse_qsl(parts.query, keep_blank_values=True):
            if key.strip().lower() in _SENSITIVE_QUERY_KEYS:
                raise HTTPException(422, "请不要把 API Key 放在地址查询参数中，请填写到 API Key 一栏")
        if policy == "allowlist" and not host_allowed(host, allowlist):
            raise HTTPException(422, {
                "code": "url_not_allowed",
                "message": "该域名未加入 AI 白名单，请管理员先在设置页添加或配置 STUDY_AI_ALLOWED_HOSTS",
                "suggested_allowlist_entry": suggested_allowlist_entry(host),
            })
    # 端口范围由调用方读取 urlsplit().port 时校验（非法端口会抛 ValueError）。


def validate_endpoint(
    value,
    *,
    resolve: bool = False,
    policy: str | None = None,
    allowed_hosts: Iterable[str] | None = None,
) -> ValidatedEndpoint:
    """按当前策略校验地址，返回含 DNS（如需要）结果的端点信息。"""
    policy = (policy or url_policy()).lower()
    if policy not in POLICIES:  # pragma: no cover - url_policy 已回退
        policy = "local_open"
    normalized_info = normalize_base_url(value)
    normalized = normalized_info["normalized"]
    notes = list(normalized_info["notes"])
    if not normalized:
        raise HTTPException(422, "API 地址不能为空")
    try:
        parts = urlsplit(normalized)
        port = parts.port
    except ValueError:
        raise HTTPException(422, "API 地址格式无效（端口不是有效数字）") from None
    if any(character in normalized for character in "\r\n\t"):
        raise HTTPException(422, "API 地址不能包含换行或制表符")
    if any(character.isspace() for character in normalized):
        raise HTTPException(422, "API 地址不能包含空格，请检查是否误粘贴了说明文字")
    _validate_static(parts, policy=policy, allowlist=allowed_hosts)
    host = _normalize_hostname(parts.hostname or "")
    scheme = parts.scheme.lower()
    effective_port = port or DEFAULT_PORTS.get(scheme, 443)
    ips: tuple[str, ...] = ()
    if resolve:
        literal = _parse_ip(host)
        if literal is not None:
            ips = (str(literal),)
        else:
            try:
                ips = resolve_host(host, effective_port)
            except HTTPException:
                if policy in PUBLIC_POLICIES:
                    raise
                # 本地模式解析失败不阻断（离线/内网 DNS 场景），仅做元数据检查的尽力而为。
                ips = ()
        for ip in ips:
            if is_metadata_ip(ip):
                raise HTTPException(422, "禁止访问云元数据地址，请检查 API 地址")
            if policy in PUBLIC_POLICIES:
                _reject_non_public_ip(ip)
    return ValidatedEndpoint(
        url=normalized, host=host, port=effective_port, scheme=scheme,
        policy=policy, ips=tuple(ips), notes=tuple(notes),
    )


def validate_base_url(
    value,
    *,
    resolve: bool = False,
    policy: str | None = None,
    allowed_hosts: Iterable[str] | None = None,
) -> str:
    """校验并返回规范化后的 API 基地址；非法地址抛 422。"""
    return validate_endpoint(
        value, resolve=resolve, policy=policy, allowed_hosts=allowed_hosts,
    ).url


# ---------------------------------------------------------------------------
# 端点拼接 / 脱敏
# ---------------------------------------------------------------------------

def join_endpoint(base_url: str, suffix: str, *, params: Mapping[str, str] | None = None) -> str:
    """把接口后缀拼到基地址上，保留原有 path/query（Azure api-version）。"""
    parts = urlsplit(str(base_url or "").strip())
    base_path = (parts.path or "").rstrip("/")
    if suffix:
        base_path = base_path + "/" + str(suffix).lstrip("/")
    query = parts.query
    if params:
        merged = dict(parse_qsl(query, keep_blank_values=True))
        merged.update({str(k): str(v) for k, v in params.items() if v is not None})
        query = urlencode(list(merged.items()))
    return urlunsplit((parts.scheme, parts.netloc, base_path, query, ""))


_SECRET_PATTERNS = (
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{4,}"), "sk-***"),
    (re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._\-]{4,}"), "Bearer ***"),
    (re.compile(r"(?i)\b(x-api-key|api-key|api_key|authorization)\b\s*[:=]\s*['\"]?[^\s'\",;}]+"),
     r"\1: ***"),
    (re.compile(r"(?i)((?:api[_-]?key|access[_-]?token|token|secret|password)\s*[\"']?\s*[:=]\s*[\"']?)[^\s'\",;}]+"),
     r"\1***"),
)


def mask_secrets(text) -> str:
    """把错误文本里的 Key/Token 片段替换为 ***。"""
    masked = str(text or "")
    for pattern, replacement in _SECRET_PATTERNS:
        masked = pattern.sub(replacement, masked)
    return masked


def mask_url(url: str) -> str:
    """脱敏 URL 中的 query Key 与 userinfo。"""
    try:
        parts = urlsplit(str(url or ""))
    except ValueError:
        return mask_secrets(str(url or ""))
    netloc = parts.netloc
    if "@" in netloc:
        netloc = "***@" + netloc.rsplit("@", 1)[1]
    query_items = []
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        if key.strip().lower() in _SENSITIVE_QUERY_KEYS:
            query_items.append(f"{key}=***")
        else:
            query_items.append(f"{key}={value}")
    masked = urlunsplit((parts.scheme, netloc, parts.path, "&".join(query_items), parts.fragment))
    return mask_secrets(masked)


# ---------------------------------------------------------------------------
# IP Pinning
# ---------------------------------------------------------------------------

class PinnedNetworkBackend(httpcore.AnyIOBackend if httpcore is not None else object):
    """把已校验的目标 IP 固定用于 TCP 连接，URL/Host/SNI 仍是原始域名。"""

    def __init__(self, host: str, ips: Sequence[str]):
        targets = [str(ip).strip() for ip in ips if str(ip).strip()]
        if not targets:
            raise ValueError("没有可用于 IP Pinning 的地址")
        self.host = _normalize_hostname(host)
        self.targets = tuple(dict.fromkeys(targets))
        self._index = 0

    def next_target(self) -> str:
        target = self.targets[self._index % len(self.targets)]
        self._index += 1
        return target

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        # host 是 URL 中的原始域名；这里只替换实际 TCP 目标，SNI/证书仍按域名校验。
        target = self.next_target()
        return await super().connect_tcp(
            target, port, timeout=timeout, local_address=local_address,
            socket_options=socket_options,
        )

    async def connect_unix_socket(self, *args, **kwargs):  # pragma: no cover - 不使用 UDS
        return await super().connect_unix_socket(*args, **kwargs)


class PinnedHTTPTransport(httpx.AsyncHTTPTransport):
    """带 IP Pinning 的异步 HTTP 传输；HTTPS 用原始域名做 SNI 与证书校验。"""

    def __init__(self, *, host: str, ips: Sequence[str], trust_env: bool = False, **kwargs):
        self.pinned_host = _normalize_hostname(host)
        self.pinned_ips = tuple(str(ip) for ip in ips)
        super().__init__(trust_env=trust_env, **kwargs)
        self.network_backend = PinnedNetworkBackend(self.pinned_host, self.pinned_ips)
        pool = getattr(self, "_pool", None)
        if pool is None:  # pragma: no cover - httpx 版本差异
            raise RuntimeError("当前 httpx 版本不支持 IP Pinning transport")
        pool._network_backend = self.network_backend

    async def handle_async_request(self, request):
        request.extensions.setdefault("sni_hostname", self.pinned_host)
        return await super().handle_async_request(request)


def pinned_transport(host: str, port: int | None, ips: Iterable[str]) -> httpx.AsyncBaseTransport:
    """构造绑定已校验 IP 的 httpx transport（HTTPS 保持 SNI/证书校验）。"""
    if httpcore is None:  # pragma: no cover - httpx 安装即自带 httpcore
        raise RuntimeError("当前环境缺少 httpcore，无法启用 IP Pinning")
    targets = [str(ip).strip() for ip in ips or () if str(ip).strip()]
    if not targets:
        raise ValueError("没有可用于 IP Pinning 的地址")
    transport = PinnedHTTPTransport(host=host, ips=targets, trust_env=False, retries=0)
    if port:
        transport.pinned_port = int(port)
    return transport


def resolve_and_pin(url: str, *, policy: str | None = None) -> httpx.AsyncBaseTransport | None:
    """公网/白名单模式下解析并校验 DNS，再返回 IP Pinning transport；本地模式返回 None。"""
    policy = policy or url_policy()
    if policy not in PUBLIC_POLICIES:
        return None
    try:
        parts = urlsplit(str(url or ""))
        port = parts.port
    except ValueError:
        raise HTTPException(422, "API 地址格式无效（端口不是有效数字）") from None
    host = _normalize_hostname(parts.hostname or "")
    if not host:
        raise HTTPException(422, "API 地址缺少有效主机名")
    effective_port = port or DEFAULT_PORTS.get((parts.scheme or "https").lower(), 443)
    ips = resolve_host(host, effective_port)
    for ip in ips:
        _reject_non_public_ip(ip)
    return pinned_transport(host, effective_port, ips)


def client_kwargs(
    url: str,
    *,
    timeout_seconds: float = 120.0,
    connect_timeout: float = 15.0,
    use_proxy_env: bool = False,
    policy: str | None = None,
    follow_redirects: bool = False,
) -> dict:
    """为 httpx.AsyncClient 构造请求参数（策略要求时附带 IP Pinning transport）。"""
    policy = policy or url_policy()
    timeout_seconds = max(1.0, float(timeout_seconds or 120.0))
    connect_timeout = max(1.0, min(float(connect_timeout or 15.0), timeout_seconds))
    kwargs: dict = {
        "timeout": httpx.Timeout(timeout_seconds, connect=connect_timeout),
        "follow_redirects": follow_redirects,
        "trust_env": bool(use_proxy_env) and policy == "local_open",
    }
    transport = resolve_and_pin(url, policy=policy)
    if transport is not None:
        kwargs["transport"] = transport
        kwargs["trust_env"] = False
    return kwargs


__all__ = [
    "POLICIES", "PUBLIC_POLICIES", "ValidatedEndpoint", "PinnedHTTPTransport",
    "PinnedNetworkBackend", "client_kwargs", "host_allowed", "is_local_host",
    "is_metadata_host", "is_metadata_ip", "join_endpoint", "mask_secrets",
    "mask_url", "normalize_base_url", "pinned_transport", "requires_pinning",
    "resolve_and_pin", "resolve_host", "suggested_allowlist_entry",
    "url_policy", "validate_base_url", "validate_endpoint",
]
