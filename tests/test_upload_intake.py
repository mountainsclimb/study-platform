"""WS9 拍题入库 OCR-first 主路径（Phase 1）验收。

覆盖：
1. 无视觉模型 Key：上传 → 本地 OCR 草稿 → 人工确认（expected_version 乐观锁）→ 保存我的题库
   全链路（TestClient + monkeypatch 本地 OCR），原图按 paperlab content 结构与资产归属可读；
2. 人工确认后异步识别回写不覆盖人工字段：识别期间/之前发生确认时，结果只进「模型草稿对照」；
3. expected_version 冲突返回 409，旧前端不带该字段仍兼容；
4. 保存失败显式提示并可重试；
5. 本地 OCR 不可用时的降级（保留原图、可手录确认保存）；
6. AI 建议答案与 OCR 答案分开展示，且不覆盖人工确认；
7. 任务中心深链与前端 OCR-first 逻辑的静态断言（node --check 另行执行）。
"""
import asyncio
from io import BytesIO
import json

from fastapi.testclient import TestClient
from PIL import Image
import pytest

from app import ai, practice
from app.ai import ocr, upload_pipeline
from app.paperlab import bank as paperlab_bank


OCR_TEXT = (
    "1. 下列关于光合作用的说法正确的是\n"
    "A. 只在叶绿体中进行\n"
    "B. 需要光照\n"
    "C. 不产生氧气\n"
    "D. 与呼吸作用无关\n"
    "答案：B"
)


# ---------------------------------------------------------------------------
# 公共假实现与辅助
# ---------------------------------------------------------------------------

def png_bytes(width=320, height=200, color="white") -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (width, height), color).save(buffer, "PNG")
    return buffer.getvalue()


def save_provider(client, name="拍题入库测试连接"):
    response = client.post("/api/providers", json={
        "name": name, "kind": "openai", "base_url": "https://api.openai.com/v1",
        "api_key": "sk-intake-unit-test", "models": ["vision-model"],
    })
    assert response.status_code == 200, response.text
    return response.json()


def user_id_of(client):
    return client.get("/api/auth/me").json()["id"]


def upload_image(client, name="手机拍题.png"):
    response = client.post("/api/uploads/question", files={
        "file": (name, png_bytes(), "image/png"),
    })
    assert response.status_code == 200, response.text
    return response.json()


def install_local_ocr(monkeypatch, text=OCR_TEXT):
    calls = []

    async def fake_recognize(image=None, **kwargs):
        calls.append(image)
        return {"text": text, "lines": [], "width": 320, "height": 200, "elapsed_ms": 3}

    monkeypatch.setattr(ocr, "recognize_image", fake_recognize)
    return calls


def no_similar(monkeypatch):
    monkeypatch.setattr(practice, "search_similar_questions", lambda *a, **k: [])


def ocr_draft(client, upload_id):
    response = client.post(f"/api/uploads/{upload_id}/ocr-draft")
    assert response.status_code == 200, response.text
    return response.json()


def confirm(client, upload_id, *, expected_version=None, **overrides):
    payload = {
        "subject": "生物",
        "type": "单选题",
        "stem": "下列关于光合作用的说法正确的是",
        "options": [
            {"label": "A", "text": "只在叶绿体中进行"},
            {"label": "B", "text": "需要光照"},
            {"label": "C", "text": "不产生氧气"},
            {"label": "D", "text": "与呼吸作用无关"},
        ],
        "conditions": [],
        "answers": ["B"],
        "explanation": "光合作用需要光照。",
        "knowledge_tags": ["光合作用"],
    }
    payload.update(overrides)
    if expected_version is not None:
        payload["expected_version"] = expected_version
    return client.patch(f"/api/uploads/{upload_id}/confirm", json=payload)


def save_to_bank(client, upload_id, **payload):
    return client.post(f"/api/uploads/{upload_id}/save-to-bank", json=payload)


# ---------------------------------------------------------------------------
# 1. OCR-first 全链路（无模型 Key）
# ---------------------------------------------------------------------------

def test_ocr_first_intake_full_chain_without_model_key(client, monkeypatch):
    """上传 → 本地 OCR 草稿 → 人工确认 → 保存我的题库；全程不创建/调用视觉模型。"""
    calls = install_local_ocr(monkeypatch)
    no_similar(monkeypatch)
    assert client.get("/api/providers").json()["items"] == []

    uploaded = upload_image(client)
    draft_result = ocr_draft(client, uploaded["id"])
    assert draft_result["ocr_available"] is True
    assert calls, "拍题入库应默认先跑本地 OCR"
    draft = draft_result["draft"]
    assert draft["source"] == "local_ocr"
    assert "光合作用" in draft["stem"]
    assert [option["label"] for option in draft["options"]] == ["A", "B", "C", "D"]
    assert draft["answers"] == ["B"]
    assert "答案" not in draft["stem"]

    state = client.get(f"/api/uploads/{uploaded['id']}/intake").json()
    assert state["status"] == "draft"
    assert state["revision"] == 1
    assert state["confirmed"] is None
    assert state["saved_question_id"] is None

    confirmed = confirm(client, uploaded["id"], expected_version=1)
    assert confirmed.status_code == 200, confirmed.text
    body = confirmed.json()
    assert body["revision"] == 2
    assert body["intake"]["status"] == "confirmed"
    assert body["intake"]["confirmed"]["answers"] == ["B"]

    saved = save_to_bank(client, uploaded["id"], expected_version=2)
    assert saved.status_code == 200, saved.text
    payload = saved.json()
    question = payload["question"]
    assert payload["saved_question_id"] and question["id"] == payload["saved_question_id"]
    assert question["subject"] == "生物"
    assert question["type"] == "SINGLE"
    assert question["content"]["stem"] == "下列关于光合作用的说法正确的是"
    assert question["content"]["answers"] == ["B"]
    assert question["content"]["explanation"] == "光合作用需要光照。"
    assert question["knowledge_tags"] == ["光合作用"]
    assert question["has_answer"] is True
    assert question["status"] == "ready"
    assert question["content"]["images"][0]["asset"].startswith("pl_")
    # 原图按 paperlab content 结构可读（资产归属校验通过）
    image = client.get(question["content"]["images"][0]["url"])
    assert image.status_code == 200
    assert image.headers["content-type"].startswith("image/")

    intake = client.get(f"/api/uploads/{uploaded['id']}/intake").json()
    assert intake["saved_question_id"] == payload["saved_question_id"]
    assert intake["revision"] == 2

    # 幂等：重复保存不重复建题
    again = save_to_bank(client, uploaded["id"], expected_version=2)
    assert again.status_code == 200
    assert again.json()["already_saved"] is True
    assert again.json()["saved_question_id"] == payload["saved_question_id"]
    listed = client.get("/api/paperlab/questions").json()
    assert listed["total"] == 1


def test_text_upload_still_works_with_intake(client, monkeypatch):
    """纯文本录入作为兼容入口：无需本地 OCR，也能走草稿 → 确认 → 保存。"""
    no_similar(monkeypatch)
    local_calls = install_local_ocr(monkeypatch)
    uploaded = client.post("/api/uploads/question", files={
        "file": ("题目.txt", "求导数 y = x^2\n答案：y' = 2x".encode("utf-8"), "text/plain"),
    }).json()
    draft = ocr_draft(client, uploaded["id"])["draft"]
    assert draft["source"] == "text"
    assert "求导数" in draft["stem"]
    assert draft["answers"] == ["y' = 2x"]
    assert local_calls == [], "文本上传不应触发本地 OCR"

    confirmed = confirm(
        client, uploaded["id"], expected_version=1,
        subject="数学", type="计算题", stem="求导数 y = x^2", options=[],
        answers=["y' = 2x"], knowledge_tags=["导数"],
    )
    assert confirmed.status_code == 200, confirmed.text
    saved = save_to_bank(client, uploaded["id"], expected_version=2)
    assert saved.status_code == 200, saved.text
    question = saved.json()["question"]
    assert question["type"] == "SUBJECTIVE"
    assert question["content"]["images"] == []


# ---------------------------------------------------------------------------
# 2. 人工确认后异步识别回写不覆盖
# ---------------------------------------------------------------------------

def _vision_call(monkeypatch, *, on_call=None, stem="模型识别题干：光合作用"):
    async def fake_call(prov, model, prompt, images=None, json_output=False, **kwargs):
        if on_call is not None:
            on_call()
        return {
            "content": json.dumps({
                "subject": "生物", "type": "单选题", "stem": stem,
                "options": [{"label": "A", "text": "模型识别选项"}],
                "answers": ["C"],
                "conditions": [], "uncertain": [],
            }),
            "model": model, "provider_name": "mock", "usage": {},
        }

    monkeypatch.setattr(ai, "_call_model", fake_call)


def test_late_vision_result_does_not_override_human_confirmation(client, monkeypatch):
    """识别开始后人工确认：异步回写只进 pending_draft，不覆盖 confirmed 关键字段。"""
    install_local_ocr(monkeypatch)
    no_similar(monkeypatch)
    provider = save_provider(client)
    user_id = user_id_of(client)
    uploaded = upload_image(client)

    def confirm_during_recognition():
        response = confirm(client, uploaded["id"], expected_version=1)
        assert response.status_code == 200, response.text

    _vision_call(monkeypatch, on_call=confirm_during_recognition)
    job_input = ai.JobInput(
        task="recognize", upload_id=uploaded["id"], provider_id=provider["id"],
        model="vision-model",
    )
    result = asyncio.run(upload_pipeline._recognize_job(
        user_id, job_input, {"id": provider["id"]}, None, job_id=101,
    ))
    assert result["draft_applied"] is False
    assert result["draft_conflict"] is True
    assert result["revision"] == 2

    intake = client.get(f"/api/uploads/{uploaded['id']}/intake").json()
    assert intake["status"] == "confirmed"
    assert intake["revision"] == 2
    # 人工确认的题干/答案原样保留
    assert intake["confirmed"]["stem"] == "下列关于光合作用的说法正确的是"
    assert intake["confirmed"]["answers"] == ["B"]
    # 模型结果保留为待确认草稿
    assert intake["pending_draft"]["stem"].startswith("模型识别题干")
    assert intake["pending_draft"]["source"] == "vision"
    assert "需再次确认" in intake["pending_draft"]["reason"]

    # 再次确认（采用模型草稿）后 pending_draft 清空、版本继续前进
    adopted = confirm(
        client, uploaded["id"], expected_version=2,
        stem="模型识别题干：光合作用", options=[{"label": "A", "text": "模型识别选项"}],
        answers=["C"],
    )
    assert adopted.status_code == 200, adopted.text
    assert adopted.json()["revision"] == 3
    intake_after = adopted.json()["intake"]
    assert intake_after["pending_draft"] is None
    assert intake_after["confirmed"]["stem"] == "模型识别题干：光合作用"


def test_vision_result_after_confirmation_only_kept_as_model_draft(client, monkeypatch):
    """先确认、后发起「增强识别」：模型结果同样不得改写人工确认。"""
    install_local_ocr(monkeypatch)
    no_similar(monkeypatch)
    provider = save_provider(client)
    user_id = user_id_of(client)
    uploaded = upload_image(client)
    assert confirm(client, uploaded["id"], expected_version=1).status_code == 200

    _vision_call(monkeypatch, stem="迟到的视觉识别结果")
    result = asyncio.run(upload_pipeline._recognize_job(
        user_id,
        ai.JobInput(task="recognize", upload_id=uploaded["id"],
                    provider_id=provider["id"], model="vision-model"),
        {"id": provider["id"]}, None, job_id=102,
    ))
    assert result["draft_applied"] is False
    intake = client.get(f"/api/uploads/{uploaded['id']}/intake").json()
    assert intake["status"] == "confirmed"
    assert intake["confirmed"]["stem"] == "下列关于光合作用的说法正确的是"
    assert intake["pending_draft"]["stem"] == "迟到的视觉识别结果"


def test_vision_draft_applies_when_not_confirmed(client, monkeypatch):
    """未人工确认时，增强识别正常写入草稿（status=recognized），供人工核对。"""
    install_local_ocr(monkeypatch)
    no_similar(monkeypatch)
    provider = save_provider(client)
    user_id = user_id_of(client)
    uploaded = upload_image(client)
    _vision_call(monkeypatch, stem="增强识别结果")
    result = asyncio.run(upload_pipeline._recognize_job(
        user_id,
        ai.JobInput(task="recognize", upload_id=uploaded["id"],
                    provider_id=provider["id"], model="vision-model"),
        {"id": provider["id"]}, None, job_id=103,
    ))
    assert result["draft_applied"] is True
    intake = client.get(f"/api/uploads/{uploaded['id']}/intake").json()
    assert intake["status"] == "recognized"
    assert intake["draft"]["stem"] == "增强识别结果"
    assert intake["draft"]["source"] == "vision"
    assert intake["confirmed"] is None


# ---------------------------------------------------------------------------
# 3. 乐观锁与兼容性
# ---------------------------------------------------------------------------

def test_expected_version_conflict_returns_409(client, monkeypatch):
    install_local_ocr(monkeypatch)
    no_similar(monkeypatch)
    uploaded = upload_image(client)
    assert confirm(client, uploaded["id"], expected_version=1).status_code == 200
    stale = confirm(client, uploaded["id"], expected_version=1)
    assert stale.status_code == 409
    assert "版本冲突" in stale.json()["detail"]
    # 保存也校验期望版本
    stale_save = save_to_bank(client, uploaded["id"], expected_version=1)
    assert stale_save.status_code == 409


def test_legacy_confirm_without_expected_version_still_works(client, monkeypatch):
    """旧前端 / 旧接口调用不传 expected_version 时保持原行为，不破坏既有流程。"""
    no_similar(monkeypatch)
    uploaded = client.post("/api/uploads/question", files={
        "file": ("旧流程.txt", "计算 1+1".encode("utf-8"), "text/plain"),
    }).json()
    response = client.patch(f"/api/uploads/{uploaded['id']}/confirm", json={
        "subject": "数学", "type": "计算题", "stem": "计算 1+1", "conditions": [],
    })
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "confirmed"
    assert body["confirmed"]["stem"] == "计算 1+1"
    assert body["revision"] == 2

    # 旧 upload 接口结构不变
    assert {"id", "filename", "media_type", "preview_url", "extra_previews", "extracted_text", "status"} <= set(uploaded)


def test_save_requires_human_confirmation(client, monkeypatch):
    install_local_ocr(monkeypatch)
    no_similar(monkeypatch)
    uploaded = upload_image(client)
    response = save_to_bank(client, uploaded["id"])
    assert response.status_code == 409
    assert "人工确认" in response.json()["detail"]


def test_save_failure_is_explicit_and_retryable(client, monkeypatch):
    install_local_ocr(monkeypatch)
    no_similar(monkeypatch)
    uploaded = upload_image(client)
    assert confirm(client, uploaded["id"], expected_version=1).status_code == 200

    original_create = paperlab_bank.create_question

    def boom(*args, **kwargs):
        raise RuntimeError("模拟题库写入失败")

    monkeypatch.setattr(paperlab_bank, "create_question", boom)
    failed = save_to_bank(client, uploaded["id"], expected_version=2)
    assert failed.status_code == 502
    assert "保存到我的题库失败" in failed.json()["detail"]
    intake = client.get(f"/api/uploads/{uploaded['id']}/intake").json()
    assert intake["saved_question_id"] is None

    # 恢复后重试成功（可重试）
    monkeypatch.setattr(paperlab_bank, "create_question", original_create)
    no_similar(monkeypatch)
    retried = save_to_bank(client, uploaded["id"], expected_version=2)
    assert retried.status_code == 200, retried.text
    assert retried.json()["saved_question_id"]


# ---------------------------------------------------------------------------
# 4. 本地 OCR 不可用与识别失败
# ---------------------------------------------------------------------------

def test_ocr_unavailable_keeps_original_and_allows_manual_save(client, monkeypatch):
    no_similar(monkeypatch)

    async def unavailable(image=None, **kwargs):
        raise ocr.OcrUnavailable("本地 OCR 依赖不可用")

    monkeypatch.setattr(ocr, "recognize_image", unavailable)
    uploaded = upload_image(client)
    result = ocr_draft(client, uploaded["id"])
    assert result["ocr_available"] is False
    assert "本地 OCR" in result["ocr_notice"]
    assert result["draft"] is None
    # 原图仍可读
    assert client.get(uploaded["preview_url"]).status_code == 200

    confirmed = confirm(
        client, uploaded["id"], expected_version=1,
        subject="数学", type="计算题", stem="手录题干：求 1+1", options=[],
        answers=["2"], knowledge_tags=[],
    )
    assert confirmed.status_code == 200
    saved = save_to_bank(client, uploaded["id"], expected_version=2)
    assert saved.status_code == 200, saved.text
    assert saved.json()["question"]["content"]["stem"] == "手录题干：求 1+1"


def test_ocr_draft_does_not_touch_confirmed_fields(client, monkeypatch):
    install_local_ocr(monkeypatch)
    no_similar(monkeypatch)
    uploaded = upload_image(client)
    assert confirm(client, uploaded["id"], expected_version=1).status_code == 200
    # 确认后再次跑本地 OCR：只刷新草稿，不改变人工确认内容
    redraft = ocr_draft(client, uploaded["id"])
    assert redraft["status"] == "confirmed"
    assert redraft["draft"]["source"] == "local_ocr"
    intake = client.get(f"/api/uploads/{uploaded['id']}/intake").json()
    assert intake["confirmed"]["stem"] == "下列关于光合作用的说法正确的是"
    assert intake["revision"] == 2


# ---------------------------------------------------------------------------
# 5. AI 建议答案与 OCR 答案分离
# ---------------------------------------------------------------------------

def test_ai_suggestion_is_separate_and_does_not_override_confirmed(client, monkeypatch):
    install_local_ocr(monkeypatch)
    no_similar(monkeypatch)
    provider = save_provider(client)
    uploaded = upload_image(client)
    assert confirm(client, uploaded["id"], expected_version=1).status_code == 200

    async def fake_call(prov, model, prompt, *args, **kwargs):
        assert "AI 建议答案" not in prompt  # 提示词只带题目
        assert "光合作用" in prompt
        return {
            "content": json.dumps({
                "answers": ["C"], "explanation": "AI 解析：叶绿体类囊体薄膜。",
                "rubric": "", "knowledge_tags": ["光合作用", "叶绿体"],
                "uncertain": [],
            }),
            "model": model, "provider_name": "mock", "usage": {},
        }

    monkeypatch.setattr(ai, "_call_model", fake_call)
    response = client.post(f"/api/uploads/{uploaded['id']}/ai-answer", json={
        "provider_id": provider["id"], "model": "vision-model", "expected_version": 2,
    })
    assert response.status_code == 200, response.text
    body = response.json()
    suggestion = body["ai_suggestion"]
    assert suggestion["answers"] == ["C"]
    assert suggestion["source"] == "ai"
    assert "仅供参考" in body["notice"]

    intake = client.get(f"/api/uploads/{uploaded['id']}/intake").json()
    # OCR/人工确认答案仍是 B，AI 建议单独存放（不覆盖）
    assert intake["confirmed"]["answers"] == ["B"]
    assert intake["ai_suggestion"]["answers"] == ["C"]
    assert intake["ai_suggestion"]["explanation"].startswith("AI 解析")

    # 逐字段采用：把 AI 建议答案与解析写回人工确认
    adopted = confirm(
        client, uploaded["id"], expected_version=2,
        answers=["C"], explanation=intake["ai_suggestion"]["explanation"],
    )
    assert adopted.status_code == 200
    intake_after = adopted.json()["intake"]
    assert intake_after["confirmed"]["answers"] == ["C"]
    assert intake_after["confirmed"]["explanation"].startswith("AI 解析")
    # 版本已前进：旧版本请求 AI 建议 409
    stale = client.post(f"/api/uploads/{uploaded['id']}/ai-answer", json={
        "provider_id": provider["id"], "model": "vision-model", "expected_version": 2,
    })
    assert stale.status_code == 409


# ---------------------------------------------------------------------------
# 6. 前端与任务中心深链的静态断言
# ---------------------------------------------------------------------------

def _read_web(relative: str) -> str:
    from pathlib import Path

    return (Path(__file__).resolve().parents[1] / "web" / relative).read_text(encoding="utf-8")


def test_frontend_ocr_first_and_task_deep_link_wiring():
    upload_js = _read_web("js/pages/upload.js")
    assert "/ocr-draft" in upload_js
    assert "/save-to-bank" in upload_js
    assert "/ai-answer" in upload_js
    assert "expected_version" in upload_js
    assert "pending_draft" in upload_js
    # 本地 OCR 是默认主路径：视觉识别按钮标记为「增强识别」且为可选
    assert "upload.intake.enhanceBtn" in upload_js
    # 任务中心深链：识别任务完成回到 #/upload?upload=<upload_id>
    tasks_js = _read_web("js/pages/tasks.js")
    assert "upload_id" in tasks_js
    assert "upload: request.upload_id" in tasks_js or "upload_id" in tasks_js
