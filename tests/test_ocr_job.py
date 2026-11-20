"""§3.3 长耗时 PDF 批量 OCR 异步任务化验收。

覆盖：>3 页创建 ai_jobs 任务、≤3 页同步、逐页落库、取消保留已保存页、断点续跑、
单页失败/超时隔离、渲染清晰度默认 2.0、任务检查点与进度事件。

不联网、不真实调用视觉模型：_call_model / _render_pdf_pages_to_images 全部替换为假实现。
jobs.submit_job / JobInput 的 OCR 扩展由 stream-core 并行交付；未交付前用
monkeypatch 兜底（契约见需求文档），交付后端点自动走真实 submit_job。
"""
import asyncio
from io import BytesIO
import json
import sys
import types

from pypdf import PdfWriter
import pytest

from app import ai, db
from app.ai import jobs as ai_jobs
from app.ai import notes_agent

OCR_PROVIDER_MODEL = "vision-model"


# ---------------------------------------------------------------------------
# 公共辅助
# ---------------------------------------------------------------------------

def save_provider(client, name="OCR 异步测试连接"):
    response = client.post("/api/providers", json={
        "name": name, "kind": "openai", "base_url": "https://api.openai.com/v1",
        "api_key": "sk-ocr-async-unit-test", "models": [OCR_PROVIDER_MODEL],
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


def upload_scanned(client, page_count, name=None):
    response = client.post("/api/documents", files={
        "file": (name or f"{page_count}页扫描件.pdf", scanned_pdf(page_count), "application/pdf"),
    })
    assert response.status_code == 200, response.text
    return response.json()


def user_id_of(client):
    return client.get("/api/auth/me").json()["id"]


class FakeVision:
    """可观察的假视觉模型：记录调用页、渲染请求，并支持失败/延迟/回调注入。"""

    def __init__(self, texts=None, failures=(), delays=None, on_call=None):
        self.texts = dict(texts or {})
        self.failures = set(failures)
        self.delays = dict(delays or {})
        self.on_call = on_call
        self.calls = []
        self.renders = []

    def install(self, monkeypatch):
        def fake_render(pdf, page_from, page_to):
            self.renders.append((page_from, page_to))
            return [
                (page, {"mime": "image/jpeg", "data": "mock", "page": page})
                for page in range(page_from, page_to + 1)
            ]

        async def fake_call(provider, model, prompt, images=None, json_output=False,
                            token_limit=5000, **kwargs):
            page = images[0]["page"]
            self.calls.append(page)
            if self.on_call:
                self.on_call(page, kwargs)
            if self.delays.get(page):
                await asyncio.sleep(self.delays[page])
            if page in self.failures:
                raise ValueError(f"第 {page} 页视觉模型失败")
            return {
                "content": self.texts.get(page, f"第 {page} 页识别文本"),
                "model": model, "provider_name": "mock", "usage": {},
            }

        monkeypatch.setattr(notes_agent, "_render_pdf_pages_to_images", fake_render)
        monkeypatch.setattr(ai, "_call_model", fake_call)
        return self


class FakeSubmit:
    """假 jobs.submit_job：只记录入参并返回任务公开对象（不发真实调度）。"""

    def __init__(self, start_id=700):
        self.submits = []
        self.next_id = start_id

    def install(self, monkeypatch):
        def submit(user, data):
            payload = data.model_dump() if hasattr(data, "model_dump") else dict(data)
            self.next_id += 1
            self.submits.append({"user": user, "data": payload})
            return {
                "id": self.next_id, "user_id": user.get("id"), "task": payload.get("task"),
                "status": "queued", "stage": "queued", "request": payload,
                "result": None, "has_checkpoint": False, "needs_attention": False,
            }

        monkeypatch.setattr(ai_jobs, "submit_job", submit, raising=False)
        return self


def install_fake_submit(monkeypatch):
    return FakeSubmit().install(monkeypatch)


def capture_publish_events(monkeypatch):
    """捕获 publish_event 推送的 stage 等事件（兼容 stream-core 交付前后的函数名）。"""
    events = []
    target = getattr(ai_jobs, "publish_event", None)
    name = "publish_event"
    if not callable(target):
        target = getattr(ai_jobs, "_emit_stream", None)
        name = "_emit_stream"

    def record(job_id, event, data):
        events.append({"job_id": job_id, "event": event, "data": dict(data or {})})

    monkeypatch.setattr(ai_jobs, name, record)
    return events


def ocr_request(client, provider, document_id, page_from, page_to=None, *,
                confirmed=True, async_job=False):
    payload = {
        "provider_id": provider["id"], "model": OCR_PROVIDER_MODEL,
        "page_from": page_from, "confirmed": confirmed,
    }
    if page_to is not None:
        payload["page_to"] = page_to
    if async_job:
        payload["async_job"] = True
    response = client.post(f"/api/documents/{document_id}/ocr", json=payload)
    assert response.status_code == 200, response.text
    return response.json()


def stored_pages(document_id):
    with db.connection() as con:
        raw = con.execute("SELECT ocr_pages_json FROM ai_documents WHERE id=?", (document_id,)).fetchone()[0]
    return json.loads(raw)


def job_snapshot(job_id):
    with db.connection() as con:
        row = con.execute(
            "SELECT status, stage, cancelled, checkpoint_json, request_json FROM ai_jobs WHERE id=?",
            (job_id,),
        ).fetchone()
    assert row is not None
    snapshot = dict(row)
    snapshot["checkpoint"] = json.loads(snapshot["checkpoint_json"]) if snapshot["checkpoint_json"] else {}
    snapshot["request"] = json.loads(snapshot["request_json"])
    return snapshot


def insert_ocr_job(user_id, *, status="queued", checkpoint=None, dispatch_token="test-token"):
    now = db.utcnow()
    request = {
        "task": "ocr", "document_id": 1, "page_from": 1, "page_to": 1,
        "provider_id": 1, "model": OCR_PROVIDER_MODEL,
    }
    with db.connection() as con:
        return con.execute(
            "INSERT INTO ai_jobs(user_id,task,status,stage,request_json,partial_content,cancelled,"
            "created_at,updated_at,dispatch_token,checkpoint_json) VALUES (?,?,?,?,?,'',0,?,?,?,?)",
            (user_id, "ocr", status, status, json.dumps(request), now, now, dispatch_token,
             json.dumps(checkpoint) if checkpoint is not None else None),
        ).lastrowid


def ocr_job_data(document_id, page_from=1, page_to=1, provider_id=1, model=OCR_PROVIDER_MODEL):
    return notes_agent._ocr_job_input(
        document_id=document_id, page_from=page_from, page_to=page_to,
        provider_id=provider_id, model=model,
    )


def run_ocr_job(job_id, user_id, data, provider):
    return asyncio.run(notes_agent._ocr_job(job_id, user_id, data, provider))


# ---------------------------------------------------------------------------
# 1. 端点分界：>3 页创建异步任务，≤3 页保持同步
# ---------------------------------------------------------------------------

def test_more_than_three_pages_submit_async_job(client, monkeypatch):
    provider = save_provider(client)
    uploaded = upload_scanned(client, page_count=5)
    vision = FakeVision().install(monkeypatch)
    submit = install_fake_submit(monkeypatch)

    # 未确认时先返回页数/费用估算，并告知将走后台任务（此时不得创建任务）。
    preview = ocr_request(client, provider, uploaded["id"], 1, 5, confirmed=False)
    assert preview["requires_confirmation"] is True
    assert preview["estimated_calls"] == 5
    assert preview["async"] is True
    assert submit.submits == []

    result = ocr_request(client, provider, uploaded["id"], 1, 5)
    assert result["async"] is True
    assert result["document_id"] == uploaded["id"]
    assert result["page_from"] == 1
    assert result["page_to"] == 5
    assert result["estimated_calls"] == 5
    assert result["job"]["id"] == submit.next_id
    # 任务参数必须带 ocr / 页范围 / 资料与模型，供 jobs._run_job 派发到 _ocr_job。
    payload = submit.submits[0]["data"]
    assert payload["task"] == "ocr"
    assert payload["document_id"] == uploaded["id"]
    assert payload["page_from"] == 1
    assert payload["page_to"] == 5
    assert payload["provider_id"] == provider["id"]
    assert payload["model"] == OCR_PROVIDER_MODEL
    # 走异步分支时不得同步调用视觉模型、也不得落下任何识别结果。
    assert vision.calls == []
    assert stored_pages(uploaded["id"])["pages"] == {}


def test_three_pages_or_fewer_stay_synchronous(client, monkeypatch):
    provider = save_provider(client)
    uploaded = upload_scanned(client, page_count=3)
    vision = FakeVision({1: "第 1 页正文", 2: "第 2 页正文", 3: "第 3 页正文"}).install(monkeypatch)
    submit = install_fake_submit(monkeypatch)

    result = ocr_request(client, provider, uploaded["id"], 1, 3)
    assert result["async"] is False
    assert submit.submits == []
    assert sorted(vision.calls) == [1, 2, 3]
    assert result["document"]["status"] == "ready"
    assert result["document"]["coverage"]["complete"] is True
    assert {page["page"] for page in result["ocr_pages"]} == {1, 2, 3}
    assert set(stored_pages(uploaded["id"])["pages"]) == {"1", "2", "3"}
    assert result["failed_pages"] == []


def test_explicit_async_flag_for_small_batch(client, monkeypatch):
    provider = save_provider(client)
    uploaded = upload_scanned(client, page_count=1)
    vision = FakeVision().install(monkeypatch)
    submit = install_fake_submit(monkeypatch)

    result = ocr_request(client, provider, uploaded["id"], 1, 1, async_job=True)
    assert result["async"] is True
    assert result["estimated_calls"] == 1
    assert submit.submits[0]["data"]["page_to"] == 1
    assert vision.calls == []


def test_default_page_range_is_capped_at_thirty(client, monkeypatch):
    provider = save_provider(client)
    uploaded = upload_scanned(client, page_count=31)
    submit = install_fake_submit(monkeypatch)

    preview = ocr_request(client, provider, uploaded["id"], 1, None, confirmed=False)
    assert preview["page_to"] == 30
    assert preview["estimated_calls"] == 30
    assert preview["async"] is True
    result = ocr_request(client, provider, uploaded["id"], 1, None)
    assert result["page_to"] == 30
    assert submit.submits[0]["data"]["page_to"] == 30


def test_async_degrades_to_sync_when_submit_missing(client, monkeypatch):
    provider = save_provider(client)
    uploaded = upload_scanned(client, page_count=4)
    vision = FakeVision().install(monkeypatch)
    monkeypatch.delattr(ai_jobs, "submit_job", raising=False)

    result = ocr_request(client, provider, uploaded["id"], 1, 4)
    assert result["async"] is False
    assert result["ocr_pages"]
    assert "后台任务队列暂不可用" in result["notice"]
    assert sorted(vision.calls) == [1, 2, 3, 4]
    assert result["document"]["coverage"]["complete"] is True


# ---------------------------------------------------------------------------
# 2. 同步路径逐页落库与单页失败/超时隔离
# ---------------------------------------------------------------------------

def test_sync_path_persists_each_page_before_next(client, monkeypatch):
    provider = save_provider(client)
    uploaded = upload_scanned(client, page_count=3)
    vision = FakeVision().install(monkeypatch)
    timeline = []
    vision.on_call = lambda page, kwargs: timeline.append(("model", page))

    original_save = notes_agent._save_ocr_pages

    async def tracking_save(document_id, user_id, pages, *, manual=False):
        timeline.extend(("save", int(page["page"])) for page in pages)
        return await original_save(document_id, user_id, pages, manual=manual)

    monkeypatch.setattr(notes_agent, "_save_ocr_pages", tracking_save)

    result = ocr_request(client, provider, uploaded["id"], 1, 3)
    assert result["document"]["coverage"]["complete"] is True
    # 每页模型调用后立刻落库，且下一页的模型调用发生在本页保存之后。
    assert timeline == [
        ("model", 1), ("save", 1),
        ("model", 2), ("save", 2),
        ("model", 3), ("save", 3),
    ]


def test_sync_path_keeps_completed_pages_when_later_save_fails(client, monkeypatch):
    provider = save_provider(client)
    uploaded = upload_scanned(client, page_count=3)
    FakeVision().install(monkeypatch)
    original_save = notes_agent._save_ocr_pages

    async def failing_save(document_id, user_id, pages, *, manual=False):
        if any(int(page["page"]) == 3 for page in pages):
            raise RuntimeError("模拟第 3 页落库失败")
        return await original_save(document_id, user_id, pages, manual=manual)

    monkeypatch.setattr(notes_agent, "_save_ocr_pages", failing_save)
    with pytest.raises(RuntimeError):
        client.post(f"/api/documents/{uploaded['id']}/ocr", json={
            "provider_id": provider["id"], "model": OCR_PROVIDER_MODEL,
            "page_from": 1, "page_to": 3, "confirmed": True,
        })

    # 第 1/2 页已逐页保存，第 3 页失败不影响前面结果。
    pages = stored_pages(uploaded["id"])["pages"]
    assert pages["1"]["state"] == "recognized"
    assert pages["2"]["state"] == "recognized"
    assert "3" not in pages


def test_sync_page_timeout_marks_failed_and_saves_others(client, monkeypatch):
    provider = save_provider(client)
    uploaded = upload_scanned(client, page_count=2)
    FakeVision({2: "第 2 页正文"}, delays={1: 5}).install(monkeypatch)
    monkeypatch.setenv("STUDY_OCR_PAGE_TIMEOUT", "0.05")

    result = ocr_request(client, provider, uploaded["id"], 1, 2)
    assert result["failed_pages"] == [1]
    pages = stored_pages(uploaded["id"])["pages"]
    assert pages["1"]["state"] == "failed"
    assert "超时" in pages["1"]["error"]
    assert pages["2"]["state"] == "recognized"
    assert "识别失败" in result["notice"]


def test_sync_path_saves_after_each_page_even_if_second_page_model_fails(client, monkeypatch):
    provider = save_provider(client)
    uploaded = upload_scanned(client, page_count=3)
    FakeVision({1: "第 1 页正文", 3: "第 3 页正文"}, failures={2}).install(monkeypatch)

    result = ocr_request(client, provider, uploaded["id"], 1, 3)
    assert result["failed_pages"] == [2]
    pages = stored_pages(uploaded["id"])["pages"]
    assert pages["1"]["state"] == "recognized"
    assert pages["2"]["state"] == "failed"
    assert "失败" in pages["2"]["error"]
    assert pages["3"]["state"] == "recognized"
    # 失败页已落库，后续只重试失败页即可。
    assert result["document"]["coverage"]["failed_pages"] == [2]
    assert "可只重试失败页" in result["notice"]


# ---------------------------------------------------------------------------
# 3. 异步执行器：逐页落库、检查点、进度事件
# ---------------------------------------------------------------------------

def test_async_executor_persists_pages_and_writes_checkpoint_per_page(client, monkeypatch):
    provider = save_provider(client)
    uploaded = upload_scanned(client, page_count=4)
    uid = user_id_of(client)
    job_id = insert_ocr_job(uid, checkpoint=None)
    checkpoint_seen = {}

    def on_call(page, kwargs):
        if page == 2:
            # 第 1 页处理完、第 2 页开始前，检查点必须已经记录第 1 页。
            checkpoint_seen["before_page2"] = job_snapshot(job_id)["checkpoint"]

    vision = FakeVision(on_call=on_call).install(monkeypatch)
    events = capture_publish_events(monkeypatch)
    save_calls = []
    original_save = notes_agent._save_ocr_pages

    async def tracking_save(document_id, user_id, pages, *, manual=False):
        save_calls.append([int(page["page"]) for page in pages])
        return await original_save(document_id, user_id, pages, manual=manual)

    monkeypatch.setattr(notes_agent, "_save_ocr_pages", tracking_save)

    result = run_ocr_job(job_id, uid, ocr_job_data(uploaded["id"], 1, 4, provider["id"]), provider)

    assert checkpoint_seen["before_page2"]["done_pages"] == [1]
    assert save_calls == [[1], [2], [3], [4]]
    assert sorted(vision.calls) == [1, 2, 3, 4]
    assert sorted(page["page"] for page in result["ocr_pages"]) == [1, 2, 3, 4]
    assert result["failed_pages"] == []
    assert result["document"]["coverage"]["complete"] is True
    snapshot = job_snapshot(job_id)
    assert snapshot["status"] == "running"
    assert snapshot["stage"] == "ocr-4/4"
    assert snapshot["checkpoint"]["done_pages"] == [1, 2, 3, 4]
    assert snapshot["checkpoint"]["failed_pages"] == []
    # 每页一条 stage 事件（带 done/total），供前端「正在识别第 X/N 页」复用。
    progress = [event for event in events if event["event"] == "stage" and "total" in event["data"]]
    assert [(item["data"]["page"], item["data"]["done"], item["data"]["total"]) for item in progress] == [
        (1, 1, 4), (2, 2, 4), (3, 3, 4), (4, 4, 4),
    ]


def test_async_executor_page_failure_does_not_block_batch(client, monkeypatch):
    provider = save_provider(client)
    uploaded = upload_scanned(client, page_count=3)
    uid = user_id_of(client)
    job_id = insert_ocr_job(uid)
    FakeVision({1: "第 1 页正文", 3: "第 3 页正文"}, failures={2}).install(monkeypatch)

    result = run_ocr_job(job_id, uid, ocr_job_data(uploaded["id"], 1, 3, provider["id"]), provider)

    assert result["failed_pages"] == [2]
    assert result["preserved_pages"] == []
    assert "识别失败" in result["notice"]
    # 第 2 页失败不影响第 1/3 页保存；任务本身不算失败（可续跑失败页）。
    pages = stored_pages(uploaded["id"])["pages"]
    assert pages["1"]["state"] == "recognized"
    assert pages["2"]["state"] == "failed"
    assert pages["3"]["state"] == "recognized"
    snapshot = job_snapshot(job_id)
    assert snapshot["status"] == "running"
    assert snapshot["checkpoint"]["done_pages"] == [1, 3]
    assert snapshot["checkpoint"]["failed_pages"] == [2]


def test_async_executor_page_timeout_marks_failed(client, monkeypatch):
    provider = save_provider(client)
    uploaded = upload_scanned(client, page_count=2)
    uid = user_id_of(client)
    job_id = insert_ocr_job(uid)
    FakeVision({2: "第 2 页正文"}, delays={1: 5}).install(monkeypatch)
    monkeypatch.setenv("STUDY_OCR_PAGE_TIMEOUT", "0.05")

    result = run_ocr_job(job_id, uid, ocr_job_data(uploaded["id"], 1, 2, provider["id"]), provider)

    assert result["failed_pages"] == [1]
    pages = stored_pages(uploaded["id"])["pages"]
    assert pages["1"]["state"] == "failed"
    assert "超时" in pages["1"]["error"]
    assert pages["2"]["state"] == "recognized"
    assert job_snapshot(job_id)["checkpoint"]["failed_pages"] == [1]


# ---------------------------------------------------------------------------
# 4. 取消：已保存页保留、状态落定为 cancelled
# ---------------------------------------------------------------------------

def test_async_executor_cancel_keeps_saved_pages(client, monkeypatch):
    provider = save_provider(client)
    uploaded = upload_scanned(client, page_count=4)
    uid = user_id_of(client)
    job_id = insert_ocr_job(uid)

    def cancel_after_first_page(page, kwargs):
        if page == 1:
            with db.connection() as con:
                con.execute(
                    "UPDATE ai_jobs SET cancelled=1,status='cancel_requested' WHERE id=?", (job_id,),
                )

    FakeVision(on_call=cancel_after_first_page).install(monkeypatch)

    result = run_ocr_job(job_id, uid, ocr_job_data(uploaded["id"], 1, 4, provider["id"]), provider)

    assert result["__cancelled__"] is True
    assert result["cancelled"] is True
    assert "任务已取消" in result["notice"]
    # 第 1 页识别结果在取消前已保存，其余页保持 pending 以便续跑。
    pages = stored_pages(uploaded["id"])["pages"]
    assert pages["1"]["state"] == "recognized"
    assert "2" not in pages
    snapshot = job_snapshot(job_id)
    assert snapshot["status"] == "cancelled"
    assert snapshot["stage"] == "cancelled"
    assert snapshot["checkpoint"]["done_pages"] == [1]


# ---------------------------------------------------------------------------
# 5. 断点续跑：跳过已完成页，只重试未完成/失败页
# ---------------------------------------------------------------------------

def test_async_executor_resume_skips_done_pages_and_retries_failed(client, monkeypatch):
    provider = save_provider(client)
    uploaded = upload_scanned(client, page_count=4)
    uid = user_id_of(client)
    # 模拟上一轮已完成第 1/2 页、第 3 页失败。
    asyncio.run(notes_agent._save_ocr_pages(uploaded["id"], uid, [
        {"page": 1, "text": "第一页已保存", "state": "recognized", "source": "ocr"},
        {"page": 2, "text": "第二页已保存", "state": "recognized", "source": "ocr"},
    ]))
    job_id = insert_ocr_job(uid, checkpoint={"done_pages": [1, 2], "failed_pages": [3]})
    vision = FakeVision({3: "第三页补识别", 4: "第四页识别"}).install(monkeypatch)

    result = run_ocr_job(job_id, uid, ocr_job_data(uploaded["id"], 1, 4, provider["id"]), provider)

    # 已完成页不再调用模型；失败页重试成功。
    assert sorted(vision.calls) == [3, 4]
    assert result["failed_pages"] == []
    assert result["document"]["coverage"]["complete"] is True
    pages = stored_pages(uploaded["id"])["pages"]
    assert pages["1"]["text"] == "第一页已保存"
    assert pages["2"]["text"] == "第二页已保存"
    assert pages["3"]["state"] == "recognized"
    assert pages["4"]["state"] == "recognized"
    snapshot = job_snapshot(job_id)
    assert snapshot["checkpoint"]["done_pages"] == [1, 2, 3, 4]
    assert snapshot["checkpoint"]["failed_pages"] == []
    # 续跑结果仍带回已完成页，前端可以复用同一渲染逻辑。
    assert sorted(page["page"] for page in result["ocr_pages"]) == [1, 2, 3, 4]


def test_async_executor_preserves_manually_reviewed_pages(client, monkeypatch):
    provider = save_provider(client)
    uploaded = upload_scanned(client, page_count=2)
    uid = user_id_of(client)
    job_id = insert_ocr_job(uid)
    reviewed = client.patch(f"/api/documents/{uploaded['id']}/ocr-text", json={
        "pages": [{"page": 1, "text": "人工校对后的公式 $a+b=c$"}],
    })
    assert reviewed.status_code == 200, reviewed.text
    FakeVision({1: "机器新结果", 2: "第 2 页正文"}).install(monkeypatch)

    result = run_ocr_job(job_id, uid, ocr_job_data(uploaded["id"], 1, 2, provider["id"]), provider)

    assert result["preserved_pages"] == [1]
    assert "保留人工校对内容" in result["notice"]
    pages = stored_pages(uploaded["id"])["pages"]
    assert pages["1"]["state"] == "reviewed"
    assert pages["1"]["text"] == "人工校对后的公式 $a+b=c$"
    assert pages["2"]["state"] == "recognized"


# ---------------------------------------------------------------------------
# 6. 单页渲染清晰度（scale 默认 2.0，可环境变量覆盖）
# ---------------------------------------------------------------------------

class _FakeBitmap:
    def __init__(self):
        from PIL import Image
        self.image = Image.new("RGB", (4, 4), (255, 255, 255))

    def to_pil(self):
        return self.image

    def close(self):
        return None


class _FakePage:
    def __init__(self, module):
        self.module = module

    def render(self, scale=1):
        self.module.scales.append(scale)
        return _FakeBitmap()

    def close(self):
        return None


class _FakeDocument:
    def __init__(self, module, pages):
        self.module = module
        self.page_count = pages

    def __len__(self):
        return self.page_count

    def __getitem__(self, index):
        self.module.page_indexes.append(index)
        return _FakePage(self.module)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def install_fake_pdfium(monkeypatch, pages=2):
    module = types.ModuleType("pypdfium2")
    module.scales = []
    module.page_indexes = []
    module.PdfDocument = lambda pdf: _FakeDocument(module, pages)
    monkeypatch.setitem(sys.modules, "pypdfium2", module)
    return module


def test_ocr_scale_defaults_to_two_and_env_override(monkeypatch):
    assert notes_agent.OCR_DEFAULT_SCALE == 2.0
    assert notes_agent._ocr_scale() == 2.0
    monkeypatch.setenv("STUDY_OCR_SCALE", "2.5")
    assert notes_agent._ocr_scale() == 2.5
    monkeypatch.setenv("STUDY_OCR_SCALE", "not-a-number")
    assert notes_agent._ocr_scale() == 2.0
    monkeypatch.setenv("STUDY_OCR_SCALE", "-1")
    assert notes_agent._ocr_scale() == 2.0

    module = install_fake_pdfium(monkeypatch)
    monkeypatch.delenv("STUDY_OCR_SCALE", raising=False)
    rendered = notes_agent._render_pdf_pages_to_images(b"%PDF-fake", 1, 2)
    assert [page for page, _ in rendered] == [1, 2]
    assert module.scales == [2.0, 2.0]
    assert module.page_indexes == [0, 1]

    monkeypatch.setenv("STUDY_OCR_SCALE", "2.6")
    notes_agent._render_pdf_pages_to_images(b"%PDF-fake", 2, 2)
    assert module.scales[-1] == 2.6

    # 显式参数优先于环境变量，便于单测/特殊资料精细控制。
    notes_agent._render_pdf_pages_to_images(b"%PDF-fake", 1, 1, scale=1.4)
    assert module.scales[-1] == 1.4


# ---------------------------------------------------------------------------
# 7. 参数校验与 jobs 契约联调探针
# ---------------------------------------------------------------------------

def test_async_executor_rejects_non_pdf_and_bad_range(client, monkeypatch):
    provider = save_provider(client)
    uid = user_id_of(client)
    FakeVision().install(monkeypatch)

    text_doc = client.post("/api/documents", files={
        "file": ("笔记.txt", "第一章 函数".encode(), "text/plain"),
    }).json()
    text_job = insert_ocr_job(uid)
    with pytest.raises(ValueError, match="仅扫描 PDF"):
        run_ocr_job(text_job, uid, ocr_job_data(text_doc["id"], 1, 1, provider["id"]), provider)

    uploaded = upload_scanned(client, page_count=35)
    over_job = insert_ocr_job(uid)
    with pytest.raises(ValueError, match="1–30 页"):
        run_ocr_job(over_job, uid, ocr_job_data(uploaded["id"], 1, 31, provider["id"]), provider)

    out_of_range_job = insert_ocr_job(uid)
    with pytest.raises(ValueError, match="1–30 页"):
        run_ocr_job(out_of_range_job, uid, ocr_job_data(uploaded["id"], 40, 40, provider["id"]), provider)


def test_jobs_ocr_contract_probe(client):
    """stream-core 交付后，JobInput/submit_job 必须支持 OCR 异步任务契约。"""
    submit = getattr(ai_jobs, "submit_job", None)
    if not callable(submit):
        pytest.skip("jobs.submit_job 尚未交付（stream-core 并行开发中），端点侧已用 monkeypatch 兜底")
    job_input_cls = getattr(ai_jobs, "JobInput", None)
    fields = getattr(job_input_cls, "model_fields", {}) or {}
    assert "page_from" in fields and "page_to" in fields, "JobInput 缺少 page_from/page_to"
    instance = job_input_cls(
        task="ocr", document_id=1, page_from=2, page_to=5, provider_id=3, model=OCR_PROVIDER_MODEL,
    )
    assert instance.task == "ocr"
    assert instance.document_id == 1
    assert instance.page_from == 2
    assert instance.page_to == 5
    # 真实 JobInput 可用时，_ocr_job_input 应返回真实模型而不是兼容对象。
    built = notes_agent._ocr_job_input(
        document_id=1, page_from=2, page_to=5, provider_id=3, model=OCR_PROVIDER_MODEL,
    )
    assert type(built) is job_input_cls


def test_real_submit_job_creates_ocr_job_and_keeps_page_range(client, monkeypatch):
    """与 jobs.submit_job 的真实联调：创建 task="ocr" 任务、保留页范围、走调度。"""
    submit = getattr(ai_jobs, "submit_job", None)
    if not callable(submit):
        pytest.skip("jobs.submit_job 尚未交付（stream-core 并行开发中），端点侧已用 monkeypatch 兜底")
    provider = save_provider(client)
    uploaded = upload_scanned(client, page_count=6)
    FakeVision().install(monkeypatch)  # 即使误派发也不会联网

    dispatched = []

    async def no_dispatch(job_id, token, runner):
        dispatched.append((job_id, token, getattr(runner, "__name__", "runner")))

    monkeypatch.setattr(ai_jobs, "_dispatch", no_dispatch)

    result = ocr_request(client, provider, uploaded["id"], 2, 6)
    assert result["async"] is True
    job = result["job"]
    assert job["task"] == "ocr"
    assert job["status"] == "queued"
    assert job["request"]["document_id"] == uploaded["id"]
    assert job["request"]["page_from"] == 2
    assert job["request"]["page_to"] == 6
    assert dispatched and dispatched[0][0] == job["id"]
    # 任务参数可还原成 JobInput（Worker/续跑反序列化路径）。
    restored = ai_jobs.JobInput(**job["request"])
    assert restored.task == "ocr"
    assert restored.page_from == 2
    assert restored.page_to == 6


def test_run_job_dispatches_ocr_and_completes(client, monkeypatch):
    """_run_job → _ocr_job 真实接线：任务完成且 result 保留同步路径返回结构。"""
    import inspect

    if "ocr" not in inspect.getsource(ai_jobs._run_job):
        pytest.skip("jobs._run_job 尚未接线 task=ocr（stream-core 并行开发中）")
    provider = save_provider(client)
    uploaded = upload_scanned(client, page_count=3)
    uid = user_id_of(client)
    job_id = insert_ocr_job(uid)
    FakeVision({1: "第 1 页正文", 2: "第 2 页正文", 3: "第 3 页正文"}).install(monkeypatch)
    data = ai_jobs.JobInput(
        task="ocr", document_id=uploaded["id"], page_from=1, page_to=3,
        provider_id=provider["id"], model=OCR_PROVIDER_MODEL,
    )

    asyncio.run(ai_jobs._run_job(job_id, uid, data))

    snapshot = job_snapshot(job_id)
    assert snapshot["status"] == "completed"
    assert snapshot["checkpoint"]["done_pages"] == [1, 2, 3]
    with db.connection() as con:
        result = json.loads(con.execute("SELECT result_json FROM ai_jobs WHERE id=?", (job_id,)).fetchone()[0])
    assert result["document"]["coverage"]["complete"] is True
    assert sorted(page["page"] for page in result["ocr_pages"]) == [1, 2, 3]
    assert result["failed_pages"] == []
    assert result["preserved_pages"] == []
    assert "prompt_version" in result and "models" in result


def test_dispatched_ocr_job_completes_with_real_dispatch_token(client, monkeypatch):
    """真实派发上下文（dispatch_token）下检查点/状态写入不被 token 过滤误伤。"""
    import inspect

    if "ocr" not in inspect.getsource(ai_jobs._run_job):
        pytest.skip("jobs._run_job 尚未接线 task=ocr（stream-core 并行开发中）")
    provider = save_provider(client)
    uploaded = upload_scanned(client, page_count=3)
    uid = user_id_of(client)
    token = "ocr-dispatch-token"
    request = {
        "task": "ocr", "document_id": uploaded["id"], "page_from": 1, "page_to": 3,
        "provider_id": provider["id"], "model": OCR_PROVIDER_MODEL,
    }
    now = db.utcnow()
    with db.connection() as con:
        job_id = con.execute(
            "INSERT INTO ai_jobs(user_id,task,status,stage,request_json,partial_content,cancelled,"
            "created_at,updated_at,dispatch_token) VALUES (?,?,?,?,?,'',0,?,?,?)",
            (uid, "ocr", "queued", "queued", json.dumps(request), now, now, token),
        ).lastrowid
    FakeVision({1: "第 1 页正文", 2: "第 2 页正文", 3: "第 3 页正文"}).install(monkeypatch)

    asyncio.run(ai_jobs.execute_dispatched_job(job_id, token, ai_jobs._run_job))

    snapshot = job_snapshot(job_id)
    assert snapshot["status"] == "completed"
    assert snapshot["stage"] == "completed"
    assert snapshot["checkpoint"]["done_pages"] == [1, 2, 3]
    assert snapshot["checkpoint"]["failed_pages"] == []
    pages = stored_pages(uploaded["id"])["pages"]
    assert {page["state"] for page in pages.values()} == {"recognized"}


def _insert_dispatched_job(uid, token, request):
    now = db.utcnow()
    with db.connection() as con:
        return con.execute(
            "INSERT INTO ai_jobs(user_id,task,status,stage,request_json,partial_content,cancelled,"
            "created_at,updated_at,dispatch_token) VALUES (?,?,?,?,?,'',0,?,?,?)",
            (uid, "ocr", "queued", "queued", json.dumps(request), now, now, token),
        ).lastrowid


def test_dispatched_cancel_settles_cancelled_and_keeps_saved_pages(client, monkeypatch):
    """真实派发上下文取消：已完成页保存、任务最终落定 cancelled（不被改成 completed）。"""
    import inspect

    if "ocr" not in inspect.getsource(ai_jobs._run_job):
        pytest.skip("jobs._run_job 尚未接线 task=ocr（stream-core 并行开发中）")
    provider = save_provider(client)
    uploaded = upload_scanned(client, page_count=4)
    uid = user_id_of(client)
    token = "ocr-cancel-token"
    job_id = _insert_dispatched_job(uid, token, {
        "task": "ocr", "document_id": uploaded["id"], "page_from": 1, "page_to": 4,
        "provider_id": provider["id"], "model": OCR_PROVIDER_MODEL,
    })

    def cancel_after_first_page(page, kwargs):
        if page == 1:
            with db.connection() as con:
                con.execute(
                    "UPDATE ai_jobs SET cancelled=1,status='cancel_requested' WHERE id=?", (job_id,),
                )

    FakeVision(on_call=cancel_after_first_page).install(monkeypatch)

    asyncio.run(ai_jobs.execute_dispatched_job(job_id, token, ai_jobs._run_job))

    snapshot = job_snapshot(job_id)
    assert snapshot["status"] == "cancelled"
    assert snapshot["stage"] == "cancelled"
    assert snapshot["checkpoint"]["done_pages"] == [1]
    pages = stored_pages(uploaded["id"])["pages"]
    assert pages["1"]["state"] == "recognized"
    assert "2" not in pages


def test_run_job_reports_range_validation_as_failure_message(client, monkeypatch):
    """越界（>30 页）不再返回 HTTP 422，而是转成任务失败信息，便于任务列表展示。"""
    import inspect

    if "ocr" not in inspect.getsource(ai_jobs._run_job):
        pytest.skip("jobs._run_job 尚未接线 task=ocr（stream-core 并行开发中）")
    provider = save_provider(client)
    uploaded = upload_scanned(client, page_count=35)
    uid = user_id_of(client)
    job_id = insert_ocr_job(uid)
    FakeVision().install(monkeypatch)
    data = ai_jobs.JobInput(
        task="ocr", document_id=uploaded["id"], page_from=1, page_to=31,
        provider_id=provider["id"], model=OCR_PROVIDER_MODEL,
    )

    asyncio.run(ai_jobs._run_job(job_id, uid, data))

    snapshot = job_snapshot(job_id)
    assert snapshot["status"] == "failed"
    with db.connection() as con:
        error = con.execute("SELECT error FROM ai_jobs WHERE id=?", (job_id,)).fetchone()[0]
    assert "1–30 页" in error


def test_run_job_resume_from_checkpoint_retries_only_failed_pages(client, monkeypatch):
    """_run_job 复跑（断点续跑）只重试未完成/失败页，已完成页不重复调用模型。"""
    import inspect

    if "ocr" not in inspect.getsource(ai_jobs._run_job):
        pytest.skip("jobs._run_job 尚未接线 task=ocr（stream-core 并行开发中）")
    provider = save_provider(client)
    uploaded = upload_scanned(client, page_count=3)
    uid = user_id_of(client)
    asyncio.run(notes_agent._save_ocr_pages(uploaded["id"], uid, [
        {"page": 1, "text": "第一页已保存", "state": "recognized", "source": "ocr"},
    ]))
    job_id = insert_ocr_job(uid, checkpoint={"done_pages": [1], "failed_pages": [2]})
    vision = FakeVision({2: "第二页补识别", 3: "第三页识别"}).install(monkeypatch)
    data = ai_jobs.JobInput(
        task="ocr", document_id=uploaded["id"], page_from=1, page_to=3,
        provider_id=provider["id"], model=OCR_PROVIDER_MODEL,
    )

    asyncio.run(ai_jobs._run_job(job_id, uid, data))

    assert sorted(vision.calls) == [2, 3]
    snapshot = job_snapshot(job_id)
    assert snapshot["status"] == "completed"
    assert snapshot["checkpoint"]["done_pages"] == [1, 2, 3]
    assert snapshot["checkpoint"]["failed_pages"] == []
    pages = stored_pages(uploaded["id"])["pages"]
    assert pages["1"]["text"] == "第一页已保存"


def test_async_executor_processes_full_thirty_page_range(client, monkeypatch):
    """单批上限 30 页可完整逐页识别并落库。"""
    provider = save_provider(client)
    uploaded = upload_scanned(client, page_count=30)
    uid = user_id_of(client)
    job_id = insert_ocr_job(uid)
    vision = FakeVision().install(monkeypatch)

    result = run_ocr_job(job_id, uid, ocr_job_data(uploaded["id"], 1, 30, provider["id"]), provider)

    assert vision.calls == list(range(1, 31))
    assert result["document"]["coverage"]["complete"] is True
    assert job_snapshot(job_id)["checkpoint"]["done_pages"] == list(range(1, 31))
    pages = stored_pages(uploaded["id"])["pages"]
    assert set(pages) == {str(page) for page in range(1, 31)}





