# 高考词汇种子（ECDICT）

| 项 | 值 |
|---|---|
| 来源 | [skywind3000/ECDICT](https://github.com/skywind3000/ECDICT)（MIT License，Copyright (c) 2025 Linwei） |
| 源文件 | `ecdict.csv`，上游提交 `9c46923fb81a2eb6c264a3a1689562ea7bc56b6a` |
| 生成 | `scripts/build_vocab.py`（2026-11-24，确定性排序） |
| 条数 | 8000 |
| 字段 | `word`, `phonetic`, `translation`, `pos`, `tag`, `collins`, `oxford`, `bnc`, `frq`, `exchange` |
| 筛选 | `tag` 含 `gk` 的高考词优先，按 `frq` 升序补齐到 8000；同词去重 |
| 种子 SHA-256 | `bcb815dddb48e310c89e8bb06a52bf7bbe8980c74b7a18bdd47a3d70db4784c2` |
| 种子大小 | 2324415 字节 |

## 内容说明

- `tag` / `collins` / `oxford` / `bnc` / `frq` / `exchange` / `pos` 为事实性标注与统计，直接使用；
- `phonetic` / `translation` 属于词典内容，来源混合（ECDICT 聚合），入库前已抽样核对；
  `detail` / `audio` 为空或非本用例所需，不使用；
- 重新生成：`python scripts/build_vocab.py`（首次会下载 CSV 到临时缓存）；
  校验：`python scripts/build_vocab.py --check`。
