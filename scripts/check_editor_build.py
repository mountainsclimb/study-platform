"""自由画布编辑器构建产物一致性检查（P1：净签出构建 vs /static/editor 发布产物）。

用法：
  # 重新构建到临时目录，再与已入库的 web/editor 比对（CI / 发版前执行）
  python scripts/check_editor_build.py --build

  # 只比对两个已存在的目录（不构建）
  python scripts/check_editor_build.py --built <目录> --published web/editor

退出码：0 完全一致；1 存在差异；2 缺少构建工具或构建失败。
说明：`web/editor` 是入库的发布产物，部署时挂到 `/static/editor`。仓库若被改动但忘记
重新构建，浏览器实际加载的仍是旧产物；本脚本把「源码 → 入库产物」的漂移显式暴露出来。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EDITOR_DIR = ROOT / "editor"
PUBLISHED_DIR = ROOT / "web" / "editor"

IGNORED_NAMES = {".DS_Store", "Thumbs.db"}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def index_directory(root: Path) -> dict[str, str]:
    """返回 ``{相对 POSIX 路径: sha256}``；目录不存在时返回空表。"""
    result: dict[str, str] = {}
    if not root.is_dir():
        return result
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.name in IGNORED_NAMES:
            continue
        result[path.relative_to(root).as_posix()] = _sha256(path)
    return result


def compare_directories(built: Path, published: Path) -> dict:
    """比较构建产物与发布产物，返回差异明细（missing/extra/changed/identical）。"""
    built_index = index_directory(built)
    published_index = index_directory(published)
    missing = sorted(set(built_index) - set(published_index))
    extra = sorted(set(published_index) - set(built_index))
    changed = sorted(
        name for name in set(built_index) & set(published_index)
        if built_index[name] != published_index[name]
    )
    return {
        "built": str(built),
        "published": str(published),
        "built_files": len(built_index),
        "published_files": len(published_index),
        "missing_in_published": missing,
        "extra_in_published": extra,
        "changed": changed,
        "identical": not (missing or extra or changed),
    }


def _find_pnpm() -> str | None:
    for candidate in ("pnpm.cmd", "pnpm", "pnpm.exe"):
        found = shutil.which(candidate)
        if found:
            return found
    return None


def build_to_directory(target: Path) -> None:
    """在 editor/ 下执行 Vite 构建，产物写入 ``target``（不触碰 web/editor）。"""
    pnpm = _find_pnpm()
    if not pnpm:
        raise RuntimeError("未找到 pnpm：请安装 Node ≥ 20 与 pnpm 10 后重试")
    if not (EDITOR_DIR / "node_modules").is_dir():
        raise RuntimeError("editor/node_modules 不存在：请先在 editor/ 执行 pnpm install")
    command = [pnpm, "exec", "vite", "build", "--outDir", str(target), "--emptyOutDir"]
    completed = subprocess.run(
        command,
        cwd=str(EDITOR_DIR),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()[-4000:]
        raise RuntimeError(f"编辑器构建失败（exit {completed.returncode}）：\n{detail}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="检查自由画布编辑器构建产物是否与发布产物一致")
    parser.add_argument("--build", action="store_true", help="先重新构建到临时目录再比对")
    parser.add_argument("--built", type=Path, default=None, help="已构建产物目录（不构建时使用）")
    parser.add_argument("--published", type=Path, default=PUBLISHED_DIR, help="发布产物目录，默认 web/editor")
    parser.add_argument("--json", action="store_true", help="输出 JSON 明细")
    args = parser.parse_args(argv)

    if args.build and args.built:
        parser.error("--build 与 --built 不能同时使用")

    temporary: str | None = None
    try:
        if args.build:
            temporary = tempfile.mkdtemp(prefix="zhixu-editor-build-")
            built_dir = Path(temporary) / "editor"
            print(f"[build] 构建编辑器到临时目录：{built_dir}")
            build_to_directory(built_dir)
        elif args.built is not None:
            built_dir = args.built.resolve()
        else:
            parser.error("需要 --build 或 --built 之一")
        report = compare_directories(built_dir, args.published.resolve())
    except RuntimeError as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return 2
    finally:
        if temporary:
            shutil.rmtree(temporary, ignore_errors=True)

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"构建产物：{report['built_files']} 个文件；发布产物：{report['published_files']} 个文件")
        if report["missing_in_published"]:
            print(f"[缺失] 发布产物缺少 {len(report['missing_in_published'])} 个文件：")
            for name in report["missing_in_published"][:20]:
                print(f"  - {name}")
        if report["extra_in_published"]:
            print(f"[多余] 发布产物多出 {len(report['extra_in_published'])} 个文件：")
            for name in report["extra_in_published"][:20]:
                print(f"  - {name}")
        if report["changed"]:
            print(f"[变化] {len(report['changed'])} 个文件内容不同：")
            for name in report["changed"][:20]:
                print(f"  - {name}")
        print("EDITOR BUILD OK" if report["identical"] else "EDITOR BUILD DRIFT")
    return 0 if report["identical"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
