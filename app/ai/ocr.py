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
"""
from __future__ import annotations

import asyncio
import base64
from contextlib import closing
from io import BytesIO
import importlib.metadata
import importlib.util
import os
from pathlib import Path
import re
import threading
import time

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from PIL import Image, ImageOps, UnidentifiedImageError

from ..auth import current_user

__all__ = [
    "ENGINE_NAME",
    "OcrError",
    "OcrUnavailable",
    "OcrTimeout",
    "local_ocr_available",
    "engine_status",
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


def engine_status() -> dict:
    """本地 OCR 状态：引擎、版本、模型与限制；不触发模型加载。"""
    mode = ocr_mode()
    available = local_ocr_available()
    if mode == "off":
        notice = "本地 OCR 已关闭（STUDY_LOCAL_OCR=off）；拍题/扫描资料仅使用视觉模型。"
    elif available:
        notice = OCR_NOTICE
    else:
        notice = _unavailable_reason() + "；不影响其它功能。"
    return {
        "engine": ENGINE_NAME,
        "mode": mode,
        "available": available,
        "loaded": _engine is not None,
        "version": _engine_version() if available else None,
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


async def _recognize_prepared(bgr, width: int, height: int, *, started: float, timeout: float) -> dict:
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
    return {
        "text": text,
        "lines": lines,
        "width": int(width),
        "height": int(height),
        "elapsed_ms": int((time.monotonic() - started) * 1000),
    }


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


async def recognize_image(data=None, path=None, *, lang: str = "ch", max_side=None, timeout=None) -> dict:
    """识别单张图片，返回 ``{text, lines, width, height, elapsed_ms}``。

    * ``data``：图片 bytes，也兼容 PIL.Image 与 ``content_refs.image_part`` 的 ``{mime, data}``；
    * ``path``：本地图片路径（与 ``data`` 二选一）；
    * ``lang``：仅 ``ch``/``en``（随包中文模型同时支持英文）；其它语言暂不支持；
    * ``max_side``：最长边上限，``None`` 时取 ``STUDY_OCR_MAX_SIDE``（默认 2400）；
    * ``timeout``：推理超时秒数，``None`` 时取 ``STUDY_OCR_TIMEOUT``（默认 45）。

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
    return await _recognize_prepared(bgr, width, height, started=started, timeout=wait_seconds)


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


async def recognize_pdf_pages(path, pages=None, dpi: int = 144) -> list[dict]:
    """逐页识别 PDF，返回 ``[{"page", "text", "lines", "width", "height", "elapsed_ms"}]``。

    * ``path``：PDF 文件路径，也兼容 PDF bytes；
    * ``pages``：``None`` 表示全部页；支持 ``1``、``"1-3"``、``"1,3,5"``、``[1, 2]`` 等写法；
    * ``dpi``：渲染清晰度，默认 144（``scale=dpi/72``），限制在 72–400；
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
            results.append({
                "page": page_no, "text": "", "lines": [], "width": 0, "height": 0,
                "elapsed_ms": int((time.monotonic() - page_started) * 1000),
                "error": f"第 {page_no} 页渲染失败，请检查 PDF 是否损坏或加密",
            })
            continue
        bgr, width, height = await asyncio.to_thread(_prepare_image, image, page_max_side)
        result = await _recognize_prepared(
            bgr, width, height, started=page_started, timeout=page_timeout,
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
    """本地 OCR 就绪状态/版本/模型/限制，供设置页与前端能力探测；不加载模型。"""
    return engine_status()


@router.post("/ai/ocr")
async def ocr_upload(
    file: UploadFile = File(...),
    pages: str | None = Form(default=None),
    user=Depends(current_user),
):
    """同步识别一张图片或 ≤5 页 PDF（multipart）。

    * 图片返回 ``{kind:"image", text, lines, width, height, elapsed_ms, ...}``；
    * PDF 返回 ``{kind:"pdf", pages:[{page, text, ...}], text, page_count, ...}``；
    * 页数超限/参数错误 422，文件超限 413，引擎不可用 503，识别超时 504；均要求登录。
    """
    name, raw = await _read_upload(file)
    suffix = Path(name).suffix.lower()
    is_pdf = suffix == ".pdf" or raw[:5] == b"%PDF-"
    started = time.monotonic()
    try:
        if is_pdf:
            page_results = await recognize_pdf_pages(raw, pages=pages)
            return {
                "kind": "pdf",
                "filename": name,
                "engine": ENGINE_NAME,
                "version": _engine_version(),
                "page_count": len(page_results),
                "pages": page_results,
                "text": "\n\n".join(item["text"] for item in page_results if item.get("text")),
                "elapsed_ms": int((time.monotonic() - started) * 1000),
                "limits": _limits(),
                "notice": OCR_NOTICE,
            }
        if suffix and suffix not in IMAGE_SUFFIXES:
            raise ValueError("仅支持 PNG、JPG、WebP、BMP、GIF、TIFF 图片或 PDF 文件")
        result = await recognize_image(raw)
        return {
            "kind": "image",
            "filename": name,
            "engine": ENGINE_NAME,
            "version": _engine_version(),
            **result,
            "limits": _limits(),
            "notice": OCR_NOTICE,
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
