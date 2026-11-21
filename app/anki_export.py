"""Anki `.apkg` 导出（genanki，MIT License，Copyright (c) Kerrick Staley）。

为什么要做：项目原先只能导出 Anki CSV——牌组、卡牌模板、错题配图与复习状态全部丢失，
学生导入后还要手工建牌组。这里直接生成 `.apkg`：双击即可导入 Anki / AnkiDroid。

两条导出线：

* **错题本**（`wrongbook_cards`）：按学科分牌组。正面＝来源 + 题干 + 选项，背面＝参考
  答案 + 解析 + 我的错因/知识点/复习状态。答案与解析沿用 `practice._public_question`
  的门控（未提交作答的题不会出现在错题本里，因此这里拿到的解析都是用户已解锁的）。
* **笔记**（`note_cards`）：与既有 Anki CSV 完全同一套切卡规则（`### Q: / **A:**` 优先，
  其次「标题 → 段落」），避免两种导出给出不同的卡。

边界：只导文字卡。题图/公式图不塞进 apkg——Anki 端没有题库的逐题素材授权上下文，
离线包里也无法校验版本；含图题会在背面注明「图片请回题库查看」，不伪造内容。
"""
from __future__ import annotations

import html
import os
import re
import tempfile
import zlib

MAX_FIELD_CHARS = 20000
MAX_CARDS = 200

_MODEL_NAME = "知序卡牌"
_CSS = """
.card {
  font-family: "Songti SC", "Noto Serif SC", serif;
  font-size: 17px;
  line-height: 1.75;
  color: #1d3b34;
  background: #fbfaf5;
  text-align: left;
  padding: 14px 16px;
}
.zx-meta { color: #6d857c; font-size: 13px; margin-bottom: 10px; }
.zx-stem { white-space: normal; }
.zx-options { margin: 10px 0 0; padding-left: 0; list-style: none; }
.zx-options li { margin: 4px 0; }
.zx-hint { margin-top: 14px; color: #947446; font-size: 13px; }
.zx-answer { margin-top: 12px; padding: 10px 12px; border-left: 3px solid #74a894; background: #f1f8f4; }
.zx-solution { margin-top: 12px; }
.zx-tags { margin-top: 14px; color: #6d857c; font-size: 13px; }
hr#answer { border: 0; border-top: 1px solid #dedfd6; margin: 16px 0; }
"""


def _genanki():
    """惰性导入：没装 genanki 时应用仍能启动，导出接口返回明确 503。"""
    import genanki  # noqa: WPS433 - 可选依赖，见 requirements.txt

    return genanki


def _stable_id(name: str, salt: str = "") -> int:
    """稳定的 deck/model id：同一份数据反复导出得到同一个牌组，导入 Anki 不会重复建。"""
    return (zlib.crc32(f"{salt}:{name}".encode("utf-8")) & 0x7FFFFFFF) + 1


def _escape(text: str) -> str:
    return html.escape(str(text or ""), quote=False)


def text_to_html(text: str) -> str:
    """纯文本 → Anki 字段 HTML（转义并保留换行）。"""
    return _escape(text).replace("\r\n", "\n").replace("\n", "<br>")


def html_to_text(markup: str) -> str:
    """把卡牌 HTML 还原成 CSV 用的纯文本（保留换行，不把标签写进表格）。"""
    import html as html_module

    text = re.sub(r"<br\s*/?>", "\n", str(markup or ""), flags=re.IGNORECASE)
    text = re.sub(r"</(div|p|li|ul|ol)>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    text = html_module.unescape(text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _clip(text: str) -> str:
    value = str(text or "")
    return value[:MAX_FIELD_CHARS]


def _safe_tags(tags) -> list[str]:
    out = []
    for raw in tags or []:
        tag = re.sub(r"[^\w\u4e00-\u9fff·-]", "_", str(raw)).strip("_")
        if tag and tag not in out:
            out.append(tag)
    return out


def _model():
    genanki = _genanki()
    return genanki.Model(
        _stable_id("model", "zhixu"),
        _MODEL_NAME,
        fields=[{"name": "Front"}, {"name": "Back"}, {"name": "Tags"}],
        templates=[{
            "name": "知序卡 1",
            "qfmt": "{{Front}}",
            "afmt": '{{FrontSide}}<hr id="answer">{{Back}}',
        }],
        css=_CSS,
    )


def build_apkg(deck_groups) -> bytes:
    """把 `[(牌组名, [卡牌 dict])]` 打成 `.apkg` 字节串；没有任何卡时返回 b""。

    卡牌 dict：`{"front": str, "back": str, "tags": [str]}`（front/back 已是 HTML）。
    """
    genanki = _genanki()
    model = _model()
    decks = []
    for name, cards in deck_groups:
        deck = genanki.Deck(_stable_id(name, "deck"), str(name)[:100])
        for card in list(cards or [])[:MAX_CARDS]:
            front = _clip(card.get("front"))
            back = _clip(card.get("back"))
            if not front or not back:
                continue
            tags = _safe_tags(card.get("tags"))
            deck.add_note(genanki.Note(
                model=model,
                fields=[front, back, " ".join(tags)],
                tags=tags,
            ))
        if deck.notes:
            decks.append(deck)
    if not decks:
        return b""

    package = genanki.Package(decks)
    fd, path = tempfile.mkstemp(suffix=".apkg")
    os.close(fd)
    try:
        package.write_to_file(path)
        with open(path, "rb") as handle:
            return handle.read()
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def attachment_headers(filename: str) -> dict[str, str]:
    """同时给 ASCII 回退名与 RFC 5987 的 UTF-8 名，中文文件名在各浏览器都能落盘。"""
    ascii_name = re.sub(r"[^A-Za-z0-9._-]", "_", filename) or "zhixu-export.apkg"
    from urllib.parse import quote

    return {
        "Content-Disposition": (
            f'attachment; filename="{ascii_name}"; '
            f"filename*=UTF-8''{quote(filename, safe='')}"
        ),
        "Cache-Control": "no-store",
    }


# ---------------------------------------------------------------------------
# 错题本 → 牌组
# ---------------------------------------------------------------------------


def _option_lines(options) -> str:
    items = []
    for option in options or []:
        if not isinstance(option, dict):
            continue
        label = _escape(option.get("label") or "")
        text = text_to_html(option.get("text") or "")
        if text:
            items.append(f'<li><strong>{label}.</strong> {text}</li>')
    if not items:
        return ""
    return '<ul class="zx-options">' + "".join(items) + "</ul>"


def _wrong_meta(item: dict) -> str:
    parts = [str(item.get("subject") or ""), str(item.get("year") or "") + ("年" if item.get("year") else "")]
    if item.get("paper_name"):
        parts.append(str(item["paper_name"])[:40])
    if item.get("number_label"):
        parts.append(f"原卷第{item['number_label']}题")
    if item.get("type_label"):
        parts.append(str(item["type_label"]))
    return " · ".join([part for part in parts if part])


def _wrong_card(user_id: int, item: dict) -> dict | None:
    """把一条错题整理成 Anki 卡；题目被隔离/不存在时返回 None（不导不完整内容）。"""
    from . import practice  # 惰性导入：practice 依赖题库，避免模块级循环

    qid = item.get("qid")
    if not qid:
        return None
    try:
        question = practice.load_question(qid)
    except Exception:  # 题库切版本后个别题可能已不存在，跳过而不是让整份导出失败
        return None
    if question.get("quarantined"):
        return None
    try:
        detail = practice._public_question(question, user_id)
    except Exception:
        detail = {}
    stem = detail.get("stem_plain") or question.get("stem_plain") or ""
    if not str(stem).strip():
        return None

    meta = _wrong_meta({**item, **{k: detail.get(k) or item.get(k) for k in ("subject", "year", "paper_name", "number_label", "type_label")}})
    front = f'<div class="zx-meta">{_escape(meta)}</div>'
    front += f'<div class="zx-stem">{text_to_html(stem)}</div>'
    front += _option_lines(detail.get("options") or question.get("options"))

    answer = detail.get("answer_text") or (question.get("answer_text") if question.get("has_answer") else "") or ""
    solution = detail.get("solution") or ""
    back_parts = []
    if str(answer).strip():
        back_parts.append(f'<div class="zx-answer"><strong>参考答案：</strong>{text_to_html(answer)}</div>')
    else:
        back_parts.append('<div class="zx-answer"><strong>参考答案：</strong>原卷未提供有效参考答案，请回题库查看讲解。</div>')
    if str(solution).strip():
        back_parts.append(f'<div class="zx-solution"><strong>解析：</strong>{text_to_html(solution)}</div>')
    wrong_bits = []
    if item.get("reason"):
        wrong_bits.append(f'错因：{_escape(item["reason"])}')
    if item.get("error_tags"):
        wrong_bits.append("错因标签：" + "、".join(_escape(str(tag)) for tag in item["error_tags"][:6]))
    if item.get("review_count"):
        wrong_bits.append(f'已复习 {_escape(item["review_count"])} 次')
    if item.get("last_result"):
        wrong_bits.append(f'最近作答：{_escape(item["last_result"])}')
    if wrong_bits:
        back_parts.append('<div class="zx-tags">' + " · ".join(wrong_bits) + "</div>")
    if item.get("images") or question.get("images"):
        back_parts.append('<div class="zx-hint">本题含图，图片请回知序题库查看原卷。</div>')

    tags = [str(item.get("subject") or "知序错题"), "知序错题"]
    tags += [str(tag) for tag in (item.get("error_tags") or [])[:3]]
    return {"front": front, "back": "".join(back_parts), "tags": tags}


def wrongbook_cards(
    user_id: int,
    subject: str | None = None,
    include_archived: bool = False,
    limit: int = MAX_CARDS,
) -> list[tuple[str, list[dict]]]:
    """按学科分组返回错题卡：`[(牌组名, [卡牌])]`。"""
    from . import practice

    items, _total = practice._wrong_items(
        user_id,
        subject=subject,
        include_archived=include_archived,
        page=1,
        page_size=max(1, min(int(limit), MAX_CARDS)),
    )
    grouped: dict[str, list[dict]] = {}
    for item in items or []:
        card = _wrong_card(user_id, item)
        if not card:
            continue
        subject_name = str(item.get("subject") or "未分学科")
        grouped.setdefault(subject_name, []).append(card)
    return [(f"知序错题 · {name}", cards) for name, cards in sorted(grouped.items())]


# ---------------------------------------------------------------------------
# 笔记 → 牌组
# ---------------------------------------------------------------------------


def note_cards(content: str, subject: str | None = None, title: str = "") -> list[dict]:
    """从笔记 Markdown 切卡，规则与 `GET /api/ai/notes/{id}/export?format=anki` 完全一致。"""
    text = content or ""
    cards: list[dict] = []
    qa_matches = re.findall(r"###\s*Q:\s*(.+?)\n+\*\*A:\*\*\s*(.+?)(?=\n###\s*Q:|\Z)", text, re.S)
    if qa_matches:
        for question, answer in qa_matches:
            cards.append({
                "front": text_to_html(question.strip()),
                "back": text_to_html(answer.strip()),
                "tags": [subject or "知序笔记"],
            })
    else:
        for section in re.split(r"\n(?=#{2,3}\s+)", text):
            lines = section.strip().splitlines()
            if len(lines) >= 2 and lines[0].startswith("#"):
                front = re.sub(r"^#+\s*", "", lines[0]).strip()
                back = "\n".join(lines[1:]).strip()
                if front and back:
                    cards.append({
                        "front": text_to_html(front),
                        "back": text_to_html(back[:1500]),
                        "tags": [subject or "知序笔记"],
                    })
    return cards


def note_deck(note: dict) -> list[tuple[str, list[dict]]]:
    title = (note.get("title") or "未命名笔记")[:40]
    cards = note_cards(note.get("content") or "", note.get("subject"), title)
    if not cards:
        return []
    return [(f"知序笔记 · {title}", cards)]
