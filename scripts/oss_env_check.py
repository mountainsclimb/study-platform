#!/usr/bin/env python
"""可选引擎环境自检：依赖、模型目录、文件 SHA、可加载性与首载耗时。

用法（仓库根目录）::

    python scripts/oss_env_check.py                 # 全量自检；有引擎不可用则退出 1
    python scripts/oss_env_check.py --soft          # 诊断模式，总是退出 0
    python scripts/oss_env_check.py --engine speech --load
    python scripts/oss_env_check.py --json report.json

设计口径：

* **不触发模型下载**：只读依赖、目录与锁文件；``--load`` 才会真实加载并推理一次。
* 依赖以 ``importlib.util.find_spec`` 判定，版本以发行包元数据为准；缺失就是缺失。
* 模型校验复用 ``app.ai.embeddings_local`` 的规格解析，避免自检与运行时两套规则。
* SHA-256 默认只算锁文件声明的文件；大模型目录可用 ``--no-sha`` 跳过。
* ``deploy/models.lock.json`` 中哈希为空时报告 ``lock_pinned=false``，但不把它算作
  引擎不可用；是否阻断由部署流水线决定（``--strict-lock`` 可改为失败）。
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import importlib
import importlib.metadata
import importlib.util
import json
import os
from pathlib import Path
import queue
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DEFAULT_LOCK = ROOT / "deploy" / "models.lock.json"
ENGINES = ("embeddings", "rerank", "formula", "table", "speech", "chem", "pdf")
ENGINE_LABELS = {
    "embeddings": "本地向量",
    "rerank": "本地重排",
    "formula": "公式识别",
    "table": "表格结构",
    "speech": "离线语音",
    "chem": "RDKit 化学信息",
    "pdf": "PDF 表格抽取",
}
ENGINE_PACKAGES = {
    "embeddings": (("fastembed", "fastembed"), ("onnxruntime", "onnxruntime")),
    "rerank": (("fastembed", "fastembed"), ("onnxruntime", "onnxruntime")),
    "formula": (("rapid_latex_ocr", "rapid-latex-ocr"), ("onnxruntime", "onnxruntime")),
    "table": (("rapid_table", "rapid-table"), ("onnxruntime", "onnxruntime")),
    "speech": (("sherpa_onnx", "sherpa-onnx"),),
    "chem": (("rdkit", "rdkit"),),
    "pdf": (("pdfplumber", "pdfplumber"),),
}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _module_installed(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def _package_status(module: str, distribution: str) -> dict:
    installed = _module_installed(module)
    version = None
    if installed:
        try:
            version = importlib.metadata.version(distribution)
        except importlib.metadata.PackageNotFoundError:
            version = None
        except Exception:
            version = None
    return {"module": module, "distribution": distribution,
            "installed": installed, "version": version}


def _packages(engine: str) -> list[dict]:
    return [_package_status(module, dist) for module, dist in ENGINE_PACKAGES[engine]]


def _packages_ok(packages: list[dict]) -> bool:
    return all(item["installed"] for item in packages)


def load_lock(path: Path = DEFAULT_LOCK) -> dict:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"models": {}, "error": f"锁文件不可读：{path}"}
    if not isinstance(payload.get("models"), dict):
        return {"models": {}, "error": f"锁文件缺少 models 对象：{path}"}
    return payload


def _lock_entry(lock: dict, key: str) -> dict:
    return dict((lock.get("models") or {}).get(key) or {})


def _hash_files(root: Path, names: list[str], with_sha: bool) -> dict[str, str]:
    hashes: dict[str, str] = {}
    if not with_sha:
        return hashes
    for name in names:
        path = Path(root) / name
        if path.is_file():
            try:
                hashes[name] = sha256_file(path)
            except OSError:
                continue
    return hashes


def _check_fastembed(engine: str, *, with_sha: bool, lock: dict) -> dict:
    from app.ai import embeddings_local

    if engine == "rerank":
        spec = embeddings_local.rerank_spec()
        directory = embeddings_local.rerank_dir()
        lock_key = "rerank"
    else:
        spec = embeddings_local.embedding_spec()
        directory = embeddings_local.embedding_dir()
        lock_key = "embeddings"
    resolved, missing = embeddings_local.resolve_model_dir(directory, spec)
    packages = _packages(engine)
    expected = [spec.model_files[0], *spec.required_files]
    if resolved is not None:
        expected = [resolved.model_file, *spec.required_files]
    hashes = _hash_files(resolved.root, expected, with_sha) if resolved else {}
    lock_entry = _lock_entry(lock, lock_key)
    return {
        "available": resolved is not None and _packages_ok(packages),
        "packages": packages,
        "model": {
            "directory": str(directory),
            "source": spec.sources[0],
            "resolved": str(resolved.root) if resolved else None,
            "model_file": resolved.model_file if resolved else None,
            "expected": expected,
            "missing": missing,
            "sha256": hashes,
            "sha_skipped": not with_sha,
        },
        "lock": {
            "key": lock_key,
            "pinned": bool(lock_entry.get("sha256")),
            "declared_files": list(lock_entry.get("files") or lock_entry.get("required_files") or []),
        },
    }


def _check_formula_or_table(engine: str, *, with_sha: bool, lock: dict) -> dict:
    module = importlib.import_module(
        "app.ai.formula_ocr" if engine == "formula" else "app.ai.table_structure"
    )
    status = module.engine_status()
    packages = _packages(engine)
    available = bool(status.get("available")) and _packages_ok(packages)
    return {
        "available": available,
        "packages": packages,
        "model": {
            "provider": "python-package",
            "files": list(status.get("models") or []),
            "directory": None,
            "sha256": {},
            "sha_skipped": True,
        },
        "lock": {"key": engine, "pinned": False,
                 "declared_files": list((_lock_entry(lock, engine)).get("required_files") or [])},
        "detail": {
            "version": status.get("version"),
            "notice": status.get("notice"),
            "error": status.get("error"),
        },
    }


def _check_speech(*, with_sha: bool, lock: dict) -> dict:
    from app.ai import speech_local

    status = speech_local.engine_status()
    packages = _packages("speech")
    tts_dir = Path(status["directories"]["tts"])
    asr_dir = Path(status["directories"]["asr"])
    tts_required = ["model.onnx", "tokens.txt", "voices.bin"]
    asr_required = ["model.onnx", "tokens.txt"]
    tts_missing = [name for name in tts_required if not (tts_dir / name).is_file()]
    asr_missing = [name for name in asr_required if not (asr_dir / name).is_file()]
    # 允许模型文件使用 model.int8.onnx 或其它 *.onnx 命名，与运行时一致。
    if tts_missing and any(tts_dir.glob("*.onnx")):
        tts_missing = [name for name in tts_missing if name != "model.onnx"]
    if asr_missing and any(asr_dir.glob("*.onnx")):
        asr_missing = [name for name in asr_missing if name != "model.onnx"]
    tts_ready = not tts_missing
    asr_ready = not asr_missing
    return {
        "available": _packages_ok(packages) and (tts_ready or asr_ready),
        "packages": packages,
        "tts_available": tts_ready,
        "asr_available": asr_ready,
        "model": {
            "tts": {"directory": str(tts_dir), "missing": tts_missing,
                    "sha256": _hash_files(tts_dir, [n for n in tts_required if n not in tts_missing], with_sha)},
            "asr": {"directory": str(asr_dir), "missing": asr_missing,
                    "sha256": _hash_files(asr_dir, [n for n in asr_required if n not in asr_missing], with_sha)},
            "sha_skipped": not with_sha,
        },
        "lock": {"key": "speech_tts", "pinned": False,
                 "declared_files": list((_lock_entry(lock, "speech_tts")).get("required_files") or [])},
        "detail": {"notice": status.get("notice"), "error": status.get("error")},
    }


def _check_simple(engine: str) -> dict:
    packages = _packages(engine)
    return {
        "available": _packages_ok(packages),
        "packages": packages,
        "model": {"provider": "python-package", "files": [], "sha256": {}, "sha_skipped": True},
        "lock": {"key": engine, "pinned": False, "declared_files": []},
    }


def check_engine(engine: str, *, with_sha: bool = True, lock: dict | None = None) -> dict:
    lock = lock if lock is not None else load_lock()
    if engine in ("embeddings", "rerank"):
        return _check_fastembed(engine, with_sha=with_sha, lock=lock)
    if engine in ("formula", "table"):
        return _check_formula_or_table(engine, with_sha=with_sha, lock=lock)
    if engine == "speech":
        return _check_speech(with_sha=with_sha, lock=lock)
    return _check_simple(engine)


def _capture(fn) -> dict:
    started = time.monotonic()
    try:
        detail = fn()
        return {"ok": True, "elapsed_ms": int((time.monotonic() - started) * 1000), **detail}
    except Exception as error:
        return {
            "ok": False,
            "elapsed_ms": int((time.monotonic() - started) * 1000),
            "error": f"{type(error).__name__}: {error}",
        }


def run_with_timeout(fn, timeout: float) -> dict:
    results: queue.Queue = queue.Queue(maxsize=1)
    thread = threading.Thread(target=lambda: results.put(_capture(fn)), daemon=True)
    thread.start()
    try:
        return results.get(timeout=timeout)
    except queue.Empty:
        return {"ok": False, "elapsed_ms": int(timeout * 1000),
                "error": f"加载超时（>{timeout:g}s）"}


def _load_embeddings() -> dict:
    from app.ai import embeddings_local

    vectors = embeddings_local.embed(["可选引擎自检"])
    return {"dimension": len(vectors[0]) if vectors else 0}


def _load_rerank() -> dict:
    from app.ai import embeddings_local

    scores = embeddings_local.rerank("函数单调性", ["函数单调性", "集合运算"])
    return {"scores": [round(float(score), 4) for score in scores]}


def _sample_image(width: int, height: int):
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    draw.text((12, max(8, height // 3)), "x = 1", fill="black")
    return image


def _load_formula() -> dict:
    from app.ai import formula_ocr

    result = formula_ocr.recognize_crop(_sample_image(320, 96))
    return {"latex_chars": len(str((result or {}).get("latex") or ""))}


def _load_table() -> dict:
    from app.ai import table_structure

    result = table_structure.recognize_table(_sample_image(320, 160))
    return {"html_chars": len(str((result or {}).get("html") or ""))}


def _silent_wav(seconds: float = 1.0, sample_rate: int = 16000) -> bytes:
    import io
    import wave

    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(b"\x00\x00" * int(sample_rate * seconds))
    return buffer.getvalue()


def _load_speech() -> dict:
    from app.ai import speech_local

    result: dict = {}
    if speech_local.tts_available():
        audio, mime = speech_local.synthesize("环境自检")
        result["tts_bytes"] = len(audio)
        result["tts_mime"] = mime
    if speech_local.asr_available():
        transcript = speech_local.transcribe(_silent_wav())
        result["asr_chars"] = len(str((transcript or {}).get("text") or ""))
    if not result:
        raise RuntimeError("本地语音不可用")
    return result


def _load_chem() -> dict:
    from rdkit import Chem
    from rdkit.Chem import Descriptors, rdMolDescriptors

    molecule = Chem.MolFromSmiles("CCO")
    if molecule is None:
        raise RuntimeError("RDKit 无法解析 CCO")
    return {
        "formula": rdMolDescriptors.CalcMolFormula(molecule),
        "molecular_weight": round(float(Descriptors.MolWt(molecule)), 2),
    }


def _load_pdf() -> dict:
    import pdfplumber

    return {"version": getattr(pdfplumber, "__version__", None)}


LOADERS = {
    "embeddings": _load_embeddings,
    "rerank": _load_rerank,
    "formula": _load_formula,
    "table": _load_table,
    "speech": _load_speech,
    "chem": _load_chem,
    "pdf": _load_pdf,
}


def build_report(engines: list[str], *, with_sha: bool, load: bool,
                 load_timeout: float, lock_path: Path) -> dict:
    lock = load_lock(lock_path)
    report_engines: dict[str, dict] = {}
    for engine in engines:
        try:
            entry = check_engine(engine, with_sha=with_sha, lock=lock)
        except Exception as error:
            entry = {
                "available": False,
                "packages": [],
                "error": f"{type(error).__name__}: {error}",
            }
        if load:
            entry["load"] = run_with_timeout(LOADERS[engine], load_timeout)
        report_engines[engine] = entry
    return {
        "ok": all(entry.get("available") for entry in report_engines.values()),
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "python": sys.version.split()[0],
        "platform": sys.platform,
        "lock": {"path": str(lock_path), "error": lock.get("error")},
        "engines": report_engines,
    }


def _human_line(engine: str, entry: dict) -> str:
    mark = "ok" if entry.get("available") else "--"
    packages = ", ".join(
        f"{item['distribution']}={item['version'] or '?'}"
        for item in entry.get("packages") or []
    ) or "无依赖信息"
    parts = [f"[{mark}] {engine:<10} {ENGINE_LABELS.get(engine, engine)}", packages]
    if not all(item["installed"] for item in entry.get("packages") or []):
        parts.append("依赖缺失")
    model = entry.get("model") or {}
    if model.get("missing"):
        parts.append(f"缺少 {', '.join(model['missing'])}")
    elif model.get("resolved"):
        parts.append(f"模型 {model['resolved']}")
    if entry.get("lock", {}).get("pinned") is False and engine in ("embeddings", "rerank"):
        parts.append("锁文件未固定哈希")
    if entry.get("error"):
        parts.append(str(entry["error"]))
    load = entry.get("load")
    if load:
        if load.get("ok"):
            parts.append(f"加载 {load.get('elapsed_ms')}ms")
        else:
            parts.append(f"加载失败：{load.get('error')}")
    return "；".join(parts)


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="可选引擎环境自检")
    parser.add_argument("--engine", action="append", choices=ENGINES, default=None,
                        help="只检查指定引擎，可重复")
    parser.add_argument("--soft", action="store_true", help="有引擎不可用时仍退出 0")
    parser.add_argument("--load", action="store_true", help="真实加载并推理一次（较慢）")
    parser.add_argument("--load-timeout", type=float, default=120.0)
    parser.add_argument("--no-sha", action="store_true", help="跳过文件哈希")
    parser.add_argument("--strict-lock", action="store_true",
                        help="锁文件未固定哈希时视为失败")
    parser.add_argument("--json", nargs="?", const="-", default=None,
                        help="输出 JSON 到文件；不带路径则打印到 stdout")
    parser.add_argument("--lock", type=Path, default=DEFAULT_LOCK)
    args = parser.parse_args(argv)

    engines = args.engine or list(ENGINES)
    report = build_report(
        engines, with_sha=not args.no_sha, load=args.load,
        load_timeout=args.load_timeout, lock_path=args.lock,
    )
    if args.strict_lock:
        unpinned = [
            engine for engine, entry in report["engines"].items()
            if engine in ("embeddings", "rerank") and entry.get("lock", {}).get("pinned") is False
        ]
        if unpinned:
            report["ok"] = False
            report["unpinned"] = unpinned

    if args.json:
        payload = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
        if args.json == "-":
            sys.stdout.write(payload)
        else:
            Path(args.json).write_text(payload, encoding="utf-8")
            for engine in engines:
                print(_human_line(engine, report["engines"][engine]))
            print(f"[{'ok' if report['ok'] else '--'}] JSON 已写入 {args.json}")
    else:
        for engine in engines:
            print(_human_line(engine, report["engines"][engine]))
        print(f"[{'ok' if report['ok'] else '--'}] 自检{'通过' if report['ok'] else '未通过'}")
    if report["ok"] or args.soft:
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
