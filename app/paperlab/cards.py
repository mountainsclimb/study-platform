"""试卷工坊 P1：答题卡模板、几何校验、SVG 与打印导出。

- 一张试卷可生成多张答题卡（``pl_cards``）；模板几何存放在 ``layout_json``（毫米坐标，
  由 ``card_svg.build_card_layout`` 生成），创建时做 ``validate_card_layout`` 几何校验。
- 试卷结构（题目/顺序/分值/选项数）变化后，旧卡自动标记 ``stale``；stale 卡不可编辑，
  但历史 SVG 仍可导出（提示重新生成），不静默篡改几何。
- SVG 由 ``card_svg.render_card_svg`` 纯字符串生成（定位点/学号气泡/题组外框/书写区/
  46 位页面码/可选二维码）；本模块只做持久化、归属、stale 与 HTTP 包装。
"""
from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from ..auth import current_user
from ..db import connection, utcnow, write_lock
from . import card_svg, papers
from .db import json_dump, json_load

router = APIRouter()

MAX_CARDS_PER_PAPER = 20
MAX_CARD_TITLE = 120
CARD_FORMATS = ("html", "svg")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS pl_cards (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    paper_id INTEGER NOT NULL REFERENCES pl_papers(id) ON DELETE CASCADE,
    title TEXT NOT NULL DEFAULT '',
    layout_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'ready',
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS pl_cards_user ON pl_cards(user_id, id);
CREATE INDEX IF NOT EXISTS pl_cards_paper ON pl_cards(paper_id, id);
"""


def ensure_tables() -> None:
    with connection() as con:
        con.executescript(_SCHEMA)


# ---------------------------------------------------------------------------
# 读取/序列化
# ---------------------------------------------------------------------------

def _row_dict(row) -> dict:
    if isinstance(row, dict):
        return row
    try:
        return dict(row)
    except Exception:  # noqa: BLE001
        return {}


def _find_card(card_id: int, user_id: int) -> dict | None:
    with connection() as con:
        row = con.execute(
            "SELECT * FROM pl_cards WHERE id=? AND user_id=?", (card_id, user_id)
        ).fetchone()
    return _row_dict(row) if row is not None else None


def fetch_card(card_id: int, user_id: int) -> dict:
    card = _find_card(card_id, user_id)
    if card is None:
        raise HTTPException(404, "未找到该答题卡")
    return card


def _layout_of(card: dict) -> dict:
    layout = json_load(card.get("layout_json"), {})
    return layout if isinstance(layout, dict) else {}


def _paper_items_for_card(paper_id: int, user_id: int) -> tuple[dict, list[dict]]:
    """返回 (paper_row, items[含 snapshot])；试卷不存在/非本人 404。"""
    paper = papers.fetch_paper(paper_id, user_id)
    raw_items = papers.list_items(paper_id, user_id)
    if not raw_items:
        raise HTTPException(422, "试卷还没有题目，无法生成答题卡")
    items = []
    for row in raw_items:
        snapshot = json_load(row.get("snapshot_json"), {})
        items.append({
            "id": row.get("id"),
            "position": row.get("position"),
            "section": row.get("section") or "",
            "question_ref": row.get("question_ref") or "",
            "score": row.get("score"),
            "snapshot": snapshot if isinstance(snapshot, dict) else {},
        })
    return paper, items


def _fingerprint(paper: dict, items: list[dict]) -> str:
    return card_svg._paper_fingerprint(paper, items)


def _refresh_stale(card: dict, layout: dict, fingerprint: str) -> dict:
    """试卷结构变化后把旧卡标记 stale（惰性刷新，避免每次建卷主动扫全部卡）。"""
    template = layout.get("template") if isinstance(layout.get("template"), dict) else {}
    stored = str(template.get("paper_fingerprint") or "")
    if not stored or stored == fingerprint:
        return card
    if card.get("status") != "stale":
        now = utcnow()
        with connection() as con:
            con.execute(
                "UPDATE pl_cards SET status='stale', updated_at=? WHERE id=? AND user_id=?",
                (now, card["id"], card["user_id"]),
            )
        card = dict(card)
        card["status"] = "stale"
        card["updated_at"] = now
    return card


def public_card(card: dict, layout: dict | None = None) -> dict:
    layout = layout if isinstance(layout, dict) else _layout_of(card)
    template = layout.get("template") if isinstance(layout.get("template"), dict) else {}
    pages = layout.get("pages") if isinstance(layout.get("pages"), list) else []
    return {
        "id": int(card["id"]),
        "paper_id": int(card["paper_id"]),
        "title": card.get("title") or "",
        "status": card.get("status") or "ready",
        "version": int(card.get("version") or 1),
        "preset": layout.get("preset") or "A4",
        "orientation": layout.get("orientation") or "portrait",
        "columns": int(layout.get("columns") or 1),
        "bubble": layout.get("bubble") or "circle",
        "include_subjective": bool(layout.get("include_subjective", True)),
        "page_count": len(pages) or int(template.get("page_count") or 0),
        "paper_width_mm": (layout.get("paper") or {}).get("width_mm"),
        "paper_height_mm": (layout.get("paper") or {}).get("height_mm"),
        "template_signature": template.get("signature"),
        "created_at": card.get("created_at") or "",
        "updated_at": card.get("updated_at") or "",
    }


def _cards_for_paper(paper_id: int, user_id: int) -> list[dict]:
    with connection() as con:
        rows = con.execute(
            "SELECT * FROM pl_cards WHERE paper_id=? AND user_id=? ORDER BY id DESC",
            (paper_id, user_id),
        ).fetchall()
    return [_row_dict(row) for row in rows]


# ---------------------------------------------------------------------------
# 创建 / 查询 / 修改 / 删除
# ---------------------------------------------------------------------------

class StudentNoInput(BaseModel):
    digits: int = Field(default=6)
    bubble: bool = True


class CardCreateInput(BaseModel):
    preset: str = Field(default="A4", max_length=8)
    orientation: str | None = Field(default=None, max_length=12)
    columns: int = Field(default=card_svg.DEFAULT_COLUMNS, ge=1, le=4)
    bubble: str = Field(default="circle", max_length=10)
    include_subjective: bool = True
    student_no: StudentNoInput = Field(default_factory=StudentNoInput)
    title: str | None = Field(default=None, max_length=MAX_CARD_TITLE)


class CardOptionsInput(BaseModel):
    """版面参数局部修改：服务端基于当前试卷快照重新生成 layout（保留 card id）。"""

    preset: str | None = Field(default=None, max_length=8)
    orientation: str | None = Field(default=None, max_length=12)
    columns: int | None = Field(default=None, ge=1, le=4)
    bubble: str | None = Field(default=None, max_length=10)
    include_subjective: bool | None = None
    student_no: StudentNoInput | None = None


class CardPatchInput(BaseModel):
    version: int = Field(ge=1)
    title: str | None = Field(default=None, max_length=MAX_CARD_TITLE)
    layout: dict | None = None
    options: CardOptionsInput | None = None


def _clean_title(value, fallback: str) -> str:
    title = str(value or "").strip() or fallback
    if len(title) > MAX_CARD_TITLE:
        raise HTTPException(422, f"答题卡标题不能超过 {MAX_CARD_TITLE} 个字符")
    return title


@router.post("/papers/{paper_id}/cards")
def create_card(paper_id: int, data: CardCreateInput, user=Depends(current_user)):
    """按试卷快照生成答题卡模板（几何校验失败 422；试卷题目为空 422）。"""
    paper, items = _paper_items_for_card(paper_id, user["id"])
    if len(_cards_for_paper(paper_id, user["id"])) >= MAX_CARDS_PER_PAPER:
        raise HTTPException(422, f"同一张试卷最多保留 {MAX_CARDS_PER_PAPER} 张答题卡，请先删除旧卡")
    default_title = f"{paper.get('title') or '试卷'} · 答题卡"
    title = _clean_title(data.title, default_title)
    try:
        layout = card_svg.build_card_layout(
            paper, items,
            preset=data.preset, columns=data.columns, bubble=data.bubble,
            include_subjective=data.include_subjective,
            student_no=data.student_no.model_dump(),
            orientation=data.orientation, title=title,
        )
        card_svg.validate_card_layout(layout)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    now = utcnow()
    with connection() as con:
        write_lock(con, "paperlab-card")
        card_id = con.execute(
            "INSERT INTO pl_cards(user_id,paper_id,title,layout_json,status,version,created_at,updated_at) "
            "VALUES (?,?,?,?, 'ready', 1, ?, ?)",
            (user["id"], paper_id, title, json_dump(layout), now, now),
        ).lastrowid
    card = fetch_card(int(card_id), user["id"])
    return {"card": public_card(card, layout), "layout": layout}


@router.get("/papers/{paper_id}/cards")
def list_paper_cards(paper_id: int, user=Depends(current_user)):
    paper, items = _paper_items_for_card(paper_id, user["id"])
    fingerprint = _fingerprint(paper, items)
    cards = []
    for card in _cards_for_paper(paper_id, user["id"]):
        layout = _layout_of(card)
        cards.append(public_card(_refresh_stale(card, layout, fingerprint), layout))
    return {"items": cards, "total": len(cards)}


@router.get("/cards/{card_id}")
def get_card(card_id: int, user=Depends(current_user)):
    card = fetch_card(card_id, user["id"])
    layout = _layout_of(card)
    try:
        paper, items = _paper_items_for_card(card["paper_id"], user["id"])
        card = _refresh_stale(card, layout, _fingerprint(paper, items))
    except HTTPException:
        card = dict(card)
        card["status"] = "stale"
    return {"card": public_card(card, layout), "layout": layout}


@router.patch("/cards/{card_id}")
def patch_card(card_id: int, data: CardPatchInput, user=Depends(current_user)):
    """修改答题卡标题/版面。

    - ``layout``：前端完整编辑后的几何（兼容旧用法，服务端重算指纹/签名/页面码后校验）；
    - ``options``：只改 preset/orientation/columns/bubble/include_subjective/student_no，
      服务端基于试卷快照重新生成几何（等价重新生成但保留 card id），与 ``layout`` 互斥。
    """
    if data.layout is not None and data.options is not None:
        raise HTTPException(422, "layout 与 options 不能同时提交：请二选一")
    with connection() as con:
        write_lock(con, "paperlab-card")
        row = con.execute(
            "SELECT * FROM pl_cards WHERE id=? AND user_id=?", (card_id, user["id"])
        ).fetchone()
        if row is None:
            raise HTTPException(404, "未找到该答题卡")
        card = _row_dict(row)
        if card.get("status") == "stale":
            raise HTTPException(409, "试卷结构已变化，该答题卡已失效；请重新生成答题卡")
        current_version = int(card.get("version") or 1)
        if int(data.version) != current_version:
            raise HTTPException(409, "答题卡已被修改（版本冲突），请刷新后重试")
        layout = _layout_of(card)
        title_value = card.get("title") or "答题卡"
        layout_changed = False
        if data.layout is not None:
            candidate = data.layout
            try:
                paper, items = _paper_items_for_card(card["paper_id"], user["id"])
                pages = candidate.get("pages") if isinstance(candidate.get("pages"), list) else []
                template = candidate.get("template") if isinstance(candidate.get("template"), dict) else {}
                template = dict(template)
                template["paper_id"] = card["paper_id"]
                template["paper_fingerprint"] = _fingerprint(paper, items)
                template["page_count"] = len(pages)
                candidate["template"] = template
                new_signature = card_svg.template_signature(candidate)
                template["signature"] = new_signature
                # 手工编辑版面：按新签名/页码重算全部页面码，避免误报几何校验失败。
                for page in pages:
                    footer = page.get("footer") if isinstance(page.get("footer"), dict) else {}
                    code_cell = footer.get("page_code")
                    if isinstance(code_cell, dict):
                        code_cell["text"] = card_svg.page_code(
                            new_signature, int(page.get("page_no") or 1))
                card_svg.validate_card_layout(candidate)
            except HTTPException:
                raise HTTPException(409, "试卷已不存在，无法更新答题卡版面") from None
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from None
            layout = candidate
            layout_changed = True
            if data.title is None:
                title_value = str(candidate["template"].get("title") or title_value)
        elif data.options is not None:
            opts = data.options
            try:
                paper, items = _paper_items_for_card(card["paper_id"], user["id"])
                current_student = layout.get("student_no") if isinstance(layout.get("student_no"), dict) else {}
                if opts.student_no is not None:
                    student_no = opts.student_no.model_dump()
                else:
                    student_no = {
                        "digits": int(current_student.get("digits") or card_svg.DEFAULT_STUDENT_DIGITS),
                        "bubble": bool(current_student.get("bubble", True)),
                    }
                next_preset = opts.preset or str(layout.get("preset") or card_svg.DEFAULT_PRESET)
                preset_changed = (
                    opts.preset is not None
                    and str(opts.preset).upper() != str(layout.get("preset") or "").upper()
                )
                if opts.orientation is not None:
                    orientation = opts.orientation
                elif preset_changed:
                    orientation = None  # 换纸张时使用该纸张默认方向（A3 默认横向）
                else:
                    orientation = layout.get("orientation")
                title_value = _clean_title(
                    data.title if data.title is not None else (card.get("title") or "答题卡"),
                    card.get("title") or "答题卡",
                )
                candidate = card_svg.build_card_layout(
                    paper, items,
                    preset=next_preset,
                    columns=opts.columns if opts.columns is not None
                    else int(layout.get("columns") or card_svg.DEFAULT_COLUMNS),
                    bubble=opts.bubble if opts.bubble is not None
                    else str(layout.get("bubble") or card_svg.DEFAULT_BUBBLE),
                    include_subjective=opts.include_subjective if opts.include_subjective is not None
                    else bool(layout.get("include_subjective", True)),
                    student_no=student_no,
                    orientation=orientation,
                    title=title_value,
                )
                card_svg.validate_card_layout(candidate)
            except HTTPException:
                raise HTTPException(409, "试卷已不存在，无法更新答题卡版面") from None
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from None
            layout = candidate
            layout_changed = True
        sets: list[str] = []
        params: list = []
        if data.title is not None:
            title_value = _clean_title(data.title, title_value)
        if layout_changed or data.title is not None:
            template = layout.get("template") if isinstance(layout.get("template"), dict) else {}
            template = dict(template)
            template["title"] = title_value
            layout["template"] = template
            sets.extend(["layout_json=?", "title=?"])
            params.extend([json_dump(layout), title_value])
        if not sets:
            raise HTTPException(422, "没有需要修改的字段")
        sets.extend(["version=?", "updated_at=?"])
        params.extend([current_version + 1, utcnow(), card_id, user["id"], current_version])
        changed = con.execute(
            "UPDATE pl_cards SET " + ", ".join(sets) +
            " WHERE id=? AND user_id=? AND version=? AND status!='stale'",
            tuple(params),
        ).rowcount
        if not changed:
            raise HTTPException(409, "答题卡已被修改（版本冲突），请刷新后重试")
    card = fetch_card(card_id, user["id"])
    return {"card": public_card(card, layout), "layout": layout}


@router.delete("/cards/{card_id}")
def delete_card(card_id: int, user=Depends(current_user)):
    with connection() as con:
        write_lock(con, "paperlab-card")
        row = con.execute(
            "SELECT id FROM pl_cards WHERE id=? AND user_id=?", (card_id, user["id"])
        ).fetchone()
        if row is None:
            raise HTTPException(404, "未找到该答题卡")
        con.execute("DELETE FROM pl_cards WHERE id=? AND user_id=?", (card_id, user["id"]))
    return {"deleted": True, "card_id": card_id}


# ---------------------------------------------------------------------------
# SVG / 打印导出
# ---------------------------------------------------------------------------

def _card_and_layout(card_id: int, user: dict) -> tuple[dict, dict]:
    card = fetch_card(card_id, user["id"])
    layout = _layout_of(card)
    try:
        paper, items = _paper_items_for_card(card["paper_id"], user["id"])
        card = _refresh_stale(card, layout, _fingerprint(paper, items))
    except HTTPException:
        card = dict(card)
        card["status"] = "stale"
    if not layout.get("pages"):
        raise HTTPException(409, "答题卡版面数据缺失，请重新生成")
    return card, layout


def _page_count(layout: dict) -> int:
    pages = layout.get("pages")
    return len(pages) if isinstance(pages, list) else 0


@router.get("/cards/{card_id}/svg")
def card_svg_page(
    card_id: int,
    page: int = Query(default=1, ge=1),
    color: str = Query(default="black", max_length=10),
    user=Depends(current_user),
):
    """单页答题卡 SVG（``color=black|red`` 印刷色；stale 卡可导出但需重新生成后再用）。"""
    card, layout = _card_and_layout(card_id, user)
    total = _page_count(layout)
    if page > total:
        raise HTTPException(404, f"答题卡只有 {total} 页，没有第 {page} 页")
    if str(color or "").lower() not in card_svg.PRINT_COLORS:
        raise HTTPException(422, "印刷色只支持 black（黑）或 red（红）")
    try:
        svg = card_svg.render_card_svg(layout, page_no=page, color=color)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    headers = {
        "Cache-Control": "private, no-store",
        "X-Card-Stale": "1" if card.get("status") == "stale" else "0",
        "X-Card-Page-Count": str(total),
    }
    return Response(content=svg, media_type="image/svg+xml; charset=utf-8", headers=headers)


def _page_selection(page: str, total: int) -> list[int]:
    value = str(page or "all").strip().lower()
    if value in ("all", ""):
        return list(range(1, total + 1))
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise HTTPException(422, "page 只支持 all 或正整数页码") from None
    if number < 1 or number > total:
        raise HTTPException(404, f"答题卡只有 {total} 页，没有第 {number} 页")
    return [number]


def _card_html(card: dict, layout: dict, pages: list[int], color: str) -> str:
    paper = layout.get("paper") if isinstance(layout.get("paper"), dict) else {}
    width = float(paper.get("width_mm") or 210)
    height = float(paper.get("height_mm") or 297)
    margin = float(paper.get("margin_mm") or 12)
    title = str(card.get("title") or "答题卡")
    escaped_title = (title.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
    svg_pages = []
    for page_no in pages:
        try:
            svg = card_svg.render_card_svg(layout, page_no=page_no, color=color)
        except ValueError:
            continue
        svg_pages.append(
            f'<section class="card-page" data-page="{page_no}">{svg}'
            f'<div class="card-page-label">第 {page_no} / {len(pages)} 页</div></section>'
        )
    notice = ""
    if card.get("status") == "stale":
        notice = ('<div class="stale-notice">该答题卡对应的试卷结构已变化，当前为历史版本；'
                  '请重新生成答题卡后再打印使用。</div>')
    return (
        '<!doctype html><html lang="zh-CN"><head><meta charset="UTF-8">'
        f"<title>{escaped_title}</title><style>"
        f"@page {{ size: {width:g}mm {height:g}mm; margin: 0; }}"
        "*{box-sizing:border-box} body{margin:0;background:#f2f2f2;font-family:'SimSun',serif;}"
        f".card-page{{width:{width:g}mm;height:{height:g}mm;margin:0 auto 8mm;background:#fff;"
        "page-break-after:always;break-after:page;position:relative;}"
        f".card-page svg{{display:block;width:{width:g}mm;height:{height:g}mm;}}"
        ".card-page-label{position:absolute;right:3mm;bottom:1.5mm;font-size:7px;color:#999;}"
        ".card-page:last-child{page-break-after:auto;break-after:auto;}"
        f".stale-notice{{background:#fbf3e3;color:#7a5a1f;border:1px solid #e6d3ae;"
        f"width:{width:g}mm;margin:0 auto 4mm;padding:6px 10px;font-size:12px;}}"
        "@media print{body{background:#fff}.card-page{margin:0}.card-page-label{display:none}"
        ".stale-notice{display:none}}"
        "</style></head><body>"
        + notice + "".join(svg_pages)
        + "</body></html>"
    )


@router.get("/cards/{card_id}/export")
async def export_card(
    card_id: int,
    format: str = Query(default="html", max_length=10),
    page: str = Query(default="all", max_length=10),
    color: str = Query(default="black", max_length=10),
    user=Depends(current_user),
):
    """答题卡打印导出：``format=html``（可 page=all）、``format=svg``（单页）或 ``format=pdf``（服务端渲染）。"""
    card, layout = _card_and_layout(card_id, user)
    total = _page_count(layout)
    fmt = str(format or "html").strip().lower()
    if fmt not in ("html", "svg", "pdf"):
        raise HTTPException(422, "答题卡导出格式只支持 html、svg 或 pdf")
    if str(color or "").lower() not in card_svg.PRINT_COLORS:
        raise HTTPException(422, "印刷色只支持 black（黑）或 red（红）")
    if fmt == "svg":
        if str(page or "").strip().lower() in ("all", ""):
            raise HTTPException(422, "SVG 格式请指定单页页码（page=1…），多页请用 format=html")
        pages = _page_selection(page, total)
        svg = card_svg.render_card_svg(layout, page_no=pages[0], color=color)
        return Response(
            content=svg, media_type="image/svg+xml; charset=utf-8",
            headers={"X-Card-Page-Count": str(total),
                     "X-Card-Stale": "1" if card.get("status") == "stale" else "0",
                     "Cache-Control": "private, no-store"},
        )
    pages = _page_selection(page, total)
    html = _card_html(card, layout, pages, color)
    if fmt == "pdf":
        from ..ai import render as ai_render

        paper_size = str(layout.get("paper") or "A4").upper()
        if paper_size not in ("A4", "A3"):
            paper_size = "A4"
        try:
            pdf = await ai_render.html_to_pdf(html, paper=paper_size, landscape=False, margin_mm=0)
        except ai_render.RenderUnavailable as exc:
            raise HTTPException(
                503,
                "服务端 PDF 暂不可用（%s）；可改用 format=html 在浏览器打印并另存为 PDF" % exc,
            ) from exc
        title = str(card.get("title") or "答题卡")
        filename = quote(f"{title}.pdf")
        return Response(
            content=pdf, media_type="application/pdf",
            headers={
                "Content-Disposition": f"attachment; filename=card.pdf; filename*=UTF-8''{filename}",
                "Cache-Control": "private, no-store",
                "X-Card-Page-Count": str(len(pages)),
                "X-Card-Stale": "1" if card.get("status") == "stale" else "0",
            },
        )
    return HTMLResponse(
        content=html,
        headers={
            "X-Frame-Options": "SAMEORIGIN",
            "Content-Security-Policy": (
                "default-src 'none'; img-src data:; style-src 'unsafe-inline'; "
                "frame-ancestors 'self'; base-uri 'none'"
            ),
            "Cache-Control": "private, no-store",
            "X-Card-Page-Count": str(total),
            "X-Card-Stale": "1" if card.get("status") == "stale" else "0",
        },
    )


__all__ = [
    "router", "ensure_tables", "public_card", "fetch_card", "MAX_CARDS_PER_PAPER",
]
