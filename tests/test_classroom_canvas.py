"""P6b 自由画布编辑器验收（构建产物 / 父页 iframe 集成协议 / 保存契约）。

覆盖：
- ``web/editor/`` 构建产物存在、index 引用资源为相对路径且可解析、无外链资源；
- ``editor/`` 构建工程可复现（package.json / vite 配置 / README / vendored 上游许可与 commit）；
- 父页组件 ``web/js/components/classroomCanvas.js`` 与 ``editor/src/protocol.ts`` 的
  postMessage 协议常量一致，且因平台 CSP（frame-ancestors 'none'）使用 iframe srcdoc 挂载；
- 后端 ``PATCH /api/classroom/courses/{id}/scenes/{sid}/content`` 能保存自由画布产出的
  slide content（复用 P6a 的 version CAS / 归一化思路，聚焦画布字段往返）；
- 有 Node 时对源 JS 组件做 ``node --check``。

不联网：只建课程/场景数据，不调用模型；构建产物已在仓库中，测试不执行构建。
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from app import db
from app.classroom import db as cdb

REPO_ROOT = Path(__file__).resolve().parents[1]
DIST = REPO_ROOT / "web" / "editor"
EDITOR_ROOT = REPO_ROOT / "editor"
CANVAS_JS = REPO_ROOT / "web" / "js" / "components" / "classroomCanvas.js"
CLASSROOM_JS = REPO_ROOT / "web" / "js" / "pages" / "classroom.js"
PROTOCOL_TS = EDITOR_ROOT / "src" / "protocol.ts"
APP_TSX = EDITOR_ROOT / "src" / "App.tsx"
VENDOR = EDITOR_ROOT / "vendor" / "openmaic"

UPSTREAM_COMMIT = "18b68aea3a87d7626e805b136d9b908eb277b00a"
CANVAS_MESSAGE_NAMES = {
    "ready": "zhixu-slide-ready",
    "saved": "zhixu-slide-saved",
    "dirty": "zhixu-slide-dirty",
    "close": "zhixu-slide-close",
}

NODE = shutil.which("node")


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------

def index_html() -> str:
    return (DIST / "index.html").read_text("utf-8")


def asset_refs(html: str) -> list[str]:
    refs = re.findall(r'<(?:script|link)\b[^>]*?\b(?:src|href)="([^"]+)"', html)
    return [ref for ref in refs]


def dist_assets() -> list[Path]:
    return sorted((DIST / "assets").glob("*")) if (DIST / "assets").is_dir() else []


def user_id_of(client):
    return client.get("/api/auth/me").json()["id"]


def create_course(client, **overrides):
    payload = {"topic": "光合作用", "subject": "生物", "grade": "初中二年级"}
    payload.update(overrides)
    response = client.post("/api/classroom/courses", json=payload)
    assert response.status_code == 200, response.text
    return response.json()["course"]


def insert_scene(course_id, user_id, scene_type, order_index, title, content):
    now = db.utcnow()
    with db.connection() as con:
        return con.execute(
            "INSERT INTO cls_scenes(course_id,user_id,scene_type,order_index,title,content_json,meta_json,status,"
            "created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (course_id, user_id, scene_type, order_index, title, cdb.json_dump(content),
             cdb.json_dump({}), "ready", now, now),
        ).lastrowid


def slide_content(*elements):
    return {
        "type": "slide",
        "schemaVersion": 1,
        "canvas": {
            "width": 1000,
            "height": 562.5,
            "theme": {
                "backgroundColor": "#ffffff",
                "themeColors": ["#1f6feb", "#e8590c"],
                "fontColor": "#222222",
                "fontName": "Microsoft YaHei",
            },
            "elements": list(elements),
        },
    }


def text_element(element_id="text_001", content="<p>自由画布</p>", **geometry):
    element = {
        "id": element_id,
        "type": "text",
        "left": 60,
        "top": 50,
        "width": 880,
        "height": 76,
        "rotate": 0,
        "content": content,
        "defaultFontName": "",
        "defaultColor": "#333333",
    }
    element.update(geometry)
    return element


def shape_element(element_id="shape_001", **geometry):
    element = {
        "id": element_id,
        "type": "shape",
        "left": 120,
        "top": 140,
        "width": 260,
        "height": 160,
        "rotate": 15,
        "path": "M 0 0 L 1 0 L 1 1 L 0 1 Z",
        "viewBox": [1, 1],
        "fill": "#1f6feb",
        "fixedRatio": False,
    }
    element.update(geometry)
    return element


def latex_element(element_id="latex_001", latex=r"6CO_2 + 6H_2O \rightarrow C_6H_{12}O_6 + 6O_2"):
    return {
        "id": element_id,
        "type": "latex",
        "left": 660,
        "top": 250,
        "width": 240,
        "height": 80,
        "rotate": 0,
        "latex": latex,
        # 渲染器需要的 KaTeX HTML：后端 schema 白名单不持久化它（保存时应被丢弃）。
        "html": '<span class="katex">…</span>',
        "color": "#1f6feb",
        "align": "center",
    }


def content_url(course_id, scene_id):
    return "/api/classroom/courses/%d/scenes/%d/content" % (course_id, scene_id)


def make_slide_scene(client):
    course = create_course(client)
    scene_id = insert_scene(
        course["id"], user_id_of(client), "slide", 1, "自由画布验收", slide_content(text_element())
    )
    return course, scene_id


# ---------------------------------------------------------------------------
# 1. 构建产物
# ---------------------------------------------------------------------------

def test_build_artifacts_exist_and_are_self_contained():
    assert (DIST / "index.html").is_file(), "缺少 web/editor/index.html（请先 cd editor && pnpm run build）"
    assets = dist_assets()
    assert any(path.suffix == ".js" for path in assets), "构建产物缺少 JS"
    assert any(path.suffix == ".css" for path in assets), "构建产物缺少 CSS"
    total = sum(path.stat().st_size for path in DIST.rglob("*") if path.is_file())
    assert total > 0
    # 离线部署体积守门：构建产物（含 KaTeX 字体）应远小于 8MB。
    assert total < 8 * 1024 * 1024, "构建产物超过 8MB：%d 字节" % total


def test_index_asset_urls_are_relative_and_resolve():
    html = index_html()
    refs = asset_refs(html)
    assert refs, "index.html 没有引用任何脚本/样式"
    for ref in refs:
        assert ref.startswith("./"), "资源引用必须相对（base './'）：%s" % ref
        assert not ref.startswith("//")
        target = (DIST / ref).resolve()
        assert target.is_file(), "index.html 引用的资源不存在：%s" % ref
        assert DIST.resolve() in target.parents, "资源越出 web/editor：%s" % ref


def test_build_has_no_external_resource_references():
    """运行时不得依赖外网/CDN：HTML 不许有外链 src/href，CSS 不许 url(http...)，JS 不许 import/http 加载。"""
    html = index_html()
    for match in re.findall(r'(?:src|href)="([^"]+)"', html):
        assert not re.match(r"(?i)^(?:https?:)?//", match), "index.html 存在外链资源：%s" % match

    for path in dist_assets():
        text = path.read_text("utf-8", errors="replace")
        if path.suffix == ".css":
            assert not re.search(r"url\(\s*['\"]?https?:", text, re.I), "%s 引用外网资源" % path.name
            assert not re.search(r"@import\s+(?:url\()?['\"]?https?:", text, re.I), "%s 导入外网样式" % path.name
        else:
            for pattern in (
                r"""import\s*\(\s*['"]https?:""",
                r"""from\s*['"]https?:""",
                r"""fetch\s*\(\s*['"]https?:""",
                r"""src\s*=\s*['"]https?:""",
            ):
                assert not re.search(pattern, text), "%s 存在外网资源加载：%s" % (path.name, pattern)
    # 说明：JS/CSS 中的 w3.org 命名空间、react.dev 错误说明链接、许可注释不属于运行时资源加载。


def test_editor_project_and_vendored_license_metadata():
    package = json.loads((EDITOR_ROOT / "package.json").read_text("utf-8"))
    assert package["scripts"]["build"] == "vite build"
    assert (EDITOR_ROOT / "vite.config.ts").is_file()
    manifest_path = VENDOR / "VENDORED.json"
    assert manifest_path.is_file(), "缺少 vendored 上游元数据"
    manifest = json.loads(manifest_path.read_text("utf-8"))
    assert manifest["commit"] == UPSTREAM_COMMIT
    assert "MIT" in manifest["license"]
    assert "THU-MAIC" in manifest["copyright"]
    for name in ("dsl", "editor", "renderer", "geometry"):
        assert (VENDOR / name / "LICENSE").is_file(), "vendored %s 缺少 LICENSE" % name
        assert (VENDOR / name / "src").is_dir(), "vendored %s 缺少 src" % name
    readme = (EDITOR_ROOT / "README.md").read_text("utf-8")
    for needle in ("pnpm run build", "MIT", UPSTREAM_COMMIT, "web/editor"):
        assert needle in readme, "editor/README.md 缺少关键信息：%s" % needle


# ---------------------------------------------------------------------------
# 2. postMessage 协议与父页集成
# ---------------------------------------------------------------------------

def test_postmessage_protocol_constants_match():
    parent = CANVAS_JS.read_text("utf-8")
    protocol = PROTOCOL_TS.read_text("utf-8")
    for name, value in CANVAS_MESSAGE_NAMES.items():
        assert value in parent, "父页组件缺少协议常量 %s" % value
        assert value in protocol, "编辑器协议缺少消息类型 %s" % value
    # 父页必须按来源与窗口校验消息（避免任意窗口伪造保存事件）。
    assert "event.source" in parent
    assert "event.origin" in parent
    # srcdoc 文档的 event.origin 为 "null"：父页需要显式允许已知 iframe 源。
    assert '"null"' in parent


def test_iframe_embedding_uses_srcdoc_because_of_platform_csp():
    """平台响应头是 frame-ancestors 'none'：父页读取 index.html 后以 srcdoc 挂载。"""
    parent = CANVAS_JS.read_text("utf-8")
    assert "CANVAS_EDITOR_PATH = '/static/editor/index.html'" in parent
    assert "srcdoc" in parent
    assert "absolutizeEditorHtml" in parent
    assert "frame-ancestors" in parent  # 注释里说明原因，防止后续误改回 src=
    # 参数通过 iframe name 传给 srcdoc 文档。
    assert "CANVAS_IFRAME_NAME_PREFIX" in parent
    editor_app = APP_TSX.read_text("utf-8")
    assert "window.name" in editor_app
    assert "zhixu-canvas:" in editor_app


def test_classroom_page_wires_canvas_entry():
    page = CLASSROOM_JS.read_text("utf-8")
    assert "classroomCanvas.js" in page
    assert "openCanvasEditor" in page
    assert "focusPlayerScene" in page
    assert "自由画布编辑" in page
    assert "renderRoute" in page
    # 仅幻灯片场景才注入入口（沿用 P6a 的 slideScenes 过滤）。
    assert "slideScenes" in page


@pytest.mark.skipif(NODE is None, reason="本机未安装 Node，跳过前端语法检查")
def test_canvas_frontend_syntax():
    for path in (CANVAS_JS, CLASSROOM_JS):
        assert path.is_file(), path
        completed = subprocess.run(
            [NODE, "--check", str(path)], capture_output=True, text=True, timeout=60
        )
        assert completed.returncode == 0, "%s\n%s" % (path, completed.stderr)


# ---------------------------------------------------------------------------
# 3. 保存契约：自由画布 content 往返
# ---------------------------------------------------------------------------

def test_get_content_returns_canvas_ready_for_editor(client):
    course, scene_id = make_slide_scene(client)
    body = client.get(content_url(course["id"], scene_id)).json()
    scene = body["scene"]
    content = scene["content"]
    assert scene["scene_type"] == "slide"
    assert scene["version"] == 1
    assert content["type"] == "slide"
    canvas = content["canvas"]
    assert canvas["width"] == 1000
    assert abs(canvas["height"] - 562.5) < 0.01
    assert canvas["theme"]["backgroundColor"] == "#ffffff"
    assert canvas["elements"][0]["id"] == "text_001"


def test_patch_saves_freedraw_canvas_content(client):
    """模拟编辑器 PATCH：拖拽后的坐标 + 旋转形状 + 公式（html 被后端丢弃）。"""
    course, scene_id = make_slide_scene(client)
    dragged = text_element(left=222.5, top=88.25, width=400)
    content = slide_content(dragged, shape_element(), latex_element())
    response = client.patch(content_url(course["id"], scene_id), json={"version": 1, "content": content})
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["scene"]["version"] == 2
    elements = {item["id"]: item for item in data["scene"]["content"]["canvas"]["elements"]}
    assert elements["text_001"]["left"] == 222.5
    assert elements["text_001"]["top"] == 88.25
    assert elements["shape_001"]["rotate"] == 15
    # 公式元素保留 latex 源码；不持久化渲染 HTML（编辑器载入时重算）。
    assert elements["latex_001"]["latex"].startswith("6CO_2")
    assert "html" not in elements["latex_001"]
    assert data["warnings"] == []

    # 播放器/导出读取同一 content_json，保存后立即可见。
    detail = client.get("/api/classroom/courses/%d" % course["id"]).json()
    scene = next(item for item in detail["scenes"] if item["id"] == scene_id)
    assert scene["content"]["canvas"]["elements"][0]["left"] == 222.5


def test_patch_canvas_content_is_version_cas(client):
    """编辑器 409 冲突处理依赖后端 CAS：旧 version 被拒绝并回传当前版本。"""
    course, scene_id = make_slide_scene(client)
    first = client.patch(
        content_url(course["id"], scene_id),
        json={"version": 1, "content": slide_content(text_element(left=100))},
    )
    assert first.status_code == 200, first.text
    assert first.json()["scene"]["version"] == 2
    stale = client.patch(
        content_url(course["id"], scene_id),
        json={"version": 1, "content": slide_content(text_element(left=300))},
    )
    assert stale.status_code == 409
    detail = stale.json()["detail"]
    assert isinstance(detail, dict) and detail["version"] == 2
    # 用服务端版本重试（「覆盖保存」）即可成功。
    retry = client.patch(
        content_url(course["id"], scene_id),
        json={"version": detail["version"], "content": slide_content(text_element(left=300))},
    )
    assert retry.status_code == 200, retry.text
    assert retry.json()["scene"]["version"] == 3


def test_patch_clamps_canvas_geometry_like_playback(client):
    """自由画布拖出游标越界时，后端按 DSL 规则钳制到画布内（播放器不会丢元素）。"""
    course, scene_id = make_slide_scene(client)
    content = slide_content(text_element(left=5000, top=-400, rotate=720))
    response = client.patch(content_url(course["id"], scene_id), json={"version": 1, "content": content})
    assert response.status_code == 200, response.text
    element = response.json()["scene"]["content"]["canvas"]["elements"][0]
    assert 0 <= element["left"] <= 1000 - element["width"]
    assert 0 <= element["top"] <= 562.5 - element["height"]
    assert -360 <= element["rotate"] <= 360


def test_patch_rejects_non_slide_scene_with_chinese_detail(client):
    course = create_course(client)
    quiz_id = insert_scene(
        course["id"],
        user_id_of(client),
        "quiz",
        1,
        "小测",
        {"type": "quiz", "questions": [
            {"id": "q1", "type": "single", "question": "光合作用场所？",
             "options": [{"label": "A", "value": "叶绿体"}], "answer": ["A"], "points": 5}
        ]},
    )
    response = client.patch(content_url(course["id"], quiz_id), json={"version": 1, "content": slide_content(text_element())})
    assert response.status_code == 422
    assert "幻灯片" in response.json()["detail"]


def test_canvas_editor_endpoints_require_login(client):
    from app.main import app

    course, scene_id = make_slide_scene(client)
    anon = TestClient(app)
    assert anon.get(content_url(course["id"], scene_id)).status_code == 401
    assert anon.patch(
        content_url(course["id"], scene_id), json={"version": 1, "content": slide_content(text_element())}
    ).status_code == 401
