"""学情分析 API（/api/insight）测试：聚合口径、用户隔离、空状态、AI 建议任务准入。

数据口径（与 app/insight.py 顶部注释一致）：
* 正确率 = 已判定作答（result != 'pending'）中判对的占比（百分比）；
* 知识点按 question_kp 的题目关联逐题统计，一题多知识点时该题分别计入每个知识点；
* 薄弱阈值 accuracy < 70；掌握分布/到期复习/错题数为当前状态，不随窗口截断。
"""
import json
import time
from datetime import datetime, timedelta, timezone

from app import ai, db, practice
from test_ai import save_provider


# ---------------------------------------------------------------------------
# 夹具：直接写应用库表（attempts/mastery/wrong_questions/question_kp）
# ---------------------------------------------------------------------------


def _user_id(client) -> int:
    response = client.get("/api/auth/me")
    assert response.status_code == 200, response.text
    return response.json()["id"]


def _bank_qids(subject: str, count: int = 4) -> list[str]:
    with practice._bank() as con:
        rows = con.execute(
            "SELECT qid FROM questions WHERE subject=? ORDER BY qid LIMIT ?", (subject, count)
        ).fetchall()
    assert len(rows) >= count, f"题库中 {subject} 题目不足"
    return [row[0] for row in rows]


def _kp_ids(subject: str, count: int = 2) -> list[int]:
    with db.connection() as con:
        rows = con.execute(
            "SELECT id FROM knowledge_points WHERE subject=? AND level=3 ORDER BY id LIMIT ?",
            (subject, count),
        ).fetchall()
        if len(rows) < count:
            rows = con.execute(
                "SELECT id FROM knowledge_points WHERE subject=? ORDER BY id LIMIT ?",
                (subject, count),
            ).fetchall()
    assert len(rows) >= count
    return [row[0] for row in rows]


def _insert_attempt(user_id: int, qid: str, result: str, created_at: str | None = None) -> None:
    with db.connection() as con:
        con.execute(
            "INSERT INTO attempts(user_id,qid,answer_text,result,grade_source,created_at) "
            "VALUES(?,?,?,?,?,?)",
            (user_id, qid, "A", result, "reference", created_at or db.utcnow()),
        )


def _insert_mastery(user_id: int, qid: str, level: str) -> None:
    with db.connection() as con:
        con.execute(
            "INSERT INTO mastery(user_id,qid,level,updated_at) VALUES(?,?,?,?) "
            "ON CONFLICT(user_id,qid) DO UPDATE SET level=excluded.level, updated_at=excluded.updated_at",
            (user_id, qid, level, db.utcnow()),
        )


def _insert_wrong(
    user_id: int,
    qid: str,
    *,
    reason: str = "答题错误",
    tags: tuple[str, ...] = (),
    next_review_at: str = "2000-01-01T00:00:00+00:00",
    added_at: str | None = None,
    last_wrong_at: str | None = None,
    archived: int = 0,
) -> None:
    now = added_at or db.utcnow()
    with db.connection() as con:
        con.execute(
            "INSERT INTO wrong_questions(user_id,qid,reason,error_tags_json,failures,streak,"
            "next_review_at,added_at,last_result,last_wrong_at,archived) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                user_id, qid, reason, json.dumps(list(tags), ensure_ascii=False), 1, 0,
                next_review_at, now, "incorrect", last_wrong_at or now, archived,
            ),
        )


def _link_kp(qid: str, kp_id: int, weight: float = 1.0) -> None:
    with db.connection() as con:
        con.execute(
            "INSERT INTO question_kp(qid,kp_id,weight,source,created_at) VALUES(?,?,?,'admin',?)",
            (qid, kp_id, weight, db.utcnow()),
        )


def _seed_standard_study(client) -> dict:
    """数学 4 次作答（3 对 1 错）+ 物理 2 次（1 对 1 待核验）+ 掌握度/错题/知识点关联。"""
    uid = _user_id(client)
    math = _bank_qids("数学", 4)
    physics = _bank_qids("物理", 2)
    kp_a, kp_b = _kp_ids("数学", 2)
    # 一题多知识点：math[0] 同时挂 kp_a / kp_b，验证「分别计入每个知识点」的口径。
    _link_kp(math[0], kp_a)
    _link_kp(math[1], kp_a)
    _link_kp(math[0], kp_b)
    _link_kp(math[2], kp_b)
    _link_kp(math[3], kp_b)
    for qid, result in (
        (math[0], "correct"), (math[1], "incorrect"), (math[2], "correct"), (math[3], "correct"),
        (physics[0], "correct"), (physics[1], "pending"),
    ):
        _insert_attempt(uid, qid, result)
    _insert_mastery(uid, math[0], "熟练掌握")
    _insert_mastery(uid, math[1], "未掌握")
    _insert_wrong(uid, math[1], tags=("计算错误", "审题失误"))
    _insert_wrong(uid, physics[0], reason="手动加入错题")
    return {
        "uid": uid,
        "math": math,
        "physics": physics,
        "kp_a": kp_a,
        "kp_b": kp_b,
    }


def _wait_job(client, job_id: int, timeout: float = 10.0) -> dict:
    deadline = time.time() + timeout
    body = None
    while time.time() < deadline:
        body = client.get(f"/api/ai/jobs/{job_id}").json()
        if body["status"] in ("completed", "failed", "interrupted", "cancelled"):
            return body
        time.sleep(0.05)
    raise AssertionError(f"任务未在 {timeout}s 内结束: {body}")


# ---------------------------------------------------------------------------
# 空状态与口径字段
# ---------------------------------------------------------------------------


def test_empty_state_has_zero_stats_and_explanations(client):
    overview = client.get("/api/insight/overview")
    assert overview.status_code == 200
    body = overview.json()
    summary = body["summary"]
    assert summary["attempts"] == 0
    assert summary["graded_attempts"] == 0
    assert summary["correct_rate"] == 0
    assert summary["mastery_counts"] == {
        "未掌握": 0, "初步掌握": 0, "比较掌握": 0, "熟练掌握": 0,
    }
    assert summary["due_reviews"] == 0
    assert summary["wrong_count"] == 0
    assert summary["ai_jobs"] == 0
    assert body["subjects"] == []
    assert body["has_data"] is False
    # 活动按天补零（默认 90 天），趋势按自然周
    assert len(body["activity"]) == 90
    assert all(item["attempts"] == 0 for item in body["activity"])
    assert len(body["trend"]) >= 12
    assert "correct_rate" in body["caliber"]

    knowledge = client.get("/api/insight/knowledge").json()
    assert knowledge["items"] == []
    assert knowledge["note"]
    assert knowledge["coverage"]["attempted_questions"] == 0

    mistakes = client.get("/api/insight/mistakes").json()
    assert mistakes["by_reason"] == [] and mistakes["by_type"] == [] and mistakes["by_kp"] == []
    assert mistakes["recent"] == [] and mistakes["total"] == 0

    report = client.get("/api/insight/report").json()
    assert report["has_data"] is False
    assert report["range"]["days"] == 90
    assert report["summary"]["attempts"] == 0
    assert report["knowledge"]["weak"] == [] and report["knowledge"]["strong"] == []
    assert report["recommendations"]
    assert report["recommendations"][0]["action"] == "practice"


# ---------------------------------------------------------------------------
# 总览聚合与用户隔离
# ---------------------------------------------------------------------------


def test_overview_aggregates_counts_and_isolates_users(client, second_client):
    _seed_standard_study(client)
    body = client.get("/api/insight/overview").json()
    summary = body["summary"]
    assert summary["attempts"] == 6
    assert summary["graded_attempts"] == 5
    assert summary["correct_attempts"] == 4
    assert summary["correct_rate"] == 80.0
    assert summary["mastery_counts"]["熟练掌握"] == 1
    assert summary["mastery_counts"]["未掌握"] == 1
    assert summary["wrong_count"] == 2
    assert summary["due_reviews"] == 2
    assert summary["ai_jobs"] == 0
    assert body["has_data"] is True

    subjects = {item["subject"]: item for item in body["subjects"]}
    assert subjects["数学"]["attempts"] == 4
    assert subjects["数学"]["graded_attempts"] == 4
    assert subjects["数学"]["accuracy"] == 75.0
    assert subjects["数学"]["mastery"]["熟练掌握"] == 1
    assert subjects["物理"]["attempts"] == 2
    # 物理只有 1 次已判定作答 → graded_attempts=1，待核验作答不计入正确率
    assert subjects["物理"]["graded_attempts"] == 1
    assert subjects["物理"]["accuracy"] == 100.0

    today = next(item for item in body["activity"] if item["attempts"])
    assert today["attempts"] == 6 and today["correct"] == 4
    assert sum(item["attempts"] for item in body["trend"]) == 6

    # 严格用户隔离：另一个账号看不到任何统计
    other = second_client.get("/api/insight/overview").json()
    assert other["summary"]["attempts"] == 0
    assert other["summary"]["wrong_count"] == 0
    assert other["subjects"] == []
    assert other["has_data"] is False


def test_overview_days_window_filters_attempts(client):
    uid = _user_id(client)
    qid = _bank_qids("数学", 1)[0]
    _insert_attempt(uid, qid, "correct")
    # 300 天前的旧作答：默认 90 天窗口不统计，显式 days=400 时计入
    old = (datetime.now(timezone.utc) - timedelta(days=300)).isoformat()
    _insert_attempt(uid, qid, "incorrect", created_at=old)
    now_window = client.get("/api/insight/overview").json()["summary"]
    assert now_window["attempts"] == 1
    wide = client.get("/api/insight/overview?days=400").json()["summary"]
    assert wide["attempts"] == 2
    assert wide["graded_attempts"] == 2
    assert wide["correct_rate"] == 50.0
    assert wide["window_days"] == 400


# ---------------------------------------------------------------------------
# 知识点掌握
# ---------------------------------------------------------------------------


def test_knowledge_accuracy_weak_threshold_and_multi_kp(client):
    seed = _seed_standard_study(client)
    body = client.get("/api/insight/knowledge?subject=数学").json()
    items = {item["kp_id"]: item for item in body["items"]}
    assert set(items) == {seed["kp_a"], seed["kp_b"]}

    kp_a = items[seed["kp_a"]]
    # math[0] correct + math[1] incorrect → 50%，低于 70% → 薄弱
    assert kp_a["attempts"] == 2
    assert kp_a["accuracy"] == 50.0
    assert kp_a["weak"] is True
    assert kp_a["path"].count("/") >= 2
    assert kp_a["mastery_level"] in ("未掌握", "初步掌握", "比较掌握", "熟练掌握")
    # math[0] 一题挂两个知识点 → kp_b 应把 math[0] 的作答也计入（不拆分）
    kp_b = items[seed["kp_b"]]
    assert kp_b["attempts"] == 3
    assert kp_b["accuracy"] == 100.0
    assert kp_b["weak"] is False
    # 正确率升序：最弱在前
    assert [item["accuracy"] for item in body["items"]] == sorted(
        item["accuracy"] for item in body["items"]
    )
    assert body["coverage"]["linked_attempts"] == 4

    # 学科筛选：物理题没有知识点关联 → 空数组 + 明确说明
    physics = client.get("/api/insight/knowledge?subject=物理").json()
    assert physics["items"] == []
    assert physics["note"]


def test_knowledge_note_when_attempted_questions_unlinked(client, second_client):
    # 学生账号有作答但没有任何知识点关联 → 空数组 + 说明，不伪装成「未掌握」
    uid = _user_id(second_client)
    qid = _bank_qids("数学", 1)[0]
    _insert_attempt(uid, qid, "correct")
    body = second_client.get("/api/insight/knowledge").json()
    assert body["items"] == []
    assert "知识点" in body["note"]
    assert body["coverage"]["attempted_questions"] == 1
    assert body["coverage"]["linked_attempts"] == 0


# ---------------------------------------------------------------------------
# 错题分布
# ---------------------------------------------------------------------------


def test_mistakes_distribution_subject_filter_and_window(client, second_client):
    seed = _seed_standard_study(client)
    math = client.get("/api/insight/mistakes?subject=数学").json()
    assert math["total"] == 1
    reasons = {row["reason"]: row["count"] for row in math["by_reason"]}
    # 人工错误标签优先，一题多标签分别计入
    assert reasons == {"计算错误": 1, "审题失误": 1}
    assert math["by_type"] and math["by_type"][0]["count"] == 1
    assert math["by_kp"]
    assert math["by_kp"][0]["kp_id"] in (seed["kp_a"], seed["kp_b"])
    assert math["recent"][0]["qid"] == seed["math"][1]
    assert math["recent"][0]["subject"] == "数学"
    assert "计算错误" in math["recent"][0]["reason"]

    physics = client.get("/api/insight/mistakes?subject=物理").json()
    assert physics["total"] == 1
    assert physics["by_reason"] == [{"reason": "手动加入错题", "count": 1}]

    # 窗口外错题：不计入分布，但仍计入 active_total（当前状态总量）
    old = (datetime.now(timezone.utc) - timedelta(days=300)).isoformat()
    _insert_wrong(seed["uid"], seed["math"][2], added_at=old, last_wrong_at=old)
    windowed = client.get("/api/insight/mistakes").json()
    assert windowed["total"] == 2
    assert windowed["active_total"] == 3

    # 只有窗口外错题的账号：分布为空但说明「更早的错题未计入」
    uid = _user_id(second_client)
    _insert_wrong(uid, seed["physics"][0], added_at=old, last_wrong_at=old)
    old_only = second_client.get("/api/insight/mistakes").json()
    assert old_only["total"] == 0
    assert old_only["active_total"] == 1
    assert "更早" in (old_only["note"] or "")


# ---------------------------------------------------------------------------
# 报告与规则推荐
# ---------------------------------------------------------------------------


def test_report_sections_and_rule_recommendations(client):
    seed = _seed_standard_study(client)
    report = client.get("/api/insight/report?subject=数学").json()
    assert report["range"]["subject"] == "数学"
    assert report["range"]["days"] == 90
    assert report["summary"]["attempts"] == 4
    assert report["subjects"] and all(row["subject"] == "数学" for row in report["subjects"])
    assert report["knowledge"]["weak"], "数学薄弱知识点应进入 weak 分组"
    weak = report["knowledge"]["weak"][0]
    assert weak["weak"] is True and weak["accuracy"] < 70
    assert isinstance(report["knowledge"]["not_started"], list)
    assert report["mistakes"]["total"] == 1
    recs = report["recommendations"]
    assert 1 <= len(recs) <= 6
    titles = " ".join(item["title"] for item in recs)
    assert "专项突破" in titles
    assert any(item.get("action") == "wrongbook" for item in recs)
    assert "generated_at" in report and "caliber" in report


# ---------------------------------------------------------------------------
# AI 建议任务（insight-report）准入与执行
# ---------------------------------------------------------------------------


def test_narrative_admission_checks_provider_and_data(client, second_client):
    provider = save_provider(client)
    # 没有作答/错题记录 → 422，不生成编造内容
    empty = client.post(
        "/api/insight/narrative",
        json={"provider_id": provider["id"], "model": "test-model"},
    )
    assert empty.status_code == 422
    assert "记录" in empty.json()["detail"]

    # 有数据后：他人连接不可用（403/404），参数缺失 422
    _seed_standard_study(client)
    stolen = second_client.post(
        "/api/insight/narrative",
        json={"provider_id": provider["id"], "model": "test-model"},
    )
    assert stolen.status_code in (403, 404)
    missing = client.post("/api/insight/narrative", json={"model": "test-model"})
    assert missing.status_code == 422


def test_narrative_job_runs_with_mocked_model(client, monkeypatch):
    provider = save_provider(client)
    seed = _seed_standard_study(client)
    captured = {}

    async def fake_call(provider_row, model, prompt, *args, **kwargs):
        captured["prompt"] = prompt
        captured["system_prompt"] = kwargs.get("system_prompt")
        return {
            "content": (
                "## 总体表现\n近 90 天共 6 次作答，已判定 5 次，正确率 80.0%。\n\n"
                "## 薄弱知识点\n集合的含义与表示正确率 50.0%，低于 70% 的薄弱线。\n\n"
                "## 错题与复习\n有 2 道错题到期，先复习计算错误类错题。\n\n"
                "## 下一步建议\n1. 每天练 2 道集合基础题。\n2. 复盘审题步骤。"
            ),
            "usage": {"prompt_tokens": 120, "completion_tokens": 80, "total_tokens": 200},
            "model": model,
        }

    monkeypatch.setattr(ai, "_call_model", fake_call)
    created = client.post(
        "/api/insight/narrative",
        json={"provider_id": provider["id"], "model": "test-model", "subject": "数学"},
    )
    assert created.status_code == 200, created.text
    job_id = created.json()["id"]
    job = _wait_job(client, job_id)
    assert job["status"] == "completed", job
    result = job["result"]
    assert result["ai_generated"] is True
    assert result["markdown"].startswith("## 总体表现")
    assert "AI 生成" in result["ai_notice"]
    assert result["prompt_version"]
    assert result["statistics_snapshot"]["summary"]["attempts"] == 4
    assert result["statistics_snapshot"]["summary"]["correct_rate"] == 75.0
    assert result["statistics_snapshot"]["knowledge"]["weak"][0]["title"]
    assert result["models"][0]["model"] == "test-model"
    assert result["input_ref"]["task"] == "insight-report"
    assert result["usage"]["total_tokens"] == 200

    # 模型 prompt 里拿到的是真实统计（不是让模型自由发挥）；禁止编造班级/排名
    prompt = captured["prompt"]
    assert '"attempts": 4' in prompt
    assert '"correct_rate": 75.0' in prompt
    assert "不得编造" in prompt
    assert captured["system_prompt"] and "禁止编造" in captured["system_prompt"]
    # 只统计本人数据：另一个账号没有数据，不会拿到本次结果
    other_jobs = client.get("/api/ai/jobs").json()["items"]
    assert any(item["id"] == job_id and item["task"] == "insight-report" for item in other_jobs)


def _create_paper_question(client) -> dict:
    response = client.post("/api/paperlab/questions", json={
        "type": "SUBJECTIVE", "subject": "数学", "grade": "高中一年级",
        "difficulty": 0.4, "knowledge_tags": ["函数"],
        "content": {
            "stem": "证明函数 $f(x)=x^2$ 在 $[0,+\\infty)$ 上单调递增。",
            "answers": ["任取 x1<x2 比较。"], "explanation": "略。", "rubric": "略。",
        },
        "status": "ready",
    })
    assert response.status_code == 200, response.text
    return response.json()["question"]


def _create_paper(client, title: str = "函数整卷练习") -> dict:
    first = _create_paper_question(client)
    second = _create_paper_question(client)
    response = client.post("/api/paperlab/papers", json={
        "title": title, "kind": "manual", "subject": "数学", "grade": "高中一年级",
        "layout": {"paper": "A4", "columns": 1},
        "items": [
            {"ref": f"app:{first['id']}", "score": 10},
            {"ref": f"app:{second['id']}", "score": 10},
        ],
    })
    assert response.status_code == 200, response.text
    return response.json()["paper"]


def _seed_integrations(client) -> dict:
    """构造课堂/整卷练习/题库/助手四类整合模块数据（全部属于当前用户）。"""
    uid = _user_id(client)
    now = db.utcnow()
    # 先通过 API 建卷（会打开自己的写事务），再在单个连接里批量插入整合模块数据，避免 SQLite 锁冲突。
    paper = _create_paper(client)
    with db.connection() as con:
        course_ids = []
        scene_ids = []
        for index, status in enumerate(("ready", "draft")):
            cursor = con.execute(
                "INSERT INTO cls_courses(user_id,title,topic,subject,grade,mode,description,"
                "requirements,status,outline_json,config_json,scene_count,cover_json,"
                "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (uid, f"函数课堂 {index + 1}", "函数", "数学", "高中一年级", "standard",
                 "", "", status, "{}", "{}", 2 if index == 0 else 1, "{}", now, now),
            )
            course_id = cursor.lastrowid
            course_ids.append(course_id)
            for order in range(2 if index == 0 else 1):
                scene_status = "ready" if (index == 0 and order == 0) else "pending"
                scene_cursor = con.execute(
                    "INSERT INTO cls_scenes(course_id,user_id,scene_type,order_index,title,"
                    "content_json,meta_json,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (course_id, uid, "slide", order, f"场景 {order + 1}", "{}", "{}",
                     scene_status, now, now),
                )
                scene_ids.append(scene_cursor.lastrowid)
        for score, max_score in ((8, 10), (6, 10)):
            con.execute(
                "INSERT INTO cls_quiz_attempts(user_id,course_id,scene_id,answers_json,score,"
                "max_score,detail_json,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (uid, course_ids[0], scene_ids[0], "[]", score, max_score, "{}", now),
            )

        attempt_cursor = con.execute(
            "INSERT INTO pl_paper_attempts(user_id,paper_id,paper_version,status,answers_json,"
            "score,max_score,detail_json,started_at,submitted_at,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (uid, paper["id"], 1, "submitted", "{}", 15, 20, "{}", now, now, now),
        )
        attempt_id = attempt_cursor.lastrowid
        for position, result in ((1, "correct"), (2, "incorrect"), (3, "pending")):
            con.execute(
                "INSERT INTO pl_paper_answers(attempt_id,user_id,item_position,question_ref,"
                "answer_json,result,score,max_score,detail_json,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (attempt_id, uid, position, f"app:{position}", "null", result,
                 5 if result == "correct" else 0, 5, "{}", now),
            )

        conversation_cursor = con.execute(
            "INSERT INTO asst_conversations(user_id,title,status,view_json,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?)",
            (uid, "错题复盘", "done", "{}", now, now),
        )
        conversation_id = conversation_cursor.lastrowid
        for role, text in (("user", "帮我复盘错题"), ("assistant", "先看函数单调性。")):
            con.execute(
                "INSERT INTO asst_messages(conversation_id,user_id,role,parts_json,created_at) "
                "VALUES(?,?,?,?,?)",
                (conversation_id, uid, role,
                 json.dumps([{"type": "text", "text": text}], ensure_ascii=False), now),
            )

    # 题库产出：一道已核验 + 一道待核验；答题卡一张（全部走 API）
    verified_question = _create_paper_question(client)
    verified = client.post(
        f"/api/paperlab/questions/{verified_question['id']}/verify", json={"status": "verified"})
    assert verified.status_code == 200, verified.text
    _create_paper_question(client)
    card = client.post(f"/api/paperlab/papers/{paper['id']}/cards", json={
        "preset": "A4", "columns": 1,
    })
    assert card.status_code == 200, card.text
    return {"uid": uid, "paper": paper, "course_ids": course_ids}


def test_overview_integrations_counts_and_isolation(client, second_client):
    _seed_standard_study(client)
    seed = _seed_integrations(client)
    body = client.get("/api/insight/overview").json()
    integrations = body["integrations"]
    classroom = integrations["classroom"]
    assert classroom["courses"] == 2
    assert classroom["ready_courses"] == 1
    assert classroom["scenes_total"] == 3
    assert classroom["scenes_completed"] == 1
    assert classroom["quiz_attempts"] == 2
    assert classroom["quiz_accuracy"] == 70.0
    papers = integrations["papers"]
    assert papers["practice_attempts"] == 1
    assert papers["practice_avg_score"] == 75.0
    assert papers["practice_accuracy"] == 50.0
    assert len(papers["recent"]) == 1
    assert papers["recent"][0]["paper_title"] == seed["paper"]["title"]
    assert papers["recent"][0]["score"] == 15.0 and papers["recent"][0]["max_score"] == 20.0
    library = integrations["library"]
    assert library["questions"] >= 3
    assert library["verified"] >= 1
    assert library["pending_review"] >= 1
    assert library["papers"] >= 1
    assert library["cards"] == 1
    assistant = integrations["assistant"]
    assert assistant["conversations"] == 1
    assert assistant["messages"] == 2
    # has_data：即使没有作答/错题，仅有整合模块数据也算有数据（但仍保留原有统计口径）
    assert body["has_data"] is True
    assert "integrations" in body["caliber"] and "papers" in body["caliber"]
    # 用户隔离：其他账号全部为 0/空
    other = second_client.get("/api/insight/overview").json()["integrations"]
    assert other["classroom"]["courses"] == 0
    assert other["papers"]["practice_attempts"] == 0 and other["papers"]["recent"] == []
    assert other["library"]["questions"] == 0
    assert other["assistant"]["conversations"] == 0


def test_overview_activity_mix_and_empty_integrations(client, second_client):
    _seed_standard_study(client)
    _seed_integrations(client)
    body = client.get("/api/insight/overview").json()
    mix = body["activity_mix"]
    counts = {item["key"]: item["count"] for item in mix["items"]}
    assert counts["practice"] == 6
    assert counts["classroom"] == 2
    assert counts["paper_practice"] == 1
    assert counts["assistant"] == 1
    assert mix["total"] == 10
    assert mix["days"] == 90
    assert len(mix["items"]) == 4
    # 空账号：四块 0 + activity_mix 0，接口仍 200
    empty = second_client.get("/api/insight/overview").json()
    assert empty["integrations"]["classroom"]["quiz_attempts"] == 0
    assert empty["integrations"]["papers"]["practice_attempts"] == 0
    assert empty["integrations"]["library"]["questions"] == 0
    assert empty["integrations"]["assistant"]["messages"] == 0
    assert empty["activity_mix"]["total"] == 0
    assert all(item["count"] == 0 for item in empty["activity_mix"]["items"])
    assert empty["has_data"] is False


def test_report_integrations_and_actionable_recommendations(client):
    _seed_standard_study(client)
    _seed_integrations(client)
    report = client.get("/api/insight/report").json()
    assert report["integrations"]["classroom"]["courses"] == 2
    assert report["integrations"]["papers"]["practice_attempts"] == 1
    assert len(report["activity_mix"]["items"]) == 4
    assert report["has_data"] is True
    actions = {item["action"] for item in report["recommendations"]}
    titles = " ".join(item["title"] for item in report["recommendations"])
    # 有课程未完成 + 有题目待核验 → 真实行动建议
    assert "classroom" in actions and "继续未完成的 AI 课堂" in titles
    assert "mybank" in actions and "核验待处理的题目" in titles
    for item in report["recommendations"]:
        assert item["title"] and item["reason"]
    assert 1 <= len(report["recommendations"]) <= 6


def test_narrative_snapshot_contains_integration_dimensions(client, monkeypatch):
    provider = save_provider(client)
    _seed_standard_study(client)
    _seed_integrations(client)
    captured = {}

    async def fake_call(provider_row, model, prompt, *args, **kwargs):
        captured["prompt"] = prompt
        return {
            "content": "## 总体表现\n真实统计。\n\n## 薄弱知识点\n暂无相关记录。\n\n"
                       "## 错题与复习\n2 道到期。\n\n## 下一步建议\n1. 继续未完成的课堂。",
            "usage": {"prompt_tokens": 50, "completion_tokens": 30, "total_tokens": 80},
            "model": model,
        }

    monkeypatch.setattr(ai, "_call_model", fake_call)
    created = client.post("/api/insight/narrative",
                          json={"provider_id": provider["id"], "model": "test-model"})
    assert created.status_code == 200, created.text
    job = _wait_job(client, created.json()["id"])
    assert job["status"] == "completed", job
    snapshot = job["result"]["statistics_snapshot"]
    assert snapshot["integrations"]["classroom"]["courses"] == 2
    assert snapshot["integrations"]["papers"]["practice_attempts"] == 1
    assert snapshot["integrations"]["library"]["pending_review"] >= 1
    assert snapshot["integrations"]["assistant"]["conversations"] == 1
    assert snapshot["activity_mix"]["total"] >= 1
    prompt = captured["prompt"]
    assert '"integrations"' in prompt and '"activity_mix"' in prompt
    assert "不得编造" in prompt and "整合模块" in prompt


def test_narrative_job_failure_is_recorded_not_faked(client, monkeypatch):
    """模型调用失败时任务落 failed 并保留中文错误，不伪造 ai_generated 结果。"""
    provider = save_provider(client)
    _seed_standard_study(client)

    async def failing_call(*_args, **_kwargs):
        raise ValueError("模型服务返回 HTTP 500；请检查 Key、额度或稍后重试")

    monkeypatch.setattr(ai, "_call_model", failing_call)
    created = client.post(
        "/api/insight/narrative",
        json={"provider_id": provider["id"], "model": "test-model"},
    )
    assert created.status_code == 200, created.text
    job = _wait_job(client, created.json()["id"])
    assert job["status"] == "failed", job
    assert "模型服务" in (job["error"] or "")
    assert job["result"] is None
