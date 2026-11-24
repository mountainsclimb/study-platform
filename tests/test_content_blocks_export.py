"""内容块在导出链路中的统一读取与渲染。"""
import io
import zipfile

import pytest

from app.content_blocks import (
    blocks_plain_text, blocks_to_html, content_blocks, table_rows,
)
from app.paperlab import exporters


def _blocks():
    return [
        {"kind": "text", "text": "已知函数 <script>alert(1)</script>"},
        {"kind": "math", "latex": "x^2+2x=0"},
        {"kind": "table", "table_html": (
            '<table><tr><td rowspan="2">A</td><td>1</td></tr>'
            '<tr><td onclick="x()">2</td></tr></table>'
        )},
        {"kind": "text", "text": "   "},
        {"kind": "math", "latex": ""},
    ]


def test_content_blocks_prefers_content_blocks_and_filters_empty():
    blocks = content_blocks({"content": {"blocks": _blocks()}})
    assert [block["kind"] for block in blocks] == ["text", "math", "table"]
    assert blocks[1]["latex"] == "x^2+2x=0"

    stem_blocks = content_blocks({"stem_blocks": [{"kind": "text", "text": "题干"}]})
    assert len(stem_blocks) == 1
    assert stem_blocks[0]["kind"] == "text"
    assert stem_blocks[0]["text"] == "题干"


def test_table_rows_parses_cells_and_ignores_attributes():
    rows = table_rows(
        '<table><tr><td rowspan="2">A</td><td>1</td></tr>'
        '<tr><td onclick="x()">2</td></tr></table>'
    )
    assert rows == [["A", "1"], ["2"]]


def test_blocks_to_html_escapes_text_and_sanitizes_tables():
    html = blocks_to_html(content_blocks({"blocks": _blocks()}))
    assert "&lt;script&gt;" in html
    assert "<script>" not in html
    assert "\\(x^2+2x=0\\)" in html
    assert "onclick" not in html
    assert "<table>" in html


def test_blocks_plain_text_joins_table_rows():
    text = blocks_plain_text(content_blocks({"blocks": _blocks()}))
    assert "A | 1" in text
    assert "$x^2+2x=0$" in text


def test_html_question_uses_structured_blocks():
    snapshot = {
        "content": {
            "stem": "旧题干",
            "blocks": [
                {"kind": "text", "text": "新题干"},
                {"kind": "math", "latex": "\\frac{1}{2}"},
            ],
        },
    }
    html = exporters._html_question({"snapshot": snapshot}, 1)
    assert "新题干" in html
    assert "\\(\\frac{1}{2}\\)" in html
    assert "旧题干" not in html


def test_build_docx_renders_structured_blocks():
    pytest.importorskip("docx")
    snapshot = {
        "type": "SUBJECTIVE",
        "content": {
            "stem": "旧题干",
            "blocks": [
                {"kind": "text", "text": "新题干"},
                {"kind": "math", "latex": "x^2"},
                {"kind": "table", "table_html": "<table><tr><td>1</td><td>2</td></tr></table>"},
            ],
        },
    }
    payload = exporters.build_docx(
        {"title": "块导出", "layout": {}},
        [{"snapshot": snapshot, "layout": {}}],
        include_answers=False,
    )
    assert payload[:2] == b"PK"
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        xml = archive.read("word/document.xml").decode("utf-8")
    assert "新题干" in xml
    assert "旧题干" not in xml
    assert "x" in xml and "2" in xml
