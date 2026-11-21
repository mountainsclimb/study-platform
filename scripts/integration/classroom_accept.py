"""AI 课堂 / 多智能体 / AI 助手浏览器验收：真实浏览器走
「创建 → 生成 → 大纲确认 → 播放 → 测验 → AI 对话 → PBL/交互 → 导出/导入 → 多智能体讨论/插话/回放」
与语音输入（ASR）能力检测，覆盖桌面 1440×900 与手机 390×844。

全部在本机完成：
- 需要一个隔离实例（独立 STUDY_DATA_DIR；建议 STUDY_BANK_DIR=稳定题库 r3）；
- 需要一个本机假模型服务：`python scripts/integration/fake_model_server.py --port 8902`
  （只监听 127.0.0.1，无外网；脚本会自动注册账号并创建指向它的连接，Key 为本地占位串）；
- 浏览器：Playwright（Chromium / 本机 Edge / Chrome），headless。

不读取真实 API Key、不访问外网、不写学生数据；截图与报告落 screenshots/integration/。
用法见 scripts/integration/README.md。
"""
import argparse
import http.cookiejar
import json
import re
import shutil
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUT = ROOT / "screenshots" / "integration"
DESKTOP = {"width": 1440, "height": 900}
MOBILE = {"width": 390, "height": 844}
DUMMY_API_KEY = "sk-local-acceptance-only"  # 仅写入本机隔离库，指向本地假模型服务


# 打开「每日一签」自动弹层的抑制脚本：验收时预置当天已读，避免模态框拦截点击。
_SUPPRESS_QUOTE_JS = """
try {
  const d = new Date();
  const pad = (n) => String(n).padStart(2, "0");
  localStorage.setItem("zhixu.quote.lastShown", `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`);
} catch (e) { /* ignore */ }
"""

BASE = "http://127.0.0.1:8827"
USERNAME = "课堂验收"
PASSWORD = "ui-pass-12345"
FAKE_MODEL_BASE_URL = "http://127.0.0.1:8899/v1"
MODEL = "mock-classroom-model"
OUT = DEFAULT_OUT

report = {"base_url": "", "steps": [], "screenshots": [], "problems": []}


def shot(page, name):
    path = OUT / name
    page.screenshot(path=str(path), full_page=False)
    report["screenshots"].append(str(path))
    return str(path)


def step(name, **data):
    report["steps"].append({"step": name, **data})


def login(page):
    page.goto(BASE + "/", wait_until="domcontentloaded")
    try:
        page.wait_for_selector(".layout, .page-heading", timeout=5000)
        return
    except Exception:
        pass
    page.wait_for_selector(".auth-form-card", timeout=15000)
    page.get_by_label("用户名").fill(USERNAME)
    page.get_by_label("密码").fill(PASSWORD)
    page.get_by_role("button", name="登录学习工作台").click()
    page.wait_for_selector(".layout", timeout=20000)


def launch(playwright):
    for channel in ("msedge", "chrome", None):
        try:
            if channel:
                return playwright.chromium.launch(channel=channel, headless=True)
            return playwright.chromium.launch(headless=True)
        except Exception:  # noqa: BLE001
            continue
    raise RuntimeError("未找到可用的浏览器")


def attach_error_capture(page, label):
    errors = []

    def on_pageerror(exc):
        errors.append(f"pageerror[{label}]: {exc}")

    def on_console(msg):
        if msg.type == "error":
            text = msg.text
            # 未登录时 /api/auth/me 的 401 是应用正常引导流程；favicon 404 也不视为页面异常。
            if "favicon" in text or ("401" in text and "Failed to load resource" in text):
                return
            errors.append(f"console[{label}]: {text}")

    page.on("pageerror", on_pageerror)
    page.on("console", on_console)
    return errors


def overflow(page):
    return page.evaluate(
        "() => Math.max(0, (document.documentElement.scrollWidth || 0) - (window.innerWidth || 0))"
    )


def check(condition, message):
    """断言失败时记录到 problems 并继续，保证失败也输出截图与报告。"""
    if not condition:
        report["problems"].append(message)
        return False
    return True


def open_multi_drawer(page):
    page.locator(".cls-ma-side-card").get_by_role("button", name="开始讨论").first.click()
    page.wait_for_selector(".cls-tutor.open", timeout=10000)
    page.wait_for_selector('.cls-chat-panel[data-chat-panel="multi"]:not([hidden])', timeout=10000)


def desktop_flow(browser):
    context = browser.new_context(viewport=DESKTOP, locale="zh-CN")
    context.add_init_script(_SUPPRESS_QUOTE_JS)
    page = context.new_page()
    errors = attach_error_capture(page, "desktop")
    login(page)

    # 1) 课程列表（首次进入，可能为空状态；结束后再补一张有课程的列表）
    page.goto(BASE + "/#/classroom", wait_until="domcontentloaded")
    page.wait_for_selector(".page-heading h1", timeout=15000)
    page.wait_for_selector(".cls-course-card, .empty", timeout=15000)
    page.wait_for_timeout(500)
    step("课程列表", title=page.locator(".page-heading h1").inner_text(),
         cards=page.locator(".cls-course-card").count(), overflow=overflow(page))

    # 2) 创建课堂
    page.get_by_role("button", name="新建课堂").first.click()
    page.wait_for_selector(".cls-create-card", timeout=15000)
    page.get_by_placeholder("例如：光合作用的原理与影响因素").fill("海洋中的光合作用与氧气循环")
    page.get_by_placeholder("可选，默认取主题前 30 字").fill("海洋光合作用入门")
    page.locator(".cls-create-grid select").nth(0).select_option("生物")
    page.locator(".cls-create-grid select").nth(1).select_option("初中二年级")
    page.locator(".cls-create-grid select").nth(2).select_option("interactive")
    page.get_by_placeholder("补充教学要求，例如：面向初中生、重点讲清原料与产物、多举生活例子。").fill(
        "面向初中生，重点讲清原料、产物与意义，多举生活例子。"
    )
    page.wait_for_timeout(400)
    shot(page, "classroom-create-desktop.png")
    step("创建课堂表单", overflow=overflow(page))

    # 3) 生成工作台（选择模型；勾选 PBL 类型以覆盖真实 PBL 结构）
    page.get_by_role("button", name="创建并进入生成").click()
    page.wait_for_selector(".cls-setup-models", timeout=20000)
    page.locator(".cls-check-pill").filter(has_text="项目式学习").locator("input").check()
    page.wait_for_timeout(500)
    hash_text = page.evaluate("() => location.hash")
    match = re.search(r"course=(\d+)", hash_text)
    course_id = int(match.group(1)) if match else None
    report["course_id"] = course_id
    shot(page, "classroom-generate-setup-desktop.png")
    step("生成工作台", course_id=course_id, overflow=overflow(page))

    # 4) 生成 → 大纲确认
    page.get_by_role("button", name="生成课程（大纲 → 场景）").click()
    page.wait_for_selector(".cls-outline-card", timeout=40000)
    page.wait_for_timeout(500)
    shot(page, "classroom-outline-confirm-desktop.png")
    scene_rows = page.locator(".cls-outline-row").count()
    step("大纲确认", scene_rows=scene_rows, overflow=overflow(page))
    first_title = page.locator(".cls-outline-row input").first
    first_title.fill("导入：海洋光合作用")

    # 5) 确认后逐场景生成完成（把第 2 页改成 PBL，覆盖真实 PBL 结构展示）
    page.locator(".cls-outline-line select").nth(1).select_option("pbl")
    page.locator(".cls-outline-line input").nth(1).fill("校园节能项目")
    page.get_by_role("button", name="确认大纲并生成场景").click()
    page.wait_for_selector(".cls-done-card", timeout=120000)
    page.wait_for_timeout(400)
    shot(page, "classroom-generate-done-desktop.png")
    step("生成完成", overflow=overflow(page))

    # 5.5) 导出：PPTX / 离线单文件 HTML / 课程 ZIP（真实下载 + 文件头/内容校验）
    downloads = Path(tempfile.mkdtemp(prefix="classroom-accept-"))
    pptx_path = downloads / "classroom-export.pptx"
    html_path = downloads / "classroom-export.html"
    zip_path = downloads / "classroom-export.zip"
    page.get_by_role("button", name="导出课堂").first.click()
    page.wait_for_selector(".cls-export-dialog", timeout=10000)
    shot(page, "classroom-export-dialog-desktop.png")
    try:
        with page.expect_download(timeout=180000) as info:
            page.get_by_role("button", name="导出 PPTX").click()
        info.value.save_as(str(pptx_path))
        check(pptx_path.stat().st_size > 1000 and pptx_path.read_bytes()[:2] == b"PK",
              "PPTX 导出文件无效（应为 ZIP 容器，PK 头）")
        with page.expect_download(timeout=180000) as info:
            page.get_by_role("button", name="导出离线 HTML").click()
        info.value.save_as(str(html_path))
        html_text = html_path.read_text(encoding="utf-8", errors="ignore")
        check("<html" in html_text.lower() and len(html_text) > 2000, "离线 HTML 内容异常（过短或非 HTML）")
        check("光合作用" in html_text, "离线 HTML 未包含课程内容")
        with page.expect_download(timeout=180000) as info:
            page.get_by_role("button", name="导出课程 ZIP").click()
        info.value.save_as(str(zip_path))
        import zipfile
        with zipfile.ZipFile(zip_path) as archive:
            check("manifest.json" in archive.namelist(), "课程 ZIP 缺少 manifest.json")
        step("导出", pptx_bytes=pptx_path.stat().st_size, html_bytes=html_path.stat().st_size,
             zip_bytes=zip_path.stat().st_size)
    except Exception as exc:  # noqa: BLE001
        report["problems"].append(f"导出链路失败：{type(exc).__name__}: {exc}")
        step("导出", error=str(exc)[:160])
    finally:
        close = page.get_by_role("button", name="关闭")
        if close.count():
            close.first.click()
        page.wait_for_timeout(300)

    # 6) 进入播放器
    page.get_by_role("button", name="进入课堂播放").first.click()
    page.wait_for_selector(".cls-stage-shell", timeout=20000)
    page.wait_for_timeout(1200)
    shot(page, "classroom-player-slide-desktop.png")
    step("播放器首屏", script=page.evaluate("() => (document.querySelector('.cls-player-meta')||{}).innerText || ''"),
         overflow=overflow(page))

    # 7) 播放：聚光灯效果
    page.locator(".cls-control-play").click()
    page.wait_for_timeout(1000)
    shot(page, "classroom-player-spotlight-desktop.png")
    step("聚光灯/字幕", caption=page.evaluate("() => (document.querySelector('.cls-caption')||{}).innerText || ''"),
         spotlight_visible=page.evaluate(
             "() => { const n = document.querySelector('.cls-effect-spot'); return Boolean(n && !n.hidden); }"))
    page.locator(".cls-control-play").click()  # 暂停
    page.wait_for_timeout(300)

    # 8) AI 老师对话
    page.get_by_role("button", name="AI 老师").first.click()
    page.wait_for_selector(".cls-tutor.open", timeout=10000)
    page.locator(".cls-tutor-input").fill("为什么水是光合作用的原料？")
    page.get_by_role("button", name="发送").click()
    page.wait_for_selector(".cls-tutor-message.is-assistant", timeout=30000)
    page.wait_for_timeout(500)
    shot(page, "classroom-tutor-desktop.png")
    step("AI 老师对话",
         assistant_text=page.locator(".cls-tutor-message.is-assistant .cls-tutor-bubble").last.inner_text()[:120])
    page.get_by_role("button", name="关闭").first.click()

    # 9) PBL 场景（真实项目结构只读展示）
    page.locator(".cls-scene-chip").nth(1).click()
    page.wait_for_selector(".cls-pbl-card", timeout=20000)
    page.wait_for_timeout(400)
    shot(page, "classroom-pbl-desktop.png")
    pbl_text = page.locator(".cls-pbl-card").inner_text()
    check("角色" in pbl_text and "里程碑" in pbl_text, "PBL 结构展示不完整（缺角色/里程碑）")
    step("PBL 场景", text=pbl_text[:160], overflow=overflow(page))

    # 10) 交互场景：沙箱 iframe + postMessage 上报 → 后端 progress.interactive
    page.locator(".cls-scene-chip").nth(2).click()
    page.wait_for_selector(".cls-interactive-frame", timeout=20000)
    page.wait_for_timeout(2200)
    shot(page, "classroom-interactive-desktop.png")
    interactive_status = page.evaluate(
        "() => (document.querySelector('.cls-interactive-status')||{}).textContent || ''"
    )
    session_id = page.evaluate(
        "(cid) => { try { return (JSON.parse(localStorage.getItem('zhixu.classroom.session.' + cid) || '{}')).id; } catch (e) { return null; } }",
        course_id,
    )
    progress = None
    if session_id:
        response = context.request.get(BASE + f"/api/classroom/sessions/{session_id}")
        if response.ok:
            progress = response.json().get("progress") or {}
    interactive_progress = (progress or {}).get("interactive") or {}
    step("交互场景", status=interactive_status,
         sandbox=page.evaluate("() => (document.querySelector('.cls-interactive-frame')||{}).getAttribute('sandbox') || ''"),
         progress_summary=interactive_progress.get("summary"),
         overflow=overflow(page))
    if "交互状态已同步" not in interactive_status and "已就绪" not in interactive_status and "上次交互" not in interactive_status:
        report["problems"].append(f"交互场景未收到 postMessage 状态上报：{interactive_status}")
    if not interactive_progress.get("summary"):
        report["problems"].append(f"交互状态未写入会话 progress（{progress}）")

    # 11) 测验场景：作答并判分
    page.locator(".cls-scene-chip").nth(3).click()
    page.wait_for_selector(".cls-quiz-panel", timeout=15000)
    page.wait_for_timeout(400)
    shot(page, "classroom-quiz-desktop.png")
    question_blocks = page.locator(".cls-quiz-question")
    question_blocks.nth(0).locator(".cls-quiz-option").nth(0).click()
    question_blocks.nth(1).locator(".cls-quiz-option").nth(0).click()
    question_blocks.nth(1).locator(".cls-quiz-option").nth(1).click()
    page.get_by_role("button", name="提交判分").click()
    page.wait_for_selector(".cls-quiz-result-head", timeout=20000)
    page.wait_for_timeout(400)
    shot(page, "classroom-quiz-result-desktop.png")
    step("测验判分", result=page.locator(".cls-quiz-result-head").inner_text()[:120],
         overflow=overflow(page))

    # 12) 进度保存：刷新后应续播到测验场景
    page.reload(wait_until="domcontentloaded")
    try:
        page.wait_for_selector(".cls-quiz-panel, .cls-stage-shell", timeout=20000)
        page.wait_for_timeout(800)
        resumed = page.evaluate("() => (document.querySelector('.cls-player-meta')||{}).innerText || ''")
        shot(page, "classroom-player-resume-desktop.png")
        step("刷新续播", resumed_meta=resumed)
    except Exception as exc:  # noqa: BLE001
        report["problems"].append(f"刷新续播失败：{exc}")

    # 13) 课程列表（已有课程）+ 导入课程 ZIP
    page.goto(BASE + "/#/classroom", wait_until="domcontentloaded")
    page.wait_for_selector(".cls-course-card", timeout=15000)
    page.wait_for_timeout(500)
    cards_before = page.locator(".cls-course-card").count()
    shot(page, "classroom-list-desktop.png")
    page.locator('.cls-page input[type="file"]').first.set_input_files(str(zip_path))
    try:
        page.wait_for_url(re.compile(r"#/classroom\?course=\d+"), timeout=60000)
        page.wait_for_selector(".cls-stage-shell", timeout=30000)
        step("导入课程 ZIP", imported=True)
    except Exception as exc:  # noqa: BLE001
        report["problems"].append(f"课程 ZIP 导入失败：{exc}")
    page.goto(BASE + "/#/classroom", wait_until="domcontentloaded")
    page.wait_for_selector(".cls-course-card", timeout=15000)
    page.wait_for_timeout(500)
    cards_after = page.locator(".cls-course-card").count()
    shot(page, "classroom-list-imported-desktop.png")
    step("课程列表（含课程）", cards_before=cards_before, cards_after=cards_after,
         overflow=overflow(page))
    if cards_after <= cards_before:
        report["problems"].append("导入课程后课程列表数量未增加")

    shutil.rmtree(downloads, ignore_errors=True)
    step("桌面端 JS 异常", errors=errors)
    if errors:
        report["problems"].extend(errors[:5])
    context.close()
    return course_id


def mobile_flow(browser, course_id):
    context = browser.new_context(viewport=MOBILE, is_mobile=True, has_touch=True, locale="zh-CN")
    context.add_init_script(_SUPPRESS_QUOTE_JS)
    page = context.new_page()
    errors = attach_error_capture(page, "mobile")
    login(page)

    page.goto(BASE + "/#/classroom", wait_until="domcontentloaded")
    page.wait_for_selector(".page-heading h1", timeout=15000)
    page.wait_for_timeout(800)
    shot(page, "classroom-list-mobile.png")
    mobile_overflow = overflow(page)
    step("移动端课程列表", overflow=mobile_overflow)

    if course_id:
        page.goto(BASE + f"/#/classroom?course={course_id}&view=play", wait_until="domcontentloaded")
        page.wait_for_selector(".cls-stage-shell", timeout=20000)
        page.wait_for_timeout(1200)
        shot(page, "classroom-player-mobile.png")
        step("移动端播放器", overflow=overflow(page), meta=page.evaluate(
            "() => (document.querySelector('.cls-player-meta')||{}).innerText || ''"))
        page.locator(".cls-scene-chip").nth(3).click()
        page.wait_for_selector(".cls-quiz-panel", timeout=15000)
        page.wait_for_timeout(400)
        shot(page, "classroom-quiz-mobile.png")
        step("移动端测验", overflow=overflow(page))
    step("移动端 JS 异常", errors=errors)
    if errors:
        report["problems"].extend(errors[:5])
    context.close()


def multiagent_flow(browser, course_id):
    """多智能体课堂：名册生成 → 讨论逐段播放/暂停/跳过/重播 → 插话 → 历史回放 → 白板 unit 坐标。"""
    context = _new_context(browser, DESKTOP)
    page = context.new_page()
    errors = attach_error_capture(page, "multiagent")
    login(page)
    page.goto(BASE + f"/#/classroom?course={course_id}&view=play", wait_until="domcontentloaded")
    page.wait_for_selector(".cls-stage-shell", timeout=25000)
    page.wait_for_selector(".cls-ma-side-card", timeout=15000)
    shotgun = "classroom-multiagent"
    step("多智能体入口", side_card=True, overflow=overflow(page))

    open_multi_drawer(page)
    page.wait_for_selector(".cls-ma-roster", timeout=10000)
    shot(page, f"{shotgun}-roster-empty-desktop.png")
    try:
        page.wait_for_selector(
            ".cls-ma-roster button:has-text('生成课堂角色'), .cls-ma-roster button:has-text('重新生成角色')",
            timeout=15000)
        page.locator(
            ".cls-ma-roster button:has-text('生成课堂角色'), .cls-ma-roster button:has-text('重新生成角色')"
        ).first.click()
        page.wait_for_selector(".cls-ma-roster button:has-text('重新生成角色')", timeout=60000)
        page.wait_for_selector(".cls-ma-agent", timeout=10000)
        page.wait_for_timeout(400)
    except Exception as exc:  # noqa: BLE001
        report["problems"].append(f"角色名册生成失败：{type(exc).__name__}: {exc}")
    roster_count = page.locator(".cls-ma-agent").count()
    shot(page, f"{shotgun}-roster-desktop.png")
    check(roster_count >= 3, f"角色名册数量不足（期望 ≥3）：{roster_count}")
    step("角色名册生成", roster_count=roster_count,
         names=page.locator(".cls-ma-agent strong").all_inner_texts())

    # 开始讨论：返回 turns 逐段出现 + 播放控制
    page.locator(".cls-ma-panel .cls-ma-topic").fill("光合作用的原料与产物")
    page.locator(".cls-ma-panel").get_by_role("button", name="开始讨论").click()
    try:
        page.wait_for_selector('.cls-tutor-message[data-role="teacher"]', timeout=40000)
        page.wait_for_selector(".cls-ma-play-controls:not([hidden])", timeout=20000)
        page.wait_for_timeout(300)
        shot(page, f"{shotgun}-discussion-desktop.png")
        step("多角色讨论", bubbles=page.locator(".cls-tutor-message").count())
    except Exception as exc:  # noqa: BLE001
        report["problems"].append(f"多角色讨论未开始：{type(exc).__name__}: {exc}")

    # 暂停 / 继续
    try:
        page.locator(".cls-ma-play-controls").get_by_role("button", name="暂停", exact=True).first.click()
        page.wait_for_timeout(400)
        paused_status = page.locator(".cls-ma-status").inner_text()
        shot(page, f"{shotgun}-pause-desktop.png")
        page.locator(".cls-ma-play-controls").get_by_role("button", name="继续", exact=True).first.click()
        page.wait_for_timeout(300)
        check("已暂停" in paused_status, f"讨论暂停状态文本异常：{paused_status}")
        step("讨论暂停/继续", paused_status=paused_status[:80])
    except Exception as exc:  # noqa: BLE001
        report["problems"].append(f"讨论暂停/继续失败：{type(exc).__name__}: {exc}")

    # 跳过到白板段，断言 unit 坐标与 7 种 op 渲染
    for _ in range(2):
        skip = page.locator(".cls-ma-play-controls").get_by_role("button", name="跳过本条")
        if skip.count():
            skip.first.click()
        page.wait_for_timeout(500)
    try:
        page.wait_for_selector(".cls-whiteboard svg, .cls-wb-path", timeout=25000)
    except Exception:  # noqa: BLE001
        pass
    page.wait_for_timeout(400)
    whiteboard = {
        "svg": page.locator(".cls-whiteboard svg").count(),
        "path": page.locator(".cls-wb-path").count(),
        "text": page.locator(".cls-wb-text").count(),
        "formula": page.locator(".cls-wb-formula").count(),
    }
    path_box = None
    if whiteboard["path"]:
        try:
            path_box = page.locator(".cls-wb-path").first.evaluate(
                "el => ({ left: parseFloat(el.style.left), top: parseFloat(el.style.top) })")
        except Exception:  # noqa: BLE001
            path_box = None
    shot(page, f"{shotgun}-whiteboard-desktop.png")
    check(all(whiteboard[key] for key in ("svg", "path", "text", "formula")),
          f"unit 白板渲染不完整：{whiteboard}")
    if path_box:
        check(abs(path_box["left"] - 0.62 * 1000) < 40 and abs(path_box["top"] - 0.62 * 562.5) < 40,
              f"白板 unit 坐标换算异常：{path_box}")
    step("白板 unit 坐标/多 op", **whiteboard, first_path_box=path_box)

    # 重播本条：气泡不重复、队列继续
    try:
        before = page.locator(".cls-tutor-message").count()
        page.locator(".cls-ma-play-controls").get_by_role("button", name="重播本条").first.click()
        page.wait_for_timeout(900)
        after = page.locator(".cls-tutor-message").count()
        shot(page, f"{shotgun}-replay-desktop.png")
        check(before == after, f"重播本条不应重复追加气泡：{before} -> {after}")
        step("重播本条", bubbles_before=before, bubbles_after=after,
             status=page.locator(".cls-ma-status").inner_text()[:80])
    except Exception as exc:  # noqa: BLE001
        report["problems"].append(f"重播本条失败：{type(exc).__name__}: {exc}")

    # 学生插话：loading 状态 + 角色回应追加
    try:
        page.route("**/api/classroom/sessions/*/interject",
                   lambda route: (time.sleep(0.8), route.continue_()))
        page.locator(".cls-ma-interject-input").fill("老师，叶绿体里的色素是怎么吸收光能的？")
        page.locator(".cls-ma-panel").get_by_role("button", name="发送插话").click()
        page.wait_for_timeout(400)
        loading_status = page.locator(".cls-ma-status").inner_text()
        shot(page, f"{shotgun}-interject-loading-desktop.png")
        page.wait_for_function(
            "() => document.querySelectorAll('.cls-tutor-message').length >= 4",
            timeout=40000)
        page.wait_for_timeout(600)
        shot(page, f"{shotgun}-interject-desktop.png")
        step("学生插话", loading_status=loading_status[:80],
             bubbles=page.locator(".cls-tutor-message").count())
        stop = page.locator(".cls-ma-play-controls").get_by_role("button", name="停止")
        if stop.count():
            stop.first.click()
            page.wait_for_timeout(300)
    except Exception as exc:  # noqa: BLE001
        report["problems"].append(f"学生插话失败：{type(exc).__name__}: {exc}")

    # 讨论写入会话 turns（持久化 + 动作白名单）
    session_id = page.evaluate(
        "(cid) => { try { return (JSON.parse(localStorage.getItem('zhixu.classroom.session.' + cid) || '{}')).id; } catch (e) { return null; } }",
        course_id,
    )
    turns = []
    if session_id:
        response = context.request.get(BASE + f"/api/classroom/sessions/{session_id}")
        if response.ok:
            turns = response.json().get("turns") or []
    roles = sorted({turn.get("role") for turn in turns})
    action_types = sorted({action.get("type") for turn in turns for action in (turn.get("actions") or [])})
    step("讨论写入会话", session_id=session_id, turns=len(turns), roles=roles,
         action_types=action_types)
    check(len(turns) > 0, "讨论未写入会话 turns")
    check({"teacher", "student"} & set(roles), f"turns 缺少多角色发言：{roles}")
    check("whiteboard" in action_types, f"讨论动作未包含 whiteboard：{action_types}")

    # 刷新回放统一历史 + 清空当前显示
    try:
        page.reload(wait_until="domcontentloaded")
        page.wait_for_selector(".cls-stage-shell", timeout=25000)
        open_multi_drawer(page)
        page.wait_for_timeout(800)
        shot(page, f"{shotgun}-history-desktop.png")
        history = page.locator(".cls-tutor-message").count()
        step("统一历史回放", bubbles=history)
        check(history >= 3, f"刷新后讨论历史未回放：{history}")
        page.locator(".cls-tutor-head").get_by_role("button", name="清空").first.click()
        page.wait_for_timeout(400)
        shot(page, f"{shotgun}-clear-desktop.png")
        cleared = page.locator(".cls-tutor-message").count()
        check(cleared == 0, f"清空对话后仍有气泡：{cleared}")
        step("清空对话显示", bubbles_after_clear=cleared)
        page.locator(".cls-tutor-head").get_by_role("button", name="关闭").first.click()
        page.wait_for_timeout(200)
    except Exception as exc:  # noqa: BLE001
        report["problems"].append(f"历史回放/清空失败：{type(exc).__name__}: {exc}")

    step("多智能体 JS 异常", errors=errors)
    report["problems"].extend(errors[:5])
    context.close()


def multiagent_mobile_flow(browser, course_id):
    """移动端 390×844：播放器侧栏进入多智能体抽屉，名册/讨论编排不溢出。"""
    context = _new_context(browser, MOBILE, mobile=True)
    page = context.new_page()
    errors = attach_error_capture(page, "multiagent-mobile")
    login(page)
    page.goto(BASE + f"/#/classroom?course={course_id}&view=play", wait_until="domcontentloaded")
    page.wait_for_selector(".cls-stage-shell", timeout=25000)
    page.wait_for_selector(".cls-ma-side-card", timeout=15000)
    open_multi_drawer(page)
    page.wait_for_timeout(800)
    shot(page, "classroom-multiagent-mobile.png")
    mobile_overflow = overflow(page)
    step("移动端多智能体", overflow=mobile_overflow,
         roster=page.locator(".cls-ma-agent").count(),
         bubbles=page.locator(".cls-tutor-message").count())
    check(mobile_overflow <= 1, f"移动端多智能体横向溢出 {mobile_overflow}px")
    step("移动端多智能体 JS 异常", errors=errors)
    report["problems"].extend(errors[:3])
    context.close()


def _http_call(opener, path, method="GET", data=None):
    body = None if data is None else json.dumps(data).encode("utf-8")
    request = urllib.request.Request(
        BASE + path, data=body, method=method,
        headers={"Content-Type": "application/json", "X-Requested-With": "XMLHttpRequest"},
    )
    try:
        with opener.open(request, timeout=30) as response:
            return response.status, json.loads(response.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as error:
        try:
            payload = json.loads(error.read().decode("utf-8") or "{}")
        except json.JSONDecodeError:
            payload = {"detail": error.reason}
        return error.code, payload


def bootstrap_account():
    """自动准备验收账号与指向本机假模型的连接（仅写隔离库；Key 为本地占位串）。"""
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    status, payload = _http_call(opener, "/api/auth/login", "POST",
                                 {"username": USERNAME, "password": PASSWORD})
    if status != 200:
        status, payload = _http_call(opener, "/api/auth/register", "POST",
                                     {"username": USERNAME, "password": PASSWORD})
        if status != 200:
            raise SystemExit(f"验收账号初始化失败（HTTP {status}）：{payload}")
        status, payload = _http_call(opener, "/api/auth/login", "POST",
                                     {"username": USERNAME, "password": PASSWORD})
        if status != 200:
            raise SystemExit(f"验收账号登录失败（HTTP {status}）：{payload}")
    target = FAKE_MODEL_BASE_URL.rstrip("/")
    status, listing = _http_call(opener, "/api/providers")
    if status != 200:
        raise SystemExit(f"读取模型连接失败（HTTP {status}）：{listing}")
    for item in listing.get("items") or []:
        if str(item.get("base_url") or "").rstrip("/") == target and MODEL in (item.get("models") or []):
            return
    status, created = _http_call(opener, "/api/providers", "POST", {
        "name": "本机假模型（验收）",
        "kind": "openai",
        "base_url": target,
        "api_key": DUMMY_API_KEY,
        "models": [MODEL],
    })
    if status != 200:
        raise SystemExit(f"创建验收模型连接失败（HTTP {status}）：{created}")


# 语音输入验收：用 mock 的 SpeechRecognition 在浏览器内模拟「临时结果 → 最终结果 → 结束」，
# 证明组件只把文本填入输入框、不会自动发送；不支持状态则删除构造函数验证降级提示。
ASR_MOCK_SCRIPT = r"""
(() => {
  class MockSpeechRecognition {
    constructor() {
      this.lang = '';
      this.continuous = false;
      this.interimResults = true;
      this.maxAlternatives = 1;
      this._timers = [];
    }
    _later(fn, ms) {
      const timer = setTimeout(() => { try { fn(); } catch (error) { /* ignore */ } }, ms);
      this._timers.push(timer);
    }
    start() {
      window.__voiceMockStarted = (window.__voiceMockStarted || 0) + 1;
      this._later(() => this.onstart && this.onstart(), 20);
      this._later(() => this.onresult && this.onresult({
        resultIndex: 0,
        results: [{ 0: { transcript: '为什么水' }, isFinal: false, length: 1 }],
      }), 120);
      this._later(() => {
        this.onresult && this.onresult({
          resultIndex: 0,
          results: [{ 0: { transcript: '为什么水是光合作用的原料？' }, isFinal: true, length: 1 }],
        });
        this.onend && this.onend();
      }, 280);
    }
    stop() {
      this._timers.forEach(timer => clearTimeout(timer));
      this._timers = [];
      this.onend && this.onend();
    }
    abort() { this.stop(); }
  }
  window.SpeechRecognition = MockSpeechRecognition;
  window.webkitSpeechRecognition = MockSpeechRecognition;
})();
"""

ASR_DISABLE_SCRIPT = (
    "try { delete window.SpeechRecognition; delete window.webkitSpeechRecognition; } "
    "catch (error) { window.SpeechRecognition = undefined; window.webkitSpeechRecognition = undefined; }"
)


def _new_context(browser, viewport, *, mobile=False, asr_script=None):
    context = browser.new_context(
        viewport=viewport, locale="zh-CN",
        is_mobile=mobile, has_touch=mobile,
    )
    # 预置「每日一签」当天已读，避免自动弹层拦截自动化点击。
    context.add_init_script(_SUPPRESS_QUOTE_JS)
    if asr_script:
        context.add_init_script(asr_script)
    return context


def asr_supported_flow(browser, course_id):
    """支持语音输入：课堂 tutor 输入框 + AI 助手输入框都只填入文本、不自动发送。"""
    if not course_id:
        report["problems"].append("语音输入验收缺少课程 id（请先跑课堂生成流程）")
        return
    context = _new_context(browser, DESKTOP, asr_script=ASR_MOCK_SCRIPT)
    page = context.new_page()
    errors = attach_error_capture(page, "asr-supported")
    login(page)

    page.goto(BASE + f"/#/classroom?course={course_id}&view=play", wait_until="domcontentloaded")
    page.wait_for_selector(".cls-stage-shell", timeout=25000)
    page.get_by_role("button", name="AI 老师").first.click()
    page.wait_for_selector(".cls-tutor.open", timeout=10000)
    tutor_voice = page.locator(".cls-tutor .cls-voice-btn")
    tutor_voice.wait_for(state="visible", timeout=10000)
    if not tutor_voice.is_enabled():
        report["problems"].append("课堂 tutor 语音按钮在支持 SpeechRecognition 时被禁用")
    turns_before = page.locator(".cls-tutor-message").count()
    tutor_voice.click()
    page.wait_for_function(
        "() => (document.querySelector('.cls-tutor-input') || {}).value.includes('光合作用')",
        timeout=8000,
    )
    tutor_value = page.locator(".cls-tutor-input").input_value()
    turns_after = page.locator(".cls-tutor-message").count()
    shot(page, "classroom-asr-supported-desktop.png")
    step("语音输入（课堂 tutor，支持）", value=tutor_value,
         messages_before=turns_before, messages_after=turns_after,
         hint=page.locator(".cls-tutor .cls-voice-hint").inner_text())
    if turns_after != turns_before:
        report["problems"].append("课堂语音输入不应自动发送消息")

    page.goto(BASE + "/#/assistant", wait_until="domcontentloaded")
    page.wait_for_selector(".asst-input-row", timeout=20000)
    assistant_voice = page.locator(".asst-voice-btn")
    assistant_voice.wait_for(state="visible", timeout=10000)
    if not assistant_voice.is_enabled():
        report["problems"].append("AI 助手语音按钮在支持 SpeechRecognition 时被禁用")
    user_messages_before = page.locator(".asst-msg-user").count()
    assistant_voice.click()
    page.wait_for_function(
        "() => (document.querySelector('.asst-input') || {}).value.includes('光合作用')",
        timeout=8000,
    )
    assistant_value = page.locator(".asst-input").input_value()
    shot(page, "assistant-asr-supported-desktop.png")
    step("语音输入（AI 助手，支持）", value=assistant_value,
         user_messages=page.locator(".asst-msg-user").count(),
         hint=page.locator(".asst-voice-hint").inner_text())
    if page.locator(".asst-msg-user").count() != user_messages_before:
        report["problems"].append("AI 助手语音输入不应自动发送消息")
    step("语音输入（支持）JS 异常", errors=errors)
    report["problems"].extend(errors[:3])
    context.close()


def asr_unsupported_flow(browser):
    """不支持语音输入：按钮禁用 + 中文降级提示（桌面）。"""
    context = _new_context(browser, DESKTOP, asr_script=ASR_DISABLE_SCRIPT)
    page = context.new_page()
    errors = attach_error_capture(page, "asr-unsupported")
    login(page)
    page.goto(BASE + "/#/assistant", wait_until="domcontentloaded")
    page.wait_for_selector(".asst-input-row", timeout=20000)
    voice = page.locator(".asst-voice-btn")
    voice.wait_for(state="visible", timeout=10000)
    disabled = voice.is_disabled()
    hint = page.locator(".asst-voice-hint").inner_text()
    page.wait_for_timeout(400)
    shot(page, "assistant-asr-unsupported-desktop.png")
    step("语音输入（不支持状态，桌面）", disabled=disabled, hint=hint)
    # 产品有两种合法降级：服务端 ASR 可用 → 按钮可用并提示「已切换到服务端转写」；
    # 服务端也不可用 → 按钮禁用并提示「不支持语音识别」。二者都不是静默失败。
    server_mode = (not disabled) and ("服务端转写" in hint)
    disabled_mode = disabled and ("不支持语音" in hint)
    if not (server_mode or disabled_mode):
        report["problems"].append("不支持 SpeechRecognition 时语音按钮未按预期降级（禁用+提示 或 服务端转写）")
    report["problems"].extend(errors[:3])
    context.close()


def asr_mobile_flows(browser):
    """语音输入移动端 390×844：支持状态（可填入文本）与不支持状态（禁用 + 提示）。"""
    for supported, label in ((True, "supported"), (False, "unsupported")):
        script = ASR_MOCK_SCRIPT if supported else ASR_DISABLE_SCRIPT
        context = _new_context(browser, MOBILE, mobile=True, asr_script=script)
        page = context.new_page()
        login(page)
        page.goto(BASE + "/#/assistant", wait_until="domcontentloaded")
        page.wait_for_selector(".asst-input-row", timeout=25000)
        voice = page.locator(".asst-voice-btn")
        voice.wait_for(state="visible", timeout=10000)
        if supported:
            voice.click()
            page.wait_for_function(
                "() => (document.querySelector('.asst-input') || {}).value.includes('光合作用')",
                timeout=8000,
            )
        else:
            page.wait_for_timeout(300)
        page.wait_for_timeout(300)
        shot(page, f"assistant-asr-{label}-mobile.png")
        step(f"语音输入（移动端，{'支持' if supported else '不支持'}）",
             disabled=voice.is_disabled(),
             value=page.locator(".asst-input").input_value() if supported else "",
             overflow=overflow(page))
        context.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base-url", default="http://127.0.0.1:8827", help="隔离实例地址（默认 8827 端口）")
    parser.add_argument("--username", default="课堂验收", help="验收账号（不存在会自动注册）")
    parser.add_argument("--password", default="ui-pass-12345", help="验收账号密码")
    parser.add_argument("--fake-model-base-url", default="http://127.0.0.1:8899/v1",
                        help="本机假模型服务地址（默认 8899 端口）")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="截图与报告输出目录")
    parser.add_argument("--skip-classroom", action="store_true", help="只跑语音输入验收")
    parser.add_argument("--skip-asr", action="store_true", help="只跑课堂全链路验收")
    parser.add_argument("--skip-multiagent", action="store_true", help="跳过课堂内的多智能体讨论验收")
    args = parser.parse_args()

    global BASE, USERNAME, PASSWORD, FAKE_MODEL_BASE_URL, OUT
    BASE = args.base_url.rstrip("/")
    USERNAME = args.username
    PASSWORD = args.password
    FAKE_MODEL_BASE_URL = args.fake_model_base_url.rstrip("/")
    OUT = Path(args.out).expanduser()
    OUT.mkdir(parents=True, exist_ok=True)
    report["base_url"] = BASE

    try:
        bootstrap_account()
    except Exception as exc:  # noqa: BLE001
        report["problems"].append(f"验收账号/模型连接初始化失败：{type(exc).__name__}: {exc}")

    with sync_playwright() as playwright:
        browser = launch(playwright)
        try:
            course_id = report.get("course_id")
            if not args.skip_classroom:
                try:
                    course_id = desktop_flow(browser)
                    report["course_id"] = course_id
                except Exception as exc:  # noqa: BLE001
                    report["problems"].append(f"课堂生成/产出流程失败：{type(exc).__name__}: {exc}")
                    course_id = report.get("course_id")
                if course_id:
                    try:
                        mobile_flow(browser, course_id)
                    except Exception as exc:  # noqa: BLE001
                        report["problems"].append(f"移动端课堂流程失败：{type(exc).__name__}: {exc}")
                    if not args.skip_multiagent:
                        try:
                            multiagent_flow(browser, course_id)
                        except Exception as exc:  # noqa: BLE001
                            report["problems"].append(f"多智能体讨论流程失败：{type(exc).__name__}: {exc}")
                        try:
                            multiagent_mobile_flow(browser, course_id)
                        except Exception as exc:  # noqa: BLE001
                            report["problems"].append(f"移动端多智能体流程失败：{type(exc).__name__}: {exc}")
            if not args.skip_asr:
                try:
                    asr_supported_flow(browser, course_id)
                except Exception as exc:  # noqa: BLE001
                    report["problems"].append(f"语音输入（支持）流程失败：{type(exc).__name__}: {exc}")
                try:
                    asr_unsupported_flow(browser)
                except Exception as exc:  # noqa: BLE001
                    report["problems"].append(f"语音输入（不支持）流程失败：{type(exc).__name__}: {exc}")
                try:
                    asr_mobile_flows(browser)
                except Exception as exc:  # noqa: BLE001
                    report["problems"].append(f"移动端语音输入流程失败：{type(exc).__name__}: {exc}")
        finally:
            browser.close()
    report["ok"] = not report["problems"]
    (OUT / "classroom-ui-report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps({"ok": report["ok"], "screenshots": report["screenshots"],
                      "problems": report["problems"]}, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
