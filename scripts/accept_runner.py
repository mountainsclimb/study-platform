"""知序本机验收 runner（scripts/ci_accept.ps1 的可测试核心）。

本模块修复 P0「验收脚本出现成功假象」：

* 阶段不再直接 ``exit``：``ci_accept.ps1`` 只是薄外壳，阶段登记/执行/汇总/退出码都在这里，
  每个阶段的结果（OK / FAIL / SKIP）都会回传汇总并落盘，避免外壳脚本提前退出导致后续阶段
  （pytest、UI 验收）漏跑后仍显示成功；
* 任何阶段失败、UI 模式服务器不可达、没有任何阶段真正执行（漏跑）都返回非零；
* 只有**全部选中阶段都真正执行且全过**（skipped==0）才返回 0；只要存在 SKIP
  （工具缺失、-SkipPytest、-Stages 过滤）就返回 3 并打印 ``ACCEPT INCOMPLETE``，
  跳过阶段单独列出，汇总里不会再出现 ``ACCEPT OK``；
* 默认题库跟随 ``releases/CURRENT``（当前 r5），r3 只能通过 ``--bank-dir`` 显式指定；
* 报告落盘 build_id / bank_version / 每阶段状态；报告 ``accept`` 字段与 ``exit_code`` 保持一致。

用法（在仓库根目录 ``E:\\responsibility\\study-platform``）：

    python scripts/accept_runner.py                       # 默认全量静态检查 + pytest（题库跟随 releases/CURRENT）
    python scripts/accept_runner.py --ui --base-url http://127.0.0.1:8840
                                                          # 追加浏览器验收；服务器不可达直接失败
    python scripts/accept_runner.py --editor-build        # 追加「编辑器构建产物与 web/editor 一致」检查
    python scripts/accept_runner.py --stages bank,compileall
                                                          # 只跑部分阶段（开发用，其余记为 SKIP → 退出 3）
    python scripts/accept_runner.py --skip-pytest         # 跳过全量 pytest（开发用，退出 3）
    python scripts/accept_runner.py --plan-json           # 只打印执行计划（不执行），便于核对基线
    python scripts/accept_runner.py --list-stages         # 列出阶段键与说明

退出码：``0`` 全部选中阶段执行且全过（skipped==0）；``1`` 存在失败阶段（含阶段异常、UI 服务器
不可达）；``2`` 参数/环境错误（未知阶段键等）；``3`` 未完成（有阶段被跳过，或没有任何阶段真正执行）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parents[1]
BANK_ROOT = (ROOT.parent / "高考题库_按学科").resolve()
DEFAULT_BASE_URL = "http://127.0.0.1:8840"
DEFAULT_REPORT_PATH = ROOT / "screenshots" / "accept-report.json"
DEFAULT_ALLOWED_HOSTS = "127.0.0.1,localhost,[::1],testserver"

# 与 app/main.py::_compute_build_id 同口径：验收留痕的 build_id 必须与页面/接口报出的一致。
# 与 app/main.py::BUILD_FINGERPRINT_PATTERNS 保持一致（方案 §7「分发与运维」）：
# 源码、迁移、提示词、前端 JS/语言包/样式、编辑器构建产物与 vendored 源码都参与指纹，
# 这样验收报告里的 build_id 与服务 /api/health 暴露的构建号同口径。
BUILD_ID_PATTERNS = (
    "app/**/*.py",
    "app/migrations/**/*.sql",
    "app/ai/prompts/**/*.yaml",
    "web/js/**/*.js",
    "web/locales/*.json",
    "web/styles*.css",
    "web/index.html",
    "web/editor/**/*",
    "web/vendor/**/*",
    "editor/src/**/*",
    "editor/vendor/**/*",
)
BUILD_ID_SKIP_PARTS = frozenset({"__pycache__", "node_modules", ".git"})

# 阶段键（-Stages / --stages 使用）。顺序即执行顺序。
STAGE_LABELS = {
    "bank": "题库基线（releases/CURRENT）",
    "compileall": "python compileall",
    "node-check": "node --check (web/js)",
    "editor-typecheck": "编辑器类型检查（tsc --noEmit）",
    "editor-build": "编辑器构建产物一致性（-EditorBuild）",
    "ui-static": "前端静态契约与 ESM 解析",
    "round5": "第五轮共用学习数据层端到端（临时数据目录）",
    "pytest": "pytest（全量）",
    "ui-server": "UI 服务器可达性",
    "ui-plaza": "广场端到端（投稿→评审→发布→撤回）",
    "ui-official": "平台精选接入学习链路（official）",
    "ui-ops": "运维监控与质量运营闭环",
    "ui-routes": "UI 路由核对",
    "ui-upload": "拍题入库页面核对",
    "ui-modules": "整合模块端到端",
    # 放在最后：该阶段会配置一个只在本阶段存活的本地假模型 provider，
    # 运行结束后 provider 指向的端口已关闭，不能影响后续 UI 阶段。
    "ui-ai-usage": "AI 用量与配额（本地假模型）",
}


class UsageError(ValueError):
    """参数/环境错误：退出码 2。"""


@dataclass
class BankInfo:
    dir: Path
    source: str
    release: str
    bank_version: str = ""
    questions: int = 0
    groups: int = 0
    ok: bool = True
    detail: str = ""

    def to_dict(self) -> dict:
        return {
            "dir": str(self.dir),
            "source": self.source,
            "release": self.release,
            "bank_version": self.bank_version,
            "questions": self.questions,
            "groups": self.groups,
            "ok": self.ok,
            "detail": self.detail,
        }


@dataclass
class Stage:
    """一个可执行阶段。command 与 runner 二选一；needs 里的工具缺失按 SKIP 处理。"""

    key: str
    name: str
    command: list[str] | None = None
    cwd: Path | None = None
    needs: tuple[str, ...] = ()
    requires: tuple[str, ...] = ()
    skip_returncodes: dict[int, str] = field(default_factory=dict)
    runner: Callable[[], "StageResult"] | None = None
    description: str = ""
    timeout: float = 3600.0

    def to_plan(self) -> dict:
        return {
            "key": self.key,
            "name": self.name,
            "description": self.description,
            "command": list(self.command) if self.command else None,
            "cwd": str(self.cwd) if self.cwd else None,
            "needs": list(self.needs),
            "requires": list(self.requires),
        }


@dataclass
class StageResult:
    key: str
    name: str
    status: str  # ok | fail | skip
    returncode: int | None = None
    duration_s: float = 0.0
    detail: str = ""

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "name": self.name,
            "status": self.status,
            "returncode": self.returncode,
            "duration_s": self.duration_s,
            "detail": self.detail,
        }


# ---------------------------------------------------------------------------
# 题库基线解析
# ---------------------------------------------------------------------------

def resolve_bank(root: Path = ROOT, explicit: str | None = None) -> BankInfo:
    """解析只读题库基线：显式 ``--bank-dir`` > ``releases/CURRENT`` 指针。

    刻意不读环境变量 ``STUDY_BANK_DIR``：验收基线只认显式参数或版本指针，避免本机残留
    环境变量把验收悄悄切到 r3 等旧基线（r3 现在只能通过 --bank-dir 显式指定）。
    """
    bank_root = (root.parent / "高考题库_按学科").resolve()
    if explicit:
        directory = Path(explicit).expanduser()
        if not directory.is_absolute():
            directory = (root / directory).resolve()
        else:
            directory = directory.resolve()
        source = "arg"
        release = directory.name
    else:
        pointer = bank_root / "releases" / "CURRENT"
        if pointer.is_file():
            try:
                release = pointer.read_text(encoding="utf-8-sig").strip()
            except OSError as exc:
                return BankInfo(bank_root, "releases/CURRENT", "", ok=False,
                                detail=f"无法读取版本指针 {pointer}：{exc}")
            if not release:
                return BankInfo(bank_root, "releases/CURRENT", "", ok=False,
                                detail=f"版本指针为空：{pointer}")
            directory = (bank_root / "releases" / release).resolve()
            source = "releases/CURRENT"
        elif (bank_root / "题库.db").is_file():
            # 旧布局：题库根目录直接放 题库.db。
            directory = bank_root
            release = bank_root.name
            source = "legacy-root"
            return _probe_bank(directory, source, release, root)
        else:
            return BankInfo(bank_root, "releases/CURRENT", "", ok=False,
                            detail=f"找不到版本指针 {pointer}，也没有旧布局题库")
    return _probe_bank(directory, source, release, root)


def _probe_bank(directory: Path, source: str, release: str, root: Path) -> BankInfo:
    """只读探测题库版本/题量；文件缺失或不可读时 ok=False（由 bank 阶段失败退出）。"""
    info = BankInfo(dir=directory, source=source, release=release)
    path = directory / "题库.db"
    if not path.is_file():
        info.ok = False
        info.detail = f"题库文件不存在：{path}"
        return info
    try:
        con = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=10)
    except sqlite3.Error as exc:
        info.ok = False
        info.detail = f"题库无法打开：{exc}"
        return info
    try:
        info.questions = int(con.execute("SELECT COUNT(*) FROM questions").fetchone()[0])
        try:
            info.groups = int(con.execute("SELECT COUNT(*) FROM groups").fetchone()[0])
        except sqlite3.Error:
            info.groups = 0
        row = con.execute("SELECT bank_version FROM questions LIMIT 1").fetchone()
        info.bank_version = str(row[0]) if row and row[0] else ""
    except sqlite3.Error as exc:
        info.ok = False
        info.detail = f"题库缺少 questions 表或不可读：{exc}"
        return info
    finally:
        con.close()
    if not info.bank_version:
        # 旧库没有 bank_version 列/值：用发布包目录名兜底（仅报告用，不做版本门禁）。
        info.bank_version = release
    if info.questions <= 0:
        info.ok = False
        info.detail = f"题库为空：{path}"
    if info.bank_version and release and release not in info.bank_version and info.bank_version not in release:
        info.detail = f"目录名与库内 bank_version 不一致：{release} / {info.bank_version}"
    return info


def compute_build_id(root: Path = ROOT) -> str:
    """内容指纹（源码 + 前端资源），与 app/main.py::_compute_build_id 同口径。"""
    digest = hashlib.sha256()
    entries: dict[str, Path] = {}
    for pattern in BUILD_ID_PATTERNS:
        for path in root.glob(pattern):
            if not path.is_file():
                continue
            if any(part in BUILD_ID_SKIP_PARTS for part in path.parts):
                continue
            try:
                relative = path.relative_to(root).as_posix()
            except ValueError:  # 符号链接越界
                continue
            entries[relative] = path
    for relative in sorted(entries):
        try:
            digest.update(relative.encode("utf-8"))
            digest.update(entries[relative].read_bytes())
        except OSError:
            continue
    return digest.hexdigest()[:12]


# ---------------------------------------------------------------------------
# 阶段实现
# ---------------------------------------------------------------------------

def _stage_result(key: str, name: str, status: str, *, returncode=None, detail="") -> StageResult:
    return StageResult(key=key, name=name, status=status, returncode=returncode, detail=detail)


def _skip_stage(key: str, name: str, detail: str) -> Stage:
    return Stage(key=key, name=name, description=detail,
                 runner=lambda: _stage_result(key, name, "skip", detail=detail))


def check_bank_stage(bank: BankInfo) -> StageResult:
    key, name = "bank", STAGE_LABELS["bank"]
    if not bank.ok:
        return _stage_result(key, name, "fail", detail=bank.detail or "题库基线不可用")
    detail = f"{bank.questions} 题 / {bank.groups} 组；bank_version={bank.bank_version}；来源 {bank.source}"
    if bank.detail:
        detail += f"；{bank.detail}"
    return _stage_result(key, name, "ok", detail=detail)


def node_syntax_stage(root: Path = ROOT) -> StageResult:
    """逐个 ``node --check`` web/js 下的 JS 文件；失败收集清单后整体失败（不再内联 exit）。"""
    key, name = "node-check", STAGE_LABELS["node-check"]
    node = shutil.which("node")
    if not node:
        return _stage_result(key, name, "skip", detail="缺少工具：node（未做 JS 语法检查）")
    js_dir = root / "web" / "js"
    files = sorted(js_dir.rglob("*.js")) if js_dir.is_dir() else []
    if not files:
        return _stage_result(key, name, "fail", detail=f"未找到任何 JS 文件：{js_dir}")
    bad: list[str] = []
    for path in files:
        try:
            completed = subprocess.run(
                [node, "--check", str(path)],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            bad.append(f"{path.relative_to(root)}（{type(exc).__name__}）")
            continue
        if completed.returncode != 0:
            tail = (completed.stderr or completed.stdout or "").strip().splitlines()[-1:]
            bad.append(f"{path.relative_to(root)}（{tail[0] if tail else 'node --check 失败'}）")
    if bad:
        detail = f"{len(bad)}/{len(files)} 个文件语法错误：" + "；".join(bad[:10])
        return _stage_result(key, name, "fail", returncode=1, detail=detail)
    return _stage_result(key, name, "ok", detail=f"{len(files)} 个 JS 文件通过 node --check")


def probe_server(base_url: str, timeout: float = 5.0) -> tuple[bool, str]:
    """UI 模式前置：服务器不可达返回 (False, 原因)，由调用方把阶段判为 FAIL。"""
    if not base_url:
        return False, "未提供 --base-url"
    url = base_url.rstrip("/") + "/api/health"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            status = getattr(response, "status", response.getcode())
            response.read(512)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return False, f"{type(exc).__name__}: {exc}"
    except Exception as exc:  # pragma: no cover - 防御未知 HTTP 异常
        return False, f"{type(exc).__name__}: {exc}"
    if status != 200:
        return False, f"HTTP {status}"
    return True, "HTTP 200"


def ui_server_stage(base_url: str) -> StageResult:
    key, name = "ui-server", STAGE_LABELS["ui-server"]
    ok, message = probe_server(base_url)
    if not ok:
        return _stage_result(key, name, "fail",
                             detail=f"UI 模式服务器不可达：{base_url}（{message}）")
    return _stage_result(key, name, "ok", detail=f"服务器可达：{base_url}（{message}）")


# ---------------------------------------------------------------------------
# 阶段编排
# ---------------------------------------------------------------------------

def _python() -> str:
    return sys.executable or "python"


def _tool(name: str) -> str:
    return shutil.which(name) or name


def build_stages(
    *,
    root: Path = ROOT,
    bank: BankInfo | None = None,
    ui: bool = False,
    base_url: str = DEFAULT_BASE_URL,
    skip_pytest: bool = False,
    selected: list[str] | None = None,
    editor_build: bool = False,
    pytest_args: list[str] | None = None,
) -> list[Stage]:
    """按参数构造阶段计划；未选择/显式跳过/工具缺失都保留为 SKIP 阶段（漏跑≠成功）。"""
    bank = bank or resolve_bank(root)
    python = _python()
    candidates: dict[str, Stage] = {
        "bank": Stage(
            key="bank", name=STAGE_LABELS["bank"], runner=lambda: check_bank_stage(bank),
            description="校验 releases/CURRENT 指向的题库存在、可读且非空",
        ),
        "compileall": Stage(
            key="compileall", name=STAGE_LABELS["compileall"],
            command=[python, "-m", "compileall", "-q", "app", "scripts", "deploy"],
            cwd=root, description="Python 字节码编译检查",
        ),
        "node-check": Stage(
            key="node-check", name=STAGE_LABELS["node-check"],
            runner=lambda: node_syntax_stage(root), needs=("node",),
            description="web/js 全部 JS 文件语法检查",
        ),
        "editor-typecheck": Stage(
            key="editor-typecheck", name=STAGE_LABELS["editor-typecheck"],
            command=[_tool("pnpm"), "exec", "tsc", "--noEmit"],
            cwd=root / "editor", needs=("pnpm", "node"),
            description="自由画布编辑器 TypeScript 类型检查",
        ),
    }
    if editor_build:
        candidates["editor-build"] = Stage(
            key="editor-build", name=STAGE_LABELS["editor-build"],
            command=[python, "scripts/check_editor_build.py", "--build"],
            cwd=root, needs=("pnpm", "node"),
            skip_returncodes={2: "缺少构建工具（check_editor_build 返回 2），未比对构建产物"},
            description="重新构建编辑器并与入库的 web/editor 比对（exit 1=漂移、2=无构建工具）",
        )
    candidates["ui-static"] = Stage(
        key="ui-static", name=STAGE_LABELS["ui-static"],
        command=[python, "scripts/integration/ui_static_accept.py"],
        cwd=root, needs=("node",),
        description="前端静态契约（任务深链/来源/Anki/locale）与 ESM 解析",
    )
    candidates["round5"] = Stage(
        key="round5", name=STAGE_LABELS["round5"],
        # 第五轮（方案 §4/§8）：在临时数据目录内跑通「拍题块→作答证据→错题→四档评分→
        # 学习附件→跨页读取」并做两账号隔离；不读写题库与生产数据，不需要 Node/浏览器。
        command=[python, "scripts/integration/round5_accept.py"],
        cwd=root,
        description="第五轮端到端：证据/附件/四档复习/OCR 块（隔离数据目录，TestClient）",
    )
    candidates["pytest"] = Stage(
        key="pytest", name=STAGE_LABELS["pytest"],
        command=[python, "-m", "pytest", "-q", *(pytest_args or [])],
        cwd=root, description="全量 pytest（目标测试清单见任务）",
    )
    if ui:
        candidates["ui-server"] = Stage(
            key="ui-server", name=STAGE_LABELS["ui-server"],
            runner=lambda: ui_server_stage(base_url),
            description="UI 模式前置：验收实例 /api/health 必须可达",
        )
        candidates["ui-routes"] = Stage(
            key="ui-routes", name=STAGE_LABELS["ui-routes"],
            command=[python, "scripts/ui_check_integration.py", "--base-url", base_url],
            cwd=root, requires=("ui-server",),
            description="Playwright 逐路由核对页面渲染",
        )
        # 广场端到端放在其它 UI 阶段之前：fresh 实例上由它注册首个管理员并授权评审/编辑角色，
        # 后续 ui-routes/ui-upload 再注册普通账号，不会抢走「首注册=管理员」。
        candidates["ui-plaza"] = Stage(
            key="ui-plaza", name=STAGE_LABELS["ui-plaza"],
            command=[python, "scripts/integration/plaza_accept.py", "--base-url", base_url],
            cwd=root, requires=("ui-server",),
            description="广场全链路：匿名浏览→投稿→初审→评审阈值→发布→撤回→抽检/互动/回滚（含页面冒烟）",
        )
        # official 与 AI 用量阶段复用 ui-plaza 建立的管理员账号；在 fresh 实例上单独运行也能自建。
        candidates["ui-official"] = Stage(
            key="ui-official", name=STAGE_LABELS["ui-official"],
            command=[python, "scripts/integration/official_accept.py", "--base-url", base_url],
            cwd=root, requires=("ui-server",),
            description="平台精选接入：official 引用检索/作答/错题/复习/工坊/撤回即时不可见",
        )
        candidates["ui-ops"] = Stage(
            key="ui-ops", name=STAGE_LABELS["ui-ops"],
            command=[python, "scripts/integration/ops_accept.py", "--base-url", base_url],
            cwd=root, requires=("ui-server",),
            description="运维监控 /api/admin/monitor、抽检门禁、一键恢复、批量处理、通知",
        )
        candidates["ui-ai-usage"] = Stage(
            key="ui-ai-usage", name=STAGE_LABELS["ui-ai-usage"],
            command=[python, "scripts/integration/ai_usage_accept.py", "--base-url", base_url],
            cwd=root, requires=("ui-server",),
            description="AI 用量账本/配额 429/普通用户 403/管理台汇总（本机假模型，无真实费用）",
        )
        candidates["ui-upload"] = Stage(
            key="ui-upload", name=STAGE_LABELS["ui-upload"],
            command=[python, "scripts/ui_check_upload.py", "--base-url", base_url],
            cwd=root, requires=("ui-server",),
            description="拍题入库页（#/upload）桌面/手机视口渲染与入口核对",
        )
        candidates["ui-modules"] = Stage(
            key="ui-modules", name=STAGE_LABELS["ui-modules"],
            command=[python, "scripts/integration/modules_accept.py",
                     "--base-url", base_url, "--seed"],
            cwd=root, requires=("ui-server",),
            description="整合模块端到端（课堂/工坊/学情/任务/助手；先注册演示账号）",
        )

    order = [key for key in STAGE_LABELS if key in candidates]
    if selected:
        wanted = []
        for item in selected:
            key = item.strip()
            if key and key not in wanted:
                wanted.append(key)
        unknown = [key for key in wanted if key not in candidates]
        if unknown:
            raise UsageError(
                "未知阶段键：%s；可用阶段：%s" % (
                    ", ".join(unknown),
                    ", ".join(f"{key}（{STAGE_LABELS[key]}）" for key in candidates),
                )
            )
        active = set(wanted)
    else:
        active = set(order)

    stages: list[Stage] = []
    for key in order:
        if skip_pytest and key == "pytest":
            stages.append(_skip_stage(key, STAGE_LABELS[key], "显式跳过（-SkipPytest）"))
        elif key not in active:
            stages.append(_skip_stage(key, STAGE_LABELS[key], "未选择（-Stages 过滤，开发模式）"))
        else:
            stages.append(candidates[key])

    if not bank.ok:
        # 基线不可用时不拿错误的题库继续跑测试：bank 失败 + 其余阶段保留为 SKIP 记录。
        stages = [candidates["bank"]] + [
            stage if stage.key == "bank" else _skip_stage(
                stage.key, stage.name, "题库基线不可用，未执行")
            for stage in stages if stage.key != "bank"
        ]
    return stages


def _stage_environment(*, bank: BankInfo | None, data_dir: str | None) -> dict:
    """子进程环境：测试开关兜底 + 把验收认定的题库/数据目录显式传给被测进程。"""
    env = os.environ.copy()
    env.setdefault("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    env.setdefault("STUDY_ALLOWED_HOSTS", DEFAULT_ALLOWED_HOSTS)
    env.setdefault("STUDY_INDEX_PREWARM", "0")
    env.setdefault("PYTHONIOENCODING", "utf-8")
    if bank is not None and bank.ok:
        env["STUDY_BANK_DIR"] = str(bank.dir)
    if data_dir:
        env["STUDY_DATA_DIR"] = str(Path(data_dir).expanduser().resolve())
    return env


def _run_command(stage: Stage, *, bank: BankInfo | None, data_dir: str | None) -> StageResult:
    name = stage.name
    if not stage.command:
        return _stage_result(stage.key, name, "fail", detail="阶段未定义命令（漏跑）")
    missing = [tool for tool in stage.needs if shutil.which(tool) is None]
    if missing:
        return _stage_result(
            stage.key, name, "skip",
            detail="缺少工具：%s（阶段未执行，不能算成功）" % ", ".join(missing),
        )
    env = _stage_environment(bank=bank, data_dir=data_dir)
    command = stage.command
    for index, item in enumerate(command):
        if item in stage.needs:
            command = list(command)
            command[index] = _tool(item)
    try:
        completed = subprocess.run(
            command, cwd=str(stage.cwd) if stage.cwd else None, env=env,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=stage.timeout,
        )
    except subprocess.TimeoutExpired:
        return _stage_result(stage.key, name, "fail", detail=f"阶段超时（>{stage.timeout:.0f}s）")
    except OSError as exc:
        return _stage_result(stage.key, name, "fail", detail=f"无法启动命令：{exc}")
    stdout = (completed.stdout or "").strip()
    stderr = (completed.stderr or "").strip()
    if completed.returncode in stage.skip_returncodes:
        return _stage_result(
            stage.key, name, "skip",
            returncode=completed.returncode,
            detail=stage.skip_returncodes[completed.returncode],
        )
    if completed.returncode != 0:
        output = (stderr or stdout).splitlines()
        tail = "；".join(line.strip() for line in output[-6:] if line.strip())[-1200:]
        return _stage_result(
            stage.key, name, "fail", returncode=completed.returncode,
            detail=f"exit {completed.returncode}" + (f"：{tail}" if tail else ""),
        )
    lines = [line.strip() for line in stdout.splitlines() if line.strip()]
    summary = lines[-1] if lines else ""
    return _stage_result(stage.key, name, "ok", returncode=0, detail=summary[-500:])


def _blocked_by_requirement(stage: Stage, results: dict[str, StageResult]) -> str | None:
    for requirement in stage.requires:
        result = results.get(requirement)
        if result is None:
            # 前置阶段不在本次计划里（用户用 -Stages 显式排掉）时由用户自行承担。
            continue
        if result.status != "ok":
            return f"前置阶段未通过：{result.name}（{result.status}）"
    return None


def run_stages(
    stages: list[Stage],
    *,
    root: Path = ROOT,
    bank: BankInfo | None = None,
    base_url: str = DEFAULT_BASE_URL,
    ui: bool = False,
    build_id: str = "",
    report_path: str | Path | None = None,
    data_dir: str | None = None,
    out=None,
) -> dict:
    """顺序执行阶段并汇总；返回报告字典（含 exit_code）。失败/漏跑一定非零。"""
    out = out or sys.stdout
    results: list[StageResult] = []
    index: dict[str, StageResult] = {}
    for stage in stages:
        started = time.perf_counter()
        try:
            blocked = _blocked_by_requirement(stage, index)
            if blocked:
                result = _stage_result(stage.key, stage.name, "skip", detail=blocked)
            elif stage.runner is not None:
                result = stage.runner()
            else:
                result = _run_command(stage, bank=bank, data_dir=data_dir)
        except Exception as exc:  # 阶段异常必须成为失败，而不是中断脚本后“看起来没失败”
            result = _stage_result(
                stage.key, stage.name, "fail",
                detail=f"阶段异常：{type(exc).__name__}: {exc}",
            )
        result.duration_s = round(time.perf_counter() - started, 2)
        results.append(result)
        index[stage.key] = result
        _print_stage_result(result, out)

    summary = _summarize(
        results, bank=bank, build_id=build_id, base_url=base_url, ui=ui,
    )
    if report_path is not None:
        path = write_report(Path(report_path), summary)
        summary["report_path"] = str(path)
    _print_summary(summary, out)
    return summary


def _summarize(results: list[StageResult], *, bank: BankInfo | None, build_id: str,
               base_url: str, ui: bool) -> dict:
    """汇总判定（与方案 §3 P0 一致）：

    * 任一阶段 FAIL（含阶段异常、UI 服务器不可达）→ exit 1 / ``FAILED``；
    * 没有任何阶段真正执行（executed==0）→ exit 3 / ``INCOMPLETE``（漏跑不能算成功）；
    * 有 SKIP（工具缺失 / -SkipPytest / -Stages 过滤）→ exit 3 / ``INCOMPLETE``；
    * 只有全部选中阶段执行且全过（skipped==0）→ exit 0 / ``OK``。
    """
    failed = [result for result in results if result.status == "fail"]
    skipped = [result for result in results if result.status == "skip"]
    passed = [result for result in results if result.status == "ok"]
    executed = len(passed) + len(failed)
    if failed:
        exit_code, accept = 1, "FAILED"
    elif skipped or executed == 0:
        exit_code, accept = 3, "INCOMPLETE"
    else:
        exit_code, accept = 0, "OK"
    assert len(results) == len({result.key for result in results}), "阶段结果重复，汇总不可信"
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "ok": exit_code == 0,
        "accept": accept,
        "exit_code": exit_code,
        "build_id": build_id,
        "bank": bank.to_dict() if bank is not None else None,
        "base_url": base_url,
        "ui": ui,
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "counts": {
            "total": len(results),
            "ok": len(passed),
            "failed": len(failed),
            "skipped": len(skipped),
            "executed": executed,
        },
        "failed_stages": [result.key for result in failed],
        "skipped_stages": [result.key for result in skipped],
        "stages": [result.to_dict() for result in results],
    }


def write_report(path: Path, summary: dict) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def _print_stage_result(result: StageResult, out=sys.stdout) -> None:
    label = {"ok": "[OK]", "fail": "[FAIL]", "skip": "[SKIP]"}.get(result.status, "[??]")
    detail = f" - {result.detail}" if result.detail else ""
    print(f"{label} {result.name}（{result.duration_s:.2f}s）{detail}", file=out)


def _print_summary(summary: dict, out=sys.stdout) -> None:
    counts = summary["counts"]
    print("", file=out)
    print("阶段汇总：共 %(total)d 个，OK %(ok)d，FAIL %(failed)d，SKIP %(skipped)d" % counts, file=out)
    if summary["failed_stages"]:
        print("失败阶段：%s" % ", ".join(summary["failed_stages"]), file=out)
    if summary["skipped_stages"]:
        print("跳过阶段（未验证，不能算成功）：%s" % ", ".join(summary["skipped_stages"]), file=out)
    if summary.get("bank"):
        bank = summary["bank"]
        print("题库基线：%s（版本 %s，来源 %s）"
              % (bank["dir"], bank["bank_version"], bank["source"]), file=out)
    print("build_id：%s" % (summary.get("build_id") or "(未计算)"), file=out)
    if summary.get("report_path"):
        print("报告：%s" % summary["report_path"], file=out)
    if summary["accept"] == "OK":
        print("ACCEPT OK", file=out)
    else:
        # FAILED / INCOMPLETE：不再出现 ACCEPT OK（跳过阶段即未完成，必须非零）。
        print("ACCEPT %s" % summary["accept"], file=out)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="accept_runner.py",
        description="知序本机验收 runner（阶段编排/汇总/退出码，供 scripts/ci_accept.ps1 调用）",
    )
    parser.add_argument("--ui", action="store_true", help="追加浏览器/UI 验收（需要先启动隔离实例）")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help="UI 验收实例地址")
    parser.add_argument("--bank-dir", default="", help="显式题库目录（默认跟随 releases/CURRENT；r3 需显式指定）")
    parser.add_argument("--data-dir", default="", help="显式应用数据目录（默认用仓库 data/）")
    parser.add_argument("--skip-pytest", action="store_true", help="跳过全量 pytest（开发用，汇总记为 SKIP）")
    parser.add_argument("--stages", default="", help="只跑部分阶段，逗号分隔（如 bank,compileall）")
    parser.add_argument("--editor-build", action="store_true",
                        help="追加编辑器构建产物一致性检查（默认关闭，避免无 Node 环境无法验收）")
    parser.add_argument("--report-path", default=str(DEFAULT_REPORT_PATH), help="验收报告 JSON 落盘路径")
    parser.add_argument("--plan-json", action="store_true", help="只输出执行计划（含基线解析），不执行")
    parser.add_argument("--list-stages", action="store_true", help="列出阶段键与说明")
    parser.add_argument("--build-id", default="", help="指定 build_id（默认按内容指纹计算；测试用）")
    return parser.parse_args(argv)


def describe_plan(stages: list[Stage], *, bank: BankInfo, build_id: str,
                  base_url: str, ui: bool) -> dict:
    return {
        "build_id": build_id,
        "bank": bank.to_dict(),
        "base_url": base_url,
        "ui": ui,
        "stage_keys": [stage.key for stage in stages],
        "stages": [stage.to_plan() for stage in stages],
    }


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    root = ROOT
    report_path = Path(args.report_path) if args.report_path else None
    try:
        bank = resolve_bank(root, args.bank_dir or None)
        selected = [item for item in (args.stages or "").split(",") if item.strip()]
        stages = build_stages(
            root=root, bank=bank, ui=args.ui, base_url=args.base_url,
            skip_pytest=args.skip_pytest, selected=selected or None,
            editor_build=args.editor_build,
        )
    except UsageError as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return 2
    build_id = args.build_id or compute_build_id(root)
    if args.plan_json:
        print(json.dumps(
            describe_plan(stages, bank=bank, build_id=build_id,
                          base_url=args.base_url, ui=args.ui),
            ensure_ascii=False, indent=2,
        ))
        return 0
    if args.list_stages:
        for stage in stages:
            print(f"{stage.key}\t{stage.name}\t{stage.description}")
        return 0
    summary = run_stages(
        stages, root=root, bank=bank, base_url=args.base_url, ui=args.ui,
        build_id=build_id, report_path=report_path, data_dir=args.data_dir or None,
    )
    return int(summary["exit_code"])


if __name__ == "__main__":
    raise SystemExit(main())
