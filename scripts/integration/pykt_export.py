"""pyKT 离线评估数据协议与规则基线（《知序功能整合与优化总方案》§5.2「pyKT 评估」）。

本脚本只做三件事，**不写业务库、不联网、不训练模型**：

1. :func:`export_dataset`：从**只读**打开的应用库 SQLite 里导出 pyKT 协议需要的
   去标识化事件（JSONL）；只取 ``study_evidence`` 中有效、已核验、已判对错的事件，
   ``pending`` / ``unknown`` / 学生自评事件一律跳过；原始 ``user_id`` / ``username`` /
   题干 / 答案不会出现在输出里（用户 ID 用「盐 + ID」的 SHA-256 前 16 位替换）。
2. :func:`evaluate_thresholds`：按数据规模与标签覆盖做「数据达标才允许评估」门控。
3. :func:`baseline_metrics`：确定性的规则基线——平滑正确率 + PFA-lite（按
   ``(user_key, kc)`` 只用**时间顺序历史**预测下一次，无未来信息泄露）。BKT / DKT 等
   深度知识追踪模型需要独立训练环境与算力，本脚本不实现，只留基线对照。

数据口径
--------

* **有效证据**：``study_evidence.valid=1``；同一份作答的旧修订版本（``valid=0``）不导出，
  所以同一作答不会被重复计数。
* **标签**：``verdict=correct → 1``、``verdict=incorrect → 0``；只有 ``confidence=verified``
  （判分来源为 reference / ai_cross_verified / manual）的已判对错才进入数据集。
* **去标识化**：``user_key = sha256(salt + ":" + user_id)[:16]``；盐必须由调用方显式提供
  （CLI 用 ``--salt`` 或环境变量 ``STUDY_PYKT_SALT``），缺失时**不导出**并返回码 2。
  盐不落盘、不写日志，保证同一盐可复现同一份匿名键。
* **条目**：``item_key = study_evidence.question_ref``（``bank:<qid>`` / ``app:<id>`` /
  ``official:<公开编号>``，旧数据的裸题号原样保留）；``item_version`` 取应用库
  ``questions`` 的 ``bank_version``，缺失时取 ``content_revision``，应用库没有题库表时为空串。
* **知识点**：``question_kp JOIN knowledge_points`` 得到 ``kc_keys``（kp id 列表，升序去重）；
  题目未关联知识点或应用库无题库表时为空列表（如实留空，不伪造关联）。
* **排序**：按 ``(user_key, timestamp, item_key, evidence_version)`` 升序，保证同输入同输出。
* **只读**：SQLite 以 ``mode=ro`` URI 打开；导出前后源库字节摘要不变（测试有断言）。

退出码
------

``0`` = 导出成功（数据是否达标只看报告里的 ``gates.approved``）；``2`` = 参数/环境错误
（缺少去标识化盐、应用库不存在、导出参数非法等），此时不写出任何数据文件。
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import date, datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys

#: 仓库根目录（本脚本位于 ``scripts/integration/`` 下）。
ROOT = Path(__file__).resolve().parents[2]
#: 默认数据目录：环境变量 ``STUDY_DATA_DIR`` 优先，否则仓库 ``data/``。
DEFAULT_DATA_DIR = Path(os.environ.get("STUDY_DATA_DIR", str(ROOT / "data"))).resolve()
DEFAULT_SCREENSHOT_DIR = ROOT / "screenshots"

#: 数据集每行字段（顺序即协议字段清单；输出用 ``sort_keys`` 保证逐字节可复现）。
DATASET_FIELDS = (
    "user_key",
    "item_key",
    "item_version",
    "kc_keys",
    "timestamp",
    "label",
    "source_key",
    "prompt_state",
    "reveal_state",
    "self_report",
    "evidence_version",
)
#: 判分结果 → 0/1 标签；只有这两个 verdict 才可能有标签。
LABEL_OF_VERDICT = {"correct": 1, "incorrect": 0}
#: 视为已核验的 confidence 取值（与 ``app/study_evidence.py`` 的口径一致）。
VERIFIED_CONFIDENCE = "verified"
#: 事件类型里明确不应进入训练集的类型（学生自评不是判分证据）。
SKIP_KINDS = frozenset({"self_report"})

#: 阈值默认值（方案 §5.2 的最小数据量口径：300 名用户 × 20 条事件 = 6000 条起步，
#: 另加总量 10000 条、每知识点 200 条、标签覆盖 80% 的门槛）。
DEFAULT_MIN_USERS = 300
DEFAULT_MIN_EVENTS_PER_USER = 20
DEFAULT_MIN_EVENTS = 10000
DEFAULT_MIN_EVENTS_PER_KC = 200
DEFAULT_MIN_LABEL_COVERAGE = 0.8

#: 报告里固定写明的口径说明（中文，供 Lead/验收直接引用）。
PROTOCOL_NOTE = (
    "数据来源：应用库 study_evidence 有效证据（valid=1）中 confidence=verified 且"
    "verdict∈{correct,incorrect} 的事件；user_id 以「盐 + ID」哈希前 16 位替换，"
    "不导出 username/题干/答案。"
)
BASELINE_NOTE = (
    "规则基线：全局拉普拉斯平滑正确率 (correct+1)/(attempts+2) + PFA-lite"
    "（按 (user_key, kc) 仅用时间顺序历史预测下一次，无未来信息泄露）。"
    "BKT / DKT 等深度知识追踪模型需在独立环境训练，本脚本不实现、不联网、不训练模型。"
)


class ExportError(ValueError):
    """导出参数/环境错误（CLI 映射为退出码 2，中文提示）。"""


# ---------------------------------------------------------------------------
# 只读连接与库结构探测
# ---------------------------------------------------------------------------


def ro_connect(path: Path) -> sqlite3.Connection:
    """只读打开 SQLite（``mode=ro``）；调用方不得写入，导出结束必须关闭。"""
    uri = Path(path).resolve().as_uri()
    con = None
    try:
        con = sqlite3.connect(uri + "?mode=ro", uri=True, timeout=10)
        # PRAGMA schema_version 会真正读取库文件头，立即暴露损坏/权限/WAL 问题。
        con.execute("PRAGMA schema_version").fetchone()
        return con
    except sqlite3.Error:
        if con is not None:
            con.close()
        # WAL 库在只读 + 无 -shm 时可回退 immutable 只读模式（仍不写业务库）；
        # 代价是看不到尚未 checkpoint 的 WAL 内容，因此只作为打不开时的兜底。
        return sqlite3.connect(uri + "?mode=ro&immutable=1", uri=True, timeout=10)


def _table_names(con: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }


def _columns(con: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in con.execute('PRAGMA table_info("%s")' % table)}


def _read_users(con: sqlite3.Connection, tables: set[str]) -> set[int]:
    """读应用库 ``users``：只用于确认事件属于真实账号，不导出任何账号字段。"""
    if "users" not in tables:
        return set()
    ids: set[int] = set()
    for row in con.execute("SELECT id FROM users"):
        try:
            value = int(row[0])
        except (TypeError, ValueError):
            continue
        if value >= 1:
            ids.add(value)
    return ids


def _read_question_versions(con: sqlite3.Connection, tables: set[str]) -> dict[str, str]:
    """题目版本映射：``bank_version`` 优先，缺失时取 ``content_revision``。

    应用库通常没有题库表（题库是独立只读 SQLite 快照）；没有 ``questions`` 表或缺少
    这两列时返回空映射，导出的 ``item_version`` 如实为空串（不伪造版本）。
    """
    if "questions" not in tables:
        return {}
    columns = _columns(con, "questions")
    if "qid" not in columns:
        return {}
    version_columns = [name for name in ("bank_version", "content_revision") if name in columns]
    if not version_columns:
        return {}
    select = ", ".join(["qid", *version_columns])
    versions: dict[str, str] = {}
    for row in con.execute('SELECT %s FROM questions' % select):
        qid = str(row[0] or "").strip()
        if not qid:
            continue
        version = ""
        for value in row[1:]:
            text = str(value or "").strip()
            if text:
                version = text
                break
        versions.setdefault(qid, version)
    return versions


def _read_question_kcs(con: sqlite3.Connection, tables: set[str]) -> dict[str, list[int]]:
    """题目 → 知识点 id 列表（``question_kp``）；知识点表缺失时如实返回空映射。"""
    if not {"question_kp", "knowledge_points"} <= tables:
        return {}
    mapping: dict[str, list[int]] = {}
    rows = con.execute(
        "SELECT qk.qid, qk.kp_id FROM question_kp qk "
        "JOIN knowledge_points kp ON kp.id = qk.kp_id "
        "ORDER BY qk.qid, qk.kp_id"
    )
    for row in rows:
        qid = str(row[0] or "").strip()
        if not qid:
            continue
        try:
            kp_id = int(row[1])
        except (TypeError, ValueError):
            continue
        bucket = mapping.setdefault(qid, [])
        if kp_id not in bucket:
            bucket.append(kp_id)
    return mapping


def _ref_candidates(ref: str) -> list[str]:
    """统一引用 → 应用库题库键的候选（裸 qid / ``bank:`` / ``app:`` / ``official:``）。"""
    text = str(ref or "").strip()
    if not text:
        return []
    candidates = [text]
    lowered = text.lower()
    if lowered.startswith("bank:"):
        candidates.append(text.split(":", 1)[1])
    elif lowered.startswith("app:"):
        candidates.append(text.split(":", 1)[1])
    return candidates


def _version_of(ref: str, versions: dict[str, str]) -> str:
    for candidate in _ref_candidates(ref):
        if candidate in versions:
            return versions[candidate]
    return ""


def _kcs_of(ref: str, question_kcs: dict[str, list[int]]) -> list[int]:
    for candidate in _ref_candidates(ref):
        kcs = question_kcs.get(candidate)
        if kcs:
            return list(kcs)
    return []


# ---------------------------------------------------------------------------
# 去标识化与时间
# ---------------------------------------------------------------------------


def user_key_of(salt: str, user_id) -> str:
    """去标识化用户键：``sha256(salt + ":" + user_id)[:16]``（同一盐可复现）。"""
    digest = hashlib.sha256(("%s:%s" % (salt, user_id)).encode("utf-8")).hexdigest()
    return digest[:16]


def _parse_timestamp(value) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _days_span(timestamps) -> float:
    parsed = [item for item in (_parse_timestamp(value) for value in timestamps) if item is not None]
    if not parsed:
        return 0.0
    return round((max(parsed) - min(parsed)).total_seconds() / 86400, 2)


def _normalise_since(since: str | None) -> str | None:
    """校验 ``since``（ISO-8601 字符串，按 UTC ISO 文本比较以便 SQL 直接下推）。"""
    if since is None:
        return None
    text = str(since).strip()
    if not text:
        return None
    if _parse_timestamp(text) is None:
        raise ExportError("since 必须是 ISO-8601 时间字符串（例如 2026-01-01T00:00:00+00:00）")
    return text


def _label_of(verdict) -> int | None:
    return LABEL_OF_VERDICT.get(str(verdict or "").strip().lower())


# ---------------------------------------------------------------------------
# 公开函数：导出数据集
# ---------------------------------------------------------------------------


def export_dataset(
    db_path: Path,
    out_path: Path,
    *,
    salt: str,
    since: str | None = None,
) -> dict:
    """导出 pyKT 协议所需的去标识化事件到 JSONL，并返回统计。

    参数
    ----
    db_path: 应用库 SQLite 路径（只读打开，绝不写入）。
    out_path: 输出 JSONL 路径（父目录自动创建；可以写 ``screenshots/`` 等非业务目录）。
    salt: **必填**去标识化盐；空值直接报 :class:`ExportError`（不允许无盐导出）。
    since: 可选 ISO-8601 起始时间（只导出 ``occurred_at >= since`` 的事件）。

    返回
    ----
    ``{rows, users, items, kc_count, label_positive_rate, skipped, days_span, out_path,
    scanned, labeled, label_coverage, skip_reasons, per_user, per_kc}``。
    其中 ``per_user`` / ``per_kc`` 是**去标识化后**的事件计数明细，供
    :func:`evaluate_thresholds` 直接核验每用户/每 KC 门槛；``scanned`` 是窗口内扫描到的
    有效证据条数，``label_coverage = labeled / scanned``。
    """
    salt_text = str(salt or "").strip()
    if not salt_text:
        raise ExportError("去标识化盐必须显式提供（--salt 或环境变量 STUDY_PYKT_SALT）")
    database = Path(db_path)
    if not database.is_file():
        raise ExportError("未找到应用库文件：%s" % database)
    since_text = _normalise_since(since)
    destination = Path(out_path)
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ExportError("无法创建输出目录：%s（%s）" % (destination.parent, exc)) from exc

    try:
        con = ro_connect(database)
    except sqlite3.Error as exc:
        raise ExportError("无法只读打开应用库：%s（%s）" % (database, exc)) from exc
    try:
        tables = _table_names(con)
        if "study_evidence" not in tables:
            raise ExportError("应用库缺少 study_evidence 表：%s（尚未初始化或不是知序应用库）" % database)
        existing_users = _read_users(con, tables)
        versions = _read_question_versions(con, tables)
        question_kcs = _read_question_kcs(con, tables)

        where = ["valid=1"]
        params: list[str] = []
        if since_text is not None:
            where.append("occurred_at >= ?")
            params.append(since_text)
        statement = (
            "SELECT user_id, question_ref, verdict, grade_source, confidence, kind, "
            "prompt_state, reveal_state, self_report, evidence_version, occurred_at "
            "FROM study_evidence WHERE %s ORDER BY user_id, occurred_at, evidence_version, id"
            % " AND ".join(where)
        )
        scanned = 0
        skip_reasons: Counter[str] = Counter()
        rows: list[dict] = []
        for record in con.execute(statement, params):
            scanned += 1
            (
                user_id,
                ref,
                verdict,
                grade_source,
                confidence,
                kind,
                prompt_state,
                reveal_state,
                self_report,
                evidence_version,
                occurred_at,
            ) = record
            try:
                uid = int(user_id)
            except (TypeError, ValueError):
                skip_reasons["用户标识非法"] += 1
                continue
            if existing_users and uid not in existing_users:
                skip_reasons["用户不存在"] += 1
                continue
            label = _label_of(verdict)
            if label is None:
                skip_reasons["非已判对错（pending/unknown）"] += 1
                continue
            if str(confidence or "").strip().lower() != VERIFIED_CONFIDENCE:
                skip_reasons["判分未核验"] += 1
                continue
            if str(kind or "").strip().lower() in SKIP_KINDS:
                skip_reasons["学生自评事件"] += 1
                continue
            item_key = str(ref or "").strip()
            if not item_key:
                skip_reasons["缺少题目引用"] += 1
                continue
            timestamp = str(occurred_at or "").strip()
            if not timestamp:
                skip_reasons["缺少发生时间"] += 1
                continue
            try:
                version = int(evidence_version)
            except (TypeError, ValueError):
                version = 0
            rows.append(
                {
                    "user_key": user_key_of(salt_text, uid),
                    "item_key": item_key,
                    "item_version": _version_of(item_key, versions),
                    "kc_keys": _kcs_of(item_key, question_kcs),
                    "timestamp": timestamp,
                    "label": label,
                    "source_key": str(grade_source or "").strip(),
                    "prompt_state": str(prompt_state or "").strip(),
                    "reveal_state": str(reveal_state or "").strip(),
                    "self_report": str(self_report or "").strip(),
                    "evidence_version": version,
                }
            )
    except sqlite3.Error as exc:
        raise ExportError("读取应用库失败：%s（%s）" % (database, exc)) from exc
    finally:
        con.close()

    rows.sort(key=lambda item: (
        item["user_key"], item["timestamp"], item["item_key"], item["evidence_version"],
    ))
    try:
        with destination.open("w", encoding="utf-8", newline="\n") as handle:
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    except OSError as exc:
        raise ExportError("无法写出数据集文件：%s（%s）" % (destination, exc)) from exc

    user_keys = {row["user_key"] for row in rows}
    item_keys = {row["item_key"] for row in rows}
    per_user = Counter(row["user_key"] for row in rows)
    per_kc: Counter[str] = Counter()
    for row in rows:
        for kc_key in row["kc_keys"]:
            per_kc[str(kc_key)] += 1
    total = len(rows)
    positive = sum(row["label"] for row in rows)
    skipped = scanned - total
    return {
        "rows": total,
        "users": len(user_keys),
        "items": len(item_keys),
        "kc_count": len(per_kc),
        "label_positive_rate": round(positive / total, 6) if total else 0.0,
        "skipped": skipped,
        "days_span": _days_span(row["timestamp"] for row in rows),
        "out_path": str(destination),
        "scanned": scanned,
        "labeled": total,
        "label_coverage": round(total / scanned, 6) if scanned else 0.0,
        "skip_reasons": dict(sorted(skip_reasons.items())),
        "per_user": dict(sorted(per_user.items())),
        "per_kc": dict(sorted(per_kc.items())),
    }


def read_dataset(path: Path) -> list[dict]:
    """读回本脚本写出的 JSONL（供 CLI 直接算基线；不读业务库）。"""
    rows: list[dict] = []
    with Path(path).open("r", encoding="utf-8") as handle:
        for line in handle:
            text = line.strip()
            if not text:
                continue
            value = json.loads(text)
            if isinstance(value, dict):
                rows.append(value)
    return rows


# ---------------------------------------------------------------------------
# 公开函数：数据达标门控
# ---------------------------------------------------------------------------


def _coverage_value(stats: dict) -> float | None:
    """标签覆盖率：优先 ``label_coverage``，其次 ``labeled / scanned``。"""
    value = stats.get("label_coverage")
    if value is not None:
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
    labeled = stats.get("labeled")
    scanned = stats.get("scanned")
    try:
        labeled_value = int(labeled) if labeled is not None else None
        scanned_value = int(scanned) if scanned is not None else None
    except (TypeError, ValueError):
        return None
    if labeled_value is None or not scanned_value:
        return None
    return labeled_value / scanned_value


def evaluate_thresholds(
    stats: dict,
    *,
    min_users: int = DEFAULT_MIN_USERS,
    min_events_per_user: int = DEFAULT_MIN_EVENTS_PER_USER,
    min_events: int = DEFAULT_MIN_EVENTS,
    min_events_per_kc: int = DEFAULT_MIN_EVENTS_PER_KC,
    min_label_coverage: float = DEFAULT_MIN_LABEL_COVERAGE,
) -> dict:
    """按数据规模与标签覆盖评估是否达标（数据达标才允许进入 pyKT 离线评估）。

    ``stats`` 为 :func:`export_dataset` 的返回值（也允许从报告 JSON 读回）：
    必需聚合字段 ``users`` / ``rows``；明细字段 ``per_user``（去标识化 user_key → 事件数）、
    ``per_kc``（kc_key → 事件数）、``labeled`` + ``scanned``（或直接 ``label_coverage``）。
    缺失明细时对应门控判为不通过，并在 ``missing`` 里说明缺什么，不猜测数据。

    返回 ``{approved, gates, missing, note}``；``gates`` 每项含 ``ok`` 与门槛/实测值。
    """
    events = int(stats.get("rows") or 0)
    user_count = int(stats.get("users") or 0)
    raw_per_user = stats.get("per_user") if isinstance(stats.get("per_user"), dict) else {}
    raw_per_kc = stats.get("per_kc") if isinstance(stats.get("per_kc"), dict) else {}
    per_user = {str(key): int(value) for key, value in raw_per_user.items()}
    per_kc = {str(key): int(value) for key, value in raw_per_kc.items()}
    kc_count = int(stats.get("kc_count") or len(per_kc))
    users_ok = sum(1 for count in per_user.values() if count >= int(min_events_per_user))
    kcs_ok = sum(1 for count in per_kc.values() if count >= int(min_events_per_kc))
    coverage = _coverage_value(stats)

    gates = {
        "user_count": {
            "value": user_count,
            "min": int(min_users),
            "ok": user_count >= int(min_users),
        },
        "events": {
            "value": events,
            "min": int(min_events),
            "ok": events >= int(min_events),
        },
        "per_user": {
            "ok": bool(per_user) and users_ok >= int(min_users),
            "users_ok": users_ok,
            "users_total": user_count,
            "min_events_per_user": int(min_events_per_user),
            "min_users": int(min_users),
        },
        "per_kc": {
            "ok": bool(per_kc) and kcs_ok >= 1,
            "kcs_ok": kcs_ok,
            "kc_count": kc_count,
            "min_events_per_kc": int(min_events_per_kc),
        },
        "label_coverage": {
            "value": round(coverage, 6) if coverage is not None else None,
            "min": float(min_label_coverage),
            "ok": coverage is not None and coverage >= float(min_label_coverage),
        },
    }

    missing: list[str] = []
    if not gates["user_count"]["ok"]:
        missing.append(
            "用户数不足：%d < %d（pyKT 评估至少需要 %d 名独立学生）"
            % (user_count, int(min_users), int(min_users))
        )
    if not gates["events"]["ok"]:
        missing.append(
            "事件数不足：%d < %d（至少需要 %d 条已判对错事件）"
            % (events, int(min_events), int(min_events))
        )
    if not gates["per_user"]["ok"]:
        if not per_user:
            missing.append("缺少每用户事件数明细（per_user），无法核验「每用户 ≥ %d 条」门槛"
                           % int(min_events_per_user))
        else:
            missing.append(
                "满足每用户 ≥ %d 条事件的学生只有 %d 名 < %d 名"
                % (int(min_events_per_user), users_ok, int(min_users))
            )
    if not gates["per_kc"]["ok"]:
        if not per_kc:
            missing.append(
                "缺少每知识点事件数明细（per_kc）：题目尚未关联知识点或应用库无题库表，"
                "无法核验「每 KC ≥ %d 条」门槛" % int(min_events_per_kc)
            )
        else:
            missing.append(
                "没有知识点达到每 KC ≥ %d 条事件（当前 %d 个达标，共 %d 个知识点）"
                % (int(min_events_per_kc), kcs_ok, kc_count)
            )
    if not gates["label_coverage"]["ok"]:
        if coverage is None:
            missing.append("缺少标签覆盖明细（labeled/scanned），无法核验标签覆盖率门槛")
        else:
            missing.append(
                "有效标签覆盖率 %.2f < %.2f（被跳过的事件占比过高）"
                % (coverage, float(min_label_coverage))
            )

    approved = all(gate["ok"] for gate in gates.values())
    return {
        "approved": approved,
        "gates": gates,
        "missing": missing,
        "note": (
            "数据达标门控：只有 approved=true 才允许把该数据集送进 pyKT 离线评估；"
            "未达标时先补数据（更多独立学生 / 更多已核验判分事件 / 更完整的知识点关联），"
            "不得放宽阈值凑数。"
        ),
    }


# ---------------------------------------------------------------------------
# 公开函数：规则基线
# ---------------------------------------------------------------------------


def baseline_metrics(rows: list[dict], *, min_history: int = 2) -> dict:
    """确定性规则基线：平滑正确率 + PFA-lite（无泄露）。

    * ``accuracy_smoothed``：全部有标签事件的拉普拉斯平滑正确率
      ``(correct + 1) / (attempts + 2)``；
    * ``pfa``：按 ``(user_key, kc)`` 维护时间顺序历史，用
      ``(历史 correct + 1) / (历史 attempts + 2)`` 预测**当前**事件（预测前只看过去，
      当前事件处理完才进历史）；只有该 ``(user_key, kc)`` 历史尝试数 ≥ ``min_history``
      时才计入预测，避免用几乎没有信息的早期事件刷指标；
    * 事件按 ``(user_key, timestamp, item_key, evidence_version)`` 排序后处理，保证
      同输入同输出。行内的每个 kc 各产生一次预测（无 kc 的事件不进 PFA，但仍计入
      ``events`` 与 ``evaluated``）。
    """
    rows = [row for row in rows if isinstance(row, dict)]
    ordered = sorted(
        rows,
        key=lambda row: (
            str(row.get("user_key") or ""),
            str(row.get("timestamp") or ""),
            str(row.get("item_key") or ""),
            int(row.get("evidence_version") or 0),
        ),
    )
    labelled: list[tuple[str, list[str], int]] = []
    for row in ordered:
        try:
            label = int(row.get("label"))
        except (TypeError, ValueError):
            continue
        if label not in (0, 1):
            continue
        kcs = [
            str(item)
            for item in (row.get("kc_keys") or [])
            if isinstance(item, (str, int)) and str(item).strip()
        ]
        labelled.append((str(row.get("user_key") or ""), kcs, label))

    events = len(ordered)
    evaluated = len(labelled)
    correct = sum(label for _, _, label in labelled)
    accuracy_smoothed = round((correct + 1) / (evaluated + 2), 6) if evaluated else 0.0

    history: dict[tuple[str, str], list[int]] = {}
    predictions = 0
    brier_sum = 0.0
    hits = 0
    min_history_value = max(0, int(min_history))
    for user_key, kcs, label in labelled:
        for kc_key in kcs:
            bucket = history.get((user_key, kc_key))
            if bucket is not None and bucket[1] >= min_history_value:
                probability = (bucket[0] + 1) / (bucket[1] + 2)
                predictions += 1
                brier_sum += (probability - label) ** 2
                predicted_label = 1 if probability >= 0.5 else 0
                if predicted_label == label:
                    hits += 1
        # 当前事件处理完才写历史：预测只使用时间顺序上更早的事件，无未来信息泄露。
        for kc_key in kcs:
            bucket = history.setdefault((user_key, kc_key), [0, 0])
            bucket[0] += label
            bucket[1] += 1

    pfa = {
        "predictions": predictions,
        "brier": round(brier_sum / predictions, 6) if predictions else 0.0,
        "accuracy": round(hits / predictions, 6) if predictions else 0.0,
    }
    note = BASELINE_NOTE
    if predictions == 0:
        note += "（当前数据量下 PFA-lite 没有满足历史门槛的预测，基线仅作占位。）"
    return {
        "events": events,
        "evaluated": evaluated,
        "accuracy_smoothed": accuracy_smoothed,
        "pfa": pfa,
        "min_history": min_history_value,
        "note": note,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pykt_export.py",
        description="pyKT 离线评估：去标识化导出学习证据 + 数据门控 + 规则基线（只读、不联网、不训练）",
    )
    parser.add_argument("--data-dir", default=None,
                        help="应用数据目录（默认 env STUDY_DATA_DIR 或仓库 data/）")
    parser.add_argument("--db", default=None, help="覆盖应用库路径（默认 <data-dir>/app.sqlite3）")
    parser.add_argument("--out", default=None,
                        help="数据集 JSONL 输出路径（默认 screenshots/pykt-dataset-<日期>.jsonl）")
    parser.add_argument("--report", default=None,
                        help="报告 JSON 输出路径（默认 screenshots/pykt-report.json）")
    parser.add_argument("--salt", default=None,
                        help="去标识化盐（默认 env STUDY_PYKT_SALT；缺失时不导出并返回 2）")
    parser.add_argument("--since", default=None,
                        help="可选起始时间（ISO-8601，只导出 occurred_at 不早于它的证据）")
    parser.add_argument("--min-users", type=int, default=DEFAULT_MIN_USERS)
    parser.add_argument("--min-events-per-user", type=int, default=DEFAULT_MIN_EVENTS_PER_USER)
    parser.add_argument("--min-events", type=int, default=DEFAULT_MIN_EVENTS)
    parser.add_argument("--min-events-per-kc", type=int, default=DEFAULT_MIN_EVENTS_PER_KC)
    parser.add_argument("--min-label-coverage", type=float, default=DEFAULT_MIN_LABEL_COVERAGE)
    return parser


def _resolve_paths(args) -> tuple[Path, Path, Path]:
    if args.data_dir:
        data_dir = Path(args.data_dir)
    else:
        # 读取时点再取环境变量，兼容调用方在 import 之后设置 STUDY_DATA_DIR。
        data_dir = Path(os.environ.get("STUDY_DATA_DIR") or str(DEFAULT_DATA_DIR))
    db_path = Path(args.db) if args.db else data_dir / "app.sqlite3"
    today = date.today().isoformat()
    out_path = Path(args.out) if args.out else DEFAULT_SCREENSHOT_DIR / (
        "pykt-dataset-%s.jsonl" % today
    )
    report_path = Path(args.report) if args.report else DEFAULT_SCREENSHOT_DIR / "pykt-report.json"
    return db_path, out_path, report_path


def main(argv: list[str] | None = None) -> int:
    """CLI 入口：0=导出成功（是否达标看报告），2=参数/环境错误（不导出）。"""
    parser = build_parser()
    args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    salt = str(args.salt or os.environ.get("STUDY_PYKT_SALT") or "").strip()
    if not salt:
        print(
            "错误：去标识化盐必须显式提供（--salt 或环境变量 STUDY_PYKT_SALT）；"
            "没有盐无法保证可复现的匿名用户键，本次不导出任何数据。",
            file=sys.stderr,
        )
        return 2
    db_path, out_path, report_path = _resolve_paths(args)
    if not Path(db_path).is_file():
        print(
            "错误：未找到应用库文件 %s（可用 --db 或 --data-dir 指定，默认读 STUDY_DATA_DIR）。"
            % db_path,
            file=sys.stderr,
        )
        return 2

    try:
        stats = export_dataset(db_path, out_path, salt=salt, since=args.since)
        rows = read_dataset(out_path)
    except (ExportError, OSError, sqlite3.Error) as exc:
        print("错误：%s" % exc, file=sys.stderr)
        return 2

    gates = evaluate_thresholds(
        stats,
        min_users=args.min_users,
        min_events_per_user=args.min_events_per_user,
        min_events=args.min_events,
        min_events_per_kc=args.min_events_per_kc,
        min_label_coverage=args.min_label_coverage,
    )
    baseline = baseline_metrics(rows)
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "protocol_note": PROTOCOL_NOTE,
        "baseline_note": BASELINE_NOTE,
        "evaluation_allowed": gates["approved"],
        "dataset": stats,
        "gates": gates,
        "baseline": baseline,
    }
    try:
        Path(report_path).parent.mkdir(parents=True, exist_ok=True)
        with Path(report_path).open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(report, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
    except OSError as exc:
        print("错误：无法写出报告文件 %s（%s）" % (report_path, exc), file=sys.stderr)
        return 2

    print("pyKT 离线评估数据导出完成（只读导出，未写业务库、未联网、未训练模型）")
    print("  数据集：%s" % out_path)
    print(
        "  样本：%d 条事件 / %d 名用户 / %d 道题 / %d 个知识点；跳过 %d 条（扫描 %d 条）"
        % (
            stats["rows"], stats["users"], stats["items"], stats["kc_count"],
            stats["skipped"], stats["scanned"],
        )
    )
    print(
        "  标签：正例率 %.4f，有效标签覆盖率 %.4f，跨度 %.2f 天"
        % (stats["label_positive_rate"], stats["label_coverage"], stats["days_span"])
    )
    print(
        "  数据门控：%s"
        % (
            "达标，可进入 pyKT 离线评估"
            if gates["approved"]
            else "未达标（本次只导出数据与规则基线参考，不进入 pyKT 模型训练/评估）"
        )
    )
    for item in gates["missing"]:
        print("    - %s" % item)
    print(
        "  规则基线：平滑正确率 %.4f；PFA-lite 预测 %d 条（Brier %.4f / 准确率 %.4f）"
        % (
            baseline["accuracy_smoothed"],
            baseline["pfa"]["predictions"],
            baseline["pfa"]["brier"],
            baseline["pfa"]["accuracy"],
        )
    )
    print("  报告：%s" % report_path)
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI 直跑
    raise SystemExit(main())
