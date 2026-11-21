---
name: zhixu-paperlab
description: 在「知序」平台上操作试卷工坊（上传识别→人工核验→入库→组卷/错题巩固→导出/答题卡/整卷练习）的 SOP。适用于外部 Agent 工作台（OpenClaw / Codex 等）替用户完成「把一份试卷电子化」「按知识点组一套卷」「生成答题卡」等任务；不适用于无登录态的匿名调用。
---

# 知序试卷工坊 · Agent Skill（SOP）

> 事实基线（与本仓库 `study-platform/INTEGRATION-API-INDEX.md` §3/§6 一致；字段以该索引与
> `app/paperlab/*.md` 为准）：
>
> 1. **没有对外 API Token**。只支持浏览器会话 Cookie `study_session`（HttpOnly、SameSite=Strict），
>    由 `POST /api/auth/login` 或浏览器登录签发。外部 Agent 必须运行在**本机同源/持有该会话**的受控
>    环境；**不得索要/记录用户密码或 Cookie**，不得绕过鉴权或伪造 `Authorization: Bearer`。
> 2. **AI 只做草稿，必须人工核验**：识别（`paper-import`）与补全（`paper-enrich`）产物是草稿；
>    题干/答案/解析要经 `PATCH /imports/{id}/items/{item_id}` 或 `POST /questions/{id}/verify`
>    核验后才可信。**不得把模型草稿当已核验题目**，不得编造题目/答案。
> 3. **写操作先确认**：上传文件、发起识别/补全/组卷任务、入库（commit）、发布试卷、生成答题卡、
>    删除题目/试卷、提交练习，都必须先与用户确认。
> 4. 资源按登录用户隔离；**他人资源一律 404**；不访问外网（识别用平台已配置的模型连接）。

## 0. 自检

| 步骤 | 调用 | 期望 |
|---|---|---|
| 服务 | `GET /api/health` | 200 |
| 登录态 | `GET /api/auth/me` | 200；401 → 请用户先在本机浏览器登录 |
| 能力/上限 | `GET /api/paperlab/meta` | `subjects/grades/qtypes/difficulties/limits/capabilities/prompt_version`；P1 起 `capabilities.papers` 与 `answer_sheet` 均为 `true` |
| 模型连接 | `GET /api/providers` | 识别需**视觉模型**（`caps.vision`），补全/组卷需文本模型；任务需 `provider_id`+`model` |

## 1. 主流程 A：试卷电子化（上传 → 识别 → 核验 → 入库）

1. **上传**：`POST /api/paperlab/imports`（multipart）
   - 字段：`files`（可多个，图片 JPG/PNG/WebP 或 PDF/DOCX；单文件 ≤30MB、≤20 文件、≤20 页）
     + `title?/subject?/grade?`；
   - → `{import:{id,status,pages:[{page_no,width,height,image_url}]}}`。超限/格式不支持 → 422。
2. **发起识别任务**：`POST /api/paperlab/imports/{import_id}/recognize`
   `{"page_from?","page_to?","provider_id","model"}` → `{job:{id,...}}`（task `paper-import`）。
   - 轮询 `GET /api/ai/jobs/{job_id}`（或 SSE `/stream`），阶段如「识别 2/5 页」；支持取消/续跑
     （`POST /api/ai/jobs/{id}/cancel|resume`，已完成页保留）。
3. **查看草稿**：`GET /api/paperlab/imports/{import_id}` → `{import, pages, items, counts}`；
   每项 `draft` 含题型/题干/选项/答案/解析/知识点/难度/裁图。
4. **人工核验/修订**：`PATCH /api/paperlab/imports/{import_id}/items/{item_id}`
   `{"draft?","bbox?","status?":"pending|accepted|skipped","skip_reason?"}`；
   已入库草稿 → 409。图片题可先 `GET /imports/{id}/pages/{no}/image` 查看页图。
5. **入库（写操作，需确认）**：`POST /api/paperlab/imports/{import_id}/commit`
   `{"item_ids?":[],"target":"app_bank"}`；缺省只入库 `accepted` 草稿；**重复 commit 幂等**
   （不会重复建题）。`target="paper"` 当前返回 422（组卷请走 §3）。
6. **入库后**：`GET /api/paperlab/questions?...` 查看；`POST /questions/{id}/verify` 标记
   `verified/rejected/pending`；`verified` 后 AI 补全不会覆盖（AI 补全 = `POST /questions/enrich`，
   task `paper-enrich`，1–50 题）。

### 1.1 切题/答题区标注（可选的进阶流程）

- 页级标注：`GET/PUT /api/paperlab/imports/{id}/pages/{no}/annotation`（归一化 boxes，`version` CAS；
  409 的 `detail` 是对象并带 `X-Edit-Version`）；单页 ≤200 框。
- 重切：`POST /api/paperlab/imports/{id}/resegment` `{"pages?","keep_items?"}`（≤20 页同步，>20 页转
  task `paper-annotate`，此时请求需带 `provider_id/model` 仅为任务框架入参、不调用模型）。
- 已入库草稿绝不覆盖；`stale`/`warnings` 要在结果里如实转述。
- 发布前校验（只校验、不复制 commit）：`POST /api/paperlab/imports/{id}/publish`。

## 2. 主流程 B：题库维护（我的题库）

| 动作 | 调用 |
|---|---|
| 列表/检索 | `GET /api/paperlab/questions?subject&grade&qtype&keyword&status&verify_status&page&page_size` |
| 新建 | `POST /api/paperlab/questions` `{type,subject,grade,difficulty?,knowledge_tags?,content,status?}` |
| 详情/编辑（CAS） | `GET /questions/{id}`；`PATCH /questions/{id}` `{version,...}`（冲突 409） |
| 软删除 | `DELETE /questions/{id}` |
| 统计 | `GET /questions/stats?subject&grade` |
| 导出 | `GET /questions/export?format=json|csv&subject&grade&status`（≤5000 题） |
| 导入 | `POST /questions/import`（multipart `file`，JSON/CSV ≤10MB、≤2000 题；`dry_run=true` 先预览） |

> 导入会按内容摘要去重，重复计入 `skipped`；`errors[]` 逐行给出原因（最多 200 条）。
> 大批量导入前先用 `dry_run` 让用户确认将新增/跳过的数量。

## 3. 主流程 C：组卷 / 错题巩固卷 / 试卷管理

1. **AI 组卷**：`POST /api/paperlab/papers/compose`
   ```json
   {"mode":"smart|knowledge|wrong|layered","subject":"数学","grade?":"初中二年级",
    "count":10,"type_counts?":{"SINGLE":6,"SUBJECTIVE":4},
    "difficulty_target?":0.5,"tolerance?":0.1,"kp_weights?":{"函数":60,"导数":40},
    "filters?":{"source":"all|app|bank","knowledge_tags?":[],"exclude_refs?":[]},
    "provider_id":1,"model":"…"}
   ```
   - 同步预检不满足（候选不足/题型不足/知识点配额不足/难度不足）→ **422 并给差额**，不建任务；
   - 通过 → `{job}`（task `paper-compose`，SSE 阶段「组卷候选准备→AI 选题→硬校验→生成快照→组卷完成」）；
   - 硬校验失败会重试一次，仍失败任务 `failed`（中文原因，**绝不缩水/编造**）；续跑按 checkpoint 幂等。
2. **错题巩固卷**：`GET /api/paperlab/revision/options?subject=` → 选 `mode`；
   `POST /api/paperlab/revision` `{mode:"replay|consolidate",subject?,kp_ids?,count?1-20,provider_id?,model?}`
   - `replay` 同步建卷（无需模型，错题为空 422）；`consolidate` → `{job}`（task `paper-revision`，
     AI 只从真实候选题选，候选不足 422）。
3. **试卷 CRUD**：`GET/POST /papers`、`GET/PATCH/DELETE /papers/{id}`（`version` CAS；已发布结构不可改）、
   `POST /papers/{id}/items/reorder`、`POST /papers/{id}/publish {published}`。
   手工建卷 `items[].ref` 形如 `app:123`（应用题库）或 `bank:qid`（只读高考题库），单卷 ≤60 题。
4. **导出**：`GET /papers/{id}/export?format=docx|html&answers=0|1&columns=1-4`
   （docx 服务端生成；html 供浏览器打印；`answers=0` 学生版）。PDF 未提供服务端渲染，用浏览器打印。
5. **答题卡**：`POST /papers/{id}/cards {preset?,orientation?,columns?,bubble?,include_subjective?,student_no?,title?}`
   （≤20 张/卷）→ `GET /cards/{id}`、`GET /cards/{id}/svg?page=N&color=black|red`、
   `GET /cards/{id}/export?format=html|svg&page=all|N`、`PATCH/DELETE /cards/{id}`。
   试卷结构变化后旧卡会变 `stale`（不可改、仍可导出，响应头 `X-Card-Stale`），应提示用户重新生成。
6. **整卷练习（自测）**：`POST /papers/{id}/attempts` → `{attempt,items}`（卷面不含答案）；
   `PATCH /attempts/{id}/answers {item_position,answer}` 幂等保存；
   `POST /attempts/{id}/submit` 判分（重复提交 409）；错题会联动既有错题本（`bank:qid` 来源）。

## 4. 错误码与上限

| 状态码 | 含义 / 处理 |
|---|---|
| 401 | 未登录：请用户本机登录；不要索要凭据 |
| 404 | 资源不存在或非本人（不要枚举 id） |
| 409 | 版本冲突（PATCH/标注 CAS）、已入库/已发布/已交卷等状态冲突；重新 `GET` 最新版本后再试 |
| 413 | 文件过大（上传/课程包/单题作答超限） |
| 422 | 参数/业务校验不通过（格式、题型、配额、候选不足、发布校验未通过）：把中文 `detail` 转述给用户，不伪造成功 |
| 429 | 配额/并发限制：稍后重试或到任务中心 |
| 503 | 上游模型/依赖暂不可用 |

- 上限速查：上传 30MB/20 文件/20 页/400 草稿；补全 1–50 题；题库单页 ≤100；组卷 1–60 题；
  巩固卷 1–20；试卷 ≤60 题；答题卡 ≤20/卷；切题框 ≤200/页；题库导入 ≤10MB/≤2000 题；
  整卷作答单题 ≤16KB（完整表见 `INTEGRATION-API-INDEX.md` §7）。
- 写操作清单（**必须先确认**）：上传文件、`recognize`/`enrich`/`compose`/`revision`（消耗额度）、
  `commit` 入库、`verify`、`publish`（试卷/发布校验）、建卡/改卡/删卡、建卷/改卷/删卷、交卷。
- 禁止行为：绕过登录 / 自编题目或答案 / 把未核验草稿当已核验 / 把 422 当成功继续 / 修改他人题目 /
  在用户未确认时批量入库或发布 / 把 Cookie、API Key 写入对话或导出物。

## 5. 推荐流程（话术示例）

1. 用户：「把这张数学卷子电子化，导进我的题库。」
   → 确认（会调用视觉模型）→ 上传 → recognize → 轮询 → 汇报页数/题目数/需人工确认的项 →
   让用户核验/确认 → commit → 汇报 `created_question_ids` 与 `existing_question_ids`（幂等）。
2. 用户：「按函数和导数各一半组一份 10 题初中卷，打印用。」
   → compose（`mode="knowledge"`、`kp_weights`、`count=10`）→ 轮询；422 时把缺口告诉用户并让 ta 调整；
   完成后 `GET /papers/{id}` 核对 → 询问是否生成答题卡/导出 docx。
3. 用户：「给我一套错题重练。」
   → `revision/options` → `mode="replay"`（不耗模型）同步返回试卷；错题为空则如实告知（422）。

## 6. 参考

- 本仓库 `study-platform/INTEGRATION-API-INDEX.md`（路由/字段/任务/上限总表，**字段以此为准**）
- `study-platform/app/paperlab/API.md`（导入识别/题库/补全）
- `study-platform/app/paperlab/PAPERS-API.md`（试卷/组卷/巩固卷/导出/整卷练习）
- `study-platform/app/paperlab/CARDS-API.md`（答题卡模板/SVG/打印）
- `study-platform/app/paperlab/ANNOTATE-API.md`（切题/答题区标注/重切/发布校验）
- `study-platform/app/paperlab/BANK-IO.md`（题库统计/JSON·CSV 导入导出）
- 只读审计：`.omx/plans/integration/API-AUDIT.md`
