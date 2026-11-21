"""AI 课堂 P1 导出验收：PPTX 可打开/中文文件名、离线单文件 HTML 无外链、ZIP 结构与素材、体积降级。

全部离线：不访问网络；TTS 用 monkeypatch 的假实现。
"""
import hashlib
import io
import json
from urllib.parse import quote
import zipfile

from fastapi.testclient import TestClient
import pytest

from app import db, storage
from app.classroom import exports, interactive, pbl, schema, tts
from app.classroom import db as cdb

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
        {"id": "text_001", "type": "text", "left": 60, "top": 40, "width": 880, "height": 70,
         "content": "<p style=\"font-size:32px\"><strong>光合作用</strong></p>", "text": "光合作用",
         "fontSize": 32, "color": "#333333", "defaultFontName": "", "defaultColor": "#333333"},
        {"id": "point_001", "type": "text", "left": 80, "top": 150, "width": 500, "height": 120,
         "content": "<p style=\"font-size:20px\">• 原料：水、二氧化碳</p>", "text": "• 原料：水、二氧化碳",
         "fontSize": 20, "color": "#333333", "defaultFontName": "", "defaultColor": "#333333"},
        {"id": "shape_001", "type": "shape", "left": 640, "top": 150, "width": 280, "height": 200,
         "path": "M 0 0 L 1 0 L 1 1 L 0 1 Z", "viewBox": [1, 1], "fill": "#e7f5ff"},
        {"id": "latex_001", "type": "latex", "left": 640, "top": 370, "width": 280, "height": 80,
         "latex": "6CO_2 + 6H_2O \\rightarrow C_6H_{12}O_6 + 6O_2", "color": "#1f6feb", "align": "center"},
        {"id": "table_001", "type": "table", "left": 80, "top": 300, "width": 500, "height": 160,
         "colWidths": [0.34, 0.33, 0.33], "data": [
             [{"id": None, "colspan": 1, "rowspan": 1, "text": "类别"},
              {"id": None, "colspan": 1, "rowspan": 1, "text": "物质"}],
             [{"id": None, "colspan": 1, "rowspan": 1, "text": "原料"},
              {"id": None, "colspan": 1, "rowspan": 1, "text": "水、二氧化碳"}],
         ], "outline": {"width": 1, "style": "solid", "color": "#cccccc"}},
        {"id": "chart_001", "type": "chart", "left": 80, "top": 470, "width": 400, "height": 80,
         "chartType": "line", "data": {"labels": ["弱光", "强光"], "legends": ["光合速率"],
                                        "series": [[12, 36]]},
         "themeColors": ["#2f9e44", "#e8590c"]},
        {"id": "code_001", "type": "code", "left": 520, "top": 470, "width": 400, "height": 70,
         "language": "python", "code": "print('photosynthesis')"},
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


def save_provider(client, name="导出测试连接", models=("test-model",)):
    response = client.post("/api/providers", json={
        "name": name, "kind": "openai", "base_url": "https://api.openai.com/v1",
        "api_key": "sk-classroom-export-test-123456", "models": list(models),
    })
    assert response.status_code == 200, response.text
    return response.json()


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


def _meta(actions=None, *, position=1, total=4, placeholder=False, warnings=None):
    return {
        "actions": actions or SPEECH_ACTIONS,
        "placeholder": placeholder,
        "requires_generation": placeholder,
        "prompt_version": "classroom-test-v1",
        "model": "test-model",
        "position": position,
        "total": total,
        "digest": "场景摘要",
        "warnings": warnings or [],
    }


def setup_course_with_scenes(client):
    """建一门含 slide/quiz/interactive/pbl 的课程（直接落库，不走模型）。"""
    course = create_course(client)
    uid = user_id_of(client)
    slide_content, _ = schema.normalize_slide_content(SLIDE_RAW)
    quiz_content, _ = schema.normalize_quiz_content(QUIZ_RAW)
    interactive_content, _ = interactive.normalize_generated_interactive(
        {"html": SAFE_HTML, "widget": "simulation", "height": 480}, title="光照强度模拟",
        widget="simulation",
    )
    pbl_content, _ = pbl.normalize_pbl_content(PBL_RAW)
    insert_scene(course["id"], uid, "slide", 1, "光合作用", slide_content, meta=_meta(total=5))
    insert_scene(course["id"], uid, "quiz", 2, "课堂小测", quiz_content, meta=_meta(total=5))
    insert_scene(course["id"], uid, "interactive", 3, "光照强度模拟", interactive_content,
                 meta=_meta(total=5))
    insert_scene(course["id"], uid, "pbl", 4, "校园节能项目", pbl_content, meta=_meta(total=5))
    insert_scene(course["id"], uid, "slide", 5, "总结回顾",
                 schema.normalize_slide_content({"elements": [
                     {"id": "t", "type": "text", "left": 60, "top": 60, "width": 800, "height": 60,
                      "content": "<p>知识回顾</p>", "text": "知识回顾", "fontSize": 28,
                      "color": "#333333", "defaultFontName": "", "defaultColor": "#333333"},
                 ]})[0], meta=_meta(total=5))
    return course, uid


def export_bytes(client, course_id, fmt, **params):
    response = client.get(f"/api/classroom/courses/{course_id}/export", params={"format": fmt, **params})
    return response


def _all_pptx_text(prs):
    texts = []
    for slide in prs.slides:
        for shape in slide.shapes:
            if getattr(shape, "has_text_frame", False):
                texts.append(shape.text_frame.text)
            if getattr(shape, "has_table", False):
                for row in shape.table.rows:
                    for cell in row.cells:
                        texts.append(cell.text)
    return "\n".join(texts)


# ---------------------------------------------------------------------------
# 1. 鉴权 / 归属 / 参数 / 空课程
# ---------------------------------------------------------------------------

def test_export_guards_login_ownership_format_and_empty_course(client, second_client):
    course, _uid = setup_course_with_scenes(client)
    assert export_bytes(client, course["id"], "pptx").status_code == 200
    assert export_bytes(second_client, course["id"], "pptx").status_code == 404
    assert export_bytes(client, course["id"], "doc").status_code == 422
    from app.main import app

    anon = TestClient(app)
    assert anon.get(f"/api/classroom/courses/{course['id']}/export?format=zip").status_code == 401

    empty = create_course(client, topic="空课程")
    response = export_bytes(client, empty["id"], "zip")
    assert response.status_code == 422
    assert "还没有场景" in response.json()["detail"]


# ---------------------------------------------------------------------------
# 2. PPTX
# ---------------------------------------------------------------------------

def test_pptx_export_is_openable_with_expected_slides_and_text(client):
    from pptx import Presentation
    from pptx.enum.shapes import MSO_SHAPE_TYPE

    course, _uid = setup_course_with_scenes(client)
    response = export_bytes(client, course["id"], "pptx")
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("application/vnd.openxmlformats")
    disposition = response.headers["content-disposition"]
    assert "attachment;" in disposition
    assert 'filename="classroom-export.pptx"' in disposition
    assert "filename*=UTF-8''" in disposition
    assert quote("光合作用.pptx") in disposition
    assert "unsafe-eval" not in disposition

    prs = Presentation(io.BytesIO(response.content))
    # 标题页 + 5 个场景各一页
    assert len(prs.slides) == 6
    text = _all_pptx_text(prs)
    assert "光合作用" in text
    assert "课堂小测" in text
    assert "光照强度模拟" in text
    assert "校园节能项目" in text
    assert "知识回顾" in text
    # 公式文本降级 + 图片 + 表格 + 代码
    assert "6CO_2" in text
    assert "photosynthesis" in text
    assert "水、二氧化碳" in text
    pictures = [shape for slide in prs.slides for shape in slide.shapes
                if shape.shape_type == MSO_SHAPE_TYPE.PICTURE]
    assert pictures
    # 备注里记录公式降级与旁白
    notes = "\n".join(
        slide.notes_slide.notes_text_frame.text for slide in prs.slides
        if getattr(slide, "has_notes_slide", False)
    )
    assert "公式以文本降级" in notes
    assert "光合作用" in notes


def test_pptx_placeholder_pages_and_pagination(client):
    from pptx import Presentation

    course = create_course(client, topic="分页测试课")
    uid = user_id_of(client)
    elements = [
        {"id": "text_%02d" % index, "type": "text", "left": 40 + (index % 3) * 300,
         "top": 40 + (index // 3) * 120, "width": 280, "height": 100,
         "content": "<p>要点 %d</p>" % index, "text": "要点 %d" % index, "fontSize": 18,
         "color": "#333333", "defaultFontName": "", "defaultColor": "#333333"}
        for index in range(1, 13)
    ]
    content, _ = schema.normalize_slide_content({"elements": elements})
    insert_scene(course["id"], uid, "slide", 1, "要点很多的幻灯片", content, meta=_meta(total=1))
    response = export_bytes(client, course["id"], "pptx")
    assert response.status_code == 200
    prs = Presentation(io.BytesIO(response.content))
    # 标题页 + 第 1 页（10 个元素）+ 第 2 页（2 个元素，标题带页码）
    assert len(prs.slides) == 3
    text = _all_pptx_text(prs)
    assert "1/2" in text and "2/2" in text
    assert "要点 1" in text and "要点 12" in text


# ---------------------------------------------------------------------------
# 3. 离线单文件 HTML
# ---------------------------------------------------------------------------

def _external_reference_problems(html: str) -> list[str]:
    lowered = html.lower()
    problems = []
    for marker in (
        "src=\"http", "src='http", "href=\"http", "href='http", "url(http", "@import",
        "<link", "<script src", "<script  src", "http://", "https://",
    ):
        if marker in lowered:
            problems.append(marker)
    return problems


def test_offline_html_contains_course_json_and_no_external_references(client):
    course, _uid = setup_course_with_scenes(client)
    response = export_bytes(client, course["id"], "html")
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/html")
    assert "filename*=UTF-8''" in response.headers["content-disposition"]
    html = response.text
    assert 'id="course-data"' in html
    assert course["title"] in html
    for title in ("光合作用", "课堂小测", "光照强度模拟", "校园节能项目", "知识回顾"):
        assert title in html
    assert "speechSynthesis" in html  # 浏览器 TTS 兜底
    assert "allow-scripts" in html  # 交互 iframe: sandbox="allow-scripts"
    assert "allow-same-origin" not in html
    assert _external_reference_problems(html) == []

    # 内联课程 JSON 可解析，且交互场景 HTML 已内联
    raw = html.split('<script id="course-data" type="application/json">', 1)[1].split("</script>", 1)[0]
    snapshot = json.loads(raw.replace("<\\/", "</"))
    assert snapshot["formatVersion"] == 1
    assert snapshot["course"]["title"] == course["title"]
    assert [scene["type"] for scene in snapshot["scenes"]] == ["slide", "quiz", "interactive", "pbl", "slide"]
    assert "postMessage" in snapshot["scenes"][2]["content"]["html"]
    assert snapshot["scenes"][3]["content"]["project"]["milestones"]


def test_offline_html_too_large_falls_back_to_zip(client, monkeypatch):
    course, _uid = setup_course_with_scenes(client)
    monkeypatch.setattr(exports, "MAX_STANDALONE_HTML_BYTES", 256)
    response = export_bytes(client, course["id"], "html")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/zip")
    assert response.headers["x-zhixu-export-fallback"] == "zip"
    assert "filename*=UTF-8''" in response.headers["content-disposition"]
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        names = archive.namelist()
        assert "manifest.json" in names
        assert "EXPORT-NOTE.txt" in names
        assert "过大" in archive.read("EXPORT-NOTE.txt").decode("utf-8")


# ---------------------------------------------------------------------------
# 4. ZIP 结构 / 素材
# ---------------------------------------------------------------------------

def _create_tts_asset(user_id, course_id, payload=b"ID3-fake-audio-bytes"):
    digest = hashlib.sha256(payload).hexdigest()
    storage_name = "tts-export-test-%s.mp3" % digest[:16]
    storage.save_file(tts.TTS_FOLDER, storage_name, payload, "audio/mpeg")
    now = db.utcnow()
    with db.connection() as con:
        asset_id = con.execute(
            "INSERT INTO cls_assets(user_id,course_id,kind,storage_name,mime_type,sha256,meta_json,created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (user_id, course_id, "tts", storage_name, "audio/mpeg", digest,
             cdb.json_dump({"voice": "alloy", "size": len(payload)}), now),
        ).lastrowid
    return int(asset_id), digest


def test_zip_export_contains_manifest_scenes_and_assets(client):
    course, uid = setup_course_with_scenes(client)
    _asset_id, digest = _create_tts_asset(uid, course["id"])
    response = export_bytes(client, course["id"], "zip")
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("application/zip")
    assert "filename*=UTF-8''" in response.headers["content-disposition"]
    assert quote("光合作用.zip") in response.headers["content-disposition"]
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        names = set(archive.namelist())
        assert {"manifest.json", "course.json"} <= names
        assert {"scenes/scene_%03d.json" % index for index in range(1, 6)} <= names
        manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
        assert manifest["format"] == exports.ZIP_FORMAT
        assert manifest["formatVersion"] == exports.ZIP_FORMAT_VERSION
        assert len(manifest["scenes"]) == 5
        assert [scene["type"] for scene in manifest["scenes"]] == ["slide", "quiz", "interactive", "pbl", "slide"]
        assert manifest["resources"], "应包含课程素材清单"
        resource = manifest["resources"][0]
        assert resource["sha256"] == digest
        assert resource["path"] in names
        assert hashlib.sha256(archive.read(resource["path"])).hexdigest() == digest
        course_json = json.loads(archive.read("course.json").decode("utf-8"))
        assert course_json["title"] == course["title"]
        scene1 = json.loads(archive.read("scenes/scene_001.json").decode("utf-8"))
        assert scene1["type"] == "slide"
        assert scene1["content"]["canvas"]["elements"]
        assert scene1["actions"][0]["type"] == "speech"


# ---------------------------------------------------------------------------
# 5. 可选 TTS
# ---------------------------------------------------------------------------

def test_tts_export_falls_back_to_browser(client, monkeypatch):
    course, _uid = setup_course_with_scenes(client)

    async def fake_synthesize(**kwargs):
        return {"fallback": "browser", "error": "未配置语音连接"}

    monkeypatch.setattr(exports.tts, "synthesize", fake_synthesize)
    response = export_bytes(client, course["id"], "html", include_tts="true")
    assert response.status_code == 200, response.text
    assert response.headers["x-zhixu-tts"] == "browser"
    assert "speechSynthesis" in response.text


def test_tts_export_inlines_generated_audio(client, monkeypatch):
    course, uid = setup_course_with_scenes(client)
    asset_id, _digest = _create_tts_asset(uid, course["id"])

    async def fake_synthesize(**kwargs):
        return {
            "asset": {"id": asset_id, "kind": "tts", "mime_type": "audio/mpeg",
                      "url": "/api/classroom/assets/%d" % asset_id},
            "provider": "mock-tts", "model": "tts-1", "voice": "alloy",
        }

    monkeypatch.setattr(exports.tts, "synthesize", fake_synthesize)
    response = export_bytes(client, course["id"], "html", include_tts="true")
    assert response.status_code == 200, response.text
    assert response.headers["x-zhixu-tts"] == "assets"
    assert "data:audio/mpeg;base64," in response.text
    assert "speechSynthesis" in response.text  # 仍保留浏览器兜底

    zip_response = export_bytes(client, course["id"], "zip", include_tts="true")
    assert zip_response.status_code == 200
    with zipfile.ZipFile(io.BytesIO(zip_response.content)) as archive:
        manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
        kinds = {resource["kind"] for resource in manifest["resources"]}
        assert "tts" in kinds


# ---------------------------------------------------------------------------
# 6. 教学资源包（format=package）
# ---------------------------------------------------------------------------

def _add_scene(course_id, uid, scene_type, order_index, title, content):
    return insert_scene(course_id, uid, scene_type, order_index, title, content,
                        meta=_meta(total=order_index))


def _package_zip(response):
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("application/zip")
    return zipfile.ZipFile(io.BytesIO(response.content))


def test_package_export_structure_files_and_no_external_refs(client):
    from pptx import Presentation

    course, uid = setup_course_with_scenes(client)
    # 追加第二个交互场景，验证「每个 interactive 场景一份独立 HTML」
    second_content, _ = interactive.normalize_generated_interactive(
        {"html": SAFE_HTML.replace("光照强度模拟", "温度影响模拟"), "widget": "simulation",
         "height": 480},
        title="温度影响模拟", widget="simulation",
    )
    _add_scene(course["id"], uid, "interactive", 6, "温度影响模拟", second_content)

    response = export_bytes(client, course["id"], "package")
    assert response.headers["content-disposition"].startswith("attachment;")
    disposition = response.headers["content-disposition"]
    assert quote("资源包-光合作用.zip") in disposition
    assert 'filename="classroom-export.zip"' in disposition
    assert response.headers["x-zhixu-export-format"] == "package"

    with _package_zip(response) as archive:
        names = set(archive.namelist())
        assert {"course.pptx", "course.json", "ets.json", "README.txt"} <= names
        interactive_names = sorted(name for name in names if name.startswith("interactive/"))
        assert len(interactive_names) == 2, interactive_names
        assert interactive_names[0] == "interactive/scene-03-光照强度模拟.html"
        assert interactive_names[1] == "interactive/scene-06-温度影响模拟.html"

        # PPTX 可被 python-pptx 打开：标题页 + 6 个场景页
        prs = Presentation(io.BytesIO(archive.read("course.pptx")))
        assert len(prs.slides) == 7
        assert "光照强度模拟" in _all_pptx_text(prs)

        # course.json 与 ets.json 内容一致，且是共享的导出快照结构
        course_json = archive.read("course.json").decode("utf-8")
        assert archive.read("ets.json").decode("utf-8") == course_json
        snapshot = json.loads(course_json)
        assert snapshot["formatVersion"] == exports.ZIP_FORMAT_VERSION
        assert snapshot["course"]["title"] == course["title"]
        assert [scene["type"] for scene in snapshot["scenes"]] == \
               ["slide", "quiz", "interactive", "pbl", "slide", "interactive"]

        # 每个交互 HTML：独立自包含（沙箱 iframe + 原内容），无外链
        for name in interactive_names:
            html = archive.read(name).decode("utf-8")
            assert html.startswith("<!DOCTYPE html>")
            assert 'sandbox="allow-scripts"' in html
            assert "allow-same-origin" not in html
            assert "postMessage" in html           # 原交互内容已嵌入 srcdoc
            assert "zhixu-interactive" in html     # 交互事件桥接提示
            assert "无需联网" in html
            assert _external_reference_problems(html) == [], name

        # README：中文说明、课程/场景清单、文件与打开方式
        readme = archive.read("README.txt").decode("utf-8")
        for marker in ("知序 AI 课堂 · 教学资源包", course["title"], "光照强度模拟",
                       "温度影响模拟", "course.pptx", "interactive/", "离线", "无需联网",
                       "course.json", "ets.json"):
            assert marker in readme, marker
        assert _external_reference_problems(readme) == []
        # course.json 文本也不含外链（图片为 data URI；交互内容无外链）
        assert _external_reference_problems(course_json) == []


def test_package_export_guards_ownership_login_and_empty_course(client, second_client):
    course, _uid = setup_course_with_scenes(client)
    assert export_bytes(client, course["id"], "package").status_code == 200
    assert export_bytes(second_client, course["id"], "package").status_code == 404

    from app.main import app

    anon = TestClient(app)
    assert anon.get(f"/api/classroom/courses/{course['id']}/export?format=package").status_code == 401

    empty = create_course(client, topic="空资源包课程")
    response = export_bytes(client, empty["id"], "package")
    assert response.status_code == 422
    assert "还没有场景" in response.json()["detail"]


def test_package_export_skips_missing_html_and_warns_on_external_refs(client):
    course = create_course(client, topic="资源包告警课")
    uid = user_id_of(client)
    slide_content, _ = schema.normalize_slide_content({"elements": [
        {"id": "t", "type": "text", "left": 60, "top": 60, "width": 800, "height": 60,
         "content": "<p>说明</p>", "text": "说明", "fontSize": 28, "color": "#333333",
         "defaultFontName": "", "defaultColor": "#333333"},
    ]})
    _add_scene(course["id"], uid, "slide", 1, "说明页", slide_content)
    # 直接落库的旧/异常内容（绕过生成期校验）：
    _add_scene(course["id"], uid, "interactive", 2, "无内容交互", {
        "type": "interactive", "widget": "simulation", "html": "", "height": 480,
    })
    _add_scene(course["id"], uid, "interactive", 3, "含外链交互", {
        "type": "interactive", "widget": "simulation",
        "html": "<!DOCTYPE html><html><body><script src=\"https://example.com/x.js\"></script></body></html>",
        "height": 480,
    })

    response = export_bytes(client, course["id"], "package")
    with _package_zip(response) as archive:
        names = set(archive.namelist())
        interactive_names = [name for name in names if name.startswith("interactive/")]
        assert interactive_names == ["interactive/scene-03-含外链交互.html"]
        readme = archive.read("README.txt").decode("utf-8")
        assert "检查提示" in readme
        assert "没有可导出的 HTML" in readme
        assert "疑似外链标记" in readme
        # 告警内容不会被静默吞掉；PPTX/course.json 仍然生成
        assert {"course.pptx", "course.json", "ets.json"} <= names

