"""公式识别后处理（app/ai/formula_ocr.py + ocr.enrich_blocks）验收。

全程假引擎，不下载/加载 ONNX 模型。覆盖方案 §3.1：
* 未安装 / 关闭两条路径：``recognize_image(include_blocks=True)`` 与基线一致；
* 假引擎返回 LaTeX：公式块被填充、``needs_review`` 仍为 True、非公式块与顺序不变；
* 引擎抛错 / 超时：块原样，``enrichment`` 写明原因，不产生 500；
* 块数超过上限：超出的块原样；
* 拍题路径：``create_ocr_draft`` 的草稿块带 latex；
* 试卷工坊：``pl_pages.ocr_blocks_json`` 的规整与 ``has_blocks`` 上报。
"""
import asyncio
from io import BytesIO
import json
import time

from PIL import Image
import pytest

from app.ai import formula_ocr, ocr, upload_pipeline
from app.paperlab import db as paperlab_db, digitize


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    ocr.reset_engine()
    formula_ocr.reset_engine()
    for name in (
        "STUDY_LOCAL_OCR", "STUDY_FORMULA_OCR", "STUDY_FORMULA_TIMEOUT",
        "STUDY_FORMULA_MAX_BLOCKS", "STUDY_TABLE_OCR",
    ):
        monkeypatch.delenv(name, raising=False)
    yield
    ocr.reset_engine()
    formula_ocr.reset_engine()


def png_bytes(width=400, height=300) -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (width, height), "white").save(buffer, "PNG")
    return buffer.getvalue()


def line(text, *, x=20, top=10, width=180, height=22, score=0.95):
    return {
        "text": text,
        "box": [[x, top], [x + width, top], [x + width, top + height], [x, top + height]],
        "score": score,
    }


def sample_lines():
    return [
        line("光合作用", x=20, top=10, width=140, score=0.98),
        line("x^2 + 2x = 0", x=20, top=40, width=200, score=0.91),
        line("姓名 | 分数", x=20, top=90, width=280, score=0.93),
        line("张三 | 90", x=20, top=120, width=280, score=0.90),
        line("二氧化碳", x=20, top=160, width=140, score=0.97),
    ]


class FakeLatexEngine:
    def __init__(self, latex="\\frac{1}{2}", *, delay=0.0, error=None):
        self.latex = latex
        self.delay = delay
        self.error = error
        self.calls = 0
        self.shapes = []

    def __call__(self, image):
        self.calls += 1
        self.shapes.append(getattr(image, "shape", None))
        if self.delay:
            time.sleep(self.delay)
        if self.error:
            raise RuntimeError(self.error)
        return (self.latex, 0.99)


def install_local_ocr(monkeypatch, lines=None):
    """替换 ``ocr._infer_guarded``：直接返回行字典，不经过 RapidOCR 原始元组格式。"""
    payload = sample_lines() if lines is None else lines

    def fake_infer(bgr):
        return [dict(item) for item in payload]

    monkeypatch.setattr(ocr, "_module_available", lambda: True)
    monkeypatch.setattr(ocr, "_infer_guarded", fake_infer)
    return payload


def install_formula(monkeypatch, engine=None):
    engine = engine or FakeLatexEngine()
    monkeypatch.setenv("STUDY_FORMULA_OCR", "on")
    monkeypatch.setattr(formula_ocr, "_module_available", lambda: True)
    monkeypatch.setattr(formula_ocr, "_get_engine", lambda: engine)
    return engine


def recognize(monkeypatch, *, include_blocks=True):
    install_local_ocr(monkeypatch)
    return asyncio.run(ocr.recognize_image(png_bytes(), include_blocks=include_blocks))


def test_off_and_uninstalled_paths_match_baseline(monkeypatch):
    baseline = recognize(monkeypatch)
    assert baseline["enrichment"] == {"formula": "off", "table": "off"}
    math_blocks = [block for block in baseline["blocks"] if block["kind"] == "math"]
    assert math_blocks and all(block["latex"] is None for block in math_blocks)

    monkeypatch.setenv("STUDY_FORMULA_OCR", "on")
    monkeypatch.setattr(formula_ocr, "_module_available", lambda: False)
    uninstalled = recognize(monkeypatch)
    assert uninstalled["enrichment"]["formula"] == "unavailable"
    assert [block["text"] for block in uninstalled["blocks"]] == [
        block["text"] for block in baseline["blocks"]
    ]
    assert [block["kind"] for block in uninstalled["blocks"]] == [
        block["kind"] for block in baseline["blocks"]
    ]
    assert all(block["latex"] is None for block in uninstalled["blocks"])


def test_fake_engine_fills_formula_latex_and_keeps_review_flag(monkeypatch):
    engine = install_formula(monkeypatch)
    result = recognize(monkeypatch)

    blocks = result["blocks"]
    math_blocks = [block for block in blocks if block["kind"] == "math"]
    assert len(math_blocks) == 1
    assert math_blocks[0]["latex"] == "\\frac{1}{2}"
    assert math_blocks[0]["latex_engine"] == formula_ocr.ENGINE_NAME
    assert math_blocks[0]["needs_review"] is True
    assert [block["order"] for block in blocks] == list(range(5))
    assert all("latex" not in block or block.get("latex") is None
               for block in blocks if block["kind"] != "math")
    assert engine.calls == 1
    assert result["enrichment"]["formula"] == "on"
    assert "formula_error" not in result["enrichment"]


def test_formula_engine_error_keeps_blocks_and_reports_reason(monkeypatch):
    install_formula(monkeypatch, FakeLatexEngine(error="模拟失败"))
    result = recognize(monkeypatch)
    math_block = next(block for block in result["blocks"] if block["kind"] == "math")
    assert math_block["latex"] is None
    assert math_block["needs_review"] is True
    assert "formula_error" in result["enrichment"]
    assert "FormulaOcrError" in result["enrichment"]["formula_error"]


def test_formula_timeout_keeps_blocks(monkeypatch):
    monkeypatch.setattr(formula_ocr, "timeout", lambda: 0.1)
    install_formula(monkeypatch, FakeLatexEngine(delay=0.4))
    result = recognize(monkeypatch)
    math_block = next(block for block in result["blocks"] if block["kind"] == "math")
    assert math_block["latex"] is None
    assert result["enrichment"]["formula_error"] == "timeout"


def test_formula_max_blocks_skips_extra_blocks(monkeypatch):
    monkeypatch.setenv("STUDY_FORMULA_MAX_BLOCKS", "1")
    engine = install_formula(monkeypatch)
    install_local_ocr(monkeypatch, [
        line("a = 1", x=20, top=10),
        line("b = 2", x=20, top=50),
        line("c = 3", x=20, top=90),
    ])
    result = asyncio.run(ocr.recognize_image(png_bytes(), include_blocks=True))
    math_blocks = [block for block in result["blocks"] if block["kind"] == "math"]
    assert len(math_blocks) == 3
    assert math_blocks[0]["latex"] == "\\frac{1}{2}"
    assert math_blocks[1]["latex"] is None and math_blocks[2]["latex"] is None
    assert result["enrichment"]["formula_skipped"] == 2
    assert engine.calls == 1


def test_enrichment_image_applies_exif_orientation_before_crop_coordinates():
    """原始上传图带 EXIF 旋转时，后处理裁图坐标必须与 OCR 预处理图一致。"""
    image = Image.new("RGB", (400, 200), "white")
    exif = Image.Exif()
    exif[274] = 6  # 顺时针旋转 90°
    buffer = BytesIO()
    image.save(buffer, "JPEG", exif=exif)
    prepared = ocr._enrichment_pil(buffer.getvalue(), width=200, height=400)
    assert prepared.size == (200, 400)


def test_many_blocks_do_not_timeout_while_queued(monkeypatch):
    """超时从拿到推理槽位时开始：8 个块、并发 2、单块 0.2s，不应因排队误判。"""
    monkeypatch.setattr(formula_ocr, "timeout", lambda: 0.5)
    monkeypatch.setenv("STUDY_FORMULA_MAX_BLOCKS", "8")
    engine = install_formula(monkeypatch, FakeLatexEngine(delay=0.2))
    lines = [line(f"x = {index}", x=20, top=10 + index * 30) for index in range(8)]
    install_local_ocr(monkeypatch, lines)
    result = asyncio.run(ocr.recognize_image(png_bytes(400, 400), include_blocks=True))
    math_blocks = [block for block in result["blocks"] if block["kind"] == "math"]
    assert len(math_blocks) == 8
    assert all(block["latex"] == "\\frac{1}{2}" for block in math_blocks)
    assert "formula_error" not in result["enrichment"]
    assert engine.calls == 8


def test_recognize_crop_limits_long_side_and_returns_latex(monkeypatch):
    engine = install_formula(monkeypatch)
    crop = Image.new("RGB", (3000, 500), "white")
    outcome = formula_ocr.recognize_crop(crop)
    assert outcome["latex"] == "\\frac{1}{2}"
    assert outcome["elapsed_ms"] >= 0
    assert max(engine.shapes[0][:2]) <= formula_ocr.MAX_CROP_SIDE


def test_engine_status_and_reset(monkeypatch):
    status = formula_ocr.engine_status()
    assert status["mode"] == "off" and status["available"] is False
    assert status["limits"]["max_blocks"] == 20
    monkeypatch.setenv("STUDY_FORMULA_OCR", "on")
    monkeypatch.setattr(formula_ocr, "_module_available", lambda: False)
    status = formula_ocr.engine_status()
    assert status["available"] is False and "rapid-latex-ocr" in status["notice"]
    assert ocr.enrichment_status()["formula"] == "unavailable"


def test_upload_draft_carries_formula_latex(client, monkeypatch):
    install_local_ocr(monkeypatch)
    install_formula(monkeypatch)
    response = client.post("/api/uploads/question", files={
        "file": ("拍题.png", png_bytes(), "image/png"),
    })
    assert response.status_code == 200, response.text
    upload_id = response.json()["id"]

    draft = client.post(f"/api/uploads/{upload_id}/ocr-draft")
    assert draft.status_code == 200, draft.text
    payload = draft.json()
    math_blocks = [block for block in payload["blocks"] if block["kind"] == "math"]
    assert math_blocks and math_blocks[0]["latex"] == "\\frac{1}{2}"
    assert math_blocks[0]["needs_review"] is True


def test_surya_reserved_marker_is_removed():
    assert "pix2text_adapter" not in ocr.CAPABILITIES
    assert ocr.CAPABILITIES == {"blocks": True, "math_recognition": False}


def test_paperlab_normalizes_blocks_and_reports_has_blocks():
    normalized = digitize._normalize_ocr_result({
        "text": "x^2 = 1",
        "lines": [],
        "width": 400,
        "height": 300,
        "blocks": [{
            "id": "b1", "kind": "math", "text": "x^2 = 1",
            "latex": "x^2=1", "needs_review": True, "order": 0, "page": 1,
        }],
        "engine": "rapidocr-onnxruntime",
    })
    assert normalized["blocks"][0]["latex"] == "x^2=1"
    page = paperlab_db.public_page({
        "page_no": 1,
        "ocr_text": "x^2 = 1",
        "ocr_lines_json": "[]",
        "ocr_blocks_json": json.dumps(normalized["blocks"], ensure_ascii=False),
        "ocr_engine": "rapidocr-onnxruntime",
    }, import_id=7)
    assert page["has_blocks"] is True
    assert page["ocr_block_count"] == 1

    empty = paperlab_db.public_page({"page_no": 1}, import_id=7)
    assert "has_blocks" not in empty


def test_validate_confirm_blocks_drops_invalid_table_html():
    notices = []
    blocks = upload_pipeline._validate_confirm_blocks([
        {
            "kind": "table",
            "text": "A | B",
            "table_html": '<table><tr><td onclick="x()">A</td></tr></table>',
            "row_count": 1,
            "col_count": 1,
        },
        {
            "kind": "table",
            "text": "坏结构",
            "table_html": "<script>alert(1)</script>",
            "row_count": 999,
        },
    ], notices)
    assert "onclick" not in blocks[0]["table_html"]
    assert '<table><tr><td>A</td></tr></table>' == blocks[0]["table_html"]
    assert blocks[0]["row_count"] == 1 and blocks[0]["col_count"] == 1
    assert "table_html" not in blocks[1]
    assert "row_count" not in blocks[1]
    assert len(notices) >= 2
