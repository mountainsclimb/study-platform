"""整合模块全链路浏览器验收（首页 / 我的题库 / 试卷工坊 / 学情分析 / 任务中心 / AI 助手）。

前置：隔离实例已启动（`STUDY_DATA_DIR` 指向临时目录、建议 `STUDY_BANK_DIR` 固定 r3），
账号可先不存在；脚本默认 `--seed` 调用既有 `scripts/seed_demo.py --register --json`
写入示例数据（不配置 AI Key、不访问外网），也可 `--skip-seed` 复用已有示例数据。

覆盖：
1. 首页 AI 工作台 6 张卡（计数非负、点击跳转、最近动态/空态）；
2. 我的题库（统计、筛选、编辑弹层、导入 dry_run、导出 JSON/CSV 下载非空）；
3. 试卷工坊（4 个 tab、导入历史/详情、手工组卷试题篮加入/移除、试卷详情、
   Word 下载 PK 头、打印 iframe 200、答题卡生成/预览 SVG、整卷练习作答→提交→结果）；
4. 学情分析（有数据渲染、无数据空态、打印媒体只保留报告、无 provider 时 AI 建议禁用）；
5. 任务中心（列表 / 状态过滤 / 详情 / 取消或续跑入口）；
6. AI 助手（会话列表、新建会话、技能选择器勾选、无 provider 发送给引导且不真实调用模型）。

产出：`screenshots/integration/modules-accept-report.json` + `modules-*.png` 截图；
任一 pageerror / 非预期 console error / 横向溢出 → 失败；退出码 0/1。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUT = ROOT / "screenshots" / "integration"
SEED_SCRIPT = ROOT / "scripts" / "seed_demo.py"
DEFAULT_USERNAME = "模块验收"
DEFAULT_PASSWORD = "ui-pass-12345"
EMPTY_USERNAME = "模块空态验收"
EMPTY_PASSWORD = "ui-pass-12345"

# 任务/页面要求的首页 6 张卡
HOME_CARD_KEYS = ["classroom", "paperlab", "mybank", "revision", "assistant", "tasks"]

# 负向用例会故意触发 4xx（无 provider 发送、导入 dry_run 校验），仅在该步骤忽略资源错误
EXPECTED_CONSOLE_STEPS = {
    "assistant-无provider发送", "题目-导入dry_run校验",
}
RESOURCE_ERROR_RE = re.compile(r"Failed to load resource.*status of (\d{3})")
RESOURCE_ERROR_OK = {"400", "422"}

# 打开「每日一签」自动弹层的抑制脚本：验收时预置当天已读，避免模态框拦截点击。
_SUPPRESS_QUOTE_JS = """
try {
  const d = new Date();
  const pad = (n) => String(n).padStart(2, "0");
  localStorage.setItem("zhixu.quote.lastShown", `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`);
} catch (e) { /* ignore */ }
"""


# AI 助手无 provider 时给中文引导的常见文案片段
PROVIDER_HINT_RE = re.compile(r"模型|provider|设置|配置")

report: dict = {
    "ok": False,
    "base_url": "",
    "generated_at": "",
    "seed": {},
    "steps": [],
    "screenshots": [],
    "problems": [],
    "warnings": [],
    "data": {},
}

_errors: list[str] = []
_current_step = "初始化"
_step_errors_mark = 0


def mark_step(name: str) -> None:
    global _current_step, _step_errors_mark
    _current_step = name
    _step_errors_mark = len(_errors)


def step(name: str, ok: bool = True, **data) -> None:
    entry = {"step": name, "ok": bool(ok), **data}
    report["steps"].append(entry)
    if not ok:
        report["problems"].append(f"{name}: {data.get('detail') or '断言失败'}")


def check(name: str, condition, detail: str = "") -> bool:
    ok = bool(condition)
    step(name, ok, detail="" if ok else str(detail))
    return ok


def warning(message: str) -> None:
    report["warnings"].append(message)


def shot(page, name: str) -> str:
    DEFAULT_OUT.mkdir(parents=True, exist_ok=True)
    path = DEFAULT_OUT / f"modules-{name}.png"
    page.screenshot(path=str(path), full_page=False)
    report["screenshots"].append(str(path))
    return str(path)


def overflow(page) -> int:
    try:
        return int(page.evaluate(
            "() => Math.max(0, (document.documentElement.scrollWidth || 0)"
            " - (window.innerWidth || 0))"))
    except Exception:  # noqa: BLE001
        return 0


def assert_no_overflow(page, label: str) -> None:
    extra = overflow(page)
    if extra > 1:
        report["problems"].append(f"{label}: 页面横向溢出 {extra}px")


def attach_error_capture(page) -> None:
    page.on("pageerror", lambda exc: _errors.append(f"pageerror: {exc}"))
    page.on("console", lambda msg: _errors.append(f"console: {msg.text}")
            if msg.type == "error" else None)


def assert_clean_errors(page) -> None:
    global _step_errors_mark
    fresh = _errors[_step_errors_mark:]
    _step_errors_mark = len(_errors)
    page_errors = [item for item in fresh if item.startswith("pageerror")]
    console_errors = [item for item in fresh if not item.startswith("pageerror")]
    for item in page_errors:
        report["problems"].append(f"{_current_step}: {item}")
    allow_resource = _current_step in EXPECTED_CONSOLE_STEPS
    for item in console_errors:
        match = RESOURCE_ERROR_RE.search(item)
        if allow_resource and match and match.group(1) in RESOURCE_ERROR_OK:
            warning(f"{_current_step}: 预期内资源错误（{match.group(1)}）")
            continue
        report["problems"].append(f"{_current_step}: {item}")


def launch_browser(playwright):
    for channel in ("msedge", "chrome", None):
        try:
            if channel:
                return playwright.chromium.launch(channel=channel, headless=True)
            return playwright.chromium.launch(headless=True)
        except Exception:  # noqa: BLE001
            continue
    raise RuntimeError("未找到可用浏览器（msedge / chrome / chromium 均不可用）")


def new_context(browser, *, mobile: bool = False):
    viewport = {"width": 390, "height": 844} if mobile else {"width": 1440, "height": 900}
    context = browser.new_context(viewport=viewport, is_mobile=mobile, has_touch=mobile)
    context.add_init_script(_SUPPRESS_QUOTE_JS)
    page = context.new_page()
    attach_error_capture(page)
    page.on("dialog", lambda dialog: dialog.accept())
    return context, page


def login(page, base_url: str, username: str, password: str) -> bool:
    goto_ready(page, base_url + "/")
    try:
        page.wait_for_selector(".auth-form-card", timeout=6000)
    except Exception:  # noqa: BLE001 - 已登录会话直接进工作台
        try:
            page.wait_for_selector(".layout, .page-heading", timeout=6000)
            return True
        except Exception:  # noqa: BLE001
            return False
    page.get_by_label("用户名").fill(username)
    page.get_by_label("密码").fill(password)
    page.get_by_role("button", name="登录学习工作台").click()
    try:
        page.wait_for_selector(".layout, .page-heading", timeout=20000)
    except Exception:  # noqa: BLE001
        return False
    return True


def api_json(page, path: str, method: str = "GET", data=None):
    response = page.request.fetch(
        BASE + path, method=method, data=data,
        headers={"Content-Type": "application/json", "X-Requested-With": "XMLHttpRequest"})
    if not response.ok:
        return None
    try:
        return response.json()
    except Exception:  # noqa: BLE001
        return None



def dismiss_overlays(page) -> None:
    """关闭「每日一签」等自动弹层（HTML <dialog>），避免拦截后续自动化点击。"""
    try:
        for _ in range(3):
            dialog = page.locator("dialog[open]").first
            if dialog.count() == 0:
                return
            if not dialog.is_visible():
                return
            closed = False
            for selector in (".dq-close", ".dq-x"):
                button = dialog.locator(selector).first
                if button.count() and button.is_visible():
                    button.click(timeout=2000)
                    closed = True
                    break
            if not closed:
                page.keyboard.press("Escape")
            page.wait_for_timeout(150)
    except Exception:
        return


def goto_ready(page, url: str) -> None:
    """导航并等待页面稳定，同时关闭可能自动弹出的模态框。"""
    page.goto(url, wait_until="domcontentloaded")
    page.wait_for_timeout(250)
    dismiss_overlays(page)


def wait_quiet(page, ms: int = 500) -> None:
    page.wait_for_timeout(ms)


# ---------------------------------------------------------------------------
# 示例数据
# ---------------------------------------------------------------------------

def run_seed(base_url: str, username: str, password: str) -> dict:
    if not SEED_SCRIPT.is_file():
        report["problems"].append(f"缺少示例数据脚本：{SEED_SCRIPT}")
        return {"status": "missing"}
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    command = [
        sys.executable, str(SEED_SCRIPT),
        "--base-url", base_url, "--username", username,
        "--password", password, "--register", "--json",
    ]
    try:
        result = subprocess.run(
            command, cwd=str(ROOT), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=900, env=env)
    except subprocess.TimeoutExpired:
        report["problems"].append("seed_demo 运行超时（900s）")
        return {"status": "timeout"}
    stdout = result.stdout or ""
    match = re.search(r"\{.*\}\s*$", stdout, re.S)
    summary = {}
    if match:
        try:
            summary = json.loads(match.group(0))
        except json.JSONDecodeError:
            summary = {}
    if result.returncode != 0 or summary.get("ok") is False:
        tail = (result.stderr or stdout)[-800:]
        report["problems"].append(f"seed_demo 失败（exit {result.returncode}）：{tail}")
        return {"status": "failed", "summary": summary, "stderr": tail}
    state = {"status": "ok"}
    modules = summary.get("modules") or {}
    state["modules"] = {label: (info or {}).get("status") for label, info in modules.items()}
    state["urls"] = summary.get("urls") or {}
    state["health"] = summary.get("health") or {}
    # seed_demo 的模块状态是自由文本（如 created），不据此判失败；以进程退出码与 summary.ok 为准。
    report["warnings"].extend(summary.get("warnings") or [])
    return state


def inspect_data(page) -> dict:
    papers = api_json(page, "/api/paperlab/papers?page_size=20") or {}
    questions = api_json(page, "/api/paperlab/questions?page_size=5") or {}
    imports = api_json(page, "/api/paperlab/imports?page_size=5") or {}
    conversations = api_json(page, "/api/assistant/conversations") or {}
    return {
        "papers": len(papers.get("items") or []),
        "questions": len(questions.get("items") or []),
        "imports": len(imports.get("items") or []),
        "conversations": len(conversations.get("items") or conversations.get("conversations") or []),
    }


# ---------------------------------------------------------------------------
# 1. 首页
# ---------------------------------------------------------------------------

def flow_home(page) -> None:
    mark_step("首页")
    goto_ready(page, BASE + "/#/home")
    page.wait_for_selector(".home-ai-card", timeout=25000)
    wait_quiet(page, 800)
    cards = page.locator(".home-ai-card")
    count = cards.count()
    check("首页-6张AI工作台卡", count == 6, f"实际 {count} 张")
    keys = [cards.nth(index).get_attribute("data-card") for index in range(count)]
    check("首页-卡片覆盖六模块", set(HOME_CARD_KEYS).issubset(set(keys)), f"keys={keys}")
    counts = [cards.nth(index).locator(".home-ai-count").first.inner_text().strip()
              for index in range(count)]
    check("首页-计数非负", all(text and not text.startswith("-") for text in counts),
          f"counts={counts}")
    activity_items = page.locator(".home-activity-item").count()
    has_empty = page.locator(".home-activity-empty").count() > 0
    check("首页-最近动态或有空态", activity_items > 0 or has_empty,
          f"items={activity_items} empty={has_empty}")
    shot(page, "home")
    assert_no_overflow(page, "首页")

    page.locator('.home-ai-card[data-card="paperlab"]').first.click()
    page.wait_for_timeout(1200)
    check("首页-点击卡片跳转", "/paperlab" in page.url, f"url={page.url}")
    goto_ready(page, BASE + "/#/home")
    page.wait_for_selector(".home-ai-card", timeout=15000)
    assert_clean_errors(page)


# ---------------------------------------------------------------------------
# 2. 我的题库
# ---------------------------------------------------------------------------

def flow_mybank(page) -> None:
    mark_step("我的题库")
    goto_ready(page, BASE + "/#/mybank")
    page.wait_for_selector(".mb-stats-grid", timeout=25000)
    wait_quiet(page, 700)
    stat_values = page.locator(".mb-stat-value").all_inner_texts()
    check("题库-统计区渲染", len(stat_values) >= 5, f"stats={stat_values[:6]}")
    shot(page, "mybank")

    filters = page.locator(".mb-filter-card select")
    if filters.count():
        subject_select = filters.first
        try:
            subject_select.select_option(label="数学")
        except Exception:  # noqa: BLE001 - 示例数据学科可能不同
            options = subject_select.locator("option").all_inner_texts()
            if len(options) > 1:
                subject_select.select_option(index=1)
        page.get_by_role("button", name="搜索", exact=True).click()
        wait_quiet(page, 900)
        check("题库-筛选可用", page.locator(".mb-list, .mb-empty-hint, .empty").count() > 0,
              "筛选后列表/空态存在")

    edit_buttons = page.get_by_role("button", name="编辑题目", exact=True)
    if edit_buttons.count():
        edit_buttons.first.click()
        page.wait_for_selector(".mb-editor-dialog", state="visible", timeout=10000)
        dialog_text = page.locator(".mb-editor-dialog").inner_text()
        shot(page, "mybank-editor")
        check("题库-编辑弹层", "取消" in dialog_text and "保存" in dialog_text, dialog_text[:80])
        page.locator(".mb-editor-dialog").get_by_role("button", name="取消", exact=True).first.click()
        page.wait_for_selector(".mb-editor-dialog", state="detached", timeout=10000)
    else:
        warning("题库没有可编辑题目，已跳过编辑弹层检查")

    # 导入 dry_run
    mark_step("题目-导入dry_run校验")
    page.get_by_role("button", name="导入题库", exact=True).first.click()
    page.wait_for_selector(".mb-import-dialog", timeout=10000)
    import_payload = json.dumps([{
        "type": "SINGLE", "subject": "数学", "grade": "高中一年级", "difficulty": "简单",
        "knowledge_tags": "函数", "stem": "示例验收导入题：已知 $f(x)=x+1$，则 $f(1)$ 为（  ）",
        "options": "A. 1\nB. 2", "answers": "B", "explanation": "代入得 2。",
    }], ensure_ascii=False).encode("utf-8")
    page.locator(".mb-import-file").set_input_files({
        "name": "acceptance-import.json", "mimeType": "application/json", "buffer": import_payload})
    page.get_by_role("button", name="预览校验（dry_run）").click()
    try:
        page.wait_for_selector(".mb-import-result:not(:empty)", timeout=15000)
    except Exception:  # noqa: BLE001
        pass
    wait_quiet(page, 400)
    result_text = page.locator(".mb-import-result").inner_text().strip()
    shot(page, "mybank-import")
    check("题库-导入dry_run", bool(result_text), result_text[:120] or "dry_run 无结果")
    page.locator(".mb-import-dialog").get_by_role("button", name="关闭", exact=True).first.click()
    page.wait_for_selector(".mb-import-dialog", state="detached", timeout=10000)

    # 导出下载（JSON / CSV 均校验字节数 > 0）
    for label, name in (("导出 JSON", "json"), ("导出 CSV", "csv")):
        with page.expect_download(timeout=20000) as download_info:
            page.get_by_role("button", name=label, exact=True).first.click()
        download = download_info.value
        path = download.path()
        size = Path(path).stat().st_size if path else 0
        if name == "json":
            try:
                parsed = json.loads(Path(path).read_text(encoding="utf-8"))
                rows = parsed if isinstance(parsed, list) else (
                    parsed.get("items") if isinstance(parsed, dict) else None)
                check("题库-导出JSON下载", size > 0 and isinstance(rows, list),
                      f"{download.suggested_filename} size={size}")
            except Exception as exc:  # noqa: BLE001
                check("题库-导出JSON下载", False, f"解析失败：{exc}")
        else:
            check("题库-导出CSV下载", size > 0,
                  f"{download.suggested_filename} size={size}")
    assert_clean_errors(page)


# ---------------------------------------------------------------------------
# 3. 试卷工坊
# ---------------------------------------------------------------------------

def pick_seed_paper(page) -> dict:
    data = api_json(page, "/api/paperlab/papers?page_size=50") or {}
    items = data.get("items") or []
    if not items:
        return {}
    items.sort(key=lambda item: 0 if "示例" in str(item.get("title") or "") else 1)
    return items[0]


def flow_paperlab(page) -> None:
    mark_step("试卷工坊")
    papers = api_json(page, "/api/paperlab/papers?page_size=50") or {}
    imports = api_json(page, "/api/paperlab/imports?page_size=10") or {}
    paper = pick_seed_paper(page)
    import_rows = imports.get("items") or []
    import_id = (import_rows[0] or {}).get("id") if import_rows else None
    paper_id = (paper or {}).get("id")
    report["data"].update({
        "paper_id": paper_id, "paper_title": (paper or {}).get("title"),
        "import_id": import_id, "imports": len(import_rows),
    })
    check("试卷-存在示例试卷", bool(paper_id),
          f"papers={len(papers.get('items') or [])}")
    if not import_id:
        warning("示例数据没有识别导入历史记录，已跳过导入历史/详情检查")

    goto_ready(page, BASE + "/#/paperlab")
    page.wait_for_selector(".pl-main-tabs", timeout=20000)
    wait_quiet(page, 600)
    tab_texts = [text.strip() for text in page.locator(".pl-main-tabs .tab").all_inner_texts()]
    check("试卷-四个分区tab", tab_texts == ["识别导入", "组卷", "我的试卷", "错题巩固卷"],
          f"tabs={tab_texts}")
    shot(page, "paperlab-imports")

    # 导入历史 / 详情
    if import_id:
        goto_ready(page, BASE + "/#/paperlab?tab=history")
        page.wait_for_selector(".pl-page-body", timeout=20000)
        wait_quiet(page, 800)
        history_text = page.locator(".pl-page-body").inner_text()
        check("试卷-导入历史列表", "导入记录" in history_text or "条" in history_text,
              history_text[:120])
        goto_ready(page, BASE + f"/#/paperlab?import={import_id}")
        page.wait_for_selector(".pl-page-body", timeout=20000)
        wait_quiet(page, 900)
        detail_text = page.locator(".pl-page-body").inner_text()
        check("试卷-导入详情", len(detail_text) > 40 and "加载" not in detail_text[:20],
              detail_text[:120])

    # 组卷：试题篮加入 / 移除
    goto_ready(page, BASE + "/#/paperlab?tab=compose")
    page.wait_for_selector(".pl-compose", timeout=25000)
    wait_quiet(page, 900)
    manual_tab = page.locator(".pl-compose-tabs .tab", has_text="手工组卷").first
    manual_tab.click()
    page.wait_for_selector(".pl-picker-card", timeout=25000)
    wait_quiet(page, 400)
    add_buttons = page.get_by_role("button", name="加入试题篮", exact=True)
    if not add_buttons.count():
        # 已加入过的按钮文案是「已在试题篮」，先清空或直接使用已有篮子
        clear_buttons = page.get_by_role("button", name="清空", exact=True)
        if clear_buttons.count():
            clear_buttons.first.click()
            wait_quiet(page, 400)
            add_buttons = page.get_by_role("button", name="加入试题篮", exact=True)
    check("试卷-组卷选题列表", page.locator(".pl-picker-card").count() > 0,
          "选题列表为空")
    if add_buttons.count():
        add_buttons.first.click()
        page.wait_for_timeout(700)
    basket_before = page.locator(".pl-basket-item").count()
    shot(page, "paperlab-compose")
    check("试卷-试题篮加入", basket_before >= 1, f"basket={basket_before}")
    remove_buttons = page.get_by_role("button", name="移除", exact=True)
    if basket_before and remove_buttons.count():
        remove_buttons.first.click()
        page.wait_for_timeout(500)
    check("试卷-试题篮移除", page.locator(".pl-basket-item").count() < max(1, basket_before),
          f"basket={page.locator('.pl-basket-item').count()}")

    if not paper_id:
        return

    # 试卷列表 + 详情
    goto_ready(page, BASE + "/#/paperlab?tab=papers")
    page.wait_for_selector(".pl-paper-card", timeout=20000)
    check("试卷-我的试卷列表", page.locator(".pl-paper-card").count() > 0,
          "未渲染试卷卡片")
    goto_ready(page, BASE + f"/#/paperlab?tab=papers&paper={paper_id}")
    page.wait_for_selector(".pl-paper-detail-head", timeout=25000)
    wait_quiet(page, 900)
    check("试卷-详情三区块",
          page.locator(".pl-paper-cards").count() > 0
          and page.locator(".pl-paper-practice").count() > 0
          and page.locator(".pl-paper-items").count() > 0,
          "答题卡/整卷练习/题目列表区块缺失")
    page.evaluate("() => window.scrollTo(0, 0)")
    wait_quiet(page, 300)
    shot(page, "paperlab-detail")

    # Word 下载：docx 是 zip，文件头 PK
    with page.expect_download(timeout=30000) as word_info:
        page.get_by_role("button", name="下载 Word（含答案与解析）", exact=True).first.click()
    word = word_info.value
    word_path = Path(word.path())
    head = word_path.read_bytes()[:4]
    size = word_path.stat().st_size
    check("试卷-Word下载PK头", head[:2] == b"PK" and size > 0,
          f"{word.suggested_filename} head={head!r} size={size}")

    # 打印 iframe：同源自包含 HTML 加载 200
    page.get_by_role("button", name="打印 / 另存 PDF（含答案）", exact=True).first.click()
    page.wait_for_selector("iframe.pl-print-frame", state="attached", timeout=20000)
    print_src = page.locator("iframe.pl-print-frame").first.get_attribute("src")
    print_response = page.request.get(BASE + print_src)
    print_body = print_response.text() if print_response.ok else ""
    check("试卷-打印iframe加载", print_response.ok and len(print_body) > 1000,
          f"status={print_response.status} len={len(print_body)} src={print_src}")
    page.evaluate("() => document.querySelectorAll('iframe.pl-print-frame').forEach(n => n.remove())")

    # 答题卡：列表 → 预览 SVG（seed_demo 已生成示例答题卡）
    cards_data = api_json(page, f"/api/paperlab/papers/{paper_id}/cards") or {}
    card_rows = page.locator(".pl-card-row")
    if cards_data.get("items") and card_rows.count():
        card_rows.first.get_by_role("button", name="预览", exact=True).click()
        page.wait_for_selector(".pl-card-studio", timeout=20000)
        page.wait_for_selector(".pl-card-preview-img", timeout=20000)
        page.wait_for_function(
            "() => { const img = document.querySelector('.pl-card-preview-img');"
            " return img && img.complete && img.naturalWidth > 0; }", timeout=20000)
        check("试卷-答题卡SVG预览", True, "SVG 已加载渲染")
        shot(page, "paperlab-card")
        page.get_by_role("button", name="返回试卷详情", exact=True).first.click()
        page.wait_for_selector(".pl-paper-detail-head", timeout=20000)
        wait_quiet(page, 600)
    else:
        check("试卷-答题卡SVG预览", False, "示例答题卡缺失（seed_demo 应生成）")

    # 整卷练习：开始 → 作答 → 提交 → 结果
    page.get_by_role("button", name="开始整卷练习", exact=True).first.click()
    page.wait_for_selector(".pl-practice-start", timeout=20000)
    page.locator(".pl-practice-start").get_by_role(
        "button", name=re.compile("重新开始一段练习|开始作答")).first.click()
    page.wait_for_selector(".pl-practice-question", timeout=20000)
    wait_quiet(page, 500)
    option = page.locator(".pl-practice-answer .pl-practice-options .option").first
    if option.count():
        option.click()
    page.get_by_role("button", name="提交并判分", exact=True).first.click()
    page.wait_for_selector(".pl-practice-result", timeout=30000)
    wait_quiet(page, 800)
    score_text = page.locator(".pl-practice-score").inner_text()
    result_text = page.locator(".pl-practice-result").inner_text()
    check("试卷-整卷练习提交结果", bool(score_text.strip()) and "逐题回顾" in result_text,
          score_text[:60])
    shot(page, "paperlab-practice-result")
    page.get_by_role("button", name="返回试卷详情", exact=True).first.click()
    page.wait_for_selector(".pl-paper-detail-head", timeout=20000)
    assert_clean_errors(page)


# ---------------------------------------------------------------------------
# 4. 学情分析
# ---------------------------------------------------------------------------

def wait_insight_ready(page, label: str) -> bool:
    """等待学情正文；若渲染出错（并行开发时静态资源短暂不一致）点「重新加载」重试一次。"""
    try:
        page.wait_for_selector(".ins-summary, .ins-empty-block", timeout=18000)
        return True
    except Exception:  # noqa: BLE001
        pass
    if page.locator(".ins-error").count():
        error_text = page.locator(".ins-error").inner_text().strip()
        warning(f"{label}: 学情首轮渲染失败（{error_text[:60]}），已点「重新加载」重试")
        try:
            page.locator(".ins-error").get_by_role("button", name="重新加载").first.click()
        except Exception:  # noqa: BLE001
            return False
        wait_quiet(page, 1500)
    try:
        page.wait_for_selector(".ins-summary, .ins-empty-block", timeout=18000)
        return True
    except Exception:  # noqa: BLE001
        return False


def flow_insight(page) -> None:
    mark_step("学情分析")
    goto_ready(page, BASE + "/#/insight")
    page.wait_for_selector(".ins-page", timeout=25000)
    wait_insight_ready(page, "学情分析")
    wait_quiet(page, 600)
    checks = {
        "摘要": page.locator(".ins-summary").count() > 0,
        "统计卡": page.locator(".ins-stat").count() >= 3,
        "图表": page.locator("svg.ins-chart").count() >= 1,
        "知识点": page.locator(".ins-kp-groups, .ins-kp-empty").count() > 0,
        "建议/空态": page.locator(".ins-recommendations, .ins-kp-empty, .ins-rec").count() > 0,
    }
    check("学情-有数据渲染", all(checks.values()), f"{checks}")
    advice_btn = page.get_by_role("button", name=re.compile("生成学习建议"))
    disabled = advice_btn.count() > 0 and advice_btn.first.is_disabled()
    check("学情-无provider时AI建议禁用", disabled or "前往模型设置" in page.locator(".ins-page").inner_text(),
          "生成建议按钮未禁用且无模型设置引导")
    shot(page, "insight")
    assert_no_overflow(page, "学情分析")

    # 打印媒体：只保留报告（工具栏/按钮隐藏）
    page.emulate_media(media="print")
    wait_quiet(page, 400)
    toolbar_display = page.locator(".ins-toolbar").first.evaluate(
        "node => getComputedStyle(node).display").strip()
    page_display = page.locator(".ins-page").first.evaluate(
        "node => getComputedStyle(node).display").strip()
    print_shot = shot(page, "insight-print")
    page.emulate_media(media="screen")
    check("学情-打印媒体只保留报告", toolbar_display == "none" and page_display != "none",
          f"toolbar={toolbar_display} page={page_display} shot={print_shot}")
    assert_clean_errors(page)


def flow_insight_empty(playwright) -> None:
    mark_step("学情-空态")
    marker = len(_errors)
    browser = launch_browser(playwright)
    try:
        context = browser.new_context(viewport={"width": 1440, "height": 900})
        context.add_init_script(_SUPPRESS_QUOTE_JS)
        page = context.new_page()
        attach_error_capture(page)
        if not login(page, BASE, EMPTY_USERNAME, EMPTY_PASSWORD):
            check("学情-空态账号可用", False, "空态账号登录失败（请确认已注册）")
            context.close()
            return
        goto_ready(page, BASE + "/#/insight")
        page.wait_for_selector(".ins-page", timeout=25000)
        wait_insight_ready(page, "学情-空态")
        wait_quiet(page, 600)
        empty_text = page.locator(".ins-page").inner_text()
        has_empty = page.locator(".ins-empty-block").count() > 0 or "暂无" in empty_text
        check("学情-空态渲染", has_empty, empty_text[:120])
        shot(page, "insight-empty")
        context.close()
    finally:
        browser.close()
    for item in _errors[marker:]:
        if item.startswith("pageerror"):
            report["problems"].append(f"学情-空态: {item}")


# ---------------------------------------------------------------------------
# 5. 任务中心
# ---------------------------------------------------------------------------

def flow_tasks(page) -> None:
    mark_step("任务中心")
    goto_ready(page, BASE + "/#/tasks")
    page.wait_for_selector(".task-page", timeout=25000)
    wait_quiet(page, 1200)
    filters = page.locator(".task-filter")
    check("任务-状态过滤器", filters.count() >= 4, f"filters={filters.count()}")
    cards = page.locator(".task-card")
    page_text = page.locator(".task-page").inner_text()
    has_empty = "还没有 AI 任务" in page_text or page.locator(".task-empty").count() > 0
    check("任务-列表或空态渲染", cards.count() >= 1 or has_empty,
          f"cards={cards.count()} empty={has_empty} text={page_text[:80]}")
    if cards.count():
        # 取消 / 续跑 / 打开结果 / 详情：至少存在详情，且动作按钮可识别
        labels = " ".join(page.locator(".task-actions").all_inner_texts())
        has_lifecycle = any(word in labels for word in ("取消任务", "续跑任务", "重新执行", "打开结果"))
        check("任务-操作入口", has_lifecycle and "详情" in labels, f"actions={labels[:120]}")
        cards.first.get_by_role("button", name="详情", exact=True).click()
        page.wait_for_selector(".task-detail", timeout=15000)
        wait_quiet(page, 500)
        detail_text = page.locator(".task-detail").inner_text()
        check("任务-详情面板", "任务详情" in detail_text, detail_text[:120])
    else:
        # 无 AI Key 的隔离实例不会产生 AI 任务：空态覆盖 + 引导入口检查
        guidance = page.locator(".task-empty-actions").inner_text() if page.locator(
            ".task-empty-actions").count() else ""
        check("任务-空态与引导入口", has_empty and ("拍题讲解" in guidance or "学情" in guidance),
              f"empty={has_empty} guidance={guidance[:80]}")
        warning("任务中心为空态（隔离实例未产生 AI 任务），未覆盖任务详情/取消/续跑入口")
    shot(page, "tasks")
    # 过滤交互
    filters.nth(1).click()
    wait_quiet(page, 500)
    check("任务-过滤交互", filters.nth(1).get_attribute("aria-pressed") == "true",
          "点击过滤后 aria-pressed 未更新")
    assert_clean_errors(page)


# ---------------------------------------------------------------------------
# 6. AI 助手
# ---------------------------------------------------------------------------

def flow_assistant(page) -> None:
    mark_step("AI助手")
    goto_ready(page, BASE + "/#/assistant")
    page.wait_for_selector(".asst-page", timeout=25000)
    wait_quiet(page, 1000)
    conversations = page.locator(".asst-conv-open")
    check("助手-会话列表", conversations.count() >= 1, f"conversations={conversations.count()}")
    page.get_by_role("button", name="新建会话", exact=True).first.click()
    wait_quiet(page, 1000)
    check("助手-新建会话", "新的对话" in page.locator(".asst-chat-title").inner_text(),
          page.locator(".asst-chat-title").inner_text())
    # 技能选择器：展开并勾选一个技能（不发送消息）
    page.locator(".asst-skills summary").first.click()
    page.wait_for_selector(".asst-skill-option", timeout=15000)
    skill_count = page.locator(".asst-skill-option").count()
    check("助手-技能选择器", skill_count >= 1, f"skills={skill_count}")
    page.locator(".asst-skill-option input").first.check(force=True)
    wait_quiet(page, 900)
    check("助手-技能勾选", page.locator(".asst-skill-chip").count() >= 1,
          f"chips={page.locator('.asst-skill-chip').count()}")
    shot(page, "assistant-skills")

    # 无 provider 发送：应中文引导去配置模型（不真实调用模型）
    mark_step("assistant-无provider发送")
    page.locator(".asst-input").fill("验收：请帮我看看最近的错题。")
    page.locator(".asst-send-row button").filter(has_text="发送").first.click()
    page.wait_for_timeout(2500)
    toast = page.evaluate(
        "() => [...document.querySelectorAll('#toast-region .toast')].map(n => n.textContent).join(' | ')")
    bubbles = page.locator(".asst-error-bubble").all_inner_texts()
    guidance = " | ".join([toast] + bubbles)
    check("助手-无provider发送引导", bool(PROVIDER_HINT_RE.search(guidance)), guidance[:160])
    shot(page, "assistant-provider")
    # 发送失败不应产生正常回复气泡（无 provider 不会真实调用模型）
    reply_bubbles = page.locator(".asst-msg .markdown, .asst-msg-body .markdown").count()
    check("助手-未真实调用模型", reply_bubbles == 0, f"reply bubbles={reply_bubbles}")
    assert_clean_errors(page)


# ---------------------------------------------------------------------------
# 移动端 390
# ---------------------------------------------------------------------------

def flow_mobile(playwright) -> None:
    mark_step("移动端")
    marker = len(_errors)
    browser = launch_browser(playwright)
    try:
        context, page = new_context(browser, mobile=True)
        if not login(page, BASE, args.username, args.password):
            check("移动端-登录", False, "移动端登录失败")
            return
        goto_ready(page, BASE + "/#/home")
        page.wait_for_selector(".home-ai-card", timeout=25000)
        wait_quiet(page, 900)
        check("移动端-首页", page.locator(".home-ai-card").count() == 6,
              f"cards={page.locator('.home-ai-card').count()}")
        shot(page, "home-mobile")
        assert_no_overflow(page, "移动端首页")

        goto_ready(page, BASE + "/#/paperlab?tab=papers")
        page.wait_for_selector(".pl-paper-card", timeout=20000)
        wait_quiet(page, 700)
        shot(page, "paperlab-mobile")
        assert_no_overflow(page, "移动端试卷工坊")

        goto_ready(page, BASE + "/#/insight")
        page.wait_for_selector(".ins-page", timeout=25000)
        wait_quiet(page, 900)
        shot(page, "insight-mobile")
        assert_no_overflow(page, "移动端学情分析")
        context.close()
    finally:
        browser.close()
    for item in _errors[marker:]:
        if item.startswith("pageerror"):
            report["problems"].append(f"移动端: {item}")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def ensure_empty_account(playwright) -> bool:
    """注册独立的空态账号（用独立 APIRequestContext，避免覆盖主验收账号的会话 Cookie）。"""
    request_context = playwright.request.new_context(base_url=BASE)
    try:
        response = request_context.post(
            "/api/auth/register",
            data={"username": EMPTY_USERNAME, "password": EMPTY_PASSWORD})
        return response.status in (200, 201, 400, 409)
    except Exception:  # noqa: BLE001
        return False
    finally:
        request_context.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="整合模块全链路浏览器验收（首页/我的题库/试卷工坊/学情/任务/助手）")
    parser.add_argument("--base-url", default="http://127.0.0.1:8828", help="隔离实例地址")
    parser.add_argument("--username", default=DEFAULT_USERNAME, help="验收账号（不存在时 seed_demo 注册）")
    parser.add_argument("--password", default=DEFAULT_PASSWORD, help="验收账号密码")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="截图与报告输出目录（相对仓库根）")
    parser.add_argument("--seed", action="store_true", help="先调用 scripts/seed_demo.py 注入示例数据")
    parser.add_argument("--skip-seed", action="store_true", help="不运行 seed_demo，直接用已有数据")
    parser.add_argument("--skip-home", action="store_true")
    parser.add_argument("--skip-mybank", action="store_true")
    parser.add_argument("--skip-paperlab", action="store_true")
    parser.add_argument("--skip-insight", action="store_true")
    parser.add_argument("--skip-tasks", action="store_true")
    parser.add_argument("--skip-assistant", action="store_true")
    parser.add_argument("--skip-mobile", action="store_true")
    parser.add_argument("--skip-empty-state", action="store_true")
    options = parser.parse_args(argv)

    global BASE, OUT, args
    args = options
    
    BASE = options.base_url.rstrip("/")
    OUT = Path(options.out)
    if not OUT.is_absolute():
        OUT = ROOT / OUT
    OUT.mkdir(parents=True, exist_ok=True)
    report["base_url"] = BASE
    report["generated_at"] = datetime.now(timezone.utc).isoformat()

    started = time.time()
    if not options.skip_seed:
        mark_step("示例数据")
        seed_state = run_seed(BASE, options.username, options.password)
        report["seed"] = seed_state
        step("示例数据-seed_demo", seed_state.get("status") == "ok",
             detail=f"status={seed_state.get('status')} modules={seed_state.get('modules')}")
        if seed_state.get("status") != "ok":
            report["problems"].append("示例数据准备失败；可手动运行 scripts/seed_demo.py 后重试")
        assert_clean_errors_none()

    with sync_playwright() as playwright:
        browser = launch_browser(playwright)
        try:
            context, page = new_context(browser)
            mark_step("登录")
            if not login(page, BASE, options.username, options.password):
                report["problems"].append(
                    f"登录失败：请确认实例已启动且账号存在；可加 --seed 由 seed_demo 注册（{BASE}）")
                report["ok"] = False
                report["elapsed_seconds"] = round(time.time() - started, 1)
                (OUT / "modules-accept-report.json").write_text(
                    json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
                print(json.dumps({"ok": False, "problems": report["problems"]}, ensure_ascii=False, indent=2))
                return 1
            step("登录", True, detail=f"账号 {options.username}")
            report["data"].update(inspect_data(page))
            if not report["data"].get("papers") or not report["data"].get("imports"):
                warning("示例数据不完整（papers/imports 为 0），部分流程将记录问题")

            empty_account_ready = ensure_empty_account(playwright)
            if not options.skip_insight and not options.skip_empty_state and not empty_account_ready:
                report["problems"].append("空态账号注册失败，学情空态检查将跳过")

            flows = [
                ("--skip-home", options.skip_home, flow_home),
                ("--skip-mybank", options.skip_mybank, flow_mybank),
                ("--skip-paperlab", options.skip_paperlab, flow_paperlab),
                ("--skip-insight", options.skip_insight, flow_insight),
                ("--skip-tasks", options.skip_tasks, flow_tasks),
                ("--skip-assistant", options.skip_assistant, flow_assistant),
            ]
            for _flag, skipped, flow in flows:
                if skipped:
                    step(f"{flow.__name__}（跳过）", True)
                    continue
                try:
                    flow(page)
                except Exception as exc:  # noqa: BLE001 - 单流程失败不阻断其它模块
                    report["problems"].append(f"{flow.__name__}: 运行异常 {exc}")
                    traceback.print_exc()
                finally:
                    assert_clean_errors(page)
            if not options.skip_insight and not options.skip_empty_state:
                try:
                    flow_insight_empty(playwright)
                except Exception as exc:  # noqa: BLE001
                    report["problems"].append(f"学情-空态: 运行异常 {exc}")
            if not options.skip_mobile:
                try:
                    flow_mobile(playwright)
                except Exception as exc:  # noqa: BLE001
                    report["problems"].append(f"移动端: 运行异常 {exc}")
            context.close()
        finally:
            browser.close()

    report["ok"] = not report["problems"]
    report["elapsed_seconds"] = round(time.time() - started, 1)
    report["step_count"] = len(report["steps"])
    report["passed_steps"] = sum(1 for item in report["steps"] if item.get("ok"))
    report_path = OUT / "modules-accept-report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "ok": report["ok"],
        "steps": f"{report['passed_steps']}/{report['step_count']}",
        "screenshots": len(report["screenshots"]),
        "report": str(report_path),
        "problems": report["problems"],
        "warnings": report["warnings"][:8],
        "elapsed_seconds": report["elapsed_seconds"],
    }, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


def assert_clean_errors_none() -> None:
    page_errors = [item for item in _errors if item.startswith("pageerror")]
    for item in page_errors:
        report["problems"].append(f"示例数据: {item}")


if __name__ == "__main__":
    sys.exit(main())
