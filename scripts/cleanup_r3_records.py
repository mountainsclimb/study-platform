#!/usr/bin/env python
"""r3 旧学习记录预检清理工具（P0）。

背景
----
题库发布指针 ``CURRENT`` 已指向 ``bank-20261121-r5``，r3（``bank-20261120-r3``）
仅作为审计对照保留在两份发布包中。应用库里可能残留只在 r3 能解析的作答、错题、
掌握度等学习记录；本工具生成**只读预检清单**，并在显式 ``--apply`` 时按
「经 r3 快照证实属于 r3 且 r5 无对应引用」的口径做单事务清理。

安全口径
--------
* 默认 ``--dry-run``：只用 URI ``mode=ro`` 只读打开应用库，不写任何文件（除显式
  ``--json`` 报告），不创建备份。
* ``--apply`` 必须显式指定；先自动调用 ``app.ops.create_backup``（或用
  ``--backup`` 指定已存在备份），再在**单事务**内删除，失败整体回滚。
* 只删除「学习记录」表中分类为 ``仅 r3 可解析`` 的行：
  ``attempts`` / ``mastery`` / ``wrong_questions`` / ``question_metadata`` /
  ``question_kp`` / ``question_method``。
  ``ai_explanations``、``content_issues``、``content_isolations`` 等用户/AI 产物
  只分类、不删除；未登记的 qid 关联表一律保守保留。
* 私人内容（``pl_questions``、``ai_uploads``、``ai_documents`` 等无 qid 列的
  表）永远不在删除范围内；apply 前后对全部非目标表与 r5 关联记录做计数 +
  SHA-256 快照比对，输出「保持不变」证据。
* PostgreSQL 模式（设置了 ``DATABASE_URL``）fail-closed：本机未验证 PG 的
  ``_Postgres`` 只读清理路径，工具直接拒绝并返回退出码 3，不做任何读写。
* 未知来源记录（qid 在两版题库都解析不到）保留并复核，不参与删除。

清理判定（按行 qid）
--------------------
* ``r5``：qid 在 r5 题目 qid 集合中 → 保留（当前基线）。
* ``r5_ref``：qid 命中 r5 的 ``source_question_id`` → 保留。
* ``r3_referenced_in_r5``：qid 在 r3 中，且 r3 行的 ``source_question_id``
  在 r5 中被引用（qid 或 source_question_id）→ 保留并复核。
* ``r3_only``：qid 仅在 r3 中，且 r3 行没有在 r5 找到对应引用 → 可清理。
* ``unknown``：两版都解析不到 → 保留并复核。

用法::

    cd study-platform
    python scripts/cleanup_r3_records.py --dry-run --json docs/r3-清理预检-20261121.json
    python scripts/cleanup_r3_records.py --apply                 # 自动备份后单事务清理
    python scripts/cleanup_r3_records.py --apply --backup backups/zhixu-xxx.zip

退出码：0 成功；1 输入/环境错误（含缺 r3 快照）；2 参数错误；3 PG fail-closed；
4 apply 被拒绝（备份不可用等，库未被修改）。
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    # 以 ``python scripts/cleanup_r3_records.py`` 运行时，保证能导入同仓 ``app`` 包。
    sys.path.insert(0, str(ROOT))

REPORT_SCHEMA = "zhixu.r3_cleanup_report.v1"

# 只有这些「学习记录 / 题库派生映射」表允许删除 r3_only 行。
LEARNING_RECORD_TABLES = (
    "attempts",
    "mastery",
    "wrong_questions",
    "question_metadata",
    "question_kp",
    "question_method",
)
# 带 qid 但不属于可清理学习记录的表：只分类、永远保留，交给人工复核。
KEEP_REVIEW_TABLES = (
    "ai_explanations",
    "content_issues",
    "content_isolations",
)
# 由当前题库摘要派生的索引/缓存表：不参与逐行分类（由应用启动时按当前题库刷新）。
DERIVED_TABLE_PREFIXES = ("bank_fts",)
DERIVED_TABLES = frozenset({"bank_leak_flags", "bank_search_meta", "bank_meta"})

# 私人内容表（永远不删；报告里单独列出计数）。
PRIVATE_TABLES = (
    "users",
    "sessions",
    "pl_questions",
    "pl_imports",
    "pl_import_items",
    "pl_pages",
    "pl_page_edits",
    "pl_papers",
    "pl_paper_items",
    "pl_paper_attempts",
    "pl_paper_answers",
    "pl_cards",
    "pl_card_scans",
    "ai_uploads",
    "ai_documents",
    "ai_notes",
    "note_versions",
    "upload_wrong_questions",
    "asst_conversations",
    "asst_messages",
    "cls_courses",
    "cls_scenes",
    "cls_assets",
    "cls_sessions",
    "cls_turns",
    "cls_quiz_attempts",
    "focus_sessions",
    "user_preferences",
    "recovery_codes",
)

CLASS_ORDER = ("r5", "r5_ref", "r3_referenced_in_r5", "r3_only", "unknown")
CLASS_LABELS = {
    "r5": "r5 可解析（保留）",
    "r5_ref": "r5 来源引用（保留）",
    "r3_referenced_in_r5": "r3 但 r5 有对应引用（保留，复核）",
    "r3_only": "仅 r3 可解析（学习记录可清理）",
    "unknown": "未知来源（保留，复核）",
}
SAMPLE_LIMIT = 5
REVIEW_QUEUE_LIMIT = 200


class CleanupError(RuntimeError):
    """输入/环境错误：报告写出后以退出码 1 结束。"""


class PostgresUnsupported(RuntimeError):
    """PostgreSQL 模式 fail-closed：本机未验证，拒绝读写。"""


class BackupRefused(CleanupError):
    """apply 前的备份不可用：退出码 4，应用库未被修改。"""


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def _open_sqlite(path: Path, *, readonly: bool) -> sqlite3.Connection:
    path = Path(path).resolve()
    if not path.is_file():
        raise CleanupError(f"应用库不存在：{path}")
    if readonly:
        return sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=10)
    con = sqlite3.connect(str(path), timeout=30)
    con.execute("PRAGMA busy_timeout=30000")
    return con


def _db_file_info(path: Path) -> dict:
    path = Path(path).resolve()
    try:
        stat = path.stat()
    except OSError as exc:
        raise CleanupError(f"应用库不可访问：{path}（{exc}）") from None
    info = {"path": str(path), "size_bytes": stat.st_size, "mtime_ns": stat.st_mtime_ns}
    info["mtime_utc"] = datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(
        timespec="seconds"
    )
    return info


def _journal_mode(con) -> str:
    try:
        return str(con.execute("PRAGMA journal_mode").fetchone()[0])
    except sqlite3.Error:
        return "unknown"


# --------------------------------------------------------------------------- #
# 题库快照加载
# --------------------------------------------------------------------------- #
def load_bank_snapshot(release_dir: Path, label: str) -> dict:
    """只读加载一份发布包的 qid / source_question_id 身份集合。

    返回 ``{"path", "db_path", "version", "rows", "qids", "source_ids",
    "source_by_qid"}``。发布包缺少 ``题目.db`` 或 questions 表时抛 CleanupError，
    让调用方 fail-closed（缺少 r3 证据就不允许清理）。
    """
    release_dir = Path(release_dir).resolve()
    db_file = release_dir / "题库.db"
    if not db_file.is_file():
        raise CleanupError(f"{label} 发布包缺少 题库.db：{db_file}")
    con = sqlite3.connect(db_file.as_uri() + "?mode=ro", uri=True, timeout=10)
    try:
        tables = {
            row[0]
            for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if "questions" not in tables:
            raise CleanupError(f"{label} 发布包没有 questions 表：{db_file}")
        columns = [row[1] for row in con.execute('PRAGMA table_info("questions")')]
        if "qid" not in columns:
            raise CleanupError(f"{label} 发布包 questions 表缺少 qid 列：{db_file}")
        select = ["qid"]
        select.append("source_question_id" if "source_question_id" in columns else "NULL")
        select.append("question_id" if "question_id" in columns else "NULL")
        select.append("bank_version" if "bank_version" in columns else "NULL")
        rows = list(con.execute(f'SELECT {", ".join(select)} FROM questions'))
    finally:
        con.close()

    qids: set[str] = set()
    source_ids: set[str] = set()
    source_by_qid: dict[str, str] = {}
    versions: set[str] = set()
    for qid, source_question_id, question_id, bank_version in rows:
        qid = (qid or "").strip()
        if not qid:
            continue
        qids.add(qid)
        source = (source_question_id or question_id or "").strip()
        if source:
            source_ids.add(source)
            source_by_qid[qid] = source
        if bank_version:
            versions.add(str(bank_version))
    if not qids:
        raise CleanupError(f"{label} 发布包 questions 表为空：{db_file}")
    return {
        "label": label,
        "path": str(release_dir),
        "db_path": str(db_file),
        "version": ",".join(sorted(versions)) if versions else "unknown",
        "rows": len(rows),
        "qids": qids,
        "source_ids": source_ids,
        "source_by_qid": source_by_qid,
    }


def _bank_summary(snapshot: dict) -> dict:
    return {
        "path": snapshot["path"],
        "db_path": snapshot["db_path"],
        "version": snapshot["version"],
        "questions": snapshot["rows"],
        "distinct_qids": len(snapshot["qids"]),
        "distinct_source_ids": len(snapshot["source_ids"]),
    }


def validate_bank_pair(r3: dict, r5: dict) -> None:
    """确认 --r3 确实是旧 r3 快照，避免把 r5/其他包当成「旧证据」误删。"""
    r3_identity = f"{r3['version']} {Path(r3['path']).name}".lower()
    r5_identity = f"{r5['version']} {Path(r5['path']).name}".lower()
    if "r3" not in r3_identity:
        raise CleanupError(
            f"--r3 指向的发布包不像 r3 旧快照（version={r3['version']!r}，"
            f"path={r3['path']}）；缺少旧快照证据时拒绝清理"
        )
    if r3["version"] == r5["version"] or r3["qids"] == r5["qids"]:
        raise CleanupError(
            f"r3 与 r5 快照相同（version={r3['version']!r}），拒绝清理；"
            "请确认 --r3 / --r5 指向不同发布包"
        )
    if "r3" in r5_identity:
        raise CleanupError(f"--r5 指向的发布包看起来是 r3 而非当前基线：{r5['path']}")


def resolve_bank_dirs(args) -> tuple[Path, Path, Path]:
    """解析 (bank_root, r3_dir, r5_dir)。

    r5 默认跟随 ``releases/CURRENT``；r3 默认取 ``releases/bank-*-r3`` 中字典序
    最后一个。找不到 r3 时抛 CleanupError（没有旧快照证据就不能清理）。
    """
    env_root = os.environ.get("STUDY_BANK_DIR", "").strip()
    bank_root = Path(args.bank_root).resolve() if args.bank_root else (
        Path(env_root).resolve() if env_root else (ROOT.parent / "高考题库_按学科").resolve()
    )
    if (bank_root / "题库.db").is_file() and bank_root.parent.name == "releases":
        # 兼容把 STUDY_BANK_DIR 指向某个具体发布包：回到 releases 的父目录。
        bank_root = bank_root.parent.parent
    releases = bank_root / "releases"

    if args.r5:
        r5_dir = Path(args.r5).resolve()
    else:
        pointer = releases / "CURRENT"
        if not pointer.is_file():
            raise CleanupError(f"未找到题库指针 {pointer}，请用 --r5 指定 r5 发布包")
        version = pointer.read_text(encoding="utf-8-sig").strip()
        r5_dir = releases / version
        if not (r5_dir / "题库.db").is_file():
            raise CleanupError(f"题库指针指向的 r5 发布包不存在：{r5_dir}")

    if args.r3:
        r3_dir = Path(args.r3).resolve()
    else:
        candidates = sorted(
            (path for path in releases.glob("bank-*-r3") if (path / "题库.db").is_file()),
            key=lambda path: path.name,
        )
        if not candidates:
            raise CleanupError(
                f"未找到 r3 发布包（{releases}/bank-*-r3），请用 --r3 指定旧快照；"
                "缺少 r3 证据时拒绝清理"
            )
        r3_dir = candidates[-1]
    return bank_root, r3_dir, r5_dir


# --------------------------------------------------------------------------- #
# 应用库扫描与分类
# --------------------------------------------------------------------------- #
def classify_qid(qid, r3: dict, r5: dict) -> str:
    """按行 qid 返回分类（见模块 docstring 的清理判定）。"""
    if not isinstance(qid, str):
        return "unknown"
    value = qid.strip()
    if not value:
        return "unknown"
    if value in r5["qids"]:
        return "r5"
    if value in r5["source_ids"]:
        return "r5_ref"
    if value in r3["qids"]:
        source = r3["source_by_qid"].get(value, "")
        if source and (source in r5["qids"] or source in r5["source_ids"]):
            return "r3_referenced_in_r5"
        return "r3_only"
    return "unknown"


def _list_tables(con) -> list[str]:
    rows = con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    )
    names = []
    for (name,) in rows:
        if name in DERIVED_TABLES or str(name).startswith(DERIVED_TABLE_PREFIXES):
            continue
        names.append(str(name))
    return names


def _table_columns(con, table: str) -> list[str]:
    return [row[1] for row in con.execute(f'PRAGMA table_info("{table}")')]


def _primary_key_columns(con, table: str) -> list[str]:
    info = list(con.execute(f'PRAGMA table_info("{table}")'))
    ordered = sorted((row[5], row[1]) for row in info if row[5])
    return [name for _, name in ordered]


def _table_policy(table: str) -> tuple[str, str]:
    if table in LEARNING_RECORD_TABLES:
        return "learning_record", "学习记录/题库派生映射：仅清理分类为 r3_only 的行"
    if table in KEEP_REVIEW_TABLES:
        return "keep_for_review", "用户/AI 产物或内容反馈：只分类，不删除，交人工复核"
    return "unknown_qid_table_keep_for_review", "未登记的 qid 关联表：保守保留，需人工复核"


def scan_database(con, r3: dict, r5: dict) -> dict:
    """扫描所有以 qid 直接关联的表并分类。

    返回 ``{"tables": [...], "totals": {...}, "deletable": {table: [qid]},
    "r5_linked": [...], "review_queue": [...], "warnings": [...]}``。
    不写库；示例只含主键与 qid，不含作答/反馈正文等隐私内容。
    """
    scanned_tables: list[dict] = []
    totals = Counter({key: 0 for key in CLASS_ORDER})
    totals.update({"rows_scanned": 0, "scanned_tables": 0})
    deletable: dict[str, list[str]] = {}
    r5_linked: list[str] = []
    review: dict[str, dict] = {}
    warnings: list[str] = []

    for table in _list_tables(con):
        columns = _table_columns(con, table)
        if "qid" not in columns:
            continue
        policy, reason = _table_policy(table)
        pk_columns = _primary_key_columns(con, table)
        if pk_columns:
            rows = list(con.execute(f'SELECT * FROM "{table}"'))
            qid_index = columns.index("qid")
            key_indexes = [columns.index(name) for name in pk_columns]
            key_names = pk_columns
        else:
            # 无显式主键的旧表/新表：退回 rowid 作为示例主键。
            rows = list(con.execute(f'SELECT rowid, * FROM "{table}"'))
            qid_index = columns.index("qid") + 1
            key_indexes = [0]
            key_names = ["rowid"]
        classes = Counter({key: 0 for key in CLASS_ORDER})
        samples: dict[str, list[dict]] = {key: [] for key in CLASS_ORDER}
        table_deletable: list[str] = []
        for row in rows:
            qid = row[qid_index] if qid_index < len(row) else None
            klass = classify_qid(qid, r3, r5)
            classes[klass] += 1
            key_values = [row[index] for index in key_indexes]
            if len(samples[klass]) < SAMPLE_LIMIT:
                samples[klass].append(
                    {
                        "keys": {name: value for name, value in zip(key_names, key_values)},
                        "qid": qid,
                    }
                )
            if klass in ("r5", "r5_ref"):
                r5_linked.append(
                    json.dumps([table, key_values, qid], ensure_ascii=False, default=str)
                )
            elif klass in ("unknown", "r3_referenced_in_r5"):
                bucket = review.setdefault(
                    str(qid), {"qid": qid, "classes": set(), "tables": Counter()}
                )
                bucket["classes"].add(klass)
                bucket["tables"][table] += 1
            if klass == "r3_only" and policy == "learning_record":
                table_deletable.append(str(qid))

        deletable_rows = 0
        if policy == "learning_record":
            table_deletable = sorted(set(table_deletable))
            deletable[table] = table_deletable
            deletable_rows = classes["r3_only"]
        else:
            if classes["r3_only"]:
                warnings.append(
                    f"{table} 有 {classes['r3_only']} 行仅 r3 可解析，但该表策略为"
                    f"「{reason}」（{policy}），apply 不会删除这些行，需人工复核"
                )
                for sample in samples["r3_only"]:
                    bucket = review.setdefault(
                        str(sample.get("qid")),
                        {"qid": sample.get("qid"), "classes": set(), "tables": Counter()},
                    )
                    bucket["classes"].add("r3_only_keep_policy")
                    bucket["tables"][table] += 1
        for key in CLASS_ORDER:
            totals[key] += classes[key]
        totals["rows_scanned"] += len(rows)
        totals["scanned_tables"] += 1
        scanned_tables.append(
            {
                "table": table,
                "policy": policy,
                "policy_reason": reason,
                "rows": len(rows),
                "classes": {key: classes[key] for key in CLASS_ORDER},
                "deletable_rows": deletable_rows,
                "primary_key": key_names,
                "sample_keys": {key: samples[key] for key in CLASS_ORDER if samples[key]},
            }
        )

    review_queue = []
    for qid, bucket in list(review.items())[:REVIEW_QUEUE_LIMIT]:
        review_queue.append(
            {
                "qid": qid,
                "classes": sorted(bucket["classes"]),
                "tables": dict(bucket["tables"]),
            }
        )
    if len(review) > REVIEW_QUEUE_LIMIT:
        warnings.append(
            f"复核队列超过 {REVIEW_QUEUE_LIMIT} 条，报告只保留前 {REVIEW_QUEUE_LIMIT} 条，"
            "完整清理需按表导出"
        )

    deletable_total = sum(len(qids) for qids in deletable.values())
    kept_r3_only_review = totals["r3_only"] - deletable_total
    totals["deletable_total"] = deletable_total
    totals["kept_r3_only_review"] = kept_r3_only_review
    totals["unknown_total"] = totals["unknown"]
    totals["r5_related_total"] = totals["r5"] + totals["r5_ref"]
    return {
        "tables": scanned_tables,
        "totals": dict(totals),
        "deletable": deletable,
        "r5_linked": sorted(r5_linked),
        "review_queue": review_queue,
        "warnings": warnings,
    }


# --------------------------------------------------------------------------- #
# 保留证据（apply 前后比对）
# --------------------------------------------------------------------------- #
def _row_digest_payload(row) -> bytes:
    return (
        json.dumps(
            [value if not isinstance(value, bytes) else value.hex() for value in row],
            ensure_ascii=False,
            default=str,
            separators=(",", ":"),
        )
    ).encode("utf-8") + b"\n"


def _table_digest(con, table: str) -> dict:
    digest = hashlib.sha256()
    rows = 0
    for row in con.execute(f'SELECT * FROM "{table}"'):
        digest.update(_row_digest_payload(row))
        rows += 1
    return {"rows": rows, "sha256": digest.hexdigest()}


def retention_snapshot(con, scan: dict) -> dict:
    """非目标表 + r5 关联记录的计数/哈希快照（不含任何正文，只输出摘要）。"""
    tables: dict[str, dict] = {}
    for table in _list_tables(con):
        columns = _table_columns(con, table)
        if "qid" in columns:
            # qid 关联表属于分类/目标范围，用 r5_linked 单独取证。
            continue
        tables[table] = _table_digest(con, table)
    linked_digest = hashlib.sha256()
    for line in scan["r5_linked"]:
        linked_digest.update(line.encode("utf-8") + b"\n")
    lines = [f"{name}:{info['rows']}:{info['sha256']}" for name, info in sorted(tables.items())]
    lines.append(f"r5_linked:{len(scan['r5_linked'])}:{linked_digest.hexdigest()}")
    overall = hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()
    return {
        "tables": tables,
        "r5_linked": {"rows": len(scan["r5_linked"]), "sha256": linked_digest.hexdigest()},
        "overall_sha256": overall,
    }


def retention_evidence(snapshot: dict) -> dict:
    """把完整快照裁剪成报告用摘要（保留每表行数与哈希、r5 关联摘要）。"""
    return {
        "non_target_tables": {
            name: {"rows": info["rows"], "sha256": info["sha256"]}
            for name, info in sorted(snapshot["tables"].items())
        },
        "r5_linked": snapshot["r5_linked"],
        "overall_sha256": snapshot["overall_sha256"],
    }


# --------------------------------------------------------------------------- #
# 备份（apply 前置条件）
# --------------------------------------------------------------------------- #
def prepare_backup(args, data_dir: Path, db_path: Path) -> dict:
    """apply 前准备可恢复备份；不可用时抛 BackupRefused（库未修改）。"""
    if args.backup:
        provided = Path(args.backup).resolve()
        if not provided.is_file():
            raise BackupRefused(f"指定的备份不存在：{provided}")
        if provided.stat().st_size == 0:
            raise BackupRefused(f"指定的备份为空文件：{provided}")
        info = {
            "mode": "provided",
            "path": str(provided),
            "size_bytes": provided.stat().st_size,
            "sha256": _sha256_file(provided),
        }
        if args.verify_backup:
            from app import ops

            try:
                info["verified"] = ops.verify_backup(provided)
            except Exception as exc:  # noqa: BLE001 - 备份校验失败必须拒绝 apply
                raise BackupRefused(f"备份校验失败：{exc}") from exc
        return info

    expected = (Path(data_dir) / "app.sqlite3").resolve()
    if Path(db_path).resolve() != expected:
        raise BackupRefused(
            "自动备份只支持标准布局 <data-dir>/app.sqlite3；"
            "请用 --backup 指定已存在备份，或改用 --data-dir"
        )
    from app import ops

    try:
        created = ops.create_backup(data_dir=Path(data_dir))
    except Exception as exc:  # noqa: BLE001 - 备份失败必须拒绝 apply
        raise BackupRefused(f"自动备份失败：{exc}") from exc
    created["mode"] = "auto"
    return created


# --------------------------------------------------------------------------- #
# 报告与主流程
# --------------------------------------------------------------------------- #
def _write_report(report: dict, path: Path | None) -> None:
    if path is None:
        return
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )


def _summary_lines(report: dict) -> list[str]:
    classification = report.get("classification") or {}
    totals = classification.get("totals") or {}
    lines = [
        f"[{report.get('mode')}] status={report.get('status')} db={report.get('app_db', {}).get('path')}",
        "分类：r5={r5} r5_ref={r5_ref} r3_ref_in_r5={r3_referenced_in_r5} r3_only={r3_only} unknown={unknown}".format(
            r5=totals.get("r5", 0),
            r5_ref=totals.get("r5_ref", 0),
            r3_referenced_in_r5=totals.get("r3_referenced_in_r5", 0),
            r3_only=totals.get("r3_only", 0),
            unknown=totals.get("unknown", 0),
        ),
        f"预计可清理 {totals.get('deletable_total', 0)} 行；保留待复核 {totals.get('unknown_total', 0) + totals.get('kept_r3_only_review', 0)} 行",
    ]
    apply_info = report.get("apply") or {}
    if apply_info.get("performed"):
        deleted = apply_info.get("deleted_by_table") or {}
        lines.append(
            "实际删除 {total} 行：{detail}".format(
                total=apply_info.get("deleted_total", 0),
                detail=", ".join(f"{table}={count}" for table, count in sorted(deleted.items())) or "无",
            )
        )
        evidence = apply_info.get("preservation") or {}
        lines.append(f"保留校验不变：{bool(evidence.get('unchanged'))}")
    recommendation = report.get("recommendation") or {}
    if recommendation.get("notes"):
        lines.append("建议：")
        lines.extend(f"- {note}" for note in recommendation["notes"])
    for warning in report.get("warnings") or []:
        lines.append(f"警告：{warning}")
    return lines


def _base_report(mode: str, *, dry_run: bool) -> dict:
    return {
        "schema": REPORT_SCHEMA,
        "generated_at": _utcnow(),
        "mode": mode,
        "dry_run": dry_run,
        "status": "ok",
        "class_labels": CLASS_LABELS,
        "policy": {
            "learning_record_tables": list(LEARNING_RECORD_TABLES),
            "keep_review_tables": list(KEEP_REVIEW_TABLES),
            "private_tables_never_deleted": list(PRIVATE_TABLES),
            "derived_tables_skipped": sorted(DERIVED_TABLES)
            + [f"{prefix}*" for prefix in DERIVED_TABLE_PREFIXES],
            "rule": "只删除「qid 仅在 r3 且 r5 无对应引用」的学习记录；未知来源与私人内容保留",
        },
        "banks": {},
        "app_db": {},
        "classification": {},
        "private_preserved": [],
        "retention_evidence": {},
        "apply": {"performed": False},
        "recommendation": {},
        "warnings": [],
    }


def _private_preserved(con) -> list[dict]:
    result = []
    names = set(_list_tables(con))
    for table in PRIVATE_TABLES:
        if table not in names:
            continue
        rows = con.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
        result.append({"table": table, "rows": rows, "preserved": True})
    return result


def _current_pointer(bank_root: Path) -> str:
    pointer = bank_root / "releases" / "CURRENT"
    try:
        return pointer.read_text(encoding="utf-8-sig").strip()
    except OSError:
        return ""


def _recommendation(totals: dict, warnings: list[str]) -> dict:
    deletable = int(totals.get("deletable_total", 0))
    unknown = int(totals.get("unknown_total", 0))
    kept_review = int(totals.get("kept_r3_only_review", 0))
    notes = []
    if deletable:
        notes.append(
            f"先复核报告中 deletable 清单（{deletable} 行的示例主键），确认均属旧 r3 学习记录"
        )
        notes.append("apply 前由运维/lead 确认可恢复备份；工具在 --apply 时会先自动备份")
        notes.append("apply 为单事务删除，二次运行应删除 0 行（幂等）")
    else:
        notes.append("当前没有可清理的 r3 学习记录，无需 apply")
    if unknown:
        notes.append(f"存在 {unknown} 行未知来源记录：保留，需人工复核后再决定")
    if kept_review:
        notes.append(
            f"有 {kept_review} 行 r3_only 位于不可删除的复核表（AI 产物/内容反馈），需人工处理"
        )
    blocking = [warning for warning in warnings if "未登记" in warning]
    return {
        "safe_to_apply": bool(deletable) and not blocking,
        "requires_operator_review": True,
        "deletable_total": deletable,
        "unknown_total": unknown,
        "kept_r3_only_review": kept_review,
        "notes": notes,
    }


def run_dry_run(args, report: dict) -> int:
    db_path = Path(args.db).resolve() if args.db else (Path(args.data_dir) / "app.sqlite3")
    bank_root, r3_dir, r5_dir = resolve_bank_dirs(args)
    r3 = load_bank_snapshot(r3_dir, "r3")
    r5 = load_bank_snapshot(r5_dir, "r5")
    validate_bank_pair(r3, r5)
    report["banks"] = {
        "bank_root": str(bank_root),
        "current_pointer": _current_pointer(bank_root),
        "r3": _bank_summary(r3),
        "r5": _bank_summary(r5),
    }
    before = _db_file_info(db_path)
    con = _open_sqlite(db_path, readonly=True)
    try:
        before["journal_mode"] = _journal_mode(con)
        scan = scan_database(con, r3, r5)
        snapshot = retention_snapshot(con, scan)
        report["classification"] = {
            "tables": scan["tables"],
            "totals": scan["totals"],
            "deletable_by_table": {
                table: len(qids) for table, qids in sorted(scan["deletable"].items()) if qids
            },
            "review_queue": scan["review_queue"],
        }
        report["private_preserved"] = _private_preserved(con)
        report["retention_evidence"] = retention_evidence(snapshot)
    finally:
        con.close()
    after = _db_file_info(db_path)
    report["app_db"] = after
    report["app_db"]["journal_mode"] = before.get("journal_mode")
    report["app_db"]["unchanged_by_dry_run"] = (
        before["size_bytes"] == after["size_bytes"]
        and before["mtime_ns"] == after["mtime_ns"]
    )
    report["warnings"].extend(scan["warnings"])
    report["recommendation"] = _recommendation(scan["totals"], report["warnings"])
    report["status"] = "ok"
    _write_report(report, Path(args.json) if args.json else None)
    for line in _summary_lines(report):
        print(line)
    if args.json:
        print(f"报告已写入：{Path(args.json).resolve()}")
    return 0


def run_apply(args, report: dict) -> int:
    db_path = Path(args.db).resolve() if args.db else (Path(args.data_dir) / "app.sqlite3")
    data_dir = Path(args.data_dir).resolve() if args.data_dir else db_path.parent
    bank_root, r3_dir, r5_dir = resolve_bank_dirs(args)
    r3 = load_bank_snapshot(r3_dir, "r3")
    r5 = load_bank_snapshot(r5_dir, "r5")
    validate_bank_pair(r3, r5)
    report["banks"] = {
        "bank_root": str(bank_root),
        "current_pointer": _current_pointer(bank_root),
        "r3": _bank_summary(r3),
        "r5": _bank_summary(r5),
    }

    # 1) 先用只读连接出计划：没有可删行时直接幂等返回，不创建备份。
    con = _open_sqlite(db_path, readonly=True)
    try:
        plan = scan_database(con, r3, r5)
        report["private_preserved"] = _private_preserved(con)
        report["app_db"] = _db_file_info(db_path)
        report["app_db"]["journal_mode"] = _journal_mode(con)
    finally:
        con.close()
    report["classification"] = {
        "tables": plan["tables"],
        "totals": plan["totals"],
        "deletable_by_table": {
            table: len(qids) for table, qids in sorted(plan["deletable"].items()) if qids
        },
        "review_queue": plan["review_queue"],
    }
    report["warnings"].extend(plan["warnings"])
    report["recommendation"] = _recommendation(plan["totals"], report["warnings"])
    if not plan["totals"]["deletable_total"]:
        report["status"] = "applied_noop"
        report["apply"] = {
            "performed": True,
            "deleted_by_table": {},
            "deleted_total": 0,
            "backup": None,
            "note": "没有可清理的 r3 学习记录，未创建备份、未写库（幂等）",
            "preservation": {"unchanged": True, "reason": "未执行删除"},
        }
        report["app_db"]["unchanged_by_apply"] = True
        _write_report(report, Path(args.json) if args.json else None)
        for line in _summary_lines(report):
            print(line)
        return 0

    # 2) 备份前置：不可用则拒绝，库保持原样。
    try:
        backup = prepare_backup(args, data_dir, db_path)
    except BackupRefused as exc:
        report["status"] = "apply_refused"
        report["apply"] = {"performed": False, "reason": str(exc), "backup": None}
        report["error"] = str(exc)
        _write_report(report, Path(args.json) if args.json else None)
        print(f"拒绝 apply：{exc}", file=sys.stderr)
        return 4

    # 3) 单事务删除 + 保留证据比对。
    db_sha256_before = _sha256_file(db_path)
    con = _open_sqlite(db_path, readonly=False)
    try:
        con.execute("BEGIN IMMEDIATE")
        try:
            fresh = scan_database(con, r3, r5)
            before_snapshot = retention_snapshot(con, fresh)
            deleted_by_table: dict[str, int] = {}
            kept_rows_by_table: dict[str, int] = {}
            for table in LEARNING_RECORD_TABLES:
                qids = fresh["deletable"].get(table) or []
                expected = next(
                    (
                        item["classes"]["r3_only"]
                        for item in fresh["tables"]
                        if item["table"] == table
                    ),
                    0,
                )
                if not qids:
                    continue
                marks = ",".join("?" for _ in qids)
                cursor = con.execute(f'DELETE FROM "{table}" WHERE qid IN ({marks})', qids)
                if cursor.rowcount != expected:
                    raise CleanupError(
                        f"{table} 删除行数不一致（预期 {expected}，实际 {cursor.rowcount}），已回滚"
                    )
                deleted_by_table[table] = int(cursor.rowcount)
            for table in LEARNING_RECORD_TABLES:
                total_rows = next(
                    (item["rows"] for item in fresh["tables"] if item["table"] == table), 0
                )
                kept_rows_by_table[table] = total_rows - deleted_by_table.get(table, 0)
            after_scan = scan_database(con, r3, r5)
            after_snapshot = retention_snapshot(con, after_scan)
            unchanged = (
                before_snapshot["tables"] == after_snapshot["tables"]
                and before_snapshot["r5_linked"] == after_snapshot["r5_linked"]
                and before_snapshot["overall_sha256"] == after_snapshot["overall_sha256"]
            )
            if not unchanged:
                raise CleanupError("保留校验不一致：非目标表或 r5 关联记录发生变化，已回滚")
            if after_scan["totals"]["deletable_total"] != 0:
                raise CleanupError("清理后仍存在可删除行（可能被并发写入），已回滚")
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
    finally:
        con.close()

    db_after = _db_file_info(db_path)
    report["app_db"] = db_after
    report["apply"] = {
        "performed": True,
        "deleted_by_table": deleted_by_table,
        "deleted_total": sum(deleted_by_table.values()),
        "kept_rows_by_table": kept_rows_by_table,
        "backup": backup,
        "db_sha256_before": db_sha256_before,
        "db_sha256_after": _sha256_file(db_path),
        "preservation": {
            "unchanged": True,
            "before": retention_evidence(before_snapshot),
            "after": retention_evidence(after_snapshot),
        },
        "note": "单事务提交；再次 --apply 应删除 0 行（幂等）",
    }
    report["status"] = "applied"
    report["app_db"]["unchanged_by_apply"] = False
    _write_report(report, Path(args.json) if args.json else None)
    for line in _summary_lines(report):
        print(line)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="知序 · r3 旧学习记录预检清理工具（默认 dry-run，只读）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  python scripts/cleanup_r3_records.py --dry-run --json docs/r3-清理预检-20261121.json\n"
            "  python scripts/cleanup_r3_records.py --apply\n"
            "  python scripts/cleanup_r3_records.py --apply --backup backups/zhixu-xxx.zip\n"
        ),
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", dest="dry_run", action="store_true", help="只读预检（默认）")
    mode.add_argument("--apply", dest="apply", action="store_true", help="显式执行清理（先备份、单事务）")
    parser.add_argument("--json", type=Path, default=None, help="报告输出路径（UTF-8 JSON）")
    parser.add_argument("--db", type=Path, default=None, help="应用库路径（默认 <data-dir>/app.sqlite3）")
    parser.add_argument("--data-dir", type=Path, default=None, help="数据目录（默认应用库所在目录）")
    parser.add_argument("--bank-root", type=Path, default=None, help="题库根目录（默认 ../../高考题库_按学科）")
    parser.add_argument("--r3", type=Path, default=None, help="r3 发布包目录（默认自动发现 releases/bank-*-r3）")
    parser.add_argument("--r5", type=Path, default=None, help="r5 发布包目录（默认跟随 releases/CURRENT）")
    parser.add_argument("--backup", type=Path, default=None, help="apply 前使用的已存在备份 zip")
    parser.add_argument(
        "--verify-backup",
        action="store_true",
        help="对 --backup 指定的备份执行 app.ops.verify_backup 校验",
    )
    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    mode = "apply" if args.apply else "dry-run"
    report = _base_report(mode, dry_run=not args.apply)
    try:
        if not args.apply and args.backup:
            print("--backup 只在 --apply 时有效", file=sys.stderr)
            return 2
        from app import db as app_db

        if app_db.postgres_enabled():
            raise PostgresUnsupported(
                "检测到 DATABASE_URL（PostgreSQL 模式）：本工具只在本机验证过 SQLite 路径，"
                "PG 清理请等待独立验证或改用 SQLite 演练副本；已 fail-closed，未做任何读写。"
            )
        if args.data_dir is None and args.db is not None:
            args.data_dir = Path(args.db).resolve().parent
        if args.data_dir is None:
            args.data_dir = Path(app_db.DATA_DIR)
        report["app_db"] = {"path": str(Path(args.db).resolve() if args.db else Path(args.data_dir) / "app.sqlite3")}
        if args.apply:
            return run_apply(args, report)
        return run_dry_run(args, report)
    except PostgresUnsupported as exc:
        report["status"] = "unsupported_postgres_fail_closed"
        report["error"] = str(exc)
        _write_report(report, Path(args.json) if args.json else None)
        print(str(exc), file=sys.stderr)
        return 3
    except BackupRefused as exc:
        report["status"] = "apply_refused"
        report["error"] = str(exc)
        _write_report(report, Path(args.json) if args.json else None)
        print(f"拒绝 apply：{exc}", file=sys.stderr)
        return 4
    except CleanupError as exc:
        report["status"] = "error"
        report["error"] = str(exc)
        _write_report(report, Path(args.json) if args.json else None)
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    except sqlite3.Error as exc:
        report["status"] = "error"
        report["error"] = f"SQLite 错误：{exc}"
        _write_report(report, Path(args.json) if args.json else None)
        print(f"SQLite 错误：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
