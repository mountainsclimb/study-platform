# 我的题库：统计 / JSON·CSV 导入导出（P1）

前缀 `/api/paperlab`，全部需登录（`current_user`），所有读写按 `user_id` 隔离；
软删除题目（`deleted_at` 非空）不参与统计与导出。实现：`app/paperlab/bank_io.py`；
测试：`tests/test_paperlab_bank_io.py`。全量契约见 `INTEGRATION-CONTRACT.md` §4.2。
全模块路由索引见仓库根 `INTEGRATION-API-INDEX.md`。

> 路由注册顺序：`bank_io.router` 在 `bank.router` 之前 include，保证
> `/questions/stats`、`/questions/export` 不会被 `/questions/{question_id}` 的整数路径参数抢先匹配。

## 1. 题库统计

`GET /api/paperlab/questions/stats?subject=&grade=`

```json
{
  "total": 12,
  "by_type": {"SINGLE": 5, "MULTIPLE": 1, "BLANK": 2, "SUBJECTIVE": 4, "MIXED": 0},
  "by_subject": [{"subject": "数学", "count": 9}, {"subject": "物理", "count": 3}],
  "by_status": {"draft": 2, "ready": 9, "archived": 1},
  "by_verify": {"pending": 8, "verified": 4, "rejected": 0},
  "missing_answers": 1,
  "missing_explanation": 3,
  "missing_tags": 2,
  "knowledge": {"total": 7, "top": [{"tag": "函数", "count": 6}, {"tag": "导数", "count": 3}]},
  "recent_7d": 4,
  "ai_generated": 2,
  "subject": "数学",
  "grade": ""
}
```

- 全部实时聚合自 `pl_questions`（不建统计表）；空库返回 0。
- `missing_answers`：无顶层答案且子题也无答案；`missing_explanation`：解析为空；
  `missing_tags`：知识点标签为空。
- `knowledge.total` 为去重知识点数，`top` 为出现次数最多的前 10 个。
- `recent_7d` 按 `created_at`；`ai_generated` 为有 `ai_job_id`（识别/补全产物）的题目数。
- `subject`/`grade` 筛选同时作用于统计全部分组（与列表筛选一致）。

## 2. 导出

`GET /api/paperlab/questions/export?format=json|csv&subject=&grade=&status=`

- 返回文件下载（`Content-Disposition: attachment`，中文文件名 `题库-学科-YYYYMMDD.json/csv`）。
- 导出上限 **5000** 题；超过返回 422「请缩小筛选条件（学科/年级/状态）」。
- `format=json`：题目数组（`db.public_question` 形状：`id/type/subject/grade/difficulty/knowledge_tags/
  content/status/verify_status/version/ai_*/source/has_answer/stem_preview/created_at/updated_at`），
  `content` 为完整内容（题干/选项/答案/解析/评分标准/子题/配图引用）。
- `format=csv`：UTF-8 **BOM** + 标准 CSV 转义（Excel 直接打开不乱码），列为：
  `type(题型),subject(学科),grade(年级),difficulty(难度),knowledge_tags(知识点)|分隔,
  stem(题干),options(A. 文本 | B. 文本),answers(|分隔),explanation(解析),rubric(评分标准),
  parts(子题摘要 JSON)`；表头为中文，导入同时兼容中英文表头。
- 非法 format 422。

## 3. 导入

`POST /api/paperlab/questions/import`（multipart）

| 字段 | 说明 |
|---|---|
| `file` | JSON 或 CSV 文件；≤10MB，单次 ≤2000 题 |
| `dry_run` | `true` 时只校验+去重预览，不写库 |

- JSON：题目数组，或 `{"items":[...]}` / `{"questions":[...]}` / `{"data":[...]}`；
  字段同导出 JSON（也接受扁平 `stem/options/answers/explanation/rubric/parts`）。
- CSV：中英文表头均可（`题型/学科/年级/难度/知识点/题干/选项/答案/解析/评分标准/子题摘要`）；
  `选项` 形如 `A. 秦朝 | B. 汉朝`，`答案` 用 `|` 分隔，`子题摘要` 为 JSON 数组。
- 校验复用 `schema.normalize_question_content`：题型/学科必填、选择题必须有选项且答案落在 label、
  混合题必须有 parts、非混合题不得含 parts、难度支持 0–1 / 0–5 / 0–10 / 中文标签。
- **去重**：按内容摘要（题型+学科+年级+难度+知识点+content 规范化 JSON 的 SHA-256）与本人
  现有题库及本文件内去重，重复计入 `skipped`，不覆盖已有题目。
- 正式导入写入 `pl_questions`：`status=draft`、`source=import`、`verify_status=pending`、`version=1`；
  逐条失败不阻断其余题目。

响应（dry_run 与正式导入同形状）：

```json
{
  "format": "json",
  "dry_run": true,
  "total": 3,
  "valid": 2,
  "would_create": 2,
  "would_skip": 0,
  "created": 0,
  "skipped": 0,
  "errors": [{"row": 3, "error": "未知的题型：BOGUS"}],
  "errors_truncated": false
}
```

- `row`：CSV 为文件行号（表头为第 1 行），JSON 为数组下标 +1（从 1 开始）。
- 错误最多返回 200 条（`errors_truncated=true` 表示还有更多）。
- 结构性错误（JSON 解析失败 / CSV 缺必要列 / 空文件 / 超过 2000 题 / 文件 >10MB）：
  - 文件超过 10MB → **413**「导入文件不能超过 10MB」；
  - 解析失败、缺列、空文件、超过 2000 题 → **422** 中文原因。
- 未登录 401；只能看到/导入到自己的题库。

## 4. 前端（`web/js/pages/mybank.js`）

- 顶部「题库统计」卡：总题数、待核验、缺答案、缺解析、缺知识点、近 7 天新增、AI 生成，
  以及题型分布条形与知识点 Top 标签；随学科/年级筛选与导入/删除自动刷新。
- 工具条：`导入题库`（对话框：选择文件 → 预览校验 dry_run → 确认导入 → 错误表）、
  `导出 JSON` / `导出 CSV`（按当前筛选下载）、`去试卷工坊识别导入`。
- 移动端：统计两列、工具按钮自动换行；空态引导导入/识别。
- 样式追加在 `web/styles-paperlab.css` 末尾（`mb-stats-*` / `mb-import-*`）。
