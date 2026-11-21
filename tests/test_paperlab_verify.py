"""试卷工坊 P2 验收：双模型识别交叉核验 + 本地 OCR 接入。

全部 mock，不联网、不依赖真实 Key：
- 两个「视觉模型」用 ``monkeypatch.setattr(ai, "_call_model", fake)`` 注入，按 model 名返回不同页结果；
- 本地 OCR 用注入 ``digitize._load_ocr_module`` 的假模块模拟（不 import 真实 app.ai.ocr）。

覆盖：
1) 双模型一致 / 不一致（题干相似度、选项集合、答案集合、flags、needs_review）；
2) 核验失败降级（不整任务失败、verify_error + 「未完成交叉核验」标注）；
3) 主/核验同模型 422、核验连接归属 404、缺核验参数 422、recognize 参数透传；
4) accept-verify（以核验模型/主模型为准）与 import 详情/list 的 diff/verify 字段；
5) 本地 OCR：/ocr 写页列、能力位与 503 引导、页码校验、OCR 提示注入识别 prompt；
6) OCR-only 草稿：无答案、needs_review、_source=local_ocr、不伪造结构、可幂等重跑、可入库溯源；
7) pl_pages / pl_import_items 新列迁移幂等（老库 DROP 列后重新补列）。
"""
import asyncio
import json
import types

from fastapi.testclient import TestClient
from PIL import Image
from pypdf import PdfWriter
from io import BytesIO

from app import ai, db
from app.ai import jobs as ai_jobs
from app.main import app
from app.paperlab import db as paperlab_db, digitize, schema

PRIMARY_MODEL = "mimo-v2.6-flash"
VERIFY_MODEL = "deepseek-flash"
PRIMARY_BASE = "https://api.xiaomimimo.com/v1"
VERIFY_BASE = "https://api.deepseek.com/v1"

SINGLE_Q = {
    "page_no": 1,
    "bbox": {"x": 0.05, "y": 0.05, "w": 0.9, "h": 0.2},
    "type": "SINGLE",
    "stem": "已知函数 $f(x)=x^2$，则 $f(2)$ 等于（  ）",
    "options": [{"label": "A", "text": "2"}, {"label": "B", "text": "4"}],
    "answers": ["B"],
    "explanation": "代入得 4。",
    "rubric": "",
    "knowledge_tags": ["函数"],
    "difficulty": 0.3,
    "score": 5,
    "images": [],
    "confidence": 0.9,
}
SUBJ_Q = {
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
    "confidence": 0.9,
}


def page_payload(*questions):
    return {
        "page_inventory": {"question_count": len(questions), "skip_count": 0},
        "questions": list(questions),
        "skipped": [],
    }


PAGE_AGREE = page_payload(SINGLE_Q, SUBJ_Q)
PAGE_DIFF = page_payload(
    {
        **SINGLE_Q,
        "stem": "下列说法中，完全不同的题干内容（  ）",
        "options": [
            {"label": "A", "text": "甲"},
            {"label": "B", "text": "乙"},
            {"label": "C", "text": "丙"},
        ],
        "answers": ["C"],
    },
    SUBJ_Q,
)
PAGE_FEWER = page_payload(SUBJ_Q)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def user_id(client) -> int:
    return client.get("/api/auth/me").json()["id"]


def save_provider(client, *, name="识别连接", base_url=PRIMARY_BASE, model=PRIMARY_MODEL):
    response = client.post("/api/providers", json={
        "name": name, "kind": "openai", "base_url": base_url,
        "api_key": "sk-paperlab-verify-unit-test", "models": [model],
    })
    assert response.status_code == 200, response.text
    return response.json()


def png_bytes(width=800, height=1200):
    image = Image.new("RGB", (width, height), (255, 255, 255))
    buffer = BytesIO()
    image.save(buffer, "PNG")
    return buffer.getvalue()


def pdf_bytes(page_count=2):
    writer = PdfWriter()
    for _ in range(page_count):
        writer.add_blank_page(width=600, height=800)
    buffer = BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def upload_png(client, count=1, **form):
    response = client.post(
        "/api/paperlab/imports",
        files=[
            ("files", (f"第{index + 1}页.png", png_bytes(), "image/png"))
            for index in range(count)
        ],
        data=form,
    )
    assert response.status_code == 200, response.text
    return response.json()["import"]


def upload_pdf(client, count=2, **form):
    response = client.post(
        "/api/paperlab/imports",
        files=[("files", ("试卷.pdf", pdf_bytes(count), "application/pdf"))],
        data=form,
    )
    assert response.status_code == 200, response.text
    return response.json()["import"]


def insert_import_job(client, import_id, *, provider_id, page_from=1, page_to=1,
                      model=PRIMARY_MODEL, cross_verify=False,
                      verify_provider_id=None, verify_model=None):
    uid = user_id(client)
    request = {
        "task": "paper-import", "provider_id": provider_id, "model": model,
        "cross_verify": cross_verify,
        "verify_provider_id": verify_provider_id,
        "verify_model": verify_model,
        "params": {"import_id": import_id, "page_from": page_from, "page_to": page_to},
    }
    now = db.utcnow()
    with db.connection() as con:
        job_id = con.execute(
            "INSERT INTO ai_jobs(user_id,task,status,stage,request_json,partial_content,"
            "cancelled,created_at,updated_at,dispatch_token) VALUES (?,?,?,?,?,'',0,?,?,?)",
            (uid, "paper-import", "queued", "queued",
             json.dumps(request, ensure_ascii=False), now, now, "verify-token"),
        ).lastrowid
    return job_id, ai_jobs.JobInput(**request)


def run_job(client, job_id, data):
    uid = user_id(client)
    provider = ai._selected_provider(uid, data.provider_id, data.model)
    verifier = None
    if data.cross_verify and data.verify_provider_id and data.verify_model:
        verifier = ai._selected_provider(uid, data.verify_provider_id, data.verify_model)
    return asyncio.run(digitize.run_import_job(job_id, uid, data, provider, verifier))


def import_detail(client, import_id):
    response = client.get(f"/api/paperlab/imports/{import_id}")
    assert response.status_code == 200, response.text
    return response.json()


def job_row(job_id):
    with db.connection() as con:
        return dict(con.execute("SELECT * FROM ai_jobs WHERE id=?", (job_id,)).fetchone())


async def no_dispatch(job_id, token, runner):
    return None


def patch_dispatch(monkeypatch):
    monkeypatch.setattr(ai_jobs, "_dispatch", no_dispatch)


class DualVision:
    """按模型名返回不同页结果的假视觉模型（可注入异常 / 取消回调）。"""

    def __init__(self, *, primary=None, verify=None, on_call=None):
        self.primary = primary if primary is not None else PAGE_AGREE
        self.verify = verify if verify is not None else PAGE_AGREE
        self.on_call = on_call
        self.calls = []

    async def call(self, provider, model, prompt, images=None, json_output=False,
                   token_limit=5000, **kwargs):
        index = len(self.calls)
        self.calls.append({"model": model, "prompt": prompt, "images": images})
        if self.on_call:
            self.on_call(index, model, prompt)
        item = self.verify if model == VERIFY_MODEL else self.primary
        if isinstance(item, Exception):
            raise item
        if callable(item):
            item = item(prompt, model)
        content = item if isinstance(item, str) else json.dumps(item, ensure_ascii=False)
        return {"content": content, "model": model, "provider_name": "mock", "usage": {}}

    def install(self, monkeypatch):
        monkeypatch.setattr(ai, "_call_model", self.call)
        return self


def install_cross_import(client, monkeypatch, *, page_from=1, page_to=1, **job_kwargs):
    imported = upload_png(client, page_to)
    primary = save_provider(client, name="主模型连接", base_url=PRIMARY_BASE, model=PRIMARY_MODEL)
    verify = save_provider(client, name="核验连接", base_url=VERIFY_BASE, model=VERIFY_MODEL)
    job_id, data = insert_import_job(
        client, imported["id"], provider_id=primary["id"],
        page_from=page_from, page_to=page_to,
        cross_verify=True, verify_provider_id=verify["id"], verify_model=VERIFY_MODEL,
        **job_kwargs,
    )
    return imported, primary, verify, job_id, data


# ---------------------------------------------------------------------------
# 0. schema 纯函数：diff 规则
# ---------------------------------------------------------------------------

def test_diff_helpers_normalize_text_and_sets():
    left = "已知函数 $f(x)=x^2$，则 $f(2)$ 等于（  ）"
    right = "已知函数 f(x)=x^2 , 则 f(2) 等于?"
    assert schema.normalize_compare_text(left) == schema.normalize_compare_text(right)
    assert schema.stem_similarity(left, right) == 1.0
    # 数学运算符不同必须判为不同（不能把 x+1 与 x-1 归一成一样）
    assert schema.stem_similarity("x+1", "x-1") < 1.0
    assert schema.stem_similarity("", "") == 1.0
    assert schema.stem_similarity("abc", "") == 0.0
    # 选项集合：顺序无关、大小写/全半角归一
    options_a = [{"label": "A", "text": "2"}, {"label": "B", "text": "4"}]
    options_b = [{"label": "b", "text": "４"}, {"label": "a", "text": "２"}]
    assert schema.draft_option_signature({"options": options_a}) == \
        schema.draft_option_signature({"options": options_b})
    # 答案集合：大小写归一
    assert schema.draft_answer_signature({"answers": ["b"]}) == \
        schema.draft_answer_signature({"answers": ["B"]})
    # MIXED 子题的答案参与比较
    mixed = {"answers": [], "parts": [{"index": 1, "answers": ["A"]}]}
    assert schema.draft_answer_signature(mixed) == ["p1:a"]


def test_compare_page_results_count_mismatch():
    comparison = schema.compare_page_results(PAGE_AGREE, PAGE_FEWER)
    assert comparison["primary_count"] == 2
    assert comparison["verify_count"] == 1
    assert comparison["count_mismatch"] is True
    entries = comparison["entries"]
    assert entries[1]["draft"] is None
    assert "missing_in_verify" in entries[1]["diff"]["flags"]
    diff = schema.compare_item_drafts(SINGLE_Q, PAGE_DIFF["questions"][0])
    assert diff["stem_ratio"] < 1.0
    assert diff["options_equal"] is False
    assert diff["answers_equal"] is False
    assert {"stem_mismatch", "options_mismatch", "answers_mismatch"} <= set(diff["flags"])


# ---------------------------------------------------------------------------
# 1. 双模型一致 / 不一致 / 核验失败降级
# ---------------------------------------------------------------------------

def test_cross_verify_models_agree(client, monkeypatch):
    imported, primary, verify, job_id, data = install_cross_import(client, monkeypatch)
    fake = DualVision(primary=PAGE_AGREE, verify=PAGE_AGREE).install(monkeypatch)
    result = run_job(client, job_id, data)

    assert not result.get("cancelled"), "双模型一致时不应取消"
    assert result["cross_verify"] is True
    assert result["status"] == "ready"
    assert result["verify"]["enabled"] is True
    assert result["verify"]["model"] == VERIFY_MODEL
    assert result["verify"]["counts"]["verified"] == 2
    assert result["verify"]["counts"]["diff"] == 0
    assert result["verify"]["counts"]["failed"] == 0
    assert [call["model"] for call in fake.calls] == [PRIMARY_MODEL, VERIFY_MODEL]
    assert fake.calls[1]["images"], "核验模型也必须独立读取页面图"

    detail = import_detail(client, imported["id"])
    items = detail["items"]
    assert len(items) == 2
    for item in items:
        assert item["verify_status"] == "verified"
        assert item["diff"]["flags"] == []
        assert item["verify"]["model"] == VERIFY_MODEL
        assert item["verify"]["draft"]["stem"]
        assert item["verify_error"] == ""
    # 第一题置信度 0.9 且双模型一致 → 无需复核
    assert items[0]["needs_review"] is False
    assert detail["verify_counts"]["verified"] == 2
    # 列表页也带核验汇总（只增字段）
    listing = client.get("/api/paperlab/imports").json()
    row = next(entry for entry in listing["items"] if entry["id"] == imported["id"])
    assert row["verify_summary"]["verified"] == 2
    assert row["verify_summary"]["total"] == 2


def test_cross_verify_diff_marks_needs_review(client, monkeypatch):
    imported, primary, verify, job_id, data = install_cross_import(client, monkeypatch)
    DualVision(primary=PAGE_AGREE, verify=PAGE_DIFF).install(monkeypatch)
    result = run_job(client, job_id, data)

    assert result["status"] == "ready"
    assert result["verify"]["counts"]["diff"] == 1
    assert result["verify"]["counts"]["verified"] == 1

    items = import_detail(client, imported["id"])["items"]
    first, second = items
    assert first["verify_status"] == "diff"
    assert first["needs_review"] is True
    assert first["status"] == "pending"
    diff = first["diff"]
    assert diff["stem_ratio"] < 0.9
    assert diff["options_equal"] is False
    assert diff["answers_equal"] is False
    assert {"stem_mismatch", "options_mismatch", "answers_mismatch"} <= set(diff["flags"])
    assert "双模型核验不一致" in first["review_reason"]
    # 核验模型草稿随 item 返回，供前端三栏差异面板
    assert first["verify"]["draft"]["stem"].startswith("下列说法中")
    assert first["verify"]["draft"]["answers"] == ["C"]
    # 第二题一致，不额外标记
    assert second["verify_status"] == "verified"
    assert second["diff"]["flags"] == []


def test_cross_verify_count_mismatch_flagged(client, monkeypatch):
    imported, primary, verify, job_id, data = install_cross_import(client, monkeypatch)
    DualVision(primary=PAGE_AGREE, verify=PAGE_FEWER).install(monkeypatch)
    result = run_job(client, job_id, data)

    assert result["verify"]["counts"]["diff"] >= 1
    assert any("数量不一致" in warning for warning in result["warnings"])
    items = import_detail(client, imported["id"])["items"]
    assert items[1]["verify_status"] == "diff"
    assert "missing_in_verify" in items[1]["diff"]["flags"]
    assert items[1]["needs_review"] is True


def test_verify_failure_degrades_keeps_primary_result(client, monkeypatch):
    imported, primary, verify, job_id, data = install_cross_import(client, monkeypatch)
    DualVision(
        primary=PAGE_AGREE,
        verify=RuntimeError("核验模型服务暂不可用"),
    ).install(monkeypatch)
    result = run_job(client, job_id, data)

    # 核验失败不整任务失败：主结果照常保存、状态 ready
    assert result["status"] == "ready"
    assert result["failed_pages"] == []
    assert result["cross_verify"] is True
    assert result["verify"]["counts"]["failed"] == 2
    assert any("未完成交叉核验" in warning for warning in result["warnings"])
    assert job_row(job_id)["status"] != "failed"

    items = import_detail(client, imported["id"])["items"]
    assert len(items) == 2
    for item in items:
        assert item["verify_status"] == "failed"
        assert "核验模型服务暂不可用" in item["verify_error"]
        assert item["needs_review"] is True
        assert "未完成交叉核验" in item["review_reason"]
        assert item["draft"]["stem"], "核验失败仍保留主模型草稿"
    assert items[0]["draft"]["answers"] == ["B"]


# ---------------------------------------------------------------------------
# 2. 触发接口：同模型 422 / 归属 404 / 参数透传
# ---------------------------------------------------------------------------

def test_recognize_rejects_same_model_and_missing_verify(client, monkeypatch):
    patch_dispatch(monkeypatch)
    imported = upload_png(client)
    primary = save_provider(client, name="主模型连接", base_url=PRIMARY_BASE, model=PRIMARY_MODEL)
    url = f"/api/paperlab/imports/{imported['id']}/recognize"

    same = client.post(url, json={
        "provider_id": primary["id"], "model": PRIMARY_MODEL,
        "cross_verify": True, "verify_provider_id": primary["id"],
        "verify_model": PRIMARY_MODEL,
    })
    assert same.status_code == 422
    assert "不构成交叉核验" in same.json()["detail"]

    missing = client.post(url, json={
        "provider_id": primary["id"], "model": PRIMARY_MODEL, "cross_verify": True,
    })
    assert missing.status_code == 422
    assert "核验" in missing.json()["detail"]


def test_recognize_rejects_foreign_verify_provider(client, second_client, monkeypatch):
    patch_dispatch(monkeypatch)
    imported = upload_png(client)
    primary = save_provider(client, name="主模型连接", base_url=PRIMARY_BASE, model=PRIMARY_MODEL)
    foreign = save_provider(second_client, name="他人核验连接", base_url=VERIFY_BASE, model=VERIFY_MODEL)
    response = client.post(f"/api/paperlab/imports/{imported['id']}/recognize", json={
        "provider_id": primary["id"], "model": PRIMARY_MODEL,
        "cross_verify": True, "verify_provider_id": foreign["id"],
        "verify_model": VERIFY_MODEL,
    })
    assert response.status_code == 404


def test_recognize_passes_cross_verify_params(client, monkeypatch):
    patch_dispatch(monkeypatch)
    imported = upload_png(client)
    primary = save_provider(client, name="主模型连接", base_url=PRIMARY_BASE, model=PRIMARY_MODEL)
    verify = save_provider(client, name="核验连接", base_url=VERIFY_BASE, model=VERIFY_MODEL)
    response = client.post(f"/api/paperlab/imports/{imported['id']}/recognize", json={
        "provider_id": primary["id"], "model": PRIMARY_MODEL,
        "cross_verify": True, "verify_provider_id": verify["id"],
        "verify_model": VERIFY_MODEL,
    })
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["cross_verify"] is True
    assert payload["verify_model"] == VERIFY_MODEL
    request = json.loads(job_row(payload["job"]["id"])["request_json"])
    assert request["cross_verify"] is True
    assert request["verify_provider_id"] == verify["id"]
    assert request["verify_model"] == VERIFY_MODEL
    assert request["params"] == {"import_id": imported["id"], "page_from": 1, "page_to": 1}


# ---------------------------------------------------------------------------
# 3. accept-verify 人工确认
# ---------------------------------------------------------------------------

def test_accept_verify_endpoint(client, monkeypatch):
    imported, primary, verify, job_id, data = install_cross_import(client, monkeypatch)
    DualVision(primary=PAGE_AGREE, verify=PAGE_DIFF).install(monkeypatch)
    run_job(client, job_id, data)
    items = import_detail(client, imported["id"])["items"]
    diff_item, agree_item = items
    base_url = f"/api/paperlab/imports/{imported['id']}/items"

    accepted_verify = client.post(
        f"{base_url}/{diff_item['id']}/accept-verify", json={"source": "verify"},
    )
    assert accepted_verify.status_code == 200, accepted_verify.text
    updated = accepted_verify.json()["item"]
    assert accepted_verify.json()["accepted"] == "verify"
    assert updated["draft"]["stem"].startswith("下列说法中")
    assert updated["needs_review"] is False
    assert updated["verify_status"] == "accepted"
    assert updated["source"] == "manual"
    assert updated["draft"]["_ai"]["model"] == VERIFY_MODEL

    accepted_primary = client.post(
        f"{base_url}/{agree_item['id']}/accept-verify", json={"source": "primary"},
    )
    assert accepted_primary.status_code == 200, accepted_primary.text
    kept = accepted_primary.json()["item"]
    assert kept["draft"]["answers"] == []
    assert kept["verify_status"] == "accepted"
    assert kept["needs_review"] is False

    bad_source = client.post(
        f"{base_url}/{diff_item['id']}/accept-verify", json={"source": "both"},
    )
    assert bad_source.status_code == 422


def test_accept_verify_ownership_and_state(client, second_client, monkeypatch):
    imported, primary, verify, job_id, data = install_cross_import(client, monkeypatch)
    DualVision(primary=PAGE_AGREE, verify=PAGE_DIFF).install(monkeypatch)
    run_job(client, job_id, data)
    item = import_detail(client, imported["id"])["items"][0]
    url = f"/api/paperlab/imports/{imported['id']}/items/{item['id']}/accept-verify"

    assert second_client.post(url, json={"source": "primary"}).status_code == 404
    # 未核验的草稿不能确认
    plain = upload_png(client)
    plain_item = None
    job_id2, data2 = insert_import_job(
        client, plain["id"], provider_id=primary["id"], cross_verify=False,
    )
    DualVision(primary=PAGE_AGREE, verify=PAGE_AGREE).install(monkeypatch)
    run_job(client, job_id2, data2)
    plain_item = import_detail(client, plain["id"])["items"][0]
    pending = client.post(
        f"/api/paperlab/imports/{plain['id']}/items/{plain_item['id']}/accept-verify",
        json={"source": "primary"},
    )
    assert pending.status_code == 422


# ---------------------------------------------------------------------------
# 4. 本地 OCR：能力位 / 提取文字 / 503 引导 / 页码校验
# ---------------------------------------------------------------------------

OCR_TEXT = (
    "一、选择题\n"
    "1. 已知函数 f(x)=x^2，则 f(2) 的值为（  ）\n"
    "A. 2    B. 4\n"
    "\n"
    "2. 计算：1+2+3+…+100 = ______。\n"
)
OCR_LINES = [
    {"text": "一、选择题", "box": [[80, 120], [280, 120], [280, 170], [80, 170]], "score": 0.98},
    {"text": "1. 已知函数 f(x)=x^2，则 f(2) 的值为（  ）",
     "box": [[80, 220], [900, 220], [900, 280], [80, 280]], "score": 0.96},
    {"text": "A. 2    B. 4", "box": [[80, 320], [500, 320], [500, 370], [80, 370]], "score": 0.95},
    {"text": "2. 计算：1+2+3+…+100 = ______。",
     "box": [[80, 520], [900, 520], [900, 580], [80, 580]], "score": 0.93},
]


def fake_ocr_module(*, available=True, text=OCR_TEXT, lines=OCR_LINES, error=None):
    module = types.SimpleNamespace()
    module.local_ocr_available = lambda: available
    module.engine_status = lambda: {"engine": "rapidocr", "version": "1.4.4", "models": "ch"}
    module.calls = []

    def recognize_image(data, *args, **kwargs):
        module.calls.append({"bytes": len(data) if data else 0})
        if error is not None:
            raise error
        return {
            "text": text, "lines": lines, "width": 1200, "height": 1680, "elapsed_ms": 12,
        }

    module.recognize_image = recognize_image
    return module


def install_fake_ocr(monkeypatch, module=None):
    module = module or fake_ocr_module()
    monkeypatch.setattr(digitize, "_load_ocr_module", lambda: module)
    return module


def test_ocr_info_and_extract_writes_page_columns(client, monkeypatch):
    module = install_fake_ocr(monkeypatch)
    imported = upload_png(client)

    info = client.get("/api/paperlab/ocr/info")
    assert info.status_code == 200
    assert info.json()["available"] is True
    assert info.json()["max_pages"] == 5
    assert info.json()["engine"] == "rapidocr"

    response = client.post(f"/api/paperlab/imports/{imported['id']}/ocr", json={"pages": [1]})
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["pages"][0]["page_no"] == 1
    assert payload["pages"][0]["chars"] > 0
    assert payload["pages"][0]["line_count"] >= 4
    assert payload["errors"] == []
    assert module.calls, "本地 OCR 应被调用"

    detail = import_detail(client, imported["id"])
    page = detail["pages"][0]
    assert page["has_ocr"] is True
    assert page["ocr_engine"] == "rapidocr"
    assert page["ocr_at"]
    assert page["ocr_chars"] > 0
    assert page["ocr_line_count"] >= 4
    assert "已知函数" in page["ocr_preview"]
    # 行框已归一化为 0~1
    with db.connection() as con:
        raw = con.execute(
            "SELECT ocr_lines_json FROM pl_pages WHERE import_id=? AND page_no=1",
            (imported["id"],),
        ).fetchone()[0]
    lines = json.loads(raw)
    assert lines and 0 <= lines[0]["bbox"]["x"] <= 1 and 0 < lines[0]["bbox"]["w"] <= 1


def test_ocr_unavailable_returns_503_with_guidance(client, monkeypatch):
    monkeypatch.setattr(digitize, "_load_ocr_module", lambda: None)
    imported = upload_png(client)
    info = client.get("/api/paperlab/ocr/info")
    assert info.status_code == 200
    assert info.json()["available"] is False
    assert "未安装" in info.json()["notice"]

    response = client.post(f"/api/paperlab/imports/{imported['id']}/ocr", json={})
    assert response.status_code == 503
    assert "本地 OCR 不可用" in response.json()["detail"]

    draft = client.post(f"/api/paperlab/imports/{imported['id']}/ocr-draft", json={})
    assert draft.status_code == 503

    # 能力位在导入详情里同样可见（前端据此禁用按钮）
    detail = import_detail(client, imported["id"])
    assert detail["ocr"]["available"] is False


def test_ocr_engine_disabled_and_page_validation(client, monkeypatch):
    install_fake_ocr(monkeypatch, fake_ocr_module(available=False))
    imported = upload_png(client)
    assert client.get("/api/paperlab/ocr/info").json()["available"] is False
    disabled = client.post(f"/api/paperlab/imports/{imported['id']}/ocr", json={})
    assert disabled.status_code == 503
    assert "未就绪" in disabled.json()["detail"]

    install_fake_ocr(monkeypatch)
    bad_page = client.post(f"/api/paperlab/imports/{imported['id']}/ocr", json={"pages": [9]})
    assert bad_page.status_code == 422
    assert "页码" in bad_page.json()["detail"]
    unknown_import = client.post("/api/paperlab/imports/9999/ocr", json={})
    assert unknown_import.status_code == 404


def test_ocr_pages_over_limit_requires_batch(client, monkeypatch):
    install_fake_ocr(monkeypatch)
    imported = upload_pdf(client, count=6)
    response = client.post(f"/api/paperlab/imports/{imported['id']}/ocr", json={})
    assert response.status_code == 422
    assert "5" in response.json()["detail"]


def test_ocr_text_injected_into_recognize_prompt(client, monkeypatch):
    install_fake_ocr(monkeypatch)
    imported = upload_png(client)
    assert client.post(
        f"/api/paperlab/imports/{imported['id']}/ocr", json={"pages": [1]},
    ).status_code == 200
    primary = save_provider(client, name="主模型连接", base_url=PRIMARY_BASE, model=PRIMARY_MODEL)
    fake = DualVision(primary=PAGE_AGREE, verify=PAGE_AGREE).install(monkeypatch)
    job_id, data = insert_import_job(client, imported["id"], provider_id=primary["id"])
    run_job(client, job_id, data)

    prompt = fake.calls[0]["prompt"]
    assert "机器 OCR" in prompt
    assert "以页面图像为准" in prompt
    assert "已知函数 f(x)=x^2" in prompt
    assert fake.calls[0]["images"], "识别仍以页面图为主输入"


# ---------------------------------------------------------------------------
# 5. OCR-only 草稿（无视觉模型降级）：不生成答案、不伪造结构、可幂等重跑、可入库
# ---------------------------------------------------------------------------

def test_ocr_draft_blank_line_no_answers_and_idempotent(client, monkeypatch):
    module = install_fake_ocr(monkeypatch)
    imported = upload_png(client)
    url = f"/api/paperlab/imports/{imported['id']}/ocr-draft"

    first = client.post(url, json={"split": "blank_line"})
    assert first.status_code == 200, first.text
    payload = first.json()
    assert payload["created"] == 2
    assert payload["import_status"] == "ready"
    assert payload["split"] == "blank_line"
    assert module.calls, "缺 OCR 时应自动先提取文字"
    assert all(item["draft"]["answers"] == [] for item in payload["items"])
    assert all(item["draft"]["type"] == "SUBJECTIVE" for item in payload["items"])
    assert all(item["draft"]["options"] == [] for item in payload["items"])
    assert all(item["draft"]["_source"] == "local_ocr" for item in payload["items"])
    assert all(item["needs_review"] is True for item in payload["items"])
    assert all(item["status"] == "pending" for item in payload["items"])
    assert all(item["source"] == "local_ocr" for item in payload["items"])
    assert all(item["verify_status"] == "pending" for item in payload["items"])
    for item in payload["items"]:
        bbox = item["draft"]["bbox"]
        assert 0 <= bbox["x"] < 1 and 0 < bbox["w"] <= 1
        assert "OCR" in item["review_reason"]
    stems = [item["draft"]["stem"] for item in payload["items"]]
    assert stems[0].startswith("一、选择题")
    assert "计算" in stems[1]

    # 幂等：重跑同页只替换 OCR 草稿，不重复堆积
    second = client.post(url, json={"split": "blank_line"})
    assert second.status_code == 200
    assert second.json()["created"] == 2
    items = import_detail(client, imported["id"])["items"]
    assert len(items) == 2
    assert [item["item_no"] for item in items] == [1, 2]


def test_ocr_draft_lines_split_and_invalid_mode(client, monkeypatch):
    install_fake_ocr(monkeypatch)
    imported = upload_png(client)
    url = f"/api/paperlab/imports/{imported['id']}/ocr-draft"
    lines = client.post(url, json={"split": "lines"})
    assert lines.status_code == 200, lines.text
    assert lines.json()["created"] == len([line for line in OCR_TEXT.splitlines() if line.strip()])

    bad = client.post(url, json={"split": "paragraph"})
    assert bad.status_code == 422
    assert "合并方式" in bad.json()["detail"]


def test_ocr_draft_commit_keeps_empty_answers_and_provenance(client, monkeypatch):
    install_fake_ocr(monkeypatch)
    imported = upload_png(client, subject="数学", grade="高中一年级")
    payload = client.post(
        f"/api/paperlab/imports/{imported['id']}/ocr-draft", json={"split": "blank_line"},
    ).json()
    item = payload["items"][0]
    accepted = client.patch(
        f"/api/paperlab/imports/{imported['id']}/items/{item['id']}",
        json={"status": "accepted"},
    )
    assert accepted.status_code == 200, accepted.text
    commit = client.post(f"/api/paperlab/imports/{imported['id']}/commit", json={})
    assert commit.status_code == 200, commit.text
    created = commit.json()["created_question_ids"]
    assert len(created) == 1, "缺省只入库人工已接受的草稿"
    question = client.get(f"/api/paperlab/questions/{created[0]}").json()["question"]
    assert question["content"]["answers"] == []
    assert question["content"]["source"]["origin"] == "local_ocr"
    assert question["content"]["ocr"]["split"] == "blank_line"
    # 不是 AI 产物：没有 AI 元数据，也不是「已核验」
    assert "ai" not in question["content"]
    assert question["ai_model"] == ""
    assert question["verify_status"] == "pending"


# ---------------------------------------------------------------------------
# 6. 迁移幂等 / 取消
# ---------------------------------------------------------------------------

def test_page_and_item_columns_migration_idempotent(client):
    def columns(table):
        with db.connection() as con:
            return {row[1] for row in con.execute(f'PRAGMA table_info("{table}")')}

    assert {"ocr_text", "ocr_lines_json", "ocr_engine", "ocr_at"} <= columns("pl_pages")
    assert {"verify_status", "verify_json", "diff_json", "verify_error"} <= columns("pl_import_items")

    # 模拟老库（P2 之前）：删掉增量列后再次 init_tables 应幂等补回
    with db.connection() as con:
        for column in ("ocr_text", "ocr_lines_json", "ocr_engine", "ocr_at"):
            con.execute(f'ALTER TABLE pl_pages DROP COLUMN "{column}"')
        for column in ("verify_status", "verify_json", "diff_json", "verify_error"):
            con.execute(f'ALTER TABLE pl_import_items DROP COLUMN "{column}"')
    paperlab_db.init_tables()
    paperlab_db.init_tables()
    assert {"ocr_text", "ocr_lines_json", "ocr_engine", "ocr_at"} <= columns("pl_pages")
    assert {"verify_status", "verify_json", "diff_json", "verify_error"} <= columns("pl_import_items")


def test_cross_verify_cancel_keeps_primary_drafts(client, monkeypatch):
    imported = upload_pdf(client, count=2)
    primary = save_provider(client, name="主模型连接", base_url=PRIMARY_BASE, model=PRIMARY_MODEL)
    verify = save_provider(client, name="核验连接", base_url=VERIFY_BASE, model=VERIFY_MODEL)
    job_id, data = insert_import_job(
        client, imported["id"], provider_id=primary["id"], page_from=1, page_to=2,
        cross_verify=True, verify_provider_id=verify["id"], verify_model=VERIFY_MODEL,
    )

    def cancel_after_primary(index, model, prompt):
        if index == 0:
            with db.connection() as con:
                con.execute("UPDATE ai_jobs SET cancelled=1 WHERE id=?", (job_id,))

    fake = DualVision(
        primary=PAGE_AGREE, verify=PAGE_AGREE, on_call=cancel_after_primary,
    ).install(monkeypatch)
    result = run_job(client, job_id, data)

    assert result["cancelled"] is True
    assert result["done_pages"] == [1]
    assert job_row(job_id)["status"] == "cancelled"
    assert all(call["model"] == PRIMARY_MODEL for call in fake.calls), \
        "取消后不应继续调用核验模型"
    items = import_detail(client, imported["id"])["items"]
    assert items and all(item["page_no"] == 1 for item in items)
    assert all(item["verify_status"] == "failed" for item in items)
    assert all("取消" in item["verify_error"] for item in items)


def test_verify_counts_require_known_import(client):
    assert client.get("/api/paperlab/imports/9999").status_code == 404
    assert client.post("/api/paperlab/imports/9999/ocr-draft", json={}).status_code == 404


def test_recognize_requires_login(client):
    anonymous = TestClient(app)
    try:
        assert anonymous.post("/api/paperlab/imports/1/ocr", json={}).status_code == 401
        assert anonymous.post("/api/paperlab/imports/1/ocr-draft", json={}).status_code == 401
        assert anonymous.post(
            "/api/paperlab/imports/1/items/1/accept-verify", json={},
        ).status_code == 401
        assert anonymous.get("/api/paperlab/ocr/info").status_code == 401
    finally:
        anonymous.close()
