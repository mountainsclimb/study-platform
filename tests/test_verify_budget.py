"""P0.1 止损回归：数值零、科学计数法、单位/量纲、受限语法、预算与裁判独立性。"""
import time

import pytest

from app.ai import verify


def _cmp(a, b, reference=None):
    return verify.compare_answers(
        {"kind": "number", "value": a}, {"kind": "number", "value": b}, reference,
    )


# ---------------------------------------------------------------------------
# 1. JSON 数值零不是缺失
# ---------------------------------------------------------------------------

def test_json_zero_is_a_real_answer():
    result = verify.compare_answers({"kind": "number", "value": 0}, {"kind": "number", "value": 0.0})
    assert result["comparable"] is True
    assert result["agreed"] is True
    # 零也可以出现在整数与字符串混用的记录里
    mixed = verify.compare_answers({"kind": "number", "value": 0}, {"kind": "number", "value": "0.00"})
    assert mixed["agreed"] is True
    # 只有真正缺失才判 missing
    missing = verify.compare_answers({"kind": "number", "value": None}, {"kind": "number", "value": 0})
    assert missing["comparable"] is False and missing["reason"] == "missing"


# ---------------------------------------------------------------------------
# 2. 科学计数法与同量纲换算
# ---------------------------------------------------------------------------

def test_scientific_notation_and_unit_conversion():
    assert _cmp("1.5e3", "1500")["agreed"] is True
    assert _cmp("1.5×10^3", "1500")["agreed"] is True
    assert _cmp("1.5×10³", "1500")["agreed"] is True
    assert _cmp("2×10^-3", "0.002")["agreed"] is True
    assert _cmp("1 km", "1000 m")["agreed"] is True
    assert _cmp("200 cm", "2 m")["agreed"] is True
    assert _cmp("50%", "0.5")["agreed"] is True
    assert _cmp("9.8 m/s^2", "9.8 N/kg")["agreed"] is True
    # 同量纲换算后不相等要判为不一致
    assert _cmp("1 km", "999 m")["agreed"] is False


# ---------------------------------------------------------------------------
# 3. 单位缺失、单位未知与异量纲明确不可比
# ---------------------------------------------------------------------------

def test_missing_or_mismatched_units_are_not_comparable():
    missing = _cmp("3.14 m/s", "3.14")
    assert missing["comparable"] is False and missing["reason"] == "unit_missing"
    mismatch = _cmp("2 m", "2 s")
    assert mismatch["comparable"] is False and mismatch["reason"] == "unit_mismatch"
    unknown = _cmp("5 元", "5 元")
    assert unknown["comparable"] is False and unknown["reason"] == "unit_unknown"
    # 单位问题不允许通过模型裁判前的程序规则被当作一致
    assert missing["agreed"] is None and mismatch["agreed"] is None


# ---------------------------------------------------------------------------
# 4. 受限语法、长度与运算预算
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad", [
    "__import__('os').system('dir')",
    "lambda: 1",
    "x.__class__",
    "open('secret.txt')",
    "foo + 1",
    "x; y",
])
def test_restricted_grammar_never_claims_agreement(bad):
    result = verify.compare_answers(
        {"kind": "expression", "value": bad}, {"kind": "expression", "value": "x"},
    )
    assert result["comparable"] is False
    assert result["agreed"] is None
    assert result["method"].startswith("rejected")


def test_expression_length_and_operation_budget(monkeypatch):
    monkeypatch.setenv("STUDY_VERIFY_MAX_CHARS", "24")
    long_result = verify.compare_answers(
        {"kind": "expression", "value": "(a+b+c+d+e+f+g+h+i+j)^2 + 1"},
        {"kind": "expression", "value": "x"},
    )
    assert long_result["comparable"] is False
    assert "too_long" in long_result["method"]

    monkeypatch.setenv("STUDY_VERIFY_MAX_CHARS", "4000")
    monkeypatch.setenv("STUDY_VERIFY_MAX_OPS", "3")
    ops_result = verify.compare_answers(
        {"kind": "expression", "value": "(a+b)*(c+d)*(e+f)"},
        {"kind": "expression", "value": "x"},
    )
    assert ops_result["comparable"] is False
    assert "too_complex" in ops_result["method"]


def test_wall_clock_budget_reports_timeout():
    status, _ = verify._thread_call(lambda: time.sleep(0.4), 20)
    assert status == "timeout"
    status, value = verify._thread_call(lambda: 42, 500)
    assert status == "ok" and value == 42


def test_approved_expression_equivalence_still_works():
    result = verify.compare_answers(
        {"kind": "expression", "value": "\\frac{1}{2}"}, {"kind": "expression", "value": "0.5"},
    )
    assert result["comparable"] is True and result["agreed"] is True
    assert result["verified"] is True
    trig = verify.compare_answers(
        {"kind": "expression", "value": "sin(x)^2 + cos(x)^2"}, {"kind": "expression", "value": "1"},
    )
    assert trig["agreed"] is True
    root = verify.compare_answers(
        {"kind": "expression", "value": "√2"}, {"kind": "number", "value": "1.41421356"},
    )
    assert root["agreed"] is True


# ---------------------------------------------------------------------------
# 5. 轨迹保留原卷参考与程序对照
# ---------------------------------------------------------------------------

def test_trace_keeps_reference_and_budget():
    result = verify.compare_answers(
        {"kind": "number", "value": "3 m"}, {"kind": "number", "value": "300 cm"}, "3 m",
    )
    assert result["agreed"] is True and result["matches_reference"] is True
    trace = result["trace"]
    assert trace["reference"] == "3 m"
    assert trace["input_a"]["value"] == "3 m" and trace["input_b"]["value"] == "300 cm"
    assert trace["kind"] == "number" and trace["method"] == "numeric_rel_1e-6"
    assert trace["budget"]["max_chars"] >= 24 and trace["budget"]["timeout_ms"] >= 1


# ---------------------------------------------------------------------------
# 6. 服务端拒绝主模型担任裁判
# ---------------------------------------------------------------------------

def _save_provider(client):
    response = client.post("/api/providers", json={
        "name": "测试连接", "kind": "openai", "base_url": "https://api.openai.com/v1",
        "api_key": "sk-unit-test-secret-value", "models": ["model-a", "model-b"],
    })
    assert response.status_code == 200, response.text
    return response.json()


def _objective_question():
    from app import practice
    with practice._bank() as con:
        ids = [r[0] for r in con.execute(
            "SELECT qid FROM questions WHERE objective=1 ORDER BY year DESC LIMIT 250"
        )]
    for qid in ids:
        question = practice.load_question(qid)
        blocking = [flag for flag in question["quality_flags"] if "参考答案待核验" not in flag]
        if question["practice_allowed"] and not blocking and question["has_answer"] and question["type"] == "single_choice":
            return question
    raise AssertionError("No suitable source question")


def test_primary_model_cannot_be_the_judge(client):
    question = _objective_question()
    submitted = client.post("/api/attempts", data={"qid": question["qid"], "answer": question["answer_text"]})
    assert submitted.status_code == 200, submitted.text
    provider = _save_provider(client)
    payload = {
        "task": "explain", "qid": question["qid"], "provider_id": provider["id"], "model": "model-a",
        "cross_verify": True, "verify_provider_id": provider["id"], "verify_model": "model-b",
        "judge_provider_id": provider["id"], "judge_model": "model-a",
    }
    rejected = client.post("/api/ai/jobs", json=payload)
    assert rejected.status_code == 422
    assert "裁判" in rejected.json()["detail"]
