"""知识点掌握路径（``app/mastery_path.py``）隔离测试。

覆盖口径（与总方案 §3 数学主流程第 3 步、§5.2「学情」、§5.3「掌握路径」对齐）：

* 建表幂等与列结构；
* 显式 ``kp_ids`` 创建：每个知识点 2 个节点（practice → concept）、progress 0、
  ``requirements_json`` 文本化、``sample_status`` sufficient / insufficient 与
  ``sample_note``；
* 自动选薄弱知识点：无数据 422（中文口径）、有证据时选正确率 <70% 的最多 3 个、
  只有旧 ``attempts`` 时回退；
* ``replayed`` 幂等：相同知识点集合的进行中路径直接返回；
* 练习节点完成条件：无证据 pending、同一题重复正确只算 1 道、两道不同题独立核验
  答对 → done、带提示 / 揭晓后 / 自评 assisted / 非核验答对不计；
* 概念复述：<20 字与 >2000 字 422、写成学生自评证据、仅 concept 节点可提交；
* 两节点全 done → 路径 completed 且 ``version`` 增加、``completed_at`` 写入；
* refresh / restate 后 ``get_path`` 一致、最近事件、``sample_status`` 实时更新；
* 归档后 list 过滤、归档路径 409、跨用户 404、两账号证据隔离、路径上限 422、
  非法知识点 422、路由参数中文 422。

测试不依赖 ``app.main`` 与只读题库：本地 FastAPI 只挂 ``mastery_path.router``，用
``app.dependency_overrides[current_user]`` 注入当前用户；数据库用 conftest 的
``isolated_db`` + ``db.init_db()`` + ``practice.init_practice()`` +
``study_evidence.init_study_evidence()`` + ``init_mastery_path()``，
学习证据一律通过 ``study_evidence.record_event`` 公开函数写入（验证口径用公开函数）。
"""
import itertools

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import db, mastery_path, practice, study_evidence
from app.auth import current_user

USER_A = 9101
USER_B = 9102
BASE = "/api/mastery-paths"

_EVENT_SEQ = itertools.count(1)


# ---------------------------------------------------------------------------
# 夹具与造数工具
# ---------------------------------------------------------------------------


@pytest.fixture
def mp_env(isolated_db):
    """真实应用库结构 + 本模块三张表 + 两个账号。"""
    db.init_db()
    practice.init_practice()
    study_evidence.init_study_evidence()
    mastery_path.init_mastery_path()
    mastery_path.init_mastery_path()  # 幂等：重复初始化不报错
    with db.connection() as con:
        for user_id, username in ((USER_A, "路径甲"), (USER_B, "路径乙")):
            con.execute(
                "INSERT INTO users(id,username,password_hash,role,created_at) "
                "VALUES(?,?,?,?,?)",
                (user_id, username, "x", "student", db.utcnow()),
            )
    return isolated_db


@pytest.fixture
def api(mp_env):
    """本模块 router 的独立小应用；切换 holder["current"] 即可模拟两个账号。"""
    app = FastAPI()
    app.include_router(mastery_path.router)
    holder = {
        "current": {"id": USER_A, "username": "路径甲", "role": "student"},
    }
    app.dependency_overrides[current_user] = lambda: holder["current"]
    with TestClient(app) as client:
        yield client, holder


def _switch(holder, user_id=USER_B):
    holder["current"] = {"id": user_id, "username": "u%d" % user_id, "role": "student"}


def _kp(subject="数学", name="二次函数", level=3) -> int:
    """建一个知识点，返回 id（不带 id 插入，由数据库分配，双引擎一致）。"""
    with db.connection() as con:
        cursor = con.execute(
            "INSERT INTO knowledge_points(subject,name,level) VALUES(?,?,?)",
            (subject, name, level),
        )
        kp_id = cursor.lastrowid
    assert kp_id is not None
    return int(kp_id)


def _link(qid: str, kp_id: int) -> None:
    """题目 → 知识点关联（question_kp 由练习模块维护，本模块只读）。"""
    with db.connection() as con:
        con.execute(
            "INSERT INTO question_kp(qid,kp_id,weight,source,created_at) "
            "VALUES(?,?,1.0,'admin',?)",
            (qid, int(kp_id), db.utcnow()),
        )


def _evidence(
    user_id,
    ref,
    *,
    verdict="correct",
    prompt_state="none",
    self_report="none",
    reveal_state="unknown",
    grade_source="reference",
    occurred_at=None,
):
    """用学习证据公开函数写一条作答证据（默认独立且核验答对）。"""
    return study_evidence.record_event(
        user_id,
        kind="attempt",
        record_kind="attempts",
        verdict=verdict,
        question_ref=ref,
        grade_source=grade_source,
        prompt_state=prompt_state,
        reveal_state=reveal_state,
        self_report=self_report,
        event_key="test:mastery:%s:%d" % (user_id, next(_EVENT_SEQ)),
        occurred_at=occurred_at,
    )


def _attempt(user_id, qid, result) -> None:
    """旧作答（仅用于验证「无学习证据时的旧 attempts 回退口径」）。"""
    with db.connection() as con:
        con.execute(
            "INSERT INTO attempts(user_id,qid,answer_text,result,grade_source,created_at) "
            "VALUES(?,?,?,?,?,?)",
            (user_id, qid, "A", result, "reference", db.utcnow()),
        )


def _create(client, **body) -> dict:
    response = client.post(BASE, json=body)
    assert response.status_code in (200, 201), response.text
    return response.json()


def _node_of(payload: dict, kind: str, kp_id: int) -> dict:
    for node in payload["nodes"]:
        if node["kind"] == kind and node["kp_id"] == kp_id:
            return node
    raise AssertionError("未找到节点：kind=%s kp_id=%s" % (kind, kp_id))


def _scalar(sql: str, params=()):
    with db.connection() as con:
        return con.execute(sql, params).fetchone()[0]


def _insert_placeholder_paths(user_id, count: int, *, status="active", prefix="占位") -> None:
    """直接插入占位路径行（只用于路径数量上限；不涉及节点与证据口径）。"""
    now = db.utcnow()
    with db.connection() as con:
        for index in range(count):
            con.execute(
                "INSERT INTO mastery_paths(user_id, title, subject, source_kind, status, "
                "sample_status, nodes_total, nodes_done, version, created_at, updated_at, "
                "completed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,NULL)",
                (
                    user_id, "%s路径 %d" % (prefix, index), "", "manual", status,
                    "unknown", 0, 0, 1, now, now,
                ),
            )


# ---------------------------------------------------------------------------
# 建表与路由参数
# ---------------------------------------------------------------------------


def test_schema_tables_created_idempotent(mp_env):
    mastery_path.init_mastery_path()
    with db.connection() as con:
        assert {
            "mastery_paths", "mastery_path_nodes", "mastery_path_events",
        } <= db.table_names(con)
        path_columns = {row[1] for row in con.execute("PRAGMA table_info(mastery_paths)")}
        node_columns = {
            row[1] for row in con.execute("PRAGMA table_info(mastery_path_nodes)")
        }
        event_columns = {
            row[1] for row in con.execute("PRAGMA table_info(mastery_path_events)")
        }
    assert path_columns == {
        "id", "user_id", "title", "subject", "source_kind", "status", "sample_status",
        "nodes_total", "nodes_done", "version", "created_at", "updated_at", "completed_at",
    }
    assert node_columns == {
        "id", "path_id", "position", "kp_id", "kp_name", "title", "kind",
        "requirements_json", "status", "evidence_json", "completed_at", "created_at",
    }
    assert event_columns == {"id", "path_id", "node_id", "kind", "payload_json", "created_at"}


def test_route_rejects_non_integer_path_id(api):
    client, _holder = api
    response = client.get(BASE + "/abc")
    assert response.status_code == 422
    assert "整数" in response.json()["detail"]


# ---------------------------------------------------------------------------
# 创建：显式知识点
# ---------------------------------------------------------------------------


def test_create_with_explicit_kp_ids_builds_two_nodes_per_kp(api):
    client, _holder = api
    kp1 = _kp(name="二次函数")
    kp2 = _kp(name="三角函数")
    for qid in ("q1", "q2"):
        _link(qid, kp1)
    for qid in ("q3", "q4"):
        _link(qid, kp2)

    response = client.post(BASE, json={"kp_ids": [kp1, kp2]})
    assert response.status_code == 201, response.text
    payload = response.json()

    path = payload["path"]
    assert path["title"] == "掌握路径 · 数学"
    assert path["subject"] == "数学"
    assert path["status"] == "active"
    assert path["source_kind"] == "manual"
    assert path["sample_status"] == "sufficient"
    assert path["progress"] == {"nodes_total": 4, "nodes_done": 0, "percent": 0}
    assert path["version"] == 1
    assert path["completed_at"] is None

    assert payload["progress"] == path["progress"]
    assert payload["sample_note"] is None
    assert payload["replayed"] is False

    nodes = payload["nodes"]
    assert len(nodes) == 4
    assert [node["position"] for node in nodes] == [0, 1, 2, 3]
    assert [(node["kp_id"], node["kind"]) for node in nodes] == [
        (kp1, "practice"), (kp1, "concept"), (kp2, "practice"), (kp2, "concept"),
    ]
    practice_node = _node_of(payload, "practice", kp1)
    concept_node = _node_of(payload, "concept", kp1)
    assert practice_node["requirements"] == {"kind": "two_independent_correct", "count": 2}
    assert concept_node["requirements"] == {"kind": "restate", "min_chars": 20}
    assert practice_node["title"] == "独立答对 2 道不同题（二次函数）"
    assert concept_node["title"] == "用自己的话复述该知识点并说明一道例题"
    for node in nodes:
        assert node["status"] == "pending"
        assert node["evidence"] == {}
        assert node["sample_status"] == "sufficient"
        assert node["completed_at"] is None


def test_create_explicit_title_subject_and_source_kind(api):
    client, _holder = api
    kp = _kp(subject="物理", name="牛顿第二定律")
    _link("p1", kp)
    _link("p2", kp)
    payload = _create(
        client,
        kp_ids=[kp],
        title="我的物理路径",
        subject="物理",
        source_kind="wrongbook",
    )
    assert payload["path"]["title"] == "我的物理路径"
    assert payload["path"]["subject"] == "物理"
    assert payload["path"]["source_kind"] == "wrongbook"
    # 未给标题时默认「掌握路径 · <学科>」。
    other = _kp(subject="化学", name="化学平衡")
    _link("c1", other)
    _link("c2", other)
    assert _create(client, kp_ids=[other])["path"]["title"] == "掌握路径 · 化学"


def test_create_sample_insufficient_and_note(api):
    client, _holder = api
    kp = _kp(name="导数")
    _link("d1", kp)  # 只有 1 道关联题 → 样本不足
    payload = _create(client, kp_ids=[kp])
    assert payload["path"]["sample_status"] == "insufficient"
    assert payload["sample_note"] == mastery_path.SAMPLE_NOTE
    assert "样本不足" in payload["sample_note"] or "无法自动核验" in payload["sample_note"]
    assert _node_of(payload, "practice", kp)["sample_status"] == "insufficient"
    # 路径详情同样显示样本不足，不猜测。
    detail = client.get(BASE + "/%d" % payload["path"]["path_id"]).json()
    assert detail["sample_note"] == mastery_path.SAMPLE_NOTE


def test_create_validation_errors_are_chinese_422(api):
    client, _holder = api
    response = client.post(BASE, json={"kp_ids": [999999]})
    assert response.status_code == 422
    assert "知识点不存在" in response.json()["detail"]

    response = client.post(BASE, json={"kp_ids": list(range(1, 10))})
    assert response.status_code == 422
    assert "最多指定 8 个知识点" in response.json()["detail"]

    kp = _kp()
    response = client.post(BASE, json={"kp_ids": [kp], "source_kind": "bogus"})
    assert response.status_code == 422
    assert "source_kind" in response.json()["detail"]


def test_create_replay_same_kp_set_is_idempotent(api):
    client, _holder = api
    kp1 = _kp(name="集合")
    kp2 = _kp(name="逻辑")
    for qid in ("r1", "r2", "r3", "r4"):
        _link(qid, kp1 if qid in ("r1", "r2") else kp2)

    first = _create(client, kp_ids=[kp1, kp2])
    assert first["replayed"] is False
    path_id = first["path"]["path_id"]

    response = client.post(BASE, json={"kp_ids": [kp2, kp1]})
    assert response.status_code == 200, response.text
    replay = response.json()
    assert replay["replayed"] is True
    assert replay["path"]["path_id"] == path_id
    assert _scalar("SELECT COUNT(*) FROM mastery_paths") == 1
    assert _scalar(
        "SELECT COUNT(*) FROM mastery_path_nodes WHERE path_id=?", (path_id,)
    ) == 4

    listed = client.get(BASE).json()
    assert listed["total"] == 1


def test_create_accepts_caller_connection(mp_env):
    kp = _kp(name="向量")
    _link("v1", kp)
    _link("v2", kp)
    with db.connection() as con:
        created = mastery_path.create_path(USER_A, kp_ids=[kp], con=con)
        path_id = created["path"]["path_id"]
        assert mastery_path.get_path(USER_A, path_id, con=con)["path"]["path_id"] == path_id
    assert mastery_path.get_path(USER_A, path_id)["path"]["path_id"] == path_id


# ---------------------------------------------------------------------------
# 自动选薄弱知识点
# ---------------------------------------------------------------------------


def test_auto_select_without_data_returns_422(api):
    client, _holder = api
    response = client.post(BASE, json={})
    assert response.status_code == 422
    assert response.json()["detail"] == mastery_path.AUTO_EMPTY_DETAIL
    assert "样本不足" in response.json()["detail"]
    assert "请显式指定知识点" in response.json()["detail"]
    # 没有任何 question_kp 关联时同样 422，不猜测。
    kp = _kp()
    response = client.post(BASE, json={"subject": "数学"})
    assert response.status_code == 422
    assert kp  # 知识点存在但没有关联题目与作答


def test_auto_select_picks_at_most_three_weakest(api):
    client, _holder = api
    # A 0%/1 次、B 25%/4 次、C 50%/2 次、D 66.7%/3 次、E 100%/2 次。
    # 样本门槛 MIN_WEAK_SAMPLE=3：A（1 次）、C（2 次）样本不足被排除，
    # 因此只选 B（25%，4 次）与 D（66.7%，3 次）两个最弱。
    kp_a = _kp(name="薄弱A")
    kp_b = _kp(name="薄弱B")
    kp_c = _kp(name="薄弱C")
    kp_d = _kp(name="临界D")
    kp_e = _kp(name="优势E")
    _link("qa1", kp_a)
    for index in range(1, 5):
        _link("qb%d" % index, kp_b)
    for index in range(1, 3):
        _link("qc%d" % index, kp_c)
    for index in range(1, 4):
        _link("qd%d" % index, kp_d)
    for index in range(1, 3):
        _link("qe%d" % index, kp_e)

    _evidence(USER_A, "bank:qa1", verdict="incorrect")
    for qid, verdict in (("qb1", "correct"), ("qb2", "incorrect"),
                         ("qb3", "incorrect"), ("qb4", "incorrect")):
        _evidence(USER_A, "bank:" + qid, verdict=verdict)
    _evidence(USER_A, "bank:qc1", verdict="correct")
    _evidence(USER_A, "bank:qc2", verdict="incorrect")
    for qid in ("qd1", "qd2", "qd3"):
        _evidence(USER_A, "bank:" + qid,
                  verdict="correct" if qid != "qd3" else "incorrect")
    _evidence(USER_A, "bank:qe1", verdict="correct")
    _evidence(USER_A, "bank:qe2", verdict="correct")

    payload = _create(client)
    kp_ids = [node["kp_id"] for node in payload["nodes"]]
    # 样本不足的知识点不自动入选：只剩 B、D（各 2 个节点）。
    assert kp_ids == [kp_b, kp_b, kp_d, kp_d]
    assert payload["path"]["progress"]["nodes_total"] == 4


def test_auto_select_falls_back_to_legacy_attempts(api):
    client, _holder = api
    kp = _kp(name="旧数据知识点")
    for index in range(1, 5):
        _link("lw%d" % index, kp)
    # 没有学习证据，只有旧 attempts：1 对 3 错 → 25%，自动生成应能选中。
    _attempt(USER_A, "lw1", "incorrect")
    _attempt(USER_A, "lw2", "incorrect")
    _attempt(USER_A, "lw3", "incorrect")
    _attempt(USER_A, "lw4", "correct")

    payload = _create(client)
    assert [node["kp_id"] for node in payload["nodes"]] == [kp, kp]


def test_path_limit_counts_active_only(api):
    client, holder = api
    kp1 = _kp(name="上限一")
    kp2 = _kp(name="上限二")
    _link("m1", kp1)
    _link("m2", kp1)
    _link("m3", kp2)
    _link("m4", kp2)
    # 49 条 active + 1 条已归档 = 50 行，但归档不占额度 → 仍可创建。
    _insert_placeholder_paths(USER_A, 49)
    _insert_placeholder_paths(USER_A, 1, status="archived")
    _create(client, kp_ids=[kp1])
    assert _scalar(
        "SELECT COUNT(*) FROM mastery_paths WHERE user_id=? AND status<>'archived'",
        (USER_A,),
    ) == 50
    # 已到 50 条上限 → 422（中文 detail）。
    response = client.post(BASE, json={"kp_ids": [kp2]})
    assert response.status_code == 422
    assert "上限" in response.json()["detail"]
    # 另一个账号不受影响。
    _switch(holder, USER_B)
    assert _create(client, kp_ids=[kp2])["path"]["status"] == "active"


# ---------------------------------------------------------------------------
# 列表 / 详情
# ---------------------------------------------------------------------------


def test_list_paths_and_status_filter(api):
    client, _holder = api
    kp1 = _kp(name="列表一")
    kp2 = _kp(name="列表二")
    for qid in ("n1", "n2"):
        _link(qid, kp1)
    for qid in ("n3", "n4"):
        _link(qid, kp2)
    first = _create(client, kp_ids=[kp1])
    second = _create(client, kp_ids=[kp2])

    listed = client.get(BASE).json()
    assert listed["total"] == 2
    assert len(listed["items"]) == 2
    item = listed["items"][0]
    assert {
        "path_id", "title", "subject", "status", "progress", "sample_status",
        "created_at", "updated_at",
    } <= set(item)
    assert item["progress"] == {"nodes_total": 2, "nodes_done": 0, "percent": 0}
    assert {entry["path_id"] for entry in listed["items"]} == {
        first["path"]["path_id"], second["path"]["path_id"],
    }

    limited = client.get(BASE, params={"limit": 1}).json()
    assert limited["total"] == 2
    assert len(limited["items"]) == 1

    active = client.get(BASE, params={"status": "active"}).json()
    assert active["total"] == 2
    archived = client.get(BASE, params={"status": "archived"}).json()
    assert archived["total"] == 0
    response = client.get(BASE, params={"status": "bogus"})
    assert response.status_code == 422
    assert "路径状态" in response.json()["detail"]


def test_get_path_unknown_and_cross_user_404(api):
    client, holder = api
    kp = _kp(name="隔离")
    for qid in ("s1", "s2"):
        _link(qid, kp)
    created = _create(client, kp_ids=[kp])
    path_id = created["path"]["path_id"]

    response = client.get(BASE + "/999999")
    assert response.status_code == 404
    assert "不存在" in response.json()["detail"]

    _switch(holder, USER_B)
    response = client.get(BASE + "/%d" % path_id)
    assert response.status_code == 404
    assert "不存在" in response.json()["detail"]
    assert client.get(BASE).json()["total"] == 0


# ---------------------------------------------------------------------------
# 练习节点：完成条件来自学习证据
# ---------------------------------------------------------------------------


def test_refresh_without_evidence_stays_pending(api):
    client, _holder = api
    kp = _kp(name="无证据")
    for qid in ("e1", "e2"):
        _link(qid, kp)
    created = _create(client, kp_ids=[kp])
    path_id = created["path"]["path_id"]
    practice_node = _node_of(created, "practice", kp)

    response = client.post(
        BASE + "/%d/nodes/%d/refresh" % (path_id, practice_node["node_id"])
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert set(payload) >= {"node", "path", "progress"}
    node = payload["node"]
    assert node["status"] == "pending"
    assert node["completed_at"] is None
    assert node["evidence"]["kind"] == "two_independent_correct"
    assert node["evidence"]["matched_questions"] == []
    assert node["evidence"]["distinct_questions"] == 0
    assert node["evidence"]["missing"] == 2
    assert node["evidence"]["note"] == "提示/揭晓后的作答不计"
    assert node["evidence"]["checked_at"]
    assert payload["path"]["status"] == "active"
    assert payload["path"]["progress"] == {
        "nodes_total": 2, "nodes_done": 0, "percent": 0,
    }


def test_refresh_same_question_twice_counts_once(api):
    client, _holder = api
    kp = _kp(name="同题重复")
    for qid in ("t1", "t2"):
        _link(qid, kp)
    created = _create(client, kp_ids=[kp])
    path_id = created["path"]["path_id"]
    practice_node = _node_of(created, "practice", kp)
    _evidence(USER_A, "bank:t1", occurred_at="2026-01-01T00:00:01+00:00")
    _evidence(USER_A, "bank:t1", occurred_at="2026-01-01T00:00:02+00:00")

    response = client.post(
        BASE + "/%d/nodes/%d/refresh" % (path_id, practice_node["node_id"])
    )
    assert response.status_code == 200
    node = response.json()["node"]
    assert node["status"] == "pending"
    assert node["evidence"]["matched_questions"] == ["t1"]
    assert node["evidence"]["missing"] == 1


def test_refresh_two_different_questions_marks_done(api):
    client, _holder = api
    kp = _kp(name="独立答对")
    for qid in ("u1", "u2", "u3"):
        _link(qid, kp)
    created = _create(client, kp_ids=[kp])
    path_id = created["path"]["path_id"]
    practice_node = _node_of(created, "practice", kp)
    _evidence(USER_A, "bank:u1", occurred_at="2026-01-01T00:00:01+00:00")
    _evidence(USER_A, "bank:u2", occurred_at="2026-01-01T00:00:02+00:00")

    response = client.post(
        BASE + "/%d/nodes/%d/refresh" % (path_id, practice_node["node_id"])
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    node = payload["node"]
    assert node["status"] == "done"
    assert node["completed_at"]
    assert set(node["evidence"]["matched_questions"]) == {"u1", "u2"}
    assert node["evidence"]["distinct_questions"] == 2
    assert node["evidence"]["missing"] == 0
    assert payload["path"]["status"] == "active"
    assert payload["path"]["progress"] == {
        "nodes_total": 2, "nodes_done": 1, "percent": 50,
    }
    detail = client.get(BASE + "/%d" % path_id).json()
    assert detail["nodes"][0]["status"] == "done"
    assert set(detail["nodes"][0]["evidence"]["matched_questions"]) == {"u1", "u2"}
    assert [event["kind"] for event in detail["recent_events"]][0] == "practice_refreshed"


def test_refresh_ignores_hint_assisted_reveal_and_unverified(api):
    client, _holder = api
    kp = _kp(name="不可计入")
    for qid in ("w1", "w2", "w3", "w4", "w5", "w6", "w7"):
        _link(qid, kp)
    created = _create(client, kp_ids=[kp])
    path_id = created["path"]["path_id"]
    practice_node = _node_of(created, "practice", kp)
    # 带提示、自评 assisted、揭晓后、判错、非核验来源都不计入。
    _evidence(USER_A, "bank:w1", prompt_state="hint")
    _evidence(USER_A, "bank:w2", self_report="assisted", prompt_state="none")
    _evidence(USER_A, "bank:w3", reveal_state="after", prompt_state="none")
    _evidence(USER_A, "bank:w4", verdict="incorrect")
    _evidence(USER_A, "bank:w5", grade_source="self")
    # 学生自评 independent 的核验答对计入（独立标记来自学生自评，但仍需核验判分）。
    _evidence(USER_A, "bank:w6", self_report="independent", prompt_state="unknown")

    response = client.post(
        BASE + "/%d/nodes/%d/refresh" % (path_id, practice_node["node_id"])
    )
    node = response.json()["node"]
    assert node["status"] == "pending"
    assert node["evidence"]["matched_questions"] == ["w6"]
    assert node["evidence"]["missing"] == 1

    # 再补一道真正的独立核验答对 → 达到 2 道不同题。
    _evidence(USER_A, "bank:w7", prompt_state="none", self_report="independent")
    response = client.post(
        BASE + "/%d/nodes/%d/refresh" % (path_id, practice_node["node_id"])
    )
    node = response.json()["node"]
    assert node["status"] == "done"
    assert set(node["evidence"]["matched_questions"]) == {"w6", "w7"}


def test_refresh_requires_practice_node(api):
    client, _holder = api
    kp = _kp(name="类型校验")
    for qid in ("x1", "x2"):
        _link(qid, kp)
    created = _create(client, kp_ids=[kp])
    concept_node = _node_of(created, "concept", kp)
    response = client.post(
        BASE + "/%d/nodes/%d/refresh" % (created["path"]["path_id"], concept_node["node_id"])
    )
    assert response.status_code == 422
    assert "练习节点" in response.json()["detail"]


# ---------------------------------------------------------------------------
# 概念复述节点
# ---------------------------------------------------------------------------


def test_restate_validation_and_done(api):
    client, _holder = api
    kp = _kp(name="复述")
    for qid in ("y1", "y2"):
        _link(qid, kp)
    created = _create(client, kp_ids=[kp])
    path_id = created["path"]["path_id"]
    concept_node = _node_of(created, "concept", kp)
    practice_node = _node_of(created, "practice", kp)
    url = BASE + "/%d/nodes/%d/restate" % (path_id, concept_node["node_id"])

    response = client.post(url, json={"text": "短" * 19})
    assert response.status_code == 422
    assert "至少 20 字" in response.json()["detail"]

    response = client.post(url, json={"text": "长" * 2001})
    assert response.status_code == 422
    assert "最多 2000 字" in response.json()["detail"]

    response = client.post(
        BASE + "/%d/nodes/%d/restate" % (path_id, practice_node["node_id"]),
        json={"text": "用自己的话复述二次函数的顶点式与图像平移规律，并说明一道例题。"},
    )
    assert response.status_code == 422
    assert "概念复述节点" in response.json()["detail"]

    text = "二次函数的顶点式是 y=a(x-h)^2+k，图像由 y=ax^2 平移得到，顶点为 (h,k)。"
    response = client.post(url, json={"text": text})
    assert response.status_code == 200, response.text
    payload = response.json()
    node = payload["node"]
    assert node["status"] == "done"
    assert node["completed_at"]
    assert node["evidence"]["kind"] == "restate"
    assert node["evidence"]["self_report"] == "independent"
    assert node["evidence"]["text"] == text
    assert node["evidence"]["chars"] == len(text)
    assert node["evidence"]["submitted_at"]
    assert payload["path"]["progress"] == {
        "nodes_total": 2, "nodes_done": 1, "percent": 50,
    }
    # 复述是学生自评，不产生练习节点的独立答对匹配。
    practice_after = client.post(
        BASE + "/%d/nodes/%d/refresh" % (path_id, practice_node["node_id"])
    ).json()["node"]
    assert practice_after["status"] == "pending"
    assert practice_after["evidence"]["matched_questions"] == []


# ---------------------------------------------------------------------------
# 全部完成 → 路径 completed
# ---------------------------------------------------------------------------


def test_both_nodes_done_completes_path_and_bumps_version(api):
    client, _holder = api
    kp = _kp(name="完成路径")
    for qid in ("z1", "z2"):
        _link(qid, kp)
    created = _create(client, kp_ids=[kp])
    path_id = created["path"]["path_id"]
    practice_node = _node_of(created, "practice", kp)
    concept_node = _node_of(created, "concept", kp)
    assert created["path"]["version"] == 1

    _evidence(USER_A, "bank:z1")
    _evidence(USER_A, "bank:z2")
    refreshed = client.post(
        BASE + "/%d/nodes/%d/refresh" % (path_id, practice_node["node_id"])
    ).json()
    assert refreshed["path"]["version"] == 2
    assert refreshed["path"]["status"] == "active"

    restated = client.post(
        BASE + "/%d/nodes/%d/restate" % (path_id, concept_node["node_id"]),
        json={"text": "复述：二次函数顶点式与平移规律，并说明一道例题的求解过程。"},
    ).json()
    assert restated["path"]["version"] == 3
    assert restated["path"]["status"] == "completed"
    assert restated["path"]["completed_at"]
    assert restated["progress"] == {"nodes_total": 2, "nodes_done": 2, "percent": 100}

    detail = client.get(BASE + "/%d" % path_id).json()
    assert detail["path"]["status"] == "completed"
    assert detail["path"]["completed_at"] == restated["path"]["completed_at"]
    assert [node["status"] for node in detail["nodes"]] == ["done", "done"]
    assert "path_completed" in [event["kind"] for event in detail["recent_events"]]
    # 完成后的路径出现在 completed 过滤中，不在 active 过滤中。
    assert client.get(BASE, params={"status": "completed"}).json()["total"] == 1
    assert client.get(BASE, params={"status": "active"}).json()["total"] == 0


def test_refresh_reverts_when_evidence_revised(api):
    """证据被修订为判错后，刷新应把完成状态退回 pending，不保留已失效的完成结论。"""
    client, _holder = api
    kp = _kp(name="证据重判")
    for qid in ("rv1", "rv2"):
        _link(qid, kp)
    created = _create(client, kp_ids=[kp])
    path_id = created["path"]["path_id"]
    practice_node = _node_of(created, "practice", kp)
    concept_node = _node_of(created, "concept", kp)
    first = _evidence(USER_A, "bank:rv1", occurred_at="2026-01-01T00:00:01+00:00")
    _evidence(USER_A, "bank:rv2", occurred_at="2026-01-01T00:00:02+00:00")

    refreshed = client.post(
        BASE + "/%d/nodes/%d/refresh" % (path_id, practice_node["node_id"])
    ).json()
    assert refreshed["node"]["status"] == "done"
    restated = client.post(
        BASE + "/%d/nodes/%d/restate" % (path_id, concept_node["node_id"]),
        json={"text": "先写下复述让路径完成，随后用一次证据修订验证完成状态会回退。"},
    ).json()
    assert restated["path"]["status"] == "completed"

    # 修订该题证据为判错：有效版本只剩 1 道独立答对 → 练习节点退回 pending。
    study_evidence.revise_event(USER_A, first["event_key"], verdict="incorrect")
    reverted = client.post(
        BASE + "/%d/nodes/%d/refresh" % (path_id, practice_node["node_id"])
    ).json()
    assert reverted["node"]["status"] == "pending"
    assert reverted["node"]["completed_at"] is None
    assert reverted["node"]["evidence"]["matched_questions"] == ["rv2"]
    assert reverted["path"]["status"] == "active"
    assert reverted["path"]["completed_at"] is None
    detail = client.get(BASE + "/%d" % path_id).json()
    assert "path_reopened" in [event["kind"] for event in detail["recent_events"]]


def test_recent_events_capped_at_ten(api):
    client, _holder = api
    kp = _kp(name="事件上限")
    for qid in ("ev1", "ev2"):
        _link(qid, kp)
    created = _create(client, kp_ids=[kp])
    path_id = created["path"]["path_id"]
    practice_node = _node_of(created, "practice", kp)
    for _ in range(12):
        response = client.post(
            BASE + "/%d/nodes/%d/refresh" % (path_id, practice_node["node_id"])
        )
        assert response.status_code == 200
    detail = client.get(BASE + "/%d" % path_id).json()
    assert len(detail["recent_events"]) == 10
    assert _scalar(
        "SELECT COUNT(*) FROM mastery_path_events WHERE path_id=?", (path_id,)
    ) == 13


# ---------------------------------------------------------------------------
# 样本状态实时计算 / 归档 / 隔离
# ---------------------------------------------------------------------------


def test_sample_status_updates_when_questions_linked_later(api):
    client, _holder = api
    kp = _kp(name="补打标")
    _link("k1", kp)
    created = _create(client, kp_ids=[kp])
    assert created["path"]["sample_status"] == "insufficient"
    _link("k2", kp)  # 之后补充题目关联
    detail = client.get(BASE + "/%d" % created["path"]["path_id"]).json()
    assert detail["path"]["sample_status"] == "sufficient"
    assert detail["sample_note"] is None
    assert all(node["sample_status"] == "sufficient" for node in detail["nodes"])


def test_archive_filters_list_and_blocks_nodes(api):
    client, _holder = api
    kp = _kp(name="归档")
    for qid in ("a1", "a2"):
        _link(qid, kp)
    created = _create(client, kp_ids=[kp])
    path_id = created["path"]["path_id"]
    practice_node = _node_of(created, "practice", kp)
    concept_node = _node_of(created, "concept", kp)

    response = client.post(BASE + "/%d/archive" % path_id)
    assert response.status_code == 200
    archived = response.json()["path"]
    assert archived["status"] == "archived"
    assert archived["version"] == 2

    assert client.get(BASE, params={"status": "active"}).json()["total"] == 0
    assert client.get(BASE, params={"status": "archived"}).json()["total"] == 1
    assert client.get(BASE).json()["total"] == 1

    # 幂等归档：不重复写事件、版本不变。
    again = client.post(BASE + "/%d/archive" % path_id).json()["path"]
    assert again["status"] == "archived"
    assert again["version"] == 2
    assert _scalar(
        "SELECT COUNT(*) FROM mastery_path_events WHERE path_id=? AND kind='archived'",
        (path_id,),
    ) == 1

    # 归档后不能继续提交节点（409，中文）。
    response = client.post(
        BASE + "/%d/nodes/%d/refresh" % (path_id, practice_node["node_id"])
    )
    assert response.status_code == 409
    assert "已归档" in response.json()["detail"]
    response = client.post(
        BASE + "/%d/nodes/%d/restate" % (path_id, concept_node["node_id"]),
        json={"text": "即使写了足够长的复述内容，归档路径也不应接受新的提交。"},
    )
    assert response.status_code == 409
    assert "已归档" in response.json()["detail"]


def test_node_must_belong_to_path(api):
    client, _holder = api
    kp1 = _kp(name="路径一")
    kp2 = _kp(name="路径二")
    for qid in ("b1", "b2", "b3", "b4"):
        _link(qid, kp1 if qid in ("b1", "b2") else kp2)
    first = _create(client, kp_ids=[kp1])
    second = _create(client, kp_ids=[kp2])
    node_id = _node_of(first, "practice", kp1)["node_id"]
    response = client.post(
        BASE + "/%d/nodes/%d/refresh" % (second["path"]["path_id"], node_id)
    )
    assert response.status_code == 404
    response = client.post(
        BASE + "/%d/nodes/%d/restate" % (second["path"]["path_id"], node_id),
        json={"text": "这条复述属于另一条路径的节点，应当返回 404 而不是写入。"},
    )
    assert response.status_code == 404


def test_two_accounts_evidence_and_paths_are_isolated(api):
    client, holder = api
    kp = _kp(name="双账号")
    for qid in ("g1", "g2"):
        _link(qid, kp)

    created_a = _create(client, kp_ids=[kp])
    path_a = created_a["path"]["path_id"]
    practice_a = _node_of(created_a, "practice", kp)

    _evidence(USER_A, "bank:g1")
    _evidence(USER_A, "bank:g2")
    refreshed = client.post(
        BASE + "/%d/nodes/%d/refresh" % (path_a, practice_a["node_id"])
    ).json()
    assert refreshed["node"]["status"] == "done"

    _switch(holder, USER_B)
    created_b = _create(client, kp_ids=[kp])
    path_b = created_b["path"]["path_id"]
    assert path_b != path_a
    practice_b = _node_of(created_b, "practice", kp)
    refreshed_b = client.post(
        BASE + "/%d/nodes/%d/refresh" % (path_b, practice_b["node_id"])
    ).json()
    assert refreshed_b["node"]["status"] == "pending"
    assert refreshed_b["node"]["evidence"]["matched_questions"] == []
    assert client.get(BASE).json()["total"] == 1
    assert client.get(BASE + "/%d" % path_a).status_code == 404
