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


def _clear_bank_caches():
    """切换只读题库后清掉进程内缓存（列探测 / 素材清单 / 检索索引 / catalog）。"""
    from app import practice
    practice._sqlite_question_columns.cache_clear()
    practice._sqlite_group_columns.cache_clear()
    practice._source_groups.cache_clear()
    practice._verified_index_key = None
    practice._verified_fts_key = None
    practice.invalidate_catalog_cache()


def test_catalog_filters_and_answer_gate(client, second_client):
    from app.practice import _bank
    catalog = client.get("/api/catalog").json()
    # 题库口径不再固定为 r3 的 7,659 题：以当前只读题库的实际行数为准（r5 为 26,374）。
    with _bank() as con:
        expected_total = con.execute("SELECT COUNT(*) FROM questions").fetchone()[0]
    assert catalog["source_total"] == expected_total
    assert catalog["available_total"] + catalog["quarantined_count"] == catalog["source_total"]
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
    # 数据侧已隔离的题组：材料/共用块混入答案解析时详情必须 409。
    # 该门禁的稳定回归由 tests/test_bank_content.py 的合成 fixture 覆盖；这里对当前基线
    # 动态抽样（r5 没有此类题组，旧 r3 固定 qid 已退役，不能写死）。
    from app.practice import _bank, _group_answer_leak
    with _bank() as con:
        leaked = [
            dict(row)["group_id"] for row in con.execute("SELECT * FROM groups")
            if dict(row).get("group_id") and _group_answer_leak(dict(row))
        ]
        for group_id in leaked[:3]:
            member = con.execute(
                "SELECT qid FROM questions WHERE group_id = ? OR doc_id || ':' || group_id = ? LIMIT 1",
                (group_id, group_id),
            ).fetchone()
            if member:
                assert client.get(
                    "/api/questions/" + quote(member[0], safe="")
                ).status_code == 409


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
    """质量门禁按当前只读题库动态抽样：不依赖 r3 的固定 qid / 固定条数。"""
    import re
    from app.practice import _bank, _has_reference, load_question

    with _bank() as con:
        placeholders = [
            dict(row) for row in con.execute("SELECT qid, answer_text FROM questions")
            if row["answer_text"] and not _has_reference(row["answer_text"])
        ]
        conflicts = [
            dict(row) for row in con.execute(
                "SELECT qid, answer_text FROM questions WHERE type='single_choice'")
            if re.fullmatch(r"[A-H]{2,}", row["answer_text"] or "")
        ]
    # 占位参考答案（只有下划线/空格等）永远不算有答案，也不能作为自动判分依据。
    assert all(not _has_reference(row["answer_text"]) for row in placeholders)
    broken = next(
        (question for question in (load_question(row["qid"]) for row in conflicts)
         if question["practice_allowed"] and not question["quarantined"]),
        None,
    )
    assert broken is not None, "当前题库应有可作答的「题型与答案冲突」题"
    assert any("题型与答案冲突" in flag for flag in broken["quality_flags"])
    option = broken["options"][0]["label"]
    response = client.post("/api/attempts", data={"qid": broken["qid"], "answer": option})
    assert response.status_code == 200
    assert response.json()["result"] == "pending"
    assert client.post("/api/mastery", json={"qid": broken["qid"], "level": "熟练掌握"}).status_code == 403


def test_polluted_question_stems_are_withheld_from_all_practice_routes(client, tmp_path, monkeypatch):
    """公开字段混入答案解析的题在所有练习入口都要被拦截。

    r5 基线没有这类题（旧 r3 固定 qid 已退役），用与 r5 同结构的合成题库验证门禁。
    """
    from app import db
    from app.practice import load_question
    from bank_fixture import make_bank, question_row

    rows = [
        question_row("docPoll:1", "题干正文，混入【答案】A 的解析", doc_id="docPoll"),
        question_row("docPoll:2", "干净题干：函数的定义域", doc_id="docPoll"),
    ]
    bank = make_bank(tmp_path / "polluted-bank", rows)
    monkeypatch.setattr(db, "BANK_DIR", bank)
    _clear_bank_caches()
    try:
        qid = "docPoll:1"
        question = load_question(qid)
        assert not question["practice_allowed"]
        assert any("混入答案解析" in flag for flag in question["quality_flags"])
        assert client.get("/api/questions/" + quote(qid, safe="")).status_code == 409
        assert client.post("/api/attempts", data={"qid": qid, "answer": "A"}).status_code == 409
        result = client.get("/api/questions", params={"q": "混入"}).json()
        assert qid not in [item["qid"] for item in result["items"]]
        clean = client.get("/api/questions", params={"q": "定义域"}).json()
        assert "docPoll:2" in [item["qid"] for item in clean["items"]]
        catalog = client.get("/api/catalog").json()
        assert catalog["available_total"] + catalog["quarantined_count"] == catalog["source_total"]
    finally:
        _clear_bank_caches()


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
    # r3 口径 50 道、r5 为 52 道：只要求存在且全部被质量门禁拦截，不写死数量。
    assert conflicts
    for row in conflicts:
        assert any("题型与答案冲突" in flag for flag in load_question(row["qid"])["quality_flags"])
    question = next(
        (candidate for candidate in (load_question(row["qid"]) for row in conflicts)
         if candidate["practice_allowed"] and not candidate["quarantined"] and candidate["options"]),
        None,
    )
    assert question is not None, "当前题库应有可作答的「题型与答案冲突」单选题"
    option = question["options"][0]["label"]
    response = client.post("/api/attempts", data={"qid": question["qid"], "answer": option})
    assert response.status_code == 200
    assert response.json()["result"] == "pending"
    assert client.post("/api/mastery", json={"qid": question["qid"], "level": "熟练掌握"}).status_code == 403
    # 参考答案重复字母（如 'ABABH'）同样必须挂起自动判分。
    with _bank() as con:
        duplicated = [row[0] for row in con.execute("SELECT qid, answer_text FROM questions")
                      if re.fullmatch(r"[A-H]+", row[1] or "") and len(set(row[1])) != len(row[1])]
    assert duplicated
    assert any("参考选项重复" in flag for qid in duplicated[:10] for flag in load_question(qid)["quality_flags"])


def test_admin_isolation_keeps_search_sql_valid(client):
    """管理员临时隔离题目后，题目检索与相似题 SQL 仍可执行（回归 "qqid" 拼接缺陷）。"""
    from app.practice import _bank, search_similar_questions

    with _bank() as con:
        row = con.execute(
            "SELECT qid FROM questions WHERE practice_status='practice' LIMIT 1"
        ).fetchone()
    qid = row[0]
    isolated = client.post(
        f"/api/admin/questions/{quote(qid, safe='')}/isolation",
        json={"active": True, "reason": "回归：临时隔离不破坏检索"},
    )
    assert isolated.status_code == 200, isolated.text
    try:
        listing = client.get("/api/questions", params={"q": "题", "page_size": 5})
        assert listing.status_code == 200, listing.text
        assert qid not in [item["qid"] for item in listing.json()["items"]]
        similar = search_similar_questions("下列各句中，没有语病的一项是")
        assert isinstance(similar, list)
    finally:
        client.post(
            f"/api/admin/questions/{quote(qid, safe='')}/isolation",
            json={"active": False},
        )


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


def test_fts_lock_is_reentrant_for_postgres_mirror_path():
    """PG 镜像刷新路径会在持锁状态下再次获取 _fts_lock（ensure_leak_flags →
    connection(attach_bank=True) → _ensure_postgres_bank → _install_bank_leak_flags），
    必须是可重入锁，否则 PG 首次启动自死锁（部署联验 2026-11-25 实测发现）。"""
    import threading

    from app import practice

    assert isinstance(practice._fts_lock, type(threading.RLock()))
    # 同线程嵌套获取不阻塞；跨线程互斥仍成立（子线程获取应超时失败）。
    with practice._fts_lock:
        got = []
        other = threading.Thread(
            target=lambda: got.append(practice._fts_lock.acquire(timeout=0.2)))
        other.start()
        other.join()
        assert got == [False]
