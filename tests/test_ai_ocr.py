"""P1 本地 OCR（app/ai/ocr.py）与拍题/扫描资料集成验收。

不加载真实 ONNX 模型：引擎、视觉模型与本地 OCR 全部用假实现替换，因此不联网、不依赖模型下载。
覆盖：

1. ``recognize_image``：返回结构/大图缩放/超时/并发信号量/语言与坏图校验；
2. PDF ``recognize_pdf_pages``：逐页结构/页码选择/单次页数上限/越界/单页渲染失败隔离；
3. 开关与降级：``STUDY_LOCAL_OCR=off``、依赖缺失、引擎初始化失败（可 reset 重试）；
4. HTTP：``GET /api/ai/ocr/info``、``POST /api/ai/ocr`` 的登录、图片/PDF、413/422/503；
5. 集成：``notes_agent._ocr_job`` 的 OCR 提示注入与 ``source='local_ocr'`` 保留；
6. 集成：``upload_pipeline._recognize_job`` 的 OCR 提示注入与结果 ``ocr_text``。
"""
import asyncio
import base64
from io import BytesIO
import json
import threading
import time

from fastapi.testclient import TestClient
from PIL import Image
from pypdf import PdfWriter
import pytest

from app import ai, db, practice
from app.ai import notes_agent, ocr


# ---------------------------------------------------------------------------
# 公共假实现与辅助
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _reset_local_ocr(monkeypatch):
    """每个用例从干净的单例/环境变量开始，避免引擎失败缓存与 env 相互污染。"""
    ocr.reset_engine()
    for name in ("STUDY_LOCAL_OCR", "STUDY_OCR_MAX_SIDE", "STUDY_OCR_TIMEOUT", "STUDY_OCR_MAX_PAGES"):
        monkeypatch.delenv(name, raising=False)
    yield
    ocr.reset_engine()


def png_bytes(width=200, height=120, color="white") -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (width, height), color).save(buffer, "PNG")
    return buffer.getvalue()


def pdf_bytes(page_count=2, width=600, height=800) -> bytes:
    writer = PdfWriter()
    for _ in range(page_count):
        writer.add_blank_page(width=width, height=height)
    buffer = BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def fake_lines():
    return [
        [[[0, 0], [40, 0], [40, 12], [0, 12]], "光合作用", 0.98],
        [[[0, 14], [40, 14], [40, 26], [0, 26]], "二氧化碳", 0.97],
    ]


class FakeEngine:
    """可观察的假 RapidOCR 引擎：记录入参尺寸、推理并发数与耗时。"""

    def __init__(self, lines=None, delay=0.0, error=None):
        self.lines = fake_lines() if lines is None else lines
        self.delay = delay
        self.error = error
        self.shapes = []
        self.concurrent = 0
        self.max_concurrent = 0
        self._lock = threading.Lock()

    def __call__(self, image):
        shape = getattr(image, "shape", None)
        with self._lock:
            self.concurrent += 1
            self.max_concurrent = max(self.max_concurrent, self.concurrent)
            self.shapes.append(shape)
        try:
            if self.delay:
                time.sleep(self.delay)
            if self.error:
                raise RuntimeError(self.error)
            return self.lines, [0.01]
        finally:
            with self._lock:
                self.concurrent -= 1


def install_fake_engine(monkeypatch, engine=None):
    engine = engine or FakeEngine()
    monkeypatch.setattr(ocr, "_get_engine", lambda: engine)
    # 无论测试机是否真的安装了 rapidocr，都按「依赖可用」处理，让假引擎成为唯一变量。
    monkeypatch.setattr(ocr, "_module_available", lambda: True)
    return engine


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# 1. recognize_image：结构、缩放、超时、并发
# ---------------------------------------------------------------------------

def test_recognize_image_returns_text_lines_and_image_size(monkeypatch):
    engine = install_fake_engine(monkeypatch)
    result = run(ocr.recognize_image(png_bytes(200, 100)))

    assert set(result) == {"text", "lines", "width", "height", "elapsed_ms"}
    assert result["text"] == "光合作用\n二氧化碳"
    assert result["width"] == 200 and result["height"] == 100
    assert isinstance(result["elapsed_ms"], int) and result["elapsed_ms"] >= 0
    assert len(result["lines"]) == 2
    first = result["lines"][0]
    assert first["text"] == "光合作用"
    assert first["score"] == pytest.approx(0.98)
    assert len(first["box"]) == 4
    assert all(isinstance(point, list) and len(point) == 2 for point in first["box"])
    # 传给引擎的是 BGR ndarray，尺寸与返回的 width/height 一致。
    assert engine.shapes[0] == (100, 200, 3)


def test_recognize_image_scales_large_image_to_max_side(monkeypatch):
    engine = install_fake_engine(monkeypatch)
    result = run(ocr.recognize_image(png_bytes(6000, 3000)))
    assert (result["width"], result["height"]) == (2400, 1200)
    assert engine.shapes[0] == (1200, 2400, 3)

    monkeypatch.setenv("STUDY_OCR_MAX_SIDE", "800")
    from_env = run(ocr.recognize_image(png_bytes(6000, 3000)))
    assert max(from_env["width"], from_env["height"]) == 800

    # 显式参数优先于环境变量，并限制在 320–6000。
    explicit = run(ocr.recognize_image(png_bytes(900, 600), max_side=320))
    assert max(explicit["width"], explicit["height"]) == 320


def test_recognize_image_times_out_with_ocr_timeout(monkeypatch):
    install_fake_engine(monkeypatch, FakeEngine(delay=0.6))
    with pytest.raises(ocr.OcrTimeout) as error:
        run(ocr.recognize_image(png_bytes(), timeout=0.15))
    assert "未完成" in str(error.value) and "秒" in str(error.value)


def test_recognize_image_limits_concurrency_to_two(monkeypatch):
    engine = install_fake_engine(monkeypatch, FakeEngine(delay=0.1))
    image = png_bytes()

    async def recognize_many():
        return await asyncio.gather(*[ocr.recognize_image(image) for _ in range(4)])

    results = run(recognize_many())
    assert all("光合作用" in item["text"] for item in results)
    assert len(engine.shapes) == 4
    # 进程级信号量默认并发 2：同时最多两个线程进入引擎。
    assert engine.max_concurrent == 2


def test_recognize_image_rejects_bad_input_and_language():
    with pytest.raises(ValueError) as error:
        run(ocr.recognize_image(b"not an image"))
    assert "有效的图片" in str(error.value)
    with pytest.raises(ValueError):
        run(ocr.recognize_image(png_bytes(), lang="ja"))
    with pytest.raises(ValueError):
        run(ocr.recognize_image(png_bytes(), max_side=-1))
    with pytest.raises(ValueError):
        run(ocr.recognize_image(png_bytes(), timeout=0))
    with pytest.raises(ValueError):
        run(ocr.recognize_image())


def test_recognize_image_accepts_image_part_dict_and_path(monkeypatch, tmp_path):
    engine = install_fake_engine(monkeypatch)
    part = {"mime": "image/png", "data": base64.b64encode(png_bytes()).decode()}
    assert "光合作用" in run(ocr.recognize_image(part))["text"]

    path = tmp_path / "题目.png"
    path.write_bytes(png_bytes())
    assert "二氧化碳" in run(ocr.recognize_image(path=str(path)))["text"]
    assert len(engine.shapes) == 2


# ---------------------------------------------------------------------------
# 2. PDF 逐页识别
# ---------------------------------------------------------------------------

def test_recognize_pdf_pages_one_result_per_page(monkeypatch):
    install_fake_engine(monkeypatch)
    pages = run(ocr.recognize_pdf_pages(pdf_bytes(3), dpi=72))
    assert [item["page"] for item in pages] == [1, 2, 3]
    for item in pages:
        assert set(item) == {"page", "text", "lines", "width", "height", "elapsed_ms"}
        assert "光合作用" in item["text"]
        assert (item["width"], item["height"]) == (600, 800)


def test_recognize_pdf_pages_selected_page_range(monkeypatch):
    install_fake_engine(monkeypatch)
    pdf = pdf_bytes(4)
    assert [item["page"] for item in run(ocr.recognize_pdf_pages(pdf, pages="2-3", dpi=72))] == [2, 3]
    assert [item["page"] for item in run(ocr.recognize_pdf_pages(pdf, pages=["4", 1], dpi=72))] == [1, 4]
    assert [item["page"] for item in run(ocr.recognize_pdf_pages(pdf, pages="3", dpi=72))] == [3]


def test_recognize_pdf_pages_enforces_page_limit_and_range(monkeypatch):
    install_fake_engine(monkeypatch)
    with pytest.raises(ValueError) as error:
        run(ocr.recognize_pdf_pages(pdf_bytes(6)))
    assert "最多识别 5 页" in str(error.value) and "分批" in str(error.value)

    with pytest.raises(ValueError) as error:
        run(ocr.recognize_pdf_pages(pdf_bytes(2), pages=[3]))
    assert "超出范围" in str(error.value)

    with pytest.raises(ValueError):
        run(ocr.recognize_pdf_pages(pdf_bytes(2), pages="1-x"))
    with pytest.raises(ValueError):
        run(ocr.recognize_pdf_pages(pdf_bytes(2), pages="3-1"))
    with pytest.raises(ValueError) as error:
        run(ocr.recognize_pdf_pages(b"not a pdf"))
    assert "PDF 无法解析" in str(error.value)


def test_recognize_pdf_single_page_render_failure_is_isolated(monkeypatch):
    install_fake_engine(monkeypatch)
    original = ocr._render_pdf_page

    def flaky_render(raw, page_no, dpi):
        if page_no == 2:
            raise RuntimeError("模拟渲染失败")
        return original(raw, page_no, dpi)

    monkeypatch.setattr(ocr, "_render_pdf_page", flaky_render)
    pages = run(ocr.recognize_pdf_pages(pdf_bytes(2), dpi=72))
    assert pages[0]["text"] and "error" not in pages[0]
    assert pages[1]["text"] == "" and pages[1]["lines"] == []
    assert "渲染失败" in pages[1]["error"]


# ---------------------------------------------------------------------------
# 3. 开关、依赖缺失与引擎初始化失败的降级
# ---------------------------------------------------------------------------

def test_switch_off_makes_local_ocr_unavailable(monkeypatch):
    monkeypatch.setenv("STUDY_LOCAL_OCR", "off")
    assert ocr.local_ocr_available() is False
    status = ocr.engine_status()
    assert status["mode"] == "off" and status["available"] is False and status["loaded"] is False
    assert "STUDY_LOCAL_OCR" in status["notice"]
    with pytest.raises(ocr.OcrUnavailable) as error:
        run(ocr.recognize_image(png_bytes()))
    assert "STUDY_LOCAL_OCR" in str(error.value)


def test_missing_dependency_reports_unavailable(monkeypatch):
    monkeypatch.setattr(ocr, "_module_available", lambda: False)
    assert ocr.local_ocr_available() is False
    status = ocr.engine_status()
    assert status["available"] is False
    assert status["version"] is None and status["models"] == []
    with pytest.raises(ocr.OcrUnavailable) as error:
        run(ocr.recognize_image(png_bytes()))
    assert "视觉模型" in str(error.value)


def test_engine_init_failure_is_cached_and_resettable(monkeypatch):
    monkeypatch.setattr(ocr, "_module_available", lambda: True)

    def broken_import():
        raise ImportError("模拟缺少 onnxruntime")

    monkeypatch.setattr(ocr, "_rapidocr_class", broken_import)
    with pytest.raises(ocr.OcrUnavailable) as error:
        ocr._get_engine()
    assert "本地 OCR" in str(error.value)
    # 失败被缓存：不再反复加载模型，状态里能拿到失败原因。
    assert ocr.local_ocr_available() is False
    assert ocr.engine_status()["error"]

    ocr.reset_engine()
    assert ocr.engine_status()["error"] is None
    # 依赖探测成功且清空失败缓存后恢复可用（真正加载仍会由下一次识别触发）。
    assert ocr.local_ocr_available() is True


def test_engine_status_reports_engine_version_models_and_limits(monkeypatch):
    monkeypatch.setattr(ocr, "_module_available", lambda: True)
    status = ocr.engine_status()
    assert status["engine"] == "rapidocr-onnxruntime"
    assert status["mode"] == "auto"
    assert status["available"] is True
    assert isinstance(status["version"], (str, type(None)))
    assert isinstance(status["models"], list)
    assert status["limits"] == {
        "max_side": 2400,
        "timeout_seconds": 45.0,
        "max_pages": 5,
        "concurrency": 2,
        "max_file_bytes": 12 * 1024 * 1024,
    }


# ---------------------------------------------------------------------------
# 4. HTTP 接口
# ---------------------------------------------------------------------------

def save_provider(client, name="本地 OCR 测试连接"):
    response = client.post("/api/providers", json={
        "name": name, "kind": "openai", "base_url": "https://api.openai.com/v1",
        "api_key": "sk-local-ocr-unit-test", "models": ["vision-model"],
    })
    assert response.status_code == 200, response.text
    return response.json()


def user_id_of(client):
    return client.get("/api/auth/me").json()["id"]


def test_ocr_api_requires_login(client):
    from app.main import app

    anonymous = TestClient(app)
    try:
        assert anonymous.get("/api/ai/ocr/info").status_code == 401
        assert anonymous.post(
            "/api/ai/ocr", files={"file": ("题目.png", png_bytes(), "image/png")},
        ).status_code == 401
    finally:
        anonymous.close()


def test_ocr_info_reports_status(client, monkeypatch):
    monkeypatch.setattr(ocr, "_module_available", lambda: True)
    response = client.get("/api/ai/ocr/info")
    assert response.status_code == 200, response.text
    payload = response.json()
    for key in ("engine", "mode", "available", "version", "models", "limits", "notice"):
        assert key in payload
    assert payload["engine"] == "rapidocr-onnxruntime"
    assert payload["limits"]["max_pages"] == 5


def test_ocr_api_recognizes_image(client, monkeypatch):
    install_fake_engine(monkeypatch)
    response = client.post("/api/ai/ocr", files={
        "file": ("题图.png", png_bytes(300, 120), "image/png"),
    })
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["kind"] == "image"
    assert payload["filename"] == "题图.png"
    assert payload["text"] == "光合作用\n二氧化碳"
    assert payload["width"] == 300 and payload["height"] == 120
    assert len(payload["lines"]) == 2
    assert payload["limits"]["timeout_seconds"] == 45.0
    assert "本地 OCR" in payload["notice"]


def test_ocr_api_recognizes_pdf_pages(client, monkeypatch):
    install_fake_engine(monkeypatch)
    response = client.post("/api/ai/ocr", files={
        "file": ("扫描件.pdf", pdf_bytes(2), "application/pdf"),
    })
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["kind"] == "pdf"
    assert payload["page_count"] == 2
    assert [item["page"] for item in payload["pages"]] == [1, 2]
    assert "光合作用" in payload["text"]

    selected = client.post(
        "/api/ai/ocr",
        files={"file": ("扫描件.pdf", pdf_bytes(2), "application/pdf")},
        data={"pages": "2"},
    )
    assert selected.status_code == 200, selected.text
    assert [item["page"] for item in selected.json()["pages"]] == [2]


def test_ocr_api_rejects_more_than_five_pdf_pages(client, monkeypatch):
    install_fake_engine(monkeypatch)
    response = client.post("/api/ai/ocr", files={
        "file": ("六页.pdf", pdf_bytes(6), "application/pdf"),
    })
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert "最多识别 5 页" in detail and "分批" in detail


def test_ocr_api_rejects_oversized_and_invalid_files(client):
    oversized = b"\x00" * (ocr.MAX_FILE + 1)
    too_big = client.post("/api/ai/ocr", files={"file": ("大图.png", oversized, "image/png")})
    assert too_big.status_code == 413
    assert "12 MB" in too_big.json()["detail"]

    bad = client.post("/api/ai/ocr", files={"file": ("坏图.png", b"not an image", "image/png")})
    assert bad.status_code == 422
    assert "有效的图片" in bad.json()["detail"]

    empty = client.post("/api/ai/ocr", files={"file": ("空.png", b"", "image/png")})
    assert empty.status_code == 422

    unsupported = client.post("/api/ai/ocr", files={"file": ("资料.txt", b"hello", "text/plain")})
    assert unsupported.status_code == 422
    assert "仅支持" in unsupported.json()["detail"]


def test_ocr_api_returns_503_when_engine_disabled(client, monkeypatch):
    monkeypatch.setenv("STUDY_LOCAL_OCR", "off")
    response = client.post("/api/ai/ocr", files={
        "file": ("题图.png", png_bytes(), "image/png"),
    })
    assert response.status_code == 503
    assert "本地 OCR" in response.json()["detail"]


# ---------------------------------------------------------------------------
# 5. notes_agent._ocr_job 集成：OCR 提示注入 / 视觉失败保留本地文本
# ---------------------------------------------------------------------------

def insert_ocr_job(user_id):
    with db.connection() as con:
        now = db.utcnow()
        return con.execute(
            "INSERT INTO ai_jobs(user_id,task,status,stage,request_json,partial_content,cancelled,"
            "created_at,updated_at,dispatch_token) VALUES (?,?,?,?,?,'',0,?,?,?)",
            (user_id, "ocr", "running", "running", "{}", now, now, "test-token"),
        ).lastrowid


def upload_scanned(client, page_count=1, name="扫描资料.pdf"):
    response = client.post("/api/documents", files={
        "file": (name, pdf_bytes(page_count), "application/pdf"),
    })
    assert response.status_code == 200, response.text
    return response.json()


def fake_render(pdf, page_from, page_to):
    """用真实 PNG 字节模拟逐页渲染结果，便于本地 OCR 假实现拿到合法图片片段。"""
    encoded = base64.b64encode(png_bytes(60, 40)).decode()
    return [
        (page, {"mime": "image/png", "data": encoded})
        for page in range(page_from, page_to + 1)
    ]


def ocr_job_data(document_id, provider_id, page_from=1, page_to=1):
    return notes_agent._ocr_job_input(
        document_id=document_id, page_from=page_from, page_to=page_to,
        provider_id=provider_id, model="vision-model",
    )


def captured_vision(text):
    prompts = []

    async def fake_call(provider, model, prompt, images=None, json_output=False,
                        token_limit=5000, **kwargs):
        prompts.append(prompt)
        return {"content": text, "model": model, "provider_name": "mock", "usage": {}}

    return prompts, fake_call


def test_ocr_job_injects_local_ocr_hint_into_vision_prompt(client, monkeypatch):
    provider = save_provider(client)
    uploaded = upload_scanned(client)
    user_id = user_id_of(client)
    job_id = insert_ocr_job(user_id)

    prompts, fake_vision = captured_vision("视觉模型校对后的正文")
    monkeypatch.setattr(notes_agent, "_render_pdf_pages_to_images", fake_render)
    monkeypatch.setattr(ai, "_call_model", fake_vision)

    async def fake_local_ocr(image):
        return {"text": "本地初稿：光合作用", "lines": [], "width": 60, "height": 40, "elapsed_ms": 3}

    monkeypatch.setattr(ocr, "recognize_image", fake_local_ocr)

    result = run(notes_agent._ocr_job(
        job_id, user_id, ocr_job_data(uploaded["id"], provider["id"]), provider,
    ))

    assert prompts and "本地 OCR 初步识别" in prompts[0]
    assert "本地初稿：光合作用" in prompts[0]
    assert "以图片内容为准" in prompts[0]
    # 视觉模型成功：页文本仍来自视觉模型，本地 OCR 只作为提示。
    page = result["ocr_pages"][0]
    assert page["text"] == "视觉模型校对后的正文"
    assert page["state"] == "recognized" and page["source"] == "ocr"
    assert result["local_ocr_hint_pages"] == [1]
    assert result["local_ocr_pages"] == []


def test_ocr_job_keeps_local_ocr_text_when_vision_fails(client, monkeypatch):
    provider = save_provider(client)
    uploaded = upload_scanned(client)
    user_id = user_id_of(client)
    job_id = insert_ocr_job(user_id)

    monkeypatch.setattr(notes_agent, "_render_pdf_pages_to_images", fake_render)

    async def failing_vision(provider, model, prompt, images=None, json_output=False,
                             token_limit=5000, **kwargs):
        raise ValueError("该模型不支持视觉输入")

    monkeypatch.setattr(ai, "_call_model", failing_vision)

    async def fake_local_ocr(image):
        return {"text": "本地识别：二氧化碳", "lines": [], "width": 60, "height": 40, "elapsed_ms": 3}

    monkeypatch.setattr(ocr, "recognize_image", fake_local_ocr)

    result = run(notes_agent._ocr_job(
        job_id, user_id, ocr_job_data(uploaded["id"], provider["id"]), provider,
    ))

    page = result["ocr_pages"][0]
    # 不伪造视觉识别成功：保留真实本地 OCR 文本并标注来源，同时明确提示需人工核对。
    assert page["text"] == "本地识别：二氧化碳"
    assert page["state"] == "recognized" and page["source"] == "local_ocr"
    assert result["local_ocr_pages"] == [1]
    assert result["failed_pages"] == []
    assert "本地 OCR 文本" in result["notice"] and "逐字核对" in result["notice"]

    with db.connection() as con:
        stored = json.loads(con.execute(
            "SELECT ocr_pages_json FROM ai_documents WHERE id=?", (uploaded["id"],),
        ).fetchone()[0])
        chunks = json.loads(con.execute(
            "SELECT chunks_json FROM ai_documents WHERE id=?", (uploaded["id"],),
        ).fetchone()[0])
    assert stored["pages"]["1"]["source"] == "local_ocr"
    assert stored["pages"]["1"]["text"] == "本地识别：二氧化碳"
    assert "本地识别：二氧化碳" in "".join(chunk["text"] for chunk in chunks)


def test_ocr_job_without_local_ocr_keeps_original_flow(client, monkeypatch):
    provider = save_provider(client)
    uploaded = upload_scanned(client)
    user_id = user_id_of(client)
    job_id = insert_ocr_job(user_id)

    prompts, fake_vision = captured_vision("纯视觉识别正文")
    monkeypatch.setattr(notes_agent, "_render_pdf_pages_to_images", fake_render)
    monkeypatch.setattr(ai, "_call_model", fake_vision)

    async def unavailable(image):
        raise ocr.OcrUnavailable("本地 OCR 依赖不可用")

    monkeypatch.setattr(ocr, "recognize_image", unavailable)

    result = run(notes_agent._ocr_job(
        job_id, user_id, ocr_job_data(uploaded["id"], provider["id"]), provider,
    ))

    assert "本地 OCR 初步识别" not in prompts[0]
    page = result["ocr_pages"][0]
    assert page["text"] == "纯视觉识别正文" and page["source"] == "ocr"
    assert result["local_ocr_hint_pages"] == []
    assert result["local_ocr_pages"] == []


def test_local_ocr_page_text_tolerates_bad_image_part(monkeypatch):
    async def unavailable(image):
        raise ocr.OcrUnavailable("不可用")

    monkeypatch.setattr(ocr, "recognize_image", unavailable)
    assert run(notes_agent._local_ocr_page_text({"mime": "image/jpeg", "data": "mock"})) == ("", "不可用")
    assert run(notes_agent._local_ocr_page_text({})) == ("", None)
    assert run(notes_agent._local_ocr_page_text(None)) == ("", None)


# ---------------------------------------------------------------------------
# 6. upload_pipeline._recognize_job 集成：拍题 OCR 提示注入与 ocr_text
# ---------------------------------------------------------------------------

def upload_question_image(client, name="拍题.png"):
    response = client.post("/api/uploads/question", files={
        "file": (name, png_bytes(300, 200), "image/png"),
    })
    assert response.status_code == 200, response.text
    return response.json()


def recognize_job_input(upload_id, provider_id):
    return ai.JobInput(
        task="recognize", upload_id=upload_id, provider_id=provider_id, model="vision-model",
    )


def vision_json_call(prompts, stem="光合作用与呼吸作用"):
    async def fake_call(provider, model, prompt, images=None, json_output=False,
                        token_limit=5000, **kwargs):
        prompts.append(prompt)
        return {
            "content": json.dumps({
                "subject": "生物", "type": "简答题", "stem": stem,
                "options": [], "conditions": [], "uncertain": [],
            }),
            "model": model, "provider_name": "mock", "usage": {},
        }
    return fake_call


def test_recognize_job_injects_local_ocr_and_returns_ocr_text(client, monkeypatch):
    provider = save_provider(client)
    uploaded = upload_question_image(client)
    user_id = user_id_of(client)
    monkeypatch.setattr(practice, "search_similar_questions", lambda *a, **k: [])

    prompts = []
    monkeypatch.setattr(ai, "_call_model", vision_json_call(prompts))

    async def fake_local_ocr(image):
        return {"text": "本地拍题初稿：二氧化碳", "lines": [], "width": 300, "height": 200, "elapsed_ms": 4}

    monkeypatch.setattr(ocr, "recognize_image", fake_local_ocr)

    result = run(ai._recognize_job(
        user_id, recognize_job_input(uploaded["id"], provider["id"]), provider, None,
    ))

    assert prompts and "本地 OCR 初步识别" in prompts[0]
    assert "本地拍题初稿：二氧化碳" in prompts[0]
    assert result["ocr_text"] == "本地拍题初稿：二氧化碳"
    assert result["ocr_notice"] == ""
    assert result["recognized"]["stem"] == "光合作用与呼吸作用"


def test_recognize_job_degrades_when_local_ocr_unavailable(client, monkeypatch):
    provider = save_provider(client)
    uploaded = upload_question_image(client)
    user_id = user_id_of(client)
    monkeypatch.setattr(practice, "search_similar_questions", lambda *a, **k: [])

    prompts = []
    monkeypatch.setattr(ai, "_call_model", vision_json_call(prompts, stem="纯视觉题干"))

    async def unavailable(image):
        raise ocr.OcrUnavailable("本地 OCR 依赖不可用")

    monkeypatch.setattr(ocr, "recognize_image", unavailable)

    result = run(ai._recognize_job(
        user_id, recognize_job_input(uploaded["id"], provider["id"]), provider, None,
    ))

    assert "本地 OCR 初步识别" not in prompts[0]
    assert result["ocr_text"] == ""
    assert "本地 OCR" in result["ocr_notice"]
    assert result["recognized"]["stem"] == "纯视觉题干"


def test_recognize_job_text_upload_skips_local_ocr(client, monkeypatch):
    provider = save_provider(client)
    uploaded = client.post("/api/uploads/question", files={
        "file": ("题目.txt", "求导：x^2".encode(), "text/plain"),
    }).json()
    user_id = user_id_of(client)
    monkeypatch.setattr(practice, "search_similar_questions", lambda *a, **k: [])

    prompts = []
    monkeypatch.setattr(ai, "_call_model", vision_json_call(prompts, stem="求导"))
    local_calls = []

    async def fake_local_ocr(image):
        local_calls.append(image)
        return {"text": "文字上传不应触发", "lines": [], "width": 0, "height": 0, "elapsed_ms": 0}

    monkeypatch.setattr(ocr, "recognize_image", fake_local_ocr)

    result = run(ai._recognize_job(
        user_id, recognize_job_input(uploaded["id"], provider["id"]), provider, None,
    ))

    assert local_calls == []
    assert result["ocr_text"] == ""
    assert "本地 OCR 初步识别" not in prompts[0]
    assert result["recognized"]["stem"] == "求导"
