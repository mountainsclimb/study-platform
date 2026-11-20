# 第三方数据与许可声明

本项目「志愿查询」使用第三方公开数据集。按上游 MIT 许可证要求，随本项目保留以下版权与许可声明。

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

## 4. 数据边界与免责声明

- 本数据集为非官方整理，可能存在缺失字段与误差。已核实的情况包括：2017–2021 年多数省份缺少录取位次与批次控制线（约 94% 的省年完全没有位次），2023–2025 年部分省份缺少招生计划人数，个别省年录取表的科类整列为空。页面会按当前省份年份如实提示，不用推断值补足。
- 志愿填报与录取结果以各省教育考试院、招生院校正式公布为准；本项目及数据提供者不对据此作出的任何决定承担责任。
- 若分发本项目或其派生数据，请保留本文件与上述 MIT 版权与许可声明。
