"""词汇模块验收（方案 §7）：种子装载、词表分页、复习卡、四档评分、幂等与隔离。"""
from __future__ import annotations

from app import anki_export, vocab


def _create_card(client, word="abandon"):
    response = client.post("/api/vocab/cards", json={"word": word})
    assert response.status_code == 200, response.text
    return response.json()["card"]


def test_vocab_meta_and_items(client):
    meta = client.get("/api/vocab/meta").json()
    assert meta["available"] is True
    assert meta["count"] == 8000
    assert meta["seed_sha"]
    assert meta["source"] == "ECDICT"
    assert meta["source_commit"]

    listed = client.get("/api/vocab/items", params={"tag": "gk", "limit": 5}).json()
    assert listed["available"] is True
    assert listed["total"] >= 3000
    assert len(listed["items"]) == 5
    for item in listed["items"]:
        assert "gk" in item["tags"]
        assert item["word"] and item["translation"]
        assert item["phonetic"] is not None

    searched = client.get("/api/vocab/items", params={"q": "abandon", "limit": 10}).json()
    assert searched["total"] >= 1
    assert any(item["word"] == "abandon" for item in searched["items"])

    missing = client.post("/api/vocab/cards", json={"word": "not-in-seed-xyz"})
    assert missing.status_code == 404


def test_vocab_card_due_rating_idempotency_and_conflict(client):
    card = _create_card(client, "abandon")
    assert card["word"] == "abandon"
    assert card["state"] == "new"
    assert card["card_version"] == 1

    due = client.get("/api/vocab/cards/due").json()
    assert due["total"] == 1
    assert due["cards"][0]["card_id"] == card["card_id"]
    assert due["cards"][0]["translation"]

    first = client.post(
        f"/api/vocab/cards/{card['card_id']}/ratings",
        json={"rating": 3, "card_version": 1, "idempotency_key": "vocab-test-key-1"},
    )
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["replayed"] is False
    assert body["card"]["reps"] == 1
    assert body["card"]["card_version"] == 2
    assert body["due_at"]

    replay = client.post(
        f"/api/vocab/cards/{card['card_id']}/ratings",
        json={"rating": 3, "card_version": 1, "idempotency_key": "vocab-test-key-1"},
    )
    assert replay.status_code == 200, replay.text
    assert replay.json()["replayed"] is True
    assert replay.json()["card"]["reps"] == 1

    conflict = client.post(
        f"/api/vocab/cards/{card['card_id']}/ratings",
        json={"rating": 4, "card_version": 1, "idempotency_key": "vocab-test-key-2"},
    )
    assert conflict.status_code == 409, conflict.text

    due_after = client.get("/api/vocab/cards/due").json()
    assert due_after["total"] == 0
    future = client.get("/api/vocab/cards/due", params={"include_future": True}).json()
    assert future["total"] == 1

    stats = client.get("/api/vocab/stats").json()
    assert stats["available"] is True
    assert stats["total"] == 1
    assert stats["review"] == 1


def test_vocab_cards_are_isolated_between_users(client, second_client):
    card = _create_card(client)
    other_read = second_client.get(f"/api/vocab/cards/{card['card_id']}")
    assert other_read.status_code == 404
    other_rate = second_client.post(
        f"/api/vocab/cards/{card['card_id']}/ratings",
        json={"rating": 3, "card_version": 1, "idempotency_key": "other-key"},
    )
    assert other_rate.status_code == 404
    other_due = second_client.get("/api/vocab/cards/due").json()
    assert other_due["total"] == 0


def test_vocab_missing_seed_degrades_without_breaking_other_features(client, tmp_path):
    try:
        vocab.init_vocab(tmp_path / "no-such-seed.json")
        meta = client.get("/api/vocab/meta").json()
        assert meta["available"] is False
        assert meta["note"]
        blocked = client.get("/api/vocab/items")
        assert blocked.status_code == 503
        # 其它功能不受影响。
        assert client.get("/api/insight/home").status_code == 200
    finally:
        vocab.init_vocab()
    assert client.get("/api/vocab/meta").json()["available"] is True


def test_vocab_home_summary_and_anki_deck(client):
    _create_card(client)
    home = client.get("/api/insight/home").json()
    assert home["vocab"]["available"] is True
    assert home["vocab"]["total"] == 1
    assert home["vocab"]["due_today"] == 1
    # 旧字段仍然存在且语义不变。
    assert "review" in home and "personal" in home

    groups = anki_export.vocab_deck([
        {"word": "abandon", "phonetic": "əˈbændən", "translation": "放弃", "tag": "gk cet4"},
    ])
    assert groups and groups[0][0].startswith("知序词汇")
    card = groups[0][1][0]
    assert "abandon" in card["front"]
    assert "əˈbændən" in card["front"]
    assert "放弃" in card["back"]
    assert "gk" in card["tags"]

    exported = client.get("/api/vocab/export")
    assert exported.status_code in (200, 503)
    if exported.status_code == 200:
        assert exported.content[:2] == b"PK"
