#!/usr/bin/env python
"""把 GaokaoCompass 数据集导入为只读的 ``data/admission.sqlite3``。

数据源（MIT 许可）：
- GitHub 仓库只提供说明文档，不含代码；数据在 Hugging Face
  ``choucsan/Gaokao-Compass-11M``。
- 目录结构 ``data/{year}/{province}/{table}.csv``，另有每省每年的
  ``meta.json``（6 类看板统计）。

本脚本只用标准库（不引入 pandas 等新依赖），流程：
1. 读取 Hugging Face 文件清单（含大小）；
2. 多线程下载到 ``data/.admission-cache``（已存在的完整文件跳过）；
3. 逐文件解析、清洗，批量写入 SQLite；
4. 建立查询索引、院校/专业维度表、覆盖度表与元信息表。

用法::

    cd study-platform
    python scripts/import_admission.py                # 下载 + 建库（约 1.35GB 源数据）
    python scripts/import_admission.py --skip-download  # 复用已下载缓存
    python scripts/import_admission.py --prune-cache    # 建库成功后删除原始 CSV 缓存

生成物可随时重建；``data/`` 已在 .gitignore 中，不入库。
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA_DIR = Path(os.environ.get("STUDY_DATA_DIR", str(ROOT / "data"))).resolve()
DATASET = "choucsan/Gaokao-Compass-11M"
DATASET_API = f"https://huggingface.co/api/datasets/{DATASET}"
TREE_API = f"{DATASET_API}/tree/main/data?recursive=true"
RESOLVE_HOSTS = ("https://huggingface.co", "https://hf-mirror.com")
USER_AGENT = "Mozilla/5.0 (compatible; zhixu-admission-importer/1.0)"
SCHEMA_VERSION = "1"
TABLE_NAMES = ("enrollment-plan", "major-admission", "school-admission", "score-range")
DB_TABLE = {
    "enrollment-plan": "enrollment_plan",
    "major-admission": "major_admission",
    "school-admission": "school_admission",
    "score-range": "score_range",
}

# 省份拼音（数据目录名）→ 中文名。目录名由上游固定，这里显式列出，避免猜测。
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

SCHEMA_SQL = [
    """CREATE TABLE score_range (
        id INTEGER PRIMARY KEY,
        province TEXT,
        province_slug TEXT NOT NULL,
        year INTEGER NOT NULL,
        category TEXT,
        batch TEXT,
        control_score INTEGER,
        score INTEGER,
        score_range TEXT,
        segment_count INTEGER,
        cumulative_count INTEGER,
        rank_range TEXT
    )""",
    """CREATE TABLE school_admission (
        id INTEGER PRIMARY KEY,
        province TEXT,
        province_slug TEXT NOT NULL,
        year INTEGER NOT NULL,
        category TEXT,
        batch TEXT,
        university_code TEXT,
        university_name TEXT NOT NULL,
        major_group TEXT,
        subject_req TEXT,
        min_score INTEGER,
        min_rank INTEGER,
        control_score INTEGER,
        score_diff INTEGER,
        admit_count INTEGER,
        school_province TEXT,
        school_nature TEXT,
        is_985 INTEGER DEFAULT 0,
        is_211 INTEGER DEFAULT 0
    )""",
    """CREATE TABLE major_admission (
        id INTEGER PRIMARY KEY,
        province TEXT,
        province_slug TEXT NOT NULL,
        year INTEGER NOT NULL,
        category TEXT,
        batch TEXT,
        university_code TEXT,
        university_name TEXT NOT NULL,
        major_group TEXT,
        major_code TEXT,
        major_name TEXT,
        major_note TEXT,
        subject_req TEXT,
        min_score INTEGER,
        min_rank INTEGER,
        max_score INTEGER,
        avg_score REAL,
        admit_count INTEGER,
        school_province TEXT,
        school_nature TEXT,
        is_985 INTEGER DEFAULT 0,
        is_211 INTEGER DEFAULT 0
    )""",
    """CREATE TABLE enrollment_plan (
        id INTEGER PRIMARY KEY,
        province TEXT,
        province_slug TEXT NOT NULL,
        year INTEGER NOT NULL,
        batch TEXT,
        category TEXT,
        university_code TEXT,
        university_name TEXT NOT NULL,
        major_group TEXT,
        major_code TEXT,
        major_name TEXT,
        major_note TEXT,
        subject_req TEXT,
        plan_count INTEGER,
        duration TEXT,
        tuition REAL
    )""",
    """CREATE TABLE admission_university (
        name TEXT PRIMARY KEY,
        school_province TEXT,
        school_nature TEXT,
        is_985 INTEGER DEFAULT 0,
        is_211 INTEGER DEFAULT 0,
        school_rows INTEGER DEFAULT 0,
        major_rows INTEGER DEFAULT 0,
        plan_rows INTEGER DEFAULT 0
    )""",
    """CREATE TABLE admission_major (
        name TEXT PRIMARY KEY,
        plan_rows INTEGER DEFAULT 0,
        plan_people INTEGER DEFAULT 0,
        admission_rows INTEGER DEFAULT 0
    )""",
    """CREATE TABLE admission_coverage (
        province_slug TEXT NOT NULL,
        year INTEGER NOT NULL,
        table_name TEXT NOT NULL,
        rows INTEGER NOT NULL,
        PRIMARY KEY (province_slug, year, table_name)
    )""",
    """CREATE TABLE admission_overview (
        province_slug TEXT NOT NULL,
        year INTEGER NOT NULL,
        payload TEXT NOT NULL,
        PRIMARY KEY (province_slug, year)
    )""",
    """CREATE TABLE admission_meta (
        key TEXT PRIMARY KEY,
        value TEXT
    )""",
]

INDEX_SQL = [
    "CREATE INDEX idx_sr_lookup ON score_range(province_slug, year, category, score)",
    "CREATE INDEX idx_sr_batch ON score_range(province_slug, year, batch)",
    "CREATE INDEX idx_sa_score ON school_admission(province_slug, year, category, min_score)",
    "CREATE INDEX idx_sa_rank ON school_admission(province_slug, year, category, min_rank)",
    "CREATE INDEX idx_sa_name ON school_admission(university_name, province_slug, year)",
    "CREATE INDEX idx_ma_school ON major_admission(province_slug, year, university_name)",
    "CREATE INDEX idx_ma_name ON major_admission(province_slug, year, major_name)",
    "CREATE INDEX idx_ma_score ON major_admission(province_slug, year, category, min_score)",
    "CREATE INDEX idx_ep_school ON enrollment_plan(province_slug, year, university_name)",
    "CREATE INDEX idx_ep_name ON enrollment_plan(province_slug, year, major_name)",
]

INSERT_SQL = {
    "score-range": (
        "INSERT INTO score_range (province, province_slug, year, category, batch, control_score,"
        " score, score_range, segment_count, cumulative_count, rank_range)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)"
    ),
    "school-admission": (
        "INSERT INTO school_admission (province, province_slug, year, category, batch, university_code,"
        " university_name, major_group, subject_req, min_score, min_rank, control_score, score_diff,"
        " admit_count, school_province, school_nature, is_985, is_211)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
    ),
    "major-admission": (
        "INSERT INTO major_admission (province, province_slug, year, category, batch, university_code,"
        " university_name, major_group, major_code, major_name, major_note, subject_req, min_score,"
        " min_rank, max_score, avg_score, admit_count, school_province, school_nature, is_985, is_211)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
    ),
    "enrollment-plan": (
        "INSERT INTO enrollment_plan (province, province_slug, year, batch, category, university_code,"
        " university_name, major_group, major_code, major_name, major_note, subject_req, plan_count,"
        " duration, tuition)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
    ),
}

_EMPTY = {"", "null", "none", "nan", "nat", "<na>", "(null)", "-", "—", "－", "/", "\\"}


def _text(value):
    if value is None:
        return None
    text = str(value).strip().replace("\u3000", " ")
    if not text or text.lower() in _EMPTY:
        return None
    return text


def _int(value):
    if value is None:
        return None
    text = str(value).strip().replace(",", "")
    if not text or text.lower() in _EMPTY:
        return None
    try:
        return int(round(float(text)))
    except ValueError:
        # 形如 "600分" / "600（含加分）" 的脏值：只取前导数字。
        digits = ""
        for ch in text:
            if ch.isdigit() or (ch == "-" and not digits):
                digits += ch
            elif digits:
                break
        try:
            return int(digits) if digits not in ("", "-") else None
        except ValueError:
            return None


def _float(value):
    if value is None:
        return None
    text = str(value).strip().replace(",", "")
    if not text or text.lower() in _EMPTY:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _code(value):
    """代码字段去掉 Excel 带出的 ``.0`` 尾巴，保留原始前导零写法。"""
    text = _text(value)
    if text is None:
        return None
    if text.endswith(".0") and text[:-2].isdigit():
        return text[:-2]
    return text


def _flag(value):
    number = _int(value)
    return 1 if number == 1 else 0


def _log(message: str) -> None:
    print(message, flush=True)


def relative_data_path(remote_path: str) -> str:
    """``data/2024/zhejiang/x.csv`` → ``2024/zhejiang/x.csv``（缓存目录内相对路径）。"""
    prefix = "data/"
    return remote_path[len(prefix):] if remote_path.startswith(prefix) else remote_path


class Downloader:
    def __init__(self, cache_dir: Path, workers: int):
        self.cache_dir = cache_dir
        self.workers = workers

    def _target(self, remote_path: str) -> Path:
        return self.cache_dir / relative_data_path(remote_path)

    def _fetch_one(self, remote_path: str, expected_size: int | None) -> tuple[str, str, int]:
        target = self._target(remote_path)
        if target.exists():
            size = target.stat().st_size
            if not expected_size or size == expected_size:
                return remote_path, "cached", size
        target.parent.mkdir(parents=True, exist_ok=True)
        last_error = None
        for host in RESOLVE_HOSTS:
            url = f"{host}/datasets/{DATASET}/resolve/main/{remote_path}"
            for attempt in range(2):
                try:
                    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
                    with urllib.request.urlopen(request, timeout=180) as response:
                        data = response.read()
                    if expected_size and len(data) != expected_size:
                        raise IOError(f"大小不符 {len(data)} != {expected_size}")
                    temp = target.with_suffix(target.suffix + ".part")
                    temp.write_bytes(data)
                    os.replace(temp, target)
                    return remote_path, "downloaded", len(data)
                except Exception as error:  # noqa: BLE001 - 逐文件重试，失败继续下一个源
                    last_error = error
                    time.sleep(0.6 * (attempt + 1))
        raise RuntimeError(f"{remote_path}: {last_error}")

    def download(self, tasks: list[tuple[str, int | None]]) -> dict[str, str]:
        """下载全部文件；返回 {remote_path: status}。"""
        results: dict[str, str] = {}
        total = len(tasks)
        done = 0
        started = time.time()
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            futures = {pool.submit(self._fetch_one, path, size): path for path, size in tasks}
            for future in as_completed(futures):
                path = futures[future]
                try:
                    _, status, _ = future.result()
                    results[path] = status
                except Exception as error:  # noqa: BLE001 - 单个文件失败不中断整体
                    results[path] = f"error: {error}"
                    _log(f"[下载失败] {path}: {error}")
                done += 1
                if done % 50 == 0 or done == total:
                    elapsed = time.time() - started
                    _log(f"  下载进度 {done}/{total}  ({elapsed:.0f}s)")
        return results


def fetch_file_list() -> list[tuple[str, int | None]]:
    """读取数据集文件清单（路径 + 大小），只保留 data/ 下的 CSV 与 meta.json。"""
    entries: list[tuple[str, int | None]] = []
    page_url = TREE_API
    seen_urls = set()
    while page_url and page_url not in seen_urls:
        seen_urls.add(page_url)
        request = urllib.request.Request(page_url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=120) as response:
            payload = json.loads(response.read())
            link = response.headers.get("Link") or ""
        for item in payload:
            path = item.get("path") or ""
            if item.get("type") != "file":
                continue
            if not (path.endswith(".csv") or path.endswith("meta.json")):
                continue
            if not path.startswith("data/"):
                continue
            entries.append((path, item.get("size")))
        next_url = None
        for part in link.split(","):
            if 'rel="next"' in part:
                next_url = part.split(";")[0].strip().strip("<>")
        page_url = next_url
    if not entries:
        raise RuntimeError("未取到任何数据文件，请检查网络或数据集是否改名")
    entries.sort(key=lambda item: item[0])
    return entries


def parse_path(remote_path: str) -> tuple[int | None, str | None, str | None]:
    """``data/{year}/{province}/{file}`` 或 ``{year}/{province}/{file}`` → (year, slug, table)。"""
    parts = remote_path.replace("\\", "/").split("/")
    parts = [part for part in parts if part and part != "data"]
    if len(parts) < 3:
        return None, None, None
    try:
        year = int(parts[0])
    except (TypeError, ValueError):
        year = None
    slug = parts[1]
    filename = parts[2]
    table = filename[:-4] if filename.endswith(".csv") else None
    return year, slug, table


def build_database(
    files: list[Path],
    cache_root: Path,
    db_path: Path,
    *,
    dataset_sha: str | None = None,
    skipped: list[str] | None = None,
) -> dict:
    """把缓存里的 CSV 写成 admission.sqlite3，返回导入统计。"""
    if db_path.exists():
        db_path.unlink()
    temp_path = db_path.with_suffix(".sqlite3.tmp")
    if temp_path.exists():
        temp_path.unlink()

    connection = sqlite3.connect(str(temp_path))
    try:
        connection.execute("PRAGMA journal_mode=OFF")
        connection.execute("PRAGMA synchronous=OFF")
        connection.execute("PRAGMA temp_store=MEMORY")
        connection.execute("PRAGMA cache_size=-200000")
        connection.execute("PRAGMA page_size=4096")
        for statement in SCHEMA_SQL:
            connection.execute(statement)

        table_rows: dict[str, int] = defaultdict(int)
        coverage: dict[tuple[str, int, str], int] = {}
        province_names: dict[str, str] = {}
        universities: dict[str, list] = {}
        majors: dict[str, list] = {}
        overview_count = 0
        parsed_files = 0
        failed_files = 0

        csv_files = [path for path in files if path.suffix == ".csv"]
        csv_files.sort()
        started = time.time()
        for index, path in enumerate(csv_files, 1):
            year, slug, table = parse_path(path.relative_to(cache_root).as_posix())
            if not year or not slug or table not in TABLE_NAMES:
                _log(f"[跳过] 无法识别路径: {path}")
                failed_files += 1
                continue
            province = PROVINCE_NAMES.get(slug)
            if province is None:
                _log(f"[跳过] 未知省份目录: {slug}")
                failed_files += 1
                continue
            province_names[slug] = province
            try:
                rows = _insert_csv(connection, path, table, year, slug, province, universities, majors)
            except Exception as error:  # noqa: BLE001 - 单文件失败继续，最后写进元信息
                _log(f"[解析失败] {path}: {error}")
                failed_files += 1
                continue
            table_rows[table] += rows
            coverage[(slug, year, table)] = coverage.get((slug, year, table), 0) + rows
            parsed_files += 1
            if index % 50 == 0 or index == len(csv_files):
                elapsed = time.time() - started
                total = sum(table_rows.values())
                _log(f"  解析进度 {index}/{len(csv_files)}  已写入 {total:,} 行 ({elapsed:.0f}s)")

        # 每省每年的 meta.json 原样保留，供前端看板卡片直接使用。
        for path in sorted(files):
            if path.name != "meta.json":
                continue
            year, slug, _ = parse_path(path.relative_to(cache_root).as_posix())
            if not year or not slug:
                continue
            try:
                payload = path.read_text(encoding="utf-8")
                json.loads(payload)  # 校验可解析后再入库
            except (OSError, ValueError) as error:
                _log(f"[meta 跳过] {path}: {error}")
                continue
            connection.execute(
                "INSERT OR REPLACE INTO admission_overview (province_slug, year, payload) VALUES (?,?,?)",
                (slug, year, payload),
            )
            overview_count += 1

        _log("  建立索引…")
        for statement in INDEX_SQL:
            connection.execute(statement)

        _log("  写入院校/专业维度表…")
        _write_dimensions(connection, universities, majors)
        connection.executemany(
            "INSERT OR REPLACE INTO admission_coverage (province_slug, year, table_name, rows)"
            " VALUES (?,?,?,?)",
            [(slug, year, table, rows) for (slug, year, table), rows in coverage.items()],
        )

        imported_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        years = sorted({year for (_, year, _) in coverage})
        provinces = sorted(province_names)
        meta_rows = {
            "schema_version": SCHEMA_VERSION,
            "dataset": DATASET,
            "dataset_sha": dataset_sha or "",
            "source_url": f"https://huggingface.co/datasets/{DATASET}",
            "license": "MIT (Copyright (c) 2025 choucisan)；数据来自各省教育考试院等公开渠道，仅供参考",
            "imported_at": imported_at,
            "parsed_files": str(parsed_files),
            "failed_files": str(failed_files),
            "skipped_files": str(len(skipped or [])),
            "total_rows": str(sum(table_rows.values())),
            "years": json.dumps(years, ensure_ascii=False),
            "provinces": json.dumps(provinces, ensure_ascii=False),
            "overview_files": str(overview_count),
        }
        for table in TABLE_NAMES:
            meta_rows[f"rows_{table}"] = str(table_rows.get(table, 0))
        connection.executemany(
            "INSERT OR REPLACE INTO admission_meta (key, value) VALUES (?,?)",
            list(meta_rows.items()),
        )
        connection.commit()
        _log("  ANALYZE…")
        connection.execute("ANALYZE")
        connection.commit()
        _log(f"  已完成，数据库 {temp_path.stat().st_size / 1048576:.0f} MB")
    finally:
        connection.close()

    if db_path.exists():
        db_path.unlink()
    os.replace(temp_path, db_path)
    return meta_rows


def _insert_csv(connection, path: Path, table: str, year: int, slug: str, province: str,
                universities: dict, majors: dict) -> int:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.reader(handle)
        try:
            header = [column.strip() for column in next(reader)]
        except StopIteration:
            return 0
        if not header:
            return 0
        index = {name: position for position, name in enumerate(header)}

        def get(row, name):
            position = index.get(name)
            if position is None or position >= len(row):
                return None
            return row[position]

        batch: list[tuple] = []
        inserted = 0
        sql = INSERT_SQL[table]
        for row in reader:
            if not row or not any(cell.strip() for cell in row):
                continue
            if table == "score-range":
                tuple_row = (
                    province, slug, year,
                    _text(get(row, "category")),
                    _text(get(row, "batch")),
                    _int(get(row, "control_score")),
                    _int(get(row, "score")),
                    _text(get(row, "score_range")),
                    _int(get(row, "segment_count")),
                    _int(get(row, "cumulative_count")),
                    _text(get(row, "rank_range")),
                )
            elif table == "school-admission":
                name = _text(get(row, "university_name"))
                if not name:
                    continue
                nature = _text(get(row, "school_nature"))
                is_985 = _flag(get(row, "is_985"))
                is_211 = _flag(get(row, "is_211"))
                school_province = _text(get(row, "school_province"))
                tuple_row = (
                    province, slug, year,
                    _text(get(row, "category")),
                    _text(get(row, "batch")),
                    _code(get(row, "university_code")),
                    name,
                    _text(get(row, "major_group")),
                    _text(get(row, "subject_req")),
                    _int(get(row, "min_score")),
                    _int(get(row, "min_rank")),
                    _int(get(row, "control_score")),
                    _int(get(row, "score_diff")),
                    _int(get(row, "admit_count")),
                    school_province,
                    nature,
                    is_985,
                    is_211,
                )
                _remember_university(universities, name, school_province, nature, is_985, is_211, "school")
            elif table == "major-admission":
                name = _text(get(row, "university_name"))
                if not name:
                    continue
                nature = _text(get(row, "school_nature"))
                is_985 = _flag(get(row, "is_985"))
                is_211 = _flag(get(row, "is_211"))
                school_province = _text(get(row, "school_province"))
                major_name = _text(get(row, "major_name"))
                tuple_row = (
                    province, slug, year,
                    _text(get(row, "category")),
                    _text(get(row, "batch")),
                    _code(get(row, "university_code")),
                    name,
                    _text(get(row, "major_group")),
                    _code(get(row, "major_code")),
                    major_name,
                    _text(get(row, "major_note")),
                    _text(get(row, "subject_req")),
                    _int(get(row, "min_score")),
                    _int(get(row, "min_rank")),
                    _int(get(row, "max_score")),
                    _float(get(row, "avg_score")),
                    _int(get(row, "admit_count")),
                    school_province,
                    nature,
                    is_985,
                    is_211,
                )
                _remember_university(universities, name, school_province, nature, is_985, is_211, "major")
                if major_name:
                    item = majors.setdefault(major_name, [0, 0, 0])
                    item[2] += 1
            else:  # enrollment-plan
                name = _text(get(row, "university_name"))
                if not name:
                    continue
                plan_count = _int(get(row, "plan_count"))
                major_name = _text(get(row, "major_name"))
                tuple_row = (
                    province, slug, year,
                    _text(get(row, "batch")),
                    _text(get(row, "category")),
                    _code(get(row, "university_code")),
                    name,
                    _text(get(row, "major_group")),
                    _code(get(row, "major_code")),
                    major_name,
                    _text(get(row, "major_note")),
                    _text(get(row, "subject_req")),
                    plan_count,
                    _text(get(row, "duration")),
                    _float(get(row, "tuition")),
                )
                _remember_university(universities, name, None, None, 0, 0, "plan")
                if major_name:
                    item = majors.setdefault(major_name, [0, 0, 0])
                    item[0] += 1
                    item[1] += plan_count or 0
            batch.append(tuple_row)
            if len(batch) >= 5000:
                connection.executemany(sql, batch)
                inserted += len(batch)
                batch.clear()
        if batch:
            connection.executemany(sql, batch)
            inserted += len(batch)
    return inserted


def _remember_university(universities: dict, name: str, school_province, nature, is_985, is_211, kind: str):
    item = universities.get(name)
    if item is None:
        item = [None, None, 0, 0, 0, 0, 0]
        universities[name] = item
    if school_province and not item[0]:
        item[0] = school_province
    if nature and not item[1]:
        item[1] = nature
    item[2] = max(item[2], is_985)
    item[3] = max(item[3], is_211)
    if kind == "school":
        item[4] += 1
    elif kind == "major":
        item[5] += 1
    else:
        item[6] += 1


def _write_dimensions(connection, universities: dict, majors: dict) -> None:
    connection.executemany(
        "INSERT OR REPLACE INTO admission_university"
        " (name, school_province, school_nature, is_985, is_211, school_rows, major_rows, plan_rows)"
        " VALUES (?,?,?,?,?,?,?,?)",
        [
            (name, item[0], item[1], item[2], item[3], item[4], item[5], item[6])
            for name, item in universities.items()
        ],
    )
    connection.executemany(
        "INSERT OR REPLACE INTO admission_major (name, plan_rows, plan_people, admission_rows)"
        " VALUES (?,?,?,?)",
        [(name, item[0], item[1], item[2]) for name, item in majors.items()],
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="导入 GaokaoCompass 数据到 admission.sqlite3")
    parser.add_argument("--data-dir", default=str(DEFAULT_DATA_DIR), help="应用数据目录（默认取 STUDY_DATA_DIR 或 ./data）")
    parser.add_argument("--cache-dir", default="", help="原始 CSV 缓存目录（默认 <data-dir>/.admission-cache）")
    parser.add_argument("--db", default="", help="输出数据库路径（默认 <data-dir>/admission.sqlite3）")
    parser.add_argument("--workers", type=int, default=8, help="下载并发数（默认 8）")
    parser.add_argument("--skip-download", action="store_true", help="跳过下载，只用已有缓存")
    parser.add_argument("--prune-cache", action="store_true", help="建库成功后删除原始 CSV 缓存")
    args = parser.parse_args(argv)

    data_dir = Path(args.data_dir).resolve()
    cache_dir = Path(args.cache_dir).resolve() if args.cache_dir else data_dir / ".admission-cache"
    db_path = Path(args.db).resolve() if args.db else data_dir / "admission.sqlite3"
    data_dir.mkdir(parents=True, exist_ok=True)
    cache_dir.mkdir(parents=True, exist_ok=True)

    _log(f"数据集: {DATASET}")
    _log(f"数据目录: {data_dir}")
    _log(f"缓存目录: {cache_dir}")
    _log("读取 Hugging Face 文件清单…")
    tasks = fetch_file_list()
    csv_count = sum(1 for path, _ in tasks if path.endswith(".csv"))
    total_bytes = sum(size or 0 for _, size in tasks)
    _log(f"文件清单: {len(tasks)} 个（CSV {csv_count} 个，合计约 {total_bytes / 1048576:.0f} MB）")

    skipped: list[str] = []
    if args.skip_download:
        _log("跳过下载，使用已有缓存…")
        statuses = {}
    else:
        downloader = Downloader(cache_dir, max(1, args.workers))
        _log(f"开始下载（并发 {args.workers}）…")
        statuses = downloader.download(tasks)
        failed = [path for path, status in statuses.items() if str(status).startswith("error")]
        if failed:
            _log(f"有 {len(failed)} 个文件下载失败，其余继续导入。")
            skipped = [path for path in failed]
            _log("失败示例: " + ", ".join(failed[:5]))

    cached: list[Path] = []
    for path, _ in tasks:
        local = cache_dir / relative_data_path(path)
        if local.exists() and local.stat().st_size > 0:
            cached.append(local)
        else:
            skipped.append(path)
    _log(f"可用缓存文件: {len(cached)} 个")

    _log("开始建库（这一步按数据量约需数分钟到十几分钟）…")
    stats = build_database(cached, cache_dir, db_path, skipped=skipped)
    _log(f"建库完成: {db_path} ({db_path.stat().st_size / 1048576:.0f} MB)")
    for key in ("total_rows", "parsed_files", "failed_files", "years"):
        _log(f"  {key}: {stats.get(key)}")

    if args.prune_cache:
        _log("清理原始 CSV 缓存…")
        for path in sorted(cache_dir.rglob("*"), reverse=True):
            try:
                if path.is_file():
                    path.unlink()
                elif path.is_dir():
                    path.rmdir()
            except OSError:
                pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
