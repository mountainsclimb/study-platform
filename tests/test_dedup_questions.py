"""题库近似去重脚本（scripts/dedup_questions.py）验收。

覆盖：
1. 题干规范化与只读读取；
2. 按学科/长度分桶的近似对识别（完全相同与轻微差异）；
3. 阈值、每题目上限与总对数上限；
4. 命令行输出 JSON/Markdown，且题库文件字节不变（只读边界）；
5. 题库根目录 + releases/CURRENT 指针解析。
"""
import hashlib
import json
from pathlib import Path
import sqlite3

import pytest

from scripts import dedup_questions as dedup


def make_bank(path: Path, rows) -> Path:
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE questions("
        "qid TEXT PRIMARY KEY, question_id TEXT, subject TEXT, year TEXT, "
        "paper_name TEXT, stem_plain TEXT, practice_status TEXT)"
    )
    con.executemany(
        "INSERT INTO questions(qid, question_id, subject, year, paper_name, stem_plain, practice_status)"
        " VALUES(?,?,?,?,?,?,?)",
        rows,
    )
    con.commit()
    con.close()
    return path


ROWS = [
    ("q1", "q1", "数学", "2024", "模拟一", "已知函数 f(x)=x^2+2x+1，求它的最小值。", "practice"),
    ("q2", "q2", "数学", "2024", "模拟二", "已知函数 f(x)=x^2+2x+1，求它的最小值", "practice"),
    ("q3", "q3", "数学", "2023", "模拟三", "已知函数 f(x)=x^2+2x+3，求它的最小值。", "practice"),
    ("q4", "q4", "语文", "2024", "模拟四", "下列对文中画线句子的理解，最恰当的一项是（ ）", "practice"),
    ("q5", "q5", "数学", "2024", "模拟五", "短", "practice"),
]


def test_compact_stem_normalizes_width_and_punctuation():
    assert dedup.compact_stem("  ＡＢＣ，你好！ ") == "abc你好"
    # NFKC 会把上标数字折叠为普通数字，便于跨输入法命中同一道题。
    assert dedup.compact_stem("x² + 2x = 1") == "x2+2x=1"


def test_load_questions_is_read_only_and_skips_short_stems(tmp_path):
    path = make_bank(tmp_path / "题库.db", ROWS)
    before = hashlib.sha256(path.read_bytes()).hexdigest()

    questions = dedup.load_questions(path, min_length=8)

    after = hashlib.sha256(path.read_bytes()).hexdigest()
    assert before == after, "去重脚本不得写入题库"
    assert [item["qid"] for item in questions] == ["q1", "q2", "q3", "q4"]
    assert all(item["compact"] for item in questions)
    # 题干里的空白与标点不参与比较，因此 q1/q2 应完全一致。
    assert questions[0]["compact"] == questions[1]["compact"]


def test_find_duplicate_pairs_finds_exact_and_near_duplicates(tmp_path):
    questions = dedup.load_questions(make_bank(tmp_path / "题库.db", ROWS), min_length=8)
    pairs = dedup.find_duplicate_pairs(questions, threshold=88)

    keys = {(item["left"]["qid"], item["right"]["qid"]) for item in pairs}
    assert ("q1", "q2") in keys
    assert ("q1", "q3") in keys
    assert all(item["score"] >= 88 for item in pairs)
    # 跨学科不比对。
    assert not any("q4" in key for key in keys)


def test_find_duplicate_pairs_respects_threshold_and_limits(tmp_path):
    questions = dedup.load_questions(make_bank(tmp_path / "题库.db", ROWS), min_length=8)

    exact = dedup.find_duplicate_pairs(questions, threshold=100)
    assert all(item["score"] == 100 for item in exact)
    assert any(
        (item["left"]["qid"], item["right"]["qid"]) == ("q1", "q2") for item in exact
    )
    only_one = dedup.find_duplicate_pairs(questions, threshold=88, max_pairs=1)
    assert len(only_one) == 1
    per_question = dedup.find_duplicate_pairs(questions, threshold=80, max_per_question=1)
    seen = set()
    for item in per_question:
        assert item["left"]["qid"] not in seen
        assert item["right"]["qid"] not in seen
        seen.update((item["left"]["qid"], item["right"]["qid"]))


def test_max_pairs_is_global_across_subjects(tmp_path):
    rows = [
        ("m1", "m1", "数学", "2024", "卷A", "已知函数 f(x)=x^2+1，求最小值。", "practice"),
        ("m2", "m2", "数学", "2024", "卷B", "已知函数 f(x)=x^2+1，求最小值", "practice"),
        ("c1", "c1", "语文", "2024", "卷C", "下列句子中没有语病的一项是（ ）", "practice"),
        ("c2", "c2", "语文", "2024", "卷D", "下列句子中没有语病的一项是", "practice"),
    ]
    questions = dedup.load_questions(make_bank(tmp_path / "题库.db", rows), min_length=8)
    assert len(dedup.find_duplicate_pairs(questions, threshold=90, max_pairs=1)) == 1
    assert len(dedup.find_duplicate_pairs(questions, threshold=90, max_pairs=2)) == 2


def test_main_writes_json_and_markdown_without_touching_bank(tmp_path):
    path = make_bank(tmp_path / "题库.db", ROWS)
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    out = tmp_path / "out"

    assert dedup.main([
        "--bank-dir", str(path),
        "--out", str(out),
        "--threshold", "88",
        "--min-length", "8",
    ]) == 0

    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
    payload = json.loads((out / "dedup-questions.json").read_text(encoding="utf-8"))
    assert payload["pair_count"] >= 1
    assert payload["total_questions"] == 4
    assert payload["pairs"][0]["similarity"] >= 88
    assert "content_isolations" in payload["notice"]
    markdown = (out / "dedup-questions.md").read_text(encoding="utf-8")
    assert "题库近似去重清单" in markdown
    assert "`q1`" in markdown


def test_resolve_bank_path_follows_current_pointer(tmp_path):
    release = tmp_path / "releases" / "bank-test"
    release.mkdir(parents=True)
    make_bank(release / "题库.db", ROWS)
    (tmp_path / "releases" / "CURRENT").write_text("bank-test\n", encoding="utf-8")

    assert dedup.resolve_bank_path(str(tmp_path)).resolve() == (release / "题库.db").resolve()
    assert dedup.resolve_bank_path(str(release / "题库.db")).resolve() == (release / "题库.db").resolve()


def test_resolve_bank_path_rejects_missing_bank(tmp_path):
    missing = tmp_path / "no-such"
    with pytest.raises(SystemExit) as error:
        dedup.load_questions(missing / "题库.db")
    assert "未找到题库" in str(error.value)
