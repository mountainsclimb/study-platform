"""P2「离线评测 eval.py 补齐」测试：裁判接入、统计口径、文本判分与断点续评。

全部离线：模型调用统一 monkeypatch `ai._call_model`；数据库用 isolated_db 临时目录。
"""
import asyncio
import json

import pytest

from app import ai, db, eval as eval_mod


# ---------------------------------------------------------------------------
# 测试辅助
# ---------------------------------------------------------------------------

def _ref(field="stem", field_label="题干", kind="text"):
    return {
        "field": field,
        "field_label": field_label,
        "block_id": None,
        "kind": kind,
        "role": None,
        "asset": None,
        "has_image": False,
        "required": True,
        "fallback_text": None,
        "missing_reason": None,
    }


def _item(qid, reference="A", kind="choice", stem="题目", subject="数学"):
    return {
        "qid": qid,
        "subject": subject,
        "type": "single_choice",
        "strata": ["text"],
        "stratum": "text",
        "stem_plain": stem,
        "options": [{"label": "A", "text": "选项A"}],
        "reference_answer": reference,
        "answer_kind": kind,
        "bank_version": "bank-test",
        "reference_source": "题库 answer_text",
        "content_refs": [_ref()],
    }


def _answer_content(kind, value):
    return json.dumps({
        "final_answer": {"kind": kind, "value": value},
        "steps": [{"no": 1, "title": "步骤", "content": "推导"}],
    }, ensure_ascii=False)


def _judge_content(**fields):
    payload = {"agreed": False, "which_is_right": "unsure", "reason": "分歧", "confidence": "low"}
    payload.update(fields)
    return json.dumps(payload, ensure_ascii=False)


class _CallRecorder:
    """按模型名返回预设内容，同时记录调用参数。"""

    def __init__(self, answers, *, judge_content=None, judge_model="m-judge"):
        self.answers = answers
        self.judge_content = judge_content
        self.judge_model = judge_model
        self.calls = []

    async def __call__(self, provider, model, prompt, images=None, json_output=False, **kwargs):
        self.calls.append({
            "provider": provider, "model": model, "prompt": prompt,
            "images": images, "json_output": json_output,
        })
        if self.judge_content is not None and model == self.judge_model:
            content = self.judge_content
        elif model in self.answers:
            content = self.answers[model]
        else:
            content = _answer_content("choice", "A")
        return {
            "content": content, "model": model,
            "provider_name": "mock", "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }

    def by_model(self, model):
        return [call for call in self.calls if call["model"] == model]


# ---------------------------------------------------------------------------
# 1. 裁判接入
# ---------------------------------------------------------------------------

def test_judge_intervenes_on_ab_disagreement(isolated_db, monkeypatch):
    db.init_db()
    recorder = _CallRecorder(
        {"m-primary": _answer_content("choice", "A"), "m-verify": _answer_content("choice", "B")},
        judge_content=_judge_content(agreed=False, which_is_right="A", confidence="high"),
    )
    recorder.judge_model = "m-judge"
    monkeypatch.setattr(ai, "_call_model", recorder)

    report = asyncio.run(eval_mod.run_evaluation(
        [_item("q-judge")],
        {"id": 1}, "m-primary",
        verifier={"id": 2}, verify_model="m-verify",
        judge={"id": 3}, judge_model="m-judge",
        name="裁判测试", concurrency=1,
    ))
    record = report["records"][0]
    assert record["a_correct"] is True and record["b_correct"] is False
    assert record["agreed"] is False and record["verification_comparable"] is True
    # 裁判结果落盘：judge / judge_agrees / judge_correct 三件套齐全。
    assert record["judge"]["agreed"] is False
    assert record["judge"]["which_is_right"] == "A"
    assert record["judge"]["model"] == "m-judge"
    assert record["judge_agrees"] is False
    assert record["judge_correct"] is True

    judge_calls = recorder.by_model("m-judge")
    assert len(judge_calls) == 1
    assert judge_calls[0]["json_output"] is True
    assert "你是独立裁判" in judge_calls[0]["prompt"]
    assert "严格返回 JSON" in judge_calls[0]["prompt"]
    # 裁判要能看到原卷参考答案，才能判定 which_is_right。
    assert "原卷参考答案" in judge_calls[0]["prompt"]

    metrics = report["metrics"]
    assert metrics["has_judge"] is True
    assert metrics["judge_calls"] == 1
    assert metrics["judge_verdicts"] == 1 and metrics["judge_errors"] == 0
    assert metrics["judge_agreed_count"] == 0
    assert metrics["judge_disagreed_count"] == 1
    assert metrics["judge_agreement_rate"] == 0.0
    assert metrics["judge_correct_count"] == 1
    assert metrics["judge_correct_rate"] == 100.0


def test_judge_only_intervenes_on_dispute(isolated_db, monkeypatch):
    db.init_db()
    recorder = _CallRecorder(
        {"m-primary": _answer_content("choice", "A"), "m-verify": _answer_content("choice", "A")},
        judge_content=_judge_content(agreed=True),
    )
    recorder.judge_model = "m-judge"
    monkeypatch.setattr(ai, "_call_model", recorder)
    report = asyncio.run(eval_mod.run_evaluation(
        [_item("q-agree")], {"id": 1}, "m-primary",
        verifier={"id": 2}, verify_model="m-verify",
        judge={"id": 3}, judge_model="m-judge", concurrency=1,
    ))
    record = report["records"][0]
    # 程序化已核验一致（choice_set）：不再浪费一次裁判调用。
    assert record["agreed"] is True
    assert record["judge"] is None and record["judge_correct"] is None
    assert recorder.by_model("m-judge") == []
    assert report["metrics"]["judge_calls"] == 0


def test_judge_used_for_non_comparable_ab_answers(isolated_db, monkeypatch):
    db.init_db()
    recorder = _CallRecorder(
        {"m-primary": _answer_content("text", "牛顿第二定律"),
         "m-verify": _answer_content("text", "欧姆定律")},
        judge_content=_judge_content(agreed=False, which_is_right="unsure"),
    )
    recorder.judge_model = "m-judge"
    monkeypatch.setattr(ai, "_call_model", recorder)
    item = _item("q-text", reference="光合作用产生氧气", kind="text")
    report = asyncio.run(eval_mod.run_evaluation(
        [item], {"id": 1}, "m-primary",
        verifier={"id": 2}, verify_model="m-verify",
        judge={"id": 3}, judge_model="m-judge", concurrency=1,
    ))
    record = report["records"][0]
    assert record["verification_comparable"] is False
    assert record["judge_agrees"] is False
    assert record["judge_correct"] is None  # unsure 无法判定对错
    assert report["metrics"]["judge_calls"] == 1
    assert report["metrics"]["judge_correct_count"] is None


def test_judge_provider_falls_back_to_verifier(isolated_db, monkeypatch):
    db.init_db()
    recorder = _CallRecorder(
        {"m-primary": _answer_content("choice", "A"), "m-verify": _answer_content("choice", "B")},
        judge_content=_judge_content(agreed=False, which_is_right="A"),
    )
    recorder.judge_model = "m-judge"
    monkeypatch.setattr(ai, "_call_model", recorder)
    verifier = {"id": 2, "user_id": 1}
    report = asyncio.run(eval_mod.run_evaluation(
        [_item("q-fallback")], {"id": 1}, "m-primary",
        verifier=verifier, verify_model="m-verify",
        judge=None, judge_model="m-judge", concurrency=1,
    ))
    judge_calls = recorder.by_model("m-judge")
    assert len(judge_calls) == 1
    assert judge_calls[0]["provider"] == verifier  # 没传裁判连接时复用核验模型连接
    assert report["judge_model"] == "m-judge"


def test_judge_parse_failure_is_recorded_not_crashing(isolated_db, monkeypatch):
    db.init_db()
    recorder = _CallRecorder(
        {"m-primary": _answer_content("choice", "A"), "m-verify": _answer_content("choice", "B")},
        judge_content="这不是 JSON",
    )
    recorder.judge_model = "m-judge"
    monkeypatch.setattr(ai, "_call_model", recorder)
    report = asyncio.run(eval_mod.run_evaluation(
        [_item("q-bad-judge")], {"id": 1}, "m-primary",
        verifier={"id": 2}, verify_model="m-verify",
        judge={"id": 3}, judge_model="m-judge", concurrency=1,
    ))
    record = report["records"][0]
    assert record["judge"] is None
    assert record["judge_error"] == "judge_unparsed"
    metrics = report["metrics"]
    assert metrics["judge_calls"] == 1
    assert metrics["judge_verdicts"] == 0 and metrics["judge_errors"] == 1


def test_judge_helpers_parse_and_judge_correctness():
    verdict, error = eval_mod._parse_judge_verdict(
        '```json\n{"agreed": false, "which_is_right": "B"}\n```'
    )
    assert error is None
    assert verdict["agreed"] is False
    assert verdict["which_is_right"] == "B"
    assert verdict["differences"] == []
    assert verdict["summary"] == ""

    assert eval_mod._parse_judge_verdict("not json") == (None, "judge_unparsed")
    assert eval_mod._parse_judge_verdict('{"agreed": "yes"}') == (None, "judge_format_invalid")

    record = {"a_correct": True, "b_correct": False}
    assert eval_mod._judge_verdict_correct({"which_is_right": "A"}, record) is True
    assert eval_mod._judge_verdict_correct({"which_is_right": "B"}, record) is False
    assert eval_mod._judge_verdict_correct({"which_is_right": "neither"}, record) is False
    assert eval_mod._judge_verdict_correct(
        {"which_is_right": "neither"}, {"a_correct": False, "b_correct": False},
    ) is True
    assert eval_mod._judge_verdict_correct({"which_is_right": "unsure"}, record) is None

    assert eval_mod._needs_judge(None) is True
    assert eval_mod._needs_judge({"comparable": False, "agreed": None}) is True
    assert eval_mod._needs_judge({"comparable": True, "agreed": False}) is True
    assert eval_mod._needs_judge({
        "comparable": True, "agreed": True, "method": "literal_equal_unverified",
    }) is True
    assert eval_mod._needs_judge({
        "comparable": True, "agreed": True, "method": "sympy",
    }) is False


# ---------------------------------------------------------------------------
# 2. 统计口径：literal_equal_unverified 不计入一致率/高可信
# ---------------------------------------------------------------------------

def test_literal_only_excluded_from_agreement_and_high_confidence():
    records = [
        {  # 仅字面一致：agreed=True 但未核验，不得计入一致率/高可信。
            "qid": "q-literal", "subject": "数学", "type": "short_answer",
            "strata": ["text"], "stratum": "text", "a_correct": True,
            "has_verifier": True, "agreed": True, "verification_comparable": True,
            "verification_method": "literal_equal_unverified",
            "literal_only": True, "verified": False, "matches_reference": True,
            "input_completeness": 1.0, "cost": {},
        },
        {  # 真正程序化核验一致，且与原卷一致 → 高可信。
            "qid": "q-verified", "subject": "数学", "type": "single_choice",
            "strata": ["text"], "stratum": "text", "a_correct": True,
            "has_verifier": True, "agreed": True, "verification_comparable": True,
            "verification_method": "sympy", "verified": True, "matches_reference": True,
            "input_completeness": 1.0, "cost": {},
        },
    ]
    metrics = eval_mod.summarize_eval(records)
    assert metrics["literal_only_count"] == 1
    assert metrics["literal_only_qids"] == ["q-literal"]
    assert metrics["agreed_count"] == 1          # 只算已核验的一致
    assert metrics["agreement_rate"] == 50.0     # 1/2，而不是 100%
    assert metrics["cross_verified_accuracy"] == 100.0
    assert metrics["high_confidence_count"] == 1
    assert metrics["high_confidence_rate"] == 50.0
    # 有效核验率同样不把字面猜测算作“可程序化比对”。
    assert metrics["programmatic_comparable_count"] == 1
    assert metrics["effective_verification_rate"] == 50.0
    # 严格口径：只在校验结果内部算一致率。
    assert metrics["programmatic_agreement_rate"] == 100.0
    # 明细仍保留，可人工复核。
    assert metrics["strata"]["text"]["literal_only_count"] == 1


def test_run_evaluation_marks_literal_only_without_agreement_credit(isolated_db, monkeypatch):
    db.init_db()
    literal = _answer_content("expression", "x; y")

    async def fake_call(provider, model, prompt, images=None, json_output=False, **kwargs):
        return {"content": literal, "model": model, "provider_name": "mock", "usage": {}}

    monkeypatch.setattr(ai, "_call_model", fake_call)
    item = _item("q-literal-run", reference="x; y", kind="expression")
    report = asyncio.run(eval_mod.run_evaluation(
        [item], {"id": 1}, "m-primary",
        verifier={"id": 2}, verify_model="m-verify", concurrency=1,
    ))
    record = report["records"][0]
    assert record["verification_method"] == "literal_equal_unverified"
    assert record["literal_only"] is True
    assert record["agreed"] is True
    metrics = report["metrics"]
    assert metrics["literal_only_count"] == 1
    assert metrics["agreed_count"] == 0
    assert metrics["agreement_rate"] == 0.0
    assert metrics["high_confidence_count"] == 0
    assert metrics["literal_only_qids"] == ["q-literal-run"]


# ---------------------------------------------------------------------------
# 3. 文本判分不再用脆弱子串包含
# ---------------------------------------------------------------------------

def test_text_prediction_substring_negative_cases():
    # 旧实现用 clean_r in clean_p，会把“不是 A”“正确答案是A”误判为含 A 正确。
    assert eval_mod.check_prediction_correct({"kind": "text", "value": "不是 A"}, "A", "text") is False
    assert eval_mod.check_prediction_correct(
        {"kind": "text", "value": "正确答案是A"}, "A", "text",
    ) is False
    assert eval_mod.check_prediction_correct({"kind": "text", "value": "B"}, "A", "text") is False
    assert eval_mod.check_prediction_correct({"kind": "text", "value": "不是牛顿第二定律"}, "牛顿第二定律", "text") is False


def test_text_prediction_strict_rules_positive_cases():
    # 相等（忽略空白/标点/大小写）
    assert eval_mod.check_prediction_correct({"kind": "text", "value": " 牛 顿 "}, "牛顿", "text") is True
    assert eval_mod.check_prediction_correct({"kind": "text", "value": "Newton"}, "newton", "text") is True
    # 答案集合逐项相等（与顺序无关）
    assert eval_mod.check_prediction_correct({"kind": "text", "value": "C、A"}, "A、C", "text") is True
    assert eval_mod.check_prediction_correct({"kind": "text", "value": "A,C"}, "A、C", "text") is True
    assert eval_mod.check_prediction_correct({"kind": "text", "value": "A、B"}, "A、C", "text") is False
    # 数值等价
    assert eval_mod.check_prediction_correct({"kind": "text", "value": "1/2"}, "0.5", "text") is True
    assert eval_mod.check_prediction_correct({"kind": "text", "value": "3.14 m"}, "314 cm", "text") is True
    # 长文本语义相似度兜底（不是子串包含）
    assert eval_mod.check_prediction_correct(
        {"kind": "text", "value": "根据牛顿第二定律可知"},
        "牛顿第二定律", "text",
    ) is True
    # 非文本 kind 仍走 compare_answers（选择题集合/数值/表达式语义不变）
    assert eval_mod.check_prediction_correct({"kind": "choice", "value": "AB"}, "BA", "choice") is True
    assert eval_mod.check_prediction_correct({"kind": "choice", "value": "A"}, "B", "choice") is False
    assert eval_mod.check_prediction_correct(None, "A", "choice") is False


# ---------------------------------------------------------------------------
# 4. JSONL 逐题断点续评
# ---------------------------------------------------------------------------

def test_resume_skips_completed_qid_model_mode(tmp_path, isolated_db, monkeypatch):
    db.init_db()
    calls = []

    async def fake_call(provider, model, prompt, images=None, json_output=False, **kwargs):
        calls.append(model)
        return {
            "content": _answer_content("choice", "A"), "model": model,
            "provider_name": "mock", "usage": {},
        }

    monkeypatch.setattr(ai, "_call_model", fake_call)
    progress = tmp_path / "eval-progress.jsonl"
    items = [_item("r1"), _item("r2")]

    first = asyncio.run(eval_mod.run_evaluation(
        items, {"id": 1}, "m1", name="续评", concurrency=1, progress_path=progress,
    ))
    assert len(calls) == 2
    assert first["executed_count"] == 2 and first["resumed_count"] == 0
    lines = [line for line in progress.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(lines) == 2
    assert {json.loads(line)["qid"] for line in lines} == {"r1", "r2"}

    # 中断后重跑：已完成的 r1/r2 直接复用，只补跑新增的 r3。
    second = asyncio.run(eval_mod.run_evaluation(
        items + [_item("r3")], {"id": 1}, "m1",
        name="续评", concurrency=1, progress_path=progress, resume=True,
    ))
    assert second["resumed_count"] == 2
    assert second["executed_count"] == 1
    assert len(calls) == 3  # 只有 r3 真正调用模型
    assert [record["qid"] for record in second["records"]] == ["r1", "r2", "r3"]
    assert second["metrics"]["total"] == 3
    assert len(progress.read_text(encoding="utf-8").strip().splitlines()) == 3

    # 坏行（半截 JSON）必须被忽略，而不是让续评炸掉。
    with progress.open("a", encoding="utf-8") as stream:
        stream.write('{"qid": "broken"\n')
    third = asyncio.run(eval_mod.run_evaluation(
        [_item("r1")], {"id": 1}, "m1",
        name="续评", concurrency=1, progress_path=progress, resume=True,
    ))
    assert third["resumed_count"] == 1 and third["executed_count"] == 0
    assert len(calls) == 3

    # 换主模型后 qid+model+mode 不匹配：必须重新执行，不能错误复用旧结果。
    fourth = asyncio.run(eval_mod.run_evaluation(
        [_item("r1")], {"id": 1}, "m2",
        name="续评", concurrency=1, progress_path=progress, resume=True,
    ))
    assert fourth["executed_count"] == 1 and fourth["resumed_count"] == 0
    assert calls[-1] == "m2"


def test_resume_without_progress_file_runs_everything(tmp_path, isolated_db, monkeypatch):
    db.init_db()

    async def fake_call(provider, model, prompt, images=None, json_output=False, **kwargs):
        return {"content": _answer_content("choice", "A"), "model": model, "usage": {}}

    monkeypatch.setattr(ai, "_call_model", fake_call)
    missing = tmp_path / "never-written.jsonl"
    report = asyncio.run(eval_mod.run_evaluation(
        [_item("fresh")], {"id": 1}, "m1", concurrency=1,
        progress_path=missing, resume=True,
    ))
    assert report["executed_count"] == 1
    assert report["resumed_count"] == 0
    assert missing.is_file()


# ---------------------------------------------------------------------------
# 5. CLI 参数
# ---------------------------------------------------------------------------

def test_cli_exposes_judge_and_resume_flags(capsys):
    with pytest.raises(SystemExit) as excinfo:
        eval_mod.main(["--help"])
    assert excinfo.value.code == 0
    help_text = capsys.readouterr().out
    for flag in ("--judge-provider-id", "--judge-model", "--out-jsonl", "--resume"):
        assert flag in help_text


def test_cli_resume_requires_out_jsonl():
    with pytest.raises(SystemExit) as excinfo:
        eval_mod.main(["--resume"])
    assert excinfo.value.code == 2


def test_cli_judge_requires_verifier_ab_pair():
    with pytest.raises(SystemExit) as excinfo:
        eval_mod.main(["--provider-id", "1", "--model", "m-primary", "--judge-model", "m-judge"])
    assert excinfo.value.code == 2
