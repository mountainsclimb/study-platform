"""axe-core 无障碍脚本（scripts/ui_check_a11y.py）的纯函数与资产自检。

不启动浏览器：路由解析、违规指纹、基线与报告渲染都用固定数据驱动；
真实浏览器扫描由 `-Ui` 验收的 ui-a11y 阶段执行。
"""
from pathlib import Path
import hashlib
import json

from scripts import ui_check_a11y as a11y

ROOT = Path(__file__).resolve().parents[1]


def test_axe_vendor_asset_matches_readme_checksum():
    axe = ROOT / "scripts" / "vendor" / "axe.min.js"
    assert axe.is_file()
    digest = hashlib.sha256(axe.read_bytes()).hexdigest()
    readme = (ROOT / "scripts" / "vendor" / "README.md").read_text(encoding="utf-8")
    assert digest in readme
    assert str(axe.stat().st_size) in readme or f"{axe.stat().st_size:,}" in readme
    assert "4.14.1" in readme
    assert (ROOT / "scripts" / "vendor" / "LICENSE-axe").is_file()


def test_resolve_routes_accepts_names_and_raw_hashes():
    routes = a11y.resolve_routes(["home", "#/notes", "settings"])
    assert routes[0] == ("home", "#/", a11y.ROUTE_SPECS["home"]["ready"])
    assert routes[1][0] == "notes" and routes[1][1] == "#/notes"
    assert routes[2][0] == "settings" and routes[2][1] == "#/settings"


def test_resolve_routes_defaults_to_main_routes():
    routes = a11y.resolve_routes(None)
    assert [route[0] for route in routes] == list(a11y.DEFAULT_ROUTES)
    assert all(route[1].startswith("#/") for route in routes)


def _entry(route="home", viewport="desktop", *, violations=None, error=None):
    return {
        "route": route,
        "hash": "#/",
        "viewport": viewport,
        "violations": violations or [],
        "incomplete": [],
        "error": error,
    }


def _violation(rule="color-contrast", targets=("div.a",), impact="serious"):
    return {
        "id": rule,
        "impact": impact,
        "help": "help",
        "helpUrl": "https://example.invalid",
        "description": "description",
        "nodes": [{"target": [target], "html": "<div>", "failureSummary": "summary"} for target in targets],
    }


def test_fingerprint_is_stable_and_target_specific():
    violation = _violation()
    first = a11y.fingerprint("home", "desktop", violation, violation["nodes"][0])
    second = a11y.fingerprint("home", "desktop", violation, violation["nodes"][0])
    assert first == second
    assert first == "home|desktop|color-contrast|div.a"
    assert a11y.fingerprint("notes", "mobile", violation, violation["nodes"][0]).startswith("notes|mobile|")


def test_summarize_counts_new_against_baseline():
    entry = _entry(violations=[_violation(targets=("div.a", "div.b"))])
    old_key = "home|desktop|color-contrast|div.a"
    summary = a11y.summarize([entry], {old_key})
    assert summary["scans"] == 1
    assert summary["violations"] == 1
    assert summary["critical"] == 0 and summary["serious"] == 1
    assert summary["new"] == 1
    assert summary["new_fingerprints"] == ["home|desktop|color-contrast|div.b"]
    assert summary["errors"] == 0

    all_old = a11y.summarize([entry], {old_key, "home|desktop|color-contrast|div.b"})
    assert all_old["new"] == 0


def test_summarize_counts_scan_errors_and_critical():
    entry = _entry(violations=[_violation(impact="critical")], error="TimeoutError: boom")
    summary = a11y.summarize([entry], set())
    assert summary["critical"] == 1
    assert summary["errors"] == 1
    assert summary["new"] == 1


def test_current_fingerprints_sorted_and_unique():
    entry = _entry(violations=[
        _violation(targets=("div.b", "div.a")),
        _violation(rule="label", targets=("input.x",)),
    ])
    values = a11y.current_fingerprints([entry])
    assert values == sorted(set(values))
    assert len(values) == 3


def test_render_markdown_contains_summary_and_details(tmp_path):
    entry = _entry(violations=[_violation()])
    report = {
        "generated_at": "2026-11-23T00:00:00+00:00",
        "base_url": "http://127.0.0.1:8840",
        "axe_version": "4.14.1",
        "baseline": str(tmp_path / "baseline.json"),
        "summary": a11y.summarize([entry], set()),
        "entries": [entry],
    }
    markdown = a11y.render_markdown(report)
    assert "无障碍回归报告" in markdown
    assert "color-contrast" in markdown
    assert "新增违规" in markdown
    assert "http://127.0.0.1:8840" in markdown


def test_main_rejects_missing_axe_asset(tmp_path, capsys):
    assert a11y.main(["--axe", str(tmp_path / "no-axe.js")]) == 2
    assert "缺少 axe-core" in capsys.readouterr().out


def test_parse_args_defaults():
    args = a11y.parse_args([])
    assert args.base_url == "http://127.0.0.1:8840"
    assert args.update_baseline is False
    assert json.dumps(args.routes) == '""'
