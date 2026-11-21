"""试卷工坊 · 整卷练习与答题卡判分接入共享学习证据（方案 §4 / §8 工单 C）的隔离测试。

覆盖：
- 整卷提交后逐题写入 ``paper_attempt`` 证据（与 ``study_evidence.question_evidence`` 的
  counts / items 对应，pending 题如实待核验，question_ref 使用统一引用）；
- 事件键 ``paper:item:<attempt_id>:<position>`` 幂等：同键重复写入只有一条有效证据，
  重判走 ``revise_event`` 升版不新增有效证据；
- 证据模块异常（record_event 抛错）时整卷提交仍 200、判分结果不退化；
- 答题卡确认后逐题写入 ``paper_scan`` 证据；无可核对参考答案的题位 verdict=pending 且
  meta.note 为中文说明，不伪造对错；重复确认 409 后证据不重复；
- 两账号证据隔离（A 的工坊证据在 B 的 question_evidence 中不可见）；
- 重复提交 409 后证据条数不变。

全部使用 ``isolated_db`` 临时应用库 + 应用题库临时数据，不联网、不依赖浏览器渲染
（答题卡用例直接在库内构造「已识别」扫描记录，再走真实 confirm 判分事务）。
"""
import json

from app import db, study_evidence
from app.paperlab import papers as paper_store
from app.paperlab import practice as paperlab_practice
from app.paperlab import scan as paperlab_scan


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------

def user_id(client) -> int:
    return client.get("/api/auth/me").json()["id"]


def create_question(client, *, qtype="SINGLE", content=None) -> dict:
    if content is None:
        content = {
            "stem": "已知 $f(x)=x^2$，则 $f(2)$ 等于（  ）",
            "options": [{"label": "A", "text": "2"}, {"label": "B", "text": "4"}],
            "answers": ["B"],
            "explanation": "代入 x=2 得 4。",
        }
    response = client.post("/api/paperlab/questions", json={
        "type": qtype, "subject": "数学", "grade": "高中一年级", "difficulty": 0.2,
        "knowledge_tags": ["函数"], "content": content, "status": "ready",
    })
    assert response.status_code == 200, response.text
    return response.json()["question"]


def create_paper(client, items, title="学习证据测试卷") -> dict:
    response = client.post("/api/paperlab/papers", json={
        "title": title, "kind": "manual", "subject": "数学", "grade": "高中一年级",
        "items": items,
    })
    assert response.status_code == 200, response.text
    return response.json()


def create_card(client, paper_id: int) -> dict:
    response = client.post(f"/api/paperlab/papers/{paper_id}/cards", json={
        "preset": "A4", "columns": 2, "bubble": "circle", "include_subjective": True,
        "student_no": {"digits": 6, "bubble": True},
    })
    assert response.status_code == 200, response.text
    return response.json()


def start_attempt(client, paper_id: int) -> dict:
    response = client.post(f"/api/paperlab/papers/{paper_id}/attempts")
    assert response.status_code == 200, response.text
    return response.json()


def submit_attempt(client, attempt_id: int, answers=None):
    return client.post(f"/api/paperlab/attempts/{attempt_id}/submit",
                       json={"answers": answers or []})


def evidence_rows(user_id_: int, *, event_key=None, kind=None) -> list[dict]:
    sql = "SELECT * FROM study_evidence WHERE user_id=?"
    params: list = [user_id_]
    if event_key is not None:
        sql += " AND event_key=?"
        params.append(event_key)
    if kind is not None:
        sql += " AND kind=?"
        params.append(kind)
    sql += " ORDER BY id"
    with db.connection() as con:
        return [dict(row) for row in con.execute(sql, params).fetchall()]


def count_evidence(user_id_: int, *, kind=None) -> int:
    return len(evidence_rows(user_id_, kind=kind))


def craft_scan_row(client, *, paper_id: int, card: dict, layout: dict,
                   answers: dict[int, list[str]]) -> int:
    """在库内构造一条「已识别」答题卡扫描记录（等价于识别成功后的 result_json）。

    不依赖浏览器渲染合成图：确认接口只读取 result_json、页面状态与版面签名，
    这里按真实识别结果的结构补齐，再走 ``POST /scans/{id}/confirm`` 的真实判分事务。
    """
    uid = user_id(client)
    items_raw = paper_store.list_items(paper_id, uid)
    item_map = paperlab_scan._layout_item_map(items_raw, layout)
    graded = paperlab_scan._grade_scan(item_map, answers)
    page_count = paperlab_scan._page_count(layout)
    result = {
        "schema": "zhixu-card-scan-v1",
        "page_count": page_count,
        "pages": [
            {"index": index + 1, "status": "ok", "page_no": index + 1}
            for index in range(page_count)
        ],
        "answers": graded["answers"],
        "questions": graded["questions"],
        "unreadable_items": [],
        "student_no": {"value": None, "conflict": False, "values": [], "digits": None},
        "missing_pages": [],
        "duplicate_pages": [],
        "score": graded["score"],
        "max_score": graded["max_score"],
        "summary": graded["summary"],
        "confirmed": False,
        "confirmed_at": None,
        "attempt_id": None,
        "card_signature": int((layout.get("template") or {}).get("signature") or 0),
    }
    now = db.utcnow()
    with db.connection() as con:
        db.write_lock(con, "paperlab-scan")
        con.execute(
            "INSERT INTO pl_card_scans(user_id,paper_id,card_id,status,page_count,result_json,"
            "score,max_score,attempt_id,created_at,updated_at) "
            "VALUES(?,?,?,'recognized',?,?,?,?,NULL,?,?)",
            (uid, int(paper_id), int(card["id"]), page_count,
             json.dumps(result, ensure_ascii=False),
             graded["score"], graded["max_score"], now, now),
        )
        return int(con.execute(
            "SELECT id FROM pl_card_scans WHERE user_id=? AND paper_id=? ORDER BY id DESC LIMIT 1",
            (uid, int(paper_id)),
        ).fetchone()[0])


# ---------------------------------------------------------------------------
# 1. 整卷提交：逐题产生一条有效证据，pending 如实待核验
# ---------------------------------------------------------------------------

def test_paper_submit_records_item_evidence(client):
    first = create_question(client)
    second = create_question(client)
    subjective = create_question(client, qtype="SUBJECTIVE", content={
        "stem": "证明 $f(x)=x^2$ 在 $[0,+\\infty)$ 上单调递增。",
        "answers": ["利用导数可证。"],
        "explanation": "对 $f$ 求导得 $f'(x)=2x\\ge 0$。",
    })
    paper = create_paper(client, [
        {"ref": f"app:{first['id']}", "score": 3},
        {"ref": f"app:{second['id']}", "score": 4},
        {"ref": f"app:{subjective['id']}", "score": 10},
    ])
    paper_id = paper["paper"]["id"]
    attempt_id = start_attempt(client, paper_id)["attempt"]["id"]

    response = submit_attempt(client, attempt_id, [
        {"item_position": 1, "answer": ["B"]},
        {"item_position": 2, "answer": ["A"]},
        {"item_position": 3, "answer": "函数在区间上单调递增，证明略。"},
    ])
    assert response.status_code == 200, response.text
    body = response.json()
    assert [item["judgement"] for item in body["items"]] == ["correct", "incorrect", "pending"]
    assert body["result"]["summary"]["correct_count"] == 1
    assert body["result"]["summary"]["incorrect_count"] == 1
    assert body["result"]["summary"]["pending_count"] == 1

    uid = user_id(client)
    expected = [
        (f"app:{first['id']}", "correct", "reference", "verified", 3, 3),
        (f"app:{second['id']}", "incorrect", "reference", "verified", 0, 4),
        (f"app:{subjective['id']}", "pending", "pending", "pending", 0, 10),
    ]
    rows = evidence_rows(uid, kind="paper_attempt")
    assert len(rows) == len(expected)
    for position, (ref, verdict, grade_source, confidence, score, max_score) in enumerate(
            expected, start=1):
        event_key = paperlab_practice._paper_item_evidence_key(attempt_id, position)
        event = study_evidence.active_event(uid, event_key)
        assert event is not None, f"第 {position} 题缺少学习证据"
        assert event["question_ref"] == ref
        assert event["verdict"] == verdict
        assert event["grade_source"] == grade_source
        assert event["confidence"] == confidence
        assert event["kind"] == "paper_attempt"
        assert event["record_kind"] == "pl_paper_attempts"
        assert event["record_id"] == attempt_id
        assert event["item_key"] == str(position)
        assert event["prompt_state"] == "none"
        assert event["reveal_state"] == "before"
        assert event["meta"]["score"] == score
        assert event["meta"]["max_score"] == max_score
        assert event["meta"]["judgement"] == verdict
        # 单题证据按统一引用可定位，计数与逐题判分一致。
        related = study_evidence.question_evidence(uid, ref)
        assert related["counts"] == {
            "total": 1,
            "correct": 1 if verdict == "correct" else 0,
            "incorrect": 1 if verdict == "incorrect" else 0,
            "pending": 1 if verdict == "pending" else 0,
        }
        assert related["events"][0]["event_key"] == event_key

    # 同一 attempt 重复提交被 409 拒绝，证据条数不变（不重复计数）。
    again = submit_attempt(client, attempt_id, [{"item_position": 1, "answer": ["B"]}])
    assert again.status_code == 409
    assert count_evidence(uid, kind="paper_attempt") == len(expected)


# ---------------------------------------------------------------------------
# 2. 事件键与幂等：同键只一条有效证据，重判升版不新增
# ---------------------------------------------------------------------------

def test_paper_evidence_event_key_idempotent_and_revision(client):
    uid = user_id(client)
    key = paperlab_practice._paper_item_evidence_key(4242, 2)
    assert key == "paper:item:4242:2"
    kwargs = {
        "kind": "paper_attempt", "record_kind": "pl_paper_attempts", "record_id": 4242,
        "item_key": "2", "question_ref": "bank:synthetic-qid", "verdict": "correct",
        "grade_source": "reference", "prompt_state": "none", "reveal_state": "before",
        "event_key": key,
    }
    first = study_evidence.record_event(uid, **kwargs)
    second = study_evidence.record_event(uid, **kwargs)
    assert first["id"] == second["id"]
    assert len(evidence_rows(uid, event_key=key)) == 1
    assert study_evidence.question_evidence(uid, "bank:synthetic-qid")["counts"]["total"] == 1

    # 重判（若业务允许）用同一基础键升版：有效证据仍只有一条，计数不增加。
    revised = study_evidence.revise_event(uid, key, verdict="incorrect")
    assert revised["evidence_version"] == 2
    assert study_evidence.active_event(uid, key)["verdict"] == "incorrect"
    assert len([row for row in evidence_rows(uid) if row["valid"] == 1]) == 1
    found = study_evidence.question_evidence(uid, "bank:synthetic-qid")
    assert found["counts"] == {"total": 1, "correct": 0, "incorrect": 1, "pending": 0}


# ---------------------------------------------------------------------------
# 3. 证据模块异常不影响整卷提交与判分
# ---------------------------------------------------------------------------

def test_paper_submit_survives_evidence_failure(client, monkeypatch):
    question = create_question(client)
    paper = create_paper(client, [{"ref": f"app:{question['id']}", "score": 5}])
    paper_id = paper["paper"]["id"]
    attempt_id = start_attempt(client, paper_id)["attempt"]["id"]

    def broken_record_event(*args, **kwargs):
        raise RuntimeError("学习证据后端不可用（测试注入）")

    monkeypatch.setattr(study_evidence, "record_event", broken_record_event)
    response = submit_attempt(client, attempt_id, [{"item_position": 1, "answer": ["B"]}])
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["items"][0]["judgement"] == "correct"
    assert body["items"][0]["score"] == 5
    assert body["result"]["summary"]["score"] == 5
    assert body["attempt"]["status"] == "submitted"
    # 判分主流程已提交成功；证据写入失败不产生半条脏数据。
    assert client.get(f"/api/paperlab/attempts/{attempt_id}").json()["attempt"]["status"] == "submitted"
    assert count_evidence(user_id(client), kind="paper_attempt") == 0


# ---------------------------------------------------------------------------
# 4. 答题卡确认：逐题写入 paper_scan 证据，无参考答案如实 pending
# ---------------------------------------------------------------------------

def test_scan_confirm_records_item_evidence(client):
    gradable = create_question(client)
    # 客观题但没有可核对参考答案：确认判分必须 pending，证据不得伪造对错。
    no_reference = create_question(client, content={
        "stem": "本题暂无参考答案的选择题（测试）",
        "options": [{"label": "A", "text": "选项 A"}, {"label": "B", "text": "选项 B"}],
        "answers": [],
        "explanation": "",
    })
    paper = create_paper(client, [
        {"ref": f"app:{gradable['id']}", "score": 5},
        {"ref": f"app:{no_reference['id']}", "score": 5},
    ])
    paper_id = paper["paper"]["id"]
    created = create_card(client, paper_id)
    layout = created["layout"]
    scan_id = craft_scan_row(
        client, paper_id=paper_id, card=created["card"], layout=layout,
        answers={1: ["B"], 2: ["A"]},
    )

    response = client.post(f"/api/paperlab/scans/{scan_id}/confirm", json={
        "answers": {"1": ["B"], "2": ["A"]},
    })
    assert response.status_code == 200, response.text
    confirmed = response.json()
    assert confirmed["scan"]["status"] == "confirmed"
    assert confirmed["attempt"] and confirmed["attempt"]["id"]

    uid = user_id(client)
    first = study_evidence.active_event(uid, f"paper:scan:{scan_id}:1")
    assert first is not None
    assert first["kind"] == "paper_scan"
    assert first["record_kind"] == "paper_scan"
    assert first["record_id"] == scan_id
    assert first["item_key"] == "1"
    assert first["question_ref"] == f"app:{gradable['id']}"
    assert first["verdict"] == "correct"
    assert first["grade_source"] == "reference"
    assert first["confidence"] == "verified"
    assert first["prompt_state"] == "none"
    assert first["reveal_state"] == "before"
    assert first["meta"]["source"] == "scan"
    assert first["meta"]["score"] == 5
    assert first["meta"]["max_score"] == 5

    second = study_evidence.active_event(uid, f"paper:scan:{scan_id}:2")
    assert second is not None
    assert second["question_ref"] == f"app:{no_reference['id']}"
    assert second["verdict"] == "pending"
    assert second["grade_source"] == "pending"
    assert second["confidence"] == "pending"
    assert second["meta"]["source"] == "scan"
    assert second["meta"]["note"] == "答题卡无可核对参考答案"
    assert study_evidence.question_evidence(
        uid, f"app:{no_reference['id']}")["counts"]["pending"] == 1

    # 重复确认 409，证据不重复。
    again = client.post(f"/api/paperlab/scans/{scan_id}/confirm", json={"answers": {}})
    assert again.status_code == 409
    assert count_evidence(uid, kind="paper_scan") == 2


# ---------------------------------------------------------------------------
# 5. 历史/脏数据：题目引用为空或非法时跳过证据，不影响提交
# ---------------------------------------------------------------------------

def test_empty_or_invalid_question_ref_is_skipped_without_500(client):
    first = create_question(client)
    second = create_question(client)
    paper = create_paper(client, [
        {"ref": f"app:{first['id']}", "score": 5},
        {"ref": f"app:{second['id']}", "score": 5},
    ])
    paper_id = paper["paper"]["id"]
    attempt_id = start_attempt(client, paper_id)["attempt"]["id"]
    # 模拟历史/脏数据：冻结行的题目引用为空/含空白（统一引用无法解析）。
    with db.connection() as con:
        con.execute("UPDATE pl_attempt_items SET question_ref='' WHERE attempt_id=? AND item_position=1",
                    (attempt_id,))
        con.execute(
            "UPDATE pl_attempt_items SET question_ref='bad ref' "
            "WHERE attempt_id=? AND item_position=2", (attempt_id,),
        )

    response = submit_attempt(client, attempt_id, [
        {"item_position": 1, "answer": ["B"]},
        {"item_position": 2, "answer": ["B"]},
    ])
    assert response.status_code == 200, response.text
    body = response.json()
    assert [item["judgement"] for item in body["items"]] == ["correct", "correct"]
    assert body["attempt"]["status"] == "submitted"
    # 引用不可定位的题位不写证据（不 500、不伪造题目引用），判分主流程照常完成。
    assert count_evidence(user_id(client), kind="paper_attempt") == 0


# ---------------------------------------------------------------------------
# 6. 两账号隔离：A 的工坊证据在 B 的 question_evidence 中不可见
# ---------------------------------------------------------------------------

def test_paper_evidence_isolated_between_accounts(client, second_client):
    question = create_question(client)
    paper = create_paper(client, [{"ref": f"app:{question['id']}", "score": 5}])
    paper_id = paper["paper"]["id"]
    attempt_id = start_attempt(client, paper_id)["attempt"]["id"]
    response = submit_attempt(client, attempt_id, [{"item_position": 1, "answer": ["B"]}])
    assert response.status_code == 200, response.text

    owner = user_id(client)
    other = user_id(second_client)
    ref = f"app:{question['id']}"
    assert study_evidence.question_evidence(owner, ref)["counts"]["total"] == 1
    stranger = study_evidence.question_evidence(other, ref)
    assert stranger["counts"]["total"] == 0
    assert stranger["events"] == []
    key = paperlab_practice._paper_item_evidence_key(attempt_id, 1)
    assert study_evidence.active_event(owner, key) is not None
    assert study_evidence.active_event(other, key) is None
    assert study_evidence.summary(other)["effective_count"] == 0
