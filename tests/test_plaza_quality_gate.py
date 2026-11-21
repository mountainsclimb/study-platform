"""广场质量运营闭环（Phase 4 · WS28）回归。

覆盖：

* 抽检门禁：模式 ``off`` / ``initial_100`` / ``always``；DB 设置 > 环境变量 > 默认
  的解析优先级；``armed`` 激活语义（显式配置或已有抽检记录后强制）；试点窗口计数；
  409 ``quality_gate_blocked`` + guidance；编辑 override（必须带理由、写事件+审计+指标）；
* 签发前抽检（pre_publish）：``verified`` / ``pending_release`` 可先在签发前抽检，
  pass 后签发成功；``community_review`` 仍 409（既有语义不回归）；
* 一键恢复：抽检 ``high`` 自动撤回 / 编辑撤回后恢复发布指针、公开可见性、作者与
  收藏者通知、``release_restored`` 事件；``confirm_resolved`` 条件；回滚撤回不可恢复；
  同一投稿已有更新版本活跃时 409；权限与作者回避；重复恢复幂等；
* 批量处理：纠错/举报/申诉批量 resolve/reject，部分成功 updated/skipped、批次审计行、
  指标批量量；参数校验（空/超 50/驳回无理由/非法动作）与权限；
* 通知增强：``GET /notifications/unread-count`` 轻量未读汇总、``POST /notifications/read``
  按 ``kind`` 全读、他人通知忽略、未知类型 422；同「接收人+类型+投稿+版本」窗口去重；
* 指标补充：``quality.gate``（模式/激活/窗口/阻断/override/通过）、
  ``withdrawals.restorations``、``restorations``、``ops.batch``、``notifications``。
"""
import json

import pytest
from fastapi.testclient import TestClient

API = "/api/plaza"
PASSWORD = "plaza-pass-123"
GATE_ENV = "STUDY_PLAZA_QUALITY_GATE"
GATE_ARMED_ENV = "STUDY_PLAZA_QUALITY_GATE_ARMED"
DEDUPE_ENV = "STUDY_PLAZA_NOTIFY_DEDUPE_SECONDS"


# ---------------------------------------------------------------------------
# 基础助手
# ---------------------------------------------------------------------------

def _client():
    from app.main import app
    return TestClient(app)


def _register(name, password=PASSWORD):
    client = _client()
    response = client.post("/api/auth/register", json={"username": name, "password": password})
    assert response.status_code == 200, response.text
    return client, response.json()


def _source(**overrides):
    payload = {
        "url": "https://example.com/exam/2025/math/12",
        "page_no": "12",
        "question_no": "3",
        "original_author": "命题组",
        "license_basis": "原创并授权知序公开展示",
        "rights_scope": "允许公开题面、答案与解析；禁止商业再分发",
        "attribution": "署名：知序投稿作者",
    }
    payload.update(overrides)
    return payload


def _db_rows(sql, params=()):
    from app import db
    with db.connection() as con:
        return [dict(row) for row in con.execute(sql, params).fetchall()]


class World:
    """隔离测试世界：管理员客户端 + 按需创建的用户与角色。"""

    def __init__(self, admin):
        self.admin = admin
        self.clients = []
        self._names = set()
        self._seq = 0

    def close(self):
        for client in self.clients:
            client.close()

    def user(self, name, roles=()):
        self._seq += 1
        unique = name
        while unique in self._names:
            unique = f"{name}-{self._seq}"
        self._names.add(unique)
        client, payload = _register(unique)
        self.clients.append(client)
        for role, subject in roles:
            granted = self.admin.post(f"{API}/admin/roles", json={
                "action": "grant", "role": role, "subject": subject, "username": unique,
            })
            assert granted.status_code == 200, granted.text
        return {"client": client, "id": payload["id"], "name": unique}

    def question(self, author, *, subject="数学", qtype="SINGLE",
                 stem="已知 1+1=？", answers=("A",)):
        content = {
            "stem": stem,
            "options": [{"label": "A", "text": "2"}, {"label": "B", "text": "3"}],
            "answers": list(answers),
            "explanation": "基础加法。",
        }
        response = author["client"].post("/api/paperlab/questions", json={
            "type": qtype, "subject": subject, "content": content,
        })
        assert response.status_code == 200, response.text
        return response.json()["question"]["id"]


@pytest.fixture
def world(client):
    value = World(client)
    yield value
    value.close()


# ---------------------------------------------------------------------------
# 流程助手（与既有广场用例同构）
# ---------------------------------------------------------------------------

def _create_submission(author, question_id):
    response = author["client"].post(f"{API}/submissions", json={
        "question_id": question_id, "source": _source(), "license_confirmed": True,
    })
    assert response.status_code == 201, response.text
    return response.json()["submission"]["id"]


def _submit(author, submission_id, version=1):
    return author["client"].post(f"{API}/submissions/{submission_id}/submit", json={
        "version": version, "confirm_preview": True,
    })


def _initial_review(editor, submission_id, *, version=1, decision="accept", reason=""):
    return editor["client"].post(f"{API}/submissions/{submission_id}/initial-review", json={
        "version": version,
        "decision": decision,
        "reason": reason,
        "source_checked": True,
        "privacy_checked": True,
        "completeness_checked": True,
    })


def _review(reviewer, submission_id, *, version=1, verdict="approve"):
    return reviewer["client"].post(f"{API}/submissions/{submission_id}/reviews", json={
        "version": version,
        "verdict": verdict,
        "reasons": "",
        "independent_answer": "我独立作答得到 A。",
        "conflict_declared": False,
    })


def _publish(editor, submission_id, *, version=1, reason="内容核验通过，签发精选",
             override=False, override_reason=""):
    payload = {"version": version, "reason": reason}
    if override:
        payload["quality_gate_override"] = True
        payload["override_reason"] = override_reason
    return editor["client"].post(
        f"{API}/submissions/{submission_id}/publish", json=payload,
    )


def _withdraw_release(editor, public_id, *, reason="收到投诉，撤回复核", mode="normal"):
    return editor["client"].post(f"{API}/releases/{public_id}/withdraw", json={
        "reason": reason, "mode": mode,
    })


def _restore(editor, public_id, *, reason="问题已处理并复核通过", confirm=True):
    payload = {"reason": reason}
    if confirm:
        payload["confirm_resolved"] = True
    return editor["client"].post(f"{API}/releases/{public_id}/restore", json=payload)


def _quality_check(editor, submission_id, *, decision="pass", severity=None,
                   findings=None, note="", version=None):
    payload = {"submission_id": submission_id, "decision": decision, "note": note}
    if severity:
        payload["severity"] = severity
    if findings is not None:
        payload["findings"] = findings
    if version is not None:
        payload["version"] = version
    return editor["client"].post(f"{API}/admin/quality/checks", json=payload)


def _gate_state(user):
    response = user["client"].get(f"{API}/admin/quality/gate")
    assert response.status_code == 200, response.text
    return response.json()


def _set_gate(admin, **payload):
    response = admin.put(f"{API}/admin/quality/gate", json=payload)
    assert response.status_code == 200, response.text
    return response.json()


def _detail(user, submission_id):
    response = user["client"].get(f"{API}/submissions/{submission_id}")
    assert response.status_code == 200, response.text
    return response.json()["submission"]


def _notifications(user, **params):
    response = user["client"].get(f"{API}/notifications", params=params)
    assert response.status_code == 200, response.text
    return response.json()


def _notification_kinds(user, **params):
    return [
        item["kind"]
        for item in _notifications(user, page_size=100, **params)["items"]
    ]


def _notification_count(user, kind):
    return _notifications(user, kind=kind, page_size=100)["total"]


def _events(user, submission_id):
    response = user["client"].get(f"{API}/submissions/{submission_id}/events")
    assert response.status_code == 200, response.text
    return response.json()["items"]


def _event_actions(user, submission_id):
    return [item["action"] for item in _events(user, submission_id)]


def _anon():
    return _client()


def _voters(world, subject="数学", seed="门禁评审"):
    voters = [
        world.user(f"{seed}复核{subject}{i}", roles=[("subject_reviewer", subject)])
        for i in range(2)
    ]
    voters += [
        world.user(f"{seed}评审{subject}{i}", roles=[("reviewer", subject)])
        for i in range(3)
    ]
    return voters


def _community(world, *, subject="数学", author=None, editor=None, seed="门禁社区"):
    author = author or world.user(f"{seed}作者-{subject}")
    if editor is None:
        editor = world.user(f"{seed}编辑-{subject}", roles=[("editor", "")])
    question_id = world.question(author, subject=subject)
    submission_id = _create_submission(author, question_id)
    submitted = _submit(author, submission_id)
    assert submitted.status_code == 200, submitted.text
    accepted = _initial_review(editor, submission_id)
    assert accepted.status_code == 200, accepted.text
    detail = _detail(author, submission_id)
    assert detail["status"] == "community_review"
    return {
        "author": author, "editor": editor, "question_id": question_id,
        "submission_id": submission_id, "public_id": detail["public_id"],
        "subject": subject, "version": 1,
    }


def _verify(world, context, *, seed="门禁评审", version=1):
    voters = _voters(world, context.get("subject", "数学"), seed=seed)
    for reviewer in voters:
        response = _review(reviewer, context["submission_id"], version=version)
        assert response.status_code == 201, response.text
    detail = _detail(context["author"], context["submission_id"])
    assert detail["status"] == "verified", detail
    context["voters"] = voters
    context["public_id"] = detail["public_id"]
    return context


def _verified(world, *, subject="数学", author=None, editor=None, seed="门禁社区"):
    context = _community(
        world, subject=subject, author=author, editor=editor, seed=seed,
    )
    _verify(world, context, seed=f"{seed}评审")
    return context


def _published(world, *, subject="数学", author=None, editor=None, seed="门禁社区"):
    context = _verified(
        world, subject=subject, author=author, editor=editor, seed=seed,
    )
    published = _publish(context["editor"], context["submission_id"])
    assert published.status_code == 200, published.text
    context["publish"] = published.json()
    context["public_id"] = context["publish"]["release"]["public_id"]
    return context


def _edit_question(author, question_id, *, stem, answers=("A",)):
    response = author["client"].patch(f"/api/paperlab/questions/{question_id}", json={
        "version": 1, "content": {"stem": stem, "answers": list(answers)},
    })
    assert response.status_code == 200, response.text
    return response.json()


def _revise_to_version_two(world, context):
    """已发布 v1 撤回后改为 v2（待评），并完成初审 + 5 票评审；返回 v2 版本号。"""
    editor = context["editor"]
    author = context["author"]
    withdrawal = _withdraw_release(editor, context["public_id"], reason="修订后重新发布")
    assert withdrawal.status_code == 200, withdrawal.text
    _edit_question(author, context["question_id"], stem="已知 2+3=？", answers=("B",))
    revised = author["client"].post(
        f"{API}/submissions/{context['submission_id']}/revise", json={"version": 1},
    )
    assert revised.status_code == 200, revised.text
    version = revised.json()["submission"]["current_version"]
    assert version == 2
    accepted = _initial_review(editor, context["submission_id"], version=version)
    assert accepted.status_code == 200, accepted.text
    for reviewer in context["voters"]:
        response = _review(reviewer, context["submission_id"], version=version)
        assert response.status_code == 201, response.text
    context["version"] = version
    return version


# ---------------------------------------------------------------------------
# 一、抽检门禁：状态、权限与模式解析
# ---------------------------------------------------------------------------

def test_gate_state_defaults_and_permissions(world, monkeypatch):
    monkeypatch.delenv(GATE_ENV, raising=False)
    monkeypatch.delenv(GATE_ARMED_ENV, raising=False)
    anon = _anon()
    try:
        assert anon.get(f"{API}/admin/quality/gate").status_code == 401
    finally:
        anon.close()
    reviewer = world.user("门禁查询越权评审", roles=[("reviewer", "数学")])
    assert reviewer["client"].get(f"{API}/admin/quality/gate").status_code == 403
    editor = world.user("门禁查询编辑", roles=[("editor", "")])
    state = _gate_state(editor)
    assert state["mode"] == "initial_100"
    assert state["mode_source"] == "default"
    assert state["armed"] is False
    assert state["armed_source"] == "auto"
    assert state["enforced"] is False
    assert state["required"] is False
    assert set(state["allowed_modes"]) == {"off", "initial_100", "always"}
    assert state["pilot_limit"] == 100
    assert state["counts"] == {"blocked": 0, "override": 0, "passed": 0}
    assert state["blocked_total"] == 0
    assert state["recent"] == []
    # 只有管理员可以修改；非法模式 422
    assert editor["client"].put(
        f"{API}/admin/quality/gate", json={"mode": "always"},
    ).status_code == 403
    assert world.admin.put(
        f"{API}/admin/quality/gate", json={"mode": "nope"},
    ).status_code == 422
    updated = _set_gate(world.admin, mode="off", pilot_limit=5)
    assert updated["mode"] == "off"
    assert updated["mode_source"] == "setting"
    assert updated["pilot_limit"] == 5
    assert updated["enforced"] is False
    reset = _set_gate(world.admin, reset=True)
    assert reset["mode"] == "initial_100"
    assert reset["mode_source"] == "default"


def test_gate_default_unactivated_allows_publish(world, monkeypatch):
    """未显式配置且库中无抽检记录：默认 initial_100 不激活，首签放行（兼容既有流程）。"""
    monkeypatch.delenv(GATE_ENV, raising=False)
    monkeypatch.delenv(GATE_ARMED_ENV, raising=False)
    context = _verified(world)
    response = _publish(context["editor"], context["submission_id"])
    assert response.status_code == 200, response.text
    gate = response.json()["quality_gate"]
    assert gate["mode"] == "initial_100"
    assert gate["armed"] is False
    assert gate["enforced"] is False
    assert gate["required"] is False
    assert gate["blocking"] is False
    assert gate["override"] is False
    state = _gate_state(context["editor"])
    assert state["blocked_total"] == 0
    assert state["passed_total"] == 0


def test_gate_env_initial_100_blocks_with_guidance(world, monkeypatch):
    monkeypatch.setenv(GATE_ENV, "initial_100")
    monkeypatch.delenv(GATE_ARMED_ENV, raising=False)
    context = _verified(world)
    response = _publish(context["editor"], context["submission_id"])
    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert detail["code"] == "quality_gate_blocked"
    assert detail["mode"] == "initial_100"
    assert detail["submission_id"] == context["submission_id"]
    assert detail["version"] == 1
    assert detail["override_allowed"] is True
    assert "guidance" in detail and "抽检" in detail["guidance"]
    assert detail["gate"]["armed"] is True
    assert detail["gate"]["enforced"] is True
    # 阻断不产生发布、不改变投稿状态、不污染作者事件链
    assert _detail(context["author"], context["submission_id"])["status"] == "verified"
    assert context["editor"]["client"].get(f"{API}/releases").json()["total"] == 0
    assert "published" not in _event_actions(
        context["author"], context["submission_id"],
    )
    # 门禁审计可查：阻断计数 + 最近记录
    state = _gate_state(context["editor"])
    assert state["armed"] is True
    assert state["mode_source"] == "env"
    assert state["blocked_total"] == 1
    assert state["recent"][0]["outcome"] == "blocked"
    assert state["recent"][0]["version"] == 1
    # 管理员可显式关闭 / 重置
    assert _set_gate(world.admin, mode="off")["enforced"] is False


def test_pre_publish_check_pass_unlocks_publish(world, monkeypatch):
    monkeypatch.setenv(GATE_ENV, "initial_100")
    context = _verified(world)
    # 签发前抽检：verified 可先抽检（community_review 仍 409）
    checked = _quality_check(
        context["editor"], context["submission_id"],
        decision="pass", note="题干/答案/解析与来源核验通过",
    )
    assert checked.status_code == 201, checked.text
    body = checked.json()
    assert body["phase"] == "pre_publish"
    assert body["check"]["phase"] == "pre_publish"
    assert body["check"]["release_id"] is None
    assert body["gate"]["publish_ready"] is True
    assert body["gate"]["blocking"] is False
    assert body["auto_withdrawn"] is False
    assert body["withdraw"] is None

    published = _publish(context["editor"], context["submission_id"])
    assert published.status_code == 200, published.text
    gate = published.json()["quality_gate"]
    assert gate["mode"] == "initial_100"
    assert gate["armed"] is True
    assert gate["enforced"] is True
    assert gate["required"] is True
    assert gate["satisfied"] is True
    assert gate["blocking"] is False
    assert gate["pass_check"]["id"] == body["check"]["id"]
    assert gate["override"] is False
    state = _gate_state(context["editor"])
    assert state["passed_total"] == 1

    # 发布后抽检仍可用（phase=post_publish，带 release_id）
    post = _quality_check(
        context["editor"], context["submission_id"], decision="pass",
        note="发布后复核通过",
    )
    assert post.status_code == 201, post.text
    assert post.json()["phase"] == "post_publish"
    assert post.json()["check"]["release_id"] is not None

    # 签发前抽检查出问题：仍阻断签发（latest 记录为准）
    other = _verified(world, seed="门禁问题社区")
    issue = _quality_check(
        other["editor"], other["submission_id"], decision="issue",
        severity="medium", findings=["解析步骤跳步"],
    )
    assert issue.status_code == 201, issue.text
    assert issue.json()["phase"] == "pre_publish"
    assert issue.json()["gate"]["blocking"] is True
    blocked = _publish(other["editor"], other["submission_id"])
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["latest_check"]["decision"] == "issue"


def test_gate_off_mode_allows_publish(world):
    _set_gate(world.admin, mode="off")
    context = _verified(world)
    response = _publish(context["editor"], context["submission_id"])
    assert response.status_code == 200, response.text
    gate = response.json()["quality_gate"]
    assert gate["mode"] == "off"
    assert gate["enforced"] is False
    assert gate["required"] is False
    assert gate["blocking"] is False


def test_gate_always_mode_requires_pass_each_version(world):
    _set_gate(world.admin, mode="always")
    context = _verified(world)
    blocked = _publish(context["editor"], context["submission_id"])
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["gate"]["required"] is True
    assert _quality_check(
        context["editor"], context["submission_id"], decision="pass",
    ).status_code == 201
    assert _publish(context["editor"], context["submission_id"]).status_code == 200

    version = _revise_to_version_two(world, context)
    blocked_v2 = _publish(context["editor"], context["submission_id"], version=version)
    assert blocked_v2.status_code == 409
    assert blocked_v2.json()["detail"]["version"] == 2
    assert _quality_check(
        context["editor"], context["submission_id"], decision="pass",
        note="新版解析复核通过",
    ).status_code == 201
    published_v2 = _publish(context["editor"], context["submission_id"], version=version)
    assert published_v2.status_code == 200, published_v2.text
    assert _db_rows(
        "SELECT version, decision FROM plaza_quality_checks "
        "WHERE submission_id=? ORDER BY id",
        (context["submission_id"],),
    )[-1]["version"] == 2


def test_gate_pilot_limit_window_closes(world):
    _set_gate(world.admin, mode="initial_100", pilot_limit=1)
    first = _verified(world)
    assert _quality_check(
        first["editor"], first["submission_id"], decision="pass",
    ).status_code == 201
    assert _publish(first["editor"], first["submission_id"]).status_code == 200
    assert _gate_state(first["editor"])["published_total"] == 1

    second = _verified(world, seed="门禁窗口社区")
    response = _publish(second["editor"], second["submission_id"])
    assert response.status_code == 200, response.text
    gate = response.json()["quality_gate"]
    assert gate["mode"] == "initial_100"
    assert gate["enforced"] is True
    assert gate["required"] is False
    assert gate["window_open"] is False
    assert gate["blocking"] is False
    # 窗口关闭后不再产生 blocked/passed 审计
    assert _gate_state(second["editor"])["blocked_total"] == 0


def test_gate_override_requires_reason_and_is_audited(world, monkeypatch):
    monkeypatch.setenv(GATE_ENV, "initial_100")
    context = _verified(world)
    no_reason = _publish(
        context["editor"], context["submission_id"], override=True,
    )
    assert no_reason.status_code == 422
    assert "override_reason" in json.dumps(no_reason.json(), ensure_ascii=False)

    overridden = _publish(
        context["editor"], context["submission_id"], override=True,
        override_reason="试点紧急上线，内容已由双人人工复核",
    )
    assert overridden.status_code == 200, overridden.text
    gate = overridden.json()["quality_gate"]
    assert gate["override"] is True
    assert gate["override_reason"].startswith("试点紧急上线")
    assert gate["blocking"] is False

    events = _events(context["author"], context["submission_id"])
    actions = [item["action"] for item in events]
    assert "quality_gate_override" in actions
    assert "published" in actions
    override_event = next(
        item for item in events if item["action"] == "quality_gate_override"
    )
    assert "双人人工复核" in override_event["reason"]
    published_event = next(item for item in events if item["action"] == "published")
    assert published_event["payload"]["quality_gate"]["override"] is True
    assert published_event["payload"]["quality_gate"]["mode"] == "initial_100"

    state = _gate_state(context["editor"])
    assert state["override_total"] == 1
    assert state["recent"][0]["outcome"] == "override"
    # 指标可查：override 次数
    metrics = context["editor"]["client"].get(f"{API}/admin/metrics").json()
    assert metrics["quality"]["gate"]["override_total"] == 1
    assert metrics["quality"]["gate"]["mode"] == "initial_100"


def test_gate_setting_overrides_env_and_reset(world, monkeypatch):
    monkeypatch.setenv(GATE_ENV, "always")
    state = _gate_state({"client": world.admin})
    assert state["mode"] == "always"
    assert state["mode_source"] == "env"
    assert state["armed"] is True
    # DB 设置优先于环境变量（运行期可切换）
    updated = _set_gate(world.admin, mode="off")
    assert updated["mode"] == "off"
    assert updated["mode_source"] == "setting"
    assert updated["enforced"] is False
    # reset 后回落到环境变量值
    reset = _set_gate(world.admin, reset=True)
    assert reset["mode"] == "always"
    assert reset["mode_source"] == "env"
    # 显式 armed=false 可单独关闭强制（模式保留，仅不激活）
    paused = _set_gate(world.admin, armed=False)
    assert paused["mode"] == "always"
    assert paused["armed"] is False
    assert paused["enforced"] is False
    resumed = _set_gate(world.admin, armed=True)
    assert resumed["armed"] is True
    assert resumed["enforced"] is True


# ---------------------------------------------------------------------------
# 二、一键恢复
# ---------------------------------------------------------------------------

def test_restore_after_quality_high_withdraw_restores_public_access(world):
    context = _published(world)
    favoriter = world.user("恢复收藏者")
    assert favoriter["client"].post(
        f"{API}/questions/{context['public_id']}/favorite"
    ).status_code == 200
    checked = _quality_check(
        context["editor"], context["submission_id"], decision="issue",
        severity="high", findings=["答案错误，正确应为 B"], note="严重错题，停止公开",
    )
    assert checked.status_code == 201, checked.text
    assert checked.json()["auto_withdrawn"] is True
    anon = _anon()
    try:
        assert anon.get(f"{API}/questions").json()["total"] == 0
        assert anon.get(
            f"{API}/questions/{context['public_id']}"
        ).json()["visible"] is False
    finally:
        anon.close()

    restored = _restore(
        context["editor"], context["public_id"],
        reason="错题答案已修订并由学科复核员复核通过",
    )
    assert restored.status_code == 200, restored.text
    body = restored.json()
    assert body["restore_source"] == "quality_auto_withdraw"
    assert body["restored_version"] == 1
    assert body["submission"]["status"] == "published"
    assert body["submission"]["public_visible"] is True
    assert body["notified"]["author"] is True
    assert body["notified"]["favoriters"] >= 1
    assert body["quality_check"]["severity"] == "high"
    assert body["idempotent"] is False

    anon = _anon()
    try:
        public = anon.get(f"{API}/questions/{context['public_id']}").json()
        assert public["visible"] is True
        assert public["status"] == "published"
        assert anon.get(f"{API}/questions").json()["total"] == 1
    finally:
        anon.close()
    releases = context["editor"]["client"].get(f"{API}/releases").json()["items"]
    assert [item["status"] for item in releases] == ["active"]
    actions = _event_actions(context["author"], context["submission_id"])
    assert "release_restored" in actions
    assert "release_restored" in _notification_kinds(context["author"])
    assert "favorite_restored" in _notification_kinds(favoriter)
    favorites = favoriter["client"].get(f"{API}/favorites").json()
    assert favorites["items"][0]["available"] is True
    # 重复恢复幂等，不重复通知
    again = _restore(context["editor"], context["public_id"])
    assert again.status_code == 200
    assert again.json()["idempotent"] is True
    assert _notification_count(context["author"], "release_restored") == 1


def test_restore_after_editor_withdraw_and_validation(world):
    context = _published(world)
    anon = _anon()
    try:
        assert anon.post(f"{API}/releases/{context['public_id']}/restore", json={
            "reason": "匿名恢复", "confirm_resolved": True,
        }).status_code == 401
    finally:
        anon.close()
    # 未知发布号 404
    assert _restore(
        context["editor"], "plz-not-exists",
    ).status_code == 404
    # 活跃发布（无需恢复）幂等 200
    active = _restore(context["editor"], context["public_id"], confirm=False)
    assert active.status_code == 200
    assert active.json()["idempotent"] is True
    # 编辑撤回后可恢复；未确认问题已处理 → 422
    withdrawal = _withdraw_release(
        context["editor"], context["public_id"],
        reason="来源权利待确认，先下架复核",
    )
    assert withdrawal.status_code == 200, withdrawal.text
    missing_confirm = _restore(context["editor"], context["public_id"], confirm=False)
    assert missing_confirm.status_code == 422
    assert "confirm_resolved" in json.dumps(
        missing_confirm.json(), ensure_ascii=False,
    )
    short_reason = context["editor"]["client"].post(
        f"{API}/releases/{context['public_id']}/restore",
        json={"reason": "x", "confirm_resolved": True},
    )
    assert short_reason.status_code == 422
    restored = _restore(context["editor"], context["public_id"])
    assert restored.status_code == 200, restored.text
    assert restored.json()["restore_source"] == "editor_withdraw"
    assert restored.json()["quality_check"] is None
    actions = _event_actions(context["author"], context["submission_id"])
    assert "release_restored" in actions
    assert "release_restored" in _notification_kinds(context["author"])


def test_restore_permissions_and_author_avoidance(world):
    author_editor = world.user("恢复兼作者编辑", roles=[("editor", "")])
    publisher = world.user("恢复独立编辑", roles=[("editor", "")])
    context = _published(world, author=author_editor, editor=publisher)
    checked = _quality_check(
        publisher, context["submission_id"], decision="issue",
        severity="high", findings=["配图与题干不符"],
    )
    assert checked.status_code == 201, checked.text
    assert checked.json()["auto_withdrawn"] is True

    reviewer = world.user("恢复越权评审", roles=[("reviewer", "数学")])
    student = world.user("恢复越权学生")
    for user in (reviewer, student):
        assert _restore(user, context["public_id"]).status_code == 403
    avoided = _restore(author_editor, context["public_id"])
    assert avoided.status_code == 403
    assert "回避" in avoided.json()["detail"]
    restored = _restore(publisher, context["public_id"])
    assert restored.status_code == 200, restored.text


def test_restore_conflict_with_newer_active_release(world, monkeypatch):
    monkeypatch.delenv(GATE_ENV, raising=False)
    context = _published(world)
    assert _quality_check(
        context["editor"], context["submission_id"], decision="issue",
        severity="high", findings=["答案错误"],
    ).json()["auto_withdrawn"] is True
    # 抽检已开始 → 默认门禁自动激活；关闭门禁后重新签发 v2（本用例只验证恢复冲突）
    _set_gate(world.admin, mode="off")
    version = _revise_to_version_two(world, context)
    published_v2 = _publish(context["editor"], context["submission_id"], version=version)
    assert published_v2.status_code == 200, published_v2.text
    conflict = _restore(context["editor"], context["public_id"])
    assert conflict.status_code == 409, conflict.text
    detail = conflict.json()["detail"]
    assert "活跃发布" in detail["message"]
    assert detail["active_version"] == 2
    assert detail["restore_version"] == 1
    # 线上仍是 v2
    anon = _anon()
    try:
        public = anon.get(f"{API}/questions/{context['public_id']}").json()
        assert "2+3" in public["content"]["stem"]
    finally:
        anon.close()


def test_restore_rejects_rollback_withdrawal(world):
    context = _published(world)
    version = _revise_to_version_two(world, context)
    assert _publish(
        context["editor"], context["submission_id"], version=version,
    ).status_code == 200
    rolled = context["editor"]["client"].post(
        f"{API}/submissions/{context['submission_id']}/rollback",
        json={"reason": "新版解析仍有争议，回滚到上一已发布版本"},
    )
    assert rolled.status_code == 200, rolled.text
    rejected = _restore(context["editor"], context["public_id"])
    assert rejected.status_code == 409, rejected.text
    assert "回滚" in rejected.json()["detail"]["message"]


# ---------------------------------------------------------------------------
# 三、批量处理
# ---------------------------------------------------------------------------

def _submit_issue(reporter, public_id, *, field="answer", description="参考答案有误"):
    response = reporter["client"].post(f"{API}/questions/{public_id}/issues", json={
        "field": field, "description": description,
    })
    assert response.status_code == 201, response.text
    return response.json()["issue"]["id"]


def test_batch_issues_partial_success(world):
    context = _published(world)
    reporters = [world.user(f"批量反馈人{i}") for i in range(3)]
    issue_ids = [
        _submit_issue(reporter, context["public_id"], description=f"反馈 {index}")
        for index, reporter in enumerate(reporters)
    ]
    # 先单条处理掉一条，制造部分成功
    handled = context["editor"]["client"].patch(
        f"{API}/admin/issues/{issue_ids[0]}",
        json={"action": "resolve", "reason": "已修订并通知作者"},
    )
    assert handled.status_code == 200, handled.text
    batch = context["editor"]["client"].post(f"{API}/admin/issues/batch", json={
        "ids": [*issue_ids, 999999],
        "action": "resolve",
        "reason": "批量核对后统一采纳",
    })
    assert batch.status_code == 200, batch.text
    body = batch.json()
    assert body["kind"] == "issue"
    assert body["action"] == "resolve"
    assert body["requested"] == 4
    assert body["updated_count"] == 2
    assert body["skipped_count"] == 2
    assert sorted(body["updated_ids"]) == sorted(issue_ids[1:])
    skipped = {item["id"]: item for item in body["skipped"]}
    assert skipped[issue_ids[0]]["reason"] == "already_handled"
    assert skipped[999999]["reason"] == "not_found"
    assert body["batch_id"] >= 1
    # 事件链与通知：处理事件逐条写入，反馈人收到结果通知
    actions = _event_actions(context["author"], context["submission_id"])
    assert actions.count("issue_resolved") == 3
    assert "issue_resolved" in _notification_kinds(reporters[1])
    assert "issue_resolved" in _notification_kinds(reporters[2])
    # 批次审计行与指标批量量
    rows = _db_rows("SELECT * FROM plaza_ops_batches ORDER BY id")
    assert len(rows) == 1
    assert rows[0]["kind"] == "issue"
    assert rows[0]["requested"] == 4
    assert rows[0]["updated"] == 2
    assert rows[0]["skipped"] == 2
    metrics = context["editor"]["client"].get(f"{API}/admin/metrics").json()
    assert metrics["ops"]["batch"]["batches"] == 1
    assert metrics["ops"]["batch"]["updated_total"] == 2
    assert metrics["ops"]["batch"]["skipped_total"] == 2
    # 重复批量全部跳过，不重复通知
    repeat = context["editor"]["client"].post(f"{API}/admin/issues/batch", json={
        "ids": issue_ids, "action": "resolve", "reason": "重复批量",
    })
    assert repeat.status_code == 200
    assert repeat.json()["updated_count"] == 0
    assert repeat.json()["skipped_count"] == 3
    assert _notification_count(reporters[1], "issue_resolved") == 1


def test_batch_validation_and_permissions(world):
    context = _published(world)
    anon = _anon()
    try:
        assert anon.post(f"{API}/admin/issues/batch", json={
            "ids": [1], "action": "resolve",
        }).status_code == 401
    finally:
        anon.close()
    reviewer = world.user("批量越权评审", roles=[("reviewer", "数学")])
    student = world.user("批量越权学生")
    for user in (reviewer, student):
        assert user["client"].post(f"{API}/admin/issues/batch", json={
            "ids": [1], "action": "resolve",
        }).status_code == 403
        assert user["client"].post(f"{API}/admin/reports/batch", json={
            "ids": [1], "action": "resolve",
        }).status_code == 403
        assert user["client"].post(f"{API}/admin/appeals/batch", json={
            "ids": [1], "action": "resolve",
        }).status_code == 403
    editor = context["editor"]["client"]
    assert editor.post(f"{API}/admin/issues/batch", json={
        "ids": [], "action": "resolve",
    }).status_code == 422
    assert editor.post(f"{API}/admin/issues/batch", json={
        "ids": list(range(1, 52)), "action": "resolve",
    }).status_code == 422
    assert editor.post(f"{API}/admin/issues/batch", json={
        "ids": [1], "action": "maybe",
    }).status_code == 422
    # 驳回必须给理由（在逐条处理前校验）
    no_reason = editor.post(f"{API}/admin/issues/batch", json={
        "ids": [1], "action": "reject",
    })
    assert no_reason.status_code == 422
    # 全部不存在：200 + skipped（部分成功语义）
    missing = editor.post(f"{API}/admin/issues/batch", json={
        "ids": [777, 888], "action": "resolve", "reason": "不存在",
    })
    assert missing.status_code == 200
    assert missing.json()["updated_count"] == 0
    assert {item["reason"] for item in missing.json()["skipped"]} == {"not_found"}


def test_batch_reports_and_appeals(world):
    context = _published(world)
    reporter1 = world.user("批量举报人1")
    reporter2 = world.user("批量举报人2")
    report_ids = []
    for reporter in (reporter1, reporter2):
        created = reporter["client"].post(
            f"{API}/questions/{context['public_id']}/reports",
            json={"reason": "copyright", "detail": "疑似未授权转载"},
        )
        assert created.status_code == 201, created.text
        report_ids.append(created.json()["report"]["id"])
    rejected = context["editor"]["client"].post(f"{API}/admin/reports/batch", json={
        "ids": report_ids, "action": "reject",
        "reason": "来源与授权材料齐全，举报不成立",
    })
    assert rejected.status_code == 200, rejected.text
    assert rejected.json()["updated_count"] == 2
    assert "report_rejected" in _notification_kinds(reporter1)
    assert "report_rejected" in _notification_kinds(reporter2)

    # 申诉批量受理
    author = world.user("批量申诉作者")
    editor = context["editor"]
    question_id = world.question(author)
    submission_id = _create_submission(author, question_id)
    assert _submit(author, submission_id).status_code == 200
    assert _initial_review(
        editor, submission_id, decision="reject", reason="来源信息不足，请补充",
    ).status_code == 200
    appeal = author["client"].post(f"{API}/submissions/{submission_id}/appeals", json={
        "reason": "来源出处已在第 12 页题号 3，请复核",
    })
    assert appeal.status_code == 201, appeal.text
    appeal_id = appeal.json()["appeal"]["id"]
    resolved = editor["client"].post(f"{API}/admin/appeals/batch", json={
        "ids": [appeal_id], "action": "resolve",
        "reason": "已复核来源，恢复作者修订权限",
    })
    assert resolved.status_code == 200, resolved.text
    assert resolved.json()["updated_count"] == 1
    assert resolved.json()["updated"][0]["status"] == "resolved"
    assert "appeal_resolved" in _notification_kinds(author)
    metrics = editor["client"].get(f"{API}/admin/metrics").json()
    kinds = {item["kind"]: item for item in metrics["ops"]["batch"]["by_kind"]}
    assert kinds["report"]["updated"] == 2
    assert kinds["appeal"]["updated"] == 1


# ---------------------------------------------------------------------------
# 四、通知增强
# ---------------------------------------------------------------------------

def test_notifications_unread_count_and_kind_read(world):
    context = _community(world)
    author = context["author"]
    anon = _anon()
    try:
        assert anon.get(f"{API}/notifications/unread-count").status_code == 401
    finally:
        anon.close()
    listing = _notifications(author, page_size=100)
    summary = author["client"].get(f"{API}/notifications/unread-count").json()
    assert summary["unread_count"] == listing["unread_count"]
    assert summary["unread_count"] >= 2
    assert summary["by_kind"]["submission_submitted"] >= 1
    assert summary["by_kind"]["initial_review_approved"] >= 1
    assert sum(summary["by_kind"].values()) == summary["unread_count"]
    assert summary["kinds_total"] == len(summary["by_kind"])
    # 按 kind 全读（无 ids）：只影响该类型
    before = summary["unread_count"]
    submitted_count = summary["by_kind"]["submission_submitted"]
    marked = author["client"].post(f"{API}/notifications/read", json={
        "kind": "submission_submitted",
    })
    assert marked.status_code == 200, marked.text
    assert marked.json()["scope"] == "kind"
    assert marked.json()["updated"] == submitted_count
    assert marked.json()["unread_count"] == before - submitted_count
    # 该类型已读，其他类型仍未读
    remaining = author["client"].get(f"{API}/notifications/unread-count").json()
    assert "submission_submitted" not in remaining["by_kind"]
    assert remaining["by_kind"]["initial_review_approved"] >= 1
    assert _notifications(author, kind="submission_submitted", unread_only=True)["total"] == 0
    # 未知类型 422
    invalid = author["client"].post(f"{API}/notifications/read", json={
        "kind": "not_a_kind",
    })
    assert invalid.status_code == 422
    assert "allowed" in invalid.json()["detail"]
    # all + kind 组合：只读该类型
    all_read = author["client"].post(f"{API}/notifications/read", json={
        "all": True, "kind": "initial_review_approved",
    })
    assert all_read.status_code == 200
    assert all_read.json()["unread_count"] == 0
    assert all_read.json()["message"].startswith("已将")


def test_notification_dedupe_window(world, monkeypatch):
    monkeypatch.delenv(DEDUPE_ENV, raising=False)
    context = _published(world)
    first = _quality_check(
        context["editor"], context["submission_id"], decision="issue",
        severity="medium", findings=["解析步骤跳步"],
    )
    assert first.status_code == 201
    assert first.json()["notified"]["author"] is True
    second = _quality_check(
        context["editor"], context["submission_id"], decision="issue",
        severity="low", findings=["解析排版问题"],
    )
    assert second.status_code == 201
    notifications = _notifications(context["author"], kind="quality_issue", page_size=100)
    assert notifications["total"] == 1
    assert notifications["items"][0]["payload"]["version"] == 1
    assert _db_rows(
        "SELECT COUNT(*) AS n FROM plaza_quality_checks WHERE submission_id=?",
        (context["submission_id"],),
    )[0]["n"] == 2
    # 关闭去重窗口（0）后同一事件会再次通知（便于对照）
    monkeypatch.setenv(DEDUPE_ENV, "0")
    third = _quality_check(
        context["editor"], context["submission_id"], decision="issue",
        severity="medium", findings=["解析仍有笔误"],
    )
    assert third.status_code == 201
    assert _notifications(
        context["author"], kind="quality_issue", page_size=100,
    )["total"] == 2


# ---------------------------------------------------------------------------
# 五、指标补充
# ---------------------------------------------------------------------------

def test_metrics_phase4_gate_restore_batch_and_notifications(world, monkeypatch):
    monkeypatch.setenv(GATE_ENV, "initial_100")
    context = _verified(world)
    blocked = _publish(context["editor"], context["submission_id"])
    assert blocked.status_code == 409
    assert _quality_check(
        context["editor"], context["submission_id"], decision="pass",
        note="签发前抽检通过",
    ).status_code == 201
    assert _publish(context["editor"], context["submission_id"]).status_code == 200
    assert _quality_check(
        context["editor"], context["submission_id"], decision="issue",
        severity="high", findings=["答案错误"],
    ).json()["auto_withdrawn"] is True
    assert _restore(context["editor"], context["public_id"]).status_code == 200

    reporters = [world.user(f"指标批量反馈人{i}") for i in range(2)]
    issue_ids = [
        _submit_issue(reporter, context["public_id"], description=f"指标反馈 {i}")
        for i, reporter in enumerate(reporters)
    ]
    assert context["editor"]["client"].post(f"{API}/admin/issues/batch", json={
        "ids": issue_ids, "action": "resolve", "reason": "批量采纳并修订",
    }).json()["updated_count"] == 2

    response = context["editor"]["client"].get(f"{API}/admin/metrics")
    assert response.status_code == 200, response.text
    body = response.json()
    gate = body["quality"]["gate"]
    assert gate["mode"] == "initial_100"
    assert gate["mode_source"] == "env"
    assert gate["armed"] is True
    assert gate["enforced"] is True
    assert gate["blocked_total"] == 1
    assert gate["passed_total"] == 1
    assert gate["override_total"] == 0
    assert gate["pilot_limit"] == 100
    assert body["quality"]["pre_publish_checks"] == 1
    assert body["quality"]["post_publish_checks"] == 1
    assert body["quality"]["sampling"]["coverage_rate"] == 1.0
    assert body["withdrawals"]["restorations"] == 1
    assert body["restorations"]["total"] == 1
    assert body["restorations"]["latest_event"]
    assert body["ops"]["batch"]["updated_total"] == 2
    assert body["ops"]["batch"]["batches"] == 1
    assert body["notifications"]["total"] >= 1
    assert body["notifications"]["unread_total"] >= 1
    assert body["notifications"]["read_total"] == (
        body["notifications"]["total"] - body["notifications"]["unread_total"]
    )
    assert "gate" in body["caliber"]["quality"]
    assert "ops" in body["caliber"]
    assert "notifications" in body["caliber"]
