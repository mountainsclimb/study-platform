"""物理演示模块：vendored 物理内核、演示配置与静态资源可访问性。

不依赖浏览器：用 Node 跑通 10 个演示的物理构建与自动校验；node 缺失时跳过。
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
VENDOR = ROOT / "web" / "vendor" / "physics-in-motion"


def test_physics_vendor_files_present():
    assert (VENDOR / "physics-core.js").is_file()
    assert (VENDOR / "demos.js").is_file()
    license_text = (VENDOR / "LICENSE").read_text(encoding="utf-8")
    assert "MIT License" in license_text
    assert "xuxuezhou" in license_text
    readme = (VENDOR / "README.md").read_text(encoding="utf-8")
    assert "physics-in-motion" in readme
    assert "d35b9a167fc4406814f5731fab6b1116890171df" in readme


def test_physics_route_registered():
    state = (ROOT / "web" / "js" / "core" / "state.js").read_text(encoding="utf-8")
    main = (ROOT / "web" / "js" / "main.js").read_text(encoding="utf-8")
    # 路由标签已 i18n 化（`['physics', 'atom', '物理演示']` + routeLabel()/t('nav.physics')），
    # 这里只断言路由与图标注册存在、标签可解析，不再依赖具体语言字面量。
    assert "['physics', 'atom'" in state
    assert "renderPhysics" in main
    assert "physics: renderPhysics" in main
    assert (ROOT / "web" / "js" / "pages" / "physics.js").is_file()
    assert (ROOT / "web" / "js" / "components" / "physicsPlayer.js").is_file()


@pytest.mark.skipif(shutil.which("node") is None, reason="需要 Node.js 验证物理内核")
def test_physics_assets_build_all_demos():
    result = subprocess.run(
        [shutil.which("node"), "scripts/verify_physics_assets.mjs"],
        cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "验证通过" in result.stdout
    for demo in ("demo-incline", "demo-projectile", "demo-wave", "demo-magnetic"):
        assert demo in result.stdout


@pytest.mark.skipif(shutil.which("node") is None, reason="需要 Node.js 语法检查")
def test_physics_js_syntax(tmp_path):
    files = ["web/js/pages/physics.js", "web/js/components/physicsPlayer.js"]
    for relative in files:
        source = ROOT / relative
        target = tmp_path / (source.name + ".mjs")
        target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
        result = subprocess.run(
            [shutil.which("node"), "--check", str(target)],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
        )
        assert result.returncode == 0, f"{relative}: {result.stderr}"


def test_physics_vendor_assets_served(client):
    core = client.get("/static/vendor/physics-in-motion/physics-core.js")
    assert core.status_code == 200
    assert len(core.content) > 30_000
    demos = client.get("/static/vendor/physics-in-motion/demos.js")
    assert demos.status_code == 200
    assert b"DEMOS" in demos.content
