"""WS22 广场质量运营前端（Phase 3 前端之二）静态契约测试。

无需浏览器、不联网：直接读源码断言页面/组件契约、接口路径与请求体、
locale 中英文键对齐，并对改动的 ESM 做真解析（vm.SourceTextModule + node --check）。

覆盖：
1. 公开详情（plaza.js）：收藏/纠错/举报按钮与 WS21 接口、匿名引导登录、
   收藏状态（payload favorited / GET /favorites 兜底）、操作 toast；
   同时保留 WS15 的来源声明、待核验提示与「展开答案」等既有契约。
2. 通知中心（components/plazaNotifications.js + main.js）：登录主壳挂载铃铛、
   未读数、打开面板、标记已读（ids/all）、link hash 跳转、清理轮询；
   访客壳不挂载通知组件，不破坏既有壳层/语言切换/移动端逻辑。
3. 编辑台（plazaAdmin.js）：抽检分区（pass/issue+severity+findings、high 自动撤回提示）、
   纠错/举报/申诉队列（PATCH resolve/reject + 理由）、质量指标卡片、发布历史回滚；
   新接口 404 时占位不崩页；既有初审/签发/撤回契约保持。
4. 投稿台（submission.js）：rejected/withdrawn/disputed 申诉表单（POST /appeals）
   与通知中心入口；事件时间线保留。
5. locale：plaza.ops./notify./favorite./report./appeal./quality./metrics. 中英对齐，
   页面里所有 t('literal') 键在 zh-CN/en-US 都存在。
6. 改动 JS 全部通过 node --check 与 ESM 解析（无 Node 时跳过）。
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web"
JS = WEB / "js"
PLAZA_JS = JS / "pages" / "plaza.js"
ADMIN_JS = JS / "pages" / "plazaAdmin.js"
SUBMISSION_JS = JS / "pages" / "submission.js"
MAIN_JS = JS / "main.js"
NOTIFY_JS = JS / "components" / "plazaNotifications.js"
ZH = WEB / "locales" / "zh-CN.json"
EN = WEB / "locales" / "en-US.json"

CHANGED_JS = [PLAZA_JS, ADMIN_JS, SUBMISSION_JS, MAIN_JS, NOTIFY_JS]
NODE = shutil.which("node")

NEW_LOCALE_PREFIXES = (
    "plaza.ops.",
    "plaza.notify.",
    "plaza.favorite.",
    "plaza.report.",
    "plaza.appeal.",
    "plaza.quality.",
    "plaza.metrics.",
)


def read(path: Path) -> str:
    assert path.is_file(), f"缺少文件：{path.relative_to(ROOT)}"
    return path.read_text(encoding="utf-8")


def compact(source: str) -> str:
    return re.sub(r"\s+", " ", source)


def body_between(source: str, start: str, end: str) -> str:
    begin = source.find(start)
    assert begin >= 0, f"未找到代码块起点：{start}"
    finish = source.find(end, begin + len(start))
    assert finish > begin, f"未找到代码块终点：{end}"
    return source[begin:finish]


def load_locale(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict) and data
    return data


# ---------------------------------------------------------------------------
# 1. 公开详情：收藏/纠错/举报
# ---------------------------------------------------------------------------


def test_plaza_detail_member_actions_contract() -> None:
    source = compact(read(PLAZA_JS))
    assert "state.user ? memberActions(data) : null" in source, "既有登录动作分支标记被破坏"
    assert "guestActions(data)" in source, "匿名应显示引导登录的动作区"
    # 收藏：详情 favorited 优先，缺失时探测收藏列表；POST/DELETE 同一路径。
    assert "function favoritePath(publicId)" in source
    assert "/plaza/questions/${encodeURIComponent(publicId)}/favorite" in source
    assert "method: next ? 'POST' : 'DELETE'" in source
    assert "typeof data.favorited !== 'boolean'" in source
    assert "/plaza/favorites?" in source
    assert "setFavorited(items.some" in source
    assert "data-favorited" in source
    # 纠错/举报：WS21 契约字段。
    assert "/plaza/questions/${encodeURIComponent(publicId)}/issues" in source
    assert "field: fieldSelect.value" in source
    assert "description: text" in source
    assert "/plaza/questions/${encodeURIComponent(publicId)}/reports" in source
    assert "reason: reasonSelect.value" in source
    assert "detail: text" in source
    # 匿名点击引导登录：提示 + 回登录（hash 保持，登录后回跳原公开题）。
    assert "plaza.ops.loginRequired" in source
    assert "go('home')" in source
    # 成功/失败 toast（busy 统一捕获失败）。
    for token in ("plaza.favorite.added", "plaza.favorite.removed",
                  "plaza.ops.issue.submitted", "plaza.report.submitted"):
        assert token in source, f"缺少操作反馈文案：{token}"
    # 动作按钮的稳定标记（验收可定位）。
    for marker in ("'data-plaza-action': 'favorite'", "'data-plaza-action': 'issue'",
                   "'data-plaza-action': 'report'", "data-plaza-member-actions", "data-plaza-guest-actions"):
        assert marker in source, f"缺少动作标记：{marker}"
    assert "fetch(" not in source, "页面不应绕过 core/api.js 的 api() 封装"


def test_plaza_detail_keeps_phase2_contracts() -> None:
    source = read(PLAZA_JS)
    for token in (
        "plaza.detail.communityNotice",
        "plaza.detail.withdrawnNotice",
        "plaza.detail.showAnswer",
        "plazaSourceDeclaration(data.source_declaration)",
        "figure_notice",
        "state.user ? memberActions(data) : null",
    ):
        assert token in source, f"Phase 2 公开详情契约丢失：{token}"


# ---------------------------------------------------------------------------
# 2. 通知中心与主壳挂载
# ---------------------------------------------------------------------------


def test_notification_component_contract() -> None:
    source = read(NOTIFY_JS)
    assert "export function mountPlazaNotifications(slot)" in source
    assert "export function openPlazaNotificationPanel(options = {})" in source
    assert "export function notificationTargetOf(item)" in source
    assert "export function unreadLabel(count)" in source
    # 契约路径与参数
    assert "NOTIFICATIONS_PATH = '/plaza/notifications'" in source
    assert "NOTIFICATIONS_READ_PATH = '/plaza/notifications/read'" in source
    assert "unread_only: 1" in source
    assert "method: 'POST'" in source
    assert "readNotifications({ all: true })" in source
    assert "readNotifications({ ids: [id] })" in source
    # 未读数与已读回写
    assert "unreadOf(data)" in source
    assert "broadcastUnread" in source
    assert "unreadSyncs" in source
    # link 直接赋给 location.hash（WS21 契约）
    assert "direct.startsWith('#/')" in source
    assert "location.hash = target" in source
    # 能力探测：未就绪隐藏且不崩
    assert "data-unavailable" in source
    assert "wrap.classList.add('hide')" in source
    # 生命周期清理：轮询 + locale 监听
    assert "window.setInterval" in source
    assert "window.clearInterval" in source
    assert "zhixu:locale-changed" in source
    assert "fetch(" not in source


def test_notification_bell_mounted_only_in_logged_in_shell() -> None:
    source = read(MAIN_JS)
    assert "import { mountPlazaNotifications } from './components/plazaNotifications.js';" in source
    assert "let notificationCleanup = null;" in source
    shell_body = body_between(source, "function renderShell()", "function renderGuestShell()")
    assert "notificationSlot" in shell_body
    assert "data-plaza-notify-slot" in shell_body
    assert "mountPlazaNotifications(notificationSlot)" in shell_body
    teardown_body = body_between(source, "function teardownShell()", "onAuthExpired(")
    assert "notificationCleanup" in teardown_body, "登出/401 时必须停止通知轮询"
    # 访客壳不挂载通知组件（访客不显示铃铛）。
    guest_body = body_between(source, "function renderGuestShell()", "async function startGuestSession()")
    assert "mountPlazaNotifications" not in guest_body
    assert "plaza-notify" not in guest_body
    # 既有壳层行为不被破坏：语言/主题切换、访客入口、登录回跳仍存在。
    for token in ("mountLanguageToggle()", "mountThemeToggle()", "data-guest-login",
                  "backToGuestPlaza", "isPublicRoute(route)"):
        assert token in source, f"壳层既有行为被破坏：{token}"


# ---------------------------------------------------------------------------
# 3. 编辑台：抽检 / 反馈队列 / 指标 / 回滚
# ---------------------------------------------------------------------------


def test_admin_quality_section_contract() -> None:
    source = compact(read(ADMIN_JS))
    assert "capabilities?.quality" in source
    assert "plaza.admin.tab.quality" in source
    assert "/plaza/admin/quality/queue?" in source
    assert "status: statusFilter" in source
    assert "/plaza/admin/quality/checks" in source
    assert "decision: 'pass'" in source
    assert "decision: 'issue'" in source
    assert "severity: severitySelect.value" in source
    assert "findings," in source
    # high 自动撤回提示（服务端 auto_withdrawn / withdraw 响应）。
    assert "auto_withdrawn" in source
    assert "plaza.quality.highWarning" in source
    assert "plaza.quality.highWithdrawn" in source
    # 待抽检/已抽检（status 筛选）与覆盖摘要。
    for token in ("plaza.quality.filter.pending", "plaza.quality.filter.passed",
                  "plaza.quality.summaryPending", "plaza.quality.summaryCoverage",
                  "coverage_rate"):
        assert token in source, f"抽检分区缺少：{token}"
    # 未就绪占位，不崩页。
    assert "data-quality-not-ready" in source


def test_admin_feedback_queues_contract() -> None:
    source = compact(read(ADMIN_JS))
    assert "capabilities?.ops" in source
    assert "plaza.admin.tab.ops" in source
    for kind in ("issues", "reports", "appeals"):
        assert f"/plaza/admin/{kind}" in source, f"缺少 {kind} 队列接口"
    assert "'data-ops-section': kind" in source
    # 处理：PATCH /plaza/admin/{kind}/{id} {action, reason}
    assert "method: 'PATCH'" in source
    assert "{ action, reason }" in source
    assert "action: 'resolve'" not in source  # 来自表单值，不硬编码
    assert "plaza.ops.action.resolve" in source
    assert "plaza.ops.action.reject" in source
    assert "plaza.ops.reasonRequired" in source
    assert "data-ops-not-ready" in source


def test_admin_metrics_and_rollback_contract() -> None:
    source = compact(read(ADMIN_JS))
    assert "capabilities?.metrics" in source
    assert "plaza.admin.tab.metrics" in source
    assert "/plaza/admin/metrics" in source
    for token in ("coverage_rate", "median_hours", "issue_rate", "by_subject",
                  "by_type", "caliber", "pending_checks", "unresolved"):
        assert token in source, f"指标分区缺少字段：{token}"
    for label in ("plaza.metrics.scorableRate", "plaza.metrics.figureRate",
                  "plaza.metrics.reviewMedian", "plaza.metrics.qualityIssueRate"):
        assert label in source, f"指标卡片缺少：{label}"
    assert "data-metrics-not-ready" in source
    # 回滚：POST /plaza/submissions/{id}/rollback {reason}，发布历史 active 行按钮。
    assert "/plaza/submissions/${encodeURIComponent(release.submission_id)}/rollback" in source
    assert "plaza.admin.history.rollback" in source
    assert "data-rollback-dialog" in source
    assert "data-rollback-submission" in source
    assert "plaza.admin.history.rollbackDone" in source


def test_admin_keeps_existing_contract() -> None:
    source = read(ADMIN_JS)
    for token in (
        "api('/plaza/me')",
        "/initial-review",
        "/publish",
        "/plaza/releases?",
        "/plaza/admin/submissions",
        "/plaza/admin/identity/queue",
        "/plaza/admin/roles",
        "data-queue-not-ready",
        "params?.get?.('tab')",
        "params?.get?.('id')",
    ):
        assert token in source, f"既有编辑台契约丢失：{token}"
    # 新标签页深链兼容通知 link 的 ?section=。
    assert "params?.get?.('section')" in source


# ---------------------------------------------------------------------------
# 4. 投稿台：申诉 + 通知入口
# ---------------------------------------------------------------------------


def test_submission_appeal_and_notify_entry_contract() -> None:
    source = compact(read(SUBMISSION_JS))
    assert "openPlazaNotificationPanel" in source
    assert "plaza.notify.openCenter" in source
    assert "data-notify-appeal" in source
    # 申诉接口与请求体
    assert "/plaza/submissions/${encodeURIComponent(id)}/appeals" in source
    assert "data: { reason }" in source
    assert "method: 'POST'" in source
    # 仅 rejected/withdrawn/disputed 可申诉
    assert "const APPEALABLE_STATUSES = ['rejected', 'withdrawn', 'disputed']" in source
    for token in ("plaza.appeal.submit", "plaza.appeal.reason", "plaza.appeal.submitted",
                  "plaza.appeal.pendingNotice", "plaza.appeal.status."):
        assert token in source, f"申诉卡缺少：{token}"
    # 事件时间线保留
    assert "eventsCard(events)" in source
    assert "plaza.submission.events.title" in source


# ---------------------------------------------------------------------------
# 5. locale 中英文对齐与键完整性
# ---------------------------------------------------------------------------


def test_new_locale_namespaces_complete_both_languages() -> None:
    zh = load_locale(ZH)
    en = load_locale(EN)
    for prefix in NEW_LOCALE_PREFIXES:
        zh_keys = {key for key in zh if key.startswith(prefix)}
        en_keys = {key for key in en if key.startswith(prefix)}
        assert zh_keys, f"zh-CN 缺少 {prefix} 命名空间"
        assert zh_keys == en_keys, (
            f"{prefix} 中英文键不一致：缺 en={sorted(zh_keys - en_keys)[:5]} "
            f"缺 zh={sorted(en_keys - zh_keys)[:5]}"
        )
    # 页面里所有 literal t('...') 键必须存在于两语言包。
    used: set[str] = set()
    for path in CHANGED_JS:
        used |= set(re.findall(r"\bt\('([^']+)'", read(path)))
    assert used
    assert not (used - set(zh)), f"zh-CN 缺少键：{sorted(used - set(zh))}"
    assert not (used - set(en)), f"en-US 缺少键：{sorted(used - set(en))}"
    # 运行期拼接的枚举键也必须成对存在。
    dynamic = [
        *(f"plaza.ops.issue.field.{value}" for value in
          ("stem", "options", "answer", "explanation", "figure", "source", "other")),
        *(f"plaza.report.reason.{value}" for value in
          ("answer_error", "copyright", "privacy", "content_quality", "spam", "other")),
        *(f"plaza.quality.severity.{value}" for value in ("low", "medium", "high")),
        *(f"plaza.quality.checkStatus.{value}" for value in ("pending", "passed", "issue")),
        *(f"plaza.ops.status.{value}" for value in ("pending", "resolved", "rejected")),
        *(f"plaza.appeal.status.{value}" for value in ("pending", "resolved", "rejected")),
        *(f"plaza.admin.tab.{value}" for value in ("quality", "ops", "metrics")),
    ]
    missing = [key for key in dynamic if key not in zh or key not in en]
    assert not missing, f"动态 locale 键缺失：{missing}"
    # 收藏/通知/抽检等关键键的实际值存在且非占位文案。
    for key in ("plaza.favorite.add", "plaza.favorite.remove", "plaza.notify.bellLabel",
                "plaza.quality.highWithdrawn", "plaza.metrics.qualityIssueRate",
                "plaza.admin.history.rollback"):
        assert zh.get(key) and en.get(key), f"缺少关键键：{key}"
    # 旧「下一轮开放」占位文案已被真实动作说明替换。
    assert "下一轮" not in str(zh["plaza.action.memberHint"])
    assert "next round" not in str(en["plaza.action.memberHint"]).lower()


def test_locale_plaza_namespace_still_symmetric() -> None:
    zh = load_locale(ZH)
    en = load_locale(EN)
    zh_plaza = {key for key in zh if key.startswith("plaza.")}
    en_plaza = {key for key in en if key.startswith("plaza.")}
    assert zh_plaza == en_plaza, f"中英文 plaza 键不一致：{sorted(zh_plaza ^ en_plaza)[:10]}"
    assert len(zh_plaza) >= 700, f"plaza 键过少：{len(zh_plaza)}"


# ---------------------------------------------------------------------------
# 6. ESM 解析 / 语法检查
# ---------------------------------------------------------------------------


@pytest.mark.skipif(NODE is None, reason="本机未安装 Node.js，跳过前端语法检查")
def test_changed_js_imports_resolve() -> None:
    pattern = re.compile(r"from\s+'(\.{1,2}/[^']+)'")
    for path in CHANGED_JS:
        for spec in pattern.findall(read(path)):
            target = (path.parent / spec).resolve()
            assert target.is_file(), f"{path.name} 引用了不存在的模块：{spec}"


@pytest.mark.skipif(NODE is None, reason="本机未安装 Node.js，跳过前端语法检查")
def test_changed_js_syntax_and_esm() -> None:
    esm_check = (
        "const fs=require('fs');"
        "const vm=require('vm');"
        "let bad=false;"
        "for (const p of process.argv.slice(1)) {"
        "  try { new vm.SourceTextModule(fs.readFileSync(p,'utf8'),{identifier:p}); }"
        "  catch (e) { console.error(p+': '+e.message); bad=true; }"
        "}"
        "process.exit(bad?1:0);"
    )
    files = [str(path) for path in CHANGED_JS]
    completed = subprocess.run(
        [NODE, "--experimental-vm-modules", "-e", esm_check, *files],
        capture_output=True, text=True, timeout=120,
    )
    assert completed.returncode == 0, f"ESM 解析失败：\n{completed.stderr}"
    for path in CHANGED_JS:
        completed = subprocess.run(
            [NODE, "--check", str(path)],
            capture_output=True, text=True, timeout=90,
        )
        assert completed.returncode == 0, f"{path.name}\n{completed.stderr}"
