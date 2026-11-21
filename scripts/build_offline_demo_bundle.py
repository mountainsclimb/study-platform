#!/usr/bin/env python3
"""生成离线课堂演示播放器 bundle：``web/vendor/offline/offline-demo.js``（M7）。

背景（方案 §5.4）：PPTX / PDF / 打印讲义只保留海报与文字，HTML / ZIP 必须保留可交互内容。
离线单文件 HTML 不允许任何外链、也不允许联网加载 ES module，因此把两个**无 import 的纯
ESM** 渲染器（``web/vendor/principles/flow.js`` / ``iso.js``）做确定性文本转换后内联：

1. 校验源文件没有 ``import`` / ``export default`` / 动态 ``import()`` 等无法文本转换的语法；
2. 去掉行首的 ``export ``（``export function`` / ``export const`` / ``export let`` / ``export class``），
   其余源码原样保留（不压缩、不改写语义，便于审阅与 ``node --check``）；
3. 两个模块内部有同名私有函数/常量（``NS``、``clamp``、``svgEl``、``hashSeed`` 等），
   因此各自包在独立的 IIFE 作用域里，再把对外函数挂到统一的
   ``window.ZhixuOfflineDemo``（``createFlowView`` / ``renderFlowPoster`` /
   ``createIsoView`` / ``renderIsoPoster`` + 固定版本号 ``OFFLINE_BUNDLE_VERSION``）；
4. 唯一需要改写的字面量是 SVG 命名空间 URI：源码里的 ``http://www.w3.org/2000/svg``
   只是 XML 命名空间标识符（不会联网），但离线 HTML 的验收口径是「文本中不出现
   http(s) 外链」，所以改成等价的字符串拼接 ``'http' + '://www.w3.org/2000/svg'``；
5. 输出 UTF-8 / LF / 末尾换行；同输入同字节；``--check`` 与磁盘产物比较，不一致时
   退出码 1 并提示重新生成。

本脚本只用 Python 标准库，不需要 Node / npm / esbuild（本机也没有这些构建依赖）。
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE_DIR = ROOT / "web" / "vendor" / "principles"
OUTPUT = ROOT / "web" / "vendor" / "offline" / "offline-demo.js"

#: 产物体积上限（flow.js + iso.js 约 76KB 源；留足余量但防止误把大文件塞进来）。
MAX_BUNDLE_BYTES = 400 * 1024

HEADER = "/* 自动生成，勿手改：python scripts/build_offline_demo_bundle.py */\n"

#: 源模块里唯一的 SVG 命名空间字面量（标识符，不是网络请求；离线 HTML 文本里不能出现 http://）。
_NS_LITERAL = "'http://www.w3.org/2000/svg'"
_NS_SAFE_LITERAL = "'http' + '://www.w3.org/2000/svg'"

#: 行首 ESM export 声明（只允许这四类；其余 export 语法一律拒绝）。
_EXPORT_LINE_RE = re.compile(r"^export (function |const |let |class )")
_STRIP_EXPORT_RE = re.compile(r"(?m)^export ")
_IMPORT_LINE_RE = re.compile(r"(?m)^[ \t]*import[ \t]")
_DYNAMIC_IMPORT_RE = re.compile(r"\bimport[ \t]*\(")
_EXPORT_DEFAULT_RE = re.compile(r"(?m)^[ \t]*export[ \t]+default\b")
_BAD_EXPORT_RE = re.compile(r"(?m)^[ \t]*export[ \t]+(?!function[ \t]|const[ \t]|let[ \t]|class[ \t])")

#: (源文件名, 模块 IIFE 变量名, 对外函数名列表)
MODULES = (
    ("flow.js", "__zhixuFlow", ("createFlowView", "renderFlowPoster")),
    ("iso.js", "__zhixuIso", ("createIsoView", "renderIsoPoster")),
)


class BundleError(RuntimeError):
    """源文件语法/体积不满足确定性文本转换条件。"""


def _read_source(name: str) -> str:
    path = SOURCE_DIR / name
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:  # pragma: no cover - 仓库文件缺失属于构建错误
        raise BundleError("无法读取源文件 %s：%s" % (path, exc)) from exc
    # 统一 LF，保证 Windows / Linux 同输入同字节。
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _validate_source(name: str, text: str) -> None:
    if _IMPORT_LINE_RE.search(text):
        raise BundleError("%s 含静态 import 语句，无法文本转换（离线 HTML 禁止模块加载）" % name)
    if _DYNAMIC_IMPORT_RE.search(text):
        raise BundleError("%s 含动态 import()，无法文本转换（离线 HTML 禁止模块加载）" % name)
    if _EXPORT_DEFAULT_RE.search(text):
        raise BundleError("%s 含 export default，文本转换只支持具名导出" % name)
    bad = _BAD_EXPORT_RE.search(text)
    if bad:
        raise BundleError("%s 含不支持的 export 语法：%r" % (name, bad.group(0)))
    for line in text.splitlines():
        if line.startswith("export ") and not _EXPORT_LINE_RE.match(line):
            raise BundleError("%s 的 export 行无法转换：%r" % (name, line[:120]))
    if _NS_LITERAL not in text:
        raise BundleError("%s 未找到预期的 SVG 命名空间字面量，源文件结构已变化" % name)
    if "</" in text:
        raise BundleError("%s 含 </ 序列，内联进 <script> 会提前闭合，拒绝生成" % name)


def _transform_module(name: str, text: str) -> str:
    stripped = _STRIP_EXPORT_RE.sub("", text)
    # SVG 命名空间改成等价拼接（文本中不再出现 http://）。
    transformed = stripped.replace(_NS_LITERAL, _NS_SAFE_LITERAL)
    if "http://" in transformed or "https://" in transformed:
        raise BundleError("%s 转换后仍含 http(s) 外链文本，拒绝生成" % name)
    return transformed.rstrip("\n") + "\n"


def build_bundle() -> str:
    """按固定顺序拼接两个模块，返回完整 bundle 文本（确定性）。"""
    chunks: list[str] = [HEADER]
    chunks.append("(function (global) {\n")
    chunks.append('"use strict";\n')
    exports: list[tuple[str, tuple[str, ...]]] = []
    for name, scope, exported in MODULES:
        source = _read_source(name)
        _validate_source(name, source)
        transformed = _transform_module(name, source)
        for function_name in exported:
            if not re.search(r"(?m)^function\s+%s\s*\(" % re.escape(function_name), transformed):
                raise BundleError("%s 去 export 后找不到函数 %s" % (name, function_name))
        chunks.append("\n/* ---- 来源：web/vendor/principles/%s（去 ESM export 后原样内联） ---- */\n" % name)
        chunks.append("var %s = (function () {\n" % scope)
        chunks.append('"use strict";\n')
        chunks.append(transformed)
        chunks.append("return {\n")
        for function_name in exported:
            chunks.append("  %s: %s,\n" % (function_name, function_name))
        chunks.append("};\n")
        chunks.append("})();\n")
        exports.append((scope, exported))
    chunks.append("\nglobal.ZhixuOfflineDemo = {\n")
    for scope, exported in exports:
        for function_name in exported:
            chunks.append("  %s: %s.%s,\n" % (function_name, scope, function_name))
    chunks.append('  OFFLINE_BUNDLE_VERSION: "1",\n')
    chunks.append('  OFFLINE_BUNDLE_SOURCE: "web/vendor/principles/flow.js + iso.js",\n')
    chunks.append("};\n")
    chunks.append("})(window);\n")
    text = "".join(chunks)
    payload = text.encode("utf-8")
    if len(payload) > MAX_BUNDLE_BYTES:
        raise BundleError(
            "bundle 体积 %d 字节超过上限 %d 字节" % (len(payload), MAX_BUNDLE_BYTES)
        )
    return text


def write_bundle(text: str) -> None:
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    # newline="\n"：所有平台都写 LF，保证提交产物与 --check 稳定。
    OUTPUT.write_text(text, encoding="utf-8", newline="\n")


def check_bundle(expected: str) -> bool:
    try:
        actual = OUTPUT.read_text(encoding="utf-8")
    except OSError:
        return False
    # 读回时统一换行，避免不同平台检出把 CRLF 误判为过期（产物本身由本脚本写 LF）。
    return actual.replace("\r\n", "\n") == expected


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="生成/校验离线课堂演示播放器 bundle（web/vendor/offline/offline-demo.js）",
    )
    parser.add_argument(
        "--check", action="store_true",
        help="只校验磁盘产物是否与重新生成的结果一致（不一致退出码 1）",
    )
    args = parser.parse_args(argv)

    try:
        expected = build_bundle()
    except BundleError as exc:
        print("bundle 生成失败：%s" % exc, file=sys.stderr)
        return 2

    if args.check:
        if check_bundle(expected):
            print("offline-demo.js 与源文件一致（%d 字节）" % len(expected.encode("utf-8")))
            return 0
        print(
            "web/vendor/offline/offline-demo.js 已过期或缺失；"
            "请运行 python scripts/build_offline_demo_bundle.py",
            file=sys.stderr,
        )
        return 1

    write_bundle(expected)
    print(
        "已生成 web/vendor/offline/offline-demo.js（%d 字节）"
        % len(expected.encode("utf-8"))
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
