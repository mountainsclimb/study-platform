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


# ---------------------------------------------------------------------------
# 站内题目引用与来源版本（统一引用 bank:<qid> / app:<id> / official:<id>）
#
# 导出到 Anki 的卡片保留站内题目链接所需的统一引用与来源版本，用户离线复习时
# 仍能对照「这道题属于哪个题库版本/精选发布版本」，也方便回站内继续练习。
# 字段只放在背面文本与标签里（不新增 Anki 字段/模板，不影响既有 .apkg 结构）。
# ---------------------------------------------------------------------------

_QUESTION_REF_RE = re.compile(
    r"(?:^|[^A-Za-z0-9_.:-])((?:bank:[A-Za-z0-9_.:-]+|app:\d+|official:plz-[A-Za-z0-9]+))"
)


def question_ref_of(item) -> dict | None:
    """统一题目引用与来源标签；缺失/非法返回 None（复用 ``app.question_ref`` 口径）。"""
    if not isinstance(item, dict):
        return None
    raw = item.get("ref") or item.get("qid") or item.get("source_ref") or item.get("question_ref")
    if not raw:
        return None
    from . import question_ref

    parsed = question_ref.try_parse(raw)
    if parsed is None:
        return None
    return {"ref": parsed.ref, "source": parsed.source, "source_label": parsed.source_label}


def source_version_of(item) -> str:
    """题目来源版本：题库题取 ``bank_version``，个人题/精选题取内容或发布版本。"""
    if not isinstance(item, dict):
        return ""
    for key in ("bank_version", "published_version", "content_revision", "source_version", "version"):
        value = item.get(key)
        if value is None or value == "":
            continue
        text = str(value).strip()
        if text and text.lower() not in ("none", "null", "0"):
            return text
    return ""


def _question_ref_footer(item) -> str:
    """错题卡背面的题目引用/来源/版本脚注；无引用时返回空串（不改变既有卡片）。"""
    info = question_ref_of(item)
    if not info:
        return ""
    parts = [f"题目引用：{info['ref']}", f"来源：{info['source_label']}"]
    version = source_version_of(item)
    if version:
        parts.append(f"来源版本：{version}")
    return '<div class="zx-ref">' + _escape(" · ".join(parts)) + "</div>"


def _reference_tags(item) -> list[str]:
    info = question_ref_of(item)
    if not info:
        return []
    tags = [f"引用_{info['source']}"]
    version = source_version_of(item)
    if version:
        tags.append(f"版本_{version[:32]}")
    return tags


def _note_refs_footer(question_refs, source_versions=None) -> str:
    """笔记卡背面的题目引用与来源版本脚注；无元数据时返回空串。"""
    refs = []
    seen = set()
    for item in question_refs or []:
        info = question_ref_of(item if isinstance(item, dict) else {"ref": item})
        if not info or info["ref"] in seen:
            continue
        seen.add(info["ref"])
        entry = {**info}
        if isinstance(item, dict):
            version = str(item.get("version") or item.get("published_version")
                          or item.get("content_revision") or item.get("bank_version") or "").strip()
            if version:
                entry["version"] = version
        refs.append(entry)
    parts = []
    if refs:
        bits = []
        for info in refs[:5]:
            bit = info["ref"]
            if info.get("version"):
                bit += f"（版本 {info['version']}）"
            bits.append(bit)
        parts.append("题目引用：" + "、".join(bits))
        sources = sorted({info["source_label"] for info in refs})
        if sources:
            parts.append("来源：" + "、".join(sources))
    versions = [str(value).strip() for value in (source_versions or []) if str(value or "").strip()]
    if versions:
        parts.append("来源版本：" + " · ".join(versions[:6]))
    if not parts:
        return ""
    return '<div class="zx-ref">' + _escape(" · ".join(parts)) + "</div>"


def _note_reference_tags(question_refs) -> list[str]:
    sources: list[str] = []
    for item in question_refs or []:
        info = question_ref_of(item if isinstance(item, dict) else {"ref": item})
        if info and info["source"] not in sources:
            sources.append(info["source"])
    return [f"笔记引用_{source}" for source in sources]


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
    # 站内题目引用 + 来源版本：离线复习时仍能对照题库/精选版本，回站内继续练习。
    footer = _question_ref_footer(item)
    if footer:
        back_parts.append(footer)

    tags = [str(item.get("subject") or "知序错题"), "知序错题"]
    tags += [str(tag) for tag in (item.get("error_tags") or [])[:3]]
    tags += _reference_tags(item)
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


def note_cards(content: str, subject: str | None = None, title: str = "", *,
               question_refs=None, source_versions=None) -> list[dict]:
    """从笔记 Markdown 切卡，规则与 `GET /api/ai/notes/{id}/export?format=anki` 完全一致。

    ``question_refs`` / ``source_versions`` 只用于在背面追加「题目引用/来源版本」脚注与
    标签（笔记正文里出现的统一题目引用、生成时资料版本）；不传时输出与既有完全一致。
    """
    text = content or ""
    cards: list[dict] = []
    footer = _note_refs_footer(question_refs, source_versions)
    extra_tags = _note_reference_tags(question_refs)

    def append(question: str, answer: str) -> None:
        cards.append({
            "front": text_to_html(question),
            "back": text_to_html(answer) + footer,
            "tags": [subject or "知序笔记", *extra_tags],
        })

    qa_matches = re.findall(r"###\s*Q:\s*(.+?)\n+\*\*A:\*\*\s*(.+?)(?=\n###\s*Q:|\Z)", text, re.S)
    if qa_matches:
        for question, answer in qa_matches:
            append(question.strip(), answer.strip())
    else:
        for section in re.split(r"\n(?=#{2,3}\s+)", text):
            lines = section.strip().splitlines()
            if len(lines) >= 2 and lines[0].startswith("#"):
                front = re.sub(r"^#+\s*", "", lines[0]).strip()
                back = "\n".join(lines[1:]).strip()
                if front and back:
                    append(front, back[:1500])
    return cards


#: 笔记正文里的统一题目引用（与站内 ``bank:/app:/official:`` 引用同一口径）。
def _note_question_refs(note: dict) -> list[dict]:
    refs: list[dict] = []
    seen: set[str] = set()

    def add(raw, meta=None) -> None:
        info = question_ref_of({"ref": raw})
        if not info or info["ref"] in seen:
            return
        seen.add(info["ref"])
        entry = {**info}
        version = source_version_of(meta) if isinstance(meta, dict) else ""
        if version:
            entry["version"] = version
        refs.append(entry)

    for source in note.get("sources") or []:
        if isinstance(source, dict):
            add(source.get("ref") or source.get("qid") or source.get("source_ref"), source)
    for match in _QUESTION_REF_RE.finditer(str(note.get("content") or "")):
        add(match.group(1))
    return refs


def _note_source_versions(note: dict) -> list[str]:
    """笔记生成时各资料的版本（``{doc_id: doc_version}``）→ ``["D1 v2", ...]``。"""
    raw = note.get("source_versions")
    if not isinstance(raw, dict):
        return []
    versions: list[str] = []
    for document_id, version in sorted(raw.items(), key=lambda pair: str(pair[0])):
        try:
            versions.append(f"D{int(document_id)} v{int(version)}")
        except (TypeError, ValueError):
            continue
    return versions


def vocab_deck(items) -> list[tuple[str, list[dict]]]:
    """词汇卡 → Anki 牌组：正面单词 + 音标，背面释义 + 标签。"""
    cards: list[dict] = []
    for item in items or []:
        word = str(item.get("word") or "").strip()
        if not word:
            continue
        tags = _safe_tags(str(item.get("tag") or "").split())
        phonetic = str(item.get("phonetic") or "").strip()
        front = f"<div class='zx-stem'><b>{_escape(word)}</b></div>"
        if phonetic:
            front += f"<div class='zx-hint'>[{_escape(phonetic)}]</div>"
        back = f"<div class='zx-answer'>{text_to_html(item.get('translation') or '')}</div>"
        if tags:
            back += f"<div class='zx-tags'>{_escape(' / '.join(tags))}</div>"
        cards.append({"front": front, "back": back, "tags": tags})
    return [("知序词汇 · 高考", cards)] if cards else []


def note_deck(note: dict) -> list[tuple[str, list[dict]]]:
    title = (note.get("title") or "未命名笔记")[:40]
    cards = note_cards(
        note.get("content") or "", note.get("subject"), title,
        question_refs=_note_question_refs(note),
        source_versions=_note_source_versions(note),
    )
    if not cards:
        return []
    return [(f"知序笔记 · {title}", cards)]
