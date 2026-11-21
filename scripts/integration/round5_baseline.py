"""第五轮基线固定与样本核对（《知序功能整合与优化总方案》§8 工单 A）。

本脚本只读：只读打开题库快照与应用库快照，不改题库、不改应用数据；输出可回放的
JSON 基线报告与「数学知识点 + OCR 分层标注」抽检样本（人工审核状态一律标记为
``pending_human_review``，不伪造已审核结论）。

用法（仓库根执行）::

    python -X utf8 scripts/integration/round5_baseline.py
    python -X utf8 scripts/integration/round5_baseline.py --report screenshots/round5-baseline.json
    python -X utf8 scripts/integration/round5_baseline.py --samples-dir docs --date 20261121 --sample-kp 5
    python -X utf8 scripts/integration/round5_baseline.py --skip-build-id      # 快速模式（不 import 应用）

报告字段：
``ok``（全部强制项通过）、``checks[]``（逐项 ok/pending/fail，pending 表示需要人工或隔离实例
环境才能确认，不计失败）、``repo``、``environment``、``bank``、``app_data``、``samples``、
``tests``、``commands``。退出码：0=报告已写出（含 pending）；1=报告已写出但存在 fail；
2=参数/环境错误（题库快照缺失等）。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_BANK_ROOT = ROOT.parent / "高考题库_按学科"
DEFAULT_DATA_DIR = ROOT / "data"
DEFAULT_REPORT = ROOT / "screenshots" / "round5-baseline.json"
DEFAULT_SAMPLES_DIR = ROOT / "docs"
BANK_DB_NAME = "题库.db"
SAMPLE_KP_LIMIT = 5
CANDIDATES_PER_KP = 3
STEM_EXCERPT = 120

# 知识点名称里不适合直接当 LIKE 关键词的片段（通用词、题型词）。
_STOP_TERMS = {
    "函数", "问题", "方法", "应用", "及其", "与", "的", "和", "基本", "概念", "性质",
    "运算", "求解", "综合", "专题", "复习", "高考", "数学", "初步", "进阶", "基础",
}


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def ro_connect(path: Path) -> sqlite3.Connection:
    """只读打开 SQLite；调用方不得写入。"""
    return sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=10)


def table_names(con: sqlite3.Connection) -> set[str]:
    return {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def scalar(con: sqlite3.Connection, sql: str, params=(), default=None):
    try:
        row = con.execute(sql, params).fetchone()
    except sqlite3.Error:
        return default
    return row[0] if row and row[0] is not None else default


def count_table(con: sqlite3.Connection, name: str) -> int | None:
    try:
        return int(scalar(con, f'SELECT COUNT(*) FROM "{name}"', default=0) or 0)
    except sqlite3.Error:
        return None


def _slug(text: str) -> str:
    value = re.sub(r"[^\w\u4e00-\u9fff]+", "-", str(text or "").strip()).strip("-")
    return value or "未命名"


def _check(checks: list[dict], name: str, status: str, detail: str, **extra) -> None:
    entry = {"name": name, "status": status, "detail": detail}
    entry.update(extra)
    checks.append(entry)


# ---------------------------------------------------------------------------
# 仓库与环境
# ---------------------------------------------------------------------------


def _git(*args: str) -> tuple[str | None, str | None]:
    try:
        proc = subprocess.run(
            ["git", "-C", str(ROOT), *args], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as error:
        return None, f"git 不可用：{error}"
    if proc.returncode != 0:
        return None, (proc.stderr or proc.stdout or "").strip() or f"git 退出码 {proc.returncode}"
    return proc.stdout.strip(), None


def repo_state(checks: list[dict]) -> dict:
    head, head_error = _git("rev-parse", "HEAD")
    branch, _ = _git("branch", "--show-current")
    status_text, status_error = _git("status", "--porcelain")
    changed = [line for line in (status_text or "").splitlines() if line.strip()]
    state = {
        "root": str(ROOT),
        "head": head,
        "branch": branch or "",
        "dirty_files": len(changed),
        "dirty_sample": changed[:20],
        "error": head_error or status_error,
    }
    if head:
        _check(checks, "仓库 HEAD 可读", "ok", f"{branch or '?'} @ {head[:12]}，工作树改动 {len(changed)} 个文件")
    else:
        _check(checks, "仓库 HEAD 可读", "fail", f"无法读取 git HEAD：{head_error}")
    return state


def build_id(checks: list[dict], enabled: bool) -> dict:
    """通过子进程 import app.main 取构建指纹（与 /api/health 同口径）。"""
    if not enabled:
        _check(checks, "构建指纹", "pending", "已用 --skip-build-id 跳过；发布验收需与 /api/health 对齐")
        return {"id": None, "skipped": True, "error": None}
    env = dict(os.environ)
    env.setdefault("STUDY_ALLOWED_HOSTS", "127.0.0.1,localhost,[::1],testserver")
    try:
        proc = subprocess.run(
            [sys.executable, "-X", "utf8", "-c", "from app.main import BUILD_ID; print(BUILD_ID)"],
            cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=180, env=env,
        )
    except (OSError, subprocess.SubprocessError) as error:
        _check(checks, "构建指纹", "fail", f"无法计算构建指纹：{error}")
        return {"id": None, "skipped": False, "error": str(error)}
    if proc.returncode != 0 or not proc.stdout.strip():
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()[-3:]
        _check(checks, "构建指纹", "fail", "import app.main 失败：" + " / ".join(detail))
        return {"id": None, "skipped": False, "error": "\n".join(detail)}
    value = proc.stdout.strip().splitlines()[-1].strip()
    _check(checks, "构建指纹", "ok", f"build_id={value}")
    return {"id": value, "skipped": False, "error": None}


# ---------------------------------------------------------------------------
# 题库快照（只读）
# ---------------------------------------------------------------------------


def resolve_bank(bank_root: Path) -> tuple[Path | None, str, str | None]:
    """返回 (快照目录, 来源标记, 错误)。跟随 releases/CURRENT；缺失时回退根目录题库.db。"""
    if not bank_root.is_dir():
        return None, "missing", f"题库根目录不存在：{bank_root}"
    pointer = bank_root / "releases" / "CURRENT"
    if pointer.is_file():
        try:
            version = pointer.read_text(encoding="utf-8-sig").strip()
        except OSError as error:
            return None, "pointer", f"读取 releases/CURRENT 失败：{error}"
        candidate = bank_root / "releases" / version
        if version and (candidate / BANK_DB_NAME).is_file():
            return candidate, f"pointer:{version}", None
        return None, "pointer", f"releases/CURRENT 指向的发布包缺少 {BANK_DB_NAME}：{candidate}"
    if (bank_root / BANK_DB_NAME).is_file():
        return bank_root, "root", None
    return None, "missing", f"题库根目录下既没有 releases/CURRENT，也没有 {BANK_DB_NAME}：{bank_root}"


def bank_snapshot(bank_dir: Path, source: str, checks: list[dict]) -> dict:
    db_path = bank_dir / BANK_DB_NAME
    stat = db_path.stat()
    snapshot = {
        "root": str(bank_dir.parent if source.startswith("pointer:") else bank_dir),
        "dir": str(bank_dir),
        "source": source,
        "version_pointer": source.split(":", 1)[1] if source.startswith("pointer:") else "",
        "db_path": str(db_path),
        "db_size": stat.st_size,
        "db_mtime": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
        "tables": {},
        "bank_version": "",
        "math_questions": 0,
        "formula_questions": 0,
        "figure_questions": 0,
        "readonly_ok": False,
        "error": None,
    }
    try:
        con = ro_connect(db_path)
    except sqlite3.Error as error:
        snapshot["error"] = f"无法只读打开题库：{error}"
        _check(checks, "题库只读打开", "fail", snapshot["error"])
        return snapshot
    try:
        names = table_names(con)
        snapshot["tables"] = {name: count_table(con, name) for name in sorted(names)}
        snapshot["bank_version"] = str(scalar(con, "SELECT bank_version FROM questions LIMIT 1", default="") or "")
        columns = {row[1] for row in con.execute('PRAGMA table_info("questions")')}
        if "subject" in columns:
            snapshot["math_questions"] = int(
                scalar(con, "SELECT COUNT(*) FROM questions WHERE TRIM(subject)='数学'", default=0) or 0
            )
        for field, key in (("has_formula", "formula_questions"), ("has_figure", "figure_questions")):
            if field in columns:
                snapshot[key] = int(
                    scalar(
                        con,
                        f"SELECT COUNT(*) FROM questions WHERE subject='数学' "
                        f"AND CAST(NULLIF(TRIM({field}),'') AS INTEGER)=1",
                        default=0,
                    ) or 0
                )
        # 二次确认只读：mode=ro 连接写操作必须被拒绝。
        try:
            con.execute("CREATE TABLE IF NOT EXISTS __round5_write_probe (id INTEGER)")
            snapshot["readonly_ok"] = False
        except sqlite3.Error:
            snapshot["readonly_ok"] = True
    finally:
        con.close()
    _check(
        checks, "题库快照只读", "ok" if snapshot["readonly_ok"] else "fail",
        f"{source} · {snapshot['bank_version'] or '无版本号'} · 题目 {snapshot['tables'].get('questions')} · "
        f"数学 {snapshot['math_questions']} · 写探测 {'被拒绝' if snapshot['readonly_ok'] else '未被拒绝'}",
    )
    return snapshot


# ---------------------------------------------------------------------------
# 应用库快照（只读）
# ---------------------------------------------------------------------------


APP_TABLES = (
    "users", "knowledge_points", "question_kp", "attempts", "wrong_questions", "mastery",
    "pl_questions", "pl_paper_attempts", "cls_quiz_attempts", "ai_notes", "ai_documents",
    "plaza_submissions", "plaza_releases", "plz_releases",
    "study_evidence", "study_artifacts", "study_artifact_revisions", "study_artifact_links",
    "review_cards", "review_ratings",
)


def app_snapshot(data_dir: Path, checks: list[dict]) -> dict:
    db_path = data_dir / "app.sqlite3"
    snapshot = {
        "data_dir": str(data_dir),
        "db_path": str(db_path),
        "exists": db_path.is_file(),
        "tables": {},
        "knowledge_points_total": 0,
        "knowledge_points_by_subject": {},
        "question_kp_links": 0,
        "users": 0,
        "error": None,
    }
    if not db_path.is_file():
        _check(checks, "应用库快照", "pending", f"未找到 {db_path}（隔离实例尚未初始化时属正常）")
        return snapshot
    try:
        con = ro_connect(db_path)
    except sqlite3.Error as error:
        snapshot["error"] = f"无法只读打开应用库：{error}"
        _check(checks, "应用库快照", "fail", snapshot["error"])
        return snapshot
    try:
        names = table_names(con)
        snapshot["tables"] = {name: count_table(con, name) for name in APP_TABLES if name in names}
        if "knowledge_points" in names:
            snapshot["knowledge_points_total"] = count_table(con, "knowledge_points") or 0
            try:
                rows = con.execute(
                    "SELECT subject, COUNT(*) FROM knowledge_points GROUP BY subject ORDER BY subject"
                ).fetchall()
                snapshot["knowledge_points_by_subject"] = {str(row[0] or "未分类"): int(row[1]) for row in rows}
            except sqlite3.Error:
                snapshot["knowledge_points_by_subject"] = {}
        if "question_kp" in names:
            snapshot["question_kp_links"] = count_table(con, "question_kp") or 0
        if "users" in names:
            snapshot["users"] = count_table(con, "users") or 0
    finally:
        con.close()
    math_kp = snapshot["knowledge_points_by_subject"].get("数学", 0)
    _check(
        checks, "应用库快照只读", "ok",
        f"知识点 {snapshot['knowledge_points_total']}（数学 {math_kp}）· 题目知识点关联 "
        f"{snapshot['question_kp_links']} · 用户 {snapshot['users']}",
    )
    if snapshot["knowledge_points_total"] and not snapshot["question_kp_links"]:
        _check(
            checks, "知识点标签待补", "pending",
            "题库知识点关联为 0：掌握路径与 pyKT 标签需首批人工审核（本轮抽检样本见 samples）",
        )
    return snapshot


# ---------------------------------------------------------------------------
# 数学知识点抽检 + OCR 分层标注模板
# ---------------------------------------------------------------------------


def _kp_terms(name: str) -> list[str]:
    """知识点名称 → 召回词：整名 + 按「的/与/和/及」切开的短语（去通用词）。"""
    text = str(name or "").strip()
    terms: list[str] = []
    if 2 <= len(text) <= 24:
        terms.append(text)
    for part in re.split(r"[^\w\u4e00-\u9fff]+|的|与|和|及|、", text):
        value = part.strip()
        if len(value) < 2 or value in _STOP_TERMS or value in terms:
            continue
        terms.append(value)
    if not terms and text:
        terms = [text[:20]]
    return terms[:6]


def _excerpt(text: str, limit: int = STEM_EXCERPT) -> str:
    value = re.sub(r"\s+", " ", str(text or "")).strip()
    return value if len(value) <= limit else value[: limit - 1] + "…"


def _candidate_questions(con: sqlite3.Connection, terms: list[str], limit: int) -> list[dict]:
    """按召回词在只读题库中检索候选题；多词命中优先，其次公式题与可练题。"""
    if not terms:
        return []
    columns = {row[1] for row in con.execute('PRAGMA table_info("questions")')}
    wanted = [column for column in ("qid", "type", "stem_plain", "has_formula", "has_figure", "practice_status") if column in columns]
    if "qid" not in wanted:
        return []
    select = ", ".join(wanted)
    found: dict[str, dict] = {}
    for term in terms:
        try:
            rows = con.execute(
                f"SELECT {select} FROM questions WHERE subject='数学' AND stem_plain LIKE ? LIMIT ?",
                (f"%{term}%", limit * 8),
            ).fetchall()
        except sqlite3.Error:
            continue
        for row in rows:
            data = dict(zip(wanted, row))
            qid = str(data.get("qid") or "")
            if not qid:
                continue
            entry = found.setdefault(qid, {**data, "matched_terms": []})
            if term not in entry["matched_terms"]:
                entry["matched_terms"].append(term)
    if not found:
        return []

    def score(question: dict) -> tuple:
        return (
            -len(question.get("matched_terms") or []),
            0 if str(question.get("has_formula") or "0") in ("1", "true", "True") else 1,
            0 if str(question.get("practice_status") or "") == "practice" else 1,
            str(question.get("qid") or ""),
        )

    return sorted(found.values(), key=score)[:limit]


#: 题型 → 首批用途（诊断/巩固/测验候选）；只作候选标注，需人工确认。
_INTENDED_USE = {
    "single_choice": "诊断题候选",
    "multi_choice": "诊断题候选",
    "fill_blank": "诊断题候选",
    "short_answer": "巩固题候选",
}


def _ocr_layers(question: dict) -> dict:
    """OCR 分层标注模板：只声明预期层与人工审核状态，绝不自称已标注。"""
    has_formula = str(question.get("has_formula") or "0") in ("1", "true", "True")
    has_figure = str(question.get("has_figure") or "0") in ("1", "true", "True")
    return {
        "text": {
            "expected": True,
            "renderer": "stem_plain",
            "annotation_status": "pending_human_review",
            "note": "文字层：人工确认阅读顺序与换行是否与原件一致",
        },
        "math": {
            "expected": bool(has_formula),
            "renderer": "stem_blocks_json / fallback_asset",
            "annotation_status": "pending_human_review" if has_formula else "not_applicable",
            "note": "公式层：本轮本机引擎不做公式识别，需人工校对 LaTeX（MathLive 校对入口）",
        },
        "table": {
            "expected": "|" in str(question.get("stem_plain") or ""),
            "renderer": "blocks.table",
            "annotation_status": "pending_human_review",
            "note": "表格层：按单元格阅读顺序标注；无表格题为 not_applicable",
        },
        "figure": {
            "expected": bool(has_figure),
            "renderer": "images_json / 原图裁切",
            "annotation_status": "pending_human_review" if has_figure else "not_applicable",
            "note": "图形层：保留原图定位（bbox/页码），禁止重绘当作原件",
        },
    }


def sampled_math_knowledge_points(
    app_data: dict, bank_dir: Path | None, limit: int, checks: list[dict],
) -> list[dict]:
    db_path = Path(app_data["db_path"]) if app_data.get("exists") else None
    if not db_path or not db_path.is_file():
        _check(checks, "数学知识点抽检", "pending", "应用库不可读，无法抽取知识点样本")
        return []
    try:
        con = ro_connect(db_path)
    except sqlite3.Error as error:
        _check(checks, "数学知识点抽检", "fail", f"无法只读打开应用库：{error}")
        return []
    rows = []
    try:
        if "knowledge_points" not in table_names(con):
            _check(checks, "数学知识点抽检", "pending", "应用库没有 knowledge_points 表")
            return []
        rows = con.execute(
            "SELECT id, subject, parent_id, name, level, description FROM knowledge_points "
            "WHERE subject='数学' ORDER BY id"
        ).fetchall()
    finally:
        con.close()
    if not rows:
        _check(checks, "数学知识点抽检", "pending", "应用库没有数学知识点记录")
        return []

    # 抽样规则：优先取**叶子知识点**（无子节点，才是可映射到题目的细粒度标签），
    # 按父节点轮转保证覆盖不同章节；叶子不足时用其余节点按 level 由深到浅补齐。
    info = {
        int(row[0]): {
            "name": str(row[3] or ""), "parent_id": int(row[2]) if row[2] not in (None, "") else None,
            "level": int(row[4] or 0),
        }
        for row in rows
    }
    row_by_id = {int(row[0]): row for row in rows}
    parents = {item["parent_id"] for item in info.values() if item["parent_id"] is not None}
    leaves = [kp_id for kp_id in sorted(info) if kp_id not in parents]
    by_parent: dict[int | None, list[int]] = {}
    for kp_id in leaves:
        by_parent.setdefault(info[kp_id]["parent_id"], []).append(kp_id)
    ordered: list[int] = []
    parent_ids = sorted(by_parent, key=lambda value: (-1 if value is None else value))
    while len(ordered) < min(limit, len(leaves)) and by_parent:
        for parent_id in list(parent_ids):
            bucket = by_parent.get(parent_id) or []
            if bucket:
                ordered.append(bucket.pop(0))
            if len(ordered) >= min(limit, len(leaves)):
                break
        parent_ids = [key for key in parent_ids if by_parent.get(key)]
    if len(ordered) < min(limit, len(rows)):
        for kp_id in sorted(info, key=lambda value: (-info[value]["level"], value)):
            if kp_id not in ordered:
                ordered.append(kp_id)
            if len(ordered) >= min(limit, len(rows)):
                break

    def parent_path(kp_id: int) -> list[str]:
        path: list[str] = []
        current = info.get(kp_id, {}).get("parent_id")
        while current is not None and current in info and len(path) < 8:
            path.append(info[current]["name"])
            current = info[current]["parent_id"]
        return list(reversed(path))

    bank_con = None
    if bank_dir is not None and (bank_dir / BANK_DB_NAME).is_file():
        try:
            bank_con = ro_connect(bank_dir / BANK_DB_NAME)
        except sqlite3.Error:
            bank_con = None
    samples: list[dict] = []
    try:
        for kp_id in ordered:
            kp_id, subject, parent_id, name, level, description = row_by_id[kp_id]
            terms = _kp_terms(name)
            candidates = _candidate_questions(bank_con, terms, CANDIDATES_PER_KP) if bank_con else []
            samples.append({
                "kp_id": int(kp_id),
                "subject": str(subject or ""),
                "parent_id": int(parent_id) if parent_id not in (None, "") else None,
                "parent_path": parent_path(int(kp_id)),
                "name": str(name or ""),
                "level": str(level or ""),
                "description": _excerpt(description, 160),
                "search_terms": terms,
                "mapping_status": "pending_human_review",
                "quiz_candidate": "待按该知识点生成课堂测验题并人工审核（本轮无测验数据，不伪造）",
                "review_note": "候选题为只读题库关键词召回，尚未人工确认知识点映射；不得当作正式标签",
                "candidate_questions": [
                    {
                        "question_ref": f"bank:{candidate.get('qid')}",
                        "qid": candidate.get("qid"),
                        "type": candidate.get("type"),
                        "practice_status": candidate.get("practice_status"),
                        "intended_use": _INTENDED_USE.get(str(candidate.get("type") or ""), "课后巩固候选"),
                        "matched_terms": candidate.get("matched_terms", []),
                        "stem_excerpt": _excerpt(candidate.get("stem_plain")),
                        "ocr_layers": _ocr_layers(candidate),
                    }
                    for candidate in candidates
                ],
            })
    finally:
        if bank_con is not None:
            bank_con.close()

    with_candidates = sum(1 for item in samples if item["candidate_questions"])
    status = "ok" if samples and with_candidates == len(samples) else "pending"
    detail = (
        f"抽取 {len(samples)} 个数学知识点，其中 {with_candidates} 个有候选题；"
        "知识点映射与 OCR 分层标注均待人工审核"
    )
    _check(checks, "数学知识点抽检", status, detail)
    return samples


# ---------------------------------------------------------------------------
# 测试与验证入口清单
# ---------------------------------------------------------------------------


def test_inventory() -> dict:
    tests_dir = ROOT / "tests"
    files = sorted(tests_dir.glob("test_*.py"))
    functions = 0
    names: list[str] = []
    pattern = re.compile(r"^\s*(?:async\s+)?def\s+(test_\w+)", re.MULTILINE)
    for path in files:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        matches = pattern.findall(text)
        functions += len(matches)
        names.extend(matches)
    return {
        "files": len(files),
        "functions": functions,
        "baseline_command": "python -X utf8 -m pytest -q -p no:cacheprovider",
        "round5_focus": [
            "tests/test_study_evidence.py",
            "tests/test_study_artifact.py",
            "tests/test_study_review.py",
            "tests/test_ocr_blocks.py",
            "tests/test_round5_integration.py",
            "tests/test_question_refs.py",
            "tests/test_review_scheduling.py",
            "tests/test_ai_ocr.py",
            "tests/test_upload_intake.py",
            "tests/test_paperlab_attempt_freeze.py",
            "tests/test_classroom_interactive3d.py",
            "tests/test_plaza_public_assets.py",
            "tests/test_vector_versioning.py",
        ],
    }


def acceptance_boundaries(app_data: dict, checks: list[dict]) -> dict:
    """两账号 / 公开题撤回 / 冻结试卷样本的可核对状态（只读判定，不代替验收）。"""
    tables = app_data.get("tables") or {}
    plaza_tables = [name for name in tables if name.startswith("plaza_")]
    frozen = tables.get("pl_paper_attempts", 0) or 0
    users = app_data.get("users", 0) or 0
    boundaries = {
        "two_accounts": users >= 2,
        "public_withdrawal": bool(plaza_tables),
        "frozen_paper": frozen > 0,
        "note": "只读判定；真实两账号、撤回与冻结样本需在隔离实例按验收脚本准备",
    }
    for key, label in (("two_accounts", "两账号样本"), ("public_withdrawal", "公开题撤回样本"), ("frozen_paper", "冻结试卷样本")):
        if boundaries.get(key):
            _check(checks, label, "ok", "应用库快照已具备")
        else:
            _check(checks, label, "pending", "当前快照未具备；需隔离实例按验收脚本（scripts/integration）准备")
    return boundaries


# ---------------------------------------------------------------------------
# 输出
# ---------------------------------------------------------------------------


def write_samples(samples: list[dict], samples_dir: Path, date: str, report: dict) -> dict:
    samples_dir.mkdir(parents=True, exist_ok=True)
    json_path = samples_dir / f"r5-数学知识点抽检-{date}.json"
    md_path = samples_dir / f"r5-数学知识点抽检-{date}.md"
    payload = {
        "generated_at": report["generated_at"],
        "bank_version": report["bank"].get("version_pointer") or report["bank"].get("bank_version"),
        "head": report["repo"].get("head"),
        "status": "pending_human_review",
        "note": "本文件为只读脚本生成的抽检候选：知识点映射与 OCR 分层标注均待人工审核，不得当作已审核结论",
        "samples": samples,
    }
    lines = [
        f"# r5 数学知识点抽检与 OCR 分层标注（{date}）",
        "",
        f"- 生成时间：{report['generated_at']}",
        f"- 仓库 HEAD：`{report['repo'].get('head') or '未知'}` · 题库：`{payload['bank_version'] or '未知'}`",
        "- 状态：**待人工审核**。候选题由只读题库关键词召回，尚未确认知识点映射；OCR 各层仅标出预期与待标注项。",
        "",
        "| # | 知识点 | 层级 | 召回词 | 候选题数 | 映射状态 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for index, sample in enumerate(samples, 1):
        terms = "、".join(sample.get("search_terms") or []) or "—"
        lines.append(
            f"| {index} | {sample['name']}（id={sample['kp_id']}） | {sample.get('level') or '—'} | "
            f"{terms} | {len(sample.get('candidate_questions') or [])} | 待人工审核 |"
        )
    lines.extend(["", "## 候选题与 OCR 分层", ""])
    for sample in samples:
        lines.append(f"### {sample['name']}（`kp_id={sample['kp_id']}`）")
        if sample.get("parent_path"):
            lines.append(f"> 章节路径：{' › '.join(sample['parent_path'])}")
        if sample.get("description"):
            lines.append(f"> {sample['description']}")
        lines.append("")
        lines.append(f"- 课堂测验题：{sample.get('quiz_candidate')}")
        if not sample.get("candidate_questions"):
            lines.append("- 候选题：无（需人工补录或调整召回词）")
            lines.append("")
            continue
        lines.append("")
        lines.append("| 题目引用 | 题型 | 建议用途 | 文字层 | 公式层 | 表格层 | 图形层 |")
        lines.append("| --- | --- | --- | --- | --- | --- | --- |")
        for candidate in sample["candidate_questions"]:
            layers = candidate["ocr_layers"]

            def mark(layer: dict) -> str:
                if layer.get("annotation_status") == "not_applicable":
                    return "不适用"
                return "待人工标注" if layer.get("expected") else "待确认"

            lines.append(
                f"| `{candidate['question_ref']}` | {candidate.get('type') or '—'} | "
                f"{candidate.get('intended_use') or '—'} | "
                f"{mark(layers['text'])} | {mark(layers['math'])} | "
                f"{mark(layers['table'])} | {mark(layers['figure'])} |"
            )
        lines.append("")
        for candidate in sample["candidate_questions"]:
            lines.append(f"- `{candidate['question_ref']}`：{candidate.get('stem_excerpt') or '（无题干摘要）'}")
        lines.append("")
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"json": str(json_path), "markdown": str(md_path)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="第五轮基线固定与数学知识点/OCR 分层标注抽检（只读）")
    parser.add_argument("--bank-root", default=str(DEFAULT_BANK_ROOT), help="题库根目录（含 releases/CURRENT）")
    parser.add_argument("--data-dir", default=os.environ.get("STUDY_DATA_DIR") or str(DEFAULT_DATA_DIR),
                        help="应用数据目录（只读读取 app.sqlite3）")
    parser.add_argument("--report", default=str(DEFAULT_REPORT), help="JSON 基线报告输出路径")
    parser.add_argument("--samples-dir", default=str(DEFAULT_SAMPLES_DIR), help="知识点抽检样本输出目录")
    parser.add_argument("--date", default=datetime.now().strftime("%Y%m%d"), help="样本文件名日期后缀")
    parser.add_argument("--sample-kp", type=int, default=SAMPLE_KP_LIMIT, help="抽取的数学知识点个数（默认 5）")
    parser.add_argument("--skip-samples", action="store_true", help="不生成知识点抽检样本")
    parser.add_argument("--skip-build-id", action="store_true", help="跳过 import app.main 计算构建指纹")
    parser.add_argument("--quiet", action="store_true", help="只输出一行结论")
    args = parser.parse_args(argv)

    checks: list[dict] = []
    report: dict = {
        "generated_at": now_iso(),
        "scope": "第五轮 W0 基线（只读）",
        "ok": False,
        "checks": checks,
    }
    bank_dir, bank_source, bank_error = resolve_bank(Path(args.bank_root))
    if bank_dir is None:
        report["bank"] = {"dir": None, "source": bank_source, "error": bank_error}
        _check(checks, "题库快照定位", "fail", bank_error or "未找到题库快照")
        report["app_data"] = app_snapshot(Path(args.data_dir), checks) if Path(args.data_dir).is_dir() else {}
        report["samples"] = []
        report["tests"] = test_inventory()
        report["commands"] = {
            "baseline_report": "python -X utf8 scripts/integration/round5_baseline.py",
            "full_pytest": "python -X utf8 -m pytest -q -p no:cacheprovider",
            "accept": ".\\scripts\\ci_accept.ps1 -Ui -EditorBuild -BaseUrl http://127.0.0.1:8840 "
                      "-BankDir \"<固定的 r5 目录>\" -DataDir \"<候选隔离数据目录>\" -ReportPath \"<候选报告.json>\"",
        }
        _write_report(report, Path(args.report), args.quiet)
        return 2

    report["repo"] = repo_state(checks)
    report["environment"] = {
        "python": sys.version.split()[0],
        "executable": sys.executable,
        "platform": sys.platform,
        "data_dir": str(Path(args.data_dir)),
        "bank_root": str(Path(args.bank_root)),
    }
    report["build"] = build_id(checks, enabled=not args.skip_build_id)
    report["bank"] = bank_snapshot(bank_dir, bank_source, checks)
    app_data = app_snapshot(Path(args.data_dir), checks) if Path(args.data_dir).is_dir() else {}
    report["app_data"] = app_data
    samples = [] if args.skip_samples else sampled_math_knowledge_points(
        app_data, bank_dir, max(1, min(10, int(args.sample_kp))), checks,
    )
    report["acceptance_boundaries"] = acceptance_boundaries(app_data, checks)
    report["samples"] = samples
    report["tests"] = test_inventory()
    report["commands"] = {
        "baseline_report": "python -X utf8 scripts/integration/round5_baseline.py",
        "full_pytest": "python -X utf8 -m pytest -q -p no:cacheprovider",
        "focused_pytest": "python -X utf8 -m pytest -q -p no:cacheprovider " + " ".join(report["tests"]["round5_focus"][:6]),
        "accept": ".\\scripts\\ci_accept.ps1 -Ui -EditorBuild -BaseUrl http://127.0.0.1:8840 "
                  "-BankDir \"<固定的 r5 目录>\" -DataDir \"<候选隔离数据目录>\" -ReportPath \"<候选报告.json>\"",
    }
    if samples and not args.skip_samples:
        report["sample_files"] = write_samples(samples, Path(args.samples_dir), str(args.date), report)
    report["ok"] = all(check["status"] != "fail" for check in checks)
    _write_report(report, Path(args.report), args.quiet)
    failed = [check for check in checks if check["status"] == "fail"]
    return 0 if not failed else 1


def _write_report(report: dict, path: Path, quiet: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    path.write_text(text, encoding="utf-8")
    if quiet:
        print(f"[{'OK' if report.get('ok') else 'FAIL'}] {path}")
        return
    checks = report.get("checks") or []
    ok = sum(1 for check in checks if check["status"] == "ok")
    pending = sum(1 for check in checks if check["status"] == "pending")
    failed = [check for check in checks if check["status"] == "fail"]
    for check in checks:
        marker = {"ok": "OK", "pending": "PENDING", "fail": "FAIL"}.get(check["status"], "?")
        print(f"[{marker}] {check['name']}：{check['detail']}")
    print(f"\n基线报告：{path}")
    print(f"汇总：OK {ok}，PENDING {pending}，FAIL {len(failed)}")
    if report.get("sample_files"):
        print(f"抽检样本：{report['sample_files']['json']} · {report['sample_files']['markdown']}")
    print("结论：", "基线可用（pending 项需人工/隔离实例继续）" if not failed else "存在失败项，需先修复基线")


if __name__ == "__main__":
    raise SystemExit(main())
