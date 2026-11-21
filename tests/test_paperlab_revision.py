"""试卷工坊 P1：错题复现卷（replay）与 AI 巩固卷（paper-revision / consolidate）。

覆盖：options 汇总、replay 去重/快照/空错题 422、consolidate 候选不足明确拒绝、
模型编造 ID/缺理由失败、成功建卷、归属隔离、取消与续跑幂等。全部 mock 模型。
"""
import asyncio
import json

from fastapi.testclient import TestClient
import pytest

from app import ai, db, practice
from app.ai import jobs as ai_jobs
from app.main import app
from app.paperlab import revision

MODEL = "test-model"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def save_provider(client) -> dict:
    response = client.post("/api/providers", json={
        "name": "巩固卷测试连接", "kind": "openai", "base_url": "https://api.openai.com/v1",
        "api_key": "sk-paperlab-revision-unit-test", "models": [MODEL],
    })
    assert response.status_code == 200, response.text
    return response.json()


def user_id(client) -> int:
    return client.get("/api/auth/me").json()["id"]


def patch_dispatch(monkeypatch):
    async def no_dispatch(*_args, **_kwargs):
        return None
    monkeypatch.setattr(ai_jobs, "_dispatch", no_dispatch)


class FakeModel:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def call(self, provider, model, prompt, images=None, json_output=False,
                   token_limit=5000, **kwargs):
        self.calls.append(prompt)
        item = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(item, Exception):
            raise item
        return {"content": json.dumps(item, ensure_ascii=False), "model": model,
                "provider_name": "mock", "usage": {}}

    def install(self, monkeypatch):
        monkeypatch.setattr(ai, "_call_model", self.call)
        return self


def bank_qids(subject: str, count: int) -> list[str]:
    with practice._bank() as con:
        rows = con.execute(
            "SELECT qid FROM questions WHERE subject=? AND type='single_choice' ORDER BY qid LIMIT ?",
            (subject, count * 6),
        ).fetchall()
    valid = []
    for row in rows:
        question = practice.load_question(row[0])
        if question.get("practice_allowed") and question.get("has_answer"):
            valid.append(row[0])
        if len(valid) >= count:
            break
    assert len(valid) >= count, "题库可用单选题不足"
    return valid


def math_kp() -> dict:
    with db.connection() as con:
        row = con.execute(
            "SELECT id, name FROM knowledge_points WHERE subject='数学' AND level=3 ORDER BY id LIMIT 1"
        ).fetchone()
    assert row is not None
    return {"kp_id": int(row[0]), "name": row[1]}


def seed_wrongs(client, *, subject="数学", wrong_count=2, candidate_count=3, kp=None) -> dict:
    uid = user_id(client)
    kp = kp or math_kp()
    qids = bank_qids(subject, wrong_count + candidate_count + 2)
    wrong_qids = qids[:wrong_count]
    candidate_qids = qids[wrong_count:wrong_count + candidate_count]
    now = db.utcnow()
    with db.connection() as con:
        for index, qid in enumerate(wrong_qids):
            con.execute(
                "INSERT INTO attempts(user_id,qid,answer_text,result,grade_source,created_at) "
                "VALUES(?,?,'A','incorrect','reference',?)", (uid, qid, now),
            )
            con.execute(
                "INSERT INTO wrong_questions(user_id,qid,reason,error_tags_json,failures,streak,"
                "next_review_at,added_at,last_result,last_wrong_at,archived) "
                "VALUES(?,?,?,?,1,0,?,?,'incorrect',?,0)",
                (uid, qid, "答题错误", json.dumps(["计算错误"] if index == 0 else ["概念不清"], ensure_ascii=False),
                 "2000-01-01T00:00:00+00:00", now, now),
            )
            con.execute(
                "INSERT OR IGNORE INTO question_kp(qid,kp_id,weight,source,created_at) "
                "VALUES(?,?,1.0,'admin',?)", (qid, kp["kp_id"], now),
            )
        for qid in candidate_qids:
            con.execute(
                "INSERT OR IGNORE INTO question_kp(qid,kp_id,weight,source,created_at) "
                "VALUES(?,?,1.0,'admin',?)", (qid, kp["kp_id"], now),
            )
    return {"uid": uid, "kp": kp, "wrong_qids": wrong_qids, "candidate_qids": candidate_qids}


def insert_revision_job(client, spec: dict, provider_id: int) -> tuple[int, dict, dict]:
    uid = user_id(client)
    request = {"task": "paper-revision", "provider_id": provider_id, "model": MODEL,
               "params": {"spec": spec}}
    now = db.utcnow()
    with db.connection() as con:
        job_id = con.execute(
            "INSERT INTO ai_jobs(user_id,task,status,stage,request_json,partial_content,"
            "cancelled,created_at,updated_at,dispatch_token) VALUES (?,?,?,?,?,'',0,?,?,?)",
            (uid, "paper-revision", "queued", "queued",
             json.dumps(request, ensure_ascii=False), now, now, "revision-token"),
        ).lastrowid
    provider = ai._selected_provider(uid, provider_id, MODEL)
    return int(job_id), request, provider


def paper_count(client) -> int:
    with db.connection() as con:
        return int(con.execute(
            "SELECT COUNT(*) FROM pl_papers WHERE user_id=?", (user_id(client),)
        ).fetchone()[0])


# ---------------------------------------------------------------------------
# 1. options
# ---------------------------------------------------------------------------

def test_revision_options_lists_wrongs_and_kps(client, second_client):
    seed = seed_wrongs(client, wrong_count=2, candidate_count=0)
    response = client.get("/api/paperlab/revision/options?subject=数学")
    assert response.status_code == 200
    body = response.json()
    assert body["wrong_count"] == 2
    assert body["subjects"] == ["数学"]
    kp_ids = {item["kp_id"] for item in body["kp_options"]}
    assert seed["kp"]["kp_id"] in kp_ids
    assert body["kp_options"][0]["wrong_count"] == 2
    modes = {item["value"]: item for item in body["modes"]}
    assert set(modes) == {"replay", "consolidate"}
    assert modes["replay"]["ai_required"] is False
    assert modes["consolidate"]["ai_required"] is True
    assert body["limits"]["count_max"] == 20
    # 用户隔离
    other = second_client.get("/api/paperlab/revision/options").json()
    assert other["wrong_count"] == 0
    assert other["kp_options"] == []


# ---------------------------------------------------------------------------
# 2. replay：直接建卷
# ---------------------------------------------------------------------------

def test_replay_creates_dedup_snapshot_paper(client, second_client):
    seed = seed_wrongs(client, wrong_count=2, candidate_count=0)
    response = client.post("/api/paperlab/revision", json={
        "mode": "replay", "subject": "数学", "count": 5,
    })
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["mode"] == "replay"
    assert body["paper"]["kind"] == "replay"
    assert body["paper"]["status"] == "ready"
    assert body["item_count"] == 2, "错题不足时用现有题数，不编造"
    refs = [item["question_ref"] for item in body["items"]]
    assert refs == [f"bank:{qid}" for qid in seed["wrong_qids"]]
    assert len(refs) == len(set(refs)), "错题复现卷必须按 qid 去重"
    detail = client.get(f"/api/paperlab/papers/{body['paper']['id']}").json()
    snapshot = detail["items"][0]["snapshot"]
    assert snapshot["source"] == "bank"
    assert snapshot["content"]["answers"]
    assert "错题复现" in snapshot["selection_reason"]
    # 复现卷默认含答案（本人可见），学生版导出不含答案
    assert client.get(f"/api/paperlab/papers/{body['paper']['id']}/export?format=html&answers=0").status_code == 200
    # 他人不可见
    assert second_client.get(f"/api/paperlab/papers/{body['paper']['id']}").status_code == 404


def test_replay_empty_wrongbook_and_bad_kp(client, second_client):
    empty = second_client.post("/api/paperlab/revision", json={"mode": "replay", "count": 3})
    assert empty.status_code == 422
    assert "错题" in empty.json()["detail"]
    seed_wrongs(client, wrong_count=1, candidate_count=0)
    missing_kp = client.post("/api/paperlab/revision", json={
        "mode": "replay", "kp_ids": [999999], "count": 3,
    })
    assert missing_kp.status_code == 404
    bad_mode = client.post("/api/paperlab/revision", json={"mode": "bogus"})
    assert bad_mode.status_code == 422
    no_provider = client.post("/api/paperlab/revision", json={
        "mode": "consolidate", "subject": "数学", "count": 1,
    })
    assert no_provider.status_code == 422
    assert "模型" in no_provider.json()["detail"]


# ---------------------------------------------------------------------------
# 3. consolidate：候选不足明确拒绝
# ---------------------------------------------------------------------------

def test_consolidate_rejects_insufficient_candidates(client, monkeypatch):
    patch_dispatch(monkeypatch)
    provider = save_provider(client)
    seed_wrongs(client, wrong_count=1, candidate_count=0)
    response = client.post("/api/paperlab/revision", json={
        "mode": "consolidate", "subject": "数学", "count": 1,
        "provider_id": provider["id"], "model": MODEL,
    })
    assert response.status_code == 422
    assert "不会缩水" in response.json()["detail"]

    second_user = TestClient(app)
    # 用另一个账号验证「候选有但少于题数」：先喂 1 道候选
    second_user.post("/api/auth/register", json={"username": "巩固卷学生", "password": "test-password-123"})
    provider2 = save_provider(second_user)
    seed_wrongs(second_user, wrong_count=1, candidate_count=1)
    shortage = second_user.post("/api/paperlab/revision", json={
        "mode": "consolidate", "subject": "数学", "count": 3,
        "provider_id": provider2["id"], "model": MODEL,
    })
    assert shortage.status_code == 422
    assert "少于需要" in shortage.json()["detail"]
    second_user.close()


def test_consolidate_without_kp_links_is_rejected(client, monkeypatch):
    patch_dispatch(monkeypatch)
    provider = save_provider(client)
    uid = user_id(client)
    qid = bank_qids("数学", 1)[0]
    now = db.utcnow()
    with db.connection() as con:
        con.execute(
            "INSERT INTO attempts(user_id,qid,answer_text,result,grade_source,created_at) "
            "VALUES(?,?,'A','incorrect','reference',?)", (uid, qid, now),
        )
        con.execute(
            "INSERT INTO wrong_questions(user_id,qid,reason,error_tags_json,failures,streak,"
            "next_review_at,added_at,last_result,last_wrong_at,archived) "
            "VALUES(?,?,'答题错误','[]',1,0,'2000-01-01T00:00:00+00:00',?,'incorrect',?,0)",
            (uid, qid, now, now),
        )
    response = client.post("/api/paperlab/revision", json={
        "mode": "consolidate", "subject": "数学", "count": 1,
        "provider_id": provider["id"], "model": MODEL,
    })
    assert response.status_code == 422
    assert "知识点" in response.json()["detail"]


# ---------------------------------------------------------------------------
# 4. consolidate 任务执行器
# ---------------------------------------------------------------------------

def test_consolidate_job_creates_paper(client, monkeypatch):
    provider = save_provider(client)
    seed = seed_wrongs(client, wrong_count=2, candidate_count=3)
    spec = {"mode": "consolidate", "subject": "数学", "kp_ids": None, "count": 2,
            "wrong_qids": seed["wrong_qids"]}
    target_refs = [f"bank:{qid}" for qid in seed["candidate_qids"][:2]]
    fake = FakeModel([{
        "selections": [
            {"ref": target_refs[0], "reason": "针对计算错误"},
            {"ref": target_refs[1], "reason": "针对概念不清"},
        ],
        "summary": "针对两道错题暴露的薄弱点巩固",
    }]).install(monkeypatch)
    job_id, request, provider_row = insert_revision_job(client, spec, provider["id"])
    result = asyncio.run(revision.run_revision_job(job_id, user_id(client), ai_jobs.JobInput(**request), provider_row))

    assert result["paper_id"] >= 1
    assert result["item_count"] == 2
    assert result["summary"]
    assert {item["ref"] for item in result["reasons"]} == set(target_refs)
    paper = client.get(f"/api/paperlab/papers/{result['paper_id']}").json()
    assert paper["paper"]["kind"] == "consolidate"
    assert paper["paper"]["status"] == "ready"
    assert len(paper["items"]) == 2
    assert paper["items"][0]["snapshot"]["content"]["answers"]
    assert paper["items"][0]["selection_reason"]
    # 候选不包含本人已作答/错题本里的题
    prompt = fake.calls[0]
    for qid in seed["wrong_qids"]:
        assert f"bank:{qid}" not in prompt
    assert len(fake.calls) == 1


def test_consolidate_rejects_fabricated_or_missing_reason(client, monkeypatch):
    provider = save_provider(client)
    seed = seed_wrongs(client, wrong_count=1, candidate_count=3)
    spec = {"mode": "consolidate", "subject": "数学", "count": 1,
            "wrong_qids": seed["wrong_qids"]}
    fake = FakeModel([
        {"selections": [{"ref": "bank:no-such-question", "reason": "编造"}]},
        {"selections": [{"ref": "bank:also-no", "reason": "还是编造"}]},
    ]).install(monkeypatch)
    job_id, request, provider_row = insert_revision_job(client, spec, provider["id"])
    with pytest.raises(ValueError) as exc:
        asyncio.run(revision.run_revision_job(job_id, user_id(client), ai_jobs.JobInput(**request), provider_row))
    assert "候选" in str(exc.value)
    assert len(fake.calls) == 2
    assert paper_count(client) == 0

    # 缺理由：同样失败
    fake.responses = [
        {"selections": [{"ref": f"bank:{seed['candidate_qids'][0]}", "reason": ""}]},
        {"selections": [{"ref": f"bank:{seed['candidate_qids'][1]}", "reason": ""}]},
    ]
    job_id, request, provider_row = insert_revision_job(client, spec, provider["id"])
    with pytest.raises(ValueError) as exc2:
        asyncio.run(revision.run_revision_job(job_id, user_id(client), ai_jobs.JobInput(**request), provider_row))
    assert "reason" in str(exc2.value)
    assert paper_count(client) == 0


def test_consolidate_job_idempotent_and_cancel(client, monkeypatch):
    provider = save_provider(client)
    seed = seed_wrongs(client, wrong_count=1, candidate_count=2)
    spec = {"mode": "consolidate", "subject": "数学", "count": 1,
            "wrong_qids": seed["wrong_qids"]}
    fake = FakeModel([{
        "selections": [{"ref": f"bank:{seed['candidate_qids'][0]}", "reason": "巩固薄弱点"}],
        "summary": "1 题巩固卷",
    }]).install(monkeypatch)
    job_id, request, provider_row = insert_revision_job(client, spec, provider["id"])
    first = asyncio.run(revision.run_revision_job(job_id, user_id(client), ai_jobs.JobInput(**request), provider_row))
    second = asyncio.run(revision.run_revision_job(job_id, user_id(client), ai_jobs.JobInput(**request), provider_row))
    assert first["paper_id"] == second["paper_id"]
    assert len(fake.calls) == 1, "已建卷的续跑不应重复调用模型"
    assert paper_count(client) == 1

    # 取消：直接标记 cancelled 后执行器抛 CancelledError
    job_id, request, provider_row = insert_revision_job(client, spec, provider["id"])
    with db.connection() as con:
        con.execute("UPDATE ai_jobs SET cancelled=1, status='cancel_requested' WHERE id=?", (job_id,))
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(revision.run_revision_job(job_id, user_id(client), ai_jobs.JobInput(**request), provider_row))
    assert paper_count(client) == 1, "取消不应产生额外试卷"


def test_revision_task_admission_requires_spec(client, monkeypatch):
    patch_dispatch(monkeypatch)
    provider = save_provider(client)
    response = client.post("/api/ai/jobs", json={
        "task": "paper-revision", "provider_id": provider["id"], "model": MODEL,
    })
    assert response.status_code == 422
