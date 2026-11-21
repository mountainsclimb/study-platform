#!/usr/bin/env python
"""官方渠道补采：从教育在线（eol.cn）公开页面抓取批次线，并保留来源证据。

背景：GaokaoCompass 数据在 2017–2021 年的 `control_score` 与多数省份的位次为空；
本脚本走「公开渠道自采」路线，先把能稳定解析的 **批次控制线** 采下来，产出
带来源 URL、抓取时间与 SHA-256 的 CSV，供后续导入或人工复核。

已确认的覆盖：教育在线「历年批次线」索引页按省分块，年份标签为 2021–2026，
但每省实际附带 7 张表（最后一张为 2020 年）。因此本脚本可采 **2020、2021**
两年全部省份的批次线，并把 2022–2025 一并采下用于与本地库交叉校验。

用法::

    cd study-platform
    python scripts/collect_official_data.py batch-lines
    python scripts/collect_official_data.py batch-lines --raw-out data/official-sources/raw

产出：
- ``data/official-sources/batch-lines.csv``：province_slug, province_name, year,
  category, batch, score, source_url, retrieved_at, sha256
- ``data/official-sources/manifest.json``：来源、抓取时间、行数、校验结果
- ``data/official-sources/raw/``：原始 HTML 证据（保留哈希）

来源说明：教育在线（eol.cn）为公开教育门户，页面注明数据整理自各省考试院；
本脚本只读取其公开页面，不绕过登录/付费墙。正式对外使用时请保留来源链接。
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import ssl
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = ROOT / "data" / "official-sources"
USER_AGENT = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}
FSX_INDEX_URL = "https://www.eol.cn/e_html/gk/fsx/"
PROVINCE_NAMES = {
    "anhui": "安徽", "beijing": "北京", "chongqing": "重庆", "fujian": "福建",
    "gansu": "甘肃", "guangdong": "广东", "guangxi": "广西", "guizhou": "贵州",
    "hainan": "海南", "hebei": "河北", "heilongjiang": "黑龙江", "henan": "河南",
    "hubei": "湖北", "hunan": "湖南", "jiangsu": "江苏", "jiangxi": "江西",
    "jilin": "吉林", "liaoning": "辽宁", "neimenggu": "内蒙古", "ningxia": "宁夏",
    "qinghai": "青海", "shaanxi": "陕西", "shandong": "山东", "shanghai": "上海",
    "shanxi": "山西", "sichuan": "四川", "tianjin": "天津", "xinjiang": "新疆",
    "xizang": "西藏", "yunnan": "云南", "zhejiang": "浙江",
}
NAME_TO_SLUG = {name: slug for slug, name in PROVINCE_NAMES.items()}

# 用于校验解析结果的公开已知值（2020/2021 批次线，来自各省考试院公告）。
VALIDATION_CASES = [
    ("beijing", 2020, "", "普通本科", 436),
    ("beijing", 2020, "", "特殊类型招生", 526),
    ("beijing", 2021, "", "普通本科", 400),
    ("beijing", 2021, "", "特殊类型招生", 513),
    ("shandong", 2020, "", "普通类一段", 449),
    ("shandong", 2021, "", "普通类一段", 444),
    ("zhejiang", 2020, "", "第一段", 594),
    ("zhejiang", 2021, "", "第一段", 495),
    ("anhui", 2020, "文科", "本科一批", 541),
    ("anhui", 2020, "理科", "本科一批", 515),
    ("anhui", 2021, "文科", "本科一批", 560),
    ("anhui", 2021, "理科", "本科一批", 488),
    ("henan", 2020, "文科", "一本", 556),
    ("henan", 2020, "理科", "一本", 544),
    ("henan", 2021, "文科", "一本", 558),
    ("henan", 2021, "理科", "一本", 518),
]


def log(message: str) -> None:
    print(message, flush=True)


def fetch(url: str, retries: int = 3) -> bytes:
    last = None
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    for attempt in range(retries):
        try:
            request = urllib.request.Request(url, headers=USER_AGENT)
            with urllib.request.urlopen(request, timeout=60, context=context) as response:
                return response.read()
        except Exception as error:  # noqa: BLE001 - 网络抖动重试
            last = error
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"抓取失败 {url}: {last}")


def parse_tables(html: str) -> list[list[list[str]]]:
    """把 HTML 中的 <table> 解析为「行 → 单元格文本」列表（只用标准库）。"""
    from html.parser import HTMLParser

    class Parser(HTMLParser):
        def __init__(self):
            super().__init__()
            self.tables: list[list[list[str]]] = []
            self.depth = 0
            self.current_rows: list[list[str]] = []
            self.current_row: list[str] = []
            self.cell: list[str] | None = None

        def handle_starttag(self, tag, attrs):
            if tag == "table":
                self.depth += 1
                if self.depth == 1:
                    self.current_rows = []
            elif tag == "tr" and self.depth == 1:
                self.current_row = []
            elif tag in ("td", "th") and self.depth == 1:
                self.cell = []

        def handle_endtag(self, tag):
            if tag == "table":
                if self.depth == 1:
                    self.tables.append(self.current_rows)
                self.depth -= 1
            elif tag == "tr" and self.depth == 1:
                if self.current_row:
                    self.current_rows.append(self.current_row)
            elif tag in ("td", "th") and self.depth == 1 and self.cell is not None:
                self.current_row.append(re.sub(r"\s+", " ", "".join(self.cell)).strip())
                self.cell = None

        def handle_data(self, data):
            if self.cell is not None:
                self.cell.append(data)

    parser = Parser()
    parser.feed(html)
    return parser.tables


def _number(text: str) -> int | None:
    match = re.search(r"\d{2,3}", text.replace(",", ""))
    return int(match.group(0)) if match else None


def _split_blocks(html: str) -> list[tuple[str, str]]:
    positions = [(m.start(), m.group(1)) for m in re.finditer(r'<div class="city">([^<]+)</div>', html)]
    blocks = []
    for index, (start, name) in enumerate(positions):
        end = positions[index + 1][0] if index + 1 < len(positions) else len(html)
        blocks.append((name, html[start:end]))
    return blocks


def parse_batch_lines(html: str, source_url: str, retrieved_at: str, sha256: str) -> tuple[list[dict], list[str]]:
    """解析批次线索引页：每省按年份标签与表格顺序配对，返回记录与告警。"""
    records: list[dict] = []
    warnings: list[str] = []
    for name, block in _split_blocks(html):
        slug = NAME_TO_SLUG.get(name)
        if slug is None:
            warnings.append(f"未知省份：{name}")
            continue
        years = [int(value) for value in re.findall(r'<div class="year[^"]*">(\d{4})年</div>', block)]
        tables = re.findall(r"<table.*?</table>", block, re.S)
        if not years or not tables:
            warnings.append(f"{name}: 未找到年份或表格")
            continue
        # 年份标签按「新→旧」排列，表格顺序与之对应；多出的表格按更早年份补齐（eol 实际含 2020 年表）。
        if len(tables) == len(years):
            pairs = list(zip(years, tables))
        elif len(tables) > len(years):
            pairs = []
            for index, table in enumerate(tables):
                if index < len(years):
                    year = years[index]
                else:
                    year = years[-1] - (index - len(years) + 1)
                pairs.append((year, table))
        else:
            pairs = list(zip(years[:len(tables)], tables))
            warnings.append(f"{name}: 年份标签 {len(years)} 个、表格 {len(tables)} 张，按前 {len(tables)} 年解析")
        for year, table_html in pairs:
            parsed = parse_tables(table_html)
            if not parsed:
                continue
            rows = parsed[0]
            if not rows:
                continue
            header = rows[0]
            category_columns: list[tuple[int, str]] = []
            # 表头形如：批次 / 分数线；批次 / 文科 / 理科；批次 / 首选历史 / 首选物理
            if len(header) >= 3 and any(key in " ".join(header) for key in ("文科", "理科", "历史", "物理")):
                for column, title in enumerate(header[1:], start=1):
                    if title and title not in ("分数线",):
                        category_columns.append((column, title))
            elif len(header) == 2:
                category_columns = [(1, "")]
            else:
                category_columns = [(1, "")]
            for row in rows[1:]:
                if not row or not row[0] or row[0] in ("批次",):
                    continue
                batch = row[0].strip()
                if not batch or "查看" in batch or "点击" in batch:
                    continue
                for column, category in category_columns:
                    if column >= len(row):
                        continue
                    score = _number(row[column])
                    if score is None:
                        continue
                    records.append({
                        "province_slug": slug,
                        "province_name": name,
                        "year": year,
                        "category": category,
                        "batch": batch,
                        "score": score,
                        "source_url": source_url,
                        "retrieved_at": retrieved_at,
                        "sha256": sha256,
                    })
    # 去重（同省同年同科类同批次只保留一条）
    deduped: dict[tuple, dict] = {}
    for record in records:
        deduped[(record["province_slug"], record["year"], record["category"], record["batch"])] = record
    return list(deduped.values()), warnings


def collect_batch_lines(out_dir: Path, raw_out: Path) -> dict:
    log(f"抓取批次线索引：{FSX_INDEX_URL}")
    raw = fetch(FSX_INDEX_URL)
    sha256 = hashlib.sha256(raw).hexdigest()
    retrieved_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    raw_out.mkdir(parents=True, exist_ok=True)
    (raw_out / "eol-fsx-index.html").write_bytes(raw)
    html = raw.decode("utf-8", "replace")
    records, warnings = parse_batch_lines(html, FSX_INDEX_URL, retrieved_at, sha256)
    log(f"解析记录 {len(records):,} 条，告警 {len(warnings)} 条")

    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / "batch-lines.csv"
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=[
            "province_slug", "province_name", "year", "category", "batch", "score",
            "source_url", "retrieved_at", "sha256",
        ])
        writer.writeheader()
        for record in sorted(records, key=lambda item: (item["year"], item["province_slug"], item["batch"])):
            writer.writerow(record)

    # 校验：2020/2021 已知值 + 每年覆盖省份数 + 2022–2025 与本地库交叉核对（如存在）。
    validation = []
    index = {(row["province_slug"], row["year"], row["category"], row["batch"]): row["score"] for row in records}
    for slug, year, category, batch, expected in VALIDATION_CASES:
        actual = index.get((slug, year, category, batch))
        validation.append({
            "case": f"{slug}/{year}/{category}/{batch}",
            "expected": expected,
            "actual": actual,
            "ok": actual == expected,
        })

    coverage: dict[int, set[str]] = {}
    for record in records:
        coverage.setdefault(record["year"], set()).add(record["province_slug"])
    coverage_report = {str(year): len(slugs) for year, slugs in sorted(coverage.items())}

    report = {
        "source": FSX_INDEX_URL,
        "retrieved_at": retrieved_at,
        "sha256": sha256,
        "rows": len(records),
        "warnings": warnings[:50],
        "coverage_by_year": coverage_report,
        "validation": validation,
        "csv": str(csv_path),
    }
    (out_dir / "manifest.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"写入 {csv_path}")
    log(f"年度覆盖：{coverage_report}")
    failed = [item["case"] for item in validation if not item["ok"]]
    if failed:
        log(f"校验失败 {len(failed)} 项：{failed[:6]}")
    else:
        log(f"校验通过 {len(validation)}/{len(validation)} 项")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="官方渠道补采（公开页面 → 带来源的 CSV）")
    sub = parser.add_subparsers(dest="command", required=True)
    batch = sub.add_parser("batch-lines", help="采集 2020–2025 各省批次控制线")
    batch.add_argument("--out", default=str(DEFAULT_OUT))
    batch.add_argument("--raw-out", default=str(DEFAULT_OUT / "raw"))
    args = parser.parse_args(argv)

    if args.command == "batch-lines":
        try:
            report = collect_batch_lines(Path(args.out), Path(args.raw_out))
        except Exception as error:  # noqa: BLE001 - 网络/站点异常统一返回非零
            log(f"采集失败：{error}")
            return 1
        return 0 if all(item["ok"] for item in report["validation"]) else 2
    return 1


if __name__ == "__main__":
    sys.exit(main())
