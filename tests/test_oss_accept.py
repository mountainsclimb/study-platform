"""可选引擎真实验收阶段（scripts/integration/oss_accept.py）验收。"""
import json

import pytest

from scripts import accept_runner, oss_env_check
from scripts.integration import oss_accept


def _report(available):
    engines = {
        name: {"available": name in available, "packages": [], "model": {}}
        for name in oss_env_check.ENGINES
    }
    return {
        "ok": bool(available),
        "generated_at": "2026-11-24T00:00:00+00:00",
        "engines": engines,
        "lock": {"path": "deploy/models.lock.json", "error": None},
    }


def test_no_engine_available_is_skip(monkeypatch, tmp_path):
    monkeypatch.setattr(
        oss_env_check, "build_report", lambda *args, **kwargs: _report([])
    )
    result = oss_accept.build_acceptance(
        with_sha=False, load_timeout=1, lock_path=tmp_path / "lock.json",
    )
    assert result["skipped"] is True
    assert result["activated"] == []
    assert oss_accept.main(["--report", str(tmp_path / "report.json")]) == 3
    payload = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert payload["skipped"] is True


def test_activated_engine_loads(monkeypatch, tmp_path):
    monkeypatch.setattr(
        oss_env_check, "build_report",
        lambda *args, **kwargs: _report(["pdf"]),
    )
    monkeypatch.setattr(
        oss_env_check, "run_with_timeout",
        lambda fn, timeout: {"ok": True, "elapsed_ms": 5},
    )
    result = oss_accept.build_acceptance(
        with_sha=False, load_timeout=1, lock_path=tmp_path / "lock.json",
    )
    assert result["ok"] is True
    assert result["loads"]["pdf"]["ok"] is True
    assert oss_accept.main(["--report", str(tmp_path / "report.json")]) == 0


def test_load_failure_fails_acceptance(monkeypatch, tmp_path):
    monkeypatch.setattr(
        oss_env_check, "build_report",
        lambda *args, **kwargs: _report(["pdf"]),
    )
    monkeypatch.setattr(
        oss_env_check, "run_with_timeout",
        lambda fn, timeout: {"ok": False, "error": "模型损坏"},
    )
    result = oss_accept.build_acceptance(
        with_sha=False, load_timeout=1, lock_path=tmp_path / "lock.json",
    )
    assert result["ok"] is False
    assert any("模型损坏" in item for item in result["problems"])
    assert oss_accept.main(["--report", str(tmp_path / "report.json")]) == 1


def test_required_engine_missing_fails(monkeypatch, tmp_path):
    monkeypatch.setattr(
        oss_env_check, "build_report",
        lambda *args, **kwargs: _report(["pdf"]),
    )
    monkeypatch.setattr(
        oss_env_check, "run_with_timeout", lambda fn, timeout: {"ok": True},
    )
    result = oss_accept.build_acceptance(
        with_sha=False, load_timeout=1, lock_path=tmp_path / "lock.json",
        require=["embeddings"],
    )
    assert any("embeddings" in item for item in result["problems"])


def test_accept_runner_registers_oss_stage_only_with_flag():
    stages = accept_runner.build_stages(selected=["oss"], oss=True)
    by_key = {stage.key: stage for stage in stages}
    assert by_key["oss"].command is not None
    assert by_key["oss"].skip_returncodes.get(3)
    assert by_key["pytest"].command is None  # 未选中阶段保留为 SKIP 记录

    with pytest.raises(accept_runner.UsageError):
        accept_runner.build_stages(selected=["oss"])
