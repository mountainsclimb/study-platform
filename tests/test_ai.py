import asyncio
from io import BytesIO
import json
import re

from fastapi import HTTPException
import httpx
from PIL import Image
import pytest

from app import ai, db, practice


def save_provider(client, **changes):
    data = {"name": "测试连接", "kind": "openai", "base_url": "https://api.openai.com/v1",
            "api_key": "sk-unit-test-secret-value", "models": ["test-model", "verify-model"]}
    data.update(changes)
    response = client.post("/api/providers", json=data)
    assert response.status_code == 200, response.text
    return response.json()


def test_provider_keys_encrypted_and_never_returned(client, second_client, isolated_db):
    saved = save_provider(client)
    assert "sk-unit-test-secret-value" not in json.dumps(saved)
    assert "encrypted_key" not in saved
    assert "api_key" not in saved
    with db.connection() as con:
        row = con.execute("SELECT encrypted_key FROM ai_providers WHERE id=?", (saved["id"],)).fetchone()
    assert row[0] != "sk-unit-test-secret-value"
    assert ai._cipher().decrypt(row[0].encode()).decode() == "sk-unit-test-secret-value"
    assert second_client.get("/api/providers").json()["items"] == []
    assert second_client.delete(f"/api/providers/{saved['id']}").status_code == 404
    assert client.delete(f"/api/providers/{saved['id']}").status_code == 200


def test_short_key_hint_cannot_be_full_key(client):
    saved = save_provider(client, api_key="12345678")
    assert "12345678" not in json.dumps(saved)
    assert saved["key_hint"] == "已加密"


@pytest.mark.parametrize("url", [
    "http://api.openai.com/v1", "https://127.0.0.1/v1", "https://localhost/v1",
    "https://evil.example/v1", "https://user:pass@api.openai.com/v1",
    "https://api.openai.com:not-a-port/v1", "https://api.openai.com/v1?key=secret",
    "https://api.openai.com/v1#fragment", "http://169.254.169.254/latest/meta-data",
])
def test_reject_unsafe_endpoints(client, monkeypatch, url):
    # 严格白名单模式（公网托管）：默认公网模式为 public_https，可用 allowlist 进一步收紧。
    monkeypatch.setenv("STUDY_AI_URL_POLICY", "allowlist")
    response = client.post("/api/providers", json={"name": "unsafe", "kind": "openai",
        "base_url": url, "api_key": "do-not-leak", "models": ["model"]})
    assert response.status_code == 422


def test_custom_allowlist_still_blocks_private_dns(monkeypatch):
    monkeypatch.setenv("STUDY_AI_URL_POLICY", "allowlist")
    monkeypatch.setenv("STUDY_AI_ALLOWED_HOSTS", "custom.example")
    monkeypatch.setattr(ai.socket, "getaddrinfo", lambda *a, **kw: [(2, 1, 6, "", ("10.0.0.1", 443))])
    with pytest.raises(HTTPException) as exc:
        ai._validate_url("https://custom.example/v1", resolve=True)
    assert exc.value.status_code == 422


def test_duplicate_connection_or_protocol_cannot_fake_independence(client):
    first = save_provider(client)
    duplicate = save_provider(client, name="重复连接", kind="gemini")
    response = client.post("/api/ai/jobs", json={"task": "question-analysis", "upload_id": 1,
        "provider_id": first["id"], "model": "test-model", "cross_verify": True,
        "verify_provider_id": duplicate["id"], "verify_model": "test-model"})
    assert response.status_code == 422
    assert "不同模型" in response.json()["detail"]


def test_bank_ai_enforces_submission_gate(client):
    provider = save_provider(client)
    qid = client.get("/api/questions?page_size=1").json()["items"][0]["qid"]
    response = client.post("/api/ai/jobs", json={"task": "explain", "qid": qid,
        "provider_id": provider["id"], "model": "test-model"})
    assert response.status_code == 403


def test_uploads_and_documents_are_private(client, second_client):
    uploaded = client.post("/api/uploads/question", files={"file": ("题目.md", "计算 1+1".encode(), "text/plain")})
    assert uploaded.status_code == 200
    upload_id = uploaded.json()["id"]
    assert "计算" in client.get(f"/api/uploads/{upload_id}").text
    assert second_client.get(f"/api/uploads/{upload_id}").status_code == 404
    document = client.post("/api/documents", files={"file": ("资料.txt", "定义和公式\n引用应可定位".encode(), "text/plain")})
    assert document.status_code == 200
    assert document.json()["status"] == "ready"
    assert second_client.get("/api/documents").json()["items"] == []
    assert client.post("/api/uploads/question", files={"file": ("假.png", b"not an image", "image/png")}).status_code == 422
    assert client.post("/api/documents", files={"file": ("程序.exe", b"program", "application/octet-stream")}).status_code == 422


def test_valid_image_is_verified_and_served(client):
    image = BytesIO()
    Image.new("RGB", (20, 20), "white").save(image, "PNG")
    response = client.post("/api/uploads/question", files={"file": ("题目.png", image.getvalue(), "image/png")})
    assert response.status_code == 200
    preview = client.get(response.json()["preview_url"])
    assert preview.status_code == 200
    assert preview.headers["content-type"] == "image/png"
    assert "no-store" in preview.headers["cache-control"]


def test_scanned_pdf_is_not_fake_success(client):
    from pypdf import PdfWriter
    writer = PdfWriter()
    writer.add_blank_page(width=600, height=800)
    raw = BytesIO()
    writer.write(raw)
    response = client.post("/api/documents", files={"file": ("扫描.pdf", raw.getvalue(), "application/pdf")})
    assert response.status_code == 200
    assert response.json()["status"] == "needs_ocr"
    provider = save_provider(client)
    job = client.post("/api/ai/jobs", json={"task": "notes", "document_id": response.json()["id"],
        "provider_id": provider["id"], "model": "test-model"})
    assert job.status_code == 422


def test_docx_tables_preserve_original_block_order():
    from docx import Document
    document = Document()
    document.add_paragraph("段落A")
    table = document.add_table(rows=1, cols=1)
    table.cell(0, 0).text = "中间表格"
    document.add_paragraph("针对上表的段落B")
    raw = BytesIO()
    document.save(raw)
    chunks, _, _ = ai._extract_document("表格.docx", raw.getvalue())
    text = "\n".join(c["text"] for c in chunks)
    assert text.index("段落A") < text.index("中间表格") < text.index("针对上表")


def test_note_owner_and_official_publishing(client, second_client):
    created = client.post("/api/notes", json={"title": "官方草稿", "content": "知识清单成品"})
    assert created.status_code == 200
    note_id = created.json()["id"]
    assert second_client.get(f"/api/notes/{note_id}").status_code == 404
    assert second_client.post(f"/api/notes/{note_id}/publish").status_code == 403
    assert client.post(f"/api/notes/{note_id}/publish").status_code == 200
    shared = second_client.get(f"/api/notes/{note_id}")
    assert shared.json()["origin"] == "official"
    assert second_client.patch(f"/api/notes/{note_id}", json={"content": "篡改"}).status_code == 404
    assert second_client.delete(f"/api/notes/{note_id}").status_code == 404
    student_note = second_client.post("/api/notes", json={"title": "学生资料", "content": "私人笔记"}).json()
    assert client.post(f"/api/notes/{student_note['id']}/publish").status_code == 404


def rubric():
    return {"max_score": 100, "criteria": [{"label": "结论", "possible": 40, "expected": "结果正确"},
                                           {"label": "推导", "possible": 60, "expected": "推导有效"}]}


def grade(earned=(40, 60)):
    total = sum(earned)
    return {"is_correct": total == 100, "score": total, "max_score": 100,
            "steps": [{"label": "结论", "earned": earned[0], "possible": 40, "feedback": "检查结果"},
                      {"label": "推导", "earned": earned[1], "possible": 60, "feedback": "检查推导"}], "feedback": "评分意见"}


def test_grade_schema_rejects_score_and_rubric_drift():
    assert ai._parse_grade(json.dumps(grade()), rubric())["is_correct"] is True
    bad = grade()
    bad["score"] = 99
    with pytest.raises(ValueError):
        ai._parse_grade(json.dumps(bad), rubric())
    bad = grade()
    bad["steps"][0]["label"] = "自创量表"
    with pytest.raises(ValueError):
        ai._parse_grade(json.dumps(bad), rubric())
    with pytest.raises(ValueError):
        ai._parse_rubric('{"max_score":100,"criteria":[]}')
    assert not ai._usable_reference("____")


@pytest.mark.parametrize("second,expected", [(grade(), "correct"), (grade((30, 60)), "pending"), (ValueError("offline"), "pending")])
def test_independent_grading_agreement_and_failure(monkeypatch, second, expected):
    captured = []
    prompts = []
    values = [rubric(), grade(), second]
    monkeypatch.setattr(ai, "_bank_evidence", lambda *a: ({"原卷参考答案": "答案为2", "原卷解析": "1+1=2"}, []))
    monkeypatch.setattr(practice, "latest_attempt", lambda *a: {"id": 7, "answer_text": "1+1=2"})
    monkeypatch.setattr(practice, "get_attempt_image", lambda *a: None)
    monkeypatch.setattr(practice, "set_ai_grade", lambda *a: captured.append(a))

    async def fake_call(provider, model, prompt, *args, **kwargs):
        prompts.append(prompt)
        value = values.pop(0)
        if isinstance(value, Exception):
            raise value
        return {"content": json.dumps(value), "model": model, "provider_name": "mock", "usage": {"total_tokens": 1}}

    monkeypatch.setattr(ai, "_call_model", fake_call)
    data = ai.JobInput(task="grade", qid="q", attempt_id=7, provider_id=1, model="one", cross_verify=True, verify_provider_id=2, verify_model="two")
    report = asyncio.run(ai._grade_job(1, data, {"id": 1}, {"id": 2}))
    assert captured[-1][2] == expected
    assert report["verified"] == (expected == "correct")
    # One common rubric is created without student work. Both graders see identical
    # prompts and neither receives its peer's result.
    assert "学生作答文字" not in prompts[0]
    assert prompts[1] == prompts[2]


def test_missing_reference_never_grades_correct(monkeypatch):
    saved = []
    monkeypatch.setattr(ai, "_bank_evidence", lambda *a: ({"原卷参考答案": "____"}, []))
    monkeypatch.setattr(practice, "latest_attempt", lambda *a: {"id": 7})
    monkeypatch.setattr(practice, "set_ai_grade", lambda *a: saved.append(a))
    data = ai.JobInput(task="grade", qid="q", attempt_id=7, provider_id=1, model="one")
    result = asyncio.run(ai._grade_job(1, data, {}, {}))
    assert saved[-1][2] == "pending"
    assert result["verification"]["status"] == "partial"


def test_reference_grade_cannot_be_overwritten_by_ai(client, monkeypatch):
    provider = save_provider(client)
    monkeypatch.setattr(practice, "ensure_unlocked", lambda *a: None)
    monkeypatch.setattr(practice, "load_question", lambda *a: {})
    monkeypatch.setattr(practice, "latest_attempt", lambda *a: {"id": 7, "result": "incorrect", "grade_source": "reference"})
    response = client.post("/api/ai/jobs", json={"task": "grade", "qid": "qid", "attempt_id": 7,
        "provider_id": provider["id"], "model": "test-model", "cross_verify": True,
        "verify_provider_id": provider["id"], "verify_model": "verify-model"})
    assert response.status_code == 422
    assert "原卷" in response.json()["detail"]


def test_same_attempt_grade_task_is_deduplicated(client, monkeypatch):
    provider = save_provider(client)
    monkeypatch.setattr(practice, "ensure_unlocked", lambda *a: None)
    monkeypatch.setattr(practice, "load_question", lambda *a: {})
    monkeypatch.setattr(practice, "latest_attempt", lambda *a: {"id": 7, "result": "pending", "grade_source": "reference"})

    async def remain_queued(*args):
        return None

    monkeypatch.setattr(ai, "_run_job", remain_queued)
    payload = {"task": "grade", "qid": "qid", "attempt_id": 7,
        "provider_id": provider["id"], "model": "test-model", "cross_verify": True,
        "verify_provider_id": provider["id"], "verify_model": "verify-model"}
    first = client.post("/api/ai/jobs", json=payload)
    second = client.post("/api/ai/jobs", json=payload)
    assert first.status_code == 200
    assert second.status_code == 409


def test_bad_comparison_keeps_both_independent_solutions(client, monkeypatch):
    saved = client.post("/api/uploads/question", files={"file": ("题目.txt", b"1+1=?", "text/plain")}).json()
    outputs = ["第一份独立题解", "第二份独立题解", "[]"]

    async def fake_call(provider, model, *a, **kw):
        return {"content": outputs.pop(0), "model": model, "provider_name": "mock", "usage": {}}

    monkeypatch.setattr(ai, "_call_model", fake_call)
    data = ai.JobInput(task="question-analysis", upload_id=saved["id"], provider_id=1, model="one", cross_verify=True, verify_provider_id=2, verify_model="two")
    result = asyncio.run(ai._explain_job(1, data, {"id": 1}, {"id": 2}))
    assert result["verification"]["status"] == "partial"
    assert len(result["verification"]["independent_results"]) == 2
    assert result["content"] == "第一份独立题解"


@pytest.mark.parametrize("kind,host", [("openai", "api.openai.com"), ("openai", "api.deepseek.com"), ("anthropic", "api.anthropic.com"), ("gemini", "generativelanguage.googleapis.com")])
def test_model_protocols_without_spending_credits(client, monkeypatch, kind, host):
    captured = []

    def respond(request):
        captured.append((request, json.loads(request.content)))
        if kind == "anthropic":
            return httpx.Response(200, json={"content": [{"type": "text", "text": "模拟返回"}], "stop_reason": "end_turn", "usage": {"input_tokens": 1}})
        return httpx.Response(200, json={"choices": [{"message": {"content": "模拟返回"}, "finish_reason": "stop"}], "usage": {"total_tokens": 2}})

    constructor = httpx.AsyncClient
    monkeypatch.setattr(ai.httpx, "AsyncClient", lambda **kw: constructor(transport=httpx.MockTransport(respond), **kw))
    monkeypatch.setattr(ai, "_validate_url", lambda value, **kw: value)
    provider = {"kind": kind, "name": "mock", "base_url": "https://" + host + "/v1", "encrypted_key": ai._cipher().encrypt(b"test-secret-key").decode()}
    result = asyncio.run(ai._call_model(provider, "test-model", "只返回 JSON", json_output=True))
    assert result["content"] == "模拟返回"
    request, body = captured[0]
    assert body["model"] == "test-model"
    if kind == "anthropic":
        assert request.url.path == "/v1/messages"
        assert request.headers["x-api-key"] == "test-secret-key"
        assert "system" in body
    else:
        assert request.url.path == "/v1/chat/completions"
        assert request.headers["authorization"] == "Bearer test-secret-key"
        assert body["response_format"]["type"] == "json_object"
        # max_tokens 参数按模型能力选择，不再按 api.openai.com 域名硬编码。
        assert "max_completion_tokens" not in body
        assert body["max_tokens"] == 5000


def test_long_document_workflow_reads_last_chunk(client, monkeypatch):
    text = "这是完整资料的一部分，需要有来源。" * 4000
    response = client.post("/api/documents", files={"file": ("长资料.txt", text.encode(), "text/plain")})
    assert response.status_code == 200
    document_id = response.json()["id"]
    provider = save_provider(client)
    with db.connection() as con:
        row = con.execute("SELECT chunks_json FROM ai_documents WHERE id=?", (document_id,)).fetchone()
        chunks = json.loads(row[0])
        job_id = con.execute("INSERT INTO ai_jobs(user_id,task,status,stage,request_json,created_at,updated_at) VALUES (1,'notes','running','test','{}',?,?)", (db.utcnow(), db.utcnow())).lastrowid
    read_citations = set()

    async def fake_call(provider, model, prompt, *args, **kwargs):
        citations = set(re.findall(r"\[D\d+:[^\]\s]+\]", prompt))
        if "阅读本批所有资料" in prompt:
            read_citations.update(citations)
            content = "\n".join("资料摘要 " + c for c in sorted(citations))
        elif "先根据资料设计" in prompt:
            content = "概念与条件"
        else:
            content = "\n".join("- 知识要点 " + c for c in sorted(citations))
        return {"content": content, "model": model, "provider_name": "mock", "usage": {}}

    monkeypatch.setattr(ai, "_call_model", fake_call)
    data = ai.JobInput(task="notes", document_id=document_id, provider_id=provider["id"], model="test-model")
    result = asyncio.run(ai._notes_job(job_id, 1, data, {"id": 1}, None))
    expected = {f"[D{document_id}:{c['chunk_id']}]" for c in chunks}
    assert read_citations == expected
    assert result["coverage"]["complete"] is True
    assert result["coverage"]["included_chunks"] == len(chunks)
    assert result["coverage"]["batches"] > 1
    note = client.get(f"/api/notes/{result['note_id']}").json()
    assert len(note["sources"]) == len(chunks)
