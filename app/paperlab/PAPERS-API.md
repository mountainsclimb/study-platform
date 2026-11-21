# 试卷工坊后端 API（P1：组卷 / 错题巩固卷 / 导出）

前缀 `/api/paperlab`，全部需登录（`current_user`），错误统一 `{detail: "中文说明"}`。
实现：`app/paperlab/{__init__,db,digitize,bank,enrich,prompts,papers,compose,revision,exporters}.py`；
测试：`tests/test_paperlab_{papers,compose,revision}.py`（P1）+ 既有 `test_paperlab_{bank,import,enrich}.py`（P0）。
契约：`INTEGRATION-CONTRACT.md` §4.2（只增字段，不改已发布字段名）。

## 0. 数据表（P1，`app/paperlab/papers.py::ensure_tables()` 幂等建表）

| 表 | 说明 | 关键字段 |
|---|---|---|
| `pl_papers` | 试卷元数据 | `user_id,title,kind,subject,grade,instructions,layout_json,status(draft\|ready\|published),published_at,source,version,created_at,updated_at` |
| `pl_paper_items` | 有序题目 + 冻结快照 | `paper_id,position,section,question_ref,snapshot_json,score,created_at` |

- `kind`：`manual|smart|knowledge|wrong|layered|replay|consolidate|exam`。
- 状态机：手工建卷 = `draft`；AI 组卷/题本生成 = `ready`；`POST /papers/{id}/publish {published:true}` → `published`
  （发布后禁止改结构：PATCH/reorder 返回 409），`{published:false}` → 回到 `ready`。
- `question_ref`：`app:ID`（应用题库 `pl_questions`，校验本人归属）或 `bank:qid`（只读高考题库，
  校验题目存在且可作答）。同一张卷不允许重复 ref。
- `snapshot_json` 冻结：`{ref,source,source_id,subject,grade,type,difficulty,knowledge_tags,score,
  verify_status?/bank_version?,content:{stem,options[{label,text}],answers[],answer_text?,
  explanation,rubric,parts[],images[],material?},origin?,frozen_at,selection_reason?}`。
  建卷后题库题目被修改/删除不影响历史卷（导出/详情都读快照）。

## 1. 手工建卷与试卷 CRUD（`papers.py`）

- `GET /papers?subject=&kind=&status=&page=&page_size=` → `{items:[paper],total,page,page_size}`
  `paper = {id,title,kind,subject,grade,instructions,layout,status,published_at,source,version,
  item_count,total_score,created_at,updated_at}`（列表不含题目快照）。
- `POST /papers` `{title,kind:'manual'|'exam',subject,grade?,instructions?,layout?,items:[{ref,score?,section?}]}`
  → `{paper, items:[item]}`（即 `papers.paper_detail` 结构；`paper` 为试卷元数据对象，不是数组）；`items` 可空（草稿卷，发布时要求 ≥1 题）。
  - `items[].ref` 不存在/不属于本人 → 404；格式非法/重复/归属不符 → 422；单卷 ≤60 题。
  - `layout`：`{paper:'A4'|'A3',columns:1..4,font_size:8..18,line_height:1..3,answer_space_mm:0..80,margin_mm:5..40,page_break_before?:bool}`。
- `GET /papers/{id}?include_snapshot=true` → `{paper,items:[{id,position,section,question_ref,score,
  source,type,type_label,subject,difficulty,knowledge_tags,stem_preview,has_answer,selection_reason,
  created_at,snapshot?}]}`
- `PATCH /papers/{id}` `{version,title?,grade?,instructions?,layout?,items?}` → `{paper,items}`
  - `version` 乐观锁 CAS，冲突 409；`items` 传入即**整卷替换**（重新冻结快照）；已发布 409。
- `POST /papers/{id}/items/reorder` `{version,item_ids:[...]}` → `{paper,items}`
  - 必须包含全部 item 且不重复/不越权，否则 422；已发布 409。
- `POST /papers/{id}/publish` `{published:true|false}` → `{paper,items}`；无题目发布 → 422。
- `DELETE /papers/{id}` → `{deleted:true,paper_id}`（题目快照级联删除；他人 404）。

## 2. AI 组卷（`compose.py`，任务 `paper-compose`）

- `POST /papers/compose`
  `{mode:'smart'|'knowledge'|'wrong'|'layered', subject, grade?, count:1..60, type_counts?:{题型:题数},
    difficulty_target?:0..1, tolerance?:(默认0.1,0..0.5), kp_weights?:{知识点:百分比}, filters?:{source:'all'|'app'|'bank',
    knowledge_tags?:[], exclude_refs?:[]}, provider_id, model}`
  → `{job,id,task:'paper-compose',status,mode,count,candidates:{total,type_availability,difficulty_range,kp_quotas}}`
- 候选池：应用题库（本人 `pl_questions`，可按年级）+ 只读高考题库同科可用题；`mode='wrong'` 时候选 = 本人错题。
  指定 `grade` 时只使用应用题库（只读题库没有年级字段）；`source` 可限制来源；题库侧难度取
  「管理员标注 → AI 预估 → 全站统计（≥20 次已判定作答）」的真实数据，未标注视为 `null`。
- 预检（同步 422，不消耗模型）：候选总数不足、指定题型可用数不足、知识点配额二分匹配不满、
  `difficulty_target` 下带难度标注的题不足或难度范围无法满足容差 → 直接 422 并给出差额。
- 执行器（`paper-compose`）：
  1. 重建候选（≤150 道，按题型/知识点配额优先保留），中文阶段 `组卷候选准备 → AI 选题 → 硬校验 → 生成试卷快照 → 组卷完成`；
  2. 模型只能从候选 JSON 选 `ref`，输出 `{"selections":[{"ref","score?","reason?"}],"summary"}`；
  3. 硬校验：题数精确、题型数量精确、学科/年级一致、平均难度在容差内、知识点配额
     （Hamilton 最大余数法 + Kuhn 二分匹配，多标签题只计一次）、去重、ref 必须在候选内；
     不满足带差额原因重试一次，仍失败 → 任务 failed（`error` 含中文原因），绝不缩水/编造；
  4. 建卷：`kind=mode 对应`、`status=ready`、`source='ai-compose:{mode}:job:{job_id}'`（同任务续跑幂等，
     不会重复建卷）；结果 `{paper_id,paper,item_count,total_score,items,summary,statistics}`。
- 取消：任务已建卷则保留试卷；未建卷不留半成品，可续跑（checkpoint 保存 proposal/paper_id）。

## 3. 错题题本（`revision.py`）

- `GET /revision/options?subject=` → `{wrong_count,subjects,kp_options:[{kp_id,name,wrong_count}],
  modes:[{value,label,description,ai_required,count_min,count_max}],limits}`
- `POST /revision` `{mode:'replay'|'consolidate', subject?, kp_ids?, count?:1..20, provider_id?, model?}`
  - `replay`（无需模型）：直接按错题本当前有效错误题建卷，qid 去重、冻结快照、保留题目分值
    （应用题库快照分 / 题库题型默认分），不可用题跳过；返回
    `{mode:'replay',paper,items,item_count,skipped,message}`，`kind='replay'`、`status='ready'`。
    错题本为空 → 422。
  - `consolidate`（需要 `provider_id+model`）：候选 = 与错题知识点相同、同科、该用户**从未作答过**、
    不在错题本的只读题库题；候选不足 → **同步 422 明确拒绝不缩水**。可行则创建任务
    `paper-revision`，返回 `{mode:'consolidate',job,id,task,status,count,candidates}`，
    其中 `candidates = {candidate_count,count,subject,wrong_count,kp_ids}`（前端请用 `candidate_count`）。
  - 任务执行器：中文阶段 `巩固卷候选准备 → AI 选题与理由 → 生成巩固卷快照 → 巩固卷完成`；
    模型输出 `{"selections":[{"ref","reason"}],"summary"}`，硬校验：题数精确、ref 在候选内、去重、
    每题必须有中文理由；建卷 `kind='consolidate'`、`status='ready'`、`source='ai-revision:consolidate:job:{job_id}'`；
    结果 `{paper_id,paper,item_count,total_score,summary,reasons,statistics}`；支持取消/checkpoint 幂等续跑。

## 4. 导出（`exporters.py`）

- `GET /papers/{id}/export?format=docx|html&answers=1|0`（默认 `answers=1`）
  - 校验归属（他人 404）；内容只读快照，与建卷时一致；`answers=0` 导出学生版（不含答案/解析）。
  - `docx`：`application/vnd.openxmlformats-officedocument.wordprocessingml.document`，
    标题/卷头信息/说明/分节（题型或自定义 section）/连续题号/题干（简单 LaTeX→纯文本）/
    选项 2 或 4 列表格/主观题答题空间/页脚页码；`answers=1` 追加「参考答案与解析」页。
    python-docx 已随环境安装（P1 未新增 requirements 依赖）。
  - `html`：`text/html`，自包含打印页（内联 CSS、`@page A4/A3`、`break-inside:avoid`、选项栏、
    答题空间、可选答案页），配图内联为 data URL；响应头 `X-Frame-Options: SAMEORIGIN`
    且 CSP 允许 `img-src data:`，前端可同源 iframe 预览后调用 `contentWindow.print()`。
  - 其它 format → 422。

## 5. 任务注册

`init_paperlab()` → `register_jobs()` 通过 `app.ai.jobs.register_task_handler` 注册：

| 任务名 | label | validate 校验 |
|---|---|---|
| `paper-import` | 试卷识别导入 | 见 P0 API.md |
| `paper-enrich` | AI 补全答案解析 | 见 P0 API.md |
| `paper-compose` | AI 组卷 | spec 合法、候选/题型/知识点/难度必要条件满足（provider 由框架校验），不满足 422 且不建任务 |
| `paper-revision` | AI 错题巩固卷 | 仅 `consolidate`；spec 合法、候选数量足以达到题数（不足 422 拒绝缩水） |

任务结果由框架追加 `prompt_version/models/input_ref`；`paper-compose`、`paper-revision` 的
`params={"spec":{...}}` 便于续跑时重建同一候选集。

## 6. 已知边界

- `GET /meta` 的 `capabilities.papers` 自 P1 起已为 `true`（由 `bank.py` 提供）；前端可直接按能力位展示入口。
- 只读高考题库没有分值与年级字段：快照按题型默认分（单选 2 / 多选 4 / 填空 2 / 主观 10），
  指定年级时组卷只用应用题库。
- 答题卡 SVG（`pl_cards`）与页内答题区标注已完成（见 `CARDS-API.md` / `ANNOTATE-API.md`）；
  扫描 OMR 读卡仍属后续里程碑，接口未开放（不返回假成功）。
- 全模块路由索引见仓库根 `INTEGRATION-API-INDEX.md`。

## 7. 整卷练习（`app/paperlab/practice.py`，个人自测）

数据（`practice.ensure_tables()` 幂等建表，题目内容始终来自建卷时的 `pl_paper_items.snapshot_json`）：

| 表 | 说明 | 关键字段 |
|---|---|---|
| `pl_paper_attempts` | 一次整卷练习 | `user_id,paper_id,paper_version,status(in_progress\|submitted),answers_json,score,max_score,detail_json,started_at,submitted_at` |
| `pl_paper_answers` | 逐题作答+判分 | `attempt_id,user_id,item_position,question_ref,answer_json,result(correct\|incorrect\|pending),score,max_score,detail_json`，`UNIQUE(attempt_id,item_position)` |

- `POST /papers/{id}/attempts` → `{attempt,paper,items}`；创建新练习（无需发布；空卷/快照不可用 422；他人/不存在 404）。
  `items` 是**练习卷面**（不含 `answers/explanation/rubric`，防止提前泄题），`attempt` 含 `id/status/score/max_score/
  correct_count/incorrect_count/pending_count/started_at/submitted_at`。
- `GET /attempts/{attempt_id}` → `{attempt,paper,items}`；本人校验（他人 404）。
  未提交：只返回卷面与已保存作答 `answer`；已提交：每题追加 `judgement/score/feedback/reference`（参考答案与解析）以及 `result`：
  `{summary,wrong_added,app_bank_unlinked,notice}`。
- `PATCH /attempts/{attempt_id}/answers` `{item_position,answer}` → `{saved,attempt,item_position,answer}`
  - 幂等 upsert（重复保存同题覆盖，不报错）；越界 `item_position` 422；已提交 409；单题作答 ≤16KB（413）。
  - `answer` 形态：选择 `["B"]` 或 `"B"`；填空/主观字符串或按空数组；MIXED 用
    `{"parts":[{"index":1,"answer":"A"},{"index":2,"answer":"..."}]}`（或按 index 顺序的数组）。
- `POST /attempts/{attempt_id}/submit` `{answers?:[{item_position,answer}]}` →
  `{attempt,paper,items,result}`；重复提交 409。
  - 判分：单选/多选按快照选项 label 精确比对（多选题需集合完全一致）；填空按归一化文本比对（支持「；/ 或」多答案、
    多空逐空比对）；主观题与含主观子题的混合题标 `pending`（不硬判、不计分，结果页展示参考答案供自测核对）。
  - 错题联动：`bank:qid` 来源的题（客观判错或主观待核验）写入既有 `wrong_questions`
    （`ON CONFLICT` 累加 failures、next_review_at=+1 天、并同步 `mastery` 降为「未掌握」）；
    `app:ID` 应用题库题只在本次练习结果中记录（错题本按只读题库 qid 索引，结果里 `app_bank_unlinked` 明示）。
  - 成绩：`score` 只累计客观题正确得分；`max_score` 为全卷总分；主观题得分不计入 `score`，结果中单列 `pending_count`。
- `GET /papers/{id}/attempts`（别名 `GET /attempts?paper_id=`）→ `{items,total,paper_id}`：本人该卷练习历史
  （得分/状态/起止时间/对错与待核验数），按时间倒序。
- `GET /papers/{id}/items/{position}/images/{index}` → 试卷快照配图文件流（本人校验；位置从 1 开始）。
  只读题库的 `/api/bank-image` 按「是否已作答解锁」授权，历史试卷/整卷练习通过该通道展示冻结配图；
  配图来源仅限建卷快照（路径安全由导出模块校验），Word/HTML 导出仍由后端内联 data URL，互不影响。

