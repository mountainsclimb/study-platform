"""广场评审 / 发布 / 撤回回归（Phase 2，WS13）。

覆盖：编辑初审（含编辑回避与 CAS）、按学科分派与投票隐藏、一人一版本一票、
独立作答校验、阈值自动流转、冲突声明、学科复核拒绝、七天期限不自动失败、
签发精选与签发者回避、撤回整条链、重复请求幂等、并发发布/初审。
"""
import threading
from datetime import datetime, timedelta, timezone

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

    def question(self, author, *, subject="数学", stem="已知 1+1=？", answers=("A",)):
        response = author["client"].post("/api/paperlab/questions", json={
            "type": "SINGLE",
            "subject": subject,
            "content": {
                "stem": stem,
                "options": [{"label": "A", "text": "2"}, {"label": "B", "text": "3"}],
                "answers": list(answers),
                "explanation": "基础加法。",
            },
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

def _create_submission(author, question_id, *, source=None):
    return author["client"].post(f"{API}/submissions", json={
        "question_id": question_id,
        "source": _source() if source is None else source,
        "license_confirmed": True,
    })


def _submit(author, submission_id, version=1):
    return author["client"].post(f"{API}/submissions/{submission_id}/submit", json={
        "version": version, "confirm_preview": True,
    })


def _initial_review(editor, submission_id, *, version=1, decision="accept", reason="",
                    checked=True):
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


def _publish(editor, submission_id, *, version=1, reason="内容核验通过，签发精选"):
    return editor["client"].post(f"{API}/submissions/{submission_id}/publish", json={
        "version": version, "reason": reason,
    })


def _publish_ready(world, *, subject="数学", editor=None, author=None):
    """社区待核验 → 5 票通过 → verified（返回上下文与评审员）。"""
    author = author or world.user(f"评审作者-{subject}")
    if editor is None:
        editor = world.user(f"初审编辑-{subject}", roles=[("editor", "")])
    question_id = world.question(author, subject=subject)
    created = _create_submission(author, question_id)
    assert created.status_code == 201, created.text
    submission_id = created.json()["submission"]["id"]
    assert _submit(author, submission_id).status_code == 200
    accepted = _initial_review(editor, submission_id)
    assert accepted.status_code == 200, accepted.text
    subject_reviewers = [
        world.user(f"{subject}复核{i}", roles=[("subject_reviewer", subject)]) for i in range(2)
    ]
    reviewers = [world.user(f"{subject}评审{i}", roles=[("reviewer", subject)]) for i in range(3)]
    for reviewer in subject_reviewers + reviewers:
        response = _review(reviewer, submission_id)
        assert response.status_code == 201, response.text
    detail = author["client"].get(f"{API}/submissions/{submission_id}").json()["submission"]
    assert detail["status"] == "verified", detail
    return {
        "author": author,
        "editor": editor,
        "submission_id": submission_id,
        "question_id": question_id,
        "subject_reviewers": subject_reviewers,
        "reviewers": reviewers,
        "public_id": detail["public_id"],
    }


def _community_setup(world, *, subject="数学", editor=None, author=None):
    author = author or world.user(f"投稿作者-{subject}")
    if editor is None:
        editor = world.user(f"平台编辑-{subject}", roles=[("editor", "")])
    question_id = world.question(author, subject=subject)
    created = _create_submission(author, question_id)
    assert created.status_code == 201, created.text
    submission_id = created.json()["submission"]["id"]
    assert _submit(author, submission_id).status_code == 200
    accepted = _initial_review(editor, submission_id)
    assert accepted.status_code == 200, accepted.text
    return {
        "author": author, "editor": editor, "submission_id": submission_id,
        "question_id": question_id,
    }


# ---------------------------------------------------------------------------
# 编辑初审
# ---------------------------------------------------------------------------

def test_initial_review_sets_deadline_and_community_public(world):
    """方案 §5：初审通过 → 社区待核验，广场可浏览（显著标注）并给 7 天评审窗口。"""
    context = _community_setup(world)
    detail = context["author"]["client"].get(
        f"{API}/submissions/{context['submission_id']}"
    ).json()["submission"]
    assert detail["status"] == "community_review"
    deadline = datetime.fromisoformat(detail["review_deadline_at"])
    now = datetime.now(timezone.utc)
    assert timedelta(days=6, hours=23) <= deadline - now <= timedelta(days=7, minutes=5)
    assert detail["public_visible"] is True
    assert detail["public_id"]
    anon = _client()
    try:
        listing = anon.get(f"{API}/questions").json()
        assert listing["total"] == 1
        item = listing["items"][0]
        assert item["public_id"] == detail["public_id"]
        assert item["status"] == "community_review"
        assert item["review_status"] == "community"
        assert item["community_pending"] is True
        assert "待核验" in item["notice"]
        public_detail = anon.get(f"{API}/questions/{detail['public_id']}")
        assert public_detail.status_code == 200, public_detail.text
        assert public_detail.json()["visible"] is True
        assert public_detail.json()["review_status"] == "community"
    finally:
        anon.close()


def test_initial_review_requires_editor_role(world):
    context = _community_setup(world)
    reviewer = world.user("初审越权评审", roles=[("reviewer", "数学")])
    denied = _initial_review(reviewer, context["submission_id"])
    assert denied.status_code == 403
    student = world.user("初审越权学生")
    assert _initial_review(student, context["submission_id"]).status_code == 403


def test_initial_review_requires_checked_items(world):
    context = _community_setup(world)
    # 已通过初审，改用第二份投稿验证「未勾选初审项」
    author = world.user("初审校验作者")
    question_id = world.question(author)
    submission_id = _create_submission(author, question_id).json()["submission"]["id"]
    assert _submit(author, submission_id).status_code == 200
    unchecked = _initial_review(context["editor"], submission_id, checked=False)
    assert unchecked.status_code == 422
    body = unchecked.json()["detail"]
    assert set(body["missing"]) == {"source_checked", "privacy_checked", "completeness_checked"}


def test_initial_review_editor_avoidance(world):
    admin = {"client": world.admin, "id": world.admin.get("/api/auth/me").json()["id"],
             "name": "测试管理员"}
    question_id = world.question(admin)
    submission_id = _create_submission(admin, question_id).json()["submission"]["id"]
    assert _submit(admin, submission_id).status_code == 200
    denied = _initial_review(admin, submission_id)
    assert denied.status_code == 403
    assert "回避" in denied.json()["detail"]


def test_initial_review_rejects_wrong_version_and_cas_conflict(world):
    context = _community_setup(world)
    wrong_version = _initial_review(context["editor"], context["submission_id"], version=99)
    assert wrong_version.status_code == 409
    # 已过初审的投稿再次初审：状态 CAS 冲突
    repeated = _initial_review(context["editor"], context["submission_id"])
    assert repeated.status_code == 409
    assert repeated.json()["detail"]["status"] == "community_review"


# ---------------------------------------------------------------------------
# 评审队列 / 投票隐藏 / 唯一票 / 参数校验
# ---------------------------------------------------------------------------

def test_review_queue_scoping_and_hiding(world):
    math = _community_setup(world, subject="数学")
    physics = _community_setup(world, subject="物理")
    reviewer_math = world.user("数学队列评审", roles=[("reviewer", "数学")])
    subject_reviewer_math = world.user(
        "数学队列复核", roles=[("subject_reviewer", "数学")],
    )
    queue = reviewer_math["client"].get(f"{API}/review/queue").json()
    assert queue["total"] == 1
    item = queue["items"][0]
    assert item["submission_id"] == math["submission_id"]
    assert item["review_hidden"] is True
    assert "independent_reviews" not in json_dumps(item)

    # 学科复核员同口径，不越学科
    assert subject_reviewer_math["client"].get(
        f"{API}/review/queue"
    ).json()["total"] == 1
    outside = reviewer_math["client"].get(f"{API}/review/queue?subject=物理")
    assert outside.status_code == 403
    # 管理员不受学科限制
    assert world.admin.get(f"{API}/review/queue").json()["total"] == 2
    # 作者带评审角色也不在队列里看到自己的投稿
    author_reviewer = world.user(
        "作者兼评审", roles=[("reviewer", "数学")],
    )
    own = _community_setup(world, author=author_reviewer, editor=math["editor"])
    ids = [row["submission_id"] for row in
           author_reviewer["client"].get(f"{API}/review/queue").json()["items"]]
    assert own["submission_id"] not in ids
    assert math["submission_id"] in ids


def json_dumps(value):
    import json
    return json.dumps(value, ensure_ascii=False)


def test_review_hidden_until_own_submission(world):
    context = _community_setup(world)
    reviewer_a = world.user("投票隐藏评审A", roles=[("reviewer", "数学")])
    reviewer_b = world.user("投票隐藏评审B", roles=[("reviewer", "数学")])

    assert _review(reviewer_a, context["submission_id"]).status_code == 201
    before = reviewer_b["client"].get(f"{API}/submissions/{context['submission_id']}").json()
    assert before["submission"]["review_summary"]["hidden"] is True
    assert before["submission"]["reviews"] == []

    assert _review(reviewer_b, context["submission_id"]).status_code == 201
    after = reviewer_b["client"].get(f"{API}/submissions/{context['submission_id']}").json()
    summary = after["submission"]["review_summary"]
    assert "hidden" not in summary
    assert summary["independent_reviews"] == 2
    assert len(after["submission"]["reviews"]) == 2
    assert any(item["mine"] for item in after["submission"]["reviews"])


def test_one_vote_per_version_unique(world):
    context = _community_setup(world)
    reviewer = world.user("唯一票评审", roles=[("reviewer", "数学")])
    assert _review(reviewer, context["submission_id"]).status_code == 201
    duplicate = _review(reviewer, context["submission_id"])
    assert duplicate.status_code == 409
    body = duplicate.json()["detail"]
    assert "一人一版本一票" in body["message"]
    assert body["review_id"] >= 1
    rows = _db_rows(
        "SELECT COUNT(*) AS n FROM plaza_reviews WHERE submission_id=? AND reviewer_id=?",
        (context["submission_id"], reviewer["id"]),
    )
    assert rows[0]["n"] == 1


def test_review_validation_and_role_gate(world):
    context = _community_setup(world)
    reviewer = world.user("参数校验评审", roles=[("reviewer", "数学")])
    submission_id = context["submission_id"]

    no_answer = _review(reviewer, submission_id, independent_answer="")
    assert no_answer.status_code == 422
    no_reason = _review(reviewer, submission_id, verdict="revise", independent_answer="答案 B")
    assert no_reason.status_code == 422
    invalid = _review(reviewer, submission_id, verdict="maybe")
    assert invalid.status_code == 422
    stranger = world.user("无资格评审用户")
    assert _review(stranger, submission_id).status_code == 403

    # 作者即便拿到评审角色也必须回避本人投稿
    author = context["author"]
    granted = world.admin.post(f"{API}/admin/roles", json={
        "action": "grant", "role": "reviewer", "subject": "数学", "username": author["name"],
    })
    assert granted.status_code == 200
    avoided = _review(author, submission_id)
    assert avoided.status_code == 403
    assert "作者回避" in avoided.json()["detail"]

    wrong_version = _review(reviewer, submission_id, version=99)
    assert wrong_version.status_code == 409


# ---------------------------------------------------------------------------
# 阈值自动流转 / 冲突声明 / 争议 / 期限
# ---------------------------------------------------------------------------

def test_threshold_auto_verifies_after_five_reviews(world):
    context = _community_setup(world)
    subject_reviewers = [
        world.user(f"阈值复核{i}", roles=[("subject_reviewer", "数学")]) for i in range(2)
    ]
    reviewers = [world.user(f"阈值评审{i}", roles=[("reviewer", "数学")]) for i in range(3)]
    for index, reviewer in enumerate(subject_reviewers + reviewers):
        response = _review(reviewer, context["submission_id"])
        assert response.status_code == 201, response.text
        submission = response.json()["submission"]
        if index < 4:
            assert submission["status"] == "community_review", (index, response.text)
        else:
            assert response.json()["transition"] == "verified"
            assert submission["status"] == "verified"
            assert submission["public_id"]
    detail = context["author"]["client"].get(
        f"{API}/submissions/{context['submission_id']}"
    ).json()["submission"]
    summary = detail["review_summary"]
    assert summary["independent_reviews"] == 5
    assert summary["approves"] == 5
    assert summary["subject_reviewer_approves"] == 2
    assert summary["threshold_met"] is True
    anon = _client()
    try:
        assert anon.get(f"{API}/questions").json()["total"] == 1
    finally:
        anon.close()


def test_subject_reject_transitions_to_disputed_and_revise_recovers(world):
    context = _community_setup(world)
    subject_reviewer = world.user("争议复核", roles=[("subject_reviewer", "数学")])
    rejected = _review(
        subject_reviewer, context["submission_id"],
        verdict="reject", reasons="参考答案与题干条件冲突，需修订",
        independent_answer="我算得 B，与标注 A 不一致。",
    )
    assert rejected.status_code == 201, rejected.text
    assert rejected.json()["transition"] == "disputed"
    detail = context["author"]["client"].get(
        f"{API}/submissions/{context['submission_id']}"
    ).json()["submission"]
    assert detail["status"] == "disputed"
    anon = _client()
    try:
        assert anon.get(f"{API}/questions").json()["total"] == 0
        # 有争议不公开结论，但旧链接保留清晰状态与原因口径
        disputed = anon.get(f"{API}/questions/{detail['public_id']}")
        assert disputed.status_code == 200, disputed.text
        body = disputed.json()
        assert body["visible"] is False
        assert body["status"] == "disputed"
        assert body["reason"] and "争议" in body["reason"]
    finally:
        anon.close()

    # 作者修订后回到待初审，旧版本票不计入
    edited = context["author"]["client"].patch(f"/api/paperlab/questions/{context['question_id']}", json={
        "version": 1, "content": {"stem": "已知 1+2=？", "answers": ["B"]},
    })
    assert edited.status_code == 200, edited.text
    revised = context["author"]["client"].post(
        f"{API}/submissions/{context['submission_id']}/revise", json={"version": 1},
    )
    assert revised.status_code == 200, revised.text
    assert revised.json()["submission"]["status"] == "pending_initial"
    assert revised.json()["submission"]["review_summary"]["independent_reviews"] == 0


def test_conflict_declared_vote_not_counted(world):
    context = _community_setup(world)
    subject_reviewers = [
        world.user(f"冲突复核{i}", roles=[("subject_reviewer", "数学")]) for i in range(2)
    ]
    reviewers = [world.user(f"冲突评审{i}", roles=[("reviewer", "数学")]) for i in range(3)]
    for reviewer in subject_reviewers + reviewers[:2]:
        assert _review(reviewer, context["submission_id"]).status_code == 201

    conflict = _review(
        reviewers[2], context["submission_id"],
        conflict=True, reasons="与作者同一教研组，按规则回避",
        independent_answer="独立作答 A。",
    )
    assert conflict.status_code == 201, conflict.text
    summary = conflict.json()["review_summary"]
    assert summary["independent_reviews"] == 4
    assert summary["conflict_declared"] == 1
    assert summary["threshold_met"] is False
    assert conflict.json()["submission"]["status"] == "community_review"

    extra = world.user("补充评审", roles=[("reviewer", "数学")])
    final = _review(extra, context["submission_id"])
    assert final.json()["transition"] == "verified"


def test_deadline_passed_keeps_current_status(world):
    context = _community_setup(world)
    past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    from app import db as app_db
    with app_db.connection() as con:
        con.execute(
            "UPDATE plaza_submissions SET review_deadline_at=? WHERE id=?",
            (past, context["submission_id"]),
        )
    detail = context["author"]["client"].get(
        f"{API}/submissions/{context['submission_id']}"
    ).json()["submission"]
    assert detail["status"] == "community_review"
    assert detail["deadline_passed"] is True
    reviewer = world.user("超期评审", roles=[("reviewer", "数学")])
    queue = reviewer["client"].get(f"{API}/review/queue").json()["items"]
    assert queue[0]["deadline_passed"] is True
    # 超过七天仍可正常投票，状态不会自动失败
    assert _review(reviewer, context["submission_id"]).status_code == 201
    assert context["author"]["client"].get(
        f"{API}/submissions/{context['submission_id']}"
    ).json()["submission"]["status"] == "community_review"


# ---------------------------------------------------------------------------
# 签发精选 / 签发回避 / 幂等 / 并发
# ---------------------------------------------------------------------------

def test_publish_requires_verified_and_editor_role(world):
    context = _community_setup(world)
    not_ready = _publish(context["editor"], context["submission_id"])
    assert not_ready.status_code == 409
    assert "仅社区核验通过" in not_ready.json()["detail"]["message"]

    reviewer = world.user("越权签发评审", roles=[("reviewer", "数学")])
    denied = _publish(reviewer, context["submission_id"])
    assert denied.status_code == 403


def test_publish_success_and_featured_visibility(world):
    context = _publish_ready(world)
    publish = _publish(context["editor"], context["submission_id"])
    assert publish.status_code == 200, publish.text
    body = publish.json()
    assert body["idempotent"] is False
    release = body["release"]
    assert release["status"] == "active"
    assert release["public_id"] == context["public_id"]
    assert release["version"] == 1
    assert body["public_url"].endswith(context["public_id"])

    anon = _client()
    try:
        detail = anon.get(f"{API}/questions/{context['public_id']}").json()
        assert detail["status"] == "published"
        assert detail["source"] == "featured"
        assert "精选" in detail["notice"]
        assert anon.get(f"{API}/questions?source=featured").json()["total"] == 1
        assert anon.get(f"{API}/questions?source=community").json()["total"] == 0
    finally:
        anon.close()
    author_detail = context["author"]["client"].get(
        f"{API}/submissions/{context['submission_id']}"
    ).json()["submission"]
    assert author_detail["status"] == "published"


def test_publish_idempotent_duplicate(world):
    context = _publish_ready(world)
    first = _publish(context["editor"], context["submission_id"])
    assert first.status_code == 200
    second = _publish(context["editor"], context["submission_id"])
    assert second.status_code == 200, second.text
    assert second.json()["idempotent"] is True
    assert second.json()["release"]["public_id"] == context["public_id"]
    rows = _db_rows(
        "SELECT status FROM plaza_releases WHERE submission_id=?",
        (context["submission_id"],),
    )
    assert [row["status"] for row in rows] == ["active"]


def test_publish_avoidance_author_and_reviewer(world):
    # admin 作为作者：不能签发自己的投稿
    admin = {
        "client": world.admin,
        "id": world.admin.get("/api/auth/me").json()["id"],
        "name": "测试管理员",
    }
    context = _publish_ready(world, author=admin)
    denied = _publish(admin, context["submission_id"])
    assert denied.status_code == 403
    assert "本人投稿" in denied.json()["detail"]

    # 编辑兼评审投票后不能签发本人复核过的题
    author = world.user("回避验证作者")
    editor_reviewer = world.user(
        "编辑兼评审", roles=[("editor", ""), ("reviewer", "数学")],
    )
    other_editor = world.user("独立签发编辑", roles=[("editor", "")])
    question_id = world.question(author)
    submission_id = _create_submission(author, question_id).json()["submission"]["id"]
    assert _submit(author, submission_id).status_code == 200
    assert _initial_review(other_editor, submission_id).status_code == 200
    subject_reviewers = [
        world.user(f"回避复核{i}", roles=[("subject_reviewer", "数学")]) for i in range(2)
    ]
    reviewers = [
        world.user(f"回避评审{i}", roles=[("reviewer", "数学")]) for i in range(2)
    ]
    for reviewer in subject_reviewers + reviewers + [editor_reviewer]:
        assert _review(reviewer, submission_id).status_code == 201
    conflict = _publish(editor_reviewer, submission_id)
    assert conflict.status_code == 403
    assert "复核" in conflict.json()["detail"]
    assert _publish(other_editor, submission_id).status_code == 200


def test_concurrent_publish_creates_single_active_release(world):
    context = _publish_ready(world)
    second_editor = world.user("并发签发编辑", roles=[("editor", "")])
    results = []
    lock = threading.Lock()

    def attempt(name, password):
        client = _login(name, password)
        try:
            response = client.post(
                f"{API}/submissions/{context['submission_id']}/publish",
                json={"version": 1, "reason": "并发签发压测"},
            )
            with lock:
                results.append((response.status_code, response.json()))
        finally:
            client.close()

    admin_name = world.admin.get("/api/auth/me").json()["username"]
    threads = [
        threading.Thread(target=attempt, args=(admin_name, "test-password-123")),
        threading.Thread(target=attempt, args=(second_editor["name"], PASSWORD)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    statuses = sorted(status for status, _ in results)
    assert statuses == [200, 200], results
    idempotent_flags = sorted(payload.get("idempotent") for _, payload in results)
    assert idempotent_flags == [False, True]
    rows = _db_rows(
        "SELECT status FROM plaza_releases WHERE public_id=?", (context["public_id"],),
    )
    assert [row["status"] for row in rows] == ["active"]


def test_concurrent_initial_review_cas(world):
    author = world.user("并发初审作者")
    editor = world.user("并发初审编辑", roles=[("editor", "")])
    question_id = world.question(author)
    submission_id = _create_submission(author, question_id).json()["submission"]["id"]
    assert _submit(author, submission_id).status_code == 200
    results = []
    lock = threading.Lock()

    def attempt():
        client = _login(editor["name"])
        try:
            response = client.post(
                f"{API}/submissions/{submission_id}/initial-review",
                json={
                    "version": 1, "decision": "accept",
                    "source_checked": True, "privacy_checked": True,
                    "completeness_checked": True,
                },
            )
            with lock:
                results.append(response.status_code)
        finally:
            client.close()

    threads = [threading.Thread(target=attempt) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(results) == [200, 409], results
    events = _db_rows(
        "SELECT action FROM plaza_events WHERE submission_id=? AND action='initial_review_accepted'",
        (submission_id,),
    )
    assert len(events) == 1


# ---------------------------------------------------------------------------
# 全链路：投稿 → 初审 → 评审阈值 → 签发精选 → 撤回
# ---------------------------------------------------------------------------

def test_full_chain_submit_review_publish_withdraw(world):
    """一条链贯穿三个阶段：作者投稿、社区阈值核验、编辑签发与撤回。"""
    author = world.user("全链作者")
    editor = world.user("全链编辑", roles=[("editor", "")])
    question_id = world.question(author, subject="数学", stem="全链测试题：1+1=？")
    created = _create_submission(author, question_id)
    assert created.status_code == 201, created.text
    submission_id = created.json()["submission"]["id"]

    # 阶段 1：作者送审 + 编辑初审
    assert _submit(author, submission_id).status_code == 200
    assert _initial_review(editor, submission_id).status_code == 200

    # 阶段 2：五位评审（2 位学科复核 + 3 位评审）全票通过 → 自动 verified
    subject_reviewers = [
        world.user(f"全链复核{i}", roles=[("subject_reviewer", "数学")]) for i in range(2)
    ]
    reviewers = [world.user(f"全链评审{i}", roles=[("reviewer", "数学")]) for i in range(3)]
    for reviewer in subject_reviewers + reviewers:
        assert _review(reviewer, submission_id).status_code == 201
    detail = author["client"].get(f"{API}/submissions/{submission_id}").json()["submission"]
    assert detail["status"] == "verified"
    public_id = detail["public_id"]
    assert public_id

    # 阶段 3：编辑签发精选，匿名可读
    published = _publish(editor, submission_id)
    assert published.status_code == 200, published.text
    anon = _client()
    try:
        item = anon.get(f"{API}/questions/{public_id}")
        assert item.status_code == 200
        assert item.json()["status"] == "published"
        assert item.json()["content"]["answers"] == ["A"]
    finally:
        anon.close()

    # 阶段 4：编辑撤回 → 公开入口立即不可见，历史与事件保留
    withdrawn = editor["client"].post(f"{API}/releases/{public_id}/withdraw", json={
        "reason": "全链路验收撤回", "mode": "emergency",
    })
    assert withdrawn.status_code == 200, withdrawn.text
    anon = _client()
    try:
        assert anon.get(f"{API}/questions").json()["total"] == 0
        assert anon.get(f"{API}/questions/{public_id}").json()["visible"] is False
    finally:
        anon.close()

    events = author["client"].get(f"{API}/submissions/{submission_id}/events").json()["items"]
    actions = [item["action"] for item in events]
    for expected in ("created", "submitted", "initial_review_accepted", "review_submitted",
                     "threshold_met", "published", "release_withdrawn"):
        assert expected in actions, actions
    final = author["client"].get(f"{API}/submissions/{submission_id}").json()["submission"]
    assert final["status"] == "withdrawn"
    # 作者仍能看到自己的投稿内容（公开入口隐藏，不等于作者数据丢失）
    assert final["snapshot"]["content"]["stem"] == "全链测试题：1+1=？"


# ---------------------------------------------------------------------------
# 撤回整条链 / 发布历史 / 事件链
# ---------------------------------------------------------------------------

def test_withdraw_release_hides_public_and_is_idempotent(world):
    context = _publish_ready(world)
    assert _publish(context["editor"], context["submission_id"]).status_code == 200
    withdraw = context["editor"]["client"].post(
        f"{API}/releases/{context['public_id']}/withdraw",
        json={"reason": "收到版权投诉，紧急隐藏", "mode": "emergency"},
    )
    assert withdraw.status_code == 200, withdraw.text
    body = withdraw.json()
    assert body["status"] == "withdrawn"
    assert body["mode"] == "emergency"

    anon = _client()
    try:
        assert anon.get(f"{API}/questions").json()["total"] == 0
        detail = anon.get(f"{API}/questions/{context['public_id']}")
        assert detail.status_code == 200
        assert detail.json()["visible"] is False
        assert detail.json()["status"] == "withdrawn"
        assert "撤回" in detail.json()["notice"]
    finally:
        anon.close()

    again = context["editor"]["client"].post(
        f"{API}/releases/{context['public_id']}/withdraw", json={"reason": "重复撤回"},
    )
    assert again.status_code == 200
    assert again.json()["idempotent"] is True

    missing_reason = context["editor"]["client"].post(
        f"{API}/releases/{context['public_id']}/withdraw", json={"reason": ""},
    )
    assert missing_reason.status_code == 422
    unknown = context["editor"]["client"].post(
        f"{API}/releases/plz-not-exists/withdraw", json={"reason": "不存在"},
    )
    assert unknown.status_code == 404


def test_release_listing_and_full_event_chain(world):
    context = _publish_ready(world)
    assert _publish(context["editor"], context["submission_id"]).status_code == 200
    assert context["editor"]["client"].post(
        f"{API}/releases/{context['public_id']}/withdraw",
        json={"reason": "作者申诉，先下架复核", "mode": "normal"},
    ).status_code == 200

    releases = context["editor"]["client"].get(f"{API}/releases?status=withdrawn")
    assert releases.status_code == 200, releases.text
    items = releases.json()["items"]
    assert len(items) == 1
    assert items[0]["public_id"] == context["public_id"]
    assert items[0]["status"] == "withdrawn"
    assert items[0]["withdraw_mode"] == "normal"
    assert "作者申诉" in items[0]["reason"]

    events = context["author"]["client"].get(
        f"{API}/submissions/{context['submission_id']}/events"
    ).json()["items"]
    actions = [item["action"] for item in events]
    for expected in ("submitted", "initial_review_accepted", "review_submitted",
                     "threshold_met", "published", "release_withdrawn"):
        assert expected in actions, actions
    published = next(item for item in events if item["action"] == "published")
    assert published["from_status"] == "verified"
    assert published["to_status"] == "published"
    withdrawn = next(item for item in events if item["action"] == "release_withdrawn")
    assert withdrawn["from_status"] == "published"
    assert withdrawn["to_status"] == "withdrawn"
    # 作者视角不含 actor_id；编辑视角保留审计字段
    assert "actor_id" not in events[0]
    editor_events = context["editor"]["client"].get(
        f"{API}/submissions/{context['submission_id']}/events"
    ).json()["items"]
    assert "actor_id" in editor_events[0]
    stranger = world.user("事件越权用户")
    assert stranger["client"].get(
        f"{API}/submissions/{context['submission_id']}/events"
    ).status_code == 403
