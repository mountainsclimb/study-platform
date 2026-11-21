"""部署前公网配置校验（WS6 / P0「公网模式的部署默认值需要显式收紧」）。

应用启动时 ``app.config_guard`` 会做一次 fail-closed 自检；本脚本在部署前用同一套
规则读取进程环境与 ``.env`` 文件，让 CI 与运维在起容器前就能拿到可判的退出码。

用法（在 study-platform 目录执行）::

    python scripts/check_public_config.py --env-file deploy/.env
    python scripts/check_public_config.py --env-file deploy/.env --admin-exists
    python scripts/check_public_config.py --allow-local      # 本地开发：跳过公网检查

行为说明：

* 未指定 ``--env-file`` 时自动读取 ``deploy/.env``（存在时）；自动读取的 .env 只补齐
  进程环境里缺失的键，显式 ``--env-file`` 则以文件为准；
* 公网模式必须显式开启（``STUDY_PUBLIC_MODE=1``），否则退出码 2；本地开发用
  ``--allow-local`` 跳过；
* 按 Compose 的规则用 ``PG_PASSWORD`` / ``REDIS_PASSWORD`` 补全 ``DATABASE_URL`` /
  ``REDIS_URL`` 后再校验，因此直接校验未改写的 ``deploy/.env`` 也能工作；
* 默认 strict：缺失 PostgreSQL/Redis/可信反代等关键生产配置也算错误（应用启动时这些
  只强告警，避免本地测试环境被误伤）；``--no-strict`` 可切换为启动期同款规则。

退出码：0 通过；1 公网配置存在错误；2 用法/环境问题（未开启公网模式、env 文件不存在）。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config_guard import check_config, public_mode_enabled  # noqa: E402

DEFAULT_ENV_FILE = ROOT / "deploy" / ".env"


def parse_env_file(path: Path) -> dict[str, str]:
    """解析 docker compose 风格的 .env：KEY=VALUE、# 注释、可选引号与 export 前缀。"""
    values: dict[str, str] = {}
    try:
        text = Path(path).read_text(encoding="utf-8-sig")
    except OSError:
        return values
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        elif not value:
            value = ""
        else:
            # 未加引号时丢弃行内注释（要求 # 前有空白，避免误伤密码里的 #）。
            for marker in (" #", "\t#"):
                if marker in value:
                    value = value.split(marker, 1)[0].rstrip()
        if key:
            values[key] = value
    return values


def build_env(explicit_file: str | None) -> tuple[dict[str, str], str | None]:
    """合并进程环境与 .env；显式文件优先，自动发现的 deploy/.env 只补齐缺失键。"""
    env = dict(os.environ)
    file_env: dict[str, str] = {}
    source: str | None = None
    if explicit_file:
        path = Path(explicit_file)
        if not path.is_file():
            raise FileNotFoundError(f"环境文件不存在：{path}")
        file_env = parse_env_file(path)
        source = str(path)
        env.update(file_env)
    elif DEFAULT_ENV_FILE.is_file():
        file_env = parse_env_file(DEFAULT_ENV_FILE)
        source = str(DEFAULT_ENV_FILE)
        for key, value in file_env.items():
            env.setdefault(key, value)
    return env, source


def derive_compose_urls(env: dict[str, str]) -> None:
    """Compose 用 PG_PASSWORD / REDIS_PASSWORD 自动拼接连接串，这里按同样规则补全。"""
    if not str(env.get("DATABASE_URL", "") or "").strip():
        password = str(env.get("PG_PASSWORD", "") or "").strip()
        if password:
            env["DATABASE_URL"] = "postgresql://zhixu:%s@postgres:5432/zhixu" % quote(password, safe="")
    if not str(env.get("REDIS_URL", "") or "").strip():
        password = str(env.get("REDIS_PASSWORD", "") or "").strip()
        if password:
            env["REDIS_URL"] = "redis://:%s@redis:6379/0" % quote(password, safe="")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="知序公网部署配置校验（部署前执行，退出码可判）"
    )
    parser.add_argument("--env-file", help="环境文件路径（默认自动读取 deploy/.env）")
    parser.add_argument(
        "--admin-exists",
        action="store_true",
        help="声明数据库已有管理员：未配置 STUDY_ADMIN_INIT_TOKEN 时不再报错",
    )
    parser.add_argument(
        "--allow-local",
        action="store_true",
        help="允许本地模式：未开启公网模式时直接返回 0（本地开发用）",
    )
    parser.add_argument(
        "--no-strict",
        action="store_true",
        help="放宽为应用启动期规则：缺失生产依赖只告警",
    )
    parser.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    args = parser.parse_args(argv)

    try:
        env, source = build_env(args.env_file)
    except FileNotFoundError as exc:
        print(f"[公网配置自检] 失败：{exc}", file=sys.stderr)
        return 2

    if not public_mode_enabled(env):
        if not args.allow_local:
            message = (
                "未启用公网模式：STUDY_PUBLIC_MODE 非真值。公网部署请显式设置 "
                "STUDY_PUBLIC_MODE=1；本地开发可用 --allow-local 跳过。"
            )
            if args.json:
                print(json.dumps(
                    {"ok": False, "public_mode": False, "source": source, "error": message},
                    ensure_ascii=False,
                ))
            else:
                print(f"[公网配置自检] 失败：{message}", file=sys.stderr)
            return 2
        if args.json:
            print(json.dumps(
                {"ok": True, "skipped": True, "public_mode": False, "source": source},
                ensure_ascii=False,
            ))
        else:
            print("[公网配置自检] 未启用公网模式，本地开发无需检查（--allow-local）。")
        return 0

    derive_compose_urls(env)
    result = check_config(
        env,
        admin_exists=True if args.admin_exists else None,
        strict=not args.no_strict,
    )
    if args.json:
        payload = result.as_dict()
        payload["source"] = source
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        if source:
            print(f"[公网配置自检] 环境文件：{source}")
        print(result.format_report())
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
