"""Surya 图片试题转 LaTeX 适配层（可选依赖 ``surya-ocr``）。

作用：为「上传题目图片 → LaTeX」提供本地识别能力。上游 `surya`
（<https://github.com/datalab-to/surya>）的 OCR 输出为 HTML：文字与公式混排，
公式放在 ``<math>...</math>`` 中（KaTeX 兼容 LaTeX）。本模块把单张/多张题目图片
的识别结果转换为知序内容块口径（``kind=text|math|image|table``，``math`` 块带
``latex`` 字段，与 :mod:`app.content_refs` 一致）以及整题 LaTeX 文本（公式 ``$...$``），
供拍题、试卷识别导入等上传链路在实现「图片题目 → LaTeX」时直接调用。

依赖与降级：执行 ``pip install -r requirements-surya.txt``（即 ``surya-ocr``）后可用；
未安装时 :func:`status` 报告不可用，:func:`recognize_images` 抛
:class:`SuryaUnavailableError`，不影响主流程。surya-ocr 只是轻量客户端/编排器，
推理后端用 vllm（NVIDIA GPU）或 llama-server（CPU / Apple Silicon），也可经
``SURYA_INFERENCE_URL`` 指向已有 OpenAI 兼容服务，详见上游 README。

边界：识别结果未经人工核对不得直接作为标准答案或入库题干，与拍题「先核对确认」
门禁一致；手写、复杂表格、拍照反光等场景仍需人工校对。
"""
from __future__ import annotations

import asyncio
import base64
from io import BytesIO
import html as html_lib
import re
import threading

from PIL import Image

__all__ = [
    "SuryaUnavailableError",
    "available",
    "status",
    "html_to_latex",
    "blocks_to_latex",
    "image_to_latex",
    "recognize_image",
    "recognize_images",
    "arecognize_images",
]

_MATH_RE = re.compile(r"<math\b[^>]*>(.*?)</math>", re.S | re.I)
_TABLE_RE = re.compile(r"<table\b[^>]*>(.*?)</table>", re.S | re.I)
_ROW_RE = re.compile(r"<tr\b[^>]*>(.*?)</tr>", re.S | re.I)
_CELL_RE = re.compile(r"<t[dh]\b[^>]*>(.*?)</t[dh]>", re.S | re.I)
_TAG_RE = re.compile(r"<[^>]+>")
_BR_RE = re.compile(r"<br\s*/?>", re.I)
_P_CLOSE_RE = re.compile(r"</p\s*>", re.I)

# 版面标签 → 内容块口径（app.content_refs.block_kind）。
_IMAGE_LABELS = frozenset({"Picture", "Figure", "Diagram", "Form"})
_TABLE_LABELS = frozenset({"Table"})
_MATH_LABELS = frozenset({"Equation", "ChemicalBlock"})


class SuryaUnavailableError(RuntimeError):
    """surya-ocr 未安装或推理后端不可用。"""


# ---------------------------------------------------------------------------
# HTML → LaTeX：公式保 KaTeX 兼容 LaTeX，表格转 tabular，其余剥标签
# ---------------------------------------------------------------------------

def html_to_latex(html: str, *, display: bool = False) -> str:
    """把 surya 块级 HTML 转成 LaTeX 文本。

    ``<math>...</math>`` 内容是 KaTeX 兼容 LaTeX，原样保留为 ``$...$``（或
    ``display=True`` 时 ``$$...$$``）；``<table>`` 转成 ``tabular``；其余标签剥除、
    实体反转义。公式先做占位符保护，避免正文中的 ``<`` / ``>`` 被当作标签剥掉。
    """
    if not html:
        return ""
    source = str(html)
    stash: list[str] = []

    def _keep_math(match: re.Match) -> str:
        body = html_lib.unescape(match.group(1)).strip()
        wrapped = f"$${body}$$" if display else f"${body}$"
        stash.append(wrapped)
        return f"\x00M{len(stash) - 1}\x00"

    source = _MATH_RE.sub(_keep_math, source)
    source = _TABLE_RE.sub(lambda m: _table_to_tabular(m.group(1)), source)
    source = _BR_RE.sub("\n", source)
    source = _P_CLOSE_RE.sub("\n", source)
    source = _TAG_RE.sub("", source)
    text = html_lib.unescape(source)
    text = re.sub(r"[ \t\xa0]+", " ", text)
    text = re.sub(r" ?\n ?", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    for index, body in enumerate(stash):
        text = text.replace(f"\x00M{index}\x00", body)
    return text.strip()


def _table_to_tabular(table_html: str) -> str:
    rows: list[list[str]] = []
    for row_match in _ROW_RE.finditer(table_html):
        cells = []
        for cell_match in _CELL_RE.finditer(row_match.group(1)):
            cell = _TAG_RE.sub("", cell_match.group(1))
            cells.append(html_lib.unescape(cell).strip().replace("\n", " "))
        if cells:
            rows.append(cells)
    if not rows:
        return ""
    width = max(len(row) for row in rows)
    lines = [r"\begin{tabular}{" + "l" * width + "}"]
    for row in rows:
        lines.append(" & ".join(row + [""] * (width - len(row))) + r" \\")
    lines.append(r"\end{tabular}")
    return "\n".join(lines)


def blocks_to_latex(blocks) -> str:
    """把内容块列表拼成整题 LaTeX 文本（独立公式 ``$$...$$``，段落空行分隔）。"""
    parts = []
    for block in blocks or []:
        if not isinstance(block, dict):
            continue
        kind = block.get("kind")
        if kind == "math":
            body = str(block.get("latex") or "").strip()
            if not body:
                continue
            parts.append(f"$${body}$$" if block.get("display", True) else f"${body}$")
        elif kind == "text":
            text = str(block.get("text") or "").strip()
            if text:
                parts.append(text)
        elif kind == "table":
            latex = str(block.get("latex") or "").strip()
            if latex:
                parts.append(latex)
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# surya 推理接入（懒加载 + 单例缓存；未安装不影响导入本模块）
# ---------------------------------------------------------------------------

_recognizer_cache = None
_cache_lock = threading.Lock()


def _get_recognizer():
    global _recognizer_cache
    with _cache_lock:
        if _recognizer_cache is None:
            try:
                from surya import RecognitionPredictor, SuryaInferenceManager
            except ImportError as exc:
                raise SuryaUnavailableError(
                    "未安装 surya-ocr，无法识别图片题目；可执行 pip install -r requirements-surya.txt"
                ) from exc
            manager = SuryaInferenceManager()
            _recognizer_cache = RecognitionPredictor(manager)
    return _recognizer_cache


def status() -> dict:
    """报告 surya 可用性，供就绪检查/设置页展示；不触发推理。"""
    try:
        import surya  # noqa: F401
    except ImportError as exc:
        return {"available": False, "reason": f"未安装 surya-ocr：{exc}"}
    except Exception as exc:  # pragma: no cover - 导入期异常仅作降级提示
        return {"available": False, "reason": f"surya-ocr 导入失败：{exc}"}
    return {"available": True, "reason": ""}


def available() -> bool:
    return bool(status()["available"])


# ---------------------------------------------------------------------------
# 图片识别 → 内容块 / 整题 LaTeX
# ---------------------------------------------------------------------------

def _field(obj, name: str, default=None):
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _to_pil(source) -> Image.Image:
    """接受 bytes / PIL.Image / ``content_refs.image_part`` 的 {mime,data} 字典。"""
    if isinstance(source, Image.Image):
        return source
    if isinstance(source, dict):
        source = base64.b64decode(source.get("data") or "")
    if isinstance(source, (bytes, bytearray)):
        with Image.open(BytesIO(source)) as image:
            image.load()
            return image.copy()
    raise TypeError(f"不支持的图片输入类型：{type(source).__name__}")


def _math_body(html: str) -> str:
    bodies = [html_lib.unescape(body).strip() for body in _MATH_RE.findall(html or "") if body.strip()]
    if bodies:
        return " ".join(bodies)
    return html_to_latex(html or "")


def _to_content_block(block) -> dict | None:
    label = str(_field(block, "label", "") or "")
    raw_html = str(_field(block, "html", "") or "")
    result: dict = {}
    bbox = _field(block, "bbox")
    if bbox is not None:
        result["bbox"] = list(bbox)
    confidence = _field(block, "confidence")
    if confidence is not None:
        result["confidence"] = float(confidence)
    if _field(block, "error"):
        return {"kind": "text", "text": "", "label": label, "error": True, **result}
    if label in _IMAGE_LABELS or _field(block, "skipped"):
        return {"kind": "image", "label": label or "Picture", **result}
    if label in _TABLE_LABELS:
        latex = html_to_latex(raw_html)
        return {"kind": "table", "latex": latex, "label": label, **result} if latex else None
    if label in _MATH_LABELS:
        body = _math_body(raw_html)
        return {"kind": "math", "latex": body, "display": True, "label": label, **result} if body else None
    text = html_to_latex(raw_html)
    return {"kind": "text", "text": text, "label": label, **result} if text else None


def _page_result(page) -> dict:
    raw_blocks = list(_field(page, "blocks", []) or [])
    raw_blocks.sort(key=lambda item: _field(item, "reading_order", 0) or 0)
    blocks = [block for block in (_to_content_block(item) for item in raw_blocks) if block]
    return {"latex": blocks_to_latex(blocks), "blocks": blocks}


def recognize_images(images, *, skip_labels=None) -> dict:
    """识别多张题目图片，返回逐图结果与合并 LaTeX 文本。

    返回 ``{"engine": "surya", "count", "items": [{"latex", "blocks"}], "latex"}``；
    未安装 surya-ocr 或推理后端不可用时抛 :class:`SuryaUnavailableError`。
    识别是重计算/阻塞调用，Web 场景请经线程池调用（或用 :func:`arecognize_images`）。
    """
    sources = list(images or [])
    if not sources:
        raise ValueError("没有可识别的题目图片")
    recognizer = _get_recognizer()
    pil_images = [_to_pil(item) for item in sources]
    kwargs = {"skip_labels": list(skip_labels)} if skip_labels is not None else {}
    try:
        predictions = recognizer(pil_images, **kwargs)
    except SuryaUnavailableError:
        raise
    except Exception as exc:
        raise SuryaUnavailableError(f"Surya 识别失败（推理后端可能未就绪）：{exc}") from exc
    items = [_page_result(page) for page in predictions]
    return {
        "engine": "surya",
        "count": len(items),
        "items": items,
        "latex": "\n\n".join(item["latex"] for item in items if item["latex"]),
    }


def recognize_image(image, *, skip_labels=None) -> dict:
    """识别单张题目图片，返回 ``{"latex", "blocks"}``。"""
    return recognize_images([image], skip_labels=skip_labels)["items"][0]


def image_to_latex(image) -> str:
    """单张题目图片 → 整题 LaTeX 文本（公式 ``$...$`` / ``$$...$$``）。"""
    return recognize_image(image)["latex"]


async def arecognize_images(images, *, skip_labels=None) -> dict:
    """:func:`recognize_images` 的异步包装，识别放线程池避免阻塞事件循环。"""
    return await asyncio.to_thread(recognize_images, images, skip_labels=skip_labels)
