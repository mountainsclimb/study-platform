"""广场质量运营后端回归（Phase 3 · WS21）。

覆盖：
* 收藏：增删幂等、公开详情 favorited（匿名 false）、我的收藏投影、撤回通知收藏者；
* 通知中心：送审/初审/自动核验/发布/撤回/抽检/申诉事件钩子、未读数、标记已读、
  权限隔离、不泄露他人身份；
* 抽检：pass/issue（low/medium/high）、high 严重问题自动撤回整条链、队列
  pending/checked/passed/issues 与首批 100% 采样覆盖提示、权限与回避；
* 纠错/举报/申诉：提交、编辑端队列、resolve/reject + 理由、结果通知、幂等/校验；
* 回滚：从「上一已发布版本」恢复 active、无历史 409、回避与权限、事件/通知；
* 指标：学科题型计数、可判分率、图可用率、评审时长、抽检问题率、反馈积压、撤回数；
* 权限与审计链：编辑/admin 队列、普通用户 403、事件链可追溯、公开读与发布记录一致。
"""
import json

import pytest
from fastapi.testclient import TestClient

API = "/api/plaza"
PASSWORD = "plaza-pass-123"


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


def _register_asset(user_id: int) -> str:
    """给用户登记一张本人 PNG 资产（供公开衍生图/图可用率用例）。"""
    from io import BytesIO

    from PIL import Image

    from app.paperlab import assets

    image = Image.new("RGB", (48, 32), (30, 90, 200))
    buffer = BytesIO()
    image.save(buffer, "PNG")
    return assets.save_asset_bytes(
        int(user_id), buffer.getvalue(), media_type="image/png",
    )


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
                 stem="已知 1+1=？", answers=("A",), images=None):
        content = {
            "stem": stem,
            "options": [{"label": "A", "text": "2"}, {"label": "B", "text": "3"}],
            "answers": list(answers),
            "explanation": "基础加法。",
        }
        if images:
            content["images"] = images
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
# 流程助手
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


def _initial_review(editor, submission_id, *, version=1, decision="accept",
                    reason="", checked=True):
    return editor["client"].post(f"{API}/submissions/{submission_id}/initial-review", json={
        "version": version,
        "decision": decision,
        "reason": reason,
        "source_checked": checked,
        "privacy_checked": checked,
        "completeness_checked": checked,
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


def _publish(editor, submission_id, *, version=1):
    return editor["client"].post(f"{API}/submissions/{submission_id}/publish", json={
        "version": version, "reason": "内容核验通过，签发精选",
    })


def _withdraw_release(editor, public_id, *, reason="收到投诉，撤回复核", mode="normal"):
    return editor["client"].post(f"{API}/releases/{public_id}/withdraw", json={
        "reason": reason, "mode": mode,
    })


def _detail(user, submission_id):
    response = user["client"].get(f"{API}/submissions/{submission_id}")
    assert response.status_code == 200, response.text
    return response.json()["submission"]


def _notifications(user, **params):
    response = user["client"].get(f"{API}/notifications", params=params)
    assert response.status_code == 200, response.text
    return response.json()


def _notification_kinds(user, **params):
    return [item["kind"] for item in _notifications(user, page_size=100, **params)["items"]]


def _events(user, submission_id):
    response = user["client"].get(f"{API}/submissions/{submission_id}/events")
    assert response.status_code == 200, response.text
    return response.json()["items"]


def _event_actions(user, submission_id):
    return [item["action"] for item in _events(user, submission_id)]


def _anon():
    return _client()


def _voters(world, subject="数学", seed="评审"):
    voters = [
        world.user(f"{seed}复核{subject}{i}", roles=[("subject_reviewer", subject)])
        for i in range(2)
    ]
    voters += [
        world.user(f"{seed}评审{subject}{i}", roles=[("reviewer", subject)])
        for i in range(3)
    ]
    return voters


def _community(world, *, subject="数学", images=None, author=None, editor=None,
               seed="社区"):
    author = author or world.user(f"{seed}作者-{subject}")
    if editor is None:
        editor = world.user(f"{seed}编辑-{subject}", roles=[("editor", "")])
    question_id = world.question(author, subject=subject, images=images)
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
        "subject": subject,
    }


def _verify(world, context, *, seed="评审", version=1):
    voters = _voters(world, context.get("subject", "数学"), seed=seed)
    for reviewer in voters:
        response = _review(reviewer, context["submission_id"], version=version)
        assert response.status_code == 201, response.text
    detail = _detail(context["author"], context["submission_id"])
    assert detail["status"] == "verified", detail
    context["voters"] = voters
    context["public_id"] = detail["public_id"]
    return context


def _published(world, *, subject="数学", images=None, author=None, editor=None):
    context = _community(
        world, subject=subject, images=images, author=author, editor=editor,
    )
    _verify(world, context)
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


# ---------------------------------------------------------------------------
# 一、收藏
# ---------------------------------------------------------------------------

def test_favorite_add_remove_idempotent_and_public_detail(world):
    context = _published(world)
    reader = world.user("收藏读者")
    public_id = context["public_id"]

    first = reader["client"].post(f"{API}/questions/{public_id}/favorite")
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["favorited"] is True
    assert body["idempotent"] is False
    assert body["favorite_id"] >= 1
    assert body["favorite_count"] == 1

    again = reader["client"].post(f"{API}/questions/{public_id}/favorite")
    assert again.status_code == 200
    assert again.json()["idempotent"] is True
    assert again.json()["favorite_count"] == 1
    assert _db_rows(
        "SELECT COUNT(*) AS n FROM plaza_favorites WHERE submission_id=?",
        (context["submission_id"],),
    )[0]["n"] == 1

    # 公开详情：登录用户 favorited=true，匿名 false，只给聚合收藏数
    mine = reader["client"].get(f"{API}/questions/{public_id}").json()
    assert mine["favorited"] is True
    assert mine["favorite_count"] == 1
    anon = _anon()
    try:
        public = anon.get(f"{API}/questions/{public_id}").json()
        assert public["favorited"] is False
        assert public["favorite_count"] == 1
    finally:
        anon.close()

    removed = reader["client"].delete(f"{API}/questions/{public_id}/favorite")
    assert removed.status_code == 200
    assert removed.json()["favorited"] is False
    assert removed.json()["idempotent"] is False
    missing = reader["client"].delete(f"{API}/questions/{public_id}/favorite")
    assert missing.status_code == 200
    assert missing.json()["idempotent"] is True
    detail = reader["client"].get(f"{API}/questions/{public_id}").json()
    assert detail["favorited"] is False
    assert detail["favorite_count"] == 0


def test_favorite_requires_login_and_visible_question(world):
    context = _community(world)
    public_id = context["public_id"]
    anon = _anon()
    try:
        assert anon.post(f"{API}/questions/{public_id}/favorite").status_code == 401
        assert anon.delete(f"{API}/questions/{public_id}/favorite").status_code == 401
    finally:
        anon.close()
    reader = world.user("收藏权限读者")
    unknown = reader["client"].post(f"{API}/questions/plz-not-exists/favorite")
    assert unknown.status_code == 404
    # 社区待核验可收藏（公开可见）
    assert reader["client"].post(
        f"{API}/questions/{public_id}/favorite"
    ).status_code == 200

    # 作者撤回后不可再收藏，但已收藏可取消、列表保留状态提示
    assert context["author"]["client"].post(
        f"{API}/submissions/{context['submission_id']}/withdraw",
        json={"reason": "作者先撤回，稍后修订"},
    ).status_code == 200
    blocked = reader["client"].post(f"{API}/questions/{public_id}/favorite")
    assert blocked.status_code == 409
    favorites = reader["client"].get(f"{API}/favorites").json()
    assert favorites["total"] == 1
    item = favorites["items"][0]
    assert item["available"] is False
    assert item["status"] == "withdrawn"
    assert item["public_id"] == public_id
    assert "撤回" in item["notice"]
    assert reader["client"].delete(
        f"{API}/questions/{public_id}/favorite"
    ).status_code == 200
    assert reader["client"].get(f"{API}/favorites").json()["total"] == 0


def test_favorite_withdraw_notifies_favoriters_and_author(world):
    context = _published(world)
    favoriter = world.user("撤回通知收藏者")
    assert favoriter["client"].post(
        f"{API}/questions/{context['public_id']}/favorite"
    ).status_code == 200

    withdrawn = _withdraw_release(
        context["editor"], context["public_id"], reason="版权投诉，紧急隐藏",
        mode="emergency",
    )
    assert withdrawn.status_code == 200, withdrawn.text
    assert withdrawn.json()["notified"]["author"] is True

    favoriter_kinds = _notification_kinds(favoriter)
    assert "favorite_withdrawn" in favoriter_kinds
    author_kinds = _notification_kinds(context["author"])
    assert "release_withdrawn" in author_kinds
    # 收藏者通知不泄露作者/编辑身份
    text = json.dumps(_notifications(favoriter), ensure_ascii=False)
    assert context["author"]["name"] not in text
    assert context["editor"]["name"] not in text
    assert "author_id" not in text


def test_author_withdraw_notifies_favoriters(world):
    context = _community(world)
    favoriter = world.user("作者撤回通知收藏者")
    assert favoriter["client"].post(
        f"{API}/questions/{context['public_id']}/favorite"
    ).status_code == 200
    withdrawn = context["author"]["client"].post(
        f"{API}/submissions/{context['submission_id']}/withdraw",
        json={"reason": "作者发现解析有误，先撤回"},
    )
    assert withdrawn.status_code == 200, withdrawn.text
    assert "favorite_withdrawn" in _notification_kinds(favoriter)
    # 作者撤回事件仍在事件链中，公开列表立即不可见
    assert _event_actions(
        context["author"], context["submission_id"],
    )[-1] == "author_withdrawn"
    anon = _anon()
    try:
        assert anon.get(f"{API}/questions").json()["total"] == 0
    finally:
        anon.close()


# ---------------------------------------------------------------------------
# 二、通知中心
# ---------------------------------------------------------------------------

def test_notifications_lifecycle_unread_and_read(world):
    context = _community(world)
    author = context["author"]
    listing = _notifications(author)
    assert listing["total"] >= 1
    assert listing["unread_count"] >= 1
    kinds = [item["kind"] for item in listing["items"]]
    assert "submission_submitted" in kinds
    assert "initial_review_approved" in kinds
    item = listing["items"][0]
    for key in ("id", "kind", "title", "body", "link", "read", "read_at",
                "created_at", "payload"):
        assert key in item
    assert item["read"] is False and item["read_at"] is None
    assert item["link"].startswith("#/")

    unread_only = _notifications(author, unread_only=True)
    assert unread_only["total"] == listing["unread_count"]
    assert all(entry["read"] is False for entry in unread_only["items"])

    mark = author["client"].post(f"{API}/notifications/read", json={
        "ids": [item["id"]],
    })
    assert mark.status_code == 200, mark.text
    assert mark.json()["updated"] == 1
    assert mark.json()["unread_count"] == listing["unread_count"] - 1
    repeat = author["client"].post(f"{API}/notifications/read", json={
        "ids": [item["id"]],
    })
    assert repeat.json()["updated"] == 0

    mark_all = author["client"].post(f"{API}/notifications/read", json={"all": True})
    assert mark_all.status_code == 200
    assert mark_all.json()["unread_count"] == 0
    assert _notifications(author, unread_only=True)["total"] == 0


def test_notifications_read_validation_and_ownership(world):
    context = _community(world)
    author = context["author"]
    other = world.user("通知越权用户")
    target = _notifications(author)["items"][0]
    # 他人 id 被忽略（updated=0，不泄露存在性），作者未读不变
    before = _notifications(author)["unread_count"]
    response = other["client"].post(f"{API}/notifications/read", json={
        "ids": [target["id"]],
    })
    assert response.status_code == 200
    assert response.json()["updated"] == 0
    assert _notifications(author)["unread_count"] == before
    # 参数缺失 422
    assert other["client"].post(f"{API}/notifications/read", json={}).status_code == 422
    assert other["client"].post(
        f"{API}/notifications/read", json={"ids": []},
    ).status_code == 422


def test_notifications_permissions_and_no_identity_leak(world):
    anon = _anon()
    try:
        assert anon.get(f"{API}/notifications").status_code == 401
        assert anon.post(f"{API}/notifications/read", json={"all": True}).status_code == 401
    finally:
        anon.close()
    context = _published(world)
    author = context["author"]
    me = author["client"].get(f"{API}/me").json()
    assert me["capabilities"]["notifications"] is True
    assert me["unread_notifications"] == me["stats"]["unread_notifications"]
    assert me["unread_notifications"] >= 1
    # 作者视角通知/事件链不出现评审员与编辑身份
    text = json.dumps(_notifications(author), ensure_ascii=False)
    assert context["editor"]["name"] not in text
    for reviewer in context["voters"]:
        assert reviewer["name"] not in text
    assert "reviewer_id" not in text


def test_notification_hooks_on_review_flow(world):
    author = world.user("通知钩子作者")
    editor = world.user("通知钩子编辑", roles=[("editor", "")])
    question_id = world.question(author)
    submission_id = _create_submission(author, question_id)
    assert _submit(author, submission_id).status_code == 200
    assert "submission_submitted" in _notification_kinds(author)
    assert _initial_review(editor, submission_id).status_code == 200
    assert "initial_review_approved" in _notification_kinds(author)
    context = {
        "author": author, "editor": editor, "question_id": question_id,
        "submission_id": submission_id, "public_id": _detail(
            author, submission_id,
        )["public_id"], "subject": "数学",
    }
    _verify(world, context)
    assert "submission_verified" in _notification_kinds(author)
    assert _publish(editor, submission_id).status_code == 200
    assert "submission_published" in _notification_kinds(author)

    # 初审驳回也会通知作者
    author2 = world.user("通知驳回作者")
    question2 = world.question(author2)
    submission2 = _create_submission(author2, question2)
    assert _submit(author2, submission2).status_code == 200
    rejected = _initial_review(
        editor, submission2, decision="reject", reason="来源链接无法核验，请补充出处",
    )
    assert rejected.status_code == 200, rejected.text
    assert "initial_review_rejected" in _notification_kinds(author2)


# ---------------------------------------------------------------------------
# 三、质量抽检
# ---------------------------------------------------------------------------

def test_quality_queue_pending_then_pass(world):
    context = _published(world)
    queue = context["editor"]["client"].get(f"{API}/admin/quality/queue")
    assert queue.status_code == 200, queue.text
    body = queue.json()
    assert body["counts"]["pending"] == 1
    assert body["counts"]["checked"] == 0
    assert body["sampling"]["mode"] == "initial_100"
    assert body["sampling"]["required"] is True
    assert body["sampling"]["coverage_rate"] == 0.0
    item = body["items"][0]
    assert item["submission_id"] == context["submission_id"]
    assert item["public_id"] == context["public_id"]
    assert item["check_status"] == "pending"
    assert item["latest_check"] is None
    assert item["auto_withdrawn"] is False

    checked = context["editor"]["client"].post(f"{API}/admin/quality/checks", json={
        "submission_id": context["submission_id"],
        "decision": "pass",
        "note": "题干/答案/解析与来源均核验通过",
    })
    assert checked.status_code == 201, checked.text
    check = checked.json()["check"]
    assert check["decision"] == "pass"
    assert check["severity"] == ""
    assert check["version"] == 1
    assert check["checker_display"].startswith("抽检员·")
    assert checked.json()["auto_withdrawn"] is False
    assert checked.json()["withdraw"] is None

    passed = context["editor"]["client"].get(
        f"{API}/admin/quality/queue?status=passed"
    ).json()
    assert passed["total"] == 1
    assert passed["counts"] == {
        "pending": 0, "passed": 1, "issues": 0, "checked": 1, "all": 1,
    }
    assert passed["sampling"]["coverage_rate"] == 1.0
    # 抽检不改变发布状态
    anon = _anon()
    try:
        assert anon.get(f"{API}/questions/{context['public_id']}").json()["visible"] is True
    finally:
        anon.close()


def test_quality_issue_medium_notifies_author_without_withdraw(world):
    context = _published(world)
    checked = context["editor"]["client"].post(f"{API}/admin/quality/checks", json={
        "submission_id": context["submission_id"],
        "decision": "issue",
        "severity": "medium",
        "findings": ["解析步骤跳步，易误导学生"],
    })
    assert checked.status_code == 201, checked.text
    body = checked.json()
    assert body["check"]["findings"] == ["解析步骤跳步，易误导学生"]
    assert body["check"]["severity_label"] == "中"
    assert body["auto_withdrawn"] is False
    assert body["notified"]["author"] is True
    assert _detail(context["author"], context["submission_id"])["status"] == "published"
    assert "quality_issue" in _notification_kinds(context["author"])
    issues = context["editor"]["client"].get(
        f"{API}/admin/quality/queue?status=issues"
    ).json()
    assert issues["total"] == 1
    assert issues["items"][0]["check_status"] == "issue"
    assert issues["items"][0]["latest_check"]["severity"] == "medium"


def test_quality_high_auto_withdraw_notifies_author_and_favoriters(world):
    context = _published(world)
    favoriter = world.user("抽检撤回收藏者")
    assert favoriter["client"].post(
        f"{API}/questions/{context['public_id']}/favorite"
    ).status_code == 200

    checked = context["editor"]["client"].post(f"{API}/admin/quality/checks", json={
        "submission_id": context["submission_id"],
        "decision": "issue",
        "severity": "high",
        "findings": ["答案错误，正确应为 B", "题干条件缺失"],
        "note": "严重错题，停止公开",
    })
    assert checked.status_code == 201, checked.text
    body = checked.json()
    assert body["auto_withdrawn"] is True
    assert body["withdraw"]["mode"] == "emergency"
    assert body["withdraw"]["status"] == "withdrawn"
    assert body["submission"]["status"] == "withdrawn"
    assert body["submission"]["public_visible"] is False
    assert body["check"]["auto_withdrawn"] is True
    assert "自动撤回" in body["message"]

    # 公开读与 admin 发布记录一致：列表消失、旧链接清晰撤回态、无 active release
    anon = _anon()
    try:
        assert anon.get(f"{API}/questions").json()["total"] == 0
        public = anon.get(f"{API}/questions/{context['public_id']}").json()
        assert public["visible"] is False
        assert public["status"] == "withdrawn"
    finally:
        anon.close()
    releases = context["editor"]["client"].get(f"{API}/releases").json()
    assert [item["status"] for item in releases["items"]] == ["withdrawn"]
    assert "quality_withdrawn" in _notification_kinds(context["author"])
    assert "favorite_withdrawn" in _notification_kinds(favoriter)
    actions = _event_actions(context["author"], context["submission_id"])
    assert "quality_issue" in actions
    assert "quality_withdrawn" in actions


def test_quality_check_validation_permissions_and_avoidance(world):
    context = _published(world)
    submission_id = context["submission_id"]
    editor = context["editor"]

    anon = _anon()
    try:
        assert anon.post(f"{API}/admin/quality/checks", json={
            "submission_id": submission_id, "decision": "pass",
        }).status_code == 401
        assert anon.get(f"{API}/admin/quality/queue").status_code == 401
    finally:
        anon.close()

    reviewer = world.user("抽检越权评审", roles=[("reviewer", "数学")])
    student = world.user("抽检越权学生")
    for user in (reviewer, student):
        assert user["client"].post(f"{API}/admin/quality/checks", json={
            "submission_id": submission_id, "decision": "pass",
        }).status_code == 403
        assert user["client"].get(f"{API}/admin/quality/queue").status_code == 403

    # 参数校验
    assert editor["client"].post(f"{API}/admin/quality/checks", json={
        "submission_id": submission_id, "decision": "maybe",
    }).status_code == 422
    assert editor["client"].post(f"{API}/admin/quality/checks", json={
        "submission_id": submission_id, "decision": "issue", "severity": "high",
    }).status_code == 422
    assert editor["client"].post(f"{API}/admin/quality/checks", json={
        "decision": "pass",
    }).status_code == 422
    assert editor["client"].post(f"{API}/admin/quality/checks", json={
        "submission_id": 999999, "decision": "pass",
    }).status_code == 404
    # 非发布状态不可抽检
    community = _community(world)
    assert editor["client"].post(f"{API}/admin/quality/checks", json={
        "submission_id": community["submission_id"], "decision": "pass",
    }).status_code == 409

    # 编辑回避：不能抽检本人投稿（另一编辑发布）
    author_editor = world.user("兼作者编辑", roles=[("editor", "")])
    publisher = world.user("独立发布编辑", roles=[("editor", "")])
    own_context = _published(world, author=author_editor, editor=publisher)
    avoided = author_editor["client"].post(f"{API}/admin/quality/checks", json={
        "submission_id": own_context["submission_id"], "decision": "pass",
    })
    assert avoided.status_code == 403
    assert "回避" in avoided.json()["detail"]


def test_quality_check_by_public_id_and_mismatch(world):
    first = _published(world)
    second = _published(world)
    response = first["editor"]["client"].post(f"{API}/admin/quality/checks", json={
        "public_id": first["public_id"], "decision": "pass",
    })
    assert response.status_code == 201, response.text
    assert response.json()["check"]["public_id"] == first["public_id"]
    mismatch = first["editor"]["client"].post(f"{API}/admin/quality/checks", json={
        "submission_id": first["submission_id"],
        "public_id": second["public_id"],
        "decision": "pass",
    })
    assert mismatch.status_code == 422


# ---------------------------------------------------------------------------
# 四、纠错 / 举报 / 申诉
# ---------------------------------------------------------------------------

def test_issue_submit_and_resolve_flow(world):
    context = _published(world)
    reporter = world.user("纠错反馈人")
    created = reporter["client"].post(
        f"{API}/questions/{context['public_id']}/issues",
        json={"field": "answer", "description": "参考答案应该是 B，不是 A。"},
    )
    assert created.status_code == 201, created.text
    issue = created.json()["issue"]
    assert issue["status"] == "pending"
    assert issue["field"] == "answer"
    assert issue["public_id"] == context["public_id"]

    queue = context["editor"]["client"].get(f"{API}/admin/issues").json()
    assert queue["total"] == 1
    assert queue["counts"]["pending"] == 1
    item = queue["items"][0]
    assert item["reporter_display"].startswith("反馈者·")
    assert item["description"].startswith("参考答案")
    assert item["question"]["public_id"] == context["public_id"]
    assert "user_id" not in item

    resolved = context["editor"]["client"].patch(
        f"{API}/admin/issues/{issue['id']}",
        json={"action": "resolve", "reason": "已核对，答案应为 B，已通知作者修订"},
    )
    assert resolved.status_code == 200, resolved.text
    body = resolved.json()
    assert body["issue"]["status"] == "resolved"
    assert "已处理" in body["issue"]["status_label"]
    assert body["issue"]["resolution"].startswith("已核对")
    assert "issue_resolved" in _notification_kinds(reporter)
    assert "issue_resolved" in _event_actions(context["author"], context["submission_id"])
    # 重复处理 409，队列计数更新
    again = context["editor"]["client"].patch(
        f"{API}/admin/issues/{issue['id']}", json={"action": "reject", "reason": "重复"},
    )
    assert again.status_code == 409
    assert context["editor"]["client"].get(
        f"{API}/admin/issues?status=resolved"
    ).json()["total"] == 1


def test_issue_validation_permissions_and_report_flow(world):
    context = _published(world)
    public_id = context["public_id"]
    anon = _anon()
    try:
        assert anon.post(f"{API}/questions/{public_id}/issues", json={
            "field": "stem", "description": "题干缺条件",
        }).status_code == 401
        assert anon.post(f"{API}/questions/{public_id}/reports", json={
            "reason": "copyright", "detail": "疑似未授权",
        }).status_code == 401
    finally:
        anon.close()
    reporter = world.user("反馈校验用户")
    assert reporter["client"].post(f"{API}/questions/{public_id}/issues", json={
        "field": "wrong_field", "description": "字段非法",
    }).status_code == 422
    assert reporter["client"].post(f"{API}/questions/{public_id}/issues", json={
        "field": "stem", "description": "x",
    }).status_code == 422
    assert reporter["client"].post(f"{API}/questions/plz-missing/issues", json={
        "field": "stem", "description": "不存在的题",
    }).status_code == 404

    # 举报：非法原因 422，创建后可驳回
    assert reporter["client"].post(f"{API}/questions/{public_id}/reports", json={
        "reason": "not_a_reason", "detail": "非法原因",
    }).status_code == 422
    report = reporter["client"].post(f"{API}/questions/{public_id}/reports", json={
        "reason": "copyright", "detail": "疑似未授权转载，请核对来源与许可。",
    })
    assert report.status_code == 201, report.text
    report_id = report.json()["report"]["id"]
    assert report.json()["report"]["reason"] == "copyright"
    queue = context["editor"]["client"].get(f"{API}/admin/reports").json()
    assert queue["total"] == 1
    assert queue["items"][0]["detail"].startswith("疑似未授权")
    rejected = context["editor"]["client"].patch(
        f"{API}/admin/reports/{report_id}",
        json={"action": "reject", "reason": "来源与授权材料齐全，举报不成立"},
    )
    assert rejected.status_code == 200, rejected.text
    assert rejected.json()["report"]["status"] == "rejected"
    assert "report_rejected" in _notification_kinds(reporter)
    # 驳回必须给原因
    reporter2 = world.user("举报驳回校验")
    report2 = reporter2["client"].post(f"{API}/questions/{public_id}/reports", json={
        "reason": "spam",
    }).json()["report"]
    no_reason = context["editor"]["client"].patch(
        f"{API}/admin/reports/{report2['id']}", json={"action": "reject"},
    )
    assert no_reason.status_code == 422

    # 编辑端队列对普通用户/评审员 403
    reviewer = world.user("反馈越权评审", roles=[("reviewer", "数学")])
    outsider = world.user("反馈越权用户")
    for user in (reviewer, outsider):
        assert user["client"].get(f"{API}/admin/issues").status_code == 403
        assert user["client"].get(f"{API}/admin/reports").status_code == 403
        assert user["client"].get(f"{API}/admin/appeals").status_code == 403


def test_appeal_flow_after_initial_reject(world):
    author = world.user("申诉作者")
    editor = world.user("申诉编辑", roles=[("editor", "")])
    question_id = world.question(author)
    submission_id = _create_submission(author, question_id)
    assert _submit(author, submission_id).status_code == 200
    rejected = _initial_review(
        editor, submission_id, decision="reject", reason="来源信息不足，请补充可核验出处",
    )
    assert rejected.status_code == 200, rejected.text

    created = author["client"].post(f"{API}/submissions/{submission_id}/appeals", json={
        "reason": "来源出处已在第 12 页题号 3，请复核；许可依据也已补充说明。",
    })
    assert created.status_code == 201, created.text
    appeal = created.json()["appeal"]
    assert appeal["status"] == "pending"
    assert appeal["submission_id"] == submission_id
    assert "user_id" not in appeal

    duplicate = author["client"].post(f"{API}/submissions/{submission_id}/appeals", json={
        "reason": "补充说明，仍在等待处理",
    })
    assert duplicate.status_code == 200
    assert duplicate.json()["idempotent"] is True
    assert duplicate.json()["appeal"]["id"] == appeal["id"]

    queue = editor["client"].get(f"{API}/admin/appeals").json()
    assert queue["total"] == 1
    item = queue["items"][0]
    assert item["author_display"].startswith("申诉作者·")
    assert "user_id" not in item

    resolved = editor["client"].patch(
        f"{API}/admin/appeals/{appeal['id']}",
        json={"action": "resolve", "reason": "已复核来源，恢复作者修订权限"},
    )
    assert resolved.status_code == 200, resolved.text
    assert resolved.json()["appeal"]["status"] == "resolved"
    assert "appeal_resolved" in _notification_kinds(author)
    detail = _detail(author, submission_id)
    assert detail["can"]["appeal"] is True
    assert detail["my_appeal"]["status"] == "resolved"
    assert len(detail["appeals"]) == 1
    mine = author["client"].get(f"{API}/submissions/{submission_id}/appeals").json()
    assert mine["total"] == 1
    # 事件链与审计：申诉提交/处理节点
    actions = _event_actions(author, submission_id)
    assert "appeal_submitted" in actions
    assert "appeal_resolved" in actions


def test_appeal_validation_and_permissions(world):
    author = world.user("申诉校验作者")
    editor = world.user("申诉校验编辑", roles=[("editor", "")])
    question_id = world.question(author)
    submission_id = _create_submission(author, question_id)

    # 草稿不可申诉
    draft = author["client"].post(f"{API}/submissions/{submission_id}/appeals", json={
        "reason": "我要求复核这份草稿",
    })
    assert draft.status_code == 409
    # 理由过短/非作者/不存在
    assert author["client"].post(f"{API}/submissions/{submission_id}/appeals", json={
        "reason": "x",
    }).status_code == 422
    stranger = world.user("申诉他人越权")
    assert stranger["client"].post(f"{API}/submissions/{submission_id}/appeals", json={
        "reason": "我不是作者但想申诉",
    }).status_code == 404
    assert author["client"].post(f"{API}/submissions/999999/appeals", json={
        "reason": "不存在的投稿",
    }).status_code == 404
    assert author["client"].get(
        f"{API}/submissions/{submission_id}/appeals"
    ).json()["total"] == 0
    assert stranger["client"].get(
        f"{API}/submissions/{submission_id}/appeals"
    ).status_code == 404
    # 驳回必须给理由；已处理不可重复
    assert _submit(author, submission_id).status_code == 200
    assert _initial_review(
        editor, submission_id, decision="reject", reason="内容不完整，请补充",
    ).status_code == 200
    appeal_id = author["client"].post(
        f"{API}/submissions/{submission_id}/appeals", json={"reason": "已补充完整内容，请复核"},
    ).json()["appeal"]["id"]
    no_reason = editor["client"].patch(
        f"{API}/admin/appeals/{appeal_id}", json={"action": "reject"},
    )
    assert no_reason.status_code == 422
    rejected = editor["client"].patch(
        f"{API}/admin/appeals/{appeal_id}",
        json={"action": "reject", "reason": "补充内容仍不完整，维持初审结论"},
    )
    assert rejected.status_code == 200
    assert "appeal_rejected" in _notification_kinds(author)
    assert editor["client"].patch(
        f"{API}/admin/appeals/{appeal_id}",
        json={"action": "resolve", "reason": "重复处理"},
    ).status_code == 409


# ---------------------------------------------------------------------------
# 五、回滚与撤回传播
# ---------------------------------------------------------------------------

def _publish_second_version(world, context):
    """已发布 v1 撤回后，作者修订 → 复审 → 再发布 v2（用于回滚）。"""
    editor = context["editor"]
    author = context["author"]
    submission_id = context["submission_id"]
    assert _withdraw_release(
        editor, context["public_id"], reason="修订后重新发布", mode="normal",
    ).status_code == 200
    _edit_question(author, context["question_id"], stem="已知 2+3=？", answers=("B",))
    revised = author["client"].post(f"{API}/submissions/{submission_id}/revise", json={
        "version": 1,
    })
    assert revised.status_code == 200, revised.text
    version = revised.json()["submission"]["current_version"]
    assert version == 2
    accepted = _initial_review(editor, submission_id, version=version)
    assert accepted.status_code == 200, accepted.text
    for reviewer in context["voters"]:
        response = _review(reviewer, submission_id, version=version)
        assert response.status_code == 201, response.text
    published = _publish(editor, submission_id, version=version)
    assert published.status_code == 200, published.text
    return published.json()


def test_rollback_restores_previous_release(world):
    context = _published(world)
    favoriter = world.user("回滚收藏者")
    assert favoriter["client"].post(
        f"{API}/questions/{context['public_id']}/favorite"
    ).status_code == 200
    second = _publish_second_version(world, context)
    assert second["release"]["version"] == 2
    anon = _anon()
    try:
        assert "2+3" in anon.get(
            f"{API}/questions/{context['public_id']}"
        ).json()["content"]["stem"]
    finally:
        anon.close()

    rolled = context["editor"]["client"].post(
        f"{API}/submissions/{context['submission_id']}/rollback",
        json={"reason": "新版解析仍有争议，回滚到上一已发布版本"},
    )
    assert rolled.status_code == 200, rolled.text
    body = rolled.json()
    assert body["restored_version"] == 1
    assert body["submission"]["status"] == "published"
    assert body["submission"]["version"] == 1
    assert body["submission"]["public_id"] == context["public_id"]
    assert body["release"]["status"] == "active"
    assert body["release"]["version"] == 1
    assert body["withdrawn_release"]["version"] == 2
    assert body["withdrawn_release"]["mode"] == "rollback"
    assert body["notified"]["author"] is True

    # 公开读恢复为 v1 内容；发布记录一致：active=v1、withdrawn=v2
    anon = _anon()
    try:
        public = anon.get(f"{API}/questions/{context['public_id']}").json()
        assert public["status"] == "published"
        assert public["content"]["stem"] == "已知 1+1=？"
        assert anon.get(f"{API}/questions").json()["total"] == 1
    finally:
        anon.close()
    releases = context["editor"]["client"].get(f"{API}/releases").json()["items"]
    active = [item for item in releases if item["status"] == "active"]
    withdrawn = [item for item in releases if item["status"] == "withdrawn"]
    assert len(active) == 1 and active[0]["version"] == 1
    assert [item["version"] for item in withdrawn] == [2]
    assert active[0]["submission_id"] == context["submission_id"]
    actions = _event_actions(context["author"], context["submission_id"])
    assert "release_rolled_back" in actions
    assert "release_rolled_back" in _notification_kinds(context["author"])
    assert "favorite_restored" in _notification_kinds(favoriter)
    # 作者仍能看到被回滚版本的快照留档
    versions = _db_rows(
        "SELECT version FROM plaza_submission_versions WHERE submission_id=? ORDER BY version",
        (context["submission_id"],),
    )
    assert [row["version"] for row in versions] == [1, 2]


def test_rollback_requires_history_permissions_and_avoidance(world):
    context = _published(world)
    submission_id = context["submission_id"]
    editor = context["editor"]

    no_history = editor["client"].post(f"{API}/submissions/{submission_id}/rollback", json={
        "reason": "首次发布没有历史版本，应拒绝回滚",
    })
    assert no_history.status_code == 409
    assert "历史发布" in no_history.json()["detail"]["message"]

    reviewer = world.user("回滚越权评审", roles=[("reviewer", "数学")])
    assert reviewer["client"].post(
        f"{API}/submissions/{submission_id}/rollback", json={"reason": "越权回滚"},
    ).status_code == 403
    anon = _anon()
    try:
        assert anon.post(f"{API}/submissions/{submission_id}/rollback", json={
            "reason": "匿名回滚",
        }).status_code == 401
    finally:
        anon.close()
    assert editor["client"].post(
        f"{API}/submissions/999999/rollback", json={"reason": "不存在的投稿"},
    ).status_code == 404
    # 理由过短由 schema 校验拒绝
    assert editor["client"].post(
        f"{API}/submissions/{submission_id}/rollback", json={"reason": "x"},
    ).status_code == 422

    # 编辑回避本人投稿：管理员作为作者、独立编辑发布后，管理员回滚应 403
    admin_me = world.admin.get("/api/auth/me").json()
    admin_user = {"client": world.admin, "id": admin_me["id"], "name": admin_me["username"]}
    publisher = world.user("回避回滚发布编辑", roles=[("editor", "")])
    own = _published(world, author=admin_user, editor=publisher)
    assert _withdraw_release(own["editor"], own["public_id"]).status_code == 200
    _edit_question(admin_user, own["question_id"], stem="已知 5+5=？", answers=("A",))
    version = admin_user["client"].post(
        f"{API}/submissions/{own['submission_id']}/revise", json={"version": 1},
    ).json()["submission"]["current_version"]
    assert _initial_review(publisher, own["submission_id"], version=version).status_code == 200
    for reviewer in own["voters"]:
        assert _review(reviewer, own["submission_id"], version=version).status_code == 201
    assert _publish(publisher, own["submission_id"], version=version).status_code == 200
    avoided = world.admin.post(
        f"{API}/submissions/{own['submission_id']}/rollback",
        json={"reason": "管理员回滚本人投稿"},
    )
    assert avoided.status_code == 403
    assert "回避" in avoided.json()["detail"]


def test_withdraw_consistency_between_public_read_and_releases(world):
    context = _published(world)
    actions_before = _event_actions(context["author"], context["submission_id"])
    assert "published" in actions_before
    withdrawn = _withdraw_release(
        context["editor"], context["public_id"],
        reason="来源权利待确认，先下架复核", mode="normal",
    )
    assert withdrawn.status_code == 200
    anon = _anon()
    try:
        assert anon.get(f"{API}/questions").json()["total"] == 0
        detail = anon.get(f"{API}/questions/{context['public_id']}").json()
        assert detail["visible"] is False
        assert detail["status"] == "withdrawn"
    finally:
        anon.close()
    releases = context["editor"]["client"].get(f"{API}/releases").json()["items"]
    assert [item["status"] for item in releases] == ["withdrawn"]
    events = _events(context["author"], context["submission_id"])
    assert events[-1]["action"] == "release_withdrawn"
    assert events[-1]["from_status"] == "published"
    assert events[-1]["to_status"] == "withdrawn"
    # 撤回后重复撤回幂等
    again = _withdraw_release(context["editor"], context["public_id"])
    assert again.status_code == 200
    assert again.json()["idempotent"] is True


# ---------------------------------------------------------------------------
# 六、质量指标
# ---------------------------------------------------------------------------

def test_metrics_permissions_and_core_fields(world):
    anon = _anon()
    try:
        assert anon.get(f"{API}/admin/metrics").status_code == 401
    finally:
        anon.close()
    context = _published(world)
    reviewer = world.user("指标越权评审", roles=[("reviewer", "数学")])
    assert reviewer["client"].get(f"{API}/admin/metrics").status_code == 403
    student = world.user("指标越权学生")
    assert student["client"].get(f"{API}/admin/metrics").status_code == 403

    metrics = context["editor"]["client"].get(f"{API}/admin/metrics")
    assert metrics.status_code == 200, metrics.text
    body = metrics.json()
    for key in ("generated_at", "totals", "by_subject", "by_type", "scorable",
                "figures", "review_duration", "quality", "feedback",
                "withdrawals", "caliber"):
        assert key in body, key
    assert body["totals"]["submissions"] == 1
    assert body["totals"]["published"] == 1
    assert body["totals"]["public_visible"] == 1
    assert body["by_subject"][0]["subject"] == "数学"
    assert body["by_subject"][0]["published"] == 1
    assert body["by_type"][0]["type"] == "SINGLE"
    assert body["scorable"]["total"] == 1
    assert body["scorable"]["scorable"] == 1
    assert body["scorable"]["rate"] == 1.0
    assert body["review_duration"]["samples"] >= 1
    assert body["review_duration"]["median_hours"] is not None
    assert body["quality"]["checks_total"] == 0
    assert body["quality"]["sampling"]["pending_total"] == 1
    assert body["feedback"]["issues_unresolved"] == 0
    assert body["withdrawals"]["rollbacks"] == 0
    assert isinstance(body["caliber"], dict) and body["caliber"]


def test_metrics_scorable_figures_review_and_quality(world):
    # 有图题：可判分 + 图可用率 100%
    author = world.user("指标图作者")
    asset_id = _register_asset(author["id"])
    context = _published(
        world, author=author, images=[{"asset": asset_id, "role": "stem"}],
    )
    metrics = context["editor"]["client"].get(f"{API}/admin/metrics").json()
    assert metrics["figures"]["total_images"] == 1
    assert metrics["figures"]["public_images"] == 1
    assert metrics["figures"]["coverage_rate"] == 1.0
    assert metrics["quality"]["checks_total"] == 0
    assert metrics["quality"]["sampling"]["coverage_rate"] == 0.0
    assert metrics["quality"]["sampling"]["pending_total"] == 1

    favoriter = world.user("指标收藏者")
    assert favoriter["client"].post(
        f"{API}/questions/{context['public_id']}/favorite"
    ).status_code == 200
    checked = context["editor"]["client"].post(f"{API}/admin/quality/checks", json={
        "submission_id": context["submission_id"], "decision": "issue",
        "severity": "high", "findings": ["配图与题干不符"],
    })
    assert checked.status_code == 201

    metrics = context["editor"]["client"].get(f"{API}/admin/metrics").json()
    assert metrics["quality"]["checks_total"] == 1
    assert metrics["quality"]["issues"] == 1
    assert metrics["quality"]["high_issues"] == 1
    assert metrics["quality"]["issue_rate"] == 1.0
    assert metrics["quality"]["high_issue_rate"] == 1.0
    assert metrics["quality"]["sampling"]["coverage_rate"] == 1.0
    assert metrics["withdrawals"]["releases_withdrawn"] == 1
    assert metrics["withdrawals"]["submissions_withdrawn"] == 1
    # 已抽检题被自动撤回后：不再有待抽检
    assert metrics["quality"]["sampling"]["pending_total"] == 0
    assert metrics["totals"]["published"] == 0


def test_metrics_feedback_and_withdrawal_counts(world):
    context = _published(world)
    reporter = world.user("指标反馈人")
    issue = reporter["client"].post(
        f"{API}/questions/{context['public_id']}/issues",
        json={"field": "stem", "description": "题干单位缺失，请核对"},
    ).json()["issue"]
    reporter2 = world.user("指标举报人")
    reporter2["client"].post(
        f"{API}/questions/{context['public_id']}/reports",
        json={"reason": "answer_error", "detail": "答案 A 似乎不对"},
    )
    # 作者申诉：先制造 rejected 状态
    other = world.user("指标申诉作者")
    editor = context["editor"]
    question2 = world.question(other)
    submission2 = _create_submission(other, question2)
    assert _submit(other, submission2).status_code == 200
    assert _initial_review(
        editor, submission2, decision="reject", reason="来源不明，请补充",
    ).status_code == 200
    assert other["client"].post(
        f"{API}/submissions/{submission2}/appeals",
        json={"reason": "来源已补充，请复核"},
    ).status_code == 201

    metrics = editor["client"].get(f"{API}/admin/metrics").json()
    assert metrics["feedback"]["issues_total"] == 1
    assert metrics["feedback"]["reports_total"] == 1
    assert metrics["feedback"]["appeals_total"] == 1
    assert metrics["feedback"]["issues_unresolved"] == 1
    assert metrics["feedback"]["reports_unresolved"] == 1
    assert metrics["feedback"]["appeals_unresolved"] == 1
    assert metrics["totals"]["rejected"] == 1
    # 处理一条后未处理数下降
    assert editor["client"].patch(
        f"{API}/admin/issues/{issue['id']}",
        json={"action": "resolve", "reason": "已补单位并通知作者"},
    ).status_code == 200
    metrics = editor["client"].get(f"{API}/admin/metrics").json()
    assert metrics["feedback"]["issues_unresolved"] == 0
    assert metrics["feedback"]["issues_total"] == 1


# ---------------------------------------------------------------------------
# 七、能力探测 / 编辑台能力
# ---------------------------------------------------------------------------

def test_me_capabilities_quality_ops_and_unread(world):
    anon = _anon()
    try:
        anonymous = anon.get(f"{API}/me").json()
        assert anonymous["authenticated"] is False
        caps = anonymous["capabilities"]
        for key in ("notifications", "favorites", "feedback", "appeals",
                    "quality", "ops", "metrics", "rollback"):
            assert caps[key] is False, key
        assert anonymous["unread_notifications"] == 0
    finally:
        anon.close()

    context = _published(world)
    author = context["author"]
    me = author["client"].get(f"{API}/me").json()
    assert me["capabilities"]["favorites"] is True
    assert me["capabilities"]["feedback"] is True
    assert me["capabilities"]["quality"] is False
    assert me["capabilities"]["metrics"] is False
    assert me["unread_notifications"] >= 1
    assert me["stats"]["favorites"] == 0
    editor_me = context["editor"]["client"].get(f"{API}/me").json()
    for key in ("quality", "ops", "metrics", "rollback"):
        assert editor_me["capabilities"][key] is True, key
    admin_me = world.admin.get(f"{API}/me").json()
    assert admin_me["capabilities"]["quality"] is True
    assert admin_me["capabilities"]["admin"] is True
    reviewer = world.user("能力探测评审", roles=[("reviewer", "数学")])
    reviewer_me = reviewer["client"].get(f"{API}/me").json()
    assert reviewer_me["capabilities"]["review"] is True
    assert reviewer_me["capabilities"]["ops"] is False
    assert reviewer_me["capabilities"]["metrics"] is False


# ---------------------------------------------------------------------------
# 八、审计事件链与撤回统计
# ---------------------------------------------------------------------------

def test_quality_and_feedback_event_chain_persisted(world):
    context = _published(world)
    reporter = world.user("审计反馈人")
    issue = reporter["client"].post(
        f"{API}/questions/{context['public_id']}/issues",
        json={"field": "explanation", "description": "解析与答案不一致，请复核"},
    ).json()["issue"]
    assert context["editor"]["client"].patch(
        f"{API}/admin/issues/{issue['id']}",
        json={"action": "resolve", "reason": "已修订解析"},
    ).status_code == 200
    assert context["editor"]["client"].post(f"{API}/admin/quality/checks", json={
        "submission_id": context["submission_id"], "decision": "issue",
        "severity": "high", "findings": ["答案与题干冲突"],
    }).status_code == 201
    rows = _db_rows(
        "SELECT action, from_status, to_status FROM plaza_events WHERE submission_id=? "
        "ORDER BY id",
        (context["submission_id"],),
    )
    actions = [row["action"] for row in rows]
    for expected in ("submitted", "initial_review_accepted", "review_submitted",
                     "threshold_met", "published", "issue_submitted", "issue_resolved",
                     "quality_issue", "quality_withdrawn"):
        assert expected in actions, actions
    quality = next(row for row in rows if row["action"] == "quality_issue")
    assert quality["from_status"] == "published"
    assert quality["to_status"] == "published"
    withdrawn = next(row for row in rows if row["action"] == "quality_withdrawn")
    assert withdrawn["to_status"] == "withdrawn"
    # 通知与事件同事务落库：作者未读通知包含质量撤回
    assert _notifications(context["author"], unread_only=True)["total"] >= 2
