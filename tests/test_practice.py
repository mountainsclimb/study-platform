"""Behavior checks against real read-only bank; app records use temp storage."""
from io import BytesIO
import json
from urllib.parse import quote

import pytest
from fastapi import HTTPException
from PIL import Image, ImageDraw


def bank_question(objective=True, grouped=False):
    from app.practice import _bank, load_question
    with _bank() as con:
        ids = [r[0] for r in con.execute(
            "SELECT qid FROM questions WHERE objective=?" + (" AND group_id IS NOT NULL" if grouped else "") + " ORDER BY year DESC LIMIT 250",
            (1 if objective else 0,))]
    for qid in ids:
        question = load_question(qid)
        # 快照题库会给主观题/待核验题附加「参考答案待核验」提示，这类信息性 flag 不算内容问题。
        blocking = [flag for flag in question["quality_flags"] if "参考答案待核验" not in flag]
        if question["practice_allowed"] and not blocking and question["has_answer"]:
            if not objective or question["type"] == "single_choice":
                return question
    raise AssertionError("No suitable source question")


def drawn_image(size=(240, 240)) -> bytes:
    """正常尺寸的手写作答图：空白门禁不再豁免小图，夹具必须 ≥100px 且有笔迹。"""
    image = Image.new("RGB", size, "white")
    draw = ImageDraw.Draw(image)
    draw.line((20, 20, size[0] - 20, size[1] - 20), fill="black", width=4)
    draw.rectangle((30, 30, size[0] - 30, size[1] - 30), outline="blue", width=3)
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def image_bytes():
    return drawn_image()


def test_catalog_filters_and_answer_gate(client, second_client):
    catalog = client.get("/api/catalog").json()
    assert catalog["source_total"] == 7659
    assert catalog["quarantined_count"] >= 17
    response = client.get("/api/questions", params={"subject": "数学", "page_size": 4})
    assert response.status_code == 200
    assert response.json()["total"] > 0
    for item in response.json()["items"]:
        assert item["subject"] == "数学"
        assert "answer" not in item and "answer_text" not in item and "solution" not in item
    question = bank_question()
    qid = question["qid"]
    detail = client.get("/api/questions/" + quote(qid, safe="")).json()
    assert "answer" not in detail and not detail["unlocked"]
    assert client.get("/api/reveal", params={"qid": qid}).status_code == 403
    assert client.post("/api/attempts", data={"qid": qid, "answer": "Z"}).status_code == 422
    assert client.post("/api/attempts", data={"qid": qid}).status_code == 422
    result = client.post("/api/attempts", data={"qid": qid, "answer": question["answer_text"]})
    assert result.status_code == 200, result.text
    assert result.json()["result"] == "correct"
    assert result.json()["question"]["answer"]["text"] == question["answer_text"]
    assert second_client.get("/api/reveal", params={"qid": qid}).status_code == 403
    # Even attempted search results remain free of answers.
    search = client.get("/api/questions", params={"q": question["stem_plain"][:15]}).json()
    assert all("answer_text" not in item for item in search["items"])


def test_mastery_tracks_latest_attempt_and_wrongbook_is_private(client, second_client):
    question = bank_question()
    qid = question["qid"]
    assert client.post("/api/mastery", json={"qid": qid, "level": "熟练掌握"}).status_code == 403
    client.post("/api/attempts", data={"qid": qid, "answer": question["answer_text"]})
    assert client.post("/api/mastery", json={"qid": qid, "level": "熟练掌握"}).status_code == 200
    wrong = next(o["label"] for o in question["options"] if o["label"] != question["answer_text"])
    response = client.post("/api/attempts", data={"qid": qid, "answer": wrong})
    assert response.json()["result"] == "incorrect"
    assert response.json()["question"]["mastery"] == "未掌握"
    assert client.post("/api/mastery", json={"qid": qid, "level": "比较掌握"}).status_code == 403
    assert client.get("/api/wrongbook").json()["total"] == 1
    assert second_client.get("/api/wrongbook").json()["total"] == 0
    assert second_client.get("/api/stats").json()["attempts"] == 0


def test_material_group_projection_never_returns_children_answers(client):
    question = bank_question(grouped=True)
    response = client.get("/api/questions/" + quote(question["qid"], safe=""))
    assert response.status_code == 200
    group = response.json()["group"]
    assert group and group["material_text"]
    assert "children" not in group and "children_json" not in group
    assert "answer" not in response.json()
    # Known material bodies containing answers are withheld, not just their fields.
    from app.practice import _bank
    with _bank() as con:
        row = con.execute("SELECT qid FROM questions WHERE doc_id='3597febaaede0d74' AND group_id='m_0001' LIMIT 1").fetchone()
    assert row
    assert client.get("/api/questions/" + quote(row[0], safe="")).status_code == 409
    assert client.get("/api/questions/" + quote("d2aa42179cd42df7:21", safe="")).status_code == 409


def test_subjective_images_owner_gate_and_ai_grade_latest_guard(client, second_client):
    from app.practice import set_ai_grade
    question = bank_question(False)
    qid = question["qid"]
    assert client.post("/api/attempts", data={"qid": qid, "answer": "随便输入"}).status_code == 422
    assert client.post("/api/attempts", data={"qid": qid}, files={"file": ("bad.png", b"not an image", "image/png")}).status_code == 415
    response = client.post("/api/attempts", data={"qid": qid}, files={"file": ("work.png", image_bytes(), "image/png")})
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["result"] == "pending"
    image_url = data["attempt"]["image_url"]
    assert client.get(image_url).status_code == 200
    assert second_client.get(image_url).status_code == 404
    assert client.post("/api/mastery", json={"qid": qid, "level": "熟练掌握"}).status_code == 403
    owner = client.get("/api/auth/me").json()["id"]
    attempt_id = data["attempt"]["id"]
    with pytest.raises(HTTPException) as exc:
        set_ai_grade(owner, attempt_id, "correct", {"verified": False})
    assert exc.value.status_code == 422
    set_ai_grade(owner, attempt_id, "correct", {"verified": True, "scores": [5, 5]})
    assert client.post("/api/mastery", json={"qid": qid, "level": "比较掌握"}).status_code == 200
    newer = client.post("/api/attempts", data={"qid": qid}, files={"file": ("next.png", image_bytes(), "image/png")})
    assert newer.json()["question"]["mastery"] == "初步掌握"
    with pytest.raises(HTTPException) as exc:
        set_ai_grade(owner, attempt_id, "correct", {"verified": True})
    assert exc.value.status_code == 409


def test_difficulty_annotation_is_admin_only_and_source_is_unmodified(client, second_client):
    question = bank_question()
    body = {"qid": question["qid"], "difficulty": "困难"}
    assert second_client.post("/api/question-metadata", json=body).status_code == 403
    assert client.post("/api/question-metadata", json=body).status_code == 200
    filtered = client.get("/api/questions", params={"difficulty": "困难"}).json()
    assert filtered["total"] == 1
    assert filtered["items"][0]["difficulty"] == "困难"
    from app.practice import _bank
    with _bank() as con:
        assert "difficulty" not in [row[1] for row in con.execute("PRAGMA table_info(questions)")]


def test_due_review_respects_filters_without_changing_search(client):
    from app.db import connection
    question = bank_question()
    qid = question["qid"]
    client.post("/api/wrongbook", json={"qid": qid})
    review = client.get("/api/reviews", params={"subject": question["subject"]}).json()
    assert review["total"] == 1 and review["items"][0]["qid"] == qid
    different = "物理" if question["subject"] != "物理" else "语文"
    assert client.get("/api/reviews", params={"subject": different}).json()["total"] == 0
    before = client.get("/api/questions", params={"subject": question["subject"]}).json()["total"]
    owner = client.get("/api/auth/me").json()["id"]
    with connection() as con:
        con.execute("UPDATE wrong_questions SET next_review_at='2099-01-01T00:00:00+00:00' WHERE user_id=?", (owner,))
    assert client.get("/api/reviews").json()["total"] == 0
    assert client.get("/api/questions", params={"subject": question["subject"]}).json()["total"] == before


def test_bank_images_are_bound_to_images_directory(client):
    question = bank_question()
    assert client.get("/api/bank-image", params={"subject": question["subject"], "file": "../题库.db"}).status_code == 404
    assert client.get("/api/bank-image", params={"subject": "../", "file": "题库.db"}).status_code == 404


def test_known_extraction_errors_cannot_auto_grade_or_expose_placeholder_answers(client):
    from app.practice import _bank, _has_reference, load_question
    broken = load_question("1ecd54df223c7ab7:4")
    # 快照题库已按原卷重切该题选项（修复「D 并入 C」），此时以「选项按原卷块重建」提示代替旧「选项合并」。
    assert any(("选项合并" in flag) or ("选项按原卷块重建" in flag)
               for flag in broken["quality_flags"])
    response = client.post("/api/attempts", data={"qid": broken["qid"], "answer": broken["answer_text"]})
    assert response.status_code == 200
    assert response.json()["result"] == "pending"
    assert client.post("/api/mastery", json={"qid": broken["qid"], "level": "熟练掌握"}).status_code == 403
    with _bank() as con:
        placeholders = [dict(row) for row in con.execute("SELECT qid,answer_text FROM questions")
                        if row["answer_text"] and not row["answer_text"].replace("_", "").replace("＿", "").replace("。", "").replace(" ", "").strip()]
    assert len(placeholders) == 16
    assert all(not _has_reference(row["answer_text"]) for row in placeholders)
    assert not load_question("5cc40f92b73592a0:21")["has_answer"]


def test_polluted_question_stems_are_withheld_from_all_practice_routes(client):
    from app.practice import load_question
    qids = ("cf04cad2296e2558:59", "03063fd6a07bea8b:20", "03063fd6a07bea8b:21", "03063fd6a07bea8b:23")
    for qid in qids:
        question = load_question(qid)
        assert not question["practice_allowed"]
        assert any("原题干混入" in flag for flag in question["quality_flags"])
        assert client.get("/api/questions/" + quote(qid, safe="")).status_code == 409
        assert client.post("/api/attempts", data={"qid": qid, "answer": "A"}).status_code == 409
        result = client.get("/api/questions", params={"q": question["stem_plain"][:25]}).json()
        assert qid not in [item["qid"] for item in result["items"]]
    catalog = client.get("/api/catalog").json()
    # 可练总数 = 总数 - 隔离数（基线 21 道；快照题库会随数据侧隔离/上下文增减，做一致性校验）
    assert catalog["available_total"] + catalog["quarantined_count"] == catalog["source_total"]
    assert catalog["quarantined_count"] >= 21


def test_ai_cannot_replace_authoritative_objective_grade(client):
    from app.practice import latest_attempt, set_ai_grade
    question = bank_question()
    wrong = next(option["label"] for option in question["options"] if option["label"] != question["answer_text"])
    owner = client.get("/api/auth/me").json()["id"]
    for answer, expected in ((wrong, "incorrect"), (question["answer_text"], "correct")):
        submitted = client.post("/api/attempts", data={"qid": question["qid"], "answer": answer}).json()
        assert submitted["result"] == expected
        for verdict in ("pending", "correct", "incorrect"):
            with pytest.raises(HTTPException) as exc:
                set_ai_grade(owner, submitted["attempt_id"], verdict, {"verified": True})
            assert exc.value.status_code == 409
        assert latest_attempt(owner, question["qid"])["result"] == expected


def test_single_choice_multiple_reference_answers_suspend_auto_grading(client):
    import re
    from app.practice import _bank, load_question
    with _bank() as con:
        conflicts = [dict(row) for row in con.execute("SELECT qid,answer_text FROM questions WHERE type='single_choice'")
                     if re.fullmatch(r"[A-H]{2,}", row["answer_text"] or "")]
    assert len(conflicts) == 50
    for row in conflicts:
        assert any("题型与答案冲突" in flag for flag in load_question(row["qid"])["quality_flags"])
    question = load_question("3766d5563c17df98:21")
    response = client.post("/api/attempts", data={"qid": question["qid"], "answer": "D"})
    assert response.status_code == 200
    assert response.json()["result"] == "pending"
    assert client.post("/api/mastery", json={"qid": question["qid"], "level": "熟练掌握"}).status_code == 403
    assert any("参考选项重复" in flag for flag in load_question("b2d6d293f0a93229:60")["quality_flags"])


def test_phone_image_orientation_survives_private_image_reencoding(client):
    question = bank_question(False)
    output = BytesIO()
    image = Image.new("RGB", (240, 120), "white")
    draw = ImageDraw.Draw(image)
    draw.line((20, 20, 200, 100), fill="black", width=5)
    exif = image.getexif()
    exif[274] = 6
    image.save(output, format="JPEG", exif=exif)
    response = client.post("/api/attempts", data={"qid": question["qid"]}, files={"file": ("phone.jpg", output.getvalue(), "image/jpeg")})
    assert response.status_code == 200
    encoded = client.get(response.json()["attempt"]["image_url"])
    with Image.open(BytesIO(encoded.content)) as saved:
        assert saved.size == (120, 240)
        assert saved.getexif().get(274, 1) == 1
