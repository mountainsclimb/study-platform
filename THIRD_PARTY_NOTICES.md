# 第三方数据与许可声明

本项目「志愿查询」使用第三方公开数据集；「图片试题转 LaTeX」预置适配层引用第三方开源 OCR 组件；顶栏专注计时、每日一签与 Anki 牌组导出引入了下列开源代码与数据。按上游许可证要求，随本项目保留以下版权与许可声明。

## 1. 数据集

| 项目 | 内容 |
|---|---|
| 名称 | GaokaoCompass — China College Admission Dataset（高考录取数据平台） |
| 作者 / 版权 | Copyright (c) 2025 choucisan |
| 上游仓库 | <https://github.com/choucisan/GaokaoCompass> |
| 数据发布 | <https://huggingface.co/datasets/choucsan/Gaokao-Compass-11M> |
| 许可证 | MIT License |
| 上游说明（摘录） | 数据来自各省教育考试院、阳光高考平台等公开渠道，经清洗、标准化和去重后整理为统一格式；覆盖 31 个省份、2017–2025 年，包含招生计划、院校投档线、专业录取分数和一分一段表。 |

## 2. MIT 许可证全文

```text
MIT License

Copyright (c) 2025 choucisan

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## 3. 本项目的使用与修改

- 只读取上游发布的 CSV 与 `meta.json`，未修改上游仓库或数据文件。
- `scripts/import_admission.py` 在本地做以下处理：统一列名与字段类型；把空值、`<NA>` 等占位符视为缺失；剔除院校名为空或占位的 Excel 空行（共 117,902 行）；为查询建立 SQLite 索引与院校、专业维度表。清洗不改变可查询记录的数值，只影响缺失值与空行。
- 生成物 `data/admission.sqlite3` 是派生的只读数据库，`data/` 已在 `.gitignore` 中，不随代码仓库分发。
- 上游的交互演示站（GitHub Pages）前端代码未附许可证，本项目没有复制其代码，页面与图表均为自行实现。

## 4. 官方渠道补采的批次线

- 来源：教育在线（eol.cn）公开页面 <https://www.eol.cn/e_html/gk/fsx/>，该页面标注数据整理自各省考试院公告；采集结果只保留「省份 / 年份 / 科类 / 批次 / 分数线」等事实数据，并保留来源 URL、抓取时间与页面 SHA-256（见 `data/official-sources/manifest.json`）。
- 处理：`scripts/collect_official_data.py` 仅读取公开页面，不绕过登录或付费墙；`scripts/import_official_data.py` 写入独立的只读库 `data/admission-official.sqlite3`，与主录取库分离。
- 局限：该公开页面对应 2020–2026 年；2017–2019 年及多数省份的一分一段表未以可解析格式公开，页面会按当前省份年份如实提示，不用推断值冒充原始数据。

## 5. 物理演示内核（physics-in-motion）

- 上游：<https://github.com/xuxuezhou/physics-in-motion>，提交 `d35b9a167fc4406814f5731fab6b1116890171df`；**MIT License，Copyright (c) 2026 xuxuezhou**，许可证全文见 `web/vendor/physics-in-motion/LICENSE`。
- 使用范围：仅使用其纯 TypeScript 物理层（`src/physics/**` 求解器与校验、`src/sim/**` 场景构建器与 `SimResult`）与 10 个自研教学案例的配置；未使用其 React/Next 页面、题库内容或 localStorage 状态层。
- 修改说明：用 esbuild 将物理层打包为浏览器 ESM（`physics-core.js`），并把其 React/SVG 渲染层改写为原生 DOM/SVG 播放器（`web/js/components/physicsPlayer.js`）；物理计算逻辑未改动，10 个演示的自动校验全部通过。
- 边界：动画为辅助理解，物理关系按题设理想模型模拟，不替代解析结论与官方答案；上游题库真题转录未逐条对照官方原卷，因此未导入。

## 6. Surya OCR（图片试题转 LaTeX 适配层）

| 项目 | 内容 |
|---|---|
| 名称 | Surya — OCR, layout analysis, reading order, table recognition in 90+ languages |
| 作者 / 版权 | Copyright (c) Vikas Paruchuri and Datalab Team |
| 上游仓库 | <https://github.com/datalab-to/surya> |
| PyPI | `surya-ocr`（本项目可选依赖，见 `requirements-surya.txt`） |
| 代码许可证 | Apache License 2.0 |
| 模型权重许可证 | Modified AI Pubs Open Rail-M License（研究、个人使用及融资/营收低于 500 万美元的初创公司免费；更大范围商用需向 Datalab 取得授权，见 <https://www.datalab.to/pricing>） |

- 用途：`app/ai/surya_latex.py` 将题目图片识别为内容块与整题 LaTeX（公式 KaTeX 兼容），为「上传题目图片 → LaTeX」功能预置能力；属可选依赖，未安装时降级、不影响现有功能。
- 本项目通过 pip 安装官方发布的 `surya-ocr` 包，未复制其源码；推理后端（vllm / llama-server）由部署方自备，模型权重按上游方式下载，本仓库不分发模型文件。
- 代码层面的使用遵循 Apache-2.0；模型权重的使用范围受上述 Open Rail-M 变体许可约束，商业部署前请自行确认授权范围。

## 7. python-pptx（AI 课堂 PPTX 导出）

| 项目 | 内容 |
|---|---|
| 名称 | python-pptx — Create, read, and update PowerPoint 2007+ (.pptx) files |
| 作者 / 版权 | Copyright (c) Steve Canny |
| 上游仓库 | <https://github.com/scanny/python-pptx> |
| 许可证 | MIT License |
| 本项目用法 | `requirements.txt` 声明 `python-pptx==1.0.2`；`app/classroom/exports.py` 用其生成课堂幻灯片 PPTX（每场景一页/按元素分页，文本、图片、表格、形状，公式以文本降级并在备注说明）。未修改其源码。 |


## 8. 专注计时内核（shigure）

| 项目 | 内容 |
|---|---|
| 名称 | Shigure: A Multi-Function Web Timer（多合一网页计时器） |
| 作者 / 版权 | Copyright (c) 2026 Hoochanlon |
| 上游仓库 | <https://github.com/hoochanlon/shigure>，提交 `5909fe7aa602bb99d9278ade9816ae1d62c5bf58` |
| 许可证 | MIT License（许可证全文见 `web/vendor/shigure/LICENSE`） |

- 使用范围：仅 `js/timer.js`（107 行计时状态机，含 `start/pause/resume/stop/restore/checkpoint`）。
- 修改说明：`web/vendor/shigure/timer.js` 原样保留；本项目在 `web/js/components/focusTimer.js` 中通过依赖注入接入 UI 与后端会话记录，未使用上游的 Vditor 看板（其从 jsDelivr 引 CDN，本项目要求离线可用）、壁纸、白噪音与 i18n 素材；界面全部按本项目 `el()` 重写。
- 边界：上游为 2026-07 新建仓库、无长期维护记录，因此只取计时内核并自留测试（`tests/test_focus.py`）。

## 9. 每日一句（古诗词 / 论语 / 幽梦影 数据集）

| 项目 | 内容 |
|---|---|
| 名称 | chinese-poetry（中华古诗词数据库）子集 |
| 作者 / 版权 | Copyright (c) 2016 JackeyGao |
| 上游仓库 | <https://github.com/chinese-poetry/chinese-poetry> |
| 许可证 | MIT License（许可证全文见 `web/vendor/quotes/LICENSE`） |
| 生成物 | `web/vendor/quotes/daily-quotes.json`（365 条，字段 `id/q/a/t/s`；来源、抽取规则、SHA-256 见同目录 `README.md`） |

- 使用范围：仅取 `宋词/宋词三百首.json`、`诗经/shijing.json`、`论语/lunyu.json`、`幽梦影/youmengying.json`、`水墨唐诗/shuimotangshi.json` 的原文短句；**未使用**全量 `poet.*` / `ci.song.*` 数据，**未使用**任何现代译文、注释、赏析或评语（上游 `prologue`、`comment` 字段一律不读）。
- 修改说明：`scripts/build_daily_quotes.py` 在构建期做以下处理：按句/联切分、去标点后限 7–30 字、剔除含 ASCII 与残缺片段、跨来源按正文去重、固定随机种子抽样 365 条并编号；明显错字按「丢弃不改写」处理（清单见生成脚本与 README）。
- 数据性质：所收均为唐宋及先秦公有领域作品的原文，不含现代译注；上游以 MIT 许可分发其整理与格式化成果。

## 10. Anki 牌组导出（genanki）

| 项目 | 内容 |
|---|---|
| 名称 | genanki — Generate Anki decks programmatically |
| 作者 / 版权 | Copyright (c) Kerrick Staley |
| 上游仓库 | <https://github.com/kerrickstaley/genanki> |
| 版本 / 依赖 | PyPI `genanki==0.13.1`（传递依赖 `cached-property`、`frozendict`、`chevron`，`pyyaml` 本项目已有） |
| 许可证 | MIT License |

- 使用方式：作为 pip 依赖安装，未复制或修改其源码；仅用于生成 `.apkg`（zip + legacy schema11 `collection.anki2`）。
- 产物：错题本导出按学科分牌组（正面＝来源+题干+选项，背面＝参考答案+解析+我的错因/知识点），笔记导出沿用既有 Anki CSV 的切卡规则；含图题不打包题图，卡片内注明「图片请回题库查看」，不伪造内容。

## 11. 既有前端 vendored 库（补登记）

此前已在 `web/vendor/` 使用但未登记，现按 `CONTRACT.md` 约定补齐：

| 库 | 版本 | 许可证 | 位置 |
|---|---|---|---|
| KaTeX | 未在压缩产物中标注版本（以文件为准） | MIT | `web/vendor/katex/` |
| markdown-it | 14.1.0 | MIT | `web/vendor/markdown-it.min.js` |
| DOMPurify | 3.2.2 | Apache-2.0 与 MPL-2.0 双许可（上游可任选其一） | `web/vendor/purify.min.js` |

- 三者均为原样引入、未修改；按各自许可证保留版权声明即可。DOMPurify 采用 MPL-2.0 时属文件级弱 copyleft，本项目未修改其源码，因此不涉及派生文件开源义务。

## 12. 本地 OCR 与媒体/渲染依赖（2026-11-21 追加）

| 项目 | 内容 |
|---|---|
| 名称 | RapidOCR（PP-OCRv4 ONNX 推理封装） |
| 作者 | RapidOCR 项目组 |
| 仓库 | <https://github.com/RapidAI/RapidOCR> |
| 许可证 | Apache License 2.0 |
| 用途 | `app/ai/ocr.py`：扫描件/拍照作业的本地文字识别（离线、CPU），作为视觉模型的结构化提示与无模型时的降级路径 |

| 项目 | 内容 |
|---|---|
| 名称 | ONNX Runtime |
| 版权 | Copyright (c) Microsoft Corporation |
| 许可证 | MIT License |
| 用途 | RapidOCR 的推理运行时（`onnxruntime==1.20.1`） |

| 项目 | 内容 |
|---|---|
| 名称 | OpenCV（opencv-python / opencv-python-headless） |
| 许可证 | Apache License 2.0 |
| 用途 | RapidOCR 的图像预处理；答题卡 OMR（定位点/透视矫正/气泡检测） |

- 模型权重：RapidOCR 随包分发 PP-OCRv4 检测/识别/方向分类模型（Apache-2.0），本仓库不分发大模型文件。
- 部署注意：`onnxruntime` 在 slim 镜像需要 `libgomp1`；课堂视频导出需要系统 `ffmpeg`（LGPL/GPL 取决于构建，
  本项目仅以子进程调用，不链接其库，也不分发其二进制）。二者已写入 `deploy/Dockerfile`。

| 项目 | 内容 |
|---|---|
| 名称 | pdfplumber（文本型 PDF 表格抽取） |
| 版权 | Copyright (c) 2015-2025 Jeremy Singer-Vine |
| 仓库 | <https://github.com/jsvine/pdfplumber> |
| 许可证 | MIT License |
| 用途 | `app/ai/pdf_tables.py`：知识清单上传文本型 PDF 时把表格行追加到对应页；可选依赖 `requirements-pdf.txt`，开关 `STUDY_PDF_TABLES=on`（默认关闭） |
| 基础依赖 | pdfminer.six（MIT）、Pillow（MIT-CMU）、pypdfium2（Apache-2.0 / BSD-3-Clause，已在 `requirements.txt`） |

| 项目 | 内容 |
|---|---|
| 名称 | RapidLaTeXOCR（公式图片转 LaTeX） |
| 作者 | SWHL（RapidAI 项目） |
| 仓库 | <https://github.com/RapidAI/RapidLaTeXOCR> |
| 许可证 | 仓库 LICENSE 为 MIT；PyPI 元数据标 Apache-2.0；模型源自 LaTeX-OCR（MIT） |
| 用途 | `app/ai/formula_ocr.py`：拍题/扫描页 math 块的可选 LaTeX 识别；默认关闭（`STUDY_FORMULA_OCR=off`） |
| 模型权重 | 由 pip 包在首次初始化时下载，本仓库不分发；离线部署需预置到包内 `models/` |

| 项目 | 内容 |
|---|---|
| 名称 | RapidTable（表格结构识别） |
| 作者 | SWHL（RapidAI 项目） |
| 仓库 | <https://github.com/RapidAI/RapidTable> |
| 许可证 | Apache License 2.0 |
| 用途 | `app/ai/table_structure.py`：连续表格块的单元格结构与 HTML 生成；服务端只保留白名单标签/属性 |
| 模型权重 | 由 pip 包在首次初始化时下载，本仓库不分发 |

| 项目 | 内容 |
|---|---|
| 名称 | fastembed（本地向量与重排） |
| 版权 | Copyright (c) 2024 Qdrant |
| 仓库 | <https://github.com/qdrant/fastembed> |
| 许可证 | Apache License 2.0 |
| 用途 | `app/ai/embeddings_local.py`、`app/ai/rerank.py`：本地文本向量与交叉编码器重排；可选依赖 `requirements-embeddings.txt`，开关 `STUDY_EMBEDDING_MODE=local` / `STUDY_RERANK=on`（默认关闭） |
| 模型权重 | `bge-small-zh-v1.5`（MIT）与 `bge-reranker-base`（MIT）由部署方预置到本地模型目录，本仓库与镜像不分发 |

| 项目 | 内容 |
|---|---|
| 名称 | MathLive（可视化公式编辑） |
| 版权 | Copyright (c) 2021 Arno Gourdol |
| 仓库 | <https://github.com/arnog/mathlive> |
| 许可证 | MIT License（全文见 `web/vendor/mathlive/LICENSE.txt`） |
| 用途 | `web/js/components/formulaEditor.js`：拍题校对、题目表单与笔记编辑器的可视化公式编辑 |
| 分发 | `web/vendor/mathlive/` 随仓库分发（主脚本、样式、20 个 KaTeX 字体、LICENSE、README）；不引入按键音效 |

## 13. 数据边界与免责声明


- 本数据集为非官方整理，可能存在缺失字段与误差。已核实的情况包括：2017–2021 年多数省份缺少录取位次与批次控制线（约 94% 的省年完全没有位次），2023–2025 年部分省份缺少招生计划人数，个别省年录取表的科类整列为空。页面会按当前省份年份如实提示，不用推断值补足；逐省年的完整缺口清单由 `scripts/report_data_gaps.py` 生成到 `数据缺失声明.md` 与 `data/admission-gaps.json`，接口为 `GET /api/admission/gaps`。
- 志愿填报与录取结果以各省教育考试院、招生院校正式公布为准；本项目及数据提供者不对据此作出的任何决定承担责任。
- 若分发本项目或其派生数据，请保留本文件与上述 MIT 版权与许可声明。

## 14. 统一来源登记表（软件组件与 vendored 源码）

> 登记口径：凡随仓库分发、或被打包进发布产物的第三方代码都登记在此；「构建命令」给出可复现路径；
> 「分发方式」区分随仓库分发 / 运行时安装 / 不进仓库。Python 依赖的许可证以各包元数据为准，
> npm 依赖的完整版本与许可证清单以 `editor/pnpm-lock.yaml` 为权威，本表只列直接依赖。

| # | 组件 | 上游 / 版本 | 许可证 | 位置 | 构建 / 获取命令 | 分发方式 |
|---|---|---|---|---|---|---|
| 1 | OpenMAIC 编辑器（dsl / geometry / renderer / editor） | github.com/THU-MAIC/OpenMAIC @ `18b68aea3a87d7626e805b136d9b908eb277b00a`；包版本 dsl 0.13.0 / geometry 0.1.0 / renderer 0.2.3 / editor 0.0.13 | MIT（Copyright (c) 2026 THU-MAIC） | `editor/vendor/openmaic/` → 构建产物 `web/editor/`（挂载为 `/static/editor`） | `cd editor && pnpm install && pnpm run build`；重新 vendoring：`pnpm run vendor`；产物校验：`python scripts/check_editor_build.py --build` | 随仓库分发（产物入库） |
| 2 | three.js | npm `three@0.186.1`（r186），esbuild 0.25.12 打包 | MIT（Copyright 2010-2026 three.js authors） | `web/vendor/interactive/three.module.min.js` | `python web/vendor/interactive/build_three.py` | 随仓库分发（服务端内联注入） |
| 3 | physics-in-motion 物理层 | github.com/xuxuezhou/physics-in-motion @ `d35b9a167fc4406814f5731fab6b1116890171df` | MIT（Copyright (c) 2026 xuxuezhou） | `web/vendor/physics-in-motion/`（`physics-core.js` 与 10 个教学案例） | 见该目录 README 的 esbuild 打包命令 | 随仓库分发（构建产物） |
| 4 | Shigure 计时内核 | github.com/hoochanlon/shigure @ `5909fe7aa602bb99d9278ade9816ae1d62c5bf58` | MIT（Copyright (c) 2026 Hoochanlon） | `web/vendor/shigure/timer.js`（上游原样保留） | 原样复制，无构建 | 随仓库分发 |
| 5 | chinese-poetry 子集 | github.com/chinese-poetry/chinese-poetry（master 五个简体原文 JSON） | MIT（Copyright (c) 2016 JackeyGao）；原文属公有领域 | `web/vendor/quotes/` | `python scripts/build_daily_quotes.py`（`--force` 重新下载） | 随仓库分发（生成物入库） |
| 6 | KaTeX | 页面版：`web/vendor/katex/`（版本以文件内标注为准）；编辑器内：npm `katex@0.16.38` | 代码 MIT；字体 SIL Open Font License 1.1 | `web/vendor/katex/`、`web/editor/assets/`（字体） | 随包引入，无独立构建 | 随仓库分发 |
| 7 | markdown-it | 14.1.0 | MIT | `web/vendor/markdown-it.min.js` | 原样引入 | 随仓库分发 |
| 8 | DOMPurify | 3.2.2 | Apache-2.0 或 MPL-2.0（上游双许可，可任选其一） | `web/vendor/purify.min.js` | 原样引入 | 随仓库分发 |
| 9 | 编辑器前端直接依赖 | React 19.2.3、React DOM 19.2.3、Vite 6.3.5、ECharts 6.0.0、ProseMirror 系列、Tailwind CSS 4、lucide-react（ISC）、motion（MIT）、shiki（已替换为本地轻量 stub）等 | MIT / ISC / Apache-2.0，逐包见 `editor/pnpm-lock.yaml` | 打包进 `web/editor/assets/` | `cd editor && pnpm install && pnpm run build` | 随仓库分发（构建产物） |
| 10 | Python 运行时依赖 | `requirements.txt`：FastAPI 0.136.0、uvicorn 0.45.0、httpx 0.28.1、cryptography 46.0.7、Pillow 12.2.0、pypdf 6.10.2、pypdfium2 5.14.0、python-docx 1.2.0、python-pptx 1.0.2、python-multipart 0.0.26、PyYAML 6.0.3、sympy 1.14.0、genanki 0.13.1、RapidOCR 1.4.4、ONNX Runtime 1.20.1；`deploy/requirements.txt`：arq 0.28、boto3 1.34、psycopg 3.2（LGPL-3.0）、psycopg_pool 3.2 | MIT / BSD / Apache-2.0 / LGPL-3.0，逐包见安装元数据 | 运行时 site-packages，不进仓库 | `pip install -r requirements.txt -r deploy/requirements.txt` | 运行时安装 |
| 11 | ffmpeg | 系统包（Debian bookworm） | LGPL/GPL 取决于发行构建；本项目仅以子进程调用，不链接、不分发二进制 | 容器镜像（`deploy/Dockerfile`） | `apt-get install -y --no-install-recommends libgomp1 ffmpeg` | 运行时安装（镜像内） |
| 12 | Playwright / Chromium（可选） | playwright 及其下载的浏览器 | Apache-2.0 | 本机/CI 浏览器的独立安装，不进仓库 | `pip install playwright && playwright install chromium` | 运行时安装（可选服务） |
| 13 | Surya OCR（可选） | `surya-ocr`（`requirements-surya.txt`） | 代码 Apache-2.0；权重 Modified AI Pubs Open Rail-M（见 §6） | 运行时 site-packages；权重由部署方按上游下载 | `pip install -r requirements-surya.txt` | 运行时安装（可选，权重不进仓库） |
| 14 | 题库发布包（只读） | `高考题库_按学科/releases/`，当前 `CURRENT` → `bank-20261121-r5`（26,374 题 / 538 组 / 1,873 卷） | 各来源不同，逐来源见 §17；题目版权归原命题机构 | `../../高考题库_按学科/releases/`，应用只读挂载 | `tools/ingest/build_release.py`（题库仓库内） | 不进仓库（只读挂载） |
| 15 | axe-core（开发期无障碍扫描） | `axe-core@4.14.1` | MPL-2.0 | `scripts/vendor/axe.min.js`（来源、大小与 SHA-256 见同目录 README） | `npm pack axe-core@4.14.1`，取包内 `axe.min.js` 与 `LICENSE` | 随仓库分发（仅开发脚本）；`.dockerignore` 已排除 `scripts/vendor/`，不进镜像 |
| 16 | RapidFuzz（开发期题库去重） | `rapidfuzz==3.14.6`（`requirements-dev.txt`） | MIT | 运行时 site-packages，不进仓库 | `pip install -r requirements-dev.txt` | 运行时安装（开发依赖），不进生产镜像 |
| 17 | pdfplumber（文本型 PDF 表格） | `pdfplumber==0.11.10`（`requirements-pdf.txt`；0.11.11 要求 Pillow≥13，与工程锁定版本不兼容） | MIT | 运行时 site-packages，不进仓库 | `pip install -r requirements-pdf.txt`；开关 `STUDY_PDF_TABLES=on` | 运行时安装（可选），不进镜像 |
| 18 | RapidLaTeXOCR（公式识别） | `rapid-latex-ocr==0.0.9`（`requirements-formula.txt`） | MIT / PyPI 元数据 Apache-2.0；模型源自 LaTeX-OCR（MIT） | 运行时 site-packages；模型随包下载，不进仓库 | `pip install -r requirements-formula.txt`；开关 `STUDY_FORMULA_OCR=on` | 运行时安装（可选），不进镜像 |
| 19 | RapidTable（表格结构） | `rapid-table==3.0.2`（`requirements-formula.txt`） | Apache-2.0 | 运行时 site-packages；模型随包下载，不进仓库 | `pip install -r requirements-formula.txt`；开关 `STUDY_TABLE_OCR=on` | 运行时安装（可选），不进镜像 |
| 20 | MathLive（可视化公式编辑） | `mathlive@0.111.4` | MIT | `web/vendor/mathlive/`（版本、大小与 SHA-256 见同目录 README） | `npm pack mathlive@0.111.4`，取主脚本、样式、字体与 LICENSE | 随仓库分发 |
| 21 | fastembed（本地向量/重排） | `fastembed==0.9.1`（`requirements-embeddings.txt`） | Apache-2.0 | 运行时 site-packages；模型由部署方预置，不进仓库 | `pip install -r requirements-embeddings.txt`；开关 `STUDY_EMBEDDING_MODE=local`、`STUDY_RERANK=on` | 运行时安装（可选），不进镜像 |

## 15. 素材、语料与模型权重

| 类别 | 内容 | 来源与许可 | 分发边界 |
|---|---|---|---|
| 题目正文与解析 | 高考真题/模拟题题干、选项、答案、解析 | 逐来源见 §17；版权归原命题机构 | 不进代码仓库，只读发布包；公开投放需按 §17 逐来源确认授权 |
| 题目配图 | 发布包内题图（r5 约 10,079 道图题） | 随题目来源（GAOKAO-MM 补图等），见 §17 | 同上；个人上传原图只用于私人处理，不默认公开（见方案 §5） |
| 录取数据 | 31 省 2017–2025 招生计划/投档线/专业分/一分一段 | GaokaoCompass，MIT（§1） | 派生只读库 `data/` 不入库；页面保留来源与 MIT 声明 |
| 官方批次线 | 2020–2026 省年批次线 | 教育在线公开页面（§4），保留来源 URL 与页面 SHA-256 | 只保留事实字段 |
| 每日一句语料 | 365 条唐宋/先秦原文 | chinese-poetry（MIT）+ 公有领域原文；构建期抽样与去重 | 生成物入库，保留上游 LICENSE |
| OCR 模型权重 | PP-OCRv4 检测/识别/方向分类 | Apache-2.0，随 RapidOCR pip 包分发 | 不进仓库 |
| 本地向量模型权重 | bge-small-zh-v1.5（512 维） | MIT，由部署方预置 | 不进仓库与镜像；目录见 `requirements-embeddings.txt` |
| 本地重排模型权重 | bge-reranker-base | MIT，由部署方预置 | 不进仓库与镜像 |
| 公式识别模型权重 | LaTeX-OCR 编码器/解码器/图像缩放/tokenizer | 源自 LaTeX-OCR（MIT），随 rapid-latex-ocr pip 包下载 | 不进仓库；离线部署需预置到包内 `models/` |
| 表格结构模型权重 | SLANet Plus | 随 rapid-table pip 包下载；许可以包内说明为准 | 不进仓库；离线部署需预置 |
| Surya 模型权重 | 版面/文字识别/公式 | Modified AI Pubs Open Rail-M（商用门槛见 §6） | 部署方自行下载，不进仓库；商用前确认授权 |
| 字体 | KaTeX 数学字体 | SIL OFL 1.1 | 随构建产物分发；不引用外部字体 CDN |
| 图标 | lucide-react 图标 | ISC | 随编辑器构建产物分发 |

## 16. 能力可用性分级

> 口径：**默认可用**＝标准安装后无需额外配置即可工作；**可选服务**＝需要配置密钥/模型/外部服务
> 或安装额外系统依赖；**实验**＝默认关闭、需要在方案验证后决定是否转正；**规划中**＝尚未实现。

| 能力 | 状态 | 依赖 / 开关 | 说明 |
|---|---|---|---|
| 题库检索、练习、错题、复习（规则排程） | 默认可用 | 只读题库挂载 + SQLite | 核心学习路径；复习排程默认 `rules` |
| 个人题库、工坊组卷/答题卡/整卷练习/导出 | 默认可用 | 本地 SQLite | 试卷快照与练习版本冻结见方案 §3 |
| 每日一句、专注计时、物理演示、Anki 导出 | 默认可用 | 随仓库构建产物 | 见 §8–§10、§5 |
| 自由画布编辑器 | 默认可用 | 构建产物入库；Node 仅构建时需要 | 类型检查：`cd editor && pnpm exec tsc --noEmit` |
| 本地 OCR（RapidOCR，CPU） | 默认可用 | `requirements.txt` 已锁定 | 缺依赖时按代码降级，不影响练习 |
| AI 讲解/助手/课堂生成 | 可选服务 | 模型提供方配置（`app/ai/providers.py`）；出站受 `netguard.py` 策略约束 | 读题与扣费动作需用户确认 |
| AI 课堂视频/PPTX/PDF 导出 | 可选服务 | ffmpeg（镜像已装）+ Playwright/Chromium | Chromium 未安装时能力探针降级，见方案 §3 P1 |
| Surya 公式转 LaTeX | 实验（待移除） | `requirements-surya.txt` + 推理后端 + 权重许可确认 | 已由拍题的公式识别后处理取代；保留一个版本周期后删除 |
| 公式图片识别（RapidLaTeXOCR） | 实验 | `requirements-formula.txt` + `STUDY_FORMULA_OCR=on`；超时/上限见 `STUDY_FORMULA_TIMEOUT`、`STUDY_FORMULA_MAX_BLOCKS` | 默认关闭；只填 math 块的 `latex`，`needs_review` 保持 true；失败/超时块原样 |
| 表格结构（RapidTable） | 实验 | `requirements-formula.txt` + `STUDY_TABLE_OCR=on`；超时 `STUDY_TABLE_TIMEOUT` | 默认关闭；只写连续表格块的第一块，HTML 经服务端白名单清洗 |
| 可视化公式编辑（MathLive） | 默认可用 | `web/vendor/mathlive/` 随仓库分发；首次打开懒加载 | 加载失败回退现有文本公式输入 |
| 文本型 PDF 表格（pdfplumber） | 实验 | `requirements-pdf.txt` + `STUDY_PDF_TABLES=on`；页数上限 `STUDY_PDF_TABLE_PAGES` | 默认关闭；只补有表格线的文本型 PDF，扫描件与无边框表格不生成内容；失败只跳过 |
| 向量检索 / embedding | 实验 | HTTP：`STUDY_EMBEDDING_BASE_URL`、`STUDY_EMBEDDING_MODEL` 等；本地：`requirements-embeddings.txt` + `STUDY_EMBEDDING_MODE=local` | 默认关闭；本地模型不可用时回退本地全文检索 |
| 本地重排（fastembed 交叉编码器） | 实验 | `STUDY_RERANK=on` + 本地重排模型目录 | 默认关闭；失败沿用原排序 |
| fsrs-lite 复习排程 | 实验 | `STUDY_REVIEW_SCHEDULER=fsrs-lite` | 默认 `rules`；先积累复习反馈再替换 |
| 社区解析精选 | 可选服务 | `COMMUNITY_EXPLANATIONS_ENABLED=1` | 登录用户的既有题目 AI 解析分享 |
| PostgreSQL / Redis Worker / 对象存储 | 可选服务（生产推荐） | `DATABASE_URL`、`REDIS_URL`、`S3_*`；公网部署校验见 `scripts/check_public_config.py` | SQLite 单机开发可用 |
| 广场、投稿、结构化评审、官方精选发布 | 规划中 | 方案 §5–§6 Phase 2/3 | 未实现；上线前需完成来源授权、评审与发布链 |
| 金钱交易、积分兑换、私信、榜单、第三方学校系统 | 暂缓 | 方案 §7 | 下一轮不排期 |

## 17. 代码许可与题目内容再分发权（分列审核）

**代码许可**与**题目内容再分发权**是两条独立链路，不能互相替代：

- 代码：本仓库随附/依赖的第三方代码许可见 §1–§14；分发时须保留对应 LICENSE 与版权声明
  （OpenMAIC、physics-in-motion、shigure、three.js、chinese-poetry、KaTeX、markdown-it、
  DOMPurify、各 pip/npm 依赖）。**本项目自有代码的对外许可尚未在仓库根目录明确声明**
  （仓库当前没有根 LICENSE 文件），对外分发或开源前需要先确定并补充。
- 内容：题目正文、答案、解析、配图的再分发权按来源分别审核，各发布包 `manifest.json`
  只记录来源与计数，**不等于**已取得公开分发授权：

| 题目来源 | 授权状态 | 可用于 | 限制 / 待办 |
|---|---|---|---|
| 数学：DxAThing/Gaokao-Math-Problems-Compilation（上游 deekur/gaokaomath） | CC BY-SA 4.0 | 站内学习、派生数据集（需署名 + 相同方式共享） | 公开投放时保留署名与来源；派生内容受 ShareAlike 约束 |
| 物理：YLXDXX/gaokao-physics-archive | 仓库未声明正式许可证 | 仅内部比对与研究使用 | **对外分发/公开广场前必须联系作者取得授权**，否则不得公开 |
| 语文/英语/化学/生物/政治/历史/地理及数理补充：rainewhk/gaokao | Apache-2.0（内含 GAOKAO-Bench 系数数据） | 站内学习、按 Apache-2.0 再分发 | 保留 NOTICE/来源；题目原文版权仍归原命题机构 |
| 带图题补图与新增：OpenMOSS/GAOKAO-MM | Apache-2.0 | 站内学习；配图随数据许可 | 公开使用需保留来源与许可声明 |
| 2025 各省真题：jeinlee1991/chinese-llm-benchmark `opendata` | 仓库未声明 LICENSE | 仅比赛/内部使用 | **不得进入公开广场或对外分发**，仅可用于离线评测 |
| 平台原创题、授权教师题、用户投稿（后续） | 由作者与平台授权协议确定 | 需在投稿时声明来源、许可与权利范围 | 方案 §5 的首批投稿字段；无授权不放公开 |

题目版权归原命题机构所有，本站当前仅用于学习与竞赛用途。**「平台精选」上线前**必须逐来源完成
授权确认（方案 §3 P0 与 §5），只允许原创题、授权教师题和确认可公开的材料进入公开广场。
