# 试卷工坊后端 API（P0 已实现；P1 能力见 PAPERS-API / CARDS-API / ANNOTATE-API / BANK-IO）

前缀 `/api/paperlab`，全部需登录（`current_user`），错误统一 `{detail: "中文说明"}`。
实现：`app/paperlab/{__init__,db,digitize,bank,enrich,prompts}.py`；测试：`tests/test_paperlab_*.py`。
契约：`INTEGRATION-CONTRACT.md` §4.2（本模块只增字段，不改已发布字段名）。
全模块路由索引见仓库根 `INTEGRATION-API-INDEX.md`。

## 0. 数据表（幂等建表，`paperlab.db.init_tables()`）

| 表 | 说明 | 关键字段 |
|---|---|---|
| `pl_imports` | 一次导入（上传批次） | `user_id,title,subject,grade,status(draft\|recognizing\|ready\|committed\|failed),page_count,job_id,source_json,note` |
| `pl_pages` | 页图 + 纯文本页 | `import_id,page_no,width,height,storage_name,media_type,text_content,status` |
| `pl_import_items` | 识别草稿 | `import_id,page_no,item_no,bbox_json,draft_json,status(pending\|accepted\|skipped),needs_review,review_reason,skip_reason,confidence,source(ai\|manual),warnings_json,question_id` |
| `pl_questions` | 应用内可写题库 | `type,subject,grade,difficulty,knowledge_tags_json,content_json,status(draft\|ready\|archived),verify_status(pending\|verified\|rejected),version,ai_job_id,ai_model,prompt_version,deleted_at` |

页图目录 `paperlab/pages`，裁切配图目录 `paperlab/assets`（内容寻址文件名）。所有私有资源先校验 `user_id`。

## 1. `GET /meta`

```json
{"enabled": true, "subjects": ["语文","数学","英语","物理","化学","生物","政治","历史","地理"],
 "grades": ["小学一年级","…","高中三年级"],
 "qtypes": [{"value":"SINGLE","label":"单选题"},{"value":"MULTIPLE","label":"多选题"},
            {"value":"BLANK","label":"填空题"},{"value":"SUBJECTIVE","label":"主观题"},
            {"value":"MIXED","label":"混合题"}],
 "difficulties": [{"value":0.2,"label":"简单"},{"value":0.5,"label":"中等"},{"value":0.8,"label":"困难"}],
 "limits": {"upload_mb":30,"import_pages_max":20,"import_files_max":20,"import_items_max":400,
            "enrich_questions_max":50,"page_size_max":100},
 "capabilities": {"vision":true,"pdf":true,"docx":true,"ai_enrich":true,"papers":true,"answer_sheet":true},
 "prompt_version": "paperlab-2026-11-20-v1"}
```

> 注：`capabilities.papers`/`answer_sheet` 自 P1 起已为 `true`（组卷、答题卡/标注、整卷练习已实现）；
> 前端可直接按能力位展示入口。

## 2. 导入与识别（`digitize.py`）

- `POST /imports`：multipart `files[]`（JPG/PNG/WebP/PDF/DOCX，单文件 ≤30MB、≤20 文件、≤20 页）+
  `title?/subject?/grade?` → `{import:{id,status,pages:[{page_no,width,height,image_url}]}, pages:[…]}`
  - PDF 用 pypdfium2 渲染（scale 2.0 ≈144 DPI）；DOCX 用 python-docx 提取文字+图片（只有文字时建 1 个纯文本页）；
    图片自动 EXIF 校正并压成 JPEG（最长边 ≤2400）。
  - 超限/格式不支持/空文件 → `422` 中文提示。
- `GET /imports?page&page_size&status` → `{items,total,page,page_size}`；`GET /imports/{id}` →
  `{import, pages, items, counts:{total,pending,accepted,skipped,needs_review,committed}}`
- `GET /imports/{id}/pages/{page_no}/image` → 页面图文件流（`FileResponse`，`Cache-Control: private`）；
  纯文本页 `404`。
- `POST /imports/{id}/recognize` `{page_from?,page_to?,provider_id,model}` → `{job:{…}, id, status, task, import_id, page_from, page_to}`
  - 任务名 `paper-import`，`params={"import_id","page_from","page_to"}`；SSE 阶段为 `识别 N/M 页`。
- `PATCH /imports/{id}/items/{item_id}` `{draft?, bbox?, status?, skip_reason?}` → `{item}`
  - `status` ∈ `pending|accepted|skipped`（`skipped` 为跳过，`pending/accepted` 恢复）；已入库草稿 `409`；
    bbox 非法 / 内容不合格 `422`。
- `POST /imports/{id}/commit` `{item_ids?, target:"app_bank"|"paper"}` →
  `{created_question_ids, existing_question_ids, committed, warnings, import_status, import_id}`
  - 缺省只入库 `accepted` 草稿；显式 `item_ids` 视为人工确认（`pending` 也可入库）；重复 commit 幂等（不重复建题）；
    `target="paper"` 返回 `422`（组卷未开放）。

识别任务要求模型输出严格 JSON（`prompts.build_import_page_prompt`）：

```json
{"page_inventory":{"question_count":3,"skip_count":1},
 "questions":[{"page_no":1,"bbox":{"x":0.05,"y":0.05,"w":0.9,"h":0.2},"type":"SINGLE",
   "stem":"…$f(x)=x^2$…","options":[{"label":"A","text":"2"},{"label":"B","text":"4"}],
   "answers":["B"],"explanation":"…","rubric":"","knowledge_tags":["函数"],
   "difficulty":0.3,"score":5,"images":[{"bbox":{"x":0.1,"y":0.3,"w":0.2,"h":0.1},"role":"stem"}],
   "confidence":0.9}],
 "skipped":[{"page_no":1,"bbox":{…},"reason":"字迹模糊"}]}
```

校验：`page_inventory.question_count == len(questions)+len(skipped)`、`skip_count == len(skipped)`、
bbox 归一化合法、选择题答案落在 options label、MIXED 必须含 parts；不合法由 `call_structured` 带错误原因重试一次。
任务逐页落草稿（同页 AI 旧草稿替换、人工稿保留），每页写 checkpoint；单页失败隔离；`is_job_cancelled` 取消后
已完成页保留、可断点续跑。

## 3. 可写题库（`bank.py`）

- `GET /questions?subject&grade&qtype&keyword&status&verify_status&page&page_size`
  → `{items:[{id,type,subject,grade,difficulty,knowledge_tags,content,status,verify_status,version,ai_job_id,
    ai_model,prompt_version,source,has_answer,stem_preview,created_at,updated_at}],total,page,page_size}`
  - `keyword` 用 LIKE（转义 `%`/`_`）匹配 `content_json`（题干摘要等）与知识点 JSON，不依赖 FTS5；`qtype` 兼容中文标签。
- `POST /questions` `{type,subject,grade,difficulty?,knowledge_tags[]?,content{...},status?}` → `{question}`
  - `content`：`stem/options[{label,text}]/answers[]/explanation/rubric/parts[]/images[{bbox,role?}]/bbox/source_page/score/confidence`；
    选择题必须有选项且答案 label 合法，MIXED 必须含 parts 且非 MIXED 不得含 parts。
- `GET /questions/{id}` → `{question}`；`PATCH /questions/{id}` `{version, …字段}` → `{question}`（`version` CAS，冲突 `409`）；
  `DELETE /questions/{id}` 软删除 → `{deleted,question_id}`。
- `GET /questions/{id}/images/{index}` → 题级裁切配图（`content.images[i].url` 即此地址，`Cache-Control: private`）。
  - P0 所有权校验：配图由服务端签发 `asset_id`（`pl_assets` 登记 owner/用途/摘要），`content.images[i].asset`
    存的是 `asset_id`；读图同时校验「题目属于当前用户且未删除 + 资产存在且可用 + 资产归属当前用户
    （或已发布 `public_derivative` 平台精选衍生图）」，任一项不满足统一 `404`，不泄露他人资产存在性。
  - 旧数据的原始文件名引用需先执行一次迁移确认归属：
    `python scripts/migrate_paperlab_assets.py --dry-run` 预检、`--apply` 登记（幂等，不改写 content）。
    匿名/其他用户不可读取未发布的私人衍生图；`public_derivative` 默认 `0`（广场功能后续再做）。
- `POST /questions/{id}/verify` `{status}` → `{question}`
  - 兼容 `verified|ready`、`rejected`、`pending|draft`；核验通过要求已有答案，`verified` 后 AI 补全不得覆盖。

## 4. AI 补全（`enrich.py`）

- `POST /questions/enrich` `{question_ids[], provider_id, model}` → `{job:{…}, id, status, task, question_ids}`
  - 任务名 `paper-enrich`，`params={"question_ids":[…]}`；单次 1–50 题；非本人题目 `404`。
  - 模型输出 `{answers[], explanation, rubric, knowledge_tags[], parts[]}`，硬校验：答案非空、解析非空、
    主观题有 rubric、选择题答案落在 label、填空题每空一个答案、MIXED 保留全部子题（数量/index 一致）。
  - 写回 `content_json` 的 `answers/explanation/rubric/parts` 与 `knowledge_tags_json`（与已有标签合并），
    设置 `verify_status='pending'`、`ai_job_id`、`ai_model`、`prompt_version`、`version+1`；
    `content.ai` 记录 `{kind,provider_id,model,prompt_version,job_id,at}`。已核验题目跳过并在结果 `skipped` 说明。
  - 执行器逐题隔离失败、SSE 阶段 `补全 N/M 题`、支持取消与 checkpoint；`result.failed[]` 列出可重试题。

## 5. 任务注册

`init_paperlab()` → `register_jobs()` 通过 `app.ai.jobs.register_task_handler` 注册：

| 任务名 | label | validate 校验 |
|---|---|---|
| `paper-import` | 试卷识别导入 | `params.import_id` 归属、导入有页面、页码范围/页数 ≤20 |
| `paper-enrich` | AI 补全答案解析 | `params.question_ids` 非空、编号合法、≤50、每题归属（provider 由框架校验） |

## 6. P0 之后的里程碑（P1 已实现，见对应文档）

P0 文档曾把以下项列为「未实现」；P1 已全部实现且接口可用（不再返回「未开放」的 422）：

| 能力 | 文档 | 路由 |
|---|---|---|
| 组卷 / 试卷快照 / 发布 / 导出 | `PAPERS-API.md` | `/papers*`、`/papers/compose`、`/papers/{id}/export` |
| 错题复现 / AI 巩固卷 | `PAPERS-API.md` | `/revision/options`、`/revision` |
| 答题卡模板 / SVG / 打印 | `CARDS-API.md` | `/papers/{id}/cards`、`/cards/{id}/svg`、`/cards/{id}/export` |
| 切题与答题区标注 / 重切 / 发布校验 | `ANNOTATE-API.md` | `/imports/{id}/pages/{page_no}/annotation`（GET/PUT，注意不是 `/annotate`）、`/imports/{id}/resegment`、`/imports/{id}/items/{item_id}/answer-box`、`/imports/{id}/publish` |
| 整卷练习（自测作答/判分） | `PAPERS-API.md` §7 | `/papers/{id}/attempts`、`/attempts*` |
| 题库统计 / JSON·CSV 导入导出 | `BANK-IO.md` | `/questions/stats`、`/questions/export`、`/questions/import` |

仍未实现（不返回假成功）：扫描 OMR 读卡（定位点检测/页面码校验/气泡识别）、试卷 PDF 服务端渲染
（`format=pdf` 未提供，前端用浏览器打印）、题目切图独立导出/OSS 上传。完整路由索引见
仓库根 `INTEGRATION-API-INDEX.md`。

## 7. OCR 块与演示附件（2026-11-24）

- 识别结果页（`pl_pages.ocr_blocks_json`）保存统一内容块：`id/kind/text/latex/table_html/
  row_count/col_count/confidence/bbox/norm/page/order/needs_review`；列表接口同时返回
  `has_blocks`、`ocr_block_count` 与 `block_notices`，前端据此显示「待校对/已校对」。
- 结构化字段只做加法：`kind` 仍为 `text|math|table|image`；公式后处理写 `latex`/
  `latex_engine`，表格后处理写 `table_html`/`table_engine`/`table_span`，均需人工校对。
- 学习附件新增 `geometry_demo`、`timeline`、`molecule_3d`、`physics_sandbox` 四类演示数据。
  写入前统一经 `normalize_demo_content` 白名单校验；只存参数，不存任意 HTML/脚本。
- 导出（Word/Anki/讲义/离线包）遇到 `table_html`/`latex` 时按结构渲染；字段缺失时回退纯文本，
  不报错、不伪造结构。
