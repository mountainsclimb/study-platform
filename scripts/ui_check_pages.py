"""其余页面（学习概览 / 知识清单 / 内容管理 / 错题复习 / 拍题 / 模型设置）界面核对。

与 scripts/ui_check.py 配套：ui_check.py 覆盖题库练习与题目阅读页，本脚本覆盖
P1 方案里“其余页面”的桌面 1440×900、手机 390×844 与 320px 窄屏重排，并断言：
  1. 每页 320 / 390 / 1440 宽度都没有页面级横向溢出；
  2. 每页主操作在首屏可见且触控高度 ≥ 44px；
  3. 知识清单三入口、内容管理三分区、拍题三步、设置高级折叠等结构可交互；
  4. 页面运行期间没有 JS 异常。

用法（先启动隔离实例）：
  python scripts/ui_check_pages.py --base-url http://127.0.0.1:8801 \
      --username 界面验收 --password ui-pass-12345 --out screenshots/ui-pages
退出码：任一断言失败或出现页面 JS 异常时为 1。
"""
import argparse
import json
from pathlib import Path
import sys
import tempfile

from playwright.sync_api import sync_playwright

DEFAULT_OUT = Path(__file__).resolve().parents[1] / "screenshots" / "ui-pages"

VIEWPORTS = {
    "desktop": {"width": 1440, "height": 900},
    "mobile": {"width": 390, "height": 844},
}
NARROW = {"width": 320, "height": 800}


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

PAGES = [
    {
        "route": "home",
        "hash": "#/",
        "ready": ".pg-focus-grid",
        "primary": ".pg-focus--primary .pg-focus-actions .button",
        "expect": [".pg-focus", ".pg-inline-stats", ".pg-focus-grid"],
    },
    {
        "route": "notes",
        "hash": "#/notes",
        "ready": ".pg-tabs",
        "primary": ".page-heading .pg-actions .button",
        "expect": [".pg-tabs", ".pg-panel"],
    },
    {
        "route": "admin",
        "hash": "#/admin",
        "ready": ".pg-tabs",
        "primary": ".pg-tabs .pg-tab[aria-selected='true']",
        "expect": [".pg-tabs", ".pg-panel", "#admin-panel-issues"],
    },
    {
        "route": "wrongbook",
        "hash": "#/wrongbook",
        "ready": ".pg-filter-bar",
        "primary": ".page-heading button:has-text('开始连续复习')",
        "expect": [".pg-filter-bar", ".pg-card-grid, .empty, .error-panel"],
    },
    {
        "route": "upload",
        "hash": "#/upload",
        "ready": ".pg-step-rail",
        "primary": "button:has-text('上传题目')",
        "expect": [".pg-step-rail", ".pg-step-card", "#upload-step-3"],
    },
    {
        "route": "settings",
        "hash": "#/settings",
        "ready": ".pg-api-status",
        "primary": ".pg-api-actions .button",
        "expect": [".pg-api-status", ".pg-default-models", ".provider-form"],
    },
]


def login(page, base_url: str, username: str, password: str) -> None:
    """登录；隔离库里账号不存在时按本机模式自动注册一次性验收账号。"""
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


def box_of(page, selector: str) -> dict:
    node = page.query_selector(selector)
    if not node:
        return {}
    box = node.bounding_box() or {}
    return {key: round(value, 1) for key, value in box.items()}


def is_visible(page, selector: str) -> bool:
    node = page.query_selector(selector)
    return bool(node and node.is_visible())


def overflow(page) -> int:
    return int(page.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth"))


def add_problem(problems, page_name: str, viewport: str, message: str, detail=None) -> None:
    item = {"page": page_name, "viewport": viewport, "problem": message}
    if detail is not None:
        item["detail"] = detail
    problems.append(item)


def check_common(page, name: str, spec: dict, viewport: str, vp_height: int, problems) -> dict:
    found = {}
    for selector in spec["expect"]:
        found[selector] = is_visible(page, selector)
        if not found[selector]:
            add_problem(problems, name, viewport, f"缺少或不可见：{selector}")
    metrics = {
        "overflow": overflow(page),
        "primary": box_of(page, spec["primary"]),
        "expect": found,
        "url_hash": page.evaluate("location.hash"),
    }
    if metrics["overflow"] > 1:
        add_problem(problems, name, viewport, f"页面横向溢出 {metrics['overflow']}px", metrics)
    primary = metrics["primary"]
    if not primary:
        add_problem(problems, name, viewport, f"主操作不存在：{spec['primary']}")
    else:
        if primary.get("height", 0) < 43.5:
            add_problem(problems, name, viewport, f"主操作高度 {primary.get('height')}px < 44px", primary)
        if primary.get("y", 9999) >= vp_height:
            add_problem(problems, name, viewport, "主操作不在首屏", primary)
    return metrics


def run_notes_checks(page, viewport: str, problems) -> dict:
    tabs = page.locator(".pg-tabs .pg-tab").count()
    if tabs != 3:
        add_problem(problems, "notes", viewport, f"知识清单入口应为 3 个，实际 {tabs}")
    # 资料库
    page.get_by_role("tab", name="资料库").click()
    page.wait_for_timeout(200)
    docs_visible = is_visible(page, "#notes-panel-docs")
    upload_zone = is_visible(page, ".upload-zone")
    if not docs_visible or not upload_zone:
        add_problem(problems, "notes", viewport, "资料库入口未显示上传区")
    # AI 整理
    page.get_by_role("tab", name="AI 整理").click()
    page.wait_for_timeout(200)
    ai_visible = is_visible(page, "#notes-panel-ai")
    steps = page.locator("#notes-panel-ai .pg-step").count()
    advanced = page.locator("#notes-panel-ai .pg-advanced").count()
    estimate = (page.locator("#notes-panel-ai .pg-estimate").inner_text() or "").strip()
    generate_visible = is_visible(page, "#notes-panel-ai .pg-form-actions .button")
    if not (ai_visible and steps == 3 and advanced >= 1 and generate_visible):
        add_problem(problems, "notes", viewport,
                    "AI 整理未按三步布局或缺少高级折叠/生成按钮",
                    {"steps": steps, "advanced": advanced, "generate": generate_visible})
    if "使用模型" not in estimate:
        add_problem(problems, "notes", viewport, "确认生成区未显示模型估算", estimate)
    # 我的笔记
    page.get_by_role("tab", name="我的笔记").click()
    page.wait_for_timeout(200)
    notes_visible = is_visible(page, "#notes-panel-notes")
    if not notes_visible:
        add_problem(problems, "notes", viewport, "我的笔记入口未显示")
    return {
        "tabs": tabs,
        "docs": docs_visible,
        "ai_steps": steps,
        "ai_advanced": advanced,
        "estimate": estimate[:120],
        "notes": notes_visible,
    }


def run_admin_checks(page, viewport: str, problems) -> dict:
    tabs = page.locator(".pg-tabs .pg-tab").count()
    if tabs != 3:
        add_problem(problems, "admin", viewport, f"内容管理分区应为 3 个，实际 {tabs}")

    def wait_panel(panel_id: str, needles, timeout: int = 15000) -> bool:
        """分区懒加载：等当前分区数据加载完成（出现关键文案或明确错误）。"""
        if isinstance(needles, str):
            needles = [needles]
        try:
            page.wait_for_function(
                """([id, needles]) => {
                    const node = document.querySelector(id);
                    if (!node) return false;
                    const text = node.innerText || '';
                    return needles.some(needle => text.includes(needle));
                }""", arg=[panel_id, list(needles)], timeout=timeout)
            return True
        except Exception:  # noqa: BLE001 - 超时由上层记入问题列表
            return False

    def panel_buttons(labels):
        missing = []
        for label in labels:
            if page.locator(f"button:has-text('{label}')").count() == 0:
                missing.append(label)
        return missing

    # 初始只加载校勘分区；题库与知识点 / 运行与账号数据在点击 Tab 后才请求。
    issues_visible = is_visible(page, "#admin-panel-issues")
    issues_ready = wait_panel("#admin-panel-issues", ["内容报错审核与校勘", "正在加载", "加载失败"])
    lazy_initial_content = page.locator(
        "#admin-panel-content button:has-text('预估题量与费用')").count()
    missing = panel_buttons(["导出已发布修订"])

    page.get_by_role("tab", name="题库与知识点").click()
    content_visible = is_visible(page, "#admin-panel-content")
    content_ready = wait_panel("#admin-panel-content", ["知识点体系管理", "加载失败"])
    has_kp = is_visible(page, "#admin-panel-content .kp-tree, #admin-panel-content .card")
    missing += panel_buttons([
        "导入 CSV / JSON 目录", "添加节点",
        "预估题量与费用", "确认启动批量打标", "管理官方知识点清单",
    ])

    page.get_by_role("tab", name="运行与账号").click()
    ops_visible = is_visible(page, "#admin-panel-ops")
    ops_ready = wait_panel("#admin-panel-ops", ["服务就绪", "就绪检查读取失败", "加载失败"])
    readiness = (page.locator("#admin-panel-ops").inner_text() or "")
    has_readiness = "服务就绪" in readiness
    has_backup = is_visible(page, "#admin-panel-ops button:has-text('立即热备份并校验')")
    missing += panel_buttons(["添加域名", "立即热备份并校验", "开放注册", "关闭注册"])

    if not (issues_visible and content_visible and ops_visible):
        add_problem(problems, "admin", viewport, "三个分区面板未全部可切换")
    if not (issues_ready and content_ready and ops_ready):
        add_problem(problems, "admin", viewport, "分区懒加载数据未在等待时间内就绪",
                    {"issues": issues_ready, "content": content_ready, "ops": ops_ready})
    if lazy_initial_content != 0:
        add_problem(problems, "admin", viewport,
                    "未切换 Tab 时「题库与知识点」分区被提前渲染（懒加载失效）", lazy_initial_content)
    if missing:
        add_problem(problems, "admin", viewport, "内容管理原有功能按钮缺失", missing)
    if not has_readiness or not has_backup:
        add_problem(problems, "admin", viewport, "运行与账号缺少依赖健康或备份入口",
                    {"readiness": has_readiness, "backup": has_backup})
    page.get_by_role("tab", name="校勘").click()
    page.wait_for_timeout(200)
    queue_text = (page.locator("#admin-panel-issues").inner_text() or "")
    has_queue = ("内容报错审核与校勘" in queue_text) or ("正在加载" in queue_text)
    if not has_queue:
        add_problem(problems, "admin", viewport, "校勘分区缺少报错队列")
    review_details = page.locator("#admin-panel-issues .pg-review-item").count()
    return {
        "tabs": tabs,
        "issues": issues_visible,
        "content": content_visible,
        "ops": ops_visible,
        "readiness": has_readiness,
        "backup": has_backup,
        "missing_buttons": missing,
        "review_items": review_details,
        "kp_section": has_kp,
        "lazy_initial_content": lazy_initial_content,
        "panels_ready": {"issues": issues_ready, "content": content_ready, "ops": ops_ready},
    }


def run_upload_checks(page, viewport: str, base_url: str, problems) -> dict:
    links = page.locator(".pg-step-rail .pg-step-link").count()
    cards = page.locator(".pg-upload-stack .pg-step-card").count()
    if links != 3:
        add_problem(problems, "upload", viewport, f"拍题步骤导航应为 3 个，实际 {links}")
    if cards != 3:
        add_problem(problems, "upload", viewport, f"拍题主顺序应有 3 张步骤卡，实际 {cards}")
    steps = [page.locator(f"#upload-step-{index}").count() for index in (1, 2, 3)]
    if steps != [1, 1, 1]:
        add_problem(problems, "upload", viewport, "上传 / 核对题干 / 查看讲解卡片缺失", steps)
    if viewport == "desktop":
        # 只做一次真实上传冒烟：确认「上传 → 核对题干」可推进，不触发 AI 付费调用。
        sample = Path(tempfile.gettempdir()) / "zhixu_ui_check_question.txt"
        sample.write_text("已知函数 f(x)=x^2-2x，求 f(x) 的最小值。", encoding="utf-8")
        try:
            page.locator(".upload-zone input[type=file]").first.set_input_files(str(sample))
            page.get_by_role("button", name="上传题目").click()
            page.wait_for_selector("text=已上传", timeout=20000)
            page.wait_for_timeout(400)
            confirm_visible = is_visible(page, "#upload-step-2")
            if not confirm_visible:
                add_problem(problems, "upload", viewport, "上传后未显示核对题干步骤")
        except Exception as error:  # noqa: BLE001 - 冒烟失败要落进报告，不中断其余页面
            add_problem(problems, "upload", viewport, f"上传冒烟失败：{error}")
        finally:
            try:
                sample.unlink(missing_ok=True)
            except OSError:
                pass
    return {"step_links": links, "step_cards": cards, "steps": steps}


def run_wrongbook_checks(page, viewport: str, problems) -> dict:
    start_visible = is_visible(page, ".page-heading button:has-text('开始连续复习')")
    cards = page.locator(".pg-question-card").count()
    empty_state = is_visible(page, ".empty")
    filters = page.locator(".pg-filter-grid select").count()
    if not start_visible:
        add_problem(problems, "wrongbook", viewport, "连续复习主操作不可见")
    if filters < 2:
        add_problem(problems, "wrongbook", viewport, f"错题筛选控件不足：{filters}")
    if cards == 0 and not empty_state:
        add_problem(problems, "wrongbook", viewport, "错题列表既无卡片也无空状态")
    card_ok = True
    if cards:
        card = page.locator(".pg-question-card").first
        text = card.inner_text() or ""
        card_ok = ("下次复习" in text) and ("再做一次" in text)
        if not card_ok:
            add_problem(problems, "wrongbook", viewport, "错题卡缺少来源/状态或操作层级", text[:200])
    return {"start": start_visible, "cards": cards, "filters": filters, "card_ok": card_ok}


def run_settings_checks(page, viewport: str, problems) -> dict:
    api_visible = is_visible(page, ".pg-api-status")
    defaults = page.locator(".pg-default-model, .pg-task-row").count()
    task_editors = page.locator(".pg-task-row [data-field='task-primary']").count()
    add_visible = is_visible(page, ".provider-form button[type=submit]")
    fold = page.locator(".pg-fold-card details.pg-advanced").first
    fold_count = page.locator(".pg-fold-card details.pg-advanced").count()
    was_open = fold.get_attribute("open") is not None if fold_count else False
    if fold_count:
        fold.locator("summary").click()
        page.wait_for_timeout(150)
        now_open = fold.get_attribute("open") is not None
        if was_open or not now_open:
            add_problem(problems, "settings", viewport, "高级参数折叠未能展开")
        save_visible = is_visible(page, ".pg-fold-card button:has-text('保存偏好设置')")
        if not save_visible:
            add_problem(problems, "settings", viewport, "折叠内保存偏好设置按钮不可见")
    else:
        add_problem(problems, "settings", viewport, "缺少高级参数折叠区")
    if defaults < 1:
        add_problem(problems, "settings", viewport, "默认模型列表为空")
    if task_editors < 1:
        add_problem(problems, "settings", viewport, "默认模型不支持行内编辑（缺少 task-primary 下拉）")
    if not (api_visible and add_visible):
        add_problem(problems, "settings", viewport, "API 状态或添加连接入口不可见")
    return {"api_status": api_visible, "defaults": defaults, "task_editors": task_editors,
            "add_form": add_visible, "fold": fold_count}


def run(args) -> int:
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    report = {
        "base_url": args.base_url,
        "screenshots": [],
        "metrics": {},
        "problems": [],
        "page_errors": [],
    }
    with sync_playwright() as playwright:
        browser = launch_browser(playwright)
        contexts = {}
        contexts["desktop"] = browser.new_context(viewport=VIEWPORTS["desktop"], locale="zh-CN")
        contexts["mobile"] = browser.new_context(
            viewport=VIEWPORTS["mobile"], locale="zh-CN", is_mobile=True, has_touch=True,
            user_agent=("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"),
        )

        for viewport, context in contexts.items():
            page = context.new_page()
            page_errors = report["page_errors"]
            state = {"route": "login"}

            def on_pageerror(error, _state=state, _errors=page_errors):
                _errors.append({"page": _state["route"], "error": str(error)})

            page.on("pageerror", on_pageerror)
            login(page, args.base_url, args.username, args.password)
            report["metrics"][viewport] = {}
            for spec in PAGES:
                name = spec["route"]
                state["route"] = name
                page.goto(args.base_url + "/" + spec["hash"], wait_until="domcontentloaded")
                page.wait_for_selector(spec["ready"], timeout=20000)
                page.wait_for_timeout(700)
                metrics = check_common(page, name, spec, viewport, VIEWPORTS[viewport]["height"], report["problems"])
                if name == "notes":
                    metrics["interaction"] = run_notes_checks(page, viewport, report["problems"])
                    page.wait_for_timeout(200)
                elif name == "admin":
                    metrics["interaction"] = run_admin_checks(page, viewport, report["problems"])
                    page.wait_for_timeout(200)
                elif name == "upload":
                    metrics["interaction"] = run_upload_checks(page, viewport, args.base_url, report["problems"])
                    page.wait_for_timeout(200)
                elif name == "wrongbook":
                    metrics["interaction"] = run_wrongbook_checks(page, viewport, report["problems"])
                elif name == "settings":
                    metrics["interaction"] = run_settings_checks(page, viewport, report["problems"])
                report["metrics"][viewport][name] = metrics
                # 截图前回到页首并清掉操作提示条，保证截图反映页面本身的结构。
                page.evaluate("window.scrollTo(0, 0); const t = document.getElementById('toast-region'); if (t) t.replaceChildren();")
                page.wait_for_timeout(200)
                shot = out / f"{viewport}-{name}.png"
                page.screenshot(path=str(shot), full_page=False)
                report["screenshots"].append(str(shot))
            # 320px 窄屏重排：同账号同标签页，只核对页面级横向溢出。
            page.set_viewport_size(NARROW)
            report["metrics"]["narrow-320"] = {}
            for spec in PAGES:
                name = spec["route"]
                state["route"] = f"{name}@320"
                page.goto(args.base_url + "/" + spec["hash"], wait_until="domcontentloaded")
                page.wait_for_selector(spec["ready"], timeout=20000)
                page.wait_for_timeout(500)
                width = overflow(page)
                report["metrics"]["narrow-320"][name] = {"overflow": width}
                if width > 1:
                    add_problem(report["problems"], name, "320", f"320px 页面横向溢出 {width}px")
            page.close()
            context.close()

        browser.close()

    if report["page_errors"]:
        report["problems"].append({
            "page": "-", "viewport": "-", "problem": "页面运行期间出现 JS 异常",
            "detail": report["page_errors"],
        })
    report["ok"] = not report["problems"]
    (out / "summary-pages.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"ok": report["ok"], "problems": report["problems"], "out": str(out)},
                     ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="知序其余页面界面核对（学习概览/知识清单/内容管理/错题/拍题/设置）")
    parser.add_argument("--base-url", default="http://127.0.0.1:8765")
    parser.add_argument("--username", default="ui-visual-check")
    parser.add_argument("--password", default="ui-visual-pass-123")
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    args = parser.parse_args(argv)
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
