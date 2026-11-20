# 知序分层评测（P1「评测分层与可追溯」）

本目录同时保存两套评测集：

| 文件 | 说明 |
| --- | --- |
| `golden.jsonl` | 旧 100 题纯文本基线，**保留不动**，供历史报告对照。 |
| `golden-v2.jsonl` | 新分层评测集，默认由 `python -m app.eval --build-golden --strata` 生成。 |

题库始终只读；构建只读取 `practice._bank()` 与 `practice.load_question(qid)`，不会写回题库。

## 一、四个分层

| stratum | 含义 | 判定口径 |
| --- | --- | --- |
| `text` | 纯文本题干 | 未命中下列任何一项。 |
| `figure_formula` | 原卷图片与公式 | `has_figure/has_formula`、图片块、公式块（含 `fallback_asset`）、`[图N]`/`[公式N]` 占位、材料图片任一命中。 |
| `material_group` | 阅读材料/共用题干 | 题目有 `group_id`（可关联到 `groups` 记录）。 |
| `group_multipart` | 多问题组 | 题组成员数 > 1（`n_members`、`n_children_total`、`member_index`、`children` 取最大值）。 |

一道题可以同时命中多个分层。记录中的 `strata` 是命中项列表（固定顺序），`stratum` 是互斥主分层，优先级为
`group_multipart > material_group > figure_formula > text`。报告里同时给出：

- `metrics.strata`：一题多标签口径，一道题会出现在它命中的每一层；
- `metrics.stratum_breakdown`：互斥主分层口径，一道题只计一次；
- `metrics.strata_counts`：各层去重后的题数（多标签）。

旧题库（例如没有 `stem_blocks_json` 的快照）按 `images_json`、`has_figure/has_formula`、`group_id` 与
`groups.n_children_total` 降级判定；脱离题组的 `sub_*` 子题不入选，避免把缺失材料当成纯文本题评测。

## 二、抽样与构建

```powershell
# 默认写 eval/golden-v2.jsonl，不覆盖 eval/golden.jsonl
python -m app.eval --build-golden --strata

# 小样本 / 指定路径
python -m app.eval --build-golden --strata --count 12 --out eval/scratch/golden-v2.jsonl

# 旧基线行为保持不变
python -m app.eval --build-golden

# 执行评测 / A-B 对比（原有参数继续可用）
python -m app.eval --provider-id 1 --model gpt-4o `
  --verify-provider-id 2 --verify-model deepseek-chat --limit 20 --out eval/report.json
python -m app.eval --compare eval/report_a.json eval/report_b.json
```

- 固定 seed（默认 `20261120`），题库版本与输入不变时，输出逐字节可复现。
- 四个分层各有配额（`count // 4`，余数按分层顺序分配）；同一题命中多层时可同时满足多个配额，
  之后用剩余候选补足总条数，不重复凑数。
- 分层内按学科轮转并给各分层不同的学科起点，小样本也尽量覆盖多学科。
- 题库声明了 `bank_version` 时原样记录；否则依次退回题库目录 `manifest.json` 与
  `题库.db` 内容摘要（`sha256:xxxxxxxxxxxx`），并在 `bank_version_source` 中标明来源。

## 三、记录字段（golden-v2）

每条 JSONL 至少包含：

| 字段 | 说明 |
| --- | --- |
| `qid` | 题库唯一题号。 |
| `subject` / `type` / `type_label` | 学科、题型代码、题型中文名。 |
| `strata` | 命中分层列表，例如 `["figure_formula","material_group"]`。 |
| `stratum` | 互斥主分层，用于 `stratum_breakdown`。 |
| `bank_version` / `bank_version_source` | 题库版本及版本来源（`question`/`manifest`/`content_sha256`/`unknown`）。 |
| `reference_answer` | 可信参考答案（题库 `answer_text`，非空才入选）。 |
| `reference_source` | 答案来源说明，含 `binding_method`、`answer_revision`、`answer_note`。 |
| `content_refs` | 计划送给模型的内容引用列表，见下。 |
| `stem_plain` / `stem_text` | 题干原文与最终送入模型的题干文本。 |
| `options` | `[{label,text}]`；选项图片/公式通过 `content_refs` 送入。 |
| `material_text` / `shared_text` | 阅读材料、父题/共用题干文本；执行评测时会一并送给模型。 |
| `answer_kind` | `choice`/`number`/`expression`/`text`，决定程序化判分方式。 |
| `bank_quality_flags` | 题库侧质量标记；缺图等缺项会保留，供 `missing_inputs` 定位。 |
| `prompt_version` | 构建时的提示词版本；执行报告会覆盖为本次运行版本。 |

`content_refs` 每项：

```json
{
  "field": "stem|option:A|material|shared",
  "field_label": "题干|选项A|材料|共用题干",
  "block_id": "b000094",
  "kind": "text|math|table|image",
  "role": "figure|formula|null",
  "asset": "images/4ad1bb495c60875b4445.png",
  "has_image": true,
  "required": true,
  "fallback_text": "latex 或可读文本（可选）",
  "missing_reason": "题库未提供对应的原卷图片（可选）"
}
```

## 四、执行时真正送图

`run_evaluation` 会按 `content_refs` 读取原卷图片，经 `app.ai.verify._image_part`
（接口不可用时使用等价实现）转为图片输入，和文字题干/选项/材料一起送给主模型与核验模型。

- 单图上限 12 MB，单题图片总量上限 20 MB，最多 40 张；超限/缺文件/格式不可读时跳过该图，
  不中断评测，并把原因写进 `missing_inputs`。
- 公式块有 `fallback_asset` 时按公式图送入；图片不可读时，若存在 `latex`/可读文本，
  会作为「公式文本降级」附在提示词中，但原卷公式图仍记为未送达。
- 旧 `golden.jsonl` 没有 `content_refs` 时，按文字题干/选项补出内容单元，行为保持兼容。

## 五、指标口径

每条评测记录都会写入：

| 字段 | 口径 |
| --- | --- |
| `input_completeness` | 题库应有内容单元的送达比例（0–1）= 实际送达单元数 / 应有单元数。文本/表格/纯 LaTeX 公式块视为随题干送达；图片/公式图必须真正作为图片输入送达才计数。 |
| `missing_inputs` | 中文缺失说明，例如「题干公式图未送入（images/x.png；单图超过 12 MB 限制）」「材料原卷图未送入（题库未提供对应的原卷图片）」。 |
| `content_refs` | 带 `delivered: true/false` 的实际内容引用快照。 |
| `prompt_version` / `model` / `verify_model` | 本次运行的提示词版本与主/核验模型。 |
| `bank_version` / `reference_source` | 与 golden-v2 记录一致，便于按版本回溯。 |
| `cost` | 本题主/核验调用费用；任一调用未知时 `estimated_cost` 为 `null`，并给出 `unknown_cost_calls`。 |

报告 `metrics`：

| 字段 | 口径 |
| --- | --- |
| `total` / `single_correct` / `single_accuracy` | 总数、主模型答对数、答对率（%）。 |
| `input_completeness` | 所有记录的题库输入完整率均值（0–1）。 |
| `input_stats` | 缺失记录数、缺失原因计数、应有/已送达单元总数。 |
| `effective_verification_rate` | 可比较的程序化核验题数 / 总题数（%）；无核验模型时为 `null`。 |
| `agreement_rate` | 主/核验模型一致题数 / 总题数（%）。 |
| `cross_verified_accuracy` | 一致的题中主模型正确的比例（%），保留旧字段语义。 |
| `agreed_but_wrong_count` / `agreed_but_wrong_rate` | 一致但答错的数量/占比（分母为一致题数）。 |
| `strata` / `stratum_breakdown` / `strata_counts` | 分层结果，见第一节。 |
| `wrong_qids` / `missing_input_qids` | 模型答错题号、存在题库输入缺项的题号（最多各 200 条；完整明细看 `records`），用于单独定位。 |
| `cost` | `estimated_cost`、`known_cost_subtotal`、`known_cost_calls`、`unknown_cost_calls`、`total_calls`、`cost_complete`、`note`。 |
| `unknown_cost_calls` | 顶层冗余计数，便于直接告警。 |

费用只在 `ai_models.input_price/output_price`（元/百万 tokens）和 token 用量都齐全时估算。
**未知费用一律显示为 `null` 并单独计数，绝不当成 0 元或免费**；部分已知时只给出
`known_cost_subtotal`，同时把整次评测的 `cost_complete` 标为 `false`。

## 六、落库（迁移 013）

报告同时写入 `eval_runs` 的 `bank_version`、`strata`、`input_stats_json`、`report_json` 列。
旧库没有这些列时自动退回旧字段插入；仍失败也只影响落库，不影响本地返回报告
（`saved_to_eval_runs` 标明是否成功）。
