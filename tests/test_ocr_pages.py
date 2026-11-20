"""P1「OCR 分页续做」验收：逐页状态机、分页合并、失败重试、人工校对保留与资料版本。"""
import asyncio
from io import BytesIO
import json
import re

from pypdf import PdfWriter
import pytest

from app import ai, db
from app.ai import notes_agent


def save_provider(client, name="OCR 测试连接"):
    response = client.post("/api/providers", json={
        "name": name, "kind": "openai", "base_url": "https://api.openai.com/v1",
        "api_key": "sk-ocr-unit-test-secret", "models": ["vision-model"],
    })
    assert response.status_code == 200, response.text
    return response.json()


def scanned_pdf(page_count):
    writer = PdfWriter()
    for _ in range(page_count):
        writer.add_blank_page(width=600, height=800)
    raw = BytesIO()
    writer.write(raw)
    return raw.getvalue()


def upload_scanned(client, page_count=31, name="31页扫描件.pdf"):
    response = client.post("/api/documents", files={
        "file": (name, scanned_pdf(page_count), "application/pdf"),
    })
    assert response.status_code == 200, response.text
    return response.json()


def install_fake_vision(monkeypatch, texts, failures=()):
    """替换逐页渲染与视觉模型，把页码放进图片部件，便于按页返回或抛错。"""
    def fake_render(pdf, page_from, page_to):
        return [(page, {"mime": "image/jpeg", "data": "mock", "page": page})
                for page in range(page_from, page_to + 1)]

    async def fake_call(provider, model, prompt, images=None, json_output=False, token_limit=5000, **kwargs):
        page = images[0]["page"]
        if page in set(failures):
            raise ValueError(f"第 {page} 页视觉模型超时")
        return {
            "content": texts.get(page, f"第 {page} 页识别文本"),
            "model": model, "provider_name": "mock", "usage": {},
        }

    monkeypatch.setattr(notes_agent, "_render_pdf_pages_to_images", fake_render)
    monkeypatch.setattr(ai, "_call_model", fake_call)


def ocr(client, provider, document_id, page_from, page_to):
    response = client.post(f"/api/documents/{document_id}/ocr", json={
        "provider_id": provider["id"], "model": "vision-model",
        "page_from": page_from, "page_to": page_to, "confirmed": True,
    })
    assert response.status_code == 200, response.text
    return response.json()


def document(client, document_id):
    items = client.get("/api/documents").json()["items"]
    return next(item for item in items if item["id"] == document_id)


def stored_pages(document_id):
    with db.connection() as con:
        raw = con.execute("SELECT ocr_pages_json FROM ai_documents WHERE id=?", (document_id,)).fetchone()[0]
    return json.loads(raw)


def stored_chunks(document_id):
    with db.connection() as con:
        raw = con.execute("SELECT chunks_json FROM ai_documents WHERE id=?", (document_id,)).fetchone()[0]
    return json.loads(raw)


def test_non_sequential_batches_keep_every_page(client, monkeypatch):
    provider = save_provider(client)
    uploaded = upload_scanned(client)
    assert uploaded["status"] == "needs_ocr"
    assert uploaded["coverage"]["state_counts"]["pending"] == 31
    install_fake_vision(monkeypatch, {1: "第 1 页正文", 31: "第 31 页正文"})

    # 先写第 31 页：单页写入不能提前进入 ready；随后补第 1 页，非顺序批次两页都要保留。
    first = ocr(client, provider, uploaded["id"], 31, 31)
    assert first["document"]["coverage"]["covered_pages"] == [31]
    assert first["document"]["status"] == "partial"

    second = ocr(client, provider, uploaded["id"], 1, 1)
    coverage = second["document"]["coverage"]
    assert coverage["covered_pages"] == [1, 31]
    assert coverage["state_counts"]["recognized"] == 2
    assert coverage["missing_count"] == 29
    assert second["document"]["status"] == "partial"

    # 第 1 页与第 31 页的非顺序批次都要留在结构块里，绝不只剩最后一页。
    pages = {chunk["page"] for chunk in stored_chunks(uploaded["id"])}
    assert pages == {1, 31}
    stored = stored_pages(uploaded["id"])
    assert stored["pages"]["1"]["text"] == "第 1 页正文"
    assert stored["pages"]["31"]["text"] == "第 31 页正文"
    assert stored["pages"]["1"]["state"] == "recognized"
    assert stored["pages"]["31"]["state"] == "recognized"
    # 只有真正有结果的页才落盘；未识别页按 pending 计入覆盖度，不会挤掉已识别页。
    assert set(stored["pages"]) == {"1", "31"}
    assert coverage["state_counts"]["pending"] == 29


def test_repeated_page_submission_keeps_single_entry(client, monkeypatch):
    provider = save_provider(client)
    uploaded = upload_scanned(client, page_count=2)
    install_fake_vision(monkeypatch, {1: "旧识别文本"})
    assert ocr(client, provider, uploaded["id"], 1, 1)["document"]["coverage"]["covered_count"] == 1
    install_fake_vision(monkeypatch, {1: "重新识别后的文本"})
    result = ocr(client, provider, uploaded["id"], 1, 1)

    stored = stored_pages(uploaded["id"])
    assert stored["pages"]["1"]["text"] == "重新识别后的文本"
    assert stored["pages"]["1"]["state"] == "recognized"
    assert sum(1 for entry in stored["pages"].values() if entry["state"] == "recognized") == 1
    assert [chunk["page"] for chunk in stored_chunks(uploaded["id"])] == [1]
    assert result["document"]["coverage"]["covered_count"] == 1
    # 内容确实被替换，资料版本才自增。
    assert result["document"]["doc_version"] == 2


def test_single_page_failure_keeps_batch_and_can_retry(client, monkeypatch):
    provider = save_provider(client)
    uploaded = upload_scanned(client, page_count=3)
    install_fake_vision(monkeypatch, {1: "第 1 页", 3: "第 3 页"}, failures={2})

    result = ocr(client, provider, uploaded["id"], 1, 3)
    assert result["failed_pages"] == [2]
    assert result["document"]["coverage"]["failed_pages"] == [2]
    assert result["document"]["status"] == "partial"
    assert result["document"]["coverage"]["state_counts"]["recognized"] == 2
    assert result["document"]["coverage"]["state_counts"]["failed"] == 1
    # 失败页记录 failed 与错误信息，其余页照常保存。
    stored = stored_pages(uploaded["id"])
    assert stored["pages"]["2"]["state"] == "failed"
    assert "超时" in stored["pages"]["2"]["error"]
    assert stored["pages"]["1"]["state"] == "recognized"
    assert stored["pages"]["3"]["state"] == "recognized"
    assert {chunk["page"] for chunk in stored_chunks(uploaded["id"])} == {1, 3}
    assert "只重试失败页" in result["notice"]

    # 同一 /ocr 接口只传失败页范围即可单页重试。
    install_fake_vision(monkeypatch, {2: "第 2 页补识别"})
    retry = ocr(client, provider, uploaded["id"], 2, 2)
    assert retry["failed_pages"] == []
    assert retry["document"]["coverage"]["failed_pages"] == []
    assert retry["document"]["coverage"]["complete"] is True
    assert retry["document"]["status"] == "ready"
    assert stored_pages(uploaded["id"])["pages"]["2"]["state"] == "recognized"


def test_reviewed_page_is_preserved_and_prompts_review(client, monkeypatch):
    provider = save_provider(client)
    uploaded = upload_scanned(client, page_count=2)
    install_fake_vision(monkeypatch, {1: "机器识别文本", 2: "第二页文本"})
    ocr(client, provider, uploaded["id"], 1, 2)

    reviewed = client.patch(f"/api/documents/{uploaded['id']}/ocr-text", json={
        "pages": [{"page": 1, "text": "人工校对后的公式 $a+b=c$"}],
    })
    assert reviewed.status_code == 200, reviewed.text
    assert reviewed.json()["coverage"]["state_counts"]["reviewed"] == 1
    assert reviewed.json()["doc_version"] == 2

    # 重跑识别前先提示该页已人工校对。
    preview = client.post(f"/api/documents/{uploaded['id']}/ocr", json={
        "provider_id": provider["id"], "model": "vision-model",
        "page_from": 1, "page_to": 1, "confirmed": False,
    })
    assert preview.status_code == 200
    assert preview.json()["reviewed_pages"] == [1]
    assert "人工校对" in preview.json()["notice"]

    install_fake_vision(monkeypatch, {1: "新的机器识别结果"})
    result = ocr(client, provider, uploaded["id"], 1, 1)
    assert result["preserved_pages"] == [1]
    assert "保留人工校对内容" in result["notice"]
    assert result["document"]["coverage"]["reviewed_pages"] == [1]
    stored = stored_pages(uploaded["id"])
    assert stored["pages"]["1"]["text"] == "人工校对后的公式 $a+b=c$"
    assert stored["pages"]["1"]["state"] == "reviewed"
    # 人工校对页的内容与状态未被改写，版本也不再自增。
    assert result["document"]["doc_version"] == 2
    assert any("人工校对后的公式" in chunk["text"] for chunk in stored_chunks(uploaded["id"]))


def test_status_partial_until_every_page_has_text(client, monkeypatch):
    provider = save_provider(client)
    uploaded = upload_scanned(client, page_count=3)
    assert uploaded["status"] == "needs_ocr"
    assert uploaded["coverage"]["missing_pages"] == [1, 2, 3]
    assert uploaded["coverage"]["complete"] is False
    assert uploaded["ocr_range"]["page_from"] == 1
    assert uploaded["ocr_range"]["page_to"] == 3

    install_fake_vision(monkeypatch, {})
    ocr(client, provider, uploaded["id"], 1, 1)
    partial = document(client, uploaded["id"])
    assert partial["status"] == "partial"
    assert partial["coverage"]["covered_pages"] == [1]
    assert partial["coverage"]["missing_pages"] == [2, 3]

    ocr(client, provider, uploaded["id"], 2, 3)
    ready = document(client, uploaded["id"])
    assert ready["status"] == "ready"
    assert ready["coverage"]["complete"] is True
    assert ready["coverage"]["missing_pages"] == []
    assert ready["coverage"]["failed_pages"] == []


def test_ready_uses_configured_recognition_range(client, monkeypatch):
    provider = save_provider(client)
    uploaded = upload_scanned(client, page_count=4)
    with db.connection() as con:
        con.execute("UPDATE ai_documents SET ocr_range_json=? WHERE id=?", (
            json.dumps({"page_from": 1, "page_to": 2, "updated_at": db.utcnow()}), uploaded["id"],
        ))
    install_fake_vision(monkeypatch, {})
    ocr(client, provider, uploaded["id"], 1, 2)
    payload = document(client, uploaded["id"])
    assert payload["coverage"]["page_count"] == 2
    assert payload["coverage"]["complete"] is True
    assert payload["status"] == "ready"


def test_first_ocr_writes_recognition_range_when_missing(client, monkeypatch):
    provider = save_provider(client)
    uploaded = upload_scanned(client, page_count=3)
    with db.connection() as con:
        con.execute("UPDATE ai_documents SET ocr_range_json='{}' WHERE id=?", (uploaded["id"],))
    # 未配置范围时按全部页考虑。
    assert document(client, uploaded["id"])["ocr_range"]["page_to"] == 3

    install_fake_vision(monkeypatch, {})
    result = ocr(client, provider, uploaded["id"], 2, 2)
    with db.connection() as con:
        stored_range = json.loads(
            con.execute("SELECT ocr_range_json FROM ai_documents WHERE id=?", (uploaded["id"],)).fetchone()[0]
        )
    assert stored_range["page_from"] == 1
    assert stored_range["page_to"] == 3
    assert stored_range["updated_at"]
    assert result["document"]["coverage"]["covered_pages"] == [2]
    assert result["document"]["status"] == "partial"


def test_text_layer_pages_are_covered_without_ocr(client):
    response = client.post("/api/documents", files={
        "file": ("讲义.txt", "第一章 函数\n函数是两个集合之间的对应关系。".encode(), "text/plain"),
    })
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["status"] == "ready"
    assert payload["doc_version"] == 1
    coverage = payload["coverage"]
    assert coverage["complete"] is True
    assert coverage["covered_pages"] == [1]
    assert coverage["state_counts"]["text"] == 1
    assert coverage["state_counts"]["pending"] == 0
    for key in ("pages", "covered_pages", "missing_pages", "failed_pages", "state_counts", "complete"):
        assert key in coverage
    assert payload["ocr_range"]["page_from"] == 1
    assert payload["ocr_range"]["page_to"] == 1


def test_legacy_page_list_is_still_readable(client, monkeypatch):
    provider = save_provider(client)
    uploaded = upload_scanned(client, page_count=3)
    legacy_chunks = [{"page": 1, "chunk_id": "p1-c1", "heading_path": "", "text": "旧版第 1 页文本"}]
    with db.connection() as con:
        con.execute(
            "UPDATE ai_documents SET ocr_pages_json=?, chunks_json=?, status='ready' WHERE id=?",
            (json.dumps([{"page": 1, "text": "旧版第 1 页文本"}]), json.dumps(legacy_chunks), uploaded["id"]),
        )

    payload = document(client, uploaded["id"])
    # 旧列表格式只有 1 页文本，不能因为历史 status 就谎报 ready。
    assert payload["status"] == "partial"
    assert payload["coverage"]["covered_pages"] == [1]
    assert payload["coverage"]["state_counts"]["recognized"] == 1
    assert payload["coverage"]["missing_pages"] == [2, 3]
    assert notes_agent._normalize_ocr_pages([{"page": 1, "text": "x"}])["pages"]["1"]["state"] == "recognized"

    # 继续识别新页时，旧列表里的第 1 页文本必须保留。
    install_fake_vision(monkeypatch, {3: "第 3 页文本"})
    result = ocr(client, provider, uploaded["id"], 3, 3)
    assert result["document"]["coverage"]["covered_pages"] == [1, 3]
    stored = stored_pages(uploaded["id"])
    assert stored["pages"]["1"]["text"] == "旧版第 1 页文本"
    assert stored["pages"]["3"]["text"] == "第 3 页文本"
    assert stored["pages"]["1"]["state"] == "recognized"
    assert stored["pages"]["3"]["state"] == "recognized"
    assert "旧版第 1 页文本" in "".join(chunk["text"] for chunk in stored_chunks(uploaded["id"]))


def test_missing_pages_are_capped_at_200():
    coverage = notes_agent._coverage_fields(250, {"page_from": 1, "page_to": 250}, {}, set())
    assert coverage["pages"] == 250
    assert len(coverage["missing_pages"]) == 200
    assert coverage["missing_count"] == 250
    assert coverage["complete"] is False


def test_note_records_document_version_and_keeps_it_after_replacement(client):
    uploaded = client.post("/api/documents", files={
        "file": ("资料.txt", "知识点与公式\n第二段内容".encode(), "text/plain"),
    }).json()
    note = client.post("/api/notes", json={
        "title": "资料笔记", "content": "人工整理的笔记", "document_id": uploaded["id"],
    })
    assert note.status_code == 200, note.text
    assert note.json()["source_versions"] == {str(uploaded["id"]): 1}

    # 资料被替换（人工改写文本）后版本自增，但已有笔记仍指向生成时的版本。
    replaced = client.patch(f"/api/documents/{uploaded['id']}/ocr-text", json={
        "pages": [{"page": 1, "text": "替换后的知识点与公式\n第二段内容"}],
    })
    assert replaced.status_code == 200, replaced.text
    assert replaced.json()["doc_version"] == 2
    assert client.get(f"/api/notes/{note.json()['id']}").json()["source_versions"] == {
        str(uploaded["id"]): 1,
    }
    newer = client.post("/api/notes", json={
        "title": "替换后笔记", "content": "按新版本整理", "document_id": uploaded["id"],
    })
    assert newer.json()["source_versions"] == {str(uploaded["id"]): 2}


def test_ai_note_records_source_versions_at_generation(client, monkeypatch):
    provider = save_provider(client)
    uploaded = client.post("/api/documents", files={
        "file": ("整理资料.txt", "定义与公式\n引用应可定位".encode(), "text/plain"),
    }).json()
    with db.connection() as con:
        job_id = con.execute(
            "INSERT INTO ai_jobs(user_id,task,status,stage,request_json,created_at,updated_at) "
            "VALUES (1,'notes','running','test','{}',?,?)",
            (db.utcnow(), db.utcnow()),
        ).lastrowid

    async def fake_call(provider, model, prompt, *args, **kwargs):
        citations = sorted(set(re.findall(r"\[D\d+:[^\]\s]+\]", prompt)))
        if "先根据资料设计" in prompt:
            content = "## 提纲"
        else:
            content = "\n".join("## 要点 " + citation for citation in citations) or "## 要点"
        return {"content": content, "model": model, "provider_name": "mock", "usage": {}}

    monkeypatch.setattr(ai, "_call_model", fake_call)
    data = ai.JobInput(task="notes", document_id=uploaded["id"],
                       provider_id=provider["id"], model="test-model")
    result = asyncio.run(ai._notes_job(job_id, 1, data, {"id": 1}, None))
    note = client.get(f"/api/notes/{result['note_id']}").json()
    assert note["source_versions"] == {str(uploaded["id"]): 1}


def test_ai_note_is_idempotent_per_job(client, monkeypatch):
    provider = save_provider(client)
    uploaded = client.post("/api/documents", files={
        "file": ("幂等资料.txt", "定义与公式\n引用应可定位".encode(), "text/plain"),
    }).json()
    with db.connection() as con:
        job_id = con.execute(
            "INSERT INTO ai_jobs(user_id,task,status,stage,request_json,created_at,updated_at) "
            "VALUES (1,'notes','running','test','{}',?,?)",
            (db.utcnow(), db.utcnow()),
        ).lastrowid

    def vision_less_call(marker):
        async def fake_call(provider, model, prompt, *args, **kwargs):
            citations = sorted(set(re.findall(r"\[D\d+:[^\]\s]+\]", prompt)))
            if "先根据资料设计" in prompt:
                content = "## 提纲"
            else:
                content = f"## {marker} " + " ".join(citations)
            return {"content": content, "model": model, "provider_name": "mock", "usage": {}}
        return fake_call

    data = ai.JobInput(task="notes", document_id=uploaded["id"],
                       provider_id=provider["id"], model="test-model")
    monkeypatch.setattr(ai, "_call_model", vision_less_call("第一版"))
    first = asyncio.run(ai._notes_job(job_id, 1, data, {"id": 1}, None))
    # 同一任务重试/续跑：复用笔记与 note_id，内容未变时不再追加版本。
    monkeypatch.setattr(ai, "_call_model", vision_less_call("第一版"))
    again = asyncio.run(ai._notes_job(job_id, 1, data, {"id": 1}, None))
    assert again["note_id"] == first["note_id"]
    with db.connection() as con:
        assert con.execute("SELECT COUNT(*) FROM ai_notes WHERE job_id=?", (job_id,)).fetchone()[0] == 1
        assert con.execute(
            "SELECT COUNT(*) FROM note_versions WHERE note_id=?", (first["note_id"],),
        ).fetchone()[0] == 1

    # 内容真的变化时才追加一条版本，仍复用同一份笔记产物。
    monkeypatch.setattr(ai, "_call_model", vision_less_call("第二版"))
    changed = asyncio.run(ai._notes_job(job_id, 1, data, {"id": 1}, None))
    assert changed["note_id"] == first["note_id"]
    with db.connection() as con:
        assert con.execute("SELECT COUNT(*) FROM ai_notes WHERE job_id=?", (job_id,)).fetchone()[0] == 1
        assert con.execute(
            "SELECT COUNT(*) FROM note_versions WHERE note_id=?", (first["note_id"],),
        ).fetchone()[0] == 2
    note = client.get(f"/api/notes/{first['note_id']}").json()
    assert "第二版" in note["content"]
    assert note["source_versions"] == {str(uploaded["id"]): 1}


def test_incomplete_document_cannot_be_summarized(client, monkeypatch):
    provider = save_provider(client)
    uploaded = upload_scanned(client, page_count=2)
    install_fake_vision(monkeypatch, {1: "第 1 页"})
    ocr(client, provider, uploaded["id"], 1, 1)
    with db.connection() as con:
        job_id = con.execute(
            "INSERT INTO ai_jobs(user_id,task,status,stage,request_json,created_at,updated_at) "
            "VALUES (1,'notes','running','test','{}',?,?)",
            (db.utcnow(), db.utcnow()),
        ).lastrowid
    data = ai.JobInput(task="notes", document_id=uploaded["id"],
                       provider_id=provider["id"], model="test-model")
    with pytest.raises(ValueError) as error:
        asyncio.run(ai._notes_job(job_id, 1, data, {"id": 1}, None))
    assert "尚未完成识别" in str(error.value)
    assert "第 2 页" in str(error.value)
