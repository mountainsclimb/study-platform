#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成「每日一句」离线数据集 web/vendor/quotes/daily-quotes.json。

数据源：GitHub chinese-poetry/chinese-poetry（MIT License，Copyright (c) 2016 JackeyGao）。
只取 5 个简体原文文件，只保留古文原文，丢弃现代译文 / 注释 / 赏析 / 评语。

用法：
    python scripts/build_daily_quotes.py            # 优先使用本地缓存
    python scripts/build_daily_quotes.py --force    # 强制重新下载

缓存目录：%TEMP%\\zhixu-quotes-cache\\（按上游原文件名保存）。
只依赖 Python 标准库。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "web" / "vendor" / "quotes"
JSON_NAME = "daily-quotes.json"
LICENSE_NAME = "LICENSE"
README_NAME = "README.md"

UPSTREAM_REPO = "https://github.com/chinese-poetry/chinese-poetry"
UPSTREAM_RAW_BASE = (
    "https://raw.githubusercontent.com/chinese-poetry/chinese-poetry/master/"
)
CACHE_DIR = (
    Path(os.environ.get("TEMP") or os.environ.get("TMP") or tempfile.gettempdir())
    / "zhixu-quotes-cache"
)

GENERATED_DATE = "2026-11-21"
RANDOM_SEED = 20261121
TOTAL_QUOTES = 365
MIN_QUOTES_PER_SOURCE = 40

MIN_HANZI = 7
MAX_HANZI = 30

# 上游 MIT 许可全文（含版权行）。
UPSTREAM_LICENSE = """The MIT License (MIT)

Copyright (c) 2016 JackeyGao

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
THE SOFTWARE.
"""

# ---------------------------------------------------------------------------
# 来源定义
#
# unit:
#   "segments"  —— 诗：`paragraphs` / `content` 的每个元素（一联 / 一段）自成一个候选
#   "sentences" —— 词 / 论语 / 幽梦影：按 。！？； 切句，每句一个候选
#
# 只读取 text_fields 列出的字段；prologue（现代译文）、comment（他人评语）、
# tags 等一律不读。
# ---------------------------------------------------------------------------

SOURCES = (
    {
        "key": "songci",
        "label": "宋词三百首",
        "path": "宋词/宋词三百首.json",
        "min_records": 200,
        "unit": "sentences",
        "text_fields": ("paragraphs",),
        "author_field": "author",
        "title_field": "rhythmic",
        "fixed_author": None,
        "record_unit": "首",
    },
    {
        "key": "shijing",
        "label": "诗经",
        "path": "诗经/shijing.json",
        "min_records": 250,
        "unit": "segments",
        "text_fields": ("content",),
        "author_field": None,
        "title_field": "title",
        "fixed_author": "佚名",
        "record_unit": "篇",
    },
    {
        "key": "lunyu",
        "label": "论语",
        "path": "论语/lunyu.json",
        "min_records": 15,
        "unit": "sentences",
        "text_fields": ("paragraphs",),
        "author_field": None,
        "title_field": "chapter",
        "fixed_author": "孔子",
        "record_unit": "篇",
    },
    {
        "key": "youmengying",
        "label": "幽梦影",
        "path": "幽梦影/youmengying.json",
        "min_records": 150,
        "unit": "sentences",
        "text_fields": ("content",),
        "author_field": None,
        "title_field": None,
        "fixed_author": "张潮",
        "record_unit": "则",
    },
    {
        "key": "shuimo",
        "label": "水墨唐诗",
        "path": "水墨唐诗/shuimotangshi.json",
        "min_records": 120,
        "unit": "segments",
        # 只取 paragraphs；prologue 是现代译文，显式忽略。
        "text_fields": ("paragraphs",),
        "author_field": "author",
        "title_field": "title",
        "fixed_author": None,
        "record_unit": "首",
    },
)

# 人工抽查时发现的「上游明显错字」条目：直接丢弃，不改写原文。
# 键为最终 q 全文（保留原标点）。新增时请写明错字原因。
MANUAL_EXCLUSIONS: frozenset[str] = frozenset(
    {
        # 诗经《兔罝》第三章上游写成「肃肃免罝」（应为「兔罝」，前两章均为「兔罝」）。
        "肃肃免罝，施于中林。赳赳武夫，公侯腹心。",
        # 诗经《角弓》上游写成「至于已斯亡」（应为「至于己斯亡」）。
        "民之无良，相怨一方。受爵不让，至于已斯亡。",
    }
)

# ---------------------------------------------------------------------------
# 文本处理
# ---------------------------------------------------------------------------

CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
ASCII_RE = re.compile(r"[\x00-\x7f]")
PAREN_RE = re.compile(r"[（）()]")
DUP_TERMINAL_RE = re.compile(r"[。！？；]{2,}")
SENTENCE_SPLIT_RE = re.compile(r"(?<=[。！？；])")
WHITESPACE_RE = re.compile(r"\s+")

TERMINAL_CHARS = "。！？；"
QUOTE_PAIRS = (
    ("“", "”"),
    ("‘", "’"),
    ("「", "」"),
    ("『", "』"),
    ("《", "》"),
    ("〈", "〉"),
    ("【", "】"),
)


def normalize_whitespace(text: str) -> str:
    """合并内部空白并去掉首尾空白（全角空格也视为空白）。"""
    text = text.replace("\u3000", " ").replace("\xa0", " ")
    return WHITESPACE_RE.sub(" ", text).strip()


def hanzi_count(text: str) -> int:
    """去标点后的汉字数。"""
    return len(CJK_RE.findall(text))


def dedupe_key(text: str) -> str:
    """按正文文本去重：只保留汉字，忽略标点差异。"""
    return "".join(CJK_RE.findall(text))


def split_sentences(text: str) -> list[str]:
    """按 。！？； 切句，标点保留在句尾。"""
    parts = SENTENCE_SPLIT_RE.split(text)
    return [part.strip() for part in parts if part and part.strip()]


def reject_reason(text: str) -> str | None:
    """返回不可用原因；None 表示通过。"""
    if not text:
        return "空白"
    # 上游个别文件混入 ASCII 字母 / 数字 / 半角标点（清洗噪声），一律丢弃。
    if ASCII_RE.search(text):
        return "含 ASCII 字符"
    # 含 （…） 注音 / 夹注。
    if PAREN_RE.search(text):
        return "含括注"
    # 引号、书名号等必须成对，避免切出半截引文。
    for opener, closer in QUOTE_PAIRS:
        if text.count(opener) != text.count(closer):
            return "引号/书名号不成对"
    # 首尾残缺：必须以汉字开头、以句末标点结尾。
    if not CJK_RE.match(text[0]):
        return "开头残缺"
    if text[-1] not in TERMINAL_CHARS:
        return "结尾残缺"
    # 连续句末标点（如「？。」）属上游噪声。
    if DUP_TERMINAL_RE.search(text):
        return "连续句末标点"
    n = hanzi_count(text)
    if n < MIN_HANZI:
        return "去标点后不足 7 字"
    if n > MAX_HANZI:
        return "去标点后超过 30 字"
    return None


# ---------------------------------------------------------------------------
# 下载与缓存
# ---------------------------------------------------------------------------


def source_url(source: dict) -> str:
    # 中文路径需要 URL 编码；quote 默认保留 "/"。
    return UPSTREAM_RAW_BASE + urllib.parse.quote(source["path"])


def cache_path(source: dict) -> Path:
    return CACHE_DIR / Path(source["path"]).name


def validate_payload(source: dict, payload: object, origin: str) -> list:
    """校验下载/缓存的 JSON 是否完整可用，防止静默产出空数据。"""
    if not isinstance(payload, list):
        raise ValueError(f"{origin}：根节点应为数组，实际为 {type(payload).__name__}")
    if len(payload) < int(source["min_records"]):
        raise ValueError(
            f"{origin}：记录数 {len(payload)} 少于预期下限 "
            f"{source['min_records']}，文件可能不完整或路径已变更"
        )
    first = payload[0]
    if not isinstance(first, dict):
        raise ValueError(f"{origin}：记录格式异常（首条不是对象）")
    for field in source["text_fields"]:
        if field not in first:
            raise ValueError(f"{origin}：缺少字段 {field!r}，上游结构可能已变更")
    if source["title_field"] and source["title_field"] not in first:
        raise ValueError(
            f"{origin}：缺少字段 {source['title_field']!r}，上游结构可能已变更"
        )
    if source["author_field"] and source["author_field"] not in first:
        raise ValueError(
            f"{origin}：缺少字段 {source['author_field']!r}，上游结构可能已变更"
        )
    return payload


def parse_payload_bytes(source: dict, data: bytes, origin: str) -> list:
    if not data:
        raise ValueError(f"{origin}：内容为空")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError(f"{origin}：不是合法的 UTF-8（{exc}）") from exc
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{origin}：不是合法 JSON（{exc}）") from exc
    return validate_payload(source, payload, origin)


def download_bytes(url: str) -> bytes:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "zhixu-study-platform/1.0 (+daily-quotes builder)",
            "Accept": "application/json,text/plain,*/*",
        },
    )
    last_error: Exception | None = None
    for attempt in range(1, 4):
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                if getattr(response, "status", 200) != 200:
                    raise urllib.error.HTTPError(
                        url, response.status, "unexpected status", None, None
                    )
                return response.read()
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as exc:
            last_error = exc
            if attempt < 3:
                print(f"  下载失败（第 {attempt} 次）：{exc}，2 秒后重试…", file=sys.stderr)
                time.sleep(2 * attempt)
    raise SystemExit(
        "[错误] 下载失败：{url}\n"
        "  原因：{exc}\n"
        "  请检查网络或代理设置。也可以手动把上游文件下载到缓存目录后重试：\n"
        "  {cache}（文件名须与上游一致）".format(
            url=url, exc=last_error, cache=CACHE_DIR
        )
    )


def load_source(source: dict, force: bool) -> list:
    """读缓存；缓存缺失/损坏或 --force 时重新下载。"""
    path = cache_path(source)
    url = source_url(source)
    if path.exists() and not force:
        try:
            payload = parse_payload_bytes(
                source, path.read_bytes(), f"缓存文件 {path}"
            )
            print(f"  使用缓存 {path.name}（{len(payload)} {source['record_unit']}）")
            return payload
        except ValueError as exc:
            print(f"  缓存不可用（{exc}），重新下载…", file=sys.stderr)
    else:
        print(f"  下载 {url}")
    data = download_bytes(url)
    payload = parse_payload_bytes(source, data, url)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)
    print(f"  已缓存 {path}（{len(data)} 字节，{len(payload)} {source['record_unit']}）")
    return payload


# ---------------------------------------------------------------------------
# 抽取与挑选
# ---------------------------------------------------------------------------


def iter_source_texts(source: dict, record: dict):
    """按 unit 规则产出候选文本。"""
    for field in source["text_fields"]:
        raw = record.get(field)
        if not raw:
            continue
        chunks = [raw] if isinstance(raw, str) else raw
        for chunk in chunks:
            if not isinstance(chunk, str):
                continue
            text = normalize_whitespace(chunk)
            if not text:
                continue
            if source["unit"] == "segments":
                yield text
            else:
                yield from split_sentences(text)


def record_author(source: dict, record: dict) -> str:
    if source["fixed_author"]:
        return str(source["fixed_author"])
    return str(record.get(source["author_field"]) or "").strip()


def record_title(source: dict, record: dict) -> str:
    if not source["title_field"]:
        return str(source["label"])
    return str(record.get(source["title_field"]) or "").strip() or str(source["label"])


def collect_candidates(force: bool):
    """逐来源抽取候选，并跨来源按正文去重。"""
    pools: dict[str, list[dict]] = {}
    capacities: dict[str, int] = {}
    reject_stats: dict[str, Counter] = {}
    source_records: dict[str, int] = {}
    seen_texts: set[str] = set()

    printed = False
    for source in SOURCES:
        if not printed:
            print("[1/3] 读取上游数据（缓存目录：%s）" % CACHE_DIR)
            printed = True
        key = source["key"]
        records = load_source(source, force)
        source_records[key] = len(records)
        candidates: list[dict] = []
        rejects: Counter = Counter()
        for record in records:
            if not isinstance(record, dict):
                rejects["记录格式异常"] += 1
                continue
            author = record_author(source, record)
            title = record_title(source, record)
            for text in iter_source_texts(source, record):
                reason = reject_reason(text)
                if reason is not None:
                    rejects[reason] += 1
                    continue
                dedupe = dedupe_key(text)
                if not dedupe:
                    rejects["无汉字"] += 1
                    continue
                if dedupe in seen_texts:
                    rejects["正文重复"] += 1
                    continue
                seen_texts.add(dedupe)
                if not author:
                    rejects["缺作者"] += 1
                    continue
                candidates.append(
                    {"q": text, "a": author, "t": title, "s": str(source["label"])}
                )
        pools[key] = candidates
        capacities[key] = len(candidates)
        reject_stats[key] = rejects

        detail = "、".join(
            f"{reason} {count} 条" for reason, count in rejects.most_common(4)
        )
        print(
            f"  - {source['label']:<6} 原始 {len(records):>4} {source['record_unit']}"
            f"，可用候选 {len(candidates):>5} 条"
            + (f"（丢弃：{detail}）" if detail else "")
        )
    return pools, capacities, reject_stats, source_records


def allocate_quotas(capacities: dict[str, int], total: int = TOTAL_QUOTES) -> dict[str, int]:
    """按各来源容量分配条数：先均分，容量不足的按比例补足，保证总和为 total。"""
    keys = [source["key"] for source in SOURCES]
    base = total // len(keys)
    quota = {key: min(base, capacities[key]) for key in keys}
    deficit = total - sum(quota.values())
    while deficit > 0:
        available = [key for key in keys if quota[key] < capacities[key]]
        if not available:
            break
        weight = sum(capacities[key] - quota[key] for key in available)
        progressed = False
        for key in available:
            share = deficit * (capacities[key] - quota[key]) / weight
            add = min(max(1, int(share)), capacities[key] - quota[key], deficit)
            if add <= 0:
                continue
            quota[key] += add
            deficit -= add
            progressed = True
            if deficit == 0:
                break
        if not progressed:
            break
    return quota


def pick_quotes(pools, capacities):
    """固定随机种子打乱候选后按配额挑选；排除人工标记的错字条目。"""
    rng = random.Random(RANDOM_SEED)
    for source in SOURCES:
        rng.shuffle(pools[source["key"]])

    quota = allocate_quotas(capacities)
    selected: list[dict] = []
    for source in SOURCES:
        key = source["key"]
        need = quota[key]
        picked: list[dict] = []
        for candidate in pools[key]:
            if len(picked) >= need:
                break
            if candidate["q"] in MANUAL_EXCLUSIONS:
                continue
            picked.append(candidate)
        if len(picked) < need:
            raise SystemExit(
                f"[错误] {source['label']} 可用条目不足：需要 {need} 条，实际只有 "
                f"{len(picked)} 条（来源要求每源至少 {MIN_QUOTES_PER_SOURCE} 条）。"
            )
        selected.extend(picked)

    if len(selected) != TOTAL_QUOTES:
        raise SystemExit(
            f"[错误] 条目数不足：需要 {TOTAL_QUOTES} 条，实际 {len(selected)} 条。"
        )
    rng.shuffle(selected)
    return selected, quota


def build_records(selected: list[dict]) -> list[dict]:
    return [
        {"id": index, "q": item["q"], "a": item["a"], "t": item["t"], "s": item["s"]}
        for index, item in enumerate(selected, start=1)
    ]


# ---------------------------------------------------------------------------
# 输出
# ---------------------------------------------------------------------------


def write_json(records: list[dict]) -> tuple[bytes, str, str]:
    text = json.dumps(records, ensure_ascii=False, separators=(",", ":"))
    data = text.encode("utf-8")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / JSON_NAME).write_bytes(data)
    sha256 = hashlib.sha256(data).hexdigest()
    return data, sha256, json.dumps(records[0], ensure_ascii=False)


def write_license() -> None:
    (OUT_DIR / LICENSE_NAME).write_text(UPSTREAM_LICENSE, encoding="utf-8", newline="\n")


def write_readme(
    records: list[dict],
    size: int,
    sha256: str,
    source_records: dict[str, int],
    capacities: dict[str, int],
    quota: dict[str, int],
    reject_stats: dict[str, Counter],
) -> None:
    lines: list[str] = []
    lines.append("# 「每日一句」离线数据集")
    lines.append("")
    lines.append(
        "前端「每日一句」弹窗使用的离线数据集，由 "
        "[`scripts/build_daily_quotes.py`](../../../scripts/build_daily_quotes.py) "
        "自动生成，请勿手工编辑。"
    )
    lines.append("")
    lines.append("## 生成信息")
    lines.append("")
    lines.append(f"- 生成日期：{GENERATED_DATE}")
    lines.append(f"- 随机种子：`{RANDOM_SEED}`（`random.Random`，结果可复现）")
    lines.append(f"- 条目数：{len(records)}")
    lines.append(f"- 文件：`{JSON_NAME}`（根节点为 JSON 数组，紧凑格式、无缩进、UTF-8 无 BOM）")
    lines.append(f"- 文件大小：{size} 字节")
    lines.append(f"- SHA-256：`{sha256}`")
    lines.append("")
    lines.append("> 重新运行生成脚本会自动更新本节的大小与 SHA-256。")
    lines.append("")
    lines.append("## 数据来源")
    lines.append("")
    lines.append(
        f"[chinese-poetry/chinese-poetry]({UPSTREAM_REPO})"
        "（MIT License，Copyright (c) 2016 JackeyGao），"
        "`master` 分支以下 5 个简体原文文件："
    )
    lines.append("")
    lines.append("| 仓库内路径 | 上游记录数 | 本数据集条数 | 可用候选数 |")
    lines.append("| --- | ---: | ---: | ---: |")
    for source in SOURCES:
        key = source["key"]
        lines.append(
            f"| `{source['path']}` | {source_records[key]} | "
            f"{quota[key]} | {capacities[key]} |"
        )
    lines.append("")
    lines.append(
        "文件通过 `raw.githubusercontent.com` 下载（中文路径经 `urllib.parse.quote` 编码），"
        "并缓存在 `%TEMP%\\zhixu-quotes-cache\\`；缓存可用于重复运行与离线重建。"
    )
    lines.append("")
    lines.append("## 抽取规则")
    lines.append("")
    lines.append("1. 只取原文：`宋词三百首` / `论语` / `水墨唐诗` 取 `paragraphs`，`诗经` / `幽梦影` 取 `content`。")
    lines.append(
        "2. 切分单位：诗（`诗经`、`水墨唐诗`）以「一联 / 一段」为一个候选；"
        "词与格言（`宋词三百首`、`论语`、`幽梦影`）按 `。！？；` 切句，标点保留在句尾。"
    )
    lines.append("3. 去首尾空白、合并内部空白后，统计**去标点后的汉字数**，要求 `7 ≤ n ≤ 30`。")
    lines.append(
        "4. 丢弃以下条目：含 ASCII 字母 / 数字 / 半角标点（上游噪声）、"
        "含 `（…）` 注音、引号或书名号不成对、首尾残缺、连续句末标点、"
        "去标点后不足 7 字或超过 30 字。"
    )
    lines.append("5. 跨来源按正文文本去重（比较去除标点后的汉字串）。")
    lines.append(
        f"6. 使用固定随机种子 `{RANDOM_SEED}` 打乱，各来源先均分再按容量比例补足，"
        f"共 {TOTAL_QUOTES} 条（当前每源 {quota[SOURCES[0]['key']]} 条），"
        "最后整体打乱并按 1..365 连续编号。"
    )
    lines.append("7. 人工抽查中发现的明显错字条目直接丢弃，**不改写原文**（脚本内 `MANUAL_EXCLUSIONS`）。")
    lines.append("")
    reasons = Counter()
    for stats in reject_stats.values():
        reasons.update(stats)
    top_reasons = "、".join(f"{reason} {count}" for reason, count in reasons.most_common())
    lines.append(f"全部来源的丢弃统计：{top_reasons}。")
    lines.append("")
    lines.append("## 未使用的现代内容")
    lines.append("")
    lines.append(
        "- **未使用任何现代译文、注释、赏析或他人评语。** "
        "`水墨唐诗/shuimotangshi.json` 的 `prologue`（现代译文）、"
        "`幽梦影/youmengying.json` 的 `comment`（他人评语）、`tags` 等字段一律不读取，"
        "只保留古文原文。"
    )
    lines.append("- 未做繁简改写、未改写原文用字；保留上游原文标点。")
    lines.append("- 上游文本本身可能有错字，发现后按第 7 条丢弃该条，不做修正。")
    lines.append("")
    lines.append("## 字段说明")
    lines.append("")
    lines.append("| 字段 | 类型 | 含义 |")
    lines.append("| --- | --- | --- |")
    lines.append("| `id` | int | 序号，1..365 连续且唯一 |")
    lines.append("| `q` | str | 原文（保留原标点） |")
    lines.append("| `a` | str | 作者 / 出处人物（诗经为「佚名」，论语为「孔子」，幽梦影为「张潮」） |")
    lines.append("| `t` | str | 篇名 / 词牌 / 章节 |")
    lines.append("| `s` | str | 来源：宋词三百首 / 诗经 / 论语 / 幽梦影 / 水墨唐诗 |")
    lines.append("")
    lines.append("示例（首条）：")
    lines.append("")
    sample = json.dumps(records[0], ensure_ascii=False)
    lines.append("```json")
    lines.append(sample)
    lines.append("```")
    lines.append("")
    lines.append("## 重新生成")
    lines.append("")
    lines.append("```")
    lines.append("python scripts/build_daily_quotes.py          # 使用缓存（缺失时自动下载）")
    lines.append("python scripts/build_daily_quotes.py --force  # 强制重新下载上游文件")
    lines.append("```")
    lines.append("")
    (OUT_DIR / README_NAME).write_text("\n".join(lines), encoding="utf-8", newline="\n")


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="生成「每日一句」离线数据集（5 个上游原文文件 → 365 条）。"
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="忽略 %TEMP%\\zhixu-quotes-cache\\ 缓存，强制重新下载上游文件",
    )
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    print("构建「每日一句」数据集（上游：chinese-poetry/chinese-poetry，MIT）")
    print("缓存目录：%s" % CACHE_DIR)

    pools, capacities, reject_stats, source_records = collect_candidates(args.force)

    total_capacity = sum(capacities.values())
    for source in SOURCES:
        key = source["key"]
        if capacities[key] < MIN_QUOTES_PER_SOURCE:
            raise SystemExit(
                f"[错误] {source['label']} 可用候选仅 {capacities[key]} 条，"
                f"少于每源下限 {MIN_QUOTES_PER_SOURCE} 条，无法满足均衡要求。"
            )
    if total_capacity < TOTAL_QUOTES:
        raise SystemExit(
            f"[错误] 可用候选合计 {total_capacity} 条，不足 {TOTAL_QUOTES} 条。"
        )

    print("[2/3] 按比例分配并随机抽取（种子 %d）" % RANDOM_SEED)
    selected, quota = pick_quotes(pools, capacities)
    records = build_records(selected)

    print("[3/3] 写出数据集")
    data, sha256, _ = write_json(records)
    write_license()
    write_readme(records, len(data), sha256, source_records, capacities, quota, reject_stats)

    distribution = Counter(record["s"] for record in records)
    print("")
    print("各来源条数：")
    for source in SOURCES:
        label = source["label"]
        print(f"  {label}: {distribution[label]} 条")
    print(f"最终条数：{len(records)}")
    print("输出文件：")
    for name in (JSON_NAME, LICENSE_NAME, README_NAME):
        path = OUT_DIR / name
        print(f"  {path}（{path.stat().st_size} 字节）")
    print(f"daily-quotes.json 大小：{len(data)} 字节")
    print(f"daily-quotes.json SHA-256：{sha256}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
