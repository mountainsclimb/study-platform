#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成高考词汇种子 app/data/vocab_seed.json（方案 §7 批次六）。

数据源：skywind3000/ECDICT（MIT License，Copyright (c) 2025 Linwei）。
只保留事实性标注与词典内容中允许的字段；detail/audio 不使用。

用法：
    python scripts/build_vocab.py                 # 优先使用本地缓存，缺失时下载
    python scripts/build_vocab.py --force         # 强制重新下载
    python scripts/build_vocab.py --source x.csv  # 使用指定 CSV（离线/测试）
    python scripts/build_vocab.py --check         # 只校验磁盘种子与重建结果一致

缓存：%TEMP%\\zhixu-ecdict-cache\\ecdict.csv。
只依赖 Python 标准库。
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "app" / "data"
SEED = DATA_DIR / "vocab_seed.json"
LICENSE_FILE = DATA_DIR / "vocab_seed.LICENSE"
README_FILE = DATA_DIR / "vocab_seed.README.md"

UPSTREAM_REPO = "https://github.com/skywind3000/ECDICT"
UPSTREAM_RAW = "https://raw.githubusercontent.com/skywind3000/ECDICT/master/ecdict.csv"
UPSTREAM_COMMIT = "9c46923fb81a2eb6c264a3a1689562ea7bc56b6a"
CACHE_DIR = (
    Path(os.environ.get("TEMP") or os.environ.get("TMP") or tempfile.gettempdir())
    / "zhixu-ecdict-cache"
)
CACHE_FILE = CACHE_DIR / "ecdict.csv"

LIMIT = 8000
GAOKAO_TAG = "gk"
GENERATED_DATE = "2026-11-24"
WORD_RE = re.compile(r"^[a-z][a-z' -]{0,63}$")
MAX_TRANSLATION = 800
MAX_PHONETIC = 100
MAX_POS = 120
MAX_TAG = 120
MAX_EXCHANGE = 300

# ECDICT 的 MIT 许可全文（上游 LICENSE，Copyright (c) 2025 Linwei）。
UPSTREAM_LICENSE = """MIT License

Copyright (c) 2025 Linwei

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
"""


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _as_int(value) -> int | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        number = int(text)
    except ValueError:
        return None
    return number if number >= 0 else None


def _text(value, limit: int) -> str:
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    return text[:limit]


def download(force: bool = False) -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    if CACHE_FILE.is_file() and not force:
        return CACHE_FILE
    print(f"下载 ECDICT：{UPSTREAM_RAW}", file=sys.stderr)
    request = urllib.request.Request(UPSTREAM_RAW, headers={"User-Agent": "zhixu-vocab-builder/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=180) as response, \
                open(CACHE_FILE, "wb") as handle:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                handle.write(chunk)
    except (urllib.error.URLError, OSError) as error:
        raise SystemExit(f"下载 ECDICT 失败：{error}") from error
    return CACHE_FILE


def normalize_row(row: dict) -> dict | None:
    word = _text(row.get("word"), 64).lower()
    if not WORD_RE.match(word):
        return None
    translation = _text(row.get("translation"), MAX_TRANSLATION)
    if not translation:
        return None
    return {
        "word": word,
        "phonetic": _text(row.get("phonetic"), MAX_PHONETIC),
        "translation": translation,
        "pos": _text(row.get("pos"), MAX_POS),
        "tag": _text(row.get("tag"), MAX_TAG),
        "collins": _as_int(row.get("collins")),
        "oxford": _as_int(row.get("oxford")),
        "bnc": _as_int(row.get("bnc")),
        "frq": _as_int(row.get("frq")),
        "exchange": _text(row.get("exchange"), MAX_EXCHANGE),
    }


def rank_key(item: dict) -> tuple:
    frq = item.get("frq")
    return (frq if frq else 10 ** 9, item["word"])


def read_candidates(source: Path) -> tuple[list[dict], list[dict]]:
    """返回 (高考词, 其他可补齐词)。同词只保留首次出现的行。"""
    gaokao: list[dict] = []
    others: list[dict] = []
    seen: set[str] = set()
    with open(source, encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            item = normalize_row(row)
            if item is None or item["word"] in seen:
                continue
            seen.add(item["word"])
            if GAOKAO_TAG in item["tag"].split():
                gaokao.append(item)
            else:
                others.append(item)
    gaokao.sort(key=rank_key)
    others.sort(key=rank_key)
    return gaokao, others


def select_items(gaokao: list[dict], others: list[dict], limit: int = LIMIT) -> list[dict]:
    selected = gaokao[:limit]
    if len(selected) < limit:
        chosen = {item["word"] for item in selected}
        for item in others:
            if item["word"] in chosen:
                continue
            selected.append(item)
            chosen.add(item["word"])
            if len(selected) >= limit:
                break
    selected.sort(key=rank_key)
    return selected


def build_payload(source: Path, limit: int = LIMIT) -> tuple[dict, bytes]:
    gaokao, others = read_candidates(source)
    items = select_items(gaokao, others, limit)
    if len(items) < limit:
        raise SystemExit(f"ECDICT 可用词条不足：需要 {limit}，只找到 {len(items)}")
    payload = {
        "meta": {
            "source": "ECDICT",
            "repo": UPSTREAM_REPO,
            "source_commit": UPSTREAM_COMMIT,
            "generated": GENERATED_DATE,
            "limit": limit,
            "count": len(items),
            "fields": ["word", "phonetic", "translation", "pos", "tag",
                       "collins", "oxford", "bnc", "frq", "exchange"],
            "selection": "tag 含 gk 的词优先，按 frq 升序补齐到 limit；同词去重",
        },
        "items": items,
    }
    data = (json.dumps(payload, ensure_ascii=False, indent=1, sort_keys=False) + "\n").encode("utf-8")
    return payload, data


def render_readme(payload: dict, seed_sha: str, seed_size: int) -> str:
    meta = payload["meta"]
    return f"""# 高考词汇种子（ECDICT）

| 项 | 值 |
|---|---|
| 来源 | [skywind3000/ECDICT]({UPSTREAM_REPO})（MIT License，Copyright (c) 2025 Linwei） |
| 源文件 | `ecdict.csv`，上游提交 `{meta['source_commit']}` |
| 生成 | `scripts/build_vocab.py`（{meta['generated']}，确定性排序） |
| 条数 | {meta['count']} |
| 字段 | {', '.join(f'`{name}`' for name in meta['fields'])} |
| 筛选 | `tag` 含 `gk` 的高考词优先，按 `frq` 升序补齐到 {meta['limit']}；同词去重 |
| 种子 SHA-256 | `{seed_sha}` |
| 种子大小 | {seed_size} 字节 |

## 内容说明

- `tag` / `collins` / `oxford` / `bnc` / `frq` / `exchange` / `pos` 为事实性标注与统计，直接使用；
- `phonetic` / `translation` 属于词典内容，来源混合（ECDICT 聚合），入库前已抽样核对；
  `detail` / `audio` 为空或非本用例所需，不使用；
- 重新生成：`python scripts/build_vocab.py`（首次会下载 CSV 到临时缓存）；
  校验：`python scripts/build_vocab.py --check`。
"""


def write_outputs(source: Path, limit: int = LIMIT) -> tuple[str, int]:
    payload, data = build_payload(source, limit)
    seed_sha = sha256(data)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    SEED.write_bytes(data)
    LICENSE_FILE.write_text(UPSTREAM_LICENSE, encoding="utf-8")
    README_FILE.write_text(render_readme(payload, seed_sha, len(data)), encoding="utf-8")
    return seed_sha, len(data)


def check(source: Path, limit: int = LIMIT) -> int:
    if not SEED.is_file() or not README_FILE.is_file():
        print("种子或 README 不存在，请先运行 python scripts/build_vocab.py", file=sys.stderr)
        return 1
    _payload, rebuilt = build_payload(source, limit)
    disk = SEED.read_bytes()
    if disk != rebuilt:
        print(f"种子与重建结果不一致：磁盘 {len(disk)} 字节，重建 {len(rebuilt)} 字节",
              file=sys.stderr)
        return 1
    checksum = sha256(disk)
    readme = README_FILE.read_text(encoding="utf-8")
    if checksum not in readme:
        print("README 未登记当前种子的 SHA-256", file=sys.stderr)
        return 1
    print(f"--check 通过：{SEED.name}（{len(disk)} 字节，SHA-256 {checksum[:12]}…）")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="生成 ECDICT 高考词汇种子")
    parser.add_argument("--source", type=Path, default=None, help="ECDICT CSV 路径")
    parser.add_argument("--force", action="store_true", help="强制重新下载")
    parser.add_argument("--check", action="store_true", help="只校验磁盘种子")
    parser.add_argument("--limit", type=int, default=LIMIT, help="生成的词条数（默认 8000）")
    args = parser.parse_args()
    if args.limit < 1:
        print("--limit 必须为正整数", file=sys.stderr)
        return 2
    if args.source is not None:
        if not args.source.is_file():
            print(f"源文件不存在：{args.source}", file=sys.stderr)
            return 2
        source = args.source
    elif CACHE_FILE.is_file() and not args.force:
        source = CACHE_FILE
    elif args.check:
        print("缓存不存在，无法在离线模式下校验；请先运行一次构建", file=sys.stderr)
        return 1
    else:
        source = download(args.force)
    if args.check:
        return check(source, args.limit)
    seed_sha, seed_size = write_outputs(source, args.limit)
    print(f"已写出 {SEED.relative_to(ROOT)}（{seed_size} 字节，SHA-256 {seed_sha}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
