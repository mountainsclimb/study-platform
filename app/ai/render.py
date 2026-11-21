"""服务端 HTML 渲染：PDF 导出与页面截图（Playwright，无新增运行依赖）。

用途：
- 试卷 / 答题卡 / 课堂离线页的**服务端 PDF**导出（浏览器打印同源方案，字体与排版与前端一致）；
- 课堂视频导出的逐帧截图（后续 ``app/classroom/video.py`` 复用）。

设计要点：
- 浏览器惰性启动、每次调用后关闭：导出频率低，避免长时间占用内存与句柄；
- 可用渠道：本机 Edge（msedge）→ Chrome（chrome）→ Playwright 自带 Chromium；
- 输入只接受服务端自己生成的 HTML（不代理任意外部 URL，防止 SSRF）；
- 失败统一抛 :class:`RenderUnavailable`，由调用方给中文引导（保留浏览器打印兜底）。
"""
from __future__ import annotations

import logging
import os

log = logging.getLogger("app.ai.render")

MAX_HTML_BYTES = 8 * 1024 * 1024
RENDER_TIMEOUT_MS = int(os.environ.get("STUDY_RENDER_TIMEOUT_MS", "45000") or 45000)
DEFAULT_VIEWPORT = {"width": 1240, "height": 1754}
_ENGINE_TOGGLE = os.environ.get("STUDY_RENDER_ENGINE", "auto").strip().lower()
_CHROMIUM_CHANNELS = ("msedge", "chrome")


class RenderUnavailable(RuntimeError):
    """渲染引擎不可用（Playwright 未安装 / 无可用浏览器 / 超时）。"""


def _playwright_importable() -> bool:
    try:
        import playwright  # noqa: F401
        from playwright.async_api import async_playwright  # noqa: F401
        return True
    except Exception:  # noqa: BLE001 - 缺包时返回不可用，不抛导入错误
        return False


def render_available() -> dict:
    """当前进程是否具备服务端渲染能力（不实际启动浏览器）。"""
    if _ENGINE_TOGGLE == "off":
        return {"available": False, "engine": None, "detail": "STUDY_RENDER_ENGINE=off"}
    if not _playwright_importable():
        return {
            "available": False,
            "engine": None,
            "detail": "未安装 Playwright（pip install playwright），可改用浏览器打印导出 PDF",
        }
    return {"available": True, "engine": "playwright", "detail": "可用（Edge/Chrome/Chromium 之一）"}


async def _launch(playwright):
    """按 Edge → Chrome → Chromium 顺序启动；全部失败抛 RenderUnavailable。"""
    last_error = "没有可用浏览器"
    for channel in _CHROMIUM_CHANNELS:
        try:
            return await playwright.chromium.launch(channel=channel, headless=True)
        except Exception as exc:  # noqa: BLE001 - 逐渠道探测
            last_error = "%s: %s" % (channel, type(exc).__name__)
    try:
        return await playwright.chromium.launch(headless=True)
    except Exception as exc:  # noqa: BLE001
        last_error = "chromium: %s" % type(exc).__name__
    raise RenderUnavailable("没有可用浏览器（%s）" % last_error)


def _validate_html(html: str) -> str:
    text = str(html or "")
    if not text.strip():
        raise RenderUnavailable("渲染内容为空")
    if len(text.encode("utf-8")) > MAX_HTML_BYTES:
        raise RenderUnavailable("渲染内容超过 %d MB" % (MAX_HTML_BYTES // (1024 * 1024)))
    return text


async def html_to_pdf(html: str, *, paper: str = "A4", landscape: bool = False,
                      margin_mm: int = 10) -> bytes:
    """把服务端生成的 HTML 渲染为 PDF 字节。"""
    content = _validate_html(html)
    if not render_available()["available"]:
        raise RenderUnavailable(render_available()["detail"])
    from playwright.async_api import async_playwright

    try:
        async with async_playwright() as playwright:
            browser = await _launch(playwright)
            try:
                page = await browser.new_page(viewport=dict(DEFAULT_VIEWPORT))
                await page.set_content(content, wait_until="load", timeout=RENDER_TIMEOUT_MS)
                await page.emulate_media(media="print")
                margin = "%dmm" % max(0, int(margin_mm))
                data = await page.pdf(
                    format=paper,
                    landscape=bool(landscape),
                    print_background=True,
                    margin={"top": margin, "bottom": margin, "left": margin, "right": margin},
                )
                if not data or not data.startswith(b"%PDF"):
                    raise RenderUnavailable("PDF 渲染失败（返回内容不是 PDF）")
                return data
            finally:
                await browser.close()
    except RenderUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001 - 浏览器/页面异常统一转可读错误
        log.warning("html_to_pdf failed: %s", type(exc).__name__)
        raise RenderUnavailable("PDF 渲染失败（%s）" % type(exc).__name__) from exc


async def screenshot_png(html: str, *, width: int = 1280, height: int = 720,
                         full_page: bool = False) -> bytes:
    """把 HTML 渲染为一帧 PNG（课堂视频导出的基础能力）。"""
    content = _validate_html(html)
    if not render_available()["available"]:
        raise RenderUnavailable(render_available()["detail"])
    from playwright.async_api import async_playwright

    safe_width = max(320, min(int(width or 1280), 2560))
    safe_height = max(240, min(int(height or 720), 1600))
    try:
        async with async_playwright() as playwright:
            browser = await _launch(playwright)
            try:
                page = await browser.new_page(viewport={"width": safe_width, "height": safe_height})
                await page.set_content(content, wait_until="load", timeout=RENDER_TIMEOUT_MS)
                data = await page.screenshot(full_page=bool(full_page), type="png")
                if not data or not data.startswith(b"\x89PNG"):
                    raise RenderUnavailable("截图渲染失败（返回内容不是 PNG）")
                return data
            finally:
                await browser.close()
    except RenderUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001
        log.warning("screenshot_png failed: %s", type(exc).__name__)
        raise RenderUnavailable("截图渲染失败（%s）" % type(exc).__name__) from exc


__all__ = [
    "MAX_HTML_BYTES", "RENDER_TIMEOUT_MS", "RenderUnavailable",
    "render_available", "html_to_pdf", "screenshot_png",
]
