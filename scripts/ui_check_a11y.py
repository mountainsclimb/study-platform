"""axe-core 无障碍回归（WCAG 2A / 2AA，桌面 1440 与手机 390 双视口）。

按《开源组件整合实施方案》§2.1 落地：

* axe-core 只在开发脚本里使用，不进运行时、镜像与 ``web/vendor/``；
* 每个路由注入 axe 源码并执行 ``axe.run``，规则集取 WCAG 2A 与 2AA；
* 存在 ``critical`` 或 ``serious`` 级**新增**违规即退出码 1；首次运行可
  ``--update-baseline`` 固化存量基线，之后只对新增违规判失败；
* 输出 JSON 与 Markdown 报告到 ``--out``（默认 ``screenshots/a11y``）。

用法（先启动隔离实例）：
    python scripts/ui_check_a11y.py --base-url http://127.0.0.1:8840
    python scripts/ui_check_a11y.py --update-baseline   # 首次固化基线
退出码：0=无新增 critical/serious；1=存在新增违规或扫描失败；2=环境错误。
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = ROOT / "screenshots" / "a11y"
DEFAULT_AXE = ROOT / "scripts" / "vendor" / "axe.min.js"
DEFAULT_BASELINE = DEFAULT_OUT / "a11y-baseline.json"

VIEWPORTS = {
    "desktop": {"width": 1440, "height": 900},
    "mobile": {"width": 390, "height": 844},
}

# 全部注册路由（20 个 SPA 页面 + Pyodide 专用页）；``--routes`` 可用路由名或
# 直接写 ``#/...`` / ``/lab/python`` 覆盖。登录账号在隔离库中为首个账号（管理员），
# 管理/评审页可正常渲染。
ROUTE_SPECS = {
    "home": {"hash": "#/", "ready": ".layout, .page-heading, .pg-focus-grid"},
    "practice": {"hash": "#/practice", "ready": ".layout, .page-heading, .pg-panel"},
    "mybank": {"hash": "#/mybank", "ready": ".page-heading, .layout"},
    "submission": {"hash": "#/submission", "ready": ".page-heading, .layout"},
    "wrongbook": {"hash": "#/wrongbook", "ready": ".layout, .page-heading, .pg-filter-bar"},
    "upload": {"hash": "#/upload", "ready": ".layout, .page-heading, .pg-step-rail"},
    "paperlab": {"hash": "#/paperlab", "ready": ".page-heading, .layout"},
    "classroom": {"hash": "#/classroom", "ready": ".page-heading, .layout"},
    "physics": {"hash": "#/physics", "ready": ".layout, .page-heading"},
    "plaza": {"hash": "#/plaza", "ready": ".page-heading, .layout"},
    "insight": {"hash": "#/insight", "ready": ".layout, .page-heading"},
    "vocab": {"hash": "#/vocab", "ready": ".page-heading, .layout"},
    "review": {"hash": "#/review", "ready": ".page-heading, .layout"},
    "plaza-admin": {"hash": "#/plaza-admin", "ready": ".page-heading, .layout"},
    "notes": {"hash": "#/notes", "ready": ".layout, .page-heading, .pg-tabs"},
    "assistant": {"hash": "#/assistant", "ready": ".page-heading, .layout"},
    "tasks": {"hash": "#/tasks", "ready": ".page-heading, .layout"},
    "admission": {"hash": "#/admission", "ready": ".page-heading, .layout"},
    "settings": {"hash": "#/settings", "ready": ".layout, .page-heading, .pg-api-status"},
    "admin": {"hash": "#/admin", "ready": ".page-heading, .layout"},
    "lab-python": {"hash": "/lab/python", "ready": ".lab-main"},
}
DEFAULT_ROUTES = tuple(ROUTE_SPECS)

#: 组件级检查（弹层/新面板）：先打开触发器，再扫描指定容器。默认不运行，
#: 由 ``--components`` 显式选择；只在触发器存在时执行。
COMPONENT_SPECS = {
    "notes-demo-menu": {
        "hash": "#/notes", "ready": ".page-heading",
        "open": "details.demo-create-menu summary", "scan": ".demo-create-list",
    },
    "vocab-shadow": {
        "hash": "#/vocab", "ready": ".page-heading",
        "open": "[data-speech-shadow-toggle]", "scan": ".speech-shadow",
    },
}

SCAN_SCRIPT = """
async () => {
  const axe = window.axe;
  if (!axe || typeof axe.run !== 'function') {
    throw new Error('axe 未注入成功');
  }
  const result = await axe.run(document, {
    runOnly: { type: 'tag', values: ['wcag2a', 'wcag2aa'] },
  });
  const keep = (violations) => (violations || [])
    .filter(item => item.impact === 'critical' || item.impact === 'serious')
    .map(item => ({
      id: item.id,
      impact: item.impact,
      help: item.help,
      helpUrl: item.helpUrl,
      description: item.description,
      nodes: (item.nodes || []).map(node => ({
        target: node.target || [],
        html: String(node.html || '').slice(0, 300),
        failureSummary: String(node.failureSummary || '').slice(0, 400),
      })),
    }));
  return {
    violations: keep(result.violations),
    incomplete: keep(result.incomplete),
    passes: (result.passes || []).length,
    axeVersion: (axe.version || ''),
  };
}
"""


def launch_browser(playwright):
    """优先本机 Edge；未安装时回退 Playwright 自带 Chromium。"""
    for channel in ("msedge", "chrome", None):
        try:
            if channel:
                return playwright.chromium.launch(channel=channel, headless=True)
            return playwright.chromium.launch(headless=True)
        except Exception:  # noqa: BLE001 - 逐个渠道探测，全部失败才抛错
            continue
    raise RuntimeError("未找到可用的浏览器（msedge / chrome / chromium 均不可用）")


def login(page, base_url: str, username: str, password: str) -> None:
    """登录；隔离库里账号不存在时自动注册一次性验收账号（与 ui_check_pages 一致）。"""
    page.goto(base_url + "/", wait_until="domcontentloaded")
    try:
        page.wait_for_selector(".layout, .page-heading", timeout=8000)
        return
    except Exception:
        pass
    page.wait_for_selector(".auth-form-card", timeout=15000)
    page.get_by_label("用户名").fill(username)
    page.get_by_label("密码").fill(password)
    page.get_by_role("button", name="登录学习工作台").click()
    try:
        page.wait_for_selector(".layout, .page-heading", timeout=15000)
        return
    except Exception:
        pass
    page.locator(".auth-switch .text-button").filter(has_text="创建账号").click()
    page.wait_for_selector(".auth-form-card", timeout=10000)
    page.get_by_label("用户名").fill(username)
    page.get_by_label("密码").fill(password)
    page.get_by_role("button", name="创建我的学习账号").click()
    page.wait_for_selector(".layout, .page-heading", timeout=25000)


def resolve_routes(values: list[str] | None) -> list[tuple[str, str, str]]:
    routes: list[tuple[str, str, str]] = []
    for value in values or DEFAULT_ROUTES:
        key = str(value).strip()
        if not key:
            continue
        if key in ROUTE_SPECS:
            spec = ROUTE_SPECS[key]
            routes.append((key, spec["hash"], spec["ready"]))
            continue
        by_hash = next(
            (name for name, spec in ROUTE_SPECS.items() if spec["hash"] == key), None,
        )
        if by_hash is not None:
            routes.append((by_hash, key, ROUTE_SPECS[by_hash]["ready"]))
            continue
        if key.startswith("/"):
            routes.append((key.strip("/").replace("/", "-") or "page", key, ".layout, .page-heading, #app, .lab-main"))
            continue
        if not key.startswith("#/"):
            key = "#/" + key.lstrip("#/")
        routes.append((key.lstrip("#/") or "home", key, ".layout, .page-heading, #app"))
    return routes


def resolve_components(values: list[str] | None) -> list[tuple[str, dict]]:
    """解析组件级检查项；未知名直接抛 ``ValueError``（避免静默漏扫）。"""
    items: list[tuple[str, dict]] = []
    for value in values or []:
        key = str(value).strip()
        if not key:
            continue
        if key not in COMPONENT_SPECS:
            raise ValueError(f"未知组件检查项：{key}")
        items.append((key, COMPONENT_SPECS[key]))
    return items


def dismiss_daily_quote(page) -> None:
    """关闭首屏「每日一句」弹层并**等它真正移除**再扫描。

    弹层用 260ms 淡出过渡（``.is-out`` 期间 ``opacity`` 从 1 降到 0），此时 axe 读到的
    是「半透明文字与背景混合后」的前景色，会把达标的配色（白字 + ``#286552``，
    6.83:1）误判成 3.69:1。按 Esc 后必须等元素从 DOM 消失再注入 axe。
    """
    try:
        if page.locator(".dq-dialog").count() == 0:
            return
        page.keyboard.press("Escape")
        page.wait_for_selector(".dq-dialog", state="detached", timeout=3000)
    except Exception:  # noqa: BLE001 - 弹层不存在/未消失都不阻塞扫描
        pass


def fingerprint(route: str, viewport: str, violation: dict, node: dict) -> str:
    target = " >> ".join(str(part) for part in node.get("target") or [])
    return f"{route}|{viewport}|{violation.get('id')}|{target}"


def scan_route(page, base_url: str, route: str, hash_value: str, ready: str,
               viewport_name: str, axe_source: str, timeout_ms: int) -> dict:
    entry = {
        "route": route,
        "hash": hash_value,
        "viewport": viewport_name,
        "violations": [],
        "incomplete": [],
        "error": None,
    }
    try:
        url = base_url + hash_value if hash_value.startswith("/") else base_url + "/" + hash_value
        page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        page.wait_for_selector(ready, timeout=timeout_ms)
        page.wait_for_timeout(600)
        # 首屏「每日一句」弹层会遮挡页面；先关闭（并等淡出结束）再扫描。
        dismiss_daily_quote(page)
        # CDP 驱动的 evaluate 不受 script-src 限制；这里只注入开发资产，不修改页面文件。
        page.evaluate("(source) => { (0, eval)(source); }", axe_source)
        result = page.evaluate(SCAN_SCRIPT)
        entry["violations"] = result.get("violations") or []
        entry["incomplete"] = result.get("incomplete") or []
        entry["passes"] = result.get("passes")
        entry["axe_version"] = result.get("axeVersion")
    except Exception as error:  # 页面未渲染/axe 注入失败都按扫描失败处理
        entry["error"] = f"{type(error).__name__}: {str(error)[:300]}"
    return entry


def scan_component(page, base_url: str, name: str, spec: dict, viewport_name: str,
                   axe_source: str, timeout_ms: int) -> dict:
    """打开组件触发器后扫描容器；触发器不存在时跳过（不记为失败）。"""
    entry = {
        "route": f"component:{name}",
        "hash": spec["hash"],
        "viewport": viewport_name,
        "violations": [],
        "incomplete": [],
        "error": None,
        "skipped": None,
    }
    try:
        url = base_url + spec["hash"] if spec["hash"].startswith("/") else base_url + "/" + spec["hash"]
        page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        page.wait_for_selector(spec["ready"], timeout=timeout_ms)
        page.wait_for_timeout(500)
        dismiss_daily_quote(page)
        trigger = page.locator(spec["open"])
        if trigger.count() == 0:
            entry["skipped"] = "触发器不存在"
            return entry
        trigger.first.click()
        page.wait_for_selector(spec["scan"], timeout=max(5000, timeout_ms // 3))
        page.wait_for_timeout(300)
        page.evaluate("(source) => { (0, eval)(source); }", axe_source)
        result = page.evaluate(SCAN_SCRIPT)
        entry["violations"] = result.get("violations") or []
        entry["incomplete"] = result.get("incomplete") or []
        entry["passes"] = result.get("passes")
        entry["axe_version"] = result.get("axeVersion")
    except Exception as error:  # noqa: BLE001 - 组件扫描失败按失败处理
        entry["error"] = f"{type(error).__name__}: {str(error)[:300]}"
    return entry


def summarize(entries: list[dict], baseline: set[str]) -> dict:
    total = critical = serious = new = 0
    new_fingerprints: list[str] = []
    for entry in entries:
        for violation in entry["violations"]:
            total += 1
            if violation.get("impact") == "critical":
                critical += 1
            elif violation.get("impact") == "serious":
                serious += 1
            for node in violation.get("nodes") or []:
                key = fingerprint(entry["route"], entry["viewport"], violation, node)
                if key not in baseline:
                    new += 1
                    if key not in new_fingerprints:
                        new_fingerprints.append(key)
    errors = [entry for entry in entries if entry.get("error")]
    return {
        "scans": len(entries),
        "violations": total,
        "critical": critical,
        "serious": serious,
        "new": new,
        "new_fingerprints": new_fingerprints,
        "errors": len(errors),
    }


def current_fingerprints(entries: list[dict]) -> list[str]:
    values: list[str] = []
    for entry in entries:
        for violation in entry["violations"]:
            for node in violation.get("nodes") or []:
                key = fingerprint(entry["route"], entry["viewport"], violation, node)
                if key not in values:
                    values.append(key)
    return sorted(values)


def render_markdown(report: dict) -> str:
    summary = report["summary"]
    lines = [
        "# 无障碍回归报告（axe-core）",
        "",
        f"- 生成时间：{report['generated_at']}",
        f"- 地址：{report['base_url']}",
        f"- axe-core：{report.get('axe_version') or '未标注'}",
        f"- 扫描次数：{summary['scans']}（桌面 1440 / 手机 390）",
        f"- critical/serious 违规：{summary['critical']} / {summary['serious']}",
        f"- 新增违规节点：{summary['new']}",
        f"- 扫描失败：{summary['errors']}",
        "",
    ]
    if report.get("baseline"):
        lines.append(f"- 基线：`{report['baseline']}`")
        lines.append("")
    if summary["new_fingerprints"]:
        lines.append("## 新增违规")
        lines.append("")
        for item in summary["new_fingerprints"][:200]:
            lines.append(f"- `{item}`")
        lines.append("")
    lines.append("## 分页结果")
    lines.append("")
    lines.append("| 路由 | 视口 | critical+serious | 通过规则 | 状态 |")
    lines.append("|---|---|---|---|---|")
    for entry in report["entries"]:
        lines.append(
            "| %s | %s | %d | %s | %s |" % (
                entry["route"],
                entry["viewport"],
                len(entry["violations"]),
                entry.get("passes", "-"),
                "失败：" + entry["error"] if entry.get("error")
                else ("跳过：" + entry["skipped"] if entry.get("skipped") else "完成"),
            )
        )
    lines.append("")
    details = [entry for entry in report["entries"] if entry["violations"]]
    if details:
        lines.append("## 违规明细")
        lines.append("")
        for entry in details:
            for violation in entry["violations"]:
                lines.append(
                    f"### {entry['route']} / {entry['viewport']} · {violation.get('impact')} · "
                    f"{violation.get('id')}"
                )
                lines.append("")
                lines.append(f"- {violation.get('help')}")
                lines.append(f"- {violation.get('helpUrl')}")
                for node in violation.get("nodes") or []:
                    lines.append(f"- 目标：`{' >> '.join(node.get('target') or [])}`")
                    if node.get("failureSummary"):
                        lines.append(f"  - {node['failureSummary'].splitlines()[0][:200]}")
                lines.append("")
    return "\n".join(lines)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="axe-core 无障碍回归（WCAG 2A/2AA）。")
    parser.add_argument("--base-url", default="http://127.0.0.1:8840")
    parser.add_argument("--username", default="无障碍验收")
    parser.add_argument("--password", default="a11y-pass-12345")
    parser.add_argument(
        "--routes",
        default="",
        help="逗号分隔的路由名或 #/路径；默认 " + ", ".join(DEFAULT_ROUTES),
    )
    parser.add_argument(
        "--components",
        default="",
        help="逗号分隔的组件级检查项（默认空）：" + ", ".join(COMPONENT_SPECS),
    )
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--axe", default=str(DEFAULT_AXE), help="axe.min.js 路径")
    parser.add_argument("--baseline", default=str(DEFAULT_BASELINE), help="违规基线 JSON")
    parser.add_argument(
        "--update-baseline", action="store_true", help="把本次结果写为基线（首次运行用）"
    )
    parser.add_argument("--timeout-ms", type=int, default=30000)
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    axe_path = Path(args.axe)
    if not axe_path.is_file():
        print(f"[环境错误] 缺少 axe-core：{axe_path}（见 scripts/vendor/README.md）")
        return 2
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("[环境错误] 未安装 playwright：pip install -r requirements-dev.txt")
        return 2
    axe_source = axe_path.read_text(encoding="utf-8")
    routes = resolve_routes([
        item for item in str(args.routes or "").split(",") if item.strip()
    ] or None)
    try:
        components = resolve_components([
            item for item in str(args.components or "").split(",") if item.strip()
        ])
    except ValueError as error:
        print(f"[环境错误] {error}")
        return 2
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    baseline_path = Path(args.baseline)
    baseline: set[str] = set()
    if baseline_path.is_file() and not args.update_baseline:
        try:
            payload = json.loads(baseline_path.read_text(encoding="utf-8"))
            baseline = set(payload.get("fingerprints") or [])
        except (ValueError, OSError):
            baseline = set()
    elif not baseline_path.is_file() and not args.update_baseline:
        print("提示：未找到基线，本次全部 critical/serious 违规都按新增处理。")

    entries: list[dict] = []
    axe_version = ""
    with sync_playwright() as playwright:
        browser = launch_browser(playwright)
        try:
            for viewport_name, viewport in VIEWPORTS.items():
                context = browser.new_context(viewport=viewport)
                page = context.new_page()
                login(page, args.base_url, args.username, args.password)
                for route, hash_value, ready in routes:
                    print(f"扫描 {route}（{viewport_name}）…")
                    entry = scan_route(
                        page, args.base_url, route, hash_value, ready,
                        viewport_name, axe_source, max(5000, args.timeout_ms),
                    )
                    axe_version = entry.get("axe_version") or axe_version
                    entries.append(entry)
                for name, spec in components:
                    print(f"扫描组件 {name}（{viewport_name}）…")
                    entry = scan_component(
                        page, args.base_url, name, spec, viewport_name,
                        axe_source, max(5000, args.timeout_ms),
                    )
                    axe_version = entry.get("axe_version") or axe_version
                    entries.append(entry)
                context.close()
        finally:
            browser.close()

    summary = summarize(entries, baseline)
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "base_url": args.base_url,
        "axe_version": axe_version or "4.14.1",
        "baseline": str(baseline_path) if baseline else None,
        "viewports": VIEWPORTS,
        "components": [name for name, _spec in components],
        "summary": summary,
        "entries": entries,
    }
    json_path = out_dir / "a11y-report.json"
    md_path = out_dir / "a11y-report.md"
    json_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    md_path.write_text(render_markdown(report), encoding="utf-8", newline="\n")
    print(f"报告：{json_path}")
    print(f"报告：{md_path}")
    print(
        "扫描 %d 次；critical=%d serious=%d 新增=%d 失败=%d" % (
            summary["scans"], summary["critical"], summary["serious"],
            summary["new"], summary["errors"],
        )
    )

    if args.update_baseline:
        baseline_path.parent.mkdir(parents=True, exist_ok=True)
        baseline_path.write_text(
            json.dumps({
                "generated_at": report["generated_at"],
                "axe_version": report["axe_version"],
                "base_url": args.base_url,
                "fingerprints": current_fingerprints(entries),
            }, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        print(f"基线已更新：{baseline_path}")
        return 0 if summary["errors"] == 0 else 1
    if summary["errors"]:
        print("[失败] 存在扫描失败的路由，见报告")
        return 1
    if summary["new"]:
        print("[失败] 存在新增 critical/serious 违规（见报告“新增违规”）")
        return 1
    print("[通过] 无新增 critical/serious 违规")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
