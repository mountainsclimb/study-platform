"""PostgreSQL 题库镜像 / 检索 SQL 的离线回归（伪连接，不连真实 PG）。

覆盖优化方案 §2.2 P0：
  * 镜像建表时直接包含 ``stem_compact`` / ``search_material_text`` 派生列，``COPY`` 一次写入；
  * 不再 COPY 后 ``ALTER TABLE ... ADD COLUMN`` + 逐行 ``UPDATE``（7,659 次双倍死元组 + 表级排他锁）；
  * DDL 顺序固定「建表（含列）→ COPY → 建 GIN(trgm) 索引 → ANALYZE」；
  * 关键词检索直接 ``q.stem_compact ILIKE``（不再 ``COALESCE`` 绕开 GIN 索引）；
  * PG 模式不再写 SQLite 专用的 ``bank_search_meta`` 镜像。
"""
import pytest

pytest.importorskip("psycopg", reason="PG 镜像离线测试需要 psycopg 的 sql.Composable 渲染")

from app import db, practice
from bank_fixture import QUESTION_COLUMNS, make_bank, question_row


def _render(statement) -> str:
    """psycopg Composable（CREATE TABLE/COPY）转 SQL 文本；普通字符串原样返回。"""
    if hasattr(statement, "as_string"):
        return statement.as_string(None)
    return str(statement)


class _FakeCursor:
    def __init__(self, raw):
        self.raw = raw
        self.lastrowid = None

    def fetchone(self):
        return None

    def fetchall(self):
        return []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def executemany(self, statement, rows):
        self.raw.events.append(("executemany", _render(statement), list(rows)))

    def copy(self, statement):
        return _FakeCopy(self.raw, _render(statement))


class _FakeCopy:
    def __init__(self, raw, statement):
        self.raw = raw
        self.statement = statement
        self.rows = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.raw.events.append(("copy", self.statement, self.rows))
        return False

    def write_row(self, row):
        self.rows.append(tuple(row))


class _FakeRaw:
    """psycopg Connection 的伪实现：记录 SQL 顺序、COPY 语句与写入行。"""

    def __init__(self):
        self.events = []

    def execute(self, statement, params=None):
        self.events.append(("execute", _render(statement), params))
        return _FakeCursor(self)

    def cursor(self):
        return _FakeCursor(self)

    def commit(self):
        pass

    def rollback(self):
        pass


def _statements(raw: _FakeRaw) -> list[str]:
    return [sql for kind, sql, _ in raw.events if kind == "execute"]


def _all_sql(raw: _FakeRaw) -> str:
    return " ".join(sql for kind, sql, _ in raw.events if kind in ("execute", "executemany"))


@pytest.fixture
def pg_bank(tmp_path, monkeypatch):
    rows = [
        question_row("docG:1", "函数 单调性：判断方法", doc_id="docG", group_id="m_0001"),
        question_row("docG:2", "集合 运算", doc_id="docG"),
    ]
    groups = [{
        "group_id": "docG:m_0001", "doc_id": "docG", "subject": "数学", "year": 2024,
        "material_text": "材料：函数 定义域为 R", "parent_stem": "",
        "shared_blocks_json": "[]", "member_index_json": "[]", "n_members": 1, "n_practice": 1,
        "bank_version": "pg-fixture-r1",
    }]
    bank = make_bank(tmp_path / "pg-bank", rows, groups)
    monkeypatch.setattr(db, "BANK_DIR", bank)
    monkeypatch.setattr(db, "_pg_bank_verified_digest", None)
    practice._sqlite_question_columns.cache_clear()
    practice._sqlite_group_columns.cache_clear()
    yield bank
    monkeypatch.setattr(db, "_pg_bank_verified_digest", None)
    practice._sqlite_question_columns.cache_clear()
    practice._sqlite_group_columns.cache_clear()


def test_pg_mirror_ddl_order_copy_columns_then_gin_then_analyze(pg_bank, monkeypatch):
    fake = _FakeRaw()
    con = db._Postgres(fake)
    installed = []
    monkeypatch.setattr(
        practice, "_install_bank_leak_flags",
        lambda con, digest=None: installed.append(digest) or True,
    )
    db._ensure_postgres_bank(con)

    events = fake.events
    # 1) 建表语句本身带派生列（不再 COPY 后 ALTER TABLE ADD COLUMN）。
    create_positions = [
        index for index, (kind, sql, _) in enumerate(events)
        if kind == "execute" and "CREATE TABLE" in sql and "bank" in sql and '"stem_compact"' in sql
    ]
    assert create_positions, "bank.questions 建表必须直接包含 stem_compact 列"
    create_pos = create_positions[0]
    created = events[create_pos][1]
    assert '"stem_compact" TEXT' in created
    assert '"search_material_text" TEXT' in created

    # 2) COPY 列包含派生列，且发生在建表之后、索引之前。
    question_copy = next(
        (index, sql, rows) for index, (kind, sql, rows) in enumerate(events)
        if kind == "copy" and '"bank"."questions"' in sql
    )
    copy_index, copy_sql, copy_rows = question_copy
    assert '"stem_compact"' in copy_sql and '"search_material_text"' in copy_sql
    groups_copy = next(
        (index, sql) for index, (kind, sql, _) in enumerate(events)
        if kind == "copy" and '"bank"."groups"' in sql
    )
    assert groups_copy[0] > create_pos and groups_copy[0] > copy_index

    # 3) 索引在数据装载之后创建；ANALYZE 在索引之后（旧实现 ANALYZE 在 UPDATE 之前）。
    script_pos, script = next(
        (index, sql) for index, (kind, sql, _) in enumerate(events)
        if kind == "execute" and "gin_trgm_ops" in sql
    )
    assert script_pos > copy_index
    assert "questions_stem_compact_trgm ON bank.questions USING GIN(stem_compact gin_trgm_ops)" in script
    assert "ANALYZE bank.questions" in script and "ANALYZE bank.groups" in script
    assert script.index("gin_trgm_ops") < script.index("ANALYZE bank.questions")

    sql_text = " ".join(_statements(fake))
    assert "ALTER TABLE" not in sql_text, "镜像构建不得再发 ALTER TABLE（ACCESS EXCLUSIVE 锁风暴）"
    assert "UPDATE bank.questions" not in sql_text, "派生列必须随 COPY 写入，不再逐行 UPDATE"
    assert "questions_stem_trgm" not in sql_text, "旧 stem_plain 三元组索引已由 stem_compact GIN 取代"
    assert installed and installed[-1], "镜像重建后同一条摘要路径刷新泄密标记"


def test_pg_mirror_copy_computes_compact_and_material_columns(pg_bank, monkeypatch):
    fake = _FakeRaw()
    monkeypatch.setattr(practice, "_install_bank_leak_flags", lambda con, digest=None: True)
    db._ensure_postgres_bank(db._Postgres(fake))

    _, copy_sql, rows = next(
        (index, sql, rows) for index, (kind, sql, rows) in enumerate(fake.events)
        if kind == "copy" and '"bank"."questions"' in sql
    )
    by_qid = {row[0]: row for row in rows}
    assert set(by_qid) == {"docG:1", "docG:2"}
    # 追加的派生列固定排在题库原有列之后：倒数第二列 stem_compact、最后一列材料镜像。
    compact = by_qid["docG:1"][-2]
    material = by_qid["docG:1"][-1]
    assert compact == practice._compact_search_text("函数 单调性：判断方法")
    assert " " not in compact and "：" not in compact
    assert material == "材料：函数 定义域为 R"
    # 无题组材料的题落空串，而不是 NULL。
    assert by_qid["docG:2"][-1] == ""
    assert len(rows[0]) == len(QUESTION_COLUMNS) + 2


def test_pg_mirror_warm_digest_is_o1_and_skips_ddl(pg_bank, monkeypatch):
    digest = db._bank_digest(db._bank_signature(db.bank_db_path().resolve()))
    monkeypatch.setattr(db, "_pg_bank_verified_digest", digest)
    fake = _FakeRaw()
    db._ensure_postgres_bank(db._Postgres(fake))
    assert fake.events == [], "题库摘要未变时进程内快路径必须零 SQL 返回"


def test_pg_mirror_uses_v3_index_key_and_skips_sqlite_search_meta(pg_bank, monkeypatch):
    fake = _FakeRaw()
    monkeypatch.setattr(practice, "_install_bank_leak_flags", lambda con, digest=None: True)
    db._ensure_postgres_bank(db._Postgres(fake))
    assert db.PG_BANK_INDEX_KEY == "pg_bank_search_v3_sha256"
    meta_writes = [
        params for kind, sql, params in fake.events
        if kind == "execute" and "INSERT INTO bank_meta" in sql
    ]
    assert meta_writes and meta_writes[-1][0] == db.PG_BANK_INDEX_KEY
    sql_text = " ".join(_statements(fake))
    assert "bank_search_meta" not in sql_text, "PG 模式不再维护仅 SQLite 使用的短词镜像表"


def test_pg_index_refresh_does_not_write_search_meta(monkeypatch):
    """PG 应用库刷新泄密标记时只写 bank_leak_flags。"""
    fake = _FakeRaw()
    con = db._Postgres(fake)
    practice._write_bank_index(
        con,
        [("qid-1", 0, 0, 1, "题干", "试卷", "材料")],
        "digest-fixture",
    )
    sql_text = _all_sql(fake)
    assert "bank_leak_flags" in sql_text
    assert "bank_search_meta" not in sql_text
    inserts = [
        sql for kind, sql, _ in fake.events
        if kind == "executemany" and "INSERT INTO bank_leak_flags" in sql
    ]
    assert inserts, "PG 泄密标记仍应写入 bank_leak_flags"
    assert "%s" in inserts[0], "PG 参数占位符必须是 %s"


def test_pg_steady_state_ensure_leak_flags_issues_no_ddl_or_queries(monkeypatch):
    """稳态（摘要未变）下 ensure_leak_flags 必须 O(1)：不查库、不发 DDL。"""
    monkeypatch.setattr(db, "postgres_enabled", lambda: True)
    monkeypatch.setattr(practice, "_bank_content_digest", lambda: "digest-fixture")
    key = practice._index_cache_key("digest-fixture")
    monkeypatch.setattr(practice, "_verified_index_key", key)
    fake = _FakeRaw()
    assert practice.ensure_leak_flags(db._Postgres(fake)) is True
    assert fake.events == []


def test_pg_keyword_search_targets_stem_compact_gin_column(monkeypatch):
    monkeypatch.setattr(db, "postgres_enabled", lambda: True)
    monkeypatch.setattr(practice, "ensure_leak_flags", lambda con=None: True)
    monkeypatch.setattr(practice, "_active_isolations", lambda force=False: {})
    monkeypatch.setattr(practice, "_bank_question_columns", lambda con: frozenset({
        "qid", "stem_plain", "paper_name", "answer_text", "solution",
        "solution_blocks_json", "practice_status", "doc_id", "group_id",
    }))
    fake = _FakeRaw()
    _, where_sql, params = practice._build_search_sql(db._Postgres(fake), 1, q="函数 单调性")
    assert "q.stem_compact ILIKE" in where_sql
    assert "COALESCE(stem_compact" not in where_sql
    assert "q.stem_plain ILIKE" not in where_sql
    assert "q.search_material_text ILIKE" in where_sql
    assert "ALTER TABLE" not in where_sql, "检索 SQL 不得包含任何 DDL"
    assert len(params) == 2 * 3, "两个检索词各 3 个 ILIKE 参数"
    assert all(value.startswith("%") and value.endswith("%") for value in params)
