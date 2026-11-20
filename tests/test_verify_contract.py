"""§2.1 AI 核验与内容链路契约回归。

覆盖三项 P0 修复：
1. 原卷冲突降级死代码：`_reference_check` 的 `agreed` 恒真，冲突必须看 `matches_reference`；
2. 结构化交叉核验：独立求解/互审必须显式 `json_output=True` 且提示词要求严格 JSON，
   解析失败时仍走裁判兜底；
3. 块级图片与公式回退图：verify（线上）与 eval（离线）共用 `app.content_refs`，
   对同一道合成题返回完全相同的图片列表（含 answer/solution 块与公式 fallback_asset）。

全部离线：模型调用一律 monkeypatch `ai._call_model`，不联网、不消耗额度。
"""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from app import ai, content_refs, db, eval as eval_mod, practice
from app.ai import verify


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------

def _structured(value, kind="choice", steps_content="推导"):
    return json.dumps({
        "final_answer": {"kind": kind, "value": value},
        "steps": [{"no": 1, "title": "分析", "content": steps_content, "score_point": True}],
        "knowledge_points": ["测试知识点"], "method": "通法", "assumptions": [], "uncertain": [],
    }, ensure_ascii=False)


def _evidence(reference="A"):
    return {
        "题干": "题干", "选项": [], "原卷参考答案": reference, "原卷解析": "",
        "材料": None, "学科": "数学", "题型": "单选题",
    }


def _write_png(path: Path, color="white"):
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (4, 4), color).save(path)
    return path


def _synthetic_question(bank_root: Path) -> dict:
    """一道同时含五类块级图、公式回退图与旧快照图片的合成题。"""
    subject = "数学"
    rel = "images"
    for name in (
        "stem.png", "option.png", "shared.png", "answer.png",
        "solution_formula.png", "legacy.png", "material.png",
    ):
        _write_png(bank_root / subject / rel / name)
    _write_png(bank_root / subject / rel / "stem_formula.png")
    return {
        "qid": "syn:contract:1",
        "subject": subject,
        "type": "single_choice",
        "type_label": "单选题",
        "stem_plain": "求 [公式1] 的值。",
        "stem_blocks": [
            {"kind": "text", "block_id": "stem-t", "text": "求"},
            {
                "kind": "math", "block_id": "stem-m", "latex": "x^{2}+1",
                "fallback_asset": {"file": "images/stem_formula.png", "role": "formula"},
            },
            {"kind": "image", "block_id": "stem-i", "asset": {"file": "images/stem.png", "role": "figure"}},
            {"kind": "text", "block_id": "stem-t2", "text": "的值。"},
        ],
        "options": [{"label": "A", "text": "选 [公式2]"}],
        "options_blocks": [
            {
                "label": "A",
                "blocks": [
                    {"kind": "math", "block_id": "opt-m", "latex": "y=2"},
                    {
                        "kind": "image", "block_id": "opt-i",
                        "asset": {"file": "images/option.png", "role": "figure"},
                    },
                ],
            },
        ],
        "shared_blocks": [
            {"kind": "image", "block_id": "shared-i", "asset": {"file": "images/shared.png"}},
        ],
        "answer_blocks": [
            {"kind": "image", "block_id": "answer-i", "asset": {"file": "images/answer.png"}},
        ],
        "solution_blocks": [
            {"kind": "text", "block_id": "solution-t", "text": "解析文本"},
            {
                "kind": "math", "block_id": "solution-m", "latex": "x=1",
                "fallback_asset": {"file": "images/solution_formula.png", "role": "formula"},
            },
        ],
        "images": [{"file": "images/legacy.png", "role": "figure"}],
        "group": {
            "group_id": "doc:g1",
            "parent_stem": "共用材料：观察图表。",
            "material_text": "材料正文",
            "material_images": [{"file": "images/material.png", "role": "figure"}],
        },
        "answer": {"text": "A", "kind": "choice", "note": None, "solution": ""},
    }


def _expected_assets():
    # 块顺序：题干 → 选项 → 共用题干 → 原卷答案 → 原卷解析 → 旧快照 images → material_images
    return [
        "images/stem_formula.png",
        "images/stem.png",
        "images/option.png",
        "images/shared.png",
        "images/answer.png",
        "images/solution_formula.png",
        "images/legacy.png",
        "images/material.png",
    ]


def _sent_assets(bank_root: Path, subject: str, sent_paths):
    prefix = f"{subject}/"
    assets = []
    for path in sent_paths:
        rel = Path(path).resolve().relative_to(Path(bank_root).resolve()).as_posix()
        assets.append(rel[len(prefix):] if rel.startswith(prefix) else rel)
    return assets


class _CallRecorder:
    def __init__(self, *, primary="A", verifier=None, judge="agreed", revisions=None):
        self.calls = []
        self.primary = primary
        self.verifier = verifier
        self.judge = judge
        self.revisions = revisions or {}

    async def __call__(self, provider, model, prompt, images=None, json_output=False, **kwargs):
        self.calls.append({
            "provider": provider, "model": model, "prompt": prompt,
            "images": images, "json_output": json_output, **kwargs,
        })
        if "请独立求解本题" in prompt:
            if model == "model-a":
                content = self.primary if isinstance(self.primary, str) else self.primary(model)
            else:
                content = self.verifier if isinstance(self.verifier, str) else (
                    self.verifier(model) if self.verifier else _structured("A")
                )
            return {"content": content, "model": model, "provider_name": "mock", "usage": {}}
        if "你是独立裁判" in prompt:
            payload = (
                self.judge if isinstance(self.judge, dict) else {
                    "agreed": self.judge == "agreed", "diverge_at_step": None,
                    "which_is_right": "unsure", "reason": "看起来一致",
                    "confidence": "high", "summary": "一致", "differences": [],
                }
            )
            return {"content": json.dumps(payload, ensure_ascii=False),
                    "model": model, "provider_name": "mock", "usage": {}}
        if "另一位独立解题者给出了不同解答" in prompt:
            content = self.revisions.get("A" if model == "model-a" else "B", _structured("A"))
            return {"content": content, "model": model, "provider_name": "mock", "usage": {}}
        return {"content": "详细解析正文", "model": model, "provider_name": "mock", "usage": {}}

    def solve_calls(self):
        return [call for call in self.calls if "请独立求解本题" in call["prompt"]]

    def judge_calls(self):
        return [call for call in self.calls if "你是独立裁判" in call["prompt"]]

    def revision_calls(self):
        return [call for call in self.calls if "另一位独立解题者给出了不同解答" in call["prompt"]]


def _structured_job(qid, **overrides):
    kwargs = dict(
        task="explain", qid=qid, provider_id=1, model="model-a",
        cross_verify=True, verify_provider_id=2, verify_model="model-b",
        judge_provider_id=3, judge_model="model-c", structured_verify=True,
    )
    kwargs.update(overrides)
    return ai.JobInput(**kwargs)


def _share_eligible(qid):
    with db.connection() as con:
        row = con.execute(
            "SELECT share_eligible FROM ai_explanations WHERE qid=?", (qid,),
        ).fetchone()
    assert row is not None, "讲解记录未落库"
    return row["share_eligible"]


# ---------------------------------------------------------------------------
# 1. 原卷冲突降级死代码（_reference_check 的 agreed 恒真 → 看 matches_reference）
# ---------------------------------------------------------------------------

def test_judge_agrees_but_reference_conflict_downgrades(client, monkeypatch):
    """裁判判「一致」，但唯一可解析的题解与原卷答案冲突 → 必须降级且不可分享。"""
    recorder = _CallRecorder(
        primary=_structured("A"),
        verifier="B 的题解（模型没按 JSON 返回，无法结构化比对）",
        judge="agreed",
    )
    monkeypatch.setattr(ai, "_call_model", recorder)
    monkeypatch.setattr(ai, "_bank_evidence", lambda *a: (_evidence("C"), []))

    data = _structured_job("q-ref-conflict-deadcode")
    result = asyncio.run(ai._explain_job(
        1, data, {"id": 1}, {"id": 2}, judge={"id": 3}, judge_model="model-c",
    ))
    verification = result["verification"]

    # 死代码语义：自比 agreed 恒为 True，真正冲突字段是 matches_reference。
    reference_check = verification["reference_check"]
    assert reference_check["agreed"] is True
    assert reference_check["matches_reference"] is False
    assert verification["programmatic"] is None  # 只有一侧可解析 → 走 _reference_check

    assert verification["status"] == "agreed"
    assert verification["confidence"] == "low"  # 降档
    assert verification["reference_conflict"] is True
    assert verification["warning"] == "reference_conflict"
    assert "降级" in verification["notice"]
    assert verification["judge"]["agreed"] is True
    assert _share_eligible("q-ref-conflict-deadcode") == 0


def test_reference_check_does_not_downgrade_when_reference_matches(client, monkeypatch):
    """裁判判一致且题解与原卷答案一致时，不能被死代码/误判错误降级。"""
    recorder = _CallRecorder(
        primary=_structured("A"),
        verifier="B 的题解（非 JSON）",
        judge="agreed",
    )
    monkeypatch.setattr(ai, "_call_model", recorder)
    monkeypatch.setattr(ai, "_bank_evidence", lambda *a: (_evidence("A"), []))

    data = _structured_job("q-ref-conflict-none")
    result = asyncio.run(ai._explain_job(
        1, data, {"id": 1}, {"id": 2}, judge={"id": 3}, judge_model="model-c",
    ))
    verification = result["verification"]
    assert verification["reference_check"]["matches_reference"] is True
    assert verification["status"] == "agreed"
    assert verification["confidence"] == "medium"
    assert verification.get("reference_conflict") is False
    assert "warning" not in verification


# ---------------------------------------------------------------------------
# 2. 结构化交叉核验 JSON 模式 + 解析失败裁判兜底
# ---------------------------------------------------------------------------

def test_structured_verify_requests_json_output_and_falls_back_to_judge(client, monkeypatch):
    """structured_verify=True 时 A/B 独立求解都要 JSON 模式；解析失败仍走裁判兜底。"""
    recorder = _CallRecorder(
        primary="这不是 JSON：模型把题解包在文字里",
        verifier="也不是 JSON",
        judge="agreed",
    )
    monkeypatch.setattr(ai, "_call_model", recorder)
    monkeypatch.setattr(ai, "_bank_evidence", lambda *a: (_evidence("A"), []))

    data = _structured_job("q-json-fallback")
    result = asyncio.run(ai._explain_job(
        1, data, {"id": 1}, {"id": 2}, judge={"id": 3}, judge_model="model-c",
    ))
    verification = result["verification"]

    solve_calls = recorder.solve_calls()
    assert len(solve_calls) == 2
    assert all(call["json_output"] is True for call in solve_calls)
    assert all("final_answer" in call["prompt"] for call in solve_calls)

    judge_calls = recorder.judge_calls()
    assert len(judge_calls) == 1
    assert judge_calls[0]["json_output"] is True
    assert verification["judge"]["agreed"] is True
    assert verification["status"] == "agreed"
    assert verification.get("reference_check") is None  # 双侧都解析失败 → 无自比结果
    # 解析失败不得抛错，也不得宣称交叉核验通过后给绿标。
    assert verification["confidence"] == "medium"


def test_unstructured_cross_verify_does_not_force_json_output(client, monkeypatch):
    """未开启 structured_verify 时 A/B 调用是讲解正文，不能强制 JSON 模式。"""
    recorder = _CallRecorder(primary="详细解析正文", verifier="另一份正文", judge="agreed")
    monkeypatch.setattr(ai, "_call_model", recorder)
    monkeypatch.setattr(ai, "_bank_evidence", lambda *a: (_evidence("A"), []))

    data = _structured_job("q-no-json", structured_verify=False)
    result = asyncio.run(ai._explain_job(
        1, data, {"id": 1}, {"id": 2}, judge={"id": 3}, judge_model="model-c",
    ))
    assert result["mode_cards"][0]["content"] == "详细解析正文"
    assert recorder.solve_calls() == []  # 未开启结构化求解时没有「独立求解」提示词
    body_calls = [call for call in recorder.calls if "【详细解析】" in call["prompt"]]
    assert body_calls and all(call["json_output"] is False for call in body_calls)


def test_peer_review_uses_strict_json_prompt(client, monkeypatch):
    """互审在结构化模式下必须只返回严格 JSON，并带 json_output=True。"""
    recorder = _CallRecorder(
        primary=_structured("A"),
        verifier=_structured("B"),
        judge={"agreed": False, "diverge_at_step": 1, "which_is_right": "A",
               "reason": "B 第 1 步符号错误", "confidence": "high",
               "summary": "符号分歧", "differences": ["第1步"]},
        revisions={"A": _structured("A"), "B": _structured("A")},
    )
    monkeypatch.setattr(ai, "_call_model", recorder)
    monkeypatch.setattr(ai, "_bank_evidence", lambda *a: (_evidence("A"), []))

    data = _structured_job("q-peer-json", peer_review=True)
    result = asyncio.run(ai._explain_job(
        1, data, {"id": 1}, {"id": 2}, judge={"id": 3}, judge_model="model-c",
    ))
    verification = result["verification"]

    revision_calls = recorder.revision_calls()
    assert len(revision_calls) == 2
    for call in revision_calls:
        assert call["json_output"] is True
        assert "只返回严格 JSON" in call["prompt"]
    # 互审修正后 A/B 都返回 A 且与原卷一致 → 复判更新为 agreed/medium。
    assert verification["peer_review"]["recheck"]["agreed"] is True
    assert verification["peer_review"]["recheck"]["matches_reference"] is True
    assert verification["status"] == "agreed"
    assert verification["confidence"] == "medium"


# ---------------------------------------------------------------------------
# 3. 块级图片与公式回退图：verify 与 eval 共用同一采集实现
# ---------------------------------------------------------------------------

def test_collect_question_images_covers_all_block_groups(tmp_path):
    question = _synthetic_question(tmp_path / "bank")
    assert content_refs.collect_question_images(question) == _expected_assets()

    # 去重：同一素材在多处出现只送一次。
    duplicated = dict(question)
    duplicated["solution_blocks"] = [
        {"kind": "image", "block_id": "dup", "asset": {"file": "images/stem.png"}},
    ]
    assert content_refs.collect_question_images(duplicated).count("images/stem.png") == 1


def test_verify_and_eval_share_identical_image_list(tmp_path, monkeypatch):
    """同一道合成题：线上判分证据图 == 离线评测 content_refs 图 == 公共采集函数。"""
    bank_root = tmp_path / "bank"
    question = _synthetic_question(bank_root)
    canonical = content_refs.collect_question_images(question)
    assert canonical == _expected_assets()

    # 离线评测：golden-v2 / run_evaluation 的 content_refs 图片资产。
    refs = eval_mod.build_content_refs(question)
    eval_assets = [ref["asset"] for ref in refs if ref["has_image"] and ref.get("asset")]
    assert eval_assets == canonical

    # 线上判分：_bank_evidence 真正读成多模态输入的顺序必须与公共采集函数一致。
    monkeypatch.setattr(db, "BANK_DIR", bank_root)
    monkeypatch.setattr(practice, "load_question", lambda qid: question)
    sent_paths = []
    real_image_part = content_refs.image_part

    def spy_image_part(source):
        sent_paths.append(Path(source))
        return real_image_part(source)

    monkeypatch.setattr(content_refs, "image_part", spy_image_part)

    evidence, images = verify._bank_evidence(1, SimpleNamespace(qid=question["qid"]))
    assert len(images) == len(canonical)
    assert all(part["mime"] == "image/png" and part["data"] for part in images)
    assert _sent_assets(bank_root, question["subject"], sent_paths) == canonical

    # 块级结构优先：公式占位符被渲染后的可读文本替代，旧字段仍保留兜底。
    assert "[公式1]" not in evidence["题干"]
    assert "x^{2}+1" in evidence["题干"]
    assert evidence["题干原文"] == question["stem_plain"]
    assert evidence["选项"] == [{"label": "A", "text": "y=2"}]  # 选项块渲染优先于旧占位符文本
    assert evidence["共用题干"] == "共用材料：观察图表。"
    # 块级解析兜底：solution 纯文本为空时用 solution_blocks 渲染文本。
    assert "解析文本" in evidence["原卷解析"]


def test_eval_reference_block_images_are_optional_not_sent(tmp_path, monkeypatch):
    """原卷答案解析图是核验证据而非独立求解输入：完整记录但不送去离线评测泄题。"""
    bank_root = tmp_path / "bank"
    question = _synthetic_question(bank_root)
    monkeypatch.setattr(db, "BANK_DIR", bank_root)
    refs = eval_mod.build_content_refs(question)

    answer_like = [ref for ref in refs if ref.get("has_image") and ref.get("field") in ("answer", "solution")]
    assert {ref["asset"] for ref in answer_like} == {
        "images/answer.png", "images/solution_formula.png",
    }
    assert all(ref["required"] is False for ref in answer_like)

    item = {"subject": question["subject"], "content_refs": refs}
    parts, delivered, _notes = eval_mod.collect_image_parts(item, refs)
    assert "images/answer.png" not in delivered
    assert "images/solution_formula.png" not in delivered
    assert "images/stem.png" in delivered
    assert len(parts) == 6  # 题干 2 + 选项 1 + 共用 1 + 旧快照 1 + 材料 1

    # 输入完整率只统计必要单元（required=True），可选证据图不影响分母。
    stats = eval_mod.compute_input_completeness(refs, delivered_assets=delivered)
    assert stats["expected_units"] == sum(1 for ref in refs if ref.get("required", True))
    assert stats["expected_units"] == 10  # 题干 4 + 选项 2 + 共用 1 + 材料 2 + 旧快照 1
    assert stats["missing_inputs"] == []
    assert stats["input_completeness"] == 1.0


def test_bank_evidence_still_fails_closed_on_missing_block_image(tmp_path, monkeypatch):
    """块级图片缺失时线上判分必须显式失败，不能悄悄少送图后声称已完整分析。"""
    bank_root = tmp_path / "bank"
    _write_png(bank_root / "数学" / "images" / "ok.png")
    monkeypatch.setattr(db, "BANK_DIR", bank_root)
    question = {
        "qid": "syn:missing",
        "subject": "数学",
        "stem_plain": "如图。",
        "stem_blocks": [
            {"kind": "image", "block_id": "ok", "asset": {"file": "images/ok.png"}},
            {"kind": "image", "block_id": "missing", "asset": {"file": "images/missing.png"}},
        ],
    }
    with pytest.raises(ValueError, match="无法读取"):
        content_refs.collect_bank_evidence(question)


def test_evidence_falls_back_to_legacy_fields_without_blocks():
    """无块级结构时证据仍用旧字段（stem_plain/options）兜底，不丢内容。"""
    question = {
        "qid": "syn:legacy",
        "subject": "数学",
        "type_label": "填空题",
        "stem_plain": "1+1=____",
        "options": [{"label": "A", "text": "2"}],
        "answer": {"text": "2", "solution": ""},
    }
    evidence = content_refs.build_evidence(question)
    assert evidence["题干"] == "1+1=____"
    assert evidence["选项"] == [{"label": "A", "text": "2"}]
    assert evidence["原卷参考答案"] == "2"
    assert "题干原文" not in evidence
