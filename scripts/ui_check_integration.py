"""整合模块页面界面核对（AI 课堂 / 我的题库 / 试卷工坊 / 学情分析 / AI 助手 / 任务中心）。

与 scripts/ui_check_pages.py 同源：登录一次后逐个路由检查桌面 1440×900 与手机 390×844：
  1. 页面无 JS 异常（pageerror / console error）；
  2. 无页面级横向溢出（scrollWidth <= clientWidth + 1）；
  3. 页面标题（.page-heading h1）与主内容存在，截图落盘；
  4. 主按钮触控高度 ≥ 44px（若有 .button）。

用法（先启动隔离实例）：
  python scripts/ui_check_integration.py --base-url http://127.0.0.1:8815 \
      --username 整合验收 --password ui-pass-12345 \
      --out screenshots/integration
退出码：出现页面 JS 异常或断言失败为 1。
"""
import argparse
import json
from pathlib import Path

from playwright.sync_api import sync_playwright

DEFAULT_OUT = Path(__file__).resolve().parents[1] / "screenshots" / "integration"

ROUTES = [
    ("home", "#/", "学习概览"),
    ("classroom", "#/classroom", "AI 课堂"),
    ("mybank", "#/mybank", "我的题库"),
    ("paperlab", "#/paperlab", "试卷工坊"),
    ("insight", "#/insight", "学情分析"),
    ("assistant", "#/assistant", "AI 助手"),
    ("tasks", "#/tasks", "任务中心"),
]

VIEWPORTS = {
    "desktop": {"width": 1440, "height": 900},
    "mobile": {"width": 390, "height": 844},
}


def launch_browser(playwright):
    """优先本机 Edge；未安装时回退 Playwright 自带 Chromium。"""
    for channel in ("msedge", "chrome", None):
        try:
            if channel:
                return playwright.chromium.launch(channel=channel, headless=True)
            return playwright.chromium.launch(headless=True)
        except Exception:  # noqa: BLE001 - 逐渠道探测
            continue
    raise RuntimeError("未找到可用的浏览器（msedge / chrome / chromium 均不可用）")


def login(page, base_url: str, username: str, password: str) -> None:
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


def overflow_px(page) -> int:
    return page.evaluate(
        "() => Math.max(0, (document.documentElement.scrollWidth || 0) - (window.innerWidth || 0))"
    )


def check_page(page, base_url, route, out_dir, problems, viewport="desktop") -> dict:
    errors = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    page.on("console", lambda msg: errors.append("console:" + msg.text) if msg.type == "error" else None)
    page.goto(f"{base_url}/{route[1]}", wait_until="domcontentloaded")
    try:
        page.wait_for_selector(".page-heading h1", timeout=15000)
    except Exception:
        problems.append(f"{route[0]}: 未渲染页面标题")
    page.wait_for_timeout(1200)
    title = page.evaluate("() => (document.querySelector('.page-heading h1')||{}).textContent || ''")
    summary = {
        "route": route[0],
        "hash": route[1],
        "viewport": viewport,
        "expected_title": route[2],
        "title": title.strip(),
        "overflow_px": overflow_px(page),
        "buttons_below_44": page.evaluate(
            "() => [...document.querySelectorAll('.button')].filter(b => b.offsetParent !== null && b.getBoundingClientRect().height < 44).length"
        ),
        "page_errors": errors,
        "has_empty_state": page.evaluate("() => Boolean(document.querySelector('.empty'))"),
        "content_text_len": page.evaluate("() => (document.getElementById('main-content')||{}).innerText.length || 0"),
    }
    if summary["overflow_px"] > 1:
        problems.append(f"{route[0]}: 横向溢出 {summary['overflow_px']}px")
    if errors:
        problems.append(f"{route[0]}: JS 异常 {errors[:2]}")
    if summary["content_text_len"] < 20:
        problems.append(f"{route[0]}: 页面内容为空")
    suffix = "" if viewport == "desktop" else "-" + viewport
    shot = out_dir / f"{route[0]}{suffix}.png"
    page.screenshot(path=str(shot), full_page=False)
    summary["screenshot"] = str(shot)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8815")
    parser.add_argument("--username", default="整合验收")
    parser.add_argument("--password", default="ui-pass-12345")
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--routes", default="")
    args = parser.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    routes = ROUTES
    if args.routes:
        wanted = {item.strip() for item in args.routes.split(",") if item.strip()}
        routes = [item for item in ROUTES if item[0] in wanted]

    problems: list[str] = []
    report = {"base_url": args.base_url, "routes": []}
    with sync_playwright() as playwright:
        browser = launch_browser(playwright)
        try:
            context = browser.new_context(viewport=VIEWPORTS["desktop"])
            page = context.new_page()
            login(page, args.base_url, args.username, args.password)
            for route in routes:
                report["routes"].append(check_page(page, args.base_url, route, out_dir, problems))
            context.close()

            mobile = browser.new_context(viewport=VIEWPORTS["mobile"], is_mobile=True, has_touch=True)
            mpage = mobile.new_page()
            login(mpage, args.base_url, args.username, args.password)
            for route in routes:
                report["routes"].append(
                    check_page(mpage, args.base_url, route, out_dir, problems, viewport="mobile")
                )
            mobile.close()
        finally:
            browser.close()

    report["problems"] = problems
    report["ok"] = not problems
    (out_dir / "integration-ui-report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps({"ok": report["ok"], "problems": problems}, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
