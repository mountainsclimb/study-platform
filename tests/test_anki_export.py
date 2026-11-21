"""Anki `.apkg` 导出：牌组结构、切卡规则与接口错误分支。

真正的 Anki 导入无法在单元测试里跑，所以这里验证**产物结构**：
`.apkg` 是 zip，必须含 `collection.anki2`；该 SQLite 里的 notes / cards / decks
就是 Anki 读到的内容。
"""
import io
import json
import os
import sqlite3
import tempfile
import zipfile

from app import anki_export


def _collection_bytes(apkg_bytes: bytes) -> bytes:
    with zipfile.ZipFile(io.BytesIO(apkg_bytes)) as archive:
        names = set(archive.namelist())
        assert "collection.anki2" in names, names
        return archive.read("collection.anki2")


def _open_collection(payload: bytes):
    handle = tempfile.NamedTemporaryFile(suffix=".anki2", delete=False)
    handle.write(payload)
    handle.close()
    return handle.name


def _deck_ids(apkg_bytes: bytes) -> dict[str, int]:
    """{牌组名: 牌组 id}：Anki 用 id 判重，同内容重复导入必须得到同一组 id。"""
    path = _open_collection(_collection_bytes(apkg_bytes))
    try:
        con = sqlite3.connect(path)
        decks = json.loads(con.execute("SELECT decks FROM col").fetchone()[0])
        con.close()
    finally:
        os.unlink(path)
    return {str(deck.get("name")): int(deck.get("id")) for deck in decks.values()}


def test_build_apkg_produces_importable_structure():
    data = anki_export.build_apkg([
        ("知序错题 · 数学", [
            {"front": "<div>题干一</div>", "back": "<div>答案一</div>", "tags": ["数学", "错因"]},
            {"front": "<div>题干二</div>", "back": "<div>答案二</div>", "tags": []},
        ]),
    ])
    assert data, "apkg 不应为空"
    payload = _collection_bytes(data)
    assert payload.startswith(b"SQLite format 3")

    path = _open_collection(payload)
    try:
        con = sqlite3.connect(path)
        assert con.execute("SELECT COUNT(*) FROM notes").fetchone()[0] == 2
        assert con.execute("SELECT COUNT(*) FROM cards").fetchone()[0] == 2
        decks = json.loads(con.execute("SELECT decks FROM col").fetchone()[0])
        assert any("知序错题 · 数学" in str(deck.get("name", "")) for deck in decks.values())
        raw_note = con.execute("SELECT flds FROM notes LIMIT 1").fetchone()[0]
        assert "题干一" in raw_note and "答案一" in raw_note
        con.close()
    finally:
        os.unlink(path)


def test_build_apkg_groups_multiple_decks_and_skips_empty():
    data = anki_export.build_apkg([
        ("空牌组", []),
        ("甲", [{"front": "F1", "back": "B1", "tags": []}]),
        ("乙", [{"front": "F2", "back": "B2", "tags": []}, {"front": "", "back": "B3", "tags": []}]),
    ])
    assert data
    path = _open_collection(_collection_bytes(data))
    try:
        con = sqlite3.connect(path)
        assert con.execute("SELECT COUNT(*) FROM notes").fetchone()[0] == 2  # 空 front 的卡被跳过
        decks = json.loads(con.execute("SELECT decks FROM col").fetchone()[0])
        names = " ".join(str(deck.get("name", "")) for deck in decks.values())
        assert "空牌组" not in names
        assert "甲" in names and "乙" in names
        con.close()
    finally:
        os.unlink(path)


def test_build_apkg_returns_empty_without_cards():
    assert anki_export.build_apkg([]) == b""
    assert anki_export.build_apkg([("空", [])]) == b""


def test_note_cards_prefers_qa_then_headings():
    qa = "## 章\n### Q: 什么是导数？\n**A:** 函数在某点的瞬时变化率。\n\n### Q: 极值条件？\n**A:** 一阶导为 0 且变号。"
    cards = anki_export.note_cards(qa, "数学")
    assert len(cards) == 2
    assert "导数" in cards[0]["front"] and "变化率" in cards[0]["back"]
    assert cards[0]["tags"] == ["数学"]

    headings = "## 定义\n导数是一个极限。\n\n### 应用\n求切线斜率与单调区间。"
    fallback = anki_export.note_cards(headings, None)
    assert len(fallback) == 2
    assert fallback[0]["tags"] == ["知序笔记"]


def test_note_deck_empty_content_returns_nothing():
    assert anki_export.note_deck({"title": "空", "content": "", "subject": "数学"}) == []


def test_attachment_headers_handle_chinese_filename():
    headers = anki_export.attachment_headers("知序错题-数学.apkg")
    assert "attachment;" in headers["Content-Disposition"]
    assert "filename*=UTF-8''" in headers["Content-Disposition"]
    assert headers["Cache-Control"] == "no-store"


def test_html_to_text_strips_markup():
    text = anki_export.html_to_text("<div>题干</div><ul><li>A. 选项</li></ul>")
    assert "<" not in text and "题干" in text and "选项" in text


def test_wrongbook_export_rejects_empty_book(client):
    response = client.get("/api/wrongbook/export?format=apkg")
    assert response.status_code == 404
    assert "还没有" in response.json()["detail"]


def test_note_apkg_export_missing_note(client):
    response = client.get("/api/notes/999999/export?format=apkg")
    assert response.status_code in (403, 404)


def test_note_apkg_export_end_to_end(client):
    """笔记 → 接口 → zip 结构全链路：Anki 能读到的就是 collection.anki2 里的内容。"""
    from app import db

    me = client.get("/api/auth/me").json()
    user_id = (me.get("user") or me)["id"]
    now = db.utcnow()
    with db.connection() as con:
        cursor = con.execute(
            "INSERT INTO ai_notes "
            "(user_id, title, content, sources_json, origin, published, created_at, updated_at, subject) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (user_id, "导数清单",
             "### Q: 什么是导数？\n**A:** 函数在某点的瞬时变化率。\n\n"
             "### Q: 极值条件？\n**A:** 一阶导数为 0 且左右变号。",
             "[]", "user", 0, now, now, "数学"),
        )
        note_id = cursor.lastrowid
    assert note_id

    response = client.get(f"/api/notes/{note_id}/export?format=apkg")
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/vnd.anki"
    assert "filename*=UTF-8''" in response.headers["content-disposition"]

    path = _open_collection(_collection_bytes(response.content))
    try:
        con = sqlite3.connect(path)
        assert con.execute("SELECT COUNT(*) FROM notes").fetchone()[0] == 2
        fields = " ".join(row[0] for row in con.execute("SELECT flds FROM notes"))
        assert "导数" in fields and "变化率" in fields
        decks = json.loads(con.execute("SELECT decks FROM col").fetchone()[0])
        assert any("导数清单" in str(deck.get("name", "")) for deck in decks.values())
        con.close()
    finally:
        os.unlink(path)

    # 同一篇笔记重复导出：牌组 id 稳定（zip 里含时间戳，字节不会相同），
    # 导入 Anki 不会因此生成重复牌组。
    again = client.get(f"/api/notes/{note_id}/export?format=apkg")
    assert again.status_code == 200
    assert _deck_ids(again.content) == _deck_ids(response.content)


# ---------------------------------------------------------------------------
# 站内题目引用与来源版本（Phase 4：笔记/Anki 保留题目链接与版本）
# ---------------------------------------------------------------------------


def test_question_ref_helpers_normalize_and_degrade():
    bank = anki_export.question_ref_of({"qid": "0028f646b2b0dde3:1"})
    assert bank == {"ref": "bank:0028f646b2b0dde3:1", "source": "bank", "source_label": "高考题库"}
    official = anki_export.question_ref_of({"ref": "official:plz-0123456789abcdef0123"})
    assert official["source"] == "official" and official["ref"].startswith("official:plz-")
    app = anki_export.question_ref_of({"qid": "app:12"})
    assert app["source"] == "app" and app["ref"] == "app:12"
    # 缺字段/空白引用安全降级（不抛错、不伪造引用）。
    assert anki_export.question_ref_of(None) is None
    assert anki_export.question_ref_of({}) is None
    assert anki_export.question_ref_of({"qid": "   "}) is None

    assert anki_export.source_version_of({}) == ""
    assert anki_export.source_version_of({"bank_version": "bank-20261121-r5"}) == "bank-20261121-r5"
    assert anki_export.source_version_of({"published_version": 3}) == "3"
    assert anki_export.source_version_of({"content_revision": "7"}) == "7"


def test_wrong_card_carries_question_ref_and_bank_version(monkeypatch):
    """错题卡背面保留站内题目引用与题库版本（离线复习也能对照版本回站内练习）。"""
    from app import practice

    qid = "0028f646b2b0dde3:1"
    question = {
        "qid": qid, "stem_plain": "题干", "options": [], "has_answer": True,
        "answer_text": "A", "solution": "解析", "images": [], "quarantined": False,
    }
    monkeypatch.setattr(practice, "load_question", lambda value: question)
    monkeypatch.setattr(practice, "_public_question", lambda value, user_id: {
        "stem_plain": "题干", "options": [], "answer_text": "A", "solution": "解析",
        "subject": "数学", "year": 2024, "paper_name": "", "number_label": "", "type_label": "单选题",
    })
    card = anki_export._wrong_card(1, {
        "qid": qid, "ref": f"bank:{qid}", "source": "bank",
        "subject": "数学", "bank_version": "bank-20261121-r5",
    })
    assert card is not None
    assert f"题目引用：bank:{qid}" in card["back"]
    assert "来源版本：bank-20261121-r5" in card["back"]
    assert "知序错题" in card["tags"] and "引用_bank" in card["tags"]


def test_wrong_card_official_ref_uses_published_version(monkeypatch):
    from app import practice

    public_id = "plz-0123456789abcdef0123"
    qid = f"official:{public_id}"
    question = {
        "qid": qid, "stem_plain": "精选题干", "options": [], "has_answer": True,
        "answer_text": "B", "solution": "", "images": [], "quarantined": False,
    }
    monkeypatch.setattr(practice, "load_question", lambda value: question)
    monkeypatch.setattr(practice, "_public_question", lambda value, user_id: {
        "stem_plain": "精选题干", "options": [], "answer_text": "B", "solution": "",
        "subject": "物理", "year": None, "paper_name": "", "number_label": "", "type_label": "填空题",
    })
    card = anki_export._wrong_card(1, {
        "qid": qid, "ref": qid, "source": "official",
        "public_id": public_id, "published_version": 3, "subject": "物理",
    })
    assert card is not None
    assert f"题目引用：{qid}" in card["back"]
    assert "来源版本：3" in card["back"]
    assert "引用_official" in card["tags"]


def test_note_cards_include_question_refs_and_versions():
    cards = anki_export.note_cards(
        "### Q: 什么是导数？\n**A:** 函数在某点的瞬时变化率。",
        "数学", "导数清单",
        question_refs=[
            {"ref": "official:plz-0123456789abcdef0123", "published_version": 3},
            {"ref": "bank:0028f646b2b0dde3:1", "bank_version": "bank-20261121-r5"},
        ],
        source_versions=["D1 v2"],
    )
    assert len(cards) == 1
    back = cards[0]["back"]
    assert "official:plz-0123456789abcdef0123" in back
    assert "bank:0028f646b2b0dde3:1" in back
    assert "bank-20261121-r5" in back and "D1 v2" in back
    # 既有切卡语义/标签顺序保持：首标签仍是学科。
    assert cards[0]["tags"][0] == "数学"
    assert "笔记引用_official" in cards[0]["tags"]


def test_note_deck_infers_question_refs_from_content_and_sources():
    deck = anki_export.note_deck({
        "title": "导数清单",
        "subject": "数学",
        "content": "## 定义\n导数是一个极限。参考 bank:0028f646b2b0dde3:1 与 "
                   "official:plz-0123456789abcdef0123。",
        "sources": [{"ref": "app:12", "content_revision": "7"}],
        "source_versions": {"1": 2},
    })
    assert len(deck) == 1
    _name, cards = deck[0]
    assert cards
    back = " ".join(card["back"] for card in cards)
    for ref in ("bank:0028f646b2b0dde3:1", "official:plz-0123456789abcdef0123", "app:12"):
        assert ref in back
    assert "D1 v2" in back


def test_apkg_model_fields_unchanged_by_reference_metadata():
    """题目引用/版本只放背面文本与标签：Anki 模板字段仍是 Front/Back/Tags。"""
    from pathlib import Path

    source = Path(anki_export.__file__).read_text(encoding="utf-8")
    assert 'fields=[{"name": "Front"}, {"name": "Back"}, {"name": "Tags"}]' in source
    assert '<hr id="answer">' in source


