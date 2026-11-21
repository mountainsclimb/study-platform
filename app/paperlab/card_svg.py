"""答题卡几何与 SVG 纯函数（不依赖第三方 SVG 库，页面码/定位点照 OMP recon §2.3/§2.11/§6）。

- 版面 JSON（``layout``）用**毫米**描述：纸张/页边距/四角定位点/页眉（标题、姓名班级、学号填涂区）/
  客观题分组与气泡/主观题书写区/页脚（页码 + 46 位页面码）/可选二维码。
- SVG 生成只做 ``mm → px``（96 DPI，1mm = 96/25.4 px）映射与字符串拼接；
  四角定位点与页面码固定黑色（便于扫描识别），其它线条/文字支持 ``black`` / ``red`` 两种印刷色。
- 页面码：``"101" + FNV32(模板签名) 32bit + 页码 8bit + "110"`` 共 46 位（与 OMP ``cardPageCode`` 对齐）。
"""
from __future__ import annotations

import json
import math

MM_TO_PX = 96.0 / 25.4
PAPER_SIZES = {
    "A4": {"portrait": (210.0, 297.0), "landscape": (297.0, 210.0)},
    "A3": {"portrait": (297.0, 420.0), "landscape": (420.0, 297.0)},
}
DEFAULT_PRESET = "A4"
DEFAULT_COLUMNS = 2
DEFAULT_BUBBLE = "circle"
DEFAULT_MARGIN_MM = 12.0
DEFAULT_STUDENT_DIGITS = 6
ANCHOR_W_MM = 12.0
ANCHOR_H_MM = 5.0
ANCHOR_CLEARANCE_MM = 3.0   # 页眉内容与四角定位点的最小安全间距（防止标题/学号区被定位点覆盖）
BUBBLE_R_MM = 1.45
BUBBLE_GAP_MM = 1.6
BUBBLE_MAX_OPTIONS = 6
OBJECTIVE_ROW_H_MM = 8.5
GROUP_TITLE_H_MM = 7.0
COLUMN_GAP_MM = 6.0
MIN_PAGE_COUNT = 1
CARD_STATUSES = ("ready", "stale", "draft")
PRINT_COLORS = {"black": "#111111", "red": "#c41e1e"}
FONT_STACK = '"SimSun","Songti SC","Microsoft YaHei",sans-serif'
MONO_STACK = '"Consolas","Courier New",monospace'


# ---------------------------------------------------------------------------
# FNV32 / 页面码 / 模板签名
# ---------------------------------------------------------------------------

def fnv32(text: str) -> int:
    """FNV-1a 32bit（与 OMP cardPageCode 的模板签名同算法口径）。"""
    value = 2166136261
    for byte in str(text or "").encode("utf-8"):
        value ^= byte
        value = (value * 16777619) & 0xFFFFFFFF
    return value


def template_signature(layout: dict) -> int:
    """模板签名：对版面静态部分做规范化 JSON 后取 FNV32（不含页面码等动态字段）。"""
    meta = layout.get("template") if isinstance(layout.get("template"), dict) else {}
    payload = {
        "paper_id": meta.get("paper_id"),
        "paper_fingerprint": meta.get("paper_fingerprint"),
        "preset": layout.get("preset"),
        "orientation": layout.get("orientation"),
        "columns": layout.get("columns"),
        "bubble": layout.get("bubble"),
        "include_subjective": layout.get("include_subjective"),
        "student_no": layout.get("student_no"),
    }
    return fnv32(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def page_code(signature: int, page_no: int) -> str:
    """46 位页面码：``101`` + 签名 32bit + 页码 8bit + ``110``。"""
    number = max(1, int(page_no)) & 0xFF
    return "101" + format(int(signature) & 0xFFFFFFFF, "032b") + format(number, "08b") + "110"


# ---------------------------------------------------------------------------
# 版面构建
# ---------------------------------------------------------------------------

def _round(value: float) -> float:
    return round(float(value) + 1e-9, 3)


def paper_dimensions(preset: str, orientation: str | None = None) -> tuple[float, float]:
    key = str(preset or DEFAULT_PRESET).upper()
    size = PAPER_SIZES.get(key)
    if size is None:
        raise ValueError("答题卡纸张只支持 A4 或 A3")
    direction = str(orientation or ("landscape" if key == "A3" else "portrait")).lower()
    if direction not in ("portrait", "landscape"):
        raise ValueError("纸张方向只支持 portrait 或 landscape")
    return _round(size[direction][0]), _round(size[direction][1])


def _paper_fingerprint(paper: dict, items: list[dict]) -> str:
    payload = {
        "paper_id": paper.get("id"),
        "kind": paper.get("kind"),
        "subject": paper.get("subject"),
        "items": [
            {
                "position": item.get("position"),
                "ref": item.get("question_ref"),
                "score": round(float(item.get("score") or 0), 2),
                "type": (item.get("snapshot") or {}).get("type") or "SUBJECTIVE",
                "options": len(((item.get("snapshot") or {}).get("content") or {}).get("options") or []),
            }
            for item in items
        ],
    }
    import hashlib

    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _bubble_options(snapshot: dict) -> list[dict]:
    content = snapshot.get("content") if isinstance(snapshot.get("content"), dict) else {}
    options = content.get("options") if isinstance(content.get("options"), list) else []
    cleaned = []
    for index, option in enumerate(options):
        if not isinstance(option, dict):
            continue
        cleaned.append({
            "label": str(option.get("label") or chr(ord("A") + index)).strip()[:2],
            "text": str(option.get("text") or "").strip()[:40],
        })
    return cleaned


def _subjective_lines(item: dict) -> int:
    score = item.get("score")
    try:
        score = float(score or 0)
    except (TypeError, ValueError):
        score = 0
    return int(max(3, min(14, round(score * 0.8) or 3)))


def _question_block(item: dict, *, question_no: int) -> dict:
    snapshot = item.get("snapshot") if isinstance(item.get("snapshot"), dict) else {}
    qtype = str(snapshot.get("type") or "SUBJECTIVE")
    options = _bubble_options(snapshot)
    if qtype in ("SINGLE", "MULTIPLE") and options:
        per_row = min(BUBBLE_MAX_OPTIONS, len(options))
        rows = int(math.ceil(len(options) / per_row))
        return {
            "kind": "question", "no": question_no, "type": qtype,
            "qtype": "objective", "group_title": _group_title(item),
            "rows": rows, "options_count": len(options),
            "bubbles": [
                {"label": option["label"], "row": index // per_row,
                 "column": index % per_row, "r": BUBBLE_R_MM}
                for index, option in enumerate(options)
            ],
            "write_area": None,
            "h": _round(OBJECTIVE_ROW_H_MM * rows + 1.0),
        }
    lines = _subjective_lines(item)
    return {
        "kind": "question", "no": question_no, "type": qtype,
        "qtype": "subjective", "group_title": _group_title(item),
        "rows": lines, "options_count": 0, "bubbles": [],
        "write_area": {"lines": lines, "style": "lines"},
        "h": _round(min(90.0, max(26.0, lines * 8.0 + 6.0))),
    }


def _group_title(item: dict) -> str:
    section = str(item.get("section") or "").strip()
    if section:
        return section
    snapshot = item.get("snapshot") if isinstance(item.get("snapshot"), dict) else {}
    labels = {"SINGLE": "单选题", "MULTIPLE": "多选题", "BLANK": "填空题",
              "SUBJECTIVE": "主观题", "MIXED": "混合题"}
    return labels.get(str(snapshot.get("type") or ""), "题目")


def _pack_columns(blocks: list[dict], *, page_width: float, page_height: float,
                  margin: float, columns: int, header_h: float, footer_h: float) -> list[dict]:
    """把题目块按列/页贪心装箱；返回带绝对坐标的页面结构（分组标题自动随栏重复）。"""
    content_top = margin + header_h
    content_bottom = page_height - margin - footer_h
    content_left = margin
    content_width = page_width - 2 * margin
    column_width = (content_width - COLUMN_GAP_MM * (columns - 1)) / columns
    capacity = max(24.0, content_bottom - content_top)
    pages: list[dict] = []
    state = {"page": None, "column": 0, "cursor": content_top}

    def new_page() -> None:
        page = {"page_no": len(pages) + 1, "blocks": []}
        pages.append(page)
        state["page"] = page
        state["column"] = 0
        state["cursor"] = content_top

    def next_column() -> None:
        state["column"] += 1
        if state["column"] >= columns:
            new_page()
        else:
            state["cursor"] = content_top

    def column_x(column: int) -> float:
        return content_left + column * (column_width + COLUMN_GAP_MM)

    new_page()
    column_groups: dict[tuple, str] = {}
    for block in blocks:
        height = min(float(block.get("h") or 0), capacity)
        if state["cursor"] + height > content_bottom + 0.01 and state["cursor"] > content_top + 0.01:
            next_column()
        page_key = (state["page"]["page_no"], state["column"])
        group_title = str(block.get("group_title") or "")
        if group_title and column_groups.get(page_key) != group_title:
            title_height = GROUP_TITLE_H_MM
            if state["cursor"] + title_height + height > content_bottom + 0.01 \
                    and state["cursor"] > content_top + 0.01:
                next_column()
                page_key = (state["page"]["page_no"], state["column"])
            x = column_x(state["column"])
            state["page"]["blocks"].append({
                "kind": "group_title", "title": group_title, "column": state["column"],
                "x": _round(x), "y": _round(state["cursor"]),
                "w": _round(column_width), "h": title_height,
            })
            state["cursor"] += title_height
            column_groups[page_key] = group_title
        x = column_x(state["column"])
        y = state["cursor"]
        # 分组标题占位后再按剩余高度收窄一次，避免标题挤占导致题目溢出页脚。
        available = content_bottom - y
        if available < height:
            height = max(6.0, available)
        placed: dict = {
            "kind": "question", "no": block["no"], "type": block["type"],
            "qtype": block["qtype"], "group_title": group_title,
            "x": _round(x), "y": _round(y), "w": _round(column_width), "h": _round(height),
            "column": state["column"], "bubbles": [], "write_area": None,
        }
        if block.get("bubbles"):
            per_row = min(BUBBLE_MAX_OPTIONS, max(1, int(block.get("options_count") or 1)))
            step = max(6.0, (column_width - 12.0) / per_row)
            placed["bubbles"] = [
                {"label": bubble["label"], "row": bubble["row"], "column": bubble["column"],
                 "r": bubble.get("r") or BUBBLE_R_MM,
                 "x": _round(x + 8.0 + bubble["column"] * step + 3.0),
                 "y": _round(y + 4.6 + bubble["row"] * OBJECTIVE_ROW_H_MM)}
                for bubble in block["bubbles"]
            ]
        if isinstance(block.get("write_area"), dict):
            placed["write_area"] = {
                "x": _round(x + 3.0), "y": _round(y + 7.0),
                "w": _round(column_width - 6.0), "h": _round(max(8.0, height - 9.0)),
                "lines": int(block["write_area"].get("lines") or 3),
                "style": "lines",
            }
        state["page"]["blocks"].append(placed)
        state["cursor"] += height
    return pages


def _build_page_groups(page: dict, *, page_width: float, margin: float, columns: int) -> list[dict]:
    column_width = (page_width - 2 * margin - COLUMN_GAP_MM * (columns - 1)) / columns
    groups: list[dict] = []
    open_group: dict | None = None
    for block in page["blocks"]:
        if block["kind"] == "group_title":
            if open_group is not None:
                groups.append(open_group)
            open_group = {
                "title": block["title"], "x": _round(block["x"]),
                "y": _round(block["y"] - 0.5), "w": _round(column_width),
                "h": _round(GROUP_TITLE_H_MM), "questions": [],
            }
            continue
        if open_group is None:
            open_group = {
                "title": "", "x": _round(block["x"]), "y": _round(block["y"]),
                "w": _round(column_width), "h": 0.0, "questions": [],
            }
        open_group["questions"].append(block)
        open_group["h"] = _round(block["y"] + block["h"] - open_group["y"] + 1.0)
    if open_group is not None:
        groups.append(open_group)
    for group in groups:
        group["questions"] = [
            {key: question[key] for key in ("no", "type", "kind", "x", "y", "w", "h", "bubbles", "write_area")}
            for question in group["questions"]
        ]
    return groups


def _anchors(page_width: float, page_height: float, margin: float) -> list[dict]:
    left = margin
    right = page_width - margin - ANCHOR_W_MM
    top = margin
    bottom = page_height - margin - ANCHOR_H_MM
    return [
        {"corner": "top_left", "x": _round(left), "y": _round(top),
         "w": ANCHOR_W_MM, "h": ANCHOR_H_MM},
        {"corner": "top_right", "x": _round(right), "y": _round(top),
         "w": ANCHOR_W_MM, "h": ANCHOR_H_MM},
        {"corner": "bottom_left", "x": _round(left), "y": _round(bottom),
         "w": ANCHOR_W_MM, "h": ANCHOR_H_MM},
        {"corner": "bottom_right", "x": _round(right), "y": _round(bottom),
         "w": ANCHOR_W_MM, "h": ANCHOR_H_MM},
    ]


def _text_width(text: str, size: float) -> float:
    """粗略文字宽度（毫米）：中文按字宽=字号，ASCII 按 0.55 倍；用于标题截断与安全区校验。"""
    width = 0.0
    for char in str(text or ""):
        width += size if ord(char) > 0x2E80 else size * 0.55
    return width


def _fit_text(text: str, max_width: float, size: float) -> str:
    """按可用宽度截断标题，超出时以 … 结尾（避免标题压到定位点/学号区）。"""
    text = str(text or "")
    if _text_width(text, size) <= max_width:
        return text
    ellipsis = "…"
    budget = max(0.0, max_width - _text_width(ellipsis, size))
    kept = ""
    for char in text:
        if _text_width(kept + char, size) > budget:
            break
        kept += char
    return kept + ellipsis


def _text_rect(cell: dict) -> dict:
    """把标题/文字单元框成保守矩形（含字号高度），用于与定位点做相交校验。"""
    size = float(cell.get("size") or 4.0)
    text = str(cell.get("text") or "")
    anchor = str(cell.get("anchor") or "start")
    x = float(cell.get("x") or 0)
    width = _text_width(text, size)
    if anchor == "middle":
        x -= width / 2.0
    elif anchor == "end":
        x -= width
    y = float(cell.get("y") or 0)
    return {"x": x, "y": y - size * 0.85, "w": width, "h": size * 1.2}


def _rects_intersect(first: dict, second: dict, tolerance: float = 0.2) -> bool:
    return (
        float(first["x"]) < float(second["x"]) + float(second["w"]) - tolerance
        and float(second["x"]) < float(first["x"]) + float(first["w"]) - tolerance
        and float(first["y"]) < float(second["y"]) + float(second["h"]) - tolerance
        and float(second["y"]) < float(first["y"]) + float(first["h"]) - tolerance
    )


def build_card_layout(
    paper: dict,
    items: list[dict],
    *,
    preset: str = DEFAULT_PRESET,
    columns: int = DEFAULT_COLUMNS,
    bubble: str = DEFAULT_BUBBLE,
    include_subjective: bool = True,
    student_no: dict | None = None,
    orientation: str | None = None,
    title: str | None = None,
) -> dict:
    """按试卷快照生成答题卡几何（毫米坐标）；非法参数抛 ``ValueError``（API 转 422）。"""
    paper_key = str(preset or DEFAULT_PRESET).upper()
    if paper_key not in PAPER_SIZES:
        raise ValueError("答题卡纸张只支持 A4 或 A3")
    try:
        columns = int(columns)
    except (TypeError, ValueError):
        raise ValueError("分栏数必须是整数") from None
    if columns < 1 or columns > 4:
        raise ValueError("答题卡分栏数应在 1–4 之间")
    bubble_shape = str(bubble or DEFAULT_BUBBLE).lower()
    if bubble_shape not in ("circle", "oval"):
        raise ValueError("气泡形状只支持 circle（圆）或 oval（椭圆）")
    student = dict(student_no or {})
    digits = int(student.get("digits") or DEFAULT_STUDENT_DIGITS)
    if digits not in (6, 8):
        raise ValueError("学号位数只支持 6 位或 8 位")
    bubble_student_no = bool(student.get("bubble", True))
    width, height = paper_dimensions(paper_key, orientation)
    margin = DEFAULT_MARGIN_MM
    fingerprint = _paper_fingerprint(paper, items)
    card_title = str(title or f"{paper.get('title') or '试卷'} · 答题卡")
    # 页眉安全区：左侧/右侧避开 12mm 定位点，顶部避开 5mm 定位点；学号区从安全区顶部开始。
    safe_left = margin + ANCHOR_W_MM + 6.0
    safe_right = width - margin - ANCHOR_W_MM - ANCHOR_CLEARANCE_MM
    safe_top = margin + ANCHOR_H_MM + ANCHOR_CLEARANCE_MM
    student_area = _student_no_area(width, margin, digits, bubble_student_no, top=safe_top)
    header_h = max(34.0, (safe_top - margin) + float(student_area["h"]) + 4.0)
    footer_h = 22.0
    blocks: list[dict] = []
    question_no = 0
    for item in items:
        snapshot = item.get("snapshot") if isinstance(item.get("snapshot"), dict) else {}
        qtype = str(snapshot.get("type") or "SUBJECTIVE")
        if qtype not in ("SINGLE", "MULTIPLE") and not include_subjective:
            continue
        question_no += 1
        blocks.append(_question_block(item, question_no=question_no))
    if not blocks:
        raise ValueError("试卷没有可排入答题卡的题目（客观题被过滤后为空）")
    pages_raw = _pack_columns(
        blocks, page_width=width, page_height=height, margin=margin,
        columns=columns, header_h=header_h, footer_h=footer_h,
    )
    if not pages_raw:
        raise ValueError("答题卡版面生成失败：没有可排入的题目")
    signature = fnv32(json.dumps({
        "paper_id": paper.get("id"), "paper_fingerprint": fingerprint, "preset": paper_key,
        "orientation": orientation or ("landscape" if paper_key == "A3" else "portrait"),
        "columns": columns, "bubble": bubble_shape, "include_subjective": include_subjective,
        "student_no": {"digits": digits, "bubble": bubble_student_no},
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    layout: dict = {
        "schema": "zhixu-card-v1",
        "preset": paper_key,
        "orientation": orientation or ("landscape" if paper_key == "A3" else "portrait"),
        "columns": columns,
        "bubble": bubble_shape,
        "include_subjective": include_subjective,
        "student_no": {"digits": digits, "bubble": bubble_student_no},
        "paper": {
            "width_mm": width, "height_mm": height, "margin_mm": margin,
            "header_mm": header_h, "footer_mm": footer_h,
        },
        "template": {
            "paper_id": paper.get("id"), "title": card_title,
            "subject": paper.get("subject") or "", "paper_fingerprint": fingerprint,
            "signature": signature, "page_count": len(pages_raw),
        },
        "pages": [],
    }
    total_pages = len(pages_raw)
    for page in pages_raw:
        page_no = page["page_no"]
        groups = _build_page_groups(page, page_width=width, margin=margin, columns=columns)
        title_available = max(40.0, float(student_area["x"]) - safe_left - 4.0)
        header = {
            "title": {"x": _round(safe_left), "y": _round(safe_top + 3.6), "size": 5.2,
                      "anchor": "start",
                      "text": _fit_text(card_title, title_available, 5.2)},
            "meta": {"x": _round(width / 2), "y": _round(safe_top + 8.0), "size": 3.2,
                     "anchor": "middle",
                     "text": " · ".join(value for value in [paper.get("subject"), paper.get("grade")] if value)},
            "fields": [
                {"label": "姓名", "x": _round(safe_left), "y": _round(safe_top + 12.0), "w": 42, "h": 6},
                {"label": "班级", "x": _round(safe_left + 46.0), "y": _round(safe_top + 12.0), "w": 42, "h": 6},
            ],
            "student_no": student_area,
        }
        footer = {
            "page_text": {"x": _round(width / 2), "y": _round(height - margin - 14),
                          "size": 3.6, "anchor": "middle",
                          "text": f"第 {page_no} / {total_pages} 页"},
            "page_code": {"x": _round(width / 2), "y": _round(height - margin - 7),
                          "size": 3.4, "anchor": "middle",
                          "text": page_code(signature, page_no)},
            "notice": {"x": _round(margin), "y": _round(height - margin - 14),
                       "size": 2.8, "text": "请用 2B 铅笔填涂；保持定位点与页面码完整"},
        }
        qr_text = f"zhixu://card/{paper.get('id')}?page={page_no}&sig={signature}"
        layout["pages"].append({
            "page_no": page_no,
            "width_mm": width, "height_mm": height,
            "anchors": _anchors(width, height, margin),
            "header": header,
            "groups": groups,
            "footer": footer,
            "qr": {"x": _round(width - margin - 40), "y": _round(height - margin - 18),
                   "size_mm": 16, "text": qr_text},
        })
    return layout


def _student_no_area(page_width: float, margin: float, digits: int, bubble: bool,
                     *, top: float | None = None) -> dict:
    bubble_step = 5.0
    width = 8.0 + 10 * bubble_step
    height = digits * 4.9 + 4.0
    x = page_width - margin - ANCHOR_W_MM - ANCHOR_CLEARANCE_MM - width
    y = margin + 4.0 if top is None else top
    area = {
        "x": _round(x), "y": _round(y), "w": _round(width), "h": _round(height),
        "digits": digits, "bubble": bubble, "digit_rows": [],
    }
    for digit_index in range(digits):
        row_y = y + 4.0 + digit_index * 4.9
        row: dict = {"position": digit_index + 1, "y": _round(row_y), "bubbles": []}
        if bubble:
            for value in range(10):
                row["bubbles"].append({
                    "value": value,
                    "x": _round(x + 8.0 + value * bubble_step + 1.4),
                    "y": _round(row_y + 1.2),
                    "r": 1.15,
                })
        else:
            row["line"] = {"x": _round(x + 8.0), "y": _round(row_y + 2.8),
                           "w": _round(10 * bubble_step)}
        area["digit_rows"].append(row)
    return area


# ---------------------------------------------------------------------------
# 版面校验
# ---------------------------------------------------------------------------

def _within(value: float, low: float, high: float, tolerance: float = 0.3) -> bool:
    return low - tolerance <= value <= high + tolerance


def _rect_inside(rect: dict, width: float, height: float, margin: float = 0.0) -> bool:
    x = float(rect.get("x") or 0)
    y = float(rect.get("y") or 0)
    w = float(rect.get("w") or 0)
    h = float(rect.get("h") or 0)
    return _within(x, margin, width - margin) and _within(y, 0, height) and w > 0 and h > 0 \
        and _within(x + w, margin, width - margin) and _within(y + h, 0, height)


def _bubbles_overlap(first: dict, second: dict) -> bool:
    dx = float(first.get("x")) - float(second.get("x"))
    dy = float(first.get("y")) - float(second.get("y"))
    r1 = float(first.get("r") or 1.4)
    r2 = float(second.get("r") or 1.4)
    return (dx * dx + dy * dy) < (r1 + r2 + 0.2) ** 2


def validate_card_layout(layout) -> None:
    """几何校验（非法抛 ``ValueError``，API 转 422）：越界/气泡重叠/定位点/学号区/页数。"""
    if not isinstance(layout, dict):
        raise ValueError("答题卡 layout 必须是 JSON 对象")
    if layout.get("schema") not in (None, "zhixu-card-v1"):
        raise ValueError("答题卡 layout 版本不正确")
    pages = layout.get("pages")
    if not isinstance(pages, list) or not pages:
        raise ValueError("答题卡缺少页面（pages）")
    page_info = layout.get("paper") if isinstance(layout.get("paper"), dict) else {}
    width = float(page_info.get("width_mm") or 0)
    height = float(page_info.get("height_mm") or 0)
    margin = float(page_info.get("margin_mm") or DEFAULT_MARGIN_MM)
    if width < 100 or height < 100:
        raise ValueError("答题卡纸张尺寸不正确")
    preset = str(layout.get("preset") or DEFAULT_PRESET).upper()
    if preset not in PAPER_SIZES:
        raise ValueError("答题卡纸张只支持 A4 或 A3")
    orientation = "landscape" if width > height else "portrait"
    expected = PAPER_SIZES[preset][orientation]
    if abs(expected[0] - width) > 0.5 or abs(expected[1] - height) > 0.5:
        raise ValueError("答题卡纸张尺寸与 preset 不一致")
    columns = int(layout.get("columns") or 1)
    if columns < 1 or columns > 4:
        raise ValueError("答题卡分栏数应在 1–4 之间")
    student = layout.get("student_no") if isinstance(layout.get("student_no"), dict) else {}
    digits = int(student.get("digits") or DEFAULT_STUDENT_DIGITS)
    if digits not in (6, 8):
        raise ValueError("学号位数只支持 6 位或 8 位")
    template = layout.get("template") if isinstance(layout.get("template"), dict) else {}
    if int(template.get("page_count") or len(pages)) != len(pages):
        raise ValueError("答题卡页数与 template.page_count 不一致")
    seen_page_numbers = []
    for page in pages:
        page_no = int(page.get("page_no") or 0)
        seen_page_numbers.append(page_no)
        if page_no < 1:
            raise ValueError("答题卡页码不正确")
        anchors = page.get("anchors")
        if not isinstance(anchors, list) or len(anchors) != 4:
            raise ValueError(f"第 {page_no} 页四角定位点必须正好 4 个")
        corners = {anchor.get("corner"): anchor for anchor in anchors if isinstance(anchor, dict)}
        if set(corners) != {"top_left", "top_right", "bottom_left", "bottom_right"}:
            raise ValueError(f"第 {page_no} 页定位点顺序/名称不正确")
        expect_positions = {
            "top_left": (margin, margin),
            "top_right": (width - margin - ANCHOR_W_MM, margin),
            "bottom_left": (margin, height - margin - ANCHOR_H_MM),
            "bottom_right": (width - margin - ANCHOR_W_MM, height - margin - ANCHOR_H_MM),
        }
        for corner, (expect_x, expect_y) in expect_positions.items():
            anchor = corners[corner]
            if abs(float(anchor.get("x") or 0) - expect_x) > 0.5 \
                    or abs(float(anchor.get("y") or 0) - expect_y) > 0.5:
                raise ValueError(f"第 {page_no} 页 {corner} 定位点位置不正确")
            if float(anchor.get("w") or 0) < 5 or float(anchor.get("h") or 0) < 2:
                raise ValueError(f"第 {page_no} 页 {corner} 定位点尺寸过小")
        student_area = (page.get("header") or {}).get("student_no") if isinstance(page.get("header"), dict) else None
        if isinstance(student_area, dict):
            rows = student_area.get("digit_rows") or []
            if len(rows) != digits:
                raise ValueError(f"第 {page_no} 页学号区位数为 {len(rows)}，与设定 {digits} 位不一致")
            bubble = bool(student_area.get("bubble"))
            for row in rows:
                bubbles = row.get("bubbles") or []
                if bubble and len(bubbles) != 10:
                    raise ValueError(f"第 {page_no} 页学号填涂必须每位 10 个气泡")
                seen: list[dict] = []
                for item in bubbles:
                    if not isinstance(item, dict):
                        raise ValueError(f"第 {page_no} 页学号气泡数据不正确")
                    point = {"x": item.get("x"), "y": item.get("y"), "r": item.get("r") or 1.15}
                    if any(_bubbles_overlap(point, other) for other in seen):
                        raise ValueError(f"第 {page_no} 页学号气泡重叠")
                    seen.append(point)
        header = page.get("header") if isinstance(page.get("header"), dict) else {}
        title_cell = header.get("title") if isinstance(header.get("title"), dict) else None
        if isinstance(title_cell, dict) and str(title_cell.get("text") or "").strip():
            title_rect = _text_rect(title_cell)
            if title_rect["x"] < 0 or title_rect["x"] + title_rect["w"] > width + 0.5:
                raise ValueError(f"第 {page_no} 页页眉标题超出页面边界")
            for anchor in anchors:
                if _rects_intersect(title_rect, anchor):
                    raise ValueError(f"第 {page_no} 页页眉标题与定位点重叠")
            if isinstance(student_area, dict):
                student_rect = {
                    "x": float(student_area.get("x") or 0),
                    "y": float(student_area.get("y") or 0),
                    "w": float(student_area.get("w") or 0),
                    "h": float(student_area.get("h") or 0),
                }
                if _rects_intersect(title_rect, student_rect):
                    raise ValueError(f"第 {page_no} 页页眉标题与学号区重叠")
        if isinstance(student_area, dict):
            student_rect = {
                "x": float(student_area.get("x") or 0),
                "y": float(student_area.get("y") or 0),
                "w": float(student_area.get("w") or 0),
                "h": float(student_area.get("h") or 0),
            }
            for anchor in anchors:
                if _rects_intersect(student_rect, anchor):
                    raise ValueError(f"第 {page_no} 页学号区与定位点重叠")
        groups = page.get("groups") if isinstance(page.get("groups"), list) else []
        if not groups:
            raise ValueError(f"第 {page_no} 页没有题组")
        for group in groups:
            if not isinstance(group, dict):
                raise ValueError(f"第 {page_no} 页题组数据不正确")
            if group.get("w", 0) <= 0 or group.get("h", 0) <= 0:
                raise ValueError(f"第 {page_no} 页题组外框尺寸不正确")
            if not _rect_inside({"x": group.get("x"), "y": group.get("y"),
                                 "w": group.get("w"), "h": group.get("h")}, width, height, margin):
                raise ValueError(f"第 {page_no} 页题组「{group.get('title')}」超出页面可打印区域")
            for question in group.get("questions") or []:
                rect = {"x": question.get("x"), "y": question.get("y"),
                        "w": question.get("w"), "h": question.get("h")}
                if not _rect_inside(rect, width, height, margin):
                    raise ValueError(f"第 {page_no} 页第 {question.get('no')} 题超出页面边界")
                bubbles = [bubble for bubble in (question.get("bubbles") or []) if isinstance(bubble, dict)]
                for index, bubble in enumerate(bubbles):
                    point = {"x": bubble.get("x"), "y": bubble.get("y"), "r": bubble.get("r") or BUBBLE_R_MM}
                    if not _within(float(point["x"]), margin, width - margin) \
                            or not _within(float(point["y"]), 0, height):
                        raise ValueError(f"第 {page_no} 页第 {question.get('no')} 题气泡越界")
                    for other in bubbles[:index]:
                        if _bubbles_overlap(point, {"x": other.get("x"), "y": other.get("y"),
                                                    "r": other.get("r") or BUBBLE_R_MM}):
                            raise ValueError(f"第 {page_no} 页第 {question.get('no')} 题气泡重叠")
                write_area = question.get("write_area")
                if isinstance(write_area, dict):
                    if not _rect_inside(write_area, width, height, margin):
                        raise ValueError(f"第 {page_no} 页第 {question.get('no')} 题书写区越界")
                    if int(write_area.get("lines") or 0) < 1:
                        raise ValueError(f"第 {page_no} 页第 {question.get('no')} 题书写行数不正确")
        footer = page.get("footer") if isinstance(page.get("footer"), dict) else {}
        code = str((footer.get("page_code") or {}).get("text") or "")
        if len(code) != 46 or not code.startswith("101") or not code.endswith("110") \
                or any(char not in "01" for char in code):
            raise ValueError(f"第 {page_no} 页页面码必须是 46 位二进制（101…110）")
        signature = template.get("signature")
        if signature is not None:
            try:
                expected_code = page_code(int(signature), page_no)
            except (TypeError, ValueError):
                raise ValueError("答题卡模板签名不正确")
            if code != expected_code:
                raise ValueError(f"第 {page_no} 页页面码与模板签名/页码不一致")
        if len(set(seen_page_numbers)) != len(seen_page_numbers):
            raise ValueError("答题卡页码重复")
    if seen_page_numbers != list(range(1, len(pages) + 1)):
        raise ValueError("答题卡页码必须从 1 开始连续编号")


# ---------------------------------------------------------------------------
# SVG 渲染
# ---------------------------------------------------------------------------

def _px(value: float) -> float:
    return round(float(value) * MM_TO_PX, 3)


def _escape(text) -> str:
    return (str(text or "").replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _rect_mm(rect: dict, attrs: str = "") -> str:
    return (f'<rect x="{_px(rect.get("x", 0))}" y="{_px(rect.get("y", 0))}" '
            f'width="{_px(rect.get("w", 0))}" height="{_px(rect.get("h", 0))}" {attrs}/>')


def _text_mm(x: float, y: float, size_mm: float, text: str, attrs: str = "") -> str:
    return (f'<text x="{_px(x)}" y="{_px(y)}" font-size="{_px(size_mm)}" {attrs}>'
            f'{_escape(text)}</text>')


def _qr_modules(text: str):
    """可选二维码：qrcode 已安装则返回模块矩阵；缺包/异常返回 None（不引入新依赖）。"""
    try:
        import qrcode
    except Exception:  # noqa: BLE001 - 二维码是可选项
        return None
    try:
        qr = qrcode.QRCode(version=None, error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=1, border=0)
        qr.add_data(str(text or ""))
        qr.make(fit=True)
        return qr.get_matrix()
    except Exception:  # noqa: BLE001
        return None


def _render_qr(qr: dict, stroke: str) -> str:
    modules = _qr_modules(qr.get("text"))
    if not modules:
        return ""
    size = float(qr.get("size_mm") or 16)
    x = float(qr.get("x") or 0)
    y = float(qr.get("y") or 0)
    count = max(1, len(modules))
    cell = size / count
    parts = [f'<g class="card-qr" fill="{stroke}">']
    for row_index, row in enumerate(modules):
        for column_index, cell_on in enumerate(row):
            if not cell_on:
                continue
            parts.append(
                f'<rect x="{_px(x + column_index * cell)}" y="{_px(y + row_index * cell)}" '
                f'width="{_px(cell + 0.05)}" height="{_px(cell + 0.05)}"/>'
            )
    parts.append("</g>")
    return "".join(parts)


def render_card_svg(layout: dict, page_no: int = 1, color: str = "black") -> str:
    """纯字符串生成某页答题卡 SVG（α 通道透明背景 + 白底；定位点/页面码固定黑色）。"""
    if not isinstance(layout, dict):
        raise ValueError("答题卡 layout 必须是 JSON 对象")
    pages = layout.get("pages") or []
    page = next((item for item in pages if int(item.get("page_no") or 0) == int(page_no)), None)
    if page is None:
        raise ValueError("答题卡没有这一页")
    page_info = layout.get("paper") if isinstance(layout.get("paper"), dict) else {}
    width_mm = float(page_info.get("width_mm") or page.get("width_mm") or 210)
    height_mm = float(page_info.get("height_mm") or page.get("height_mm") or 297)
    palette = PRINT_COLORS.get(str(color or "black").lower(), PRINT_COLORS["black"])
    stroke = palette
    if str(color or "").lower() == "red":
        stroke = PRINT_COLORS["red"]
    width = _px(width_mm)
    height = _px(height_mm)
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" data-card-page="{int(page_no)}" '
        f'data-card-color="{_escape(color)}" font-family=\'{FONT_STACK}\'>',
        f'<rect x="0" y="0" width="{width}" height="{height}" fill="#ffffff"/>',
        f'<g class="card-anchors" fill="#111111">',
    ]
    for anchor in page.get("anchors") or []:
        parts.append(_rect_mm(anchor))
    parts.append("</g>")
    header = page.get("header") if isinstance(page.get("header"), dict) else {}
    title = header.get("title") if isinstance(header.get("title"), dict) else {}
    if title:
        parts.append(_text_mm(
            title.get("x", width_mm / 2), title.get("y", 8), title.get("size", 5.2),
            title.get("text") or "",
            f'text-anchor="{title.get("anchor", "start")}" font-weight="bold" fill="{stroke}"',
        ))
    meta = header.get("meta") if isinstance(header.get("meta"), dict) else {}
    if meta:
        parts.append(_text_mm(
            meta.get("x", width_mm / 2), meta.get("y", 14), meta.get("size", 3.2),
            meta.get("text") or "",
            f'text-anchor="{meta.get("anchor", "middle")}" fill="{stroke}"',
        ))
    for item in header.get("fields") or []:
        if not isinstance(item, dict):
            continue
        parts.append(_text_mm(
            float(item.get("x") or 0), float(item.get("y") or 0) + 4.2, 3.4,
            f'{item.get("label") or ""}：', f'fill="{stroke}"',
        ))
        parts.append(f'<line x1="{_px(float(item.get("x") or 0) + 12)}" '
                     f'y1="{_px(float(item.get("y") or 0) + 4.6)}" '
                     f'x2="{_px(float(item.get("x") or 0) + float(item.get("w") or 30))}" '
                     f'y2="{_px(float(item.get("y") or 0) + 4.6)}" stroke="{stroke}" stroke-width="0.3"/>')
    student_area = header.get("student_no") if isinstance(header.get("student_no"), dict) else None
    if student_area:
        parts.append(_text_mm(
            float(student_area.get("x") or 0), float(student_area.get("y") or 0) + 3.0, 3.0,
            "学号（填涂）" if student_area.get("bubble") else "学号（书写）", f'fill="{stroke}"',
        ))
        for row in student_area.get("digit_rows") or []:
            if not isinstance(row, dict):
                continue
            parts.append(_text_mm(
                float(student_area.get("x") or 0) + 2.0, float(row.get("y") or 0) + 2.6, 2.6,
                str(row.get("position") or ""), f'fill="{stroke}"',
            ))
            for bubble in row.get("bubbles") or []:
                cx = _px(bubble.get("x", 0))
                cy = _px(bubble.get("y", 0))
                r = _px(bubble.get("r", 1.15))
                if str(layout.get("bubble")) == "oval":
                    parts.append(
                        f'<ellipse cx="{cx}" cy="{cy}" rx="{round(r * 1.25, 3)}" ry="{r}" '
                        f'fill="none" stroke="{stroke}" stroke-width="0.25" class="card-bubble"/>'
                    )
                else:
                    parts.append(
                        f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="none" stroke="{stroke}" '
                        f'stroke-width="0.25" class="card-bubble" data-value="{bubble.get("value")}"/>'
                    )
            if isinstance(row.get("line"), dict):
                line = row["line"]
                parts.append(f'<line x1="{_px(line.get("x", 0))}" y1="{_px(line.get("y", 0))}" '
                             f'x2="{_px(float(line.get("x", 0)) + float(line.get("w") or 0))}" '
                             f'y2="{_px(line.get("y", 0))}" stroke="{stroke}" stroke-width="0.3"/>')
    for group in page.get("groups") or []:
        if not isinstance(group, dict):
            continue
        parts.append(_rect_mm(group, f'fill="none" stroke="{stroke}" stroke-width="0.4" class="card-group"'))
        if group.get("title"):
            parts.append(_text_mm(
                float(group.get("x") or 0) + 1.8, float(group.get("y") or 0) + 4.2, 3.2,
                group.get("title") or "", f'fill="{stroke}" font-weight="bold"',
            ))
        for question in group.get("questions") or []:
            if not isinstance(question, dict):
                continue
            parts.append(_text_mm(
                float(question.get("x") or 0) + 1.6, float(question.get("y") or 0) + 4.6, 3.2,
                f'{question.get("no")}.', f'fill="{stroke}" font-weight="bold"',
            ))
            for bubble in question.get("bubbles") or []:
                cx = _px(bubble.get("x", 0))
                cy = _px(bubble.get("y", 0))
                r = _px(bubble.get("r", BUBBLE_R_MM))
                if str(layout.get("bubble")) == "oval":
                    parts.append(
                        f'<ellipse cx="{cx}" cy="{cy}" rx="{round(r * 1.25, 3)}" ry="{r}" '
                        f'fill="none" stroke="{stroke}" stroke-width="0.25" class="card-bubble"/>'
                    )
                else:
                    parts.append(
                        f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="none" stroke="{stroke}" '
                        f'stroke-width="0.25" class="card-bubble"/>'
                    )
                parts.append(_text_mm(
                    float(bubble.get("x") or 0) + 3.6, float(bubble.get("y") or 0) + 1.1, 2.6,
                    bubble.get("label") or "", f'fill="{stroke}"',
                ))
            write_area = question.get("write_area")
            if isinstance(write_area, dict):
                lines = max(1, int(write_area.get("lines") or 1))
                area_x = float(write_area.get("x") or 0)
                area_y = float(write_area.get("y") or 0) + 3.0
                area_w = float(write_area.get("w") or 0)
                area_h = float(write_area.get("h") or 0)
                parts.append(f'<rect x="{_px(area_x)}" y="{_px(area_y)}" '
                             f'width="{_px(area_w)}" height="{_px(max(4.0, area_h - 3.0))}" '
                             f'fill="none" stroke="{stroke}" stroke-width="0.25" class="card-write-area"/>')
                line_step = max(2.5, (area_h - 4.0) / lines)
                for index in range(lines):
                    y = area_y + 4.0 + index * line_step
                    if y >= area_y + area_h:
                        break
                    parts.append(f'<line x1="{_px(area_x)}" y1="{_px(y)}" '
                                 f'x2="{_px(area_x + area_w)}" y2="{_px(y)}" '
                                 f'stroke="{stroke}" stroke-width="0.2"/>')
    footer = page.get("footer") if isinstance(page.get("footer"), dict) else {}
    for key in ("notice", "page_text", "page_code"):
        item = footer.get(key) if isinstance(footer.get(key), dict) else None
        if not item:
            continue
        anchor = item.get("anchor") or "start"
        attrs = f'fill="#111111" text-anchor="{anchor}"'
        if key == "page_code":
            attrs += f' font-family=\'{MONO_STACK}\' letter-spacing="0.2"'
        parts.append(_text_mm(
            float(item.get("x") or 0), float(item.get("y") or 0),
            float(item.get("size") or 3.0), item.get("text") or "", attrs,
        ))
    qr = page.get("qr") if isinstance(page.get("qr"), dict) else None
    if qr:
        parts.append(_render_qr(qr, "#111111"))
    parts.append("</svg>")
    return "".join(parts)


__all__ = [
    "MM_TO_PX", "PAPER_SIZES", "CARD_STATUSES", "build_card_layout", "validate_card_layout",
    "render_card_svg", "page_code", "fnv32", "template_signature", "PRINT_COLORS",
]
