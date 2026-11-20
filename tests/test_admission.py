"""志愿查询接口：只读数据层、筛选回退、冲稳保分级与鉴权。

测试不依赖 1.35GB 真实数据，也不联网：用 ``scripts/import_admission.py`` 的建表
语句在临时目录里造一个小数据库，再通过 FastAPI 路由验证查询语义。
"""
from __future__ import annotations

import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]


def _load_importer():
    spec = importlib.util.spec_from_file_location(
        "import_admission_for_tests", ROOT / "scripts" / "import_admission.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def admission_db(isolated_db):
    """在隔离数据目录里生成一个包含 2 个院校/4 条投档线的小型只读库。"""
    importer = _load_importer()
    path = isolated_db / "admission.sqlite3"
    isolated_db.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    try:
        for statement in importer.SCHEMA_SQL:
            connection.execute(statement)
        connection.executemany(
            "INSERT INTO score_range (province, province_slug, year, category, batch, control_score,"
            " score, score_range, segment_count, cumulative_count, rank_range)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            [
                ("浙江", "zhejiang", 2024, "综合", "平行录取一段", 492, 601, "601", 900, 53000, "52960-53000"),
                ("浙江", "zhejiang", 2024, "综合", "平行录取一段", 492, 600, "600", 1005, 53964, "52960-53964"),
                ("浙江", "zhejiang", 2024, "综合", "平行录取一段", 492, 599, "599", 1100, 55000, "53965-55000"),
            ],
        )
        connection.executemany(
            "INSERT INTO school_admission (province, province_slug, year, category, batch, university_code,"
            " university_name, major_group, subject_req, min_score, min_rank, control_score, score_diff,"
            " admit_count, school_province, school_nature, is_985, is_211)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                ("浙江", "zhejiang", 2024, "综合", "平行录取一段", "0001", "浙江大学", None, "不限",
                 664, 200, 492, 172, 16, "浙江", "公办", 1, 1),
                ("浙江", "zhejiang", 2024, "综合", "平行录取一段", "0002", "浙江工业大学", None, "不限",
                 599, 13353, 492, 107, 2400, "浙江", "公办", 0, 0),
                ("浙江", "zhejiang", 2024, "综合", "平行录取一段", "0003", "浙江海洋大学", None, "不限",
                 601, 52095, 492, 109, 900, "浙江", "公办", 0, 0),
                ("浙江", "zhejiang", 2024, "综合", "平行录取一段", "0004", "浙江万里学院", None, "不限",
                 590, 70000, 492, 98, 1200, "浙江", "民办", 0, 0),
                ("浙江", "zhejiang", 2024, "综合", "平行录取一段", "0005", "宁波大学", None, "不限",
                 610, 40000, 492, 118, 1500, "浙江", "公办", 0, 0),
                ("浙江", "zhejiang", 2024, "综合", "平行录取一段", "0006", "浙江科技学院", None, "不限",
                 585, 75000, 492, 93, 800, "浙江", "公办", 0, 0),
            ],
        )
        connection.executemany(
            "INSERT INTO major_admission (province, province_slug, year, category, batch, university_code,"
            " university_name, major_group, major_code, major_name, major_note, subject_req, min_score,"
            " min_rank, max_score, avg_score, admit_count, school_province, school_nature, is_985, is_211)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                ("浙江", "zhejiang", 2024, "综合", "平行录取一段", "0001", "浙江大学", None, "01",
                 "计算机科学与技术", None, "不限", 680, 120, None, None, None, "浙江", "公办", 1, 1),
                ("浙江", "zhejiang", 2024, "综合", "平行录取一段", "0002", "浙江工业大学", None, "02",
                 "计算机科学与技术", None, "不限", 640, 8000, None, None, None, "浙江", "公办", 0, 0),
            ],
        )
        connection.executemany(
            "INSERT INTO enrollment_plan (province, province_slug, year, batch, category, university_code,"
            " university_name, major_group, major_code, major_name, major_note, subject_req, plan_count,"
            " duration, tuition) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                ("浙江", "zhejiang", 2024, "平行录取一段", "综合", "0001", "浙江大学", None, "01",
                 "计算机科学与技术", None, "不限", 10, "四年", 6900.0),
                ("浙江", "zhejiang", 2024, "平行录取一段", "综合", "0002", "浙江工业大学", None, "02",
                 "计算机科学与技术", None, "不限", 30, "四年", 5500.0),
            ],
        )
        connection.executemany(
            "INSERT INTO admission_university (name, school_province, school_nature, is_985, is_211,"
            " school_rows, major_rows, plan_rows) VALUES (?,?,?,?,?,?,?,?)",
            [
                ("浙江大学", "浙江", "公办", 1, 1, 1, 1, 1),
                ("浙江工业大学", "浙江", "公办", 0, 0, 1, 1, 1),
                ("浙江海洋大学", "浙江", "公办", 0, 0, 1, 0, 0),
                ("浙江万里学院", "浙江", "民办", 0, 0, 1, 0, 0),
                ("宁波大学", "浙江", "公办", 0, 0, 1, 0, 0),
                ("浙江科技学院", "浙江", "公办", 0, 0, 1, 0, 0),
            ],
        )
        connection.executemany(
            "INSERT INTO admission_major (name, plan_rows, plan_people, admission_rows) VALUES (?,?,?,?)",
            [("计算机科学与技术", 2, 40, 2)],
        )
        connection.executemany(
            "INSERT INTO admission_coverage (province_slug, year, table_name, rows) VALUES (?,?,?,?)",
            [
                ("zhejiang", 2024, "score-range", 3),
                ("zhejiang", 2024, "school-admission", 4),
                ("zhejiang", 2024, "major-admission", 2),
                ("zhejiang", 2024, "enrollment-plan", 2),
            ],
        )
        connection.execute(
            "INSERT INTO admission_overview (province_slug, year, payload) VALUES (?,?,?)",
            (
                "zhejiang",
                2024,
                json.dumps({
                    "exam_overview": {"total_candidates": 281057},
                    "university_stats": {"total": 4, "is_985": 1, "is_211": 1, "public": 3, "private": 1},
                    "enrollment_stats": {"total_plan": 40, "university_count": 4, "major_count": 1},
                }, ensure_ascii=False),
            ),
        )
        meta = {
            "schema_version": "1",
            "dataset": "choucsan/Gaokao-Compass-11M",
            "source_url": "https://huggingface.co/datasets/choucsan/Gaokao-Compass-11M",
            "license": "MIT",
            "imported_at": "2026-11-20T00:00:00Z",
            "years": json.dumps([2024]),
            "provinces": json.dumps(["zhejiang"]),
            "total_rows": "11",
            "rows_score-range": "3",
            "rows_school-admission": "4",
            "rows_major-admission": "2",
            "rows_enrollment-plan": "2",
            "overview_files": "1",
        }
        connection.executemany(
            "INSERT INTO admission_meta (key, value) VALUES (?,?)", list(meta.items())
        )
        connection.commit()
    finally:
        connection.close()
    return path


def test_admission_requires_login(client):
    from app.main import app

    anonymous = TestClient(app)
    try:
        response = anonymous.get("/api/admission/meta")
        assert response.status_code == 401
    finally:
        anonymous.close()


def test_admission_meta_and_filters(client, admission_db):
    meta = client.get("/api/admission/meta")
    assert meta.status_code == 200, meta.text
    body = meta.json()
    assert body["ready"] is True
    assert body["years"] == [2024]
    assert body["provinces"] == [{"slug": "zhejiang", "name": "浙江"}]
    assert body["rows"]["score-range"] == 3
    assert body["total_rows"] == 11

    filters = client.get("/api/admission/filters", params={"province": "zhejiang", "year": 2024})
    assert filters.status_code == 200
    payload = filters.json()
    assert payload["categories"] == ["综合"]
    assert payload["rank_supported"] is True
    assert payload["control_score_supported"] is True
    assert payload["plan_supported"] is True
    assert payload["category_available"] is True
    assert payload["counts"]["school-admission"] == 4


def test_admission_score_rank_exact_and_nearest(client, admission_db):
    exact = client.get("/api/admission/score-rank", params={
        "province": "zhejiang", "year": 2024, "category": "综合", "score": 600,
    })
    assert exact.status_code == 200, exact.text
    body = exact.json()
    assert body["exact"] is True
    assert body["cumulative_count"] == 53964
    assert body["rank_range"] == "52960-53964"
    assert body["control_score"] == 492
    assert len(body["distribution"]) == 3
    assert body["total"] == 55000

    nearest = client.get("/api/admission/score-rank", params={
        "province": "zhejiang", "year": 2024, "category": "综合", "score": 650,
    })
    assert nearest.status_code == 200
    assert nearest.json()["exact"] is False
    assert nearest.json()["nearest_score"] == 601


def test_admission_schools_score_bands(client, admission_db):
    response = client.get("/api/admission/schools", params={
        "province": "zhejiang", "year": 2024, "category": "综合",
        "score": 600, "band": 20, "page_size": 20,
    })
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["mode"] == "score"
    assert body["total"] == 5
    levels = {item["university_name"]: item["level"] for item in body["items"]}
    assert levels["宁波大学"] == "冲"          # 610 分，高于考生分数
    assert levels["浙江海洋大学"] == "冲"      # 601 分，高 1 分
    assert levels["浙江工业大学"] == "稳"      # 599 分，低 1 分
    assert levels["浙江万里学院"] == "稳"      # 590 分，低 10 分
    assert levels["浙江科技学院"] == "保"      # 585 分，低 15 分

    paged = client.get("/api/admission/schools", params={
        "province": "zhejiang", "year": 2024, "category": "综合",
        "score": 600, "band": 20, "page": 2, "page_size": 2,
    })
    assert paged.status_code == 200
    assert len(paged.json()["items"]) == 2


def test_admission_schools_rank_mode(client, admission_db):
    response = client.get("/api/admission/schools", params={
        "province": "zhejiang", "year": 2024, "category": "综合", "rank": 50000,
    })
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["mode"] == "rank"
    assert body["rank_supported"] is True
    levels = {item["university_name"]: item["level"] for item in body["items"]}
    assert levels["宁波大学"] == "冲"          # 40000 / 50000 = 0.8
    assert levels["浙江海洋大学"] == "稳"      # 52095 / 50000 ≈ 1.04
    assert levels["浙江万里学院"] == "保"      # 70000 / 50000 = 1.4
    assert levels["浙江科技学院"] == "保"      # 75000 / 50000 = 1.5


def test_admission_school_keyword_search(client, admission_db):
    response = client.get("/api/admission/schools", params={
        "province": "zhejiang", "year": 2024, "keyword": "浙江工",
    })
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["mode"] == "keyword"
    assert [item["university_name"] for item in body["items"]] == ["浙江工业大学"]


def test_admission_school_detail_and_trend(client, admission_db):
    response = client.get("/api/admission/school", params={
        "province": "zhejiang", "year": 2024, "name": "浙江大学",
    })
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["university"]["is_985"] == 1
    assert len(body["cutoffs"]) == 1
    assert body["cutoffs"][0]["min_score"] == 664
    assert body["majors"][0]["major_name"] == "计算机科学与技术"
    assert body["plans"][0]["plan_count"] == 10
    assert body["plan_summary"] == {"rows": 1, "people": 10}
    assert body["trend"] == [{"year": 2024, "min_score": 664, "min_rank": 200, "admit_count": 16}]

    trend = client.get("/api/admission/trend", params={"province": "zhejiang", "university": "浙江大学"})
    assert trend.status_code == 200
    assert trend.json()["items"][0]["min_score"] == 664


def test_admission_plans_and_majors_search(client, admission_db):
    plans = client.get("/api/admission/plans", params={
        "province": "zhejiang", "year": 2024, "keyword": "计算机",
    })
    assert plans.status_code == 200, plans.text
    body = plans.json()
    assert body["total"] == 2
    assert body["total_people"] == 40

    majors = client.get("/api/admission/majors", params={
        "province": "zhejiang", "year": 2024, "keyword": "计算机",
    })
    assert majors.status_code == 200
    assert majors.json()["total"] == 2

    missing = client.get("/api/admission/plans", params={
        "province": "zhejiang", "year": 2024, "keyword": "不存在的专业",
    })
    assert missing.status_code == 200
    assert missing.json()["total"] == 0


def test_admission_suggest(client, admission_db):
    response = client.get("/api/admission/suggest", params={"q": "浙江"})
    assert response.status_code == 200
    body = response.json()
    names = [item["name"] for item in body["universities"]]
    assert names[0] == "浙江大学"
    assert "浙江工业大学" in names
    assert body["majors"] == []

    major_suggest = client.get("/api/admission/suggest", params={"q": "计算机"})
    assert major_suggest.status_code == 200
    assert major_suggest.json()["majors"][0]["name"] == "计算机科学与技术"


def test_admission_missing_database_returns_503(client, admission_db, monkeypatch, tmp_path):
    from app import admission

    monkeypatch.setattr(admission, "_db_path", lambda: tmp_path / "missing.sqlite3")

    meta = client.get("/api/admission/meta")
    assert meta.status_code == 200
    assert meta.json()["ready"] is False

    response = client.get("/api/admission/score-rank", params={
        "province": "zhejiang", "year": 2024, "category": "综合", "score": 600,
    })
    assert response.status_code == 503
    assert "import_admission" in response.json()["detail"]
