# 知序 P6b · 自由画布幻灯片编辑器（editor/）

知序课堂「免费画布」之外的另一种幻灯片编辑方式：在 1000×562.5 的幻灯片画布上自由拖拽、
缩放、旋转元素，编辑富文本、形状、图片、表格、图表、线条与公式，保存回知序后端后
播放器/导出立即生效。

- 父页入口：课堂播放器/生成工作台页头「自由画布编辑」按钮（`web/js/components/classroomCanvas.js`）
- 编辑器本体：本目录（React 19 + Vite 6），构建到仓库内 `web/editor/`（产物入库，离线部署无需 Node）
- 数据接口：`GET/PATCH /api/classroom/courses/{course_id}/scenes/{scene_id}/content`（version CAS）

## 采用路线：A（vendored OpenMAIC 编辑器）

实现前先评估了任务给出的两条路线。**实际采用路线 A**：把 OpenMAIC 的 `dsl / geometry /
renderer / editor` 四个包源码 vendored 到 `editor/vendor/openmaic/`，用 Vite alias 指向各包
`src/index.ts`，`editor/src/App.tsx` 直接挂载上游 `@openmaic/editor/ui` 的
`EditableSlideCanvasWithUI`（自由画布交互、选择/拖拽/缩放/旋转、吸附参考线、文本
ProseMirror 编辑器、格式工具条、插入工具栏、右键菜单、对齐/组合/层级/剪贴板快捷键都由上游提供），
宿主侧（本目录 `src/`）负责知序的数据契约、保存/冲突/草稿、图层面板、属性面板与 iframe 通信。

未采用路线 B（完全自研画布）：路线 A 在 90 分钟内即跑通构建与真实编辑（见 git 历史/验证记录），
上游已经提供 PPTist 级交互，自研只会更慢且功能更少。

### 上游来源与许可

| 项目 | 值 |
| --- | --- |
| 上游仓库 | https://github.com/THU-MAIC/OpenMAIC |
| 只读副本路径 | `E:\responsibility\OpenMAIC\packages\@openmaic\{dsl,editor,renderer,geometry}` |
| 上游 commit | `18b68aea3a87d7626e805b136d9b908eb277b00a` |
| 许可 | MIT，Copyright (c) 2026 THU-MAIC |
| vendored 包 | `@openmaic/dsl@0.13.0`、`@openmaic/geometry@0.1.0`、`@openmaic/renderer@0.2.3`、`@openmaic/editor@0.0.13` |

- 各包 `LICENSE` 保留在 `editor/vendor/openmaic/<pkg>/LICENSE`；来源与文件清单位于
  `editor/vendor/openmaic/VENDORED.json`。
- 对上游源码**只**做了 import 路径改写（ESM 相对导入的 `.js` 扩展名去掉，交给 Vite 解析
  `.ts/.tsx`），运行语义未修改；`editor/scripts/vendor-openmaic.mjs` 可重复生成。
- 运行时不加载任何外网资源：没有引入上游 `fonts.css`（它引用 `file.maic.chat` 的外链字体），
  KaTeX 字体从本地 `node_modules` 打包；`shiki` 语法高亮引擎（全量语言包约 9.5MB）以
  `editor/src/stubs/shiki.ts` 轻量替代，代码元素回退为纯文本显示。

## 构建

要求：Node ≥ 20、pnpm 10（本机验证版本 Node v24.13.0 / pnpm 10.34.9）。首次构建需要联网安装
npm 依赖；**运行时**不依赖外网。

```bash
cd editor
pnpm install

# 可选：从 OpenMAIC 只读仓库重新 vendoring（默认 ../OpenMAIC，也可传路径）
pnpm run vendor

# 构建到 ../web/editor（base './'、emptyOutDir）
pnpm run build

# 类型检查（CI 必跑；见下）
pnpm run typecheck
```

**浏览器支持范围**：由 `editor/package.json` 的 `browserslist` 与 `editor/tsconfig.json`
的 `lib: ES2022` 共同声明——Chrome/Edge ≥ 93、Firefox ≥ 92、Safari/iOS ≥ 15.4。
vendored OpenMAIC 源码使用 `Object.hasOwn`（ES2022）与 `String.replaceAll`（ES2021），
这两个内置对象不随 Vite 的 `build.target` 转译；低于上述版本会运行时报错。
`vite.config.ts` 的构建 target 仍为 `es2020`（语法降级），不改运行语义。

**发布前检查**（类型检查已纳入 `scripts/ci_accept.ps1`）：

```bash
cd editor && pnpm run typecheck            # tsc --noEmit，必须 0 错误
# 仓库根目录：重新构建并与入库产物 web/editor 比对，防止「改源码忘重构建」
python scripts/check_editor_build.py --build
```

`editor/package.json` 的 `build` 脚本即 `vite build`；Vite 配置见 `editor/vite.config.ts`：

- `base: './'`（静态前缀任意）
- `build.outDir: '../web/editor'`、`emptyOutDir: true`
- `@openmaic/*` alias 指向 `editor/vendor/openmaic/*/src/index.ts`
- Tailwind 4 只用于生成上游 renderer 的 utility class（`@source '../vendor/openmaic/renderer/src'`），
  上游编辑器 UI 样式由包内 `EDITING_UI_STYLES` 自行注入

### 构建产物体积（2026-11-21，`web/editor` 共 69 个文件）

| 分类 | 原始 | gzip |
| --- | --- | --- |
| 合计 | 3,464,375 B（3.30 MiB） | 1,638,928 B（1.56 MiB） |
| JS（7 个 chunk） | 2,341,928 B | 759,363 B |
| CSS（2 个） | 48,643 B | 12,770 B |
| KaTeX 字体（59 个 woff2/woff/ttf） | 1,072,948 B | 1,072,948 B（已压缩格式） |

体积要点：`charts-*.js`（ECharts，1.14MB）和 `katex-*.js`、`prosemirror-*.js` 已拆成独立 chunk；
ECharts 仅在页面存在图表元素时动态加载。首屏实际加载约 1.16MB（gzip 约 380KB）。
产物 < 8MB，无需额外体积豁免。

## 与知序的集成

### iframe 挂载方式：为什么用 srcdoc

知序平台全局安全响应头（`app/main.py::security_headers`）是
`Content-Security-Policy: ... frame-ancestors 'none'` + `X-Frame-Options: DENY`，
浏览器会拦截「iframe 直接加载 `/static/editor/index.html`」（实测 `ERR_BLOCKED_BY_RESPONSE`）。
本任务不改 Lead 负责的 `app/main.py`，因此父页组件采用等价方案：

1. `fetch('/static/editor/index.html')` 读取构建入口；
2. 把入口里的相对 `<script src>` / `<link href>` 绝对化为 `/static/editor/assets/...`
   （`absolutizeEditorHtml`，平台 CSP `base-uri 'none'` 不允许 `<base>`）；
3. 通过 **iframe `srcdoc`** 挂载：srcdoc 文档继承父页 origin，cookie / localStorage /
   `fetch('/api/...')` 都按同源工作，脚本与 CSS 仍从站内静态目录加载；
4. 参数经 iframe `name`（`zhixu-canvas:{"course":1,"scene":2}`）传给编辑器（srcdoc 没有查询串）；
   直接打开 `/static/editor/index.html?course=&scene=` 时仍按 URL 查询参数工作（本地调试/独立部署）。
5. 若部署环境放宽了 iframe 限制，`buildEditorSrcdoc()` 失败时会回退为直接 `iframe.src=` 加载。

### postMessage 协议（同源）

父页 `web/js/components/classroomCanvas.js` 与编辑器 `editor/src/protocol.ts` 中的常量必须一致
（`tests/test_classroom_canvas.py` 校验）：

| 方向 | 消息 | 含义 |
| --- | --- | --- |
| 编辑器 → 父页 | `{type:'zhixu-slide-ready'}` | 编辑器挂载完成 |
| 编辑器 → 父页 | `{type:'zhixu-slide-saved', version, close}` | PATCH 保存成功；`close=true` 表示「保存并关闭」 |
| 编辑器 → 父页 | `{type:'zhixu-slide-dirty', dirty}` | 未保存变更状态 |
| 编辑器 → 父页 | `{type:'zhixu-slide-close'}` | 用户关闭编辑器 |

父页校验 `event.origin`（srcdoc 文档为 `"null"`，显式允许）与 `event.source === iframe.contentWindow`。
保存成功后父页把学习会话进度指向刚编辑的场景（`PATCH /api/classroom/sessions/{id}`）再重渲染
播放器，因此关闭后直接展示修改后的幻灯片。

### 数据契约（`editor/src/types.ts`）

知序后端持久化结构：

```json
{"type":"slide","schemaVersion":1,"canvas":{"width":1000,"height":562.5,"theme":{...},"elements":[...]}}
```

`@openmaic/dsl` 的 `SlideContent` 要求 `canvas` 是带 `id/viewportSize/viewportRatio/theme` 的
`Slide`。适配层负责：

- 载入：补 `slide.id`、`viewportSize`、`viewportRatio`，补元素 `rotate`、表格 `cellMinHeight`
  与单元格 id、code `lines`；公式元素按 `latex` 源码用 KaTeX 重算 `html`（后端不持久化
  `html`，renderer 需要它才能显示公式）；无法转换的元素跳过并显示「载入提示」；
- 保存：回到后端规范结构（`width/height/theme/elements`，纯色 `background` 一并回传），
  后端 `schema.normalize_slide_content` 会再归一化/钳制/净化；
- 元素上限与后端一致：单页最多 24 个（超出时忽略新增并提示）。

## 功能清单

宿主 chrome（`editor/src/App.tsx` / `panels.tsx`）：

- 顶部：场景标题、版本徽标、保存状态、撤销/重做（≥120 步历史）、缩放（适应窗口/100/90/75/50%）、
  插入形状（矩形/圆角矩形/圆形/三角形）、图层/属性面板开关、保存 / 保存并关闭 / 关闭；
- 画布（上游）：点选/框选/Shift 多选、8 向缩放手柄、旋转手柄、拖拽移动、吸附对齐参考线、
  双击编辑文字（ProseMirror，支持加粗/斜体/下划线/字号/颜色/对齐/列表）、形状内文字、
  表格单元格编辑、插入文本/图片/表格/图表/线条/背景/公式、元素工具条、右键菜单
  （复制/粘贴/剪贴/删除/锁定/组合/层级/对齐）、Ctrl+C/V/X/A/G/L 快捷键；
- 右侧面板：图层面板（全部元素、点选、上移/下移/置顶/置底、复制、锁定、删除）与
  属性面板（X/Y/宽/高/旋转；形状填充；线条颜色/线宽/线型；图片站内地址；LaTeX 源码；
  文本默认字体色；页面背景色）；
- 保存：PATCH 带 version；409 显示冲突横幅（重新载入服务端版本 / 以最新版本覆盖保存）；
  401 显示登录过期遮罩；422 显示服务端中文原因；保存后用服务端归一化内容回填本地；
- 草稿：编辑后 800ms 防抖写 localStorage，重开时提示「恢复/丢弃」；保存成功自动清除；
- iframe 通信、加载/错误提示、移动端「建议使用电脑」提示。

## 已知限制 / 未做项

- **组合/锁定/元素阴影/圆角等扩展属性只在本会话生效**：后端 `normalize_slide_content` 是白名单式
  归一化，`groupId`/`lock`/`shadow` 等字段不会被持久化，保存后会被收回（本地编辑/撤销仍可用）。
- **图片只能使用站内相对地址**（如 `/api/classroom/assets/12`）：后端离线策略会丢弃外链与 data URL；
  离线课堂没有通用的「编辑器图片上传」后端接口，因此没有实现本地上传。
- **视频/音频元素不支持插入/编辑**：后端 slide schema 白名单不含 video/audio；插入工具栏已隐藏这两项。
- **文本行内公式在保存时会被后端 HTML 净化去掉**（`data-inline-math` 等属性不在白名单）；
  独立「公式」元素（latex）不受影响。
- 缩放只提供「适应窗口/缩小」几档，不支持放大后平移画布（上游画布容器 `overflow:hidden`）。
- 代码元素回退为纯文本（离线构建不打包 shiki 语法高亮引擎）。
- 未实现多页/多场景画布管理（一次编辑一页，场景选择在父页入口栏）。
- 保存后的服务端归一化可能微调坐标/颜色（如坐标钳制到画布内），界面会以服务端结果回填。

## 测试与验证

- 后端契约与产物检查：`python -m pytest -q tests/test_classroom_canvas.py`（复用 P6a 的
  version CAS / 归一化 / 权限思路）。
- 前端语法：`node --check web/js/components/classroomCanvas.js`（测试里有 Node 时自动执行）。
- 隔离实例人工/Playwright 验收（2026-11-21，端口 8851、r3 题库、`demo/demo-pass-12345`）：
  播放器 → 自由画布编辑（iframe 挂载、无控制台错误）→ 拖动形状 → 保存并关闭 →
  播放器刷新显示新坐标（截图 `screenshots/integration/canvas-editor-*.png`）。
- 全量回归：`python -m pytest -q` + `python -m compileall -q app` + 全部 `web/js` 的 `node --check`。

## 维护：升级 vendored 上游

1. 把 OpenMAIC 只读仓库切到目标 commit；
2. `cd editor && node scripts/vendor-openmaic.mjs E:/path/to/OpenMAIC`（会重写 `VENDORED.json`）；
3. `pnpm install`（若上游依赖变化）→ `pnpm run build`；
4. 跑 `python -m pytest -q tests/test_classroom_canvas.py` 与隔离实例验收；
5. 更新本 README 的 commit / 体积记录。
