"""开源组件 vendor 资产自检（批次二起，后续演示库复用）。

覆盖 MathLive、3Dmol / OpenChemLib、Matter.js、JSXGraph、markmap 与 vis-timeline 等
随仓库分发的第三方资产：
* 主文件、样式、字体与 LICENSE 存在；
* README.md 里的版本、大小与 SHA-256 与磁盘一致；
* 不引入按键音效、字符串求值模块等非必要资源；
* 预设纪律与组件接线（JSXGraph / markmap / vis-timeline）。
"""
from pathlib import Path
import hashlib
import re

import pytest

ROOT = Path(__file__).resolve().parents[1]
VENDOR = ROOT / "web" / "vendor"

ASSETS = {
    "mathlive": {
        "version": "0.111.4",
        "files": {
            "mathlive.min.js": "9967efce1bf98421748a8f3541739d700bf1c0beb6eb7f4c0765bd512ba65388",
            "mathlive-static.css": "c9698f6b0c60482e195020660f7074aa65cf049a1b1160a8729294c21c4bc868",
            "LICENSE.txt": "3caebd18611959ca725ab976a004d408017eb1d7093d6c9b9b09c5a4790eaff8",
        },
    },
    "3dmol": {
        "version": "3.0.0",
        "files": {
            "3Dmol-min.js": "a886e24944310cea8111915209efb2c1246e327858a898ad56ac90fccc314053",
            "LICENSE": "4c6eaaed856f3f28a3b1a98e74f4a8a71618de7d51ea4155c29f6f793bcef861",
        },
    },
    "openchemlib": {
        "version": "9.26.0",
        "files": {
            "openchemlib.js": "2d7fb693bc4ee83dbc8d3a2fd3cd7edcc0195ac2af3b5daac948b4f99cede4b3",
            "resources.json": "bd976efcd5ea199cb5d5ed847a6f44431cd95a0940b0df8151cb7131325edb53",
            "LICENSE": "38dc3aed3def8cc4dd15ac879daa4af9b0d71af86fef82611ca1752497c6f464",
        },
    },
    "matter": {
        "version": "0.20.0",
        "files": {
            "matter.min.js": "72d30be0f579eb02ce1e0b6f9d359a4f392e6837e5a26ba8be5dbee7f88e24ae",
            "LICENSE": "ed182087be5b26734aa6d4789743de3a97417950e8c1e3ff2e3d19c6462720d3",
        },
    },
    "jsxgraph": {
        "version": "1.14.3",
        "files": {
            "jsxgraphcore.js": "3b256219e90c83e183f253374ffcecab0cfbb1350a7b4252a69af134a684ee43",
            "jsxgraph.css": "c82ee298a077701100fd99e85374b7e572ebe483fc287fd13cf72a6e5f86d64c",
            "LICENSE.MIT": "43e325001487de7f09745fbe086b054bb1b70d4ab58060e87153849365b23ff1",
        },
    },
    "markmap": {
        "version": "0.18.13",
        "files": {
            "markmap-view.iife.min.js": "cd44f6fb7316bffa5b5782c6c76aaf0baee90cb9d4ac16a463572b2ff4366095",
            "LICENSE": "3f92393bdf2d11859119785b6cd442fa6fcfebe01f67c72c79997ef52a1f1df7",
        },
    },
    "vis-timeline": {
        "version": "8.5.6",
        "files": {
            "vis-timeline-graph2d.min.js": "01e838f31f1aaef88d686915b5a3f1453ba970270155157daed5fe6063a25889",
            "vis-timeline-graph2d.min.css": "43689b3d6a6342da896e7ea9d78873d8d370d7b9805850f7e9508e918dde5a8a",
            "LICENSE.Apache-2.0.txt": "1509904280a272780fd69c04b858f512cf216f8e3464a0dd98d8f34416feced9",
            "LICENSE.MIT.txt": "507e7e91e0a385c9de0464889068fada0620e327d23f1d64605a7d046490ec97",
        },
    },
    "graph": {
        "version": "4.0.6",
        "files": {
            "sigma.min.js": "4c7a2b7e717393717ffac2dd8736e92d77aeafae39ac40a9c4a83c5f78d1d4d0",
            "graphology.umd.min.js": "dc337efa23903f61e064c8e7e7f93a429e6855dccfc2458802b4ed30c621c087",
            "SIGMA-LICENSE.txt": "7036bbeb954c1220102cced5a608198a54f4a19b2d81d96f67997dbb9d7a51f1",
            "GRAPHOLOGY-LICENSE.txt": "9d396b4882c329077f32861c0d6822dcee48f2d0ff6196d8459af70844196275",
        },
    },
}


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.mark.parametrize("name", sorted(ASSETS))
def test_vendor_asset_files_and_readme_checksums(name):
    spec = ASSETS[name]
    directory = VENDOR / name
    assert directory.is_dir(), f"缺少 vendor 目录 {directory}"
    readme_path = directory / "README.md"
    assert readme_path.is_file(), f"缺少 {name} 的 README.md"
    readme = readme_path.read_text(encoding="utf-8")
    assert spec["version"] in readme

    for filename, expected in spec["files"].items():
        path = directory / filename
        assert path.is_file(), f"缺少 {name}/{filename}"
        assert sha256(path) == expected, f"{name}/{filename} 的 SHA-256 与测试不一致"
        size = path.stat().st_size
        assert expected in readme, f"README 未登记 {filename} 的 SHA-256"
        # README 中的大小允许千分位逗号。
        assert re.search(rf"{size:,}|{size}\b", readme), f"README 未登记 {filename} 的大小"


def test_mathlive_ships_fonts_but_not_sounds():
    directory = VENDOR / "mathlive"
    fonts = sorted((directory / "fonts").glob("*.woff2"))
    assert len(fonts) == 20
    assert sum(path.stat().st_size for path in fonts) == 259792
    assert not (directory / "sounds").exists(), "不引入 MathLive 按键音效资源"
    assert (directory / "LICENSE.txt").read_text(encoding="utf-8").strip()


def test_geometry_presets_discipline():
    """几何预设纪律（方案 §6.1）：无 Math.random / eval / new Function；
    不出现字符串形式的数学表达式入参（主站 CSP 无 unsafe-eval）；
    预设数量为 10；JS 语法经 node --check。"""
    import shutil
    import subprocess

    content = (VENDOR / "geometry" / "geometryPresets.js").read_text(encoding="utf-8")
    # 只检查代码本身：头部纪律注释里会出现被禁词的字面说明。
    code_only = re.sub(r"/\*.*?\*/", "", content, flags=re.S)
    code_only = re.sub(r"//[^\n]*", "", code_only)
    assert "Math.random" not in code_only, "几何预设不允许 Math.random（演示需可重放）"
    for pattern in (r"new\s+Function", r"\beval\s*\("):
        assert not re.search(pattern, code_only), f"几何预设不允许 {pattern}"
    assert not re.search(r"create\(['\"]\w+['\"]\s*,\s*\[\s*['\"]", code_only), (
        "几何预设不允许字符串形式的数学表达式入参"
    )
    # text 的字符串内容会走 JessieCode 的 eval 编译路径,严格 CSP 下不可用
    #（审查 H1）:内容必须用函数入参。
    assert not re.search(r"create\(\s*'text'[^;]*?\[\s*[^,\]]+\s*,\s*[^,\]]+\s*,\s*['\"]", code_only), (
        "几何预设的 text 内容不允许用字符串（需函数入参或显式 parse:false）"
    )
    assert code_only.count("    id: '") == 10, "几何预设应为 10 个"

    node = shutil.which("node")
    if node:
        # 模块模式真解析：复制为 .mjs 后 node --check（脚本模式解析会漏掉模板表达式错误）。
        import tempfile

        source = VENDOR / "geometry" / "geometryPresets.js"
        with tempfile.NamedTemporaryFile(suffix=".mjs", delete=False) as handle:
            target = handle.name
        Path(target).write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
        try:
            result = subprocess.run(
                [node, "--check", target],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
            )
        finally:
            Path(target).unlink(missing_ok=True)
        assert result.returncode == 0, result.stderr


def test_jsxgraph_vendor_excludes_expression_modules():
    """npm 包里依赖字符串求值的独立模块（JessieScript / geogebra / geonext / intergeo）
    与非必需资源不随仓库分发（CSP script-src 'self'、无 unsafe-eval）。"""
    directory = VENDOR / "jsxgraph"
    for forbidden in ("JessieScript.js", "geogebra.min.js", "geonext.min.js",
                      "intergeo.min.js", "sketch.min.js", "jsxcompressor.zip"):
        assert not (directory / forbidden).exists(), forbidden
    assert not (directory / "themes").exists(), "themes/ 非本用例所需，不带"


def test_geometry_preset_normalizers_via_node():
    """预设纯函数的数值边界（审查 M2/M4）：仅接受真数值或可完整解析的非空数值
    字符串；null/空串/数组/布尔回退默认值而非隐式转 0/1。Node 缺席时跳过
    （与 node --check 同口径）。"""
    import json as json_lib
    import shutil
    import subprocess

    node = shutil.which("node")
    if not node:
        return
    presets_path = (VENDOR / "geometry" / "geometryPresets.js").as_uri()
    script = (
        "const mod = await import(" + json_lib.dumps(presets_path) + ");\n"
        "const preset = mod.findGeometryPreset('conic-unified');\n"
        "const norm = raw => mod.normalizeGeometryParams(preset, raw).e;\n"
        "console.log(JSON.stringify({\n"
        "  unknown: mod.findGeometryPreset('no-such-preset'),\n"
        "  known: !!preset,\n"
        "  missing: norm({}),\n"
        "  nullValue: norm({e: null}),\n"
        "  emptyString: norm({e: ''}),\n"
        "  array: norm({e: []}),\n"
        "  bool: norm({e: true}),\n"
        "  overMax: norm({e: 99}),\n"
        "  underMin: norm({e: -5}),\n"
        "  numericString: norm({e: '1.5'}),\n"
        "  garbageString: norm({e: 'abc'}),\n"
        "}));\n"
    )
    result = subprocess.run(
        [node, "--input-type=module", "-e", script],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
    )
    assert result.returncode == 0, result.stderr
    data = json_lib.loads(result.stdout.strip().splitlines()[-1])
    assert data["unknown"] is None
    assert data["known"] is True
    # conic-unified 的 e: value=0.6, min=0.2, max=2.8。
    assert data["missing"] == 0.6
    for key in ("nullValue", "emptyString", "array", "bool", "garbageString"):
        assert data[key] == 0.6, f"{key} 应回退默认值,得到 {data[key]}"
    assert data["overMax"] == 2.8
    assert data["underMin"] == 0.2
    assert data["numericString"] == 1.5


def test_markmap_bundle_is_registered_and_pinned():
    """markmap 打包产物（§6.4）：构建脚本版本锁定、产物可由脚本重建、
    组件与笔记页接线存在。--check 的字节级复现校验为手动命令（需网络），
    见 web/vendor/markmap/README.md。"""
    import json as json_lib

    build_script = (ROOT / "scripts" / "build_markmap_bundle.mjs").read_text(encoding="utf-8")
    for pinned in ("'markmap-view': '0.18.13'", "'markmap-common': '0.18.9'",
                   "d3: '7.9.2'", "'@babel/runtime': '7.29.15'"):
        assert pinned in build_script, pinned
    assert "--check" in build_script
    # 顶层与全部传递依赖由仓库内 lockfile 钉死（npm ci），不再依赖运行时解析。
    lock_dir = ROOT / "scripts" / "markmap-bundle"
    assert (lock_dir / "package.json").is_file()
    lock = json_lib.loads((lock_dir / "package-lock.json").read_text(encoding="utf-8"))
    for name, version in (
        ("markmap-view", "0.18.13"), ("markmap-common", "0.18.9"),
        ("d3", "7.9.2"), ("d3-scale", "4.0.3"), ("@gera2ld/jsx-dom", "2.2.2"),
    ):
        assert lock["packages"][f"node_modules/{name}"]["version"] == version, name
    assert "npm ci" in build_script
    assert "metafile: true" in build_script
    # LICENSE 按 metafile 的实际打入包逐包聚合，不能只登记四个顶层包。
    license_text = (VENDOR / "markmap" / "LICENSE").read_text(encoding="utf-8")
    assert license_text.count("\n## ") + license_text.startswith("## ") >= 33
    for name in ("markmap-view", "markmap-common", "d3", "d3-scale", "d3-selection"):
        assert f"## {name} " in license_text, name

    mindmap = (ROOT / "web" / "js" / "components" / "mindmap.js").read_text(encoding="utf-8")
    assert "renderInteractiveMindmap" in mindmap
    assert "/static/vendor/markmap/markmap-view.iife.min.js" in mindmap
    assert "mindmap.interactiveFailed" in mindmap
    # markmap 的 content 按 HTML 渲染：笔记文本必须先转义（安全审查 HIGH）。
    assert "escapeMindmapLabel" in mindmap
    assert "content: escapeMindmapLabel(node.label)" in mindmap

    notes = (ROOT / "web" / "js" / "pages" / "notes.js").read_text(encoding="utf-8")
    assert "renderInteractiveMindmap" in notes
    assert "mindmap.viewInteractive" in notes
    assert "aria-pressed" in notes
    # 切换按钮常驻，切换视图不重建按钮（键盘焦点保留，审查 M4）。
    assert "mindmapModeButtons" in notes
    assert "mindmapMount.contains(mindmapModes)" in notes
    # 打包产物是唯一的 vendor 文件:无源映射、无多余产物。
    directory = VENDOR / "markmap"
    assert sorted(item.name for item in directory.iterdir()) == [
        "LICENSE", "README.md", "markmap-view.iife.min.js",
    ]


def test_mindmap_escape_label_via_node():
    """交互脑图的纯文本转义（安全审查 HIGH）：content 进入 markmap 前必须转义。"""
    import json as json_lib
    import shutil
    import subprocess

    node = shutil.which("node")
    if not node:
        return
    module = (ROOT / "web" / "js" / "components" / "mindmap.js").as_uri()
    script = (
        "const mod = await import(" + json_lib.dumps(module) + ");\n"
        "console.log(mod.escapeMindmapLabel('<img src=x onerror=1> & \"q\"'));\n"
    )
    result = subprocess.run(
        [node, "--input-type=module", "-e", script],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "&lt;img src=x onerror=1&gt; &amp; &quot;q&quot;"


def test_timeline_presets_discipline():
    """时间轴预设纪律（方案 §6.5）：无 Math.random / eval / new Function；
    四个专题、事件按开始日期排序、分组引用有效（日期校验见 Node 用例）。"""
    content = (VENDOR / "timeline" / "timelinePresets.js").read_text(encoding="utf-8")
    code_only = re.sub(r"/\*.*?\*/", "", content, flags=re.S)
    code_only = re.sub(r"//[^\n]*", "", code_only)
    assert "Math.random" not in code_only, "时间轴预设不允许 Math.random"
    for pattern in (r"new\s+Function", r"\beval\s*\("):
        assert not re.search(pattern, code_only), f"时间轴预设不允许 {pattern}"
    assert code_only.count("    id: '") == 4, "时间轴预设应为 4 个"


def test_timeline_presets_and_normalizers_via_node():
    """预设纯函数边界：日期严格校验、事件上限、选项白名单、HTML 转义。"""
    import json as json_lib
    import shutil
    import subprocess

    node = shutil.which("node")
    if not node:
        return
    presets_path = (VENDOR / "timeline" / "timelinePresets.js").as_uri()
    script = (
        "const mod = await import(" + json_lib.dumps(presets_path) + ");\n"
        "const presets = mod.TIMELINE_PRESETS;\n"
        "const def = mod.normalizeTimelineDefinition({preset_id: presets[0].id});\n"
        "const bad = raw => mod.normalizeTimelineDefinition(raw) === null;\n"
        "const events = presets.flatMap(p => p.events);\n"
        "console.log(JSON.stringify({\n"
        "  presets: presets.length,\n"
        "  idsUnique: new Set(presets.map(p => p.id)).size === presets.length,\n"
        "  events: events.length,\n"
        "  sorted: presets.every(p => p.events.every((e, i) => !i || p.events[i-1].start <= e.start)),\n"
        "  groupsOk: presets.every(p => p.events.every(e => !e.group || p.groups.some(g => g.id === e.group))),\n"
        "  datesOk: events.every(e => mod.parseTimelineDate(e.start) && (!e.end || mod.parseTimelineDate(e.end) >= mod.parseTimelineDate(e.start))),\n"
        "  defaultCount: def.events.length,\n"
        "  badPreset: bad({preset_id: 'nope'}),\n"
        "  badMonth: mod.parseTimelineDate('1840-13-01') === null,\n"
        "  badDay: mod.parseTimelineDate('1900-02-29') === null,\n"
        "  leap: mod.parseTimelineDate('2000-02-29') !== null,\n"
        "  overLimit: bad({preset_id: presets[0].id, events: Array.from({length: 201}, (_, i) => ({start: '1900', title: 'e' + i}))}),\n"
        "  endBeforeStart: bad({preset_id: presets[0].id, events: [{start: '1900-02-01', end: '1900-01-01', title: 'x'}]}),\n"
        "  badHeight: bad({preset_id: presets[0].id, options: {height: 99}}),\n"
        "  escaped: mod.escapeTimelineHtml('<img>'),\n"
        "}));\n"
    )
    result = subprocess.run(
        [node, "--input-type=module", "-e", script],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
    )
    assert result.returncode == 0, result.stderr
    data = json_lib.loads(result.stdout.strip().splitlines()[-1])
    assert 3 <= data["presets"] <= 5
    assert data["idsUnique"] is True
    assert data["events"] == 71
    assert data["sorted"] is True
    assert data["groupsOk"] is True
    assert data["datesOk"] is True
    assert data["defaultCount"] == 21
    for key in ("badPreset", "badMonth", "badDay", "overLimit", "endBeforeStart", "badHeight"):
        assert data[key] is True, key
    assert data["leap"] is True
    assert data["escaped"] == "&lt;img&gt;"


def test_timeline_vendor_bundle_and_wiring():
    """vis-timeline standalone 产物无字符串求值；组件与五步接线齐全。"""
    bundle = (VENDOR / "vis-timeline" / "vis-timeline-graph2d.min.js").read_text(
        encoding="utf-8", errors="ignore")
    for pattern in (r"new\s+Function", r"\beval\s*\("):
        assert not re.search(pattern, bundle), f"vis-timeline 产物不允许 {pattern}"
    directory = VENDOR / "vis-timeline"
    assert sorted(item.name for item in directory.iterdir()) == [
        "LICENSE.Apache-2.0.txt", "LICENSE.MIT.txt", "README.md",
        "vis-timeline-graph2d.min.css", "vis-timeline-graph2d.min.js",
    ]

    component = (ROOT / "web" / "js" / "components" / "timelineDemo.js").read_text(encoding="utf-8")
    for needle in (
        "createTimelineView", "openTimelineDemo", "escapeTimelineHtml",
        "/static/vendor/vis-timeline/vis-timeline-graph2d.min.js",
        "study.oss.timeline", "destroy()",
    ):
        assert needle in component, needle

    question = (ROOT / "web" / "js" / "pages" / "question.js").read_text(encoding="utf-8")
    assert "TIMELINE_SUBJECTS" in question
    assert "timelineEntryButton" in question
    notes = (ROOT / "web" / "js" / "pages" / "notes.js").read_text(encoding="utf-8")
    assert "timelineDemo.js" in notes and "createTimelineView" in notes
    player = (ROOT / "web" / "js" / "components" / "classroomPlayer.js").read_text(encoding="utf-8")
    assert "kind === 'timeline'" in player
    offline = (ROOT / "web" / "js" / "offline" / "offlineDemo.js").read_text(encoding="utf-8")
    assert "timeline: true" in offline and 'timeline: "时间轴"' in offline


def test_graph_vendor_bundle_and_wiring():
    """sigma/graphology 产物无字符串求值；知识图谱组件与学情页接线齐全。"""
    for filename in ("sigma.min.js", "graphology.umd.min.js"):
        bundle = (VENDOR / "graph" / filename).read_text(encoding="utf-8", errors="ignore")
        for pattern in (r"new\s+Function", r"\beval\s*\("):
            assert not re.search(pattern, bundle), f"graph/{filename} 不允许 {pattern}"
    directory = VENDOR / "graph"
    assert sorted(item.name for item in directory.iterdir()) == [
        "GRAPHOLOGY-LICENSE.txt", "README.md", "SIGMA-LICENSE.txt",
        "graphology.umd.min.js", "sigma.min.js",
    ]
    component = (ROOT / "web" / "js" / "components" / "knowledgeGraph.js").read_text(encoding="utf-8")
    for needle in (
        "renderKnowledgeGraph", "normalizeKnowledgeGraph",
        "/static/vendor/graph/sigma.min.js",
        "/static/vendor/graph/graphology.umd.min.js",
        "insight.graph", "renderer.kill",
    ):
        assert needle in component, needle
    page = (ROOT / "web" / "js" / "pages" / "insight.js").read_text(encoding="utf-8")
    assert "knowledgeGraphSection" in page
    assert "/insight/kp-graph" in page
    endpoint = (ROOT / "app" / "insight.py").read_text(encoding="utf-8")
    assert '@router.get("/kp-graph")' in endpoint
    assert "KP_GRAPH_MAX_EDGES = 300" in endpoint


def test_vendor_static_assets_served(client):
    """懒加载的同源 vendor 脚本必须能经 /static 实际访问（CSP script-src 'self'）。"""
    for name, spec in sorted(ASSETS.items()):
        for filename in spec["files"]:
            response = client.get(f"/static/vendor/{name}/{filename}")
            assert response.status_code == 200, f"/static/vendor/{name}/{filename}"
            assert len(response.content) == (VENDOR / name / filename).stat().st_size
