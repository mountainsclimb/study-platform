"""Tests for P3 deliverables: 100-question golden eval & A/B, hybrid vector search, recovery codes, reg limit & hot backup/restore."""
import asyncio
import json
from pathlib import Path
import zipfile

import pytest

from app import ai, auth, db, eval as eval_mod, ops, search_vector


def test_golden_dataset_covers_all_nine_subjects_with_100_reliable_questions():
    golden = eval_mod.load_golden_dataset()
    assert len(golden) == 100
    qids = {it["qid"] for it in golden}
    assert len(qids) == 100
    subjects = {it["subject"] for it in golden}
    assert subjects == {"数学", "物理", "化学", "生物", "语文", "英语", "历史", "地理", "政治"}
    assert all(it["reference_answer"] for it in golden)


def test_eval_runner_cross_verify_metrics_and_ab_comparison(client, monkeypatch):
    items = [
        {"qid": "q1", "subject": "数学", "type": "single_choice", "stem_plain": "1+1=?", "options": [], "reference_answer": "A", "answer_kind": "choice"},
        {"qid": "q2", "subject": "数学", "type": "single_choice", "stem_plain": "2+2=?", "options": [], "reference_answer": "B", "answer_kind": "choice"},
        {"qid": "q3", "subject": "物理", "type": "single_choice", "stem_plain": "F=ma?", "options": [], "reference_answer": "C", "answer_kind": "choice"},
        {"qid": "q4", "subject": "物理", "type": "single_choice", "stem_plain": "v=at?", "options": [], "reference_answer": "D", "answer_kind": "choice"},
    ]
    # q1: A=A, B=A (agreed & right)
    # q2: A=A, B=A (agreed & WRONG -> tests agreed_but_wrong_rate!)
    # q3: A=C, B=A (disagreed, A right)
    # q4: A=A, B=D (disagreed, A wrong)
    script_v1 = {
        ("mA", "1+1=?"): "A", ("mB", "1+1=?"): "A",
        ("mA", "2+2=?"): "A", ("mB", "2+2=?"): "A",
        ("mA", "F=ma?"): "C", ("mB", "F=ma?"): "A",
        ("mA", "v=at?"): "A", ("mB", "v=at?"): "D",
    }

    async def fake_eval_call(prov, model, prompt, *a, **kw):
        ans = "A"
        for (m, stem), val in script_v1.items():
            if m == model and stem in prompt:
                ans = val
                break
        payload = {"final_answer": {"kind": "choice", "value": ans}, "steps": [{"no": 1, "title": "s", "content": "c"}]}
        return {"content": json.dumps(payload), "model": model, "provider_name": "mock", "usage": {}}

    monkeypatch.setattr(ai, "_call_model", fake_eval_call)
    report_a = asyncio.run(eval_mod.run_evaluation(
        items, {"id": 1}, "mA", verifier={"id": 2}, verify_model="mB", name="prompt-v1", concurrency=1
    ))
    m = report_a["metrics"]
    assert m["total"] == 4
    assert m["single_accuracy"] == 50.0
    assert m["agreement_rate"] == 50.0
    assert m["cross_verified_accuracy"] == 50.0
    assert m["agreed_but_wrong_count"] == 1
    assert m["agreed_but_wrong_rate"] == 50.0

    # Run B where q2 is fixed -> improved_count == 1
    script_v1[("mA", "2+2=?")] = "B"
    script_v1[("mB", "2+2=?")] = "B"
    report_b = asyncio.run(eval_mod.run_evaluation(
        items, {"id": 1}, "mA", verifier={"id": 2}, verify_model="mB", name="prompt-v2", concurrency=1
    ))
    diff = eval_mod.compare_reports(report_a, report_b)
    assert diff["single_accuracy_delta"] == 25.0
    assert diff["agreed_but_wrong_delta"] == -50.0
    assert diff["improved_qids"] == ["q2"]

    # Admin API lists saved eval runs
    runs = client.get("/api/admin/eval-runs").json()["items"]
    assert len(runs) >= 2


def test_hybrid_vector_retriever_sparse_and_dense(isolated_db):
    db.init_db()
    chunks = [
        {"document_id": 1, "chunk_id": "p1-c1", "heading_path": "牛顿运动定律", "text": "牛顿第二定律 F=ma 描述力与加速度的关系"},
        {"document_id": 1, "chunk_id": "p1-c2", "heading_path": "电磁感应", "text": "法拉第电磁感应定律与楞次定律判断感应电流方向"},
        {"document_id": 1, "chunk_id": "p1-c3", "heading_path": "化学平衡", "text": "勒夏特列原理判断可逆反应平衡移动"},
    ]
    ranked = search_vector.rank_chunks_hybrid(chunks, "如何用楞次定律判断感应电流方向", top_k=2)
    assert ranked[0]["chunk_id"] == "p1-c2"

    # Store dense embeddings and verify dense cosine boosts matching chunk
    with db.connection() as con:
        con.execute("INSERT INTO chunk_embeddings VALUES (1,'p1-c1','emb-test','[1.0, 0.0]',?)", (db.utcnow(),))
        con.execute("INSERT INTO chunk_embeddings VALUES (1,'p1-c3','emb-test','[0.0, 1.0]',?)", (db.utcnow(),))
    ranked_dense = search_vector.rank_chunks_hybrid(
        chunks, "完全无关词", top_k=2, query_vector=[0.0, 1.0], embedding_model="emb-test"
    )
    assert ranked_dense[0]["chunk_id"] == "p1-c3"


def test_change_password_recovery_codes_reset_and_registration_rate_limit(client):
    # Generate 8 recovery codes（需要当前密码二次校验）
    wrong_password = client.post("/api/auth/recovery-codes", json={"current_password": "wrong-password-999"})
    assert wrong_password.status_code == 401
    gen = client.post("/api/auth/recovery-codes", json={"current_password": "test-password-123"})
    assert gen.status_code == 200
    codes = gen.json()["codes"]
    assert len(codes) == 8

    # Change password
    assert client.post("/api/auth/change-password", json={
        "current_password": "wrong-password", "new_password": "new-password-999"
    }).status_code == 401
    assert client.post("/api/auth/change-password", json={
        "current_password": "test-password-123", "new_password": "new-password-999"
    }).status_code == 200

    # Logout and reset password with one-time recovery code
    client.post("/api/auth/logout")
    reset = client.post("/api/auth/reset-password", json={
        "username": "测试管理员", "recovery_code": codes[0], "new_password": "recovered-pass-888"
    })
    assert reset.status_code == 200
    # Same recovery code cannot be replayed
    replay = client.post("/api/auth/reset-password", json={
        "username": "测试管理员", "recovery_code": codes[0], "new_password": "another-pass-777"
    })
    assert replay.status_code == 401

    # Login succeeds with recovered password
    assert client.post("/api/auth/login", json={
        "username": "测试管理员", "password": "recovered-pass-888"
    }).status_code == 200

    # Registration IP rate limit triggers after 20 registrations in window（跨进程记录在数据库）
    with db.connection() as con:
        con.execute("DELETE FROM auth_rate_events")
    auth._reg_attempts.clear()
    for i in range(20):
        r = client.post("/api/auth/register", json={"username": f"限流测试用户{i}", "password": "password-12345"})
        assert r.status_code == 200, r.text
    blocked = client.post("/api/auth/register", json={"username": "限流测试用户21", "password": "password-12345"})
    assert blocked.status_code == 429
    with db.connection() as con:
        con.execute("DELETE FROM auth_rate_events")


def test_online_hot_backup_verify_restore_and_tamper_detection(client, tmp_path):
    # Admin endpoint creates & verifies backup
    resp = client.post("/api/admin/backup")
    assert resp.status_code == 200, resp.text
    info = resp.json()
    archive = Path(info["path"])
    assert archive.is_file()
    assert info["verified"]["integrity_check"] == "ok"
    assert info["verified"]["users"] >= 1

    # Restore drill into fresh target directory
    restore_dir = tmp_path / "restored"
    drill = ops.restore_backup(archive, restore_dir)
    assert drill["ok"] is True
    assert (restore_dir / "app.sqlite3").is_file()
    # 默认备份包不含 api-key.key（应用级加密密钥），需完整恢复时显式 --include-key。
    assert (restore_dir / "api-key.key").is_file() is False
    assert info["key_included"] is False

    # Tampering with archived file causes verify_backup to reject
    tampered = tmp_path / "tampered.zip"
    with zipfile.ZipFile(archive, "r") as zin, zipfile.ZipFile(tampered, "w") as zout:
        for name in zin.namelist():
            data = zin.read(name)
            if name == "app.sqlite3":
                data = b"tampered-bytes"
            zout.writestr(name, data)
    with pytest.raises(ValueError, match="校验和"):
        ops.verify_backup(tampered)

    archive.unlink(missing_ok=True)
