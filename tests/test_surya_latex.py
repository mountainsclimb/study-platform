"""Surya 图片试题转 LaTeX 适配层（app/ai/surya_latex.py）的纯文本转换单测。

不安装 surya-ocr 也能运行：识别入口的降级与结构映射用假预测对象覆盖。
"""
import base64
from io import BytesIO

from PIL import Image
import pytest

from app.ai import surya_latex
from app.ai.surya_latex import (
    SuryaUnavailableError,
    blocks_to_latex,
    html_to_latex,
    recognize_images,
    status,
)


@pytest.fixture(autouse=True)
def clear_recognizer(monkeypatch):
    monkeypatch.setattr(surya_latex, "_recognizer_cache", None)
    yield
    surya_latex._recognizer_cache = None


class FakeBlock:
    def __init__(self, label, html, *, bbox=(0, 0, 10, 10), confidence=0.9,
                 reading_order=0, skipped=False, error=False):
        self.label = label
        self.html = html
        self.bbox = list(bbox)
        self.confidence = confidence
        self.reading_order = reading_order
        self.skipped = skipped
        self.error = error


class FakePage:
    def __init__(self, blocks):
        self.blocks = blocks
        self.image_bbox = [0, 0, 100, 100]


def _png_bytes() -> bytes:
    output = BytesIO()
    Image.new("RGB", (32, 32), "white").save(output, "PNG")
    return output.getvalue()


# ---------------------------------------------------------------------------
# html_to_latex
# ---------------------------------------------------------------------------

def test_html_to_latex_keeps_inline_math_and_strips_tags():
    html = "<p>已知 <math>x^2+1</math>，求证结论&lt;成立&gt;。</p>"
    assert html_to_latex(html) == "已知 $x^2+1$，求证结论<成立>。"


def test_html_to_latex_display_math():
    assert html_to_latex("<math>\\frac{a}{b}</math>", display=True) == "$$\\frac{a}{b}$$"


def test_html_to_latex_math_with_angle_brackets_survives_tag_strip():
    html = "<p>若 <math>a &lt; b</math> 且 <math>c &gt; d</math></p>"
    assert html_to_latex(html) == "若 $a < b$ 且 $c > d$"


def test_html_to_latex_table_becomes_tabular():
    html = "<table><tr><th>年</th><th>分</th></tr><tr><td>2024</td><td>600</td></tr></table>"
    assert html_to_latex(html) == (
        "\\begin{tabular}{ll}\n"
        "年 & 分 \\\\\n"
        "2024 & 600 \\\\\n"
        "\\end{tabular}"
    )


# ---------------------------------------------------------------------------
# blocks_to_latex
# ---------------------------------------------------------------------------

def test_blocks_to_latex_joins_blocks():
    blocks = [
        {"kind": "text", "text": "第一段"},
        {"kind": "math", "latex": "E=mc^2", "display": True},
        {"kind": "math", "latex": "x", "display": False},
        {"kind": "image", "label": "Picture"},
        {"kind": "table", "latex": "\\begin{tabular}{l}1 \\\\\n\\end{tabular}"},
    ]
    assert blocks_to_latex(blocks) == (
        "第一段\n\n$$E=mc^2$$\n\n$x$\n\n\\begin{tabular}{l}1 \\\\\n\\end{tabular}"
    )


# ---------------------------------------------------------------------------
# 识别入口：结构映射与降级
# ---------------------------------------------------------------------------

def test_recognize_images_maps_blocks_and_orders_by_reading_order(monkeypatch):
    page = FakePage([
        FakeBlock("Equation", "<math>E=mc^2</math>", reading_order=1),
        FakeBlock("Text", "<p>质能方程</p>", reading_order=0),
        FakeBlock("Picture", "", reading_order=2, skipped=True),
        FakeBlock("Table", "<table><tr><td>1</td></tr></table>", reading_order=3),
    ])
    monkeypatch.setattr(
        surya_latex, "_get_recognizer", lambda: (lambda images, **kwargs: [page] * len(images))
    )

    result = recognize_images([_png_bytes(), _png_bytes()])

    assert result["engine"] == "surya"
    assert result["count"] == 2
    first = result["items"][0]
    assert [block["kind"] for block in first["blocks"]] == ["text", "math", "image", "table"]
    assert first["blocks"][1] == {
        "kind": "math", "latex": "E=mc^2", "display": True,
        "label": "Equation", "bbox": [0, 0, 10, 10], "confidence": 0.9,
    }
    assert first["latex"] == "质能方程\n\n$$E=mc^2$$\n\n\\begin{tabular}{l}\n1 \\\\\n\\end{tabular}"
    assert result["latex"].count("质能方程") == 2


def test_recognize_images_accepts_image_part_dict(monkeypatch):
    seen = {}

    def fake_recognizer(images, **kwargs):
        seen["count"] = len(images)
        return [FakePage([FakeBlock("Text", "<p>hi</p>")])]

    monkeypatch.setattr(surya_latex, "_get_recognizer", lambda: fake_recognizer)
    part = {"mime": "image/png", "data": base64.b64encode(_png_bytes()).decode()}

    result = recognize_images([part])

    assert seen["count"] == 1
    assert result["items"][0]["latex"] == "hi"


def test_recognize_images_without_surya_raises(monkeypatch):
    def raise_unavailable():
        raise SuryaUnavailableError("未安装 surya-ocr")

    monkeypatch.setattr(surya_latex, "_get_recognizer", raise_unavailable)
    with pytest.raises(SuryaUnavailableError):
        recognize_images([_png_bytes()])


def test_recognize_images_requires_images():
    with pytest.raises(ValueError):
        recognize_images([])


def test_status_reports_unavailable_without_surya():
    info = status()
    assert set(info) == {"available", "reason"}
    # 本测试环境未安装 surya-ocr（requirements-surya.txt 为可选依赖）；
    # 若恰好已安装则只校验结构。
    assert isinstance(info["available"], bool)
    if not info["available"]:
        assert "surya" in info["reason"].lower()
