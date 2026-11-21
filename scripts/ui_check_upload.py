"""拍题入库（#/upload）浏览器核对：桌面 + 手机视口、无 JS 异常、入口可用、截图落盘。

配合 `scripts/ui_check_integration.py`（整合模块 7 个路由）覆盖本轮新增的「拍题入库」
主路径页面壳。需要先启动隔离实例（独立 STUDY_DATA_DIR），脚本会通过 API 注册临时账号。

用法：
  python scripts/ui_check_upload.py --base-url http://127.0.0.1:8840 \
      --username 拍题验收 --password ui-pass-12345 --out screenshots/integration

退出码：页面 JS 异常 / 关键入口缺失 / 断言失败为 1。
"""
import argparse
import json
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

DEFAULT_OUT = Path(__file__).resolve().parents[1] / "screenshots" / "integration"
VIEWPORTS = {
    "desktop": {"width": 1440, "height": 900},
    "mobile": {"width": 390, "height": 844},
}
# 拍题入库页面必须存在的可操作入口（与 upload.js 的 DOM 结构对应，允许新旧两套入口并存）。
REQUIRED_SELECTORS = (
    ("#upload-file-input, input[type=file]", "文件/拍照选择入口"),
    ("#upload-step-1", "拍题上传卡片"),
)


def launch_browser(playwright):
    """优先本机 Edge；未安装时回退 Playwright 自带 Chromium。"""
    for channel in ("msedge", "chrome", None):
        try:
            if channel:
                return playwright.chromium.launch(channel=channel, headless=True)
            return playwright.chromium.launch(headless=True)
        except Exception:  # noqa: BLE001 - 逐渠道探测
            continue
    raise RuntimeError("未找到可用的浏览器（msedge / chrome / chromium 均不可用）")


def register(context, base_url: str, username: str, password: str) -> None:
    """注册临时账号；账号已存在时回退为登录（可重复运行，不因 409 失败）。"""
    response = context.request.post(
        base_url + "/api/auth/register",
        data={"username": username, "password": password},
    )
    if response.status == 200:
        return
    if response.status in (403, 409):
        # 403 = 注册已关闭（公网/受控实例），409 = 账号已存在：都回退为登录。
        login = context.request.post(
            base_url + "/api/auth/login",
            data={"username": username, "password": password},
        )
        if login.status == 200:
            return
        raise RuntimeError(
            f"无法创建或登录临时账号（注册 {response.status} / 登录 {login.status}）："
            f"{login.text() or response.text()}"
        )
    raise RuntimeError(f"注册临时账号失败：HTTP {response.status} {response.text()}")


def overflow_px(page) -> int:
    return page.evaluate(
        "() => Math.max(0, (document.documentElement.scrollWidth || 0) - (window.innerWidth || 0))"
    )


def check_viewport(page, base_url: str, name: str, out_dir: Path) -> dict:
    # 视口由 context 创建时决定（手机用独立 is_mobile 上下文，避免桌面窗口缩窄的
    # 滚动条/visual viewport 差异造成假溢出；真机同样不会出现该 3px）。
    errors: list[str] = []
    page.on("pageerror", lambda exc: errors.append(f"pageerror: {exc}"))
    page.on("console", lambda msg: errors.append(f"console.{msg.type}: {msg.text}")
            if msg.type == "error" else None)
    page.goto(base_url + "/#/upload", wait_until="domcontentloaded")
    page.wait_for_selector(".layout, .page-heading", timeout=20000)
    page.wait_for_timeout(800)
    heading = ""
    heading_node = page.locator(".page-heading h1")
    if heading_node.count():
        heading = heading_node.first.inner_text().strip()
    missing = []
    for selector, label in REQUIRED_SELECTORS:
        if page.locator(selector).count() == 0:
            missing.append(label)
    result = {
        "viewport": name,
        "heading": heading,
        "overflow_px": overflow_px(page),
        "errors": errors,
        "missing_entries": missing,
    }
    shot = out_dir / f"upload-intake-{name}.png"
    page.screenshot(path=str(shot), full_page=True)
    result["screenshot"] = str(shot)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="拍题入库页面浏览器核对")
    parser.add_argument("--base-url", default="http://127.0.0.1:8840")
    parser.add_argument("--username", default="拍题验收")
    parser.add_argument("--password", default="ui-pass-12345")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args(argv)
    args.out.mkdir(parents=True, exist_ok=True)

    results: list[dict] = []
    with sync_playwright() as playwright:
        browser = launch_browser(playwright)
        try:
            for name, viewport in VIEWPORTS.items():
                # 每个视口用独立 context：手机视口启用 is_mobile/touch，与真机一致；
                # 同一页面 resize 会把桌面滚动条宽度算进 scrollWidth，产生假溢出。
                mobile = name == "mobile"
                context = browser.new_context(
                    viewport=viewport,
                    is_mobile=mobile,
                    has_touch=mobile,
                    device_scale_factor=2 if mobile else 1,
                )
                try:
                    register(context, args.base_url, args.username, args.password)
                    page = context.new_page()
                    results.append(check_viewport(page, args.base_url, name, args.out))
                finally:
                    context.close()
        finally:
            browser.close()

    ok = True
    for item in results:
        problems = []
        if item["errors"]:
            problems.append(f"JS 异常 {len(item['errors'])} 条：{item['errors'][:3]}")
        if item["missing_entries"]:
            problems.append("缺少入口：" + "、".join(item["missing_entries"]))
        if item["overflow_px"] > 1:
            problems.append(f"横向溢出 {item['overflow_px']}px")
        if not item["heading"]:
            problems.append("缺少页面标题")
        item["ok"] = not problems
        item["problems"] = problems
        ok = ok and not problems
        print(f"[{'OK' if item['ok'] else 'FAIL'}] {item['viewport']} 拍题入库 "
              f"标题={item['heading']!r} 溢出={item['overflow_px']}px")
        for problem in problems:
            print(f"  - {problem}")
    print(json.dumps({"ok": ok, "results": results}, ensure_ascii=False, indent=2))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
