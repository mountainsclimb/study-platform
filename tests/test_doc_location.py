"""资料原文定位与摘录（``app/doc_location.py``）的隔离测试。

本文件不依赖 ``app.main``：用本地 ``FastAPI()`` 只挂载 ``doc_location.router_docs`` 与
``doc_location.router``，并通过 ``app.dependency_overrides[current_user]`` 注入当前用户；
切换 ``holder["current"]`` 即可模拟两个账号之间的隔离。

数据目录由 conftest 的 ``isolated_db`` 指到 tmp_path：先 ``db.init_db()``（建 users）与
``ai.init_ai()``（建 ``ai_documents`` / ``ai_notes``，本模块只读这两张表），再
``init_doc_location()``；随后用 ``db.connection()`` 直接插入中文资料的 2 页 3 chunks 与笔记。

覆盖：chunk_id / quote（含跨行空白与 ASCII 大小写归一）/ page 三种定位；参数校验与跨用户
404；摘录创建（doc_version / heading_path 快照、颜色、备注、note_id 归属、单文档上限）；
列表实时校验（资料更新保留/删除摘录文本、资料删除、chunks 损坏不 500）；refresh 迁移/失效；
删除 404/200；两账号隔离。
"""
import hashlib
import json

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
import pytest

from app import ai, db, doc_location
from app.auth import current_user

USER_A = {"id": 501, "username": "定位测试甲", "role": "student"}
USER_B = {"id": 502, "username": "定位测试乙", "role": "student"}
DOC_A = 9101
DOC_B = 9102
NOTE_A = 9201
NOTE_B = 9202

#: 2 页 3 chunks 的中文资料：p1-c1 / p1-c2 在第 1 页，p2-c1 在第 2 页。
DOC_A_CHUNKS = [
    {
        "page": 1,
        "chunk_id": "p1-c1",
        "heading_path": "第一章 函数",
        "text": "第一章 函数\n函数是描述变化的基本工具，定义域与值域是函数概念的核心。",
    },
    {
        "page": 1,
        "chunk_id": "p1-c2",
        "heading_path": "第一章 函数 › 1.2 单调性",
        "text": "1.2 函数的单调性\n若函数在区间上单调递增，则其导数在该区间上非负。",
    },
    {
        "page": 2,
        "chunk_id": "p2-c1",
        "heading_path": "第二章 导数",
        "text": "第二章 导数\nDerivative 表示函数的变化率，记作 f'(x)。",
    },
]
DOC_B_CHUNKS = [
    {
        "page": 1,
        "chunk_id": "p1-c1",
        "heading_path": "第一章 运动",
        "text": "第一章 运动\n位移随时间的变化率称为速度。",
    },
]

LOCATE_KEYS = {
    "document_id", "title", "doc_version", "pages", "page", "chunk_id", "heading_path",
    "quote", "snippet", "start", "end", "match", "valid", "notice",
}
ANNOTATION_KEYS = {
    "annotation_id", "document_id", "note_id", "page", "chunk_id", "heading_path",
    "quote", "snippet", "color", "note", "doc_version", "valid", "invalid_reason",
    "created_at", "updated_at",
}


# ---------------------------------------------------------------------------
# 夹具与工具
# ---------------------------------------------------------------------------


def _insert_user(con, user) -> None:
    con.execute(
        "INSERT INTO users(id,username,password_hash,role,created_at) VALUES (?,?,?,?,?)",
        (user["id"], user["username"], "unit-test-hash", "student", db.utcnow()),
    )


def _insert_document(con, document_id, user_id, chunks, *, title="数学讲义（测试资料）",
                     pages=2, status="ready") -> None:
    con.execute(
        "INSERT INTO ai_documents(id,user_id,title,storage_name,pages,status,chunks_json,created_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (
            document_id, user_id, title, f"doc-{document_id}.txt", pages, status,
            json.dumps(chunks, ensure_ascii=False), db.utcnow(),
        ),
    )


def _insert_note(con, note_id, user_id) -> None:
    now = db.utcnow()
    con.execute(
        "INSERT INTO ai_notes(id,user_id,document_id,title,content,sources_json,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (note_id, user_id, None, f"笔记{note_id}", "摘录笔记内容", "[]", now, now),
    )


def _update_chunks(document_id, chunks) -> None:
    with db.connection() as con:
        con.execute(
            "UPDATE ai_documents SET chunks_json=? WHERE id=?",
            (json.dumps(chunks, ensure_ascii=False), document_id),
        )


def _delete_document(document_id) -> None:
    with db.connection() as con:
        con.execute("DELETE FROM ai_documents WHERE id=?", (document_id,))


@pytest.fixture
def doc_data(isolated_db):
    """隔离应用库：建表 + 两个用户 + 两份资料 + 两条笔记。"""
    db.init_db()
    # ai_documents / ai_notes 的真实建表语句来自 AI 模块（本模块只读这两张表）。
    ai.init_ai()
    doc_location.init_doc_location()
    with db.connection() as con:
        _insert_user(con, USER_A)
        _insert_user(con, USER_B)
        _insert_document(con, DOC_A, USER_A["id"], DOC_A_CHUNKS)
        _insert_document(con, DOC_B, USER_B["id"], DOC_B_CHUNKS, title="物理讲义（他人资料）", pages=1)
        _insert_note(con, NOTE_A, USER_A["id"])
        _insert_note(con, NOTE_B, USER_B["id"])
    return isolated_db


@pytest.fixture
def api(doc_data):
    """本模块两个 router 的独立小应用；holder 切换当前用户，不依赖真实登录 cookie。"""
    app = FastAPI()
    app.include_router(doc_location.router_docs)
    app.include_router(doc_location.router)
    holder = {"current": dict(USER_A)}
    app.dependency_overrides[current_user] = lambda: holder["current"]
    with TestClient(app) as client:
        yield client, holder


def _as(holder, user) -> None:
    holder["current"] = dict(user)


def _locate(client, document_id=DOC_A, **body):
    return client.post(f"/api/documents/{document_id}/locate", json=body)


def _create(client, **overrides):
    payload = {"document_id": DOC_A, "quote": "若函数在区间上单调递增"}
    payload.update(overrides)
    return client.post("/api/doc-annotations", json=payload)


def _created(client, **overrides) -> dict:
    response = _create(client, **overrides)
    assert response.status_code == 201, response.text
    return response.json()["annotation"]


def _list(client, **params):
    return client.get("/api/doc-annotations", params=params)


# ---------------------------------------------------------------------------
# 建表与版本口径
# ---------------------------------------------------------------------------


def test_init_doc_location_is_idempotent(doc_data):
    """建表幂等：重复调用不报错，列与索引齐全。"""
    doc_location.init_doc_location()
    doc_location.init_doc_location()
    with db.connection() as con:
        tables = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        columns = {row[1] for row in con.execute("PRAGMA table_info(doc_annotations)")}
        indexes = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    assert "doc_annotations" in tables
    assert {
        "id", "user_id", "document_id", "note_id", "page", "chunk_id", "heading_path",
        "quote", "snippet", "color", "note", "doc_version", "created_at", "updated_at",
    } <= columns
    assert {
        "idx_doc_annotations_user", "idx_doc_annotations_document", "idx_doc_annotations_note",
    } <= indexes


def test_document_version_is_content_digest(doc_data):
    """doc_version = sha256(规范 JSON)[:16]：与键顺序/输入形式无关，内容变则版本变。"""
    expected = hashlib.sha256(
        json.dumps(DOC_A_CHUNKS, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()[:16]
    assert doc_location.document_version(DOC_A_CHUNKS) == expected
    assert len(expected) == 16
    assert all(char in "0123456789abcdef" for char in expected)
    # 传入 JSON 字符串（chunks_json 的存储形式）得到同一摘要。
    assert doc_location.document_version(
        json.dumps(DOC_A_CHUNKS, ensure_ascii=False)
    ) == expected
    # 字典键顺序不影响摘要（sort_keys）。
    reordered = [dict(reversed(list(chunk.items()))) for chunk in DOC_A_CHUNKS]
    assert doc_location.document_version(reordered) == expected
    # 内容变化 → 新版本；非法 JSON 字符串按空列表处理。
    changed = json.loads(json.dumps(DOC_A_CHUNKS))
    changed[0]["text"] += "（新增补充说明）"
    assert doc_location.document_version(changed) != expected
    assert doc_location.document_version("{不是 JSON") == doc_location.document_version([])


# ---------------------------------------------------------------------------
# 资料加载（只读 ai_documents，404 不泄露存在性）
# ---------------------------------------------------------------------------


def test_load_document_returns_owned_chunks(doc_data):
    with db.connection() as con:
        doc = doc_location._load_document(USER_A["id"], DOC_A, con=con)
    assert doc["id"] == DOC_A
    assert doc["title"] == "数学讲义（测试资料）"
    assert doc["pages"] == 2
    assert doc["status"] == "ready"
    assert doc["doc_version"] == doc_location.document_version(DOC_A_CHUNKS)
    assert [chunk["chunk_id"] for chunk in doc["chunks"]] == ["p1-c1", "p1-c2", "p2-c1"]
    assert [chunk["page"] for chunk in doc["chunks"]] == [1, 1, 2]
    assert doc["chunks"][1]["heading_path"] == "第一章 函数 › 1.2 单调性"


def test_load_document_hides_other_user_and_rejects_broken_chunks(doc_data):
    # 他人资料与不存在资料同一文案、同一状态码，不泄露存在性。
    with pytest.raises(HTTPException) as other:
        doc_location._load_document(USER_B["id"], DOC_A)
    assert other.value.status_code == 404
    assert other.value.detail == "资料不存在或无权访问"
    with pytest.raises(HTTPException) as missing:
        doc_location._load_document(USER_A["id"], 999999)
    assert missing.value.status_code == 404
    assert missing.value.detail == "资料不存在或无权访问"
    # chunks_json 结构损坏是服务端数据问题：503，而不是把它当成空资料。
    with db.connection() as con:
        _insert_document(con, 9103, USER_A["id"], [{"page": 1, "chunk_id": "p1-c1", "text": "占位"}])
        con.execute("UPDATE ai_documents SET chunks_json=? WHERE id=?", ("{不是 JSON 数组", 9103))
    with pytest.raises(HTTPException) as broken:
        doc_location._load_document(USER_A["id"], 9103)
    assert broken.value.status_code == 503
    assert broken.value.detail == "资料内容暂不可用，请稍后重试"


# ---------------------------------------------------------------------------
# 定位：chunk_id / quote（空白折叠 + ASCII 大小写）/ page
# ---------------------------------------------------------------------------


def test_locate_by_chunk_id(api):
    client, _holder = api
    response = _locate(client, chunk_id="p1-c2")
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == LOCATE_KEYS
    assert body["document_id"] == DOC_A
    assert body["title"] == "数学讲义（测试资料）"
    assert body["pages"] == 2
    assert body["doc_version"] == doc_location.document_version(DOC_A_CHUNKS)
    assert body["page"] == 1
    assert body["chunk_id"] == "p1-c2"
    assert body["heading_path"] == "第一章 函数 › 1.2 单调性"
    assert body["quote"] == ""
    assert body["start"] == 0 and body["end"] == 0
    assert body["match"] == "exact"
    assert body["valid"] is True
    assert body["snippet"].startswith("1.2 函数的单调性")
    assert "已定位到片段开头" in body["notice"]


def test_locate_by_exact_quote(api):
    client, _holder = api
    quote = "若函数在区间上单调递增"
    response = _locate(client, quote=quote)
    assert response.status_code == 200, response.text
    body = response.json()
    text = DOC_A_CHUNKS[1]["text"]
    assert body["chunk_id"] == "p1-c2"
    assert body["page"] == 1
    assert body["match"] == "exact"
    assert body["quote"] == quote
    assert body["start"] == text.index(quote)
    assert body["end"] == body["start"] + len(quote)
    assert quote in body["snippet"]
    assert body["notice"] == ""


def test_locate_normalizes_whitespace_and_ascii_case(api):
    client, _holder = api
    # 原文里的换行被用户替换成空格：空白折叠后命中，返回的是资料原始文字与原始偏移。
    response = _locate(client, quote="1.2 函数的单调性 若函数")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["match"] == "normalized"
    assert body["chunk_id"] == "p1-c2"
    assert body["quote"] == "1.2 函数的单调性\n若函数"
    assert body["start"] == 0
    assert body["end"] == len("1.2 函数的单调性\n若函数")
    assert "已按忽略空白与英文大小写" in body["notice"]
    assert "\n" not in body["snippet"] and "1.2 函数的单调性 若函数" in body["snippet"]

    # ASCII 大小写不敏感 + 跨行空白：原文 "Derivative 表示函数的变化率"。
    response = _locate(client, quote="derivative\n\n表示函数的变化率")
    assert response.status_code == 200, response.text
    body = response.json()
    text = DOC_A_CHUNKS[2]["text"]
    assert body["match"] == "normalized"
    assert body["page"] == 2
    assert body["chunk_id"] == "p2-c1"
    assert body["quote"] == "Derivative 表示函数的变化率"
    assert body["start"] == text.index("Derivative")
    assert body["end"] == body["start"] + len("Derivative 表示函数的变化率")
    assert "\n" not in body["snippet"] and "Derivative 表示函数的变化率" in body["snippet"]


def test_locate_by_page_uses_first_chunk(api):
    client, _holder = api
    first_page = _locate(client, page=1)
    assert first_page.status_code == 200, first_page.text
    body = first_page.json()
    assert body["chunk_id"] == "p1-c1"
    assert body["page"] == 1
    assert body["quote"] == ""
    assert body["start"] == 0 and body["end"] == 0
    assert body["match"] == "exact"
    assert body["snippet"].startswith("第一章 函数")
    assert "该页第一个片段" in body["notice"]

    second_page = _locate(client, page=2).json()
    assert second_page["chunk_id"] == "p2-c1"
    assert second_page["page"] == 2


def test_locate_page_scopes_quote_candidates(api):
    client, _holder = api
    quote = "Derivative 表示函数的变化率"
    on_wrong_page = _locate(client, page=1, quote=quote)
    assert on_wrong_page.status_code == 422
    assert on_wrong_page.json()["detail"] == "在当前资料版本中未找到该摘录"
    on_right_page = _locate(client, page=2, quote=quote)
    assert on_right_page.status_code == 200, on_right_page.text
    assert on_right_page.json()["chunk_id"] == "p2-c1"


def test_locate_requires_condition_and_validates_params(api):
    client, _holder = api
    empty = _locate(client)
    assert empty.status_code == 422
    assert empty.json()["detail"] == "请提供 quote、chunk_id 或 page 之一"
    empty_body = client.post(f"/api/documents/{DOC_A}/locate", json={})
    assert empty_body.status_code == 422
    assert empty_body.json()["detail"] == "请提供 quote、chunk_id 或 page 之一"

    for body, fragment in [
        ({"page": 0}, "页码"),
        ({"page": 100_001}, "页码"),
        ({"quote": ""}, "摘录"),
        ({"quote": "长" * 501}, "500"),
        ({"chunk_id": "x" * 65}, "片段标识"),
    ]:
        response = _locate(client, **body)
        assert response.status_code == 422, body
        assert fragment in response.json()["detail"], body


def test_locate_quote_not_found_and_page_without_chunks(api):
    client, _holder = api
    missing = _locate(client, quote="不存在的摘录XYZ")
    assert missing.status_code == 422
    assert missing.json()["detail"] == "在当前资料版本中未找到该摘录"

    empty_page = _locate(client, page=3)
    assert empty_page.status_code == 422
    assert "第 3 页" in empty_page.json()["detail"]

    stale_chunk = _locate(client, chunk_id="p9-c9")
    assert stale_chunk.status_code == 422
    assert stale_chunk.json()["detail"] == "在当前资料版本中未找到该片段"

    # chunk_id 已失效但摘录仍在新版本里：回退到按 quote 定位并说明已重新定位。
    fallback = _locate(client, chunk_id="p9-c9", quote="若函数在区间上单调递增")
    assert fallback.status_code == 200, fallback.text
    body = fallback.json()
    assert body["chunk_id"] == "p1-c2"
    assert "原片段标识已失效" in body["notice"]

    # 显式指定 chunk_id 时，摘录必须在该分块内，否则 422。
    mismatched = _locate(client, chunk_id="p1-c1", quote="Derivative 表示函数的变化率")
    assert mismatched.status_code == 422
    assert mismatched.json()["detail"] == "在当前资料版本中未找到该摘录"


def test_locate_other_user_and_missing_document_are_404(api):
    client, holder = api
    other = _locate(client, document_id=DOC_B, quote="第一章 运动")
    assert other.status_code == 404
    assert other.json()["detail"] == "资料不存在或无权访问"
    missing = _locate(client, document_id=999999, page=1)
    assert missing.status_code == 404
    assert missing.json()["detail"] == "资料不存在或无权访问"
    _as(holder, USER_B)
    reverse = _locate(client, document_id=DOC_A, quote="若函数在区间上单调递增")
    assert reverse.status_code == 404
    assert reverse.json()["detail"] == "资料不存在或无权访问"


def test_locate_document_without_text(api):
    client, _holder = api
    with db.connection() as con:
        _insert_document(
            con, 9104, USER_A["id"], [], title="扫描件（待识别）", pages=0, status="needs_ocr",
        )
    for body in ({"quote": "任意摘录"}, {"page": 1}):
        response = _locate(client, document_id=9104, **body)
        assert response.status_code == 422
        assert "没有可定位的文本" in response.json()["detail"]


# ---------------------------------------------------------------------------
# 摘录创建
# ---------------------------------------------------------------------------


def test_create_annotation_snapshot(api):
    client, _holder = api
    annotation = _created(
        client, quote="若函数在区间上单调递增", note="重点复习", color="green", page=1,
    )
    assert set(annotation) == ANNOTATION_KEYS
    assert annotation["annotation_id"] >= 1
    assert annotation["document_id"] == DOC_A
    assert annotation["note_id"] is None
    assert annotation["page"] == 1
    assert annotation["chunk_id"] == "p1-c2"
    assert annotation["heading_path"] == "第一章 函数 › 1.2 单调性"
    assert annotation["quote"] == "若函数在区间上单调递增"
    assert "若函数在区间上单调递增" in annotation["snippet"]
    assert annotation["color"] == "green"
    assert annotation["note"] == "重点复习"
    # 快照的 doc_version 就是当前内容摘要（本轮不新增资料版本列）。
    assert annotation["doc_version"] == doc_location.document_version(DOC_A_CHUNKS)
    assert annotation["valid"] is True
    assert annotation["invalid_reason"] == ""
    assert annotation["created_at"] and annotation["created_at"] == annotation["updated_at"]


def test_create_annotation_stores_source_text_snapshot(api):
    """用户提交的是归一化摘录时，快照保存资料原文（不是用户输入的大小写/空白形式）。"""
    client, _holder = api
    annotation = _created(client, quote="derivative\n\n表示函数的变化率")
    assert annotation["quote"] == "Derivative 表示函数的变化率"
    assert annotation["page"] == 2
    assert annotation["chunk_id"] == "p2-c1"
    assert annotation["heading_path"] == "第二章 导数"


def test_create_annotation_note_ownership(api):
    client, holder = api
    own_note = _created(client, note_id=NOTE_A)
    assert own_note["note_id"] == NOTE_A

    foreign_note = _create(client, note_id=NOTE_B)
    assert foreign_note.status_code == 404
    assert foreign_note.json()["detail"] == "笔记不存在或无权访问"
    missing_note = _create(client, note_id=999999)
    assert missing_note.status_code == 404
    assert missing_note.json()["detail"] == "笔记不存在或无权访问"

    _as(holder, USER_B)
    my_note_as_other = _create(client, note_id=NOTE_A)
    assert my_note_as_other.status_code == 404


def test_create_annotation_validation(api):
    client, _holder = api
    cases = [
        ({"quote": ""}, "摘录"),
        ({"quote": "长" * 501}, "500"),
        ({"note": "备注" * 501}, "1000"),
        ({"color": "purple"}, "颜色"),
        ({"quote": "Derivative 表示函数的变化率", "page": 1}, "未找到该摘录"),
        ({"document_id": DOC_B}, "资料不存在或无权访问"),
        ({"document_id": 999999}, "资料不存在或无权访问"),
    ]
    for overrides, fragment in cases:
        response = _create(client, **overrides)
        assert response.status_code in (404, 422), overrides
        assert fragment in response.json()["detail"], overrides

    default_color = _created(client)
    assert default_color["color"] == "yellow"
    assert default_color["note"] == ""
    for color in ("yellow", "green", "blue", "pink"):
        assert _created(client, color=color)["color"] == color


def test_create_annotation_limit_per_document(api, monkeypatch):
    # 按契约允许的模块常量临时调小上限，验证「单文档 ≤ 上限」规则本身，
    # 避免用例真的插入 200 条拖慢测试（上限默认为 200）。
    monkeypatch.setattr(doc_location, "MAX_ANNOTATIONS_PER_DOC", 2)
    client, _holder = api
    _created(client, quote="定义域与值域")
    _created(client, quote="若函数在区间上单调递增")
    over_limit = _create(client, quote="Derivative 表示函数的变化率")
    assert over_limit.status_code == 422
    assert "最多 2 条" in over_limit.json()["detail"]
    # 另一份资料的配额互不影响（且他人资料仍先按 404 处理）。
    assert _list(client, document_id=DOC_A).json()["total"] == 2


def test_create_annotation_detects_version_change_during_save(api, monkeypatch):
    """定位之后资料又被更新（模拟并发写）→ 409，不把旧版本快照落库；重新定位后可保存。"""
    client, _holder = api
    real_locate = doc_location.locate

    def stale_locate(user_id, document_id, **kwargs):
        located = real_locate(user_id, document_id, **kwargs)
        # 模拟「locate 读到旧版本、提交前资料已更新」：把快照版本改成旧摘要。
        located["doc_version"] = "0" * 16
        return located

    monkeypatch.setattr(doc_location, "locate", stale_locate)
    conflict = _create(client, quote="若函数在区间上单调递增")
    assert conflict.status_code == 409
    assert "资料在保存摘录期间发生变化" in conflict.json()["detail"]
    assert _list(client).json()["total"] == 0  # 冲突时事务回滚，没有半条摘录。

    monkeypatch.setattr(doc_location, "locate", real_locate)
    assert _create(client, quote="若函数在区间上单调递增").status_code == 201


# ---------------------------------------------------------------------------
# 摘录列表：排序 / 过滤 / 实时有效性 / 不 500
# ---------------------------------------------------------------------------


def test_list_annotations_order_filters_and_limit(api):
    client, _holder = api
    first = _created(client, quote="定义域与值域")
    second = _created(client, quote="若函数在区间上单调递增", note="重点", note_id=NOTE_A)
    third = _created(client, quote="Derivative 表示函数的变化率", color="blue")

    response = _list(client)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 3
    assert [item["annotation_id"] for item in body["items"]] == [
        third["annotation_id"], second["annotation_id"], first["annotation_id"],
    ]
    assert all(item["valid"] is True and item["invalid_reason"] == "" for item in body["items"])

    by_page = _list(client, page=2).json()
    assert by_page["total"] == 1 and by_page["items"][0]["chunk_id"] == "p2-c1"
    by_note = _list(client, note_id=NOTE_A).json()
    assert by_note["total"] == 1 and by_note["items"][0]["note"] == "重点"
    by_document = _list(client, document_id=DOC_A).json()
    assert by_document["total"] == 3
    assert _list(client, document_id=DOC_B).json()["total"] == 0

    limited = _list(client, limit=2).json()
    assert limited["total"] == 3 and len(limited["items"]) == 2
    for bad_limit in (0, 201):
        response = _list(client, limit=bad_limit)
        assert response.status_code == 422
        assert "数量" in response.json()["detail"]


def test_list_annotations_isolated_between_users(api):
    client, holder = api
    mine = _created(client, quote="若函数在区间上单调递增")
    _as(holder, USER_B)
    assert _list(client).json()["total"] == 0
    theirs = _created(client, document_id=DOC_B, quote="位移随时间的变化率")
    assert _list(client).json()["total"] == 1
    _as(holder, USER_A)
    body = _list(client).json()
    assert body["total"] == 1
    assert body["items"][0]["annotation_id"] == mine["annotation_id"]
    assert all(item["annotation_id"] != theirs["annotation_id"] for item in body["items"])


def test_list_annotations_invalid_after_update_keeping_quote(api):
    """资料更新（摘录文本保留）→ 列表失效原因「资料已更新」；refresh 可迁移回有效。"""
    client, _holder = api
    annotation = _created(client, quote="若函数在区间上单调递增")
    updated = [dict(chunk) for chunk in DOC_A_CHUNKS]
    updated[0]["text"] += "（新增补充说明）"
    _update_chunks(DOC_A, updated)

    listing = _list(client, document_id=DOC_A).json()
    assert listing["total"] == 1
    item = listing["items"][0]
    assert item["valid"] is False
    assert item["invalid_reason"] == doc_location.INVALID_REASON_VERSION
    # 失效条目照常返回快照与原因，不删除、不 500。
    assert item["quote"] == "若函数在区间上单调递增"
    assert item["doc_version"] == doc_location.document_version(DOC_A_CHUNKS)

    refreshed = client.post(f"/api/doc-annotations/{annotation['annotation_id']}/refresh")
    assert refreshed.status_code == 200, refreshed.text
    body = refreshed.json()
    assert body["valid"] is True
    assert body["migrated"] is True
    assert body["invalid_reason"] == ""
    assert body["annotation"]["doc_version"] == doc_location.document_version(updated)
    assert "若函数在区间上单调递增" in body["annotation"]["snippet"]
    assert _list(client, document_id=DOC_A).json()["items"][0]["valid"] is True


def test_list_annotations_invalid_after_quote_removed(api):
    """资料更新（删掉含摘录的分块）→ 「当前版本中已找不到该摘录」，refresh 仍是 200 失效。"""
    client, _holder = api
    annotation = _created(client, quote="若函数在区间上单调递增")
    remaining = [chunk for chunk in DOC_A_CHUNKS if chunk["chunk_id"] != "p1-c2"]
    _update_chunks(DOC_A, remaining)

    listing = _list(client, document_id=DOC_A).json()
    assert listing["total"] == 1
    assert listing["items"][0]["valid"] is False
    assert listing["items"][0]["invalid_reason"] == doc_location.INVALID_REASON_QUOTE

    refreshed = client.post(f"/api/doc-annotations/{annotation['annotation_id']}/refresh")
    assert refreshed.status_code == 200, refreshed.text
    body = refreshed.json()
    assert body["valid"] is False
    assert body["migrated"] is False
    assert body["invalid_reason"] == doc_location.INVALID_REASON_QUOTE
    # 找不到时不覆盖原快照的 doc_version。
    assert body["annotation"]["doc_version"] == doc_location.document_version(DOC_A_CHUNKS)


def test_list_annotations_invalid_after_document_deleted(api):
    client, _holder = api
    annotation = _created(client, quote="若函数在区间上单调递增")
    _delete_document(DOC_A)

    listing = _list(client)
    assert listing.status_code == 200
    assert listing.json()["items"][0]["valid"] is False
    assert listing.json()["items"][0]["invalid_reason"] == doc_location.INVALID_REASON_MISSING

    refreshed = client.post(f"/api/doc-annotations/{annotation['annotation_id']}/refresh")
    assert refreshed.status_code == 200, refreshed.text
    body = refreshed.json()
    assert body["valid"] is False and body["migrated"] is False
    assert body["invalid_reason"] == doc_location.INVALID_REASON_MISSING


def test_list_does_not_500_on_broken_chunks(api):
    """chunks_json 损坏时列表仍 200（仅该条降级失效），定位返回 503、刷新 200 失效。"""
    client, _holder = api
    annotation = _created(client, quote="若函数在区间上单调递增")
    with db.connection() as con:
        con.execute("UPDATE ai_documents SET chunks_json=? WHERE id=?", ("{损坏的 JSON", DOC_A))

    listing = _list(client)
    assert listing.status_code == 200, listing.text
    assert listing.json()["total"] == 1
    assert listing.json()["items"][0]["valid"] is False
    assert listing.json()["items"][0]["invalid_reason"] == doc_location.INVALID_REASON_VERSION

    located = _locate(client, quote="若函数在区间上单调递增")
    assert located.status_code == 503
    assert located.json()["detail"] == "资料内容暂不可用，请稍后重试"

    refreshed = client.post(f"/api/doc-annotations/{annotation['annotation_id']}/refresh")
    assert refreshed.status_code == 200, refreshed.text
    assert refreshed.json()["valid"] is False
    assert refreshed.json()["invalid_reason"] == doc_location.INVALID_REASON_VERSION


# ---------------------------------------------------------------------------
# 摘录刷新：迁移到新版本的新位置
# ---------------------------------------------------------------------------


def test_refresh_repositions_annotation_to_new_page(api):
    client, _holder = api
    annotation = _created(client, quote="若函数在区间上单调递增")
    moved = [
        dict(DOC_A_CHUNKS[0]),
        {
            "page": 2,
            "chunk_id": "p2-c2",
            "heading_path": "第二章 导数 › 2.3 单调性",
            "text": "2.3 单调性与导数\n若函数在区间上单调递增，则其导数在该区间上非负。",
        },
    ]
    _update_chunks(DOC_A, moved)

    refreshed = client.post(f"/api/doc-annotations/{annotation['annotation_id']}/refresh")
    assert refreshed.status_code == 200, refreshed.text
    body = refreshed.json()
    assert body["migrated"] is True and body["valid"] is True
    updated = body["annotation"]
    assert updated["page"] == 2
    assert updated["chunk_id"] == "p2-c2"
    assert updated["heading_path"] == "第二章 导数 › 2.3 单调性"
    assert updated["doc_version"] == doc_location.document_version(moved)
    assert "若函数在区间上单调递增" in updated["snippet"]
    assert updated["updated_at"] != annotation["updated_at"]

    listing = _list(client, document_id=DOC_A).json()
    assert listing["items"][0]["valid"] is True
    assert listing["items"][0]["chunk_id"] == "p2-c2"


def test_refresh_other_user_annotation_is_404(api):
    client, holder = api
    annotation = _created(client, quote="若函数在区间上单调递增")
    _as(holder, USER_B)
    response = client.post(f"/api/doc-annotations/{annotation['annotation_id']}/refresh")
    assert response.status_code == 404
    assert response.json()["detail"] == "摘录不存在或无权访问"
    missing = client.post("/api/doc-annotations/999999/refresh")
    assert missing.status_code == 404
    assert missing.json()["detail"] == "摘录不存在或无权访问"
    # 他人失败不影响原主人刷新。
    _as(holder, USER_A)
    assert client.post(
        f"/api/doc-annotations/{annotation['annotation_id']}/refresh"
    ).status_code == 200


# ---------------------------------------------------------------------------
# 删除与共享连接
# ---------------------------------------------------------------------------


def test_delete_annotation_permissions(api):
    client, holder = api
    mine = _created(client, quote="若函数在区间上单调递增")
    _as(holder, USER_B)
    foreign = client.delete(f"/api/doc-annotations/{mine['annotation_id']}")
    assert foreign.status_code == 404
    assert foreign.json()["detail"] == "摘录不存在或无权访问"
    assert _list(client).json()["total"] == 0  # B 本来就看不到 A 的摘录
    _as(holder, USER_A)
    assert _list(client).json()["total"] == 1  # A 的摘录未被他人删除

    deleted = client.delete(f"/api/doc-annotations/{mine['annotation_id']}")
    assert deleted.status_code == 200, deleted.text
    assert deleted.json() == {"deleted": True, "annotation_id": mine["annotation_id"]}
    assert _list(client).json()["total"] == 0
    again = client.delete(f"/api/doc-annotations/{mine['annotation_id']}")
    assert again.status_code == 404


def test_functions_accept_shared_connection(doc_data):
    """公开函数支持传入调用方事务连接（不自行提交），便于上层模块组合读写。"""
    with db.connection() as con:
        doc = doc_location._load_document(USER_A["id"], DOC_A, con=con)
        located = doc_location.locate(
            USER_A["id"], DOC_A, quote="若函数在区间上单调递增", con=con,
        )
        created = doc_location.create_annotation(
            USER_A["id"], document_id=DOC_A, quote="若函数在区间上单调递增", con=con,
        )
        listed = doc_location.list_annotations(USER_A["id"], con=con)
    assert doc["id"] == DOC_A
    assert located["chunk_id"] == "p1-c2"
    assert created["annotation"]["annotation_id"] >= 1
    assert listed["total"] == 1
    assert listed["items"][0]["valid"] is True
