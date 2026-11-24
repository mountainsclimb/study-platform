"""内容块后处理总预算、缓存与熔断（app/ai/ocr.py 的 enrich_blocks）。"""
import asyncio
import time

from PIL import Image
import pytest

from app.ai import formula_ocr, ocr, table_structure


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    ocr.reset_engine()
    formula_ocr.reset_engine()
    table_structure.reset_engine()
    for name in (
        "STUDY_ENRICH_TIMEOUT", "STUDY_FORMULA_OCR", "STUDY_TABLE_OCR",
        "STUDY_FORMULA_TIMEOUT", "STUDY_TABLE_TIMEOUT",
    ):
        monkeypatch.delenv(name, raising=False)
    yield
    ocr.reset_engine()
    formula_ocr.reset_engine()
    table_structure.reset_engine()


class FakeLatexEngine:
    def __init__(self, *, delay: float = 0.0):
        self.delay = delay
        self.calls = 0

    def __call__(self, image):
        self.calls += 1
        if self.delay:
            time.sleep(self.delay)
        return ("\\frac{1}{2}", 0.99)


class FakeTableEngine:
    def __init__(self, *, delay: float = 0.0):
        self.delay = delay
        self.calls = 0

    def __call__(self, images, ocr_results=None):
        self.calls += 1
        if self.delay:
            time.sleep(self.delay)
        return type("Outcome", (), {
            "pred_htmls": ["<table><tr><td>1</td></tr></table>"],
        })()


def install_formula(monkeypatch, engine):
    monkeypatch.setenv("STUDY_FORMULA_OCR", "on")
    monkeypatch.setattr(formula_ocr, "_module_available", lambda: True)
    monkeypatch.setattr(formula_ocr, "_get_engine", lambda: engine)


def install_table(monkeypatch, engine):
    monkeypatch.setenv("STUDY_TABLE_OCR", "on")
    monkeypatch.setattr(table_structure, "_module_available", lambda: True)
    monkeypatch.setattr(table_structure, "_get_engine", lambda: engine)


def block(kind: str, *, text: str, top: int, order: int = 0) -> dict:
    return {
        "id": f"b{order + 1}",
        "kind": kind,
        "text": text,
        "latex": None,
        "needs_review": kind == "math",
        "confidence": 0.9,
        "bbox": {"x": 10, "y": top, "w": 220, "h": 24},
        "norm": {"x": 0.025, "y": top / 300, "w": 0.55, "h": 0.08},
        "page": 1,
        "order": order,
        "rotation": 0,
        "source": "rapidocr",
    }


def white_image() -> Image.Image:
    return Image.new("RGB", (400, 300), "white")


def test_formula_budget_exhaustion_is_reported(monkeypatch):
    install_formula(monkeypatch, FakeLatexEngine(delay=0.5))
    monkeypatch.setattr(formula_ocr, "timeout", lambda: 5.0)
    monkeypatch.setenv("STUDY_ENRICH_TIMEOUT", "0.2")

    outcome = asyncio.run(ocr.enrich_blocks(
        [block("math", text="x = 1", top=10)], white_image(),
    ))
    enrichment = outcome["enrichment"]
    assert enrichment["budget_exhausted"] == "formula"
    assert outcome["blocks"][0]["latex"] is None
    assert "formula_error" not in enrichment


def test_table_budget_exhaustion_is_reported(monkeypatch):
    install_table(monkeypatch, FakeTableEngine(delay=0.5))
    monkeypatch.setattr(table_structure, "timeout", lambda: 5.0)
    monkeypatch.setenv("STUDY_ENRICH_TIMEOUT", "0.2")

    outcome = asyncio.run(ocr.enrich_blocks(
        [
            block("table", text="A | B", top=10, order=0),
            block("table", text="1 | 2", top=40, order=1),
        ],
        white_image(),
    ))
    assert outcome["enrichment"]["budget_exhausted"] == "table"
    assert "table_html" not in outcome["blocks"][0]


def test_enrichment_cache_reuses_successful_result(monkeypatch):
    engine = FakeLatexEngine()
    install_formula(monkeypatch, engine)
    image = white_image()

    first = asyncio.run(ocr.enrich_blocks(
        [block("math", text="x = 1", top=10)], image,
    ))
    assert engine.calls == 1
    assert first["blocks"][0]["latex"] == "\\frac{1}{2}"

    second_blocks = [block("math", text="x = 1", top=10)]
    second = asyncio.run(ocr.enrich_blocks(second_blocks, image))
    assert second["enrichment"].get("cache") == "hit"
    assert engine.calls == 1
    assert second["blocks"][0]["latex"] == "\\frac{1}{2}"


def test_reset_engine_clears_enrichment_cache(monkeypatch):
    engine = FakeLatexEngine()
    install_formula(monkeypatch, engine)
    image = white_image()

    asyncio.run(ocr.enrich_blocks([block("math", text="x = 1", top=10)], image))
    ocr.reset_engine()
    asyncio.run(ocr.enrich_blocks([block("math", text="x = 1", top=10)], image))
    assert engine.calls == 2


def test_consecutive_timeouts_open_circuit(monkeypatch):
    monkeypatch.setattr(ocr, "ENRICH_ABANDON_LIMIT", 1)
    monkeypatch.setattr(ocr, "ENRICH_CIRCUIT_SECONDS", 30.0)
    install_formula(monkeypatch, FakeLatexEngine(delay=0.3))
    monkeypatch.setattr(formula_ocr, "timeout", lambda: 0.05)

    first = asyncio.run(ocr.enrich_blocks(
        [block("math", text="x = 1", top=10)], white_image(),
    ))
    assert first["enrichment"].get("formula_error") == "timeout"

    second = asyncio.run(ocr.enrich_blocks(
        [block("math", text="x = 1", top=10)], white_image(),
    ))
    assert second["enrichment"].get("circuit_open") is True


def test_enrich_timeout_env_is_bounded(monkeypatch):
    monkeypatch.setenv("STUDY_ENRICH_TIMEOUT", "0")
    assert ocr.enrich_timeout() == 0.1
    monkeypatch.setenv("STUDY_ENRICH_TIMEOUT", "9999")
    assert ocr.enrich_timeout() == 120.0
    monkeypatch.setenv("STUDY_ENRICH_TIMEOUT", "not-a-number")
    assert ocr.enrich_timeout() == ocr.DEFAULT_ENRICH_TIMEOUT
