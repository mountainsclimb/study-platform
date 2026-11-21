#!/usr/bin/env python
"""把官方渠道补采的批次线 CSV 导入为只读的 ``data/admission-official.sqlite3``。

数据来自 ``scripts/collect_official_data.py`` 的采集结果（公开页面，带来源 URL 与
抓取时间）。本库与主录取库分离，缺失时应用自动忽略，不影响主流程。
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA_DIR = Path(os.environ.get("STUDY_DATA_DIR", str(ROOT / "data"))).resolve()
SCHEMA_VERSION = "1"

SCHEMA_SQL = [
    """CREATE TABLE control_line (
        province_slug TEXT NOT NULL,
        province_name TEXT,
        year INTEGER NOT NULL,
        category TEXT NOT NULL DEFAULT '',
        batch TEXT NOT NULL,
        score INTEGER NOT NULL,
        source_url TEXT,
        retrieved_at TEXT,
        sha256 TEXT,
        PRIMARY KEY (province_slug, year, category, batch)
    )""",
    "CREATE INDEX idx_control_line_year ON control_line(province_slug, year)",
    """CREATE TABLE official_meta (
        key TEXT PRIMARY KEY,
        value TEXT
    )""",
]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="导入官方补采的批次线 CSV")
    parser.add_argument("--data-dir", default=str(DEFAULT_DATA_DIR))
    parser.add_argument("--csv", default="", help="批次线 CSV（默认 <data-dir>/official-sources/batch-lines.csv）")
    parser.add_argument("--db", default="", help="输出库（默认 <data-dir>/admission-official.sqlite3）")
    args = parser.parse_args(argv)

    data_dir = Path(args.data_dir).resolve()
    csv_path = Path(args.csv).resolve() if args.csv else data_dir / "official-sources" / "batch-lines.csv"
    db_path = Path(args.db).resolve() if args.db else data_dir / "admission-official.sqlite3"
    if not csv_path.is_file():
        print(f"未找到批次线 CSV：{csv_path}\n请先运行 `python scripts/collect_official_data.py batch-lines`。",
              file=sys.stderr)
        return 1

    rows: list[tuple] = []
    with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            score = str(row.get("score") or "").strip()
            if not score.isdigit():
                continue
            rows.append((
                row.get("province_slug") or "",
                row.get("province_name") or "",
                int(row.get("year") or 0),
                row.get("category") or "",
                row.get("batch") or "",
                int(score),
                row.get("source_url") or "",
                row.get("retrieved_at") or "",
                row.get("sha256") or "",
            ))
    rows = [row for row in rows if row[0] and row[3] is not None and row[4]]
    if not rows:
        print(f"CSV 中没有可用记录：{csv_path}", file=sys.stderr)
        return 1

    temp_path = db_path.with_suffix(".sqlite3.tmp")
    if temp_path.exists():
        temp_path.unlink()
    connection = sqlite3.connect(str(temp_path))
    try:
        connection.execute("PRAGMA journal_mode=OFF")
        connection.execute("PRAGMA synchronous=OFF")
        for statement in SCHEMA_SQL:
            connection.execute(statement)
        connection.executemany(
            "INSERT OR REPLACE INTO control_line (province_slug, province_name, year, category, batch,"
            " score, source_url, retrieved_at, sha256) VALUES (?,?,?,?,?,?,?,?,?)",
            rows,
        )
        years = sorted({row[2] for row in rows})
        provinces = sorted({row[0] for row in rows})
        meta = {
            "schema_version": SCHEMA_VERSION,
            "source": "教育在线（eol.cn）历年批次线页面，整理自各省考试院公告",
            "source_url": "https://www.eol.cn/e_html/gk/fsx/",
            "imported_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "rows": str(len(rows)),
            "years": json.dumps(years),
            "provinces": json.dumps(provinces),
            "license_note": "公开页面数据，仅用于本机查询；页面标注来源为非官方整理，以官方公布为准",
        }
        connection.executemany(
            "INSERT OR REPLACE INTO official_meta (key, value) VALUES (?,?)", list(meta.items()))
        connection.commit()
        connection.execute("ANALYZE")
        connection.commit()
    finally:
        connection.close()

    if db_path.exists():
        db_path.unlink()
    os.replace(temp_path, db_path)
    print(f"已导入 {len(rows):,} 条批次线 → {db_path}")
    print(f"覆盖年份：{years}，省份：{len(provinces)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
