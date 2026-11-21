"""专题报告保存为带出处的笔记（M7, §5.3``save_report_as_note``）测试。

隔离测试用本地 ``FastAPI()`` 只挂 ``topic_report.router``，通过
``app.dependency_overrides[current_user]`` 注入当前用户（切换 holder 模拟两个账号）；数据库由
conftest 的 ``isolated_db`` 指向 tmp_path：``db.init_db()`` + ``ai.init_ai()``（建 ``ai_documents`` /
``ai_notes`` 并补 ``source_versions_json`` 等列）+ ``init_topic_reports()``；报告用真实
``topic_report.create_report`` 生成，不伪造内容。

覆盖口径：
* 保存成功：``ai_notes`` 行存在、标题 / 正文（报告正文 +「## 出处清单」）、``sources_json``
  精简来源、``source_versions_json`` 文档版本、返回结构 ``saved=true`` 且不返回全文；
* 重复保存：``saved=false`` + ``duplicate_of_note_id``，笔记行数不增加；报告改动后再保存新建；
* 他人报告 / 不存在报告 404（中文，不泄露存在性），且不产生笔记；
* 报告引用的资料已删除时仍能保存：``document_id`` 为空、``source_versions_json`` 记录失效说明；
* ``_note_content_from_report``：出处清单逐条列出来源、quote 前 120 字、幂等不重复追加；
* 正文超长：monkeypatch 上限后截断并注明；
* 两账号隔离：笔记 ``user_id`` 正确，JSON 列可解析且字段类型合法；
* 真实应用 ``client`` 集成：上传资料 → 建报告 → save-note → ``GET /api/notes/{id}`` 可读。
"""
from __future__ import annotations

import json

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
import pytest

from app import ai, db, practice, topic_report
from app.auth import current_user

USER_A = {"id": 9501, "username": "笔记甲", "role": "student"}
USER_B = {"id": 9502, "username": "笔记乙", "role": "student"}

TOPIC = "二次函数"
BASE = "/api/topic-reports"

DOC_A = 95101
DOC_B = 95103
DOC_A_TITLE = "数学讲义（笔记测试）"
DOC_B_TITLE = "物理讲义（他人资料）"

DOC_A_CHUNKS = [
    {
        "page": 1,
        "chunk_id": "p1-c1",
        "heading_path": "第一章 函数",
        "text": "1.1 二次函数\n二次函数的图像是抛物线，顶点与对称轴是核心概念。",
    },
    {
        "page": 2,
        "chunk_id": "p2-c1",
        "heading_path": "第二章 导数",
        "text": "Derivative 表示函数的变化率，记作 f'(x)。",
    },
]
DOC_B_CHUNKS = [
    {
        "page": 1,
        "chunk_id": "p1-c1",
        "heading_path": "第一章 运动",
        "text": "位移随时间的变化率称为速度。",
    },
]


# ---------------------------------------------------------------------------
# 夹具与造数工具
# ---------------------------------------------------------------------------


def _insert_user(con, user) -> None:
    con.execute(
        "INSERT INTO users(id,username,password_hash,role,created_at) VALUES (?,?,?,?,?)",
        (user["id"], user["username"], "unit-test-hash", "student", db.utcnow()),
    )


def _insert_document(con, document_id, user_id, title, chunks, *, pages=2) -> None:
    con.execute(
        "INSERT INTO ai_documents(id,user_id,title,storage_name,pages,status,chunks_json,created_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (
            document_id, user_id, title, "doc-%s.txt" % document_id, pages, "ready",
            json.dumps(chunks, ensure_ascii=False), db.utcnow(),
        ),
    )


def _note_rows(user_id) -> list[dict]:
    with db.connection() as con:
        rows = con.execute(
            "SELECT * FROM ai_notes WHERE user_id=? ORDER BY id", (int(user_id),)
        ).fetchall()
    return [dict(row) for row in rows]


def _all_note_count() -> int:
    with db.connection() as con:
        return int(con.execute("SELECT COUNT(*) FROM ai_notes").fetchone()[0])


@pytest.fixture
def note_env(isolated_db, monkeypatch):
    """隔离应用库 + 两个账号各一份资料与一份专题报告；返回报告编号。"""
    db.init_db()
    ai.init_ai()
    topic_report.init_topic_reports()
    with db.connection() as con:
        _insert_user(con, USER_A)
        _insert_user(con, USER_B)
        _insert_document(con, DOC_A, USER_A["id"], DOC_A_TITLE, DOC_A_CHUNKS)
        _insert_document(con, DOC_B, USER_B["id"], DOC_B_TITLE, DOC_B_CHUNKS, pages=1)
    # 隔离测试不挂真实题库：候选题检索固定返回空，报告其余部分照常规则汇编。
    monkeypatch.setattr(practice, "search_similar_questions", lambda *args, **kwargs: [])
    report_a = topic_report.create_report(
        USER_A["id"], topic=TOPIC, document_ids=[DOC_A]
    )["report"]
    report_b = topic_report.create_report(
        USER_B["id"], topic=TOPIC, document_ids=[DOC_B]
    )["report"]
    return {"report_a": report_a, "report_b": report_b}


@pytest.fixture
def note_api(note_env):
    """本模块 router 的独立小应用；holder 切换当前用户。"""
    app = FastAPI()
    app.include_router(topic_report.router)
    holder = {"current": dict(USER_A)}
    app.dependency_overrides[current_user] = lambda: holder["current"]
    with TestClient(app) as client:
        yield client, holder


# ---------------------------------------------------------------------------
# 保存成功：笔记行 / 字段映射 / 出处清单
# ---------------------------------------------------------------------------


def test_save_report_as_note_success(note_env):
    """保存成功：ai_notes 行、标题、正文含报告正文与「出处清单」、来源与文档版本字段正确。"""
    report = note_env["report_a"]
    result = topic_report.save_report_as_note(USER_A["id"], report["report_id"])

    assert result["saved"] is True
    assert result["report_id"] == report["report_id"]
    note = result["note"]
    assert set(note) == {"id", "title", "created_at", "source_count", "content_bytes"}
    assert "content" not in note  # 不返回全文，避免大响应
    assert int(note["id"]) >= 1
    assert note["title"] == "专题报告 · %s" % TOPIC
    assert len(note["title"]) <= topic_report.NOTE_TITLE_MAX_LENGTH

    rows = _note_rows(USER_A["id"])
    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == note["id"]
    assert row["user_id"] == USER_A["id"]
    assert row["title"] == note["title"]
    assert row["document_id"] == DOC_A
    # 报告正文保留 + 追加真实出处清单（只追加一次）。
    assert "## 二、资料依据" in row["content"]
    assert "二次函数的图像是抛物线" in row["content"]
    assert "## 出处清单" in row["content"]
    assert row["content"].count("## 出处清单") == 1
    assert "资料：《%s》" % DOC_A_TITLE in row["content"]
    assert "第 1 页" in row["content"] and "chunk p1-c1" in row["content"]
    assert "出处 ai_documents:%s" % DOC_A in row["content"]
    assert note["content_bytes"] == len(row["content"].encode("utf-8"))
    assert len(row["content"]) <= topic_report.NOTE_CONTENT_MAX_CHARS
    assert row["created_at"] and row["updated_at"] == row["created_at"]

    # sources_json：精简真实来源（kind/id/title/page/chunk_id/quote），JSON 可解析。
    sources = json.loads(row["sources_json"])
    assert isinstance(sources, list) and sources
    assert note["source_count"] == len(sources)
    doc_source = sources[0]
    assert doc_source["kind"] == "document"
    assert doc_source["id"] == DOC_A
    assert doc_source["title"] == DOC_A_TITLE
    assert doc_source["page"] == 1
    assert doc_source["chunk_id"] == "p1-c1"
    assert doc_source["quote"] and len(doc_source["quote"]) <= topic_report.NOTE_SOURCE_QUOTE_CHARS
    assert set(doc_source) <= {
        "kind", "id", "title", "ref", "page", "chunk_id", "version", "quote",
    }

    # source_versions_json：{"<document_id>": ai_documents.doc_version}（默认 1）。
    versions = json.loads(row["source_versions_json"])
    assert versions == {str(DOC_A): 1}
    assert row["kp_id"] is None and row["subject"] is None


def test_save_report_as_note_duplicate_and_changed_report(note_env):
    """重复保存同一版本：不重复插入；报告改动后再保存则新建一份笔记。"""
    report = note_env["report_a"]
    first = topic_report.save_report_as_note(USER_A["id"], report["report_id"])
    second = topic_report.save_report_as_note(USER_A["id"], report["report_id"])

    assert first["saved"] is True
    assert second["saved"] is False
    assert second["duplicate_of_note_id"] == first["note"]["id"]
    assert second["note"]["id"] == first["note"]["id"]
    assert second["report_id"] == report["report_id"]
    assert second["message"] == topic_report.NOTE_DUPLICATE_MESSAGE
    assert len(_note_rows(USER_A["id"])) == 1

    # 报告标题改动后，标题 + 内容哈希不再相同 → 新建笔记（不返回 409）。
    topic_report.update_report(
        USER_A["id"], report["report_id"], title="专题报告 · 二次函数（改）",
        expected_version=report["version"],
    )
    third = topic_report.save_report_as_note(USER_A["id"], report["report_id"])
    assert third["saved"] is True
    assert third["note"]["id"] != first["note"]["id"]
    assert third["note"]["title"] == "专题报告 · 二次函数（改）"
    assert len(_note_rows(USER_A["id"])) == 2


# ---------------------------------------------------------------------------
# 归属与存在性：404（不产生笔记）
# ---------------------------------------------------------------------------


def test_save_report_as_note_foreign_or_missing_404(note_env):
    """他人报告 / 不存在报告 404（中文同文案），且不写入任何笔记。"""
    with pytest.raises(HTTPException) as foreign:
        topic_report.save_report_as_note(USER_A["id"], note_env["report_b"]["report_id"])
    assert foreign.value.status_code == 404
    assert "不存在或无权访问" in foreign.value.detail

    with pytest.raises(HTTPException) as missing:
        topic_report.save_report_as_note(USER_A["id"], 99999999)
    assert missing.value.status_code == 404
    assert "不存在或无权访问" in missing.value.detail

    assert _all_note_count() == 0


def test_save_report_as_note_route_201_and_foreign_404(note_api, note_env):
    """路由：本人报告 201 + saved=true；切到他人账号后同一报告 404。"""
    client, holder = note_api
    report = note_env["report_a"]
    response = client.post("%s/%s/save-note" % (BASE, report["report_id"]))
    assert response.status_code == 201
    body = response.json()
    assert body["saved"] is True
    assert body["report_id"] == report["report_id"]
    assert body["note"]["id"] >= 1 and "content" not in body["note"]

    holder["current"] = dict(USER_B)
    foreign = client.post("%s/%s/save-note" % (BASE, report["report_id"]))
    assert foreign.status_code == 404
    assert "不存在或无权访问" in foreign.json()["detail"]
    # 路径参数非法 → 422（FastAPI 路径校验，不产生笔记）。
    assert client.post("%s/not-a-number/save-note" % BASE).status_code == 422


# ---------------------------------------------------------------------------
# 资料失效 / 超长截断 / 出处清单幂等
# ---------------------------------------------------------------------------


def test_save_report_as_note_when_document_deleted(note_env):
    """报告引用的资料已删除时仍可保存：document_id 为空并记录失效说明，不阻断保存。"""
    report = note_env["report_a"]
    with db.connection() as con:
        con.execute("DELETE FROM ai_documents WHERE id=?", (DOC_A,))

    result = topic_report.save_report_as_note(USER_A["id"], report["report_id"])
    assert result["saved"] is True
    row = _note_rows(USER_A["id"])[0]
    assert row["document_id"] is None
    versions = json.loads(row["source_versions_json"])
    assert versions["invalid_document_id"] == DOC_A
    assert "已删除或无权访问" in versions["invalid_reason"]
    assert str(DOC_A) not in versions
    # 报告正文与出处清单照常保存（快照，不因资料删除而失败）。
    assert "## 出处清单" in row["content"]
    assert "资料：《%s》" % DOC_A_TITLE in row["content"]


def test_note_content_from_report_appends_sources_and_is_idempotent():
    """`_note_content_from_report`：逐条列出来源，quote 前 120 字；重复调用不叠加小节。"""
    long_quote = "二次函数的图像是抛物线" + "长" * 300
    sources = [
        {
            "kind": "document", "id": DOC_A, "title": DOC_A_TITLE,
            "ref": "ai_documents:%s" % DOC_A, "page": 3, "chunk_id": "p3-c1",
            "version": "0123456789abcdef", "quote": long_quote,
        },
        {"kind": "question", "id": "bank:1", "title": "求二次函数的顶点。", "ref": "bank:1"},
    ]
    content = topic_report._note_content_from_report("# 报告\n\n正文", sources)
    assert content.startswith("# 报告")
    assert "## 出处清单" in content
    assert "资料：《%s》" % DOC_A_TITLE in content
    assert "第 3 页" in content and "chunk p3-c1" in content
    assert "版本 0123456789abcdef" in content
    assert "出处 ai_documents:%s" % DOC_A in content
    assert "题目：《求二次函数的顶点。》" in content
    quote_lines = [line for line in content.splitlines() if line.strip().startswith("> ")]
    assert quote_lines and len(quote_lines[0].strip()[2:]) <= 120

    # 幂等：把已带出处清单的内容再传一次，不再追加第二个小节。
    again = topic_report._note_content_from_report(content, sources)
    assert again == content
    assert again.count("## 出处清单") == 1

    # 来源为空时如实写「没有可记录的真实来源」，不虚构来源。
    empty = topic_report._note_content_from_report("正文", [])
    assert topic_report.NOTE_SOURCES_EMPTY in empty
    # 正文里已有出处清单小节 → 不重复追加（即使来源列表非空）。
    existing = topic_report._note_content_from_report("## 出处清单\n\n已有内容", sources)
    assert existing.count("## 出处清单") == 1

    # 精简来源：quote 截断到 300 字、条数上限 50 条。
    slim = topic_report._slim_note_sources(
        [{"kind": "note", "id": index, "title": "笔记 %s" % index, "quote": "摘" * 500}
         for index in range(60)]
    )
    assert len(slim) == topic_report.NOTE_SOURCES_MAX == 50
    assert len(slim[0]["quote"]) == topic_report.NOTE_SOURCE_QUOTE_CHARS == 300
    assert slim[0]["kind"] == "note"


def test_save_report_as_note_truncates_long_content(note_env, monkeypatch):
    """正文超过上限时截断并注明（monkeypatch 上限，验证截断说明与字符数）。"""
    monkeypatch.setattr(topic_report, "NOTE_CONTENT_MAX_CHARS", 400)
    report = note_env["report_a"]
    result = topic_report.save_report_as_note(USER_A["id"], report["report_id"])
    assert result["saved"] is True
    row = _note_rows(USER_A["id"])[0]
    assert len(row["content"]) <= 400
    assert "已截断" in row["content"]
    assert "完整内容请查看原专题报告" in row["content"]
    assert result["note"]["content_bytes"] == len(row["content"].encode("utf-8"))


# ---------------------------------------------------------------------------
# 两账号隔离 / JSON 列类型
# ---------------------------------------------------------------------------


def test_notes_json_fields_and_owner_isolation(note_env):
    """两账号各自保存自己的报告：笔记 user_id 正确，JSON 列可解析且字段类型合法。"""
    topic_report.save_report_as_note(USER_A["id"], note_env["report_a"]["report_id"])
    topic_report.save_report_as_note(USER_B["id"], note_env["report_b"]["report_id"])

    rows_a = _note_rows(USER_A["id"])
    rows_b = _note_rows(USER_B["id"])
    assert len(rows_a) == 1 and len(rows_b) == 1
    assert rows_a[0]["document_id"] == DOC_A
    assert rows_b[0]["document_id"] == DOC_B
    assert rows_a[0]["id"] != rows_b[0]["id"]

    for row in (rows_a[0], rows_b[0]):
        assert isinstance(row["content"], str) and row["content"].strip()
        sources = json.loads(row["sources_json"])
        assert isinstance(sources, list) and sources
        for item in sources:
            assert isinstance(item, dict) and isinstance(item["kind"], str)
            if "id" in item:
                assert isinstance(item["id"], (int, str))
            if "page" in item:
                assert isinstance(item["page"], int) and item["page"] >= 1
            if "quote" in item:
                assert isinstance(item["quote"], str) and len(item["quote"]) <= 300
        versions = json.loads(row["source_versions_json"])
        assert isinstance(versions, dict) and versions
        assert all(
            isinstance(key, str) and isinstance(value, int) for key, value in versions.items()
        )


# ---------------------------------------------------------------------------
# 真实应用集成：上传资料 → 建报告 → save-note → GET /api/notes/{id}
# ---------------------------------------------------------------------------

DOC_TEXT = (
    "第一章 函数与导数\n"
    "1.1 函数的概念\n"
    "M7专题验收关键词BETA 用于验证专题报告保存为笔记后的出处清单。\n"
    "1.2 函数的单调性\n"
    "函数单调递增时导数非负。\n"
)


def _upload_document(client) -> dict:
    response = client.post(
        "/api/documents",
        files={"file": ("m7-note.txt", DOC_TEXT.encode("utf-8"), "text/plain")},
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_topic_report_save_note_integration(client, second_client):
    """真实应用：上传资料 → 建报告 → save-note → 笔记详情可读；重复保存不新增、他人 404。"""
    document = _upload_document(client)
    created = client.post(
        BASE,
        json={"topic": "M7专题验收关键词BETA", "document_ids": [document["id"]]},
    )
    assert created.status_code == 201, created.text
    report = created.json()["report"]
    report_id = report["report_id"]

    notes_before = len(client.get("/api/notes").json()["items"])
    saved = client.post("%s/%s/save-note" % (BASE, report_id))
    assert saved.status_code == 201, saved.text
    body = saved.json()
    assert body["saved"] is True and body["report_id"] == report_id
    note_id = body["note"]["id"]
    assert note_id >= 1 and "content" not in body["note"]

    detail = client.get("/api/notes/%s" % note_id)
    assert detail.status_code == 200, detail.text
    note = detail.json()
    assert note["id"] == note_id
    assert note["title"] == report["title"]
    assert "M7专题验收关键词BETA" in note["content"]
    assert "## 出处清单" in note["content"]
    assert "出处 ai_documents:%s" % document["id"] in note["content"]
    assert note["sources"] and note["sources"][0]["kind"] == "document"
    assert note["sources"][0]["id"] == document["id"]
    assert str(document["id"]) in note["source_versions"]
    assert int(note["source_versions"][str(document["id"])]) >= 1

    # 同一报告重复保存：saved=false、不重复插入。
    duplicate = client.post("%s/%s/save-note" % (BASE, report_id))
    assert duplicate.status_code == 201
    duplicate_body = duplicate.json()
    assert duplicate_body["saved"] is False
    assert duplicate_body["duplicate_of_note_id"] == note_id
    assert len(client.get("/api/notes").json()["items"]) == notes_before + 1

    # 他人不可保存（视为不存在，不泄露存在性）。
    assert second_client.post("%s/%s/save-note" % (BASE, report_id)).status_code == 404
