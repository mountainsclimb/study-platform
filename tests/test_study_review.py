"""四档复习卡与复习排程参考适配器（``app/study_review.py``）测试。

覆盖口径（与总方案 §4「复习卡及日志」/§5.2「排程/迁移」/§8 工单 E 对齐）：

* ``schedule`` 纯函数：new 卡四档间隔严格递增（1<2<4<16）、同输入同输出、Again 后
  ``lapses+1`` 且 ``state=relearning``、同日/长期间隔公式、间隔上下限与 1-2 组固定
  输入→期望输出的数值快照（先跑一次记录，再锁定）；
* 迁移：``sync_user_cards`` 幂等（第二次 created=0）、归档迁移、**已有卡真实排程状态不被覆盖**；
* 评分：正常评分写卡 + 复习日志、``version+1``；同幂等键重放不重复写；``card_version``
  冲突 409；评分越界/幂等键非法 422；
* 降级：``STUDY_REVIEW_ENGINE=external`` 时 ``degraded=true``、due 不变、不写日志；
  恢复后同幂等键正常应用且只应用一次；
* 双账号隔离（A 的卡 B 读 404）、``compare_due`` 新旧一致/不一致分类、到期队列排序稳定。

测试不依赖 ``app.main``：本文件用本地 FastAPI 只挂 ``study_review.router``，并用
``app.dependency_overrides[current_user]`` 注入当前用户；数据库用 conftest 的
``isolated_db`` + ``tmp_path``，不触碰真实 ``data/app.sqlite3``。
"""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import db, practice
from app import study_review as sr
from app.auth import current_user

FIXED_NOW = datetime(2026, 3, 1, 8, 0, tzinfo=timezone.utc)
FIXED_CLOCK = "2026-03-01T08:00:00+00:00"
USER_A = 1
USER_B = 2


# ---------------------------------------------------------------------------
# 夹具与工具
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch):
    """所有用例默认固定时钟，保证 due/created_at 可复现（单独用例可覆盖/删除）。"""
    monkeypatch.setenv("STUDY_REVIEW_CLOCK", FIXED_CLOCK)
    monkeypatch.delenv("STUDY_REVIEW_ENGINE", raising=False)


@pytest.fixture
def review_env(isolated_db):
    """真实应用库结构（users/wrong_questions）+ 本模块两张表 + 两个账号。"""
    db.init_db()
    practice.init_practice()
    sr.init_study_review()
    sr.init_study_review()  # 幂等：重复启动初始化不报错
    with db.connection() as con:
        for user_id, username in ((USER_A, "复习甲"), (USER_B, "复习乙")):
            con.execute(
                "INSERT INTO users(id,username,password_hash,role,created_at) VALUES(?,?,?,?,?)",
                (user_id, username, "x", "student", db.utcnow()),
            )
    return isolated_db


def _client_for(user_id: int) -> TestClient:
    """本地 FastAPI 只挂本模块路由，当前用户由依赖覆盖注入。"""
    app = FastAPI()
    app.include_router(sr.router)
    app.dependency_overrides[current_user] = lambda: {
        "id": user_id,
        "username": f"u{user_id}",
        "role": "student",
    }
    return TestClient(app)


def _insert_wrong(
    user_id: int,
    qid: str,
    *,
    next_review_at: str = "2026-02-25T08:00:00+00:00",
    archived: int = 0,
    added_at: str = "2026-02-01T00:00:00+00:00",
) -> None:
    with db.connection() as con:
        con.execute(
            "INSERT INTO wrong_questions(user_id,qid,reason,next_review_at,added_at,archived) "
            "VALUES(?,?,?,?,?,?)",
            (user_id, qid, "答题错误", next_review_at, added_at, archived),
        )


def _card_row(card_id: int, user_id: int = USER_A) -> dict:
    with db.connection() as con:
        row = con.execute(
            "SELECT * FROM review_cards WHERE id=? AND user_id=?", (card_id, user_id)
        ).fetchone()
    assert row is not None
    return dict(row)


def _rating_rows(user_id: int = USER_A) -> list[dict]:
    with db.connection() as con:
        rows = con.execute(
            "SELECT * FROM review_ratings WHERE user_id=? ORDER BY id", (user_id,)
        ).fetchall()
    return [dict(row) for row in rows]


def _sync(client: TestClient) -> dict:
    response = client.post("/api/review-cards/sync")
    assert response.status_code == 200, response.text
    return response.json()


def _new_state(**overrides) -> dict:
    state = {
        "state": "new",
        "stability": None,
        "difficulty": None,
        "reps": 0,
        "lapses": 0,
        "last_rating": None,
        "last_review": None,
        "due": "2026-02-20T08:00:00+00:00",
    }
    state.update(overrides)
    return state


def _review_state(**overrides) -> dict:
    state = {
        "state": "review",
        "stability": 10.0,
        "difficulty": 5.0,
        "reps": 3,
        "lapses": 0,
        "last_rating": 3,
        "last_review": (FIXED_NOW - timedelta(days=10)).isoformat(),
        "due": (FIXED_NOW - timedelta(days=1)).isoformat(),
    }
    state.update(overrides)
    return state


# ---------------------------------------------------------------------------
# schedule 纯函数
# ---------------------------------------------------------------------------


def test_fsrs_weights_are_fsrs5_default_19():
    """权重必须是 FSRS-5 公开默认 19 个（来源见模块 docstring/常量注释）。"""
    assert sr.FSRS_ALGORITHM == "fsrs-reference"
    assert sr.FSRS_ALGORITHM_VERSION == "5.0-local"
    assert sr.REQUEST_RETENTION == 0.9
    assert sr.MAX_INTERVAL_DAYS == 36500
    assert len(sr.FSRS_WEIGHTS) == 19
    assert sr.FSRS_WEIGHTS[0] == pytest.approx(0.40255)
    assert sr.FSRS_WEIGHTS[3] == pytest.approx(15.69105)
    assert sr.FSRS_WEIGHTS[18] == pytest.approx(0.6621)


def test_schedule_new_card_four_ratings_increase_and_deterministic():
    new = _new_state()
    outputs = {rating: sr.schedule(new, rating, FIXED_NOW) for rating in (1, 2, 3, 4)}
    intervals = [outputs[rating]["interval_days"] for rating in (1, 2, 3, 4)]
    # new 卡四档间隔严格递增：ceil(w0..w3) = 1 < 2 < 4 < 16。
    assert intervals == [1, 2, 4, 16]
    assert intervals[0] < intervals[1] < intervals[2] < intervals[3]
    for rating, expected_stability in zip((1, 2, 3, 4), sr.FSRS_WEIGHTS[:4]):
        output = outputs[rating]
        assert output["stability"] == pytest.approx(expected_stability, abs=1e-9)
        assert 1.0 <= output["difficulty"] <= 10.0
        assert output["reps"] == 1
        assert output["lapses"] == 0
        assert output["last_rating"] == rating
        assert output["last_review"] == FIXED_NOW.isoformat()
        assert output["due"] == (FIXED_NOW + timedelta(days=output["interval_days"])).isoformat()
        expected_state = "learning" if rating == 1 else "review"
        assert output["state"] == expected_state

    # 确定性：同输入同输出（含浮点与 ISO 字符串），不引入随机模糊。
    assert sr.schedule(new, 3, FIXED_NOW) == sr.schedule(new, 3, FIXED_NOW)
    # 时钟不同则 due 不同，但间隔只由卡状态与评分决定。
    later = sr.schedule(new, 3, FIXED_NOW + timedelta(days=1))
    assert later["interval_days"] == 4
    assert later["due"] == (FIXED_NOW + timedelta(days=5)).isoformat()


def test_schedule_review_numeric_snapshot():
    """固定输入（S=10, D=5, 距上次复习 10 天）→ 锁定数值。"""
    review = _review_state()
    outputs = {rating: sr.schedule(review, rating, FIXED_NOW) for rating in (1, 2, 3, 4)}

    good = outputs[3]
    assert good["state"] == "review"
    assert good["reps"] == 4
    assert good["lapses"] == 0
    assert good["stability"] == pytest.approx(32.95426399, abs=1e-6)
    assert good["difficulty"] == pytest.approx(4.99183271, abs=1e-6)
    assert good["interval_days"] == 33
    assert good["due"] == "2026-04-03T08:00:00+00:00"

    easy = outputs[4]
    assert easy["stability"] == pytest.approx(78.62865848, abs=1e-6)
    assert easy["difficulty"] == pytest.approx(4.18423151, abs=1e-6)
    assert easy["interval_days"] == 79

    hard = outputs[2]
    assert hard["stability"] == pytest.approx(15.31391211, abs=1e-6)
    assert hard["difficulty"] == pytest.approx(5.79943391, abs=1e-6)
    assert hard["interval_days"] == 16

    again = outputs[1]
    assert again["state"] == "relearning"
    assert again["lapses"] == 1
    assert again["stability"] == pytest.approx(2.10769626, abs=1e-6)
    assert again["difficulty"] == pytest.approx(6.60703511, abs=1e-6)
    assert again["interval_days"] == 3

    # 复习卡四档间隔同样严格递增（Again 走遗忘路径，间隔最小）。
    intervals = [outputs[rating]["interval_days"] for rating in (1, 2, 3, 4)]
    assert intervals == sorted(intervals) and len(set(intervals)) == 4


def test_schedule_again_lapses_and_relearning_recovery():
    review = _review_state(lapses=2)
    again = sr.schedule(review, 1, FIXED_NOW)
    assert again["state"] == "relearning"
    assert again["lapses"] == 3
    assert again["last_rating"] == 1

    # 恢复：relearning 评分成功（Good）回到 review，lapses 不再增加。
    recovered = sr.schedule(again, 3, FIXED_NOW + timedelta(days=1))
    assert recovered["state"] == "review"
    assert recovered["lapses"] == 3
    assert recovered["stability"] == pytest.approx(4.24512791, abs=1e-6)
    assert recovered["difficulty"] == pytest.approx(6.59147546, abs=1e-6)
    assert recovered["interval_days"] == 5


def test_schedule_new_again_enters_learning_without_lapse():
    new_again = sr.schedule(_new_state(), 1, FIXED_NOW)
    assert new_again["state"] == "learning"
    assert new_again["lapses"] == 0
    assert new_again["interval_days"] == 1
    # learning → review：间隔重新扩大，lapses 仍为 0。
    graduated = sr.schedule(new_again, 3, FIXED_NOW + timedelta(days=1))
    assert graduated["state"] == "review"
    assert graduated["lapses"] == 0
    assert graduated["stability"] == pytest.approx(2.26500942, abs=1e-6)
    assert graduated["interval_days"] == 3


def test_schedule_interval_bounds_and_same_day_formula():
    huge = _review_state(stability=10**9)
    bounded = sr.schedule(huge, 3, FIXED_NOW)
    assert bounded["stability"] == pytest.approx(36500.0)
    assert bounded["interval_days"] == 36500
    assert bounded["due"] == (FIXED_NOW + timedelta(days=36500)).isoformat()

    tiny = _review_state(stability=1e-6)
    floored = sr.schedule(tiny, 3, FIXED_NOW)
    assert floored["stability"] == pytest.approx(sr.MIN_STABILITY)
    assert floored["interval_days"] == 1

    # 同日（不足 1 天）重复评分走 FSRS-5 同日更新公式：S' = S·e^(w17(G-3+w18))。
    same_day = sr.schedule(_review_state(), 3, FIXED_NOW - timedelta(days=10) + timedelta(hours=2))
    assert same_day["stability"] == pytest.approx(
        10.0 * 2.718281828459045 ** (sr.FSRS_WEIGHTS[17] * (3 - 3 + sr.FSRS_WEIGHTS[18])), abs=1e-6
    )


def test_schedule_rejects_invalid_rating_and_now():
    for bad in (0, 5, -1, True, "3"):
        with pytest.raises(Exception) as excinfo:
            sr.schedule(_new_state(), bad, FIXED_NOW)
        assert getattr(excinfo.value, "status_code", None) == 422
    from fastapi import HTTPException

    with pytest.raises(HTTPException):
        sr.schedule(_new_state(), 3, "not-a-time")


def test_now_utc_uses_fixed_clock_and_falls_back(monkeypatch):
    assert sr.now_utc() == FIXED_NOW
    monkeypatch.setenv("STUDY_REVIEW_CLOCK", "2026-02-02T03:04:05Z")
    assert sr.now_utc() == datetime(2026, 2, 2, 3, 4, 5, tzinfo=timezone.utc)
    # 无法解析的固定时钟回退到真实 UTC 时钟，而不是抛异常。
    monkeypatch.setenv("STUDY_REVIEW_CLOCK", "not-a-clock")
    assert abs((sr.now_utc() - datetime.now(timezone.utc)).total_seconds()) < 30
    monkeypatch.delenv("STUDY_REVIEW_CLOCK")
    assert abs((sr.now_utc() - datetime.now(timezone.utc)).total_seconds()) < 30


# ---------------------------------------------------------------------------
# 迁移：wrong_questions → review_cards
# ---------------------------------------------------------------------------


def test_sync_idempotent_and_existing_schedule_not_overwritten(review_env):
    _insert_wrong(
        USER_A, "docA:1", next_review_at="2026-02-25T08:00:00+00:00", archived=0
    )
    _insert_wrong(
        USER_A, "app:12", next_review_at="2026-02-26T08:00:00+00:00", archived=1
    )
    client = _client_for(USER_A)

    first = _sync(client)
    assert first["created"] == 2
    assert first["updated"] == 0
    assert first["archived"] == 1
    assert first["total"] == 2
    assert first["algorithm"] == sr.FSRS_ALGORITHM
    assert first["algorithm_version"] == sr.FSRS_ALGORITHM_VERSION

    cards = {
        card["card_key"]: card
        for card in (sr.card_by_ref(USER_A, "docA:1"), sr.card_by_ref(USER_A, "app:12"))
    }
    assert set(cards) == {"q:docA:1", "q:app:12"}
    doc = cards["q:docA:1"]
    assert doc["card_type"] == "wrong"
    assert doc["source_kind"] == "migrated"
    assert doc["question_ref"] == "bank:docA:1"
    assert doc["qid"] == "docA:1"
    assert doc["state"] == "new"
    assert doc["due_at"] == "2026-02-25T08:00:00+00:00"  # 首次创建取旧到期
    assert doc["created_at"] == FIXED_CLOCK
    assert cards["q:app:12"]["archived"] is True

    # 第二次迁移幂等：created=0，已有卡不变。
    second = _sync(client)
    assert second["created"] == 0
    assert second["updated"] == 0
    assert second["unchanged"] == 2
    assert second["total"] == 2

    # 真实评分推进卡状态后，错的旧排程再变化也不得覆盖新卡 due/state。
    rated = client.post(
        f"/api/review-cards/{doc['card_id']}/ratings",
        json={"rating": 3, "card_version": doc["version"], "idempotency_key": "mig-rate-1"},
    )
    assert rated.status_code == 200, rated.text
    rated_body = rated.json()
    assert rated_body["due_at"] == "2026-03-05T08:00:00+00:00"
    with db.connection() as con:
        con.execute(
            "UPDATE wrong_questions SET next_review_at=? WHERE user_id=? AND qid=?",
            ("2026-04-01T00:00:00+00:00", USER_A, "docA:1"),
        )
    third = _sync(client)
    assert third["created"] == 0
    after = _card_row(doc["card_id"])
    assert after["due_at"] == "2026-03-05T08:00:00+00:00"
    assert after["version"] == rated_body["card"]["version"] == 2
    assert '"state": "review"' in after["state_json"]
    assert _rating_rows(USER_A)[0]["due_at"] == "2026-03-05T08:00:00+00:00"

    # 归档恢复：只同步归档标记，不重写 due。
    with db.connection() as con:
        con.execute(
            "UPDATE wrong_questions SET archived=0 WHERE user_id=? AND qid=?",
            (USER_A, "app:12"),
        )
    restored = _sync(client)
    assert restored["updated"] == 1
    assert restored["archived"] == 0
    app_card = sr.card_by_ref(USER_A, "app:12")
    assert app_card["archived"] is False
    assert app_card["due_at"] == "2026-02-26T08:00:00+00:00"
    assert app_card["version"] == 1  # 归档同步不推进版本


def test_sync_wrong_question_single_and_missing(review_env):
    assert sr.sync_wrong_question(USER_A, "docA:1") is None
    assert sr.card_by_ref(USER_A, "docA:1") is None
    assert sr.card_by_ref(USER_A, "") is None  # 非法引用 → None，不抛 500

    _insert_wrong(USER_A, "docA:1", next_review_at="2026-02-25T08:00:00+00:00")
    card = sr.sync_wrong_question(USER_A, "bank:docA:1")  # 带 bank: 前缀也能定位
    assert card is not None
    assert card["card_key"] == "q:docA:1"
    again = sr.sync_wrong_question(USER_A, "docA:1")
    assert again["card_id"] == card["card_id"]  # 不重复建卡

    with db.connection() as con:
        con.execute(
            "UPDATE wrong_questions SET archived=1 WHERE user_id=? AND qid=?",
            (USER_A, "docA:1"),
        )
    archived = sr.sync_wrong_question(USER_A, "docA:1")
    assert archived["archived"] is True
    assert sr.sync_wrong_question(USER_B, "docA:1") is None  # 别的账号不会拿到卡


def test_sync_skips_invalid_qid(review_env):
    _insert_wrong(USER_A, "docA:1", archived=0)
    with db.connection() as con:
        con.execute(
            "INSERT INTO wrong_questions(user_id,qid,reason,next_review_at,added_at,archived) "
            "VALUES(?,?,?,?,?,0)",
            (USER_A, "a b", "答题错误", "2026-02-25T08:00:00+00:00", db.utcnow()),
        )
    result = _sync(_client_for(USER_A))
    assert result["created"] == 1
    assert result["skipped"] == 1
    assert result["total"] == 2


# ---------------------------------------------------------------------------
# 评分：写卡 + 日志 + 幂等 + 版本冲突 + 参数校验
# ---------------------------------------------------------------------------


def test_rate_card_writes_card_version_and_rating_log(review_env):
    _insert_wrong(USER_A, "docA:1", next_review_at="2026-02-25T08:00:00+00:00")
    client = _client_for(USER_A)
    _sync(client)
    card = client.get("/api/review-cards/due").json()["items"][0]

    response = client.post(
        f"/api/review-cards/{card['card_id']}/ratings",
        json={"rating": 3, "card_version": 1, "idempotency_key": "rate-good-1"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["replay"] is False
    assert body["replayed"] is False
    assert body["degraded"] is False
    assert body["rating"] == 3
    assert body["due_at"] == "2026-03-05T08:00:00+00:00"
    assert body["algorithm"] == sr.FSRS_ALGORITHM
    assert body["algorithm_version"] == sr.FSRS_ALGORITHM_VERSION
    assert body["idempotency_key"] == "rate-good-1"
    updated = body["card"]
    assert updated["version"] == 2
    assert updated["state"] == "review"
    assert updated["reps"] == 1
    assert updated["lapses"] == 0
    assert updated["last_rating"] == 3
    assert updated["stability"] == pytest.approx(3.173)

    # 复习日志只写一行，含前后状态与算法版本。
    rows = _rating_rows(USER_A)
    assert len(rows) == 1
    assert rows[0]["card_id"] == card["card_id"]
    assert rows[0]["rating"] == 3
    assert rows[0]["card_version"] == 1
    assert rows[0]["due_at"] == "2026-03-05T08:00:00+00:00"
    assert rows[0]["degraded"] == 0
    assert '"state": "new"' in rows[0]["state_before_json"]
    assert '"state": "review"' in rows[0]["state_after_json"]

    single = client.get(f"/api/review-cards/{card['card_id']}")
    assert single.status_code == 200
    assert single.json()["card"]["version"] == 2

    stats = client.get("/api/review-cards/stats").json()
    assert stats["review"] == 1
    assert stats["new"] == 0
    assert stats["due_now"] == 0

    following = client.get("/api/review-cards/due?include_future=true").json()
    assert following["total"] == 1
    assert following["items"][0]["due_at"] == "2026-03-05T08:00:00+00:00"
    assert following["items"][0]["rating_scale"] == [1, 2, 3, 4]
    assert following["items"][0]["overdue_days"] == 0
    assert client.get("/api/review-cards/due").json()["total"] == 0


def test_rate_card_idempotent_replay_does_not_write_twice(review_env):
    _insert_wrong(USER_A, "docA:1")
    client = _client_for(USER_A)
    _sync(client)
    card = client.get("/api/review-cards/due").json()["items"][0]
    url = f"/api/review-cards/{card['card_id']}/ratings"

    first = client.post(url, json={"rating": 3, "idempotency_key": "replay-key"})
    assert first.status_code == 200
    replay = client.post(url, json={"rating": 3, "idempotency_key": "replay-key"})
    assert replay.status_code == 200
    assert replay.json()["replayed"] is True
    assert replay.json()["replay"] is True
    assert replay.json()["due_at"] == first.json()["due_at"]
    assert replay.json()["card"]["version"] == 2
    assert len(_rating_rows(USER_A)) == 1

    # 再评一档推进版本；旧幂等键带过期 card_version 仍应返回原结果（重放优先于版本校验）。
    second = client.post(url, json={"rating": 4, "idempotency_key": "second-key"})
    assert second.status_code == 200
    assert second.json()["card"]["version"] == 3
    stale_replay = client.post(
        url, json={"rating": 3, "card_version": 1, "idempotency_key": "replay-key"}
    )
    assert stale_replay.status_code == 200
    assert stale_replay.json()["replayed"] is True
    assert stale_replay.json()["due_at"] == first.json()["due_at"]
    assert len(_rating_rows(USER_A)) == 2


def test_rate_card_version_conflict_and_validation(review_env):
    _insert_wrong(USER_A, "docA:1")
    client = _client_for(USER_A)
    _sync(client)
    card = client.get("/api/review-cards/due").json()["items"][0]
    url = f"/api/review-cards/{card['card_id']}/ratings"

    conflict = client.post(
        url, json={"rating": 3, "card_version": 99, "idempotency_key": "conflict-1"}
    )
    assert conflict.status_code == 409
    assert conflict.json()["detail"] == "复习卡已被更新，请刷新后重试"

    for bad_rating in (0, 5, -1, 9):
        invalid = client.post(url, json={"rating": bad_rating, "idempotency_key": "bad-rate"})
        assert invalid.status_code == 422
        assert "1 到 4" in invalid.json()["detail"]
    assert _rating_rows(USER_A) == []

    bad_key = client.post(url, json={"rating": 3, "idempotency_key": "bad key!"})
    assert bad_key.status_code == 422
    assert "幂等键" in bad_key.json()["detail"]
    too_long = client.post(url, json={"rating": 3, "idempotency_key": "k" * 121})
    assert too_long.status_code == 422

    # 缺省幂等键：服务端生成唯一键并写唯一行；两次缺省调用各写一行。
    first = client.post(url, json={"rating": 3})
    assert first.status_code == 200
    generated = first.json()["idempotency_key"]
    assert generated.startswith("srv-")
    second = client.post(url, json={"rating": 4})
    assert second.status_code == 200
    assert second.json()["idempotency_key"] != generated
    assert len(_rating_rows(USER_A)) == 2

    # 不存在的卡 → 404。
    missing = client.post(
        "/api/review-cards/999999/ratings", json={"rating": 3, "idempotency_key": "missing-1"}
    )
    assert missing.status_code == 404
    assert missing.json()["detail"] == "复习卡不存在"


def test_two_accounts_are_isolated(review_env):
    _insert_wrong(USER_A, "docA:1")
    client_a = _client_for(USER_A)
    client_b = _client_for(USER_B)
    _sync(client_a)
    card = client_a.get("/api/review-cards/due").json()["items"][0]

    assert client_b.get(f"/api/review-cards/{card['card_id']}").status_code == 404
    assert client_b.post(
        f"/api/review-cards/{card['card_id']}/ratings",
        json={"rating": 3, "idempotency_key": "cross-user"},
    ).status_code == 404
    assert client_b.get("/api/review-cards/due").json()["total"] == 0
    assert client_b.get("/api/review-cards/due?include_future=true").json()["total"] == 0
    assert client_b.get("/api/review-cards/stats").json()["total"] == 0
    assert client_b.get("/api/review-cards/compare").json()["checked"] == 0
    # B 迁移只处理 B 自己的错题，A 的卡保持可用。
    assert _sync(client_b)["created"] == 0
    assert client_a.get(f"/api/review-cards/{card['card_id']}").status_code == 200


# ---------------------------------------------------------------------------
# 引擎降级与恢复
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("engine", ["external", "ts-fsrs"])
def test_engine_unavailable_degrades_then_recovers(review_env, monkeypatch, engine):
    _insert_wrong(USER_A, "docA:1")
    client = _client_for(USER_A)
    _sync(client)
    card = client.get("/api/review-cards/due").json()["items"][0]
    url = f"/api/review-cards/{card['card_id']}/ratings"
    before = client.get(f"/api/review-cards/{card['card_id']}").json()["card"]

    monkeypatch.setenv("STUDY_REVIEW_ENGINE", engine)
    degraded = client.post(
        url, json={"rating": 3, "idempotency_key": "engine-retry-1"}
    )
    assert degraded.status_code == 200, degraded.text
    body = degraded.json()
    assert body["degraded"] is True
    assert body["retry_allowed"] is True
    assert body["due_at"] == before["due_at"]
    assert body["card"]["version"] == before["version"]
    assert body["card"]["due_at"] == before["due_at"]
    assert body["note"] == "排程引擎暂不可用，已保留最近一次有效到期时间，请稍后重试"
    assert _rating_rows(USER_A) == []  # 降级不写复习日志

    # 恢复后同一幂等键正常应用一次（若降级阶段写过日志，这里会变成重放）。
    monkeypatch.delenv("STUDY_REVIEW_ENGINE")
    recovered = client.post(url, json={"rating": 3, "idempotency_key": "engine-retry-1"})
    assert recovered.status_code == 200, recovered.text
    assert recovered.json()["degraded"] is False
    assert recovered.json()["replayed"] is False
    assert recovered.json()["card"]["version"] == before["version"] + 1
    assert recovered.json()["due_at"] == "2026-03-05T08:00:00+00:00"
    assert len(_rating_rows(USER_A)) == 1

    replay = client.post(url, json={"rating": 3, "idempotency_key": "engine-retry-1"})
    assert replay.json()["replayed"] is True
    assert len(_rating_rows(USER_A)) == 1


# ---------------------------------------------------------------------------
# 到期队列 / 统计 / 双读对账
# ---------------------------------------------------------------------------


def test_due_cards_filter_sort_limit_and_overdue(review_env):
    # 5 张活跃卡 + 1 张归档卡：到期时间涵盖已逾期/今天/未来，两卡同一到期验证稳定次序。
    _insert_wrong(USER_A, "docA:1", next_review_at="2026-02-27T08:00:00+00:00")
    _insert_wrong(USER_A, "docA:2", next_review_at="2026-02-28T08:00:00+00:00")
    _insert_wrong(USER_A, "docA:3", next_review_at="2026-03-01T08:00:00+00:00")
    _insert_wrong(USER_A, "docA:4", next_review_at="2026-03-03T08:00:00+00:00")
    _insert_wrong(USER_A, "docA:5", next_review_at="2026-02-27T08:00:00+00:00")
    _insert_wrong(
        USER_A, "docA:6", next_review_at="2026-02-01T08:00:00+00:00", archived=1
    )
    client = _client_for(USER_A)
    _sync(client)

    due = client.get("/api/review-cards/due").json()
    assert due["total"] == 4
    assert len(due["items"]) == 4
    assert due["now"] == FIXED_CLOCK
    assert isinstance(due["message"], str) and due["message"]
    due_values = [item["due_at"] for item in due["items"]]
    assert due_values == [
        "2026-02-27T08:00:00+00:00",
        "2026-02-27T08:00:00+00:00",
        "2026-02-28T08:00:00+00:00",
        "2026-03-01T08:00:00+00:00",
    ]
    ids = [item["card_id"] for item in due["items"]]
    assert ids[0] < ids[1]  # 同一到期按 id 稳定次序
    assert due["items"][0]["overdue_days"] == 2
    assert due["items"][2]["overdue_days"] == 1
    assert due["items"][3]["overdue_days"] == 0
    assert all(item["rating_scale"] == [1, 2, 3, 4] for item in due["items"])
    assert all(item["state"] == "new" for item in due["items"])

    limited = client.get("/api/review-cards/due?limit=2").json()
    assert len(limited["items"]) == 2
    assert limited["total"] == 4  # total 不受 limit 截断

    future = client.get("/api/review-cards/due?include_future=true").json()
    assert future["total"] == 5
    assert [item["due_at"] for item in future["items"]][-1] == "2026-03-03T08:00:00+00:00"
    assert future["items"][-1]["overdue_days"] == 0
    # 归档卡不进入任何到期视图。
    assert all(item["card_key"] != "q:docA:6" for item in future["items"])


def test_stats_counts_due_states_and_archived(review_env):
    client = _client_for(USER_A)
    empty = client.get("/api/review-cards/stats").json()
    assert empty["total"] == 0
    assert empty["due_now"] == 0 and empty["due_today"] == 0
    assert empty["algorithm"] == sr.FSRS_ALGORITHM
    assert empty["algorithm_version"] == sr.FSRS_ALGORITHM_VERSION

    _insert_wrong(USER_A, "docA:1", next_review_at="2026-02-25T08:00:00+00:00")
    _insert_wrong(USER_A, "docA:2", next_review_at="2026-03-05T08:00:00+00:00")
    _sync(client)
    before = client.get("/api/review-cards/stats").json()
    assert before["total"] == 2
    assert before["new"] == 2
    assert before["due_now"] == 1
    assert before["due_today"] == 1

    card = client.get("/api/review-cards/due").json()["items"][0]
    rated = client.post(
        f"/api/review-cards/{card['card_id']}/ratings",
        json={"rating": 3, "idempotency_key": "stats-rate-1"},
    )
    assert rated.status_code == 200
    after = client.get("/api/review-cards/stats").json()
    assert after["review"] == 1
    assert after["new"] == 1
    assert after["due_now"] == 0
    assert after["due_today"] == 0

    with db.connection() as con:
        con.execute(
            "UPDATE wrong_questions SET archived=1 WHERE user_id=? AND qid=?",
            (USER_A, "docA:2"),
        )
    _sync(client)
    final = client.get("/api/review-cards/stats").json()
    assert final["total"] == 2
    assert final["archived"] == 1
    assert final["new"] == 0
    assert final["review"] == 1


def test_compare_due_classifies_match_mismatch_missing_extra(review_env):
    _insert_wrong(USER_A, "docA:1", next_review_at="2026-02-25T08:00:00+00:00")
    _insert_wrong(USER_A, "docA:2", next_review_at="2026-02-26T08:00:00+00:00")
    client = _client_for(USER_A)
    _sync(client)

    # 旧口径：第二张卡的旧 next_review_at 比新卡 due 早 48 小时 → mismatch。
    with db.connection() as con:
        con.execute(
            "UPDATE review_cards SET due_at=? WHERE user_id=? AND card_key=?",
            ("2026-02-28T08:00:00+00:00", USER_A, "q:docA:2"),
        )
    # 有卡但活跃错题已删 → extra_cards。
    _insert_wrong(USER_A, "docA:4", next_review_at="2026-02-27T08:00:00+00:00")
    _sync(client)
    with db.connection() as con:
        con.execute(
            "DELETE FROM wrong_questions WHERE user_id=? AND qid=?", (USER_A, "docA:4")
        )
    # 活跃错题但还没有卡 → missing_cards（最后插入，不再 sync）。
    _insert_wrong(USER_A, "docA:3", next_review_at="2026-02-27T08:00:00+00:00")

    compared = client.get("/api/review-cards/compare").json()
    assert compared["checked"] == 3  # docA:1/2/3（docA:4 已不在活跃错题里）
    assert compared["matched"] == 1
    assert len(compared["mismatched"]) == 1
    mismatch = compared["mismatched"][0]
    assert mismatch["qid"] == "docA:2"
    assert mismatch["old_due"] == "2026-02-26T08:00:00+00:00"
    assert mismatch["new_due"] == "2026-02-28T08:00:00+00:00"
    assert mismatch["diff_hours"] == 48.0
    assert compared["missing_cards"] == ["docA:3"]
    assert compared["extra_cards"] == ["q:docA:4"]
    assert "只读" in compared["note"] and "容差 1 秒" in compared["note"]

    # 对账是只读的：不改变任何数据，也不写复习日志。
    assert _card_row(sr.card_by_ref(USER_A, "docA:2")["card_id"])["due_at"] == (
        "2026-02-28T08:00:00+00:00"
    )
    assert _rating_rows(USER_A) == []


def test_compare_due_after_migration_is_clean(review_env):
    _insert_wrong(USER_A, "docA:1", next_review_at="2026-02-25T08:00:00+00:00")
    _insert_wrong(USER_A, "docA:2", next_review_at="2026-02-26T08:00:00+00:00")
    client = _client_for(USER_A)
    _sync(client)
    compared = client.get("/api/review-cards/compare").json()
    assert compared["checked"] == 2
    assert compared["matched"] == 2
    assert compared["mismatched"] == []
    assert compared["missing_cards"] == []
    assert compared["extra_cards"] == []


def test_routes_extract_card_by_ref_and_404(review_env):
    _insert_wrong(USER_A, "docA:1")
    client = _client_for(USER_A)
    _sync(client)
    card = client.get("/api/review-cards/due").json()["items"][0]
    assert sr.card_by_ref(USER_A, "docA:1")["card_id"] == card["card_id"]
    assert sr.card_by_ref(USER_A, "bank:docA:1")["card_id"] == card["card_id"]
    assert sr.card_by_ref(USER_A, "unknown:9") is None
    assert client.get("/api/review-cards/999999").status_code == 404
