"""Pyodide 实验台验收（§9 进阶项）：独立 CSP、专用页、同源资产与接线。"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VENDOR = ROOT / "web" / "vendor" / "pyodide"
LAB = ROOT / "web" / "lab"

EXPECTED = {
    "pyodide.js": (16811, "564b1acbfeb07eda9ba849ee0a0ed1eb8587aab62d9f18082ed905ed36432384"),
    "pyodide.asm.js": (1072130, "da195d86c80caece821124dcd4f049e6465a7a73797f1b2b4147283e445d02a2"),
    "pyodide.asm.wasm": (8645010, "4ee3dc1deab639b454fdc6683651b4cf6c86fc587072e4f7b6c8eaed777d9db6"),
    "python_stdlib.zip": (2416414, "1cc90fccf52e5145bd1bd3ef3540f5e1dd8bf17c5fc66c3e037802ef06ea62af"),
    "pyodide-lock.json": (108703, "6c6ba6e257a75193fc935a456963b0bfd3f9f4e307c52378b030e25b0b968227"),
    "LICENSE": (16725, "1f256ecad192880510e84ad60474eab7589218784b9a50bc7ceee34c2b91f1d5"),
}


def test_pyodide_assets_and_readme():
    assert VENDOR.is_dir()
    assert sorted(item.name for item in VENDOR.iterdir()) == sorted(
        [*EXPECTED, "README.md"])
    readme = (VENDOR / "README.md").read_text(encoding="utf-8")
    assert "0.28.0" in readme
    for name, (size, digest) in EXPECTED.items():
        path = VENDOR / name
        assert path.is_file(), name
        assert path.stat().st_size == size, name
        assert hashlib.sha256(path.read_bytes()).hexdigest() == digest, name
        assert digest in readme and (f"{size:,}" in readme or str(size) in readme), name


def test_pyodide_lab_page_has_dedicated_csp(client):
    response = client.get("/lab/python")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    csp = response.headers["content-security-policy"]
    assert "'wasm-unsafe-eval'" in csp
    assert "'unsafe-eval'" not in csp
    assert "worker-src 'none'" in csp
    assert "script-src 'self' 'wasm-unsafe-eval'" in csp
    assert response.headers.get("x-frame-options") == "DENY"
    body = response.text
    assert "/static/lab/python.js" in body
    assert "/static/lab/python.css" in body
    assert "Python 实验台" in body
    # 页面只允许一个外部 module 脚本，不写内联脚本。
    assert len(re.findall(r"<script\b", body)) == 1
    assert "onerror=" not in body and "onclick=" not in body


def test_main_site_csp_is_not_relaxed(client):
    response = client.get("/")
    assert response.status_code == 200
    csp = response.headers["content-security-policy"]
    assert "'wasm-unsafe-eval'" not in csp
    assert "script-src 'self'" in csp


def test_pyodide_lab_requires_login(isolated_db):
    from fastapi.testclient import TestClient
    from app.main import app

    with TestClient(app) as visitor:
        response = visitor.get("/lab/python")
        assert response.status_code == 401


def test_pyodide_lab_assets_and_wiring(client):
    for path in (
        "/static/vendor/pyodide/pyodide.js",
        "/static/vendor/pyodide/pyodide.asm.wasm",
        "/static/vendor/pyodide/python_stdlib.zip",
        "/static/lab/python.js",
        "/static/lab/python.css",
    ):
        response = client.get(path)
        assert response.status_code == 200, path
        assert response.content
    script = (LAB / "python.js").read_text(encoding="utf-8")
    assert "loadPyodide" in script
    assert "/static/vendor/pyodide/" in script
    assert "runPythonAsync" in script
    assert "textContent" in script
    assert "eval(" not in script and "new Function" not in script
    assert "URLSearchParams" in script and "get('code')" in script
    assert "#code=" in script


def test_classroom_code_widget_links_to_pyodide_lab():
    player = (ROOT / "web" / "js" / "components" / "classroomPlayer.js").read_text(encoding="utf-8")
    assert "interactiveCodeText" in player
    assert "widget" in player and "=== 'code'" in player
    assert "/lab/python" in player and "#code=" in player
    assert "target: '_blank'" in player
    assert "DOMParser" in player

