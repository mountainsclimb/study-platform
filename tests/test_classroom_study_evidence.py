"""课堂测验提交接入共享学习证据（整合方案 §4「作答与学习证据」/ §8 工单 C）隔离测试。

覆盖：
1. 逐题 verdict / grade_source 与 ``grading.grade_quiz`` 明细一致，显式 question_ref 原样落库；
2. 无（或非 bank/app/official 前缀）question_ref 的课堂小题不伪造引用，仍可按
   ``record_id + item_key`` / ``active_event`` 检索，``summary`` 计数正确；
3. 证据写入异常（monkeypatch 抛错）时测验提交仍成功、得分正确、attempt 落库；
4. 同一 scene 重复提交生成新 attempt 与新证据（条数 +N，不串旧 attempt）；
5. 两账号证据隔离；
6. 既有测验判分行为不退化；
7. 判分只有总分（无逐题明细）时只写一条汇总证据（item_key=summary），不编造逐题对错。

全部走 ``isolated_db`` + TestClient，直接建库造课程/场景，不联网、不调用模型。
"""
import json

import pytest

from app import db, study_evidence
from app.classroom import api as classroom_api
from app.classroom import db as cdb
from app.classroom import grading, schema


@pytest.fixture(autouse=True)
def _evidence_table(isolated_db):
    """显式建好学习证据表（生产由 app.main lifespan 调用 init；本模块幂等）。"""
    study_evidence.init_study_evidence()


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------

def uid_of(client) -> int:
    response = client.get("/api/auth/me")
    assert response.status_code == 200, response.text
    return int(response.json()["id"])


def create_course(client) -> dict:
    response = client.post("/api/classroom/courses", json={
        "topic": "光合作用", "subject": "生物", "grade": "初中二年级",
    })
    assert response.status_code == 200, response.text
    return response.json()["course"]


def insert_quiz_scene(course_id: int, user_id: int, questions: list[dict]) -> int:
    """直接入库一个测验场景（保留 question_ref，绕开归一化时丢弃未知字段的问题）。"""
    content = {"type": "quiz", "questions": questions}
    now = db.utcnow()
    with db.connection() as con:
        return int(con.execute(
            "INSERT INTO cls_scenes(course_id,user_id,scene_type,order_index,title,content_json,meta_json,"
            "status,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (course_id, user_id, "quiz", 1, "课堂小测", cdb.json_dump(content),
             cdb.json_dump({}), "ready", now, now),
        ).lastrowid)


def create_session(client, course_id: int) -> int:
    response = client.post(f"/api/classroom/courses/{course_id}/sessions")
    assert response.status_code == 200, response.text
    return int(response.json()["session"]["id"])


def submit(client, session_id: int, scene_id: int, answers: list[dict]):
    return client.post(f"/api/classroom/sessions/{session_id}/quiz", json={
        "scene_id": scene_id, "answers": answers,
    })


def evidence_rows(user_id: int, *, attempt_id: int | None = None) -> list[dict]:
    sql = "SELECT * FROM study_evidence WHERE user_id=?"
    params: list = [user_id]
    if attempt_id is not None:
        sql += " AND record_id=?"
        params.append(int(attempt_id))
    sql += " ORDER BY id"
    with db.connection() as con:
        return [dict(row) for row in con.execute(sql, tuple(params)).fetchall()]


def evidence_meta(row: dict) -> dict:
    return json.loads(row["meta_json"]) if row.get("meta_json") else {}


def basic_questions(*, with_refs: bool) -> list[dict]:
    questions = [
        {"id": "q1", "type": "single", "question": "光合作用发生的场所是？",
         "options": [{"label": "叶绿体", "value": "A"}, {"label": "线粒体", "value": "B"}],
         "answer": ["A"], "analysis": "叶绿体是场所。", "points": 10},
        {"id": "q2", "type": "multiple", "question": "光合作用的原料包括？（多选）",
         "options": [{"label": "水", "value": "A"}, {"label": "氧气", "value": "B"},
                     {"label": "二氧化碳", "value": "C"}],
         "answer": ["A", "C"], "analysis": "水和二氧化碳是原料。", "points": 10},
        {"id": "q3", "type": "short_answer", "question": "简述光合作用的意义。",
         "commentPrompt": "评分标准：要点完整", "analysis": "参考要点。", "points": 20},
    ]
    if with_refs:
        questions[0]["question_ref"] = "bank:docA:1"
        questions[1]["question_ref"] = "official:plz-9"
    return questions


BASIC_ANSWERS = [
    {"question_id": "q1", "values": ["A"]},          # 正确
    {"question_id": "q2", "values": ["A"]},          # 少选 → 错误
    {"question_id": "q3", "values": ["为生物提供有机物和氧气"]},  # 主观题 → 待核验
]


# ---------------------------------------------------------------------------
# 1. 逐题证据与判分一致 + question_ref 原样使用
# ---------------------------------------------------------------------------

def test_quiz_evidence_matches_grading_and_uses_question_ref(client):
    user_id = uid_of(client)
    course = create_course(client)
    questions = basic_questions(with_refs=True)
    scene_id = insert_quiz_scene(course["id"], user_id, questions)
    session_id = create_session(client, course["id"])

    expected = grading.grade_quiz({"questions": questions}, BASIC_ANSWERS)
    response = submit(client, session_id, scene_id, BASIC_ANSWERS)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["score"] == expected["score"] == 10
    assert body["max_score"] == expected["max_score"] == 40
    assert body["correct_count"] == expected["correct_count"] == 1
    assert body["needs_review_count"] == expected["needs_review_count"] == 1

    attempt_id = int(body["attempt_id"])
    rows = evidence_rows(user_id, attempt_id=attempt_id)
    assert len(rows) == len(expected["detail"]) == 3
    by_item = {row["item_key"]: row for row in rows}
    ref_map = {"q1": "bank:docA:1", "q2": "official:plz-9", "q3": None}
    for detail in expected["detail"]:
        qid = detail["question_id"]
        item_key = "%d:%d:%s" % (course["id"], scene_id, qid)
        row = by_item[item_key]
        pending = bool(detail["needs_review"])
        assert row["event_key"] == "quiz:%d:%s" % (attempt_id, item_key)
        assert row["kind"] == "classroom_quiz"
        assert row["record_kind"] == "cls_quiz_attempts"
        assert int(row["record_id"]) == attempt_id
        assert row["question_ref"] == ref_map[qid]
        assert bool(int(row["valid"])) is True
        assert row["verdict"] == ("pending" if pending else
                                  ("correct" if detail["correct"] else "incorrect"))
        assert row["grade_source"] == ("pending" if pending else "reference")
        assert row["confidence"] == ("pending" if pending else "verified")
        assert row["prompt_state"] == "unknown"
        assert row["reveal_state"] == "after"
        assert row["self_report"] == "none"
        meta = evidence_meta(row)
        assert meta["course_id"] == course["id"]
        assert meta["scene_id"] == scene_id
        assert meta["question_key"] == qid
        assert meta["score"] == expected["score"]
        assert meta["max_score"] == expected["max_score"]
        assert meta["needs_review"] is pending

    # 带 question_ref 的题可按统一引用检索到这条课堂测验证据。
    single = study_evidence.question_evidence(user_id, "bank:docA:1")
    assert single["counts"] == {"total": 1, "correct": 1, "incorrect": 0, "pending": 0}
    assert single["events"][0]["record_id"] == attempt_id
    assert single["events"][0]["kind"] == "classroom_quiz"
    official = study_evidence.question_evidence(user_id, "official:plz-9")
    assert official["counts"] == {"total": 1, "correct": 0, "incorrect": 1, "pending": 0}

    overview = study_evidence.summary(user_id)
    assert overview["by_kind"]["classroom_quiz"] == 3
    assert overview["by_record_kind"]["cls_quiz_attempts"] == 3


# ---------------------------------------------------------------------------
# 2. 无 question_ref 的课堂小题：不伪造引用，仍可按 record_id/item_key 检索
# ---------------------------------------------------------------------------

def test_question_without_ref_does_not_fabricate_reference(client):
    user_id = uid_of(client)
    course = create_course(client)
    questions = basic_questions(with_refs=False)
    # 非 bank/app/official 前缀的旧式值也不能被当成题库引用（不伪造 bank:）。
    questions[1]["question_ref"] = "q-legacy-bare"
    scene_id = insert_quiz_scene(course["id"], user_id, questions)
    session_id = create_session(client, course["id"])

    response = submit(client, session_id, scene_id, BASIC_ANSWERS)
    assert response.status_code == 200, response.text
    attempt_id = int(response.json()["attempt_id"])

    rows = evidence_rows(user_id, attempt_id=attempt_id)
    assert len(rows) == 3
    assert all(row["question_ref"] is None for row in rows)
    assert all(row["item_key"] == "%d:%d:%s" % (course["id"], scene_id, row_item)
               for row, row_item in zip(rows, ("q1", "q2", "q3")))

    # 按 event_key / record_id+item_key 仍能取到这条证据。
    key = "quiz:%d:%d:%d:q1" % (attempt_id, course["id"], scene_id)
    event = study_evidence.active_event(user_id, key)
    assert event is not None
    assert event["record_id"] == attempt_id and event["item_key"].endswith(":q1")
    assert event["question_ref"] is None and event["verdict"] == "correct"
    with db.connection() as con:
        retrieved = con.execute(
            "SELECT COUNT(*) FROM study_evidence WHERE user_id=? AND record_id=? AND item_key=?",
            (user_id, attempt_id, "%d:%d:q1" % (course["id"], scene_id)),
        ).fetchone()[0]
        referenced = con.execute(
            "SELECT COUNT(*) FROM study_evidence WHERE user_id=? AND question_ref IS NOT NULL",
            (user_id,),
        ).fetchone()[0]
    assert retrieved == 1
    assert referenced == 0

    overview = study_evidence.summary(user_id)
    assert overview["by_kind"]["classroom_quiz"] == 3
    assert overview["by_question_source"]["none"] == 3


# ---------------------------------------------------------------------------
# 3. 证据写入异常不影响测验提交主流程
# ---------------------------------------------------------------------------

def test_evidence_failure_does_not_block_quiz_submission(client, monkeypatch):
    user_id = uid_of(client)
    course = create_course(client)
    questions = basic_questions(with_refs=True)
    scene_id = insert_quiz_scene(course["id"], user_id, questions)
    session_id = create_session(client, course["id"])

    def broken_record_event(*args, **kwargs):
        raise RuntimeError("学习证据写入失败（模拟）")

    monkeypatch.setattr(study_evidence, "record_event", broken_record_event)
    response = submit(client, session_id, scene_id, BASIC_ANSWERS)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["score"] == 10
    assert body["max_score"] == 40
    assert body["correct_count"] == 1
    assert body["detail"]

    attempt_id = int(body["attempt_id"])
    with db.connection() as con:
        attempt = con.execute(
            "SELECT * FROM cls_quiz_attempts WHERE id=?", (attempt_id,)
        ).fetchone()
    assert attempt is not None
    assert attempt["user_id"] == user_id
    assert attempt["score"] == 10
    # 证据写入整体失败，但提交事务照常提交、不产生半截证据。
    assert evidence_rows(user_id) == []


# ---------------------------------------------------------------------------
# 4. 同一 scene 重复提交 → 新 attempt、新证据，不串旧 attempt
# ---------------------------------------------------------------------------
def test_repeat_submission_creates_new_attempt_and_evidence(client):
    user_id = uid_of(client)
    course = create_course(client)
    questions = basic_questions(with_refs=True)
    scene_id = insert_quiz_scene(course["id"], user_id, questions)
    session_id = create_session(client, course["id"])

    first = submit(client, session_id, scene_id, BASIC_ANSWERS)
    second = submit(client, session_id, scene_id, BASIC_ANSWERS)
    assert first.status_code == 200 and second.status_code == 200
    first_attempt = int(first.json()["attempt_id"])
    second_attempt = int(second.json()["attempt_id"])
    assert first_attempt != second_attempt
    assert second.json()["score"] == first.json()["score"] == 10

    with db.connection() as con:
        attempt_count = con.execute(
            "SELECT COUNT(*) FROM cls_quiz_attempts WHERE user_id=? AND scene_id=?",
            (user_id, scene_id),
        ).fetchone()[0]
    assert attempt_count == 2

    first_rows = evidence_rows(user_id, attempt_id=first_attempt)
    second_rows = evidence_rows(user_id, attempt_id=second_attempt)
    assert len(first_rows) == 3 and len(second_rows) == 3
    # 两次 attempt 的证据各自独立，事件键不串。
    assert all(row["event_key"].startswith("quiz:%d:" % first_attempt) for row in first_rows)
    assert all(row["event_key"].startswith("quiz:%d:" % second_attempt) for row in second_rows)
    first_key = "quiz:%d:%d:%d:q1" % (first_attempt, course["id"], scene_id)
    second_key = "quiz:%d:%d:%d:q1" % (second_attempt, course["id"], scene_id)
    assert study_evidence.active_event(user_id, first_key)["record_id"] == first_attempt
    assert study_evidence.active_event(user_id, second_key)["record_id"] == second_attempt
    # 同一题同一账号两次作答 → 两条有效证据（属于不同 attempt，正常）。
    single = study_evidence.question_evidence(user_id, "bank:docA:1")
    assert single["counts"]["total"] == 2
    assert {event["record_id"] for event in single["events"]} == {first_attempt, second_attempt}


def test_same_attempt_evidence_is_idempotent(client):
    """同一 attempt 的同一题重放证据写入：事件键稳定，只保留一条有效证据。"""
    user_id = uid_of(client)
    course = create_course(client)
    questions = basic_questions(with_refs=True)
    scene_id = insert_quiz_scene(course["id"], user_id, questions)
    session_id = create_session(client, course["id"])

    response = submit(client, session_id, scene_id, BASIC_ANSWERS)
    assert response.status_code == 200, response.text
    attempt_id = int(response.json()["attempt_id"])
    before = evidence_rows(user_id, attempt_id=attempt_id)
    assert len(before) == 3

    content = {"type": "quiz", "questions": questions}
    result = grading.grade_quiz(content, BASIC_ANSWERS)
    with db.connection() as con:
        classroom_api._record_quiz_evidence(
            con=con, user_id=user_id, course_id=course["id"], scene_id=scene_id,
            attempt_id=attempt_id, content=content, result=result,
        )
    after = evidence_rows(user_id, attempt_id=attempt_id)
    assert len(after) == 3
    assert [row["id"] for row in after] == [row["id"] for row in before]


# ---------------------------------------------------------------------------
# 5. 两账号证据隔离
# ---------------------------------------------------------------------------

def test_two_accounts_evidence_isolated(client, second_client):
    first_user = uid_of(client)
    second_user = uid_of(second_client)
    assert first_user != second_user

    # 账号 A：答对同一道带引用题目。
    course_a = create_course(client)
    scene_a = insert_quiz_scene(course_a["id"], first_user, basic_questions(with_refs=True))
    session_a = create_session(client, course_a["id"])
    response_a = submit(client, session_a, scene_a, BASIC_ANSWERS)
    assert response_a.status_code == 200, response_a.text
    attempt_a = int(response_a.json()["attempt_id"])

    # 账号 B：自己的课程/场景，同一 question_ref，答错。
    course_b = create_course(second_client)
    scene_b = insert_quiz_scene(course_b["id"], second_user, basic_questions(with_refs=True))
    session_b = create_session(second_client, course_b["id"])
    answers_b = [
        {"question_id": "q1", "values": ["B"]},  # 错
        {"question_id": "q2", "values": ["A", "C"]},  # 对
        {"question_id": "q3", "values": ["套话"]},
    ]
    response_b = submit(second_client, session_b, scene_b, answers_b)
    assert response_b.status_code == 200, response_b.text
    attempt_b = int(response_b.json()["attempt_id"])
    assert attempt_a != attempt_b

    evidence_a = study_evidence.question_evidence(first_user, "bank:docA:1")
    evidence_b = study_evidence.question_evidence(second_user, "bank:docA:1")
    assert evidence_a["counts"] == {"total": 1, "correct": 1, "incorrect": 0, "pending": 0}
    assert evidence_b["counts"] == {"total": 1, "correct": 0, "incorrect": 1, "pending": 0}
    assert evidence_a["events"][0]["record_id"] == attempt_a
    assert evidence_b["events"][0]["record_id"] == attempt_b

    rows_a = evidence_rows(first_user)
    rows_b = evidence_rows(second_user)
    assert {int(row["record_id"]) for row in rows_a} == {attempt_a}
    assert {int(row["record_id"]) for row in rows_b} == {attempt_b}
    assert study_evidence.summary(first_user)["by_kind"]["classroom_quiz"] == 3
    assert study_evidence.summary(second_user)["by_kind"]["classroom_quiz"] == 3
    assert study_evidence.active_event(first_user, "quiz:%d:%d:%d:q1"
                                       % (attempt_b, course_b["id"], scene_b)) is None


# ---------------------------------------------------------------------------
# 6. 既有测验判分行为不退化
# ---------------------------------------------------------------------------

QUIZ_RAW = {"questions": [
    {"id": "q1", "type": "single", "question": "光合作用发生的场所是？",
     "options": [{"label": "叶绿体", "value": "A"}, {"label": "线粒体", "value": "B"}],
     "answer": ["A"], "analysis": "叶绿体是场所。", "points": 10},
    {"id": "q2", "type": "multiple", "question": "光合作用的原料包括？（多选）",
     "options": [{"label": "水", "value": "A"}, {"label": "氧气", "value": "B"},
                 {"label": "二氧化碳", "value": "C"}],
     "answer": ["A", "C"], "analysis": "水和二氧化碳是原料。", "points": 10},
    {"id": "q3", "type": "short_answer", "question": "简述光合作用的意义。",
     "commentPrompt": "评分标准：要点完整", "analysis": "参考要点。", "points": 20},
]}


def test_existing_quiz_grading_not_degraded(client):
    user_id = uid_of(client)
    course = create_course(client)
    quiz_content, _ = schema.normalize_quiz_content(QUIZ_RAW)
    now = db.utcnow()
    with db.connection() as con:
        scene_id = int(con.execute(
            "INSERT INTO cls_scenes(course_id,user_id,scene_type,order_index,title,content_json,meta_json,"
            "status,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (course["id"], user_id, "quiz", 1, "课堂小测", cdb.json_dump(quiz_content),
             cdb.json_dump({"actions": [{"id": "act_001", "type": "speech", "text": "先做题"}]}),
             "ready", now, now),
        ).lastrowid)
    session_id = create_session(client, course["id"])

    response = submit(client, session_id, scene_id, BASIC_ANSWERS)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["score"] == 10
    assert body["max_score"] == 40
    assert body["correct_count"] == 1
    assert body["question_count"] == 3
    assert body["needs_review_count"] == 1
    by_id = {item["question_id"]: item for item in body["detail"]}
    assert by_id["q1"]["correct"] is True and by_id["q1"]["earned"] == 10
    assert by_id["q2"]["correct"] is False and by_id["q2"]["correct_answer"] == ["A", "C"]
    assert by_id["q3"]["needs_review"] is True and by_id["q3"]["earned"] == 0
    assert by_id["q1"]["analysis"]
    # 既有响应字段语义不变（证据信息不回写响应）。
    assert set(body) == {
        "attempt_id", "scene_id", "score", "max_score", "correct_count",
        "question_count", "needs_review_count", "detail",
    }
    with db.connection() as con:
        attempt = con.execute(
            "SELECT * FROM cls_quiz_attempts WHERE id=?", (body["attempt_id"],)
        ).fetchone()
    assert attempt is not None and attempt["score"] == 10

    # 归一化会丢弃未声明的 question_ref，因此这些课堂题一律不伪造引用。
    rows = evidence_rows(user_id, attempt_id=int(body["attempt_id"]))
    assert len(rows) == 3
    assert all(row["question_ref"] is None for row in rows)


# ---------------------------------------------------------------------------
# 7. 判分只有总分时：只写一条汇总证据，不编造逐题对错
# ---------------------------------------------------------------------------

def test_total_only_grading_writes_summary_evidence(client, monkeypatch):
    user_id = uid_of(client)
    course = create_course(client)
    questions = basic_questions(with_refs=False)
    scene_id = insert_quiz_scene(course["id"], user_id, questions)
    session_id = create_session(client, course["id"])

    calls = []

    def total_only_grade(content, answers):
        calls.append({"question_count": len(content.get("questions") or []), "answers": answers})
        return {
            "score": 5.0, "max_score": 10.0, "correct_count": 1,
            "question_count": 2, "needs_review_count": 0, "detail": [],
        }

    monkeypatch.setattr(grading, "grade_quiz", total_only_grade)
    response = submit(client, session_id, scene_id, BASIC_ANSWERS)
    assert response.status_code == 200, response.text
    body = response.json()
    assert calls and calls[0]["answers"] == BASIC_ANSWERS
    assert body["score"] == 5.0 and body["max_score"] == 10.0
    assert body["detail"] == []

    rows = evidence_rows(user_id, attempt_id=int(body["attempt_id"]))
    assert len(rows) == 1
    row = rows[0]
    assert row["item_key"] == "summary"
    assert row["event_key"] == "quiz:%d:summary" % int(body["attempt_id"])
    assert row["question_ref"] is None
    assert row["verdict"] == "pending" and row["grade_source"] == "pending"
    assert row["confidence"] == "pending"
    assert row["reveal_state"] == "after" and row["prompt_state"] == "unknown"
    meta = evidence_meta(row)
    assert meta["course_id"] == course["id"] and meta["scene_id"] == scene_id
    assert meta["score"] == 5.0 and meta["max_score"] == 10.0
    assert "无逐题核验" in meta["note"]
