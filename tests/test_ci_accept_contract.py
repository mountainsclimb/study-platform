"""验收脚本契约测试（P0：验收脚本不得再出现「成功假象」）。

只验证阶段编排与失败传播，**不跑全量 pytest**（任务明确要求），用假阶段/假命令覆盖四种退出码：
- 全部选中阶段执行且全过（skipped==0）→ exit 0，汇总 ``ACCEPT OK``；
- 任一阶段失败（含假失败命令、阶段异常、UI 服务器不可达）→ exit 1，汇总 ``ACCEPT FAILED``；
- 有阶段被跳过（工具缺失 / -SkipPytest / -Stages 过滤）→ exit 3，汇总 ``ACCEPT INCOMPLETE``
  并列出跳过阶段，不得再出现 ``ACCEPT OK``；
- 没有任何阶段真正执行（空计划/全跳过，漏跑）→ exit 3 / ``INCOMPLETE``；
- 未知阶段键 → 参数错误 exit 2；
- 报告 JSON 的 ``accept`` 与 ``exit_code`` 保持一致（OK / FAILED / INCOMPLETE）；
- 默认题库跟随 ``releases/CURRENT``（r5），r3 只能显式 --bank-dir 指定；
- ``scripts/ci_accept.ps1`` 外壳把 runner 的退出码原样传出。
"""
import json
import shutil
import socket
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
CURRENT = ROOT.parent / "高考题库_按学科" / "releases" / "CURRENT"
CI_ACCEPT = ROOT / "scripts" / "ci_accept.ps1"

sys.path.insert(0, str(ROOT))

from scripts import accept_runner as runner  # noqa: E402


def _fake_ok(key="fake-ok", name="假通过阶段"):
    return runner.Stage(
        key=key, name=name,
        command=[sys.executable, "-c", "print('fake ok')"], timeout=60,
    )


def _fake_fail(key="fake-fail", name="假失败阶段", code=3):
    return runner.Stage(
        key=key, name=name,
        command=[sys.executable, "-c", f"import sys; sys.exit({code})"], timeout=60,
    )


def _closed_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _statuses(summary):
    return {stage["key"]: stage["status"] for stage in summary["stages"]}


def test_all_stages_executed_and_passed_is_exit_zero():
    summary = runner.run_stages([_fake_ok(), _fake_ok("fake-ok-2", "假通过阶段2")],
                                build_id="test-build")
    assert summary["exit_code"] == 0
    assert summary["accept"] == "OK"
    assert summary["ok"] is True
    assert summary["counts"] == {
        "total": 2, "ok": 2, "failed": 0, "skipped": 0, "executed": 2,
    }
    assert summary["failed_stages"] == [] and summary["skipped_stages"] == []


def test_failed_stage_propagates_nonzero_and_report(tmp_path):
    report_path = tmp_path / "accept.json"
    summary = runner.run_stages(
        [_fake_ok(), _fake_fail()], build_id="test-build", report_path=report_path,
    )
    assert summary["exit_code"] == 1, "阶段失败必须 exit 1"
    assert summary["ok"] is False
    assert summary["accept"] == "FAILED"
    assert summary["failed_stages"] == ["fake-fail"]
    assert _statuses(summary) == {"fake-ok": "ok", "fake-fail": "fail"}
    assert report_path.is_file(), "失败也必须落盘报告，便于定位漏跑/失败原因"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["ok"] is False and report["exit_code"] == 1
    assert report["accept"] == "FAILED"
    assert report["failed_stages"] == ["fake-fail"]
    assert report["build_id"] == "test-build"
    assert {stage["key"]: stage["status"] for stage in report["stages"]} == {
        "fake-ok": "ok", "fake-fail": "fail",
    }
    assert report["counts"] == {
        "total": 2, "ok": 1, "failed": 1, "skipped": 0, "executed": 2,
    }


def test_missing_tool_stage_is_incomplete_exit_three(tmp_path, capsys):
    """有 SKIP（工具缺失）时即使其余阶段全过也不能 ACCEPT OK：exit 3 / INCOMPLETE。"""
    skip_stage = runner.Stage(
        key="fake-skip", name="假跳过阶段",
        needs=("ws1-definitely-missing-tool",),
        command=["ws1-definitely-missing-tool", "--version"], timeout=60,
    )
    report_path = tmp_path / "accept-skip.json"
    summary = runner.run_stages([_fake_ok(), skip_stage], build_id="test-build",
                                report_path=report_path)
    output = capsys.readouterr().out
    assert summary["exit_code"] == 3, "只要存在 SKIP 就必须 exit 3（不能算成功）"
    assert summary["accept"] == "INCOMPLETE"
    assert summary["ok"] is False
    assert "ACCEPT INCOMPLETE" in output
    assert "ACCEPT OK" not in output, "存在跳过阶段时不得打印 ACCEPT OK"
    assert summary["skipped_stages"] == ["fake-skip"]
    statuses = _statuses(summary)
    assert statuses["fake-skip"] == "skip"
    assert statuses["fake-skip"] != "ok"
    assert summary["counts"]["ok"] == 1 and summary["counts"]["skipped"] == 1
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["accept"] == "INCOMPLETE" and report["exit_code"] == 3, \
        "报告 accept 字段必须与 exit_code 一致"
    assert report["skipped_stages"] == ["fake-skip"]


def test_all_skipped_plan_is_incomplete_and_nonzero():
    skip_stage = runner.Stage(
        key="fake-skip", name="假跳过阶段",
        needs=("ws1-definitely-missing-tool",), command=["ws1-definitely-missing-tool"],
    )
    summary = runner.run_stages([skip_stage], build_id="test-build")
    assert summary["exit_code"] == 3, "全部跳过（漏跑）不能返回成功"
    assert summary["accept"] == "INCOMPLETE"
    assert summary["counts"]["executed"] == 0


def test_empty_stage_plan_is_nonzero():
    summary = runner.run_stages([], build_id="test-build")
    assert summary["exit_code"] == 3, "没有阶段真正执行（漏跑）必须非零"
    assert summary["accept"] == "INCOMPLETE"
    assert summary["counts"]["total"] == 0
    assert summary["counts"]["executed"] == 0


def test_stage_exception_is_failure_not_silent_pass():
    def boom():
        raise RuntimeError("阶段内部异常")

    summary = runner.run_stages(
        [runner.Stage(key="fake-exc", name="假异常阶段", runner=boom)], build_id="test-build",
    )
    assert summary["exit_code"] == 1
    assert summary["accept"] == "FAILED"
    assert summary["failed_stages"] == ["fake-exc"]
    detail = summary["stages"][0]["detail"]
    assert "阶段异常" in detail and "RuntimeError" in detail


def test_unknown_stage_key_is_usage_error(capsys):
    code = runner.main(["--stages", "ws1-no-such-stage", "--plan-json"])
    assert code == 2
    assert "未知阶段键" in capsys.readouterr().err


def test_ui_unreachable_server_fails_and_blocks_following_ui_stages():
    base_url = f"http://127.0.0.1:{_closed_port()}"
    server = runner.Stage(
        key="ui-server", name="UI 服务器可达性",
        runner=lambda: runner.ui_server_stage(base_url),
    )
    routes = runner.Stage(
        key="ui-routes", name="UI 路由核对",
        command=[sys.executable, "-c", "print('should not run')"], requires=("ui-server",),
    )
    summary = runner.run_stages([server, routes], base_url=base_url, ui=True, build_id="test-build")
    assert summary["exit_code"] == 1, "UI 模式服务器不可达必须 exit 1"
    assert summary["accept"] == "FAILED"
    assert summary["failed_stages"] == ["ui-server"]
    statuses = _statuses(summary)
    assert statuses["ui-routes"] == "skip"
    assert "不可达" in summary["stages"][0]["detail"]


def test_plan_defaults_to_current_pointer_not_r3(capsys):
    assert runner.main(["--plan-json", "--build-id", "test-build"]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["build_id"] == "test-build"
    keys = plan["stage_keys"]
    for required in ("bank", "compileall", "node-check", "editor-typecheck", "pytest"):
        assert required in keys, f"默认全量验收缺少阶段 {required}"
    # UI 需要显式 -Ui：默认计划里不出现；服务器不可达时不能假装跑过。
    assert "ui-server" not in keys and "ui-routes" not in keys
    # 编辑器构建比对默认关闭，避免无 Node 环境无法验收。
    assert "editor-build" not in keys
    if not CURRENT.is_file():
        pytest.skip("当前环境没有 releases/CURRENT，跳过基线断言")
    release = CURRENT.read_text(encoding="utf-8-sig").strip()
    assert plan["bank"]["ok"] is True
    assert plan["bank"]["source"] == "releases/CURRENT"
    assert plan["bank"]["release"] == release
    assert plan["bank"]["bank_version"], "计划里要能读出 bank_version"
    assert "r3" not in plan["bank"]["release"]
    # r3 只能在显式 --bank-dir 时出现。
    assert "bank-20261120-r3" not in plan["bank"]["dir"]


def test_ui_and_editor_build_are_opt_in(capsys):
    assert runner.main(["--plan-json", "--build-id", "b", "--ui",
                        "--base-url", "http://127.0.0.1:8840"]) == 0
    ui_plan = json.loads(capsys.readouterr().out)
    assert {"ui-server", "ui-routes", "ui-modules"} <= set(ui_plan["stage_keys"])

    assert runner.main(["--plan-json", "--build-id", "b", "--editor-build"]) == 0
    build_plan = json.loads(capsys.readouterr().out)
    assert "editor-build" in build_plan["stage_keys"]

    # 显式题库路径要回显来源，便于核对验收到底用了哪一版。
    assert runner.main(["--plan-json", "--build-id", "b",
                        "--bank-dir", str(ROOT / "no-such-bank")]) == 0
    explicit_plan = json.loads(capsys.readouterr().out)
    assert explicit_plan["bank"]["source"] == "arg"
    assert explicit_plan["bank"]["ok"] is False


def test_skip_pytest_leads_to_incomplete_exit_three(capsys):
    """-SkipPytest 是显式跳过：阶段状态 SKIP → exit 3 / ACCEPT INCOMPLETE，不得 ACCEPT OK。"""
    assert runner.main(["--plan-json", "--build-id", "b", "--skip-pytest"]) == 0
    plan = json.loads(capsys.readouterr().out)
    pytest_stage = next(stage for stage in plan["stages"] if stage["key"] == "pytest")
    assert "显式跳过" in pytest_stage["description"]

    stages = runner.build_stages(selected=["pytest"], skip_pytest=True)
    summary = runner.run_stages(stages, build_id="test-build")
    output = capsys.readouterr().out
    assert summary["exit_code"] == 3
    assert summary["accept"] == "INCOMPLETE"
    assert "ACCEPT INCOMPLETE" in output and "ACCEPT OK" not in output
    statuses = _statuses(summary)
    assert statuses["pytest"] == "skip"
    assert "pytest" in summary["skipped_stages"]


def test_stages_filter_leads_to_incomplete_exit_three(capsys):
    """-Stages 过滤掉的阶段必须是 SKIP：部分运行不能宣称全量验收通过。"""
    stages = runner.build_stages(selected=["bank"])
    summary = runner.run_stages(stages, build_id="test-build")
    output = capsys.readouterr().out
    assert summary["exit_code"] == 3
    assert summary["accept"] == "INCOMPLETE"
    assert "ACCEPT OK" not in output
    assert set(summary["skipped_stages"]) >= {"compileall", "node-check", "editor-typecheck", "pytest"}


def _powershell():
    for candidate in ("pwsh", "powershell"):
        found = shutil.which(candidate)
        if found:
            return found
    return None


@pytest.mark.parametrize("case", ["bad-bank", "incomplete"])
def test_ci_accept_ps1_propagates_runner_exit_code(tmp_path, case):
    """外壳必须原样透传 runner 的退出码：1=失败、3=未完成（有跳过）。"""
    shell = _powershell()
    if shell is None:
        pytest.skip("本机没有 PowerShell，跳过外壳退出码联测")
    report_path = tmp_path / f"ps1-{case}.json"
    command = [
        shell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(CI_ACCEPT),
        "-Stages", "bank", "-ReportPath", str(report_path),
    ]
    if case == "bad-bank":
        command += ["-BankDir", str(tmp_path / "no-such-bank")]
        expected_code, expected_accept = 1, "FAILED"
    else:
        if not CURRENT.is_file():
            pytest.skip("当前环境没有 releases/CURRENT，跳过未完成路径联测")
        # -Stages bank 只跑基线阶段，其余阶段记 SKIP → 必须 exit 3 / INCOMPLETE。
        expected_code, expected_accept = 3, "INCOMPLETE"
    completed = subprocess.run(
        command, cwd=str(ROOT), capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=180,
    )
    assert report_path.is_file(), completed.stdout + completed.stderr
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert completed.returncode == expected_code, completed.stdout + completed.stderr
    assert report["exit_code"] == expected_code
    assert report["accept"] == expected_accept
    assert report["ok"] is (expected_code == 0)
    if case == "bad-bank":
        assert report["failed_stages"] == ["bank"]
    else:
        assert report["bank"]["bank_version"]
        assert report["build_id"]
        assert set(report["skipped_stages"]) >= {"compileall", "node-check", "pytest"}
