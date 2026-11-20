"""测试用最小只读题库构造器：不触碰真实发布包，用于鉴权/检索索引等回归。

真实 R3 题库有 7,659 行宽数据，单测里复制太慢；这里按 ``_bank_question_columns``
需要的最小列集合建一个临时 ``题库.db``，通过 monkeypatch ``db.BANK_DIR`` 指向它。
"""
from io import BytesIO
from pathlib import Path
import sqlite3

from PIL import Image, ImageDraw

QUESTION_COLUMNS = (
    "qid", "doc_id", "group_id", "subject", "year", "province", "paper_name",
    "paper_type", "type", "objective", "number_label", "group_role",
    "order_in_paper", "stem_plain", "options_json", "images_json", "answer_text",
    "answer_kind", "answer_note", "solution", "solution_blocks_json",
    "stem_blocks_json", "options_blocks_json", "answer_blocks_json",
    "shared_blocks_json", "member_index_json", "quality_flags_json",
    "practice_status", "grading_status", "bank_version", "has_figure",
)
GROUP_COLUMNS = (
    "group_id", "doc_id", "subject", "year", "material_text", "parent_stem",
    "shared_blocks_json", "member_index_json", "material_kind", "section_title",
    "n_members", "n_practice", "bank_version",
)


def question_row(qid: str, stem: str, *, doc_id: str = "docA", **values) -> dict:
    row = {column: None for column in QUESTION_COLUMNS}
    row.update({
        "qid": qid, "doc_id": doc_id, "group_id": None, "subject": "数学",
        "year": 2024, "province": "全国", "paper_name": "测试卷",
        "paper_type": "模拟", "type": "single_choice", "objective": 1,
        "number_label": "1", "group_role": None, "order_in_paper": 1,
        "stem_plain": stem, "options_json": '[{"label":"A","text":"选项A"}]',
        "images_json": "[]", "answer_text": "A", "answer_kind": "choice",
        "answer_note": None, "solution": "", "solution_blocks_json": "[]",
        "stem_blocks_json": "[]", "options_blocks_json": "[]",
        "answer_blocks_json": "[]", "shared_blocks_json": "[]",
        "member_index_json": "[]", "quality_flags_json": "[]",
        "practice_status": "practice", "grading_status": "auto",
        "bank_version": "bank-fixture-r1", "has_figure": 0,
    })
    row.update(values)
    return row


def make_bank(root: Path, rows, groups=()) -> Path:
    """在 ``root/题库.db`` 建最小题库；返回可给 ``db.BANK_DIR`` 的目录。"""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(root / "题库.db"))
    try:
        con.execute(
            "CREATE TABLE questions ("
            + ", ".join(f'"{column}" TEXT' for column in QUESTION_COLUMNS)
            + ", PRIMARY KEY(qid))"
        )
        con.execute(
            "CREATE TABLE groups ("
            + ", ".join(f'"{column}" TEXT' for column in GROUP_COLUMNS)
            + ", PRIMARY KEY(group_id))"
        )
        for row in rows:
            values = {column: row.get(column) for column in QUESTION_COLUMNS}
            con.execute(
                f"INSERT INTO questions({', '.join(QUESTION_COLUMNS)}) "
                f"VALUES ({', '.join('?' for _ in QUESTION_COLUMNS)})",
                list(values.values()),
            )
        for row in groups:
            values = {column: row.get(column) for column in GROUP_COLUMNS}
            con.execute(
                f"INSERT INTO groups({', '.join(GROUP_COLUMNS)}) "
                f"VALUES ({', '.join('?' for _ in GROUP_COLUMNS)})",
                list(values.values()),
            )
        con.commit()
    finally:
        con.close()
    return root


def drawn_image_bytes(size=(240, 240), color="black") -> bytes:
    image = Image.new("RGB", size, "white")
    draw = ImageDraw.Draw(image)
    draw.line((10, 10, size[0] - 10, size[1] - 10), fill=color, width=4)
    draw.rectangle((20, 20, size[0] - 20, size[1] - 20), outline="blue", width=3)
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def blank_image_bytes(size=(240, 240)) -> bytes:
    buffer = BytesIO()
    Image.new("RGB", size, "white").save(buffer, format="PNG")
    return buffer.getvalue()
