"""AI 课堂 P1 导入验收：ZIP 往返（课程/场景/素材）、损坏包 422、体积上限、归属隔离、不执行模型。"""
import base64
import hashlib
import io
import json
import zipfile

from fastapi.testclient import TestClient

from app import ai, db, storage
from app.classroom import exports, importer, interactive, pbl, schema, tts
from app.classroom import db as cdb
from app.classroom import images as cls_images

TINY_PNG = (
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)

SAFE_HTML = (
    "<!DOCTYPE html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\"><title>光照模拟</title></head>"
    "<body><div id=\"experiment\"><h1>光照强度模拟</h1><button id=\"start\">开始</button></div>"
    "<script>(function(){var n=0;var button=document.getElementById('start');"
    "button.addEventListener('click',function(){n+=1;window.parent.postMessage("
    "{source:'zhixu-interactive',type:'state',payload:{summary:'已开始 '+n+' 次',state:{runs:n}}},'*');});"
    "window.parent.postMessage({source:'zhixu-interactive',type:'ready',payload:{}},'*');})();</script>"
    "<script>window.zhixuReport=window.zhixuReport||function(t,p){window.parent.postMessage("
    "{source:'zhixu-interactive',type:t,payload:p||{}},'*');};</script>"
    "</body></html>"
)

SLIDE_RAW = {
    "elements": [
        {"id": "text_001", "type": "text", "left": 60, "top": 50, "width": 880, "height": 76,
         "content": "<p style=\"font-size:32px\"><strong>光合作用</strong></p>", "text": "光合作用",
         "fontSize": 32, "color": "#333333", "defaultFontName": "", "defaultColor": "#333333"},
        {"id": "point_001", "type": "text", "left": 80, "top": 150, "width": 800, "height": 120,
         "content": "<p style=\"font-size:20px\">• 原料：水、二氧化碳</p>", "text": "• 原料：水、二氧化碳",
         "fontSize": 20, "color": "#333333", "defaultFontName": "", "defaultColor": "#333333"},
        {"id": "latex_001", "type": "latex", "left": 640, "top": 350, "width": 280, "height": 80,
         "latex": "6CO_2 + 6H_2O \\rightarrow C_6H_{12}O_6 + 6O_2", "color": "#1f6feb", "align": "center"},
        {"id": "image_001", "type": "image", "left": 700, "top": 40, "width": 220, "height": 110,
         "src": TINY_PNG, "alt": "示意图", "fixedRatio": False},
    ]
}

QUIZ_RAW = {"questions": [
    {"id": "q1", "type": "single", "question": "光合作用发生的场所是？",
     "options": [{"label": "叶绿体", "value": "A"}, {"label": "线粒体", "value": "B"}],
     "answer": ["A"], "analysis": "叶绿体是场所。", "points": 10},
]}

PBL_RAW = {
    "project": {
        "title": "校园节能行动方案",
        "background": "学校电费上涨，请调查用电并提出节能方案。",
        "goals": ["理解电功率与电能", "用数据支撑决策"],
        "roles": [{"name": "节能项目导师", "description": "陪你完成调研与方案。"}],
        "milestones": [
            {"title": "调研校园用电", "description": "记录三类设备用电。",
             "tasks": ["列出三类设备", "估算日均耗电量"],
             "deliverable": "用电调查表", "checkpoint": "数据有来源、计算可复核"},
            {"title": "提出节能方案", "description": "针对最大耗电项设计方案。",
             "tasks": ["写具体做法", "估算节省"],
             "deliverable": "一页节能方案", "checkpoint": "方案可执行且有依据"},
        ],
        "rubric": [{"criterion": "数据真实性", "levels": [
            {"level": "优秀", "description": "数据有来源"}, {"level": "合格", "description": "数据合理"}]}],
        "mentor_hints": ["先想清楚哪些设备一直在耗电？"],
    }
}

SPEECH_ACTIONS = [{"id": "act_001", "type": "speech", "text": "这一页我们看光合作用。"}]


def create_course(client, **overrides):
    payload = {"topic": "光合作用", "subject": "生物", "grade": "初中二年级"}
    payload.update(overrides)
    response = client.post("/api/classroom/courses", json=payload)
    assert response.status_code == 200, response.text
    return response.json()["course"]


def user_id_of(client):
    return client.get("/api/auth/me").json()["id"]


def insert_scene(course_id, user_id, scene_type, order_index, title, content,
                 *, meta=None, status="ready"):
    now = db.utcnow()
    with db.connection() as con:
        return con.execute(
            "INSERT INTO cls_scenes(course_id,user_id,scene_type,order_index,title,content_json,meta_json,status,"
            "created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (course_id, user_id, scene_type, order_index, title, cdb.json_dump(content),
             cdb.json_dump(meta or {}), status, now, now),
        ).lastrowid


def setup_course(client):
    course = create_course(client)
    uid = user_id_of(client)
    slide_content, _ = schema.normalize_slide_content(SLIDE_RAW)
    quiz_content, _ = schema.normalize_quiz_content(QUIZ_RAW)
    interactive_content, _ = interactive.normalize_generated_interactive(
        {"html": SAFE_HTML, "widget": "simulation", "height": 480}, title="光照强度模拟",
        widget="simulation",
    )
    pbl_content, _ = pbl.normalize_pbl_content(PBL_RAW)
    meta = {
        "actions": SPEECH_ACTIONS, "placeholder": False, "requires_generation": False,
        "prompt_version": "classroom-test-v1", "model": "test-model",
        "position": 1, "total": 4, "digest": "场景摘要", "warnings": [],
    }
    insert_scene(course["id"], uid, "slide", 1, "光合作用", slide_content, meta=meta)
    insert_scene(course["id"], uid, "quiz", 2, "课堂小测", quiz_content, meta=meta)
    insert_scene(course["id"], uid, "interactive", 3, "光照强度模拟", interactive_content, meta=meta)
    insert_scene(course["id"], uid, "pbl", 4, "校园节能项目", pbl_content, meta=meta)
    return course, uid


def export_zip(client, course_id):
    response = client.get(f"/api/classroom/courses/{course_id}/export", params={"format": "zip"})
    assert response.status_code == 200, response.text
    return response.content


def _create_tts_asset(user_id, course_id, payload=b"ID3-import-audio"):
    digest = hashlib.sha256(payload).hexdigest()
    storage_name = "tts-import-test-%s.mp3" % digest[:16]
    storage.save_file(tts.TTS_FOLDER, storage_name, payload, "audio/mpeg")
    now = db.utcnow()
    with db.connection() as con:
        con.execute(
            "INSERT INTO cls_assets(user_id,course_id,kind,storage_name,mime_type,sha256,meta_json,created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (user_id, course_id, "tts", storage_name, "audio/mpeg", digest,
             cdb.json_dump({"voice": "alloy"}), now),
        )
    return digest


def do_import(client, data: bytes, filename="课程.zip"):
    return client.post("/api/classroom/import", files={"file": (filename, data, "application/zip")})


def _rewrite_zip(data: bytes, replacements: dict[str, bytes | None]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(data)) as source, zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as target:
        for info in source.infolist():
            if info.filename in replacements:
                replacement = replacements[info.filename]
                if replacement is None:
                    continue
                target.writestr(info.filename, replacement)
            else:
                target.writestr(info.filename, source.read(info.filename))
    return buffer.getvalue()


# ---------------------------------------------------------------------------
# 1. ZIP 往返
# ---------------------------------------------------------------------------

def test_zip_roundtrip_creates_new_course_with_scenes(client):
    course, _uid = setup_course(client)
    data = export_zip(client, course["id"])

    response = do_import(client, data)
    assert response.status_code == 200, response.text
    body = response.json()
    imported = body["course"]
    assert imported["id"] != course["id"]
    assert imported["title"] == course["title"]
    assert imported["status"] == "ready"
    assert imported["scene_count"] == 4
    assert body["scene_count"] == 4
    assert (imported.get("config") or {}).get("imported", {}).get("format") == exports.ZIP_FORMAT

    detail = client.get(f"/api/classroom/courses/{imported['id']}").json()
    original = client.get(f"/api/classroom/courses/{course['id']}").json()
    assert [scene["type"] for scene in detail["scenes"]] == \
           [scene["type"] for scene in original["scenes"]] == ["slide", "quiz", "interactive", "pbl"]
    assert [scene["title"] for scene in detail["scenes"]] == \
           [scene["title"] for scene in original["scenes"]]
    assert [scene["status"] for scene in detail["scenes"]] == ["ready"] * 4

    imported_slide = detail["scenes"][0]
    assert len(imported_slide["content"]["canvas"]["elements"]) == len(
        original["scenes"][0]["content"]["canvas"]["elements"])
    assert imported_slide["actions"][0]["type"] == "speech"
    imported_interactive = detail["scenes"][2]
    assert imported_interactive["content"]["html"] == original["scenes"][2]["content"]["html"]
    imported_pbl = detail["scenes"][3]
    assert imported_pbl["content"]["project"]["milestones"] == \
           original["scenes"][3]["content"]["project"]["milestones"]
    assert imported_pbl["content"]["project"]["goals"]
    assert imported_pbl["meta"]["placeholder"] is False

    listing = client.get("/api/classroom/courses").json()
    assert listing["total"] == 2


def test_import_route_alias_and_ownership_isolation(client, second_client):
    course, _uid = setup_course(client)
    data = export_zip(client, course["id"])
    response = client.post("/api/classroom/courses/import", files={
        "file": ("课程.zip", data, "application/zip"),
    })
    assert response.status_code == 200, response.text
    imported_id = response.json()["course"]["id"]
    assert second_client.get(f"/api/classroom/courses/{imported_id}").status_code == 404
    assert second_client.get("/api/classroom/courses").json()["total"] == 0

    from app.main import app

    anon = TestClient(app)
    assert anon.post("/api/classroom/import", files={
        "file": ("课程.zip", data, "application/zip"),
    }).status_code == 401


def test_import_restores_assets(client):
    course, uid = setup_course(client)
    digest = _create_tts_asset(uid, course["id"])
    data = export_zip(client, course["id"])
    response = do_import(client, data)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["imported_assets"] >= 1
    imported_id = body["course"]["id"]
    with db.connection() as con:
        rows = [dict(row) for row in con.execute(
            "SELECT * FROM cls_assets WHERE course_id=? AND user_id=?", (imported_id, uid),
        ).fetchall()]
    assert rows, "素材应随课程导入"
    restored = next(row for row in rows if row["sha256"] == digest)
    assert storage.read_file(tts.TTS_FOLDER, restored["storage_name"]) == b"ID3-import-audio"
    assert (cdb.json_load(restored["meta_json"], {}) or {}).get("imported") is True


def test_import_does_not_call_model(client, monkeypatch):
    course, _uid = setup_course(client)
    data = export_zip(client, course["id"])

    async def forbidden(*args, **kwargs):
        raise AssertionError("导入不应调用模型")

    monkeypatch.setattr(ai, "_call_model", forbidden)
    response = do_import(client, data)
    assert response.status_code == 200, response.text


# ---------------------------------------------------------------------------
# 2. 损坏 / 非法课程包 → 明确 422
# ---------------------------------------------------------------------------

def test_import_rejects_non_zip_and_missing_manifest(client):
    response = do_import(client, "这不是 zip".encode("utf-8"))
    assert response.status_code == 422
    assert "ZIP" in response.json()["detail"]

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("course.json", b"{}")
    response = do_import(client, buffer.getvalue())
    assert response.status_code == 422
    assert "manifest" in response.json()["detail"]

    response = do_import(client, b"x" * 10, filename="课程.rar")
    assert response.status_code == 422
    assert "zip" in response.json()["detail"]


def test_import_rejects_wrong_format_and_version(client):
    course, _uid = setup_course(client)
    data = export_zip(client, course["id"])
    manifest = json.loads(zipfile.ZipFile(io.BytesIO(data)).read("manifest.json"))
    wrong_format = dict(manifest)
    wrong_format["format"] = "other-app-zip"
    response = do_import(client, _rewrite_zip(data, {
        "manifest.json": json.dumps(wrong_format, ensure_ascii=False).encode("utf-8"),
    }))
    assert response.status_code == 422
    assert "课程包" in response.json()["detail"]

    too_new = dict(manifest)
    too_new["formatVersion"] = 99
    response = do_import(client, _rewrite_zip(data, {
        "manifest.json": json.dumps(too_new, ensure_ascii=False).encode("utf-8"),
    }))
    assert response.status_code == 422
    assert "版本" in response.json()["detail"]


def test_import_rejects_corrupt_scene_and_unknown_type(client):
    course, _uid = setup_course(client)
    data = export_zip(client, course["id"])
    corrupt = _rewrite_zip(data, {"scenes/scene_001.json": b"not-json"})
    response = do_import(client, corrupt)
    assert response.status_code == 422
    assert "JSON" in response.json()["detail"]

    manifest = json.loads(zipfile.ZipFile(io.BytesIO(data)).read("manifest.json"))
    scene_entry = next(item for item in manifest["scenes"] if item["type"] == "slide")
    scene_payload = json.loads(zipfile.ZipFile(io.BytesIO(data)).read(scene_entry["path"]))
    scene_payload["scene_type"] = "video"
    response = do_import(client, _rewrite_zip(data, {
        scene_entry["path"]: json.dumps(scene_payload, ensure_ascii=False).encode("utf-8"),
    }))
    assert response.status_code == 422
    assert "不支持" in response.json()["detail"]


def test_import_rejects_path_traversal_and_empty_scenes(client):
    course, _uid = setup_course(client)
    data = export_zip(client, course["id"])
    manifest = json.loads(zipfile.ZipFile(io.BytesIO(data)).read("manifest.json"))
    traversal = json.loads(json.dumps(manifest, ensure_ascii=False))
    traversal["scenes"][0]["path"] = "../evil.json"
    response = do_import(client, _rewrite_zip(data, {
        "manifest.json": json.dumps(traversal, ensure_ascii=False).encode("utf-8"),
    }))
    assert response.status_code == 422
    assert "路径" in response.json()["detail"]

    empty = json.loads(json.dumps(manifest, ensure_ascii=False))
    empty["scenes"] = []
    response = do_import(client, _rewrite_zip(data, {
        "manifest.json": json.dumps(empty, ensure_ascii=False).encode("utf-8"),
    }))
    assert response.status_code == 422
    assert "scenes" in response.json()["detail"]


def test_import_rejects_asset_checksum_mismatch(client):
    course, uid = setup_course(client)
    _create_tts_asset(uid, course["id"])
    data = export_zip(client, course["id"])
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        asset_path = manifest["resources"][0]["path"]
    tampered = {asset_path: b"tampered-audio"}
    response = do_import(client, _rewrite_zip(data, tampered))
    assert response.status_code == 422
    assert "sha256" in response.json()["detail"]


def test_import_file_size_limit(client, monkeypatch):
    course, _uid = setup_course(client)
    data = export_zip(client, course["id"])
    monkeypatch.setattr(importer, "MAX_IMPORT_BYTES", 8)
    response = do_import(client, data)
    assert response.status_code == 413
    assert "MB" in response.json()["detail"] or "超过" in response.json()["detail"]


# ---------------------------------------------------------------------------
# 3. P1 素材类型控制（按内容识别、不信包内声明）与删除/恢复
# ---------------------------------------------------------------------------

def _tiny_png() -> bytes:
    return base64.b64decode(TINY_PNG.split(",", 1)[1])


def _image_slide(asset_id):
    return {
        "type": "slide", "schemaVersion": 1,
        "canvas": {"width": 1000, "height": 562.5, "theme": {}, "elements": [
            {"id": "image_001", "type": "image", "left": 20, "top": 20, "width": 120,
             "height": 120, "asset_id": int(asset_id),
             "src": "/api/classroom/assets/%d" % int(asset_id), "alt": "配图"},
        ]},
    }


def _insert_asset_row(user_id, course_id, payload, mime, *, kind="image"):
    digest = hashlib.sha256(payload).hexdigest()
    storage_name = "forged-%s-%s.bin" % (kind, digest[:20])
    storage.save_file(cdb.ASSET_FOLDER, storage_name, payload, mime)
    now = db.utcnow()
    with db.connection() as con:
        asset_id = con.execute(
            "INSERT INTO cls_assets(user_id,course_id,kind,storage_name,mime_type,sha256,meta_json,created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (user_id, course_id, kind, storage_name, mime, digest,
             cdb.json_dump({"forged": True}), now),
        ).lastrowid
    return int(asset_id), storage_name


def _imported_assets(course_id):
    with db.connection() as con:
        return [dict(row) for row in con.execute(
            "SELECT * FROM cls_assets WHERE course_id=? ORDER BY id", (course_id,),
        ).fetchall()]


def _first_image_element(course_detail: dict):
    for scene in course_detail["scenes"]:
        for element in ((scene.get("content") or {}).get("canvas") or {}).get("elements") or []:
            if isinstance(element, dict) and element.get("type") == "image":
                return element
    return None


def test_import_uses_content_type_not_package_declaration(client):
    course = create_course(client)
    uid = user_id_of(client)
    asset = cls_images.save_image_asset(uid, course["id"], _tiny_png(), "image/png")
    insert_scene(course["id"], uid, "slide", 1, "配图页", _image_slide(asset["id"]))
    data = export_zip(client, course["id"])

    # 包内把真实 PNG 声明成 text/html：导入应按内容识别为 image/png，仍可内联读取
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        manifest = json.loads(archive.read("manifest.json"))
    image_resource = next(item for item in manifest["resources"] if item["kind"] == "image")
    image_resource["mimeType"] = "text/html"
    forged = _rewrite_zip(data, {
        "manifest.json": json.dumps(manifest, ensure_ascii=False).encode("utf-8"),
    })
    response = do_import(client, forged)
    assert response.status_code == 200, response.text
    body = response.json()
    assert any("与内容不符" in item for item in body["asset_warnings"])
    imported_id = body["course"]["id"]

    assets = [row for row in _imported_assets(imported_id) if row["kind"] == "image"]
    assert len(assets) == 1
    assert assets[0]["mime_type"] == "image/png"      # 不采信 text/html 声明
    assert (cdb.json_load(assets[0]["meta_json"], {}) or {}).get("inline_allowed") is not False
    served = client.get("/api/classroom/assets/%d" % int(assets[0]["id"]))
    assert served.status_code == 200
    assert served.headers["content-type"].startswith("image/png")
    assert "content-disposition" not in served.headers

    detail = client.get(f"/api/classroom/courses/{imported_id}").json()
    element = _first_image_element(detail)
    assert element is not None and client.get(element["src"]).status_code == 200


def test_import_downgrades_forged_executable_asset_to_isolated_download(client):
    course = create_course(client)
    uid = user_id_of(client)
    html = b"<!DOCTYPE html><html><body><script>alert(1)</script></body></html>"
    asset_id, _storage_name = _insert_asset_row(uid, course["id"], html, "image/png", kind="image")
    insert_scene(course["id"], uid, "slide", 1, "伪造图片页", _image_slide(asset_id))
    data = export_zip(client, course["id"])

    response = do_import(client, data)
    assert response.status_code == 200, response.text
    body = response.json()
    assert any("隔离下载" in item for item in body["asset_warnings"])
    imported_id = body["course"]["id"]
    assets = [row for row in _imported_assets(imported_id) if row["kind"] == "image"]
    assert len(assets) == 1
    assert assets[0]["mime_type"] == "application/octet-stream"  # 主动内容降级为二进制
    meta = cdb.json_load(assets[0]["meta_json"], {}) or {}
    assert meta.get("inline_allowed") is False
    assert meta.get("declared_mime_type") == "image/png"

    served = client.get("/api/classroom/assets/%d" % int(assets[0]["id"]))
    assert served.status_code == 200
    assert "attachment" in served.headers["content-disposition"]
    assert served.headers["content-type"].startswith("application/octet-stream")
    assert served.headers["x-content-type-options"] == "nosniff"
    assert served.content == html


def test_import_twice_shares_content_file_and_delete_keeps_other(client):
    course = create_course(client)
    uid = user_id_of(client)
    asset = cls_images.save_image_asset(uid, course["id"], _tiny_png(), "image/png")
    insert_scene(course["id"], uid, "slide", 1, "配图页", _image_slide(asset["id"]))
    data = export_zip(client, course["id"])

    first_id = do_import(client, data).json()["course"]["id"]
    second_id = do_import(client, data).json()["course"]["id"]
    first_assets = [row for row in _imported_assets(first_id) if row["kind"] == "image"]
    second_assets = [row for row in _imported_assets(second_id) if row["kind"] == "image"]
    assert len(first_assets) == 1 and len(second_assets) == 1
    # 两次导入内容相同 → 共享同一 storage_name 文件
    assert first_assets[0]["storage_name"] == second_assets[0]["storage_name"]
    second_url = "/api/classroom/assets/%d" % int(second_assets[0]["id"])
    assert client.get(second_url).status_code == 200

    deleted = client.delete(f"/api/classroom/courses/{first_id}")
    assert deleted.status_code == 200
    audit = deleted.json()["assets"]
    assert audit["reclaimed"] == []
    assert audit["files_removed"] == 0
    assert audit["shared_files_kept"]
    assert client.get(second_url).status_code == 200  # 另一门课仍可读同一内容文件

    client.delete(f"/api/classroom/courses/{second_id}")
    assert client.get(second_url).status_code == 404


def test_delete_then_reimport_restores_image_assets(client):
    course = create_course(client)
    uid = user_id_of(client)
    asset = cls_images.save_image_asset(uid, course["id"], _tiny_png(), "image/png")
    insert_scene(course["id"], uid, "slide", 1, "配图页", _image_slide(asset["id"]))
    data = export_zip(client, course["id"])
    original_url = "/api/classroom/assets/%d" % int(asset["id"])
    assert client.get(original_url).status_code == 200

    deleted = client.delete(f"/api/classroom/courses/{course['id']}")
    assert deleted.status_code == 200
    audit = deleted.json()["assets"]
    assert [item["asset_id"] for item in audit["reclaimed"]] == [int(asset["id"])]
    assert client.get(original_url).status_code == 404  # 素材已随课程回收

    # 重新导入 = 按现有恢复语义复原课程与素材引用
    restored = do_import(client, data)
    assert restored.status_code == 200, restored.text
    restored_id = restored.json()["course"]["id"]
    detail = client.get(f"/api/classroom/courses/{restored_id}").json()
    element = _first_image_element(detail)
    assert element is not None
    served = client.get(element["src"])
    assert served.status_code == 200
    assert served.headers["content-type"].startswith("image/png")
