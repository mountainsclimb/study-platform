# 知序 · AI 题库与知识清单（v2 · P0–P3 全量升级版）

本机可运行、亦支持容器化公网部署的多用户学习工作台。读取上级目录高考题库（7,659 题，保持只读），账号、学习记录、知识点树、模型配置、上传资料和版本化知识笔记保存在 `data/`。

## 启动

在 PowerShell 执行：

```powershell
cd E:\responsibility\study-platform
.\start.ps1
```

也可双击 `start.cmd`，或执行：

```powershell
python -m uvicorn app.main:app --host 127.0.0.1 --port 8765
```

浏览器访问 <http://127.0.0.1:8765>。题库版本由 `高考题库_按学科/releases/CURRENT` 指针决定（当前
`bank-20261120-r3`），`start.ps1` 会把它解析为实际的发布包目录；不设 `STUDY_BANK_DIR` 时自动跟随指针，
显式指向题库根目录时按基线库运行，指向具体发布包时按该版本运行（可用 `STUDY_BANK_FOLLOW_POINTER=1`
强制跟随指针，`STUDY_EXPECTED_BANK_VERSION` 声明期望版本）。

本地模式下首次注册的账号自动成为管理员，后续注册为普通学习者；
公网模式（`STUDY_PUBLIC_MODE=1`）默认关闭注册，须先配置 `STUDY_ADMIN_INIT_TOKEN` 并调用 `/api/auth/init-admin` 完成一次性管理员初始化，再由管理员在「内容管理 → 账号与注册策略」显式开放注册。详见[部署指南](deploy/README.md)。

## 数据来源与许可

「志愿查询」使用的 GaokaoCompass 数据集来自 Hugging Face（`choucsan/Gaokao-Compass-11M`），
**MIT License，Copyright (c) 2025 choucisan**，整理自各省教育考试院、阳光高考平台等公开渠道。
本项目只读取其发布的 CSV，本地重新清洗并生成只读库，未修改上游数据；完整版权、许可证全文、
本项目对数据的处理方式与非官方免责声明见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
分发本项目或派生数据时请一并保留该文件与 MIT 声明。上游交互演示站的前端代码未附许可证，本项目未复制其代码。

## 核心能力

1. **题库检索与知识点体系**：
   - 应用库内置 `bank_fts`（FTS5 `trigram` 全文索引，自动校验源库 SHA-256）+ 跨库 `ATTACH` 批量查询（零 N+1）。
   - 内置九大学科课标三级知识点树（章 → 节 → 知识点），支持多选题型/地区、年份区间、知识点树、题型解法标签、有效答案/原卷解析/AI 讲解缓存、我的状态及三源难度（管理员标注 → 统计难度 ≥20 人次 → 可选 AI 预估）筛选。
   - 支持管理员 CSV/JSON 导入知识点、AI 批量打标（费用估算、检查点续跑、低置信度进复核队列）。
2. **门控与错题定时穿插复习**：
   - 作答前后端严格屏蔽答案与解析；答案与解析独立展开，提交后选项标出 `✓ / ✗`，支持历次作答记录与 Pillow 空白图拦截。
   - 搜索结果支持在第 4、9 位定时穿插符合当前筛选（或同知识点）的到期错题卡，不污染检索总数。
   - 掌握度与复习间隔自动联动（未掌握随时可标、比较掌握 ×2、熟练掌握 ×2.5、连对 3 次自动归档），支持错因分类、知识点聚合、连续复习模式及打印导出 PDF。
3. **模型管理、SSE 流式与交叉验证 2.0**：
   - 支持编辑连接、向厂商 `/models` 真实拉取模型列表、10 家预设厂商、模型能力标签（👁视觉 / JSON / 🧠推理）与单价配置、Token 用量账本、数据库级自定义 HTTPS 域名白名单。
   - **交叉验证 2.0**：双模型独立结构化求解 → 零成本程序化比对（选项集合 / 数值 `<1e-6` / `sympy` 表达式等价 / 原卷冲突提示）→ 独立裁判模型 C（或核验模型 B，绝不由主模型 A 自判）定位分歧步骤 → 可选 1 轮互审 → 🟢/🟡/🔴 置信度徽章。
   - **6 种讲解模式**（详细解析、标准步骤、题型教学、知识拓展、启发引导、变式练习），结果按账号与题目自动持久化复用，支持 SSE 流式增量渲染与指数退避轮询兜底。
   - 可在部署时开启社区讲解。通过核验的合格题库讲解可由用户主动分享，管理员可设为平台精选。
4. **拍题双步流水线**：
   - 支持多图、相机调起、`Ctrl+V` 粘贴截图、90° 旋转与交互式 Canvas **框选裁剪**。
   - 先由视觉模型识别题干（支持双模型差异提示）→ 用户核对确认 → FTS5 + 向量混合检索题库相似真题 → 多模式求解，并支持将自传拍题加入错题本。
   - **图片试题转 LaTeX（公式识别后处理）**：拍题/扫描页的 math 块可经 `STUDY_FORMULA_OCR=on` 用 RapidLaTeXOCR 填 `latex`（默认关闭，失败/超时保持为空并标「需校对」），前端可用 MathLive 可视化修正后再确认；旧 `app/ai/surya_latex.py` 适配层已不接业务路由，按方案保留一个版本周期后删除。可选依赖见 `requirements-formula.txt` 与 `requirements-surya.txt`。
5. **知识清单受控工作流 Agent 与方格提示词**：
   - 标题层级感知结构化分块（保留 `heading_path` 与 DOCX 表格顺序）；扫描 PDF 支持 `pypdfium2` 渲染 + 用户确认页数后视觉 OCR 与人工校对。
   - L0–L5 分层提示词与 6 维度「方格选择」组合器、最终要求预览、「我的模板」存取与公开分享。管理员可推荐已公开模板。
   - 可按任务开启逐块 JSON 抽取。配置 Embedding 服务后，新上传或 OCR 校对的资料自动生成稠密向量。
   - 支持多文档合并、官方清单+我的补充、从错题生成 3 种模式；内置有限工具（题库例题/官方清单/个人错题/混合向量检索）；支持「人在环中」提纲暂停编辑与最多 3 并发分节生成、自动对齐课标树与 `[资料特有]` 标注、版本历史回滚，以及 Markdown / PDF / Anki CSV / 页面内 SVG 思维导图渲染与导出。
6. **评测集、账号安全与生产运维（P3）**：
   - **100 题黄金评测集**：[eval/golden.jsonl](file:///E:/responsibility/study-platform/eval/golden.jsonl)（覆盖九科可靠答案）与 `python -m app.eval`（统计单模型准确率、交叉验证准确率、一致率、「一致但错」比例及 A/B 报告对比）。`--build-golden --strata` 生成分层评测集 v2（纯文本 / 原卷图与公式 / 阅读材料与共用题干 / 多问题组），报告单列题库输入完整率、有效核验率与费用。
   - **账号安全**：注册开关与一次性管理员初始化、跨进程登录/注册/恢复码限流（登录/重置新增用户名维度全局限流与常量时间假 `scrypt` 防枚举，改密/生成恢复码按账号限流）、修改密码、8 组一次性找回密码恢复码（哈希存库，离线可用）；公网模式强制 Secure Cookie。
   - **在线热备份与恢复演练**：`python -m app.ops backup / verify / restore`（SQLite 在线 `.backup()` + SHA-256 清单校验 + `PRAGMA integrity_check`）及管理页一键热备份；默认写入数据目录 `backups/`，默认排除 `api-key.key`，解压前做 Windows/POSIX 双重 Zip-Slip 校验并先暂存后原子替换。
   - **存活与就绪**：`/api/health/live` 供容器探针；`/api/health/ready` 逐个核对题库版本、应用库、对象存储、Redis 与 Worker 心跳，返回降级依赖与受影响功能；未鉴权只返回精简状态，管理员或 `X-Health-Token`（`STUDY_HEALTH_TOKEN`）可读完整详情。Redis 检查支持 `rediss://` TLS、URL 编码密码与 ACL。
   - **公网容器化部署**：支持 PostgreSQL `pg_trgm`、Redis/arq 队列、S3/MinIO 存储与 Caddy 自动 HTTPS。可选 FSRS-lite 和其他运行配置见 [部署指南](deploy/README.md)。
7. **原卷显示、校勘与任务可靠性（整体优化 P0–P2）**：
   - **内容块与题组**：题干、选项、材料、答案与解析按有序内容块渲染；LaTeX 走本地 KaTeX，失败回退原卷公式图；题组共用内容只显示一次，按原卷顺序导航各小问。
   - **素材逐题授权**：图片请求携带题库版本、当前小题与素材 ID；答案图、解析图只对当前小题作答后可见，只答第（2）问不会解锁第（3）问；管理员原卷预览走单独授权入口。
   - **表达式核验加固**：数值核验支持零值、科学计数法、单位与量纲换算；同形异量纲、单位缺失、超长或超出运算预算的表达式一律降为待核验；主模型不能担任同一任务的裁判。
   - **报错与校勘闭环**：题目旁「报告问题」自动带题库版本、题号、字段与问题类型；管理员在「内容管理」按学科、风险、类型和版本筛选，按「待核对 → 待修订 / 确认无误 → 待发布 → 已发布」推进，可保留证据临时隔离与撤销，并导出已发布修订供题库重建。
   - **公开答案止损**：题干、选项、共用材料或只读成员块中检测到「【答案】/【详解】/【解析】/【解答】/参考答案：」时，先停止该题作答、详情返回 409 并从检索与相似题中排除；题组共用材料命中时题组接口 409。该门禁在题库重建修复合入内容后自动解除。
   - **检索与局部失败**：筛选变化自动清理失效选中项，返回列表恢复有效定位；到期错题条和历史讲解加载失败只影响对应区块并单独重试。
   - **OCR 分页续做**：按资料版本和页码逐页区分「已有文本 / 待识别 / 已识别 / 人工校对 / 识别失败」，批次按页合并、失败页单独重试，人工校对页不被覆盖，覆盖度与可继续识别范围在页面可见。
   - **任务状态与幂等**：区分等待、执行、取消请求、已取消、需处理与已完成；取消前已保存的产物按任务规则保留；笔记与讲解按 `job_id` 幂等写入，同一任务重试只保留一份最终产物；任务结果记录输入引用、题库版本、提示词版本与用量。

8. **版本与界面（下一轮优化第一轮）**：
   - **版本对齐**：内容指纹构建号写入 OpenAPI、`/api/health`、`/api/health/live`、`/api/health/ready`
     与每个响应的 `X-Zhixu-Build` 头；就绪信息同时给出实读题库版本、来源（`pointer:` / `env` / `default`）
     与目录，期望版本不一致时降级。可用 `STUDY_BUILD_ID` 覆盖构建号。
   - **题库练习页**：桌面为「结果列 + 阅读列」工作区，常用筛选只留搜索/学科/年份/题型，「更多筛选」收纳其余
     条件并显示已选数量；结果卡展示来源、原卷题号与题组标签；手机默认停在结果列表，进题后可返回并恢复位置。
   - **题目阅读页**：按「原卷出处与状态 → 题组小问 → 共用材料（只出现一次、可折叠）→ 题干 → 选项 → 底部操作条」
     排列；题号统一为「原卷第 N 题 · 第（3）问」；小问入口按实时状态禁用；答案与解析按需展开、互不覆盖。
   - **可访问性**：新配色对比度 ≥4.5:1、移动端主控件 ≥44px、焦点可见、侧栏收起时 `inert`、打印只保留阅读内容。
   - **界面基线脚本**：`python scripts/ui_check.py --base-url http://127.0.0.1:8765` 输出桌面/手机截图与几何指标
     （筛选卡高、首条结果位置、阅读列宽、视图切换、打印媒体）。

9. **自定义 API 地址与厂商（本轮）**：
   - 地址策略分三层：本地模式默认 `local_open`（直接放行任意 HTTPS/HTTP 中转站、自定义端口、Ollama/LM Studio/vLLM 等本地模型，可留空 Key）；公网默认 `public_https`（任意公网 HTTPS + DNS 解析校验 + IP Pinning 防 DNS Rebinding）；严格托管可设 `STUDY_AI_URL_POLICY=allowlist` 配合 `STUDY_AI_ALLOWED_HOSTS`。云元数据地址任何模式都禁止。
   - 保存前即可「拉取厂商模型」与「测试连接」（无需先落库），自动清洗误粘贴的 `/chat/completions` 等尾部路径，模型列表不截断；支持自定义鉴权方式（Azure `api-key`、Anthropic `x-api-key`、Gemini 原生 query key、无鉴权本地服务）与自定义请求头（≤8 条，加密存储）。
   - 支持 OpenAI 兼容、Anthropic、Gemini 兼容 / 原生、Azure OpenAI；`max_tokens`/`max_completion_tokens` 自动适配；推理模型（DeepSeek-R1/QwQ 等）的思考内容单独推送并显示「思考中」，不混入正文；错误响应与 Key 全程脱敏。
   - 「API 状态与默认模型」行内可编辑各任务的默认模型/核验模型并可清空；切换模型或模式后切题不再回弹。
   - 语义检索（Embedding）按账号配置：关闭 / 复用已有连接 / 独立地址，设置页可测试向量接口，就绪信息展示状态。
10. **安全与运维（本轮）**：
   - `/api/health/ready` 分级：匿名只返回各项可用性；完整详情（题库路径/题量/依赖诊断/Embedding 状态）仅管理员或 `X-Health-Token` 探针口令可见。
   - 登录/重置/改密/恢复码均有原子限流（含用户名全局桶与常量时间假校验防枚举）；公网模式强制 Secure Cookie。
   - Redis 就绪检查支持 `rediss://` TLS、URL 编码密码与 Redis 6+ ACL；Compose 默认启用 Redis 密码。
   - 备份：默认写入数据目录 `backups/`，默认排除 `api-key.key`（`--include-key` 才包含），还原前校验归档成员（防 Zip-Slip）并原子替换；报错截图目录纳入备份与 S3 核查。
   - 题图响应保留 `private, max-age=3600` 缓存并支持 ETag/304；构建号覆盖 `styles-pages.css` 与提示词模板，旧标签页提示更准确。
   - 检索索引启动时预热；题库切换后首次请求重建一次，之后默认检索约 35ms、关键词约 34ms（7,659 题本机 SQLite）。

11. **志愿查询（GaokaoCompass 数据接入）**：
    - 数据来自 Hugging Face 数据集 `choucsan/Gaokao-Compass-11M`（MIT 许可，31 省 2017–2025，约 1,135 万行），由 `scripts/import_admission.py` 下载并导入只读的 `data/admission.sqlite3`；应用只读该库，不修改源数据，`data/` 不入库。
    - 页面「志愿查询」提供一分一段（分数 → 位次与分布图）、按分数/位次找学校（冲 / 稳 / 保）、院校详情（投档线、专业录取线、招生计划、历年趋势）与招生计划检索；SVG 折线图与筛选器均为原生实现，不引入新依赖。
    - 数据边界：2017–2021 多省缺少位次与批次线（按位次匹配时提示改用分数）；2023–2025 部分省份缺少招生计划人数（2023、2024 各约一半省年、2025 有 6 个省年完全没有计划数），界面显示「未提供」而非补零；个别省年科类整列为空时自动忽略科类筛选并说明。页面顶部按当前省份年份显示「位次/批次线/科类/计划人数」四项完整度，并固定标注数据来源、MIT 许可与非官方免责说明。
    - 未导入数据时页面与接口给出明确操作指引（`GET /api/admission/meta` 返回 `ready:false`，数据接口 503），不伪造空结果。
    - **官方渠道补采批次线**：`scripts/collect_official_data.py` 从教育在线（eol.cn）公开页面采集 2020–2026 年各省批次控制线（1,170 条，31 省全覆盖），保留来源 URL、抓取时间与 SHA-256，经 `scripts/import_official_data.py` 写入独立的 `data/admission-official.sqlite3`；主数据缺少批次线的年份（如 2020、2021）由它补齐，页面标注「官方补采」或「官方补采，按分数匹配批次」，接口新增 `GET /api/admission/control-lines`。
    - 2017–2019 年批次线与 2017–2020 年一分一段仍未补齐：公开页面多为图片/PDF，阳光高考有 WAF（412），部分省考试院栏目为 JS/ASP 动态列表，需逐省适配与 OCR 视觉识别，详见验收记录。
    - **数据缺失声明**：`scripts/report_data_gaps.py` 逐省年统计位次 / 批次线 / 计划数 / 科类 / 缺表情况，生成 `数据缺失声明.md` 与机器可读的 `data/admission-gaps.json`；接口 `GET /api/admission/gaps`，页面「志愿查询」底部可展开查看总览与当前省年状态（随筛选实时更新），缺失处一律如实标注、不推断。

12. **异步任务与真流式（本轮）**：
   - 讲解、笔记、拍题、判分与 OCR 任务共用任务卡：流式输出采用固定壳 + 150ms 节流局部更新，流式过程中「取消任务」按钮保持可用；显示阶段进度百分比与已耗时；断线自动重连（2 次）后回退轮询；顶栏显示后台任务胶囊。
   - Redis 模式使用 Pub/Sub 跨进程推送增量（不再每 0.75s 查库重发全量）；未配置 Redis 时自动回退轮询，任务与结果不受影响。
   - PDF 扫描件 OCR：≤3 页同步逐页保存；>3 页自动转为后台任务，逐页落库、实时进度、可取消、失败页可断点续跑；渲染清晰度 144 DPI。
   - 后台管理页三个分区按需加载，操作后只局部刷新；错题/笔记/管理列表的筛选请求带防竞态取消。
   - 数学比对使用常驻 SymPy 子进程池（冷启动 2.3s → 0.7s，热态约 4ms/次；池不可用时自动回退）；离线评测接通裁判模型、剔除字面猜测、支持逐题断点续评。

13. **物理演示（physics-in-motion 内核接入，方案 B）**：
    - 只引入上游 `physics/`（求解器 + 守恒校验）与 `sim/`（场景构建器 + `SimResult`）纯 TS 物理层，用 esbuild 打成 **38KB 浏览器 ESM**（`web/vendor/physics-in-motion/physics-core.js`），不引入 React/Next 构建链；页面、SVG 渲染、播放器、图表与公式面板均为原生实现。
    - 10 个自研教学案例（斜面、刹车、平抛、单摆、碰撞、传送带、圆轨道、波、折射、带电粒子）由前端按 `visualization` 参数即时计算动画，动画/图表/物理量/公式读取同一份 `SimResult`；每个演示展示自动验证结果（守恒律、事件顺序、答案一致性，全部 3/3–5/5 通过）。
    - 题目详情页对物理题增加「物理演示」入口；演示页支持播放/暂停/单步/倍速/时间轴/事件标记、速度/加速度/受力/合力/轨迹/坐标/标注开关、v-t/s-t/a-t 图表与能量柱状图、分步解析（点击步骤跳转动画）。
    - 来源与许可：physics-in-motion（MIT，Copyright (c) 2026 xuxuezhou）已登记到 `THIRD_PARTY_NOTICES.md`，vendored 目录保留上游 LICENSE 与构建说明；上游题库内容未导入，仅使用其自研教学案例。

14. **AI 课堂 / 试卷工坊 / 学情分析 / AI 助手（整合 OpenMAIC、Oh My Paper、小鹿老师）**：
    - **AI 课堂**（`#/classroom`）：主题/资料/学段/模式 → 大纲（可编辑确认）→ 逐场景生成
      （幻灯片 / 测验 / 交互 HTML / PBL）。播放器支持 1000×562.5 舞台、KaTeX 公式、动作时间轴
      （聚光灯 / 激光 / 高亮 / 白板 SVG / 切场 / 语音旁白字幕）、浏览器 TTS、测验判分、交互场景
      沙箱 iframe（独立 CSP、`allow-scripts` 且无 same-origin）、PBL 项目结构、AI 老师对话与多智能体
      课堂（角色名册、讨论/自由问答/讲授、白板）。支持 PPTX / 离线单文件 HTML / 课程 ZIP / 教学资源包
      （PPTX + 交互场景独立网页）导出与 ZIP 导入、可选 TTS 音频与可选配图生成；无模型时所有 AI 操作禁用
      并给出引导，不伪造内容。另提供 `docs/skills/zhixu-{classroom,paperlab}/SKILL.md` 供外部 Agent 工作台
      按 SOP 调用（本机同源会话）。
    - **试卷工坊**（`#/paperlab`）与**我的题库**（`#/mybank`）：图片/PDF/DOCX 试卷识别导入
      （视觉模型逐页结构化提取 → 逐题草稿核验 → 入库，进度/取消/续跑/失败隔离）、切题与答题区标注
      （页图框选、页级 edit.json 版本 CAS、按框重切、发布前校验）、可写题库（编辑/核验/AI 补全/
      统计/JSON·CSV 导入导出）、组卷（手工/AI 智能/知识点/错题/分层，硬校验不缩水）、试卷详情与发布、
      Word 导出 / A4·A3 分栏打印、答题卡模板与 SVG、整卷练习（自测判分、错题进错题本）、错题复现卷与
      AI 巩固卷。AI 组卷/补全/巩固需要用户自己的模型连接。
    - **学情分析**（`#/insight`）：个人维度的正确率、掌握分布、知识点强弱、错因/题型分布、趋势与
      规则建议；AI 学习建议只基于真实统计并明确标注 AI 生成。按用户要求不含班级/学校维度。
    - **AI 助手**（`#/assistant`，整合小纸）：会话 + JSON 工具协议（检索真题、读题（未作答不返回答案）、
      错题、统计、笔记、知识点、我的题库；建讲解任务/巩固卷/AI 组卷等写操作需二次确认）+ 技能库
      （内置方法论技能 + 自定义技能）+ 引用与停止/续跑。
    - **讲错题 / 大屏讲解**：错题与题目页可全屏分步讲解、深色/大字、Canvas 批注、可选录制与保存
      「原卷留痕」PNG。
    - **任务中心 / 设置**：全部 AI 任务统一查看（中文名/状态/阶段/进度/取消/续跑/结果/费用估算）；
      设置页支持 14 个任务默认模型与 AI 用量估算（只展示不扣减）。
    - **排除项**：按用户要求不引入作业系统、学校管理、三角色工作台，以及小鹿老师的作业管理/班级管理/
      我的学校等基础模块；组卷与整卷练习均为**个人自测**，不涉及班级收交。
    - **支持的真实厂商（已实测）**：小米 MiMo（`mimo-v2.6-flash` 视觉+文本、`mimo-v2.5-tts` 音色合成、
      `mimo-v2.5-asr` 语音识别）、DeepSeek（`deepseek-flash`，视觉+文本，适合做交叉核验）、
      MiniMax（`MiniMax-M3.1-Flash` 文本、`speech-2.8-turbo` 音色、`asr-1.0` 语音识别、
      `image-01` 图片生成）；课堂 TTS/ASR/配图会按连接自动选择对应协议。
    - 新增运行依赖：`python-pptx==1.0.2`（PPTX 导出；本机环境已安装，`requirements.txt` 已登记）。
      契约与接口：`INTEGRATION-CONTRACT.md`、各模块目录下的 `API.md` / `*API.md`。
    - 文档示例数据：`python scripts/seed_demo.py`（可选，无需 AI Key，见 `scripts/README-demo.md`）。

15. **本轮（2026-11-21）新增能力**：
    - **本地 OCR（离线 CPU）**：RapidOCR(PP-OCRv4) 集成（`app/ai/ocr.py`，图片/PDF，≤5 页同步），扫描件与拍照
      识别先跑本地 OCR 再作视觉模型提示；无视觉连接时可按 OCR 生成「无答案草稿」供人工整理（不伪造结构）；
      接口 `POST /api/ai/ocr`、`GET /api/ai/ocr/info`。
    - **试卷识别双模型交叉核验**：主模型（如小米 `mimo-v2.6-flash`）+ 核验模型（如 `deepseek-flash`）逐题比对
      题干/选项/答案，差异题目标红待人工确认；同一连接+模型不允许自核验。
    - **联网搜索**：DeepSeek 官方 `/v1/tools/search` 接入（`GET /api/ai/web-search`、助手工具 `web_search`），
      课堂生成可开启「联网查找资料」，来源带链接落库并在 PPTX/离线 HTML/ZIP 导出中附「参考来源」页。
    - **课堂媒体**：图片参考图编辑（MiniMax `image-01` subject_reference / OpenAI edits）、按连接聚合的音色
      （MiniMax `/get_voice`、MiMo 固定音色）、批量旁白任务（`course-narration`，幂等/取消/续跑）、服务端
      ASR 兜底（浏览器不支持语音识别时 MediaRecorder 录音上传）。
    - **课堂产出**：服务端 PDF、MP4 教学视频（`course-video` 任务：静态场景 + 旁白音轨，H.264/AAC，
      场景≤12/时长≤10 分钟）、幻灯片结构化编辑器（拖拽/缩放/属性/撤销/版本 CAS）。
    - **3D 交互场景**：three.js（MIT）由服务端按 sha256 白名单内联注入自包含 HTML，沙箱 CSP 与
      `allow-scripts` 不放宽，仍禁 eval/fetch/父窗口访问。
    - **试卷 / 答题卡 PDF**：`format=pdf` 服务端渲染（失败 503 提示改用浏览器打印）。
    - **答题卡扫描（OMR）**：定位点透视矫正 + 46 位页面码 + 气泡/学号识别 + 人工纠错确认判分
      （opencv，无新依赖）。
    - **暗色主题**：全站令牌与组件覆盖、顶栏「跟随系统/浅/深」三态、打印强制浅色。
    - **中英双语**：`web/locales/{zh-CN,en-US}.json`（2,200+ 键）+ `web/js/core/i18n.js`，主要页面与任务中心
      可切换英文（题目、AI 生成内容、数据不翻译）。
    - 验收：`scripts/ci_accept.ps1`（compileall + node --check + 全量 pytest，`-Ui` 追加浏览器验收）。

15. **专注计时、每日一签与 Anki 牌组导出（本轮）**：
    - **顶栏番茄钟**：`.topbar-right` 的专注胶囊，支持专注/短休/长休、25/45/60 分钟与自定义 1–180 分钟、
      任务标签、暂停/继续/结束、结束提示音（WebAudio 合成，无音频文件），剩余时间同步到页面标题；
      面板里直接显示今日/本周分钟数与连续天数。计时内核 vendored 自 shigure
      （MIT，`web/vendor/shigure/timer.js`，提交固定），用绝对时间戳推算剩余，刷新/切标签页不漂移。
      会话落库 `focus_sessions`（`app/focus.py`，`client_id` 幂等、按用户隔离、日期按用户本地时区分桶），
      接口：`POST/GET /api/focus/sessions`、`GET /api/focus/summary`、`DELETE /api/focus/sessions/{id}`。
    - **每日一签**：每天首次进入工作台弹出古诗词/论语/幽梦影一句（可换一句、收藏、开始学习），
      侧栏底部同一句可点击再看。数据为构建期从 chinese-poetry（MIT）抽取的 365 条
      （`web/vendor/quotes/`，来源与 SHA-256 见同目录 README；生成脚本 `scripts/build_daily_quotes.py`，
      只取原文、丢弃现代译文），完全离线，取不到数据就静默不弹。浏览器不允许「关闭页面时弹自定义卡片」，
      因此只在打开时弹；关闭侧由「结束学习」场景负责。
    - **Anki 牌组导出（`.apkg`）**：错题本页新增「导出 Anki 牌组」——按学科分牌组，正面＝来源+题干+选项，
      背面＝参考答案+解析+我的错因/知识点/复习状态；笔记页在原有 Anki CSV 之外新增「Anki 牌组」。
      由 `app/anki_export.py`（genanki，MIT）生成，双击即可导入 Anki / AnkiDroid；含图题不打包题图并明确提示，
      不伪造内容。新增运行依赖 `genanki==0.13.1`（纯 Python）。
    - 许可登记：`THIRD_PARTY_NOTICES.md` 新增第 8–10 节（shigure / chinese-poetry 子集 / genanki），
      并补登记既有的 KaTeX、markdown-it、DOMPurify（第 11 节）。

16. **开源组件整合（批次一 / 二 / 三 / 四 / 五·6.2–6.3，默认不改变运行时）**：
    - **无障碍回归**：`scripts/ui_check_a11y.py` 用随脚本分发的 axe-core 4.14.1（MPL-2.0，仅开发期，
      不进镜像）在桌面 1440 与手机 390 视口跑 WCAG 2A/2AA，存在新增 critical/serious 即失败；
      首次可 `--update-baseline` 固化存量基线，报告落 `screenshots/a11y/`。已注册为 `-Ui` 验收的
      `ui-a11y` 阶段。
    - **题库近似去重**：`scripts/dedup_questions.py` 以只读方式打开题库，按学科与题干长度分桶后
      用 RapidFuzz 计算相似度，输出 JSON/Markdown 人工复核清单，不写任何库。
    - **文本 PDF 表格**：可选依赖 `requirements-pdf.txt`（pdfplumber 0.11.10，MIT；0.11.11 要求 Pillow≥13，与工程锁定版本不兼容）；设置
      `STUDY_PDF_TABLES=on` 后，知识清单上传文本型 PDF 时表格行按 `" | ".join(单元格)` 追加到对应页，
      与 DOCX 口径一致；关闭/未安装/抽取失败时结果与现状逐字节一致。页数上限
      `STUDY_PDF_TABLE_PAGES`（默认 50）。
    - **公式与表格结构后处理**：`ocr.enrich_blocks()` 作为拍题、扫描页、`POST /api/ai/ocr` 与试卷工坊
      的共享入口；`STUDY_FORMULA_OCR=on` 时用 RapidLaTeXOCR 给 math 块填 `latex`（仍标「需校对」），
      `STUDY_TABLE_OCR=on` 时用 RapidTable 给连续表格块生成经白名单清洗的 `table_html`/行列数。
      两者默认关闭，未安装/超时/失败时块原样；可选依赖见 `requirements-formula.txt`
      （该清单按 rapid-latex-ocr 的声明固定 numpy 1.26.x；需要 numpy 2 的部署请放到独立虚拟环境）。
    - **可视化公式编辑**：`web/vendor/mathlive/`（0.111.4，MIT，版本与 SHA-256 见目录 README）
      提供 `formulaEditor.js`；拍题校对、题目表单（题干/解析/评分标准/子题）与笔记编辑器可打开
      原生 `<dialog>` 编辑公式，首次打开懒加载，加载失败回退现有文本公式输入。
    - **本地向量与重排**：`requirements-embeddings.txt`（fastembed 0.9.1，Apache-2.0）；设
      `STUDY_EMBEDDING_MODE=local` 后，资料向量由本机 `bge-small-zh-v1.5` 计算，存储标签为
      `local://...`，与任何 HTTP 模型标签隔离；设 `STUDY_RERANK=on` 且预置 `bge-reranker-base`
      后，笔记检索取混合得分前 20 条重排（0.3 混合 + 0.7 重排），相似题条目增加 `rerank_score`。
      模型目录由部署方预置，失败/未安装时回退现有 HTTP Embedding 或本地全文检索。
    - **离线语音**：`requirements-speech.txt`（sherpa-onnx 2.0.3，Apache-2.0）；设
      `STUDY_LOCAL_SPEECH=on` 并预置 Kokoro `kokoro-multi-lang-v1_1` 与 Paraformer-zh 后，
      零厂商配置也能完成课堂批量旁白（`provider: "local"`）、音色列表与语音输入识别；
      识别只接受 16k 单声道 PCM16 WAV，前端 `wavEncode.js` 用 WebAudio 自动重采样，
      模型缺失/合成失败时旁白回退浏览器朗读，不影响其它功能。
    - **分子模型与配平**：`web/vendor/3dmol/`（3Dmol.js 3.0.0）与
      `web/vendor/openchemlib/`（OpenChemLib 9.26.0，均 BSD-3-Clause，版本与 SHA-256 见各自 README）
      提供化学题「分子模型」入口：OpenChemLib 把 SMILES 解析为 3D 构象，3Dmol 渲染，
      支持球棍/空间填充/线框切换与 `molecule_3d` 学习附件；`POST /api/chem/balance`
      用 sympy 配平元素/括号/数字下标组成的方程式，无解返回 `ok:false`，不落库。
    - **物理自由沙盒**：`web/vendor/matter/`（Matter.js 0.20.0，MIT）提供物理题「自由沙盒」入口与
      物理演示页「自由沙盒」页签，支持刚体/弹簧/斜面/碰撞预设、播放暂停、速度与位置读数，
      保存为 `physics_sandbox` 学习附件；页面固定提示「探索模式，结果可能有细微差异」，
      需要回放对账的演示仍走 `physics-in-motion` 确定性内核。

## 验证

```powershell
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD="1"; python -m pytest -v
python -m compileall -q app
Get-ChildItem web/js -Recurse -Filter *.js | ForEach-Object { node --check $_.FullName }

# 本轮新增：专注计时 / 每日一签数据 / Anki 牌组导出
python -m pytest tests/test_focus.py tests/test_anki_export.py tests/test_daily_quotes.py -v
python scripts/build_daily_quotes.py   # 重建每日一句数据（需要网络，可复用缓存；产物条数/SHA-256 随 README 刷新）

# 志愿数据导入（约 1.35GB 源数据；--skip-download 复用缓存，--prune-cache 建库后清理）
python scripts/import_admission.py
python -m pytest tests/test_admission.py -v

# 官方渠道补采批次线（2020–2026，31 省；需要网络）并导入独立小库
python scripts/collect_official_data.py batch-lines
python scripts/import_official_data.py

# 数据缺失声明（逐省年 JSON + Markdown）
python scripts/report_data_gaps.py

# 物理演示：vendored 物理内核验证（10 个演示全部构建 + 自动校验通过）
node scripts/verify_physics_assets.mjs
python -m pytest tests/test_physics_assets.py -v

# 真实录取库冒烟：隔离实例 + 硬链接 2.6GB 库，61 项断言（--sweep 遍历全部 232 个省年）
python scripts/admission_smoke.py
python scripts/admission_smoke.py --sweep --json-out admission-smoke.json

# 安全/门禁/数据/检索性能的独立验收（默认跟随 releases/CURRENT）
$env:STUDY_DATA_DIR="$env:TEMP\zhixu-audit"; python scripts/audit_accept.py

# 界面验收（先启动隔离实例，三个脚本互补）
python -m uvicorn app.main:app --host 127.0.0.1 --port 8813
python scripts/ui_check.py --base-url http://127.0.0.1:8813 --out screenshots/ui-audit
python scripts/ui_check_pages.py --base-url http://127.0.0.1:8813 --out screenshots/ui-audit
python scripts/ui_check_flows.py --base-url http://127.0.0.1:8813 --out screenshots/ui-audit

# 整合模块（AI 课堂 / 试卷工坊 / 学情 / AI 助手 / 任务中心）
# 题库默认跟随 releases/CURRENT（当前 r5）；r3 已退役，只有审计对照时才显式 STUDY_BANK_DIR。
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD="1"; python -m pytest -q
python scripts/ui_check_integration.py --base-url http://127.0.0.1:8813 --routes home,classroom,mybank,paperlab,insight,assistant,tasks
# 拍题入库页（#/upload）桌面/手机视口核对（本轮新增，需隔离实例与 Playwright）
python scripts/ui_check_upload.py --base-url http://127.0.0.1:8813
# 无障碍回归（axe-core WCAG 2A/2AA；首次先固化基线，之后只对新增 critical/serious 判失败）
python scripts/ui_check_a11y.py --base-url http://127.0.0.1:8813 --update-baseline
python scripts/ui_check_a11y.py --base-url http://127.0.0.1:8813
# 题库近似去重（只读；输出人工复核清单，不改库）
python scripts/dedup_questions.py
# 文本型 PDF 表格（可选依赖；关闭时行为不变）
pip install -r requirements-pdf.txt
$env:STUDY_PDF_TABLES="on"; python -m pytest tests/test_pdf_tables.py -v

# 公式识别 / 表格结构（可选依赖；未安装或关闭时块保持原样）
pip install -r requirements-formula.txt
$env:STUDY_FORMULA_OCR="on"; $env:STUDY_TABLE_OCR="on"; python -m pytest tests/test_formula_ocr.py tests/test_table_structure.py -v

# vendor 资产自检（版本 / 大小 / SHA-256 与磁盘一致）
python -m pytest tests/test_oss_vendor_assets.py -v

# 本地向量与重排（可选依赖；模型由部署方预置，未安装/关闭时行为不变）
pip install -r requirements-embeddings.txt
$env:STUDY_EMBEDDING_MODE="local"; python -m pytest tests/test_embeddings_local.py -v
$env:STUDY_RERANK="on"; python -m pytest tests/test_rerank.py -v

# 离线语音（可选依赖；模型由部署方预置，未安装/关闭时行为不变）
pip install -r requirements-speech.txt
python -m pytest tests/test_speech_local.py tests/test_classroom_local_tts.py -v

# 分子模型 / 化学配平（vendor 资产 + sympy，无新增 Python 依赖）
python -m pytest tests/test_chem_balance.py tests/test_demo_kinds.py tests/test_oss_vendor_assets.py -v
# 物理自由沙盒（Matter.js vendor 资产）
python -m pytest tests/test_demo_kinds.py tests/test_oss_vendor_assets.py -v
# 课堂全链路验收（本机假模型，无需真实 Key；详见 scripts/integration/README.md）
python scripts/integration/fake_model_server.py --port 8899
python scripts/integration/classroom_accept.py --base-url http://127.0.0.1:8813 --fake-model-base-url http://127.0.0.1:8899
# 演示数据（可选，无需 AI Key，标题均带「示例」，--reset 可清理）
python scripts/seed_demo.py --register
```

### 一键验收（本轮重写，默认 r5 基线）

```powershell
.\scripts\ci_accept.ps1                              # 题库基线 + compileall + node --check + 编辑器类型检查 + 全量 pytest
.\scripts\ci_accept.ps1 -Ui -BaseUrl http://127.0.0.1:8840   # 追加 UI 路由/广场全链路/精选接入/AI 用量/拍题入库/整合模块端到端
# 广场全链路（匿名浏览→投稿→初审→评审阈值→发布→撤回→抽检/互动/回滚）也可单独跑：
#   python scripts/integration/plaza_accept.py --base-url http://127.0.0.1:8840
#   （已播种实例需加 --admin-username/--admin-password；公网模式可用 --admin-token）
# 精选接入学习链路 / AI 用量与配额（本地假模型，无真实费用）：
#   python scripts/integration/official_accept.py --base-url http://127.0.0.1:8840
#   python scripts/integration/ai_usage_accept.py --base-url http://127.0.0.1:8840
.\scripts\ci_accept.ps1 -EditorBuild                 # 追加「编辑器净构建 vs web/editor 入库产物」一致性检查
```

判定语义：任一阶段失败/UI 不可达 → exit 1；存在跳过（工具缺失、`-SkipPytest`、`-Stages` 过滤）或零阶段执行 → exit 3
`ACCEPT INCOMPLETE`；只有全部选中阶段执行且全过才 exit 0 并落盘 `build_id`/`bank_version` 报告（默认 `screenshots/accept-report.json`）。
本轮实施与验收明细见 [`docs/知序优化实施记录-20261121.md`](docs/知序优化实施记录-20261121.md)。

### 数据维护（本轮新增）

```powershell
# r3 退役记录：只读预检 → 备份+单事务清理 → 幂等复跑（PG 模式 fail-closed）
python scripts/cleanup_r3_records.py --dry-run --json docs/r3-清理预检.json
python scripts/cleanup_r3_records.py --apply  --json docs/r3-清理执行.json

# 个人题库配图资产迁移（旧文件名 → 服务端 asset_id；默认只预检）
python scripts/migrate_paperlab_assets.py --dry-run
python scripts/migrate_paperlab_assets.py --apply
```

开发与验收依赖（pytest、playwright）在 `requirements-dev.txt`；生产镜像只安装 `requirements.txt`。
可选的图片试题转 LaTeX 依赖（`surya-ocr`）在 `requirements-surya.txt`，不装不影响现有功能，
详见 `app/ai/surya_latex.py` 模块说明与上级 `docs/平台设计.md`「图片试题转 LaTeX」章节。
首次检索会构建题库索引（约 0.5s）与 FTS（首次 ≥3 字检索约 1.5s），之后检索稳定在 50ms 内。
生成找回密码恢复码需要输入当前密码（二次校验）。
