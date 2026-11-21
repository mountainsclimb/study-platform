"""试卷工坊 P1 · 切题与答题区标注验收（页级 edit.json / CAS / 重切 / 发布校验）。

不联网、不调用模型：页图由 Pillow 直接生成后走真实上传接口；重切用本地 Pillow 裁图。
覆盖：annotation 读写与 version CAS 409、box 校验 422、页/导入归属、重切生成草稿与裁图、
重复重切幂等、已入库草稿 stale 不覆盖、keep_items=false 清理、答题区更新、页列表、
发布前校验、任务化分支、未登录与上限。
"""
import asyncio
import json
from io import BytesIO
from types import SimpleNamespace

from fastapi.testclient import TestClient
from PIL import Image, ImageDraw
import pytest

from app import ai, db, storage
from app.ai import jobs as ai_jobs
from app.main import app
from app.paperlab import annotate
from app.paperlab.db import ASSET_FOLDER

VISION_MODEL = "vision-model"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def page_png(width=600, height=800):
    image = Image.new("RGB", (width, height), (255, 255, 255))
    draw = ImageDraw.Draw(image)
    draw.rectangle([30, 40, 300, 240], fill=(220, 40, 40))
    draw.rectangle([320, 60, 560, 260], fill=(40, 80, 220))
    draw.rectangle([60, 400, 540, 560], fill=(250, 200, 40))
    buffer = BytesIO()
    image.save(buffer, "PNG")
    return buffer.getvalue()


def upload_pages(client, count=1, *, title="切题测试卷", subject="数学", grade="高中一年级"):
    files = [("files", (f"第{index + 1}页.png", page_png(), "image/png")) for index in range(count)]
    response = client.post("/api/paperlab/imports", files=files,
                           data={"title": title, "subject": subject, "grade": grade})
    assert response.status_code == 200, response.text
    return response.json()["import"]


def user_id(client):
    return client.get("/api/auth/me").json()["id"]


def save_provider(client, name="切题任务连接"):
    response = client.post("/api/providers", json={
        "name": name, "kind": "openai", "base_url": "https://api.openai.com/v1",
        "api_key": "sk-paperlab-annotate-test", "models": [VISION_MODEL],
    })
    assert response.status_code == 200, response.text
    return response.json()


def get_annotation(client, import_id, page_no=1):
    response = client.get(f"/api/paperlab/imports/{import_id}/pages/{page_no}/annotation")
    assert response.status_code == 200, response.text
    return response.json()


def save_annotation(client, import_id, boxes, *, version=0, page_no=1):
    return client.put(
        f"/api/paperlab/imports/{import_id}/pages/{page_no}/annotation",
        json={"version": version, "boxes": boxes},
    )


def box(kind, x, y, w, h, **extra):
    payload = {"kind": kind, "x": x, "y": y, "w": w, "h": h}
    payload.update(extra)
    return payload


def question_box(x, y, w, h, **extra):
    return box("question", x, y, w, h, **extra)


def answer_box(x, y, w, h, **extra):
    return box("answer", x, y, w, h, **extra)


def resegment(client, import_id, **payload):
    return client.post(f"/api/paperlab/imports/{import_id}/resegment", json=payload)


def import_detail(client, import_id):
    response = client.get(f"/api/paperlab/imports/{import_id}")
    assert response.status_code == 200, response.text
    return response.json()


def insert_item(import_id, user_id_value, page_no, *, item_no=1, bbox=None,
                draft=None, status="pending", source="ai", question_id=None,
                warnings=None):
    now = db.utcnow()
    draft = dict(draft or {})
    if bbox is not None and "bbox" not in draft:
        draft["bbox"] = bbox
    draft.setdefault("stem", "已有识别题干")
    draft.setdefault("type", "SUBJECTIVE")
    draft.setdefault("page_no", page_no)
    with db.connection() as con:
        return con.execute(
            "INSERT INTO pl_import_items(user_id,import_id,page_no,item_no,bbox_json,draft_json,"
            "status,needs_review,review_reason,skip_reason,confidence,source,warnings_json,"
            "question_id,created_at,updated_at) VALUES (?,?,?,?,?,?,?,0,'','',NULL,?,?,?,?,?)",
            (user_id_value, import_id, page_no, item_no, json.dumps(bbox or {}, ensure_ascii=False),
             json.dumps(draft, ensure_ascii=False), status, source,
             json.dumps(warnings or [], ensure_ascii=False), question_id, now, now),
        ).lastrowid


def crop_from_asset(asset_name):
    raw = storage.read_file(ASSET_FOLDER, asset_name)
    with Image.open(BytesIO(raw)) as image:
        return image.size, image.mode


def accept_all(client, import_id):
    detail = import_detail(client, import_id)
    ids = []
    for item in detail["items"]:
        if item["status"] == "skipped":
            continue
        response = client.patch(
            f"/api/paperlab/imports/{import_id}/items/{item['id']}", json={"status": "accepted"},
        )
        assert response.status_code == 200, response.text
        ids.append(item["id"])
    return ids


# ---------------------------------------------------------------------------
# 1. annotation 读写 / CAS / 校验
# ---------------------------------------------------------------------------

def test_annotation_read_write_and_version_cas(client):
    imported = upload_pages(client)
    import_id = imported["id"]

    empty = get_annotation(client, import_id)
    assert empty["edit"]["version"] == 0
    assert empty["edit"]["boxes"] == []
    assert empty["edit"]["edited"] is False
    assert empty["page"]["page_no"] == 1
    assert empty["page"]["width"] == 600 and empty["page"]["height"] == 800
    assert empty["page"]["naturalWidth"] == 600
    assert empty["page"]["naturalHeight"] == 800
    assert empty["page"]["image_url"] == f"/api/paperlab/imports/{import_id}/pages/1/image"

    created = save_annotation(client, import_id, [
        question_box(0.05, 0.05, 0.45, 0.25, id="q1", label="第1题"),
        answer_box(0.1, 0.6, 0.8, 0.2, id="a1"),
    ])
    assert created.status_code == 200, created.text
    body = created.json()
    assert body["edit"]["version"] == 1
    assert [item["id"] for item in body["edit"]["boxes"]] == ["q1", "a1"]
    assert body["edit"]["boxes"][0]["label"] == "第1题"
    assert body["warnings"] == []

    loaded = get_annotation(client, import_id)
    assert loaded["edit"]["version"] == 1 and loaded["edit"]["edited"] is True
    assert len(loaded["edit"]["boxes"]) == 2

    updated = save_annotation(client, import_id, [question_box(0.2, 0.2, 0.5, 0.3, id="q1")], version=1)
    assert updated.status_code == 200, updated.text
    assert updated.json()["edit"]["version"] == 2

    conflict = save_annotation(client, import_id, [], version=1)
    assert conflict.status_code == 409, conflict.text
    detail = conflict.json()["detail"]
    assert detail["version"] == 2
    assert detail["updated_at"]
    assert "已被他人修改" in detail["message"]
    assert conflict.headers["x-edit-version"] == "2"
    assert conflict.headers["x-edit-updated-at"] == detail["updated_at"]

    empty_version = save_annotation(client, import_id, [], version=0)
    assert empty_version.status_code == 409
    assert empty_version.json()["detail"]["version"] == 2


@pytest.mark.parametrize("boxes,snippet", [
    ([{"kind": "note", "x": 0.1, "y": 0.1, "w": 0.2, "h": 0.2}], "kind"),
    ([{"kind": "question", "x": -0.1, "y": 0.1, "w": 0.2, "h": 0.2}], "0≤x,y,w,h≤1"),
    ([{"kind": "question", "x": 0.1, "y": 0.1, "w": 0, "h": 0.2}], "0≤x,y,w,h≤1"),
    ([{"kind": "question", "x": 0.9, "y": 0.1, "w": 0.2, "h": 0.2}], "超出页面范围"),
    ([{"kind": "question", "x": 0.1, "y": 0.1, "w": 1.2, "h": 0.2}], "0≤x,y,w,h≤1"),
    ([{"kind": "question", "x": 0.1, "y": 0.1, "w": 0.2}], "缺少合法坐标"),
    ([{"kind": "answer", "x": 0.1, "y": 0.1, "w": 0.2, "h": 0.2, "score": -1}], "分值"),
])
def test_annotation_box_validation_422(client, boxes, snippet):
    imported = upload_pages(client)
    response = save_annotation(client, imported["id"], boxes)
    assert response.status_code == 422, response.text
    assert snippet in response.json()["detail"]


def test_annotation_limits_and_duplicate_ids(client):
    imported = upload_pages(client)
    import_id = imported["id"]
    two_boxes = save_annotation(client, import_id, [
        question_box(0.05, 0.05, 0.4, 0.2, id="q1"),
        question_box(0.55, 0.05, 0.4, 0.2, id="q1"),
    ])
    assert two_boxes.status_code == 200, two_boxes.text
    ids = [item["id"] for item in two_boxes.json()["edit"]["boxes"]]
    assert ids[0] == "q1" and ids[1].startswith("q1") and ids[1] != "q1"

    too_many = save_annotation(client, import_id, [
        question_box(0.01, 0.01, 0.02, 0.02, id="b%d" % index) for index in range(201)
    ])
    assert too_many.status_code == 422
    assert "200" in too_many.json()["detail"]

    missing_link = save_annotation(client, import_id, [
        question_box(0.1, 0.1, 0.3, 0.2, item_id=999999),
    ])
    assert missing_link.status_code == 422
    assert "草稿不存在" in missing_link.json()["detail"]

    invalid_pages = resegment(client, import_id, pages=[0])
    assert invalid_pages.status_code == 422
    assert "正整数" in invalid_pages.json()["detail"]


def test_annotation_overlap_is_tolerated_with_warning(client):
    imported = upload_pages(client)
    response = save_annotation(client, imported["id"], [
        question_box(0.1, 0.1, 0.5, 0.3, id="q1"),
        question_box(0.15, 0.12, 0.5, 0.3, id="q2"),
    ])
    assert response.status_code == 200, response.text
    assert any("重叠" in warning for warning in response.json()["warnings"])


def test_annotation_rejects_text_page(client):
    from docx import Document

    document = Document()
    document.add_paragraph("1. 纯文本页题目")
    buffer = BytesIO()
    document.save(buffer)
    response = client.post("/api/paperlab/imports", files=[
        ("files", ("讲义.docx", buffer.getvalue(),
                   "application/vnd.openxmlformats-officedocument.wordprocessingml.document")),
    ])
    assert response.status_code == 200, response.text
    import_id = response.json()["import"]["id"]
    page = get_annotation(client, import_id)["page"]
    assert page["image_url"] is None and page["has_text"] is True
    put = save_annotation(client, import_id, [question_box(0.1, 0.1, 0.5, 0.2)])
    assert put.status_code == 422
    assert "纯文本页" in put.json()["detail"]


# ---------------------------------------------------------------------------
# 2. 归属 / 未登录 / 页不存在
# ---------------------------------------------------------------------------

def test_annotation_ownership_and_login(client, second_client):
    imported = upload_pages(client)
    import_id = imported["id"]
    save_annotation(client, import_id, [question_box(0.05, 0.05, 0.4, 0.2)])
    assert second_client.get(
        f"/api/paperlab/imports/{import_id}/pages/1/annotation").status_code == 404
    assert second_client.put(
        f"/api/paperlab/imports/{import_id}/pages/1/annotation",
        json={"version": 0, "boxes": []}).status_code == 404
    assert second_client.get(f"/api/paperlab/imports/{import_id}/pages").status_code == 404
    assert resegment(second_client, import_id, pages=[1]).status_code == 404
    assert second_client.post(
        f"/api/paperlab/imports/{import_id}/publish").status_code == 404

    from app.main import app as fastapi_app

    anon = TestClient(fastapi_app)
    assert anon.get(f"/api/paperlab/imports/{import_id}/pages/1/annotation").status_code == 401
    assert anon.put(f"/api/paperlab/imports/{import_id}/pages/1/annotation",
                    json={"version": 0, "boxes": []}).status_code == 401
    assert anon.post(f"/api/paperlab/imports/{import_id}/resegment", json={}).status_code == 401

    missing_page = save_annotation(client, import_id, [], page_no=9)
    assert missing_page.status_code == 404
    missing_get = client.get(f"/api/paperlab/imports/{import_id}/pages/9/annotation")
    assert missing_get.status_code == 404
    assert resegment(client, import_id, pages=[9]).status_code == 404


def test_page_list_with_annotation_summary(client):
    imported = upload_pages(client, count=2)
    import_id = imported["id"]
    save_annotation(client, import_id, [
        question_box(0.05, 0.05, 0.4, 0.2),
        answer_box(0.5, 0.6, 0.3, 0.1),
    ], page_no=1)
    insert_item(import_id, user_id(client), 1, item_no=1, bbox={"x": 0.05, "y": 0.05, "w": 0.4, "h": 0.2})

    response = client.get(f"/api/paperlab/imports/{import_id}/pages")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 2
    first, second = body["items"]
    assert first["page_no"] == 1
    assert first["annotation"]["edited"] is True
    assert first["annotation"]["version"] == 1
    assert first["annotation"]["box_count"] == 2
    assert first["annotation"]["question_count"] == 1
    assert first["annotation"]["answer_count"] == 1
    assert first["annotation"]["items"]["items"] == 1
    assert second["annotation"]["edited"] is False
    assert second["annotation"]["box_count"] == 0


# ---------------------------------------------------------------------------
# 3. 重切：生成草稿 + 裁图 + 幂等
# ---------------------------------------------------------------------------

def test_resegment_creates_items_and_crops(client):
    imported = upload_pages(client)
    import_id = imported["id"]
    save_annotation(client, import_id, [
        question_box(0.05, 0.05, 0.45, 0.25, id="q1", label="第1题"),
        question_box(0.5, 0.1, 0.4, 0.2, id="q2"),
        answer_box(0.15, 0.2, 0.2, 0.05, id="a1"),
    ])
    response = resegment(client, import_id)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["created"] == 2 and body["updated"] == 0
    assert body["skipped"] == 0 and body["stale"] == 0
    assert body["import_id"] == import_id
    assert body["page_results"][0]["page_no"] == 1

    detail = import_detail(client, import_id)
    items = detail["items"]
    assert len(items) == 2
    first, second = sorted(items, key=lambda item: item["item_no"])
    assert first["status"] == "pending"
    assert first["needs_review"] is True
    assert first["draft"]["bbox"] == {"x": 0.05, "y": 0.05, "w": 0.45, "h": 0.25}
    assert first["draft"]["_annotation"]["box_id"] == "q1"
    images = first["draft"]["images"]
    assert images and images[0]["role"] == "stem" and images[0]["asset"]
    size, mode = crop_from_asset(images[0]["asset"])
    # q1: 0.45*600=270, 0.25*800=200（页图上传后仍为 600×800）
    assert size == (270, 200)
    assert mode == "RGB"
    # 答题区按包含关系附加到第 1 题（a1 在 q1 内）
    assert first["draft"]["answer_box"] == {"x": 0.15, "y": 0.2, "w": 0.2, "h": 0.05}
    # 第 2 题没有答题区
    assert "answer_box" not in second["draft"]

    # 再次重切：应更新而不是重复创建
    again = resegment(client, import_id)
    assert again.status_code == 200, again.text
    again_body = again.json()
    assert again_body["created"] == 0 and again_body["updated"] == 2
    assert len(import_detail(client, import_id)["items"]) == 2


def test_resegment_keeps_items_when_no_annotation(client):
    imported = upload_pages(client)
    response = resegment(client, imported["id"])
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["created"] == 0
    assert any("没有切题框" in warning for warning in body["warnings"])


def test_resegment_multiple_pages(client):
    imported = upload_pages(client, count=2)
    import_id = imported["id"]
    for page_no in (1, 2):
        saved = save_annotation(client, import_id, [
            question_box(0.05, 0.05, 0.4, 0.2),
            question_box(0.5, 0.1, 0.4, 0.2),
        ], page_no=page_no)
        assert saved.status_code == 200, saved.text
    response = resegment(client, import_id)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["pages"] == [1, 2]
    assert body["created"] == 4
    assert len(import_detail(client, import_id)["items"]) == 4
    # 只重切第 2 页
    only_second = resegment(client, import_id, pages=[2])
    assert only_second.status_code == 200
    assert only_second.json()["pages"] == [2]


def test_resegment_keep_items_false_removes_stale_drafts(client):
    imported = upload_pages(client)
    import_id = imported["id"]
    uid = user_id(client)
    save_annotation(client, import_id, [question_box(0.05, 0.05, 0.4, 0.2, id="q1")])
    old_item = insert_item(import_id, uid, 1, item_no=1,
                           bbox={"x": 0.6, "y": 0.6, "w": 0.2, "h": 0.1},
                           draft={"stem": "旧的 AI 草稿", "bbox": {"x": 0.6, "y": 0.6, "w": 0.2, "h": 0.1}})
    response = resegment(client, import_id, keep_items=False)
    assert response.status_code == 200, response.text
    items = import_detail(client, import_id)["items"]
    assert len(items) == 1
    assert old_item not in {item["id"] for item in items}


# ---------------------------------------------------------------------------
# 4. 已入库草稿 stale（不覆盖）
# ---------------------------------------------------------------------------

def test_resegment_marks_committed_items_stale_without_overwrite(client):
    imported = upload_pages(client)
    import_id = imported["id"]
    save_annotation(client, import_id, [
        question_box(0.05, 0.05, 0.45, 0.25, id="q1"),
        question_box(0.5, 0.1, 0.4, 0.2, id="q2"),
    ])
    first = resegment(client, import_id)
    assert first.status_code == 200 and first.json()["created"] == 2
    accepted = accept_all(client, import_id)
    assert len(accepted) == 2
    committed = client.post(f"/api/paperlab/imports/{import_id}/commit",
                            json={"target": "app_bank"})
    assert committed.status_code == 200, committed.text
    assert committed.json()["committed"] == 2
    stored = {item["id"]: item for item in import_detail(client, import_id)["items"]}
    old_question_ids = {item_id: stored[item_id]["question_id"] for item_id in accepted}
    old_stems = {item_id: stored[item_id]["draft"]["stem"] for item_id in accepted}
    assert all(old_question_ids.values())

    edit = get_annotation(client, import_id)
    changed = save_annotation(client, import_id, [
        question_box(0.055, 0.055, 0.45, 0.25, id="q1"),
        question_box(0.505, 0.105, 0.4, 0.2, id="q2"),
    ], version=edit["edit"]["version"])
    assert changed.status_code == 200, changed.text

    second = resegment(client, import_id)
    assert second.status_code == 200, second.text
    body = second.json()
    assert body["stale"] == 2
    assert body["updated"] == 0 and body["created"] == 0
    assert len(body["stale_items"]) == 2
    assert all(item["question_id"] for item in body["stale_items"])

    after = {item["id"]: item for item in import_detail(client, import_id)["items"]}
    for item_id in accepted:
        assert after[item_id]["question_id"] == old_question_ids[item_id]
        assert after[item_id]["draft"]["stem"] == old_stems[item_id]  # 未覆盖
        assert after[item_id]["needs_review"] is True
        assert "stale" in after[item_id]["review_reason"]
        assert any("stale" in warning for warning in after[item_id]["warnings"])


# ---------------------------------------------------------------------------
# 5. 答题区更新
# ---------------------------------------------------------------------------

def test_answer_box_update_writes_draft_and_edit(client):
    imported = upload_pages(client)
    import_id = imported["id"]
    save_annotation(client, import_id, [question_box(0.05, 0.05, 0.45, 0.25)])
    resegment(client, import_id)
    item = import_detail(client, import_id)["items"][0]

    response = client.post(
        f"/api/paperlab/imports/{import_id}/items/{item['id']}/answer-box",
        json={"box": {"x": 0.1, "y": 0.7, "w": 0.6, "h": 0.2}},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["item"]["draft"]["answer_box"] == {"x": 0.1, "y": 0.7, "w": 0.6, "h": 0.2}
    assert body["item"]["draft"]["answer_boxes"] == [{"x": 0.1, "y": 0.7, "w": 0.6, "h": 0.2}]
    assert body["edit"]["edited"] is True
    assert body["edit"]["version"] == 2  # annotation 保存(1) + answer-box 同步(2)
    answer_boxes = [item for item in body["edit"]["boxes"] if item["kind"] == "answer"]
    assert answer_boxes and answer_boxes[0]["item_id"] == item["id"]
    assert answer_boxes[0]["x"] == 0.1

    # 校验与归属
    bad = client.post(
        f"/api/paperlab/imports/{import_id}/items/{item['id']}/answer-box",
        json={"box": {"x": 0.1, "y": 0.1, "w": 0, "h": 0.2}},
    )
    assert bad.status_code == 422
    missing = client.post(
        f"/api/paperlab/imports/{import_id}/items/999999/answer-box",
        json={"box": {"x": 0.1, "y": 0.1, "w": 0.2, "h": 0.2}},
    )
    assert missing.status_code == 404


def test_answer_box_update_rejects_committed_item(client):
    imported = upload_pages(client)
    import_id = imported["id"]
    save_annotation(client, import_id, [question_box(0.05, 0.05, 0.45, 0.25)])
    resegment(client, import_id)
    accept_all(client, import_id)
    committed = client.post(f"/api/paperlab/imports/{import_id}/commit", json={"target": "app_bank"})
    assert committed.status_code == 200, committed.text
    item = import_detail(client, import_id)["items"][0]
    response = client.post(
        f"/api/paperlab/imports/{import_id}/items/{item['id']}/answer-box",
        json={"box": {"x": 0.1, "y": 0.1, "w": 0.2, "h": 0.2}},
    )
    assert response.status_code == 409
    assert "已入库" in response.json()["detail"]


# ---------------------------------------------------------------------------
# 6. 发布前校验
# ---------------------------------------------------------------------------

def test_publish_rejects_before_accepting(client):
    imported = upload_pages(client)
    import_id = imported["id"]
    save_annotation(client, import_id, [question_box(0.05, 0.05, 0.45, 0.25)])
    resegment(client, import_id)
    response = client.post(f"/api/paperlab/imports/{import_id}/publish")
    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    assert detail["ready"] is False
    assert detail["summary"]["pending"] == 1
    assert detail["errors"]


def test_publish_passes_for_accepted_and_committed(client):
    imported = upload_pages(client, count=2)
    import_id = imported["id"]
    for page_no in (1, 2):
        save_annotation(client, import_id, [
            question_box(0.05, 0.05, 0.45, 0.25),
        ], page_no=page_no)
    resegment(client, import_id)
    accept_all(client, import_id)

    response = client.post(f"/api/paperlab/imports/{import_id}/publish")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ready"] is True
    assert body["summary"]["accepted"] == 2
    assert body["summary"]["committed"] == 0
    assert body["summary"]["page_count"] == 2
    assert [entry["page_no"] for entry in body["order"]] == [1, 2]
    assert body["summary"]["page_summaries"][0]["annotation_version"] == 1

    committed = client.post(f"/api/paperlab/imports/{import_id}/commit", json={"target": "app_bank"})
    assert committed.status_code == 200, committed.text
    after = client.post(f"/api/paperlab/imports/{import_id}/publish")
    assert after.status_code == 200, after.text
    assert after.json()["summary"]["committed"] == 2


def test_publish_reports_invalid_bbox(client):
    imported = upload_pages(client)
    import_id = imported["id"]
    uid = user_id(client)
    item_id = insert_item(import_id, uid, 1, item_no=1, bbox={},
                          draft={"stem": "题干了", "type": "SUBJECTIVE", "page_no": 1},
                          status="accepted")
    response = client.post(f"/api/paperlab/imports/{import_id}/publish")
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["errors"][0]["item_id"] == item_id
    assert any("bbox" in message for message in detail["errors"][0]["errors"])


# ---------------------------------------------------------------------------
# 7. 任务化分支（> 同步页数阈值时）与任务准入
# ---------------------------------------------------------------------------

async def no_dispatch(job_id, token, runner):
    return None


def test_resegment_job_branch_and_runner(client, monkeypatch):
    imported = upload_pages(client, count=2)
    import_id = imported["id"]
    for page_no in (1, 2):
        save_annotation(client, import_id, [question_box(0.05, 0.05, 0.4, 0.2)], page_no=page_no)
    provider = save_provider(client)
    monkeypatch.setattr(ai_jobs, "_dispatch", no_dispatch)
    monkeypatch.setattr(annotate, "MAX_SYNC_RESEGMENT_PAGES", 1)
    response = resegment(client, import_id, provider_id=provider["id"], model=VISION_MODEL)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["task"] == annotate.RESEGMENT_JOB_TASK
    job_id = body["job"]["id"]
    assert body["pages"] == [1, 2]

    with db.connection() as con:
        request = json.loads(con.execute(
            "SELECT request_json FROM ai_jobs WHERE id=?", (job_id,)).fetchone()[0])
    result = asyncio.run(annotate.run_annotate_job(
        job_id, user_id(client), ai_jobs.JobInput(**request), None,
    ))
    assert result["created"] == 2
    assert len(import_detail(client, import_id)["items"]) == 2

    with pytest.raises(Exception):
        asyncio.run(annotate.run_annotate_job(
            job_id, user_id(client),
            SimpleNamespace(params={"import_id": import_id, "pages": [9]}), None,
        ))


def test_resegment_job_branch_requires_provider(client, monkeypatch):
    imported = upload_pages(client, count=2)
    for page_no in (1, 2):
        save_annotation(client, imported["id"], [question_box(0.05, 0.05, 0.4, 0.2)], page_no=page_no)
    monkeypatch.setattr(annotate, "MAX_SYNC_RESEGMENT_PAGES", 1)
    response = resegment(client, imported["id"])
    assert response.status_code == 422
    assert "模型设置" in response.json()["detail"]


def test_validate_annotate_job(client):
    imported = upload_pages(client, count=2)
    uid = user_id(client)
    data = SimpleNamespace(params={"import_id": imported["id"], "pages": [1, 2]})
    annotate.validate_annotate_job(data, {"id": uid})

    with pytest.raises(Exception):
        annotate.validate_annotate_job(SimpleNamespace(params={"import_id": 999999}), {"id": uid})
    with pytest.raises(Exception):
        annotate.validate_annotate_job(
            SimpleNamespace(params={"import_id": imported["id"], "pages": [9]}), {"id": uid})
    with pytest.raises(Exception):
        annotate.validate_annotate_job(
            SimpleNamespace(params={"import_id": imported["id"], "pages": []}), {"id": uid})
