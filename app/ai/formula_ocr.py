"""公式图片识别（RapidLaTeXOCR，可选依赖）：延迟加载、失败缓存、可重置。

按《开源组件整合实施方案》§3.1 与 §1.1 落地：

* ``STUDY_FORMULA_OCR=on`` 才启用（默认关闭）；未安装/关闭/初始化失败时调用方
  行为与现状逐字节一致，公式块 ``latex`` 保持 ``None``、``needs_review`` 保持 ``True``。
* 同步推理由调用方经 ``asyncio.to_thread`` 执行，并用进程级信号量限制并发；
  单块超时由调用方用 ``asyncio.wait_for`` 控制（``STUDY_FORMULA_TIMEOUT``）。
* 单块输入最长边限制 1600 像素，输出 LaTeX 截断到 2000 字符；识别结果只作为
  草稿供人工校对，不冒充标准答案。
"""
from __future__ import annotations

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
    "FormulaOcrError",
    "FormulaUnavailable",
    "recognize_crop",
    "local_formula_available",
    "engine_status",
    "engine_meta",
    "reset_engine",
    "mode",
    "max_blocks",
    "timeout",
    "MAX_CROP_SIDE",
    "MAX_LATEX_CHARS",
]

ENGINE_NAME = "rapid-latex-ocr"
FORMULA_MODES = ("on", "off")
DEFAULT_TIMEOUT = 5.0
DEFAULT_MAX_BLOCKS = 20
MAX_CROP_SIDE = 1600
MAX_LATEX_CHARS = 2000
MAX_CONCURRENCY = 2
NOTICE = (
    "公式识别由 RapidLaTeXOCR 本地模型生成，面向印刷体公式；"
    "手写体与复杂排版可能出错，识别结果仍需人工核对。"
)

_engine = None
_engine_error: str | None = None
_engine_lock = threading.Lock()
_infer_slots = threading.BoundedSemaphore(MAX_CONCURRENCY)


class FormulaOcrError(RuntimeError):
    """公式识别异常基类。"""


class FormulaUnavailable(FormulaOcrError):
    """开关关闭、依赖缺失或引擎初始化失败。"""


def mode() -> str:
    value = os.environ.get("STUDY_FORMULA_OCR", "").strip().lower()
    return "on" if value == "on" else "off"


def max_blocks() -> int:
    """单次请求最多处理的公式块数，默认 20，可用 STUDY_FORMULA_MAX_BLOCKS 覆盖（1–100）。"""
    raw = os.environ.get("STUDY_FORMULA_MAX_BLOCKS", "").strip()
    if not raw:
        return DEFAULT_MAX_BLOCKS
    try:
        value = int(float(raw))
    except (TypeError, ValueError):
        return DEFAULT_MAX_BLOCKS
    return min(100, max(1, value))


def timeout() -> float:
    """单块公式识别超时秒数，默认 5，可用 STUDY_FORMULA_TIMEOUT 覆盖（0.5–60）。"""
    raw = os.environ.get("STUDY_FORMULA_TIMEOUT", "").strip()
    if not raw:
        return DEFAULT_TIMEOUT
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT
    return min(60.0, max(0.5, value))


def _module_available() -> bool:
    for name in ("rapid_latex_ocr", "onnxruntime"):
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
        spec = importlib.util.find_spec("rapid_latex_ocr")
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


def _get_engine():
    """返回 LaTeXOCR 单例；首次调用才加载模型（可能触发模型下载），失败缓存。"""
    global _engine, _engine_error
    with _engine_lock:
        if _engine is not None:
            return _engine
        if _engine_error is not None:
            raise FormulaUnavailable(_engine_error)
        try:
            from rapid_latex_ocr import LaTeXOCR
        except Exception as error:  # ImportError / onnxruntime DLL 等
            _engine_error = (
                f"公式识别依赖不可用（{type(error).__name__}），"
                "公式块将保持 latex 为空并标记需人工校对"
            )
            raise FormulaUnavailable(_engine_error) from None
        try:
            engine = LaTeXOCR()
        except Exception as error:  # 模型文件缺失/下载失败/onnxruntime 初始化失败
            _engine_error = (
                f"公式识别引擎初始化失败（{type(error).__name__}），"
                "公式块将保持 latex 为空并标记需人工校对"
            )
            raise FormulaUnavailable(_engine_error) from None
        _engine = engine
        return _engine


def reset_engine() -> None:
    """重置单例与失败缓存（安装依赖或模型后重试；测试隔离用）。"""
    global _engine, _engine_error
    with _engine_lock:
        _engine = None
        _engine_error = None


def local_formula_available() -> bool:
    if mode() != "on":
        return False
    if _engine is not None:
        return True
    if _engine_error is not None:
        return False
    return _module_available()


def _unavailable_reason() -> str:
    if mode() != "on":
        return "公式识别已关闭（STUDY_FORMULA_OCR=off）"
    if _engine_error is not None:
        return _engine_error
    return "公式识别不可用（未安装可选依赖 rapid-latex-ocr 或 onnxruntime）"


def engine_meta() -> dict:
    return {
        "engine": ENGINE_NAME,
        "engine_version": _engine_version(),
        "capabilities": {"formula_latex": True, "handwriting": False},
    }


def engine_status() -> dict:
    available = local_formula_available()
    if mode() != "on":
        notice = "公式识别已关闭（STUDY_FORMULA_OCR=off）；math 块 latex 为空、需人工校对。"
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
        "capabilities": {"formula_latex": True, "handwriting": False},
        "limits": {
            "timeout_seconds": timeout(),
            "max_blocks": max_blocks(),
            "max_crop_side": MAX_CROP_SIDE,
            "max_latex_chars": MAX_LATEX_CHARS,
            "concurrency": MAX_CONCURRENCY,
        },
        "models": _engine_models() if available else [],
        "notice": notice,
        "error": _engine_error,
    }


def _to_pil(source) -> Image.Image:
    if isinstance(source, Image.Image):
        return source
    if isinstance(source, dict):
        import base64

        encoded = source.get("data")
        if not isinstance(encoded, str) or not encoded:
            raise ValueError("公式裁图片段缺少 data 字段")
        try:
            raw = base64.b64decode(encoded)
        except (ValueError, TypeError):
            raise ValueError("公式裁图不是合法的 base64 数据") from None
        source = raw
    if isinstance(source, (bytes, bytearray)):
        try:
            with Image.open(BytesIO(bytes(source))) as opened:
                opened.load()
                return opened.copy()
        except (UnidentifiedImageError, OSError, ValueError):
            raise ValueError("公式裁图不是有效的图片") from None
    raise ValueError("公式裁图仅支持 PIL.Image、bytes 或图片片段")


def _prepare_crop(source) -> Image.Image:
    image = _to_pil(source)
    if image.mode != "RGB":
        image = image.convert("RGB")
    if max(image.size) > MAX_CROP_SIDE:
        image = image.copy()
        image.thumbnail((MAX_CROP_SIDE, MAX_CROP_SIDE), Image.LANCZOS)
    return image


def recognize_crop(image) -> dict:
    """识别一块公式裁图，返回 ``{"latex", "elapsed_ms"}``（同步）。

    依赖不可用抛 :class:`FormulaUnavailable`；引擎异常抛 :class:`FormulaOcrError`。
    识别结果为空时 ``latex`` 为空字符串，由调用方保持原块不变。
    """
    if not local_formula_available():
        raise FormulaUnavailable(_unavailable_reason())
    started = time.monotonic()
    prepared = _prepare_crop(image)
    try:
        import numpy as np
    except Exception:
        raise FormulaOcrError("公式识别缺少 numpy 依赖") from None
    # LaTeXOCR 的 LoadImage 将 3 通道 ndarray 视为 RGB 并内部转 BGR。
    array = np.ascontiguousarray(np.asarray(prepared))
    with _infer_slots:
        engine = _get_engine()
        try:
            outcome = engine(array)
        except Exception as error:
            raise FormulaOcrError(
                f"公式识别失败（{type(error).__name__}），该块保持原样"
            ) from None
    latex = ""
    if isinstance(outcome, dict):
        latex = str(outcome.get("latex") or "")
    elif isinstance(outcome, (tuple, list)):
        latex = str(outcome[0]) if outcome else ""
    else:
        latex = str(outcome or "")
    latex = latex.strip()[:MAX_LATEX_CHARS]
    return {
        "latex": latex,
        "elapsed_ms": int((time.monotonic() - started) * 1000),
    }
