"""广场投稿 / 公开只读端回归（Phase 2，WS13）。

覆盖：可选登录、投稿快照与预览校验、送审/改版/撤回、公开可见性与脱敏、
角色授权、身份申请、事件链与作者视角权限。
"""
import json

import pytest
from fastapi.testclient import TestClient

API = "/api/plaza"
PASSWORD = "plaza-pass-123"


def _client():
    from app.main import app
    return TestClient(app)


def _register(name, password=PASSWORD):
    client = _client()
    response = client.post("/api/auth/register", json={"username": name, "password": password})
    assert response.status_code == 200, response.text
    return client, response.json()


def _login(name, password=PASSWORD):
    client = _client()
    response = client.post("/api/auth/login", json={"username": name, "password": password})
    assert response.status_code == 200, response.text
    return client


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

    def close(self):
        for client in self.clients:
            client.close()

    def user(self, name, roles=()):
        client, payload = _register(name)
        self.clients.append(client)
        for role, subject in roles:
            granted = self.admin.post(f"{API}/admin/roles", json={
                "action": "grant", "role": role, "subject": subject, "username": name,
            })
            assert granted.status_code == 200, granted.text
        return {"client": client, "id": payload["id"], "name": name}

    def question(self, author, *, subject="数学", qtype="SINGLE",
                 stem="已知 1+1=？", answers=("A",), options=None,
                 explanation="基础加法。", tags=("有理数",)):
        content = {
            "stem": stem,
            "options": options or [
                {"label": "A", "text": "2"},
                {"label": "B", "text": "3"},
                {"label": "C", "text": "4"},
            ],
            "answers": list(answers),
            "explanation": explanation,
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
# 助手
# ---------------------------------------------------------------------------

def _create_submission(author, question_id, *, source=None, license_confirmed=True):
    return author["client"].post(f"{API}/submissions", json={
        "question_id": question_id,
        "source": _source() if source is None else source,
        "license_confirmed": license_confirmed,
    })


def _submit(author, submission_id, version, **overrides):
    body = {"version": version, "confirm_preview": True}
    body.update(overrides)
    return author["client"].post(f"{API}/submissions/{submission_id}/submit", json=body)


def _accept_initial(editor, submission_id, version=1):
    return editor["client"].post(f"{API}/submissions/{submission_id}/initial-review", json={
        "version": version, "decision": "accept",
        "source_checked": True, "privacy_checked": True, "completeness_checked": True,
    })


def _review(reviewer, submission_id, *, version=1, verdict="approve",
            reasons="", independent_answer="我独立作答得到 A。", conflict=False):
    return reviewer["client"].post(f"{API}/submissions/{submission_id}/reviews", json={
        "version": version,
        "verdict": verdict,
        "reasons": reasons,
        "independent_answer": independent_answer,
        "conflict_declared": conflict,
    })


def _community_setup(world, *, subject="数学", editor=None, author_name=None):
    """作者投稿 → 送审 → 编辑初审通过，返回社区待核验上下文。"""
    author = world.user(author_name or f"投稿作者-{subject}")
    if editor is None:
        editor = world.user(f"平台编辑-{subject}", roles=[("editor", "")])
    question_id = world.question(author, subject=subject)
    created = _create_submission(author, question_id)
    assert created.status_code == 201, created.text
    submission_id = created.json()["submission"]["id"]
    assert _submit(author, submission_id, 1).status_code == 200
    accepted = _accept_initial(editor, submission_id, 1)
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["submission"]["status"] == "community_review"
    return {
        "author": author, "editor": editor, "question_id": question_id,
        "submission_id": submission_id,
    }


def _voters(world, subject="数学"):
    voters = [world.user(f"{subject}学科复核{i}", roles=[("subject_reviewer", subject)])
              for i in range(2)]
    voters += [world.user(f"{subject}评审员{i}", roles=[("reviewer", subject)]) for i in range(3)]
    return voters


def _verified_setup(world, *, subject="数学"):
    context = _community_setup(world, subject=subject)
    voters = _voters(world, subject=subject)
    for reviewer in voters:
        response = _review(reviewer, context["submission_id"], version=1)
        assert response.status_code == 201, response.text
    context["voters"] = voters
    detail = context["author"]["client"].get(f"{API}/submissions/{context['submission_id']}").json()
    assert detail["submission"]["status"] == "verified"
    context["public_id"] = detail["submission"]["public_id"]
    return context


def _edit_question(author, question_id, *, stem="已知 2+2=？", answers=("A",)):
    response = author["client"].patch(f"/api/paperlab/questions/{question_id}", json={
        "version": 1,
        "content": {"stem": stem, "answers": list(answers)},
    })
    assert response.status_code == 200, response.text
    return response.json()["question"]


# ---------------------------------------------------------------------------
# 可选登录与能力摘要
# ---------------------------------------------------------------------------

def test_optional_user_me_anonymous_and_authenticated(world):
    anon = _client()
    try:
        payload = anon.get(f"{API}/me")
        assert payload.status_code == 200
        body = payload.json()
        assert body["authenticated"] is False
        assert body["capabilities"]["submit"] is False
        # 公开列表匿名可读，且不带任何会话要求
        assert anon.get(f"{API}/questions").status_code == 200
        # 伪造/过期会话按匿名处理，不返回 401，也不影响公开读
        anon.cookies.set("study_session", "forged-session-token")
        assert anon.get(f"{API}/questions").status_code == 200
        assert anon.get(f"{API}/me").json()["authenticated"] is False
    finally:
        anon.close()

    me = world.admin.get(f"{API}/me").json()
    assert me["authenticated"] is True
    assert me["capabilities"]["admin"] is True
    assert me["capabilities"]["initial_review"] is True
    assert me["user"]["username"] == "测试管理员"


# ---------------------------------------------------------------------------
# 投稿创建 / 预览校验
# ---------------------------------------------------------------------------

def test_create_submission_builds_immutable_preview_and_checks(world):
    author = world.user("投稿作者A")
    question_id = world.question(author)
    created = _create_submission(author, question_id)
    assert created.status_code == 201, created.text
    body = created.json()
    submission = body["submission"]
    assert submission["status"] == "draft"
    assert submission["version"] == 1
    assert submission["subject"] == "数学"
    assert submission["snapshot"]["content"]["stem"] == "已知 1+1=？"
    assert submission["public_id"] is None
    assert submission["public_visible"] is False
    assert body["checks"]["blocking"] == []
    # 作者自检是提示项、不阻断投稿
    assert "self_verified" in body["checks"]["warnings"][0]["key"]
    assert body["preview"]["answer_count"] == 1
    assert body["preview"]["source_declaration"]["license_basis"].startswith("原创")
    # 来源声明确认信息只留在内部视图，公开投影不含
    assert submission["source"]["confirmed_by_user_id"] == author["id"]

    versions = _db_rows(
        "SELECT version, snapshot_json FROM plaza_submission_versions WHERE submission_id=?",
        (submission["id"],),
    )
    assert [row["version"] for row in versions] == [1]
    events = _db_rows(
        "SELECT action, from_status, to_status FROM plaza_events WHERE submission_id=?",
        (submission["id"],),
    )
    assert events[0]["action"] == "created"


def test_create_submission_requires_owned_question(world):
    author = world.user("投稿作者B")
    other = world.user("他人C")
    question_id = world.question(author)
    denied = _create_submission(other, question_id)
    assert denied.status_code == 404
    missing = _create_submission(author, 999999)
    assert missing.status_code == 404
    assert author["client"].post(f"{API}/submissions", json={
        "question_id": question_id,
    }).status_code == 201  # 未声明授权也可保存草稿，但送审会被拦截


def test_submit_requires_preview_confirmation_and_complete_declaration(world):
    author = world.user("投稿作者D")
    question_id = world.question(author)
    submission_id = _create_submission(
        author, question_id, source={"url": "https://example.com/only-url"},
        license_confirmed=False,
    ).json()["submission"]["id"]

    unconfirmed = _submit(author, submission_id, 1, confirm_preview=False)
    assert unconfirmed.status_code == 422
    assert "预览" in unconfirmed.json()["detail"]["message"]

    incomplete = _submit(author, submission_id, 1)
    assert incomplete.status_code == 422
    detail = incomplete.json()["detail"]
    assert "license_confirmed" in detail["blocking"]
    assert "license_basis" in detail["blocking"]
    assert "rights_scope" in detail["blocking"]
    assert "attribution" in detail["blocking"]
    # 仍停在草稿，公开不可见
    mine = author["client"].get(f"{API}/submissions/mine").json()
    assert mine["items"][0]["status"] == "draft"


def test_submit_moves_to_pending_initial_and_repeat_conflicts(world):
    author = world.user("投稿作者E")
    question_id = world.question(author)
    submission_id = _create_submission(author, question_id).json()["submission"]["id"]
    first = _submit(author, submission_id, 1)
    assert first.status_code == 200, first.text
    assert first.json()["submission"]["status"] == "pending_initial"
    repeated = _submit(author, submission_id, 1)
    assert repeated.status_code == 409
    assert "重复提交" in repeated.json()["detail"]


# ---------------------------------------------------------------------------
# 草稿改版 / 送审后改版 / 撤回
# ---------------------------------------------------------------------------

def test_patch_draft_creates_new_version_and_keeps_history(world):
    author = world.user("投稿作者F")
    question_id = world.question(author)
    submission_id = _create_submission(author, question_id).json()["submission"]["id"]

    unchanged = author["client"].patch(f"{API}/submissions/{submission_id}", json={
        "version": 1, "source": _source(),
    })
    assert unchanged.status_code == 200, unchanged.text
    assert unchanged.json()["changed"] is False
    assert unchanged.json()["submission"]["version"] == 1

    _edit_question(author, question_id, stem="已知 2+2=？")
    changed = author["client"].patch(f"{API}/submissions/{submission_id}", json={"version": 1})
    assert changed.status_code == 200, changed.text
    body = changed.json()
    assert body["changed"] is True
    assert body["submission"]["version"] == 2
    assert body["submission"]["snapshot"]["content"]["stem"] == "已知 2+2=？"

    versions = _db_rows(
        "SELECT version, snapshot_json FROM plaza_submission_versions "
        "WHERE submission_id=? ORDER BY version",
        (submission_id,),
    )
    assert [row["version"] for row in versions] == [1, 2]
    old_snapshot = json.loads(versions[0]["snapshot_json"])
    assert old_snapshot["content"]["stem"] == "已知 1+1=？"
    # 旧版本号不能再次修改
    stale = author["client"].patch(f"{API}/submissions/{submission_id}", json={"version": 1})
    assert stale.status_code == 409
    assert "版本冲突" in stale.json()["detail"]


def test_patch_rejected_outside_draft(world):
    context = _community_setup(world)
    response = context["author"]["client"].patch(
        f"{API}/submissions/{context['submission_id']}", json={"version": 1},
    )
    assert response.status_code == 409
    assert "草稿" in response.json()["detail"]


def test_revise_after_review_resets_votes_and_versions(world):
    context = _community_setup(world)
    submission_id = context["submission_id"]
    voters = _voters(world)
    for reviewer in voters[:2]:
        assert _review(reviewer, submission_id).status_code == 201

    before = context["author"]["client"].get(f"{API}/submissions/{submission_id}").json()
    assert before["submission"]["review_summary"]["independent_reviews"] == 2

    # 内容未变时拒绝创建新版本
    same = context["author"]["client"].post(f"{API}/submissions/{submission_id}/revise", json={
        "version": 1,
    })
    assert same.status_code == 422

    _edit_question(context["author"], context["question_id"], stem="已知 3+3=？")
    revised = context["author"]["client"].post(f"{API}/submissions/{submission_id}/revise", json={
        "version": 1,
    })
    assert revised.status_code == 200, revised.text
    body = revised.json()
    assert body["changed"] is True
    assert body["submission"]["version"] == 2
    assert body["submission"]["status"] == "pending_initial"
    assert body["submission"]["review_summary"]["independent_reviews"] == 0
    # 旧版本投票留档，不计入新版本
    archived = _db_rows(
        "SELECT version FROM plaza_reviews WHERE submission_id=? ORDER BY id", (submission_id,),
    )
    assert [row["version"] for row in archived] == [1, 1]
    events = _db_rows(
        "SELECT action FROM plaza_events WHERE submission_id=? ORDER BY id", (submission_id,),
    )
    assert "revised" in [row["action"] for row in events]


def test_initial_reject_then_revise_returns_to_draft(world):
    author = world.user("投稿作者G")
    editor = world.user("平台编辑G", roles=[("editor", "")])
    question_id = world.question(author)
    submission_id = _create_submission(author, question_id).json()["submission"]["id"]
    assert _submit(author, submission_id, 1).status_code == 200

    no_reason = editor["client"].post(f"{API}/submissions/{submission_id}/initial-review", json={
        "version": 1, "decision": "reject",
    })
    assert no_reason.status_code == 422
    rejected = editor["client"].post(f"{API}/submissions/{submission_id}/initial-review", json={
        "version": 1, "decision": "reject", "reason": "来源链接无法打开，请补充可核验出处",
    })
    assert rejected.status_code == 200, rejected.text
    assert rejected.json()["submission"]["status"] == "rejected"

    _edit_question(author, question_id, stem="已知 4+4=？")
    revised = author["client"].post(f"{API}/submissions/{submission_id}/revise", json={"version": 1})
    assert revised.status_code == 200, revised.text
    assert revised.json()["submission"]["status"] == "draft"
    assert _submit(author, submission_id, 2).status_code == 200


def test_author_withdraw_and_public_state(world):
    context = _community_setup(world)
    response = context["author"]["client"].post(
        f"{API}/submissions/{context['submission_id']}/withdraw",
        json={"reason": "作者发现解析有误，撤回修改"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["submission"]["status"] == "withdrawn"
    anon = _client()
    try:
        assert anon.get(f"{API}/questions").json()["total"] == 0
    finally:
        anon.close()
    again = context["author"]["client"].post(
        f"{API}/submissions/{context['submission_id']}/withdraw",
    )
    assert again.status_code == 409
    events = context["author"]["client"].get(
        f"{API}/submissions/{context['submission_id']}/events"
    ).json()
    assert [item["action"] for item in events["items"]][-1] == "author_withdrawn"


# ---------------------------------------------------------------------------
# 公开只读：可见性、筛选与脱敏
# ---------------------------------------------------------------------------

def test_public_detail_after_verified_and_redacts_internal_fields(world):
    context = _verified_setup(world)
    public_id = context["public_id"]
    anon = _client()
    try:
        listing = anon.get(f"{API}/questions").json()
        assert listing["total"] == 1
        item = listing["items"][0]
        assert item["public_id"] == public_id
        assert item["source"] == "community"
        assert item["status"] == "verified"
        assert "社区" in item["notice"]

        detail = anon.get(f"{API}/questions/{public_id}")
        assert detail.status_code == 200, detail.text
        body = detail.json()
        assert body["visible"] is True
        assert body["content"]["answers"] == ["A"]
        assert body["content"]["explanation"] == "基础加法。"
        assert body["source_declaration"]["license_basis"]
        assert body["review"]["threshold_met"] is True
        assert "question_id" not in body
        text = json.dumps(body, ensure_ascii=False)
        # 公开投影禁止内部身份/模型/私有附件路径
        assert context["author"]["name"] not in text
        assert "confirmed_by_user_id" not in text
        assert "author_id" not in text
        assert "paperlab/" not in text
        assert "reviewer_id" not in text
    finally:
        anon.close()


def test_public_list_filters_and_pagination(world):
    context = _verified_setup(world)
    anon = _client()
    try:
        assert anon.get(f"{API}/questions?subject=数学").json()["total"] == 1
        assert anon.get(f"{API}/questions?subject=物理").json()["total"] == 0
        assert anon.get(f"{API}/questions?keyword=已知").json()["total"] == 1
        assert anon.get(f"{API}/questions?keyword=不存在的知识点").json()["total"] == 0
        assert anon.get(f"{API}/questions?type=单选题").json()["total"] == 1
        assert anon.get(f"{API}/questions?type=不存在的题型").status_code == 422
        assert anon.get(f"{API}/questions?source=community").json()["total"] == 1
        assert anon.get(f"{API}/questions?source=featured").json()["total"] == 0
        first = anon.get(f"{API}/questions?page=1&page_size=1").json()
        assert first["page_size"] == 1 and first["total"] == 1
        assert anon.get(f"{API}/questions?page_size=500").status_code == 422
        assert anon.get(f"{API}/questions/{context['public_id']}").status_code == 200
        assert anon.get(f"{API}/questions/plz-not-exists").status_code == 404
    finally:
        anon.close()


def test_community_review_submission_is_public_and_annotated(world):
    """方案 §5：初审通过进入「社区待核验」后，广场即可浏览并显著标注待核验。"""
    context = _community_setup(world)
    anon = _client()
    try:
        listing = anon.get(f"{API}/questions").json()
        assert listing["total"] == 1
        item = listing["items"][0]
        assert item["status"] == "community_review"
        assert item["status_label"] == "社区待核验"
        assert item["review_status"] == "community"
        assert item["source"] == "community"
        assert item["community_pending"] is True
        assert "待核验" in item["notice"]
        assert item["review"]["threshold_met"] is False
        assert item["review"]["min_reviews"] == 5
        # 初审通过即分配公开题号，旧链接可定位
        assert item["public_id"].startswith("plz-")

        detail = anon.get(f"{API}/questions/{item['public_id']}")
        assert detail.status_code == 200, detail.text
        body = detail.json()
        assert body["visible"] is True
        assert body["review_status"] == "community"
        assert body["community_pending"] is True
        assert "待核验" in body["notice"]
        assert body["content"]["answers"] == ["A"]
        # 公开投影不含内部身份、评审人身份与私有附件路径
        text = json.dumps(body, ensure_ascii=False)
        assert "author_id" not in text
        assert "reviewer_id" not in text
        assert "paperlab/" not in text
        assert context["author"]["name"] not in text
        # source 过滤：社区可见、精选为空
        assert anon.get(f"{API}/questions?source=community").json()["total"] == 1
        assert anon.get(f"{API}/questions?source=featured").json()["total"] == 0
    finally:
        anon.close()
    # 作者内部视图同步标记公开可见
    internal = context["author"]["client"].get(
        f"{API}/submissions/{context['submission_id']}"
    ).json()
    assert internal["submission"]["public_visible"] is True


def test_unknown_public_id_returns_404(world):
    _community_setup(world)
    anon = _client()
    try:
        assert anon.get(f"{API}/questions/plz-0000000000000000").status_code == 404
    finally:
        anon.close()


def test_non_author_cannot_read_submission_detail(world):
    context = _community_setup(world)
    stranger = world.user("路人用户")
    denied = stranger["client"].get(f"{API}/submissions/{context['submission_id']}")
    assert denied.status_code == 403

    reviewer = world.user("数学评审X", roles=[("reviewer", "数学")])
    allowed = reviewer["client"].get(f"{API}/submissions/{context['submission_id']}")
    assert allowed.status_code == 200, allowed.text
    body = allowed.json()["submission"]
    # 未提交自己的评审前看不到已有投票
    assert body["review_summary"]["hidden"] is True
    assert body["reviews"] == []


# ---------------------------------------------------------------------------
# 角色 / 身份 / 事件
# ---------------------------------------------------------------------------

def test_identity_apply_and_me(world):
    user = world.user("身份申请人")
    applied = user["client"].post(f"{API}/identity/apply", json={
        "kind": "teacher", "subject": "数学", "evidence": "教师资格证编号脱敏截图",
    })
    assert applied.status_code == 201, applied.text
    identity = applied.json()["identity"]
    assert identity["status"] == "pending"
    assert identity["subject"] == "数学"

    duplicate = user["client"].post(f"{API}/identity/apply", json={
        "kind": "teacher", "subject": "数学", "evidence": "重复提交",
    })
    assert duplicate.status_code == 200
    assert duplicate.json()["idempotent"] is True

    me = user["client"].get(f"{API}/me").json()
    assert me["identity"]["status"] == "pending"
    assert me["capabilities"]["submit"] is True
    assert me["capabilities"]["review"] is False

    invalid = user["client"].post(f"{API}/identity/apply", json={
        "kind": "not_a_kind", "subject": "数学", "evidence": "x",
    })
    assert invalid.status_code == 422
    anon = _client()
    try:
        assert anon.post(f"{API}/identity/apply", json={
            "kind": "teacher", "subject": "数学", "evidence": "xx",
        }).status_code == 401
    finally:
        anon.close()


def test_admin_roles_grant_revoke_and_expiry(world):
    user = world.user("待授权用户")
    granted = world.admin.post(f"{API}/admin/roles", json={
        "action": "grant", "role": "reviewer", "subject": "数学", "username": user["name"],
    })
    assert granted.status_code == 200, granted.text
    me = user["client"].get(f"{API}/me").json()
    assert me["capabilities"]["review"] is True
    assert any(role["role"] == "reviewer" and role["subject"] == "数学" for role in me["roles"])

    missing_subject = world.admin.post(f"{API}/admin/roles", json={
        "action": "grant", "role": "subject_reviewer", "username": user["name"],
    })
    assert missing_subject.status_code == 422

    # 已到期的授权不生效（admin/roles 返回时 active_roles 已过滤）
    expired = world.admin.post(f"{API}/admin/roles", json={
        "action": "grant", "role": "editor", "username": user["name"],
        "expires_at": "2020-01-01T00:00:00Z",
    })
    assert expired.status_code == 200, expired.text
    me = user["client"].get(f"{API}/me").json()
    assert not any(role["role"] == "editor" for role in me["roles"])

    revoked = world.admin.post(f"{API}/admin/roles", json={
        "action": "revoke", "role": "reviewer", "subject": "数学", "username": user["name"],
    })
    assert revoked.status_code == 200
    me = user["client"].get(f"{API}/me").json()
    assert me["capabilities"]["review"] is False

    forbidden = user["client"].post(f"{API}/admin/roles", json={
        "action": "revoke", "role": "reviewer", "username": world.admin.get("/api/auth/me").json()["username"],
    })
    assert forbidden.status_code == 403


def test_events_chain_visible_to_author_and_editor(world):
    context = _community_setup(world)
    events = context["author"]["client"].get(
        f"{API}/submissions/{context['submission_id']}/events"
    )
    assert events.status_code == 200, events.text
    items = events.json()["items"]
    actions = [item["action"] for item in items]
    assert actions[:3] == ["created", "submitted", "initial_review_accepted"]
    assert items[1]["from_status"] == "draft" and items[1]["to_status"] == "pending_initial"
    assert items[2]["from_status"] == "pending_initial"
    assert items[2]["to_status"] == "community_review"
    # 作者视图不带 actor_id（评审员/操作者身份不外露），编辑视图带审计字段
    assert "actor_id" not in items[0]
    editor_events = context["editor"]["client"].get(
        f"{API}/submissions/{context['submission_id']}/events"
    ).json()["items"]
    assert "actor_id" in editor_events[0]

    stranger = world.user("事件路人")
    assert stranger["client"].get(
        f"{API}/submissions/{context['submission_id']}/events"
    ).status_code == 403
