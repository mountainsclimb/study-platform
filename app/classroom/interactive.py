"""交互场景（interactive）：真实 HTML 生成、内联库注入、静态安全校验、沙箱响应头与运行时事件上报。

对齐 OpenMAIC recon §2.6 / §2.8 / §4.5.4b / §4.5.6 / §7：

- **生成**：一次结构化模型调用产出 ``{widget, height, html}``，HTML 是自包含单文件；
- **内联库（P7）**：沙箱 iframe 是**不透明源**（``sandbox="allow-scripts"``，无 ``allow-same-origin``）
  且响应 CSP 无 ``unsafe-eval``、无外链，``<script src>``/CDN/ESM 延迟加载都不可靠，因此 3D 场景
  使用的 three.js 必须**同步内联**：模型只写使用 ``THREE.*`` 的逻辑，服务端先对模型 HTML 做静态
  安全校验，再把 ``web/vendor/interactive/three.module.min.js`` 注入到文档最前面（早于用户脚本），
  注入后按 widget 类型重新做体积/安全终检；库里不含 ``eval``/``new Function``，适配无
  ``unsafe-eval`` 的 CSP。库片段以 ``data-zhixu-lib="three@版本"`` 标记，终检/导入时按 sha256
  逐字节校验后豁免（伪造片段不豁免）；
- **静态校验**：入库前拒绝外网 URL/``<script src>``/``fetch``/``XMLHttpRequest``/``eval``/
  ``new Function``/父窗口访问/凭据访问/iframe/object/form 等，并按 widget 限制体积
  （普通 600KB，``visualization3d`` 含内联库上限 1.5MB）；校验失败由 ``call_structured`` 自动带
  纠错提示重试一次，仍失败则场景落 ``failed`` 并保存原因（不伪造可用场景）；
- **沙箱**：``GET /api/classroom/scenes/{scene_id}/html`` 由本模块统一提供 CSP 与
  ``X-Frame-Options: SAMEORIGIN``（见 ``INTERACTIVE_CSP``），父页面 iframe 只给
  ``sandbox="allow-scripts"``，**绝不**给 ``allow-same-origin``；因此页面是 null origin，
  它只能通过 ``window.parent.postMessage`` 上报；
- **事件上报**：``POST /api/classroom/scenes/{scene_id}/interactive-event`` 接收
  ``error`` / ``ready`` / ``state`` 白名单事件；``state`` 写入会话 ``progress``（``interactive``
  字段）供课堂追问（tutor）使用，大小有上限。
"""
from __future__ import annotations

import hashlib
import html as html_lib
import re
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ..auth import current_user
from . import db as cdb
from . import schema

router = APIRouter()

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

#: 交互场景 HTML 响应专用 CSP（由 ``app/classroom/api.py`` 的沙箱路由下发）。
#: ``unsafe-eval`` 刻意不给：生成侧已禁止 eval/new Function，内联 three 也不含 eval；
#: 其余指令全部收紧到 ``'none'``，页面无法发起任何网络请求。
INTERACTIVE_CSP = (
    "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
    "img-src data: blob:; font-src data:; media-src data: blob:; "
    "connect-src 'none'; frame-src 'none'; worker-src 'none'; object-src 'none'; "
    "base-uri 'none'; form-action 'none'"
)

#: 生成侧 HTML 体积上限（UTF-8 字节）：普通交互 600KB；3D 含服务端内联 three 后放宽到 1.5MB。
#: 入库/路由沿用 ``schema.MAX_HTML_BYTES``（2MB），两个上限都低于它，DB/素材仍可正常存储。
MAX_INTERACTIVE_HTML_BYTES = schema.MAX_INTERACTIVE_HTML_BYTES
MAX_INTERACTIVE3D_HTML_BYTES = schema.MAX_INTERACTIVE3D_HTML_BYTES

#: 父页面 iframe 必须使用的 sandbox 属性（供前端/导出实现引用；不得包含 allow-same-origin）。
IFRAME_SANDBOX = "allow-scripts"

EVENT_TYPES = ("error", "ready", "state")
MAX_EVENT_MESSAGE_CHARS = 2000
MAX_EVENT_SUMMARY_CHARS = 500
MAX_EVENT_STATE_BYTES = 8 * 1024
MAX_STORED_EVENTS = 12
MAX_STORED_PROGRESS_BYTES = 16 * 1024
MAX_INTERACTIVE_HEIGHT = 1200

_GENERATION_MARKER = "交互场景 HTML 生成器"


# ---------------------------------------------------------------------------
# 内联库（three.js）
# ---------------------------------------------------------------------------
# 设计要点（安全顺序不可调换）：
#   1. 先校验模型 HTML（此时页面里没有任何库源码，危险 API 一视同仁地拒绝）；
#   2. 再把「逐字节来自 web/vendor/interactive/ 的库源码」注入到 <head> 之后的第一位；
#   3. 最后对注入后的完整 HTML 再校验一次：库片段按 sha256 校验后豁免，用户 HTML 仍全量扫描。
# 这样既能放行库（three 里可能出现 parent./fetch 等普通标识），又不会给用户 HTML 开任何口子。

#: 仓库内静态库目录（来源/许可/构建说明见 ``web/vendor/interactive/README.md``）。
INLINE_LIBRARY_DIR = Path(__file__).resolve().parents[2] / "web" / "vendor" / "interactive"

#: 可用内联库白名单。版本写死在这里；文件大小/sha256 在加载时按文件自动计算（升级只改版本号）。
INLINE_LIBRARIES: dict[str, dict] = {
    "three": {
        "name": "three",
        "version": "0.186.1",
        "revision": "186",
        "file": "three.module.min.js",
        "license": "MIT",
        #: 允许使用该库的交互类型（只给 3D 用，防止 2D 场景白白多背 ~720KB）。
        "widgets": ("visualization3d",),
        #: 模型 HTML 里的「使用信号」：显式请求注释或 THREE.* 调用，命中即注入。
        "request_re": re.compile(r"(?is)<!--\s*zhixu-lib\s*:\s*three\s*-->"),
        "usage_re": re.compile(r"(?i)\bTHREE\s*\."),
    },
}

_INLINE_LIB_SCRIPT_RE = re.compile(
    r"(?is)<script\b[^>]*\bdata-zhixu-lib\s*=\s*[\"']([^\"']+)[\"'][^>]*>(.*?)</script\s*>"
)
_DATA_LIB_ATTR_RE = re.compile(r"(?is)\bdata-zhixu-lib\s*=")
_THREE_DEFINE_RE = re.compile(r"(?i)(?:\b(?:var|let|const)\s+THREE\s*=|window\s*\.\s*THREE\s*=)")
_HEAD_OPEN_RE = re.compile(r"(?is)<head\b[^>]*>")
_HTML_OPEN_RE = re.compile(r"(?is)<html\b[^>]*>")
_FIRST_SCRIPT_RE = re.compile(r"(?is)<script\b")
_META_CHARSET_RE = re.compile(r"(?is)<meta\b[^>]*\bcharset\b[^>]*>")

#: name -> 已加载库信息（含 text/script，进程内缓存；测试可清空以重新加载）。
_LIBRARY_CACHE: dict[str, dict] = {}


class InteractiveHTMLValidationError(schema.ClassroomSchemaError):
    """交互 HTML 未通过静态安全校验（消息可直接展示给模型/用户）。"""


def _library_error(message: str) -> InteractiveHTMLValidationError:
    return InteractiveHTMLValidationError(message)


def load_inline_library(name: str) -> dict:
    """加载并校验内联库，返回 ``{name, version, revision, license, bytes, sha256, text, script, ...}``。

    只读 ``web/vendor/interactive/`` 下的静态文件；缺失/为空/含 ``</script`` 时抛
    :class:`InteractiveHTMLValidationError`（消息给管理员/模型看，不静默降级）。
    """
    key = str(name or "").strip().lower()
    spec = INLINE_LIBRARIES.get(key)
    if spec is None:
        raise _library_error("未知内联库：%s" % (name or ""))
    cached = _LIBRARY_CACHE.get(key)
    if cached is not None:
        return cached
    path = INLINE_LIBRARY_DIR / spec["file"]
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise _library_error(
            "内联库 %s 不可用（缺少文件 %s）：请确认 web/vendor/interactive/ 已随仓库部署" % (key, path)
        ) from exc
    if not raw:
        raise _library_error("内联库 %s 文件为空：%s" % (key, path))
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise _library_error("内联库 %s 不是 UTF-8 文本：%s" % (key, path)) from None
    lowered = text.lower()
    if "</script" in lowered or "<!--" in lowered:
        raise _library_error("内联库 %s 含无法安全内联的序列（</script 或 <!--）" % key)
    info = {
        "name": key,
        "version": spec["version"],
        "revision": spec["revision"],
        "license": spec["license"],
        "widgets": tuple(spec["widgets"]),
        "file": spec["file"],
        "bytes": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "token": "%s@%s" % (key, spec["version"]),
        "text": text,
    }
    info["script"] = '<script data-zhixu-lib="%s">%s</script>' % (info["token"], text)
    _LIBRARY_CACHE[key] = info
    return info


def inline_library_meta(name: str) -> dict:
    """内联库的公开元数据（不含库源码，可安全写入 content/meta）。"""
    info = load_inline_library(name)
    return {
        "name": info["name"],
        "version": info["version"],
        "revision": info["revision"],
        "license": info["license"],
        "bytes": info["bytes"],
        "sha256": info["sha256"],
    }


def detect_inline_libraries(html: str, widget: str = "") -> list[str]:
    """判断模型 HTML 需要注入哪些内联库（按白名单顺序，结果确定）。"""
    text = str(html or "")
    needed: list[str] = []
    for name, spec in INLINE_LIBRARIES.items():
        if widget and widget not in spec["widgets"]:
            continue
        if spec["request_re"].search(text) or spec["usage_re"].search(text):
            needed.append(name)
    return needed


def strip_inline_libraries(html: str) -> tuple[str, list[dict]]:
    """剔除「服务端注入且 sha256 逐字节一致」的内联库片段。

    返回 ``(剩余用户 HTML, 已注入库元数据列表)``。只豁免与仓库内联库完全一致的片段；
    伪造 ``data-zhixu-lib`` 的片段不会被豁免（会继续走用户代码的静态安全检查）。
    用于入库/导入/终检时重新校验已含库源码的 HTML。
    """
    found: list[dict] = []
    seen: set[str] = set()

    def replace(match: re.Match) -> str:
        token = str(match.group(1) or "").strip()
        name = token.split("@", 1)[0].strip().lower()
        if name not in INLINE_LIBRARIES:
            return match.group(0)
        try:
            info = load_inline_library(name)
        except InteractiveHTMLValidationError:
            return match.group(0)
        body = match.group(2)
        if hashlib.sha256(body.encode("utf-8")).hexdigest() != info["sha256"]:
            return match.group(0)
        if name not in seen:
            seen.add(name)
            found.append(inline_library_meta(name))
        return "<!--zhixu-inline-library:%s-->" % token

    return _INLINE_LIB_SCRIPT_RE.sub(replace, str(html or "")), found


def _insert_after_document_head(html: str, snippet: str) -> str:
    """把同步脚本插到文档最前部、且位于用户脚本之前。

    优先插在 ``<meta charset>`` 之后（浏览器只在文档前 1024 字节内识别 charset，
    库源码很大，插在 charset 之前会让离线/srcdoc 打开时编码嗅探失效）；
    其余回退顺序：``<head>`` 之后 → ``<html>`` 之后 → 第一个 ``<script>`` 之前。
    """
    charset = _META_CHARSET_RE.search(html)
    first_script = _FIRST_SCRIPT_RE.search(html)
    if charset and (first_script is None or charset.start() < first_script.start()):
        return html[:charset.end()] + snippet + html[charset.end():]
    head = _HEAD_OPEN_RE.search(html)
    if head:
        return html[:head.end()] + snippet + html[head.end():]
    html_tag = _HTML_OPEN_RE.search(html)
    if html_tag:
        return html[:html_tag.end()] + snippet + html[html_tag.end():]
    if first_script:
        return html[:first_script.start()] + snippet + html[first_script.start():]
    return snippet + html


def inject_inline_libraries(html: str, widget: str = "") -> tuple[str, list[dict]]:
    """把模型 HTML 需要的库源码注入到文档最前部，返回 ``(注入后 HTML, 已注入库元数据)``。

    - 同步经典脚本（IIFE）在解析阶段执行，早于模型写在页面里的任何 ``<script>``；
    - 幂等：HTML 里已存在且 sha256 一致的库不会重复注入（导入/重新校验时安全）；
    - ``widget`` 决定允许注入哪些库（目前只有 ``visualization3d`` 允许 three）。
    """
    text = str(html or "")
    _user_html, existing = strip_inline_libraries(text)
    existing_names = {item["name"] for item in existing}
    needed = detect_inline_libraries(text, widget)
    for name in needed:
        if name in existing_names:
            continue
        info = load_inline_library(name)
        text = _insert_after_document_head(text, info["script"])
        existing_names.add(name)
    libraries = [inline_library_meta(name) for name in INLINE_LIBRARIES if name in existing_names]
    return text, libraries


_WIDGET_GUIDES = {
    "simulation": (
        "模拟实验：用原生 Canvas 或 SVG 做实时可视化；提供滑杆/按钮等控件调节变量，"
        "参数变化时按真实学科公式重算并重绘，显示当前读数与目标/预设；避免用静态图片冒充模拟。"
    ),
    "mindmap": (
        "思维导图：用 SVG 画节点与连线（所有节点必须相连、无孤立节点），节点文字短小；"
        "点击节点展开细节或按 revealOrder 逐步揭示；节点位置与动画分离（不要把 CSS transform "
        "加到已带 SVG transform 的定位元素上）；高对比配色。"
    ),
    "game": (
        "互动游戏：必须是真实玩法（计时、拖拽、平衡、匹配、瞄准等），不要做成选择题；"
        "包含开始/重置/计分与明确目标，玩法要与本场景教学要点绑定，结束后给出小结；"
        "用原生 Canvas 2D/SVG/CSS 实现（本类型不注入第三方库）。"
    ),
    "code": (
        "在线编程子集：做代码阅读/填空/纠错/追踪变量练习；**禁止执行任意代码**（沙箱 CSP 无 "
        "unsafe-eval，服务端也禁止 eval/new Function，且不注入任何解释器）。"
        "用代码块 + 填空/选择控件 + 预期输出 + 提示实现，判定用字符串/规则比较。"
    ),
    "visualization3d": (
        "3D 可视化：**必须使用服务端已内联的 three.js（全局 THREE）做真实 WebGL 3D**，"
        "不要用 2D 假透视冒充。用 Scene + PerspectiveCamera + WebGLRenderer + 灯光 + 几何体/材质"
        "搭建与教学要点相关的三维模型（如分子/几何体/天体/受力示意）；支持鼠标/触摸拖拽旋转、"
        "滚轮或按钮缩放、重置视角；显示坐标轴或中文标签；必须有 requestAnimationFrame 渲染循环、"
        "窗口 resize 自适应、初始与交互后的 state 上报；WebGL 不可用时用 try/catch 显示中文提示，"
        "不要抛未捕获异常。"
    ),
}


class InteractiveHTMLValidationError(schema.ClassroomSchemaError):
    """交互 HTML 未通过静态安全校验（消息可直接展示给模型/用户）。"""


# ---------------------------------------------------------------------------
# 静态安全校验
# ---------------------------------------------------------------------------

#: SVG/XML 命名空间是标识符而不是网络请求，允许出现。
_ALLOWED_NAMESPACE_URIS = (
    "http://www.w3.org/2000/svg",
    "http://www.w3.org/1999/xlink",
    "http://www.w3.org/1999/xhtml",
    "http://www.w3.org/1998/Math/MathML",
    "http://www.w3.org/XML/1998/namespace",
    "https://www.w3.org/2000/svg",
)

_HTTP_URL_RE = re.compile(r"(?i)\bhttps?://")
_PROTOCOL_RELATIVE_RE = re.compile(
    r"""(?i)(?:\b(?:src|href|action|poster|data|formaction|srcset|xlink:href)\s*=\s*["']?\s*)//"""
)
_CSS_URL_RE = re.compile(r"""(?i)\burl\s*\(\s*["']?\s*(?:https?:)?//""")
_CSS_IMPORT_RE = re.compile(r"""(?i)@import\s+(?:url\()?\s*["']?\s*(?:https?:)?//""")
_SCRIPT_SRC_RE = re.compile(r"""(?i)<script\b[^>]*\bsrc\s*=""")
_LINK_TAG_RE = re.compile(r"""(?i)<\s*link\b""")
_DANGEROUS_TAG_RE = re.compile(
    r"""(?i)<\s*(?:iframe|frame|frameset|object|embed|applet|base|form|portal)\b"""
)
_META_REFRESH_RE = re.compile(r"""(?i)<\s*meta\b[^>]*http-equiv\s*=\s*["']?refresh""")
_JS_SCHEME_RE = re.compile(r"""(?i)\b(?:javascript|vbscript)\s*:""")
_DATA_HTML_RE = re.compile(r"""(?i)\bdata\s*:\s*text/html""")

#: 危险脚本 API（(正则, 中文原因)）。命中任意一条即拒绝。
_FORBIDDEN_APIS = (
    (re.compile(r"(?i)\bfetch\s*\("), "禁止 fetch 网络请求"),
    (re.compile(r"(?i)\bXMLHttpRequest\b"), "禁止 XMLHttpRequest"),
    (re.compile(r"(?i)\bWebSocket\b"), "禁止 WebSocket"),
    (re.compile(r"(?i)\bEventSource\b"), "禁止 EventSource"),
    (re.compile(r"(?i)\bsendBeacon\s*\("), "禁止 sendBeacon"),
    (re.compile(r"(?i)\bimportScripts\s*\("), "禁止 importScripts"),
    (re.compile(r"(?i)\beval\s*\("), "禁止 eval"),
    (re.compile(r"(?i)\bnew\s+(?:window\s*\.\s*)?Function\s*\("), "禁止 new Function"),
    (re.compile(r"""(?i)\bset(?:Timeout|Interval)\s*\(\s*["']"""), "禁止把字符串当代码执行"),
    (re.compile(r"(?i)\b(?:localStorage|sessionStorage|indexedDB)\b"), "禁止读写浏览器凭据/持久存储"),
    (re.compile(r"(?i)\bdocument\s*\.\s*cookie\b"), "禁止读写 Cookie"),
    # 只允许 parent/top 的 postMessage；读取 parent.document / parent.location 等全部拒绝。
    (re.compile(
        r"(?i)(?:\bwindow\s*\.\s*)?\b(?:parent|top)\s*(?:\.\s*(?!postMessage\b)[A-Za-z_$]|\[)"
    ), "禁止访问父窗口（仅允许 parent.postMessage 事件上报）"),
    (re.compile(r"(?i)\b(?:parent|top)\s*="), "禁止改写父窗口引用"),
    (re.compile(r"(?i)\bopener\b"), "禁止访问 window.opener"),
    (re.compile(r"(?i)\bwindow\s*\.\s*open\s*\("), "禁止 window.open"),
    (re.compile(r"(?i)\blocation\s*\.\s*(?:href|assign|replace|reload)\b"), "禁止页面跳转"),
    (re.compile(r"(?i)\bhistory\s*\.\s*(?:pushState|replaceState|go)\b"), "禁止改写浏览历史"),
)


def _strip_namespaces(text: str) -> str:
    """去掉 SVG/XML 命名空间标识，避免把 ``xmlns="http://www.w3.org/..."`` 误判为外链。"""
    for uri in _ALLOWED_NAMESPACE_URIS:
        text = text.replace(uri, "")
    return text


def _wrap_html_fragment(fragment: str, title: str) -> str:
    safe_title = html_lib.escape((title or "交互场景").strip()[:120] or "交互场景")
    return (
        "<!DOCTYPE html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        "<title>%s</title><style>html,body{margin:0;padding:0;font-family:system-ui,sans-serif;}"
        "#experiment{box-sizing:border-box;padding:16px;}</style></head>"
        "<body><div id=\"experiment\">%s</div></body></html>"
    ) % (safe_title, fragment)


def ensure_complete_document(html: str, title: str = "") -> str:
    """补全缺失的 HTML 外壳（模型偶尔只返回 body 片段），返回完整文档字符串。"""
    text = str(html or "").strip()
    lowered = text.lower()
    if "<html" in lowered:
        if not lowered.startswith("<!doctype"):
            text = "<!DOCTYPE html>" + text
        return text
    return _wrap_html_fragment(text, title)


def static_check_interactive_html(html: str, *, max_bytes: int | None = None,
                                  widget: str | None = None) -> list[str]:
    """静态安全校验，返回问题列表（空列表 = 通过）。

    只做「可判定的硬性约束」：外链、危险 API、父窗口访问、凭据访问、危险标签、
    运行时上报脚本缺失、体积超限、内联库与交互类型是否匹配。不做 JS 语法解析
    （Python 无内置 JS parser），语法/运行期错误由 iframe 的 ``error`` 上报兜底。

    ``widget``：已知交互类型时按类型取体积上限（3D 含内联库 1.5MB，其余 600KB）
    并校验「内联库只能用于 3D」；未知时不限制类型相关规则。
    ``max_bytes``：显式覆盖体积上限（生成流程第一步用它把「模型原始 HTML」限制在 600KB）。

    已由服务端注入、且 sha256 与仓库内联库逐字节一致的 ``data-zhixu-lib`` 片段会先被剔除
    （见 :func:`strip_inline_libraries`），因此「入库/导入/终检」时可以对完整 HTML 重复校验；
    用户自己伪造的库片段不会被豁免。
    """
    problems: list[str] = []
    text = str(html or "")
    if not text.strip():
        return ["交互 HTML 为空"]
    user_html, inline_libraries = strip_inline_libraries(text)
    if max_bytes is not None:
        limit = int(max_bytes)
    elif widget:
        limit = schema.interactive_html_limit(widget)
    else:
        limit = MAX_INTERACTIVE_HTML_BYTES
    size = len(text.encode("utf-8", "ignore"))
    if size > limit:
        problems.append("交互 HTML 体积 %d 字节，超过 %d 字节上限" % (size, limit))

    stripped = _strip_namespaces(user_html)

    if _HTTP_URL_RE.search(stripped):
        problems.append("禁止引用外网 URL（http/https）")
    if _PROTOCOL_RELATIVE_RE.search(stripped):
        problems.append("禁止引用协议相对的站外资源（// 开头）")
    if _CSS_URL_RE.search(stripped) or _CSS_IMPORT_RE.search(stripped):
        problems.append("禁止通过 CSS url()/@import 引用外网资源")
    if _SCRIPT_SRC_RE.search(stripped):
        problems.append("禁止 <script src> 外链脚本")
    if _LINK_TAG_RE.search(stripped):
        problems.append("禁止 <link> 外部样式/资源引用")
    if _DANGEROUS_TAG_RE.search(stripped):
        problems.append("禁止使用 iframe/object/embed/base/form 等可绕过沙箱的标签")
    if _META_REFRESH_RE.search(stripped):
        problems.append("禁止 meta refresh 跳转")
    if _JS_SCHEME_RE.search(stripped):
        problems.append("禁止 javascript:/vbscript: 伪协议")
    if _DATA_HTML_RE.search(stripped):
        problems.append("禁止 data:text/html 嵌套文档")

    for pattern, reason in _FORBIDDEN_APIS:
        if pattern.search(stripped):
            problems.append(reason)

    # 内联库：只允许服务端注入（按 sha256 豁免）；用户 HTML 不得自行声明/定义。
    injected_names = {item["name"] for item in inline_libraries}
    for name in injected_names:
        allowed_widgets = tuple(INLINE_LIBRARIES.get(name, {}).get("widgets") or ())
        if widget and widget not in allowed_widgets:
            problems.append("内联库 %s 只允许用于 %s 交互类型" % (name, "/".join(allowed_widgets) or "3D"))
    if _DATA_LIB_ATTR_RE.search(user_html):
        problems.append("禁止自行声明 data-zhixu-lib 内联库（库由服务端注入，不要粘贴/引用）")
    if widget == "visualization3d" and _THREE_DEFINE_RE.search(user_html):
        problems.append("禁止在页面内自行定义 THREE（服务端已注入 three.js）")
    for name, spec in INLINE_LIBRARIES.items():
        if widget and widget not in spec["widgets"]:
            if spec["request_re"].search(user_html) or spec["usage_re"].search(user_html):
                problems.append("%s 内联库只允许用于 visualization3d 交互类型" % name)

    lowered = stripped.lower()
    if "< script" in lowered:  # 只是防御性写法，避免敏感词被拆开绕过
        problems.append("交互 HTML 中检测到异常脚本标签")
    if "<script" not in lowered:
        problems.append("交互场景缺少内联脚本，无法交互")
    if "postmessage" not in lowered:
        problems.append("缺少 postMessage 运行时上报")
    if "zhixureport" not in lowered:
        problems.append("缺少 zhixuReport 运行时事件上报脚本")
    if "zhixu-interactive" not in lowered and "zhixu—interactive" not in lowered:
        problems.append("缺少运行时事件来源标识 zhixu-interactive")
    return problems


def assert_safe_interactive_html(html: str, *, max_bytes: int | None = None,
                                 widget: str | None = None) -> None:
    """不通过则抛 :class:`InteractiveHTMLValidationError`（消息可直接驱动模型重试）。"""
    problems = static_check_interactive_html(html, max_bytes=max_bytes, widget=widget)
    if problems:
        raise InteractiveHTMLValidationError(
            "交互 HTML 未通过安全校验：" + "；".join(problems[:4])
        )


def _extract_html(raw) -> str:
    if isinstance(raw, str):
        return raw
    if isinstance(raw, dict):
        for key in ("html", "document", "content", "page"):
            value = raw.get(key)
            if isinstance(value, str) and value.strip():
                return value
    return ""


def normalize_generated_interactive(raw, *, title: str = "", widget: str = "simulation") -> tuple[dict, list[str]]:
    """校验并归一化模型产出的交互场景内容，返回 ``(content, warnings)``。

    安全顺序（不可调换）：

    1. 先对**模型原始 HTML**（尚未注入任何库）做静态校验，危险 API 一视同仁地拒绝；
    2. 再按 HTML 里的 ``THREE.*``/``<!-- zhixu-lib: three -->`` 信号，把仓库内联库源码
       注入到文档最前部（同步经典脚本，早于模型脚本执行）；
    3. 最后对**注入后的完整 HTML** 再校验一次：库片段按 sha256 豁免，用户部分仍全量扫描，
       体积按 widget 类型限制（3D 1.5MB / 其余 600KB）。

    因此该函数对「模型输出」与「导入的已注入内容」都可用（注入幂等，不会重复注入）。
    """
    warnings: list[str] = []
    payload = schema.parse_json_payload(raw)
    if isinstance(payload, str):
        payload = {"html": payload}
    if not isinstance(payload, dict):
        raise InteractiveHTMLValidationError("交互场景输出必须是 JSON 对象或 HTML 字符串")
    html = _extract_html(payload)
    if not html.strip():
        raise InteractiveHTMLValidationError("交互场景缺少 html 内容")
    html = ensure_complete_document(html, title=title)
    expected_widget = schema.normalize_widget(widget, "simulation")
    output_widget = schema.normalize_widget(payload.get("widget") or payload.get("widgetType"), expected_widget)
    if output_widget != expected_widget:
        warnings.append("交互类型由大纲的 %s 归一化，忽略模型输出的 %s" % (expected_widget, output_widget))
        output_widget = expected_widget
    # 1) 先校验模型原始 HTML（此时页面里没有任何库源码；已注入过的导入内容先剔除信任片段再查）。
    user_html, _existing = strip_inline_libraries(html)
    assert_safe_interactive_html(
        user_html, max_bytes=MAX_INTERACTIVE_HTML_BYTES, widget=expected_widget,
    )
    # 2) 再注入内联库（幂等；只有 visualization3d 会注入 three）。
    html, libraries = inject_inline_libraries(html, expected_widget)
    # 3) 终检：库片段按 sha256 豁免，用户 HTML 仍全量扫描，体积按 widget 上限。
    assert_safe_interactive_html(html, widget=expected_widget)
    raw_height = payload.get("height")
    try:
        height = int(float(raw_height)) if raw_height is not None else 480
    except (TypeError, ValueError):
        height = 480
    height = max(240, min(MAX_INTERACTIVE_HEIGHT, height))
    content = {
        "type": "interactive",
        "widget": output_widget,
        "html": html,
        "height": height,
    }
    if libraries:
        # 供导入/导出/排查使用：记录注入的库与内容指纹（不含库源码）。
        content["libraries"] = libraries
    return content, warnings


# ---------------------------------------------------------------------------
# 生成 prompt（移植 OpenMAIC interactive-* snippets 的协议，中文化并去掉第三方库）
# ---------------------------------------------------------------------------

_REPORTER_SNIPPET = """<script>
(function () {
  function zhixuReport(type, payload) {
    try {
      window.parent.postMessage(
        { source: 'zhixu-interactive', type: type, payload: payload || {} }, '*'
      );
    } catch (error) { /* 上报失败不影响页面交互 */ }
  }
  window.zhixuReport = zhixuReport;
  window.addEventListener('error', function (event) {
    zhixuReport('error', { message: String((event && event.message) || '脚本错误').slice(0, 500) });
  });
  window.addEventListener('unhandledrejection', function (event) {
    zhixuReport('error', { message: String((event && event.reason) || '异步错误').slice(0, 500) });
  });
  window.addEventListener('load', function () { zhixuReport('ready', {}); });
})();
</script>"""


_THREE_API_PROMPT = """- **three.js 已由服务端内联注入**：页面最前面会自动出现
  `<script data-zhixu-lib="three@0.186.1">…</script>`，它先于你的所有脚本执行并暴露全局 `THREE`
  （three.js r186，MIT 许可）。
- **不要**写 `<script src>`、`import`/`require`、CDN 链接；**不要**粘贴 three 源码；**也不要**重新
  定义 `THREE`（`var/let/const THREE =` 或 `window.THREE =` 会被服务端拒绝）。
- 可直接使用的 API（完整 three 导出里都有）：`THREE.Scene` `PerspectiveCamera` `OrthographicCamera`
  `WebGLRenderer` `Color` `Fog` `FogExp2` `AmbientLight` `DirectionalLight` `PointLight`
  `HemisphereLight` `SpotLight` `Mesh` `Group` `InstancedMesh` `Line` `LineSegments` `Points`
  `Sprite` `Object3D` `BoxGeometry` `SphereGeometry` `CylinderGeometry` `ConeGeometry` `PlaneGeometry`
  `CircleGeometry` `RingGeometry` `TorusGeometry` `TorusKnotGeometry` `IcosahedronGeometry`
  `OctahedronGeometry` `ExtrudeGeometry` `ShapeGeometry` `EdgesGeometry` `WireframeGeometry`
  `MeshStandardMaterial` `MeshBasicMaterial` `MeshPhongMaterial` `MeshLambertMaterial`
  `LineBasicMaterial` `PointsMaterial` `SpriteMaterial` `ShaderMaterial` `CanvasTexture` `Texture`
  `Vector2` `Vector3` `Quaternion` `Euler` `Matrix4` `MathUtils` `Clock` `Raycaster` `Layers`
  `AxesHelper` `GridHelper` `ArrowHelper`；常量如 `THREE.DoubleSide`、`THREE.SRGBColorSpace`、
  `THREE.ACESFilmicToneMapping` 也可用。
- 2D 叠加仍可用原生 Canvas 2D/SVG/CSS；不要引用任何外链图片/字体。
- 可选：在 `<head>` 里写一行 `<!-- zhixu-lib: three -->` 显式声明需要 3D 库；不写也会按
  `THREE.` 调用自动识别并注入。"""

_CODE_LIBRARY_PROMPT = """- 本类型**不注入任何第三方库**，也**不执行任意代码**：沙箱 CSP 无 `unsafe-eval`，
  服务端禁止 `eval`/`new Function`，页面里没有 Python/JS 解释器。用代码块 + 填空/选择/纠错/
  变量追踪判定实现练习。
- 不要写 `<script src>`、`import`/`require`、CDN 链接；公式/示意图用文本、Canvas 2D 或 SVG 绘制。"""

_DEFAULT_LIBRARY_PROMPT = """- 本类型**不注入任何第三方库**（没有 THREE / 图表库 / KaTeX）：用原生 JS + Canvas 2D/SVG/CSS 实现。
- 不要写 `<script src>`、`import`/`require`、CDN 链接；公式可用文字/Unicode/Canvas 绘制。"""

_THREE_WIDGET_RULES = """
## 3D 场景专项（visualization3d，必须遵守）
- 必须用内联 three.js 做**真实 WebGL 3D**：`new THREE.WebGLRenderer(...)` → `new THREE.Scene()` →
  `new THREE.PerspectiveCamera(...)` → 灯光/几何体/材质 → `requestAnimationFrame` 渲染循环。
- 画布结构建议：`<div id="stage"><canvas id="scene-canvas"></canvas></div>`，渲染器尺寸随容器变化；
  监听 `resize` 更新 `camera.aspect` 与 `renderer.setSize`。
- 交互：鼠标/触摸拖拽旋转（pointerdown/pointermove/pointerup），滚轮或按钮缩放，重置视角按钮；
  初始渲染后与每次交互后用 `zhixuReport('state', ...)` 上报中文摘要（如当前视角/旋转角度/缩放）。
- 模型必须与本页教学要点直接相关（分子结构/几何体/天体运行/受力示意等），不要做与内容无关的炫技。
- WebGL 不可用时用 `try { renderer = new THREE.WebGLRenderer(...) } catch (err) { ... }`，
  在页面里显示中文提示（这不算未捕获错误）；渲染循环不要抛出异常。
- 严禁用静态图片或 2D 假透视冒充 3D；不要自己再实现一套 3D 投影库。
"""


def _library_prompt_section(widget_key: str) -> str:
    if widget_key == "visualization3d":
        return _THREE_API_PROMPT
    if widget_key == "code":
        return _CODE_LIBRARY_PROMPT
    return _DEFAULT_LIBRARY_PROMPT


def build_interactive_prompt(*, scene_title: str, widget: str = "simulation",
                             outline_points: list[str] | tuple[str, ...] = (),
                             description: str = "", course_title: str = "",
                             position: int = 1, total: int = 1,
                             language_directive: str = "", depth: str = "standard",
                             course_requirements: str = "",
                             pbl_config: dict | None = None) -> str:
    """交互 HTML 生成 prompt：严格 JSON、单文件、无外链、带运行时上报、按类型说明可用内联库。"""
    widget_key = schema.normalize_widget(widget, "simulation")
    widget_label = schema.WIDGET_LABELS.get(widget_key, widget_key)
    guide = _WIDGET_GUIDES.get(widget_key, _WIDGET_GUIDES["simulation"])
    library_section = _library_prompt_section(widget_key)
    widget_rules = ("\n" + _THREE_WIDGET_RULES.strip()) if widget_key == "visualization3d" else ""
    size_label = (
        "≤ 1.5MB（含服务端注入的 three.js；你的 HTML 本身建议 < 200KB）"
        if widget_key == "visualization3d" else "≤ 600KB"
    )
    widget_check = (
        "\n- （visualization3d）是否真的用了 `THREE.WebGLRenderer` 渲染 3D、有渲染循环与拖拽/缩放/重置？\n"
        "  是否**没有**自己粘贴 three 源码、没有重新定义 THREE、没有 `<script src>`？"
        if widget_key == "visualization3d" else ""
    )
    points = [str(point).strip() for point in (outline_points or []) if str(point).strip()][:6]
    points_text = "；".join(points) or "（无，请按场景标题设计）"
    depth_rule = {
        "brief": "内容精简：1-2 个可调变量 + 一个清晰的可视化/读数即可。",
        "standard": "内容丰富度适中：2-4 个可调变量、实时可视化、关键读数与简短引导文字。",
        "deep": "内容较深入：3-5 个变量、多状态/多步骤、数据读数与结论提示，但仍要单文件、可直接运行。",
    }.get(str(depth or "standard"), "内容丰富度适中。")
    config_line = ""
    if isinstance(pbl_config, dict) and pbl_config:
        topic = str(pbl_config.get("project_topic") or "").strip()
        skills = [str(item).strip() for item in (pbl_config.get("target_skills") or []) if str(item).strip()]
        if topic or skills:
            config_line = "- 大纲补充：项目主题=%s；目标技能=%s\n" % (
                topic or "未指定", "、".join(skills) or "未指定")
    return f"""# 交互场景 HTML 生成器

你是一名资深前端工程师兼学科老师，负责为一个「AI 课堂」交互场景生成**可直接在沙箱 iframe 中运行的单文件 HTML**。

## 本场景信息
- 课程：{str(course_title or "").strip()[:120] or "未命名课程"}
- 场景标题：{str(scene_title or "").strip()[:120] or "交互场景"}
- 交互类型：{widget_key}（{widget_label}）
- 场景要点：{points_text}
- 场景简介：{str(description or "").strip()[:600] or "（无）"}
- 本课位置：第 {position} / {total} 个场景
- 授课语言：{str(language_directive or "").strip()[:300] or "使用简体中文；术语首次出现时给出中文解释"}
- 深度要求：{depth_rule}
{config_line}- 教师补充要求：{str(course_requirements or "").strip()[:1500] or "无"}

## 本类型的具体要求
{guide}

## 可用内联库与 API 清单
{library_section}
{widget_rules}

## 输出格式（必须严格遵守）
只输出一个 JSON 对象（首字符 `{{`、末字符 `}}`），不要 Markdown 代码块、不要任何解释文字：
{{
  "widget": "{widget_key}",
  "height": 480,
  "html": "<!DOCTYPE html><html lang=\\"zh-CN\\">……完整单文件 HTML……</html>"
}}

`html` 是一个 JSON 字符串：内部换行必须转义为 `\\n`，双引号必须转义为 `\\"`，保证 `json.loads` 能直接解析。

## HTML 硬性约束（违反任意一条都会被服务端拒绝并触发重写）
1. 完整 HTML5 文档：`<!DOCTYPE html>` 开头，包含 `<html><head>…</head><body>…</body></html>`；CSS/JS 全部内联。
2. **完全离线**：禁止任何第三方库/CDN/外链字体/外链图片/外链脚本；不得出现 `http://` 或 `https://` 地址
   （SVG 的 `xmlns` 命名空间除外）；禁止 `<script src>`、`<link>`、`@import`、`url(http...)`。
   图片只能内联为 SVG 或 `data:` URI。visualization3d 的 three.js 由**服务端注入**，你只写使用代码。
3. 禁止任何网络请求：`fetch`、`XMLHttpRequest`、`WebSocket`、`EventSource`、`sendBeacon`、`importScripts`。
4. 禁止动态执行代码：`eval`、`new Function`、把字符串传给 `setTimeout/setInterval`。
5. 禁止访问父窗口：不得读取 `parent`/`top` 的 `document`、`location`、`frames` 等；
   **唯一允许**的父窗口调用是 `window.parent.postMessage({...}, '*')` 事件上报。
6. 禁止读写浏览器存储/凭据：`localStorage`、`sessionStorage`、`document.cookie`、`indexedDB`。
7. 禁止 `<iframe>`/`<object>`/`<embed>`/`<base>`/`<form>`、`meta refresh`、`javascript:` 链接、
   `window.open`、`location.href` 跳转。
8. 体积 {size_label}（UTF-8）；移动端可用（`<meta name="viewport">`，390px 宽无横向滚动，控件触控区域 ≥ 40px）。
9. 必须用**原生 JS/CSS/SVG/Canvas**（3D 场景用已注入的 three.js）实现真实交互：
   可调参数/拖拽 → 画面/数据实时变化；符合上方教学要点；面向学生的文字全部使用简体中文；
   不要伪造与教学内容无关的画面。
10. 用 `<div id="experiment">` 包裹整个交互区；页面可见区域适配 iframe 宽度。
11. **运行时事件上报（必须）**：把下面的脚本原样放在 HTML 末尾（`source` 的值 `zhixu-interactive`
    不可修改）；在初始渲染后以及每次有意义的交互状态变化后，调用
    `window.zhixuReport('state', {{ summary: '一句学生能读懂的状态描述', state: {{...}} }})`。
    不要用 fetch 上报：父页面会监听 postMessage 并转发给课堂。

```html
{_REPORTER_SNIPPET}
```

## 质量自检（输出前逐条检查）
- `json.loads` 能否直接解析整个输出？`html` 字符串里的换行/引号都转义了吗？
- 页面里是否还有任何外链、fetch/XHR、eval/new Function、父窗口 DOM 访问、localStorage？
- 交互是否真实可用（不只是静态文字）？控件与画面是否联动？读数是否符合学科公式？
- 初始状态是否已经调用过一次 `zhixuReport('state', ...)`？{widget_check}
"""


# ---------------------------------------------------------------------------
# 课堂追问使用的运行时状态摘要
# ---------------------------------------------------------------------------

def tutor_state_digest(progress, scene_id: int) -> str:
    """把会话 progress 里的交互状态整理成给 tutor prompt 的中文摘要；没有则返回空串。"""
    if not isinstance(progress, dict):
        return ""
    block = progress.get("interactive")
    if not isinstance(block, dict):
        return ""
    try:
        if int(block.get("scene_id") or 0) != int(scene_id):
            return ""
    except (TypeError, ValueError):
        return ""
    summary = str(block.get("summary") or "").strip()[:500]
    state = block.get("state")
    lines = ["学生当前在交互页面的状态（由页面运行时上报，供追问参考）："]
    if summary:
        lines.append("摘要：%s" % summary)
    if isinstance(state, dict) and state:
        try:
            payload = cdb.json_dump(state)
        except (TypeError, ValueError):
            payload = ""
        if payload:
            lines.append("状态：" + payload[:3000])
    return "\n".join(lines) if len(lines) > 1 else ""


# ---------------------------------------------------------------------------
# 运行时事件上报
# ---------------------------------------------------------------------------

class InteractiveEventInput(BaseModel):
    type: str = Field(min_length=1, max_length=20)
    session_id: int | None = None
    message: str | None = Field(default=None, max_length=MAX_EVENT_MESSAGE_CHARS)
    summary: str | None = Field(default=None, max_length=MAX_EVENT_SUMMARY_CHARS)
    state: dict | None = None


def _db():
    from .. import db

    return db


def _event_state_size(state) -> int:
    """校验状态 JSON 的体积（只统计 state 本体，message/summary 有字符数上限）。"""
    if not isinstance(state, dict) or not state:
        return 0
    try:
        raw = cdb.json_dump(state)
    except (TypeError, ValueError):
        return MAX_EVENT_STATE_BYTES + 1
    return len(raw.encode("utf-8"))


def _append_event_log(progress: dict, entry: dict) -> None:
    events = progress.get("interactive_events")
    if not isinstance(events, list):
        events = []
    events.append(entry)
    progress["interactive_events"] = events[-MAX_STORED_EVENTS:]


@router.post("/scenes/{scene_id}/interactive-event")
def report_interactive_event(scene_id: int, data: InteractiveEventInput, user=Depends(current_user)):
    """接收交互页运行时事件：error / ready / state（白名单），state 落会话 progress。"""
    event_type = str(data.type or "").strip().lower()
    if event_type not in EVENT_TYPES:
        raise HTTPException(422, "交互事件类型只能是 error / ready / state")
    scene = cdb.owned_scene(scene_id, user["id"])
    if scene["scene_type"] != "interactive":
        raise HTTPException(422, "该场景不是交互场景")
    content = cdb.json_load(scene.get("content_json"), {})
    if not isinstance(content, dict) or not str(content.get("html") or "").strip():
        raise HTTPException(422, "该交互场景还没有可播放内容")
    if _event_state_size(data.state) > MAX_EVENT_STATE_BYTES:
        raise HTTPException(422, "交互状态数据过大（上限 8KB）")

    now = _db().utcnow()
    stored = False
    session_id = data.session_id
    if session_id is not None:
        session = cdb.owned_session(int(session_id), user["id"])
        if int(session["course_id"]) != int(scene["course_id"]):
            raise HTTPException(404, "未找到该学习会话")
        progress = cdb.json_load(session.get("progress_json"), {})
        progress = dict(progress) if isinstance(progress, dict) else {}
        message = str(data.message or "").strip()[:MAX_EVENT_MESSAGE_CHARS]
        summary = str(data.summary or "").strip()[:MAX_EVENT_SUMMARY_CHARS]
        if event_type == "state":
            state = data.state if isinstance(data.state, dict) else {}
            if not state and not summary:
                raise HTTPException(422, "state 事件需要提供 state 或 summary")
            progress["interactive"] = {
                "scene_id": int(scene["id"]),
                "summary": summary,
                "state": state,
                "updated_at": now,
            }
            _append_event_log(progress, {
                "type": "state", "scene_id": int(scene["id"]),
                "message": summary[:300], "at": now,
            })
        else:
            _append_event_log(progress, {
                "type": event_type, "scene_id": int(scene["id"]),
                "message": message[:300], "at": now,
            })
        payload = cdb.json_dump(progress)
        if len(payload.encode("utf-8")) > MAX_STORED_PROGRESS_BYTES:
            raise HTTPException(422, "学习进度数据过大，无法保存交互状态")
        with _db().connection() as con:
            con.execute(
                "UPDATE cls_sessions SET progress_json=?, updated_at=? WHERE id=? AND user_id=?",
                (payload, now, int(session_id), user["id"]),
            )
        stored = True
    return {
        "ok": True,
        "type": event_type,
        "stored": stored,
        "session_id": int(session_id) if session_id is not None else None,
        "scene_id": int(scene["id"]),
    }


__all__ = [
    "EVENT_TYPES", "IFRAME_SANDBOX", "INLINE_LIBRARIES", "INLINE_LIBRARY_DIR",
    "INTERACTIVE_CSP", "InteractiveHTMLValidationError", "MAX_EVENT_STATE_BYTES",
    "MAX_INTERACTIVE3D_HTML_BYTES", "MAX_INTERACTIVE_HTML_BYTES",
    "assert_safe_interactive_html", "build_interactive_prompt", "detect_inline_libraries",
    "ensure_complete_document", "inject_inline_libraries", "inline_library_meta",
    "load_inline_library", "normalize_generated_interactive", "router",
    "static_check_interactive_html", "strip_inline_libraries", "tutor_state_digest",
]
