"""WS15 广场公开页与访客壳（Phase 2 前端）静态契约测试。

覆盖：
1. 访客壳入口：未登录时公开路由进入访客壳、非公开路由回登录页、登录后按当前 hash 回跳；
   访客壳不渲染个人导航与个人组件（专注/每日一句/AI 工作台）。
2. 公开路由白名单集中在 core/state.js（PUBLIC_ROUTES），router.renderRoute 只允许访客渲染公开路由。
3. core/api.js 的 401 处理仍回落登录页（会话过期不留在访客壳）。
4. 广场公开页：列表筛选/分页、详情、答案折叠、review_status/notice/withdrawn 分支、公开配图容错。
5. 路由只注册 plaza 一个（submission/review/plaza-admin 由 Lead 后续接线，本任务不得提前注册）。
6. index.html 引入 styles-plaza.css 与 styles-plaza-workbench.css；locale 中英文键齐全且 JSON 合法。
7. 改动的 ESM 文件全部 node --check 通过（无 Node 时跳过）。

本文件只做静态契约（不启动服务、不联网），与 Playwright 冒烟互补。
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

MAIN_JS = WEB / "js" / "main.js"
ROUTER_JS = WEB / "js" / "core" / "router.js"
STATE_JS = WEB / "js" / "core" / "state.js"
API_JS = WEB / "js" / "core" / "api.js"
PLAZA_JS = WEB / "js" / "pages" / "plaza.js"
PLAZA_CARD_JS = WEB / "js" / "components" / "plazaCard.js"
INDEX_HTML = WEB / "index.html"
STYLES_PLAZA = WEB / "styles-plaza.css"
STYLES_PLAZA_WORKBENCH = WEB / "styles-plaza-workbench.css"
LOCALES = WEB / "locales"

CHANGED_JS = [MAIN_JS, ROUTER_JS, STATE_JS, API_JS, PLAZA_JS, PLAZA_CARD_JS]

PLAZA_JS_KEYS = [PLAZA_JS, PLAZA_CARD_JS, MAIN_JS]


def read(path: Path) -> str:
    assert path.is_file(), f"缺少文件：{path.relative_to(ROOT)}"
    return path.read_text(encoding="utf-8")


def compact(source: str) -> str:
    """去掉空白便于跨行断言（不改变字符串字面量之外的关键结构）。"""
    return re.sub(r"\s+", " ", source)


def function_body(source: str, start_marker: str, end_marker: str) -> str:
    start = source.find(start_marker)
    assert start >= 0, f"未找到代码块起点：{start_marker}"
    end = source.find(end_marker, start)
    assert end > start, f"未找到代码块终点：{end_marker}"
    return source[start:end]


# ---------------------------------------------------------------------------
# 1. 公开路由白名单与访客态
# ---------------------------------------------------------------------------

def test_public_routes_declared_in_state():
    source = compact(read(STATE_JS))
    assert "export const PUBLIC_ROUTES = new Set(['plaza']);" in source
    assert "export function isPublicRoute" in source
    # 公开集合只放广场；投稿/评审/编辑台都必须登录。
    public_block = function_body(source, "export const PUBLIC_ROUTES", "export function isPublicRoute")
    for private_route in ("submission", "review", "plaza-admin", "plazaAdmin"):
        assert private_route not in public_block, f"{private_route} 不能被登记为公开路由"


def test_plaza_routes_registered_and_only_plaza_is_public():
    source = compact(read(STATE_JS))
    route_defs = function_body(source, "const ROUTE_DEFS = [", "];" + " const ROUTE_FALLBACK_LABELS")
    assert "['plaza', 'layers', '广场']" in route_defs
    assert "['home', 'home', '学习概览']" in route_defs  # 既有路由顺序/结构保持
    # Phase 2 收口（Lead 接线）：投稿/评审/编辑台已注册为登录后工作台路由。
    for key in ("submission", "review", "plaza-admin"):
        assert f"['{key}'" in route_defs, f"{key} 路由应已注册"
    # 但只有 plaza 是公开路由，访客壳不得渲染工作台页面。
    assert "export const PUBLIC_ROUTES = new Set(['plaza']);" in source
    main_source = compact(read(MAIN_JS))
    for renderer in ("renderPlaza", "renderSubmission", "renderReview", "renderPlazaAdmin"):
        assert renderer in main_source, f"main.js 未注册 {renderer}"
    # 导航分组里登录用户可见广场与工作台入口（工作台按能力过滤）。
    assert "'upload', 'paperlab', 'classroom', 'physics', 'plaza'" in source
    assert "'home', 'practice', 'wrongbook', 'mybank', 'submission'" in source


def test_guest_state_and_session_cleanup():
    source = compact(read(STATE_JS))
    assert "guest: false," in source
    clear_block = function_body(source, "export function clearUserSession()", "for (const fn of [...sessionResetHooks])")
    assert "state.guest = false;" in clear_block


# ---------------------------------------------------------------------------
# 2. router / api 适配
# ---------------------------------------------------------------------------

def test_router_supports_guest_only_for_public_routes():
    source = compact(read(ROUTER_JS))
    assert "import { routes, state, isPublicRoute } from './state.js';" in source
    assert "Boolean(state.user || state.guest)" in source
    assert "if (guest && (!state.guest || !isPublicRoute(parsedRoute))) return;" in source
    assert "if (guest && !isPublicRoute(route)) return;" in source
    assert "(!state.user && !state.guest)" in source


def test_api_401_falls_back_to_login():
    source = compact(read(API_JS))
    assert "response.status === 401 && state.user" in source
    # clearUserSession 会清空 state.guest；401 仍交给 authExpiredHandler 回落登录页。
    assert "clearUserSession();" in source
    assert "authExpiredHandler()" in source


# ---------------------------------------------------------------------------
# 3. 访客壳（main.js）
# ---------------------------------------------------------------------------

def test_guest_shell_markers_and_entries():
    source = read(MAIN_JS)
    body = function_body(source, "function renderGuestShell()", "async function startGuestSession()")
    for marker in (
        "data-guest-shell",
        "data-guest-login",
        "data-guest-register",
        "data-guest-plaza",
        "plaza.guest.login",
        "plaza.guest.register",
        "nav.plaza",
        "mountLanguageToggle",
        "mountThemeToggle",
    ):
        assert marker in body, f"访客壳缺少标记/入口：{marker}"
    # 访客壳复用主内容挂载点，并重新挂构建提示条。
    assert "id: 'main-content'" in body
    assert "remountBuildBanner()" in body


def test_guest_shell_has_no_personal_navigation_or_widgets():
    source = read(MAIN_JS)
    body = function_body(source, "function renderGuestShell()", "async function startGuestSession()")
    for forbidden in (
        "mountFocusWidget",
        "mountDailyQuoteTeaser",
        "maybeShowDailyQuote",
        "focusSlot",
        "sidebar",
        "renderShell",
        "navGroups",
    ):
        assert forbidden not in body, f"访客壳不应渲染个人导航/组件：{forbidden}"
    for personal_route in ("wrongbook", "mybank", "practice"):
        assert f"go('{personal_route}')" not in body


def test_guest_flow_initialize_and_login_return():
    source = compact(read(MAIN_JS))
    # 首屏未登录：公开路由进入访客壳，其余回登录页（hash 不变，登录后回跳）。
    init_block = function_body(source, "(async function initialize()", "})();")
    assert "if (isPublicRoute(route)) await startGuestSession();" in init_block
    assert "else renderAuth(false);" in init_block
    # hashchange：访客在公开路由内继续渲染，非公开路由回登录页。
    hash_block = function_body(source, "window.addEventListener('hashchange'", "window.addEventListener('unhandledrejection'")
    assert "if (!state.guest) return;" in hash_block
    assert "if (isPublicRoute(route))" in hash_block
    assert "renderAuth(false);" in hash_block
    # 登录表单成功后退出访客态，startSession 按当前 hash 渲染（含 #/plaza?id=）。
    login_block = function_body(source, "const response = await api(`/auth/${register ? 'register' : 'login'}`", "} catch (error) {")
    assert "state.guest = false;" in login_block
    # 访客从登录页可返回广场；若 hash 停在非公开路由，先归位到 #/plaza 再进访客壳。
    assert "plaza.guest.backToPlaza" in source
    assert "backToGuestPlaza" in source
    back_block = function_body(source, "function backToGuestPlaza()", "void startGuestSession();")
    assert "isPublicRoute(parseHash().route)" in back_block
    assert "replaceState(null, '', '#/plaza')" in back_block


def test_plaza_renderer_registered_and_imported():
    source = compact(read(MAIN_JS))
    assert "import { renderPlaza } from './pages/plaza.js';" in source
    assert "plaza: renderPlaza," in source


# ---------------------------------------------------------------------------
# 4. 广场公开页
# ---------------------------------------------------------------------------

def test_plaza_page_list_contract():
    source = read(PLAZA_JS)
    assert "export async function renderPlaza(container, version, params)" in source
    assert "`/plaza/questions?" in source
    for param in ("subject: filters.subject", "type: filters.type", "source: filters.source", "keyword: filters.q"):
        assert param in source, f"列表缺少筛选参数：{param}"
    for token in ("page_size: PAGE_SIZE", "common.prevPage", "common.nextPage", "plaza.list.emptyTitle", "common.retry"):
        assert token in source, f"列表缺少分页/空态/重试：{token}"
    # 详情用 params.id 打开（#/plaza?id=public_id）。
    assert "id: String(get('id') || '').trim()" in source
    assert "if (initial.id) return renderPlazaDetail(container, version, initial.id);" in source
    assert "`/plaza/questions/${encodeURIComponent(publicId)}`" in source


def test_plaza_page_detail_branches():
    source = read(PLAZA_JS)
    for token in (
        "data.visible === false",
        "plaza.detail.withdrawnNotice",
        "plaza.detail.communityNotice",
        "plaza.detail.showAnswer",
        "plaza.detail.hideAnswer",
        "plaza.detail.answerUnavailable",
        "plaza.detail.reviewCounts",
        "plazaSourceDeclaration",
        "state.user ? memberActions(data) : null",
    ):
        assert token in source, f"详情缺少分支/文案：{token}"
    # 社区待核验按 review_status 判定（WS14 契约），不能只看 status。
    card_source = read(PLAZA_CARD_JS)
    assert "record.review_status" in card_source
    assert "reviewStatus === 'community'" in compact(card_source)


def test_plaza_page_figures_capability_detection():
    source = read(PLAZA_JS)
    assert "figure_notice" in source
    assert "figure_urls" in source
    # 公开图：优先 payload URL；旧/边界 payload 用契约图片接口能力探测，失败退占位。
    assert "content.images" in source
    assert "/api/plaza/questions/${encodeURIComponent(publicId)}/images/" in source
    assert "plaza.detail.figureUnavailable" in source
    assert "figureLoadFailed" in source
    # 不拼接私有存储路径：只允许站内相对路径与 http(s)。
    assert "isRenderableImageUrl" in source


def test_plaza_card_component_contract():
    source = read(PLAZA_CARD_JS)
    for symbol in (
        "export function plazaCard(",
        "export function plazaStatusMeta(",
        "export function plazaSourceMeta(",
        "export function plazaTypeLabel(",
        "export function plazaSourceDeclaration(",
    ):
        assert symbol in source, f"缺少导出：{symbol}"
    assert "'data-public-id': item.public_id" in source
    assert "plaza.card.hasAnswer" in source
    assert "plaza.card.hasFigure" in source
    # 来源与授权声明卡片由组件渲染（详情页只负责调用）。
    assert "plaza.detail.sourceTitle" in source
    assert "SOURCE_FIELD_DEFS" in source


# ---------------------------------------------------------------------------
# 5. 样式与入口
# ---------------------------------------------------------------------------

def test_styles_linked_in_index_html():
    html = read(INDEX_HTML)
    assert 'href="/static/styles-plaza.css"' in html
    assert 'href="/static/styles-plaza-workbench.css"' in html
    assert html.index("styles-plaza.css") < html.index("styles-plaza-workbench.css")
    assert STYLES_PLAZA.is_file() and STYLES_PLAZA.stat().st_size > 0
    # 占位文件由并行前端任务填充，这里只要求存在（避免 404），不要求内容。
    assert STYLES_PLAZA_WORKBENCH.is_file()


def test_plaza_styles_cover_guest_and_mobile():
    css = read(STYLES_PLAZA)
    for token in (".guest-layout", ".guest-topbar", ".guest-nav-item", ".plaza-page", ".plaza-grid", "@media (max-width: 700px)"):
        assert token in css, f"样式缺少：{token}"
    # 访客壳覆盖主壳的侧栏布局，避免出现空侧栏/横向溢出。
    assert ".guest-layout {" in css and "display: block;" in css


# ---------------------------------------------------------------------------
# 6. locale：plaza.* 中英文键齐全
# ---------------------------------------------------------------------------

def load_locale(name: str) -> dict:
    path = LOCALES / f"{name}.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict) and data
    return data


def test_locale_files_valid_and_plaza_keys_complete():
    zh = load_locale("zh-CN")
    en = load_locale("en-US")
    assert zh.get("nav.plaza") == "广场"
    assert en.get("nav.plaza")
    # 静态扫描 plaza 页面/组件/访客壳里通过 t('...') 与 data-i18n 引用的 plaza.* 键。
    used: set[str] = set()
    for path in PLAZA_JS_KEYS:
        text = path.read_text(encoding="utf-8")
        used.update(re.findall(r"\bt\('([^']+)'", text))
        used.update(re.findall(r"data-i18n(?:-placeholder|-title|-aria-label)?['\"]?\s*:\s*['\"]([^'\"]+)", text))
        used.update(re.findall(r'data-i18n="([^"]+)"', text))
    plaza_keys = sorted(key for key in used if key.startswith("plaza."))
    assert len(plaza_keys) >= 40, f"plaza 语言键过少：{plaza_keys}"
    missing_zh = [key for key in plaza_keys if key not in zh]
    missing_en = [key for key in plaza_keys if key not in en]
    assert not missing_zh, f"zh-CN 缺少 plaza 键：{missing_zh}"
    assert not missing_en, f"en-US 缺少 plaza 键：{missing_en}"
    # 间接引用（SOURCE_FIELD_DEFS 数据键与状态/题型映射）也必须齐全。
    indirect = [
        "plaza.detail.source.url", "plaza.detail.source.author", "plaza.detail.source.license",
        "plaza.detail.source.rights", "plaza.detail.source.attribution",
        "plaza.detail.source.reference", "plaza.detail.source.page",
        "plaza.detail.source.question",
        "plaza.status.communityPending", "plaza.status.verified", "plaza.status.featured",
        "plaza.status.withdrawn", "plaza.status.disputed", "plaza.status.unknown",
        "plaza.source.community", "plaza.source.featured", "plaza.type.unknown",
    ]
    assert not [key for key in indirect if key not in zh or key not in en]
    # 中英文 plaza 命名空间键集合一致（并行任务各自追加后仍应保持成对）。
    zh_plaza = {key for key in zh if key.startswith("plaza.")}
    en_plaza = {key for key in en if key.startswith("plaza.")}
    assert zh_plaza == en_plaza, f"中英文 plaza 键不一致：{sorted(zh_plaza ^ en_plaza)[:10]}"


# ---------------------------------------------------------------------------
# 7. JS 语法检查（node --check）
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("path", CHANGED_JS, ids=[str(p.relative_to(WEB)) for p in CHANGED_JS])
def test_changed_js_syntax(path: Path):
    node = shutil.which("node")
    if not node:
        pytest.skip("本机未安装 Node，跳过 node --check")
    result = subprocess.run(
        [node, "--check", str(path)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, f"node --check 失败：{path.name}\n{result.stderr}"
