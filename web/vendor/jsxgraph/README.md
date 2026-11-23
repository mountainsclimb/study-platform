# JSXGraph（随仓库分发的 vendor 资产）

| 项 | 值 |
|---|---|
| 版本 | 1.14.3（npm `jsxgraph@1.14.3`） |
| 许可证 | 双许可 MIT 或 LGPL-3.0，本仓库按 **MIT** 使用（全文见同目录 `LICENSE.MIT`） |
| 来源 | npm 包 `distrib/` 目录，未做任何改动 |
| 用途 | `web/js/components/geometryDemo.js`：数学题「几何演示」与学习附件 `geometry_demo` 的交互渲染；首次打开时才注入同源脚本 |

## 文件清单

| 文件 | 大小（字节） | SHA-256 |
|---|---|---|
| `jsxgraphcore.js` | 996,214 | `3b256219e90c83e183f253374ffcecab0cfbb1350a7b4252a69af134a684ee43` |
| `jsxgraph.css` | 4,822 | `c82ee298a077701100fd99e85374b7e572ebe483fc287fd13cf72a6e5f86d64c` |
| `LICENSE.MIT` | 1,201 | `43e325001487de7f09745fbe086b054bb1b70d4ab58060e87153849365b23ff1` |

## 不随仓库分发的文件

npm 包 `distrib/` 里的 `JessieScript.js`、`geogebra.min.js`、`geonext.min.js`、
`intergeo.min.js` 依赖 `new Function` 字符串求值，与主站
`Content-Security-Policy: script-src 'self'`（无 `unsafe-eval`）冲突，一律不带；
`sketch.min.js`、`jsxcompressor.zip`、`themes/` 与 `.map` 文件非本用例所需，不带。

## CSP 说明

主站 CSP 为 `script-src 'self'`（无 `unsafe-eval`）。JSXGraph 内部有两条字符串求值路径：
`Env.supportsES6` 特性探测（`new Function`，包在 try/catch 中，不中断加载），以及
`text` 元素对**字符串内容**的 JessieCode 编译（`board.jc.snippet` → `eval`）。
本仓库的防御（见 `web/js/components/geometryDemo.js` 的 `loadJsxGraph`）：

1. 加载成功后设 `Options.jc.compile = false`——字符串内容走解释器而非 eval，零违规；
2. 预设的全部文本内容用 JS 函数入参（`create('text', [x, y, () => '…'])`），
   字符串内容被 `tests/test_oss_vendor_assets.py::test_geometry_presets_discipline` 的
   静态断言禁止；
3. 不引入 `JessieScript.js` / `geogebra.min.js` / `geonext.min.js` / `intergeo.min.js`
   这些独立文件（依赖字符串求值）。
