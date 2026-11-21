"""WS19 · 平台精选（``official:<public_id>``）统一引用与学习链路测试（Phase 3）。

覆盖：
- ``app/official.py`` 的可见性规则（仅 community_review/verified/published 且 published 需 active
  发布指针；draft/撤回/争议在**查询时**立即不可见）与公开投影（公开图 URL、来源声明）；
- ``app/question_ref.py`` 的 official 来源支持（storage_key 仍为 ``official:<id>``）；
- ``GET /api/questions?source=bank|official|all``（默认 bank 向后兼容、official 筛选、all 合并
  与各自 total、非法 source 422）；
- 详情/作答判分/答案门控/幂等提交/错题本/复习队列/掌握度/stats 等统一学习链路；
- 撤回/争议后搜索与详情立即不可见，历史作答/错题保留但只显示「内容已撤回」占位；
- 跨用户不泄露草稿、公开题私态隔离；
- 工坊组卷快照冻结 official 内容 + public_id + 发布版本，回看标注来源且不受后续修订影响；
- 非法引用（空 public_id、格式非法、未发布）422/404。

广场数据用 ``plaza_submissions/plaza_releases/plaza_public_assets`` 最小种子写入（不依赖评审流程），
另用一条真实「投稿→初审→5 票评审→签发→撤回」端到端用例证明精选发布链路可被练习读取。
题库使用 ``bank_fixture`` 自含最小库；不联网。
"""
import json

import pytest
from fastapi.testclient import TestClient

from app import question_ref
from app import practice as practice_module

PLAZA = "/api/plaza"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def user_id(client) -> int:
    return client.get("/api/auth/me").json()["id"]


def clear_bank_caches():
    """切换只读题库后清掉进程内缓存（列探测 / 素材清单 / 检索索引 / catalog）。"""
    practice_module._sqlite_question_columns.cache_clear()
    practice_module._sqlite_group_columns.cache_clear()
    practice_module._source_groups.cache_clear()
    practice_module._verified_index_key = None
    practice_module._verified_fts_key = None
    practice_module.invalidate_catalog_cache()


@pytest.fixture
def install_bank(tmp_path, monkeypatch):
    """安装自含最小只读题库；返回安装函数。"""
    from app import db
    from bank_fixture import make_bank

    sequence = {"n": 0}

    def install(rows=(), groups=()):
        sequence["n"] += 1
        bank = make_bank(tmp_path / f"bank-{sequence['n']}", rows, groups)
        monkeypatch.setattr(db, "BANK_DIR", bank)
        clear_bank_caches()
        return bank

    yield install
    clear_bank_caches()


def bank_single(qid="docQ:1", **values):
    from bank_fixture import question_row

    values.setdefault("doc_id", qid.split(":", 1)[0])
    values.setdefault("options_json", '[{"label":"A","text":"2"},{"label":"B","text":"3"}]')
    values.setdefault("answer_text", "A")
    return question_row(qid, "已知 1+1=2，下列结论正确的是（  ）", **values)


def official_snapshot(*, stem="已知 1+1=2，下列结论正确的是（  ）",
                      options=None, answers=("B",), explanation="基础加法。",
                      qtype="SINGLE", subject="数学", grade="高中一年级",
                      tags=("函数",), difficulty=0.2, parts=None, images=None,
                      verify_status="verified"):
    content = {
        "stem": stem,
        "options": options if options is not None else [
            {"label": "A", "text": "2"}, {"label": "B", "text": "3"},
        ],
        "answers": list(answers),
        "explanation": explanation,
        "rubric": "",
        "parts": list(parts or []),
        "images": list(images or []),
    }
    return {
        "question_id": 1,
        "question_version": 1,
        "type": qtype,
        "subject": subject,
        "grade": grade,
        "difficulty": difficulty,
        "knowledge_tags": list(tags),
        "content": content,
        "verify_status": verify_status,
    }


def _source_json():
    return {
        "url": "https://example.com/exam/2026/math/12",
        "original_author": "命题组",
        "license_basis": "原创并授权知序公开展示",
        "rights_scope": "允许公开题面、答案与解析；禁止商业再分发",
        "attribution": "署名：知序投稿作者",
        "reference": "2026 模拟卷",
        "page_no": "12",
        "question_no": "3",
    }


def seed_official(*, public_id="plz-test-1", status="published", release=True,
                  release_status="active", version=1, snapshot=None,
                  subject="数学", type="SINGLE", author_id=1, images=(),
                  published_by=1):
    """直接写入最小广场数据（不依赖评审流程），返回 submission_id。"""
    from app import db
    from app.db import connection, utcnow
    from app.plaza import db as plaza_db

    plaza_db.ensure_tables()
    snapshot = snapshot if snapshot is not None else official_snapshot()
    now = utcnow()
    with connection() as con:
        db.write_lock(con, "plaza-write")
        submission_id = plaza_db.insert_row(con, "plaza_submissions", {
            "author_id": int(author_id),
            "question_id": None,
            "version": int(version),
            "current_version": int(version),
            "subject": subject,
            "type": type,
            "grade": "高中一年级",
            "snapshot_json": plaza_db.json_dump(snapshot),
            "source_json": plaza_db.json_dump(_source_json()),
            "fingerprint": "f" * 8,
            "license_confirmed": 1,
            "status": status,
            "public_id": public_id,
            "review_deadline_at": None,
            "created_at": now,
            "updated_at": now,
        })
        if release:
            plaza_db.insert_row(con, "plaza_releases", {
                "submission_id": submission_id,
                "version": int(version),
                "public_id": public_id,
                "status": release_status,
                "active_public_id": public_id if release_status == "active" else None,
                "published_by": int(published_by),
                "published_at": now,
                "withdrawn_at": None,
                "withdraw_mode": "",
                "reason": "",
            })
        for index, (asset_id, media_type) in enumerate(images):
            plaza_db.insert_row(con, "plaza_public_assets", {
                "submission_id": submission_id,
                "version": int(version),
                "image_index": int(index),
                "source_ref": "",
                "public_asset_id": asset_id,
                "media_type": media_type,
                "sha256": "0" * 64,
                "status": "active",
                "reason": "",
                "created_at": now,
            })
    return submission_id


def set_official_status(public_id: str, status: str, *, release_status=None):
    from app import db
    from app.db import connection, utcnow

    with connection() as con:
        db.write_lock(con, "plaza-write")
        con.execute("UPDATE plaza_submissions SET status=? WHERE public_id=?", (status, public_id))
        if release_status is not None:
            con.execute(
                "UPDATE plaza_releases SET status=?, active_public_id=NULL, withdrawn_at=? "
                "WHERE public_id=?",
                (release_status, utcnow(), public_id),
            )


def withdraw_official(public_id: str):
    set_official_status(public_id, "withdrawn", release_status="withdrawn")


def update_snapshot(public_id: str, snapshot: dict):
    from app import db
    from app.db import connection
    from app.plaza import db as plaza_db

    with connection() as con:
        db.write_lock(con, "plaza-write")
        con.execute(
            "UPDATE plaza_submissions SET snapshot_json=? WHERE public_id=?",
            (plaza_db.json_dump(snapshot), public_id),
        )


def attempt_rows(uid, qid) -> list[dict]:
    from app.db import connection

    with connection() as con:
        return [
            dict(row) for row in con.execute(
                "SELECT * FROM attempts WHERE user_id=? AND qid=? ORDER BY id", (uid, qid)
            )
        ]


def wrong_row(uid, qid):
    from app.db import connection

    with connection() as con:
        return con.execute(
            "SELECT * FROM wrong_questions WHERE user_id=? AND qid=?", (uid, qid)
        ).fetchone()


def submit_attempt(client, qid, answer=None, submission_id=None):
    data = {"qid": qid}
    if answer is not None:
        data["answer"] = answer
    if submission_id is not None:
        data["submission_id"] = submission_id
    return client.post("/api/attempts", data=data)


def create_paper(client, items, title="WS19 精选整卷") -> dict:
    response = client.post("/api/paperlab/papers", json={
        "title": title, "kind": "manual", "subject": "数学", "grade": "高中一年级",
        "items": items,
    })
    assert response.status_code == 200, response.text
    return response.json()


def queue_refs(data) -> dict:
    return {
        "graded": {item["ref"] for item in data["items"]},
        "pending": {item["ref"] for item in data["pending_items"]},
    }


def _register(name, password="official-pass-123"):
    from app.main import app

    client = TestClient(app)
    response = client.post("/api/auth/register", json={"username": name, "password": password})
    assert response.status_code == 200, response.text
    return client, response.json()


# ---------------------------------------------------------------------------
# 1. 统一引用解析：official 来源已开放
# ---------------------------------------------------------------------------

def test_question_ref_official_supported_storage_key_and_labels():
    parsed = question_ref.parse("official:plz-abc123")
    assert parsed.source == question_ref.SOURCE_OFFICIAL
    assert parsed.supported is True
    assert parsed.is_official is True
    assert parsed.ref == "official:plz-abc123"
    # 写库键保持 official:<public_id>（attempts/wrong_questions/mastery 统一身份）。
    assert parsed.storage_key == "official:plz-abc123"
    assert parsed.source_label == "平台精选"
    assert question_ref.SOURCE_OFFICIAL in question_ref.SUPPORTED_SOURCES
    assert question_ref.normalize("official:plz-abc123") == "official:plz-abc123"
    assert question_ref.build("official", "plz-abc123") == "official:plz-abc123"
    assert question_ref.split("official:plz-abc123") == ("official", "plz-abc123")
    assert question_ref.describe("official:plz-abc123")["source_label"] == "平台精选"
    # 裸 qid / bank: 仍按题库解析（旧客户端不变）。
    assert question_ref.parse("docA:12").storage_key == "docA:12"
    with pytest.raises(question_ref.QuestionRefError):
        question_ref.parse("official:")


# ---------------------------------------------------------------------------
# 2. 详情 / 答案门控 / 公开图片 / 来源声明
# ---------------------------------------------------------------------------

def test_official_detail_visibility_answer_gate_and_public_images(client):
    pid = "plz-detail-1"
    seed_official(
        public_id=pid,
        snapshot=official_snapshot(images=[{"asset": "pub-asset-1", "role": "figure"}]),
        images=[("pub-asset-1", "image/png")],
    )
    ref = f"official:{pid}"
    detail = client.get("/api/questions/" + ref)
    assert detail.status_code == 200, detail.text
    payload = detail.json()
    assert payload["qid"] == ref and payload["ref"] == ref
    assert payload["source"] == "official" and payload["source_label"] == "平台精选"
    assert payload["public_id"] == pid
    assert payload["review_status"] == "featured"
    assert payload["featured"] is True and payload["community_pending"] is False
    assert payload["published_version"] == 1
    assert payload["objective"] is True
    assert payload["unlocked"] is False
    assert "answer" not in payload and not payload.get("answer_text")
    assert payload["source_declaration"]["original_author"] == "命题组"
    image = payload["images"][0]
    assert image["accessible"] is True
    assert image["url"].endswith(f"/api/plaza/questions/{pid}/images/0")
    # 未作答前查看答案被门控。
    assert client.get("/api/reveal", params={"qid": ref}).status_code == 403

    submitted = submit_attempt(client, ref, "B")
    assert submitted.status_code == 200, submitted.text
    assert submitted.json()["result"] == "correct"
    assert submitted.json()["question"]["unlocked"] is True
    assert submitted.json()["question"]["answer"]["text"] == "B"
    revealed = client.get("/api/reveal", params={"qid": ref})
    assert revealed.status_code == 200
    assert revealed.json()["answer_text"] == "B"


# ---------------------------------------------------------------------------
# 3. 列表：source=official 筛选 / 非法 source / 旧默认 bank 不变
# ---------------------------------------------------------------------------

def test_official_list_filtering_and_legacy_bank_default(client, install_bank):
    install_bank([bank_single()])
    seed_official(
        public_id="plz-math-1", subject="数学",
        snapshot=official_snapshot(stem="精选数学题干：函数与导数", tags=("函数",)),
    )
    seed_official(
        public_id="plz-physics-1", subject="物理", type="MULTIPLE",
        snapshot=official_snapshot(
            stem="精选物理题干：动量守恒", qtype="MULTIPLE", subject="物理",
            options=[{"label": "A", "text": "甲"}, {"label": "B", "text": "乙"}],
            answers=["AB"], tags=("动量",),
        ),
    )
    listing = client.get("/api/questions", params={"source": "official"})
    assert listing.status_code == 200, listing.text
    data = listing.json()
    assert data["source"] == "official"
    assert data["total"] == 2 == data["official_total"]
    assert {item["qid"] for item in data["items"]} == {
        "official:plz-math-1", "official:plz-physics-1",
    }
    for item in data["items"]:
        assert item["source"] == "official" and item["source_label"] == "平台精选"
        assert item["public_id"].startswith("plz-")
        assert item["review_status"] == "featured"
        assert item["unlocked"] is False and "answer" not in item

    math_only = client.get(
        "/api/questions", params={"source": "official", "subject": "数学"},
    ).json()
    assert math_only["total"] == 1
    assert math_only["items"][0]["qid"] == "official:plz-math-1"

    # 练习侧题型（single_choice）与工坊题型（MULTIPLE）两种口径都能筛选。
    single_only = client.get(
        "/api/questions", params={"source": "official", "type": "single_choice"},
    ).json()
    assert [item["qid"] for item in single_only["items"]] == ["official:plz-math-1"]
    multi_only = client.get(
        "/api/questions", params={"source": "official", "type": "MULTIPLE"},
    ).json()
    assert [item["qid"] for item in multi_only["items"]] == ["official:plz-physics-1"]

    keyword = client.get(
        "/api/questions", params={"source": "official", "q": "动量"},
    ).json()
    assert [item["qid"] for item in keyword["items"]] == ["official:plz-physics-1"]

    # 默认 source=bank：结构/行为保持旧口径（只有题库、不带 official 字段）。
    default = client.get("/api/questions").json()
    assert default["total"] == 1
    assert [item["qid"] for item in default["items"]] == ["docQ:1"]
    assert "source" not in default and default["items"][0]["source"] == "bank"

    # 非法 source 统一 422。
    bad = client.get("/api/questions", params={"source": "plaza"})
    assert bad.status_code == 422


def test_official_list_source_all_merges_totals(client, install_bank):
    install_bank([bank_single()])
    seed_official(public_id="plz-all-1")
    seed_official(public_id="plz-all-2", subject="物理",
                  snapshot=official_snapshot(subject="物理", qtype="BLANK",
                                             options=[], answers=["6"]))
    data = client.get("/api/questions", params={"source": "all"}).json()
    assert data["source"] == "all"
    assert data["bank_total"] == 1 and data["official_total"] == 2
    assert data["total"] == 3
    refs = [item["qid"] for item in data["items"]]
    # 合并口径：题库在前，平台精选题在后，同一页内按合并偏移严格分页。
    assert refs[0] == "docQ:1"
    assert set(refs[1:]) == {"official:plz-all-1", "official:plz-all-2"}

    # 分页偏移跨过题库行：第 2 页 page_size=1 应全为 official。
    page = client.get(
        "/api/questions", params={"source": "all", "page": 2, "page_size": 1},
    ).json()
    assert page["total"] == 3 and page["page"] == 2
    assert len(page["items"]) == 1 and page["items"][0]["source"] == "official"

    # 过滤条件同时作用于两类：题库侧按关键词「混入」查不到（fixture 题干无该词），官方侧按学科。
    filtered = client.get(
        "/api/questions", params={"source": "all", "subject": "物理"},
    ).json()
    assert filtered["bank_total"] == 0
    assert filtered["official_total"] == 1


# ---------------------------------------------------------------------------
# 4. 作答判分、错题本、复习队列、掌握度、stats
# ---------------------------------------------------------------------------

def test_official_attempt_grading_wrongbook_queue_mastery_stats(client, install_bank):
    install_bank([bank_single()])
    seed_official(public_id="plz-practice-1")
    ref = "official:plz-practice-1"
    uid = user_id(client)

    wrong = submit_attempt(client, ref, "A", submission_id="official-1")
    assert wrong.status_code == 200, wrong.text
    payload = wrong.json()
    assert payload["result"] == "incorrect"
    assert payload["duplicate"] is False
    assert payload["question"]["source"] == "official"
    assert payload["question"]["public_id"] == "plz-practice-1"
    assert payload["question"]["answer"]["text"] == "B"
    assert len(attempt_rows(uid, ref)) == 1
    row = wrong_row(uid, ref)
    assert row is not None and row["failures"] == 1 and row["last_result"] == "incorrect"

    box = client.get("/api/wrongbook").json()
    assert box["total"] == 1
    card = box["items"][0]
    assert card["qid"] == ref and card["ref"] == ref
    assert card["source"] == "official" and card["source_label"] == "平台精选"
    assert card["public_id"] == "plz-practice-1"
    assert card["stem_plain"].startswith("已知 1+1=2")
    assert card["unavailable"] is False
    assert card["last_result"] == "incorrect"

    queue = client.get("/api/review/queue").json()
    assert queue["pending_items"] == []
    assert ref in queue_refs(queue)["graded"]
    graded = next(item for item in queue["items"] if item["ref"] == ref)
    assert graded["graded"] is True and graded["source"] == "official"

    # 掌握度：判对后才允许标注「比较掌握」。
    assert client.post("/api/mastery", json={"qid": ref, "level": "比较掌握"}).status_code == 403
    corrected = submit_attempt(client, ref, "B")
    assert corrected.status_code == 200 and corrected.json()["result"] == "correct"
    assert client.post("/api/mastery", json={"qid": ref, "level": "比较掌握"}).status_code == 200
    detail = client.get("/api/questions/" + ref).json()
    assert detail["mastery"] == "比较掌握"

    stats = client.get("/api/stats").json()
    assert stats["official_attempts"] == 2
    assert stats["official_practiced"] == 1
    assert stats["official_wrong_count"] == 1
    # 既有字段语义不变（official 作答也计入 attempts/practiced）。
    assert stats["attempts"] == 2 and stats["practiced"] == 1
    assert stats["recent_attempts"][0]["source"] == "official"
    assert stats["recent_attempts"][0]["ref"].startswith("official:")


def test_official_blank_and_subjective_grading_pending(client):
    seed_official(
        public_id="plz-blank-1", type="BLANK",
        snapshot=official_snapshot(
            stem="计算：1+2+3=____。", qtype="BLANK", options=[], answers=["6"],
            explanation="1+2+3=6。",
        ),
    )
    seed_official(
        public_id="plz-subj-1", type="SUBJECTIVE",
        snapshot=official_snapshot(
            stem="证明单调性。", qtype="SUBJECTIVE", options=[],
            answers=["利用导数可证。"], explanation="对 f 求导。", tags=("导数",),
        ),
    )
    blank = submit_attempt(client, "official:plz-blank-1", "6")
    assert blank.status_code == 200, blank.text
    assert blank.json()["result"] == "correct"
    wrong_blank = submit_attempt(client, "official:plz-blank-1", "7")
    assert wrong_blank.json()["result"] == "incorrect"

    subjective = submit_attempt(client, "official:plz-subj-1", "我的证明：由导数可证。")
    assert subjective.status_code == 200, subjective.text
    assert subjective.json()["result"] == "pending"
    assert subjective.json()["question"]["grading_status"] == "pending"

    queue = client.get("/api/review/queue").json()
    refs = queue_refs(queue)
    assert "official:plz-subj-1" in refs["pending"]
    assert "official:plz-subj-1" not in refs["graded"]


def test_official_attempt_idempotent_submission(client):
    seed_official(public_id="plz-idem-1")
    ref = "official:plz-idem-1"
    uid = user_id(client)
    first = submit_attempt(client, ref, "A", submission_id="weak-net-selected-1")
    second = submit_attempt(client, ref, "A", submission_id="weak-net-selected-1")
    assert first.status_code == second.status_code == 200
    assert first.json()["attempt_id"] == second.json()["attempt_id"]
    assert second.json()["duplicate"] is True
    assert len(attempt_rows(uid, ref)) == 1
    assert wrong_row(uid, ref)["failures"] == 1
    # 不同 submission_id 是新的作答（允许重复练习），错题次数累加。
    third = submit_attempt(client, ref, "A", submission_id="weak-net-selected-2")
    assert third.json()["duplicate"] is False
    assert len(attempt_rows(uid, ref)) == 2
    assert wrong_row(uid, ref)["failures"] == 2
    # official 与 app/bank 的同一 submission_id 不互相吞并（幂等键含题目键）。
    assert question_ref.submission_key(ref, "x-1") == "official:plz-idem-1|x-1"
    invalid = submit_attempt(client, ref, "A", submission_id="bad id")
    assert invalid.status_code == 422


# ---------------------------------------------------------------------------
# 5. 撤回 / 争议：查询时立即不可见；历史记录保留但不回显正文
# ---------------------------------------------------------------------------

def test_official_withdraw_immediately_invisible_history_kept(client, install_bank):
    install_bank([bank_single()])
    seed_official(public_id="plz-withdraw-1")
    ref = "official:plz-withdraw-1"
    assert submit_attempt(client, ref, "A").json()["result"] == "incorrect"

    withdraw_official("plz-withdraw-1")

    # 详情/作答/复习解析按查询时可见性判定：撤回后立即 404。
    assert client.get("/api/questions/" + ref).status_code == 404
    assert submit_attempt(client, ref, "B").status_code == 404
    assert client.post("/api/mastery", json={"qid": ref, "level": "未掌握"}).status_code == 404
    listing = client.get("/api/questions", params={"source": "official"}).json()
    assert listing["total"] == 0 and listing["items"] == []
    merged = client.get("/api/questions", params={"source": "all"}).json()
    assert merged["official_total"] == 0

    # 历史成绩/错题保留：错题本显示「内容已撤回」占位，不回显受限正文与选项。
    box = client.get("/api/wrongbook").json()
    assert box["total"] == 1
    card = box["items"][0]
    assert card["qid"] == ref and card["source"] == "official"
    assert card["unavailable"] is True and card["withdrawn"] is True
    assert card["stem_plain"] == "" and card["options"] == []
    assert "撤回" in card["notice"]
    assert card["failures"] == 1
    # 复习队列同样保留历史（graded 组），但不回显正文。
    queue = client.get("/api/review/queue").json()
    assert ref in queue_refs(queue)["graded"]
    retained = next(item for item in queue["items"] if item["ref"] == ref)
    assert retained["unavailable"] is True and retained["stem_plain"] == ""


def test_official_disputed_invisible_and_placeholder(client):
    seed_official(public_id="plz-disputed-1", status="disputed", release_status="withdrawn",
                  snapshot=official_snapshot(stem="有争议的题干"))
    ref = "official:plz-disputed-1"
    assert client.get("/api/questions/" + ref).status_code == 404
    assert submit_attempt(client, ref, "B").status_code == 404
    assert client.get(
        "/api/questions", params={"source": "official"},
    ).json()["total"] == 0
    from app import official as official_module
    info = official_module.inspect_official("plz-disputed-1")
    assert info["visible"] is False
    assert info["exists"] is True
    assert info["review_status"] == "disputed"
    assert "暂停展示" in info["notice"]


def test_official_draft_and_pending_initial_not_leaked(client, second_client):
    seed_official(public_id="plz-draft-secret-1", status="draft", release=False)
    seed_official(public_id="plz-pending-secret-1", status="pending_initial", release=False)
    for pid in ("plz-draft-secret-1", "plz-pending-secret-1"):
        assert client.get(f"/api/questions/official:{pid}").status_code == 404
        assert second_client.get(f"/api/questions/official:{pid}").status_code == 404
    assert client.get("/api/questions", params={"source": "official"}).json()["total"] == 0
    assert second_client.get(
        "/api/questions", params={"source": "official"},
    ).json()["total"] == 0
    # 草稿/待初审的正文不进入练习侧任何读路径（含组卷快照）。
    response = client.post("/api/paperlab/papers", json={
        "title": "草稿题组卷", "kind": "manual", "subject": "数学",
        "items": [{"ref": "official:plz-draft-secret-1", "score": 5}],
    })
    assert response.status_code == 404, response.text


def test_official_published_requires_active_release(client):
    from app import db
    from app.db import connection, utcnow
    from app.plaza import db as plaza_db

    seed_official(public_id="plz-norelease-1", release=False)
    assert client.get("/api/questions/official:plz-norelease-1").status_code == 404
    assert client.get("/api/questions", params={"source": "official"}).json()["total"] == 0
    # 补上 active 发布指针后立即可见（每次查询实时判定，不缓存）。
    with connection() as con:
        db.write_lock(con, "plaza-write")
        row = con.execute(
            "SELECT * FROM plaza_submissions WHERE public_id=?", ("plz-norelease-1",)
        ).fetchone()
        plaza_db.insert_row(con, "plaza_releases", {
            "submission_id": int(row["id"]),
            "version": int(row["version"]),
            "public_id": "plz-norelease-1",
            "status": "active",
            "active_public_id": "plz-norelease-1",
            "published_by": 1,
            "published_at": utcnow(),
            "withdrawn_at": None,
            "withdraw_mode": "",
            "reason": "",
        })
    assert client.get("/api/questions/official:plz-norelease-1").status_code == 200
    assert client.get(
        "/api/questions", params={"source": "official"},
    ).json()["total"] == 1


def test_official_cross_user_public_but_private_state_isolated(client, second_client):
    seed_official(public_id="plz-public-1")
    ref = "official:plz-public-1"
    first_uid = user_id(client)
    assert submit_attempt(client, ref, "A").json()["result"] == "incorrect"

    # 公开题人人可读（含答案门控按各自作答状态判定）。
    other_detail = second_client.get("/api/questions/" + ref)
    assert other_detail.status_code == 200
    assert other_detail.json()["unlocked"] is False
    assert second_client.get("/api/attempts/history", params={"qid": ref}).json()["items"] == []
    assert second_client.get("/api/wrongbook").json()["total"] == 0
    assert second_client.get("/api/stats").json()["official_attempts"] == 0
    # 第一用户私态不受影响。
    assert first_uid == user_id(client)
    assert client.get("/api/attempts/history", params={"qid": ref}).json()["items"]


# ---------------------------------------------------------------------------
# 6. 非法引用 / 不存在的公开题
# ---------------------------------------------------------------------------

def test_official_invalid_or_missing_refs(client):
    assert client.get("/api/questions/official:").status_code == 422
    assert client.get("/api/questions/official:plz-does-not-exist").status_code == 404
    assert submit_attempt(client, "official:plz-does-not-exist", "A").status_code == 404
    assert submit_attempt(client, "official:", "A").status_code == 422
    response = client.post("/api/paperlab/papers", json={
        "title": "非法精选引用", "kind": "manual", "subject": "数学",
        "items": [{"ref": "official:"}],
    })
    assert response.status_code == 422, response.text
    response = client.post("/api/paperlab/papers", json={
        "title": "非法精选引用2", "kind": "manual", "subject": "数学",
        "items": [{"ref": "official:plz-does-not-exist", "score": 5}],
    })
    assert response.status_code == 404, response.text
    # 非法 public_id 格式 → 422。
    assert client.get("/api/questions/official:bad id").status_code == 422


# ---------------------------------------------------------------------------
# 7. 工坊组卷：official 引用冻结与回看
# ---------------------------------------------------------------------------

def test_official_paper_snapshot_frozen_and_review(client, install_bank):
    install_bank([bank_single()])
    pid = "plz-paper-1"
    seed_official(public_id=pid, snapshot=official_snapshot(stem="建卷时的题干"))
    ref = f"official:{pid}"

    created = create_paper(client, [{"ref": ref, "score": 5}])
    paper_id = created["paper"]["id"]
    item = created["items"][0]
    assert item["question_ref"] == ref
    assert item["source"] == "official"
    assert item["source_label"] == "平台精选"
    assert item["public_id"] == pid
    assert item["published_version"] == 1
    assert item["review_status"] == "featured"
    assert item["snapshot"]["content"]["stem"] == "建卷时的题干"
    assert item["snapshot"]["content"]["answers"] == ["B"]

    attempt = client.post(f"/api/paperlab/papers/{paper_id}/attempts").json()
    attempt_id = attempt["attempt"]["id"]
    assert attempt["items"][0]["source"] == "official"
    assert attempt["items"][0]["public_id"] == pid
    saved = client.patch(
        f"/api/paperlab/attempts/{attempt_id}/answers",
        json={"item_position": 1, "answer": ["A"]},
    )
    assert saved.status_code == 200, saved.text
    submitted = client.post(f"/api/paperlab/attempts/{attempt_id}/submit", json={})
    assert submitted.status_code == 200, submitted.text
    result = submitted.json()["result"]
    assert [entry["ref"] for entry in result["official_wrong_added"]] == [ref]
    assert result["wrong_added"] == []

    # 错题联动：official 引用写入统一错题本与复习队列。
    assert wrong_row(user_id(client), ref)["failures"] == 1
    assert ref in {item["ref"] for item in client.get("/api/wrongbook").json()["items"]}
    assert ref in queue_refs(client.get("/api/review/queue").json())["graded"]

    # 平台精选内容后续修订/撤回不影响已建试卷与已开始的练习（冻结语义不变，仅标注来源）。
    update_snapshot(pid, official_snapshot(stem="修订后的题干"))
    withdraw_official(pid)
    review = client.get(f"/api/paperlab/attempts/{attempt_id}")
    assert review.status_code == 200, review.text
    review_item = review.json()["items"][0]
    assert review_item["source"] == "official"
    assert review_item["public_id"] == pid
    assert review_item["content"]["stem"] == "建卷时的题干"
    paper_review = client.get(f"/api/paperlab/papers/{paper_id}")
    assert paper_review.json()["items"][0]["snapshot"]["content"]["stem"] == "建卷时的题干"


# ---------------------------------------------------------------------------
# 8. 社区待核验 / 已核验的来源标注
# ---------------------------------------------------------------------------

def test_official_community_and_verified_review_status(client):
    seed_official(public_id="plz-community-1", status="community_review", release=False,
                  subject="数学")
    seed_official(public_id="plz-verified-1", status="verified", release=False,
                  subject="物理", snapshot=official_snapshot(subject="物理"))
    data = client.get("/api/questions", params={"source": "official"}).json()
    by_ref = {item["qid"]: item for item in data["items"]}
    assert by_ref["official:plz-community-1"]["review_status"] == "community"
    assert by_ref["official:plz-community-1"]["community_pending"] is True
    assert by_ref["official:plz-community-1"]["featured"] is False
    assert "社区核验" in by_ref["official:plz-community-1"]["notice"]
    assert by_ref["official:plz-verified-1"]["review_status"] == "verified"
    # 社区待核验题可作答、判分，错题进统一队列。
    submitted = submit_attempt(client, "official:plz-community-1", "B")
    assert submitted.status_code == 200 and submitted.json()["result"] == "correct"
    detail = client.get("/api/questions/official:plz-community-1").json()
    assert detail["review_status"] == "community"
    assert detail["review_status_label"] == "社区待核验"


# ---------------------------------------------------------------------------
# 9. 真实「投稿 → 初审 → 评审阈值 → 签发 → 撤回」端到端
# ---------------------------------------------------------------------------

def _grant(admin, username, role, subject=""):
    response = admin.post(f"{PLAZA}/admin/roles", json={
        "action": "grant", "role": role, "subject": subject, "username": username,
    })
    assert response.status_code == 200, response.text


def test_official_real_publish_pipeline_end_to_end(client):
    admin = client
    created_clients = []
    try:
        author, author_info = _register("精选作者-WS19")
        created_clients.append(author)
        editor, editor_info = _register("精选编辑-WS19")
        created_clients.append(editor)
        _grant(admin, editor_info["username"], "editor")
        reviewers = []
        for index, role in enumerate(("subject_reviewer", "subject_reviewer",
                                      "reviewer", "reviewer", "reviewer")):
            reviewer, reviewer_info = _register(f"精选评审{index}-WS19")
            created_clients.append(reviewer)
            _grant(admin, reviewer_info["username"], role, "数学")
            reviewers.append(reviewer)

        question = author.post("/api/paperlab/questions", json={
            "type": "SINGLE", "subject": "数学",
            "content": {
                "stem": "端到端精选：2+2=？",
                "options": [{"label": "A", "text": "4"}, {"label": "B", "text": "5"}],
                "answers": ["A"], "explanation": "2+2=4。",
            },
        })
        assert question.status_code == 200, question.text
        question_id = question.json()["question"]["id"]
        source = {
            "url": "https://example.com/ws19/e2e", "page_no": "1", "question_no": "1",
            "original_author": "命题组", "license_basis": "原创授权",
            "rights_scope": "允许公开", "attribution": "署名：WS19",
        }
        created = author.post(f"{PLAZA}/submissions", json={
            "question_id": question_id, "source": source, "license_confirmed": True,
        })
        assert created.status_code == 201, created.text
        submission_id = created.json()["submission"]["id"]
        submitted = author.post(f"{PLAZA}/submissions/{submission_id}/submit", json={
            "version": 1, "confirm_preview": True,
        })
        assert submitted.status_code == 200, submitted.text
        accepted = editor.post(f"{PLAZA}/submissions/{submission_id}/initial-review", json={
            "version": 1, "decision": "accept", "reason": "",
            "source_checked": True, "privacy_checked": True, "completeness_checked": True,
        })
        assert accepted.status_code == 200, accepted.text
        for reviewer in reviewers:
            reviewed = reviewer.post(f"{PLAZA}/submissions/{submission_id}/reviews", json={
                "version": 1, "verdict": "approve", "reasons": "",
                "independent_answer": "独立作答得 A。", "conflict_declared": False,
            })
            assert reviewed.status_code == 201, reviewed.text
        detail = author.get(f"{PLAZA}/submissions/{submission_id}").json()["submission"]
        assert detail["status"] == "verified", detail
        published = editor.post(f"{PLAZA}/submissions/{submission_id}/publish", json={
            "version": 1, "reason": "核验通过，签发精选",
        })
        assert published.status_code == 200, published.text
        public_id = published.json().get("public_id") or detail.get("public_id")
        assert public_id
        ref = f"official:{public_id}"

        # 发布后：练习侧详情/列表可见，可作答判分。
        official_detail = author.get("/api/questions/" + ref)
        assert official_detail.status_code == 200, official_detail.text
        assert official_detail.json()["source"] == "official"
        assert official_detail.json()["review_status"] == "featured"
        assert official_detail.json()["published_version"] == 1
        listing = author.get("/api/questions", params={"source": "official"}).json()
        assert ref in {item["qid"] for item in listing["items"]}
        attempt = submit_attempt(author, ref, "A")
        assert attempt.status_code == 200 and attempt.json()["result"] == "correct"

        # 撤回：公开检索/详情/作答立即不可见；历史作答记录（attempts 行）保留在库中，
        # 但按统一可见性口径不再通过历史接口回显正文/内容。
        withdrawn = editor.post(f"{PLAZA}/releases/{public_id}/withdraw", json={
            "mode": "normal", "reason": "权利人申请撤回",
        })
        assert withdrawn.status_code == 200, withdrawn.text
        assert author.get("/api/questions/" + ref).status_code == 404
        assert submit_attempt(author, ref, "A").status_code == 404
        assert author.get(
            "/api/questions", params={"source": "official"},
        ).json()["total"] == 0
        assert author.get("/api/attempts/history", params={"qid": ref}).status_code == 404
        from app.db import connection

        with connection() as con:
            retained = con.execute(
                "SELECT COUNT(*) FROM attempts WHERE user_id=? AND qid=?",
                (author_info["id"], ref),
            ).fetchone()[0]
        assert retained == 1
    finally:
        for created_client in created_clients:
            created_client.close()


# ---------------------------------------------------------------------------
# 10. 工坊卷面回看标注来源（bank 卷不受新增字段影响）
# ---------------------------------------------------------------------------

def test_official_paper_bank_item_unaffected(client, install_bank):
    install_bank([bank_single()])
    created = create_paper(client, [{"ref": "bank:docQ:1", "score": 2}])
    item = created["items"][0]
    assert item["source"] == "bank"
    assert item["source_label"] == "高考题库"
    assert item["public_id"] == ""
    assert item["review_status"] == ""
    assert item["snapshot"]["content"]["stem"].startswith("已知 1+1=2")


# ---------------------------------------------------------------------------
# 11. 错题标签 / 归档 / 恢复 / 历史作答（official 写操作链）
# ---------------------------------------------------------------------------

def test_official_wrongbook_tags_archive_restore_and_history(client):
    seed_official(public_id="plz-wrongbook-1")
    ref = "official:plz-wrongbook-1"
    assert submit_attempt(client, ref, "A").json()["result"] == "incorrect"

    tagged = client.post("/api/wrongbook/tags", json={
        "qid": ref, "error_tags": ["审题失误"], "reason": "看错选项",
    })
    assert tagged.status_code == 200, tagged.text
    assert tagged.json()["qid"] == ref
    card = client.get("/api/wrongbook").json()["items"][0]
    assert card["error_tags"] == ["审题失误"]
    assert card["reason"] == "看错选项"

    assert client.delete("/api/wrongbook/" + ref).status_code == 200
    assert client.get("/api/wrongbook").json()["total"] == 0
    archived = client.get("/api/wrongbook", params={"include_archived": True}).json()
    assert archived["total"] == 1 and archived["items"][0]["archived"] is True
    assert client.post("/api/wrongbook/restore", json={"qid": ref}).status_code == 200
    assert client.get("/api/wrongbook").json()["total"] == 1

    history = client.get("/api/attempts/history", params={"qid": ref})
    assert history.status_code == 200
    assert len(history.json()["items"]) == 1
    assert history.json()["items"][0]["ref"] == ref
    assert history.json()["items"][0]["source"] == "official"


# ---------------------------------------------------------------------------
# 12. 列表分页与非法题型筛选
# ---------------------------------------------------------------------------

def test_official_list_pagination_and_invalid_type_filter(client):
    for index in range(3):
        seed_official(public_id=f"plz-page-{index}", subject="数学")
    first = client.get(
        "/api/questions", params={"source": "official", "page": 1, "page_size": 2},
    ).json()
    assert first["total"] == 3 and len(first["items"]) == 2
    second = client.get(
        "/api/questions", params={"source": "official", "page": 2, "page_size": 2},
    ).json()
    assert second["total"] == 3 and len(second["items"]) == 1
    first_refs = {item["qid"] for item in first["items"]}
    second_refs = {item["qid"] for item in second["items"]}
    assert not (first_refs & second_refs)
    assert len(first_refs | second_refs) == 3
    bad = client.get(
        "/api/questions", params={"source": "official", "type": "not_a_question_type"},
    )
    assert bad.status_code == 422


# ---------------------------------------------------------------------------
# 13. 三种来源（bank/app/official）在同一错题本与复习队列合并
# ---------------------------------------------------------------------------

def test_wrongbook_merges_bank_app_official_sources(client, install_bank):
    install_bank([bank_single()])
    seed_official(public_id="plz-merge-1")
    app_question = client.post("/api/paperlab/questions", json={
        "type": "SINGLE", "subject": "数学",
        "content": {
            "stem": "个人题：2+3=？",
            "options": [{"label": "A", "text": "5"}, {"label": "B", "text": "6"}],
            "answers": ["A"], "explanation": "2+3=5。",
        },
    })
    assert app_question.status_code == 200, app_question.text
    app_ref = f"app:{app_question.json()['question']['id']}"
    assert submit_attempt(client, "docQ:1", "B").json()["result"] == "incorrect"
    assert submit_attempt(client, app_ref, "B").json()["result"] == "incorrect"
    assert submit_attempt(client, "official:plz-merge-1", "A").json()["result"] == "incorrect"

    box = client.get("/api/wrongbook").json()
    assert box["total"] == 3
    by_source = {}
    for item in box["items"]:
        by_source.setdefault(item["source"], set()).add(item["ref"])
    assert set(by_source) == {"bank", "app", "official"}
    assert by_source["official"] == {"official:plz-merge-1"}
    assert by_source["app"] == {app_ref}
    assert by_source["bank"] == {"bank:docQ:1"}
    graded = queue_refs(client.get("/api/review/queue").json())["graded"]
    assert {"bank:docQ:1", app_ref, "official:plz-merge-1"} <= graded
