# 知序整合工程 · 实施契约（INTEGRATION CONTRACT）

本文件是「把 OpenMAIC / Oh My Paper / 小鹿老师（xl-frontend）非基础模块整合进知序」的统一实施契约。
所有参与实施的代理必须先读本文件与对应仓库盘点：

- `.omx/plans/integration/00-总方案.md`（范围、排除项、里程碑）
- `.omx/plans/integration/recon/openmaic.md`、`oh-my-paper.md`、`xl-frontend.md`（源码盘点）
- `study-platform/CONTRACT.md`（知序既有模块契约，仍然有效）

## 1. 不可违反的总原则

1. **复用优先，不重写**：题库检索/门控、作答与错题、模型连接与协议适配、任务队列/SSE/取消/续跑、
   笔记 Agent、向量检索、导出与打印基线都已存在。新功能通过既有设施承载，不另起一套。
2. **用户隔离**：所有新表带 `user_id`，所有读写先校验归属；不得沿用前端的可见性做权限。
3. **不伪造 AI 结果**：没有配置模型/Key 时必须明确报错或显示引导；AI 产物必须标注模型与 prompt 版本，
   核验/发布状态可追溯。缺数据时显示空状态，不生成假数据。
4. **不新增运行依赖**：只能用 `requirements.txt` 已声明的库（另含环境已装的 python-pptx / reportlab /
   qrcode / lxml 时须谨慎——优先用已声明库与标准库；如确需新依赖，先在方案中说明并等 Lead 批准）。
   本地无 Node 构建：前端只能是原生 ESM + CSS，不得引入打包工具/框架。
5. **SQLite 与 PostgreSQL 双兼容**：DDL 一律用 `con.executescript(...)`（PG 模式会自动把
   `id INTEGER PRIMARY KEY` 翻译成 identity）；SQL 用 `?` 占位；不用 SQLite 专有函数（新模块不上 FTS5）；
   时间统一 `db.utcnow()` 的 ISO-8601 UTC 字符串；JSON 存 TEXT（`json.dumps(..., ensure_ascii=False)`）。
6. **保持基线绿色**：改动后 `python -m pytest -q` 必须全绿；新功能必须有测试；前端 `node --check` 通过；
   `python -m compileall -q app` 通过。任何改动不得让既有测试退化（第二轮收敛基线 1450 passed / 3 skipped，
   见 `docs/知序优化实施记录-20261121-第二轮.md` §5.0；第三轮全量 pytest **1557 passed / 3 skipped**
   （779.07s，无失败），见 `docs/知序优化实施记录-20261121-第三轮.md` §6.4）。Phase 3 的增量专项测试与
   路由/契约自查见第三轮记录 §6；浏览器一键验收由 Lead 在隔离实例收敛。

## 2. 文件所有权与写作用域

Lead 独占（其他代理**不要修改**）：`app/main.py`、`app/ai/jobs.py`、`app/ai/__init__.py`、
`app/ai/providers.py`、`app/db.py`、`app/auth.py`、`app/practice.py`、`app/ai/verify.py`、
`app/ai/notes_agent.py`、`app/ai/adapters.py`、`deploy/worker.py`、`tests/conftest.py`、`web/index.html`、
`web/js/main.js`、`web/js/core/state.js`、`web/js/core/dom.js`、`web/js/core/api.js`、`web/js/core/jobs.js`、
`web/js/core/router.js`、`README.md`、`CONTRACT.md`、`验收记录.md`、`requirements*.txt`、`deploy/**`。

功能模块所有权（可自由创建/修改本包内文件 + 对应前端页面/CSS + 本模块测试）：

| 工作流 | 后端包 | 前端页面 | CSS | 测试 |
|---|---|---|---|---|
| AI 课堂（OpenMAIC） | `app/classroom/**` | `web/js/pages/classroom.js`（含播放器，可拆 `web/js/components/classroom*.js`） | `web/styles-classroom.css` | `tests/test_classroom_*.py` |
| 试卷工坊（识别/组卷/答题卡/巩固卷） | `app/paperlab/**` | `web/js/pages/paperlab.js`、`web/js/pages/mybank.js`、可拆 `web/js/components/paper*.js` | `web/styles-paperlab.css` | `tests/test_paperlab_*.py` |
| 学情分析（报告） | `app/insight.py`（单文件即可） | `web/js/pages/insight.js` | `web/styles-insight.css` | `tests/test_insight.py` |
| AI 助手（小纸） | `app/assistant.py`（单文件即可） | `web/js/pages/assistant.js` | `web/styles-assistant.css` | `tests/test_assistant.py` |
| 任务中心 | 复用 `/api/ai/jobs*`（不新增后端文件） | `web/js/pages/tasks.js` | `web/styles-tasks.css` | 归各任务测试 |

`app/main.py` 已完成接线：每个模块只需提供
`router: APIRouter(prefix="/api/<module>")` 与 `init_<module>()`，Lead 在 lifespan 与路由注册处调用。
**新增/修改 API 形状后，本模块要在自己的 `docs` 说明中记录，并同步给 Lead。**

Phase 3（2026-11-21）新增归属：`app/plaza/**`（质量运营扩展：notify/favorites/feedback/quality/metrics 与
release 回滚）、`app/official.py` + `app/question_ref.py`（平台精选读取与统一引用）、`app/ai/usage.py`（用量账本
与配额，仅在 `app/ai/jobs.py`、`app/ai/__init__.py`、`app/main.py` 做最小接线）；对应前端页面/组件
（`web/js/pages/{practice,question,paperlab,home,plaza,plazaAdmin,submission,tasks,admin,settings}.js`、
`web/js/components/{paperCompose,paperAttempt,plazaNotifications}.js`）与测试分别由各工作流负责，均已并入主干。
Phase 3 经 Lead 批准在 `app/ai/jobs.py` / `app/ai/__init__.py` 增加用量钩子与路由注册（最小改动），
§3.2 中「不要修改 `app/ai/jobs.py`」的约束对本轮由 Lead 解除；后续任务仍应先申请、由 Lead 统一加通用能力。

## 3. 后端模块规范

### 3.1 包结构

```
app/classroom/__init__.py   # router、init_classroom()、register_jobs()、include 子路由
app/classroom/schema.py     # 数据结构校验/规范化
app/classroom/prompts.py    # prompt 构建（文本常量或读取 app/ai/prompts/*.yaml）
app/classroom/generate.py   # 任务执行器（course 生成管线）
...
```

`init_<module>()` 在应用启动时被调用一次：幂等建表（`con.executescript`）、注册任务、创建目录。
`register_jobs()` 由 `init_<module>()` 调用，向任务注册表登记任务：

```python
from ..ai.jobs import register_task_handler

register_task_handler(
    "course",                          # 任务名：小写字母/数字/连字符，≤40 字符
    run_course_job,                    # async def handler(job_id, user_id, data, provider, verifier)
    validate_course_job,               # def validate(data: JobInput, user: dict) -> None，失败抛 HTTPException
    label="AI 课堂生成",
)
```

任务参数一律通过 `JobInput.params`（JSON 对象，≤16KB）传入，`JobInput` 已有字段（provider_id/model/
cross_verify/requirements/document_ids 等）可直接复用。**不要修改 `app/ai/jobs.py`**；需要新的通用能力时
在集成记录里提出，由 Lead 统一加。

### 3.2 任务执行器要求

- 阶段可读：用 `from ..ai.jobs import publish_event, update_job, is_job_cancelled, _job_set_checkpoint, _job_get_checkpoint`
  推送 `stage` 事件（中文阶段名）与进度；在每页/每场景之间检查 `is_job_cancelled(job_id)`，
  取消时先保存已完成的幂等产物再 `raise asyncio.CancelledError`。
- 断点续跑：长任务把进度写入 checkpoint（`_job_set_checkpoint`），执行器开头读取并跳过已完成部分。
- 结果：返回 JSON dict（会写入 `ai_jobs.result_json`）。大内容（课程场景、试卷快照）落业务表，
  result 只放摘要 + 主键（例如 `{"course_id": 3, "scene_count": 12, "summary": "..."}`）。
- 幂等：同一 `job_id` 重试只应生成一份最终产物（用 `job_id` 唯一索引或 `INSERT ... ON CONFLICT`）。
- 流式：如需流式输出，`publish_event(job_id, "delta", {"text": ...})`。
- 人力确认（可选）：需要用户确认提纲时，任务改为 `_job_update(job_id, "awaiting_outline", ...)` 并
  `publish_event(job_id, "done", {"status": "awaiting_outline"})` 后 return；用户确认走既有
  `POST /api/ai/jobs/{id}/confirm-outline`（checkpoint 里取 `confirmed_outline`）后任务被重新派发。
- AI 调用：用 `app.ai.providers._selected_provider(user_id, provider_id, model)` 取连接（已在
  `submit_job` 里校验过），用 `app.ai.adapters._call_model(provider, model, prompt, images, json_output,
  token_limit, system_prompt=..., on_delta=...)` 或 `call_structured(...)`；图片片段用
  `app.content_refs.image_part(bytes_or_path)`。**禁止**自己发 HTTP 或拼厂商请求。
- 错误：给用户看的中文错误放 `HTTPException(4xx, "...")`；执行器内异常由框架统一落 failed，
  错误信息不得包含 API Key（框架会脱敏，但仍不要拼接密钥）。
- 输入上限：单文件/单页/批量数量必须写死上限并返回可操作提示。

### 3.3 表与数据约定

- 表名前缀：`cls_`（课堂）、`pl_`（试卷工坊）、`asst_`（助手）；学情分析不建表（实时聚合）。
- 主键：`id INTEGER PRIMARY KEY`；外键：`REFERENCES 表(id) ON DELETE CASCADE`；
  时间：`created_at TEXT NOT NULL`、`updated_at TEXT NOT NULL`；软删除用 `deleted_at TEXT`。
- 需要并发的表加 `version INTEGER NOT NULL DEFAULT 1`，更新时 `WHERE id=? AND version=?` 做 CAS。
- 所有私有表必须 `user_id INTEGER NOT NULL REFERENCES users(id)` 并建 `(user_id, ...)` 索引。
- JSON 字段统一后缀 `_json`（如 `content_json`、`result_json`），读时用 `.get` 容错。
- 新增主键表名要告诉 Lead 加进 `app/db.py::_ID_TABLES`（PG 模式下 `cursor.lastrowid` 依赖它）。
- 2026-11-21 第二轮新增 `pl_attempt_items`（作答冻结行，`UNIQUE(attempt_id,item_position)`）与
  `pl_assets`（服务端资产登记，`asset_id TEXT NOT NULL UNIQUE`）；两者插入后不使用 `lastrowid`
  （按唯一键/同事务查询取 id），因此无需登记 `_ID_TABLES`。`pl_paper_attempts.paper_id` 不再挂
  `ON DELETE CASCADE`：旧 SQLite 库由 `practice.ensure_tables()` 一次性重建表迁移，PostgreSQL 按系统目录
  删除外键约束。
- 2026-11-21 Phase 1 剩余新增 `pl_question_versions`（个人题不可变内容版本：`UNIQUE(question_id,version)`、
  `origin` ∈ `create|edit|collect|source_update|snapshot`、`content_sha256` 内容指纹、`asset_refs_json`、
  `source_ref/source_question_id/source_bank_version/source_content_revision/source_answer_revision`；
  无修改入口，只追加）与 `pl_questions` 的来源列（`content_sha256`、`source_ref`、`source_kind`、
  `source_question_id`、`source_bank_version`、`source_content_revision`、`source_answer_revision`、
  `source_meta_json`、`collected_at`；老库由 `paperlab.db.init_tables()` 的 `ensure_column` 幂等补齐，
  不重建表；来源列补完后建 `pl_questions(user_id, source_ref)` 索引）。
- 广场 Phase 2 的 8 张表（`plaza_submissions/plaza_submission_versions/plaza_reviews/plaza_releases/
  plaza_events/plaza_public_assets/plaza_roles/plaza_identity`）由 `app/plaza/db.py::ensure_tables()` 自建，
  不在本文件 §3.3 的 `cls_/pl_/asst_` 前缀规则内；表结构与状态机见 `INTEGRATION-API-INDEX.md` §16 与
  `CONTRACT.md` 第二轮不变量。广场写事务统一 `db.write_lock(con, "plaza-write")` 后显式分配主键
  （不依赖 `lastrowid`/`RETURNING`），因此无需登记 `_ID_TABLES`。
- 广场 Phase 3（2026-11-21）再新增 6 张表（仍由 `app/plaza/db.py::ensure_tables()` 幂等自建，显式主键，
  不登记 `_ID_TABLES`）：`plaza_notifications`（个人通知，`(user_id, read_at)` 未读索引）、`plaza_favorites`
  （`UNIQUE(user_id,submission_id)`）、`plaza_quality_checks`（抽检记录，`auto_withdrawn` 标记）、
  `plaza_issues` / `plaza_reports` / `plaza_appeals`（反馈闭环，状态 `pending/resolved/rejected`、处理人与时间）。
  广场表共 14 张；本轮未改既有 8 张表的列结构（回滚复用 `plaza_releases.status/active_public_id/withdraw_mode`）。
- AI 用量（Phase 3）的 `ai_job_usage`（`job_id` 唯一）与 `ai_quotas`（`kind` 主键）由
  `app/ai/usage.py::_ensure_tables()` 在 `app/ai/__init__.py::init_ai()` 启动时幂等建表（先于 `reap_stale_jobs()`），
  `ai_jobs` 通过 `db.ensure_column` 补 `started_at`、`retry_count` 两列；两表由 AI 工作流独占，不在本文件
  `cls_/pl_/asst_` 前缀规则内，也无需登记 `_ID_TABLES`（显式 INSERT，不使用 `lastrowid`）。

### 3.4 错误与状态码基线

`401` 未登录；`403` 非本人/非管理员；`404` 不存在；`409` 版本冲突/状态不允许；`422` 参数或前置条件
不满足（例如未配置模型、题数不足）；`429` 配额；`503` 依赖未就绪（例如未导入数据）。

## 4. API 契约 v1

所有路由都在 `/api/<module>` 下，JSON 请求/响应，需登录（`Depends(current_user)`）。
错误统一 `{detail: "中文说明"}`。列表统一 `{items, total, page, page_size}`（小列表可省略分页）。
时间均为 ISO 字符串。**实现可以先于契约完成，但已发布字段不得随意改名；需要变更时更新本文件。**

### 4.1 AI 课堂 `/api/classroom`（OpenMAIC 原生化）

课程模型：一门课 = `course`（标题/主题/学科/年级/模式/状态/大纲） + 有序 `scenes`。
场景类型：`slide`（幻灯片）、`quiz`（测验）、`interactive`（HTML 交互/模拟/3D/游戏/思维导图/编程）、
`pbl`（项目制学习）。幻灯片内容使用**块模型**（标题/要点/段落/图片/公式/表格/提示卡/分栏），
不移植 PPTist 自由画布；交互场景是受限沙箱 HTML。

- `GET /meta` → `{enabled, scene_types:[...], modes:['standard','interactive'], tts:{provider:'...'|null}, limits:{scene_count_max, ...}}`
- `GET /courses` → 列表 `{items:[{id,title,topic,subject,grade,mode,status,scene_count,updated_at}]}`
- `POST /courses` `{title?, topic, subject?, grade?, mode?, requirements?, document_ids?}` → `{course}`
  （只建草稿，不调用 AI；topic 必填 2–500 字；document_ids 复用人纪资料作为参考）
- `GET /courses/{id}` → `{course, scenes:[...]}`（场景含完整 `content_json` 解析后的内容）
- `PATCH /courses/{id}` `{title?, description?, subject?, grade?}` → `{course}`
- `DELETE /courses/{id}`（级联场景/会话/素材；有运行中任务时 409）
- `POST /courses/{id}/generate` `{stage:'outline'|'scenes'|'all', provider_id, model, scene_types?, scene_count?, depth?,
  web_search?:bool, web_search_limit?:1-8, source_ref?:string}` → `{job}`。`web_search=true` 时大纲前用 DeepSeek 官方
  `/v1/tools/search` 检索并把带 `[n]` 链接的来源注入 prompt，来源落 `course.outline.sources`/
  `course.config.web_search`（无搜索连接时降级不报错，标注未使用联网资料；不得伪造来源）。
  （task=`course`，params={course_id, stage, ...}；`stage=outline|all` 先产大纲，进入 `awaiting_outline`；
  用户在 UI 编辑大纲后调用既有 `POST /api/ai/jobs/{job_id}/confirm-outline` 继续）
- **P4 平台精选题作为课程素材（2026-11-21 第四轮）**：`source_ref=official:<公开编号>`（目前只支持 official）——
  格式非法/来源不支持 **422**，未发布/撤回/争议 **404**（仅在新建大纲时校验，已有课程续跑/单场景重生成不重新引用）；
  `outline.sources` 追加 `{kind:'official', public_id, published_version, review_status(_label), summary,
  source_declaration, question_ref}` 条目，`course.config.seed_source` 记录建课来源，任务结果下发
  `official_source_count/official_sources[]`；prompt 只注入公开题面/答案要点（答案仅供备课）与公开来源声明，
  不得编造公开编号或输出内部 ID/投稿人/审核员；撤回后不可新建课程，已生成课程与导出保留当时的来源标记。
- `POST /courses/{id}/scenes/{scene_id}/regenerate` `{provider_id, model, instruction?}` → `{job}`
- **P1/P2 增量（已实现）**：`GET|PATCH /courses/{id}/agents`、`POST /courses/{id}/agents/generate`（角色名册）；
  `POST /sessions/{id}/discussion`（`{scene_id?,mode:'qa'|'discussion'|'lecture',topic?,rounds?,user_message?}`
  → 1–6 turns 多角色脚本）、`POST /sessions/{id}/interject`（插话回应）；`POST /courses/{id}/generate`
  额外接受 `images`（null 自动/false 关/true 强制）、`images_per_scene`(0–2)、`image_provider_id/image_model`；
  课程对象含 `interactive_mode:boolean`；`GET /meta` 含 `agent_roles/discussion_modes/interactive_mode/images`；
  白板动作在 `coord_space:"unit"` 时按 1000×562.5 归一化坐标，支持 `text/formula/line/rect/circle/arrow/path`。
  课程 ZIP manifest 含 `agents`/`interactiveMode`/资源 `assetId`；导入恢复名册并重映射图片素材。
- `GET /courses/{id}/export?format=pptx|html|zip|package|pdf|mp4&include_tts=` → 文件流；`POST /courses/import`
  （multipart `.zip`）。`pdf` 走服务端浏览器渲染（不可用 503 提示改 html 打印）；`mp4` 需先生成视频任务
  （`POST /courses/{id}/video`，任务 `course-video`，未生成时 409）。
- 媒体能力（P4）：`POST /courses/{id}/images`（multipart `prompt` + 可选 `reference` 参考图 → MiniMax 图片编辑 /
  OpenAI `/images/edits`，落 `cls_assets`）；`GET /voices`（按连接聚合音色：MiniMax `/get_voice` 缓存、
  MiMo 固定音色、OpenAI 默认音色）；`POST /courses/{id}/narration` 与 `GET /courses/{id}/narration`
  （任务 `course-narration` 批量合成旁白，幂等/取消/续跑；结果存 `course.config.narration`）；
  `GET /asr/info`、`POST /asr`（服务端语音识别兜底，≤25MB）。
- 幻灯片编辑（P6a）：`GET|PATCH /courses/{id}/scenes/{sid}/content`（`version` CAS 409、仅 slide、schema 归一化/
  校验、非法 422；`GET` 返回含 `version` 的完整场景）；`POST /courses/{id}/scenes/{sid}/duplicate`。
- 交互库内联（P7）：3D 场景（`widget=visualization3d`）由服务端把 three.js（r186，MIT）内联注入自包含 HTML；
  沙箱 CSP 与 `sandbox="allow-scripts"` **不放宽**（库按 sha256 白名单剔除后校验用户代码，用户 HTML 仍禁
  eval/fetch/父窗口访问等）；`interactive` 上限按 widget 区分（3D 1.5MB / 其它 600KB）。
- `GET /scenes/{id}/html`（独立 CSP、`X-Frame-Options: SAMEORIGIN`）；`POST /scenes/{id}/interactive-event`
  `{type:'ready'|'error'|'state', session_id?, summary?, message?, state?}`（state ≤8KB 入会话 progress）
- `POST /courses/{id}/sessions` → `{session}`；`GET /sessions/{id}` → `{session, course, progress}`
- `PATCH /sessions/{id}` `{scene_id?, progress?}` → `{session}`（进度保存；`progress` 是小型 JSON）
- `POST /sessions/{id}/tutor` `{scene_id, message, mode:'qa'|'discussion'|'lecture', history?}` →
  `{reply:{speaker, text, actions:[...]}, scene_hint?, usage}`（同步调用；actions 见下）。
  注：上游 OpenMAIC 实际只有 `qa|discussion|lecture` 三种对话模式（README 的「圆桌辩论」用多角色
  discussion 实现），不设独立 debate 分支。
- `POST /sessions/{id}/quiz` `{scene_id, answers:[{question_id, values:[]|string}]}` → `{score,max_score,detail}`
- `POST /courses/{id}/tts` `{text, voice?, speed?}` → `{asset:{id,url}, provider}` 或 `{fallback:'browser'}`
- `GET /assets/{asset_id}` → 文件流（仅本人；`Cache-Control: private, max-age=3600` + ETag）
- **素材回收与类型控制（P0/P1，2026-11-21）**：
  - `DELETE /courses/{id}` 改为安全回收并返回审计 `{deleted, course_id, assets:{examined, retained,
    reclaimed, shared_files_kept, files_removed, file_errors}}`：先现查该用户其它课程/场景的存活引用，仍被
    引用的素材把 `course_id` 迁移到引用课程并保留文件；同一 `storage_name` 还被其它素材行共享时只删行、
    不删文件；只删除无存活引用且文件未被共享的素材（提交后复查并发写入）。
  - `GET /assets/{id}` 按字节内容嗅探真实类型：仅白名单媒体（png/jpeg/webp/gif、常见音频、`video/mp4`）
    内联；其余 `Content-Disposition: attachment` + `X-Content-Type-Options: nosniff` +
    `Content-Security-Policy: default-src 'none'; sandbox` 隔离下载，SVG/HTML/XML 等主动内容降级为
    `application/octet-stream`；文件缺失/已回收 404 中文降级（不 500）。
  - `GET /courses/{id}` 在场景引用的素材已缺失/被回收时追加 `missing_asset_ids` 与中文 `warnings`。
  - `POST /courses/import` 按内容识别素材真实类型（不采信 manifest 声明），伪造/主动内容降级为隔离下载，
    导入照常完成并在 `asset_warnings` 返回中文提示（≤20 条）。
  - `POST /courses/{id}/images` 参考图有界读取 ≤8MB（超限 413），服务端压到最长边 1600 / JPEG 85；
    `POST /asr` 音频 ≤25MB（超限 413）。

`actions` 动作集（课堂播放器可执行）：`{"type":"spotlight","element_id":"..."}`、`laser`（坐标百分比）、
`highlight`、`whiteboard`（`{ops:[{kind:'text'|'line'|'rect'|'circle'|'formula',...}]}`）、
`next_scene`、`show_slide`、`play_audio`。动作必须由服务端校验后下发，前端只执行白名单动作。

### 4.2 试卷工坊 `/api/paperlab`（Oh My Paper + 小鹿老师）

应用内可写题库 `pl_questions`（与只读高考题库并存；`source.bank='source'|'app'` 区分）。
识别导入 = 上传 → 渲染分页 → 视觉模型结构化提取（草稿）→ 人工核验编辑 → 批量入库。
图片题目转 LaTeX 预置适配层见 `app/ai/surya_latex.py`（Surya OCR，可选依赖）：输出内容块
（text/math/image/table）与整题 LaTeX，识别结果须经人工核对后方可入库，详见 `docs/平台设计.md`。

- `GET /meta` → `{enabled, subjects, grades, qtypes, limits:{upload_mb, import_pages_max, import_items_max}, capabilities:{vision:true}}`
- `POST /imports` multipart `files[]`（图片/PDF/DOCX，≤30MB/个，≤20 页）+ `title?/subject?/grade?` →
  `{import:{id,status,pages:[{page_no,width,height,image_url}]}}`
- `GET /imports` → 列表；`GET /imports/{id}` → `{import, pages, items:[{id,page_no,bbox,draft,status}]}`
- `POST /imports/{id}/recognize` `{page_from?,page_to?,provider_id,model}` → `{job}`（task=`paper-import`）
- `PATCH /imports/{id}/items/{item_id}` `{draft:{...}}` → `{item}`（人工改写题干/答案/知识点/裁图框）
- `POST /imports/{id}/commit` `{item_ids?:[], target:'app_bank'|'paper'}` → `{created_question_ids, paper_id?}`
- `GET /imports/{id}/pages/{page_no}/image` → 页面图；`GET|PUT /imports/{id}/pages/{page_no}/annotation`
  （页级 edit.json：`{version, boxes:[{id,kind:'question'|'answer',x,y,w,h,label?,item_id?,score?}]}`，
  归一化 0–1 坐标 + version CAS 409）；`GET /imports/{id}/pages`（页列表/标注概况）；
  `POST /imports/{id}/resegment`（按框 Pillow 裁图生成/更新草稿，已入库只标 stale）；
  `POST /imports/{id}/items/{item_id}/answer-box`（答题区坐标）；
  `POST /imports/{id}/publish`（发布前校验：题干/bbox/顺序，不复制 commit 语义）
- `GET /questions?subject&grade&qtype&keyword&page&page_size` → `{items,total,...}`；`GET /questions/stats`
  （题型/学科/状态/缺失项/知识点 Top）；`GET /questions/export?format=json|csv`；`POST /questions/import`
  （multipart，dry_run 预检、逐行错误、内容摘要去重）
- `POST /questions` `{type,subject,grade,difficulty,knowledge_tags[],content{draft结构见 recon}}` → `{question}`
- `GET/PATCH/DELETE /questions/{id}`（PATCH 带 `version` CAS；`status: draft|ready|archived`）
- `POST /questions/enrich` `{question_ids[], provider_id, model}` → `{job}`（task=`paper-enrich`，AI 补全答案/解析/知识点）
- `POST /questions/{id}/verify` `{status:'ready'|'draft'}` → `{question}`（核验后 AI 不得覆盖）
- `GET /papers` / `POST /papers` `{title,kind,subject,grade,instructions?,layout?,items:[{question_ref,score,section?}]}`
- `GET/PATCH/DELETE /papers/{id}`；`POST /papers/{id}/items/reorder`
- `POST /papers/compose` `{mode:'smart'|'knowledge'|'wrong'|'layered'|'consolidate', filters, count, layout?,
  provider_id?, model?}` → smart/consolidate 走 AI（task=`paper-compose`，校验不过 422）；手工模式直接建卷
- `POST /papers/{id}/publish` `{published:bool}`（发布后只读；可导出/打印）
- `GET /papers/{id}/export?format=docx|html&answers=0|1&columns=1-4`（试卷：docx 服务端生成；html 为浏览器打印页；
  PDF 走浏览器「另存为 PDF」）；`POST /papers/{id}/cards` `{preset?,orientation?,columns?,bubble?,include_subjective?,
  student_no?}` → 答题卡模板；`GET /cards/{id}`、`PATCH /cards/{id}`（version CAS，支持 `options` 局部重排）、
  `DELETE /cards/{id}`；`GET /cards/{id}/svg?page&color=black|red`；`GET /cards/{id}/export?format=html|svg|pdf`
- 答题卡扫描（P9）：`GET /scan/meta`、`POST /papers/{id}/scan`（multipart 多页图片 + `card_id?` + `threshold?`，
  ≤10 页/≤30MB/页 → 定位点/透视矫正/46 位页面码/气泡 OMR/学号，结果含 `unreadable` 不猜读数）、
  `GET /scans/{id}`、`POST /scans/{id}/confirm`（人工纠错后判分并写 `pl_paper_attempts`，`source=scan`）、
  `GET /papers/{id}/scans`
- 本地 OCR（P1）：`GET /api/ai/ocr/info`、`POST /api/ai/ocr`（图片/PDF，≤5 页同步）；试卷侧
  `POST /imports/{id}/ocr`、`POST /imports/{id}/ocr-draft`（无视觉模型时按 OCR 行生成无答案草稿）；
  识别任务支持 `cross_verify/verify_provider_id/verify_model`（双模型逐题 diff，`verify_status/diff` 字段）
- 整卷练习：`POST /papers/{id}/attempts`、`GET /attempts/{id}`、`PATCH /attempts/{id}/answers`、
  `POST /attempts/{id}/submit`、`GET /papers/{id}/attempts`（提交后判分；`bank:` 与 `app:` 题错题都写入统一
  `wrong_questions`；整卷逐题入账以 `question_ref.paper_item_key(attempt_id, position)` =
  `paper:<attempt_id>:<item_position>` 为幂等键〔`submission_ledger.kind='paper_item'`〕，重复提交/重试不重复
  累加错题与掌握度；`app:` 题未关联成功时在响应 `app_bank_unlinked` 明示，已入账项在 `app_wrong_added`
  回报；题目标识 `ref` 统一归一化为 `bank:<qid>`/`app:<id>` 并带 `source/source_label`）
  - **题面冻结（P0，2026-11-21）**：创建作答的同一写事务把逐题快照冻结进 `pl_attempt_items`（题目引用、
    `snapshot_json`、`score/max_score`、题序、配图引用），试卷元数据写入
    `pl_paper_attempts.paper_snapshot_json`；之后改题/删题/重排/改分值/删卷不影响历史作答，判分使用冻结
    参考答案与分值。作答响应统一含 `frozen:bool`、`paper_deleted:bool`、`notice:str`（中文）；旧作答无
    冻结行时回退现读当前试卷并 `frozen=false` +「历史题面不可追溯」提示（不 500）；试卷已删除时用冻结
    元数据补齐卷面。
  - `DELETE /papers/{id}` 不再级联删除练习记录（返回 `attempts_kept`）；`GET /papers/{id}/attempts` 在删卷后
    仍返回历史记录。
  - 新增 `GET /attempts/{id}/items/{item_position}/images/{image_index}`：作答侧冻结配图流（按开始练习时的
    逐题快照，改题/重排/删卷后仍指向原图；本人校验，404 越界/非本人）。
- 题图资产所有权（P0，2026-11-21）：`GET /questions/{id}/images/{index}` 同时校验题目本人未删除、
  `pl_assets` 资产存在且可用、资产属于本人（或 `public_derivative` 已发布）、被本题 content 引用，
  任一失败统一 **404**；`POST/PATCH /questions` 校验配图引用（他人/未知/失效 **422**）；旧文件名引用用
  `python scripts/migrate_paperlab_assets.py --dry-run|--apply` 确认归属（幂等、不改写 content）。
- 拍题入库保存（跨模块，2026-11-21）：`POST /api/uploads/{id}/save-to-bank` 按本节契约写入 `pl_questions`：
  必须已人工确认（否则 **409**）、`expected_version` 对应 `ai_uploads.intake_revision` 乐观锁（**409**）、
  `saved_question_id` 已存在时幂等返回同一题；有答案 `status=ready`、否则 `draft`；默认把原图登记为
  `pl_assets` 资产并写 `content.images[{asset,role:'original',source:'upload'}]`。AI 建议答案只存
  `ai_uploads.ai_suggestion_json`，需人工采用并 `PATCH /api/uploads/{id}/confirm` 后才进入题目内容。
  该入口写入个人题时同时按下方「个人收录版本」记录 `origin=create` 的不可变版本。
- **upload → 个人题 → 投稿（Phase 2 跨模块链路）**：拍题入库保存的个人题（`pl_questions`，默认
  `status=draft|ready`、私有）可作为投稿来源：作者调用 `POST /api/plaza/submissions
  {question_id, source, license_confirmed}` 生成**不可变投稿快照**（白名单内容 + 来源/授权声明），投稿不复制
  原始上传文件、不自动公开个人题；公开图由下一行的公开衍生图链路单独生成。投稿状态机、评审与签发见
  `INTEGRATION-API-INDEX.md` §16 与 `CONTRACT.md` 第二轮不变量 §3–§4。
- **asset → 公开衍生图（Phase 2 跨模块）**：`app/plaza/figures.py` 在投稿进入公开可见状态（初审通过 /
  社区核验通过 / 签发精选）时，把快照引用的**本人** `pl_assets` 图片复制为 `public_derivative=1、
  status='published'` 的新资产（读文件 ≤20MB、`sha256` 复核一致才复制），在
  `plaza_public_assets(submission_id,version,image_index,public_asset_id,status,reason)` 登记映射；原件归属/
  用途不变，撤回只使公开副本映射失效（旧版本映射置 `disabled`）。无法公开的引用（题库原始素材、未登记归属、
  他人私有、停用/缺失/哈希不一致、非 png/jpeg/webp）不阻断审核，逐图记 `unavailable` 原因码并给编辑/访客
  中文提示；匿名读图 `GET /api/plaza/assets/{id}`、`GET /api/plaza/questions/{public_id}/images/{index}`
  同时校验「稿件当前公开 + 映射属于当前版本且 `active` + 公开副本可读」，失败 **404**、非白名单类型 **415**，
  绝不输出存储路径或他人私有资产；响应 `Cache-Control: no-store`，撤回立即失效。
- **个人收录版本（Phase 1 剩余，2026-11-21）**：`POST /questions/collect {qid}` 把只读题库题收录到
  `pl_questions` 并固定收录时版本（`source_ref=bank:<qid>` + 题库版本/revision + 内容指纹；同用户同来源
  重复收录幂等返回 `already_collected:true`，不静默覆盖）；`GET /questions/{id}/versions?limit=` 返回
  不可变版本历史（`pl_question_versions`，`origin=create|edit|collect|source_update|snapshot`，历史只读）；
  `GET /questions/{id}/source-status` 报告 `source_available`/`has_new_version`/收藏与当前 revision 及
  `diff.changed` 字段；`POST /questions/{id}/update-from-source {confirm:true, expected_version?}` 用题库新版
  生成新版本（未确认 **422**、版本冲突 **409**、已最新 `updated:false` 不建版本）。错误口径：题库不存在
  **404**、隔离/下架 **409**、题组共用材料/格式非法 **422**、题库暂不可用 **503**。列表/详情统一附
  `has_new_version`/`source_available`（`mybank` 页面据此显示「有新版」并可查看差异/选择更新）。
- **平台精选选题与建卷快照（Phase 3，2026-11-21）**：建卷/改卷 `items[].ref` 可传 `official:<public_id>`
  （格式非法 **422**、题不可见/撤回/争议在快照时 **404**），由 `papers.snapshot_official()` 冻结
  `source='official'`、`public_id`、`published_version`、`review_status(_label)` 与公开内容/公开配图引用
  （`images[{kind:'official',public_asset_id,url,accessible,reason}]`，不写私有路径）。冻结语义与整卷练习一致：
  已建卷/已开始练习不因精选题后续修订或撤回而改题面，判分用冻结答案；公开配图读取
  （`GET /papers/{id}/items/{pos}/images/{idx}`、`GET /attempts/{id}/items/{pos}/images/{idx}`）遇 official 图时
  按广场「公开副本 + 当前版本 + 公开状态」实时授权（撤回/停用 **404**）。整卷提交对 official 题写入统一错题本
  （`wrong_questions.qid=official:<public_id>`），响应新增 `official_bank_unlinked`/`official_wrong_added`；
  卷面投影带 `source_label/public_id/review_status/published_version`。AI 组卷（`paperlab.compose`）候选池仍只含
  题库与个人题，official 通过手工选题页签加入试题篮（组卷页「平台精选/全部来源」）。
- `GET /revision/options?subject` → `{wrong_count, kp_options:[...], modes:['replay','consolidate']}`
- `POST /revision` `{mode:'replay'|'consolidate', subject?, kp_ids?[], count?, provider_id?, model?}` → `{job}` 或 `{paper}`

### 4.3 学情分析 `/api/insight`

实时聚合（不建表）：作答、掌握度、错题、复习计划、AI 任务使用。

- `GET /overview` → `{summary:{attempts,correct_rate,mastery_counts,due_reviews,ai_jobs}, subjects:[{subject,attempts,accuracy,mastery}], activity:[{date,attempts}], trend:[{week,accuracy}]}`
- `GET /knowledge?subject=` → `{items:[{kp_id,title,path,attempts,accuracy,mastery_level,weak:bool}]}`
- `GET /mistakes?subject=` → `{by_reason:[{reason,count}], by_type:[...], by_kp:[...], recent:[...]}`
- `GET /report?subject=&days=` → 完整报告数据（学生个人报告；不含班级/学校维度）
- `GET /home?days=`（Phase 1 剩余，2026-11-21）→ 首页统计快照 `{generated_at, range, bank, official, personal, caliber}`：
  题库全局「可练/可自动判分/待核验/门禁排除」+ **平台精选口径**（第四轮追加：`official{available,total_questions,
  practice_pool,auto_gradable,attempts,practiced,wrong_count,due_reviews,caliber,note}`，与 `app/official.official_caliber`
  同源，撤回/争议查询时剔除；`bank.official_practice_pool/official_auto_gradable` 供首页题库卡片）+ 个人「待复习/
  今日练习/首次正确率/重复练习/待判分/薄弱知识点/我的题库/继续练习」，全部复用 `app/home_stats.py` 的真实口径
  （题库门禁复用 practice 查询构件；个人指标以 `attempts` 为唯一来源），前端首页/学情页不得本地估算；
  `days` 1–3650，默认 90。`GET /api/stats` 同步增加嵌套 `official` 块（平铺 `official_attempts/official_practiced/
  official_wrong_count` 语义不变）。
- `POST /narrative` `{provider_id, model, subject?}` → `{job}`（task=`insight-report`；AI 只基于真实统计生成文字建议，
  结果显著标注「AI 生成」）

### 4.4 AI 助手（小纸） `/api/assistant`

- `GET /conversations`；`POST /conversations` `{title?}` → `{conversation}`
- `GET /conversations/{id}?page&page_size` → `{conversation, messages:[{id,role,parts:[...],created_at}], has_older}`
- `PATCH /conversations/{id}` `{title}` → `{conversation}`（重命名）；`DELETE /conversations/{id}`
- `POST /conversations/{id}/messages` `{content, provider_id?, model?}` → `{job, message, conversation}`
  （task=`assistant-turn`；前端用既有任务 SSE 观察工具步骤；完成后重新拉取会话）
- `POST /conversations/{id}/stop` → `{cancelled:true}`（取消当前任务）
- `POST /conversations/{id}/confirm` `{action_id, confirm:bool}` → 写工具（建卷/建任务）的用户二次确认；
  助手只准备候选，不直接确认成绩、发布或代用户执行未确认的写操作。
- `GET /tools` → `{items:[{name,description,side_effect:bool,schema?}]}`（供 UI 展示能力边界）

工具白名单（服务端函数直调，不暴露任意 HTTP，共 **13 个**）：`search_source_bank`、`read_question`、
`search_official`（第四轮新增，只读检索平台精选）、`list_wrongs`、`get_stats`、`list_notes`、`read_note`、
`list_knowledge_points`、`list_mybank_questions`、`create_explain_job`、`create_revision_paper`、`compose_paper`、
`web_search`。所有工具以 `user_id` 复校验权限；写操作只准备候选，用户通过 `POST /conversations/{id}/confirm`
二次确认后执行，助手不能确认成绩或发布。

- **平台精选工具契约（2026-11-21 第四轮）**：`read_question` 接受 `official:<公开编号>`，读取走
  `app/official.py` 的查询时可见性（撤回/争议/不可见给清晰中文提示并**绝不回显题面/选项/答案**）；答案/解析仍只在
  「本人已作答」后返回。`search_official` 只返回公开投影（`public_id/source/source_label/review_status(_label)/
  status_label/published_version/featured/community_pending/subject/grade/type/stem(≤160)/has_answer/
  knowledge_tags`），不输出内部 ID/投稿人/审核员；至少给一个检索条件。系统提示要求引用写「平台精选 · 公开编号
  plz-...」并说明审核状态；`get_stats` 结果可带 `official` 口径（与 `/api/stats` 同源），既有计数语义不变。

- 技能库（P2）：`GET /skills`（内置 + 本人）、`POST /skills`、`PATCH /skills/{id}`（version CAS，内置 409）、
  `DELETE /skills/{id}`；会话/消息可选 `skill_ids`。技能只作为方法论注入系统提示末尾，不改变系统规则、
  工具白名单、未解锁门控与写操作确认；总长超限截断。

### 4.5 任务中心

纯前端页面，复用：`GET /api/ai/jobs`（扩展为分页+task 过滤时由 Lead 改）、`GET /api/ai/jobs/{id}`、
`POST /api/ai/jobs/{id}/cancel`、`POST /api/ai/jobs/{id}/resume`、既有 SSE `/api/ai/jobs/{id}/stream`。

Phase 3 起任务中心/管理台另消费 AI 用量与配额（`app/ai/usage.py`，见 `INTEGRATION-API-INDEX.md` §19）：
`GET /api/ai/usage/me?days=1` 显示今日任务数/tokens/失败数/费用估算与各类配额剩余（接口未就绪时整块隐藏，
不影响任务列表）；`GET /api/admin/ai/usage`、`GET|POST /api/admin/ai/quotas` 只在管理台使用。任务详情/列表展示
`started_at/finished_at/retry_count`、模型、tokens 与费用估算（无单价显示未知）；任务创建在超额/并发满时返回
**429**，前端展示可读提示且不落库。

### 4.6 Phase 3 跨模块链路（2026-11-21）

- **official → 练习 / 工坊**：`app/official.py` 只读适配层把广场公开题接进统一引用；练习侧由
  `app/practice.py` 在既有路由上支持 `official:`（详情/作答/错题/复习/统计），工坊由
  `app/paperlab/{papers,practice}.py` 冻结来源快照与配图引用（§4.2）。可见性查询时判定，撤回/争议即时失效，
  历史作答保留。
- **plaza → 通知 / 收藏**：质量运营的事件钩子（送审、初审、核验、争议、签发、撤回、回滚、抽检、反馈处理）
  在同一事务内写 `plaza_notifications` 并通知相关用户；收藏只对当前公开题有效、撤回后保留记录并通知收藏者；
  通知正文不含他人身份与评审原文。
- **ai_usage → 任务中心**：任务终态写 `ai_job_usage` 并在 `GET /api/ai/jobs/{id}` 之外提供本人/全站用量查询；
  配额与并发在校验阶段返回 **429**，前端在任务创建失败时展示配额提示，不把失败请求算作已消耗。

### 4.6.1 Phase 4 跨模块链路（2026-11-21 第四轮）

- **official → 助手**：`app/assistant.py` 的 `read_question`/`search_official` 只读调用 `app/official.py`；
  可见性在查询时判定，撤回/争议只给中文提示、不回显正文；工具结果携带公开编号与审核状态，回答必须写
  「平台精选 · 公开编号」——助手不会把精选题写成内部 ID，也不会替用户作答或确认成绩。
- **official → 课堂**：`POST /api/classroom/courses/{id}/generate` 的 `source_ref` 复用练习侧统一引用与可见性；
  建课时按当时可见性读取快照并写入 `outline.sources`（公开编号/精选版本/审核状态/来源声明），撤回后不可新建课程，
  已生成课程与导出保留来源标记（§4.1）。
- **gate / restore → 公开可见性**：`POST /api/plaza/submissions/{id}/publish` 的抽检门禁（`app/plaza/gate.py`）
  只决定「是否允许签发」，不改变公开可见性判定；签发被阻断时发布指针不落库，公开读保持原状。
  `POST /api/plaza/releases/{public_id}/restore` 恢复活跃发布指针后，广场公开读与 `official:` 精选检索/详情/
  作答**查询时即时**重新可见；`withdraw`/`rollback` 仍即时不可见——三者都只改指针/投稿状态，历史数据保留。
- **批量处理 / 通知增强（Phase 4 运营端）**：`POST /api/plaza/admin/{issues|reports|appeals}/batch` 为编辑/管理员
  批量处理反馈（≤50、部分成功 `updated/skipped`、每批写 `plaza_ops_batches` 审计）；`GET /api/plaza/notifications/
  unread-count` 与 `POST /notifications/read`（`all|ids[≤200]|kind`）只服务本人，未知 kind **422**；通知按
  「接收人+类型+投稿+版本」窗口去重（`STUDY_PLAZA_NOTIFY_DEDUPE_SECONDS`，默认 300s）。这些接口不改变
  公开可见性，也不把反馈/通知内容泄露给无关用户。
- **monitor → 只读探针**：`GET /api/admin/monitor` 复用 `app.main._health_token_ok` 的 `X-Health-Token` 口令做
  只读探针；只执行 SELECT、单检查独立降级、进程内短缓存（默认 15s），不写业务表、不触发业务函数。
  管理台 `web/js/pages/admin.js` 消费该接口展示五组卡片；部署可用同一探针口令读取而无需管理员会话。

## 5. 前端规范

1. 每个页面导出 `export async function renderX(container, version, params)`（与 `web/js/pages/*.js`
   既有约定一致），在渲染中使用 `web/js/core/dom.js` 的 `el/heading/button/field/input/toast/busy/icon`
   与 `core/api.js` 的 `api()`；禁止 `innerHTML` 拼接不可信内容（Markdown 用 `core/markdown.js`）。
2. 任务统一用 `core/jobs.js`（`startJobStream`/轮询兜底），UI 显示阶段、进度、取消、失败重试。
3. 大列表/编辑器：分页 + 防竞态（AbortController 或版本判定）；每次渲染检查 `version === state.renderVersion`。
4. CSS 写在本模块的 `web/styles-<module>.css`（已由 Lead 在 index.html 引入），类名加前缀
   （`.cls-`、`.pl-`、`.ins-`、`.asst-`、`.task-`），移动端 390px 无横向溢出，主控件 ≥44px，对比度 ≥4.5:1。
5. CSP 禁止内联脚本。AI 课堂交互场景通过 `/api/classroom/scenes/{id}/html` 单独响应（该响应由后端设置
   自己的 CSP），前端用 `<iframe sandbox="allow-scripts" srcdoc="...">` 或 `src` 加载；父页面 CSP 已允许
   `frame-src 'self'`。不要引入外部 CDN 资源（离线可用是产品要求）。
6. 新页面路由键已由 Lead 注册：`classroom`、`mybank`、`paperlab`、`insight`、`assistant`、`tasks`。
   页面内子视图用 hash 参数（`?course=1`、`?tab=import`）实现，不改中央路由。
7. Phase 2 前端（2026-11-21）：新增登录后工作台路由 `submission`（我的投稿）、`review`（评审台）、
   `plaza-admin`（编辑台）与公开只读路由 `plaza`（广场；`core/state.js` 的 `PUBLIC_ROUTES` 仅此一项）。
   访客壳只渲染公开路由；未登录访问非公开路由回落登录页并保留原 hash，登录后按原 hash 回跳。
   评审台/编辑台导航与页面按 `GET /api/plaza/me` 的 `capabilities.review|editor` 过滤，不按前端身份标签
   判断权限；工作台接口未就绪时页面显示占位/手工 ID 入口，不得崩溃或伪造审核结论。
8. Phase 3 前端（2026-11-21）：登录主壳挂载通知铃铛（`components/plazaNotifications.js`，未读轮询 60s、
   页面隐藏跳过、登出清理、访客壳不挂载、接口不可用时隐藏）；广场详情加收藏/纠错/举报（登录引导 + 原 hash 回跳）；
   编辑台加 `quality`/`ops`/`metrics` 分区与发布历史回滚（按 `/me` 能力位过滤，接口未就绪显示占位）；投稿台对
   `rejected/withdrawn/disputed` 显示申诉表单与记录；任务中心显示今日用量/配额面板，管理台显示 AI 用量/配额区。
   练习页提供来源筛选（`bank`/`official`/`all`），题目详情显示精选来源/版本/状态与题库题「收录到我的题库」入口，
   组卷页提供平台精选选题页签；首页「平台精选可练数量」仅在能力探测成功时显示。所有未就绪接口一律降级
   （隐藏控件/占位/提示），不得伪造数字或审核结论。
9. Phase 4 前端收口（2026-11-21 第四轮）：
   - `question.js`：official 公开配图按 `images[].accessible/url` 渲染（撤回后回退 `figure_notice`，兼容旧
     `figure_urls`）；精选纠错走 `POST /api/plaza/questions/{public_id}/issues`（登录引导、成功 toast、失败可读）；
     保留「在广场查看」（`#/plaza?id=<public_id>`）与来源/版本徽标。
   - `practice.js`：学段筛选（`grade`）进入筛选状态与 URL；选项来自 `GET /api/catalog` 的 `grade_stages`（缺失回退
     固定学段）；以合法请求响应是否含 `grade_filter` 做能力探测，旧后端隐藏控件并丢弃 `grade`；
     `grade_filter.applied=false` 时显示「仅平台精选生效」说明。
   - `insight.js`/`home.js`：读取 `/api/insight/home` 顶层 `official`（可练/已练/错题）与
     `bank.official_practice_pool`，能力探测失败隐藏，不写死数字。
   - `tasks.js`：任务深链 `#/paperlab?import=`、`#/paperlab?tab=papers&paper=`、`#/classroom?course=`、
     `#/notes?note=`、`#/notes?document=`（保持既有 `#/upload?upload=` 等）；未知任务不跳转、只提示。
   - `notes.js`：站内题目引用 `bank:`/`app:`/`official:` 显示类型标签 + 跳转 + 版本提示（`version/published_version/
     content_revision/bank_version`）；`app/anki_export.py` 只在错题卡/笔记卡背面与 tag 追加题目引用/来源/版本，
     不改 `.apkg` 模板字段。
   - `admin.js`：新增「运维监控」区（五组卡片、状态徽标、build/缓存/降级提示、`?refresh=1` 刷新），失败降级不崩页。
   - 新增控件/接口未就绪时必须降级（隐藏/占位/中文提示），不得把默认题库伪装成精选、不得本地估算统计。

## 6. 测试与验收规范

1. 每个后端模块至少覆盖：建表/初始化幂等、CRUD 归属校验（他人资源 403/404）、任务准入校验
   （未知参数/越权/上限 → 422）、任务执行器在 mock AI 下的成功路径与失败/取消路径、关键业务规则
   （禁止伪造、状态机、CAS 冲突）。
2. 测试用 `tests/conftest.py` 的 `client` fixture（TestClient + 隔离数据目录）；AI 用
   `monkeypatch.setattr(ai, "_call_model", fake)` 或模块内注入的测试钩子，不访问外网。
3. 前端：`node --check` 每个 JS 文件；UI 用 Playwright（已装）截图到 `screenshots/`，检查无 JS 异常、
   无横向溢出、关键操作可达。
4. 交付时给出：`python -m pytest -q` 全量结果、`node --check` 结果、`compileall`、本模块 UI 截图路径、
   以及实施记录（追加到 `.omx/plans/integration/`）。
5. Phase 3 新增专项测试：`tests/test_official_refs.py`（平台精选引用/可见性/列表/作答/错题/复习/工坊快照）、
   `tests/test_official_ui.py`（来源筛选/徽标/收录入口静态契约）、`tests/test_plaza_quality_ops.py`（抽检/通知/
   收藏/反馈申诉/回滚/指标）、`tests/test_plaza_ops_ui.py`（通知铃铛/编辑台分区/申诉入口）、
   `tests/test_ai_usage.py`（账本/配额/并发/查询权限），合计 107 passed（文档整理时实测）；本轮全量
   `python -m pytest -q` 为 **1557 passed / 3 skipped**（779.07s，无失败）。Phase 3 端到端验收阶段：
   WS24 已新增 `scripts/integration/official_accept.py`、`scripts/integration/ai_usage_accept.py` 并扩展
   `plaza_accept.py` 的 Phase 3 场景（抽检/收藏/纠错举报申诉/回滚/通知、精选学习链路、AI 用量）；
   `accept_runner --ui` 阶段接线与隔离实例一键验收由 Lead 收敛，最终结果以 Lead 收敛后的
   `screenshots/accept-report.json` 与 `docs/知序优化实施记录-20261121-第三轮.md` 为准。
6. Phase 4 新增专项测试（2026-11-21 第四轮）：`tests/test_official_integrations.py`（助手 official 工具/课堂
   official 素材与 sources/insight+stats official 口径/grade 筛选/catalog 元数据）、`tests/test_monitor.py`
   （五组检查/权限与探针口令/降级/缓存/撤回传播/管理台契约）、`tests/test_build_fingerprint.py`（指纹范围与
   `.dockerignore`/Dockerfile 契约）、`tests/test_plaza_quality_gate.py`（门禁模式/armed/override 审计/签发前抽检/
   一键恢复/批量部分成功/通知增强/指标字段）、`tests/test_phase4_ui.py`（official 配图与纠错/学段筛选/精选口径/
   任务深链/笔记与 Anki 字段/locale 对齐）；文档整理时实测五个文件 **105 passed in 81.34s**。
   全量 pytest 与一键/浏览器验收由 Lead 在隔离实例收敛后回填（见 `docs/知序优化实施记录-20261121-第四轮.md` §6.4）。

## 7. 定义完成（DoD）

- 功能可用：真实页面入口 + 真实 API + 空状态/错误/加载/取消/重试齐全，无 `TODO`/占位式假数据。
- 数据可靠：用户隔离、幂等、版本冲突可解释、删除级联正确、上限校验有中文提示。
- 证据齐全：测试通过、截图、实施记录（改了什么、验证了什么、未做什么及原因）。

## 8. 第五轮共用学习数据层（2026-11-21）

> 对应《知序功能整合与优化总方案》§4/§5.1/§5.2/§5.4 与 §8 工单 A–E 的首批交付；
> 详细记录见 `docs/知序优化实施记录-20261121-第五轮.md`。

### 8.1 模块与表归属（`init_*()` 各自幂等建表，不在 `app/db.py` 建业务表）

| 模块 | 表 | 归属/口径 |
| --- | --- | --- |
| `app/study_evidence.py` | `study_evidence` | 单事件表 + 版本行（`valid=1` 才算有效证据）；`UNIQUE(user_id,event_key)` 幂等；`record_event(con=)` 复用调用方事务；`revise_event` 只升版。 |
| `app/study_artifact.py` | `study_artifacts`、`study_artifact_revisions`、`study_artifact_links` | 附件元数据/不可变版本/跨页链接；CAS 版本冲突 409；软删；本人 404 隔离；data ≤256KiB、preview ≤64KiB。 |
| `app/study_review.py` | `review_cards`、`review_ratings` | 每用户每卡面键一行；四档评分日志含评分前后状态与服务端到期；幂等键唯一；算法/版本随行落库。 |

三张表的 `id` 已登记 `app/db.py::_ID_TABLES`（PostgreSQL 主键适配）；所有私有表带 `user_id` 并做归属校验；
时间统一 `db.utcnow()` ISO-8601 UTC；JSON 列 TEXT；SQL `?` 占位；不使用 SQLite 专有函数/PG `RETURNING`。

### 8.2 写入接线（同一事务，失败只记日志、不阻断主流程）

| 写入方 | 事件 `kind` / `record_kind` | `event_key` 规则 |
| --- | --- | --- |
| `app/practice.py::_record_attempt` | `attempt` / `attempts` | `attempt:attempts:<attempt_id>:-`（AI 复核经 `revise_attempt_evidence` 同键升版） |
| `app/paperlab/practice.py::submit_attempt` | `paper_attempt` / `pl_paper_attempts` | `paper:item:<attempt_id>:<position>` |
| `app/paperlab/scan.py::confirm_scan` | `paper_scan` / `paper_scan` | `paper:scan:<scan_id>:<position>`（表名 `pl_card_scans`，record_id 为扫描 id） |
| `app/classroom/api.py::submit_quiz` | `classroom_quiz` / `cls_quiz_attempts` | `quiz:<quiz_attempt_id>:<item_key>`（无统一引用时不伪造 `bank:/app:/official:`） |

`verdict` 仅 `correct/incorrect/pending/unknown`；`grade_source` 仅明确参考/交叉核验判分记 `reference`/`ai_cross_verified`，
否则 `pending`；缺少提示/揭晓信息记 `unknown`；学生自评单独 `self_report`，不覆盖核验结果。

### 8.3 复习卡与旧错题的关系

- `sync_wrong_question`/`sync_user_cards` 只读 `wrong_questions`；首次迁移取旧 `next_review_at` 建 `new` 卡，
  已有卡只同步归档标记（**不覆盖真实排程**）；可重复调用。
- 评分：`POST /api/review-cards/{id}/ratings`，请求 `{rating:1-4, card_version, idempotency_key}`；
  版本不符 409、同幂等键重放 `replayed=true`；外部引擎不可用时 200 降级（到期保持不变、不写评分行、`retry_allowed=true`）。
- 切换读路径前用 `GET /api/review-cards/compare` 输出 `matched/mismatched/missing/extra` 作为对账证据；
  旧 `GET /api/reviews`、`GET /api/review/queue` 暂不改口径。

### 8.4 OCR 块与确认请求体

- `POST /api/ai/ocr`、`POST /api/uploads/{id}/ocr-draft` 响应新增 `blocks/engine_version/capabilities`（旧字段保留）；
- `PATCH /api/uploads/{id}/confirm` 新增可选 `blocks`（≤200，kind∈{text,math,table,image}），传了才覆盖、
  未传沿用 draft/confirmed、旧 confirmed 无 blocks 时不加空键；
- `recognize_image`/`recognize_pdf_pages` 默认返回不变，`include_blocks=True` 才返回块（向后兼容）。

### 8.5 前端入口与降级

- 拍题页识别块校对（公式块用 `formulaInput.js`，原图定位框 `attachBlockOverlay`，确认带 `blocks`，422 保留本地编辑）；
- 错题复习页四档评分（`reviewRating.js`）：`/due` + `/stats` 并行读取，评分后只更新本行日期；接口不可用时显示
  「四档复习暂不可用，仍按原有到期时间复习」；
- 题目详情「查看原理」（`principleDemo.js` + `web/vendor/principles/*`）：按需 import，三视图（流程/等距/3D），
  无 WebGL 自动降级等距并中文提示，`onFrameReady` 回执供后续视频录制；「保存为学习附件」走 `/api/artifacts`；
- 新样式 `web/index.html` 引入；`study.formula/review/demo` locale 中英各 4,785 键对齐；
- `scripts/integration/ui_static_accept.py` 新增「第五轮组件与页面接线静态契约」步骤（组件齐全、页面接入点、
  播放器降级分支、本地 three.js、无 CDN、样式与关键键）。
