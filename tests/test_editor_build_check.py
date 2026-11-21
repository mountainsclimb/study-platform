"""自由画布编辑器：类型检查配置与构建产物一致性检查（P1）。

覆盖 `scripts/check_editor_build.py` 的比对逻辑，不依赖本机是否安装 pnpm/Node：
- 同一产物 → 0；
- 内容变化 / 缺文件 / 多文件 → 1，并在报告里列出具体路径；
- 缺少构建工具时返回 2（错误信息可读）。
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts import check_editor_build as checker  # noqa: E402


def _write(root: Path, relative: str, content: str) -> None:
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def test_identical_directories_compare_clean(tmp_path, capsys):
    built = tmp_path / "built"
    published = tmp_path / "published"
    for root in (built, published):
        _write(root, "index.html", "<html>editor</html>")
        _write(root, "assets/app-abc.js", "console.log('x')")
    assert checker.main(["--built", str(built), "--published", str(published)]) == 0
    assert "EDITOR BUILD OK" in capsys.readouterr().out


def test_changed_missing_and_extra_files_are_reported(tmp_path, capsys):
    built = tmp_path / "built"
    published = tmp_path / "published"
    _write(built, "index.html", "<html>new</html>")
    _write(published, "index.html", "<html>old</html>")
    _write(built, "assets/app-new.js", "new")
    _write(published, "assets/app-old.js", "old")
    assert checker.main(["--built", str(built), "--published", str(published)]) == 1
    output = capsys.readouterr().out
    assert "EDITOR BUILD DRIFT" in output
    assert "index.html" in output and "assets/app-new.js" in output and "assets/app-old.js" in output


def test_json_report_lists_differences(tmp_path, capsys):
    built = tmp_path / "built"
    published = tmp_path / "published"
    _write(built, "a.js", "same")
    _write(published, "a.js", "same")
    _write(built, "b.js", "only-built")
    assert checker.main(["--built", str(built), "--published", str(published), "--json"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["identical"] is False
    assert report["missing_in_published"] == ["b.js"]
    assert report["changed"] == []


def test_missing_pnpm_reports_clear_error(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(checker, "_find_pnpm", lambda: None)
    code = checker.main(["--build"])
    assert code == 2
    assert "pnpm" in capsys.readouterr().err


def test_tsconfig_enables_es2022_type_library_and_typecheck_script():
    """画布 vendored 代码使用 Object.hasOwn / String.replaceAll，需要 ES2022 类型库。"""
    import json

    root = Path(__file__).resolve().parent.parent
    tsconfig = json.loads((root / "editor" / "tsconfig.json").read_text(encoding="utf-8"))
    assert "ES2022" in tsconfig["compilerOptions"]["lib"]
    package = json.loads((root / "editor" / "package.json").read_text(encoding="utf-8"))
    assert package["scripts"]["typecheck"] == "tsc --noEmit"
    assert package.get("browserslist"), "需要显式声明浏览器支持范围（Object.hasOwn 等内置对象）"
