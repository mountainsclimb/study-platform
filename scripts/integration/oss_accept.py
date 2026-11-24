#!/usr/bin/env python
"""可选引擎真实验收：只在依赖与模型都已就绪时加载推理，否则显式跳过。

退出码：``0`` 已启用的引擎全部加载/推理通过；``1`` 有引擎失败或缺少 ``--require``
指定的引擎；``3`` 没有任何可选引擎被启用（未安装依赖或未预置模型），阶段记为 SKIP。

用法（仓库根目录）::

    python scripts/integration/oss_accept.py
    python scripts/integration/oss_accept.py --require embeddings,speech
    python scripts/integration/oss_accept.py --report screenshots/oss-env-report.json
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import oss_env_check  # noqa: E402


def build_acceptance(*, with_sha: bool, load_timeout: float,
                     lock_path: Path, require: list[str] | None = None) -> dict:
    started = time.monotonic()
    report = oss_env_check.build_report(
        list(oss_env_check.ENGINES), with_sha=with_sha, load=False,
        load_timeout=load_timeout, lock_path=lock_path,
    )
    activated = [
        engine for engine, entry in report["engines"].items()
        if entry.get("available")
    ]
    loads: dict[str, dict] = {}
    problems: list[str] = []
    for engine in activated:
        result = oss_env_check.run_with_timeout(
            oss_env_check.LOADERS[engine], load_timeout,
        )
        loads[engine] = result
        if not result.get("ok"):
            problems.append(f"{engine} 加载失败：{result.get('error')}")
    for engine in require or []:
        if engine not in activated:
            problems.append(f"缺少必需引擎：{engine}（未安装依赖或未预置模型）")
    return {
        "ok": bool(activated) and not problems,
        "skipped": not activated,
        "activated": activated,
        "require": list(require or []),
        "loads": loads,
        "problems": problems,
        "elapsed_s": round(time.monotonic() - started, 1),
        "generated_at": report["generated_at"],
        "engines": report["engines"],
        "lock": report["lock"],
    }


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="可选引擎真实验收")
    parser.add_argument("--report", type=Path,
                        default=ROOT / "screenshots" / "oss-env-report.json")
    parser.add_argument("--lock", type=Path, default=oss_env_check.DEFAULT_LOCK)
    parser.add_argument("--require", default="",
                        help="必须启用并通过的引擎，逗号分隔（如 embeddings,speech）")
    parser.add_argument("--no-sha", action="store_true", help="跳过模型文件哈希")
    parser.add_argument("--load-timeout", type=float, default=180.0)
    args = parser.parse_args(argv)

    require = [item.strip() for item in args.require.split(",") if item.strip()]
    result = build_acceptance(
        with_sha=not args.no_sha, load_timeout=args.load_timeout,
        lock_path=args.lock, require=require,
    )
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    if result["skipped"]:
        print("可选引擎未启用：未安装依赖或未预置模型；"
              f"报告已写入 {args.report}")
        if require:
            for problem in result["problems"]:
                print(f"[error] {problem}", file=sys.stderr)
            return 1
        return 3
    print(
        "可选引擎验收：启用 %s；加载通过 %s；耗时 %.1fs；报告 %s" % (
            ", ".join(result["activated"]),
            ", ".join(name for name, item in result["loads"].items() if item.get("ok")) or "无",
            result["elapsed_s"], args.report,
        )
    )
    if result["problems"]:
        for problem in result["problems"]:
            print(f"[error] {problem}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
