"""试卷工坊 P1：试卷 CRUD / 题目快照 / 版本 CAS / 发布只读 / Word/HTML 导出。

全部基于应用库临时数据 + 只读高考题库真实 qid；不调用任何模型、不访问外网。
"""
import io
import json

from fastapi.testclient import TestClient
import pytest

from app import db, practice
from app.main import app


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def user_id(client) -> int:
    return client.get("/api/auth/me").json()["id"]


def create_question(client, **overrides) -> dict:
    payload = {
        "type": "SINGLE",
        "subject": "数学",
        "grade": "高中一年级",
        "difficulty": 0.2,
        "knowledge_tags": ["函数"],
        "content": {
            "stem": "已知 $f(x)=x^2$，则 $f(2)$ 等于？",
            "options": [{"label": "A", "text": "2"}, {"label": "B", "text": "4"}],
            "answers": ["B"],
            "explanation": "代入 x=2 得 4。",
        },
        "status": "ready",
    }
    payload.update(overrides)
    response = client.post("/api/paperlab/questions", json=payload)
    assert response.status_code == 200, response.text
    return response.json()["question"]


def bank_qid(subject: str = "数学") -> str:
    with practice._bank() as con:
        rows = con.execute(
            "SELECT qid FROM questions WHERE subject=? AND type='single_choice' "
            "ORDER BY qid LIMIT 40", (subject,),
        ).fetchall()
    for row in rows:
        question = practice.load_question(row[0])
        if question.get("practice_allowed") and question.get("has_answer"):
            return row[0]
    raise AssertionError("no suitable bank question")


def docx_text(content: bytes) -> str:
    from docx import Document
    document = Document(io.BytesIO(content))
    parts = [paragraph.text for paragraph in document.paragraphs]
    for table in document.tables:
        for row in table.rows:
            for cell in row.cells:
                parts.append(cell.text)
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# 1. 手工建卷 + 快照
# ---------------------------------------------------------------------------

def test_create_paper_freezes_app_and_bank_snapshots(client):
    first = create_question(client)
    second = create_question(
        client, type="SUBJECTIVE", difficulty=0.5, knowledge_tags=["函数"],
        content={"stem": "证明 $f(x)=x^2$ 在 $[0,+\\infty)$ 上单调递增。", "answers": ["略"]},
    )
    qid = bank_qid()
    response = client.post("/api/paperlab/papers", json={
        "title": "函数与导数练习",
        "kind": "manual",
        "subject": "数学",
        "grade": "高中一年级",
        "instructions": "先独立完成，再核对答案。",
        "layout": {"paper": "A4", "columns": 2, "answer_space_mm": 20},
        "items": [
            {"ref": f"app:{first['id']}", "score": 6, "section": "选择题"},
            {"ref": f"app:{second['id']}", "score": 12, "section": "解答题"},
            {"ref": f"bank:{qid}", "score": 5, "section": "拓展题"},
        ],
    })
    assert response.status_code == 200, response.text
    body = response.json()
    paper = body["paper"]
    assert paper["title"] == "函数与导数练习"
    assert paper["kind"] == "manual"
    assert paper["status"] == "draft"
    assert paper["item_count"] == 3
    assert paper["total_score"] == 23
    items = body["items"]
    assert [item["position"] for item in items] == [0, 1, 2]
    app_snapshot = items[0]["snapshot"]
    assert app_snapshot["ref"] == f"app:{first['id']}"
    assert app_snapshot["source"] == "app"
    assert "x^2" in app_snapshot["content"]["stem"]
    assert app_snapshot["content"]["answers"] == ["B"]
    assert items[0]["score"] == 6 and items[0]["section"] == "选择题"
    bank_snapshot = items[2]["snapshot"]
    assert bank_snapshot["source"] == "bank"
    assert bank_snapshot["content"]["answers"], "只读题库快照必须包含答案（仅服务端/本人可见）"
    assert items[2]["has_answer"] is True

    detail = client.get(f"/api/paperlab/papers/{paper['id']}").json()
    assert len(detail["items"]) == 3
    listing = client.get("/api/paperlab/papers").json()
    assert listing["total"] == 1
    assert listing["items"][0]["item_count"] == 3
    assert listing["items"][0]["total_score"] == 23
    filtered = client.get("/api/paperlab/papers?subject=数学&kind=manual&status=draft").json()
    assert filtered["total"] == 1
    assert client.get("/api/paperlab/papers?subject=物理").json()["total"] == 0


def test_create_paper_validates_refs_duplicates_and_auth(client, second_client):
    question = create_question(client)
    missing_app = client.post("/api/paperlab/papers", json={
        "title": "坏卷", "subject": "数学",
        "items": [{"ref": "app:999999"}],
    })
    assert missing_app.status_code == 404
    missing_bank = client.post("/api/paperlab/papers", json={
        "title": "坏卷", "subject": "数学",
        "items": [{"ref": "bank:no-such-question"}],
    })
    assert missing_bank.status_code == 404
    bad_format = client.post("/api/paperlab/papers", json={
        "title": "坏卷", "subject": "数学", "items": [{"ref": "question:1"}],
    })
    assert bad_format.status_code == 422
    duplicate = client.post("/api/paperlab/papers", json={
        "title": "坏卷", "subject": "数学",
        "items": [{"ref": f"app:{question['id']}"}, {"ref": f"app:{question['id']}"}],
    })
    assert duplicate.status_code == 422
    assert "重复" in duplicate.json()["detail"]
    # 他人的题目不能加入自己的卷
    other = create_question(second_client)
    foreign = client.post("/api/paperlab/papers", json={
        "title": "坏卷", "subject": "数学", "items": [{"ref": f"app:{other['id']}"}],
    })
    assert foreign.status_code == 404
    # 未登录
    anonymous = TestClient(app)
    assert anonymous.get("/api/paperlab/papers").status_code == 401
    assert anonymous.post("/api/paperlab/papers", json={"title": "x", "subject": "数学"}).status_code == 401


def test_patch_reorder_publish_and_readonly(client):
    question = create_question(client)
    created = client.post("/api/paperlab/papers", json={
        "title": "版本测试卷", "subject": "数学",
        "items": [{"ref": f"app:{question['id']}", "score": 5}],
    }).json()
    paper_id = created["paper"]["id"]
    version = created["paper"]["version"]

    stale = client.patch(f"/api/paperlab/papers/{paper_id}", json={"version": version + 1, "title": "x"})
    assert stale.status_code == 409
    patched = client.patch(f"/api/paperlab/papers/{paper_id}", json={"version": version, "title": "版本测试卷（修订）"})
    assert patched.status_code == 200
    assert patched.json()["paper"]["title"] == "版本测试卷（修订）"
    version = patched.json()["paper"]["version"]

    second_question = create_question(client)
    with_items = client.patch(f"/api/paperlab/papers/{paper_id}", json={
        "version": version,
        "items": [
            {"ref": f"app:{second_question['id']}", "score": 8},
            {"ref": f"app:{question['id']}", "score": 5},
        ],
    })
    assert with_items.status_code == 200
    version = with_items.json()["paper"]["version"]
    item_ids = [item["id"] for item in with_items.json()["items"]]
    assert len(item_ids) == 2

    partial = client.post(f"/api/paperlab/papers/{paper_id}/items/reorder",
                          json={"version": version, "item_ids": item_ids[:1]})
    assert partial.status_code == 422
    reordered = client.post(f"/api/paperlab/papers/{paper_id}/items/reorder",
                            json={"version": version, "item_ids": list(reversed(item_ids))})
    assert reordered.status_code == 200
    assert [item["id"] for item in reordered.json()["items"]] == list(reversed(item_ids))
    version = reordered.json()["paper"]["version"]

    published = client.post(f"/api/paperlab/papers/{paper_id}/publish", json={"published": True})
    assert published.status_code == 200
    assert published.json()["paper"]["status"] == "published"
    assert published.json()["paper"]["published_at"]
    assert client.patch(f"/api/paperlab/papers/{paper_id}",
                        json={"version": version, "title": "x"}).status_code == 409
    assert client.post(f"/api/paperlab/papers/{paper_id}/items/reorder",
                       json={"version": version, "item_ids": list(reversed(item_ids))}).status_code == 409

    unpublished = client.post(f"/api/paperlab/papers/{paper_id}/publish", json={"published": False})
    assert unpublished.status_code == 200
    assert unpublished.json()["paper"]["status"] == "ready"
    version = unpublished.json()["paper"]["version"]
    assert client.patch(f"/api/paperlab/papers/{paper_id}",
                        json={"version": version, "title": "恢复可编辑"}).status_code == 200

    deleted = client.delete(f"/api/paperlab/papers/{paper_id}")
    assert deleted.status_code == 200
    assert client.get(f"/api/paperlab/papers/{paper_id}").status_code == 404


def test_publish_without_items_is_rejected(client):
    created = client.post("/api/paperlab/papers", json={"title": "空卷", "subject": "数学"}).json()
    response = client.post(f"/api/paperlab/papers/{created['paper']['id']}/publish",
                           json={"published": True})
    assert response.status_code == 422
    assert "题目" in response.json()["detail"]


def test_paper_ownership_isolation(client, second_client):
    question = create_question(client)
    created = client.post("/api/paperlab/papers", json={
        "title": "私有卷", "subject": "数学",
        "items": [{"ref": f"app:{question['id']}", "score": 5}],
    }).json()
    paper_id = created["paper"]["id"]
    assert second_client.get(f"/api/paperlab/papers/{paper_id}").status_code == 404
    assert second_client.patch(f"/api/paperlab/papers/{paper_id}",
                               json={"version": 1, "title": "x"}).status_code == 404
    assert second_client.delete(f"/api/paperlab/papers/{paper_id}").status_code == 404
    assert second_client.get(f"/api/paperlab/papers/{paper_id}/export?format=docx").status_code == 404
    assert second_client.get("/api/paperlab/papers").json()["total"] == 0
    # 他人题目不能复制进自己的卷
    assert client.get(f"/api/paperlab/papers/{paper_id}").status_code == 200


def test_snapshot_is_immune_to_question_edits(client):
    question = create_question(client, content={
        "stem": "原始题干：$x^2$ 的导数是什么？",
        "options": [{"label": "A", "text": "2x"}, {"label": "B", "text": "x"}],
        "answers": ["A"],
        "explanation": "导数为 2x。",
    })
    created = client.post("/api/paperlab/papers", json={
        "title": "快照冻结卷", "subject": "数学",
        "items": [{"ref": f"app:{question['id']}", "score": 5}],
    }).json()
    paper_id = created["paper"]["id"]
    # 修改题库题目（version CAS）
    updated = client.patch(f"/api/paperlab/questions/{question['id']}", json={
        "version": question["version"],
        "content": dict(question["content"], stem="修改后的题干（不应影响历史卷）"),
    })
    assert updated.status_code == 200, updated.text
    snapshot = client.get(f"/api/paperlab/papers/{paper_id}").json()["items"][0]["snapshot"]
    assert "原始题干" in snapshot["content"]["stem"]
    assert "修改后的题干" not in snapshot["content"]["stem"]
    html = client.get(f"/api/paperlab/papers/{paper_id}/export?format=html").text
    assert "原始题干" in html and "修改后的题干" not in html


# ---------------------------------------------------------------------------
# 2. 导出
# ---------------------------------------------------------------------------

def test_export_docx_and_html_answers_toggle(client):
    single = create_question(client)
    subjective = create_question(client, type="SUBJECTIVE", content={
        "stem": "证明函数 $f(x)=x^2$ 在 $[0,+\\infty)$ 上单调递增。",
        "answers": ["任取 0≤x1<x2，比较 f(x2)-f(x1) 即可。"],
        "explanation": "利用差商与因式分解可证。",
        "rubric": "构造差商 3 分，结论 2 分。",
    })
    created = client.post("/api/paperlab/papers", json={
        "title": "函数专题练习", "subject": "数学", "grade": "高中一年级",
        "instructions": "满分 20 分，时间 40 分钟。",
        "layout": {"paper": "A4", "columns": 2, "answer_space_mm": 30},
        "items": [
            {"ref": f"app:{single['id']}", "score": 5, "section": "选择题"},
            {"ref": f"app:{subjective['id']}", "score": 15, "section": "解答题"},
        ],
    }).json()
    paper_id = created["paper"]["id"]

    student = client.get(f"/api/paperlab/papers/{paper_id}/export?format=docx&answers=0")
    assert student.status_code == 200
    assert student.headers["content-type"].startswith(
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
    assert student.content[:2] == b"PK"
    student_text = docx_text(student.content)
    assert "函数专题练习" in student_text
    assert "已知 $f(x)=x^2$" not in student_text  # LaTeX 已转为可读文本
    assert "f(x)=x²" in student_text  # 上标 2 转为 ²
    assert "A. 2" in student_text and "B. 4" in student_text
    assert "参考答案与解析" not in student_text
    assert "任取 0≤x1<x2" not in student_text

    full = client.get(f"/api/paperlab/papers/{paper_id}/export?format=docx&answers=1")
    assert full.status_code == 200
    full_text = docx_text(full.content)
    assert "参考答案与解析" in full_text
    assert "任取 0≤x1<x2" in full_text
    assert "利用差商与因式分解可证" in full_text
    assert "构造差商 3 分" in full_text

    html_student = client.get(f"/api/paperlab/papers/{paper_id}/export?format=html&answers=0")
    assert html_student.status_code == 200
    assert html_student.headers["content-type"].startswith("text/html")
    assert html_student.headers.get("x-frame-options") == "SAMEORIGIN"
    body = html_student.text
    assert "@page" in body and "break-inside: avoid" in body
    assert "函数专题练习" in body
    assert 'class="opt-label">A.</span>' in body and 'class="opt-label">B.</span>' in body
    assert "参考答案与解析" not in body
    assert "任取 0≤x1&lt;x2" not in body

    html_full = client.get(f"/api/paperlab/papers/{paper_id}/export?format=html").text
    assert "参考答案与解析" in html_full
    assert "任取 0≤x1&lt;x2" in html_full

    # pdf：服务端浏览器渲染（与浏览器打印一致）；不可用时降级 503 并提示改 html。
    pdf = client.get(f"/api/paperlab/papers/{paper_id}/export?format=pdf")
    assert pdf.status_code in (200, 503)
    if pdf.status_code == 200:
        assert pdf.headers["content-type"].startswith("application/pdf")
        assert pdf.content.startswith(b"%PDF")
    # 未知格式仍拒绝
    assert client.get(f"/api/paperlab/papers/{paper_id}/export?format=xyz").status_code == 422


def test_latex_to_text_helper():
    from app.paperlab.exporters import latex_to_text
    assert latex_to_text("$\\frac{1}{2}+\\sqrt{3}$") == "(1)/(2)+√(3)"
    assert latex_to_text("$x^{2}+y^{2}=1$") == "x²+y²=1"
    assert latex_to_text("$\\angle A=90^{\\circ}$") == "∠ A=90°"
    assert latex_to_text("材料 [图1] 与 [公式2]") == "材料 〔图〕 与 〔公式〕"


def test_export_requires_known_paper(client):
    assert client.get("/api/paperlab/papers/99999/export?format=docx").status_code == 404
