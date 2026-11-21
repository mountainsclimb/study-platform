"""带引文的专题报告（``app/topic_report.py``）隔离测试。

本文件不依赖 ``app.main`` 与只读题库：用本地 ``FastAPI()`` 只挂载 ``topic_report.router``，并通过
``app.dependency_overrides[current_user]`` 注入当前用户，切换 ``holder["current"]`` 即可模拟两个账号。
数据库由 conftest 的 ``isolated_db`` 指向 tmp_path：先 ``db.init_db()`` 与 ``ai.init_ai()``
（建 ``ai_documents`` / ``ai_notes``，本模块只读这两张表），再 ``init_topic_reports()``；
资料分块、笔记、知识点与学习附件全部用真实公开函数或直接造数写入。

覆盖口径（与总方案 §5.3 后续功能、§7 M6 对齐）：

* 建表幂等与列结构；
* 四类来源（资料 / 笔记 / 知识点 / 学习附件）创建成功，文档标题 / 页码 / chunk_id / 文档版本、
  笔记 id、原文摘录 id、附件固定版本、候选题引用同时出现在 ``content_md`` 与 ``sources``；
* 主题关键字命中优先、未命中时回退资料开头 2 段并注明；无摘录不虚构；
* 非本人资料 / 笔记 / 附件 404（同文案不泄露），非法知识点 422，四类来源全空 422，
  主题过短 / 过长 422，单类来源超过 10 个 422；
* 列表 / 详情 / 删除、个人报告数上限 422（monkeypatch 调小上限）；
* PATCH 标题与正文、``expected_version`` 冲突 409（含过期的 0）、标题 / 正文上限 422；
* Markdown 下载内容与 ``content_md`` 一致、文件名安全且按 RFC 5987 编码；
* 同输入同输出（冻结时钟）与两账号隔离；
* 报告为规则汇编（``generator='rules'``）：模块不引用 AI 供应商 / 不调用 AI 模型。
"""
from __future__ import annotations

import inspect
import json
from urllib.parse import unquote

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError
import pytest

from app import ai, db, doc_location, practice, study_artifact, study_evidence, topic_report
from app.auth import current_user

USER_A = {"id": 9401, "username": "报告甲", "role": "student"}
USER_B = {"id": 9402, "username": "报告乙", "role": "student"}

TOPIC = "二次函数"
BASE = "/api/topic-reports"
FIXED_NOW = "2026-11-22T00:00:00+00:00"

DOC_A = 94101
DOC_A2 = 94102
DOC_B = 94103
NOTE_A = 94201
NOTE_A2 = 94202
NOTE_B = 94203
KP_A1 = 94301
KP_A2 = 94302

#: 资料 A：两段分块，只有第一段包含主题关键字「二次函数」。
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
#: 资料 A2：两段分块都不含主题关键字 -> 回退资料开头摘录。
DOC_A2_CHUNKS = [
    {
        "page": 1,
        "chunk_id": "p1-c1",
        "heading_path": "第二章 立体几何",
        "text": "第二章 立体几何\n空间点线面的位置关系。",
    },
    {
        "page": 2,
        "chunk_id": "p2-c1",
        "heading_path": "第二章 立体几何 › 2.2",
        "text": "2.2 空间向量\n空间向量可以表示方向与距离。",
    },
]
#: 他人资料：只用于跨账号 404。
DOC_B_CHUNKS = [
    {
        "page": 1,
        "chunk_id": "p1-c1",
        "heading_path": "第一章 运动",
        "text": "位移随时间的变化率称为速度。",
    },
]

DOC_A_TITLE = "数学讲义（测试资料）"
DOC_A2_TITLE = "几何讲义（无主题命中）"
DOC_B_TITLE = "物理讲义（他人资料）"

ARTIFACT_TITLE = "二次函数流程图"

#: 题库检索替身：隔离测试不挂真实题库，避免依赖只读题库与检索索引。
FAKE_QUESTIONS = [
    {"qid": "docA:1", "stem_plain": "已知二次函数的图像经过点 (1, 0)，求函数解析式。", "similarity": 0.5},
    {"qid": "app:12", "stem_plain": "求二次函数的最大值与最小值。", "similarity": 0.4},
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


def _insert_note(con, note_id, user_id, title, content) -> None:
    now = db.utcnow()
    con.execute(
        "INSERT INTO ai_notes(id,user_id,document_id,title,content,sources_json,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (note_id, user_id, None, title, content, "[]", now, now),
    )


@pytest.fixture
def report_env(isolated_db, monkeypatch):
    """隔离应用库 + 两个账号的真实资料 / 笔记 / 知识点 / 学习附件 / 摘录，返回造数编号。"""
    db.init_db()
    ai.init_ai()
    topic_report.init_topic_reports()
    topic_report.init_topic_reports()  # 幂等：重复初始化不报错

    now = db.utcnow()
    with db.connection() as con:
        _insert_user(con, USER_A)
        _insert_user(con, USER_B)
        _insert_document(con, DOC_A, USER_A["id"], DOC_A_TITLE, DOC_A_CHUNKS)
        _insert_document(con, DOC_A2, USER_A["id"], DOC_A2_TITLE, DOC_A2_CHUNKS)
        _insert_document(con, DOC_B, USER_B["id"], DOC_B_TITLE, DOC_B_CHUNKS, pages=1)
        _insert_note(
            con, NOTE_A, USER_A["id"], "二次函数笔记",
            "二次函数 y=a(x-h)^2+k 的顶点是 (h,k)，开口方向由 a 的符号决定。",
        )
        _insert_note(con, NOTE_A2, USER_A["id"], "函数复习笔记", "函数是描述变化的基本工具。")
        _insert_note(con, NOTE_B, USER_B["id"], "他人笔记", "位移随时间的变化率称为速度。")
        con.execute(
            "INSERT INTO knowledge_points(id,subject,name,level) VALUES (?,?,?,?)",
            (KP_A1, "数学", "二次函数", 3),
        )
        con.execute(
            "INSERT INTO knowledge_points(id,subject,name,level) VALUES (?,?,?,?)",
            (KP_A2, "数学", "函数图像", 3),
        )
    annotation = doc_location.create_annotation(
        USER_A["id"], document_id=DOC_A, quote="二次函数的图像是抛物线", note_id=NOTE_A
    )
    artifact = study_artifact.create_artifact(
        USER_A["id"], kind="flow_animation", title=ARTIFACT_TITLE,
        data={"steps": ["顶点", "对称轴"]},
    )
    search_state = {"questions": [dict(item) for item in FAKE_QUESTIONS], "error": None, "calls": []}

    def _fake_search(text, subject=None, limit=5):
        search_state["calls"].append({"text": text, "subject": subject, "limit": limit})
        if search_state["error"] is not None:
            raise search_state["error"]
        return [dict(item) for item in search_state["questions"]][:limit]

    monkeypatch.setattr(practice, "search_similar_questions", _fake_search)
    return {
        "annotation_id": int(annotation["annotation"]["annotation_id"]),
        "artifact_id": str(artifact["artifact"]["artifact_id"]),
        "search": search_state,
    }


@pytest.fixture
def api(report_env):
    """本模块 router 的独立小应用；holder 切换当前用户。"""
    app = FastAPI()
    app.include_router(topic_report.router)
    holder = {"current": dict(USER_A)}
    app.dependency_overrides[current_user] = lambda: holder["current"]
    with TestClient(app) as client:
        yield client, holder


def _switch(holder, user=USER_B) -> None:
    holder["current"] = dict(user)


def _create(client, **overrides):
    payload = {"topic": TOPIC}
    payload.update(overrides)
    return client.post(BASE, json=payload)


def _created(client, **overrides) -> dict:
    response = _create(client, **overrides)
    assert response.status_code == 201, response.text
    return response.json()


# ---------------------------------------------------------------------------
# 建表与创建：四类来源 / 引用 / 摘要口径
# ---------------------------------------------------------------------------


def test_init_topic_reports_is_idempotent(report_env):
    """建表幂等：重复调用不报错，列与索引齐全。"""
    topic_report.init_topic_reports()
    topic_report.init_topic_reports()
    with db.connection() as con:
        tables = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        columns = {row[1] for row in con.execute("PRAGMA table_info(topic_reports)")}
        indexes = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    assert "topic_reports" in tables
    assert {
        "id", "user_id", "topic", "title", "generator", "content_md", "sources_json",
        "status", "version", "created_at", "updated_at",
    } <= columns
    assert "idx_topic_reports_user" in indexes


def test_create_report_cites_four_source_kinds(api, report_env):
    """四类来源全部创建成功；文档页码 / chunk / 版本、笔记 id、摘录 id、附件版本进入正文与 sources。"""
    client, _holder = api
    body = _created(
        client,
        document_ids=[DOC_A, DOC_A2],
        note_ids=[NOTE_A],
        kp_ids=[KP_A1, KP_A2],
        artifact_ids=[report_env["artifact_id"]],
    )
    report = body["report"]
    content = body["content_md"]
    sources = body["sources"]

    assert report["topic"] == TOPIC
    assert report["title"] == "专题报告 · " + TOPIC
    assert report["generator"] == "rules"
    assert report["status"] == "ready"
    assert report["version"] == 1
    assert report["created_at"] and report["updated_at"]
    assert isinstance(report["report_id"], int) and report["report_id"] >= 1

    # 标题 + 规则汇编声明 + 主题与范围计数。
    assert content.startswith("# 专题报告 · 二次函数")
    assert "不含 AI 生成内容" in content
    assert "规则汇编" in content
    assert "来源数量：资料 2 / 笔记 1 / 知识点 2 / 附件 1" in content
    assert "关联知识点：二次函数（#%s）、函数图像（#%s）" % (KP_A1, KP_A2) in content

    # 资料引用：标题 / 页码 / chunk_id / 文档版本（内容摘要）。
    assert DOC_A_TITLE in content
    assert "第 1 页 · chunk p1-c1 · 文档版本" in content
    doc_sources = [item for item in sources if item["kind"] == "document"]
    assert doc_sources
    first = doc_sources[0]
    assert first["id"] == DOC_A
    assert first["title"] == DOC_A_TITLE
    assert first["ref"] == "ai_documents:%s" % DOC_A
    assert first["page"] == 1
    assert first["chunk_id"] == "p1-c1"
    assert first["version"] and first["version"] in content

    # 笔记与原文摘录：笔记 id、内容摘录、摘录 id 与所属资料页码同时出现。
    assert "（笔记 #%s）" % NOTE_A in content
    assert "（来源：《%s》第 1 页 · 摘录 #%s）" % (DOC_A_TITLE, report_env["annotation_id"]) in content
    note_sources = [item for item in sources if item["kind"] == "note"]
    assert note_sources[0]["id"] == NOTE_A and note_sources[0]["title"] == "二次函数笔记"
    assert note_sources[0]["quote"]
    annotation_sources = [item for item in sources if item["kind"] == "annotation"]
    assert annotation_sources[0]["id"] == report_env["annotation_id"]
    assert annotation_sources[0]["page"] == 1
    assert annotation_sources[0]["quote"] == "二次函数的图像是抛物线"

    # 学习附件：标题 + kind + 固定版本 v1，不内联内容 / base64。
    artifact_sources = [item for item in sources if item["kind"] == "artifact"]
    assert artifact_sources[0]["id"] == report_env["artifact_id"]
    assert artifact_sources[0]["version"] == 1
    assert artifact_sources[0]["note"] == "flow_animation"
    assert "二次函数流程图（v1）" in content
    assert "可在知序中按该版本打开" in content
    assert "base64" in content
    assert json.dumps(sources, ensure_ascii=False) and "data" not in json.dumps(
        [item for item in sources if item["kind"] == "artifact"], ensure_ascii=False
    )

    # 候选题：题干 + 统一题目引用 + 来源标签，并注明非正式知识点标签。
    assert "（引用：bank:docA:1 · 高考题库）" in content
    assert "（引用：app:12 · 我的题库）" in content
    assert "候选题为题库检索结果，非正式知识点标签" in content
    question_sources = [item for item in sources if item["kind"] == "question"]
    assert {item["ref"] for item in question_sources} == {"bank:docA:1", "app:12"}
    # 检索确实按主题调用且 limit=5。
    assert report_env["search"]["calls"]
    assert report_env["search"]["calls"][0]["text"] == TOPIC
    assert report_env["search"]["calls"][0]["limit"] == 5

    # 详情与创建返回一致。
    detail = client.get("%s/%s" % (BASE, report["report_id"]))
    assert detail.status_code == 200
    assert detail.json()["content_md"] == content
    assert detail.json()["sources"] == sources


def test_keyword_hit_priority_and_opening_excerpt_fallback(api, report_env):
    """命中主题关键字的 chunk 优先引用；无命中资料回退开头 2 段并注明。"""
    client, _holder = api
    content = _created(client, document_ids=[DOC_A, DOC_A2])["content_md"]
    # 资料 A 命中的第一段被引用；不含关键字的第二段不引用。
    assert "> 1.1 二次函数 二次函数的图像是抛物线" in content
    assert "Derivative 表示函数的变化率" not in content
    # 资料 A2 无命中：明确注明，并给出资料开头两段。
    assert "未检索到与主题直接相关的段落，以下为资料开头摘录" in content
    assert "空间点线面的位置关系" in content
    assert "空间向量可以表示方向与距离" in content


def test_note_annotations_only_real_quotes(api, report_env):
    """有摘录的笔记引用真实摘录，无摘录的笔记只有内容摘录，不虚构 quote。"""
    client, _holder = api
    body = _created(client, note_ids=[NOTE_A, NOTE_A2])
    content = body["content_md"]
    annotation_sources = [item for item in body["sources"] if item["kind"] == "annotation"]
    assert len(annotation_sources) == 1
    assert annotation_sources[0]["id"] == report_env["annotation_id"]
    # 两篇笔记的标题 / 内容摘录都在，但只有一篇有真实摘录（不虚构第二条）。
    assert "二次函数笔记" in content and "函数复习笔记" in content
    assert content.count("（来源：《%s》" % DOC_A_TITLE) == 1
    assert "摘录 #%s" % report_env["annotation_id"] in content
    assert "（笔记 #%s）" % NOTE_A2 in content


# ---------------------------------------------------------------------------
# 参数 / 归属校验
# ---------------------------------------------------------------------------


def test_sources_validation_422(api, report_env):
    """四类来源全空 / 主题越界 / 单类超过 10 个 / 编号非法 -> 422（中文 detail）。"""
    client, _holder = api
    empty = _create(client)
    assert empty.status_code == 422
    assert "请至少选择一份资料、笔记、知识点或学习附件" in empty.json()["detail"]

    assert _create(client, topic="二", document_ids=[DOC_A]).status_code == 422
    assert _create(client, topic="二" * 201, document_ids=[DOC_A]).status_code == 422
    assert _create(client, document_ids=list(range(1, 12))).status_code == 422
    bad_id = _create(client, document_ids=[0])
    assert bad_id.status_code == 422 and "资料编号不正确" in bad_id.json()["detail"]
    bad_artifact = _create(client, artifact_ids=[""])
    assert bad_artifact.status_code == 422 and "附件编号不正确" in bad_artifact.json()["detail"]

    # 主题恰好 200 字可创建，默认标题截断到 120 字内。
    long_topic = "二" * 200
    created = _created(client, topic=long_topic, document_ids=[DOC_A])
    assert created["report"]["topic"] == long_topic
    assert created["report"]["title"].startswith("专题报告 · ")
    assert len(created["report"]["title"]) <= topic_report.MAX_TITLE_LENGTH


def test_foreign_sources_404_and_invalid_kp_422(api, report_env):
    """他人资料 / 笔记 / 附件 -> 404（同一文案）；不存在的知识点 -> 422。"""
    client, holder = api
    _switch(holder, USER_B)
    for payload in (
        {"document_ids": [DOC_A]},
        {"note_ids": [NOTE_A]},
        {"artifact_ids": [report_env["artifact_id"]]},
    ):
        response = _create(client, **payload)
        assert response.status_code == 404, response.text
        assert "不存在或无权访问" in response.json()["detail"]

    _switch(holder, USER_A)
    invalid_kp = _create(client, kp_ids=[999999])
    assert invalid_kp.status_code == 422
    assert "知识点不存在" in invalid_kp.json()["detail"]


# ---------------------------------------------------------------------------
# 列表 / 详情 / 删除
# ---------------------------------------------------------------------------


def test_list_and_get_reports(api, report_env):
    """列表最近更新在前且 total 不受 limit 截断；详情含正文与 sources；他人 / 不存在 404。"""
    client, holder = api
    first = _created(client, document_ids=[DOC_A], title="第一份")
    second = _created(client, note_ids=[NOTE_A], title="第二份")

    listing = client.get(BASE)
    assert listing.status_code == 200
    body = listing.json()
    assert body["total"] == 2 and body["limit"] == 20 and body["note"]
    assert [item["report_id"] for item in body["items"]] == [
        second["report"]["report_id"], first["report"]["report_id"],
    ]
    limited = client.get(BASE, params={"limit": 1}).json()
    assert limited["limit"] == 1 and len(limited["items"]) == 1 and limited["total"] == 2

    detail = client.get("%s/%s" % (BASE, first["report"]["report_id"])).json()
    assert detail["content_md"] == first["content_md"]
    assert detail["sources"] == first["sources"]
    assert "第一份" in detail["content_md"]

    missing = client.get("%s/999999" % BASE)
    assert missing.status_code == 404
    _switch(holder, USER_B)
    assert client.get("%s/%s" % (BASE, first["report"]["report_id"])).status_code == 404


def test_delete_report(api, report_env):
    """删除本人报告返回 {}；删除后读取 404、列表移除；他人删除 404。"""
    client, holder = api
    created = _created(client, document_ids=[DOC_A])
    report_id = created["report"]["report_id"]

    deleted = client.delete("%s/%s" % (BASE, report_id))
    assert deleted.status_code == 200
    assert deleted.json() == {"deleted": True, "report_id": report_id}
    assert client.get("%s/%s" % (BASE, report_id)).status_code == 404
    assert client.delete("%s/%s" % (BASE, report_id)).status_code == 404
    assert client.get(BASE).json()["total"] == 0

    other = _created(client, document_ids=[DOC_A])
    _switch(holder, USER_B)
    assert client.delete("%s/%s" % (BASE, other["report"]["report_id"])).status_code == 404


# ---------------------------------------------------------------------------
# 更新：标题 / 正文 / CAS 版本冲突
# ---------------------------------------------------------------------------


def test_update_title_content_and_cas(api, report_env):
    """PATCH 标题与正文各自 +1 版本；过期 expected_version（含 0）409；上限 422；他人 404。"""
    client, holder = api
    created = _created(client, document_ids=[DOC_A], title="原标题")
    report_id = created["report"]["report_id"]
    original_content = created["content_md"]

    renamed = client.patch("%s/%s" % (BASE, report_id), json={"title": "新标题"})
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["report"]["version"] == 2
    assert renamed.json()["report"]["title"] == "新标题"
    assert renamed.json()["content_md"] == original_content

    rewritten = client.patch(
        "%s/%s" % (BASE, report_id), json={"content_md": "# 自定义正文\n\n用户改写的正文。"}
    )
    assert rewritten.status_code == 200
    assert rewritten.json()["report"]["version"] == 3
    assert rewritten.json()["content_md"] == "# 自定义正文\n\n用户改写的正文。"
    # 正文改写不改变来源快照。
    assert rewritten.json()["sources"] == created["sources"]

    stale = client.patch(
        "%s/%s" % (BASE, report_id), json={"title": "冲突", "expected_version": 2}
    )
    assert stale.status_code == 409
    assert "刷新" in stale.json()["detail"]
    # 过期的 0 版本号也是冲突（不是参数错误）：兼容集成测试的 version-1 口径。
    assert client.patch(
        "%s/%s" % (BASE, report_id), json={"title": "冲突", "expected_version": 0}
    ).status_code == 409
    # 冲突不写入：版本与标题保持上一次成功更新后的值。
    current = client.get("%s/%s" % (BASE, report_id)).json()["report"]
    assert current["version"] == 3 and current["title"] == "新标题"

    assert client.patch("%s/%s" % (BASE, report_id), json={}).status_code == 422
    assert client.patch(
        "%s/%s" % (BASE, report_id), json={"title": "标" * 121}
    ).status_code == 422
    too_large = client.patch(
        "%s/%s" % (BASE, report_id),
        json={"content_md": "内容" * (topic_report.MAX_CONTENT_BYTES // 2)},
    )
    assert too_large.status_code == 422
    assert "200 KB" in too_large.json()["detail"]

    _switch(holder, USER_B)
    assert client.patch("%s/%s" % (BASE, report_id), json={"title": "他人"}).status_code == 404


def test_update_reorders_listing(api, report_env):
    """更新后 ``updated_at`` 刷新，列表把该报告排在前面。"""
    client, _holder = api
    first = _created(client, document_ids=[DOC_A], title="较早")
    second = _created(client, note_ids=[NOTE_A], title="较新")
    assert client.get(BASE).json()["items"][0]["report_id"] == second["report"]["report_id"]
    updated = client.patch(
        "%s/%s" % (BASE, first["report"]["report_id"]),
        json={"title": "较早（更新）", "expected_version": 1},
    )
    assert updated.status_code == 200
    assert client.get(BASE).json()["items"][0]["report_id"] == first["report"]["report_id"]


# ---------------------------------------------------------------------------
# Markdown 下载
# ---------------------------------------------------------------------------


def test_report_markdown_download(api, report_env):
    """Markdown 下载内容与 content_md 一致；文件名安全且中文名按 RFC 5987 编码。"""
    client, holder = api
    created = _created(client, document_ids=[DOC_A])
    report_id = created["report"]["report_id"]

    response = client.get("%s/%s/markdown" % (BASE, report_id))
    assert response.status_code == 200
    assert "text/markdown" in response.headers["content-type"]
    assert response.text == created["content_md"]

    disposition = response.headers["content-disposition"]
    assert "attachment" in disposition
    assert 'filename="topic-report-v1.md"' in disposition
    encoded = disposition.split("filename*=UTF-8''", 1)[1]
    filename = unquote(encoded)
    expected_name, expected_text = topic_report.report_markdown(
        USER_A["id"], report_id
    )
    assert filename == expected_name == "专题报告-二次函数-v1.md"
    assert expected_text == created["content_md"]
    for character in '\\/:*?"<>|':
        assert character not in filename

    # 非法文件名字符被清洗，不能逃出目录。
    weird_topic = '二次/函数:测试*报告?"<>|'
    weird = _created(client, topic=weird_topic, document_ids=[DOC_A])
    weird_md = client.get("%s/%s/markdown" % (BASE, weird["report"]["report_id"]))
    assert weird_md.status_code == 200
    weird_filename = unquote(
        weird_md.headers["content-disposition"].split("filename*=UTF-8''", 1)[1]
    )
    assert weird_filename.startswith("专题报告-二次_函数_测试_报告")
    assert weird_filename.endswith("-v1.md")
    for character in '\\/:*?"<>|':
        assert character not in weird_filename

    assert client.get("%s/999999/markdown" % BASE).status_code == 404
    _switch(holder, USER_B)
    assert client.get("%s/%s/markdown" % (BASE, report_id)).status_code == 404


# ---------------------------------------------------------------------------
# 数量上限 / 确定性 / 两账号隔离 / 规则汇编口径
# ---------------------------------------------------------------------------


def test_report_count_limit_422(api, report_env, monkeypatch):
    """个人报告数达上限（monkeypatch 调小到 2）后创建 -> 422 中文提示。"""
    client, _holder = api
    monkeypatch.setattr(topic_report, "MAX_REPORTS_PER_USER", 2)
    assert _create(client, document_ids=[DOC_A]).status_code == 201
    assert _create(client, note_ids=[NOTE_A]).status_code == 201
    rejected = _create(client, document_ids=[DOC_A2])
    assert rejected.status_code == 422
    assert "上限" in rejected.json()["detail"]
    assert "2 份" in rejected.json()["detail"]


def test_report_is_deterministic_for_same_inputs(api, report_env, monkeypatch):
    """冻结时钟后同一输入生成完全相同的正文与 sources（不依赖随机 / AI）。"""
    client, _holder = api
    monkeypatch.setattr(db, "utcnow", lambda: FIXED_NOW)
    payload = {
        "document_ids": [DOC_A, DOC_A2],
        "note_ids": [NOTE_A],
        "kp_ids": [KP_A1],
        "artifact_ids": [report_env["artifact_id"]],
    }
    first = _created(client, **payload)
    second = _created(client, topic=TOPIC, **payload)
    assert first["content_md"] == second["content_md"]
    assert first["sources"] == second["sources"]
    assert FIXED_NOW in first["content_md"]
    assert first["report"]["report_id"] != second["report"]["report_id"]


def test_two_accounts_isolated(api, report_env):
    """两账号报告互不可见：列表、详情、更新、删除都隔离；各自可创建自己的报告。"""
    client, holder = api
    mine = _created(client, document_ids=[DOC_A])
    mine_id = mine["report"]["report_id"]

    _switch(holder, USER_B)
    assert client.get(BASE).json()["total"] == 0
    assert client.get("%s/%s" % (BASE, mine_id)).status_code == 404
    assert client.patch("%s/%s" % (BASE, mine_id), json={"title": "他人"}).status_code == 404
    assert client.delete("%s/%s" % (BASE, mine_id)).status_code == 404
    theirs = _created(client, document_ids=[DOC_B], note_ids=[NOTE_B])
    assert client.get(BASE).json()["total"] == 1
    assert client.get("%s/%s" % (BASE, theirs["report"]["report_id"])).status_code == 200

    _switch(holder, USER_A)
    listing = client.get(BASE).json()
    assert listing["total"] == 1
    assert listing["items"][0]["report_id"] == mine_id


def test_report_is_rule_based_not_ai(api, report_env):
    """报告固定 generator=rules，正文注明规则汇编 / 非 AI 生成；模块不引用 AI 供应商。"""
    source = inspect.getsource(topic_report)
    assert "providers" not in source
    assert "openai" not in source.lower()
    assert "ai_providers" not in source
    assert topic_report.GENERATOR == "rules"

    client, _holder = api
    body = _created(client, document_ids=[DOC_A])
    assert body["report"]["generator"] == "rules"
    content = body["content_md"]
    assert "规则汇编" in content
    assert "非 AI 生成" in content
    assert "不含 AI 生成内容" in content
    assert "口径与局限" in content
    assert "引用可能失效" in content


def test_evidence_section_uses_real_fields(api, report_env, monkeypatch):
    """学习证据小节只写 summary 真实字段；样本门槛可调，充足与不足两种口径都出现。"""
    client, _holder = api
    monkeypatch.setattr(topic_report, "EVIDENCE_MIN_EFFECTIVE", 2)
    for record_id, verdict in ((1, "correct"), (2, "incorrect")):
        study_evidence.record_event(
            USER_A["id"], kind="attempt", record_kind="attempts", verdict=verdict,
            question_ref="bank:docA:1", record_id=record_id,
        )
    body = _created(client, document_ids=[DOC_A])
    content = body["content_md"]
    assert "有效证据：2 条" in content
    assert "已判定作答：正确 1 / 错误 1" in content
    assert "学生自评：0 条" in content
    assert "样本是否充足：样本充足" in content

    monkeypatch.setattr(topic_report, "EVIDENCE_MIN_EFFECTIVE", 5)
    strict = _created(client, note_ids=[NOTE_A])["content_md"]
    assert "有效证据：2 条" in strict
    assert "样本是否充足：样本不足" in strict


def test_question_search_degradation(api, report_env):
    """题库检索不可用时正文如实降级说明，报告仍成功（不抛 503）。"""
    client, _holder = api
    report_env["search"]["error"] = FileNotFoundError("未找到题库")
    body = _created(client, document_ids=[DOC_A])
    assert "题库暂不可用，未检索到候选题" in body["content_md"]
    assert not [item for item in body["sources"] if item["kind"] == "question"]

    report_env["search"]["error"] = None
    report_env["search"]["questions"] = []
    empty = _created(client, note_ids=[NOTE_A])
    assert "未检索到相关候选题" in empty["content_md"]
    assert "候选题为题库检索结果，非正式知识点标签" in empty["content_md"]


def test_create_report_supports_caller_connection(api, report_env):
    """公开函数支持调用方事务：``con`` 传入时写入同一连接，由调用方提交。"""
    client, _holder = api
    with db.connection() as con:
        created = topic_report.create_report(
            USER_A["id"], topic=TOPIC, note_ids=[NOTE_A], con=con
        )
    assert created["report"]["version"] == 1
    detail = client.get("%s/%s" % (BASE, created["report"]["report_id"]))
    assert detail.status_code == 200
    assert detail.json()["content_md"] == created["content_md"]


def test_router_models_are_wired():
    """请求模型字段与路由路径稳定（接线方依赖的接口面）。"""
    create_fields = set(topic_report.CreateReportInput.model_fields)
    assert {
        "topic", "title", "document_ids", "note_ids", "kp_ids", "artifact_ids",
    } == create_fields
    update_fields = set(topic_report.UpdateReportInput.model_fields)
    assert {"title", "content_md", "expected_version"} == update_fields
    paths = {(route.path, tuple(sorted(route.methods))) for route in topic_report.router.routes}
    assert ("/api/topic-reports", ("POST",)) in paths
    assert ("/api/topic-reports", ("GET",)) in paths
    assert ("/api/topic-reports/{report_id}", ("GET",)) in paths
    assert ("/api/topic-reports/{report_id}", ("PATCH",)) in paths
    assert ("/api/topic-reports/{report_id}", ("DELETE",)) in paths
    assert ("/api/topic-reports/{report_id}/markdown", ("GET",)) in paths
    # 模型校验本身也能拦住明显非法请求体（路由层 422 的第一道防线）。
    with pytest.raises(ValidationError):
        topic_report.CreateReportInput(topic=TOPIC, document_ids=["不是编号"])
    with pytest.raises(ValidationError):
        topic_report.CreateReportInput(topic=TOPIC, title=123)
