"""知序 · 前端主流程与修复项验收（Playwright + 本机 Edge）。

覆盖本轮前端修复的 P0/P1 项（与 scripts/ui_check.py、ui_check_pages.py 互补）：

  1. Markdown fail-closed 与两套净化配置（runtime 断言：XSS 剥离、DOMPurify 缺失降级 <pre>）；
  2. 公式渲染失败回退原卷图（mock：非法 LaTeX + fallback_asset，断言出现 <img> 而非 .katex-error）；
  3. 无答案/无解析时不渲染「查看答案 / 查看解析」按钮；
  4. 错题连续复习：提交后「进入下一道复习题」工具条保留，连续走通 3 题并看到本轮小结；
  5. 跨页带 qid 进入：目标题不在当前页结果中也单独打开（桌面 + 手机），当前页命中时高亮；
  6. 提交后 keepSelection：筛选（未做/待复习）把刚答的题刷出列表时不自动跳题；
  7. 题型/地区多选、回车搜索、列表摘要不露 [公式N]、选中态与阅读区同步；
  8. 知识清单：输出形态网格回到第 2 步主界面、打开生成的笔记不跳回 AI 表单、资料卡重建索引/失败页重试；
  9. 旧标签页横幅（mock X-Zhixu-Build 与启动 build_id 不一致）与真实响应头一致性；
 10. 工程项：路由切页关闭非全局 EventSource、busy() 不恢复已脱离文档按钮、登出/401 清空跨账号状态；
 11. 设置页：自定义厂商向导（地址规范化预览 / 120 个模型不截断 / 保存前 probe / 保存）；
 12. 设置页：默认模型行内编辑与 judge 清空（PUT body 断言 + 局部刷新不整页重渲染）；
 13. modelPicker：精确/快速模式与模型选择写回内存默认值，切下一题不回弹；
 14. 手机端 390/320：更多筛选/清空筛选可见、列表不被裁剪、多选面板不越界；
 15. 流式固定壳：取消按钮保持同一 DOM 节点且可点击、delta 增量追加、阶段进度百分比、
     已耗时计时与点击取消后的 POST；
 16. 笔记页 OCR 异步任务：mock async:true + SSE → 流式进度、完成刷新资料卡、失败页重试；
 17. 内容管理分区懒加载：初始只请求校勘接口、切 Tab 按需加载、操作后只局部刷新；
 18. 全局断言：收集 console.error 与 4xx/5xx（排除 401/403/409 门禁），出现即失败。

用法（先启动隔离实例）：
  python scripts/ui_check_flows.py --base-url http://127.0.0.1:8801 \
      --username ui-flow-check --password ui-flow-pass-123 --out screenshots/ui-audit
  # 新增 checks：streaming_shell_stable / ocr_job_progress / admin_lazy_tabs
  # 内容管理懒加载项使用首个管理员账号 ui-visual-check/ui-visual-pass-123。

脚本会在账号不存在时自动注册（本机模式）。退出码：任一检查失败或页面出现 JS 异常时为 1。
"""
import argparse
import base64
import json
from pathlib import Path
import re
import sys
import tempfile
import urllib.parse

from playwright.sync_api import sync_playwright

DEFAULT_OUT = Path(__file__).resolve().parents[1] / "screenshots" / "ui-audit"
MOBILE_VIEWPORT = {"width": 390, "height": 844}
DESKTOP_VIEWPORT = {"width": 1440, "height": 900}
MOBILE_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
             "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1")

# mock /api/bank-image 用的小公式图（120×36 深绿线条示意），让截图里能直接看到回退图片。
TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAHgAAAAkCAIAAADNSmkJAAAAr0lEQVR4nO3XIRKDMBBA0aXD2bBVvQCD7UVqkTA1SBQ2p+MEzUwFn0zyn0Uk81mx6T7bErpeHxHT8wWc1LL12B9336EVhoYYGmJoiKEhhoYYGmJo8MGSMbxH6iY1SPP31ycnuoyJzvwi/cWJhhgaYmiIoSGGhtS5R6fyliUnGuIeDXGiIYaGGBpiaIihIXXu0QXu4E40xD0a4kRDDA0xNMTQEENDDA2ud+uxU8dFs05kgxXZHz1MVAAAAABJRU5ErkJggg=="
)

FORMULA_FIXTURE = {
    "qid": "fixture-formula-fallback",
    "subject": "数学",
    "type": "single_choice",
    "type_label": "单选题",
    "year": 2026,
    "province": "上海",
    "number_label": "1",
    "paper_name": "公式回退测试卷",
    "paper_type": "test",
    "stem_plain": "若[公式1]，求值。",
    "stem_blocks": [
        {"kind": "text", "text": "若", "block_id": "b1", "order": 0},
        {"kind": "math", "latex": "\\frac{1}{", "block_id": "b1", "order": 1,
         "fallback_asset": {"file": "formula.png", "role": "formula", "permission": "public"}},
        {"kind": "text", "text": "，求值。", "block_id": "b1", "order": 2},
    ],
    "options": [{"label": "A", "text": "1"}, {"label": "B", "text": "2"}],
    "images": [],
    "objective": True,
    "has_answer": True,
    "has_solution": True,
    "unlocked": False,
    "quality_flags": [],
    "practice_allowed": True,
    "bank_version": "fixture",
    "practice_status": "practice",
    "grading_status": "auto",
    "group": None,
    "member_index": [],
    "shared_blocks": [],
    "latest_attempt": None,
    "attempts_history": [],
    "mastery": "未掌握",
    "knowledge_points": [],
    "methods": [],
    "has_ai_explanation": False,
    "wrong_info": {"in_wrongbook": False},
    "difficulty": "待标注",
    "difficulty_source": "",
}


class CheckFailure(AssertionError):
    pass


def fail(message: str):
    raise CheckFailure(message)


def assert_true(condition, message: str):
    if not condition:
        fail(message)


_FLOW_REPORT = None
_OBSERVED_PAGE_IDS = set()


def attach_observers(page, report) -> None:
    """收集 console.error、页面 JS 异常与 4xx/5xx；401/403/409 属于预期门禁，排除。"""
    if report is None or id(page) in _OBSERVED_PAGE_IDS:
        return
    _OBSERVED_PAGE_IDS.add(id(page))

    def on_console(message):
        if message.type != "error":
            return
        text = message.text or ""
        if re.search(r"\b(401|403|409)\b", text):
            return
        for allowed in report.get("allowed_console", []):
            if re.search(allowed, text):
                return
        report["console_errors"].append({"url": page.url[:200], "text": text[:300]})

    def on_response(response):
        status = response.status
        if status < 400 or status in (401, 403, 409):
            return
        url = response.url
        for pattern in report.get("allowed_http", []):
            if pattern in url:
                return
        report["http_errors"].append({"status": status, "url": url[:300]})

    def on_pageerror(error):
        report["page_errors"].append({"url": page.url[:200], "error": str(error)[:300]})

    page.on("console", on_console)
    page.on("response", on_response)
    page.on("pageerror", on_pageerror)


def observe_login_page(page) -> None:
    """ensure_login 内自动挂观察器（覆盖跨 context 新建的手机验收页）。"""
    if _FLOW_REPORT is not None:
        attach_observers(page, _FLOW_REPORT)


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


def attach_observers(page, report) -> None:
    """收集 console.error、页面 JS 异常与 4xx/5xx；401/403/409 属于预期门禁，排除。"""

    def on_console(message):
        if message.type != "error":
            return
        text = message.text or ""
        if re.search(r"\b(401|403|409)\b", text):
            return
        for allowed in report.get("allowed_console", []):
            if re.search(allowed, text):
                return
        report["console_errors"].append({"url": page.url[:200], "text": text[:300]})

    def on_response(response):
        status = response.status
        if status < 400 or status in (401, 403, 409):
            return
        url = response.url
        for pattern in report.get("allowed_http", []):
            if pattern in url:
                return
        report["http_errors"].append({"status": status, "url": url[:300]})

    def on_pageerror(error):
        report["page_errors"].append({"url": page.url[:200], "error": str(error)[:300]})

    page.on("console", on_console)
    page.on("response", on_response)
    page.on("pageerror", on_pageerror)



# ---------------------------------------------------------------------------
# 登录 / 页面辅助
# ---------------------------------------------------------------------------
def ensure_login(page, base_url: str, username: str, password: str) -> None:
    """登录；账号不存在时按本机模式注册（第一个账号为管理员，后续为学习者）。"""
    observe_login_page(page)
    page.goto(base_url + "/", wait_until="domcontentloaded")
    try:
        page.wait_for_selector(".layout, .page-heading", timeout=8000)
        return
    except Exception:
        pass
    page.wait_for_selector(".auth-form-card", timeout=20000)
    page.get_by_label("用户名").fill(username)
    page.get_by_label("密码").fill(password)
    page.get_by_role("button", name="登录学习工作台").click()
    try:
        page.wait_for_selector(".layout, .page-heading", timeout=15000)
        return
    except Exception:
        pass
    # 登录失败 → 尝试注册一次性账号（数据目录应为隔离库）。
    page.locator(".auth-switch .text-button").filter(has_text="创建账号").click()
    page.wait_for_selector(".auth-form-card", timeout=10000)
    page.get_by_label("用户名").fill(username)
    page.get_by_label("密码").fill(password)
    page.get_by_role("button", name="创建我的学习账号").click()
    page.wait_for_selector(".layout, .page-heading", timeout=25000)


def shot(page, out: Path, name: str, report: dict, full_page: bool = False) -> str:
    path = out / name
    page.screenshot(path=str(path), full_page=full_page)
    report["screenshots"].append(str(path))
    return str(path)


def page_hash(page) -> str:
    return page.evaluate("location.hash")


def hash_query(raw_hash: str) -> dict:
    """从 `#/practice?a=b&c=d` 形式的 hash 里解析查询参数（urlsplit 不解析 fragment 内问号）。"""
    parts = str(raw_hash or "").split("?", 1)
    if len(parts) != 2:
        return {}
    return urllib.parse.parse_qs(parts[1])


def goto_practice(page, base_url: str, query: str = ""):
    url = base_url + "/#/practice" + (("?" + query) if query else "")
    page.goto(url, wait_until="domcontentloaded")
    page.wait_for_selector(".practice-workspace", timeout=30000)


def wait_results(page, timeout: int = 30000):
    page.wait_for_selector(".result-card", timeout=timeout)
    page.wait_for_timeout(350)


def api_json(page, path: str, method: str = "GET", data=None):
    return page.evaluate(
        """async ({path, method, data}) => {
            const init = {method, credentials: 'same-origin',
                          headers: {'X-Requested-With': 'XMLHttpRequest'}};
            if (data !== null && data !== undefined) {
                init.headers['Content-Type'] = 'application/json';
                init.body = JSON.stringify(data);
            }
            const response = await fetch('/api' + path, init);
            let body = null;
            try { body = await response.json(); } catch (e) { body = null; }
            return {status: response.status, body};
        }""",
        {"path": path, "method": method, "data": data},
    )


def archive_all_wrongs(page):
    data = api_json(page, "/wrongbook?include_archived=false&page_size=200")
    items = ((data.get("body") or {}).get("items") or []) if data["status"] == 200 else []
    for item in items:
        qid = item.get("qid")
        if qid:
            api_json(page, "/wrongbook/" + urllib.parse.quote(qid, safe=""), method="DELETE")
    return len(items)


# ---------------------------------------------------------------------------
# 设置页 / 模型选择器验收用 mock 契约（后端未就绪时也能跑前端验收）
# ---------------------------------------------------------------------------
SETTINGS_PROVIDERS_FIXTURE = {
    "items": [
        {
            "id": 11, "name": "Mock 中转站", "kind": "openai",
            "base_url": "https://relay.example.com/v1", "key_hint": "已加密 · ****abcd",
            "has_api_key": True, "auth_mode": "bearer",
            "extra_header_names": [], "compat": {"timeout_seconds": 120, "max_tokens_param": "auto"},
            "models": ["model-a", "model-b"],
            "model_details": [
                {"id": 101, "model_id": "model-a", "display_name": "model-a",
                 "caps": {"vision": True, "json": True, "stream": True}, "enabled": True},
                {"id": 102, "model_id": "model-b", "display_name": "model-b",
                 "caps": {"vision": False, "json": True, "stream": True}, "enabled": True},
            ],
        },
        {
            "id": 12, "name": "第二连接", "kind": "openai",
            "base_url": "https://api.example.org/v1", "key_hint": "已加密 · ****efgh",
            "has_api_key": True, "auth_mode": "auto",
            "extra_header_names": [], "compat": {"timeout_seconds": 90},
            "models": ["model-c"],
            "model_details": [
                {"id": 103, "model_id": "model-c", "display_name": "model-c",
                 "caps": {"vision": True, "json": True, "stream": True}, "enabled": True},
            ],
        },
    ],
    "presets": [
        {"name": "OpenAI", "kind": "openai", "base_url": "https://api.openai.com/v1", "models": []},
        {"name": "DeepSeek", "kind": "openai", "base_url": "https://api.deepseek.com/v1", "models": []},
    ],
    "url_policy": "local_open",
    "public_mode": False,
}

PREVIEW_MODEL_COUNT = 120
SETTINGS_PREVIEW_MODELS = [
    {"model_id": f"relay-model-{index:03d}", "caps": {"vision": index % 2 == 0, "json": True}}
    for index in range(PREVIEW_MODEL_COUNT)
]

SETTINGS_EMBEDDING_FIXTURE = {
    "mode": "custom",
    "provider_id": None,
    "base_url": "https://embed.example.com/v1",
    "model": "text-embedding-3-small",
    "has_api_key": True,
    "key_hint": "已加密",
    "env_configured": False,
    "effective_source": "custom",
    "effective": {
        "base_url": "https://embed.example.com/v1",
        "model": "text-embedding-3-small",
        "endpoint": "https://embed.example.com/v1/embeddings",
        "ready": True,
    },
    "compatible_providers": [
        {"id": 11, "name": "Mock 中转站", "kind": "openai", "models": ["model-a", "text-embedding-3-small"]},
    ],
}


def model_value(provider_id: int, model: str) -> str:
    """前端 select 的 value 是 JSON.stringify([provider_id, model])（无空格）。"""
    return json.dumps([provider_id, model], separators=(",", ":"))


def wait_task_default_put(page, timeout: int = 15000):
    return page.expect_response(
        lambda response: "/api/ai/task-defaults" in response.url and response.request.method == "PUT",
        timeout=timeout,
    )



# ---------------------------------------------------------------------------
# 各检查项
# ---------------------------------------------------------------------------
def check_markdown_sanitization(page, out, report) -> dict:
    # 净化用例故意包含 <img src=x>，浏览器仍会尝试请求 /x（404），属预期噪音；
    # 真实 404 仍会被 response 观察器的 4xx 断言捕获，这里只放行 console 文本。
    report.setdefault("allowed_http", []).append("/x")
    report.setdefault("allowed_console", []).extend([
        r"Content Security Policy|inline event handler",
        r"status of 404",
    ])
    detail = page.evaluate(
        """async () => {
            const md = await import('/static/js/core/markdown.js');
            const out = {};
            const saved = window.DOMPurify;
            try {
                const dirty = md.markdown(
                    '<div style="position:fixed;inset:0;z-index:9999">x</div>' +
                    '<details open><summary>答案</summary>正文</details>' +
                    '<script>window.__xss = 1<\/script>' +
                    '<img src=x onerror="window.__xss = 2">');
                out.stripped_style = !dirty.querySelector('[style]');
                out.kept_details = Boolean(dirty.querySelector('details > summary'));
                out.no_script = dirty.querySelectorAll('script').length === 0;
                out.no_inline_handler = [...dirty.querySelectorAll('*')]
                    .every(n => ![...n.attributes].some(a => a.name.toLowerCase().startsWith('on')));
                out.xss_flag = window.__xss || null;
                out.plain_text_present = dirty.textContent.includes('答案');

                // DOMPurify 缺失：必须 fail-closed 为纯文本 <pre>，而不是 innerHTML 直出。
                window.DOMPurify = undefined;
                const failed = md.markdown('**粗体** <img src=x onerror=alert(1)> <b>标签</b>');
                out.failclosed_pre = Boolean(failed.querySelector('pre.markdown-fallback'));
                out.failclosed_no_raw = failed.querySelectorAll('img, b').length === 0;
                out.failclosed_text = failed.textContent.includes('<img');
            } finally {
                window.DOMPurify = saved;
            }
            // 两套白名单分离：MathML 才允许 math 标签/style，用户 Markdown 禁止。
            out.math_allows_math = md.MATH_SANITIZE_OPTIONS.ADD_TAGS.includes('math');
            out.math_allows_style = md.MATH_SANITIZE_OPTIONS.ADD_ATTR.includes('style');
            out.markdown_forbids_math = md.MARKDOWN_SANITIZE_OPTIONS.FORBID_TAGS.includes('math');
            out.markdown_forbids_style = md.MARKDOWN_SANITIZE_OPTIONS.FORBID_ATTR.includes('style');
            // 正常 Markdown 与公式仍然渲染，不因净化而丢内容。
            const normal = md.markdown('## 小节\\n\\n行内 $x^2$ 与独立 $$y=1$$，以及 <details><summary>答案</summary>42</details>');
            out.normal_heading = Boolean(normal.querySelector('h2'));
            out.normal_details = Boolean(normal.querySelector('details > summary'));
            out.normal_formula = normal.querySelectorAll('.katex, math').length > 0;
            return out;
        }"""
    )
    problems = [
        key for key in ("stripped_style", "kept_details", "no_script", "no_inline_handler",
                        "failclosed_pre", "failclosed_no_raw", "failclosed_text",
                        "math_allows_math", "math_allows_style", "markdown_forbids_math",
                        "markdown_forbids_style", "normal_heading", "normal_details", "normal_formula")
        if not detail.get(key)
    ]
    assert_true(not problems, f"Markdown 净化未达预期：{problems}")
    assert_true(detail.get("xss_flag") is None, "Markdown 渲染触发了注入内容")
    return detail


def check_job_stream_cleanup(page, out, report) -> dict:
    detail = page.evaluate(
        """async () => {
            const jobs = await import('/static/js/core/jobs.js');
            const st = await import('/static/js/core/state.js');
            let closedDirect = 0;
            const fake = {close() { closedDirect += 1; }};
            st.state.activeEventSources.add(fake);
            jobs.closeJobStreams();
            const afterDirect = {closed: closedDirect, size: st.state.activeEventSources.size};
            let closedRoute = 0;
            const fake2 = {close() { closedRoute += 1; }};
            st.state.activeEventSources.add(fake2);
            location.hash = '#/wrongbook';
            await new Promise(resolve => setTimeout(resolve, 1200));
            return {afterDirect, closedRoute, sizeAfterRoute: st.state.activeEventSources.size};
        }"""
    )
    assert_true(detail["afterDirect"]["closed"] == 1 and detail["afterDirect"]["size"] == 0,
                f"closeJobStreams 未清理直连 EventSource：{detail['afterDirect']}")
    assert_true(detail["closedRoute"] == 1 and detail["sizeAfterRoute"] == 0,
                f"路由切换未关闭旧 EventSource：closed={detail['closedRoute']} size={detail['sizeAfterRoute']}")
    page.wait_for_selector(".pg-filter-bar", timeout=20000)
    return detail


def check_busy_button_state(page, out, report) -> dict:
    detail = page.evaluate(
        """async () => {
            const dom = await import('/static/js/core/dom.js');
            const connected = document.createElement('button');
            document.body.append(connected);
            await dom.busy(connected, async () => {});
            const connectedRestored = connected.disabled === false && !connected.hasAttribute('aria-busy');
            const detached = document.createElement('button');
            document.body.append(detached);
            await dom.busy(detached, async () => { detached.remove(); });
            const detachedKept = detached.disabled === true;
            connected.remove();
            return {connectedRestored, detachedKept};
        }"""
    )
    assert_true(detail["connectedRestored"], "busy() 结束后未恢复仍在页面上的按钮")
    assert_true(detail["detachedKept"], "busy() 仍会改写已脱离文档的旧按钮状态")
    return detail


def check_build_banner(context, base_url: str, out, report) -> dict:
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    try:
        page.goto(base_url + "/", wait_until="domcontentloaded")
        page.wait_for_selector(".layout, .page-heading", timeout=25000)
        # 真实响应头与 /api/health/live 的 build_id 一致。
        real = page.evaluate(
            """async () => {
                const [stats, live] = await Promise.all([
                    fetch('/api/stats', {credentials: 'same-origin'}),
                    fetch('/api/health/live', {credentials: 'same-origin'}).then(r => r.json()),
                ]);
                return {header: stats.headers.get('X-Zhixu-Build'), build: live.build_id};
            }"""
        )
        assert_true(bool(real["header"]), "API 响应缺少 X-Zhixu-Build 头")
        assert_true(real["header"] == real["build"],
                    f"X-Zhixu-Build({real['header']}) 与 /api/health/live build_id({real['build']}) 不一致")

        # Mock：服务端构建号变化，页面应出现可关闭的「应用已更新」横幅。
        def stats_handler(route):
            response = route.fetch()
            headers = dict(response.headers)
            headers["x-zhixu-build"] = "mock-stale-build"
            route.fulfill(response=response, headers=headers)

        pattern = re.compile(r".*/api/stats(\?.*)?$")
        page.route(pattern, stats_handler)
        page.goto(base_url + "/#/", wait_until="domcontentloaded")
        page.wait_for_selector(".build-stale-banner", timeout=20000)
        page.wait_for_timeout(400)
        assert_true(page.locator(".build-stale-banner").is_visible(), "旧构建提示条不可见")
        text = page.locator(".build-stale-banner").inner_text()
        assert_true("应用已更新" in text and "刷新" in text, f"旧构建提示文案不符：{text}")
        banner_path = shot(page, out, "flows-build-banner.png", report)
        page.get_by_role("button", name="关闭更新提示").click()
        page.wait_for_timeout(300)
        assert_true(page.locator(".build-stale-banner").count() == 0, "旧构建提示无法关闭")
        # 关闭后仍可正常进入练习页作答，不被横幅阻塞。
        page.goto(base_url + "/#/practice?sort=year_desc", wait_until="domcontentloaded")
        wait_results(page)
        assert_true(page.locator(".result-card").count() > 0, "出现更新提示后练习页无法加载")
        page.unroute(pattern)

        # 登出/401 清理：跨账号缓存与页面视图状态必须同步失效。
        reset_state = page.evaluate(
            """async () => {
                const st = await import('/static/js/core/state.js');
                st.state.practiceView = 'question';
                st.state.resultsScrollTop = 240;
                st.state.filters = {q: '函数'};
                st.state.taskDefaults = {explain: {primary_model: 'x'}};
                st.state.providers = [{id: 1, name: '旧账号连接'}];
                st.state.noteTab = 'official';
                st.state.seenReviewQids.add('q-old');
                st.clearUserSession();
                return {
                    user: st.state.user,
                    practiceView: st.state.practiceView,
                    resultsScrollTop: st.state.resultsScrollTop,
                    filters: st.state.filters,
                    taskDefaults: st.state.taskDefaults,
                    providers: st.state.providers,
                    noteTab: st.state.noteTab,
                    seen: st.state.seenReviewQids.size,
                    catalog: st.state.catalog,
                };
            }"""
        )
        assert_true(reset_state["user"] is None and reset_state["practiceView"] is None,
                    f"clearUserSession 未清空 user/practiceView：{reset_state}")
        assert_true(reset_state["filters"] == {} and reset_state["taskDefaults"] == {}
                    and reset_state["providers"] == [] and reset_state["noteTab"] == "all"
                    and reset_state["seen"] == 0 and reset_state["catalog"] is None,
                    f"clearUserSession 未清空跨账号残存状态：{reset_state}")

        assert_true(not errors, f"旧构建横幅检查出现 JS 异常：{errors[:2]}")
        return {"real_header": real["header"], "banner_text": text,
                "reset_state": reset_state, "banner_screenshot": banner_path}
    finally:
        page.close()


def check_formula_fallback(context, base_url: str, out, report) -> dict:
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))

    def question_handler(route):
        route.fulfill(status=200, content_type="application/json",
                      body=json.dumps(FORMULA_FIXTURE, ensure_ascii=False))

    def image_handler(route):
        route.fulfill(status=200, content_type="image/png", body=TINY_PNG)

    q_pattern = re.compile(r".*/api/questions/fixture-formula-fallback$")
    img_pattern = re.compile(r".*/api/bank-image(\?.*)?$")
    try:
        page.route(q_pattern, question_handler)
        page.route(img_pattern, image_handler)
        page.goto(base_url + "/#/practice?qid=fixture-formula-fallback", wait_until="domcontentloaded")
        page.wait_for_selector("#question-detail .reading-title-row", timeout=30000)
        page.wait_for_timeout(700)
        detail = {
            "asset_images": page.locator("#question-detail .block-math-asset img").count(),
            "katex_errors": page.locator("#question-detail .katex-error").count(),
            "image_errors": page.locator("#question-detail .image-error").count(),
            "fallback_blocks": page.locator("#question-detail .block-math-asset").count(),
        }
        assert_true(detail["asset_images"] >= 1, "非法 LaTeX 未回退到原卷公式图（缺少 <img>）")
        assert_true(detail["katex_errors"] == 0, "页面仍渲染 .katex-error 红色错误公式")
        detail["screenshot"] = shot(page, out, "flows-formula-fallback.png", report)
        assert_true(not errors, f"公式回退检查出现 JS 异常：{errors[:2]}")
        return detail
    finally:
        page.unroute(q_pattern)
        page.unroute(img_pattern)
        page.close()


def check_cross_page_qid(context, base_url: str, out, report) -> dict:
    page = context.new_page()
    try:
        page.goto(base_url + "/", wait_until="domcontentloaded")
        page.wait_for_selector(".layout, .page-heading", timeout=25000)
        data = page.evaluate(
            """async () => {
                const subject = '化学';
                const load = async page => (await fetch(
                    '/api/questions?subject=' + encodeURIComponent(subject) +
                    '&page=' + page + '&page_size=12', {credentials: 'same-origin'})).json();
                const [first, second] = await Promise.all([load(1), load(2)]);
                const firstIds = new Set((first.items || []).map(item => item.qid));
                const target = (second.items || []).find(item => !firstIds.has(item.qid));
                return {subject, target: target || null, page1: (first.items || []).map(i => i.qid)};
            }"""
        )
        target = data.get("target")
        assert_true(bool(target), "题库第 2 页没有可用于跨页跳题的题目")
        encoded = urllib.parse.quote(target["qid"], safe="")
        url = (base_url + "/#/practice?subject=" + urllib.parse.quote(data["subject"]) + "&qid=" + encoded)

        # 桌面：不在当前页结果中的 qid 也必须单独打开。
        page.goto(url, wait_until="domcontentloaded")
        page.wait_for_selector("#question-detail .reading-title-row", timeout=30000)
        page.wait_for_timeout(600)
        title = page.locator("#question-detail .reading-title-row h2").inner_text().strip()
        expected = f"原卷第{target.get('number_label')}题" if target.get("number_label") else ""
        detail = {
            "qid": target["qid"],
            "title": title,
            "expected_title": expected,
            "error_panels": page.locator("#question-detail .error-panel").count(),
            "selected_cards": page.locator(".result-card.selected").count(),
            "help_notice": page.locator("#question-detail p.help").first.inner_text()
            if page.locator("#question-detail p.help").count() else "",
        }
        assert_true(detail["error_panels"] == 0, "跨页打开目标 qid 时阅读区报错")
        assert_true(not expected or expected in title, f"阅读区未打开目标题：期望 {expected}，实际 {title}")
        assert_true(detail["selected_cards"] == 0, "目标题不在当前页时不应错误高亮某张卡片")
        detail["screenshot"] = shot(page, out, "flows-cross-page-desktop.png", report)

        # 当前页命中时高亮选中。
        hit_qid = (data.get("page1") or [None])[0]
        if hit_qid:
            page.goto(base_url + "/#/practice?subject=" + urllib.parse.quote(data["subject"]) +
                      "&qid=" + urllib.parse.quote(hit_qid, safe=""), wait_until="domcontentloaded")
            page.wait_for_selector("#question-detail .reading-title-row", timeout=30000)
            page.wait_for_timeout(600)
            selected = page.locator(".result-card.selected")
            hit = {
                "hit_qid": hit_qid,
                "selected_count": selected.count(),
                "selected_qid": selected.first.get_attribute("data-qid") if selected.count() else None,
            }
            assert_true(hit["selected_count"] == 1 and hit["selected_qid"] == hit_qid,
                        f"当前页命中时未正确高亮：{hit}")
            detail["in_page"] = hit

        # 手机：带 qid 进入必须切到 question 视图并显示目标题。
        mobile = context.browser.new_context(viewport=MOBILE_VIEWPORT, locale="zh-CN",
                                             is_mobile=True, has_touch=True, user_agent=MOBILE_UA)
        page_m = mobile.new_page()
        try:
            ensure_login(page_m, base_url, USERNAME, PASSWORD)
            page_m.goto(url, wait_until="domcontentloaded")
            page_m.wait_for_selector(".practice-workspace[data-view='question']", timeout=30000)
            page_m.wait_for_selector(".reading-title-row", timeout=30000)
            page_m.wait_for_timeout(500)
            mobile_title = page_m.locator(".reading-title-row h2").inner_text().strip()
            assert_true(not expected or expected in mobile_title,
                        f"手机跨页进入未打开目标题：期望 {expected}，实际 {mobile_title}")
            detail["mobile_title"] = mobile_title
            detail["mobile_screenshot"] = shot(page_m, out, "flows-cross-page-mobile.png", report)
        finally:
            mobile.close()
        return detail
    finally:
        page.close()


def check_continuous_review(context, base_url: str, out, report) -> dict:
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    try:
        ensure_login(page, base_url, USERNAME, PASSWORD)
        archived = archive_all_wrongs(page)
        seeded = page.evaluate(
            """async () => {
                const response = await fetch('/api/questions?subject=' + encodeURIComponent('化学') +
                    '&type=single_choice&status=unattempted&page_size=10', {credentials: 'same-origin'});
                const data = await response.json();
                const ids = [];
                for (const item of (data.items || []).slice(0, 3)) {
                    const saved = await fetch('/api/wrongbook', {
                        method: 'POST', credentials: 'same-origin',
                        headers: {'Content-Type': 'application/json', 'X-Requested-With': 'XMLHttpRequest'},
                        body: JSON.stringify({qid: item.qid}),
                    });
                    if (saved.status === 200) ids.push(item.qid);
                }
                const session = await (await fetch('/api/wrongbook/session?limit=20',
                    {credentials: 'same-origin'})).json();
                return {ids, sessionTotal: session.total, sessionQids: (session.items || []).map(i => i.qid)};
            }"""
        )
        assert_true(len(seeded["ids"]) == 3, f"错题种子不足 3 道：{seeded}")
        assert_true(seeded["sessionTotal"] >= 3, f"连续复习会话不足 3 题：{seeded}")

        page.goto(base_url + "/#/wrongbook", wait_until="domcontentloaded")
        page.wait_for_selector(".pg-filter-bar", timeout=25000)
        page.wait_for_timeout(400)
        page.get_by_role("button", name="开始连续复习").click()
        page.wait_for_selector(".pg-session-card--active .reading-title-row", timeout=25000)
        page.wait_for_timeout(500)

        rounds = []
        summary_text = ""
        for index in range(3):
            card = page.locator(".pg-session-card--active")
            title = card.locator(".reading-title-row h2").inner_text().strip()
            choices = card.locator(".choice input")
            assert_true(choices.count() > 0, f"连续复习第 {index + 1} 题没有可选项，无法提交")
            choices.first.check(force=True)
            card.locator('button[form="answer-form"]').click()
            page.wait_for_timeout(1700)
            tools = page.locator(".pg-session-tools .button")
            assert_true(tools.count() >= 1,
                        f"连续复习第 {index + 1} 题提交后「下一题」工具条消失（P0 回归）")
            tool_text = tools.first.inner_text().strip()
            rounds.append({"round": index + 1, "title": title, "next_text": tool_text})
            if index == 0:
                shot(page, out, "flows-review-next-bar.png", report)
            tools.first.click()
            page.wait_for_timeout(1300)
            if "小结" in tool_text:
                page.wait_for_selector(".pg-session-card", timeout=10000)
                summary_text = page.locator(".pg-session-card").inner_text()
                break
        assert_true("本轮连续复习完成" in summary_text,
                    f"连续复习 3 题后未出现本轮小结：{summary_text[:120]}")
        detail = {"archived_before": archived, "seeded": seeded, "rounds": rounds,
                  "summary": summary_text[:200]}
        detail["summary_screenshot"] = shot(page, out, "flows-review-summary.png", report)
        assert_true(not errors, f"连续复习出现 JS 异常：{errors[:2]}")
        return detail
    finally:
        page.close()


def check_submit_keep_selection(context, base_url: str, out, report) -> dict:
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    try:
        ensure_login(page, base_url, USERNAME, PASSWORD)
        listing = page.evaluate(
            """async () => {
                const response = await fetch('/api/questions?subject=' + encodeURIComponent('化学') +
                    '&type=single_choice&status=unattempted&page_size=10', {credentials: 'same-origin'});
                return await response.json();
            }"""
        )
        items = listing.get("items") or []
        assert_true(bool(items), "没有可用于「提交后不跳题」检查的未做题")
        target = items[0]
        page.goto(base_url + "/#/practice?subject=" + urllib.parse.quote("化学") +
                  "&type=single_choice&status=unattempted", wait_until="domcontentloaded")
        wait_results(page)
        page.locator(f".result-card[data-qid='{target['qid']}']").first.click()
        page.wait_for_selector("#question-detail .reading-title-row", timeout=25000)
        page.wait_for_timeout(500)
        before = page.locator("#question-detail .reading-title-row h2").inner_text().strip()
        before_buttons = page.locator(".reading-actions-group .button").all_inner_texts()
        assert_true(page.locator(".choice input").count() > 0, "目标题没有选项，无法提交")
        page.locator(".choice input").first.check(force=True)
        page.locator(".reading-actions-group .button").filter(has_text=re.compile(r"提交")).last.click()
        page.wait_for_timeout(2600)
        after = page.locator("#question-detail .reading-title-row h2").inner_text().strip()
        after_buttons = page.locator(".reading-actions-group .button").all_inner_texts()
        current_hash = page_hash(page)
        detail = {
            "qid": target["qid"],
            "title_before": before,
            "title_after": after,
            "buttons_before": before_buttons,
            "buttons_after": after_buttons,
            "hash": current_hash,
            "has_answer": target.get("has_answer"),
            "has_solution": target.get("has_solution"),
        }
        assert_true(before == after, f"提交后阅读区自动跳题：{before} → {after}")
        assert_true(any("提交" in text for text in after_buttons),
                    f"提交后操作条丢失提交/查看按钮：{after_buttons}")
        encoded_qid = urllib.parse.quote(target["qid"], safe="")
        assert_true(encoded_qid in current_hash or target["qid"] in current_hash,
                    f"提交后 hash 未保留当前题 qid：{current_hash}")
        if not target.get("has_answer"):
            assert_true(not any("查看答案" in text for text in before_buttons + after_buttons),
                        "has_answer=false 时仍渲染「查看答案」按钮")
        if not target.get("has_solution"):
            assert_true(not any("查看解析" in text for text in before_buttons + after_buttons),
                        "has_solution=false 时仍渲染「查看解析」按钮")
        detail["screenshot"] = shot(page, out, "flows-submit-keep-selection.png", report)
        assert_true(not errors, f"提交后不跳题检查出现 JS 异常：{errors[:2]}")
        return detail
    finally:
        page.close()


def check_hidden_reveal_buttons(context, base_url: str, out, report) -> dict:
    page = context.new_page()
    try:
        ensure_login(page, base_url, USERNAME, PASSWORD)
        page.goto(base_url + "/#/practice?has_answer=false&has_solution=false&sort=year_desc",
                  wait_until="domcontentloaded")
        wait_results(page)
        page.locator(".result-card").first.click()
        page.wait_for_selector("#question-detail .reading-title-row", timeout=25000)
        page.wait_for_timeout(600)
        buttons = page.locator(".reading-actions-group .button")
        texts = buttons.all_inner_texts()
        detail = {
            "buttons": texts,
            "answer_button": sum(1 for text in texts if "查看答案" in text),
            "solution_button": sum(1 for text in texts if "查看解析" in text),
            "empty_group": buttons.count() == 0,
        }
        assert_true(detail["answer_button"] == 0, "has_answer=false 时仍渲染「查看答案」按钮")
        assert_true(detail["solution_button"] == 0, "has_solution=false 时仍渲染「查看解析」按钮")
        assert_true(not detail["empty_group"], "操作组为空，提交入口丢失")
        detail["screenshot"] = shot(page, out, "flows-hidden-reveal-buttons.png", report)
        return detail
    finally:
        page.close()


def check_multi_filter_and_summary(context, base_url: str, out, report) -> dict:
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    try:
        ensure_login(page, base_url, USERNAME, PASSWORD)
        goto_practice(page, base_url, "sort=year_desc")
        wait_results(page)

        # 题型多选：勾选两个题型后 hash 中应出现逗号多值。
        page.locator(".filter-compact .multi-select summary").first.click()
        options = page.locator(".filter-compact .multi-select-panel .multi-select-option")
        assert_true(options.count() >= 2, "题型多选没有可选项")
        picked = []
        for index in range(2):
            checkbox = options.nth(index).locator("input")
            picked.append(checkbox.get_attribute("value"))
            checkbox.check()
            page.wait_for_timeout(500)
        page.wait_for_timeout(1400)
        type_hash = page_hash(page)
        type_param = hash_query(type_hash).get("type", [""])[0]
        assert_true("," in type_param, f"题型多选未写入逗号多值：{type_hash}")
        assert_true(set(type_param.split(",")) == set(picked),
                    f"题型多选值与预期不一致：{type_param} vs {picked}")
        detail = {"type_param": type_param}

        # 地区多选（更多筛选内）。
        page.locator(".filter-toggle").click()
        page.wait_for_timeout(300)
        more = page.locator(".filter-more .multi-select").first
        assert_true(more.count() > 0, "更多筛选里缺少地区多选")
        more.locator("summary").click()
        page.wait_for_timeout(300)
        province_options = more.locator(".multi-select-option")
        assert_true(province_options.count() >= 2, "地区多选没有可选项")
        provinces = []
        for index in range(2):
            checkbox = province_options.nth(index).locator("input")
            provinces.append(checkbox.get_attribute("value"))
            checkbox.check()
            page.wait_for_timeout(500)
        page.wait_for_timeout(1400)
        province_hash = page_hash(page)
        province_param = hash_query(province_hash).get("province", [""])[0]
        assert_true("," in province_param, f"地区多选未写入逗号多值：{province_hash}")
        detail["province_param"] = province_param

        # 回车搜索：无需点击搜索按钮。
        page.locator(".filter-toggle").click()  # 收起更多筛选
        page.wait_for_timeout(200)
        keyword_input = page.locator(".search-wrap input").first
        keyword_input.fill("函数")
        keyword_input.press("Enter")
        page.wait_for_timeout(2000)
        wait_results(page)
        search_hash = page_hash(page)
        assert_true("q=%E5%87%BD%E6%95%B0" in search_hash or "q=函数" in search_hash,
                    f"回车未触发搜索：{search_hash}")
        count_text = page.locator(".results-count").inner_text()
        assert_true("找到" in count_text, f"搜索结果计数未刷新：{count_text}")
        detail["search_hash"] = search_hash
        detail["result_count"] = count_text

        # 列表摘要不直接暴露 [公式N] / [图N]。
        listing = page.evaluate(
            """async () => {
                const response = await fetch('/api/questions?subject=' + encodeURIComponent('化学') +
                    '&sort=year_desc&page_size=12', {credentials: 'same-origin'});
                return await response.json();
            }"""
        )
        raw_items = [item for item in (listing.get("items") or [])
                     if "[公式" in (item.get("stem_plain") or "") or "[图" in (item.get("stem_plain") or "")]
        page.goto(base_url + "/#/practice?subject=" + urllib.parse.quote("化学") + "&sort=year_desc",
                  wait_until="domcontentloaded")
        wait_results(page)
        checked = []
        leaked = []
        for item in raw_items[:4]:
            card = page.locator(f".result-card[data-qid='{item['qid']}']")
            if not card.count():
                continue
            text = card.first.locator(".result-card-title").inner_text()
            checked.append(text[:80])
            if "[公式" in text or "[图" in text:
                leaked.append(text[:80])
        assert_true(not leaked, f"列表摘要仍暴露占位符：{leaked}")
        assert_true(bool(checked), "列表页没有找到可验证摘要占位符的卡片")
        detail["summary_samples"] = checked

        # 选中态与阅读区同步：点击卡片后高亮 + hash 带 qid。
        first_card = page.locator(".result-card").first
        first_qid = first_card.get_attribute("data-qid")
        first_card.click()
        page.wait_for_selector("#question-detail .reading-title-row", timeout=25000)
        page.wait_for_timeout(500)
        selected = page.locator(".result-card.selected")
        assert_true(selected.count() == 1 and selected.first.get_attribute("data-qid") == first_qid,
                    "点击结果卡后 selectedQid 与阅读区不同步")
        assert_true(urllib.parse.quote(first_qid, safe="") in page_hash(page) or first_qid in page_hash(page),
                    f"选中题未写入 hash：{page_hash(page)}")
        detail["screenshot"] = shot(page, out, "flows-multi-filter.png", report)
        assert_true(not errors, f"多选/搜索检查出现 JS 异常：{errors[:2]}")
        return detail
    finally:
        page.close()


def check_notes_composer_and_notes_state(context, base_url: str, out, report) -> dict:
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))

    # 资料库 mock：一份 index_status=stale 且含失败页的扫描 PDF。
    state = {"stale": True}

    def document_payload():
        return {
            "id": 77001,
            "title": "验收扫描讲义.pdf",
            "pages": 3,
            "status": "partial",
            "doc_version": 2,
            "ocr_range": {"page_from": 1, "page_to": 3},
            "coverage": {
                "pages": 3, "page_count": 3, "covered_pages": [1],
                "missing_pages": [2, 3], "pending_pages": [2, 3], "failed_pages": [2],
                "reviewed_pages": [], "covered_count": 1, "complete": False,
                "next_page_from": 2, "next_page_to": 3,
            },
            "created_at": "2026-11-20T10:00:00",
            "chunks": 2,
            "headings": ["测试章节"],
            "text_excerpt": "测试讲义文本",
            "index_status": "stale" if state["stale"] else "ready",
            "index_digest": None,
            "index_updated_at": None,
            "index_enabled": True,
            "notice": "",
        }

    documents_pattern = re.compile(r".*/api/documents$")
    reindex_pattern = re.compile(r".*/api/documents/77001/reindex$")

    def documents_handler(route):
        if route.request.method != "GET":
            route.continue_()
            return
        route.fulfill(status=200, content_type="application/json",
                      body=json.dumps({"items": [document_payload()], "embedding_enabled": True},
                                      ensure_ascii=False))

    def reindex_handler(route):
        state["stale"] = False
        route.fulfill(status=200, content_type="application/json",
                      body=json.dumps({"document": document_payload(), "index_status": "ready"},
                                      ensure_ascii=False))

    try:
        ensure_login(page, base_url, USERNAME, PASSWORD)
        page.route(documents_pattern, documents_handler)
        page.route(reindex_pattern, reindex_handler)
        page.goto(base_url + "/#/notes", wait_until="domcontentloaded")
        page.wait_for_selector("#notes-panel-notes", timeout=30000)
        page.get_by_role("tab", name="AI 整理").click()
        page.wait_for_timeout(500)
        step_two = page.locator("#notes-panel-ai .pg-step").nth(1)
        detail = {
            "steps": page.locator("#notes-panel-ai .pg-step").count(),
            "format_grid_in_step2": step_two.locator(".prompt-grid-dims").count(),
            "format_grid_visible": step_two.locator(".prompt-grid-dims").is_visible()
            if step_two.locator(".prompt-grid-dims").count() else False,
            "requirements_visible": step_two.locator("textarea").count() > 0,
            "advanced_in_step3": page.locator("#notes-panel-ai .pg-step").nth(2)
            .locator("details.pg-advanced").count(),
            "advanced_model_picker": page.locator("#notes-panel-ai .pg-advanced .model-picker").count(),
            "advanced_collapsed": page.locator("#notes-panel-ai .pg-advanced").first.get_attribute("open") is None,
        }
        assert_true(detail["steps"] == 3, "AI 整理不是三步布局")
        assert_true(detail["format_grid_in_step2"] == 1 and detail["format_grid_visible"],
                    "方格选择（输出形态）没有回到第 2 步主界面可见区域")
        assert_true(detail["advanced_in_step3"] == 1 and detail["advanced_model_picker"] == 1,
                    "第 3 步缺少模型/核验等高级设置折叠")
        assert_true(detail["advanced_collapsed"], "高级设置默认展开，技术参数未折叠")
        detail["step2_screenshot"] = shot(page, out, "flows-notes-ai-step2.png", report)
        # 源码级：模板强压映射已删除，format 直接决定 template。
        source = page.evaluate(
            """async () => (await fetch('/static/js/pages/notes.js')).text()"""
        )
        assert_true("template: cfg.format" in source, "notes.js 未直接把所选格式作为 template")
        assert_true("includes(cfg.format)" not in source, "notes.js 仍存在把 6 种形态强压 checklist 的映射")

        # 「打开生成的笔记」跳转：选中笔记 + 残留 noteJobId 时不应跳回 AI 表单。
        created = api_json(page, "/notes", method="POST",
                           data={"title": "验收笔记 · 打开跳转", "content": "## 小节\n\n正文内容"})
        assert_true(created["status"] == 200, f"创建验收笔记失败：{created}")
        note_id = (created["body"] or {}).get("id")
        page.goto(base_url + "/#/practice?sort=year_desc", wait_until="domcontentloaded")
        wait_results(page)
        # 残留的假 job id 会触发一次不存在的 SSE 流（422），本项正是为了验证它不会把
        # 页面卡在 AI 表单，属于预期噪音。
        report.setdefault("allowed_http", []).append("/api/ai/jobs/stale-job-after-completion")
        report.setdefault("allowed_console", []).append(r"status of 422")
        page.evaluate(
            """async ({noteId}) => {
                const st = await import('/static/js/core/state.js');
                st.state.noteJobId = 'stale-job-after-completion';
                st.state.selectedNote = noteId;
                location.hash = '#/notes';
            }""",
            {"noteId": note_id},
        )
        page.wait_for_selector("#notes-panel-notes .pg-note-detail h2", timeout=30000)
        page.wait_for_timeout(600)
        detail["opened_note_visible"] = page.locator("#notes-panel-notes").is_visible()
        detail["ai_panel_hidden"] = page.locator("#notes-panel-ai").is_hidden()
        note_text = page.locator("#notes-panel-notes").inner_text()
        assert_true(detail["opened_note_visible"] and detail["ai_panel_hidden"],
                    "打开生成的笔记时仍停留在 AI 整理分区（noteJobId/noteSection 残留）")
        assert_true("验收笔记 · 打开跳转" in note_text, "笔记详情没有打开被选中的笔记")

        # 资料库：stale 索引出现「重建索引」，失败页可重试并打开 OCR 对话框。
        page.get_by_role("tab", name="资料库").click()
        page.wait_for_timeout(700)
        docs_text = page.locator("#notes-panel-docs").inner_text()
        detail["reindex_button"] = page.locator("#notes-panel-docs button:has-text('重建索引')").count()
        detail["failed_hint"] = "识别失败" in docs_text and "重试第 2 页" in docs_text
        assert_true(detail["reindex_button"] == 1, "index_status=stale 时资料卡未显示「重建索引」按钮")
        assert_true(detail["failed_hint"], f"failed_pages 未提示可重试失败页：{docs_text[:200]}")
        detail["docs_screenshot"] = shot(page, out, "flows-notes-docs.png", report)
        page.locator("#notes-panel-docs button:has-text('重试第 2 页')").first.click()
        page.wait_for_selector("dialog.modal-dialog", timeout=10000)
        detail["ocr_dialog"] = page.locator("dialog.modal-dialog").is_visible()
        assert_true(detail["ocr_dialog"], "重试失败页没有打开 OCR 对话框")
        page.get_by_role("button", name="关闭").first.click()
        page.wait_for_timeout(400)
        page.locator("#notes-panel-docs button:has-text('重建索引')").first.click()
        page.wait_for_timeout(1200)
        detail["reindex_cleared"] = page.locator("#notes-panel-docs button:has-text('重建索引')").count()
        assert_true(detail["reindex_cleared"] == 0, "重建索引成功后资料卡未刷新（按钮仍在）")
        assert_true(not errors, f"知识清单检查出现 JS 异常：{errors[:2]}")
        return detail
    finally:
        page.unroute(documents_pattern)
        page.unroute(reindex_pattern)
        page.close()


def check_sidebar_note_wrap(context, base_url: str, out, report) -> dict:
    """P1 样式：左侧边栏寄语卡片在窄侧栏（1024px）不应出现末字单独折行。"""
    page = context.new_page()
    try:
        page.goto(base_url + "/", wait_until="domcontentloaded")
        page.wait_for_selector(".layout", timeout=25000)
        page.set_viewport_size({"width": 1024, "height": 900})
        page.wait_for_timeout(500)
        geoms = {}
        for width in (1440, 1024, 390):
            page.set_viewport_size({"width": width, "height": 900})
            page.wait_for_timeout(350)
            if width == 390:
                page.locator(".mobile-toggle").click()
                page.wait_for_timeout(350)
            geoms[str(width)] = page.evaluate(
                """() => {
                    const note = document.querySelector('.sidebar-note');
                    const strong = note.querySelector('strong');
                    const range = document.createRange();
                    range.selectNodeContents(strong);
                    const lines = [...range.getClientRects()].filter(rect => rect.width > 1);
                    return {lines: lines.length, text: strong.innerText,
                            noteWidth: Math.round(note.getBoundingClientRect().width)};
                }"""
            )
            if width == 390:
                page.keyboard.press("Escape")
                page.wait_for_timeout(250)
        page.set_viewport_size({"width": 1440, "height": 900})
        page.wait_for_timeout(350)
        bad = [key for key, value in geoms.items() if value["lines"] != 1]
        assert_true(not bad, f"侧栏寄语标题出现折行（含孤字）：{[geoms[key] for key in bad]}")
        page.locator(".sidebar-note").screenshot(path=str(Path(out) / "flows-sidebar-note.png"))
        report["screenshots"].append(str(Path(out) / "flows-sidebar-note.png"))
        return {"geoms": geoms, "screenshot": str(Path(out) / "flows-sidebar-note.png")}
    finally:
        page.close()


def check_settings_recovery_codes(context, base_url: str, out, report) -> dict:
    """设置页生成恢复码必须携带当前密码（后端已要求二次校验，缺密码会 422）。"""
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    dialogs = []
    page.on("dialog", lambda dialog: (dialogs.append(dialog.message), dialog.accept()))
    try:
        requests = []

        def capture(request):
            if request.url.endswith("/api/auth/recovery-codes"):
                try:
                    requests.append(request.post_data_json or {})
                except Exception:
                    requests.append({})

        page.on("request", capture)
        page.goto(base_url + "/#/settings", wait_until="domcontentloaded")
        page.wait_for_selector(".pg-api-status", timeout=25000)
        page.wait_for_timeout(600)
        page.fill('input[autocomplete="current-password"]', PASSWORD)
        button = page.get_by_role("button", name="生成 8 组找回密码恢复码")
        button.scroll_into_view_if_needed()
        with page.expect_response(
                lambda response: "/api/auth/recovery-codes" in response.url,
                timeout=20000) as response_info:
            button.click()
        response = response_info.value
        assert_true(response.status == 200,
                    f"生成恢复码接口返回 {response.status}：{response.text()[:200]}")
        page.wait_for_timeout(800)
        assert_true(bool(requests), "设置页没有发起 /api/auth/recovery-codes 请求")
        assert_true(all("current_password" in body for body in requests),
                    f"生成恢复码请求缺少 current_password：{requests}")
        codes = page.locator(".prompt-preview-pre")
        assert_true(codes.count() > 0 and codes.first.is_visible(),
                    "生成恢复码后页面没有展示恢复码")
        text = codes.first.inner_text()
        assert_true(len([line for line in text.splitlines() if line.strip()]) >= 8,
                    f"恢复码数量不足：{text[:80]}")
        assert_true(bool(dialogs), "生成恢复码缺少二次确认对话框")
        shot(page, out, "flows-settings-recovery.png", report)
        assert_true(not errors, f"设置页恢复码检查出现 JS 异常：{errors[:2]}")
        return {"requests": requests, "dialog": dialogs[:1],
                "screenshot": str(Path(out) / "flows-settings-recovery.png")}
    finally:
        page.close()


def check_upload_state_and_method_badge(context, base_url: str, out, report) -> dict:
    """拍题页：重传新图重置步骤 2–3 / uploadJobId；确认保存失败必须显式报错；
    结果卡展示 method 题型标签（aiResultCard 统一渲染）。"""
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    job_posts = {"count": 0}
    upload_pattern = re.compile(r".*/api/uploads/question$")
    confirm_pattern = re.compile(r".*/api/uploads/99001/confirm$")
    jobs_pattern = re.compile(r".*/api/ai/jobs$")

    def upload_handler(route):
        route.fulfill(status=200, content_type="application/json",
                      body=json.dumps({"id": 99001, "filename": "验收题.txt",
                                       "extracted_text": "已知函数 f(x)=x^2-2x，求 f(x) 的最小值。"},
                                      ensure_ascii=False))

    def confirm_handler(route):
        route.fulfill(status=500, content_type="application/json",
                      body=json.dumps({"detail": "模拟保存失败"}, ensure_ascii=False))

    def jobs_handler(route):
        if route.request.method == "POST":
            job_posts["count"] += 1
        route.continue_()

    first_file = Path(tempfile.gettempdir()) / "zhixu_flow_upload_a.txt"
    second_file = Path(tempfile.gettempdir()) / "zhixu_flow_upload_b.txt"
    first_file.write_text("第一道验收题：已知函数 f(x)=x^2-2x，求最小值。", encoding="utf-8")
    second_file.write_text("第二道验收题（换图后应清空旧状态）：求 y=ln x 的导数。", encoding="utf-8")
    # 本项故意 mock 保存失败（500）验证错误提示，全局 4xx/5xx 断言需放行该请求。
    report.setdefault("allowed_http", []).append("/api/uploads/99001/confirm")
    report.setdefault("allowed_console", []).append(r"status of 500")
    try:
        ensure_login(page, base_url, USERNAME, PASSWORD)

        # 1) 结果卡 method 题型标签（renderAiResult 公共渲染）。
        method_badge = page.evaluate(
            """async () => {
                const ai = await import('/static/js/components/aiResultCard.js');
                const node = ai.renderAiResult({
                    method: '导数-含参讨论',
                    content: '## 解析\\n\\n先求导再分类讨论。',
                }, {task: 'question-analysis'});
                return {text: node.innerText, has_method: node.innerText.includes('导数-含参讨论'),
                        has_label: node.innerText.includes('题型')};
            }"""
        )
        assert_true(method_badge["has_method"] and method_badge["has_label"],
                    f"AI 结果卡未展示 method 题型标签：{method_badge['text'][:160]}")

        page.route(upload_pattern, upload_handler)
        page.route(confirm_pattern, confirm_handler)
        page.route(jobs_pattern, jobs_handler)
        page.goto(base_url + "/#/upload", wait_until="domcontentloaded")
        page.wait_for_selector(".pg-step-rail", timeout=30000)
        upload_input = page.locator("#upload-step-1 .upload-zone input[type=file]").first
        upload_input.set_input_files(str(first_file))
        page.wait_for_timeout(400)
        page.get_by_role("button", name="上传题目").click()
        page.wait_for_selector("text=已上传", timeout=20000)
        page.wait_for_timeout(500)
        stem_value = page.locator("#upload-step-2 textarea").first.input_value()
        state_after_upload = page.evaluate(
            """async () => {
                const st = await import('/static/js/core/state.js');
                return {id: st.state.questionUpload?.id || null, jobId: st.state.uploadJobId};
            }"""
        )
        assert_true(state_after_upload["id"] == 99001, f"上传后 state.questionUpload 未更新：{state_after_upload}")
        assert_true("x^2-2x" in stem_value, f"上传后题干未回填：{stem_value[:80]}")

        # 2) 生成前自动保存失败：必须显示错误并中止，不得静默继续。
        page.get_by_role("button", name="开始求解与分析").click()
        page.wait_for_timeout(1500)
        toast_text = page.locator("#toast-region").inner_text()
        assert_true("题干保存失败" in toast_text, f"确认保存失败被静默吞掉，未显示错误：{toast_text[:120]}")
        assert_true(job_posts["count"] == 0, "题干保存失败后仍发起了 AI 求解任务")

        # 3) 重新选择新图：重置步骤 2–3、uploadState 与 uploadJobId。
        upload_input = page.locator("#upload-step-1 .upload-zone input[type=file]").first
        upload_input.set_input_files(str(second_file))
        page.wait_for_timeout(900)
        reset_state = page.evaluate(
            """async () => {
                const st = await import('/static/js/core/state.js');
                return {
                    upload: st.state.questionUpload,
                    jobId: st.state.uploadJobId,
                    stem: document.querySelector('#upload-step-2 textarea')?.value ?? null,
                    uploaded_notice: document.querySelector('#upload-step-1')?.innerText.includes('已上传：') || false,
                };
            }"""
        )
        assert_true(reset_state["upload"] is None, "重选新图后 state.questionUpload 未清空")
        assert_true(reset_state["jobId"] is None, "重选新图后 state.uploadJobId 未清空")
        assert_true(reset_state["stem"] == "", f"重选新图后题干未重置：{reset_state['stem']}")
        assert_true(not reset_state["uploaded_notice"], "重选新图后仍显示上一题的已上传提示")
        detail = {
            "method_badge": method_badge,
            "uploaded_extracted": "x^2-2x" in stem_value,
            "save_failure_toast": toast_text[:120],
            "job_posts_after_failure": job_posts["count"],
            "reset_state": {key: value for key, value in reset_state.items() if key != "stem"},
        }
        detail["screenshot"] = shot(page, out, "flows-upload-reset.png", report)
        assert_true(not errors, f"拍题状态检查出现 JS 异常：{errors[:2]}")
        return detail
    finally:
        page.unroute(upload_pattern)
        page.unroute(confirm_pattern)
        page.unroute(jobs_pattern)
        for path in (first_file, second_file):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        page.close()


def check_settings_custom_provider(context, base_url: str, out, report) -> dict:
    """设置页：自定义厂商向导 → 地址规范化预览 → 拉模型（120 个不截断）→ 测通 → 保存；
    同时校验 Embedding 配置与 /health/ready 脱敏（不展示绝对路径）。"""
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    captured = {"preview": [], "probe": [], "create": [], "embedding_put": [], "embedding_test": []}

    providers_pattern = re.compile(r".*/api/providers$")
    defaults_pattern = re.compile(r".*/api/ai/task-defaults$")
    preview_pattern = re.compile(r".*/api/providers/fetch-models-preview$")
    probe_pattern = re.compile(r".*/api/providers/probe$")
    emb_config_pattern = re.compile(r".*/api/providers/embeddings-config$")
    emb_test_pattern = re.compile(r".*/api/providers/embeddings-test$")
    ready_pattern = re.compile(r".*/api/health/ready(\?.*)?$")

    def providers_handler(route):
        if route.request.method == "POST":
            payload = route.request.post_data_json or {}
            captured["create"].append(payload)
            route.fulfill(status=200, content_type="application/json", body=json.dumps({
                "id": 99, "name": payload.get("name") or "我的中转站",
                "kind": payload.get("kind") or "openai",
                "base_url": "https://relay.example.com/v1",
                "key_hint": "已加密 · ****wxyz", "has_api_key": True,
                "auth_mode": payload.get("auth_mode") or "auto",
                "extra_header_names": list((payload.get("extra_headers") or {}).keys()),
                "compat": payload.get("compat") or {},
                "models": payload.get("models") or [], "model_details": [],
            }, ensure_ascii=False))
            return
        route.fulfill(status=200, content_type="application/json", body=json.dumps({
            "items": [], "presets": SETTINGS_PROVIDERS_FIXTURE["presets"],
            "url_policy": "local_open", "public_mode": False,
        }, ensure_ascii=False))

    def defaults_handler(route):
        if route.request.method == "PUT":
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps(route.request.post_data_json or {}, ensure_ascii=False))
        else:
            route.fulfill(status=200, content_type="application/json", body='{"items": {}}')

    def preview_handler(route):
        captured["preview"].append(route.request.post_data_json or {})
        route.fulfill(status=200, content_type="application/json", body=json.dumps({
            "ok": True,
            "normalized_base_url": "https://relay.example.com/v1",
            "resolved_endpoint": "https://relay.example.com/v1/models",
            "models": SETTINGS_PREVIEW_MODELS,
            "model_ids": [item["model_id"] for item in SETTINGS_PREVIEW_MODELS],
            "count": len(SETTINGS_PREVIEW_MODELS),
            "capabilities": {item["model_id"]: item["caps"] for item in SETTINGS_PREVIEW_MODELS},
            "http_status": 200,
            "diagnosis": "",
            "notes": ["已自动清洗尾部路径 /chat/completions"],
        }, ensure_ascii=False))

    def probe_handler(route):
        captured["probe"].append(route.request.post_data_json or {})
        route.fulfill(status=200, content_type="application/json", body=json.dumps({
            "ok": True, "http_status": 200, "latency_ms": 123,
            "diagnosis": "连接正常：地址、鉴权与模型名可用",
            "request_url": "https://relay.example.com/v1/chat/completions",
            "normalized_base_url": "https://relay.example.com/v1",
            "resolved_endpoint": "https://relay.example.com/v1/chat/completions",
            "kind": "openai", "model": "relay-model-000", "usage": {},
            "content_preview": "OK", "notes": [],
        }, ensure_ascii=False))

    def emb_config_handler(route):
        if route.request.method == "PUT":
            payload = route.request.post_data_json or {}
            captured["embedding_put"].append(payload)
            body = dict(SETTINGS_EMBEDDING_FIXTURE)
            body.update({key: value for key, value in payload.items() if value is not None})
            body["effective_source"] = payload.get("mode") or "custom"
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps(body, ensure_ascii=False))
        else:
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps(SETTINGS_EMBEDDING_FIXTURE, ensure_ascii=False))

    def emb_test_handler(route):
        captured["embedding_test"].append(route.request.post_data_json or {})
        route.fulfill(status=200, content_type="application/json", body=json.dumps({
            "ok": True, "dimension": 1536, "latency_ms": 46,
            "model": "text-embedding-3-small",
            "endpoint": "https://embed.example.com/v1/embeddings",
            "source": "custom", "diagnosis": "向量接口正常",
        }, ensure_ascii=False))

    def ready_handler(route):
        # 匿名精简响应：只有状态与布尔值，绝不含宿主机绝对路径。
        route.fulfill(status=200, content_type="application/json", body=json.dumps({
            "status": "ok", "version": "0.1.0", "build_id": "mock-ready",
            "study_ready": True, "ai_ready": True,
            "checks": {"app_db": {"ok": True}, "bank": {"ok": True}, "storage": {"ok": True}},
        }, ensure_ascii=False))

    try:
        page.route(providers_pattern, providers_handler)
        page.route(defaults_pattern, defaults_handler)
        page.route(preview_pattern, preview_handler)
        page.route(probe_pattern, probe_handler)
        page.route(emb_config_pattern, emb_config_handler)
        page.route(emb_test_pattern, emb_test_handler)
        page.route(ready_pattern, ready_handler)
        ensure_login(page, base_url, USERNAME, PASSWORD)
        page.goto(base_url + "/#/settings", wait_until="domcontentloaded")
        page.wait_for_selector(".pg-api-status", timeout=30000)
        page.wait_for_timeout(400)

        # 1) Embedding 折叠卡片：懒加载配置 → 测试 → 保存，并检查不泄露绝对路径。
        emb_summary = page.locator(".pg-embedding-card details.pg-advanced > summary")
        assert_true(emb_summary.count() == 1, "设置页缺少「语义检索（Embedding）配置」折叠卡片")
        emb_summary.click()
        page.wait_for_selector(".pg-embedding-status .pg-tag", timeout=15000)
        page.wait_for_timeout(300)
        emb_status_text = page.locator(".pg-embedding-status").inner_text()
        assert_true("已启用" in emb_status_text or "text-embedding" in emb_status_text,
                    f"Embedding 状态未展示已启用配置：{emb_status_text[:160]}")
        page.get_by_role("button", name="测试向量接口").click()
        page.wait_for_selector(".pg-embedding-result .notice", timeout=15000)
        emb_test_text = page.locator(".pg-embedding-result .notice").inner_text()
        assert_true("1536" in emb_test_text, f"Embedding 测试结果未展示向量维度：{emb_test_text[:160]}")
        assert_true(bool(captured["embedding_test"]), "点击测试向量接口未发起 /embeddings-test 请求")
        page.get_by_role("button", name="保存 Embedding 配置").click()
        page.wait_for_timeout(600)
        assert_true(bool(captured["embedding_put"]), "保存 Embedding 配置未发起 PUT 请求")
        emb_put = captured["embedding_put"][-1]
        assert_true(emb_put.get("mode") == "custom" and emb_put.get("model") == "text-embedding-3-small",
                    f"Embedding PUT 请求体不符：{emb_put}")
        main_text = page.locator("#main-content").inner_text()
        leaked = re.findall(r"[A-Za-z]:\\\\[^\s\"']+|bank_dir|STUDY_BANK_DIR|api-key\.key|app\.sqlite3", main_text)
        assert_true(not leaked, f"设置页展示了疑似宿主机绝对路径/敏感文件：{leaked[:3]}")
        assert_true("维度" not in emb_status_text, "Embedding 未测试时不应凭空显示维度")

        # 2) 连接向导
        page.locator(".provider-form button[type=submit]").click()
        page.wait_for_selector(".provider-dialog", timeout=15000)
        dialog = page.locator(".provider-dialog")
        groups = dialog.locator("[data-field='preset'] optgroup").evaluate_all(
            "nodes => nodes.map(node => node.label)")
        assert_true(any("常用自定义与本地" in label for label in groups),
                    f"厂商预设缺少「常用自定义与本地」分组：{groups[:3]}")
        preset_labels = dialog.locator("[data-field='preset'] option").all_inner_texts()
        for expected in ("本地 Ollama", "Azure OpenAI", "Gemini 原生接口"):
            assert_true(any(expected in label for label in preset_labels),
                        f"厂商预设缺少「{expected}」")
        dialog.locator("[data-field='preset']").select_option("custom-0")
        page.fill(".provider-dialog [data-field='name']", "我的中转站")
        page.fill(".provider-dialog [data-field='base-url']",
                  "https://relay.example.com/v1/chat/completions")
        page.fill(".provider-dialog [data-field='api-key']", "sk-ui-flow-test-123456")
        page.wait_for_selector(".provider-preview-endpoint", timeout=15000)
        page.wait_for_timeout(300)

        endpoint_text = page.locator(".provider-preview-endpoint").inner_text()
        assert_true("POST" in endpoint_text and "https://relay.example.com/v1/chat/completions" in endpoint_text,
                    f"规范化预览未展示对话请求端点：{endpoint_text[:200]}")
        assert_true("/chat/completions/chat/completions" not in endpoint_text,
                    f"误粘贴的尾部路径未清洗：{endpoint_text[:200]}")
        preview_text = page.locator(".provider-preview-mount").inner_text()
        assert_true("清洗" in preview_text or "规范化" in preview_text,
                    f"规范化预览缺少清洗提示：{preview_text[:200]}")
        assert_true(bool(captured["preview"]), "预览未请求 fetch-models-preview")
        preview_body = captured["preview"][-1]
        assert_true(preview_body.get("auth_mode") == "auto" and "compat" in preview_body,
                    f"预览请求体缺少 auth_mode/compat：{preview_body}")

        model_boxes = dialog.locator(".provider-model-list input[type=checkbox]")
        assert_true(model_boxes.count() == PREVIEW_MODEL_COUNT,
                    f"模型列表被截断：预期 {PREVIEW_MODEL_COUNT}，实际 {model_boxes.count()}")
        count_text = dialog.locator(".provider-model-count").inner_text()
        assert_true(f"共 {PREVIEW_MODEL_COUNT} 个模型" in count_text,
                    f"模型数量提示不符：{count_text}")
        dialog.locator("[data-field='model-search']").fill("relay-model-119")
        page.wait_for_timeout(200)
        assert_true(model_boxes.count() == 1, "搜索不出第 120 个模型（列表被截断）")
        dialog.locator("[data-field='model-search']").fill("")
        page.wait_for_timeout(200)
        dialog.locator("[data-action='model-select-all']").click()
        page.wait_for_timeout(200)
        count_text = dialog.locator(".provider-model-count").inner_text()
        assert_true(f"已选 {PREVIEW_MODEL_COUNT}" in count_text,
                    f"全选后已选数量不符：{count_text}")

        # 3) 高级连接选项 + 保存前测通
        dialog.locator("details.provider-advanced > summary").click()
        page.wait_for_timeout(200)
        dialog.locator("[data-field='auth-mode']").select_option("bearer")
        dialog.locator("[data-field='max-tokens-param']").select_option("max_tokens")
        dialog.locator("[data-field='timeout']").fill("45")
        dialog.locator("[data-action='add-header']").click()
        dialog.locator("[data-field='header-key']").fill("HTTP-Referer")
        dialog.locator("[data-field='header-value']").fill("https://zhixu.local")
        dialog.locator("[data-action='probe']").click()
        page.wait_for_selector(".provider-probe-result .notice", timeout=20000)
        probe_text = page.locator(".provider-probe-result").inner_text()
        assert_true("连接成功" in probe_text and "123" in probe_text,
                    f"保存前测试连接未展示耗时/诊断：{probe_text[:200]}")
        assert_true(bool(captured["probe"]), "保存前测试连接未请求 /providers/probe")
        probe_body = captured["probe"][-1]
        assert_true(probe_body.get("model") == "relay-model-000"
                    and probe_body.get("auth_mode") == "bearer"
                    and "compat" in probe_body,
                    f"probe 请求体不符：{probe_body}")

        dialog.locator("[data-action='save']").click()
        page.wait_for_selector(".provider-dialog", state="detached", timeout=20000)
        page.wait_for_timeout(300)
        assert_true(page.locator(".provider-dialog").count() == 0, "保存成功后连接向导未关闭")
        assert_true(bool(captured["create"]), "保存连接未发起 POST /api/providers")
        create_body = captured["create"][-1]
        assert_true(create_body.get("auth_mode") == "bearer",
                    f"保存请求体缺少 auth_mode：{create_body}")
        compat = create_body.get("compat") or {}
        assert_true(compat.get("max_tokens_param") == "max_tokens"
                    and compat.get("timeout_seconds") == 45,
                    f"保存请求体 compat 不符：{compat}")
        assert_true(len(create_body.get("models") or []) == PREVIEW_MODEL_COUNT,
                    f"保存时模型列表被截断：{len(create_body.get('models') or [])}")
        assert_true((create_body.get("extra_headers") or {}).get("HTTP-Referer")
                    == "https://zhixu.local",
                    f"保存请求体缺少自定义请求头：{create_body.get('extra_headers')}")
        assert_true(create_body.get("base_url") == "https://relay.example.com/v1/chat/completions",
                    f"保存请求体 base_url 不是用户填写值：{create_body.get('base_url')}")

        detail = {
            "preview_endpoint": endpoint_text[:200],
            "preview_request": preview_body,
            "models_listed": PREVIEW_MODEL_COUNT,
            "probe_request": probe_body,
            "save_request": {
                "auth_mode": create_body.get("auth_mode"), "compat": compat,
                "models": len(create_body.get("models") or []),
                "extra_headers": create_body.get("extra_headers"),
            },
            "embedding_save": emb_put,
            "embedding_test_text": emb_test_text[:160],
            "privacy_leaks": leaked,
        }
        detail["screenshot"] = shot(page, out, "flows-settings-custom-provider.png", report, full_page=True)
        assert_true(not errors, f"设置页自定义厂商检查出现 JS 异常：{errors[:2]}")
        return detail
    finally:
        page.close()


def check_settings_task_defaults(context, base_url: str, out, report) -> dict:
    """设置页顶部默认模型卡片：行内设置 / 清空 judge 必须真实发请求，且局部刷新不整页重渲染。"""
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    puts = []
    providers_pattern = re.compile(r".*/api/providers$")
    defaults_pattern = re.compile(r".*/api/ai/task-defaults$")

    def providers_handler(route):
        route.fulfill(status=200, content_type="application/json",
                      body=json.dumps(SETTINGS_PROVIDERS_FIXTURE, ensure_ascii=False))

    def defaults_handler(route):
        if route.request.method == "PUT":
            payload = route.request.post_data_json or {}
            puts.append(payload)
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps(payload, ensure_ascii=False))
        else:
            route.fulfill(status=200, content_type="application/json", body='{"items": {}}')

    try:
        page.route(providers_pattern, providers_handler)
        page.route(defaults_pattern, defaults_handler)
        ensure_login(page, base_url, USERNAME, PASSWORD)
        page.goto(base_url + "/#/settings", wait_until="domcontentloaded")
        page.wait_for_selector(".pg-task-defaults", timeout=30000)
        page.wait_for_timeout(500)
        rows = page.locator(".pg-task-row")
        assert_true(rows.count() >= 7, f"默认模型行不足（应含 explain/grade/judge/notes/ocr/upload/tagging）：{rows.count()}")

        grade_row = page.locator(".pg-task-row[data-task-row='grade']")
        assert_true(grade_row.count() == 1, "缺少 grade（答题判分）默认模型行")
        grade_first_option = grade_row.locator("[data-field='task-primary'] option").first.inner_text()
        assert_true("跟随讲解模型" in grade_first_option,
                    f"grade 槽位未说明「跟随讲解模型」：{grade_first_option}")
        grade_text = grade_row.locator(".pg-task-provider").inner_text()
        assert_true("跟随讲解模型" in grade_text, f"grade 槽位未显示跟随讲解模型：{grade_text}")

        page.evaluate("document.querySelector('.pg-api-status').dataset.flowMarker = 'kept'")

        judge_row = page.locator(".pg-task-row[data-task-row='judge']")
        with wait_task_default_put(page) as response_info:
            judge_row.locator("[data-field='task-primary']").select_option(model_value(11, "model-a"))
        page.wait_for_timeout(300)
        judge_body = response_info.value.request.post_data_json
        assert_true(judge_body.get("task") == "judge", f"judge 设置请求 task 不符：{judge_body}")
        assert_true(judge_body.get("judge_provider_id") == 11
                    and judge_body.get("judge_model") == "model-a",
                    f"judge 设置请求缺少 judge_provider_id/judge_model：{judge_body}")
        assert_true(judge_body.get("primary_provider_id") is None,
                    f"judge 设置不应写 primary 字段：{judge_body}")
        judge_text = judge_row.locator(".pg-task-provider").inner_text()
        assert_true("Mock 中转站" in judge_text,
                    f"judge 行未解析出厂商名：{judge_text}")

        with wait_task_default_put(page) as response_info:
            judge_row.locator("[data-action='clear-task']").click()
        page.wait_for_timeout(300)
        clear_body = response_info.value.request.post_data_json
        assert_true(clear_body.get("task") == "judge", "judge 清空请求 task 不符")
        assert_true(clear_body.get("judge_provider_id") is None
                    and clear_body.get("judge_model") is None,
                    f"judge 清空未真正发送 null 字段：{clear_body}")
        judge_text = judge_row.locator(".pg-task-provider").inner_text()
        assert_true("未固定" in judge_text, f"judge 清空后行内文案未刷新：{judge_text}")

        marker = page.evaluate("document.querySelector('.pg-api-status').dataset.flowMarker")
        assert_true(marker == "kept", "行内保存默认模型触发了整页 renderRoute（卡片被重建）")

        explain_row = page.locator(".pg-task-row[data-task-row='explain']")
        with wait_task_default_put(page) as response_info:
            explain_row.locator("[data-field='task-primary']").select_option(model_value(11, "model-a"))
        page.wait_for_timeout(250)
        with wait_task_default_put(page) as response_info:
            explain_row.locator("[data-field='task-verify']").select_option(model_value(12, "model-c"))
        page.wait_for_timeout(300)
        explain_body = response_info.value.request.post_data_json
        assert_true(explain_body.get("task") == "explain", "explain 设置请求 task 不符")
        assert_true(explain_body.get("primary_provider_id") == 11
                    and explain_body.get("primary_model") == "model-a",
                    f"explain 主模型字段不符：{explain_body}")
        assert_true(explain_body.get("verify_provider_id") == 12
                    and explain_body.get("verify_model") == "model-c"
                    and explain_body.get("mode") == "accurate",
                    f"explain 核验模型字段不符：{explain_body}")

        detail = {
            "task_rows": rows.count(),
            "judge_set_request": judge_body,
            "judge_clear_request": clear_body,
            "explain_request": explain_body,
            "put_count": len(puts),
        }
        detail["screenshot"] = shot(page, out, "flows-settings-task-defaults.png", report, full_page=True)
        assert_true(not errors, f"默认模型行内编辑出现 JS 异常：{errors[:2]}")
        return detail
    finally:
        page.close()


def check_model_picker_persistence(context, base_url: str, out, report) -> dict:
    """modelPicker：切换模型/精确-快速模式后写回内存默认模型，切下一题不回弹。"""
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    puts = []
    providers_pattern = re.compile(r".*/api/providers$")
    defaults_pattern = re.compile(r".*/api/ai/task-defaults$")

    def providers_handler(route):
        route.fulfill(status=200, content_type="application/json",
                      body=json.dumps(SETTINGS_PROVIDERS_FIXTURE, ensure_ascii=False))

    def defaults_handler(route):
        if route.request.method == "PUT":
            puts.append(route.request.post_data_json or {})
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps(route.request.post_data_json or {}, ensure_ascii=False))
        else:
            route.fulfill(status=200, content_type="application/json", body='{"items": {}}')

    def state_snapshot(page_obj):
        return page_obj.evaluate(
            """async () => {
                const st = await import('/static/js/core/state.js');
                return {
                    qid: st.state.currentQuestion?.qid || null,
                    explain: st.state.taskDefaults?.explain || null,
                };
            }"""
        )

    try:
        page.route(providers_pattern, providers_handler)
        page.route(defaults_pattern, defaults_handler)
        ensure_login(page, base_url, USERNAME, PASSWORD)
        page.goto(base_url + "/#/practice", wait_until="domcontentloaded")
        page.wait_for_selector(".practice-workspace", timeout=30000)
        page.wait_for_selector(".result-card", timeout=30000)
        page.wait_for_timeout(400)

        cards = page.locator(".result-card")
        assert_true(cards.count() >= 3, f"题库结果不足 3 条，无法验证切题回弹：{cards.count()}")
        first_qid = cards.first.get_attribute("data-qid")
        cards.first.click()
        page.wait_for_selector(".ai-panel .model-picker", timeout=30000)
        page.wait_for_timeout(600)

        primary_sel = page.locator(".ai-panel .model-picker select").first
        verifier_sel = page.locator(".ai-panel .model-picker select").nth(1)
        primary_value = primary_sel.locator("option").nth(1).get_attribute("value")
        assert_true(bool(primary_value), "模型选择器没有可选模型")
        primary_sel.select_option(primary_value)
        page.wait_for_timeout(400)
        fast_state = state_snapshot(page)
        assert_true(fast_state["explain"] and fast_state["explain"].get("mode") == "fast",
                    f"切换主模型后未写回 state.taskDefaults（fast）：{fast_state['explain']}")

        page.locator(".ai-panel .mode-pill").filter(has_text="精确模式").click()
        page.wait_for_timeout(400)
        verifier_options = verifier_sel.locator("option")
        verifier_value = verifier_options.nth(2).get_attribute("value") or verifier_options.nth(1).get_attribute("value")
        verifier_sel.select_option(verifier_value)
        page.wait_for_timeout(500)
        accurate_state = state_snapshot(page)
        explain_state = accurate_state["explain"] or {}
        assert_true(explain_state.get("mode") == "accurate",
                    f"切换精确模式后未写回 state（accurate）：{explain_state}")
        assert_true(bool(explain_state.get("verify_model")),
                    f"精确模式未写回 verify 字段：{explain_state}")

        # 切到下一题：精确模式与主模型不得回弹。
        second_qid = cards.nth(1).get_attribute("data-qid")
        assert_true(second_qid != first_qid, "题库前两条结果相同，无法验证切题")
        cards.nth(1).click()
        page.wait_for_function(
            """async qid => {
                const st = await import('/static/js/core/state.js');
                return st.state.currentQuestion?.qid === qid;
            }""", arg=second_qid, timeout=30000)
        page.wait_for_selector(".ai-panel .model-picker", timeout=20000)
        page.wait_for_timeout(500)
        accurate_pill = page.locator(".ai-panel .mode-pill").filter(has_text="精确模式")
        assert_true("active" in (accurate_pill.get_attribute("class") or ""),
                    "切到下一题后精确模式弹回快速模式")
        assert_true(page.locator(".ai-panel .model-picker select").first.input_value() == primary_value,
                    "切到下一题后主模型弹回旧选择")

        # 再切快速模式，继续切题：不得弹回精确模式。
        page.locator(".ai-panel .mode-pill").filter(has_text="快速模式").click()
        page.wait_for_timeout(500)
        switched_state = state_snapshot(page)
        switched_explain = switched_state["explain"] or {}
        assert_true(switched_explain.get("mode") == "fast"
                    and not switched_explain.get("verify_model"),
                    f"切回快速模式后 state 未清空核验字段：{switched_explain}")
        third_qid = cards.nth(2).get_attribute("data-qid")
        cards.nth(2).click()
        page.wait_for_function(
            """async qid => {
                const st = await import('/static/js/core/state.js');
                return st.state.currentQuestion?.qid === qid;
            }""", arg=third_qid, timeout=30000)
        page.wait_for_selector(".ai-panel .model-picker", timeout=20000)
        page.wait_for_timeout(500)
        fast_pill = page.locator(".ai-panel .mode-pill").filter(has_text="快速模式")
        assert_true("active" in (fast_pill.get_attribute("class") or ""),
                    "切回快速模式后切题又弹回精确模式")
        assert_true(page.locator(".ai-panel .model-picker select").first.input_value() == primary_value,
                    "切回快速模式后主模型也发生弹回")

        modes = [body.get("mode") for body in puts if body.get("task") == "explain"]
        assert_true("accurate" in modes and "fast" in modes,
                    f"modelPicker 未按模式写回默认模型请求：{modes}")
        detail = {
            "primary_value": primary_value,
            "modes_written": modes,
            "fast_state": fast_state["explain"],
            "accurate_state": accurate_state["explain"],
            "current_model_label": page.locator(".ai-panel .model-picker-current").inner_text()
            if page.locator(".ai-panel .model-picker-current").count() else "",
        }
        detail["screenshot"] = shot(page, out, "flows-model-picker-persistence.png", report)
        assert_true(not errors, f"模型选择器持久化检查出现 JS 异常：{errors[:2]}")
        return detail
    finally:
        page.close()


def check_mobile_filters(context, base_url: str, out, report) -> dict:
    """390 / 320 手机：更多筛选与清空筛选可见、列表不被 280px 裁剪、多选面板不越界。"""
    mobile = context.browser.new_context(
        viewport=MOBILE_VIEWPORT, locale="zh-CN", is_mobile=True, has_touch=True, user_agent=MOBILE_UA)
    page = mobile.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))

    def open_practice_mobile():
        page.goto(base_url + "/#/practice", wait_until="domcontentloaded")
        page.wait_for_selector(".practice-workspace", timeout=30000)
        page.wait_for_selector(".result-card", timeout=30000)
        page.wait_for_timeout(500)

    def filter_metrics():
        return page.evaluate(
            """() => {
                const more = document.querySelector('.filter-toggle');
                const clear = [...document.querySelectorAll('.filter-head .button')]
                    .find(node => (node.textContent || '').includes('清空筛选'));
                const headTitle = document.querySelector('.filter-head h2');
                const list = document.querySelector('.question-list');
                const scrollBox = document.querySelector('.results-scroll');
                const style = list ? getComputedStyle(list) : null;
                return {
                    more_visible: Boolean(more && more.offsetParent !== null),
                    clear_visible: Boolean(clear && clear.offsetParent !== null),
                    head_title_hidden: Boolean(headTitle && getComputedStyle(headTitle).display === 'none'),
                    list_max_height: style ? style.maxHeight : null,
                    list_overflow: style ? style.overflow : null,
                    list_scroll_height: list ? list.scrollHeight : 0,
                    list_client_height: list ? list.clientHeight : 0,
                    scroll_overflow: scrollBox ? getComputedStyle(scrollBox).overflow : null,
                    list_box_height: list ? list.getBoundingClientRect().height : 0,
                };
            }"""
        )

    def panel_box():
        return page.evaluate(
            """() => {
                const panel = document.querySelector('.multi-select[open] .multi-select-panel');
                if (!panel) return null;
                const rect = panel.getBoundingClientRect();
                return {left: rect.left, right: rect.right, width: rect.width,
                        viewport: window.innerWidth,
                        overflow_right: rect.right > window.innerWidth + 0.5,
                        overflow_left: rect.left < -0.5};
            }"""
        )

    try:
        ensure_login(page, base_url, USERNAME, PASSWORD)
        open_practice_mobile()

        metrics = filter_metrics()
        assert_true(metrics["more_visible"], "手机端「更多筛选」按钮不可见")
        assert_true(metrics["clear_visible"], "手机端「清空筛选」按钮不可见")
        assert_true(metrics["head_title_hidden"], "手机端应只隐藏 .filter-head h2 标题")
        assert_true(metrics["list_max_height"] in ("none", "0px", None)
                    or metrics["list_max_height"] == "none",
                    f"手机端 .question-list 仍被 max-height 裁剪：{metrics['list_max_height']}")
        assert_true(metrics["list_overflow"] in ("visible", "clip"),
                    f"手机端 .question-list overflow 未解除：{metrics['list_overflow']}")
        assert_true(metrics["list_box_height"] > 300,
                    f"手机端题目列表高度仅 {metrics['list_box_height']}px，疑似仍被 280px 裁剪")
        assert_true(metrics["list_scroll_height"] <= metrics["list_client_height"] + 2,
                    "题目列表自身仍有内部滚动裁剪")

        page.locator(".filter-toggle").first.click()
        page.wait_for_timeout(300)
        assert_true(page.locator(".filter-more").first.is_visible(), "「更多筛选」展开后内容不可见")
        assert_true(page.locator(".filter-more select[aria-label='难度']").first.is_visible(),
                    "「更多筛选」展开后缺少难度筛选控件")

        page.locator(".filter-compact .multi-select > summary").first.click()
        page.wait_for_selector(".multi-select[open]", timeout=8000)
        page.wait_for_timeout(200)
        box = panel_box()
        assert_true(bool(box), "多选面板未打开")
        assert_true(not box["overflow_right"] and not box["overflow_left"],
                    f"390px 下多选面板溢出视口：{box}")

        search = page.locator(".filter-compact .search-wrap input").first
        search.fill("函数")
        page.locator(".filter-compact .search-submit").first.click()
        page.wait_for_timeout(1200)
        assert_true(search.input_value() == "函数", "搜索后关键词输入框被清空")
        page.get_by_role("button", name="清空筛选").click()
        page.wait_for_function(
            """() => {
                const input = document.querySelector('.filter-compact .search-wrap input');
                return Boolean(input) && input.value === '';
            }""", timeout=15000)
        page.wait_for_timeout(500)
        assert_true(search.input_value() == "", "清空筛选后搜索框仍保留关键词")
        assert_true(page.locator(".filter-chip").count() == 0, "清空筛选后仍残留已选筛选芯片")

        # 320px 窄屏重排：不能横向溢出，且多选面板同样不越界。
        page.set_viewport_size({"width": 320, "height": 800})
        open_practice_mobile()
        metrics_320 = filter_metrics()
        assert_true(metrics_320["more_visible"], "320px 下「更多筛选」不可见")
        overflow_320 = page.evaluate(
            "document.documentElement.scrollWidth - document.documentElement.clientWidth")
        assert_true(overflow_320 <= 1, f"320px 页面横向溢出 {overflow_320}px")
        page.locator(".filter-compact .multi-select > summary").first.click()
        page.wait_for_selector(".multi-select[open]", timeout=8000)
        page.wait_for_timeout(200)
        box_320 = panel_box()
        assert_true(bool(box_320) and not box_320["overflow_right"] and not box_320["overflow_left"],
                    f"320px 下多选面板溢出视口：{box_320}")

        detail = {
            "metrics_390": metrics,
            "panel_390": box,
            "metrics_320": metrics_320,
            "panel_320": box_320,
            "overflow_320": overflow_320,
        }
        detail["screenshot_390"] = shot(page, out, "flows-mobile-filters-320.png", report)
        assert_true(not errors, f"手机端筛选检查出现 JS 异常：{errors[:2]}")
        return detail
    finally:
        page.close()
        mobile.close()


# ---------------------------------------------------------------------------
# §3.2 / §3.3 验收：流式固定壳、OCR 异步任务进度、内容管理分区懒加载
# ---------------------------------------------------------------------------
# 前端模块可注入的 EventSource 假实现：读取 window.__zhixuFakeSseScripts[jobId]
# 里的 [delayMs, event, payload] 脚本，按时间顺序派发 SSE 帧，便于断言流式过程。
FAKE_EVENT_SOURCE_JS = r"""
(() => {
  class ZhixuFakeEventSource {
    constructor(url) {
      this.url = String(url || '');
      this.readyState = 0;
      this.closed = false;
      this._listeners = {};
      this._timers = [];
      const parts = this.url.split('/ai/jobs/');
      const jobId = parts.length > 1 ? decodeURIComponent(parts[1].split('/')[0]) : '';
      const attempts = window.__zhixuFakeSseAttempts || (window.__zhixuFakeSseAttempts = {});
      const attempt = (attempts[jobId] = (attempts[jobId] || 0) + 1);
      const spec = (window.__zhixuFakeSseScripts || {})[jobId];
      let scripts = [];
      if (Array.isArray(spec)) scripts = spec;
      else if (spec) scripts = spec[attempt] || spec.default || [];
      setTimeout(() => {
        if (this.closed) return;
        this.readyState = 1;
        for (const entry of scripts) {
          const delay = entry[0];
          const type = entry[1];
          const payload = entry[2];
          this._timers.push(setTimeout(() => {
            if (this.closed) return;
            const event = {data: typeof payload === 'string' ? payload : JSON.stringify(payload)};
            for (const listener of (this._listeners[type] || []).slice()) {
              try { listener(event); } catch (error) { /* 单帧监听异常不影响其它监听器 */ }
            }
            if (type === 'error' && typeof this.onerror === 'function') {
              try { this.onerror(event); } catch (error) { /* ignore */ }
            }
          }, delay));
        }
      }, 0);
    }
    addEventListener(type, listener) {
      (this._listeners[type] = this._listeners[type] || []).push(listener);
    }
    removeEventListener(type, listener) {
      const list = this._listeners[type] || [];
      const index = list.indexOf(listener);
      if (index >= 0) list.splice(index, 1);
    }
    close() {
      this.closed = true;
      this.readyState = 2;
      for (const timer of this._timers) clearTimeout(timer);
      this._timers = [];
    }
  }
  window.EventSource = ZhixuFakeEventSource;
  window.__zhixuFakeEventSourceInstalled = true;
})();
"""

# 内容管理懒加载验收使用与 ui_check.py / ui_check_pages.py 相同的首个账号：
# 隔离库按 README 顺序跑三套脚本时它是管理员；单独跑 flows 时自动注册为管理员。
ADMIN_USERNAME = "ui-visual-check"
ADMIN_PASSWORD = "ui-visual-pass-123"


def check_streaming_shell_stable(context, base_url: str, out, report) -> dict:
    """§3.2：流式输出期间使用固定壳——取消按钮保持同一个 DOM 节点且可点击、
    正文增量更新、阶段进度百分比变化、点击取消后向服务端发出 POST。"""
    page = context.browser.new_context(viewport=DESKTOP_VIEWPORT, locale="zh-CN")
    page.add_init_script(FAKE_EVENT_SOURCE_JS)
    tab = page.new_page()
    errors = []
    tab.on("pageerror", lambda error: errors.append(str(error)))
    cancel_posts = []
    cancel_pattern = re.compile(r".*/api/ai/jobs/424242/cancel$")
    job_pattern = re.compile(r".*/api/ai/jobs/424242$")

    def cancel_handler(route):
        cancel_posts.append(route.request.post_data)
        route.fulfill(status=200, content_type="application/json",
                      body='{"cancelled": true, "status": "cancel_requested"}')

    def job_handler(route):
        route.fulfill(status=200, content_type="application/json", body=json.dumps({
            "id": 424242, "task": "notes", "status": "running", "stage": "tagging-30/50",
            "partial_content": "", "created_at": "2026-11-20T10:00:00+00:00",
        }, ensure_ascii=False))

    try:
        ensure_login(tab, base_url, USERNAME, PASSWORD)
        tab.route(cancel_pattern, cancel_handler)
        tab.route(job_pattern, job_handler)
        tab.evaluate(
            """async () => {
                const jobs = await import('/static/js/core/jobs.js');
                // 挂到 body 最前：登录后首页可能还在异步渲染 #main-content，避免被路由重渲染移除；
                // 置顶也便于截图取证。
                const container = document.createElement('div');
                container.id = 'flows-stream-mount';
                container.style.margin = '16px';
                document.body.prepend(container);
                window.__zhixuFakeSseScripts = {
                  '424242': [
                    [80, 'snapshot', {id: 424242, task: 'notes', status: 'running',
                      stage: 'read-batch-2-of-5', partial_content: '第一段正文。',
                      created_at: new Date(Date.now() - 7000).toISOString()}],
                    [330, 'delta', {text: '第二段增量。'}],
                    [620, 'stage', {status: 'running', stage: 'tagging-25/50'}],
                    [900, 'delta', {text: '第三段增量。'}],
                    [1180, 'stage', {status: 'running', stage: 'tagging-30/50'}]
                  ]
                };
                jobs.mountJob(container, 424242, null, {task: 'notes', titleHint: '流式验收'});
                return true;
            }"""
        )
        tab.wait_for_selector('#flows-stream-mount .job-card-live [data-action="job-cancel"]',
                              timeout=15000)
        tab.wait_for_function(
            """() => {
                const label = document.querySelector('#flows-stream-mount .job-progress-label');
                return Boolean(label) && (label.textContent || '').includes('40%');
            }""", timeout=15000)
        tab.evaluate(
            """() => {
                window.__flowsCancelNode = document.querySelector('#flows-stream-mount [data-action="job-cancel"]');
                window.__flowsElapsed = document.querySelector('#flows-stream-mount .job-elapsed')?.textContent || '';
            }"""
        )
        first_body = tab.locator('#flows-stream-mount .job-live-body').inner_text()
        assert_true("第一段正文" in first_body, f"流式首个快照正文未渲染：{first_body[:120]}")
        tab.wait_for_function(
            """() => {
                const label = document.querySelector('#flows-stream-mount .job-progress-label');
                const body = document.querySelector('#flows-stream-mount .job-live-body');
                return Boolean(label) && (label.textContent || '').includes('50%')
                    && Boolean(body) && (body.innerText || '').includes('第三段增量');
            }""", timeout=15000)
        tab.wait_for_function(
            """() => {
                const current = document.querySelector('#flows-stream-mount .job-elapsed')?.textContent || '';
                return Boolean(current) && current !== window.__flowsElapsed;
            }""", timeout=10000)
        live = tab.evaluate(
            """() => {
                const card = document.querySelector('#flows-stream-mount .job-card-live');
                const cancel = document.querySelector('#flows-stream-mount [data-action="job-cancel"]');
                const label = document.querySelector('#flows-stream-mount .job-progress-label');
                const progress = document.querySelector('#flows-stream-mount .job-progress');
                return {
                    same_cancel_node: Boolean(cancel) && cancel === window.__flowsCancelNode,
                    cancel_enabled: Boolean(cancel) && !cancel.disabled,
                    body_text: card ? (card.innerText || '') : '',
                    percent_attr: label ? label.getAttribute('data-progress-percent') : null,
                    progress_class: progress ? progress.className : '',
                    elapsed: document.querySelector('#flows-stream-mount .job-elapsed')?.textContent || '',
                    badge_status: document.querySelector('#flows-stream-mount .job-status-badge')?.getAttribute('data-job-status') || '',
                };
            }"""
        )
        assert_true(live["same_cancel_node"], "流式更新期间「取消任务」按钮 DOM 节点被重建")
        assert_true(live["cancel_enabled"], "流式输出期间「取消任务」按钮不可点击")
        assert_true(live["percent_attr"] == "50",
                    f"阶段文本 progress 未更新到 50%：{live['percent_attr']}")
        assert_true("job-progress-determinate" in live["progress_class"],
                    f"解析出明确进度后进度条未切到确定态：{live['progress_class']}")
        assert_true("第二段增量" in live["body_text"] and "第三段增量" in live["body_text"],
                    f"delta 增量未追加到正文容器：{live['body_text'][:160]}")
        assert_true(re.search(r"已用 \d{2}:\d{2}", live["elapsed"]),
                    f"未显示已耗时计时：{live['elapsed']}")
        assert_true(live["badge_status"] == "running", f"流式状态徽章异常：{live['badge_status']}")
        tab.evaluate("window.scrollTo(0, 0)")
        tab.wait_for_timeout(120)
        live_shot = shot(tab, out, "flows-streaming-shell-live.png", report)

        with tab.expect_request(
                lambda request: request.method == "POST" and "/api/ai/jobs/424242/cancel" in request.url):
            tab.locator('#flows-stream-mount [data-action="job-cancel"]').click()
        tab.wait_for_function(
            """() => {
                const cancel = document.querySelector('#flows-stream-mount [data-action="job-cancel"]');
                return Boolean(cancel) && cancel.disabled && (cancel.textContent || '').includes('取消');
            }""", timeout=15000)
        tab.wait_for_timeout(300)
        after = tab.evaluate(
            """() => {
                const cancel = document.querySelector('#flows-stream-mount [data-action="job-cancel"]');
                return {
                    same_cancel_node: Boolean(cancel) && cancel === window.__flowsCancelNode,
                    cancel_disabled: Boolean(cancel) && cancel.disabled,
                    cancel_text: cancel ? (cancel.textContent || '') : '',
                };
            }"""
        )
        assert_true(len(cancel_posts) == 1, f"点击取消未发出 POST：{cancel_posts}")
        assert_true(after["same_cancel_node"], "发出取消请求后「取消任务」按钮节点被重建")
        assert_true(after["cancel_disabled"] and "取消" in after["cancel_text"],
                    f"取消请求后按钮未进入取消中状态：{after}")

        # 断线重连：第 1 次连接推送半截正文后 error；第 2 次连接以 snapshot.partial_content
        # + replace 补齐。断言有限重连生效且正文不会重复追加。
        tab.evaluate(
            """async () => {
                const jobs = await import('/static/js/core/jobs.js');
                const container = document.createElement('div');
                container.id = 'flows-reconnect-mount';
                document.body.append(container);
                window.__zhixuFakeSseScripts['515151'] = {
                  1: [
                    [60, 'snapshot', {id: 515151, task: 'notes', status: 'running',
                      stage: 'generate', partial_content: ''}],
                    [160, 'delta', {text: '上半段。'}],
                    [360, 'delta', {text: '下半段。'}],
                    [520, 'error', {}]
                  ],
                  2: [
                    [80, 'snapshot', {id: 515151, task: 'notes', status: 'running',
                      stage: 'generate', partial_content: '上半段。下半段。'}],
                    [160, 'delta', {text: '上半段。下半段。', replace: true}],
                    [420, 'stage', {status: 'running', stage: 'tagging-1/1'}]
                  ]
                };
                jobs.mountJob(container, 515151, null, {task: 'notes'});
                return true;
            }"""
        )
        tab.wait_for_function(
            """() => {
                const body = document.querySelector('#flows-reconnect-mount .job-live-body');
                return Boolean(body) && (body.innerText || '').includes('上半段');
            }""", timeout=15000)
        tab.wait_for_timeout(1800)  # error → 600ms 后重连 → 第二次连接 replace 补齐
        reconnect = tab.evaluate(
            """() => ({
                attempts: (window.__zhixuFakeSseAttempts || {})['515151'] || 0,
                text: (document.querySelector('#flows-reconnect-mount .job-live-body')?.innerText || '')
                    .replace(/\\s+/g, ''),
            })"""
        )
        assert_true(reconnect["attempts"] >= 2,
                    f"SSE 瞬断后未执行有限重连：attempts={reconnect['attempts']}")
        assert_true(reconnect["text"].count("上半段") == 1
                    and reconnect["text"].count("下半段") == 1,
                    f"重连后正文被重复追加：{reconnect['text'][:160]}")

        detail = {
            "live": live,
            "cancel_posts": len(cancel_posts),
            "cancel_after": after,
            "reconnect": reconnect,
            "screenshot_live": live_shot,
        }
        tab.evaluate("window.scrollTo(0, 0)")
        tab.wait_for_timeout(120)
        detail["screenshot"] = shot(tab, out, "flows-streaming-shell-cancelled.png", report)
        assert_true(not errors, f"流式固定壳检查出现 JS 异常：{errors[:2]}")
        return detail
    finally:
        try:
            tab.unroute(cancel_pattern)
            tab.unroute(job_pattern)
        except Exception:  # noqa: BLE001 - 页面关闭后 unroute 失败可忽略
            pass
        tab.close()
        page.close()


def check_ocr_job_progress(context, base_url: str, out, report) -> dict:
    """§3.3：笔记页 OCR 命中 async:true 时改走任务流式进度，完成时刷新资料列表与资料卡。"""
    ctx = context.browser.new_context(viewport=DESKTOP_VIEWPORT, locale="zh-CN")
    ctx.add_init_script(FAKE_EVENT_SOURCE_JS)
    page = ctx.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on("dialog", lambda dialog: dialog.accept())
    state = {"ocr_done": False, "documents_calls": 0}
    ocr_requests = []

    def document_payload(updated: bool = False):
        if updated:
            return {
                "id": 77001, "title": "验收扫描讲义.pdf", "pages": 3, "status": "ready",
                "doc_version": 3,
                "ocr_range": {"page_from": 1, "page_to": 3},
                "coverage": {
                    "pages": 3, "page_count": 3, "covered_pages": [1, 2, 3],
                    "missing_pages": [], "pending_pages": [], "failed_pages": [],
                    "reviewed_pages": [], "covered_count": 3, "complete": True,
                    "next_page_from": None, "next_page_to": None,
                },
                "created_at": "2026-11-20T10:00:00", "chunks": 3,
                "headings": ["测试章节"], "text_excerpt": "测试讲义文本",
                "index_status": "stale", "index_digest": None, "index_updated_at": None,
                "index_enabled": True, "notice": "",
            }
        return {
            "id": 77001, "title": "验收扫描讲义.pdf", "pages": 3, "status": "partial",
            "doc_version": 2,
            "ocr_range": {"page_from": 1, "page_to": 3},
            "coverage": {
                "pages": 3, "page_count": 3, "covered_pages": [1],
                "missing_pages": [2, 3], "pending_pages": [2, 3], "failed_pages": [2],
                "reviewed_pages": [], "covered_count": 1, "complete": False,
                "next_page_from": 2, "next_page_to": 3,
            },
            "created_at": "2026-11-20T10:00:00", "chunks": 2,
            "headings": ["测试章节"], "text_excerpt": "测试讲义文本",
            "index_status": "stale", "index_digest": None, "index_updated_at": None,
            "index_enabled": True, "notice": "",
        }

    documents_pattern = re.compile(r".*/api/documents$")
    ocr_pattern = re.compile(r".*/api/documents/77001/ocr$")
    job_get_pattern = re.compile(r".*/api/ai/jobs/9001$")

    def documents_handler(route):
        if route.request.method != "GET":
            route.continue_()
            return
        state["documents_calls"] += 1
        route.fulfill(status=200, content_type="application/json", body=json.dumps(
            {"items": [document_payload(state["ocr_done"])], "embedding_enabled": False},
            ensure_ascii=False))

    def ocr_handler(route):
        payload = route.request.post_data_json or {}
        ocr_requests.append(payload)
        page_from = int(payload.get("page_from") or 1)
        page_to = int(payload.get("page_to") or 3)
        if not payload.get("confirmed"):
            route.fulfill(status=200, content_type="application/json", body=json.dumps({
                "requires_confirmation": True, "document_id": 77001,
                "page_from": page_from, "page_to": page_to, "estimated_calls": 2,
                "notice": f"即将调用视觉模型 mock-vision 识别第 {page_from}–{page_to} 页"
                          "（共 2 次模型调用），确认后执行。",
            }, ensure_ascii=False))
            return
        route.fulfill(status=200, content_type="application/json", body=json.dumps({
            "async": True,
            "job": {"id": 9001, "task": "ocr", "status": "queued", "stage": "queued",
                    "created_at": "2026-11-20T10:00:00+00:00"},
            "document_id": 77001, "page_from": page_from, "page_to": page_to,
            "estimated_calls": 2,
        }, ensure_ascii=False))

    def job_get_handler(route):
        state["ocr_done"] = True
        route.fulfill(status=200, content_type="application/json", body=json.dumps({
            "id": 9001, "task": "ocr", "status": "completed", "stage": "done",
            "created_at": "2026-11-20T10:00:00+00:00", "partial_content": "",
            "result": {
                "document": document_payload(True),
                "ocr_pages": [
                    {"page": 2, "text": "第二页 OCR 文本", "state": "recognized"},
                    {"page": 3, "text": "第三页 OCR 文本", "state": "recognized"},
                ],
                "failed_pages": [], "notices": ["识别完成：第 2–3 页已保存。"],
            },
        }, ensure_ascii=False))

    try:
        ensure_login(page, base_url, USERNAME, PASSWORD)
        page.route(documents_pattern, documents_handler)
        page.route(ocr_pattern, ocr_handler)
        page.route(job_get_pattern, job_get_handler)
        page.goto(base_url + "/#/notes", wait_until="domcontentloaded")
        page.wait_for_selector("#notes-panel-notes", timeout=30000)
        # 注意：登录后 goto 同一个 URL 只改 hash 属于同文档导航，add_init_script 不会重跑；
        # 因此在这里直接注入假 SSE 脚本表，供 OCR 任务创建 EventSource 时读取。
        page.evaluate(
            """() => {
                window.__zhixuFakeSseScripts = {
                  '9001': [
                    [120, 'snapshot', {id: 9001, task: 'ocr', status: 'running', stage: 'ocr-1/2',
                      created_at: '2026-11-20T10:00:00+00:00'}],
                    [800, 'thinking', {text: '正在转写扫描页文本…', chars: 9}],
                    [1500, 'stage', {status: 'running', stage: 'ocr-2/2'}],
                    [3200, 'done', {status: 'completed'}]
                  ]
                };
            }"""
        )
        # 隔离库通常没有配置付费模型：直接注入一个视觉模型连接，走真实 picker/payload 逻辑。
        page.evaluate(
            """async () => {
                const st = await import('/static/js/core/state.js');
                st.state.providers = [{
                    id: 11, name: 'Mock 视觉连接', kind: 'openai',
                    base_url: 'https://mock.example.com/v1', has_api_key: true,
                    models: ['mock-vision'],
                    model_details: [{id: 101, model_id: 'mock-vision', display_name: 'mock-vision',
                        caps: {vision: true, json: true, stream: true}, enabled: true}],
                }];
                st.state.taskDefaults = {};
            }"""
        )
        page.get_by_role("tab", name="资料库").click()
        page.wait_for_selector("#notes-panel-docs .pg-doc-item", timeout=20000)
        page.locator("#notes-panel-docs .pg-doc-actions button").filter(
            has_text=re.compile("OCR")).first.click()
        page.wait_for_selector("dialog.modal-dialog", timeout=10000)
        dialog = page.locator("dialog.modal-dialog")
        number_inputs = dialog.locator("input[type=number]")
        number_inputs.nth(0).fill("2")
        number_inputs.nth(1).fill("3")
        page.get_by_role("button", name="确认页数并执行视觉 OCR").click()

        page.wait_for_function(
            """() => {
                const label = document.querySelector('.pg-ocr-job-mount .job-progress-label');
                return Boolean(label) && (label.textContent || '').includes('50%');
            }""", timeout=25000)
        progress_50 = page.evaluate(
            """() => {
                const label = document.querySelector('.pg-ocr-job-mount .job-progress-label');
                const stage = document.querySelector('.pg-ocr-job-mount .job-stage-line');
                return {
                    percent: label ? label.getAttribute('data-progress-percent') : null,
                    stage: stage ? stage.textContent : '',
                };
            }"""
        )
        assert_true(progress_50["percent"] == "50", f"OCR 进度百分比异常：{progress_50['percent']}")
        assert_true("识别扫描页" in progress_50["stage"],
                    f"OCR 阶段文案未展示逐页进度：{progress_50['stage']}")
        # thinking 事件：推理/视觉模型思考时展示「已思考 X 字」，不污染正文。
        page.wait_for_selector('.pg-ocr-job-mount .job-thinking:not([hidden])', timeout=10000)
        progress = page.evaluate(
            """() => {
                const mount = document.querySelector('.pg-ocr-job-mount');
                const label = mount?.querySelector('.job-progress-label');
                const stage = mount?.querySelector('.job-stage-line');
                return {
                    mount: Boolean(mount),
                    title: mount?.querySelector('.job-title')?.textContent || '',
                    stage: stage ? stage.textContent : '',
                    percent: label ? label.getAttribute('data-progress-percent') : null,
                    thinking_visible: Boolean(mount?.querySelector('.job-thinking:not([hidden])')),
                    thinking_text: mount?.querySelector('.job-thinking')?.textContent || '',
                };
            }"""
        )
        assert_true(progress["mount"], "OCR async:true 后未在对话框内挂载任务进度")
        assert_true("OCR" in progress["title"], f"OCR 任务卡标题异常：{progress['title']}")
        assert_true(progress["percent"] in ("50", "100"),
                    f"OCR 进度百分比异常：{progress['percent']}")
        assert_true(progress["thinking_visible"], "OCR 任务思考提示（thinking 事件）未展示")
        progress_shot = shot(page, out, "flows-ocr-job-progress.png", report)

        page.wait_for_function(
            """() => {
                const label = document.querySelector('.pg-ocr-job-mount .job-progress-label');
                return Boolean(label) && (label.textContent || '').includes('100%');
            }""", timeout=25000)
        page.wait_for_function(
            """() => {
                const dlg = document.querySelector('dialog.modal-dialog');
                return Boolean(dlg) && (dlg.innerText || '').includes('识别结果已按页合并保存');
            }""", timeout=30000)
        result = page.evaluate(
            """() => {
                const dlg = document.querySelector('dialog.modal-dialog');
                const docs = document.querySelector('#notes-panel-docs');
                return {
                    dialog_text: dlg ? (dlg.innerText || '') : '',
                    textareas: [...(dlg ? dlg.querySelectorAll('textarea') : [])].map(node => node.value).join('\\n'),
                    docs_text: docs ? (docs.innerText || '') : '',
                };
            }"""
        )
        assert_true("第二页 OCR 文本" in result["textareas"] and "第三页 OCR 文本" in result["textareas"],
                    f"OCR 完成后未展示逐页校对文本：{result['textareas'][:160]}")
        assert_true(state["documents_calls"] >= 2,
                    f"OCR 完成后未刷新资料列表（/api/documents 仅请求 {state['documents_calls']} 次）")
        assert_true("覆盖 3/3 页" in result["docs_text"],
                    f"资料卡覆盖度未刷新：{result['docs_text'][:200]}")
        assert_true("识别失败：第" not in result["docs_text"],
                    f"识别成功后资料卡仍残留失败页提示：{result['docs_text'][:200]}")
        confirmed = [item for item in ocr_requests if item.get("confirmed")]
        estimates = [item for item in ocr_requests if not item.get("confirmed")]
        assert_true(len(estimates) >= 1, "OCR 执行前缺少页数与费用确认请求")
        assert_true(len(confirmed) == 1, f"OCR 确认执行请求次数异常：{len(confirmed)}")
        assert_true(confirmed[0].get("page_from") == 2 and confirmed[0].get("page_to") == 3,
                    f"OCR 页码区间未按输入提交：{confirmed[0]}")
        detail = {
            "progress": progress,
            "documents_calls": state["documents_calls"],
            "ocr_requests": ocr_requests,
            "docs_excerpt": result["docs_text"][:200],
            "screenshot_progress": progress_shot,
        }
        detail["screenshot"] = shot(page, out, "flows-ocr-job-result.png", report)
        assert_true(not errors, f"OCR 异步任务检查出现 JS 异常：{errors[:2]}")
        return detail
    finally:
        try:
            page.unroute(documents_pattern)
            page.unroute(ocr_pattern)
            page.unroute(job_get_pattern)
        except Exception:  # noqa: BLE001 - 页面关闭后 unroute 失败可忽略
            pass
        page.close()
        ctx.close()


def check_admin_lazy_tabs(context, base_url: str, out, report) -> dict:
    """§3.3：内容管理三个分区按 Tab 懒加载、切换后缓存、操作后只局部刷新对应卡片。"""
    ctx = context.browser.new_context(viewport=DESKTOP_VIEWPORT, locale="zh-CN")
    page = ctx.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    requests = []
    page.on("request", lambda request: requests.append(
        {"url": request.url, "method": request.method}))
    state = {"registration_open": False}
    counts = {
        "content_issues": 0, "overview": 0, "review_queue": 0, "templates": 0,
        "community": 0, "allowed_hosts": 0, "eval_runs": 0,
        "registration_get": 0, "registration_post": 0, "health_ready": 0,
    }

    def fulfillment(route, body):
        route.fulfill(status=200, content_type="application/json",
                      body=json.dumps(body, ensure_ascii=False))

    def content_issues_handler(route):
        counts["content_issues"] += 1
        fulfillment(route, {"items": [], "total": 0, "has_more": False})

    def overview_handler(route):
        counts["overview"] += 1
        fulfillment(route, {"counts": {"users": 2}})

    def review_queue_handler(route):
        counts["review_queue"] += 1
        fulfillment(route, {"items": []})

    def templates_handler(route):
        counts["templates"] += 1
        fulfillment(route, {"items": []})

    def community_handler(route):
        counts["community"] += 1
        fulfillment(route, {"items": [], "enabled": True})

    def allowed_hosts_handler(route):
        counts["allowed_hosts"] += 1
        if route.request.method == "GET":
            fulfillment(route, {"presets": ["api.openai.com"], "env": [], "custom": []})
        else:
            fulfillment(route, {"ok": True})

    def eval_runs_handler(route):
        counts["eval_runs"] += 1
        fulfillment(route, {"items": []})

    def registration_handler(route):
        if route.request.method == "POST":
            counts["registration_post"] += 1
            payload = route.request.post_data_json or {}
            state["registration_open"] = payload.get("mode") == "open"
            fulfillment(route, {"ok": True, "mode": payload.get("mode") or "closed"})
            return
        counts["registration_get"] += 1
        fulfillment(route, {
            "mode": "open" if state["registration_open"] else "closed",
            "public_mode": False, "admin_ready": True,
            "admin_init_configured": False, "env_override": False,
        })

    def health_ready_handler(route):
        counts["health_ready"] += 1
        fulfillment(route, {
            "status": "ok", "generated_at": "2026-11-20T10:00:00+00:00",
            "affected_features": [],
            "checks": {
                "app_db": {"ok": True, "detail": "ok", "engine": "sqlite"},
                "bank": {"ok": True, "detail": "ok", "bank_version": "mock-bank"},
            },
        })

    def knowledge_points_handler(route):
        fulfillment(route, {"items": [{"id": 1, "subject": "数学", "name": "函数",
                                       "level": 1, "parent_id": None, "order": 1}]})

    def prompt_grid_handler(route):
        fulfillment(route, {"dimensions": {}, "subjects": []})

    routes = [
        (re.compile(r".*/api/admin/content-issues(\?.*)?$"), content_issues_handler),
        (re.compile(r".*/api/admin/overview$"), overview_handler),
        (re.compile(r".*/api/admin/kp-review-queue(\?.*)?$"), review_queue_handler),
        (re.compile(r".*/api/admin/prompt-templates$"), templates_handler),
        (re.compile(r".*/api/admin/community-explanations$"), community_handler),
        (re.compile(r".*/api/admin/allowed-hosts.*$"), allowed_hosts_handler),
        (re.compile(r".*/api/admin/eval-runs$"), eval_runs_handler),
        (re.compile(r".*/api/auth/admin/registration$"), registration_handler),
        (re.compile(r".*/api/health/ready(\?.*)?$"), health_ready_handler),
        (re.compile(r".*/api/knowledge-points$"), knowledge_points_handler),
        (re.compile(r".*/api/prompt-grid$"), prompt_grid_handler),
    ]
    try:
        ensure_login(page, base_url, ADMIN_USERNAME, ADMIN_PASSWORD)
        role = page.evaluate(
            """async () => {
                const st = await import('/static/js/core/state.js');
                return st.state.user?.role || '';
            }"""
        )
        if role != "admin":
            return {"skipped": True,
                    "reason": f"验收账号 ui-visual-check 角色为 {role}，内容管理懒加载检查需要管理员"}
        for pattern, handler in routes:
            page.route(pattern, handler)
        requests.clear()
        page.locator(".nav-item[data-route='admin']").click()
        page.wait_for_function(
            """() => {
                const panel = document.querySelector('#admin-panel-issues');
                return Boolean(panel) && (panel.innerText || '').includes('内容报错审核与校勘');
            }""", timeout=30000)
        page.wait_for_timeout(500)
        initial_paths = {urllib.parse.urlparse(item["url"]).path for item in requests}
        assert_true("/api/admin/content-issues" in initial_paths,
                    f"进入内容管理未加载校勘队列：{sorted(initial_paths)}")
        forbidden_initial = sorted(set(initial_paths) & {
            "/api/admin/overview", "/api/admin/allowed-hosts", "/api/admin/eval-runs",
            "/api/auth/admin/registration", "/api/health/ready",
            "/api/admin/kp-review-queue", "/api/admin/prompt-templates",
            "/api/admin/community-explanations", "/api/knowledge-points",
        })
        assert_true(not forbidden_initial,
                    f"初始只应加载校勘分区，却提前请求：{forbidden_initial}")
        lazy_issues_shot = shot(page, out, "flows-admin-lazy-issues.png", report)

        page.get_by_role("tab", name="题库与知识点").click()
        page.wait_for_function(
            """() => {
                const panel = document.querySelector('#admin-panel-content');
                return Boolean(panel) && (panel.innerText || '').includes('知识点体系管理');
            }""", timeout=30000)
        page.wait_for_timeout(500)
        content_paths = {urllib.parse.urlparse(item["url"]).path for item in requests}
        for path in ("/api/admin/overview", "/api/admin/kp-review-queue",
                     "/api/admin/prompt-templates", "/api/knowledge-points"):
            assert_true(path in content_paths,
                        f"切换到题库与知识点分区未加载 {path}：{sorted(content_paths)}")
        premature_ops = sorted(set(content_paths) & {
            "/api/admin/allowed-hosts", "/api/admin/eval-runs",
            "/api/auth/admin/registration", "/api/health/ready",
        })
        assert_true(not premature_ops,
                    f"切换题库分区时提前请求了运行与账号接口：{premature_ops}")

        page.get_by_role("tab", name="运行与账号").click()
        page.wait_for_function(
            """() => {
                const panel = document.querySelector('#admin-panel-ops');
                return Boolean(panel) && (panel.innerText || '').includes('服务就绪');
            }""", timeout=30000)
        page.wait_for_timeout(600)
        ops_paths = {urllib.parse.urlparse(item["url"]).path for item in requests}
        for path in ("/api/admin/allowed-hosts", "/api/admin/eval-runs",
                     "/api/auth/admin/registration", "/api/health/ready"):
            assert_true(path in ops_paths,
                        f"切换到运行与账号分区未加载 {path}：{sorted(ops_paths)}")
        ops_shot = shot(page, out, "flows-admin-lazy-ops.png", report)

        # 操作后局部刷新：开放注册只重发注册策略接口，不重发其它分区依赖。
        before = dict(counts)
        with page.expect_response(
                lambda response: "/api/auth/admin/registration" in response.url
                and response.request.method == "GET"):
            page.get_by_role("button", name="开放注册").click()
        page.wait_for_timeout(500)
        after = dict(counts)
        assert_true(after["registration_post"] == before["registration_post"] + 1,
                    f"开放注册未发出 POST：{after}")
        assert_true(after["registration_get"] > before["registration_get"],
                    f"开放注册后未局部刷新注册策略：{after}")
        unchanged = [
            key for key in ("allowed_hosts", "eval_runs", "health_ready", "overview",
                            "content_issues", "review_queue", "templates", "community")
            if after[key] != before[key]
        ]
        assert_true(not unchanged, f"操作后重发了其它卡片请求：{unchanged}")

        # 校勘筛选：只重发 content-issues，旧响应由序号/取消丢弃。
        page.get_by_role("tab", name="校勘").click()
        page.wait_for_timeout(400)
        before_filter = dict(counts)
        page.locator("#admin-panel-issues .pg-admin-filters select").first.select_option(
            label="待修订")
        page.wait_for_timeout(1200)
        after_filter = dict(counts)
        assert_true(after_filter["content_issues"] == before_filter["content_issues"] + 1,
                    f"校勘筛选未恰好触发一次队列刷新：{before_filter} -> {after_filter}")
        touched = [key for key in ("allowed_hosts", "eval_runs", "health_ready", "overview")
                   if after_filter[key] != before_filter[key]]
        assert_true(not touched, f"校勘筛选重发了其它分区请求：{touched}")

        detail = {
            "role": role,
            "initial_paths": sorted(initial_paths),
            "content_paths": sorted(content_paths),
            "ops_paths": sorted(ops_paths),
            "counts_before_ops_action": before,
            "counts_after_ops_action": after,
            "counts_after_issue_filter": after_filter,
            "screenshot_issues": lazy_issues_shot,
            "screenshot_ops": ops_shot,
        }
        detail["screenshot"] = shot(page, out, "flows-admin-lazy-filtered.png", report)
        assert_true(not errors, f"内容管理懒加载检查出现 JS 异常：{errors[:2]}")
        return detail
    finally:
        for pattern, _handler in routes:
            try:
                page.unroute(pattern)
            except Exception:  # noqa: BLE001 - 页面关闭后 unroute 失败可忽略
                pass
        page.close()
        ctx.close()


CHECKS = [
    ("markdown_sanitize", "markdown"),
    ("job_stream_cleanup", "stream"),
    ("busy_button_state", "busy"),
    ("build_banner", "banner"),
    ("formula_fallback", "formula"),
    ("cross_page_qid", "cross"),
    ("continuous_review", "review"),
    ("submit_keep_selection", "submit"),
    ("hidden_reveal_buttons", "reveal"),
    ("multi_filter_and_summary", "filter"),
    ("notes_composer_and_notes_state", "notes"),
    ("sidebar_note_wrap", "sidebar"),
    ("upload_state_and_method_badge", "upload"),
    ("settings_recovery_codes", "recovery"),
    ("settings_custom_provider", "settings-provider"),
    ("settings_task_defaults", "settings-defaults"),
    ("model_picker_persistence", "picker"),
    ("mobile_filters", "mobile-filters"),
    ("streaming_shell_stable", "stream-shell"),
    ("ocr_job_progress", "ocr-job"),
    ("admin_lazy_tabs", "admin-lazy"),
]

USERNAME = "ui-flow-check"
PASSWORD = "ui-flow-pass-123"


def run(args) -> int:
    global USERNAME, PASSWORD, _FLOW_REPORT
    USERNAME = args.username
    PASSWORD = args.password
    # Windows 控制台默认 GBK：题目/详情里的 emoji 会让 print 崩溃，统一改 UTF-8。
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001 - 非 TTY/旧 Python 环境忽略
            pass
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    report = {
        "base_url": args.base_url,
        "checks": [],
        "screenshots": [],
        "page_errors": [],
        "console_errors": [],
        "http_errors": [],
        "allowed_http": [],
        "allowed_console": [],
        "problems": [],
    }
    _FLOW_REPORT = report
    with sync_playwright() as playwright:
        browser = launch_browser(playwright)
        context = browser.new_context(viewport=DESKTOP_VIEWPORT, locale="zh-CN")
        context.on("page", lambda new_page: attach_observers(new_page, report))
        page = context.new_page()
        page_errors = report["page_errors"]
        current_route = {"name": "login"}
        page.on("pageerror", lambda error: page_errors.append(
            {"route": current_route["name"], "error": str(error)}))
        ensure_login(page, args.base_url, args.username, args.password)

        simple_checks = {
            "markdown_sanitize": check_markdown_sanitization,
            "job_stream_cleanup": check_job_stream_cleanup,
            "busy_button_state": check_busy_button_state,
        }
        context_checks = {
            "build_banner": lambda: check_build_banner(context, args.base_url, out, report),
            "formula_fallback": lambda: check_formula_fallback(context, args.base_url, out, report),
            "cross_page_qid": lambda: check_cross_page_qid(context, args.base_url, out, report),
            "continuous_review": lambda: check_continuous_review(context, args.base_url, out, report),
            "submit_keep_selection": lambda: check_submit_keep_selection(context, args.base_url, out, report),
            "hidden_reveal_buttons": lambda: check_hidden_reveal_buttons(context, args.base_url, out, report),
            "multi_filter_and_summary": lambda: check_multi_filter_and_summary(context, args.base_url, out, report),
            "notes_composer_and_notes_state": lambda: check_notes_composer_and_notes_state(
                context, args.base_url, out, report),
            "sidebar_note_wrap": lambda: check_sidebar_note_wrap(context, args.base_url, out, report),
            "upload_state_and_method_badge": lambda: check_upload_state_and_method_badge(
                context, args.base_url, out, report),
            "settings_recovery_codes": lambda: check_settings_recovery_codes(
                context, args.base_url, out, report),
            "settings_custom_provider": lambda: check_settings_custom_provider(
                context, args.base_url, out, report),
            "settings_task_defaults": lambda: check_settings_task_defaults(
                context, args.base_url, out, report),
            "model_picker_persistence": lambda: check_model_picker_persistence(
                context, args.base_url, out, report),
            "mobile_filters": lambda: check_mobile_filters(
                context, args.base_url, out, report),
            "streaming_shell_stable": lambda: check_streaming_shell_stable(
                context, args.base_url, out, report),
            "ocr_job_progress": lambda: check_ocr_job_progress(
                context, args.base_url, out, report),
            "admin_lazy_tabs": lambda: check_admin_lazy_tabs(
                context, args.base_url, out, report),
        }

        for name, short in CHECKS:
            current_route["name"] = name
            try:
                detail = simple_checks[name](page, out, report) if name in simple_checks \
                    else context_checks[name]()
                report["checks"].append({"check": name, "ok": True, "detail": detail})
                print(json.dumps({"check": name, "ok": True, "detail": detail}, ensure_ascii=False))
            except Exception as error:  # noqa: BLE001 - 每项失败都进报告，不中断整轮
                message = f"{type(error).__name__}: {error}"[:500]
                report["checks"].append({"check": name, "ok": False, "problem": message})
                report["problems"].append({"check": name, "problem": message})
                try:
                    if page.is_closed() is False:
                        shot(page, out, f"flows-failure-{short}.png", report)
                except Exception:
                    pass
                print(json.dumps({"check": name, "ok": False, "problem": message}, ensure_ascii=False))

        page.close()
        context.close()
        browser.close()

    if report["page_errors"]:
        report["problems"].append({
            "check": "-", "problem": "页面运行期间出现 JS 异常",
            "detail": report["page_errors"][:5],
        })
    if report["console_errors"]:
        report["problems"].append({
            "check": "-", "problem": "页面出现 console.error（已排除 401/403/409 门禁）",
            "detail": report["console_errors"][:5],
        })
    if report["http_errors"]:
        report["problems"].append({
            "check": "-", "problem": "页面出现非预期 4xx/5xx 响应（已排除 401/403/409 门禁）",
            "detail": report["http_errors"][:5],
        })
    report["ok"] = not report["problems"]
    (out / "summary-flows.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "ok": report["ok"],
        "problems": report["problems"],
        "out": str(out),
        "checks": [{"check": item["check"], "ok": item["ok"]} for item in report["checks"]],
    }, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="知序前端主流程与修复项验收（跨页跳题/连续复习/公式回退/净化/横幅等）")
    parser.add_argument("--base-url", default="http://127.0.0.1:8765")
    parser.add_argument("--username", default=USERNAME)
    parser.add_argument("--password", default=PASSWORD)
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    args = parser.parse_args(argv)
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
