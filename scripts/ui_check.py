"""界面基线截图与几何核对（Playwright + 本机 Edge）。

用法（先启动一个用于验收的实例，指向已验收题库版本）：
  python scripts/ui_check.py --base-url http://127.0.0.1:8801 --out screenshots/ui-baseline

脚本会：
  1. 用一次性账号登录（注册接口；数据目录应为隔离库）；
  2. 记录桌面 1440×900 与手机 390×844 的题库页 / 题目页截图；
  3. 输出关键几何指标（筛选卡高、首条结果 y、阅读列宽、底部操作条是否可见等）到 JSON；
  4. 检查移动端「结果 → 题目 → 返回」的视图切换与焦点返回；
  5. 检查打印媒体下不出现筛选、结果列表和 AI 控件。

退出码：几何指标越界或交互失败时为 1。
"""
import argparse
import json
import os
from pathlib import Path
import re
import sys

from playwright.sync_api import sync_playwright

DEFAULT_OUT = Path(__file__).resolve().parents[1] / "screenshots" / "ui-baseline"


def launch_browser(playwright):
    """优先本机 Edge；未安装时回退 Playwright 自带 Chromium（验收环境自适应）。"""
    for channel in ("msedge", "chrome", None):
        try:
            if channel:
                return playwright.chromium.launch(channel=channel, headless=True)
            return playwright.chromium.launch(headless=True)
        except Exception:  # noqa: BLE001 - 逐个渠道探测，全部失败才抛错
            continue
    raise RuntimeError("未找到可用的浏览器（msedge / chrome / chromium 均不可用）")


def _login(page, base_url: str, username: str, password: str) -> None:
    """登录；隔离库里账号不存在时按本机模式自动注册一次性验收账号。"""
    page.goto(base_url + "/", wait_until="domcontentloaded")
    try:
        page.wait_for_selector(".practice-workspace, .pg-focus-grid, .page-heading", timeout=8000)
        return
    except Exception:
        pass
    page.wait_for_selector(".auth-form-card", timeout=15000)
    page.get_by_label("用户名").fill(username)
    page.get_by_label("密码").fill(password)
    page.get_by_role("button", name="登录学习工作台").click()
    try:
        page.wait_for_selector(".practice-workspace, .pg-focus-grid, .page-heading", timeout=15000)
        return
    except Exception:
        pass
    page.locator(".auth-switch .text-button").filter(has_text="创建账号").click()
    page.wait_for_selector(".auth-form-card", timeout=10000)
    page.get_by_label("用户名").fill(username)
    page.get_by_label("密码").fill(password)
    page.get_by_role("button", name="创建我的学习账号").click()
    page.wait_for_selector(".practice-workspace, .pg-focus-grid, .page-heading", timeout=25000)


def _ensure_practice(page, base_url: str) -> None:
    page.goto(base_url + "/#/practice", wait_until="domcontentloaded")
    page.wait_for_selector(".practice-workspace", timeout=20000)
    page.wait_for_selector(".result-card", timeout=20000)


def _box(page, selector: str) -> dict:
    node = page.query_selector(selector)
    if not node:
        return {}
    box = node.bounding_box() or {}
    return {key: round(value, 1) for key, value in box.items()}


def _report(errors: list[str], name: str, detail: dict) -> None:
    print(json.dumps({"check": name, **detail}, ensure_ascii=False))


def run(args) -> int:
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    report: dict = {"base_url": args.base_url, "screenshots": [], "metrics": {}, "problems": []}
    with sync_playwright() as playwright:
        browser = launch_browser(playwright)
        context = browser.new_context(viewport={"width": 1440, "height": 900}, locale="zh-CN")
        page_errors: list[str] = []
        page = context.new_page()
        page.on("pageerror", lambda error: page_errors.append(str(error)))
        _login(page, args.base_url, args.username, args.password)
        _ensure_practice(page, args.base_url)
        page.wait_for_timeout(700)

        # 桌面几何
        filter_box = _box(page, ".filter-card")
        first_result = _box(page, ".result-card")
        reading = _box(page, ".reading-pane")
        actions = _box(page, ".reading-actions-inner")
        desktop = {
            "filter_card_height": filter_box.get("height"),
            "first_result_y": (first_result.get("y") or 0) + (first_result.get("height") or 0) / 2,
            "reading_width": reading.get("width"),
            "action_bar_visible": bool(actions),
            "result_cards": page.locator(".result-card").count(),
        }
        report["metrics"]["desktop"] = desktop
        page.screenshot(path=str(out / "desktop-practice.png"), full_page=False)
        report["screenshots"].append(str(out / "desktop-practice.png"))
        _report(report["problems"], "desktop-practice", desktop)
        if (desktop["filter_card_height"] or 999) > 260:
            report["problems"].append("桌面筛选卡高 %.0f，目标 ≤260" % desktop["filter_card_height"])

        # 打开一条题组小题，检查阅读区结构
        page.locator(".result-card").first.click()
        page.wait_for_selector(".reading-title-row", timeout=15000)
        page.wait_for_timeout(500)
        question = {
            "tabs": page.locator(".reading-tab").count(),
            "choices": page.locator(".choice").count(),
            "action_primary": page.locator(".reading-actions-group .button").count(),
            "shared_material": page.locator(".shared-material").count(),
            "title": (page.locator(".reading-title-row h2").inner_text() or "").strip(),
            "path": (page.locator(".reading-path").inner_text() or "").strip(),
        }
        report["metrics"]["question"] = question
        page.screenshot(path=str(out / "desktop-question.png"), full_page=False)
        report["screenshots"].append(str(out / "desktop-question.png"))
        _report(report["problems"], "desktop-question", question)

        # 手机：结果 → 题目 → 返回
        mobile = browser.new_context(viewport={"width": 390, "height": 844}, locale="zh-CN",
                                     is_mobile=True, has_touch=True,
                                     user_agent=("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                                                 "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"))
        page_m = mobile.new_page()
        _login(page_m, args.base_url, args.username, args.password)
        page_m.goto(args.base_url + "/#/practice", wait_until="domcontentloaded")
        page_m.wait_for_selector(".practice-workspace", timeout=20000)
        page_m.wait_for_selector(".result-card", timeout=20000)
        page_m.wait_for_timeout(400)
        initial_view = page_m.get_attribute(".practice-workspace", "data-view")
        first_y = (_box(page_m, ".result-card").get("y") or 0)
        page_m.screenshot(path=str(out / "mobile-results.png"), full_page=False)
        report["screenshots"].append(str(out / "mobile-results.png"))
        page_m.locator(".result-card").first.click()
        page_m.wait_for_selector(".reading-title-row", timeout=15000)
        page_m.wait_for_timeout(500)
        question_view = page_m.get_attribute(".practice-workspace", "data-view")
        back_visible = page_m.locator(".reading-back").first.is_visible()
        action_visible = page_m.locator(".reading-actions-inner").first.is_visible()
        page_m.screenshot(path=str(out / "mobile-question.png"), full_page=False)
        report["screenshots"].append(str(out / "mobile-question.png"))
        page_m.locator(".reading-back").first.click()
        page_m.wait_for_timeout(400)
        back_view = page_m.get_attribute(".practice-workspace", "data-view")
        overflow = page_m.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth")
        mobile_metrics = {
            "initial_view": initial_view,
            "first_result_y": round(first_y, 1),
            "question_view": question_view,
            "back_visible": bool(back_visible),
            "action_bar_visible": bool(action_visible),
            "back_view": back_view,
            "horizontal_overflow": overflow,
        }
        report["metrics"]["mobile"] = mobile_metrics
        _report(report["problems"], "mobile", mobile_metrics)
        if initial_view != "results":
            report["problems"].append("手机默认应停在结果列表，实际为 %s" % initial_view)
        if question_view != "question":
            report["problems"].append("手机点选题目后未切到阅读视图")
        if back_view != "results":
            report["problems"].append("手机返回后未回到结果列表")
        if (first_y or 9999) > 420:
            report["problems"].append("手机首条结果 y=%.0f，目标 ≤420" % first_y)
        if (overflow or 0) > 1:
            report["problems"].append("手机页面存在横向溢出 %spx" % overflow)

        # 作答后展开/收起答案与解析：按文案定位按钮，兼容「无答案/无解析不渲染按钮」的新门禁。
        answer_flow = {
            "submitted": False,
            "has_answer_button": False,
            "has_solution_button": False,
            "answer_panel": False,
            "solution_panel": False,
            "collapsed": False,
            "errors": [],
        }
        actions_group = page.locator(".reading-actions-group")
        submit_button = actions_group.locator("button").filter(has_text=re.compile(r"提交")).last
        if submit_button.count() and page.locator(".choice input").count():
            page.locator(".choice").first.click()
            answer_flow["submitted"] = True
            submit_button.click()
            page.wait_for_timeout(1500)
            answer_flow["buttons_after_submit"] = actions_group.locator(".button").all_inner_texts()
            if actions_group.count() and actions_group.locator("button").count() == 0:
                report["problems"].append("提交后底部操作组为空（无按钮）")
            answer_button = actions_group.locator("button").filter(
                has_text=re.compile(r"查看答案|收起答案")).first
            solution_button = actions_group.locator("button").filter(
                has_text=re.compile(r"查看解析|收起解析")).first
            answer_flow["has_answer_button"] = answer_button.count() > 0
            answer_flow["has_solution_button"] = solution_button.count() > 0
            try:
                if answer_flow["has_answer_button"]:
                    answer_button.click(timeout=10000)
                    page.wait_for_timeout(700)
                    answer_flow["answer_panel"] = page.locator(".answer-box:not(.solution-box)").count() > 0
                    answer_button.click(timeout=10000)
                    page.wait_for_timeout(400)
                    answer_flow["collapsed"] = page.locator(".answer-box[data-panel='answer']").count() == 0
                if answer_flow["has_solution_button"]:
                    solution_button.click(timeout=10000)
                    page.wait_for_timeout(700)
                    answer_flow["solution_panel"] = page.locator(".answer-box.solution-box").count() > 0
            except Exception as exc:  # 记录状态而不是让整轮验收中断
                answer_flow["click_error"] = str(exc)[:200]
                page.screenshot(path=str(out / "debug-reveal-failure.png"), full_page=False)
        answer_flow["errors"] = list(page_errors)
        report["metrics"]["answer_flow"] = answer_flow
        _report(report["problems"], "answer-flow", answer_flow)
        if answer_flow["submitted"] and not (
                answer_flow["has_answer_button"] or answer_flow["has_solution_button"]
                or answer_flow["buttons_after_submit"]):
            report["problems"].append("提交后没有任何操作按钮")
        if page_errors:
            report["problems"].append("页面出现 JS 异常：%s" % page_errors[:3])

        # 打印：只保留阅读内容
        page.emulate_media(media="print")
        page.wait_for_timeout(200)
        print_checks = {
            "filter_visible": page.locator(".filter-card").first.is_visible(),
            "results_visible": page.locator(".results-pane").first.is_visible(),
            "ai_panel_visible": page.locator(".ai-panel").first.is_visible(),
            "reading_visible": page.locator(".reading-pane").first.is_visible(),
        }
        report["metrics"]["print"] = print_checks
        page.screenshot(path=str(out / "print-question.png"), full_page=True)
        report["screenshots"].append(str(out / "print-question.png"))
        _report(report["problems"], "print", print_checks)
        if print_checks["filter_visible"] or print_checks["results_visible"]:
            report["problems"].append("打印视图仍显示筛选或结果列表")
        if print_checks["ai_panel_visible"]:
            report["problems"].append("打印视图仍显示 AI 控件")

        mobile.close()
        context.close()
        browser.close()

    report["ok"] = not report["problems"]
    (out / "summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"ok": report["ok"], "problems": report["problems"], "out": str(out)},
                     ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="知序界面基线截图与几何核对")
    parser.add_argument("--base-url", default="http://127.0.0.1:8765")
    parser.add_argument("--username", default="ui-visual-check")
    parser.add_argument("--password", default="ui-visual-pass-123")
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    args = parser.parse_args(argv)
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
