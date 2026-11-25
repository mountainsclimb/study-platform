"""可选引擎工具链（scripts/oss_env_check.py、scripts/fetch_models.py）验收。"""
import hashlib
import json
from pathlib import Path

import pytest

from scripts import fetch_models, oss_env_check


def _entry(files, **extra):
    return {
        "provider": "huggingface",
        "source": "Qdrant/bge-small-zh-v1.5",
        "revision": "main",
        "target_dir": "models/embeddings",
        "files": files,
        "sha256": {},
        "sizes": {},
        **extra,
    }


def test_record_then_check_pins_hashes(tmp_path):
    root = tmp_path / "models" / "embeddings"
    root.mkdir(parents=True)
    files = ["config.json", "tokenizer.json", "model_optimized.onnx"]
    for name in files:
        (root / name).write_bytes(name.encode("utf-8"))
    entry = _entry(files)

    problems = fetch_models.check_model(
        "embeddings", entry, tmp_path, allow_unpinned=False
    )
    assert problems and all("尚未记录 SHA-256" in item for item in problems)
    assert fetch_models.check_model(
        "embeddings", entry, tmp_path, allow_unpinned=True
    ) == []

    assert fetch_models.record_model("embeddings", entry, tmp_path) == []
    assert set(entry["sha256"]) == set(files)
    assert entry["sizes"]["model_optimized.onnx"] == len("model_optimized.onnx")
    assert fetch_models.check_model(
        "embeddings", entry, tmp_path, allow_unpinned=False
    ) == []

    (root / "config.json").write_bytes(b"changed")
    problems = fetch_models.check_model(
        "embeddings", entry, tmp_path, allow_unpinned=False
    )
    assert any("SHA-256 不匹配" in item for item in problems)


def test_check_model_names_every_missing_file(tmp_path):
    entry = _entry(["config.json", "tokenizer.json", "model_optimized.onnx"])
    problems = fetch_models.check_model(
        "embeddings", entry, tmp_path, allow_unpinned=True
    )
    assert len(problems) == 3
    assert all("缺少" in item for item in problems)


def test_check_manual_model_requires_files(tmp_path):
    entry = {
        "provider": "manual",
        "target_dir": "models/speech/paraformer-zh",
        "required_files": ["model.onnx", "tokens.txt"],
    }
    problems = fetch_models.check_model("speech_asr", entry, tmp_path, allow_unpinned=True)
    assert len(problems) == 2
    root = tmp_path / "models" / "speech" / "paraformer-zh"
    root.mkdir(parents=True)
    (root / "model.onnx").write_bytes(b"fake")
    (root / "tokens.txt").write_text("a 1", encoding="utf-8")
    assert fetch_models.check_model(
        "speech_asr", entry, tmp_path, allow_unpinned=True
    ) == []


def test_download_file_verifies_sha(tmp_path):
    source = tmp_path / "source.bin"
    payload = b"model-bytes" * 1024
    source.write_bytes(payload)
    expected = hashlib.sha256(payload).hexdigest()

    destination = tmp_path / "out.bin"
    assert fetch_models.download_file(
        source.as_uri(), destination, expected_sha256=expected
    ) == expected
    assert destination.read_bytes() == payload

    with pytest.raises(fetch_models.FetchError):
        fetch_models.download_file(
            source.as_uri(), tmp_path / "bad.bin", expected_sha256="0" * 64
        )
    assert not (tmp_path / "bad.bin").exists()


def test_registry_sources_accepts_fastembed_dict_layout():
    """fastembed 0.9.x 把 sources 从 list 改成 dict：不能取到键名（hf）。"""
    assert fetch_models._registry_sources(
        {"sources": {"hf": "BAAI/bge-reranker-base", "url": None}}, "BAAI/bge-reranker-base"
    ) == ["BAAI/bge-reranker-base"]
    # 旧版列表布局与其他键名仍可用。
    assert fetch_models._registry_sources(
        {"sources": ["Qdrant/legacy"]}, "Qdrant/legacy"
    ) == ["Qdrant/legacy"]
    assert fetch_models._registry_sources(
        {"sources": {"url": "https://example.com/m.onnx"}}, "x"
    ) == ["https://example.com/m.onnx"]
    assert fetch_models._registry_sources({}, "fallback") == ["fallback"]


def test_lock_file_schema_is_complete():
    payload = json.loads(
        (Path(__file__).resolve().parents[1] / "deploy" / "models.lock.json").read_text(
            encoding="utf-8"
        )
    )
    models = payload["models"]
    for key in ("embeddings", "rerank", "speech_tts", "speech_asr", "formula", "table"):
        assert key in models, key
    for key in ("embeddings", "rerank"):
        entry = models[key]
        assert entry["provider"] in ("huggingface", "fastembed")
        assert entry["license"] == "MIT"
        assert entry["target_dir"].startswith("models/")
        assert entry["required_files"]
    for key in ("speech_tts", "speech_asr"):
        assert models[key]["provider"] == "manual"
        assert models[key]["required_files"]


def test_duplicate_distributions_groups_same_package():
    duplicates = oss_env_check._duplicate_distributions([
        ("Pillow", "12.2.0"),
        ("pillow", "13.0.0"),
        ("numpy", "2.2.6"),
    ])
    assert duplicates == {"pillow": ["12.2.0", "13.0.0"]}


def test_runtime_report_has_coexistence_fields():
    report = oss_env_check.runtime_report()
    assert "duplicate_distributions" in report
    assert isinstance(report["onnx_runtime_dlls"], list)
    assert isinstance(report["onnx_dll_coexistence"], bool)
    assert report["pip_check"]["ok"] is None


def test_package_status_missing_module():
    status = oss_env_check._package_status(
        "module_that_does_not_exist_for_env_check", "missing-dist"
    )
    assert status["installed"] is False
    assert status["version"] is None


def test_check_simple_reflects_packages(monkeypatch):
    monkeypatch.setattr(
        oss_env_check,
        "_packages",
        lambda engine: [
            {"module": "x", "distribution": "x", "installed": False, "version": None}
        ],
    )
    assert oss_env_check._check_simple("pdf")["available"] is False


def test_build_report_marks_unavailable(monkeypatch, tmp_path):
    monkeypatch.setattr(
        oss_env_check,
        "check_engine",
        lambda engine, **kwargs: {"available": False, "packages": [], "model": {}},
    )
    report = oss_env_check.build_report(
        ["pdf"], with_sha=False, load=False, load_timeout=1,
        lock_path=tmp_path / "missing.lock",
    )
    assert report["ok"] is False
    assert "pdf" in report["engines"]
    assert "锁文件不可读" in report["lock"]["error"]


def test_main_soft_and_strict_exit_codes(monkeypatch, capsys):
    monkeypatch.setattr(
        oss_env_check,
        "check_engine",
        lambda *args, **kwargs: {"available": False, "packages": [], "model": {}},
    )
    assert oss_env_check.main(["--engine", "pdf", "--no-sha"]) == 1
    capsys.readouterr()
    assert oss_env_check.main(["--engine", "pdf", "--no-sha", "--soft"]) == 0


def test_load_timeout_returns_failure():
    import time

    result = oss_env_check.run_with_timeout(lambda: time.sleep(0.2), 0.01)
    assert result["ok"] is False
    assert "超时" in result["error"]
