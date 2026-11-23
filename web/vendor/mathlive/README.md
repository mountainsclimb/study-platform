# MathLive（可视化公式编辑）

| 项目 | 内容 |
|---|---|
| 名称 | MathLive |
| 版本 | 0.111.4 |
| 上游 | <https://github.com/arnog/mathlive>（npm 包 `mathlive`） |
| 许可证 | MIT License（全文见 `LICENSE.txt`） |
| 获取方式 | `npm pack mathlive@0.111.4`，取包内 `mathlive.min.js`、`mathlive-static.css`、`fonts/*.woff2` 与 `LICENSE.txt`，未修改 |
| 用途 | `web/js/components/formulaEditor.js`：拍题校对、题目表单与笔记编辑器的可视化公式编辑；首次打开时懒加载 |
| 分发方式 | 随仓库分发（`web/vendor/**` 已由构建指纹与 `.dockerignore` 保留） |

## 文件校验

| 文件 | 大小（字节） | SHA-256 |
|---|---|---|
| `mathlive.min.js` | 847,547 | `9967efce1bf98421748a8f3541739d700bf1c0beb6eb7f4c0765bd512ba65388` |
| `mathlive-static.css` | 13,205 | `c9698f6b0c60482e195020660f7074aa65cf049a1b1160a8729294c21c4bc868` |
| `LICENSE.txt` | 1,099 | `3caebd18611959ca725ab976a004d408017eb1d7093d6c9b9b09c5a4790eaff8` |
| `fonts/*.woff2`（20 个） | 共 259,792 | 见各文件；均为 KaTeX 字体，SIL OFL 1.1 |

## 边界

- 只引入浏览器产物与字体，不引入 npm 包的 `sounds/`（按键音效），也不引入 SSR/Vue 入口。
- 主站 CSP 为 `script-src 'self'`；本目录内的脚本与样式均为同源资源，不依赖 `eval`。
- 字体与 CSS 随 `mathlive-static.css` 内的相对路径 `fonts/...` 加载，移动/删除目录时需整体保留。
- 升级时替换文件并同步更新本表的版本、大小与 SHA-256；`tests/test_oss_vendor_assets.py` 会逐项校验。
