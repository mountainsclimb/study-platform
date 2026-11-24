# vis-timeline（随仓库分发的 vendor 资产）

| 项 | 值 |
|---|---|
| 版本 | 8.5.6（npm `vis-timeline@8.5.6`） |
| 许可证 | 双许可 Apache-2.0 或 MIT，本仓库按 **MIT** 使用（两份全文见同目录） |
| 来源 | npm 包 `dist/vis-timeline-graph2d.min.js` 与 `dist/vis-timeline-graph2d.min.css`，未做任何改动 |
| 用途 | `web/js/components/timelineDemo.js`：历史题「时间轴」与学习附件 `timeline` 的交互渲染；首次打开时才注入同源脚本与样式 |

## 文件清单

| 文件 | 大小（字节） | SHA-256 |
|---|---|---|
| `vis-timeline-graph2d.min.js` | 528,740 | `01e838f31f1aaef88d686915b5a3f1453ba970270155157daed5fe6063a25889` |
| `vis-timeline-graph2d.min.css` | 19,780 | `43689b3d6a6342da896e7ea9d78873d8d370d7b9805850f7e9508e918dde5a8a` |
| `LICENSE.Apache-2.0.txt` | 10,171 | `1509904280a272780fd69c04b858f512cf216f8e3464a0dd98d8f34416feced9` |
| `LICENSE.MIT.txt` | 1,145 | `507e7e91e0a385c9de0464889068fada0620e327d23f1d64605a7d046490ec97` |

## 不随仓库分发的文件

npm 包里的 `esnext/`、`declarations/`、`types/`、`peer/` 与全部 `.map` 文件非本用例所需，
不带；`vis-timeline-graph2d.esm.js` 与 `.mjs` 是模块入口，主站无构建、只需 UMD 单文件，不带。

## CSP 说明

主站 CSP 为 `script-src 'self'`（无 `unsafe-eval`）。8.5.6 的 standalone 产物打包了
`moment` 与 `vis-data`，构建产物内不出现 `new Function` / `eval(`（测试静态断言）。
懒加载走同源 `/static/vendor/vis-timeline/`，样式同为外部 CSS。

## 数据安全

`vis-timeline` 把事件的 `content` 当作 HTML 写入 DOM。组件 `timelineDemo.js` 在进入引擎前
统一 HTML 转义标题与分组名（先做纯文本事件；富文本后置），避免笔记/题目来源的文本注入。
