"""WS27 构建指纹与分发治理：``_compute_build_id`` 覆盖范围 + ``.dockerignore`` / Dockerfile 契约。

方案 §7「分发与运维」要求：固定提交、依赖及题库清单；构建指纹覆盖编辑器、vendor、语言包、
样式和数据库迁移；镜像构建忽略本地库、备份和 ``node_modules``，分发制品携带三方声明。

本文件不联网、不需要浏览器：只读仓库文件，并在临时目录构造最小仓库树验证指纹行为。
覆盖：
1. 指纹模式覆盖上述关键面（源码 / 迁移 / 提示词 / JS / 语言包 / 样式 / 编辑器与 vendor）；
2. 每个关键面内容变化都会改变指纹，同一内容稳定且与遍历顺序无关；
3. 缓存目录（``__pycache__`` / ``node_modules``）不参与指纹；
4. ``STUDY_BUILD_ID`` 显式覆盖优先，缺失目录不抛错；
5. ``.dockerignore`` 排除本地数据 / 备份 / 依赖 / 版本库 / 验收产物，同时保留
   ``web/editor``、``web/vendor``、``web/locales``、``editor/vendor`` 与 ``app/migrations``；
6. Dockerfile 随包携带三方声明与 vendored 编辑器源码。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app import main

ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _write(root: Path, relative: str, content: str) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _synthetic_root(tmp_path: Path) -> Path:
    """最小仓库树：每个关键构建面各放一个文件，供指纹变化用例逐项修改。"""
    root = tmp_path / "repo"
    files = {
        "app/main.py": "# app\n",
        "app/migrations/001_init.sql": "CREATE TABLE t(id INTEGER);\n",
        "app/ai/prompts/grid.yaml": "a: 1\n",
        "web/js/main.js": "// js\n",
        "web/locales/zh-CN.json": "{}\n",
        "web/styles.css": "body{}\n",
        "web/styles-pages.css": ".a{}\n",
        "web/index.html": "<html></html>\n",
        "web/editor/index.html": "<html>editor</html>\n",
        "web/editor/assets/index-abc.js": "// editor asset\n",
        "web/vendor/katex/katex.min.js": "// katex\n",
        "editor/src/App.tsx": "export default 1;\n",
        "editor/vendor/openmaic/VENDORED.json": "{}\n",
    }
    for relative, content in files.items():
        _write(root, relative, content)
    return root


SURFACES = (
    "app/main.py",
    "app/migrations/001_init.sql",
    "app/ai/prompts/grid.yaml",
    "web/js/main.js",
    "web/locales/zh-CN.json",
    "web/styles.css",
    "web/styles-pages.css",
    "web/index.html",
    "web/editor/assets/index-abc.js",
    "web/vendor/katex/katex.min.js",
    "editor/src/App.tsx",
    "editor/vendor/openmaic/VENDORED.json",
)


@pytest.fixture
def synthetic_root(tmp_path, monkeypatch) -> Path:
    root = _synthetic_root(tmp_path)
    monkeypatch.delenv("STUDY_BUILD_ID", raising=False)
    monkeypatch.setattr(main, "__file__", str(root / "app" / "main.py"))
    return root


def _read_dockerignore() -> list[str]:
    path = ROOT / ".dockerignore"
    assert path.is_file(), "缺少 .dockerignore（构建上下文未收敛）"
    lines = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        lines.append(line)
    return lines


# ---------------------------------------------------------------------------
# 1. 指纹覆盖范围
# ---------------------------------------------------------------------------

def test_patterns_cover_required_build_surfaces():
    patterns = tuple(main.BUILD_FINGERPRINT_PATTERNS)
    assert "app/**/*.py" in patterns
    assert "app/migrations/**/*.sql" in patterns, "迁移必须进入构建指纹"
    assert "app/ai/prompts/**/*.yaml" in patterns
    assert "web/js/**/*.js" in patterns
    assert "web/locales/*.json" in patterns, "语言包必须进入构建指纹"
    # 用通配覆盖全部样式文件，而不是只列 styles.css / styles-pages.css 两份。
    assert "web/styles*.css" in patterns
    assert "web/editor/**/*" in patterns, "编辑器构建产物必须进入构建指纹"
    assert "web/vendor/**/*" in patterns, "vendored 运行时必须进入构建指纹"
    assert "editor/src/**/*" in patterns
    assert "editor/vendor/**/*" in patterns, "vendored 源码必须进入构建指纹"
    # 模式必须是相对路径（不能把宿主绝对路径写进指纹口径）。
    for pattern in patterns:
        assert not pattern.startswith(("/", "\\")) and ":" not in pattern, pattern


def test_iter_fingerprint_files_covers_every_required_surface(synthetic_root):
    """``_synthetic_root`` 里的每个关键面都必须真的被指纹枚举到，否则后面会假通过。"""
    relatives = {relative for _path, relative in main._iter_build_fingerprint_files(synthetic_root)}
    for relative in SURFACES:
        assert relative in relatives, f"构建指纹未枚举到 {relative}"


# ---------------------------------------------------------------------------
# 2. 内容变化 → 指纹变化（逐关键面）
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("relative", SURFACES)
def test_fingerprint_changes_when_a_key_surface_changes(synthetic_root, relative):
    base = main._compute_build_id()
    assert len(base) == 12 and all(character in "0123456789abcdef" for character in base)
    _write(synthetic_root, relative, "changed-content\n")
    changed = main._compute_build_id()
    assert changed != base, f"修改 {relative} 后构建指纹未变化"


def test_fingerprint_is_stable_and_order_independent(tmp_path, monkeypatch):
    monkeypatch.delenv("STUDY_BUILD_ID", raising=False)
    first_root = _synthetic_root(tmp_path / "a")
    second_root = _synthetic_root(tmp_path / "b")
    monkeypatch.setattr(main, "__file__", str(first_root / "app" / "main.py"))
    first = main._compute_build_id()
    assert main._compute_build_id() == first
    monkeypatch.setattr(main, "__file__", str(second_root / "app" / "main.py"))
    assert main._compute_build_id() == first
    # 新增一个未覆盖的文件不影响指纹（口径稳定）。
    _write(first_root, "docs/notes.md", "doc\n")
    monkeypatch.setattr(main, "__file__", str(first_root / "app" / "main.py"))
    assert main._compute_build_id() == first


def test_fingerprint_ignores_cache_and_dependency_dirs(synthetic_root):
    base = main._compute_build_id()
    _write(synthetic_root, "app/__pycache__/ghost.py", "# cache\n")
    _write(synthetic_root, "web/js/__pycache__/ghost.js", "// cache\n")
    _write(synthetic_root, "editor/vendor/node_modules/pkg/ghost.js", "// dep\n")
    _write(synthetic_root, "web/vendor/node_modules/pkg/ghost.js", "// dep\n")
    assert main._compute_build_id() == base


def test_fingerprint_override_and_missing_dirs(tmp_path, monkeypatch):
    monkeypatch.delenv("STUDY_BUILD_ID", raising=False)
    empty = tmp_path / "empty-repo"
    empty.mkdir()
    (empty / "app").mkdir()
    (empty / "app" / "main.py").write_text("# app\n", encoding="utf-8")
    monkeypatch.setattr(main, "__file__", str(empty / "app" / "main.py"))
    # 关键目录整体缺失时不能抛错，仍返回 12 位十六进制。
    value = main._compute_build_id()
    assert len(value) == 12 and all(character in "0123456789abcdef" for character in value)
    monkeypatch.setenv("STUDY_BUILD_ID", "commit-abc123")
    assert main._compute_build_id() == "commit-abc123"


# ---------------------------------------------------------------------------
# 3. 真实仓库：指纹实际覆盖编辑器 / 语言包 / 样式 / 迁移
# ---------------------------------------------------------------------------

def test_real_repo_fingerprint_covers_editor_locales_styles_and_migrations():
    files = list(main._iter_build_fingerprint_files(ROOT))
    relatives = {relative for _path, relative in files}
    assert len(relatives) == len(files), "指纹文件列表存在重复"
    assert len(files) > 200, f"指纹文件过少（{len(files)}），覆盖范围可能被误删"
    assert any(relative.startswith("web/editor/") for relative in relatives)
    assert any(relative.startswith("web/vendor/") for relative in relatives)
    assert any(relative.startswith("editor/vendor/") for relative in relatives)
    assert any(relative.startswith("editor/src/") for relative in relatives)
    assert "web/locales/zh-CN.json" in relatives and "web/locales/en-US.json" in relatives
    assert any(relative.startswith("app/migrations/") and relative.endswith(".sql")
               for relative in relatives)
    assert any(relative.startswith("web/styles") and relative.endswith(".css")
               for relative in relatives)
    # 缓存/依赖目录不得进入指纹。
    assert not any("__pycache__" in relative.split("/") for relative in relatives)
    assert not any("node_modules" in relative.split("/") for relative in relatives)


def test_import_time_build_id_is_stable_hex(monkeypatch):
    monkeypatch.delenv("STUDY_BUILD_ID", raising=False)
    assert len(main.BUILD_ID) == 12
    assert main.BUILD_ID == main.BUILD_ID.lower()
    current = main._compute_build_id()
    assert len(current) == 12
    assert all(character in "0123456789abcdef" for character in current)


# ---------------------------------------------------------------------------
# 4. .dockerignore：排除本地状态、保留分发所需目录
# ---------------------------------------------------------------------------

REQUIRED_EXCLUSIONS = (
    "data/",
    "backups/",
    ".git/",
    "node_modules/",
    "editor/node_modules/",
    "screenshots/",
    "*.log",
    "*.sqlite3",
    ".env",
)

# 这些目录是分发制品的一部分，绝不能被排除（否则镜像/页面缺资源）。
DISTRIBUTION_DIRS = (
    "web/editor",
    "web/vendor",
    "web/locales",
    "web/js",
    "app/migrations",
    "editor/vendor",
    "editor/src",
)


def _matches(relative: str, pattern: str) -> bool:
    """近似 Docker .dockerignore 语义：目录/文件前缀与简单 ``*`` 通配。"""
    import fnmatch

    if pattern.endswith("/"):
        base = pattern.rstrip("/")
        return relative == base or relative.startswith(base + "/")
    return fnmatch.fnmatchcase(relative, pattern)


def test_dockerignore_excludes_local_state_and_acceptance_artifacts():
    lines = _read_dockerignore()
    for pattern in REQUIRED_EXCLUSIONS:
        assert pattern in lines, f".dockerignore 缺少排除规则：{pattern}"
    # 验收/截图/日志产物也应在上下文之外。
    assert any(pattern.startswith("screenshots") for pattern in lines)
    assert any(pattern in ("accept-*.json", "pytest-*.log", "*.log") for pattern in lines)


def test_dockerignore_keeps_distribution_assets():
    lines = _read_dockerignore()
    samples = (
        "web/editor/index.html",
        "web/editor/assets/index-abc.js",
        "web/vendor/katex/katex.min.js",
        "web/locales/zh-CN.json",
        "web/js/main.js",
        "app/migrations/001_init.sql",
        "editor/vendor/openmaic/VENDORED.json",
        "editor/src/App.tsx",
    )
    for relative in samples:
        for pattern in lines:
            if pattern.startswith("!"):
                continue
            assert not _matches(relative, pattern), (
                f".dockerignore 规则 {pattern!r} 误排除了分发资源 {relative}"
            )
    # 明确保留声明：注释里应写清楚这些目录必须留在上下文中。
    raw = (ROOT / ".dockerignore").read_text(encoding="utf-8")
    for relative in DISTRIBUTION_DIRS:
        assert relative in raw, f".dockerignore 未声明保留 {relative}"


# ---------------------------------------------------------------------------
# 5. Dockerfile 分发契约
# ---------------------------------------------------------------------------

def _dockerfile() -> str:
    path = ROOT / "deploy" / "Dockerfile"
    assert path.is_file(), "缺少 deploy/Dockerfile"
    return path.read_text(encoding="utf-8")


def test_dockerfile_copies_runtime_and_distribution_assets():
    text = _dockerfile()
    assert "COPY app ./app" in text
    assert "COPY web ./web" in text
    assert "COPY eval ./eval" in text
    assert "COPY deploy ./deploy" in text
    assert "COPY scripts ./scripts" in text
    # 分发制品携带三方声明（方案 §7）。
    assert "THIRD_PARTY_NOTICES.md" in text
    # vendored 编辑器源码随镜像分发（web/editor 是构建产物，已由 COPY web 覆盖）。
    assert "COPY editor/vendor" in text
    # 不把依赖目录/node_modules 拷进镜像。
    copy_lines = [line for line in text.splitlines() if line.strip().upper().startswith("COPY")]
    assert copy_lines, "Dockerfile 缺少 COPY 指令"
    assert not any("node_modules" in line for line in copy_lines)
