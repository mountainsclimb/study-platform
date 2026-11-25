"""表格结构识别与清洗（app/ai/table_structure.py + ocr.enrich_blocks）验收。

覆盖方案 §3.2：
* 未安装 / 关闭：块原样、不调用引擎；
* 连续表格块聚组、只写组内第一块、``table_span`` 正确；
* 清洗器剥离 script/事件属性/style/白名单外标签，保留 rowspan/colspan；
* ``table_html`` 超长被拒；
* 扫描页白名单保留 ``table_html`` / ``row_count`` / ``col_count``；
* 确认接口对非法 ``table_html`` 的处理（见 tests/test_formula_ocr.py）。
"""
import asyncio
from io import BytesIO

from PIL import Image
import pytest

from app.ai import notes_agent, ocr, table_structure


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    table_structure.reset_engine()
    for name in ("STUDY_TABLE_OCR", "STUDY_TABLE_TIMEOUT", "STUDY_FORMULA_OCR"):
        monkeypatch.delenv(name, raising=False)
    yield
    table_structure.reset_engine()


class FakeOutput:
    def __init__(self, html):
        self.pred_htmls = [html]


class FakeTableEngine:
    def __init__(self, html=None, *, delay=0.0, error=None):
        self.html = html or "<table><thead><tr><th>A</th><th>B</th></tr></thead><tbody><tr><td>1</td><td>2</td></tr></tbody></table>"
        self.delay = delay
        self.error = error
        self.calls = 0
        self.ocr_results = None

    def __call__(self, images, ocr_results=None):
        self.calls += 1
        self.ocr_results = ocr_results
        if self.delay:
            import time
            time.sleep(self.delay)
        if self.error:
            raise RuntimeError(self.error)
        return FakeOutput(self.html)


def install_table(monkeypatch, engine=None):
    engine = engine or FakeTableEngine()
    monkeypatch.setenv("STUDY_TABLE_OCR", "on")
    monkeypatch.setattr(table_structure, "_module_available", lambda: True)
    monkeypatch.setattr(table_structure, "_get_engine", lambda: engine)
    return engine


def image() -> Image.Image:
    return Image.new("RGB", (400, 300), "white")


def table_block(text, *, top, height=20, x=10, width=200, order=0):
    return {
        "id": f"b{order + 1}",
        "kind": "table",
        "text": text,
        "latex": None,
        "needs_review": False,
        "confidence": 0.9,
        "bbox": {"x": x, "y": top, "w": width, "h": height},
        "norm": {"x": 0.025, "y": top / 300, "w": 0.5, "h": height / 300},
        "page": 1,
        "order": order,
        "rotation": 0,
        "source": "rapidocr",
    }


def test_off_and_uninstalled_paths_keep_blocks_unchanged(monkeypatch):
    blocks = [table_block("A | B", top=10, order=0), table_block("1 | 2", top=40, order=1)]
    original = [dict(block) for block in blocks]

    outcome = asyncio.run(ocr.enrich_blocks(blocks, image(), page=1))
    assert outcome["enrichment"]["table"] == "off"
    assert outcome["blocks"] == original
    assert all("table_html" not in block for block in outcome["blocks"])

    monkeypatch.setenv("STUDY_TABLE_OCR", "on")
    monkeypatch.setattr(table_structure, "_module_available", lambda: False)
    outcome = asyncio.run(ocr.enrich_blocks(blocks, image(), page=1))
    assert outcome["enrichment"]["table"] == "unavailable"
    assert outcome["blocks"] == original


def test_sanitize_table_html_keeps_white_list_and_spans():
    raw = (
        '<table style="color:red" onclick="bad()">'
        '<thead><tr><th colspan="2" rowspan="1" class="x">头</th></tr></thead>'
        '<tbody><tr><td onclick="steal()">A</td><td><strong>B</strong></td></tr></tbody>'
        '</table><script>alert(1)</script><iframe src="x"></iframe>'
    )
    cleaned = table_structure.sanitize_table_html(raw)
    assert cleaned.startswith("<table>")
    assert 'colspan="2"' in cleaned and 'rowspan="1"' in cleaned
    assert "onclick" not in cleaned and "style=" not in cleaned and "class=" not in cleaned
    assert "<strong>" not in cleaned and "<script" not in cleaned and "<iframe" not in cleaned
    assert "alert(1)" not in cleaned
    assert "头" in cleaned and "A" in cleaned and "B" in cleaned
    # 标签外的文本被丢弃（只保留合法表格结构）。
    assert table_structure.sanitize_table_html("普通文本") == ""
    assert table_structure.sanitize_table_html("<script>x</script>") == ""


def test_sanitize_rejects_overlong_html():
    raw = "<table><tr><td>" + "A" * table_structure.MAX_TABLE_HTML_CHARS + "</td></tr></table>"
    with pytest.raises(ValueError):
        table_structure.sanitize_table_html(raw)


def test_html_shape_counts_rows_and_colspan():
    rows, columns = table_structure.html_shape(
        '<table><tr><td colspan="2">A</td><td>B</td></tr><tr><td>1</td><td>2</td><td>3</td></tr></table>'
    )
    assert rows == 2 and columns == 3


def test_recognize_table_returns_clean_html_and_shape(monkeypatch):
    engine = install_table(monkeypatch)
    boxes = [[[0, 0], [10, 0], [10, 10], [0, 10]]]
    outcome = table_structure.recognize_table(
        image(), ocr_results=(boxes, ["A"], [0.9]),
    )
    assert outcome["row_count"] == 2
    assert outcome["col_count"] == 2
    assert outcome["html"].startswith("<table>")
    assert engine.calls == 1
    # 调用方已有的 OCR 结果被透传，避免重复跑 OCR。
    assert engine.ocr_results and engine.ocr_results[0][1] == ["A"]


def test_recognize_table_without_bundled_ocr_explains_missing_rapidocr(monkeypatch):
    """rapid-table 缺 rapidocr 时会把 ocr_engine 置 None 并抛 TypeError：

    我们要给出「缺文字检测」的真实原因，而不是误导性的「识别失败」。
    """
    engine = install_table(monkeypatch)
    monkeypatch.setattr(table_structure, "_has_bundled_ocr", lambda: False)
    with pytest.raises(table_structure.TableOcrError) as caught:
        table_structure.recognize_table(image())
    assert "rapidocr" in str(caught.value)
    assert "ocr_results" in str(caught.value)
    # 引擎没有被调用（问题在文字检测，不在表格结构）。
    assert engine.calls == 0
    # 传入调用方自己的 OCR 行结果时不需要 rapidocr，照常识别。
    monkeypatch.setattr(table_structure, "_get_engine", lambda: engine)
    boxes = [[[0, 0], [10, 0], [10, 10], [0, 10]]]
    outcome = table_structure.recognize_table(image(), ocr_results=(boxes, ["A"], [0.9]))
    assert outcome["html"].startswith("<table>")
    assert engine.calls == 1


def test_get_engine_disables_internal_ocr_when_rapidocr_missing(monkeypatch):
    """缺 rapidocr 时用 use_ocr=False 建引擎，避免上游静默返回 None。"""
    import sys
    import types

    captured = {}

    class Recorder:
        def __init__(self, config):
            captured["use_ocr"] = config.use_ocr
            self.config = config

    class RecorderInput:
        def __init__(self, **kwargs):
            self.use_ocr = kwargs.get("use_ocr")

    stub = types.ModuleType("rapid_table")
    stub.RapidTable = Recorder
    stub.RapidTableInput = RecorderInput
    monkeypatch.setitem(sys.modules, "rapid_table", stub)
    monkeypatch.setattr(table_structure, "_has_bundled_ocr", lambda: False)
    table_structure.reset_engine()
    table_structure._get_engine()
    assert captured.get("use_ocr") is False
    table_structure.reset_engine()
    monkeypatch.setattr(table_structure, "_has_bundled_ocr", lambda: True)
    table_structure._get_engine()
    assert captured.get("use_ocr") is True
    table_structure.reset_engine()


def test_enrich_groups_consecutive_table_blocks(monkeypatch):
    engine = install_table(monkeypatch)
    blocks = [
        table_block("A | B", top=10, order=0),
        table_block("1 | 2", top=40, order=1),
        {
            "id": "b3", "kind": "text", "text": "说明", "latex": None,
            "needs_review": False, "confidence": 0.9,
            "bbox": {"x": 10, "y": 100, "w": 100, "h": 20},
            "norm": {}, "page": 1, "order": 2, "rotation": 0, "source": "rapidocr",
        },
    ]
    original = [dict(block) for block in blocks]

    outcome = asyncio.run(ocr.enrich_blocks(blocks, image(), page=1))
    result = outcome["blocks"]
    assert blocks == original, "不得原地修改调用方的块列表"
    assert result[0]["table_span"] == 2
    assert result[0]["table_engine"] == table_structure.ENGINE_NAME
    assert result[0]["table_html"].startswith("<table>")
    assert result[0]["row_count"] == 2 and result[0]["col_count"] == 2
    assert "table_html" not in result[1]
    assert result[2] == original[2]
    assert engine.calls == 1
    # OCR 行框按裁图原点换算（组外接框 10,10，短边补白 2px → 裁图原点 8,8）。
    boxes = engine.ocr_results[0][0]
    assert boxes[0][0] == [2.0, 2.0]


def test_enrich_table_error_keeps_blocks(monkeypatch):
    install_table(monkeypatch, FakeTableEngine(error="模拟失败"))
    blocks = [table_block("A | B", top=10, order=0), table_block("1 | 2", top=40, order=1)]
    original = [dict(block) for block in blocks]
    outcome = asyncio.run(ocr.enrich_blocks(blocks, image(), page=1))
    assert outcome["blocks"] == original
    assert "table_error" in outcome["enrichment"]
    assert "TableOcrError" in outcome["enrichment"]["table_error"]


def test_table_timeout_keeps_blocks(monkeypatch):
    monkeypatch.setattr(table_structure, "timeout", lambda: 0.1)
    install_table(monkeypatch, FakeTableEngine(delay=0.4))
    blocks = [table_block("A | B", top=10, order=0), table_block("1 | 2", top=40, order=1)]
    outcome = asyncio.run(ocr.enrich_blocks(blocks, image(), page=1))
    assert all("table_html" not in block for block in outcome["blocks"])
    assert outcome["enrichment"]["table_error"] == "timeout"


def test_notes_agent_whitelist_keeps_sanitized_table_fields():
    blocks = notes_agent._normalize_ocr_blocks([{
        "id": "b1",
        "kind": "table",
        "text": "A | B",
        "order": 0,
        "page": 1,
        "table_html": '<table><tr><td onclick="x()">A</td></tr></table><script>bad()</script>',
        "row_count": 1,
        "col_count": 1,
    }])
    assert len(blocks) == 1
    block = blocks[0]
    assert block["table_html"] == "<table><tr><td>A</td></tr></table>"
    assert block["row_count"] == 1 and block["col_count"] == 1
    assert "script" not in block["table_html"]


def test_notes_agent_drops_invalid_table_html():
    blocks = notes_agent._normalize_ocr_blocks([{
        "id": "b1", "kind": "table", "text": "A | B", "order": 0, "page": 1,
        "table_html": "<div>not a table</div>", "row_count": 1, "col_count": 1,
    }])
    assert "table_html" not in blocks[0]
    assert "row_count" not in blocks[0]


def test_engine_status_reports_off_and_unavailable(monkeypatch):
    status = table_structure.engine_status()
    assert status["mode"] == "off" and status["available"] is False
    monkeypatch.setenv("STUDY_TABLE_OCR", "on")
    monkeypatch.setattr(table_structure, "_module_available", lambda: False)
    status = table_structure.engine_status()
    assert status["available"] is False and "rapid-table" in status["notice"]
    assert ocr.enrichment_status()["table"] == "unavailable"
