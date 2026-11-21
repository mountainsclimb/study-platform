"""互动讲义（``app/handout.py``）的隔离测试。

本文件不依赖 ``app.main``：用 ``isolated_db`` 把数据目录指到 tmp_path，依次 ``db.init_db()``、
``ai.init_ai()``、``classroom`` 建表、``study_artifact`` / ``doc_location`` / ``study_evidence``
/ ``handout`` 各自的 ``init_*`` 幂等建表；用 ``db.connection()`` 直接造用户、课程与场景、
资料与笔记摘录、知识点，用 ``study_artifact.create_artifact`` 造固定版本附件，用
``study_evidence.record_event`` 造真实学习证据。本地 ``FastAPI()`` 只挂
``handout.router``，通过 ``app.dependency_overrides[current_user]`` 注入当前用户。

覆盖：建表幂等与列齐全；课程 + 笔记 + 附件 + 知识点四类来源创建后的小节顺序与真实内容
（场景标题 / 类型中文名 / 旁白摘要、附件固定版本与预览、摘录出处、候选题 ref、证据字段、
规则汇编声明）；空来源 422；他人课程 / 笔记 / 附件 404 与非法知识点 422；列表 / 详情 / 删除；
两账号隔离；自包含 HTML 导出与注入转义；预览累计超限省略；PDF 两条分支（503 / 200）；
导出格式非法 422；数量与标题上限；同输入两次 sections 确定性。
"""
import base64
import json

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
import pytest

from app import ai, db, doc_location, handout, study_artifact, study_evidence
from app.ai import render as ai_render
from app.auth import current_user
from app.classroom import db as classroom_db

USER_A = {"id": 8101, "username": "讲义测试甲", "role": "student"}
USER_B = {"id": 8102, "username": "讲义测试乙", "role": "student"}

COURSE_TITLE = "二次函数与图像"
COURSE_SUBJECT = "数学"
DOC_TITLE = "数学讲义（讲义测试资料）"
NOTE_TITLE = "二次函数摘录笔记"
ARTIFACT_TITLE = "二次函数图像"
KP_NAME = "二次函数的图像与性质"
QUOTE_TEXT = "二次函数的图像是一条抛物线，其对称轴为直线 x=-b/(2a)。"
DOC_CHUNKS = [
    {
        "page": 2,
        "chunk_id": "p2-c1",
        "heading_path": "第二章 二次函数",
        "text": "第二章 二次函数\n" + QUOTE_TEXT,
    }
]
PRACTICE_HIT = {
    "qid": "q-handout-001",
    "subject": "数学",
    "year": 2023,
    "province": "新课标Ⅰ卷",
    "paper_name": "",
    "type": "single",
    "type_label": "单选题",
    "stem_plain": "已知二次函数 y=x^2-4x+3，求其顶点坐标与对称轴。",
    "similarity": 0.9,
    "exact_match": False,
}

SECTION_KEYS = {"id", "kind", "title", "paragraphs", "quotes", "items"}
HANDOUT_DETAIL_KEYS = {
    "handout_id", "title", "course_id", "version", "generator", "status",
    "created_at", "updated_at",
}
HANDOUT_SUMMARY_KEYS = {
    "handout_id", "title", "course_id", "version", "section_count", "created_at", "updated_at",
}
TABLE_COLUMNS = {
    "id", "user_id", "title", "course_id", "note_ids_json", "artifact_ids_json",
    "question_refs_json", "sections_json", "generator", "version", "created_at", "updated_at",
}


# ---------------------------------------------------------------------------
# 夹具与工具
# ---------------------------------------------------------------------------


def _insert_user(con, user) -> None:
    con.execute(
        "INSERT INTO users(id,username,password_hash,role,created_at) VALUES (?,?,?,?,?)",
        (user["id"], user["username"], "unit-test-hash", "student", db.utcnow()),
    )


def _insert_course(con, user_id, title=COURSE_TITLE) -> int:
    now = db.utcnow()
    return con.execute(
        "INSERT INTO cls_courses(user_id,title,topic,subject,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?)",
        (user_id, title, "二次函数", COURSE_SUBJECT, now, now),
    ).lastrowid


def db_json(value) -> str:
    return json.dumps(value, ensure_ascii=False)


def _insert_scene(con, course_id, user_id, scene_type, order_index, title, actions):
    now = db.utcnow()
    return con.execute(
        "INSERT INTO cls_scenes(course_id,user_id,scene_type,order_index,title,content_json,"
        "meta_json,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            course_id,
            user_id,
            scene_type,
            order_index,
            title,
            db_json({"type": scene_type}),
            db_json({"actions": actions}),
            "ready",
            now,
            now,
        ),
    ).lastrowid


def _insert_document(con, user_id, title=DOC_TITLE) -> int:
    return con.execute(
        "INSERT INTO ai_documents(user_id,title,storage_name,pages,status,chunks_json,created_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (
            user_id,
            title,
            "doc-handout-test.txt",
            2,
            "ready",
            db_json(DOC_CHUNKS),
            db.utcnow(),
        ),
    ).lastrowid


def _insert_note(con, user_id, title=NOTE_TITLE, document_id=None) -> int:
    now = db.utcnow()
    return con.execute(
        "INSERT INTO ai_notes(user_id,document_id,title,content,sources_json,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (user_id, document_id, title, "本笔记用于整理原文摘录。", "[]", now, now),
    ).lastrowid


def _insert_annotation(con, user_id, document_id, note_id) -> int:
    now = db.utcnow()
    return con.execute(
        "INSERT INTO doc_annotations(user_id,document_id,note_id,page,chunk_id,heading_path,"
        "quote,snippet,color,note,doc_version,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            user_id,
            document_id,
            note_id,
            2,
            "p2-c1",
            "第二章 二次函数",
            QUOTE_TEXT,
            QUOTE_TEXT,
            "yellow",
            "",
            doc_location.document_version(DOC_CHUNKS),
            now,
            now,
        ),
    ).lastrowid


def _insert_kp(con, name=KP_NAME) -> int:
    return con.execute(
        "INSERT INTO knowledge_points(subject,parent_id,name,level,aliases_json,description,sort,source) "
        "VALUES (?,?,?,?,?,?,?,?)",
        ("数学", None, name, 1, "[]", "", 0, "official"),
    ).lastrowid


def _png_data_uri(payload: bytes = b"\x89PNG\r\n\x1a\nhandout-test") -> str:
    return "data:image/png;base64," + base64.b64encode(payload).decode("ascii")


@pytest.fixture
def env(isolated_db):
    """隔离应用库：建全部相关表、造两个账号与真实来源数据，返回 id 映射。"""
    db.init_db()
    # ai_documents / ai_notes 的真实建表语句来自 AI 模块（讲义只读这两张表）。
    ai.init_ai()
    classroom_db.init_classroom_db()
    study_artifact.init_study_artifacts()
    doc_location.init_doc_location()
    study_evidence.init_study_evidence()
    handout.init_handouts()
    ids: dict = {}
    with db.connection() as con:
        _insert_user(con, USER_A)
        _insert_user(con, USER_B)
        # 甲：课程 + 2 个场景（slides / quiz，旁白在 meta.actions）。
        ids["course_id"] = _insert_course(con, USER_A["id"])
        _insert_scene(
            con,
            ids["course_id"],
            USER_A["id"],
            "slide",
            0,
            "认识二次函数",
            [
                {"id": "a1", "type": "speech", "text": "今天我们来学习二次函数的图像与性质。"},
            ],
        )
        _insert_scene(
            con,
            ids["course_id"],
            USER_A["id"],
            "quiz",
            1,
            "课堂小测",
            [{"id": "a2", "type": "speech", "text": "请独立完成本页小测。"}],
        )
        # 甲：资料 + 笔记 + 摘录。
        ids["document_id"] = _insert_document(con, USER_A["id"])
        ids["note_id"] = _insert_note(
            con, USER_A["id"], title=NOTE_TITLE, document_id=ids["document_id"]
        )
        ids["annotation_id"] = _insert_annotation(
            con, USER_A["id"], ids["document_id"], ids["note_id"]
        )
        ids["note_empty_id"] = _insert_note(
            con, USER_A["id"], title="暂无摘录的笔记", document_id=None
        )
        # 甲：知识点。
        ids["kp_id"] = _insert_kp(con)
        # 乙：用于归属隔离的课程 / 笔记 / 附件。
        ids["course_b"] = _insert_course(con, USER_B["id"], title="他人的课程")
        ids["note_b"] = _insert_note(con, USER_B["id"], title="他人的笔记", document_id=None)
    ids["artifact"] = study_artifact.create_artifact(
        USER_A["id"],
        kind="function_graph",
        title=ARTIFACT_TITLE,
        data={"expression": "y=x^2"},
        preview={"png": _png_data_uri()},
        source_ref="app:handout-test",
    )["artifact"]["artifact_id"]
    ids["artifact_b"] = study_artifact.create_artifact(
        USER_B["id"],
        kind="whiteboard",
        title="他人的白板",
        data={"elements": []},
    )["artifact"]["artifact_id"]
    study_evidence.record_event(
        USER_A["id"],
        kind="attempt",
        record_kind="attempts",
        record_id=1,
        verdict="correct",
        grade_source="reference",
        prompt_state="none",
        reveal_state="before",
        question_ref="bank:q-handout-evidence",
    )
    return ids


@pytest.fixture(autouse=True)
def fake_practice_search(monkeypatch):
    """题库检索固定返回一条候选题（不联网、不依赖真实题库索引，保证确定性）。"""

    def fake(text, subject=None, limit=5):
        return [dict(PRACTICE_HIT)]

    monkeypatch.setattr(handout.practice, "search_similar_questions", fake)


@pytest.fixture
def api(env):
    """本模块 router 的独立小应用；holder 切换当前用户，不依赖真实登录 cookie。"""
    app = FastAPI()
    app.include_router(handout.router)
    holder = {"user": dict(USER_A)}
    app.dependency_overrides[current_user] = lambda: holder["user"]
    client = TestClient(app)
    try:
        yield client, holder, env
    finally:
        client.close()


def _as(holder, user) -> None:
    holder["user"] = dict(user)


def _create(client, **payload):
    return client.post("/api/handouts", json=payload)


def _created(client, **payload) -> dict:
    response = _create(client, **payload)
    assert response.status_code == 201, response.text
    return response.json()


def _all_sources(env) -> dict:
    return {
        "course_id": env["course_id"],
        "note_ids": [env["note_id"]],
        "artifact_ids": [env["artifact"]],
        "kp_ids": [env["kp_id"]],
    }


def _section(body: dict, kind: str) -> dict:
    for section in body["sections"]:
        if section["kind"] == kind:
            return section
    raise AssertionError(f"缺少小节：{kind}")


def _paragraphs(section: dict) -> str:
    return "\n".join(section["paragraphs"])


# ---------------------------------------------------------------------------
# 建表
# ---------------------------------------------------------------------------


def test_init_handouts_is_idempotent_and_columns_complete(env):
    """建表幂等：重复调用不报错，``handouts`` 列与交付规格完全一致。"""
    handout.init_handouts()
    handout.init_handouts()
    with db.connection() as con:
        tables = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        columns = {row[1] for row in con.execute("PRAGMA table_info(handouts)")}
    assert "handouts" in tables
    assert columns == TABLE_COLUMNS
    assert "handouts" in db._ID_TABLES


# ---------------------------------------------------------------------------
# 创建：小节顺序与真实内容
# ---------------------------------------------------------------------------


def test_create_with_all_sources_builds_sections_in_fixed_order(api):
    """课程 + 笔记 + 附件 + 知识点：小节固定顺序、内容全部来自真实行。"""
    client, _holder, env = api
    body = _created(client, **_all_sources(env))
    assert set(body) == {"handout", "sections"}
    detail = body["handout"]
    assert set(detail) == HANDOUT_DETAIL_KEYS
    assert detail["title"] == f"互动讲义 · {COURSE_TITLE}"
    assert detail["course_id"] == env["course_id"]
    assert detail["version"] == 1 and detail["generator"] == "rules"
    assert detail["status"] == "ready"
    assert detail["created_at"] and detail["updated_at"]
    # 固定顺序。
    assert [section["kind"] for section in body["sections"]] == list(handout.SECTION_ORDER)
    for section in body["sections"]:
        assert set(section) == SECTION_KEYS
        assert isinstance(section["paragraphs"], list)
        assert isinstance(section["quotes"], list)
        assert isinstance(section["items"], list)

    # 1. 课程大纲：真实场景标题、中文类型、旁白摘要。
    outline = _section(body, "course_outline")
    assert outline["items"][0]["title"] == "认识二次函数"
    assert "幻灯片" in outline["items"][0]["detail"]
    assert "今天我们来学习二次函数的图像与性质" in outline["items"][0]["detail"]
    assert outline["items"][1]["title"] == "课堂小测"
    assert "课堂测验" in outline["items"][1]["detail"]
    assert f"场景数：2 个。" in _paragraphs(outline)

    # 2. 学习附件：kind 中文名、固定版本、来源说明与 PNG 预览。
    artifact_section = _section(body, "artifact")
    assert len(artifact_section["items"]) == 1
    item = artifact_section["items"][0]
    assert item["title"] == ARTIFACT_TITLE
    assert "函数图" in item["detail"]
    assert "固定版本 v1" in item["detail"]
    assert "app:handout-test" in item["detail"]
    assert item["ref"].startswith("artifact:") and item["ref"].endswith("@1")
    assert item["preview_data_uri"].startswith("data:image/png;base64,")

    # 3. 原文摘录：text 为真实 quote，source 含文档标题 / 页码 / 摘录 id。
    excerpt = _section(body, "excerpt")
    assert len(excerpt["quotes"]) == 1
    assert excerpt["quotes"][0]["text"] == QUOTE_TEXT
    assert excerpt["quotes"][0]["source"] == (
        f"《{DOC_TITLE}》第 2 页 · 摘录 #{env['annotation_id']}"
    )
    assert NOTE_TITLE in _paragraphs(excerpt)

    # 4. 练习候选题：ref 为题库 qid、title 为题干摘要、来源真实。
    practice = _section(body, "practice")
    assert len(practice["items"]) == 1
    candidate = practice["items"][0]
    assert candidate["ref"] == PRACTICE_HIT["qid"]
    assert candidate["title"] == PRACTICE_HIT["stem_plain"]
    assert candidate["detail"] == "来源：数学 · 2023 年 · 新课标Ⅰ卷"
    assert "非正式标签" in _paragraphs(practice)

    # 5. 学习证据：有效事件 / 独立作答 / 样本是否充足来自真实 evidence 行。
    evidence = _section(body, "evidence")
    evidence_text = _paragraphs(evidence)
    assert "有效事件 1 条" in evidence_text
    assert "独立作答（独立且核验）1 条" in evidence_text
    assert "样本是否充足" in evidence_text
    assert "样本不足" in evidence_text
    assert "正确 1 条" in evidence_text

    # 6. 生成说明：规则汇编 + 引用局限 + generator。
    meta = _section(body, "meta")
    meta_text = _paragraphs(meta)
    assert "规则汇编，非 AI 生成" in meta_text
    assert "不含 AI 生成" in meta_text
    assert "引用随资料/附件版本或删除可能失效" in meta_text
    assert "附件按创建时固定版本引用" in meta_text
    assert "generator：rules" in meta_text


def test_empty_sources_422(api):
    """没有任何来源（{} 或全部空数组）→ 422 中文提示。"""
    client, _holder, _env = api
    response = _create(client)
    assert response.status_code == 422
    assert "请至少选择一门课程、一篇笔记、一份学习附件或一个知识点" in response.json()["detail"]
    response = _create(client, note_ids=[], artifact_ids=[], kp_ids=[])
    assert response.status_code == 422
    assert "请至少选择" in response.json()["detail"]


def test_foreign_sources_404_and_missing_kp_422(api):
    """他人课程 / 笔记 / 附件 404；不存在的知识点 422。"""
    client, _holder, env = api
    cases = [
        ({"course_id": env["course_b"]}, 404, "未找到该课程"),
        ({"note_ids": [env["note_b"]]}, 404, "笔记不存在或无权访问"),
        ({"artifact_ids": [env["artifact_b"]]}, 404, "附件不存在或无权访问"),
        ({"kp_ids": [999999]}, 422, "知识点不存在或不可用"),
    ]
    for payload, status, message in cases:
        response = _create(client, **payload)
        assert response.status_code == status, (payload, response.text)
        assert message in response.json()["detail"]


def test_note_without_annotations_does_not_fabricate_quotes(api):
    """笔记没有摘录时 excerpt 只写说明、quotes 为空（不虚构）。"""
    client, _holder, env = api
    body = _created(client, note_ids=[env["note_empty_id"]])
    excerpt = _section(body, "excerpt")
    assert excerpt["quotes"] == []
    assert "不虚构" in _paragraphs(excerpt)


# ---------------------------------------------------------------------------
# 列表 / 详情 / 删除 / 隔离
# ---------------------------------------------------------------------------


def test_list_get_delete_flow(api):
    """创建两份讲义后：列表摘要、详情小节、删除与删除后 404。"""
    client, _holder, env = api
    first = _created(client, course_id=env["course_id"])["handout"]
    second = _created(client, note_ids=[env["note_id"]])["handout"]

    listing = client.get("/api/handouts")
    assert listing.status_code == 200
    data = listing.json()
    assert data["total"] == 2 and data["limit"] == 20
    assert {item["handout_id"] for item in data["items"]} == {
        first["handout_id"], second["handout_id"],
    }
    for item in data["items"]:
        assert set(item) == HANDOUT_SUMMARY_KEYS
        assert item["section_count"] == 3

    detail = client.get(f"/api/handouts/{first['handout_id']}")
    assert detail.status_code == 200
    detail_body = detail.json()
    assert set(detail_body) == {"handout", "sections"}
    assert [section["kind"] for section in detail_body["sections"]] == [
        "course_outline", "evidence", "meta",
    ]

    deleted = client.delete(f"/api/handouts/{second['handout_id']}")
    assert deleted.status_code == 200
    assert deleted.json() == {"deleted": True, "handout_id": second["handout_id"]}
    assert client.get(f"/api/handouts/{second['handout_id']}").status_code == 404
    assert client.delete(f"/api/handouts/{second['handout_id']}").status_code == 404
    assert client.get("/api/handouts").json()["total"] == 1


def test_two_accounts_are_isolated(api):
    """两账号：列表、详情、删除、导出都看不到对方讲义；来源归属同样隔离。"""
    client, holder, env = api
    created = _created(client, course_id=env["course_id"])["handout"]["handout_id"]
    _as(holder, USER_B)
    assert client.get("/api/handouts").json()["total"] == 0
    for method, path in (
        ("get", f"/api/handouts/{created}"),
        ("delete", f"/api/handouts/{created}"),
        ("get", f"/api/handouts/{created}/export"),
    ):
        response = getattr(client, method)(path)
        assert response.status_code == 404, (method, path, response.text)
    # 乙用自己的课程创建成功，且看不到甲的讲义。
    own = _created(client, course_id=env["course_b"])
    assert own["handout"]["handout_id"] != created
    assert client.get("/api/handouts").json()["total"] == 1
    _as(holder, USER_A)
    assert client.get("/api/handouts").json()["total"] == 1


def test_direct_functions_404_for_missing_handout(env):
    """直接调用公开函数：不存在 / 非本人讲义统一 404。"""
    with pytest.raises(HTTPException) as error:
        handout.get_handout(USER_A["id"], 424242)
    assert error.value.status_code == 404
    assert "讲义不存在或无权访问" in error.value.detail
    with pytest.raises(HTTPException):
        handout.delete_handout(USER_A["id"], 424242)
    with pytest.raises(HTTPException):
        handout.handout_html(USER_A["id"], 424242)


# ---------------------------------------------------------------------------
# HTML 导出
# ---------------------------------------------------------------------------


def test_html_export_is_self_contained_single_file(api):
    """HTML 导出：自包含（无外链 / 无脚本）、带固定版本标记、内容来自真实小节。"""
    client, _holder, env = api
    created = _created(client, **_all_sources(env))["handout"]
    response = client.get(f"/api/handouts/{created['handout_id']}/export")
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/html")
    disposition = response.headers["content-disposition"]
    assert disposition.startswith("attachment;")
    assert f'filename="handout-{created["handout_id"]}.html"' in disposition
    assert "filename*=UTF-8''" in disposition

    page = response.text
    assert '<meta charset="utf-8">' in page
    assert "<style>" in page
    assert f'data-handout-id="{created["handout_id"]}"' in page
    assert 'data-version="1"' in page
    assert "规则汇编 · 引用局限" in page
    assert "非 AI 生成" in page
    # 自包含：不允许任何外链资源或脚本。
    assert "http://" not in page and "https://" not in page
    assert "<script" not in page.lower()
    # 内容来自真实行：课程、摘录、候选题、证据数字。
    assert COURSE_TITLE in page
    assert QUOTE_TEXT in page
    assert PRACTICE_HIT["qid"] in page
    assert "有效事件 1 条" in page
    # 附件预览是合法 PNG data URI。
    assert "data:image/png;base64," in page


def test_html_export_escapes_injected_markup_and_unsafe_preview(api):
    """标题 / 笔记名 / 资料名 / 附件名里的 HTML 与脚本被转义；非法预览不进 src。"""
    client, _holder, env = api
    malicious_title = '<script>alert("讲义")</script>'
    with db.connection() as con:
        con.execute("UPDATE ai_notes SET title=? WHERE id=?", (malicious_title, env["note_id"]))
        con.execute(
            "UPDATE ai_documents SET title=? WHERE id=?",
            ('<img src=x onerror=alert(1)>', env["document_id"]),
        )
    unsafe_artifact = study_artifact.create_artifact(
        USER_A["id"],
        kind="whiteboard",
        title='<script>alert("附件")</script>',
        data={"elements": []},
        preview={"png": "javascript:alert(1)"},
    )["artifact"]["artifact_id"]
    created = _created(
        client,
        title=malicious_title,
        note_ids=[env["note_id"]],
        artifact_ids=[unsafe_artifact],
    )["handout"]
    page = client.get(f"/api/handouts/{created['handout_id']}/export").text
    assert "<script" not in page.lower()
    assert "<img src=x" not in page
    assert "&lt;script&gt;" in page
    assert "javascript:" not in page
    assert created["title"] not in page  # 原始未转义标题不会被直接拼进 HTML


def test_artifact_fixed_version_snapshot_survives_revision(api):
    """附件按创建时固定版本引用：源附件升版后，讲义小节与 HTML 导出仍保持 v1 快照。"""
    client, _holder, env = api
    created = _created(client, artifact_ids=[env["artifact"]])["handout"]
    detail = client.get(f"/api/handouts/{created['handout_id']}").json()
    section = _section(detail, "artifact")
    assert "固定版本 v1" in section["items"][0]["detail"]
    assert section["items"][0]["ref"].endswith("@1")

    study_artifact.add_revision(
        USER_A["id"],
        env["artifact"],
        title="二次函数图像（新版）",
        data={"expression": "y=2x^2"},
    )
    page = client.get(f"/api/handouts/{created['handout_id']}/export").text
    assert "二次函数图像（新版）" not in page
    assert "固定版本 v1" in page


def test_html_preview_budget_omits_oversized_previews(api):
    """内嵌预览累计超过 300KB 时省略并注明（真实经 study_artifact 保存的 PNG data URI）。"""
    client, _holder, _env = api
    big_uri = "data:image/png;base64," + "A" * 63000
    artifact_ids = [
        study_artifact.create_artifact(
            USER_A["id"],
            kind="function_graph",
            title=f"大预览附件{i}",
            data={"expression": f"y=x^{i}"},
            preview={"png": big_uri},
        )["artifact"]["artifact_id"]
        for i in range(5)
    ]
    body = _created(client, artifact_ids=artifact_ids)
    section = _section(body, "artifact")
    assert len(section["items"]) == 5
    with_preview = [item for item in section["items"] if item.get("preview_data_uri")]
    assert len(with_preview) == 4  # 4 × ~63KB ≤ 300KB，第 5 份超限
    text = _paragraphs(section)
    assert "预览过大已省略" in text
    assert "大预览附件4" in text


# ---------------------------------------------------------------------------
# PDF 导出与格式校验
# ---------------------------------------------------------------------------


def test_pdf_export_503_when_renderer_unavailable(api, monkeypatch):
    """渲染依赖不可用 → 503 中文，引导改用 HTML 导出/浏览器打印。"""
    client, _holder, env = api
    created = _created(client, course_id=env["course_id"])["handout"]
    monkeypatch.setattr(
        ai_render,
        "render_available",
        lambda: {"available": False, "engine": None, "detail": "未安装 Playwright"},
    )
    response = client.get(f"/api/handouts/{created['handout_id']}/export", params={"format": "pdf"})
    assert response.status_code == 503
    detail = response.json()["detail"]
    assert "PDF 渲染不可用" in detail
    assert "HTML" in detail


def test_pdf_export_200_when_renderer_available(api, monkeypatch):
    """渲染可用 → 200 application/pdf，附件下载头含安全文件名与 RFC 5987 中文名。"""
    client, _holder, env = api
    created = _created(client, course_id=env["course_id"])["handout"]
    monkeypatch.setattr(
        ai_render,
        "render_available",
        lambda: {"available": True, "engine": "playwright", "detail": "可用"},
    )
    captured: dict = {}

    async def fake_html_to_pdf(html, **kwargs):
        captured["html"] = html
        return b"%PDF-1.4 fake handout"

    monkeypatch.setattr(ai_render, "html_to_pdf", fake_html_to_pdf)
    response = client.get(
        f"/api/handouts/{created['handout_id']}/export", params={"format": "pdf"}
    )
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("application/pdf")
    assert response.content.startswith(b"%PDF")
    disposition = response.headers["content-disposition"]
    assert disposition.startswith("attachment;")
    assert f'filename="handout-{created["handout_id"]}.pdf"' in disposition
    assert "filename*=UTF-8''" in disposition
    # 渲染输入是 HTML 导出同一份自包含文档。
    assert f'data-handout-id="{created["handout_id"]}"' in captured["html"]


def test_export_format_invalid_422(api):
    """format 只接受 html / pdf，其它值 422 中文。"""
    client, _holder, env = api
    created = _created(client, course_id=env["course_id"])["handout"]
    response = client.get(
        f"/api/handouts/{created['handout_id']}/export", params={"format": "docx"}
    )
    assert response.status_code == 422
    assert "导出格式不正确" in response.json()["detail"]
    # 大小写不敏感：HTML / PDF 均可用。
    assert client.get(
        f"/api/handouts/{created['handout_id']}/export", params={"format": "HTML"}
    ).status_code == 200


# ---------------------------------------------------------------------------
# 上限与确定性
# ---------------------------------------------------------------------------


def test_limits_and_title_validation(api, monkeypatch):
    """来源 ≤10、个人讲义 ≤50（可 monkeypatch 调小）、标题 ≤120。"""
    client, _holder, env = api
    # 单类来源超过 10 个：先于归属校验直接 422。
    response = _create(client, note_ids=list(range(1, 12)))
    assert response.status_code == 422
    assert "笔记最多选择 10 个" in response.json()["detail"]
    response = _create(client, artifact_ids=[f"artifact-{i}" for i in range(11)])
    assert response.status_code == 422
    assert "附件最多选择 10 个" in response.json()["detail"]
    # 标题超长。
    response = _create(client, course_id=env["course_id"], title="长" * 121)
    assert response.status_code == 422
    assert "标题最多 120 个字符" in response.json()["detail"]
    # 个人讲义上限可用常量调小验证。
    monkeypatch.setattr(handout, "MAX_HANDOUTS_PER_USER", 2)
    assert _create(client, course_id=env["course_id"]).status_code == 201
    assert _create(client, course_id=env["course_id"]).status_code == 201
    response = _create(client, course_id=env["course_id"])
    assert response.status_code == 422
    assert "个人讲义最多保存 2 份" in response.json()["detail"]


def test_sections_are_deterministic_for_same_input(api):
    """同一组来源连续创建两次，sections 完全一致（规则汇编可复现）。"""
    client, _holder, env = api
    payload = _all_sources(env)
    first = _created(client, **payload)
    second = _created(client, **payload)
    assert first["sections"] == second["sections"]
    assert first["handout"]["handout_id"] != second["handout"]["handout_id"]
