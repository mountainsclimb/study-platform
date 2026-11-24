"""知识点本地向量打标（app/kp_tagging.py）。"""
import asyncio
from types import SimpleNamespace

from app import db, kp_tagging


def test_assign_tags_threshold_and_confidence():
    result = kp_tagging.assign_tags(
        [[1.0, 0.0], [0.0, 1.0], [1.0, 1.0]],
        [[1.0, 0.0], [0.0, 1.0]],
        threshold=0.5, high_confidence=0.9,
    )
    assert result[0] == [{"kp_index": 0, "score": 1.0, "source": "local_ai"}]
    assert result[1] == [{"kp_index": 1, "score": 1.0, "source": "local_ai"}]
    assert {item["kp_index"] for item in result[2]} == {0, 1}
    assert all(item["source"] == "local_ai_pending_review" for item in result[2])


def test_assign_tags_ignores_zero_vectors():
    result = kp_tagging.assign_tags([[0.0, 0.0]], [[1.0, 0.0]])
    assert result == [[]]


def _seed_kps(user_id: int):
    with db.connection() as con:
        for kp_id, name in ((101, "函数定义域"), (102, "概率初步")):
            con.execute(
                "INSERT OR IGNORE INTO knowledge_points"
                "(id,subject,parent_id,name,level,aliases_json,description,sort,source) "
                "VALUES (?,?,NULL,?,3,'[]','',1,'official')",
                (kp_id, "数学", name),
            )


def _patch(monkeypatch, questions, kps, vectors):
    monkeypatch.setattr(
        kp_tagging.embeddings, "embedding_config",
        lambda user_id=None: SimpleNamespace(storage_model="test-model@local"),
    )

    async def embed(texts, user_id=None):
        return vectors

    monkeypatch.setattr(kp_tagging.embeddings, "embed_texts", embed)
    monkeypatch.setattr(kp_tagging, "_load_kps", lambda con, subject: kps)
    monkeypatch.setattr(kp_tagging, "_load_questions", lambda con, subject, limit: questions)


def test_run_local_tagging_writes_local_sources(client, monkeypatch):
    user_id = client.get("/api/auth/me").json()["id"]
    _seed_kps(user_id)
    questions = [{"qid": "bank:q1", "subject": "数学", "stem_plain": "求函数定义域"}]
    kps = [
        {"kp_id": 101, "subject": "数学", "name": "函数定义域", "text": "函数定义域"},
        {"kp_id": 102, "subject": "数学", "name": "概率初步", "text": "概率初步"},
    ]
    _patch(monkeypatch, questions, kps, [[1.0, 0.0], [0.0, 1.0], [1.0, 0.0]])

    result = asyncio.run(kp_tagging.run_local_tagging(user_id=user_id, limit=10))
    assert result["available"] is True
    assert result["written"] == 1
    assert result["tagged"] == 1

    with db.connection() as con:
        rows = con.execute(
            "SELECT qid,kp_id,source,confidence FROM question_kp WHERE qid='bank:q1'"
        ).fetchall()
    assert len(rows) == 1
    assert rows[0]["kp_id"] == 101
    assert rows[0]["source"] == "local_ai"


def test_low_confidence_enters_review_queue(client, monkeypatch):
    user_id = client.get("/api/auth/me").json()["id"]
    _seed_kps(user_id)
    questions = [{"qid": "bank:q2", "subject": "数学", "stem_plain": "概率题"}]
    kps = [{"kp_id": 101, "subject": "数学", "name": "函数定义域", "text": "函数定义域"}]
    # 余弦约 0.707：高于阈值 0.45、低于高置信 0.9。
    _patch(monkeypatch, questions, kps, [[1.0, 0.0], [1.0, 1.0]])
    result = asyncio.run(kp_tagging.run_local_tagging(
        user_id=user_id, limit=10, high_confidence=0.9,
    ))
    assert result["queued"] == 1
    with db.connection() as con:
        row = con.execute(
            "SELECT source FROM question_kp WHERE qid='bank:q2'"
        ).fetchone()
    assert row["source"] == "local_ai_pending_review"


def test_manual_association_is_not_overwritten(client, monkeypatch):
    user_id = client.get("/api/auth/me").json()["id"]
    _seed_kps(user_id)
    with db.connection() as con:
        con.execute(
            "INSERT INTO question_kp(qid,kp_id,weight,source,confidence,reviewed_by,created_at) "
            "VALUES ('bank:q3',101,1.0,'admin',1.0,?,?)",
            (user_id, db.utcnow()),
        )
    questions = [{"qid": "bank:q3", "subject": "数学", "stem_plain": "函数题"}]
    kps = [{"kp_id": 101, "subject": "数学", "name": "函数定义域", "text": "函数定义域"}]
    _patch(monkeypatch, questions, kps, [[1.0, 0.0], [1.0, 0.0]])
    asyncio.run(kp_tagging.run_local_tagging(user_id=user_id, limit=10))
    with db.connection() as con:
        row = con.execute(
            "SELECT source,reviewed_by FROM question_kp WHERE qid='bank:q3' AND kp_id=101"
        ).fetchone()
    assert row["source"] == "admin"
    assert row["reviewed_by"] == user_id


def test_no_embedding_config_writes_nothing(client, monkeypatch):
    user_id = client.get("/api/auth/me").json()["id"]
    monkeypatch.setattr(kp_tagging.embeddings, "embedding_config", lambda user_id=None: None)
    result = asyncio.run(kp_tagging.run_local_tagging(user_id=user_id))
    assert result["available"] is False
    assert result["tagged"] == 0
    assert "未配置" in result["notice"]
