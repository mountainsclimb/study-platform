"""AI 课堂 DSL 校验与规范化（对齐 OpenMAIC ``Stage/Scene/Slide/Quiz/Action`` 子集）。

设计原则（契约 §3.1 / recon §3）：

- 幻灯片是 **1000 × 562.5 绝对定位画布**，元素字段名与上游 PPTist 兼容
  （``id/type/left/top/width/height/rotate/content/...``），便于课程 ZIP 互导与 PPTX 映射；
- 对模型输出做宽松归一化：补默认值、越界钳制、丢弃未知元素/动作并记录 warning，
  只有「结构不可用」（缺 elements、题目答案非法、交互 HTML 引用外网等）才抛
  :class:`ClassroomSchemaError`，让执行器用一次带纠错提示的重试兜底；
- 动作只允许播放器白名单，服务端校验后才下发，前端只执行白名单动作。
"""
from __future__ import annotations

import base64
import html as html_lib
import json
import math
import re

# ---------------------------------------------------------------------------
# 业务常量（上限写死；API 层据此返回可操作的中文提示）
# ---------------------------------------------------------------------------

SCENE_TYPES = ("slide", "quiz", "interactive", "pbl")
#: 手工插入的课堂场景类型（不由 AI 生成流程分配；``SCENE_TYPES`` 保持既有 4 类不变）。
MANUAL_SCENE_TYPES = ("demo",)
#: 全部课堂场景类型 = AI 生成类型 + 手工类型（播放器 / 导出 / 导入可按此白名单分派）。
ALL_SCENE_TYPES = SCENE_TYPES + MANUAL_SCENE_TYPES
SCENE_TYPE_LABELS = {
    "slide": "幻灯片",
    "quiz": "课堂测验",
    "interactive": "交互场景",
    "pbl": "项目式学习",
    "demo": "原理演示",
}
WIDGET_TYPES = ("simulation", "mindmap", "game", "code", "visualization3d")
WIDGET_LABELS = {
    "simulation": "模拟实验",
    "mindmap": "思维导图",
    "game": "互动游戏",
    "code": "在线编程",
    "visualization3d": "3D 可视化",
}
MODES = ("standard", "interactive")
DEPTHS = ("brief", "standard", "deep")
COURSE_STATUSES = ("draft", "outline_ready", "generating", "ready", "failed")
SCENE_STATUSES = ("pending", "generating", "ready", "failed")

#: 可插入为「原理演示」场景的学习附件类型（与 ``study_artifact.ARTIFACT_KINDS`` 的演示交集）。
DEMO_ARTIFACT_KINDS = (
    "function_graph",
    "whiteboard",
    "flow_animation",
    "isometric",
    "science_demo",
    "demo_3d",
    # M5：机械 3D 与工厂仿真（app/demo_library.py 模板库产物）。
    "mech_3d",
    "factory_sim",
    # 批次五（§6.1）：JSXGraph 交互几何。
    "geometry_demo",
    # 批次五（§6.2）：3Dmol 分子模型。
    "molecule_3d",
    # 批次五（§6.3）：Matter.js 物理自由沙盒。
    "physics_sandbox",
)
#: 演示附件类型的中文名（PPTX / 离线 HTML / 打印讲义共用）。
DEMO_ARTIFACT_KIND_LABELS = {
    "function_graph": "函数图",
    "whiteboard": "解题白板",
    "flow_animation": "流程动画",
    "isometric": "等距结构",
    "science_demo": "科学演示",
    "demo_3d": "3D 演示",
    "mech_3d": "机械 3D",
    "factory_sim": "工厂仿真",
    "geometry_demo": "几何演示",
    "molecule_3d": "分子模型",
    "physics_sandbox": "物理沙盒",
}
#: 演示附件快照限额（与 ``study_artifact`` 的 256KB / 64KB 存储上限兼容，课堂侧再收窄）。
MAX_DEMO_DATA_BYTES = 200 * 1024
MAX_DEMO_PNG_BYTES = 200 * 1024
MAX_DEMO_SVG_CHARS = 60 * 1024
MAX_DEMO_CONTENT_BYTES = 260 * 1024
MAX_DEMO_SOURCE_NOTE_CHARS = 300
MAX_DEMO_ASSUMPTIONS = 6
MAX_DEMO_ASSUMPTION_CHARS = 200
MAX_DEMO_STEP_COUNT = 50
MAX_DEMO_REVISION = 100_000
#: 课堂演示海报只接受 PNG data URI（SVG 不用于渲染，仅作为附件预览数据随快照保存）。
DEMO_PNG_DATA_PREFIX = "data:image/png;base64,"
#: 演示快照里的公开附件 ID（比 ``study_artifact`` 的既有 ID 规则更宽，兼容导入的第三方 ID）。
DEMO_ARTIFACT_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{6,64}$")
#: 批次五（§6.3）：物理沙盒演示数据的结构上限与形状白名单，与前端
#: ``normalizeSandboxScene`` 对齐。附件端（``study_artifact``）只限字节，
#: 课堂插入固定版本时做结构校验，避免不可重放的场景固化进导出。
SANDBOX_DEMO_SHAPES = ("rect", "circle", "slope")
SANDBOX_DEMO_MAX_BODIES = 60
SANDBOX_DEMO_MAX_CONSTRAINTS = 40
_SANDBOX_BODY_NUMBERS = ("x", "y", "width", "height", "radius", "angle",
                         "restitution", "friction", "vx", "vy")

MIN_SCENE_COUNT = 3
MAX_SCENE_COUNT = 12
DEFAULT_SCENE_COUNT = 6
MAX_ELEMENTS = 24
MAX_ACTIONS = 40
MAX_QUESTIONS = 12
MAX_QUIZ_OPTIONS = 8
MAX_HTML_BYTES = 2_000_000
#: 交互场景 HTML 体积上限（UTF-8 字节，按 widget 区分）：
#: - 普通交互（simulation/mindmap/game/code）600KB，与 P1 基线一致；
#: - ``visualization3d`` 需要服务端内联 three.js（约 720KB），放宽到 1.5MB；
#: 两者都远小于入库 JSON 的 ``MAX_HTML_BYTES``（2MB），DB/素材存储不受影响。
MAX_INTERACTIVE_HTML_BYTES = 600 * 1024
MAX_INTERACTIVE3D_HTML_BYTES = 1_500_000


def interactive_html_limit(widget: str = "") -> int:
    """按交互类型返回 HTML 体积上限（3D 含内联 three，其余 600KB）。"""
    return MAX_INTERACTIVE3D_HTML_BYTES if str(widget or "").strip() == "visualization3d" else MAX_INTERACTIVE_HTML_BYTES


MAX_SLIDE_HTML_CHARS = 2000
MAX_SPEECH_CHARS = 2000
MAX_POINTS = 100
DEFAULT_QUIZ_POINTS = 5.0

CANVAS_WIDTH = 1000.0
CANVAS_HEIGHT = 562.5
WHITEBOARD_WIDTH = 1000.0
WHITEBOARD_HEIGHT = 562.5

ELEMENT_TYPES = ("text", "image", "shape", "line", "latex", "table", "chart", "code")
ELEMENT_TYPE_LABELS = {
    "text": "文本",
    "image": "图片",
    "shape": "形状",
    "line": "线条",
    "latex": "公式",
    "table": "表格",
    "chart": "图表",
    "code": "代码",
}
_ELEMENT_ALIASES = {
    "text": "text", "textbox": "text", "paragraph": "text", "title": "text",
    "image": "image", "img": "image", "picture": "image",
    "shape": "shape", "rect": "shape", "rectangle": "shape", "square": "shape",
    "circle": "shape", "ellipse": "shape", "triangle": "shape",
    "line": "line", "arrow": "line",
    "latex": "latex", "formula": "latex", "math": "latex",
    "table": "table",
    "chart": "chart",
    "code": "code",
}

ACTION_TYPES = (
    "speech", "spotlight", "laser", "highlight",
    "whiteboard", "next_scene", "show_slide", "play_audio",
)
_ACTION_ALIASES = {
    "speech": "speech", "say": "speech", "narration": "speech", "text": "speech",
    "spotlight": "spotlight", "focus": "spotlight",
    "laser": "laser", "laser_pointer": "laser",
    "highlight": "highlight",
    "whiteboard": "whiteboard", "wb": "whiteboard", "white_board": "whiteboard",
    "next_scene": "next_scene", "next": "next_scene",
    "show_slide": "show_slide", "show": "show_slide",
    "play_audio": "play_audio", "audio": "play_audio",
}

_WIDGET_ALIASES = {
    "simulation": "simulation", "sim": "simulation", "模拟": "simulation",
    "mindmap": "mindmap", "diagram": "mindmap", "mind_map": "mindmap",
    "思维导图": "mindmap", "概念图": "mindmap",
    "game": "game", "游戏": "game",
    "code": "code", "coding": "code", "programming": "code", "编程": "code",
    "visualization3d": "visualization3d", "3d": "visualization3d",
    "three_d": "visualization3d", "visualization_3d": "visualization3d", "3d可视化": "visualization3d",
}

_SCENE_TYPE_ALIASES = {
    "slide": "slide", "slides": "slide", "ppt": "slide", "presentation": "slide",
    "幻灯片": "slide", "讲义": "slide", "讲解": "slide",
    "quiz": "quiz", "test": "quiz", "quiz_scene": "quiz", "测验": "quiz",
    "小测": "quiz", "练习": "quiz",
    "interactive": "interactive", "interaction": "interactive", "互动": "interactive",
    "交互": "interactive", "交互场景": "interactive",
    "pbl": "pbl", "project": "pbl", "项目": "pbl", "项目式学习": "pbl",
    # 手工「原理演示」场景（学习附件固定版本）：只由插入接口创建，不参与 AI 生成分配。
    "demo": "demo", "principle": "demo", "演示": "demo", "原理演示": "demo",
}

_QUIZ_TYPE_ALIASES = {
    "single": "single", "single_choice": "single", "single-choice": "single",
    "radio": "single", "单选": "single", "单选题": "single",
    "multiple": "multiple", "multiple_choice": "multiple", "multi": "multiple",
    "checkbox": "multiple", "多选": "multiple", "多选题": "multiple",
    "short_answer": "short_answer", "short": "short_answer", "text": "short_answer",
    "subjective": "short_answer", "essay": "short_answer", "问答": "short_answer",
    "简答": "short_answer", "简答题": "short_answer", "主观题": "short_answer",
}

_QUIZ_STATS = ("single", "multiple", "short_answer")

_DEFAULT_THEME = {
    "backgroundColor": "#ffffff",
    "themeColors": ["#1f6feb", "#e8590c", "#2f9e44", "#7048e8", "#c2255c"],
    "fontColor": "#222222",
    "fontName": "Microsoft YaHei",
}

# 文本元素高度速查表（与上游 slide-content prompt 的 Height Lookup Table 一致，行高 1.5）。
TEXT_HEIGHT_TABLE = {
    14: [43, 64, 85, 106, 127],
    16: [46, 70, 94, 118, 142],
    18: [49, 76, 103, 130, 157],
    20: [52, 82, 112, 142, 172],
    24: [58, 94, 130, 166, 202],
    28: [64, 106, 148, 190, 232],
    32: [70, 118, 166, 214, 262],
    36: [76, 130, 184, 238, 292],
}

_ALLOWED_TEXT_TAGS = frozenset({
    "p", "span", "strong", "b", "em", "i", "u", "s", "br", "div",
    "h1", "h2", "h3", "h4", "h5", "h6",
    "ul", "ol", "li", "sub", "sup",
})
_ALLOWED_STYLE_PROPS = frozenset({
    "font-size", "color", "text-align", "line-height", "font-weight", "font-family",
})
_FULL_TAG_RE = re.compile(r"(?is)<\s*/?\s*([a-zA-Z][a-zA-Z0-9]*)((?:\s[^>]*)?)/?>")
_ATTR_RE = re.compile(
    r"""([a-zA-Z_:][-a-zA-Z0-9_:.]*)\s*=\s*("[^"]*"|'[^']*'|[^\s"'>]+)"""
)
_UNSAFE_URL_RE = re.compile(r"(?i)(javascript|vbscript)\s*:")
_HEX_COLOR_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$")
_EXTERNAL_URL_RE = re.compile(r"""(?i)(?:src|href)\s*=\s*["']?\s*(?:https?:)?//""")
_EXTERNAL_IMPORT_RE = re.compile(r"""(?i)@import\s+(?:url\()?["']?\s*(?:https?:)?//""")
_EXTERNAL_FETCH_RE = re.compile(r"""(?i)\b(?:fetch|WebSocket)\s*\(\s*["']\s*(?:https?:|wss?:)?//""")
_SCRIPT_SRC_RE = re.compile(r"""(?i)<script[^>]+\bsrc\s*=""")


class ClassroomSchemaError(ValueError):
    """课程 DSL 结构非法：消息可直接展示给用户，也用于驱动模型重试。"""


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------

def _as_text(value, limit: int = 400) -> str:
    """把模型输出里常见的标量安全转成去空白字符串。"""
    if value is None or isinstance(value, bool):
        return ""
    if isinstance(value, (int, float)):
        value = str(value)
    if not isinstance(value, str):
        return ""
    return value.strip()[:limit]


def _as_number(value):
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip().rstrip("px").strip())
        except (TypeError, ValueError):
            return None
    return None


def _num(value, default: float, lo: float, hi: float) -> float:
    number = _as_number(value)
    if number is None:
        number = default
    return max(lo, min(hi, number))


def _as_list(value) -> list:
    return value if isinstance(value, list) else []


def _as_dict(value) -> dict:
    return value if isinstance(value, dict) else {}


def _as_int(value, default: int = 0) -> int:
    number = _as_number(value)
    return int(number) if number is not None else default


def _clean_color(value, default: str) -> str:
    text = _as_text(value, 24)
    return text if _HEX_COLOR_RE.match(text) else default


def _clean_colors(value, default: list[str]) -> list[str]:
    colors = [
        _as_text(item, 24) for item in _as_list(value)
        if _HEX_COLOR_RE.match(_as_text(item, 24))
    ]
    return colors[:8] or list(default)


def _strip_json_fences(text: str) -> str:
    value = (text or "").strip()
    if value.startswith("```"):
        value = re.sub(r"^```(?:json|JSON)?\s*", "", value)
        value = re.sub(r"\s*```$", "", value)
    return value.strip()


def parse_json_payload(raw):
    """接受 dict/list 或 JSON 文本（含代码围栏）；非法 JSON 抛 ClassroomSchemaError。"""
    if isinstance(raw, (dict, list)):
        return raw
    if not isinstance(raw, str):
        raise ClassroomSchemaError("模型输出不是 JSON 对象或数组")
    text = _strip_json_fences(raw)
    if not text:
        raise ClassroomSchemaError("模型输出为空")
    try:
        return json.loads(text)
    except (TypeError, ValueError) as exc:
        raise ClassroomSchemaError("模型输出不是合法 JSON（%s）" % exc) from None


def clamp_scene_count(value, default: int = DEFAULT_SCENE_COUNT) -> int:
    number = _as_int(value, default)
    return max(MIN_SCENE_COUNT, min(MAX_SCENE_COUNT, number))


def normalize_depth(value, default: str = "standard") -> str:
    text = _as_text(value, 20).lower()
    return text if text in DEPTHS else default


# ---------------------------------------------------------------------------
# 富文本净化（幻灯片 text 元素 content 为 HTML 字符串）
# ---------------------------------------------------------------------------

def _clean_style(style: str) -> str:
    """只保留白名单 inline style 声明，其余丢弃。"""
    kept = []
    for declaration in str(style or "").split(";"):
        if ":" not in declaration:
            continue
        prop, _, value = declaration.partition(":")
        prop = prop.strip().lower()
        value = value.strip()
        if prop in _ALLOWED_STYLE_PROPS and value and not _UNSAFE_URL_RE.search(value):
            kept.append("%s: %s" % (prop, value))
    return "; ".join(kept)


def sanitize_text_html(raw, limit: int = MAX_SLIDE_HTML_CHARS) -> str:
    """净化模型生成的文本 HTML：只留白名单标签/样式，去掉脚本、事件属性与危险协议。"""
    text = str(raw or "")[: max(limit * 2, 400)]
    if not text:
        return ""
    # 先整体删除危险容器及其内容，再逐标签清理（未知标签只去掉标签本身、保留文字）。
    text = re.sub(
        r"(?is)<\s*(script|style|iframe|object|embed|svg|template)[^>]*>.*?</\s*\1\s*>",
        "", text,
    )
    text = re.sub(r"(?is)<\s*(script|style|iframe|object|embed|link|meta|base)[^>]*/?>", "", text)

    def _tag(match: re.Match) -> str:
        name = match.group(1).lower()
        if name not in _ALLOWED_TEXT_TAGS:
            return ""
        full = match.group(0)
        if full.lstrip().startswith("</"):
            return "</%s>" % name
        if name == "br":
            return "<br>"
        styles = [
            _clean_style(item.group(2)[1:-1])
            for item in _ATTR_RE.finditer(full)
            if item.group(1).lower() == "style"
        ]
        style = "; ".join(part for part in styles if part)
        return "<%s%s>" % (name, (' style="%s"' % style) if style else "")

    text = _FULL_TAG_RE.sub(_tag, text)
    # 未闭合的裸尖括号转义，避免浏览器把它们当标签解析。
    text = re.sub(r"<(?![/a-zA-Z])", "&lt;", text)
    text = _UNSAFE_URL_RE.sub("", text)
    return text.strip()[:limit]


def html_to_plain(raw) -> str:
    text = str(raw or "")
    text = re.sub(r"(?is)<\s*br\s*/?\s*>", " ", text)
    text = re.sub(r"(?is)</\s*(p|div|li|h[1-6])\s*>", " ", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = html_lib.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _extract_style_value(raw_html: str, prop: str, default=None):
    match = re.search(r"(?is)%s\s*:\s*([^;\"']+)" % re.escape(prop), str(raw_html or ""))
    if not match:
        return default
    return match.group(1).strip()


# ---------------------------------------------------------------------------
# 幻灯片元素
# ---------------------------------------------------------------------------

def _element_id(raw: dict, etype: str, index: int) -> str:
    text = _as_text(raw.get("id"), 40)
    text = re.sub(r"[^A-Za-z0-9_\-]", "", text)
    return text or "%s_%03d" % (etype, index)


def _unique_id(candidate: str, used: set[str], etype: str, index: int) -> str:
    if candidate not in used:
        used.add(candidate)
        return candidate
    for _ in range(50):
        candidate = "%s_%03d" % (etype, index + _)
        if candidate not in used:
            used.add(candidate)
            return candidate
    used.add("elem_%d" % len(used))
    return "elem_%d" % len(used)


def _base_geometry(raw: dict, etype: str, width: float, height: float) -> dict:
    box_width = _num(raw.get("width"), min(320.0, width), 40.0, width)
    box_height = _num(raw.get("height"), min(120.0, height), 16.0, height)
    left = _num(raw.get("left"), 0.0, 0.0, max(0.0, width - box_width))
    top = _num(raw.get("top"), 0.0, 0.0, max(0.0, height - box_height))
    return {
        "left": round(left, 2),
        "top": round(top, 2),
        "width": round(box_width, 2),
        "height": round(box_height, 2),
        "rotate": round(_num(raw.get("rotate"), 0.0, -360.0, 360.0), 2),
    }


def _normalize_text_element(raw: dict, index: int, width: float, height: float, warnings: list[str]) -> dict | None:
    geometry = _base_geometry(raw, "text", width, height)
    raw_html = raw.get("content") or raw.get("html")
    plain = _as_text(raw.get("text"), MAX_SLIDE_HTML_CHARS)
    if raw_html:
        content = sanitize_text_html(raw_html)
        inner_plain = html_to_plain(content)
        if inner_plain and not plain:
            plain = inner_plain[:MAX_SLIDE_HTML_CHARS]
    else:
        content = ""
    if not content and not plain:
        warnings.append("已丢弃空的文本元素")
        return None
    if not content:
        content = '<p style="font-size: %dpx;">%s</p>' % (
            int(_num(raw.get("fontSize"), 18, 10, 72)),
            html_lib.escape(plain[:MAX_SLIDE_HTML_CHARS]),
        )
    size_value = _as_number(_extract_style_value(content, "font-size"))
    font_size = int(round(_num(raw.get("fontSize"), size_value or 18, 10, 72)))
    color_value = _extract_style_value(content, "color")
    color = _clean_color(raw.get("color") or raw.get("defaultColor") or color_value, "#333333")
    align = _as_text(raw.get("align") or _extract_style_value(content, "text-align"), 12).lower()
    element = {
        "id": _element_id(raw, "text", index),
        "type": "text",
        **geometry,
        "content": content,
        "text": plain[:MAX_SLIDE_HTML_CHARS],
        "fontSize": font_size,
        "color": color,
        "defaultFontName": _as_text(raw.get("defaultFontName"), 40),
        "defaultColor": color,
        "lineHeight": round(_num(raw.get("lineHeight"), 1.5, 1.0, 3.0), 2),
    }
    if align in ("left", "center", "right"):
        element["align"] = align
    v_align = _as_text(raw.get("vAlign"), 10)
    if v_align in ("top", "middle", "bottom"):
        element["vAlign"] = v_align
    return element


def _normalize_image_element(raw: dict, index: int, width: float, height: float, warnings: list[str]) -> dict | None:
    src = _as_text(raw.get("src") or raw.get("url") or raw.get("image"), 1000)
    if not src:
        warnings.append("已丢弃缺少 src 的图片元素")
        return None
    if re.match(r"(?i)^(?:https?:)?//", src):
        warnings.append("已丢弃引用外网地址的图片元素（离线课堂不允许外链）")
        return None
    geometry = _base_geometry(raw, "image", width, height)
    return {
        "id": _element_id(raw, "image", index),
        "type": "image",
        **geometry,
        "src": src,
        "alt": _as_text(raw.get("alt"), 120),
        "fixedRatio": bool(raw.get("fixedRatio", False)),
    }


def _normalize_shape_element(raw: dict, index: int, width: float, height: float, warnings: list[str]) -> dict | None:
    geometry = _base_geometry(raw, "shape", width, height)
    path = _as_text(raw.get("path"), 2000)
    if not path:
        shape_type = _as_text(raw.get("shape") or raw.get("shapeType"), 20).lower()
        path = "M 1 0.5 A 0.5 0.5 0 1 1 0 0.5 A 0.5 0.5 0 1 1 1 0.5 Z" if shape_type == "circle" else "M 0 0 L 1 0 L 1 1 L 0 1 Z"
    view_box_raw = _as_list(raw.get("viewBox")) or _as_list(raw.get("viewbox"))
    view_box = [_num(view_box_raw[0], 1.0, 0.01, 10000.0), _num(view_box_raw[1], 1.0, 0.01, 10000.0)] if len(view_box_raw) == 2 else [1.0, 1.0]
    return {
        "id": _element_id(raw, "shape", index),
        "type": "shape",
        **geometry,
        "path": path,
        "viewBox": view_box,
        "fill": _clean_color(raw.get("fill"), "#5b9bd5"),
        "fixedRatio": bool(raw.get("fixedRatio", False)),
    }


def _normalize_line_element(raw: dict, index: int, width: float, height: float, warnings: list[str]) -> dict | None:
    start = _as_list(raw.get("start"))
    end = _as_list(raw.get("end"))
    if len(start) != 2 or len(end) != 2:
        warnings.append("已丢弃缺少 start/end 的线条元素")
        return None
    stroke = _num(raw.get("width"), 2.0, 1.0, 8.0)
    if (_as_number(raw.get("width")) or 0) > 8:
        warnings.append("线条元素 width 超过 6 像素，已按描边宽度归一化为 6")
        stroke = 6.0
    style = _as_text(raw.get("style"), 10).lower()
    points_raw = _as_list(raw.get("points"))
    points = ["", ""]
    if len(points_raw) == 2:
        for pos, item in enumerate(points_raw):
            value = _as_text(item, 8).lower()
            points[pos] = value if value in ("", "arrow", "dot") else ""
    return {
        "id": _element_id(raw, "line", index),
        "type": "line",
        "left": round(_num(raw.get("left"), 0.0, -width, width), 2),
        "top": round(_num(raw.get("top"), 0.0, -height, height), 2),
        "width": round(stroke, 2),
        "start": [round(_num(start[0], 0.0, -width, width), 2), round(_num(start[1], 0.0, -height, height), 2)],
        "end": [round(_num(end[0], 0.0, -width, width), 2), round(_num(end[1], 0.0, -height, height), 2)],
        "style": style if style in ("solid", "dashed", "dotted") else "solid",
        "color": _clean_color(raw.get("color"), "#333333"),
        "points": points,
    }


def _normalize_latex_element(raw: dict, index: int, width: float, height: float, warnings: list[str]) -> dict | None:
    latex = _as_text(raw.get("latex") or raw.get("formula") or raw.get("content"), 2000).strip("$")
    if not latex:
        warnings.append("已丢弃缺少 latex 的公式元素")
        return None
    geometry = _base_geometry(raw, "latex", width, height)
    align = _as_text(raw.get("align"), 10).lower()
    return {
        "id": _element_id(raw, "latex", index),
        "type": "latex",
        **geometry,
        "latex": latex,
        "color": _clean_color(raw.get("color"), "#000000"),
        "align": align if align in ("left", "center", "right") else "center",
    }


def _normalize_table_element(raw: dict, index: int, width: float, height: float, warnings: list[str]) -> dict | None:
    rows_raw = _as_list(raw.get("data")) or _as_list(raw.get("rows"))
    rows = []
    for row in rows_raw[:8]:
        cells = []
        for cell in _as_list(row)[:6]:
            if isinstance(cell, dict):
                text = _as_text(cell.get("text") or cell.get("content"), 200)
                cells.append({
                    "id": _as_text(cell.get("id"), 30) or None,
                    "colspan": max(1, min(6, _as_int(cell.get("colspan"), 1))),
                    "rowspan": max(1, min(8, _as_int(cell.get("rowspan"), 1))),
                    "text": text,
                })
            else:
                cells.append({"id": None, "colspan": 1, "rowspan": 1, "text": _as_text(cell, 200)})
        if cells:
            rows.append(cells)
    if not rows:
        warnings.append("已丢弃缺少 data 的表格元素")
        return None
    geometry = _base_geometry(raw, "table", width, height)
    col_widths = [_num(item, 0.0, 0.0, 1.0) for item in _as_list(raw.get("colWidths"))]
    if not col_widths:
        col_count = max(len(row) for row in rows)
        col_widths = [round(1.0 / col_count, 4)] * col_count
    return {
        "id": _element_id(raw, "table", index),
        "type": "table",
        **geometry,
        "colWidths": col_widths[:6],
        "data": rows,
        "outline": {"width": 1, "style": "solid", "color": "#cccccc"},
    }


_CHART_TYPES = ("bar", "column", "line", "pie", "ring", "area", "radar", "scatter")


def _normalize_chart_element(raw: dict, index: int, width: float, height: float, warnings: list[str]) -> dict | None:
    chart_type = _as_text(raw.get("chartType") or raw.get("chart_type"), 20).lower()
    data = _as_dict(raw.get("data"))
    labels = [_as_text(item, 40) for item in _as_list(data.get("labels"))[:12] if _as_text(item, 40)]
    series = []
    for row in _as_list(data.get("series"))[:6]:
        numbers = [_as_number(item) for item in _as_list(row)[:12]]
        if any(item is not None for item in numbers):
            series.append([round(item or 0.0, 4) for item in numbers])
    if not labels or not series:
        warnings.append("已丢弃缺少有效 data 的图表元素")
        return None
    geometry = _base_geometry(raw, "chart", width, height)
    legends = [_as_text(item, 40) for item in _as_list(data.get("legends"))[:6]]
    return {
        "id": _element_id(raw, "chart", index),
        "type": "chart",
        **geometry,
        "chartType": chart_type if chart_type in _CHART_TYPES else "bar",
        "data": {"labels": labels, "legends": legends, "series": series},
        "themeColors": _clean_colors(raw.get("themeColors"), _DEFAULT_THEME["themeColors"]),
    }


def _normalize_code_element(raw: dict, index: int, width: float, height: float, warnings: list[str]) -> dict | None:
    code = _as_text(raw.get("code"), 4000)
    if not code:
        lines = _as_list(raw.get("lines"))
        code = "\n".join(_as_text(item.get("content") if isinstance(item, dict) else item, 300) for item in lines)[:4000]
    if not code:
        warnings.append("已丢弃缺少 code 的代码元素")
        return None
    geometry = _base_geometry(raw, "code", width, height)
    return {
        "id": _element_id(raw, "code", index),
        "type": "code",
        **geometry,
        "language": _as_text(raw.get("language"), 24) or "text",
        "code": code,
    }


_ELEMENT_NORMALIZERS = {
    "text": _normalize_text_element,
    "image": _normalize_image_element,
    "shape": _normalize_shape_element,
    "line": _normalize_line_element,
    "latex": _normalize_latex_element,
    "table": _normalize_table_element,
    "chart": _normalize_chart_element,
    "code": _normalize_code_element,
}


def _normalize_theme(raw) -> dict:
    source = _as_dict(raw)
    theme = {
        "backgroundColor": _clean_color(source.get("backgroundColor") or source.get("background"), _DEFAULT_THEME["backgroundColor"]),
        "themeColors": _clean_colors(source.get("themeColors") or source.get("colors"), _DEFAULT_THEME["themeColors"]),
        "fontColor": _clean_color(source.get("fontColor") or source.get("color"), _DEFAULT_THEME["fontColor"]),
        "fontName": _as_text(source.get("fontName"), 60) or _DEFAULT_THEME["fontName"],
    }
    if bool(source.get("default")) or not source:
        return dict(_DEFAULT_THEME)
    return theme


def normalize_slide_content(raw) -> tuple[dict, list[str]]:
    """归一化幻灯片内容，返回 ``(content, warnings)``；结构不可用时抛 ClassroomSchemaError。"""
    warnings: list[str] = []
    payload = parse_json_payload(raw)
    if not isinstance(payload, dict):
        raise ClassroomSchemaError("幻灯片内容必须是 JSON 对象")
    canvas = payload.get("canvas")
    if not isinstance(canvas, dict):
        # 兼容上游 slide-content 的 ``{background, elements}`` 输出与直接给 elements 的写法。
        canvas = payload
    raw_width = _as_number(canvas.get("width")) or _as_number(canvas.get("viewportSize"))
    raw_height = _as_number(canvas.get("height"))
    ratio = _as_number(canvas.get("viewportRatio"))
    if raw_height is None and raw_width and ratio:
        raw_height = raw_width * ratio
    if (raw_width and abs(raw_width - CANVAS_WIDTH) > 1) or (raw_height and abs(raw_height - CANVAS_HEIGHT) > 1):
        warnings.append("画布尺寸已统一归一化为 %.0f×%.1f" % (CANVAS_WIDTH, CANVAS_HEIGHT))
    theme = _normalize_theme(canvas.get("theme") or payload.get("theme"))
    background = _as_dict(canvas.get("background") or payload.get("background"))
    if _as_text(background.get("color"), 24):
        theme["backgroundColor"] = _clean_color(background.get("color"), theme["backgroundColor"])
    elements_raw = canvas.get("elements")
    if not isinstance(elements_raw, list) or not elements_raw:
        raise ClassroomSchemaError("幻灯片内容缺少 elements 数组")
    elements: list[dict] = []
    used_ids: set[str] = set()
    for index, item in enumerate(elements_raw):
        if len(elements) >= MAX_ELEMENTS:
            warnings.append("元素数量超过上限 %d，后续元素已丢弃" % MAX_ELEMENTS)
            break
        if not isinstance(item, dict):
            warnings.append("已丢弃非对象的幻灯片元素")
            continue
        raw_type = _as_text(item.get("type") or item.get("elementType"), 24).lower()
        etype = _ELEMENT_ALIASES.get(raw_type)
        if etype is None:
            warnings.append("已丢弃未知元素类型：%s" % (raw_type or "空"))
            continue
        element = _ELEMENT_NORMALIZERS[etype](item, index, CANVAS_WIDTH, CANVAS_HEIGHT, warnings)
        if element is None:
            continue
        element["id"] = _unique_id(element["id"], used_ids, etype, index)
        elements.append(element)
    if not elements:
        raise ClassroomSchemaError("幻灯片没有任何可用元素（allowed: %s）" % ", ".join(ELEMENT_TYPES))
    return {
        "type": "slide",
        "schemaVersion": 1,
        "canvas": {
            "width": CANVAS_WIDTH,
            "height": CANVAS_HEIGHT,
            "theme": theme,
            "elements": elements,
        },
    }, warnings


def slide_element_ids(content: dict) -> list[str]:
    canvas = _as_dict(content.get("canvas")) if isinstance(content, dict) else {}
    return [
        _as_text(item.get("id"), 40)
        for item in _as_list(canvas.get("elements"))
        if isinstance(item, dict) and _as_text(item.get("id"), 40)
    ]


def text_height_for(font_size: int, lines: int) -> int:
    """按上游 Height Lookup Table 取文本元素高度（越界回退到最近档位）。"""
    size = min(TEXT_HEIGHT_TABLE, key=lambda key: abs(key - int(font_size)))
    row = TEXT_HEIGHT_TABLE[size]
    return row[max(0, min(len(row) - 1, int(lines) - 1))]


# ---------------------------------------------------------------------------
# 测验
# ---------------------------------------------------------------------------

def _letter(index: int) -> str:
    return chr(ord("A") + index) if 0 <= index < 26 else "A"


def _normalize_quiz_options(raw) -> list[dict]:
    options: list[dict] = []
    seen: set[str] = set()
    for index, item in enumerate(_as_list(raw)):
        if isinstance(item, str):
            label, value = item.strip(), _letter(index)
        elif isinstance(item, dict):
            label = _as_text(item.get("label") or item.get("text") or item.get("content"), 300)
            value = _as_text(item.get("value") or item.get("key") or item.get("letter"), 10).upper()
        else:
            continue
        # 修正上游明确警告的 label/value 反转：单字母在 label、正文在 value。
        if re.fullmatch(r"[A-Z]", label or "") and value and not re.fullmatch(r"[A-Z]", value):
            label, value = value, label
        if not label:
            continue
        if not re.fullmatch(r"[A-Z]", value or ""):
            value = _letter(len(options))
        if value in seen:
            continue
        seen.add(value)
        options.append({"label": label[:300], "value": value})
        if len(options) >= MAX_QUIZ_OPTIONS:
            break
    return options


def _normalize_answers(raw, options: list[dict]) -> list[str]:
    values = [raw] if isinstance(raw, str) else _as_list(raw)
    by_value = {option["value"]: option["value"] for option in options}
    by_label = {option["label"]: option["value"] for option in options}
    answers: list[str] = []
    for item in values:
        text = _as_text(item, 300).upper()
        if text in by_value:
            answer = text
        elif _as_text(item, 300) in by_label:
            answer = by_label[_as_text(item, 300)]
        else:
            continue
        if answer not in answers:
            answers.append(answer)
    return answers


def normalize_quiz_content(raw) -> tuple[dict, list[str]]:
    """归一化测验内容；单选/多选答案非法时抛 ClassroomSchemaError（触发一次重试）。"""
    warnings: list[str] = []
    payload = parse_json_payload(raw)
    questions_raw = payload.get("questions") if isinstance(payload, dict) else payload
    if not isinstance(questions_raw, list) or not questions_raw:
        raise ClassroomSchemaError("测验内容缺少 questions 数组")
    questions: list[dict] = []
    used_ids: set[str] = set()
    for index, item in enumerate(questions_raw):
        if len(questions) >= MAX_QUESTIONS:
            warnings.append("题目数量超过上限 %d，后续题目已丢弃" % MAX_QUESTIONS)
            break
        if not isinstance(item, dict):
            warnings.append("已丢弃非对象的测验题目")
            continue
        qtype = _QUIZ_TYPE_ALIASES.get(_as_text(item.get("type") or item.get("qtype"), 24).lower())
        if qtype is None:
            warnings.append("已丢弃未知题型：%s" % (_as_text(item.get("type"), 24) or "空"))
            continue
        question_text = _as_text(item.get("question") or item.get("stem") or item.get("title"), 1500)
        if not question_text:
            warnings.append("已丢弃缺少题干的测验题目")
            continue
        qid = _as_text(item.get("id"), 40) or "q%d" % (index + 1)
        if qid in used_ids:
            qid = "q%d" % (index + 1)
        while qid in used_ids:
            qid = "%s_%d" % (qid, index)
        used_ids.add(qid)
        points = round(_num(item.get("points") or item.get("score"), DEFAULT_QUIZ_POINTS, 1.0, MAX_POINTS), 2)
        analysis = _as_text(item.get("analysis") or item.get("explanation"), 1500)
        if qtype == "short_answer":
            question = {
                "id": qid,
                "type": "short_answer",
                "question": question_text,
                "options": [],
                "answer": [],
                "analysis": analysis,
                "commentPrompt": _as_text(item.get("commentPrompt") or item.get("rubric"), 1500),
                "points": points,
                "hasAnswer": False,
            }
            questions.append(question)
            continue
        options = _normalize_quiz_options(item.get("options") or item.get("choices"))
        if len(options) < 2:
            raise ClassroomSchemaError("第 %d 题（%s）至少需要 2 个有效选项" % (index + 1, qid))
        answers = _normalize_answers(item.get("answer") or item.get("answers"), options)
        if not answers:
            raise ClassroomSchemaError("第 %d 题（%s）的 answer 不是合法选项 value（应为 A/B/C…）" % (index + 1, qid))
        if qtype == "single":
            if len(answers) > 1:
                warnings.append("第 %d 题是单选题但有多个答案，已保留第一个" % (index + 1))
                answers = answers[:1]
        elif len(answers) == 1:
            warnings.append("第 %d 题是多选题但只有 1 个答案，已保留" % (index + 1))
        questions.append({
            "id": qid,
            "type": qtype,
            "question": question_text,
            "options": options,
            "answer": answers,
            "analysis": analysis,
            "points": points,
            "hasAnswer": True,
        })
    if not questions:
        raise ClassroomSchemaError("测验没有可用题目")
    return {"type": "quiz", "questions": questions}, warnings


# ---------------------------------------------------------------------------
# 交互场景 / PBL
# ---------------------------------------------------------------------------

def normalize_widget(value, default: str = "simulation") -> str:
    text = _as_text(value, 40).lower().replace("-", "").replace(" ", "")
    for key, widget in _WIDGET_ALIASES.items():
        if key.replace("-", "").replace(" ", "") == text:
            return widget
    return default


def normalize_interactive_html(raw, title: str = "", widget: str = "") -> str:
    """校验并补全交互 HTML；外链资源/超限/空内容抛 ClassroomSchemaError。

    ``widget`` 用于按交互类型选择体积上限：3D 场景含服务端内联的 three.js，上限放宽到 1.5MB；
    其余类型沿用 600KB。调用方应先归一化 widget 再传入。
    """
    text = _as_text(raw, MAX_HTML_BYTES + 1) if not isinstance(raw, str) else str(raw).strip()
    if not text:
        raise ClassroomSchemaError("交互场景缺少 html 内容")
    limit = min(MAX_HTML_BYTES, interactive_html_limit(widget))
    size = len(text.encode("utf-8", "ignore"))
    if size > limit:
        raise ClassroomSchemaError("交互场景 HTML 超过 %dKB 上限（当前 %d 字节）" % (limit // 1024, size))
    if _EXTERNAL_URL_RE.search(text) or _EXTERNAL_IMPORT_RE.search(text) or _EXTERNAL_FETCH_RE.search(text) or _SCRIPT_SRC_RE.search(text):
        raise ClassroomSchemaError("交互场景不允许引用外网资源（离线可用要求），请把脚本与样式内联")
    lowered = text.lower()
    if "<html" not in lowered:
        safe_title = html_lib.escape(_as_text(title, 120) or "交互场景")
        text = (
            "<!DOCTYPE html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
            "<title>%s</title><style>html,body{margin:0;padding:0;font-family:system-ui,sans-serif;}"
            "body{box-sizing:border-box;padding:16px;color:#1f2933;background:#fff;}"
            "#experiment{display:block;}</style></head><body><div id=\"experiment\">%s</div></body></html>"
        ) % (safe_title, text)
    return text


def normalize_interactive_content(raw) -> tuple[dict, list[str]]:
    warnings: list[str] = []
    payload = parse_json_payload(raw)
    if isinstance(payload, str):
        payload = {"html": payload}
    if not isinstance(payload, dict):
        raise ClassroomSchemaError("交互场景内容必须是 JSON 对象")
    widget_raw = payload.get("widget") or payload.get("widgetType") or payload.get("widget_type")
    widget = normalize_widget(widget_raw)
    if widget_raw and normalize_widget(widget_raw, default="") == "":
        warnings.append("未知交互类型 %s，已归一化为 simulation" % _as_text(widget_raw, 40))
    html = normalize_interactive_html(
        payload.get("html") or payload.get("content") or payload.get("document"),
        _as_text(payload.get("title"), 120),
        widget=widget,
    )
    height = int(round(_num(payload.get("height"), 480, 240, 1200)))
    return {"type": "interactive", "widget": widget, "html": html, "height": height}, warnings


def placeholder_interactive_html(title: str, widget: str, notice: str) -> str:
    """P0 占位交互页：明确说明内容待生成，不伪造 AI 结果。"""
    safe_title = html_lib.escape(title or "交互场景")
    safe_widget = html_lib.escape(WIDGET_LABELS.get(widget, widget))
    safe_notice = html_lib.escape(notice)
    return (
        "<!DOCTYPE html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        "<title>%s</title><style>html,body{margin:0;height:100%%;font-family:system-ui,sans-serif;}"
        "body{display:flex;align-items:center;justify-content:center;background:#f8fafc;color:#334155;}"
        ".box{max-width:520px;padding:28px;border:1px dashed #94a3b8;border-radius:12px;background:#fff;}"
        ".badge{display:inline-block;padding:2px 10px;border-radius:999px;background:#e2e8f0;font-size:12px;}"
        "h1{font-size:20px;margin:12px 0 8px;}p{line-height:1.7;margin:6px 0;font-size:14px;}</style>"
        "</head><body><div id=\"experiment\" class=\"box\"><span class=\"badge\">占位 · 待生成</span>"
        "<h1>%s</h1><p>交互类型：%s</p><p>%s</p></div></body></html>"
    ) % (safe_title, safe_title, safe_widget, safe_notice)


def normalize_pbl_content(raw) -> tuple[dict, list[str]]:
    warnings: list[str] = []
    payload = parse_json_payload(raw)
    if not isinstance(payload, dict):
        raise ClassroomSchemaError("PBL 内容必须是 JSON 对象")
    project = payload.get("project") or payload.get("projectV2") or payload.get("project_v2") or payload
    if not isinstance(project, dict):
        raise ClassroomSchemaError("PBL 内容缺少 project 对象")
    title = _as_text(project.get("title"), 120)
    if not title:
        raise ClassroomSchemaError("PBL 项目缺少标题")
    roles = []
    for index, item in enumerate(_as_list(project.get("roles"))[:6]):
        if isinstance(item, str):
            name, description = item.strip(), ""
        elif isinstance(item, dict):
            name = _as_text(item.get("name") or item.get("title"), 60)
            description = _as_text(item.get("description") or item.get("persona"), 300)
        else:
            continue
        if name:
            roles.append({"id": _as_text(item.get("id"), 40) if isinstance(item, dict) else "", "name": name, "description": description})
    milestones = []
    for index, item in enumerate(_as_list(project.get("milestones"))[:8]):
        if not isinstance(item, dict):
            continue
        milestone_title = _as_text(item.get("title"), 120)
        if not milestone_title:
            continue
        tasks = []
        for task in _as_list(item.get("tasks") or item.get("microtasks"))[:8]:
            text = _as_text(task.get("title") if isinstance(task, dict) else task, 200)
            if text:
                tasks.append(text)
        milestones.append({
            "title": milestone_title,
            "tasks": tasks,
            "deliverable": _as_text(item.get("deliverable") or item.get("completionCriteria"), 300),
        })
    rubric = []
    for item in _as_list(project.get("rubric"))[:8]:
        text = _as_text(item.get("criterion") if isinstance(item, dict) else item, 200)
        if text:
            rubric.append(text)
    if not milestones:
        warnings.append("PBL 项目没有里程碑，已保留空里程碑列表（内容待补充）")
    return {
        "type": "pbl",
        "project": {
            "title": title,
            "background": _as_text(project.get("background") or project.get("description"), 2000),
            "roles": roles,
            "milestones": milestones,
            "rubric": rubric,
        },
    }, warnings


def placeholder_pbl_content(title: str, notice: str) -> dict:
    """P0 占位 PBL 结构：显式标注待生成，不编造角色/里程碑。"""
    return {
        "type": "pbl",
        "project": {
            "title": title[:120] or "项目式学习",
            "background": notice,
            "roles": [],
            "milestones": [],
            "rubric": [],
        },
        "placeholder": True,
    }


# ---------------------------------------------------------------------------
# 手工「原理演示」场景：固定版本学习附件快照
# ---------------------------------------------------------------------------
#
# 输入由课堂插入接口组装（附件读取已完成归属与版本校验）：
#   {"artifact": {"artifact_id", "revision", "kind", "title", "data", "preview"},
#    "source_note": str, "assumptions": [str], "step_count": int, "fixed": true}
# 输出把附件内容**冻结**成 content 快照：课堂播放与 PPTX / 离线 HTML / 打印讲义 / ZIP 导出
# 都只读该快照，源附件后续升版不会自动改变已插入的演示场景。


def _demo_dump_json(value) -> str:
    """演示内容的稳定 JSON 序列化；NaN / Infinity / 自定义对象按非法处理（中文提示）。"""
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError):
        raise ClassroomSchemaError("演示内容包含无法序列化的数据") from None


def _demo_json_bytes(value) -> int:
    return len(_demo_dump_json(value).encode("utf-8"))


def _demo_text(value, field: str, limit: int, *, required: bool = False) -> str:
    """演示快照文本字段校验：必须是文本、去空白后不超限；``required`` 时空值报错。"""
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise ClassroomSchemaError("%s必须是文本" % field)
    text = value.strip()
    if len(text) > limit:
        raise ClassroomSchemaError("%s最多 %d 个字符（当前 %d 个）" % (field, limit, len(text)))
    if required and not text:
        raise ClassroomSchemaError("%s不能为空" % field)
    return text


def _demo_int(value, field: str, low: int, high: int, *, default: int | None = None) -> int:
    """演示快照整数字段校验（拒绝布尔 / 小数 / 非数字），越界抛中文 schema 错误。"""
    if value is None and default is not None:
        return int(default)
    if isinstance(value, bool) or value is None:
        raise ClassroomSchemaError("%s必须是整数" % field)
    if isinstance(value, float) and not value.is_integer():
        raise ClassroomSchemaError("%s必须是整数" % field)
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ClassroomSchemaError("%s必须是整数" % field) from None
    if number < low or number > high:
        raise ClassroomSchemaError("%s需在 %d–%d 之间（当前 %d）" % (field, low, high, number))
    return number


def _normalize_demo_preview(raw) -> dict | None:
    """校验附件 preview 快照：``png`` 必须是可解码且 ≤200KB 的 PNG data URI，``svg`` ≤60KB。

    其他预览键（如 ``kind`` / ``preset_id``）原样保留，保证快照与源附件一致；未知键也必须
    JSON 可序列化。``svg`` 只随 JSON 快照保存，绝不内联进 HTML / PPTX 渲染（避免主动内容）。
    """
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ClassroomSchemaError("演示附件 preview 必须是 JSON 对象")
    preview: dict = {}
    png = raw.get("png")
    if png is not None:
        if not isinstance(png, str):
            raise ClassroomSchemaError("演示海报 png 必须是 PNG data URI 文本")
        text = png.strip()
        if not text.startswith(DEMO_PNG_DATA_PREFIX):
            raise ClassroomSchemaError("演示海报 png 必须以 data:image/png;base64, 开头")
        try:
            payload = base64.b64decode(text[len(DEMO_PNG_DATA_PREFIX):], validate=True)
        except (TypeError, ValueError):
            raise ClassroomSchemaError("演示海报 png 不是合法的 base64 数据") from None
        if len(payload) > MAX_DEMO_PNG_BYTES:
            raise ClassroomSchemaError("演示海报 png 超过 200KB 上限，请压缩后再插入")
        preview["png"] = text
    svg = raw.get("svg")
    if svg is not None:
        if not isinstance(svg, str):
            raise ClassroomSchemaError("演示海报 svg 必须是文本")
        if len(svg) > MAX_DEMO_SVG_CHARS:
            raise ClassroomSchemaError("演示海报 svg 超过 60KB 上限，请精简后再插入")
        preview["svg"] = svg
    for key, value in raw.items():
        if key not in ("png", "svg"):
            preview[str(key)] = value
    if not preview:
        return None
    _demo_dump_json(preview)  # 其他预览字段同样必须可 JSON 序列化
    return preview


def _sandbox_number(value: object) -> bool:
    """可选数值字段存在时必须是有限数（bool 不算数值）。"""
    return (not isinstance(value, bool)
            and isinstance(value, (int, float))
            and math.isfinite(value))


def _validate_sandbox_bodies(data: dict) -> list:
    """校验刚体列表：非空、≤60、形状白名单、数值字段有限；返回刚体列表。"""
    bodies = data.get("bodies")
    if not isinstance(bodies, list) or not bodies:
        raise ClassroomSchemaError("物理沙盒刚体 bodies 必须是非空数组")
    if len(bodies) > SANDBOX_DEMO_MAX_BODIES:
        raise ClassroomSchemaError("物理沙盒刚体数量超过 %d 上限" % SANDBOX_DEMO_MAX_BODIES)
    for index, body in enumerate(bodies):
        if not isinstance(body, dict):
            raise ClassroomSchemaError("第 %d 个刚体必须是 JSON 对象" % (index + 1))
        if body.get("shape") not in SANDBOX_DEMO_SHAPES:
            raise ClassroomSchemaError(
                "第 %d 个刚体形状仅支持 rect/circle/slope" % (index + 1))
        for field in _SANDBOX_BODY_NUMBERS:
            if field in body and not _sandbox_number(body[field]):
                raise ClassroomSchemaError(
                    "第 %d 个刚体的 %s 必须是有限数值" % (index + 1, field))
    return bodies


def _validate_sandbox_demo_data(data: dict) -> None:
    """校验物理沙盒演示快照结构（约束引用与重力数值；刚体见上一函数）。"""
    bodies = _validate_sandbox_bodies(data)
    constraints = data.get("constraints")
    if constraints is None:
        constraints = []
    if not isinstance(constraints, list):
        raise ClassroomSchemaError("物理沙盒约束 constraints 必须是数组")
    if len(constraints) > SANDBOX_DEMO_MAX_CONSTRAINTS:
        raise ClassroomSchemaError(
            "物理沙盒约束数量超过 %d 上限" % SANDBOX_DEMO_MAX_CONSTRAINTS)
    for index, item in enumerate(constraints):
        if not isinstance(item, dict):
            raise ClassroomSchemaError("第 %d 条约束必须是 JSON 对象" % (index + 1))
        ends = {}
        for name in ("bodyA", "bodyB"):
            value = item.get(name)
            if not isinstance(value, int) or isinstance(value, bool):
                raise ClassroomSchemaError(
                    "第 %d 条约束的 %s 必须是整数索引" % (index + 1, name))
            if not 0 <= value < len(bodies):
                raise ClassroomSchemaError(
                    "第 %d 条约束的 %s 超出刚体索引范围" % (index + 1, name))
            ends[name] = value
        if ends["bodyA"] == ends["bodyB"]:
            raise ClassroomSchemaError(
                "第 %d 条约束的两端不能是同一个刚体" % (index + 1))
        for field in ("stiffness", "length", "damping"):
            if field in item and not _sandbox_number(item[field]):
                raise ClassroomSchemaError(
                    "第 %d 条约束的 %s 必须是有限数值" % (index + 1, field))
    gravity = data.get("gravity")
    if gravity is None:
        gravity = {}
    if not isinstance(gravity, dict):
        raise ClassroomSchemaError("物理沙盒重力 gravity 必须是 JSON 对象")
    for field in ("x", "y"):
        if field in gravity and not _sandbox_number(gravity[field]):
            raise ClassroomSchemaError("物理沙盒重力的 %s 必须是有限数值" % field)


def normalize_demo_content(raw) -> tuple[dict, list[str]]:
    """归一化「原理演示」场景内容（固定版本学习附件快照），返回 ``(content, warnings)``。

    说明：

    - 输出固定 ``type="demo"``、``fixed=True`` 与 ``fixed_revision``；``artifact`` 为附件快照
      （``artifact_id`` / ``revision`` / ``kind`` / ``title`` / ``data`` / ``preview``）；
    - 限额：data ≤200KB、png ≤200KB、svg ≤60KB、整个 content ≤260KB、source_note ≤300 字、
      assumptions ≤6 条且每条 ≤200 字、step_count 0–50、revision 1–100000；
    - 结构或限额非法抛 :class:`ClassroomSchemaError`（中文，可直接作为 422 detail 展示）；
    - warnings 固定提示「演示为固定版本 vN：课堂与导出使用该版本，源附件更新不会自动改变」。
    """
    warnings: list[str] = []
    payload = parse_json_payload(raw)
    if not isinstance(payload, dict):
        raise ClassroomSchemaError("演示内容必须是 JSON 对象")
    artifact_raw = payload.get("artifact")
    if not isinstance(artifact_raw, dict):
        raise ClassroomSchemaError("演示内容缺少 artifact 附件快照对象")
    artifact_id = _demo_text(artifact_raw.get("artifact_id"), "附件 ID", 64, required=True)
    if not DEMO_ARTIFACT_ID_RE.match(artifact_id):
        raise ClassroomSchemaError("附件 ID 格式不正确：需为 6–64 位字母、数字、下划线或短横线")
    revision = _demo_int(artifact_raw.get("revision"), "附件版本号", 1, MAX_DEMO_REVISION)
    kind = _demo_text(artifact_raw.get("kind"), "附件类型", 40, required=True)
    if kind not in DEMO_ARTIFACT_KINDS:
        raise ClassroomSchemaError(
            "演示附件类型仅支持：%s" % "、".join(DEMO_ARTIFACT_KINDS)
        )
    title = _demo_text(artifact_raw.get("title"), "附件标题", 120, required=True)
    data_raw = artifact_raw.get("data")
    if not isinstance(data_raw, dict):
        raise ClassroomSchemaError("演示附件 data 必须是 JSON 对象")
    data_payload = _demo_dump_json(data_raw)
    if len(data_payload.encode("utf-8")) > MAX_DEMO_DATA_BYTES:
        raise ClassroomSchemaError("演示附件 data 超过 200KB 上限，请精简后再插入")
    if kind == "physics_sandbox":
        # 结构上限（刚体/约束/形状/数值）在插入固定版本时校验，见常量区注释。
        _validate_sandbox_demo_data(data_raw)
    preview = _normalize_demo_preview(artifact_raw.get("preview"))
    source_note = _demo_text(
        payload.get("source_note"), "演示说明", MAX_DEMO_SOURCE_NOTE_CHARS,
    )
    assumptions_raw = payload.get("assumptions")
    if assumptions_raw is None:
        assumptions_raw = []
    if not isinstance(assumptions_raw, (list, tuple)):
        raise ClassroomSchemaError("演示假设 assumptions 必须是数组")
    if len(assumptions_raw) > MAX_DEMO_ASSUMPTIONS:
        raise ClassroomSchemaError("演示假设最多 %d 条" % MAX_DEMO_ASSUMPTIONS)
    assumptions: list[str] = []
    for item in assumptions_raw:
        text = _demo_text(item, "演示假设", MAX_DEMO_ASSUMPTION_CHARS)
        if text:
            assumptions.append(text)
    step_count = _demo_int(
        payload.get("step_count"), "演示步骤数", 0, MAX_DEMO_STEP_COUNT, default=0,
    )
    content = {
        "type": "demo",
        "schemaVersion": 1,
        "artifact": {
            "artifact_id": artifact_id,
            "revision": revision,
            "kind": kind,
            "title": title,
            "data": json.loads(data_payload),
            "preview": preview,
        },
        "source_note": source_note,
        "assumptions": assumptions,
        "step_count": step_count,
        "fixed": True,
        "fixed_revision": revision,
    }
    if _demo_json_bytes(content) > MAX_DEMO_CONTENT_BYTES:
        raise ClassroomSchemaError(
            "演示场景内容超过 260KB 上限，请精简附件预览或说明后再插入"
        )
    warnings.append(
        "演示为固定版本 v%d：课堂与导出使用该版本，源附件更新不会自动改变" % revision
    )
    return content, warnings


# ---------------------------------------------------------------------------
# 场景动作（播放器白名单）
# ---------------------------------------------------------------------------

_WHITEBOARD_KINDS = ("text", "line", "rect", "circle", "formula")


def _wb_number(value, limit: float) -> float:
    return round(_num(value, 0.0, -limit, limit), 2)


def _normalize_whiteboard_op(raw) -> dict | None:
    if not isinstance(raw, dict):
        return None
    kind_raw = _as_text(raw.get("kind") or raw.get("type") or raw.get("op"), 16).lower()
    aliases = {
        "text": "text", "label": "text", "文字": "text",
        "line": "line", "arrow": "line", "线": "line",
        "rect": "rect", "rectangle": "rect", "box": "rect", "矩形": "rect",
        "circle": "circle", "ellipse": "circle", "圆": "circle",
        "formula": "formula", "latex": "formula", "公式": "formula",
    }
    kind = aliases.get(kind_raw)
    if kind is None:
        return None
    if kind == "text":
        content = _as_text(raw.get("content") or raw.get("text"), 500)
        if not content:
            return None
        return {"kind": "text", "content": content, "x": _wb_number(raw.get("x"), WHITEBOARD_WIDTH), "y": _wb_number(raw.get("y"), WHITEBOARD_HEIGHT),
                "font_size": int(_num(raw.get("font_size") or raw.get("fontSize"), 18, 10, 72)), "color": _clean_color(raw.get("color"), "#333333")}
    if kind == "line":
        return {"kind": "line", "x1": _wb_number(raw.get("x1", raw.get("start_x")), WHITEBOARD_WIDTH), "y1": _wb_number(raw.get("y1", raw.get("start_y")), WHITEBOARD_HEIGHT),
                "x2": _wb_number(raw.get("x2", raw.get("end_x")), WHITEBOARD_WIDTH), "y2": _wb_number(raw.get("y2", raw.get("end_y")), WHITEBOARD_HEIGHT),
                "color": _clean_color(raw.get("color"), "#333333"), "width": round(_num(raw.get("width"), 2, 1, 8), 2)}
    if kind in ("rect", "circle"):
        return {"kind": kind, "x": _wb_number(raw.get("x"), WHITEBOARD_WIDTH), "y": _wb_number(raw.get("y"), WHITEBOARD_HEIGHT),
                "width": round(_num(raw.get("width"), 200, 10, WHITEBOARD_WIDTH), 2), "height": round(_num(raw.get("height"), 120, 10, WHITEBOARD_HEIGHT), 2),
                "fill": _clean_color(raw.get("fill") or raw.get("color"), "#5b9bd5")}
    latex = _as_text(raw.get("latex") or raw.get("formula"), 500).strip("$")
    if not latex:
        return None
    return {"kind": "formula", "latex": latex, "x": _wb_number(raw.get("x"), WHITEBOARD_WIDTH), "y": _wb_number(raw.get("y"), WHITEBOARD_HEIGHT),
            "width": round(_num(raw.get("width"), 300, 20, WHITEBOARD_WIDTH), 2)}


def _normalize_action_item(raw, seq: int, element_ids: set[str] | None) -> tuple[dict | None, str | None]:
    if not isinstance(raw, dict):
        return None, "已丢弃非对象的课堂动作"
    # 兼容上游「动作信封」：{"type":"action","name":"spotlight","params":{...}} 与
    # {"type":"text","content":"旁白"}；同时保留本模块的扁平写法 {"type":"spotlight",...}。
    params = raw.get("params")
    if isinstance(params, dict):
        flattened = dict(params)
        flattened.update({key: value for key, value in raw.items() if key != "params"})
        raw = flattened
    type_raw = _as_text(raw.get("type"), 24).lower()
    if type_raw in ("action", "", "tool"):
        type_raw = _as_text(raw.get("name"), 24).lower()
    elif type_raw == "text" and raw.get("name"):
        type_raw = _as_text(raw.get("name"), 24).lower()
    action_type = _ACTION_ALIASES.get(type_raw)
    if action_type is None:
        return None, "已拒绝未知课堂动作：%s" % (type_raw or "空")
    action_id = _as_text(raw.get("id"), 40) or "act_%03d" % seq
    key = _as_text(raw.get("element_id") or raw.get("elementId") or raw.get("target"), 60)
    if action_type in ("spotlight", "laser", "highlight"):
        if not key:
            return None, "%s 动作缺少 element_id，已丢弃" % action_type
        if element_ids is not None and key not in element_ids:
            return None, "%s 动作引用了不存在的元素 %s，已丢弃" % (action_type, key)
        action = {"id": action_id, "type": action_type, "element_id": key}
        if action_type == "spotlight":
            action["dim_opacity"] = round(_num(raw.get("dim_opacity") or raw.get("dimOpacity"), 0.5, 0.0, 1.0), 2)
        elif action_type in ("laser", "highlight"):
            action["color"] = _clean_color(raw.get("color"), "#ff0000" if action_type == "laser" else "#ffd43b")
        return action, None
    if action_type == "speech":
        text = _as_text(raw.get("text") or raw.get("content"), MAX_SPEECH_CHARS)
        if not text:
            return None, "speech 动作缺少旁白文本，已丢弃"
        action = {"id": action_id, "type": "speech", "text": text}
        audio_id = _as_number(raw.get("audio_id") or raw.get("audioId"))
        if audio_id is not None:
            action["audio_id"] = int(audio_id)
        voice = _as_text(raw.get("voice"), 60)
        if voice:
            action["voice"] = voice
        if _as_number(raw.get("speed")) is not None:
            action["speed"] = round(_num(raw.get("speed"), 1.0, 0.5, 2.0), 2)
        return action, None
    if action_type == "whiteboard":
        ops = [_normalize_whiteboard_op(item) for item in _as_list(raw.get("ops") or raw.get("operations"))]
        ops = [op for op in ops if op is not None][:20]
        if not ops:
            return None, "whiteboard 动作没有有效操作，已丢弃"
        return {"id": action_id, "type": "whiteboard", "ops": ops}, None
    if action_type == "next_scene":
        action = {"id": action_id, "type": "next_scene"}
        scene_id = _as_text(raw.get("scene_id") or raw.get("sceneId"), 40)
        if scene_id:
            action["scene_id"] = scene_id
        return action, None
    if action_type == "show_slide":
        action = {"id": action_id, "type": "show_slide"}
        if _as_number(raw.get("index")) is not None:
            action["index"] = max(0, _as_int(raw.get("index"), 0))
        scene_id = _as_text(raw.get("scene_id") or raw.get("sceneId"), 40)
        if scene_id:
            action["scene_id"] = scene_id
        if "index" not in action and "scene_id" not in action:
            return None, "show_slide 动作缺少 index/scene_id，已丢弃"
        return action, None
    # play_audio
    action = {"id": action_id, "type": "play_audio"}
    asset_id = _as_number(raw.get("asset_id") or raw.get("audio_id") or raw.get("audioId"))
    url = _as_text(raw.get("url"), 500)
    if asset_id is not None:
        action["asset_id"] = int(asset_id)
    if url:
        if re.match(r"(?i)^(?:https?:)?//", url):
            return None, "play_audio 不允许外网音频地址，已丢弃"
        action["url"] = url
    if "asset_id" not in action and "url" not in action:
        return None, "play_audio 动作缺少 asset_id/url，已丢弃"
    return action, None


def normalize_actions(raw, *, element_ids: list[str] | set[str] | None = None, max_actions: int = MAX_ACTIONS) -> tuple[list[dict], list[str]]:
    """校验并规范化动作列表，返回 ``(actions, warnings)``；未知动作一律拒绝。"""
    warnings: list[str] = []
    if raw is None:
        return [], warnings
    if isinstance(raw, dict):
        raw = raw.get("actions") or raw.get("items") or []
    if not isinstance(raw, list):
        return [], ["动作列表不是数组，已忽略"]
    allowed_ids = set(element_ids) if element_ids is not None else None
    actions: list[dict] = []
    used_ids: set[str] = set()
    for index, item in enumerate(raw[:max_actions]):
        action, warning = _normalize_action_item(item, index + 1, allowed_ids)
        if warning:
            warnings.append(warning)
        if action is None:
            continue
        if action["id"] in used_ids:
            action["id"] = "act_%03d" % (len(used_ids) + 1)
        used_ids.add(action["id"])
        actions.append(action)
    if len(raw) > max_actions:
        warnings.append("动作数量超过上限 %d，后续动作已丢弃" % max_actions)
    return actions, warnings


def speech_text_of(actions: list[dict]) -> str:
    return " ".join(action.get("text") or "" for action in actions if action.get("type") == "speech").strip()


# ---------------------------------------------------------------------------
# 场景内容 / 大纲
# ---------------------------------------------------------------------------

_CONTENT_NORMALIZERS = {
    "slide": normalize_slide_content,
    "quiz": normalize_quiz_content,
    "interactive": normalize_interactive_content,
    "pbl": normalize_pbl_content,
    # 手工「原理演示」场景：固定版本学习附件快照（仅由插入接口调用，不参与 AI 生成）。
    "demo": normalize_demo_content,
}


def normalize_scene_content(scene_type: str, raw) -> tuple[dict, list[str]]:
    normalizer = _CONTENT_NORMALIZERS.get(scene_type)
    if normalizer is None:
        raise ClassroomSchemaError("未知场景类型：%s" % scene_type)
    content, warnings = normalizer(raw)
    content["type"] = scene_type
    return content, warnings


def normalize_scene_type(value, default: str = "slide") -> str:
    return _SCENE_TYPE_ALIASES.get(_as_text(value, 24).lower(), default)


def normalize_scene_types(values, default: tuple[str, ...] = SCENE_TYPES) -> list[str]:
    """归一化“AI 生成”场景类型白名单：只保留 ``SCENE_TYPES``，手工演示类型不参与批量生成。

    别名表虽然识别 ``demo`` / ``原理演示``（供导入、播放器与前端分派使用），但 AI 生成流程
    仍以 :data:`SCENE_TYPES` 为白名单，不会把手工插入的演示场景分配给模型生成。
    """
    if not isinstance(values, (list, tuple)):
        return list(default)
    result: list[str] = []
    for item in values:
        scene_type = _SCENE_TYPE_ALIASES.get(_as_text(item, 24).lower())
        if scene_type and scene_type in SCENE_TYPES and scene_type not in result:
            result.append(scene_type)
    return result or list(default)


def normalize_outline(raw, *, fallback_title: str = "未命名课程", scene_count: int = DEFAULT_SCENE_COUNT,
                     scene_types: tuple[str, ...] | list[str] = SCENE_TYPES) -> tuple[dict, list[str]]:
    """归一化大纲，返回 ``(outline, warnings)``。

    兼容本模块 prompt 的 ``{title,description,scenes[]}`` 与上游 ``{courseTitle,outlines[]}`` 两种形状。
    """
    warnings: list[str] = []
    payload = parse_json_payload(raw)
    if isinstance(payload, list):
        payload = {"scenes": payload}
    if not isinstance(payload, dict):
        raise ClassroomSchemaError("大纲必须是 JSON 对象")
    allowed_types = tuple(scene_types) or SCENE_TYPES
    title = _as_text(payload.get("title") or payload.get("courseTitle") or payload.get("course_title"), 60) or fallback_title
    description = _as_text(payload.get("description") or payload.get("courseDescription") or payload.get("summary"), 500)
    language_directive = _as_text(payload.get("languageDirective") or payload.get("language_directive"), 600)
    scenes_raw = payload.get("scenes") or payload.get("outlines") or []
    if not isinstance(scenes_raw, list) or not scenes_raw:
        raise ClassroomSchemaError("大纲缺少 scenes 数组")
    scenes: list[dict] = []
    for index, item in enumerate(scenes_raw):
        if not isinstance(item, dict):
            warnings.append("已丢弃非对象的大纲场景")
            continue
        scene_title = _as_text(item.get("title") or item.get("name"), 120)
        if not scene_title:
            continue
        scene_type = normalize_scene_type(item.get("type") or item.get("sceneType"), "slide")
        if scene_type not in allowed_types:
            replacement = "slide" if "slide" in allowed_types else allowed_types[0]
            warnings.append("场景「%s」的类型 %s 不在本次范围，已改为 %s" % (scene_title, scene_type, replacement))
            scene_type = replacement
        points = [
            _as_text(point, 200)
            for point in _as_list(item.get("outline_points") or item.get("keyPoints") or item.get("key_points") or item.get("points"))[:6]
            if _as_text(point, 200)
        ]
        description_text = _as_text(item.get("description") or item.get("summary"), 500)
        if not points and description_text:
            points = [description_text]
            warnings.append("场景「%s」缺少要点，已用简介补位" % scene_title)
        scene = {
            "id": "scene_%d" % (len(scenes) + 1),
            "title": scene_title,
            "type": scene_type,
            "description": description_text,
            "outline_points": points,
        }
        widget = item.get("widgetType") or item.get("widget_type") or item.get("widget")
        if scene_type == "interactive" and _as_text(widget, 40):
            scene["widget"] = normalize_widget(widget)
        quiz_config = _as_dict(item.get("quizConfig") or item.get("quiz_config") or item.get("quiz"))
        if scene_type == "quiz" and quiz_config:
            scene["quiz"] = {
                "question_count": max(1, min(MAX_QUESTIONS, _as_int(quiz_config.get("questionCount") or quiz_config.get("question_count"), 3))),
                "difficulty": _as_text(quiz_config.get("difficulty"), 12) or "medium",
            }
        pbl_config = _as_dict(item.get("pblConfig") or item.get("pbl_config") or item.get("pbl"))
        if scene_type == "pbl" and pbl_config:
            scene["pbl"] = {
                "project_topic": _as_text(pbl_config.get("projectTopic") or pbl_config.get("project_topic"), 200),
                "target_skills": [_as_text(skill, 80) for skill in _as_list(pbl_config.get("targetSkills") or pbl_config.get("target_skills"))[:5] if _as_text(skill, 80)],
            }
        scenes.append(scene)
    if not scenes:
        raise ClassroomSchemaError("大纲没有可用场景")
    if len(scenes) > scene_count:
        warnings.append("大纲场景数超过本次要求的 %d 个，多余场景已截断" % scene_count)
        scenes = scenes[:scene_count]
    if len(scenes) < scene_count:
        warnings.append("大纲只生成了 %d 个场景（要求 %d 个）" % (len(scenes), scene_count))
    for index, scene in enumerate(scenes, 1):
        scene["id"] = "scene_%d" % index
        scene["order"] = index
    return {
        "title": title,
        "description": description,
        "language_directive": language_directive or "使用简体中文授课；术语首次出现时给出中文解释，专有名词保留英文。",
        "scenes": scenes,
    }, warnings


# ---------------------------------------------------------------------------
# 课堂对话（tutor）
# ---------------------------------------------------------------------------

_TUTOR_SPEAKERS = ("teacher", "assistant", "student", "system")


def normalize_tutor_reply(raw, *, element_ids: list[str] | set[str] | None = None,
                           scene_count: int = 0) -> tuple[dict, list[str]]:
    """校验 tutor 同步回复：``{reply:{speaker,text,actions},scene_hint?}``。"""
    warnings: list[str] = []
    payload = parse_json_payload(raw)
    if not isinstance(payload, dict):
        raise ClassroomSchemaError("对话回复必须是 JSON 对象")
    reply_raw = payload.get("reply") if isinstance(payload.get("reply"), dict) else payload
    text = _as_text(reply_raw.get("text") or reply_raw.get("content") or reply_raw.get("message"), 2000)
    if not text:
        raise ClassroomSchemaError("对话回复缺少 reply.text")
    speaker = _as_text(reply_raw.get("speaker") or reply_raw.get("role"), 30) or "teacher"
    if speaker not in _TUTOR_SPEAKERS:
        speaker = "teacher"
    actions, action_warnings = normalize_actions(reply_raw.get("actions"), element_ids=element_ids, max_actions=8)
    warnings.extend(action_warnings)
    result = {"reply": {"speaker": speaker, "text": text, "actions": actions}}
    hint_raw = payload.get("scene_hint") or payload.get("sceneHint")
    if isinstance(hint_raw, dict):
        hint = {}
        raw_index = hint_raw.get("index")
        if raw_index is None:
            raw_index = hint_raw.get("scene_index")
        index = _as_number(raw_index)
        if index is not None:
            value = int(index)
            if scene_count and 0 <= value < scene_count:
                hint["index"] = value
            elif not scene_count:
                hint["index"] = max(0, value)
        scene_id = _as_text(hint_raw.get("scene_id") or hint_raw.get("sceneId"), 40)
        if scene_id:
            hint["scene_id"] = scene_id
        reason = _as_text(hint_raw.get("reason"), 200)
        if reason:
            hint["reason"] = reason
        if hint:
            result["scene_hint"] = hint
    return result, warnings


__all__ = [
    "ACTION_TYPES", "ALL_SCENE_TYPES", "CANVAS_HEIGHT", "CANVAS_WIDTH", "ClassroomSchemaError",
    "COURSE_STATUSES", "DEFAULT_QUIZ_POINTS", "DEFAULT_SCENE_COUNT", "DEMO_ARTIFACT_ID_RE",
    "DEMO_ARTIFACT_KINDS", "DEMO_ARTIFACT_KIND_LABELS", "DEMO_PNG_DATA_PREFIX", "DEPTHS",
    "ELEMENT_TYPES", "MANUAL_SCENE_TYPES", "MAX_ACTIONS", "MAX_DEMO_ASSUMPTIONS",
    "MAX_DEMO_ASSUMPTION_CHARS", "MAX_DEMO_CONTENT_BYTES", "MAX_DEMO_DATA_BYTES",
    "MAX_DEMO_PNG_BYTES", "MAX_DEMO_REVISION", "MAX_DEMO_SOURCE_NOTE_CHARS",
    "MAX_DEMO_STEP_COUNT", "MAX_DEMO_SVG_CHARS", "MAX_ELEMENTS", "MAX_HTML_BYTES",
    "MAX_INTERACTIVE3D_HTML_BYTES", "MAX_INTERACTIVE_HTML_BYTES", "MAX_QUESTIONS",
    "MAX_SCENE_COUNT", "MIN_SCENE_COUNT", "MODES", "SCENE_STATUSES", "SCENE_TYPES",
    "SCENE_TYPE_LABELS", "TEXT_HEIGHT_TABLE", "WIDGET_LABELS", "WIDGET_TYPES",
    "clamp_scene_count", "html_to_plain", "interactive_html_limit",
    "normalize_actions", "normalize_demo_content", "normalize_depth",
    "normalize_interactive_content", "normalize_interactive_html", "normalize_outline",
    "normalize_pbl_content", "normalize_quiz_content", "normalize_scene_content",
    "normalize_scene_type", "normalize_scene_types", "normalize_slide_content",
    "normalize_tutor_reply", "normalize_widget", "parse_json_payload",
    "placeholder_interactive_html", "placeholder_pbl_content", "sanitize_text_html",
    "slide_element_ids", "speech_text_of", "text_height_for",
]
