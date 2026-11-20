"""检索索引（bank_leak_flags / bank_search_meta）与 PG 双引擎 SQL 回归。

覆盖优化方案：
  * P0「检索路径零 DDL（消除 PG ACCESS EXCLUSIVE 锁风暴）」；
  * P0「关键词检索命中 stem_compact GIN 索引、不再 COALESCE 全表扫」；
  * P1「检索 <50ms、catalog 缓存、seen_reviews 上限、stats 分批、LIKE 通配符转义」；
  * P1「_excluded_sql 兼容带前缀 group_id」「PG 路径缺 bank. 前缀」。
"""
import json
import os
import sqlite3
import time
from contextlib import contextmanager

import pytest

from app import db, practice
from bank_fixture import make_bank, question_row


def _clear_bank_caches():
    practice._sqlite_question_columns.cache_clear()
    practice._sqlite_group_columns.cache_clear()
    practice._source_groups.cache_clear()


@pytest.fixture(autouse=True)
def _reset_index_state():
    """每个用例前后清掉进程级索引状态，避免临时应用库/题库切换时互相污染。"""
    practice._verified_index_key = None
    practice._verified_fts_key = None
    practice.invalidate_catalog_cache()
    yield
    practice._verified_index_key = None
    practice._verified_fts_key = None
    practice.invalidate_catalog_cache()


def _real_choice_question():
    """基线题库里挑一道可作答的干净单选题（集成用例用）。"""
    with practice._bank() as con:
        qids = [r[0] for r in con.execute(
            "SELECT qid FROM questions WHERE objective=1 ORDER BY year DESC LIMIT 200"
        )]
    for qid in qids:
        question = practice.load_question(qid)
        blocking = [flag for flag in question["quality_flags"] if "参考答案待核验" not in flag]
        if (question["practice_allowed"] and not blocking and question["has_answer"]
                and question["type"] == "single_choice" and question["options"]):
            return question
    raise AssertionError("No suitable source question")


@pytest.fixture
def indexed_bank(tmp_path, monkeypatch):
    rows = [
        question_row("docA:1", "函数题目：混入答案的题干【答案】A"),
        question_row("docA:2", "函数单调性的判断方法"),
        question_row("docA:3", "集合运算与概率初步"),
        question_row(
            "docA:4", "第(1)问：请写出定义域", doc_id="docA",
            group_id="m_0001", group_role="sub", number_label="(1)",
        ),
        question_row(
            "docA:5", "上下文成员，仅供阅读", practice_status="context",
        ),
    ]
    groups = [{
        "group_id": "docA:m_0001", "doc_id": "docA", "subject": "数学", "year": 2024,
        "material_text": "材料：函数 的定义域为 R，值域为 [0, 2]。",
        "parent_stem": "父题：已知函数 f(x)=x+1", "shared_blocks_json": "[]",
        "member_index_json": "[]", "n_members": 1, "n_practice": 1,
        "bank_version": "bank-fixture-r1",
    }]
    bank = make_bank(tmp_path / "bank", rows, groups)
    monkeypatch.setattr(db, "BANK_DIR", bank)
    _clear_bank_caches()
    yield bank
    _clear_bank_caches()


def _flags(bank_dir):
    with db.connection(attach_bank=True) as con:
        return {
            row["qid"]: dict(row)
            for row in con.execute("SELECT qid, leak, excluded, has_solution FROM bank_leak_flags")
        }


def test_leak_flags_and_search_meta_are_precomputed(client, indexed_bank):
    # 首次检索时按题库摘要构建索引。
    response = client.get("/api/questions", params={"q": "函数", "page_size": 50})
    assert response.status_code == 200
    body = response.json()
    qids = {item["qid"] for item in body["items"]}
    # 泄密题（题干含【答案】）被索引过滤；材料里出现「函数」的题组小题可被 2 字短词命中。
    assert "docA:1" not in qids
    assert {"docA:2", "docA:4"} <= qids
    assert body["total"] == 2

    flags = _flags(indexed_bank)
    assert flags["docA:1"]["leak"] == 1
    assert flags["docA:2"]["leak"] == 0
    # context 成员已被隔离标记排除（与旧检索门禁同口径）。
    assert flags["docA:5"]["excluded"] == 1

    with db.connection(attach_bank=True) as con:
        meta = {row["qid"]: dict(row) for row in con.execute("SELECT * FROM bank_search_meta")}
    assert set(meta) == {"docA:1", "docA:2", "docA:3", "docA:4", "docA:5"}
    assert "函数" in meta["docA:4"]["material_text"]


def test_index_rebuilds_when_bank_content_changes(client, indexed_bank):
    assert client.get("/api/questions", params={"q": "函数"}).json()["total"] == 2
    # 篡改题库：把干净题改成泄密题（长度/内容变化 → 摘要变化 → 索引重建）。
    db_file = indexed_bank / "题库.db"
    con = sqlite3.connect(str(db_file))
    try:
        con.execute(
            "UPDATE questions SET stem_plain=? WHERE qid=?",
            ("函数单调性的判断方法【详解】答案混入", "docA:2"),
        )
        con.commit()
    finally:
        con.close()
    os.utime(db_file, (time.time() + 5, time.time() + 5))
    refreshed = client.get("/api/questions", params={"q": "函数"}).json()
    assert "docA:2" not in {item["qid"] for item in refreshed["items"]}
    assert _flags(indexed_bank)["docA:2"]["leak"] == 1


def test_missing_index_tables_are_recreated_by_startup_init(client, indexed_bank, monkeypatch):
    """检索索引表只在启动初始化重建；检索路径本身不再发 DDL（P0 PG 锁风暴修复）。"""
    assert client.get("/api/questions", params={"q": "函数"}).json()["total"] == 2
    with db.connection() as con:
        con.execute("DROP TABLE bank_leak_flags")
        con.execute("DROP TABLE bank_search_meta")
        con.execute("DELETE FROM bank_meta WHERE key LIKE 'bank_leak_sha256_%'")
    # 模拟进程重启：init_practice() 在启动阶段补齐表结构，随后由检索/预热重建数据行。
    monkeypatch.setattr(practice, "_verified_index_key", None)
    monkeypatch.setattr(practice, "_verified_fts_key", None)
    practice.init_practice()
    recreated = client.get("/api/questions", params={"q": "函数"})
    assert recreated.status_code == 200, recreated.text
    with db.connection() as con:
        tables = db.table_names(con)
        assert {"bank_leak_flags", "bank_search_meta"} <= tables
        assert con.execute("SELECT COUNT(*) FROM bank_leak_flags").fetchone()[0] == 5


def test_search_similar_questions_excludes_leak_flagged(client, indexed_bank):
    client.get("/api/questions", params={"q": "函数"})  # 触发索引构建
    results = practice.search_similar_questions("函数单调性的判断方法", limit=5)
    assert all(item["qid"] != "docA:1" for item in results)


def test_leak_flags_cover_group_parent_stem_and_shared_blocks(client, tmp_path, monkeypatch):
    """PG 分支曾漏检 parent_stem / shared_blocks_json：预计算后双引擎同口径。"""
    rows = [
        question_row("docB:1", "干净题干一", doc_id="docB", group_id="m_0001"),
        question_row("docB:2", "干净题干二", doc_id="docB", group_id="m_0002"),
    ]
    groups = [
        {
            "group_id": "docB:m_0001", "doc_id": "docB", "material_text": "材料一",
            "parent_stem": "父题【解析】答案混入", "shared_blocks_json": "[]",
            "member_index_json": "[]", "n_members": 1, "n_practice": 1,
        },
        {
            "group_id": "docB:m_0002", "doc_id": "docB", "material_text": "材料二",
            "parent_stem": "父题说明",
            "shared_blocks_json": json.dumps([{"kind": "text", "text": "共用块【答案】A"}], ensure_ascii=False),
            "member_index_json": "[]", "n_members": 1, "n_practice": 1,
        },
    ]
    bank = make_bank(tmp_path / "leak-group-bank", rows, groups)
    monkeypatch.setattr(db, "BANK_DIR", bank)
    _clear_bank_caches()
    try:
        listing = client.get("/api/questions", params={"page_size": 50}).json()
        assert listing["total"] == 0, listing
        flags = _flags(bank)
        assert flags["docB:1"]["leak"] == 1
        assert flags["docB:2"]["leak"] == 1
    finally:
        _clear_bank_caches()


# ---------------------------------------------------------------------------
# P1: _excluded_sql 兼容带 <doc>: 前缀的 group_id（R3 有 796 题）
# ---------------------------------------------------------------------------

def test_excluded_sql_matches_prefixed_and_bare_group_id(client, tmp_path, monkeypatch):
    """R3 有 796 题 group_id 已带 ``<doc>:`` 前缀：两种形态都要能匹配隔离题组。"""
    quarantined = "3597febaaede0d74:m_0001"
    assert quarantined in practice.QUARANTINED_GROUPS
    rows = [
        question_row("ok:1", "普通题", doc_id="c0ffee", group_id=None),
        # 带前缀形态（R3 新格式）
        question_row("3600aa:1", "隔离题组题（带前缀）",
                     doc_id="3597febaaede0d74", group_id="3597febaaede0d74:m_0001"),
        # 裸 group_id 形态（旧格式）
        question_row("3600aa:2", "隔离题组题（裸 id）",
                     doc_id="3597febaaede0d74", group_id="m_0001"),
        # 其他正常题组
        question_row("3600aa:3", "正常题组题", doc_id="c0ffee", group_id="m_0009"),
    ]
    bank = make_bank(tmp_path / "excluded-bank", rows)
    clause, values = practice._excluded_sql("q")
    # 两种形态都要出现在 SQL 里，且参数按两个投影各带一份。
    assert "q.group_id NOT IN" in clause
    assert "q.doc_id || ':' || q.group_id NOT IN" in clause
    assert len(values) >= len(practice.QUARANTINED_GROUPS) * 2

    # 检索与可练总数也必须同时排除两种形态（API 级回归）。
    monkeypatch.setattr(db, "BANK_DIR", bank)
    _clear_bank_caches()
    try:
        listing = client.get("/api/questions", params={"page_size": 50}).json()
        assert {item["qid"] for item in listing["items"]} == {"ok:1", "3600aa:3"}
        assert listing["total"] == 2
        catalog = client.get("/api/catalog").json()
        assert catalog["available_total"] == 2
    finally:
        _clear_bank_caches()


# ---------------------------------------------------------------------------
# P1: PostgreSQL 分支的 bank. 前缀与同口径过滤条件（不连真实 PG）
# ---------------------------------------------------------------------------

class _FakeCursor:
    def __init__(self, statements):
        self.statements = statements

    def fetchall(self):
        return []

    def fetchone(self):
        return None


class _FakeRaw:
    def __init__(self):
        self.statements = []

    def execute(self, statement, params=None):
        self.statements.append(statement)
        return _FakeCursor(self.statements)


def test_live_member_status_uses_bank_schema_prefix_in_postgres(monkeypatch):
    monkeypatch.setattr(db, "postgres_enabled", lambda: True)
    fake = _FakeRaw()
    con = db._Postgres(fake)
    monkeypatch.setattr(practice, "_bank_question_columns", lambda con: frozenset(
        {"qid", "practice_status", "grading_status"}))
    monkeypatch.setattr(practice, "_active_isolations", lambda force=False: {})
    status = practice._live_member_status(con, ["docA:1", "docA:2"])
    assert set(status) == {"docA:1", "docA:2"}
    sql = " ".join(fake.statements)
    assert "FROM bank.questions" in sql
    assert "practice_status" in sql and "grading_status" in sql
    # 不允许再出现未加 schema 的裸表名。
    assert "FROM questions" not in sql


def test_search_sql_postgres_uses_precomputed_flags_and_no_not_like(monkeypatch):
    monkeypatch.setattr(db, "postgres_enabled", lambda: True)
    monkeypatch.setattr(practice, "ensure_leak_flags", lambda con=None: True)
    monkeypatch.setattr(practice, "_active_isolations", lambda force=False: {})
    monkeypatch.setattr(practice, "_bank_question_columns", lambda con: frozenset({
        "qid", "stem_plain", "paper_name", "answer_text", "solution",
        "solution_blocks_json", "practice_status", "doc_id", "group_id",
    }))
    fake = _FakeRaw()
    con = db._Postgres(fake)
    join_sql, where_sql, params = practice._build_search_sql(con, 1, q="函数")
    # 泄密/隔离过滤已改为预计算标记：不再对 7 列拼 NOT LIKE。
    assert "bank_leak_flags" in where_sql
    assert "NOT LIKE" not in where_sql
    assert "f.leak=1" in where_sql and "f.excluded=1" in where_sql
    # PG 关键词检索必须直接命中 stem_compact（GIN trgm 索引列），不能被 COALESCE 绕开。
    assert "q.stem_compact ILIKE" in where_sql
    assert "COALESCE(q.stem_compact" not in where_sql
    assert "q.stem_plain ILIKE" not in where_sql
    assert "ILIKE" in where_sql
    assert params and join_sql == ""
    # search_similar_questions 复用同一条件模板，同样走索引列。
    assert "q.stem_compact ILIKE" in practice._PG_BANK_CONTAINS
    assert "COALESCE" not in practice._PG_BANK_CONTAINS

    # has_solution 走预计算列（含块级解析），不是只查旧 solution 字段。
    _, solution_where, _ = practice._build_search_sql(con, 1, has_solution=True)
    assert "f.has_solution=1" in solution_where


def test_bank_table_helper_prefixes_only_for_postgres(monkeypatch):
    monkeypatch.setattr(db, "postgres_enabled", lambda: False)
    assert practice._bank_table(object(), "questions") == "questions"
    monkeypatch.setattr(db, "postgres_enabled", lambda: True)
    assert practice._bank_table(db._Postgres(_FakeRaw()), "questions") == "bank.questions"


# ---------------------------------------------------------------------------
# P0: 稳态检索路径零 DDL / 只有题库摘要变化才重建
# ---------------------------------------------------------------------------

def test_search_path_issues_no_ddl_once_index_is_warm(client, indexed_bank, monkeypatch):
    assert client.get("/api/questions", params={"q": "函数"}).json()["total"] == 2
    ddl_calls, write_calls = [], []
    monkeypatch.setattr(
        practice, "_ensure_leak_flags_tables", lambda con: ddl_calls.append(1),
    )
    monkeypatch.setattr(
        practice, "_write_bank_index",
        lambda con, rows, digest: write_calls.append(digest),
    )
    for _ in range(3):
        response = client.get("/api/questions", params={"q": "函数"})
        assert response.status_code == 200
    assert response.json()["total"] == 2
    # 稳态：检索主事务内不再建表/补列，也不再重写索引数据（O(1) 快路径）。
    assert ddl_calls == []
    assert write_calls == []


def test_index_rebuilds_only_when_bank_digest_changes(client, indexed_bank, monkeypatch):
    assert client.get("/api/questions", params={"q": "函数"}).json()["total"] == 2
    write_calls = []
    original = practice._write_bank_index

    def counting_write(con, rows, digest):
        write_calls.append(digest)
        return original(con, rows, digest)

    monkeypatch.setattr(practice, "_write_bank_index", counting_write)
    for _ in range(3):
        assert client.get("/api/questions", params={"q": "函数"}).status_code == 200
    assert write_calls == [], "题库摘要未变不应重建索引"

    # 篡改题库 → 摘要变化 → 恰好重建一次（不是每次请求）。
    db_file = indexed_bank / "题库.db"
    con = sqlite3.connect(str(db_file))
    try:
        con.execute(
            "UPDATE questions SET stem_plain=? WHERE qid=?",
            ("函数单调性判断【答案】A 混入解析", "docA:3"),
        )
        con.commit()
    finally:
        con.close()
    os.utime(db_file, (time.time() + 5, time.time() + 5))
    first = client.get("/api/questions", params={"q": "函数"})
    second = client.get("/api/questions", params={"q": "函数"})
    assert first.status_code == second.status_code == 200
    assert "docA:3" not in {item["qid"] for item in second.json()["items"]}
    assert len(write_calls) == 1


def test_init_practice_prewarms_search_indexes(tmp_path, monkeypatch, isolated_db):
    """P1：FTS + 泄密标记在进程启动时批量构建，首个请求不再长阻塞。"""
    rows = [question_row("docW:1", "预热题库：函数的定义域")]
    bank = make_bank(tmp_path / "prewarm-bank", rows)
    monkeypatch.setattr(db, "BANK_DIR", bank)
    monkeypatch.setattr(practice, "_verified_index_key", None)
    monkeypatch.setattr(practice, "_verified_fts_key", None)
    monkeypatch.setenv("STUDY_INDEX_PREWARM", "1")
    _clear_bank_caches()
    try:
        db.init_db()
        practice.init_practice()
        with db.connection() as con:
            assert con.execute("SELECT COUNT(*) FROM bank_fts").fetchone()[0] == 1
            assert con.execute("SELECT COUNT(*) FROM bank_leak_flags").fetchone()[0] == 1
        assert practice._verified_index_key is not None
        assert practice._verified_fts_key is not None
        # 预热后可关闭：环境变量 0 时启动不再构建（单测套件默认值）。
        assert practice._prewarm_enabled() is True
    finally:
        _clear_bank_caches()


def test_high_value_indexes_exist(client):
    """P1 补建索引：sessions(expires_at)、auth_rate_events(at)、bank_leak_flags 部分索引。"""
    with db.connection() as con:
        rows = {
            row[0]: row[1]
            for row in con.execute("SELECT name, sql FROM sqlite_master WHERE type='index'")
        }
    assert {"idx_sessions_expires", "idx_auth_rate_at", "idx_bank_leak_flags_active"} <= set(rows)
    assert "WHERE leak=1 OR excluded=1" in (rows["idx_bank_leak_flags_active"] or "")


# ---------------------------------------------------------------------------
# P1: /api/catalog 进程内缓存、seen_reviews 上限、stats 分批、LIKE 转义
# ---------------------------------------------------------------------------

def test_catalog_is_cached_until_bank_digest_changes(client, tmp_path, monkeypatch):
    rows = [question_row("docC:1", "目录缓存题一"), question_row("docC:2", "目录缓存题二")]
    bank = make_bank(tmp_path / "catalog-bank", rows)
    monkeypatch.setattr(db, "BANK_DIR", bank)
    _clear_bank_caches()
    practice.invalidate_catalog_cache()
    calls = []
    original = practice._build_catalog

    def counting_build():
        calls.append(1)
        return original()

    monkeypatch.setattr(practice, "_build_catalog", counting_build)
    try:
        first = client.get("/api/catalog")
        second = client.get("/api/catalog")
        assert first.status_code == second.status_code == 200
        assert first.json() == second.json()
        assert first.json()["source_total"] == 2
        assert len(calls) == 1, "第二次 /api/catalog 必须命中进程内缓存，不查库"

        # 题库内容变化（摘要变化）→ 缓存自动失效并重建。
        con = sqlite3.connect(str(bank / "题库.db"))
        try:
            con.execute("UPDATE questions SET practice_status='context' WHERE qid='docC:2'")
            con.commit()
        finally:
            con.close()
        os.utime(bank / "题库.db", (time.time() + 5, time.time() + 5))
        third = client.get("/api/catalog").json()
        assert len(calls) == 2
        assert third["available_total"] == first.json()["available_total"] - 1
    finally:
        practice.invalidate_catalog_cache()
        _clear_bank_caches()


def test_seen_reviews_capped_before_building_in_clause(client):
    seen = ",".join(f"fake:{index}" for index in range(250))
    keys = practice._seen_review_set(seen, [])
    assert practice.MAX_SEEN_REVIEWS == 200
    assert len(keys) == 200
    response = client.get(
        "/api/questions",
        params={"page_size": 4, "include_reviews": "true", "seen_reviews": seen},
    )
    assert response.status_code == 200, response.text
    assert response.json()["items"]


def test_stats_subject_progress_batches_large_attempt_history(client, monkeypatch):
    me = client.get("/api/auth/me").json()
    with practice._bank() as con:
        qids = [row[0] for row in con.execute("SELECT qid FROM questions LIMIT 12")]
    assert len(qids) == 12
    with db.connection() as con:
        for qid in qids:
            con.execute(
                "INSERT INTO attempts(user_id,qid,answer_text,result,grade_source,created_at) "
                "VALUES (?,?,?,?,?,?)",
                (me["id"], qid, "A", "pending", "pending", db.utcnow()),
            )
    chunks = []
    original = practice._chunked

    def counting_chunks(values, size=None):
        for chunk in original(values, size):
            chunks.append(chunk)
            yield chunk

    monkeypatch.setattr(practice, "IN_CHUNK_SIZE", 5)
    monkeypatch.setattr(practice, "_chunked", counting_chunks)
    body = client.get("/api/stats").json()
    assert body["practiced"] == 12
    assert sum(item["practiced"] for item in body["subject_progress"]) == 12
    assert len(chunks) == 3, "12 个 qid / 每批 5 个应分 3 批，避免超出 SQLITE_MAX_VARIABLE_NUMBER"


def test_wrongbook_error_tag_like_wildcards_are_escaped(client):
    question = _real_choice_question()
    qid = question["qid"]
    added = client.post("/api/wrongbook", json={"qid": qid, "error_tags": ["100%"]})
    assert added.status_code == 200, added.text
    assert client.get("/api/wrongbook", params={"error_tag": "100%"}).json()["total"] == 1
    # 未转义时 % 会命中全部、_ 会匹配任意单字符；转义后必须是精确的 JSON 标签匹配。
    assert client.get("/api/wrongbook", params={"error_tag": "%"}).json()["total"] == 0
    assert client.get("/api/wrongbook", params={"error_tag": "10_"}).json()["total"] == 0


@contextmanager
def _broken_connection(*args, **kwargs):
    class BrokenConnection:
        def execute(self, *argv, **kwargs):
            raise sqlite3.IntegrityError("FOREIGN KEY constraint failed")
    yield BrokenConnection()


def test_attempt_integrity_error_maps_to_422(client, monkeypatch):
    question = _real_choice_question()
    monkeypatch.setattr(practice, "connection", _broken_connection)
    response = client.post(
        "/api/attempts", data={"qid": question["qid"], "answer": question["answer_text"]},
    )
    assert response.status_code == 422, response.text
    assert "作答数据" in response.json()["detail"]


def test_issue_integrity_error_maps_to_422(client, monkeypatch):
    question = _real_choice_question()
    monkeypatch.setattr(practice, "connection", _broken_connection)
    response = client.post(
        f"/api/questions/{question['qid']}/issues",
        data={"field": "formula", "issue_type": "formula", "note": "公式显示异常"},
    )
    assert response.status_code == 422, response.text
    assert "报错数据" in response.json()["detail"]


def test_knowledge_point_import_rejects_broken_payloads(client):
    def upload(name, payload):
        return client.post(
            "/api/admin/knowledge-points/import",
            files={"file": (name, payload, "application/octet-stream")},
        )

    assert upload("kp.csv", b"\xff\xfe\xfd\x00").status_code == 422  # 非 UTF-8
    assert upload("kp.json", b'{"x": 1}').status_code == 422  # 章节不是列表
    assert upload("kp.json", b'{"x": [{"children": []}]}').status_code == 422  # 缺 name
    assert upload("kp.json", b"{not json").status_code == 422


def test_question_kp_fk_violation_maps_to_422(client):
    """知识点在提交瞬间被删除等外键冲突返回 422，而不是 500。"""
    question = _real_choice_question()
    response = client.post(
        "/api/admin/question-kp", json={"qid": question["qid"], "kp_ids": [9999999]},
    )
    assert response.status_code == 422, response.text
    assert "知识点 ID" in response.json()["detail"]
