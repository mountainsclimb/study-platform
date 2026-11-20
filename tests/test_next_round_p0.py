"""下一轮 P0 回归：版本对齐、组级实时成员状态、素材公开引用、评测命令安全。"""
import json
import sqlite3
from pathlib import Path
from urllib.parse import quote

import pytest

from app import db, practice
from app import eval as app_eval


# ---------------------------------------------------------------------------
# 1. 构建号与题库版本对齐
# ---------------------------------------------------------------------------

def test_health_reports_build_and_bank_version(client):
    live = client.get("/api/health/live").json()
    assert live["status"] == "ok"
    assert live["build_id"] and len(live["build_id"]) >= 8
    assert live["version_label"].startswith(live["version"] + "+")
    ready = client.get("/api/health/ready").json()
    assert ready["build_id"] == live["build_id"]
    assert ready["bank_version"] == ready["checks"]["bank"]["bank_version"]
    # 就绪信息按「学习可用 / AI 队列可用」分别给出结论。
    assert ready["study_ready"] is True
    # 每个响应都带构建号，旧标签页可据此发现版本变化。
    assert client.get("/api/health").headers["x-zhixu-build"] == live["build_id"]


def test_bank_dir_follows_release_pointer(tmp_path, monkeypatch):
    bank = tmp_path / "bank"
    release = bank / "releases" / "bank-test-r9"
    release.mkdir(parents=True)
    (release / "题库.db").write_bytes(b"")
    # PowerShell 写出的指针可能带 BOM 与行尾空白，也必须能解析。
    (bank / "releases" / "CURRENT").write_text("\ufeffbank-test-r9\r\n", encoding="utf-8")
    monkeypatch.setattr(db, "BANK_SOURCE", db.BANK_SOURCE)
    original = db._resolve_bank_dir
    monkeypatch.setenv("STUDY_BANK_DIR", str(bank))
    resolved, source = db._resolve_bank_dir()
    assert resolved == release.resolve()
    assert source == "pointer:bank-test-r9"
    monkeypatch.delenv("STUDY_BANK_DIR", raising=False)
    assert db._resolve_bank_dir() is not None
    assert callable(original)


# ---------------------------------------------------------------------------
# 2. 组接口与题目详情使用同一份实时成员状态
# ---------------------------------------------------------------------------

def _snapshot_bank_or_skip():
    from app import practice as practice_mod
    with practice_mod._bank() as con:
        if "practice_status" not in practice_mod._bank_question_columns(con):
            pytest.skip("当前题库不是快照包，实时成员状态回归由快照模式覆盖")
        row = con.execute(
            "SELECT group_id FROM groups WHERE member_index_json LIKE '%\"in_bank\": true%' "
            "AND member_index_json LIKE '%,%' LIMIT 1"
        ).fetchone()
    if not row:
        pytest.skip("题库没有包含多个可作答成员的题组")
    return row[0]


def test_group_counts_follow_live_member_status(client, monkeypatch):
    group_id = _snapshot_bank_or_skip()
    url = "/api/groups/" + quote(group_id, safe="")
    before = client.get(url)
    if before.status_code == 409:
        pytest.skip("该题组公开内容待复核，已在数据侧隔离")
    assert before.status_code == 200, before.text
    members = before.json()["members"]
    practice_members = [m for m in members if m["in_bank"] and m.get("available")]
    if not practice_members:
        pytest.skip("该题组没有可作答成员")
    assert before.json()["n_practice"] == len(practice_members)

    # 管理员隔离其中一问后：组接口与题目详情都必须立刻把它从可作答里去掉。
    target = practice_members[-1]["qid"]
    isolated = client.post(
        f"/api/admin/questions/{quote(target, safe='')}/isolation",
        json={"active": True, "reason": "回归：临时隔离成员"},
    )
    assert isolated.status_code == 200, isolated.text
    try:
        after = client.get(url).json()
        after_members = {m["qid"]: m for m in after["members"] if m.get("qid")}
        assert after["n_practice"] == len(practice_members) - 1
        assert after_members[target]["available"] is False
        assert after_members[target]["practice_status"] == "quarantined"
        detail = client.get(f"/api/questions/{quote(target, safe='')}")
        assert detail.status_code == 409
    finally:
        client.post(
            f"/api/admin/questions/{quote(target, safe='')}/isolation",
            json={"active": False},
        )
    restored = client.get(url).json()
    assert restored["n_practice"] == len(practice_members)


def test_group_quality_gate_blocks_pua_and_bare_markers(client, monkeypatch):
    stored = {
        "material_text": "材料正文\ue023乱码",
        "parent_stem": "",
        "shared_blocks_json": "[]",
        "member_index_json": "[]",
    }
    issues = practice._group_public_issues(stored)
    assert any("私用区字符" in item for item in issues)
    stored = {
        "material_text": "材料正文 [公式3]",
        "parent_stem": "",
        "shared_blocks_json": "[]",
        "member_index_json": "[]",
    }
    assert any("未解析素材标记" in item for item in practice._group_public_issues(stored))
    # 只读成员里被剔除答案段后不应再触发门禁
    stored = {
        "material_text": "材料正文",
        "parent_stem": "",
        "shared_blocks_json": "[]",
        "member_index_json": json.dumps([{
            "in_bank": False, "qid": None, "source_question_id": "q_x",
            "blocks": [{"kind": "text", "text": "【解答】答案正文"}],
        }], ensure_ascii=False),
    }
    assert practice._group_public_issues(stored) == []


# ---------------------------------------------------------------------------
# 3. 素材：公开内容引用优先，答案引用仍受门禁
# ---------------------------------------------------------------------------

def test_public_ref_for_same_question_wins_over_gated_ref(client, monkeypatch):
    user_id = client.get("/api/auth/me").json()["id"]
    monkeypatch.setattr(practice, "_question_group_key", lambda qid: None)
    # 同一题同时有题干公开引用与答案解析引用：题干图必须能直接显示。
    both = {
        "asset_id": "both",
        "refs": [
            {"qid": "doc:7", "group_id": None, "field": "stem", "permission": "public"},
            {"qid": "doc:7", "group_id": None, "field": "solution", "permission": "after_attempt"},
        ],
    }
    practice._asset_authorized(both, "doc:7", user_id)
    # 只有答案引用时仍需作答
    only_answer = {
        "asset_id": "answer-only",
        "refs": [{"qid": "doc:8", "group_id": None, "field": "solution", "permission": "after_attempt"}],
    }
    from fastapi import HTTPException
    with pytest.raises(HTTPException):
        practice._asset_authorized(only_answer, "doc:8", user_id)


# ---------------------------------------------------------------------------
# 4. 评测命令不影响在线任务
# ---------------------------------------------------------------------------

def test_compare_command_does_not_touch_running_jobs(client, tmp_path):
    user_id = client.get("/api/auth/me").json()["id"]
    with db.connection() as con:
        cursor = con.execute(
            "INSERT INTO ai_jobs(user_id,task,status,stage,request_json,partial_content,cancelled,"
            "created_at,updated_at,dispatch_token) VALUES (?,?,?,?,?,'',0,?,?,?)",
            (user_id, "explain", "running", "outline",
             json.dumps({"task": "explain", "provider_id": 1, "model": "m"}),
             db.utcnow(), db.utcnow(), "token-x"),
        )
        job_id = cursor.lastrowid
    report_a = {"name": "a", "model": "m", "scale": {}, "metrics": {"total": 1, "single_accuracy": 50.0},
                "bank_version": "bank-x", "records": []}
    report_b = {"name": "b", "model": "m", "scale": {}, "metrics": {"total": 1, "single_accuracy": 60.0},
                "bank_version": "bank-x", "records": []}
    path_a = tmp_path / "a.json"
    path_b = tmp_path / "b.json"
    path_a.write_text(json.dumps(report_a), encoding="utf-8")
    path_b.write_text(json.dumps(report_b), encoding="utf-8")

    app_eval.main(["--compare", str(path_a), str(path_b)])

    with db.connection() as con:
        row = con.execute("SELECT status, updated_at FROM ai_jobs WHERE id=?", (job_id,)).fetchone()
    assert row["status"] == "running"


def test_compare_flags_bank_version_mismatch():
    report_a = {"name": "a", "metrics": {"total": 1}, "bank_version": "bank-r2", "records": []}
    report_b = {"name": "b", "metrics": {"total": 1}, "bank_version": "bank-r1", "records": []}
    diff = app_eval.compare_reports(report_a, report_b)
    assert diff["bank_mismatch"] is True
    assert diff["comparable"] is False
    assert diff["bank_version_a"] == "bank-r2"
    same = app_eval.compare_reports(report_a, report_a)
    assert same["bank_mismatch"] is False and same["comparable"] is True


def test_golden_v2_records_bank_version():
    path = Path(app_eval.DEFAULT_GOLDEN_V2)
    if not path.is_file():
        pytest.skip("未生成分层评测集")
    rows = app_eval.load_golden_dataset(path)
    assert rows
    versions = {row.get("bank_version") for row in rows}
    assert all(versions), "分层评测集必须记录题库版本"
    assert not any(str(version).startswith("sha256:") for version in versions), versions
