"""Tests for v2 P0+P1+P2 optimizations: FTS5, review interleaving, KP tree, Cross-Verify 2.0, Notes Agent, SSE & uploads."""
import asyncio
from io import BytesIO
import json
import time
from urllib.parse import quote

import httpx
from PIL import Image, ImageDraw
import pytest

from app import ai, db, practice


def save_provider(client, **changes):
    data = {"name": "测试连接", "kind": "openai", "base_url": "https://api.openai.com/v1",
            "api_key": "sk-unit-test-secret-value", "models": ["test-model", "verify-model"]}
    data.update(changes)
    response = client.post("/api/providers", json=data)
    assert response.status_code == 200, response.text
    return response.json()


def bank_question(objective=True):
    with practice._bank() as con:
        ids = [r[0] for r in con.execute(
            "SELECT qid FROM questions WHERE objective=? ORDER BY year DESC LIMIT 250",
            (1 if objective else 0,))]
    for qid in ids:
        question = practice.load_question(qid)
        blocking = [flag for flag in question["quality_flags"] if "参考答案待核验" not in flag]
        if question["practice_allowed"] and not blocking and question["has_answer"]:
            if not objective or question["type"] == "single_choice":
                return question
    raise AssertionError("No suitable source question")


def test_fsrs_lite_requires_graded_history_and_preserves_wrong_answer_rules(client, monkeypatch):
    from app.review_schedule import fsrs_enabled

    q = bank_question()
    qid = q["qid"]
    correct = q["answer_text"]
    incorrect = next(opt["label"] for opt in q["options"] if opt["label"] != correct)
    monkeypatch.setenv("STUDY_REVIEW_SCHEDULER", "fsrs-lite")
    assert client.post("/api/attempts", data={"qid": qid, "answer": incorrect}).json()["result"] == "incorrect"
    with db.connection() as con:
        stored = con.execute("SELECT * FROM wrong_questions WHERE qid=?", (qid,)).fetchone()
        assert stored["stability"] is None
        user_id = stored["user_id"]
        con.execute(
            "WITH RECURSIVE sample(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM sample WHERE n<2000) "
            "INSERT INTO attempts(user_id,qid,result,grade_source,created_at) "
            "SELECT ?,?,CASE WHEN n<=1998 THEN 'correct' ELSE 'pending' END,"
            "CASE WHEN n<=1998 THEN 'reference' ELSE 'pending' END,? FROM sample",
            (user_id, qid, db.utcnow()),
        )
        assert not fsrs_enabled(con)

    assert client.post("/api/attempts", data={"qid": qid, "answer": correct}).json()["result"] == "correct"
    with db.connection() as con:
        assert fsrs_enabled(con)
        recalled = con.execute("SELECT * FROM wrong_questions WHERE user_id=? AND qid=?", (user_id, qid)).fetchone()
        assert recalled["stability"] == pytest.approx(3.7145)
        assert recalled["difficulty"] == pytest.approx(5.1618)
        assert recalled["interval_days"] == 4
    assert client.post("/api/mastery", json={"qid": qid, "level": "熟练掌握"}).status_code == 200
    assert client.get("/api/wrongbook").json()["items"][0]["next_review_at"] == recalled["next_review_at"]

    assert client.post("/api/attempts", data={"qid": qid, "answer": incorrect}).json()["result"] == "incorrect"
    with db.connection() as con:
        missed = con.execute("SELECT * FROM wrong_questions WHERE user_id=? AND qid=?", (user_id, qid)).fetchone()
        assert missed["stability"] < recalled["stability"]
        assert missed["difficulty"] > recalled["difficulty"]
        assert missed["interval_days"] == 1
        assert missed["streak"] == 0
        assert con.execute("SELECT level FROM mastery WHERE user_id=? AND qid=?", (user_id, qid)).fetchone()[0] == "未掌握"
        monkeypatch.delenv("STUDY_REVIEW_SCHEDULER")
        assert not fsrs_enabled(con)


def test_notes_map_extract_deduplicates_and_resumes_before_outline(client, monkeypatch):
    doc_ids = []
    for title, body in [("一.md", "第一份原文"), ("二.md", "第二份原文")]:
        uploaded = client.post("/api/documents", files={"file": (title, body.encode(), "text/markdown")})
        assert uploaded.status_code == 200, uploaded.text
        assert uploaded.json()["chunks"] == 1
        doc_ids.append(uploaded.json()["id"])
    tags = [f"[D{doc_id}:p1-c1]" for doc_id in doc_ids]
    provider = save_provider(client)
    map_run = []
    outline_run = []

    async def fake_call(prov, model, prompt, *args, **kwargs):
        if kwargs.get("json_output"):
            index = 0 if "第一份原文" in prompt else 1
            map_run.append(index)
            if index == 1 and map_run.count(1) < 3:
                content = '{"concepts":["missing fields"]}'
            else:
                content = json.dumps({
                    "concepts": ["定义域是自变量取值范围"],
                    "formulas": [],
                    "conditions": ["先检查分母不等于零"] if index else [],
                    "examples": [],
                }, ensure_ascii=False)
        elif "先根据资料设计" in prompt:
            outline_run.append(prompt)
            content = "概念和条件"
        else:
            content = "定义域是自变量取值范围 " + " ".join(tags)
        return {"content": content, "model": model, "provider_name": "mock", "usage": {}}

    monkeypatch.setattr(ai, "_call_model", fake_call)
    created = client.post("/api/ai/jobs", json={
        "task": "notes", "document_ids": doc_ids, "provider_id": provider["id"],
        "model": "test-model", "map_extract": True, "confirm_outline": True,
    })
    assert created.status_code == 200, created.text
    job_id = created.json()["id"]

    def wait_for(status):
        path = f"/api/ai/jobs/{job_id}"
        for _ in range(75):
            job = client.get(path).json()
            if job["status"] == status:
                return job
            time.sleep(0.03)
        pytest.fail(f"Expected {status}, got {job}")

    failed = wait_for("failed")
    assert failed["has_checkpoint"]
    assert not outline_run
    assert client.post(f"/api/ai/jobs/{job_id}/resume").status_code == 200
    wait_for("awaiting_outline")
    assert map_run == [0, 1, 1, 1]
    assert len(outline_run) == 1
    assert outline_run[0].count("定义域是自变量取值范围") == 1
    assert f"定义域是自变量取值范围 {' '.join(tags)}" in outline_run[0]
    assert "第一份原文" not in outline_run[0]

    confirmed = client.post(f"/api/ai/jobs/{job_id}/confirm-outline", json={"outline": "概念和条件"})
    assert confirmed.status_code == 200, confirmed.text
    done = wait_for("completed")
    assert map_run == [0, 1, 1, 1]
    assert len(outline_run) == 1
    assert done["result"]["coverage"]["map_extracted_chunks"] == 2
    assert done["result"]["coverage"]["cited_chunks"] == 2


def _drawn_image(size=(240, 240)):
    img = Image.new("RGB", size, "white")
    draw = ImageDraw.Draw(img)
    draw.line((20, 20, 200, 180), fill="black", width=5)
    draw.rectangle((40, 120, 180, 210), outline="blue", width=4)
    out = BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()


def _blank_image(size=(240, 240)):
    out = BytesIO()
    Image.new("RGB", size, "white").save(out, format="PNG")
    return out.getvalue()


# ---------------------------------------------------------------------------
# 1. FTS5 search, extended filters & blank image gate
# ---------------------------------------------------------------------------

def test_fts5_search_short_and_long_terms_and_extended_filters(client):
    q = bank_question()
    # >= 3 char Chinese term uses FTS5 trigram index
    long_term = q["stem_plain"][:6].strip()
    if len(long_term) >= 3:
        res = client.get("/api/questions", params={"q": long_term, "subject": q["subject"]})
        assert res.status_code == 200
        assert res.json()["total"] >= 1
    # < 3 char Chinese term falls back cleanly to LIKE
    res2 = client.get("/api/questions", params={"q": "函数", "subject": "数学", "year_from": 2020, "year_to": 2026})
    assert res2.status_code == 200
    assert res2.json()["total"] >= 1
    # Status filter: unattempted
    res3 = client.get("/api/questions", params={"subject": q["subject"], "status": "unattempted", "page_size": 5})
    assert res3.status_code == 200
    assert len(res3.json()["items"]) == 5


def test_blank_photo_rejected_while_drawn_photo_accepted(client):
    q = bank_question(False)
    blank_res = client.post("/api/attempts", data={"qid": q["qid"]}, files={"file": ("blank.png", _blank_image(), "image/png")})
    assert blank_res.status_code == 422
    assert "空白" in blank_res.json()["detail"]

    drawn_res = client.post("/api/attempts", data={"qid": q["qid"]}, files={"file": ("work.png", _drawn_image(), "image/png")})
    assert drawn_res.status_code == 200
    assert drawn_res.json()["result"] == "pending"
    hist = client.get("/api/attempts/history", params={"qid": q["qid"]}).json()
    assert len(hist["items"]) == 1


# ---------------------------------------------------------------------------
# 2. Wrongbook interleaving, mastery-linked intervals, 3-streak archive & tags
# ---------------------------------------------------------------------------

def test_interleaved_reviews_do_not_pollute_total_and_respect_filters(client):
    # Pick a question from page 3 so it isn't already on page 1
    page3 = client.get("/api/questions", params={"subject": "数学", "page": 3, "page_size": 12}).json()
    target_qid = page3["items"][0]["qid"]
    base_total = client.get("/api/questions", params={"subject": "数学", "page": 1, "page_size": 12}).json()["total"]

    # Add to wrongbook (immediately due)
    assert client.post("/api/wrongbook", json={"qid": target_qid, "error_tags": ["计算错误"]}).status_code == 200

    # With include_reviews=true, target appears as review slot without altering total
    res = client.get("/api/questions", params={"subject": "数学", "page": 1, "page_size": 12, "include_reviews": "true"}).json()
    assert res["total"] == base_total
    assert len(res["review_slots"]) == 1
    assert res["review_slots"][0]["qid"] == target_qid
    review_card = res["items"][res["review_slots"][0]["index"]]
    assert review_card["slot"] == "review"
    assert "计算错误" in review_card["error_tags"]

    # Seen reviews are skipped in same session
    res_skip = client.get("/api/questions", params={
        "subject": "数学", "page": 1, "page_size": 12, "include_reviews": "true", "seen_reviews": target_qid
    }).json()
    assert res_skip["review_slots"] == []

    # Different subject never receives math wrong question
    res_other = client.get("/api/questions", params={"subject": "物理", "page": 1, "page_size": 12, "include_reviews": "true"}).json()
    assert all(s["qid"] != target_qid for s in res_other["review_slots"])


def test_mastery_unmastered_always_allowed_and_intervals_and_3streak_archive(client):
    q = bank_question()
    qid = q["qid"]
    # §4.2: 「未掌握」allowed even before any attempt
    assert client.post("/api/mastery", json={"qid": qid, "level": "未掌握"}).status_code == 200
    # 「初步掌握」still requires >= 1 attempt
    assert client.post("/api/mastery", json={"qid": qid, "level": "初步掌握"}).status_code == 403

    # Wrong answer puts question into wrongbook with interval=1
    wrong = next(o["label"] for o in q["options"] if o["label"] != q["answer_text"])
    client.post("/api/attempts", data={"qid": qid, "answer": wrong})
    assert client.post("/api/wrongbook/tags", json={"qid": qid, "error_tags": ["概念不清", "审题失误"]}).status_code == 200

    # Correct 1st time -> mark 比较掌握 -> interval >= 4
    client.post("/api/attempts", data={"qid": qid, "answer": q["answer_text"]})
    assert client.post("/api/mastery", json={"qid": qid, "level": "比较掌握"}).status_code == 200
    wb1 = client.get("/api/wrongbook").json()["items"][0]
    assert wb1["interval_days"] >= 4
    assert wb1["archived"] is False

    # 2nd and 3rd consecutive correct -> auto-archived!
    client.post("/api/attempts", data={"qid": qid, "answer": q["answer_text"]})
    client.post("/api/attempts", data={"qid": qid, "answer": q["answer_text"]})
    assert client.get("/api/wrongbook").json()["total"] == 0
    archived_wb = client.get("/api/wrongbook", params={"include_archived": "true"}).json()
    assert archived_wb["total"] == 1
    assert archived_wb["items"][0]["archived"] is True


# ---------------------------------------------------------------------------
# 3. Knowledge Points Tree, Tagging & Review Queue
# ---------------------------------------------------------------------------

def test_knowledge_point_tree_seeded_import_tagging_and_review(client, monkeypatch):
    kps = client.get("/api/knowledge-points", params={"subject": "数学"}).json()["items"]
    assert len(kps) >= 15
    target_kp = next(k for k in kps if k["level"] == 3)
    q = bank_question()
    qid = q["qid"]

    # Manual tag & filter by parent chapter expands descendants
    assert client.post("/api/admin/question-kp", json={"qid": qid, "kp_ids": [target_kp["id"]]}).status_code == 200
    parent_section = next(k for k in kps if k["id"] == target_kp["parent_id"])
    filtered = client.get("/api/questions", params={"kp_id": str(parent_section["id"])}).json()
    assert any(it["qid"] == qid for it in filtered["items"])

    # CSV import adds custom KP cleanly
    csv_data = "subject,chapter,section,point\n数学,函数与导数,测试新节,隐零点技巧\n".encode("utf-8")
    imp = client.post("/api/admin/knowledge-points/import", files={"file": ("kps.csv", csv_data, "text/csv")})
    assert imp.status_code == 200
    assert imp.json()["imported"] >= 1

    # Batch AI tagging job with low-confidence item entering review queue
    provider = save_provider(client)
    est = client.post("/api/admin/tagging/estimate", json={"subject": "物理", "limit": 5}).json()
    assert est["will_process"] == 5

    phys_kps = client.get("/api/knowledge-points", params={"subject": "物理"}).json()["items"]
    valid_kp_id = phys_kps[0]["id"]

    tag_call_meta = []

    async def fake_tag_call(prov, model, prompt, *a, **kw):
        import re
        tag_call_meta.append(kw)
        qids_found = [q for q in re.findall(r'"qid":\s*"([^"]+)"', prompt) if q != "..."]
        items = []
        for idx, found_qid in enumerate(qids_found):
            items.append({
                "qid": found_qid,
                "kps": [{"kp_id": valid_kp_id, "confidence": 0.92 if idx == 0 else 0.55}],
                "difficulty_score": 4,
            })
        return {"content": json.dumps({"items": items}), "model": model, "provider_name": "mock", "usage": {"total_tokens": 10}}

    monkeypatch.setattr(ai, "_call_model", fake_tag_call)
    with db.connection() as con:
        jid = con.execute(
            "INSERT INTO ai_jobs(user_id,task,status,stage,request_json,created_at,updated_at) VALUES (1,'tagging','running','test','{}',?,?)",
            (db.utcnow(), db.utcnow()),
        ).lastrowid
    data = ai.JobInput(task="tagging", subject="物理", tagging_limit=5, provider_id=provider["id"], model="test-model")
    res = asyncio.run(ai._tagging_job(jid, 1, data, {"id": provider["id"]}))
    assert res["auto_tagged"] == 1
    assert res["needs_review"] == 4
    # 批量打标的每次模型调用都必须带任务记账标识
    assert tag_call_meta and all(m.get("job_id") == jid and m.get("user_id") == 1 for m in tag_call_meta)

    queue = client.get("/api/admin/kp-review-queue", params={"subject": "物理"}).json()["items"]
    assert len(queue) == 5
    first = queue[0]
    assert client.post("/api/admin/question-kp/review", json={"qid": first["qid"], "kp_id": first["kp_id"], "action": "approve"}).status_code == 200


# ---------------------------------------------------------------------------
# 4. Provider PATCH, Fetch Models, Model Caps, Custom Allowed Hosts & Usage
# ---------------------------------------------------------------------------

def test_provider_edit_fetch_models_caps_and_custom_hosts(client, monkeypatch):
    p = save_provider(client, models=["gpt-4o", "deepseek-r1"])
    assert p["model_details"][0]["caps"]["vision"] is True
    assert p["model_details"][1]["caps"]["reasoning"] is True

    # Edit provider without re-entering key
    patched = client.patch(f"/api/providers/{p['id']}", json={"name": "更新后的名称", "models": ["gpt-4o", "o3-mini"]})
    assert patched.status_code == 200
    assert patched.json()["name"] == "更新后的名称"
    assert "o3-mini" in patched.json()["models"]

    # Update model metadata & price
    m_id = patched.json()["model_details"][0]["id"]
    m_upd = client.patch(f"/api/models/{m_id}", json={"caps": {"vision": False}, "input_price": 2.5, "output_price": 10.0})
    assert m_upd.status_code == 200
    assert m_upd.json()["caps"]["vision"] is False
    assert m_upd.json()["input_price"] == 2.5

    # Mock GET /models fetch
    def respond(req):
        assert req.url.path == "/v1/models"
        return httpx.Response(200, json={"data": [{"id": "gpt-4o"}, {"id": "gpt-4.1-mini"}]})

    orig_client = httpx.AsyncClient
    monkeypatch.setattr(ai.httpx, "AsyncClient", lambda **kw: orig_client(transport=httpx.MockTransport(respond), **kw))
    monkeypatch.setattr(ai, "_validate_url", lambda v, **kw: v)
    fetched = client.post(f"/api/providers/{p['id']}/fetch-models")
    assert fetched.status_code == 200
    assert fetched.json()["count"] == 2

    # Admin custom allowed hosts in DB
    assert client.post("/api/admin/allowed-hosts", json={"hostname": "127.0.0.1"}).status_code == 422
    assert client.post("/api/admin/allowed-hosts", json={"hostname": "gateway.my-school.edu.cn"}).status_code == 200
    hosts = client.get("/api/admin/allowed-hosts").json()
    assert any(h["hostname"] == "gateway.my-school.edu.cn" for h in hosts["custom"])


# ---------------------------------------------------------------------------
# 5. Cross-Verification 2.0 (Programmatic / SymPy / Judge C / Peer Review / Persist)
# ---------------------------------------------------------------------------

def test_programmatic_and_sympy_answer_comparison():
    # Choice set
    c1 = ai.compare_answers({"kind": "choice", "value": "BA"}, {"kind": "choice", "value": "A, B"}, reference="AB")
    assert c1["agreed"] is True and c1["matches_reference"] is True
    # Numeric with identical units (相对误差 1e-6)
    n1 = ai.compare_answers({"kind": "number", "value": "3.14 m/s"}, {"kind": "number", "value": "3.1400001 m/s"}, reference="3.14 m/s")
    assert n1["agreed"] is True and n1["method"] == "numeric_rel_1e-6"
    # 一侧缺单位时不得当成一致，必须降为待核验（P0.1 止损口径）
    n2 = ai.compare_answers({"kind": "number", "value": "3.14 m/s"}, {"kind": "number", "value": "3.1400001"})
    assert n2["comparable"] is False and n2["reason"] == "unit_missing"
    # 同形异量纲明确不可比
    n3 = ai.compare_answers({"kind": "number", "value": "2 m"}, {"kind": "number", "value": "2 s"})
    assert n3["comparable"] is False and n3["reason"] == "unit_mismatch"
    # JSON 数值零是有效答案
    n4 = ai.compare_answers({"kind": "number", "value": 0}, {"kind": "number", "value": 0.0})
    assert n4["comparable"] is True and n4["agreed"] is True
    # SymPy expression equivalence
    e1 = ai.compare_answers({"kind": "expression", "value": "(x+1)^2"}, {"kind": "expression", "value": "x^2 + 2x + 1"})
    assert e1["agreed"] is True and e1["method"] == "sympy"
    e2 = ai.compare_answers({"kind": "expression", "value": "x+1"}, {"kind": "expression", "value": "x+2"})
    assert e2["agreed"] is False
    # 受限语法外的表达式不给出一致结论
    e3 = ai.compare_answers({"kind": "expression", "value": "__import__('os')"}, {"kind": "expression", "value": "x"})
    assert e3["comparable"] is False and e3["agreed"] is None


def test_cross_verify_v2_judge_never_primary_and_persists_explanation(client, monkeypatch):
    q = bank_question()
    qid = q["qid"]
    client.post("/api/attempts", data={"qid": qid, "answer": q["answer_text"]})
    provider = save_provider(client, models=["model-a", "model-b", "model-c"])

    sol_a = json.dumps({
        "final_answer": {"kind": "choice", "value": "A"},
        "steps": [{"no": 1, "title": "分析", "content": "推导得出 A", "score_point": True}],
        "knowledge_points": ["导数的几何意义"], "method": "切线法", "assumptions": [], "uncertain": [],
    })
    sol_b = json.dumps({
        "final_answer": {"kind": "choice", "value": "B"},
        "steps": [{"no": 1, "title": "分析", "content": "推导得出 B", "score_point": True}],
        "knowledge_points": ["导数的几何意义"], "method": "切线法", "assumptions": [], "uncertain": [],
    })
    judge_out = json.dumps({
        "agreed": False, "diverge_at_step": 1, "which_is_right": "A",
        "reason": "B 在第 1 步符号错误", "confidence": "high", "summary": "第1步符号分歧", "differences": ["第1步"],
    })
    called_models = []
    outputs = [sol_a, sol_b, judge_out, "A 维持原解", "B 承认第 1 步有误并修正", "## 详细解析正文\n讲解内容"]

    async def fake_call(prov, model, prompt, *a, **kw):
        called_models.append(model)
        return {"content": outputs.pop(0), "model": model, "provider_name": "mock", "usage": {"prompt_tokens": 50, "completion_tokens": 30}}

    monkeypatch.setattr(ai, "_call_model", fake_call)
    data = ai.JobInput(
        task="explain", qid=qid, provider_id=provider["id"], model="model-a",
        cross_verify=True, verify_provider_id=provider["id"], verify_model="model-b",
        judge_provider_id=provider["id"], judge_model="model-c",
        structured_verify=True, peer_review=True, explain_mode="detailed",
    )
    owner = client.get("/api/auth/me").json()["id"]
    result = asyncio.run(ai._explain_job(
        owner, data, {"id": provider["id"], "name": "p"}, {"id": provider["id"], "name": "p"},
        judge={"id": provider["id"], "name": "p"}, judge_model="model-c",
    ))
    assert called_models[2] == "model-c"  # Independent judge model C, NEVER model-a!
    assert result["verification"]["status"] == "disagreed"
    assert result["verification"]["judge"]["diverge_at_step"] == 1
    assert result["verification"]["peer_review"]["round"] == 1

    # Persisted explanation retrieved via GET /api/explanations
    exps = client.get("/api/explanations", params={"qid": qid}).json()["items"]
    assert len(exps) == 1
    assert exps[0]["mode"] == "detailed"


# ---------------------------------------------------------------------------
# 6. Photo Upload 2-step Pipeline (Multi-image -> Recognize -> Confirm -> Similar)
# ---------------------------------------------------------------------------

def test_photo_two_step_pipeline_and_similar_bank_match(client, monkeypatch):
    q = bank_question()
    res = client.post("/api/uploads/question", files=[
        ("files", ("stem.png", _drawn_image(), "image/png")),
        ("files", ("fig.png", _drawn_image(), "image/png")),
    ])
    assert res.status_code == 200
    up = res.json()
    assert len(up["extra_previews"]) == 1
    assert client.get(up["extra_previews"][0]["preview_url"]).status_code == 200

    # Confirm with text matching a real bank question triggers similar match
    conf = client.patch(f"/api/uploads/{up['id']}/confirm", json={
        "subject": q["subject"], "type": "单选题", "stem": q["stem_plain"], "conditions": ["已知图示"],
    })
    assert conf.status_code == 200
    sim = conf.json()["similar_questions"]
    assert any(s["qid"] == q["qid"] and s["exact_match"] for s in sim)


# ---------------------------------------------------------------------------
# 7. Notes Agent: Heading Chunking, Prompt Grid, HITL Outline, Versioning & Exports
# ---------------------------------------------------------------------------

def test_notes_agent_heading_chunks_prompt_grid_hitl_versions_and_exports(client, monkeypatch):
    md_doc = "# 第一章 函数\n## 1.1 定义域\n函数定义域是自变量取值范围。\n\n## 1.2 单调性\n利用导数判断单调区间。"
    doc = client.post("/api/documents", files={"file": ("函数笔记.md", md_doc.encode("utf-8"), "text/markdown")}).json()
    assert doc["status"] == "ready"
    assert doc["chunks"] >= 1

    # Prompt Grid preview returns L1-L4 without leaking L0
    grid = client.get("/api/prompt-grid").json()
    assert "format" in grid["dimensions"]
    prev = client.post("/api/prompt-grid/preview", json={
        "config": {"format": "anki", "stage": "gaokao_sprint", "depth": "concise", "focus": ["mistakes"], "extras": ["mnemonic"], "style": "blackboard"},
        "subject": "数学", "requirements": "多举反例",
    }).json()
    assert "问答卡片" in prev["preview"]
    assert "数学学科要求" in prev["preview"]
    assert "不可信任务数据" not in prev["preview"]  # L0 stays hidden

    # Save & list user prompt template
    tpl = client.post("/api/prompt-templates", json={"name": "我的冲刺模板", "config": {"format": "one_pager"}, "custom_requirements": "简明"}).json()
    assert any(t["id"] == tpl["id"] for t in client.get("/api/prompt-templates").json()["items"])

    # HITL outline pause & confirm resume
    provider = save_provider(client)
    async def fake_notes_call(prov, model, prompt, *a, **kw):
        if "先根据资料设计" in prompt:
            return {"content": "## 1. 定义域\n## 2. 单调性", "model": model, "provider_name": "mock", "usage": {}}
        return {"content": f"### Q: 什么是定义域？\n**A:** 自变量取值范围 [D{doc['id']}:p1-c1]", "model": model, "provider_name": "mock", "usage": {}}

    monkeypatch.setattr(ai, "_call_model", fake_notes_call)
    job = client.post("/api/ai/jobs", json={
        "task": "notes", "document_id": doc["id"], "provider_id": provider["id"],
        "model": "test-model", "confirm_outline": True, "prompt_config": {"format": "anki"},
    }).json()
    # Wait briefly for background task to pause at awaiting_outline
    import time
    for _ in range(30):
        st = client.get(f"/api/ai/jobs/{job['id']}").json()
        if st["status"] == "awaiting_outline":
            break
        time.sleep(0.05)
    assert st["status"] == "awaiting_outline"
    assert "定义域" in st["result"]["outline"]

    # Confirm outline -> generates sections in parallel -> completes & saves note!
    resumed = client.post(f"/api/ai/jobs/{job['id']}/confirm-outline", json={
        "outline": "## 1. 定义域\n## 2. 单调性",
    })
    assert resumed.status_code == 200
    for _ in range(40):
        st2 = client.get(f"/api/ai/jobs/{job['id']}").json()
        if st2["status"] == "completed":
            break
        time.sleep(0.05)
    assert st2["status"] == "completed"
    note_id = st2["result"]["note_id"]

    # Edit note & check version history + rollback
    client.patch(f"/api/notes/{note_id}", json={"content": "修改后的内容", "change_note": "精简表述"})
    vers = client.get(f"/api/notes/{note_id}/versions").json()["items"]
    assert len(vers) == 2
    v1_id = vers[-1]["id"]
    rolled = client.post(f"/api/notes/{note_id}/rollback/{v1_id}").json()
    assert "定义域" in rolled["content"]

    # Exports: Anki CSV & Mermaid Mindmap
    anki_csv = client.get(f"/api/notes/{note_id}/export", params={"format": "anki"}).text
    assert "Front,Back,Tags" in anki_csv
    mindmap = client.get(f"/api/notes/{note_id}/export", params={"format": "mindmap"}).text
    assert mindmap.startswith("mindmap")


# ---------------------------------------------------------------------------
# 8. Job Cancellation & SSE Stream Endpoint
# ---------------------------------------------------------------------------

def test_job_cancel_and_sse_stream_endpoint(client, monkeypatch):
    provider = save_provider(client)
    up = client.post("/api/uploads/question", files={"file": ("t.txt", "求导".encode(), "text/plain")}).json()

    async def slow_call(*a, **kw):
        await asyncio.sleep(5)
        return {"content": "done", "model": "m", "provider_name": "p", "usage": {}}

    monkeypatch.setattr(ai, "_call_model", slow_call)
    job = client.post("/api/ai/jobs", json={
        "task": "explain", "upload_id": up["id"], "provider_id": provider["id"], "model": "test-model"
    }).json()
    cancelled = client.post(f"/api/ai/jobs/{job['id']}/cancel")
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"

    # SSE stream on terminal job immediately sends snapshot + done
    sse = client.get(f"/api/ai/jobs/{job['id']}/stream")
    assert sse.status_code == 200
    assert "event: snapshot" in sse.text
    assert "event: done" in sse.text



# ---------------------------------------------------------------------------
# 9. Polish items: statistical difficulty SQL filter, same_kp_only, methods,
#    upload wrongbook, and KP tree alignment in Notes Agent
# ---------------------------------------------------------------------------

def test_statistical_difficulty_filter_and_multi_select_and_methods(client):
    q = bank_question()
    qid = q["qid"]
    # Insert 20 graded attempts (16 correct = 80% -> statistical '简单')
    owner = client.get("/api/auth/me").json()["id"]
    now = db.utcnow()
    with db.connection() as con:
        for i in range(20):
            res_str = "correct" if i < 16 else "incorrect"
            con.execute(
                "INSERT INTO attempts(user_id,qid,answer_text,result,grade_source,created_at,graded_at) VALUES (?,?,?,?,?,?,?)",
                (owner, qid, "A", res_str, "reference", now, now),
            )
    # Filtering by difficulty='简单' finds this question via statistical difficulty SQL!
    easy_res = client.get("/api/questions", params={"difficulty": "简单"}).json()
    assert any(it["qid"] == qid and it["difficulty_source"] == "统计" for it in easy_res["items"])

    # Multi-select type & has_answer
    multi = client.get("/api/questions", params={"type": "single_choice,multi_choice", "has_answer": "true", "page_size": 5}).json()
    assert multi["total"] > 0
    assert all(it["type"] in ("single_choice", "multi_choice") and it["has_answer"] for it in multi["items"])

    # Admin method tag + filter by method_id
    m_res = client.post("/api/admin/question-method", json={"qid": qid, "method_name": "导数-含参讨论"})
    assert m_res.status_code == 200
    mid = m_res.json()["method_id"]
    methods_list = client.get("/api/methods", params={"subject": q["subject"]}).json()["items"]
    assert any(m["id"] == mid for m in methods_list)
    by_m = client.get("/api/questions", params={"method_id": str(mid)}).json()
    assert [it["qid"] for it in by_m["items"]] == [qid]


def test_same_kp_only_interleave_and_upload_wrongbook(client):
    # Enable same_kp_only preference
    pref = client.patch("/api/preferences", json={"same_kp_only": True, "interleave_per_page": 2}).json()
    assert pref["same_kp_only"] is True

    kps = client.get("/api/knowledge-points", params={"subject": "数学"}).json()["items"]
    kp1, kp2 = [k for k in kps if k["level"] == 3][:2]

    page1 = client.get("/api/questions", params={"subject": "数学", "page": 1, "page_size": 12}).json()["items"]
    page3 = client.get("/api/questions", params={"subject": "数学", "page": 3, "page_size": 12}).json()["items"]
    # Tag page1[0] with kp1; tag page3[0] with kp1 (matching) and page3[1] with kp2 (non-matching)
    client.post("/api/admin/question-kp", json={"qid": page1[0]["qid"], "kp_ids": [kp1["id"]]})
    client.post("/api/admin/question-kp", json={"qid": page3[0]["qid"], "kp_ids": [kp1["id"]]})
    client.post("/api/admin/question-kp", json={"qid": page3[1]["qid"], "kp_ids": [kp2["id"]]})

    client.post("/api/wrongbook", json={"qid": page3[0]["qid"]})
    client.post("/api/wrongbook", json={"qid": page3[1]["qid"]})

    res = client.get("/api/questions", params={"subject": "数学", "page": 1, "page_size": 12, "include_reviews": "true"}).json()
    slot_qids = [s["qid"] for s in res["review_slots"]]
    assert page3[0]["qid"] in slot_qids
    assert page3[1]["qid"] not in slot_qids

    # Uploaded photo wrongbook CRUD
    up = client.post("/api/uploads/question", files={"file": ("my_q.png", _drawn_image(), "image/png")}).json()
    add = client.post("/api/wrongbook/upload", json={
        "upload_id": up["id"], "subject": "数学", "stem_preview": "求函数极值", "error_tags": ["计算错误"]
    })
    assert add.status_code == 200
    ups = client.get("/api/wrongbook/uploads").json()
    assert ups["total"] == 1 and ups["items"][0]["upload_id"] == up["id"]
    assert client.delete(f"/api/wrongbook/uploads/{up['id']}").status_code == 200
    assert client.get("/api/wrongbook/uploads").json()["total"] == 0
