"""快照读模型（知序快照契约 v1 §5/§6）回归测试。

测试不再依赖 ``releases/`` 下真实题库里的固定题目：fixture 只从当前基线库复制 schema
（表/索引 DDL），再插入自己构造的题目、题组与素材，通过 monkeypatch 把 ``db.BANK_DIR``
指向临时目录。这样 r3→r5 换基线时不会因为固定 qid 不存在而初始化失败，也不会改动只读
发布包；被测语义（快照读模型、答案解锁、可练状态门禁、题组、泄漏拦截）保持不变。

另一组用例仍指向基线题库，覆盖「题库没有新列时行为与现状一致」（基线已是快照库时跳过）。
"""
import json
import sqlite3
from contextlib import closing
from pathlib import Path
from urllib.parse import quote

import pytest

SNAPSHOT_VERSION = "bank-fixture-r1"

# 自造题目的固定 qid（只存在于本 fixture 的临时题库里，不依赖任何基线题目）。
MAIN_QID = "fixture0001:1"  # 干净单选，答案 D，无题组
QUARANTINED_QID = "fixture0001:10"  # 数据侧隔离
CONTEXT_QID = "fixture0001:11"  # context 上下文成员
REVIEW_QID = "fixture0001:12"  # review（判 pending）
PENDING_QID = "fixture0001:13"  # grading_status=pending
LEAK_QID = "fixture0001:2"  # 公开题干混入答案解析
GROUP_DOC_ID = "fixturegroup"
GROUP_ID = GROUP_DOC_ID + ":m_0002"
GROUP_QID = GROUP_DOC_ID + ":14"  # 题组成员，答案 B
GROUP_REVIEW_QID = GROUP_DOC_ID + ":15"  # 题组成员（review）
QUARANTINE_MARKER = "隔离门禁标记题"
CONTEXT_MARKER = "上下文门禁标记题"
LEAK_MARKER = "答案泄漏门禁标记题"
SUBJECT = "英语"
GROUP_SUBJECT = "语文"
EXPECTED_QUESTIONS = 8
EXPECTED_UNAVAILABLE = 2  # context + quarantined，不计入可练总数

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


def _options_json():
    """旧列 options_json：纯文本选项，不含「A. 」前缀（避免被当作选项合并而暂停判分）。"""
    return [{"label": label, "text": f"选项{label}"} for label in "ABCD"]


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


def _copy_schema(source: Path, destination: Path) -> None:
    """只复制基线库的表/索引 DDL；fixture 数据完全自造（不复制任何真实题目）。"""
    with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True, timeout=20)) as source_con:
        tables = [row[0] for row in source_con.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND sql IS NOT NULL ORDER BY name")]
        indexes = [row[0] for row in source_con.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND sql IS NOT NULL ORDER BY name")]
    assert any("CREATE TABLE questions" in sql for sql in tables), "基线题库缺少 questions 表"
    assert any("CREATE TABLE groups" in sql for sql in tables), "基线题库缺少 groups 表"
    with closing(sqlite3.connect(destination)) as con:
        for statement in [*tables, *indexes]:
            con.execute(statement)
        con.commit()


def _insert_row(con, table, values):
    """按目标表实际列插入：老/新基线 schema 差异由列集合自适应。"""
    columns = [row[1] for row in con.execute(f'PRAGMA table_info("{table}")')]
    marks = ", ".join("?" for _ in columns)
    names = ", ".join(f'"{column}"' for column in columns)
    con.execute(f'INSERT INTO "{table}" ({names}) VALUES ({marks})',
                [values.get(column) for column in columns])


def _question_row(qid, *, doc_id="fixture0001", subject=SUBJECT, **values):
    """一条自造题目：默认是「无题组单选」（新列全部显式赋值，不依赖基线旧列）。"""
    row = {
        "qid": qid, "question_id": qid, "subject": subject, "year": 2024,
        "province": "全国", "paper_type": "模拟", "paper_name": f"{subject}测试卷",
        "doc_id": doc_id, "exam_id": doc_id, "section_title": None, "section_kind": None,
        "number_label": "1", "order_in_paper": 1, "type": "single_choice",
        "objective": 1, "stem_plain": "", "options_json": "[]",
        "answer_text": None, "answer_kind": "choice", "answer_note": None,
        "solution": "", "binding_method": None, "group_id": None, "group_role": None,
        "has_figure": 0, "has_formula": 0, "images_json": "[]", "source_blocks_json": "[]",
        "tier": None, "bank_version": SNAPSHOT_VERSION,
        "source_question_id": "q_" + qid.replace(":", "_"),
        "parent_source_question_id": None, "group_kind": None,
        "practice_status": "practice", "grading_status": "auto",
        "content_revision": None, "answer_revision": None, "pua_remaining": 0,
        "stem_blocks_json": "[]", "options_blocks_json": "[]", "answer_blocks_json": "[]",
        "solution_blocks_json": "[]", "shared_blocks_json": "[]",
        "member_index_json": "[]", "quality_flags_json": "[]",
    }
    row.update(values)
    return row


def _group_payload():
    """题组的共用块、成员索引与题目行必须一致（详情页与组接口共用这份读模型）。"""
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
            "qid": GROUP_REVIEW_QID, "in_bank": True, "source_question_id": "q_group_15",
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
    return shared_blocks, members


def _seed_snapshot(con):
    """清空复制来的 schema 中的任何数据，写入自造题库（共 EXPECTED_QUESTIONS 题）。"""
    for table in ("question_images", "images", "papers", "groups", "questions"):
        con.execute(f'DELETE FROM "{table}"')

    main_quality = [{"code": "FORMULA_FALLBACK_ONLY", "message": "公式仅有原卷图，按图显示"}]
    main_stem = "1.—I’m sorry I didn’t finish it on time."
    _insert_row(con, "questions", _question_row(
        MAIN_QID,
        stem_plain=main_stem,
        options_json=_dump(_options_json()),
        answer_text="D",
        solution="解析：选 D。",
        content_revision="c0ffee123456",
        answer_revision="feedbeef7890",
        stem_blocks_json=_dump([
            _block("b900001", main_stem),
            _block("b900002", "语法填空 x_{1} + y^{2}", order=1, kind="math"),
        ]),
        options_blocks_json=_dump(_options_blocks()),
        answer_blocks_json=_dump([_block("b900020", "D")]),
        solution_blocks_json=_dump([_block("b900021", "解析：选 D。")]),
        quality_flags_json=_dump(main_quality),
    ))
    _insert_row(con, "questions", _question_row(
        QUARANTINED_QID, number_label="10", order_in_paper=10,
        practice_status="quarantined", grading_status="disabled", pua_remaining=3,
        stem_plain=QUARANTINE_MARKER + "，整段乱码无法还原",
        quality_flags_json=_dump([
            {"code": "PUA_MOJIBAKE_QUARANTINED", "message": "整段乱码，无法还原，整题隔离"},
        ]),
    ))
    _insert_row(con, "questions", _question_row(
        CONTEXT_QID, number_label="11", order_in_paper=11,
        practice_status="context", grading_status="disabled",
        stem_plain=CONTEXT_MARKER + "仅供阅读",
        stem_blocks_json=_dump([_block("b900011", CONTEXT_MARKER)]),
    ))
    _insert_row(con, "questions", _question_row(
        REVIEW_QID, number_label="12", order_in_paper=12,
        practice_status="review", grading_status="auto",
        options_json=_dump(_options_json()),
        options_blocks_json=_dump(_options_blocks()),
        answer_text="B",
    ))
    _insert_row(con, "questions", _question_row(
        PENDING_QID, number_label="13", order_in_paper=13,
        practice_status="practice", grading_status="pending",
        options_json=_dump(_options_json()),
        options_blocks_json=_dump(_options_blocks()),
        answer_text="C",
        quality_flags_json=_dump([
            {"code": "ANSWER_MISSING", "message": "参考答案缺失，判分待核验"},
        ]),
    ))
    leak_stem = LEAK_MARKER + "【答案】B 【详解】解析内容混入题干"
    _insert_row(con, "questions", _question_row(
        LEAK_QID, number_label="2", order_in_paper=2,
        stem_plain=leak_stem,
        stem_blocks_json=_dump([_block("b900030", leak_stem)]),
        options_json=_dump(_options_json()),
        options_blocks_json=_dump(_options_blocks()),
        answer_text="B",
    ))

    shared_blocks, members = _group_payload()
    _insert_row(con, "questions", _question_row(
        GROUP_QID, doc_id=GROUP_DOC_ID, subject=GROUP_SUBJECT, year=2000,
        group_id="m_0002", group_role="sub",
        number_label="14", order_in_paper=14,
        parent_source_question_id="p_q_0025", group_kind="material",
        stem_plain="14.下列各个句子在文中的意思，不正确的一项是",
        stem_blocks_json=_dump([_block("b900120", "14.下列各个句子在文中的意思，不正确的一项是")]),
        options_json=_dump(_options_json()),
        options_blocks_json=_dump(_options_blocks()),
        answer_text="B",
        solution="解析：选 B。",
        answer_blocks_json=_dump([_block("b900121", "B")]),
        solution_blocks_json=_dump([_block("b900122", "解析：选 B。")]),
        shared_blocks_json=_dump(shared_blocks),
        member_index_json=_dump(members),
        content_revision="c0ffee123456",
        answer_revision="feedbeef7890",
    ))
    _insert_row(con, "questions", _question_row(
        GROUP_REVIEW_QID, doc_id=GROUP_DOC_ID, subject=GROUP_SUBJECT, year=2000,
        group_id="m_0002", group_role="sub",
        number_label="15", order_in_paper=15,
        stem_plain="15.下列各句中，没有语病的一项是",
        options_json=_dump(_options_json()),
        options_blocks_json=_dump(_options_blocks()),
        answer_text="B",
        practice_status="review",
        grading_status="auto",
    ))
    _insert_row(con, "groups", {
        "group_id": GROUP_ID, "doc_id": GROUP_DOC_ID, "subject": GROUP_SUBJECT,
        "year": 2000, "province": "全国", "paper_name": "语文测试卷",
        "section_title": None, "material_kind": "material",
        "material_text": "材料正文：阅读下面的文字，完成 14～16 题。",
        "parent_label": "14", "parent_type": "material",
        "parent_stem": "父题说明：下列各句中，加点词的意义和用法相同的一项是",
        "n_children_total": 3, "n_children_strict": 2, "children_json": "[]",
        "bank_version": SNAPSHOT_VERSION, "group_kind": "material",
        "parent_source_question_id": None,
        "shared_blocks_json": _dump(shared_blocks),
        "member_index_json": _dump(members),
        "n_members": 3, "n_practice": 2,
    })


@pytest.fixture(scope="module")
def snapshot_bank(tmp_path_factory):
    """基线 schema + 自造数据的临时快照题库（不复制真实题目，不依赖固定 qid）。"""
    from app import db

    source = db.BANK_DIR / "题库.db"
    assert source.is_file(), f"missing baseline bank: {source}"
    root = tmp_path_factory.mktemp("snapshot-bank")
    destination = root / "题库.db"
    _copy_schema(source, destination)
    with closing(sqlite3.connect(destination)) as con:
        _alter(con, "questions", QUESTION_COLUMNS)
        _alter(con, "groups", GROUP_COLUMNS)
        _seed_snapshot(con)
        con.commit()
    material_dir = root / GROUP_SUBJECT
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
    # 列表摘要同样读取新列且不泄漏答案；隔离/上下文/泄密题不进常规检索（共 3 道可练题）。
    listing = snapshot_client.get("/api/questions", params={"subject": SUBJECT, "page_size": 3})
    assert listing.status_code == 200, listing.text
    body = listing.json()
    assert body["total"] == 3
    items = body["items"]
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

    # 目录计数来自自造题库：context/quarantined 不计入可练总数。
    catalog = snapshot_client.get("/api/catalog").json()
    assert catalog["source_total"] == EXPECTED_QUESTIONS
    assert catalog["available_total"] == EXPECTED_QUESTIONS - EXPECTED_UNAVAILABLE
    assert catalog["quarantined_count"] == EXPECTED_UNAVAILABLE
    assert catalog["available_total"] + catalog["quarantined_count"] == catalog["source_total"]
    assert SUBJECT in catalog["subjects"] and GROUP_SUBJECT in catalog["subjects"]


def test_snapshot_group_endpoint_and_member_progress(snapshot_client, second_client):
    url = "/api/groups/" + quote(GROUP_ID, safe="")
    response = snapshot_client.get(url)
    assert response.status_code == 200, response.text
    group = response.json()
    assert group["group_id"] == GROUP_ID
    assert group["bank_version"] == SNAPSHOT_VERSION
    assert group["subject"] == GROUP_SUBJECT
    assert int(group["year"]) == 2000
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
    search = snapshot_client.get("/api/questions", params={"q": LEAK_MARKER})
    assert search.status_code == 200
    assert search.json()["total"] == 0
    # 未混入答案的题目不受影响
    assert snapshot_client.get(_detail_url(MAIN_QID)).status_code == 200
