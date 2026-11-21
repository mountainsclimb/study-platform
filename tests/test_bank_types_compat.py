"""r5 纯 TEXT 题库的读侧类型兼容回归（P0）。

r5 发布包把 ``questions`` / ``groups`` 的所有列都建成 TEXT（objective='0'/'1'、
order_in_paper='10'、year='2010'，且 qid 无主键）。``bool('0')`` 在 Python 里为真、
TEXT 列按字典序排序，都会让判型、题序、年份区间和题组成员顺序出错。

本文件覆盖：
  * 与 r5 同构的纯 TEXT 临时题库（无主键）：标志位/数字字段正规化、非客观题带选项判
    pending、题目列表题序、年份区间与精确年份过滤、题组顺序与组接口计数；
  * 离线伪 PG 连接：镜像导入层把数字/标志列显式落成 INTEGER，旧纯 TEXT 镜像自动重建；
  * 真实 r5 只读抽样断言（当前题库不是 r5 纯 TEXT 形态时自动跳过）。
"""
import json
import sqlite3
from urllib.parse import quote

import pytest

from app import db, practice


# 与 r5 发布包 questions/groups 完全同构的列顺序（全部 TEXT、无主键）。
QUESTION_COLUMNS = (
    "qid", "question_id", "subject", "year", "province", "paper_type", "paper_name",
    "doc_id", "exam_id", "section_title", "section_kind", "number_label",
    "order_in_paper", "type", "objective", "stem_plain", "options_json",
    "answer_text", "answer_kind", "answer_note", "solution", "binding_method",
    "group_id", "group_role", "has_figure", "has_formula", "images_json",
    "source_blocks_json", "tier", "bank_version", "source_question_id",
    "parent_source_question_id", "group_kind", "practice_status", "grading_status",
    "content_revision", "answer_revision", "pua_remaining", "stem_blocks_json",
    "options_blocks_json", "answer_blocks_json", "solution_blocks_json",
    "shared_blocks_json", "member_index_json", "quality_flags_json",
)
GROUP_COLUMNS = (
    "group_id", "doc_id", "subject", "year", "province", "paper_name",
    "section_title", "material_kind", "material_text", "parent_label",
    "parent_type", "parent_stem", "n_children_total", "n_children_strict",
    "children_json", "bank_version", "group_kind", "parent_source_question_id",
    "shared_blocks_json", "member_index_json", "n_members", "n_practice",
)


def _dump(value):
    return json.dumps(value, ensure_ascii=False)


def _question(qid, **values):
    """r5 同构的一行：所有值都写成 TEXT（与发布包一致，不经过 INTEGER 亲和）。"""
    row = {column: None for column in QUESTION_COLUMNS}
    row.update({
        "qid": qid, "question_id": qid, "subject": "数学", "year": "2024",
        "province": "全国", "paper_type": "模拟", "paper_name": "TEXT 类型回归卷",
        "doc_id": "docDefault", "exam_id": "exam1", "section_title": "一、选择题",
        "section_kind": "choice", "number_label": "1", "order_in_paper": "1",
        "type": "single_choice",
        "objective": "1", "stem_plain": f"题干：{qid}",
        "options_json": _dump([{"label": "A", "text": "甲"}, {"label": "B", "text": "乙"}]),
        "answer_text": "A", "answer_kind": "choice", "answer_note": None,
        "solution": "解析文本", "binding_method": "doc", "group_id": None,
        "group_role": None, "has_figure": "0", "has_formula": "0",
        "images_json": "[]", "source_blocks_json": "[]", "tier": "1",
        "bank_version": "bank-r5-text-fixture", "source_question_id": qid,
        "parent_source_question_id": None, "group_kind": None,
        "practice_status": "practice", "grading_status": "auto",
        "content_revision": "c0ffee123456", "answer_revision": "feedbeef7890",
        "pua_remaining": "0", "stem_blocks_json": "[]", "options_blocks_json": "[]",
        "answer_blocks_json": "[]", "solution_blocks_json": "[]",
        "shared_blocks_json": "[]", "member_index_json": "[]",
        "quality_flags_json": "[]",
    })
    row.update(values)
    return row


def _group(group_id, **values):
    row = {column: None for column in GROUP_COLUMNS}
    row.update({
        "group_id": group_id, "doc_id": group_id.split(":", 1)[0],
        "subject": "物理", "year": "2018", "province": "全国",
        "paper_name": "TEXT 类型回归卷", "section_title": "二、材料题",
        "material_kind": "text", "material_text": "材料：题组共用材料",
        "parent_label": "1", "parent_type": "material_group", "parent_stem": "",
        "n_children_total": "3", "n_children_strict": "3", "children_json": "[]",
        "bank_version": "bank-r5-text-fixture", "group_kind": "material",
        "parent_source_question_id": None, "shared_blocks_json": "[]",
        "member_index_json": "[]", "n_members": "3", "n_practice": "3",
    })
    row.update(values)
    return row


def _make_text_bank(root, questions, groups=()):
    """按 r5 结构建纯 TEXT 题库：无 PRIMARY KEY、所有值用 TEXT 亲和写入。"""
    root.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(root / "题库.db"))
    try:
        con.execute(
            "CREATE TABLE questions ("
            + ", ".join(f'"{column}" TEXT' for column in QUESTION_COLUMNS)
            + ")"
        )
        con.execute(
            "CREATE TABLE groups ("
            + ", ".join(f'"{column}" TEXT' for column in GROUP_COLUMNS)
            + ")"
        )
        for row in questions:
            values = {column: row.get(column) for column in QUESTION_COLUMNS}
            con.execute(
                f"INSERT INTO questions({', '.join(QUESTION_COLUMNS)}) "
                f"VALUES ({', '.join('?' for _ in QUESTION_COLUMNS)})",
                list(values.values()),
            )
        for row in groups:
            values = {column: row.get(column) for column in GROUP_COLUMNS}
            con.execute(
                f"INSERT INTO groups({', '.join(GROUP_COLUMNS)}) "
                f"VALUES ({', '.join('?' for _ in GROUP_COLUMNS)})",
                list(values.values()),
            )
        con.commit()
    finally:
        con.close()
    return root


def _clear_bank_caches():
    practice._sqlite_question_columns.cache_clear()
    practice._sqlite_group_columns.cache_clear()
    practice._source_groups.cache_clear()
    practice._bank_hash_cache.clear()
    practice._verified_index_key = None
    practice._verified_fts_key = None
    practice.invalidate_catalog_cache()


@pytest.fixture
def text_bank(tmp_path, monkeypatch):
    questions = [
        # 一套 10 题以上、题序含 '10' 的卷：文本序 1,10,11,2；数值序 1,2,10,11。
        *[
            _question(f"docP:{order}", doc_id="docP", order_in_paper=order,
                      number_label=order, year="2010", stem_plain=f"卷内第{order}题")
            for order in ("1", "2", "10", "11")
        ],
        # 年份：文本字典序下 '999' > '2026'，必须按数值比较；空年份安全降级。
        _question("docY:1", doc_id="docY", year="999", order_in_paper="1", stem_plain="年份异常题"),
        _question("docY:2", doc_id="docY", year="2010", order_in_paper="2", stem_plain="年份二零一零"),
        _question("docY:3", doc_id="docY", year="2020", order_in_paper="3", stem_plain="年份二零二零"),
        _question("docY:4", doc_id="docY", year="", order_in_paper="4", stem_plain="年份缺失题"),
        # 判型：objective='0' 但带选项（r5 有 184 条），只能 pending 不能按选择题判分。
        _question(
            "docS:1", doc_id="docS", order_in_paper="1", stem_plain="非客观题但带选项",
            type="short_answer", objective="0", answer_text="A",
            options_json=_dump([{"label": label, "text": f"选项{label}"} for label in "ABCD"]),
        ),
        _question("docS:2", doc_id="docS", order_in_paper="2", stem_plain="客观单选题"),
        _question(
            "docS:3", doc_id="docS", order_in_paper="3", stem_plain="无选项主观题",
            type="short_answer", objective="0", options_json="[]", answer_text="解答文本",
        ),
        # 题组 A：没有 member_index，走 questions 反查兜底 + 数值题序。
        _question("docG:2", doc_id="docG", subject="物理", group_id="docG:m_0001",
                  order_in_paper="2", number_label="2", stem_plain="题组第2问"),
        _question("docG:3", doc_id="docG", subject="物理", group_id="docG:m_0001",
                  order_in_paper="3", number_label="3", stem_plain="题组第3问"),
        _question("docG:10", doc_id="docG", subject="物理", group_id="docG:m_0001",
                  order_in_paper="10", number_label="10", stem_plain="题组第10问"),
        # 题组 B：member_index 的 order 是 TEXT，也要按数值排序。
        _question("docM:2", doc_id="docM", subject="化学", group_id="docM:m_0099",
                  order_in_paper="2", number_label="2", stem_plain="成员第2问"),
        _question("docM:3", doc_id="docM", subject="化学", group_id="docM:m_0099",
                  order_in_paper="3", number_label="3", stem_plain="成员第3问"),
        _question("docM:10", doc_id="docM", subject="化学", group_id="docM:m_0099",
                  order_in_paper="10", number_label="10", stem_plain="成员第10问"),
    ]
    members = [
        {"qid": f"docM:{order}", "in_bank": True, "order": order, "type": "single_choice",
         "group_role": "sub", "number_label": order, "practice_status": "practice",
         "grading_status": "auto"}
        for order in ("10", "2", "3")  # 故意乱序 + TEXT
    ]
    groups = [
        _group("docG:m_0001", doc_id="docG", subject="物理", year="2018"),
        _group("docM:m_0099", doc_id="docM", subject="化学", year="2019",
               member_index_json=_dump(members), n_members="3", n_practice="3"),
    ]
    bank = _make_text_bank(tmp_path / "r5-text-bank", questions, groups)
    _clear_bank_caches()
    monkeypatch.setattr(db, "BANK_DIR", bank)
    yield bank
    _clear_bank_caches()


# ---------------------------------------------------------------------------
# 1. 标志位与数字字段正规化
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(("raw", "expected"), [
    ("0", False), ("1", True), ("", False), (None, False),
    (0, False), (1, True), ("true", True), ("false", False), ("2", True),
])
def test_flag_normalizes_text_columns(raw, expected):
    assert practice._flag(raw) is expected


def test_int_sql_is_engine_aware():
    sqlite_sql = practice._int_sql(sqlite3.connect(":memory:"), "q.year")
    assert "CAST(NULLIF(TRIM(q.year), '') AS INTEGER)" in sqlite_sql
    assert practice._int_sql(db._Postgres(object()), "q.year") == "COALESCE(q.year, 0)"


def test_load_question_and_api_return_stable_types(client, text_bank):
    # 题库文件本身保持 r5 的 TEXT 形态（只读，不做任何转换）。
    with practice._bank() as con:
        raw = con.execute(
            "SELECT objective, year, order_in_paper, has_figure FROM questions WHERE qid=?",
            ("docS:1",),
        ).fetchone()
    assert raw["objective"] == "0"
    assert raw["year"] == "2024"
    assert raw["order_in_paper"] == "1"
    assert raw["has_figure"] == "0"

    question = practice.load_question("docS:1")
    assert question["objective"] is False
    assert question["year"] == 2024
    assert isinstance(question["year"], int)
    assert isinstance(question["order_in_paper"], int)
    assert question["has_figure"] is False
    assert practice.load_question("docS:2")["objective"] is True

    detail = client.get("/api/questions/" + quote("docS:1", safe="")).json()
    assert detail["objective"] is False
    listing = client.get("/api/questions", params={"subject": "数学", "page_size": 50}).json()
    item = next(row for row in listing["items"] if row["qid"] == "docS:1")
    assert item["objective"] is False
    assert item["year"] == 2024


# ---------------------------------------------------------------------------
# 2. 判型：objective='0' 带选项不得按选择题判分
# ---------------------------------------------------------------------------

def test_non_objective_with_options_submits_pending(client, text_bank):
    response = client.post("/api/attempts", data={"qid": "docS:1", "answer": _dump(["A"])})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["result"] == "pending"
    assert body["attempt"]["result"] == "pending"
    # 作答本身要保留（选项格式仍校验、正规化后入库），只是不自动判分。
    assert body["attempt"]["answer_text"] == "A"
    assert body["question"]["objective"] is False


def test_objective_with_options_still_graded(client, text_bank):
    correct = client.post("/api/attempts", data={"qid": "docS:2", "answer": _dump(["A"])})
    assert correct.status_code == 200, correct.text
    assert correct.json()["result"] == "correct"
    wrong = client.post("/api/attempts", data={"qid": "docS:2", "answer": _dump(["B"])})
    assert wrong.json()["result"] == "incorrect"
    # 无选项的主观题仍需图片，不能把文字当选择题提交。
    missing_image = client.post("/api/attempts", data={"qid": "docS:3", "answer": "文字作答"})
    assert missing_image.status_code == 422


# ---------------------------------------------------------------------------
# 3. 题序 / 年份区间 / 题组成员顺序
# ---------------------------------------------------------------------------

def test_question_list_order_is_numeric(client, text_bank):
    listing = client.get("/api/questions", params={
        "subject": "数学", "sort": "year_desc", "page_size": 50,
    }).json()
    docp = [item["qid"] for item in listing["items"] if item["qid"].startswith("docP:")]
    assert docp == ["docP:1", "docP:2", "docP:10", "docP:11"]


def test_catalog_years_are_numeric_and_sorted(client, text_bank):
    catalog = client.get("/api/catalog").json()
    assert catalog["years"] == [2024, 2020, 2010, 999]
    assert all(isinstance(year, int) for year in catalog["years"])


def test_year_filters_compare_numerically(client, text_bank):
    # 旧实现 TEXT 比较会把 '999' 当成大于 '2010'：必须按数值过滤掉。
    ranged = client.get("/api/questions", params={
        "subject": "数学", "year_from": 2010, "year_to": 2020, "page_size": 50,
    }).json()
    assert ranged["total"] == 6, ranged
    assert {item["year"] for item in ranged["items"]} == {2010, 2020}
    exact = client.get("/api/questions", params={
        "subject": "数学", "year": 2010, "page_size": 50,
    }).json()
    assert exact["total"] == 5
    assert {item["year"] for item in exact["items"]} == {2010}
    # 非法年份输入安全降级为 422（而不是 500）。
    assert client.get("/api/questions", params={"year": "not-a-year"}).status_code == 422


def test_search_sql_year_is_numeric_for_both_engines(monkeypatch):
    monkeypatch.setattr(practice, "ensure_leak_flags", lambda con=None: True)
    monkeypatch.setattr(practice, "_active_isolations", lambda force=False: {})
    sqlite_con = sqlite3.connect(":memory:")
    _, where_sql, params = practice._build_search_sql(
        sqlite_con, 1, year=2015, year_from=2010, year_to=2020,
    )
    assert "CAST(NULLIF(TRIM(q.year), '') AS INTEGER)" in where_sql
    assert params == [2015, 2010, 2020]
    # 非法值直接调用（绕过 FastAPI 校验）时安全降级为不过滤。
    _, invalid_sql, invalid_params = practice._build_search_sql(sqlite_con, 1, year="not-a-year")
    assert "q.year" not in invalid_sql and invalid_params == []
    fake = _FakeRaw()
    _, pg_sql, pg_params = practice._build_search_sql(
        db._Postgres(fake), 1, year_from=2010,
    )
    assert "COALESCE(q.year, 0) >= ?" in pg_sql
    assert "CAST" not in pg_sql and pg_params == [2010]


def test_group_member_fallback_is_numeric(client, text_bank):
    response = client.get("/api/groups/" + quote("docG:m_0001", safe=""))
    assert response.status_code == 200, response.text
    group = response.json()
    # 文本序会是 10,2,3；接口必须按数值排序。
    assert [member["qid"] for member in group["members"]] == ["docG:2", "docG:3", "docG:10"]
    assert [member["order"] for member in group["members"]] == [2, 3, 10]
    assert group["year"] == 2018 and isinstance(group["year"], int)
    assert group["n_members"] == 3 and group["n_practice"] == 3
    assert isinstance(group["n_members"], int) and isinstance(group["n_practice"], int)

    detail = practice.load_question("docG:10")
    detail_group = detail["group"]
    assert detail_group["n_children_total"] == 3
    assert isinstance(detail_group["n_children_total"], int)
    assert detail_group["n_practice"] == 3
    assert detail["order_in_paper"] == 10


def test_group_member_index_order_is_numeric(client, text_bank):
    response = client.get("/api/groups/" + quote("docM:m_0099", safe=""))
    assert response.status_code == 200, response.text
    members = response.json()["members"]
    assert [member["qid"] for member in members] == ["docM:2", "docM:3", "docM:10"]
    assert [member["order"] for member in members] == [2, 3, 10]
    index = practice.load_question("docM:2")["group"]["member_index"]
    assert [entry["qid"] for entry in index] == ["docM:2", "docM:3", "docM:10"]


# ---------------------------------------------------------------------------
# 4. PostgreSQL 镜像导入层：数字/标志列落成 INTEGER，旧 TEXT 镜像自动重建
# ---------------------------------------------------------------------------

def _render(statement) -> str:
    if hasattr(statement, "as_string"):
        return statement.as_string(None)
    return str(statement)


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


class _FakeCursor:
    def __init__(self, raw, meta_value=None):
        self.raw = raw
        self.meta_value = meta_value
        self.lastrowid = None

    def fetchone(self):
        return (self.meta_value,) if self.meta_value is not None else None

    def fetchall(self):
        return []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def copy(self, statement):
        return _FakeCopy(self.raw, _render(statement))


class _FakeRaw:
    """psycopg 连接的伪实现：记录 SQL 顺序、COPY 语句与写入行。"""

    def __init__(self, meta_value=None):
        self.events = []
        self.meta_value = meta_value

    def execute(self, statement, params=None):
        rendered = _render(statement)
        self.events.append(("execute", rendered, params))
        return _FakeCursor(self, self.meta_value if "FROM bank_meta" in rendered else None)

    def cursor(self):
        return _FakeCursor(self)

    def commit(self):
        pass

    def rollback(self):
        pass


def _pg_mirror_events(text_bank, monkeypatch, meta_value=None):
    pytest.importorskip("psycopg", reason="PG 镜像离线测试需要 psycopg 的 sql.Composable 渲染")
    monkeypatch.setattr(practice, "_install_bank_leak_flags", lambda con, digest=None: True)
    monkeypatch.setattr(db, "_pg_bank_verified_digest", None)
    fake = _FakeRaw(meta_value=meta_value)
    db._ensure_postgres_bank(db._Postgres(fake))
    return fake


def test_pg_mirror_declares_integer_columns_and_casts_values(text_bank, monkeypatch):
    fake = _pg_mirror_events(text_bank, monkeypatch)
    executions = [statement for kind, statement, _ in fake.events if kind == "execute"]
    created_questions = next(
        statement for statement in executions
        if "CREATE TABLE" in statement and '"bank"."questions"' in statement
    )
    created_groups = next(
        statement for statement in executions
        if "CREATE TABLE" in statement and '"bank"."groups"' in statement
    )
    for column in ("year", "order_in_paper", "objective", "has_figure"):
        assert f'"{column}" INTEGER' in created_questions, column
    for column in ("year", "n_members", "n_practice"):
        assert f'"{column}" INTEGER' in created_groups, column

    _, copy_sql, copy_rows = next(
        event for event in fake.events
        if event[0] == "copy" and '"bank"."questions"' in event[1]
    )
    assert '"year"' in copy_sql
    names = [*QUESTION_COLUMNS, "stem_compact", "search_material_text"]
    index = {name: position for position, name in enumerate(names)}
    by_qid = {row[index["qid"]]: row for row in copy_rows}
    paper_row = by_qid["docP:10"]
    assert paper_row[index["year"]] == 2010 and isinstance(paper_row[index["year"]], int)
    assert paper_row[index["order_in_paper"]] == 10
    assert paper_row[index["objective"]] == 1
    assert paper_row[index["has_figure"]] == 0
    assert paper_row[index["stem_compact"]] == practice._compact_search_text("卷内第10题")
    # 非客观题带选项：镜像里 objective 是整数 0（不是字符串 '0'）。
    assert by_qid["docS:1"][index["objective"]] == 0

    group_names = list(GROUP_COLUMNS)
    group_index = {name: position for position, name in enumerate(group_names)}
    _, _, group_rows = next(
        event for event in fake.events
        if event[0] == "copy" and '"bank"."groups"' in event[1]
    )
    groups_by_id = {row[group_index["group_id"]]: row for row in group_rows}
    group_row = groups_by_id["docG:m_0001"]
    assert group_row[group_index["year"]] == 2018
    assert group_row[group_index["n_members"]] == 3
    assert group_row[group_index["n_practice"]] == 3

    raw_digest = db._bank_digest(db._bank_signature(db.bank_db_path().resolve()))
    salted = db._pg_mirror_digest(raw_digest)
    assert salted != raw_digest, "镜像结构版本必须写进 bank_meta 值，以触发旧镜像重建"
    meta_writes = [
        params for kind, statement, params in fake.events
        if kind == "execute" and "INSERT INTO bank_meta" in statement
    ]
    assert meta_writes[-1] == (db.PG_BANK_INDEX_KEY, salted)


def test_pg_mirror_rebuilds_legacy_text_mirror(text_bank, monkeypatch):
    pytest.importorskip("psycopg", reason="PG 镜像离线测试需要 psycopg 的 sql.Composable 渲染")
    raw_digest = db._bank_digest(db._bank_signature(db.bank_db_path().resolve()))
    # 旧版本把未加盐的题库摘要写进 bank_meta：结构已变化，必须重建而不是直接复用。
    fake = _pg_mirror_events(text_bank, monkeypatch, meta_value=raw_digest)
    assert any(
        kind == "execute" and "DROP TABLE" in statement and "bank.questions" in statement
        for kind, statement, _ in fake.events
    ), "旧纯 TEXT 镜像不能被当成最新结构复用"


def test_pg_mirror_warm_digest_skips_ddl(text_bank, monkeypatch):
    pytest.importorskip("psycopg", reason="PG 镜像离线测试需要 psycopg 的 sql.Composable 渲染")
    raw_digest = db._bank_digest(db._bank_signature(db.bank_db_path().resolve()))
    # 镜像已是当前结构：bank_meta 里应存加盐摘要，进程内快路径按原始摘要 O(1) 返回。
    fake = _pg_mirror_events(
        text_bank, monkeypatch, meta_value=db._pg_mirror_digest(raw_digest),
    )
    assert not any(
        kind == "execute" and "CREATE TABLE" in statement
        for kind, statement, _ in fake.events
    ), "镜像结构未变时不应重建"
    monkeypatch.setattr(db, "_pg_bank_verified_digest", raw_digest)
    warm = _FakeRaw()
    db._ensure_postgres_bank(db._Postgres(warm))
    assert warm.events == [], "进程内已验证摘要应 O(1) 返回"


def test_pg_similar_search_orders_by_snippet_hits(monkeypatch):
    """PG 相似题检索也要按片段命中数排序，且 SQL 占位符与参数一一对应。"""
    from contextlib import contextmanager

    monkeypatch.setattr(db, "postgres_enabled", lambda: True)
    monkeypatch.setattr(practice, "ensure_leak_flags", lambda con=None: True)
    monkeypatch.setattr(practice, "_active_isolations", lambda force=False: {})
    fake = _FakeRaw()
    con = db._Postgres(fake)

    @contextmanager
    def _fake_app_with_bank():
        yield con

    monkeypatch.setattr(practice, "_app_with_bank", _fake_app_with_bank)
    assert practice.search_similar_questions("函数单调性的判断方法", subject="数学", limit=5) == []
    statement, params = fake.events[-1][1], fake.events[-1][2]
    assert "ORDER BY ((CASE WHEN" in statement
    assert statement.count("%s") == len(params) > 0
    # 6 个片段：WHERE 18 个模糊匹配参数 + 1 个学科参数，ORDER BY 末尾再重复 18 个。
    assert len(params) == 37, params
    assert params[18] == "数学"
    assert params[19:] == params[:18]


# ---------------------------------------------------------------------------
# 5. 真实 r5 只读抽样（题库不是 r5 纯 TEXT 形态 / 数据包时跳过）
# ---------------------------------------------------------------------------

def _real_r5_or_skip():
    path = db.BANK_DIR / "题库.db"
    if not path.is_file():
        pytest.skip("未找到只读题库")
    try:
        with practice._bank() as con:
            columns = practice._bank_question_columns(con)
            required = {
                "qid", "objective", "order_in_paper", "year", "options_json",
                "practice_status", "grading_status",
            }
            if not required <= columns:
                pytest.skip("当前题库不是 r5 快照形态")
            types = con.execute(
                "SELECT typeof(objective), typeof(order_in_paper), typeof(year) FROM questions LIMIT 1"
            ).fetchone()
            total = con.execute("SELECT COUNT(*) FROM questions").fetchone()[0]
    except sqlite3.Error:
        pytest.skip("当前题库不可读")
    if tuple(types) != ("text", "text", "text"):
        pytest.skip("当前题库不是 r5 纯 TEXT 发布包（已是数值类型）")
    if total < 20000:
        pytest.skip("当前题库不是 r5 大盘（行数不足）")


def test_real_r5_paper_order_is_numeric():
    _real_r5_or_skip()
    with practice._bank() as con:
        order_expr = practice._int_sql(con, "order_in_paper")
        candidates = [
            row[0] for row in con.execute(
                "SELECT doc_id FROM questions WHERE doc_id IS NOT NULL AND doc_id <> '' "
                "GROUP BY doc_id HAVING COUNT(*) >= 10 LIMIT 400"
            )
        ]
        chosen = None
        for doc_id in candidates:
            text_order = [
                row[0] for row in con.execute(
                    "SELECT qid FROM questions WHERE doc_id=? ORDER BY order_in_paper, qid",
                    (doc_id,),
                )
            ]
            numeric_order = [
                row[0] for row in con.execute(
                    f"SELECT qid FROM questions WHERE doc_id=? ORDER BY {order_expr}, qid",
                    (doc_id,),
                )
            ]
            if text_order != numeric_order:
                chosen = (doc_id, text_order, numeric_order)
                break
    assert chosen is not None, "r5 应至少有一套卷的文本题序与数值题序不同"
    doc_id, text_order, numeric_order = chosen
    with practice._bank() as con:
        order_expr = practice._int_sql(con, "order_in_paper")
        orders = [
            int(row[0]) for row in con.execute(
                f"SELECT order_in_paper FROM questions WHERE doc_id=? ORDER BY {order_expr}, qid",
                (doc_id,),
            )
        ]
    assert len(orders) >= 10
    assert orders == sorted(orders), f"{doc_id} 题序应按数值升序"
    assert numeric_order != text_order


def test_real_r5_group_members_sorted_numerically(client):
    _real_r5_or_skip()
    with practice._bank() as con:
        candidates = [
            row[0] for row in con.execute(
                "SELECT group_id FROM questions WHERE group_id IS NOT NULL AND group_id <> '' "
                "GROUP BY group_id HAVING COUNT(*) >= 10 LIMIT 200"
            )
        ]
    for group_id in candidates:
        response = client.get("/api/groups/" + quote(group_id, safe=""))
        if response.status_code != 200:
            continue
        body = response.json()
        members = body["members"]
        if len(members) < 10:
            continue
        orders = [member["order"] for member in members]
        assert all(isinstance(order, int) for order in orders)
        assert orders == sorted(orders), f"{group_id} 题组成员顺序应按数值排序"
        assert body["n_members"] == len(members)
        assert isinstance(body["n_members"], int)
        return
    pytest.skip("r5 没有可读取的 10 成员以上题组")


def test_real_r5_non_objective_with_options_submits_pending(client):
    _real_r5_or_skip()
    with practice._bank() as con:
        qids = [
            row[0] for row in con.execute(
                "SELECT qid FROM questions WHERE objective='0' AND options_json IS NOT NULL "
                "AND options_json NOT IN ('', '[]') AND practice_status='practice' ORDER BY qid LIMIT 120"
            )
        ]
    chosen = None
    for qid in qids:
        question = practice.load_question(qid)
        if question["objective"] or not question["options"]:
            continue
        if question["quarantined"] or question["grading_flags"]:
            continue
        chosen = question
        break
    assert chosen is not None, "r5 应有可作答的非客观题同时含选项（doc 记录为 184 条）"
    labels = [
        str(option["label"]) for option in chosen["options"]
        if str(option.get("label") or "").upper() in set("ABCDEFGH")
    ]
    assert labels
    answer = _dump([labels[0]])
    response = client.post("/api/attempts", data={"qid": chosen["qid"], "answer": answer})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["result"] == "pending"
    assert body["question"]["objective"] is False
    assert body["attempt"]["answer_text"] == labels[0].upper()


def test_real_r5_catalog_years_and_year_filter(client):
    _real_r5_or_skip()
    catalog = client.get("/api/catalog").json()
    years = catalog["years"]
    assert years and all(isinstance(year, int) for year in years)
    assert years == sorted(years, reverse=True)
    response = client.get(
        "/api/questions", params={"year_from": 2020, "year_to": 2020, "page_size": 20},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] > 0
    assert all(item["year"] == 2020 for item in body["items"])


def test_real_r5_similar_search_ranks_identical_latex_stem(client):
    """LaTeX 题干（含 $\\left 等记号）也必须能找到题干完全相同的题。

    r5 的 FTS 候选若不按相关度排序，LIMIT 40 会被大量公共三角片段挤掉相同题干
    （test_v2_features 拍照相似题用例的回归根因）。
    """
    _real_r5_or_skip()
    with practice._bank() as con:
        qids = [
            row[0] for row in con.execute(
                "SELECT qid FROM questions WHERE stem_plain LIKE '%$%' "
                "AND practice_status='practice' LIMIT 80"
            )
        ]
    chosen = None
    for qid in qids:
        question = practice.load_question(qid)
        stem = question.get("stem_plain") or ""
        if question["quarantined"] or "$" not in stem or len(stem) < 12:
            continue
        chosen = question
        break
    if chosen is None:
        pytest.skip("当前题库没有可用的 LaTeX 题干样本")
    results = practice.search_similar_questions(
        chosen["stem_plain"], subject=chosen["subject"], limit=5,
    )
    assert any(
        item["qid"] == chosen["qid"] and item["exact_match"] for item in results
    ), f"题干相同的 LaTeX 题应排进相似题候选：{chosen['qid']}"
