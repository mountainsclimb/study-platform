"""广场公开可见性 / 公开衍生图 / 身份审核 / 编辑台与评审台接口回归（Phase 2，WS14）。

覆盖：

* 「社区待核验」初审通过即进入公开列表/详情，显著标注 ``review_status=community``
  并附评审进度摘要；``disputed`` 不公开结论；``published`` 标注 featured；
* 匿名公开图：发布/初审时登记公开衍生副本（复制字节 + sha256 校验 + public_derivative），
  读图接口只服务当前公开稿件当前版本的公开副本，白名单内容类型内联，撤回/争议/他人私有
  资产/非图片类型一律拒绝且不泄露存储路径；
* 题库来源图无法公开时降级为 ``figure_notice``，不阻断发布但发布响应给编辑明确提示；
* 身份认证审核（编辑台队列/approve/reject/到期）、编辑台投稿队列（脱敏作者）、
  评审台「我的评审历史」与权限隔离。
"""
import hashlib
import json
from io import BytesIO

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app import db, storage
from app.main import app
from app.paperlab import assets
from app.paperlab.db import ASSET_FOLDER

API = "/api/plaza"
PASSWORD = "plaza-pass-123"


# ---------------------------------------------------------------------------
# 基础助手
# ---------------------------------------------------------------------------

def _client():
    from app.main import app as fastapi_app
    return TestClient(fastapi_app)


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
    with db.connection() as con:
        return [dict(row) for row in con.execute(sql, params).fetchall()]


def png_bytes(width: int = 96, height: int = 64, color=(180, 40, 40)) -> bytes:
    image = Image.new("RGB", (width, height), color)
    buffer = BytesIO()
    image.save(buffer, "PNG")
    return buffer.getvalue()


def _asset_bytes(color, text: str = "") -> bytes:
    """带一点差异的 PNG 字节（不同颜色 + 文本块便于肉眼区分）。"""
    image = Image.new("RGB", (112, 72), color)
    if text:
        # 不依赖字体渲染，写入原始字节尾部即可产生不同 sha256。
        buffer = BytesIO()
        image.save(buffer, "PNG")
        return buffer.getvalue() + b"\n#" + text.encode("utf-8")
    buffer = BytesIO()
    image.save(buffer, "PNG")
    return buffer.getvalue()


def _register_asset(user_id: int, data: bytes | None = None, **kwargs) -> str:
    return assets.save_asset_bytes(
        int(user_id), _asset_bytes((20, 120, 20)) if data is None else data, **kwargs,
    )


def _insert_question_raw(owner_id: int, content: dict, *, subject: str = "数学") -> int:
    """绕过写入层直接落库（模拟旧内容/题库素材引用，写入层会拒绝这类引用）。"""
    now = db.utcnow()
    with db.connection() as con:
        return int(con.execute(
            "INSERT INTO pl_questions(user_id,source,type,subject,grade,difficulty,"
            "knowledge_tags_json,content_json,status,verify_status,version,ai_job_id,"
            "ai_model,prompt_version,created_at,updated_at,deleted_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,NULL,'','',?,?,NULL)",
            (
                int(owner_id), "manual", "SINGLE", subject, "高中一年级", 0.5,
                "[]", json.dumps(content, ensure_ascii=False),
                "draft", "pending", 1, now, now,
            ),
        ).lastrowid)


class World:
    """隔离测试世界：管理员 + 按需创建的用户与角色。"""

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

    def question(self, author, *, subject="数学", images=None):
        content = {
            "stem": "如图所示，选出正确答案。",
            "options": [{"label": "A", "text": "2"}, {"label": "B", "text": "3"}],
            "answers": ["A"],
            "explanation": "基础题。",
        }
        if images:
            content["images"] = images
        response = author["client"].post("/api/paperlab/questions", json={
            "type": "SINGLE", "subject": subject, "content": content,
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


def _initial_review(editor, submission_id, *, version=1, decision="accept", reason="",
                    checked=True, aliases=False):
    body = {"version": version, "decision": decision, "reason": reason}
    if aliases:
        body.update({"source_ok": checked, "privacy_ok": checked, "integrity_ok": checked})
    else:
        body.update({
            "source_checked": checked, "privacy_checked": checked,
            "completeness_checked": checked,
        })
    return editor["client"].post(f"{API}/submissions/{submission_id}/initial-review", json=body)


def _review(reviewer, submission_id, *, version=1, verdict="approve",
            reasons="", independent_answer="我独立作答得到 A。", conflict=False):
    return reviewer["client"].post(f"{API}/submissions/{submission_id}/reviews", json={
        "version": version, "verdict": verdict, "reasons": reasons,
        "independent_answer": independent_answer, "conflict_declared": conflict,
    })


def _author_detail(author, submission_id):
    return author["client"].get(f"{API}/submissions/{submission_id}").json()["submission"]


def _community(world, *, subject="数学", images=None, raw_question_id=None,
               author=None, editor=None, seed="社区"):
    author = author or world.user(f"{seed}作者-{subject}")
    if editor is None:
        editor = world.user(f"{seed}编辑-{subject}", roles=[("editor", "")])
    question_id = raw_question_id if raw_question_id is not None else world.question(
        author, subject=subject, images=images,
    )
    submission_id = _create_submission(author, question_id)
    submitted = _submit(author, submission_id)
    assert submitted.status_code == 200, submitted.text
    accepted = _initial_review(editor, submission_id)
    assert accepted.status_code == 200, accepted.text
    detail = _author_detail(author, submission_id)
    assert detail["status"] == "community_review"
    return {
        "author": author, "editor": editor, "question_id": question_id,
        "submission_id": submission_id, "public_id": detail["public_id"],
        "accepted": accepted.json(),
    }


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


def _make_verified(world, context, *, seed="评审"):
    voters = _voters(world, context.get("subject", "数学"), seed=seed)
    for reviewer in voters:
        response = _review(reviewer, context["submission_id"])
        assert response.status_code == 201, response.text
    detail = _author_detail(context["author"], context["submission_id"])
    assert detail["status"] == "verified", detail
    context["voters"] = voters
    context["public_id"] = detail["public_id"]
    return context


def _publish(editor, submission_id):
    return editor["client"].post(f"{API}/submissions/{submission_id}/publish", json={
        "version": 1, "reason": "内容核验通过，签发精选",
    })


def _anon_list(**params):
    client = _client()
    try:
        return client.get(f"{API}/questions", params=params).json()
    finally:
        client.close()


def _anon_get(path):
    client = _client()
    try:
        return client.get(path)
    finally:
        client.close()


# ---------------------------------------------------------------------------
# 1. 社区待核验公开可见且标注、附评审进度、不泄露身份
# ---------------------------------------------------------------------------

def test_community_review_public_with_progress_and_no_identity(world):
    context = _community(world)
    listing = _anon_list()
    assert listing["total"] == 1
    item = listing["items"][0]
    assert item["public_id"] == context["public_id"]
    assert item["status"] == "community_review"
    assert item["status_label"] == "社区待核验"
    assert item["review_status"] == "community"
    assert item["source"] == "community"
    assert item["community_pending"] is True
    assert "待核验" in item["notice"]
    progress = item["review"]
    assert progress["independent_reviews"] == 0
    assert progress["threshold_met"] is False
    assert progress["min_reviews"] == 5 and progress["min_approves"] == 4
    assert progress["reviews_needed"] == 5 and progress["approves_needed"] == 4
    assert progress["min_subject_reviewer_approves"] == 2

    detail = _anon_get(f"{API}/questions/{context['public_id']}").json()
    assert detail["visible"] is True
    assert detail["review_status"] == "community"
    assert detail["community_pending"] is True
    assert "待核验" in detail["notice"]
    assert detail["content"]["answers"] == ["A"]
    text = json.dumps(detail, ensure_ascii=False)
    for forbidden in ("author_id", "reviewer_id", "user_id", "paperlab/"):
        assert forbidden not in text
    assert context["author"]["name"] not in text
    # 来源过滤：社区可见、精选为空
    assert _anon_list(source="community")["total"] == 1
    assert _anon_list(source="featured")["total"] == 0


# ---------------------------------------------------------------------------
# 2. 有争议不公开结论（visible=false + 原因）
# ---------------------------------------------------------------------------

def test_disputed_hidden_with_reason(world):
    context = _community(world)
    subject_reviewer = world.user("争议复核员", roles=[("subject_reviewer", "数学")])
    rejected = _review(
        subject_reviewer, context["submission_id"], verdict="reject",
        reasons="参考答案与题干条件冲突，需修订",
        independent_answer="我算得 B，与标注 A 不一致。",
    )
    assert rejected.status_code == 201, rejected.text
    assert rejected.json()["transition"] == "disputed"
    assert _anon_list()["total"] == 0
    detail = _anon_get(f"{API}/questions/{context['public_id']}")
    assert detail.status_code == 200, detail.text
    body = detail.json()
    assert body["visible"] is False
    assert body["status"] == "disputed"
    assert body["review_status"] == "disputed"
    assert body["reason"] and "争议" in body["reason"]
    # 不泄露评审意见原文与评审人
    text = json.dumps(body, ensure_ascii=False)
    assert "我算得 B" not in text
    assert "reviewer_id" not in text


# ---------------------------------------------------------------------------
# 3. 精选可见 + 匿名可读公开衍生图
# ---------------------------------------------------------------------------

def test_published_featured_and_anonymous_image_read(world):
    data = _asset_bytes((30, 90, 200), "published-image")
    author = world.user("精选作者")
    asset_id = _register_asset(author["id"], data, media_type="image/png")
    context = _community(world, images=[{"asset": asset_id, "role": "stem"}], author=author)
    context["subject"] = "数学"
    _make_verified(world, context)
    published = _publish(context["editor"], context["submission_id"])
    assert published.status_code == 200, published.text
    body = published.json()
    assert body["idempotent"] is False
    assert body["figures"]["total"] == 1 and body["figures"]["public"] == 1
    # 初审已登记公开副本，发布会幂等复用，不会重复复制字节/重复生成资产
    derivative_rows = _db_rows(
        "SELECT asset_id FROM pl_assets WHERE user_id=? AND public_derivative=1",
        (author["id"],),
    )
    assert len(derivative_rows) == 1

    detail = _anon_get(f"{API}/questions/{context['public_id']}").json()
    assert detail["status"] == "published"
    assert detail["source"] == "featured"
    assert detail["review_status"] == "featured"
    assert detail["featured"] is True
    image = detail["content"]["images"][0]
    assert image["accessible"] is True
    assert image["asset_id"].startswith("pl_")
    assert image["media_type"] == "image/png"
    assert image["url"] == f"/api/plaza/assets/{image['asset_id']}"
    assert image["question_image_url"] == \
        f"/api/plaza/questions/{context['public_id']}/images/0"
    # 公开 payload 不得暴露原始私人资产 ID 或存储路径
    text = json.dumps(detail, ensure_ascii=False)
    assert asset_id not in text
    assert "paperlab/" not in text

    direct = _anon_get(image["url"])
    assert direct.status_code == 200, direct.text
    assert direct.headers["content-type"].startswith("image/png")
    assert direct.headers.get("x-content-type-options") == "nosniff"
    assert direct.content == data
    by_index = _anon_get(image["question_image_url"])
    assert by_index.status_code == 200
    assert by_index.content == data


# ---------------------------------------------------------------------------
# 4. 撤回后公开图 404、详情 visible=false
# ---------------------------------------------------------------------------

def test_withdraw_release_hides_public_images(world):
    data = _asset_bytes((200, 80, 20), "withdraw-image")
    author = world.user("撤回作者")
    asset_id = _register_asset(author["id"], data)
    context = _community(world, images=[{"asset": asset_id}], author=author)
    _make_verified(world, context)
    assert _publish(context["editor"], context["submission_id"]).status_code == 200
    detail = _anon_get(f"{API}/questions/{context['public_id']}").json()
    image = detail["content"]["images"][0]
    assert image["accessible"] is True
    assert _anon_get(image["url"]).status_code == 200

    withdrawn = context["editor"]["client"].post(
        f"{API}/releases/{context['public_id']}/withdraw",
        json={"reason": "收到版权投诉，紧急隐藏", "mode": "emergency"},
    )
    assert withdrawn.status_code == 200, withdrawn.text
    assert _anon_list()["total"] == 0
    after = _anon_get(f"{API}/questions/{context['public_id']}").json()
    assert after["visible"] is False
    assert after["status"] == "withdrawn"
    assert _anon_get(image["url"]).status_code == 404
    assert _anon_get(image["question_image_url"]).status_code == 404


# ---------------------------------------------------------------------------
# 5. 他人私有资产/原件不可匿名读
# ---------------------------------------------------------------------------

def test_other_users_private_asset_not_readable(world):
    data_a = _asset_bytes((10, 10, 200), "user-a")
    author_a = world.user("图片作者A")
    asset_a = _register_asset(author_a["id"], data_a)
    context = _community(world, images=[{"asset": asset_a}], author=author_a)
    detail = _anon_get(f"{API}/questions/{context['public_id']}").json()
    public_asset = detail["content"]["images"][0]["asset_id"]
    assert public_asset != asset_a
    # 公开副本可读，但原始私人资产不可读（即使它被公开快照引用）
    assert _anon_get(f"/api/plaza/assets/{public_asset}").status_code == 200
    assert _anon_get(f"/api/plaza/assets/{asset_a}").status_code == 404

    # 他人（B）的私有资产、未公开稿件图片也不可读
    author_b = world.user("图片作者B")
    asset_b = _register_asset(author_b["id"], _asset_bytes((10, 200, 10), "user-b"))
    assert _anon_get(f"/api/plaza/assets/{asset_b}").status_code == 404
    question_b = world.question(author_b, images=[{"asset": asset_b}])
    submission_b = _create_submission(author_b, question_b)
    # B 的稿件还在草稿：公开题号无从枚举，配图不可读
    draft_b = _author_detail(author_b, submission_b)
    assert draft_b["status"] == "draft" and draft_b["public_id"] is None
    assert _anon_get(f"/api/plaza/questions/plz-not-exists/images/0").status_code == 404
    assert _anon_get(f"/api/plaza/questions/{context['public_id']}/images/0").status_code == 200
    # 公开详情不包含原始资产 ID、存储文件名或他人用户名
    text = json.dumps(detail, ensure_ascii=False)
    assert asset_a not in text and "paperlab/" not in text
    assert author_b["name"] not in text


# ---------------------------------------------------------------------------
# 6. 公开衍生登记：复制字节 + public_derivative + sha256
# ---------------------------------------------------------------------------

def test_public_derivative_copy_registered_with_sha256(world):
    data = _asset_bytes((90, 30, 160), "sha-check")
    digest = hashlib.sha256(data).hexdigest()
    author = world.user("衍生图作者")
    asset_id = _register_asset(author["id"], data)
    context = _community(world, images=[{"asset": asset_id}], author=author)

    original = assets.fetch_asset(asset_id)
    assert original is not None
    assert int(original["public_derivative"]) == 0
    assert original["status"] == "active"
    assert original["sha256"] == digest

    rows = _db_rows(
        "SELECT * FROM plaza_public_assets WHERE submission_id=? AND version=1",
        (context["submission_id"],),
    )
    assert len(rows) == 1
    mapping = rows[0]
    assert mapping["status"] == "active"
    assert mapping["public_asset_id"].startswith("pl_")
    assert mapping["sha256"] == digest
    derivative = assets.fetch_asset(mapping["public_asset_id"])
    assert derivative is not None
    assert int(derivative["public_derivative"]) == 1
    assert derivative["status"] == "published"
    assert derivative["sha256"] == digest
    assert int(derivative["user_id"]) == int(author["id"])
    assert derivative["storage_ref"] != original["storage_ref"]
    # 公开详情只给公开副本
    detail = _anon_get(f"{API}/questions/{context['public_id']}").json()
    assert detail["content"]["images"][0]["asset_id"] == mapping["public_asset_id"]


# ---------------------------------------------------------------------------
# 7. sha256 一致性校验失败 → 降级提示，不阻断发布
# ---------------------------------------------------------------------------

def test_sha256_mismatch_degrades_with_figure_notice(world):
    data = _asset_bytes((120, 120, 20), "tamper-before")
    author = world.user("校验失败作者")
    asset_id = _register_asset(author["id"], data)
    editor = world.user("校验编辑", roles=[("editor", "")])
    question_id = world.question(author, images=[{"asset": asset_id}])
    submission_id = _create_submission(author, question_id)
    assert _submit(author, submission_id).status_code == 200

    # 初审前替换磁盘文件：登记哈希与真实字节不一致
    row = assets.fetch_asset(asset_id)
    path = storage.local_path(ASSET_FOLDER, row["storage_ref"])
    path.write_bytes(_asset_bytes((0, 0, 0), "tampered"))
    accepted = _initial_review(editor, submission_id)
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["figures"]["unavailable"] == 1
    assert "校验" in accepted.json()["figures"]["notices"][0]

    detail = _author_detail(author, submission_id)
    context = {
        "author": author, "editor": editor, "submission_id": submission_id,
        "public_id": detail["public_id"], "subject": "数学",
    }
    public = _anon_get(f"{API}/questions/{context['public_id']}").json()
    assert public["content"]["images"][0]["accessible"] is False
    assert public["content"]["images"][0]["reason"] == "integrity"
    assert "暂不可公开" in public["figure_notice"]
    assert _anon_get(
        f"/api/plaza/questions/{context['public_id']}/images/0"
    ).status_code == 404

    _make_verified(world, context, seed="校验")
    published = _publish(editor, submission_id)
    assert published.status_code == 200, published.text
    assert published.json()["figures"]["public"] == 0
    assert published.json()["figures"]["unavailable"] == 1


# ---------------------------------------------------------------------------
# 8. 题库来源图降级：配图需登录后查看，发布不阻断但编辑端有提示
# ---------------------------------------------------------------------------

def test_bank_figure_degrades_with_login_notice(world):
    author = world.user("题库图作者")
    editor = world.user("题库图编辑", roles=[("editor", "")])
    content = {
        "stem": "根据下图回答问题（原卷图）。",
        "options": [{"label": "A", "text": "2"}],
        "answers": ["A"],
        "explanation": "略。",
        # 题库原始素材相对路径：无法作为个人资产公开
        "images": [{"asset": "数学/images/bank-raw-1.png", "role": "figure"}],
    }
    question_id = _insert_question_raw(author["id"], content)
    submission_id = _create_submission(author, question_id)
    assert _submit(author, submission_id).status_code == 200
    accepted = _initial_review(editor, submission_id)
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["figures"]["total"] == 1
    assert accepted.json()["figures"]["public"] == 0
    assert "题库" in accepted.json()["figures"]["notices"][0]

    detail = _author_detail(author, submission_id)
    context = {
        "author": author, "editor": editor, "submission_id": submission_id,
        "public_id": detail["public_id"], "subject": "数学",
    }
    public = _anon_get(f"{API}/questions/{context['public_id']}").json()
    assert public["figure_notice"] == "配图需登录后查看"
    assert public["content"]["images"][0]["accessible"] is False
    assert public["content"]["images"][0]["reason"] == "bank"
    assert "bank-raw-1" not in json.dumps(public, ensure_ascii=False)
    assert _anon_get(
        f"/api/plaza/questions/{context['public_id']}/images/0"
    ).status_code == 404

    # 配图降级不阻断评审与发布
    _make_verified(world, context, seed="题库")
    published = _publish(editor, submission_id)
    assert published.status_code == 200, published.text
    body = published.json()
    assert body["figures"]["figure_notice"] == "配图需登录后查看"
    assert body["figures"]["unavailable"] == 1
    assert body["figures"]["notices"]


# ---------------------------------------------------------------------------
# 9. 白名单外内容类型拒绝（415，不输出字节）
# ---------------------------------------------------------------------------

def test_non_image_media_type_rejected(client):
    data = _asset_bytes((45, 45, 45), "mime")
    admin_id = client.get("/api/auth/me").json()["id"]
    author = {"client": client, "id": admin_id, "name": "测试管理员"}
    world = World(client)
    try:
        editor = world.user("媒体类型编辑", roles=[("editor", "")])
        community = _community(world, images=[{"asset": _register_asset(admin_id, data)}],
                               author=author, editor=editor, seed="媒体")
        _make_verified(world, community, seed="媒体")
        assert _publish(editor, community["submission_id"]).status_code == 200
        detail = _anon_get(f"{API}/questions/{community['public_id']}").json()
        image = detail["content"]["images"][0]
        assert _anon_get(image["url"]).status_code == 200
        # 篡改为非白名单内容类型：拒绝内联返回
        with db.connection() as con:
            con.execute(
                "UPDATE pl_assets SET media_type='application/x-msdownload' WHERE asset_id=?",
                (image["asset_id"],),
            )
        rejected = _anon_get(image["url"])
        assert rejected.status_code == 415
        assert b"<" not in rejected.content  # 不输出任何内容
        assert _anon_get(image["question_image_url"]).status_code == 415
    finally:
        for item in world.clients:
            item.close()


# ---------------------------------------------------------------------------
# 10. 公开图只对当前版本生效（改版后旧图 404）
# ---------------------------------------------------------------------------

def test_public_image_scoped_to_current_version(world):
    data_one = _asset_bytes((200, 0, 0), "v1")
    data_two = _asset_bytes((0, 200, 0), "v2")
    author = world.user("版本图片作者")
    asset_one = _register_asset(author["id"], data_one)
    asset_two = _register_asset(author["id"], data_two)
    context = _community(world, images=[{"asset": asset_one}], author=author)
    old_detail = _anon_get(f"{API}/questions/{context['public_id']}").json()
    old_public = old_detail["content"]["images"][0]["asset_id"]
    assert _anon_get(f"/api/plaza/assets/{old_public}").status_code == 200

    edited = author["client"].patch(f"/api/paperlab/questions/{context['question_id']}", json={
        "version": 1,
        "content": {
            "stem": "改版后的题目（含新配图）。",
            "options": [{"label": "A", "text": "2"}],
            "answers": ["A"],
            "images": [{"asset": asset_two}],
        },
    })
    assert edited.status_code == 200, edited.text
    revised = author["client"].post(
        f"{API}/submissions/{context['submission_id']}/revise", json={"version": 1},
    )
    assert revised.status_code == 200, revised.text
    assert revised.json()["submission"]["status"] == "pending_initial"
    accepted = _initial_review(context["editor"], context["submission_id"], version=2)
    assert accepted.status_code == 200, accepted.text
    new_detail = _author_detail(author, context["submission_id"])
    assert new_detail["current_version"] == 2
    public = _anon_get(f"{API}/questions/{context['public_id']}").json()
    new_public = public["content"]["images"][0]["asset_id"]
    assert new_public != old_public
    assert _anon_get(f"/api/plaza/assets/{new_public}").status_code == 200
    # 旧版本公开图立即失效
    assert _anon_get(f"/api/plaza/assets/{old_public}").status_code == 404
    assert _anon_get(
        f"/api/plaza/questions/{context['public_id']}/images/0"
    ).status_code == 200
    mapping = _db_rows(
        "SELECT version, status FROM plaza_public_assets WHERE submission_id=? ORDER BY version",
        (context["submission_id"],),
    )
    assert [(row["version"], row["status"]) for row in mapping] == [(1, "disabled"), (2, "active")]


# ---------------------------------------------------------------------------
# 11. 身份审核：队列权限、approve/reject、事件审计、到期
# ---------------------------------------------------------------------------

def test_identity_review_permissions_and_audit(world):
    applicant = world.user("身份申请人甲")
    applied = applicant["client"].post(f"{API}/identity/apply", json={
        "kind": "teacher", "subject": "数学", "evidence": "教师资格证脱敏材料",
    })
    assert applied.status_code == 201, applied.text
    identity_id = applied.json()["identity"]["id"]

    editor = world.user("身份审核编辑", roles=[("editor", "")])
    reviewer = world.user("身份越权评审", roles=[("reviewer", "数学")])
    plain = world.user("身份越权用户")
    assert reviewer["client"].get(f"{API}/admin/identity/queue").status_code == 403
    assert plain["client"].get(f"{API}/admin/identity/queue").status_code == 403
    assert plain["client"].post(
        f"{API}/admin/identity/{identity_id}/review",
        json={"decision": "approve"},
    ).status_code == 403

    queue = editor["client"].get(f"{API}/admin/identity/queue")
    assert queue.status_code == 200, queue.text
    body = queue.json()
    assert body["total"] == 1
    item = body["items"][0]
    assert item["id"] == identity_id
    assert item["user"]["username"] == applicant["name"]
    assert item["status"] == "pending"
    assert item["evidence"] == "教师资格证脱敏材料"

    # 驳回必须给原因；通过后写事件审计
    no_reason = editor["client"].post(
        f"{API}/admin/identity/{identity_id}/review", json={"decision": "reject"},
    )
    assert no_reason.status_code == 422
    approved = editor["client"].post(
        f"{API}/admin/identity/{identity_id}/review",
        json={"decision": "approve", "reason": "材料齐全，可认证", "expires_at": "2030-01-01T00:00:00Z"},
    )
    assert approved.status_code == 200, approved.text
    identity = approved.json()["identity"]
    assert identity["status"] == "approved"
    assert identity["expires_at"]
    assert applicant["client"].get(f"{API}/me").json()["identity"]["status"] == "approved"

    repeated = editor["client"].post(
        f"{API}/admin/identity/{identity_id}/review", json={"decision": "approve"},
    )
    assert repeated.status_code == 409
    events = _db_rows(
        "SELECT action, actor_id, to_status FROM plaza_events WHERE action LIKE 'identity_%'"
    )
    assert [row["action"] for row in events] == ["identity_approved"]
    assert int(events[0]["actor_id"]) == int(editor["id"])
    assert events[0]["to_status"] == "approved"

    # 过期时间必须是未来
    applicant2 = world.user("身份申请人乙")
    identity2 = applicant2["client"].post(f"{API}/identity/apply", json={
        "kind": "contributor", "subject": "", "evidence": "高质量解析贡献记录",
    }).json()["identity"]["id"]
    past = editor["client"].post(
        f"{API}/admin/identity/{identity2}/review",
        json={"decision": "approve", "expires_at": "2020-01-01T00:00:00Z"},
    )
    assert past.status_code == 422
    rejected = editor["client"].post(
        f"{API}/admin/identity/{identity2}/review",
        json={"decision": "reject", "reason": "证明材料无法核验"},
    )
    assert rejected.status_code == 200, rejected.text
    assert rejected.json()["identity"]["status"] == "rejected"
    assert applicant2["client"].get(f"{API}/me").json()["identity"]["status"] == "rejected"


def test_identity_expiry_marks_expired(world):
    applicant = world.user("到期身份用户")
    identity = applicant["client"].post(f"{API}/identity/apply", json={
        "kind": "student_teacher", "subject": "物理", "evidence": "师范生证明",
    }).json()["identity"]
    editor = world.user("到期审核编辑", roles=[("editor", "")])
    approved = editor["client"].post(
        f"{API}/admin/identity/{identity['id']}/review",
        json={"decision": "approve", "reason": "材料有效", "expires_at": "2030-01-01T00:00:00Z"},
    )
    assert approved.status_code == 200, approved.text
    with db.connection() as con:
        con.execute(
            "UPDATE plaza_identity SET expires_at='2020-01-01T00:00:00+00:00' WHERE id=?",
            (identity["id"],),
        )
    me = applicant["client"].get(f"{API}/me").json()
    assert me["identity"]["status"] == "expired"
    queue = editor["client"].get(f"{API}/admin/identity/queue?status=expired")
    assert queue.status_code == 200, queue.text
    assert queue.json()["total"] == 1
    assert queue.json()["items"][0]["expired"] is True
    # 已过期申请仍保留原始存储状态（审计可查）
    assert queue.json()["items"][0]["stored_status"] == "approved"


# ---------------------------------------------------------------------------
# 12. 编辑台投稿队列：权限、默认待处理、脱敏作者、筛选
# ---------------------------------------------------------------------------

def test_admin_submission_queue_permissions_and_masking(world):
    pending = _community_pending_only(world, seed="队列待初审")
    verified = _community(world)
    verified["subject"] = "数学"
    _make_verified(world, verified, seed="队列核验")
    # 待平台发布：手工置一个社区稿为 pending_release（历史/人工流转数据）
    community_for_release = _community(world, subject="物理", seed="队列待发布")
    with db.connection() as con:
        con.execute(
            "UPDATE plaza_submissions SET status='pending_release' WHERE id=?",
            (community_for_release["submission_id"],),
        )

    editor = verified["editor"]
    queue = editor["client"].get(f"{API}/admin/submissions")
    assert queue.status_code == 200, queue.text
    body = queue.json()
    statuses = {item["status"] for item in body["items"]}
    assert "pending_initial" in statuses
    assert "verified" in statuses
    assert "pending_release" in statuses
    assert body["total"] >= 3
    texts = json.dumps(body, ensure_ascii=False)
    for author in (pending["author"], verified["author"], community_for_release["author"]):
        assert author["name"] not in texts
    item = body["items"][0]
    assert "author_id" not in item and "user_id" not in item
    assert item["author_display"].startswith("投稿者·")
    assert item["stem_preview"]
    assert set(item["review"]) >= {
        "independent_reviews", "approves", "subject_reviewer_approves", "threshold_met",
    }
    assert "blocking_count" in item and "deadline_passed" in item

    # 权限：评审员/普通用户 403
    reviewer = world.user("队列越权评审", roles=[("reviewer", "数学")])
    plain = world.user("队列越权用户")
    assert reviewer["client"].get(f"{API}/admin/submissions").status_code == 403
    assert plain["client"].get(f"{API}/admin/submissions").status_code == 403
    # 管理员可用
    assert world.admin.get(f"{API}/admin/submissions").status_code == 200


def _community_pending_only(world, *, seed="待初审", subject="数学"):
    """只送审到 pending_initial（不经过初审）用于编辑台队列断言。"""
    author = world.user(f"{seed}作者")
    editor = world.user(f"{seed}编辑", roles=[("editor", "")])
    question_id = world.question(author, subject=subject)
    submission_id = _create_submission(author, question_id)
    assert _submit(author, submission_id).status_code == 200
    return {
        "author": author, "editor": editor, "question_id": question_id,
        "submission_id": submission_id,
    }


def test_admin_submission_queue_filters(world):
    editor = world.user("队列筛选编辑", roles=[("editor", "")])
    math_ctx = _community_pending_only(world, seed="筛选数学", subject="数学")
    physics_ctx = _community_pending_only(world, seed="筛选物理", subject="物理")
    # 复用编辑账号无法直接审别人？可以：编辑不是作者。
    for ctx in (math_ctx, physics_ctx):
        assert _initial_review(editor, ctx["submission_id"]).status_code == 200
    queue = editor["client"].get(f"{API}/admin/submissions?status=community_review")
    assert queue.status_code == 200
    assert queue.json()["total"] == 2
    only_math = editor["client"].get(
        f"{API}/admin/submissions?status=community_review&subject=数学"
    )
    assert only_math.json()["total"] == 1
    multi = editor["client"].get(
        f"{API}/admin/submissions?status=pending_initial,community_review"
    )
    assert multi.json()["total"] == 2
    empty = editor["client"].get(f"{API}/admin/submissions?status=community_review&subject=化学")
    assert empty.json()["total"] == 0
    invalid = editor["client"].get(f"{API}/admin/submissions?status=not_a_status")
    assert invalid.status_code == 422
    alias = editor["client"].get(f"{API}/admin/submissions?status= verified ")
    assert alias.status_code == 200 and alias.json()["total"] == 0


# ---------------------------------------------------------------------------
# 13. 评审台「我的评审历史」
# ---------------------------------------------------------------------------

def test_review_mine_history_and_permissions(world):
    context_one = _community(world, seed="历史一")
    context_two = _community(world, seed="历史二")
    reviewer = world.user("历史评审员", roles=[("reviewer", "数学")])
    for context in (context_one, context_two):
        response = _review(reviewer, context["submission_id"], reasons="独立作答通过")
        assert response.status_code == 201, response.text

    history = reviewer["client"].get(f"{API}/review/mine")
    assert history.status_code == 200, history.text
    body = history.json()
    assert body["total"] == 2
    assert len(body["items"]) == 2
    item = body["items"][0]
    assert item["submission_id"] in (context_one["submission_id"], context_two["submission_id"])
    assert item["verdict"] == "approve"
    assert item["version"] == 1
    assert item["status"] == "community_review"
    assert item["status_label"] == "社区待核验"
    assert item["subject"] == "数学"
    assert item["stem_preview"]
    assert item["reviewed_at"]
    # 作者回避：作者本人投稿不会出现在任何评审员的「我的评审历史」里
    assert context_one["author"]["client"].get(f"{API}/review/mine").status_code == 403
    # 普通用户 403；编辑/admin 可看自己的评审历史（为空）
    plain = world.user("历史越权用户")
    assert plain["client"].get(f"{API}/review/mine").status_code == 403
    editor = context_one["editor"]
    empty = editor["client"].get(f"{API}/review/mine")
    assert empty.status_code == 200 and empty.json()["total"] == 0


# ---------------------------------------------------------------------------
# 14. 编辑台字段兼容（角色授权 active / 初审 source_ok 等另一套写法）
# ---------------------------------------------------------------------------

def test_admin_roles_accepts_active_alias(world):
    """编辑台另一套角色授权写法（active:true/false，无 action）与正式口径等价。"""
    user = world.user("兼容角色用户")
    granted = world.admin.post(f"{API}/admin/roles", json={
        "active": True, "role": "reviewer", "subject": "数学", "username": user["name"],
    })
    assert granted.status_code == 200, granted.text
    assert granted.json()["action"] == "grant"
    assert user["client"].get(f"{API}/me").json()["capabilities"]["review"] is True
    revoked = world.admin.post(f"{API}/admin/roles", json={
        "active": False, "role": "reviewer", "subject": "数学", "username": user["name"],
    })
    assert revoked.status_code == 200, revoked.text
    assert revoked.json()["action"] == "revoke"
    assert user["client"].get(f"{API}/me").json()["capabilities"]["review"] is False
    # 两种字段都缺失 → 422
    missing = world.admin.post(f"{API}/admin/roles", json={
        "role": "editor", "username": user["name"],
    })
    assert missing.status_code == 422


def test_initial_review_accepts_editor_alias_fields(world):
    author = world.user("兼容字段作者")
    editor = world.user("兼容字段编辑", roles=[("editor", "")])
    question_id = world.question(author)
    submission_id = _create_submission(author, question_id)
    assert _submit(author, submission_id).status_code == 200
    accepted = _initial_review(editor, submission_id, aliases=True)
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["submission"]["status"] == "community_review"

    # 全部未勾选时仍然 422，且 missing 使用正式字段名
    author2 = world.user("兼容字段作者二")
    submission_id2 = _create_submission(author2, world.question(author2))
    assert _submit(author2, submission_id2).status_code == 200
    unchecked = _initial_review(editor, submission_id2, checked=False)
    assert unchecked.status_code == 422
    assert set(unchecked.json()["detail"]["missing"]) == {
        "source_checked", "privacy_checked", "completeness_checked",
    }
