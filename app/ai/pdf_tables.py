"""文本型 PDF 表格抽取（pdfplumber，可选依赖）：延迟加载、只读、失败即跳过。

设计边界（对齐《开源组件整合实施方案》§2.3 与 §1.1）：

* **默认关闭**：``STUDY_PDF_TABLES=on`` 才启用；未安装或关闭时调用方行为与现状
  逐字节一致。
* **只读、不落库**：只在知识清单上传的当次请求里解析内存中的 PDF，不写数据库、
  不落盘、不改原文。
* **失败不拖垮解析**：表格抽取抛错/超时由调用方捕获后只跳过追加，文本层与分块
  结果保持原样；本模块自身把依赖缺失与初始化失败统一转成
  :class:`PdfTablesUnavailable`。
* **口径与 DOCX 一致**：每行表格以 ``" | ".join(单元格)`` 的形式返回，供
  ``notes_agent._extract_document_pages()`` 追加到对应页的行列表末尾。

返回结构::

    [{"page": 1, "rows": [["姓名", "分数"], ["张三", "90"]]}, ...]
"""

from __future__ import annotations

import importlib.metadata
import importlib.util
from io import BytesIO
import os
from pathlib import Path
import threading
import time

__all__ = [
    "ENGINE_NAME",
    "PdfTablesError",
    "PdfTablesUnavailable",
    "extract_tables",
    "pdf_tables_available",
    "enabled",
    "engine_status",
    "reset_engine",
    "mode",
    "max_pages",
]

ENGINE_NAME = "pdfplumber"
PDF_TABLE_MODES = ("on", "off")
DEFAULT_MAX_PAGES = 50
MAX_ROWS_PER_TABLE = 300
MAX_TABLES_PER_PAGE = 20
MAX_CELL_CHARS = 300
MAX_COLUMNS = 60
NOTICE = (
    "文本型 PDF 表格由 pdfplumber 抽取，仅补充有表格线的表格行；"
    "扫描件与无边框表格不会凭空生成内容。"
)

_engine = None
_engine_error: str | None = None
_engine_lock = threading.Lock()


class PdfTablesError(RuntimeError):
    """PDF 表格抽取异常基类（不向上层泄漏第三方库细节）。"""


class PdfTablesUnavailable(PdfTablesError):
    """开关关闭、依赖缺失或模块初始化失败。"""


def mode() -> str:
    """``STUDY_PDF_TABLES``：只有显式 ``on`` 才启用，其余值按关闭处理。"""
    value = os.environ.get("STUDY_PDF_TABLES", "").strip().lower()
    return "on" if value == "on" else "off"


def max_pages() -> int:
    """单份文档最多处理的页数，默认 50，可用 STUDY_PDF_TABLE_PAGES 覆盖（1–200）。"""
    raw = os.environ.get("STUDY_PDF_TABLE_PAGES", "").strip()
    if not raw:
        return DEFAULT_MAX_PAGES
    try:
        value = int(float(raw))
    except (TypeError, ValueError):
        return DEFAULT_MAX_PAGES
    return min(200, max(1, value))


def _module_available() -> bool:
    """只探测依赖是否可导入，不触发 pdfminer 初始化。"""
    try:
        return importlib.util.find_spec("pdfplumber") is not None
    except (ImportError, ValueError):
        return False


def _engine_version() -> str | None:
    try:
        return importlib.metadata.version(ENGINE_NAME)
    except importlib.metadata.PackageNotFoundError:
        return None
    except Exception:
        return None


def _get_engine():
    """返回 pdfplumber 模块单例；首次调用才导入，失败缓存（可 reset_engine 重试）。"""
    global _engine, _engine_error
    with _engine_lock:
        if _engine is not None:
            return _engine
        if _engine_error is not None:
            raise PdfTablesUnavailable(_engine_error)
        try:
            import pdfplumber  # 延迟导入：未安装时不影响应用启动
        except Exception as error:  # ImportError / pdfminer 依赖缺失
            _engine_error = (
                f"PDF 表格抽取依赖不可用（{type(error).__name__}），"
                "将跳过表格抽取并保留文本层结果"
            )
            raise PdfTablesUnavailable(_engine_error) from None
        if not callable(getattr(pdfplumber, "open", None)):
            _engine_error = "pdfplumber 模块缺少 open()，无法读取 PDF"
            raise PdfTablesUnavailable(_engine_error)
        _engine = pdfplumber
        return _engine


def reset_engine() -> None:
    """重置单例与失败缓存（依赖安装后重试；测试隔离用）。"""
    global _engine, _engine_error
    with _engine_lock:
        _engine = None
        _engine_error = None


def pdf_tables_available() -> bool:
    """开关开启且依赖可用；不触发解析。"""
    if mode() != "on":
        return False
    if _engine is not None:
        return True
    if _engine_error is not None:
        return False
    return _module_available()


def enabled() -> bool:
    """调用方的唯一开关判断：``STUDY_PDF_TABLES=on`` 且依赖可用。"""
    return pdf_tables_available()


def _unavailable_reason() -> str:
    if mode() != "on":
        return "PDF 表格抽取已关闭（STUDY_PDF_TABLES=off）"
    if _engine_error is not None:
        return _engine_error
    return "PDF 表格抽取不可用（未安装可选依赖 pdfplumber）"


def engine_status() -> dict:
    """引擎状态（不加载 pdfminer）：字段与 ocr.engine_status() 对齐。"""
    available = pdf_tables_available()
    if mode() != "on":
        notice = "PDF 表格抽取已关闭（STUDY_PDF_TABLES=off）；上传 PDF 只保留文本层结果。"
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
        "capabilities": {"text_pdf_tables": True, "scanned_pdf_tables": False},
        "limits": {
            "max_pages": max_pages(),
            "max_rows_per_table": MAX_ROWS_PER_TABLE,
            "max_tables_per_page": MAX_TABLES_PER_PAGE,
            "max_cell_chars": MAX_CELL_CHARS,
        },
        "notice": notice,
        "error": _engine_error,
    }


def _clean_cell(value) -> str:
    """单元格文本归一：None → 空串，折叠换行/多空格，限长。"""
    text = " ".join(str(value or "").split())
    return text[:MAX_CELL_CHARS]


def _clean_table(table) -> list[list[str]]:
    rows: list[list[str]] = []
    for raw_row in table or []:
        cells = [_clean_cell(cell) for cell in (raw_row or [])[:MAX_COLUMNS]]
        if not any(cells):
            continue
        rows.append(cells)
        if len(rows) >= MAX_ROWS_PER_TABLE:
            break
    return rows


def extract_tables(source, pages=None) -> list[dict]:
    """抽取文本型 PDF 的表格（同步，可能耗时，调用方应放线程池）。

    ``source`` 可以是文件路径、``bytes`` 或 ``Path``；``pages`` 为 1 基页码集合，
    缺省时取前 :func:`max_pages` 页。返回 ``[{"page", "rows"}]``，按页码升序；
    少于两行或少于两列的表格不返回（与"表格行"口径一致）。
    """
    if not pdf_tables_available():
        raise PdfTablesUnavailable(_unavailable_reason())
    engine = _get_engine()

    if isinstance(source, (bytes, bytearray)):
        raw = bytes(source)
        opener = engine.open(BytesIO(raw))
    else:
        path = Path(source)
        if not path.is_file():
            raise PdfTablesError("PDF 文件不存在，无法抽取表格")
        opener = engine.open(str(path))

    started = time.monotonic()
    results: list[dict] = []
    try:
        with opener as document:
            page_count = len(document.pages)
            if pages is None:
                selected = list(range(1, min(page_count, max_pages()) + 1))
            else:
                limit = max_pages()
                selected = sorted({
                    int(number) for number in pages
                    if isinstance(number, int) and 1 <= number <= page_count
                })[:limit]
            for page_no in selected:
                page = document.pages[page_no - 1]
                try:
                    tables = page.extract_tables() or []
                except Exception:  # 单页抽取失败：跳过该页，不影响其它页
                    continue
                page_tables: list[dict] = []
                for table in tables[:MAX_TABLES_PER_PAGE]:
                    rows = _clean_table(table)
                    if len(rows) < 2:
                        continue
                    if max((len(row) for row in rows), default=0) < 2:
                        continue
                    page_tables.append({"rows": rows})
                if page_tables:
                    results.extend(
                        {"page": page_no, "rows": table["rows"]} for table in page_tables
                    )
    except PdfTablesError:
        raise
    except Exception as error:  # 损坏 PDF / 内存限制 / pdfminer 异常
        raise PdfTablesError(
            f"PDF 表格抽取失败（{type(error).__name__}），将保留文本层结果"
        ) from None
    finally:
        elapsed_ms = int((time.monotonic() - started) * 1000)
        # 耗时只放调试属性，不进入返回结构（返回结构保持最小、可序列化）。
        extract_tables.last_elapsed_ms = elapsed_ms
    return results


extract_tables.last_elapsed_ms = 0
