# sigma.js + graphology（随仓库分发的 vendor 资产）

| 项 | 值 |
|---|---|
| 版本 | sigma 4.0.6、graphology 0.26.0 |
| 许可证 | 两者均为 MIT（全文见同目录 `SIGMA-LICENSE.txt`、`GRAPHOLOGY-LICENSE.txt`） |
| 来源 | npm 包 `sigma@4.0.6` 的 `dist/sigma.min.js`、`graphology@0.26.0` 的 `dist/graphology.umd.min.js`，未做任何改动 |
| 用途 | `web/js/components/knowledgeGraph.js`：学情页「知识图谱」的绘制与布局；首次点击时懒加载同源脚本 |

## 文件清单

| 文件 | 大小（字节） | SHA-256 |
|---|---|---|
| `sigma.min.js` | 393,874 | `4c7a2b7e717393717ffac2dd8736e92d77aeafae39ac40a9c4a83c5f78d1d4d0` |
| `graphology.umd.min.js` | 73,629 | `dc337efa23903f61e064c8e7e7f93a429e6855dccfc2458802b4ed30c621c087` |
| `SIGMA-LICENSE.txt` | 1,121 | `7036bbeb954c1220102cced5a608198a54f4a19b2d81d96f67997dbb9d7a51f1` |
| `GRAPHOLOGY-LICENSE.txt` | 1,104 | `9d396b4882c329077f32861c0d6822dcee48f2d0ff6196d8459af70844196275` |

## 不随仓库分发的文件

npm 包里的 `esnext/`、`types/`、`declarations/`、`peer/` 与全部 `.map` 文件非本用例所需，
不带；本组件只需 UMD 单文件与 MIT 许可。

## CSP 说明

两个产物均为纯渲染/图结构运行时，不含 `new Function` / `eval(`（测试静态断言）；
懒加载走同源 `/static/vendor/graph/`，主站 `script-src 'self'` 不变。
sigma 使用 Canvas 2D 绘制，无外部 CSS/图片依赖。
