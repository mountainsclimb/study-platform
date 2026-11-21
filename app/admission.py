"""志愿查询：GaokaoCompass 高考录取数据的只读查询接口。

数据来源与许可见 ``scripts/import_admission.py``：数据集为 MIT 许可，
由各省教育考试院等公开渠道整理，仅供参考，以官方公布为准。

本模块只做读取，不写任何表；数据库文件 ``<DATA_DIR>/admission.sqlite3``
由导入脚本生成，缺失时接口返回 503 而不是伪造空结果。
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from . import db
from .auth import current_user

router = APIRouter(prefix="/api/admission", tags=["admission"])

DB_FILENAME = "admission.sqlite3"
OFFICIAL_DB_FILENAME = "admission-official.sqlite3"
IMPORT_HINT = (
    "志愿数据尚未导入。请在 study-platform 目录运行 "
    "`python scripts/import_admission.py` 生成 data/admission.sqlite3 后重试。"
)

TABLE_LABELS = {
    "score-range": "一分一段",
    "school-admission": "院校投档线",
    "major-admission": "专业录取线",
    "enrollment-plan": "招生计划",
}
TABLE_NAMES = tuple(TABLE_LABELS)

# 数据目录名（拼音）→ 中文省名；与 scripts/import_admission.py 保持一致。
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

# 导入统计（行数、年份、省份）缓存：键包含路径与文件 mtime/size，热替换数据库后自动失效。
_info_cache: tuple[tuple[str, int, int], dict[str, Any]] | None = None
# 官方补采批次线库（可选，缺失时自动忽略）的元信息缓存。
_official_info_cache: tuple[tuple[str, int, int], dict[str, Any]] | None = None

MAX_PAGE_SIZE = 50


def _db_path():
    return db.DATA_DIR / DB_FILENAME


def _official_db_path():
    return db.DATA_DIR / OFFICIAL_DB_FILENAME


@contextmanager
def _official_connect():
    """打开官方补采库；文件缺失时 yield None，由调用方降级。"""
    path = _official_db_path()
    if not path.exists():
        yield None
        return
    connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=10)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only=ON")
        yield connection
    finally:
        connection.close()


def _official_info() -> dict[str, Any]:
    global _official_info_cache
    path = _official_db_path()
    if not path.exists():
        return {"available": False}
    stat = path.stat()
    cache_key = (str(path), stat.st_mtime_ns, stat.st_size)
    if _official_info_cache and _official_info_cache[0] == cache_key:
        return _official_info_cache[1]
    with _official_connect() as connection:
        if connection is None:
            return {"available": False}
        rows = {row["key"]: row["value"] for row in connection.execute("SELECT key, value FROM official_meta")}
    info = {
        "available": True,
        "source": rows.get("source", ""),
        "source_url": rows.get("source_url", ""),
        "imported_at": rows.get("imported_at", ""),
        "rows": int(rows.get("rows") or 0),
        "years": _json_list(rows.get("years")),
        "license_note": rows.get("license_note", ""),
    }
    _official_info_cache = (cache_key, info)
    return info


def _official_lines(province: str, year: int, category: str = "") -> list[dict[str, Any]]:
    """官方补采批次线；category 为空或未匹配时返回该省年全部记录。"""
    with _official_connect() as connection:
        if connection is None:
            return []
        rows = connection.execute(
            "SELECT category, batch, score, source_url FROM control_line"
            " WHERE province_slug=? AND year=? ORDER BY category, batch",
            (province, year),
        ).fetchall()
    items = [dict(row) for row in rows]
    if category:
        exact = [item for item in items if item["category"] == category]
        if exact:
            return exact
    return items


@contextmanager
def _connect():
    path = _db_path()
    if not path.exists():
        raise HTTPException(503, IMPORT_HINT)
    connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=10)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only=ON")
        yield connection
    finally:
        connection.close()


def _dataset_info() -> dict[str, Any]:
    """导入元信息（含覆盖度），供前端初始化筛选器。"""
    global _info_cache
    path = _db_path()
    if not path.exists():
        return {"ready": False, "detail": IMPORT_HINT}
    stat = path.stat()
    cache_key = (str(path), stat.st_mtime_ns, stat.st_size)
    if _info_cache and _info_cache[0] == cache_key:
        return _info_cache[1]

    with _connect() as connection:
        meta = {
            row["key"]: row["value"]
            for row in connection.execute("SELECT key, value FROM admission_meta")
        }
        coverage_rows = connection.execute(
            "SELECT province_slug, year, table_name, rows FROM admission_coverage"
            " ORDER BY province_slug, year, table_name"
        ).fetchall()
        overview_rows = connection.execute(
            "SELECT province_slug, year FROM admission_overview ORDER BY province_slug, year"
        ).fetchall()

    coverage: list[dict[str, Any]] = []
    index: dict[tuple[str, int], dict[str, Any]] = {}
    for row in coverage_rows:
        key = (row["province_slug"], row["year"])
        entry = index.get(key)
        if entry is None:
            entry = {"province": key[0], "year": key[1], "tables": {}}
            index[key] = entry
            coverage.append(entry)
        entry["tables"][row["table_name"]] = row["rows"]

    slugs = sorted({entry["province"] for entry in coverage})
    info = {
        "ready": True,
        "dataset": meta.get("dataset", ""),
        "source_url": meta.get("source_url", ""),
        "license": meta.get("license", ""),
        "imported_at": meta.get("imported_at", ""),
        "schema_version": meta.get("schema_version", ""),
        "years": _json_list(meta.get("years")),
        "provinces": [{"slug": slug, "name": PROVINCE_NAMES.get(slug, slug)} for slug in slugs],
        "rows": {
            table: int(meta.get(f"rows_{table}") or 0) for table in TABLE_NAMES
        },
        "total_rows": int(meta.get("total_rows") or 0),
        "overview_years": [
            {"province": row["province_slug"], "year": row["year"]} for row in overview_rows
        ],
        "coverage": coverage,
    }
    _info_cache = (cache_key, info)
    return info


def _json_list(value: str | None) -> list:
    try:
        parsed = json.loads(value or "[]")
    except ValueError:
        return []
    return parsed if isinstance(parsed, list) else []


def _require_ready() -> dict[str, Any]:
    info = _dataset_info()
    if not info.get("ready"):
        raise HTTPException(503, info.get("detail") or "志愿数据尚未导入")
    return info


def _page(page: int, page_size: int) -> tuple[int, int]:
    page = max(1, int(page or 1))
    page_size = min(MAX_PAGE_SIZE, max(1, int(page_size or 20)))
    return page, page_size


def _pick_category(connection: sqlite3.Connection, table: str, slug: str, year: int,
                   category: str) -> tuple[str, bool]:
    """返回 (实际使用的科类, 是否按科类过滤)。

    数据质量问题：部分省份年份的 ``category`` 整列为空（如河南 2024 录取表），
    此时显式传入科类会让查询全部落空，这里退化为不过滤并向前端说明。
    """
    if not category:
        row = connection.execute(
            f"SELECT category FROM {table} WHERE province_slug=? AND year=? AND category IS NOT NULL"
            " GROUP BY category ORDER BY COUNT(*) DESC LIMIT 1",
            (slug, year),
        ).fetchone()
        return (row["category"] if row else ""), bool(row)
    row = connection.execute(
        f"SELECT COUNT(*) AS total, COUNT(category) AS with_category FROM {table}"
        " WHERE province_slug=? AND year=?",
        (slug, year),
    ).fetchone()
    if row and row["total"] and not row["with_category"]:
        return "", False
    return category, True


def _like(term: str) -> str:
    return "%" + term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _resolve_names(connection: sqlite3.Connection, table: str, column: str, keyword: str,
                   order: str = "", limit: int = 12) -> list[str]:
    """在维度表里按关键词取名称（仅用于联想；事实表检索直接用 LIKE，避免 IN 列表截断）。"""
    if not keyword.strip():
        return []
    suffix = f" ORDER BY {order}" if order else ""
    rows = connection.execute(
        f"SELECT {column} FROM {table} WHERE {column} LIKE ? ESCAPE '\\'{suffix} LIMIT ?",
        (_like(keyword.strip()), limit),
    ).fetchall()
    return [row[column] for row in rows]


@router.get("/meta")
def admission_meta(user=Depends(current_user)):
    """数据集元信息：版本、省份、年份、各表行数与官方补采批次线情况。"""
    info = _dataset_info()
    if info.get("ready"):
        info = {**info, "official": _official_info()}
    return info


@router.get("/overview")
def admission_overview(
    province: str = Query(..., description="省份拼音，如 zhejiang"),
    year: int = Query(..., ge=2017, le=2100),
    user=Depends(current_user),
):
    """某省某年的看板统计（来自数据源自带的 meta.json）。"""
    _require_ready()
    with _connect() as connection:
        row = connection.execute(
            "SELECT payload FROM admission_overview WHERE province_slug=? AND year=?",
            (province, year),
        ).fetchone()
        if row is None:
            raise HTTPException(404, "该省份年份没有看板统计（数据源未提供 meta.json）")
        counts = {
            table: connection.execute(
                "SELECT rows FROM admission_coverage WHERE province_slug=? AND year=? AND table_name=?",
                (province, year, table),
            ).fetchone()["rows"]
            if connection.execute(
                "SELECT 1 FROM admission_coverage WHERE province_slug=? AND year=? AND table_name=?",
                (province, year, table),
            ).fetchone()
            else 0
            for table in TABLE_NAMES
        }
    return {
        "province": province,
        "province_name": PROVINCE_NAMES.get(province, province),
        "year": year,
        "counts": counts,
        "overview": json.loads(row["payload"]),
    }


@router.get("/filters")
def admission_filters(
    province: str = Query(...),
    year: int = Query(..., ge=2017, le=2100),
    user=Depends(current_user),
):
    """某省某年可用的科类、批次与位次支持情况，供前端筛选器初始化。"""
    _require_ready()
    with _connect() as connection:
        categories: list[str] = []
        batches: list[str] = []
        for table in ("score_range", "school_admission", "major_admission", "enrollment_plan"):
            for row in connection.execute(
                f"SELECT DISTINCT category FROM {table} WHERE province_slug=? AND year=?"
                " AND category IS NOT NULL",
                (province, year),
            ):
                if row["category"] not in categories:
                    categories.append(row["category"])
        for table in ("score_range", "school_admission", "enrollment_plan"):
            for row in connection.execute(
                f"SELECT DISTINCT batch FROM {table} WHERE province_slug=? AND year=?"
                " AND batch IS NOT NULL",
                (province, year),
            ):
                if row["batch"] not in batches:
                    batches.append(row["batch"])
        stats = connection.execute(
            "SELECT COUNT(*) AS total, COUNT(min_rank) AS with_rank FROM school_admission"
            " WHERE province_slug=? AND year=?",
            (province, year),
        ).fetchone()
        plan_stats = connection.execute(
            "SELECT COUNT(*) AS total, COUNT(plan_count) AS with_plan FROM enrollment_plan"
            " WHERE province_slug=? AND year=?",
            (province, year),
        ).fetchone()
        score_stats = connection.execute(
            "SELECT COUNT(*) AS total, COUNT(control_score) AS with_control FROM score_range"
            " WHERE province_slug=? AND year=?",
            (province, year),
        ).fetchone()
        category_stats = connection.execute(
            "SELECT COUNT(*) AS total,"
            " SUM(CASE WHEN category IS NOT NULL THEN 1 ELSE 0 END) AS with_category"
            " FROM school_admission WHERE province_slug=? AND year=?",
            (province, year),
        ).fetchone()
        counts = {
            row["table_name"]: row["rows"]
            for row in connection.execute(
                "SELECT table_name, rows FROM admission_coverage WHERE province_slug=? AND year=?",
                (province, year),
            )
        }
    return {
        "province": province,
        "province_name": PROVINCE_NAMES.get(province, province),
        "year": year,
        "categories": categories,
        "batches": batches,
        "rank_supported": bool(stats and stats["total"] and stats["with_rank"]),
        "control_score_supported": bool(score_stats and score_stats["with_control"])
        or bool(_official_lines(province, year)),
        "official_control_supported": bool(_official_lines(province, year)),
        "plan_supported": bool(plan_stats and plan_stats["with_plan"]),
        "category_available": bool(category_stats and category_stats["with_category"]),
        "counts": counts,
    }


# 数据缺失声明（由 scripts/report_data_gaps.py 生成）的读取缓存。
_gaps_cache: tuple[tuple[str, int, int], dict[str, Any]] | None = None


def _gaps_path():
    return db.DATA_DIR / "admission-gaps.json"


@router.get("/gaps")
def admission_gaps(user=Depends(current_user)):
    """数据缺失声明：按省年列出位次、批次线、计划数、科类与缺表情况。"""
    global _gaps_cache
    path = _gaps_path()
    if not path.exists():
        return {
            "available": False,
            "detail": "尚未生成数据缺失声明。请运行 `python scripts/report_data_gaps.py` 后刷新。",
        }
    stat = path.stat()
    cache_key = (str(path), stat.st_mtime_ns, stat.st_size)
    if _gaps_cache and _gaps_cache[0] == cache_key:
        payload = _gaps_cache[1]
    else:
        payload = json.loads(path.read_text(encoding="utf-8"))
        _gaps_cache = (cache_key, payload)
    return {"available": True, **payload}


@router.get("/control-lines")
def admission_control_lines(
    province: str = Query(...),
    year: int = Query(..., ge=2017, le=2100),
    category: str = Query(""),
    user=Depends(current_user),
):
    """官方渠道补采的批次控制线（2020 年起，含来源 URL）。"""
    _require_ready()
    items = _official_lines(province, year, category)
    return {
        "province": province,
        "province_name": PROVINCE_NAMES.get(province, province),
        "year": year,
        "category": category,
        "items": items,
        "official": _official_info(),
    }


@router.get("/score-rank")
def admission_score_rank(
    province: str = Query(...),
    year: int = Query(..., ge=2017, le=2100),
    category: str = Query("", description="科类；留空自动选择数据最多的科类"),
    score: int = Query(..., ge=0, le=1000, description="高考分数"),
    user=Depends(current_user),
):
    """一分一段：分数 → 累计位次，并返回同科类分布用于画图。"""
    _require_ready()
    with _connect() as connection:
        used_category, filtered = _pick_category(connection, "score_range", province, year, category)
        if not filtered:
            raise HTTPException(404, "该省份年份没有一分一段数据")
        rows = connection.execute(
            "SELECT batch, control_score, score, segment_count, cumulative_count, rank_range"
            " FROM score_range WHERE province_slug=? AND year=? AND category=?"
            " AND score IS NOT NULL ORDER BY score DESC",
            (province, year, used_category),
        ).fetchall()
        if not rows:
            raise HTTPException(404, "该省份年份没有一分一段数据")
        exact = [row for row in rows if row["score"] == score]
        if exact:
            matches = exact
            nearest = exact[0]
        else:
            nearest = min(rows, key=lambda item: abs(item["score"] - score))
            matches = [nearest]
        # 同一分数可能出现在多个批次（如本科一批/二批）：全部返回，取批次线最高者为主口径。
        matches = sorted(
            matches,
            key=lambda item: (item["control_score"] is None, -(item["control_score"] or 0)),
        )
        primary = matches[0]
        chart_batch = primary["batch"]
        distribution = [
            {
                "score": row["score"],
                "cumulative_count": row["cumulative_count"],
                "segment_count": row["segment_count"],
            }
            for row in rows
            if row["batch"] == chart_batch
        ]
        distribution.sort(key=lambda item: item["score"])
        sampled = _sample_series(distribution, 60)

    # 主数据源没有批次线时，用官方补采库补齐并标注来源：
    # 1) 一分一段表带批次名 → 按批次精确匹配；
    # 2) 一分一段表整表没有批次（2017–2021 常见）→ 按分数匹配「该分数能过线的最严批次」，
    #    来源标记为 official-inferred，界面需说明是推导，不是原表批次。
    official_lines = _official_lines(province, year, used_category)
    official_match = next(
        (line for line in official_lines if line["batch"] == primary["batch"]), None)
    control_score = primary["control_score"]
    control_source = "score-range" if control_score is not None else None
    control_batch = primary["batch"]
    if control_score is None and official_match is not None:
        control_score = official_match["score"]
        control_batch = official_match["batch"]
        control_source = "official"
    elif control_score is None and not primary["batch"] and official_lines:
        cleared = [line for line in official_lines if line["score"] <= score]
        best = max(cleared, key=lambda line: line["score"], default=None)
        if best is None:
            best = min(official_lines, key=lambda line: line["score"])
        control_score = best["score"]
        control_batch = best["batch"]
        control_source = "official-inferred"
    return {
        "province": province,
        "province_name": PROVINCE_NAMES.get(province, province),
        "year": year,
        "category": used_category,
        "category_filtered": filtered,
        "score": score,
        "exact": bool(exact),
        "nearest_score": nearest["score"],
        "batch": primary["batch"],
        "control_score": control_score,
        "control_batch": control_batch,
        "control_source": control_source,
        "official_batches": [
            {"category": line["category"], "batch": line["batch"], "score": line["score"]}
            for line in official_lines
        ],
        "segment_count": primary["segment_count"],
        "cumulative_count": primary["cumulative_count"],
        "rank_range": primary["rank_range"],
        "total": max((item["cumulative_count"] or 0) for item in rows),
        "matches": [
            {
                "batch": item["batch"],
                "control_score": item["control_score"],
                "segment_count": item["segment_count"],
                "cumulative_count": item["cumulative_count"],
                "rank_range": item["rank_range"],
            }
            for item in matches
        ],
        "distribution": sampled,
    }


def _sample_series(points: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    if len(points) <= limit:
        return points
    step = (len(points) - 1) / (limit - 1)
    picked = [points[round(index * step)] for index in range(limit)]
    return picked


@router.get("/schools")
def admission_schools(
    province: str = Query(...),
    year: int = Query(..., ge=2017, le=2100),
    category: str = Query(""),
    score: int | None = Query(None, ge=0, le=1000, description="按分数找学校（与 rank 二选一）"),
    rank: int | None = Query(None, ge=1, description="按位次找学校"),
    band: int = Query(15, ge=1, le=80, description="分数浮动范围"),
    keyword: str = Query("", max_length=40, description="院校名称关键词"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=MAX_PAGE_SIZE),
    user=Depends(current_user),
):
    """按分数/位次/关键词找学校，并按「冲/稳/保」标注。"""
    _require_ready()
    page, page_size = _page(page, page_size)
    with _connect() as connection:
        used_category, filtered = _pick_category(
            connection, "school_admission", province, year, category
        )
        where = ["province_slug=?", "year=?"]
        params: list[Any] = [province, year]
        if filtered and used_category:
            where.append("category=?")
            params.append(used_category)
        mode = ""
        rank_supported = True
        if keyword.strip():
            # 事实表按「省份 + 年份」已索引，直接 LIKE 扫描单省单年（最多数万行），
            # 比把关键词解析成 IN 列表更准确：同名专业存在大量带后缀的变体。
            where.append("university_name LIKE ? ESCAPE '\\'")
            params.append(_like(keyword.strip()))
            mode = "keyword"
        elif score is not None:
            where.append("min_score BETWEEN ? AND ?")
            params.extend([score - band, score + band])
            mode = "score"
        elif rank is not None:
            stats = connection.execute(
                "SELECT COUNT(*) AS total, COUNT(min_rank) AS with_rank FROM school_admission"
                " WHERE province_slug=? AND year=?",
                (province, year),
            ).fetchone()
            rank_supported = bool(stats and stats["total"] and stats["with_rank"])
            if not rank_supported:
                return _schools_payload(province, year, used_category, filtered, "rank", [], 0, page, page_size, False,
                                        note="该省份年份的投档线缺少位次数据，请改用分数查询。")
            where.append("min_rank BETWEEN ? AND ?")
            params.extend([max(1, int(rank * 0.65)), int(rank * 1.6)])
            mode = "rank"
        else:
            raise HTTPException(422, "请提供 score、rank 或 keyword 之一")

        clause = " AND ".join(where)
        total_row = connection.execute(
            f"SELECT COUNT(DISTINCT university_name) AS total FROM school_admission WHERE {clause}",
            params,
        ).fetchone()
        total = int(total_row["total"] if total_row else 0)
        # 按「离考生分数/位次最近」排序：冲（略高）与稳（略低）交替出现，保底院校靠后，
        # 避免首页 20 条全是同一档。
        if mode == "score":
            order, order_params = "ABS(MIN(min_score) - ?) ASC", [score]
        elif mode == "rank":
            order, order_params = "ABS(MIN(min_rank) - ?) ASC", [rank]
        else:
            order, order_params = "MIN(min_score) DESC", []
        rows = connection.execute(
            "SELECT university_name, MIN(min_score) AS min_score, MIN(min_rank) AS min_rank,"
            " MAX(admit_count) AS admit_count, MAX(is_985) AS is_985, MAX(is_211) AS is_211,"
            " MAX(school_province) AS school_province, MAX(school_nature) AS school_nature,"
            " COUNT(*) AS cutoff_rows"
            f" FROM school_admission WHERE {clause} GROUP BY university_name"
            f" ORDER BY {order} LIMIT ? OFFSET ?",
            [*params, *order_params, page_size, (page - 1) * page_size],
        ).fetchall()

    items = []
    for row in rows:
        item = {
            "university_name": row["university_name"],
            "min_score": row["min_score"],
            "min_rank": row["min_rank"],
            "admit_count": row["admit_count"],
            "is_985": int(row["is_985"] or 0),
            "is_211": int(row["is_211"] or 0),
            "school_province": row["school_province"],
            "school_nature": row["school_nature"],
            "cutoff_rows": row["cutoff_rows"],
        }
        if mode == "score" and row["min_score"] is not None:
            item["delta"] = int(score - row["min_score"])
            item["level"] = "冲" if item["delta"] < 0 else "稳" if item["delta"] <= 10 else "保"
        elif mode == "rank" and row["min_rank"]:
            ratio = row["min_rank"] / rank
            item["ratio"] = round(ratio, 2)
            item["level"] = "冲" if ratio < 0.85 else "稳" if ratio <= 1.2 else "保"
        items.append(item)
    return _schools_payload(province, year, used_category, filtered, mode, items, total, page, page_size, rank_supported)


def _schools_payload(province, year, category, filtered, mode, items, total, page, page_size,
                     rank_supported, note: str = ""):
    payload = {
        "province": province,
        "province_name": PROVINCE_NAMES.get(province, province),
        "year": year,
        "category": category,
        "category_filtered": filtered,
        "mode": mode,
        "rank_supported": rank_supported,
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }
    if note:
        payload["note"] = note
    return payload


@router.get("/school")
def admission_school(
    province: str = Query(...),
    year: int = Query(..., ge=2017, le=2100),
    name: str = Query(..., min_length=1, max_length=80),
    category: str = Query(""),
    user=Depends(current_user),
):
    """单个学校的投档线、专业录取线、招生计划与历年趋势。"""
    _require_ready()
    with _connect() as connection:
        university = connection.execute(
            "SELECT * FROM admission_university WHERE name=?", (name,)
        ).fetchone()
        used_category, filtered = _pick_category(
            connection, "school_admission", province, year, category
        )
        school_where = "province_slug=? AND year=? AND university_name=?"
        school_params: list[Any] = [province, year, name]
        if filtered and used_category:
            school_where += " AND category=?"
            school_params.append(used_category)
        cutoffs = connection.execute(
            "SELECT category, batch, major_group, subject_req, min_score, min_rank, control_score,"
            " score_diff, admit_count FROM school_admission WHERE " + school_where +
            " ORDER BY min_score DESC",
            school_params,
        ).fetchall()
        majors = connection.execute(
            "SELECT category, batch, major_name, major_note, subject_req, min_score, min_rank,"
            " avg_score, admit_count FROM major_admission WHERE " + school_where +
            " ORDER BY min_score DESC LIMIT 300",
            school_params,
        ).fetchall()
        plan_where = "province_slug=? AND year=? AND university_name=?"
        plan_params: list[Any] = [province, year, name]
        if filtered and used_category:
            plan_where += " AND category=?"
            plan_params.append(used_category)
        plans = connection.execute(
            "SELECT batch, major_name, major_note, subject_req, plan_count, duration, tuition"
            " FROM enrollment_plan WHERE " + plan_where +
            " ORDER BY plan_count DESC LIMIT 300",
            plan_params,
        ).fetchall()
        plan_sum = connection.execute(
            "SELECT COUNT(*) AS rows, SUM(plan_count) AS people FROM enrollment_plan WHERE " + plan_where,
            plan_params,
        ).fetchone()
        trend = connection.execute(
            "SELECT year, MIN(min_score) AS min_score, MIN(min_rank) AS min_rank,"
            " MAX(admit_count) AS admit_count FROM school_admission"
            " WHERE province_slug=? AND university_name=? GROUP BY year ORDER BY year",
            (province, name),
        ).fetchall()

    return {
        "province": province,
        "province_name": PROVINCE_NAMES.get(province, province),
        "year": year,
        "name": name,
        "category": used_category,
        "category_filtered": filtered,
        "university": {
            "name": university["name"],
            "school_province": university["school_province"],
            "school_nature": university["school_nature"],
            "is_985": int(university["is_985"] or 0),
            "is_211": int(university["is_211"] or 0),
            "school_rows": university["school_rows"],
            "major_rows": university["major_rows"],
            "plan_rows": university["plan_rows"],
        } if university else {"name": name},
        "cutoffs": [dict(row) for row in cutoffs],
        "majors": [dict(row) for row in majors],
        "plans": [dict(row) for row in plans],
        "plan_summary": {
            "rows": int(plan_sum["rows"] or 0) if plan_sum else 0,
            "people": int(plan_sum["people"] or 0) if plan_sum and plan_sum["people"] else 0,
        },
        "trend": [dict(row) for row in trend],
    }


@router.get("/majors")
def admission_majors(
    province: str = Query(...),
    year: int = Query(..., ge=2017, le=2100),
    category: str = Query(""),
    keyword: str = Query("", max_length=40, description="专业名称关键词"),
    university: str = Query("", max_length=80),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=MAX_PAGE_SIZE),
    user=Depends(current_user),
):
    """专业录取线检索：按专业关键词或院校过滤。"""
    _require_ready()
    page, page_size = _page(page, page_size)
    with _connect() as connection:
        used_category, filtered = _pick_category(
            connection, "major_admission", province, year, category
        )
        where = ["province_slug=?", "year=?"]
        params: list[Any] = [province, year]
        if filtered and used_category:
            where.append("category=?")
            params.append(used_category)
        if university.strip():
            where.append("university_name LIKE ? ESCAPE '\\'")
            params.append(_like(university.strip()))
        if keyword.strip():
            where.append("major_name LIKE ? ESCAPE '\\'")
            params.append(_like(keyword.strip()))
        clause = " AND ".join(where)
        total = int(connection.execute(
            f"SELECT COUNT(*) AS total FROM major_admission WHERE {clause}", params
        ).fetchone()["total"])
        rows = connection.execute(
            "SELECT university_name, major_name, batch, major_group, subject_req, min_score,"
            " min_rank, max_score, avg_score, admit_count, school_province, school_nature, is_985, is_211"
            f" FROM major_admission WHERE {clause} ORDER BY min_score DESC, university_name"
            " LIMIT ? OFFSET ?",
            [*params, page_size, (page - 1) * page_size],
        ).fetchall()
    return {
        "province": province,
        "province_name": PROVINCE_NAMES.get(province, province),
        "year": year,
        "category": used_category,
        "category_filtered": filtered,
        "items": [dict(row) for row in rows],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@router.get("/plans")
def admission_plans(
    province: str = Query(...),
    year: int = Query(..., ge=2017, le=2100),
    category: str = Query(""),
    keyword: str = Query("", max_length=40, description="专业名称关键词"),
    university: str = Query("", max_length=80),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=MAX_PAGE_SIZE),
    user=Depends(current_user),
):
    """招生计划检索：按专业关键词或院校过滤。"""
    _require_ready()
    page, page_size = _page(page, page_size)
    with _connect() as connection:
        used_category, filtered = _pick_category(
            connection, "enrollment_plan", province, year, category
        )
        where = ["province_slug=?", "year=?"]
        params: list[Any] = [province, year]
        if filtered and used_category:
            where.append("category=?")
            params.append(used_category)
        if university.strip():
            where.append("university_name LIKE ? ESCAPE '\\'")
            params.append(_like(university.strip()))
        if keyword.strip():
            where.append("major_name LIKE ? ESCAPE '\\'")
            params.append(_like(keyword.strip()))
        clause = " AND ".join(where)
        total = int(connection.execute(
            f"SELECT COUNT(*) AS total FROM enrollment_plan WHERE {clause}", params
        ).fetchone()["total"])
        summary = connection.execute(
            "SELECT SUM(plan_count) AS people FROM enrollment_plan WHERE " + clause, params
        ).fetchone()
        rows = connection.execute(
            "SELECT university_name, major_name, batch, major_group, subject_req, plan_count,"
            " duration, tuition, major_note"
            f" FROM enrollment_plan WHERE {clause} ORDER BY plan_count DESC, university_name"
            " LIMIT ? OFFSET ?",
            [*params, page_size, (page - 1) * page_size],
        ).fetchall()
    return {
        "province": province,
        "province_name": PROVINCE_NAMES.get(province, province),
        "year": year,
        "category": used_category,
        "category_filtered": filtered,
        "items": [dict(row) for row in rows],
        "total": total,
        "total_people": int(summary["people"] or 0) if summary and summary["people"] else 0,
        "page": page,
        "page_size": page_size,
    }


@router.get("/suggest")
def admission_suggest(
    q: str = Query(..., min_length=1, max_length=40),
    user=Depends(current_user),
):
    """院校 / 专业名称联想，用于搜索框自动补全。"""
    _require_ready()
    term = _like(q.strip())
    with _connect() as connection:
        universities = connection.execute(
            "SELECT name, school_province, school_nature, is_985, is_211 FROM admission_university"
            " WHERE name LIKE ? ESCAPE '\\' ORDER BY is_985 DESC, is_211 DESC, name LIMIT 12",
            (term,),
        ).fetchall()
        majors = connection.execute(
            "SELECT name, plan_rows, plan_people FROM admission_major WHERE name LIKE ? ESCAPE '\\'"
            " ORDER BY plan_rows DESC, name LIMIT 12",
            (term,),
        ).fetchall()
    return {
        "universities": [dict(row) for row in universities],
        "majors": [dict(row) for row in majors],
    }


@router.get("/trend")
def admission_trend(
    province: str = Query(...),
    university: str = Query(..., min_length=1, max_length=80),
    category: str = Query(""),
    user=Depends(current_user),
):
    """某校在某省的历年投档线趋势（分数 / 位次）。"""
    _require_ready()
    with _connect() as connection:
        where = ["province_slug=?", "university_name=?"]
        params: list[Any] = [province, university]
        if category:
            where.append("category=?")
            params.append(category)
        rows = connection.execute(
            "SELECT year, MIN(min_score) AS min_score, MIN(min_rank) AS min_rank,"
            " MAX(admit_count) AS admit_count FROM school_admission WHERE " + " AND ".join(where) +
            " GROUP BY year ORDER BY year",
            params,
        ).fetchall()
    return {
        "province": province,
        "province_name": PROVINCE_NAMES.get(province, province),
        "university": university,
        "items": [dict(row) for row in rows],
    }
