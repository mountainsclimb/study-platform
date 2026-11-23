"""P1 本地 OCR（RapidOCR + ONNX Runtime）：延迟单例引擎、图片/PDF 识别、状态与 HTTP 接口。

设计边界：

* **导入失败不拖垮应用**：``rapidocr_onnxruntime`` / ``onnxruntime`` / ``numpy`` 全部延迟导入，
  首次识别时才加载模型；依赖缺失或引擎初始化失败统一抛 :class:`OcrUnavailable`，调用方
  （笔记资料 OCR、拍题识别）降级为只用视觉模型，不影响其它功能。
* **开关与限制**：``STUDY_LOCAL_OCR=auto|on|off`` 控制是否启用；``STUDY_OCR_MAX_SIDE``
  （默认 2400）限制识别图片最长边；``STUDY_OCR_TIMEOUT``（默认 45 秒）限制单张图片/单页
  推理时间；``STUDY_OCR_MAX_PAGES``（默认 5）限制单次同步 PDF 识别页数。
* **不阻塞事件循环**：Pillow 解码/缩放与 CPU 推理都在 :func:`asyncio.to_thread` 中执行；
  推理经进程级信号量限制并发（默认 2），避免多图/多页并发把机器打满。
* **不落库、不伪造**：本模块只把用户当次上传的文件读成内存做识别，不写数据库、不落盘、
  不写用户原文以外的数据；识别失败抛异常，不返回占位文本。
* **用户隔离**：HTTP 接口都要求登录（``current_user``），只处理当次请求里的文件；
  不按 id 读取他人资料。

返回结构::

    {"text": "...", "lines": [{"text": "...", "box": [[x, y], ...4 点], "score": 0.98}],
     "width": 1200, "height": 800, "elapsed_ms": 1234}

坐标是缩放后图片的像素坐标（``box`` 为 4 点四边形，顺序与 RapidOCR 一致）。

内容块（blocks，本轮新增）::

    {"id": "b1", "kind": "text|math|table|image", "text": "...", "latex": None,
     "needs_review": False, "confidence": 0.98,
     "bbox": {"x": 10.0, "y": 20.0, "w": 120.0, "h": 24.0},   # 原图像素坐标
     "norm": {"x": 0.05, "y": 0.08, "w": 0.6, "h": 0.1},      # 0–1 归一化
     "page": 1, "order": 0, "rotation": 0, "source": "rapidocr",
     "engine": ENGINE_NAME, "engine_version": "..."}

块构造口径（可解释、不伪造）：
* 一行一个块（不做跨行合并），``order`` 即阅读顺序：先按行 ``top`` 聚类成视觉行，
  同一视觉行内再按 ``x`` 从左到右编号；
* ``kind`` 用启发式判定：含 LaTeX 反斜杠/公式定界符或数学符号占比高（``=``、``×``、
  ``√``、``^``、``_{}`` 等）→ ``math``；连续 ≥2 行含 ``|``/制表符/多列等宽数字 → ``table``；
  其余 → ``text``；``image`` 预留给后续版面/裁图来源，本地 OCR 不会凭空造图片块；
* 本地引擎**不做公式识别**：所有块 ``latex`` 恒为 ``None``，``math`` 块
  ``needs_review=true`` 并提示需人工或后续 Pix2Text 适配层校对，绝不复述/伪造 LaTeX；
* ``rotation`` 固定 0（RapidOCR 不返回角度），字段保留供后续引擎填充。
"""
from __future__ import annotations

import asyncio
import base64
from contextlib import closing
from io import BytesIO
import importlib.metadata
import importlib.util
import math
import os
from pathlib import Path
import re
import threading
import time

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from PIL import Image, ImageOps, UnidentifiedImageError

from ..auth import current_user
from . import formula_ocr, table_structure

__all__ = [
    "ENGINE_NAME",
    "BLOCK_KINDS",
    "BLOCK_SCHEMA_VERSION",
    "BLOCKS_NOTICE",
    "CAPABILITIES",
    "OcrError",
    "OcrUnavailable",
    "OcrTimeout",
    "local_ocr_available",
    "engine_status",
    "engine_meta",
    "enrichment_status",
    "enrich_blocks",
    "build_blocks",
    "reset_engine",
    "recognize_image",
    "recognize_pdf_pages",
    "router",
]

ENGINE_NAME = "rapidocr-onnxruntime"
OCR_MODES = ("auto", "on", "off")
DEFAULT_MAX_SIDE = 2400
DEFAULT_TIMEOUT = 45.0
DEFAULT_MAX_PAGES = 5
MAX_CONCURRENCY = 2
MAX_IMAGE_PIXELS = 60_000_000
MAX_FILE = 12 * 1024 * 1024          # 与资料/拍题上传一致的单文件上限
MAX_PDF_BYTES = 32 * 1024 * 1024     # 本地 PDF 渲染的内存保护上限
IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".tif", ".tiff")
_EXIF_ORIENTATION = 274
OCR_NOTICE = "本地 OCR 由本机模型生成，仅供草稿/校对参考；识别内容不会上传外网，重要内容请人工核对。"
BLOCKS_NOTICE = (
    "本轮提供文字块与原图定位；公式识别与表格结构可通过 STUDY_FORMULA_OCR / "
    "STUDY_TABLE_OCR 显式启用（默认关闭），未启用时 math 块 latex 为空、需人工校对。"
)

# 内容块能力声明：blocks=true 表示返回块与原图定位；math_recognition=false 表示基础
# 本地引擎不做公式识别（不伪造 LaTeX）。公式/表格后处理是可选增强，状态通过
# engine_meta()/engine_status() 的 enrichment 字段如实上报，不改动本常量。
BLOCK_SCHEMA_VERSION = "ocr-blocks-v1"
BLOCK_KINDS = ("text", "math", "table", "image")
CAPABILITIES = {
    "blocks": True,
    "math_recognition": False,
}

# 块 kind 启发式阈值（集中定义，便于后续调参和解释）：
# * math：数学符号字符占比 ≥ MATH_RATIO_MIN 且含运算符；含 LaTeX 反斜杠/公式定界符直接判 math；
#   中文较多且符号占比低于 MATH_STRONG_RATIO 的混排行按 text 处理（避免把普通题干误标公式）；
# * table：含 "|"/制表符，或由 2+ 空格分隔出 ≥3 列的行，连续 ≥TABLE_MIN_ROWS 行判 table。
MATH_SYMBOL_CHARS = frozenset("=×÷±√∑∫≤≥≠°²³^_{}[]\\→⇌")
MATH_OPERATORS = frozenset("=×÷±√∑∫≤≥≠→⇌")
MATH_RATIO_MIN = 0.12
MATH_STRONG_RATIO = 0.45
MATH_MIN_SYMBOLS = 2
TABLE_MIN_ROWS = 2
TABLE_SEPARATORS = ("|", "\t")
READ_ORDER_ROW_TOLERANCE = 6.0


class OcrError(RuntimeError):
    """本地 OCR 业务异常基类（统一异常，避免向上层泄漏第三方库细节）。"""


class OcrUnavailable(OcrError):
    """本地 OCR 不可用：开关关闭、依赖缺失或引擎初始化失败。"""


class OcrTimeout(OcrError):
    """单张图片/单页识别超过超时时间。"""


# ---------------------------------------------------------------------------
# 引擎：延迟加载单例 + 进程级并发信号量
# ---------------------------------------------------------------------------

_engine = None
_engine_error: str | None = None
_engine_lock = threading.Lock()
_infer_slots = threading.BoundedSemaphore(MAX_CONCURRENCY)


def _rapidocr_class():
    """延迟导入 RapidOCR 引擎类；依赖缺失时转成 OcrUnavailable。"""
    try:
        from rapidocr_onnxruntime import RapidOCR
    except Exception:  # ImportError / DLL、onnxruntime 初始化异常
        raise OcrUnavailable(
            "本地 OCR 依赖不可用（未安装 rapidocr-onnxruntime 或缺少 ONNX Runtime），将改用视觉模型识别"
        ) from None
    return RapidOCR


def _get_engine():
    """返回单例引擎；首次调用才加载模型，加载失败后不再反复尝试（可 reset_engine 重试）。"""
    global _engine, _engine_error
    with _engine_lock:
        if _engine is not None:
            return _engine
        if _engine_error is not None:
            raise OcrUnavailable(_engine_error)
        try:
            engine = _rapidocr_class()()
        except OcrUnavailable:
            raise
        except Exception as error:  # 模型文件损坏、onnxruntime 加载失败等
            _engine_error = (
                f"本地 OCR 引擎初始化失败（{type(error).__name__}），将改用视觉模型识别"
            )
            raise OcrUnavailable(_engine_error) from None
        _engine = engine
        return _engine


def reset_engine() -> None:
    """重置单例与失败缓存（模型升级或依赖安装后重试；测试也用它隔离状态）。"""
    global _engine, _engine_error
    with _engine_lock:
        _engine = None
        _engine_error = None


def _module_available() -> bool:
    """只探测依赖是否可导入，不加载模型（供 /info 与降级判断使用）。"""
    for name in (ENGINE_NAME.replace("-", "_"), "onnxruntime"):
        try:
            if importlib.util.find_spec(name) is None:
                return False
        except (ImportError, ValueError):
            return False
    return True


def local_ocr_available() -> bool:
    """本地 OCR 是否可用；不触发模型加载，导入失败/引擎初始化失败都返回 False。"""
    if ocr_mode() == "off":
        return False
    if _engine is not None:
        return True
    if _engine_error is not None:
        return False
    return _module_available()


def _engine_version() -> str | None:
    try:
        return importlib.metadata.version(ENGINE_NAME)
    except importlib.metadata.PackageNotFoundError:
        return None
    except Exception:
        return None


def _engine_models() -> list[str]:
    """列出随包分发的 ONNX 模型文件名（识别模型/检测模型/方向分类模型）。"""
    try:
        spec = importlib.util.find_spec("rapidocr_onnxruntime")
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


# ---------------------------------------------------------------------------
# 环境变量与限制（每次调用都读环境变量，测试可用 monkeypatch.setenv 覆盖）
# ---------------------------------------------------------------------------

def ocr_mode() -> str:
    mode = os.environ.get("STUDY_LOCAL_OCR", "").strip().lower()
    return mode if mode in OCR_MODES else "auto"


def _env_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(float(raw))
    except (TypeError, ValueError):
        return default
    return min(maximum, max(minimum, value))


def _env_float(name: str, default: float, *, minimum: float, maximum: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    return min(maximum, max(minimum, value))


def max_side() -> int:
    """识别图片最长边上限，默认 2400，可用 STUDY_OCR_MAX_SIDE 覆盖（限制在 600–6000）。"""
    return _env_int("STUDY_OCR_MAX_SIDE", DEFAULT_MAX_SIDE, minimum=600, maximum=6000)


def ocr_timeout() -> float:
    """单张图片/单页识别超时秒数，默认 45，可用 STUDY_OCR_TIMEOUT 覆盖（限制在 1–600）。"""
    return _env_float("STUDY_OCR_TIMEOUT", DEFAULT_TIMEOUT, minimum=1.0, maximum=600.0)


def max_pages() -> int:
    """单次同步 PDF 识别页数上限，默认 5，可用 STUDY_OCR_MAX_PAGES 覆盖（限制在 1–30）。"""
    return _env_int("STUDY_OCR_MAX_PAGES", DEFAULT_MAX_PAGES, minimum=1, maximum=30)


def _limits() -> dict:
    return {
        "max_side": max_side(),
        "timeout_seconds": ocr_timeout(),
        "max_pages": max_pages(),
        "concurrency": MAX_CONCURRENCY,
        "max_file_bytes": MAX_FILE,
    }


def _unavailable_reason() -> str:
    if ocr_mode() == "off":
        return "本地 OCR 已关闭（STUDY_LOCAL_OCR=off），将改用视觉模型识别"
    if _engine_error is not None:
        return _engine_error
    return "本地 OCR 引擎不可用（未安装 rapidocr-onnxruntime 或 onnxruntime），将改用视觉模型识别"


def _enrichment_state(module) -> str:
    """可选后处理引擎状态：``on`` / ``off`` / ``unavailable``（不加载模型）。"""
    try:
        if module.mode() != "on":
            return "off"
        return "on" if module.engine_status().get("available") else "unavailable"
    except Exception:
        return "unavailable"


def enrichment_status() -> dict:
    """公式识别与表格结构的独立开关状态（默认均为 off）。"""
    return {
        "formula": _enrichment_state(formula_ocr),
        "table": _enrichment_state(table_structure),
    }


def engine_meta() -> dict:
    """引擎名/版本/能力三元组（不加载模型），供 HTTP 响应与拍题草稿响应复用。

    ``engine_version`` 取随包安装的 RapidOCR 版本（未安装时为 None）；
    ``capabilities`` 如实声明本轮能力：有文字块与原图定位、不做公式识别；
    ``enrichment`` 为公式/表格可选后处理的开关状态（加法字段）。
    """
    return {
        "engine": ENGINE_NAME,
        "engine_version": _engine_version(),
        "capabilities": dict(CAPABILITIES),
        "enrichment": enrichment_status(),
    }


def engine_status() -> dict:
    """本地 OCR 状态：引擎、版本、模型、能力与限制；不触发模型加载。"""
    mode = ocr_mode()
    available = local_ocr_available()
    if mode == "off":
        notice = "本地 OCR 已关闭（STUDY_LOCAL_OCR=off）；拍题/扫描资料仅使用视觉模型。"
        notice += "文字块与原图定位随本地 OCR 一起停用；公式识别尚未启用（可选 STUDY_FORMULA_OCR=on）。"
    elif available:
        notice = OCR_NOTICE + BLOCKS_NOTICE
    else:
        notice = _unavailable_reason() + "；不影响其它功能。"
        notice += "文字块与原图定位仅在本地 OCR 可用时提供；公式识别尚未启用（可选 STUDY_FORMULA_OCR=on）。"
    version = _engine_version() if available else None
    return {
        "engine": ENGINE_NAME,
        "mode": mode,
        "available": available,
        "loaded": _engine is not None,
        "version": version,
        # 新增：引擎版本（与 version 同值）与内容块能力声明；旧字段保持不变。
        "engine_version": version,
        "capabilities": dict(CAPABILITIES),
        "enrichment": enrichment_status(),
        "block_schema_version": BLOCK_SCHEMA_VERSION,
        "formula": formula_ocr.engine_status(),
        "table": table_structure.engine_status(),
        "models": _engine_models() if available else [],
        "limits": _limits(),
        "notice": notice,
        "error": _engine_error,
    }


# ---------------------------------------------------------------------------
# 图片预处理：解码 → EXIF 转正 → 缩放到 max_side → BGR ndarray
# ---------------------------------------------------------------------------

def _open_image_bytes(raw: bytes) -> Image.Image:
    if not raw:
        raise ValueError("图片数据为空")
    if len(raw) > MAX_FILE:
        raise ValueError("图片不能超过 12 MB，请压缩后再识别")
    try:
        with Image.open(BytesIO(raw)) as opened:
            opened.load()
            return opened.copy()
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError):
        raise ValueError("文件不是有效的图片，请上传 PNG、JPG、WebP、BMP、GIF 或 TIFF 图片") from None


def _to_pil(source) -> Image.Image:
    """接受 bytes / PIL.Image / ``content_refs.image_part`` 的 ``{mime, data}`` 字典。"""
    if isinstance(source, Image.Image):
        return source
    if isinstance(source, dict):
        encoded = source.get("data")
        if not isinstance(encoded, str) or not encoded:
            raise ValueError("图片片段缺少 data 字段")
        try:
            raw = base64.b64decode(encoded)
        except (ValueError, TypeError):
            raise ValueError("图片片段不是合法的 base64 数据") from None
        return _open_image_bytes(raw)
    if isinstance(source, (bytes, bytearray)):
        return _open_image_bytes(bytes(source))
    raise ValueError("不支持的图片输入类型，请传 bytes、图片路径或图片片段")


def _normalize_pil(image: Image.Image, limit: int) -> Image.Image:
    if image.width <= 0 or image.height <= 0:
        raise ValueError("图片尺寸无效")
    if image.width * image.height > MAX_IMAGE_PIXELS:
        raise ValueError("图片像素过大，请先压缩到 6000 万像素以内再识别")
    try:
        orientation = image.getexif().get(_EXIF_ORIENTATION)
    except Exception:  # 部分图片没有 EXIF 或读取失败，按无需转正处理
        orientation = None
    if orientation not in (None, 1):
        transposed = ImageOps.exif_transpose(image)
        if transposed is not None:
            image = transposed
    if image.mode != "RGB":
        image = image.convert("RGB")
    longest = max(image.size)
    if longest > limit:
        image = image.copy()
        image.thumbnail((limit, limit), Image.LANCZOS)
    return image


def _image_bgr(image: Image.Image):
    """RGB PIL → BGR ndarray（RapidOCR 的 LoadImage 对 ndarray 按 BGR 处理）。"""
    import numpy as np

    array = np.asarray(image)
    if array.ndim != 3 or array.shape[2] != 3:
        raise ValueError("图片通道数不支持，请转换为 RGB 图片后再识别")
    return np.ascontiguousarray(array[:, :, ::-1])


def _prepare_image(source, limit: int):
    image = _normalize_pil(_to_pil(source), limit)
    return _image_bgr(image), image.width, image.height


# ---------------------------------------------------------------------------
# 推理：线程池内执行，进程级信号量限制并发
# ---------------------------------------------------------------------------

def _lines_from_result(result) -> list[dict]:
    lines: list[dict] = []
    for item in result or []:
        try:
            box, text, score = item[0], item[1], item[2]
        except (TypeError, IndexError, KeyError):
            continue
        text = str(text or "").strip()
        if not text:
            continue
        points: list[list[float]] = []
        for point in box or []:
            try:
                points.append([round(float(point[0]), 1), round(float(point[1]), 1)])
            except (TypeError, IndexError, ValueError):
                continue
        if not points:
            continue
        try:
            confidence = round(float(score), 4)
        except (TypeError, ValueError):
            confidence = 0.0
        lines.append({"text": text, "box": points, "score": confidence})
    return lines


# ---------------------------------------------------------------------------
# 内容块：在 OCR 行结果基础上构造（本轮一行一个块，order 即阅读顺序）
# ---------------------------------------------------------------------------

def _round_number(value, digits: int) -> float:
    """四舍五入并消除 -0.0，保证坐标/归一化值序列化稳定。"""
    number = round(float(value), digits)
    return 0.0 if number == 0 else number


def _clamp_score(value) -> float:
    """行置信度归一到 0–1（非法值按 0 处理），保留 4 位小数。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(number):
        return 0.0
    return _round_number(min(1.0, max(0.0, number)), 4)


def _is_math_line(text: str) -> bool:
    """判断一行是否像公式/数学表达式（启发式，口径可解释）。

    含 LaTeX 反斜杠或 ``$`` 定界符直接判定为 math；否则统计数学符号占比：
    含运算符（``=``、``×``、``√``、``^`` 等）且占比 ≥ ``MATH_RATIO_MIN``，或符号数
    ≥ ``MATH_MIN_SYMBOLS`` 且占比达标。中文较多（符号占比 < ``MATH_STRONG_RATIO``）的混排行
    按 text 处理，避免把普通题干误标成公式。本函数只做分类，不解析也不生成 LaTeX。
    """
    value = str(text or "").strip()
    if not value:
        return False
    if "\\" in value or "$" in value:
        return True
    compact = re.sub(r"\s+", "", value)
    if not compact:
        return False
    symbol_count = sum(1 for char in compact if char in MATH_SYMBOL_CHARS)
    if symbol_count <= 0:
        return False
    math_ratio = symbol_count / len(compact)
    has_operator = any(char in MATH_OPERATORS for char in compact)
    if not has_operator and symbol_count < MATH_MIN_SYMBOLS:
        return False
    cjk_count = sum(1 for char in compact if "\u4e00" <= char <= "\u9fff")
    if cjk_count and math_ratio < MATH_STRONG_RATIO:
        return False
    if has_operator:
        return math_ratio >= MATH_RATIO_MIN
    return symbol_count >= MATH_MIN_SYMBOLS and math_ratio >= MATH_RATIO_MIN


def _is_table_like_line(text: str) -> bool:
    """判断一行是否带表格特征：含 ``|``/制表符，或由 2+ 空格分出 ≥3 列（制表分隔/等宽列）。

    表头行通常不含数字，因此不再要求列里含数字；正常 OCR 文本行很少出现 2+ 连续空格
    分隔的 ≥3 列，误判风险可控，连续多行成立才判 table（见 :func:`build_blocks`）。
    """
    value = str(text or "")
    if any(separator in value for separator in TABLE_SEPARATORS):
        return True
    parts = [part for part in re.split(r"\s{2,}", value.strip()) if part]
    return len(parts) >= 3


def _line_bbox(line: dict, width: int, height: int) -> dict | None:
    """行四边形 → 原图像素包围盒（保留 1 位小数），越界坐标裁剪到图片范围。"""
    box = line.get("box") if isinstance(line, dict) else None
    points: list[tuple[float, float]] = []
    for point in box or []:
        try:
            x_value, y_value = float(point[0]), float(point[1])
        except (TypeError, IndexError, ValueError):
            continue
        if math.isfinite(x_value) and math.isfinite(y_value):
            points.append((x_value, y_value))
    if not points:
        return None
    image_width = max(0.0, float(width or 0))
    image_height = max(0.0, float(height or 0))
    left = min(point[0] for point in points)
    top = min(point[1] for point in points)
    right = max(point[0] for point in points)
    bottom = max(point[1] for point in points)
    if image_width > 0:
        left = min(max(0.0, left), image_width)
        right = min(max(0.0, right), image_width)
    else:
        left = max(0.0, left)
    if image_height > 0:
        top = min(max(0.0, top), image_height)
        bottom = min(max(0.0, bottom), image_height)
    else:
        top = max(0.0, top)
    return {
        "x": _round_number(left, 1),
        "y": _round_number(top, 1),
        "w": _round_number(max(0.0, right - left), 1),
        "h": _round_number(max(0.0, bottom - top), 1),
    }


def _norm_bbox(bbox: dict, width: int, height: int) -> dict:
    """像素包围盒 → 0–1 归一化（保留 4 位小数）。"""
    image_width = max(0.0, float(width or 0))
    image_height = max(0.0, float(height or 0))

    def ratio(value, total: float) -> float:
        if total <= 0:
            return 0.0
        return _round_number(min(1.0, max(0.0, float(value) / total)), 4)

    return {
        "x": ratio(bbox["x"], image_width),
        "y": ratio(bbox["y"], image_height),
        "w": ratio(bbox["w"], image_width),
        "h": ratio(bbox["h"], image_height),
    }


def build_blocks(lines, width: int, height: int, *, page: int = 1) -> list[dict]:
    """把 OCR 行结果构造成内容块列表（**一行一个块**，``order`` 即阅读顺序）。

    - 阅读顺序：先按行 ``top`` 聚类视觉行（相差 ``READ_ORDER_ROW_TOLERANCE`` 像素内视为同一行），
      同一视觉行内按 ``x`` 从左到右；``order`` 从 0 递增，``id`` 为 ``b1..bN``（页内稳定顺序）；
    - ``kind``：连续 ≥``TABLE_MIN_ROWS`` 个表格特征视觉行 → ``table``；否则数学特征 → ``math``；
      其余 → ``text``；``image`` 仅保留在 :data:`BLOCK_KINDS` 枚举中，本地 OCR 不产出图片块；
    - 不伪造公式识别：所有块 ``latex=None``，``math`` 块 ``needs_review=True``；
    - ``bbox`` 为原图像素坐标（0 保留 1 位小数），``norm`` 为 0–1 归一化（保留 4 位小数）；
    - ``page``：单图固定 1；PDF 为页码（1 基）；多图拍题由调用方传图片序号。
    """
    entries: list[dict] = []
    for line in lines or []:
        if not isinstance(line, dict):
            continue
        text_value = str(line.get("text") or "").strip()
        if not text_value:
            continue
        bbox = _line_bbox(line, width, height)
        if bbox is None:
            continue
        entries.append({
            "text": text_value,
            "bbox": bbox,
            "top": bbox["y"],
            "left": bbox["x"],
            "confidence": _clamp_score(line.get("score")),
            "table_like": _is_table_like_line(text_value),
        })
    if not entries:
        return []

    # 1) 视觉行聚类：按 top 排序，top 相差在容差内归入同一视觉行，行内按 x 从左到右。
    rows: list[list[dict]] = []
    for entry in sorted(entries, key=lambda item: (item["top"], item["left"])):
        if rows and abs(entry["top"] - rows[-1][0]["top"]) <= READ_ORDER_ROW_TOLERANCE:
            rows[-1].append(entry)
        else:
            rows.append([entry])
    for row in rows:
        row.sort(key=lambda item: (item["left"], item["top"]))

    # 2) 连续 ≥TABLE_MIN_ROWS 个表格特征视觉行判为表格行；单独一行表格特征不算表格。
    table_rows: set[int] = set()
    run_start: int | None = None
    for index, row in enumerate(rows):
        if any(item["table_like"] for item in row):
            if run_start is None:
                run_start = index
        else:
            if run_start is not None and index - run_start >= TABLE_MIN_ROWS:
                table_rows.update(range(run_start, index))
            run_start = None
    if run_start is not None and len(rows) - run_start >= TABLE_MIN_ROWS:
        table_rows.update(range(run_start, len(rows)))

    # 3) 按阅读顺序逐行生成块。
    engine_version = _engine_version()
    blocks: list[dict] = []
    for row_index, row in enumerate(rows):
        for entry in row:
            order = len(blocks)
            if row_index in table_rows:
                kind = "table"
            elif _is_math_line(entry["text"]):
                kind = "math"
            else:
                kind = "text"
            blocks.append({
                "id": f"b{order + 1}",
                "kind": kind,
                "text": entry["text"],
                "latex": None,
                "needs_review": kind == "math",
                "confidence": entry["confidence"],
                "bbox": dict(entry["bbox"]),
                "norm": _norm_bbox(entry["bbox"], width, height),
                "page": int(page),
                "order": order,
                "rotation": 0,
                "source": "rapidocr",
                "engine": ENGINE_NAME,
                "engine_version": engine_version,
            })
    return blocks


def _blocks_extension(lines, width: int, height: int, *, page: int = 1) -> dict:
    """识别结果可选新增字段：内容块 + 引擎名/版本 + 能力声明（旧字段保持不变）。"""
    return {"blocks": build_blocks(lines, width, height, page=page), **engine_meta()}


# ---------------------------------------------------------------------------
# 内容块可选后处理（公式识别 / 表格结构）：共享入口，四个调用方零复制
# ---------------------------------------------------------------------------

ENRICH_TABLE_MAX_BLOCKS = 20
ENRICH_TABLE_MAX_AREA_RATIO = 0.85
ENRICH_TABLE_MAX_SPAN = 30


def _enrichment_pil(image, width=None, height=None) -> Image.Image:
    """把调用方传入的图片统一成 RGB PIL，并缩放到与块 bbox 相同的坐标系。"""
    if isinstance(image, Image.Image):
        prepared = image
    else:
        prepared = _to_pil(image)
    # 原始上传图可能带 EXIF 旋转；块 bbox 来自已转正的预处理图，后处理裁图必须同步转正。
    try:
        orientation = prepared.getexif().get(_EXIF_ORIENTATION)
    except Exception:
        orientation = None
    if orientation not in (None, 1):
        transposed = ImageOps.exif_transpose(prepared)
        if transposed is not None:
            prepared = transposed
    if prepared.mode != "RGB":
        prepared = prepared.convert("RGB")
    try:
        target_width = int(width) if width is not None else 0
        target_height = int(height) if height is not None else 0
    except (TypeError, ValueError):
        target_width = target_height = 0
    if target_width > 0 and target_height > 0 and prepared.size != (target_width, target_height):
        prepared = prepared.resize((target_width, target_height), Image.LANCZOS)
    return prepared


def _pil_from_bgr(bgr) -> Image.Image:
    """OCR 预处理后的 BGR ndarray → RGB PIL（只在需要后处理时转换）。"""
    import numpy as np

    array = np.asarray(bgr)
    if array.ndim != 3 or array.shape[2] != 3:
        raise ValueError("后处理图像通道数不支持")
    return Image.fromarray(np.ascontiguousarray(array[:, :, ::-1]))


def _block_crop_with_origin(image: Image.Image, block: dict, *, pad_ratio: float = 0.03):
    """裁出块区域，返回 ``(crop, left, top)``；表格 OCR 框需按裁图原点换算。"""
    bbox = block.get("bbox")
    if not isinstance(bbox, dict):
        return None
    try:
        x_value = float(bbox.get("x"))
        y_value = float(bbox.get("y"))
        w_value = float(bbox.get("w"))
        h_value = float(bbox.get("h"))
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(item) for item in (x_value, y_value, w_value, h_value)):
        return None
    if w_value <= 0 or h_value <= 0:
        return None
    padding = max(2, int(min(w_value, h_value) * pad_ratio))
    left = max(0, int(x_value) - padding)
    top = max(0, int(y_value) - padding)
    right = min(image.width, int(x_value + w_value) + padding)
    bottom = min(image.height, int(y_value + h_value) + padding)
    if right - left < 4 or bottom - top < 4:
        return None
    return image.crop((left, top, right, bottom)), left, top


def _block_crop(image: Image.Image, block: dict, *, pad_ratio: float = 0.03):
    result = _block_crop_with_origin(image, block, pad_ratio=pad_ratio)
    return result[0] if result is not None else None


async def _enrich_formula_blocks(blocks: list[dict], image: Image.Image, enrichment: dict) -> None:
    """为公式块填 ``latex``；失败/超时/空结果保持原块不变。"""
    candidates = [
        (index, block) for index, block in enumerate(blocks)
        if block.get("kind") == "math" and not str(block.get("latex") or "").strip()
    ]
    if not candidates:
        return
    limit = formula_ocr.max_blocks()
    skipped = max(0, len(candidates) - limit)
    candidates = candidates[:limit]
    block_timeout = formula_ocr.timeout()
    # 关键：超时计时只在真正拿到推理槽位后开始。否则 20 个块同时提交线程池，
    # 后排在队列里等待时也会消耗超时时间，健康引擎下后半个块会被误判 timeout。
    gate = asyncio.Semaphore(formula_ocr.MAX_CONCURRENCY)

    async def recognize_one(item):
        index, block = item
        crop = _block_crop(image, block)
        if crop is None:
            return index, None, "bbox"
        async with gate:
            try:
                outcome = await asyncio.wait_for(
                    asyncio.to_thread(formula_ocr.recognize_crop, crop),
                    timeout=block_timeout,
                )
            except asyncio.TimeoutError:
                return index, None, "timeout"
            except Exception as error:  # 单块失败不影响其它块
                return index, None, type(error).__name__
        latex = str((outcome or {}).get("latex") or "").strip()
        if not latex:
            return index, None, "empty"
        return index, latex, None

    results = await asyncio.gather(*(recognize_one(item) for item in candidates))
    errors: list[str] = []
    for index, latex, error in results:
        if latex:
            blocks[index]["latex"] = latex[:formula_ocr.MAX_LATEX_CHARS]
            blocks[index]["latex_engine"] = formula_ocr.ENGINE_NAME
            # 识别结果仍属草稿：needs_review 保持 True，由用户改动后才由前端清除。
            blocks[index]["needs_review"] = True
        elif error:
            errors.append(str(error))
    if skipped:
        enrichment["formula_skipped"] = skipped
    if errors:
        enrichment["formula_error"] = errors[0]


def _table_block_groups(blocks: list[dict]) -> list[list[int]]:
    """把同一页上连续的表格块聚成区域（组内保持阅读顺序）。"""
    groups: list[list[int]] = []
    current: list[int] = []
    for index, block in enumerate(blocks):
        if block.get("kind") == "table" and isinstance(block.get("bbox"), dict):
            current.append(index)
        else:
            if current:
                groups.append(current)
            current = []
    if current:
        groups.append(current)
    return groups


def _group_bbox(blocks: list[dict], group: list[int]) -> dict | None:
    boxes = [blocks[index]["bbox"] for index in group]
    try:
        left = min(float(box["x"]) for box in boxes)
        top = min(float(box["y"]) for box in boxes)
        right = max(float(box["x"]) + float(box["w"]) for box in boxes)
        bottom = max(float(box["y"]) + float(box["h"]) for box in boxes)
    except (KeyError, TypeError, ValueError):
        return None
    if right <= left or bottom <= top:
        return None
    return {"x": left, "y": top, "w": right - left, "h": bottom - top}


async def _enrich_table_blocks(blocks: list[dict], image: Image.Image, enrichment: dict) -> None:
    """为连续表格块填结构；只写组内第一块，其余块保持原样。"""
    groups = [
        group for group in _table_block_groups(blocks)
        if len(group) <= ENRICH_TABLE_MAX_BLOCKS
    ]
    if not groups:
        return
    table_timeout = table_structure.timeout()
    image_area = max(1, image.width * image.height)
    errors: list[str] = []

    for group in groups:
        if len(group) > ENRICH_TABLE_MAX_SPAN:
            errors.append("span")
            continue
        bbox = _group_bbox(blocks, group)
        if bbox is None:
            continue
        if (bbox["w"] * bbox["h"]) / image_area > ENRICH_TABLE_MAX_AREA_RATIO:
            errors.append("area")
            continue
        crop_result = _block_crop_with_origin(image, {"bbox": bbox})
        if crop_result is None:
            continue
        crop, left, top = crop_result
        boxes, texts, scores = [], [], []
        for index in group:
            block_bbox = blocks[index]["bbox"]
            x_value = float(block_bbox.get("x") or 0)
            y_value = float(block_bbox.get("y") or 0)
            w_value = float(block_bbox.get("w") or 0)
            h_value = float(block_bbox.get("h") or 0)
            boxes.append([
                [x_value - left, y_value - top],
                [x_value + w_value - left, y_value - top],
                [x_value + w_value - left, y_value + h_value - top],
                [x_value - left, y_value + h_value - top],
            ])
            texts.append(str(blocks[index].get("text") or "")[:500])
            scores.append(float(blocks[index].get("confidence") or 0))
        try:
            outcome = await asyncio.wait_for(
                asyncio.to_thread(
                    table_structure.recognize_table,
                    crop,
                    ocr_results=(boxes, texts, scores),
                ),
                timeout=table_timeout,
            )
        except asyncio.TimeoutError:
            errors.append("timeout")
            continue
        except Exception as error:
            errors.append(type(error).__name__)
            continue
        html = str((outcome or {}).get("html") or "")
        if not html:
            errors.append("empty")
            continue
        first = blocks[group[0]]
        first["table_html"] = html[:table_structure.MAX_TABLE_HTML_CHARS]
        first["row_count"] = int((outcome or {}).get("row_count") or 0)
        first["col_count"] = int((outcome or {}).get("col_count") or 0)
        first["table_engine"] = table_structure.ENGINE_NAME
        first["table_span"] = len(group)
    if errors:
        enrichment["table_error"] = errors[0]


async def enrich_blocks(blocks, image=None, *, page: int = 1,
                        width=None, height=None) -> dict:
    """内容块共享后处理：公式块填 LaTeX、连续表格块填结构。

    返回 ``{"blocks": [...], "enrichment": {"formula": ..., "table": ...}}``；
    不改任何块的 ``kind``/``order``/``text``/``bbox``/``norm``，只在识别成功时
    写入加法字段。开关关闭、引擎不可用、单块失败或超时都保持原块不变。
    """
    result_blocks = [dict(block) for block in (blocks or []) if isinstance(block, dict)]
    enrichment = enrichment_status()
    if image is None or not result_blocks:
        return {"blocks": result_blocks, "enrichment": enrichment}
    if enrichment["formula"] != "on" and enrichment["table"] != "on":
        return {"blocks": result_blocks, "enrichment": enrichment}
    try:
        prepared = await asyncio.to_thread(_enrichment_pil, image, width, height)
    except Exception as error:
        enrichment["error"] = f"{type(error).__name__}: 图片无法用于后处理"
        return {"blocks": result_blocks, "enrichment": enrichment}
    if enrichment["formula"] == "on":
        await _enrich_formula_blocks(result_blocks, prepared, enrichment)
    if enrichment["table"] == "on":
        await _enrich_table_blocks(result_blocks, prepared, enrichment)
    return {"blocks": result_blocks, "enrichment": enrichment}


def _infer_guarded(bgr):
    with _infer_slots:
        engine = _get_engine()
        try:
            outcome = engine(bgr)
        except Exception as error:
            raise OcrError(f"本地 OCR 识别失败（{type(error).__name__}），请重试或改用视觉模型") from None
    if isinstance(outcome, tuple):
        result, _elapse = outcome[0], outcome[1] if len(outcome) > 1 else None
    else:
        result = outcome
    return _lines_from_result(result)


async def _recognize_prepared(
    bgr, width: int, height: int, *, started: float, timeout: float,
    include_blocks: bool = False, page: int = 1,
) -> dict:
    """识别预处理后的图片；``include_blocks=True`` 时追加 blocks/engine/能力字段。

    默认（``include_blocks=False``）返回结构与此前完全一致，旧调用方不受影响。
    """
    if not local_ocr_available():
        raise OcrUnavailable(_unavailable_reason())
    remaining = timeout - (time.monotonic() - started)
    if remaining <= 0:
        raise OcrTimeout(f"本地 OCR 超过 {timeout:g} 秒未完成，请缩小图片或分批识别")
    try:
        lines = await asyncio.wait_for(asyncio.to_thread(_infer_guarded, bgr), timeout=remaining)
    except asyncio.TimeoutError:
        raise OcrTimeout(f"本地 OCR 超过 {timeout:g} 秒未完成，请缩小图片或分批识别") from None
    text = "\n".join(line["text"] for line in lines)
    result = {
        "text": text,
        "lines": lines,
        "width": int(width),
        "height": int(height),
        "elapsed_ms": int((time.monotonic() - started) * 1000),
    }
    if include_blocks:
        extension = _blocks_extension(lines, width, height, page=page)
        if extension.get("blocks") and any(
            state == "on" for state in extension.get("enrichment", {}).values()
        ):
            try:
                prepared = await asyncio.to_thread(_pil_from_bgr, bgr)
                enriched = await enrich_blocks(
                    extension["blocks"], prepared, page=page,
                    width=width, height=height,
                )
                extension["blocks"] = enriched["blocks"]
                extension["enrichment"] = enriched["enrichment"]
            except Exception as error:  # 后处理只是增强：失败时保留原块与状态
                extension["enrichment"] = {
                    **enrichment_status(),
                    "error": f"{type(error).__name__}: 后处理失败",
                }
        result.update(extension)
    return result


# ---------------------------------------------------------------------------
# 图片识别
# ---------------------------------------------------------------------------

def _read_image_path(path) -> bytes:
    if path is None:
        raise ValueError("必须提供图片数据（data）或图片路径（path）")
    try:
        raw = Path(path).read_bytes()
    except OSError:
        raise ValueError("图片文件读取失败，请检查路径是否正确") from None
    if not raw:
        raise ValueError("图片文件为空")
    if len(raw) > MAX_FILE:
        raise ValueError("图片不能超过 12 MB，请压缩后再识别")
    return raw


def _resolve_max_side(value) -> int:
    if value is None:
        return max_side()
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ValueError("max_side 必须是正整数") from None
    if number < 1:
        raise ValueError("max_side 必须是正整数")
    return min(6000, max(320, number))


def _resolve_timeout(value) -> float:
    if value is None:
        return ocr_timeout()
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError("timeout 必须是正数秒数") from None
    if number <= 0:
        raise ValueError("timeout 必须是正数秒数")
    return min(600.0, max(0.1, number))


async def recognize_image(
    data=None, path=None, *, lang: str = "ch", max_side=None, timeout=None,
    include_blocks: bool = False,
) -> dict:
    """识别单张图片，返回 ``{text, lines, width, height, elapsed_ms}``。

    * ``data``：图片 bytes，也兼容 PIL.Image 与 ``content_refs.image_part`` 的 ``{mime, data}``；
    * ``path``：本地图片路径（与 ``data`` 二选一）；
    * ``lang``：仅 ``ch``/``en``（随包中文模型同时支持英文）；其它语言暂不支持；
    * ``max_side``：最长边上限，``None`` 时取 ``STUDY_OCR_MAX_SIDE``（默认 2400）；
    * ``timeout``：推理超时秒数，``None`` 时取 ``STUDY_OCR_TIMEOUT``（默认 45）；
    * ``include_blocks``：默认 ``False``，返回结构与本轮之前完全一致（旧调用方零改动）；
      传 ``True`` 时追加 ``blocks``（内容块+原图定位）、``engine``、``engine_version``、
      ``capabilities``。调用方需要块/公式校对时显式打开。

    引擎不可用抛 :class:`OcrUnavailable`，超时抛 :class:`OcrTimeout`，
    图片/参数问题抛 :class:`ValueError`（中文信息，供接口转 422）。
    """
    started = time.monotonic()
    if data is not None and path is not None:
        raise ValueError("请只提供图片数据（data）或图片路径（path）之一")
    if lang not in ("ch", "en"):
        raise ValueError("本地 OCR 仅支持中文/英文（lang 取 ch 或 en）")
    source = data if data is not None else _read_image_path(path)
    limit = _resolve_max_side(max_side)
    wait_seconds = _resolve_timeout(timeout)
    # 先在事件循环外完成解码/缩放/校验：坏图片立刻报错，也不会为非法输入加载模型。
    bgr, width, height = await asyncio.to_thread(_prepare_image, source, limit)
    return await _recognize_prepared(
        bgr, width, height, started=started, timeout=wait_seconds,
        include_blocks=include_blocks, page=1,
    )


# ---------------------------------------------------------------------------
# PDF 逐页识别（复用 pypdfium2 渲染）
# ---------------------------------------------------------------------------

def _read_pdf_source(path) -> bytes:
    if isinstance(path, (bytes, bytearray)):
        raw = bytes(path)
    elif path is None:
        raise ValueError("必须提供 PDF 文件路径或 PDF 数据")
    else:
        try:
            raw = Path(path).read_bytes()
        except OSError:
            raise ValueError("PDF 文件读取失败，请检查路径是否正确") from None
    if not raw:
        raise ValueError("PDF 文件为空")
    if len(raw) > MAX_PDF_BYTES:
        raise ValueError("PDF 超过 32 MB，请拆分后再识别")
    return raw


def _pdf_page_count(raw: bytes) -> int:
    import pypdfium2 as pdfium

    with pdfium.PdfDocument(raw) as doc:
        return len(doc)


def _render_pdf_page(raw: bytes, page_no: int, dpi: int) -> Image.Image:
    import pypdfium2 as pdfium

    scale = min(4.0, max(0.5, float(dpi) / 72.0))
    with pdfium.PdfDocument(raw) as doc:
        if page_no < 1 or page_no > len(doc):
            raise ValueError(f"第 {page_no} 页超出 PDF 页数范围")
        with closing(doc[page_no - 1]) as page, closing(page.render(scale=scale)) as bitmap:
            return bitmap.to_pil().convert("RGB")


def _select_pages(pages, total: int) -> list[int]:
    total = int(total or 0)
    if total <= 0:
        raise ValueError("PDF 没有可识别的页面")
    if pages is None:
        return list(range(1, total + 1))
    if isinstance(pages, str):
        raw_items: list = [part for part in re.split(r"[,，;；\s]+", pages.strip()) if part]
    elif isinstance(pages, (list, tuple, set, frozenset)):
        raw_items = list(pages)
    else:
        raw_items = [pages]
    selected: set[int] = set()
    for item in raw_items:
        numbers: list[int] = []
        if isinstance(item, bool):
            raise ValueError("页码参数格式无效")
        if isinstance(item, int):
            numbers = [item]
        elif isinstance(item, str):
            text = item.strip()
            if re.fullmatch(r"\d+", text):
                numbers = [int(text)]
            elif re.fullmatch(r"\d+\s*-\s*\d+", text):
                start_text, end_text = re.split(r"\s*-\s*", text)
                start, end = int(start_text), int(end_text)
                if end < start:
                    raise ValueError(f"页码范围「{text}」无效，结束页不能小于起始页")
                numbers = list(range(start, end + 1))
            else:
                raise ValueError(f"无法识别的页码「{text}」，请使用 1、1-3、1,3 这类写法")
        else:
            raise ValueError("页码参数格式无效")
        for number in numbers:
            if number < 1 or number > total:
                raise ValueError(f"页码超出范围：PDF 共 {total} 页，收到第 {number} 页")
            selected.add(number)
    if not selected:
        raise ValueError("请提供有效的页码")
    return sorted(selected)


async def recognize_pdf_pages(path, pages=None, dpi: int = 144, *, include_blocks: bool = False) -> list[dict]:
    """逐页识别 PDF，返回 ``[{"page", "text", "lines", "width", "height", "elapsed_ms"}]``。

    * ``path``：PDF 文件路径，也兼容 PDF bytes；
    * ``pages``：``None`` 表示全部页；支持 ``1``、``"1-3"``、``"1,3,5"``、``[1, 2]`` 等写法；
    * ``dpi``：渲染清晰度，默认 144（``scale=dpi/72``），限制在 72–400；
    * ``include_blocks``：默认 ``False`` 时每页结构与之前完全一致；``True`` 时每页追加
      ``blocks``（``page`` 为页码、``order`` 为页内阅读顺序）、``engine``、``engine_version``、
      ``capabilities``（单页渲染失败的页也会带 ``blocks: []`` 与引擎信息）；
    * 页数超过 ``STUDY_OCR_MAX_PAGES``（默认 5）抛 :class:`ValueError` 提示分批；
    * 单页渲染失败只在该页返回 ``error``，不中断整批。

    该函数是单次同步识别入口，长文档请由调用方按页分批。
    """
    raw = _read_pdf_source(path)
    try:
        total = await asyncio.to_thread(_pdf_page_count, raw)
    except Exception:
        raise ValueError("PDF 无法解析，请上传未加密的有效 PDF 文件") from None
    selected = _select_pages(pages, total)
    limit = max_pages()
    if len(selected) > limit:
        raise ValueError(
            f"本地 OCR 单次最多识别 {limit} 页 PDF，请用 pages 指定页码分批处理"
            f"（本次选择 {len(selected)} 页，共 {total} 页）"
        )
    if not local_ocr_available():
        raise OcrUnavailable(_unavailable_reason())
    try:
        render_dpi = int(dpi)
    except (TypeError, ValueError):
        render_dpi = 144
    render_dpi = min(400, max(72, render_dpi))
    page_max_side = max_side()
    page_timeout = ocr_timeout()
    results: list[dict] = []
    for page_no in selected:
        page_started = time.monotonic()
        try:
            image = await asyncio.to_thread(_render_pdf_page, raw, page_no, render_dpi)
        except Exception:
            page_result = {
                "page": page_no, "text": "", "lines": [], "width": 0, "height": 0,
                "elapsed_ms": int((time.monotonic() - page_started) * 1000),
                "error": f"第 {page_no} 页渲染失败，请检查 PDF 是否损坏或加密",
            }
            if include_blocks:
                page_result.update(_blocks_extension([], 0, 0, page=page_no))
            results.append(page_result)
            continue
        bgr, width, height = await asyncio.to_thread(_prepare_image, image, page_max_side)
        result = await _recognize_prepared(
            bgr, width, height, started=page_started, timeout=page_timeout,
            include_blocks=include_blocks, page=page_no,
        )
        results.append({"page": page_no, **result})
    return results


# ---------------------------------------------------------------------------
# HTTP 接口（GET /api/ai/ocr/info、POST /api/ai/ocr）
# ---------------------------------------------------------------------------

router = APIRouter(prefix="/api", tags=["AI local OCR"])


def _validate_upload_bytes(raw: bytes) -> None:
    if not raw:
        raise HTTPException(422, "文件为空")
    if len(raw) > MAX_FILE:
        raise HTTPException(413, "文件不能超过 12 MB，请压缩或分批上传")


async def _read_upload(file: UploadFile) -> tuple[str, bytes]:
    name = Path(file.filename or "上传文件").name[:180]
    raw = await file.read(MAX_FILE + 1)
    await file.close()
    _validate_upload_bytes(raw)
    return name, raw


@router.get("/ai/ocr/info")
def ocr_info(user=Depends(current_user)):
    """本地 OCR 就绪状态/版本/模型/限制，以及内容块能力声明；不加载模型。"""
    return engine_status()


@router.post("/ai/ocr")
async def ocr_upload(
    file: UploadFile = File(...),
    pages: str | None = Form(default=None),
    user=Depends(current_user),
):
    """同步识别一张图片或 ≤5 页 PDF（multipart）。

    * 图片返回 ``{kind:"image", text, lines, width, height, elapsed_ms, blocks,
      engine_version, capabilities, ...}``（旧字段全部保留）；
    * PDF 返回 ``{kind:"pdf", pages:[{page, text, ..., blocks, engine_version}],
      blocks, text, page_count, engine_version, capabilities, ...}``；每页 ``blocks`` 的
      ``page`` 为该页页码、``order`` 为页内阅读顺序，顶层 ``blocks`` 为各页块顺序拼接；
    * 本轮本地引擎不做公式识别：``blocks[].latex`` 恒为 null，``math`` 块
      ``needs_review=true``，需人工或后续 Pix2Text 适配层校对；
    * 页数超限/参数错误 422，文件超限 413，引擎不可用 503，识别超时 504；均要求登录。
    """
    name, raw = await _read_upload(file)
    suffix = Path(name).suffix.lower()
    is_pdf = suffix == ".pdf" or raw[:5] == b"%PDF-"
    started = time.monotonic()
    try:
        if is_pdf:
            page_results = await recognize_pdf_pages(raw, pages=pages, include_blocks=True)
            blocks = [
                block
                for page_result in page_results
                for block in (page_result.get("blocks") or [])
            ]
            version = _engine_version()
            return {
                "kind": "pdf",
                "filename": name,
                "engine": ENGINE_NAME,
                "version": version,
                "engine_version": version,
                "capabilities": dict(CAPABILITIES),
                "blocks": blocks,
                "page_count": len(page_results),
                "pages": page_results,
                "text": "\n\n".join(item["text"] for item in page_results if item.get("text")),
                "elapsed_ms": int((time.monotonic() - started) * 1000),
                "limits": _limits(),
                "notice": OCR_NOTICE + BLOCKS_NOTICE,
            }
        if suffix and suffix not in IMAGE_SUFFIXES:
            raise ValueError("仅支持 PNG、JPG、WebP、BMP、GIF、TIFF 图片或 PDF 文件")
        result = await recognize_image(raw, include_blocks=True)
        return {
            "kind": "image",
            "filename": name,
            "engine": ENGINE_NAME,
            "version": _engine_version(),
            **result,
            "limits": _limits(),
            "notice": OCR_NOTICE + BLOCKS_NOTICE,
        }
    except HTTPException:
        raise
    except OcrTimeout as error:
        raise HTTPException(504, str(error)) from None
    except OcrUnavailable as error:
        raise HTTPException(503, str(error)) from None
    except OcrError as error:
        raise HTTPException(503, str(error)) from None
    except ValueError as error:
        raise HTTPException(422, str(error)) from None
