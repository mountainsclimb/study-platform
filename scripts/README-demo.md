# 本机演示数据（无需任何 AI Key）

`scripts/seed_demo.py` 通过**真实 HTTP API** 给一个账号写入一套「示例」数据，让你在不配置任何
AI Key 的情况下，打开知序就能看到 AI 课堂、试卷工坊、我的题库、AI 助手等整合模块的完整效果。
脚本不直接写数据库、不改应用代码，也不访问外网（只访问 `--base-url` 指向的本机服务）。

> 所有示例数据统一带「示例」标记：课程/试卷/会话标题以 `示例：` 开头，题干与知识点带
> 「示例 / 演示数据」标签。`--reset` 只按这些标记识别并删除本脚本创建的数据，不会碰你的真实数据。

## 1. 前置条件

- 已安装依赖：`pip install -r requirements.txt`（脚本用到 `httpx` 与 `Pillow`，均已在依赖中）。
- 知序服务已启动（默认 `http://127.0.0.1:8765`）。例如：

```powershell
# 在 study-platform 目录
python -m uvicorn app.main:app --host 127.0.0.1 --port 8765
```

- 可选：只读高考题库用于「示例卷」里的 1-2 道真题。没有题库也能跑（示例卷只使用应用题库题），
  指定方式：启动服务前设置 `STUDY_BANK_DIR`（例如 `...\releases\bank-20261120-r3`）。

## 2. 用法

```powershell
# 第一次：登录既有账号；账号不存在时自动注册 demo 演示账号
python scripts/seed_demo.py --base-url http://127.0.0.1:8765 --register

# 只输出 JSON（便于自动化/验收；失败退出码非 0）
python scripts/seed_demo.py --base-url http://127.0.0.1:8765 --register --json

# 清空本脚本创建的示例数据后重建（交互确认；--yes 跳过确认）
python scripts/seed_demo.py --base-url http://127.0.0.1:8765 --register --reset --yes

# 使用已有账号（不加 --register；登录失败会给出中文提示）
python scripts/seed_demo.py --base-url http://127.0.0.1:8765 --username 我的账号 --password 我的密码
```

参数：

| 参数 | 默认 | 说明 |
|---|---|---|
| `--base-url` | `http://127.0.0.1:8765` | 已启动的知序服务地址 |
| `--username` | `demo` | 登录账号 |
| `--password` | `demo-pass-12345` | 登录密码（演示账号） |
| `--register` | 关闭 | 账号不存在时自动注册（使用上面的用户名/密码） |
| `--reset` | 关闭 | 先清理示例数据再重建（按标题识别，不删真实数据） |
| `--yes` | 关闭 | `--reset` 跳过交互确认 |
| `--json` | 关闭 | 只输出 JSON 摘要 |

## 3. 会创建什么

所有内容均明确标注「示例」，重复执行默认**跳过已存在数据**（幂等）。

### 3.1 AI 课堂

- 用脚本构造 importer 兼容 ZIP（`manifest.json` + `course.json` + `scenes/*.json`），
  调用 `POST /api/classroom/import` 导入课程 **「示例：光合作用互动课堂」**；
- 3 个场景，全部离线可播放，不需要模型：
  - `slide`：文字 + 公式 + spotlight/laser/speech 动作；
  - `quiz`：2 道示例题（单选/多选），可直接作答判分；
  - `pbl`：结构化项目（背景/目标/角色/2 个里程碑/评价 rubric/导师提示）。

### 3.2 试卷工坊 / 我的题库

- Pillow 生成一张 900×1200 的示例试卷页 PNG（标题/题干/选项/「示例数据」水印），
  调用 `POST /api/paperlab/imports` 上传为 **「示例：函数基础自测卷页」**；
- `PUT .../pages/1/annotation` 写入 2 个题目框 + 1 个答题区 →
  `POST .../resegment` 裁图生成草稿 → `PATCH .../items/{id}` 改写成 2 道示例题
  （填空 + 单选，题干带「示例」）→ `POST .../commit` 入库到「我的题库」；
- 建卷 **「示例：函数基础自测卷」**（2 道应用题库题 + 最多 2 道高考真题 `bank:<qid>`）并发布；
- 生成一张 **A4 答题卡**（`POST /api/paperlab/papers/{id}/cards`）；
- 创建并提交一次整卷练习（`POST /papers/{id}/attempts` + `POST /attempts/{id}/submit`），
  得到一份示例成绩（客观题按参考答案判分，不调用 AI；真题按演示答案作答）。

### 3.3 AI 助手

- `POST /api/assistant/conversations` 创建一个空会话 **「示例：如何复习错题」**；
- 不调用 AI；配置模型后在该会话里即可开始对话。

### 3.4 不伪造的部分

- 任务中心不创建任何假任务；学情分析展示真实统计（演示账号初始没有真实作答数据时为空态）；
- 所有示例数据都来自真实 API 写入，未伪造 AI 生成内容；示例题的答案/解析是脚本写死的
  「示例解析」，不是模型输出。

## 4. 清理（`--reset`）

`--reset` 会先列出/清理本脚本创建的示例数据，再重新创建。识别规则：

| 数据 | 清理方式 |
|---|---|
| AI 课堂课程 | 标题含「示例」→ `DELETE /api/classroom/courses/{id}` |
| 试卷（含答题卡/练习记录级联） | 标题含「示例」→ 先删答题卡再 `DELETE /api/paperlab/papers/{id}` |
| 我的题库应用题 | 题干或知识点含「示例」→ `DELETE /api/paperlab/questions/{id}`（软删除） |
| AI 助手会话 | 标题含「示例」→ `DELETE /api/assistant/conversations/{id}` |
| **试卷识别导入记录** | **后端暂无删除 API**：脚本保留并复用它们（不会重复上传），在 `--reset` 摘要中明确列出；如需彻底清理可重建演示数据库目录或等后续版本提供删除接口 |

展示即将删除的数据后会要求输入 `yes` 确认（`--yes` 跳过）。`--reset` 不会删除任何不带
「示例」标记的数据。

幂等性：不传 `--reset` 重复执行时，已存在的课程/导入/试卷/答题卡/练习/会话都会跳过；
`--reset` 后重建时复用已存在的示例导入记录，但会新建课程/试卷/答题卡/练习。
注意：`--reset` 会软删除示例题，脚本会自动用 `POST /api/paperlab/questions` 补齐，保证示例卷可重建。

## 5. 入口

脚本结束会打印入口地址，常用：

| 页面 | 地址 |
|---|---|
| 首页工作台 | `/#/` |
| AI 课堂列表 / 播放器 | `/#/classroom`、`/#/classroom?course=<id>&view=play` |
| 我的题库 | `/#/mybank` |
| 试卷工坊 / 试卷详情 | `/#/paperlab?tab=papers`、`/#/paperlab?tab=papers&paper=<id>` |
| 答题卡 SVG | `/api/paperlab/cards/<card_id>/svg` |
| 学情分析 / AI 助手 / 任务中心 | `/#/insight`、`/#/assistant`、`/#/tasks` |

## 6. 验收与截图

隔离实例（`STUDY_BANK_DIR=...\releases\bank-20261120-r3` + 临时 `STUDY_DATA_DIR`，端口 8831）
上实测：

- `python scripts/seed_demo.py --base-url http://127.0.0.1:8831 --register`
  → AI 课堂/试卷工坊/AI 助手三个模块 `created`；
- 紧接着重复执行 → 三个模块 `skipped`，`--json` 摘要中 `本次新建 0 题`，没有重复课程/试卷/导入/会话；
- `--reset --yes` → 清理 `courses:1, papers:1, questions:2, cards:1, conversations:1`，
  保留并复用 1 条导入记录，随后重建成功（新建 1 门课程/1 张试卷/1 张答题卡/1 次练习）；
- reset 后再执行一次 → 三个模块 `skipped`，示例题按「示例」标记**复用 2 道**（不再重复创建），
  「我的题库」仍只有 2 道示例题；
- 数据自检：课堂 `slide+quiz+pbl` 3 场景；示例卷 4 题/20 分（2 道应用题库题 + 2 道高考真题）；
  答题卡 SVG 200 且包含「示例」；整卷练习 `submitted`，示例成绩 **15/20**。
- Playwright 截图（`screenshots/integration/`，20 张，`seed-demo-evidence-report.json` 全部 checks 通过）：
  - 桌面 1440×900：`seed-home.png`、`seed-classroom-list.png`、`seed-classroom-player.png`、
    `seed-mybank.png`、`seed-paperlab.png`、`seed-paper-detail.png`、`seed-card.png`、
    `seed-insight.png`、`seed-assistant.png`、`seed-tasks.png`
  - 手机 390×844：同名 `-mobile.png`（答题卡为 `seed-card-mobile.png`）；手机端无横向溢出、无 JS 异常。

## 7. 常见问题

- **连不上服务**：确认 `--base-url` 与启动端口一致，浏览器可打开 `/api/health`。
- **登录失败**：加 `--register`，或用 `--username/--password` 指定已有账号（演示账号默认
  `demo / demo-pass-12345`）。
- **没有 AI Key 能不能用**：能。本脚本创建的数据都可在播放器/题库/试卷/答题卡/练习中直接查看与
  操作；只有「AI 生成新内容」「AI 对话」「AI 组卷」等需要你在「模型设置」里配置模型。
- **高考真题缺失**：服务未挂载题库时示例卷自动只用应用题库题，不影响其它模块。
- **答题卡页眉标题被裁切**：当前答题卡模板对长标题按页宽截断，示例卡使用短标题「示例答题卡」；
  如果看到既有页面上的答题卡入口提示「后续版本提供」，说明前端答题卡入口还在并行开发中，
  可直接打开上面的 `card_svg` 地址查看已生成的答题卡。
- **任务中心/学情为空**：正常——脚本不伪造任务，学情只统计真实作答。
