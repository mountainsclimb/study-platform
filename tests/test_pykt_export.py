"""pyKT 离线评估数据协议与规则基线测试（``scripts/integration/pykt_export.py``）。

用临时 SQLite 造最小应用库表（users / knowledge_points / question_kp / questions /
study_evidence），验证：

1. :func:`export_dataset`：JSONL 行数/字段/排序/版本回退/知识点映射，以及**去标识化**
   （不含 username / 原始 user_id / 题干答案）；``pending`` / ``unknown`` / 自评 / 旧版本 /
   孤儿用户事件被跳过；同一盐重复导出逐字节一致；源库只读（哈希 + mtime 不变）。
2. :func:`evaluate_thresholds`：数据不足不达标、调小阈值后达标、缺明细时如实标记缺失。
3. :func:`baseline_metrics`：数值在 0..1、PFA-lite 只用时间顺序历史（固定最小历史下的
   预期值）、输入顺序无关且确定性。
4. CLI：缺盐返回 2 且不写文件；带 ``--salt``/env 盐导出返回 0，报告含门控与基线。

不联网、不读真实业务库、不训练模型。
"""
import hashlib
import json
from pathlib import Path
import sqlite3

import pytest

from scripts.integration import pykt_export


# ---------------------------------------------------------------------------
# 临时应用库
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE users (
    id INTEGER PRIMARY KEY,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'student',
    created_at TEXT NOT NULL
);
CREATE TABLE knowledge_points (
    id INTEGER PRIMARY KEY,
    subject TEXT NOT NULL,
    name TEXT NOT NULL
);
CREATE TABLE question_kp (
    qid TEXT NOT NULL,
    kp_id INTEGER NOT NULL,
    weight REAL NOT NULL DEFAULT 1.0,
    PRIMARY KEY (qid, kp_id)
);
CREATE TABLE questions (
    qid TEXT PRIMARY KEY,
    bank_version TEXT,
    content_revision TEXT
);
CREATE TABLE study_evidence (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL,
    event_key TEXT NOT NULL,
    kind TEXT NOT NULL,
    question_ref TEXT,
    record_kind TEXT NOT NULL,
    record_id INTEGER,
    item_key TEXT,
    verdict TEXT NOT NULL,
    grade_source TEXT NOT NULL,
    confidence TEXT NOT NULL,
    prompt_state TEXT NOT NULL,
    reveal_state TEXT NOT NULL,
    self_report TEXT NOT NULL,
    evidence_version INTEGER NOT NULL DEFAULT 1,
    superseded_by INTEGER,
    valid INTEGER NOT NULL DEFAULT 1,
    occurred_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    meta_json TEXT,
    UNIQUE(user_id, event_key)
);
"""

_USERS = (
    (1, "alice-zhixu", "hash-a", "student", "2026-10-01T00:00:00+00:00"),
    (2, "bob-secret-name", "hash-b", "student", "2026-10-01T00:00:00+00:00"),
)
_KPS = ((101, "数学", "函数"), (102, "数学", "导数"))
_QUESTION_KP = (("q1", 101), ("q1", 102), ("q2", 101))
_QUESTIONS = (
    ("q1", "r5-20261121", "c3"),   # bank_version 优先
    ("q2", "", "c9"),              # bank_version 为空 → content_revision
)
# (user_id, event_key, kind, question_ref, verdict, grade_source, confidence,
#  prompt_state, reveal_state, self_report, evidence_version, valid, occurred_at)
_EVIDENCE = (
    (1, "e1", "attempt", "q1", "correct", "reference", "verified",
     "none", "before", "independent", 1, 1, "2026-11-01T08:00:00+00:00"),
    (1, "e2", "attempt", "q2", "incorrect", "ai_cross_verified", "verified",
     "hint", "after", "assisted", 1, 1, "2026-11-02T08:00:00+00:00"),
    (1, "e3", "attempt", "q1", "correct", "manual", "verified",
     "none", "before", "independent", 2, 1, "2026-11-03T08:00:00+00:00"),
    (2, "e4", "paper_attempt", "q1", "incorrect", "reference", "verified",
     "step", "after", "unknown", 1, 1, "2026-11-04T08:00:00+00:00"),
    (2, "e5", "review_rating", "q2", "correct", "reference", "verified",
     "unknown", "unknown", "none", 1, 1, "2026-11-05T08:00:00+00:00"),
    (1, "e6", "attempt", "q3", "correct", "reference", "verified",
     "none", "before", "none", 1, 1, "2026-11-06T08:00:00+00:00"),
    # 以下 4 条事件应被跳过（e10 因 valid=0 根本不进入扫描窗口）
    (1, "e7", "attempt", "q1", "pending", "pending", "pending",
     "none", "unknown", "unknown", 1, 1, "2026-11-02T09:00:00+00:00"),
    (1, "e8", "self_report", "q2", "correct", "self", "unknown",
     "none", "before", "independent", 1, 1, "2026-11-02T10:00:00+00:00"),
    (9, "e9", "attempt", "q1", "correct", "reference", "verified",
     "none", "before", "none", 1, 1, "2026-11-01T09:00:00+00:00"),
    (2, "e11", "self_report", "q2", "incorrect", "manual", "verified",
     "none", "before", "assisted", 1, 1, "2026-11-02T11:00:00+00:00"),
    # valid=0 的旧版本（重判前）不进入扫描窗口，也不导出
    (2, "e10", "attempt", "q1", "correct", "reference", "verified",
     "none", "before", "none", 2, 0, "2026-11-07T08:00:00+00:00"),
)


def _create_app_db(path: Path) -> Path:
    con = sqlite3.connect(path)
    try:
        con.executescript(_SCHEMA)
        con.executemany(
            "INSERT INTO users(id, username, password_hash, role, created_at) VALUES (?,?,?,?,?)",
            _USERS,
        )
        con.executemany(
            "INSERT INTO knowledge_points(id, subject, name) VALUES (?,?,?)", _KPS
        )
        con.executemany(
            "INSERT INTO question_kp(qid, kp_id) VALUES (?,?)", _QUESTION_KP
        )
        con.executemany(
            "INSERT INTO questions(qid, bank_version, content_revision) VALUES (?,?,?)",
            _QUESTIONS,
        )
        con.executemany(
            "INSERT INTO study_evidence(user_id, event_key, kind, question_ref, record_kind, "
            "verdict, grade_source, confidence, prompt_state, reveal_state, self_report, "
            "evidence_version, valid, occurred_at, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                (row[0], row[1], row[2], row[3], row[2], row[4], row[5], row[6],
                 row[7], row[8], row[9], row[10], row[11], row[12], row[12])
                for row in _EVIDENCE
            ],
        )
        con.commit()
    finally:
        con.close()
    return path


@pytest.fixture
def app_db(tmp_path):
    return _create_app_db(tmp_path / "app.sqlite3")


@pytest.fixture
def exported(app_db, tmp_path):
    out = tmp_path / "pykt-dataset.jsonl"
    stats = pykt_export.export_dataset(app_db, out, salt="unit-test-salt")
    return stats, out


def _hash(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


# ---------------------------------------------------------------------------
# 导出：内容 / 去标识化 / 排序
# ---------------------------------------------------------------------------

def test_export_dataset_fields_labels_and_deidentification(app_db, tmp_path):
    out = tmp_path / "dataset" / "pykt.jsonl"
    stats = pykt_export.export_dataset(app_db, out, salt="unit-test-salt")

    assert out.is_file()
    assert stats["rows"] == 6
    assert stats["users"] == 2
    assert stats["items"] == 3
    assert stats["kc_count"] == 2
    assert stats["skipped"] == 4
    assert stats["scanned"] == 10
    assert stats["label_coverage"] == pytest.approx(0.6)
    assert stats["label_positive_rate"] == pytest.approx(4 / 6)
    assert stats["days_span"] == pytest.approx(5.0)
    assert stats["out_path"] == str(out)
    assert sum(stats["skip_reasons"].values()) == 4

    text = out.read_text(encoding="utf-8")
    lines = [json.loads(line) for line in text.splitlines() if line.strip()]
    assert len(lines) == 6
    for row in lines:
        assert set(row) == set(pykt_export.DATASET_FIELDS)

    # 去标识化：不含原始用户名、原始题目引用之外的题干/答案痕迹；用户键为哈希。
    assert "alice-zhixu" not in text
    assert "bob-secret-name" not in text
    assert "题干" not in text and "参考答案" not in text
    key_of = pykt_export.user_key_of
    assert {row["user_key"] for row in lines} == {
        key_of("unit-test-salt", 1), key_of("unit-test-salt", 2),
    }
    for row in lines:
        assert row["user_key"] != "1" and row["user_key"] != "2"
        assert len(row["user_key"]) == 16
        assert row["label"] in (0, 1)
        assert isinstance(row["kc_keys"], list)
        assert row["item_key"].startswith("q")

    # 关键一行逐字段核对（含证据版本、提示/揭晓/自评口径）。
    user_one = key_of("unit-test-salt", 1)
    row = next(
        item for item in lines
        if item["user_key"] == user_one and item["item_key"] == "q1"
        and item["evidence_version"] == 2
    )
    assert row == {
        "user_key": user_one,
        "item_key": "q1",
        "item_version": "r5-20261121",
        "kc_keys": [101, 102],
        "timestamp": "2026-11-03T08:00:00+00:00",
        "label": 1,
        "source_key": "manual",
        "prompt_state": "none",
        "reveal_state": "before",
        "self_report": "independent",
        "evidence_version": 2,
    }
    assert row["item_version"] == "r5-20261121"
    # 版本回退：bank_version 为空时取 content_revision；题目无版本信息时为空串。
    assert {item["item_version"] for item in lines if item["item_key"] == "q2"} == {"c9"}
    assert {item["item_version"] for item in lines if item["item_key"] == "q3"} == {""}
    # 无知识点关联的题如实给空列表，不伪造 KC。
    assert all(item["kc_keys"] == [] for item in lines if item["item_key"] == "q3")

    # 排序稳定：按 (user_key, timestamp, item_key, evidence_version) 升序。
    assert lines == sorted(
        lines,
        key=lambda item: (
            item["user_key"], item["timestamp"], item["item_key"], item["evidence_version"],
        ),
    )
    # per_user / per_kc 是去标识化明细，供门控使用（q1/q2 都关联 kp 101）。
    assert stats["per_user"][user_one] == 4
    assert stats["per_kc"]["101"] == 5 and stats["per_kc"]["102"] == 3


def test_export_dataset_skips_pending_unknown_self_report_and_old_versions(exported):
    stats, out = exported
    lines = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    # e7=待判、e8/e11=自评、e9=孤儿用户 被跳过；e10=valid=0 旧版本不进入扫描窗口。
    assert stats["skip_reasons"]["非已判对错（pending/unknown）"] == 1
    assert stats["skip_reasons"]["判分未核验"] == 1
    assert stats["skip_reasons"]["学生自评事件"] == 1
    assert stats["skip_reasons"]["用户不存在"] == 1
    labels = sorted(row["label"] for row in lines)
    assert labels == [0, 0, 1, 1, 1, 1]


def test_export_dataset_is_deterministic_and_salt_sensitive(app_db, tmp_path):
    first = tmp_path / "a.jsonl"
    second = tmp_path / "b.jsonl"
    other = tmp_path / "c.jsonl"
    stats_a = pykt_export.export_dataset(app_db, first, salt="salt-one")
    stats_b = pykt_export.export_dataset(app_db, second, salt="salt-one")
    stats_c = pykt_export.export_dataset(app_db, other, salt="salt-two")

    assert first.read_bytes() == second.read_bytes()
    comparable_a = {key: value for key, value in stats_a.items() if key != "out_path"}
    comparable_b = {key: value for key, value in stats_b.items() if key != "out_path"}
    assert comparable_a == comparable_b
    assert stats_c["rows"] == stats_a["rows"]
    assert set(stats_c["per_user"]) != set(stats_a["per_user"])
    # 同一盐可复现同一匿名键
    assert pykt_export.user_key_of("salt-one", 1) == pykt_export.user_key_of("salt-one", 1)
    assert pykt_export.user_key_of("salt-one", 1) != pykt_export.user_key_of("salt-two", 1)


def test_export_dataset_since_filter(app_db, tmp_path):
    out = tmp_path / "since.jsonl"
    stats = pykt_export.export_dataset(
        app_db, out, salt="salt", since="2026-11-03T00:00:00+00:00",
    )
    assert stats["rows"] == 4
    assert stats["scanned"] == 4
    assert stats["skipped"] == 0
    assert stats["label_coverage"] == pytest.approx(1.0)
    assert stats["per_user"] == {
        pykt_export.user_key_of("salt", 1): 2,
        pykt_export.user_key_of("salt", 2): 2,
    }
    lines = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    assert all(row["timestamp"] >= "2026-11-03T00:00:00+00:00" for row in lines)


def test_export_dataset_requires_salt_and_existing_db(app_db, tmp_path):
    with pytest.raises(pykt_export.ExportError):
        pykt_export.export_dataset(app_db, tmp_path / "x.jsonl", salt="   ")
    with pytest.raises(pykt_export.ExportError):
        pykt_export.export_dataset(tmp_path / "missing.sqlite3", tmp_path / "x.jsonl", salt="s")
    with pytest.raises(pykt_export.ExportError):
        pykt_export.export_dataset(
            app_db, tmp_path / "x.jsonl", salt="s", since="不是时间",
        )


def test_export_dataset_rejects_non_sqlite_file(tmp_path):
    bogus = tmp_path / "not-a-db.sqlite3"
    bogus.write_bytes(b"this is not a sqlite database")
    with pytest.raises(pykt_export.ExportError):
        pykt_export.export_dataset(bogus, tmp_path / "x.jsonl", salt="s")


def test_export_dataset_is_read_only(app_db, tmp_path):
    before_hash = _hash(app_db)
    before_stat = app_db.stat()
    out = tmp_path / "ro.jsonl"
    pykt_export.export_dataset(app_db, out, salt="readonly-salt")
    after_stat = app_db.stat()
    assert _hash(app_db) == before_hash
    assert after_stat.st_mtime_ns == before_stat.st_mtime_ns
    assert out.is_file()


# ---------------------------------------------------------------------------
# 数据达标门控
# ---------------------------------------------------------------------------

def test_evaluate_thresholds_fails_when_data_insufficient(exported):
    stats, _out = exported
    result = pykt_export.evaluate_thresholds(stats)
    assert result["approved"] is False
    gates = result["gates"]
    assert gates["user_count"]["ok"] is False
    assert gates["events"]["ok"] is False
    assert gates["per_user"]["ok"] is False
    assert gates["per_kc"]["ok"] is False
    assert gates["label_coverage"]["ok"] is False
    assert gates["label_coverage"]["value"] == pytest.approx(0.6)
    assert len(result["missing"]) == 5
    assert all(isinstance(item, str) and item for item in result["missing"])
    assert "用户数不足" in result["missing"][0]
    assert "pyKT" in result["note"]


def test_evaluate_thresholds_passes_with_lowered_thresholds(exported):
    stats, _out = exported
    result = pykt_export.evaluate_thresholds(
        stats,
        min_users=2,
        min_events_per_user=2,
        min_events=6,
        min_events_per_kc=3,
        min_label_coverage=0.5,
    )
    assert result["approved"] is True
    assert result["missing"] == []
    gates = result["gates"]
    assert gates["per_user"]["users_ok"] == 2
    assert gates["per_kc"]["kcs_ok"] == 2
    assert gates["label_coverage"]["ok"] is True


def test_evaluate_thresholds_reports_missing_details():
    result = pykt_export.evaluate_thresholds({"rows": 10, "users": 2, "kc_count": 1})
    assert result["approved"] is False
    assert any("per_user" in item for item in result["missing"])
    assert any("per_kc" in item for item in result["missing"])
    assert any("标签覆盖" in item for item in result["missing"])
    assert result["gates"]["per_user"]["users_ok"] == 0


# ---------------------------------------------------------------------------
# 规则基线
# ---------------------------------------------------------------------------

def test_baseline_metrics_values_are_bounded_and_deterministic(exported):
    _stats, out = exported
    rows = pykt_export.read_dataset(out)
    first = pykt_export.baseline_metrics(rows)
    second = pykt_export.baseline_metrics(list(reversed(rows)))
    assert first == second  # 内部按时间排序，与调用方输入顺序无关
    assert first["events"] == 6
    assert first["evaluated"] == 6
    assert first["accuracy_smoothed"] == pytest.approx((4 + 1) / (6 + 2))
    assert 0.0 <= first["accuracy_smoothed"] <= 1.0
    assert 0 <= first["pfa"]["predictions"] <= first["evaluated"]
    assert 0.0 <= first["pfa"]["brier"] <= 1.0
    assert 0.0 <= first["pfa"]["accuracy"] <= 1.0
    # 默认 min_history=2 时，只有 u1 的第三个事件（同 KC 已有 2 条历史）可预测：
    # 历史 (1 对 / 2 次) → p=(1+1)/(2+2)=0.5，标签 1，命中且 Brier=0.25。
    assert first["pfa"]["predictions"] == 1
    assert first["pfa"]["brier"] == pytest.approx(0.25)
    assert first["pfa"]["accuracy"] == pytest.approx(1.0)
    assert "BKT" in first["note"] and "DKT" in first["note"]


def test_baseline_metrics_handles_empty_and_unlabelled_rows():
    rows = [
        {"user_key": "u1", "item_key": "q1", "kc_keys": [], "label": 1,
         "timestamp": "2026-11-01T00:00:00+00:00", "evidence_version": 1},
        {"user_key": "u1", "item_key": "q2", "kc_keys": ["kp:1"], "label": "bad",
         "timestamp": "2026-11-02T00:00:00+00:00", "evidence_version": 1},
    ]
    result = pykt_export.baseline_metrics(rows)
    assert result["events"] == 2
    assert result["evaluated"] == 1
    assert result["accuracy_smoothed"] == pytest.approx(2 / 3)
    assert result["pfa"] == {"predictions": 0, "brier": 0.0, "accuracy": 0.0}
    assert "没有满足历史门槛" in result["note"]


def test_baseline_metrics_uses_only_past_history():
    """同一 (user, kc) 第 1 条不预测、第 2 条用第 1 条预测：若用未来信息，第 2 条会不同。"""
    rows = [
        {"user_key": "u", "item_key": "q1", "kc_keys": [7], "label": 1,
         "timestamp": "2026-11-01T00:00:00+00:00", "evidence_version": 1},
        {"user_key": "u", "item_key": "q2", "kc_keys": [7], "label": 0,
         "timestamp": "2026-11-02T00:00:00+00:00", "evidence_version": 1},
    ]
    result = pykt_export.baseline_metrics(rows, min_history=1)
    assert result["pfa"]["predictions"] == 1
    assert result["pfa"]["brier"] == pytest.approx((2 / 3) ** 2)
    # p=2/3 → 预测「正确」，实际 incorrect → 准确率 0
    assert result["pfa"]["accuracy"] == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def test_cli_without_salt_returns_2_and_writes_nothing(app_db, tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("STUDY_PYKT_SALT", raising=False)
    out = tmp_path / "no-salt.jsonl"
    report = tmp_path / "no-salt-report.json"
    code = pykt_export.main(
        ["--db", str(app_db), "--out", str(out), "--report", str(report)]
    )
    assert code == 2
    assert not out.exists()
    assert not report.exists()
    captured = capsys.readouterr()
    assert "去标识化盐" in captured.err


def test_cli_exports_report_and_gates(app_db, tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("STUDY_PYKT_SALT", raising=False)
    out = tmp_path / "cli.jsonl"
    report = tmp_path / "cli-report.json"
    code = pykt_export.main([
        "--db", str(app_db),
        "--out", str(out),
        "--report", str(report),
        "--salt", "cli-salt",
        "--min-users", "2",
        "--min-events-per-user", "2",
        "--min-events", "6",
        "--min-events-per-kc", "3",
        "--min-label-coverage", "0.5",
    ])
    assert code == 0
    assert out.is_file() and report.is_file()
    body = json.loads(report.read_text(encoding="utf-8"))
    assert body["dataset"]["rows"] == 6
    assert body["gates"]["approved"] is True
    assert body["evaluation_allowed"] is True
    assert body["gates"]["missing"] == []
    assert body["baseline"]["events"] == 6
    assert body["protocol_note"] and "study_evidence" in body["protocol_note"]
    print_text = capsys.readouterr()
    assert "pyKT 离线评估数据导出完成" in print_text.out
    assert "达标" in print_text.out
    assert "规则基线" in print_text.out


def test_cli_insufficient_data_still_exports_with_gate_failure(app_db, tmp_path, capsys):
    """退出码 0 = 导出成功；达标与否体现在报告（默认阈值下小样本必然未达标）。"""
    out = tmp_path / "insufficient.jsonl"
    report = tmp_path / "insufficient-report.json"
    code = pykt_export.main([
        "--db", str(app_db), "--out", str(out), "--report", str(report),
        "--salt", "cli-salt",
    ])
    assert code == 0
    body = json.loads(report.read_text(encoding="utf-8"))
    assert body["dataset"]["rows"] == 6
    assert body["gates"]["approved"] is False
    assert body["evaluation_allowed"] is False
    assert body["gates"]["missing"]
    assert "未达标" in capsys.readouterr().out


def test_cli_uses_env_salt(app_db, tmp_path, monkeypatch):
    monkeypatch.setenv("STUDY_PYKT_SALT", "env-salt-value")
    out = tmp_path / "env.jsonl"
    report = tmp_path / "env-report.json"
    code = pykt_export.main([
        "--db", str(app_db), "--out", str(out), "--report", str(report),
        "--min-users", "1", "--min-events-per-user", "1", "--min-events", "1",
        "--min-events-per-kc", "1", "--min-label-coverage", "0.1",
    ])
    assert code == 0
    assert out.is_file() and report.is_file()
    body = json.loads(report.read_text(encoding="utf-8"))
    assert body["dataset"]["rows"] == 6
    keys = {row["user_key"] for row in pykt_export.read_dataset(out)}
    assert keys == {
        pykt_export.user_key_of("env-salt-value", 1),
        pykt_export.user_key_of("env-salt-value", 2),
    }


def test_cli_missing_db_returns_2(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("STUDY_PYKT_SALT", "s")
    code = pykt_export.main([
        "--db", str(tmp_path / "missing.sqlite3"),
        "--out", str(tmp_path / "x.jsonl"),
        "--report", str(tmp_path / "x.json"),
    ])
    assert code == 2
    assert "未找到应用库文件" in capsys.readouterr().err
