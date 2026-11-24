"""题目内容块在导出链路中的统一读取与渲染。

题目可能来自三处：应用题库 ``pl_questions.content_json``、题库快照或试卷快照。
结构化块字段并不完全一致（``blocks`` / ``stem_blocks`` / ``shared_blocks``），
本模块只做三件事：

* :func:`content_blocks` 从任意容器里取出块列表并规整为统一形状；
* :func:`blocks_to_html` 为 HTML / Anki 导出渲染（文本转义、公式用 ``\\(...\\)``、
  表格再次走服务端白名单清洗）；
* :func:`table_rows` 把 ``table_html`` 还原成二维文本，供 Word/纯文本导出使用。

没有块时一律返回空列表，调用方回退旧字段，保证历史数据导出不变。
"""
from __future__ import annotations

from html import escape
from html.parser import HTMLParser
import re
from typing import Iterable

__all__ = [
    "BLOCK_KINDS",
    "content_blocks",
    "blocks_to_html",
    "blocks_plain_text",
    "table_rows",
]

BLOCK_KINDS = frozenset({"text", "math", "table", "image"})
_MAX_BLOCKS = 200
_MAX_TEXT = 4000
_MAX_LATEX = 2000


def _as_list(value) -> list:
    return value if isinstance(value, list) else []


def content_blocks(source) -> list[dict]:
    """从快照/题目/content 容器中取出可导出的内容块。"""
    if isinstance(source, dict):
        raw = source.get("blocks")
        if not isinstance(raw, list):
            content = source.get("content")
            raw = content.get("blocks") if isinstance(content, dict) else None
        if not isinstance(raw, list):
            raw = source.get("stem_blocks")
    else:
        raw = source
    blocks: list[dict] = []
    for item in _as_list(raw)[:_MAX_BLOCKS]:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind") or item.get("type") or "text").strip().lower()
        if kind not in BLOCK_KINDS:
            kind = "text"
        text = str(item.get("text") or "")[:_MAX_TEXT]
        latex = str(item.get("latex") or "").strip()[:_MAX_LATEX]
        table_html = str(item.get("table_html") or "").strip()
        if kind == "math" and not latex:
            continue
        if kind != "math" and not text.strip() and not table_html:
            continue
        block = {"kind": kind, "text": text, "latex": latex or None}
        if table_html:
            block["table_html"] = table_html
        blocks.append(block)
    return blocks


class _TableRows(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[str]] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag == "tr":
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in ("td", "th") and self._cell is not None and self._row is not None:
            self._row.append(re.sub(r"\s+", " ", "".join(self._cell)).strip())
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if self._row:
                self.rows.append(self._row)
            self._row = None


def table_rows(html: str) -> list[list[str]]:
    """把清洗后的 ``table_html`` 还原为二维文本；解析失败返回空列表。"""
    parser = _TableRows()
    try:
        parser.feed(str(html or ""))
        parser.close()
    except Exception:
        return []
    return parser.rows


def _safe_table_html(html: str) -> str:
    try:
        from .ai.table_structure import sanitize_table_html

        return sanitize_table_html(html)
    except Exception:
        return ""


def blocks_to_html(blocks: Iterable[dict], *, sanitize_tables: bool = True) -> str:
    """块 → 导出用 HTML；文本转义，公式用 ``\\(...\\)``，表格走白名单清洗。"""
    parts: list[str] = []
    for block in blocks or []:
        kind = block.get("kind")
        if kind == "table":
            html = _safe_table_html(block.get("table_html") or "") if sanitize_tables else ""
            if html:
                parts.append(f'<div class="zx-table">{html}</div>')
            elif block.get("text"):
                parts.append(f'<div class="zx-text">{escape(block["text"])}</div>')
        elif kind == "math":
            latex = str(block.get("latex") or "").strip()
            if latex:
                parts.append(f'<div class="zx-math">\\({escape(latex)}\\)</div>')
        elif block.get("text"):
            parts.append(f'<div class="zx-text">{escape(block["text"]).replace(chr(10), "<br>")}</div>')
    return "".join(parts)


def blocks_plain_text(blocks: Iterable[dict], *, latex_delimiters: bool = True) -> str:
    """块 → 纯文本；表格用 `` | `` 连接单元格，缺失结构时退回文本。"""
    lines: list[str] = []
    for block in blocks or []:
        kind = block.get("kind")
        if kind == "table":
            rows = table_rows(block.get("table_html") or "")
            if rows:
                lines.extend(" | ".join(row) for row in rows)
            elif block.get("text"):
                lines.append(str(block["text"]))
        elif kind == "math":
            latex = str(block.get("latex") or "").strip()
            if latex:
                lines.append(f"${latex}$" if latex_delimiters else latex)
        elif block.get("text"):
            lines.append(str(block["text"]))
    return "\n".join(line for line in lines if line.strip())
