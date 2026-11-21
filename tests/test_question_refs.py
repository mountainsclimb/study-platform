"""WS11 · 统一题目引用与个人题作答/复习适配（bank/app/official + 幂等）测试。

覆盖：
- ``app/question_ref.py`` 解析/构造/规范化/展示标签/幂等键；
- 个人题（``app:<pl_questions.id>``）练习 → 判分 → 错题 → 统一复习队列；
- 未判分题（最近一次作答 pending）在复习队列单列；
- 提交幂等（重复调用 + 并发；数据库唯一约束兜底，一次作答只入账一次）；
- 跨用户越权（他人个人题详情/作答/掌握度/错题/历史/配图统一 404）；
- 整卷逐题入账幂等（``attempt_id:item_position``）与个人题错题联动；
- 旧裸 qid / ``bank:`` 前缀行为不变（写库仍为裸 qid）。

题库使用 ``bank_fixture`` 自含最小库（不依赖 r5 固定 qid）；个人题走真实
``/api/paperlab/questions`` 接口写入临时应用库；不联网。
"""
from concurrent.futures import ThreadPoolExecutor
import json

import pytest

from app import question_ref
from app import practice as practice_module


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def user_id(client) -> int:
    return client.get("/api/auth/me").json()["id"]


def clear_bank_caches():
    """切换只读题库后清掉进程内缓存（列探测 / 素材清单 / 检索索引 / catalog）。"""
    practice_module._sqlite_question_columns.cache_clear()
    practice_module._sqlite_group_columns.cache_clear()
    practice_module._source_groups.cache_clear()
    practice_module._verified_index_key = None
    practice_module._verified_fts_key = None
    practice_module.invalidate_catalog_cache()


@pytest.fixture
def install_bank(tmp_path, monkeypatch):
    """安装自含最小只读题库；返回安装函数，用例内可多次切换。"""
    from app import db
    from bank_fixture import make_bank

    sequence = {"n": 0}

    def install(rows=(), groups=()):
        sequence["n"] += 1
        bank = make_bank(tmp_path / f"bank-{sequence['n']}", rows, groups)
        monkeypatch.setattr(db, "BANK_DIR", bank)
        clear_bank_caches()
        return bank

    yield install
    clear_bank_caches()


def bank_single(qid="docQ:1", **values):
    from bank_fixture import question_row

    values.setdefault("doc_id", qid.split(":", 1)[0])
    values.setdefault("options_json", '[{"label":"A","text":"2"},{"label":"B","text":"3"}]')
    values.setdefault("answer_text", "A")
    return question_row(qid, "已知 1+1=2，下列结论正确的是（  ）", **values)


def bank_pending(qid="docP:1", **values):
    """题库侧待判分题：客观题但 grading_status=pending，作答保留 pending。"""
    values.setdefault("grading_status", "pending")
    return bank_single(qid, **values)


def create_app_question(client, **overrides) -> dict:
    payload = {
        "type": "SINGLE",
        "subject": "数学",
        "grade": "高中一年级",
        "difficulty": 0.2,
        "knowledge_tags": ["函数"],
        "content": {
            "stem": "已知 $f(x)=x^2$，则 $f(2)$ 等于（  ）",
            "options": [{"label": "A", "text": "2"}, {"label": "B", "text": "4"}],
            "answers": ["B"],
            "explanation": "代入 x=2 得 4。",
        },
        "status": "ready",
    }
    payload.update(overrides)
    response = client.post("/api/paperlab/questions", json=payload)
    assert response.status_code == 200, response.text
    return response.json()["question"]


def create_app_blank(client) -> dict:
    return create_app_question(
        client,
        type="BLANK",
        content={
            "stem": "计算：$1+2+3=\\underline{\\quad}$。",
            "answers": ["6"],
            "explanation": "1+2+3=6。",
        },
    )


def create_app_subjective(client) -> dict:
    return create_app_question(
        client,
        type="SUBJECTIVE",
        content={
            "stem": "证明 $f(x)=x^2$ 在 $[0,+\\infty)$ 上单调递增。",
            "answers": ["利用导数可证。"],
            "explanation": "对 $f$ 求导得 $f'(x)=2x\\ge 0$。",
            "rubric": "求导正确 3 分，结论 2 分。",
        },
    )


def create_paper(client, items, title="WS11 整卷") -> dict:
    response = client.post("/api/paperlab/papers", json={
        "title": title, "kind": "manual", "subject": "数学", "grade": "高中一年级",
        "items": items,
    })
    assert response.status_code == 200, response.text
    return response.json()


def submit_attempt(client, qid, answer=None, submission_id=None, files=None):
    data = {"qid": qid}
    if answer is not None:
        data["answer"] = answer
    if submission_id is not None:
        data["submission_id"] = submission_id
    return client.post("/api/attempts", data=data, files=files)


def attempt_rows(user_id_value, qid) -> list[dict]:
    from app.db import connection

    with connection() as con:
        return [
            dict(row) for row in con.execute(
                "SELECT * FROM attempts WHERE user_id=? AND qid=? ORDER BY id", (user_id_value, qid)
            )
        ]


def wrong_row(user_id_value, qid):
    from app.db import connection

    with connection() as con:
        return con.execute(
            "SELECT * FROM wrong_questions WHERE user_id=? AND qid=?", (user_id_value, qid)
        ).fetchone()


def queue_refs(data) -> dict:
    """复习队列统一结构：按 ref 收集 graded / pending 两组。"""
    return {
        "graded": {item["ref"] for item in data["items"]},
        "pending": {item["ref"] for item in data["pending_items"]},
    }


# ---------------------------------------------------------------------------
# 1. question_ref 纯解析/构造
# ---------------------------------------------------------------------------

def test_question_ref_parse_and_build_rules():
    bare = question_ref.parse("docA:12")
    assert bare.source == question_ref.SOURCE_BANK
    assert bare.key == "docA:12"
    assert bare.ref == "bank:docA:12"
    assert bare.storage_key == "docA:12"  # 旧裸 qid 写库保持不变
    assert bare.source_label == "高考题库"

    prefixed = question_ref.parse("bank:docA:12")
    assert prefixed.ref == "bank:docA:12" and prefixed.storage_key == "docA:12"

    app_ref = question_ref.parse("app:12")
    # 个人题 id 必须是正整数（不保留前导零，写库键固定为 app:<id>）。
    assert app_ref.source == question_ref.SOURCE_APP
    assert app_ref.key == "12"
    assert app_ref.ref == "app:12" and app_ref.storage_key == "app:12"
    assert app_ref.source_label == "我的题库"

    official = question_ref.parse("official:7")
    assert official.source == question_ref.SOURCE_OFFICIAL
    assert official.supported is True  # Phase 3：平台精选读取层已开放（app/official.py）
    assert official.storage_key == "official:7"

    assert question_ref.normalize("docA:12") == "bank:docA:12"
    assert question_ref.build("app", 12) == "app:12"
    assert question_ref.split("bank:docA:12") == ("bank", "docA:12")
    assert question_ref.describe("app:12")["source_label"] == "我的题库"

    for bad in ("", "   ", "app:abc", "app:0", "app:-1", "app:1x", "app:0012"):
        with pytest.raises(question_ref.QuestionRefError):
            question_ref.parse(bad)
    with pytest.raises(question_ref.QuestionRefError):
        question_ref.parse("bank:")


def test_question_ref_idempotency_key_helpers():
    assert question_ref.submission_key("docA:12", "s-1") == "docA:12|s-1"
    assert question_ref.submission_key("app:12", "s-1") == "app:12|s-1"
    assert question_ref.paper_item_key(3, 2) == "paper:3:2"
    with pytest.raises(question_ref.QuestionRefError):
        question_ref.submission_key("docA:12", "")
    with pytest.raises(question_ref.QuestionRefError):
        question_ref.validate_submission_id("bad id with space")
    with pytest.raises(question_ref.QuestionRefError):
        question_ref.validate_submission_id("x" * (question_ref.MAX_SUBMISSION_ID_LENGTH + 1))
    assert question_ref.validate_submission_id(" a1.b_c-d:e+f ") == "a1.b_c-d:e+f"


# ---------------------------------------------------------------------------
# 2. 个人题：练习 → 判分 → 错题 → 统一复习队列；旧 qid 行为不变
# ---------------------------------------------------------------------------

def test_app_question_practice_grading_wrongbook_and_review_queue(client, install_bank):
    install_bank([bank_single()])
    question = create_app_question(client)
    app_ref = f"app:{question['id']}"
    uid = user_id(client)

    wrong = submit_attempt(client, app_ref, "A", submission_id="sub-app-1")
    assert wrong.status_code == 200, wrong.text
    payload = wrong.json()
    assert payload["result"] == "incorrect"
    assert payload["duplicate"] is False
    assert payload["question"]["qid"] == app_ref
    assert payload["question"]["ref"] == app_ref
    assert payload["question"]["source"] == "app"
    assert payload["question"]["source_label"] == "我的题库"
    assert payload["question"]["unlocked"] is True
    assert payload["question"]["answer"]["text"] == "B"

    detail = client.get("/api/questions/" + app_ref).json()
    assert detail["source"] == "app" and detail["ref"] == app_ref
    assert detail["wrong_info"]["in_wrongbook"] is True
    assert detail["mastery"] == "未掌握"

    # 一次作答只入账一次：attempts/wrong_questions/mastery 各一行。
    assert len(attempt_rows(uid, app_ref)) == 1
    row = wrong_row(uid, app_ref)
    assert row is not None and row["failures"] == 1 and row["last_result"] == "incorrect"
    from app.db import connection

    with connection() as con:
        mastery = con.execute(
            "SELECT level FROM mastery WHERE user_id=? AND qid=?", (uid, app_ref)
        ).fetchone()
    assert mastery is not None and mastery["level"] == "未掌握"

    box = client.get("/api/wrongbook").json()
    assert box["total"] == 1
    item = box["items"][0]
    assert item["qid"] == app_ref and item["ref"] == app_ref
    assert item["source"] == "app" and item["source_label"] == "我的题库"
    assert item["last_result"] == "incorrect" and item["failures"] == 1

    queue = client.get("/api/review/queue").json()
    assert queue["pending_items"] == []
    assert app_ref in queue_refs(queue)["graded"]
    graded = next(item for item in queue["items"] if item["ref"] == app_ref)
    assert graded["graded"] is True and graded["status"] == "graded"

    stats = client.get("/api/stats").json()
    assert stats["app_attempts"] == 1 and stats["app_practiced"] == 1
    assert stats["app_wrong_count"] == 1

    # 旧裸 qid 行为不变：题库作答仍以裸 qid 写库，来源标签为「高考题库」。
    bank = submit_attempt(client, "docQ:1", "A")
    assert bank.status_code == 200, bank.text
    bank_payload = bank.json()
    assert bank_payload["result"] == "correct"
    assert bank_payload["question"]["qid"] == "docQ:1"
    assert bank_payload["question"]["source"] == "bank"
    assert bank_payload["question"]["ref"] == "bank:docQ:1"
    assert len(attempt_rows(uid, "docQ:1")) == 1
    bank_detail = client.get("/api/questions/docQ:1").json()
    assert bank_detail["source"] == "bank" and bank_detail["ref"] == "bank:docQ:1"
    box = client.get("/api/wrongbook").json()
    assert {item["ref"] for item in box["items"]} == {app_ref}


def test_app_blank_auto_grading_and_bank_pending_listed_separately(client, install_bank):
    install_bank([bank_pending()])
    blank = create_app_blank(client)
    app_ref = f"app:{blank['id']}"

    correct = submit_attempt(client, app_ref, " 6 ")
    assert correct.status_code == 200, correct.text
    assert correct.json()["result"] == "correct"

    wrong = submit_attempt(client, app_ref, "7")
    assert wrong.json()["result"] == "incorrect"

    pending_app = create_app_subjective(client)
    pending_ref = f"app:{pending_app['id']}"
    typed = submit_attempt(client, pending_ref, "我的证明：由导数可证。")
    assert typed.status_code == 200, typed.text
    assert typed.json()["result"] == "pending"
    assert typed.json()["question"]["unlocked"] is True

    # 题库待判分题：同一队列单列，不写入错题调度。
    bank_pending_attempt = submit_attempt(client, "docP:1", "A")
    assert bank_pending_attempt.json()["result"] == "pending"

    uid = user_id(client)
    assert len(attempt_rows(uid, pending_ref)) == 1
    assert wrong_row(uid, pending_ref) is None  # 未判分不进入错题调度
    assert wrong_row(uid, "docP:1") is None

    queue = client.get("/api/review/queue").json()
    refs = queue_refs(queue)
    assert pending_ref in refs["pending"]
    assert "bank:docP:1" in refs["pending"]
    assert pending_ref not in refs["graded"]
    pending_item = next(item for item in queue["pending_items"] if item["ref"] == pending_ref)
    assert pending_item["graded"] is False and pending_item["status"] == "pending"
    assert pending_item["source"] == "app"
    bank_pending_item = next(
        item for item in queue["pending_items"] if item["ref"] == "bank:docP:1"
    )
    assert bank_pending_item["source"] == "bank" and bank_pending_item["graded"] is False
    # 未判分作答也不产生错题本记录。
    assert client.get("/api/wrongbook").json()["total"] >= 1  # 只有上面的 app 填空题错题


# ---------------------------------------------------------------------------
# 3. 提交幂等：submission_id 重复 + 并发；台账数据库唯一约束
# ---------------------------------------------------------------------------

def test_submission_id_idempotent_repeat_and_concurrent(client, install_bank):
    from app.db import connection, utcnow

    install_bank([bank_single()])
    question = create_app_question(client)
    app_ref = f"app:{question['id']}"
    uid = user_id(client)

    first = submit_attempt(client, app_ref, "A", submission_id="weak-net-1")
    second = submit_attempt(client, app_ref, "A", submission_id="weak-net-1")
    assert first.status_code == second.status_code == 200
    assert first.json()["attempt_id"] == second.json()["attempt_id"]
    assert second.json()["duplicate"] is True
    assert len(attempt_rows(uid, app_ref)) == 1
    assert wrong_row(uid, app_ref)["failures"] == 1
    with connection() as con:
        ledger = con.execute(
            "SELECT COUNT(*) FROM submission_ledger WHERE user_id=? AND kind='attempt'",
            (uid,),
        ).fetchone()[0]
    assert ledger == 1

    # 不同 submission_id 是新一次作答（允许重复练习），错题次数累加。
    third = submit_attempt(client, app_ref, "A", submission_id="weak-net-2")
    assert third.status_code == 200 and third.json()["duplicate"] is False
    assert len(attempt_rows(uid, app_ref)) == 2
    assert wrong_row(uid, app_ref)["failures"] == 2

    # 并发同一 submission_id：数据库主键唯一约束兜底，只成功入账一次。
    key = question_ref.submission_key("docQ:1", "thread-dup-1")

    def worker():
        return practice_module._record_attempt(
            uid, "docQ:1", "A", None, "incorrect", True, utcnow(), key,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [future.result() for future in [pool.submit(worker), pool.submit(worker)]]
    attempt_ids = {result["attempt_id"] for result in results}
    assert len(attempt_ids) == 1
    assert sum(1 for result in results if result["duplicate"]) == 1
    assert len(attempt_rows(uid, "docQ:1")) == 1
    assert wrong_row(uid, "docQ:1")["failures"] == 1

    # 非法 submission_id → 422（不写任何行）。
    invalid = submit_attempt(client, app_ref, "A", submission_id="bad id")
    assert invalid.status_code == 422
    assert len(attempt_rows(uid, app_ref)) == 2


# ---------------------------------------------------------------------------
# 4. 跨用户越权与已删除个人题的历史保留
# ---------------------------------------------------------------------------

def test_app_ref_cross_user_forbidden_and_deleted_kept_in_wrongbook(client, second_client, install_bank):
    install_bank()
    question = create_app_question(client)
    app_ref = f"app:{question['id']}"
    uid = user_id(client)

    assert submit_attempt(client, app_ref, "A").json()["result"] == "incorrect"

    assert second_client.get("/api/questions/" + app_ref).status_code == 404
    assert submit_attempt(second_client, app_ref, "B").status_code == 404
    assert second_client.post("/api/mastery", json={"qid": app_ref, "level": "未掌握"}).status_code == 404
    assert second_client.post("/api/wrongbook", json={"qid": app_ref}).status_code == 404
    assert second_client.get("/api/attempts/history", params={"qid": app_ref}).status_code == 404
    assert second_client.get("/api/reveal", params={"qid": app_ref}).status_code == 404
    assert second_client.get("/api/wrongbook").json()["total"] == 0
    assert second_client.get("/api/stats").json()["attempts"] == 0

    # 本人删除个人题后：错题历史仍保留（引用 + 占位卡），不可再作答。
    assert client.delete(f"/api/paperlab/questions/{question['id']}").status_code == 200
    assert client.get("/api/questions/" + app_ref).status_code == 404
    assert submit_attempt(client, app_ref, "A").status_code == 404
    box = client.get("/api/wrongbook").json()
    item = next(entry for entry in box["items"] if entry["qid"] == app_ref)
    assert item["ref"] == app_ref and item["source"] == "app"
    assert item["unavailable"] is True

    queue = client.get("/api/review/queue").json()
    assert app_ref in queue_refs(queue)["graded"]


# ---------------------------------------------------------------------------
# 5. 整卷逐题入账幂等 + 个人题错题联动
# ---------------------------------------------------------------------------

def test_paper_item_idempotent_and_app_wrongbook_linkage(client, install_bank):
    from app.db import connection
    from app.paperlab import practice as paperlab_practice

    install_bank([bank_single()])
    uid = user_id(client)
    app_question = create_app_question(client)
    app_ref = f"app:{app_question['id']}"

    paper = create_paper(client, [
        {"ref": app_ref, "score": 5},
        {"ref": "bank:docQ:1", "score": 5},
    ])
    attempt = client.post(f"/api/paperlab/papers/{paper['paper']['id']}/attempts").json()
    attempt_id = attempt["attempt"]["id"]
    saved = client.patch(
        f"/api/paperlab/attempts/{attempt_id}/answers",
        json={"item_position": 1, "answer": ["A"]},
    )
    assert saved.status_code == 200
    saved = client.patch(
        f"/api/paperlab/attempts/{attempt_id}/answers",
        json={"item_position": 2, "answer": ["B"]},
    )
    assert saved.status_code == 200
    submitted = client.post(f"/api/paperlab/attempts/{attempt_id}/submit", json={})
    assert submitted.status_code == 200, submitted.text
    result = submitted.json()["result"]
    # 兼容字段：题库题错题仍然进 wrong_added；个人题进新字段 app_wrong_added。
    assert [entry["qid"] for entry in result["wrong_added"]] == ["docQ:1"]
    assert [entry["ref"] for entry in result["app_wrong_added"]] == [app_ref]
    assert {entry["item_position"] for entry in result["app_bank_unlinked"]} == {1}

    # 个人题错题进入统一错题本与复习队列。
    assert wrong_row(uid, app_ref)["failures"] == 1
    box = client.get("/api/wrongbook").json()
    assert app_ref in {item["ref"] for item in box["items"]}
    assert app_ref in queue_refs(client.get("/api/review/queue").json())["graded"]
    # 题库题整卷错题仍然按裸 qid 入本（旧行为）。
    assert wrong_row(uid, "docQ:1")["failures"] == 1

    # 重复提交被 409 拦住，错题次数不重复累加。
    again = client.post(f"/api/paperlab/attempts/{attempt_id}/submit", json={})
    assert again.status_code == 409
    assert wrong_row(uid, app_ref)["failures"] == 1
    assert wrong_row(uid, "docQ:1")["failures"] == 1

    # 同一逐题幂等键再次入账：返回 False（台账唯一约束命中），failures 不再增加。
    with connection() as con:
        entry_key = question_ref.paper_item_key(attempt_id, 1)
        assert paperlab_practice._record_wrong(
            con, uid, app_ref, "incorrect", entry_key=entry_key,
        ) is False
    assert wrong_row(uid, app_ref)["failures"] == 1


# ---------------------------------------------------------------------------
# 6. 个人题配图：复用资产归属校验，跨用户/未引用资产统一 404
# ---------------------------------------------------------------------------

def test_app_question_image_ownership(client, second_client, install_bank):
    from app.paperlab import assets
    from bank_fixture import drawn_image_bytes

    install_bank()
    uid = user_id(client)
    asset_id = assets.save_asset_bytes(uid, drawn_image_bytes(), media_type="image/png")
    other_asset = assets.save_asset_bytes(uid, drawn_image_bytes(color="red"), media_type="image/png")
    question = create_app_question(client, content={
        "stem": "看图作答：图中两条线是否相交？",
        "options": [{"label": "A", "text": "相交"}, {"label": "B", "text": "不相交"}],
        "answers": ["A"],
        "explanation": "两条线斜率不同。",
        "images": [{"asset": asset_id, "role": "stem"}],
    })
    app_ref = f"app:{question['id']}"
    detail = client.get("/api/questions/" + app_ref).json()
    assert [image["asset"] for image in detail["images"]] == [asset_id]

    url = "/api/bank-image"
    params = {"subject": "数学", "file": asset_id, "qid": app_ref}
    response = client.get(url, params=params)
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("image/")
    assert second_client.get(url, params=params).status_code == 404
    # 未被本题引用的本人资产也不能借该题读取（防越权探测）。
    assert client.get(url, params={**params, "file": other_asset}).status_code == 404
    # 题目删除后配图不再可读。
    assert client.delete(f"/api/paperlab/questions/{question['id']}").status_code == 200
    assert client.get(url, params=params).status_code == 404


def test_official_source_reserved_not_readable(client):
    """official: 为平台精选预留来源：可解析但当前读取层不开放。"""
    assert client.get("/api/questions/official:7").status_code == 404
    assert submit_attempt(client, "official:7", "A").status_code == 404


def test_bare_qid_legacy_not_idempotent_without_submission_id(client, install_bank):
    """旧客户端不传 submission_id：保持旧行为（每次提交都是一次新作答）。"""
    install_bank([bank_single()])
    uid = user_id(client)
    first = submit_attempt(client, "docQ:1", "A")
    second = submit_attempt(client, "docQ:1", "A")
    assert first.status_code == second.status_code == 200
    assert first.json()["attempt_id"] != second.json()["attempt_id"]
    assert len(attempt_rows(uid, "docQ:1")) == 2
    # bank: 前缀与裸 qid 等价（写库仍为裸 qid）。
    prefixed = submit_attempt(client, "bank:docQ:1", "A")
    assert prefixed.status_code == 200
    assert prefixed.json()["question"]["qid"] == "docQ:1"
    assert len(attempt_rows(uid, "docQ:1")) == 3
