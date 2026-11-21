"""``scripts/cleanup_r3_records.py``（r3 旧学习记录预检清理工具）回归测试。

覆盖口径：
* dry-run 零修改、报告结构完整、示例只含主键/qid（不含隐私正文）；
* 只删除「仅 r3 可解析且 r5 无对应引用」的学习记录行；
* r5 可解析 / r5 来源引用 / r3 但被 r5 引用 / 未知来源 / 私人内容 / AI 产物全部保留；
* 未登记的 qid 关联表保守保留并告警；
* apply 前置备份（无备份拒绝、自动备份成功、指定备份可用）；
* 单事务 + 保留证据（非目标表与 r5 关联记录哈希不变）；
* 幂等（二次 apply 删除 0 行）；
* PostgreSQL 模式 fail-closed；缺 r3 快照 fail-closed。

全部使用 ``tmp_path`` 合成发布包与应用库，不触碰真实 ``data/app.sqlite3``。
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SECRET_ANSWER = "SECRET_ANSWER_TEXT_不应该出现在报告里"
SECRET_AI = "SECRET_AI_EXPLANATION"
SECRET_ISSUE = "SECRET_ISSUE_NOTE"
SECRET_PRIVATE_Q = "SECRET_PRIVATE_QUESTION_JSON"
SECRET_UPLOAD = "SECRET_UPLOAD_TEXT"


def _load_cleanup_module():
    spec = importlib.util.spec_from_file_location(
        "cleanup_r3_records_for_tests", ROOT / "scripts" / "cleanup_r3_records.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


cleanup = _load_cleanup_module()


# --------------------------------------------------------------------------- #
# 合成发布包 / 应用库
# --------------------------------------------------------------------------- #
def _make_bank(root: Path, version: str, rows) -> Path:
    """在 ``root/题库.db`` 建最小 questions 表（仅清理判定所需列）。"""
    root.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(root / "题库.db"))
    try:
        con.execute(
            "CREATE TABLE questions ("
            "qid TEXT PRIMARY KEY, question_id TEXT, source_question_id TEXT, "
            "bank_version TEXT)"
        )
        con.executemany(
            "INSERT INTO questions(qid, question_id, source_question_id, bank_version) "
            "VALUES (?, ?, ?, ?)",
            [(qid, qid, source, version) for qid, source in rows],
        )
        con.commit()
    finally:
        con.close()
    return root


R3_ROWS = (
    ("r3:only1", "q_a"),
    ("r3:only2", "q_b"),
    ("r3:only3", "q_c"),
    ("r3:ref", "rwk-shared"),
)
R5_ROWS = (
    ("r5:keep1", "rwk-1"),
    ("r5:keep2", "rwk-shared"),
)

APP_SCHEMA = """
CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT NOT NULL);
CREATE TABLE sessions (id INTEGER PRIMARY KEY, user_id INTEGER);
CREATE TABLE attempts (
    id INTEGER PRIMARY KEY, user_id INTEGER, qid TEXT NOT NULL,
    answer_text TEXT NOT NULL DEFAULT '', result TEXT NOT NULL, created_at TEXT
);
CREATE TABLE mastery (
    user_id INTEGER, qid TEXT NOT NULL, level TEXT NOT NULL, updated_at TEXT,
    PRIMARY KEY (user_id, qid)
);
CREATE TABLE wrong_questions (
    user_id INTEGER, qid TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '',
    next_review_at TEXT NOT NULL, added_at TEXT NOT NULL, PRIMARY KEY (user_id, qid)
);
CREATE TABLE question_metadata (qid TEXT PRIMARY KEY, difficulty TEXT, updated_at TEXT);
CREATE TABLE question_kp (qid TEXT, kp_id INTEGER, PRIMARY KEY (qid, kp_id));
CREATE TABLE question_method (qid TEXT, method_id INTEGER, PRIMARY KEY (qid, method_id));
CREATE TABLE ai_explanations (id INTEGER PRIMARY KEY, qid TEXT, content_json TEXT);
CREATE TABLE content_issues (id INTEGER PRIMARY KEY, qid TEXT, note TEXT);
CREATE TABLE content_isolations (qid TEXT PRIMARY KEY, reason TEXT);
CREATE TABLE pl_questions (id INTEGER PRIMARY KEY, user_id INTEGER, content_json TEXT);
CREATE TABLE ai_uploads (id INTEGER PRIMARY KEY, user_id INTEGER, text_content TEXT);
"""


def _make_app_db(data_dir: Path, *, extra_qid_table: bool = False) -> Path:
    data_dir.mkdir(parents=True, exist_ok=True)
    db_path = data_dir / "app.sqlite3"
    con = sqlite3.connect(str(db_path))
    try:
        con.executescript(APP_SCHEMA)
        if extra_qid_table:
            con.execute("CREATE TABLE future_qid_table (qid TEXT)")
            con.execute("INSERT INTO future_qid_table(qid) VALUES ('r3:only1')")
        con.execute("INSERT INTO users VALUES (1, '测试学生')")
        con.execute("INSERT INTO sessions VALUES (1, 1)")
        con.executemany(
            "INSERT INTO attempts(id, user_id, qid, answer_text, result, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [
                (1, 1, "r3:only1", SECRET_ANSWER, "incorrect", "2026-11-19T00:00:00+00:00"),
                (2, 1, "r5:keep1", "r5 answer", "correct", "2026-11-20T00:00:00+00:00"),
                (3, 1, "r3:ref", "r3 ref answer", "correct", "2026-11-19T00:00:00+00:00"),
                (4, 1, "ghost:1", "unknown answer", "incorrect", "2026-11-19T00:00:00+00:00"),
            ],
        )
        con.executemany(
            "INSERT INTO mastery VALUES (?, ?, ?, ?)",
            [
                (1, "r3:only2", "未掌握", "2026-11-19T00:00:00+00:00"),
                (1, "r5:keep2", "初步掌握", "2026-11-20T00:00:00+00:00"),
            ],
        )
        con.executemany(
            "INSERT INTO wrong_questions VALUES (?, ?, ?, ?, ?)",
            [
                (1, "r3:only1", "", "2026-11-20T00:00:00+00:00", "2026-11-19T00:00:00+00:00"),
                (1, "ghost:2", "", "2026-11-20T00:00:00+00:00", "2026-11-19T00:00:00+00:00"),
                (1, "r5:keep2", "", "2026-11-20T00:00:00+00:00", "2026-11-20T00:00:00+00:00"),
            ],
        )
        con.executemany(
            "INSERT INTO question_metadata VALUES (?, ?, ?)",
            [
                ("r3:only1", "困难", "2026-11-19T00:00:00+00:00"),
                ("r5:keep1", "简单", "2026-11-20T00:00:00+00:00"),
            ],
        )
        con.execute("INSERT INTO question_kp VALUES ('r3:only2', 1)")
        con.execute("INSERT INTO question_kp VALUES ('r5:keep1', 1)")
        con.execute("INSERT INTO question_method VALUES ('r3:only1', 1)")
        con.execute(
            "INSERT INTO ai_explanations(id, qid, content_json) VALUES (1, 'r3:only1', ?)",
            (SECRET_AI,),
        )
        con.execute(
            "INSERT INTO content_issues(id, qid, note) VALUES (1, 'r3:only1', ?)",
            (SECRET_ISSUE,),
        )
        con.execute(
            "INSERT INTO content_isolations(qid, reason) VALUES ('r3:only2', '复核')"
        )
        con.execute(
            "INSERT INTO pl_questions(id, user_id, content_json) VALUES (1, 1, ?)",
            (SECRET_PRIVATE_Q,),
        )
        con.execute(
            "INSERT INTO ai_uploads(id, user_id, text_content) VALUES (1, 1, ?)",
            (SECRET_UPLOAD,),
        )
        con.commit()
    finally:
        con.close()
    return db_path


@pytest.fixture
def env(tmp_path):
    bank_root = tmp_path / "题库"
    r3_dir = _make_bank(bank_root / "releases" / "bank-20261120-r3", "bank-20261120-r3", R3_ROWS)
    r5_dir = _make_bank(bank_root / "releases" / "bank-20261121-r5", "bank-20261121-r5", R5_ROWS)
    (bank_root / "releases" / "CURRENT").write_text("bank-20261121-r5\n", encoding="utf-8")
    data_dir = tmp_path / "appdata"
    db_path = _make_app_db(data_dir)
    report_path = tmp_path / "report.json"
    return {
        "tmp": tmp_path,
        "bank_root": bank_root,
        "r3": r3_dir,
        "r5": r5_dir,
        "data_dir": data_dir,
        "db": db_path,
        "report": report_path,
    }


def _base_args(env, *extra: str) -> list[str]:
    return [
        "--data-dir", str(env["data_dir"]),
        "--bank-root", str(env["bank_root"]),
        "--r3", str(env["r3"]),
        "--r5", str(env["r5"]),
        *extra,
    ]


def _run(env, *extra: str) -> int:
    return cleanup.main(_base_args(env, "--json", str(env["report"]), *extra))


def _read_report(env) -> dict:
    return json.loads(Path(env["report"]).read_text(encoding="utf-8"))


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _qids(db_path: Path, table: str) -> list[str]:
    con = sqlite3.connect(f"file:{Path(db_path).as_posix()}?mode=ro", uri=True)
    try:
        return [row[0] for row in con.execute(f'SELECT qid FROM "{table}" ORDER BY qid')]
    finally:
        con.close()


def _count(db_path: Path, table: str) -> int:
    con = sqlite3.connect(f"file:{Path(db_path).as_posix()}?mode=ro", uri=True)
    try:
        return con.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
    finally:
        con.close()


EXPECTED_DELETABLE_BY_TABLE = {
    "attempts": 1,
    "mastery": 1,
    "wrong_questions": 1,
    "question_metadata": 1,
    "question_kp": 1,
    "question_method": 1,
}


# --------------------------------------------------------------------------- #
# dry-run
# --------------------------------------------------------------------------- #
def test_dry_run_is_default_and_never_modifies_db(env):
    db_hash = _file_sha256(env["db"])
    assert _run(env) == 0  # 不传 --dry-run 也默认 dry-run
    assert _file_sha256(env["db"]) == db_hash
    report = _read_report(env)
    assert report["schema"] == cleanup.REPORT_SCHEMA
    assert report["mode"] == "dry-run" and report["dry_run"] is True
    assert report["status"] == "ok"
    assert report["apply"] == {"performed": False}
    assert report["app_db"]["unchanged_by_dry_run"] is True


def test_dry_run_report_classification_and_retention_evidence(env):
    assert _run(env, "--dry-run") == 0
    report = _read_report(env)
    totals = report["classification"]["totals"]
    assert totals["r3_only"] == 9
    assert totals["deletable_total"] == 6
    assert totals["kept_r3_only_review"] == 3
    assert totals["unknown_total"] == 2
    assert totals["r5"] == 5
    assert totals["r3_referenced_in_r5"] == 1
    assert report["classification"]["deletable_by_table"] == EXPECTED_DELETABLE_BY_TABLE

    by_table = {item["table"]: item for item in report["classification"]["tables"]}
    assert by_table["attempts"]["classes"] == {
        "r5": 1,
        "r5_ref": 0,
        "r3_referenced_in_r5": 1,
        "r3_only": 1,
        "unknown": 1,
    }
    # 示例只含主键与 qid，不含作答/反馈正文。
    sample = by_table["attempts"]["sample_keys"]["r3_only"][0]
    assert sample["qid"] == "r3:only1"
    assert sample["keys"] == {"id": 1}
    text = Path(env["report"]).read_text(encoding="utf-8")
    for secret in (SECRET_ANSWER, SECRET_AI, SECRET_ISSUE, SECRET_PRIVATE_Q, SECRET_UPLOAD):
        assert secret not in text

    private = {item["table"]: item["rows"] for item in report["private_preserved"]}
    assert private["pl_questions"] == 1
    assert private["ai_uploads"] == 1
    assert private["users"] == 1
    evidence = report["retention_evidence"]
    assert evidence["non_target_tables"]["pl_questions"]["rows"] == 1
    assert evidence["r5_linked"]["rows"] == 5
    assert len(evidence["overall_sha256"]) == 64
    assert report["banks"]["r3"]["version"] == "bank-20261120-r3"
    assert report["banks"]["r5"]["version"] == "bank-20261121-r5"
    assert report["banks"]["current_pointer"] == "bank-20261121-r5"
    assert report["recommendation"]["safe_to_apply"] is True
    assert report["recommendation"]["deletable_total"] == 6
    review_queue = report["classification"]["review_queue"]
    assert review_queue  # 未知来源与 r3 被 r5 引用记录都进复核队列
    review_qids = {item["qid"] for item in review_queue}
    assert {"ghost:1", "ghost:2", "r3:ref"} <= review_qids


def test_dry_run_with_unknown_qid_table_keeps_rows_and_warns(env):
    data_dir = env["tmp"] / "appdata_extra"
    db_path = _make_app_db(data_dir, extra_qid_table=True)
    db_hash = _file_sha256(db_path)
    args = _base_args(env)
    args[1] = str(data_dir)
    assert cleanup.main([*args, "--json", str(env["report"])]) == 0
    assert _file_sha256(db_path) == db_hash
    report = _read_report(env)
    warning_text = "\n".join(report["warnings"])
    assert "future_qid_table" in warning_text
    assert "未登记" in warning_text
    # 未登记表里的 r3_only 行不会进入可删除清单，但会被记入复核队列。
    assert "future_qid_table" not in report["classification"]["deletable_by_table"]
    entry = next(
        item for item in report["classification"]["review_queue"] if item["qid"] == "r3:only1"
    )
    assert "future_qid_table" in entry["tables"]


def test_bank_root_with_baseline_db_still_follows_releases_pointer(env):
    """题库根目录同时存在基线 题库.db 时，不能把根目录误判为发布包目录。"""
    (env["bank_root"] / "题库.db").write_bytes(b"")  # 模拟根目录基线库（不参与清理判定）
    args = [
        "--data-dir", str(env["data_dir"]),
        "--bank-root", str(env["bank_root"]),
        "--json", str(env["report"]),
    ]
    assert cleanup.main(args) == 0
    report = _read_report(env)
    assert report["status"] == "ok"
    assert report["banks"]["r3"]["version"] == "bank-20261120-r3"
    assert report["banks"]["r5"]["version"] == "bank-20261121-r5"
    assert report["classification"]["totals"]["deletable_total"] == 6


# --------------------------------------------------------------------------- #
# apply
# --------------------------------------------------------------------------- #
def test_apply_deletes_only_r3_learning_records_and_keeps_everything_else(env):
    assert _run(env, "--apply") == 0
    report = _read_report(env)
    assert report["status"] == "applied"
    apply_info = report["apply"]
    assert apply_info["performed"] is True
    assert apply_info["deleted_by_table"] == EXPECTED_DELETABLE_BY_TABLE
    assert apply_info["deleted_total"] == 6
    assert apply_info["kept_rows_by_table"] == {
        "attempts": 3,
        "mastery": 1,
        "wrong_questions": 2,
        "question_metadata": 1,
        "question_kp": 1,
        "question_method": 0,
    }
    # 自动备份：落在 <data-dir>/backups 下，是真实可读的 zip。
    backup = apply_info["backup"]
    assert backup["mode"] == "auto"
    assert Path(backup["path"]).is_file() and backup["size_bytes"] > 0
    # 保留证据不变。
    preservation = apply_info["preservation"]
    assert preservation["unchanged"] is True
    assert preservation["after"]["non_target_tables"]["pl_questions"]["rows"] == 1
    assert preservation["after"]["r5_linked"] == preservation["before"]["r5_linked"]
    assert apply_info["db_sha256_before"] != apply_info["db_sha256_after"]

    # 学习记录：只删 r3_only。
    assert _qids(env["db"], "attempts") == ["ghost:1", "r3:ref", "r5:keep1"]
    assert _qids(env["db"], "mastery") == ["r5:keep2"]
    assert _qids(env["db"], "wrong_questions") == ["ghost:2", "r5:keep2"]
    assert _qids(env["db"], "question_metadata") == ["r5:keep1"]
    assert _qids(env["db"], "question_kp") == ["r5:keep1"]
    assert _qids(env["db"], "question_method") == []
    # 用户/AI 产物与私人内容：一行不少。
    assert _count(env["db"], "ai_explanations") == 1
    assert _count(env["db"], "content_issues") == 1
    assert _count(env["db"], "content_isolations") == 1
    assert _count(env["db"], "pl_questions") == 1
    assert _count(env["db"], "ai_uploads") == 1
    assert _count(env["db"], "users") == 1
    assert _count(env["db"], "sessions") == 1


def test_apply_is_idempotent(env):
    assert _run(env, "--apply") == 0
    first = _read_report(env)
    first_hash = _file_sha256(env["db"])
    backups_after_first = sorted((env["data_dir"] / "backups").glob("*.zip"))
    assert len(backups_after_first) == 1

    assert _run(env, "--apply") == 0
    second = _read_report(env)
    assert second["status"] == "applied_noop"
    assert second["apply"]["deleted_total"] == 0
    assert second["apply"]["deleted_by_table"] == {}
    assert second["apply"]["backup"] is None  # 无删除不创建新备份
    assert _file_sha256(env["db"]) == first_hash
    assert len(sorted((env["data_dir"] / "backups").glob("*.zip"))) == 1
    assert first["apply"]["preservation"]["unchanged"] is True


def test_apply_refused_without_usable_backup_leaves_db_untouched(env):
    db_hash = _file_sha256(env["db"])
    missing = env["tmp"] / "no-such-backup.zip"
    ret = cleanup.main(
        _base_args(
            env, "--apply", "--backup", str(missing), "--json", str(env["report"])
        )
    )
    assert ret == 4
    assert _file_sha256(env["db"]) == db_hash
    report = _read_report(env)
    assert report["status"] == "apply_refused"
    assert report["apply"]["performed"] is False
    assert "不存在" in report["error"]
    assert _count(env["db"], "attempts") == 4
    assert not (env["data_dir"] / "backups").exists()


def test_apply_with_existing_verified_backup(env):
    from app import ops

    backup_info = ops.create_backup(data_dir=env["data_dir"])
    backup_path = Path(backup_info["path"])
    assert backup_path.is_file()
    ret = cleanup.main(
        _base_args(
            env,
            "--apply",
            "--backup", str(backup_path),
            "--verify-backup",
            "--json", str(env["report"]),
        )
    )
    assert ret == 0
    report = _read_report(env)
    assert report["status"] == "applied"
    assert report["apply"]["backup"]["mode"] == "provided"
    assert report["apply"]["backup"]["verified"]["ok"] is True
    assert report["apply"]["deleted_total"] == 6


# --------------------------------------------------------------------------- #
# fail-closed 分支
# --------------------------------------------------------------------------- #
def test_postgres_mode_fails_closed(env, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pass@127.0.0.1:5432/zhixu_test")
    db_hash = _file_sha256(env["db"])
    ret = cleanup.main(_base_args(env, "--apply", "--json", str(env["report"])))
    assert ret == 3
    assert _file_sha256(env["db"]) == db_hash
    report = _read_report(env)
    assert report["status"] == "unsupported_postgres_fail_closed"
    assert "PostgreSQL" in report["error"]
    assert _count(env["db"], "attempts") == 4
    assert not (env["data_dir"] / "backups").exists()


def test_missing_r3_snapshot_fails_closed(env):
    db_hash = _file_sha256(env["db"])
    args = [
        "--data-dir", str(env["data_dir"]),
        "--bank-root", str(env["bank_root"]),
        "--r3", str(env["tmp"] / "missing-r3"),
        "--r5", str(env["r5"]),
        "--apply",
        "--json", str(env["report"]),
    ]
    assert cleanup.main(args) == 1
    assert _file_sha256(env["db"]) == db_hash
    report = _read_report(env)
    assert report["status"] == "error"
    assert "r3" in report["error"]
    assert _count(env["db"], "attempts") == 4
    assert not (env["data_dir"] / "backups").exists()


def test_r3_pointer_must_be_old_r3_snapshot(env):
    """把 r5 误传成 --r3 时必须 fail-closed（缺少旧快照证据，不许删除）。"""
    db_hash = _file_sha256(env["db"])
    args = [
        "--data-dir", str(env["data_dir"]),
        "--bank-root", str(env["bank_root"]),
        "--r3", str(env["r5"]),
        "--r5", str(env["r5"]),
        "--apply",
        "--json", str(env["report"]),
    ]
    assert cleanup.main(args) == 1
    assert _file_sha256(env["db"]) == db_hash
    report = _read_report(env)
    assert report["status"] == "error"
    assert "r3" in report["error"]
    assert _count(env["db"], "attempts") == 4
    assert not (env["data_dir"] / "backups").exists()


def test_apply_without_usable_backup_does_not_auto_backup_when_db_layout_unexpected(env):
    """--db 指向非标准布局时，自动备份不可用 → 拒绝 apply 且不写库。"""
    custom = env["tmp"] / "elsewhere"
    custom.mkdir()
    custom_db = custom / "custom.sqlite3"
    custom_db.write_bytes(env["db"].read_bytes())
    db_hash = _file_sha256(custom_db)
    ret = cleanup.main(
        [
            "--db", str(custom_db),
            "--data-dir", str(custom),
            "--bank-root", str(env["bank_root"]),
            "--r3", str(env["r3"]),
            "--r5", str(env["r5"]),
            "--apply",
            "--json", str(env["report"]),
        ]
    )
    assert ret == 4
    assert _file_sha256(custom_db) == db_hash
    assert not (custom / "backups").exists()
