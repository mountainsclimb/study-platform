"""文本型 PDF 表格抽取（app/ai/pdf_tables.py）与知识清单接入验收。

覆盖方案 §2.3 的四组用例：
1. 关闭/未安装两条路径：``_extract_document_pages()`` 的分块与页状态与基线一致；
2. 打桩 ``extract_tables``：表格行追加到对应页末尾，块键集合不变；
3. 抽取抛错：文档解析不失败、不产生 500；
4. 真实 pdfplumber 抽取：仓库内动态生成的带表格线小样 PDF（``importorskip``）。
"""
from io import BytesIO

from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
import pytest

from app.ai import notes_agent, pdf_tables


@pytest.fixture(autouse=True)
def _reset_pdf_tables(monkeypatch):
    pdf_tables.reset_engine()
    for name in ("STUDY_PDF_TABLES", "STUDY_PDF_TABLE_PAGES"):
        monkeypatch.delenv(name, raising=False)
    yield
    pdf_tables.reset_engine()


def table_pdf_bytes(pages: int = 1) -> bytes:
    """生成带表格线的文本型 PDF（不依赖 reportlab；pypdf 直接写内容流）。"""
    writer = PdfWriter()
    for _ in range(pages):
        page = writer.add_blank_page(width=600, height=800)
        content = DecodedStreamObject()
        content.set_data(b"""
0.7 w
50 700 m 550 700 l S
50 660 m 550 660 l S
50 620 m 550 620 l S
50 700 m 50 620 l S
300 700 m 300 620 l S
550 700 m 550 620 l S
BT /F1 12 Tf 60 675 Td (Name) Tj ET
BT /F1 12 Tf 310 675 Td (Score) Tj ET
BT /F1 12 Tf 60 635 Td (Alice) Tj ET
BT /F1 12 Tf 310 635 Td (95) Tj ET
""")
        page[NameObject("/Contents")] = writer._add_object(content)
        font = DictionaryObject()
        font.update({
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        })
        resources = DictionaryObject()
        resources[NameObject("/Font")] = DictionaryObject({
            NameObject("/F1"): writer._add_object(font),
        })
        page[NameObject("/Resources")] = resources
    buffer = BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def plain_pdf_bytes() -> bytes:
    writer = PdfWriter()
    writer.add_blank_page(width=600, height=800)
    buffer = BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def extract(name="资料.pdf", raw=None):
    return notes_agent._extract_document_pages(name, raw if raw is not None else table_pdf_bytes())


def chunk_signature(chunks):
    return [
        (chunk["page"], chunk["chunk_id"], chunk["heading_path"], chunk["text"])
        for chunk in chunks
    ]


def test_module_status_off_and_uninstalled(monkeypatch):
    status = pdf_tables.engine_status()
    assert status["mode"] == "off"
    assert status["available"] is False
    assert status["loaded"] is False
    assert "已关闭" in status["notice"]
    assert pdf_tables.enabled() is False
    with pytest.raises(pdf_tables.PdfTablesUnavailable):
        pdf_tables.extract_tables(table_pdf_bytes())

    monkeypatch.setenv("STUDY_PDF_TABLES", "on")
    monkeypatch.setattr(pdf_tables, "_module_available", lambda: False)
    assert pdf_tables.enabled() is False
    status = pdf_tables.engine_status()
    assert status["mode"] == "on"
    assert status["available"] is False
    assert "pdfplumber" in status["notice"]


def test_off_and_uninstalled_paths_match_baseline(monkeypatch):
    baseline_chunks, baseline_pages, baseline_status, baseline_map = extract()

    # 关闭（默认）：表格不进入分块。
    assert all(" | " not in chunk["text"] for chunk in baseline_chunks)
    assert set(baseline_chunks[0]) == {"page", "chunk_id", "heading_path", "text"}

    # 开启但依赖不可用：与关闭逐项一致。
    monkeypatch.setenv("STUDY_PDF_TABLES", "on")
    monkeypatch.setattr(pdf_tables, "_module_available", lambda: False)
    uninstalled = extract()
    assert chunk_signature(uninstalled[0]) == chunk_signature(baseline_chunks)
    assert uninstalled[1] == baseline_pages
    assert uninstalled[2] == baseline_status
    assert set(uninstalled[3]) == set(baseline_map)


def test_stubbed_tables_are_appended_to_matching_page(monkeypatch):
    monkeypatch.setenv("STUDY_PDF_TABLES", "on")
    monkeypatch.setattr(pdf_tables, "_module_available", lambda: True)
    calls = {}

    def fake_extract(source, pages=None):
        calls["pages"] = pages
        return [
            {"page": 1, "rows": [["科目", "分数"], ["数学", "95"]]},
            {"page": 2, "rows": [["城市", "人口"]]},
        ]

    monkeypatch.setattr(pdf_tables, "extract_tables", fake_extract)
    chunks, pages, _status, _page_map = extract(raw=table_pdf_bytes(pages=2))

    assert calls["pages"] == [1, 2]
    assert pages == 2
    # 每一页的表格行只追加到对应页，块键集合不变。
    page_one = [chunk["text"] for chunk in chunks if chunk["page"] == 1]
    page_two = [chunk["text"] for chunk in chunks if chunk["page"] == 2]
    assert any("数学 | 95" in text for text in page_one)
    assert any("城市 | 人口" in text for text in page_two)
    assert all(set(chunk) == {"page", "chunk_id", "heading_path", "text"} for chunk in chunks)


def test_multi_page_tables_append_to_each_page(monkeypatch):
    monkeypatch.setenv("STUDY_PDF_TABLES", "on")
    monkeypatch.setattr(pdf_tables, "_module_available", lambda: True)
    monkeypatch.setattr(
        pdf_tables,
        "extract_tables",
        lambda source, pages=None: [
            {"page": 1, "rows": [["A", "B"], ["1", "2"]]},
            {"page": 2, "rows": [["C", "D"], ["3", "4"]]},
        ],
    )
    raw = table_pdf_bytes(pages=2)
    chunks, pages, _status, _page_map = notes_agent._extract_document_pages("两页.pdf", raw)

    assert pages == 2
    page_one = [chunk["text"] for chunk in chunks if chunk["page"] == 1]
    page_two = [chunk["text"] for chunk in chunks if chunk["page"] == 2]
    assert any("1 | 2" in text for text in page_one)
    assert any("3 | 4" in text for text in page_two)


def test_extraction_error_keeps_document_result(monkeypatch):
    baseline = extract()

    monkeypatch.setenv("STUDY_PDF_TABLES", "on")
    monkeypatch.setattr(pdf_tables, "_module_available", lambda: True)

    def broken(source, pages=None):
        raise pdf_tables.PdfTablesError("模拟抽取失败")

    monkeypatch.setattr(pdf_tables, "extract_tables", broken)
    result = extract()
    assert chunk_signature(result[0]) == chunk_signature(baseline[0])
    assert result[1] == baseline[1]
    assert result[2] == baseline[2]


def test_max_pages_env_is_bounded(monkeypatch):
    assert pdf_tables.max_pages() == 50
    monkeypatch.setenv("STUDY_PDF_TABLE_PAGES", "0")
    assert pdf_tables.max_pages() == 1
    monkeypatch.setenv("STUDY_PDF_TABLE_PAGES", "9999")
    assert pdf_tables.max_pages() == 200
    monkeypatch.setenv("STUDY_PDF_TABLE_PAGES", "bad")
    assert pdf_tables.max_pages() == 50


def test_real_pdfplumber_extracts_table_rows(monkeypatch):
    pytest.importorskip("pdfplumber")
    monkeypatch.setenv("STUDY_PDF_TABLES", "on")
    monkeypatch.setattr(pdf_tables, "_module_available", lambda: True)
    tables = pdf_tables.extract_tables(table_pdf_bytes())
    assert tables
    first = tables[0]
    assert first["page"] == 1
    assert ["Name", "Score"] in first["rows"]
    assert ["Alice", "95"] in first["rows"]

    # 走完整解析：表格行以 " | " 进入分块文本。
    monkeypatch.setattr(pdf_tables, "enabled", lambda: True)
    chunks, pages, _status, _page_map = notes_agent._extract_document_pages(
        "表格.pdf", table_pdf_bytes()
    )
    assert pages == 1
    assert any("Name | Score" in chunk["text"] for chunk in chunks)
    assert any("Alice | 95" in chunk["text"] for chunk in chunks)


def test_real_pdf_without_tables_returns_empty(monkeypatch):
    pytest.importorskip("pdfplumber")
    monkeypatch.setenv("STUDY_PDF_TABLES", "on")
    monkeypatch.setattr(pdf_tables, "_module_available", lambda: True)
    assert pdf_tables.extract_tables(plain_pdf_bytes()) == []


def test_engine_initialization_failure_is_cached_and_resettable(monkeypatch):
    monkeypatch.setenv("STUDY_PDF_TABLES", "on")
    monkeypatch.setattr(pdf_tables, "_module_available", lambda: True)

    import builtins

    real_import = builtins.__import__

    def broken_import(name, *args, **kwargs):
        if name == "pdfplumber":
            raise ImportError("模拟 pdfplumber 初始化失败")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", broken_import)
    with pytest.raises(pdf_tables.PdfTablesUnavailable):
        pdf_tables.extract_tables(table_pdf_bytes())
    assert pdf_tables.extract_tables.last_elapsed_ms >= 0
    with pytest.raises(pdf_tables.PdfTablesUnavailable) as cached:
        pdf_tables.extract_tables(table_pdf_bytes())
    assert "依赖不可用" in str(cached.value) and "ImportError" in str(cached.value)

    # 重置后可重试；恢复真实导入后应成功。
    monkeypatch.undo()
    monkeypatch.setenv("STUDY_PDF_TABLES", "on")
    monkeypatch.setattr(pdf_tables, "_module_available", lambda: True)
    pdf_tables.reset_engine()
    assert pdf_tables.extract_tables(table_pdf_bytes())
