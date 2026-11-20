"""P1 错题调度回归：同一轮作答最多推升一次间隔、恢复/重新入本重置、FSRS 时区统一。"""
from datetime import datetime, timezone

import pytest

from app import db
from app.review_schedule import fsrs_review


def _wrong_row(qid: str) -> dict:
    with db.connection() as con:
        row = con.execute("SELECT * FROM wrong_questions WHERE qid=?", (qid,)).fetchone()
    assert row is not None, qid
    return dict(row)


def test_mastery_repeated_clicks_push_interval_only_once(client):
    from test_practice import bank_question

    q = bank_question()
    qid = q["qid"]
    wrong = next(option["label"] for option in q["options"] if option["label"] != q["answer_text"])
    assert client.post("/api/attempts", data={"qid": qid, "answer": wrong}).json()["result"] == "incorrect"
    assert client.post("/api/attempts", data={"qid": qid, "answer": q["answer_text"]}).json()["result"] == "correct"

    assert client.post("/api/mastery", json={"qid": qid, "level": "熟练掌握"}).status_code == 200
    first = _wrong_row(qid)
    # 连点 4 次：同一轮作答内不得再乘 2.5 倍（旧实现 5 连点会到数百年）。
    for _ in range(4):
        assert client.post("/api/mastery", json={"qid": qid, "level": "熟练掌握"}).status_code == 200
    after = _wrong_row(qid)

    assert first["interval_days"] == after["interval_days"]
    assert after["interval_days"] < 100, after["interval_days"]
    assert first["next_review_at"] == after["next_review_at"]
    assert after["last_mastery_push_attempt"] is not None

    # 新一轮作答开启下一次推升机会（最多 +1 次，而不是每次点击累乘）。
    assert client.post("/api/attempts", data={"qid": qid, "answer": q["answer_text"]}).json()["result"] == "correct"
    assert client.post("/api/mastery", json={"qid": qid, "level": "熟练掌握"}).status_code == 200
    next_round = _wrong_row(qid)
    assert next_round["interval_days"] > after["interval_days"]
    assert next_round["last_mastery_push_attempt"] != after["last_mastery_push_attempt"]


def test_restore_and_readd_wrong_resets_schedule(client):
    from test_practice import bank_question

    q = bank_question()
    qid = q["qid"]
    wrong = next(option["label"] for option in q["options"] if option["label"] != q["answer_text"])
    client.post("/api/attempts", data={"qid": qid, "answer": wrong})

    def inflate_and_archive():
        with db.connection() as con:
            con.execute(
                "UPDATE wrong_questions SET interval_days=45, streak=2, archived=1, "
                "last_mastery_push_attempt=123 WHERE qid=?",
                (qid,),
            )

    # 恢复错题 = 重新入本：streak=0、interval_days=1。
    inflate_and_archive()
    assert client.post("/api/wrongbook/restore", json={"qid": qid}).status_code == 200
    restored = _wrong_row(qid)
    assert restored["archived"] == 0
    assert restored["streak"] == 0
    assert restored["interval_days"] == 1
    assert restored["last_mastery_push_attempt"] is None

    # 归档后重新加入错题本同样重置排程。
    inflate_and_archive()
    assert client.post("/api/wrongbook", json={"qid": qid}).status_code == 200
    readded = _wrong_row(qid)
    assert readded["archived"] == 0
    assert readded["streak"] == 0
    assert readded["interval_days"] == 1

    items = client.get("/api/wrongbook").json()["items"]
    assert next(item for item in items if item["qid"] == qid)["interval_days"] == 1


def test_fsrs_review_normalizes_timezone_to_utc():
    # 旧行可能是无时区 ISO：与 aware now 相减不得抛 TypeError。
    interval, stability, difficulty = fsrs_review(
        True, 2.0, 5.0, "2026-01-01T00:00:00", datetime(2026, 2, 1, tzinfo=timezone.utc)
    )
    assert interval >= 1
    assert 1.0 <= difficulty <= 10.0
    # 带 Z 的 UTC 字符串同样可用。
    interval_z, _, _ = fsrs_review(
        True, 2.0, 5.0, "2026-01-01T00:00:00Z", datetime(2026, 2, 1, tzinfo=timezone.utc)
    )
    assert interval_z == interval
    # 无法解析的历史值按「首次复习」处理，而不是 500。
    _, stability_bad, _ = fsrs_review(True, None, None, "not-a-date", datetime.now(timezone.utc))
    assert stability_bad == pytest.approx(3.7145)
