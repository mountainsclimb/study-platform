"""M2「统一到期值」集成测试（方案 §4「复习卡及日志」/§5.2「迁移」）。

验证服务端只有一个复习到期真相源（四档复习卡），并以同一事务镜像到旧
``wrong_questions.next_review_at``：首页、错题本、复习队列、学情与 /api/stats
看到同一天；真实评分推进排程后，普通判分不再覆盖已评分卡的到期；旧用户首次读取
自动惰性迁移；两账号互不可见。
"""
from __future__ import annotations

import secrets

import pytest

FIXED_CLOCK = "2026-12-01T00:00:00+00:00"
LATER_CLOCK = "2026-12-06T00:00:00+00:00"


def _pick_question(client) -> dict:
    for page in (1, 2):
        response = client.get("/api/questions", params={"subject": "数学", "page": page, "page_size": 30})
        assert response.status_code == 200, response.text
        for item in response.json().get("items") or []:
            if item.get("type") != "single_choice" or not item.get("practice_allowed", True):
                continue
            detail = client.get(f"/api/questions/{item['qid']}")
            if detail.status_code != 200:
                continue
            body = detail.json()
            labels = [str(option["label"]).upper() for option in body.get("options") or [] if option.get("label")]
            if len(labels) >= 2:
                return {"ref": body.get("ref") or item["qid"], "qid": item["qid"], "labels": labels}
    pytest.skip("题库中未找到可自动判分的单选题")


def _submit(client, question: dict, answer: str, submission_id: str | None = None) -> dict:
    response = client.post(
        "/api/attempts",
        data={
            "qid": question["ref"],
            "answer": answer,
            "submission_id": submission_id or ("m2-" + secrets.token_hex(4)),
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def _card(client, question: dict) -> dict:
    response = client.get("/api/review-cards/due", params={"include_future": "true", "limit": 200})
    assert response.status_code == 200, response.text
    for card in response.json().get("items") or []:
        if card.get("question_ref") == question["ref"] or card.get("qid") == question["qid"]:
            return card
    raise AssertionError("未找到题目对应的复习卡")


def _wrongbook_item(client, question: dict) -> dict:
    response = client.get("/api/wrongbook", params={"page_size": 200})
    assert response.status_code == 200, response.text
    for item in response.json().get("items") or []:
        if item.get("ref") == question["ref"] or item.get("qid") == question["qid"]:
            return item
    raise AssertionError("未找到错题本条目")


def _legacy_due(client, question: dict) -> str:
    from app import db

    with db.connection() as con:
        row = con.execute(
            "SELECT next_review_at FROM wrong_questions WHERE user_id=? AND qid=?",
            (int(client.get("/api/auth/me").json()["id"]), question["qid"]),
        ).fetchone()
    assert row is not None, "错题记录缺失"
    return str(row[0] or "")


def test_attempt_card_and_wrongbook_share_one_due_date(client, monkeypatch):
    monkeypatch.setenv("STUDY_REVIEW_CLOCK", FIXED_CLOCK)
    question = _pick_question(client)
    _submit(client, question, question["labels"][-1])

    card = _card(client, question)
    item = _wrongbook_item(client, question)
    assert item["due_source"] == "review-cards"
    assert item["next_review_at"] == card["due_at"]
    assert item["review_card_id"] == card["card_id"]
    assert _legacy_due(client, question) == card["due_at"]

    home = client.get("/api/insight/home").json()
    assert home["review"]["available"] is True
    assert home["review"]["due_source"] == "review-cards"
    assert home["review"]["algorithm"] == "fsrs-reference"
    overview = client.get("/api/insight/overview").json()
    assert overview["review"]["due_source"] == "review-cards"
    stats = client.get("/api/stats").json()
    assert stats["review"]["available"] is True and stats["review"]["due_now"] == home["review"]["due_now"]


def test_rating_advances_every_read_path(client, monkeypatch):
    monkeypatch.setenv("STUDY_REVIEW_CLOCK", FIXED_CLOCK)
    question = _pick_question(client)
    _submit(client, question, question["labels"][-1])
    card = _card(client, question)

    rated = client.post(
        f"/api/review-cards/{card['card_id']}/ratings",
        json={"rating": 3, "card_version": card["version"],
              "idempotency_key": "m2-rate-" + secrets.token_hex(4)},
    )
    assert rated.status_code == 200, rated.text
    due_after = rated.json()["due_at"]
    assert due_after != card["due_at"]

    # 旧表与卡同一天：错题本/首页/学情/统计读到的都是服务端返回的 due_at。
    assert _legacy_due(client, question) == due_after
    assert _wrongbook_item(client, question)["next_review_at"] == due_after
    home = client.get("/api/insight/home").json()
    assert home["review"]["due_now"] == 0  # 新到期在未来
    assert home["review"]["total"] >= 1

    # 时钟前进到新到期日：四个入口同时显示到期，/api/reviews 与 due_only 也同步。
    monkeypatch.setenv("STUDY_REVIEW_CLOCK", LATER_CLOCK)
    home = client.get("/api/insight/home").json()
    assert home["review"]["due_now"] >= 1
    overview = client.get("/api/insight/overview").json()
    assert overview["review"]["due_now"] == home["review"]["due_now"]
    stats = client.get("/api/stats").json()
    assert stats["review"]["due_now"] == home["review"]["due_now"]
    reviews = client.get("/api/reviews").json()
    assert any(item.get("ref") == question["ref"] for item in reviews["items"])
    due_only = client.get("/api/wrongbook", params={"due_only": "true"}).json()
    assert any(item.get("ref") == question["ref"] for item in due_only["items"])
    queue = client.get("/api/review/queue").json()
    assert any(item.get("ref") == question["ref"] for item in queue["items"])
    # 双读对账：镜像之后不存在漂移。
    compare = client.get("/api/review-cards/compare").json()
    assert compare["checked"] >= 1 and not compare["mismatched"]


def test_rated_card_due_is_authoritative_over_new_attempt(client, monkeypatch):
    monkeypatch.setenv("STUDY_REVIEW_CLOCK", FIXED_CLOCK)
    question = _pick_question(client)
    _submit(client, question, question["labels"][-1])
    card = _card(client, question)
    rated = client.post(
        f"/api/review-cards/{card['card_id']}/ratings",
        json={"rating": 3, "card_version": card["version"],
              "idempotency_key": "m2-auth-" + secrets.token_hex(4)},
    )
    assert rated.status_code == 200, rated.text
    scheduled_due = rated.json()["due_at"]

    # 再次答错：普通判分只更新失败次数/掌握度，不推进已评分卡的排程；旧表跟随卡。
    monkeypatch.setenv("STUDY_REVIEW_CLOCK", LATER_CLOCK)
    _submit(client, question, question["labels"][0])
    assert _legacy_due(client, question) == scheduled_due
    assert _card(client, question)["due_at"] == scheduled_due
    compare = client.get("/api/review-cards/compare").json()
    assert not compare["mismatched"]


def test_lazy_migration_for_legacy_wrong_question(client, monkeypatch):
    """旧用户只有 wrong_questions 行、没有复习卡：首次读首页/错题即迁移并统一到期。"""
    from app import db

    monkeypatch.setenv("STUDY_REVIEW_CLOCK", FIXED_CLOCK)
    question = _pick_question(client)
    user_id = int(client.get("/api/auth/me").json()["id"])
    due = "2026-11-30T00:00:00+00:00"
    with db.connection() as con:
        con.execute(
            "INSERT INTO wrong_questions(user_id,qid,reason,failures,streak,next_review_at,added_at,"
            "last_result,interval_days,review_count,archived,last_wrong_at) "
            "VALUES(?,?,?,1,0,?,?, 'incorrect',1,0,0,?)",
            (user_id, question["qid"], "迁移测试", due, "2026-11-20T00:00:00+00:00",
             "2026-11-20T00:00:00+00:00"),
        )
    home = client.get("/api/insight/home").json()
    assert home["review"]["available"] is True
    card = _card(client, question)
    assert card["due_at"] == due
    assert _legacy_due(client, question) == due
    item = _wrongbook_item(client, question)
    assert item["due_source"] == "review-cards" and item["next_review_at"] == due
    # 迁移幂等：再次读取不会重复创建卡。
    cards = client.get("/api/review-cards/due", params={"include_future": "true", "limit": 200}).json()
    assert sum(1 for entry in cards["items"] if entry["question_ref"] == question["ref"]) == 1


def test_due_isolation_between_accounts(client, second_client, monkeypatch):
    monkeypatch.setenv("STUDY_REVIEW_CLOCK", FIXED_CLOCK)
    question = _pick_question(client)
    _submit(client, question, question["labels"][-1])
    assert _card(client, question)["question_ref"] == question["ref"]

    home_b = second_client.get("/api/insight/home").json()
    assert home_b["review"]["available"] is True and home_b["review"]["total"] == 0
    wrongbook_b = second_client.get("/api/wrongbook").json()
    assert wrongbook_b["total"] == 0
    cards_b = second_client.get("/api/review-cards/due", params={"include_future": "true"}).json()
    assert cards_b["total"] == 0
