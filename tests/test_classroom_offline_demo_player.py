"""M7 验收：课堂导出 HTML / ZIP 的离线可交互演示播放（方案 §5.4）。

覆盖：

1. 含演示场景的离线 HTML 内联播放器（flow / iso 渲染器 bundle + 离线挂载脚本），
   静态海报、``data-demo-frame-ready``、固定版本标记与内嵌演示 JSON 全部保留；
2. 播放器自包含安全口径：无 http(s) 外链 / 无 ``<script src`` / 无 ``eval`` / ``new Function`` /
   动态 import，附件数据里的 ``</script>`` 被转义、只作为 JSON 数据存在；
3. 支持矩阵：flow/isometric/factory_sim 挂载可交互视图；function_graph /
   whiteboard / mech_3d 保留海报（``data-offline-demo-error`` 中文提示），不误标 mounted；
4. 工厂仿真回放只用真实 metrics / events（指标卡 + 工位队列 + 事件时间轴，不依赖 three.js）；
5. 视频捕获模式（``body.capture``）隐藏演示播放控件；
6. 无演示场景的课程输出与既有结构一致（不含任何播放器标记 / 占位符）；
7. ZIP 含 ``offline.html``（同一内联播放器）与 ``demos/*.json``；无演示课程 ZIP 不新增文件；
8. print / PDF 入口仍只输出静态海报 + 文字（不出现播放器）；
9. bundle 构建可复现、``--check`` 通过、体积 ≤400KB，``node --check`` 通过（node 不可用时跳过）。

全部离线：不调用模型与网络，导出走 ``client`` 夹具（临时数据目录）。
"""
from __future__ import annotations

import importlib.util
import json
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from app.classroom import exports
from app.classroom import db as cdb

ROOT = Path(__file__).resolve().parents[1]
BUNDLE_PATH = ROOT / "web" / "vendor" / "offline" / "offline-demo.js"
PLAYER_PATH = ROOT / "web" / "js" / "offline" / "offlineDemo.js"
BUILD_SCRIPT = ROOT / "scripts" / "build_offline_demo_bundle.py"
MAX_BUNDLE_BYTES = 400 * 1024

# 1×1 透明 PNG（与既有演示场景用例相同的合法最小海报）。
TINY_PNG = (
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)

FLOW_DATA = {
    "layout": "flow",
    "style": "clean",
    "nodes": [
        {"id": "n1", "label": "起点", "kind": "start"},
        {"id": "n2", "label": "中间步骤", "kind": "process"},
        {"id": "n3", "label": "终点", "kind": "end"},
    ],
    "edges": [{"from": "n1", "to": "n2"}, {"from": "n2", "to": "n3"}],
    "steps": [
        {"label": "第一步", "node_ids": ["n1"]},
        {"label": "第二步", "node_ids": ["n2", "n3"]},
    ],
}

ISO_DATA = {
    "style": "clean",
    "parts": [
        {"id": "p1", "label": "底座", "shape": "plate", "position": [0, 0, 0], "size": [3, 0.3, 2]},
        {"id": "p2", "label": "立柱", "shape": "box", "position": [0, 1.2, 0], "size": [0.4, 2, 0.4]},
    ],
    "labels": [{"part_id": "p2", "text": "立柱"}],
    "steps": [
        {"label": "整体", "part_ids": []},
        {"label": "拆开", "part_ids": ["p2"], "explode": 0.6},
    ],
}

FACTORY_DATA = {
    "version": 1,
    "basis": "deterministic-des",
    "seed": 42,
    "flow": {
        "nodes": [
            {"id": "s0", "label": "工位 1", "kind": "station"},
            {"id": "s1", "label": "工位 2", "kind": "station"},
            {"id": "s2", "label": "工位 3", "kind": "station"},
        ],
        "edges": [{"from": "s0", "to": "s1"}, {"from": "s1", "to": "s2"}],
    },
    "station_labels": ["工位 1", "工位 2", "工位 3"],
    "metrics": {
        "sim_seconds": 180,
        "arrivals": 22,
        "completed": 18,
        "throughput_per_min": 6.0,
        "avg_wait_s": [1.2, 3.4, 5.6],
        "max_queue": [1, 2, 3],
        "utilization": [0.5, 0.6, 0.7],
        "bottleneck_station": 2,
        "bottleneck_reason": "工位 3 平均等待最高",
    },
    "events": [
        {"t": 1.0, "type": "arrival", "station": 0, "item_id": 1, "queue": 0},
        {"t": 2.0, "type": "service_start", "station": 0, "item_id": 1, "queue": 0},
        {"t": 4.0, "type": "service_end", "station": 0, "item_id": 1, "queue": 1},
        {"t": 4.5, "type": "arrival", "station": 1, "item_id": 1, "queue": 1},
        {"t": 6.0, "type": "service_end", "station": 1, "item_id": 1, "queue": 2},
        {"t": 8.0, "type": "service_end", "station": 2, "item_id": 1, "queue": 3},
    ],
}

MECH_DATA = {
    "version": 1,
    "basis": "local-teaching",
    "seed": 7,
    "parts": [
        {"id": "p1", "label": "箱体", "kind": "box", "position": [0, 0, 0], "size": [2, 1.2, 1.4]},
    ],
    "steps": [{"label": "拆下箱盖", "part_ids": ["p1"], "explode": 0.4}],
}


# ---------------------------------------------------------------------------
# 辅助（与 tests/test_classroom_demo_scenes.py 同口径）
# ---------------------------------------------------------------------------

def user_id_of(client):
    return client.get("/api/auth/me").json()["id"]


def create_course(client, **overrides):
    payload = {"topic": "M7 离线播放课堂", "subject": "物理", "grade": "高中"}
    payload.update(overrides)
    response = client.post("/api/classroom/courses", json=payload)
    assert response.status_code == 200, response.text
    return response.json()["course"]


def create_artifact(client, *, kind="flow_animation", title="演示附件", data=None,
                    preview=None):
    payload = {"kind": kind, "title": title, "data": data if data is not None else FLOW_DATA}
    if preview is not None:
        payload["preview"] = {"png": preview} if isinstance(preview, str) else preview
    response = client.post("/api/artifacts", json=payload)
    assert response.status_code == 201, response.text
    body = response.json()
    return {
        "artifact_id": body["artifact"]["artifact_id"],
        "revision": body["revision"]["revision"],
    }


def insert_scene(course_id, user_id, scene_type, order_index, title, content):
    from app import db

    now = db.utcnow()
    with db.connection() as con:
        return con.execute(
            "INSERT INTO cls_scenes(course_id,user_id,scene_type,order_index,title,content_json,"
            "meta_json,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (course_id, user_id, scene_type, order_index, title, cdb.json_dump(content),
             cdb.json_dump({}), "ready", now, now),
        ).lastrowid


def attach_demo(client, course_id, artifact, **payload):
    body = {"artifact_id": artifact["artifact_id"], **payload}
    response = client.post(f"/api/classroom/courses/{course_id}/scenes/demo", json=body)
    assert response.status_code in (200, 201), response.text
    return response.json()["scene"]


def course_with_demo(client, artifact, *, title="M7 离线播放课堂"):
    course = create_course(client, topic=title)
    attach_demo(client, course["id"], artifact)
    return course["id"]


def export_html(client, course_id):
    response = client.get(f"/api/classroom/courses/{course_id}/export", params={"format": "html"})
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/html")
    return response.text


def _offline_snapshot(html: str) -> dict:
    raw = html.split('<script id="course-data" type="application/json">', 1)[1]
    raw = raw.split("</script>", 1)[0]
    return json.loads(raw.replace("<\\/", "</"))


def _appendix_payload(html: str) -> dict:
    appendix = html.split('<section class="cls-demo-appendix">', 1)[1]
    raw = appendix.split('<script type="application/json" class="cls-demo-data">', 1)[1]
    raw = raw.split("</script>", 1)[0]
    return json.loads(raw.replace("<\\/", "</"))


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


def _assert_self_contained(html: str) -> None:
    """离线 HTML 必须零外链、零脚本执行附件数据。"""
    assert _external_reference_problems(html) == []
    assert "new Function" not in html
    assert re.search(r"\beval\s*\(", html) is None
    assert "import(" not in html
    assert "export default" not in html
    # 模板占位符必须全部替换干净。
    for placeholder in ("__DATA__", "__JS__", "__CSS__", "__DEMOS__", "__SOURCES__",
                        "__OFFLINE_DEMO", "__DEMO_META__"):
        assert placeholder not in html, placeholder


def _bundle_module():
    spec = importlib.util.spec_from_file_location("m7_build_offline_demo_bundle", BUILD_SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# 1. 含演示场景的离线 HTML：内联播放器 + 静态海报共存
# ---------------------------------------------------------------------------

def test_flow_demo_offline_html_embeds_interactive_player(client):
    artifact = create_artifact(client, kind="flow_animation", title="流程回放",
                               data=FLOW_DATA, preview=TINY_PNG)
    course_id = course_with_demo(client, artifact)
    html = export_html(client, course_id)

    # 内联播放器 bundle + 挂载脚本（固定版本号便于第三方核对）
    assert "ZhixuOfflineDemo" in html
    assert "ZhixuOfflineDemoInit" in html
    assert 'OFFLINE_BUNDLE_VERSION: "1"' in html
    assert "createFlowView" in html and "createIsoView" in html
    # flow/iso 渲染器依赖的 .pd-* 样式随播放器一并内联（离线单文件不加载全局样式表）。
    assert ".pd-flow-node" in html and ".pd-iso-label" in html
    assert "--accent-deep:" in html
    # 演示 section 静态标记 + 运行时标记（mounted 由脚本挂载成功后才写入 DOM）。
    assert 'data-offline-demo="flow_animation"' in html
    assert "data-offline-demo-controls" in html
    assert 'data-offline-demo-mounted="1"' not in html

    # 既有结构保留：静态海报、固定版本、帧就绪、业务 JS 的 renderDemo。
    assert 'class="cls-demo-scene"' in html
    assert 'data-fixed-revision="1"' in html
    assert f'data-artifact-id="{artifact["artifact_id"]}"' in html
    assert "data:image/png;base64," in html
    assert 'data-demo-frame-ready="1"' in html
    assert "zhixu-demo-frame-ready" in html
    assert "renderDemo" in html
    assert "__zhixuCapture" in html and "sceneCount" in html

    # 内嵌演示 JSON（播放器数据源）与静态附录固定版本快照都可解析。
    snapshot = _offline_snapshot(html)
    demo_scene = next(scene for scene in snapshot["scenes"] if scene["type"] == "demo")
    assert demo_scene["content"]["fixed"] is True
    assert demo_scene["content"]["artifact"]["artifact_id"] == artifact["artifact_id"]
    assert demo_scene["content"]["artifact"]["data"] == FLOW_DATA
    appendix = _appendix_payload(html)
    assert appendix["format"] == "zhixu-classroom-demo-snapshot"
    assert appendix["fixed"] is True
    assert appendix["artifact"]["kind"] == "flow_animation"

    _assert_self_contained(html)


def test_isometric_demo_offline_html_uses_iso_renderer(client):
    artifact = create_artifact(client, kind="isometric", title="等距回放", data=ISO_DATA)
    course_id = course_with_demo(client, artifact)
    html = export_html(client, course_id)

    assert "ZhixuOfflineDemo" in html
    assert "createIsoView" in html
    # 视频捕获模式使用 staticMode（确定性首帧），正常播放仍可拆解/装配分步。
    assert "staticMode" in html
    assert 'data-offline-demo="isometric"' in html
    assert 'data-offline-demo-mounted="1"' not in html
    assert 'data-fixed-revision="1"' in html
    assert "等距结构" in html
    _assert_self_contained(html)


# ---------------------------------------------------------------------------
# 2. 工厂仿真：轻量回放只用真实 metrics / events
# ---------------------------------------------------------------------------

def test_factory_demo_offline_html_has_lightweight_replay(client):
    artifact = create_artifact(client, kind="factory_sim", title="工厂回放", data=FACTORY_DATA)
    course_id = course_with_demo(client, artifact)
    html = export_html(client, course_id)

    assert "ZhixuOfflineDemo" in html
    assert 'data-offline-demo="factory_sim"' in html
    # 指标卡 / 工位队列 / 事件时间轴 / 控件容器。
    assert "offline-demo-metrics" in html
    assert "offline-demo-flow-svg" in html
    assert "offline-demo-line" in html
    assert "offline-demo-timeline" in html
    assert "data-offline-demo-controls" in html
    assert "bottleneck" in html and "工位 3 平均等待最高" in html
    assert "吞吐" in html and "完成 / 到达" in html
    # 附件快照里的事件序列完整保留（真实数据驱动回放，不伪造读数）。
    appendix = _appendix_payload(html)
    assert appendix["artifact"]["data"]["events"] == FACTORY_DATA["events"]
    assert appendix["artifact"]["data"]["metrics"] == FACTORY_DATA["metrics"]
    _assert_self_contained(html)


# ---------------------------------------------------------------------------
# 3. 安全自包含：附件数据只作 JSON，不执行；数据里的 </script> 被转义
# ---------------------------------------------------------------------------

def test_offline_player_never_executes_artifact_data(client):
    malicious = "</script><script>alert(1)</script>"
    data = {
        "nodes": [
            {"id": "n1", "label": malicious, "kind": "start"},
            {"id": "n2", "label": "正常节点", "kind": "end"},
        ],
        "edges": [{"from": "n1", "to": "n2"}],
        "steps": [{"label": malicious}],
    }
    artifact = create_artifact(client, kind="flow_animation", title="注入测试", data=data)
    course_id = course_with_demo(client, artifact)
    html = export_html(client, course_id)

    # 数据里的 </script> 必须转义，不会提前闭合 JSON script、更不会被当成可执行脚本。
    assert "</script><script>alert(1)" not in html
    assert "alert(1)</script>" not in html
    assert "<\\/script><script>alert(1)" in html
    # 转义后数据仍可被 JSON 解析且内容原样保留（只有数据语义，没有执行语义）。
    snapshot = _offline_snapshot(html)
    demo_scene = next(scene for scene in snapshot["scenes"] if scene["type"] == "demo")
    assert demo_scene["content"]["artifact"]["data"]["nodes"][0]["label"] == malicious
    appendix = _appendix_payload(html)
    assert appendix["artifact"]["data"]["nodes"][0]["label"] == malicious
    _assert_self_contained(html)


# ---------------------------------------------------------------------------
# 4. 支持矩阵：flow / iso / factory 交互；其余 kind 仅海报
# ---------------------------------------------------------------------------

def test_player_kind_support_matrix_in_inlined_source(client):
    artifact = create_artifact(client, kind="flow_animation", title="支持矩阵", data=FLOW_DATA)
    course_id = course_with_demo(client, artifact)
    html = export_html(client, course_id)

    # 三个可交互分支：flow（含 science_demo / demo_3d 的 flow 形状兜底）、iso、factory。
    for marker in (
        'kind === "flow_animation"',
        'kind === "science_demo"',
        'kind === "demo_3d"',
        'kind === "isometric"',
        'kind === "factory_sim"',
    ):
        assert marker in html, marker
    # 仅海报 kind 清单（离线单文件不引入函数图/白板/机械 3D 的复杂运行时）。
    poster_only = re.search(r"POSTER_ONLY_KINDS\s*=\s*\{(.*?)\};", html, re.S)
    assert poster_only, "缺少 POSTER_ONLY_KINDS 清单"
    body = poster_only.group(1)
    for kind in ("function_graph", "whiteboard", "mech_3d", "molecule_3d", "physics_sandbox", "geometry_demo"):
        assert kind in body, kind
    for kind in ("flow_animation", "isometric", "factory_sim"):
        assert kind not in body, kind
    # 明确注释「离线 HTML 仅海报」。
    assert "离线 HTML 仅海报" in html


def test_unsupported_mech3d_scene_keeps_poster_without_mounted_marker(client):
    artifact = create_artifact(client, kind="mech_3d", title="机械演示",
                               data=MECH_DATA, preview=TINY_PNG)
    course_id = course_with_demo(client, artifact)
    html = export_html(client, course_id)

    # 播放器仍内联（同页可能有其它可交互演示），但 mech_3d 只保留固定版本海报。
    assert "ZhixuOfflineDemo" in html
    assert 'data-offline-demo="mech_3d"' in html
    assert 'data-offline-demo-mounted="1"' not in html
    assert "data-offline-demo-error" in html
    assert "仅保留固定版本海报" in html
    # 海报 / 固定版本 / 帧就绪标记仍在，视频与 PDF 路径不受影响。
    assert "data:image/png;base64," in html
    assert 'data-demo-frame-ready="1"' in html
    assert "机械 3D" in html
    assert f'data-artifact-id="{artifact["artifact_id"]}"' in html
    _assert_self_contained(html)


def test_demo_without_renderable_data_falls_back_to_poster(client):
    """数据形状不匹配（science_demo 非 flow / factory 缺 metrics+events）时保留海报并中文提示。"""
    science = create_artifact(client, kind="science_demo", title="科学演示",
                              data={"assumptions": ["理想条件"]})
    factory = create_artifact(client, kind="factory_sim", title="空工厂",
                              data={"version": 1, "basis": "deterministic-des"})
    course = create_course(client, topic="M7 数据缺失回退")
    attach_demo(client, course["id"], science)
    attach_demo(client, course["id"], factory)
    html = export_html(client, course["id"])

    # 播放器代码里保留了「仅海报 / 中文提示」的降级分支（运行时按真实数据决定是否挂载）。
    assert "数据不是流程结构" in html
    assert "缺少真实指标与事件数据" in html
    assert "data-offline-demo-error" in html
    # 两个 demo 的静态海报结构都还在，固定版本与帧就绪标记不丢。
    assert html.count('class="cls-demo-scene"') >= 2
    assert 'data-offline-demo="science_demo"' in html
    assert 'data-offline-demo="factory_sim"' in html
    assert 'data-demo-frame-ready="1"' in html
    assert 'data-offline-demo-mounted="1"' not in html
    _assert_self_contained(html)


# ---------------------------------------------------------------------------
# 5. 视频捕获模式隐藏演示播放控件
# ---------------------------------------------------------------------------

def test_capture_mode_css_hides_offline_demo_controls(client):
    artifact = create_artifact(client, kind="factory_sim", title="捕获测试", data=FACTORY_DATA)
    course_id = course_with_demo(client, artifact)
    html = export_html(client, course_id)

    rule = re.search(
        r"body\.capture\s+\[data-offline-demo-controls\]\s*\{[^}]*display:\s*none\s*!important",
        html,
    )
    assert rule, "捕获模式必须隐藏 data-offline-demo-controls"
    # 控件容器只由播放器脚本创建，且离线播放器在捕获模式下用静态首帧（无补间）。
    assert 'setAttribute("data-offline-demo-controls"' in html
    assert "isCaptureMode" in html
    assert "staticMode: isCaptureMode()" in html


# ---------------------------------------------------------------------------
# 6. 无演示场景：输出与既有结构一致（不含任何播放器痕迹）
# ---------------------------------------------------------------------------

def test_course_without_demo_keeps_offline_html_unchanged(client):
    course = create_course(client, topic="M7 无演示课堂")
    user_id = user_id_of(client)
    insert_scene(course["id"], user_id, "slide", 1, "普通幻灯片", {
        "type": "slide",
        "canvas": {"width": 1000, "height": 562.5, "theme": {}, "elements": [
            {"id": "t1", "type": "text", "left": 60, "top": 60, "width": 800, "height": 80,
             "content": "<p>普通内容</p>", "text": "普通内容", "fontSize": 20},
        ]},
    })
    html = export_html(client, course["id"])

    for marker in (
        "ZhixuOfflineDemo", "data-offline-demo", "offline-demo-",
        "OFFLINE_BUNDLE_VERSION", "ZhixuOfflineDemoInit", ".pd-flow-node",
    ):
        assert marker not in html, marker
    for placeholder in ("__OFFLINE_DEMO", "__DEMOS__", "__DATA__", "__JS__"):
        assert placeholder not in html, placeholder
    # 既有结构：课程 JSON + 业务 JS 两段脚本，静态海报之外没有新增脚本/样式块。
    assert html.count("<script") == 2
    assert 'id="course-data"' in html
    assert "renderDemo" in html
    assert "普通内容" in html
    assert "http://" not in html and "https://" not in html


# ---------------------------------------------------------------------------
# 7. ZIP：演示课程携带 offline.html + demos/*.json；无演示课程结构不变
# ---------------------------------------------------------------------------

def test_zip_contains_offline_player_and_demo_snapshots(client):
    artifact = create_artifact(client, kind="flow_animation", title="ZIP 流程回放",
                               data=FLOW_DATA, preview=TINY_PNG)
    course_id = course_with_demo(client, artifact)
    response = client.get(f"/api/classroom/courses/{course_id}/export", params={"format": "zip"})
    assert response.status_code == 200, response.text

    import io

    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        names = archive.namelist()
        assert "offline.html" in names
        assert any(name.startswith("demos/") for name in names)
        offline_html = archive.read("offline.html").decode("utf-8")
        assert "ZhixuOfflineDemo" in offline_html
        assert 'data-offline-demo="flow_animation"' in offline_html
        assert "data-offline-demo-controls" in offline_html
        assert 'data-fixed-revision="1"' in offline_html
        _assert_self_contained(offline_html)
        manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
        assert manifest["offlineHtml"] == "offline.html"
        assert len(manifest["demoScenes"]) == 1
        assert "demo-scenes" in manifest["capabilities"]

    # 无演示课程：ZIP 结构与既有完全一致（不新增 offline.html / manifest 字段）。
    plain = create_course(client, topic="M7 无演示 ZIP")
    user_id = user_id_of(client)
    insert_scene(plain["id"], user_id, "slide", 1, "普通页", {
        "type": "slide",
        "canvas": {"width": 1000, "height": 562.5, "theme": {}, "elements": []},
    })
    plain_response = client.get(
        f"/api/classroom/courses/{plain['id']}/export", params={"format": "zip"},
    )
    assert plain_response.status_code == 200, plain_response.text
    with zipfile.ZipFile(io.BytesIO(plain_response.content)) as archive:
        assert "offline.html" not in archive.namelist()
        manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
        assert "offlineHtml" not in manifest
        assert "demoScenes" not in manifest


# ---------------------------------------------------------------------------
# 8. print / PDF：仍只输出静态海报 + 文字
# ---------------------------------------------------------------------------

def test_print_and_pdf_keep_static_poster_only(client):
    artifact = create_artifact(client, kind="flow_animation", title="打印演示",
                               data=FLOW_DATA, preview=TINY_PNG)
    course_id = course_with_demo(client, artifact)
    user_id = user_id_of(client)
    course = cdb.find_course(course_id, user_id)
    scenes = cdb.course_scenes(course_id, user_id)
    print_html = exports.build_print_html(course, scenes)

    assert 'class="cls-demo-scene"' in print_html
    assert 'data-fixed-revision="1"' in print_html
    assert "固定版本 v1" in print_html
    assert "data:image/png;base64," in print_html
    for marker in ("ZhixuOfflineDemo", "data-offline-demo", "offline-demo-",
                   "data-offline-demo-controls"):
        assert marker not in print_html, marker

    demo_content = next(
        cdb.json_load(scene["content_json"], {})
        for scene in scenes if scene["scene_type"] == "demo"
    )
    body = exports._print_demo_body(demo_content, "打印演示")
    assert 'class="cls-demo-scene"' in body
    assert "data-offline-demo" not in body
    assert "ZhixuOfflineDemo" not in body
    # 共享的静态片段默认（offline_player=False）不出现任何播放器标记，打印/PDF 结构不变。
    appendix_static = exports._demo_sections_html({
        "scenes": [{"type": "demo", "title": "打印演示", "content": demo_content}],
    })
    assert 'class="cls-demo-scene"' in appendix_static
    assert "data-offline-demo" not in appendix_static
    assert "ZhixuOfflineDemo" not in appendix_static

    # 服务端 PDF：渲染引擎可用则返回 PDF，不可用时按既有口径 503（不因播放器 500）。
    pdf_response = client.get(
        f"/api/classroom/courses/{course_id}/export", params={"format": "pdf"},
    )
    assert pdf_response.status_code in (200, 503)
    if pdf_response.status_code == 200:
        assert pdf_response.content[:4] == b"%PDF"


# ---------------------------------------------------------------------------
# 9. bundle：确定性构建、--check、体积与语法
# ---------------------------------------------------------------------------

def test_offline_demo_bundle_build_check_passes():
    assert BUNDLE_PATH.is_file(), "缺少 web/vendor/offline/offline-demo.js（请运行构建脚本）"
    result = subprocess.run(
        [sys.executable, "-X", "utf8", str(BUILD_SCRIPT), "--check"],
        cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=120,
    )
    assert result.returncode == 0, result.stderr
    assert "一致" in result.stdout

    node = shutil.which("node")
    if node:
        for path in (BUNDLE_PATH, PLAYER_PATH):
            checked = subprocess.run(
                [node, "--check", str(path)], capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=120,
            )
            assert checked.returncode == 0, "%s node --check 失败：%s" % (path.name, checked.stderr)
    else:  # pragma: no cover - 本机有 node；无 node 的验收环境跳过语法检查
        pytest.skip("node 不可用，跳过 node --check")


def test_offline_demo_bundle_is_deterministic_and_bounded():
    module = _bundle_module()
    first = module.build_bundle()
    second = module.build_bundle()
    assert first == second, "bundle 构建必须同输入同字节"
    assert "\r\n" not in first, "bundle 必须使用 LF"
    assert first.endswith("\n"), "bundle 必须以换行结尾"
    payload = first.encode("utf-8")
    assert len(payload) <= MAX_BUNDLE_BYTES, "bundle 体积应 ≤400KB"
    assert BUNDLE_PATH.read_text(encoding="utf-8").replace("\r\n", "\n") == first
    # 源模块已去掉 ESM 语法，且两个模块的同名私有符号不会串作用域（各自 IIFE）。
    assert "export function" not in first
    assert "export const" not in first
    assert "__zhixuFlow" in first and "__zhixuIso" in first
    for key in ("createFlowView", "renderFlowPoster", "createIsoView",
                "renderIsoPoster", "OFFLINE_BUNDLE_VERSION"):
        assert key in first
    assert "http://" not in first and "https://" not in first
    assert "</" not in first
