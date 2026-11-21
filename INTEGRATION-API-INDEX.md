# 知序整合工程 · 整合模块 API 索引（INTEGRATION API INDEX）

> 生成/核对方式（2026-11-21，只读）：用临时数据目录导入 FastAPI 应用生成路由清单，未启动服务、
> 未触碰生产库；随后逐条对照 `app/classroom`、`app/paperlab`、`app/insight.py`、`app/assistant.py`
> 源码与各自文档核对。**本索引只覆盖整合工程新增的六个模块面**；既有平台接口（练习/题库/笔记/
> 认证/设置等）见 `README.md` 与 `CONTRACT.md`。
>
> 维护约定：新增/修改整合模块路由、字段名、任务类型、上限时，同步更新本文件与对应模块文档；
> 已发布字段不得改名（`INTEGRATION-CONTRACT.md` §4）。只读审计报告见
> `.omx/plans/integration/API-AUDIT.md`（相对仓库根 `E:\responsibility`）。
>
> **本轮增量（2026-11-21 第二轮，task-10）**：新增 §14 拍题入库 OCR-first（`GET /api/uploads/{id}/intake`、
> `POST .../ocr-draft`、`POST .../ai-answer`、`POST .../save-to-bank`；`PATCH /api/uploads/{id}/confirm`
> 增加 `expected_version` 乐观锁）；补充 §3.5 整卷练习冻结与作答侧配图路由、§3.3 删卷保留练习、
> §3.2 题图资产所有权、§2.1 课堂素材回收与类型白名单；§7/§10 同步更新。每条新增路由均在代码中
> `grep @router` 核对过实现位置（见 §10 变更记录）。
>
> **本轮增量（2026-11-21 · Phase 1 剩余 + Phase 2，task-19）**：新增 §16 广场 / 投稿 / 评审 / 发布
> （`/api/plaza` **25 条**：公开只读与公开图、作者投稿链路、评审阈值与自动流转、签发/撤回/事件链、
> 角色身份与编辑台），新增 §15 统一题目引用（`bank:`/`app:`/`official:`）、幂等入账与统一复习队列、
> 个人收录版本与「有新版」、首页统计口径；同步更新 §0 计数、§4 学情 `/home`、§7 上限、§8 版本字段、
> §9 文档索引、§10 变更记录。核实方式：临时数据目录导入应用生成路由清单（plaza 25 / paperlab 64 /
> classroom 36 / insight 6，共 293 条 `/api` 路径）+ `grep @router` 与源码逐条对照；错误码与响应字段
> 全部取自 `app/plaza/**`、`app/question_ref.py`、`app/paperlab/versions.py`、`app/home_stats.py` 实现。
>
> **本轮增量（2026-11-21 · Phase 3，WS25）**：新增 §17 平台精选接入（`official:<public_id>` 读取适配层、
> `/api/questions?source=bank|official|all`、平台精选题的作答/错题/复习/stats、工坊建卷快照与冻结练习）、
> §18 质量运营（`/api/plaza` **25 → 44 条**：通知 2、收藏 3、纠错/举报/申诉 10、抽检 2、指标 1、回滚 1；
> 新增 6 张表与通知/事件钩子）、§19 AI 用量与配额（`ai_job_usage` / `ai_quotas` 账本、
> `GET /api/ai/usage/me`、`GET /api/admin/ai/usage`、`GET|POST /api/admin/ai/quotas` 4 条）；同步更新
> §0 计数、§1 错误对象与配额口径、§7 上限、§8 版本字段、§9 文档索引、§10 变更记录。核实方式：临时数据目录
> 只读导入应用生成路由清单（`/api/plaza` 44、AI 用量/配额 4、`/api` 路径共 **316** 条）+ `grep @router` 与源码
> 逐条对照；错误码/字段取自 `app/official.py`、`app/question_ref.py`、`app/practice.py`、
> `app/paperlab/{papers,practice}.py`、`app/plaza/{notify,favorites,feedback,quality,metrics,release}.py`、
> `app/ai/usage.py` 实现。
>
> **本轮增量（2026-11-21 · Phase 4 收尾，WS31）**：新增 §20 平台精选接入助手与课堂、§21 运维监控
> （`GET /api/admin/monitor` 五组只读检查/降级/短缓存/构建分发治理）、§22 质量运营闭环
> （抽检门禁 `app/plaza/gate.py`、签发前抽检、一键恢复 `POST /api/plaza/releases/{public_id}/restore`、
> 三类批量处理、通知 `unread-count`/`kind` 全读/窗口去重、指标新增字段）、§23 第四轮前端收口
> （official 配图/纠错、学段筛选、精选学情口径、任务深链、笔记/Anki 引用与版本、管理台监控区）；
> 同步更新 §0 计数（`/api/plaza` **44 → 51 条**、`/api/admin/monitor` 1 条、`/api` 共 **324** 条目）、
> §2.1 课堂生成参数、§4 `/api/insight`、§5 助手工具计数、§15.4 首页 official 口径、§17.2 学段筛选、
> §17.5/§17.6 前端与边界、§18 计数与新增路由/指标、§9 文档索引、§10 变更记录。核实方式：临时数据目录
> 只读导入应用生成路由清单 + `grep "@router"`/源码逐条对照（详见 §10 与实施记录 §6.1）。

## 0. 快速信息

| # | 模块 | 前缀 | 路由数 | 任务类型 | 主文档 |
|---|---|---|---|---|---|
| 1 | AI 课堂（OpenMAIC） | `/api/classroom` | 25 | `course` | `app/classroom/API-SAMPLES.md`、`EXPORT-INTERACTIVE.md`、`MULTIAGENT-API.md`、`GENERATION-PLUS.md` |
| 2 | 试卷工坊（Oh My Paper + 小鹿） | `/api/paperlab` | 50 | `paper-import`、`paper-enrich`、`paper-compose`、`paper-revision`、`paper-annotate` | `app/paperlab/API.md`、`PAPERS-API.md`、`CARDS-API.md`、`ANNOTATE-API.md`、`BANK-IO.md` |
| 3 | 学情分析 | `/api/insight` | 5 | `insight-report` | 本文 §3（无独立文档） |
| 4 | AI 助手（小纸） | `/api/assistant` | 13 | `assistant-turn` | `app/assistant.md` |
| 5 | 任务中心（复用） | `/api/ai/jobs*` | 7 | 全部任务 | `README.md` / `CO.md` 既有文档 + 本文 §5 |
| 6 | 模型连接与设置（复用） | `/api/providers*`、`/api/ai/task-defaults`、`/api/models/*` | 复用 | — | `README.md`、`app/ai/providers.py` |
| 7 | 广场 / 投稿 / 评审 / 发布 / 质量运营（Phase 2 + 3 + 4） | `/api/plaza` | **51** | — | 本文 §16、§18、§22、`app/plaza/**` 模块 docstring |
| 8 | AI 用量与配额（Phase 3） | `/api/ai/usage`、`/api/admin/ai/*` | **4** | 全部任务（账本/配额） | 本文 §19、`app/ai/usage.py` docstring |
| 9 | 运维监控（Phase 4） | `/api/admin/monitor` | **1** | — | 本文 §21、`app/monitor.py` docstring |

合计整合模块路由 **93** 条（classroom 25 / paperlab 50 / insight 5 / assistant 13），
全部经依赖注入 `current_user` 校验登录（脚本遍历 `route.dependant` 校验，0 条缺失）；
私有资源再按 `user_id` 归属校验（他人资源统一 **404**，不泄露存在性）。

> 计数口径：上表与「合计 93 条」为 task-24 生成时点值；§11–§14 的后续增量（课堂导入/编辑器/导出/视频、
> 工坊 OCR/扫描/PDF、拍题入库等）未回填。2026-11-21 第二轮对工作区 `grep @router` 重算为
> `app/classroom/**` 36 条、`app/paperlab/**` 60 条、`app/insight.py` 5 条、`app/assistant.py` 13 条；
> 精确清单以文首 openapi 复现命令重新生成为准（本表保留历史口径，不逐轮改写）。
>
> **2026-11-21 本轮（Phase 1 剩余 + Phase 2）再次重算**：`app/classroom/**` 36 条、
> `app/paperlab/**` **64 条**（新增个人题版本 4 条）、`app/insight.py` **6 条**（新增 `/home`）、
> `app/assistant.py` 13 条，整合模块面合计 **119 条**；另新增独立模块面 `app/plaza` **25 条**
> （第 7 行，不计入上面「93 条」历史口径）；`/api/uploads` 9 条与 `app/practice.py` 45 条属既有平台模块，
> 本轮增量（统一题目引用、幂等入账、复习队列、个人收录版本、首页统计）在 §15 单列。
> 鉴权口径：plaza 公开只读 3 条用 `optional_user`（无会话按匿名处理，不 401），公开图 2 条无登录依赖；
> 其余模块仍全部 `current_user`；平台角色（评审员/学科复核员/编辑）在接口内用
> `service.require_reviewer|require_editor|require_plaza_role` 复校验（`admin` 直通）。
>
> **2026-11-21 Phase 3 再次重算（本轮）**：临时数据目录只读导入 `app.main` 生成路由清单——
> `/api/plaza` **44 条**（Phase 2 25 条 + Phase 3 新增 19 条，见 §18.1）、AI 用量/配额 **4 条**
> （`/api/ai/usage/me` 1 + `/api/admin/ai/*` 3 个路径/4 个方法）、`/api/paperlab` 64 条、
> `/api/ai/jobs` 7 条、`/api` 路径条目共 **316** 条（第二轮 293 + 新增 19 + AI 用量 4 = 316）。
> 平台精选接入**不新增 HTTP 路由**：复用 `/api/questions`（新增 `source` 参数）、练习作答/错题/复习/
> 统计与工坊既有路由，由 `app/official.py` 只读适配层提供数据（§17）。本轮新增路由的鉴权：
> 通知/收藏/反馈/申诉/用量为 `current_user`（用量管理为 `require_admin`），抽检/指标/队列/回滚为
> `service.require_plaza_role("editor")`（`admin` 直通）。
>
> **2026-11-21 Phase 4 再次重算（本轮）**：临时数据目录只读导入 `app.main` 生成路由清单——
> `/api/plaza` **51 条**（Phase 3 44 + 第四轮新增 7：`GET /notifications/unread-count` 1、
> `GET|PUT /admin/quality/gate` 2、`POST /releases/{public_id}/restore` 1、
> `POST /admin/{issues|reports|appeals}/batch` 3）、`GET /api/admin/monitor` **1 条**、
> `/api` 方法条目共 **324 条**（Phase 3 316 + 8；路径 277 条）。其它模块面：`/api/classroom` 36、
> `/api/paperlab` 64、`/api/insight` 6、`/api/assistant` 13、`/api/ai/usage` 2、`/api/admin/ai` 3（4 方法）。
> 平台精选接入**助手/课堂**与**学段筛选**同样不新增 HTTP 路由：助手在既有 `/api/assistant/*` 上新增只读工具
> `search_official` 并扩展 `read_question`（工具 12 → **13**），课堂在 `POST /courses/{id}/generate` 上新增
> `source_ref` 参数，筛选复用 `GET /api/questions?grade=` 与 `GET /api/catalog` 元数据，学情/统计复用
> `GET /api/insight/home`、`GET /api/stats` 新增字段（§20）。本轮新增路由鉴权：通知未读数为 `current_user`；
> 抽检门禁读取为编辑/管理员、修改**仅管理员**（`require_admin`）；一键恢复与批量处理为编辑/管理员
> （`require_plaza_role("editor")`，作者回避 403）；`/api/admin/monitor` 为管理员会话或 `X-Health-Token`
> 部署探针口令只读（§21）。

复现命令（不启动服务）：

```powershell
$env:STUDY_DATA_DIR = Join-Path $env:TEMP ("zhixu-openapi-" + [guid]::NewGuid())
$env:STUDY_INDEX_PREWARM = "0"
$env:STUDY_ALLOWED_HOSTS = "testserver,127.0.0.1"
python -X utf8 -c "from app.main import app; import json; open('openapi.json','w',encoding='utf-8').write(json.dumps(app.openapi(), ensure_ascii=False, indent=2))"
```

## 1. 公共约定

- **鉴权**：除 `POST /api/auth/register|login` 等认证入口外全部需登录；整合模块 93 条路由全部
  `Depends(current_user)`。跨账号访问一律 `404`（`403` 仅用于管理员接口/内置只读资源）。
- **错误**：统一 `{"detail": "中文说明"}`；个别接口的 `detail` 为对象（切题标注 409、
  切题发布校验 422，见 `app/paperlab/ANNOTATE-API.md` §3.2/§6；本轮另有：AI 配额/并发 **429**
  `{code:'ai_quota_exceeded', kind, message}`、通知已读参数 **422** `{message, field:'ids|all'}`、
  收藏不可用 **409** `{message,status,status_label}`、反馈/抽检队列未知状态 **422** `{message,allowed}`、
  AI 配额调整 **422** `{code:'unknown_task_kind',message}`，见 §18/§19）。
- **状态码基线**：`401` 未登录；`403` 非本人/只读；`404` 不存在或非本人；`409` 版本冲突/状态不允许；
  `413` 文件超限；`422` 参数/前置条件不满足；`429` 配额/并发（任务配额按类型、UTC 自然日；失败/取消不计数，
  见 §19）；`503` 依赖未就绪或上游失败。
- **列表分页**：`{items, total, page, page_size}`；`page_size` 服务端上限见 §7。
- **时间**：ISO-8601 UTC 字符串；**JSON 字段**统一 `_json` 后缀存 TEXT（SQLite/PostgreSQL 双兼容）。
- **任务**：`POST` 创建返回 `{job}`（或 `{job, ...业务 id}`）；进度用 `GET /api/ai/jobs/{id}` 或 SSE
  `GET /api/ai/jobs/{id}/stream`；取消 `POST .../cancel`、续跑 `POST .../resume`（任务参数必须可重建）。
- **AI 产物可追溯**：任务结果/场景 meta/题库记录带 `model`、`prompt_version`（各模块 PROMPT_VERSION 见 §8）；
  本轮起每个任务终态另写一条 `ai_job_usage` 账本（模型/状态/耗时/重试/token 汇总/费用估算/入参分类，
  不记录题干与资料内容），并提供按类型的每日配额与全站并发（§19）。

## 2. AI 课堂 `/api/classroom`（25 条）

### 2.1 元信息 / 课程 / 场景

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/meta` | 场景类型/模式/深度/widget、TTS、多智能体（`agent_roles`/`discussion_modes`）、`interactive_mode`、`images`、limits、`prompt_version`（v2） |
| GET | `/courses` | 课程列表 `?page&page_size&status` |
| POST | `/courses` | 建草稿课（不调 AI）：`{title?, topic(2-500), subject?, grade?, mode?, requirements?, document_ids?}` |
| GET | `/courses/{course_id}` | `{course, scenes}`（`course.interactive_mode`；scenes 含 content/actions/meta）；素材缺失时追加 `missing_asset_ids` 与中文 `warnings`（页面占位降级，不 500） |
| PATCH | `/courses/{course_id}` | `{title?, description?, subject?, grade?}` |
| DELETE | `/courses/{course_id}` | 级联删场景/会话；有运行中 `course` 任务 → 409；素材按跨课程存活引用安全回收，返回审计 `{deleted, course_id, assets:{examined,retained,reclaimed,shared_files_kept,files_removed,file_errors}}`（共享素材迁移归属课程、只回收无引用素材） |
| POST | `/courses/{course_id}/generate` | `{stage:'outline'|'scenes'|'all', provider_id, model, scene_types?, scene_count?3-12, depth?, requirements?, material_text?, images?, images_per_scene?0-2, image_provider_id?, image_model?, web_search?, web_search_limit?1-8, source_ref?}` → `{job, course_id}`；`stage=outline/all` 先进入 `awaiting_outline`；`source_ref=official:<公开编号>` 为第四轮新增**平台精选题课程素材**（见下方说明） |
| POST | `/courses/{course_id}/scenes/{scene_id}/regenerate` | `{provider_id, model, instruction?}` → `{job, course_id, scene_id}`（单场景，不触发自动名册/配图） |
| POST | `/courses/{course_id}/sessions` | 建学习会话 `{scene_id?}` → `{session}` |
| GET | `/sessions/{session_id}` | `{session, course, progress, scenes(摘要), turns(最近 20)}` |
| PATCH | `/sessions/{session_id}` | `{scene_id?, progress?}` 进度保存（progress ≤16KB） |
| POST | `/sessions/{session_id}/quiz` | `{scene_id, answers:[{question_id, values}]}` → 判分；写 `cls_quiz_attempts` |
| POST | `/sessions/{session_id}/tutor` | 单智能体 tutor：`{scene_id?, message, mode:'qa'|'discussion', history?, provider_id?, model?}` → `{reply,scene_hint,usage,...}` |
| POST | `/sessions/{session_id}/discussion` | 多角色讨论：`{scene_id?, mode:'discussion'|'qa'|'lecture', topic?, rounds?1-3, user_message?, provider_id?, model?}` → 1-6 段 `turns`，写 `cls_turns` |
| POST | `/sessions/{session_id}/interject` | 学生插话：`{scene_id?, message}` → 1-2 段回应，写 `cls_turns` |
| POST | `/courses/{course_id}/tts` | `{text(≤2000), scene_id?, voice?, speed?, provider_id?, model?}` → `{asset}` 或 `{fallback:'browser'}` |
| GET | `/assets/{asset_id}` | 本人素材流（TTS/旁白音频、配图、视频；`Cache-Control: private` + ETag/304）；按内容嗅探真实类型：仅白名单媒体（png/jpeg/webp/gif、常见音频、video/mp4）内联，其余 `Content-Disposition: attachment` + `X-Content-Type-Options: nosniff` + `CSP: default-src 'none'; sandbox` 隔离下载（主动内容降级 `application/octet-stream`）；文件缺失/被回收 404 中文降级 |
| GET | `/scenes/{scene_id}/html` | 交互场景 HTML（本人；独立 CSP + `X-Frame-Options: SAMEORIGIN`） |
| POST | `/scenes/{scene_id}/interactive-event` | 交互页运行时事件上报 `{type, session_id?, message?, summary?, state?}`（state ≤8KB；session 归属校验） |
| GET | `/courses/{course_id}/export` | `?format=pptx|html|zip|package&include_tts=`（HTML/ZIP 可内联 TTS；配图在 PPTX/HTML 内联 data URI；package=教学资源包：course.pptx + 每个交互场景独立自包含 HTML + course.json/ets.json + README.txt） |
| POST | `/courses/import` | multipart `.zip` 导入（≤200MB，超限 413；校验 manifest/场景/素材 sha256；不调模型）；素材按内容嗅探真实类型（不采信 manifest 声明），伪造/主动内容降级为隔离下载并在 `asset_warnings` 返回中文提示（≤20 条） |
| POST | `/import` | 上一行的别名（两条路径同时可用） |

> **课堂素材安全（P0/P1，`app/classroom/images.py`）**：删课不直接删素材——先现查该用户其它课程/场景的
> 存活引用，仍被引用的素材把 `course_id` 迁移到引用课程并保留文件；同一 `storage_name` 还被其它素材行
> 共享时只删行、不删文件；只删除「无存活引用且文件未被共享」的素材（事务提交后复查，避免误删并发写入）。
> 删除接口返回的 `assets` 审计可直接用于前端提示；`GET /courses/{id}` 的 `missing_asset_ids`/`warnings`
> 让页面显示占位而不是 500；`GET /assets/{id}` 的类型判定以字节内容为准，导入素材的声明类型不参与信任。
>
> **平台精选题课程素材（P4，2026-11-21 第四轮，`app/classroom/{api,generate,prompts}.py`）**：
> `POST /courses/{id}/generate` 新增可选 `source_ref=official:<公开编号>`（目前**只支持 official**，
> `SEED_QUESTION_SOURCES=("official",)`）。格式非法/来源不支持 **422**；未发布/撤回/争议在任务准入
> （`validate_course_job`，仅 `stage=outline|all` 且尚无大纲时）与生成大纲时各校验一次并 **404**，
> 撤回题提示「不能再作为新课程素材；历史课程保留当时的来源标记」。大纲 prompt 注入
> 「公开编号/审核状态/学科/学段/知识点/题面/选项/参考答案（仅供备课）/解析要点/来源声明」
> （`prompts._official_seed_block`，上限 4000 字，规定引用必须写「平台精选 · 公开编号 plz-...」且不得编造）；
> `outline.sources` 记录 `{kind:'official',public_id,published_version,review_status(_label),summary,
> source_declaration,question_ref}`，`course.config.seed_source` 记录本次建课来源；任务结果追加
> `official_source_count/official_sources[]`，summary 追加「课程素材来源：平台精选 plz-xxx（vN）」；
> 导出（PPTX/HTML/ZIP `sources.json`/资源包）沿用 `outline.sources` 保留来源标记；撤回不影响已生成课程与导出。

### 2.2 角色名册（多智能体，task-14）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/courses/{course_id}/agents` | 名册 `{roster:{teacher,classmates}, agents, has_roster, prompt_version, generated_at, limits, roles}` |
| POST | `/courses/{course_id}/agents/generate` | 一次结构化调用生成名册（覆盖已存）：`{provider_id?, model?, requirements?, classmate_count?1-5}` |
| PATCH | `/courses/{course_id}/agents` | `{roster}` 完整替换 / `{teacher:{id,...}, classmates:[{id,...}]}` 局部编辑 / `{clear:true}` 清空 |

`course` 任务会在生成场景前自动补名册（失败不阻塞；结果 `roster_generated/roster_agent_count/roster_warnings`）。

## 3. 试卷工坊 `/api/paperlab`（50 条）

### 3.1 元信息 / 导入识别 / 核验入库

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/meta` | 学科/年级/题型/难度、limits、capabilities（`papers/answer_sheet` 已为 true）、`prompt_version` |
| POST | `/imports` | multipart `files[]`（图片/PDF/DOCX；≤30MB/个、≤20 文件、≤20 页）+ `title?/subject?/grade?` → `{import, pages}` |
| GET | `/imports` | 导入列表 `?page&page_size&status` |
| GET | `/imports/{import_id}` | `{import, pages, items, counts}` |
| GET | `/imports/{import_id}/pages/{page_no}/image` | 页图文件流（本人；纯文本页 404） |
| POST | `/imports/{import_id}/recognize` | `{page_from?, page_to?, provider_id, model}` → `{job}`（task `paper-import`） |
| PATCH | `/imports/{import_id}/items/{item_id}` | 核验草稿 `{draft?, bbox?, status?, skip_reason?}`；已入库 409 |
| POST | `/imports/{import_id}/commit` | `{item_ids?, target:'app_bank'|'paper'}` → 入库（幂等；重复 commit 不重复建题） |
| GET | `/imports/{import_id}/pages` | 页列表（尺寸/图地址/标注概况/草稿统计） |
| GET | `/imports/{import_id}/pages/{page_no}/annotation` | 页级切题标注（归一化 boxes/version） |
| PUT | `/imports/{import_id}/pages/{page_no}/annotation` | 保存切题标注（`version` CAS；409 `detail` 为对象） |
| POST | `/imports/{import_id}/resegment` | `{pages?, keep_items?}` 重切出草稿（≤20 页同步；>20 页 → task `paper-annotate`） |
| POST | `/imports/{import_id}/items/{item_id}/answer-box` | 单题答题区坐标 `{box}` |
| POST | `/imports/{import_id}/publish` | 发布前校验（只统计+校验，不复制 commit 语义） |

### 3.2 可写题库 / 统计 / 导入导出

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/questions` | 题库列表 `?subject&grade&qtype&keyword&status&verify_status&page&page_size` |
| POST | `/questions` | 建题 `{type,subject,grade,difficulty?,knowledge_tags?,content,status?}` |
| GET | `/questions/{question_id}` | 单题详情（软删除不返回） |
| PATCH | `/questions/{question_id}` | 编辑（`version` CAS，冲突 409；已核验字段保护） |
| DELETE | `/questions/{question_id}` | 软删除 |
| POST | `/questions/{question_id}/verify` | 核验 `{status:'verified'|'rejected'|'pending'}` |
| GET | `/questions/{question_id}/images/{image_index}` | 题级裁切配图（本人） |
| GET | `/questions/stats` | 题库统计（题型/学科/状态/核验/缺字段/知识点 Top/近 7 天/AI 生成） |
| GET | `/questions/export` | `?format=json|csv&subject&grade&status` 文件下载（≤5000 题） |
| POST | `/questions/import` | multipart JSON/CSV（≤10MB、≤2000 题；`dry_run` 预览；内容摘要去重） |
| POST | `/questions/enrich` | `{question_ids[], provider_id, model}` → `{job}`（task `paper-enrich`；1-50 题） |

> **题图资产所有权（P0，`app/paperlab/assets.py`）**：新配图由服务端签发不可猜的 `asset_id` 并登记
> `pl_assets`（owner/用途/`sha256`/`size`/状态/`public_derivative`）；`content.images[i].asset` 只存
> `asset_id`，不再使用客户端或旧内容寻址文件名。`GET /questions/{id}/images/{index}` 同时校验
> 「题目本人且未删除 + 资产存在且 `active/published` + 资产属于当前用户（或 `public_derivative=1` 且
> `published`）+ 被本题 content 引用」，任一失败统一 **404**（不泄露他人资产是否存在）。
> `POST/PATCH /questions` 校验配图引用：他人/未知/失效引用 **422**；旧内容里的原文件名引用仅在
> 「编辑旧内容且原样保留」时兼容，确认归属用 `python scripts/migrate_paperlab_assets.py --dry-run`
> 预检、`--apply` 登记（幂等、不改写 content；有登记失败退出码 1，`pl_assets` 未建表退出码 2）。
> 工坊导入入库裁图会登记 `asset_id`，无法归属的旧引用被移除并给出中文提示（`asset_warnings`）。

### 3.3 试卷 / 组卷 / 错题巩固 / 导出

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/papers` | 试卷列表 `?subject&kind&status&page&page_size` |
| POST | `/papers` | 手工/考试卷 `{title,kind,subject,grade?,instructions?,layout?,items:[{ref,score?,section?}]}`（≤60 题） |
| GET | `/papers/{paper_id}` | `{paper, items}`（`?include_snapshot=true` 含冻结快照） |
| PATCH | `/papers/{paper_id}` | `{version, ...}`；`items` 整卷替换；已发布 409 |
| DELETE | `/papers/{paper_id}` | 删除试卷（题项快照级联）；不再级联删除练习记录，返回 `{deleted, paper_id, attempts_kept}`，历史练习按冻结快照回看 |
| POST | `/papers/{paper_id}/items/reorder` | `{version, item_ids[]}` 排序 |
| POST | `/papers/{paper_id}/publish` | `{published:true|false}`（无题发布 422；发布后结构只读） |
| POST | `/papers/compose` | `{mode:'smart'|'knowledge'|'wrong'|'layered', subject, grade?, count?1-60, type_counts?, difficulty_target?, tolerance?, kp_weights?, filters?, provider_id, model}` → `{job, candidates}`（task `paper-compose`；预检不足 422 不建任务） |
| GET | `/papers/{paper_id}/export` | `?format=docx|html&answers=0|1&columns=1-4`（快照只读；html 供浏览器打印） |
| GET | `/revision/options` | `?subject=` → 错题数/知识点选项/modes/limits |
| POST | `/revision` | `{mode:'replay'|'consolidate', subject?, kp_ids?, count?1-20, provider_id?, model?}` → replay 同步建卷 / consolidate → `{job}`（task `paper-revision`；候选不足 422） |

### 3.4 答题卡

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/papers/{paper_id}/cards` | 生成答题卡 `{preset?, orientation?, columns?1-4, bubble?, include_subjective?, student_no?, title?}`（≤20 张/卷） |
| GET | `/papers/{paper_id}/cards` | 卡列表（惰性刷新 stale） |
| GET | `/cards/{card_id}` | 卡详情 `{card, layout}` |
| PATCH | `/cards/{card_id}` | `{version, title?, layout?}`（CAS；stale 卡 409） |
| DELETE | `/cards/{card_id}` | 删除卡 |
| GET | `/cards/{card_id}/svg` | `?page=N&color=black|red` → `image/svg+xml`（页码越界 404） |
| GET | `/cards/{card_id}/export` | `?format=html|svg&page=all|N&color=`（stale 带 `X-Card-Stale`） |

### 3.5 整卷练习（个人自测；P0 起题面冻结）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/papers/{paper_id}/attempts` | 开始练习 → `{attempt, paper, items, frozen:true, paper_deleted:false, notice:""}`（卷面不含答案；创建作答的同一事务冻结逐题快照） |
| GET | `/papers/{paper_id}/attempts` | 该卷练习历史（删卷后仍有记录则照常返回，不 404） |
| GET | `/attempts` | 练习列表（`?paper_id=`）同上的别名入口 |
| GET | `/attempts/{attempt_id}` | 练习详情（未交卷只含已存作答；已交卷追加判分/参考；含 `frozen/paper_deleted/notice`） |
| PATCH | `/attempts/{attempt_id}/answers` | `{item_position, answer}` 幂等保存（单题 ≤16KB；题位取自冻结快照；已交卷 409） |
| POST | `/attempts/{attempt_id}/submit` | `{answers?}` 交卷判分（重复提交 409；判分用冻结参考答案与分值；错题联动） |
| GET | `/papers/{paper_id}/items/{item_position}/images/{image_index}` | 试卷快照配图流（本人；位置从 1 开始） |
| GET | `/attempts/{attempt_id}/items/{item_position}/images/{image_index}` | **本轮新增**：作答侧冻结配图流（按开始练习时的逐题快照，改题/重排/删卷后仍指向原图；本人；404 非本人/越界/无此图） |

**题面冻结（P0，`pl_attempt_items`，`app/paperlab/practice.py`）**：创建作答的同一写事务内，把
`papers.list_items` 的逐题快照冻结为 `pl_attempt_items` 行（题目引用、`snapshot_json`、分值
`score/max_score`、题序与配图引用），并把试卷元数据写入 `pl_paper_attempts.paper_snapshot_json`。
此后改题、删题、重排、改分值、删卷都不影响已开始的作答；`GET/PATCH/submit/结果/历史回看/配图`
一律优先冻结快照，判分使用冻结的参考答案与分值。作答响应统一新增：
- `frozen: bool`、`paper_deleted: bool`、`notice: str`（中文提示）；
- 正常冻结：`frozen=true`、`notice=""`（前端显示固定文案「本次练习已固定开始时的题面」）；
- 旧作答无冻结行：回退现读当前试卷并 `frozen=false` + 「该练习创建于题面冻结之前，历史题面不可追溯……」提示，不 500；
- 试卷已删除：用冻结元数据补齐卷面（`paper_deleted=true`），历史作答/判分/回看不受影响。

## 4. 学情分析 `/api/insight`（6 条，实时聚合不建表）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/overview` | `?days(1-3650, 默认 90)&subject=` 汇总/各科/活跃/趋势 |
| GET | `/knowledge` | `?days&subject=` 知识点掌握列表（弱项/未开始） |
| GET | `/mistakes` | `?days&subject=` 错因/题型/知识点分布 |
| GET | `/report` | `?days&subject=` 完整个人报告数据 |
| GET | `/home` | **第二轮新增 / 第四轮扩展**：首页统计口径（`?days(1-3650, 默认 90)`）→ `{generated_at, range, bank, official, personal, caliber}`：题库全局「可练/可自动判分/待核验/门禁排除」+ **平台精选口径**（第四轮：顶层 `official{available,total_questions,practice_pool,auto_gradable,attempts,practiced,wrong_count,due_reviews,caliber,note}`，`bank.official_practice_pool/official_auto_gradable`）+ 个人「待复习/今日练习/首次正确率/重复练习/待判分/薄弱知识点/我的题库/继续练习」；实现 `app/home_stats.py`，与 `/overview`、`/report`、`/api/stats` 同一数据源，见 §15.4、§20.2 |
| POST | `/narrative` | `{provider_id, model, subject?, days?}` → `{job}`（task `insight-report`；无真实学习记录 422 不生成，避免编造） |

## 5. AI 助手 `/api/assistant`（13 条）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/conversations` | 会话列表（`message_count/preview/job_id/pending_action/skill_ids`） |
| POST | `/conversations` | 新建 `{title?, skill_ids?}` |
| GET | `/conversations/{id}` | 会话 + 消息分页 `?page&page_size`（page=1 为最新页，`has_older`）；含 `skills` |
| PATCH | `/conversations/{id}` | `{title?, skill_ids?}`（至少一项） |
| DELETE | `/conversations/{id}` | 删除会话（回复中 409） |
| POST | `/conversations/{id}/messages` | `{content(≤4000), provider_id, model, skill_ids?}` → `{job, message, conversation}`（task `assistant-turn`） |
| POST | `/conversations/{id}/stop` | 取消当前轮 → `{cancelled, notice, job, conversation}` |
| POST | `/conversations/{id}/confirm` | 写操作二次确认 `{action_id, confirm}`（HITL：建卷/组卷/讲解任务） |
| GET | `/tools` | 工具白名单（**13 个**，`side_effect`/`requires_confirmation`/参数摘要）+ 上限 + `scope_note`（第四轮新增只读工具 `search_official`；`read_question` 支持 `official:<公开编号>`，见 §20.1） |
| GET | `/skills` | 技能库（内置只读 + 本人自建，含 content/enabled/version/limits） |
| POST | `/skills` | 新建自建技能 `{name≤40, description≤200, content≤20000, enabled?}`（≤100/人） |
| PATCH | `/skills/{id}` | 编辑/启停（`version` CAS；内置 409） |
| DELETE | `/skills/{id}` | 删除自建技能（内置 409） |

## 6. 任务中心（复用 `/api/ai/jobs`，7 个入口）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/ai/jobs` | 通用任务提交（整合模块通常用自己的业务入口提交，便于校验） |
| GET | `/api/ai/jobs` | 任务列表（当前返回本人最近 **50** 条，暂无分页/`task`/`status` 过滤；契约 §4.5 的「分页+task 过滤」仍待 Lead 扩展，见 `API-AUDIT.md` A11） |
| GET | `/api/ai/jobs/{id}` | 任务详情（status/stage/error/request/result/checkpoint 摘要） |
| POST | `/api/ai/jobs/{id}/cancel` | 取消（执行器检查 `is_job_cancelled`，保留幂等产物） |
| POST | `/api/ai/jobs/{id}/confirm-outline` | 确认/编辑课程大纲后继续（`course` 任务 `awaiting_outline` 专用） |
| POST | `/api/ai/jobs/{id}/resume` | 续跑（执行器读 checkpoint 跳过已完成部分） |
| GET | `/api/ai/jobs/{id}/stream` | SSE：`stage`/`delta`/`done`/`error` 事件 |

### 6.1 整合模块任务类型

| 任务 | 模块 | 参数（`params`） | 取消/续跑/幂等 |
|---|---|---|---|
| `course` | classroom | `{course_id, stage, scene_types, scene_count, depth, instruction?, scene_ids?, material_text?, images?, images_per_scene?, image_provider_id?, image_model?}` | 取消保留已 ready 场景；checkpoint `done_scene_ids/failed_scene_ids`；按 `(course_id, order_index)` 幂等 upsert；自动名册仅缺失时生成 |
| `paper-import` | paperlab | `{import_id, page_from, page_to}` | 逐页草稿；checkpoint `done_pages/failed_pages`；取消保留已完成页；同页 AI 旧稿替换、人工稿保留 |
| `paper-enrich` | paperlab | `{question_ids[]}`（1-50） | 逐题隔离；checkpoint `done_ids/failed_ids/skipped/failed`；已核验题跳过 |
| `paper-compose` | paperlab | `{spec:{mode,subject,grade,count,type_counts,difficulty_target,tolerance,kp_weights,filters}}` | checkpoint `fingerprint/proposal/paper_id`；同任务续跑复用 proposal/已建卷（幂等） |
| `paper-revision` | paperlab | `{spec:{mode:'consolidate',...}}` | 同 `paper-compose`（proposal/paper_id 复用） |
| `paper-annotate` | paperlab | `{import_id, pages[]}`（>20 页才异步） | checkpoint `done_pages`；每页重切幂等 |
| `insight-report` | insight | `{days, subject?}` | 单次模型调用；取消检查存在；**无 checkpoint**（续跑会重新调用模型，结果原子写入） |
| `assistant-turn` | assistant | `{conversation_id, message_id, skill_ids}` | checkpoint `steps/observations`；取消落定 `cancelled` 并回 idle；续跑不重复工具步骤 |

> 任务结果统一由框架追加 `prompt_version/models/input_ref`；`course` 结果另含
> `roster_generated/roster_agent_count/roster_warnings`，`paper-*` 结果含业务摘要与失败清单。

## 7. 服务端上限汇总（选择项）

**AI 课堂**：场景 3-12（默认 6）；单场景元素 ≤24；动作 ≤40；交互 HTML ≤2MB；TTS 文本 ≤2000 字；
名册角色 ≤6（老师 1 + 同学 ≤5）；角色名 ≤24 / persona ≤400 字；讨论 1-6 段、插话 ≤2 段、轮次 1-3、
单段台词 ≤500 字；白板 ops ≤40（text ≤200 / formula ≤300 / path ≤600 字符）；每课 interactive 场景 ≤2；
配图 ≤2 张/场景；会话 progress ≤16KB；导入 ZIP ≤200MB（条目 ≤2000、解压 ≤500MB、场景 ≤100、素材 ≤100MB/个）。
本轮补充：课程参考图 ≤8MB（`POST /courses/{id}/images`，multipart 有界读取超限 **413**，服务端压到最长边
1600 / JPEG 85）；服务端 ASR 音频 ≤25MB（**413**）；课堂素材只有 png/jpeg/webp/gif、常见音频与 mp4 可内联，
其余一律隔离下载。

**试卷工坊**：单文件 ≤30MB、≤20 文件、≤20 页、≤400 草稿；补全题数 1-50；题库单页 ≤100；
组卷题数 1-60、候选池 ≤150、知识点配额 ≤20 项、排除 ref ≤100 个；试卷 ≤60 题、列表单页 ≤50；
巩固卷 1-20 题、候选 ≤120；答题卡 ≤20 张/卷、分栏 1-4；切题框 ≤200/页、PATCH CAS；
题库导入导出 ≤5000 题 / 导入 ≤10MB、≤2000 题、错误 ≤200 条；整卷作答单题 ≤16KB、题位 ≤200。
本轮补充：工坊题库导入/页图上传改为「读上限+1」有界读取，实测超限 **413**（若 multipart 声明的大小已超限
先返回 422）；页图/PDF 解码前像素预算 **40MP**（超限 422）；答题卡扫描单次 ≤10 页，单页超 30MB 或 40MP
按 **422** 返回。

**拍题入库（`/api/uploads`，见 §14）**：单次 ≤5 张图、单文件 ≤12MB（413）、单图 ≤25MP（422）、总大小
≤40MB（422）、服务端 EXIF 转正并压到最长边 2048；OCR 草稿文本 ≤8,000 字；人工确认/保存题干 ≤20,000 字、
选项 ≤12、答案 ≤8、补充条件 ≤20、知识点 ≤30；`expected_version` 对应 `ai_uploads.intake_revision`
（仅人工确认时递增）。

**广场 / 投稿 / 评审 / 发布（`/api/plaza`，见 §16）**：公开列表 `page_size` ≤50、`keyword` ≤120、
`subject` ≤40、来源筛选仅 `community|featured`；投稿不可变快照正文（题干/解析）各 ≤20,000 字、选项 ≤12、
答案 ≤48、子题 ≤20、配图 ≤20、知识点标签 ≤30、难度 0–1；来源声明各字段 ≤500 字、`source.note` ≤1000
（仅作者/编辑可见）；评审 `reasons` ≤4000、`independent_answer` ≤8000；签发 `reason` ≤2000，
撤回 `reason` 2–2000、`mode=normal|emergency`；编辑台投稿队列/发布历史 `page_size` ≤100（状态可逗号多选，
未知状态 422）；身份申请 `kind` 2–40、`subject` ≤40、`evidence` 2–2000；公开衍生图单个复制 ≤20MB
（原件哈希不一致或超出即降级不可公开，不阻断审核）。

**学情分析**：`days` 1-3650（默认 90）；`subject` ≤40 字；无学习记录不生成 AI 建议（422）。

**AI 助手**：会话 ≤100/人；单会话用户消息 ≤200、消息总量 ≤1200；单条消息 ≤4000 字；
技能 ≤100/人、单个正文 ≤20000 字、单轮注入 ≤5 个、技能提示 ≤8000 字；Agent 循环 ≤6 步；
工具观察 ≤6000 字/条；最终回答 ≤14000 字；引用 ≤16 个。

**Phase 3 增量（2026-11-21）**：

- 平台精选：`/api/questions` 单页 ≤50、`q` ≤200；official 列表仅支持学科/题型/关键词（题库专属筛选不适用），
  关键词 ≤120、`type` 未知 **422**；工坊建卷题项引用 ≤340 字符、单卷 ≤60 题（沿用 §3）；official 快照配图
  最多随快照 20 张（广场侧快照白名单上限）。
- 质量运营：公开列表 `page_size` ≤50；收藏/通知/反馈/申诉/抽检/指标均继承 §16 的公开字段上限——
  通知 `page_size` ≤100、已读 `ids` ≤200；纠错 `field` ≤20、`description` 2–4000；举报 `reason` ≤20、
  `detail` ≤4000；申诉 `reason` 2–2000；编辑端反馈队列 `page_size` ≤100、状态仅
  `pending|resolved|rejected|all`；抽检 `findings` ≤20 条（每条 ≤500 字）、`note` ≤2000、严重度
  `low|medium|high`；抽检队列 `page_size` ≤100、状态仅 `all|pending|checked|passed|issues`；
  回滚 `reason` 2–2000；发布历史 `page_size` ≤100。
- AI 用量与配额：`GET /api/ai/usage/me` 的 `days` 1–365（默认 14），返回最近 10 条；管理台用量 `days`
  1–365（默认 30）、`page` ≤1000、单页 20、单次查询最多 10000 行（超出 `truncated:true`）；
  配额调整 `daily_limit` 0–100000、`concurrency_limit` 0–1000（0=不限制）；账本保留默认 180 天
  （`STUDY_AI_USAGE_RETENTION_DAYS`，≤0 永久保留）。

## 8. 版本与可追溯字段

| 模块 | 版本字段 | 当前值 |
|---|---|---|
| AI 课堂生成 | `prompts.PROMPT_VERSION` / 任务结果 `prompt_version` | `classroom-2026-11-21-v2` |
| 多智能体 | `multiagent.PROMPT_VERSION` / 名册 `agents_meta.prompt_version` / `/meta.multiagent_prompt_version` | `classroom-multiagent-2026-11-21-v1` |
| 试卷工坊 | `paperlab.prompts.PROMPT_VERSION` / `/meta.prompt_version` | `paperlab-2026-11-20-v1` |
| AI 助手 | `assistant.PROMPT_VERSION` / 任务结果 | `assistant-2026-11-21-v1` |
| 学情分析 | `app.ai.PROMPT_VERSION` / 任务结果 | 见 `app/ai`（框架统一） |
| 广场投稿版本 | `plaza_submissions.version/current_version` + `fingerprint`（归一化内容 SHA-256） | 内容/来源声明/授权任一实质变化 → `version/current_version` 同步 +1，`plaza_submission_versions` 留档；旧版本评审票保留但不再计入阈值 |
| 个人收录版本 | `pl_question_versions.version` + `content_sha256` + `source_*` | 创建/编辑/收录/题库更新各追加一版（`origin=create|edit|collect|source_update|snapshot`），无修改入口；列表/详情 `has_new_version` 由题库实时 revision 现算 |
| 平台精选（Phase 3） | `official:<public_id>` + 快照/作答的 `published_version` | `public_id` = 广场 `plz-`+20 hex（与内部 id 解耦）；`published_version` 取活跃 release 的 `version`，随详情/工坊快照/练习冻结快照一起记录；撤回后详情立即 404、历史作答保留 `official:` 写库键 |
| AI 任务用量（Phase 3） | `ai_job_usage.job_id`（唯一）+ `kind/model/status/duration_ms/retries/tokens_*/cost_micro/meta_json` | 每任务终态一行、幂等更新；`prompt_version`/`input_kind`/`models`/`cross_verify` 等只在 `meta_json` 存分类与计数，不含题干/资料正文；配额覆盖见 `ai_quotas.updated_at` |

## 9. 文档索引

| 文档 | 内容 |
|---|---|
| `app/classroom/API-SAMPLES.md` | 课堂 P0 真实响应样例 + P1/P2 增量（多智能体/交互模式/配图/ZIP manifest） |
| `app/classroom/EXPORT-INTERACTIVE.md` | 交互 HTML 沙箱/CSP、交互事件、导出导入格式、ZIP manifest、静态安全校验 |
| `app/classroom/MULTIAGENT-API.md` | 角色名册、讨论/插话、动作白名单、白板 0-1 坐标、与生成/导出交叉说明 |
| `app/classroom/GENERATION-PLUS.md` | 深度交互模式、课程任务自动名册、可选配图（能力探测/失败降级/导出导入） |
| `app/paperlab/API.md` | 导入识别、题库、AI 补全、任务注册、P1 能力索引 |
| `app/paperlab/PAPERS-API.md` | 试卷/组卷/错题巩固/导出/整卷练习 |
| `app/paperlab/CARDS-API.md` | 答题卡模板/SVG/打印 |
| `app/paperlab/ANNOTATE-API.md` | 切题与答题区标注/重切/发布前校验 |
| `app/paperlab/BANK-IO.md` | 题库统计/JSON·CSV 导入导出 |
| `app/assistant.md` | AI 助手架构、API/工具、HITL 写操作、技能库 |
| `app/plaza/**` 模块 docstring | 广场 Phase 2 数据层/服务层/公开/投稿/评审/签发/角色/编辑台的设计边界与状态机（`app/plaza/__init__.py` 为模块索引） |
| `app/home_stats.py` docstring | 首页统计口径（题库门禁、首次正确率、重复练习、待判分、薄弱知识点） |
| `app/official.py` docstring | 平台精选读取适配层：`official:<public_id>` 可见性（查询时判定）、公开投影、检索与不可见摘要（Phase 3） |
| `app/plaza/{notify,favorites,feedback,quality,metrics}.py` docstring | 质量运营边界：通知去重与脱敏、收藏可收藏态、反馈/申诉状态机、抽检 100% 与 high 自动撤回、指标口径（Phase 3） |
| `app/ai/usage.py` docstring | AI 用量账本（`ai_job_usage`）、配额/并发优先级与 429 语义、入参分类与保留策略（Phase 3） |
| `app/monitor.py` docstring | 运维监控聚合：五组只读检查、单检查降级、进程内短缓存、SQLite/PG 兼容与阈值环境变量（Phase 4） |
| `app/plaza/gate.py` docstring | 抽检门禁：`off/initial_100/always` 模式、DB > env > 默认解析、armed 试点激活、试点窗口计数、override 审计与 409 guidance（Phase 4） |
| `docs/知序优化实施记录-20261121-第三轮.md` | Phase 3（平台精选接入 / 质量运营 / AI 可追踪）实施记录与验收证据 |
| `docs/知序优化实施记录-20261121-第四轮.md` | Phase 4 收尾（精选接入助手/课堂、运维监控与构建分发、质量运营闭环、前端收口）实施记录与验收证据 |
| `.omx/plans/integration/API-AUDIT.md` | 本次只读契约/安全审计报告（发现、证据、建议、Lead 决策项） |

## 10. 变更记录

| 日期 | 变更 |
|---|---|
| 2026-11-21 | 新建本索引（task-24）：对齐 93 条整合模块路由；同步 prompt v2/多智能体/交互模式/配图/名册导出导入；修正 paperlab 文档 P1 能力与路由名；审计报告见 `API-AUDIT.md`。 |
| 2026-11-21（第二轮） | task-10 增量：新增 §14 拍题入库 OCR-first（`/api/uploads/{id}/intake|ocr-draft|ai-answer|save-to-bank`，`PATCH .../confirm` 增加 `intake_revision` 乐观锁）；§3.5 整卷练习冻结（`pl_attempt_items`、`frozen/paper_deleted/notice`、作答侧配图路由）、§3.3 删卷保留练习（`attempts_kept`）、§3.2 题图资产所有权（`pl_assets`、越权 404、迁移脚本）、§2.1 课堂素材回收/类型白名单、§7 上传与像素上限、§0 计数口径说明。核实方式：`grep @router` 对照 `app/ai/upload_pipeline.py`、`app/paperlab/{practice,assets,bank,papers}.py`、`app/classroom/{images,api,importer}.py` 实现与错误码。 |
| 2026-11-21（Phase 1 剩余 + Phase 2） | task-19 增量：新增 §15 统一题目引用（`bank:`/`app:`/`official:`）、`submission_ledger` 幂等入账、`GET /api/review/queue`、个人收录版本 4 条（`collect/versions/source-status/update-from-source`）、`GET /api/insight/home` 口径；新增 §16 广场/投稿/评审/发布 25 条（公开只读 4、作者端 7、评审端 4、签发/撤回/历史/事件 4、角色身份 3、编辑台 3），含公开可见性（`community_review` 待核验 / `verified` / `published=featured`）、公开衍生图授权、阈值/回避/一人一票、`optional_user` 访客口径；§0/§4/§7/§8/§9 同步。核实方式：临时数据目录 `from app.main import app` 生成路由清单并逐条 `grep @router`，错误码/字段对照 `app/plaza/{public,submissions,review,release,roles,admin,db,service,figures}.py`、`app/question_ref.py`、`app/practice.py`、`app/paperlab/versions.py`、`app/home_stats.py`。 |
| 2026-11-21（Phase 3 · 第三轮） | WS25 增量：新增 §17 平台精选接入（`official:<public_id>` 读取适配层、`/api/questions?source=bank\|official\|all`、平台精选题作答/错题/复习/统计、工坊建卷快照与冻结练习、前端来源筛选/徽标/收录入口）、§18 质量运营（`/api/plaza` 44 条：通知 2 / 收藏 3 / 纠错举报申诉 10 / 抽检 2 / 指标 1 / 回滚 1；6 张新表、通知与事件钩子、100% 抽检与 high 自动撤回、回滚状态机、指标口径）、§19 AI 用量与配额（`ai_job_usage`/`ai_quotas`、任务生命周期入账、配额/并发 429、4 条查询/管理接口、前端任务中心/管理台/设置展示）；§0/§1/§7/§8/§9 同步。核实方式：临时数据目录只读生成路由清单（plaza 44、AI 用量 4、`/api` 共 316）+ `grep @router`/字段对照源码；本轮五个专项测试文件 `tests/test_official_refs.py`、`test_official_ui.py`、`test_ai_usage.py`、`test_plaza_quality_ops.py`、`test_plaza_ops_ui.py` 合计 **107 passed**（88.42s）；本轮全量 pytest **1557 passed / 3 skipped**（779.07s，无失败）；locale 中英键 4,534/4,534 一致；Phase 3 端到端脚本（`scripts/integration/{official_accept,ai_usage_accept}.py` + `plaza_accept.py` 扩展）由 WS24 交付，一键 UI 验收由 Lead 收敛。 |
| 2026-11-21（Phase 4 · 第四轮） | WS31 增量：新增 §20 平台精选接入助手/课堂（只读工具 `search_official`、`read_question` 支持 `official:`、system prompt 引用规则；课堂 `source_ref=official:<公开编号>`、`outline.sources`/`config.seed_source`、`official_source_count/official_sources`）、§21 运维监控（`app/monitor.py` + `GET /api/admin/monitor` 五组只读检查、单检查降级、15s 短缓存、`X-Health-Token` 探针、管理台监控区、`.dockerignore`/Dockerfile/构建指纹）、§22 质量运营闭环（`app/plaza/gate.py` 门禁三模式/armed/试点窗口/409 guidance/override 审计、签发前抽检 `pre_publish`、`POST /releases/{public_id}/restore` 一键恢复、三类批量处理部分成功、`GET /notifications/unread-count` 与 `read` 的 `kind` 全读、通知窗口去重、指标新增 `quality.gate/restorations/ops.batch/notifications`）、§23 前端收口（official 配图/纠错、学段筛选、精选学情口径、任务深链、笔记/Anki 引用与版本、管理台监控区）；§0/§2/§4/§5/§9/§10/§15/§17/§18 同步（`/api/plaza` 44 → **51** 条、`/api/admin/monitor` 1 条、`/api` **324** 条目、助手工具 12 → **13**）。核实方式：临时数据目录只读生成路由清单（`/api/plaza` 51、monitor 1、`/api` 324 条目 / 277 路径）+ `grep "@router"`/字段对照源码；五个专项测试文件 `tests/test_official_integrations.py`、`test_monitor.py`、`test_build_fingerprint.py`、`test_plaza_quality_gate.py`、`test_phase4_ui.py` 合计 **105 passed**（81.34s）；locale 中英键 4,632/4,632 一致；全量/一键验收由 Lead 收敛后回填（见第四轮实施记录 §6.4）。 |

## 11. 本地 OCR / 联网搜索 / 服务端渲染（P0–P3 增量）

### 11.1 本地 OCR（`app/ai/ocr.py`，P1）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/ai/ocr/info` | 引擎状态：`{engine, mode, available, loaded, version, models, limits:{max_side,timeout_seconds,max_pages,concurrency,max_file_bytes}, notice}` |
| POST | `/api/ai/ocr` | multipart `file`（图片或 PDF）+ 可选 `pages`；图片 → `{kind:'image', text, lines:[{text, box(4 点像素), score}], width, height, elapsed_ms}`；PDF → `{kind:'pdf', page_count, pages:[...], text}`。≤5 页同步，>5 页 422；≤12MB（413）；引擎不可用 503；超时 504 |

环境变量：`STUDY_LOCAL_OCR=auto|on|off`、`STUDY_OCR_MAX_SIDE`(600–6000，默认 2400)、`STUDY_OCR_TIMEOUT`(1–600s)、`STUDY_OCR_MAX_PAGES`(1–30)。实现：RapidOCR 1.4.4 + onnxruntime，延迟单例、`asyncio.to_thread` + 并发 2。
集成：扫描资料 OCR（`notes_agent._ocr_job`）先本地 OCR→作视觉提示；视觉失败保留本地文本并标 `source='local_ocr'`；拍题识别（`upload_pipeline._recognize_job`）注入 OCR 校对提示并返回 `ocr_text`。**不伪造识别成功、不生成答案。**

### 11.2 联网搜索（`app/ai/search.py`，P3；DeepSeek 官方 `/v1/tools/search`）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/ai/search/info` | `{available, provider?, endpoint?}`；无连接时 `available:false` + 中文引导 |
| GET | `/api/ai/web-search?q=&limit=1-8` | 返回 `{query, provider, results:[{title,url,site,date,summary,authority}]}`；无连接 422、上游失败 502 |

调用方：AI 助手只读工具 `web_search`；课堂生成 `web_search` 参数（待 P3 合入）；笔记 Agent（规划中）。要求：引用必须带 URL，不得把搜索结果改写成无出处结论。

### 11.3 服务端渲染（`app/ai/render.py`，P5 基础设施）

`render_available()`、`html_to_pdf(html, paper='A4', landscape=False)`、`screenshot_png(html, width, height)`；Playwright（Edge→Chrome→Chromium），输入仅限服务端自生成 HTML，≤8MB，失败抛 `RenderUnavailable`（调用方给中文引导并保留浏览器打印兜底）。环境变量：`STUDY_RENDER_ENGINE=auto|off`、`STUDY_RENDER_TIMEOUT_MS`。

### 11.4 本轮变更记录

| 日期 | 变更 |
|---|---|
| 2026-11-21 | P0：修复助手能力说明与旧断言冲突；requirements/Dockerfile/第三方许可补 RapidOCR+onnxruntime+OpenCV+FFmpeg。P1：本地 OCR 模块与拍题/扫描集成（28 测试）。P3：DeepSeek 搜索适配层 + `/api/ai/web-search` + 助手 `web_search` 工具（7 测试）。P5：`app/ai/render.py` PDF/截图基础设施（5 测试）。 |

## 12. 服务端 PDF 导出（P5，2026-11-21）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/paperlab/papers/{id}/export?format=pdf&answers=0\|1&columns=1-4` | 服务端浏览器渲染 PDF（与浏览器打印一致）；`X-Page-Count`/`X-Paper-Columns`/`X-Paper-Size`；引擎不可用 503 中文提示改用 html 打印 |
| GET | `/api/paperlab/cards/{id}/export?format=pdf&page=all\|N` | 答题卡 PDF；`X-Card-Page-Count`/`X-Card-Stale` |

实现：`app/ai/render.py`（Playwright：Edge→Chrome→Chromium，`html_to_pdf`/`screenshot_png`，仅渲染服务端自生成 HTML，≤8MB，失败抛 `RenderUnavailable`）。环境变量 `STUDY_RENDER_ENGINE=auto|off`、`STUDY_RENDER_TIMEOUT_MS`。部署镜像已装 ffmpeg（视频导出用），Playwright 浏览器需在镜像内 `playwright install chromium` 或使用系统 Chromium（未安装时 PDF/视频自动降级）。

## 13. 2026-11-21 增量能力（P1–P9）

| 能力 | 路由 / 任务 | 文档 |
|---|---|---|
| 本地 OCR | `GET /api/ai/ocr/info`、`POST /api/ai/ocr`；试卷侧 `POST /imports/{id}/ocr`、`POST /imports/{id}/ocr-draft` | `app/ai/ocr.py` docstring、`.omx/plans/integration/M1-实施记录.md` |
| 双模型识别核验 | `POST /imports/{id}/recognize`（`cross_verify`/核验模型）；items 含 `verify_status/diff` | `app/paperlab/API.md`、`tests/test_paperlab_verify.py` |
| 联网搜索 | `GET /api/ai/search/info`、`GET /api/ai/web-search`；课堂 `generate.web_search`；助手工具 `web_search` | `app/ai/search.py`、`app/classroom/GENERATION-PLUS.md` |
| 图片编辑 / 音色 / 旁白 / ASR 兜底 | `POST /courses/{id}/images`、`GET /voices`、`POST|GET /courses/{id}/narration`、`GET /asr/info`、`POST /asr`；任务 `course-narration` | `app/classroom/API-SAMPLES.md`、`tests/test_classroom_media.py` |
| 幻灯片编辑器 | `GET|PATCH /courses/{id}/scenes/{sid}/content`、`POST .../duplicate` | `app/classroom/editor.py` docstring、`tests/test_classroom_editor.py` |
| 3D 交互库内联 | 交互场景 HTML 自动内联 three.js（sha256 白名单），沙箱 CSP 不放宽 | `web/vendor/interactive/README.md`、`app/classroom/EXPORT-INTERACTIVE.md` |
| 课堂 PDF / MP4 | `GET /courses/{id}/export?format=pdf|mp4`、`POST/GET /courses/{id}/video`；任务 `course-video` | `app/classroom/EXPORT-INTERACTIVE.md` §10、`.omx/plans/integration/P5-实施记录.md` |
| 试卷/答题卡 PDF | `GET /papers/{id}/export?format=pdf`、`GET /cards/{id}/export?format=pdf` | 本文 §12 |
| 答题卡扫描 OMR | `GET /scan/meta`、`POST /papers/{id}/scan`、`GET /scans/{id}`、`POST /scans/{id}/confirm`、`GET /papers/{id}/scans` | `app/paperlab/scan.py` docstring、`tests/test_paperlab_scan.py` |
| 暗色主题 | 前端 `html[data-theme=dark]` + 顶栏三态切换（localStorage `zhixu.theme`） | `web/js/core/theme.js`、`screenshots/integration/dark-theme-report.json` |
| 中英双语 | `web/locales/{zh-CN,en-US}.json`（2215 键）、`web/js/core/i18n.js`、顶栏语言切换 | `.omx/tmp/i18n-progress.md`、`screenshots/integration/i18n*-report.json` |

验收脚本（2026-11-21 第二轮重写）：`scripts/ci_accept.ps1` 仅作薄外壳，阶段编排/汇总/退出码在
`scripts/accept_runner.py`：任一阶段 FAIL 或 UI 不可达 → exit 1；存在 SKIP（`-SkipPytest`/`-Stages`/
工具缺失）或 0 阶段执行 → exit 3 `ACCEPT INCOMPLETE`；全跑全过才 exit 0。默认基线跟随 `releases/CURRENT`
（r5），报告 JSON 落盘 `build_id`/`bank_version`/每阶段状态（默认 `screenshots/accept-report.json`）。

### P6b 自由画布编辑器（2026-11-21 追加）

- 构建工程：`editor/`（React 19 + Vite 6），vendored OpenMAIC `dsl/geometry/renderer/editor` 四包源码（MIT，来源 commit
  `18b68aea…`，各包 LICENSE 与 `VENDORED.json` 保留）；`pnpm run build` 产物入库 `web/editor/`（69 文件，3.30 MiB /
  gzip 1.56 MiB，运行时零外链）。
- 集成：课堂页头「自由画布编辑」按钮（仅 slide 场景）→ 同源 iframe（因全局 CSP `frame-ancestors 'none'` / XFO DENY，
  采用 `srcdoc` + 资源绝对化挂载，仍为同源 cookie/fetch）；postMessage 协议
  `zhixu-slide-ready / zhixu-slide-dirty / zhixu-slide-saved / zhixu-slide-close`；保存走
  `PATCH /api/classroom/courses/{id}/scenes/{sid}/content`（version CAS），父页刷新播放器到该场景。
- 能力：拖拽/8 向缩放/旋转、框选与多选、吸附参考线、富文本工具条、插入文本/图片/表格/图表/线条/公式/形状、
  右键菜单（层级/组合/对齐/锁定）、图层面板、属性面板、撤销重做（120 步）、缩放、409 冲突处理、localStorage 草稿。
- 边界（`editor/README.md`）：后端 `schema.normalize_slide_content` 白名单归一化会收回组合/锁定/阴影/圆角等扩展属性
  （仅会话内生效）；图片仅站内相对地址；文本内联公式保存时被净化（独立 latex 元素不受影响）；无多页画布/平移放大。

### P5 增强：课堂视频逐帧动画与转场（2026-11-21 追加）

- `POST /courses/{id}/video` 增 `mode:'static'|'animated'`（默认 static，向后兼容）、`transition:'none'|'fade'`
  （默认 fade）、`transition_ms:100–1000`（默认 400）；非法 422 中文。
- 离线播放器 capture API 扩展 `seekMs(t)` / `sceneDurationMs(i)`：聚光灯/激光/高亮/白板绘制/字幕按时间轴确定性推进；
  `video.py` 逐帧截图（单场景 ≤300 帧、总帧 ≤6000）→ ffmpeg `xfade`/`acrossfade` 转场合成 H.264/AAC；
  取消清理临时帧、按场景 checkpoint 复用；缺失 seek API 时自动降级 static 并在结果标注。
- 实测（ffprobe）：2 场景 `fps=8, animated, fade 400ms` → 3.75s / 33 帧 / h264+aac / 22KB，生成 3.6s（热机）。

### i18n 第三阶段（2026-11-21 追加）

- 语言包 2,215 → **3,520 键**（两语言一致、无空值、占位符一致）；新增 `paper.*`/`classroom.*`/`assistant.*`/
  `mybank.*`/`paperlab.*` 分组；试卷工坊、我的题库、AI 助手页面与导入/组卷/答题卡/扫描/切题/练习弹窗全部英文可用，
  `validate_i18n.py` 与 Playwright 验收（CJK=0、无缺键）通过。
- 剩余未接入：`classroomPlayer.js`/`classroomEditor.js`/`classroomMultiagent.js`/`pages/classroom.js` 与 `voiceInput.js`
  的中文文案（课堂整页英文为后续阶段）；后端数据本地化（题目/学科/知识点等）仍按数据不翻译处理。

## 14. 拍题入库 OCR-first（2026-11-21 第二轮增量，`/api/uploads`）

> 定位：`ai_uploads` 的「拍题 → 本地 OCR 草稿 → 人工确认 → 保存到我的题库」链路，是工坊
> `pl_questions` 的单题入口（实现 `app/ai/upload_pipeline.py`，`router` 前缀 `/api`）。它属既有 AI
> 模块而非 §0 的六个整合模块面，因本轮新增「上传 → 工坊保存」跨模块契约在此单列。
> 既有 `POST /uploads/question`、`GET /uploads/{id}`、`GET /uploads/{id}/extra/{index}` 保持兼容。

### 14.1 路由与状态

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/uploads/question` | （既有）multipart `file` 或 `files[]` → `{id, filename, media_type, preview_url, extra_previews, extracted_text, status:'uploaded'}`；≤5 张、单文件 ≤12MB（413）、单图 ≤25MP（422）、总 ≤40MB（422）、服务端 EXIF 转正并压到最长边 2048；文本 `.txt/.md` ≤4 万字 |
| GET | `/uploads/{id}/intake` | 入库状态（刷新/任务深链恢复）：`{upload_id, filename, media_type, preview_url, status, revision, confirmed, draft, pending_draft, ai_suggestion, confirmed_at, saved_question_id, saved_at, extracted_text, intake:true}` |
| POST | `/uploads/{id}/ocr-draft` | 本地 OCR 生成/刷新识别草稿（无需视觉模型 Key）→ `{upload_id, revision, ocr_available, ocr_notice, draft, status, intake, notice}`；OCR 不可用/超时 **200** + `ocr_available:false`（保留原图可重试/手录）；图片损坏或无图 **422**；已人工确认时只更新 `draft_json`；不递增 `intake_revision` |
| POST | `/uploads/{id}/ai-answer` | `{provider_id, model, expected_version?}` → `{upload_id, revision, ai_suggestion:{answers,explanation,rubric,knowledge_tags,uncertain,model,provider_name,created_at,source:'ai'}, notice, intake}`；AI 建议答案单独存 `ai_suggestion_json`；题干为空 **422**；模型失败/非结构化 **502**；与 `intake_revision` 不一致 **409** |
| POST | `/uploads/{id}/save-to-bank` | 见 §14.2；写 `pl_questions` 并登记原图为 `pl_assets` 资产；未确认 **409**、题干空 **422**、版本冲突 **409**、写入失败 **502**；已保存幂等返回 `already_saved:true` + `saved_question_id` |
| PATCH | `/uploads/{id}/confirm` | 人工确认：`{expected_version?, subject, type, stem(必填), options, conditions, answers?, explanation, knowledge_tags?}` → `{id, status:'confirmed', confirmed, similar_questions(≤5), revision, intake}`；`expected_version`（=`intake_revision`）不一致 **409**（条件 UPDATE 二次校验）；成功递增 `intake_revision`、清空 `pending_draft_json`；不带 `expected_version` 时保持旧前端直接确认的兼容行为 |
| POST | `/uploads/{id}/similar` | 相似题（≤5）：`{items, has_exact_match}`（题干取人工确认内容，其次视觉识别结果，最后原文文本） |

`ai_uploads` 本轮新增列（启动/首次访问幂等 `db.ensure_column` 补齐，失败 **503**）：`intake_revision`（默认 1）、
`draft_json`、`pending_draft_json`、`ai_suggestion_json`、`confirmed_at`、`saved_question_id`、`saved_at`。
鉴权：以上全部 `Depends(current_user)`；他人上传统一 **404**（`未找到该上传题目`）。

### 14.2 草稿 / 确认 / 回写 / 保存版本规则

1. **草稿与人工确认分离**：`draft_json` = 当前识别草稿（本地 OCR 或视觉「增强识别」，可被新草稿替换）；
   `confirmed_json` = 人工确认内容（仅 `PATCH /confirm` 写入）；`ai_suggestion_json` = AI 建议答案，
   仅供参考、可逐字段采用，**绝不并入确认内容或题目内容**。
2. **版本（乐观锁）**：`intake_revision` 只在人工确认时 +1。`confirm`/`ai-answer`/`save-to-bank` 均接受
   `expected_version`，与当前 `intake_revision` 不一致 → **409**（中文提示「题目已更新/已被其他操作更新」）；
   `confirm` 另有 `WHERE intake_revision=?` 的条件更新兜底并发。
3. **异步识别回写（既有 `recognize` 任务）**：视觉识别仍走 `POST /api/ai/jobs`（`task:'recognize'`，
   `upload_id` 归属校验，可选 `cross_verify`/核验模型）。回写用「版本未变且 `confirmed_json` 为空」的
   原子 UPDATE：命中 → 结果写入 `recognized_json`/`draft_json`（任务结果 `draft_applied=true`）；未命中
   （识别期间已人工确认）→ 只写 `pending_draft_json`（模型草稿对照，需再次确认），返回
   `draft_applied:false`/`draft_conflict:true` 与中文 `notice`，**绝不覆盖人工确认内容**。
4. **保存到我的题库**：必须存在非空 `confirmed_json`（否则 409）；`expected_version` 冲突 409；
   `saved_question_id` 已存在时幂等返回同一题（`already_saved:true`，题已被删除则重新创建）。按工坊契约
   写入 `pl_questions`：题型经 `schema.normalize_qtype` 归一到 `SINGLE/MULTIPLE/BLANK/SUBJECTIVE`，
   选择题答案归一到选项标号（无法解析的原文写入解析提示，不因 OCR 噪声保存失败）；有答案 `status='ready'`、
   否则 `'draft'`。`include_original_image`（默认 true）时把原图 `save_asset_bytes(purpose='question_image')`
   登记为 `pl_assets` 资产并写入 `content.images[{asset:<asset_id>, role:'original', source:'upload'}]`。
5. **前端深链/任务中心**：`#/upload?upload=<id>` 与任务中心的 recognize 跳转均以 `GET .../intake` 恢复；
   草稿/人工确认/待确认模型草稿/AI 建议答案分开展示，旧文件选择、文本录入、裁剪旋转、AI 讲解全部保留。

### 14.3 错误码速查

`401` 未登录；`404` 上传不存在/非本人；`409` 版本冲突、未人工确认即保存；`422` 图片损坏/无图、题干为空、
参数越界；`502` 模型调用失败、AI 结果非结构化、原图入库或保存失败；`503` `ai_uploads` 入库字段未就绪。

## 15. 统一题目引用 / 幂等入账 / 个人收录版本 / 首页统计（2026-11-21 Phase 1 剩余增量）

> 本节按本轮跨模块契约需要单列**既有平台面**（练习/首页）与工坊题库增量，不扩展文首「六个整合模块面」口径；
> 实现分别位于 `app/question_ref.py`、`app/practice.py`、`app/paperlab/versions.py`、`app/home_stats.py`。

### 15.1 统一题目引用（`app/question_ref.py`）

引用语法（`parse`/`normalize`/`describe`，纯字符串工具、不访问数据库）：

| 引用 | 含义 | 读取行为 | 写库键（`attempts/wrong_questions/mastery.qid`） |
|---|---|---|---|
| `bank:<qid>` | 只读高考题库题目（r5 基线） | 走只读题库 | 裸 `<qid>`（向后兼容旧数据/旧接口） |
| `<qid>`（裸 ID，含 `docA:1` 这类题库 qid） | 同 `bank:`（旧客户端兼容） | 同上 | 裸 `<qid>` |
| `app:<pl_questions.id>` | 我的题库个人题 | 走工坊归属校验（本人 + 未删除） | `app:<id>` |
| `official:<id>` | 平台精选来源（**预留**） | 读取层未开放：**404**「该来源题目暂未开放」 | `official:<id>` |

规则与错误码：

- 引用长度 ≤300；`bank`/`official` 的 key 不能为空、不能含空白与 `\x00\r\n`；`app` 的 key 必须是
  `^[1-9][0-9]{0,17}$`（正整数 id）；格式非法统一 **422**（中文 `QuestionRefError` 文案，不 500）。
- 以下接口的 `qid`/`ref` 参数接受上述统一引用；他人或已删除的 `app:` 题统一 **404**「题目不存在」
  （不泄露存在性），`bank:` 题保持既有只读与隔离门禁（隔离 **409**、题组共用材料不可作答 **409**）：
  `GET /api/questions/{qid:path}`、`GET /api/reveal?qid=`、`GET /api/attempts/history?qid=`、
  `POST /api/attempts`（Form `qid`）、`POST /api/mastery`、`POST /api/wrongbook`、
  `GET /api/bank-image?qid=app:<id>&file|asset_id`（个人题配图分支，统一 404）。
- 响应投影新增 `ref`/`source`/`source_label`，旧字段 `qid` 仍是写库键：
  题库题 `ref=bank:<qid>`、`source=bank`；个人题 `ref=app:<id>`、`source=app`、
  `source_label=我的题库`（题库题为 `高考题库`）。错题卡、复习队列、统计均按同一口径带
  `ref/source/source_label`；`GET /api/wrongbook` 的 `app:` 卡在个人题删除后降级为 `_missing_app_card`
  （仍可读历史成绩，不 500）。

### 15.2 幂等入账与统一复习队列（`app/practice.py`）

`POST /api/attempts` 新增可选 Form `submission_id`（客户端弱网重试/并发重复提交复用同一值）：

| 项 | 规则 |
|---|---|
| 校验 | 非空、≤160、仅 `A-Za-z0-9._:+-`；非法 **422** |
| 幂等键 | `ledger_key = <写库题目键>|<submission_id>`；`submission_ledger(user_id,kind='attempt',ledger_key)` 主键即唯一约束 |
| 重复/并发 | 直接回放已入账作答（`duplicate:true` + 既有 `attempt`），不再插入 `attempts` 行；并发落败方丢弃本次新存图片 |
| 兼容 | 不带 `submission_id` 保持旧的非幂等语义（每次都是一次新作答） |
| 整卷侧 | 整卷逐题入账用 `paper:<attempt_id>:<item_position>`（`kind='paper_item'`），与单题 `kind='attempt'` 互不干扰；`bank:`/`app:` 错题都写入统一 `wrong_questions`，未关联成功的 `app:` 题在 `app_bank_unlinked`/`app_wrong_added` 明示 |

`GET /api/review/queue`（新增，`?subject=&limit=1-200，默认 50`）：题库题与个人题（`app:`）同一结构的
统一复习队列。`items` 为已判分的到期/历史错题（`graded:true`），`pending_items` 为最近一次作答仍未判分的题
（`graded:false`，单列且不参与错题调度）；返回 `{items, pending_items, total, graded_count, pending_count,
limit, message}`，条目含 `ref/source/source_label`。`GET /api/stats` 增加 `app_attempts/app_practiced/
app_wrong_count`（个人题单独计数，既有 `total/practiced/wrong_count` 语义不变）。

### 15.3 个人收录版本与「有新版」（`/api/paperlab/questions*`，`app/paperlab/versions.py`）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/questions/collect` | `{qid}`（题库题 ID/`bank:` 引用）→ 把只读题库题收录到 `pl_questions` 并**固定当时版本**（题面/选项/答案/解析/共用材料/`bank_version`/`content_revision`/`answer_revision` 一并快照）；同用户同题重复收录返回 `already_collected:true`（**200**，不静默覆盖、题版本仍为 1）；返回 `{collected, already_collected, question, question_id, version, has_new_version, source_available, source, warnings, notice}`。错误：题库中不存在该题 **404**、编号格式非法 **422**、题目隔离或下架 **409**、题组共用材料/内容不完整 **422**、题库暂不可用 **503** |
| GET | `/questions/{question_id}/versions` | `?limit=1-200（默认 50）` → 不可变版本历史：`{question_id, current_version, content_sha256, total, items:[{version, origin, type, subject, grade, knowledge_tags, difficulty, content, content_sha256, asset_refs, source_ref, source_question_id, source_*_revision, created_at, summary, is_current}], notice}`；`origin ∈ create/edit/collect/source_update/snapshot`；历史只读无修改入口；老数据首次读取时补一条 `origin=snapshot` 的当前版本行（只追加） |
| GET | `/questions/{question_id}/source-status` | 来源状态：`{question_id, version, source_kind, source_ref, source_question_id, collected_at, collected_revision, collected_*_revision, collected_content_sha256, current_*_revision, source_available, source_restricted, has_new_version, local_edited, diff:{changed,field_count,stem_similarity}, notice, warnings}`。来源下架/受限时 `source_available=false` 且不拉取、不回显受限正文；有新版时 `diff.changed` 给出「题干/答案/选项/解析/配图…」等字段名（不回显题库受限正文） |
| POST | `/questions/{question_id}/update-from-source` | `{confirm:true, expected_version?}` → 用题库新版内容生成**新版本**（`version+1`，写 `pl_question_versions`，历史版本与旧练习继续绑定旧内容）；未确认 **422**、版本冲突 **409**、非题库收录题 **422**、来源下架/受限按来源错误码（404/409/503）；已是最新返回 `updated:false`（不建版本）；成功返回 `{updated, question, previous_version, version, source, diff, warnings, notice}` |

列表/详情（`GET /questions`、`GET /questions/{id}`）统一附加 `has_new_version`/`source_available`（列表批量只读
探针，不逐题请求）；`has_new_version=null` 表示题库暂不可读、无法判定。数据侧见 §16。前端消费：
`web/js/pages/mybank.js`（来源/版本面板、`has_new_version` 徽标、`update-from-source` 显式确认）与
`web/js/pages/submission.js`（从个人题创建投稿）。

### 15.4 首页统计口径（`GET /api/insight/home`，`app/home_stats.py`）

`GET /api/insight/home?days=1-3650（默认 90）` → `{generated_at, range:{days,today,timezone:'UTC'}, bank, personal, caliber}`：

- `bank`（题库全局，只读发布包 + 运行期门禁）：`available/bank_version/bank_source/total_questions/
  visible_questions/practice_pool/auto_gradable/pending_verification/excluded_by_gates/status_counts/
  grading_counts/gates/caliber`。口径：**可练** = `practice_status='practice'` 扣除答案泄露/硬隔离/管理员临时隔离；
  **可自动判分** = 可练且 `grading_status='auto'`，再扣除题组材料缺失、参考答案缺失/占位与数据侧
  `missing_figure/missing_image/answer_missing/answer_and_solution_missing/multi_choice_answer` 质量标记；
  **待核验** = 可见题中不能自动判分的部分（`可自动判分 + 待核验 = 可见题数`，被门禁排除的题在 `excluded_by_gates`）。
- `personal`：`window_days/window_start/due_reviews/wrong_count/attempts/graded_attempts/correct_attempts/
  today{date,total,practice,classroom,paper}/first_attempt{samples,correct,accuracy}/
  repeat_practice{questions,attempts}/pending_grading{questions,attempts}/library{questions,pending_review,drafts}/
  continue{last_attempt,last_paper}/weak_knowledge[]/weak_note/knowledge_coverage/caliber`。
  - `first_attempt.accuracy` = 每题**第一次已判定**作答的正确占比（`pending` 不计样本、重复作答不重复计入；
    题库题与 `app:` 个人题按题目身份去重，`attempts` 表为唯一来源）。
  - `pending_grading` = 最近一次作答仍 `pending` 的题目数；`due_reviews` 与错题本一致（未归档且已到复习时间）。
  - `weak_knowledge` 复用 `app.insight` 知识点口径（<70% 为薄弱），返回真实样本数，已判定样本 <3 标
    `sample_limited`，不做个性化推断。
- `official`（**第四轮新增**，`app/official.official_caliber`）：`available/total_questions/practice_pool/
  auto_gradable/attempts/practiced/wrong_count/due_reviews/caliber/note`。口径：只统计当前公开可见精选题
  （社区待核验/已核验/精选；撤回/争议在查询时即时剔除、不缓存）；可自动判分 = 单选/多选（有选项且有答案）或
  填空题（有答案）；本人已练/错题/到期复习同样按题目当前可见性过滤（撤回题历史保留但不计入）。
  `bank.official_practice_pool`/`bank.official_auto_gradable` 仅在 official `available` 时下发，供首页题库卡片展示；
  广场表不可读时 official 块降级为 `available:false` + 中文 note，不影响其他统计。
- `caliber`：各口径中文说明（前端直接展示，不复制数字/不本地估算）。前端 `home.js`/`insight.js` 与
  `/api/insight/report` 并行读取，首页/学情同一后端口径；接口失败时页面保留既有空态/降级，不编造数字。

## 16. 广场 / 投稿 / 评审 / 发布（Phase 2，`/api/plaza`，25 条；Phase 3 增至 44 条，见 §18）

> 定位：方案 §5「广场、身份和入库规则」与 §6「投稿、评审和发布」的后端落地。实现
> `app/plaza/**`（`__init__` 汇总路由；`db` 建表/常量/事件；`service` 快照/校验/阈值/序列化；
> `public`/`figures` 匿名只读与公开衍生图；`submissions` 作者端；`review` 初审与评审；
> `release` 签发/撤回/历史/事件；`roles` 能力/身份/角色；`admin` 编辑台队列）。
> 数据表（8 张，均建在应用库、幂等 `CREATE TABLE IF NOT EXISTS`）：`plaza_submissions`（投稿主表 + 当前快照）、
> `plaza_submission_versions`（逐版本留档）、`plaza_reviews`（一人一版本一票）、`plaza_releases`（发布指针 + 历史）、
> `plaza_events`（全链路事件）、`plaza_public_assets`（公开衍生图映射）、`plaza_roles`、`plaza_identity`。

### 16.1 鉴权与公开口径

- 公开只读：`GET /questions`、`GET /questions/{public_id}`、`GET /me` 用 `app.auth.optional_user`
  （无/过期/损坏会话按匿名处理，不 401；`GET /me` 匿名返回 **200** + `authenticated:false`）；
  `GET /assets/{asset_id}`、`GET /questions/{public_id}/images/{index}` 无登录依赖（授权由「公开稿件 +
  公开衍生图」决定）。
- 其余 20 条全部 `current_user`；平台角色在接口内复校验：`service.require_reviewer`
  （`reviewer|subject_reviewer|editor|admin`）、`service.require_editor`（`editor|admin`）、
  `service.require_plaza_role(...)`。角色不足或无权查看他人评审/事件等统一 **403**；作者端私有资源
  （`GET/PATCH/submit/revise/withdraw` 投稿）对非作者统一 **404**「未找到该投稿」，不确认存在性。
  （Phase 3 新增 19 条的鉴权口径见 §18.1：登录用户/编辑管理员分层。）
- **公开可见性**（`db.PUBLIC_STATUSES`）：`community_review`（初审通过、社区核验中，公开可见并标注
  `review_status=community`「社区待核验」+ 评审进度）、`verified`（`review_status=verified`）、
  `published`（`review_status=featured`，平台精选）；`disputed`/`withdrawn` 不在列表，
  详情按 public_id 返回 **200** + `visible:false` + 中文 `notice`/`reason`（清楚状态而不是静默 404）；
  其他状态 **404**「未找到该公开题目」。
- 公开投影绝不输出内部 `user_id`、用户名、模型配置、私有附件存储路径或评审人身份；评审意见在公开详情中
  只给聚合进度（`review.independent_reviews/approves/revises/rejects/unsure/subject_reviewer_approves/
  threshold_met/min_*/reviews_needed/...`）。

### 16.2 公开只读（4 条，`app/plaza/public.py`）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/questions` | 公开列表：`?subject(≤40)&type|qtype(≤20，经 `schema.normalize_qtype`，未知 **422**「未知的题型筛选」)&keyword(≤120，`%_\` 转义)&source=community|featured&page&page_size(≤50)` → `{items,total,page,page_size}`；仅 `PUBLIC_STATUSES` 且 `public_id` 非空，排序「精选 > 核验通过 > 待核验」；条目含 `public_id/status/status_label/review_status/source/featured/community_pending/subject/type/grade/difficulty/knowledge_tags/stem_preview/has_answer/has_figures/review/notice` |
| GET | `/questions/{public_id}` | 公开详情：题面/选项/子题/答案/解析/评分标准/知识点/公开图（`images[]` 只给公开衍生图 `url`/`asset_id`/`question_image_url`，不可公开给 `accessible:false` + `reason`）/来源声明（`url/original_author/license_basis/rights_scope/attribution/reference/page_no/question_no`）/审核状态与进度/`figure_notice`/`notice`；`withdrawn`/`disputed` 返回 `visible:false` 状态页 |
| GET | `/assets/{asset_id}` | 公开衍生图直链（仅被当前公开稿件当前版本引用的公开副本）；非 PNG/JPEG/WebP **415**，其余任何不可公开 **404**「配图不可公开访问」；`Cache-Control: no-store` + `X-Content-Type-Options: nosniff` + `Content-Disposition: inline`（撤回立即失效，不在浏览器/CDN 留旧图） |
| GET | `/questions/{public_id}/images/{index}` | 按「公开题号 + 配图序号」读公开图；稿件非公开/争议/撤回、映射非当前版本或非 active、副本不可读一律 **404** |

### 16.3 作者端投稿（7 条，`app/plaza/submissions.py`）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/submissions` | **201**：`{question_id, source:{url?,original_author?,license_basis?,rights_scope?,attribution?,reference?,page_no?,question_no?,note?}, license_confirmed?}` → 从本人 `pl_questions` 生成**不可变快照**（白名单字段、配图只收资产引用、不复制任意键）并建草稿；返回 `{submission, checks, preview:{subject,type,grade,stem_preview,answer_count,image_count,source_declaration,notice}}` + 记录 `created` 事件；个人题不存在/非本人/已删除 **404** |
| GET | `/submissions/mine` | `?status&page&page_size(≤50)` → 我的投稿（`author_list_item`：状态/版本/预览/公开 ID/评审进度/截止）；未知状态 **422** |
| GET | `/submissions/{submission_id}` | 投稿详情（作者/评审/编辑视角）：`{submission:{...snapshot, checks, blocking, warnings, review_summary, reviews, my_review, viewer, can{submit/revise/patch/withdraw/initial_review/review/publish}}, checks}`；非作者/评审/编辑 **403**；评审员提交自己的票之前 `review_summary` 为 `{hidden:true, notice}`；作者只看到评审结论与理由（不含他人独立作答）；编辑/管理员可见 `reviewer_id`/`id` |
| PATCH | `/submissions/{submission_id}` | 仅 `draft`：`{version, source?, license_confirmed?}`；非草稿 **409**、版本冲突 **409**、原个人题已删除 **404**；内容/来源/授权均无实质变化 → `changed:false`「内容与当前版本一致，未创建新版本」；有变化 → `version+1` + 版本留档 + `updated` 事件 |
| POST | `/submissions/{submission_id}/submit` | 送审：`{version, confirm_preview:true, license_confirmed?}`；非草稿 **409**（按状态给中文原因）、版本冲突 **409**；未确认预览 **422**（`detail:{message, checklist}`）；阻断项 **422**（`detail:{message, blocking[], hints[]}`）→ `pending_initial` + `submitted` 事件 |
| POST | `/submissions/{submission_id}/revise` | 送审后改版：`{version, source?, license_confirmed?}`；仅 `pending_initial/community_review/disputed/rejected`（否则 **409**）；无实质变化 **422**「请先修改个人题或声明」；个人题已删除 **404**；生成新版本（`rejected→draft`，其余 →`pending_initial`），旧版本评审票留档且**不再计入阈值** + `revised` 事件 |
| POST | `/submissions/{submission_id}/withdraw` | 作者撤回：`{reason?≤2000}`；已撤回/已发布/当前状态不可撤回 **409**（已发布只能走发布端整条链）；写 `author_withdrawn` 事件 |

### 16.4 评审端（4 条，`app/plaza/review.py`）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/submissions/{submission_id}/initial-review` | 编辑初审：`{version, decision:'accept'\|'reject', reason, source_checked, privacy_checked, completeness_checked}`（兼容 `source_ok/privacy_ok/integrity_ok` 别名）；**403** 编辑不能初审本人投稿；非待初审 **409**；版本不符 **409**；退回原因 <2 字 **422**；通过时三项勾选缺项 **422**、授权未确认 **422**、仍存在阻断项 **422**；通过 → `community_review` + 分配不可猜 `public_id`（`plz-`+20 hex）+ 7 天评审窗口 + 登记公开衍生图 + `initial_review_accepted` 事件（响应含 `figures` 摘要） |
| GET | `/review/queue` | 评审队列：`?subject&page&page_size(≤50)`；按 `reviewer_subjects` 学科授权过滤（越权学科 **403**；管理员或纯编辑〔未同时持有评审角色〕不受学科限制，同时持有评审角色的编辑按学科授权过滤），作者回避，已评过当前版本的不再出现；按 `review_deadline_at` 升序；条目含 `submission_id/version/subject/type/grade/difficulty/knowledge_tags/stem_preview/review_deadline_at/deadline_passed/review_hidden/notice`（提交前看不到他人投票） |
| GET | `/review/mine` | 我的评审历史：`?page&page_size(≤50)` → `{items:[{review_id,submission_id,public_id,version,current_version,is_current_version,verdict,reviewer_role,reasons,independent_answer,conflict_declared,status,status_label,subject,type,grade,stem_preview,created_at,reviewed_at}],total,...}`；旧版票标 `is_current_version:false` |
| POST | `/submissions/{submission_id}/reviews` | **201** 提交结构化评审：`{version, verdict:'approve'\|'revise'\|'reject'\|'unsure', reasons≤4000, independent_answer≤8000, conflict_declared?}`；投稿不存在 **404**；作者自评 **403**；学科外评审 **403**；状态非 `community_review` **409**；非当前版本 **409**「旧版本投票已留档且不再计入」；同一版本重复投票 **409**（`detail` 含既有 `review`，一人一版本一票）；`revise/reject/unsure` 原因 <2 字 **422**；`approve` 独立作答/论证 <2 字 **422**；`conflict_declared=true` 的票留档但不计入独立票、不触发流转；学科复核员 `reject` → `disputed`；达到阈值 → `verified`（自动补 `public_id`、兜底登记公开图、写 `threshold_met` 事件）；响应 `{review, review_summary, transition:'pending'|'verified'|'disputed', submission:{id,status,status_label,version,public_id}}` |

### 16.5 签发 / 撤回 / 发布历史 / 事件链（4 条，`app/plaza/release.py`）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/submissions/{submission_id}/publish` | 编辑签发精选：`{version, reason?≤2000}`；**403** 签发本人投稿或本人承担过复核（有该投稿任意评审记录）的题；投稿不存在 **404**；版本不符 **409**；仅 `verified\|pending_release` 可签发（否则 **409**，`detail` 带 status/status_label）；授权未确认 **409**；评审阈值未达成 **409**（`detail.review` 摘要）；已发布且存在活跃发布记录 → **幂等 200**（`idempotent:true`）；成功 → `published` + `plaza_releases(active)` + 公开图登记 + `published` 事件；返回 `{release, submission, figures, idempotent, public_url:/api/plaza/questions/{public_id}}` |
| POST | `/releases/{public_id}/withdraw` | 编辑撤回整条链：`{reason(2–2000), mode:'normal'\|'emergency'}`；发布记录不存在 **404**；已撤回 → **幂等 200**（`idempotent:true`）；记录状态异常 **409**；成功 → 发布指针 `withdrawn`/`active_public_id=NULL` + 投稿 `withdrawn` + `release_withdrawn` 事件；公开列表/详情/公开图立即失效（公开图不共享缓存） |
| GET | `/releases` | 发布历史（编辑/管理员）：`?status=active\|withdrawn&page&page_size(≤100)` → `{items:[{id,public_id,submission_id,version,status,subject,type,grade,published_by,published_at,withdrawn_at,withdraw_mode,reason}],total,...}` |
| GET | `/submissions/{submission_id}/events` | 事件链：作者/编辑/管理员可看（评审员与其他用户 **403**）；作者视角不含 `actor_id/id`、编辑视角含完整审计字段；返回 `{submission_id,status,status_label,items:[{action,from_status,to_status,reason,payload,created_at,(actor_id,id 仅编辑)}]}`；`action` 取值见 §16.7 |

### 16.6 角色 / 身份 / 编辑台（6 条，`app/plaza/roles.py` + `admin.py`）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/me` | 能力摘要（可选登录）：匿名 **200** `{authenticated:false,user:null,roles:[],identity:null,capabilities:{read_public:true,submit/review/initial_review/publish/grant_roles/admin:false},stats:{submissions:0}}`；登录后返回 `roles[]`（有效角色，含学科/到期）、`identity`（最新身份申请，到期按 `expired` 展示、保留 `stored_status`）、`capabilities`、`stats.submissions`（`admin` 直通编辑/评审/签发能力） |
| POST | `/identity/apply` | **201** 提交身份申请：`{kind:'teacher'\|'student_teacher'\|'student'\|'contributor', subject?≤40, evidence(2–2000)}`；同 kind+subject 已有 pending → **200** `idempotent:true`；kind 非法 **422**。身份仅展示/运营审核，**不参与权限判断**（权限只认 `plaza_roles`） |
| POST | `/admin/roles` | 管理员授权/撤销：`{action:'grant'\|'revoke'（或兼容 active:bool）, role:'reviewer'\|'subject_reviewer'\|'editor', subject?, username?|user_id?, expires_at?}`；缺 action/active **422**；评审/学科复核角色 grant 必须带学科 **422**；`expires_at` 非 ISO **422**；用户不存在 **404**；grant 先置同 role+subject 旧行为 `active=0` 再插入（留档）；revoke 无对应角色 **404**；响应含该用户当前有效 `roles` |
| GET | `/admin/submissions` | 编辑台投稿队列（编辑/管理员）：`?status(逗号多选，默认 `pending_initial,community_review,verified,pending_release`；未知状态 **422** `detail:{message,value,allowed}`)&subject&page&page_size(≤100)`；作者以脱敏名 `投稿者·<sha256 前 6 位>` 展示（不含内部 `user_id`/用户名/评审人身份）；返回每条含状态/版本/预览/`author_display`/`submitted_at`/`review` 阈值进度/`review_summary`/`deadline_passed`/`blocking`/`blocking_count` |
| GET | `/admin/identity/queue` | 身份审核队列（编辑/管理员）：`?status=pending\|approved\|rejected\|expired\|expiring\|all（默认 pending）&page&page_size(≤100)`；`expiring` = 已通过且 30 天内到期；条目含申请人 `user{id,username}`、`kind/subject/evidence/status/stored_status/applied_at/reviewed_at/reviewed_by/expires_at/expired/review_due`；证明仅授权人员可见 |
| POST | `/admin/identity/{identity_id}/review` | 身份审核（编辑/管理员）：`{decision:'approve'\|'reject', reason?, expires_at?}`；驳回原因 <2 字 **422**；`expires_at` 非 ISO 或已过期 **422**；申请不存在 **404**；已处理 **409**；通过默认到期 365 天；写 `identity_approved`/`identity_rejected` 广场事件；返回更新后的身份 + 操作信息 |

### 16.7 状态机 / 阈值 / 审计字段 / 错误码速查

**投稿状态机**（`db.SUBMISSION_STATUSES` + `STATUS_LABELS`）：
`draft（草稿）→ pending_initial（待初审）→ community_review（社区待核验）→ verified（社区核验通过）
→ published（平台精选）`；分支 `disputed（有争议或待修订）`、`pending_release（待平台发布，预留）`、
`rejected（初审未通过）`、`withdrawn（已撤回）`。

- 可评审：仅 `community_review`；可签发：`verified`/`pending_release`；作者可改版：
  `pending_initial/community_review/disputed/rejected`（改版生成新版本、旧票留档不计阈值）；
  作者可撤回：`WITHDRAWABLE_STATUSES`（不含 `published`）。
- **试点评审阈值**（`db.THRESHOLD_MIN_*`，集中常量、可按试点调整）：同版**独立评审 ≥5**、**通过 ≥4**、
  其中**学科复核员通过 ≥2**，且**不存在未解决 reject 阻断** → `verified`；学科复核员 `reject` → `disputed`；
  7 天（`DEFAULT_REVIEW_DAYS`）未凑齐保持当前状态（不自动失败、不自动通过）。
- **独立票口径**：排除作者自评与 `conflict_declared=1` 的票；管理员/编辑无学科复核资格时不冒充
  `subject_reviewer` 票；同一评审人对同一版本唯一（数据库 `UNIQUE(submission_id,version,reviewer_id)`）。
- **回避**：作者不能评审自己的投稿；编辑不能初审自己的投稿；编辑不能签发本人投稿或本人评审过的题。
- **投票隐藏**：评审员提交自己的评审之前看不到已有投票与意见（服务端 `review_summary.hidden`）；
  提交后可见聚合与他人的评审结果（不含身份）；作者只看结论与理由。
- **事件链**（`plaza_events`，与状态更新同事务）：`action` 取值 `created/updated/submitted/revised/
  author_withdrawn/initial_review_accepted/initial_review_rejected/review_submitted/threshold_met/
  disputed/published/release_withdrawn/identity_approved/identity_rejected`；每条含
  `from_status/to_status/reason/payload_json/actor_id/created_at`（公开接口不返回内部身份）。
  Phase 3 新增事件：`issue_submitted/report_submitted/appeal_submitted/issue_resolved/issue_rejected/
  report_resolved/report_rejected/appeal_resolved/appeal_rejected/quality_checked/quality_issue/
  quality_withdrawn/release_rolled_back`（触发点与 payload 见 §18.4/§18.5/§18.7）。
- **撤回传播**：撤回只改活跃发布指针与投稿状态；公开列表/详情/公开图授权都以「稿件公开状态 + 当前版本 +
  活跃 release」实时判定，公开图响应 `Cache-Control: no-store`；历史作答/成绩保留（方案 §5 要求）。
- **公开衍生图**（`app/plaza/figures.py`）：进入公开可见状态时为快照逐图复制公开副本
  （`pl_assets.public_derivative=1,status='published'`，sha256 校验、≤20MB），登记
  `plaza_public_assets(submission_id,version,image_index,public_asset_id,status,reason)`；无法公开的原因码
  `bank/unregistered/private/disabled/missing/integrity/media_type/unresolved/error` 逐图记录并给编辑/访客
  中文提示；旧版本公开图自动置 `disabled`；配图问题**不阻断**初审/签发。
- **错误码速查**：`401` 未登录（公开只读不走 401）；`403` 角色不足/作者回避/学科外评审/无权查看；
  `404` 投稿/发布记录/身份申请/个人题不存在或非本人（私有资源不泄露存在性）；`409` 版本或状态冲突、
  重复投票、重复撤回/签发幂等（`idempotent:true` 仍为 200）、作者撤回已发布题；`415` 公开图类型非白名单；
  `422` 参数/前置条件不满足（未确认预览、阻断项、原因缺失、未知状态筛选、身份类型非法等；部分 `detail`
  为对象）；`503` 依赖未就绪（如资产模块异常导致公开图复制降级不报错，个别写失败由上层返回）。
- **前端页面**（Phase 2 前端，见 `web/js/pages/`）：`#/plaza`（公开列表/详情，访客可读）、`#/submission`
  （我的投稿）、`#/review`（评审台）、`#/plaza-admin`（编辑台）；访客壳 `renderGuestShell` 只渲染公开路由，
  未登录访问非公开路由回落登录页并在登录成功后按原 hash 回跳；工作台入口按 `/api/plaza/me` 能力过滤。
  本轮（Phase 3）前端增量见 §18.8。

## 17. 平台精选接入（Phase 3，`official:<public_id>`）

> 定位：方案 §6「统一题目引用」+ §8 阶段 3「发布精选接入检索、练习、错题、工坊、助手和课堂」。
> 实现：只读适配层 `app/official.py`；引用解析 `app/question_ref.SUPPORTED_SOURCES`；练习链路 `app/practice.py`；
> 工坊快照 `app/paperlab/{papers,practice}.py`；前端 `practice/question/paperlab/home` 页面与
> `paperCompose`/`paperAttempt` 组件。本轮**没有新增 HTTP 路由**：精选通过既有路由的 `source` 参数与
> `official:` 引用进入学习链路。

### 17.1 引用、可见性与投影（`app/official.py`）

- 引用 `official:<public_id>`；`public_id` 由广场签发（`plz-` + 20 位 hex，与内部 id 解耦）。校验正则
  `^[A-Za-z0-9][A-Za-z0-9._:-]{0,119}$`（宽松以兼容历史/测试数据）；读取层对不合法/不存在的 public_id 一律按
  不存在处理（**404**，不泄露格式/存在性）；引用解析层对空 key/空白/超长等格式错误仍 **422**（与 bank/app 一致）。
  `normalize_public_id()` 返回空串表示非法。
- 可见性（与 `app/plaza/db.py` 同一口径，**查询时判定、不做进程内缓存**）：
  - 可读：`community_review`（社区待核验）、`verified`（社区核验通过）、`published`（平台精选），且 `public_id` 非空；
  - `published` 还必须存在 `plaza_releases` 的 `status='active'` 指针（签发后撤回立即从检索/详情/作答/组卷退出）；
  - `draft/pending_initial/disputed/withdrawn/rejected/pending_release` 一律不可读：详情、列表、作答、组卷快照统一 **404**。
- 投影字段（与 `practice.load_question` 同形）：`qid/ref=official:<public_id>`、`source=official`、
  `source_label=平台精选`、`subject/grade/type/type_label/objective/has_answer/has_solution/knowledge_tags/
  stem_plain/options/images`；平台侧元数据 `public_id/official_status/status_label/review_status(community|verified|
  featured)/review_status_label/published_version/published_at/featured/community_pending/notice/source_declaration/
  figure_notice`；答案/解析只在作答后经详情解锁（列表卡不含答案）。`images[]` 为公开衍生图：
  `{index,role,alt,accessible,url,asset,media_type,reason}`，`url=/api/plaza/questions/{public_id}/images/{index}`
  （撤回/争议后 404）。
- 状态提示（`NOTICE_*` 中文）：社区待核验/已核验/精选各自的说明；`inspect_official()` 供错题卡/复习队列在题目撤回后
  只显示「内容已撤回/暂停展示」与状态，不返回受限正文。
- 错误码：读取不可见 **404**（`未找到该平台精选题目` / `该平台精选题目不存在或已不可见`）；广场表不可读时
  `inspect_official` 降级为不可见（日志告警，不阻断错题/复习列表）。

### 17.2 `GET /api/questions` 来源筛选（`source=bank|official|all`）

| `source` | 行为 | 响应差异 |
|---|---|---|
| `bank`（默认） | 原有题库分页/筛选/排序行为与结构不变；支持题库全部筛选与 `include_reviews` | 原字段；`review_slots` 仅此模式提供 |
| `official` | 平台精选公开题列表；只应用 `subject`/`type`/`q`/`page`/`page_size`；年份/省份/难度/知识点等题库专属筛选不适用 | `source:'official'`、`bank_total:0`、`official_total`、`review_slots:[]`、`notice`（说明筛选范围） |
| `all` | 题库在前、精选在后的合并列表；按合并总偏移严格分页 | `source:'all'`、`bank_total`、`official_total`、`total=bank_total+official_total`、`review_slots:[]`、`notice` |

- 非法 `source` **422**「来源筛选值无效，只支持 bank / official / all」（`QUESTION_SOURCES`）。
- `type` 同时接受工坊题型（`SINGLE/MULTIPLE/BLANK/SUBJECTIVE/MIXED`）与练习题型（`single_choice` 等），
  经 `paperlab.schema.normalize_qtype` 归一；未知题型 **422**「未知的题型筛选」。
- 排序（official 内部）：`published > verified > community_review`，同状态按 `updated_at DESC, id DESC`；
  关键词为快照 JSON 的 `LIKE`（`%\` 转义），题库专属排序参数对 official 不生效。
- 前端能力探测：旧后端会忽略未知 `source` 参数并照常返回题库结果，因此练习页/组卷/首页均先用**非法值**确认
  参数受控（422）再展示来源控件；探测不通过时整体隐藏来源筛选（不把默认题库伪装成「平台精选」）。
  **第四轮修正**：练习页/首页/组卷的能力探测改为合法 `source=official` 请求（200）并按响应是否含
  `official_total`/`grade_filter` 判定（浏览器不再把 4xx 记为控制台错误），旧后端自然降级。

#### 17.2.1 学段筛选 `grade`（第四轮新增，`app/practice.py` + `app/official.py`）

- `GET /api/questions?grade=` 接受标准年级（小学一年级…高中三年级，**精确匹配**）与学段（小学/初中/高中，
  **前缀匹配**），支持逗号分隔多值；未知/超长值**安全降级不 422**（`degraded:true` + `reason`）。
- 生效范围：official 侧按**快照 grade** 过滤；题库侧仅当只读题库存在 `grade` 列时生效（`r5` 无该列 →
  不筛选，`bank_applied:false`）。`source=bank|official|all` 合并分页语义不变（`all` 仍题库在前，学段两侧分别生效）。
- 响应追加 `grade_filter:{requested,applied,bank_applied,official_applied,values,stages,reason}`；
  `applied` = 任一侧实际生效；题库无列且请求了学段时 `reason` 为
  「当前只读题库没有年级字段，学段筛选在题库侧暂不生效（平台精选按快照年级过滤）」。
- `GET /api/catalog` 同步下发 `grades`（标准年级，优先 `paperlab.db.GRADES`）、`grade_stages`（小学/初中/高中）、
  `grade_note`（口径说明）；前端练习页据此生成筛选项并做能力探测（§23.2）。
- 错误口径：非法/未知学段值**不报错**（安全降级）；只有 `source`/`type` 等既有参数保持 422 语义。

### 17.3 练习 / 作答 / 错题 / 复习 / 统计（`app/practice.py`）

- 接受 `official:` 引用的接口：`GET /api/questions/{qid:path}`、`GET /api/reveal?qid=`、
  `GET /api/attempts/history?qid=`、`POST /api/attempts`（Form `qid`）、`POST /api/mastery`、
  `POST /api/wrongbook`；`GET /api/bank-image` **不接 official**（公开图走 §17.1 的广场图接口）。
- 写库键：`attempts/wrong_questions/mastery.qid = official:<public_id>`（与 bank 裸 qid、`app:<id>` 并列）；
  幂等键 = `official:<public_id>|<submission_id>`（`submission_ledger.kind='attempt'`）。
- 判分与题库/个人题同一门槛：客观题（快照题型 `SINGLE/MULTIPLE` 且选项非空、答案可解析）自动判分；
  `BLANK` 按文本规则（多空/备选答案）；其余主观题保留作答判 `pending`；撤回/争议后不可再作答（404）。
- 错题与复习：`GET /api/wrongbook` 合并 bank/app/official 卡片（条目带 `ref/source/source_label`）；official 题撤回后
  降级为占位卡（`unavailable:true`、`withdrawn:true`、`notice` 中文提示），历史成绩仍可读；`GET /api/review/queue`
  的 `items`/`pending_items` 同样包含 official 条目。
- `GET /api/stats` 新增 `official_attempts/official_practiced/official_wrong_count`（个人题有 `app_*` 对应字段）；
  既有 `attempts/practiced/wrong_count` 语义不变（official 作答同样计入既有字段）。
- `recent_attempts`/`attempts_history` 条目带 `ref/source/source_label`（历史作答也可按来源解析）。
- 纠错入口边界：题库侧 `POST /api/questions/{qid}/issues` 仍只接受只读题库题（内部 `load_question` 不解析 official）；
  平台精选题的纠错/举报走广场接口 `POST /api/plaza/questions/{public_id}/issues|reports`（§18.4）。

### 17.4 工坊建卷快照与冻结练习（`app/paperlab/{papers,practice}.py`）

- `papers.split_ref()` 支持 `official:public_id`：格式非法 **422**、不可见（未发布/撤回/争议）在快照时 **404**；
  同一卷内重复引用 **422**、引用 ≤340 字符、单卷 ≤60 题（沿用既有上限）。
- `snapshot_official()` 冻结内容：`ref/source='official'/source_id/public_id/published_version/review_status(_label)/
  featured/community_pending/notice/subject/grade/type/difficulty/knowledge_tags/score/content{stem,options,answers,
  answer_text,answer_kind,explanation,rubric,parts≤20,images[{kind:'official',index,role,alt,url,public_asset_id,
  media_type,accessible,reason}]}/source_declaration/origin/frozen_at`。
- 建卷/改卷题项与整卷练习逐题冻结（`pl_attempt_items`）语义不变：**已经建好的卷与已开始的练习不因精选题后续修订/撤回而改题面**；
  但公开配图读取按「广场公开状态 + 当前版本公开副本」实时授权，撤回后配图 **404**（题面文字与判分参考答案仍按冻结快照）。
- 冻结配图路由：`GET /api/paperlab/papers/{id}/items/{pos}/images/{idx}` 与
  `GET /api/paperlab/attempts/{id}/items/{pos}/images/{idx}` 遇 official 图片时调用 `plaza.figures.authorize_public_asset`
  （只读公开副本；撤回/停用/越权一律 **404**）。
- 判分与错题：整卷提交对 official 题复用统一错题本（`wrong_questions.qid=official:<public_id>`），响应新增
  `official_bank_unlinked[]`/`official_wrong_added[]`（逐题给出 `item_position/ref/judgement/linked/reason`）；
  `notice` 汇总三类来源的错题入账数。已建卷/历史练习按冻结快照回看。
- 卷面投影：`public_item` 增加 `source_label/public_id/review_status/review_status_label/published_version`；
  导出/打印/回看标注「平台精选」来源。
- AI 组卷候选池（`paperlab.compose`）仍只从题库与个人题取候选；official 目前通过**手工选题**（组卷页「平台精选/全部来源」
  页签）加入试题篮，见 §17.6 与实施记录「未实施」。

### 17.5 前端（练习 / 详情 / 组卷 / 首页 / 整卷练习）

- 练习页 `web/js/pages/practice.js`：来源筛选（高考题库=默认省略参数 / 平台精选 / 全部）；筛选值进入 `state.filters`
  与 URL 深链（`#/practice?...&source=`），刷新/分享保持；来源徽标（高考题库/平台精选/我的题库）+ 审核状态徽标
  （社区待核验/已核验/精选）；official 模式提示题库专属高级筛选不适用，空态单独文案；能力探测失败整体隐藏来源控件。
- 题目详情 `web/js/pages/question.js`：平台精选来源标签、`public_id` 与精选版本、官方/审核提示与后端 `notice`
  原样展示；official 题隐藏题库专属动作（AI 讲解/管理员标注/收录）；答案门控沿用「提交后揭示」（旧后端 `/reveal`
  未接 official 时只做一次降级提示，不把整页打成错误）；题库题的「收录到我的题库」按钮调用
  `POST /api/paperlab/questions/collect {qid}`（幂等，已在会话内标记「已收录」，重复点击由 `already_collected` 纠正）。
  **第四轮补齐**（详见 §23.1）：official 公开配图按 `images[].accessible/url` 渲染（回退 `figure_notice`，兼容
  `figure_urls`）；精选「报错」按钮改为真实纠错入口 `POST /api/plaza/questions/{public_id}/issues`（登录引导/
  成功 toast）；保留「在广场查看」`#/plaza?id=<public_id>`。
- 组卷 `web/js/pages/paperlab.js` + `web/js/components/paperCompose.js`：选题来源页签「我的题库/高考题库/平台精选/全部」；
  官方卡转试题篮用统一引用 `official:<public_id>`，建卷由服务端按引用冻结来源快照；能力探测未就绪时保守隐藏 official/all。
- 首页 `web/js/pages/home.js`：题库口径卡片「平台精选可练数量」——第四轮起后端 `GET /api/insight/home` 已下发
  `official` 块与 `bank.official_practice_pool`/`official_auto_gradable`，首页/学情页**优先**展示官方精选口径
  （`practice_pool/practiced/wrong_count`，字段缺失时隐藏）；`/api/questions?source=official` 能力探测仅作旧后端
  回退（不伪造数字），`/api/stats` 的 `official` 块也可用。
- 整卷练习 `web/js/components/paperAttempt.js`：来源徽标统一读后端 `source_label`（高考题库/平台精选/我的题库），
  `source` 缺失时按来源兜底；official 快照配图用 `image.url` 直接渲染；题库/个人题配图仍走冻结配图路由。

### 17.6 错误码与已知边界速查

- `404`：public_id 非法/不存在/不可见（draft、未发布、撤回、争议、无活跃发布指针）；快照建卷时不可见；撤回后的公开图
  （广场图接口）。`422`：`/api/questions` 的 `source` 非法、题型未知；工坊 `official:` 格式非法 / 重复引用 / 超上限。
  `401`：未登录（列表/详情/作答均需登录）。没有为 official 引入新的 409 语义。
- 边界（第三轮登记项已在第四轮闭环，见 §20/§23）：练习详情页 official **配图**已渲染公开图（撤回回退
  `figure_notice`）；official「报错」按钮已接广场纠错接口；official 已接入 AI 助手工具与课堂素材（`source_ref`）；
  `/api/insight/home` 与 `/api/stats` 已下发 official 口径。仍保留的边界：official 未进入 AI 组卷候选池（手工选题
  页签）；学段筛选在 `r5` 题库侧因无 `grade` 列而安全降级（仅 official 生效）；official 检索为快照 JSON `LIKE`。

## 18. 质量运营：通知 / 收藏 / 反馈 / 抽检 / 指标 / 回滚（Phase 3，`/api/plaza` 44 条；Phase 4 增至 51 条，见 §22）

> 定位：方案 §5「投稿到官方发布」「文件、版权和未成年人」+ §8 阶段 3「上线抽检、通知及回滚」。
> 实现 `app/plaza/{notify,favorites,feedback,quality,metrics}.py` 与 `release.py` 回滚；在 Phase 2 的 25 条上新增
> **19 条**（合计 **44 条**）。新增 6 张表（`plaza_notifications`、`plaza_favorites`、`plaza_quality_checks`、
> `plaza_issues`、`plaza_reports`、`plaza_appeals`），广场表 14 张；所有写事务仍统一
> `db.write_lock(con,"plaza-write")` + 显式主键，通知/事件与状态更新**同一事务**。
> **第四轮（Phase 4）增量**：`/api/plaza` **44 → 51 条**（通知未读数 1、抽检门禁 GET/PUT 2、一键恢复 1、
> 三类批量处理 3），新增 2 张表（`plaza_quality_gate_log`/`plaza_ops_batches`，广场表共 16 张）、
> 签发前抽检与门禁接线、通知 `kind` 全读与窗口去重、指标新增字段；详见 §22。

### 18.1 本轮新增 19 条路由

| 分组 | 方法 | 路径 | 说明 |
|---|---|---|---|
| 通知（2） | GET | `/notifications` | 我的通知列表：`?unread_only&kind&page&page_size(≤100)` → `{items,total,unread_count,page,page_size}`；条目 `{id,kind,title,body,submission_id,public_id,link,payload,read,read_at,created_at}`；只返回本人 |
| | POST | `/notifications/read` | 标记已读：`{ids?:[≤200], all?:bool}`（至少一项，否则 **422** 对象）→ `{updated,unread_count,message}`；他人通知被忽略 |
| 收藏（3） | POST | `/questions/{public_id}/favorite` | 收藏（幂等）：已收藏返回 `idempotent:true`；不可见/撤回/争议 **409**（`detail` 带 status/status_label）；未知题 **404** |
| | DELETE | `/questions/{public_id}/favorite` | 取消收藏（幂等，已撤回也允许）；返回 `{favorited:false, public_id, favorite_count, idempotent, message}` |
| | GET | `/favorites` | 我的收藏：`?page&page_size(≤100)` → 按当前投稿状态实时投影（`available/status/status_label/review_status/featured/subject/type/grade/difficulty/stem_preview/source/link/notice`） |
| 纠错 / 举报 / 申诉（10） | POST | `/questions/{public_id}/issues`（**201**） | 题目纠错：`{field, description(2–4000)}`；`field ∈ stem/options/answer/explanation/figure/source/other`（未知 **422** 带 `allowed`）；仅当前公开可见题（否则 **409**） |
| | POST | `/questions/{public_id}/reports`（**201**） | 举报：`{reason, detail≤4000}`；`reason ∈ copyright/privacy/answer_error/content_quality/spam/other`（未知 **422**） |
| | POST | `/submissions/{submission_id}/appeals` | 作者申诉：`{reason(2–2000)}`；仅作者本人（他人 **404**）；仅 `rejected/withdrawn/disputed`（否则 **409** 对象）；同投稿已有 pending 申诉 → **200** `idempotent:true` 返回既有申诉；成功 **201** |
| | GET | `/submissions/{submission_id}/appeals` | 申诉记录：作者看本人、编辑/管理员看全部（申诉作者脱敏 `申诉作者·<hash>`）；他人 **404** |
| | GET | `/admin/issues` | 纠错队列（编辑/管理员）：`?status=pending\|resolved\|rejected\|all&page&page_size(≤100)` → `{items,total,page,page_size,status_filter,counts}`；反馈人脱敏 |
| | GET | `/admin/reports` | 举报队列，同上 |
| | GET | `/admin/appeals` | 申诉队列，同上（申诉作者脱敏） |
| | PATCH | `/admin/issues/{issue_id}` | 处理：`{action:'resolve'\|'reject', reason?}`；非 pending **409**；驳回必须给原因（<2 字 **422**）；结果写事件 + 通知反馈人 |
| | PATCH | `/admin/reports/{report_id}` | 同上 |
| | PATCH | `/admin/appeals/{appeal_id}` | 同上 |
| 抽检（2） | GET | `/admin/quality/queue` | 抽检队列（编辑/管理员）：`?status=all\|pending\|checked\|passed\|issues&subject&page&page_size(≤100)`；条目含 `submission_id/public_id/version/subject/type/grade/stem_preview/status/status_label/published_at/check_status/latest_check/checks_count/auto_withdrawn/updated_at`；`counts` + `sampling{mode:'initial_100',required:true,label,published_total,checked_total,pending_total,coverage_rate}`；未知状态 **422** 对象 |
| | POST | `/admin/quality/checks`（**201**） | 记录抽检：`{submission_id?\|public_id?, decision:'pass'\|'issue', severity?:low\|medium\|high, findings?:[≤20 条，每条≤500 字], note≤2000}`；仅已发布且有活跃发布（否则 **409** 对象）；作者回避 **403**；`issue` 必须 findings（**422**）；**high** 自动撤回（见 §18.5） |
| 指标（1） | GET | `/admin/metrics` | 质量指标（编辑/管理员）：题量/状态/学科/题型、可判分率、图可用率、评审中位时长、抽检问题率与 100% 覆盖、纠错/举报/申诉积压、撤回/回滚数 + `caliber` 口径说明（见 §18.6） |
| 回滚（1） | POST | `/submissions/{submission_id}/rollback` | 发布回滚：`{reason(2–2000)}`；编辑/管理员、作者回避 **403**；无活跃发布/无更早历史发布/快照缺失/CAS 冲突 **409**；事务内撤回当前发布并恢复上一已发布版本（见 §18.7） |

> **第四轮追加 7 条**（Phase 4，见 §22）：`GET /notifications/unread-count`、`GET|PUT /admin/quality/gate`、
> `POST /releases/{public_id}/restore`、`POST /admin/{issues|reports|appeals}/batch`；并扩展
> `POST /notifications/read`（`kind` 过滤）、`POST /submissions/{id}/publish`（`quality_gate_override`/
> `override_reason`）、`POST /admin/quality/checks`（`pre_publish` 签发前抽检）。

### 18.2 通知中心（`app/plaza/notify.py`）

- 只发给**相关用户**：投稿作者、收藏者、评审员本人、反馈人；`notify_many` 按 `user_id` 去重并支持 `exclude`
  （如排除作者本人）。通知正文只写事件与处理结论，不含他人 `user_id`/用户名/评审意见原文。
- 通知类型集中登记（`db.NOTIFICATION_KINDS`，新增只追加）：`submission_submitted/submission_revised/
  initial_review_approved/initial_review_rejected/submission_verified/submission_disputed/submission_published/
  release_withdrawn/release_rolled_back/submission_withdrawn/favorite_withdrawn/favorite_restored/quality_issue/
  quality_withdrawn/issue_resolved/issue_rejected/report_resolved/report_rejected/appeal_resolved/appeal_rejected`
  （Phase 4 追加 `release_restored`，共 21 类，见 §22.5）。
- 钩子位置：送审/改版/作者撤回（`submissions.py`）、初审通过与退回、核验通过/争议（`review.py`）、签发/撤回/回滚/
  一键恢复（`release.py`）、抽检问题与自动撤回（`quality.py`）、反馈处理（`feedback.py`，含批量）、收藏者撤回/恢复
  （`favorites.notify_favoriters`）。跳转 `link` 为前端 hash（`#/plaza?id=`、`#/submission?id=`、`#/review`、
  `#/plaza-admin?section=ops`）。
- 读取接口只返回本人；`ids` 中属于他人的通知被忽略；`unread_count` 为本人全部未读数；匿名 `/me` 能力为 false。
  第四轮另增轻量未读数与 `kind` 全读，并对同「接收人+类型+投稿+版本」窗口去重（§22.5）。

### 18.3 收藏（`app/plaza/favorites.py`）

- 只有当前公开可见题可收藏：`status ∈ PUBLIC_STATUSES` 且 `figures.submission_publicly_visible()` 为真
  （`published` 还要求活跃发布指针）；草稿/未通过/撤回/争议 **409**（`detail:{message,status,status_label}`）。
- `plaza_favorites` 以 `UNIQUE(user_id,submission_id)` 保证幂等；列表按当前投稿状态实时投影，
  撤回后保留记录并 `available:false` + `notice`，不删除收藏。
- 公开详情 `GET /api/plaza/questions/{public_id}` 本轮新增 `favorited`（匿名 false）/`favorite_count`；
  撤回/争议状态页同样返回这两个字段（`favorited` 仍可查看、`available` 由状态判断）。

### 18.4 纠错 / 举报 / 申诉（`app/plaza/feedback.py`）

- 目标必须是**当前公开可见**题：未知公开题号 **404**，不可反馈状态 **409**（`detail:{message,status,status_label}`）；
  反馈提交写 `plaza_events`（`issue_submitted/report_submitted`）并在响应返回已脱敏的反馈条目。
- 编辑端三个队列共用 `_queue`：`status` 过滤 `pending|resolved|rejected|all`（未知 **422** 对象 `{message,allowed}`）、
  反馈人/申诉作者脱敏（`db.masked_display`），附题目摘要与题目状态。
- 处理动作：`resolve` → `resolved`、`reject` → `rejected`；重复处理 **409**（`detail` 带当前状态），
  并发用条件 UPDATE（`WHERE status='pending'`）；驳回原因必填（<2 字 **422**）；同事务写
  `issue_resolved/issue_rejected/report_resolved/report_rejected/appeal_resolved/appeal_rejected` 事件并通知提交人。
- 作者申诉：`db.APPEALABLE_STATUSES=('rejected','withdrawn','disputed')`；提交幂等（待处理申诉返回既有记录，HTTP 200）；
  编辑/管理员可在 `submission_detail` 看到 `appeals`/`my_appeal`（作者只看自己的），`can.appeal` 由状态与作者身份决定。

### 18.5 抽检与 high 自动撤回（`app/plaza/quality.py`）

- 试点规则常量（`db`）：`QUALITY_SAMPLING_MODE='initial_100'`、`QUALITY_SAMPLING_REQUIRED=True`
  （首批发布题 100% 独立抽检）；`QUALITY_AUTO_WITHDRAW_SEVERITIES=('high',)`；稳定后可由运营改为常规 10%、高风险 100%
  （改常量，不改状态机）。
- 抽检只对「已发布 + 有活跃发布指针」的题；抽检员回避本人投稿（**403**）；同一版本可多次抽检，队列按**最新一次**结论
  计算 `check_status`（`pending/checked/passed/issues`），并保留全部历史记录。
- `issue` 必填 `findings`（缺失 **422**）；`severity` 缺省 `medium`；`pass` 忽略 severity/findings。
- **high 自动撤回**：同一事务调用 `release.withdraw_active_release`（`mode='emergency'`、
  `event_action='quality_withdrawn'`）——发布指针 `withdrawn`、投稿 `withdrawn`、写事件、通知作者与收藏者；
  抽检行置 `auto_withdrawn=1`；响应 `auto_withdrawn:true` + `withdraw` 摘要 + `notified:{author,favoriters}`。
  撤回后公开列表/详情/公开图/`official:` 检索立即失效，历史作答保留（与 §16.7 撤回传播一致）。
- 非 high 的 `issue` 只记录并通知作者修订（`quality_issue`）。

### 18.6 质量指标（`app/plaza/metrics.py`）

`GET /api/plaza/admin/metrics` → `{generated_at, totals, by_subject[], by_type[], scorable, figures, review_duration,
quality, feedback, withdrawals, caliber}`：

- `totals`：投稿总数、`public_visible`、各状态计数与 `by_status`。
- `by_subject`/`by_type`：按学科/题型的投稿数及公开状态分布（`count/published/verified/community_review`）。
- `scorable`：公开可见题中快照含参考答案（含子题）的占比 `{total,scorable,rate}`。
- `figures`：公开可见题**当前版本**配图引用中可匿名读取的公开衍生图占比
  `{total_images,public_images,unavailable,coverage_rate}`。
- `review_duration`：`initial_review_accepted → threshold_met` 中位小时数 + `submitted → published` 中位小时数
  （样本数并列）。
- `quality`：`checks_total/passed/issues/high_issues/issue_rate/high_issue_rate/pending_checks/sampling{...}`；
  100% 抽样覆盖率把「抽检后被撤回的题」仍计入发布基数，不因严重问题自动撤回而虚降。
- `feedback`：纠错/举报/申诉总数与 `pending` 未处理数；`withdrawals`：发布撤回数、投稿撤回数、回滚次数（事件链口径）。
- 所有口径由后端计算，前端只展示（`plazaAdmin.js` 指标分区）；指标为运营口径，不做个性化。
- **第四轮新增字段**（见 §22.6）：`quality.gate{...门禁模式/激活/窗口/阻断/override/通过计数}`、
  `quality.pre_publish_checks/post_publish_checks`、`withdrawals.restorations`、顶层 `restorations{total,latest_event}`、
  `ops.batch{batches,requested,updated_total,skipped_total,by_kind}`、`notifications{total,unread_total,read_total}`；
  `caliber` 同步追加对应中文口径说明。

### 18.7 回滚与撤回传播（`app/plaza/release.py`）

- `POST /submissions/{id}/rollback`：仅编辑/管理员（作者回避 **403**）。前置校验：投稿存在 **404**、当前必须有活跃发布
  （否则 **409** 对象）、必须存在更早的 `status='withdrawn'` 历史发布且版本号更早（否则 **409**）、历史版本快照存在
  （否则 **409**）。
- 同一事务：当前 release → `withdrawn`（`withdraw_mode='rollback'`、原因）；上一 release → `active`
  （`active_public_id` 恢复、清空撤回字段）；投稿主表回到恢复版本的快照/来源/指纹/授权并 `status='published'`
  （条件 UPDATE 防并发）；重新登记公开衍生图；写 `release_rolled_back` 事件（含 `withdrawn_release_id/
  restored_release_id/from_version/to_version/figures`）；通知作者（`release_rolled_back`）与收藏者
  （`favorite_restored`）。
- 响应：`{submission:{status:'published',version,current_version,public_id,public_visible:true}, release,
  restored_release, withdrawn_release, restored_version, notified:{author,favoriters}, figures, public_url,
  idempotent:false, message}`；CAS 失败 **409**「发布记录已被其他操作修改」/「投稿已被其他操作修改」。
- 与撤回的一致性：两种操作都只改「活跃发布指针 + 投稿状态/版本」，公开读（列表/详情/公开图/精选检索）以查询时状态为准；
  `plaza_releases.active_public_id` 唯一索引保证单一活跃发布。
- **Phase 4 增补**：抽检门禁与「一键恢复」见 §22.2/§22.4——回滚（rollback，版本回退）与恢复（restore，同版本重新 active）
  是两条独立路径；`withdraw_mode='rollback'` 的撤回记录不支持一键恢复（**409**）。

### 18.8 前端（Phase 3）

- 通知中心：`web/js/components/plazaNotifications.js` 顶栏铃铛（`data-plaza-notification-bell`、未读红点、60s 轮询、
  页面隐藏跳过）；面板列表/全部已读/单条已读/按 `link` 跳转；`web/js/main.js` 只在登录主壳挂载、登出清理；访客壳不挂载；
  接口未就绪时铃铛隐藏（能力探测，不崩页）；投稿台可复用 `openPlazaNotificationPanel()`。
- 广场公开页 `web/js/pages/plaza.js`：登录后详情操作区加入收藏（`data-plaza-action="favorite"` + `data-favorited`）、
  纠错、举报（匿名点击引导登录并保留回跳）；`favorited` 缺失时探测 `/plaza/favorites` 降级。
- 编辑台 `web/js/pages/plazaAdmin.js`：新增 `quality`（抽检队列/结论表单/high 自动撤回提示）、`ops`（纠错/举报/申诉
  队列与处理）、`metrics`（指标卡片与学科/题型分布、口径说明）分区；发布历史加「回滚」对话框；能力位
  `capabilities.quality/ops/metrics/rollback`（`/me` 下发）控制入口；接口未就绪显示占位。
- 投稿台 `web/js/pages/submission.js`：对 `rejected/withdrawn/disputed` 显示申诉表单与记录（`my_appeal/appeals`），
  已有 pending 申诉只展示状态；`can.appeal` 与服务端一致。
- 中英文键：新增 `plaza.notify.*`(15)、`plaza.favorite.*`(4)、`plaza.report.*`(15)、`plaza.appeal.*`(15)、
  `plaza.quality.*`(49)、`plaza.metrics.*`(24)、`plaza.ops.*`(55)、`plaza.admin.history.rollback*`(9) 等；
  两语言键集合一致（本轮校验 4,534/4,534）。

### 18.9 错误码速查（Phase 3 增量）

`401` 未登录；`403` 非编辑/管理员（队列、抽检、指标、回滚、处理动作）；`404` 公开题/投稿/反馈记录/申诉记录不存在或非本人；
`409` 收藏不可用、反馈目标不可反馈、申诉状态不允许、重复处理、抽检对象状态不符、回滚前置条件失败；`422` 未知筛选/
字段/原因、驳回原因缺失、抽检 findings 缺失、通知已读参数缺失、回滚原因缺失；`415`/`503` 同 §16。

## 19. AI 用量与配额（Phase 3，`app/ai/usage.py`）

> 定位：方案 §7「AI 结果可追踪」——记录模型、版本、实际入参类型、耗时、重试和费用，按任务类型设置每人配额与全站并发。
> 实现 `app/ai/usage.py`（账本 + 配额 + 4 条接口），生命周期钩子在 `app/ai/jobs.py`，`app/main.py` 只加
> `include_router(ai.usage_router)` 与启动 `init_usage()`（在 `init_ai()` 内、`reap_stale_jobs()` 之前建表）。
> 与既有 `ai_usage` 的分工：`ai_usage` 仍是**逐次模型调用**的 token 账本（设置页沿用），`ai_job_usage` 是
> **每任务一行、终态写一次**的追踪账本；两表以 `job_id` 关联，互不覆盖。

### 19.1 数据表（应用库，幂等建表）

| 表 | 关键列/约束 | 说明 |
|---|---|---|
| `ai_job_usage` | `id`、`user_id`、`job_id`（**UNIQUE**，`idx_ai_job_usage_job`）、`kind`、`provider_id`、`model`、`status`、`duration_ms`、`retries`、`tokens_in/out`、`tokens_estimate`、`cost_micro`、`created_at`、`meta_json` | 每个任务终态一行；按 `(user_id,created_at)`、`(kind,created_at)` 建索引；`meta_json` 只放分类/计数/版本（`input_kind`、`models`、`cross_verify`、`structured_verify`、`peer_review`、`source_mode`、`explain_mode`、`modes_count`、`document_count`、`requirements_present`、`retries`、`prompt_version`），**不记录题干/资料正文/文件名等用户内容** |
| `ai_quotas` | `kind PRIMARY KEY`、`daily_limit`、`concurrency_limit`、`updated_by`、`updated_at` | 管理员按任务类型的覆盖值；`daily_limit/concurrency_limit=0` 表示不限制 |

`ai_jobs` 经 `db.ensure_column` 幂等补列：`started_at TEXT`、`retry_count INTEGER NOT NULL DEFAULT 0`
（老库不重建表；耗时优先 `started_at → finished_at`，未开始执行的从创建时间起算）。

### 19.2 任务生命周期入账（`app/ai/jobs.py` 钩子）

- 创建任务：`ai_usage.check_new_job(con, user_id, kind)` 在 **INSERT 之前、同一事务**校验配额与并发，超限抛
  **429** `{code:'ai_quota_exceeded', kind, message}`——请求不落库、不消耗配额；通过后 `note_job_active(job_id, kind)`
  登记进程内活跃任务（数据库计数异常时的兜底）。
- 终态：成功/失败/取消/中断（含启动回收 `reap_stale_jobs` 的 `interrupted`）、取消落定、SSE 结束、任务更新为终态等路径
  均调用 `record_job_usage(job_id)`；账本按 `job_id` 唯一**幂等更新**，入账失败只记日志、绝不影响任务状态机/SSE/产物。
- 续跑/重新执行：`check_resume` 只校验同类型其他在途任务的并发（本任务不占并发）；`clear_job_usage(job_id)` 在重新派发前
  删除旧终态行，下一次终态重建唯一行。
- token 汇总：从 `ai_usage` 按 `job_id` 汇总调用次数/输入/输出 token；本地模型不回传 usage 时用「长度粗估」并在响应
  标 `tokens_estimate:true`。费用：仅当 `ai_models.input_price/output_price` 都已配置才计算
  `cost_micro = tokens_in×input_price + tokens_out×output_price`（单价口径元/百万 tokens），任一缺失返回 `null`，
  不按 0 元编造。
- 保留策略：`init_usage()` 启动时按 `STUDY_AI_USAGE_RETENTION_DAYS`（默认 180 天，≤0 永久）清理过期账本；
  清理失败只告警。

### 19.3 配额与全站并发

- 优先级：管理员 `ai_quotas`（db） → 环境变量 `STUDY_AI_QUOTA_<KIND>` / `STUDY_AI_CONCURRENCY_<KIND>` → 内置默认
  （`DEFAULT_DAILY_QUOTAS`/`DEFAULT_CONCURRENCY`，覆盖 explain/grade/question-analysis/notes/recognize/tagging/ocr/
  course/course-narration/course-video/paper-import/paper-enrich/paper-compose/paper-revision/paper-annotate/
  assistant-turn/insight-report 共 17 类） → 未配置（不阻断，只记警告）。
- 每人每日配额：按 **UTC 自然日**统计 `ai_jobs` 中 `status NOT IN ('failed','cancelled','interrupted')` 的任务数；
  失败/取消**不消耗**配额；`0` 表示不限制。
- 全站并发：按 `kind` 统计 `ai_jobs` 中 `status ∈ ('queued','running','cancel_requested','awaiting_outline')` 的任务数
  （数据库为跨进程权威来源，异常时退化为进程内注册表）；超限 **429** 可读提示（与配额同一错误结构）。
- 管理接口与普通接口的 429 均发生在写库前，因此「被拒绝的请求不消耗配额」也适用于并发拒绝。

### 19.4 查询/管理接口（4 条）

| 方法 | 路径 | 鉴权 | 说明 |
|---|---|---|---|
| GET | `/api/ai/usage/me` | `current_user` | 本人用量：`?days=1-365（默认 14）` → `{days,generated_at,today{...},by_day[],by_kind[],quota{date,timezone:'UTC',kinds[],notice},recent[≤10],truncated,notice}`；`quota.kinds[]` 为每类 `{kind,label,daily_limit,daily_source,concurrency_limit,concurrency_source,used_today,running,remaining_today,limit_reached}`；只统计本人 |
| GET | `/api/admin/ai/usage` | `require_admin` | 全站汇总：`?days=1-365（默认 30）&kind&user_id&page` → `{days,generated_at,filters,totals,by_kind[],by_model[],by_day[],page{page,page_size:20,total,has_more,items[]},truncated,page_size_limit:10000,notice}`；`totals` 含 `jobs/succeeded/failed/cancelled/failure_rate/duration_p50_ms/duration_p95_ms/duration_avg_ms/tokens_in/tokens_out/retries/cost_micro/cost_covered_jobs/cost_unpriced_jobs` |
| GET | `/api/admin/ai/quotas` | `require_admin` | 配额视图：`{generated_at,date,timezone:'UTC',defaults{daily,concurrency,retention_days,daily_env_prefix,concurrency_env_prefix},overrides[],items[],notice}`；`items` 为每类有效配额与来源、今日全站消耗与当前并发 |
| POST | `/api/admin/ai/quotas` | `require_admin` | 调整：`{kind, daily_limit?(0-100000), concurrency_limit?(0-1000), reset?:bool}`；未知任务类型 **422** `{code:'unknown_task_kind',message}`；未给限制且非 reset **422**；`reset:true` 删除覆盖恢复默认；成功返回 `{kind,reset,daily_limit,daily_source,concurrency_limit,concurrency_source,item}` |

### 19.5 前端展示

- 任务中心 `web/js/pages/tasks.js`：`loadUsage()` 调 `/api/ai/usage/me?days=1` 显示今日用量面板（今日任务数/tokens/失败数/
  费用估算 + 各类配额剩余与 `limit_reached` 警示），接口未就绪整块隐藏；任务列表/详情显示模型、耗时、重试次数、tokens
  与费用估算（无单价显示「未知」）。
- 管理台 `web/js/pages/admin.js`：新增「AI 用量/配额」区——近 14 天汇总（任务数/失败率/P50/P95/tokens/费用）、
  按类型分布表、配额表（每日/并发输入 + 保存/恢复默认，来源标签 db/env/default），接口失败降级提示。
- 设置页 `web/js/pages/settings.js`：保留既有 `/api/ai/usage?days=30` 逐次调用账本视图，并增加
  `/api/ai/usage/me?days=1` 的今日配额条（`used_today/remaining_today`，只显示有消耗的类型；`quota.date` 为 UTC 自然日）。
- 中英文键：`tasks.usage.*`(20)、`admin.aiUsage.*`(43)、`ai.usage.*`(7) 等，两语言键集合一致。

### 19.6 错误码与边界

- **429**：每日配额用完/全站并发已达上限（`detail:{code:'ai_quota_exceeded',kind,message}`）；失败/取消不计数。
- **422**：配额调整参数非法/未知任务类型；**403/401**：非管理员调用 `/api/admin/ai/*`、未登录调用 `/api/ai/usage/me`。
- 边界：`ai_job_usage` 记录的是任务终态快照，不替代 `ai_usage` 的逐次调用明细；费用为估算（不扣费）；单次汇总查询最多
  10000 行（`truncated` 标记）；老库 `ai_jobs` 无 `started_at/retry_count` 时由 `ensure_column` 补齐，历史任务耗时可能为 0。

## 20. 平台精选接入助手 / 课堂（Phase 4 收尾）

> 定位：方案 §8 阶段 3「发布精选接入检索、练习、错题、工坊、**助手和课堂**」中最后两个面（第三轮只做了
> 检索/练习/错题/工坊）。实现：助手工具 `/api/assistant/*`（`app/assistant.py`）、课堂生成
> `POST /api/classroom/courses/{id}/generate`（`app/classroom/{api,generate,prompts}.py`），读取统一走
> `app/official.py`（查询时可见性）。**本组不新增 HTTP 路由**：助手新增工具、课堂新增参数、学情/统计新增字段。

### 20.1 AI 助手接入 official（`app/assistant.py`）

- 工具白名单 **12 → 13**：新增只读工具 `search_official`；`read_question` 扩展接受 `official:<公开编号>`。
  写操作工具（`create_explain_job`/`create_revision_paper`/`compose_paper`）不变且仍需用户二次确认。
- `search_official` 参数 `{keyword?, subject?, grade?, qtype?, limit?1-10(默认5)}`，至少给一个检索条件
  （否则工具错误「请至少提供 keyword / subject / grade / qtype 之一后再检索平台精选」）；检索走
  `official.search_official`（可见性查询时判定，撤回/争议不出现）。返回 `items[]`：
  `{qid, source:'official', source_label:'平台精选', public_id, review_status, review_status_label, status_label,
  published_version, featured, community_pending, subject, grade, type, stem(≤160), has_answer, knowledge_tags(≤5)}`；
  `refs[]` 带 `qid/source/public_id/review_status`；`total` 与 `grade_filter` 同返回；summary
  「平台精选命中 N 道（匹配总数 X，其中平台精选 Y 道；引用时写公开编号）」。**不输出内部 ID/投稿人/审核员**。
- `read_question` 对 `official:` 的行为：
  - 格式不正确（如 `official:`/`official:??`）→ 工具错误「平台精选引用格式不正确：应为 official:<公开编号>
    （如 official:plz-xxxx）」（不是 500）；
  - 读取走 `practice.load_question`/`official.load_official_question`；撤回/争议/不可见时用
    `official.inspect_official` 组织中文提示（已撤回/有争议/当前不可见，均注明历史作答、错题与成绩记录保留），
    **绝不回显题面/选项/答案**；
  - 作答门控沿用「本人已作答才返回答案与解析」；结果含 `source=official/public_id/official_status/review_status(_label)/
    published_version/source_declaration`，并在 `next_steps` 提示「回答中引用时写『平台精选 <公开编号>』，
    不要提任何内部 ID、投稿人或审核员信息」。
- 系统提示（`_system_prompt`）新增第 5 条规则：平台精选题只引用工具返回的公开信息；引用必须写
  「平台精选 · 公开编号 plz-...」并说明审核状态（社区待核验/已核验/精选）；绝不透露或猜测内部 ID、投稿人、
  审核员与后台字段；已撤回/有争议的题目不得复述题面、答案或解析。`GET /api/assistant/tools` 的 `scope_note`
  明确「只读高考题库 + 当前公开可见的平台精选」。
- `get_stats` 工具结果新增 `official`（与 `/api/stats` 同源）；`available` 时 summary 追加
  「平台精选已练 X 道（可练 Y 道）、精选错题 Z 道」；既有统计字段语义不变。

### 20.2 课堂以精选题为素材（`POST /api/classroom/courses/{id}/generate`）

- 请求新增可选 `source_ref`（`max_length=160`）：只接受 `official:<公开编号>`（`SEED_QUESTION_SOURCES=("official",)`）；
  格式非法/来源不支持 **422**「课程素材引用只支持平台精选题，格式为 official:<公开编号>（如 official:plz-xxxx）」；
  超 160 字 **422**。
- 可见性：`validate_course_job()` 在「`stage ∈ outline|all` 且课程尚无大纲」时校验一次（任务准入），
  `run_course_job()` 生成大纲时再按查询时可见性读取一次；未发布/撤回/争议 **404**（任务失败原因可读，不落 500），
  撤回题提示「该平台精选题已撤回，不能再作为新课程素材；历史课程保留当时的来源标记」，争议题提示暂停展示。
  已有大纲的续跑/单场景重生成**不重新引用素材**，因此题目后续撤回不影响既有课程与导出。
- 大纲 prompt：`generate.format_seed_question_material()` 输出「标题（平台精选·公开编号）/审核状态/学科·学段/
  知识点/题面/选项/参考答案（仅供备课设计，不要直接写进学生题目文字）/解析要点（仅供备课设计）/来源声明/
  公开提示」；`prompts._official_seed_block()` 上限 `OFFICIAL_SEED_PROMPT_MAX_CHARS=4000` 并规定引用写法、
  禁止编造公开编号/来源声明/题目没有的数据、学生题面先作答再揭晓、不输出内部 ID/投稿人/审核员。
- `outline.sources` 追加条目（`generate.official_source_entry()`）：
  `{index, kind:'official', title, url:'', site:'平台精选', date:'', summary, question_ref:'official:<public_id>',
  public_id, published_version, review_status, review_status_label, subject, grade, source_declaration}`；
  `outline_sources()` 统一清洗 web/official 两类来源（缺失/损坏返回空列表，不抛错）；
  `course.config.seed_source` 记录 `{source_ref, public_id, published_version, review_status(_label), title}`。
- 任务结果：`_web_search_result_fields()` 统一追加 `official_source_count`/`official_sources[]`
  （逐条 `source_ref/public_id/published_version/review_status(_label)/title/summary/source_declaration`），
  同时保留既有 `web_search/source_count/web_source_count/web_search_skipped`；完成阶段 summary 追加
  「课程素材来源：平台精选 plz-xxx（vN）」。
- 导出/回看：既有 `outline.sources` 链路（PPTX 末页 / 离线 HTML / ZIP `sources.json` / 教学资源包 README /
  讲义打印）保留 official 来源条目；`GET /courses/{id}` 的 `course.config.seed_source` 可回看建课来源。
- 错误码速查：**422** 引用格式/来源不支持/超长；**404** 题不存在或当前不可见（撤回/争议/未发布）；
  **401** 未登录；**409** 课程有运行中生成任务（既有语义）；**403** 课程归属他人（既有语义）。

### 20.3 official 学情口径与学段筛选（`/api/insight/home`、`/api/stats`、`GET /api/questions?grade=`）

- `GET /api/insight/home`（`app/home_stats.build_home`，路由在 `app/insight.py`）：
  `{generated_at, range, bank, official, personal, caliber}`；`official` 为 `app/official.official_caliber()` 原样下发
  （`available/total_questions/practice_pool/auto_gradable/attempts/practiced/wrong_count/due_reviews/caliber/note`）；
  `bank` 块在 official 可用时追加 `official_practice_pool`/`official_auto_gradable`；`caliber.official` 为中文口径说明。
  既有字段语义不变。
- `GET /api/stats`（`app/practice.stats`）：新增嵌套 `official`（同一 caliber）；平铺
  `official_attempts/official_practiced/official_wrong_count` 语义不变（按 `qid LIKE 'official:%'`，含已撤回题历史）。
  精选口径失败降级 `available:false` + note，不影响整体统计。
- `GET /api/questions?grade=` 与 `GET /api/catalog` 的 `grades/grade_stages/grade_note`：见 §17.2.1（safe-degrade、
  `grade_filter` 元数据、`source=all` 合并分页不变、r5 题库无 grade 列时 bank 侧不生效）。
- 错误码：精选/学段口径均**不新增错误码**；未知学段值安全降级（不 422），`source`/`type` 等既有参数保持原语义。

## 21. 运维监控（Phase 4，`GET /api/admin/monitor`）

> 定位：方案 §7「分发与运维」——监控作业失败、数据库等待、OCR 延迟、渲染队列、违规举报与撤回传播，并治理
> 构建/镜像分发。实现 [`app/monitor.py`](app/monitor.py)（只读聚合、短缓存、单检查降级）；`app/main.py`
> 只加 `include_router(monitor.router)` 与构建指纹范围，无新任务类型。

### 21.1 接口与鉴权

| 方法 | 路径 | 鉴权 | 说明 |
|---|---|---|---|
| GET | `/api/admin/monitor` | 管理员会话 **或** `X-Health-Token` 部署探针口令（只读） | `?refresh=1` 绕过进程内短缓存；返回五组只读检查（见 21.2）；非管理员 **403**「需要管理员权限」、未登录 **401**「请先登录」 |

- 探针口令：`STUDY_HEALTH_TOKEN` 环境变量 + 请求头 `X-Health-Token`，复用 `app.main._health_token_ok` 常量时间比较
  （与 `/api/health/ready` 同一口令，不新增密钥体系）；命中时 `monitor_access` 返回 `{role:'probe',username:'health-probe',
  via:'health_token'}`，只读访问监控。
- 管理台入口：`web/js/pages/admin.js` 的「运维监控」区（§23.6）。

### 21.2 响应结构与五组检查

`GET /api/admin/monitor` → 

```json
{
  "ok": true, "status": "ok", "generated_at": "ISO-8601 UTC", "duration_ms": 12.3,
  "build": {"build_id": "...", "version": "0.1.0", "bank_version": "bank-20261121-r5"},
  "window_days": 7,
  "thresholds": {"queue_warn_seconds":120,"queue_error_seconds":900,"failure_warn_rate":0.25,
                 "job_timeout_seconds":1800,"lease_seconds":90,"ocr_p95_warn_ms":120000,
                 "feedback_warn_pending":20,"feedback_warn_age_seconds":259200,
                 "backup_warn_age_seconds":172800,"wal_warn_bytes":67108864,"db_wait_warn_ms":500},
  "degraded": [], "checks": {"tasks": {...}, "ai": {...}, "media": {...}, "data": {...}, "plaza": {...}},
  "cache": {"ttl_seconds": 15.0, "cached": false, "age_seconds": 0.0}
}
```

| 分组 | 返回字段（`status/detail/alerts` 之外） | 告警/错误口径 |
|---|---|---|
| `tasks` | `window_days/timeout_seconds/lease_seconds/oldest_queued_seconds/oldest_queued_kind/timed_out/stale_running/reclaimed/failure_rate/success_rate/totals{active+terminal 各状态}/by_kind[≤20]/truncated`；`by_kind[]` 每项 `{kind,label,queued,running,cancel_requested,awaiting_outline,completed,failed,interrupted,cancelled,oldest_queued_seconds,timed_out,stale_running,failure_rate}` | 无 `ai_jobs` 表 → warn「AI 任务模块尚未初始化」；终态 ≥4 且失败率 ≥25% → warn；最老排队 ≥120s → warn / ≥900s → error；执行超时/心跳超期 → warn |
| `ai` | `window_days/source('ai_job_usage'\|'ai_jobs')/totals{jobs,completed,failed,cancelled,failure_rate,duration_p50_ms,duration_p95_ms,duration_avg_ms,tokens_in,tokens_out,tokens_total,retries,cost_micro,cost_covered_jobs,cost_unpriced_jobs}/by_kind[≤20]/truncated/ocr{...同上聚合+kinds=['ocr','recognize']}/model_calls` | 账本不可读 → 退回任务表粗算并 warn；失败率 ≥25%（≥4）→ warn；OCR P95 ≥120s 或失败率 ≥30%（≥3）→ warn；费用无单价 → `cost_micro:null`（不伪造 0） |
| `media` | `window_days/tasks=['course-video','course-narration']/totals/failure_rate/oldest_queued_seconds/timed_out/stale_running/by_kind[≤20]/assets{total,recent,by_kind}/courses{...}/scenes{...}/capabilities{render,ffmpeg,pdf,video}{available,detail}` | 失败率 ≥34%（≥2）→ warn；排队 ≥120s / 超时 / 心跳超期 → warn；有渲染任务但视频能力不可用 → warn |
| `data` | `engine/database{engine,readable,wait_ms,table_count,size_bytes,wal_bytes,shm_bytes,database}/bank{present,readable,size_bytes,version,questions,expected_version,version_matches}/backups{count,latest|null,latest_age_seconds,warn_age_seconds,detail}`；`latest` 含 `{name,size_bytes,modified_at,age_seconds,manifest_ok,created_at,file_count,bank_version,format_version,key_included}` | 题库版本不匹配 → **error**；题库缺失/不可读 → warn；WAL ≥64MB → warn；库等待 ≥500ms → warn；备份清单不可解析或过期 → warn |
| `plaza` | `initialized/releases{active,withdrawn,withdrawn_recent}/withdrawn{public_statuses,sample_size,checked,leaked,leaked_items[≤20],orphan_active,orphan_active_items[≤20],published_without_active,recent_withdrawn}/feedback{issues,reports,appeals,total_pending,oldest_pending_seconds}` | 广场表未初始化 → ok（提示）；撤回后的发布仍出现在公开读 → **error**；活跃发布对应投稿不在公开状态 → **error**；已发布但无活跃发布指针 → warn；未处理反馈 ≥20 或最老 ≥72h → warn |

### 21.3 只读、降级与缓存语义

- **只读**：只执行 `SELECT`/只读探测（题库以 `mode=ro` 打开、备份只读 ZIP `manifest.json` 且 >2MB 跳过），
  不写业务表、不触发广场/AI 业务函数；不输出密钥、绝对宿主路径或用户内容（测试覆盖）。
- **单检查失败不 500**：`_run_check()` 每检查独立连接 + 独立异常边界；失败只把该组标 `status='error'` 并给中文
  `detail`/`alerts`，其它组照常返回；整体 `ok = (status != 'error')`，`status` 取五组最严重值；
  `degraded` 列出非 ok 组名。检查函数未注册/返回非 dict 也按 error 呈现。
- **短缓存**：进程内缓存 `STUDY_MONITOR_CACHE_SECONDS`（默认 15s，0 关闭，最大 300s）；
  缓存键 `(db.DATA_DIR, postgres_enabled(), ai_window_days())`；命中时 `cache.cached=true` + `age_seconds`；
  `refresh=1` 或 `reset_cache()` 绕过；按进程隔离（多 Worker 各自缓存）。
- 阈值环境变量：`STUDY_MONITOR_AI_WINDOW_DAYS`（7，1-365）、`STUDY_MONITOR_JOB_TIMEOUT_SECONDS`
  （回退 `STUDY_WORKER_JOB_TIMEOUT`，1800）、`STUDY_MONITOR_LEASE_SECONDS`（回退 `AI_JOB_LEASE_SECONDS`，90）、
  `STUDY_MONITOR_WAL_WARN_MB`（64）、`STUDY_MONITOR_BACKUP_WARN_HOURS`（48）、
  `STUDY_MONITOR_FEEDBACK_WARN_PENDING`（20）；查询行上限 5000、撤回抽样 200、单组 by_kind 输出 ≤20。
- SQLite/PostgreSQL 兼容：只用 `db.connection()/db.table_names()` 封装，不使用 SQLite 专有函数；时间聚合在 Python 侧做；
  广场模块不可用时用本地兜底常量（不反向依赖广场业务代码）。

### 21.4 构建指纹与分发治理

- `app/main.py::_compute_build_id()` → `BUILD_FINGERPRINT_PATTERNS`：
  `app/**/*.py`、`app/migrations/**/*.sql`、`app/ai/prompts/**/*.yaml`、`web/js/**/*.js`、`web/locales/*.json`、
  `web/styles*.css`、`web/index.html`、`web/editor/**/*`、`web/vendor/**/*`、`editor/src/**/*`、`editor/vendor/**/*`；
  跳过 `__pycache__/node_modules/.git`；按相对路径排序聚合 sha256 取前 12 位，内容相同则稳定；
  `STUDY_BUILD_ID` 显式覆盖（容器镜像写入提交号）。随响应头 `X-Zhixu-Build`、`/api/health`（`build_id`/`version_label`）、
  就绪信息与 OpenAPI 下发；`X-Zhixu-Bank` 下发题库版本提示。
- `.dockerignore`（新增）：排除 `data/`、`backups/`、`*.sqlite3*`、`api-key.key`、`.env*`（保留 `.env.example`）、
  `.git/`、`.pytest_cache/`、`.ruff_cache/`、`.mypy_cache/`、`__pycache__/`、`*.pyc`、`node_modules/`、
  `editor/node_modules/`、`screenshots/`、`*.log`、`accept-*.json`、`accept-*.log`、`pytest-*.log`、`*.zip`、
  `.omx/`、`.playwright-cli/`、`eval/runs/`、`editor/dist/`；显式保留 `web/**`（含 `web/editor`、`web/vendor`、
  `web/locales`）、`editor/vendor/**`、`editor/src/**`、`app/migrations/**`。
- `deploy/Dockerfile`：由 `.dockerignore` 收敛构建上下文；`COPY app/web/eval/deploy/scripts` + `THIRD_PARTY_NOTICES.md`
  + `editor/vendor`；保留 libgomp1/ffmpeg 与 `/api/health/live` HEALTHCHECK；非 root 用户 `zhixu`。

### 21.5 管理台「运维监控」区（`web/js/pages/admin.js`）

- 分组卡片 `tasks/ai/media/data/plaza`：状态徽标（ok/warn/error 三色）、`build.build_id/version/bank_version`、
  `generated_at`/采集耗时/缓存命中（`cache.cached/age_seconds`）、`degraded` 组提示；
  任务按 kind 表格、AI 总量与 OCR 指标（P50/P95/失败率/费用未知）、渲染队列与导出能力、应用库/题库/备份、
  广场发布/撤回/反馈积压；「刷新」请求 `?refresh=1`；接口失败/未就绪显示降级提示（不崩页）。
- 中英文键 `admin.monitor.*`（分组/状态/表格列/时长与字节格式化/缓存与降级提示），两语言键集合一致。

### 21.6 错误码与边界

- `401` 未登录且无探针口令；`403` 非管理员（无探针口令）；没有 422/409（`refresh` 为布尔型宽松解析）。
- 边界：不持久化、无告警外发（邮件/IM/Webhook）、无历史趋势；撤回传播为最近 200 条撤回发布的抽样断言（非全量扫描）；
  PG 下数据库大小/等待为增强信息（失败置 null）；真实 PostgreSQL/Redis/多 Worker 环境未联验；`X-Health-Token`
  只读监控，不替代 `/api/health/ready` 的就绪判定。

## 22. 质量运营闭环：门禁 / 恢复 / 批量 / 通知（Phase 4，`/api/plaza` 51 条）

> 定位：方案 §8 阶段 3「试点首批 100% 独立抽检，发现严重错题或隐私问题停增并纠正」「上线抽检、通知及回滚」。
> 实现 [`app/plaza/gate.py`](app/plaza/gate.py) + 扩展
> [`quality.py`](app/plaza/quality.py)、[`release.py`](app/plaza/release.py)、
> [`feedback.py`](app/plaza/feedback.py)、[`notify.py`](app/plaza/notify.py)、[`metrics.py`](app/plaza/metrics.py)、
> [`db.py`](app/plaza/db.py)；`/api/plaza` **44 → 51 条**，新增 2 张表（广场表共 16 张）。不改变既有广场状态机、
> 评审阈值与撤回/回滚语义（只追加/接线）。

### 22.1 第四轮新增路由（7 条）

| 分组 | 方法 | 路径 | 鉴权 | 说明 |
|---|---|---|---|---|
| 通知增强 | GET | `/api/plaza/notifications/unread-count` | 登录 | 轻量未读汇总：`{unread_count, by_kind, kinds_total, message}`；只本人 |
| 抽检门禁（2） | GET | `/api/plaza/admin/quality/gate` | 编辑/管理员 | 门禁状态：配置（mode/mode_label/mode_source/armed/armed_source/enforced/required/blocking_possible/window_open/pilot_limit/published_total/pilot_remaining/checks_total）、审计计数（`counts{blocked,override,passed}` + override_total/blocked_total/passed_total/evaluated_total/by_mode）、`recent[≤10]`、`notice` |
| | PUT | `/api/plaza/admin/quality/gate` | **仅管理员** | `{mode?:'off'\|'initial_100'\|'always', armed?:bool, pilot_limit?:0-1000000, reset?:bool}`；非法模式/负窗口 **422**；`reset` 删除设置恢复默认；返回最新状态 |
| 一键恢复 | POST | `/api/plaza/releases/{public_id}/restore` | 编辑/管理员（作者回避 **403**） | `{reason(2–2000), confirm_resolved:true}`；恢复最近一次「抽检 high 自动撤回/编辑撤回」的发布（详见 §22.4） |
| 批量处理（3） | POST | `/api/plaza/admin/issues/batch` | 编辑/管理员 | 批量处理纠错（详见 §22.5） |
| | POST | `/api/plaza/admin/reports/batch` | 编辑/管理员 | 批量处理举报 |
| | POST | `/api/plaza/admin/appeals/batch` | 编辑/管理员 | 批量处理作者申诉 |

> 同时扩展（非新增路径）：`POST /api/plaza/notifications/read` 增加 `kind` 过滤；`POST /api/plaza/submissions/{id}/publish`
> 增加 `quality_gate_override`/`override_reason` 并接门禁；`POST /api/plaza/admin/quality/checks` 增加签发前抽检
> （`phase` 由状态推导，响应回显）；`GET /api/plaza/admin/metrics` 增字段。

### 22.2 抽检门禁（`app/plaza/gate.py` + `plaza_quality_gate_log`）

- 模式 `off|initial_100|always`（默认 `initial_100`）；解析优先级 **管理员运行期设置**（`app_settings.plaza_quality_gate`）>
  **环境变量** `STUDY_PLAZA_QUALITY_GATE` > 内置默认；未知模式回退默认并在 `mode_source` 标 `:invalid`。
  - `off`：不阻断（抽检记录保留）；
  - `initial_100`：试点窗口内（按成功签发数 `pilot_limit`，默认 100，`0` 不限）每个版本都要独立 `pass`；
    窗口结束后不再阻断；
  - `always`：所有发布版本都要独立 `pass`。
- `armed`（试点激活）：显式配置了模式（设置/环境变量且非 off）或库中已有任意抽检记录 → 已激活；也可
  `plaza_quality_gate_armed`/`STUDY_PLAZA_QUALITY_GATE_ARMED` 显式 true/false（`armed_source=setting|env|auto`）；
  未激活时签发不阻断（提示「抽检门禁尚未激活」）。
- 判定：该版本**最新一条**抽检记录为 `pass` 且抽检员 ≠ 作者（`_independent_pass`）→ 允许签发；否则 `blocking`。
- 签发接线（`publish_submission`，在状态/授权/阈值校验通过后）：阻断时抛 `QualityGateBlocked` → 签发事务回滚 →
  `record_blocked()` 用**独立事务**写 `plaza_quality_gate_log`（`outcome='blocked'`，审计失败不掩盖 409）→
  **409** `{code:'quality_gate_blocked', message, submission_id, version, mode, mode_label, guidance,
  override_allowed:true, latest_check, gate:{mode,armed,enforced,required,window_open,pilot_limit,published_total}}`。
- override：`quality_gate_override=true` 且 `override_reason` ≥2 字（否则 **422** 对象带 `field:'override_reason'` +
  guidance）；同事务写 `quality_gate_override` 事件（mode/version/latest_check/guidance）与审计行 `outcome='override'`；
  正常通过写 `outcome='passed'`；签发事件 payload 追加
  `quality_gate{mode,armed,required,override,override_reason,pass_check_id}`；响应 `quality_gate{...}` 回显模式/激活/
  窗口/override/最新抽检/guidance/message。
- 审计表 `plaza_quality_gate_log(id, submission_id, version, mode, outcome, actor_id, reason, created_at)`，
  `GATE_OUTCOMES=('blocked','override','passed')`；`outcome` 与 `mode` 建索引。
- 设置键：`plaza_quality_gate`/`plaza_quality_gate_armed`/`plaza_quality_gate_limit`（管理员接口运行时写入，`reset` 删除）。

### 22.3 签发前抽检（`app/plaza/quality.py`）

- `POST /api/plaza/admin/quality/checks` 兼容两阶段：`post_publish`（`status='published'` + 活跃发布，既有口径）与
  `pre_publish`（`status ∈ ('verified','pending_release')` 且无活跃发布，社区核验通过待签发）。
  `community_review` 仍 **409**（保持既有语义）；其它状态 **409** 对象（`status/status_label`）。
- 请求 `{submission_id?|public_id?, decision:'pass'|'issue', severity?:low|medium|high, findings?:[≤20，每条≤500],
  note≤2000}`；`public_id` 与 `submission_id` 不匹配 **422**；作者抽检本人投稿 **403**；`issue` 必填 findings（否则 **422**）。
- 发布后 `high` 自动撤回逻辑不变（同事务 `withdraw_active_release(mode='emergency',
  event_action='quality_withdrawn')`：发布指针/投稿状态置 withdrawn、写事件、通知作者与收藏者、抽检行
  `auto_withdrawn=1`，响应 `auto_withdrawn:true` + `withdraw` + `notified{author,favoriters}`）；**签发前抽检的 high
  不撤回**（无发布），只记录并通知作者修订。
- 响应追加 `phase`、`gate{mode,mode_label,mode_source,armed,enforced,required,blocking,publish_ready,guidance,
  override_guidance}`、`message`（中文按结论/阶段说明）；`publish_ready` 仅在签发前 `pass` 且未阻断时为 true。

### 22.4 一键恢复（`POST /api/plaza/releases/{public_id}/restore`）

- 鉴权：编辑/管理员（`require_editor`）；作者恢复本人投稿 **403**「恢复回避：不能恢复本人投稿」。
- 前置（按顺序）：发布记录不存在 **404**；无撤回记录且已有活跃发布 → **200 幂等** `{idempotent:true,
  message:'该发布已处于活跃状态，无需重复恢复'}`；无撤回记录且无活跃 → **409**；恢复对象=该 public_id **最近一次**
  撤回的发布。撤回来源必须为「抽检 high 自动撤回」（存在 `plaza_quality_checks.release_id + auto_withdrawn=1`）或
  「编辑撤回」（`withdraw_mode ∈ normal|emergency` 且有 `withdrawn_at`）；`withdraw_mode='rollback'` → **409**
  「回滚产生的撤回记录不支持一键恢复；如需恢复旧版本内容请重新送审并签发」；其它来源 **409**。
- 条件（均 **409** 带 JSON 详情）：该投稿已有更新版本的活跃发布（返回 `active_public_id/active_version/restore_version`）、
  投稿 `current_version` ≠ 要恢复的版本（`current_version/restore_version`）、投稿当前状态不是 `withdrawn`、
  历史版本快照 `plaza_submission_versions` 缺失。`confirm_resolved` 未为 true → **422**
  `{field:'confirm_resolved', guidance}`；理由 <2 字 → **422**。
- 同事务：`plaza_releases` 该行 `status='active'`（清空 `withdrawn_at/withdraw_mode/reason`，CAS `WHERE status='withdrawn'`）→
  `plaza_submissions` 恢复该版本快照/来源/指纹/授权并 `status='published'`（CAS `WHERE status='withdrawn' AND version=?`）→
  重新登记公开衍生图 → 写 `release_restored` 事件（`release_id/public_id/version`、`source='quality_auto_withdraw'|
  'editor_withdraw'`、`quality_check_id`、`previous_withdraw{mode,reason,withdrawn_at}`、`confirm_resolved:true`、figures 摘要）→
  通知作者（`release_restored`）与收藏者（`favorite_restored`，返回通知人数）。
- 响应：`{submission{status:'published',status_label,version,current_version,public_id,public_visible:true}, release,
  restored_release, restored_version, restore_source, previous_withdraw, quality_check|null, notified{author,favoriters},
  figures, public_url, idempotent:false, message}`。
- 与回滚的区别：回滚是「撤销当前发布并恢复上一已发布版本」（`/submissions/{id}/rollback`，版本回退）；
  一键恢复是「把被撤回的同版本发布重新设为 active」（不改变版本号，仅用于抽检/编辑撤回后问题已处理）。
  两者都只改发布指针/投稿状态，公开读（广场列表/详情/公开图、`official:` 检索/详情/作答）**查询时即时**生效；
  历史作答/成绩/收藏保留。

### 22.5 批量处理（`POST /api/plaza/admin/{issues|reports|appeals}/batch`）

- 请求 `{ids:[1-50], action:'resolve'|'reject', reason?≤2000}`：ids 去重、非正数忽略；空 ids **422**
  `{field:'ids'}`；超过 `MAX_BATCH_ITEMS=50` **422** `{message,max_items:50}`；批量驳回 reason <2 字 **422**
  「批量驳回必须填写原因（便于反馈人/作者理解）」。
- 处理：同一写事务（`plaza-write`）内逐条 `_apply_feedback`（状态 resolve→resolved / reject→rejected、写事件、
  通知提交人）；**部分成功**：不存在 → `skipped[{id,reason:'not_found',message}]`；已处理/非 pending →
  `skipped[{id,reason:'already_handled',status,status_label,message}]`；单条冲突（HTTPException）→
  `skipped[{id,reason:'conflict',message}]`；成功条目不会因其它条目失败被丢弃。
- 响应 `{kind,kind_label,action,target_status,requested,updated[≤50],updated_ids,updated_count,skipped,skipped_count,
  batch_id,max_items,message}`；每次批量动作写 `plaza_ops_batches` 审计行（供 `metrics.ops.batch` 聚合）。
- 鉴权：编辑/管理员（`require_plaza_role("editor")`，`admin` 直通）。

### 22.6 通知增强（`app/plaza/notify.py`）

- `GET /api/plaza/notifications/unread-count`（登录，只本人）→ `{unread_count, by_kind, kinds_total, message}`；
  `message` 为「你有 N 条未读通知」/「暂无未读通知」。
- `POST /api/plaza/notifications/read` `{all?:bool, ids?:[≤200], kind?:str}`：
  - `all:true`（可带 `kind`）：全部/某类型标记已读（`scope='all'`）；
  - `ids`：按 id 标记（他人通知被忽略不报错、不泄露存在性；`scope='ids'`）；
  - 仅 `kind`：该类型未读全部标记已读（`scope='kind'`）；
  - 未知 kind **422** `{message:'未知的通知类型', field:'kind', allowed:[...]}`；三者都缺 **422**
    `{message, field:'ids|kind|all'}`；响应 `{updated, unread_count, kind, scope, message}`。
- 去重：`notify()` 默认按「接收人 + 类型 + 投稿 + 版本」在 `STUDY_PLAZA_NOTIFY_DEDUPE_SECONDS`（默认 **300s**，
  0 关闭）窗口内去重（重复事件返回既有通知 id、不新增）；版本取 payload 的 `version/restored_version/withdrawn_version`，
  缺失时回退投稿当前版本；`dedupe=False` 强制新增；`notify_many` 仍按 user_id 去重并支持 `exclude`。
- 通知类型：`db.NOTIFICATION_KINDS` 追加 `release_restored`（20 → **21 类**）；正文只写事件与处理结论，不含他人
  身份/评审原文；通知与状态更新/事件同事务（失败一起回滚）。

### 22.7 指标新增字段（`GET /api/plaza/admin/metrics`）

- `quality.gate{mode,mode_label,mode_source,armed,armed_source,enforced,required,window_open,pilot_limit,
  pilot_remaining,published_total,checks_total,blocked_total,override_total,passed_total,evaluated_total,by_mode,notice}`；
  `quality` 追加 `pre_publish_checks/post_publish_checks`。
- `withdrawals.restorations`；顶层 `restorations{total, latest_event}`（`release_restored` 事件链口径）。
- `ops.batch{batches,requested,updated_total,skipped_total,by_kind[{kind,batches,requested,updated,skipped}]}`。
- `notifications{total,unread_total,read_total}`（admin 聚合，不含用户身份）。
- `caliber` 追加 `quality.gate`/`withdrawals.restorations`/`ops`/`notifications` 中文口径说明；既有指标字段不变。

### 22.8 数据表与常量

| 表 | 关键列/约束 | 说明 |
|---|---|---|
| `plaza_quality_gate_log` | `id`、`submission_id`、`version`、`mode`、`outcome`、`actor_id`、`reason`、`created_at`；索引 `(submission_id,id)`、`(outcome,id)` | 门禁判定审计（blocked/override/passed）；阻断记录独立事务写入（签发事务已回滚） |
| `plaza_ops_batches` | `id`、`kind`、`action`、`requested`、`updated`、`skipped`、`actor_id`、`reason`、`created_at`；索引 `(kind,id)` | 批量处理审计，供运营指标聚合 |

- 新增常量：`QUALITY_GATE_MODES=('off','initial_100','always')`、`QUALITY_GATE_DEFAULT_MODE='initial_100'`、
  `QUALITY_CHECK_PHASES=('post_publish','pre_publish')`、`QUALITY_PRE_PUBLISH_STATUSES=('verified','pending_release')`；
  `NOTIFICATION_KINDS += 'release_restored'`；门禁常量集中在 `app/plaza/gate.py`。
- 广场表 14 → **16 张**；无强制迁移、无数据回填。

### 22.9 错误码速查（Phase 4 增量）

- **401** 未登录（通知未读数/批量/恢复/门禁读取）；**403** 非编辑/管理员、作者回避（抽检/签发/恢复）。
- **409**：`quality_gate_blocked`（未抽检签发，含 guidance/override_allowed）；抽检对象状态不符（community_review
  或非可抽检状态）；恢复条件不满足（无撤回记录/已有更新版本活跃/current_version 不一致/状态非 withdrawn/快照缺失/
  回滚来源不可恢复）；批量单条冲突（进入 `skipped`，整体仍 200）。
- **422**：override 理由缺失/过短；门禁设置模式非法/负窗口；恢复未确认或理由过短；批量 ids 空/超 50/驳回缺原因；
  通知已读未知 kind/无选择器；抽检 public_id 与 submission_id 不匹配/findings 缺失。
- **404**：发布记录不存在；恢复的投稿不存在。

## 23. 第四轮前端收口（页面契约）

> 对应任务 WS29；均为既有页面的能力补齐/接线，不新增中央路由（沿用 `main.js` 既有页面键与 hash 参数约定）。

### 23.1 题目详情（`web/js/pages/question.js`）

- official 公开配图：解析 official 投影 `images[]`（`{index,role,alt,accessible,url,asset,media_type,reason}`），
  仅 `accessible && url` 渲染；兼容 `figure_urls`（数组或对象，元素为字符串或 `{url}`）；撤回/争议导致公开图 404 时
  回退 `figure_notice` 中文占位。
- official 纠错：按钮打开 `data-official-issue-dialog` 对话框，字段 `stem/options/answer/explanation/figure/source/other`，
  描述 2–4000 字；提交 `POST /api/plaza/questions/{public_id}/issues`；未登录引导登录；成功 toast（响应 `message`），
  失败展示可读错误；不再只是占位提示。
- 「在广场查看」：`#/plaza?id=<public_id>`（缺失 public_id 时不渲染）；来源标签/精选版本/审核状态徽标保持。
- 题库题的收录按钮/来源徽标/答案门控保持第三轮行为。

### 23.2 练习页学段筛选（`web/js/pages/practice.js`）

- 学段筛选下拉（`grade`）进入 `state.filters` 与 URL 深链（`#/practice?...&grade=`），支持重置与清除芯片；
  选项优先 `GET /api/catalog` 的 `grade_stages`（未下发回退固定「小学/初中/高中」）。
- 能力探测：合法 `GET /api/questions?page=1&page_size=1&grade=<探测值>`，响应含 `grade_filter` 对象即支持；
  旧后端无该字段 → 控件整体隐藏并从 URL/本地状态丢弃 `grade`（不留假筛选）。
- `results.grade_filter.applied === false` 时显示 `practice.gradeFilterNotice` 中文说明（题库无年级列时仅平台精选生效）；
  来源/审核徽标、分页、空态保持第三轮行为。

### 23.3 学情 / 首页精选口径（`web/js/pages/insight.js` + `home.js`）

- 读取 `/api/insight/home` 顶层 `official`（`practice_pool/total_questions/practiced/wrong_count/note|caliber`）与
  `bank.official_practice_pool`；展示「精选可练 / 已练精选 / 精选错题」卡片 + 口径说明；字段缺失（旧后端/接口失败）
  时整块隐藏，不写死数字、不本地估算。
- 既有个人/题库统计卡与 `caliber` 说明透传逻辑不变。

### 23.4 任务中心深链（`web/js/pages/tasks.js`）

| 任务 | 深链 |
|---|---|
| `paper-import` | `#/paperlab?import=<import_id>` |
| `paper-enrich` | `#/paperlab?tab=papers&paper=<paper_id>`；无试卷 id 时 `#/mybank?edit=<question_id>` |
| `paper-compose` / `paper-revision` | `#/paperlab?tab=papers&paper=<paper_id>`（缺失回落 `#/paperlab`） |
| `course` | `#/classroom?course=<course_id>` |
| `notes` | `#/notes?note=<note_id>` |
| `ocr` | `#/notes?document=<document_id>`（缺失回落 `#/notes?section=docs`） |
| `recognize` | `#/upload?upload=<upload_id>`（既有） |
| `tagging` / `insight-report` / `assistant-turn` | `#/admin` / `#/insight` / `#/assistant`（既有） |

- 未知任务不跳转、只提示（`tasks.noRouteToast`）；深链参数与对应页面读取约定一致（paperlab 读 `import`/`tab`/`paper`，
  classroom 读 `course`，notes 读 `note`/`document`/`section`）。

### 23.5 笔记与 Anki 题目引用/版本

- `web/js/pages/notes.js`：收集笔记中的站内题目引用（来源条目 `ref/qid/source_ref` + 正文统一引用文本），
  按 `bank:`/`app:`/`official:` 显示类型标签与跳转，并带版本提示
  （`version ?? published_version ?? content_revision ?? bank_version`）；无引用不渲染；笔记版本历史/回滚保持既有行为。
- `app/anki_export.py`：`question_ref_of()` 统一解析 `bank:<qid>`/`app:<id>`/`official:<id>`（复用 `app.question_ref`），
  `source_version_of()` 取 `bank_version/published_version/content_revision/source_version/version`；
  错题卡背面追加「题目引用：… · 来源：… · 来源版本：…」脚注，tag 追加 `来源_…`/`版本_…`；笔记卡背面追加题目引用
  （≤5 条，带版本）与 `source_versions`（`D<id> v<n>`）；**不改变 `.apkg` 模板字段与既有卡片结构**（无引用时输出不变）。
- 相关测试：`tests/test_phase4_ui.py`（静态契约）、`tests/test_anki_export.py`（字段级用例）。

### 23.6 管理台运维监控（`web/js/pages/admin.js`）

- 见 §21.5：五组卡片 + 状态徽标 + build/缓存/degraded + 刷新；`admin.monitor.*` 中英文键对齐；接口失败降级不崩页。

### 23.7 前端错误与降级边界

- 所有新增控件（学段筛选、精选口径卡、监控卡、纠错对话框、深链）在接口未就绪/权限不足/数据缺失时**隐藏或提示**，
  不 500、不伪造数字、不把默认题库伪装成精选；locale 新增键中英对齐（本轮 4,632/4,632）。

## 24. 第五轮共用学习数据层（2026-11-21，`/api/evidence`、`/api/artifacts`、`/api/review-cards`）

> 对应《知序功能整合与优化总方案》§4/§5.1/§5.2/§5.4 与 §8 工单 A–E 首批；
> 详细记录见 `docs/知序优化实施记录-20261121-第五轮.md`。全部路由需登录（`Depends(current_user)`）；
> 错误统一 `{detail: 中文}`；私有资源他人/不存在一律 404，不泄露存在性。

### 24.1 学习证据 `/api/evidence`（`app/study_evidence.py`，3 条）

| 方法/路径 | 请求 | 响应要点 |
| --- | --- | --- |
| `GET /api/evidence/summary?days=90` | `days` 1–3650（越界夹取） | `{generated_at,window_days,effective_count,revised_count,counts:{kind,verdict,grade_source,prompt_state,reveal_state,self_report},by_source,by_question_source,caliber,note}`；`by_source` 是判分来源，`by_question_source` 是题目来源（bank/app/official/none）。 |
| `GET /api/evidence/question/{ref:path}` | `ref` = `bank:<qid>`/裸 qid/`app:<id>`/`official:<public_id>` | `{ref,question_ref,counts:{total,correct,incorrect,pending},effective:<最新有效事件>,events:[最近 20 条有效版本（含自评）],first_independent_correct,sample_sufficient,note}`；非法引用 **422**，不回显题干/答案。 |
| `POST /api/evidence/self-report` | `{ref, verdict:"correct"\|"incorrect", note?≤200}` | `{event}`：`kind=self_report`、`record_kind=self`、`self_report=independent`，**不改动**核验证据；同 `ref+verdict+note` 幂等。 |

口径：一次有效证据一行；重判通过 `revise_attempt_evidence` 同键升版（旧行 `valid=0` + `superseded_by`）；
`sample_sufficient=false` 表示独立且已核验的有效作答 <2 次，界面必须显示「样本不足」。四种题目引用之外的输入 422；
空证据返回空结构而非 404。

### 24.2 学习附件 `/api/artifacts`（`app/study_artifact.py`，9 条）

| 方法/路径 | 请求 | 响应要点 |
| --- | --- | --- |
| `GET /api/artifacts?kind=&target_kind=&target_key=&page=&page_size=` | 分页 1–100 | `{items:[公开元数据，不含 data],total,page,page_size}` |
| `GET /api/artifacts/by-target?target_kind=&target_key=` | target_kind ∈ question/note/classroom/review/assistant | `{items:[{artifact,revision,data,preview,link}],total,skipped}`；固定版本链接读旧版，`revision=NULL` 跟随最新；已删附件跳过并计入 `skipped`。 |
| `POST /api/artifacts`（201） | `{kind,title,data,preview?,source_ref?,source_kind?,artifact_id?,links?:[{target_kind,target_key,revision?}]}` | `{artifact,revision:{revision,checksum,created_at}}`；自定义 `artifact_id` 需匹配 `^[a-z0-9][a-z0-9_-]{7,63}$`，重复（含已删）**409**。 |
| `GET /api/artifacts/{artifact_id}?revision=` | 版本缺省=当前 | `{artifact,revision:{revision,data,preview,checksum,created_at,author_id}}`；他人/已删 **404**。 |
| `PATCH /api/artifacts/{artifact_id}` | `{data,preview?,title?,expected_revision}` | 内容变化→新 revision；`expected_revision` 不符 **409**「附件已被更新，请刷新后重试」；仅改标题不升版。 |
| `DELETE /api/artifacts/{artifact_id}` | — | `{deleted:true,artifact_id}` 软删；历史与链接保留在库，读取/列表/按目标均不可见。 |
| `GET /api/artifacts/{artifact_id}/revisions` | — | `{items:[{revision,checksum,created_at,author_id}],total}`（已删附件 **404**）。 |
| `POST /api/artifacts/{artifact_id}/links` | `{target_kind,target_key,revision?}` | `{link}`；同目标重链只更新 revision；单附件 ≤20 条链接（422）。 |
| `DELETE /api/artifacts/{artifact_id}/links` | body `{target_kind,target_key}` | `{deleted:true}`；DELETE 携带请求体，前端/代理需保留 body。 |

限额：data 序列化 ≤256 KiB、preview ≤64 KiB → **413**；title ≤120 → **422**；`data`/`preview` 必须是 JSON 对象。
checksum = sha256(`json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",",":"))`)。
题目详情在既有字段之外追加 `study:{evidence,artifacts,artifact_total,review_card,review_rating_scale,notice}`，
模块读取失败降级为空结构（不 500）。

### 24.3 四档复习卡 `/api/review-cards`（`app/study_review.py`，6 条）

| 方法/路径 | 请求 | 响应要点 |
| --- | --- | --- |
| `GET /api/review-cards/due?limit=50&include_future=false` | limit 1–200 | `{items:[{card_id,card_key,card_type,question_ref,qid,due_at,state,version,reps,lapses,overdue_days}],total,now,message}`；`due_at` 升序、id 稳定次序；`total` 不受 limit 截断。 |
| `GET /api/review-cards/compare` | — | `{checked,matched,mismatched:[{qid,old_due,new_due,diff_hours}],missing_cards,extra_cards,note}`：新旧到期双读对账（只读，不改数据）。 |
| `GET /api/review-cards/stats` | — | `{due_today,due_now,new,learning,review,relearning,total,archived,algorithm,algorithm_version}` |
| `POST /api/review-cards/sync` | — | `{created,updated,archived,total}`：把本人 `wrong_questions` 幂等迁成卡；已有卡不被覆盖。 |
| `GET /api/review-cards/{card_id}` | — | `{card}`；他人/不存在 404。 |
| `POST /api/review-cards/{card_id}/ratings` | `{rating:1\|2\|3\|4, card_version?, idempotency_key?}` | `{card,rating,due_at,replayed,degraded,retry_allowed,algorithm,algorithm_version,idempotency_key,note}`：rating 越界/幂等键非法 **422**；`card_version` 不符 **409**；同幂等键重放 `replayed=true`（不重复推进）；外部引擎不可用时 200 降级（到期不变、不写评分行、`retry_allowed=true`）。 |

算法口径：`fsrs-reference` / `5.0-local`（FSRS-5 公开默认权重、保留率 0.9、日粒度、间隔夹 1–36500 天）；
`STUDY_REVIEW_CLOCK` 固定服务端时钟（验收/测试）；`STUDY_REVIEW_ENGINE=ts-fsrs|external` 为外部引擎适配位
（本环境必降级，恢复后同幂等键只应用一次）。旧 `GET /api/reviews`、`GET /api/review/queue` 未切换口径。

### 24.4 OCR 块与拍题确认（增量字段，旧字段保留）

- `GET /api/ai/ocr/info` → 追加 `engine_version`、`capabilities:{blocks:true,math_recognition:false,pix2text_adapter:"reserved"}`、
  `block_schema_version:"ocr-blocks-v1"`；notice 说明公式识别未启用。
- `POST /api/ai/ocr` → 图片顶层与 PDF 每页追加 `blocks`（PDF 顶层为各页块拼接，页内 `page` 为页码），
  追加 `engine_version`/`capabilities`；旧 `text/lines/width/height/elapsed_ms/engine/version` 不变。
- `POST /api/uploads/{id}/ocr-draft` → 追加 `blocks`、`engine`、`engine_version`、`capabilities`、`block_notice`；
  `draft` 内追加 `schema_version:"intake-blocks-v1"` 与 `blocks`；OCR 不可用仍 200 + `ocr_available:false` + `blocks:[]`。
- `PATCH /api/uploads/{id}/confirm` → 请求体新增可选 `blocks`（≤200）；`kind∈{text,math,table,image}`、`text≤4000`、
  `latex≤2000`、`order 0–500`、`page 1–200`、bbox/norm 有限数值，非法 **422**；传了才覆盖、未传沿用、旧 JSON 不加空键；
  响应 `confirmed` 内含 `blocks` 与 `schema_version`。
- 块字段：`{id,kind,text,latex,needs_review,confidence,bbox{x,y,w,h},norm{x,y,w,h},page,order,rotation,source,engine,engine_version}`；
  本地引擎不做公式识别（`kind=math` → `latex=null`、`needs_review=true`），必须人工校对。

### 24.5 前端组件与静态契约

- `web/js/components/formulaInput.js`：`createFormulaInput/parseBlocksWithFormulas/buildConfirmBlocks/renderBlockLocation/attachBlockOverlay`；
  本地 KaTeX 预览、纯文本回退、`^ _ \frac \sqrt` 等工具栏、光标模板、`validate()`。
- `web/js/components/reviewRating.js`：`renderReviewRating/ratingLabel/formatDue/formatDueNote/algorithmLabel/
  ensureReviewCards/dueCards`；四档按钮 + 降级/409/重放分支；页面 `wrongbook.js` 接线。
- `web/js/components/principleDemo.js` + `web/vendor/principles/*`：`renderPrincipleDemo/renderPrinciplePoster/PRESET_DEMOS`；
  三视图（`flow_animation/isometric/demo_3d`）、键盘步进、`onFrameReady` 帧回执、无 WebGL 降级等距图并显示中文提示；
  three.js 只从 `web/vendor/interactive/three.module.min.js` 懒加载；题目页按需 import。
- `web/index.html` 引入 `styles-formula.css`、`styles-review.css`、`styles-principles.css`；
  locale 新增 `study.formula.*`（77）/`study.review.*`（33）/`study.demo.*`（43），zh/en 各 4,785 键且键集合一致。
- `scripts/integration/ui_static_accept.py` 新增第 7 步「第五轮组件与页面接线静态契约」：
  组件文件齐全、三页面接入点、播放器三视图/帧回执/WebGL 降级、本地 three.js、无 CDN、样式与关键键。

### 24.6 验收入口

- `python -X utf8 scripts/integration/round5_baseline.py`：只读基线固定与数学知识点/OCR 分层抽检样本
  （`screenshots/round5-baseline.json`、`docs/r5-数学知识点抽检-20261121.{json,md}`）。
- `python -X utf8 scripts/integration/round5_accept.py`：端到端 29 项（拍题块→作答证据→错题→四档评分→
  附件→跨页读取→M2 统一到期），报告 `screenshots/round5-accept-report.json`。
- `python -X utf8 scripts/integration/ui_static_accept.py`：前端静态契约 7 步（含第五轮/M2）。
- 专项测试：`tests/test_study_{evidence,artifact,review}.py`、`tests/test_ocr_blocks.py`、
  `tests/test_{paperlab,classroom}_study_evidence.py`、`tests/test_round5_integration.py`、
  `tests/test_review_due_unified.py`（M2）。

## 25. M2 四档复习统一到期读路径（2026-11-21，增量字段）

> 详细记录见 `docs/知序优化实施记录-20261121-第六轮.md`。**服务端唯一到期真相源 =
> `review_cards.due_at`**：四档评分更新卡后在同一事务镜像到 `wrong_questions.next_review_at`
> （旧读取方继续可用）；未评分卡跟随普通判分推进首次待复习，已评分卡只在真实四档评分后推进。
> 读接口字段只增不改，旧 `due_reviews`/`due_count`/`next_review_at` 语义保持（值已统一）。

### 25.1 统一约定

- `due_source`：`"review-cards"`（服务端复习卡口径）或 `"legacy"`（复习模块不可用时的回退）。
- 惰性迁移：任一读入口发现「有错题没有对应卡」时自动 `sync_user_cards`（幂等），旧用户首次打开
  首页/错题/复习/统计即完成迁移；无缺卡时只做两次小查询、不写库。
- 时钟：到期比较/`due` 判定使用 `study_review.now_utc()`（`STUDY_REVIEW_CLOCK` 可固定），
  复习模块不可用时回退 `db.utcnow()`；对账容差 1 秒。

### 25.2 各读接口的增量字段

| 方法/路径 | 增量 |
| --- | --- |
| `GET /api/insight/home` | 顶层 `review:{available,due_now,due_today,total,algorithm,algorithm_version,due_source,caliber,note}`；`personal.due_reviews` 保留。 |
| `GET /api/insight/overview` | 顶层 `review`（同结构 + `new/learning/review/relearning` 分布）。 |
| `GET /api/stats` | 顶层 `review` 块；`due_count` 保留且一致。 |
| `GET /api/wrongbook?due_only=` | 顶层 `due_source`/`due_note`；条目 `due_source`、`review_card_id`、`review_card_state`、`review_card_rated`；`next_review_at`/`due` 按卡到期覆盖；到期筛选使用统一时钟。 |
| `GET /api/review/queue` | 同 wrongbook；`due_source`/`due_note` 顶层。 |
| `GET /api/reviews` | 同条目字段；返回 `due_source`/`due_note`。 |
| `GET /api/review-cards/due` | 顶层 `due_source`、`note`。 |
| `GET /api/review-cards/stats` | 顶层 `due_source:"review-cards"`、`legacy_mirror:true`。 |
| `GET /api/review-cards/compare` | 语义更新：对账前幂等惰性迁移（逐条补缺卡）；不改变已有排程/评分日志；`matched==checked` 表示无漂移。 |
| `POST /api/review-cards/{id}/ratings` | 评分成功后同事务镜像旧错题到期与 `interval_days`；响应字段不变。 |

### 25.3 前端

- `web/js/pages/home.js`：首页「今日复习」优先读 `review` 块（`available && due_now != null`），
  显示「今日待复习 N 张（服务端四档排程 fsrs-reference v5.0-local）」与「去复习」；不可用时回退
  `personal.due_reviews` 并显示中文降级说明，不伪造 0。
- `web/js/pages/wrongbook.js`：统计条下补充统一口径说明；条目显示「服务端排程」/「已评分卡」徽标；
  上一轮四档评分、降级、409、幂等重放逻辑不变。
- 新增 locale 键：`study.review.unifiedNote/serverSchedule/ratedCard/homeDue/homeDueCaliber/homeUnavailable/goReview`
  （合并后 zh/en 各 4,792 键）。

### 25.4 验收

- `tests/test_review_due_unified.py`（5 项）：作答/评分/旧用户惰性迁移/两账号隔离/统一到期与对账无漂移。
- `scripts/integration/round5_accept.py` 新增 M2 三项：错题到期来源、评分后四入口统一到期、镜像后对账无漂移（29/29）。

## 26. M3 逐步提示 / 资料原文定位与摘录 / 掌握路径（2026-11-21）

> 详细记录见 `docs/知序优化实施记录-20261121-第七轮.md`。全部路由需登录；私有资源他人/不存在统一 404
> （不泄露存在性）；错误 `{detail: 中文}`。三张模块表由各自 `init_*()` 幂等建表并登记 `db.py::_ID_TABLES`。

### 26.1 逐步提示 `/api/guided-hints`（`app/guided_hint.py`，6 条）

| 方法/路径 | 请求 | 响应要点 |
| --- | --- | --- |
| `POST /api/guided-hints` | `{ref}` | 200/201 `{hint:{hint_id,question_ref,status,progress:{total,unlocked,next_position,done},source_note,created_at,updated_at},steps:[step],replayed?}`；同用户同题目幂等复用。 |
| `GET /api/guided-hints` | `?limit=20` | `{items:[hint 摘要],total,limit}`（最近更新在前）。 |
| `GET /api/guided-hints/by-ref` | `?ref=` | `{hint,steps}`；无会话 404。 |
| `GET /api/guided-hints/{hint_id}` | — | 同上；他人 404。 |
| `POST /api/guided-hints/{hint_id}/advance` | `{attempt?:str(1–2000)}` | 顶层 `{hint,steps,unlocked:{position,title,body,…}|null,requires_attempt,advanced,completed,message,attempt,replayed}`：末尾已解锁步骤有尝试才解锁下一步；无尝试返回 `requires_attempt=true` 且不解锁。 |
| `POST /api/guided-hints/{hint_id}/attempt` | `{step_position:int,text:str}` | `{attempt,hint}`；仅已解锁位置（否则 422）；`(hint_id,position)` 唯一，重复 `replayed:true`。 |

`step` 视图：已解锁 `{position,kind,title,body,requires_attempt,attempted,unlocked_at}`；
未解锁 `{position,kind,title,locked:true,requires_attempt:true}`（**任何接口都不返回锁定正文**）。
`kind ∈ focus|concept|first_step|solution`；`status ∈ active|completed`；`source_note` 明示为规则分步提示
（非 AI 生成），完整解析仅在提交尝试后解锁。题目隔离/不可作答题 409；非法引用 422。

### 26.2 资料原文定位与摘录（`app/doc_location.py`，5 条）

| 方法/路径 | 请求 | 响应要点 |
| --- | --- | --- |
| `POST /api/documents/{document_id}/locate` | `{page?,chunk_id?,quote?}` | `{document_id,title,doc_version,pages,page,chunk_id,heading_path,quote,snippet,start,end,match:'exact'/'normalized',valid:true,notice}`；参数缺失/摘录找不到 422；他人 404。 |
| `POST /api/doc-annotations` | `{document_id,quote,note?,page?,chunk_id?,note_id?,color?}` | 201 `{annotation}`：保存 quote + `doc_version` 快照 + 位置（page/chunk/heading_path）；quote ≤500、note ≤1000、color ∈ yellow/green/blue/pink、单文档 ≤200 条（422）；note_id 非本人 404。 |
| `GET /api/doc-annotations` | `?document_id=&note_id=&page=&limit=` | `{items:[annotation],total}`；实时校验：`valid:true` 或 `valid:false` + `invalid_reason`（资料更新/已删除/当前版本找不到摘录），失效条目不 500。 |
| `POST /api/doc-annotations/{annotation_id}/refresh` | — | 在新版本重新定位：`{annotation,migrated,valid,invalid_reason,notice}`；找不到仍 200 + `valid:false`（不迁怒 404）。 |
| `DELETE /api/doc-annotations/{annotation_id}` | — | `{deleted:true,annotation_id}`；他人 404。 |

版本口径：`doc_version = sha256(json.dumps(chunks, ensure_ascii=False, sort_keys=True))[:16]`（内容摘要即版本）。
定位顺序：`chunk_id` 精确 → `quote`（空白折叠 + ASCII 大小写归一，返回原文偏移）→ `page`（该页首块起点）。

### 26.3 掌握路径 `/api/mastery-paths`（`app/mastery_path.py`，6 条）

| 方法/路径 | 请求 | 响应要点 |
| --- | --- | --- |
| `POST /api/mastery-paths` | `{subject?,kp_ids?:[int]≤8,title?,source_kind?}` | 201 `{path,nodes,progress,sample_note,replayed,note}`；同知识点集合 active 路径复用（200 + `replayed:true`）；非法知识点 422；自动选择无证据时 422「样本不足，无法自动生成掌握路径；请显式指定知识点」。 |
| `GET /api/mastery-paths` | `?status=&limit=` | `{items:[{path_id,title,subject,status,progress:{nodes_total,nodes_done,percent},sample_status,created_at,updated_at}],total,limit,status,note}`。 |
| `GET /api/mastery-paths/{path_id}` | — | `{path,nodes:[{node_id,position,kp_id,kp_name,title,kind,status,requirements,evidence,sample_status,completed_at}],progress,sample_note,recent_events[≤10],caliber}`；他人 404。 |
| `POST /api/mastery-paths/{path_id}/nodes/{node_id}/restate` | `{text(20–2000)}` | 仅 `concept` 节点：写 `self_report=independent` 自评证据并完成节点；过短 422；`{node,path,progress}`。 |
| `POST /api/mastery-paths/{path_id}/nodes/{node_id}/refresh` | — | 仅 `practice` 节点：按证据核验「≥2 道不同题独立答对」；不足保持 pending 并返回 `evidence.{distinct_questions,matched_questions,missing,note}`。 |
| `POST /api/mastery-paths/{path_id}/archive` | — | 归档（幂等）；归档路径节点操作 409。 |

完成条件口径：练习节点只认 `study_evidence` 有效、已核验、`verdict=correct` 且独立（`self_report=independent`
或 `prompt_state=none`）的**不同题**，提示/揭晓后作答不计；概念复述是学生自评独立标记，不冒充核验结果；
知识点关联题 <2 时 `sample_status=insufficient` 并显示「样本不足」，路径绝不伪造完成。

### 26.4 前端接线与静态契约

- `question.js`：「函数图像」（数学/含公式，懒加载 `functionGraph.js`）、「画解题思路」（懒加载 `sketchBoard.js`）、
  「按步骤提示」（`/api/guided-hints`，锁定正文不缓存）；保存后不整页刷新。入口带
  `data-study-tool-open="function-graph|whiteboard|guided-hints"`；导出 `questionRefOf`/`openGuidedHintsPanel` 供错题页复用。
- `wrongbook.js`：每行「解题工具」菜单（画思路/看图/按步骤提示），复用题目页入口；四档评分与既有功能不变。
- `notes.js`：资料/来源「定位原文」「保存为摘录」「我的摘录」（有效/失效、重新定位、删除）；接口不可用中文降级。
- `insight.js`：掌握路径列表/生成（薄弱知识点勾选 ≤8 或自动）/详情（刷新核验、概念复述、完成横幅、归档、样本不足提示）。
- 组件数据模型：`function_graph`（`source:"mafs-compatible"`，expressions/params/xRange/yRange）与
  `whiteboard`（`source:"excalidraw-compatible"`，elements 七类）均保存为 `study_artifact` 附件（版本化、by-target 重开）；
  函数图表达式用自写 AST 解析器求值（**不用 eval**）；白板预览 PNG 压缩 ≤64KiB。
- 新增 locale 键 218 个（`study.tools.*` 118 + `study.m3.*` 100），zh/en 各 **5,010** 键对齐；
  新增样式 `styles-study-tools.css`、`styles-m3-pages.css` 由 `web/index.html` 加载。
- `scripts/integration/ui_static_accept.py` 新增 M3 断言：组件文件齐全、题目/错题/笔记/学情接入点、
  样式加载、`study.tools./study.m3.` 命名空间下限与关键键。

### 26.5 验收入口

- `python -X utf8 scripts/integration/m3_accept.py`：端到端 27 项（逐步提示解锁协议/资料定位与摘录/
  掌握路径证据核验/图形附件 by-target），报告 `screenshots/m3-accept-report.json`。
- `scripts/accept_runner.py` 新增 `m3` 阶段（`bank → compileall → node-check → ui-static → round5 → m3 → pytest`）。
- 专项测试：`tests/test_guided_hint.py`（20）、`tests/test_doc_location.py`（29）、
  `tests/test_mastery_path.py`（27）、`tests/test_m3_integration.py`（6）。

## 27. M4 课堂版本化演示 / 固定版本导出 / 手绘模板（2026-11-21）

> 详细记录见 `docs/知序优化实施记录-20261121-第八轮.md`。`demo` 是**手工场景类型**，不参与 AI 生成：
> `SCENE_TYPES` 仍为 `slide/quiz/interactive/pbl`，新增 `MANUAL_SCENE_TYPES=("demo",)`；`/meta.scene_types`
> 不变，只追加手工场景能力字段。演示场景插入时冻结附件 revision 快照（data/preview/title），之后源附件升版
> 不影响课堂与导出；只读快照不执行附件内容、不引外链。

### 27.1 课堂演示场景（`app/classroom/{schema,api}.py`，2 条新路由）

| 方法/路径 | 请求 | 响应要点 |
| --- | --- | --- |
| `POST /api/classroom/courses/{course_id}/scenes/demo` | `{artifact_id, revision?, title?, source_note?}` | 201 `{scene, course:{id,scene_count}}`：`scene_type="demo"`、`content.artifact.{artifact_id,revision,kind,title,data,preview}` 快照、`fixed=true`、`fixed_revision`、`source_note≤300`、`assumptions`、`step_count`；课程归属 404、运行中课程任务 409、场景数超限 409、他人/不存在附件 404、kind 非法或内容超限 422。 |
| `DELETE /api/classroom/courses/{course_id}/scenes/{scene_id}` | — | 仅 `demo`（其他类型 409）；删除后 `order_index` 重排连续；`{deleted:true,scene_id,course:{id,scene_count}}`。 |

- `GET /api/classroom/meta` 追加（既有字段不变）：`manual_scene_types:["demo"]`、
  `demo_artifact_kinds:{demo:["function_graph","whiteboard","flow_animation","isometric","science_demo","demo_3d"]}`、
  `limits.demo_content_bytes`。
- `normalize_demo_content` 限额：artifact_id `^[a-zA-Z0-9_-]{6,64}$`、revision 1..100000、data ≤200KB、
  preview PNG dataURI ≤200KB（SVG ≤60KB）、content ≤260KB、source_note ≤300、假设 ≤6×200、步骤 0..50。
- `/scenes/{id}/html` 对 demo 返回 404 中文（不直出 HTML）；生成白名单拒绝 demo（422）。

### 27.2 固定版本导出（`app/classroom/exports.py`、导入 `app/classroom/importer.py`）

- **PPTX**：demo 独立页：标题 + 「原理演示 · kind 中文 · 固定版本 vN」+ 附件标题/ID + 出处/假设/步骤数，
  `preview.png` 可解码则等比插图，否则中文提示；备注写固定版本与 artifact_id。
- **离线 HTML / 打印 HTML（PDF）**：`section.cls-demo-scene[data-scene-type="demo"][data-artifact-id]
  [data-fixed-revision]`：徽标/标题/PNG dataURI/出处/假设/步骤数 + `<script type="application/json">` 内嵌快照
  （`</script>` 转义，不执行附件内容）。
- **ZIP**：`demos/scene-<NN>-<artifact_id>.json` 快照 + manifest `demoScenes`/`capabilities: demo-scenes`；
  课程 `scenes/*.json` 保留完整 demo content；资源包 PPTX 同样固定版本。
- **导入**：`importer` 接受 `ALL_SCENE_TYPES`，demo 用 `normalize_demo_content` 重新校验，导回后保持原固定版本；
  旧 ZIP（无 demo）行为不变。

### 27.3 前端接线

- `classroomPlayer.js`：demo 舞台（`.cls-demo-stage`），按 kind 懒加载 `principleDemo`/`functionGraph`/`sketchBoard`
  只读渲染；标题/固定版本徽标/出处/假设/步骤数/海报；渲染完成 `data-demo-ready="1"` + `zhixu:demo-ready` 事件；
  失败中文降级并展示海报；切换场景销毁组件。
- `classroom.js`：「插入演示」弹层（六类演示附件去重/排序/失败忽略该类）、插入固定当前版本、演示管理列表
  删除（仅删场景、confirm）；空列表中文引导。
- `notes.js`：笔记「学习附件」区（`GET /api/artifacts/by-target?target_kind=note&target_key=<note id>`），
  只读打开/下载海报/失效数量；无数据隐藏、接口不可用中文降级。
- 模板与预设（`web/vendor/principles/{flow,iso,presets}.js`、`question.js`）：
  flow 支持 `layout: flow|loop|timeline|state`、`style: clean|sketch`（确定性手绘，无随机数）；
  iso 支持 `shape: box|cylinder|gear|plate|rod`、多行 labels、sketch；
  新增预设 `flow-review-loop`/`flow-function-timeline`/`flow-photosynthesis-states`/`iso-anatomy-gearbox`/
  `iso-biology-stomata`；题目「查看原理」按学科推荐 + 下拉切换（8 个预设、按 kind 分组、`source_ref`=预设 id）。
- locale 新增 68 键（`study.m4.*`），zh/en 各 **5,078** 键对齐；ui_static 新增 M4 断言与命名空间下限。

### 27.4 验收入口

- `python -X utf8 scripts/integration/m4_accept.py`：端到端 20 项（附件→课堂固定版本场景→PPTX/HTML/ZIP/PDF
  固定版本→ZIP 导回→笔记 by-target→两账号隔离→删除与序号），报告 `screenshots/m4-accept-report.json`。
- `scripts/accept_runner.py` 新增 `m4` 阶段（… → round5 → m3 → m4 → pytest）。
- 专项测试：`tests/test_classroom_demo_scenes.py`（7）、`tests/test_m4_integration.py`（6），
  连同既有 `test_classroom_{api,export,import,dsl}.py` 共 80 passed。

## 28. M5 机械 3D / 工厂固定种子仿真（2026-11-21，`/api/demos`）

> 详细记录见 `docs/知序优化实施记录-20261121-第九轮.md`。模板由服务端注册表提供（模板 ID + 结构化参数），
> 模型/前端只拿模板 ID 与参数，不编造几何与数值；工厂指标由确定性 DES 计算，同 `seed` 可复现并落库回读。
> 新增学习附件 kind `mech_3d` / `factory_sim`（`app/study_artifact.ARTIFACT_KINDS` 与课堂
> `DEMO_ARTIFACT_KINDS` 仅追加）。

### 28.1 模板与构建/仿真（`app/demo_library.py`，4 条）

| 方法/路径 | 请求 | 响应要点 |
| --- | --- | --- |
| `GET /api/demos/templates` | — | `{items:[{template_id,kind,title,description,parametric:true,params:[{name,label,type:int/float/list,default,min,max,step,unit,length?}],assumptions,source_note}],total}` |
| `POST /api/demos/build` | `{template_id, params?}` | `{template_id,kind,title,seed,params,data,assumptions,source_note}`；不落库、确定性；未知模板 404，未知/越界/类型错误 422 中文。 |
| `POST /api/demos/simulate` | `{template_id:"factory-line", params?}` | `{run:{run_id,template_id,seed,params,result{flow,station_labels,metrics,events},created_at},deterministic:true}`；仅工厂类模板可持久化。 |
| `GET /api/demos/simulate/{run_id}` | — | `{run}`；本人限定，他人/不存在 404。 |

模板：`gearbox-3d`（mech_3d，seed/input_rpm/driver_teeth/driven_teeth/module_mm）、`pump-3d`（mech_3d，
seed/stroke_mm/rpm/bore_mm）、`factory-line`（factory_sim，seed/sim_seconds/arrival_interval_s/service_s[3]/
buffer_capacity）、`factory-line-2d`（flow_animation，复用 DES 结果）。
机械数据 `data`：`{version,basis:"local-teaching",seed,parts[{id,label,kind:box|cylinder|gear|rod|plate,
position,size,color,parent?}],steps[{label,part_ids,explode}],readouts[{id,label,value,unit,note}],camera,
assumptions,source_note}`；读数均工程公式 + 「教学近似」标注（速比=driven/driver、输出转速=input_rpm/速比、
扭矩放大≈速比、分度圆直径=m·z、中心距=(d1+d2)/2；泵 V=π(b/2)²s、Q=Vn、v=2sn/60、ω=2πn/60）。
工厂 DES：唯一随机源 `random.Random(seed)`、固定抽取顺序；事件 `{t(5 位小数),type:arrival|queue|
service_start|service_end|blocked,station:0-2,item_id,queue}` 稳定排序、≤2000 条；指标
`sim_seconds/arrivals/completed/throughput_per_min/avg_wait_s[3]/max_queue[3]/utilization[3]/
bottleneck_station/bottleneck_reason/dropped/blocked`。

### 28.2 前端组件与接入

- `web/js/components/mechDemo.js`：`createMechView`（本地 three.js 拆解/剖切/运转/读数；
  无 WebGL 降级 SVG 等距示意 + 中文提示；`onFrameReady`）、`createFactoryView`（指标卡/三工位流程/
  队列曲线/事件回放时间轴，事件只回放不重算）、`openMechDemo`（模板选择、只读参数、保存附件）。
- `web/vendor/principles/mechPresets.js`：本地兜底预设（gearbox-3d / pump-3d / factory-line）；
  `web/styles-mech.css` 由 `web/index.html` 加载。
- 题目页「机械演示」（物理/机械/技术，默认模板按学科，失败回退本地预设，保存为学习附件并 links 到题目）；
  课堂播放器与笔记附件面板支持 `mech_3d`/`factory_sim` 只读打开（沿用固定版本徽标与
  `data-demo-ready`/`zhixu:demo-ready`）。
- 导出：课堂 demo 导出的 kind 标签追加「机械 3D」「工厂仿真」；离线 HTML demo 场景含
  `<meta name="zhixu-demo-frame-ready">`、静态 `data-demo-frame-ready="1"` 与运行时 DOM 标记，
  视频/截图管线可等待帧就绪（仍在 `__zhixuCapture.sceneCount` 内逐场景截帧）。
- locale 新增 99 个 `study.m5.*` 键，zh/en 各 **5,177** 键对齐；ui_static 新增 M5 7 条断言与
  `study.m5.` 命名空间下限。

### 28.3 验收入口

- `python -X utf8 scripts/integration/m5_accept.py`：端到端 18 项（模板/机械确定性与读数/参数校验/
  工厂同种子可复现与指标/运行回读与隔离/附件与课堂固定版本导出/帧就绪标记/ZIP 双演示快照），
  报告 `screenshots/m5-accept-report.json`。
- `scripts/accept_runner.py` 新增 `m5` 阶段（… → m4 → m5 → pytest）。
- 专项测试：`tests/test_demo_library.py`（15）、`tests/test_m5_integration.py`（5）、
  `tests/test_m5_video_frames.py`（3），连同既有附件/课堂回归共 46 passed（专项 23 passed）。

## 29. M6 专题报告 / 互动讲义 / 小纸分步提示 / pyKT 协议（2026-11-21）

> 详细记录见 `docs/知序优化实施记录-20261121-第十轮.md`。专题报告与互动讲义均为**服务端规则汇编**
> （`generator="rules"`，不调用 AI 模型），引用只来自真实资料片段/摘录/附件/题目；失败中文降级、不伪造内容。
> 新增表 `topic_reports` / `handouts`（各自 `init_*()` 幂等建表，已登记 `db.py::_ID_TABLES`）。

### 29.1 专题报告 `/api/topic-reports`（`app/topic_report.py`，6 条）

| 方法/路径 | 请求 | 响应要点 |
| --- | --- | --- |
| `POST /api/topic-reports` | `{topic, title?, document_ids?, note_ids?, kp_ids?, artifact_ids?}` | 201 `{report:{report_id,topic,title,generator,status,version,created_at,updated_at}, content_md, sources}`；四类来源至少一项（422）、每类 ≤10、个人 ≤50；他人资料/笔记/附件 404、非法知识点 422。 |
| `GET /api/topic-reports` | `?limit=20` | `{items:[summary],total,limit,note}`（最近更新在前）。 |
| `GET /api/topic-reports/{id}` | — | `{report,content_md,sources}`；他人 404。 |
| `PATCH /api/topic-reports/{id}` | `{title?,content_md?,expected_version?}` | 版本 CAS 冲突 409；title ≤120、content_md ≤200KB（422）；成功 version+1。 |
| `DELETE /api/topic-reports/{id}` | — | `{deleted:true,report_id}`；他人 404。 |
| `GET /api/topic-reports/{id}/markdown` | — | `text/markdown; charset=utf-8` 附件（RFC 5987 中文文件名 + ASCII 回退）。 |

正文固定七节：标题/说明 → 一、主题与范围 → 二、资料依据（命中主题关键字的 chunk，带
`《标题》第 N 页 · chunk <id> · 文档版本 <doc_version>`；未命中回退开头并注明）→ 三、笔记摘录
（笔记摘要 + `doc_annotations` 摘录出处）→ 四、学习附件（kind/固定版本 vN）→ 五、相关练习（题库检索候选，
注明非正式标签）→ 六、学习证据（`study_evidence.summary` 真实字段 + 样本是否充足）→ 七、口径与局限
（规则汇编非 AI、引用随版本/删除失效）。`sources` 与正文一一对应，不编造。

### 29.2 互动讲义 `/api/handouts`（`app/handout.py`，5 条）

| 方法/路径 | 请求 | 响应要点 |
| --- | --- | --- |
| `POST /api/handouts` | `{title?,course_id?,note_ids?,artifact_ids?,kp_ids?}` | 201 `{handout:{handout_id,title,course_id,version,generator,created_at,updated_at}, sections:[...]}`；至少一项来源（422）、单类 ≤10、个人 ≤50；课程/笔记/附件他人 404、知识点 422。 |
| `GET /api/handouts` | `?limit=20` | `{items:[summary],total,limit}`。 |
| `GET /api/handouts/{id}` | — | `{handout,sections}`；他人 404。 |
| `DELETE /api/handouts/{id}` | — | 删除；他人 404。 |
| `GET /api/handouts/{id}/export?format=html\|pdf` | 默认 html | html=`text/html; charset=utf-8` 自包含单文件（内联 CSS、无脚本/外链、`html.escape`、`data-handout-id`/`data-version`）；pdf=`application/pdf`（依赖 `app.ai.render.html_to_pdf`，不可用 503 中文）；非法 format 422；均带安全下载文件名。 |

小节固定顺序：`course_outline`（场景标题/类型/speech 摘要）→ `artifact`（kind/固定版本/来源/合法 PNG 预览，累计
≤300KB 超限省略并注明）→ `excerpt`（笔记摘录 ≤10，带文档出处）→ `practice`（题库候选）→ `evidence`
（学习证据 + 样本是否充足）→ `meta`（规则汇编声明/引用局限/固定版本说明）。

### 29.3 小纸只读工具 `step_hint`（`app/assistant.py`）

- `TOOL_SPECS` 新增 `step_hint`：`side_effect=false`、`requires_confirmation=false`、参数 `ref`（必填 ≤160）；
  `/api/assistant/tools` 工具数 13 → 14。
- 处理器读取 `guided_hint.get_hint_by_ref`，无会话时幂等创建；只返回公开视图（`hint`+`steps`），
  锁定步骤服务端无正文、工具不补全；`refs` 指向题目统一引用；异常转中文 `ToolError` 不 500。
- 系统提示新增规则：只引用已解锁步骤、不得编造/推测锁定内容、完整解析须提示学生页面逐级提交尝试。

### 29.4 扫描资料本地识别内容块（`app/ai/notes_agent.py`）

- 扫描页本地 OCR 结果新增内容块（`id/kind/text/latex/needs_review/confidence/order/page/rotation/bbox/norm/source`，
  ≤200 块/页，空文字块丢弃）；视觉模型超时/失败时保留 `source=local_ocr` 的文本 + 块，视觉成功不冒充块来源。
- 页面结构（`ocr_pages_json`）与 `_page_entry`/`_normalize_ocr_pages` 支持 `blocks` 往返，旧页面不新增空键；
  `_local_ocr_page_text` 保留兼容；`_local_ocr_page` 调用 `ocr.recognize_image(..., include_blocks=True)`。
- 单测 `tests/test_m6_ocr_blocks.py`（6）覆盖规范化/截断/往返/fallback/落库。

### 29.5 pyKT 离线评估协议（`scripts/integration/pykt_export.py`）

- 只读 SQLite；去标识化盐必须显式提供（`--salt` 或 `STUDY_PYKT_SALT`，缺失拒绝导出退出码 2）；
  JSONL 字段 `user_key(sha256 盐+id[:16])/item_key/item_version/kc_keys/timestamp/label/source_key/
  prompt_state/reveal_state/self_report/evidence_version`，不含用户名/题干/答案；跳过 pending/unknown/自评/孤儿行。
- 阈值门控：用户数、总事件、每用户事件（达标人数）、每 KC 事件、标签覆盖率；`evaluation_allowed` 如实反映；
  `baseline_metrics`：平滑正确率 + PFA-lite（仅用时间顺序历史、无泄露）；报告默认 `screenshots/pykt-report.json`。
- 真实数据不足时只输出协议/门控/基线结论，不在仓库写入真实用户数据（默认产物已 .gitignore）。

### 29.6 前端与静态契约

- `web/js/components/learningDocs.js`：`renderTopicReportsPanel`/`renderHandoutsPanel`（列表/创建/详情/
  编辑 CAS/下载 Markdown/导出 HTML/PDF/删除；接口不可用独立中文降级、隐藏创建）；
  `web/js/pages/notes.js` 页面底部「学习文档」区块（两个折叠面板），会话重置销毁；
  `web/styles-m6.css`（`.m6-`）由 `web/index.html` 加载。
- locale 新增 110 个 `study.m6.*` 键，zh/en 各 **5,287** 键对齐；ui_static 新增 M6 组件/页面/样式/命名空间断言。

### 29.7 验收入口

- `python -X utf8 scripts/integration/m6_accept.py`：端到端 15 项（专题报告真实引用/版本 CAS/Markdown/隔离；
  互动讲义小节/自包含 HTML/PDF/隔离；助手只读 step_hint；pyKT 去标识化导出与门控/基线结构），
  报告 `screenshots/m6-accept-report.json`。
- `scripts/accept_runner.py` 新增 `m6` 阶段（… → m5 → m6 → pytest）。
- 专项测试：`tests/test_topic_report.py`（19）、`tests/test_handout.py`（17）、
  `tests/test_assistant_step_hint.py`（7）、`tests/test_pykt_export.py`（18）、
  `tests/test_m6_integration.py`（5）、`tests/test_m6_ocr_blocks.py`（6），新增合计 72 passed。

## 30. M7 离线可交互演示导出 / 专题报告转笔记（2026-11-21，方案收尾）

> 详细记录见 `docs/知序优化实施记录-20261121-第十二轮.md`。补齐《总方案》§5.3「专题报告保存为带出处的笔记」
> 与 §5.4「PPTX/PDF 放海报、HTML/ZIP 保留可交互内容」。离线播放器完全自包含（零外链、零 eval、零动态 import）；
> 无演示课程的导出与旧版逐字节一致；PPTX/PDF/print 仍为海报+文字。

### 30.1 离线可交互演示（`app/classroom/exports.py` + `scripts/build_offline_demo_bundle.py`）

- 构建：`python -X utf8 scripts/build_offline_demo_bundle.py` 读取 `web/vendor/principles/{flow,iso}.js`
  （均无 import 的纯 ESM），文本转换后各自 IIFE 隔离同名符号，挂 `window.ZhixuOfflineDemo`
  （`createFlowView/renderFlowPoster/createIsoView/renderIsoPoster`）。
  产物 `web/vendor/offline/offline-demo.js`：76,722 字节、UTF-8+LF、确定性；`--check` 校验产物与源一致
  （已纳入 ui_static 断言）；源 `web/js/offline/offlineDemo.js` 负责遍历 `section.cls-demo-scene`、
  解析内嵌 `script.cls-demo-data` 并按 kind 挂载。
- HTML（含 demo）：保留既有静态海报/`data-fixed-revision`/内嵌 JSON/`data-demo-frame-ready`；末尾内联
  bundle + 挂载脚本 + flow/iso 样式（读取本地 `web/styles-principles.css`，无外链）；
  每个 demo section `data-offline-demo="<kind>"`，挂载成功 `data-offline-demo-mounted="1"`，
  控件 `data-offline-demo-controls="1"`，`body.capture` 下隐藏控件（视频截帧无控件；iso 用 staticMode 首帧）。
- 支持矩阵：`flow_animation`（createFlowView 逐步交互）、`isometric`（createIsoView）、
  `science_demo/demo_3d`（flow 形状时渲染，否则海报）、`factory_sim`（SVG 指标卡/工位流程/队列/事件时间轴回放，
  真实 metrics/events，缺失显示「—」不伪造）；`function_graph/whiteboard/mech_3d` 离线 HTML 仅海报
  （`data-offline-demo-error` 中文提示，不标 mounted）。
- ZIP（含 demo）：新增 `offline.html`（与 HTML 同源）与 `manifest.offlineHtml`；原 `demos/*.json`/
  `demoScenes`/capabilities 不变，导入器兼容（固定版本一致）。
- 无 demo 课程：HTML/ZIP 输出与旧版一致（逐字节对照两样本）；PPTX/PDF/print 不出现播放器标记。
- 安全：零 `http(s)://`/`<script src`/`<link`/`@import`/`url(`，无 `eval`/`new Function`/动态 import；
  附件数据仅 `JSON.parse`，`</script>` 转义；文本一律 textContent/createTextNode。

### 30.2 专题报告保存为带出处的笔记（`app/topic_report.py`）

| 方法/路径 | 请求 | 响应要点 |
| --- | --- | --- |
| `POST /api/topic-reports/{report_id}/save-note` | — | 201 `{note:{id,title,created_at,source_count,content_bytes},report_id,saved}`；报告非本人/不存在 404；重复保存（同用户最近 50 条笔记中标题+正文 SHA-256 相同）返回 `saved:false` + `duplicate_of_note_id` + 中文 `message`，不重复插入。 |

- 笔记内容 = 报告 `content_md` + `## 出处清单`（kind 中文/标题/页码/chunk/版本/出处/引用摘要前 120 字；
  已有清单幂等；无来源如实说明）。`ai_notes` 字段：title ≤180、content ≤120,000（超长截断并注明）、
  sources_json 精简来源 ≤50 条、source_versions_json 记录文档版本；文档失效不阻断并记录
  `invalid_document_id/invalid_reason`；归属校验不绕过。
- 前端 `learningDocs.js`：专题报告详情新增「保存为笔记」按钮（成功/已存在/失败中文提示，不跳转），
  新增 6 个 `study.m7.*` locale 键（zh/en 各 **5,293** 键对齐）。

### 30.3 验收入口

- `scripts/integration/m4_accept.py`：**21/21**（新增「离线 HTML 内联交互演示播放器」检查）；ZIP 导回固定版本一致。
- `scripts/integration/m6_accept.py`：**17/17**（新增「保存为带出处的笔记」「重复保存不重复创建」检查）。
- `ui_static_accept.py`：**7/7**（新增离线播放器/构建一致性与保存笔记入口断言）。
- 专项测试：`tests/test_classroom_offline_demo_player.py`（13）、`tests/test_topic_report_note.py`（9）、
  `tests/test_m7_integration.py`（5）；直连回归 68 passed。
- 来源变更后需重跑 `python scripts/build_offline_demo_bundle.py`（ui_static 已校验产物一致性）。
