# AI 课堂 P1：交互场景沙箱 / PBL / 导出导入（实现说明）

> 适用对象：前端代理（课堂播放器、导出按钮）、后续接手后端的同学。
> 契约依据：`INTEGRATION-CONTRACT.md` §4.1（AI 课堂）；设计依据：`.omx/plans/integration/recon/openmaic.md`
> §2.6/§2.7/§4.5.4/§4.5.5/§6/§7。本文只描述 P1 增量；P0 的课程 CRUD、生成任务、会话、
> 测验判分、TTS、素材路由见 `app/classroom/API-SAMPLES.md`。
> 全模块路由索引见仓库根 `INTEGRATION-API-INDEX.md`；P1/P2 增量说明见 `GENERATION-PLUS.md`
> 与 `MULTIAGENT-API.md`。

## 1. 交付概览

| 文件 | 作用 |
|---|---|
| `app/classroom/interactive.py` | 交互 HTML 生成 prompt、静态安全校验、沙箱 CSP 常量、运行时事件上报路由 |
| `app/classroom/pbl.py` | PBL 项目生成 prompt 与严格结构校验 |
| `app/classroom/exports.py` | PPTX / 离线单文件 HTML / 课程 ZIP 导出（含可选 TTS） |
| `app/classroom/importer.py` | 课程 ZIP 导入（校验 + 建课程/场景/素材，不调用模型） |
| `app/classroom/generate.py` | **最小增量**：`scene_type=interactive/pbl` 从占位改为真实生成；meta 记录校验信息 |
| `app/classroom/api.py` | **最小增量**：交互 HTML 路由改用新 CSP；课堂追问 prompt 注入交互状态 |
| `app/classroom/__init__.py` | **增量接线**：include 新路由；`course` 任务注册行为不变 |

不新增任务类型：交互/PBL 仍由既有 `course` 任务逐场景生成（`run_course_job` 内按 `scene_type` 分派），
因此 `/api/ai/jobs/{id}`、SSE、取消、续跑、`awaiting_outline` 全部沿用 P0，无需前端改任务链路。

## 2. 新增/变更 API

### 2.1 `GET /api/classroom/scenes/{scene_id}/html`（P0 已有，P1 收紧 CSP）

返回交互场景 HTML（本人资源；他人/不存在 404；非 `interactive` 场景 404；无内容 404）。

响应头（沙箱关键）：

```http
Content-Type: text/html; charset=utf-8
Content-Security-Policy: default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data: blob:; font-src data:; media-src data: blob:; connect-src 'none'; frame-src 'none'; worker-src 'none'; object-src 'none'; base-uri 'none'; form-action 'none'
X-Frame-Options: SAMEORIGIN
Cache-Control: private, max-age=300
X-Content-Type-Options: nosniff
```

与 P0 的差异：**移除了 `script-src 'unsafe-eval'`**（生成侧已禁止 `eval/new Function`），
`script-src` 只剩内联脚本；`connect-src`/`frame-src`/`object-src` 全为 `'none'`，页面无法联网。

前端 iframe 必须（安全边界，不可放宽）：

```js
const frame = document.createElement('iframe');
frame.setAttribute('sandbox', 'allow-scripts');   // 绝不加 allow-same-origin
frame.setAttribute('referrerpolicy', 'no-referrer');
frame.src = `/api/classroom/scenes/${sceneId}/html`; // 推荐 src；也可用 srcdoc 加载同一 HTML
```

> `allow-scripts` + `allow-same-origin` 会让 iframe 取消沙箱，属于禁止项。
> 因为 iframe 是 null origin，父页面**不能**读它的 DOM；只能通过 `postMessage` 接收状态。

### 2.2 `POST /api/classroom/scenes/{scene_id}/interactive-event`（新增）

交互页运行时事件上报。页面内（生成时已内置的 `zhixuReport()`）通过
`window.parent.postMessage({source:'zhixu-interactive', type, payload}, '*')` 发给父页面；
父页面校验 `event.source === iframe.contentWindow` 与 `event.data.source === 'zhixu-interactive'` 后
转发到本接口（同源 `fetch`，带现有登录 Cookie/凭据即可）：

```js
window.addEventListener('message', (event) => {
  if (event.source !== frame.contentWindow) return;
  const message = event.data;
  if (!message || message.source !== 'zhixu-interactive') return;
  const type = message.type;                     // ready | error | state
  if (!['ready', 'error', 'state'].includes(type)) return;
  const payload = message.payload || {};
  fetch(`/api/classroom/scenes/${sceneId}/interactive-event`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      type,
      session_id: sessionId,                     // 必传才能把 state 落到会话 progress
      summary: String(payload.summary || '').slice(0, 500),
      state: payload.state && typeof payload.state === 'object' ? payload.state : null,
      message: type === 'error' ? String(payload.message || '').slice(0, 2000) : null,
    }),
  }).catch(() => {});
});
```

请求：

```json
{
  "type": "state",
  "session_id": 3,
  "summary": "学生把光照强度调到 80，光合速率 48",
  "state": {"light": 80, "rate": 48}
}
```

- `type` 白名单：`error` / `ready` / `state`，其他 → **422**「交互事件类型只能是 error / ready / state」。
- `state` 事件：写入会话 `progress.interactive = {scene_id, summary, state, updated_at}`，
  并追加到 `progress.interactive_events`（最多保留 12 条）；`state`/`summary` 都为空 → 422。
- `message` ≤ 2000 字，`summary` ≤ 500 字，`state` JSON ≤ **8KB**（超限 422「交互状态数据过大」）。
- 会话必须属于当前用户且与场景同课程，否则 404；场景必须是 `interactive`，否则 422。
- 不传 `session_id` 时不落库（`stored=false`），仅用于无会话的预览页。
- 响应：`{"ok": true, "type": "state", "stored": true, "session_id": 3, "scene_id": 12}`。

**课堂追问联动**：`POST /api/classroom/sessions/{id}/tutor` 会把当前场景的
`progress.interactive` 摘要（summary + state JSON，截断 3000 字）追加进 tutor prompt 的
“本页内容摘要”，AI 教师因此“看得见”学生刚才的操作（例如滑杆调到 80）。前端无需额外传参，
只要保证 `state` 事件带上 `session_id`。

### 2.3 `GET /api/classroom/courses/{course_id}/export?format=pptx|html|zip|package|pdf|mp4`

- 需登录；课程必须属于当前用户（他人/不存在 404）；课程无场景 → **422**「课程还没有场景，无法导出」。
- `format` 非法 → **422**「导出格式只能是 pptx / html / zip / package / pdf / mp4」。
- 可选 `include_tts=true`（默认 false）：导出前调用既有 `tts.synthesize` 为每个场景的 speech 旁白
  合成语音（内容寻址去重；未配置语音连接/合成失败自动回退浏览器语音，不报错）。
  仅对 `format=html|zip` 生效；`pptx` / `package` 不嵌入音频（package 的 PPTX 仍保留备注旁白文字）。
- 响应为文件流，响应头示例：

```http
Content-Type: application/vnd.openxmlformats-officedocument.presentationml.presentation
Content-Disposition: attachment; filename="classroom-export.pptx"; filename*=UTF-8''%E5%85%89%E5%90%88%E4%BD%9C%E7%94%A8.pptx
X-Zhixu-Export-Format: pptx
X-Zhixu-TTS: browser            # browser | assets
Cache-Control: private, no-store
```

中文文件名同时给 RFC 5987 `filename*=UTF-8''…` 与 ASCII fallback（`classroom-export.<ext>`）；
浏览器/前端按 `filename*` 取名即可（前端若用 `URL.createObjectURL(blob)` 下载，需自行从
`Content-Disposition` 解析或使用课程标题拼文件名）。

各格式：

- **pptx**：标题页 + 每个场景至少一页（`slide` 画布元素映射为文本框/形状/线条/表格/图表数据表/
  代码/图片；单页元素超过 10 个自动分页，标题带 `（1/2）`；LaTeX 以文本降级并在备注写明；
  quiz/interactive/pbl 为带标题与说明的占位页）；备注含旁白与导出说明。python-pptx 可直接打开。
- **html**：离线单文件播放器（内联课程 JSON + 内联 CSS/JS，无任何 http(s) 外链）；支持场景切换、
  字幕/浏览器 `speechSynthesis` 朗读、测验查看答案/解析、交互场景 `iframe sandbox="allow-scripts"
  srcdoc=…` 内嵌、PBL 只读展示；有 TTS 素材时以 `data:` URI 内嵌 `<audio>`（仍保留浏览器兜底）。
  体积超过 `MAX_STANDALONE_HTML_BYTES`（400MB）时自动改为 ZIP，响应头
  `X-Zhixu-Export-Fallback: zip`，ZIP 内 `EXPORT-NOTE.txt` 说明原因。
- **zip**：见 §4，可被 `/api/classroom/import` 原样导入。
- **package**：教学资源包（PPTX + 每个交互场景独立自包含 HTML + 课程 JSON + 中文 README），
  见 §4.3；`Content-Type: application/zip`，`Content-Disposition` 中文文件名为
  `资源包-{课程名}.zip`（ASCII fallback `classroom-export.zip`），响应头
  `X-Zhixu-Export-Format: package`。
- **pdf**：服务端 PDF 讲义（A4 横向；封面 + 每场景一页 + 有联网来源时一页），见 §10.1；
  `Content-Type: application/pdf`，中文文件名 `{课程名}.pdf`，响应头含 `X-Page-Count`；
  渲染引擎不可用/渲染失败 → **503** 中文引导（可改用离线 HTML + 浏览器打印）。
- **mp4**：下载最近一次成功生成的视频素材（先由 `POST /courses/{id}/video` 异步生成），见 §10.2；
  尚未生成 → **409** 中文提示；成功时 `Content-Type: video/mp4`，中文文件名 `{课程名}.mp4`，
  响应头含 `X-Zhixu-Asset-Id` / `X-Zhixu-Video-Duration` / `X-Zhixu-Video-Scenes` / `X-Zhixu-Video-Fps`。

### 2.4 `POST /api/classroom/import`（新增；同时保留契约路径 `POST /api/classroom/courses/import`）

`multipart/form-data`，字段名 **`file`**（`.zip`，≤ **200MB**；超限 413）。校验 manifest/结构/素材后，
为当前用户创建一门**新课程**（不调用模型）。成功响应：

```json
{
  "course": {"id": 7, "title": "光合作用", "status": "ready", "scene_count": 5, "...": "同 GET /courses/{id} 的 course"},
  "scene_count": 5,
  "imported_assets": 1
}
```

错误（统一 `{detail: 中文说明}`）：非 ZIP/缺 `manifest.json`/format 不符/版本过新/场景 JSON 损坏/
场景类型不支持/路径穿越/素材 sha256 不匹配/场景为空 → **422**；文件过大 → **413**。

## 3. 交互场景与 PBL 的内容结构

### 3.1 交互场景 `content`

```json
{
  "type": "interactive",
  "widget": "simulation",          // simulation | mindmap | game | code | visualization3d
  "height": 480,                   // 建议 iframe 高度 240-1200
  "html": "<!DOCTYPE html>…"       // 自包含单文件；已通过静态安全校验
}
```

场景 `meta` 新增字段（前端可用来提示/重试）：

```json
{
  "placeholder": false,            // P1 真实生成成功后为 false；P0 旧占位页才是 true
  "requires_generation": false,
  "interactive_checked": true,     // 已通过服务端静态安全校验
  "widget": "simulation",
  "html_bytes": 8421,
  "warnings": []
}
```

失败场景 `status="failed"`，`content={}`，`meta.error` 为失败原因（含“交互 HTML 未通过安全校验：…”），
`meta.requires_generation=true`；前端应提供“重新生成”入口，**不要**把失败场景当可用场景播放。

生成协议（服务端 prompt 已约束，前端无需关心）：模型输出严格 JSON
`{"widget": "...", "height": 480, "html": "..."}`，HTML 末尾内置固定上报脚本：

```html
<script>
(function () {
  function zhixuReport(type, payload) {
    try {
      window.parent.postMessage({ source: 'zhixu-interactive', type: type, payload: payload || {} }, '*');
    } catch (error) { /* 上报失败不影响页面交互 */ }
  }
  window.zhixuReport = zhixuReport;
  window.addEventListener('error', function (event) {
    zhixuReport('error', { message: String((event && event.message) || '脚本错误').slice(0, 500) });
  });
  window.addEventListener('unhandledrejection', function (event) {
    zhixuReport('error', { message: String((event && event.reason) || '异步错误').slice(0, 500) });
  });
  window.addEventListener('load', function () { zhixuReport('ready', {}); });
})();
</script>
```

除此之外，prompt 要求模型在初始渲染与每次状态变化时调用
`window.zhixuReport('state', {summary: '一句学生能读懂的状态', state: {...}})`。

### 3.2 PBL 场景 `content`

```json
{
  "type": "pbl",
  "project": {
    "title": "校园节能行动方案",
    "background": "学校电费上涨……",
    "goals": ["理解电功率与电能", "用数据支撑决策"],
    "roles": [{"name": "节能项目导师", "description": "陪你完成调研与方案。"}],
    "milestones": [
      {
        "title": "调研校园用电",
        "description": "记录三类设备用电。",
        "tasks": ["列出三类设备", "估算日均耗电量"],
        "deliverable": "用电调查表",
        "checkpoint": "数据有来源、计算可复核"
      }
    ],
    "rubric": [
      {"criterion": "数据真实性", "levels": [
        {"level": "优秀", "description": "数据有来源且计算无误"},
        {"level": "合格", "description": "数据基本合理"}
      ]}
    ],
    "mentor_hints": ["先想清楚哪些设备一直在耗电？"]
  }
}
```

严格校验：`title` 非空、`background` ≥10 字、`goals` 2-6 条、`roles` 1-6 个、
`milestones` 2-8 个且每个必须有非空 `tasks`、`deliverable`、`checkpoint`、`rubric` ≥1 条、
`mentor_hints` ≥1 条；不满足会带纠错提示重试一次，仍失败则该场景 `failed` 并记录原因。
场景 `meta` 额外记录：`meta.pbl = {title, milestone_count, goal_count, mentor_hint_count}`。
PBL 工作台（任务提交/导师对话/评估）不在 P1 范围，前端按只读结构展示即可。

## 4. 课程 ZIP 格式（`formatVersion=1`，可导入）

```
manifest.json
course.json
scenes/scene_001.json
scenes/scene_002.json
...
assets/<kind>/<sha256前16位><扩展名>      # 课程已落库素材（TTS 音频 / 配图）
EXPORT-NOTE.txt                          # 仅 HTML 体积超限降级时出现
```

`manifest.json` 示例（字段可能增加，前端/工具请容忍未知字段；P1/P2 增量见 §9）：

```json
{
  "format": "zhixu-classroom-zip",
  "formatVersion": 1,
  "app": "zhixu-study-platform",
  "exportedAt": "2026-11-21T01:40:00.000000+00:00",
  "courseId": 3,
  "course": {"title": "光合作用", "topic": "光合作用", "subject": "生物", "grade": "初中二年级", "mode": "interactive"},
  "interactiveMode": true,
  "agents": {"teacher": {"id": "teacher_1", "name": "林老师", "role": "teacher", "persona": "…",
                          "avatar": "", "color": "#3b82f6", "priority": 10},
             "classmates": [{"id": "student_1", "name": "小舟", "role": "student", "persona": "…",
                              "avatar": "", "color": "#10b981", "priority": 5}]},
  "agentsPromptVersion": "classroom-multiagent-2026-11-21-v1",
  "scenes": [
    {"order": 1, "id": 21, "title": "光合作用", "type": "slide", "path": "scenes/scene_001.json"}
  ],
  "resources": [
    {"path": "assets/tts/0123456789abcdef.mp3", "sha256": "…64位…", "size": 24112,
     "mimeType": "audio/mpeg", "kind": "tts", "assetId": 7},
    {"path": "assets/image/82a58bf59c35bf52.png", "sha256": "82a58bf5…", "size": 69,
     "mimeType": "image/png", "kind": "image", "assetId": 1}
  ],
  "capabilities": ["slide", "quiz", "interactive", "pbl", "tts", "agents", "interactive-mode", "images"],
  "promptVersion": "classroom-2026-11-21-v2"
}
```

`scenes/scene_NNN.json`：`{order, id, title, type, content, actions, meta}`（`id` 仅为源课程 id，
导入时会重新分配；`meta` 只保留安全可迁移字段）。导入侧会重新跑 DSL 归一化与交互 HTML 静态校验，
旧版占位内容走兼容模式并带 warning。

### 导入限制（防御性）

- 上传 ≤200MB；ZIP 条目 ≤2000；解压总大小 ≤500MB；单个素材 ≤100MB；场景 ≤100。
- 条目名禁止绝对路径/反斜杠/`..`/盘符（路径穿越 → 422）。
- `manifest.format` 必填 `zhixu-classroom-zip`；`formatVersion` 目前仅支持 1（更高版本 → 422）。
- 素材逐个校验 sha256（不匹配 → 422）；导入课程不调用任何模型，`config.document_ids` 被清空，
  不引用源账号资料。

## 4.3 教学资源包（`format=package`，单 ZIP 发放）

面向「把一节课分发给学生/教研组/离线归档」：**PPTX 课件 + 每个交互场景一份可双击打开的独立网页
+ 课程结构 JSON + 中文 README**，全部无外链、无需登录即可打开。

```
资源包-{课程名}.zip
├─ course.pptx                      # 复用 PPTX 生成逻辑（图片内联 data URI；交互/项目为说明页）
├─ README.txt                       # 中文说明：课程信息/文件说明/场景清单/打开方式/离线与安全/检查提示
├─ course.json                      # 课程+场景导出快照（与 ZIP 导出的 course.json 同一结构 build_snapshot）
├─ ets.json                         # 与 course.json 内容完全一致（便于不同工具约定读取）
└─ interactive/
   ├─ scene-03-光照强度模拟.html     # 每个 interactive 场景一份独立自包含 HTML（序号=场景 order）
   └─ scene-06-温度影响模拟.html
```

- **命名**：`scene-{order:02d}-{slug}.html`；`slug` 取场景标题（去路径/控制字符、空格转 `-`、≤30 字符），
  标题为空时回退 `scene-NN`；ZIP 条目名 UTF-8（中文文件名可正常解压）。
- **交互页结构**：`<!DOCTYPE html>` 单文件 + 顶部课程标题/第 N/M 页/场景标题/交互类型徽标 +
  工具栏（重新加载、全屏）+ `iframe sandbox="allow-scripts" srcdoc=…`（原场景 HTML 经转义后内嵌，
  与主站同一套隔离口径，**不带 `allow-same-origin`**）+ 底部状态栏（监听 `zhixu-interactive`
  `postMessage` 显示交互摘要）。无任何 `link/script src/http(s)` 外链。
- **course.json/ets.json**：`exports.build_snapshot(course, scenes)` 结果（`{formatVersion, exportedAt,
  course, scenes:[{id,order,title,type,content,actions,meta}]}`）；与 ZIP 导出共用结构，
  但**资源包不包含 `assets/**` 与 `manifest.json`**，因此不能直接走导入接口（要可导入请用 `format=zip`）；
  其中图片引用为站内素材地址（幻灯片图片仅在 PPTX 中内联）。
- **检查提示**：导出时扫描每个交互页与 README 的外链标记（允许 XML 命名空间 URI）。正常生成/导入的
  内容已通过静态安全校验，应无命中；若命中（历史/异常数据），不会静默丢弃，而会写入 README
  「五、检查提示」列出场景与标记，供打开前人工确认；交互场景缺少 HTML 时也会在 README 记录并跳过该页。
- **体积**：资源包不设单独上限（复用的离线 HTML 400MB 阈值仅用于提示）；拼接内容估算超过
  `MAX_STANDALONE_HTML_BYTES` 时，README 追加「体积提示」（建议改用 PPTX 或可导入 ZIP）。
- **鉴权/归属**：与其它导出格式一致（需登录、本人课程；他人/不存在 404、无场景 422）。
- **不含敏感信息**：资源包内无账号、Cookie、API Key、对话历史；`include_tts` 对 package 不生效
  （如需语音用 `format=html|zip&include_tts=true`）。

## 5. 静态安全校验清单（交互 HTML 生成后、入库前）

`interactive.static_check_interactive_html()` 逐条检查，命中任一即失败（`ClassroomSchemaError`，
由 `call_structured` 带纠错提示自动重试一次；仍失败场景落 `failed` 并写入 `meta.error`）：

1. 体积 ≤ **600KB**（UTF-8）；必须含内联 `<script>`、`zhixuReport`、`postMessage`、
   来源标识 `zhixu-interactive`。
2. 禁止任何 `http://` / `https://`（SVG/XML 命名空间 URI 白名单除外）、协议相对 `//` 资源、
   CSS `url(http…)`/`@import`、`<script src>`、`<link>`。
3. 禁止 `fetch`、`XMLHttpRequest`、`WebSocket`、`EventSource`、`sendBeacon`、`importScripts`。
4. 禁止 `eval`、`new Function`、字符串形式的 `setTimeout/setInterval`。
5. 禁止父窗口访问：`parent.document/location/frames…`、`top.document…`、`window.opener`；
   **仅允许** `window.parent.postMessage(...)` 做事件上报。
6. 禁止凭据/存储：`localStorage`、`sessionStorage`、`document.cookie`、`indexedDB`。
7. 禁止 `iframe/frame/frameset/object/embed/base/form`、`meta refresh`、`javascript:`/`vbscript:`、
   `data:text/html`、`window.open`、`location.href` 跳转。

运行期兜底：页面 `error`/`unhandledrejection` → `zhixuReport('error')` → 父页转 `interactive-event`，
播放器可展示“交互页面报错，可重新生成”；服务端 CSP 同时阻断网络请求，`sandbox` 阻断同源 DOM 访问。

> 说明：P1 不引入 KaTeX/Three.js/Chart.js/Tailwind 等第三方库（离线/零新增依赖要求），
> 交互页统一用原生 JS/CSS/SVG/Canvas；`visualization3d` 子集用 Canvas 2D 透视投影实现，
> `code` 子集只做代码阅读/填空/纠错（不执行任意用户代码，因此不需要 eval）。

## 6. 样例与验收结果（本人跑通，隔离数据目录）

样例命令（不联网、不调模型，直接落库样例课程后导出/导入）：

```powershell
python E:\responsibility\.omx\tmp\p1_classroom_verify.py
# 产物：E:\responsibility\.omx\tmp\p1-classroom-samples\
```

本次样例（1 门 5 场景课：slide/quiz/interactive/pbl/slide）：

| 格式 | 大小 | 校验结果 |
|---|---|---|
| `.pptx` | 45,831 B | python-pptx 打开成功；**6 页** = 标题页 + 5 场景；含课程/场景标题、quiz 在线说明、交互说明、PBL 里程碑、LaTeX 文本降级、图片/表格/代码；备注含旁白与“公式以文本降级” |
| `.html` | 27,420 B | 离线单文件；外部引用扫描 `http:// / https:// / <link / <script src / url(http) / @import` 均为 **0**；内联课程 JSON 5 场景可解析；含 `speechSynthesis` 兜底；交互 iframe `allow-scripts` 且无 `allow-same-origin` |
| `.zip` | 5,235 B | 7 个条目：manifest + course + 5 场景；`manifest.format=zhixu-classroom-zip`、`formatVersion=1`、5 场景 |
| 往返导入 | — | 新课程 id=2、5 场景类型/标题一致、交互 HTML 与 PBL 里程碑逐字节一致 |
| 交互 HTML 路由 | 706 B | `200`；CSP 含 `default-src 'none' / script-src 'unsafe-inline' / style-src 'unsafe-inline' / img-src data: blob: / font-src data: / connect-src 'none'`，无 `unsafe-eval`；`X-Frame-Options: SAMEORIGIN` |

自动化测试（全部 mock，不联网）：

```powershell
python -m pytest tests/test_classroom_interactive.py tests/test_classroom_export.py tests/test_classroom_import.py -q
# 27 + 8 + 10 = 45 passed
```

覆盖：外链/危险 API/父窗口/凭据/体积/上报脚本缺失等拒绝项、生成失败重试一次并记录原因、
CSP 与归属、事件白名单/progress/追问联动、PBL 严格结构、PPTX 打开与分页、
离线 HTML 无外链与 JSON 可解析、400MB 阈值降级 ZIP、ZIP 结构与素材 sha256、往返导入、
损坏包/路径穿越/版本过新/超限 422/413、导入不调用模型。

## 7. 前端对接要点（播放器/导出按钮）

1. **播放器交互场景**：按 §2.1 建 iframe（`sandbox="allow-scripts"`），用 `src` 加载
   `/api/classroom/scenes/{id}/html`；监听 `postMessage` 并转发 §2.2；把 `ready/error` 显示成
   状态条；离开场景时移除 iframe（或置空 `src`）避免后台运行。
2. **会话 id**：交互事件要带 `session_id`（`POST /courses/{id}/sessions` 返回值）才能写入
   `progress` 并被课堂追问使用；`GET /sessions/{id}` 的 `progress.interactive` 可直接回显。
3. **导出按钮**：`GET /courses/{id}/export?format=pptx|html|zip`，用 `fetch` + `response.blob()`
   （注意 HTTP 头 `Content-Disposition` 的中文名）；`format=html` 可能因体积降级返回
   `X-Zhixu-Export-Fallback: zip`，前端按响应 `Content-Type` 提示“已改为 ZIP 导出”。
4. **导入**：`POST /api/classroom/import`，`FormData.append('file', file)`，成功刷新课程列表；
   `detail` 可能是字符串（本模块统一中文 {detail: "..."}），展示即可。
5. **重新生成**：`interactive`/`pbl` 场景 `status=failed` 或 `meta.placeholder=true` 时，用既有
   `POST /courses/{id}/scenes/{scene_id}/regenerate` 重试（任务类型仍是 `course`）。

## 8. 已知边界 / 遗留

- PPTX 中图表导出为“数据表”而不是原生图表；`latex` 为文本降级（备注已说明），不是 OMML 公式。
- 交互页不加载第三方可视化库（3D 为 Canvas 2D 子集、代码页不执行用户代码），与上游全量体验有差距，
  属于 P1 的刻意裁剪；后续可按 recon §7.5 增加 `web/vendor/interactive/` 本地库白名单。
- `include_tts=true` 会为每个有旁白的场景发起一次 TTS 调用（最多场景数 12，串行），导出耗时随场景增加；
  失败自动回退浏览器语音。PPTX 不嵌入音频（python-pptx 音频支持有限）。
- P5 起视频导出已实现（服务端截帧 + ffmpeg 合成 MP4），见 §10；P1 的「无视频导出」结论仅指当时范围。
- `POST /courses/import` 与 `POST /import` 两个路径同时可用（契约与任务描述各写了一个，取并集）。
- 兼容说明：既有 `course` 任务的 pending/ready/failed 状态、`/api/classroom/*` 已发布字段均未改名；
  `interactive`/`pbl` 由 P0 占位改为真实生成后，只有旧数据仍保留 `meta.placeholder=true`。

## 9. P1/P2 增量（自动名册 / 深度交互模式 / 可选配图）

> 详细实现与接口见 `GENERATION-PLUS.md`；prompt 版本 `classroom-2026-11-21-v2`。
> 本节只说明对**导出/导入**的影响，前端无需改导出按钮。

1. **ZIP manifest 携带角色名册**：`manifest.agents`（完整 `{teacher,classmates}`）+ `agentsPromptVersion`；
   `course.json` 的 `config.agents` 同样存在。导入时优先 `manifest.agents`、退化为
   `course.json.config.agents`，经 `normalize_agent_roster` 重新校验后写入新课程；非法名册丢弃、不阻断导入。
   导入后 `GET /courses/{id}/agents` 直接 `has_roster:true`。
2. **`interactiveMode`**：manifest 新增布尔字段（由课程 `mode` 计算）；`capabilities` 追加
   `interactive-mode`；`course.json.config.interactive_mode` 亦为 `true`。导入保留课程 `mode`。
3. **配图素材与引用**：
   - `resources[]` 新增 `assetId`（源课程素材 id）；配图作为 `kind:"image"` 素材入 ZIP（`assets/image/…`），
     manifest `capabilities` 追加 `images`；
   - ZIP 内 scene JSON 保留 `{"type":"image","src":"/api/classroom/assets/{id}","asset_id":…}` 引用；
     导入时按 `resources[].assetId` 建立「旧 id → 新 id」映射，重写 image 元素的 `asset_id/src`
     （导入后图片可正常访问，不会串到他账号素材）；
   - **PPTX / 离线 HTML** 导出会把配图内联为 `data:image/...;base64,…`（离线可打开、无外链）；
     PPTX 不支持内联时在备注写明跳过；离线 HTML 体积仍受 400MB 上限约束（超限自动降级 ZIP）。
4. **深度交互模式 slide 的「动手试一试」提示块**：作为普通 `text` 元素（`hint_*`）存在于
   `content.canvas.elements`，因此 PPTX/HTML/ZIP 导出**自动携带**，无需额外处理；
   额外汇总字段 `content.interactive_hint`（`{element_id,text}`）仅在线端使用，导出时忽略。
5. **课程任务自动名册提示**：任务结果 `roster_generated/roster_agent_count/roster_warnings`
   仅影响生成/任务 UI；失败时课程仍可导出（此时 manifest 无 `agents`，导入后 `has_roster:false`，
   前端可再次手动生成名册）。

## 10. P5：服务端 PDF 讲义与 MP4 教学视频

> 实现：`app/classroom/exports.py`（PDF）、`app/classroom/video.py`（视频任务/截帧/ffmpeg）；
> 任务名 **`course-video`**，label「课堂视频导出」；不新增 pip 依赖（Playwright 已用于渲染，ffmpeg 为系统二进制）。

### 10.1 `format=pdf`（课堂讲义）

- 路由：`GET /api/classroom/courses/{id}/export?format=pdf`（需登录、归属校验同其它格式）。
- 生成方式：服务端把课程快照渲染成**打印版离线 HTML**（封面 + 每场景一页 + 有联网来源时附「参考来源」页；
  幻灯片按 1000×562.5 绝对定位，测验页附参考答案/解析，交互场景以说明文字代替），
  再经 `app.ai.render.html_to_pdf`（Playwright，A4 横向）转 PDF。
- 响应头：`Content-Type: application/pdf`；`Content-Disposition` 中文名 `{课程名}.pdf`（RFC 5987）；
  `X-Page-Count: <页数>`；`X-Zhixu-Export-Format: pdf`。
- 失败：渲染引擎不可用或渲染异常 → **503** `{detail: "服务端 PDF 导出不可用：…可改用「导出离线 HTML」后在浏览器中打印为 PDF。"}`。
- 限制：讲义是静态版式，交互场景与视频/动画不会出现在 PDF 中；字体使用系统字体（与前端一致的常见中文族）。

### 10.2 `course-video` 任务与视频接口

**`POST /api/classroom/courses/{id}/video`**（JSON 可选
`{mode?, fps?, max_scenes?, transition?, transition_ms?}`）→

```json
{
  "job": {"id": 12, "task": "course-video", "status": "queued", "stage": "queued", "request": {"params": {"course_id": 3, "fps": 12, "max_scenes": 12, "mode": "animated", "transition": "fade", "transition_ms": 400}}},
  "course_id": 3, "scene_count": 5, "render_scene_count": 5, "fps": 12,
  "mode": "animated", "transition": "fade", "transition_ms": 400,
  "capabilities": {"available": true, "detail": "可用：逐场景截图 + ffmpeg 合成 H.264 MP4（1280×720；static 静帧 / animated 逐帧 + xfade 转场）",
                   "render": {"available": true, "engine": "playwright"}, "ffmpeg": {"available": true, "path": "..."},
                   "limits": {"max_scenes": 12, "max_seconds": 600, "max_bytes": 209715200, "fps_default": 12,
                              "fps_min": 5, "fps_max": 24, "silent_scene_seconds": 3, "width": 1280, "height": 720,
                              "modes": ["static", "animated"], "mode_default": "static",
                              "transitions": ["none", "fade"], "transition_default": "fade",
                              "transition_ms_default": 400, "transition_ms_min": 100, "transition_ms_max": 1000,
                              "max_frames_per_scene": 300, "max_frames_total": 6000, "animated_audio": "aac"}},
  "limits": {"...": "同上"}
}
```

- 准入：课程无场景 422；课程场景数 > **12** → 422「超过视频导出上限」；`fps` 不在 5–24 → 422；
  `mode`/`transition` 非法值 → **422 中文说明**（`视频模式只能是 static…` / `场景转场只能是 none…`）；
  `transition_ms` 不在 100–1000 → 422；预计总时长 > **600 秒（10 分钟）** → 422；
  `animated` 额外预检单场景 ≤300 帧、总帧数 ≤6000 → 422 中文说明；
  同课程已有运行中 `course-video` 任务 → **409**；
  Playwright 或 ffmpeg 不可用 → **503** 中文原因；未配置任何 AI 连接 → 422
  （任务系统要求 `JobInput` 携带 provider/model，仅作为任务载体，导出过程不会调用模型）。
- 执行器（`render_course_video`）：离线播放器暴露 `window.__zhixuCapture = {ready, sceneCount, current,
  gotoScene(index), seekMs(t), sceneDurationMs(i)}`（`gotoScene` 进入 capture 模式：隐藏播放控件、画布固定
  1280×720 并铺满视口；`seekMs` 见 §10.4）；
  - **static（默认，向后兼容）**：逐场景 `gotoScene` 截图；有旁白（`config.narration` 记录的素材）
    按音频时长生成静帧片段并混入 AAC 音轨，无旁白场景默认 **3 秒**静帧（有任意旁白时其余片段补静音轨，
    整门课无旁白则输出纯视频）；ffmpeg 逐片段编码 H.264（`yuv420p`、faststart）→ concat demuxer `-c copy` 合并；
  - **animated**：按 fps 逐帧采样 `seekMs(t)` 截图（每场景帧数 = ceil(时长×fps)，单场景 ≤300 帧），
    场景时长以旁白音频为基准、无旁白时用播放器 actions 时间轴估算（≥1.5s）；每场景编码 JPEG 序列 +
    音轨（旁白不足补静音）为片段，`transition=fade`（默认 400ms）时用 ffmpeg `xfade` + `acrossfade`
    一次重编码合成，`transition=none` 时走 concat；**animated 恒带 AAC 音轨**（无旁白场景为静音）。
    详见 §10.4。
- 阶段（SSE `stage` 事件，中文）：`准备导出视频` → `视频时长约 N 秒，开始渲染画面` →
  `渲染第 n/N 场景：<标题>`（static）/ `渲染第 n/N 场景 · 第 k/m 帧`（animated）→
  `合成视频片段 n/N：<标题>` → `合成视频（转场）：N 个场景 · fade 400 ms`（animated fade）/
  `合并视频片段` → `保存视频素材` → 完成；`stage` 数据含 `done/total`。
- checkpoint / 取消：static 每帧落盘后写 `checkpoint_json`（场景清单 + 内容摘要 + 帧文件），animated
  每个场景的帧集完成后写 `checkpoint_json`（`anim.scenes[scene_id]` = 内容摘要 + 旁白音频摘要 + fps +
  时长/帧数 + 帧目录）；取消时删除半成品与**未完成场景的帧目录**，已完成场景帧保留，
  `POST /api/ai/jobs/{id}/resume` 续跑复用（场景内容/旁白音频变化会自动重截，animated 按场景复用）。
  失败同样保留 checkpoint 供续跑。
- 产物：`cls_assets`（`kind='video'`、`mime_type='video/mp4'`、sha256 内容寻址去重），
  下载地址 `/api/classroom/assets/{id}`；`meta` 含
  `duration_seconds/scene_count/fps/has_audio/narrated_scene_count/size/truncated`，animated 另含
  `mode/animated/frame_count/transition/transition_ms/fallback_reason`。
- 限额：场景 **≤12**、总时长 **≤600 秒**、产物 **≤200MB**；animated 单场景 **≤300 帧**、总帧数 **≤6000**；
  转场 100–1000ms（默认 400ms fade）；产物超限时任务失败并保留帧，不落库。
- 降级：`mode='animated'` 但离线播放器缺少 `seekMs/sceneDurationMs`（旧模板/旧课程 HTML）或首次调用失败时，
  自动回退 static（任务结果 `mode_used='static'`、`fallback_reason` 中文原因、`warnings` 追加提示），
  视频仍会生成；其它渲染错误（浏览器崩溃等）不降级，按失败处理。

**`GET /api/classroom/courses/{id}/video`** → 导出能力 + 最近产物 + 最近任务：

```json
{
  "course_id": 3,
  "capabilities": {"pdf": {"available": true, "detail": "可用：服务端渲染 A4 横向 PDF 讲义"},
                   "video": {"available": true, "detail": "...", "render": {"...": "..."}, "ffmpeg": {"...": "..."}},
                   "limits": {"...": "同 10.2"}},
  "limits": {"...": "..."},
  "asset": {"id": 9, "kind": "video", "mime_type": "video/mp4", "url": "/api/classroom/assets/9",
            "meta": {"duration_seconds": 36.0, "scene_count": 5, "fps": 12, "has_audio": true, "size": 812345,
                     "mode": "animated", "animated": true, "frame_count": 420,
                     "transition": "fade", "transition_ms": 400, "fallback_reason": ""}, "created_at": "..."},
  "latest_job": {"id": 12, "status": "completed", "stage": "completed", "error": "", "has_checkpoint": false,
                 "result": {"asset_id": 9, "scene_count": 5, "fps": 12, "duration_seconds": 36.0, "size_bytes": 812345,
                            "mode": "animated", "mode_used": "animated", "animated": true, "frame_count": 420,
                            "transition": "fade", "transition_ms": 400, "fallback_reason": ""}},
  "scene_count": 5
}
```

**`GET /api/classroom/courses/{id}/export?format=mp4`**：返回最近一次成功视频文件流；
未生成 → **409**「该课程还没有生成 MP4 视频：请先在「导出课堂」中选择「生成 MP4 视频」…」；
文件丢失 → 503。成功头：`Content-Type: video/mp4`、中文 `Content-Disposition`、
`X-Zhixu-Asset-Id` / `X-Zhixu-Video-Duration` / `X-Zhixu-Video-Scenes` / `X-Zhixu-Video-Fps`。

### 10.3 前端对接

- 导出对话框（`web/js/components/classroomShared.js::openExportDialog`）新增「导出 PDF」「生成 MP4 视频」按钮：
  打开时 `GET /courses/{id}/video` 探测能力，`capabilities.pdf/video.available=false` 时**禁用并显示原因**；
  已有产物显示「最近视频 + 下载已有视频」；MP4 点击 → `POST /courses/{id}/video` → 用既有
  `core/jobs.js::mountJob` 显示任务卡（阶段/进度/取消）→ 完成自动 `GET export?format=mp4` 下载；
  失败/取消且 `has_checkpoint` 时显示「续跑上次视频任务」。
- 任务中文名 `course-video` 在 `classroomShared.js` 里补进 `core/jobs.js` 的 `taskNames` 注册表
  （该文件为 Lead 独占，未修改其实现）。
- 已知边界：static 模式视频为**静态场景 + 旁白音轨**；animated 模式视频为**逐帧动画 + fade 转场**，
  但动画只覆盖播放器 actions 里的聚光灯/激光/高亮/白板/字幕（幻灯片元素本身不做位移/缩放动画），
  且白板公式以斜体文本降级（离线播放器不加载 KaTeX）；quiz/pbl/interactive 场景以播放器中的静态
  画面入镜（仅字幕随 t 推进），过长的卡片内容会被 1280×720 视口裁切；不含浏览器语音
  （static 无 TTS 素材时无音轨，animated 恒有静音 AAC 音轨）。

### 10.4 逐帧动画 + 场景转场（animated，P5 增强）

> 仅后端与导出模板：`app/classroom/exports.py`（离线播放器 capture API 扩展）、
> `app/classroom/video.py`（animated 截帧/合成/降级/限额/checkpoint）；不修改前端 JS 组件、不新增 pip 依赖。

**① 导出模板 capture API（`window.__zhixuCapture`，离线单文件播放器内联 JS）**

| 成员 | 说明 |
|---|---|
| `ready` | 首帧渲染完成后置 `true`（既有） |
| `sceneCount` / `current` | 场景总数 / 当前场景下标（既有） |
| `gotoScene(index)` | 切场并进入 capture 模式（隐藏播放控件、画布固定 1280×720；既有） |
| `seekMs(t)` | 把当前场景按 actions 时间轴**确定性**推进到 t 毫秒：聚光灯/激光/高亮取「t 之前最近一次」动作后按 t 计算呼吸/点移动样式，白板按每个 op 的起止时间渐进绘制（文字逐字显示、线条/箭头生长、形状淡入），字幕按 speech 文本进度逐字显示（`#capture-subtitle` 底部浮层）；同一 t 反复调用结果一致 |
| `sceneDurationMs(i)` | 场景时长估算（毫秒）：有旁白音频快照注入 `scene.durationMs` 时取音频时长；否则按 actions 估算（speech = 800ms + 每字 250ms，最少 1500ms；spotlight/laser/highlight 各 700ms；whiteboard 每 op 260ms 收敛到 700–3000ms；play_audio 1000ms；next_scene/show_slide 不占时间），场景总时长最少 **1500ms** |
| `timeMs` | 最近一次 `seekMs` 的时间（便于调试） |

- 正常播放完全不受影响：效果层 `.fx-layer` 默认 `display:none`，只有 `seekMs` 调用时才写入样式；
  字幕浮层只在 capture 模式创建。
- 动画参数与 `video.py::estimate_scene_duration_ms` **同一套常量**（speech/base/visual/whiteboard/audio），
  两边改动需同步（route 预检与播放器采样一致）。

**② video.py animated 流程**

1. 校验参数（route `_validate_video_params` + 任务内二次校验）：`mode ∈ {static, animated}`（默认 static）、
   `transition ∈ {none, fade}`（默认 fade）、`transition_ms ∈ [100, 1000]`（默认 400）。
2. 旁白音频抽取/ffprobe 时长（与 static 一致）：有旁白场景以音频时长为时间基准；无旁白场景用
   `sceneDurationMs(i)` 估算；总时长 ≤600s。
3. 逐场景 `gotoScene(i)` → 按 `帧数 = ceil(时长 × fps)` 逐帧 `seekMs(round(k×1000/fps))` → JPEG 截图
   （`frames/sNNN/00000.jpg…`）；每帧后等一次 rAF 确保样式/绘制提交；单场景 ≤300 帧、总帧数 ≤6000。
4. 每场景：`ffmpeg -framerate fps -i frames/sNNN/%05d.jpg` +（旁白音频或 `anullsrc` 静音）→
   H.264/AAC 片段（`-af apad` 保证音频不足补静音，`-t 帧数/fps` 保证音画同长）。
5. `transition=fade`（且场景数 ≥2）：`filter_complex` 链式 `xfade=transition=fade:duration=T:offset=O`
   （offset 按前序片段累计时长推算）+ `acrossfade=d=T` 音频交叉淡化一次重编码；转场时长自动收敛到
   最短场景的一半以内（`effective_transition_seconds`），实际值写入结果 `transition_ms`。
   `transition=none` 或单场景：concat demuxer `-c copy` 直接拼接。
6. 落库 `cls_assets(kind='video')`，`meta` 记录 `mode/animated/frame_count/transition/transition_ms/fallback_reason`。

**③ 降级与边界**

- 老模板/老课程 HTML 没有 `seekMs/sceneDurationMs` 时：`_capture_animated_frames` 抛
  `AnimatedUnavailable` → 自动回退 static，任务结果 `mode_used='static'`、`fallback_reason` 写明缺失接口，
  `warnings` 追加提示；视频仍会生成（`fallback_reason` 同时写入素材 meta）。
- 浏览器崩溃/截图失败等真实渲染错误不降级（避免掩盖环境问题），按任务失败处理并保留 checkpoint。
- animated 恒输出 AAC 音轨：无旁白场景为静音，保证与旁白场景/转场片段结构一致、播放器兼容。
- 白板 `coord_space='unit'`（0–1 归一化，多智能体讨论脚本）与像素坐标（1000×562.5，生成管线）都支持。
- 实测（真实 Playwright + ffmpeg 9.0.2）：2 个含 speech+spotlight 的短场景 → animated 3.750s，
  h264 + aac、1280×720、8fps；xfade 边界帧可见两场景交叉淡化；白板/字幕按帧推进。
