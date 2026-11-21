# 整合模块本机验收脚本（AI 课堂 / 多智能体 / 交互 / PBL / 导出导入 / 语音输入 ASR / 首页 / 题库 / 试卷 / 学情 / 任务 / 助手）

本目录脚本用于在**本机隔离实例**上真实浏览器走通 AI 课堂的完整链路，并产出截图与 JSON 报告。
另含 `modules_accept.py`：覆盖除课堂外的首页、我的题库、试卷工坊、学情分析、任务中心、AI 助手全链路。
脚本不访问外网、不读取真实 API Key、不接触生产数据。

| 文件 | 作用 |
|---|---|
| `classroom_accept.py` | Playwright 浏览器验收：课程创建 → 生成 → 大纲确认 → 播放器（聚光灯/字幕）→ 测验判分 → AI 老师对话 → **PBL / 交互场景** → **导出 PPTX/HTML/ZIP** → **课程 ZIP 导入** → **多智能体课堂（名册/讨论/插话/回放/白板）**；以及语音输入（课堂 tutor / AI 助手）支持与不支持两种状态、桌面与手机截图 |
| `modules_accept.py` | Playwright 浏览器验收（**不含课堂**）：首页 AI 工作台 6 卡/跳转/动态、我的题库（统计/筛选/编辑弹层/导入 dry_run/导出下载）、试卷工坊（4 tab/导入历史/组卷试题篮/试卷详情/Word PK 头/打印 iframe/答题卡 SVG/整卷练习提交）、学情分析（有数据/空态/打印媒体/无 provider 时 AI 建议禁用）、任务中心（列表/过滤/详情或空态）、AI 助手（会话/新建/技能选择/无 provider 发送引导）、移动端 390 无溢出 |
| `plaza_accept.py` | 广场 Phase 2 + Phase 3 端到端验收：HTTP 全链路（匿名访客读广场 → 作者投稿/声明/送审 → 编辑初审 → 5 人独立评审阈值 → 自动核验通过 → 编辑签发精选 → 撤回 → 匿名不可见；Phase 3 追加抽检 pass/high 自动撤回、收藏/纠错/举报/申诉闭环、回滚恢复旧版本、通知中心与质量指标）+ 可选 Playwright 页面冒烟（匿名广场桌面/手机、作者投稿台、评审台、编辑台、详情收藏/纠错/举报、通知铃铛、编辑台抽检/反馈/指标、作者申诉卡），输出 `plaza-accept-report.json` 与 23 张截图 |
| `official_accept.py` | 平台精选接入学习链路端到端验收（15 步）：复用 plaza_accept 账号/角色/发布链路发布客观题+主观题 → 匿名/登录检索与 `official:<public_id>` 详情 → official 作答（客观判分/主观 pending）→ 错题与复习队列 → 工坊建卷（快照含 official 来源）→ 整卷练习提交/回看 → **学段筛选 `/api/questions?grade=` 与 `/api/insight/home` 精选口径** → **课堂以 `source_ref=official:<id>` 建课生成大纲（`outline.sources` 含公开编号/精选版本）** → **助手 HTTP 会话驱动 `read_question` 读取 official 题面** → 撤回后检索/详情/作答/建卷/课堂建课立即失效、助手读取给出已撤回提示，输出 `official-accept-report.json` |
| `ops_accept.py` | WS30 质量运营 + 运维端到端验收：`GET /api/admin/monitor` 结构/权限/短缓存/单组降级构造与恢复；抽检门禁 `initial_100 + armed`（未抽检签发 409 → 签发前抽检 pass → 200、override 审计、普通用户 403）；一键恢复（抽检 high 自动撤回→恢复公开、编辑撤回→恢复、未确认 422、未知 404、改版 v2 已活跃时恢复旧发布 409、幂等）；批量处理 issues/reports/appeals 部分成功语义；通知 unread-count/kind 全读/ids 去重/事件去重；指标 gate/restorations/ops.batch/notifications 字段，输出 `ops-accept-report.json` |
| `ui_static_accept.py` | WS30 前端静态验收（不启动服务/不联网）：`node --check` 遍历 `web/js/**/*.js` + ESM 解析；任务深链格式（课堂/笔记/OCR/工坊/我的题库/拍题）；平台精选公开配图与纠错接口；学段筛选接线；笔记/Anki 题链接与来源版本字段；locale 中英文键对齐，输出 `ui-static-accept-report.json` |
| `ai_usage_accept.py` | AI 用量与配额端到端验收：脚本内嵌只监听 127.0.0.1 的 OpenAI 兼容假模型 → 普通用户创建 explain 任务完成任务 → `/api/ai/usage/me` 用量行（模型/耗时/tokens/入参分类）→ 管理台 `/api/admin/ai/usage` 汇总 → 普通用户 403 → 配额收紧为 1 后超额 429（不落库/不消耗）→ 恢复默认配额，输出 `ai-usage-accept-report.json`；不产生真实模型费用 |
| `fake_model_server.py` | 仅供本目录验收使用的 **OpenAI 兼容假模型服务**（只监听 `127.0.0.1`，按 prompt 标记返回固定结构化 JSON） |
| `round5_baseline.py` | 第五轮基线固定与样本抽检（**只读**）：仓库 HEAD/工作树、构建指纹、`releases/CURRENT` 题库版本与计数、应用库快照（知识点/关联/用户）、只读写探测；产出 3–5 个数学叶子知识点 + 候选题 + OCR 分层标注模板（**待人工审核**，不伪造结论），输出 `screenshots/round5-baseline.json` 与 `docs/r5-数学知识点抽检-<日期>.{json,md}` |
| `round5_accept.py` | 第五轮端到端验收（临时数据目录 + TestClient，不访问外网/不读写生产数据、不调用真实模型）：拍题上传 → 确认含识别块（公式 LaTeX + bbox 往返/非法块 422）→ 题库取单选题 → 独立作答 → 同 `submission_id` 重复提交只入账一次 → 证据可读 → 错题本 → 四档复习迁移/评分/幂等重放/版本 409/他人 404/新旧到期对账 → 学习附件创建/新版本 CAS/固定版本读取/跨账号 404 → 题目详情 `study` 聚合 → 自评不覆盖核验证据 → 证据汇总与复习统计，输出 `round5-accept-report.json`；已接入 `scripts/accept_runner.py` 的 `round5` 阶段 |
| `m3_accept.py` | M3 端到端验收（临时数据目录 + TestClient）：逐步提示创建（锁定正文不下发）→ 未提交尝试不能解锁 → 尝试后逐级解锁至完整解析 → 他人 404；上传 TXT 资料 → 按摘录/页/chunk 定位原文 → 缺失摘录 422 → 保存摘录与列表有效 → 重新定位 → 他人不可定位；掌握路径生成（2 节点/样本状态）→ 概念复述门槛与完成 → 变式练习无独立证据保持待完成 → 他人 404 → 归档；函数图与白板作为学习附件保存并可按题目 `by-target` 读取，输出 `m3-accept-report.json`；已接入 `accept_runner.py` 的 `m3` 阶段 |
| `m4_accept.py` | M4 端到端验收（临时数据目录 + TestClient）：创建流程演示学习附件 → 建课堂 → meta 声明手工 `demo` 场景（既有生成类型不变）→ 插入固定版本演示场景 → 源附件升版后课堂仍保留原版本 → 导出 PPTX（含「固定版本」）/离线 HTML（`data-fixed-revision` + 内嵌快照）/ZIP（`demos/*.json`）/PDF → ZIP 导回课堂且演示仍为固定版本 → 笔记 `by-target` 读取附件 → 他人课程 404 → 删除演示场景并清空，输出 `m4-accept-report.json`；已接入 `accept_runner.py` 的 `m4` 阶段 |
| `README.md` | 本文件 |

## 安全边界（务必遵守）

- 假模型服务只绑定回环地址 `127.0.0.1`，没有外网访问；**不要**把它暴露到局域网/公网。
- 验收脚本自动创建的连接使用占位 Key `sk-local-acceptance-only`，只会写入**隔离实例的数据目录**。
- 请始终给验收实例单独的 `STUDY_DATA_DIR`（临时目录），不要指向真实用户数据目录。
- 假模型输出的是固定样例内容，仅用于验证 UI/接口链路，**不代表真实 AI 生成质量**。

## 前置条件

- 仓库根目录执行；Python 3.11（依赖已在 `requirements.txt` 中）。
- Playwright 可用（`pip show playwright` 有输出即可；脚本优先使用本机 Edge，其次 Chrome，最后 Playwright Chromium）。
- 无需额外依赖、无需外网。

## 覆盖点

假模型服务按 prompt 标记识别以下产物（标记都来自产品 prompt 标题，避免误匹配）：

| 标记（prompt 标题） | 产物 | 覆盖的产品能力 |
|---|---|---|
| `课程大纲生成器` | 4 场景大纲（slide/interactive/quiz） | 课程生成两阶段 |
| `幻灯片内容生成器` | 文本/形状/公式/表格/图表元素 | 播放器幻灯片渲染 |
| `测验内容生成器` | 单选 + 多选题目 | 测验作答与判分 |
| `交互场景 HTML 生成器` | 自包含 HTML（`zhixuReport` ready/state postMessage） | 沙箱 iframe + `interactive-event` 上报 |
| `项目式学习（PBL）设计器` | 背景/目标/角色/里程碑/交付物/rubric | PBL 只读渲染 |
| `课堂旁白与动作生成器` | 旁白 + spotlight/laser/whiteboard（像素坐标） | 播放动作时间轴 |
| `课堂 AI 教师对话` | tutor 回复 + spotlight 动作 | 自由问答抽屉 |
| `课堂角色名册生成` | 1 名老师 + 2 名同学名册 | 多智能体名册生成 |
| `多智能体课堂脚本生成` | 5 段讨论（spotlight / **whiteboard `coord_space="unit"` 7 种 op** / laser） | 多角色讨论逐段播放 |
| `多智能体课堂 · 学生插话回应` | 2 段回应 | 学生插话 |

未识别 prompt 返回**明确的 JSON error 对象**（HTTP 200，不 500），后端会给出结构化校验提示。

验收脚本逐步覆盖：

- 课程列表 / 创建 / 生成工作台 / awaiting_outline 大纲编辑与确认 / 生成完成；
- 导出对话框：PPTX（PK 头）、离线 HTML（含课程内容）、课程 ZIP（含 `manifest.json`）；
- 课程 ZIP 导入后列表数量 +1 并自动进入课程；
- 播放器：聚光灯/字幕、PBL 结构、交互 iframe（`sandbox="allow-scripts"`、`progress.interactive` 有摘要）、测验判分、刷新续播；
- 多智能体：生成名册 → 讨论逐段气泡 + TTS → 暂停/继续 → 跳过 → 白板 unit 坐标渲染断言 → 重播不重复气泡 → 插话 loading/追加回应 → 会话 `turns` 持久化（含多角色与白名单动作）→ 刷新回放统一历史 → 清空当前显示 → 移动端抽屉不溢出；
- 语音输入（ASR）：支持/不支持两种状态、桌面与手机；只把最终文本填入输入框、不自动发送。

所有断言失败会记录到报告 `problems[]` 并继续（能截的图仍会落盘），脚本退出码非 0。

## 步骤

### 1. 启动隔离实例（终端 A）

端口自选空闲端口（示例 8832）：

```powershell
cd E:\responsibility\study-platform
$env:STUDY_DATA_DIR = Join-Path $env:TEMP ("zhixu-classroom-accept-" + (Get-Random))
$env:STUDY_ALLOWED_HOSTS = "127.0.0.1,localhost"
$env:STUDY_INDEX_PREWARM = "0"
# 可选：固定稳定题库版本（跑全量 pytest 验收时同样建议固定 r3，避免题库升级影响旧基线）
$env:STUDY_BANK_DIR = "E:\responsibility\高考题库_按学科\releases\bank-20261120-r3"
python -m uvicorn app.main:app --host 127.0.0.1 --port 8832
```

浏览器访问 <http://127.0.0.1:8832> 能打开登录页即可。首次注册的账号自动成为管理员（隔离库）。

### 2. 启动本机假模型服务（终端 B）

```powershell
cd E:\responsibility\study-platform
python scripts/integration/fake_model_server.py --port 8902
```

看到 `fake model server on http://127.0.0.1:8902/v1` 即就绪；验收结束后 `Ctrl+C` 停止。

### 3. 运行验收（终端 C）

```powershell
cd E:\responsibility\study-platform
python scripts/integration/classroom_accept.py `
    --base-url http://127.0.0.1:8832 `
    --fake-model-base-url http://127.0.0.1:8902/v1
```

脚本会自动：登录/注册验收账号 → 检查并创建指向假模型服务的连接 → 走完整链路 → 输出截图与报告。
退出码 `0` 表示 `ok:true`。

常用参数：

| 参数 | 默认值 | 说明 |
|---|---|---|
| `--base-url` | `http://127.0.0.1:8827` | 隔离实例地址 |
| `--username` / `--password` | `课堂验收` / `ui-pass-12345` | 验收账号（不存在时自动注册，仅写入隔离库） |
| `--fake-model-base-url` | `http://127.0.0.1:8899/v1` | 本机假模型服务地址 |
| `--out` | `screenshots/integration` | 截图与报告输出目录（相对仓库根） |
| `--skip-classroom` | 关闭 | 只跑语音输入验收 |
| `--skip-asr` | 关闭 | 只跑课堂全链路验收 |
| `--skip-multiagent` | 关闭 | 跳过课堂内的多智能体讨论验收 |

示例（只验证多智能体/课堂，不跑 ASR）：

```powershell
python scripts/integration/classroom_accept.py --base-url http://127.0.0.1:8832 `
    --fake-model-base-url http://127.0.0.1:8902/v1 --skip-asr
```

## 产物

- 截图：`screenshots/integration/classroom-*.png`、`assistant-asr-*.png`，包括：
  - 课程列表 / 创建 / 生成设置 / 大纲确认 / 生成完成 / 导出对话框；
  - 播放器首屏、聚光灯+字幕、PBL、交互场景、测验、判分、刷新续播；
  - AI 老师对话、桌面/手机列表与播放器/测验；
  - **多智能体**：`classroom-multiagent-roster-empty-desktop.png`、`-roster-desktop`、`-discussion-desktop`、
    `-pause-desktop`、`-whiteboard-desktop`、`-replay-desktop`、`-interject-loading-desktop`、
    `-interject-desktop`、`-history-desktop`、`-clear-desktop`、`-mobile`；
  - **语音输入**：课堂 tutor（支持）、AI 助手（支持/不支持，桌面）、AI 助手（支持/不支持，手机）。
- 报告：`screenshots/integration/classroom-ui-report.json`，字段：
  - `ok`：无 `problems` 即 `true`；
  - `steps[]`：每步实测数据（如导出文件字节数、交互 `progress.interactive` 摘要、多智能体 `turns`/角色/动作、
    白板 unit 坐标换算、气泡数变化、语音输入填入文本、横向溢出 `overflow` 等）；
  - `screenshots[]`、`problems[]`（断言失败/流程失败的说明与截图路径）。

## 语音输入（ASR）验收说明

组件 `web/js/components/voiceInput.js` 使用浏览器原生 `SpeechRecognition` / `webkitSpeechRecognition`：

- 支持时：点击麦克风 → 实时临时转写 → 最终文本**只填入输入框**（不自动发送）；
- 不支持时：按钮禁用并显示「当前浏览器不支持语音输入」中文降级提示；
- 验收脚本用 `add_init_script` 注入一个 mock `SpeechRecognition`（模拟临时结果与最终结果）来稳定复现
  「支持」状态；用删除构造函数的脚本复现「不支持」状态。两者都不涉及真实麦克风与网络。

## 整合模块全链路验收（`modules_accept.py`，不含 AI 课堂）

### 1. 启动隔离实例

沿用上文「启动隔离实例（终端 A）」，端口自选（示例 8832）。**无需假模型服务**：本脚本验证的是
「没有 AI Key 时各模块的真实表现」（AI 建议/任务/助手应给出中文配置引导与空态，而不是假数据）。

### 2. 运行验收（示例数据由既有 `scripts/seed_demo.py` 注入）

```powershell
cd E:\responsibility\study-platform
python scripts/integration/modules_accept.py --base-url http://127.0.0.1:8832 --seed
```

- `--seed`：先调用 `scripts/seed_demo.py --register --json` 写入「示例」数据（课堂/题库/试卷/答题卡/练习记录/
  助手会话），随后每个模块自动断言；退出码 `0` 表示 `ok:true`。
- 若隔离实例已有示例数据（或不想重新注入），用 `--skip-seed` 直接验收：

```powershell
python scripts/integration/modules_accept.py --base-url http://127.0.0.1:8832 --skip-seed
```

常用参数：

| 参数 | 默认值 | 说明 |
|---|---|---|
| `--base-url` | `http://127.0.0.1:8828` | 隔离实例地址 |
| `--username` / `--password` | `模块验收` / `ui-pass-12345` | 验收账号（`--seed` 时由 seed_demo 注册） |
| `--out` | `screenshots/integration` | 截图与报告输出目录（相对仓库根） |
| `--seed` | 关闭 | 先运行 `scripts/seed_demo.py --register --json` |
| `--skip-seed` | 关闭 | 复用已有示例数据（与 `--seed` 互斥使用） |
| `--skip-home` / `--skip-mybank` / `--skip-paperlab` / `--skip-insight` / `--skip-tasks` / `--skip-assistant` | 关闭 | 跳过对应模块 |
| `--skip-empty-state` / `--skip-mobile` | 关闭 | 跳过学情空态账号检查 / 移动端 390 截图 |

覆盖点：

- **首页**：AI 工作台 6 张卡（`classroom/paperlab/mybank/revision/assistant/tasks`）、计数非负、
  点击跳转、最近动态/空态；
- **我的题库**：统计卡、学科筛选、编辑弹层打开/关闭、导入 dry_run 校验、导出 JSON/CSV 下载（字节数 > 0）；
- **试卷工坊**：4 个分区 tab、导入历史/详情、手工组卷选题→加入试题篮→移除、试卷详情三区块、
  Word 下载（文件头 `PK`）、打印 iframe 加载 200（后端自包含 HTML）、答题卡 SVG 预览加载、
  整卷练习开始→作答→提交→结果页；
- **学情分析**：摘要/统计卡/图表/知识点/建议卡、无 provider 时「生成学习建议」禁用并给「前往模型设置」、
  `@media print` 下工具栏隐藏只保留报告；另用独立空态账号验证「还没有学习记录」空态；
- **任务中心**：列表/状态过滤/详情；无 AI Key 的隔离实例没有 AI 任务时验证空态与引导入口
  （报告记录 warning，不伪造任务）；
- **AI 助手**：会话列表、新建会话、技能选择器展开并勾选（不发送）、无 provider 发送时给出中文配置引导
  且不产生模型回复；
- **移动端 390**：首页 / 试卷工坊列表 / 学情分析截图并断言无横向溢出。

### 3. 产物

- 截图：`screenshots/integration/modules-*.png`，含首页、题库（页面/编辑弹层/导入 dry_run）、试卷工坊
  （导入/组卷/详情/答题卡/练习结果）、学情（有数据/打印媒体/空态）、任务中心、助手（技能/无 provider 引导）、
  移动端首页/试卷工坊/学情；
- 报告：`screenshots/integration/modules-accept-report.json`，字段：
  - `ok`：无 `problems` 即 `true`；`steps[]`：每步实测数据与断言结果；
  - `seed`：seed_demo 摘要（模块状态、入口 URL、题库版本）；`screenshots[]`、`warnings[]`、`problems[]`、
    `elapsed_seconds`、`data`（示例试卷/导入/会话数量）。
- 说明：`warnings` 不判失败（如「无 AI 任务空态」「首轮静态资源短暂不一致已重试」）；
  任一 **pageerror / 非预期 console error / 横向溢出** 都会进入 `problems[]` 并让退出码为 1。
  脚本不写入产品数据库（仅通过 UI/公开 API 操作），不残留后台进程。

## 广场 Phase 2 + Phase 3 端到端验收（`plaza_accept.py`，WS17 / WS24）

HTTP 全链路全部经公开 API（不直接读写数据库），可选 Playwright 页面冒烟；退出码 0/1
（仅 HTTP 全链路失败时非 0；UI 问题只记录在报告 `ui.problems`，不阻断 HTTP 判定）。

### 1. 启动隔离实例（全新数据目录）

```powershell
cd E:\responsibility\study-platform
$env:STUDY_ALLOWED_HOSTS = "127.0.0.1,localhost,[::1],testserver"
$env:STUDY_INDEX_PREWARM = "0"
$env:STUDY_DATA_DIR = Join-Path $env:TEMP ("zhixu-plaza-accept-" + (Get-Random))
python -m uvicorn app.main:app --host 127.0.0.1 --port 8846
```

> 本地模式下**首个注册账号自动成为管理员**。请勿复用已有数据目录：若该实例已预先播种管理员
> （例如先跑过 demo / 整合验收账号），请用下文 `--admin-username` 指定已存在管理员。

### 2. 运行验收

```powershell
cd E:\responsibility\study-platform
python scripts/integration/plaza_accept.py --base-url http://127.0.0.1:8846
# 跳过 UI（只验 HTTP 全链路，最快）：
python scripts/integration/plaza_accept.py --base-url http://127.0.0.1:8846 --skip-ui
```

常用参数：

| 参数 | 默认值 | 说明 |
|---|---|---|
| `--base-url` | `http://127.0.0.1:8840` | 隔离实例地址 |
| `--out` | `screenshots/integration` | 截图输出目录（相对仓库根） |
| `--json` | `screenshots/integration/plaza-accept-report.json` | 报告 JSON 路径 |
| `--skip-ui` | 关闭 | 跳过 Playwright UI 冒烟（Playwright/浏览器不可用时也会自动降级为 warning） |
| `--admin-username` | 默认验收管理员 | 指定**已存在且 `role=admin`** 的管理员账号用于授权角色（适用于预先播种过的实例） |
| `--admin-password` | 默认验收密码 | `--admin-username` 对应密码 |
| `--admin-token` | 空 | 公网模式一次性管理员初始化口令（对应实例 `STUDY_ADMIN_INIT_TOKEN`）；登录失败时调用 `POST /api/auth/init-admin` |

管理员来源按顺序回退：① `--admin-username` 登录；② `--admin-token` 初始化；③ 默认账号
注册/登录（本地模式全新实例首个注册=管理员）。默认模式若发现该账号已是普通用户（实例被
预先播种），会输出可读错误并提示使用 `--admin-username` 或全新隔离实例，不会堆栈崩溃。

### 3. 覆盖点

- 账号：首账号管理员 / 作者 / 编辑 / 3 评审 / 2 学科复核；管理员授权 `plaza_roles`（学科=数学）；
- 投稿：作者创建个人题（题干/选项/答案/解析/知识点）→ 创建投稿草稿（阻断项）→ `PATCH` 来源与
  授权声明（版本 +1、阻断项清零）→ 送审（重复送审 409）；
- 审核：编辑初审通过 → 社区待核验（匿名列表/详情可见、显著标注、7 天窗口、公开投影无内部身份）；
  5 位评审独立作答 + approve（≥5 人、≥4 通过、含 ≥2 学科复核）→ 自动 `verified`；
- 发布：编辑签发精选（重复签发幂等）→ 匿名列表/详情为「平台精选」、`source=featured` 筛选命中；
- 撤回：作者不能撤回已发布（409）→ 编辑撤回整条链（重复撤回幂等）→ 匿名列表不可见、
  旧链接详情 `visible=false` 且不回显正文、活跃发布指针清空；
- 附加：评审队列/投票隐藏/一人一票、我的评审、编辑台队列（作者脱敏）、事件链、身份认证申请与审核、
  权限负向（匿名不能投稿、作者不能签发、评审员不能看编辑台/身份队列、非编辑不能看发布历史）；
- **Phase 3 质量运营**（WS21 新增接口）：
  - 质量抽检：发布后抽检 `pass` → 已抽检队列 / 首批 100% 抽样覆盖率 / `/admin/metrics`；另一题抽检
    `issue`+`high` → 自动撤回（公开不可见、发布历史 withdrawn、作者与收藏者收到通知、指标 high_issues）；
  - 互动闭环：登录用户收藏 → 我的收藏投影（favorited/available）→ 撤回后收藏者通知、回滚后恢复通知；
    纠错/举报提交 → 编辑台 pending 队列 → `resolve` → 反馈人通知（重复处理 409、非编辑 403、展示名脱敏）；
    作者对 `withdrawn` 投稿申诉 → 编辑台申诉队列 → 处理 → 作者通知（幂等/非作者 404）；
  - 回滚：同一投稿发布 v1 → 撤回 → 改版 v2 重新评审发布 → `rollback` 恢复上一已发布版本 `active`，
    公开详情读回旧版本正文、发布历史（active + withdrawn/rollback）、作者/收藏者通知；
  - 通知中心：`GET /notifications` unread → `POST /notifications/read`（ids / all）→ 未读数下降；
  - 质量指标口径与权限负向（作者/评审员 403）；
- UI 冒烟（`--skip-ui` 关闭）：匿名访客壳广场列表/详情（桌面 1440×900 + 手机 390×844，无
  pageerror/console error、无横向溢出、社区待核验/平台精选/已撤回标签、来源与授权声明、答案折叠）；
  作者 `#/submission`、评审员 `#/review`（队列 + 评审表单）、编辑 `#/plaza-admin`（待发布队列，
  显示评审阈值）；Phase 3 追加：登录用户广场详情收藏/纠错/举报按钮与操作弹层、匿名手机动作区、
  通知铃铛未读数与通知面板、编辑台「抽检 / 反馈队列 / 质量指标」三页签分区渲染（桌面 + 手机抽检）、
  作者投稿台申诉卡与已处理申诉记录。
  > 说明：`/api/auth/me` 首屏 401 与前端 official 来源能力探测（`source=__zhixu_official_probe__`）
  > 的 422 均属预期内资源错误，脚本按响应 URL 识别后记入 `ui.warnings`，不计为 UI 失败。

### 4. 产物

- 报告：`screenshots/integration/plaza-accept-report.json`，字段：
  - `ok` / `exit_code`：仅由 HTTP 全链路决定；
  - `steps[]`：每步 `name/ok/status/elapsed_ms/checks[]`（失败原因可读）；
  - `api`：`unique_endpoints/total_calls/endpoints[]`（方法、路径模板、次数、状态码集合）；
  - `ui`：`enabled/skipped/ok/steps[]/problems[]/warnings[]/screenshots[]`（与 HTTP 分开列）；
  - `data`：账号来源与模式、题目/投稿 id、公开题号、运行标记；`environment`：管理员来源、
    参数、隔离实例启动方式与必需环境变量；
  - `warnings[]` / `problems[]`。
- 截图：`screenshots/integration/plaza-*.png`（社区待核验/平台精选/已撤回 × 桌面/手机、
  作者投稿台、评审表单、编辑台待发布队列，以及 Phase 3 的详情收藏/纠错/举报、匿名手机动作区、
  通知铃铛/通知面板、编辑台抽检/反馈/指标、手机抽检、作者申诉卡等，共 23 张）。

### 5. 可重复运行与已验证结果（2026-11-21）

- 账号已存在时自动回退登录（登录优先，复跑 0 次注册请求，不触发注册限流）；每次运行新建
  个人题与投稿并在报告 `data` 标记（不复用旧投稿）；重复签发/撤回/授权按幂等处理，不因 409 失败；
- 全新隔离实例（`STUDY_DATA_DIR` 临时目录）带 UI 冒烟：HTTP 27/27 + UI 82/82 断言通过、
  49 个 API 端点 / 191 次调用、23 张截图，exit 0（约 35s；首跑会注册 8 个验收账号）；
- 同一实例复跑 `--skip-ui`：Phase 2+3 共 27/27 步通过，48 个 API 端点 / 183 次调用，
  exit 0（账号复用登录，0 次注册请求，不触发注册限流）；带 UI 复跑同样 HTTP 27/27 +
  UI 82/82 通过；匿名/登录首屏 `/api/auth/me` 的 401 与 official 来源能力探测 422 属预期，
  记入 `ui.warnings` 不判失败；
- 预先播种实例（首个账号不是本脚本管理员）：默认模式 exit 1 且给出可读错误；加
  `--admin-username/--admin-password` 后通过；
- 公网模式未初始化管理员的实例：`--admin-token` 走 `POST /api/auth/init-admin` 初始化成功，
  通过（本机 http 下会话 Cookie 的 `Secure` 属性由脚本仅对 httpx 降级；浏览器 UI 冒烟
  在公网模式 + 本机 http 下建议配合 `--skip-ui` 或使用 HTTPS）。

## 平台精选接入学习链路验收（`official_accept.py`，WS24 Phase 3）

HTTP 全链路验收（无 UI 冒烟），复用 `plaza_accept.py` 的账号/角色/投稿评审发布链路在**同一隔离实例**
上发布两条平台精选题，随后验证 `official:<public_id>` 接入检索、练习、错题、复习与工坊组卷，
以及撤回后的即时失效。全部操作经公开 HTTP API（`httpx.Client(trust_env=False)`），不直接读写数据库。

```powershell
# 与 plaza_accept 同一隔离实例（账号自动复用登录；也可单独在全新实例上运行，会自动注册账号）
python scripts/integration/official_accept.py --base-url http://127.0.0.1:8846
```

常用参数：

| 参数 | 默认值 | 说明 |
|---|---|---|
| `--base-url` | `http://127.0.0.1:8848` | 隔离实例地址 |
| `--out` | `screenshots/integration` | 输出目录（相对仓库根） |
| `--json` | `screenshots/integration/official-accept-report.json` | 报告 JSON 路径 |
| `--admin-username` / `--admin-password` | 广场验收管理员 / 验收密码 | 已存在管理员（复用 plaza_accept 账号命名，便于 accept_runner 串联） |
| `--admin-token` | 空 | 公网模式一次性管理员初始化口令 |

覆盖点：

- 发布：客观题（单选，自动判分）与主观题（作答后 pending）各一条，均走「投稿 → 声明 → 送审 →
  初审 → 5 票评审 → 签发」；
- 检索与详情：匿名可读广场公开详情；`/api/questions` 匿名 401（登录门禁）；登录用户
  `source=official` / `source=all` 关键词检索命中、`official:<public_id>` 详情含来源标签/审核状态/
  公开题号/发布版本；非法来源 422、未知 official 引用 404；
- 学习链路：客观题正确/错误作答（correct/incorrect）、主观题 pending → `/api/review/queue`
  已判分项含 official 错题、`pending_items` 含主观题；`/api/stats` 的 `official_attempts`/
  `official_practiced`/`official_wrong_count` 计数；
- 工坊：`POST /api/paperlab/papers` 用 official 引用建卷（快照 `source=official`/`public_id`/
  来源声明）→ 整卷练习逐题作答 → 提交判分（客观错题入统一错题本、主观题 pending）→ 回看；
- 撤回：编辑撤回两条精选 → `source=official` 检索/详情/作答/官方作答历史/工坊建卷立即 404
  或不可见；`/api/review/queue` 保留历史并显示「已撤回」占位提示（不回显受限正文）；
- **学段筛选（WS26/WS29）**：`/api/questions?grade=高中|高中一年级|初中|未知值` 的标准年级精确/
  学段前缀匹配、`grade_filter` 元数据（`applied/official_applied`）、未知值安全降级、题库侧
  无 grade 列时 `applied=false` 的说明；`/api/catalog` 下发 `grade_stages` 与标准年级；
- **首页精选口径（WS26）**：`/api/insight/home` 的 `official{available,total_questions,practice_pool,
  auto_gradable,attempts,practiced,wrong_count}` 与本人作答链路一致，`bank.official_practice_pool`
  与 official 口径同值，`caliber.official` 口径说明存在；
- **课堂接入精选（WS26）**：脚本内嵌只监听 127.0.0.1 的假模型服务 + `POST /api/providers` 建连接，
  `source_ref=official:<public_id>` 建课生成大纲 → 课程 `outline.sources` 记录 `kind=official` /
  `public_id` / `published_version` / 来源声明，`config.seed_source` 可回看；格式非法 422；
  撤回后再次以该素材建课由生成准入直接 404（提示已撤回）；
- **助手接入精选（WS26/WS27）**：HTTP 会话（`POST /api/assistant/conversations[/{id}/messages]`）驱动
  助手 `read_question` 工具读取 `official:<public_id>` → 工具结果 `ok=true` 且含 `public_id`/
  `published_version`/`review_status`/`source_declaration`，助手消息 `refs` 含 official 引用；
  撤回后同一工具返回结构化「已撤回」错误且不回显正文；假模型连接不可用时自动降级为
  **接口契约静态校验**并在报告/`warnings` 显式标记（不静默跳过）；
- **抽检门禁兼容（WS28）**：实例显式 `armed`（env `STUDY_PLAZA_QUALITY_GATE` 或管理员设置）时，
  发布链路前临时关闭门禁、结束前恢复原模式（报告 `data.quality_gate` 记录 origin/restore）；
  未显式配置且库中无抽检记录时不阻断；
- **课堂生成并发保障**：课堂大纲任务停在 `awaiting_outline`（产品语义）仍占用全站 `course`
  并发；脚本探测历史遗留任务并尝试 cancel，同时以管理员临时把 `course` 并发上限从默认 2 提到 20、
  结束恢复原设置（报告 `data.classroom.concurrency_guard`），保证同一实例可重复运行；
  已知产品限制：暂停态任务的 `POST /api/ai/jobs/{id}/cancel` 返回 200 但状态不变；
- 产物：`screenshots/integration/official-accept-report.json`（`steps[]` 每步断言、`api` 覆盖、
  `data` 公开题号/课堂 `outline.sources`/助手工具结果/学段筛选/门禁 origin、`environment`
  启动方式与必需环境变量），退出码 0/1。

## AI 用量与配额验收（`ai_usage_accept.py`，WS24 Phase 3）

轻量 HTTP 验收：脚本**内嵌一个只监听 `127.0.0.1` 的 OpenAI 兼容假模型服务**（支持流式/非流式并
回传固定 token usage），为普通用户创建指向它的验收连接，真实走通「创建任务 → 完成 → 用量入账 →
管理台汇总 → 配额收紧 429」链路；不访问外网、不使用真实 Key、不产生模型费用。运行结束删除临时连接
并停止假模型服务，可重复运行。

```powershell
python scripts/integration/ai_usage_accept.py --base-url http://127.0.0.1:8846
```

常用参数：

| 参数 | 默认值 | 说明 |
|---|---|---|
| `--base-url` | `http://127.0.0.1:8849` | 隔离实例地址（需本地模式 `local_open`，允许 127.0.0.1 模型地址） |
| `--out` / `--json` | `screenshots/integration` / `ai-usage-accept-report.json` | 输出目录 / 报告路径 |
| `--admin-username` / `--admin-password` | 广场验收管理员 / 验收密码 | 与 plaza_accept 共用账号命名 |
| `--normal-username` | 广场验收作者 | 普通用户（不存在时自动注册） |

覆盖点：

- 任务与用量：普通用户对已解锁题库题创建 `explain` 任务 → 假模型返回固定讲解 → 任务 `completed`
  → `GET /api/ai/usage/me` 出现该 `job_id` 用量行（kind/model/status/耗时/tokens/`input_kind`，
  不含题目内容）；
- 管理台：`GET /api/admin/ai/usage` 全站汇总（总量/失败率/P50/P95/token/按类型/按模型/分页）；
  普通用户访问 403、匿名访问本人用量 401；
- 配额：管理员 `POST /api/admin/ai/quotas` 把 `explain` 每日配额调为 1 → 普通用户再建任务返回
  429（`ai_quota_exceeded`），被拒请求不落库、不消耗配额；`reset=true` 恢复默认（60/天）；
- 产物：`screenshots/integration/ai-usage-accept-report.json`，退出码 0/1。

> 可重复运行说明：配额断言按「被拒请求前后 `used_today` 不变」判定（同一实例同日复跑时
> `used_today` 可能已 >1，仍会正确触发 429）；`explain` 默认每人每日配额 60 次，同一实例
> 同日重复运行超过该配额时任务创建会被 429 拒绝，请换全新 `STUDY_DATA_DIR` 实例复跑。

> 串联建议（`scripts/accept_runner.py -Ui`）：同一隔离实例上依次运行
> `plaza_accept.py` → `official_accept.py` → `ops_accept.py` → `ui_static_accept.py` 最省时；
> 前三个脚本共用「广场验收管理员/广场验收作者」验收账号命名，顺序变化时会自动登录复用或按
> 首个注册账号=管理员初始化，不依赖预设数据；`ui_static_accept.py` 为纯静态检查，可在任意时刻运行。
> 注意 `ops_accept.py` 会把抽检门禁显式设置为 `initial_100+armed` 验证拦截后**最终恢复为 off**
> （脚本负责的 armed 场景，不把实例留在强制抽检状态）；如需继续验证「实例显式 armed 门禁」，
> 请在它之前运行 `official_accept.py` / `plaza_accept.py`（两者会在显式 armed 时临时关闭门禁、
> 结束前恢复原模式）。

## WS30 第四轮端到端验收扩展（`ops_accept.py` / `ui_static_accept.py` / 扩展 `official_accept.py`）

WS30 在既有验收脚本上补齐第四轮能力：运维监控、抽检门禁、一键恢复、批量处理、通知增强、
精选接入课堂/助手、学段筛选、任务深链与笔记/Anki 版本字段。全部经公开 HTTP API 或纯静态断言，
`httpx.Client(trust_env=False)`；只改 `scripts/integration/` 与 `docs/验收记录.md`，未改
`app/`、`web/`、`tests/`。报告 JSON 落 `screenshots/integration/`（目录已 gitignore）。

### 1. 启动隔离实例（全新数据目录，建议显式 armed 门禁）

```powershell
cd E:\responsibility\study-platform
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'
$env:STUDY_ALLOWED_HOSTS='127.0.0.1,localhost,[::1],testserver'
$env:STUDY_INDEX_PREWARM='0'
# 建议：实例启动时显式 armed，验证「抽检门禁兼容」分支（official/plaza 临时关闭并恢复；
# ops 显式 armed 验证后最终恢复 off）
$env:STUDY_PLAZA_QUALITY_GATE='initial_100'
$env:STUDY_DATA_DIR = Join-Path $env:TEMP ('zhixu-ws30-' + (Get-Random))
python -m uvicorn app.main:app --host 127.0.0.1 --port 8856
```

> 抽检门禁语义（WS28 交付）：模式 `off|initial_100(默认)|always`；显式配置（env
> `STUDY_PLAZA_QUALITY_GATE` 或管理员 `PUT /api/plaza/admin/quality/gate {mode,armed}`）即激活；
> 未显式配置且库中无抽检记录时不阻断（兼容既有脚本首跑发布）。触发流程：
> `verified → publish 409(detail.code=quality_gate_blocked)` → `POST /api/plaza/admin/quality/checks
> {submission_id,decision:"pass"}`（`phase=pre_publish`，响应 `gate.publish_ready=true`）→ 再 publish 200。

### 2. 运行验收（建议顺序）

```powershell
# ① 平台精选接入（含课堂/助手 official + 学段筛选/首页口径；实例显式 armed 时自动兼容）
python scripts/integration/official_accept.py --base-url http://127.0.0.1:8856
# ② 质量运营 + 运维监控（抽检门禁/一键恢复/批量/通知/指标；结束恢复门禁 off）
python scripts/integration/ops_accept.py --base-url http://127.0.0.1:8856
# ③ 前端静态验收（不启动服务，可任意时刻运行；node --check 需要本机 Node.js）
python scripts/integration/ui_static_accept.py
# ④（可选）既有广场全链路回归（显式 armed 时自动临时关闭/恢复门禁）
python scripts/integration/plaza_accept.py --base-url http://127.0.0.1:8856 --skip-ui
```

`ops_accept.py` 常用参数：`--base-url`（默认 8856）、`--out`、`--json`、`--admin-username` /
`--admin-password` / `--admin-token`（与 plaza_accept 一致，预先播种实例复用已存在管理员）。
`ui_static_accept.py` 常用参数：`--out`、`--json`。

### 3. `ops_accept.py` 覆盖点（17 步）

- **运维监控**：`GET /api/admin/monitor` 管理员可读、普通用户 403、匿名 401；结构
  `ok/status/checks{tasks,ai,media,data,plaza}/build/thresholds/cache`、短缓存与 `refresh=1`；
  构造 20 条待处理纠错使 `checks.plaza` 单组降级（warn/error）且整体不 500、清理后恢复、
  其他组结构完整；响应不含敏感 Key；
- **批量处理**：`POST /api/plaza/admin/{issues|reports|appeals}/batch` 部分成功语义
  （`updated/skipped`、`already_handled`/`not_found`、重复批量不重复通知）、反馈人通知、
  事件链与 `metrics.ops.batch` 口径；
- **抽检门禁**：管理员显式 `{mode:initial_100,armed:true}`；未抽检签发 409（code/guidance/
  override 入口/gate 状态/blocked 审计）→ 签发前抽检 pass（`phase=pre_publish`、
  `publish_ready=true`）→ 再签发 200（`satisfied/pass_check`）；override 缺理由 422、带理由
  200（`quality_gate_override` 审计事件+指标）；普通用户改门禁 403；
- **一键恢复**：抽检 `issue+high` 自动撤回 → 公开不可见 → 收藏者/作者通知 → `restore`
  （`confirm_resolved=true`）恢复公开（`restore_source=quality_auto_withdraw`、事件
  `release_restored`、收藏者 `favorite_restored`）→ 重复恢复幂等不重复通知；编辑撤回后恢复
  （`editor_withdraw`）、未确认 422、未知发布 404、活跃发布幂等；改版 v2 已活跃时恢复旧发布
  409（`active_version`/`restore_version`，避免内容回退）；
- **通知增强**：`unread-count` 与列表同口径（`by_kind` 求和/`kinds_total`）、按 kind 全读、
  按 ids 读取（重复 id 去重）、未知 kind 422、`all=true` 全读；同一事件重复触发在去重窗口内只
  产生一条通知；
- **指标补充**：`/api/plaza/admin/metrics` 的 `quality.gate`（blocked/passed/override/
  pilot_limit）、`withdrawals.restorations`/`restorations`、`ops.batch`（batches/requested/
  updated/skipped/by_kind）、`notifications`（total=unread+read）、`caliber` 口径说明；
- **收尾**：抽检门禁恢复 `off`（脚本负责显式 armed 场景，不把实例留在强制抽检状态）。

### 4. `ui_static_accept.py` 覆盖点（6 步，纯静态）

- `node --check` 遍历 `web/js/**/*.js` + `vm.SourceTextModule` 真 ESM 解析（Node 缺失时记
  warning 并失败该步，不静默跳过）；
- 任务深链：`tasks.js` 的 `#/paperlab?import=|tab=papers&paper=`、`#/classroom?course=`、
  `#/notes?note=|document=`、`#/mybank?edit=`、`#/upload?upload=` 与各页面 `params.get(...)`
  读取一致；
- 平台精选配图/纠错：`images[].accessible/url` 公开图渲染、`figure_urls` 兼容、
  `figure_notice`/加载失败降级、`[图N]/[公式N]` 占位、`POST /api/plaza/questions/{id}/issues`
  字段白名单/登录引导/成功后 toast、精选编号跳广场；
- 学段筛选：能力探测、`catalog.grade_stages` 选项、URL 恢复/重置/清除芯片、
  `grade_filter.applied=false` 降级提示；后端 `/api/questions?grade=` 与 official 学段口径；
- 笔记/Anki：统一题目引用（`bank:/app:/official:`）链接与来源版本字段，`.apkg` 模型字段不变；
- locale：中英文键集合/占位符一致、前端字面量键齐全、WS30 命名空间键量与非空。

### 5. 报告产物

| 报告 | 内容 |
|---|---|
| `screenshots/integration/ops-accept-report.json` | `steps[]/checks[]`、`api`（端点/调用）、`data{baseline,monitor,batch,gate,restore,notifications,metrics}`、`warnings/problems`、`environment`（门禁语义与启动方式） |
| `screenshots/integration/official-accept-report.json` | 追加 `data{classroom,assistant,grade,insight,quality_gate}`（课堂 outline.sources、助手工具结果、学段筛选元数据、门禁 origin/恢复） |
| `screenshots/integration/ui-static-accept-report.json` | `steps[]/checks[]`、`environment`（Node 版本、检查范围）、`problems/warnings` |

> 可重复运行：账号登录优先（复跑 0 次注册请求）；`ops_accept.py` 会清理历史失败运行遗留的
> 待处理纠错积压并断言实例累计指标（`>=` 口径），同一实例复跑通过。已知缺陷：运维监控
> `withdrawn` 撤回传播抽样对「同一 public_id 的新版本重新签发（v2 active + v1 withdrawn）」
> 会误报「撤回泄露」（既有 `app/monitor.py` 口径问题，非本脚本数据），`ops_accept.py` 记录为
> warning 不判断失败，待 WS27 修复。

## 清理

- 停止终端 A/B 的 uvicorn 与假模型进程（`Ctrl+C`）；
- 隔离数据目录是系统临时目录（`$env:TEMP\zhixu-classroom-accept-*`），可自行删除；
- 脚本内导出的临时文件使用系统临时目录并在流程结束时清理。
