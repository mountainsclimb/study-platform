"""快照读模型（知序快照契约 v1 §5/§6）回归测试。

测试不依赖 releases/ 下的真实快照：把只读基线题库备份到临时目录，按契约 §5
ALTER 出新增列并写入 fixture，再通过 monkeypatch 把 ``db.BANK_DIR`` 指向临时目录。
另一组用例仍指向基线题库，覆盖「题库没有新列时行为与现状一致」。
"""
import json
import sqlite3
from contextlib import closing
from pathlib import Path
from urllib.parse import quote

import pytest

SNAPSHOT_VERSION = "bank-fixture-r1"
# 基线题库中的真实 qid（复制后只改新列，不动旧列）。
MAIN_QID = "0028f646b2b0dde3:1"  # 干净单选，答案 D，无题组
QUARANTINED_QID = "0028f646b2b0dde3:10"  # 数据侧隔离
CONTEXT_QID = "0028f646b2b0dde3:11"  # context 上下文成员
REVIEW_QID = "0028f646b2b0dde3:12"  # review（答案 B）
PENDING_QID = "0028f646b2b0dde3:13"  # grading_status=pending（答案 C）
LEAK_QID = "0028f646b2b0dde3:2"  # 公开题干混入答案解析（核收记录阻断项 1）
GROUP_ID = "52d8ed86ac5664af:m_0002"
GROUP_QID = "52d8ed86ac5664af:14"  # 题组成员，答案 B
QUARANTINE_MARKER = "隔离门禁标记题"
CONTEXT_MARKER = "上下文门禁标记题"

QUESTION_COLUMNS = {
    "bank_version": "TEXT",
    "source_question_id": "TEXT",
    "parent_source_question_id": "TEXT",
    "group_kind": "TEXT",
    "practice_status": "TEXT",
    "grading_status": "TEXT",
    "content_revision": "TEXT",
    "answer_revision": "TEXT",
    "pua_remaining": "INTEGER",
    "stem_blocks_json": "TEXT",
    "options_blocks_json": "TEXT",
    "answer_blocks_json": "TEXT",
    "solution_blocks_json": "TEXT",
    "shared_blocks_json": "TEXT",
    "member_index_json": "TEXT",
    "quality_flags_json": "TEXT",
}
GROUP_COLUMNS = {
    "bank_version": "TEXT",
    "group_kind": "TEXT",
    "parent_source_question_id": "TEXT",
    "shared_blocks_json": "TEXT",
    "member_index_json": "TEXT",
    "n_members": "INTEGER",
    "n_practice": "INTEGER",
}


def _block(block_id, text, order=0, kind="text"):
    block = {
        "id": f"{block_id}#{order}", "kind": kind, "order": order,
        "block_id": block_id, "block_order": order,
        "paragraph_id": f"p{order:06d}", "line_index": 0, "page": 1,
        "source": f"para:{order}", "text": text,
    }
    if kind == "math":
        block.update({"latex": None, "fallback_asset": {"file": "images/fixture-formula.png", "role": "formula", "w": 24, "h": 60}})
    return block


def _options_blocks():
    return [
        {"label": label, "blocks": [_block(f"b90010{index}", f"{label}. 选项{label}", kind="text")]}
        for index, label in enumerate("ABCD")
    ]


def _dump(value):
    return json.dumps(value, ensure_ascii=False)


def _alter(con, table, columns):
    existing = {row[1] for row in con.execute(f'PRAGMA table_info("{table}")')}
    for name, kind in columns.items():
        if name not in existing:
            con.execute(f'ALTER TABLE "{table}" ADD COLUMN "{name}" {kind}')


def _update_question(con, qid, **values):
    assignments = ", ".join(f'"{key}"=?' for key in values)
    con.execute(f"UPDATE questions SET {assignments} WHERE qid=?", [*values.values(), qid])
    assert con.execute("SELECT 1 FROM questions WHERE qid=?", (qid,)).fetchone(), qid


def _seed_snapshot(con):
    main_quality = [{"code": "FORMULA_FALLBACK_ONLY", "message": "公式仅有原卷图，按图显示"}]
    _update_question(
        con, MAIN_QID,
        bank_version=SNAPSHOT_VERSION, source_question_id="q_fixture_0001",
        parent_source_question_id=None, group_kind=None,
        practice_status="practice", grading_status="auto",
        content_revision="c0ffee123456", answer_revision="feedbeef7890", pua_remaining=0,
        stem_blocks_json=_dump([
            _block("b900001", "1.—I’m sorry I didn’t finish it on time."),
            _block("b900002", "语法填空 x_{1} + y^{2}", order=1, kind="math"),
        ]),
        options_blocks_json=_dump(_options_blocks()),
        answer_blocks_json=_dump([_block("b900020", "D")]),
        solution_blocks_json=_dump([_block("b900021", "解析：选 D。")]),
        shared_blocks_json="[]", member_index_json="[]",
        quality_flags_json=_dump(main_quality),
    )
    _update_question(
        con, QUARANTINED_QID,
        bank_version=SNAPSHOT_VERSION, source_question_id="q_fixture_0010",
        practice_status="quarantined", grading_status="disabled", pua_remaining=3,
        stem_plain=QUARANTINE_MARKER + "，整段乱码无法还原",
        stem_blocks_json="[]", options_blocks_json="[]", answer_blocks_json="[]",
        solution_blocks_json="[]", shared_blocks_json="[]", member_index_json="[]",
        quality_flags_json=_dump([{"code": "PUA_MOJIBAKE_QUARANTINED", "message": "整段乱码，无法还原，整题隔离"}]),
    )
    _update_question(
        con, CONTEXT_QID,
        bank_version=SNAPSHOT_VERSION, source_question_id="q_fixture_0011",
        practice_status="context", grading_status="disabled",
        stem_plain=CONTEXT_MARKER + "仅供阅读",
        stem_blocks_json=_dump([_block("b900011", CONTEXT_MARKER)]),
        options_blocks_json="[]", answer_blocks_json="[]", solution_blocks_json="[]",
        shared_blocks_json="[]", member_index_json="[]", quality_flags_json="[]",
    )
    _update_question(
        con, REVIEW_QID,
        bank_version=SNAPSHOT_VERSION, source_question_id="q_fixture_0012",
        practice_status="review", grading_status="auto",
        stem_blocks_json="[]", options_blocks_json=_dump(_options_blocks()),
        answer_blocks_json="[]", solution_blocks_json="[]",
        shared_blocks_json="[]", member_index_json="[]", quality_flags_json="[]",
    )
    _update_question(
        con, PENDING_QID,
        bank_version=SNAPSHOT_VERSION, source_question_id="q_fixture_0013",
        practice_status="practice", grading_status="pending",
        stem_blocks_json="[]", options_blocks_json=_dump(_options_blocks()),
        answer_blocks_json="[]", solution_blocks_json="[]",
        shared_blocks_json="[]", member_index_json="[]",
        quality_flags_json=_dump([{"code": "ANSWER_MISSING", "message": "参考答案缺失，判分待核验"}]),
    )
    _update_question(
        con, LEAK_QID,
        bank_version=SNAPSHOT_VERSION, source_question_id="q_fixture_0014",
        practice_status="practice", grading_status="auto",
        stem_plain="答案泄漏门禁标记题【答案】B 【详解】解析内容混入题干",
        stem_blocks_json=_dump([_block("b900030", "答案泄漏门禁标记题【答案】B 【详解】解析内容混入题干")]),
        options_blocks_json=_dump(_options_blocks()),
        answer_blocks_json="[]", solution_blocks_json="[]",
        shared_blocks_json="[]", member_index_json="[]", quality_flags_json="[]",
    )

    shared_blocks = [
        _block("b900100", "材料正文：阅读下面的文字，完成 14～16 题。"),
        _block("b900101", "父题说明：下列各句中，加点词的意义和用法相同的一项是"),
    ]
    members = [
        {
            "qid": GROUP_QID, "in_bank": True, "source_question_id": "q_group_14",
            "number_label": "14", "sub_question_label": None, "type": "single_choice",
            "group_role": "sub", "order": 14,
            "practice_status": "practice", "grading_status": "auto",
        },
        {
            "qid": "52d8ed86ac5664af:15", "in_bank": True, "source_question_id": "q_group_15",
            "number_label": "15", "sub_question_label": None, "type": "single_choice",
            "group_role": "sub", "order": 15,
            "practice_status": "review", "grading_status": "auto",
        },
        {
            "qid": None, "in_bank": False, "source_question_id": "q_group_context",
            "number_label": "16", "sub_question_label": "（1）", "type": "sub_short_answer",
            "group_role": "sub", "order": 16,
            "practice_status": "context", "grading_status": "disabled",
            "blocks": [
                _block("b900110", "只读上下文成员内容"),
                _block("b900111", "【解答】只读成员混入的答案内容"),
            ],
        },
    ]
    _update_question(
        con, GROUP_QID,
        bank_version=SNAPSHOT_VERSION, source_question_id="q_group_14",
        parent_source_question_id="p_q_0025", group_kind="material",
        practice_status="practice", grading_status="auto",
        content_revision="c0ffee123456", answer_revision="feedbeef7890",
        stem_blocks_json=_dump([_block("b900120", "14.下列各个句子在文中的意思，不正确的一项是")]),
        options_blocks_json=_dump(_options_blocks()),
        answer_blocks_json=_dump([_block("b900121", "B")]),
        solution_blocks_json=_dump([_block("b900122", "解析：选 B。")]),
        shared_blocks_json=_dump(shared_blocks),
        member_index_json=_dump(members),
        quality_flags_json="[]",
    )
    con.execute("UPDATE questions SET group_kind='material' WHERE doc_id='52d8ed86ac5664af' AND group_id='m_0002'")
    # 成员状态以题库行实时值为准：fixture 的 member_index 与题目行必须一致。
    _update_question(con, "52d8ed86ac5664af:15",
                     bank_version=SNAPSHOT_VERSION, practice_status="review", grading_status="auto")
    con.execute(
        "UPDATE groups SET bank_version=?, group_kind='material', parent_source_question_id=NULL, "
        "shared_blocks_json=?, member_index_json=?, n_members=?, n_practice=? WHERE group_id=?",
        (SNAPSHOT_VERSION, _dump(shared_blocks), _dump(members), 3, 2, GROUP_ID),
    )
    assert con.execute("SELECT 1 FROM groups WHERE group_id=?", (GROUP_ID,)).fetchone(), GROUP_ID


@pytest.fixture(scope="module")
def snapshot_bank(tmp_path_factory):
    """只读基线题库的临时快照副本：复制 → ALTER 新列 → 写入读模型 fixture。"""
    from app import db

    source = db.BANK_DIR / "题库.db"
    assert source.is_file(), f"missing baseline bank: {source}"
    root = tmp_path_factory.mktemp("snapshot-bank")
    destination = root / "题库.db"
    with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True, timeout=20)) as source_con:
        with closing(sqlite3.connect(destination)) as destination_con:
            source_con.backup(destination_con)
    with closing(sqlite3.connect(destination)) as con:
        _alter(con, "questions", QUESTION_COLUMNS)
        _alter(con, "groups", GROUP_COLUMNS)
        _seed_snapshot(con)
        for statement in (
            "CREATE INDEX IF NOT EXISTS idx_q_practice_status ON questions(practice_status)",
            "CREATE INDEX IF NOT EXISTS idx_q_parent_src ON questions(parent_source_question_id)",
            "CREATE INDEX IF NOT EXISTS idx_g_doc ON groups(doc_id)",
        ):
            con.execute(statement)
        con.commit()
    material_dir = root / "语文"
    material_dir.mkdir()
    (material_dir / "groups.jsonl").write_text(
        _dump({
            "group_id": GROUP_ID,
            "material_images": [{"file": "images/fixture-material.png", "role": "figure", "w": 640, "h": 480}],
        }) + "\n",
        encoding="utf-8",
    )
    return root


@pytest.fixture()
def snapshot_client(client, snapshot_bank, monkeypatch):
    """把题库切到临时快照；退出时清掉按学科/按库缓存的辅助索引。"""
    from app import db, practice

    monkeypatch.setattr(db, "BANK_DIR", snapshot_bank)
    practice._source_groups.cache_clear()
    practice._sqlite_question_columns.cache_clear()
    yield client
    practice._source_groups.cache_clear()
    practice._sqlite_question_columns.cache_clear()


def _detail_url(qid):
    return "/api/questions/" + quote(qid, safe="")


def test_snapshot_question_detail_exposes_read_model(snapshot_client):
    response = snapshot_client.get(_detail_url(MAIN_QID))
    assert response.status_code == 200, response.text
    detail = response.json()
    assert detail["bank_version"] == SNAPSHOT_VERSION
    assert detail["practice_status"] == "practice"
    assert detail["grading_status"] == "auto"
    assert detail["content_revision"] == "c0ffee123456"
    assert detail["answer_revision"] == "feedbeef7890"
    assert detail["parent_source_question_id"] is None
    assert detail["group_kind"] is None
    assert [block["text"] for block in detail["stem_blocks"]] == [
        "1.—I’m sorry I didn’t finish it on time.",
        "语法填空 x_{1} + y^{2}",
    ]
    assert detail["stem_blocks"][1]["kind"] == "math"
    assert [entry["label"] for entry in detail["options_blocks"]] == ["A", "B", "C", "D"]
    assert detail["options_blocks"][3]["blocks"][0]["text"] == "D. 选项D"
    assert [option["label"] for option in detail["options"]] == ["A", "B", "C", "D"]
    assert detail["options"][3]["blocks"][0]["text"] == "D. 选项D"
    assert detail["shared_blocks"] == [] and detail["member_index"] == []
    assert any("公式仅有原卷图" in flag for flag in detail["quality_flags"])
    assert detail["practice_allowed"] is True
    # 快照契约 §6.1：作答前不得返回答案/解析块。
    assert "answer_blocks" not in detail and "solution_blocks" not in detail
    assert "answer" not in detail and "solution" not in detail
    assert snapshot_client.get("/api/reveal", params={"qid": MAIN_QID}).status_code == 403
    # 列表摘要同样读取新列且不泄漏答案。
    listing = snapshot_client.get("/api/questions", params={"subject": "英语", "page_size": 3})
    assert listing.status_code == 200, listing.text
    items = listing.json()["items"]
    assert items
    for item in items:
        assert "answer_blocks" not in item and "answer_text" not in item
        assert item["practice_status"] in ("practice", "review", "context", "quarantined")
        assert item["grading_status"] in ("auto", "pending", "disabled")
        assert "bank_version" in item


def test_snapshot_answer_blocks_unlock_only_after_attempt(snapshot_client, second_client):
    locked = snapshot_client.get(_detail_url(MAIN_QID)).json()
    assert "answer_blocks" not in locked and "solution_blocks" not in locked
    submitted = snapshot_client.post("/api/attempts", data={"qid": MAIN_QID, "answer": "D"})
    assert submitted.status_code == 200, submitted.text
    payload = submitted.json()
    assert payload["result"] == "correct"
    question = payload["question"]
    assert question["unlocked"] is True
    assert question["answer"]["text"] == "D"
    assert [block["text"] for block in question["answer_blocks"]] == ["D"]
    assert [block["text"] for block in question["solution_blocks"]] == ["解析：选 D。"]
    # 另一位用户尚未作答，仍看不到答案块。
    other = second_client.get(_detail_url(MAIN_QID)).json()
    assert other["unlocked"] is False
    assert "answer_blocks" not in other and "solution_blocks" not in other


def test_snapshot_practice_status_gates_and_pending_grading(snapshot_client):
    # quarantined：详情 409（带隔离原因）、提交 409，且不再进入常规检索与练习列表。
    response = snapshot_client.get(_detail_url(QUARANTINED_QID))
    assert response.status_code == 409
    assert "复核队列" in response.json()["detail"]
    assert "数据侧标记为隔离" in response.json()["detail"]
    blocked = snapshot_client.post("/api/attempts", data={"qid": QUARANTINED_QID, "answer": "C"})
    assert blocked.status_code == 409
    quarantined_search = snapshot_client.get("/api/questions", params={"q": QUARANTINE_MARKER}).json()
    assert quarantined_search["total"] == 0
    assert QUARANTINED_QID not in [item["qid"] for item in quarantined_search["items"]]

    # context：详情可 GET，提交 409，检索/练习列表不出现。
    context = snapshot_client.get(_detail_url(CONTEXT_QID))
    assert context.status_code == 200, context.text
    context_detail = context.json()
    assert context_detail["practice_status"] == "context"
    assert context_detail["practice_allowed"] is False
    assert any("上下文" in flag for flag in context_detail["quality_flags"])
    assert snapshot_client.post("/api/attempts", data={"qid": CONTEXT_QID, "answer": "D"}).status_code == 409
    assert snapshot_client.get("/api/questions", params={"q": CONTEXT_MARKER}).json()["total"] == 0

    # review：允许保存作答，但判分 result=pending / grade_source=pending。
    review = snapshot_client.post("/api/attempts", data={"qid": REVIEW_QID, "answer": "B"})
    assert review.status_code == 200, review.text
    review_body = review.json()
    assert review_body["result"] == "pending"
    assert review_body["attempt"]["result"] == "pending"
    assert review_body["attempt"]["grade_source"] == "pending"
    assert any(
        "参考答案待核验" in flag and "practice_status:review" in flag
        for flag in review_body["question"]["quality_flags"]
    )

    # grading_status=pending：同样保留作答但判 pending，质量提示含 grading_status 文案。
    pending = snapshot_client.post("/api/attempts", data={"qid": PENDING_QID, "answer": "C"})
    assert pending.status_code == 200, pending.text
    pending_body = pending.json()
    assert pending_body["result"] == "pending"
    assert pending_body["attempt"]["grade_source"] == "pending"
    assert any(
        "参考答案待核验" in flag and "grading_status:pending" in flag
        for flag in pending_body["question"]["quality_flags"]
    )

    catalog = snapshot_client.get("/api/catalog").json()
    assert catalog["source_total"] == 7659
    # 可练总数 = 总数 - 隔离数；快照题库会叠加数据侧隔离结论，这里校验三者一致性。
    assert catalog["available_total"] + catalog["quarantined_count"] == catalog["source_total"]
    assert catalog["quarantined_count"] >= 21


def test_snapshot_group_endpoint_and_member_progress(snapshot_client, second_client):
    url = "/api/groups/" + quote(GROUP_ID, safe="")
    response = snapshot_client.get(url)
    assert response.status_code == 200, response.text
    group = response.json()
    assert group["group_id"] == GROUP_ID
    assert group["bank_version"] == SNAPSHOT_VERSION
    assert group["subject"] == "语文"
    assert group["year"] == 2000
    assert group["group_kind"] == "material"
    assert group["n_members"] == 3
    assert group["n_practice"] == 2
    assert [block["text"] for block in group["shared_blocks"]] == [
        "材料正文：阅读下面的文字，完成 14～16 题。",
        "父题说明：下列各句中，加点词的意义和用法相同的一项是",
    ]
    assert group["material_images"][0]["file"] == "images/fixture-material.png"
    assert len(group["members"]) == 3
    first, second, context_member = group["members"]
    assert first["qid"] == GROUP_QID and first["in_bank"] is True
    assert first["progress"] == {"attempted": False, "result": None, "mastery": "未掌握"}
    assert second["practice_status"] == "review"
    assert context_member["qid"] is None and context_member["in_bank"] is False
    assert context_member["practice_status"] == "context"
    assert context_member["blocks"][0]["text"] == "只读上下文成员内容"
    assert context_member["blocks_redacted"] == 1
    assert "只读上下文成员内容" in json.dumps(group, ensure_ascii=False)
    # 核收记录阻断项 1：只读成员混入的答案不能在题组接口出现。
    assert "只读成员混入的答案内容" not in json.dumps(group, ensure_ascii=False)
    payload = json.dumps(group, ensure_ascii=False)
    assert "answer_blocks" not in payload and "solution_blocks" not in payload

    # 题目详情里的 group 对象同样带快照新字段。
    detail = snapshot_client.get(_detail_url(GROUP_QID)).json()
    detail_group = detail["group"]
    assert detail_group["group_kind"] == "material"
    assert detail_group["n_members"] == 3 and detail_group["n_practice"] == 2
    assert len(detail_group["shared_blocks"]) == 2
    assert detail_group["member_index"] == detail["member_index"]
    assert len(detail["member_index"]) == 3
    assert "只读成员混入的答案内容" not in json.dumps(detail, ensure_ascii=False)

    # 作答成员后进度更新；其他用户进度独立。
    submitted = snapshot_client.post("/api/attempts", data={"qid": GROUP_QID, "answer": "B"})
    assert submitted.status_code == 200, submitted.text
    assert submitted.json()["result"] == "correct"
    refreshed = snapshot_client.get(url).json()
    assert refreshed["members"][0]["progress"] == {"attempted": True, "result": "correct", "mastery": "未掌握"}
    other = second_client.get(url)
    assert other.status_code == 200
    assert other.json()["members"][0]["progress"]["attempted"] is False


def test_legacy_bank_without_snapshot_columns_falls_back(client):
    """基线题库没有新列：详情/列表/题组接口保持旧行为，禁止 500。"""
    from app.practice import QUARANTINED_GROUPS, _bank, _bank_question_columns, load_question

    with _bank() as con:
        if "practice_status" in _bank_question_columns(con):
            pytest.skip("当前 STUDY_BANK_DIR 已是快照题库（含新列），旧库兜底由基线题库回归覆盖")

    with _bank() as con:
        candidates = [row[0] for row in con.execute(
            "SELECT qid FROM questions WHERE type='single_choice' AND group_id IS NULL ORDER BY qid"
        )]
        groups = [
            row[0] for row in con.execute("SELECT group_id FROM groups WHERE n_children_total>=2 ORDER BY group_id")
            if row[0] not in QUARANTINED_GROUPS
        ]
    qid = next(candidate for candidate in candidates if load_question(candidate)["practice_allowed"])
    detail = client.get(_detail_url(qid))
    assert detail.status_code == 200, detail.text
    body = detail.json()
    assert body["bank_version"] is None
    assert body["practice_status"] == "practice"
    assert body["grading_status"] == "auto"
    assert body["content_revision"] is None and body["answer_revision"] is None
    assert body["stem_blocks"] == [] and body["options_blocks"] == []
    assert body["shared_blocks"] == [] and body["member_index"] == []
    assert "answer_blocks" not in body and "solution_blocks" not in body
    legacy_group = load_question(qid)["group"]
    if legacy_group:
        assert legacy_group["n_members"] == legacy_group["n_children_total"]
        assert legacy_group["member_index"] == [] and legacy_group["shared_blocks"] == []

    listing = client.get("/api/questions", params={"subject": "英语", "page_size": 3})
    assert listing.status_code == 200
    legacy_catalog = client.get("/api/catalog").json()
    assert legacy_catalog["available_total"] + legacy_catalog["quarantined_count"] == legacy_catalog["source_total"]

    group_id = groups[0]
    group_response = client.get("/api/groups/" + quote(group_id, safe=""))
    assert group_response.status_code == 200, group_response.text
    group = group_response.json()
    assert group["group_id"] == group_id
    assert group["bank_version"] is None
    assert group["members"]
    assert all(member["in_bank"] is True for member in group["members"])
    assert all(set(member["progress"]) == {"attempted", "result", "mastery"} for member in group["members"])
    assert group["n_members"] == len(group["members"])
    assert "answer_blocks" not in json.dumps(group, ensure_ascii=False)
    assert client.get("/api/groups/0000000000000000:m_9999").status_code == 404


def test_public_answer_leak_stops_practice_and_search(snapshot_client):
    """核收记录阻断项 1：公开题干混入答案解析时先隔离，不再计分或出现在检索里。"""
    detail = snapshot_client.get(_detail_url(LEAK_QID))
    assert detail.status_code == 409
    assert "答案解析" in detail.json()["detail"]
    assert snapshot_client.post("/api/attempts", data={"qid": LEAK_QID, "answer": "B"}).status_code == 409
    search = snapshot_client.get("/api/questions", params={"q": "答案泄漏门禁标记题"})
    assert search.status_code == 200
    assert search.json()["total"] == 0
    # 未混入答案的题目不受影响
    assert snapshot_client.get(_detail_url(MAIN_QID)).status_code == 200
