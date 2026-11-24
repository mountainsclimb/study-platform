#!/usr/bin/env python
"""从 FastAPI 应用生成 ``docs/api-index.json``，作为端点漂移检查的唯一事实源。

用法（仓库根目录）::

    python scripts/update_api_index.py          # 重新生成并写入
    python scripts/update_api_index.py --check  # 只比对，不写文件（CI 用）

``tests/test_contract_drift.py`` 会调用 :func:`collect_routes` 与应用实际路由比对；
新增/删除端点后必须重新生成索引并随代码提交。
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

INDEX_PATH = ROOT / "docs" / "api-index.json"
FRAMEWORK_PATHS = frozenset({
    "/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc",
})
IGNORED_METHODS = frozenset({"HEAD", "OPTIONS"})


def collect_routes() -> list[dict]:
    """返回 ``[{"method": "GET", "path": "/api/..."}]``，按路径/方法排序。"""
    from app.main import app

    routes: set[tuple[str, str]] = set()
    for route in app.routes:
        path = str(getattr(route, "path", "") or "")
        methods = getattr(route, "methods", None)
        if not path or not methods or path in FRAMEWORK_PATHS:
            continue
        for method in methods:
            method = str(method).upper()
            if method in IGNORED_METHODS:
                continue
            routes.add((path, method))
    return [
        {"method": method, "path": path}
        for path, method in sorted(routes)
    ]


def build_index() -> dict:
    routes = collect_routes()
    return {
        "version": 1,
        "generated_from": "app.main:app",
        "count": len(routes),
        "routes": routes,
    }


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="生成端点索引")
    parser.add_argument("--check", action="store_true", help="只比对，不写文件")
    args = parser.parse_args(argv)

    payload = build_index()
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    if args.check:
        if not INDEX_PATH.is_file():
            print(f"[error] 索引不存在：{INDEX_PATH}", file=sys.stderr)
            return 1
        current = INDEX_PATH.read_text(encoding="utf-8")
        if current != text:
            print("[error] 端点索引已漂移，请运行 python scripts/update_api_index.py",
                  file=sys.stderr)
            return 1
        print(f"[ok] 端点索引一致（{payload['count']} 条）")
        return 0
    INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)
    INDEX_PATH.write_text(text, encoding="utf-8")
    print(f"[ok] 已写入 {INDEX_PATH}（{payload['count']} 条）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
