"""广场（Phase 2 + Phase 3 质量运营）端到端验收：HTTP 全链路 + 可选 Playwright 页面冒烟。

覆盖场景（全部经公开 HTTP API，不直接读写数据库）：

1. 注册首个账号 = 管理员（本地模式）→ 注册作者 / 编辑 / 3 位评审员 / 2 位学科复核员；
2. 管理员授权广场角色（editor / reviewer / subject_reviewer，学科=数学）；
3. 作者创建 1 道个人题（paperlab ``POST /questions``，含题干/选项/答案/解析/知识点）；
4. ``POST /api/plaza/submissions`` 建投稿 → ``PATCH`` 填来源/授权声明（版本 +1）→ 送审；
5. 编辑初审 accept → 社区待核验（匿名公开可见、显著标注）；
6. 5 位评审员各自独立作答 + approve（≥5 人、≥4 通过、含 ≥2 学科复核）→ 自动 verified；
7. 编辑签发精选 → 匿名列表/详情可见「平台精选」；
8. 编辑撤回整条链 → 匿名列表不可见、详情 ``visible=false``；
9. 附加覆盖：身份认证申请/审核、投稿事件链、评审队列/我的评审/编辑台队列；
10. 权限负向：匿名不能投稿、作者不能签发、评审员不能看编辑台、重复评审/重复送审 409 等。

Phase 3（质量运营 / 互动 / 回滚 / 通知，均经 WS21 新增接口）：

11. 质量抽检：发布后编辑抽检 ``pass`` → 已抽检队列/抽样覆盖率指标；另一题抽检 ``issue`` + ``high``
    → 自动撤回（公开不可见、发布历史 withdrawn、作者与收藏者收到通知）；
12. 互动闭环：登录用户收藏 → 我的收藏 ``favorited/available`` 投影 → 撤回后收藏者通知、回滚后恢复通知；
    题目纠错/举报提交 → 编辑台 pending 队列 → ``resolve`` → 反馈人通知（重复处理 409、非编辑 403）；
    作者对 ``withdrawn`` 投稿申诉 → 编辑台申诉队列 → 处理 → 作者通知；
13. 回滚：同一投稿先发布 v1 → 撤回 → 改版 v2 重新评审发布 → ``rollback`` 恢复上一已发布版本 active，
    公开详情读回旧版本正文，发布历史/事件链/通知核对；
14. 通知中心：``GET /notifications`` unread → ``POST /notifications/read``（ids / all）→ 未读数下降；
15. 质量指标：编辑台 ``/admin/metrics`` 抽检问题率/发布撤回数/回滚次数/反馈积压口径，以及指标接口权限负向。

可选 UI 冒烟（默认开启，``--skip-ui`` 关闭；Playwright 不可用时自动降级为 warning）：
匿名访客壳 ``#/plaza`` 列表与详情（桌面 1440×900 + 手机 390×844，无 pageerror/console error、
无横向溢出、社区待核验/平台精选/已撤回标签、来源与授权声明、答案折叠）；作者 ``#/submission``；
评审员 ``#/review`` 队列与评审表单；编辑 ``#/plaza-admin`` 队列（待发布分区）。Phase 3 追加：
登录用户广场详情收藏/纠错/举报按钮与操作弹层（桌面）、匿名手机动作区引导；通知铃铛未读数与通知面板；
编辑台「抽检 / 反馈队列 / 质量指标」三页签分区渲染（桌面 + 手机抽检）；作者投稿台申诉卡与已处理记录。截图落盘。

UI 未接线或元素缺失只记录到 ``ui.problems``（不阻断 HTTP 链路判定），HTTP 全链路失败才 exit 1。

前置（每次运行均需设置的环境变量与隔离实例，详见 scripts/integration/README.md）：

```powershell
$env:STUDY_ALLOWED_HOSTS='127.0.0.1,localhost,[::1],testserver'
$env:STUDY_INDEX_PREWARM='0'
$env:STUDY_DATA_DIR=Join-Path $env:TEMP ("zhixu-plaza-accept-" + (Get-Random))
python -m uvicorn app.main:app --host 127.0.0.1 --port 8846
python scripts/integration/plaza_accept.py --base-url http://127.0.0.1:8846
```

注意：
- ``httpx`` 一律 ``trust_env=False``（Windows 系统代理会劫持 127.0.0.1 返回 502）；
- 管理员账号有三种来源（按顺序回退）：
  1. 默认：使用验收账号注册/登录，并断言 ``role=admin``（**本地模式全新 STUDY_DATA_DIR 下
     首个注册账号自动成为管理员**）；
  2. ``--admin-username`` / ``--admin-password``：实例已预先播种过管理员（例如先跑了
     demo / 整合验收账号）时，指定已存在管理员登录后再授权角色，不再依赖首个注册者；
  3. ``--admin-token``：公网模式实例未初始化管理员时，登录失败后调用一次性
     ``POST /api/auth/init-admin`` 初始化。
  默认模式若发现首个账号已不是管理员，会给出可读错误并提示改用 ``--admin-username``
  或全新隔离实例，不会堆栈崩溃；
- 可重复运行：账号已存在自动回退登录（登录优先，复跑不消耗注册额度），题目/投稿每次新建
  并在报告 ``data`` 中标记，重复签发/撤回/授权按幂等处理，不会因 409 失败；

产出：``screenshots/integration/plaza-accept-report.json``（``--json`` 可改）与
``screenshots/integration/plaza-*.png``（``--out`` 可改）。退出码 0/1。
"""
from __future__ import annotations

import argparse
import json
import re
import secrets
import sys
import time
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUT = ROOT / "screenshots" / "integration"
DEFAULT_REPORT = DEFAULT_OUT / "plaza-accept-report.json"
DEFAULT_BASE_URL = "http://127.0.0.1:8840"

PASSWORD = "plaza-accept-12345"
AUTH_COOKIE = "study_session"
SUBJECT = "数学"
GRADE = "高一"
#: WS28 抽检门禁状态/设置接口：实例显式 armed 时验收链路需临时关闭门禁（结束恢复原模式）。
QUALITY_GATE_PATH = "/api/plaza/admin/quality/gate"
STUDY_DATA_DIR_HINT = "请换一个全新的 STUDY_DATA_DIR 重启隔离实例后重试"
ADMIN_HINT = (
    "实例可能已预先播种了其它账号（首个注册账号已不是本脚本默认管理员）："
    "请用 --admin-username/--admin-password 指定**已存在且 role=admin** 的账号，"
    "或使用全新 STUDY_DATA_DIR 重启隔离实例；公网模式未初始化管理员时可用 --admin-token"
)

ADMIN_NAME = "广场验收管理员"
AUTHOR_NAME = "广场验收作者"
EDITOR_NAME = "广场验收编辑"
REVIEWER_NAMES = ("广场验收评审1", "广场验收评审2", "广场验收评审3")
SUBJECT_REVIEWER_NAMES = ("广场验收复核1", "广场验收复核2")

#: 来源与授权声明（服务端阻断项要求：license_basis / rights_scope / attribution / 可追溯出处）。
SOURCE = {
    "url": "https://example.com/zhixu/acceptance/math-q1",
    "page_no": "12",
    "question_no": "3",
    "original_author": "验收命题组",
    "license_basis": "原创并授权知序公开展示（验收自造数据）",
    "rights_scope": "允许公开题面、答案与解析；禁止商业再分发",
    "attribution": "署名：知序广场验收作者",
    "reference": "广场 Phase 2 端到端验收脚本自造数据",
}

SCENARIO = [
    "匿名访客读广场（公开只读）",
    "作者创建个人题并投稿（PATCH 来源声明 → 送审）",
    "编辑初审通过 → 社区待核验（匿名可见）",
    "5 位评审独立作答 + approve（含 2 位学科复核）→ 自动 verified",
    "编辑签发精选 → 匿名可见「平台精选」",
    "编辑撤回整条链 → 匿名列表不可见、详情 visible=false",
    "附加：身份认证申请/审核、事件链、评审队列与编辑台队列、权限负向",
    "Phase3：抽检 pass（队列/指标）与 issue+high 自动撤回（作者/收藏者通知）",
    "Phase3：收藏/纠错/举报/申诉闭环（编辑台 resolve → 通知）与回滚恢复上一已发布版本",
    "Phase3：通知中心 unread/read 与质量指标口径、编辑端权限负向",
    "UI 冒烟：匿名广场列表/详情（桌面+手机）、作者投稿台、评审台、编辑台",
    "Phase3 UI：详情收藏/纠错/举报、通知铃铛、编辑台抽检/反馈/指标、作者申诉卡",
]

#: 公开投影里绝不能出现的内部身份/内部 id 字段（JSON key 精确匹配）。
INTERNAL_FIELD_NAMES = (
    "user_id", "author_id", "reviewer_id", "username", "actor_id",
    "confirmed_by_user_id", "published_by", "created_by", "question_id",
)

#: 预期内 console 资源错误：匿名/登录首屏 /api/auth/me 必然 401，浏览器会打一条资源错误；
#: 前端对「official 来源是否可用」的能力探测固定会请求 `source=__zhixu_official_probe__` 并收到
#: 服务端 422（无效来源），也属预期（脚本按响应 URL 区分，不计为 UI 失败）。
_EXPECTED_401_RE = re.compile(r"Failed to load resource.*status of 401")
_EXPECTED_RESOURCE_RE = re.compile(r"Failed to load resource.*status of (\d+)")

#: 预期内非 2xx 资源：登录态探测（/api/auth/me 401）与 official 来源能力探测（422）。
_EXPECTED_RESOURCE_RULES = (
    (401, re.compile(r"/api/auth/me(?:\?|$)")),
    (422, re.compile(r"source=__zhixu_official_probe__")),
)

#: 抑制登录后「每日一签」自动弹层（HTML <dialog> 会拦截 Playwright 点击）。
_SUPPRESS_QUOTE_JS = """
try {
  const d = new Date();
  const pad = (n) => String(n).padStart(2, "0");
  localStorage.setItem("zhixu.quote.lastShown", `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`);
} catch (e) { /* ignore */ }
"""


class FlowFailure(RuntimeError):
    """HTTP 全链路断言失败（可读原因，终止后续步骤但保留报告）。"""


# ---------------------------------------------------------------------------
# 可选 Playwright UI 冒烟
# ---------------------------------------------------------------------------

class UiSmoke:
    """Playwright 页面冒烟：失败只进 ui.problems，不影响 HTTP 链路退出码。"""

    def __init__(self, base_url: str, out_dir: Path):
        self.base = base_url.rstrip("/")
        self.out = out_dir
        self.steps: list[dict] = []
        self.problems: list[str] = []
        self.warnings: list[str] = []
        self.screenshots: list[str] = []
        self.enabled = False
        self.skipped = False
        self._pw = None
        self._browser = None
        self._errors: dict[int, list[str]] = {}
        self._responses: dict[int, list[dict]] = {}
        self._zh = self._load_locale()

    # -- 生命周期 ---------------------------------------------------------
    def _load_locale(self) -> dict:
        path = ROOT / "web" / "locales" / "zh-CN.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:  # noqa: BLE001 - 语言包缺失时回退兜底文案
            return {}

    def t(self, key: str, fallback: str = "") -> str:
        value = self._zh.get(key)
        return str(value) if value else fallback

    def start(self) -> bool:
        try:
            from playwright.sync_api import sync_playwright
        except Exception as exc:  # noqa: BLE001 - 未安装 Playwright 时只降级
            self.skipped = True
            self.warnings.append(f"Playwright 不可用，跳过 UI 冒烟：{exc}")
            return False
        try:
            self._pw = sync_playwright().start()
            self._browser = self._launch()
        except Exception as exc:  # noqa: BLE001
            self.skipped = True
            self.warnings.append(f"未找到可用浏览器（msedge/chrome/chromium），跳过 UI 冒烟：{exc}")
            self.stop()
            return False
        self.enabled = True
        return True

    def _launch(self):
        for channel in ("msedge", "chrome", None):
            try:
                if channel:
                    return self._pw.chromium.launch(channel=channel, headless=True)
                return self._pw.chromium.launch(headless=True)
            except Exception:  # noqa: BLE001 - 依次尝试本机 Edge/Chrome/自带 Chromium
                continue
        raise RuntimeError("msedge / chrome / chromium 均不可用")

    def stop(self) -> None:
        try:
            if self._browser is not None:
                self._browser.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            if self._pw is not None:
                self._pw.stop()
        except Exception:  # noqa: BLE001
            pass
        self._browser = None
        self._pw = None

    # -- 页面 / 断言 / 截图 ----------------------------------------------
    def new_page(self, *, mobile: bool = False):
        viewport = {"width": 390, "height": 844} if mobile else {"width": 1440, "height": 900}
        context = self._browser.new_context(
            viewport=viewport, is_mobile=mobile, has_touch=mobile,
        )
        context.add_init_script(_SUPPRESS_QUOTE_JS)
        page = context.new_page()
        errors: list[str] = []
        responses: list[dict] = []
        page.on("pageerror", lambda exc: errors.append(f"pageerror: {exc}"))
        page.on("console", lambda msg: errors.append(f"console: {msg.text}")
                if msg.type == "error" else None)
        page.on("response", lambda response: responses.append({
            "status": int(response.status or 0), "url": str(response.url or ""),
        }))
        page.on("dialog", lambda dialog: dialog.accept())
        self._errors[id(page)] = errors
        self._responses[id(page)] = responses
        return context, page

    def check(self, name: str, condition, detail: str = "") -> bool:
        ok = bool(condition)
        self.steps.append({"name": name, "ok": ok, "detail": "" if ok else str(detail)[:500]})
        if not ok:
            self.problems.append(f"{name}: {detail or '断言失败'}")
        return ok

    def shot(self, page, name: str) -> str:
        self.out.mkdir(parents=True, exist_ok=True)
        path = self.out / f"plaza-{name}.png"
        page.screenshot(path=str(path), full_page=False)
        self.screenshots.append(str(path))
        return str(path)

    @staticmethod
    def overflow(page) -> int:
        try:
            return int(page.evaluate(
                "() => Math.max(0, (document.documentElement.scrollWidth || 0)"
                " - (window.innerWidth || 0))"))
        except Exception:  # noqa: BLE001
            return 0

    def dismiss_overlays(self, page) -> None:
        """兜底关闭「每日一签」等自动弹层，避免拦截后续点击。"""
        try:
            for _ in range(3):
                dialog = page.locator("dialog[open]").first
                if dialog.count() == 0 or not dialog.is_visible():
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
        except Exception:  # noqa: BLE001 - 弹层关闭失败不阻断验收
            pass

    def wait(self, locator, timeout: int = 20000, label: str = "") -> bool:
        try:
            locator.first.wait_for(state="attached", timeout=timeout)
            return True
        except Exception as exc:  # noqa: BLE001
            if label:
                self.check(label, False, f"等待元素超时：{str(exc)[:200]}")
            return False

    def goto(self, page, url: str, selector: str, label: str = "", timeout: int = 25000) -> None:
        try:
            page.goto(url, wait_until="load", timeout=30000)
            page.wait_for_selector(selector, timeout=timeout)
            page.wait_for_timeout(350)
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"{label or url} 未渲染出 {selector}：{str(exc)[:200]}") from exc

    def nav_hash(self, page, hash_value: str, selector: str, label: str = "",
                 timeout: int = 20000) -> None:
        try:
            page.evaluate("(h) => { if (location.hash !== h) location.hash = h; }", hash_value)
            page.wait_for_selector(selector, timeout=timeout)
            page.wait_for_timeout(350)
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"{label or hash_value} 未渲染出 {selector}：{str(exc)[:200]}") from exc

    def _expected_resource_error(self, status: int, url: str) -> bool:
        """按「状态码 + 请求路径」判断是否为预期内非 2xx（不算 UI 失败）。"""
        for expected_status, pattern in _EXPECTED_RESOURCE_RULES:
            if status == expected_status and pattern.search(url or ""):
                return True
        return False

    def assert_clean(self, page, label: str) -> None:
        errors = self._errors.pop(id(page), [])
        responses = self._responses.pop(id(page), [])
        page_errors = [item for item in errors if item.startswith("pageerror")]
        console_errors = [item for item in errors if not item.startswith("pageerror")]
        expected = []
        unexpected = []
        for item in console_errors:
            match = _EXPECTED_RESOURCE_RE.search(item)
            if match:
                status = int(match.group(1))
                if any(
                    response["status"] == status
                    and self._expected_resource_error(status, response["url"])
                    for response in responses
                ):
                    expected.append(item)
                    continue
                if status == 401 and _EXPECTED_401_RE.search(item):
                    expected.append(item)
                    continue
            unexpected.append(item)
        if expected:
            self.warnings.append(
                f"{label}: 预期内资源错误（/api/auth/me 401、official 能力探测 422）"
                f"共 {len(expected)} 条，不计为 UI 失败")
        problems = page_errors + unexpected
        self.check(f"{label}：无 pageerror / console error", not problems,
                   "；".join(problems[:3]))

    def login(self, username: str, *, mobile: bool = False):
        context, page = self.new_page(mobile=mobile)
        try:
            self.goto(page, self.base + "/", ".auth-form-card", "登录页")
            page.locator(".auth-form-card input[autocomplete='username']").fill(username)
            page.locator(".auth-form-card input[type='password']").fill(PASSWORD)
            page.locator(".auth-form-card button[type='submit']").click()
            page.wait_for_selector("#app-sidebar", timeout=25000)
            page.wait_for_timeout(600)
            self.dismiss_overlays(page)
        except Exception:
            context.close()
            raise
        return context, page

    # -- 报告 -------------------------------------------------------------
    def payload(self) -> dict:
        if not self.enabled:
            return {
                "enabled": False, "skipped": True, "ok": None,
                "steps": [], "problems": [], "warnings": list(self.warnings),
                "screenshots": [], "step_count": 0, "passed_steps": 0,
            }
        passed = sum(1 for item in self.steps if item.get("ok"))
        return {
            "enabled": True,
            "skipped": False,
            "ok": not self.problems,
            "steps": self.steps,
            "problems": self.problems,
            "warnings": self.warnings,
            "screenshots": self.screenshots,
            "step_count": len(self.steps),
            "passed_steps": passed,
        }


# ---------------------------------------------------------------------------
# UI 冒烟流程（每个函数只做页面断言；异常由调用方兜底记录，不阻断 HTTP）
# ---------------------------------------------------------------------------

def ui_flow_community(ui: UiSmoke, public_id: str, submission_id: int, marker: str) -> None:
    """社区待核验阶段：匿名列表/详情（桌面+手机）、作者投稿台、评审员评审台。"""
    pending_label = ui.t("plaza.status.communityPending", "社区待核验")
    community_source = ui.t("plaza.source.community", "社区来源")
    has_answer = ui.t("plaza.card.hasAnswer", "含答案")

    # 1) 匿名访客壳：列表 → 详情（桌面）
    context, page = ui.new_page()
    try:
        ui.goto(page, ui.base + "/#/plaza", ".plaza-page[data-plaza-view='list']", "广场列表（匿名）")
        ui.check("匿名访客壳渲染", page.locator("[data-guest-shell]").count() == 1)
        ui.check("匿名访客壳不渲染个人侧栏", page.locator("#app-sidebar").count() == 0)
        card = page.locator(f".plaza-card[data-public-id='{public_id}']")
        if ui.wait(card, 20000, "广场列表-当前投稿卡片"):
            card_text = card.inner_text()
            ui.check("列表卡片-社区待核验标签", pending_label in card_text, card_text[:200])
            ui.check("列表卡片-社区来源标签", community_source in card_text, card_text[:200])
            ui.check("列表卡片-含答案标签", has_answer in card_text, card_text[:160])
            overflow = ui.overflow(page)
            ui.check("广场列表（桌面）-无横向溢出", overflow <= 1, f"overflow={overflow}px")
            ui.shot(page, "list-community-desktop")
            card.locator("button").first.click()
            if ui.wait(page.locator(".plaza-page[data-plaza-view='detail']"), 20000, "广场详情页"):
                head_text = page.locator(".plaza-detail-head").inner_text()
                ui.check("详情-社区待核验标签", pending_label in head_text, head_text[:200])
                ui.check("详情-评审进度摘要", page.locator(".plaza-review-summary").count() >= 1)
                ui.check("详情-来源与授权声明卡", page.locator(".plaza-source-card").count() >= 1)
                page_text = page.locator(".plaza-page").inner_text()
                ui.check("详情-题干为本次验收投稿", marker in page_text, page_text[:200])
                ui.check("详情-答案默认折叠", page.locator("#plaza-answer-body.hide").count() == 1)
                page.locator(".plaza-answer button").first.click()
                page.wait_for_timeout(350)
                ui.check("详情-展开答案与解析", page.locator("#plaza-answer-body.hide").count() == 0)
                overflow = ui.overflow(page)
                ui.check("广场详情（桌面）-无横向溢出", overflow <= 1, f"overflow={overflow}px")
                ui.shot(page, "detail-community-desktop")
    finally:
        ui.assert_clean(page, "广场公开页（社区待核验，桌面）")
        context.close()

    # 2) 匿名访客壳：手机 390 列表 + 详情
    context, page = ui.new_page(mobile=True)
    try:
        ui.goto(page, ui.base + "/#/plaza", ".plaza-page[data-plaza-view='list']", "广场列表（匿名手机）")
        if ui.wait(page.locator(f".plaza-card[data-public-id='{public_id}']"), 20000,
                   "手机广场列表-当前投稿卡片"):
            overflow = ui.overflow(page)
            ui.check("广场列表（手机）-无横向溢出", overflow <= 1, f"overflow={overflow}px")
            ui.shot(page, "list-community-mobile")
        ui.goto(page, ui.base + "/#/plaza?id=" + public_id,
                ".plaza-page[data-plaza-view='detail']", "广场详情（匿名手机）")
        ui.check("手机详情-社区待核验标签",
                 pending_label in page.locator(".plaza-detail-head").inner_text())
        overflow = ui.overflow(page)
        ui.check("广场详情（手机）-无横向溢出", overflow <= 1, f"overflow={overflow}px")
        ui.shot(page, "detail-community-mobile")
    finally:
        ui.assert_clean(page, "广场公开详情（社区待核验，手机）")
        context.close()

    # 3) 作者投稿台：列表 → 详情（不可变快照 / 校验项 / 事件链）
    context, page = ui.login(AUTHOR_NAME)
    try:
        ui.nav_hash(page, "#/submission", ".plzw-page[data-page='plaza-submission']", "作者投稿台")
        item = page.locator(f".plzw-item[data-submission-id='{submission_id}']")
        if ui.wait(item, 20000, "投稿台-本次投稿条目"):
            item_text = item.inner_text()
            ui.check("投稿台-状态为社区待核验",
                     ui.t("plaza.submission.status.community_review", "社区待核验") in item_text,
                     item_text[:200])
            ui.shot(page, "submission-list-community")
            item.locator("button").first.click()
            if ui.wait(page.locator(".plzw-grid"), 20000, "投稿详情页"):
                ui.check("投稿详情-不可变快照与校验项", page.locator(".plzw-checks").count() >= 1)
                ui.check("投稿详情-事件链渲染", page.locator(".plzw-timeline").count() >= 1)
                ui.check("投稿详情-来源声明可编辑区", page.locator(".plzw-checkbox").count() >= 1)
                ui.shot(page, "submission-detail-community")
    finally:
        ui.assert_clean(page, "作者投稿台（社区待核验）")
        context.close()

    # 4) 评审员评审台：队列条目 → 评审表单（独立作答 / 四类结论）
    context, page = ui.login(REVIEWER_NAMES[0])
    try:
        ui.nav_hash(page, "#/review", ".plzw-page[data-page='plaza-review']", "评审台")
        ui.check("评审台-页签渲染", page.locator(".pg-tab").count() >= 2,
                 f"tabs={page.locator('.pg-tab').count()}")
        item = page.locator(f".plzw-item[data-submission-id='{submission_id}']")
        if ui.wait(item, 20000, "评审台-本次待评审条目"):
            item.locator("button").first.click()
            if ui.wait(page.locator("[data-review-form='true']"), 20000, "评审台-评审表单"):
                form = page.locator("[data-review-form='true']")
                radios = page.locator(
                    "[data-review-form='true'] input[type='radio']").count()
                ui.check("评审表单-四类结论单选", radios >= 4, f"radios={radios}")
                textareas = form.locator("textarea").count()
                ui.check("评审表单-独立作答与原因输入", textareas >= 2,
                         f"textareas={textareas}")
                ui.shot(page, "review-form")
    finally:
        ui.assert_clean(page, "评审台（社区待核验）")
        context.close()


def ui_flow_editor_queue(ui: UiSmoke, submission_id: int, marker: str) -> None:
    """核验通过阶段：编辑台「待发布」队列应包含本次投稿且显示评审阈值。"""
    context, page = ui.login(EDITOR_NAME)
    try:
        ui.nav_hash(page, "#/plaza-admin", ".plzw-page[data-page='plaza-admin']", "编辑台")
        ui.check("编辑台-页签渲染", page.locator(".pg-tab").count() >= 4,
                 f"tabs={page.locator('.pg-tab').count()}")
        ui.check("编辑台-队列接口就绪（无 notReady 占位）",
                 page.locator("[data-queue-not-ready]").count() == 0)
        release_tab = page.locator("#plaza-admin-tab-release")
        if ui.wait(release_tab, 15000, "编辑台-待发布页签"):
            release_tab.click()
            item = page.locator(f".plzw-item[data-submission-id='{submission_id}']")
            if ui.wait(item, 20000, "编辑台-待发布队列条目"):
                item_text = item.inner_text()
                ui.check("待发布队列-本次投稿可见", marker in item_text, item_text[:200])
                ui.check("待发布队列-评审阈值展示",
                         ui.t("plaza.admin.queue.openPublish", "签发这道") in item_text,
                         item_text[:200])
                ui.shot(page, "admin-release-queue")
    finally:
        ui.assert_clean(page, "编辑台（待发布队列）")
        context.close()


def ui_flow_featured(ui: UiSmoke, public_id: str, marker: str) -> None:
    """签发精选后：匿名列表/详情显示「平台精选」与精选来源标签（桌面+手机）。"""
    featured_label = ui.t("plaza.status.featured", "平台精选")
    featured_source = ui.t("plaza.source.featured", "平台精选")
    context, page = ui.new_page()
    try:
        ui.goto(page, ui.base + "/#/plaza", ".plaza-page[data-plaza-view='list']", "广场列表（精选）")
        card = page.locator(f".plaza-card[data-public-id='{public_id}']")
        if ui.wait(card, 20000, "精选列表-当前投稿卡片"):
            card_text = card.inner_text()
            ui.check("精选列表-平台精选状态标签", featured_label in card_text, card_text[:200])
            ui.check("精选列表-平台精选来源标签", featured_source in card_text, card_text[:200])
            overflow = ui.overflow(page)
            ui.check("精选列表-无横向溢出", overflow <= 1, f"overflow={overflow}px")
            ui.shot(page, "list-featured-desktop")
            card.locator("button").first.click()
            if ui.wait(page.locator(".plaza-page[data-plaza-view='detail']"), 20000, "精选详情页"):
                head_text = page.locator(".plaza-detail-head").inner_text()
                ui.check("精选详情-平台精选标签", featured_label in head_text, head_text[:200])
                ui.check("精选详情-来源与授权声明卡", page.locator(".plaza-source-card").count() >= 1)
                ui.check("精选详情-正文为本次投稿",
                         marker in page.locator(".plaza-page").inner_text())
                overflow = ui.overflow(page)
                ui.check("精选详情-无横向溢出", overflow <= 1, f"overflow={overflow}px")
                ui.shot(page, "detail-featured-desktop")
    finally:
        ui.assert_clean(page, "广场公开页（平台精选，桌面）")
        context.close()

    # 手机详情
    context, page = ui.new_page(mobile=True)
    try:
        ui.goto(page, ui.base + "/#/plaza?id=" + public_id,
                ".plaza-page[data-plaza-view='detail']", "精选详情（匿名手机）")
        ui.check("手机精选详情-平台精选标签",
                 featured_label in page.locator(".plaza-detail-head").inner_text())
        overflow = ui.overflow(page)
        ui.check("手机精选详情-无横向溢出", overflow <= 1, f"overflow={overflow}px")
        ui.shot(page, "detail-featured-mobile")
    finally:
        ui.assert_clean(page, "广场公开详情（平台精选，手机）")
        context.close()


def ui_flow_withdrawn(ui: UiSmoke, public_id: str, marker: str) -> None:
    """撤回后：匿名旧链接显示清晰撤回状态，且不回显任何受限正文。"""
    withdrawn_label = ui.t("plaza.status.withdrawn", "已撤回")
    unavailable_title = ui.t("plaza.detail.unavailableTitle", "内容暂不可见")
    for mobile, suffix in ((False, "desktop"), (True, "mobile")):
        context, page = ui.new_page(mobile=mobile)
        try:
            ui.goto(page, ui.base + "/#/plaza?id=" + public_id,
                    ".plaza-page[data-plaza-view='detail']", f"撤回详情页（{suffix}）")
            ui.check(f"撤回详情（{suffix}）-隐藏态标记",
                     page.locator(".plaza-detail-head[data-plaza-hidden='true']").count() == 1)
            page_text = page.locator(".plaza-page").inner_text()
            ui.check(f"撤回详情（{suffix}）-已撤回标签", withdrawn_label in page_text, page_text[:200])
            ui.check(f"撤回详情（{suffix}）-内容暂不可见提示", unavailable_title in page_text,
                     page_text[:200])
            ui.check(f"撤回详情（{suffix}）-不回显题干", marker not in page_text, "撤回后仍回显题干")
            overflow = ui.overflow(page)
            ui.check(f"撤回详情（{suffix}）-无横向溢出", overflow <= 1, f"overflow={overflow}px")
            ui.shot(page, f"detail-withdrawn-{suffix}")
        finally:
            ui.assert_clean(page, f"广场公开详情（已撤回，{suffix}）")
            context.close()


# ---------------------------------------------------------------------------
# Phase 3 UI 冒烟（收藏/纠错/举报、通知铃铛、编辑台质量运营分区、作者申诉卡）
# ---------------------------------------------------------------------------

def ui_flow_phase3_detail(ui: UiSmoke, public_id: str, marker: str) -> None:
    """登录用户广场详情：收藏/纠错/举报按钮、收藏状态与举报弹层；匿名手机动作区。"""
    context, page = ui.login(REVIEWER_NAMES[0])
    try:
        ui.goto(page, ui.base + "/#/plaza?id=" + public_id,
                ".plaza-page[data-plaza-view='detail']", "广场详情（Phase3 登录）")
        actions = page.locator("[data-plaza-member-actions]")
        if ui.wait(actions, 20000, "广场详情-登录用户动作区"):
            favorite = actions.locator("[data-plaza-action='favorite']").first
            ui.check("详情-收藏/纠错/举报按钮齐全",
                     actions.locator("[data-plaza-action='favorite']").count() == 1
                     and actions.locator("[data-plaza-action='issue']").count() == 1
                     and actions.locator("[data-plaza-action='report']").count() == 1,
                     "动作区按钮缺失或重复")
            state = favorite.get_attribute("data-favorited")
            ui.check("详情-收藏按钮为已收藏状态", state == "true", f"data-favorited={state}")
            ui.check("详情-正文为 Phase3 回滚后的旧版本正文",
                     marker in page.locator(".plaza-page").inner_text())
            overflow = ui.overflow(page)
            ui.check("Phase3 广场详情（桌面）-无横向溢出", overflow <= 1, f"overflow={overflow}px")
            ui.shot(page, "detail-phase3-actions-desktop")
            actions.locator("[data-plaza-action='report']").first.click()
            dialog = page.locator("[data-plaza-action-dialog]")
            if ui.wait(dialog, 15000, "广场详情-举报弹层"):
                ui.check("举报弹层渲染", dialog.is_visible())
                ui.shot(page, "detail-phase3-report-dialog")
                page.keyboard.press("Escape")
                page.wait_for_timeout(300)
    finally:
        ui.assert_clean(page, "广场详情（Phase3 登录，桌面）")
        context.close()

    context, page = ui.new_page(mobile=True)
    try:
        ui.goto(page, ui.base + "/#/plaza?id=" + public_id,
                ".plaza-page[data-plaza-view='detail']", "广场详情（Phase3 匿名手机）")
        guest = page.locator("[data-plaza-guest-actions]")
        if ui.wait(guest, 20000, "广场详情-匿名动作区"):
            ui.check("匿名手机-收藏/纠错/举报引导按钮",
                     guest.locator("[data-plaza-action]").count() >= 3,
                     f"buttons={guest.locator('[data-plaza-action]').count()}")
        overflow = ui.overflow(page)
        ui.check("Phase3 广场详情（手机）-无横向溢出", overflow <= 1, f"overflow={overflow}px")
        ui.shot(page, "detail-phase3-actions-mobile")
    finally:
        ui.assert_clean(page, "广场详情（Phase3 匿名，手机）")
        context.close()


def ui_flow_notifications(ui: UiSmoke, username: str) -> None:
    """通知铃铛：未读数 > 0、点击打开通知面板并渲染条目。"""
    context, page = ui.login(username)
    try:
        wrap = page.locator("[data-plaza-notify-slot] .plaza-notify-wrap")
        if ui.wait(wrap, 20000, "通知铃铛"):
            try:
                page.wait_for_function(
                    "() => { const el = document.querySelector('.plaza-notify-wrap');"
                    " return !!el && Number(el.dataset.unread || 0) > 0; }",
                    timeout=20000)
            except Exception:  # noqa: BLE001 - 未读数不足时仍记录实测值
                pass
            unread = int(page.evaluate(
                "() => Number(document.querySelector('.plaza-notify-wrap')?.dataset.unread || 0)"))
            ui.check("通知铃铛未读数 > 0", unread > 0, f"unread={unread}")
            ui.check("通知铃铛渲染", page.locator("[data-plaza-notification-bell]").count() == 1)
            ui.shot(page, "notify-bell-desktop")
            bell = page.locator("[data-plaza-notification-bell]").first
            if bell.count() == 1:
                bell.click()
                panel = page.locator("[data-plaza-notification-panel]")
                if ui.wait(panel, 15000, "通知面板"):
                    items = panel.locator(".plaza-notify-item").count()
                    ui.check("通知面板渲染条目", panel.is_visible() and items > 0,
                             f"items={items}")
                    ui.shot(page, "notify-panel-desktop")
                    page.keyboard.press("Escape")
                    page.wait_for_timeout(300)
    finally:
        ui.assert_clean(page, "通知中心（桌面）")
        context.close()


def ui_flow_admin_phase3(ui: UiSmoke) -> None:
    """编辑台 Phase 3：抽检 / 反馈队列 / 质量指标三个分区渲染（桌面；手机抽检）。"""
    context, page = ui.login(EDITOR_NAME)
    try:
        page.goto(ui.base + "/#/plaza-admin?tab=quality", wait_until="load", timeout=30000)
        page.wait_for_selector(".plzw-page[data-page='plaza-admin']", timeout=25000)
        page.wait_for_timeout(400)
        quality_tab = page.locator("#plaza-admin-tab-quality")
        if ui.wait(quality_tab, 15000, "编辑台-抽检页签"):
            quality_tab.click()
            section = page.locator("[data-quality-section]")
            if ui.wait(section, 20000, "编辑台-抽检分区"):
                ui.check("抽检分区渲染", section.is_visible())
                ui.check("抽检队列无未就绪占位",
                         page.locator("[data-quality-not-ready]").count() == 0)
                page.wait_for_timeout(600)
                ui.check("抽检分区展示条目",
                         page.locator("[data-quality-item]").count() >= 1,
                         f"items={page.locator('[data-quality-item]').count()}")
                ui.shot(page, "admin-quality-desktop")

        ops_tab = page.locator("#plaza-admin-tab-ops")
        if ui.wait(ops_tab, 15000, "编辑台-反馈队列页签"):
            ops_tab.click()
            if ui.wait(page.locator("[data-ops-section]"), 20000, "编辑台-反馈队列分区"):
                page.wait_for_timeout(600)
                ui.check("反馈队列三个分区渲染",
                         page.locator("[data-ops-section]").count() == 3,
                         f"sections={page.locator('[data-ops-section]').count()}")
                ui.check("反馈队列无未就绪占位",
                         page.locator("[data-ops-not-ready]").count() == 0)
                ui.shot(page, "admin-ops-desktop")

        metrics_tab = page.locator("#plaza-admin-tab-metrics")
        if ui.wait(metrics_tab, 15000, "编辑台-质量指标页签"):
            metrics_tab.click()
            if ui.wait(page.locator("[data-metrics-section]"), 20000, "编辑台-质量指标分区"):
                page.wait_for_timeout(600)
                ui.check("质量指标分区渲染",
                         page.locator("[data-metrics-section]").first.is_visible())
                ui.check("质量指标无未就绪占位",
                         page.locator("[data-metrics-not-ready]").count() == 0)
                cards = page.locator("[data-metrics-grid] > *").count()
                ui.check("质量指标卡片渲染", cards >= 6, f"cards={cards}")
                ui.shot(page, "admin-metrics-desktop")
    finally:
        ui.assert_clean(page, "编辑台（Phase3 抽检/反馈/指标，桌面）")
        context.close()

    context, page = ui.login(EDITOR_NAME, mobile=True)
    try:
        page.goto(ui.base + "/#/plaza-admin?tab=quality", wait_until="load", timeout=30000)
        page.wait_for_selector(".plzw-page[data-page='plaza-admin']", timeout=25000)
        page.wait_for_timeout(400)
        quality_tab = page.locator("#plaza-admin-tab-quality")
        if ui.wait(quality_tab, 15000, "编辑台-抽检页签（手机）"):
            quality_tab.click()
        if ui.wait(page.locator("[data-quality-section]"), 20000, "编辑台-抽检分区（手机）"):
            overflow = ui.overflow(page)
            ui.check("Phase3 编辑台（手机）-无横向溢出", overflow <= 1, f"overflow={overflow}px")
            ui.shot(page, "admin-quality-mobile")
    finally:
        ui.assert_clean(page, "编辑台（Phase3 抽检，手机）")
        context.close()


def ui_flow_appeal(ui: UiSmoke, submission_id: int) -> None:
    """作者投稿台：申诉卡与已处理申诉记录。"""
    resolved_label = ui.t("plaza.appeal.status.resolved", "已处理")
    context, page = ui.login(AUTHOR_NAME)
    try:
        ui.nav_hash(page, f"#/submission?id={submission_id}",
                    ".plzw-page[data-page='plaza-submission']", "作者投稿台（申诉）")
        card = page.locator("[data-notify-appeal]")
        if ui.wait(card, 20000, "作者投稿台-申诉卡"):
            ui.check("申诉卡渲染", card.is_visible())
            if ui.wait(page.locator("[data-appeal-history]"), 15000, "申诉历史"):
                page.wait_for_timeout(400)
                card_text = card.inner_text()
                ui.check("申诉历史含已处理记录",
                         page.locator("[data-appeal-id]").count() >= 1
                         and resolved_label in card_text,
                         card_text[:200])
            ui.shot(page, "submission-appeal-desktop")
    finally:
        ui.assert_clean(page, "作者投稿台（申诉，桌面）")
        context.close()


# ---------------------------------------------------------------------------
# HTTP 全链路验收
# ---------------------------------------------------------------------------

class _ClientProxy:
    """httpx.Client 轻代理：无论调用方写 ``client.request(...)`` 还是
    ``flow.request(client, ...)``，都统一走 ``PlazaAccept.request`` 做 API 覆盖统计
    与可读的连接错误包装。"""

    def __init__(self, owner: "PlazaAccept", raw: httpx.Client):
        self._owner = owner
        self._raw = raw

    def request(self, method: str, path: str, **kwargs):
        return self._owner.request(self._raw, method, path, **kwargs)

    def __getattr__(self, name):
        return getattr(self._raw, name)


class PlazaAccept:
    def __init__(self, base_url: str, out_dir: Path, report_path: Path, ui: UiSmoke | None,
                 run_id: str, *, admin_username: str = ADMIN_NAME,
                 admin_password: str = PASSWORD, admin_username_explicit: bool = False,
                 admin_token: str = "", skip_ui: bool = False):
        self.base = base_url.rstrip("/")
        self.out = out_dir
        self.report_path = report_path
        self.ui = ui
        self.run_id = run_id
        self.marker = f"广场验收{run_id}"
        #: 管理员来源：默认「首个注册账号=管理员」；也可用 --admin-username 指定已存在管理员，
        #: 或用 --admin-token 走一次性 POST /api/auth/init-admin 初始化（公网模式）。
        self.admin_username = admin_username
        self.admin_password = admin_password
        self.admin_username_explicit = admin_username_explicit
        self.admin_token = admin_token
        self.admin_source = "default"
        self.cli = {
            "admin_username": admin_username,
            "admin_username_explicit": admin_username_explicit,
            "admin_token_used": bool(admin_token),
            "skip_ui": skip_ui,
        }
        #: 抽检门禁兼容：原状态快照 / 是否由本脚本临时关闭（结束前恢复）。
        self.gate_origin: dict | None = None
        self.gate_disabled = False
        self.clients: dict[str, httpx.Client] = {}
        self.api_calls: dict[str, dict] = {}
        self.data: dict = {
            "run_id": run_id,
            "marker": self.marker,
            "note": "每次运行新建个人题与投稿（不复用旧投稿，避免 409）；账号已存在时回退登录。",
            "accounts": {},
            "question_id": None,
            "submission_id": None,
            "public_id": None,
            "versions": {},
        }
        self.report: dict = {
            "ok": False,
            "base_url": self.base,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "run_id": run_id,
            "scenario": SCENARIO,
            "steps": [],
            "api": {},
            "ui": {},
            "warnings": [],
            "problems": [],
            "data": self.data,
            "exit_code": 1,
        }
        self._started = 0.0

    # -- 基础工具 ---------------------------------------------------------
    def warning(self, message: str) -> None:
        if message not in self.report["warnings"]:
            self.report["warnings"].append(message)

    def new_client(self) -> _ClientProxy:
        # trust_env=False：本机 Windows 系统代理会劫持 127.0.0.1，导致 502。
        raw = httpx.Client(base_url=self.base, trust_env=False, timeout=30.0)
        return _ClientProxy(self, raw)

    def close(self) -> None:
        for client in self.clients.values():
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass
        self.clients.clear()

    def relax_session_cookie(self, client) -> None:
        """公网模式（STUDY_PUBLIC_MODE=1）会话 Cookie 带 ``Secure``，在本机 ``http://`` 验收时
        httpx 按规范不会回传，表现为登录 200 后 /api/auth/me 仍 401。这里把已收到的会话 Cookie
        降级为普通 Cookie 仅用于本机验收；真实 HTTPS 部署不受影响。"""
        if not self.base.lower().startswith("http://"):
            return
        raw = client._raw if isinstance(client, _ClientProxy) else client
        try:
            cookies = list(raw.cookies.jar)
        except Exception:  # noqa: BLE001
            return
        for cookie in cookies:
            if cookie.name != AUTH_COOKIE:
                continue
            try:
                raw.cookies.set(AUTH_COOKIE, cookie.value,
                                domain=cookie.domain or "127.0.0.1",
                                path=cookie.path or "/")
            except Exception:  # noqa: BLE001
                try:
                    raw.cookies.set(AUTH_COOKIE, cookie.value)
                except Exception:  # noqa: BLE001
                    pass


    @staticmethod
    def _snippet(response: httpx.Response, limit: int = 400) -> str:
        try:
            body = response.json()
        except ValueError:
            return response.text[:limit]
        if isinstance(body, dict):
            detail = body.get("detail")
            if isinstance(detail, str):
                return detail[:limit]
            if detail is not None:
                body = detail
        try:
            return json.dumps(body, ensure_ascii=False)[:limit]
        except (TypeError, ValueError):
            return str(body)[:limit]

    @staticmethod
    def _template(path: str) -> str:
        path_only = path.split("?")[0]
        path_only = re.sub(r"/plz-[0-9a-f]+", "/{public_id}", path_only)
        path_only = re.sub(r"/\d+(?=/|$)", "/{id}", path_only)
        return path_only

    @staticmethod
    def _forbidden_fields(text: str) -> list[str]:
        """公开响应文本中出现的内部身份/内部 id 字段名（JSON key 精确匹配）。"""
        return [key for key in INTERNAL_FIELD_NAMES
                if re.search(r'"%s"\s*:' % re.escape(key), text)]

    def _note_api(self, method: str, path: str, status: int) -> None:
        template = self._template(path)
        key = f"{method} {template}"
        item = self.api_calls.setdefault(
            key, {"method": method, "path": template, "calls": 0, "statuses": []})
        item["calls"] += 1
        status_text = str(status)
        if status_text not in item["statuses"]:
            item["statuses"].append(status_text)

    def request(self, client, method: str, path: str, *,
                data=None, params=None, timeout: float = 30.0,
                **extra) -> httpx.Response:
        raw = client._raw if isinstance(client, _ClientProxy) else client
        body = data if data is not None else extra.get("json")
        try:
            response = raw.request(method, path, json=body, params=params, timeout=timeout)
        except httpx.TimeoutException as exc:
            raise FlowFailure(
                f"请求超时：{method} {path}（{self.base} 30s 无响应，请确认隔离实例未卡住）") from exc
        except httpx.RequestError as exc:
            raise FlowFailure(
                f"无法连接实例：{method} {path} → {exc.__class__.__name__}；"
                f"请确认已用 --base-url 指定的地址启动隔离实例（当前 {self.base}）") from exc
        self._note_api(method, path, response.status_code)
        return response

    def expect(self, response: httpx.Response, statuses, label: str) -> httpx.Response:
        if isinstance(statuses, int):
            statuses = (statuses,)
        if response.status_code not in statuses:
            raise FlowFailure(
                f"{label}：期望 HTTP {tuple(statuses)}，实际 {response.status_code}；"
                f"响应：{self._snippet(response)}")
        return response

    def payload(self, response: httpx.Response, label: str):
        try:
            body = response.json()
        except ValueError as exc:
            raise FlowFailure(
                f"{label}：响应不是合法 JSON（HTTP {response.status_code}）："
                f"{response.text[:200]}") from exc
        if not isinstance(body, (dict, list)):
            raise FlowFailure(f"{label}：响应结构异常（{type(body).__name__}）")
        return body

    def step(self, name: str, checks: list, *, started: float | None = None,
             data: dict | None = None) -> None:
        """记录一个验收步骤；任一断言失败即抛 FlowFailure（附可读失败原因）。"""
        started = started if started is not None else self._started
        elapsed = round((time.time() - started) * 1000, 1) if started else 0.0
        normalized = []
        failures = []
        for item in checks:
            label = str(item[0])
            ok = bool(item[1])
            detail = str(item[2]) if len(item) > 2 and item[2] else ""
            normalized.append({"label": label, "ok": ok, "detail": "" if ok else detail})
            if not ok:
                failures.append((label, detail))
        entry = {
            "name": name,
            "ok": not failures,
            "status": "passed" if not failures else "failed",
            "elapsed_ms": elapsed,
            "checks": normalized,
        }
        if data:
            entry["data"] = data
        self.report["steps"].append(entry)
        if failures:
            detail = "；".join(
                f"{label}（{extra}）" if extra else label for label, extra in failures)
            self.report["problems"].append(f"{name}: {detail}")
            raise FlowFailure(f"{name}: {detail}")

    # -- UI 钩子（不阻断 HTTP） ------------------------------------------
    def ui_hook(self, label: str, fn, *args) -> None:
        if self.ui is None or not self.ui.enabled:
            return
        try:
            fn(self.ui, *args)
        except Exception as exc:  # noqa: BLE001 - UI 失败只记录，不影响 HTTP 链路
            traceback.print_exc()
            message = f"UI 冒烟「{label}」异常中断：{exc}"
            self.ui.problems.append(message)
            self.warning(message)

    # -- 场景步骤 ---------------------------------------------------------
    def prepare_accounts(self) -> None:
        # 1) 管理员：默认首个注册账号=管理员；也支持 --admin-username 指定已存在管理员，
        #    或 --admin-token 走一次性 /api/auth/init-admin（公网模式）。
        started = time.time()
        try:
            admin_client, admin_user, admin_mode = self._prepare_admin()
        except FlowFailure as exc:
            self.step("准备管理员账号（首个注册账号或 --admin-username 指定）",
                      [("管理员权限就绪（role=admin）", False, str(exc))],
                      started=started)
            raise
        self.clients["admin"] = admin_client
        admin_role = str(admin_user.get("role") or "")
        self.data["accounts"]["admin"] = {
            "username": self.admin_username,
            "user_id": admin_user.get("id"),
            "role": admin_role,
            "mode": admin_mode,
        }
        admin_ok = admin_role == "admin"
        if self.admin_source == "default":
            admin_detail = (
                f"实际 role={admin_role}；实例可能已被其它账号抢先注册（预先播种）。{ADMIN_HINT}")
        else:
            admin_detail = f"实际 role={admin_role}；{ADMIN_HINT}"
        self.step("准备管理员账号（首个注册账号或 --admin-username 指定）",
                  [("管理员权限就绪（role=admin）", admin_ok, admin_detail)],
                  started=started,
                  data={"username": self.admin_username, "mode": admin_mode,
                        "source": self.admin_source, "role": admin_role})
        self.data["admin_source"] = self.admin_source
        self.data["admin"] = self.data["accounts"]["admin"]

        # 2) 作者 / 编辑 / 3 评审 / 2 学科复核（已存在时回退登录，复跑不消耗注册额度）。
        started = time.time()
        specs = [
            ("author", AUTHOR_NAME),
            ("editor", EDITOR_NAME),
            *[(f"reviewer{i + 1}", name) for i, name in enumerate(REVIEWER_NAMES)],
            *[(f"subject_reviewer{i + 1}", name)
              for i, name in enumerate(SUBJECT_REVIEWER_NAMES)],
        ]
        checks = []
        for key, username in specs:
            client, user, mode = self._ensure_account(username)
            self.clients[key] = client
            self.data["accounts"][key] = {
                "username": username,
                "user_id": user.get("id"),
                "role": user.get("role"),
                "mode": mode,
            }
        checks.append(("作者/编辑/3 评审/2 学科复核共 7 个账号就绪",
                       len(specs) == 7
                       and all(self.data["accounts"].get(key, {}).get("user_id")
                               for key, _ in specs),
                       "存在未就绪账号"))
        self.step("准备作者/编辑/5 位评审账号（已存在则回退登录）", checks,
                  started=started, data={"accounts": {
                      key: self.data["accounts"][key] for key, _ in specs}})

    def _fetch_me(self, client, username: str) -> dict:
        me = self.request(client, "GET", "/api/auth/me")
        self.expect(me, 200, f"读取账号「{username}」会话")
        user = self.payload(me, f"账号「{username}」会话")
        if not isinstance(user, dict) or user.get("username") != username:
            raise FlowFailure(
                f"账号「{username}」会话异常：{json.dumps(user, ensure_ascii=False)[:200]}")
        return user

    def _prepare_admin(self):
        """获取可用于授权角色的管理员会话。

        顺序：登录（默认账号或 --admin-username）→ 失败且有 --admin-token 时
        POST /api/auth/init-admin → 失败且为默认模式时注册首个账号（本地模式首个注册=管理员）。
        """
        username = self.admin_username
        password = self.admin_password
        client = self.new_client()
        login = self.request(client, "POST", "/api/auth/login",
                             data={"username": username, "password": password})
        if login.status_code == 200:
            self.relax_session_cookie(client)
            self.admin_source = "explicit" if self.admin_username_explicit else "default"
            mode = ("login(--admin-username)" if self.admin_username_explicit
                    else "login(默认管理员账号)")
            self.warning(f"管理员账号「{username}」已存在，复用登录（可重复运行）")
            return client, self._fetch_me(client, username), mode
        if login.status_code == 429:
            raise FlowFailure(
                "管理员登录触发限流（429）：请等待 5 分钟后重试，或使用全新 STUDY_DATA_DIR 重启实例")

        if self.admin_token:
            init = self.request(client, "POST", "/api/auth/init-admin", data={
                "username": username, "password": password, "token": self.admin_token,
            })
            if init.status_code == 200:
                self.relax_session_cookie(client)
                self.admin_source = "init-admin(--admin-token)"
                self.warning(f"已通过 POST /api/auth/init-admin 初始化管理员「{username}」")
                return client, self._fetch_me(client, username), "init-admin(--admin-token)"
            if init.status_code == 409:
                raise FlowFailure(
                    f"管理员已存在但账号「{username}」登录失败（HTTP 401）：请用 "
                    f"--admin-username/--admin-password 指定已存在的真实管理员账号；"
                    f"响应：{self._snippet(init)}")
            raise FlowFailure(
                f"POST /api/auth/init-admin 初始化管理员失败：HTTP {init.status_code}；"
                f"{self._snippet(init)}（请确认 --admin-token 与实例 STUDY_ADMIN_INIT_TOKEN 一致、"
                f"且实例尚未初始化管理员）")

        if self.admin_username_explicit:
            raise FlowFailure(
                f"管理员账号「{username}」登录失败（HTTP {login.status_code}："
                f"{self._snippet(login)}）。--admin-username 需指向**已存在且 role=admin** 的账号；"
                f"若实例已预先播种管理员请提供其账号密码，或改用全新 STUDY_DATA_DIR 让首个注册账号成为管理员")

        # 默认模式：注册首个账号（本地模式下自动管理员）。
        register = self.request(client, "POST", "/api/auth/register",
                                data={"username": username, "password": password})
        if register.status_code != 200:
            raise FlowFailure(
                f"注册默认管理员账号「{username}」失败：HTTP {register.status_code}；"
                f"{self._snippet(register)}；{ADMIN_HINT}")
        self.relax_session_cookie(client)
        self.admin_source = "default"
        self.warning(f"注册默认管理员账号「{username}」（本地模式首个注册账号=管理员）")
        return client, self._fetch_me(client, username), "registered(首个账号)"

    def _ensure_account(self, username: str):
        """先尝试登录（复跑时零注册请求），失败再注册（首跑）。

        先注册会在每次复跑时产生 8 次 409 注册请求，可能触发注册限流（20 次/10 分钟）；
        因此按「登录优先」顺序，复跑不消耗注册额度。
        """
        client = self.new_client()
        login_payload = {"username": username, "password": PASSWORD}
        login = self.request(client, "POST", "/api/auth/login", data=login_payload)
        if login.status_code == 200:
            self.relax_session_cookie(client)
            mode = "login(已存在)"
            self.warning(f"账号「{username}」已存在，复用登录（可重复运行）")
        else:
            register = self.request(client, "POST", "/api/auth/register", data=login_payload)
            if register.status_code == 200:
                self.relax_session_cookie(client)
                mode = "registered"
            elif register.status_code in (403, 409):
                # 403=注册关闭 / 409=并发已存在：再回退登录一次。
                retry = self.request(client, "POST", "/api/auth/login", data=login_payload)
                if retry.status_code != 200:
                    raise FlowFailure(
                        f"账号「{username}」注册失败（HTTP {register.status_code}）且登录失败"
                        f"（HTTP {retry.status_code}：{self._snippet(retry)}）；{STUDY_DATA_DIR_HINT}")
                self.relax_session_cookie(client)
                mode = f"login(回退, register={register.status_code})"
                self.warning(f"账号「{username}」已存在，回退登录（可重复运行）")
            elif register.status_code == 429:
                raise FlowFailure(
                    f"注册账号「{username}」触发限流（429）：请等待 10 分钟或使用全新的 "
                    f"STUDY_DATA_DIR 重启隔离实例；{STUDY_DATA_DIR_HINT}")
            else:
                raise FlowFailure(
                    f"注册账号「{username}」失败：HTTP {register.status_code}；"
                    f"响应：{self._snippet(register)}")
        return client, self._fetch_me(client, username), mode

    def grant_roles(self) -> None:
        started = time.time()
        admin = self.clients["admin"]
        grants = [
            ("editor", EDITOR_NAME, ""),
            *(("reviewer", name, SUBJECT) for name in REVIEWER_NAMES),
            *(("subject_reviewer", name, SUBJECT) for name in SUBJECT_REVIEWER_NAMES),
        ]
        checks = []
        for role, username, subject in grants:
            response = self.request(admin, "POST", "/api/plaza/admin/roles", data={
                "action": "grant", "role": role, "subject": subject, "username": username,
            })
            self.expect(response, 200, f"授权 {role} 给「{username}」")
            body = self.payload(response, "角色授权")
            granted_role = (body.get("role") or {}).get("role") if isinstance(body, dict) else ""
            checks.append((f"授权 {role}（学科={subject or '—'}）",
                           body.get("action") == "grant" and granted_role == role,
                           json.dumps(body, ensure_ascii=False)[:200]))
        for key, username in (("editor", EDITOR_NAME),
                              ("reviewer1", REVIEWER_NAMES[0]),
                              ("subject_reviewer1", SUBJECT_REVIEWER_NAMES[0])):
            me = self.payload(self.expect(
                self.request(self.clients[key], "GET", "/api/plaza/me"),
                200, f"读取「{username}」广场能力"), "广场能力")
            caps = me.get("capabilities") or {}
            if key == "editor":
                checks.append(("编辑能力 initial_review/publish",
                               bool(caps.get("initial_review")) and bool(caps.get("publish")),
                               json.dumps(caps, ensure_ascii=False)[:200]))
            else:
                checks.append((f"{username} 评审能力 review", bool(caps.get("review")),
                               json.dumps(caps, ensure_ascii=False)[:200]))
        denied = self.request(self.clients["author"], "POST", "/api/plaza/admin/roles", data={
            "action": "grant", "role": "editor", "subject": "", "username": AUTHOR_NAME,
        })
        checks.append(("普通作者不能授权角色（403）", denied.status_code == 403,
                       f"HTTP {denied.status_code}"))
        self.step("管理员授权广场角色（编辑/评审/学科复核）", checks, started=started)

    # -- 抽检门禁兼容（WS28）：显式 armed 实例的验收前关闭/结束后恢复 --------
    def read_quality_gate(self) -> dict:
        """编辑视角读取抽检门禁状态（配置 + 审计计数 + 最近判定）。"""
        editor = self.clients["editor"]
        response = self.request(editor, "GET", QUALITY_GATE_PATH)
        self.expect(response, 200, "读取抽检门禁状态")
        return self.payload(response, "抽检门禁状态")

    def ensure_gate_compatible(self) -> None:
        """实例显式激活抽检门禁（env/管理员设置 armed）时，验收链路先临时关闭门禁。

        记录原状态；``restore_gate()`` 在链路结束（含失败）时恢复原模式，避免破坏实例配置。
        未显式配置且库中无抽检记录时门禁不阻断（armed=False），这里不会改动任何设置。
        """
        self.gate_origin = None
        self.gate_disabled = False
        if "editor" not in self.clients:
            return
        state = self.read_quality_gate()
        self.gate_origin = state
        mode = str(state.get("mode") or "")
        armed = bool(state.get("armed"))
        if mode == "off" or not (armed or state.get("enforced")):
            return
        admin = self.clients["admin"]
        response = self.request(admin, "PUT", QUALITY_GATE_PATH, data={"mode": "off"})
        self.expect(response, 200, "验收前临时关闭抽检门禁")
        body = self.payload(response, "临时关闭抽检门禁")
        if str(body.get("mode") or "") != "off" or body.get("enforced"):
            raise FlowFailure(
                "抽检门禁临时关闭失败：PUT %s {mode:'off'} 返回 mode=%r enforced=%r"
                % (QUALITY_GATE_PATH, body.get("mode"), body.get("enforced")))
        self.gate_disabled = True
        self.warning(
            "实例抽检门禁已显式激活（mode=%s armed=%s source=%s/%s）："
            "验收链路已临时关闭门禁，结束前恢复原模式"
            % (mode, armed, state.get("mode_source"), state.get("armed_source")))

    def restore_gate(self) -> None:
        """恢复 ``ensure_gate_compatible()`` 记录的抽检门禁原状态（失败只记问题，不掩盖主流程）。"""
        if not self.gate_disabled:
            return
        started = time.time()
        origin = self.gate_origin or {}
        admin = self.clients["admin"]
        restored = False
        last_error = ""
        try:
            mode_source = str(origin.get("mode_source") or "")
            if mode_source == "setting":
                response = self.request(admin, "PUT", QUALITY_GATE_PATH,
                                        data={"mode": str(origin.get("mode") or "")})
            else:
                # 原模式来自 env/默认：reset 删除运行期设置后回落原来源（env 优先）。
                response = self.request(admin, "PUT", QUALITY_GATE_PATH, data={"reset": True})
            self.expect(response, 200, "恢复抽检门禁原模式")
            body = self.payload(response, "恢复抽检门禁原模式")
            if (mode_source != "setting"
                    and str(origin.get("armed_source") or "") == "setting"
                    and origin.get("armed") is not None):
                response = self.request(admin, "PUT", QUALITY_GATE_PATH,
                                        data={"armed": bool(origin.get("armed"))})
                self.expect(response, 200, "恢复抽检门禁 armed 设置")
                body = self.payload(response, "恢复抽检门禁 armed 设置")
            current = self.read_quality_gate()
            restored = (
                str(current.get("mode") or "") == str(origin.get("mode") or "")
                and bool(current.get("armed")) == bool(origin.get("armed")))
            self.data["quality_gate"] = {
                "origin": {key: origin.get(key) for key in (
                    "mode", "mode_source", "armed", "armed_source", "enforced",
                    "required", "pilot_limit", "published_total")},
                "restored": restored,
                "final": {key: current.get(key) for key in (
                    "mode", "mode_source", "armed", "armed_source", "enforced", "required")},
            }
        except FlowFailure as exc:  # noqa: PERF203 - 恢复失败记录为问题，不覆盖主流程异常
            last_error = str(exc)
        checks = [
            ("抽检门禁恢复为验收前原模式（mode/armed 一致）", restored,
             last_error or json.dumps(self.data.get("quality_gate") or {},
                                      ensure_ascii=False)[:300]),
        ]
        try:
            self.step("恢复抽检门禁原状态（验收不改变实例门禁配置）", checks, started=started)
        except FlowFailure:
            pass

    def create_question(self) -> None:
        started = time.time()
        author = self.clients["author"]
        content = {
            "stem": f"【{self.marker}】已知函数 f(x)=x+1，求 f(2) 的值。",
            "options": [
                {"label": "A", "text": "3"},
                {"label": "B", "text": "2"},
                {"label": "C", "text": "4"},
                {"label": "D", "text": "1"},
            ],
            "answers": ["A"],
            "explanation": f"代入 x=2 得 f(2)=2+1=3，因此选 A。（验收标记 {self.marker}）",
        }
        response = self.request(author, "POST", "/api/paperlab/questions", data={
            "type": "SINGLE",
            "subject": SUBJECT,
            "grade": GRADE,
            "difficulty": 0.5,
            "knowledge_tags": [self.marker, "函数", "一次函数"],
            "content": content,
        })
        self.expect(response, 200, "作者创建个人题")
        body = self.payload(response, "创建个人题")
        question = body.get("question") if isinstance(body, dict) else None
        if not isinstance(question, dict):
            raise FlowFailure(f"创建个人题：响应缺少 question 字段：{json.dumps(body, ensure_ascii=False)[:200]}")
        question_id = int(question.get("id") or 0)
        self.data["question_id"] = question_id
        self.data["versions"]["question"] = question.get("version")
        detail = self.payload(self.expect(
            self.request(author, "GET", f"/api/paperlab/questions/{question_id}"),
            200, "读取个人题详情"), "个人题详情")
        detail_question = detail.get("question") or {}
        checks = [
            ("个人题 id 有效", question_id > 0, f"id={question_id}"),
            ("学科/题型正确", question.get("subject") == SUBJECT and question.get("type") == "SINGLE",
             json.dumps({k: question.get(k) for k in ("subject", "type")}, ensure_ascii=False)),
            ("题干/选项/答案/解析齐全",
             bool((question.get("content") or {}).get("stem"))
             and len(((question.get("content") or {}).get("options")) or []) == 4
             and ((question.get("content") or {}).get("answers")) == ["A"]
             and bool((question.get("content") or {}).get("explanation")),
             json.dumps(question.get("content"), ensure_ascii=False)[:200]),
            ("知识点包含验收标记", self.marker in (question.get("knowledge_tags") or []),
             json.dumps(question.get("knowledge_tags"), ensure_ascii=False)),
            ("详情接口一致", int(detail_question.get("id") or 0) == question_id,
             f"detail id={detail_question.get('id')}"),
        ]
        self.step("作者创建个人题（题干/选项/答案/解析/知识点）", checks, started=started,
                  data={"question_id": question_id})

    def create_submission(self) -> None:
        started = time.time()
        author = self.clients["author"]
        question_id = self.data["question_id"]
        response = self.request(author, "POST", "/api/plaza/submissions", data={
            "question_id": question_id, "source": {}, "license_confirmed": False,
        })
        self.expect(response, 201, "创建投稿草稿")
        body = self.payload(response, "创建投稿草稿")
        submission = body.get("submission") or {}
        checks_payload = body.get("checks") or {}
        blocking = checks_payload.get("blocking") or []
        blocking_keys = {str(item.get("key") or "") for item in blocking if isinstance(item, dict)}
        submission_id = int(submission.get("id") or 0)
        self.data["submission_id"] = submission_id
        self.data["versions"]["submission"] = submission.get("version")
        self.data["checks_initial_blocking"] = sorted(blocking_keys)
        anonymous = self.clients["anon"]
        listing = self.payload(self.expect(
            anonymous.request("GET", "/api/plaza/questions",
                              params={"keyword": self.marker}, timeout=30.0),
            200, "匿名检索投稿前列表"), "匿名列表（投稿前）")
        checks = [
            ("投稿 id 有效", submission_id > 0, f"id={submission_id}"),
            ("初始状态 draft / 版本 1",
             submission.get("status") == "draft" and int(submission.get("version") or 0) == 1,
             json.dumps({k: submission.get(k) for k in ("status", "version")}, ensure_ascii=False)),
            ("未分配公开题号（未公开）", not submission.get("public_id"),
             f"public_id={submission.get('public_id')}"),
            ("草稿缺少来源声明 → 存在阻断项",
             {"license_confirmed", "license_basis", "rights_scope", "attribution",
              "provenance"} <= blocking_keys,
             f"blocking={sorted(blocking_keys)}"),
            ("投稿前匿名列表不可见", int(listing.get("total") or 0) == 0,
             f"total={listing.get('total')}"),
        ]
        self.step("作者创建投稿草稿（未公开、阻断项可见）", checks, started=started,
                  data={"submission_id": submission_id})

    def patch_submission(self) -> None:
        started = time.time()
        author = self.clients["author"]
        submission_id = self.data["submission_id"]
        response = self.request(author, "PATCH", f"/api/plaza/submissions/{submission_id}", data={
            "version": 1, "source": SOURCE, "license_confirmed": True,
        })
        self.expect(response, 200, "PATCH 来源/授权声明")
        body = self.payload(response, "PATCH 来源声明")
        submission = body.get("submission") or {}
        version = int(submission.get("version") or 0)
        self.data["versions"]["submission_after_patch"] = version
        repeat = self.request(author, "PATCH", f"/api/plaza/submissions/{submission_id}", data={
            "version": version, "source": SOURCE, "license_confirmed": True,
        })
        self.expect(repeat, 200, "重复 PATCH（无实际变化）")
        repeat_body = self.payload(repeat, "重复 PATCH")
        repeat_submission = repeat_body.get("submission") or {}
        checks = [
            ("PATCH 后创建新版本（1 → 2）", version == 2, f"version={version}"),
            ("状态仍为 draft", submission.get("status") == "draft",
             f"status={submission.get('status')}"),
            ("授权声明已确认", bool(submission.get("license_confirmed"))),
            ("来源声明已保存",
             (submission.get("source") or {}).get("license_basis") == SOURCE["license_basis"],
             json.dumps(submission.get("source"), ensure_ascii=False)[:200]),
            ("阻断项已清零", list(submission.get("blocking") or []) == [],
             f"blocking={submission.get('blocking')}"),
            ("重复 PATCH 幂等（不升版本、changed=false）",
             repeat_body.get("changed") is False
             and int(repeat_submission.get("version") or 0) == version,
             json.dumps(repeat_body, ensure_ascii=False)[:200]),
        ]
        self.step("PATCH 填写来源/授权声明（版本 +1，阻断项清零）", checks, started=started)

    def submit_submission(self) -> None:
        started = time.time()
        author = self.clients["author"]
        submission_id = self.data["submission_id"]
        version = int(self.data["versions"]["submission_after_patch"])
        response = self.request(author, "POST",
                                f"/api/plaza/submissions/{submission_id}/submit",
                                data={"version": version, "confirm_preview": True})
        self.expect(response, 200, "送审")
        submission = (self.payload(response, "送审") or {}).get("submission") or {}
        duplicate = self.request(author, "POST",
                                 f"/api/plaza/submissions/{submission_id}/submit",
                                 data={"version": version, "confirm_preview": True})
        anonymous = self.clients["anon"]
        listing = self.payload(self.expect(
            anonymous.request("GET", "/api/plaza/questions",
                              params={"keyword": self.marker}, timeout=30.0),
            200, "匿名检索待初审列表"), "匿名列表（待初审）")
        checks = [
            ("送审后状态 pending_initial", submission.get("status") == "pending_initial",
             f"status={submission.get('status')}"),
            ("版本仍为 2", int(submission.get("version") or 0) == version,
             f"version={submission.get('version')}"),
            ("重复送审返回 409（可读提示）", duplicate.status_code == 409,
             f"HTTP {duplicate.status_code}"),
            ("待初审投稿匿名不可见", int(listing.get("total") or 0) == 0,
             f"total={listing.get('total')}"),
        ]
        self.step("作者送审（待初审，匿名不可见，重复送审 409）", checks, started=started)

    def initial_review(self) -> str:
        started = time.time()
        editor = self.clients["editor"]
        submission_id = self.data["submission_id"]
        version = int(self.data["versions"]["submission_after_patch"])
        response = self.request(editor, "POST",
                                f"/api/plaza/submissions/{submission_id}/initial-review",
                                data={
                                    "version": version,
                                    "decision": "accept",
                                    "reason": "验收：来源、隐私与完整性逐项核对通过",
                                    "source_checked": True,
                                    "privacy_checked": True,
                                    "completeness_checked": True,
                                })
        self.expect(response, 200, "编辑初审通过")
        body = self.payload(response, "编辑初审")
        submission = body.get("submission") or {}
        public_id = str(submission.get("public_id") or "")
        self.data["public_id"] = public_id
        duplicate = self.request(editor, "POST",
                                 f"/api/plaza/submissions/{submission_id}/initial-review",
                                 data={"version": version, "decision": "accept",
                                       "source_checked": True, "privacy_checked": True,
                                       "completeness_checked": True})
        early_publish = self.request(editor, "POST",
                                     f"/api/plaza/submissions/{submission_id}/publish",
                                     data={"version": version, "reason": "验收：阈值未达成时提前签发"})
        figures = body.get("figures") or {}
        checks = [
            ("初审通过 → community_review", submission.get("status") == "community_review",
             f"status={submission.get('status')}"),
            ("分配公开题号（plz- 前缀）", public_id.startswith("plz-"), f"public_id={public_id}"),
            ("设置 7 天评审窗口", bool(submission.get("review_deadline_at")),
             f"deadline={submission.get('review_deadline_at')}"),
            ("无配图时公开图摘要为空",
             int(figures.get("total") or 0) == 0 and int(figures.get("public") or 0) == 0,
             json.dumps(figures, ensure_ascii=False)[:200]),
            ("重复初审返回 409", duplicate.status_code == 409, f"HTTP {duplicate.status_code}"),
            ("阈值未达成时签发被拒（409）", early_publish.status_code == 409,
             f"HTTP {early_publish.status_code}"),
        ]
        self.step("编辑初审通过 → 社区待核验（匿名公开可见）", checks, started=started,
                  data={"public_id": public_id})
        return public_id

    def assert_community_public(self) -> None:
        started = time.time()
        anonymous = self.clients["anon"]
        author = self.clients["author"]
        public_id = self.data["public_id"]
        submission_id = self.data["submission_id"]
        listing = self.payload(self.expect(
            anonymous.request("GET", "/api/plaza/questions",
                              params={"keyword": self.marker}, timeout=30.0),
            200, "匿名公开列表"), "匿名公开列表")
        items = [item for item in (listing.get("items") or [])
                 if isinstance(item, dict) and item.get("public_id") == public_id]
        item = items[0] if items else {}
        notice = str(item.get("notice") or "")
        detail = self.payload(self.expect(
            anonymous.request("GET", f"/api/plaza/questions/{public_id}", timeout=30.0),
            200, "匿名公开详情"), "匿名公开详情")
        unknown = self.request(anonymous, "GET", "/api/plaza/questions/plz-not-a-real-id")
        asset = self.request(anonymous, "GET", "/api/plaza/assets/not-a-real-asset")
        content = detail.get("content") or {}
        review = detail.get("review") or {}
        source_decl = detail.get("source_declaration") or {}
        detail_text = json.dumps(detail, ensure_ascii=False)
        author_detail = self.payload(self.expect(
            author.request("GET", f"/api/plaza/submissions/{submission_id}", timeout=30.0),
            200, "作者查看投稿详情"), "作者投稿详情").get("submission") or {}
        checks = [
            ("匿名列表按关键词命中本次投稿", bool(items),
             f"total={listing.get('total')} public_id={public_id}"),
            ("列表状态 community_review / review_status=community",
             item.get("status") == "community_review" and item.get("review_status") == "community",
             json.dumps({k: item.get(k) for k in ("status", "review_status")}, ensure_ascii=False)),
            ("列表显著标注「社区待核验」（notice）", "待核验" in notice, notice[:200]),
            ("列表来源为社区（community）", item.get("source") == "community",
             f"source={item.get('source')}"),
            ("列表标记含答案", item.get("has_answer") is True, f"has_answer={item.get('has_answer')}"),
            ("详情 visible=true 且状态一致",
             detail.get("visible") is True and detail.get("status") == "community_review",
             json.dumps({k: detail.get(k) for k in ("visible", "status")}, ensure_ascii=False)),
            ("详情题面包含验收标记",
             self.marker in str(content.get("stem") or ""), str(content.get("stem"))[:160]),
            ("详情答案/解析匿名可读",
             content.get("answers") == ["A"] and bool(content.get("explanation")),
             json.dumps({k: content.get(k) for k in ("answers", "explanation")},
                        ensure_ascii=False)[:200]),
            ("详情来源声明公开且不含确认者内部信息",
             source_decl.get("license_basis") == SOURCE["license_basis"]
             and "confirmed_by_user_id" not in source_decl,
             json.dumps(source_decl, ensure_ascii=False)[:200]),
            ("详情评审进度阈值 = 5/4/2",
             int(review.get("min_reviews") or 0) == 5
             and int(review.get("min_approves") or 0) == 4
             and int(review.get("min_subject_reviewer_approves") or 0) == 2,
             json.dumps(review, ensure_ascii=False)[:200]),
            ("公开详情不泄露内部身份/内部 id 字段",
             not self._forbidden_fields(detail_text),
             f"found={self._forbidden_fields(detail_text)}"),
            ("未知公开题号返回 404", unknown.status_code == 404, f"HTTP {unknown.status_code}"),
            ("未知公开资产返回 404", asset.status_code == 404, f"HTTP {asset.status_code}"),
            ("作者详情可见 public_id 且 public_visible",
             author_detail.get("public_id") == public_id
             and author_detail.get("public_visible") is True,
             json.dumps({k: author_detail.get(k) for k in ("public_id", "public_visible")},
                        ensure_ascii=False)),
        ]
        self.step("匿名访客读广场（社区待核验可见、公开投影无内部信息）", checks,
                  started=started, data={"public_id": public_id})

    def assert_queues_and_events(self) -> None:
        started = time.time()
        submission_id = self.data["submission_id"]
        reviewer = self.clients["reviewer1"]
        editor = self.clients["editor"]
        queue = self.payload(self.expect(
            reviewer.request("GET", "/api/plaza/review/queue", timeout=30.0),
            200, "评审员评审队列"), "评审队列")
        queue_items = [item for item in (queue.get("items") or [])
                       if isinstance(item, dict)
                       and int(item.get("submission_id") or 0) == submission_id]
        queue_item = queue_items[0] if queue_items else {}
        admin_queue = self.payload(self.expect(
            editor.request("GET", "/api/plaza/admin/submissions",
                           params={"status": "community_review", "page_size": 50}, timeout=30.0),
            200, "编辑台投稿队列"), "编辑台队列")
        admin_items = [item for item in (admin_queue.get("items") or [])
                       if isinstance(item, dict)
                       and int(item.get("id") or 0) == submission_id]
        admin_item = admin_items[0] if admin_items else {}
        author_events = self.payload(self.expect(
            self.clients["author"].request(
                "GET", f"/api/plaza/submissions/{submission_id}/events", timeout=30.0),
            200, "作者查看事件链"), "事件链")
        editor_events = self.payload(self.expect(
            editor.request("GET", f"/api/plaza/submissions/{submission_id}/events", timeout=30.0),
            200, "编辑查看事件链"), "事件链（编辑）")
        events_denied = self.request(
            reviewer, "GET", f"/api/plaza/submissions/{submission_id}/events")
        actions = [str(item.get("action") or "") for item in (author_events.get("items") or [])]
        editor_actions = [str(item.get("action") or "")
                          for item in (editor_events.get("items") or [])]
        checks = [
            ("评审队列包含本次投稿", bool(queue_items),
             f"total={queue.get('total')} submission_id={submission_id}"),
            ("队列条目隐藏他人投票（review_hidden=true）",
             queue_item.get("review_hidden") is True,
             json.dumps(queue_item, ensure_ascii=False)[:200]),
            ("编辑台队列包含本次投稿", bool(admin_items),
             f"total={admin_queue.get('total')}"),
            ("编辑台队列显示评审阈值（5/4/2）",
             int(((admin_item.get("review") or {}).get("thresholds") or {})
                 .get("min_reviews") or 0) == 5,
             json.dumps(admin_item.get("review"), ensure_ascii=False)[:200]),
            ("编辑台作者展示名脱敏（不暴露用户名）",
             str(admin_item.get("author_display") or "").startswith("投稿者·")
             and AUTHOR_NAME not in json.dumps(admin_item, ensure_ascii=False)
             and "author_id" not in admin_item,
             json.dumps(admin_item.get("author_display"), ensure_ascii=False)),
            ("作者事件链覆盖创建/改声明/送审/初审通过",
             {"created", "updated", "submitted", "initial_review_accepted"}
             <= set(actions), f"actions={actions}"),
            ("编辑可见完整事件链 actor_id", editor_actions and
             all("actor_id" in item for item in (editor_events.get("items") or [])),
             f"actions={editor_actions}"),
            ("评审员无权查看事件链（403）", events_denied.status_code == 403,
             f"HTTP {events_denied.status_code}"),
        ]
        self.step("评审队列 / 编辑台队列 / 事件链与隐私", checks, started=started)

    def hide_votes_check(self) -> None:
        started = time.time()
        reviewer = self.clients["reviewer1"]
        submission_id = self.data["submission_id"]
        detail = self.payload(self.expect(
            reviewer.request("GET", f"/api/plaza/submissions/{submission_id}", timeout=30.0),
            200, "评审员查看投稿详情"), "评审员投稿详情").get("submission") or {}
        summary = detail.get("review_summary") or {}
        mine = detail.get("my_review")
        checks = [
            ("提交本人评审前隐藏他人投票", summary.get("hidden") is True,
             json.dumps(summary, ensure_ascii=False)[:200]),
            ("评审员尚无本人评审记录", mine is None,
             json.dumps(mine, ensure_ascii=False)[:160]),
            ("评审员可评审（can.review=true）", (detail.get("can") or {}).get("review") is True,
             json.dumps(detail.get("can"), ensure_ascii=False)[:200]),
        ]
        self.step("评审前隐藏他人投票与意见", checks, started=started)

    def submit_reviews(self) -> None:
        started = time.time()
        submission_id = self.data["submission_id"]
        version = int(self.data["versions"]["submission_after_patch"])
        order = [
            ("subject_reviewer1", "subject_reviewer", "复核员一：独立解得 f(2)=3，A。"),
            ("subject_reviewer2", "subject_reviewer", "复核员二：代入验证 x=2 得 3，选 A。"),
            ("reviewer1", "reviewer", "评审员一：按定义计算 f(2)=2+1=3，选 A。"),
            ("reviewer2", "reviewer", "评审员二：函数值计算正确，答案 A。"),
            ("reviewer3", "reviewer", "评审员三：独立作答结果 A，与解析一致。"),
        ]
        checks = []
        final_transition = ""
        for index, (key, expected_role, answer) in enumerate(order, start=1):
            response = self.request(self.clients[key], "POST",
                                    f"/api/plaza/submissions/{submission_id}/reviews",
                                    data={
                                        "version": version,
                                        "verdict": "approve",
                                        "reasons": "",
                                        "independent_answer": answer,
                                        "conflict_declared": False,
                                    })
            self.expect(response, 201, f"{key} 提交独立评审")
            body = self.payload(response, f"{key} 评审响应")
            review = body.get("review") or {}
            transition = str(body.get("transition") or "")
            final_transition = transition
            expected_transition = "verified" if index == len(order) else "pending"
            checks.append((
                f"第 {index} 票 approve（{expected_role}）",
                review.get("verdict") == "approve"
                and review.get("reviewer_role") == expected_role,
                json.dumps(review, ensure_ascii=False)[:200],
            ))
            checks.append((
                f"第 {index} 票流转 = {expected_transition}",
                transition == expected_transition,
                f"transition={transition}",
            ))
        duplicate = self.request(self.clients["reviewer1"], "POST",
                                 f"/api/plaza/submissions/{submission_id}/reviews",
                                 data={"version": version, "verdict": "approve",
                                       "independent_answer": "重复投票", "conflict_declared": False})
        checks.append(("重复评审返回 409（一人一版本一票）", duplicate.status_code == 409,
                       f"HTTP {duplicate.status_code}"))
        checks.append(("最后一票自动流转 verified", final_transition == "verified",
                       f"transition={final_transition}"))
        self.step("5 位评审独立作答 + approve（自动流转 verified）", checks, started=started)

    def assert_verified_public(self) -> None:
        started = time.time()
        submission_id = self.data["submission_id"]
        public_id = self.data["public_id"]
        reviewer = self.clients["reviewer1"]
        author = self.clients["author"]
        editor = self.clients["editor"]
        anonymous = self.clients["anon"]
        detail = self.payload(self.expect(
            author.request("GET", f"/api/plaza/submissions/{submission_id}", timeout=30.0),
            200, "作者查看核验后投稿"), "核验后投稿详情").get("submission") or {}
        summary = detail.get("review_summary") or {}
        listing = self.payload(self.expect(
            anonymous.request("GET", "/api/plaza/questions",
                              params={"keyword": self.marker}, timeout=30.0),
            200, "匿名核验后列表"), "匿名列表（核验后）")
        items = [item for item in (listing.get("items") or [])
                 if isinstance(item, dict) and item.get("public_id") == public_id]
        item = items[0] if items else {}
        public_detail = self.payload(self.expect(
            anonymous.request("GET", f"/api/plaza/questions/{public_id}", timeout=30.0),
            200, "匿名核验后详情"), "匿名详情（核验后）")
        mine = self.payload(self.expect(
            reviewer.request("GET", "/api/plaza/review/mine", timeout=30.0),
            200, "评审员我的评审历史"), "我的评审历史")
        my_items = [entry for entry in (mine.get("items") or [])
                    if isinstance(entry, dict)
                    and int(entry.get("submission_id") or 0) == submission_id]
        admin_queue = self.payload(self.expect(
            editor.request("GET", "/api/plaza/admin/submissions",
                           params={"status": "verified", "page_size": 50}, timeout=30.0),
            200, "编辑台核验通过队列"), "编辑台队列（核验通过）")
        admin_ids = [int(entry.get("id") or 0) for entry in (admin_queue.get("items") or [])
                     if isinstance(entry, dict)]
        public_review = public_detail.get("review") or {}
        checks = [
            ("投稿状态 verified 且公开题号不变",
             detail.get("status") == "verified" and detail.get("public_id") == public_id,
             json.dumps({k: detail.get(k) for k in ("status", "public_id")}, ensure_ascii=False)),
            ("独立评审 5 / 通过 5 / 学科复核 2",
             int(summary.get("independent_reviews") or 0) == 5
             and int(summary.get("approves") or 0) == 5
             and int(summary.get("subject_reviewer_approves") or 0) == 2,
             json.dumps(summary, ensure_ascii=False)[:240]),
            ("评审阈值达成（threshold_met=true）", summary.get("threshold_met") is True,
             json.dumps(summary, ensure_ascii=False)[:200]),
            ("匿名列表状态 verified / review_status=verified",
             item.get("status") == "verified" and item.get("review_status") == "verified",
             json.dumps({k: item.get(k) for k in ("status", "review_status")}, ensure_ascii=False)),
            ("匿名详情 verified 且评审进度 5/5/2",
             public_detail.get("status") == "verified"
             and int(public_review.get("approves") or 0) == 5
             and int(public_review.get("subject_reviewer_approves") or 0) == 2,
             json.dumps(public_review, ensure_ascii=False)[:240]),
            ("「我的评审」包含本次评审且为当前版本", bool(my_items)
             and my_items[0].get("is_current_version") is True,
             json.dumps(my_items[0] if my_items else {}, ensure_ascii=False)[:240]),
            ("编辑台「核验通过」队列包含本次投稿", submission_id in admin_ids,
             f"queue_ids={admin_ids[:20]}"),
        ]
        self.step("核验通过后公开可见性与评审历史", checks, started=started)

    def publish(self) -> None:
        started = time.time()
        submission_id = self.data["submission_id"]
        public_id = self.data["public_id"]
        version = int(self.data["versions"]["submission_after_patch"])
        editor = self.clients["editor"]
        response = self.request(editor, "POST",
                                f"/api/plaza/submissions/{submission_id}/publish",
                                data={"version": version, "reason": "验收：评审阈值达成，签发精选"})
        self.expect(response, 200, "编辑签发精选")
        body = self.payload(response, "签发精选")
        release = body.get("release") or {}
        repeat = self.request(editor, "POST",
                              f"/api/plaza/submissions/{submission_id}/publish",
                              data={"version": version, "reason": "验收：重复签发（幂等）"})
        self.expect(repeat, 200, "重复签发（幂等）")
        repeat_body = self.payload(repeat, "重复签发")
        releases = self.payload(self.expect(
            editor.request("GET", "/api/plaza/releases", params={"status": "active"},
                           timeout=30.0),
            200, "编辑查看活跃发布"), "发布历史")
        active_ids = [str(entry.get("public_id") or "")
                      for entry in (releases.get("items") or []) if isinstance(entry, dict)]
        releases_denied = self.clients["reviewer1"].request(
            "GET", "/api/plaza/releases", timeout=30.0)
        events = self.payload(self.expect(
            editor.request("GET", f"/api/plaza/submissions/{submission_id}/events",
                           timeout=30.0), 200, "签发后事件链"), "事件链（签发后）")
        actions = [str(item.get("action") or "") for item in (events.get("items") or [])]
        checks = [
            ("签发成功：release active / submission published",
             release.get("status") == "active"
             and (body.get("submission") or {}).get("status") == "published",
             json.dumps({"release": release.get("status"),
                         "submission": (body.get("submission") or {}).get("status")},
                        ensure_ascii=False)),
            ("public_url 指向公开详情",
             str(body.get("public_url") or "").endswith(public_id),
             str(body.get("public_url"))),
            ("重复签发幂等（idempotent=true）", repeat_body.get("idempotent") is True,
             json.dumps(repeat_body, ensure_ascii=False)[:200]),
            ("发布历史含本次活跃发布", public_id in active_ids,
             f"active={active_ids[:20]}"),
            ("非编辑不能查看发布历史（403）", releases_denied.status_code == 403,
             f"HTTP {releases_denied.status_code}"),
            ("事件链含 published", "published" in actions, f"actions={actions[-6:]}"),
        ]
        self.step("编辑签发精选（幂等 / 发布历史 / 事件链）", checks, started=started)

    def assert_featured_public(self) -> None:
        started = time.time()
        public_id = self.data["public_id"]
        submission_id = self.data["submission_id"]
        anonymous = self.clients["anon"]
        author = self.clients["author"]
        listing = self.payload(self.expect(
            anonymous.request("GET", "/api/plaza/questions",
                              params={"keyword": self.marker}, timeout=30.0),
            200, "匿名精选列表"), "匿名列表（精选）")
        items = [item for item in (listing.get("items") or [])
                 if isinstance(item, dict) and item.get("public_id") == public_id]
        item = items[0] if items else {}
        featured_listing = self.payload(self.expect(
            anonymous.request("GET", "/api/plaza/questions",
                              params={"source": "featured", "keyword": self.marker}, timeout=30.0),
            200, "匿名精选来源筛选"), "匿名精选筛选")
        featured_ids = [str(entry.get("public_id") or "")
                        for entry in (featured_listing.get("items") or []) if isinstance(entry, dict)]
        detail = self.payload(self.expect(
            anonymous.request("GET", f"/api/plaza/questions/{public_id}", timeout=30.0),
            200, "匿名精选详情"), "匿名详情（精选）")
        author_detail = self.payload(self.expect(
            author.request("GET", f"/api/plaza/submissions/{submission_id}", timeout=30.0),
            200, "作者查看已发布投稿"), "作者投稿（已发布）").get("submission") or {}
        denied = author.request("POST", f"/api/plaza/submissions/{submission_id}/publish",
                                json={"version": int(
                                    self.data["versions"]["submission_after_patch"])},
                                timeout=30.0)
        notice = str(detail.get("notice") or "")
        checks = [
            ("精选列表状态 published / review_status=featured",
             item.get("status") == "published" and item.get("review_status") == "featured",
             json.dumps({k: item.get(k) for k in ("status", "review_status")}, ensure_ascii=False)),
            ("精选列表来源 featured / featured=true",
             item.get("source") == "featured" and item.get("featured") is True,
             json.dumps({k: item.get(k) for k in ("source", "featured")}, ensure_ascii=False)),
            ("source=featured 筛选包含本次投稿", public_id in featured_ids,
             f"featured_ids={featured_ids[:20]}"),
            ("精选详情 featured=true 且提示精选", detail.get("featured") is True
             and "精选" in notice, f"notice={notice[:120]}"),
            ("精选详情仍公开题面与来源声明",
             self.marker in str((detail.get("content") or {}).get("stem") or "")
             and (detail.get("source_declaration") or {}).get("license_basis")
             == SOURCE["license_basis"]),
            ("作者详情 published / public_visible",
             author_detail.get("status") == "published"
             and author_detail.get("public_visible") is True,
             json.dumps({k: author_detail.get(k) for k in ("status", "public_visible")},
                        ensure_ascii=False)),
            ("作者不能签发（403）", denied.status_code == 403, f"HTTP {denied.status_code}"),
        ]
        self.step("精选后匿名可见（列表/详情/来源筛选）", checks, started=started)

    def withdraw(self) -> None:
        started = time.time()
        submission_id = self.data["submission_id"]
        public_id = self.data["public_id"]
        editor = self.clients["editor"]
        author = self.clients["author"]
        author_withdraw = self.request(author, "POST",
                                       f"/api/plaza/submissions/{submission_id}/withdraw",
                                       data={"reason": "验收：作者尝试撤回已发布投稿"})
        response = self.request(editor, "POST", f"/api/plaza/releases/{public_id}/withdraw",
                                data={"reason": "验收：演示撤回整条链", "mode": "normal"})
        self.expect(response, 200, "编辑撤回发布")
        body = self.payload(response, "撤回发布")
        repeat = self.request(editor, "POST", f"/api/plaza/releases/{public_id}/withdraw",
                              data={"reason": "验收：重复撤回（幂等）", "mode": "normal"})
        self.expect(repeat, 200, "重复撤回（幂等）")
        repeat_body = self.payload(repeat, "重复撤回")
        reviewer_denied = self.clients["reviewer1"].request(
            "POST", f"/api/plaza/releases/{public_id}/withdraw",
            json={"reason": "验收：评审员尝试撤回", "mode": "normal"}, timeout=30.0)
        checks = [
            ("作者不能撤回已发布投稿（409）", author_withdraw.status_code == 409,
             f"HTTP {author_withdraw.status_code}"),
            ("编辑撤回成功（status=withdrawn）", body.get("status") == "withdrawn",
             json.dumps(body, ensure_ascii=False)[:200]),
            ("重复撤回幂等（idempotent=true）", repeat_body.get("idempotent") is True,
             json.dumps(repeat_body, ensure_ascii=False)[:200]),
            ("评审员不能撤回发布（403）", reviewer_denied.status_code == 403,
             f"HTTP {reviewer_denied.status_code}"),
        ]
        self.step("编辑撤回整条链（作者不可撤回已发布 / 重复撤回幂等）", checks, started=started)

    def assert_withdrawn_invisible(self) -> None:
        started = time.time()
        public_id = self.data["public_id"]
        submission_id = self.data["submission_id"]
        anonymous = self.clients["anon"]
        author = self.clients["author"]
        editor = self.clients["editor"]
        listing = self.payload(self.expect(
            anonymous.request("GET", "/api/plaza/questions",
                              params={"keyword": self.marker}, timeout=30.0),
            200, "匿名撤回后列表"), "匿名列表（撤回后）")
        listed_ids = [str(item.get("public_id") or "")
                      for item in (listing.get("items") or []) if isinstance(item, dict)]
        detail_response = anonymous.request(
            "GET", f"/api/plaza/questions/{public_id}", timeout=30.0)
        detail = self.payload(detail_response, "匿名详情（撤回后）")
        detail_text = json.dumps(detail, ensure_ascii=False)
        author_detail = self.payload(self.expect(
            author.request("GET", f"/api/plaza/submissions/{submission_id}", timeout=30.0),
            200, "作者查看已撤回投稿"), "作者投稿（已撤回）").get("submission") or {}
        events = self.payload(self.expect(
            editor.request("GET", f"/api/plaza/submissions/{submission_id}/events",
                           timeout=30.0), 200, "撤回后事件链"), "事件链（撤回后）")
        actions = [str(item.get("action") or "") for item in (events.get("items") or [])]
        releases = self.payload(self.expect(
            editor.request("GET", "/api/plaza/releases", params={"status": "active"},
                           timeout=30.0), 200, "撤回后活跃发布"), "活跃发布（撤回后）")
        active_ids = [str(entry.get("public_id") or "")
                      for entry in (releases.get("items") or []) if isinstance(entry, dict)]
        checks = [
            ("撤回后匿名列表不可见", int(listing.get("total") or 0) == 0
             and public_id not in listed_ids,
             f"total={listing.get('total')} listed={listed_ids[:20]}"),
            ("撤回后旧链接返回清晰撤回状态（visible=false）",
             detail_response.status_code == 200 and detail.get("status") == "withdrawn"
             and detail.get("visible") is False,
             detail_text[:240]),
            ("撤回后不回显受限正文",
             "content" not in detail and self.marker not in detail_text,
             detail_text[:240]),
            ("撤回后 review_status=withdrawn 且带 notice",
             detail.get("review_status") == "withdrawn" and bool(detail.get("notice")),
             detail_text[:240]),
            ("作者详情 withdrawn / public_visible=false",
             author_detail.get("status") == "withdrawn"
             and author_detail.get("public_visible") is False,
             json.dumps({k: author_detail.get(k) for k in ("status", "public_visible")},
                        ensure_ascii=False)),
            ("事件链含 release_withdrawn", "release_withdrawn" in actions,
             f"actions={actions[-6:]}"),
            ("活跃发布列表不再包含本次发布", public_id not in active_ids,
             f"active={active_ids[:20]}"),
        ]
        self.step("撤回后匿名不可见（列表不可见 / 详情 visible=false / 无正文泄露）",
                  checks, started=started)

    def identity_flow(self) -> None:
        started = time.time()
        author = self.clients["author"]
        editor = self.clients["editor"]
        reviewer = self.clients["reviewer1"]
        evidence = f"验收材料：教师资格证编号 ACCEPT-{self.run_id}"
        expires_at = (datetime.now(timezone.utc) + timedelta(days=365)).isoformat()
        response = self.request(author, "POST", "/api/plaza/identity/apply", data={
            "kind": "teacher", "subject": SUBJECT, "evidence": evidence,
        })
        self.expect(response, 201, "身份认证申请")
        identity = (self.payload(response, "身份申请") or {}).get("identity") or {}
        identity_id = int(identity.get("id") or 0)
        queue = self.payload(self.expect(
            editor.request("GET", "/api/plaza/admin/identity/queue",
                           params={"status": "pending"}, timeout=30.0),
            200, "身份审核队列"), "身份审核队列")
        queue_ids = [int(entry.get("id") or 0)
                     for entry in (queue.get("items") or []) if isinstance(entry, dict)]
        queue_entry = next((entry for entry in (queue.get("items") or [])
                            if isinstance(entry, dict)
                            and int(entry.get("id") or 0) == identity_id), {})
        denied = reviewer.request("GET", "/api/plaza/admin/identity/queue",
                                  params={"status": "pending"}, timeout=30.0)
        reviewed = self.request(editor, "POST",
                                f"/api/plaza/admin/identity/{identity_id}/review",
                                data={"decision": "approve",
                                      "reason": "验收：材料齐全，通过认证",
                                      "expires_at": expires_at})
        self.expect(reviewed, 200, "身份审核通过")
        reviewed_body = self.payload(reviewed, "身份审核结果")
        me = self.payload(self.expect(author.request("GET", "/api/plaza/me", timeout=30.0),
                                      200, "作者广场能力"), "作者广场能力")
        checks = [
            ("身份申请创建（pending / teacher）",
             identity.get("status") == "pending" and identity.get("kind") == "teacher",
             json.dumps(identity, ensure_ascii=False)[:200]),
            ("编辑身份审核队列包含该申请", identity_id in queue_ids,
             f"queue_ids={queue_ids[:20]}"),
            ("队列展示申请人用户名（仅授权人员）",
             ((queue_entry.get("user") or {}).get("username")) == AUTHOR_NAME,
             json.dumps(queue_entry.get("user"), ensure_ascii=False)),
            ("评审员无权查看身份队列（403）", denied.status_code == 403,
             f"HTTP {denied.status_code}"),
            ("审核通过 identity.status=approved",
             (reviewed_body.get("identity") or {}).get("status") == "approved",
             json.dumps(reviewed_body.get("identity"), ensure_ascii=False)[:240]),
            ("作者 /plaza/me 展示已通过身份",
             ((me.get("identity") or {}).get("status")) == "approved",
             json.dumps(me.get("identity"), ensure_ascii=False)[:240]),
        ]
        self.step("身份认证申请 → 编辑审核通过（队列/权限）", checks, started=started)

    # -- Phase 3：质量运营 / 互动 / 回滚 / 通知 ---------------------------
    def _phase3(self) -> dict:
        return self.data.setdefault("phase3", {})

    def _notifications(self, client, *, unread_only: bool = False, kind: str | None = None,
                       page_size: int = 100) -> dict:
        params: dict = {"page": 1, "page_size": page_size}
        if unread_only:
            params["unread_only"] = True
        if kind:
            params["kind"] = kind
        return self.payload(self.expect(
            client.request("GET", "/api/plaza/notifications", params=params, timeout=30.0),
            200, "读取通知中心"), "通知中心")

    @staticmethod
    def _find_notifications(payload: dict, *, kind: str | None = None,
                            public_id: str | None = None,
                            submission_id: int | None = None) -> list[dict]:
        """按 kind / public_id / submission_id 过滤本人通知（报告不打印通知正文细节）。"""
        items = payload.get("items") if isinstance(payload, dict) else []
        found = []
        for item in items or []:
            if not isinstance(item, dict):
                continue
            if kind and str(item.get("kind") or "") != kind:
                continue
            if public_id and str(item.get("public_id") or "") != public_id:
                continue
            if submission_id is not None and int(item.get("submission_id") or 0) != int(submission_id):
                continue
            found.append(item)
        return found

    def create_verified_submission(self, label: str, *, qtype: str = "SINGLE",
                                   content: dict | None = None,
                                   grade: str = GRADE) -> dict:
        """创建个人题 → 投稿 → 声明 → 送审 → 初审 → 5 票独立评审，停在 verified（不签发）。

        供抽检门禁（先 409 → 签发前抽检 pass → 再签发）、一键恢复等需要「待签发」投稿的
        验收步骤复用；每一步都用 ``self.expect`` 断言，失败会带可读标签抛 ``FlowFailure``。
        """
        author = self.clients["author"]
        editor = self.clients["editor"]
        if content is None:
            content = {
                "stem": f"【{self.marker}】{label}：已知函数 f(x)=x+1，求 f(2) 的值。",
                "options": [
                    {"label": "A", "text": "3"},
                    {"label": "B", "text": "2"},
                    {"label": "C", "text": "4"},
                    {"label": "D", "text": "1"},
                ],
                "answers": ["A"],
                "explanation": f"代入 x=2 得 f(2)=2+1=3，因此选 A。（{label}）",
            }
        created = self.payload(self.expect(
            self.request(author, "POST", "/api/paperlab/questions", data={
                "type": qtype, "subject": SUBJECT, "grade": grade, "difficulty": 0.5,
                "knowledge_tags": [self.marker, label, "函数"],
                "content": content,
            }), 200, f"{label}：创建个人题"), "创建个人题")
        question = created.get("question") if isinstance(created, dict) else {}
        question_id = int((question or {}).get("id") or 0)
        created_sub = self.payload(self.expect(
            self.request(author, "POST", "/api/plaza/submissions", data={
                "question_id": question_id, "source": {}, "license_confirmed": False,
            }), 201, f"{label}：创建投稿草稿"), "创建投稿草稿")
        submission_id = int(((created_sub.get("submission") or {}).get("id")) or 0)
        patched = self.payload(self.expect(
            self.request(author, "PATCH", f"/api/plaza/submissions/{submission_id}", data={
                "version": 1, "source": SOURCE, "license_confirmed": True,
            }), 200, f"{label}：PATCH 来源声明"), "PATCH 来源声明")
        version = int(((patched.get("submission") or {}).get("version")) or 0)
        self.expect(self.request(author, "POST",
                                 f"/api/plaza/submissions/{submission_id}/submit",
                                 data={"version": version, "confirm_preview": True}),
                    200, f"{label}：送审")
        reviewed = self.payload(self.expect(
            self.request(editor, "POST",
                         f"/api/plaza/submissions/{submission_id}/initial-review", data={
                             "version": version,
                             "decision": "accept",
                             "reason": f"验收：{label} 来源/隐私/完整性核对通过",
                             "source_checked": True,
                             "privacy_checked": True,
                             "completeness_checked": True,
                         }), 200, f"{label}：编辑初审通过"), "编辑初审")
        public_id = str(((reviewed.get("submission") or {}).get("public_id")) or "")
        review_order = [
            "subject_reviewer1", "subject_reviewer2", "reviewer1", "reviewer2", "reviewer3",
        ]
        transitions: list[str] = []
        for index, key in enumerate(review_order, start=1):
            reviewed_body = self.payload(self.expect(
                self.request(self.clients[key], "POST",
                             f"/api/plaza/submissions/{submission_id}/reviews", data={
                                 "version": version,
                                 "verdict": "approve",
                                 "reasons": "",
                                 "independent_answer": f"验收独立作答（{label} 第 {index} 票）：A",
                                 "conflict_declared": False,
                             }), 201, f"{label}：第 {index} 票评审"), "评审响应")
            transitions.append(str(reviewed_body.get("transition") or ""))
        return {"question_id": question_id, "submission_id": submission_id,
                "public_id": public_id, "version": version, "transitions": transitions}

    def publish_new_question(self, label: str, *, qtype: str = "SINGLE",
                             content: dict | None = None,
                             record_step: bool = True, grade: str = GRADE) -> dict:
        """Phase 3 复用：创建个人题 → 投稿 → 声明 → 送审 → 初审 → 5 票 → 签发，返回发布信息。

        供 plaza_accept 自身（Phase 3 抽检/回滚）与 official_accept（精选链路）共用；
        每一步都用 ``self.expect`` 断言，失败会带可读标签抛 ``FlowFailure``（不静默跳过）。
        """
        started = time.time()
        editor = self.clients["editor"]
        prepared = self.create_verified_submission(
            label, qtype=qtype, content=content, grade=grade)
        question_id = prepared["question_id"]
        submission_id = prepared["submission_id"]
        public_id = prepared["public_id"]
        version = prepared["version"]
        transitions = prepared["transitions"]
        published = self.payload(self.expect(
            self.request(editor, "POST", f"/api/plaza/submissions/{submission_id}/publish", data={
                "version": version,
                "reason": f"验收：{label} 评审阈值达成，签发精选",
            }), 200, f"{label}：编辑签发精选"), "签发精选")
        release = published.get("release") or {}
        if record_step:
            checks = [
                (f"{label}：题目/投稿/公开题号就绪",
                 question_id > 0 and submission_id > 0 and public_id.startswith("plz-"),
                 f"q={question_id} sub={submission_id} pid={public_id}"),
                (f"{label}：第 5 票自动流转 verified", transitions and transitions[-1] == "verified",
                 f"transitions={transitions}"),
                (f"{label}：签发 active 发布",
                 release.get("status") == "active"
                 and (published.get("submission") or {}).get("status") == "published",
                 json.dumps({"release": release.get("status")}, ensure_ascii=False)),
            ]
            self.step(f"发布额外验收题（{label}）", checks, started=started,
                      data={"question_id": question_id, "submission_id": submission_id,
                            "public_id": public_id, "version": version})
        return {"question_id": question_id, "submission_id": submission_id,
                "public_id": public_id, "version": version, "transitions": transitions}

    def phase3_publish_extra_questions(self) -> None:
        """Phase 3：发布 B（抽检 pass / 收藏 / 回滚）与 C（抽检 high 自动撤回）两条题。"""
        started = time.time()
        state = self._phase3()
        b_stem_v1 = f"【{self.marker}】Phase3-B 初始版：已知函数 f(x)=x+1，求 f(2) 的值。"
        b = self.publish_new_question(
            "Phase3-B（抽检 pass + 收藏 + 回滚）",
            content={
                "stem": b_stem_v1,
                "options": [
                    {"label": "A", "text": "3"}, {"label": "B", "text": "2"},
                    {"label": "C", "text": "4"}, {"label": "D", "text": "1"},
                ],
                "answers": ["A"],
                "explanation": "代入 x=2 得 f(2)=2+1=3，因此选 A。（Phase3-B 初始版）",
            },
            record_step=False,
        )
        state["b"] = {**b, "stem_v1": b_stem_v1, "stem_v2": "Phase3-B 修订版"}
        c = self.publish_new_question("Phase3-C（抽检 issue+high 自动撤回）", record_step=False)
        state["c"] = c
        anonymous = self.clients["anon"]
        listing = self.payload(self.expect(
            anonymous.request("GET", "/api/plaza/questions",
                              params={"keyword": self.marker}, timeout=30.0),
            200, "Phase3 公开列表"), "Phase3 公开列表")
        listed_ids = [str(item.get("public_id") or "")
                      for item in (listing.get("items") or []) if isinstance(item, dict)]
        checks = [
            ("Phase3-B 发布成功且匿名可见",
             b["public_id"].startswith("plz-") and b["public_id"] in listed_ids,
             f"public_id={b['public_id']} listed={listed_ids[:8]}"),
            ("Phase3-C 发布成功且匿名可见",
             c["public_id"].startswith("plz-") and c["public_id"] in listed_ids,
             f"public_id={c['public_id']} listed={listed_ids[:8]}"),
            ("两条 Phase3 题均为 5 票末票 verified",
             b["transitions"][-1:] == ["verified"] and c["transitions"][-1:] == ["verified"],
             f"b={b['transitions']} c={c['transitions']}"),
        ]
        self.step("Phase3 发布两条额外验收题（供抽检 pass / high 自动撤回）", checks,
                  started=started, data={"b": b["public_id"], "c": c["public_id"]})

    def phase3_quality_pass(self) -> None:
        """Phase 3：发布后抽检 pass → 已抽检队列 / 抽样覆盖率 / 指标；权限与入参负向。"""
        started = time.time()
        state = self._phase3()
        b = state["b"]
        editor = self.clients["editor"]
        author = self.clients["author"]
        reviewer = self.clients["reviewer1"]
        author_denied = self.request(author, "POST", "/api/plaza/admin/quality/checks",
                                     data={"public_id": b["public_id"], "decision": "pass"})
        withdrawn_denied = self.request(
            editor, "POST", "/api/plaza/admin/quality/checks",
            data={"submission_id": int(self.data["submission_id"]), "decision": "pass"})
        missing_findings = self.request(
            editor, "POST", "/api/plaza/admin/quality/checks",
            data={"public_id": b["public_id"], "decision": "issue", "severity": "high"})
        created = self.request(editor, "POST", "/api/plaza/admin/quality/checks", data={
            "public_id": b["public_id"],
            "decision": "pass",
            "note": f"验收：Phase3 抽检通过（{self.run_id}）",
        })
        self.expect(created, 201, "抽检 pass")
        body = self.payload(created, "抽检 pass 响应")
        check = body.get("check") or {}
        check_id = int(check.get("id") or 0)
        state["quality_pass_check_id"] = check_id
        queue = self.payload(self.expect(
            editor.request("GET", "/api/plaza/admin/quality/queue",
                           params={"status": "passed", "page_size": 50}, timeout=30.0),
            200, "已抽检队列（pass）"), "已抽检队列")
        queue_items = [item for item in (queue.get("items") or []) if isinstance(item, dict)]
        entry = next((item for item in queue_items
                      if int(item.get("submission_id") or 0) == int(b["submission_id"])), {})
        metrics = self.payload(self.expect(
            editor.request("GET", "/api/plaza/admin/metrics", timeout=30.0),
            200, "质量指标"), "质量指标")
        quality = metrics.get("quality") or {}
        sampling = quality.get("sampling") or {}
        checks = [
            ("作者不能提交抽检（403）", author_denied.status_code == 403,
             f"HTTP {author_denied.status_code}"),
            ("非 published 投稿不可抽检（409）", withdrawn_denied.status_code == 409,
             f"HTTP {withdrawn_denied.status_code}"),
            ("issue 缺 findings 时 422", missing_findings.status_code == 422,
             f"HTTP {missing_findings.status_code}"),
            ("抽检 pass 记录创建（decision=pass）",
             check_id > 0 and check.get("decision") == "pass",
             json.dumps(check, ensure_ascii=False)[:200]),
            ("pass 不自动撤回", body.get("auto_withdrawn") is False,
             f"auto_withdrawn={body.get('auto_withdrawn')}"),
            ("已抽检队列包含该题且 check_status=passed",
             int(entry.get("submission_id") or 0) == int(b["submission_id"])
             and entry.get("check_status") == "passed"
             and (entry.get("latest_check") or {}).get("decision") == "pass",
             json.dumps(entry, ensure_ascii=False)[:260]),
            ("抽样口径 initial_100 且覆盖率 > 0",
             sampling.get("mode") == "initial_100"
             and float(sampling.get("coverage_rate") or 0) > 0
             and int(sampling.get("checked_total") or 0) >= 1,
             json.dumps(sampling, ensure_ascii=False)[:240]),
            ("指标 quality.passed/checks_total 已累计",
             int(quality.get("passed") or 0) >= 1
             and int(quality.get("checks_total") or 0) >= 1,
             json.dumps({k: quality.get(k) for k in ("checks_total", "passed", "issues")},
                        ensure_ascii=False)),
            ("非编辑不能查看质量指标（403）",
             reviewer.request("GET", "/api/plaza/admin/metrics", timeout=30.0).status_code == 403),
        ]
        self.step("Phase3 发布后抽检 pass（队列/覆盖率/指标/权限负向）", checks,
                  started=started, data={"check_id": check_id})

    def phase3_feedback_on_b(self) -> None:
        """Phase 3：纠错/举报提交 → 编辑台 pending 队列 → resolve → 反馈人通知。"""
        started = time.time()
        state = self._phase3()
        b = state["b"]
        editor = self.clients["editor"]
        author = self.clients["author"]
        issue_reporter = self.clients["reviewer1"]
        report_reporter = self.clients["reviewer2"]
        issue = self.payload(self.expect(
            self.request(issue_reporter, "POST",
                         f"/api/plaza/questions/{b['public_id']}/issues", data={
                             "field": "answer",
                             "description": f"验收：Phase3 纠错 {self.run_id}：解析与参考答案不一致",
                         }), 201, "提交纠错"), "纠错提交")
        issue_id = int(((issue.get("issue") or {}).get("id")) or 0)
        report = self.payload(self.expect(
            self.request(report_reporter, "POST",
                         f"/api/plaza/questions/{b['public_id']}/reports", data={
                             "reason": "content_quality",
                             "detail": f"验收：Phase3 举报 {self.run_id}：内容质量问题",
                         }), 201, "提交举报"), "举报提交")
        report_id = int(((report.get("report") or {}).get("id")) or 0)
        anon_issue = self.request(self.clients["anon"], "POST",
                                  f"/api/plaza/questions/{b['public_id']}/issues",
                                  data={"field": "answer", "description": "匿名提交纠错"})
        issue_queue = self.payload(self.expect(
            editor.request("GET", "/api/plaza/admin/issues",
                           params={"status": "pending", "page_size": 50}, timeout=30.0),
            200, "纠错队列"), "纠错队列")
        report_queue = self.payload(self.expect(
            editor.request("GET", "/api/plaza/admin/reports",
                           params={"status": "pending", "page_size": 50}, timeout=30.0),
            200, "举报队列"), "举报队列")
        issue_ids = [int(entry.get("id") or 0) for entry in (issue_queue.get("items") or [])
                     if isinstance(entry, dict)]
        report_ids = [int(entry.get("id") or 0) for entry in (report_queue.get("items") or [])
                      if isinstance(entry, dict)]
        denied = author.request("GET", "/api/plaza/admin/issues",
                                params={"status": "pending"}, timeout=30.0)
        resolved_issue = self.payload(self.expect(
            self.request(editor, "PATCH", f"/api/plaza/admin/issues/{issue_id}", data={
                "action": "resolve", "reason": "验收：已核对解析并采纳修订",
            }), 200, "处理纠错"), "处理纠错")
        repeat_issue = self.request(editor, "PATCH", f"/api/plaza/admin/issues/{issue_id}",
                                    data={"action": "resolve", "reason": "验收：重复处理"})
        resolved_report = self.payload(self.expect(
            self.request(editor, "PATCH", f"/api/plaza/admin/reports/{report_id}", data={
                "action": "resolve", "reason": "验收：已核验并处理举报",
            }), 200, "处理举报"), "处理举报")
        repeat_report = self.request(editor, "PATCH", f"/api/plaza/admin/reports/{report_id}",
                                     data={"action": "resolve", "reason": "验收：重复处理"})
        issue_notes = self._notifications(issue_reporter)
        report_notes = self._notifications(report_reporter)
        checks = [
            ("纠错提交成功（201 / pending）",
             issue_id > 0 and (issue.get("issue") or {}).get("status") == "pending",
             json.dumps(issue.get("issue"), ensure_ascii=False)[:220]),
            ("举报提交成功（201 / pending）",
             report_id > 0 and (report.get("report") or {}).get("status") == "pending",
             json.dumps(report.get("report"), ensure_ascii=False)[:220]),
            ("匿名不能提交纠错（401）", anon_issue.status_code == 401,
             f"HTTP {anon_issue.status_code}"),
            ("编辑纠错队列包含本次纠错", issue_id in issue_ids, f"ids={issue_ids[:20]}"),
            ("编辑举报队列包含本次举报", report_id in report_ids, f"ids={report_ids[:20]}"),
            ("反馈人展示名脱敏（不含用户名）",
             "反馈者·" in json.dumps(issue_queue.get("items") or [], ensure_ascii=False)
             and AUTHOR_NAME not in json.dumps(report_queue.get("items") or [], ensure_ascii=False),
             "反馈队列可能泄露用户名"),
            ("普通作者不能查看编辑反馈队列（403）", denied.status_code == 403,
             f"HTTP {denied.status_code}"),
            ("纠错 resolve 成功（status=resolved）",
             (resolved_issue.get("issue") or {}).get("status") == "resolved",
             json.dumps(resolved_issue.get("issue"), ensure_ascii=False)[:220]),
            ("举报 resolve 成功（status=resolved）",
             (resolved_report.get("report") or {}).get("status") == "resolved",
             json.dumps(resolved_report.get("report"), ensure_ascii=False)[:220]),
            ("重复处理纠错返回 409", repeat_issue.status_code == 409,
             f"HTTP {repeat_issue.status_code}"),
            ("重复处理举报返回 409", repeat_report.status_code == 409,
             f"HTTP {repeat_report.status_code}"),
            ("纠错人收到 issue_resolved 通知",
             bool(self._find_notifications(issue_notes, kind="issue_resolved",
                                           submission_id=int(b["submission_id"]))),
             f"kinds={[n.get('kind') for n in (issue_notes.get('items') or [])][:8]}"),
            ("举报人收到 report_resolved 通知",
             bool(self._find_notifications(report_notes, kind="report_resolved",
                                           submission_id=int(b["submission_id"]))),
             f"kinds={[n.get('kind') for n in (report_notes.get('items') or [])][:8]}"),
        ]
        state["feedback"] = {"issue_id": issue_id, "report_id": report_id}
        self.step("Phase3 纠错/举报提交 → 编辑台队列 resolve → 通知（权限/重复处理）",
                  checks, started=started, data={"issue_id": issue_id, "report_id": report_id})

    def phase3_favorite_and_withdraw_b(self) -> None:
        """Phase 3：收藏 → 列表投影 → 编辑撤回 → 收藏者/作者通知 + 不可再收藏。"""
        started = time.time()
        state = self._phase3()
        b = state["b"]
        public_id = b["public_id"]
        editor = self.clients["editor"]
        author = self.clients["author"]
        favoriter = self.clients["reviewer1"]
        anonymous = self.clients["anon"]
        favorite = self.payload(self.expect(
            self.request(favoriter, "POST", f"/api/plaza/questions/{public_id}/favorite"),
            200, "收藏公开题"), "收藏")
        favorite_repeat = self.payload(self.expect(
            self.request(favoriter, "POST", f"/api/plaza/questions/{public_id}/favorite"),
            200, "重复收藏（幂等）"), "重复收藏")
        detail = self.payload(self.expect(
            favoriter.request("GET", f"/api/plaza/questions/{public_id}", timeout=30.0),
            200, "收藏后公开详情"), "收藏后公开详情")
        favorites = self.payload(self.expect(
            favoriter.request("GET", "/api/plaza/favorites",
                              params={"page_size": 100}, timeout=30.0),
            200, "我的收藏"), "我的收藏")
        favorite_entry = next(
            (item for item in (favorites.get("items") or [])
             if isinstance(item, dict) and str(item.get("public_id") or "") == public_id), {})
        withdraw = self.payload(self.expect(
            self.request(editor, "POST", f"/api/plaza/releases/{public_id}/withdraw", data={
                "reason": "验收：Phase3 撤回后回滚恢复上一发布版本", "mode": "normal",
            }), 200, "编辑撤回发布"), "撤回发布")
        listing = self.payload(self.expect(
            anonymous.request("GET", "/api/plaza/questions",
                              params={"keyword": self.marker}, timeout=30.0),
            200, "撤回后公开列表"), "撤回后公开列表")
        listed_ids = [str(item.get("public_id") or "")
                      for item in (listing.get("items") or []) if isinstance(item, dict)]
        detail_after = self.payload(self.expect(
            anonymous.request("GET", f"/api/plaza/questions/{public_id}", timeout=30.0),
            200, "撤回后公开详情"), "撤回后公开详情")
        favorites_after = self.payload(self.expect(
            favoriter.request("GET", "/api/plaza/favorites",
                              params={"page_size": 100}, timeout=30.0),
            200, "撤回后我的收藏"), "撤回后我的收藏")
        favorite_after = next(
            (item for item in (favorites_after.get("items") or [])
             if isinstance(item, dict) and str(item.get("public_id") or "") == public_id), {})
        favorite_denied = self.request(favoriter, "POST",
                                       f"/api/plaza/questions/{public_id}/favorite")
        author_notes = self._notifications(author)
        favoriter_notes = self._notifications(favoriter)
        releases = self.payload(self.expect(
            editor.request("GET", "/api/plaza/releases",
                           params={"status": "withdrawn", "page_size": 100}, timeout=30.0),
            200, "发布历史（撤回）"), "发布历史")
        release_hit = next(
            (item for item in (releases.get("items") or [])
             if isinstance(item, dict) and str(item.get("public_id") or "") == public_id), {})
        checks = [
            ("收藏成功（favorited=true，首次非幂等）",
             favorite.get("favorited") is True and favorite.get("idempotent") is False
             and int(favorite.get("favorite_count") or 0) >= 1,
             json.dumps(favorite, ensure_ascii=False)[:220]),
            ("重复收藏幂等（idempotent=true）", favorite_repeat.get("idempotent") is True,
             json.dumps(favorite_repeat, ensure_ascii=False)[:200]),
            ("已收藏用户详情 favorited=true", detail.get("favorited") is True,
             f"favorited={detail.get('favorited')}"),
            ("我的收藏投影（published/featured/available=true）",
             favorite_entry.get("status") == "published"
             and favorite_entry.get("featured") is True
             and favorite_entry.get("available") is True,
             json.dumps(favorite_entry, ensure_ascii=False)[:240]),
            ("编辑撤回成功（status=withdrawn）", withdraw.get("status") == "withdrawn",
             json.dumps(withdraw, ensure_ascii=False)[:220]),
            ("撤回后匿名列表不可见", public_id not in listed_ids,
             f"listed={listed_ids[:8]}"),
            ("撤回后详情 visible=false", detail_after.get("visible") is False,
             f"visible={detail_after.get('visible')}"),
            ("我的收藏撤回投影（withdrawn / available=false / 状态提示）",
             favorite_after.get("status") == "withdrawn"
             and favorite_after.get("available") is False
             and bool(str(favorite_after.get("notice") or "")),
             json.dumps(favorite_after, ensure_ascii=False)[:260]),
            ("撤回后不能再收藏（409）", favorite_denied.status_code == 409,
             f"HTTP {favorite_denied.status_code}"),
            ("作者收到 release_withdrawn 通知",
             bool(self._find_notifications(author_notes, kind="release_withdrawn",
                                           submission_id=int(b["submission_id"]))),
             f"kinds={[n.get('kind') for n in (author_notes.get('items') or [])][:8]}"),
            ("收藏者收到 favorite_withdrawn 通知",
             bool(self._find_notifications(favoriter_notes, kind="favorite_withdrawn",
                                           public_id=public_id)),
             f"kinds={[n.get('kind') for n in (favoriter_notes.get('items') or [])][:8]}"),
            ("发布历史含本次撤回（status=withdrawn）",
             release_hit.get("status") == "withdrawn",
             json.dumps(release_hit, ensure_ascii=False)[:220]),
        ]
        self.step("Phase3 收藏 → 撤回（收藏者/作者通知、收藏投影、不可再收藏）",
                  checks, started=started, data={"favoriter": "reviewer1"})

    def phase3_revise_republish_rollback(self) -> None:
        """Phase 3：改版 v2 重新评审发布 → rollback 恢复上一已发布版本 → 公开正文回旧版。"""
        started = time.time()
        state = self._phase3()
        b = state["b"]
        submission_id = int(b["submission_id"])
        public_id = b["public_id"]
        author = self.clients["author"]
        editor = self.clients["editor"]
        anonymous = self.clients["anon"]
        q_patch = self.payload(self.expect(
            self.request(author, "PATCH", f"/api/paperlab/questions/{b['question_id']}", data={
                "version": 1,
                "content": {
                    "stem": f"【{self.marker}】Phase3-B 修订版：已知函数 f(x)=2x+1，求 f(2) 的值。",
                },
            }), 200, "编辑个人题（v2 内容）"), "编辑个人题")
        question_version = int(((q_patch.get("question") or {}).get("version")) or 0)
        revised = self.payload(self.expect(
            self.request(author, "POST", f"/api/plaza/submissions/{submission_id}/revise",
                         data={"version": int(b["version"])}),
            200, "改版创建新投稿版本"), "改版")
        revised_submission = revised.get("submission") or {}
        new_version = int(revised_submission.get("version") or 0)
        self.expect(self.request(
            editor, "POST", f"/api/plaza/submissions/{submission_id}/initial-review", data={
                "version": new_version,
                "decision": "accept",
                "reason": "验收：Phase3-B v2 初审通过",
                "source_checked": True,
                "privacy_checked": True,
                "completeness_checked": True,
            }), 200, "v2 编辑初审")
        review_order = [
            "subject_reviewer1", "subject_reviewer2", "reviewer1", "reviewer2", "reviewer3",
        ]
        transitions: list[str] = []
        for index, key in enumerate(review_order, start=1):
            reviewed_body = self.payload(self.expect(
                self.request(self.clients[key], "POST",
                             f"/api/plaza/submissions/{submission_id}/reviews", data={
                                 "version": new_version,
                                 "verdict": "approve",
                                 "reasons": "",
                                 "independent_answer": f"验收 v2 独立作答（第 {index} 票）：A",
                                 "conflict_declared": False,
                             }), 201, f"v2 第 {index} 票"), "v2 评审响应")
            transitions.append(str(reviewed_body.get("transition") or ""))
        published = self.payload(self.expect(
            self.request(editor, "POST", f"/api/plaza/submissions/{submission_id}/publish", data={
                "version": new_version,
                "reason": "验收：Phase3-B v2 评审阈值达成，签发精选",
            }), 200, "v2 签发精选"), "v2 签发")
        detail_v2 = self.payload(self.expect(
            anonymous.request("GET", f"/api/plaza/questions/{public_id}", timeout=30.0),
            200, "v2 公开详情"), "v2 公开详情")
        v2_text = json.dumps(detail_v2, ensure_ascii=False)
        rollback = self.payload(self.expect(
            self.request(editor, "POST", f"/api/plaza/submissions/{submission_id}/rollback", data={
                "reason": "验收：Phase3 回滚到上一已发布版本",
            }), 200, "回滚发布"), "回滚")
        detail_v1 = self.payload(self.expect(
            anonymous.request("GET", f"/api/plaza/questions/{public_id}", timeout=30.0),
            200, "回滚后公开详情"), "回滚后公开详情")
        v1_text = json.dumps(detail_v1, ensure_ascii=False)
        releases = self.payload(self.expect(
            editor.request("GET", "/api/plaza/releases",
                           params={"page_size": 100}, timeout=30.0),
            200, "回滚后发布历史"), "回滚后发布历史")
        b_releases = [item for item in (releases.get("items") or [])
                      if isinstance(item, dict)
                      and int(item.get("submission_id") or 0) == submission_id]
        active_releases = [item for item in b_releases if item.get("status") == "active"]
        rollback_releases = [item for item in b_releases
                             if item.get("status") == "withdrawn"
                             and item.get("withdraw_mode") == "rollback"]
        second_rollback = self.request(
            editor, "POST", f"/api/plaza/submissions/{submission_id}/rollback",
            data={"reason": "验收：没有更早历史版本时回滚应 409"})
        author_submission = self.payload(self.expect(
            author.request("GET", f"/api/plaza/submissions/{submission_id}", timeout=30.0),
            200, "回滚后投稿详情"), "回滚后投稿详情").get("submission") or {}
        author_notes = self._notifications(author)
        favoriter_notes = self._notifications(self.clients["reviewer1"])
        checks = [
            ("个人题内容已修订（版本 2）", question_version == 2, f"version={question_version}"),
            ("改版创建新投稿版本并回到待评",
             new_version == int(b["version"]) + 1
             and revised_submission.get("status") == "pending_initial",
             json.dumps({k: revised_submission.get(k) for k in ("status", "version")},
                        ensure_ascii=False)),
            ("v2 五票自动 verified", transitions[-1:] == ["verified"], f"transitions={transitions}"),
            ("v2 签发 active 发布",
             (published.get("release") or {}).get("status") == "active",
             json.dumps(published.get("release"), ensure_ascii=False)[:220]),
            ("v2 公开详情为修订版正文", "2x+1" in v2_text,
             str((detail_v2.get("content") or {}).get("stem"))[:160]),
            ("回滚恢复上一已发布版本（restored_version=发布版本）",
             int(rollback.get("restored_version") or 0) == int(b["version"]),
             json.dumps({k: rollback.get(k) for k in ("restored_version", "message")},
                        ensure_ascii=False)[:240]),
            ("回滚后投稿 published 且版本恢复",
             (rollback.get("submission") or {}).get("status") == "published"
             and int((rollback.get("submission") or {}).get("version") or 0) == int(b["version"])
             and author_submission.get("status") == "published",
             json.dumps(rollback.get("submission"), ensure_ascii=False)[:240]),
            ("回滚后公开详情读回旧版本正文",
             "2x+1" not in v1_text and "x+1" in v1_text,
             str((detail_v1.get("content") or {}).get("stem"))[:160]),
            ("发布历史：恢复一条 active + 一条 withdrawn(rollback)",
             len(active_releases) == 1 and len(rollback_releases) >= 1,
             json.dumps([{k: item.get(k) for k in ("status", "version", "withdraw_mode")}
                         for item in b_releases], ensure_ascii=False)[:260]),
            ("无更早历史版本时再次回滚 409", second_rollback.status_code == 409,
             f"HTTP {second_rollback.status_code}"),
            ("作者收到 release_rolled_back 通知",
             bool(self._find_notifications(author_notes, kind="release_rolled_back",
                                           submission_id=submission_id)),
             f"kinds={[n.get('kind') for n in (author_notes.get('items') or [])][:8]}"),
            ("收藏者收到 favorite_restored 通知",
             bool(self._find_notifications(favoriter_notes, kind="favorite_restored",
                                           public_id=public_id)),
             f"kinds={[n.get('kind') for n in (favoriter_notes.get('items') or [])][:8]}"),
        ]
        self.step("Phase3 改版 v2 重新发布 → rollback 恢复上一版本（公开正文/历史/通知）",
                  checks, started=started,
                  data={"new_version": new_version, "restored_version": b["version"]})

    def phase3_quality_high_withdraw(self) -> None:
        """Phase 3：对另一条题抽检 issue+high → 自动撤回（公开不可见/发布历史/作者+收藏者通知）。"""
        started = time.time()
        state = self._phase3()
        c = state["c"]
        public_id = c["public_id"]
        editor = self.clients["editor"]
        author = self.clients["author"]
        favoriter = self.clients["reviewer2"]
        anonymous = self.clients["anon"]
        favorite = self.payload(self.expect(
            self.request(favoriter, "POST", f"/api/plaza/questions/{public_id}/favorite"),
            200, "收藏 Phase3-C"), "收藏 Phase3-C")
        created = self.request(editor, "POST", "/api/plaza/admin/quality/checks", data={
            "public_id": public_id,
            "decision": "issue",
            "severity": "high",
            "findings": [
                "验收：题干条件缺失，导致答案不唯一",
                "验收：选项与参考答案不匹配",
            ],
            "note": f"验收：Phase3 高风险抽检自动撤回（{self.run_id}）",
        })
        self.expect(created, 201, "抽检 issue+high")
        body = self.payload(created, "抽检 issue+high 响应")
        check = body.get("check") or {}
        notified = body.get("notified") or {}
        listing = self.payload(self.expect(
            anonymous.request("GET", "/api/plaza/questions",
                              params={"keyword": self.marker}, timeout=30.0),
            200, "高风险撤回后公开列表"), "撤回后公开列表")
        listed_ids = [str(item.get("public_id") or "")
                      for item in (listing.get("items") or []) if isinstance(item, dict)]
        detail = self.payload(self.expect(
            anonymous.request("GET", f"/api/plaza/questions/{public_id}", timeout=30.0),
            200, "高风险撤回后公开详情"), "撤回后公开详情")
        releases = self.payload(self.expect(
            editor.request("GET", "/api/plaza/releases",
                           params={"status": "withdrawn", "page_size": 100}, timeout=30.0),
            200, "高风险撤回发布历史"), "发布历史")
        release_hit = next(
            (item for item in (releases.get("items") or [])
             if isinstance(item, dict) and str(item.get("public_id") or "") == public_id), {})
        queue = self.payload(self.expect(
            editor.request("GET", "/api/plaza/admin/quality/queue",
                           params={"status": "issues", "page_size": 50}, timeout=30.0),
            200, "抽检问题队列"), "抽检问题队列")
        queue_entry = next(
            (item for item in (queue.get("items") or [])
             if isinstance(item, dict)
             and int(item.get("submission_id") or 0) == int(c["submission_id"])), {})
        favorites_after = self.payload(self.expect(
            favoriter.request("GET", "/api/plaza/favorites",
                              params={"page_size": 100}, timeout=30.0),
            200, "高风险撤回后收藏列表"), "收藏列表")
        favorite_after = next(
            (item for item in (favorites_after.get("items") or [])
             if isinstance(item, dict) and str(item.get("public_id") or "") == public_id), {})
        author_notes = self._notifications(author)
        favoriter_notes = self._notifications(favoriter)
        metrics = self.payload(self.expect(
            editor.request("GET", "/api/plaza/admin/metrics", timeout=30.0),
            200, "高风险撤回后指标"), "指标")
        quality = metrics.get("quality") or {}
        checks = [
            ("收藏 Phase3-C 成功", favorite.get("favorited") is True,
             json.dumps(favorite, ensure_ascii=False)[:200]),
            ("抽检 issue+high 记录创建（auto_withdrawn=true）",
             body.get("auto_withdrawn") is True and check.get("severity") == "high"
             and check.get("decision") == "issue" and bool(check.get("findings")),
             json.dumps(check, ensure_ascii=False)[:260]),
            ("自动撤回通知作者与收藏者",
             notified.get("author") is True and int(notified.get("favoriters") or 0) >= 1,
             json.dumps(notified, ensure_ascii=False)),
            ("撤回后投稿状态 withdrawn / 公开不可见",
             (body.get("submission") or {}).get("status") == "withdrawn"
             and (body.get("submission") or {}).get("public_visible") is False,
             json.dumps(body.get("submission"), ensure_ascii=False)[:240]),
            ("撤回后匿名列表不再包含该题", public_id not in listed_ids,
             f"listed={listed_ids[:8]}"),
            ("撤回后旧链接 visible=false 且不回显正文",
             detail.get("visible") is False
             and str((detail.get("content") or {}).get("stem") or "") == "",
             json.dumps({k: detail.get(k) for k in ("visible", "status")}, ensure_ascii=False)),
            ("发布历史含本次高风险撤回",
             release_hit.get("status") == "withdrawn",
             json.dumps(release_hit, ensure_ascii=False)[:220]),
            ("抽检问题队列标记 auto_withdrawn 与 high",
             queue_entry.get("auto_withdrawn") is True
             and queue_entry.get("check_status") == "issue"
             and (queue_entry.get("latest_check") or {}).get("severity") == "high",
             json.dumps(queue_entry, ensure_ascii=False)[:260]),
            ("收藏者收藏投影 withdrawn / available=false",
             favorite_after.get("status") == "withdrawn"
             and favorite_after.get("available") is False,
             json.dumps(favorite_after, ensure_ascii=False)[:240]),
            ("作者收到 quality_withdrawn 通知",
             bool(self._find_notifications(author_notes, kind="quality_withdrawn",
                                           public_id=public_id)),
             f"kinds={[n.get('kind') for n in (author_notes.get('items') or [])][:8]}"),
            ("收藏者收到 favorite_withdrawn 通知",
             bool(self._find_notifications(favoriter_notes, kind="favorite_withdrawn",
                                           public_id=public_id)),
             f"kinds={[n.get('kind') for n in (favoriter_notes.get('items') or [])][:8]}"),
            ("指标 high_issues >= 1 且问题率 > 0",
             int(quality.get("high_issues") or 0) >= 1
             and float(quality.get("issue_rate") or 0) > 0,
             json.dumps({k: quality.get(k) for k in ("checks_total", "issues", "high_issues",
                                                     "issue_rate")}, ensure_ascii=False)),
        ]
        state["high_check_id"] = int(check.get("id") or 0)
        self.step("Phase3 另一条题抽检 issue+high → 自动撤回（公开/历史/通知/指标）",
                  checks, started=started,
                  data={"check_id": state["high_check_id"], "public_id": public_id})

    def phase3_appeal_flow(self) -> None:
        """Phase 3：作者对 withdrawn 投稿申诉 → 编辑台处理 → 作者通知（幂等/权限）。"""
        started = time.time()
        state = self._phase3()
        submission_id = int(self.data["submission_id"])
        author = self.clients["author"]
        editor = self.clients["editor"]
        reviewer = self.clients["reviewer1"]
        reason = f"验收：Phase3 对撤回有异议，申请复核（{self.run_id}）"
        appeal = self.payload(self.expect(
            self.request(author, "POST", f"/api/plaza/submissions/{submission_id}/appeals",
                         data={"reason": reason}), 201, "作者申诉"), "申诉")
        appeal_id = int(((appeal.get("appeal") or {}).get("id")) or 0)
        appeal_repeat = self.payload(self.expect(
            self.request(author, "POST", f"/api/plaza/submissions/{submission_id}/appeals",
                         data={"reason": reason}), 200, "重复申诉（幂等）"), "重复申诉")
        queue = self.payload(self.expect(
            editor.request("GET", "/api/plaza/admin/appeals",
                           params={"status": "pending", "page_size": 50}, timeout=30.0),
            200, "申诉队列"), "申诉队列")
        queue_ids = [int(entry.get("id") or 0) for entry in (queue.get("items") or [])
                     if isinstance(entry, dict)]
        queue_entry = next((entry for entry in (queue.get("items") or [])
                            if isinstance(entry, dict)
                            and int(entry.get("id") or 0) == appeal_id), {})
        denied = reviewer.request("POST", f"/api/plaza/submissions/{submission_id}/appeals",
                                  json={"reason": "验收：非作者尝试申诉"}, timeout=30.0)
        resolved = self.payload(self.expect(
            self.request(editor, "PATCH", f"/api/plaza/admin/appeals/{appeal_id}", data={
                "action": "resolve", "reason": "验收：申诉受理并已记录复核结论",
            }), 200, "处理申诉"), "处理申诉")
        repeat_resolve = self.request(editor, "PATCH", f"/api/plaza/admin/appeals/{appeal_id}",
                                      data={"action": "resolve", "reason": "验收：重复处理"})
        history = self.payload(self.expect(
            author.request("GET", f"/api/plaza/submissions/{submission_id}/appeals", timeout=30.0),
            200, "作者申诉记录"), "申诉记录")
        author_notes = self._notifications(author)
        history_item = next(
            (item for item in (history.get("items") or [])
             if isinstance(item, dict) and int(item.get("id") or 0) == appeal_id), {})
        checks = [
            ("作者对 withdrawn 投稿申诉成功（pending）",
             appeal_id > 0 and (appeal.get("appeal") or {}).get("status") == "pending"
             and appeal.get("idempotent") is False,
             json.dumps(appeal.get("appeal"), ensure_ascii=False)[:240]),
            ("重复申诉幂等返回既有待处理申诉",
             appeal_repeat.get("idempotent") is True, json.dumps(appeal_repeat, ensure_ascii=False)[:200]),
            ("编辑申诉队列包含本次申诉", appeal_id in queue_ids, f"ids={queue_ids[:20]}"),
            ("申诉人展示名脱敏（不含用户名）",
             str(queue_entry.get("author_display") or "").startswith("申诉作者·")
             and AUTHOR_NAME not in json.dumps(queue_entry, ensure_ascii=False),
             json.dumps(queue_entry.get("author_display"), ensure_ascii=False)),
            ("非作者不能申诉他人投稿（404）", denied.status_code == 404,
             f"HTTP {denied.status_code}"),
            ("申诉 resolve 成功（status=resolved）",
             (resolved.get("appeal") or {}).get("status") == "resolved",
             json.dumps(resolved.get("appeal"), ensure_ascii=False)[:240]),
            ("重复处理申诉返回 409", repeat_resolve.status_code == 409,
             f"HTTP {repeat_resolve.status_code}"),
            ("作者申诉记录可见已处理结果",
             history_item.get("status") == "resolved" and bool(history_item.get("resolution")),
             json.dumps(history_item, ensure_ascii=False)[:240]),
            ("作者收到 appeal_resolved 通知",
             bool(self._find_notifications(author_notes, kind="appeal_resolved",
                                           submission_id=submission_id)),
             f"kinds={[n.get('kind') for n in (author_notes.get('items') or [])][:8]}"),
        ]
        state["appeal_id"] = appeal_id
        self.step("Phase3 作者申诉 → 编辑处理 → 通知（幂等/脱敏/权限）", checks,
                  started=started, data={"appeal_id": appeal_id,
                                         "submission_id": submission_id})

    def phase3_notification_read_flow(self) -> None:
        """Phase 3：通知中心 unread → read（ids / all）→ 未读数下降；匿名 401。"""
        started = time.time()
        reviewer = self.clients["reviewer1"]
        before = self._notifications(reviewer, unread_only=True)
        unread_before = int(before.get("unread_count") or 0)
        ids = [int(item.get("id") or 0)
               for item in (before.get("items") or []) if isinstance(item, dict)][:2]
        read = self.payload(self.expect(
            self.request(reviewer, "POST", "/api/plaza/notifications/read",
                         data={"ids": ids}), 200, "按 ids 标记已读"), "标记已读")
        after = self._notifications(reviewer, unread_only=True)
        after_ids = [int(item.get("id") or 0)
                     for item in (after.get("items") or []) if isinstance(item, dict)]
        read_all = self.payload(self.expect(
            self.request(reviewer, "POST", "/api/plaza/notifications/read",
                         data={"all": True}), 200, "全部标记已读"), "全部已读")
        invalid = self.request(reviewer, "POST", "/api/plaza/notifications/read", data={})
        anonymous = self.request(self.clients["anon"], "GET", "/api/plaza/notifications")
        checks = [
            ("存在未读通知（unread_count>0）", unread_before > 0 and bool(ids),
             f"unread={unread_before}"),
            ("按 ids 标记已读：updated>=1 且未读数下降",
             int(read.get("updated") or 0) >= 1
             and int(read.get("unread_count") or 0) < unread_before,
             json.dumps(read, ensure_ascii=False)[:200]),
            ("已读 id 不再出现在未读列表",
             all(item_id not in after_ids for item_id in ids),
             f"read_ids={ids} after_ids={after_ids[:10]}"),
            ("未读列表与已读接口返回的未读数一致",
             int(after.get("unread_count") or 0) == int(read.get("unread_count") or 0),
             f"after={after.get('unread_count')} read={read.get('unread_count')}"),
            ("all=true 全部已读后未读数归零", int(read_all.get("unread_count") or 0) == 0,
             json.dumps(read_all, ensure_ascii=False)[:200]),
            ("空请求返回 422", invalid.status_code == 422, f"HTTP {invalid.status_code}"),
            ("匿名不能读通知中心（401）", anonymous.status_code == 401,
             f"HTTP {anonymous.status_code}"),
        ]
        self.step("Phase3 通知中心 unread → read（ids/all）→ 未读数下降", checks,
                  started=started, data={"unread_before": unread_before})

    def phase3_metrics(self) -> None:
        """Phase 3：质量指标口径（抽检问题率/撤回/回滚/反馈）与权限负向。"""
        started = time.time()
        state = self._phase3()
        editor = self.clients["editor"]
        author = self.clients["author"]
        reviewer = self.clients["reviewer1"]
        metrics = self.payload(self.expect(
            editor.request("GET", "/api/plaza/admin/metrics", timeout=30.0),
            200, "质量指标（最终）"), "质量指标")
        quality = metrics.get("quality") or {}
        sampling = quality.get("sampling") or {}
        feedback = metrics.get("feedback") or {}
        withdrawals = metrics.get("withdrawals") or {}
        totals = metrics.get("totals") or {}
        author_denied = author.request("GET", "/api/plaza/admin/metrics", timeout=30.0)
        reviewer_denied = reviewer.request("GET", "/api/plaza/admin/metrics", timeout=30.0)
        checks = [
            ("抽检记录累计 >= 2（pass + issue）",
             int(quality.get("checks_total") or 0) >= 2
             and int(quality.get("passed") or 0) >= 1
             and int(quality.get("issues") or 0) >= 1,
             json.dumps({k: quality.get(k) for k in ("checks_total", "passed", "issues")},
                        ensure_ascii=False)),
            ("高风险问题数 >= 1 且问题率 > 0",
             int(quality.get("high_issues") or 0) >= 1
             and float(quality.get("issue_rate") or 0) > 0,
             json.dumps({k: quality.get(k) for k in ("high_issues", "issue_rate")},
                        ensure_ascii=False)),
            ("首批 100% 抽检覆盖率口径可用（initial_100）",
             sampling.get("mode") == "initial_100"
             and float(sampling.get("coverage_rate") or 0) > 0,
             json.dumps(sampling, ensure_ascii=False)[:240]),
            ("反馈指标覆盖纠错/举报/申诉",
             int(feedback.get("issues_total") or 0) >= 1
             and int(feedback.get("reports_total") or 0) >= 1
             and int(feedback.get("appeals_total") or 0) >= 1,
             json.dumps(feedback, ensure_ascii=False)[:240]),
            ("发布撤回数 >= 3 且回滚次数 >= 1",
             int(withdrawals.get("releases_withdrawn") or 0) >= 3
             and int(withdrawals.get("rollbacks") or 0) >= 1,
             json.dumps(withdrawals, ensure_ascii=False)[:240]),
            ("投稿状态分布含 published / withdrawn",
             int(totals.get("published") or 0) >= 1
             and int(totals.get("withdrawn") or 0) >= 2,
             json.dumps({k: totals.get(k) for k in ("published", "withdrawn")},
                        ensure_ascii=False)),
            ("指标附带口径说明（caliber）", bool(metrics.get("caliber"))),
            ("普通作者不能读质量指标（403）", author_denied.status_code == 403,
             f"HTTP {author_denied.status_code}"),
            ("评审员不能读质量指标（403）", reviewer_denied.status_code == 403,
             f"HTTP {reviewer_denied.status_code}"),
        ]
        self.step("Phase3 质量指标口径（抽检问题率/撤回/回滚/反馈）与权限负向", checks,
                  started=started, data={"quality": {k: quality.get(k) for k in (
                      "checks_total", "passed", "issues", "high_issues", "issue_rate")}})

    # -- 主流程 -----------------------------------------------------------
    def run(self) -> None:
        self.clients["anon"] = self.new_client()
        started = time.time()
        health = self.request(self.clients["anon"], "GET", "/api/health")
        self.expect(health, 200, "隔离实例健康检查")
        health_body = self.payload(health, "健康检查")
        self.report["data"]["health"] = {
            "status": health_body.get("status"),
            "build_id": health_body.get("build_id"),
            "bank_available": health_body.get("bank_available"),
            "bank_version": health_body.get("bank_version"),
        }
        self.warning(f"隔离实例 build_id={health_body.get('build_id')} "
                     f"bank_available={health_body.get('bank_available')}")

        self.prepare_accounts()
        self.grant_roles()
        # WS28 抽检门禁兼容：实例显式 armed 时先临时关闭门禁，链路结束（含失败）恢复原模式。
        self.ensure_gate_compatible()
        try:
            self.create_question()
            self.create_submission()
            self.patch_submission()
            self.submit_submission()
            self.initial_review()
            self.assert_community_public()
            self.assert_queues_and_events()

            # UI：社区待核验（公开列表/详情、作者投稿台、评审员评审台）
            self.ui_hook("广场公开页与工作台（社区待核验）", ui_flow_community,
                         self.data["public_id"], self.data["submission_id"], self.marker)

            self.hide_votes_check()
            self.submit_reviews()
            self.assert_verified_public()
            self.ui_hook("编辑台待发布队列（核验通过）", ui_flow_editor_queue,
                         self.data["submission_id"], self.marker)
            self.publish()
            self.assert_featured_public()
            self.ui_hook("广场公开页（平台精选）", ui_flow_featured,
                         self.data["public_id"], self.marker)
            self.withdraw()
            self.assert_withdrawn_invisible()
            self.ui_hook("广场公开详情（已撤回隐藏态）", ui_flow_withdrawn,
                         self.data["public_id"], self.marker)
            self.identity_flow()

            # ---- Phase 3：质量运营 / 互动 / 回滚 / 通知 ------------------
            self.phase3_publish_extra_questions()
            self.phase3_quality_pass()
            self.phase3_feedback_on_b()
            self.phase3_favorite_and_withdraw_b()
            self.phase3_revise_republish_rollback()
            self.phase3_quality_high_withdraw()
            self.phase3_appeal_flow()

            phase3 = self._phase3()
            self.ui_hook("Phase3 广场详情（收藏/纠错/举报，桌面+手机）",
                         ui_flow_phase3_detail, phase3["b"]["public_id"],
                         phase3["b"]["stem_v1"])
            self.ui_hook("Phase3 通知铃铛与通知面板", ui_flow_notifications,
                         REVIEWER_NAMES[0])
            self.ui_hook("Phase3 编辑台抽检/反馈/指标分区", ui_flow_admin_phase3)
            self.ui_hook("Phase3 作者投稿台申诉卡", ui_flow_appeal,
                         int(self.data["submission_id"]))

            self.phase3_notification_read_flow()
            self.phase3_metrics()

            self.report["elapsed_http_seconds"] = round(time.time() - started, 2)
        finally:
            self.restore_gate()

    # -- 收尾 -------------------------------------------------------------
    def finalize(self) -> int:
        endpoints = [self.api_calls[key] for key in sorted(self.api_calls)]
        total_calls = sum(item["calls"] for item in endpoints)
        self.report["api"] = {
            "unique_endpoints": len(endpoints),
            "total_calls": total_calls,
            "endpoints": endpoints,
        }
        passed = sum(1 for item in self.report["steps"] if item.get("ok"))
        total = len(self.report["steps"])
        self.report["http"] = {
            "ok": not self.report["problems"],
            "passed_steps": passed,
            "step_count": total,
            "failed_steps": total - passed,
        }
        self.report["ui"] = self.ui.payload() if self.ui is not None else {
            "enabled": False, "skipped": True, "ok": None,
            "steps": [], "problems": [], "warnings": ["通过 --skip-ui 跳过 UI 冒烟"],
            "screenshots": [], "step_count": 0, "passed_steps": 0,
        }
        if self.ui is not None and self.ui.enabled and self.ui.problems:
            self.warning(f"UI 冒烟存在 {len(self.ui.problems)} 项问题"
                         "（不阻断 HTTP 链路判定，详见报告 ui.problems）")
        self.report["ok"] = not self.report["problems"]
        self.report.setdefault(
            "elapsed_seconds",
            round(self.report.get("elapsed_http_seconds", 0.0), 2))
        self.report["exit_code"] = 0 if self.report["ok"] else 1
        self.report["environment"] = {
            "base_url": self.base,
            "http_client": "httpx.Client(trust_env=False)（本机系统代理会劫持 127.0.0.1）",
            "arguments": dict(self.cli, admin_username=self.admin_username),
            "admin_source": self.admin_source,
            "required_env": [
                "PYTEST_DISABLE_PLUGIN_AUTOLOAD=1",
                "STUDY_ALLOWED_HOSTS=127.0.0.1,localhost,[::1],testserver",
                "STUDY_INDEX_PREWARM=0",
                "STUDY_DATA_DIR=<临时目录>（必须全新，首个注册账号自动成为管理员）",
            ],
            "instance_start_example": (
                "pwsh: cd <repo>; $env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'; "
                "$env:STUDY_ALLOWED_HOSTS='127.0.0.1,localhost,[::1],testserver'; "
                "$env:STUDY_INDEX_PREWARM='0'; "
                "$env:STUDY_DATA_DIR=Join-Path $env:TEMP ('zhixu-plaza-accept-'+(Get-Random)); "
                "python -m uvicorn app.main:app --host 127.0.0.1 --port 8846"
            ),
            "run_command": (
                "python scripts/integration/plaza_accept.py "
                "--base-url http://127.0.0.1:8846 "
                "[--admin-username <管理员> --admin-password <密码>] "
                "[--admin-token <初始化口令>] [--skip-ui]"
            ),
        }
        self.report_path.parent.mkdir(parents=True, exist_ok=True)
        self.report_path.write_text(
            json.dumps(self.report, ensure_ascii=False, indent=2), encoding="utf-8")
        return self.report["exit_code"]


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def _configure_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001 - 非交互流不支持时忽略
            pass


def _summarize(report: dict) -> dict:
    http = report.get("http") or {}
    ui = report.get("ui") or {}
    return {
        "ok": report.get("ok"),
        "exit_code": report.get("exit_code"),
        "http": {
            "ok": http.get("ok"),
            "steps": f"{http.get('passed_steps', 0)}/{http.get('step_count', 0)}",
        },
        "ui": {
            "enabled": ui.get("enabled"),
            "ok": ui.get("ok"),
            "steps": f"{ui.get('passed_steps', 0)}/{ui.get('step_count', 0)}",
            "problems": len(ui.get("problems") or []),
            "screenshots": len(ui.get("screenshots") or []),
        },
        "api": {
            "unique_endpoints": (report.get("api") or {}).get("unique_endpoints"),
            "total_calls": (report.get("api") or {}).get("total_calls"),
        },
        "data": {
            "run_id": (report.get("data") or {}).get("run_id"),
            "question_id": (report.get("data") or {}).get("question_id"),
            "submission_id": (report.get("data") or {}).get("submission_id"),
            "public_id": (report.get("data") or {}).get("public_id"),
            "phase3": {
                "b": (((report.get("data") or {}).get("phase3") or {}).get("b") or {}).get("public_id"),
                "c": (((report.get("data") or {}).get("phase3") or {}).get("c") or {}).get("public_id"),
            },
        },
        "problems": report.get("problems") or [],
        "warnings": (report.get("warnings") or [])[:10],
        "elapsed_seconds": report.get("elapsed_seconds"),
        "report": report.get("report_path"),
    }


def main(argv: list[str] | None = None) -> int:
    _configure_console()
    parser = argparse.ArgumentParser(
        description="广场 Phase 2 端到端验收（HTTP 全链路 + 可选 Playwright UI 冒烟）")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL,
                        help=f"隔离实例地址（默认 {DEFAULT_BASE_URL}）")
    parser.add_argument("--out", default=str(DEFAULT_OUT),
                        help="截图输出目录（相对仓库根，默认 screenshots/integration）")
    parser.add_argument("--json", default=str(DEFAULT_REPORT),
                        help="报告 JSON 路径（相对仓库根，默认 "
                             "screenshots/integration/plaza-accept-report.json）")
    parser.add_argument("--skip-ui", action="store_true", help="跳过 Playwright UI 冒烟")
    parser.add_argument(
        "--admin-username",
        help="已存在的管理员账号（用于 role=admin 授权角色）；省略时使用默认验收账号并依赖"
             "「本地模式首个注册账号=管理员」")
    parser.add_argument("--admin-password",
                        help="--admin-username 对应密码（省略时使用默认验收密码）")
    parser.add_argument(
        "--admin-token",
        help="公网模式一次性管理员初始化口令（对应实例 STUDY_ADMIN_INIT_TOKEN）："
             "登录失败时尝试 POST /api/auth/init-admin")
    options = parser.parse_args(argv)

    admin_username = (options.admin_username or ADMIN_NAME).strip()
    admin_password = options.admin_password or PASSWORD
    admin_token = (options.admin_token or "").strip()

    out_dir = Path(options.out)
    if not out_dir.is_absolute():
        out_dir = ROOT / out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    report_path = Path(options.json)
    if not report_path.is_absolute():
        report_path = ROOT / report_path
    report_path.parent.mkdir(parents=True, exist_ok=True)

    run_id = datetime.now().strftime("%Y%m%d%H%M%S") + secrets.token_hex(2)
    ui: UiSmoke | None = None
    if not options.skip_ui:
        ui = UiSmoke(options.base_url, out_dir)
        if not ui.start():
            report_warning = ui.warnings[-1] if ui.warnings else "UI 冒烟不可用"
            print(json.dumps({"ui": "skipped", "reason": report_warning},
                             ensure_ascii=False))
    flow = PlazaAccept(
        options.base_url, out_dir, report_path, ui, run_id,
        admin_username=admin_username,
        admin_password=admin_password,
        admin_username_explicit=bool(options.admin_username),
        admin_token=admin_token,
        skip_ui=bool(options.skip_ui),
    )
    if admin_token:
        flow.warning("已提供 --admin-token：登录失败时将尝试 POST /api/auth/init-admin")
    if options.admin_username:
        flow.warning(f"已通过 --admin-username 指定管理员账号「{admin_username}」（不依赖首个注册者）")
    if ui is None:
        flow.warning("UI 冒烟未执行（--skip-ui 跳过）")
    elif not ui.enabled:
        flow.warning("UI 冒烟未执行（Playwright / 浏览器不可用，已记录到 ui.warnings）")
    started = time.time()
    try:
        flow.run()
    except FlowFailure as exc:
        if str(exc) not in flow.report["problems"]:
            flow.report["problems"].append(str(exc))
        # 可读失败：只打印简短原因（不输出堆栈），完整报告落在 JSON。
        print(json.dumps({"step_failed": str(exc)}, ensure_ascii=False))
    except Exception as exc:  # noqa: BLE001 - 未知异常也要落盘可读原因
        flow.report["problems"].append(f"未预期异常：{exc.__class__.__name__}: {exc}")
        traceback.print_exc()
    finally:
        flow.close()
        if ui is not None:
            ui.stop()
        flow.report["elapsed_seconds"] = round(time.time() - started, 2)
        flow.report["report_path"] = str(report_path)
        code = flow.finalize()

    summary = _summarize(flow.report)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return code


if __name__ == "__main__":
    sys.exit(main())
