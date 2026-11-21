"""试卷工坊 · 整卷练习（个人自测作答/判分/错题联动）测试。

覆盖：创建练习（卷面不泄题）、归属/未登录、逐题保存幂等、提交判分（单选/多选/填空/主观 pending）、
重复提交 409、提交后不可改、错题联动既有 wrong_questions（只读题库 qid）、应用题库题记录在练习结果、
历史成绩、空卷 422。全部使用应用题库临时数据 + 只读题库真实 qid，不联网。
"""
import json

from fastapi.testclient import TestClient

from app import db, practice
from app.main import app
from app.paperlab import papers as paper_store


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def user_id(client) -> int:
    return client.get("/api/auth/me").json()["id"]


def create_question(client, **overrides) -> dict:
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


def create_paper(client, items, title="整卷练习测试卷") -> dict:
    response = client.post("/api/paperlab/papers", json={
        "title": title, "kind": "manual", "subject": "数学", "grade": "高中一年级",
        "items": items,
    })
    assert response.status_code == 200, response.text
    return response.json()


def start_attempt(client, paper_id):
    response = client.post(f"/api/paperlab/papers/{paper_id}/attempts")
    assert response.status_code == 200, response.text
    return response.json()


def save_answer(client, attempt_id, position, answer):
    return client.patch(f"/api/paperlab/attempts/{attempt_id}/answers",
                        json={"item_position": position, "answer": answer})


def submit(client, attempt_id, answers=None):
    payload = {"answers": answers} if answers is not None else {}
    return client.post(f"/api/paperlab/attempts/{attempt_id}/submit", json=payload)


def bank_qid(subject="数学", type_name="single_choice") -> str:
    with practice._bank() as con:
        rows = con.execute(
            "SELECT qid FROM questions WHERE subject=? AND type=? ORDER BY qid LIMIT 40",
            (subject, type_name),
        ).fetchall()
    for row in rows:
        question = practice.load_question(row[0])
        if question.get("practice_allowed") and question.get("has_answer"):
            return row[0]
    raise AssertionError("no suitable bank question")


def bank_qid_with_image() -> str:
    """找一道带配图的只读题库单选题（用于验证历史试卷快照配图通道）。"""
    with practice._bank() as con:
        rows = con.execute(
            "SELECT qid FROM questions WHERE subject='数学' ORDER BY qid LIMIT 300"
        ).fetchall()
    for row in rows:
        question = practice.load_question(row[0])
        if not question.get("practice_allowed"):
            continue
        snapshot = paper_store.snapshot_bank(row[0])
        images = (snapshot.get("content") or {}).get("images") or []
        if images:
            return row[0]
    raise AssertionError("no bank question with image")


def standard_paper(client):
    single = create_question(client)
    multiple = create_question(client, type="MULTIPLE", content={
        "stem": "下列说法正确的是（  ）",
        "options": [{"label": "A", "text": "甲"}, {"label": "B", "text": "乙"}],
        "answers": ["A", "B"], "explanation": "甲乙均正确。",
    })
    blank = create_question(client, type="BLANK", content={
        "stem": "计算：$1+2+3=\\underline{\\quad}$。",
        "answers": ["6"], "explanation": "1+2+3=6。",
    })
    subjective = create_question(client, type="SUBJECTIVE", content={
        "stem": "证明 $f(x)=x^2$ 在 $[0,+\\infty)$ 上单调递增。",
        "answers": ["利用导数可证。"], "explanation": "对 $f$ 求导得 $f'(x)=2x\\ge 0$。",
        "rubric": "求导正确 3 分，结论 2 分。",
    })
    paper = create_paper(client, [
        {"ref": f"app:{single['id']}", "score": 3, "section": "一、客观题"},
        {"ref": f"app:{multiple['id']}", "score": 4, "section": "一、客观题"},
        {"ref": f"app:{blank['id']}", "score": 2, "section": "二、填空题"},
        {"ref": f"app:{subjective['id']}", "score": 10, "section": "三、解答题"},
    ])
    return paper, {"single": single, "multiple": multiple, "blank": blank, "subjective": subjective}


# ---------------------------------------------------------------------------
# 1. 创建练习：卷面不泄题 / 归属 / 未登录
# ---------------------------------------------------------------------------

def test_create_attempt_returns_sheet_without_answers(client):
    paper, meta = standard_paper(client)
    sheet = start_attempt(client, paper["paper"]["id"])
    attempt = sheet["attempt"]
    assert attempt["status"] == "in_progress"
    assert attempt["item_count"] == 4
    assert attempt["max_score"] == 19
    assert [item["item_position"] for item in sheet["items"]] == [1, 2, 3, 4]
    assert [item["question_ref"] for item in sheet["items"]] == [
        f"app:{meta['single']['id']}", f"app:{meta['multiple']['id']}",
        f"app:{meta['blank']['id']}", f"app:{meta['subjective']['id']}",
    ]
    dumped = json.dumps(sheet, ensure_ascii=False)
    assert "参考答案" not in dumped and "利用导数可证" not in dumped
    for item in sheet["items"]:
        assert "answers" not in item["content"]
        assert "explanation" not in item["content"]
        assert "rubric" not in item["content"]
    assert sheet["paper"]["title"] == "整卷练习测试卷"


def test_attempt_ownership_and_login(client, second_client):
    paper, _ = standard_paper(client)
    sheet = start_attempt(client, paper["paper"]["id"])
    attempt_id = sheet["attempt"]["id"]

    assert second_client.get(f"/api/paperlab/attempts/{attempt_id}").status_code == 404
    assert save_answer(second_client, attempt_id, 1, ["B"]).status_code == 404
    assert second_client.post(
        f"/api/paperlab/papers/{paper['paper']['id']}/attempts"
    ).status_code == 404
    assert second_client.get(
        f"/api/paperlab/papers/{paper['paper']['id']}/attempts"
    ).status_code == 404

    anonymous = TestClient(app)
    try:
        assert anonymous.get(f"/api/paperlab/attempts/{attempt_id}").status_code == 401
        assert anonymous.post(
            f"/api/paperlab/papers/{paper['paper']['id']}/attempts"
        ).status_code == 401
    finally:
        anonymous.close()


def test_create_attempt_rejects_empty_paper(client):
    paper = create_paper(client, [])
    assert paper["paper"]["item_count"] == 0
    response = client.post(f"/api/paperlab/papers/{paper['paper']['id']}/attempts")
    assert response.status_code == 422
    assert "题目" in response.json()["detail"]


# ---------------------------------------------------------------------------
# 2. 逐题保存（幂等 / 越界 / 过长）
# ---------------------------------------------------------------------------

def test_patch_answer_is_idempotent(client):
    paper, _ = standard_paper(client)
    attempt_id = start_attempt(client, paper["paper"]["id"])["attempt"]["id"]

    first = save_answer(client, attempt_id, 1, ["A"])
    assert first.status_code == 200, first.text
    assert first.json()["saved"] is True
    second = save_answer(client, attempt_id, 1, ["B"])
    assert second.status_code == 200
    sheet = client.get(f"/api/paperlab/attempts/{attempt_id}").json()
    assert sheet["items"][0]["answer"] == ["B"]

    out_of_range = save_answer(client, attempt_id, 99, ["B"])
    assert out_of_range.status_code == 422
    assert "序号" in out_of_range.json()["detail"]
    too_long = save_answer(client, attempt_id, 1, "x" * 20000)
    assert too_long.status_code == 413


def test_attempt_answer_isolation_between_attempts(client):
    paper, _ = standard_paper(client)
    first = start_attempt(client, paper["paper"]["id"])["attempt"]["id"]
    second = start_attempt(client, paper["paper"]["id"])["attempt"]["id"]
    save_answer(client, first, 1, ["B"])
    first_sheet = client.get(f"/api/paperlab/attempts/{first}").json()
    second_sheet = client.get(f"/api/paperlab/attempts/{second}").json()
    assert first_sheet["items"][0]["answer"] == ["B"]
    assert second_sheet["items"][0]["answer"] is None


# ---------------------------------------------------------------------------
# 3. 提交判分：客观自动 / 主观 pending / 应用题库题记录在结果
# ---------------------------------------------------------------------------

def test_submit_grades_objective_and_marks_subjective_pending(client):
    paper, _ = standard_paper(client)
    attempt_id = start_attempt(client, paper["paper"]["id"])["attempt"]["id"]
    answers = [
        {"item_position": 1, "answer": ["B"]},   # 单选正确 3 分
        {"item_position": 2, "answer": ["A"]},   # 多选缺 B → 错误
        {"item_position": 3, "answer": "6"},     # 填空正确 2 分
        {"item_position": 4, "answer": "我的证明：由导数可证。"},  # 主观 pending
    ]
    response = submit(client, attempt_id, answers)
    assert response.status_code == 200, response.text
    payload = response.json()
    attempt = payload["attempt"]
    assert attempt["status"] == "submitted"
    assert attempt["score"] == 5.0
    assert attempt["max_score"] == 19.0
    assert attempt["correct_count"] == 2
    assert attempt["incorrect_count"] == 1
    assert attempt["pending_count"] == 1
    assert attempt["submitted_at"]

    items = payload["items"]
    assert [item["judgement"] for item in items] == ["correct", "incorrect", "correct", "pending"]
    assert [item["score"] for item in items] == [3.0, 0.0, 2.0, 0.0]
    # 提交后才返回参考答案与解析
    assert items[3]["reference"]["answers"]
    assert "求导" in items[3]["reference"]["explanation"]
    assert items[1]["feedback"]
    result = payload["result"]
    assert result["summary"]["auto_graded"] == 3
    assert "主观题待核验" in result["notice"]
    # 应用题库题不写错题本，但记录在练习结果中
    assert result["wrong_added"] == []
    assert {entry["item_position"] for entry in result["app_bank_unlinked"]} == {2, 4}

    # 提交后不能重复提交 / 不能改答案
    assert submit(client, attempt_id).status_code == 409
    assert save_answer(client, attempt_id, 1, ["A"]).status_code == 409


def test_submit_accepts_single_request_answers_without_patch(client):
    paper, _ = standard_paper(client)
    attempt_id = start_attempt(client, paper["paper"]["id"])["attempt"]["id"]
    response = submit(client, attempt_id, [{"item_position": 1, "answer": "B"}])
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["items"][0]["judgement"] == "correct"
    assert payload["items"][0]["score"] == 3.0


# ---------------------------------------------------------------------------
# 4. 错题联动（只读题库 qid → 既有 wrong_questions）
# ---------------------------------------------------------------------------

def test_bank_question_wrong_added_to_wrongbook(client):
    qid = bank_qid()
    snapshot = paper_store.snapshot_bank(qid)
    content = snapshot.get("content") or {}
    options = [option for option in content.get("options") or [] if option.get("label")]
    assert options
    reference_text = str(content.get("answer_text") or "").upper()
    correct_labels = {char for char in reference_text if char.isalpha()}
    wrong_label = next((option["label"] for option in options
                        if option["label"] not in correct_labels), None)
    assert wrong_label, "bank question has no wrong option to submit"

    paper = create_paper(client, [{"ref": f"bank:{qid}", "score": 5, "section": "真题"}])
    attempt_id = start_attempt(client, paper["paper"]["id"])["attempt"]["id"]
    save_answer(client, attempt_id, 1, [wrong_label])
    response = submit(client, attempt_id)
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["items"][0]["judgement"] == "incorrect"
    wrong_added = payload["result"]["wrong_added"]
    assert [entry["qid"] for entry in wrong_added] == [qid]
    assert "错题已加入错题本" in payload["result"]["notice"]

    uid = user_id(client)
    with db.connection() as con:
        row = con.execute(
            "SELECT * FROM wrong_questions WHERE user_id=? AND qid=?", (uid, qid)
        ).fetchone()
        mastery = con.execute(
            "SELECT level FROM mastery WHERE user_id=? AND qid=?", (uid, qid)
        ).fetchone()
    assert row is not None
    assert row["archived"] == 0
    assert row["last_result"] == "incorrect"
    assert "整卷练习" in row["reason"]
    assert mastery is not None and mastery["level"] == "未掌握"


def test_wrongbook_not_duplicated_when_submitting_again_in_new_attempt(client):
    qid = bank_qid()
    snapshot = paper_store.snapshot_bank(qid)
    content = snapshot.get("content") or {}
    options = [option for option in content.get("options") or [] if option.get("label")]
    reference_text = str(content.get("answer_text") or "").upper()
    correct_labels = {char for char in reference_text if char.isalpha()}
    wrong_label = next((option["label"] for option in options
                        if option["label"] not in correct_labels), None)
    assert wrong_label

    paper = create_paper(client, [{"ref": f"bank:{qid}", "score": 5}])
    for _ in range(2):
        attempt_id = start_attempt(client, paper["paper"]["id"])["attempt"]["id"]
        save_answer(client, attempt_id, 1, [wrong_label])
        assert submit(client, attempt_id).status_code == 200
    uid = user_id(client)
    with db.connection() as con:
        rows = con.execute(
            "SELECT * FROM wrong_questions WHERE user_id=? AND qid=?", (uid, qid)
        ).fetchall()
    assert len(rows) == 1
    assert rows[0]["failures"] >= 2


def test_frozen_paper_image_requires_owner(client, second_client):
    qid = bank_qid_with_image()
    paper = create_paper(client, [{"ref": f"bank:{qid}", "score": 5}])
    paper_id = paper["paper"]["id"]
    url = f"/api/paperlab/papers/{paper_id}/items/1/images/0"
    response = client.get(url)
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("image/")
    assert len(response.content) > 0
    assert second_client.get(url).status_code == 404
    assert client.get(f"/api/paperlab/papers/{paper_id}/items/1/images/99").status_code == 404
    assert client.get(f"/api/paperlab/papers/{paper_id}/items/9/images/0").status_code == 404
    assert client.get("/api/paperlab/papers/999999/items/1/images/0").status_code == 404


# ---------------------------------------------------------------------------
# 5. 历史成绩 / 结果读取
# ---------------------------------------------------------------------------

def test_attempt_history_and_result_read(client):
    paper, _ = standard_paper(client)
    attempt_id = start_attempt(client, paper["paper"]["id"])["attempt"]["id"]
    save_answer(client, attempt_id, 1, ["B"])
    submit(client, attempt_id, [{"item_position": 3, "answer": "6"}])

    history = client.get(f"/api/paperlab/papers/{paper['paper']['id']}/attempts")
    assert history.status_code == 200
    data = history.json()
    assert data["total"] >= 1
    entry = data["items"][0]
    assert entry["id"] == attempt_id
    assert entry["status"] == "submitted"
    assert entry["score"] == 5.0
    assert entry["correct_count"] == 2

    alias = client.get(f"/api/paperlab/attempts?paper_id={paper['paper']['id']}")
    assert alias.status_code == 200
    assert alias.json()["total"] == data["total"]

    detail = client.get(f"/api/paperlab/attempts/{attempt_id}")
    assert detail.status_code == 200
    detail_payload = detail.json()
    assert detail_payload["attempt"]["status"] == "submitted"
    assert detail_payload["result"]["summary"]["score"] == 5.0
    assert detail_payload["items"][0]["judgement"] == "correct"
