"""开源组件 vendor 资产自检（批次二起，后续演示库复用）。

当前覆盖 MathLive（《开源组件整合实施方案》§3.3）：
* 主文件、样式、字体与 LICENSE 存在；
* README.md 里的版本、大小与 SHA-256 与磁盘一致；
* 不引入按键音效等非必要资源。
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



def test_vendor_static_assets_served(client):
    """懒加载的同源 vendor 脚本必须能经 /static 实际访问（CSP script-src 'self'）。"""
    for name, spec in sorted(ASSETS.items()):
        for filename in spec["files"]:
            response = client.get(f"/static/vendor/{name}/{filename}")
            assert response.status_code == 200, f"/static/vendor/{name}/{filename}"
            assert len(response.content) == (VENDOR / name / filename).stat().st_size
