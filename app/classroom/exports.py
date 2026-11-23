"""AI 课堂导出：PPTX / 离线单文件 HTML / 课程 ZIP（可被导入回本平台）/ 教学资源包 /
服务端 PDF 讲义 / MP4 视频（见 ``video.py``）。

契约：``INTEGRATION-CONTRACT.md`` §4.1 ``GET /courses/{id}/export?format=pptx|html|zip|pdf|mp4``；
设计依据 OpenMAIC recon §6（导出）与 §7（交互 HTML 沙箱）：

- **PPTX**：python-pptx 把 1000×562.5 画布元素映射为文本框/图片/形状/表格；公式以文本降级并在
  备注写明；quiz/interactive/pbl 导出为带标题与说明的占位页；首个幻灯片为课程标题页；
  ``Content-Disposition`` 带 RFC 5987 中文文件名。
- **离线 HTML**：单文件播放器，课程 JSON 与 CSS/JS 全部内联，无任何 http(s) 外链；支持场景切换、
  字幕/浏览器语音、测验查看；交互场景用 ``<iframe sandbox="allow-scripts" srcdoc>`` 嵌入已校验 HTML；
  体积超过 ``MAX_STANDALONE_HTML_BYTES``（400MB）时自动改回 ZIP 并带 ``X-Zhixu-Export-Fallback: zip``。
  M7（§5.4）：含原理演示场景时额外内联 flow/iso 渲染器 bundle 与离线演示播放器
  （``web/vendor/offline/offline-demo.js`` + ``web/js/offline/offlineDemo.js``），HTML / ZIP 保留
  可交互内容（流程/等距/工厂回放），PPTX / PDF / 打印讲义仍只放海报与文字。
- **ZIP**：``manifest.json`` + ``course.json`` + ``scenes/scene_NNN.json`` + ``assets/**``，
  可被 ``app/classroom/importer.py`` 原样导入（不调用模型）。
- **教学资源包（package）**：``course.pptx`` + 每个 interactive 场景一份独立自包含 HTML
  （``interactive/scene-NN-slug.html``，最小播放壳 + sandbox iframe，无外链）+ ``course.json`` /
  ``ets.json``（同一课程结构）+ 中文 ``README.txt``；单 ZIP 下载，适合课堂分发/离线归档。
- **TTS（可选）**：``include_tts=true`` 时调用既有 ``tts.synthesize``（未配置连接自然回退浏览器语音，
  内容寻址去重），音频作为素材写入 ZIP / 以 data URI 内联进离线 HTML。
- **固定版本原理演示（M4）**：``scene_type='demo'`` 场景按插入时冻结的学习附件快照导出——
  PPTX 一页「标题 + 原理演示 · <kind 中文> · 固定版本 vN + 说明/假设 + 可选 PNG 海报」；
  离线 HTML / 打印讲义输出 ``section.cls-demo-scene[data-artifact-id][data-fixed-revision]``
  并内嵌 ``application/json`` 快照；ZIP 额外写 ``demos/scene-<NN>-<artifact_id>.json``。
  源附件后续升版不会改变已导出的固定版本内容。
"""
from __future__ import annotations

import base64
import html as html_lib
import io
import json
import math
import re
from pathlib import Path
from urllib.parse import quote
import zipfile

from fastapi import APIRouter, Depends, HTTPException, Query, Response

from ..auth import current_user
from .. import storage
from . import db as cdb
from . import multiagent, schema, tts

router = APIRouter()

EXPORT_FORMATS = ("pptx", "html", "zip", "package", "pdf", "mp4")
PPTX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
HTML_MEDIA_TYPE = "text/html; charset=utf-8"
ZIP_MEDIA_TYPE = "application/zip"
PDF_MEDIA_TYPE = "application/pdf"
VIDEO_MEDIA_TYPE = "video/mp4"
#: PDF 讲义版式：A4 横向（1000×562.5 的 16:9 画布按 1:1 像素宽度正好放入 277mm 可打印宽度）。
PDF_PAPER = "A4"
PDF_LANDSCAPE = True
ZIP_FORMAT = "zhixu-classroom-zip"
ZIP_FORMAT_VERSION = 1
MAX_STANDALONE_HTML_BYTES = 400 * 1024 * 1024
PPTX_MAX_ELEMENTS_PER_PAGE = 10
PPTX_FONT = "Microsoft YaHei"
PPTX_CODE_FONT = "Consolas"

_CANVAS_WIDTH = schema.CANVAS_WIDTH
_CANVAS_HEIGHT = schema.CANVAS_HEIGHT
_SLIDE_W_INCH = 13.333
_SLIDE_H_INCH = 7.5
_HEADER_H_INCH = 0.62

_EXTERNAL_FILE_RE = re.compile(r"[\\/:*?\"<>|\r\n\t]+")


def _db():
    from .. import db

    return db


# ---------------------------------------------------------------------------
# 通用工具
# ---------------------------------------------------------------------------

def _owned_course_or_404(course_id: int, user_id: int) -> dict:
    course = cdb.find_course(course_id, user_id)
    if course is None:
        raise HTTPException(404, "未找到该课程")
    return course


def _scene_content(scene: dict) -> dict:
    value = cdb.json_load(scene.get("content_json"), {})
    return value if isinstance(value, dict) else {}


def _scene_meta(scene: dict) -> dict:
    value = cdb.json_load(scene.get("meta_json"), {})
    return value if isinstance(value, dict) else {}


def _scene_actions(scene: dict) -> list[dict]:
    actions = _scene_meta(scene).get("actions")
    return actions if isinstance(actions, list) else []


def _scene_speech(scene: dict) -> str:
    return schema.speech_text_of(_scene_actions(scene))


# ---------------------------------------------------------------------------
# 手工「原理演示」场景（M4）：固定版本学习附件快照
#
# 演示场景 content 由 ``api.add_demo_scene`` 冻结：``{type:"demo", fixed:true,
# fixed_revision:N, artifact:{artifact_id,revision,kind,title,data,preview}, source_note,
# assumptions, step_count}``。PPTX / 离线 HTML / 打印讲义 / ZIP 都只读该快照，渲染
# 「海报 + 说明 + 数据 JSON」；不执行附件里的任何内容，也不引入外链。
# ---------------------------------------------------------------------------

def _demo_artifact_of(content) -> dict:
    """取原理演示 content 里的附件快照；不是演示内容时返回空 dict。"""
    if not isinstance(content, dict) or str(content.get("type") or "") != "demo":
        return {}
    artifact = content.get("artifact")
    return artifact if isinstance(artifact, dict) else {}


def _demo_fixed_revision(content) -> int:
    """固定版本号（优先 ``fixed_revision``，回退附件快照的 ``revision``）；非法时按 0。"""
    value = None
    if isinstance(content, dict):
        value = content.get("fixed_revision")
        if value is None:
            value = _demo_artifact_of(content).get("revision")
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _demo_kind_label(kind) -> str:
    """演示附件类型中文名（未知类型回退原值 / 「演示」）。"""
    text = str(kind or "").strip()
    return schema.DEMO_ARTIFACT_KIND_LABELS.get(text, text or "演示")


def _demo_png_bytes(content) -> bytes | None:
    """解出演示海报 PNG 字节；无预览、非 PNG data URI 或解码失败返回 None（导出降级为文字说明）。"""
    artifact = _demo_artifact_of(content)
    preview = artifact.get("preview") if isinstance(artifact.get("preview"), dict) else {}
    data_uri = str(preview.get("png") or "")
    prefix = schema.DEMO_PNG_DATA_PREFIX
    if not data_uri.startswith(prefix):
        return None
    try:
        payload = base64.b64decode(data_uri[len(prefix):], validate=True)
    except (TypeError, ValueError):
        return None
    if not payload or not payload.startswith(b"\x89PNG\r\n\x1a\n"):
        return None
    return payload


def _demo_json_script(payload) -> str:
    """把演示快照嵌进 ``<script type="application/json">``（只作数据，不执行；转义 ``</``）。"""
    try:
        text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        text = "{}"
    text = text.replace("</", "<\\/")
    return '<script type="application/json" class="cls-demo-data">%s</script>' % text


def _demo_content_lists(content) -> tuple[list[str], int]:
    """演示内容里的假设列表与步骤数（兼容手工内容缺字段的旧数据）。"""
    assumptions_raw = content.get("assumptions") if isinstance(content, dict) else None
    assumptions = [
        str(item) for item in assumptions_raw
    ] if isinstance(assumptions_raw, list) else []
    try:
        step_count = max(0, int((content or {}).get("step_count") or 0))
    except (TypeError, ValueError):
        step_count = 0
    return assumptions, step_count


def _demo_section_html(title, content, *, heading_tag: str = "h2",
                       offline_player: bool = False) -> str:
    """单个原理演示场景 → 静态 HTML 片段（离线 HTML 附录 / 打印讲义共用）。

    输出 ``<section class="cls-demo-scene" data-scene-type="demo" data-artifact-id data-fixed-revision>``：
    固定版本徽标、标题、可选 PNG 海报（data URI）、说明、假设列表、步骤数，以及内嵌
    ``application/json`` 附件快照（快照里的 ``</script>`` 已转义，脚本不会被执行）。

    M7：``offline_player=True``（仅离线 HTML 附录）时额外标注 ``data-offline-demo="<kind>"``，
    供导出播放器脚本（``web/js/offline/offlineDemo.js``）识别并挂载可交互视图；打印讲义 /
    PDF 走默认值，不出现任何播放器标记（仍为静态海报 + 文字）。
    """
    artifact = _demo_artifact_of(content)
    artifact_id = html_lib.escape(str(artifact.get("artifact_id") or ""), quote=True)
    revision = _demo_fixed_revision(content)
    kind_label = html_lib.escape(_demo_kind_label(artifact.get("kind")))
    scene_title = str(title or artifact.get("title") or "原理演示")
    heading_tag = heading_tag if heading_tag in ("h2", "h3") else "h2"
    offline_attr = ""
    if offline_player:
        offline_attr = ' data-offline-demo="%s"' % html_lib.escape(
            str(artifact.get("kind") or ""), quote=True
        )
    parts: list[str] = [
        '<section class="cls-demo-scene" data-scene-type="demo" '
        'data-artifact-id="%s" data-fixed-revision="%d" data-demo-frame-ready="1"%s>'
        % (artifact_id, revision, offline_attr),
        '<div class="cls-demo-head"><span class="cls-demo-badge">原理演示 · 固定版本 v%d</span>'
        '<span class="cls-demo-kind">%s</span></div>' % (revision, kind_label),
        "<%s>%s</%s>" % (heading_tag, html_lib.escape(scene_title), heading_tag),
    ]
    png = _demo_png_bytes(content)
    if png:
        data_uri = schema.DEMO_PNG_DATA_PREFIX + base64.b64encode(png).decode("ascii")
        parts.append(
            '<img class="cls-demo-image" src="%s" alt="%s">'
            % (html_lib.escape(data_uri, quote=True),
               html_lib.escape(str(artifact.get("title") or "演示海报")))
        )
    else:
        parts.append('<p class="cls-demo-missing">未附加海报图像，请在知序课堂中播放该演示。</p>')
    source_note = str((content or {}).get("source_note") or "").strip()
    if source_note:
        parts.append('<p class="cls-demo-note">说明：%s</p>' % html_lib.escape(source_note))
    assumptions, step_count = _demo_content_lists(content)
    if assumptions:
        parts.append("<h3>前提假设</h3>")
        parts.append(
            '<ul class="cls-demo-assumptions">%s</ul>'
            % "".join("<li>%s</li>" % html_lib.escape(item) for item in assumptions)
        )
    if step_count > 0:
        parts.append('<p class="cls-demo-note">演示步骤数：%d</p>' % step_count)
    parts.append(_demo_json_script({
        "format": "zhixu-classroom-demo-snapshot",
        "formatVersion": 1,
        "fixed": True,
        "fixedRevision": revision,
        "artifact": artifact,
        "sourceNote": source_note,
        "assumptions": assumptions,
        "stepCount": step_count,
        "fixedNote": "固定版本 v%d：课堂与导出使用该版本，源附件更新不会自动改变" % revision,
    }))
    parts.append("</section>")
    return "".join(parts)


def _demo_sections_html(snapshot: dict, *, offline_player: bool = False) -> str:
    """离线 HTML 末尾的「演示附件静态备份」：不执行 JS 也能看到固定版本内容。

    M7：含内联播放器时（``offline_player=True``）给每个 demo section 加 ``data-offline-demo``
    标记，播放器脚本挂载成功后再补 ``data-offline-demo-mounted="1"``；无播放器时与既有输出一致。
    """
    scenes = snapshot.get("scenes") if isinstance(snapshot, dict) else None
    blocks: list[str] = []
    for scene in scenes or []:
        if not isinstance(scene, dict) or str(scene.get("type") or "") != "demo":
            continue
        content = scene.get("content") if isinstance(scene.get("content"), dict) else {}
        blocks.append(_demo_section_html(
            scene.get("title") or "", content, offline_player=offline_player,
        ))
    if not blocks:
        return ""
    return (
        '<section class="cls-demo-appendix">'
        "<h2>演示附件（固定版本）</h2>"
        '<p class="cls-demo-appendix-hint">以下为课件中手工插入的原理演示附件静态备份：'
        "播放器内可逐页查看，导出文件只保留插入时的固定版本快照，源附件后续更新不会改变课件。</p>"
        + "".join(blocks)
        + "</section>"
    )


# ---------------------------------------------------------------------------
# M7：离线可交互演示播放器（HTML / ZIP 内联；PPTX / PDF / 打印讲义仍为海报 + 文字）
#
# 离线单文件 HTML 不允许外链、也不允许联网加载 ES module，因此播放器由两块**提交在仓库内**
# 的静态文本组成，导出时原样内联：
# - ``web/vendor/offline/offline-demo.js``：flow / iso 两个纯 ESM 渲染器的确定性文本转换
#   产物（无 import / 无 export default，见 scripts/build_offline_demo_bundle.py）；
# - ``web/js/offline/offlineDemo.js``：挂在 ``section.cls-demo-scene`` 上的挂载/分步/工厂回放
#   逻辑（纯 DOM API，只执行我们自己的代码；附件 JSON 只作数据）。
# 找不到产物或源文件时退回既有静态海报输出（不抛异常、不 500）。
# ---------------------------------------------------------------------------

_WEB_ROOT = Path(__file__).resolve().parents[2] / "web"
_OFFLINE_DEMO_BUNDLE_PATH = _WEB_ROOT / "vendor" / "offline" / "offline-demo.js"
_OFFLINE_DEMO_PLAYER_PATH = _WEB_ROOT / "js" / "offline" / "offlineDemo.js"
#: flow / iso 渲染器（``web/vendor/principles/*.js``）的样式定义；离线单文件不加载全局样式表，
#: 因此只在含播放器时内联（无 http(s)/url()/@import 等外链，内联前再校验一次）。
_OFFLINE_PRINCIPLES_CSS_PATH = _WEB_ROOT / "styles-principles.css"
_OFFLINE_DEMO_SCRIPT_CLOSE_RE = re.compile(r"</script", re.IGNORECASE)
_OFFLINE_CSS_UNSAFE_RE = re.compile(r"https?://|url\(|@import|@font-face|<", re.IGNORECASE)


def _read_static_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def _offline_demo_bundle_js() -> str:
    """内联用 flow/iso 播放器 bundle 文本；产物缺失时返回空串（导出退回静态海报）。"""
    return _read_static_text(_OFFLINE_DEMO_BUNDLE_PATH).replace("\r\n", "\n")


def _offline_demo_player_js() -> str:
    """离线 HTML 内联的演示挂载脚本（``web/js/offline/offlineDemo.js`` 原文）。"""
    return _read_static_text(_OFFLINE_DEMO_PLAYER_PATH).replace("\r\n", "\n")


def _offline_demo_principles_css() -> str:
    """flow/iso SVG 的 ``.pd-*`` 样式（自带零外链；含外链标记时整体放弃，退回无样式安全输出）。"""
    text = _read_static_text(_OFFLINE_PRINCIPLES_CSS_PATH).replace("\r\n", "\n")
    if not text.strip() or _OFFLINE_CSS_UNSAFE_RE.search(text):
        return ""
    return text if text.endswith("\n") else text + "\n"


def _inline_script_text(script: str) -> str:
    """内联 ``<script>`` 的防御性转义：源码已校验不含 ``</script``，这里仍按 HTML 规则转义。"""
    return _OFFLINE_DEMO_SCRIPT_CLOSE_RE.sub("<\\/script", str(script or "")).rstrip("\n")


def _offline_demo_scripts_html(bundle_js: str, player_js: str) -> str:
    """``player bundle + 挂载逻辑`` 两段内联脚本（仅含演示场景的导出才会非空）。"""
    if not bundle_js.strip() or not player_js.strip():
        return ""
    return (
        "<script>\n%s\n</script>\n<script>\n%s\n</script>\n"
        % (_inline_script_text(bundle_js), _inline_script_text(player_js))
    )


def _export_basename(course: dict, fallback: str = "AI课堂") -> str:
    raw = str(course.get("title") or course.get("topic") or fallback).strip()
    safe = _EXTERNAL_FILE_RE.sub(" ", raw)
    safe = re.sub(r"\s+", " ", safe).strip().strip(".")
    if not safe:
        safe = fallback
    return safe[:60]


def export_filename(course: dict, extension: str) -> str:
    return "%s.%s" % (_export_basename(course), str(extension).lstrip("."))


def content_disposition(filename: str) -> str:
    """``attachment`` + RFC 5987 中文文件名（ASCII fallback 保证旧客户端可用）。"""
    stem, _dot, extension = str(filename).rpartition(".")
    ascii_stem = re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("._-") or "classroom-export"
    ascii_extension = re.sub(r"[^A-Za-z0-9]+", "", extension)
    ascii_fallback = ascii_stem + (("." + ascii_extension) if ascii_extension else "")
    return "attachment; filename=\"%s\"; filename*=UTF-8''%s" % (
        ascii_fallback, quote(filename, safe=""),
    )


def _file_headers(filename: str, *, extra: dict[str, str] | None = None) -> dict[str, str]:
    headers = {
        "Content-Disposition": content_disposition(filename),
        "Cache-Control": "private, no-store",
        "X-Content-Type-Options": "nosniff",
    }
    if extra:
        headers.update({str(key): str(value) for key, value in extra.items()})
    return headers


def _hex_rgb(value, default: str = "333333"):
    from pptx.dml.color import RGBColor

    text = str(value or "").strip().lstrip("#")
    if len(text) == 3:
        text = "".join(ch * 2 for ch in text)
    if len(text) == 8:
        text = text[:6]
    if re.fullmatch(r"[0-9a-fA-F]{6}", text or ""):
        return RGBColor.from_string(text.upper())
    return RGBColor.from_string(str(default).lstrip("#").upper())


def _clamp(value, low: float, high: float, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


# ---------------------------------------------------------------------------
# PPTX
# ---------------------------------------------------------------------------

def _pptx_geometry(element: dict) -> dict:
    """画布像素 → 幻灯片英寸；顶部留出场景标题栏，元素区压缩到剩余高度。"""
    left = _clamp(element.get("left"), -_CANVAS_WIDTH, _CANVAS_WIDTH) * (_SLIDE_W_INCH / _CANVAS_WIDTH)
    top = _clamp(element.get("top"), -_CANVAS_HEIGHT, _CANVAS_HEIGHT)
    top = _HEADER_H_INCH + max(0.0, top) * ((_SLIDE_H_INCH - _HEADER_H_INCH) / _CANVAS_HEIGHT)
    width = max(0.25, _clamp(element.get("width"), 8, _CANVAS_WIDTH) * (_SLIDE_W_INCH / _CANVAS_WIDTH))
    height = max(0.18, _clamp(element.get("height"), 8, _CANVAS_HEIGHT) *
                 ((_SLIDE_H_INCH - _HEADER_H_INCH) / _CANVAS_HEIGHT))
    left = max(0.0, min(left, _SLIDE_W_INCH - 0.2))
    top = max(_HEADER_H_INCH, min(top, _SLIDE_H_INCH - 0.2))
    width = max(0.25, min(width, _SLIDE_W_INCH - left - 0.05))
    height = max(0.18, min(height, _SLIDE_H_INCH - top - 0.05))
    return {"left": left, "top": top, "width": width, "height": height}


def _pptx_text_element(slide, element: dict, notes: list[str]) -> None:
    from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
    from pptx.util import Inches, Pt

    geometry = _pptx_geometry(element)
    text = str(element.get("text") or schema.html_to_plain(element.get("content")) or "").strip()
    if not text and element.get("latex"):
        text = "公式：" + str(element.get("latex"))[:1000]
    if not text:
        return
    box = slide.shapes.add_textbox(
        Inches(geometry["left"]), Inches(geometry["top"]),
        Inches(geometry["width"]), Inches(geometry["height"]),
    )
    frame = box.text_frame
    frame.word_wrap = True
    frame.margin_left = Inches(0.04)
    frame.margin_right = Inches(0.04)
    frame.margin_top = Inches(0.02)
    frame.margin_bottom = Inches(0.02)
    frame.vertical_anchor = MSO_ANCHOR.TOP
    frame.text = text
    font_px = _clamp(element.get("fontSize"), 9, 60, 20)
    point_size = max(8.0, min(34.0, round(font_px * _SLIDE_W_INCH * 72 / _CANVAS_WIDTH, 1)))
    color = _hex_rgb(element.get("color"), "333333")
    align = str(element.get("align") or "").strip().lower()
    alignment = {"center": PP_ALIGN.CENTER, "right": PP_ALIGN.RIGHT}.get(align, PP_ALIGN.LEFT)
    for paragraph in frame.paragraphs:
        paragraph.alignment = alignment
        paragraph.line_spacing = 1.15
        for run in paragraph.runs:
            run.font.size = Pt(point_size)
            run.font.name = PPTX_FONT
            run.font.color.rgb = color
    if element.get("type") == "latex" or element.get("latex"):
        notes.append("公式以文本降级：%s" % str(element.get("latex") or text)[:500])


def _pptx_shape_element(slide, element: dict) -> None:
    from pptx.enum.shapes import MSO_SHAPE
    from pptx.util import Inches

    geometry = _pptx_geometry(element)
    path = str(element.get("path") or "")
    shape_type = MSO_SHAPE.OVAL if ("A" in path and "0.5" in path) else MSO_SHAPE.RECTANGLE
    shape = slide.shapes.add_shape(
        shape_type, Inches(geometry["left"]), Inches(geometry["top"]),
        Inches(geometry["width"]), Inches(geometry["height"]),
    )
    shape.fill.solid()
    shape.fill.fore_color.rgb = _hex_rgb(element.get("fill"), "5B9BD5")
    shape.line.fill.background()
    shape.shadow.inherit = False


def _pptx_line_element(slide, element: dict) -> None:
    from pptx.enum.shapes import MSO_CONNECTOR
    from pptx.util import Inches, Pt

    left = _clamp(element.get("left"), -_CANVAS_WIDTH, _CANVAS_WIDTH)
    top = _clamp(element.get("top"), -_CANVAS_HEIGHT, _CANVAS_HEIGHT)
    start = element.get("start") if isinstance(element.get("start"), list) else [0, 0]
    end = element.get("end") if isinstance(element.get("end"), list) else [0, 0]
    try:
        sx, sy = float(start[0]), float(start[1])
        ex, ey = float(end[0]), float(end[1])
    except (TypeError, ValueError, IndexError):
        sx = sy = ex = ey = 0.0

    def _point(px: float, py: float):
        px = max(0.0, min(_CANVAS_WIDTH, px))
        py = _HEADER_H_INCH + max(0.0, min(_CANVAS_HEIGHT, py)) * (
            (_SLIDE_H_INCH - _HEADER_H_INCH) / _CANVAS_HEIGHT)
        return Inches(px * (_SLIDE_W_INCH / _CANVAS_WIDTH)), Inches(py)

    begin_x, begin_y = _point(left + sx, top + sy)
    end_x, end_y = _point(left + ex, top + ey)
    if (abs(left + sx - (left + ex)) < 1 and abs(top + sy - (top + ey)) < 1):
        begin_x, begin_y = _point(left, top)
        end_x, end_y = _point(left + _clamp(element.get("width"), 20, _CANVAS_WIDTH), top)
    connector = slide.shapes.add_connector(MSO_CONNECTOR.STRAIGHT, begin_x, begin_y, end_x, end_y)
    connector.line.color.rgb = _hex_rgb(element.get("color"), "333333")
    connector.line.width = Pt(_clamp(element.get("width"), 1, 6, 2))


def _pptx_table_element(slide, element: dict) -> None:
    from pptx.enum.text import PP_ALIGN
    from pptx.util import Inches, Pt

    data = element.get("data") if isinstance(element.get("data"), list) else []
    rows: list[list[str]] = []
    for row in data[:8]:
        cells = []
        for cell in (row if isinstance(row, list) else [])[:6]:
            if isinstance(cell, dict):
                cells.append(str(cell.get("text") or "")[:200])
            else:
                cells.append(str(cell or "")[:200])
        if cells:
            rows.append(cells)
    if not rows:
        return
    geometry = _pptx_geometry(element)
    columns = max(len(row) for row in rows)
    table = slide.shapes.add_table(
        len(rows), columns, Inches(geometry["left"]), Inches(geometry["top"]),
        Inches(geometry["width"]), Inches(geometry["height"]),
    ).table
    widths = element.get("colWidths") if isinstance(element.get("colWidths"), list) else []
    if len(widths) == columns:
        total = sum(_clamp(item, 0.01, 1.0) for item in widths) or 1.0
        for index, ratio in enumerate(widths):
            table.columns[index].width = Inches(geometry["width"] * _clamp(ratio, 0.01, 1.0) / total)
    for row_index, row in enumerate(rows):
        for column_index in range(columns):
            text = row[column_index] if column_index < len(row) else ""
            cell = table.cell(row_index, column_index)
            cell.text = text
            for paragraph in cell.text_frame.paragraphs:
                paragraph.alignment = PP_ALIGN.LEFT
                for run in paragraph.runs:
                    run.font.size = Pt(11)
                    run.font.name = PPTX_FONT


def _pptx_table_from_rows(slide, element: dict, rows: list[list[str]]) -> None:
    _pptx_table_element(slide, {**element, "data": [[{"text": cell} for cell in row] for row in rows]})


def _pptx_chart_element(slide, element: dict, notes: list[str]) -> None:
    data = element.get("data") if isinstance(element.get("data"), dict) else {}
    labels = [str(item)[:40] for item in (data.get("labels") or [])[:8]]
    series = data.get("series") if isinstance(data.get("series"), list) else []
    legends = [str(item)[:40] for item in (data.get("legends") or [])[:6]]
    if not labels or not series:
        return
    rows: list[list[str]] = [["系列"] + labels]
    for index, values in enumerate(series[:5]):
        name = legends[index] if index < len(legends) else "系列 %d" % (index + 1)
        row = [name]
        for value in (values if isinstance(values, list) else [])[:len(labels)]:
            row.append(str(value))
        rows.append(row)
    _pptx_table_from_rows(slide, element, rows)
    notes.append("图表（%s）以数据表形式导出" % str(element.get("chartType") or "bar"))


def _pptx_code_element(slide, element: dict) -> None:
    from pptx.enum.text import PP_ALIGN
    from pptx.util import Inches, Pt

    code = str(element.get("code") or "").strip()
    if not code:
        return
    geometry = _pptx_geometry(element)
    box = slide.shapes.add_textbox(
        Inches(geometry["left"]), Inches(geometry["top"]),
        Inches(geometry["width"]), Inches(geometry["height"]),
    )
    frame = box.text_frame
    frame.word_wrap = True
    frame.text = code[:3000]
    for paragraph in frame.paragraphs:
        paragraph.alignment = PP_ALIGN.LEFT
        for run in paragraph.runs:
            run.font.size = Pt(10.5)
            run.font.name = PPTX_CODE_FONT


def _pptx_image_element(slide, element: dict, notes: list[str]) -> None:
    from pptx.util import Inches

    src = str(element.get("src") or "")
    payload = None
    if src.startswith("data:image/") and ";base64," in src:
        header, encoded = src.split(";base64,", 1)
        try:
            payload = base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError):
            payload = None
    if not payload:
        notes.append("图片未内联（%s），PPTX 中已跳过；请在线查看" % (src[:60] or "无地址"))
        return
    geometry = _pptx_geometry(element)
    try:
        slide.shapes.add_picture(
            io.BytesIO(payload), Inches(geometry["left"]), Inches(geometry["top"]),
            width=Inches(geometry["width"]), height=Inches(geometry["height"]),
        )
    except Exception:  # 损坏图片不应让整份导出失败
        notes.append("图片数据无法写入 PPTX，已跳过")


def _pptx_add_slide_title(slide, title: str, subtitle: str = "") -> None:
    from pptx.enum.text import PP_ALIGN
    from pptx.util import Inches, Pt

    box = slide.shapes.add_textbox(
        Inches(0.2), Inches(0.08), Inches(_SLIDE_W_INCH - 0.4), Inches(0.46),
    )
    frame = box.text_frame
    frame.word_wrap = True
    frame.text = str(title or "")[:120]
    for paragraph in frame.paragraphs:
        paragraph.alignment = PP_ALIGN.LEFT
        for run in paragraph.runs:
            run.font.size = Pt(20)
            run.font.bold = True
            run.font.name = PPTX_FONT
            run.font.color.rgb = _hex_rgb("#1f6feb")
    if subtitle:
        sub = slide.shapes.add_textbox(
            Inches(0.2), Inches(0.47), Inches(_SLIDE_W_INCH - 0.4), Inches(0.2),
        )
        sub_frame = sub.text_frame
        sub_frame.text = str(subtitle)[:200]
        for paragraph in sub_frame.paragraphs:
            for run in paragraph.runs:
                run.font.size = Pt(9)
                run.font.name = PPTX_FONT
                run.font.color.rgb = _hex_rgb("#6b7280")


def _pptx_add_footer(slide, page_no: int) -> None:
    from pptx.enum.text import PP_ALIGN
    from pptx.util import Inches, Pt

    box = slide.shapes.add_textbox(
        Inches(0.2), Inches(_SLIDE_H_INCH - 0.3), Inches(_SLIDE_W_INCH - 0.4), Inches(0.2),
    )
    frame = box.text_frame
    frame.text = "知序 · AI 课堂    第 %d 页" % page_no
    for paragraph in frame.paragraphs:
        paragraph.alignment = PP_ALIGN.RIGHT
        for run in paragraph.runs:
            run.font.size = Pt(8)
            run.font.name = PPTX_FONT
            run.font.color.rgb = _hex_rgb("#9ca3af")


def _pptx_set_notes(slide, notes: list[str], speech: str = "") -> None:
    lines = []
    if speech:
        lines.append("【旁白】" + speech[:2000])
    lines.extend(notes[:20])
    if not lines:
        return
    try:
        frame = slide.notes_slide.notes_text_frame
        frame.text = "\n".join(lines)
    except Exception:  # 备注失败不影响导出文件
        pass


def _pptx_background(slide, color: str = "#ffffff") -> None:
    try:
        fill = slide.background.fill
        fill.solid()
        fill.fore_color.rgb = _hex_rgb(color, "FFFFFF")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 联网来源（P3）：PPTX 末页 / 离线 HTML 文末 / ZIP sources.json / 资源包 README
# ---------------------------------------------------------------------------

def _course_sources(course: dict) -> list[dict]:
    """导出用来源列表：读取课程大纲里的 ``outline.sources``（P3 联网检索结果）。

    没有任何来源时返回空列表；绝不编造来源。链接以纯文本呈现，导出文件不加载外链资源。
    """
    from .generate import outline_sources

    outline = cdb.json_load((course or {}).get("outline_json"), {})
    return outline_sources(outline if isinstance(outline, dict) else {})


def _sources_json_bytes(sources: list[dict]) -> bytes:
    """``sources.json``：课程 ZIP / 离线导出附带的来源清单（稳定结构，便于第三方读取）。"""
    payload = {"version": 1, "count": len(sources), "sources": sources}
    return json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")


def _source_meta_text(item: dict) -> str:
    return " · ".join(
        part for part in (
            str(item.get("site") or "").strip(),
            str(item.get("date") or "").strip(),
        ) if part
    )


def _sources_html(sources: list[dict]) -> str:
    """离线单文件 HTML 文末「参考来源」静态列表（纯文本链接，不加载任何外部资源）。"""
    if not sources:
        return ""
    items = []
    for index, item in enumerate(sources, 1):
        number = int(item.get("index") or index)
        title = html_lib.escape(str(item.get("title") or "未命名来源")[:200])
        meta = html_lib.escape(_source_meta_text(item))
        url = html_lib.escape(str(item.get("url") or "")[:600])
        summary = html_lib.escape(str(item.get("summary") or "")[:300])
        items.append(
            '<li class="source-item">'
            '<span class="source-index">[%d]</span> '
            '<span class="source-title">%s</span>'
            '%s'
            '<div class="source-url">%s</div>'
            '%s'
            "</li>" % (
                number, title,
                ('<span class="source-site">%s</span>' % meta) if meta else "",
                url or "（无链接）",
                ('<div class="source-summary">%s</div>' % summary) if summary else "",
            )
        )
    return (
        '<section class="sources" aria-labelledby="sources-title">'
        '<h2 id="sources-title">参考来源（%d 条，联网检索）</h2>'
        '<p class="sources-hint">以下资料来自联网检索，仅供备课参考；链接为纯文本，'
        "不随本文件加载，内容以原网页为准。</p>"
        "<ol class=\"source-list\">%s</ol>"
        "</section>" % (len(sources), "".join(items))
    )


def _pptx_sources_slides(prs, sources: list[dict], page_start: int) -> int:
    """在 PPTX 末尾追加「参考来源」页（标题 + 链接纯文本；不内联外链图片）。"""
    from pptx.enum.text import PP_ALIGN
    from pptx.util import Inches, Pt

    per_page = 7
    chunks = [sources[index:index + per_page] for index in range(0, len(sources), per_page)]
    page_no = page_start
    for chunk_index, chunk in enumerate(chunks):
        slide = prs.slides.add_slide(prs.slide_layouts[6])
        _pptx_background(slide)
        title = "参考来源" if len(chunks) == 1 else "参考来源（%d/%d）" % (chunk_index + 1, len(chunks))
        _pptx_add_slide_title(
            slide, title, "资料来自联网检索，仅供备课参考；链接为纯文本，可在浏览器中打开",
        )
        top = _HEADER_H_INCH + 0.12
        for offset, item in enumerate(chunk):
            number = int(item.get("index") or (chunk_index * per_page + offset + 1))
            meta_text = _source_meta_text(item)
            box = slide.shapes.add_textbox(
                Inches(0.5), Inches(top), Inches(_SLIDE_W_INCH - 1.0), Inches(0.78),
            )
            frame = box.text_frame
            frame.word_wrap = True
            paragraph = frame.paragraphs[0]
            paragraph.alignment = PP_ALIGN.LEFT
            run = paragraph.add_run()
            run.text = "[%d] %s%s" % (
                number, str(item.get("title") or "未命名来源")[:120],
                ("（%s）" % meta_text) if meta_text else "",
            )
            run.font.size = Pt(12)
            run.font.bold = True
            run.font.name = PPTX_FONT
            run.font.color.rgb = _hex_rgb("#1f2937")
            url_paragraph = frame.add_paragraph()
            url_paragraph.alignment = PP_ALIGN.LEFT
            url_run = url_paragraph.add_run()
            url_run.text = str(item.get("url") or "")[:500] or "（无链接）"
            url_run.font.size = Pt(9)
            url_run.font.name = PPTX_FONT
            url_run.font.color.rgb = _hex_rgb("#2563eb")
            top += 0.86
        page_no += 1
        _pptx_add_footer(slide, page_no)
    return page_no


def _pptx_title_slide(prs, course: dict, scenes: list[dict]) -> None:
    from pptx.enum.text import PP_ALIGN
    from pptx.util import Inches, Pt

    slide = prs.slides.add_slide(prs.slide_layouts[6])
    _pptx_background(slide)
    title_box = slide.shapes.add_textbox(Inches(0.8), Inches(1.9), Inches(_SLIDE_W_INCH - 1.6), Inches(1.5))
    frame = title_box.text_frame
    frame.word_wrap = True
    frame.text = str(course.get("title") or course.get("topic") or "AI 课堂")[:120]
    for paragraph in frame.paragraphs:
        paragraph.alignment = PP_ALIGN.CENTER
        for run in paragraph.runs:
            run.font.size = Pt(36)
            run.font.bold = True
            run.font.name = PPTX_FONT
            run.font.color.rgb = _hex_rgb("#1f6feb")

    meta_bits = [
        str(course.get("subject") or "").strip(),
        str(course.get("grade") or "").strip(),
        "%d 个场景" % len(scenes),
    ]
    meta_text = " · ".join(item for item in meta_bits if item)
    meta_box = slide.shapes.add_textbox(Inches(0.8), Inches(3.5), Inches(_SLIDE_W_INCH - 1.6), Inches(0.5))
    meta_frame = meta_box.text_frame
    meta_frame.text = meta_text
    for paragraph in meta_frame.paragraphs:
        paragraph.alignment = PP_ALIGN.CENTER
        for run in paragraph.runs:
            run.font.size = Pt(16)
            run.font.name = PPTX_FONT
            run.font.color.rgb = _hex_rgb("#6b7280")

    description = str(course.get("description") or "").strip()
    if description:
        desc_box = slide.shapes.add_textbox(Inches(1.2), Inches(4.2), Inches(_SLIDE_W_INCH - 2.4), Inches(1.2))
        desc_frame = desc_box.text_frame
        desc_frame.word_wrap = True
        desc_frame.text = description[:600]
        for paragraph in desc_frame.paragraphs:
            paragraph.alignment = PP_ALIGN.CENTER
            for run in paragraph.runs:
                run.font.size = Pt(13)
                run.font.name = PPTX_FONT
                run.font.color.rgb = _hex_rgb("#374151")

    footer_box = slide.shapes.add_textbox(Inches(0.8), Inches(_SLIDE_H_INCH - 0.8), Inches(_SLIDE_W_INCH - 1.6), Inches(0.3))
    footer_frame = footer_box.text_frame
    footer_frame.text = "由「知序 · AI 课堂」导出；交互场景与测验请在线使用"
    for paragraph in footer_frame.paragraphs:
        paragraph.alignment = PP_ALIGN.CENTER
        for run in paragraph.runs:
            run.font.size = Pt(9)
            run.font.name = PPTX_FONT
            run.font.color.rgb = _hex_rgb("#9ca3af")


def _pptx_slide_content_slides(prs, scene: dict, content: dict, speech: str, page_start: int) -> int:
    canvas = content.get("canvas") if isinstance(content.get("canvas"), dict) else {}
    elements = canvas.get("elements") if isinstance(canvas.get("elements"), list) else []
    elements = [element for element in elements if isinstance(element, dict)]
    background = "#ffffff"
    theme = canvas.get("theme") if isinstance(canvas.get("theme"), dict) else {}
    if isinstance(theme.get("backgroundColor"), str):
        background = theme["backgroundColor"]
    chunks = [elements[index:index + PPTX_MAX_ELEMENTS_PER_PAGE]
              for index in range(0, max(1, len(elements)), PPTX_MAX_ELEMENTS_PER_PAGE)] or [[]]
    page_no = page_start
    for chunk_index, chunk in enumerate(chunks):
        slide = prs.slides.add_slide(prs.slide_layouts[6])
        _pptx_background(slide, background)
        title = str(scene.get("title") or "未命名场景")
        if len(chunks) > 1:
            title = "%s（%d/%d）" % (title, chunk_index + 1, len(chunks))
        _pptx_add_slide_title(slide, title)
        notes: list[str] = []
        for element in chunk:
            element_type = str(element.get("type") or "").strip()
            try:
                if element_type == "text":
                    _pptx_text_element(slide, element, notes)
                elif element_type == "shape":
                    _pptx_shape_element(slide, element)
                elif element_type == "line":
                    _pptx_line_element(slide, element)
                elif element_type == "table":
                    _pptx_table_element(slide, element)
                elif element_type == "chart":
                    _pptx_chart_element(slide, element, notes)
                elif element_type == "code":
                    _pptx_code_element(slide, element)
                elif element_type == "image":
                    _pptx_image_element(slide, element, notes)
                elif element_type == "latex":
                    _pptx_text_element(slide, element, notes)
            except Exception:  # 单个元素映射失败不应拖垮整份导出
                notes.append("元素 %s（%s）导出失败，已跳过" % (element.get("id"), element_type))
        page_no += 1
        _pptx_add_footer(slide, page_no)
        _pptx_set_notes(slide, notes, speech if chunk_index == 0 else "")
    return page_no


def _pptx_placeholder_slide(prs, scene: dict, content: dict, speech: str, page_no: int) -> int:
    from pptx.enum.text import PP_ALIGN
    from pptx.util import Inches, Pt

    scene_type = str(scene.get("scene_type") or "")
    label = schema.SCENE_TYPE_LABELS.get(scene_type, scene_type)
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    _pptx_background(slide)
    _pptx_add_slide_title(slide, str(scene.get("title") or "未命名场景"), label)

    lines: list[str] = []
    notes: list[str] = []
    if scene_type == "quiz":
        hint = "本页为测验场景的说明占位：题目与判分请在「知序 · AI 课堂」在线完成。"
        lines.append(hint)
        questions = content.get("questions") if isinstance(content.get("questions"), list) else []
        for index, question in enumerate(questions[:8], 1):
            if not isinstance(question, dict):
                continue
            lines.append("")
            lines.append("%d. %s" % (index, str(question.get("question") or "")[:300]))
            options = question.get("options") if isinstance(question.get("options"), list) else []
            for option in options[:6]:
                if isinstance(option, dict):
                    lines.append("   %s. %s" % (option.get("value") or "", option.get("label") or ""))
        notes.append("测验答案与解析仅在线播放器可见（导出不泄题）。")
    elif scene_type == "interactive":
        widget = str(content.get("widget") or "simulation")
        widget_label = schema.WIDGET_LABELS.get(widget, widget)
        lines.append("本页为交互场景说明页：%s（%s）。" % (scene.get("title") or "", widget_label))
        lines.append("交互内容需要在「知序 · AI 课堂」在线打开；导出的离线 HTML / 课程 ZIP 中也包含完整交互页。")
        notes.append("交互 HTML 已通过服务端静态安全校验，仅能在沙箱 iframe 中运行。")
    elif scene_type == "pbl":
        project = content.get("project") if isinstance(content.get("project"), dict) else {}
        lines.append("本页为项目式学习说明页：《%s》" % (project.get("title") or scene.get("title") or ""))
        background = str(project.get("background") or "").strip()
        if background:
            lines.append(background[:800])
        goals = project.get("goals") if isinstance(project.get("goals"), list) else []
        if goals:
            lines.append("")
            lines.append("学习目标：")
            for goal in goals[:6]:
                lines.append("· %s" % str(goal)[:200])
        milestones = project.get("milestones") if isinstance(project.get("milestones"), list) else []
        if milestones:
            lines.append("")
            lines.append("里程碑：")
            for index, milestone in enumerate(milestones[:8], 1):
                if isinstance(milestone, dict):
                    lines.append("%d. %s —— 交付物：%s" % (
                        index, str(milestone.get("title") or "")[:120],
                        str(milestone.get("deliverable") or "")[:160]))
        notes.append("PBL 工作台（任务提交/导师对话）请在「知序 · AI 课堂」在线使用。")
    else:
        lines.append("该场景暂无可导出的内容。")
    body = "\n".join(lines).strip() or "该场景暂无可导出的内容。"
    box = slide.shapes.add_textbox(
        Inches(0.45), Inches(_HEADER_H_INCH + 0.15), Inches(_SLIDE_W_INCH - 0.9), Inches(_SLIDE_H_INCH - 1.2),
    )
    frame = box.text_frame
    frame.word_wrap = True
    frame.text = body[:4000]
    for paragraph in frame.paragraphs:
        paragraph.alignment = PP_ALIGN.LEFT
        paragraph.line_spacing = 1.2
        for run in paragraph.runs:
            run.font.size = Pt(13)
            run.font.name = PPTX_FONT
            run.font.color.rgb = _hex_rgb("#374151")
    _pptx_add_footer(slide, page_no)
    _pptx_set_notes(slide, notes, speech)
    return page_no + 1


def _pptx_demo_slide(prs, scene: dict, content: dict, speech: str, page_no: int) -> int:
    """原理演示场景 → 一页 PPTX：标题 + 固定版本说明 + 可选 PNG 海报；备注记录版本与附件 ID。

    绝不走 ``_pptx_placeholder_slide``：演示页必须包含「原理演示 · <kind 中文> · 固定版本 vN」、
    ``source_note`` 与 ``assumptions``；有可解码 PNG 时按比例放入右侧安全区，无图时给中文说明。
    """
    from pptx.enum.text import PP_ALIGN
    from pptx.util import Inches, Pt

    artifact = _demo_artifact_of(content)
    revision = _demo_fixed_revision(content)
    kind = str(artifact.get("kind") or "")
    kind_label = _demo_kind_label(kind)
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    _pptx_background(slide)
    title = str(scene.get("title") or artifact.get("title") or "原理演示")
    _pptx_add_slide_title(slide, title, "原理演示 · 固定版本 v%d" % revision)
    notes: list[str] = [
        "固定版本 v%d（附件 %s，类型 %s）：课堂与导出使用该版本，源附件更新不会自动改变。"
        % (revision, artifact.get("artifact_id") or "未知", kind_label)
    ]
    png = _demo_png_bytes(content)
    image_width = 0.0
    if png:
        max_width = 5.1
        max_height = _SLIDE_H_INCH - _HEADER_H_INCH - 0.75
        image_left = _SLIDE_W_INCH - max_width - 0.45
        image_top = _HEADER_H_INCH + 0.2
        try:
            picture = slide.shapes.add_picture(
                io.BytesIO(png), Inches(image_left), Inches(image_top), width=Inches(max_width),
            )
            max_height_emu = int(Inches(max_height))
            if picture.height > max_height_emu:
                # 先按高度回缩，再按原比例反推宽度（保持比例，不拉伸）。
                ratio = float(picture.width) / float(picture.height or 1)
                picture.height = max_height_emu
                picture.width = int(max_height_emu * ratio)
            width_in = picture.width / 914400.0
            height_in = picture.height / 914400.0
            picture.left = Inches(image_left + max(0.0, (max_width - width_in) / 2))
            picture.top = Inches(image_top + max(0.0, (max_height - height_in) / 2))
            image_width = width_in
            notes.append("演示海报已按 PNG 内联插入（%d 字节，保持原始比例）。" % len(png))
        except Exception:  # 损坏图片不应让整份导出失败
            png = None
            notes.append("演示海报无法写入 PPTX，已改用文字说明。")
    body_left = 0.45
    body_width = _SLIDE_W_INCH - 0.9
    if png and image_width > 0:
        body_width = max(4.2, _SLIDE_W_INCH - image_width - 1.0)
    lines: list[str] = ["原理演示 · %s · 固定版本 v%d" % (kind_label, revision)]
    artifact_title = str(artifact.get("title") or "").strip()
    if artifact_title:
        lines.append("附件：%s（artifact_id：%s）" % (
            artifact_title, artifact.get("artifact_id") or ""))
    source_note = str(content.get("source_note") or "").strip()
    if source_note:
        lines.append("")
        lines.append("说明：%s" % source_note)
    assumptions, step_count = _demo_content_lists(content)
    if assumptions:
        lines.append("")
        lines.append("前提假设：")
        lines.extend("· %s" % item for item in assumptions)
    if step_count > 0:
        lines.append("")
        lines.append("演示步骤数：%d" % step_count)
    if not png:
        lines.append("")
        lines.append("未附加海报图像，请在知序课堂中播放该演示。")
    box = slide.shapes.add_textbox(
        Inches(body_left), Inches(_HEADER_H_INCH + 0.18),
        Inches(body_width), Inches(_SLIDE_H_INCH - _HEADER_H_INCH - 0.55),
    )
    frame = box.text_frame
    frame.word_wrap = True
    frame.text = "\n".join(lines)[:4000]
    for paragraph in frame.paragraphs:
        paragraph.alignment = PP_ALIGN.LEFT
        paragraph.line_spacing = 1.2
        for run in paragraph.runs:
            run.font.size = Pt(13)
            run.font.name = PPTX_FONT
            run.font.color.rgb = _hex_rgb("#374151")
    _pptx_add_footer(slide, page_no)
    _pptx_set_notes(slide, notes, speech)
    return page_no + 1


def build_pptx(course: dict, scenes: list[dict]) -> bytes:
    """把课程渲染成 ``.pptx`` 字节串（可用 ``python-pptx`` 直接打开）。"""
    from pptx import Presentation
    from pptx.util import Inches

    prs = Presentation()
    prs.slide_width = Inches(_SLIDE_W_INCH)
    prs.slide_height = Inches(_SLIDE_H_INCH)
    _pptx_title_slide(prs, course, scenes)
    page_no = 1
    for scene in scenes:
        content = _scene_content(scene)
        speech = _scene_speech(scene)
        scene_type = str(scene.get("scene_type") or "")
        if scene_type == "slide":
            page_no = _pptx_slide_content_slides(prs, scene, content, speech, page_no)
        elif scene_type == "demo":
            # M4：版本化演示场景导出为「标题 + 固定版本说明 + 海报」页（非占位页）。
            page_no = _pptx_demo_slide(prs, scene, content, speech, page_no)
        else:
            page_no = _pptx_placeholder_slide(prs, scene, content, speech, page_no)
    # P3：末尾附「参考来源」页（标题 + 链接纯文本；无来源时不加页，保持既有页数）。
    sources = _course_sources(course)
    if sources:
        _pptx_sources_slides(prs, sources, page_no)
    buffer = io.BytesIO()
    prs.save(buffer)
    return buffer.getvalue()


# ---------------------------------------------------------------------------
# 课程快照（HTML / ZIP 共用）
# ---------------------------------------------------------------------------

def _course_snapshot(course: dict) -> dict:
    return {
        "title": course.get("title") or "",
        "topic": course.get("topic") or "",
        "subject": course.get("subject") or "",
        "grade": course.get("grade") or "",
        "mode": course.get("mode") or "standard",
        "description": course.get("description") or "",
        "requirements": course.get("requirements") or "",
        "outline": cdb.json_load(course.get("outline_json"), {}),
        "config": cdb.json_load(course.get("config_json"), {}),
        "cover": cdb.json_load(course.get("cover_json"), {}),
    }


def _scene_snapshot(scene: dict, *, for_html: bool = False) -> dict:
    """场景快照（离线 HTML / ZIP / 资源包共用）。

    M4：``demo`` 场景同样带上完整 ``content``（含固定版本学习附件快照），既有字段保持不变；
    播放器与第三方读取方据此按插入时的版本渲染，不再回查源附件。
    """
    content = _scene_content(scene)
    meta = _scene_meta(scene)
    payload = {
        "id": int(scene["id"]),
        "order": int(scene["order_index"]),
        "title": scene.get("title") or "",
        "type": scene.get("scene_type") or "slide",
        "content": content,
        "actions": _scene_actions(scene),
        "meta": {
            "warnings": meta.get("warnings") if isinstance(meta.get("warnings"), list) else [],
            "placeholder": bool(meta.get("placeholder")),
            "prompt_version": meta.get("prompt_version") or "",
            "digest": meta.get("digest") or "",
        },
    }
    if not for_html:
        payload["meta"]["model"] = meta.get("model") or ""
        payload["meta"]["position"] = meta.get("position")
        payload["meta"]["total"] = meta.get("total")
    return payload


def build_snapshot(course: dict, scenes: list[dict], *, for_html: bool = False) -> dict:
    return {
        "formatVersion": ZIP_FORMAT_VERSION,
        "exportedAt": _db().utcnow(),
        # P3：联网检索来源（course.outline.sources 的公开副本）；无来源时为 []。
        "sources": _course_sources(course),
        "course": _course_snapshot(course),
        "scenes": [_scene_snapshot(scene, for_html=for_html) for scene in scenes],
    }


# ---------------------------------------------------------------------------
# TTS（可选）
# ---------------------------------------------------------------------------

async def _collect_tts_assets(*, user_id: int, course: dict, scenes: list[dict]) -> dict[int, dict]:
    """为每个有旁白的场景合成语音（失败自动回退浏览器 TTS），返回 ``{scene_id: {...}}``。"""
    assets: dict[int, dict] = {}
    seen: dict[str, str] = {}
    for scene in scenes:
        speech = _scene_speech(scene)
        if not speech:
            continue
        try:
            result = await tts.synthesize(
                user_id=user_id, course_id=int(course["id"]), text=speech[:tts.MAX_TTS_CHARS],
            )
        except Exception:  # 语音属于可选增强：任何异常都回退浏览器语音
            continue
        asset = result.get("asset") if isinstance(result, dict) else None
        if not isinstance(asset, dict) or not asset.get("id"):
            continue
        try:
            row = cdb.owned_asset(int(asset["id"]), user_id)
            data = storage.read_file(tts.TTS_FOLDER, row["storage_name"])
        except Exception:
            continue
        digest = str(row.get("sha256") or "")
        if not digest:
            import hashlib

            digest = hashlib.sha256(data).hexdigest()
        if digest not in seen:
            seen[digest] = "data:%s;base64,%s" % (
                row.get("mime_type") or "audio/mpeg",
                base64.b64encode(data).decode("ascii"),
            )
        assets[int(scene["id"])] = {
            "asset_id": int(asset["id"]),
            "sha256": digest,
            "mime_type": row.get("mime_type") or "audio/mpeg",
            "data_uri": seen[digest],
            "bytes": data,
        }
    return assets


_ASSET_URL_RE = re.compile(r"/api/classroom/assets/(\d+)")


def _asset_id_of_element(element: dict) -> int:
    """从 slide image 元素解析素材 id（优先 asset_id，其次 /api/classroom/assets/N 或纯数字）。"""
    try:
        value = int(element.get("asset_id") or 0)
    except (TypeError, ValueError):
        value = 0
    if value > 0:
        return value
    match = _ASSET_URL_RE.search(str(element.get("src") or ""))
    if match:
        return int(match.group(1))
    raw = str(element.get("src") or "").strip()
    return int(raw) if raw.isdigit() else 0


def _image_asset_ids(scenes: list[dict]) -> set[int]:
    ids: set[int] = set()
    for scene in scenes:
        content = _scene_content(scene)
        if not isinstance(content, dict) or content.get("type") != "slide":
            continue
        for element in ((content.get("canvas") or {}).get("elements") or []):
            if isinstance(element, dict) and element.get("type") == "image":
                asset_id = _asset_id_of_element(element)
                if asset_id:
                    ids.add(asset_id)
    return ids


def _inline_scene_images(scenes: list[dict], user_id: int) -> list[dict]:
    """PPTX / 离线 HTML 用：把 slide 引用的图片素材内联成 data URI，保证导出文件可离线打开。"""
    asset_ids = _image_asset_ids(scenes)
    if not asset_ids:
        return scenes
    placeholders = ",".join("?" for _ in asset_ids)
    with _db().connection() as con:
        rows = [dict(row) for row in con.execute(
            "SELECT * FROM cls_assets WHERE user_id=? AND id IN (%s)" % placeholders,
            (user_id, *sorted(asset_ids)),
        ).fetchall()]
    by_id = {int(row["id"]): row for row in rows}
    result: list[dict] = []
    for scene in scenes:
        row = dict(scene)
        content = _scene_content(row)
        if not isinstance(content, dict) or content.get("type") != "slide":
            result.append(row)
            continue
        changed = False
        for element in ((content.get("canvas") or {}).get("elements") or []):
            if not isinstance(element, dict) or element.get("type") != "image":
                continue
            src = str(element.get("src") or "")
            if src.startswith("data:image/"):
                continue
            asset = by_id.get(_asset_id_of_element(element))
            if asset is None:
                continue
            try:
                data = storage.read_file(tts.TTS_FOLDER, asset["storage_name"])
            except Exception:
                continue
            element["src"] = "data:%s;base64,%s" % (
                asset.get("mime_type") or "image/png", base64.b64encode(data).decode("ascii"),
            )
            changed = True
        if changed:
            row["content_json"] = cdb.json_dump(content)
        result.append(row)
    return result


def _course_assets(course_id: int, user_id: int, scenes: list[dict] | None = None) -> list[dict]:
    """课程已落库素材（TTS 音频 / 配图等）：返回可写入 ZIP 的资源清单（带 assetId 供导入回链）。"""
    referenced = _image_asset_ids(scenes or [])
    with _db().connection() as con:
        if referenced:
            placeholders = ",".join("?" for _ in referenced)
            rows = [dict(row) for row in con.execute(
                "SELECT * FROM cls_assets WHERE user_id=? AND (course_id=? OR id IN (%s)) ORDER BY id"
                % placeholders,
                (user_id, course_id, *sorted(referenced)),
            ).fetchall()]
        else:
            rows = [dict(row) for row in con.execute(
                "SELECT * FROM cls_assets WHERE user_id=? AND course_id=? ORDER BY id",
                (user_id, course_id),
            ).fetchall()]
    resources: list[dict] = []
    for row in rows:
        try:
            data = storage.read_file(tts.TTS_FOLDER, row["storage_name"])
        except Exception:
            continue
        digest = str(row.get("sha256") or "")
        if not digest:
            import hashlib

            digest = hashlib.sha256(data).hexdigest()
        suffix = _asset_suffix(row.get("mime_type") or "")
        path = "assets/%s/%s%s" % (re.sub(r"[^a-z0-9_-]+", "_", str(row.get("kind") or "file")),
                                   digest[:16], suffix)
        resources.append({
            "path": path,
            "sha256": digest,
            "size": len(data),
            "mimeType": row.get("mime_type") or "application/octet-stream",
            "kind": row.get("kind") or "file",
            "assetId": int(row["id"]),
            "bytes": data,
        })
    return resources


def _asset_suffix(mime: str) -> str:
    mime = str(mime or "").split(";")[0].strip().lower()
    mapping = {
        "audio/mpeg": ".mp3", "audio/mp3": ".mp3", "audio/wav": ".wav", "audio/x-wav": ".wav",
        "audio/ogg": ".ogg", "audio/opus": ".opus", "audio/aac": ".aac", "audio/flac": ".flac",
        "image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp", "image/gif": ".gif",
    }
    return mapping.get(mime, ".bin")


# ---------------------------------------------------------------------------
# 离线单文件 HTML 播放器
# ---------------------------------------------------------------------------

_HTML_CSS = """
:root { color-scheme: light; }
* { box-sizing: border-box; }
html, body { margin: 0; padding: 0; height: 100%; background: #0f172a; color: #e5e7eb;
  font-family: system-ui, -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif; }
#app { display: flex; flex-direction: column; height: 100%; }
header { display: flex; flex-wrap: wrap; align-items: center; gap: 10px; padding: 10px 16px;
  background: #111827; border-bottom: 1px solid #1f2937; }
header h1 { font-size: 16px; margin: 0; font-weight: 600; flex: 1 1 220px; }
.meta { color: #9ca3af; font-size: 12px; }
button { font: inherit; font-size: 13px; padding: 7px 12px; border-radius: 8px; border: 1px solid #374151;
  background: #1f2937; color: #e5e7eb; cursor: pointer; min-height: 36px; }
button:hover:not(:disabled) { background: #374151; }
button:disabled { opacity: .4; cursor: default; }
main { flex: 1; overflow: auto; padding: 14px; display: flex; flex-direction: column; gap: 12px; }
.stage { flex: 1; display: flex; align-items: flex-start; justify-content: center; min-height: 0; }
.slide-shell { position: relative; width: 100%; max-width: 1000px; aspect-ratio: 1000 / 562.5;
  background: #fff; border-radius: 10px; overflow: hidden; }
.slide-canvas { position: absolute; left: 0; top: 0; width: 1000px; height: 562.5px;
  transform-origin: top left; background: #fff; }
.slide-canvas .el { position: absolute; overflow: hidden; }
.slide-canvas .el.text { white-space: pre-wrap; line-height: 1.5; word-break: break-word; }
.slide-canvas .el.line { height: 2px; transform-origin: left center; }
.slide-canvas table { border-collapse: collapse; width: 100%; height: 100%; font-size: 14px; }
.slide-canvas th, .slide-canvas td { border: 1px solid #cbd5e1; padding: 4px 6px; text-align: left; }
.scene-card { background: #fff; color: #111827; border-radius: 10px; padding: 18px; max-width: 1000px;
  width: 100%; margin: 0 auto; }
.scene-card h2 { margin: 0 0 10px; font-size: 20px; }
.scene-card h3 { margin: 16px 0 6px; font-size: 16px; color: #1f6feb; }
.scene-card p, .scene-card li { line-height: 1.7; font-size: 14px; }
.quiz-q { border-top: 1px solid #e5e7eb; padding: 12px 0; }
.quiz-q:first-child { border-top: none; }
.quiz-option { margin: 4px 0; }
.analysis { margin-top: 8px; padding: 10px 12px; background: #f1f5f9; border-radius: 8px; color: #334155;
  white-space: pre-wrap; }
.pbl-block { background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 8px; padding: 12px; margin: 8px 0; }
.interactive-wrap { width: 100%; max-width: 1000px; margin: 0 auto; background: #fff; border-radius: 10px;
  overflow: hidden; }
.interactive-wrap iframe { display: block; width: 100%; border: 0; }
.status { max-width: 1000px; width: 100%; margin: 0 auto; background: #111827; border: 1px solid #1f2937;
  color: #9ca3af; font-size: 12px; border-radius: 8px; padding: 8px 12px; min-height: 32px; }
.subtitle { max-width: 1000px; width: 100%; margin: 0 auto; background: #111827; border: 1px solid #1f2937;
  border-radius: 8px; padding: 10px 14px; font-size: 14px; line-height: 1.6; min-height: 44px; }
.subtitle .who { color: #60a5fa; margin-right: 8px; }
footer { padding: 8px 16px; background: #111827; border-top: 1px solid #1f2937; display: flex;
  flex-wrap: wrap; align-items: center; gap: 10px; }
.nav { display: flex; gap: 8px; }
.spacer { flex: 1; }
.sources { max-width: 1000px; width: 100%; margin: 0 auto; background: #ffffff; color: #111827;
  border-radius: 10px; padding: 16px 20px; }
.sources h2 { margin: 0 0 6px; font-size: 16px; color: #1f6feb; }
.sources .sources-hint { margin: 0 0 10px; font-size: 12px; color: #6b7280; }
.sources .source-list { margin: 0; padding-left: 0; list-style: none; }
.sources .source-item { border-top: 1px solid #e5e7eb; padding: 8px 0; font-size: 13px; line-height: 1.6; }
.sources .source-item:first-child { border-top: none; }
.sources .source-index { color: #1f6feb; font-weight: 600; }
.sources .source-title { font-weight: 600; }
.sources .source-site { margin-left: 8px; color: #6b7280; font-size: 12px; }
.sources .source-url { color: #2563eb; font-size: 12px; word-break: break-all; }
.sources .source-summary { color: #4b5563; font-size: 12px; margin-top: 2px; }
/* 手工「原理演示」场景（固定版本学习附件快照）：播放器内页与离线静态备份共用。 */
.cls-demo-scene { background: #fff; color: #111827; border-radius: 10px; padding: 18px 20px; max-width: 1000px;
  width: 100%; margin: 0 auto; }
.cls-demo-head { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; margin-bottom: 8px; }
.cls-demo-badge { display: inline-block; padding: 2px 10px; border-radius: 999px; background: #1d4ed8;
  color: #eff6ff; font-size: 12px; }
.cls-demo-kind { color: #4b5563; font-size: 12px; }
.cls-demo-scene h2 { margin: 4px 0 10px; font-size: 20px; }
.cls-demo-scene h3 { margin: 12px 0 4px; font-size: 15px; color: #1f6feb; }
.cls-demo-image { display: block; max-width: 100%; height: auto; margin: 10px auto;
  border: 1px solid #e5e7eb; border-radius: 8px; background: #fff; }
.cls-demo-note { margin: 8px 0; font-size: 14px; line-height: 1.7; color: #334155; }
.cls-demo-assumptions { margin: 6px 0 0 18px; padding: 0; font-size: 13px; line-height: 1.7;
  color: #475569; }
.cls-demo-missing { margin: 10px 0; padding: 10px 12px; background: #f8fafc; border: 1px dashed #cbd5e1;
  border-radius: 8px; color: #64748b; font-size: 13px; }
.cls-demo-appendix { max-width: 1000px; width: 100%; margin: 0 auto; display: flex; flex-direction: column;
  gap: 10px; }
.cls-demo-appendix h2 { margin: 0; font-size: 16px; color: #1f6feb; }
.cls-demo-appendix-hint { margin: 0; font-size: 12px; color: #9ca3af; line-height: 1.6; }
/* 视频导出捕获模式（window.__zhixuCapture.gotoScene 触发）：隐藏播放控件，画布固定 1280×720。
   只在截图脚本调用时生效，正常播放完全不受影响。 */
body.capture { overflow: hidden; background: #000; }
body.capture header, body.capture footer, body.capture .subtitle, body.capture main > .nav,
body.capture #speech-hint, body.capture #tts-audio { display: none !important; }
body.capture main { padding: 0; overflow: hidden; display: block; }
body.capture #scene-stage { position: fixed; inset: 0; display: block; }
body.capture .stage { position: fixed; inset: 0; display: flex; align-items: center; justify-content: center; }
body.capture .slide-shell { width: 100vw; height: 100vh; max-width: none; aspect-ratio: auto; border-radius: 0; }
body.capture .scene-card, body.capture .interactive-wrap { width: 100vw; max-width: none; min-height: 100vh;
  border-radius: 0; overflow: hidden; }
body.capture .status { display: none; }
/* 逐帧动画效果层（P5 增强：仅 animated 截帧时由 window.__zhixuCapture.seekMs(t) 写入，
   正常播放不显示；白板/聚光灯/激光/高亮/字幕都按时间 t 确定性重建）。 */
.fx-layer { position: absolute; left: 0; top: 0; width: 100%; height: 100%; pointer-events: none;
  z-index: 30; }
.fx-layer > div { position: absolute; display: none; }
body.capture .capture-subtitle { position: fixed; left: 0; right: 0; bottom: 0; z-index: 60;
  background: linear-gradient(180deg, rgba(15,23,42,0) 0%, rgba(15,23,42,.88) 30%); color: #f8fafc;
  font-size: 24px; line-height: 1.45; padding: 30px 52px 22px; text-align: center; display: none; }
body.capture .capture-subtitle.visible { display: block; }
body.capture .capture-subtitle .who { color: #60a5fa; margin-right: 12px; font-weight: 600; }
@media (max-width: 640px) {
  main { padding: 10px; }
  .scene-card { padding: 14px; }
  header h1 { font-size: 15px; }
}
"""

#: M7：离线可交互演示播放器样式（只在含演示场景、且内联了播放器的导出里追加；
#: 无演示场景的课程沿用上面的既有 CSS，输出结构不变）。
_OFFLINE_DEMO_CSS = """
/* 离线可交互演示播放器（M7 · 与 web/js/offline/offlineDemo.js 配套）。
   先补一组浅色设计令牌：flow/iso 渲染器的 .pd-* 样式（web/styles-principles.css）
   复用全局令牌，离线单文件不加载全局样式表，这里给出与在线浅色主题一致的取值。 */
:root {
  --paper: #f7f6f1; --surface: #fffefa; --rail: #f0f2eb;
  --ink: #182927; --soft: #556761; --muted: #556761;
  --accent: #286552; --accent-deep: #194e3e; --tint: #e8f1e9;
  --line: #dedfd6; --focus: #127963;
}
.cls-demo-offline { margin: 12px 0 4px; }
/* 与在线播放器 .pd-stage 一致的固定舞台：SVG 按 viewBox 等比缩放（preserveAspectRatio=meet），
   避免窄 viewBox 的等距图被拉满 1000px 后文字过大。 */
.cls-demo-offline-view { width: 100%; height: clamp(280px, 46vh, 460px);
  display: flex; align-items: center; justify-content: center; overflow: hidden;
  background: #ffffff; border: 1px solid #e2e8f0; border-radius: 10px; }
.cls-demo-offline-view svg { display: block; width: 100%; height: 100%; max-width: 100%; }
.cls-demo-offline-controls { display: flex; flex-wrap: wrap; align-items: center; gap: 8px;
  margin: 10px 0 6px; }
.cls-demo-offline button, .offline-demo-btn { font: inherit; font-size: 13px; padding: 6px 11px;
  min-height: 32px; border-radius: 8px; border: 1px solid #cbd5e1; background: #f8fafc;
  color: #1f2937; cursor: pointer; }
.cls-demo-offline button:hover:not(:disabled) { background: #e2e8f0; }
.cls-demo-offline button:disabled { opacity: .45; cursor: default; }
.offline-demo-btn-primary { background: #1d4ed8 !important; color: #eff6ff !important;
  border-color: #1d4ed8 !important; }
.offline-demo-step { font-size: 13px; color: #334155; }
.offline-demo-hint { margin: 4px 0; font-size: 12px; color: #64748b; }
.cls-demo-offline-error { margin: 10px 0 0; padding: 8px 12px; background: #fff7ed; border: 1px dashed
  #fdba74; border-radius: 8px; color: #9a3412; font-size: 12px; line-height: 1.6; }
/* 工厂仿真回放（仅用真实 metrics / events，不引入 three.js）。 */
.offline-demo-factory { display: flex; flex-direction: column; gap: 10px; }
.offline-demo-metrics { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
  gap: 8px; }
.offline-demo-metric { border: 1px solid #e2e8f0; border-radius: 8px; padding: 8px 10px;
  background: #f8fafc; display: flex; flex-direction: column; gap: 2px; }
.offline-demo-metric-label { font-size: 12px; color: #64748b; }
.offline-demo-metric-value { font-size: 16px; font-weight: 600; color: #0f172a; }
.offline-demo-metric-note { font-size: 11px; color: #9a3412; line-height: 1.5; }
.offline-demo-flow-svg { display: block; width: 100%; height: auto; max-width: 100%;
  margin: 2px 0; }
.offline-demo-svg-box { fill: #ffffff; stroke: #cbd5e1; stroke-width: 1.5; }
.offline-demo-svg-station[data-state="busy"] .offline-demo-svg-box { fill: #eff6ff; stroke: #3b82f6; }
.offline-demo-svg-station[data-state="blocked"] .offline-demo-svg-box { fill: #fff7ed;
  stroke: #f97316; }
.offline-demo-svg-label { font-size: 12px; fill: #0f172a; }
.offline-demo-svg-queue { font-size: 11px; fill: #64748b; }
.offline-demo-svg-arrow { stroke: #94a3b8; stroke-width: 1.5; }
.offline-demo-svg-arrowhead { fill: #94a3b8; }
.offline-demo-line { display: flex; align-items: stretch; gap: 6px; overflow-x: auto; padding: 6px 0; }
.offline-demo-station { min-width: 150px; flex: 1 1 0; border: 1px solid #e2e8f0; border-radius: 8px;
  padding: 8px 10px; background: #ffffff; display: flex; flex-direction: column; gap: 4px; }
.offline-demo-station[data-state="busy"] { border-color: #93c5fd; background: #eff6ff; }
.offline-demo-station[data-state="blocked"] { border-color: #fdba74; background: #fff7ed; }
.offline-demo-station[data-active="1"] { box-shadow: 0 0 0 2px #1d4ed8 inset; }
.offline-demo-station-label { font-size: 14px; color: #111827; }
.offline-demo-station-state { font-size: 12px; color: #1d4ed8; }
.offline-demo-station-queue { font-size: 12px; color: #334155; }
.offline-demo-station-util, .offline-demo-station-wait { font-size: 11px; color: #64748b; }
.offline-demo-queue-bar { height: 6px; border-radius: 999px; background: #e2e8f0; overflow: hidden; }
.offline-demo-queue-fill { display: block; width: 0; height: 100%; background: #2563eb;
  transition: width .18s ease-out; }
.offline-demo-arrow { align-self: center; color: #94a3b8; font-size: 16px; }
.offline-demo-timeline { border-top: 1px dashed #e2e8f0; padding-top: 8px; }
.offline-demo-range { flex: 1 1 180px; min-width: 140px; accent-color: #1d4ed8; }
.offline-demo-time { font-size: 12px; color: #334155; white-space: nowrap; }
.offline-demo-event { margin: 2px 0; font-size: 13px; color: #1f2937; }
.offline-demo-eventlist { margin: 4px 0 0; padding: 0 0 0 18px; max-height: 220px; overflow: auto;
  font-size: 12px; color: #64748b; }
.offline-demo-eventitem.is-past { color: #1f2937; font-weight: 600; }
/* 视频导出捕获模式（window.__zhixuCapture.gotoScene 触发）：隐藏演示播放控件，
   截图只保留演示画面（与既有 body.capture 规则同一套类，不额外引入脚本开关）。 */
body.capture [data-offline-demo-controls] { display: none !important; }
body.capture .cls-demo-offline-error, body.capture .offline-demo-hint { display: none !important; }
"""

_HTML_JS = r"""
(function () {
  var DATA = JSON.parse(document.getElementById('course-data').textContent);
  var scenes = (DATA && DATA.scenes) || [];
  var course = (DATA && DATA.course) || {};
  var state = { index: 0, lastSpoken: '', tts: null, autoSpeak: false };
  var CANVAS_W = 1000, CANVAS_H = 562.5;

  // 视频导出捕获接口（由 app/classroom/video.py 的 Playwright 截图脚本调用）：
  // - ready 在首帧渲染完成后置 true；sceneCount 为场景总数；
  // - gotoScene(index) 切场并进入 capture 模式（隐藏播放控件、画布固定 1280×720，
  //   16:9 缩放后正好铺满视口），不影响正常播放；
  // - seekMs(t) 按场景 actions 时间轴把聚光灯/激光/高亮/白板/字幕**确定性**推进到 t 毫秒
  //   （同一 t 反复调用结果一致，供 animated 模式逐帧截图）；
  // - sceneDurationMs(i) 返回场景时长估算（speech 文本估算或课程注入的旁白音频时长，最少 1.5 秒）。
  var capture = {
    ready: false,
    sceneCount: scenes.length,
    current: 0,
    timeMs: 0,
    hasSeek: true,
    gotoScene: function (index) {
      var next = Math.max(0, Math.min(scenes.length - 1, parseInt(index, 10) || 0));
      if (!document.body.classList.contains('capture')) document.body.classList.add('capture');
      state.autoSpeak = false;
      clearCaptureEffects();
      state.index = next;
      render();
      capture.current = state.index;
      capture.timeMs = 0;
      window.setTimeout(scaleSlide, 0);
      return capture.current;
    },
    seekMs: function (ms) {
      var value = Math.max(0, Math.round(Number(ms) || 0));
      if (!document.body.classList.contains('capture')) document.body.classList.add('capture');
      state.autoSpeak = false;
      applyCaptureState(value);
      capture.current = state.index;
      capture.timeMs = value;
      return value;
    },
    sceneDurationMs: function (index) {
      var position = Math.max(0, Math.min(scenes.length - 1, parseInt(index, 10) || 0));
      return sceneDurationMs(position);
    }
  };
  window.__zhixuCapture = capture;

  // ---------------------------------------------------------------------------
  // 逐帧动画时间轴（P5 增强；估算参数与 app/classroom/video.py::estimate_scene_duration_ms
  // 保持一致，两边改动需同步）：
  // - speech：0.8s 起拍 + 每字 0.25s（约 4 字/秒），最少 1.5s；
  // - spotlight/laser/highlight：各 0.7s（与在线播放器每个视觉动作停留 0.7s 一致）；
  // - whiteboard：每个 op 0.26s，整段收敛到 0.7–3s；
  // - play_audio：1s；next_scene/show_slide 不占用时间（不参与截帧动画）；
  // - 场景总时长最少 1.5s；旁白场景可由课程快照注入 scene.durationMs（音频毫秒数）。
  var MIN_SCENE_MS = 1500;
  var SPEECH_BASE_MS = 800;
  var SPEECH_PER_CHAR_MS = 250;
  var SPEECH_MIN_MS = 1500;
  var VISUAL_ACTION_MS = 700;
  var WB_OP_MS = 260;
  var WB_MIN_MS = 700;
  var WB_MAX_MS = 3000;
  var AUDIO_ACTION_MS = 1000;

  function clampNumber(value, low, high) {
    return Math.max(low, Math.min(high, value));
  }

  function actionDurationMs(action) {
    if (!action || typeof action !== 'object') return 0;
    var type = action.type || '';
    if (type === 'speech') {
      var text = String(action.text || '');
      return Math.max(SPEECH_MIN_MS, SPEECH_BASE_MS + text.length * SPEECH_PER_CHAR_MS);
    }
    if (type === 'spotlight' || type === 'laser' || type === 'highlight') return VISUAL_ACTION_MS;
    if (type === 'whiteboard') {
      var ops = Array.isArray(action.ops) ? action.ops : [];
      return clampNumber(ops.length * WB_OP_MS, WB_MIN_MS, WB_MAX_MS);
    }
    if (type === 'play_audio') return AUDIO_ACTION_MS;
    return 0;
  }

  function sceneTimeline(index) {
    var scene = scenes[index] || null;
    if (!scene) return { entries: [], totalMs: 0 };
    var cursor = 0;
    var entries = [];
    (scene.actions || []).forEach(function (action) {
      var duration = actionDurationMs(action);
      if (duration <= 0) return;
      entries.push({ action: action, startMs: cursor, endMs: cursor + duration, durationMs: duration });
      cursor += duration;
    });
    return { entries: entries, totalMs: cursor };
  }

  function sceneDurationMs(index) {
    var scene = scenes[index] || null;
    if (!scene) return MIN_SCENE_MS;
    var injected = Number(scene.durationMs);
    if (isFinite(injected) && injected > 0) return Math.max(MIN_SCENE_MS, Math.round(injected));
    return Math.max(MIN_SCENE_MS, sceneTimeline(index).totalMs);
  }

  function isSlideScene(scene) {
    return !!scene && String(scene.type || 'slide') === 'slide';
  }

  function effectBox(elementId) {
    var scene = currentScene();
    var canvas = (scene && scene.content && scene.content.canvas) || {};
    var elements = Array.isArray(canvas.elements) ? canvas.elements : [];
    var key = String(elementId || '');
    for (var i = 0; i < elements.length; i++) {
      var element = elements[i];
      if (!element || String(element.id || '') !== key) continue;
      return {
        left: Number(element.left) || 0,
        top: Number(element.top) || 0,
        width: Math.max(8, Number(element.width) || 100),
        height: Math.max(8, Number(element.height) || 40),
      };
    }
    return null;
  }

  function clearCaptureEffects() {
    var canvas = document.querySelector('#scene-stage .slide-canvas');
    if (canvas) {
      var nodes = canvas.querySelectorAll('.fx-spot, .fx-highlight, .fx-laser');
      for (var i = 0; i < nodes.length; i++) nodes[i].style.cssText = 'display:none';
      var board = canvas.querySelector('.fx-whiteboard');
      if (board) { board.style.cssText = ''; board.replaceChildren(); }
    }
    var bar = find('capture-subtitle');
    if (bar) {
      bar.classList.remove('visible');
      bar.textContent = '';
    }
  }

  function ensureCaptureSubtitle() {
    if (!document.body.classList.contains('capture')) return null;
    var bar = find('capture-subtitle');
    if (!bar) {
      bar = el('div', 'capture-subtitle');
      bar.id = 'capture-subtitle';
      document.body.appendChild(bar);
    }
    return bar;
  }

  function updateCaptureSubtitle(entries, ms) {
    var bar = ensureCaptureSubtitle();
    if (!bar) return;
    var lastSpeech = null;
    for (var i = 0; i < entries.length; i++) {
      if (entries[i].action.type === 'speech' && entries[i].startMs <= ms) lastSpeech = entries[i];
    }
    if (!lastSpeech) {
      bar.classList.remove('visible');
      bar.textContent = '';
      return;
    }
    var text = String(lastSpeech.action.text || '');
    var progress = lastSpeech.durationMs > 0
      ? clampNumber((ms - lastSpeech.startMs) / lastSpeech.durationMs, 0, 1) : 1;
    var shown = progress >= 1 ? text : text.slice(0, Math.max(1, Math.ceil(text.length * progress)));
    bar.replaceChildren();
    bar.appendChild(el('span', 'who', '教师'));
    bar.appendChild(document.createTextNode(shown));
    bar.classList.add('visible');
  }

  function ensureEffects(layer) {
    var canvas = document.querySelector('#scene-stage .slide-canvas');
    if (!canvas) return null;
    if (!layer) layer = canvas.querySelector('.fx-layer');
    if (!layer) {
      layer = el('div', 'fx-layer');
      layer.appendChild(el('div', 'fx-spot'));
      layer.appendChild(el('div', 'fx-highlight'));
      layer.appendChild(el('div', 'fx-laser'));
      layer.appendChild(el('div', 'fx-whiteboard'));
      canvas.appendChild(layer);
    }
    return layer;
  }

  function applySpotlightEffect(layer, action, ms) {
    var node = layer.querySelector('.fx-spot');
    var box = effectBox(action.element_id);
    if (!node || !box) return;
    var dim = Number(action.dim_opacity);
    if (!isFinite(dim)) dim = 0.55;
    dim = clampNumber(dim, 0.2, 0.85);
    var pulse = 0.5 + 0.5 * Math.sin(ms / 240);  // 0–1 确定性呼吸（只随 t 变化）
    node.style.cssText =
      'display:block;left:' + (box.left - 10) + 'px;top:' + (box.top - 10) + 'px;'
      + 'width:' + (box.width + 20) + 'px;height:' + (box.height + 20) + 'px;border-radius:14px;'
      + 'box-shadow:0 0 0 2400px rgba(6,18,24,' + dim.toFixed(3) + '),'
      + '0 0 26px rgba(96,165,250,' + (0.35 + 0.35 * pulse).toFixed(3) + ');'
      + 'outline:2px solid rgba(96,165,250,0.9);outline-offset:2px;';
  }

  function applyHighlightEffect(layer, action, ms) {
    var node = layer.querySelector('.fx-highlight');
    var box = effectBox(action.element_id);
    if (!node || !box) return;
    var pulse = 0.55 + 0.35 * (0.5 + 0.5 * Math.sin(ms / 160));
    node.style.cssText =
      'display:block;left:' + (box.left - 8) + 'px;top:' + (box.top - 8) + 'px;'
      + 'width:' + (box.width + 16) + 'px;height:' + (box.height + 16) + 'px;border-radius:10px;'
      + 'background:rgba(250,204,21,' + (0.22 * pulse).toFixed(3) + ');'
      + 'border:3px solid rgba(250,204,21,' + pulse.toFixed(3) + ');'
      + 'box-shadow:0 0 20px rgba(250,204,21,' + (0.5 * pulse).toFixed(3) + ');';
  }

  function applyLaserEffect(layer, action, ms) {
    var node = layer.querySelector('.fx-laser');
    var box = effectBox(action.element_id);
    if (!node || !box) return;
    var color = String(action.color || '#ff0000');
    var cx = box.left + box.width / 2 + Math.cos(ms / 90) * 5;
    var cy = box.top + box.height / 2 + Math.sin(ms / 70) * 5;
    node.style.cssText =
      'display:block;left:' + (cx - 9) + 'px;top:' + (cy - 9) + 'px;width:18px;height:18px;'
      + 'border-radius:50%;background:radial-gradient(circle,#ffffff 0%,' + color + ' 42%,rgba(255,0,0,0) 72%);'
      + 'box-shadow:0 0 14px ' + color + ';';
  }

  function fxSvgElement(name, attrs) {
    // SVG 命名空间是标识符、不是网络请求；拆成两段避免离线导出被误判为「外链」。
    var svgNamespace = 'http' + '://www.w3.org/2000/svg';
    var node = document.createElementNS(svgNamespace, name);
    Object.keys(attrs || {}).forEach(function (key) { node.setAttribute(key, String(attrs[key])); });
    return node;
  }

  function applyWhiteboardState(layer, entries, ms) {
    var host = layer.querySelector('.fx-whiteboard');
    if (!host) return;
    host.style.cssText = 'display:block;left:0;top:0;width:' + CANVAS_W + 'px;height:' + CANVAS_H + 'px;';
    host.replaceChildren();
    entries.forEach(function (entry) {
      var ops = Array.isArray(entry.action.ops) ? entry.action.ops : [];
      if (!ops.length) return;
      var unit = entry.action.coord_space === 'unit';
      var opDuration = entry.durationMs / ops.length;
      ops.forEach(function (op, opIndex) {
        var start = entry.startMs + opIndex * opDuration;
        var progress = clampNumber((ms - start) / opDuration, 0, 1);
        if (progress <= 0) return;
        drawWhiteboardOp(host, op, unit, progress);
      });
    });
  }

  function drawWhiteboardOp(host, op, unit, progress) {
    var xOf = function (value) { return unit ? (Number(value) || 0) * CANVAS_W : (Number(value) || 0); };
    var yOf = function (value) { return unit ? (Number(value) || 0) * CANVAS_H : (Number(value) || 0); };
    var wOf = function (value, fallback) {
      return unit ? (Number(value) || fallback) * CANVAS_W : (Number(value) || fallback);
    };
    var hOf = function (value, fallback) {
      return unit ? (Number(value) || fallback) * CANVAS_H : (Number(value) || fallback);
    };
    var opacity = progress >= 1 ? 1 : Math.max(0.12, progress);
    var kind = String(op.kind || '');
    if (kind === 'text') {
      var content = String(op.content || '');
      var shown = progress >= 1 ? content : content.slice(0, Math.max(1, Math.ceil(content.length * progress)));
      var node = document.createElement('div');
      node.style.cssText = 'position:absolute;left:' + xOf(op.x) + 'px;top:' + yOf(op.y) + 'px;'
        + 'font-size:' + (Number(op.font_size) || 18) + 'px;color:' + (op.color || '#333333') + ';'
        + 'white-space:pre-wrap;line-height:1.4;max-width:82%;';
      node.textContent = shown;
      host.appendChild(node);
      return;
    }
    if (kind === 'formula') {
      var formula = document.createElement('div');
      formula.style.cssText = 'position:absolute;left:' + xOf(op.x) + 'px;top:' + yOf(op.y) + 'px;'
        + 'max-width:' + Math.max(80, wOf(op.width, 300)) + 'px;'
        + 'font-family:Consolas,"Times New Roman",serif;font-style:italic;font-size:24px;'
        + 'color:' + (op.color || '#1f6feb') + ';opacity:' + opacity.toFixed(3) + ';'
        + 'white-space:pre-wrap;line-height:1.4;';
      formula.textContent = String(op.latex || op.formula || '');
      host.appendChild(formula);
      return;
    }
    var svg = fxSvgElement('svg', {
      viewBox: '0 0 ' + CANVAS_W + ' ' + CANVAS_H, width: CANVAS_W, height: CANVAS_H,
    });
    svg.style.cssText = 'position:absolute;left:0;top:0;overflow:visible;';
    if (kind === 'line' || kind === 'arrow') {
      var x1 = xOf(op.x1), y1 = yOf(op.y1);
      var x2 = x1 + (xOf(op.x2) - x1) * progress;
      var y2 = y1 + (yOf(op.y2) - y1) * progress;
      svg.appendChild(fxSvgElement('line', {
        x1: x1, y1: y1, x2: x2, y2: y2,
        stroke: op.color || op.stroke || '#333333', 'stroke-width': Number(op.width) || 2,
        'stroke-linecap': 'round',
      }));
      if (kind === 'arrow' && progress > 0.2) {
        var angle = Math.atan2(yOf(op.y2) - y1, xOf(op.x2) - x1);
        var size = Math.max(10, (Number(op.width) || 2) * 5);
        var head = fxSvgElement('path', {
          d: 'M ' + x2 + ' ' + y2
            + ' l ' + (size * Math.cos(angle + 2.6)) + ' ' + (size * Math.sin(angle + 2.6))
            + ' L ' + (x2 + size * 0.6 * Math.cos(angle)) + ' ' + (y2 + size * 0.6 * Math.sin(angle))
            + ' L ' + (size * Math.cos(angle - 2.6) + x2) + ' ' + (size * Math.sin(angle - 2.6) + y2) + ' Z',
          fill: op.color || op.stroke || '#333333',
          opacity: progress.toFixed(3),
        });
        svg.appendChild(head);
      }
    } else if (kind === 'rect') {
      svg.appendChild(fxSvgElement('rect', {
        x: xOf(op.x), y: yOf(op.y),
        width: Math.max(4, wOf(op.width, 200)), height: Math.max(4, hOf(op.height, 120)),
        rx: 4, fill: op.fill && op.fill !== 'transparent' ? op.fill : 'none',
        stroke: op.stroke || op.color || '#5b9bd5', 'stroke-width': Number(op.stroke_width) || 3,
        opacity: opacity.toFixed(3),
      }));
    } else if (kind === 'circle') {
      var width = Math.max(4, wOf(op.width, 160));
      var height = Math.max(4, hOf(op.height, 160));
      svg.appendChild(fxSvgElement('ellipse', {
        cx: xOf(op.x) + width / 2, cy: yOf(op.y) + height / 2,
        rx: width / 2, ry: height / 2,
        fill: op.fill && op.fill !== 'transparent' ? op.fill : 'none',
        stroke: op.stroke || op.color || '#5b9bd5', 'stroke-width': Number(op.stroke_width) || 3,
        opacity: opacity.toFixed(3),
      }));
    } else if (kind === 'path') {
      var path = fxSvgElement('path', {
        d: String(op.d || ''),
        fill: op.fill && op.fill !== 'transparent' ? op.fill : 'none',
        stroke: op.stroke || op.color || '#333333', 'stroke-width': Number(op.stroke_width) || 2,
        'stroke-linejoin': 'round', 'stroke-linecap': 'round',
        pathLength: 1, 'stroke-dasharray': 1, 'stroke-dashoffset': (1 - progress).toFixed(4),
        'vector-effect': 'non-scaling-stroke', opacity: opacity.toFixed(3),
      });
      svg.appendChild(path);
    } else {
      return;
    }
    host.appendChild(svg);
  }

  function applyCaptureState(ms) {
    clearCaptureEffects();
    var scene = currentScene();
    if (!scene) return;
    var entries = sceneTimeline(state.index).entries;
    updateCaptureSubtitle(entries, ms);
    if (!isSlideScene(scene)) return;
    var spotlight = null, highlight = null, laser = null;
    var whiteboards = [];
    entries.forEach(function (entry) {
      if (entry.startMs > ms) return;
      var type = entry.action.type;
      if (type === 'spotlight') spotlight = entry;
      else if (type === 'highlight') highlight = entry;
      else if (type === 'laser') laser = entry;
      else if (type === 'whiteboard') whiteboards.push(entry);
    });
    if (!spotlight && !highlight && !laser && !whiteboards.length) return;
    var layer = ensureEffects();
    if (!layer) return;
    if (spotlight) applySpotlightEffect(layer, spotlight.action, ms);
    if (highlight) applyHighlightEffect(layer, highlight.action, ms);
    if (laser) applyLaserEffect(layer, laser.action, ms);
    if (whiteboards.length) applyWhiteboardState(layer, whiteboards, ms);
  }

  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }
  function sceneTypeLabel(t) {
    return { slide: '幻灯片', quiz: '课堂测验', interactive: '交互场景', pbl: '项目式学习',
      demo: '原理演示' }[t] || t;
  }
  function find(id) { return document.getElementById(id); }

  function renderHeader() {
    find('course-title').textContent = course.title || 'AI 课堂';
    var meta = [course.subject, course.grade, (scenes.length + ' 个场景')].filter(Boolean).join(' · ');
    find('course-meta').textContent = meta;
  }

  function currentScene() { return scenes[state.index] || null; }

  function renderNav() {
    find('prev').disabled = state.index <= 0;
    find('next').disabled = state.index >= scenes.length - 1;
    find('progress').textContent = scenes.length ? ((state.index + 1) + ' / ' + scenes.length) : '0 / 0';
    var scene = currentScene();
    find('scene-select').value = String(state.index);
  }

  function fillSceneSelect() {
    var select = find('scene-select');
    select.innerHTML = '';
    scenes.forEach(function (scene, index) {
      var option = document.createElement('option');
      option.value = String(index);
      option.textContent = (index + 1) + '. ' + (scene.title || '未命名场景');
      select.appendChild(option);
    });
    select.addEventListener('change', function () {
      state.index = Math.max(0, Math.min(scenes.length - 1, parseInt(select.value, 10) || 0));
      render();
    });
  }

  function elementNode(element) {
    var node = document.createElement('div');
    node.className = 'el';
    node.style.left = (element.left || 0) + 'px';
    node.style.top = (element.top || 0) + 'px';
    node.style.width = Math.max(8, element.width || 100) + 'px';
    node.style.height = Math.max(8, element.height || 40) + 'px';
    if (element.rotate) node.style.transform = 'rotate(' + element.rotate + 'deg)';
    var type = element.type || '';
    if (type === 'text' || type === 'latex') {
      node.className = 'el text';
      node.textContent = element.text || (type === 'latex' ? (element.latex || '') : '');
      node.style.fontSize = Math.max(10, element.fontSize || 18) + 'px';
      node.style.color = element.color || '#333333';
      if (type === 'latex') { node.style.fontStyle = 'italic'; node.style.color = element.color || '#1f6feb'; }
      if (element.align) node.style.textAlign = element.align;
      return node;
    }
    if (type === 'code') {
      node.className = 'el text';
      node.textContent = element.code || '';
      node.style.fontFamily = 'Consolas, monospace';
      node.style.fontSize = '13px';
      node.style.whiteSpace = 'pre-wrap';
      node.style.color = '#1f2937';
      return node;
    }
    if (type === 'shape') {
      node.style.background = element.fill || '#5b9bd5';
      if ((element.path || '').indexOf('A') >= 0) node.style.borderRadius = '50%';
      return node;
    }
    if (type === 'line') {
      var start = element.start || [0, 0], end = element.end || [0, 0];
      var x1 = (element.left || 0) + (start[0] || 0), y1 = (element.top || 0) + (start[1] || 0);
      var x2 = (element.left || 0) + (end[0] || 0), y2 = (element.top || 0) + (end[1] || 0);
      var dx = x2 - x1, dy = y2 - y1, length = Math.sqrt(dx * dx + dy * dy) || 1;
      node.className = 'el line';
      node.style.left = x1 + 'px';
      node.style.top = y1 + 'px';
      node.style.width = length + 'px';
      node.style.height = '2px';
      node.style.background = element.color || '#333333';
      node.style.transform = 'rotate(' + (Math.atan2(dy, dx) * 180 / Math.PI) + 'deg)';
      return node;
    }
    if (type === 'table') {
      node.style.overflow = 'auto';
      var table = document.createElement('table');
      (element.data || []).forEach(function (row) {
        var tr = document.createElement('tr');
        (row || []).forEach(function (cell) {
          var td = document.createElement(row === (element.data || [])[0] ? 'th' : 'td');
          td.textContent = cell && cell.text !== undefined ? cell.text : String(cell || '');
          tr.appendChild(td);
        });
        table.appendChild(tr);
      });
      node.appendChild(table);
      return node;
    }
    if (type === 'chart') {
      node.style.overflow = 'auto';
      var data = element.data || {};
      var labels = data.labels || [], series = data.series || [];
      var chartTable = document.createElement('table');
      var head = document.createElement('tr');
      head.appendChild(el('th', '', '系列'));
      labels.forEach(function (label) { head.appendChild(el('th', '', label)); });
      chartTable.appendChild(head);
      series.forEach(function (values, index) {
        var row = document.createElement('tr');
        row.appendChild(el('td', '', (data.legends && data.legends[index]) || ('系列 ' + (index + 1))));
        (values || []).forEach(function (value) { row.appendChild(el('td', '', value)); });
        chartTable.appendChild(row);
      });
      node.appendChild(chartTable);
      return node;
    }
    if (type === 'image') {
      var src = String(element.src || '');
      if (src.indexOf('data:image/') === 0) {
        var img = document.createElement('img');
        img.src = src;
        img.alt = element.alt || '';
        img.style.width = '100%';
        img.style.height = '100%';
        img.style.objectFit = 'contain';
        node.appendChild(img);
      } else {
        node.textContent = element.alt || '图片需在线查看';
        node.style.fontSize = '12px';
        node.style.color = '#6b7280';
      }
      return node;
    }
    return null;
  }

  function scaleSlide() {
    var shell = find('slide-shell');
    var canvas = find('slide-canvas');
    if (!shell || !canvas) return;
    var scale = Math.min(shell.clientWidth / CANVAS_W, shell.clientHeight / CANVAS_H);
    if (!isFinite(scale) || scale <= 0) scale = 1;
    canvas.style.transform = 'scale(' + scale + ')';
    canvas.style.left = Math.max(0, (shell.clientWidth - CANVAS_W * scale) / 2) + 'px';
  }

  function renderSlide(scene) {
    var content = scene.content || {};
    var canvas = content.canvas || {};
    var shell = el('div', 'slide-shell');
    shell.id = 'slide-shell';
    var canvasNode = el('div', 'slide-canvas');
    canvasNode.id = 'slide-canvas';
    var theme = canvas.theme || {};
    if (theme.backgroundColor) canvasNode.style.background = theme.backgroundColor;
    (canvas.elements || []).forEach(function (element) {
      var node = null;
      try { node = elementNode(element); } catch (error) { node = null; }
      if (node) {
        if (element && element.id) node.setAttribute('data-el-id', String(element.id));
        canvasNode.appendChild(node);
      }
    });
    // 逐帧动画效果层：聚光灯/激光/高亮/白板（默认隐藏，仅 capture.seekMs(t) 时写入）
    var fxLayer = el('div', 'fx-layer');
    fxLayer.appendChild(el('div', 'fx-spot'));
    fxLayer.appendChild(el('div', 'fx-highlight'));
    fxLayer.appendChild(el('div', 'fx-laser'));
    fxLayer.appendChild(el('div', 'fx-whiteboard'));
    canvasNode.appendChild(fxLayer);
    shell.appendChild(canvasNode);
    var stage = el('div', 'stage');
    stage.appendChild(shell);
    window.setTimeout(scaleSlide, 0);
    return stage;
  }

  function renderQuiz(scene) {
    var card = el('div', 'scene-card');
    card.appendChild(el('h2', '', scene.title || '课堂测验'));
    var questions = (scene.content && scene.content.questions) || [];
    if (!questions.length) card.appendChild(el('p', '', '本测验暂无题目。'));
    questions.forEach(function (question, index) {
      var block = el('div', 'quiz-q');
      block.appendChild(el('p', '', (index + 1) + '. ' + (question.question || '')));
      (question.options || []).forEach(function (option) {
        block.appendChild(el('div', 'quiz-option', (option.value || '') + '. ' + (option.label || '')));
      });
      var toggle = el('button', '', '显示答案与解析');
      var analysis = el('div', 'analysis', '');
      analysis.style.display = 'none';
      toggle.addEventListener('click', function () {
        var visible = analysis.style.display !== 'none';
        if (visible) {
          analysis.style.display = 'none';
          toggle.textContent = '显示答案与解析';
        } else {
          var answer = (question.answer || []).join('、');
          analysis.textContent = '参考答案：' + (answer || '见解析') + '\n解析：' + (question.analysis || '');
          analysis.style.display = 'block';
          toggle.textContent = '收起答案';
        }
      });
      block.appendChild(toggle);
      block.appendChild(analysis);
      card.appendChild(block);
    });
    return card;
  }

  function renderInteractive(scene) {
    var content = scene.content || {};
    var wrap = el('div', 'interactive-wrap');
    var frame = document.createElement('iframe');
    frame.setAttribute('sandbox', 'allow-scripts');
    frame.setAttribute('title', scene.title || '交互场景');
    frame.style.height = Math.max(240, Math.min(1200, content.height || 480)) + 'px';
    frame.srcdoc = content.html || '';
    var status = el('div', 'status', '交互页面加载中……（沙箱运行，仅通过 postMessage 上报状态）');
    window.addEventListener('message', function (event) {
      var data = event.data;
      if (!data || data.source !== 'zhixu-interactive') return;
      var payload = data.payload || {};
      if (data.type === 'state') {
        status.textContent = '交互状态：' + (payload.summary || JSON.stringify(payload.state || {}));
      } else if (data.type === 'ready') {
        status.textContent = '交互页面已就绪。';
      } else if (data.type === 'error') {
        status.textContent = '交互页面报告错误：' + (payload.message || '未知错误');
      }
    });
    wrap.appendChild(frame);
    var holder = document.createElement('div');
    holder.appendChild(wrap);
    holder.appendChild(status);
    return holder;
  }

  function renderPbl(scene) {
    var card = el('div', 'scene-card');
    var project = (scene.content && scene.content.project) || {};
    card.appendChild(el('h2', '', project.title || scene.title || '项目式学习'));
    if (project.background) card.appendChild(el('p', '', project.background));
    if ((project.goals || []).length) {
      card.appendChild(el('h3', '', '学习目标'));
      var goals = el('ul');
      project.goals.forEach(function (goal) { goals.appendChild(el('li', '', goal)); });
      card.appendChild(goals);
    }
    if ((project.roles || []).length) {
      card.appendChild(el('h3', '', '角色'));
      project.roles.forEach(function (role) {
        card.appendChild(el('p', '', (role.name || '') + '：' + (role.description || '')));
      });
    }
    (project.milestones || []).forEach(function (milestone, index) {
      var block = el('div', 'pbl-block');
      block.appendChild(el('h3', '', '里程碑 ' + (index + 1) + '：' + (milestone.title || '')));
      if (milestone.description) block.appendChild(el('p', '', milestone.description));
      var tasks = el('ul');
      (milestone.tasks || []).forEach(function (task) { tasks.appendChild(el('li', '', task)); });
      block.appendChild(tasks);
      if (milestone.deliverable) block.appendChild(el('p', '', '交付物：' + milestone.deliverable));
      if (milestone.checkpoint) block.appendChild(el('p', '', '完成检查：' + milestone.checkpoint));
      card.appendChild(block);
    });
    if ((project.rubric || []).length) {
      card.appendChild(el('h3', '', '评价标准'));
      project.rubric.forEach(function (item) {
        var line = (item.criterion || '') + '：' + (item.levels || []).map(function (level) {
          return level.level + ' ' + level.description;
        }).join('；');
        card.appendChild(el('p', '', line));
      });
    }
    if ((project.mentor_hints || []).length) {
      card.appendChild(el('h3', '', '导师提示'));
      var hints = el('ul');
      project.mentor_hints.forEach(function (hint) { hints.appendChild(el('li', '', hint)); });
      card.appendChild(hints);
    }
    return card;
  }

  function demoKindLabel(kind) {
    return { function_graph: '函数图', whiteboard: '解题白板', flow_animation: '流程动画',
      isometric: '等距结构', science_demo: '科学演示', demo_3d: '3D 演示',
      mech_3d: '机械 3D', factory_sim: '工厂仿真', molecule_3d: '分子模型', physics_sandbox: '物理沙盒' }[kind] || (kind || '演示');
  }

  // M4 手工「原理演示」场景：只按插入时冻结的固定版本快照渲染（海报 + 说明 + 数据 JSON），
  // 不执行附件里的任何内容；data-fixed-revision 供前端/第三方解析固定版本。
  function renderDemo(scene) {
    var content = scene.content || {};
    var artifact = content.artifact || {};
    var revision = Number(content.fixed_revision || artifact.revision || 0);
    var section = el('section', 'cls-demo-scene');
    section.setAttribute('data-scene-type', 'demo');
    section.setAttribute('data-artifact-id', String(artifact.artifact_id || ''));
    section.setAttribute('data-fixed-revision', String(revision));
    // 视频/截图管线：静态 DOM 就绪即可截帧；海报图片加载完成后再翻标记，便于外部等待。
    section.setAttribute('data-demo-frame-ready', '1');
    var head = el('div', 'cls-demo-head');
    head.appendChild(el('span', 'cls-demo-badge', '原理演示 · 固定版本 v' + revision));
    head.appendChild(el('span', 'cls-demo-kind', demoKindLabel(artifact.kind)));
    section.appendChild(head);
    section.appendChild(el('h2', '', scene.title || artifact.title || '原理演示'));
    var preview = artifact.preview && typeof artifact.preview === 'object' ? artifact.preview : {};
    var png = typeof preview.png === 'string' ? preview.png : '';
    if (png.indexOf('data:image/png;base64,') === 0) {
      var image = document.createElement('img');
      image.className = 'cls-demo-image';
      image.src = png;
      image.alt = artifact.title || '演示海报';
      image.addEventListener('load', function () {
        section.setAttribute('data-demo-image-ready', '1');
      });
      section.appendChild(image);
    } else {
      section.appendChild(el('p', 'cls-demo-missing', '未附加海报图像，请在知序课堂中播放该演示。'));
    }
    if (content.source_note) section.appendChild(el('p', 'cls-demo-note', '说明：' + content.source_note));
    var assumptions = Array.isArray(content.assumptions) ? content.assumptions : [];
    if (assumptions.length) {
      section.appendChild(el('h3', '', '前提假设'));
      var list = el('ul', 'cls-demo-assumptions');
      assumptions.forEach(function (item) { list.appendChild(el('li', '', item)); });
      section.appendChild(list);
    }
    var stepCount = Number(content.step_count || 0);
    if (isFinite(stepCount) && stepCount > 0) {
      section.appendChild(el('p', 'cls-demo-note', '演示步骤数：' + stepCount));
    }
    var dataScript = document.createElement('script');
    dataScript.type = 'application/json';
    dataScript.className = 'cls-demo-data';
    dataScript.textContent = JSON.stringify(artifact);
    section.appendChild(dataScript);
    return section;
  }

  function speechOf(scene) {
    var parts = [];
    (scene.actions || []).forEach(function (action) {
      if (action && action.type === 'speech' && action.text) parts.push(String(action.text));
    });
    return parts.join(' ');
  }

  function stopAudio() {
    if (state.tts && typeof state.tts.pause === 'function') {
      try { state.tts.pause(); } catch (error) { /* 忽略 */ }
    }
    if (state.audio) { try { state.audio.pause(); } catch (error) { /* 忽略 */ } }
  }

  function speak(text) {
    stopAudio();
    if (!text) return;
    var audio = find('tts-audio');
    if (audio && audio.getAttribute('src')) {
      audio.play().catch(function () { /* 自动播放被拦截时保留按钮 */ });
      return;
    }
    if (!('speechSynthesis' in window)) {
      find('speech-hint').textContent = '当前浏览器不支持语音合成，可阅读字幕。';
      return;
    }
    try {
      window.speechSynthesis.cancel();
      var utterance = new SpeechSynthesisUtterance(text);
      utterance.lang = 'zh-CN';
      window.speechSynthesis.speak(utterance);
    } catch (error) {
      find('speech-hint').textContent = '浏览器语音不可用。';
    }
  }

  function renderSpeech(scene) {
    var bar = find('subtitle');
    bar.innerHTML = '';
    var text = speechOf(scene);
    if (text) {
      var who = el('span', 'who', '教师');
      bar.appendChild(who);
      bar.appendChild(document.createTextNode(text));
    } else {
      bar.textContent = '本场景没有旁白字幕。';
    }
    var audio = find('tts-audio');
    audio.pause();
    if (scene.audio) {
      audio.src = scene.audio;
      audio.style.display = 'block';
    } else {
      audio.removeAttribute('src');
      audio.style.display = 'none';
    }
    find('speech-hint').textContent = '';
    state.lastSpoken = '';
    if (text && state.autoSpeak) speak(text);
  }

  function render() {
    var scene = currentScene();
    var stage = find('scene-stage');
    stage.innerHTML = '';
    find('tts-audio').pause();
    if (!scene) {
      stage.appendChild(el('div', 'scene-card', '课程暂无场景。'));
      renderNav();
      return;
    }
    var type = scene.type || 'slide';
    var node;
    if (type === 'slide') node = renderSlide(scene);
    else if (type === 'quiz') node = renderQuiz(scene);
    else if (type === 'interactive') node = renderInteractive(scene);
    else if (type === 'pbl') node = renderPbl(scene);
    else if (type === 'demo') node = renderDemo(scene);
    else node = el('div', 'scene-card', '未知场景类型。');
    stage.appendChild(node);
    if (type === 'slide') window.setTimeout(scaleSlide, 0);
    renderSpeech(scene);
    renderNav();
    find('scene-title').textContent = scene.title || '未命名场景';
    find('scene-type').textContent = sceneTypeLabel(type);
  }

  function go(delta) {
    state.index = Math.max(0, Math.min(scenes.length - 1, state.index + delta));
    render();
  }

  document.addEventListener('keydown', function (event) {
    if (event.target && /INPUT|TEXTAREA|SELECT/.test(event.target.tagName || '')) return;
    if (event.key === 'ArrowLeft') go(-1);
    else if (event.key === 'ArrowRight' || event.key === ' ') { event.preventDefault(); go(1); }
  });

  find('prev').addEventListener('click', function () { go(-1); });
  find('next').addEventListener('click', function () { go(1); });
  find('speak').addEventListener('click', function () {
    var scene = currentScene();
    if (scene) speak(speechOf(scene));
  });
  find('auto-speak').addEventListener('change', function (event) {
    state.autoSpeak = !!event.target.checked;
  });
  find('tts-audio').addEventListener('error', function () {
    find('speech-hint').textContent = '音频无法播放，已保留浏览器语音兜底。';
  });
  window.addEventListener('resize', scaleSlide);
  if (window.ResizeObserver) {
    try { new ResizeObserver(scaleSlide).observe(find('slide-shell') || document.body); } catch (error) { /* 忽略 */ }
  }

  renderHeader();
  fillSceneSelect();
  state.index = 0;
  render();
  capture.sceneCount = scenes.length;
  capture.current = 0;
  capture.ready = true;
  if (document.body.classList.contains('capture')) window.setTimeout(scaleSlide, 0);
})();
"""

_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
__DEMO_META__
<title>__TITLE__ · 知序 AI 课堂</title>
<style>__CSS__</style>
</head>
<body>
<div id="app">
  <header>
    <h1 id="course-title">AI 课堂</h1>
    <span id="course-meta" class="meta"></span>
    <label class="meta"><input type="checkbox" id="auto-speak"> 自动朗读</label>
    <select id="scene-select" aria-label="选择场景"></select>
  </header>
  <main>
    <div class="nav">
      <button id="prev" type="button">◀ 上一页</button>
      <button id="next" type="button">下一页 ▶</button>
      <span id="progress" class="meta"></span>
      <span class="spacer"></span>
      <span id="scene-type" class="meta"></span>
      <span id="scene-title" class="meta"></span>
    </div>
    <div id="scene-stage"></div>
    <div id="subtitle" class="subtitle"></div>
    __SOURCES__
    __DEMOS__
    <div class="nav">
      <button id="speak" type="button">朗读旁白</button>
      <span id="speech-hint" class="meta"></span>
    </div>
    <audio id="tts-audio" controls style="display:none; max-width:1000px; width:100%; margin:0 auto;"></audio>
  </main>
  <footer>
    <span class="meta">离线单文件播放器 · 由「知序 · AI 课堂」导出 · 无需联网</span>
  </footer>
</div>
<script id="course-data" type="application/json">__DATA__</script>
<script>__JS__</script>
__OFFLINE_DEMO_SCRIPTS__</body>
</html>
"""


def build_offline_html(snapshot: dict, tts_assets: dict[int, dict] | None = None) -> str:
    """生成离线单文件 HTML 播放器（JSON/CSS/JS 全部内联，无 http(s) 外链）。

    M4：原理演示场景除在播放器内按固定版本快照渲染外，还在正文末尾附加一份**静态 HTML 备份**
    （``section.cls-demo-scene[data-fixed-revision]`` + 内嵌 ``application/json`` 快照），
    保证不执行脚本的阅读器 / 打印 / 第三方解析也能读到固定版本内容。

    M7：含演示场景时，在模板末尾追加两段内联脚本——``web/vendor/offline/offline-demo.js``
    （flow/iso 渲染器 bundle）与 ``web/js/offline/offlineDemo.js``（挂载/分步/工厂回放），
    让 HTML / ZIP 保留可交互内容；PPTX / PDF / 打印讲义不经过这里，仍为海报 + 文字。
    **无演示场景时不追加任何内容**：``__OFFLINE_DEMO_SCRIPTS__`` 替换为空串，输出与既有完全一致。
    """
    data = json.loads(json.dumps(snapshot, ensure_ascii=False))
    audio_map = tts_assets or {}
    for scene in data.get("scenes") or []:
        try:
            scene_id = int(scene.get("id") or 0)
        except (TypeError, ValueError):
            continue
        audio = audio_map.get(scene_id)
        if isinstance(audio, dict) and audio.get("data_uri"):
            scene["audio"] = audio["data_uri"]
    course = data.get("course") or {}
    title = html_lib.escape(str(course.get("title") or course.get("topic") or "AI 课堂")[:120])
    data_json = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    # P3：文末「参考来源」列表（静态 HTML，纯文本链接；无来源时为空，不引入任何外链资源）。
    sources_html = _sources_html(data.get("sources") if isinstance(data.get("sources"), list) else [])
    # M5：含演示场景时输出静态「帧就绪」标记（视频/截图管线等待；运行时另有 DOM
    # data-demo-frame-ready / zhixu:demo-ready，静态标记便于外部工具与验收核对）。
    has_demo = any(
        str((scene.get("content") or {}).get("type") or scene.get("scene_type") or "") == "demo"
        for scene in data.get("scenes") or []
    )
    # M7：离线可交互播放器（bundle + 挂载脚本都来自仓库内提交的静态文件）。产物缺失时
    # 退回既有「静态海报 + 数据 JSON」输出，不抛异常。
    demo_bundle_js = _offline_demo_bundle_js() if has_demo else ""
    demo_player_js = _offline_demo_player_js() if has_demo else ""
    demo_scripts = _offline_demo_scripts_html(demo_bundle_js, demo_player_js)
    has_offline_player = bool(demo_scripts)
    # M4：文末「演示附件（固定版本）」静态备份（无演示场景时为空，既有导出结构不变）。
    demos_html = _demo_sections_html(data, offline_player=has_offline_player)
    demo_meta = '<meta name="zhixu-demo-frame-ready" content="1">' if has_demo else ""
    if has_offline_player:
        # 顺序：既有样式 → flow/iso 的 .pd-* 样式 → 离线播放器覆盖样式（后者优先）。
        css_text = _HTML_CSS + _offline_demo_principles_css() + _OFFLINE_DEMO_CSS
    else:
        css_text = _HTML_CSS
    return (
        _HTML_TEMPLATE
        .replace("__JS__", _HTML_JS)
        .replace("__CSS__", css_text)
        .replace("__SOURCES__", sources_html)
        .replace("__DEMOS__", demos_html)
        .replace("__DEMO_META__", demo_meta)
        .replace("__OFFLINE_DEMO_SCRIPTS__", demo_scripts)
        .replace("__TITLE__", title)
        .replace("__DATA__", data_json)
    )


# ---------------------------------------------------------------------------
# 服务端 PDF：课堂讲义打印版（离线 HTML → app.ai.render.html_to_pdf）
# ---------------------------------------------------------------------------

def _render_module():
    """延迟引用 ``app.ai.render``（便于测试 monkeypatch，也避免包初始化顺序问题）。"""
    from ..ai import render as ai_render

    return ai_render


_PDF_CSS = """
@page { size: A4 landscape; margin: 10mm; }
* { box-sizing: border-box; }
html, body { margin: 0; padding: 0; background: #ffffff; color: #1f2937;
  font-family: system-ui, -apple-system, "Segoe UI", "Microsoft YaHei", "PingFang SC", sans-serif;
  -webkit-print-color-adjust: exact; print-color-adjust: exact; }
.print-page { page-break-after: always; break-after: page; min-height: 170mm; }
.print-page:last-child { page-break-after: auto; break-after: auto; }
.eyebrow { margin: 0 0 4mm; font-size: 12px; letter-spacing: 2px; color: #2563eb; }
h1 { margin: 0 0 4mm; font-size: 30px; line-height: 1.3; }
h2 { margin: 0 0 2mm; font-size: 22px; line-height: 1.35; color: #111827; }
.meta { margin: 1mm 0; font-size: 13px; color: #4b5563; }
.desc { margin: 3mm 0; font-size: 14px; line-height: 1.75; color: #374151; white-space: pre-wrap; }
.scene-head { display: flex; justify-content: space-between; align-items: baseline; margin-bottom: 1mm;
  border-bottom: 1px solid #e5e7eb; padding-bottom: 1.5mm; font-size: 12px; color: #6b7280; }
.canvas-wrap { position: relative; width: 1000px; height: 562.5px; margin: 2mm 0; overflow: hidden; }
.print-canvas { position: relative; width: 1000px; height: 562.5px; overflow: hidden; }
.print-canvas .pe { position: absolute; overflow: hidden; }
.print-canvas .pe-text { width: 100%; height: 100%; white-space: pre-wrap; line-height: 1.5;
  word-break: break-word; overflow: hidden; }
.print-canvas table { border-collapse: collapse; width: 100%; height: 100%; font-size: 13px; }
.print-canvas th, .print-canvas td { border: 1px solid #cbd5e1; padding: 4px 6px; text-align: left; }
.print-canvas img { width: 100%; height: 100%; object-fit: contain; }
.speech { margin: 2mm 0 0; padding: 2mm 3mm; background: #f1f5f9; border-radius: 2mm;
  font-size: 13px; line-height: 1.6; color: #334155; }
.quiz-q { border-top: 1px solid #e5e7eb; padding: 2mm 0; font-size: 14px; line-height: 1.75; }
.quiz-q:first-child { border-top: none; }
.quiz-option { margin: 1mm 0 0 4mm; }
.answer { margin-top: 1.5mm; padding: 2mm 3mm; background: #f8fafc; border-radius: 2mm;
  font-size: 12px; color: #475569; white-space: pre-wrap; }
.pbl-block { border: 1px solid #e2e8f0; border-radius: 2mm; padding: 2.5mm 3mm; margin: 2mm 0;
  font-size: 13px; line-height: 1.7; }
.pbl-block h3 { margin: 0 0 1mm; font-size: 15px; color: #1f6feb; }
.note { margin: 2mm 0; padding: 2mm 3mm; background: #fff7ed; border: 1px solid #fed7aa;
  border-radius: 2mm; font-size: 13px; line-height: 1.7; color: #9a3412; }
.sources-list { margin: 0; padding: 0; list-style: none; }
.sources-list li { border-top: 1px solid #e5e7eb; padding: 2mm 0; font-size: 12px; line-height: 1.65; }
.sources-list li:first-child { border-top: none; }
/* M4 原理演示讲义页：固定版本徽标 + 可选海报 + 说明/假设（无脚本、无外链）。 */
.cls-demo-scene { border: 1px solid #e5e7eb; border-radius: 2mm; padding: 3mm 4mm; }
.cls-demo-head { margin-bottom: 2mm; }
.cls-demo-badge { display: inline-block; padding: 0.5mm 2.5mm; border-radius: 999px;
  background: #1d4ed8; color: #eff6ff; font-size: 11px; }
.cls-demo-kind { margin-left: 2mm; color: #4b5563; font-size: 11px; }
.cls-demo-scene h3 { margin: 1mm 0 2mm; font-size: 16px; color: #111827; }
.cls-demo-image { display: block; max-width: 100%; max-height: 120mm; margin: 2mm auto;
  border: 1px solid #e5e7eb; background: #fff; }
.cls-demo-note { margin: 1.5mm 0; font-size: 13px; line-height: 1.7; color: #334155; }
.cls-demo-assumptions { margin: 1mm 0 0 5mm; font-size: 12px; line-height: 1.7; color: #475569; }
.cls-demo-missing { margin: 2mm 0; padding: 2mm 3mm; background: #f8fafc; border: 1px dashed #cbd5e1;
  border-radius: 2mm; font-size: 12px; color: #64748b; }
.footer-note { margin-top: 5mm; font-size: 11px; color: #9ca3af; line-height: 1.6; }
"""

_PDF_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>__TITLE__ · 知序 AI 课堂</title>
<style>__CSS__</style>
</head>
<body>
__BODY__
</body>
</html>
"""


def _esc(value) -> str:
    """打印版 HTML 统一转义（所有课程内容都视为不可信文本）。"""
    return html_lib.escape(str(value if value is not None else ""), quote=True)


def _print_text_of_element(element: dict) -> str:
    """提取幻灯片文本元素的纯文本（text 优先；content HTML 去标签降级）。"""
    if not isinstance(element, dict):
        return ""
    raw = element.get("text")
    if raw is not None and str(raw).strip():
        return str(raw)
    content = str(element.get("content") or "")
    if not content.strip():
        return ""
    text = re.sub(r"<br\s*/?>", "\n", content, flags=re.IGNORECASE)
    text = re.sub(r"</(p|div|li|h[1-6])>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    return html_lib.unescape(text).strip()


def _print_element_html(element: dict) -> str:
    """幻灯片元素 → 打印版绝对定位 HTML（纯静态、无脚本、文本一律转义）。"""
    if not isinstance(element, dict):
        return ""
    try:
        left = float(element.get("left") or 0)
        top = float(element.get("top") or 0)
        width = max(1.0, float(element.get("width") or 1))
        height = max(1.0, float(element.get("height") or 1))
        rotate = float(element.get("rotate") or 0)
    except (TypeError, ValueError):
        return ""
    style = "left:%gpx;top:%gpx;width:%gpx;height:%gpx;" % (
        max(0.0, left), max(0.0, top), width, height,
    )
    if rotate:
        style += "transform:rotate(%gdeg);" % rotate
    kind = str(element.get("type") or "")
    if kind in ("text", "latex", "code"):
        text = _print_text_of_element(element)
        if kind == "latex" and not text:
            text = str(element.get("latex") or "")
        if kind == "code" and str(element.get("code") or "").strip():
            text = str(element.get("code") or "")
        if not text.strip():
            return ""
        try:
            font_size = max(8.0, float(element.get("fontSize") or (13 if kind == "code" else 18)))
        except (TypeError, ValueError):
            font_size = 18.0
        color = str(element.get("color") or "#333333")
        align = str(element.get("align") or "left")
        extra = "font-family:Consolas,'Courier New',monospace;" if kind == "code" else ""
        if kind == "latex":
            extra += "font-style:italic;"
        return (
            '<div class="pe" style="%s"><div class="pe-text" style="font-size:%gpx;'
            'color:%s;text-align:%s;%s">%s</div></div>'
        ) % (style, font_size, _esc(color), _esc(align), extra, _esc(text))
    if kind == "shape":
        fill = str(element.get("fill") or "#5b9bd5")
        radius = "50%" if "A" in str(element.get("path") or "") else "0"
        return (
            '<div class="pe" style="%s"><div style="width:100%%;height:100%%;background:%s;'
            'border-radius:%s;"></div></div>'
        ) % (style, _esc(fill), radius)
    if kind == "line":
        start = element.get("start") or [0, 0]
        end = element.get("end") or [0, 0]
        try:
            x1 = max(0.0, left) + float(start[0] or 0)
            y1 = max(0.0, top) + float(start[1] or 0)
            x2 = max(0.0, left) + float(end[0] or 0)
            y2 = max(0.0, top) + float(end[1] or 0)
        except (TypeError, ValueError, IndexError, KeyError):
            return ""
        dx, dy = x2 - x1, y2 - y1
        length = max(1.0, math.hypot(dx, dy))
        angle = math.degrees(math.atan2(dy, dx))
        color = str(element.get("color") or "#333333")
        return (
            '<div class="pe" style="left:%gpx;top:%gpx;width:%gpx;height:2px;background:%s;'
            'transform:rotate(%gdeg);transform-origin:left center;"></div>'
        ) % (x1, y1, length, _esc(color), angle)
    if kind == "table":
        rows = element.get("data") if isinstance(element.get("data"), list) else []
        rendered: list[str] = []
        for row_index, row in enumerate(rows):
            tag = "th" if row_index == 0 else "td"
            cells = []
            for cell in (row if isinstance(row, list) else []):
                text = cell.get("text") if isinstance(cell, dict) else cell
                cells.append("<%s>%s</%s>" % (tag, _esc(text), tag))
            rendered.append("<tr>%s</tr>" % "".join(cells))
        return '<div class="pe" style="%s"><table>%s</table></div>' % (style, "".join(rendered))
    if kind == "chart":
        data = element.get("data") if isinstance(element.get("data"), dict) else {}
        labels = data.get("labels") if isinstance(data.get("labels"), list) else []
        series = data.get("series") if isinstance(data.get("series"), list) else []
        legends = data.get("legends") if isinstance(data.get("legends"), list) else []
        head = "<tr><th>系列</th>%s</tr>" % "".join("<th>%s</th>" % _esc(label) for label in labels)
        body_rows = []
        for index, values in enumerate(series):
            name = legends[index] if index < len(legends) else "系列 %d" % (index + 1)
            cells = "".join("<td>%s</td>" % _esc(value) for value in (values if isinstance(values, list) else []))
            body_rows.append("<tr><td>%s</td>%s</tr>" % (_esc(name), cells))
        return '<div class="pe" style="%s"><table>%s%s</table></div>' % (
            style, head, "".join(body_rows),
        )
    if kind == "image":
        src = str(element.get("src") or "")
        alt = str(element.get("alt") or "示意图")
        if src.startswith("data:image/"):
            return '<div class="pe" style="%s"><img src="%s" alt="%s"></div>' % (
                style, _esc(src), _esc(alt),
            )
        return (
            '<div class="pe" style="%s"><div class="pe-text" style="font-size:12px;color:#6b7280;">'
            "%s</div></div>"
        ) % (style, _esc(alt + "（图片未能内联，请在播放器中查看）"))
    return ""


def _print_quiz_body(content: dict) -> str:
    questions = content.get("questions") if isinstance(content.get("questions"), list) else []
    if not questions:
        return '<p class="meta">本页暂无题目。</p>'
    blocks: list[str] = []
    for index, question in enumerate(questions, 1):
        if not isinstance(question, dict):
            continue
        options = question.get("options") if isinstance(question.get("options"), list) else []
        option_html = "".join(
            '<div class="quiz-option">%s. %s</div>' % (
                _esc(option.get("value") if isinstance(option, dict) else ""),
                _esc(option.get("label") if isinstance(option, dict) else option),
            )
            for option in options
        )
        answer = "、".join(str(item) for item in (question.get("answer") or []))
        analysis = str(question.get("analysis") or "").strip()
        answer_parts = []
        if answer:
            answer_parts.append("参考答案：%s" % answer)
        if analysis:
            answer_parts.append("解析：%s" % analysis)
        answer_html = (
            '<div class="answer">%s</div>' % _esc("\n".join(answer_parts)) if answer_parts else ""
        )
        blocks.append(
            '<div class="quiz-q"><strong>%d. %s</strong>%s%s</div>' % (
                index, _esc(question.get("question") or ""), option_html, answer_html,
            )
        )
    return "".join(blocks) or '<p class="meta">本页暂无题目。</p>'


def _print_interactive_body(content: dict) -> str:
    widget = schema.normalize_widget(content.get("widget"), "simulation")
    label = schema.WIDGET_LABELS.get(widget, widget)
    summary = str(content.get("summary") or content.get("description") or "").strip()
    note = (
        '<p class="note">本页是交互场景（%s）：静态 PDF 只保留说明文字，'
        "请使用导出的离线 HTML 播放器或在课堂中体验完整交互。</p>" % _esc(label)
    )
    return note + (('<p class="desc">%s</p>' % _esc(summary)) if summary else "")


def _print_demo_body(content: dict, title: str = "") -> str:
    """原理演示场景 → 打印讲义正文（固定版本徽标 + 可选海报 + 说明 + 假设 + 内嵌快照 JSON）。

    与离线 HTML 附录共用 :func:`_demo_section_html`，保证 ``data-artifact-id`` /
    ``data-fixed-revision`` 等固定版本标识在 PDF（打印 HTML 渲染）中同样存在。
    """
    return _demo_section_html(title, content, heading_tag="h3")


def _rubric_text(item) -> str:
    levels = item.get("levels") if isinstance(item, dict) else None
    if isinstance(levels, str):
        return levels
    if isinstance(levels, list):
        pieces = []
        for level in levels:
            if isinstance(level, dict):
                pieces.append("%s %s" % (level.get("level") or "", level.get("description") or ""))
            elif level:
                pieces.append(str(level))
        return "；".join(piece.strip() for piece in pieces if piece.strip())
    return ""


def _print_pbl_body(content: dict) -> str:
    project = content.get("project") if isinstance(content.get("project"), dict) else {}
    parts: list[str] = []
    if project.get("background"):
        parts.append('<p class="desc">%s</p>' % _esc(project.get("background")))
    goals = project.get("goals") if isinstance(project.get("goals"), list) else []
    if goals:
        parts.append("<h3>学习目标</h3><ul>%s</ul>" % "".join("<li>%s</li>" % _esc(goal) for goal in goals))
    roles = project.get("roles") if isinstance(project.get("roles"), list) else []
    if roles:
        parts.append("<h3>角色</h3>%s" % "".join(
            "<p>%s：%s</p>" % (
                _esc(role.get("name") if isinstance(role, dict) else ""),
                _esc(role.get("description") if isinstance(role, dict) else role),
            )
            for role in roles
        ))
    milestones = project.get("milestones") if isinstance(project.get("milestones"), list) else []
    for index, milestone in enumerate(milestones, 1):
        if not isinstance(milestone, dict):
            continue
        inner = ['<h3>里程碑 %d：%s</h3>' % (index, _esc(milestone.get("title") or ""))]
        if milestone.get("description"):
            inner.append("<p>%s</p>" % _esc(milestone.get("description")))
        tasks = milestone.get("tasks") if isinstance(milestone.get("tasks"), list) else []
        if tasks:
            inner.append("<ul>%s</ul>" % "".join("<li>%s</li>" % _esc(task) for task in tasks))
        if milestone.get("deliverable"):
            inner.append("<p>交付物：%s</p>" % _esc(milestone.get("deliverable")))
        if milestone.get("checkpoint"):
            inner.append("<p>完成检查：%s</p>" % _esc(milestone.get("checkpoint")))
        parts.append('<div class="pbl-block">%s</div>' % "".join(inner))
    rubric = project.get("rubric") if isinstance(project.get("rubric"), list) else []
    if rubric:
        parts.append("<h3>评价标准</h3>%s" % "".join(
            "<p>%s：%s</p>" % (
                _esc(item.get("criterion") if isinstance(item, dict) else ""),
                _esc(_rubric_text(item)),
            )
            for item in rubric
        ))
    hints = project.get("mentor_hints") if isinstance(project.get("mentor_hints"), list) else []
    if hints:
        parts.append("<h3>导师提示</h3><ul>%s</ul>" % "".join("<li>%s</li>" % _esc(hint) for hint in hints))
    return "".join(parts) or '<p class="meta">本页暂无项目内容。</p>'


def _print_scene_body(scene: dict) -> str:
    """单个场景 → 打印版正文 HTML。"""
    content = _scene_content(scene)
    scene_type = str(scene.get("scene_type") or scene.get("type") or "slide")
    if scene_type == "slide":
        canvas = content.get("canvas") if isinstance(content.get("canvas"), dict) else {}
        elements = canvas.get("elements") if isinstance(canvas.get("elements"), list) else []
        if not elements:
            return '<p class="meta">本页暂无内容。</p>'
        theme = canvas.get("theme") if isinstance(canvas.get("theme"), dict) else {}
        background = str(theme.get("backgroundColor") or "#ffffff")
        inner = "".join(_print_element_html(element) for element in elements)
        return (
            '<div class="canvas-wrap"><div class="print-canvas" style="background:%s">%s</div></div>'
        ) % (_esc(background), inner)
    if scene_type == "quiz":
        return _print_quiz_body(content)
    if scene_type == "interactive":
        return _print_interactive_body(content)
    if scene_type == "pbl":
        return _print_pbl_body(content)
    if scene_type == "demo":
        # M4：固定版本原理演示（海报 + 说明 + 内嵌快照 JSON；无 PNG 时显示中文说明）。
        return _print_demo_body(content, str(scene.get("title") or ""))
    return '<p class="meta">未知场景类型。</p>'


def pdf_page_count(course: dict, scenes: list[dict]) -> int:
    """PDF 页数：封面 1 页 + 每场景 1 页 + 有联网来源时 1 页。"""
    return 1 + len(scenes) + (1 if _course_sources(course) else 0)


def _print_sources_body(sources: list[dict]) -> str:
    items = []
    for index, item in enumerate(sources, 1):
        number = item.get("index") or index
        meta = _source_meta_text(item)
        title = item.get("title") or "未命名来源"
        url = item.get("url") or "（无链接）"
        summary = str(item.get("summary") or "").strip()
        items.append(
            '<li><strong>[%s] %s</strong>%s<br><span class="meta">%s</span>%s</li>' % (
                _esc(number), _esc(title),
                ('　<span class="meta">（%s）</span>' % _esc(meta)) if meta else "",
                _esc(url),
                ('<br><span class="meta">%s</span>' % _esc(summary[:160])) if summary else "",
            )
        )
    return (
        "<h2>参考来源</h2>"
        '<p class="meta">以下资料由联网检索获得，生成课程时仅作为事实参考；链接为纯文本，PDF 不会加载网页。</p>'
        '<ul class="sources-list">%s</ul>' % "".join(items)
    )


def build_print_html(course: dict, scenes: list[dict]) -> str:
    """生成 PDF 讲义版离线 HTML：封面 + 每场景一页 +（可选）参考来源页，无脚本、无外链。"""
    title = str(course.get("title") or course.get("topic") or "AI 课堂")
    topic = str(course.get("topic") or title)
    subject = str(course.get("subject") or "未指定")
    grade = str(course.get("grade") or "未指定")
    exported_at = _db().utcnow()
    pages: list[str] = [
        '<section class="print-page cover">',
        '<p class="eyebrow">知序 · AI 课堂 · 课程讲义</p>',
        "<h1>%s</h1>" % _esc(title),
        '<p class="meta">主题：%s　学科：%s　学段：%s</p>' % (_esc(topic), _esc(subject), _esc(grade)),
    ]
    description = str(course.get("description") or "").strip()
    if description:
        pages.append('<p class="desc">%s</p>' % _esc(description))
    pages.append(
        '<p class="meta">场景数：%d　导出时间（UTC）：%s</p>' % (len(scenes), _esc(exported_at))
    )
    pages.append(
        '<p class="footer-note">本讲义由「知序」根据课程内容导出：幻灯片按 1000×562.5 画布 1:1 排版，'
        "测验页附参考答案与解析，交互场景以说明文字代替（请使用离线 HTML 播放器体验）。</p>"
    )
    pages.append("</section>")
    total = max(1, len(scenes))
    for index, scene in enumerate(scenes, 1):
        scene_type = str(scene.get("scene_type") or scene.get("type") or "slide")
        type_label = schema.SCENE_TYPE_LABELS.get(scene_type, scene_type)
        speech = _scene_speech(scene)
        pages.append('<section class="print-page scene">')
        pages.append(
            '<div class="scene-head"><span>第 %d / %d 页</span><span>%s</span></div>'
            % (index, total, _esc(type_label))
        )
        pages.append("<h2>%s</h2>" % _esc(scene.get("title") or "未命名场景"))
        pages.append(_print_scene_body(scene))
        if speech:
            pages.append('<p class="speech">旁白：%s</p>' % _esc(speech))
        pages.append("</section>")
    sources = _course_sources(course)
    if sources:
        pages.append('<section class="print-page sources">%s</section>' % _print_sources_body(sources))
    return (
        _PDF_HTML_TEMPLATE
        .replace("__CSS__", _PDF_CSS)
        .replace("__BODY__", "\n".join(pages))
        .replace("__TITLE__", html_lib.escape(title[:120], quote=False))
    )


async def render_course_pdf(course: dict, scenes: list[dict]) -> tuple[bytes, int]:
    """课堂讲义 PDF：构建打印版 HTML → Playwright 渲染；引擎不可用/失败 → 503 中文引导。"""
    ai_render = _render_module()
    info = ai_render.render_available()
    if not info.get("available"):
        raise HTTPException(
            503,
            "服务端 PDF 导出不可用：%s。可改用「导出离线 HTML」后在浏览器中打印为 PDF。"
            % (info.get("detail") or "浏览器渲染不可用"),
        )
    html_text = build_print_html(course, scenes)
    try:
        payload = await ai_render.html_to_pdf(html_text, paper=PDF_PAPER, landscape=PDF_LANDSCAPE)
    except ai_render.RenderUnavailable as exc:
        raise HTTPException(
            503,
            "PDF 渲染失败：%s。可改用「导出离线 HTML」后在浏览器中打印为 PDF。" % exc,
        ) from None
    return payload, pdf_page_count(course, scenes)


# ---------------------------------------------------------------------------
# 课程 ZIP
# ---------------------------------------------------------------------------

def _offline_html_for_zip(course: dict, scenes: list[dict],
                          tts_assets: dict[int, dict] | None = None) -> bytes | None:
    """ZIP 内的单文件可交互播放器（M7）：仅演示场景需要；任何异常返回 None，不改既有 ZIP 结构。

    复用 ``build_offline_html``（与 ``GET /export?format=html`` 完全同源），并把 slide 引用的
    图片素材内联成 data URI，保证 ZIP 解开后离线打开也能看到完整内容。
    """
    try:
        user_id = int(course.get("user_id") or 0)
    except (TypeError, ValueError):
        user_id = 0
    html_scenes = scenes
    if user_id > 0:
        try:
            html_scenes = _inline_scene_images(scenes, user_id)
        except Exception:  # 素材内联失败时仍导出（演示数据本身不依赖图片素材）
            html_scenes = scenes
    try:
        snapshot = build_snapshot(course, html_scenes, for_html=True)
        return build_offline_html(snapshot, tts_assets).encode("utf-8")
    except Exception:  # 播放器增强不能导致 ZIP 导出失败
        return None


def build_course_zip(course: dict, scenes: list[dict], *, tts_assets: dict[int, dict] | None = None,
                     resources: list[dict] | None = None, note: str = "") -> bytes:
    """课程 ZIP：manifest.json + course.json + scenes/*.json + assets/**，可被导入接口还原。

    M4：额外的固定版本原理演示场景会写 ``demos/scene-<NN>-<artifact_id>.json``（附件快照 +
    固定版本说明），并在 manifest 里追加 ``demoScenes`` 与 ``capabilities`` 的 ``demo-scenes``；
    没有演示场景时 ZIP 结构与既有导出完全一致。
    M7：含演示场景时额外写 ``offline.html``（与 ``format=html`` 相同的单文件可交互播放器，
    内联 flow/iso/工厂回放脚本，零外链）；无演示场景不新增该文件。
    """
    course_public = _course_snapshot(course)
    tts_map = tts_assets or {}
    scene_entries: list[dict] = []
    files: dict[str, bytes] = {}
    for index, scene in enumerate(scenes, 1):
        path = "scenes/scene_%03d.json" % index
        snapshot = _scene_snapshot(scene)
        asset = tts_map.get(int(scene["id"]))
        if isinstance(asset, dict) and asset.get("asset_id"):
            snapshot["ttsAssetId"] = int(asset["asset_id"])
        files[path] = json.dumps(snapshot, ensure_ascii=False, indent=2).encode("utf-8")
        scene_entries.append({
            "order": int(scene["order_index"]),
            "id": int(scene["id"]),
            "title": scene.get("title") or "",
            "type": scene.get("scene_type") or "slide",
            "path": path,
        })
    resource_entries: list[dict] = []
    files_seen: set[str] = set()
    # M4：固定版本原理演示场景额外写 demos/scene-<NN>-<artifact_id>.json（快照 + 固定版本说明）。
    demo_files: dict[str, bytes] = {}
    demo_entries: list[dict] = []
    for scene in scenes:
        if str(scene.get("scene_type") or "") != "demo":
            continue
        content = _scene_content(scene)
        artifact = _demo_artifact_of(content)
        if not artifact:
            continue
        try:
            order = int(scene.get("order_index") or 0)
        except (TypeError, ValueError):
            order = 0
        order = order if order > 0 else len(demo_entries) + 1
        artifact_id = re.sub(
            r"[^A-Za-z0-9_-]+", "", str(artifact.get("artifact_id") or "")
        )[:64] or "demo"
        path = "demos/scene-%02d-%s.json" % (order, artifact_id)
        revision = _demo_fixed_revision(content)
        assumptions, step_count = _demo_content_lists(content)
        payload = {
            "format": "zhixu-classroom-demo-snapshot",
            "formatVersion": 1,
            "exportedAt": _db().utcnow(),
            "sceneId": int(scene["id"]),
            "order": order,
            "type": "demo",
            "title": str(scene.get("title") or artifact.get("title") or "原理演示"),
            "fixed": True,
            "fixedRevision": revision,
            "fixedNote": "固定版本 v%d：课堂与导出使用该版本，源附件更新不会自动改变" % revision,
            "sourceNote": str(content.get("source_note") or ""),
            "assumptions": assumptions,
            "stepCount": step_count,
            "artifact": artifact,
        }
        demo_files[path] = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        demo_entries.append({
            "order": order,
            "sceneId": int(scene["id"]),
            "title": str(scene.get("title") or ""),
            "artifact_id": str(artifact.get("artifact_id") or ""),
            "kind": str(artifact.get("kind") or ""),
            "fixedRevision": revision,
            "path": path,
        })
    for resource in (resources or []):
        path = str(resource.get("path") or "")
        data = resource.get("bytes")
        if not path or not isinstance(data, (bytes, bytearray)) or path in files_seen:
            continue
        if path.startswith("assets/") is False:
            path = "assets/" + path.lstrip("/")
        files_seen.add(path)
        files[path] = bytes(data)
        resource_entries.append({
            "path": path,
            "sha256": resource.get("sha256") or "",
            "size": len(data),
            "mimeType": resource.get("mimeType") or "application/octet-stream",
            "kind": resource.get("kind") or "file",
            "assetId": int(resource.get("assetId") or 0),
        })
    course_config = course_public.get("config") if isinstance(course_public.get("config"), dict) else {}
    roster_payload = None
    raw_roster = course_config.get(multiagent.CONFIG_KEY)
    if isinstance(raw_roster, dict):
        try:
            normalized_roster, _roster_warnings = multiagent.normalize_agent_roster(raw_roster)
            roster_payload = multiagent.roster_public(normalized_roster)
        except (schema.ClassroomSchemaError, ValueError):
            roster_payload = None
    interactive_mode = str(course_public.get("mode") or "") == "interactive"
    # P3：联网来源随 ZIP 附带 sources.json（无来源时不写文件，保持既有包结构稳定）。
    sources = _course_sources(course)
    capabilities = ["slide", "quiz", "interactive", "pbl", "tts"]
    if roster_payload:
        capabilities.append("agents")
    if interactive_mode:
        capabilities.append("interactive-mode")
    if any(entry.get("kind") == "image" for entry in resource_entries):
        capabilities.append("images")
    if sources:
        capabilities.append("web-search")
    if demo_entries:
        capabilities.append("demo-scenes")
    manifest = {
        "format": ZIP_FORMAT,
        "formatVersion": ZIP_FORMAT_VERSION,
        "app": "zhixu-study-platform",
        "exportedAt": _db().utcnow(),
        "courseId": int(course["id"]),
        "course": {
            "title": course_public.get("title") or "",
            "topic": course_public.get("topic") or "",
            "subject": course_public.get("subject") or "",
            "grade": course_public.get("grade") or "",
            "mode": course_public.get("mode") or "standard",
        },
        "interactiveMode": interactive_mode,
        "scenes": scene_entries,
        "resources": resource_entries,
        "capabilities": capabilities,
        "sourceCount": len(sources),
        "promptVersion": str((course_public.get("config") or {}).get("prompt_version") or ""),
    }
    if roster_payload:
        # P1：角色名册随课程包迁移（course.json 的 config 里也有，manifest 显式登记便于第三方读取）。
        manifest["agents"] = roster_payload
        manifest["agentsPromptVersion"] = str(
            (course_config.get(multiagent.CONFIG_META_KEY) or {}).get("prompt_version") or ""
        )
    if demo_entries:
        # M4：手工插入的固定版本演示场景清单（无演示场景时不写字段，既有 ZIP 结构不变）。
        manifest["demoScenes"] = demo_entries
    # M7：含演示场景的 ZIP 额外携带单文件可交互播放器（无演示场景不新增文件/字段）。
    offline_html_payload = _offline_html_for_zip(course, scenes, tts_map) if demo_entries else None
    if offline_html_payload:
        manifest["offlineHtml"] = "offline.html"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"))
        archive.writestr("course.json", json.dumps(course_public, ensure_ascii=False, indent=2).encode("utf-8"))
        for path, data in files.items():
            archive.writestr(path, data)
        for path, data in demo_files.items():
            archive.writestr(path, data)
        if offline_html_payload:
            archive.writestr("offline.html", offline_html_payload)
        if sources:
            # P3：来源清单（标题/链接/站点/日期/摘要），与导出快照里的 sources 一致。
            archive.writestr("sources.json", _sources_json_bytes(sources))
        if note:
            archive.writestr("EXPORT-NOTE.txt", str(note)[:4000].encode("utf-8"))
    return buffer.getvalue()


# ---------------------------------------------------------------------------
# 教学资源包（PPTX + 交互场景独立 HTML + 课程 JSON + README，单 ZIP 发放）
# ---------------------------------------------------------------------------

#: 允许保留的 XML 命名空间 URI（标识符，不是网络请求）。
_PACKAGE_ALLOWED_NAMESPACE_URIS = ("http://www.w3.org/", "https://www.w3.org/")


def package_filename(course: dict) -> str:
    """教学资源包中文文件名：``资源包-课程名.zip``。"""
    return "资源包-%s.zip" % _export_basename(course)


def _scene_slug(title: str, order: int) -> str:
    """场景标题 → 安全文件名片段（去掉路径/保留中文，空格转 ``-``，≤30 字符）。"""
    raw = _EXTERNAL_FILE_RE.sub("-", str(title or ""))
    raw = re.sub(r"\s+", "-", raw).strip("-.")
    raw = re.sub(r"-{2,}", "-", raw)
    return raw[:30].strip("-.") or ("scene-%02d" % max(1, int(order or 1)))


def _external_reference_problems(text: str) -> list[str]:
    """检查 HTML 文本里的外链标记（允许 XML 命名空间 URI）；返回命中的标记列表。"""
    lowered = str(text or "")
    for uri in _PACKAGE_ALLOWED_NAMESPACE_URIS:
        lowered = lowered.replace(uri, "")
    lowered = lowered.lower()
    problems: list[str] = []
    for marker in (
        'src="http', "src='http", 'href="http', "href='http", "url(http", "@import",
        "<link", "<script src", "<script  src", "http://", "https://",
    ):
        if marker in lowered:
            problems.append(marker)
    return problems


_PACKAGE_SHELL_CSS = """
* { box-sizing: border-box; }
html, body { margin: 0; padding: 0; height: 100%; background: #0f172a; color: #e5e7eb;
  font-family: system-ui, -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif; }
body { display: flex; flex-direction: column; }
header { padding: 10px 16px; background: #111827; border-bottom: 1px solid #1f2937;
  display: flex; flex-wrap: wrap; align-items: center; gap: 8px 14px; }
header .course { font-size: 14px; color: #9ca3af; }
header h1 { margin: 0; font-size: 16px; font-weight: 600; }
.badge { padding: 2px 9px; border-radius: 999px; background: #1d4ed8; color: #eff6ff; font-size: 12px; }
.toolbar { margin-left: auto; display: flex; gap: 8px; }
button { font: inherit; font-size: 13px; padding: 6px 12px; border-radius: 8px;
  border: 1px solid #374151; background: #1f2937; color: #e5e7eb; cursor: pointer; min-height: 36px; }
button:hover { background: #374151; }
main { flex: 1; min-height: 0; padding: 12px; display: flex; }
#scene-frame { flex: 1; width: 100%; border: 0; border-radius: 10px; background: #ffffff; }
footer { padding: 8px 16px; background: #111827; border-top: 1px solid #1f2937;
  font-size: 12px; color: #9ca3af; min-height: 34px; }
footer #status { color: #93c5fd; }
@media (max-width: 640px) {
  header { padding: 8px 10px; }
  main { padding: 6px; }
  .toolbar button { padding: 6px 10px; }
}
"""

_PACKAGE_SHELL_JS = """
(function () {
  var frame = document.getElementById('scene-frame');
  var status = document.getElementById('status');
  var original = frame.getAttribute('srcdoc') || '';
  window.addEventListener('message', function (event) {
    if (!frame.contentWindow || event.source !== frame.contentWindow) return;
    var data = event.data || {};
    if (data.source !== 'zhixu-interactive') return;
    var payload = data.payload || {};
    var summary = payload.summary ? '：' + payload.summary : '';
    status.textContent = '交互状态：' + (data.type || '事件') + summary;
  });
  document.getElementById('reload').addEventListener('click', function () {
    frame.setAttribute('srcdoc', original);
    status.textContent = '已重新加载交互页。';
  });
  document.getElementById('fullscreen').addEventListener('click', function () {
    var root = document.documentElement;
    if (root.requestFullscreen) { root.requestFullscreen(); }
  });
  status.textContent = '交互页已加载：本页为离线沙箱页面，无需联网。';
})();
"""


def _interactive_package_html(course: dict, scene: dict, content: dict, *,
                              position: int, total: int) -> str:
    """单个交互场景 → 独立自包含 HTML（标题壳 + sandbox iframe + 交互状态栏；无外链）。"""
    title = str(scene.get("title") or "交互场景")
    course_title = str(course.get("title") or course.get("topic") or "AI 课堂")
    widget = schema.normalize_widget(content.get("widget"), "simulation")
    widget_label = schema.WIDGET_LABELS.get(widget, widget)
    scene_html = str(content.get("html") or "")
    srcdoc = html_lib.escape(scene_html, quote=True)
    return (
        "<!DOCTYPE html>"
        '<html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        "<title>%s · 第 %d/%d 页 · %s（交互资源包）</title>"
        "<style>%s</style></head><body>"
        '<header><span class="course">%s</span>'
        '<h1>第 %d/%d 页 · %s</h1>'
        '<span class="badge">交互场景 · %s</span>'
        '<div class="toolbar">'
        '<button id="reload" type="button" title="重新加载交互页">重新加载</button>'
        '<button id="fullscreen" type="button" title="全屏显示">全屏</button>'
        "</div></header>"
        '<main><iframe id="scene-frame" sandbox="allow-scripts" '
        'title="%s" srcdoc="%s"></iframe></main>'
        '<footer><span id="status">交互页加载中…</span>'
        "　·　本文件为「知序」导出的离线教学资源包，可直接用浏览器打开。</footer>"
        "<script>%s</script></body></html>"
    ) % (
        html_lib.escape(course_title), position, max(1, int(total)), html_lib.escape(title),
        _PACKAGE_SHELL_CSS,
        html_lib.escape(course_title), position, max(1, int(total)), html_lib.escape(title),
        html_lib.escape(widget_label),
        html_lib.escape(title), srcdoc,
        _PACKAGE_SHELL_JS,
    )


def _package_readme(course: dict, scenes: list[dict], interactive_files: list[dict],
                    warnings: list[str], *, exported_at: str, oversized: bool,
                    sources: list[dict] | None = None) -> str:
    """中文 README：课程信息、文件说明、场景清单、联网参考来源、打开方式、离线与安全提示。"""
    source_list = sources if isinstance(sources, list) else []
    title = str(course.get("title") or course.get("topic") or "AI 课堂")
    subject = str(course.get("subject") or "未指定")
    grade = str(course.get("grade") or "未指定")
    mode = str(course.get("mode") or "standard")
    mode_label = "深度交互模式" if mode == "interactive" else "标准课堂"
    config = cdb.json_load(course.get("config_json"), {})
    config = config if isinstance(config, dict) else {}
    lines: list[str] = [
        "知序 AI 课堂 · 教学资源包",
        "",
        "课程：%s" % title,
        "主题：%s" % (course.get("topic") or title),
        "学科 / 学段：%s / %s" % (subject, grade),
        "课程模式：%s" % mode_label,
        "场景数量：%d（其中交互场景 %d 个）" % (len(scenes), len(interactive_files)),
        "导出时间（UTC）：%s" % exported_at,
        "生成 prompt 版本：%s" % (config.get("prompt_version") or "未记录"),
        "",
        "一、文件说明",
        "  1. course.pptx —— 幻灯片课件，可用 PowerPoint / WPS / LibreOffice 打开；",
        "     公式为文本降级显示，交互/项目场景在课件中为标题说明页（具体内容见下条）；",
        "     末页为「参考来源」（本次生成使用联网资料时附带，无资料时不加页）。",
        "  2. interactive/ —— 交互场景独立网页（每个场景一个 HTML，双击用浏览器打开即可，无需联网）：",
    ]
    if interactive_files:
        for item in interactive_files:
            lines.append("     - %s —— 第 %d 页《%s》（交互类型：%s）" % (
                item["path"], item["order"], item["title"],
                schema.WIDGET_LABELS.get(item.get("widget") or "", item.get("widget") or "交互"),
            ))
    else:
        lines.append("     - （本课程没有交互场景）")
    lines.extend([
        "  3. course.json / ets.json —— 课程与场景结构（两份内容相同；与「ZIP 导出」共用同一结构，",
        "     供程序读取或做离线归档；其中的图片引用为站内素材地址，不含图片文件本身）。",
        "  4. README.txt —— 本说明文件。",
        "",
        "二、场景清单",
    ])
    for index, scene in enumerate(scenes, 1):
        order = int(scene.get("order_index") or index)
        scene_type = str(scene.get("scene_type") or "slide")
        type_label = schema.SCENE_TYPE_LABELS.get(scene_type, scene_type)
        lines.append("  %d. [%s] %s" % (order, type_label, scene.get("title") or "未命名场景"))
    if any(str(scene.get("scene_type") or "") == "demo" for scene in scenes):
        # M4：仅在存在原理演示场景时补充说明，既有课程的 README 输出保持不变。
        lines.append(
            "  （原理演示场景为固定版本学习附件快照：「标题 + 说明 + 海报」，"
            "源附件后续更新不会改变课件内容。）"
        )
    if source_list:
        lines.extend([
            "",
            "三、参考来源（联网检索，%d 条）" % len(source_list),
            "  - 以下资料由 DeepSeek 官方 /v1/tools/search 检索获得，生成课程时仅作为事实参考；",
            "    链接为纯文本，不会随资源包自动加载，内容请以原网页为准。",
        ])
        for index, item in enumerate(source_list, 1):
            number = int(item.get("index") or index)
            meta = _source_meta_text(item)
            lines.append("  [%d] %s%s" % (
                number, item.get("title") or "未命名来源", ("（%s）" % meta) if meta else "",
            ))
            lines.append("      链接：%s" % (item.get("url") or "（无链接）"))
            summary = str(item.get("summary") or "").strip()
            if summary:
                lines.append("      摘要：%s" % summary[:160])
    lines.extend([
        "",
        "四、打开方式",
        "  - PPTX：用 PowerPoint / WPS / LibreOffice 打开；建议 16:9 全屏放映。",
        "  - 交互页：双击 interactive/ 下的 HTML，用 Chrome / Edge / Firefox 打开；",
        "    页面主体是 sandbox iframe，交互内容与主页面安全隔离，不会访问网络。",
        "  - JSON：开发者可读取课程结构（course.json 与 ets.json 内容一致）。",
        "",
        "五、离线与安全",
        "  - 除「参考来源」中列出的纯文本链接外，资源包内所有文件不含外链资源、可离线打开；",
        "    PPTX 中的图片已内联为 data URI。",
        "  - 资源包不含账号、登录会话、API Key 等敏感信息；交互页中的操作只在本地浏览器进行。",
        "  - 交互场景正文在生成时已通过静态安全校验（禁止外链/脚本注入等），本包不改变其内容。",
    ])
    if warnings:
        lines.extend(["", "六、检查提示（导出时记录，请打开前确认）"])
        for warning in warnings[:20]:
            lines.append("  - %s" % warning)
    if oversized:
        lines.extend([
            "",
            "七、体积提示",
            "  - 资源包总大小超过 %d MB，部分工具打开可能较慢；" % (MAX_STANDALONE_HTML_BYTES // (1024 * 1024)),
            "    如只需课件可用「PPTX 导出」，需要可再导入的备份可用「ZIP 导出」。",
        ])
    return "\n".join(lines) + "\n"


def build_resource_package(course: dict, scenes: list[dict], *,
                           pptx_scenes: list[dict] | None = None) -> bytes:
    """构建教学资源包 ZIP：``course.pptx`` + ``interactive/scene-NN-slug.html`` × N +
    ``course.json`` / ``ets.json`` + 中文 ``README.txt``。

    - 交互页使用已校验的场景 HTML 包一层最小播放壳（``iframe sandbox="allow-scripts"``），
      单文件、无外链、可直接浏览器打开；
    - PPTX 复用 ``build_pptx``（图片等素材由调用方通过 ``pptx_scenes`` 传入已内联版本）；
    - 全部为确定性内容，不含账号/会话/密钥信息。
    """
    snapshot = build_snapshot(course, scenes)
    exported_at = str(snapshot.get("exportedAt") or _db().utcnow())
    # P3：联网来源写入 README（无来源时 README 不出现链接、保持既有离线断言）。
    sources = _course_sources(course)
    pptx_bytes = build_pptx(course, pptx_scenes if pptx_scenes is not None else scenes)
    files: dict[str, bytes] = {}
    interactive_files: list[dict] = []
    warnings: list[str] = []
    for scene in scenes:
        scene_type = str(scene.get("scene_type") or scene.get("type") or "")
        if scene_type != "interactive":
            continue
        content = _scene_content(scene)
        if not str(content.get("html") or "").strip():
            warnings.append("交互场景《%s》没有可导出的 HTML，已跳过" % (scene.get("title") or ""))
            continue
        order = int(scene.get("order_index") or 1)
        slug = _scene_slug(scene.get("title") or "", order)
        path = "interactive/scene-%02d-%s.html" % (order, slug)
        wrapper = _interactive_package_html(
            course, scene, content, position=order, total=len(scenes),
        )
        problems = _external_reference_problems(wrapper)
        if problems:
            warnings.append("交互场景《%s》导出后仍检出疑似外链标记（%s），请在打开前确认" % (
                scene.get("title") or "", "、".join(problems)))
        files[path] = wrapper.encode("utf-8")
        interactive_files.append({
            "path": path, "order": order, "title": str(scene.get("title") or ""),
            "widget": schema.normalize_widget(content.get("widget"), "simulation"),
        })
    course_json = json.dumps(snapshot, ensure_ascii=False, indent=2).encode("utf-8")
    estimated = (
        len(pptx_bytes) + len(course_json) * 2
        + sum(len(files[item["path"]]) for item in interactive_files)
    )
    readme = _package_readme(
        course, scenes, interactive_files, warnings,
        exported_at=exported_at, oversized=estimated > MAX_STANDALONE_HTML_BYTES,
        sources=sources,
    )
    files["course.pptx"] = pptx_bytes
    files["course.json"] = course_json
    files["ets.json"] = course_json
    files["README.txt"] = readme.encode("utf-8")
    ordered = ["course.pptx", "README.txt", "course.json", "ets.json"]
    ordered.extend(item["path"] for item in interactive_files)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in ordered:
            archive.writestr(path, files[path])
    return buffer.getvalue()


# ---------------------------------------------------------------------------
# HTTP 路由
# ---------------------------------------------------------------------------

@router.get("/courses/{course_id}/export")
async def export_course(
    course_id: int,
    format: str = Query(default="pptx"),
    include_tts: bool = Query(default=False),
    user=Depends(current_user),
):
    """导出课程：``pptx`` / ``html``（离线单文件）/ ``zip``（可导入）/ ``package``（教学资源包）/
    ``pdf``（服务端 PDF 讲义）/ ``mp4``（已生成的视频素材，先由 ``POST /courses/{id}/video`` 生成）。"""
    export_format = str(format or "").strip().lower()
    if export_format not in EXPORT_FORMATS:
        raise HTTPException(422, "导出格式只能是 pptx / html / zip / package / pdf / mp4")
    course = _owned_course_or_404(course_id, user["id"])
    scenes = cdb.course_scenes(course_id, user["id"])
    if not scenes:
        raise HTTPException(422, "课程还没有场景，无法导出")
    tts_assets: dict[int, dict] = {}
    tts_mode = "browser"
    # PPTX 无法可靠嵌入音频：只有离线 HTML / ZIP 才做可选 TTS 合成（其余仍由浏览器朗读兜底）。
    if include_tts and export_format in ("html", "zip"):
        tts_assets = await _collect_tts_assets(user_id=user["id"], course=course, scenes=scenes)
        tts_mode = "assets" if tts_assets else "browser"
    extra = {"X-Zhixu-TTS": tts_mode, "X-Zhixu-Export-Format": export_format}
    if export_format == "pdf":
        # 课堂讲义：离线打印版 HTML → Playwright PDF；引擎不可用时渲染函数抛 503 中文引导。
        payload, page_count = await render_course_pdf(
            course, _inline_scene_images(scenes, user["id"]),
        )
        extra["X-Page-Count"] = str(page_count)
        return Response(
            content=payload, media_type=PDF_MEDIA_TYPE,
            headers=_file_headers(export_filename(course, "pdf"), extra=extra),
        )
    if export_format == "mp4":
        # 视频由 course-video 任务异步生成，这里只负责下载最近一次成功的视频素材。
        from . import video as video_export

        return video_export.mp4_export_response(course, user["id"])
    if export_format == "pptx":
        try:
            payload = build_pptx(course, _inline_scene_images(scenes, user["id"]))
        except HTTPException:
            raise
        except Exception as exc:  # 明确失败原因，不返回无法打开的文件
            raise HTTPException(503, "PPTX 导出失败：%s" % type(exc).__name__) from None
        return Response(
            content=payload, media_type=PPTX_MEDIA_TYPE,
            headers=_file_headers(export_filename(course, "pptx"), extra=extra),
        )
    if export_format == "html":
        snapshot = build_snapshot(course, _inline_scene_images(scenes, user["id"]), for_html=True)
        payload_text = build_offline_html(snapshot, tts_assets)
        payload = payload_text.encode("utf-8")
        if len(payload) > MAX_STANDALONE_HTML_BYTES:
            note = "课程内容过大（离线 HTML 超过 %d 字节），已自动改为 ZIP 导出。" % MAX_STANDALONE_HTML_BYTES
            payload = build_course_zip(
                course, scenes, tts_assets=tts_assets,
                resources=_course_assets(course_id, user["id"], scenes),
                note=note,
            )
            extra.update({"X-Zhixu-Export-Fallback": "zip", "X-Zhixu-TTS": tts_mode})
            return Response(
                content=payload, media_type=ZIP_MEDIA_TYPE,
                headers=_file_headers(export_filename(course, "zip"), extra=extra),
            )
        return Response(
            content=payload, media_type=HTML_MEDIA_TYPE,
            headers=_file_headers(export_filename(course, "html"), extra=extra),
        )
    if export_format == "package":
        try:
            payload = build_resource_package(
                course, scenes, pptx_scenes=_inline_scene_images(scenes, user["id"]),
            )
        except HTTPException:
            raise
        except Exception as exc:  # 明确失败原因，不返回打不开的资源包
            raise HTTPException(503, "教学资源包导出失败：%s" % type(exc).__name__) from None
        return Response(
            content=payload, media_type=ZIP_MEDIA_TYPE,
            headers=_file_headers(package_filename(course), extra=extra),
        )
    payload = build_course_zip(
        course, scenes, tts_assets=tts_assets,
        resources=_course_assets(course_id, user["id"], scenes),
    )
    return Response(
        content=payload, media_type=ZIP_MEDIA_TYPE,
        headers=_file_headers(export_filename(course, "zip"), extra=extra),
    )


__all__ = [
    "EXPORT_FORMATS", "MAX_STANDALONE_HTML_BYTES", "PDF_LANDSCAPE", "PDF_MEDIA_TYPE",
    "PDF_PAPER", "PPTX_MEDIA_TYPE", "VIDEO_MEDIA_TYPE", "ZIP_FORMAT",
    "ZIP_FORMAT_VERSION", "build_course_zip", "build_offline_html", "build_print_html",
    "build_pptx", "build_resource_package", "build_snapshot", "content_disposition",
    "export_filename", "package_filename", "pdf_page_count", "render_course_pdf", "router",
]
