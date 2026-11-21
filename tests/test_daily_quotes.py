# -*- coding: utf-8 -*-
"""「每日一句」离线数据集（web/vendor/quotes/daily-quotes.json）的结构与质量断言。

只做静态校验：不访问网络、不启动应用、不写任何文件。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DATA_FILE = ROOT / "web" / "vendor" / "quotes" / "daily-quotes.json"

TOTAL = 365
MAX_BYTES = 300 * 1024
MIN_HANZI = 7
MAX_HANZI = 30
ALLOWED_SOURCES = {"宋词三百首", "诗经", "论语", "幽梦影", "水墨唐诗"}
MIN_PER_SOURCE = 40

CJK_RE = re.compile(r"[\u4e00-\u9fff]")
BOM = b"\xef\xbb\xbf"


def hanzi(text: str) -> str:
    """去掉标点/空白，只保留汉字。"""
    return "".join(CJK_RE.findall(text))


@pytest.fixture(scope="module")
def raw_bytes() -> bytes:
    assert DATA_FILE.is_file(), f"数据集文件不存在：{DATA_FILE}"
    return DATA_FILE.read_bytes()


@pytest.fixture(scope="module")
def quotes(raw_bytes: bytes) -> list:
    return json.loads(raw_bytes.decode("utf-8"))


def test_file_size_under_limit(raw_bytes: bytes) -> None:
    assert len(raw_bytes) < MAX_BYTES, f"文件大小 {len(raw_bytes)} 字节，超过 300KB"


def test_utf8_without_bom(raw_bytes: bytes) -> None:
    assert not raw_bytes.startswith(BOM), "文件带 UTF-8 BOM"
    raw_bytes.decode("utf-8")  # 非法 UTF-8 会直接抛错


def test_json_parses_to_array(quotes: list) -> None:
    assert isinstance(quotes, list), "根节点必须是 JSON 数组"
    assert len(quotes) == TOTAL, f"期望 {TOTAL} 条，实际 {len(quotes)} 条"


def test_ids_unique_and_continuous(quotes: list) -> None:
    ids = [item["id"] for item in quotes]
    assert all(isinstance(value, int) and not isinstance(value, bool) for value in ids)
    assert len(set(ids)) == TOTAL, "id 存在重复"
    assert sorted(ids) == list(range(1, TOTAL + 1)), "id 必须是 1..365 的连续整数"


def test_required_fields_non_empty(quotes: list) -> None:
    for index, item in enumerate(quotes):
        assert isinstance(item, dict), f"第 {index} 条不是对象"
        for field in ("q", "a", "t", "s"):
            assert field in item, f"第 {index} 条缺少字段 {field!r}"
            value = item[field]
            assert isinstance(value, str), f"第 {index} 条字段 {field!r} 不是字符串"
            assert value.strip(), f"第 {index} 条字段 {field!r} 为空"
        assert item["s"] in ALLOWED_SOURCES, f"第 {index} 条来源非法：{item['s']!r}"


def test_quote_length_after_stripping_punctuation(quotes: list) -> None:
    for item in quotes:
        length = len(hanzi(item["q"]))
        assert MIN_HANZI <= length <= MAX_HANZI, (
            f"id={item['id']} 去标点后 {length} 字，超出 {MIN_HANZI}-{MAX_HANZI}："
            f"{item['q']!r}"
        )


def test_no_duplicate_text(quotes: list) -> None:
    keys = [hanzi(item["q"]) for item in quotes]
    assert len(set(keys)) == len(keys), "存在正文重复的条目"


def test_source_distribution_balanced(quotes: list) -> None:
    counts = {source: 0 for source in ALLOWED_SOURCES}
    for item in quotes:
        counts[item["s"]] += 1
    for source, count in counts.items():
        assert count >= MIN_PER_SOURCE, f"来源 {source} 仅 {count} 条，少于 {MIN_PER_SOURCE} 条"
