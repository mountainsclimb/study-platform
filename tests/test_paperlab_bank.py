"""试卷工坊 · 可写题库（pl_questions）P0 验收：

建表幂等、CRUD、筛选/关键词检索（LIKE，非 FTS5）、版本 CAS 409、软删除、
核验状态（verified 后 AI 不覆盖由 test_paperlab_enrich 覆盖）、归属隔离与参数校验。
"""
from fastapi.testclient import TestClient
import pytest

from app.main import app
from app.paperlab.db import init_tables


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def create_question(client, **overrides):
    payload = {
        "type": "SUBJECTIVE",
        "subject": "数学",
        "grade": "高中一年级",
        "difficulty": 0.5,
        "knowledge_tags": ["函数"],
        "content": {"stem": "证明函数 $f(x)=x^2$ 在 $[0,+\\infty)$ 上单调递增。"},
        "status": "draft",
    }
    payload.update(overrides)
    response = client.post("/api/paperlab/questions", json=payload)
    assert response.status_code == 200, response.text
    return response.json()["question"]


def single_choice(**overrides):
    content = {
        "stem": "已知 $f(x)=x^2$，则 $f(2)$ 等于？",
        "options": [{"label": "A", "text": "2"}, {"label": "B", "text": "4"}],
        "answers": ["B"],
        "explanation": "代入得 4。",
    }
    content.update(overrides.pop("content", {}))
    payload = {
        "type": "SINGLE", "subject": "数学", "grade": "高中一年级",
        "knowledge_tags": ["函数"], "content": content,
    }
    payload.update(overrides)
    return payload


def question_list(client, **params):
    response = client.get("/api/paperlab/questions", params=params)
    assert response.status_code == 200, response.text
    return response.json()


# ---------------------------------------------------------------------------
# meta / 建表
# ---------------------------------------------------------------------------

def test_meta_reports_limits_and_dictionaries(client):
    response = client.get("/api/paperlab/meta")
    assert response.status_code == 200
    meta = response.json()
    assert meta["enabled"] is True
    assert "数学" in meta["subjects"] and "高中一年级" in meta["grades"]
    assert {"SINGLE", "MULTIPLE", "BLANK", "SUBJECTIVE", "MIXED"} == {
        item["value"] for item in meta["qtypes"]
    }
    assert meta["limits"]["upload_mb"] == 30
    assert meta["limits"]["import_pages_max"] == 20
    assert meta["limits"]["import_items_max"] >= 1
    assert meta["capabilities"]["vision"] is True
    assert meta["prompt_version"].startswith("paperlab-")


def test_init_tables_is_idempotent(client):
    init_tables()
    init_tables()
    question = create_question(client)
    assert question["id"] >= 1


# ---------------------------------------------------------------------------
# 新建 / 详情
# ---------------------------------------------------------------------------

def test_create_and_get_question(client):
    question = create_question(client)
    assert question["type"] == "SUBJECTIVE"
    assert question["status"] == "draft"
    assert question["verify_status"] == "pending"
    assert question["version"] == 1
    assert question["content"]["stem"].startswith("证明函数")
    assert question["knowledge_tags"] == ["函数"]

    detail = client.get(f"/api/paperlab/questions/{question['id']}")
    assert detail.status_code == 200
    assert detail.json()["question"]["id"] == question["id"]


def test_create_question_accepts_chinese_qtype_alias(client):
    payload = single_choice(type="单选题")
    response = client.post("/api/paperlab/questions", json=payload)
    assert response.status_code == 200, response.text
    assert response.json()["question"]["type"] == "SINGLE"


@pytest.mark.parametrize("overrides, keyword", [
    ({"type": "UNKNOWN"}, "题型"),
    ({"content": {"stem": ""}}, "题干"),
    ({"type": "SINGLE", "content": {"stem": "选择题"}}, "选项"),
    ({"type": "SINGLE", "content": {
        "stem": "选择题", "options": [{"label": "A", "text": "1"}], "answers": ["C"],
    }}, "答案"),
    ({"type": "MIXED", "content": {"stem": "混合题"}}, "子题"),
    ({"type": "SUBJECTIVE", "content": {
        "stem": "主观题", "parts": [{"index": 1, "answers": ["x"]}],
    }}, "子题"),
])
def test_create_question_rejects_invalid_payload(client, overrides, keyword):
    payload = {
        "type": "SUBJECTIVE", "subject": "数学", "grade": "高中一年级",
        "content": {"stem": "题目"},
    }
    payload.update(overrides)
    response = client.post("/api/paperlab/questions", json=payload)
    assert response.status_code == 422, response.text
    assert keyword in response.json()["detail"]


# ---------------------------------------------------------------------------
# 列表筛选 / 检索
# ---------------------------------------------------------------------------

def test_list_filters_keyword_and_pagination(client):
    create_question(client, content={"stem": "函数单调性证明"}, knowledge_tags=["函数"])
    create_question(client, content={"stem": "数列求和的通项公式"}, knowledge_tags=["数列"])
    response = client.post("/api/paperlab/questions", json=single_choice())
    assert response.status_code == 200
    create_question(client, type="BLANK", subject="英语",
                    content={"stem": "He ___ to school every day.", "answers": ["goes"]})

    assert question_list(client)["total"] == 4
    assert question_list(client, subject="数学")["total"] == 3
    assert question_list(client, subject="英语")["total"] == 1
    assert question_list(client, qtype="SINGLE")["total"] == 1
    assert question_list(client, qtype="单选题")["total"] == 1
    # 关键词命中题干或知识点（单选题目录了「函数」知识点）
    function_hits = question_list(client, keyword="函数")
    assert function_hits["total"] == 3
    # 关键词命中知识点
    assert question_list(client, keyword="数列")["total"] == 1
    # 分页
    first_page = question_list(client, page=1, page_size=2)
    assert first_page["total"] == 4 and len(first_page["items"]) == 2
    second_page = question_list(client, page=2, page_size=2)
    assert len(second_page["items"]) == 2
    assert not ({item["id"] for item in first_page["items"]} &
                {item["id"] for item in second_page["items"]})

    detailed = question_list(client, keyword="函数")
    assert all("content" in item and "knowledge_tags" in item for item in detailed["items"])
    assert all("stem_preview" in item for item in detailed["items"])


def test_keyword_like_wildcards_are_escaped(client):
    create_question(client, content={"stem": "正确率 100% 的题目"})
    create_question(client, content={"stem": "普通题目"})
    assert question_list(client, keyword="%")["total"] == 1
    assert question_list(client, keyword="100%")["total"] == 1
    assert question_list(client, keyword="_")["total"] == 0


def test_list_rejects_unknown_filters(client):
    assert client.get("/api/paperlab/questions", params={"qtype": "NOPE"}).status_code == 422
    assert client.get("/api/paperlab/questions", params={"status": "weird"}).status_code == 422


# ---------------------------------------------------------------------------
# PATCH（version CAS）
# ---------------------------------------------------------------------------

def test_patch_question_version_cas(client):
    question = create_question(client)
    url = f"/api/paperlab/questions/{question['id']}"

    conflict = client.patch(url, json={"version": 99, "status": "ready"})
    assert conflict.status_code == 409
    assert "版本冲突" in conflict.json()["detail"]

    updated = client.patch(url, json={
        "version": 1, "status": "ready", "difficulty": 0.8,
        "knowledge_tags": ["函数", "单调性"],
    })
    assert updated.status_code == 200, updated.text
    patched = updated.json()["question"]
    assert patched["version"] == 2
    assert patched["status"] == "ready"
    assert patched["difficulty"] == 0.8
    assert patched["knowledge_tags"] == ["函数", "单调性"]
    # 未提供的字段保持不变
    assert patched["content"]["stem"].startswith("证明函数")

    # 内容局部合并：只改解析，题干保留
    merged = client.patch(url, json={
        "version": 2, "content": {"explanation": "利用导数判断单调性。"},
    })
    assert merged.status_code == 200
    content = merged.json()["question"]["content"]
    assert content["explanation"] == "利用导数判断单调性。"
    assert content["stem"].startswith("证明函数")


def test_patch_question_rejects_invalid_content(client):
    question = create_question(client)
    response = client.patch(
        f"/api/paperlab/questions/{question['id']}",
        json={"version": 1, "content": {"stem": ""}},
    )
    assert response.status_code == 422
    assert "题干" in response.json()["detail"]


# ---------------------------------------------------------------------------
# 删除（软删除）
# ---------------------------------------------------------------------------

def test_delete_question_soft_deletes(client):
    question = create_question(client)
    url = f"/api/paperlab/questions/{question['id']}"
    assert client.delete(url).status_code == 200
    assert client.get(url).status_code == 404
    assert question_list(client)["total"] == 0
    assert client.delete(url).status_code == 404


# ---------------------------------------------------------------------------
# 核验状态
# ---------------------------------------------------------------------------

def test_verify_question_sets_verified_and_ready(client):
    response = client.post("/api/paperlab/questions", json=single_choice())
    question = response.json()["question"]
    verified = client.post(
        f"/api/paperlab/questions/{question['id']}/verify", json={"status": "verified"},
    )
    assert verified.status_code == 200, verified.text
    payload = verified.json()["question"]
    assert payload["verify_status"] == "verified"
    assert payload["status"] == "ready"
    assert payload["version"] == 2

    rejected = client.post(
        f"/api/paperlab/questions/{question['id']}/verify", json={"status": "rejected"},
    )
    assert rejected.json()["question"]["verify_status"] == "rejected"
    assert rejected.json()["question"]["status"] == "draft"


def test_verify_rejects_unknown_status_and_question_without_answer(client):
    question = create_question(client)
    bad = client.post(
        f"/api/paperlab/questions/{question['id']}/verify", json={"status": "maybe"},
    )
    assert bad.status_code == 422
    no_answer = client.post(
        f"/api/paperlab/questions/{question['id']}/verify", json={"status": "verified"},
    )
    assert no_answer.status_code == 422
    assert "答案" in no_answer.json()["detail"]


# ---------------------------------------------------------------------------
# 归属 / 登录
# ---------------------------------------------------------------------------

def test_question_owner_isolation(client, second_client):
    question = create_question(client)
    url = f"/api/paperlab/questions/{question['id']}"
    assert second_client.get(url).status_code == 404
    assert second_client.patch(url, json={"version": 1, "status": "ready"}).status_code == 404
    assert second_client.delete(url).status_code == 404
    assert second_client.post(
        f"{url}/verify", json={"status": "verified"}
    ).status_code == 404
    assert question_list(second_client)["total"] == 0


def test_question_routes_require_login(client):
    anonymous = TestClient(app)
    try:
        assert anonymous.get("/api/paperlab/questions").status_code == 401
        assert anonymous.post("/api/paperlab/questions", json={
            "subject": "数学", "content": {"stem": "题目"},
        }).status_code == 401
    finally:
        anonymous.close()
