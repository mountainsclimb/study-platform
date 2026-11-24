# markmap 交互脑图（随仓库分发的构建产物）

| 项 | 值 |
|---|---|
| 打包入口 | markmap-view 0.18.13（npm 包无自包含浏览器产物，构建产物随仓库分发） |
| 实际打入 | markmap-view、markmap-common 与 d3 及其 30 个子包（共 33 个包；@babel/runtime 仅安装、未进入产物），逐包许可全文见同目录 LICENSE |
| 版本锁定 | `scripts/markmap-bundle/package.json` + `package-lock.json`（顶层与全部传递依赖均钉死，`npm ci` 安装） |
| 构建工具 | esbuild 0.28.2（仅构建期，不进分发产物） |
| 用途 | `web/js/components/mindmap.js` 的 `renderInteractiveMindmap()`：笔记「交互 / 静态」脑图切换的交互视图；首次切换时懒加载 |

## 文件清单

| 文件 | 大小（字节） | SHA-256 |
|---|---|---|
| `markmap-view.iife.min.js` | 120134 | `cd44f6fb7316bffa5b5782c6c76aaf0baee90cb9d4ac16a463572b2ff4366095` |
| `LICENSE` | 28181 | `3f92393bdf2d11859119785b6cd442fa6fcfebe01f67c72c79997ef52a1f1df7` |

## 重建与校验

```powershell
node scripts/build_markmap_bundle.mjs          # 重建（lockfile 钉死版本，字节一致）
node scripts/build_markmap_bundle.mjs --check  # 校验磁盘产物与重建结果一致
```

依赖由 `scripts/markmap-bundle/package-lock.json` 钉死（`npm ci`）；升级时更新该目录的两个文件、重建产物并修订本表。
产物与许可均由 esbuild metafile 的实际 inputs 生成，避免漏登记传递依赖。

## 已知边界

- `initialExpandLevel` 不能作为选项传入（0.18 的 deriveOptions 白名单处理会崩，默认 -1 全展开）；
- options 必须直接用 `deriveOptions(...)` 的结果，不能作为 `style:` 二次嵌套。

## CSP 说明

打包产物为纯渲染运行时，无字符串求值；节点数据由 `mindmap.js` 从既有树结构转换
（`{content, children}`），样式经 markmap 的 `embedGlobalCSS` 内嵌在 SVG 里，
主站 `script-src 'self'` 不变，懒加载同源注入。

