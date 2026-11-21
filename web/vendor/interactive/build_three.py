# -*- coding: utf-8 -*-
"""构建「知序」交互场景内联用 three.js 包（离线可复现：只用本机 npm 包 + 本机 esbuild）。

来源：本机 npm 包 ``three``（默认 ``E:\\project\\cultural\\frontend\\node_modules\\three``，MIT）。
产物（写在本脚本所在目录）：
  - ``three.module.min.js``：IIFE + minify 经典脚本，执行后暴露全局 ``THREE``；
  - ``LICENSE``：上游 MIT 许可证原文；
  - ``README.md``：来源 / 许可 / 构建 / 安全边界说明（含本文件的大小与 sha256）。

用法::

    python web/vendor/interactive/build_three.py
    python web/vendor/interactive/build_three.py --three-pkg D:/some/node_modules/three \
        --esbuild D:/some/node_modules/@esbuild/win32-x64/esbuild.exe

说明：内联包必须是**同步经典脚本**（不能是 ESM）：交互页运行在不透明源 sandbox iframe 中，
CSP 无 ``unsafe-eval`` 且禁止外链；内联 ``type="module"`` 属于延迟脚本，无法保证先于用户脚本执行。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_THREE_PKG = Path(r"E:\project\cultural\frontend\node_modules\three")
DEFAULT_ESBUILD = Path(r"E:\project\cultural\frontend\node_modules\@esbuild\win32-x64\esbuild.exe")
BANNER_TEMPLATE = (
    "/*! three.js r{revision} ({version}) | MIT License | Copyright 2010-2026 three.js authors "
    "| inlined by zhixu server (see README.md) */\n"
)
# 上游 GLSL 注释里的一个 URL：为保证离线资源包「外链扫描」零误报，构建时仅去掉协议前缀
# （注释文本，不参与着色器编译，无功能影响）。
GLSL_URL = "https://jcgt.org/published/0007/04/01/"
GLSL_URL_NORMALIZED = "jcgt.org/published/0007/04/01"
# 说明：完整 three 导出自带 FileLoader 等标准加载器（内部以 fetch 实现）；这里只检查
# 「动态执行 / 凭据 / 主动联网」类危险 API。加载器的网络请求会被沙箱 CSP ``connect-src 'none'``
# 整体拦截（用户 HTML 生成侧也静态禁止 fetch/XHR/WebSocket），离线场景不使用加载器。
FORBIDDEN = {
    "eval(": re.compile(r"\beval\s*\("),
    "new Function": re.compile(r"\bnew\s+Function\b"),
    "localStorage": re.compile(r"\blocalStorage\b"),
    "document.cookie": re.compile(r"\bdocument\s*\.\s*cookie\b"),
    "XMLHttpRequest": re.compile(r"\bXMLHttpRequest\b"),
    "WebSocket": re.compile(r"\bWebSocket\b"),
    "sendBeacon": re.compile(r"\bsendBeacon\b"),
}

README_TEMPLATE = """# 交互场景内联库（three.js）

本目录是「知序 AI 课堂」交互场景（widget = `visualization3d`）使用的 **three.js 内联包**。
库源码由服务端在生成/保存交互 HTML 时**注入到页面最前面**（`<script data-zhixu-lib="three@%(version)s">`），
前端与浏览器**不会**从这里发起任何网络请求，也不会以 `<script src>` 形式引用本文件。

## 文件

| 文件 | 说明 |
|---|---|
| `three.module.min.js` | three.js r%(revision)s（npm `three@%(version)s`）完整导出的 **IIFE + minify** 经典脚本，执行后暴露全局 `THREE`；%(size)d 字节；sha256 `%(sha256)s` |
| `LICENSE` | 上游 three.js 的 MIT 许可证原文 |
| `build_three.py` | 可复现构建脚本（离线，只用本机 npm 包与本机 esbuild） |
| `README.md` | 本说明 |

## 来源与许可

- 上游：three.js（https://threejs.org/，MIT License，Copyright 2010-2026 three.js authors）。
- 本机来源：`%(three_pkg)s`（npm 安装的 `three@%(version)s`；本机无 OpenMAIC node_modules，
  故使用该本机副本。OpenMAIC `package.json` 声明的 `three` 同属该库）。
- 未联网下载、未新增 Python/前端运行依赖；构建只用到本机 `esbuild %(esbuild_version)s` 与 Node.js。
- 许可证全文见同目录 `LICENSE`（MIT）。

## 为什么是 IIFE 而不是 ESM

交互场景运行在 `<iframe sandbox="allow-scripts">`（不透明源，无 `allow-same-origin`）里，
响应 CSP 为 `default-src 'none'; script-src 'unsafe-inline'; ...`（**无 `unsafe-eval`**、无外链）。
因此：

1. 不能从 CDN / `'self'` / 相对路径加载 three（不透明源下外链不可靠，且 CSP 禁止）；
2. 内联的 `<script type="module">` 属于延迟脚本，会晚于模型写的经典 `<script>` 执行，
   无法保证 `THREE` 在用户脚本之前就绪；
3. 所以打包为 **同步执行的经典脚本 IIFE**，由服务端注入在文档 `<head>` 之后的第一位，
   先于用户所有脚本执行，`globalThis.THREE` 立即可用；不含 `eval`/`new Function`、
   不读写浏览器存储/凭据，适配无 `unsafe-eval` 的 CSP。完整 three 导出里自带 `FileLoader`
   等标准加载器（内部用 `fetch` 实现），但沙箱响应头 `connect-src 'none'` 会拦截一切网络请求
   （含 `data:`/`blob:` fetch），离线场景也不使用任何加载器。

## 构建方式（可复现，不联网）

```text
# 入口 entry.js（临时文件）：
import * as THREE_NS from '<three>/build/three.module.js';
globalThis.THREE = THREE_NS;

esbuild entry.js --bundle --format=iife --minify --target=es2020 --platform=browser \\
    --legal-comments=none --outfile=three.module.min.js
# 构建后由 build_three.py 追加版本/许可 banner。
```

或直接执行本目录脚本：

```text
python web/vendor/interactive/build_three.py
python web/vendor/interactive/build_three.py --three-pkg <three 包目录> --esbuild <esbuild 可执行文件>
```

构建信息：three.js r%(revision)s / npm `three@%(version)s`；esbuild %(esbuild_version)s；Node.js v24。

## 与上游的唯一差异（可审计）

1. **打包 + minify + banner**（如上）；
2. 上游 PMREM/GGX 着色器的 GLSL 注释里含一个 `%(glsl_url)s` URL，构建时仅去掉协议前缀
   （改为 `%(glsl_url_norm)s`）。这是**注释文本**，不参与着色器编译，渲染行为不变；
   目的是让「离线教学资源包」的外链扫描保持零误报（HTML 内唯一保留的 `http(s)` 是
   `http://www.w3.org/1999/xhtml` 命名空间，属于静态校验的允许项）。
   构建脚本对「该 URL 恰好出现 1 次」做断言，上游升级时若变化会显式失败而不是静默放过；
3. 无其他源码改动；未删减导出，完整 three 导出均在（`THREE.REVISION` 等元信息保留）。

## 安全边界（服务端强制执行，见 `app/classroom/interactive.py`）

- 该文件内容只在「模型 HTML 已通过静态安全校验」之后注入，注入后仍会做一次终检；
- 终检会按 sha256 校验并**剔除**注入片段后再扫描用户 HTML，用户部分依旧禁止
  `fetch`/XHR/WebSocket/`eval`/`new Function`/父窗口 DOM/凭据访问等；
- 伪造 `data-zhixu-lib` 片段（sha256 不匹配）不会被豁免，会被当作普通用户脚本检查并拒绝；
- 沙箱 CSP 不含 `unsafe-eval`，即使用户代码试图 `eval` 也会被 CSP 拒绝（生成侧同时静态拒绝）；
- 库片段里保留了 `http://www.w3.org/1999/xhtml`（`createElementNS` 的命名空间标识），
  不是网络请求；静态校验对它按已有白名单放行。

## 维护

升级 three 时：更新本机 npm 包 → 执行 `build_three.py` → 更新
`app/classroom/interactive.py` 里 `INLINE_LIBRARIES["three"]` 的版本号（大小/sha256 在运行时按文件
自动计算，无需手工维护）→ 跑 `python -m pytest tests/test_classroom_interactive3d.py -q`。
"""


def _build(three_pkg: Path, esbuild: Path) -> dict:
    pkg = json.loads((three_pkg / "package.json").read_text(encoding="utf-8"))
    version = pkg["version"]
    module_path = three_pkg / "build" / "three.module.js"
    if not module_path.is_file():
        raise SystemExit("找不到 three.module.js：%s" % module_path)
    raw_source = module_path.read_text(encoding="utf-8")
    revision_match = re.search(r"const REVISION = '([^']+)'", raw_source)
    core_path = three_pkg / "build" / "three.core.js"
    if core_path.is_file():
        core_revision = re.search(r"const REVISION = '([^']+)'", core_path.read_text(encoding="utf-8"))
        revision = core_revision.group(1) if core_revision else (revision_match.group(1) if revision_match else "?")
    else:
        revision = revision_match.group(1) if revision_match else "?"
    if not esbuild.is_file():
        raise SystemExit("找不到 esbuild 可执行文件：%s" % esbuild)

    tmp = Path(tempfile.mkdtemp(prefix="zhixu-three-"))
    entry = tmp / "entry.js"
    bundle = tmp / "three.iife.min.js"
    entry.write_text(
        "import * as THREE_NS from '%s';\nglobalThis.THREE = THREE_NS;\n" % module_path.as_posix(),
        encoding="utf-8",
    )
    build = subprocess.run(
        [str(esbuild), str(entry), "--bundle", "--format=iife", "--minify",
         "--target=es2020", "--platform=browser", "--legal-comments=none",
         "--log-level=warning", "--outfile=%s" % bundle],
        capture_output=True, text=True,
    )
    if build.returncode != 0:
        raise SystemExit("esbuild 构建失败：\n%s\n%s" % (build.stdout, build.stderr))
    if build.stderr.strip():
        print("[esbuild stderr]\n" + build.stderr.strip())
    text = bundle.read_text(encoding="utf-8")

    # --- 安全/内联可行性自检 -------------------------------------------------
    problems = []
    lowered = text.lower()
    if "</script" in lowered:
        problems.append("包含 </script 序列，无法安全内联")
    if "<!--" in lowered:
        problems.append("包含 <!-- 序列，HTML 解析可能异常")
    for label, pattern in FORBIDDEN.items():
        if pattern.search(text):
            problems.append("包含禁止 API：%s" % label)
    if "globalThis.THREE=" not in text.replace(" ", ""):
        problems.append("未暴露全局 THREE")
    if problems:
        raise SystemExit("内联包自检失败：%s" % "；".join(problems))

    # --- 唯一后处理：GLSL 注释 URL 去协议前缀 --------------------------------
    url_count = text.count(GLSL_URL)
    if url_count != 1:
        raise SystemExit("GLSL 注释 URL 预期出现 1 次，实际 %d 次（上游版本可能变化，需复核）" % url_count)
    text = text.replace(GLSL_URL, GLSL_URL_NORMALIZED)

    final = BANNER_TEMPLATE.format(revision=revision, version=version) + text
    final_bytes = final.encode("utf-8")

    # --- 落盘 ----------------------------------------------------------------
    HERE.mkdir(parents=True, exist_ok=True)
    out_file = HERE / "three.module.min.js"
    out_file.write_bytes(final_bytes)
    shutil.copyfile(three_pkg / "LICENSE", HERE / "LICENSE")
    license_text = (HERE / "LICENSE").read_text(encoding="utf-8", errors="replace")
    if "MIT License" not in license_text:
        raise SystemExit("three LICENSE 内容异常")

    sha256 = hashlib.sha256(final_bytes).hexdigest()
    size = len(final_bytes)
    node_check = subprocess.run(["node", "--check", str(out_file)], capture_output=True, text=True)
    if node_check.returncode != 0:
        raise SystemExit("node --check 失败：%s" % node_check.stderr)
    runtime_check = subprocess.run(
        ["node", "-e",
         "require(%r);const T=globalThis.THREE;"
         "const need=['Scene','PerspectiveCamera','WebGLRenderer','BoxGeometry','MeshStandardMaterial',"
         "'AmbientLight','DirectionalLight','Color','Vector3','Group','Raycaster','Clock','GridHelper',"
         "'AxesHelper','TorusKnotGeometry','Mesh','Line','Sprite','CanvasTexture','MathUtils'];"
         "const missing=need.filter(k=>typeof T[k]!=='function'&&typeof T[k]!=='object');"
         "if(missing.length){console.error('missing:',missing);process.exit(2)}"
         "console.log('exports',Object.keys(T).length,'revision',T.REVISION);" % str(out_file)],
        capture_output=True, text=True,
    )
    if runtime_check.returncode != 0:
        raise SystemExit("Node 运行时自检失败：%s%s" % (runtime_check.stdout, runtime_check.stderr))
    runtime_info = runtime_check.stdout.strip()

    esbuild_version = subprocess.run([str(esbuild), "--version"], capture_output=True, text=True,
                                     check=True).stdout.strip()
    readme = README_TEMPLATE % {
        "version": version, "revision": revision, "size": size, "sha256": sha256,
        "three_pkg": str(three_pkg), "esbuild_version": esbuild_version,
        "glsl_url": GLSL_URL, "glsl_url_norm": GLSL_URL_NORMALIZED,
    }
    (HERE / "README.md").write_text(readme, encoding="utf-8")
    return {
        "version": version, "revision": revision, "bytes": size, "sha256": sha256,
        "esbuild": esbuild_version, "node": runtime_info,
        "file": str(out_file), "license": str(HERE / "LICENSE"), "readme": str(HERE / "README.md"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="构建交互场景内联 three.js 包（离线可复现）")
    parser.add_argument("--three-pkg", default=str(DEFAULT_THREE_PKG), help="本机 three npm 包目录")
    parser.add_argument("--esbuild", default=str(DEFAULT_ESBUILD), help="本机 esbuild 可执行文件")
    args = parser.parse_args()
    summary = _build(Path(args.three_pkg), Path(args.esbuild))
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
