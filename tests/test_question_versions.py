"""WS10 · 个人收录版本与「有新版」（Phase 1 剩余 · 方案 §6.2/§6.3）验收。

覆盖：
1. 收录幂等：同用户重复收录同版本复用条目，不产生重复收藏；
2. 固定版本：题库更新后收藏版内容不变，``source-status`` 报告 ``has_new_version`` 与差异字段；
3. 更新：``update-from-source`` 必须显式 ``confirm``，生成新版本且旧版本内容不可变；
4. 个人题：创建即版本 1，PATCH 生成新版本，版本内容只读（历史行不被改写）；
5. 来源缺失降级：题库下架后 ``source_available=false``，不拉取/回显受限正文，收藏版仍可读；
6. 跨用户隔离：他人题目版本/来源/更新一律 404；第二用户收录得到独立条目；
7. 附图：题库只读配图不伪造个人资产引用（记录提示）；个人题版本记录附图资产引用。
"""
import json
import sqlite3
from contextlib import closing
from io import BytesIO

import pytest
from PIL import Image

from app import db
from app.paperlab.db import init_tables

BANK_QID = "rwk:version-fixture:1-1"
OLD_STEM = "已知 $f(x)=x^2$，则 $f(2)$ 等于？"
NEW_STEM = "已知 $f(x)=x^3$，则 $f(2)$ 等于？"


# ---------------------------------------------------------------------------
# helpers / fixture
# ---------------------------------------------------------------------------

def png_bytes(color=(20, 60, 200)) -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (32, 32), color).save(buffer, "PNG")
    return buffer.getvalue()


def bank_question(qid: str, stem: str, **overrides) -> dict:
    from bank_fixture import question_row

    row = question_row(
        qid, stem,
        doc_id="fixture-doc",
        options_json=json.dumps(
            [{"label": "A", "text": "2"}, {"label": "B", "text": "4"}],
            ensure_ascii=False,
        ),
        answer_text="B", answer_kind="choice",
        solution="代入得 4。", images_json="[]",
        practice_status="practice", grading_status="auto",
        bank_version="bank-fixture-r5",
    )
    row.update(overrides)
    return row


def revision_bank(root, rows, revisions) -> object:
    """最小题库 + r5 的 content_revision/answer_revision 列（模拟可检测更新的发布包）。"""
    from bank_fixture import make_bank

    base = make_bank(root, rows)
    with closing(sqlite3.connect(base / "题库.db")) as con:
        con.execute("ALTER TABLE questions ADD COLUMN content_revision TEXT")
        con.execute("ALTER TABLE questions ADD COLUMN answer_revision TEXT")
        for qid, (content_revision, answer_revision) in revisions.items():
            con.execute(
                "UPDATE questions SET content_revision=?, answer_revision=? WHERE qid=?",
                (content_revision, answer_revision, qid),
            )
        con.commit()
    return base


def update_bank_row(bank, qid: str, **fields) -> None:
    """直接修改临时题库行，模拟官方题面/答案更新或下架。"""
    if not fields:
        return
    columns = ", ".join(f"{column}=?" for column in fields)
    with closing(sqlite3.connect(bank / "题库.db")) as con:
        con.execute(f"UPDATE questions SET {columns} WHERE qid=?", (*fields.values(), qid))
        con.commit()


def delete_bank_row(bank, qid: str) -> None:
    with closing(sqlite3.connect(bank / "题库.db")) as con:
        con.execute("DELETE FROM questions WHERE qid=?", (qid,))
        con.commit()


def collect(client, qid: str = BANK_QID):
    return client.post("/api/paperlab/questions/collect", json={"qid": qid})


def versions_of(client, question_id: int):
    return client.get(f"/api/paperlab/questions/{question_id}/versions")


def source_status_of(client, question_id: int):
    return client.get(f"/api/paperlab/questions/{question_id}/source-status")


def create_question(client, **overrides) -> dict:
    payload = {
        "type": "SUBJECTIVE",
        "subject": "数学",
        "grade": "高中一年级",
        "knowledge_tags": ["函数"],
        "content": {"stem": "证明函数 $f(x)=x^2$ 在 $[0,+\\infty)$ 上单调递增。"},
        "status": "draft",
    }
    payload.update(overrides)
    response = client.post("/api/paperlab/questions", json=payload)
    assert response.status_code == 200, response.text
    return response.json()["question"]


@pytest.fixture()
def bank_env(client, tmp_path, monkeypatch):
    """把只读题库切到带 revision 的临时发布包（不改真实题库）。"""
    from app import practice

    bank = revision_bank(
        tmp_path / "bank",
        [bank_question(BANK_QID, OLD_STEM)],
        {BANK_QID: ("rev-content-1", "rev-answer-1")},
    )
    monkeypatch.setattr(db, "BANK_DIR", bank)
    practice._source_groups.cache_clear()
    practice._sqlite_question_columns.cache_clear()
    practice._bank_version_cached.cache_clear()
    yield bank
    practice._source_groups.cache_clear()
    practice._sqlite_question_columns.cache_clear()
    practice._bank_version_cached.cache_clear()


# ---------------------------------------------------------------------------
# 1. 收录：固定版本 + 幂等
# ---------------------------------------------------------------------------

def test_collect_snapshots_current_version_and_reuses_same_version(client, bank_env):
    response = collect(client)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["collected"] is True and body["already_collected"] is False
    question = body["question"]
    question_id = question["id"]

    # 收录内容来自题库当前题面/选项/答案/解析；个人快照默认私有。
    assert question["source_kind"] == "bank"
    assert question["source_ref"] == f"bank:{BANK_QID}"
    assert question["source_question_id"] == BANK_QID
    assert question["content"]["stem"] == OLD_STEM
    assert question["content"]["options"] == [{"label": "A", "text": "2"}, {"label": "B", "text": "4"}]
    assert question["content"]["answers"] == ["B"]
    assert question["content"]["explanation"] == "代入得 4。"
    assert question["status"] == "draft"
    assert question["verify_status"] == "pending"
    assert question["version"] == 1
    assert question["source_content_revision"] == "rev-content-1"
    assert question["source_answer_revision"] == "rev-answer-1"
    assert question["collected_at"]
    assert question["content_sha256"]
    # 不自动公开：没有任何公开读取端点，且状态不是 ready/published。
    assert body["notice"]

    # 同版本重复收录：复用条目，不产生第二道收藏。
    again = collect(client)
    assert again.status_code == 200
    reused = again.json()
    assert reused["already_collected"] is True
    assert reused["question_id"] == question_id
    assert reused["has_new_version"] is False
    assert client.get("/api/paperlab/questions").json()["total"] == 1

    # 创建即固定版本 1 的历史行。
    history = versions_of(client, question_id).json()
    assert history["total"] == 1
    item = history["items"][0]
    assert item["version"] == 1 and item["origin"] == "collect"
    assert item["is_current"] is True
    assert item["content"]["stem"] == OLD_STEM
    assert item["source_ref"] == f"bank:{BANK_QID}"
    assert item["source_content_revision"] == "rev-content-1"


def test_collect_rejects_unknown_or_invalid_qid(client, bank_env):
    assert collect(client, "rwk:version-fixture:does-not-exist").status_code == 404
    assert collect(client, "  ").status_code == 422
    assert collect(client, "../../etc/passwd").status_code == 422


# ---------------------------------------------------------------------------
# 2. 固定版本：题库更新后收藏版不变，「有新版」可检测
# ---------------------------------------------------------------------------

def test_collected_copy_stays_fixed_after_bank_update(client, bank_env):
    question_id = collect(client).json()["question_id"]
    update_bank_row(
        bank_env, BANK_QID,
        stem_plain=NEW_STEM, answer_text="A",
        content_revision="rev-content-2", answer_revision="rev-answer-2",
    )

    detail = client.get(f"/api/paperlab/questions/{question_id}").json()["question"]
    assert detail["content"]["stem"] == OLD_STEM
    assert detail["content"]["answers"] == ["B"]
    assert detail["version"] == 1
    # 已收藏内容仍指向原版本（source 元数据不被静默改写）。
    assert detail["source_content_revision"] == "rev-content-1"

    status = source_status_of(client, question_id).json()
    assert status["source_available"] is True
    assert status["has_new_version"] is True
    assert "rev-content-1" in status["collected_revision"]
    assert "rev-content-2" in status["current_revision"]
    assert status["collected_content_revision"] == "rev-content-1"
    assert status["current_content_revision"] == "rev-content-2"
    assert "题干" in status["diff"]["changed"]
    assert "答案" in status["diff"]["changed"]
    assert "新版" in status["notice"]

    # 列表批量徽标同样报告有新版。
    listing = client.get("/api/paperlab/questions").json()["items"]
    assert listing[0]["has_new_version"] is True

    # 重复「收录」不会静默覆盖收藏版，只提示有新版。
    reused = collect(client).json()
    assert reused["already_collected"] is True
    assert reused["has_new_version"] is True
    assert client.get(f"/api/paperlab/questions/{question_id}").json()["question"]["version"] == 1


def test_source_status_reports_up_to_date_after_update(client, bank_env):
    question_id = collect(client).json()["question_id"]
    update_bank_row(
        bank_env, BANK_QID, stem_plain=NEW_STEM,
        content_revision="rev-content-2", answer_revision="rev-answer-2",
    )
    updated = client.post(
        f"/api/paperlab/questions/{question_id}/update-from-source",
        json={"confirm": True, "expected_version": 1},
    )
    assert updated.status_code == 200, updated.text
    status = source_status_of(client, question_id).json()
    assert status["has_new_version"] is False
    assert status["source_available"] is True


# ---------------------------------------------------------------------------
# 3. 更新：显式确认 + 新版本 + 旧版本不可变
# ---------------------------------------------------------------------------

def test_update_from_source_requires_confirm_and_creates_new_version(client, bank_env):
    question_id = collect(client).json()["question_id"]
    update_bank_row(
        bank_env, BANK_QID,
        stem_plain=NEW_STEM, answer_text="A",
        content_revision="rev-content-2", answer_revision="rev-answer-2",
    )

    # 未确认：拒绝（不静默更新）。
    refused = client.post(f"/api/paperlab/questions/{question_id}/update-from-source", json={})
    assert refused.status_code == 422
    assert "确认" in refused.json()["detail"]
    assert client.get(f"/api/paperlab/questions/{question_id}").json()["question"]["version"] == 1

    # 版本冲突：409。
    conflict = client.post(
        f"/api/paperlab/questions/{question_id}/update-from-source",
        json={"confirm": True, "expected_version": 99},
    )
    assert conflict.status_code == 409

    updated = client.post(
        f"/api/paperlab/questions/{question_id}/update-from-source",
        json={"confirm": True, "expected_version": 1},
    )
    assert updated.status_code == 200, updated.text
    body = updated.json()
    assert body["updated"] is True
    assert body["previous_version"] == 1 and body["version"] == 2
    assert body["question"]["content"]["stem"] == NEW_STEM
    assert body["question"]["content"]["answers"] == ["A"]
    assert body["question"]["source_content_revision"] == "rev-content-2"
    assert body["question"]["version"] == 2
    assert "题干" in (body["diff"] or {}).get("changed", [])

    history = versions_of(client, question_id).json()
    assert history["total"] == 2
    by_version = {item["version"]: item for item in history["items"]}
    # 旧版本内容不可变：仍是最初收藏时的题面/答案。
    assert by_version[1]["content"]["stem"] == OLD_STEM
    assert by_version[1]["content"]["answers"] == ["B"]
    assert by_version[1]["origin"] == "collect"
    assert by_version[1]["is_current"] is False
    assert by_version[2]["content"]["stem"] == NEW_STEM
    assert by_version[2]["origin"] == "source_update"
    assert by_version[2]["is_current"] is True

    # 再更新：已是最新版本，不再生成版本行。
    again = client.post(
        f"/api/paperlab/questions/{question_id}/update-from-source",
        json={"confirm": True},
    ).json()
    assert again["updated"] is False
    assert versions_of(client, question_id).json()["total"] == 2


# ---------------------------------------------------------------------------
# 4. 个人题创建/编辑的不可变版本
# ---------------------------------------------------------------------------

def test_personal_question_create_and_edit_create_immutable_versions(client):
    question = create_question(client)
    question_id = question["id"]
    assert question["version"] == 1

    first = versions_of(client, question_id).json()
    assert first["total"] == 1
    assert first["items"][0]["version"] == 1
    assert first["items"][0]["origin"] == "create"
    assert first["items"][0]["is_current"] is True

    patched = client.patch(
        f"/api/paperlab/questions/{question_id}",
        json={"version": 1, "content": {"stem": "修改后的题干：求导。"}},
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["question"]["version"] == 2

    second = versions_of(client, question_id).json()
    assert second["total"] == 2
    by_version = {item["version"]: item for item in second["items"]}
    assert by_version[1]["content"]["stem"].startswith("证明函数")
    assert by_version[1]["is_current"] is False
    assert by_version[2]["content"]["stem"] == "修改后的题干：求导。"
    assert by_version[2]["origin"] == "edit"
    assert by_version[2]["is_current"] is True

    # 再次编辑：版本 3，历史版本内容仍不可变。
    client.patch(
        f"/api/paperlab/questions/{question_id}",
        json={"version": 2, "knowledge_tags": ["函数", "导数"]},
    )
    third = versions_of(client, question_id).json()
    assert third["total"] == 3
    assert max(item["version"] for item in third["items"]) == 3
    by_version = {item["version"]: item for item in third["items"]}
    assert by_version[1]["content"]["stem"].startswith("证明函数")
    assert by_version[2]["content"]["stem"] == "修改后的题干：求导。"
    assert by_version[3]["knowledge_tags"] == ["函数", "导数"]


def test_personal_question_version_records_asset_refs(client):
    from app.paperlab import assets

    user_id = client.get("/api/auth/me").json()["id"]
    asset_id = assets.save_asset_bytes(user_id, png_bytes())
    question = create_question(
        client, content={
            "stem": "看图作答：这条直线斜率是多少？",
            "images": [{"asset": asset_id, "role": "stem"}],
        },
    )
    history = versions_of(client, question["id"]).json()
    assert history["items"][0]["asset_refs"] == [asset_id]
    assert history["items"][0]["content"]["images"][0]["asset"] == asset_id


def test_personal_question_edit_cas_still_rejects_stale_version(client):
    question = create_question(client)
    conflict = client.patch(
        f"/api/paperlab/questions/{question['id']}",
        json={"version": 99, "status": "ready"},
    )
    assert conflict.status_code == 409
    assert versions_of(client, question["id"]).json()["total"] == 1


# ---------------------------------------------------------------------------
# 5. 来源缺失降级（普通下架：保留收藏版，不拉取受限正文）
# ---------------------------------------------------------------------------

def test_missing_source_degrades_without_leaking_and_keeps_collected_copy(client, bank_env):
    question_id = collect(client).json()["question_id"]
    delete_bank_row(bank_env, BANK_QID)

    status = source_status_of(client, question_id).json()
    assert status["source_available"] is False
    assert status["has_new_version"] is False
    assert status["source_restricted"] is False
    assert status["current_revision"] is None
    assert "下架" in status["notice"]

    # 收藏版仍可读（普通下架在授权允许范围内保留已收录版本）。
    detail = client.get(f"/api/paperlab/questions/{question_id}").json()["question"]
    assert detail["content"]["stem"] == OLD_STEM

    # 更新请求优雅降级：不生成新版本、不回显来源正文。
    updated = client.post(
        f"/api/paperlab/questions/{question_id}/update-from-source",
        json={"confirm": True},
    )
    assert updated.status_code == 200, updated.text
    body = updated.json()
    assert body["updated"] is False
    assert body["source_available"] is False
    assert body["has_new_version"] is False
    assert versions_of(client, question_id).json()["total"] == 1

    listing = client.get("/api/paperlab/questions").json()["items"]
    assert listing[0]["source_available"] is False


# ---------------------------------------------------------------------------
# 6. 跨用户隔离
# ---------------------------------------------------------------------------

def test_versions_are_owner_scoped_and_collect_is_per_user(client, second_client, bank_env):
    question_id = collect(client).json()["question_id"]

    assert second_client.get(
        f"/api/paperlab/questions/{question_id}/versions"
    ).status_code == 404
    assert second_client.get(
        f"/api/paperlab/questions/{question_id}/source-status"
    ).status_code == 404
    assert second_client.post(
        f"/api/paperlab/questions/{question_id}/update-from-source",
        json={"confirm": True},
    ).status_code == 404
    assert second_client.get("/api/paperlab/questions").json()["total"] == 0

    # 第二用户自己收录同一道题：得到独立条目与独立版本历史。
    other = collect(second_client).json()
    assert other["question_id"] != question_id
    assert second_client.get("/api/paperlab/questions").json()["total"] == 1
    assert client.get("/api/paperlab/questions").json()["total"] == 1
    assert versions_of(second_client, other["question_id"]).json()["total"] == 1


# ---------------------------------------------------------------------------
# 7. 附图：题库只读素材不伪造引用；个人资产引用被版本记录
# ---------------------------------------------------------------------------

def test_collect_records_image_hint_instead_of_fake_reference(client, bank_env):
    update_bank_row(
        bank_env, BANK_QID,
        images_json=json.dumps([{"file": "images/fixture.png", "role": "figure"}]),
        content_revision="rev-image-1",
    )
    response = collect(client)
    assert response.status_code == 200, response.text
    body = response.json()
    question = body["question"]
    # 不伪造个人资产引用，也不复制只读题库素材。
    assert question["content"]["images"] == []
    assert question["collect_warnings"]
    assert any("配图" in warning for warning in question["collect_warnings"])
    assert body["warnings"] == question["collect_warnings"]
    # 收录的版本行同样不包含伪造的附图引用。
    assert versions_of(client, question["id"]).json()["items"][0]["asset_refs"] == []


def test_source_status_for_personal_question_has_no_bank_source(client):
    """非题库收录题：没有题库来源版本，也不允许从题库更新。"""
    question = create_question(client)
    status = source_status_of(client, question["id"]).json()
    assert status["source_kind"] in ("manual", "import")
    assert status["source_ref"] == ""
    assert status["source_available"] is None
    assert status["has_new_version"] is False
    assert "不是题库收录题" in status["notice"]

    refused = client.post(
        f"/api/paperlab/questions/{question['id']}/update-from-source",
        json={"confirm": True},
    )
    assert refused.status_code == 422
    assert "不是题库收录题" in refused.json()["detail"]


def test_init_tables_adds_version_table_and_source_columns(client):
    """建表幂等：版本表存在，来源列经 ensure_column 存在（老库增量迁移口径）。"""
    init_tables()
    init_tables()
    with db.connection() as con:
        tables = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        columns = {row[1] for row in con.execute("PRAGMA table_info(pl_questions)")}
    assert "pl_question_versions" in tables
    for column in ("content_sha256", "source_ref", "source_kind", "source_question_id",
                   "source_bank_version", "source_content_revision", "source_answer_revision",
                   "source_meta_json", "collected_at"):
        assert column in columns


# ---------------------------------------------------------------------------
# 8. 老库增量迁移 + 懒补历史（不 drop / 重建 pl_questions）
# ---------------------------------------------------------------------------

_LEGACY_QUESTION_SCHEMA = """
CREATE TABLE users (id INTEGER PRIMARY KEY);
CREATE TABLE pl_questions (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    source TEXT NOT NULL DEFAULT 'manual',
    type TEXT NOT NULL DEFAULT 'SUBJECTIVE',
    subject TEXT NOT NULL DEFAULT '',
    grade TEXT NOT NULL DEFAULT '',
    difficulty REAL,
    knowledge_tags_json TEXT NOT NULL DEFAULT '[]',
    content_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'draft',
    verify_status TEXT NOT NULL DEFAULT 'pending',
    version INTEGER NOT NULL DEFAULT 1,
    ai_job_id INTEGER,
    ai_model TEXT NOT NULL DEFAULT '',
    prompt_version TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    deleted_at TEXT
);
"""


def test_init_tables_migrates_legacy_questions_and_backfills_history(tmp_path, monkeypatch):
    from app import db as app_db
    from app.paperlab import versions

    legacy = tmp_path / "legacy-data"
    legacy.mkdir()
    with closing(sqlite3.connect(legacy / "app.sqlite3")) as con:
        con.executescript(_LEGACY_QUESTION_SCHEMA)
        con.execute("INSERT INTO users(id) VALUES (1)")
        con.execute(
            "INSERT INTO pl_questions(user_id,type,subject,content_json,created_at,updated_at) "
            "VALUES (1,'SUBJECTIVE','数学','{\"stem\":\"迁移前的旧题\"}',?,?)",
            ("2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"),
        )
        con.commit()
    monkeypatch.setattr(app_db, "DATA_DIR", legacy)
    init_tables()
    with app_db.connection() as con:
        tables = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        columns = {row[1] for row in con.execute("PRAGMA table_info(pl_questions)")}
        row = con.execute("SELECT * FROM pl_questions WHERE id=1").fetchone()
    assert "pl_question_versions" in tables
    assert {"content_sha256", "source_ref", "source_kind", "source_question_id",
            "source_bank_version", "source_content_revision", "source_answer_revision",
            "source_meta_json", "collected_at"} <= columns
    legacy_row = dict(row)
    # 老数据原样保留，不 drop / 重建。
    assert legacy_row["content_json"] and legacy_row["source_ref"] == ""
    assert legacy_row["content_sha256"] == ""

    # 首次读取版本历史时懒补一条 origin=snapshot，并回填内容指纹。
    versions.ensure_history(legacy_row)
    with app_db.connection() as con:
        version_rows = [dict(item) for item in con.execute(
            "SELECT * FROM pl_question_versions ORDER BY version"
        )]
        digest = con.execute("SELECT content_sha256 FROM pl_questions WHERE id=1").fetchone()[0]
    assert len(version_rows) == 1
    assert version_rows[0]["origin"] == "snapshot"
    assert version_rows[0]["version"] == 1
    assert version_rows[0]["content_sha256"] == versions.content_sha256({"stem": "迁移前的旧题"})
    assert digest == version_rows[0]["content_sha256"]


def test_new_version_detected_for_legacy_bank_without_revision_columns(client, tmp_path, monkeypatch):
    """旧库没有 content/answer revision 列时，退化为内容指纹比较仍能发现新版。"""
    from bank_fixture import make_bank, question_row
    from app import practice

    bank = make_bank(tmp_path / "legacy-bank", [question_row(BANK_QID, OLD_STEM)])
    monkeypatch.setattr(db, "BANK_DIR", bank)
    practice._source_groups.cache_clear()
    practice._sqlite_question_columns.cache_clear()
    practice._bank_version_cached.cache_clear()
    try:
        question_id = collect(client).json()["question_id"]
        update_bank_row(bank, BANK_QID, stem_plain=NEW_STEM, answer_text="A")
        status = source_status_of(client, question_id).json()
        assert status["source_available"] is True
        assert status["has_new_version"] is True
        assert "题干" in status["diff"]["changed"]
    finally:
        practice._source_groups.cache_clear()
        practice._sqlite_question_columns.cache_clear()
        practice._bank_version_cached.cache_clear()


def test_restricted_source_hides_content_and_blocks_update(client, bank_env, monkeypatch):
    """来源受限（复核/撤回）：source_available=false，不拉取/回显受限正文。"""
    from app import practice

    question_id = collect(client).json()["question_id"]
    monkeypatch.setattr(practice, "QUARANTINED_QUESTIONS", frozenset({BANK_QID}))

    status = source_status_of(client, question_id).json()
    assert status["source_available"] is False
    assert status["source_restricted"] is True
    assert status["has_new_version"] is False
    assert status["current_revision"] is None
    assert "复核" in status["notice"] or "受限" in status["notice"]

    # 已收录版本仍可读（个人历史保留），不把受限来源正文写进收藏。
    detail = client.get(f"/api/paperlab/questions/{question_id}").json()["question"]
    assert detail["content"]["stem"] == OLD_STEM

    updated = client.post(
        f"/api/paperlab/questions/{question_id}/update-from-source",
        json={"confirm": True},
    )
    assert updated.status_code == 200
    body = updated.json()
    assert body["updated"] is False
    assert body["source_available"] is False
    assert body["source_restricted"] is True
    assert versions_of(client, question_id).json()["total"] == 1
