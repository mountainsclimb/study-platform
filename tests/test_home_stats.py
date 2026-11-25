"""首页统计口径（/api/insight/home）回归：题库门禁口径、个人首次正确率/重复/待判分、样本不足标注。

数据全部为测试自造：题库用 ``tests/bank_fixture.make_bank`` 造小型只读题库并通过
monkeypatch 指向它；应用库数据（作答/错题/课堂/整卷/我的题库）直接写入 ``client`` 的
隔离临时库。这样既不依赖 r5 固定题目，也不改动只读发布包。

口径与 app/home_stats.py 顶部注释一致：
* 可练 = practice 状态扣除答案泄露/硬隔离/临时隔离；
* 可自动判分 = 可练 + grading auto，再扣除题组材料缺失、答案缺失/占位、缺图/多选待定；
* 待核验 = 题库可见题中不能自动判分的部分（可自动判分 + 待核验 = 可见题数）；
* 首次正确率按「每题第一次已判定作答」去重；重复练习/待判分分别统计。
"""
from datetime import datetime, timedelta, timezone
import json

import pytest

from app import db, home_stats, practice
from bank_fixture import make_bank, question_row


def _clear_caches():
    practice._sqlite_question_columns.cache_clear()
    practice._sqlite_group_columns.cache_clear()
    practice._source_groups.cache_clear()
    practice._verified_index_key = None
    practice._verified_fts_key = None
    home_stats._BANK_CACHE["key"] = None
    home_stats._BANK_CACHE["value"] = None


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch):
    _clear_caches()
    yield
    _clear_caches()


DUE_QID = "fx:1"
CLEAN_QIDS = ("fx:1", "fx:2", "fx:3")

BANK_ROWS = [
    # 干净可自动判分：3 道
    question_row("fx:1", "集合的含义与表示：下列集合中表示同一集合的是"),
    question_row("fx:2", "函数的单调性：判断下列函数在区间上的单调性"),
    question_row("fx:3", "概率初步：计算事件的概率"),
    # 状态门禁：pending grading / review
    question_row("fx:4", "待核验参考答案的题目", grading_status="pending"),
    question_row("fx:5", "评审状态待核验的题目", practice_status="review"),
    question_row("fx:6", "评审+待核验的题目", practice_status="review", grading_status="pending"),
    # 数据侧质量标记：缺图 / 缺答案 / 多选答案待定
    question_row(
        "fx:7", "题干提到图，但数据源无图片",
        quality_flags_json='[{"code": "missing_figure", "message": "题干提到图，但数据源无图片"}]',
    ),
    question_row("fx:8", "参考答案为空的题目", answer_text=""),
    question_row(
        "fx:14", "答案含多个选项字母的题目",
        quality_flags_json='[{"code": "multi_choice_answer", "message": "答案含多个选项字母"}]',
    ),
    # 题组门禁：成员有 group_id 但题组记录缺失
    question_row("fx:9", "脱离题组的子题", group_id="m_9999", group_role="sub"),
    # 题组成员：题组记录存在，可自动判分
    question_row("fx:10", "题组内可自动判分的成员", group_id="m_0002", group_role="sub"),
    # 硬隔离 / 上下文 / 答案泄露
    question_row("fx:11", "数据侧隔离的题目", practice_status="quarantined"),
    question_row("fx:12", "仅为上下文材料的成员", practice_status="context"),
    question_row("fx:13", "答案泄漏门禁标记题【答案】A"),
    # disabled 判分状态：可练但待核验
    question_row("fx:15", "自动判分已停用的题目", grading_status="disabled"),
]

BANK_GROUPS = [{
    "group_id": "docA:m_0002", "doc_id": "docA", "subject": "数学", "year": 2024,
    "material_text": "", "parent_stem": "", "shared_blocks_json": "[]",
    "member_index_json": "[]", "n_members": 1, "n_practice": 1,
    "bank_version": "bank-fixture-r1",
}]

# 期望值（15 题：3 题被运行期门禁排除，12 题可见）
EXPECTED_TOTAL = 15
EXPECTED_EXCLUDED = 3
EXPECTED_VISIBLE = 12
EXPECTED_PRACTICE_POOL = 10
EXPECTED_AUTO = 4
EXPECTED_PENDING = 8


@pytest.fixture
def fixture_bank(tmp_path, monkeypatch):
    bank = make_bank(tmp_path / "bank", BANK_ROWS, BANK_GROUPS)
    monkeypatch.setattr(db, "BANK_DIR", bank)
    _clear_caches()
    return bank


def _user_id(client) -> int:
    response = client.get("/api/auth/me")
    assert response.status_code == 200, response.text
    return response.json()["id"]


def _iso(delta: timedelta = timedelta(0)) -> str:
    return (datetime.now(timezone.utc) - delta).isoformat()


def _insert_attempt(user_id: int, qid: str, result: str, created_at: str | None = None) -> int:
    with db.connection() as con:
        cursor = con.execute(
            "INSERT INTO attempts(user_id,qid,answer_text,result,grade_source,created_at) "
            "VALUES(?,?,?,?,?,?)",
            (user_id, qid, "A", result, "reference", created_at or db.utcnow()),
        )
        return cursor.lastrowid


def _insert_wrong(user_id: int, qid: str, *, next_review_at: str) -> None:
    now = db.utcnow()
    with db.connection() as con:
        con.execute(
            "INSERT INTO wrong_questions(user_id,qid,reason,error_tags_json,failures,streak,"
            "next_review_at,added_at,last_result,last_wrong_at,archived) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (user_id, qid, "答题错误", "[]", 1, 0, next_review_at, now, "incorrect", now, 0),
        )


def _insert_classroom_quiz(user_id: int, created_at: str) -> None:
    now = db.utcnow()
    with db.connection() as con:
        course = con.execute(
            "INSERT INTO cls_courses(user_id,title,topic,subject,grade,mode,description,"
            "requirements,status,outline_json,config_json,scene_count,cover_json,"
            "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (user_id, "函数课堂", "函数", "数学", "高中一年级", "standard",
             "", "", "ready", "{}", "{}", 1, "{}", now, now),
        ).lastrowid
        scene = con.execute(
            "INSERT INTO cls_scenes(course_id,user_id,scene_type,order_index,title,"
            "content_json,meta_json,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (course, user_id, "slide", 0, "场景 1", "{}", "{}", "ready", now, now),
        ).lastrowid
        con.execute(
            "INSERT INTO cls_quiz_attempts(user_id,course_id,scene_id,answers_json,score,"
            "max_score,detail_json,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (user_id, course, scene, "[]", 8, 10, "{}", created_at),
        )


def _insert_paper_attempt(user_id: int, *, submitted_at: str, paper_id: int = 901) -> None:
    with db.connection() as con:
        con.execute(
            "INSERT INTO pl_paper_attempts(user_id,paper_id,paper_version,status,answers_json,"
            "score,max_score,detail_json,started_at,submitted_at,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (user_id, paper_id, 1, "submitted", "{}", 15, 20, "{}",
             submitted_at, submitted_at, submitted_at),
        )


def _insert_library_question(user_id: int, *, verify_status: str, status: str) -> None:
    now = db.utcnow()
    with db.connection() as con:
        con.execute(
            "INSERT INTO pl_questions(user_id,source,type,subject,grade,content_json,status,"
            "verify_status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (user_id, "manual", "SUBJECTIVE", "数学", "高中一年级", "{}",
             status, verify_status, now, now),
        )


def _insert_question_kp(qid: str, kp_id: int) -> None:
    with db.connection() as con:
        con.execute(
            "INSERT INTO question_kp(qid,kp_id,weight,source,created_at) VALUES(?,?,?,'admin',?)",
            (qid, kp_id, 1.0, db.utcnow()),
        )


def _kp_ids(subject: str, count: int = 2) -> list[int]:
    with db.connection() as con:
        rows = con.execute(
            "SELECT id FROM knowledge_points WHERE subject=? ORDER BY id LIMIT ?",
            (subject, count),
        ).fetchall()
    assert len(rows) >= count, f"知识点种子缺少 {subject}"
    return [row[0] for row in rows]


# ---------------------------------------------------------------------------
# 题库全局口径
# ---------------------------------------------------------------------------


def test_bank_caliber_reuses_runtime_gates(client, fixture_bank):
    body = client.get("/api/insight/home").json()
    bank = body["bank"]
    assert bank["available"] is True
    assert bank["total_questions"] == EXPECTED_TOTAL
    assert bank["excluded_by_gates"] == EXPECTED_EXCLUDED
    assert bank["visible_questions"] == EXPECTED_VISIBLE
    # 可练：practice 状态扣除泄露/硬隔离/上下文
    assert bank["practice_pool"] == EXPECTED_PRACTICE_POOL
    # 可自动判分：practice+auto 再扣除缺图/缺答案/题组缺失/多选待定
    assert bank["auto_gradable"] == EXPECTED_AUTO
    # 待核验 = 可见题 - 可自动判分（两数相加必须回到可见题，不重不漏）
    assert bank["pending_verification"] == EXPECTED_PENDING
    assert bank["auto_gradable"] + bank["pending_verification"] == bank["visible_questions"]
    # 门禁明细：3 题被泄露/隔离排除；4 道 practice+auto 候选各命中一种数据门禁
    assert bank["gates"]["leak_or_quarantine"] == 3
    assert bank["gates"]["isolated"] == 0
    assert bank["gates"]["missing_figure"] == 1
    assert bank["gates"]["missing_answer"] == 1
    assert bank["gates"]["missing_group"] == 1
    assert bank["gates"]["multi_choice_answer"] == 1
    assert bank["status_counts"]["practice"] == EXPECTED_PRACTICE_POOL
    assert bank["status_counts"]["review"] == 2
    assert bank["bank_version"] == "bank-fixture-r1"
    assert "可自动判分" in bank["caliber"]
    # 题库题本身与用户无关：另一账号看到同一组真实计数
    assert body["personal"]["attempts"] == 0


def test_bank_caliber_excludes_admin_isolation(client, fixture_bank):
    before = client.get("/api/insight/home").json()["bank"]
    user_id = _user_id(client)
    with db.connection() as con:
        con.execute(
            "INSERT INTO content_isolations(qid,group_id,issue_id,reason,created_by,"
            "created_at,revoked_at) VALUES(?,?,?,?,?,?,NULL)",
            ("fx:2", None, None, "管理员按报错证据临时隔离", user_id, db.utcnow()),
        )
    practice._invalidate_isolations()
    home_stats._BANK_CACHE["key"] = None
    after = client.get("/api/insight/home").json()["bank"]
    assert after["available"] is True
    assert after["excluded_by_gates"] == before["excluded_by_gates"] + 1
    assert after["gates"]["isolated"] == 1
    assert after["practice_pool"] == before["practice_pool"] - 1
    assert after["auto_gradable"] == before["auto_gradable"] - 1
    assert after["visible_questions"] == before["visible_questions"] - 1


def test_bank_caliber_matches_real_baseline_invariants(client):
    """基线题库（r5）只读抽样：三数自洽、可自动判分不超过可练、数字非写死。"""
    body = client.get("/api/insight/home").json()
    bank = body["bank"]
    if not bank.get("available"):
        pytest.skip("当前环境没有可用题库")
    assert bank["total_questions"] > 0
    assert 0 < bank["practice_pool"] <= bank["visible_questions"] <= bank["total_questions"]
    assert 0 <= bank["auto_gradable"] <= bank["practice_pool"]
    assert bank["auto_gradable"] + bank["pending_verification"] == bank["visible_questions"]
    assert bank["status_counts"]["practice"] >= bank["practice_pool"]
    assert isinstance(bank["gates"], dict) and "missing_figure" in bank["gates"]
    assert bank["bank_version"] == "" or bank["bank_version"].startswith("bank-")


def test_bank_caliber_degrades_when_bank_missing(client, tmp_path, monkeypatch):
    monkeypatch.setattr(db, "BANK_DIR", tmp_path / "missing-bank")
    _clear_caches()
    body = client.get("/api/insight/home")
    assert body.status_code == 200
    payload = body.json()
    assert payload["bank"]["available"] is False
    assert payload["bank"]["note"]
    # 题库不可用时个人统计仍返回真实（空）数据，不 500、不伪造
    assert payload["personal"]["first_attempt"]["samples"] == 0
    assert payload["personal"]["today"]["total"] == 0


# ---------------------------------------------------------------------------
# 个人口径：今日练习 / 首次正确率 / 重复练习 / 待判分 / 待复习
# ---------------------------------------------------------------------------


def test_personal_metrics_dedupe_today_and_activity_mix(client, fixture_bank):
    user_id = _user_id(client)
    old = _iso(timedelta(days=2))
    # fx:1：两天前首次作对（首次正确率样本），今天又练一次（重复练习，不计首次样本）
    _insert_attempt(user_id, "fx:1", "correct", old)
    _insert_attempt(user_id, "fx:1", "incorrect", _iso())
    # fx:2：今天首次作答错误
    _insert_attempt(user_id, "fx:2", "incorrect", _iso())
    # fx:3：今天作答待判分（最近一次仍 pending）
    _insert_attempt(user_id, "fx:3", "pending", _iso())
    # 个人题引用按题目身份参与同一套去重口径（本题只在应用库出现）
    _insert_attempt(user_id, "app:42", "pending", old)
    _insert_wrong(user_id, "fx:2", next_review_at=_iso(timedelta(days=1)))
    _insert_wrong(user_id, "fx:1", next_review_at=_iso(timedelta(days=-5)))
    _insert_classroom_quiz(user_id, _iso())
    _insert_classroom_quiz(user_id, _iso(timedelta(days=1)))
    _insert_paper_attempt(user_id, submitted_at=_iso(), paper_id=901)
    _insert_paper_attempt(user_id, submitted_at=old, paper_id=902)
    _insert_library_question(user_id, verify_status="pending", status="ready")
    _insert_library_question(user_id, verify_status="verified", status="draft")

    payload = client.get("/api/insight/home").json()
    personal = payload["personal"]

    # 今日：题库/个人题作答 3 次 + 课堂小测 1 次 + 已提交整卷 1 次
    assert personal["today"]["practice"] == 3
    assert personal["today"]["classroom"] == 1
    assert personal["today"]["paper"] == 1
    assert personal["today"]["total"] == 5
    # 待复习：1 道到期；错题总数 2
    assert personal["due_reviews"] == 1
    assert personal["wrong_count"] == 2
    # 首次正确率按题目去重：fx:1 首次作对、fx:2 首次作答错误 → 2 个样本 50%
    assert personal["first_attempt"] == {"samples": 2, "correct": 1, "accuracy": 50.0}
    # 重复练习：只有 fx:1 作答 ≥2 次
    assert personal["repeat_practice"] == {"questions": 1, "attempts": 1}
    # 待判分：最近一次作答仍 pending 的是 fx:3 与 app:42
    assert personal["pending_grading"] == {"questions": 2, "attempts": 2}
    # 与学情页同一份作答统计（窗口内）
    assert personal["attempts"] == 5
    assert personal["graded_attempts"] == 3
    assert personal["correct_attempts"] == 1
    # 我的题库真实计数
    assert personal["library"] == {"questions": 2, "pending_review": 1, "drafts": 1}
    # 继续上次练习：最近一次作答与最近一次已提交整卷
    assert personal["continue"]["last_attempt"]["qid"] == "app:42"
    assert personal["continue"]["last_attempt"]["result"] == "pending"
    assert personal["continue"]["last_paper"]["paper_id"] == 901
    assert payload["range"]["today"]
    assert "first_attempt" in personal["caliber"]


def test_personal_metrics_isolated_between_users(client, second_client, fixture_bank):
    user_id = _user_id(client)
    _insert_attempt(user_id, "fx:1", "correct", _iso())
    _insert_wrong(user_id, "fx:1", next_review_at=_iso(timedelta(days=1)))
    mine = client.get("/api/insight/home").json()["personal"]
    assert mine["attempts"] == 1 and mine["due_reviews"] == 1

    other = second_client.get("/api/insight/home").json()
    assert other["personal"]["attempts"] == 0
    assert other["personal"]["due_reviews"] == 0
    assert other["personal"]["first_attempt"] == {"samples": 0, "correct": 0, "accuracy": 0.0}
    assert other["personal"]["pending_grading"] == {"questions": 0, "attempts": 0}
    assert other["personal"]["library"]["questions"] == 0
    # 题库全局口径与账号无关
    assert other["bank"]["auto_gradable"] == EXPECTED_AUTO
    assert other["bank"]["visible_questions"] == EXPECTED_VISIBLE


# ---------------------------------------------------------------------------
# 薄弱知识点：真实样本数 + 样本不足标注
# ---------------------------------------------------------------------------


def test_weak_knowledge_reports_real_sample_counts(client, fixture_bank):
    user_id = _user_id(client)
    kp_limited, kp_enough = _kp_ids("数学", 2)
    # kp_limited：2 次已判定作答（不足 3 次）→ sample_limited
    _insert_question_kp("fx:1", kp_limited)
    _insert_question_kp("fx:2", kp_limited)
    _insert_attempt(user_id, "fx:1", "incorrect", _iso())
    _insert_attempt(user_id, "fx:2", "incorrect", _iso())
    # kp_enough：3 次已判定作答（样本充足）→ 不做推断，只展示真实样本
    for qid in ("fx:3", "fx:4", "fx:5"):
        _insert_question_kp(qid, kp_enough)
        _insert_attempt(user_id, qid, "incorrect", _iso())

    payload = client.get("/api/insight/home").json()
    weak = {item["kp_id"]: item for item in payload["personal"]["weak_knowledge"]}
    assert set(weak) == {kp_limited, kp_enough}
    assert weak[kp_limited]["graded_attempts"] == 2
    assert weak[kp_limited]["attempts"] == 2
    assert weak[kp_limited]["accuracy"] == 0.0
    assert weak[kp_limited]["sample_limited"] is True
    assert weak[kp_enough]["graded_attempts"] == 3
    assert weak[kp_enough]["sample_limited"] is False
    assert payload["personal"]["weak_note"] and "样本" in payload["personal"]["weak_note"]
    assert payload["personal"]["knowledge_coverage"]["linked_attempts"] == 5
    assert "sample_limited" in payload["caliber"]["weak"]


# ---------------------------------------------------------------------------
# 前端接线（静态断言：无需浏览器即可防回归）
# ---------------------------------------------------------------------------


def test_frontend_wiring_static():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    home_js = (root / "web/js/pages/home.js").read_text(encoding="utf-8")
    insight_js = (root / "web/js/pages/insight.js").read_text(encoding="utf-8")
    main_js = (root / "web/js/main.js").read_text(encoding="utf-8")
    state_js = (root / "web/js/core/state.js").read_text(encoding="utf-8")
    zh = json.loads((root / "web/locales/zh-CN.json").read_text(encoding="utf-8"))
    en = json.loads((root / "web/locales/en-US.json").read_text(encoding="utf-8"))

    # 首页从同一统计接口取数，且三个动作直达现有路由；不写死数字。
    assert "/insight/home" in home_js
    for route in ("go('upload')", "go('mybank')", "go('wrongbook')"):
        assert route in home_js
    assert "home.action.upload.title" in home_js
    assert "home.bank.practiceLabel" in home_js
    # 学情页补首次正确率/重复练习/待判分 + 样本不足提示，口径与后端同源。
    assert "/insight/home" in insight_js
    assert "insight.summary.first" in insight_js
    assert "insight.kp.sampleLimited" in insight_js
    # 样本不足（未达 MIN_WEAK_SAMPLE）的知识点单独成组，不混进「其他」里消失。
    assert "insight.kp.groupLimited" in insight_js
    # 导航按分组渲染，且没有注册尚未实现的广场/评审路由。
    assert "navGroups" in main_js and "navGroups" in state_js
    for group in ("learn", "content", "tools"):
        assert f"nav.group.{group}" in zh and f"nav.group.{group}" in en
    # Phase 2 起广场已接入：注册 plaza 渲染器并标记为公开（访客可读）路由；
    # 投稿/评审/编辑台由后续接线，不在本断言范围。
    assert "plaza" in state_js
    assert "PUBLIC_ROUTES" in state_js and "plaza" in state_js.split("PUBLIC_ROUTES", 1)[1][:120]
    # 中英文新增键对齐（缺键会让英文界面回退中文/显示键名）。
    for key in (
        "home.action.upload.title", "home.action.mybank.title", "home.action.review.title",
        "home.stats.dueLabel", "home.stats.todayLabel", "home.stats.firstAccuracyLabel",
        "home.weak.title", "home.bank.autoLabel", "home.next.last.label",
        "insight.summary.first", "insight.summary.repeat", "insight.summary.pending",
        "insight.kp.sampleLimited", "insight.kp.groupLimited", "nav.group.learn",
    ):
        assert key in zh, f"zh-CN 缺少 {key}"
        assert key in en, f"en-US 缺少 {key}"


# ---------------------------------------------------------------------------
# 鉴权
# ---------------------------------------------------------------------------


def test_home_stats_requires_login(client):
    client.cookies.clear()
    response = client.get("/api/insight/home")
    assert response.status_code == 401
