#!/usr/bin/env python
"""按 ``deploy/models.lock.json`` 预置可选引擎模型，并做 SHA-256 校验。

用法（仓库根目录）::

    python scripts/fetch_models.py --check          # 只校验本地模型与锁文件
    python scripts/fetch_models.py                  # 下载缺失文件
    python scripts/fetch_models.py --record         # 下载后回填 sha256/sizes
    python scripts/fetch_models.py --check --allow-unpinned

设计取舍：

* Hugging Face 条目按 ``<base>/<repo>/resolve/<revision>/<file>`` 下载，``--mirror``
  或 ``HF_ENDPOINT`` 可切换到镜像；下载先写 ``*.part``，校验通过后原子替换。
* 重排模型的实际来源与 ONNX 文件名由 fastembed 注册表解析（``provider=fastembed``），
  避免把 ``BAAI/bge-reranker-base`` 的 PyTorch 仓库误当成 ONNX 仓库。
* 首次下载后哈希为空，``--check`` 默认拒绝；``--record`` 回填并提交锁文件后，
  后续部署才具备可复现校验。``manual`` 条目（离线语音）只检查必需文件是否齐全。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LOCK = ROOT / "deploy" / "models.lock.json"
DEFAULT_DATA_DIR = Path(os.environ.get("STUDY_DATA_DIR", str(ROOT / "data"))).resolve()
CHUNK_SIZE = 1024 * 1024
USER_AGENT = "zhixu-model-fetcher/1.0"


class FetchError(RuntimeError):
    """模型下载或校验失败。"""


def load_lock(path: Path = DEFAULT_LOCK) -> dict:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise FetchError(f"锁文件不存在：{path}") from None
    except json.JSONDecodeError as error:
        raise FetchError(f"锁文件不是合法 JSON：{path}（{error}）") from None
    if not isinstance(payload.get("models"), dict):
        raise FetchError(f"锁文件缺少 models 对象：{path}")
    return payload


def save_lock(payload: dict, path: Path = DEFAULT_LOCK) -> None:
    Path(path).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _model_root(dest: Path, entry: dict) -> Path:
    return Path(dest) / entry["target_dir"]


def _registry_sources(item: dict, model_name: str) -> list[str]:
    """从 fastembed 注册表条目解析来源仓库列表。

    fastembed 0.9.x 把 ``sources`` 从字符串列表改成了字典
    （``{"hf": "BAAI/bge-reranker-base", "url": None, ...}``）；直接 ``list(dict)``
    只会拿到键名（``hf``），拼出的下载地址会 404。这里统一归一化：优先取 ``hf`` /
    ``hf-mirror`` 键，否则取第一个非空值。
    """
    sources_raw = item.get("sources") or [model_name]
    if not isinstance(sources_raw, dict):
        return list(sources_raw) or [model_name]
    preferred = [value for key, value in sources_raw.items()
                 if key in ("hf", "hf-mirror") and value]
    if preferred:
        return preferred
    fallback = [value for value in sources_raw.values() if value]
    return fallback or [model_name]


def _fastembed_files(key: str, entry: dict) -> tuple[list[str], str]:
    """用 fastembed 注册表解析真实来源仓库与 ONNX 文件名。"""
    try:
        if key == "rerank":
            from fastembed.rerank.cross_encoder import TextCrossEncoder as factory
        else:
            from fastembed import TextEmbedding as factory
        supported = factory.list_supported_models()
    except Exception as error:
        raise FetchError(
            f"{key} 需要先安装 fastembed 才能解析来源（{type(error).__name__}）；"
            "请执行 pip install -r requirements-oss.txt"
        ) from None
    model_name = entry.get("source", "")
    for item in supported:
        if item.get("model") != model_name:
            continue
        sources = _registry_sources(item, model_name)
        model_file = str(item.get("model_file") or "")
        if not model_file:
            break
        files = list(dict.fromkeys([*entry.get("auxiliary_files", []), model_file]))
        return files, sources[0]
    raise FetchError(f"fastembed 注册表找不到模型 {model_name}（{key}）")


def download_manifest(key: str, entry: dict) -> tuple[list[str], str]:
    """返回 ``(相对文件列表, 来源仓库)``；manual 条目抛出 FetchError。"""
    provider = entry.get("provider")
    if provider == "huggingface":
        return list(entry.get("files") or []), entry["source"]
    if provider == "fastembed":
        return _fastembed_files(key, entry)
    raise FetchError(f"{key} 是 {provider} 条目，无法自动下载；{entry.get('hint', '')}".strip())


def _download_url(base: str, repo: str, revision: str, name: str) -> str:
    return f"{base.rstrip('/')}/{repo}/resolve/{revision}/{name}"


def download_file(url: str, destination: Path, *, expected_sha256: str | None,
                  timeout: float = 60.0) -> str:
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_name(destination.name + ".part")
    digest = hashlib.sha256()
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response, partial.open("wb") as handle:
            for chunk in iter(lambda: response.read(CHUNK_SIZE), b""):
                handle.write(chunk)
                digest.update(chunk)
    except (urllib.error.URLError, OSError) as error:
        partial.unlink(missing_ok=True)
        raise FetchError(f"下载失败：{url}（{error}）") from None
    actual = digest.hexdigest()
    if expected_sha256 and actual != expected_sha256:
        partial.unlink(missing_ok=True)
        raise FetchError(
            f"SHA-256 不匹配：{destination.name}（期望 {expected_sha256}，实际 {actual}）"
        )
    shutil.move(str(partial), str(destination))
    return actual


def _entry_hashes(entry: dict) -> dict[str, str]:
    return dict(entry.get("sha256") or {})


def check_model(key: str, entry: dict, dest: Path, *, allow_unpinned: bool) -> list[str]:
    """校验一个模型条目；返回问题列表（空表示通过）。"""
    root = _model_root(dest, entry)
    provider = entry.get("provider")
    if provider in ("python-package", "manual"):
        if provider == "python-package":
            return []
        problems = []
        for name in entry.get("required_files") or []:
            if not (root / name).exists():
                problems.append(f"{key}：缺少 {root / name}")
        return problems

    problems: list[str] = []
    hashes = _entry_hashes(entry)
    try:
        files, _ = download_manifest(key, entry)
    except FetchError:
        # fastembed 未安装时仍可用锁文件里的已知文件做存在性检查。
        files = list(dict.fromkeys([
            *(entry.get("files") or []),
            *(entry.get("required_files") or []),
            *(entry.get("auxiliary_files") or []),
        ]))
    if not files:
        return [f"{key}：锁文件没有可校验的文件清单"]
    for name in files:
        path = root / name
        if not path.is_file():
            problems.append(f"{key}：缺少 {path}")
            continue
        expected = hashes.get(name)
        if expected:
            actual = sha256_file(path)
            if actual != expected:
                problems.append(
                    f"{key}：SHA-256 不匹配 {name}（期望 {expected}，实际 {actual}）"
                )
        elif not allow_unpinned:
            problems.append(
                f"{key}：{name} 尚未记录 SHA-256；"
                "首次校验请加 --allow-unpinned，确认后运行 --record 回填锁文件"
            )
    return problems


def fetch_model(key: str, entry: dict, dest: Path, *, base: str, revision: str,
                force: bool, timeout: float) -> list[str]:
    files, repo = download_manifest(key, entry)
    root = _model_root(dest, entry)
    hashes = _entry_hashes(entry)
    messages: list[str] = []
    for name in files:
        destination = root / name
        expected = hashes.get(name)
        if destination.is_file() and not force:
            if expected and sha256_file(destination) == expected:
                messages.append(f"[skip] {key}/{name}（哈希一致）")
                continue
            if not expected:
                messages.append(f"[skip] {key}/{name}（已存在，哈希未固定）")
                continue
        url = _download_url(base, repo, revision, name)
        actual = download_file(url, destination, expected_sha256=expected, timeout=timeout)
        messages.append(f"[ok] {key}/{name} sha256={actual}")
    return messages


def record_model(key: str, entry: dict, dest: Path) -> list[str]:
    """为已存在的模型文件回填 sha256/sizes；返回缺失文件说明。"""
    root = _model_root(dest, entry)
    provider = entry.get("provider")
    if provider in ("manual", "python-package"):
        return []
    try:
        files, _ = download_manifest(key, entry)
    except FetchError:
        files = list(dict.fromkeys([
            *(entry.get("files") or []),
            *(entry.get("required_files") or []),
            *(entry.get("auxiliary_files") or []),
        ]))
    hashes = dict(entry.get("sha256") or {})
    sizes = dict(entry.get("sizes") or {})
    missing: list[str] = []
    for name in files:
        path = root / name
        if not path.is_file():
            missing.append(f"{key}：缺少 {path}")
            continue
        hashes[name] = sha256_file(path)
        sizes[name] = path.stat().st_size
    entry["sha256"] = hashes
    entry["sizes"] = sizes
    return missing


def selected_models(payload: dict, only: list[str] | None) -> dict:
    models = payload.get("models") or {}
    if not only:
        return models
    unknown = [key for key in only if key not in models]
    if unknown:
        raise FetchError(f"未知模型条目：{', '.join(unknown)}")
    return {key: models[key] for key in only}


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="预置并校验可选引擎模型")
    parser.add_argument("--lock", type=Path, default=DEFAULT_LOCK)
    parser.add_argument("--dest", type=Path, default=DEFAULT_DATA_DIR,
                        help="数据根目录（默认 STUDY_DATA_DIR 或 data/）")
    parser.add_argument("--only", action="append", default=None,
                        help="只处理指定条目，可重复")
    parser.add_argument("--check", action="store_true", help="只校验，不下载")
    parser.add_argument("--record", action="store_true", help="回填 sha256/sizes 到锁文件")
    parser.add_argument("--allow-unpinned", action="store_true",
                        help="校验时允许尚未记录哈希（仅检查存在性）")
    parser.add_argument("--force", action="store_true", help="即使文件已存在也重新下载")
    parser.add_argument("--mirror", default=os.environ.get("HF_ENDPOINT", "https://huggingface.co"),
                        help="Hugging Face 兼容镜像地址")
    parser.add_argument("--timeout", type=float, default=60.0, help="单文件网络超时秒数")
    args = parser.parse_args(argv)

    started = time.monotonic()
    try:
        payload = load_lock(args.lock)
        models = selected_models(payload, args.only)
    except FetchError as error:
        print(f"[error] {error}", file=sys.stderr)
        return 2

    problems: list[str] = []
    for key, entry in models.items():
        provider = entry.get("provider")
        if provider == "python-package":
            print(f"[skip] {key}：模型随 pip 包分发（{entry.get('source')}）")
            continue
        if args.check:
            problems.extend(check_model(key, entry, args.dest, allow_unpinned=args.allow_unpinned))
            continue
        if provider == "manual":
            problems.extend(check_model(key, entry, args.dest, allow_unpinned=True))
            continue
        try:
            for message in fetch_model(
                key, entry, args.dest, base=args.mirror,
                revision=str(entry.get("revision") or "main"),
                force=args.force, timeout=args.timeout,
            ):
                print(message)
        except FetchError as error:
            problems.append(str(error))
    if args.record:
        for key, entry in models.items():
            problems.extend(record_model(key, entry, args.dest))

    if problems:
        for problem in dict.fromkeys(problems):
            print(f"[error] {problem}", file=sys.stderr)
        return 1
    if args.record:
        try:
            save_lock(payload, args.lock)
        except OSError as error:
            print(f"[error] 无法写入锁文件：{error}", file=sys.stderr)
            return 2
        print(f"[ok] 已回填锁文件：{args.lock}")
    print(f"[ok] 模型清单校验通过（{time.monotonic() - started:.1f}s）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
