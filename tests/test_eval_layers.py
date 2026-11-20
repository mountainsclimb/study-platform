"""P1「评测分层与可追溯」离线测试。

不联网、不调用真实模型：需要模型结果的用例统一 monkeypatch `ai._call_model`；
分层评测集构建只读题库，若本机没有题库文件则跳过构建用例。
"""
import asyncio
import json
from pathlib import Path

import pytest

from app import ai, db, eval as eval_mod


def _ref(
    field="stem",
    field_label="题干",
    kind="text",
    *,
    block_id=None,
    asset=None,
    has_image=False,
    role=None,
    fallback_text=None,
    missing_reason=None,
):
    return {
        "field": field,
        "field_label": field_label,
        "block_id": block_id,
        "kind": kind,
        "role": role,
        "asset": asset,
        "has_image": has_image,
        "required": True,
        "fallback_text": fallback_text,
        "missing_reason": missing_reason,
    }


def _known_cost():
    call = eval_mod.estimate_call_cost(
        {"prompt_tokens": 1000, "completion_tokens": 500}, 2.0, 4.0
    )
    return eval_mod.combine_call_costs([call])


def _unknown_cost():
    call = eval_mod.estimate_call_cost(
        {"prompt_tokens": 1000, "completion_tokens": 500}, None, None
    )
    return eval_mod.combine_call_costs([call])


# ---------------------------------------------------------------------------
# 分层分类
# ---------------------------------------------------------------------------

def test_classify_strata_covers_four_layers_and_combinations():
    text_question = {
        "qid": "t1",
        "stem_plain": "1+1=?",
        "options": [{"label": "A", "text": "2"}],
    }
    assert eval_mod.classify_strata(text_question) == ["text"]
    assert eval_mod.primary_stratum(["text"]) == "text"

    figure_question = {
        "qid": "f1",
        "stem_plain": "如图所示，求面积。",
        "stem_blocks": [
            {"kind": "image", "block_id": "b1", "asset": {"file": "images/a.png", "role": "figure"}}
        ],
    }
    assert "figure_formula" in eval_mod.classify_strata(figure_question)

    formula_option_question = {
        "qid": "f2",
        "stem_plain": "选择正确表达式。",
        "options_blocks": [
            {
                "label": "A",
                "blocks": [
                    {
                        "kind": "math",
                        "block_id": "b2",
                        "latex": "x^2+1",
                        "fallback_asset": {"file": "images/formula.png", "role": "formula"},
                    }
                ],
            }
        ],
    }
    assert eval_mod.classify_strata(formula_option_question) == ["figure_formula"]

    material_question = {"qid": "m1", "group_id": "doc:g1", "stem_plain": "阅读材料后回答。"}
    assert eval_mod.classify_strata(material_question) == ["material_group"]

    multipart_question = {
        "qid": "m2",
        "group_id": "doc:g1",
        "group": {"n_members": 3, "material_text": "材料"},
        "stem_plain": "（1）问题",
    }
    strata = eval_mod.classify_strata(multipart_question)
    assert set(strata) == {"material_group", "group_multipart"}
    assert eval_mod.primary_stratum(strata) == "group_multipart"

    combined = {
        "qid": "m3",
        "group_id": "doc:g1",
        "group": {"member_index": [{"qid": "m3"}, {"qid": "m4"}], "material_text": "材料"},
        "stem_plain": "[图1] 观察图表。",
        "stem_blocks": [{"kind": "image", "block_id": "b3", "asset": {"file": "images/b.png"}}],
    }
    assert set(eval_mod.classify_strata(combined)) == {
        "figure_formula", "material_group", "group_multipart",
    }
    assert eval_mod.primary_stratum(eval_mod.classify_strata(combined)) == "group_multipart"


# ---------------------------------------------------------------------------
# 输入完整率
# ---------------------------------------------------------------------------

def test_input_completeness_marks_missing_formula_image_and_limits():
    refs = [
        _ref(),
        _ref("stem", "题干", "image", block_id="b2", role="formula",
             asset="images/f.png", has_image=True),
    ]
    partial = eval_mod.compute_input_completeness(refs)
    assert partial["input_completeness"] == 0.5
    assert partial["expected_units"] == 2
    assert partial["delivered_units"] == 1
    assert any("题干公式图未送入" in message for message in partial["missing_inputs"])

    degraded = eval_mod.compute_input_completeness(
        refs, delivered_assets=set(), image_notes={"images/f.png": "单图超过 12 MB 限制"}
    )
    assert degraded["input_completeness"] == 0.5
    assert any("12 MB" in message for message in degraded["missing_inputs"])

    complete = eval_mod.compute_input_completeness(refs, delivered_assets={"images/f.png"})
    assert complete["input_completeness"] == 1.0
    assert complete["missing_inputs"] == []
    assert all(ref["delivered"] for ref in complete["content_refs"])

    # 题库根本没有对应公式图时，构建阶段就落 missing_reason，执行阶段应继续记为缺项。
    bank_gap = [
        _ref(),
        _ref("stem", "题干", "image", role="figure", has_image=True,
             missing_reason="题库未提供对应的原卷图片"),
    ]
    gap_stats = eval_mod.compute_input_completeness(bank_gap)
    assert gap_stats["input_completeness"] == 0.5
    assert any("题库未提供" in message for message in gap_stats["missing_inputs"])

    assert eval_mod.compute_input_completeness([])["input_completeness"] == 1.0


# ---------------------------------------------------------------------------
# 指标聚合与费用
# ---------------------------------------------------------------------------

def test_summarize_eval_keeps_legacy_fields_and_adds_strata_and_input_metrics():
    records = [
        {
            "qid": "q1", "subject": "数学", "type": "single_choice", "strata": ["text"],
            "stratum": "text", "a_correct": True, "has_verifier": True, "agreed": True,
            "verification_comparable": True, "input_completeness": 1.0,
            "input_expected_units": 2, "input_delivered_units": 2,
            "missing_inputs": [], "cost": _known_cost(),
        },
        {
            "qid": "q2", "subject": "数学", "type": "single_choice", "strata": ["text"],
            "stratum": "text", "a_correct": False, "has_verifier": True, "agreed": False,
            "verification_comparable": False, "input_completeness": 0.5,
            "input_expected_units": 2, "input_delivered_units": 1,
            "missing_inputs": ["题干公式图未送入"], "cost": _unknown_cost(),
        },
        {
            "qid": "q3", "subject": "物理", "type": "single_choice", "strata": ["figure_formula"],
            "stratum": "figure_formula", "a_correct": False, "has_verifier": True, "agreed": True,
            "verification_comparable": True, "input_completeness": 0.5,
            "input_expected_units": 2, "input_delivered_units": 1,
            "missing_inputs": [], "cost": _known_cost(),
        },
        {
            "qid": "q4", "subject": "语文", "type": "short_answer", "strata": ["group_multipart"],
            "stratum": "group_multipart", "a_correct": True, "has_verifier": True, "agreed": False,
            "verification_comparable": True, "input_completeness": 0.25,
            "input_expected_units": 4, "input_delivered_units": 1,
            "missing_inputs": [], "cost": _known_cost(),
        },
    ]
    metrics = eval_mod.summarize_eval(records)

    # 旧字段保持兼容
    assert metrics["total"] == 4
    assert metrics["single_correct"] == 2
    assert metrics["single_accuracy"] == 50.0
    assert metrics["agreement_rate"] == 50.0
    assert metrics["cross_verified_accuracy"] == 50.0
    assert metrics["agreed_but_wrong_count"] == 1
    assert metrics["agreed_but_wrong_rate"] == 50.0

    # P1 新指标
    assert metrics["input_completeness"] == 0.5625
    assert metrics["effective_verification_rate"] == 75.0
    assert metrics["programmatic_comparable_count"] == 3
    assert metrics["strata_counts"]["text"] == 2
    assert metrics["strata"]["text"]["total"] == 2
    assert metrics["strata"]["text"]["input_completeness"] == 0.75
    assert metrics["strata"]["figure_formula"]["single_accuracy"] == 0.0
    assert metrics["strata"]["material_group"]["total"] == 0
    assert metrics["stratum_breakdown"]["group_multipart"]["total"] == 1
    assert metrics["unknown_cost_calls"] == 1
    assert metrics["wrong_qids"] == ["q2", "q3"]
    assert metrics["missing_input_qids"] == ["q2"]
    assert metrics["cost"]["estimated_cost"] is None
    assert metrics["cost"]["known_cost_subtotal"] == 0.012
    assert metrics["cost"]["cost_complete"] is False
    assert "未把未知调用按 0 元计入" in metrics["cost"]["note"]


def test_unknown_cost_is_counted_never_shown_as_zero():
    known = eval_mod.estimate_call_cost(
        {"prompt_tokens": 1000, "completion_tokens": 500}, 2.0, 4.0
    )
    assert known["price_known"] is True
    assert known["estimated_cost"] == 0.004
    assert known["unknown_cost_calls"] == 0

    unknown = eval_mod.estimate_call_cost(
        {"prompt_tokens": 1000, "completion_tokens": 500}, None, None
    )
    assert unknown["price_known"] is False
    assert unknown["estimated_cost"] is None
    assert unknown["unknown_cost_calls"] == 1

    no_usage = eval_mod.estimate_call_cost({}, 2.0, 4.0)
    assert no_usage["estimated_cost"] is None
    assert no_usage["unknown_cost_calls"] == 1

    all_known = eval_mod.summarize_cost([
        {"cost": eval_mod.combine_call_costs([known])},
    ])
    assert all_known["estimated_cost"] == 0.004
    assert all_known["unknown_cost_calls"] == 0
    assert all_known["cost_complete"] is True

    mixed = eval_mod.summarize_cost([
        {"cost": eval_mod.combine_call_costs([known])},
        {"cost": eval_mod.combine_call_costs([unknown])},
    ])
    assert mixed["estimated_cost"] is None  # 有未知调用时绝不显示 0 元/免费
    assert mixed["known_cost_subtotal"] == 0.004
    assert mixed["unknown_cost_calls"] == 1
    assert mixed["cost_complete"] is False


def test_cost_uses_ai_models_prices_when_configured(isolated_db, monkeypatch):
    db.init_db()
    with db.connection() as con:
        con.execute(
            "INSERT INTO users(id,username,password_hash,role,created_at) VALUES (?,?,?,?,?)",
            (1, "费用测试用户", "hash", "admin", db.utcnow()),
        )
        con.execute(
            "INSERT INTO ai_models(provider_id,user_id,model_id,input_price,output_price) VALUES (?,?,?,?,?)",
            (1, 1, "mock-primary", 2.0, 4.0),
        )
        con.execute(
            "INSERT INTO ai_models(provider_id,user_id,model_id,input_price,output_price) VALUES (?,?,?,?,?)",
            (2, 1, "mock-verifier", 1.0, 2.0),
        )

    async def fake_call(provider, model, prompt, images=None, *args, **kwargs):
        payload = {
            "final_answer": {"kind": "choice", "value": "A"},
            "steps": [{"no": 1, "title": "步骤", "content": "推导"}],
        }
        return {
            "content": json.dumps(payload, ensure_ascii=False),
            "usage": {"prompt_tokens": 100, "completion_tokens": 50},
            "model": model,
        }

    monkeypatch.setattr(ai, "_call_model", fake_call)
    item = {
        "qid": "unit:cost",
        "subject": "数学",
        "type": "single_choice",
        "strata": ["text"],
        "stratum": "text",
        "stem_plain": "1+1=?",
        "options": [],
        "reference_answer": "A",
        "answer_kind": "choice",
        "bank_version": "bank-test",
        "reference_source": "题库 answer_text",
        "content_refs": [_ref()],
    }
    report = asyncio.run(eval_mod.run_evaluation(
        [item],
        {"id": 1, "user_id": 1},
        "mock-primary",
        verifier={"id": 2, "user_id": 1},
        verify_model="mock-verifier",
        name="费用测试",
        concurrency=1,
    ))
    record = report["records"][0]
    # 主模型 100*2/1e6 + 50*4/1e6 = 0.0004；核验模型 = 0.0002。
    assert record["cost"]["estimated_cost"] == 0.0006
    assert record["cost"]["known_cost_calls"] == 2
    assert record["cost"]["unknown_cost_calls"] == 0
    assert record["cost"]["price_known"] is True
    metrics = report["metrics"]
    assert metrics["cost"]["estimated_cost"] == 0.0006
    assert metrics["cost"]["cost_complete"] is True
    assert metrics["unknown_cost_calls"] == 0
    assert "按 ai_models 单价" in metrics["cost"]["note"]


# ---------------------------------------------------------------------------
# 评测执行记录字段与可追溯写入
# ---------------------------------------------------------------------------

def test_eval_records_include_traceability_fields_and_persist(isolated_db, monkeypatch):
    db.init_db()

    async def fake_call(provider, model, prompt, images=None, *args, **kwargs):
        payload = {
            "final_answer": {"kind": "choice", "value": "A"},
            "steps": [{"no": 1, "title": "步骤", "content": "推导"}],
        }
        return {
            "content": json.dumps(payload, ensure_ascii=False),
            "usage": {"prompt_tokens": 100, "completion_tokens": 50},
            "model": model,
        }

    monkeypatch.setattr(ai, "_call_model", fake_call)

    items = [
        {
            "qid": "unit:text",
            "subject": "数学",
            "type": "single_choice",
            "strata": ["text"],
            "stratum": "text",
            "stem_plain": "1+1=?",
            "options": [{"label": "A", "text": "2"}],
            "reference_answer": "A",
            "answer_kind": "choice",
            "bank_version": "bank-test",
            "reference_source": "题库 answer_text（卷内原文答案）",
            "content_refs": [_ref()],
        },
        {
            "qid": "unit:figure",
            "subject": "物理",
            "type": "single_choice",
            "strata": ["figure_formula"],
            "stratum": "figure_formula",
            "stem_plain": "[公式1] 求加速度。",
            "options": [{"label": "A", "text": "2"}],
            "reference_answer": "A",
            "answer_kind": "choice",
            "bank_version": "bank-test",
            "reference_source": "题库 answer_text（卷内原文答案）",
            "content_refs": [
                _ref(),
                _ref("stem", "题干", "image", role="formula",
                     asset="images/__p1_missing__.png", has_image=True),
            ],
        },
    ]

    report = asyncio.run(eval_mod.run_evaluation(
        items,
        {"id": 1, "user_id": 1},
        "mock-primary",
        verifier={"id": 2, "user_id": 1},
        verify_model="mock-verifier",
        name="分层记录测试",
        concurrency=1,
    ))

    record = report["records"][0]
    required = {
        "qid", "subject", "type", "strata", "bank_version", "reference_answer",
        "reference_source", "content_refs", "input_completeness", "missing_inputs",
        "prompt_version", "model", "verify_model", "cost",
    }
    assert required <= set(record)
    assert record["prompt_version"] == ai.PROMPT_VERSION
    assert record["model"] == "mock-primary"
    assert record["verify_model"] == "mock-verifier"
    assert record["bank_version"] == "bank-test"

    figure_record = report["records"][1]
    assert figure_record["input_completeness"] == 0.5
    assert any("题干公式图未送入" in message for message in figure_record["missing_inputs"])
    assert figure_record["cost"]["unknown_cost_calls"] >= 2

    metrics = report["metrics"]
    assert metrics["total"] == 2
    assert metrics["input_completeness"] == 0.75
    assert metrics["strata"]["text"]["total"] == 1
    assert metrics["strata"]["figure_formula"]["total"] == 1
    assert metrics["cost"]["estimated_cost"] is None
    assert metrics["unknown_cost_calls"] >= 4
    assert report["saved_to_eval_runs"] is True

    with db.connection() as con:
        row = con.execute(
            "SELECT bank_version,strata,input_stats_json,report_json FROM eval_runs "
            "WHERE name=?", ("分层记录测试",),
        ).fetchone()
    assert row is not None
    assert row["bank_version"] == "bank-test"
    assert json.loads(row["strata"])["figure_formula"] == 1
    assert json.loads(row["input_stats_json"])["input_completeness"] == 0.75
    stored = json.loads(row["report_json"])
    assert stored["metrics"]["total"] == 2
    assert stored["records"][1]["qid"] == "unit:figure"


def test_existing_image_is_really_sent_as_multimodal_part(isolated_db, monkeypatch, tmp_path):
    from PIL import Image

    bank_root = tmp_path / "bank"
    image_dir = bank_root / "数学" / "images"
    image_dir.mkdir(parents=True)
    Image.new("RGB", (4, 4), "white").save(image_dir / "tiny.png")
    monkeypatch.setattr(db, "BANK_DIR", bank_root)

    captured = {}

    async def fake_call(provider, model, prompt, images=None, *args, **kwargs):
        captured["images"] = images
        payload = {
            "final_answer": {"kind": "choice", "value": "A"},
            "steps": [{"no": 1, "title": "步骤", "content": "推导"}],
        }
        return {
            "content": json.dumps(payload, ensure_ascii=False),
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            "model": model,
        }

    monkeypatch.setattr(ai, "_call_model", fake_call)
    item = {
        "qid": "unit:image",
        "subject": "数学",
        "type": "single_choice",
        "strata": ["figure_formula"],
        "stratum": "figure_formula",
        "stem_plain": "如图。",
        "options": [],
        "reference_answer": "A",
        "answer_kind": "choice",
        "bank_version": "bank-test",
        "reference_source": "题库 answer_text",
        "content_refs": [
            _ref(),
            _ref("stem", "题干", "image", role="figure", asset="images/tiny.png", has_image=True),
        ],
    }
    report = asyncio.run(eval_mod.run_evaluation(
        [item], {"id": 1}, "mock-primary", name="图片送模测试", concurrency=1
    ))
    assert captured["images"] and len(captured["images"]) == 1
    assert captured["images"][0]["mime"] == "image/png"
    assert captured["images"][0]["data"]
    record = report["records"][0]
    assert record["input_completeness"] == 1.0
    assert record["missing_inputs"] == []
    assert record["content_refs"][1]["delivered"] is True


# ---------------------------------------------------------------------------
# 分层评测集构建（只读题库）
# ---------------------------------------------------------------------------

def test_build_golden_v2_small_sample(tmp_path):
    bank_db = Path(db.BANK_DIR) / "题库.db"
    if not bank_db.is_file():
        pytest.skip("本机未配置只读题库，跳过分层评测集构建用例")

    golden = eval_mod.DEFAULT_GOLDEN
    golden_before = golden.read_bytes() if golden.is_file() else None

    out = tmp_path / "golden-v2.jsonl"
    items = eval_mod.build_golden_v2(out, count=12)
    assert len(items) == 12
    assert out.is_file()
    assert len([line for line in out.read_text(encoding="utf-8").splitlines() if line.strip()]) == 12

    # 绝不覆盖旧基线
    if golden_before is not None:
        assert golden.read_bytes() == golden_before

    required = {
        "qid", "subject", "type", "strata", "bank_version",
        "reference_answer", "reference_source", "content_refs",
    }
    covered = set()
    for item in items:
        assert required <= set(item)
        assert eval_mod.DEFAULT_GOLDEN_V2.name == "golden-v2.jsonl"
        assert item["strata"] and set(item["strata"]) <= set(eval_mod.STRATA)
        assert item["stratum"] in eval_mod.STRATA
        assert isinstance(item["bank_version"], str) and item["bank_version"].strip()
        assert isinstance(item["reference_answer"], str) and item["reference_answer"].strip()
        assert isinstance(item["reference_source"], str) and item["reference_source"].strip()
        assert isinstance(item["content_refs"], list) and item["content_refs"]
        for ref in item["content_refs"]:
            assert {"field", "block_id", "kind", "asset", "has_image"} <= set(ref)
            assert isinstance(ref["has_image"], bool)
            if ref["has_image"]:
                assert ref["asset"] or ref["missing_reason"]
        covered.update(item["strata"])

    support = eval_mod.bank_strata_counts()
    available = {stratum for stratum, count in support.items() if count > 0}
    assert available, "题库没有可用的分层候选"
    assert available <= covered, f"抽样未覆盖题库可用的分层：{available - covered}"
    # 分层结果要真的能支撑原卷图/公式与材料输入的评测，而不只是标签。
    assert any(ref["has_image"] for item in items for ref in item["content_refs"])
    assert any(ref["field"] in ("material", "shared") for item in items for ref in item["content_refs"])

    # 同一个只读题库、同一个 seed 应得到逐字节一致的结果。
    out2 = tmp_path / "golden-v2-again.jsonl"
    items2 = eval_mod.build_golden_v2(out2, count=12)
    assert [item["qid"] for item in items2] == [item["qid"] for item in items]
    assert out2.read_bytes() == out.read_bytes()
