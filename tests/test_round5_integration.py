"""第五轮共用接口与主链路集成测试（方案 §4 / §8 工单 C、E）。

覆盖：作答 → 学习证据 → 错题/复习卡 → 四档评分幂等与版本冲突；学习附件跨页引用与
两账号隔离；拍题确认的识别块往返与非法块拒绝；引擎不可用时的降级与恢复重试。
只读题库，全部数据落在 ``isolated_db`` 的临时目录。
"""
from __future__ import annotations

import io
import secrets

import pytest
from PIL import Image, ImageDraw


def _png_bytes() -> bytes:
    image = Image.new("RGB", (640, 360), "white")
    draw = ImageDraw.Draw(image)
    draw.text((20, 20), "f(x)=x^2+2x", fill="black")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _pick_practice_question(client) -> dict:
    """挑一道可自动判分的题库单选题（返回 ref/qid/选项标签）。"""
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


def _submit_wrong_attempt(client, question: dict) -> dict:
    submission_id = "r5-" + secrets.token_hex(4)
    response = client.post(
        "/api/attempts",
        data={"qid": question["ref"], "answer": question["labels"][-1], "submission_id": submission_id},
    )
    assert response.status_code == 200, response.text
    return response.json()


def _find_card(client, question: dict) -> dict | None:
    due = client.get("/api/review-cards/due", params={"include_future": "true", "limit": 200})
    assert due.status_code == 200, due.text
    for card in due.json().get("items") or []:
        if card.get("question_ref") == question["ref"] or card.get("qid") == question["qid"]:
            return card
    return None


# ---------------------------------------------------------------------------
# 作答证据与四档复习
# ---------------------------------------------------------------------------


def test_attempt_records_evidence_and_review_card(client):
    question = _pick_practice_question(client)
    recorded = _submit_wrong_attempt(client, question)
    assert recorded.get("duplicate") is False

    evidence = client.get(f"/api/evidence/question/{question['ref']}")
    assert evidence.status_code == 200, evidence.text
    body = evidence.json()
    assert body["counts"]["total"] == 1
    assert body["effective"]["verdict"] in ("correct", "incorrect")
    assert body["effective"]["grade_source"] == "reference"

    sync = client.post("/api/review-cards/sync")
    assert sync.status_code == 200, sync.text
    card = _find_card(client, question)
    assert card is not None, "错误作答应自动生成复习卡"
    assert card["question_ref"] == question["ref"]


def test_duplicate_submission_records_single_evidence(client):
    question = _pick_practice_question(client)
    submission_id = "r5-dup-" + secrets.token_hex(4)
    payload = {"qid": question["ref"], "answer": question["labels"][0], "submission_id": submission_id}
    first = client.post("/api/attempts", data=payload)
    second = client.post("/api/attempts", data=payload)
    assert first.status_code == 200 and second.status_code == 200
    assert second.json().get("duplicate") is True

    history = client.get("/api/attempts/history", params={"qid": question["ref"]}).json()["items"]
    assert len(history) == 1
    body = client.get(f"/api/evidence/question/{question['ref']}").json()
    assert body["counts"]["total"] == 1


def test_review_rating_idempotent_and_version_conflict(client):
    question = _pick_practice_question(client)
    _submit_wrong_attempt(client, question)
    assert client.post("/api/review-cards/sync").status_code == 200
    card = _find_card(client, question)
    assert card is not None
    key = "r5-rate-" + secrets.token_hex(4)
    rated = client.post(
        f"/api/review-cards/{card['card_id']}/ratings",
        json={"rating": 3, "card_version": card["version"], "idempotency_key": key},
    )
    assert rated.status_code == 200, rated.text
    result = rated.json()
    assert result["degraded"] is False
    assert result["due_at"]

    replay = client.post(
        f"/api/review-cards/{card['card_id']}/ratings",
        json={"rating": 3, "card_version": card["version"], "idempotency_key": key},
    )
    assert replay.status_code == 200
    assert replay.json()["replayed"] is True
    assert replay.json()["due_at"] == result["due_at"]

    stale = client.post(
        f"/api/review-cards/{card['card_id']}/ratings",
        json={"rating": 4, "card_version": card["version"],
              "idempotency_key": "r5-stale-" + secrets.token_hex(4)},
    )
    assert stale.status_code == 409


def test_review_engine_degraded_keeps_due_and_retry_applies(client, monkeypatch):
    question = _pick_practice_question(client)
    _submit_wrong_attempt(client, question)
    assert client.post("/api/review-cards/sync").status_code == 200
    card = _find_card(client, question)
    assert card is not None

    monkeypatch.setenv("STUDY_REVIEW_ENGINE", "external")
    key = "r5-engine-" + secrets.token_hex(4)
    degraded = client.post(
        f"/api/review-cards/{card['card_id']}/ratings",
        json={"rating": 4, "card_version": card["version"], "idempotency_key": key},
    )
    assert degraded.status_code == 200, degraded.text
    body = degraded.json()
    assert body["degraded"] is True and body["retry_allowed"] is True
    assert body["due_at"] == card["due_at"]

    monkeypatch.delenv("STUDY_REVIEW_ENGINE")
    retried = client.post(
        f"/api/review-cards/{card['card_id']}/ratings",
        json={"rating": 4, "card_version": card["version"], "idempotency_key": key},
    )
    assert retried.status_code == 200, retried.text
    applied = retried.json()
    assert applied["degraded"] is False and applied["replayed"] is False
    assert applied["due_at"] != card["due_at"]


# ---------------------------------------------------------------------------
# 学习附件与两账号隔离
# ---------------------------------------------------------------------------


def test_artifact_versions_and_cross_account_isolation(client, second_client):
    question = _pick_practice_question(client)
    created = client.post(
        "/api/artifacts",
        json={
            "kind": "whiteboard",
            "title": "解题白板（验收）",
            "data": {"elements": [{"type": "text", "x": 1, "y": 2, "text": "推导"}]},
            "links": [{"target_kind": "question", "target_key": question["ref"], "revision": 1}],
        },
    )
    assert created.status_code == 201, created.text
    artifact_id = created.json()["artifact"]["artifact_id"]

    revised = client.patch(
        f"/api/artifacts/{artifact_id}",
        json={"data": {"elements": [{"type": "text", "x": 1, "y": 2, "text": "推导（第二版）"}]},
              "expected_revision": 1},
    )
    assert revised.status_code == 200, revised.text
    stale = client.patch(
        f"/api/artifacts/{artifact_id}",
        json={"data": {"elements": []}, "expected_revision": 1},
    )
    assert stale.status_code == 409

    pinned = client.get("/api/artifacts/by-target",
                        params={"target_kind": "question", "target_key": question["ref"]})
    assert pinned.status_code == 200
    items = pinned.json()["items"]
    assert items and items[0]["revision"] == 1

    foreign = second_client.get(f"/api/artifacts/{artifact_id}")
    assert foreign.status_code == 404
    foreign_target = second_client.get(
        "/api/artifacts/by-target", params={"target_kind": "question", "target_key": question["ref"]}
    )
    assert foreign_target.status_code == 200 and foreign_target.json()["total"] == 0


def test_question_detail_exposes_study_context(client):
    question = _pick_practice_question(client)
    _submit_wrong_attempt(client, question)
    detail = client.get(f"/api/questions/{question['ref']}")
    assert detail.status_code == 200
    study = detail.json().get("study") or {}
    assert study.get("review_rating_scale") == [1, 2, 3, 4]
    assert (study.get("evidence") or {}).get("counts", {}).get("total") == 1
    evidence_event = study["evidence"].get("effective") or {}
    assert evidence_event.get("question_ref") == question["ref"]


# ---------------------------------------------------------------------------
# 拍题识别块与自评
# ---------------------------------------------------------------------------


def test_ocr_blocks_confirm_round_trip(client):
    upload = client.post("/api/uploads/question", files={"file": ("r5.png", _png_bytes(), "image/png")})
    assert upload.status_code == 200, upload.text
    upload_id = upload.json()["id"]
    blocks = [
        {"id": "b1", "kind": "text", "text": "已知函数 f(x)=x^2+2x。", "order": 0, "page": 1},
        {
            "id": "b2", "kind": "math", "text": "x^2+2x=0", "latex": "x^{2}+2x=0", "order": 1, "page": 1,
            "bbox": {"x": 12, "y": 40, "w": 180, "h": 24}, "norm": {"x": 0.05, "y": 0.15, "w": 0.4, "h": 0.06},
            "confidence": 0.9, "needs_review": False,
        },
    ]
    confirmed = client.patch(
        f"/api/uploads/{upload_id}/confirm",
        json={"stem": "已知函数 f(x)=x^2+2x。", "type": "short_answer", "blocks": blocks},
    )
    assert confirmed.status_code == 200, confirmed.text
    intake = client.get(f"/api/uploads/{upload_id}/intake").json()
    stored = (intake.get("confirmed") or {}).get("blocks") or []
    assert [block["id"] for block in stored] == ["b1", "b2"]
    assert stored[1]["latex"] == "x^{2}+2x=0"
    assert stored[1]["bbox"]["x"] == 12

    invalid = client.patch(
        f"/api/uploads/{upload_id}/confirm",
        json={"stem": "仍然合法的题干", "blocks": [{"id": "b1", "kind": "unknown", "text": "x"}]},
    )
    assert invalid.status_code == 422


def test_self_report_does_not_override_verified_evidence(client):
    question = _pick_practice_question(client)
    _submit_wrong_attempt(client, question)
    before = client.get(f"/api/evidence/question/{question['ref']}").json()
    report = client.post(
        "/api/evidence/self-report",
        json={"ref": question["ref"], "verdict": "correct", "note": "自评"},
    )
    assert report.status_code == 200, report.text
    after = client.get(f"/api/evidence/question/{question['ref']}").json()
    assert after["counts"]["total"] == before["counts"]["total"]
    assert after["effective"]["verdict"] == before["effective"]["verdict"]
    kinds = {event.get("kind") for event in after.get("events") or []}
    assert "self_report" in kinds
