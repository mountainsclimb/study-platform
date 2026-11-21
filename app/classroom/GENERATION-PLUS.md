# AI 课堂 P1/P2 增量：深度交互模式生成 · 名册自动生成 · 可选配图

> 版本：prompt `classroom-2026-11-21-v2`（`app.classroom.prompts.PROMPT_VERSION`）；
> 多智能体名册 prompt 版本沿用 `classroom-multiagent-2026-11-21-v1`。
> 本文档记录本次三项能力的**接口增量、生成行为、样例与前端接入要点**；既有字段/行为不变，
> 其余接口见 `API-SAMPLES.md` 与 `MULTIAGENT-API.md`。

## 0. 能力总览

| 能力 | 触发 | 关闭/降级 | 主要新增 API 字段 |
|---|---|---|---|
| 深度交互模式 | 课程 `mode="interactive"`（`POST /courses` 时指定） | 标准模式行为不变 | 课程对象 `interactive_mode`；`config.interactive_mode`；`GET /meta.interactive_mode` |
| 名册自动生成 | `course` 生成任务（`stage=all` 确认大纲后 / `stage=scenes`） | 失败只记 `roster_warnings`，不阻塞；已有名册跳过 | 任务结果 `roster_generated` / `roster_agent_count` / `roster_warnings`；ZIP `manifest.agents` |
| 可选配图 | 用户连接具备图片生成能力时自动（可关闭）；或显式传参 | 无连接/关闭/失败 → 纯文本，不造假图 | `POST /courses/{id}/generate` 的 `images/images_per_scene/image_provider_id/image_model`；`GET /meta.images` |

## 1. 深度交互模式（interactive mode）

### 1.1 触发与大纲约束

课程创建时 `POST /api/classroom/courses` 传 `mode:"interactive"` 即进入深度交互模式
（`mode` 只允许 `standard` / `interactive`）。生成任务自动走「交互优先」分支：

- **大纲 prompt**：`interactive-first` 分支（对齐上游 `interactive-outlines`）——
  全课 **1-2 个 `interactive` 场景**（优先中段）、slide 只做导入/框架/过渡/总结、quiz 0-1 个；
  每个 interactive 场景必须给 `widget`，并附 widget 选择表（simulation/mindmap/game/code/visualization3d）。
- **大纲后处理**（服务端确定性约束，`generate._apply_interactive_outline_policy`）：
  - interactive 场景 **> 2 个** → 第 3 个起改为 `slide`（保留要点），`outline.warnings` 说明；
  - interactive 缺失 `widget` → 兜底 `simulation` 并告警；
  - **0 个 interactive** → 大纲警告「建议至少 1 个 interactive 场景」，仍按现有场景生成（不伪造交互页）；
  - `outline.interactive_mode=true` 写入大纲 JSON。
  - 用户在大纲编辑后确认（`confirm-outline`）同样过一遍该策略，超限仍会被裁剪/提示。
- **场景生成**：interactive 场景沿用既有真实生成器（自包含 HTML + 静态安全校验）；slide 在
  交互模式下追加**「动手试一试」提示块**：
  - slide prompt 要求模型输出 `id` 以 `hint_` 开头的底部文本元素；
  - 模型漏输出时服务端按大纲要点确定性补入（不编造交互结果），并写
    `content.interactive_hint = {element_id, text}` 与 `meta.interactive_hint`，
    `meta.warnings` 标注「已补入…提示块」；
  - 标准模式**不生成**提示块、prompt 也不含相关要求。

### 1.2 API 增量

课程对象（`GET /courses` 列表项、`GET /courses/{id}`、`GET /sessions/{id}.course`）新增：

```json
{
  "mode": "interactive",
  "interactive_mode": true
}
```

生成完成后课程 `config` 会写入：

```json
{
  "config": {
    "depth": "standard",
    "scene_types": ["slide", "quiz", "interactive"],
    "scene_count": 6,
    "prompt_version": "classroom-2026-11-21-v2",
    "interactive_mode": true
  }
}
```

`GET /api/classroom/meta` 新增能力位：

```json
{
  "interactive_mode": {
    "enabled": true,
    "mode": "interactive",
    "interactive_scenes_min": 1,
    "interactive_scenes_max": 2,
    "slide_hint": true
  }
}
```

slide 场景数据增量样例（`GET /courses/{id}.scenes[i]`）：

```json
{
  "type": "slide",
  "content": {
    "type": "slide",
    "schemaVersion": 1,
    "canvas": {
      "width": 1000,
      "height": 562.5,
      "elements": [
        {"id": "text_001", "type": "text", "left": 60, "top": 50, "width": 880, "height": 76,
         "content": "<p style=\"font-size:32px;\"><strong>光合作用</strong></p>"},
        {"id": "hint_001", "type": "text", "left": 60, "top": 466, "width": 880, "height": 46,
         "content": "<p style=\"font-size:16px;\"><strong>动手试一试</strong>：结合本页内容想一想：水是原料——你能自己举例或验证一次吗？</p>",
         "text": "动手试一试：结合本页内容想一想：水是原料——你能自己举例或验证一次吗？"}
      ]
    },
    "interactive_hint": {
      "element_id": "hint_001",
      "text": "动手试一试：结合本页内容想一想：水是原料——你能自己举例或验证一次吗？"
    }
  },
  "meta": {
    "interactive_hint": {"element_id": "hint_001", "text": "…"},
    "warnings": ["已补入「动手试一试」提示块（按大纲要点生成，未编造交互结果）"]
  }
}
```

## 2. 课程生成自动生成角色名册

### 2.1 行为

`course` 任务在**开始生成场景之前**自动补一次角色名册（多智能体课堂用）：

- 触发时机：`stage=all` 在用户 `confirm-outline` 后的续跑阶段 / `stage=scenes`；
  `stage=outline`（只生成大纲）不触发；单场景 `regenerate`（带 `scene_ids`）不触发；
- **幂等**：课程 `config.agents` 已有合法名册（用户手动生成过）→ 跳过，不重复调用模型；
- **失败不阻塞**：名册调用/校验失败只产生 `result.roster_warnings`，课程场景照常生成；
  用户之后可在课程页手动 `POST /courses/{id}/agents/generate`；
- SSE 阶段事件：`生成课堂角色名册`、`课堂角色名册已生成（N 个角色）`。

任务结果（`GET /api/ai/jobs/{id}.result`）增量字段：

```json
{
  "course_id": 1,
  "scene_count": 6,
  "generated_scene_count": 6,
  "failed_scene_ids": [],
  "summary": "课程生成完成：6 个场景可用",
  "roster_generated": true,
  "roster_agent_count": 4,
  "roster_warnings": [],
  "prompt_version": "classroom-2026-11-21-v2",
  "classroom_prompt_version": "classroom-2026-11-21-v2"
}
```

失败时（示例）：

```json
{
  "roster_generated": false,
  "roster_agent_count": 0,
  "roster_warnings": ["角色名册自动生成失败，已跳过（可在课程页手动生成）：ValueError"],
  "summary": "课程生成完成：6 个场景可用；角色名册自动生成失败，可稍后手动生成"
}
```

名册落库位置不变：`cls_courses.config_json.agents` + `agents_meta`
（见 `MULTIAGENT-API.md` §1.1）。

### 2.2 导出 / 导入携带名册

`GET /courses/{id}/export?format=zip` 的 `manifest.json` 增量：

```json
{
  "format": "zhixu-classroom-zip",
  "formatVersion": 1,
  "interactiveMode": true,
  "agents": {
    "teacher": {"id": "teacher_1", "name": "林老师", "role": "teacher", "persona": "…",
                 "avatar": "", "color": "#3b82f6", "priority": 10},
    "classmates": [
      {"id": "student_1", "name": "小舟", "role": "student", "persona": "…",
       "avatar": "", "color": "#10b981", "priority": 5}
    ]
  },
  "agentsPromptVersion": "classroom-multiagent-2026-11-21-v1",
  "capabilities": ["slide", "quiz", "interactive", "pbl", "tts", "agents", "interactive-mode"],
  "resources": [{"path": "assets/image/….png", "sha256": "…", "kind": "image", "assetId": 3}]
}
```

导入（`POST /api/classroom/import`）时：优先 `manifest.agents`，退化为 `course.json` 的
`config.agents`；一律经 `multiagent.normalize_agent_roster` 重新校验后才写入新课程
（非法名册丢弃，不阻断导入）。导入后的课程 `GET /courses/{id}/agents` 直接 `has_roster:true`。

## 3. 可选配图（image slot）

### 3.1 配置与能力探测

配图复用既有 `ai_providers` / `ai_models`（不新增配置页/表）：

1. 优先看模型元数据 `ai_models.caps_json` 的 `image` / `image_generation` / `images` 标记；
2. 否则对 **OpenAI 兼容**连接（`kind=openai|azure_openai`）按模型名启发式识别：
   `dall-e / gpt-image / flux / stable-diffusion / imagen / cogwin / kolors / seedream / qwen-image / image`。
3. `GET /meta.images` 返回当前用户可用的第一条配图连接（前端据此显示开关）。

接口（`POST /api/classroom/courses/{id}/generate`）新增参数：

```json
{
  "stage": "all",
  "provider_id": 1,
  "model": "your-chat-model",
  "images": true,
  "images_per_scene": 1,
  "image_provider_id": 2,
  "image_model": "gpt-image-1"
}
```

| 字段 | 说明 |
|---|---|
| `images` | `null`(默认)=自动（检测到可用图片连接就配图）；`false`=本次不配图；`true`=强制开启，无可用连接 → **422** 中文引导 |
| `images_per_scene` | 每个 slide 场景配图数量 `0-2`，默认 1；`0`/`images=false` 等同关闭 |
| `image_provider_id` + `image_model` | 显式指定配图连接（必须同时给，需属于本人连接且模型在连接内）；不传则自动选第一条可用连接 |

**无配置/关闭时不生成**：不写 `cls_assets`、不向 slide 追加图片元素、无「配图失败」告警（不造假图）。

### 3.2 生成产物

- 每个 slide 场景生成 0-2 张配图（仅 `slide`；quiz/interactive/pbl 不配图）；
- 图片字节按 **sha256 内容寻址**写入 `cls_assets(kind="image", sha256=…)`，同用户同图只存一份；
  图片文件存 `data/classroom/assets/image-<sha前32位>.<png|jpg|webp>`，通过既有
  `GET /api/classroom/assets/{id}` 提供（本人校验 + ETag/缓存头）；
- slide 元素引用：

```json
{
  "id": "img_001",
  "type": "image",
  "left": 640, "top": 108, "width": 300, "height": 192,
  "src": "/api/classroom/assets/3",
  "asset_id": 3,
  "asset_sha256": "82a58bf59c35bf52…",
  "alt": "教学配图",
  "fixedRatio": true
}
```

- 同时写 `content.image_assets` 与 `meta.images`（含 `asset_id/element_id/url/mime_type/alt/prompt/sha256`）：

```json
{
  "content": {
    "image_assets": [
      {"asset_id": 3, "element_id": "img_001", "url": "/api/classroom/assets/3",
       "mime_type": "image/png", "alt": "教学配图",
       "prompt": "为一节面向初二/生物 的课堂幻灯片绘制一张教学配图。…", "sha256": "…"}
    ]
  },
  "meta": {"image_count": 1, "images": ["…同上…"]}
}
```

- 配图 prompt（`prompts.build_image_prompt`）：教学示意插画风、浅色背景、2-4 种主色、构图留白；
  **禁止画面出现文字/字母/数字/公式/水印/logo、真实人物照片与实景照片、不适龄内容**。
- **失败降级**：单张失败只记 `meta.warnings`（「配图生成失败，本页保持纯文本（ValueError）」），
  场景仍 `ready`；全部失败也不影响课程完成。
- **取消 / 续跑**：配图在场景生成循环内执行；取消时已 ready 场景与其配图保留，续跑只补未完成场景，
  不会重复调用已完成的配图（同名场景内容不重建）。
- **幂等**：相同字节的配图内容寻址去重为一行 asset；重复生成（强制重新生成场景）不会产生重复素材行。

### 3.3 导出 / 导入与配图

- **离线 HTML / PPTX**：导出时把 slide 图片素材**内联为 data URI**（可离线打开，无外链）；
- **ZIP**：`resources` 登记图片素材（`kind:"image"`、`sha256`、`assetId`），场景 JSON 保留
  `/api/classroom/assets/{id}` 引用；导入时按 `manifest.resources[].assetId` 建立「旧素材 id → 新素材 id」
  映射，重写 slide image 元素的 `asset_id` 与 `src`（导入后图片可直接打开，不串到别人素材）。
- manifest `capabilities` 在存在图片素材时追加 `"images"`。

`GET /api/classroom/meta` 配图增量：

```json
{
  "images": {
    "enabled": true,
    "provider_id": 2,
    "provider": "示例图片连接",
    "model": "gpt-image-1",
    "max_per_scene": 2,
    "size": "1024x1024"
  },
  "limits": {
    "interactive_scenes_max": 2,
    "images_per_scene_max": 2
  }
}
```

无可用连接时 `images` 为 `{"enabled": false, "provider_id": null, "provider": null, "model": null,
"max_per_scene": 2, "size": "1024x1024"}`。

## 4. 前端接入要点

1. **交互模式开关**：创建课程时选 `mode:"interactive"`（既有字段）；课程对象新增
   `interactive_mode` 可直接用于徽标/提示；`GET /meta.interactive_mode` 说明能力与 1-2 个上限。
2. **大纲确认页**：interactive 模式大纲可能带 `warnings`（超限改 slide / 缺 widget / 0 个 interactive），
   请展示提示；确认后生成任务会再走一遍同样策略。
3. **播放器**：slide 若存在 `content.interactive_hint`（或 `meta.interactive_hint`），
   在幻灯片底部渲染「动手试一试」提示条（元素本身也会作为画布文本元素出现，二者选一渲染即可，
   建议用 `interactive_hint` 做强调样式，画布元素保留纯文本展示）。
4. **名册**：课程生成完成后 `GET /courses/{id}/agents` 通常已 `has_roster:true`；若 `result.roster_warnings`
   非空则显示「名册自动生成失败，点击生成」按钮（失败不阻塞成课）；`summary` 已含失败提示。
5. **配图**：`GET /meta.images.enabled` 决定是否显示「生成配图」开关（默认开启=自动）；
   生成请求给 `images/images_per_scene`；结果里 slide `content.image_assets` / `meta.images`
   可用于「配图」角标；图片 URL 直接 `<img src="/api/classroom/assets/{id}">`（同源带 cookie）。
6. **回放/导出**：图片元素渲染与既有 image 元素一致（`element.src` 为站内 URL）；
   离线 HTML/PPTX 已内联 data URI，ZIP 导入后图片引用会自动重映射。
7. **错误**：配图相关 422（无可用连接/参数不合法）为中文 `detail`；配图运行期失败只出现在
   `meta.warnings`，不影响课程完成，前端不要因此把课程标为失败。

## 5. 已知限制（P2 范围）

- 配图仅对 `slide` 场景生成，最多 2 张/场景，固定 `1024×1024` 输入、右侧栏布局（可能与模型
  自由排版元素重叠，前端可自行放大/浮层查看）；
- 不支持视频/图片编辑/图片审核流；`GET /assets/{id}` 仍是本人私有校验（分享课程包需走 ZIP）；
- 图片按「同用户 + sha256」跨课程去重复用一行 `cls_assets`（与 TTS 一致）；删除课程按
  `asset_references` 只回收无存活引用的素材（仍被其它课程引用时保留，并把归属迁移到存活课程），
  不再出现「删一门课误删另一门课共享素材」；
- 名册自动生成不覆盖用户手动编辑过的名册（只在缺失时生成）；单场景重新生成不触发名册/配图；
- 交互模式仍复用既有 5 种 widget prompt（simulation/mindmap/game/code/visualization3d），
  3D 为 Canvas 2D 透视投影子集（不引入外部库）。
