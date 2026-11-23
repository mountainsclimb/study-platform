"""第五轮 OCR 内容块（blocks）与公式校对接口验收。

覆盖（全程 monkeypatch 假引擎/假视觉模型，不加载真实 ONNX 模型、不联网）：

1. 块构造：kind 启发式（math/text/table/image 边界）、bbox/norm 原图定位与归一化、
   order 阅读顺序、confidence、latex 恒为 null、math 块 needs_review=true、引擎字段；
2. ``recognize_image`` / ``recognize_pdf_pages`` 的 ``include_blocks=True`` 新字段，
   并校验默认 ``include_blocks=False`` 时返回结构与旧版完全一致（向后兼容）；
3. ``ocr.engine_status()`` / ``GET /api/ai/ocr/info`` 的 engine_version、capabilities、notice；
4. ``POST /api/ai/ocr`` 图片/PDF 响应新增 blocks、engine_version、capabilities；
5. ``POST /api/uploads/{id}/ocr-draft`` 响应 blocks/引擎元信息，OCR 不可用时 200 + 空块；
6. ``PATCH /api/uploads/{id}/confirm`` 的 blocks 往返、非法 422、未传沿用 base、
   旧请求不追加键；
7. 迟到识别不覆盖人工确认：确认后 blocks/revision 不变，pending_draft 携带新 blocks。
"""
import asyncio
import json
from io import BytesIO

from PIL import Image
from pypdf import PdfWriter
import pytest

from app import ai, practice
from app.ai import ocr, upload_pipeline


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


def png_bytes(width=400, height=300, color="white") -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (width, height), color).save(buffer, "PNG")
    return buffer.getvalue()


def pdf_bytes(page_count=2) -> bytes:
    writer = PdfWriter()
    for _ in range(page_count):
        writer.add_blank_page(width=600, height=800)
    buffer = BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def text_line(text, *, x=20, top=10, width=180, height=22, score=0.95):
    """构造一条与 RapidOCR 行结果同构的假行（4 点 box + score）。"""
    return {
        "text": text,
        "box": [[x, top], [x + width, top], [x + width, top + height], [x, top + height]],
        "score": score,
    }


def sample_lines():
    return [
        text_line("光合作用", x=20, top=10, width=140, score=0.98),
        text_line("x^2 + 2x = 0", x=20, top=40, width=200, score=0.91),
        text_line("姓名 | 分数", x=20, top=90, width=280, score=0.93),
        text_line("张三 | 90", x=20, top=120, width=280, score=0.90),
        text_line("二氧化碳", x=20, top=160, width=140, score=0.97),
    ]


def install_fake_infer(monkeypatch, lines=None):
    """替换 ``_infer_guarded``：不加载模型，直接返回构造好的 lines。"""
    payload = sample_lines() if lines is None else lines

    def fake_infer(bgr):
        return [dict(line) for line in payload]

    monkeypatch.setattr(ocr, "_infer_guarded", fake_infer)
    # 与 test_ai_ocr 相同：无论测试机是否安装了 rapidocr，都按「依赖可用」处理。
    monkeypatch.setattr(ocr, "_module_available", lambda: True)
    return payload


def install_local_ocr(monkeypatch, lines=None):
    """替换 ``ocr.recognize_image``：返回旧结构（text/lines/width/height/elapsed_ms）。"""
    payload = sample_lines() if lines is None else lines

    async def fake_recognize(image=None, **kwargs):
        return {
            "text": "\n".join(line["text"] for line in payload),
            "lines": [dict(line) for line in payload],
            "width": 400,
            "height": 300,
            "elapsed_ms": 2,
        }

    monkeypatch.setattr(ocr, "recognize_image", fake_recognize)


def run(coro):
    return asyncio.run(coro)


def upload_image(client, name="手机拍题.png"):
    response = client.post("/api/uploads/question", files={
        "file": (name, png_bytes(400, 300), "image/png"),
    })
    assert response.status_code == 200, response.text
    return response.json()


def post_ocr_draft(client, upload_id):
    response = client.post(f"/api/uploads/{upload_id}/ocr-draft")
    assert response.status_code == 200, response.text
    return response.json()


def confirm_payload(**overrides):
    payload = {
        "subject": "数学",
        "type": "计算题",
        "stem": "求 x^2 = 0 的解",
        "options": [],
        "conditions": [],
        "answers": ["0"],
        "explanation": "",
        "knowledge_tags": [],
    }
    payload.update(overrides)
    return payload


def save_provider(client, name="内容块测试连接"):
    response = client.post("/api/providers", json={
        "name": name, "kind": "openai", "base_url": "https://api.openai.com/v1",
        "api_key": "sk-ocr-blocks-unit-test", "models": ["vision-model"],
    })
    assert response.status_code == 200, response.text
    return response.json()


# ---------------------------------------------------------------------------
# 1. 块构造：kind 启发式、原图定位、阅读顺序、置信度
# ---------------------------------------------------------------------------

def test_build_blocks_text_math_table_with_bbox_norm_order_and_confidence():
    blocks = ocr.build_blocks(sample_lines(), 400, 300)

    assert [block["id"] for block in blocks] == ["b1", "b2", "b3", "b4", "b5"]
    assert [block["kind"] for block in blocks] == ["text", "math", "table", "table", "text"]
    assert [block["order"] for block in blocks] == [0, 1, 2, 3, 4]
    # 本地引擎不做公式识别：latex 永远为空，math 块必须人工/后续引擎校对。
    assert all(block["latex"] is None for block in blocks)
    assert [block["needs_review"] for block in blocks] == [False, True, False, False, False]
    assert all(block["page"] == 1 for block in blocks)
    assert all(block["rotation"] == 0 for block in blocks)
    assert all(block["source"] == "rapidocr" for block in blocks)
    assert all(block["engine"] == ocr.ENGINE_NAME for block in blocks)
    assert all("engine_version" in block for block in blocks)
    # image 块仅保留枚举，本地 OCR 不凭空造图片块。
    assert all(block["kind"] != "image" for block in blocks)
    assert set(ocr.BLOCK_KINDS) == {"text", "math", "table", "image"}

    math_block = blocks[1]
    assert math_block["text"] == "x^2 + 2x = 0"
    assert math_block["confidence"] == pytest.approx(0.91)
    # 原图像素坐标（0 保留 1 位）与 0–1 归一化。
    assert math_block["bbox"] == {"x": 20.0, "y": 40.0, "w": 200.0, "h": 22.0}
    assert math_block["norm"]["x"] == pytest.approx(0.05)
    assert math_block["norm"]["y"] == pytest.approx(0.1333)
    assert math_block["norm"]["w"] == pytest.approx(0.5)
    assert math_block["norm"]["h"] == pytest.approx(0.0733)
    assert blocks[0]["confidence"] == pytest.approx(0.98)
    assert blocks[4]["confidence"] == pytest.approx(0.97)


def test_build_blocks_orders_by_reading_order_not_input_order():
    lines = [
        text_line("第二行", x=10, top=60, width=100),
        text_line("同一行右侧", x=220, top=10, width=100),
        text_line("第一行", x=10, top=10, width=100),
    ]
    blocks = ocr.build_blocks(lines, 400, 200)
    # 先按行 top 聚类视觉行，同一视觉行内按 x 从左到右。
    assert [block["text"] for block in blocks] == ["第一行", "同一行右侧", "第二行"]
    assert [block["id"] for block in blocks] == ["b1", "b2", "b3"]
    assert [block["order"] for block in blocks] == [0, 1, 2]


def test_math_kind_heuristics_never_invent_latex():
    lines = [
        text_line("光合作用需要光照", top=10),
        text_line("3 × 4 = 12", top=40),
        text_line("y = 2x + 1", top=70),
        text_line("如图（1）所示", top=100),
        text_line(r"$\frac{1}{2}$", top=130),
    ]
    blocks = ocr.build_blocks(lines, 400, 300)
    assert [block["kind"] for block in blocks] == ["text", "math", "math", "text", "math"]
    for block in blocks:
        assert block["latex"] is None
        assert block["needs_review"] is (block["kind"] == "math")


def test_table_kind_requires_consecutive_table_like_rows():
    single = ocr.build_blocks([text_line("姓名 | 分数")], 400, 200)
    assert single[0]["kind"] == "text"

    pair = ocr.build_blocks([
        text_line("姓名 | 分数", top=10),
        text_line("张三 | 90", top=40),
    ], 400, 200)
    assert [block["kind"] for block in pair] == ["table", "table"]

    # 制表符/多列空格式（等宽列）同样按连续行判表格；表头行不含数字也成立。
    spaced = ocr.build_blocks([
        text_line("科目\t分数\t排名", top=10),
        text_line("数学\t95\t3", top=40),
    ], 400, 200)
    assert [block["kind"] for block in spaced] == ["table", "table"]


def test_build_blocks_handles_empty_and_malformed_lines():
    assert ocr.build_blocks([], 100, 100) == []
    blocks = ocr.build_blocks([
        {"text": "", "box": [[0, 0], [10, 0], [10, 10], [0, 10]], "score": 0.9},
        {"text": "没有坐标", "box": [], "score": 0.8},
        {"text": "坏分数", "box": [[0, 0], [50, 0], [50, 20], [0, 20]], "score": "bad"},
        "不是字典",
    ], 100, 100)
    assert [block["text"] for block in blocks] == ["坏分数"]
    assert blocks[0]["confidence"] == 0.0
    assert blocks[0]["kind"] == "text"


# ---------------------------------------------------------------------------
# 2. recognize_image / recognize_pdf_pages 的 include_blocks 分支
# ---------------------------------------------------------------------------

def test_recognize_image_include_blocks_adds_fields_and_keeps_legacy_default(monkeypatch):
    install_fake_infer(monkeypatch)

    legacy = run(ocr.recognize_image(png_bytes(400, 300)))
    assert set(legacy) == {"text", "lines", "width", "height", "elapsed_ms"}
    assert legacy["text"] == "\n".join(line["text"] for line in sample_lines())

    result = run(ocr.recognize_image(png_bytes(400, 300), include_blocks=True))
    assert {"blocks", "engine", "engine_version", "capabilities", "enrichment"} <= set(result)
    assert result["capabilities"] == {"blocks": True, "math_recognition": False}
    assert result["enrichment"] == {"formula": "off", "table": "off"}
    assert result["engine"] == ocr.ENGINE_NAME
    assert (result["width"], result["height"]) == (400, 300)
    assert len(result["blocks"]) == 5
    assert [block["kind"] for block in result["blocks"]] == ["text", "math", "table", "table", "text"]
    assert all(block["page"] == 1 for block in result["blocks"])


def test_recognize_pdf_pages_include_blocks_per_page(monkeypatch):
    install_fake_infer(monkeypatch)

    legacy_pages = run(ocr.recognize_pdf_pages(pdf_bytes(2), dpi=72))
    for page in legacy_pages:
        assert set(page) == {"page", "text", "lines", "width", "height", "elapsed_ms"}

    pages = run(ocr.recognize_pdf_pages(pdf_bytes(2), dpi=72, include_blocks=True))
    assert [page["page"] for page in pages] == [1, 2]
    for page in pages:
        assert {"blocks", "engine", "engine_version", "capabilities"} <= set(page)
        assert page["blocks"] and len(page["blocks"]) == 5
        assert all(block["page"] == page["page"] for block in page["blocks"])
        assert [block["order"] for block in page["blocks"]] == list(range(5))


def test_recognize_pdf_error_page_carries_empty_blocks(monkeypatch):
    install_fake_infer(monkeypatch)
    original_render = ocr._render_pdf_page

    def flaky_render(raw, page_no, dpi):
        if page_no == 2:
            raise RuntimeError("模拟渲染失败")
        return original_render(raw, page_no, dpi)

    monkeypatch.setattr(ocr, "_render_pdf_page", flaky_render)
    pages = run(ocr.recognize_pdf_pages(pdf_bytes(2), dpi=72, include_blocks=True))
    assert pages[0]["blocks"]
    assert "渲染失败" in pages[1]["error"]
    assert pages[1]["blocks"] == []
    assert pages[1]["capabilities"]["blocks"] is True


# ---------------------------------------------------------------------------
# 3. 引擎状态与能力声明
# ---------------------------------------------------------------------------

def test_engine_status_and_info_expose_blocks_capability(client, monkeypatch):
    monkeypatch.setattr(ocr, "_module_available", lambda: True)
    status = ocr.engine_status()
    assert status["engine_version"] == status["version"]
    assert status["capabilities"] == {"blocks": True, "math_recognition": False}
    assert "文字块" in status["notice"] and "STUDY_FORMULA_OCR" in status["notice"]
    assert status["enrichment"] == {"formula": "off", "table": "off"}
    assert status["formula"]["mode"] == "off" and status["table"]["mode"] == "off"
    assert ocr.BLOCK_KINDS == ("text", "math", "table", "image")
    assert {"BLOCK_KINDS", "BLOCKS_NOTICE", "CAPABILITIES", "build_blocks", "engine_meta"} <= set(ocr.__all__)

    payload = client.get("/api/ai/ocr/info").json()
    # 旧字段保持不变，新字段追加。
    for key in ("engine", "mode", "available", "version", "models", "limits", "notice"):
        assert key in payload
    assert payload["engine_version"] == payload["version"]
    assert payload["capabilities"]["math_recognition"] is False
    assert "STUDY_FORMULA_OCR" in payload["notice"]


# ---------------------------------------------------------------------------
# 4. POST /api/ai/ocr 响应新增 blocks / engine_version
# ---------------------------------------------------------------------------

def test_ocr_api_image_returns_blocks(client, monkeypatch):
    install_fake_infer(monkeypatch)
    response = client.post("/api/ai/ocr", files={
        "file": ("题图.png", png_bytes(400, 300), "image/png"),
    })
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["kind"] == "image"
    assert payload["text"] == "\n".join(line["text"] for line in sample_lines())
    assert [block["kind"] for block in payload["blocks"]] == ["text", "math", "table", "table", "text"]
    assert payload["blocks"][1]["latex"] is None and payload["blocks"][1]["needs_review"] is True
    assert payload["engine_version"] == payload["version"]
    assert payload["capabilities"]["blocks"] is True
    assert "本地 OCR" in payload["notice"] and "公式识别" in payload["notice"]


def test_ocr_api_pdf_returns_page_blocks_and_top_level_blocks(client, monkeypatch):
    install_fake_infer(monkeypatch)
    response = client.post("/api/ai/ocr", files={
        "file": ("扫描件.pdf", pdf_bytes(2), "application/pdf"),
    })
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["kind"] == "pdf" and payload["page_count"] == 2
    assert payload["blocks"]
    assert len(payload["blocks"]) == sum(len(page["blocks"]) for page in payload["pages"])
    for page in payload["pages"]:
        assert page["blocks"]
        assert all(block["page"] == page["page"] for block in page["blocks"])
    assert payload["engine_version"] == payload["version"]
    assert payload["capabilities"]["math_recognition"] is False


# ---------------------------------------------------------------------------
# 5. ocr-draft：草稿块与引擎元信息
# ---------------------------------------------------------------------------

def test_ocr_draft_contains_blocks_schema_and_engine_meta(client, monkeypatch):
    install_local_ocr(monkeypatch)
    uploaded = upload_image(client)
    body = post_ocr_draft(client, uploaded["id"])

    assert body["ocr_available"] is True
    assert [block["kind"] for block in body["blocks"]] == ["text", "math", "table", "table", "text"]
    assert body["draft"]["blocks"] == body["blocks"]
    assert body["draft"]["schema_version"] == upload_pipeline.INTAKE_BLOCKS_SCHEMA_VERSION
    assert body["engine"] == ocr.ENGINE_NAME
    assert "engine_version" in body and "capabilities" in body
    assert body["capabilities"]["math_recognition"] is False
    # 草稿落库，刷新/深链恢复时同样带块。
    state = client.get(f"/api/uploads/{uploaded['id']}/intake").json()
    assert state["draft"]["blocks"] == body["blocks"]
    assert state["draft"]["schema_version"] == upload_pipeline.INTAKE_BLOCKS_SCHEMA_VERSION


def test_ocr_draft_unavailable_still_200_with_empty_blocks(client, monkeypatch):
    async def unavailable(image=None, **kwargs):
        raise ocr.OcrUnavailable("本地 OCR 依赖不可用")

    monkeypatch.setattr(ocr, "recognize_image", unavailable)
    uploaded = upload_image(client)
    body = post_ocr_draft(client, uploaded["id"])

    assert body["ocr_available"] is False
    assert body["blocks"] == []
    assert body["draft"] is None
    assert body["capabilities"]["blocks"] is True
    assert body["engine"] == ocr.ENGINE_NAME
    assert "engine_version" in body
    assert "本地 OCR" in body["ocr_notice"]


def test_parse_question_text_only_attaches_blocks_when_processed():
    plain = upload_pipeline._parse_question_text("题干一行\n答案：B")
    assert "blocks" not in plain and "schema_version" not in plain
    assert plain["answers"] == ["B"]

    empty = upload_pipeline._parse_question_text("题干一行", blocks=[])
    assert empty["schema_version"] == upload_pipeline.INTAKE_BLOCKS_SCHEMA_VERSION
    assert empty["blocks"] == []

    block = {"id": "b1", "kind": "text", "text": "题干一行", "page": 1, "order": 0}
    attached = upload_pipeline._parse_question_text("题干一行", blocks=[block])
    assert attached["blocks"] == [block]
    assert attached["stem"] == "题干一行"


# ---------------------------------------------------------------------------
# 6. confirm：blocks 往返、非法 422、未传沿用、旧请求不追加键
# ---------------------------------------------------------------------------

def test_confirm_inherits_draft_blocks_then_accepts_explicit_override(client, monkeypatch):
    install_local_ocr(monkeypatch)
    uploaded = upload_image(client)
    draft = post_ocr_draft(client, uploaded["id"])

    inherited = client.patch(
        f"/api/uploads/{uploaded['id']}/confirm", json=confirm_payload(),
    )
    assert inherited.status_code == 200, inherited.text
    body = inherited.json()
    assert body["revision"] == 2
    assert body["confirmed"]["blocks"] == draft["blocks"]
    assert body["confirmed"]["schema_version"] == upload_pipeline.INTAKE_BLOCKS_SCHEMA_VERSION
    assert body["intake"]["confirmed"]["blocks"] == draft["blocks"]

    custom = [{
        "id": "b1", "kind": "math", "text": "人工校对公式 x^2 = 4", "latex": None,
        "needs_review": True, "confidence": 0.88,
        "bbox": {"x": 10, "y": 20, "w": 120, "h": 24},
        "norm": {"x": 0.025, "y": 0.0667, "w": 0.3, "h": 0.08},
        "page": 1, "order": 0,
    }]
    overridden = client.patch(
        f"/api/uploads/{uploaded['id']}/confirm",
        json=confirm_payload(expected_version=2, blocks=custom),
    )
    assert overridden.status_code == 200, overridden.text
    assert overridden.json()["revision"] == 3
    assert overridden.json()["confirmed"]["blocks"] == custom
    intake = client.get(f"/api/uploads/{uploaded['id']}/intake").json()
    assert intake["confirmed"]["blocks"] == custom
    assert intake["revision"] == 3

    # 显式传空列表表示「确认后没有块」，覆盖草稿块而不是沿用。
    emptied = client.patch(
        f"/api/uploads/{uploaded['id']}/confirm",
        json=confirm_payload(expected_version=3, blocks=[]),
    )
    assert emptied.status_code == 200, emptied.text
    assert emptied.json()["confirmed"]["blocks"] == []


@pytest.mark.parametrize("blocks", [
    [{}],
    [{"kind": "formula", "text": "x"}],
    [{"kind": "text", "text": "x" * 4001}],
    [{"kind": "math", "text": "x", "latex": "\\frac" * 500}],
    [{"kind": "text", "text": "x", "order": 501}],
    [{"kind": "text", "text": "x", "order": -1}],
    [{"kind": "text", "text": "x", "order": True}],
    [{"kind": "text", "text": "x", "page": 0}],
    [{"kind": "text", "text": "x", "page": 201}],
    [{"kind": "text", "text": 123}],
    [{"kind": "text", "text": "x", "bbox": "bad"}],
    [{"kind": "text", "text": "x", "norm": {"x": "bad"}}],
])
def test_confirm_rejects_invalid_blocks_with_chinese_422(client, blocks):
    uploaded = upload_image(client)
    response = client.patch(
        f"/api/uploads/{uploaded['id']}/confirm", json=confirm_payload(blocks=blocks),
    )
    assert response.status_code == 422
    assert "块" in response.text


def test_confirm_rejects_more_than_200_blocks(client):
    uploaded = upload_image(client)
    blocks = [{"kind": "text", "text": "x"} for _ in range(upload_pipeline.CONFIRM_BLOCKS_MAX + 1)]
    response = client.patch(
        f"/api/uploads/{uploaded['id']}/confirm", json=confirm_payload(blocks=blocks),
    )
    assert response.status_code == 422


def test_legacy_confirm_without_blocks_keeps_confirmed_json_clean(client):
    uploaded = client.post("/api/uploads/question", files={
        "file": ("旧流程.txt", "计算 1+1".encode("utf-8"), "text/plain"),
    }).json()
    response = client.patch(f"/api/uploads/{uploaded['id']}/confirm", json={
        "subject": "数学", "type": "计算题", "stem": "计算 1+1", "conditions": [],
    })
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["revision"] == 2
    # 旧草稿/旧请求没有 blocks：不得凭空追加空键，保持旧 confirmed JSON 原样。
    assert "blocks" not in body["confirmed"]
    assert "schema_version" not in body["confirmed"]
    intake = client.get(f"/api/uploads/{uploaded['id']}/intake").json()
    assert "blocks" not in intake["confirmed"]


# ---------------------------------------------------------------------------
# 7. 迟到识别：确认后只写 pending_draft，confirmed blocks/revision 不变
# ---------------------------------------------------------------------------

def test_late_recognition_keeps_confirmed_blocks_and_writes_pending_blocks(client, monkeypatch):
    install_local_ocr(monkeypatch)
    monkeypatch.setattr(practice, "search_similar_questions", lambda *args, **kwargs: [])
    provider = save_provider(client)
    user_id = client.get("/api/auth/me").json()["id"]
    uploaded = upload_image(client)

    manual_blocks = [{
        "id": "b1", "kind": "math", "text": "人工确认公式 x^2 = 4", "latex": None,
        "needs_review": True, "confidence": 0.9,
        "bbox": {"x": 0, "y": 0, "w": 60, "h": 20},
        "norm": {"x": 0, "y": 0, "w": 0.5, "h": 0.1},
        "page": 1, "order": 0,
    }]

    def confirm_during_recognition():
        response = client.patch(
            f"/api/uploads/{uploaded['id']}/confirm",
            json=confirm_payload(expected_version=1, blocks=manual_blocks),
        )
        assert response.status_code == 200, response.text

    async def fake_call(prov, model, prompt, images=None, json_output=False,
                        token_limit=5000, **kwargs):
        confirm_during_recognition()
        return {
            "content": json.dumps({
                "subject": "数学", "type": "计算题", "stem": "迟到的视觉识别题干",
                "options": [], "answers": ["2"], "conditions": [], "uncertain": [],
            }),
            "model": model, "provider_name": "mock", "usage": {},
        }

    monkeypatch.setattr(ai, "_call_model", fake_call)
    result = run(upload_pipeline._recognize_job(
        user_id,
        ai.JobInput(task="recognize", upload_id=uploaded["id"],
                    provider_id=provider["id"], model="vision-model"),
        {"id": provider["id"]}, None, job_id=901,
    ))

    assert result["draft_applied"] is False
    assert result["draft_conflict"] is True
    assert result["revision"] == 2
    assert result["blocks"] and result["blocks"][0]["source"] == "rapidocr"
    intake = client.get(f"/api/uploads/{uploaded['id']}/intake").json()
    # 人工确认内容（题干/块）与版本都不变。
    assert intake["status"] == "confirmed"
    assert intake["revision"] == 2
    assert intake["confirmed"]["stem"] == "求 x^2 = 0 的解"
    assert intake["confirmed"]["blocks"] == manual_blocks
    # 迟到的识别结果进入待确认草稿，并带本轮内容块。
    pending = intake["pending_draft"]
    assert pending["blocks"] and pending["blocks"][0]["source"] == "rapidocr"
    assert pending["blocks"] != manual_blocks
    assert pending["schema_version"] == upload_pipeline.INTAKE_BLOCKS_SCHEMA_VERSION
    assert pending["reason"]
