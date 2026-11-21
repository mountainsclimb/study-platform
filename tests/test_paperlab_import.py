"""试卷工坊 · 识别导入 P0 验收：

上传（图片/PDF/DOCX）→ 页图 → ``paper-import`` 任务逐页识别 → 草稿 → 人工编辑/跳过 →
commit 入库 → 题库查询。全部使用 mock AI（``app.ai._call_model``），不联网、不访问真实 Key。

覆盖：上传限额 422、归属隔离 404/401、未知任务 422、识别 JSON 非法重试一次、
取消后保留已完成页、checkpoint 断点续跑、commit 幂等、配图 bbox 裁切、纯文本 DOCX 页。
"""
import asyncio
from io import BytesIO
import json

from docx import Document
from fastapi.testclient import TestClient
from PIL import Image
from pypdf import PdfWriter
import pytest

from app import ai, db
from app.ai import jobs as ai_jobs
from app.main import app
from app.paperlab import digitize

VISION_MODEL = "vision-model"

PAGE_ONE = {
    "page_inventory": {"question_count": 3, "skip_count": 1},
    "questions": [
        {
            "page_no": 1,
            "bbox": {"x": 0.05, "y": 0.05, "w": 0.9, "h": 0.2},
            "type": "SINGLE",
            "stem": "已知函数 $f(x)=x^2$，则 $f(2)$ 等于？",
            "options": [{"label": "A", "text": "2"}, {"label": "B", "text": "4"}],
            "answers": ["B"],
            "explanation": "代入得 4。",
            "rubric": "",
            "knowledge_tags": ["函数"],
            "difficulty": 0.3,
            "score": 5,
            "images": [{"bbox": {"x": 0.1, "y": 0.3, "w": 0.2, "h": 0.1}, "role": "stem"}],
            "confidence": 0.9,
        },
        {
            "page_no": 1,
            "bbox": {"x": 0.05, "y": 0.3, "w": 0.9, "h": 0.3},
            "type": "SUBJECTIVE",
            "stem": "证明勾股定理。",
            "options": [],
            "answers": [],
            "explanation": "",
            "rubric": "",
            "knowledge_tags": ["几何"],
            "difficulty": 0.5,
            "score": 10,
            "images": [],
            "confidence": 0.4,
        },
    ],
    "skipped": [
        {
            "page_no": 1,
            "bbox": {"x": 0.1, "y": 0.7, "w": 0.5, "h": 0.1},
            "reason": "字迹模糊无法辨认",
        }
    ],
}

PAGE_TWO = {
    "page_inventory": {"question_count": 1, "skip_count": 0},
    "questions": [
        {
            "page_no": 2,
            "bbox": {"x": 0.1, "y": 0.1, "w": 0.8, "h": 0.4},
            "type": "BLANK",
            "stem": "计算：$1+1=$____。",
            "options": [],
            "answers": ["2"],
            "explanation": "直接相加得 2。",
            "rubric": "",
            "knowledge_tags": ["有理数"],
            "difficulty": 0.1,
            "score": 3,
            "images": [],
            "confidence": 0.95,
        }
    ],
    "skipped": [],
}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def save_provider(client, name="试卷识别测试连接"):
    response = client.post("/api/providers", json={
        "name": name, "kind": "openai", "base_url": "https://api.openai.com/v1",
        "api_key": "sk-paperlab-unit-test", "models": [VISION_MODEL],
    })
    assert response.status_code == 200, response.text
    return response.json()


def png_bytes(width=800, height=1200, color=(255, 255, 255)):
    image = Image.new("RGB", (width, height), color)
    buffer = BytesIO()
    image.save(buffer, "PNG")
    return buffer.getvalue()


def pdf_bytes(page_count=2, width=600, height=800):
    writer = PdfWriter()
    for _ in range(page_count):
        writer.add_blank_page(width=width, height=height)
    buffer = BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def docx_bytes(paragraphs=("第一题 已知函数 f(x)=x^2，求 f(2)。",)):
    document = Document()
    for text in paragraphs:
        document.add_paragraph(text)
    buffer = BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def docx_with_image_bytes():
    from docx.shared import Inches

    document = Document()
    document.add_paragraph("1. 如图，求阴影部分的面积。")
    document.add_picture(BytesIO(png_bytes(300, 200)), width=Inches(2))
    buffer = BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def upload(client, files, **form):
    return client.post(
        "/api/paperlab/imports",
        files=[("files", (name, data, media)) for name, data, media in files],
        data=form,
    )


def upload_png(client, count=1, **form):
    response = upload(
        client,
        [(f"第{index + 1}页.png", png_bytes(), "image/png") for index in range(count)],
        **form,
    )
    assert response.status_code == 200, response.text
    return response.json()["import"]


def user_id(client):
    return client.get("/api/auth/me").json()["id"]


def provider_row(client, provider):
    return ai._selected_provider(user_id(client), provider["id"], VISION_MODEL)


def insert_import_job(client, import_id, page_from=1, page_to=None, provider_id=None):
    uid = user_id(client)
    provider_id = provider_id or save_provider(client)["id"]
    request = {
        "task": "paper-import", "provider_id": provider_id, "model": VISION_MODEL,
        "params": {
            "import_id": import_id,
            "page_from": page_from,
            "page_to": page_to if page_to is not None else page_from,
        },
    }
    now = db.utcnow()
    with db.connection() as con:
        job_id = con.execute(
            "INSERT INTO ai_jobs(user_id,task,status,stage,request_json,partial_content,"
            "cancelled,created_at,updated_at,dispatch_token) VALUES (?,?,?,?,?,'',0,?,?,?)",
            (uid, "paper-import", "queued", "queued", json.dumps(request, ensure_ascii=False),
             now, now, "import-token"),
        ).lastrowid
    return job_id, ai_jobs.JobInput(**request), provider_row(client, {"id": provider_id})


def run_import_job(job_id, user, data, provider):
    return asyncio.run(digitize.run_import_job(job_id, user["id"], data, provider))


def import_detail(client, import_id):
    response = client.get(f"/api/paperlab/imports/{import_id}")
    assert response.status_code == 200, response.text
    return response.json()


def job_row(job_id):
    with db.connection() as con:
        return dict(con.execute("SELECT * FROM ai_jobs WHERE id=?", (job_id,)).fetchone())


def invalid_page():
    payload = json.loads(json.dumps(PAGE_ONE, ensure_ascii=False))
    payload["page_inventory"]["question_count"] = 99
    return payload


class FakeVision:
    """可观察的假视觉模型：按顺序返回预设 JSON，可注入取消回调 / 失败。"""

    def __init__(self, responses=None, on_call=None):
        self.responses = list(responses or [])
        self.on_call = on_call
        self.calls = []

    async def call(self, provider, model, prompt, images=None, json_output=False,
                   token_limit=5000, **kwargs):
        index = len(self.calls)
        self.calls.append({
            "prompt": prompt, "images": images, "json_output": json_output, "kwargs": kwargs,
        })
        if self.on_call:
            self.on_call(index, prompt)
        if self.responses:
            item = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        else:
            item = PAGE_ONE
        if isinstance(item, Exception):
            raise item
        if callable(item):
            item = item(prompt, images)
        content = item if isinstance(item, str) else json.dumps(item, ensure_ascii=False)
        return {"content": content, "model": model, "provider_name": "mock", "usage": {}}

    def install(self, monkeypatch):
        monkeypatch.setattr(ai, "_call_model", self.call)
        return self


async def no_dispatch(job_id, token, runner):
    return None


def patch_dispatch(monkeypatch):
    monkeypatch.setattr(ai_jobs, "_dispatch", no_dispatch)


def accept_all(client, import_id):
    detail = import_detail(client, import_id)
    accepted = []
    for item in detail["items"]:
        if item["status"] == "skipped":
            continue
        response = client.patch(
            f"/api/paperlab/imports/{import_id}/items/{item['id']}",
            json={"status": "accepted"},
        )
        assert response.status_code == 200, response.text
        accepted.append(item["id"])
    return accepted


# ---------------------------------------------------------------------------
# 1. 上传
# ---------------------------------------------------------------------------

def test_upload_png_creates_pages_and_serves_image(client):
    imported = upload_png(client, title="测试数学卷", subject="数学", grade="高中一年级")
    assert imported["status"] == "draft"
    assert imported["page_count"] == 1
    assert imported["title"] == "测试数学卷"
    page = imported["pages"][0]
    assert page["page_no"] == 1
    assert page["width"] > 0 and page["height"] > 0
    assert page["image_url"] == f"/api/paperlab/imports/{imported['id']}/pages/1/image"

    detail = import_detail(client, imported["id"])
    assert detail["pages"][0]["page_no"] == 1
    assert detail["items"] == []
    assert detail["counts"]["total"] == 0

    image = client.get(page["image_url"])
    assert image.status_code == 200
    assert image.headers["content-type"].startswith("image/jpeg")
    assert image.headers["cache-control"].startswith("private")
    assert image.content


def test_upload_multi_page_pdf_renders_each_page(client):
    response = upload(client, [("试卷.pdf", pdf_bytes(3), "application/pdf")], subject="数学")
    assert response.status_code == 200, response.text
    imported = response.json()["import"]
    assert imported["page_count"] == 3
    assert [page["page_no"] for page in imported["pages"]] == [1, 2, 3]
    for page in imported["pages"]:
        assert page["width"] > 0 and page["height"] > 0
        image = client.get(page["image_url"])
        assert image.status_code == 200, image.text
        assert image.content


def test_upload_text_only_docx_creates_text_page(client):
    response = upload(
        client,
        [("讲义.docx", docx_bytes(("1. 已知函数 f(x)=x^2，求 f(2) 的值。",)), "application/vnd.openxmlformats-officedocument.wordprocessingml.document")],
        subject="数学",
    )
    assert response.status_code == 200, response.text
    imported = response.json()["import"]
    assert imported["page_count"] == 1
    page = imported["pages"][0]
    assert page["has_text"] is True
    assert page["image_url"] is None
    assert client.get(f"/api/paperlab/imports/{imported['id']}/pages/1/image").status_code == 404


def test_upload_docx_with_embedded_image_creates_image_page(client):
    response = upload(
        client,
        [("图文讲义.docx", docx_with_image_bytes(),
          "application/vnd.openxmlformats-officedocument.wordprocessingml.document")],
    )
    assert response.status_code == 200, response.text
    imported = response.json()["import"]
    assert imported["page_count"] == 1
    page = imported["pages"][0]
    assert page["image_url"] is not None
    assert page["width"] > 0 and page["height"] > 0
    # DOCX 文字作为首页识别参考，仍保留
    assert page["has_text"] is True
    assert client.get(page["image_url"]).status_code == 200


def test_upload_rejects_unsupported_format(client):
    response = upload(client, [("笔记.txt", b"hello", "text/plain")])
    assert response.status_code == 422
    assert "不支持的文件格式" in response.json()["detail"]


def test_upload_rejects_too_many_files(client):
    files = [(f"p{index}.png", png_bytes(40, 40), "image/png") for index in range(21)]
    response = upload(client, files)
    assert response.status_code == 422
    assert "最多上传 20 个文件" in response.json()["detail"]


def test_upload_rejects_pdf_over_page_limit(client):
    response = upload(client, [("大题量.pdf", pdf_bytes(21), "application/pdf")])
    assert response.status_code == 422
    assert "最多 20 页" in response.json()["detail"]


def test_upload_rejects_oversized_file(client, monkeypatch):
    monkeypatch.setattr(digitize, "MAX_FILE_BYTES", 1024 * 1024)
    response = upload(client, [("大图.png", b"0" * (1024 * 1024 + 1), "image/png")])
    assert response.status_code == 422
    assert "1MB" in response.json()["detail"]


def test_upload_requires_login(client):
    anonymous = TestClient(app)
    try:
        assert anonymous.get("/api/paperlab/imports").status_code == 401
        assert anonymous.get("/api/paperlab/meta").status_code == 401
    finally:
        anonymous.close()


def test_import_owner_isolation(client, second_client):
    imported = upload_png(client)
    import_id = imported["id"]
    assert second_client.get(f"/api/paperlab/imports/{import_id}").status_code == 404
    assert second_client.get(
        f"/api/paperlab/imports/{import_id}/pages/1/image"
    ).status_code == 404
    assert second_client.post(
        f"/api/paperlab/imports/{import_id}/commit", json={}
    ).status_code == 404


# ---------------------------------------------------------------------------
# 2. 触发识别任务（HTTP 层）
# ---------------------------------------------------------------------------

def test_recognize_creates_paper_import_job(client, monkeypatch):
    patch_dispatch(monkeypatch)
    imported = upload_png(client)
    provider = save_provider(client)
    response = client.post(
        f"/api/paperlab/imports/{imported['id']}/recognize",
        json={"provider_id": provider["id"], "model": VISION_MODEL},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    job = payload["job"]
    assert job["task"] == "paper-import"
    assert job["status"] == "queued"
    assert payload["import_id"] == imported["id"]
    assert "dispatch_token" not in job
    record = job_row(job["id"])
    request = json.loads(record["request_json"])
    assert request["params"] == {
        "import_id": imported["id"], "page_from": 1, "page_to": 1,
    }
    assert import_detail(client, imported["id"])["import"]["status"] == "recognizing"


def test_recognize_rejects_bad_range_and_unknown_provider(client, monkeypatch):
    patch_dispatch(monkeypatch)
    imported = upload_png(client)
    provider = save_provider(client)
    bad_range = client.post(
        f"/api/paperlab/imports/{imported['id']}/recognize",
        json={"page_from": 3, "page_to": 3, "provider_id": provider["id"], "model": VISION_MODEL},
    )
    assert bad_range.status_code == 422
    assert "起始页" in bad_range.json()["detail"]
    missing = client.post(
        f"/api/paperlab/imports/{imported['id']}/recognize",
        json={"provider_id": 9999, "model": VISION_MODEL},
    )
    assert missing.status_code == 404
    unknown_import = client.post(
        "/api/paperlab/imports/9999/recognize",
        json={"provider_id": provider["id"], "model": VISION_MODEL},
    )
    assert unknown_import.status_code == 404
    no_provider = client.post(
        f"/api/paperlab/imports/{imported['id']}/recognize",
        json={"model": VISION_MODEL},
    )
    assert no_provider.status_code == 422


def test_unknown_task_type_rejected_422(client, monkeypatch):
    patch_dispatch(monkeypatch)
    provider = save_provider(client)
    response = client.post("/api/ai/jobs", json={
        "task": "paper-unknown", "provider_id": provider["id"], "model": VISION_MODEL,
    })
    assert response.status_code == 422
    detail = response.json()["detail"]
    text = detail if isinstance(detail, str) else json.dumps(detail, ensure_ascii=False)
    assert "未知任务类型" in text


# ---------------------------------------------------------------------------
# 3. 任务执行器：逐页草稿 / 重试 / 取消 / 断点续跑
# ---------------------------------------------------------------------------

def test_executor_writes_drafts_and_crops_images(client, monkeypatch):
    imported = upload_png(client, subject="数学", grade="高中一年级")
    fake = FakeVision([PAGE_ONE]).install(monkeypatch)
    job_id, data, provider = insert_import_job(client, imported["id"])
    result = run_import_job(job_id, {"id": user_id(client)}, data, provider)

    assert result["status"] == "ready"
    assert result["done_pages"] == [1]
    assert result["failed_pages"] == []
    assert fake.calls and fake.calls[0]["images"], "视觉识别必须带上页面图"
    assert job_row(job_id)["status"] != "failed"

    detail = import_detail(client, imported["id"])
    items = detail["items"]
    assert [item["status"] for item in items] == ["pending", "pending", "skipped"]
    first, second, skipped = items
    assert first["draft"]["stem"].startswith("已知函数")
    assert first["draft"]["type"] == "SINGLE"
    assert first["draft"]["answers"] == ["B"]
    assert first["question_id"] is None
    assert first["needs_review"] is False
    # 配图按 bbox 裁切并保存，草稿可预览
    assert first["draft"]["images"][0]["asset"]
    assert second["needs_review"] is True
    assert "置信度" in second["review_reason"]
    assert skipped["skip_reason"] == "字迹模糊无法辨认"
    assert detail["pages"][0]["status"] == "recognized"
    assert detail["import"]["status"] == "ready"
    assert detail["counts"] == {
        "total": 3, "pending": 2, "accepted": 0, "skipped": 1,
        # 跳过的题也要人工看一眼（needs_review=2：低置信度 + 跳过）
        "needs_review": 2, "committed": 0,
    }


def test_executor_retries_invalid_json_once(client, monkeypatch):
    imported = upload_png(client)
    fake = FakeVision([invalid_page(), PAGE_ONE]).install(monkeypatch)
    job_id, data, provider = insert_import_job(client, imported["id"])
    result = run_import_job(job_id, {"id": user_id(client)}, data, provider)

    assert len(fake.calls) == 2
    assert "格式修正" in fake.calls[1]["prompt"]
    assert result["status"] in {"ready", "failed"}
    items = import_detail(client, imported["id"])["items"]
    assert len(items) == 3


def test_executor_keeps_other_pages_when_one_page_invalid_twice(client, monkeypatch):
    response = upload(client, [("试卷.pdf", pdf_bytes(2), "application/pdf")])
    imported = response.json()["import"]
    fake = FakeVision([invalid_page(), invalid_page(), PAGE_ONE, PAGE_ONE]).install(monkeypatch)
    job_id, data, provider = insert_import_job(
        client, imported["id"], page_from=1, page_to=2,
    )
    result = run_import_job(job_id, {"id": user_id(client)}, data, provider)

    assert fake.calls
    assert result["failed_pages"] == [1]
    assert result["done_pages"] == [2]
    assert result["status"] == "ready"
    assert "识别失败" in result["notice"]
    detail = import_detail(client, imported["id"])
    assert detail["import"]["status"] == "ready"
    assert len(detail["items"]) == 3
    assert all(item["page_no"] == 2 for item in detail["items"])


def test_executor_cancel_keeps_done_pages_then_resume(client, monkeypatch):
    response = upload(client, [("试卷.pdf", pdf_bytes(2), "application/pdf")])
    imported = response.json()["import"]
    job_id, data, provider = insert_import_job(
        client, imported["id"], page_from=1, page_to=2,
    )

    def cancel_after_first(index, prompt):
        if index == 0:
            with db.connection() as con:
                con.execute("UPDATE ai_jobs SET cancelled=1 WHERE id=?", (job_id,))

    fake = FakeVision([PAGE_ONE, PAGE_TWO], on_call=cancel_after_first).install(monkeypatch)
    result = run_import_job(job_id, {"id": user_id(client)}, data, provider)
    assert result["cancelled"] is True
    assert result["done_pages"] == [1]
    assert job_row(job_id)["status"] == "cancelled"

    detail = import_detail(client, imported["id"])
    assert len(detail["items"]) == 3
    assert all(item["page_no"] == 1 for item in detail["items"])

    # 断点续跑：只重试第 2 页（第 1 页草稿保留）
    with db.connection() as con:
        con.execute("UPDATE ai_jobs SET status='queued', cancelled=0 WHERE id=?", (job_id,))
    fake2 = FakeVision([PAGE_TWO]).install(monkeypatch)
    resumed = run_import_job(job_id, {"id": user_id(client)}, data, provider)
    assert len(fake2.calls) == 1
    assert "第 2 页" in fake2.calls[0]["prompt"]
    assert resumed["done_pages"] == [1, 2]
    items = import_detail(client, imported["id"])["items"]
    assert {item["page_no"] for item in items} == {1, 2}
    assert len(items) == 4


def test_executor_uses_text_prompt_for_docx_text_page(client, monkeypatch):
    response = upload(client, [("讲义.docx", docx_bytes(), "application/vnd.openxmlformats-officedocument.wordprocessingml.document")])
    imported = response.json()["import"]
    fake = FakeVision([PAGE_ONE]).install(monkeypatch)
    job_id, data, provider = insert_import_job(client, imported["id"])
    result = run_import_job(job_id, {"id": user_id(client)}, data, provider)

    assert result["status"] == "ready"
    assert fake.calls[0]["images"] is None
    assert "已知函数 f(x)=x^2" in fake.calls[0]["prompt"]
    assert len(import_detail(client, imported["id"])["items"]) == 3


# ---------------------------------------------------------------------------
# 4. 人工编辑 / commit
# ---------------------------------------------------------------------------

def test_item_patch_bbox_skip_restore_and_validation(client, monkeypatch):
    imported = upload_png(client)
    fake = FakeVision([PAGE_ONE]).install(monkeypatch)
    job_id, data, provider = insert_import_job(client, imported["id"])
    run_import_job(job_id, {"id": user_id(client)}, data, provider)
    item = import_detail(client, imported["id"])["items"][0]
    url = f"/api/paperlab/imports/{imported['id']}/items/{item['id']}"

    invalid = client.patch(url, json={"bbox": {"x": 2, "y": 0, "w": 0.1, "h": 0.1}})
    assert invalid.status_code == 422

    updated = client.patch(url, json={"bbox": {"x": 0.2, "y": 0.1, "w": 0.5, "h": 0.3}})
    assert updated.status_code == 200, updated.text
    patched = updated.json()["item"]
    assert patched["bbox"] == {"x": 0.2, "y": 0.1, "w": 0.5, "h": 0.3}
    assert patched["source"] == "manual"

    # 人工改写题干/答案（draft 局部覆盖）
    edited = client.patch(url, json={"draft": {
        "stem": "人工改写后的题干：求 $f(2)$。", "answers": ["B"],
    }})
    assert edited.status_code == 200, edited.text
    edited_item = edited.json()["item"]
    assert edited_item["draft"]["stem"].startswith("人工改写后的题干")
    assert edited_item["draft"]["type"] == "SINGLE"
    assert edited_item["draft"]["options"]

    skipped = client.patch(url, json={"status": "skipped", "skip_reason": "题目重复"})
    assert skipped.status_code == 200
    assert skipped.json()["item"]["status"] == "skipped"
    assert skipped.json()["item"]["skip_reason"] == "题目重复"

    restored = client.patch(url, json={"status": "pending"})
    assert restored.status_code == 200
    assert restored.json()["item"]["status"] == "pending"
    assert restored.json()["item"]["skip_reason"] == ""


def test_commit_requires_accepted_and_is_idempotent(client, monkeypatch):
    imported = upload_png(client, subject="数学")
    fake = FakeVision([PAGE_ONE]).install(monkeypatch)
    job_id, data, provider = insert_import_job(client, imported["id"])
    run_import_job(job_id, {"id": user_id(client)}, data, provider)

    empty = client.post(f"/api/paperlab/imports/{imported['id']}/commit", json={})
    assert empty.status_code == 422
    assert "确认" in empty.json()["detail"]

    accepted = accept_all(client, imported["id"])
    assert len(accepted) == 2
    first = client.post(f"/api/paperlab/imports/{imported['id']}/commit", json={})
    assert first.status_code == 200, first.text
    created = first.json()["created_question_ids"]
    assert len(created) == 2
    assert first.json()["import_status"] == "committed"

    bank = client.get("/api/paperlab/questions")
    assert bank.json()["total"] == 2

    # 重复 commit（含显式 item_ids）不重复建题
    second = client.post(
        f"/api/paperlab/imports/{imported['id']}/commit", json={"item_ids": accepted},
    )
    assert second.status_code == 200
    assert second.json()["created_question_ids"] == []
    assert set(second.json()["existing_question_ids"]) == set(created)
    assert client.get("/api/paperlab/questions").json()["total"] == 2

    # 已入库草稿不可再改
    detail = import_detail(client, imported["id"])
    locked = client.patch(
        f"/api/paperlab/imports/{imported['id']}/items/{detail['items'][0]['id']}",
        json={"draft": {"stem": "改写题干"}},
    )
    assert locked.status_code == 409


def test_commit_rejects_unsupported_target(client, monkeypatch):
    imported = upload_png(client)
    fake = FakeVision([PAGE_ONE]).install(monkeypatch)
    job_id, data, provider = insert_import_job(client, imported["id"])
    run_import_job(job_id, {"id": user_id(client)}, data, provider)
    accept_all(client, imported["id"])
    response = client.post(
        f"/api/paperlab/imports/{imported['id']}/commit", json={"target": "paper"},
    )
    assert response.status_code == 422
    assert "组卷功能尚未开放" in response.json()["detail"]


def test_committed_question_images_are_served_from_assets(client, monkeypatch):
    imported = upload_png(client)
    fake = FakeVision([PAGE_ONE]).install(monkeypatch)
    job_id, data, provider = insert_import_job(client, imported["id"])
    run_import_job(job_id, {"id": user_id(client)}, data, provider)
    accept_all(client, imported["id"])
    commit = client.post(f"/api/paperlab/imports/{imported['id']}/commit", json={})
    question_ids = commit.json()["created_question_ids"]
    question = client.get(f"/api/paperlab/questions/{question_ids[0]}").json()["question"]
    images = question["content"]["images"]
    assert images and images[0]["asset"]
    image_url = images[0]["url"]
    response = client.get(image_url)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/png")
    assert response.content


def test_crop_failure_keeps_draft_and_commit_works(client, monkeypatch):
    imported = upload_png(client)
    # 模拟裁切失败（bbox 无法换算为像素框）：只影响裁图，不影响识别本身。
    monkeypatch.setattr(digitize, "_bbox_pixels", lambda bbox, width, height: None)
    fake = FakeVision([PAGE_ONE]).install(monkeypatch)
    job_id, data, provider = insert_import_job(client, imported["id"])
    result = run_import_job(job_id, {"id": user_id(client)}, data, provider)

    assert result["status"] == "ready"
    items = import_detail(client, imported["id"])["items"]
    assert len(items) == 3
    assert any("未裁剪" in message for message in items[0]["warnings"])
    assert items[0]["draft"]["images"][0].get("asset") is None

    # 裁切失败不阻塞入库
    accept_all(client, imported["id"])
    commit = client.post(f"/api/paperlab/imports/{imported['id']}/commit", json={})
    assert commit.status_code == 200, commit.text
    assert len(commit.json()["created_question_ids"]) == 2


def test_import_detail_requires_known_import(client):
    assert client.get("/api/paperlab/imports/9999").status_code == 404
    assert client.get("/api/paperlab/imports/9999/pages/1/image").status_code == 404
