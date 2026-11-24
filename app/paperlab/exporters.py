"""试卷工坊 P1：试卷导出（Word ``docx`` / 自包含打印 HTML）。

- 导出只读 ``pl_papers`` + ``pl_paper_items.snapshot_json``，与建卷时冻结的快照一致，
  不重新读取题库（题目之后被修改/删除也不影响已导出内容）。
- Word 用 python-docx（已随环境安装，未新增 requirements 依赖）：标题、说明、分节、
  连续题号、题干纯文本（简单 LaTeX 转文本）、选项 2/4 列、答题空间、页脚页码；可选附答案页。
- HTML 为打印用自包含页面（内联 CSS、@page、``break-inside: avoid``），配图内联为 data URL；
  允许同源 iframe 嵌入以便前端调用 ``contentWindow.print()`` 打印或存 PDF。
"""
from __future__ import annotations

import base64
import html as html_lib
import io
import re
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi.responses import HTMLResponse

from .. import storage
from ..auth import current_user
from ..content_blocks import blocks_plain_text, blocks_to_html, content_blocks, table_rows
from . import assets, db, papers
from .db import ASSET_FOLDER, json_load

router = APIRouter()

_TYPE_LABELS = {
    "SINGLE": "单选题", "MULTIPLE": "多选题", "BLANK": "填空题",
    "SUBJECTIVE": "主观题", "MIXED": "混合题",
}
_CN_NUMBERS = "一二三四五六七八九十"
_MAX_EMBED_IMAGE_BYTES = 2 * 1024 * 1024
_TEX_COMMANDS = {
    "\\times": "×", "\\cdot": "·", "\\div": "÷", "\\pm": "±", "\\mp": "∓",
    "\\le": "≤", "\\leq": "≤", "\\ge": "≥", "\\geq": "≥", "\\neq": "≠", "\\ne": "≠",
    "\\approx": "≈", "\\infty": "∞", "\\pi": "π", "\\alpha": "α", "\\beta": "β",
    "\\gamma": "γ", "\\theta": "θ", "\\lambda": "λ", "\\mu": "μ", "\\sigma": "σ",
    "\\Delta": "Δ", "\\angle": "∠", "\\triangle": "△", "\\because": "∵", "\\therefore": "∴",
    "\\in": "∈", "\\notin": "∉", "\\subset": "⊂", "\\subseteq": "⊆", "\\cup": "∪", "\\cap": "∩",
    "\\emptyset": "∅", "\\varnothing": "∅", "\\rightarrow": "→", "\\to": "→",
    "\\Rightarrow": "⇒", "\\Leftrightarrow": "⇔", "\\perp": "⊥", "\\parallel": "∥",
    "\\degree": "°", "\\circ": "°", "\\ldots": "…", "\\cdots": "…", "\\sum": "∑",
    "\\int": "∫", "\\lim": "lim", "\\sin": "sin", "\\cos": "cos", "\\tan": "tan",
    "\\log": "log", "\\ln": "ln", "\\sqrt": "√",
}
_SUPERSCRIPTS = str.maketrans("0123456789+-=()n", "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁼⁽⁾ⁿ")
_SUBSCRIPTS = str.maketrans("0123456789", "₀₁₂₃₄₅₆₇₈₉")


# ---------------------------------------------------------------------------
# 文本处理：简单 LaTeX → 可读纯文本
# ---------------------------------------------------------------------------

def latex_to_text(value) -> str:
    """把题干/选项里的常见 LaTeX 与占位符转成可读纯文本（导出用，不做完整排版）。"""
    text = str(value or "")
    text = re.sub(r"\[公式\s*\d+\]", "〔公式〕", text)
    text = re.sub(r"\[图\s*\d+\]", "〔图〕", text)
    text = re.sub(r"\$\$(.+?)\$\$", lambda match: _tex_inline(match.group(1)), text, flags=re.S)
    text = re.sub(r"(?<!\$)\$(?!\$)(.+?)(?<!\$)\$(?!\$)", lambda match: _tex_inline(match.group(1)), text, flags=re.S)
    text = re.sub(r"\\\[(.+?)\\\]", lambda match: _tex_inline(match.group(1)), text, flags=re.S)
    text = re.sub(r"\\\((.+?)\\\)", lambda match: _tex_inline(match.group(1)), text, flags=re.S)
    text = text.replace("\r\n", "\n").strip()
    return text


def _tex_inline(expr: str) -> str:
    value = str(expr or "")
    for _ in range(6):
        updated = re.sub(r"\\frac\{([^{}]*)\}\{([^{}]*)\}", r"(\1)/(\2)", value)
        updated = re.sub(r"\\dfrac\{([^{}]*)\}\{([^{}]*)\}", r"(\1)/(\2)", updated)
        updated = re.sub(r"\\sqrt\[(\d+)\]\{([^{}]*)\}", r"\1√(\2)", updated)
        updated = re.sub(r"\\sqrt\{([^{}]*)\}", r"√(\1)", updated)
        if updated == value:
            break
        value = updated
    value = re.sub(r"\^\{([^{}]*)\}", lambda match: _with_default(match.group(1), _SUPERSCRIPTS), value)
    value = re.sub(r"\^(\d)", lambda match: match.group(1).translate(_SUPERSCRIPTS), value)
    value = re.sub(r"\_\{([^{}]*)\}", lambda match: _with_default(match.group(1), _SUBSCRIPTS), value)
    for command, replacement in _TEX_COMMANDS.items():
        value = value.replace(command, replacement)
    value = re.sub(r"\\text\{([^{}]*)\}", r"\1", value)
    value = re.sub(r"\\mathrm\{([^{}]*)\}", r"\1", value)
    value = re.sub(r"\\[a-zA-Z]+", "", value)
    value = value.replace("\\\\", " ")
    value = value.replace("{", "").replace("}", "").replace("\\", "")
    return re.sub(r"\s+", " ", value).strip()


def _with_default(content: str, table) -> str:
    converted = content.translate(table)
    return converted if converted != content else content


# ---------------------------------------------------------------------------
# 分组与编号
# ---------------------------------------------------------------------------

def group_sections(items: list[dict]) -> list[dict]:
    """按 ``section`` 聚题（空 section 用题型名）；保持首次出现顺序。"""
    sections: list[dict] = []
    index: dict[str, dict] = {}
    for item in items:
        snapshot = item.get("snapshot") if isinstance(item.get("snapshot"), dict) else {}
        key = str(item.get("section") or "").strip() or _TYPE_LABELS.get(
            str(snapshot.get("type") or "SUBJECTIVE"), "题目")
        bucket = index.get(key)
        if bucket is None:
            bucket = {"title": key, "items": []}
            index[key] = bucket
            sections.append(bucket)
        bucket["items"].append(item)
    return sections


def _item_text(snapshot: dict, key: str) -> str:
    content = snapshot.get("content") if isinstance(snapshot.get("content"), dict) else {}
    return latex_to_text(content.get(key) or "")


def _item_options(snapshot: dict) -> list[dict]:
    content = snapshot.get("content") if isinstance(snapshot.get("content"), dict) else {}
    options = content.get("options") if isinstance(content.get("options"), list) else []
    cleaned = []
    for option in options:
        if not isinstance(option, dict):
            continue
        cleaned.append({
            "label": latex_to_text(option.get("label") or ""),
            "text": latex_to_text(option.get("text") or ""),
        })
    return cleaned


def _item_answers(snapshot: dict) -> tuple[list[str], str, str]:
    content = snapshot.get("content") if isinstance(snapshot.get("content"), dict) else {}
    answers = [latex_to_text(item) for item in (content.get("answers") or []) if str(item).strip()]
    if not answers and str(content.get("answer_text") or "").strip():
        answers = [latex_to_text(content.get("answer_text"))]
    explanation = latex_to_text(content.get("explanation") or "")
    rubric = latex_to_text(content.get("rubric") or "")
    parts = content.get("parts")
    if isinstance(parts, list):
        for index, part in enumerate(parts, start=1):
            if not isinstance(part, dict):
                continue
            part_answers = [latex_to_text(item) for item in (part.get("answers") or []) if str(item).strip()]
            if part_answers:
                answers.append(f"第 {index} 问：{'；'.join(part_answers)}")
    return answers, explanation, rubric


def _material_text(snapshot: dict) -> str:
    content = snapshot.get("content") if isinstance(snapshot.get("content"), dict) else {}
    material = content.get("material") if isinstance(content.get("material"), dict) else None
    if not material:
        return ""
    return latex_to_text(material.get("text") or "")


def _score_text(item: dict) -> str:
    score = item.get("score")
    if score is None:
        return ""
    try:
        numeric = float(score)
    except (TypeError, ValueError):
        return ""
    return f"{numeric:g} 分"


def _paper_meta(paper: dict, items: list[dict]) -> str:
    total = round(sum(float(item.get("score") or 0) for item in items), 2)
    parts = [paper.get("subject") or "", paper.get("grade") or ""]
    parts.append(f"共 {len(items)} 题")
    if total:
        parts.append(f"满分 {total:g} 分")
    return " · ".join(part for part in parts if part)


# ---------------------------------------------------------------------------
# HTML 导出（自包含打印页）
# ---------------------------------------------------------------------------

def _image_data_url(image: dict, *, user_id: int | None = None) -> str | None:
    """把快照里的配图引用读成 data URL（只读题库 / 试卷工坊素材，单张 ≤2MB）。

    P0：试卷工坊配图（``kind=app``）的 ``asset`` 是服务端签发的 asset_id（或迁移前
    的旧文件名），读取前按资产登记校验归属；``user_id`` 为导出/查看者，缺失时只做
    上层已完成归属校验的兼容调用（旧试卷快照配图路由）。
    """
    if not isinstance(image, dict):
        return None
    try:
        if image.get("kind") == "bank" and image.get("file"):
            from .. import practice as practice_module
            bank_root = practice_module._bank_dir().resolve()
            candidate = (bank_root / str(image.get("subject") or "") / str(image["file"])).resolve()
            if not candidate.is_relative_to(bank_root) or not candidate.is_file():
                return None
            if candidate.stat().st_size > _MAX_EMBED_IMAGE_BYTES:
                return None
            data = candidate.read_bytes()
            suffix = candidate.suffix.lower().lstrip(".")
            mime = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
                    "webp": "image/webp"}.get(suffix, "image/png")
            return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"
        if image.get("kind") == "app" and image.get("asset"):
            asset = assets.resolve_asset_for_read(image.get("asset"), user_id)
            if not asset:
                return None
            data = storage.read_file(
                str(asset.get("folder") or ASSET_FOLDER),
                str(asset.get("storage_ref") or ""),
            )
            if not data or len(data) > _MAX_EMBED_IMAGE_BYTES:
                return None
            mime = str(asset.get("media_type") or "").strip() or "image/png"
            return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"
    except Exception:  # noqa: BLE001 - 配图缺失不应影响整卷导出
        return None
    return None


def _html_images(snapshot: dict, *, user_id: int | None = None) -> str:
    content = snapshot.get("content") if isinstance(snapshot.get("content"), dict) else {}
    images = content.get("images") if isinstance(content.get("images"), list) else []
    parts = []
    for image in images:
        url = _image_data_url(image, user_id=user_id)
        if not url:
            continue
        role = "公式" if str(image.get("role")) == "formula" else "配图"
        parts.append(f'<img class="q-img" src="{url}" alt="{html_lib.escape(role)}">')
    material = content.get("material") if isinstance(content.get("material"), dict) else None
    if material and isinstance(material.get("images"), list):
        for image in material["images"]:
            url = _image_data_url(image, user_id=user_id)
            if url:
                parts.append(f'<img class="q-img" src="{url}" alt="材料配图">')
    return "".join(parts)


def _html_question(item: dict, number: int, *, user_id: int | None = None) -> str:
    snapshot = item.get("snapshot") if isinstance(item.get("snapshot"), dict) else {}
    stem = html_lib.escape(_item_text(snapshot, "stem")).replace("\n", "<br>")
    material = html_lib.escape(_material_text(snapshot)).replace("\n", "<br>")
    options = _item_options(snapshot)
    score = html_lib.escape(_score_text(item))
    blocks = [
        '<section class="question">',
        '<div class="q-head"><span class="q-no">%d.</span><span class="q-score">%s</span></div>'
        % (number, score),
    ]
    if material:
        blocks.append(f'<div class="material">{material}</div>')
    content = snapshot.get("content") if isinstance(snapshot.get("content"), dict) else snapshot
    stem_blocks = content_blocks(content)
    stem_html = blocks_to_html(stem_blocks) if stem_blocks else ""
    blocks.append(f'<div class="stem">{stem_html or stem}</div>')
    images = _html_images(snapshot, user_id=user_id)
    if images:
        blocks.append(f'<div class="q-images">{images}</div>')
    if options:
        columns = 2 if len(options) <= 2 else 4
        blocks.append(f'<div class="options cols-{columns}">')
        for option in options:
            blocks.append(
                '<div class="option"><span class="opt-label">%s.</span><span>%s</span></div>'
                % (html_lib.escape(option["label"]), html_lib.escape(option["text"]))
            )
        blocks.append("</div>")
    else:
        blocks.append('<div class="answer-space"></div>')
    blocks.append("</section>")
    return "".join(blocks)


def _html_answers(items: list[dict]) -> str:
    blocks = ['<section class="answers"><h2>参考答案与解析</h2>']
    for number, item in enumerate(items, start=1):
        snapshot = item.get("snapshot") if isinstance(item.get("snapshot"), dict) else {}
        answers, explanation, rubric = _item_answers(snapshot)
        blocks.append(f'<div class="answer-item"><strong>{number}. 答案：</strong>'
                      f'{html_lib.escape("；".join(answers) or "未提供")}')
        if explanation:
            blocks.append(f'<div class="answer-detail">解析：{html_lib.escape(explanation)}</div>')
        if rubric:
            blocks.append(f'<div class="answer-detail">评分标准：{html_lib.escape(rubric)}</div>')
        blocks.append("</div>")
    blocks.append("</section>")
    return "".join(blocks)


def _text_weight(text: str) -> int:
    """粗略字符宽度：CJK 记 2，ASCII 记 1（用于估算分页高度）。"""
    weight = 0
    for char in str(text or ""):
        weight += 2 if ord(char) > 0x2E7F else 1
    return weight


def _estimate_item_height(item: dict, *, chars_per_line: int, answer_space_mm: float) -> float:
    snapshot = item.get("snapshot") if isinstance(item.get("snapshot"), dict) else {}
    content = snapshot.get("content") if isinstance(snapshot.get("content"), dict) else {}
    stem = latex_to_text(content.get("stem") or "")
    material = latex_to_text((content.get("material") or {}).get("text") or "") \
        if isinstance(content.get("material"), dict) else ""
    text_lines = int((_text_weight(material) + chars_per_line - 1) // chars_per_line)
    text_lines += int((_text_weight(stem) + chars_per_line - 1) // chars_per_line)
    height = 10.0 + max(1, text_lines) * 5.6
    options = _item_options(snapshot)
    if options:
        per_row = 2 if len(options) <= 2 else 4
        height += 5.6 * int((len(options) + per_row - 1) // per_row)
    images = content.get("images") if isinstance(content.get("images"), list) else []
    height += 22.0 * min(3, len(images))
    qtype = str(snapshot.get("type") or "")
    if qtype in ("SUBJECTIVE", "MIXED", "BLANK"):
        height += max(8.0, answer_space_mm or 24.0)
    else:
        height += 5.0
    return max(14.0, min(height, 160.0))


def _paginate_items(items: list[dict], layout: dict, columns: int) -> list[list[dict]]:
    """按题目高度估算 + 分栏容量做贪心分页（不计答案页）。"""
    paper_size = "A3" if str(layout.get("paper")).upper() == "A3" else "A4"
    page_height = 297.0
    page_width = 420.0 if paper_size == "A3" else 210.0
    margin = float(layout.get("margin_mm") or 18)
    content_width = page_width - 2 * margin
    column_width = (content_width - 8.0 * (columns - 1)) / columns
    chars_per_line = max(16, int(column_width / 2.35))
    content_height = page_height - margin * 2 - 16.0 - 12.0  # 页眉 + 页脚
    page_capacity = content_height * columns * 0.94
    answer_space = float(layout.get("answer_space_mm") or 0)
    pages: list[list[dict]] = []
    current: list[dict] = []
    used = 0.0
    for item in items:
        height = _estimate_item_height(item, chars_per_line=chars_per_line,
                                       answer_space_mm=answer_space)
        if current and used + height > page_capacity:
            pages.append(current)
            current = []
            used = 0.0
        current.append(item)
        used += height
    if current:
        pages.append(current)
    return pages or [[]]


def build_html(paper: dict, items: list[dict], *, include_answers: bool = True,
               columns: int | None = None, user_id: int | None = None) -> str:
    layout = paper.get("layout") if isinstance(paper.get("layout"), dict) else {}
    layout = {**papers.LAYOUT_DEFAULTS, **layout}
    answer_space = max(0.0, float(layout.get("answer_space_mm") or 0))
    font_size = float(layout.get("font_size") or 10.5)
    line_height = float(layout.get("line_height") or 1.6)
    margin = float(layout.get("margin_mm") or 18)
    paper_size = "A3" if str(layout.get("paper")).upper() == "A3" else "A4"
    if columns is None:
        columns = int(layout.get("columns") or 1)
    columns = max(1, min(4, int(columns)))
    title = html_lib.escape(paper.get("title") or "试卷")
    meta = html_lib.escape(_paper_meta(paper, items))
    instructions = html_lib.escape(paper.get("instructions") or "").replace("\n", "<br>")
    page_width_mm = 420.0 if paper_size == "A3" else 210.0
    page_height_mm = 297.0
    pages = _paginate_items(items, layout, columns)
    total_pages = len(pages) + (1 if include_answers else 0)
    number = 0
    sheets: list[str] = []
    for page_no, page_items in enumerate(pages, start=1):
        bodies: list[str] = []
        for item in page_items:
            number += 1
            bodies.append(_html_question(item, number, user_id=user_id))
        header_html = (
            '<header class="sheet-head"><span class="sheet-title">%s</span>'
            '<span class="sheet-meta">%s</span></header>' % (title, meta)
        )
        if page_no == 1 and instructions:
            header_html += f'<div class="instructions">{instructions}</div>'
        footer_html = (
            '<footer class="sheet-foot"><span>知序 · 试卷工坊</span>'
            f'<span>第 {page_no} / {total_pages} 页</span></footer>'
        )
        sheets.append(
            f'<section class="sheet" data-page="{page_no}">{header_html}'
            f'<div class="sheet-columns" style="column-count:{columns};">{"".join(bodies)}</div>'
            f'{footer_html}</section>'
        )
    answers_html = ""
    if include_answers:
        answers_html = (f'<section class="sheet answers-sheet" data-page="{total_pages}">'
                        f'<header class="sheet-head"><span class="sheet-title">参考答案与解析</span>'
                        f'<span class="sheet-meta">{title}</span></header>'
                        f'{_html_answers(items)}'
                        f'<footer class="sheet-foot"><span>知序 · 试卷工坊</span>'
                        f'<span>第 {total_pages} / {total_pages} 页</span></footer></section>')
    css = f"""
    @page {{ size: {paper_size}{' landscape' if paper_size == 'A3' else ' portrait'}; margin: 0; }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0 auto; background: #f3f3f3;
            font-family: "Songti SC", "SimSun", "Microsoft YaHei", serif;
            font-size: {font_size:g}pt; line-height: {line_height:g}; color: #111; }}
    .sheet {{ width: {page_width_mm:g}mm; min-height: {page_height_mm:g}mm; margin: 0 auto 8mm;
              padding: {margin:g}mm; background: #fff; position: relative;
              page-break-after: always; break-after: page; }}
    .sheet:last-child {{ page-break-after: auto; break-after: auto; }}
    .sheet-head {{ display: flex; justify-content: space-between; align-items: baseline;
                   border-bottom: 1px solid #333; padding-bottom: 4px; margin-bottom: 8px; }}
    .sheet-title {{ font-size: 1.1em; font-weight: 700; }}
    .sheet-meta {{ font-size: .78em; color: #444; }}
    .sheet-columns {{ column-count: {columns}; column-gap: 8mm; }}
    .sheet-foot {{ display: flex; justify-content: space-between; color: #777; font-size: .72em;
                   border-top: 1px solid #ccc; padding-top: 4px; margin-top: 8px; }}
    .instructions {{ border: 1px solid #999; padding: 6px 8px; font-size: .82em; margin-bottom: 8px; }}
    .section-title {{ font-size: 1.02em; margin: 12px 0 5px; border-bottom: 1px solid #333; padding-bottom: 2px; }}
    .question {{ break-inside: avoid; page-break-inside: avoid; margin-bottom: 9px; }}
    .q-head {{ display: flex; justify-content: space-between; font-weight: 600; }}
    .q-score {{ font-weight: 400; color: #555; font-size: .82em; }}
    .material {{ background: #f7f7f7; border-left: 3px solid #999; padding: 5px 7px; margin: 3px 0; font-size: .92em; }}
    .stem {{ margin: 2px 0; overflow-wrap: anywhere; }}
    .q-images {{ margin: 3px 0; }}
    .q-img {{ max-width: 82%; max-height: 90mm; display: block; margin: 3px auto; }}
    .options {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 2px 8px; margin: 3px 0; }}
    .options.cols-4 {{ grid-template-columns: repeat(4, minmax(0, 1fr)); }}
    .option {{ overflow-wrap: anywhere; }}
    .opt-label {{ font-weight: 600; margin-right: 3px; }}
    .answer-space {{ height: {answer_space:g}mm; min-height: 8mm; border-bottom: 1px dashed #ccc; margin-top: 5px; }}
    .answers-sheet {{ break-before: page; }}
    .answers-sheet .answers {{ column-count: 1; }}
    .answers h2 {{ font-size: 1.05em; margin: 0 0 8px; }}
    .answer-item {{ margin-bottom: 8px; break-inside: avoid; font-size: .92em; }}
    .answer-detail {{ color: #333; margin-top: 2px; white-space: pre-wrap; }}
    @media print {{
      body {{ background: #fff; }}
      .sheet {{ margin: 0; box-shadow: none; }}
      .no-print {{ display: none; }}
    }}
    """
    return (
        '<!doctype html><html lang="zh-CN"><head><meta charset="UTF-8">'
        f'<meta name="page-count" content="{total_pages}">'
        f"<title>{title}</title><style>{css}</style></head><body>"
        + "".join(sheets) + answers_html
        + "</body></html>"
    )


# ---------------------------------------------------------------------------
# Word 导出（python-docx）
# ---------------------------------------------------------------------------

def _set_default_font(document, font_name: str = "宋体") -> None:
    style = document.styles["Normal"]
    style.font.name = "Times New Roman"
    style.font.size = None
    try:
        from docx.oxml.ns import qn
        style.element.rPr.rFonts.set(qn("w:eastAsia"), font_name)
    except Exception:  # noqa: BLE001 - 字体设置失败不影响正文导出
        pass


def _add_page_number_footer(document, title: str) -> None:
    try:
        from docx.enum.text import WD_ALIGN_PARAGRAPH
        from docx.oxml import OxmlElement
        from docx.oxml.ns import qn
    except Exception:  # noqa: BLE001
        return
    for section in document.sections:
        footer = section.footer
        paragraph = footer.paragraphs[0] if footer.paragraphs else footer.add_paragraph()
        paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
        if title:
            run = paragraph.add_run(title + "　　")
            run.font.size = None
        run = paragraph.add_run("第 ")
        fld_begin = OxmlElement("w:fldChar")
        fld_begin.set(qn("w:fldCharType"), "begin")
        instr = OxmlElement("w:instrText")
        instr.set(qn("xml:space"), "preserve")
        instr.text = "PAGE"
        fld_end = OxmlElement("w:fldChar")
        fld_end.set(qn("w:fldCharType"), "end")
        run._r.append(fld_begin)
        run._r.append(instr)
        run._r.append(fld_end)
        paragraph.add_run(" 页")


def _resolve_page_setup(layout: dict) -> tuple[float, float, str]:
    """返回 (宽 mm, 高 mm, 方向)；A3 默认横向、A4 默认纵向（与 OMP 答题卡/试卷习惯一致）。"""
    paper = "A3" if str(layout.get("paper")).upper() == "A3" else "A4"
    direction = str(layout.get("orientation") or "").strip().lower()
    if direction not in ("portrait", "landscape"):
        direction = "landscape" if paper == "A3" else "portrait"
    if paper == "A3":
        return (420.0, 297.0, "landscape") if direction == "landscape" else (297.0, 420.0, "portrait")
    return (297.0, 210.0, "landscape") if direction == "landscape" else (210.0, 297.0, "portrait")


def _set_section_columns(section, count: int) -> None:
    """python-docx 无 columns API：直接写 ``sectPr/w:cols``（Word 分栏）。"""
    count = max(1, min(4, int(count)))
    try:
        from docx.oxml import OxmlElement
        from docx.oxml.ns import qn
    except Exception:  # noqa: BLE001
        return
    sect_pr = section._sectPr
    columns = sect_pr.find(qn("w:cols"))
    if columns is None:
        columns = OxmlElement("w:cols")
        sect_pr.append(columns)
    columns.set(qn("w:num"), str(count))
    columns.set(qn("w:space"), "240")
    columns.set(qn("w:equalWidth"), "1")


def _apply_docx_page_setup(document, layout: dict, columns: int) -> None:
    from docx.enum.section import WD_ORIENT
    from docx.shared import Mm

    width, height, direction = _resolve_page_setup(layout)
    margin = float(layout.get("margin_mm") or 18)
    for section in document.sections:
        section.page_width = Mm(width)
        section.page_height = Mm(height)
        section.orientation = WD_ORIENT.LANDSCAPE if direction == "landscape" else WD_ORIENT.PORTRAIT
        section.left_margin = Mm(margin)
        section.right_margin = Mm(margin)
        section.top_margin = Mm(max(10.0, margin - 4))
        section.bottom_margin = Mm(max(10.0, margin - 4))
        _set_section_columns(section, columns)


def _option_table(document, options: list[dict]) -> None:
    from docx.shared import Pt
    columns = 2 if len(options) <= 2 else 4
    rows = (len(options) + columns - 1) // columns
    table = document.add_table(rows=rows, cols=columns)
    table.autofit = True
    for index, option in enumerate(options):
        row, column = divmod(index, columns)
        cell = table.cell(row, column)
        cell.text = f"{option['label']}. {option['text']}"
        for paragraph in cell.paragraphs:
            for run in paragraph.runs:
                run.font.size = Pt(10)


def _answer_lines(document, item: dict, snapshot: dict) -> None:
    from docx.shared import Mm
    layout = item.get("_layout") or {}
    space_mm = float(layout.get("answer_space_mm") or 0)
    if space_mm <= 0:
        space_mm = 20.0 if str(snapshot.get("type")) in ("SUBJECTIVE", "MIXED") else 0
    if space_mm <= 0:
        return
    lines = max(1, int(round(space_mm / 9)))
    for _ in range(lines):
        paragraph = document.add_paragraph("＿" * 34)
        paragraph.paragraph_format.space_after = Mm(1)


def _add_docx_blocks(document, blocks: list[dict]) -> None:
    """把内容块写入 Word：文本/公式按段落，表格还原为可编辑表格。"""
    from docx.shared import Pt

    for block in blocks or []:
        kind = block.get("kind")
        if kind == "table":
            rows = table_rows(block.get("table_html") or "")
            columns = max((len(row) for row in rows), default=0)
            if rows and columns and len(rows) <= 30 and columns <= 12:
                table = document.add_table(rows=len(rows), cols=columns)
                table.autofit = True
                for row_index, row in enumerate(rows):
                    for column_index in range(columns):
                        cell = table.cell(row_index, column_index)
                        cell.text = row[column_index] if column_index < len(row) else ""
                        for paragraph in cell.paragraphs:
                            for run in paragraph.runs:
                                run.font.size = Pt(9)
                continue
            text = blocks_plain_text([block], latex_delimiters=False)
            if text:
                document.add_paragraph(text)
        elif kind == "math":
            latex = str(block.get("latex") or "").strip()
            if latex:
                document.add_paragraph(latex_to_text(latex))
        elif str(block.get("text") or "").strip():
            document.add_paragraph(str(block["text"]))


def build_docx(paper: dict, items: list[dict], *, include_answers: bool = True,
               columns: int | None = None) -> bytes:
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Pt

    document = Document()
    _set_default_font(document)
    layout = {**papers.LAYOUT_DEFAULTS, **(paper.get("layout") or {})}
    if columns is None:
        columns = int(layout.get("columns") or 1)
    columns = max(1, min(4, int(columns)))
    _apply_docx_page_setup(document, layout, columns)
    title = paper.get("title") or "试卷"
    heading = document.add_paragraph()
    heading.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = heading.add_run(title)
    run.bold = True
    run.font.size = Pt(16)
    meta = document.add_paragraph(_paper_meta(paper, items))
    meta.alignment = WD_ALIGN_PARAGRAPH.CENTER
    for paragraph_run in meta.runs:
        paragraph_run.font.size = Pt(9)
    instructions = paper.get("instructions") or ""
    if instructions:
        document.add_paragraph(latex_to_text(instructions))
    sections = group_sections(items)
    number = 0
    for index, section in enumerate(sections):
        cn = _CN_NUMBERS[index] if index < len(_CN_NUMBERS) else str(index + 1)
        section_heading = document.add_paragraph()
        run = section_heading.add_run(f"{cn}、{section['title']}")
        run.bold = True
        run.font.size = Pt(12)
        if index > 0 and layout.get("page_break_before"):
            section_heading.paragraph_format.page_break_before = True
        for item in section["items"]:
            number += 1
            snapshot = item.get("snapshot") if isinstance(item.get("snapshot"), dict) else {}
            material = _material_text(snapshot)
            if material:
                document.add_paragraph("材料：" + material)
            stem_blocks = content_blocks(snapshot.get("content") or snapshot)
            if stem_blocks:
                number_paragraph = document.add_paragraph()
                number_paragraph.add_run(f"{number}. ").bold = True
                score = _score_text(item)
                if score:
                    score_run = number_paragraph.add_run(f"（{score}）")
                    score_run.font.size = Pt(9)
                _add_docx_blocks(document, stem_blocks)
            else:
                stem_paragraph = document.add_paragraph()
                stem_paragraph.add_run(f"{number}. ").bold = True
                stem_paragraph.add_run(_item_text(snapshot, "stem"))
                score = _score_text(item)
                if score:
                    score_run = stem_paragraph.add_run(f"（{score}）")
                    score_run.font.size = Pt(9)
            options = _item_options(snapshot)
            if options:
                _option_table(document, options)
            else:
                _answer_lines(document, {**item, "_layout": layout}, snapshot)
    if include_answers:
        from docx.enum.section import WD_SECTION

        answer_section = document.add_section(WD_SECTION.NEW_PAGE)
        _set_section_columns(answer_section, 1)
        heading = document.add_paragraph()
        heading.alignment = WD_ALIGN_PARAGRAPH.CENTER
        run = heading.add_run("参考答案与解析")
        run.bold = True
        run.font.size = Pt(14)
        for answer_number, item in enumerate(items, start=1):
            snapshot = item.get("snapshot") if isinstance(item.get("snapshot"), dict) else {}
            answers, explanation, rubric = _item_answers(snapshot)
            paragraph = document.add_paragraph()
            paragraph.add_run(f"{answer_number}. 答案：").bold = True
            paragraph.add_run("；".join(answers) or "未提供")
            if explanation:
                document.add_paragraph("解析：" + explanation)
            if rubric:
                document.add_paragraph("评分标准：" + rubric)
    _add_page_number_footer(document, title)
    output = io.BytesIO()
    document.save(output)
    return output.getvalue()


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def _export_title(paper: dict, items: list[dict], include_answers: bool) -> str:
    suffix = "（含答案）" if include_answers else "（学生版）"
    return f"{paper.get('title') or '试卷'}{suffix}"


@router.get("/papers/{paper_id}/export")
async def export_paper(
    paper_id: int,
    format: str = Query(default="docx", max_length=10),
    answers: bool = Query(default=True, description="是否包含参考答案与解析"),
    columns: int | None = Query(default=None, ge=1, le=4, description="分栏数 1–4（缺省用试卷版面设置）"),
    user=Depends(current_user),
):
    """导出试卷：``format=docx|html|pdf``；``answers=0`` 学生版；``columns=1..4`` 分栏排版。

    导出内容来自建卷时的快照，与题库后续修改无关；只允许本人导出（他人 404）。
    A4/A3 与分栏由 ``columns``（或建卷时 layout）决定；超过一页自动分页，页数见
    ``X-Page-Count`` 响应头与 HTML ``<meta name="page-count">``。
    ``format=pdf`` 用服务端浏览器渲染（与浏览器打印结果一致）；渲染引擎不可用时 503 并提示
    改用 ``format=html`` 在浏览器打印。
    """
    fmt = str(format or "").strip().lower()
    if fmt not in ("docx", "html", "pdf"):
        raise HTTPException(422, "导出格式只支持 docx、html 或 pdf")
    paper_row = papers.fetch_paper(paper_id, user["id"])
    raw_items = papers.list_items(paper_id, user["id"])
    items = [
        {
            "id": item["id"], "section": item.get("section") or "",
            "score": item.get("score"),
            "snapshot": json_load(item.get("snapshot_json"), {}) or {},
        }
        for item in raw_items
    ]
    paper = papers.public_paper(paper_row, item_count=len(items),
                                total_score=round(sum(float(i["score"] or 0) for i in items), 2))
    layout = {**papers.LAYOUT_DEFAULTS, **(paper.get("layout") or {})}
    column_count = columns if columns is not None else int(layout.get("columns") or 1)
    column_count = max(1, min(4, int(column_count)))
    estimated_pages = len(_paginate_items(items, layout, column_count)) + (1 if answers else 0)
    if fmt == "docx":
        content = build_docx(paper, items, include_answers=answers, columns=column_count)
        filename = quote(_export_title(paper, items, answers) + ".docx")
        return Response(
            content=content,
            media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            headers={
                "Content-Disposition": f"attachment; filename=paper.docx; filename*=UTF-8''{filename}",
                "Cache-Control": "no-store",
                "X-Page-Count": str(estimated_pages),
                "X-Paper-Columns": str(column_count),
            },
        )
    page = build_html(paper, items, include_answers=answers, columns=column_count,
                      user_id=user["id"])
    if fmt == "pdf":
        from ..ai import render as ai_render

        # 版面纸张与方向跟随建卷 layout（A4/A3），分栏已在 HTML 内处理。
        paper_size = str(layout.get("paper") or "A4").upper()
        if paper_size not in ("A4", "A3"):
            paper_size = "A4"
        try:
            pdf = await ai_render.html_to_pdf(page, paper=paper_size, landscape=False, margin_mm=8)
        except ai_render.RenderUnavailable as exc:
            raise HTTPException(
                503,
                "服务端 PDF 暂不可用（%s）；可改用 format=html 在浏览器打印并另存为 PDF" % exc,
            ) from exc
        filename = quote(_export_title(paper, items, answers) + ".pdf")
        return Response(
            content=pdf,
            media_type="application/pdf",
            headers={
                "Content-Disposition": f"attachment; filename=paper.pdf; filename*=UTF-8''{filename}",
                "Cache-Control": "no-store",
                "X-Page-Count": str(estimated_pages),
                "X-Paper-Columns": str(column_count),
                "X-Paper-Size": paper_size,
            },
        )
    return HTMLResponse(
        content=page,
        headers={
            # 允许同源 iframe 内嵌打印；页面只用内联样式与 data URL 图片，无脚本。
            "X-Frame-Options": "SAMEORIGIN",
            "Content-Security-Policy": (
                "default-src 'none'; img-src 'self' data:; style-src 'unsafe-inline'; "
                "frame-ancestors 'self'; base-uri 'none'"
            ),
            "Cache-Control": "no-store",
            "X-Page-Count": str(estimated_pages),
            "X-Paper-Columns": str(column_count),
        },
    )


__all__ = ["router", "build_docx", "build_html", "latex_to_text", "group_sections"]
