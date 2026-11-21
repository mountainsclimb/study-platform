"""公网部署配置自检（WS6 / P0「公网模式的部署默认值需要显式收紧」）。

背景
----
docker-compose 的本地默认值（信任任意转发来源、示例 Redis 密码、localhost Host、
本地开放的 AI 出站地址）不能让公网部署悄悄生效。本模块提供一套**纯标准库**
的校验规则，供三处复用：

* :func:`check_config`：对给定环境变量做检查，返回结构化错误/告警，供测试与脚本使用；
* :func:`enforce_public_config`：FastAPI lifespan 启动时调用。公网模式下显式危险的
  配置直接拒绝启动（fail-closed）；本地模式（``STUDY_PUBLIC_MODE`` 非真值）完全不触发，
  开发与测试默认值保持可用；
* ``scripts/check_public_config.py``：部署前读取 env/.env 做同一套校验，退出码可判。
  脚本默认更严格：应用启动时只强告警的「缺失生产依赖」在部署前检查里算错误。

严重级别
--------
* ``error``：显式危险或明确缺失关键路径的公网配置（``FORWARDED_ALLOW_IPS=*``、
  示例密码、仅本地 Host、``STUDY_AI_URL_POLICY=local_open``、没有管理员初始化路径等），
  应用拒绝启动。
* ``warning``：缺失或不完整、但不会立刻造成已知漏洞的生产依赖（例如未配置
  ``DATABASE_URL`` 时退回 SQLite、未显式声明转发来源）。启动时强告警；``strict=True``
  时升级为错误（部署前脚本默认如此）。

检查范围（公网模式）
--------------------
1. ``STUDY_ALLOWED_HOSTS``：显式、非通配、不能只有本地/回环地址；``STUDY_DOMAIN``
   （Caddy 对外域名）必须命中该白名单；
2. ``FORWARDED_ALLOW_IPS``：禁止 ``*`` 与 0 前缀网段，必须是显式反代 IP/CIDR；
3. ``DATABASE_URL``：必须是 PostgreSQL，密码不能是示例/占位值；
4. ``REDIS_URL`` / ``REDIS_PASSWORD``：密码不能是示例/占位值，不允许空密码；
5. ``STUDY_ADMIN_INIT_TOKEN``：已配置且不是示例/过短口令，或数据库里已有管理员；
6. ``STUDY_AI_URL_POLICY``：只能是 ``public_https`` / ``allowlist``，禁止 ``local_open``。
"""
from __future__ import annotations

from dataclasses import dataclass
import fnmatch
import ipaddress
import logging
import os
import re
from typing import Callable, Mapping
from urllib.parse import unquote, urlsplit

_log = logging.getLogger("zhixu.config")

PUBLIC_MODE_VALUES = frozenset({"1", "true", "yes", "on"})
LOCAL_MODE_VALUES = frozenset({"0", "false", "no", "off"})
AI_POLICIES = ("local_open", "public_https", "allowlist")
POSTGRES_SCHEMES = ("postgres", "postgresql")
REDIS_SCHEMES = ("redis", "rediss")

_LOCAL_HOSTNAMES = frozenset({
    "localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback",
    "host.docker.internal", "gateway.docker.internal", "host.containers.internal",
})
_LOCAL_HOST_SUFFIXES = (".localhost", ".local", ".localdomain", ".internal")

# 常见示例/占位密码：公网部署沿用会让服务被直接猜中。
_SAMPLE_SECRETS = frozenset({
    "changeme", "change-me", "change_me", "please-change-me", "please_change_me",
    "pleasechangeme", "password", "passwd", "secret", "example", "demo", "test",
})
_PLACEHOLDER_MARKERS = (
    "${", "<", "your-", "your_", "placeholder", "changeme", "change-me",
    "please-change", "please_change", "xxx", "example", "replace-me", "replace_me",
)

_METADATA_HOSTS = frozenset({
    "metadata.google.internal", "metadata.goog", "metadata.azure.com",
    "instance-data", "instance-data.ec2.internal", "metadata.tencent-cloud.com",
})
_METADATA_NETWORKS = tuple(
    ipaddress.ip_network(value)
    for value in (
        "169.254.169.254/32", "169.254.170.2/32", "169.254.169.253/32",
        "100.100.100.200/32", "fd00:ec2::254/128",
    )
)

_HOST_ENTRY_RE = re.compile(r"^[a-z0-9]([a-z0-9._-]*[a-z0-9])?$")


class ConfigGuardError(RuntimeError):
    """公网配置自检失败：应用必须拒绝启动（fail-closed）。"""

    def __init__(self, result: "GuardResult"):
        self.result = result
        super().__init__(result.format_report())


@dataclass(frozen=True)
class Issue:
    """一条自检结果；``fix`` 是可直接照做的修复提示。"""

    level: str  # "error" | "warning"
    key: str
    message: str
    fix: str = ""

    def as_dict(self) -> dict:
        return {"level": self.level, "key": self.key, "message": self.message, "fix": self.fix}


@dataclass(frozen=True)
class GuardResult:
    """一次公网配置自检的结果。"""

    public_mode: bool
    issues: tuple[Issue, ...] = ()

    @property
    def errors(self) -> tuple[Issue, ...]:
        return tuple(item for item in self.issues if item.level == "error")

    @property
    def warnings(self) -> tuple[Issue, ...]:
        return tuple(item for item in self.issues if item.level == "warning")

    @property
    def ok(self) -> bool:
        """没有 error 即视为可启动；warning 只提示不阻断。"""
        return not self.errors

    def as_dict(self) -> dict:
        return {
            "public_mode": self.public_mode,
            "ok": self.ok,
            "errors": [item.as_dict() for item in self.errors],
            "warnings": [item.as_dict() for item in self.warnings],
        }

    def format_report(self) -> str:
        if not self.public_mode:
            lines = ["[公网配置自检] 未启用公网模式（STUDY_PUBLIC_MODE 非真值），公网检查未触发。"]
        else:
            status = "通过" if self.ok else "失败"
            lines = [
                "[公网配置自检] %s：%d 个错误，%d 个告警"
                % (status, len(self.errors), len(self.warnings))
            ]
            if not self.issues:
                lines.append("  公网关键配置均已显式收紧。")
        ordered = self.errors + self.warnings
        for index, item in enumerate(ordered):
            if not self.public_mode and index == 0:
                lines.append("  本地模式提示：")
            tag = "错误" if item.level == "error" else "告警"
            lines.append("- [%s] %s：%s" % (tag, item.key, item.message))
            if item.fix:
                lines.append("  修复：%s" % item.fix)
        return "\n".join(lines)


class _Report:
    """收集检查项；``strict`` 时把标记为 promote 的告警升级为错误。"""

    def __init__(self, public_mode: bool, strict: bool):
        self.public_mode = public_mode
        self.strict = strict
        self.issues: list[Issue] = []

    def error(self, key: str, message: str, fix: str = "") -> None:
        self.issues.append(Issue("error", key, message, fix))

    def warn(self, key: str, message: str, fix: str = "", *, promote: bool = False) -> None:
        level = "error" if (promote and self.strict) else "warning"
        self.issues.append(Issue(level, key, message, fix))

    def result(self) -> GuardResult:
        return GuardResult(self.public_mode, tuple(self.issues))


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------

def public_mode_enabled(env: Mapping[str, str] | None = None) -> bool:
    """与 ``app.auth._public_mode`` 保持一致的真值判断。"""
    value = (os.environ if env is None else env).get("STUDY_PUBLIC_MODE", "")
    return str(value).strip().lower() in PUBLIC_MODE_VALUES


def _text(env: Mapping[str, str], key: str) -> str:
    return str(env.get(key, "") or "").strip()


def _entries(raw: str) -> list[str]:
    return [item.strip() for item in str(raw or "").split(",") if item.strip()]


def _normalize_host(value) -> str:
    host = str(value or "").strip().lower()
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    return host.rstrip(".")


def _is_local_host(value) -> bool:
    """回环/私网/链路本地/单机名/内部域：公网 Host 与 AI 地址都不应出现。"""
    host = _normalize_host(value)
    if not host:
        return False
    if host in _LOCAL_HOSTNAMES:
        return True
    if any(host.endswith(suffix) for suffix in _LOCAL_HOST_SUFFIXES):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return bool(
        ip.is_private or ip.is_loopback or ip.is_link_local
        or ip.is_unspecified or (ip.version == 6 and ip.ipv4_mapped is not None)
    )


def _is_metadata_host(value) -> bool:
    host = _normalize_host(value)
    if not host:
        return False
    if host in _METADATA_HOSTS:
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return any(ip in network for network in _METADATA_NETWORKS)


def _valid_host_entry(value) -> bool:
    """Host 项只接受域名/IP 字面量，不接受协议、路径、端口或空白。"""
    raw = str(value or "").strip()
    host = _normalize_host(raw)
    if not host or " " in host or "/" in host or "://" in host or host.startswith("."):
        return False
    if raw.startswith("["):
        try:
            ipaddress.IPv6Address(host)
        except ValueError:
            return False
        return True
    if ":" in host:  # IPv4:port 或未加方括号的 IPv6
        return False
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        pass
    return bool(_HOST_ENTRY_RE.match(host))


def _secret_problem(value) -> str:
    """返回「示例/占位密钥」的原因；空值返回空串（由调用方单独判断缺失）。"""
    text = str(value or "").strip()
    if not text:
        return ""
    lowered = text.lower()
    if lowered in _SAMPLE_SECRETS:
        return "是示例值"
    for marker in _PLACEHOLDER_MARKERS:
        if marker in lowered:
            return "含占位/示例标记 %r" % marker
    return ""


def _check_public_mode_value(env: Mapping[str, str], report: _Report) -> None:
    value = _text(env, "STUDY_PUBLIC_MODE").lower()
    if value and value not in PUBLIC_MODE_VALUES and value not in LOCAL_MODE_VALUES:
        report.warn(
            "STUDY_PUBLIC_MODE",
            "无法识别的值 %r：应用按本地模式运行（公网部署会意外保留全部本地默认值）" % value,
            "使用 1 表示公网模式、0 表示本地模式",
            promote=True,
        )


# ---------------------------------------------------------------------------
# 逐项检查
# ---------------------------------------------------------------------------

def _check_allowed_hosts(env: Mapping[str, str], report: _Report) -> None:
    key = "STUDY_ALLOWED_HOSTS"
    raw = _text(env, key)
    if not raw:
        report.error(
            key,
            "公网模式必须显式配置允许的 Host（未配置时应用只接受回环 Host，公网域名会被拒绝）",
            "在 .env 中设置公网域名，例如 STUDY_ALLOWED_HOSTS=zhixu.example.com,www.zhixu.example.com",
        )
        return
    if "${" in raw:
        report.error(
            key,
            "包含未展开的变量占位符（${...}）",
            "确认 .env 已提供真实域名（Compose 会先展开变量）",
        )
        return
    entries = _entries(raw)
    if not entries:
        report.error(key, "配置为空", "至少填写一个公网域名，例如 zhixu.example.com")
        return
    invalid = [item for item in entries if not _valid_host_entry(item)]
    if invalid:
        report.error(
            key,
            "包含无效的 Host 项：%s" % ", ".join(invalid),
            "只填写域名或 IP（IPv6 用方括号），不要带协议、路径、端口或空格",
        )
    wildcards = [item for item in entries if "*" in item]
    if wildcards:
        report.error(
            key,
            "公网模式不允许通配符 Host：%s" % ", ".join(wildcards),
            "逐一列出实际对外域名；通配符会让 Host 头校验形同虚设",
        )
    local = [item for item in entries if _is_local_host(item)]
    if local and len(local) == len(entries):
        report.error(
            key,
            "只包含本地/回环地址：%s（公网域名没有被允许，部署后无法访问）" % ", ".join(local),
            "改为实际公网域名；localhost 只用于本机调试",
        )
    elif local:
        report.warn(
            key,
            "包含本地/回环地址：%s（正式部署保留这些项会扩大 Host 校验面）" % ", ".join(local),
            "公网部署只保留实际对外域名，把 localhost/127.0.0.1 留给本地调试配置",
            promote=True,
        )
    single_label = [
        item for item in entries
        if _valid_host_entry(item)
        and not _is_local_host(item)
        and _normalize_host(item)
        and "." not in _normalize_host(item)
    ]
    if single_label:
        report.warn(
            key,
            "包含单标签主机名：%s（公网部署建议使用完整域名）" % ", ".join(single_label),
            "使用解析到服务器的完整域名，例如 zhixu.example.com",
        )


def _check_public_domain(env: Mapping[str, str], report: _Report) -> None:
    """Caddy 对外域名必须命中 STUDY_ALLOWED_HOSTS，否则反向代理转发后会被 Host 校验拒绝。"""
    domain = _text(env, "STUDY_DOMAIN")
    allowed = _entries(_text(env, "STUDY_ALLOWED_HOSTS"))
    if not domain or not allowed:
        return
    host = _normalize_host(domain)
    if not host:
        return
    normalized = [_normalize_host(item) for item in allowed]
    matched = host in normalized or any(
        entry.startswith("*.") and fnmatch.fnmatchcase(host, entry)
        for entry in normalized
    )
    if matched:
        return
    message = (
        "Caddy 对外域名 %r 不在 STUDY_ALLOWED_HOSTS 中：反向代理转发后会被 Host 校验拒绝"
        % host
    )
    fix = "把 %s 加入 STUDY_ALLOWED_HOSTS，或把 STUDY_DOMAIN 改成其中一个域名" % host
    if _deployment_layout(env):
        report.error("STUDY_DOMAIN", message, fix)
    else:
        report.warn("STUDY_DOMAIN", message, fix, promote=True)


def _check_forwarded_allow_ips(env: Mapping[str, str], report: _Report) -> None:
    key = "FORWARDED_ALLOW_IPS"
    raw = _text(env, key)
    if not raw:
        fix = "设置为反代所在网段或 IP，例如 FORWARDED_ALLOW_IPS=172.16.0.0/12；禁止使用 *"
        if _deployment_layout(env):
            report.error(key, "公网部署必须显式声明可信反向代理来源（不能信任任意 X-Forwarded-*）", fix)
        else:
            report.warn(
                key,
                "未显式声明可信反向代理来源：uvicorn 只信任 127.0.0.1，反代的 X-Forwarded-* 会被忽略"
                "（客户端 IP 与登录限流可能退化为代理地址）",
                fix,
                promote=True,
            )
        return
    if "${" in raw:
        report.error(key, "包含未展开的变量占位符（${...}）", "确认 .env 已提供真实的代理地址/CIDR")
        return
    entries = _entries(raw)
    if not entries:
        report.error(key, "配置为空", "填写反代 IP（如 127.0.0.1）或网段（如 172.16.0.0/12）")
        return
    if any(item == "*" for item in entries):
        report.error(
            key,
            "不能信任任意转发来源（*）：任何人都能伪造 X-Forwarded-For 冒充客户端 IP",
            "显式列出反代 IP/网段（例如 Caddy 所在 docker 网段 172.16.0.0/12），只信任这些来源",
        )
        return
    invalid: list[str] = []
    for entry in entries:
        try:
            if "/" in entry:
                network = ipaddress.ip_network(entry, strict=False)
                if network.prefixlen == 0:
                    report.error(
                        key,
                        "网段 %s 覆盖全部地址，等同于 *" % entry,
                        "改为只覆盖反代所在网段的 CIDR，例如 172.16.0.0/12",
                    )
            else:
                ipaddress.ip_address(entry)
        except ValueError:
            invalid.append(entry)
    if invalid:
        report.error(
            key,
            "包含无法识别的 IP/CIDR：%s" % ", ".join(invalid),
            "填写反代 IP（如 127.0.0.1）或网段（如 172.16.0.0/12），多个用逗号分隔",
        )


def _deployment_layout(env: Mapping[str, str]) -> bool:
    """是否像真实部署（Compose 等）而不是本地开发/测试。

    两种信号（任一即可）：

    * 显式 ``STUDY_DEPLOYMENT_LAYOUT`` 真值（Compose 会设置，非标准目录的部署也可用）；
    * 容器内的标准目录布局：``STUDY_DATA_DIR=/data`` 或 ``STUDY_BANK_DIR=/bank`` 及其子目录。

    只设置 ``STUDY_DATA_DIR``/``STUDY_BANK_DIR`` 指向本机普通路径（例如开发和 CI 固定题库到
    ``E:\\...\\releases\\bank-xxx``）**不算**部署布局，否则本地/测试在公网模式下会被误伤。
    """
    if _text(env, "STUDY_DEPLOYMENT_LAYOUT").strip().lower() in PUBLIC_MODE_VALUES:
        return True

    def _container_path(key: str, root: str) -> bool:
        value = _text(env, key).strip().replace("\\", "/").rstrip("/")
        return value == root or value.startswith(root + "/")

    return _container_path("STUDY_DATA_DIR", "/data") or _container_path("STUDY_BANK_DIR", "/bank")


def _check_database_url(env: Mapping[str, str], report: _Report) -> None:
    key = "DATABASE_URL"
    raw = _text(env, key)
    if not raw:
        fix = (
            "设置 PostgreSQL 连接串，例如 DATABASE_URL=postgresql://zhixu:<强密码>@postgres:5432/zhixu；"
            "Compose 会用 PG_PASSWORD 自动拼接"
        )
        if _deployment_layout(env):
            report.error(
                key,
                "公网部署必须使用 PostgreSQL：未配置 DATABASE_URL 会退回 SQLite 单文件库，"
                "多进程、备份与恢复都不适合公网",
                fix,
            )
        else:
            report.warn(
                key,
                "公网模式未配置 DATABASE_URL，将退回 SQLite 单文件库（多进程、备份与恢复都不适合公网）",
                fix,
                promote=True,
            )
        return
    if "${" in raw:
        report.error(key, "包含未展开的变量占位符（${...}）", "确认 .env 已提供真实密码（Compose 会先展开 PG_PASSWORD）")
        return
    try:
        parts = urlsplit(raw)
    except ValueError:
        report.error(key, "连接串格式无效", "使用 postgresql://用户:密码@主机:5432/数据库 形式")
        return
    scheme = (parts.scheme or "").lower()
    if scheme not in POSTGRES_SCHEMES:
        report.error(
            key,
            "公网模式必须使用 PostgreSQL，当前协议为 %r" % (scheme or "未知"),
            "把 DATABASE_URL 改为 postgresql:// 连接串；SQLite 只用于本地开发",
        )
        return
    password = unquote(parts.password or "")
    if not password:
        report.warn(
            key,
            "PostgreSQL 连接串没有密码",
            "设置数据库强密码；Compose 中 PG_PASSWORD 为必填项",
            promote=True,
        )
        return
    problem = _secret_problem(password)
    if problem:
        report.error(
            key,
            "DATABASE_URL 的密码%s，公网可被直接猜中" % problem,
            "生成至少 24 位随机密码并同步到 PG_PASSWORD / DATABASE_URL",
        )
    elif len(password) < 12:
        report.warn(
            key,
            "PostgreSQL 密码长度不足 12 位",
            "使用至少 24 位随机密码",
            promote=True,
        )


def _check_redis_url(env: Mapping[str, str], report: _Report) -> None:
    redis_password_env = _text(env, "REDIS_PASSWORD")
    if redis_password_env:
        problem = _secret_problem(redis_password_env)
        if problem:
            report.error(
                "REDIS_PASSWORD",
                "REDIS_PASSWORD %s，公网可被直接猜中" % problem,
                "生成至少 24 位随机密码；Compose 会用该密码启动 requirepass 并拼进 REDIS_URL",
            )
    key = "REDIS_URL"
    raw = _text(env, key)
    if not raw:
        fix = "按 README 启用 Redis（Compose 由 REDIS_PASSWORD 自动拼接），或明确接受公网关闭 AI 队列"
        if _deployment_layout(env):
            report.error(key, "公网部署缺少 REDIS_URL：AI 任务队列、Worker 心跳与任务恢复不可用", fix)
        else:
            report.warn(
                key,
                "未配置 REDIS_URL：AI 任务队列、Worker 心跳与任务恢复不可用",
                fix,
                promote=True,
            )
        return
    if "${" in raw:
        report.error(key, "包含未展开的变量占位符（${...}）", "确认 .env 已提供真实密码（Compose 会先展开 REDIS_PASSWORD）")
        return
    try:
        parts = urlsplit(raw)
    except ValueError:
        report.error(key, "连接串格式无效", "使用 redis://:密码@主机:6379/0 或 rediss:// 形式")
        return
    scheme = (parts.scheme or "").lower()
    if scheme not in REDIS_SCHEMES:
        report.error(
            key,
            "仅支持 redis:// 或 rediss://，当前协议为 %r" % (scheme or "未知"),
            "按 README 的 REDIS_URL 格式配置；跨主机/公网链路建议 rediss://",
        )
        return
    password = unquote(parts.password or "")
    if not password:
        report.warn(
            key,
            "Redis 连接串没有密码：容器网络内的任意进程都能读写任务队列",
            "设置 REDIS_PASSWORD（Compose 默认 requirepass）并重新生成 REDIS_URL",
            promote=True,
        )
        return
    problem = _secret_problem(password)
    if problem:
        report.error(
            key,
            "Redis 密码%s，公网可被直接猜中" % problem,
            "生成至少 24 位随机密码并同步到 REDIS_PASSWORD / REDIS_URL",
        )


def _check_admin_init(
    env: Mapping[str, str], report: _Report, admin_exists: bool | None
) -> None:
    key = "STUDY_ADMIN_INIT_TOKEN"
    token = _text(env, key)
    if token:
        if "${" in token:
            report.error(key, "包含未展开的变量占位符（${...}）", "确认 .env 已提供真实的一次性口令")
            return
        if len(token) < 8:
            report.error(
                key,
                "一次性初始化口令少于 8 位，容易被暴力猜测",
                "使用至少 24 位随机串（可执行 python -c \"import secrets;print(secrets.token_urlsafe(32))\" 生成）",
            )
            return
        problem = _secret_problem(token)
        if problem:
            report.error(
                key,
                "一次性初始化口令%s，公网可被直接猜中" % problem,
                "使用随机口令；初始化成功后立即从环境移除",
            )
            return
        if admin_exists is True:
            report.warn(
                key,
                "数据库已有管理员，但一次性初始化口令仍保留在环境变量中",
                "管理员初始化完成后从 .env 移除该口令并重启（初始化接口已返回 409，不会再重复创建）",
            )
        return
    if admin_exists is True:
        return
    if admin_exists is False:
        report.error(
            key,
            "公网模式首次部署没有管理员初始化路径：注册默认关闭，且首个注册者不会成为管理员",
            "设置一次性口令 STUDY_ADMIN_INIT_TOKEN（至少 8 位随机串），部署后调用 "
            "/api/auth/init-admin 创建管理员",
        )
    else:
        report.error(
            key,
            "公网模式未配置管理员初始化口令，且部署前无法确认数据库是否已有管理员",
            "设置 STUDY_ADMIN_INIT_TOKEN；若管理员已初始化，可加 --admin-exists 声明，"
            "或直接由应用启动自检（可查数据库）复核",
        )


def _check_ai_url_policy(env: Mapping[str, str], report: _Report) -> None:
    key = "STUDY_AI_URL_POLICY"
    configured = _text(env, key).lower()
    if configured and configured not in AI_POLICIES:
        report.warn(
            key,
            "无法识别的策略 %r：公网模式将回退 public_https" % configured,
            "只使用 public_https（默认）或 allowlist（严格托管）",
            promote=True,
        )
    effective = configured if configured in AI_POLICIES else "public_https"
    if effective == "local_open":
        report.error(
            key,
            "公网模式禁止 local_open：它允许任意 HTTP/私网模型地址（包括内网与回环服务），"
            "等于把服务端出站请求目标向普通账号开放",
            "改为 public_https（仅公网 HTTPS）或 allowlist（再用域名白名单收紧）",
        )
        return
    allowed_key = "STUDY_AI_ALLOWED_HOSTS"
    allowed_raw = _text(env, allowed_key)
    entries = _entries(allowed_raw)
    if effective == "allowlist":
        if not entries:
            report.warn(
                allowed_key,
                "allowlist 策略未配置额外放行域名：普通用户只能使用内置厂商预设与管理页白名单，"
                "自定义模型地址会被拒绝",
                "按需设置 STUDY_AI_ALLOWED_HOSTS=api.example.com,*.openai.azure.com，"
                "或由管理员在设置页添加白名单",
            )
        for entry in entries:
            if entry == "*":
                report.error(
                    allowed_key,
                    "白名单包含 *：等同允许任意出站目的地",
                    "使用具体域名或 *.example.com 形式的子域通配，禁止单个 *",
                )
            elif "://" in entry or "/" in entry:
                report.error(
                    allowed_key,
                    "白名单包含非法项：%r" % entry,
                    "只填写域名，例如 api.example.com 或 *.openai.azure.com",
                )
            elif _is_metadata_host(entry):
                report.error(
                    allowed_key,
                    "白名单包含云元数据地址：%r" % entry,
                    "移除该地址；元数据服务在任何策略下都不允许访问",
                )
    elif entries:
        report.warn(
            allowed_key,
            "当前策略为 public_https，STUDY_AI_ALLOWED_HOSTS 不生效",
            "如确需限定模型域名，把 STUDY_AI_URL_POLICY 设为 allowlist",
        )


# ---------------------------------------------------------------------------
# 对外入口
# ---------------------------------------------------------------------------

def check_config(
    env: Mapping[str, str] | None = None,
    *,
    admin_exists: bool | None = None,
    strict: bool = False,
) -> GuardResult:
    """校验公网配置。

    :param env: 环境变量映射；默认读取 ``os.environ``。
    :param admin_exists: ``True``/``False`` 表示数据库是否已有管理员；``None`` 表示无法确认
        （部署前脚本场景，此时要求显式配置 ``STUDY_ADMIN_INIT_TOKEN``）。
    :param strict: ``True`` 时把「缺失关键生产依赖」等可升级告警视为错误（部署前脚本默认）。
    """
    env_map = dict(os.environ if env is None else env)
    public = public_mode_enabled(env_map)
    report = _Report(public, strict)
    _check_public_mode_value(env_map, report)
    if not public:
        return report.result()
    _check_allowed_hosts(env_map, report)
    _check_public_domain(env_map, report)
    _check_forwarded_allow_ips(env_map, report)
    _check_database_url(env_map, report)
    _check_redis_url(env_map, report)
    _check_admin_init(env_map, report, admin_exists)
    _check_ai_url_policy(env_map, report)
    return report.result()


def enforce_public_config(
    *,
    env: Mapping[str, str] | None = None,
    admin_exists: Callable[[], bool] | bool | None = None,
    strict: bool = False,
) -> GuardResult:
    """应用启动自检：公网模式有 error 时抛 :class:`ConfigGuardError`；本地模式直接放行。

    ``admin_exists`` 可传可调用对象（延迟到确实需要时才查数据库），只在数据库已有管理员
    且未配置一次性初始化口令时才是唯一的合法路径。
    """
    env_map = dict(os.environ if env is None else env)
    known: bool | None
    if callable(admin_exists):
        token = str(env_map.get("STUDY_ADMIN_INIT_TOKEN", "") or "").strip()
        known = None if token else bool(admin_exists())
    else:
        known = admin_exists
    result = check_config(env_map, admin_exists=known, strict=strict)
    if not result.public_mode:
        for issue in result.issues:
            _log.warning("配置自检（本地模式）：[%s] %s", issue.key, issue.message)
        return result
    for issue in result.warnings:
        _log.warning("公网配置告警 [%s] %s；修复：%s", issue.key, issue.message, issue.fix)
    if result.errors:
        report = result.format_report()
        _log.error(report)
        raise ConfigGuardError(result)
    _log.info("公网配置自检通过（%d 个告警）", len(result.warnings))
    return result


__all__ = [
    "AI_POLICIES",
    "ConfigGuardError",
    "GuardResult",
    "Issue",
    "PUBLIC_MODE_VALUES",
    "check_config",
    "enforce_public_config",
    "public_mode_enabled",
]
