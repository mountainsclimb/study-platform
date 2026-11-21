# 交互场景内联库（three.js）

本目录是「知序 AI 课堂」交互场景（widget = `visualization3d`）使用的 **three.js 内联包**。
库源码由服务端在生成/保存交互 HTML 时**注入到页面最前面**（`<script data-zhixu-lib="three@0.186.1">`），
前端与浏览器**不会**从这里发起任何网络请求，也不会以 `<script src>` 形式引用本文件。

## 文件

| 文件 | 说明 |
|---|---|
| `three.module.min.js` | three.js r186（npm `three@0.186.1`）完整导出的 **IIFE + minify** 经典脚本，执行后暴露全局 `THREE`；742823 字节；sha256 `d09c3b925f96d0a7eaa9e94b0e155f90dbb9878da7fa1702543c74b79eabead1` |
| `LICENSE` | 上游 three.js 的 MIT 许可证原文 |
| `build_three.py` | 可复现构建脚本（离线，只用本机 npm 包与本机 esbuild） |
| `README.md` | 本说明 |

## 来源与许可

- 上游：three.js（https://threejs.org/，MIT License，Copyright 2010-2026 three.js authors）。
- 本机来源：`E:\project\cultural\frontend\node_modules\three`（npm 安装的 `three@0.186.1`；本机无 OpenMAIC node_modules，
  故使用该本机副本。OpenMAIC `package.json` 声明的 `three` 同属该库）。
- 未联网下载、未新增 Python/前端运行依赖；构建只用到本机 `esbuild 0.25.12` 与 Node.js。
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

esbuild entry.js --bundle --format=iife --minify --target=es2020 --platform=browser \
    --legal-comments=none --outfile=three.module.min.js
# 构建后由 build_three.py 追加版本/许可 banner。
```

或直接执行本目录脚本：

```text
python web/vendor/interactive/build_three.py
python web/vendor/interactive/build_three.py --three-pkg <three 包目录> --esbuild <esbuild 可执行文件>
```

构建信息：three.js r186 / npm `three@0.186.1`；esbuild 0.25.12；Node.js v24。

## 与上游的唯一差异（可审计）

1. **打包 + minify + banner**（如上）；
2. 上游 PMREM/GGX 着色器的 GLSL 注释里含一个 `https://jcgt.org/published/0007/04/01/` URL，构建时仅去掉协议前缀
   （改为 `jcgt.org/published/0007/04/01`）。这是**注释文本**，不参与着色器编译，渲染行为不变；
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
