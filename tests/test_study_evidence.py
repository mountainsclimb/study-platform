"""学习证据共用模块（app/study_evidence.py）的隔离测试。

覆盖：幂等（同事件只一行）、修订版本链（仅一条有效、旧行 superseded_by 指向新行）、
置信级别推导、不同 kind 独立、两账号隔离、单题证据统计与独立样本判断、自评不影响核验证据、
汇总计数与时间窗口、event_key 生成规则、路由参数校验（401 / 422）以及「传入 con 不自行提交」。

全部用 ``isolated_db`` 把数据目录指到 tmp_path，不读写真实应用库，也不依赖题库。
"""
import threading

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import db, question_ref, study_evidence
from app.auth import current_user

USER_A = 901
USER_B = 902


@pytest.fixture
def evidence_db(isolated_db):
    """隔离数据目录 + 幂等建表。"""
    study_evidence.init_study_evidence()
    return isolated_db


@pytest.fixture
def evidence_api(evidence_db):
    """本模块 router 的独立小应用（Lead 稍后才把 router 接进 app.main）。

    通过 ``dependency_overrides`` 指定当前用户，测试内切换 ``holder["current"]`` 即可模拟
    两个账号，不依赖真实登录状态。
    """
    app = FastAPI()
    app.include_router(study_evidence.router)
    holder = {"current": {"id": USER_A, "username": "证据测试甲", "role": "student"}}
    app.dependency_overrides[current_user] = lambda: holder["current"]
    with TestClient(app) as client:
        yield client, holder


def _count(user_id=None) -> int:
    with db.connection() as con:
        if user_id is None:
            return int(con.execute("SELECT COUNT(*) FROM study_evidence").fetchone()[0])
        return int(con.execute(
            "SELECT COUNT(*) FROM study_evidence WHERE user_id=?", (user_id,)
        ).fetchone()[0])


def _rows(user_id, *, valid=None) -> list[dict]:
    sql = "SELECT * FROM study_evidence WHERE user_id=?"
    params: list = [user_id]
    if valid is not None:
        sql += " AND valid=?"
        params.append(valid)
    sql += " ORDER BY id"
    with db.connection() as con:
        return [dict(row) for row in con.execute(sql, params).fetchall()]


def _valid_rows(user_id) -> list[dict]:
    return _rows(user_id, valid=1)


# ---------------------------------------------------------------------------
# 建表与事件键
# ---------------------------------------------------------------------------

def test_init_is_idempotent(evidence_db):
    study_evidence.init_study_evidence()
    study_evidence.init_study_evidence()
    with db.connection() as con:
        assert "study_evidence" in db.table_names(con)
        columns = {row[1] for row in con.execute("PRAGMA table_info(study_evidence)")}
    assert {
        "id", "user_id", "event_key", "kind", "question_ref", "record_kind", "record_id",
        "item_key", "verdict", "grade_source", "confidence", "prompt_state", "reveal_state",
        "self_report", "evidence_version", "superseded_by", "valid", "occurred_at",
        "created_at", "meta_json",
    } <= columns


def test_default_event_key_rules(evidence_db):
    event = study_evidence.record_event(
        USER_A, kind="attempt", record_kind="attempts", record_id=7,
        verdict="correct", grade_source="reference",
    )
    assert event["event_key"] == "attempt:attempts:7:-"
    with_item = study_evidence.record_event(
        USER_A, kind="paper_attempt", record_kind="pl_paper_attempts", record_id=9,
        item_key="paper:9:3", verdict="incorrect", grade_source="reference",
    )
    assert with_item["event_key"] == "paper_attempt:pl_paper_attempts:9:paper:9:3"
    # 与 practice.py AI 复核（set_ai_grade）共用的便捷键完全一致。
    assert study_evidence.attempt_event_key(7) == "attempt:attempts:7:-"


# ---------------------------------------------------------------------------
# 幂等与置信级别
# ---------------------------------------------------------------------------

def test_duplicate_event_keeps_one_row_and_returns_same_id(evidence_db):
    first = study_evidence.record_attempt_evidence(
        USER_A, ref="bank:q1", attempt_id=1, verdict="correct", grade_source="reference",
    )
    second = study_evidence.record_attempt_evidence(
        USER_A, ref="bank:q1", attempt_id=1, verdict="correct", grade_source="reference",
    )
    assert first["id"] == second["id"]
    assert _count(USER_A) == 1


def test_confidence_is_derived_from_verdict_and_source(evidence_db):
    verified = study_evidence.record_attempt_evidence(
        USER_A, ref="bank:q2", attempt_id=1, verdict="correct", grade_source="ai_cross_verified",
    )
    assert verified["confidence"] == "verified"
    assert verified["kind"] == "attempt"
    assert verified["record_kind"] == "attempts"
    assert verified["record_id"] == 1
    assert verified["question_ref"] == "bank:q2"

    pending = study_evidence.record_attempt_evidence(
        USER_A, ref="bank:q2", attempt_id=2, verdict="pending", grade_source="pending",
    )
    assert pending["confidence"] == "pending"

    manual = study_evidence.record_attempt_evidence(
        USER_A, ref="bank:q2", attempt_id=3, verdict="incorrect", grade_source="manual",
    )
    assert manual["confidence"] == "verified"

    # 自评/缺失信息只能是 unknown，不伪造成 verified。
    self_graded = study_evidence.record_attempt_evidence(
        USER_A, ref="q2-bare", attempt_id=4, verdict="correct", grade_source="self",
    )
    assert self_graded["confidence"] == "unknown"
    assert self_graded["question_ref"] == "bank:q2-bare"


def test_all_question_ref_forms_are_normalized(evidence_db):
    cases = {
        "docA:1": "bank:docA:1",
        "bank:docA:1": "bank:docA:1",
        "app:12": "app:12",
        "official:plz-9": "official:plz-9",
    }
    for index, (raw, expected) in enumerate(cases.items(), start=1):
        event = study_evidence.record_attempt_evidence(
            USER_A, ref=raw, attempt_id=index, verdict="correct", grade_source="reference",
        )
        assert event["question_ref"] == expected


# ---------------------------------------------------------------------------
# 修订（重判）
# ---------------------------------------------------------------------------

def test_revise_creates_version_and_marks_old_row(evidence_db):
    first = study_evidence.record_attempt_evidence(
        USER_A, ref="bank:q3", attempt_id=11, verdict="correct", grade_source="reference",
    )
    revised = study_evidence.revise_event(USER_A, first["event_key"], verdict="incorrect")
    assert revised["id"] != first["id"]
    assert revised["evidence_version"] == 2
    assert revised["valid"] == 1
    assert revised["verdict"] == "incorrect"
    assert revised["grade_source"] == "reference"  # 未提供字段沿用旧值
    assert revised["question_ref"] == "bank:q3"

    rows = _rows(USER_A)
    assert len(rows) == 2
    old, new = rows
    assert old["valid"] == 0
    assert old["superseded_by"] == new["id"]
    assert new["evidence_version"] == 2
    assert len(_valid_rows(USER_A)) == 1

    active = study_evidence.active_event(USER_A, first["event_key"])
    assert active is not None and active["id"] == revised["id"]

    # 重判不改变有效证据条数；也可以用修订后返回的 event_key 继续修订。
    assert study_evidence.question_evidence(USER_A, "bank:q3")["counts"]["total"] == 1
    third = study_evidence.revise_event(USER_A, revised["event_key"], verdict="correct")
    assert third["evidence_version"] == 3
    assert [row["valid"] for row in _rows(USER_A)] == [0, 0, 1]


def test_revise_keeps_unprovided_fields(evidence_db):
    event = study_evidence.record_attempt_evidence(
        USER_A, ref="bank:q4", attempt_id=21, verdict="correct", grade_source="reference",
        prompt_state="hint", reveal_state="after", self_report="assisted",
        occurred_at="2026-01-02T03:04:05+00:00",
        meta={"source": "practice", "attempt_no": 2},
    )
    revised = study_evidence.revise_event(USER_A, event["event_key"], verdict="pending")
    assert revised["verdict"] == "pending"
    assert revised["prompt_state"] == "hint"
    assert revised["reveal_state"] == "after"
    assert revised["self_report"] == "assisted"
    assert revised["occurred_at"] == event["occurred_at"]
    assert revised["meta"] == {"source": "practice", "attempt_no": 2}


def test_revise_missing_event_degrades_to_record_event(evidence_db):
    created = study_evidence.revise_event(
        USER_A, "attempt:attempts:99:-", verdict="correct", grade_source="reference",
    )
    assert created["evidence_version"] == 1
    assert created["valid"] == 1
    assert created["kind"] == "attempt"
    assert created["record_kind"] == "attempts"
    assert created["record_id"] == 99
    assert created["event_key"] == "attempt:attempts:99:-"
    assert _count(USER_A) == 1


def test_revise_attempt_evidence_creates_then_updates(evidence_db):
    created = study_evidence.revise_attempt_evidence(
        USER_A, attempt_id=31, ref="app:7", verdict="pending", grade_source="pending",
    )
    assert created["kind"] == "attempt"
    assert created["record_id"] == 31
    assert created["question_ref"] == "app:7"
    assert created["evidence_version"] == 1
    assert created["confidence"] == "pending"

    revised = study_evidence.revise_attempt_evidence(
        USER_A, attempt_id=31, verdict="correct", grade_source="ai_cross_verified",
    )
    assert revised["evidence_version"] == 2
    # 库内修订行带版本后缀，调用方仍用基础键读取（active_event / question_evidence）。
    assert revised["event_key"].startswith("attempt:attempts:31:-#v")
    assert study_evidence.active_event(USER_A, "attempt:attempts:31:-")["id"] == revised["id"]
    assert revised["question_ref"] == "app:7"  # 未提供 ref 时沿用旧值
    assert revised["confidence"] == "verified"
    assert len(_valid_rows(USER_A)) == 1


# ---------------------------------------------------------------------------
# kind 独立与账号隔离
# ---------------------------------------------------------------------------

def test_different_kinds_are_independent(evidence_db):
    study_evidence.record_event(
        USER_A, kind="attempt", record_kind="attempts", record_id=61,
        question_ref="bank:q5", verdict="correct", grade_source="reference",
    )
    study_evidence.record_event(
        USER_A, kind="paper_attempt", record_kind="pl_paper_attempts", record_id=62,
        question_ref="bank:q5", verdict="incorrect", grade_source="reference",
    )
    study_evidence.record_event(
        USER_A, kind="classroom_quiz", record_kind="cls_quiz_attempts", record_id=63,
        question_ref="bank:q5", verdict="correct", grade_source="manual",
    )
    found = study_evidence.question_evidence(USER_A, "bank:q5")
    assert found["counts"] == {"total": 3, "correct": 2, "incorrect": 1, "pending": 0}

    payload = study_evidence.summary(USER_A)
    assert payload["by_kind"]["attempt"] == 1
    assert payload["by_kind"]["paper_attempt"] == 1
    assert payload["by_kind"]["classroom_quiz"] == 1
    assert payload["by_source"]["manual"] == 1


def test_users_are_isolated(evidence_db):
    study_evidence.record_attempt_evidence(
        USER_A, ref="bank:q6", attempt_id=51, verdict="correct", grade_source="reference",
    )
    assert _count(USER_B) == 0
    assert study_evidence.question_evidence(USER_B, "bank:q6")["counts"]["total"] == 0
    assert study_evidence.active_event(USER_B, "attempt:attempts:51:-") is None
    assert study_evidence.summary(USER_B)["effective_count"] == 0
    assert study_evidence.summary(USER_A)["effective_count"] == 1


# ---------------------------------------------------------------------------
# 单题证据：计数、最新有效事件、独立作答样本
# ---------------------------------------------------------------------------

def test_question_evidence_counts_and_sample_sufficiency(evidence_db):
    for attempt_id in (41, 42):
        study_evidence.record_attempt_evidence(
            USER_A, ref="bank:q7", attempt_id=attempt_id, verdict="correct",
            grade_source="reference", prompt_state="none", reveal_state="before",
        )
    study_evidence.record_attempt_evidence(
        USER_A, ref="bank:q7", attempt_id=43, verdict="pending", grade_source="pending",
    )
    found = study_evidence.question_evidence(USER_A, "q7")  # 裸 qid 兼容旧键
    assert found["ref"] == "q7"  # ref 回显调用方传入的原始引用
    assert found["question_ref"] == "bank:q7"  # question_ref 是规范化后的统一引用
    assert found["counts"] == {"total": 3, "correct": 2, "incorrect": 0, "pending": 1}
    assert found["first_independent_correct"] is True
    assert found["sample_sufficient"] is True
    assert len(found["events"]) == 3
    assert found["effective"]["id"] == found["events"][0]["id"]
    assert "样本充足" in found["note"]


def test_question_evidence_without_independent_context_reports_insufficient(evidence_db):
    study_evidence.record_attempt_evidence(
        USER_A, ref="bank:q8", attempt_id=44, verdict="correct",
        grade_source="reference", prompt_state="hint", reveal_state="before",
    )
    found = study_evidence.question_evidence(USER_A, "bank:q8")
    assert found["counts"]["correct"] == 1
    # 看过提示不算独立作答：不能据此判断「首次独立答对」或样本充足。
    assert found["first_independent_correct"] is False
    assert found["sample_sufficient"] is False
    assert "样本不足" in found["note"]

    empty = study_evidence.question_evidence(USER_A, "bank:never-seen")
    assert empty["counts"] == {"total": 0, "correct": 0, "incorrect": 0, "pending": 0}
    assert empty["effective"] is None
    assert empty["events"] == []
    assert "暂无本人学习证据" in empty["note"]


def test_self_selected_independent_marker_counts_as_independent(evidence_db):
    study_evidence.record_attempt_evidence(
        USER_A, ref="bank:q9", attempt_id=45, verdict="correct", grade_source="reference",
        self_report="independent", prompt_state="unknown", reveal_state="unknown",
    )
    found = study_evidence.question_evidence(USER_A, "bank:q9")
    assert found["first_independent_correct"] is True
    assert found["sample_sufficient"] is False  # 只有 1 条，仍不足 2 条


# ---------------------------------------------------------------------------
# 汇总：计数与时间窗口
# ---------------------------------------------------------------------------

def test_summary_counts_and_window(evidence_db):
    study_evidence.record_attempt_evidence(
        USER_A, ref="bank:qw1", attempt_id=81, verdict="correct", grade_source="reference",
        occurred_at="2020-01-01T00:00:00+00:00",
    )
    revised = study_evidence.record_attempt_evidence(
        USER_A, ref="bank:qw2", attempt_id=82, verdict="incorrect", grade_source="reference",
    )
    study_evidence.record_event(
        USER_A, kind="review_rating", record_kind="review_cards", record_id=83,
        question_ref="bank:qw2", verdict="correct", grade_source="manual",
    )
    study_evidence.revise_event(USER_A, revised["event_key"], verdict="pending")

    payload = study_evidence.summary(USER_A, days=90)
    assert payload["window_days"] == 90
    assert payload["effective_count"] == 2  # qw1 在窗口外；qw2 有效版本 + 复习评分
    assert payload["revised_count"] == 1  # 被替换的旧版本行
    assert payload["by_kind"]["attempt"] == 1
    assert payload["by_kind"]["review_rating"] == 1
    assert payload["by_verdict"]["pending"] == 1
    assert payload["by_verdict"]["correct"] == 1
    assert payload["by_source"]["reference"] == 1
    assert payload["by_source"]["manual"] == 1
    assert payload["by_question_source"]["bank"] == 2
    assert payload["generated_at"]
    assert "有效证据" in payload["caliber"]["effective"]

    wide = study_evidence.summary(USER_A, days=study_evidence.MAX_DAYS)
    assert wide["effective_count"] == 3
    # days 越界按范围钳制，不抛错也不无限扩大。
    assert study_evidence.summary(USER_A, days=0)["window_days"] == 1
    assert study_evidence.summary(USER_A, days=10 ** 6)["window_days"] == 3650


# ---------------------------------------------------------------------------
# 传入 con：只在当前事务执行，不 commit、不自行开启事务
# ---------------------------------------------------------------------------

def test_record_with_con_does_not_commit(evidence_db):
    with db.connection() as con:
        event = study_evidence.record_attempt_evidence(
            USER_A, ref="bank:q10", attempt_id=71, verdict="correct",
            grade_source="reference", con=con,
        )
        assert event["id"] >= 1
        assert con.execute(
            "SELECT COUNT(*) FROM study_evidence WHERE user_id=?", (USER_A,)
        ).fetchone()[0] == 1
        # 外层回滚必须把证据一起回滚：本模块没有自行 commit。
        con.rollback()
    assert _count(USER_A) == 0


def test_revise_with_con_does_not_commit(evidence_db):
    first = study_evidence.record_attempt_evidence(
        USER_A, ref="bank:q11", attempt_id=72, verdict="correct", grade_source="reference",
    )
    with db.connection() as con:
        revised = study_evidence.revise_event(USER_A, first["event_key"], verdict="incorrect",
                                              con=con)
        assert revised["evidence_version"] == 2
        con.rollback()
    assert _count(USER_A) == 1
    assert _valid_rows(USER_A)[0]["verdict"] == "correct"


def test_record_then_revise_in_same_transaction(evidence_db):
    """practice 的接入方式：同一事务先记证据、AI 复核再修订，能读到本事务内未提交的 v1。"""
    with db.connection() as con:
        created = study_evidence.record_attempt_evidence(
            USER_A, ref="bank:q17", attempt_id=121, verdict="pending",
            grade_source="pending", con=con,
        )
        assert created["evidence_version"] == 1
        revised = study_evidence.revise_attempt_evidence(
            USER_A, attempt_id=121, verdict="correct", grade_source="ai_cross_verified", con=con,
        )
        assert revised["evidence_version"] == 2
        assert revised["question_ref"] == "bank:q17"
        valid_in_tx = con.execute(
            "SELECT COUNT(*) FROM study_evidence WHERE user_id=? AND valid=1", (USER_A,)
        ).fetchone()[0]
        assert valid_in_tx == 1
    assert _valid_rows(USER_A)[0]["verdict"] == "correct"


def test_concurrent_revise_keeps_exactly_one_valid_version(evidence_db):
    """两个并发重判（各自连接）串行化后只保留一条有效版本，历史版本各指向新版本。"""
    first = study_evidence.record_attempt_evidence(
        USER_A, ref="bank:q18", attempt_id=131, verdict="pending", grade_source="pending",
    )
    errors: list[Exception] = []

    def worker(verdict: str) -> None:
        try:
            study_evidence.revise_event(USER_A, first["event_key"], verdict=verdict)
        except Exception as exc:  # noqa: BLE001 - 线程内异常带回主线程断言
            errors.append(exc)

    threads = [
        threading.Thread(target=worker, args=(verdict,))
        for verdict in ("correct", "incorrect")
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    rows = _rows(USER_A)
    assert len(_valid_rows(USER_A)) == 1
    assert len(rows) == 3
    assert sum(1 for row in rows if row["superseded_by"]) == 2


# ---------------------------------------------------------------------------
# 参数校验
# ---------------------------------------------------------------------------

def test_invalid_enums_raise_value_error(evidence_db):
    with pytest.raises(ValueError):
        study_evidence.record_event(
            USER_A, kind="attempt", record_kind="attempts", verdict="maybe",
            grade_source="reference",
        )
    with pytest.raises(ValueError):
        study_evidence.record_event(
            USER_A, kind="unknown_kind", record_kind="attempts", verdict="correct",
            grade_source="reference",
        )
    with pytest.raises(ValueError):
        study_evidence.revise_event(USER_A, "attempt:attempts:1:-", prompt_state="maybe")
    with pytest.raises(ValueError):
        study_evidence.record_attempt_evidence(
            USER_A, ref="bank:q12", attempt_id=73, verdict="correct", grade_source="reference",
            occurred_at="不是时间",
        )
    with pytest.raises(question_ref.QuestionRefError):
        study_evidence.record_attempt_evidence(
            USER_A, ref="bank:", attempt_id=74, verdict="correct", grade_source="reference",
        )


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------

def test_route_summary_and_question(evidence_api):
    client, holder = evidence_api
    study_evidence.record_attempt_evidence(
        holder["current"]["id"], ref="bank:q13", attempt_id=91,
        verdict="correct", grade_source="reference",
        prompt_state="none", reveal_state="before",
    )
    body = client.get("/api/evidence/summary?days=90")
    assert body.status_code == 200, body.text
    payload = body.json()
    assert payload["effective_count"] == 1
    assert payload["window_days"] == 90
    assert isinstance(payload["caliber"], dict)

    found = client.get("/api/evidence/question/bank:q13")
    assert found.status_code == 200, found.text
    data = found.json()
    assert data["question_ref"] == "bank:q13"
    assert data["counts"]["total"] == 1
    assert data["first_independent_correct"] is True
    # 不回显题干/答案/解析等题目内容。
    assert "stem" not in data and "answer" not in data

    invalid = client.get("/api/evidence/question/bank:")
    assert invalid.status_code == 422
    assert "题目引用" in invalid.json()["detail"]

    empty_ref = client.get("/api/evidence/question/")
    assert empty_ref.status_code == 422
    assert "题目引用" in empty_ref.json()["detail"]


def test_route_requires_login(evidence_api):
    client, _holder = evidence_api
    client.app.dependency_overrides.clear()
    assert client.get("/api/evidence/summary").status_code == 401


def test_route_self_report_does_not_change_verified_evidence(evidence_api):
    client, holder = evidence_api
    user_id = holder["current"]["id"]
    study_evidence.record_attempt_evidence(
        user_id, ref="bank:q14", attempt_id=101, verdict="incorrect", grade_source="reference",
    )
    before = study_evidence.question_evidence(user_id, "bank:q14")

    response = client.post("/api/evidence/self-report", json={
        "ref": "bank:q14", "verdict": "correct", "note": "其实是粗心算错",
    })
    assert response.status_code == 200, response.text
    event = response.json()
    assert event["kind"] == "self_report"
    assert event["record_kind"] == "self"
    assert event["verdict"] == "correct"
    assert event["grade_source"] == "self"
    assert event["self_report"] == "independent"
    assert event["confidence"] == "unknown"
    assert event["meta"] == {"note": "其实是粗心算错"}

    after = study_evidence.question_evidence(user_id, "bank:q14")
    # 自评不覆盖核验结果：计数与最新有效核验事件都不变，自评只作为独立事件出现。
    assert after["counts"] == before["counts"]
    assert after["effective"]["id"] == before["effective"]["id"]
    assert any(item["kind"] == "self_report" for item in after["events"])
    assert _count(user_id) == 2

    summary_payload = client.get("/api/evidence/summary").json()
    assert summary_payload["by_kind"]["self_report"] == 1
    assert summary_payload["by_self_report"]["independent"] == 1
    # 自评事件不计入单题核验计数（见上），汇总层仍按事件类型单列。

    # 同一 ref + verdict + note 重复提交：event_key 幂等，只保留一行。
    repeat = client.post("/api/evidence/self-report", json={
        "ref": "bank:q14", "verdict": "correct", "note": "其实是粗心算错",
    })
    assert repeat.status_code == 200
    assert repeat.json()["id"] == event["id"]
    assert _count(user_id) == 2


def test_route_self_report_validation(evidence_api):
    client, _holder = evidence_api
    bad_verdict = client.post("/api/evidence/self-report", json={
        "ref": "bank:q15", "verdict": "maybe",
    })
    assert bad_verdict.status_code == 422
    assert "自评结论" in bad_verdict.json()["detail"]

    bad_ref = client.post("/api/evidence/self-report", json={
        "ref": "bank:", "verdict": "correct",
    })
    assert bad_ref.status_code == 422
    assert "题目引用" in bad_ref.json()["detail"]


def test_route_two_users_are_isolated(evidence_api):
    client, holder = evidence_api
    study_evidence.record_attempt_evidence(
        USER_A, ref="bank:q16", attempt_id=111, verdict="correct", grade_source="reference",
    )
    assert client.get("/api/evidence/question/bank:q16").json()["counts"]["total"] == 1

    holder["current"] = {"id": USER_B, "username": "证据测试乙", "role": "student"}
    other = client.get("/api/evidence/question/bank:q16").json()
    assert other["counts"]["total"] == 0
    assert other["events"] == []
    assert client.get("/api/evidence/summary").json()["effective_count"] == 0
