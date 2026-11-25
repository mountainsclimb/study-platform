"""表格单元格结构识别（RapidTable，可选依赖）：识别 + 服务端 HTML 清洗。

按《开源组件整合实施方案》§3.2 与 §1.1 落地：

* ``STUDY_TABLE_OCR=on`` 才启用（默认关闭）；未安装/关闭/失败时块原样返回。
* ``recognize_table(crop)`` 识别表格区域，输出经 :func:`sanitize_table_html` 清洗后
  才返回：白名单只留 ``table``/``thead``/``tbody``/``tr``/``td``/``th`` 与
  ``rowspan``/``colspan``（1–50 的整数），其余标签、脚本、事件属性、style 全部剥离。
* 同步推理由调用方经 ``asyncio.to_thread`` 执行，进程级信号量限制并发；单区域超时
  由调用方用 ``asyncio.wait_for`` 控制（``STUDY_TABLE_TIMEOUT``，默认 8 秒）。
* 若调用方已有 OCR 行结果，可通过 ``ocr_results=(boxes, texts, scores)`` 传入，
  避免重复跑 OCR；没有时由 RapidTable 自带的 OCR 引擎处理。
"""
from __future__ import annotations

from html import escape
from html.parser import HTMLParser
import importlib.metadata
import importlib.util
from io import BytesIO
import os
from pathlib import Path
import threading
import time

from PIL import Image, UnidentifiedImageError

__all__ = [
    "ENGINE_NAME",
    "TableOcrError",
    "TableUnavailable",
    "recognize_table",
    "sanitize_table_html",
    "html_shape",
    "local_table_available",
    "engine_status",
    "engine_meta",
    "warmup",
    "reset_engine",
    "mode",
    "timeout",
    "MAX_TABLE_HTML_CHARS",
]

ENGINE_NAME = "rapid-table"
TABLE_MODES = ("on", "off")
DEFAULT_TIMEOUT = 8.0
MAX_TABLE_HTML_CHARS = 20000
MAX_CROP_SIDE = 2400
MAX_CONCURRENCY = 2
ALLOWED_TAGS = frozenset({"table", "thead", "tbody", "tr", "td", "th"})
ALLOWED_CELL_ATTRS = frozenset({"rowspan", "colspan"})
MAX_SPAN = 50
NOTICE = (
    "表格结构由 RapidTable 本地模型生成，仅作为校对草稿；"
    "合并单元格与无边框表格的识别质量需人工核对。"
)

_engine = None
_engine_error: str | None = None
_engine_lock = threading.Lock()
_infer_slots = threading.BoundedSemaphore(MAX_CONCURRENCY)


class TableOcrError(RuntimeError):
    """表格结构识别异常基类。"""


class TableUnavailable(TableOcrError):
    """开关关闭、依赖缺失或引擎初始化失败。"""


def mode() -> str:
    value = os.environ.get("STUDY_TABLE_OCR", "").strip().lower()
    return "on" if value == "on" else "off"


def timeout() -> float:
    """单个表格区域识别超时秒数，默认 8，可用 STUDY_TABLE_TIMEOUT 覆盖（1–120）。"""
    raw = os.environ.get("STUDY_TABLE_TIMEOUT", "").strip()
    if not raw:
        return DEFAULT_TIMEOUT
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT
    return min(120.0, max(1.0, value))


def _module_available() -> bool:
    for name in ("rapid_table", "onnxruntime"):
        try:
            if importlib.util.find_spec(name) is None:
                return False
        except (ImportError, ValueError):
            return False
    return True


def _engine_version() -> str | None:
    try:
        return importlib.metadata.version(ENGINE_NAME)
    except importlib.metadata.PackageNotFoundError:
        return None
    except Exception:
        return None


def _engine_models() -> list[str]:
    try:
        spec = importlib.util.find_spec("rapid_table")
    except (ImportError, ValueError):
        return []
    origin = getattr(spec, "origin", None)
    if not origin:
        return []
    models_dir = Path(origin).resolve().parent / "models"
    try:
        return sorted(path.name for path in models_dir.glob("*.onnx"))
    except OSError:
        return []


def _has_bundled_ocr() -> bool:
    """RapidTable 内置 OCR（``rapidocr`` 包）是否可用。

    rapid-table 3.0.2 的 ``use_ocr=True`` 在 ``rapidocr`` 缺失时并不报错，而是把
    ``self.ocr_engine`` 置为 ``None``（只打一行 warning），随后在没有 ``ocr_results``
    时抛 ``TypeError: 'NoneType' object is not callable``，被包装成「识别失败」。
    这里提前探明，让失败原因指向真实问题（缺 rapidocr），而非引擎异常。
    """
    try:
        return importlib.util.find_spec("rapidocr") is not None
    except (ImportError, ValueError):
        return False


def _get_engine():
    """返回 RapidTable 单例；首次调用加载/下载模型，失败缓存，可 reset_engine 重试。"""
    global _engine, _engine_error
    with _engine_lock:
        if _engine is not None:
            return _engine
        if _engine_error is not None:
            raise TableUnavailable(_engine_error)
        try:
            from rapid_table import RapidTable, RapidTableInput
        except Exception as error:
            _engine_error = (
                f"表格结构依赖不可用（{type(error).__name__}），"
                "表格块将保持无结构并标记需人工校对"
            )
            raise TableUnavailable(_engine_error) from None
        try:
            engine = RapidTable(RapidTableInput(use_ocr=_has_bundled_ocr()))
        except Exception as error:
            _engine_error = (
                f"表格结构引擎初始化失败（{type(error).__name__}），"
                "表格块将保持无结构并标记需人工校对"
            )
            raise TableUnavailable(_engine_error) from None
        _engine = engine
        return _engine


def reset_engine() -> None:
    global _engine, _engine_error
    with _engine_lock:
        _engine = None
        _engine_error = None


def local_table_available() -> bool:
    if mode() != "on":
        return False
    if _engine is not None:
        return True
    if _engine_error is not None:
        return False
    return _module_available()


def _unavailable_reason() -> str:
    if mode() != "on":
        return "表格结构识别已关闭（STUDY_TABLE_OCR=off）"
    if _engine_error is not None:
        return _engine_error
    return "表格结构识别不可用（未安装可选依赖 rapid-table 或 onnxruntime）"


def engine_meta() -> dict:
    return {
        "engine": ENGINE_NAME,
        "engine_version": _engine_version(),
        "capabilities": {"table_html": True, "merged_cells": True},
    }


def engine_status() -> dict:
    available = local_table_available()
    if mode() != "on":
        notice = "表格结构识别已关闭（STUDY_TABLE_OCR=off）；表格块只有文字、无单元格结构。"
    elif available:
        notice = NOTICE
    else:
        notice = _unavailable_reason() + "；不影响其它功能。"
    version = _engine_version() if available else None
    return {
        "engine": ENGINE_NAME,
        "mode": mode(),
        "available": available,
        "loaded": _engine is not None,
        "version": version,
        "capabilities": {"table_html": True, "merged_cells": True},
        "limits": {
            "timeout_seconds": timeout(),
            "max_html_chars": MAX_TABLE_HTML_CHARS,
            "max_crop_side": MAX_CROP_SIDE,
            "concurrency": MAX_CONCURRENCY,
        },
        "models": _engine_models() if available else [],
        "notice": notice,
        "error": _engine_error,
    }


def warmup() -> dict:
    """启动预热：只加载模型、不做推理；关闭或不可用时如实跳过。"""
    started = time.monotonic()
    base = {"engine": ENGINE_NAME, "loaded": False}
    if mode() != "on":
        return {**base, "skipped": "STUDY_TABLE_OCR 未开启", "elapsed_ms": 0}
    if not local_table_available():
        return {**base, "skipped": _unavailable_reason(),
                "elapsed_ms": int((time.monotonic() - started) * 1000)}
    try:
        _get_engine()
    except Exception as error:
        return {**base, "error": f"{type(error).__name__}: {error}",
                "elapsed_ms": int((time.monotonic() - started) * 1000)}
    return {**base, "loaded": True,
            "elapsed_ms": int((time.monotonic() - started) * 1000)}


# ---------------------------------------------------------------------------
# 服务端清洗：白名单标签 + 仅 rowspan/colspan
# ---------------------------------------------------------------------------

class _TableSanitizer(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.stack: list[str] = []
        self.skip_depth = 0

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if self.skip_depth:
            if tag in ("script", "style"):
                self.skip_depth += 1
            return
        if tag in ("script", "style"):
            self.skip_depth = 1
            return
        if tag not in ALLOWED_TAGS:
            return
        kept: list[str] = []
        if tag in ("td", "th"):
            for name, value in attrs or []:
                name = str(name or "").lower()
                if name not in ALLOWED_CELL_ATTRS:
                    continue
                try:
                    span = int(str(value).strip())
                except (TypeError, ValueError):
                    continue
                if 1 <= span <= MAX_SPAN:
                    kept.append(f' {name}="{span}"')
        self.parts.append(f"<{tag}{''.join(kept)}>")
        self.stack.append(tag)

    def handle_startendtag(self, tag, attrs):
        # 自闭合标签在白名单里没有语义（table/tr/td 不应自闭合），直接丢弃。
        return

    def handle_endtag(self, tag):
        tag = tag.lower()
        if self.skip_depth:
            if tag in ("script", "style"):
                self.skip_depth -= 1
            return
        if tag not in ALLOWED_TAGS or tag not in self.stack:
            return
        while self.stack:
            top = self.stack.pop()
            self.parts.append(f"</{top}>")
            if top == tag:
                break

    def handle_data(self, data):
        if self.skip_depth:
            return
        self.parts.append(escape(str(data or ""), quote=False))

    def close(self):
        super().close()
        while self.stack:
            self.parts.append(f"</{self.stack.pop()}>")

    def output(self) -> str:
        return "".join(self.parts)


def sanitize_table_html(raw, *, max_chars: int = MAX_TABLE_HTML_CHARS) -> str:
    """清洗表格 HTML；返回只含白名单标签/属性的片段。

    * 无 ``<table>`` 或清洗后为空时返回空串（调用方据此丢弃）；
    * 超过 ``max_chars`` 抛 :class:`ValueError`，由调用方按"非法 table_html"处理。
    """
    if not isinstance(raw, str) or not raw.strip():
        return ""
    parser = _TableSanitizer()
    try:
        parser.feed(raw)
        parser.close()
    except Exception:
        return ""
    output = parser.output().strip()
    if "<table" not in output.lower():
        return ""
    if len(output) > max_chars:
        raise ValueError(f"表格 HTML 超过 {max_chars} 字符")
    return output


class _ShapeCounter(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows = 0
        self.max_columns = 0
        self._columns = 0

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag == "tr":
            self.rows += 1
            self._columns = 0
        elif tag in ("td", "th"):
            span = 1
            for name, value in attrs or []:
                if str(name).lower() == "colspan":
                    try:
                        span = max(1, min(MAX_SPAN, int(str(value).strip())))
                    except (TypeError, ValueError):
                        span = 1
            self._columns += span
            self.max_columns = max(self.max_columns, self._columns)


def html_shape(html: str) -> tuple[int, int]:
    """统计表格行数与最大列数（colspan 计入；rowspan 不额外展开）。"""
    if not isinstance(html, str) or not html.strip():
        return 0, 0
    counter = _ShapeCounter()
    try:
        counter.feed(html)
        counter.close()
    except Exception:
        return 0, 0
    return counter.rows, counter.max_columns


# ---------------------------------------------------------------------------
# 识别
# ---------------------------------------------------------------------------

def _to_pil(source) -> Image.Image:
    if isinstance(source, Image.Image):
        return source
    if isinstance(source, dict):
        import base64

        encoded = source.get("data")
        if not isinstance(encoded, str) or not encoded:
            raise ValueError("表格裁图片段缺少 data 字段")
        try:
            raw = base64.b64decode(encoded)
        except (ValueError, TypeError):
            raise ValueError("表格裁图不是合法的 base64 数据") from None
        source = raw
    if isinstance(source, (bytes, bytearray)):
        try:
            with Image.open(BytesIO(bytes(source))) as opened:
                opened.load()
                return opened.copy()
        except (UnidentifiedImageError, OSError, ValueError):
            raise ValueError("表格裁图不是有效的图片") from None
    raise ValueError("表格裁图仅支持 PIL.Image、bytes 或图片片段")


def _prepare_crop(source) -> Image.Image:
    image = _to_pil(source)
    if image.mode != "RGB":
        image = image.convert("RGB")
    if max(image.size) > MAX_CROP_SIDE:
        image = image.copy()
        image.thumbnail((MAX_CROP_SIDE, MAX_CROP_SIDE), Image.LANCZOS)
    return image


def recognize_table(crop, *, ocr_results=None) -> dict:
    """识别一个表格区域，返回 ``{"html", "row_count", "col_count", "elapsed_ms"}``。

    ``ocr_results`` 可选，形如 ``(boxes, texts, scores)``；boxes 为四点多边形列表。
    未传时由 RapidTable 内置 OCR 引擎处理（需要随包的 rapidocr）。
    """
    if not local_table_available():
        raise TableUnavailable(_unavailable_reason())
    if ocr_results is None and not _has_bundled_ocr():
        # 没传我们自己的 OCR 结果，又缺 rapidocr 内置引擎：直接说明缺什么，
        # 不把 ``NoneType is not callable`` 包装成「识别失败」。
        raise TableOcrError(
            "表格结构识别缺少文字检测：未传入 ocr_results，且未安装 rapidocr；"
            "该表格块保持原样（安装 rapidocr 或经 ocr_results 传入行结果）"
        )
    started = time.monotonic()
    prepared = _prepare_crop(crop)
    try:
        import numpy as np
    except Exception:
        raise TableOcrError("表格结构识别缺少 numpy 依赖") from None
    array = np.ascontiguousarray(np.asarray(prepared))
    with _infer_slots:
        engine = _get_engine()
        try:
            if ocr_results is not None:
                output = engine([array], ocr_results=[ocr_results])
            else:
                output = engine([array])
        except Exception as error:
            raise TableOcrError(
                f"表格结构识别失败（{type(error).__name__}），该表格块保持原样"
            ) from None
    htmls = list(getattr(output, "pred_htmls", None) or [])
    raw_html = htmls[0] if htmls else ""
    if not raw_html:
        raise TableOcrError("表格结构识别未返回 HTML，该表格块保持原样")
    try:
        cleaned = sanitize_table_html(raw_html)
    except ValueError as error:
        raise TableOcrError(str(error)) from None
    if not cleaned:
        raise TableOcrError("表格结构清洗后为空，该表格块保持原样")
    rows, columns = html_shape(cleaned)
    return {
        "html": cleaned,
        "row_count": rows,
        "col_count": columns,
        "elapsed_ms": int((time.monotonic() - started) * 1000),
    }
