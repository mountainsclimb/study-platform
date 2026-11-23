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
