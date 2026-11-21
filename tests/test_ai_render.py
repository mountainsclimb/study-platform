"""服务端渲染（PDF / 截图）单元测试：真实调用本机 Edge/Chromium，不联网。"""
import asyncio

import pytest

from app.ai import render as ai_render


def _html() -> str:
    return (
        "<html><head><meta charset='utf-8'><style>body{font-family:'Microsoft YaHei';}"
        "h1{font-size:28px}</style></head><body><h1>光合作用自测卷</h1>"
        "<p>1. 光合作用的原料是水和二氧化碳。</p><p>A. 水　B. 二氧化碳　C. 氧气</p></body></html>"
    )


def test_render_available_shape():
    info = ai_render.render_available()
    assert set(info) == {"available", "engine", "detail"}
    assert isinstance(info["available"], bool)


def test_html_to_pdf_produces_real_pdf():
    info = ai_render.render_available()
    if not info["available"]:
        pytest.skip("本机无可用浏览器：%s" % info["detail"])
    data = asyncio.run(ai_render.html_to_pdf(_html()))
    assert data.startswith(b"%PDF")
    assert len(data) > 1500


def test_screenshot_png_produces_real_png():
    info = ai_render.render_available()
    if not info["available"]:
        pytest.skip("本机无可用浏览器：%s" % info["detail"])
    data = asyncio.run(ai_render.screenshot_png(_html(), width=800, height=600))
    assert data.startswith(b"\x89PNG")
    assert len(data) > 2000


def test_rejects_empty_and_oversized_html():
    with pytest.raises(ai_render.RenderUnavailable):
        asyncio.run(ai_render.html_to_pdf("   "))
    with pytest.raises(ai_render.RenderUnavailable):
        asyncio.run(ai_render.screenshot_png("x" * (ai_render.MAX_HTML_BYTES + 10)))


def test_engine_can_be_disabled(monkeypatch):
    monkeypatch.setattr(ai_render, "_ENGINE_TOGGLE", "off")
    info = ai_render.render_available()
    assert info["available"] is False
    with pytest.raises(ai_render.RenderUnavailable):
        asyncio.run(ai_render.html_to_pdf(_html()))
