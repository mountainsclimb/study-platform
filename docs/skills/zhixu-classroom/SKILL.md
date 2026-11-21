---
name: zhixu-classroom
description: 在「知序」平台上操作 AI 课堂（课程创建、异步成课、大纲确认、会话/多角色讨论、导出/导入）的 SOP。适用于外部 Agent 工作台（OpenClaw / Codex 等）替用户完成「做一节课 / 导出一份课堂资源包 / 把课程包导回」等任务；不适用于无登录态的匿名调用。
---

# 知序 AI 课堂 · Agent Skill（SOP）

> 事实基线（与本仓库 `study-platform/INTEGRATION-API-INDEX.md` §2/§6 一致；字段以该索引与
> `app/classroom/API-SAMPLES.md` 为准）：
>
> 1. **没有对外 API Token**。知序目前只支持 **浏览器会话 Cookie**：
>    `study_session`（HttpOnly、SameSite=Strict、7 天），由 `POST /api/auth/login` 或浏览器登录后签发。
>    外部 Agent 必须运行在**本机同源环境**（例如本机浏览器扩展、带该 Cookie 的本机客户端、或用户在
>    本机登录后把会话交给 Agent 的受控环境），**不得要求用户把 Cookie/密码写进对话或日志**，也不得
>    尝试绕过鉴权。`Authorization: Bearer` 支持不存在——不要伪造该头。
> 2. **不伪造**：没有可用模型连接时，生成接口会返回 `422` 中文引导；把失败/占位内容当成功、自己编造
>    课程内容或场景，都是错误行为。
> 3. **写操作先确认**：创建课程、发起（消耗额度的）生成任务、删除课程、确认大纲、导入课程包等
>    写操作，必须先向用户说明将要做什么并得到确认再调用。
> 4. **不访问外网**（除平台自身模型连接配置好的上游）：课程/交互页/导出内容均为站内资源；
>    交互场景页面已做静态安全校验（无外链）。
> 5. 所有资源按当前登录用户隔离；**他人资源一律 404**（平台不区分 403，不要据此推断存在性）。

## 0. 自检（每次任务开始）

| 步骤 | 调用 | 期望 |
|---|---|---|
| 服务存活 | `GET /api/health`（或 `/api/health/ready`） | 200 `{"status":"ok"}` 一类 |
| 登录态 | `GET /api/auth/me` | 200 且含 `id/username/role`；401 → 请用户先在本机浏览器登录 |
| 能力/上限 | `GET /api/classroom/meta` | `enabled/scene_types/modes/depths/widgets/limits/prompt_version`，P1/P2 还含 `interactive_mode`、`images`、`agent_roles` 等 |
| 模型连接 | `GET /api/providers` | `items[].id/name/kind/models`；生成任务需要 `provider_id` + `model` |
| 任务默认模型 | `GET /api/ai/task-defaults` | 若有 `course` 默认，可优先使用；否则让用户选择 |

> 若 `GET /api/providers` 为空：不要自行编造 `provider_id`；提示用户到「模型设置」配置连接，或使用
> 用户明确提供的信息调用 `POST /api/providers`（含 `api_key` 的请求必须在用户确认后进行，且不要把 Key
> 写入对话/文件）。

## 1. 主流程 A：生成一门课（两阶段 + 轮询）

1. **列课程 / 建草稿**
   - 找：`GET /api/classroom/courses?page=1&page_size=20`（可 `status` 过滤）。
   - 建：`POST /api/classroom/courses`
     `{"title?","topic(2-500 字必填)","subject?","grade?","mode?":"standard|interactive","requirements?","document_ids?"}`
     → `{course:{id,...}}`。**只建草稿，不调用 AI**。
2. **发起生成任务**：`POST /api/classroom/courses/{course_id}/generate`
   ```json
   {"stage": "all", "provider_id": 1, "model": "…", "scene_count": 6,
    "scene_types": ["slide", "quiz"], "depth": "standard",
    "images": false, "images_per_scene": 1}
   ```
   - `stage="outline"` 只生成大纲；`"all"` 生成大纲后暂停等确认；`"scenes"` 需已有大纲。
   - `mode="interactive"` 的课程：全课 1–2 个 `interactive` 场景、slide 自动带「动手试一试」提示块。
   - `images` 省略 = 自动（检测到可用图片连接才配图）；`false` 关闭；`true` 无连接 → 422。
   - 返回 `{job:{id,status:"queued",...}, course_id}`。
3. **轮询任务**（推荐 2–5 秒一次，或订阅 SSE）：`GET /api/ai/jobs/{job_id}`
   - `status`：`queued|running|awaiting_outline|completed|failed|cancelled`（另有 `interrupted`）。
   - `stage` 为中文阶段名（如「生成第 2/6 个场景：…」）；`result` 在完成/暂停后可用。
   - 或 `GET /api/ai/jobs/{job_id}/stream`（SSE：`stage`/`delta`/`done`/`error`）。
4. **确认大纲**（`status="awaiting_outline"` 时）：
   - 把 `result.outline` 交给用户核对/编辑；
   - `POST /api/ai/jobs/{job_id}/confirm-outline`
     `{"outline": "<JSON 字符串或 Markdown 标题>", "sections": ["…"]}`（`outline` 1–30000 字符；
     `sections` 可选，缺省自动从 `## ` 标题提取）；
   - 任务重新排队，继续轮询到 `completed`。
5. **读取结果**：`GET /api/classroom/courses/{course_id}` → `{course, scenes}`；
   任务 `result` 摘要含 `scene_count/generated_scene_count/failed_scene_ids/roster_generated/
   roster_warnings/summary/prompt_version`。失败场景可单独重新生成（见下）。

### 1.1 失败与续跑

- 单个场景失败不影响其它场景；`GET /courses/{id}` 中该场景 `status="failed"`、`meta.error` 有中文原因。
- 单场景重生成：`POST /api/classroom/courses/{course_id}/scenes/{scene_id}/regenerate`
  `{"provider_id","model","instruction?"}` → 新 job。
- 任务中途取消：`POST /api/ai/jobs/{job_id}/cancel`（已生成场景保留）；续跑：`POST /api/ai/jobs/{job_id}/resume`
  （执行器读 checkpoint 跳过已完成部分；不要重复提交同一任务）。
- 课程有运行中 `course` 任务时删除课程会 `409`，先取消/等待完成。

## 2. 主流程 B：课堂互动（可选）

1. 建会话：`POST /api/classroom/courses/{course_id}/sessions` `{"scene_id?}` → `{session:{id}}`。
2. 单智能体问答：`POST /api/classroom/sessions/{session_id}/tutor`
   `{"scene_id?","message","mode":"qa|discussion","provider_id?","model?"}` → `{reply,scene_hint?,usage}`。
3. 多角色课堂（需先有名册）：
   - `GET /api/classroom/courses/{course_id}/agents` → `has_roster`；
   - 无则 `POST /courses/{course_id}/agents/generate`（或让用户手动生成）；
   - `POST /api/classroom/sessions/{session_id}/discussion`
     `{"scene_id?","mode":"discussion|qa|lecture","topic?","rounds":1-3,"user_message?","provider_id?","model?"}`
     → 1–6 段 `turns`（每段含 `speaker_name/role/text/actions`，白板动作坐标 0–1、`coord_space:"unit"`）；
   - 学生插话：`POST /api/classroom/sessions/{session_id}/interject` `{"scene_id?","message"}` → 1–2 段。
   - 脚本会写入 `cls_turns`，可用 `GET /api/classroom/sessions/{session_id}` 回放（`turns`）。
4. 测验判分：`POST /api/classroom/sessions/{session_id}/quiz` `{"scene_id","answers":[…]}`。

## 3. 主流程 C：导出 / 资源包 / 导入

| 需求 | 调用 | 说明 |
|---|---|---|
| 课件 | `GET /courses/{id}/export?format=pptx` | python-pptx 文件；公式文本降级；交互场景为说明页 |
| 离线单文件网页 | `GET /courses/{id}/export?format=html` | 单文件播放器，无外链；可 `include_tts=true` 内联语音 |
| 可再导入备份 | `GET /courses/{id}/export?format=zip` | `manifest.json+course.json+scenes+assets`，可 `POST /api/classroom/import` |
| **教学资源包** | `GET /courses/{id}/export?format=package` | ZIP：`course.pptx` + 每个交互场景独立自包含 HTML（`interactive/scene-NN-title.html`）+ `course.json`/`ets.json` + 中文 `README.txt`；解压双击即可用，无需登录 |
| 导入课程包 | `POST /api/classroom/import` | multipart 字段名 **`file`**（`.zip` ≤200MB）；创建**当前用户的新课程**（不调模型） |

- 下载响应带 `Content-Disposition`（RFC 5987 中文文件名）；前端/Agent 取文件名请按 `filename*`。
- 资源包（package）**不可导入**（没有 `assets/**` 与 `manifest.json`）；要可导入请用 zip。
- 导出/导入后不要伪造“已发布”“已入库”等状态，以接口返回为准。

## 4. 关键约束与错误码

| 状态码 | 含义 / 处理 |
|---|---|
| 401 | 未登录：请用户在本机浏览器登录（不要索要密码/Cookie） |
| 404 | 资源不存在或不属于当前用户（不要猜测/枚举他人 id） |
| 409 | 状态冲突：任务在运行/课程已发布/大纲未处于等待确认等，按 `detail` 调整 |
| 422 | 参数/前置条件不满足（无模型连接、topic 过短、场景数越界、审核未通过…）：把中文 `detail` 转述给用户，不重试同样参数 |
| 429 | 配额（任务并发/24h 次数）：提示用户稍后或到任务中心查看 |
| 503 | 上游模型/依赖暂时不可用：可建议更换模型或稍后重试 |

- 上限（服务端强制，详见 `INTEGRATION-API-INDEX.md` §7）：场景 3–12；单场景元素 ≤24；动作 ≤40；
  TTS ≤2000 字；名册 ≤6 角色；讨论 ≤6 段/轮次 1–3；每课 interactive ≤2；配图 ≤2/场景；
  导入 ZIP ≤200MB。
- 写操作清单（**必须先确认**）：建课、任何 `generate`/`regenerate`/`agents/generate`、删除课程、
  `confirm-outline`、`import`、TTS 合成、上传资料。
- 禁止行为：绕过登录 / 伪造 `provider_id` 或模型名 / 把 422 重试成“成功” / 用占位或自编内容冒充
  AI 产物 / 修改或读取他人资源 / 把 Cookie、API Key 写入对话、日志或导出物 / 在用户未确认时批量删除。

## 5. 推荐给用户的交互话术（示例）

1. 用户：「帮我做一节初二生物《光合作用》的课，10 分钟，能互动。」
   → 先确认（会消耗模型额度）→ 建课 `mode="interactive"` → 生成 `stage="all"` → 轮询到
   `awaiting_outline` → 把大纲要点摘要给用户 → 用户确认/改标题与顺序 → `confirm-outline` →
   轮询到 `completed` → 汇报 `scene_count/失败场景` → 询问是否需要资源包。
2. 用户：「把刚才的课导成能直接发给学生的文件。」
   → `format=package` 下载 `资源包-课程名.zip`，说明：PPTX 用 Office 打开、`interactive/` 下 HTML
   双击即用、README 有清单；无需登录、无外链。
3. 用户：「换成微信能发的可导入备份。」
   → `format=zip`，说明可被平台 `POST /api/classroom/import` 导回（仅本人账号）。

## 6. 参考

- 本仓库 `study-platform/INTEGRATION-API-INDEX.md`（路由/字段/任务/上限总表，**字段以此为准**）
- `study-platform/app/classroom/API-SAMPLES.md`（真实响应样例 + P1/P2 增量）
- `study-platform/app/classroom/EXPORT-INTERACTIVE.md`（导出/导入/资源包/交互沙箱）
- `study-platform/app/classroom/MULTIAGENT-API.md`、`GENERATION-PLUS.md`（多智能体、交互模式、配图）
- 只读审计：`.omx/plans/integration/API-AUDIT.md`
