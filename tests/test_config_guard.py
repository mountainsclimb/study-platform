"""WS6 公网部署默认值收紧：启动自检、部署前脚本与普通账号 AI 出站权限回归。

已有行为（公网注册关闭 / 一次性管理员初始化 / Secure Cookie / 管理员白名单）保持不变，
本文件只覆盖新增的“默认值不能悄悄带进公网”约束：

* ``app/config_guard.py`` 的每个拒绝/通过分支；
* FastAPI lifespan 在公网坏配置下拒绝启动、本地模式不受影响；
* ``scripts/check_public_config.py`` 的退出码；
* 普通账号不能自行开放 AI 出站目的地（平台白名单仅管理员可改）。
"""
import json
import os
from pathlib import Path
import subprocess
import sys

from fastapi.testclient import TestClient
import pytest

from app import config_guard

ROOT = Path(__file__).resolve().parents[1]

# 一套「已收紧」的公网配置：各分支测试只改动其中一项。
_PUBLIC_ENV = {
    "STUDY_PUBLIC_MODE": "1",
    "STUDY_ALLOWED_HOSTS": "zhixu.example.com",
    "FORWARDED_ALLOW_IPS": "172.16.0.0/12",
    "DATABASE_URL": "postgresql://zhixu:Q7vmZ2rT9wLp4xKd8sNf6b@postgres:5432/zhixu",
    "REDIS_URL": "redis://:H3sRp9Wq2Ty6Uv4Nd8Zc@redis:6379/0",
    "STUDY_ADMIN_INIT_TOKEN": "op-token-7Qm2vZ9r",
    "STUDY_AI_URL_POLICY": "public_https",
}

# 测试前后需要清理的键：避免宿主环境或前一个用例污染公网自检。
_ENV_KEYS = (
    "STUDY_PUBLIC_MODE",
    "STUDY_ALLOWED_HOSTS",
    "STUDY_REGISTRATION_MODE",
    "FORWARDED_ALLOW_IPS",
    "DATABASE_URL",
    "REDIS_URL",
    "PG_PASSWORD",
    "REDIS_PASSWORD",
    "STUDY_ADMIN_INIT_TOKEN",
    "STUDY_AI_URL_POLICY",
    "STUDY_AI_ALLOWED_HOSTS",
    # 部署布局信号：若宿主环境带这些变量，公网自检会按“真实部署”提高严格度。
    "STUDY_DATA_DIR",
    "STUDY_BANK_DIR",
    "STUDY_DEPLOYMENT_LAYOUT",
    "STUDY_DOMAIN",
)


def _public_env(**overrides):
    env = dict(_PUBLIC_ENV)
    for key, value in overrides.items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    return env


def _errors(result):
    return {item.key: item for item in result.errors}


def _warnings(result):
    return {item.key: item for item in result.warnings}


def _apply_env(monkeypatch, values):
    """把给定映射设为进程环境，并清掉其余公网相关键。"""
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    for key, value in values.items():
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)


# ---------------------------------------------------------------------------
# A. check_config：通过 / 拒绝 / 告警分支
# ---------------------------------------------------------------------------

def test_hardened_public_config_has_no_issue():
    result = config_guard.check_config(_public_env(), admin_exists=False, strict=True)
    assert result.public_mode is True
    assert result.ok is True
    assert result.issues == ()


def test_local_mode_is_never_checked_or_blocked():
    # 本地开发/测试的默认值（任意转发来源、示例 Redis、local_open AI、本地 Host）不应触发任何检查。
    env = {
        "STUDY_PUBLIC_MODE": "0",
        "STUDY_ALLOWED_HOSTS": "localhost,127.0.0.1",
        "FORWARDED_ALLOW_IPS": "*",
        "REDIS_URL": "redis://:changeme@redis:6379/0",
        "DATABASE_URL": "sqlite:///data/app.sqlite3",
        "STUDY_AI_URL_POLICY": "local_open",
    }
    result = config_guard.check_config(env, admin_exists=False, strict=True)
    assert result.public_mode is False
    assert result.ok is True
    assert result.issues == ()


@pytest.mark.parametrize(
    ("override", "key"),
    [
        pytest.param({"STUDY_ALLOWED_HOSTS": None}, "STUDY_ALLOWED_HOSTS", id="hosts-missing"),
        pytest.param({"STUDY_ALLOWED_HOSTS": "*"}, "STUDY_ALLOWED_HOSTS", id="hosts-wildcard"),
        pytest.param({"STUDY_ALLOWED_HOSTS": "localhost,127.0.0.1,[::1]"}, "STUDY_ALLOWED_HOSTS", id="hosts-local-only"),
        pytest.param({"STUDY_ALLOWED_HOSTS": "https://zhixu.example.com"}, "STUDY_ALLOWED_HOSTS", id="hosts-with-scheme"),
        pytest.param({"FORWARDED_ALLOW_IPS": "*"}, "FORWARDED_ALLOW_IPS", id="forwarded-wildcard"),
        pytest.param({"FORWARDED_ALLOW_IPS": "0.0.0.0/0"}, "FORWARDED_ALLOW_IPS", id="forwarded-all-network"),
        pytest.param({"FORWARDED_ALLOW_IPS": "proxy.local"}, "FORWARDED_ALLOW_IPS", id="forwarded-not-ip"),
        pytest.param({"REDIS_URL": None}, "REDIS_URL", id="redis-missing-deploy"),
        pytest.param({"REDIS_URL": "redis://:changeme@redis:6379/0"}, "REDIS_URL", id="redis-sample-password"),
        pytest.param({"REDIS_URL": "http://redis:6379/0"}, "REDIS_URL", id="redis-bad-scheme"),
        pytest.param({"REDIS_PASSWORD": "please-change-me"}, "REDIS_PASSWORD", id="redis-password-env-sample"),
        pytest.param({"DATABASE_URL": None}, "DATABASE_URL", id="database-missing-deploy"),
        pytest.param({"DATABASE_URL": "sqlite:///data/app.sqlite3"}, "DATABASE_URL", id="database-sqlite"),
        pytest.param(
            {"DATABASE_URL": "postgresql://zhixu:changeme@postgres:5432/zhixu"},
            "DATABASE_URL",
            id="database-sample-password",
        ),
        pytest.param({"STUDY_AI_URL_POLICY": "local_open"}, "STUDY_AI_URL_POLICY", id="ai-local-open"),
        pytest.param(
            {"STUDY_AI_URL_POLICY": "allowlist", "STUDY_AI_ALLOWED_HOSTS": "*"},
            "STUDY_AI_ALLOWED_HOSTS",
            id="ai-allowlist-wildcard",
        ),
        pytest.param({"STUDY_ADMIN_INIT_TOKEN": None}, "STUDY_ADMIN_INIT_TOKEN", id="admin-token-missing"),
        pytest.param({"STUDY_ADMIN_INIT_TOKEN": "short"}, "STUDY_ADMIN_INIT_TOKEN", id="admin-token-too-short"),
        pytest.param(
            {"STUDY_ADMIN_INIT_TOKEN": "please-change-me"},
            "STUDY_ADMIN_INIT_TOKEN",
            id="admin-token-sample",
        ),
    ],
)
def test_public_config_rejects_dangerous_defaults(override, key):
    result = config_guard.check_config(_public_env(**override), admin_exists=False, strict=True)
    assert result.ok is False
    assert key in _errors(result)
    issue = _errors(result)[key]
    assert issue.message
    assert issue.fix  # 每条错误都必须给出可照做的修复提示


def test_missing_production_dependencies_warn_at_startup_but_fail_deploy_check():
    env = _public_env(FORWARDED_ALLOW_IPS=None, DATABASE_URL=None, REDIS_URL=None)
    # 应用启动（非 strict）：只强告警，避免本地/兼容测试环境被误伤。
    startup = config_guard.check_config(env, admin_exists=False, strict=False)
    assert startup.ok is True
    for key in ("FORWARDED_ALLOW_IPS", "DATABASE_URL", "REDIS_URL"):
        assert key in _warnings(startup)
    # 部署前脚本（strict）：缺失关键生产依赖就是错误。
    deploy = config_guard.check_config(env, admin_exists=False, strict=True)
    assert deploy.ok is False
    for key in ("FORWARDED_ALLOW_IPS", "DATABASE_URL", "REDIS_URL"):
        assert key in _errors(deploy)


def test_deployment_layout_requires_postgres_redis_and_trusted_proxy():
    # 设置 STUDY_DATA_DIR / STUDY_BANK_DIR 说明是容器/服务器布局：
    # 缺失 PostgreSQL、Redis 或可信反代来源时，即使应用启动的宽松模式也必须失败。
    env = _public_env(
        STUDY_DATA_DIR="/data",
        DATABASE_URL=None,
        REDIS_URL=None,
        FORWARDED_ALLOW_IPS=None,
    )
    result = config_guard.check_config(env, admin_exists=False, strict=False)
    assert result.ok is False
    for key in ("DATABASE_URL", "REDIS_URL", "FORWARDED_ALLOW_IPS"):
        assert key in _errors(result)


def test_local_fixed_paths_are_not_a_deployment_layout():
    """开发和 CI 会把题库/数据目录固定到本机路径（例如 E:\\...\\releases\\bank-r5）。

    这不是容器部署：公网模式启动自检只应告警，不能因此把测试/本地服务判死。
    真实部署用标准 /data、/bank 或显式 STUDY_DEPLOYMENT_LAYOUT=1 表达。
    """
    env = _public_env(
        STUDY_DATA_DIR=r"E:\responsibility\study-platform\data",
        STUDY_BANK_DIR=r"E:\responsibility\高考题库_按学科\releases\bank-20261121-r5",
        DATABASE_URL=None,
        REDIS_URL=None,
        FORWARDED_ALLOW_IPS=None,
    )
    startup = config_guard.check_config(env, admin_exists=False, strict=False)
    assert startup.ok is True
    for key in ("DATABASE_URL", "REDIS_URL", "FORWARDED_ALLOW_IPS"):
        assert key in _warnings(startup)
    assert config_guard._deployment_layout(env) is False
    # 显式声明部署布局时，即使目录非标准也按真实部署 fail-closed。
    explicit = _public_env(
        STUDY_DEPLOYMENT_LAYOUT="1",
        DATABASE_URL=None,
        REDIS_URL=None,
        FORWARDED_ALLOW_IPS=None,
    )
    assert config_guard._deployment_layout(explicit) is True
    assert config_guard.check_config(explicit, admin_exists=False, strict=False).ok is False


def test_existing_admin_makes_init_token_optional():
    result = config_guard.check_config(
        _public_env(STUDY_ADMIN_INIT_TOKEN=None), admin_exists=True, strict=True
    )
    assert result.ok is True
    assert "STUDY_ADMIN_INIT_TOKEN" not in _errors(result)


def test_unknown_admin_status_requires_init_token():
    result = config_guard.check_config(
        _public_env(STUDY_ADMIN_INIT_TOKEN=None), admin_exists=None, strict=False
    )
    assert result.ok is False
    assert "STUDY_ADMIN_INIT_TOKEN" in _errors(result)
    assert "无法确认" in _errors(result)["STUDY_ADMIN_INIT_TOKEN"].message


def test_stale_init_token_after_admin_ready_is_warning_only():
    result = config_guard.check_config(_public_env(), admin_exists=True, strict=True)
    assert result.ok is True
    assert "STUDY_ADMIN_INIT_TOKEN" in _warnings(result)


def test_mixed_local_host_entries_warn_at_startup_but_fail_deploy_check():
    env = _public_env(STUDY_ALLOWED_HOSTS="zhixu.example.com,localhost")
    startup = config_guard.check_config(env, admin_exists=False, strict=False)
    assert startup.ok is True
    assert "STUDY_ALLOWED_HOSTS" in _warnings(startup)
    deploy = config_guard.check_config(env, admin_exists=False, strict=True)
    assert deploy.ok is False
    assert "STUDY_ALLOWED_HOSTS" in _errors(deploy)


def test_public_domain_must_be_in_allowed_hosts():
    # Caddy 配了对外域名但 Host 白名单没放行 → 反向代理转发后所有请求 400。
    env = _public_env(STUDY_DOMAIN="other.example.com")
    startup = config_guard.check_config(env, admin_exists=False, strict=False)
    assert startup.ok is True
    assert "STUDY_DOMAIN" in _warnings(startup)
    deploy = config_guard.check_config(env, admin_exists=False, strict=True)
    assert "STUDY_DOMAIN" in _errors(deploy)


def test_public_domain_mismatch_fails_in_deployment_layout():
    env = _public_env(STUDY_DOMAIN="other.example.com", STUDY_DATA_DIR="/data")
    result = config_guard.check_config(env, admin_exists=False, strict=False)
    assert "STUDY_DOMAIN" in _errors(result)


def test_single_label_host_is_warning_only():
    result = config_guard.check_config(
        _public_env(STUDY_ALLOWED_HOSTS="testserver"), admin_exists=False, strict=True
    )
    assert result.ok is True
    assert "STUDY_ALLOWED_HOSTS" in _warnings(result)


def test_allowlist_without_explicit_hosts_is_warning_only():
    result = config_guard.check_config(
        _public_env(STUDY_AI_URL_POLICY="allowlist"), admin_exists=False, strict=True
    )
    assert result.ok is True
    assert "STUDY_AI_ALLOWED_HOSTS" in _warnings(result)


def test_unknown_public_mode_value_is_flagged_as_typo():
    result = config_guard.check_config({"STUDY_PUBLIC_MODE": "enabled", "FORWARDED_ALLOW_IPS": "*"})
    assert result.public_mode is False  # 不把无法识别的值当成公网模式
    # 但必须告警：否则公网部署会意外保留全部本地默认值。
    assert "STUDY_PUBLIC_MODE" in _warnings(result)
    assert "1" in _warnings(result)["STUDY_PUBLIC_MODE"].fix


def test_format_report_contains_key_and_fix_hint():
    result = config_guard.check_config(_public_env(FORWARDED_ALLOW_IPS="*"), admin_exists=False)
    report = result.format_report()
    assert "[公网配置自检] 失败" in report
    assert "FORWARDED_ALLOW_IPS" in report
    assert "修复：" in report


# ---------------------------------------------------------------------------
# B. enforce_public_config：启动期语义
# ---------------------------------------------------------------------------

def test_enforce_raises_actionable_error_for_dangerous_config():
    with pytest.raises(config_guard.ConfigGuardError) as excinfo:
        config_guard.enforce_public_config(
            env=_public_env(FORWARDED_ALLOW_IPS="*"), admin_exists=False
        )
    message = str(excinfo.value)
    assert "FORWARDED_ALLOW_IPS" in message
    assert "修复：" in message
    assert excinfo.value.result.errors


def test_enforce_skips_local_mode_completely():
    env = {
        "STUDY_PUBLIC_MODE": "0",
        "FORWARDED_ALLOW_IPS": "*",
        "STUDY_ALLOWED_HOSTS": "localhost",
        "STUDY_AI_URL_POLICY": "local_open",
    }
    result = config_guard.enforce_public_config(env=env, admin_exists=False)
    assert result.public_mode is False
    assert result.ok is True


def test_enforce_queries_admin_presence_only_when_token_missing():
    calls = []

    def admin_exists():
        calls.append(True)
        return False

    # 已配置一次性口令：不需要查数据库。
    result = config_guard.enforce_public_config(env=_public_env(), admin_exists=admin_exists)
    assert result.ok is True
    assert calls == []
    # 未配置口令：需要查库确认是否已有管理员；查无管理员时拒绝启动。
    with pytest.raises(config_guard.ConfigGuardError):
        config_guard.enforce_public_config(
            env=_public_env(STUDY_ADMIN_INIT_TOKEN=None), admin_exists=admin_exists
        )
    assert calls == [True]


# ---------------------------------------------------------------------------
# C. lifespan：公网坏配置拒绝启动，本地模式不受影响
# ---------------------------------------------------------------------------

def test_lifespan_rejects_wildcard_forwarded_ips(isolated_db, monkeypatch):
    from app.main import app

    # 不配置 DATABASE_URL/REDIS_URL：避免测试环境真的去连 PostgreSQL/Redis。
    _apply_env(monkeypatch, _public_env(
        FORWARDED_ALLOW_IPS="*", DATABASE_URL=None, REDIS_URL=None,
    ))
    with pytest.raises(config_guard.ConfigGuardError) as excinfo:
        with TestClient(app):
            pass
    assert "FORWARDED_ALLOW_IPS" in str(excinfo.value)


def test_lifespan_rejects_missing_admin_init_path(isolated_db, monkeypatch):
    from app.main import app

    _apply_env(monkeypatch, _public_env(
        STUDY_ADMIN_INIT_TOKEN=None, DATABASE_URL=None, REDIS_URL=None,
    ))
    with pytest.raises(config_guard.ConfigGuardError) as excinfo:
        with TestClient(app):
            pass
    assert "STUDY_ADMIN_INIT_TOKEN" in str(excinfo.value)


def test_lifespan_rejects_deployment_layout_without_postgres(isolated_db, monkeypatch):
    from app.main import app

    # 数据目录 + 公网模式 = 真实部署布局：缺 PostgreSQL/Redis/可信反代必须拒绝启动。
    _apply_env(monkeypatch, _public_env(
        STUDY_DATA_DIR="/data",
        DATABASE_URL=None,
        REDIS_URL=None,
        FORWARDED_ALLOW_IPS=None,
    ))
    with pytest.raises(config_guard.ConfigGuardError) as excinfo:
        with TestClient(app):
            pass
    assert "DATABASE_URL" in str(excinfo.value)


def test_lifespan_starts_with_warnings_when_production_deps_are_missing(isolated_db, monkeypatch):
    """缺失 DATABASE_URL/Redis 只强告警：兼容不依赖这些组件的测试与演练环境。"""
    from app.main import app

    _apply_env(monkeypatch, _public_env(
        DATABASE_URL=None, REDIS_URL=None, FORWARDED_ALLOW_IPS=None,
    ))
    with TestClient(app) as client:
        assert client.get("/api/health/live").status_code == 200


def test_lifespan_accepts_hardened_public_config(isolated_db, monkeypatch):
    from app.main import app

    _apply_env(monkeypatch, _public_env(DATABASE_URL=None, REDIS_URL=None))
    # 这里不真正连 PostgreSQL/Redis（测试环境没有依赖），其余公网关键项全部合规。
    with TestClient(app, base_url="https://testserver") as client:
        assert client.get("/api/health/live").status_code == 200


def test_lifespan_local_mode_ignores_bad_public_values(isolated_db, monkeypatch):
    from app.main import app

    _apply_env(monkeypatch, {
        "STUDY_PUBLIC_MODE": "0",
        "STUDY_ALLOWED_HOSTS": "localhost,127.0.0.1",
        "FORWARDED_ALLOW_IPS": "*",
        "REDIS_PASSWORD": "changeme",
        "STUDY_AI_URL_POLICY": "local_open",
    })
    with TestClient(app) as client:
        assert client.get("/api/health/live").status_code == 200
        assert client.get("/api/health/ready").status_code == 200


# ---------------------------------------------------------------------------
# D. 普通账号不能配置开放的 AI 出站目的地（白名单仅管理员可改）
# ---------------------------------------------------------------------------

def _provider_payload(base_url):
    return {
        "name": "自定义连接",
        "kind": "openai",
        "base_url": base_url,
        "api_key": "sk-unit-test-secret-123",
        "models": ["model-a"],
    }


def test_student_cannot_manage_ai_allowlist(client, second_client, monkeypatch):
    monkeypatch.setenv("STUDY_AI_URL_POLICY", "allowlist")
    # 平台级白名单只能管理员维护：普通账号既不能读也不能加。
    assert second_client.get("/api/admin/allowed-hosts").status_code == 403
    assert second_client.post(
        "/api/admin/allowed-hosts", json={"hostname": "relay.example.com"}
    ).status_code == 403

    added = client.post("/api/admin/allowed-hosts", json={"hostname": "relay.example.com"})
    assert added.status_code == 200, added.text
    hosts = client.get("/api/admin/allowed-hosts").json()
    assert any(item["hostname"] == "relay.example.com" for item in hosts["custom"])

    # 普通账号：未获管理员放行的目的地一律拒绝，管理员放行后才能保存连接。
    rejected = second_client.post(
        "/api/providers", json=_provider_payload("https://attacker.example.net/v1")
    )
    assert rejected.status_code == 422, rejected.text
    detail = rejected.json()["detail"]
    assert isinstance(detail, dict)
    assert detail.get("code") == "url_not_allowed"

    accepted = second_client.post(
        "/api/providers", json=_provider_payload("https://relay.example.com/v1")
    )
    assert accepted.status_code == 200, accepted.text


def test_public_https_blocks_insecure_or_metadata_ai_destinations(second_client, monkeypatch):
    monkeypatch.setenv("STUDY_AI_URL_POLICY", "public_https")
    http_rejected = second_client.post(
        "/api/providers", json=_provider_payload("http://api.example.com/v1")
    )
    assert http_rejected.status_code == 422, http_rejected.text
    assert "HTTPS" in json.dumps(http_rejected.json(), ensure_ascii=False)

    metadata_rejected = second_client.post(
        "/api/providers", json=_provider_payload("https://169.254.169.254/latest")
    )
    assert metadata_rejected.status_code == 422, metadata_rejected.text
    assert "元数据" in json.dumps(metadata_rejected.json(), ensure_ascii=False)


def test_public_mode_defaults_ai_policy_to_public_https(second_client, monkeypatch):
    # 只开公网模式、不给显式策略：netguard 必须回退 public_https（而不是本地 local_open）。
    monkeypatch.setenv("STUDY_PUBLIC_MODE", "1")
    monkeypatch.delenv("STUDY_AI_URL_POLICY", raising=False)
    rejected = second_client.post(
        "/api/providers", json=_provider_payload("http://127.0.0.1:11434/v1")
    )
    assert rejected.status_code == 422, rejected.text
    assert "HTTPS" in json.dumps(rejected.json(), ensure_ascii=False)


# ---------------------------------------------------------------------------
# E. scripts/check_public_config.py：部署前校验的退出码
# ---------------------------------------------------------------------------

def _script_env():
    """干净的子进程环境：去掉宿主上的公网相关键，避免干扰 --env-file。"""
    return {
        key: value for key, value in os.environ.items()
        if key not in _ENV_KEYS and key not in {"PYTEST_CURRENT_TEST", "PYTEST_ADDOPTS"}
    }


def _run_config_script(*arguments):
    return subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "check_public_config.py"), *arguments],
        cwd=str(ROOT),
        env=_script_env(),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )


def _write_env_file(path, lines):
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


_GOOD_ENV_LINES = [
    "STUDY_PUBLIC_MODE=1",
    "STUDY_ALLOWED_HOSTS=zhixu.example.com",
    "FORWARDED_ALLOW_IPS=172.16.0.0/12",
    "STUDY_DOMAIN=zhixu.example.com",
    "PG_PASSWORD=Q7vmZ2rT9wLp4xKd8sNf6b",
    "REDIS_PASSWORD=H3sRp9Wq2Ty6Uv4Nd8Zc",
    "STUDY_ADMIN_INIT_TOKEN=op-token-7Qm2vZ9r",
    "STUDY_AI_URL_POLICY=public_https",
]


def test_deploy_check_script_passes_good_env_file(tmp_path):
    env_file = _write_env_file(tmp_path / "good.env", _GOOD_ENV_LINES)
    proc = _run_config_script("--env-file", str(env_file), "--json")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["ok"] is True
    assert payload["errors"] == []
    # Compose 会用 PG_PASSWORD/REDIS_PASSWORD 拼接连接串，脚本要识别为 PostgreSQL/Redis。
    assert payload["public_mode"] is True


def test_deploy_check_script_fails_bad_env_file(tmp_path):
    lines = list(_GOOD_ENV_LINES)
    lines[2] = "FORWARDED_ALLOW_IPS=*"
    env_file = _write_env_file(tmp_path / "bad.env", lines)
    proc = _run_config_script("--env-file", str(env_file), "--json")
    assert proc.returncode == 1, proc.stdout + proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["ok"] is False
    assert any(item["key"] == "FORWARDED_ALLOW_IPS" for item in payload["errors"])


def test_deploy_check_script_requires_public_mode(tmp_path):
    env_file = _write_env_file(tmp_path / "local.env", ["STUDY_PUBLIC_MODE=0"])
    strict = _run_config_script("--env-file", str(env_file), "--json")
    assert strict.returncode == 2, strict.stdout + strict.stderr
    assert json.loads(strict.stdout)["ok"] is False
    # 本地开发可显式跳过。
    skipped = _run_config_script("--env-file", str(env_file), "--allow-local", "--json")
    assert skipped.returncode == 0, skipped.stdout + skipped.stderr
    assert json.loads(skipped.stdout)["skipped"] is True


def test_deploy_check_script_admin_exists_flag(tmp_path):
    lines = [line for line in _GOOD_ENV_LINES if not line.startswith("STUDY_ADMIN_INIT_TOKEN=")]
    env_file = _write_env_file(tmp_path / "ready.env", lines)
    without_flag = _run_config_script("--env-file", str(env_file), "--json")
    assert without_flag.returncode == 1, without_flag.stdout + without_flag.stderr
    with_flag = _run_config_script("--env-file", str(env_file), "--admin-exists", "--json")
    assert with_flag.returncode == 0, with_flag.stdout + with_flag.stderr
