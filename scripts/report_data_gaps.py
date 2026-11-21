#!/usr/bin/env python
"""生成《数据缺失声明》：把录取数据的缺口按省年、字段逐项量化并落盘。

产物：
- ``data/admission-gaps.json``：机器可读的缺口清单（接口 ``GET /api/admission/gaps`` 读取）
- ``数据缺失声明.md``：可直接阅读的声明（总览、分年度、逐省年、补齐说明与免责）

只读主录取库与官方补采库，不修改任何数据；可随时重算。

用法::

    cd study-platform
    python scripts/report_data_gaps.py
    python scripts/report_data_gaps.py --json-out data/admission-gaps.json --md-out 数据缺失声明.md
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA_DIR = Path(os.environ.get("STUDY_DATA_DIR", str(ROOT / "data"))).resolve()
SCHEMA_VERSION = "1"
TABLE_LABELS = {
    "score-range": "一分一段",
    "school-admission": "院校投档线",
    "major-admission": "专业录取线",
    "enrollment-plan": "招生计划",
}
STATUS_LABELS = {
    "ok": "完整",
    "partial": "部分缺失",
    "missing": "缺失",
    "official": "官方补采",
    "native": "主数据自带",
    "no_data": "无该表数据",
}
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


def _connect(path: Path):
    connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=10)
    connection.row_factory = sqlite3.Row
    return connection


def _ratio_status(total: int, filled: int, *, missing_label: str = "missing") -> str:
    if total <= 0:
        return "no_data"
    if filled <= 0:
        return missing_label
    if filled < total:
        return "partial"
    return "ok"


def compute_gaps(data_dir: Path) -> dict:
    """读取主库与官方补采库，计算逐省年缺口。"""
    main_path = data_dir / "admission.sqlite3"
    if not main_path.is_file():
        raise FileNotFoundError(f"未找到主录取库：{main_path}，请先运行 scripts/import_admission.py")
    official_path = data_dir / "admission-official.sqlite3"

    main = _connect(main_path)
    try:
        meta = {row["key"]: row["value"] for row in main.execute("SELECT key, value FROM admission_meta")}
        coverage = {}
        for row in main.execute("SELECT province_slug, year, table_name, rows FROM admission_coverage"):
            coverage.setdefault((row["province_slug"], row["year"]), {})[row["table_name"]] = row["rows"]
        years = _json_list(meta.get("years"))
        provinces = _json_list(meta.get("provinces"))
        rows = {table: int(meta.get(f"rows_{table}") or 0) for table in TABLE_LABELS}

        school_stats = {
            (row["province_slug"], row["year"]): (row["total"], row["with_rank"], row["with_category"])
            for row in main.execute(
                "SELECT province_slug, year, COUNT(*) AS total, COUNT(min_rank) AS with_rank,"
                " COUNT(category) AS with_category FROM school_admission GROUP BY province_slug, year"
            )
        }
        plan_stats = {
            (row["province_slug"], row["year"]): (row["total"], row["with_plan"])
            for row in main.execute(
                "SELECT province_slug, year, COUNT(*) AS total, COUNT(plan_count) AS with_plan"
                " FROM enrollment_plan GROUP BY province_slug, year"
            )
        }
        control_stats = {
            (row["province_slug"], row["year"]): (row["total"], row["with_control"])
            for row in main.execute(
                "SELECT province_slug, year, COUNT(*) AS total, COUNT(control_score) AS with_control"
                " FROM score_range GROUP BY province_slug, year"
            )
        }
    finally:
        main.close()

    official_years: set[int] = set()
    official_rows = 0
    official_pairs: set[tuple[str, int]] = set()
    if official_path.is_file():
        official = _connect(official_path)
        try:
            official_rows = official.execute("SELECT COUNT(*) FROM control_line").fetchone()[0]
            for row in official.execute("SELECT DISTINCT province_slug, year FROM control_line"):
                official_pairs.add((row["province_slug"], row["year"]))
            for row in official.execute("SELECT DISTINCT year FROM control_line"):
                official_years.add(row["year"])
        finally:
            official.close()

    province_years = []
    for (slug, year), tables in sorted(coverage.items(), key=lambda item: (item[0][1], item[0][0])):
        school_total, school_rank, school_category = school_stats.get((slug, year), (0, 0, 0))
        plan_total, plan_filled = plan_stats.get((slug, year), (0, 0))
        control_total, control_filled = control_stats.get((slug, year), (0, 0))
        if control_filled > 0:
            control_status = "native"
        elif (slug, year) in official_pairs:
            control_status = "official"
        else:
            control_status = _ratio_status(control_total, control_filled)
        entry = {
            "province": slug,
            "province_name": PROVINCE_NAMES.get(slug, slug),
            "year": year,
            "tables": sorted(tables),
            "missing_tables": [table for table in TABLE_LABELS if table not in tables],
            "rank": _ratio_status(school_total, school_rank),
            "control": control_status,
            "plan": _ratio_status(plan_total, plan_filled),
            "category": _ratio_status(school_total, school_category),
        }
        province_years.append(entry)

    summary = {
        "province_years": len(province_years),
        "rank": _count_statuses(province_years, "rank"),
        "control": _count_statuses(province_years, "control"),
        "plan": _count_statuses(province_years, "plan"),
        "category": _count_statuses(province_years, "category"),
        "province_years_missing_tables": sum(1 for item in province_years if item["missing_tables"]),
    }

    by_year = []
    for year in years:
        items = [item for item in province_years if item["year"] == year]
        if not items:
            continue
        by_year.append({
            "year": year,
            "province_years": len(items),
            "rank_missing": sum(1 for item in items if item["rank"] in ("missing", "no_data")),
            "control_missing": sum(1 for item in items if item["control"] in ("missing", "no_data")),
            "control_official": sum(1 for item in items if item["control"] == "official"),
            "plan_missing": sum(1 for item in items if item["plan"] in ("missing", "no_data")),
            "plan_partial": sum(1 for item in items if item["plan"] == "partial"),
            "category_missing": sum(1 for item in items if item["category"] in ("missing", "no_data")),
            "missing_tables": sum(1 for item in items if item["missing_tables"]),
        })

    return {
        "version": SCHEMA_VERSION,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "dataset": {
            "dataset": meta.get("dataset", ""),
            "imported_at": meta.get("imported_at", ""),
            "total_rows": int(meta.get("total_rows") or 0),
            "years": years,
            "provinces": len(provinces),
            "rows": rows,
        },
        "official": {
            "available": official_path.is_file(),
            "years": sorted(official_years),
            "rows": official_rows,
        },
        "summary": summary,
        "by_year": by_year,
        "province_years": province_years,
    }


def _json_list(value) -> list:
    try:
        parsed = json.loads(value or "[]")
    except ValueError:
        return []
    return parsed if isinstance(parsed, list) else []


def _count_statuses(items: list[dict], field: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        counts[item[field]] = counts.get(item[field], 0) + 1
    return counts


def render_markdown(report: dict) -> str:
    summary = report["summary"]
    dataset = report["dataset"]
    official = report["official"]
    if official["available"]:
        official_years = "、".join(str(year) for year in official["years"])
        official_text = f"可用，覆盖 {official_years} 年，共 {official['rows']:,} 条"
    else:
        official_text = "不可用（未运行官方补采导入）"
    lines = [
        "# 数据缺失声明",
        "",
        f"生成时间：{report['generated_at']}。数据来源：GaokaoCompass（MIT，Copyright (c) 2025 choucisan，"
        f"整理自各省考试院等公开渠道），以及教育在线公开页面的官方补采批次线。",
        "",
        "本文件由 `scripts/report_data_gaps.py` 根据实际数据库自动生成，逐省年声明缺失项；"
        "缺失处不做推断、不补零，页面与接口按当前省份年份如实提示。",
        "",
        "## 总览",
        "",
        f"- 数据年份：{dataset['years'][0]}–{dataset['years'][-1]}" if dataset["years"] else "- 数据年份：无",
        f"- 省份数：{dataset['provinces']}；省年覆盖：{summary['province_years']}",
        f"- 总行数：{dataset['total_rows']:,}（一分一段 {dataset['rows']['score-range']:,}、"
        f"院校投档线 {dataset['rows']['school-admission']:,}、专业录取线 {dataset['rows']['major-admission']:,}、"
        f"招生计划 {dataset['rows']['enrollment-plan']:,}）",
        f"- 官方补采批次线：{official_text}",
        "",
        "| 缺失项 | 省年数 | 说明 |",
        "|---|---:|---|",
        f"| 院校投档位次缺失 | {summary['rank'].get('missing', 0) + summary['rank'].get('no_data', 0)} | "
        "2017–2021 多数省份投档线没有位次，按位次匹配不可用 |",
        f"| 批次控制线缺失（未补齐） | {summary['control'].get('missing', 0) + summary['control'].get('no_data', 0)} | "
        "主数据 `score_range.control_score` 为空且无官方补采的省年 |",
        f"| 招生计划人数缺失 | {summary['plan'].get('missing', 0) + summary['plan'].get('no_data', 0)} | "
        f"整表没有计划数；另有 {summary['plan'].get('partial', 0)} 个省年部分缺失 |",
        f"| 科类整列缺失 | {summary['category'].get('missing', 0) + summary['category'].get('no_data', 0)} | "
        "录取表科类为空，筛选时自动忽略 |",
        f"| 四表不全的省年 | {summary['province_years_missing_tables']} | 数据源未提供该表 |",
        "",
        "## 分年度",
        "",
        "| 年份 | 省年 | 位次缺失 | 批次线缺失 | 批次线官方补采 | 计划数缺失 | 计划数部分缺失 | 科类缺失 | 缺表省年 |",
        "|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in report["by_year"]:
        lines.append(
            f"| {row['year']} | {row['province_years']} | {row['rank_missing']} | {row['control_missing']} | "
            f"{row['control_official']} | {row['plan_missing']} | {row['plan_partial']} | "
            f"{row['category_missing']} | {row['missing_tables']} |"
        )
    lines += [
        "",
        "## 逐省年明细",
        "",
        "| 省份 | 年份 | 位次 | 批次线 | 招生计划数 | 科类 | 缺失的表 |",
        "|---|---:|---|---|---|---|---|",
    ]
    for item in report["province_years"]:
        missing_tables = "、".join(TABLE_LABELS.get(table, table) for table in item["missing_tables"]) or "—"
        lines.append(
            f"| {item['province_name']} | {item['year']} | {STATUS_LABELS.get(item['rank'], item['rank'])} | "
            f"{STATUS_LABELS.get(item['control'], item['control'])} | "
            f"{STATUS_LABELS.get(item['plan'], item['plan'])} | "
            f"{STATUS_LABELS.get(item['category'], item['category'])} | {missing_tables} |"
        )
    lines += [
        "",
        "## 补齐范围与仍缺失的原因",
        "",
        "1. **已补齐**：2020–2026 年各省批次控制线（教育在线公开页面，保留来源 URL、抓取时间与页面哈希），由 "
        "`scripts/collect_official_data.py` 采集、`scripts/import_official_data.py` 导入独立库；"
        "主数据缺批次线的省年按批次或分数补齐，页面标注「官方补采」。",
        "2. **仍未补齐**：2017–2019 年批次线、2017–2020 年一分一段。公开页面多为图片/PDF，阳光高考存在 WAF（412），"
        "部分考试院栏目为动态列表；补齐需要逐省适配与 OCR/视觉识别。",
        "3. **不可推断**：院校投档位次不能用一分一段反推（实测与原生位次仅 18–29% 吻合），因此缺位次的年份"
        "只提供分数相关功能，不生成推测位次。",
        "",
        "## 使用与免责",
        "",
        "- 页面「志愿查询」顶部按当前省份年份显示「位次 / 批次线 / 科类 / 计划人数」完整度，缺失处直接标注。",
        "- 接口 `GET /api/admission/gaps` 返回本声明的机器可读版本。",
        "- 本数据非官方发布，可能存在误差；志愿填报与录取结果以各省教育考试院、招生院校正式公布为准，"
        "本项目及数据提供者不对据此作出的决定承担责任。",
        "- 完整版权与许可声明见 `THIRD_PARTY_NOTICES.md`；分发时请一并保留。",
        "",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成《数据缺失声明》（JSON + Markdown）")
    parser.add_argument("--data-dir", default=str(DEFAULT_DATA_DIR))
    parser.add_argument("--json-out", default="", help="默认 <data-dir>/admission-gaps.json")
    parser.add_argument("--md-out", default="", help="默认 <项目根>/数据缺失声明.md")
    args = parser.parse_args(argv)

    data_dir = Path(args.data_dir).resolve()
    json_out = Path(args.json_out).resolve() if args.json_out else data_dir / "admission-gaps.json"
    md_out = Path(args.md_out).resolve() if args.md_out else ROOT / "数据缺失声明.md"

    try:
        report = compute_gaps(data_dir)
    except Exception as error:  # noqa: BLE001 - 数据缺失/读库失败统一返回非零
        print(f"生成失败：{error}", file=sys.stderr)
        return 1

    json_out.parent.mkdir(parents=True, exist_ok=True)
    json_out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    md_out.write_text(render_markdown(report), encoding="utf-8")
    summary = report["summary"]
    print(f"已写入 {json_out} 与 {md_out}")
    print(f"省年 {summary['province_years']}：位次缺失 {summary['rank'].get('missing', 0)}、"
          f"批次线缺失 {summary['control'].get('missing', 0)}、"
          f"计划数缺失 {summary['plan'].get('missing', 0)}、"
          f"科类缺失 {summary['category'].get('missing', 0)}、"
          f"缺表省年 {summary['province_years_missing_tables']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
