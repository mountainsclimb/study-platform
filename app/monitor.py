"""运维监控聚合（方案 §7「分发与运维」）：只读、短缓存、单检查降级。

五个分组（``checks`` 的键固定为 ``tasks / ai / media / data / plaza``）：

* ``tasks``：AI 任务按 kind 的排队 / 运行 / 失败率 / 超时、最老排队时长、中断回收数；
* ``ai``：最近 N 天调用量、token、费用估算、失败率，以及 OCR（``ocr`` / ``recognize``）
  任务耗时 P50 / P95；
* ``media``：课堂视频 / 旁白渲染队列与失败、课堂素材与课程状态、PDF / 视频导出能力；
* ``data``：应用库大小与 SQLite WAL、数据库等待、题库版本与可读性、最近备份元数据；
* ``plaza``：撤回传播抽样断言（被撤回的发布是否仍出现在公开读查询里）、未处理反馈积压。

工程约束（与任务口径一致）：

1. **只读**：只执行 SELECT / 只读探测，不写任何业务表，也不触发广场 / AI 的业务函数；
2. **单检查失败不 500**：每个检查单独捕获异常，标 ``status=error``；整体 ``ok=false``；
3. **短缓存**：结果在进程内缓存 ``STUDY_MONITOR_CACHE_SECONDS``（默认 15 秒，0 关闭），
   避免管理台轮询把数据库查穿；``refresh=1`` 或 ``reset_cache()`` 可绕过；
4. **SQLite / PostgreSQL 兼容**：只用 ``db.connection()`` / ``db.table_names()`` 封装，
   不用 SQLite 专有函数（``julianday`` / ``strftime`` / ``PRAGMA``），时间聚合在 Python 侧做；
5. **不泄露密钥**：只输出计数、耗时、体积、版本与中文摘要，不含密钥、绝对宿主路径或用户内容。

状态口径：``ok`` 正常；``warn`` 需要关注（积压、失败率偏高、备份过期等）；
``error`` 明确异常（撤回内容仍公开、题库版本不匹配、检查本身抛错）。
"""
from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
from functools import lru_cache
import json
import logging
import os
from pathlib import Path
import shutil
import sqlite3
import threading
import time
from typing import Any, Callable
import zipfile

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from . import auth, db

router = APIRouter(prefix="/api", tags=["运维监控"])
log = logging.getLogger("zhixu.monitor")

# ---------------------------------------------------------------------------
# 常量与阈值（集中在这里，便于运维按试点规模调整）
# ---------------------------------------------------------------------------

CACHE_TTL_ENV = "STUDY_MONITOR_CACHE_SECONDS"
DEFAULT_CACHE_TTL_SECONDS = 15.0
MAX_CACHE_TTL_SECONDS = 300.0

AI_WINDOW_ENV = "STUDY_MONITOR_AI_WINDOW_DAYS"
DEFAULT_AI_WINDOW_DAYS = 7
MAX_AI_WINDOW_DAYS = 365

JOB_TIMEOUT_ENV = "STUDY_MONITOR_JOB_TIMEOUT_SECONDS"
WORKER_TIMEOUT_ENV = "STUDY_WORKER_JOB_TIMEOUT"
DEFAULT_JOB_TIMEOUT_SECONDS = 1800

LEASE_ENV = "STUDY_MONITOR_LEASE_SECONDS"
AI_LEASE_ENV = "AI_JOB_LEASE_SECONDS"
DEFAULT_LEASE_SECONDS = 90

WAL_WARN_ENV = "STUDY_MONITOR_WAL_WARN_MB"
DEFAULT_WAL_WARN_MB = 64
DB_WAIT_WARN_MS = 500
BACKUP_WARN_ENV = "STUDY_MONITOR_BACKUP_WARN_HOURS"
DEFAULT_BACKUP_WARN_HOURS = 48
FEEDBACK_WARN_ENV = "STUDY_MONITOR_FEEDBACK_WARN_PENDING"
DEFAULT_FEEDBACK_WARN_PENDING = 20

QUEUE_WARN_SECONDS = 120
QUEUE_ERROR_SECONDS = 900
FAILURE_WARN_RATE = 0.25
OCR_P95_WARN_MS = 120_000
FEEDBACK_WARN_AGE_SECONDS = 72 * 3600

QUERY_ROW_LIMIT = 5000
SAMPLE_LIMIT = 200

ACTIVE_JOB_STATUSES = ("queued", "running", "cancel_requested", "awaiting_outline")
TERMINAL_JOB_STATUSES = ("completed", "failed", "interrupted", "cancelled")
OCR_TASKS = ("ocr", "recognize")
MEDIA_TASKS = ("course-video", "course-narration")

CHECK_ORDER = ("tasks", "ai", "media", "data", "plaza")
CHECK_LABELS = {
    "tasks": "任务",
    "ai": "AI 用量",
    "media": "渲染/媒体",
    "data": "数据",
    "plaza": "广场",
}
STATUS_ORDER = {"ok": 0, "warn": 1, "error": 2}

#: 与 ``app/plaza/db.PUBLIC_STATUSES`` 同口径；广场模块不可用时用这份兜底常量，
#: 保证监控不依赖广场业务代码也能做撤回传播断言。
_FALLBACK_PUBLIC_STATUSES = ("community_review", "verified", "published")

_FALLBACK_KIND_LABELS = {
    "ocr": "OCR 识别",
    "recognize": "题目识别",
    "explain": "AI 讲解",
    "grade": "AI 批改",
    "tagging": "知识点打标",
    "question-analysis": "题目分析",
    "notes": "笔记生成",
    "course": "AI 课堂生成",
    "course-video": "课堂视频导出",
    "course-narration": "课堂批量旁白",
    "paper-import": "试卷识别导入",
    "paper-enrich": "试卷补全解析",
    "paper-compose": "AI 组卷",
    "paper-revision": "错题巩固卷",
    "paper-annotate": "试卷切题重切",
    "assistant-turn": "AI 助手对话",
    "insight-report": "学情报告",
}


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------

def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso(value) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _age_seconds(value, now: datetime | None = None) -> int | None:
    parsed = _parse_iso(value)
    if parsed is None:
        return None
    reference = now or _utcnow()
    return max(0, int((reference - parsed).total_seconds()))


def _percentile(values, fraction: float) -> int | None:
    ordered = sorted(int(value) for value in values if value is not None)
    if not ordered:
        return None
    position = (len(ordered) - 1) * float(fraction)
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    if low == high:
        return ordered[low]
    return int(round(ordered[low] + (ordered[high] - ordered[low]) * (position - low)))


def _row_dict(row) -> dict:
    """sqlite3.Row / 自定义 ``_Row`` 统一转 dict（``dict(row)`` 对元组子类不可靠）。"""
    try:
        keys = row.keys()
    except AttributeError:
        return {}
    return {key: row[key] for key in keys}


def _env_float(name: str, default: float, *, minimum: float, maximum: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, value))


def _env_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        value = int(float(str(raw).strip()))
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, value))


def _cache_ttl_seconds() -> float:
    return _env_float(
        CACHE_TTL_ENV, DEFAULT_CACHE_TTL_SECONDS, minimum=0.0, maximum=MAX_CACHE_TTL_SECONDS,
    )


def _ai_window_days() -> int:
    return _env_int(
        AI_WINDOW_ENV, DEFAULT_AI_WINDOW_DAYS, minimum=1, maximum=MAX_AI_WINDOW_DAYS,
    )


def _job_timeout_seconds() -> int:
    raw = os.environ.get(JOB_TIMEOUT_ENV) or os.environ.get(WORKER_TIMEOUT_ENV)
    try:
        return max(1, int(float(str(raw).strip()))) if raw and str(raw).strip() else DEFAULT_JOB_TIMEOUT_SECONDS
    except (TypeError, ValueError):
        return DEFAULT_JOB_TIMEOUT_SECONDS


def _lease_seconds() -> int:
    raw = os.environ.get(LEASE_ENV) or os.environ.get(AI_LEASE_ENV)
    try:
        return max(1, int(float(str(raw).strip()))) if raw and str(raw).strip() else DEFAULT_LEASE_SECONDS
    except (TypeError, ValueError):
        return DEFAULT_LEASE_SECONDS


def _wal_warn_bytes() -> int:
    return _env_int(
        WAL_WARN_ENV, DEFAULT_WAL_WARN_MB, minimum=1, maximum=1024 * 1024,
    ) * 1024 * 1024


def _feedback_warn_pending() -> int:
    return _env_int(
        FEEDBACK_WARN_ENV, DEFAULT_FEEDBACK_WARN_PENDING, minimum=1, maximum=100000,
    )


def _backup_warn_seconds() -> int:
    return _env_int(
        BACKUP_WARN_ENV, DEFAULT_BACKUP_WARN_HOURS, minimum=1, maximum=24 * 365,
    ) * 3600


@lru_cache(maxsize=128)
def _kind_label(kind: str) -> str:
    name = str(kind or "")
    try:
        from .ai import usage as ai_usage

        label = str(ai_usage.kind_label(name) or name)
    except Exception:  # 标签只是展示信息，AI 模块不可用不能影响监控
        label = _FALLBACK_KIND_LABELS.get(name, name)
    return label or name or "未命名任务"


def _public_statuses() -> tuple[str, ...]:
    try:
        from .plaza import db as plaza_db

        statuses = tuple(str(value) for value in plaza_db.PUBLIC_STATUSES)
        return statuses or _FALLBACK_PUBLIC_STATUSES
    except Exception:
        return _FALLBACK_PUBLIC_STATUSES


def _placeholders(values) -> str:
    return ", ".join("?" for _ in values)


def _worse(current: str, candidate: str) -> str:
    return candidate if STATUS_ORDER.get(candidate, 0) > STATUS_ORDER.get(current, 0) else current


def _result(status: str = "ok", detail: str = "", alerts=None, **data) -> dict:
    return {"status": status, "detail": detail, "alerts": list(alerts or []), **data}


# ---------------------------------------------------------------------------
# 进程内短缓存
# ---------------------------------------------------------------------------

_cache_lock = threading.Lock()
_cache_state: dict[str, Any] = {"key": None, "at": 0.0, "payload": None}


def _cache_key() -> tuple:
    """缓存键：数据目录 / 数据库引擎 / AI 统计窗口变化时自然失效。"""
    return (str(db.DATA_DIR), db.postgres_enabled(), _ai_window_days())


def reset_cache() -> None:
    """清空进程内短缓存（测试隔离或运维手工刷新用；不触碰数据库）。"""
    with _cache_lock:
        _cache_state.update(key=None, at=0.0, payload=None)


def _cache_lookup(ttl: float) -> dict | None:
    state = _cache_state
    if state["payload"] is None or state["key"] != _cache_key():
        return None
    age = time.monotonic() - float(state["at"] or 0.0)
    if age > ttl:
        return None
    payload = copy.deepcopy(state["payload"])
    payload["cache"] = {
        "ttl_seconds": round(ttl, 3),
        "cached": True,
        "age_seconds": round(max(0.0, age), 3),
    }
    return payload


# ---------------------------------------------------------------------------
# 检查框架：单检查失败只标 error，不让整个接口 500
# ---------------------------------------------------------------------------

def _run_check(name: str, now: datetime) -> dict:
    """执行单个检查：独立连接 + 独立异常边界。

    PostgreSQL 下某条 SQL 出错会让当前事务进入 aborted 状态，因此每个检查独用一个
    连接：单检查失败只标该组 ``error``，不会连累其余四组；SQLite 下同样干净隔离。
    """
    label = CHECK_LABELS.get(name, name)
    func = _CHECK_FUNCS.get(name)
    if func is None:  # pragma: no cover - 配置错误属代码缺陷，按 error 呈现
        return _result("error", "%s检查未注册" % label, ["%s检查未注册" % label])
    try:
        with db.connection() as con:
            tables = db.table_names(con)
            wait_started = time.perf_counter()
            con.execute("SELECT 1").fetchone()
            db_wait_ms = round((time.perf_counter() - wait_started) * 1000, 3)
            result = func({
                "con": con,
                "tables": tables,
                "now": now,
                "db_wait_ms": db_wait_ms,
            })
        if not isinstance(result, dict):
            raise TypeError("check returned %r" % type(result).__name__)
    except Exception as exc:  # noqa: BLE001 - 监控检查不允许把异常抛给调用方
        log.warning("运维监控检查 %s 失败：%s", name, type(exc).__name__)
        return _result(
            "error",
            "%s检查失败（%s），请查看服务日志" % (label, type(exc).__name__),
            ["%s检查失败（%s）" % (label, type(exc).__name__)],
        )
    result.setdefault("status", "ok")
    result.setdefault("detail", "")
    result.setdefault("alerts", [])
    return result


# ---------------------------------------------------------------------------
# tasks：AI 任务队列 / 失败率 / 超时 / 中断回收
# ---------------------------------------------------------------------------

def _active_job_rows(con, tasks: tuple[str, ...] | None = None) -> list[dict]:
    where = ["status IN (%s)" % _placeholders(ACTIVE_JOB_STATUSES)]
    params: list = list(ACTIVE_JOB_STATUSES)
    if tasks:
        where.append("task IN (%s)" % _placeholders(tasks))
        params.extend(tasks)
    rows = con.execute(
        "SELECT id, task, status, created_at, started_at, heartbeat_at FROM ai_jobs "
        "WHERE " + " AND ".join(where) + " ORDER BY id DESC LIMIT ?",
        (*params, QUERY_ROW_LIMIT),
    ).fetchall()
    return [_row_dict(row) for row in rows]


def _active_job_stats(rows: list[dict], now: datetime, *, timeout_seconds: int,
                      lease_seconds: int) -> dict[str, dict]:
    """按任务类型汇总活跃任务：排队时长、执行超时、心跳超期。"""
    stats: dict[str, dict] = {}
    for row in rows:
        kind = str(row.get("task") or "unknown")
        entry = stats.setdefault(kind, {
            "queued": 0, "running": 0, "cancel_requested": 0, "awaiting_outline": 0,
            "oldest_queued_seconds": None, "timed_out": 0, "stale_running": 0,
        })
        status = str(row.get("status") or "")
        if status in entry:
            entry[status] += 1
        created_age = _age_seconds(row.get("created_at"), now)
        if status == "queued":
            if created_age is not None:
                previous = entry["oldest_queued_seconds"]
                entry["oldest_queued_seconds"] = created_age if previous is None else max(previous, created_age)
        elif status in ("running", "cancel_requested"):
            started_age = _age_seconds(row.get("started_at"), now)
            elapsed = started_age if started_age is not None else created_age
            if elapsed is not None and elapsed > timeout_seconds:
                entry["timed_out"] += 1
            heartbeat_age = _age_seconds(row.get("heartbeat_at"), now)
            if heartbeat_age is None:
                # 从未写心跳：执行时间超过租约即视为失联（与 reap_stale_jobs 同口径）。
                if elapsed is not None and elapsed > lease_seconds:
                    entry["stale_running"] += 1
            elif heartbeat_age > lease_seconds:
                entry["stale_running"] += 1
    return stats


def _terminal_counts(con, since: str, *, tasks: tuple[str, ...] | None = None) -> dict[str, dict]:
    where = ["created_at>=?", "status IN (%s)" % _placeholders(TERMINAL_JOB_STATUSES)]
    params: list = [since, *TERMINAL_JOB_STATUSES]
    if tasks:
        where.append("task IN (%s)" % _placeholders(tasks))
        params.extend(tasks)
    rows = con.execute(
        "SELECT task, status, COUNT(*) AS total FROM ai_jobs WHERE "
        + " AND ".join(where) + " GROUP BY task, status",
        tuple(params),
    ).fetchall()
    grouped: dict[str, dict] = {}
    for row in rows:
        data = _row_dict(row)
        kind = str(data.get("task") or "unknown")
        status = str(data.get("status") or "")
        entry = grouped.setdefault(kind, {value: 0 for value in TERMINAL_JOB_STATUSES})
        if status in entry:
            entry[status] += int(data.get("total") or 0)
    return grouped


def _failure_rate(counts: dict) -> float:
    terminal = sum(int(counts.get(status) or 0) for status in TERMINAL_JOB_STATUSES)
    if terminal <= 0:
        return 0.0
    failed = int(counts.get("failed") or 0) + int(counts.get("interrupted") or 0)
    return round(failed / terminal, 4)


def _check_tasks(ctx: dict) -> dict:
    con, tables, now = ctx["con"], ctx["tables"], ctx["now"]
    if "ai_jobs" not in tables:
        return _result("warn", "缺少 ai_jobs 表（AI 任务模块尚未初始化）", ["AI 任务模块尚未初始化"])

    window = _ai_window_days()
    since = (now - timedelta(days=window)).isoformat()
    timeout_seconds = _job_timeout_seconds()
    lease_seconds = _lease_seconds()

    terminal_grouped = _terminal_counts(con, since)
    active_stats = _active_job_stats(
        _active_job_rows(con), now,
        timeout_seconds=timeout_seconds, lease_seconds=lease_seconds,
    )

    by_kind: dict[str, dict] = {}
    for kind, counts in terminal_grouped.items():
        entry = by_kind.setdefault(kind, {"kind": kind, "label": _kind_label(kind)})
        entry.update({status: int(counts.get(status) or 0) for status in TERMINAL_JOB_STATUSES})
    for kind, stats in active_stats.items():
        entry = by_kind.setdefault(kind, {"kind": kind, "label": _kind_label(kind)})
        # 同一 kind 可能既有终态计数又有活跃任务：只补零，不能覆盖已统计的终态。
        for status in TERMINAL_JOB_STATUSES:
            entry.setdefault(status, 0)
        entry.update(stats)

    totals = {status: 0 for status in ACTIVE_JOB_STATUSES}
    totals.update({status: 0 for status in TERMINAL_JOB_STATUSES})
    items = []
    oldest_queued = None
    oldest_queued_kind = None
    timed_out = stale_running = 0
    for entry in by_kind.values():
        for status in (*ACTIVE_JOB_STATUSES, *TERMINAL_JOB_STATUSES):
            value = int(entry.get(status) or 0)
            entry[status] = value
            totals[status] += value
        entry.setdefault("oldest_queued_seconds", None)
        entry.setdefault("timed_out", 0)
        entry.setdefault("stale_running", 0)
        entry["failure_rate"] = _failure_rate(entry)
        items.append(entry)
        if entry["oldest_queued_seconds"] is not None:
            if oldest_queued is None or entry["oldest_queued_seconds"] > oldest_queued:
                oldest_queued = entry["oldest_queued_seconds"]
                oldest_queued_kind = entry["kind"]
        timed_out += int(entry.get("timed_out") or 0)
        stale_running += int(entry.get("stale_running") or 0)

    items.sort(key=lambda item: (
        -(int(item.get("queued") or 0) + int(item.get("running") or 0)),
        -int(item.get("failed") or 0),
        str(item.get("kind") or ""),
    ))

    failure_rate = _failure_rate(totals)
    reclaimed = int(totals.get("interrupted") or 0)
    alerts: list[str] = []
    status = "ok"
    terminal_total = sum(int(totals.get(status) or 0) for status in TERMINAL_JOB_STATUSES)
    if terminal_total >= 4 and failure_rate >= FAILURE_WARN_RATE:
        status = _worse(status, "warn")
        alerts.append("任务失败率 %.1f%%（%d/%d）" % (failure_rate * 100, int(totals["failed"]) + reclaimed, terminal_total))
    if oldest_queued is not None and oldest_queued >= QUEUE_WARN_SECONDS:
        status = _worse(status, "warn")
        alerts.append("最老排队任务已等待 %d 秒（%s）" % (oldest_queued, _kind_label(oldest_queued_kind or "")))
    if oldest_queued is not None and oldest_queued >= QUEUE_ERROR_SECONDS:
        status = _worse(status, "error")
        alerts.append("排队严重积压：最老任务已等待 %d 秒" % oldest_queued)
    if timed_out:
        status = _worse(status, "warn")
        alerts.append("%d 个执行中任务超过超时阈值（%d 秒）" % (timed_out, timeout_seconds))
    if stale_running:
        status = _worse(status, "warn")
        alerts.append("%d 个执行中任务心跳超期（租约 %d 秒），可能已失联" % (stale_running, lease_seconds))

    detail = "近 %d 天终态 %d 个，失败率 %.1f%%，排队 %d、运行 %d、中断回收 %d" % (
        window, terminal_total, failure_rate * 100,
        totals["queued"], totals["running"] + totals["cancel_requested"], reclaimed,
    )
    return _result(
        status, detail, alerts,
        window_days=window,
        timeout_seconds=timeout_seconds,
        lease_seconds=lease_seconds,
        oldest_queued_seconds=oldest_queued,
        oldest_queued_kind=oldest_queued_kind,
        timed_out=timed_out,
        stale_running=stale_running,
        reclaimed=reclaimed,
        failure_rate=failure_rate,
        success_rate=round(1.0 - failure_rate, 4) if terminal_total else None,
        totals=totals,
        by_kind=items[:20],
        truncated=len(items) > 20,
    )


# ---------------------------------------------------------------------------
# ai：最近 N 天用量 / token / 费用估算 / 失败率 / OCR 延迟
# ---------------------------------------------------------------------------

def _ledger_rows(con, since: str) -> list[dict]:
    rows = con.execute(
        "SELECT kind, status, duration_ms, retries, tokens_in, tokens_out, cost_micro, created_at "
        "FROM ai_job_usage WHERE created_at>=? ORDER BY id DESC LIMIT ?",
        (since, QUERY_ROW_LIMIT),
    ).fetchall()
    return [_row_dict(row) for row in rows]


def _job_duration_ms(row: dict) -> int:
    started = _parse_iso(row.get("started_at")) or _parse_iso(row.get("created_at"))
    finished = _parse_iso(row.get("finished_at")) or _parse_iso(row.get("updated_at"))
    if not started or not finished:
        return 0
    return max(0, int((finished - started).total_seconds() * 1000))


def _job_fallback_rows(con, since: str) -> list[dict]:
    rows = con.execute(
        "SELECT task, status, created_at, updated_at, started_at, finished_at, retry_count "
        "FROM ai_jobs WHERE created_at>=? AND status IN (%s) ORDER BY id DESC LIMIT ?"
        % _placeholders(TERMINAL_JOB_STATUSES),
        (since, *TERMINAL_JOB_STATUSES, QUERY_ROW_LIMIT),
    ).fetchall()
    output = []
    for row in rows:
        data = _row_dict(row)
        output.append({
            "kind": str(data.get("task") or "unknown"),
            "status": str(data.get("status") or ""),
            "duration_ms": _job_duration_ms(data),
            "retries": int(data.get("retry_count") or 0),
            "tokens_in": 0,
            "tokens_out": 0,
            "cost_micro": None,
            "created_at": data.get("created_at"),
        })
    return output


def _aggregate_usage(rows: list[dict]) -> dict:
    jobs = len(rows)
    counts = {status: 0 for status in TERMINAL_JOB_STATUSES}
    durations: list[int] = []
    tokens_in = tokens_out = retries = 0
    cost_micro = 0
    covered = unpriced = 0
    for row in rows:
        status = str(row.get("status") or "")
        if status in counts:
            counts[status] += 1
        duration = int(row.get("duration_ms") or 0)
        if duration > 0:
            durations.append(duration)
        tokens_in += int(row.get("tokens_in") or 0)
        tokens_out += int(row.get("tokens_out") or 0)
        retries += int(row.get("retries") or 0)
        cost = row.get("cost_micro")
        if cost is None:
            unpriced += 1
        else:
            cost_micro += int(cost)
            covered += 1
    failed = counts["failed"] + counts["interrupted"]
    terminal = sum(counts.values())
    return {
        "jobs": jobs,
        "completed": counts["completed"],
        "failed": failed,
        "cancelled": counts["cancelled"],
        "failure_rate": round(failed / terminal, 4) if terminal else 0.0,
        "duration_p50_ms": _percentile(durations, 0.5),
        "duration_p95_ms": _percentile(durations, 0.95),
        "duration_avg_ms": int(sum(durations) / len(durations)) if durations else None,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "tokens_total": tokens_in + tokens_out,
        "retries": retries,
        "cost_micro": cost_micro if covered else None,
        "cost_covered_jobs": covered,
        "cost_unpriced_jobs": unpriced,
    }


def _check_ai(ctx: dict) -> dict:
    con, tables, now = ctx["con"], ctx["tables"], ctx["now"]
    window = _ai_window_days()
    since = (now - timedelta(days=window)).isoformat()

    rows: list[dict] | None = None
    source = "unavailable"
    ledger_note = ""
    if "ai_job_usage" in tables:
        try:
            rows = _ledger_rows(con, since)
            source = "ai_job_usage"
        except Exception as exc:  # noqa: BLE001 - 账本不可读时退回任务表粗算
            log.warning("AI 用量账本不可读：%s", type(exc).__name__)
            ledger_note = "用量账本不可读（%s）" % type(exc).__name__
    if rows is None and "ai_jobs" in tables:
        rows = _job_fallback_rows(con, since)
        source = "ai_jobs"
        if not ledger_note:
            ledger_note = "用量账本尚未建立，已按任务表粗算"
    if rows is None:
        return _result("warn", "缺少 AI 用量账本与任务表，无法统计 AI 指标", ["AI 用量不可统计"])

    totals = _aggregate_usage(rows)
    by_kind: dict[str, list[dict]] = {}
    for row in rows:
        by_kind.setdefault(str(row.get("kind") or "unknown"), []).append(row)
    kind_items = []
    for kind, group in by_kind.items():
        summary = _aggregate_usage(group)
        summary["kind"] = kind
        summary["label"] = _kind_label(kind)
        kind_items.append(summary)
    kind_items.sort(key=lambda item: (-int(item.get("jobs") or 0), str(item.get("kind") or "")))

    ocr_rows = [row for row in rows if str(row.get("kind") or "") in OCR_TASKS]
    ocr = _aggregate_usage(ocr_rows)
    ocr["kinds"] = list(OCR_TASKS)

    model_calls = None
    if "ai_usage" in tables:
        try:
            model_calls = int(con.execute(
                "SELECT COUNT(*) FROM ai_usage WHERE created_at>=?", (since,),
            ).fetchone()[0])
        except Exception:  # noqa: BLE001 - 逐次调用账本只是增强信息
            model_calls = None

    alerts: list[str] = []
    status = "ok"
    if ledger_note:
        status = _worse(status, "warn")
        alerts.append(ledger_note)
    if totals["jobs"] >= 4 and totals["failure_rate"] >= FAILURE_WARN_RATE:
        status = _worse(status, "warn")
        alerts.append("AI 任务失败率 %.1f%%（%d/%d）" % (
            totals["failure_rate"] * 100, totals["failed"], totals["jobs"],
        ))
    if ocr["jobs"] >= 3 and ocr["duration_p95_ms"] is not None and ocr["duration_p95_ms"] >= OCR_P95_WARN_MS:
        status = _worse(status, "warn")
        alerts.append("OCR 耗时 P95 %.1f 秒，超过 %d 秒" % (
            ocr["duration_p95_ms"] / 1000, OCR_P95_WARN_MS // 1000,
        ))
    if ocr["jobs"] >= 3 and ocr["failure_rate"] >= 0.3:
        status = _worse(status, "warn")
        alerts.append("OCR 失败率 %.1f%%" % (ocr["failure_rate"] * 100))

    detail = "近 %d 天任务 %d 个，失败率 %.1f%%，token %d，费用%s" % (
        window, totals["jobs"], totals["failure_rate"] * 100, totals["tokens_total"],
        "未知（未配置单价）" if totals["cost_micro"] is None else "已按单价估算",
    )
    return _result(
        status, detail, alerts,
        window_days=window,
        source=source,
        totals=totals,
        by_kind=kind_items[:20],
        truncated=len(kind_items) > 20,
        ocr=ocr,
        model_calls=model_calls,
    )


# ---------------------------------------------------------------------------
# media：课堂视频 / 旁白渲染队列、素材与能力
# ---------------------------------------------------------------------------

def _media_capabilities() -> dict:
    render_info: dict = {"available": False, "detail": "渲染模块不可用"}
    try:
        from .ai import render as ai_render

        probed = ai_render.render_available()
        if isinstance(probed, dict):
            render_info = probed
    except Exception as exc:  # noqa: BLE001 - 能力探测失败只降级展示
        render_info = {"available": False, "detail": "渲染能力探测失败（%s）" % type(exc).__name__}
    ffmpeg_path = shutil.which("ffmpeg") or ""
    render_ok = bool(render_info.get("available"))
    video_ok = render_ok and bool(ffmpeg_path)
    return {
        "render": {
            "available": render_ok,
            "detail": str(render_info.get("detail") or ("可用" if render_ok else "不可用"))[:240],
        },
        "ffmpeg": {
            "available": bool(ffmpeg_path),
            "detail": "可用：%s" % Path(ffmpeg_path).name if ffmpeg_path else "未检测到 ffmpeg（系统 PATH）",
        },
        "pdf": {
            "available": render_ok,
            "detail": "可用：服务端渲染 PDF 讲义" if render_ok else "服务端 PDF 导出不可用",
        },
        "video": {
            "available": video_ok,
            "detail": "可用：渲染引擎 + ffmpeg 合成 MP4" if video_ok else "视频导出不可用（需要渲染引擎与 ffmpeg）",
        },
    }


def _asset_counts(con, tables: dict, since: str) -> dict:
    result = {"total": 0, "recent": 0, "by_kind": {}}
    if "cls_assets" not in tables:
        return result
    rows = con.execute(
        "SELECT kind, COUNT(*) AS total FROM cls_assets GROUP BY kind",
    ).fetchall()
    for row in rows:
        data = _row_dict(row)
        result["by_kind"][str(data.get("kind") or "unknown")] = int(data.get("total") or 0)
        result["total"] += int(data.get("total") or 0)
    recent = con.execute(
        "SELECT COUNT(*) FROM cls_assets WHERE created_at>=?", (since,),
    ).fetchone()
    result["recent"] = int(recent[0] or 0) if recent else 0
    return result


def _status_counts(con, table: str, tables: dict) -> dict:
    if table not in tables:
        return {}
    rows = con.execute(
        'SELECT status, COUNT(*) AS total FROM "%s" GROUP BY status' % table,
    ).fetchall()
    output = {}
    for row in rows:
        data = _row_dict(row)
        output[str(data.get("status") or "unknown")] = int(data.get("total") or 0)
    return output


def _check_media(ctx: dict) -> dict:
    con, tables, now = ctx["con"], ctx["tables"], ctx["now"]
    window = _ai_window_days()
    since = (now - timedelta(days=window)).isoformat()
    timeout_seconds = _job_timeout_seconds()
    lease_seconds = _lease_seconds()

    capabilities = _media_capabilities()
    has_jobs = "ai_jobs" in tables
    terminal_grouped = _terminal_counts(con, since, tasks=MEDIA_TASKS) if has_jobs else {}
    active_stats = _active_job_stats(
        _active_job_rows(con, MEDIA_TASKS), now,
        timeout_seconds=timeout_seconds, lease_seconds=lease_seconds,
    ) if has_jobs else {}

    by_kind: dict[str, dict] = {}
    for kind, counts in terminal_grouped.items():
        entry = by_kind.setdefault(kind, {"kind": kind, "label": _kind_label(kind)})
        entry.update({status: int(counts.get(status) or 0) for status in TERMINAL_JOB_STATUSES})
    for kind, stats in active_stats.items():
        entry = by_kind.setdefault(kind, {"kind": kind, "label": _kind_label(kind)})
        # 同一 kind 可能既有终态计数又有活跃任务：只补零，不能覆盖已统计的终态。
        for status in TERMINAL_JOB_STATUSES:
            entry.setdefault(status, 0)
        entry.update(stats)
    totals = {status: 0 for status in (*ACTIVE_JOB_STATUSES, *TERMINAL_JOB_STATUSES)}
    items = []
    oldest_queued = None
    timed_out = stale_running = 0
    for entry in by_kind.values():
        for status in (*ACTIVE_JOB_STATUSES, *TERMINAL_JOB_STATUSES):
            value = int(entry.get(status) or 0)
            entry[status] = value
            totals[status] += value
        entry.setdefault("oldest_queued_seconds", None)
        entry.setdefault("timed_out", 0)
        entry.setdefault("stale_running", 0)
        entry["failure_rate"] = _failure_rate(entry)
        items.append(entry)
        if entry["oldest_queued_seconds"] is not None:
            oldest_queued = entry["oldest_queued_seconds"] if oldest_queued is None else max(
                oldest_queued, entry["oldest_queued_seconds"],
            )
        timed_out += int(entry.get("timed_out") or 0)
        stale_running += int(entry.get("stale_running") or 0)
    items.sort(key=lambda item: (-int(item.get("failed") or 0), -int(item.get("queued") or 0),
                                 str(item.get("kind") or "")))

    failure_rate = _failure_rate(totals)
    assets = _asset_counts(con, tables, since)
    courses = _status_counts(con, "cls_courses", tables)
    scenes = _status_counts(con, "cls_scenes", tables)
    running_media = int(totals.get("queued") or 0) + int(totals.get("running") or 0) \
        + int(totals.get("cancel_requested") or 0)

    alerts: list[str] = []
    status = "ok"
    terminal_total = sum(int(totals.get(status) or 0) for status in TERMINAL_JOB_STATUSES)
    if terminal_total >= 2 and failure_rate >= 0.34:
        status = _worse(status, "warn")
        alerts.append("渲染/媒体任务失败率 %.1f%%（%d/%d）" % (
            failure_rate * 100, int(totals.get("failed") or 0) + int(totals.get("interrupted") or 0),
            terminal_total,
        ))
    if oldest_queued is not None and oldest_queued >= QUEUE_WARN_SECONDS:
        status = _worse(status, "warn")
        alerts.append("渲染队列最老任务已等待 %d 秒" % oldest_queued)
    if timed_out:
        status = _worse(status, "warn")
        alerts.append("%d 个渲染任务执行超时（阈值 %d 秒）" % (timed_out, timeout_seconds))
    if stale_running:
        status = _worse(status, "warn")
        alerts.append("%d 个渲染任务心跳超期" % stale_running)
    if running_media and not capabilities["video"]["available"]:
        status = _worse(status, "warn")
        alerts.append("有 %d 个渲染任务排队/运行，但当前环境视频导出能力不可用" % running_media)

    detail = "近 %d 天渲染终态 %d 个，失败率 %.1f%%，排队 %d、运行 %d；视频导出%s" % (
        window, terminal_total, failure_rate * 100,
        totals["queued"], totals["running"] + totals["cancel_requested"],
        "可用" if capabilities["video"]["available"] else "不可用",
    )
    return _result(
        status, detail, alerts,
        window_days=window,
        tasks=list(MEDIA_TASKS),
        totals=totals,
        failure_rate=failure_rate,
        oldest_queued_seconds=oldest_queued,
        timed_out=timed_out,
        stale_running=stale_running,
        by_kind=items[:20],
        assets=assets,
        courses=courses,
        scenes=scenes,
        capabilities=capabilities,
    )


# ---------------------------------------------------------------------------
# data：应用库 / 题库 / 备份
# ---------------------------------------------------------------------------

def _file_size(path: Path) -> int | None:
    try:
        return path.stat().st_size if path.is_file() else None
    except OSError:
        return None


def _database_info(ctx: dict) -> dict:
    con = ctx["con"]
    engine = "postgresql" if db.postgres_enabled() else "sqlite"
    info = {
        "engine": engine,
        "readable": True,
        "wait_ms": ctx.get("db_wait_ms"),
        "table_count": len(ctx.get("tables") or ()),
        "size_bytes": None,
        "wal_bytes": None,
        "shm_bytes": None,
        "database": "postgresql" if engine == "postgresql" else "app.sqlite3",
    }
    if engine == "sqlite":
        base = Path(db.DATA_DIR)
        info["size_bytes"] = _file_size(base / "app.sqlite3")
        info["wal_bytes"] = _file_size(base / "app.sqlite3-wal")
        info["shm_bytes"] = _file_size(base / "app.sqlite3-shm")
    else:
        try:
            row = con.execute("SELECT pg_database_size(current_database())").fetchone()
            info["size_bytes"] = int(row[0]) if row and row[0] is not None else None
        except Exception:  # noqa: BLE001 - 引擎统计是增强信息
            info["size_bytes"] = None
    return info


def _bank_info() -> dict:
    path = db.bank_db_path()
    info = {
        "file": "题库.db",
        "present": path.is_file(),
        "readable": False,
        "size_bytes": _file_size(path),
        "version": None,
        "questions": None,
        "expected_version": os.environ.get("STUDY_EXPECTED_BANK_VERSION", "").strip() or None,
        "version_matches": None,
    }
    if not info["present"]:
        return info
    try:
        with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=10) as con:
            columns = {row[1] for row in con.execute("PRAGMA table_info(questions)")}
            info["questions"] = int(con.execute("SELECT COUNT(*) FROM questions").fetchone()[0])
            if "bank_version" in columns:
                row = con.execute("SELECT bank_version FROM questions LIMIT 1").fetchone()
                info["version"] = str(row[0]) if row and row[0] else None
            info["readable"] = True
    except (sqlite3.Error, OSError):
        info["readable"] = False
    expected = info["expected_version"]
    actual = info["version"] or ("baseline" if info["readable"] else None)
    info["version_matches"] = None if not expected else bool(actual == expected)
    return info


def _backup_manifest(path: Path) -> dict | None:
    """只读 ZIP 内的 ``manifest.json``（限制大小，不落盘、不解压其它成员）。"""
    try:
        with zipfile.ZipFile(path, "r") as archive:
            info = archive.getinfo("manifest.json")
            if info.file_size > 2 * 1024 * 1024:
                return None
            raw = archive.read("manifest.json")
        data = json.loads(raw.decode("utf-8"))
        return data if isinstance(data, dict) else None
    except (KeyError, OSError, ValueError, zipfile.BadZipFile):
        return None


def _backup_info(now: datetime) -> dict:
    folder = Path(db.DATA_DIR) / "backups"
    info = {
        "directory": "backups",
        "count": 0,
        "latest": None,
        "latest_age_seconds": None,
        "warn_age_seconds": _backup_warn_seconds(),
        "detail": "",
    }
    if not folder.is_dir():
        info["detail"] = "尚无备份目录（可用 python -m app.ops backup 生成）"
        return info
    try:
        candidates = [item for item in folder.glob("*.zip") if item.is_file()]
        candidates.sort(key=lambda item: item.stat().st_mtime)
    except OSError:
        info["detail"] = "备份目录不可读"
        return info
    info["count"] = len(candidates)
    if not candidates:
        info["detail"] = "尚无备份文件（可用 python -m app.ops backup 生成）"
        return info
    latest = candidates[-1]
    try:
        stat = latest.stat()
    except OSError:
        info["detail"] = "备份文件不可读"
        return info
    manifest = _backup_manifest(latest)
    age = max(0, int((now - datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)).total_seconds()))
    info["latest"] = {
        "name": latest.name,
        "size_bytes": stat.st_size,
        "modified_at": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
        "age_seconds": age,
        "manifest_ok": manifest is not None,
        "created_at": (manifest or {}).get("created_at"),
        "file_count": (manifest or {}).get("file_count"),
        "bank_version": (manifest or {}).get("bank_version"),
        "format_version": (manifest or {}).get("format_version"),
        "key_included": bool((manifest or {}).get("key_included")),
    }
    info["latest_age_seconds"] = age
    info["detail"] = "最近备份 %s（%d 秒前）" % (latest.name, age)
    return info


def _check_data(ctx: dict) -> dict:
    now = ctx["now"]
    database = _database_info(ctx)
    bank = _bank_info()
    backups = _backup_info(now)

    alerts: list[str] = []
    status = "ok"
    if bank["version_matches"] is False:
        status = _worse(status, "error")
        alerts.append("题库版本不匹配：期望 %s，实际 %s" % (
            bank["expected_version"], bank["version"] or "baseline",
        ))
    elif not bank["present"] or not bank["readable"]:
        status = _worse(status, "warn")
        alerts.append("题库文件缺失或不可读（检查 STUDY_BANK_DIR）")
    wal_bytes = database.get("wal_bytes")
    if wal_bytes is not None and wal_bytes >= _wal_warn_bytes():
        status = _worse(status, "warn")
        alerts.append("SQLite WAL 已达 %.1f MB（阈值 %d MB），建议检查备份/检查点" % (
            wal_bytes / (1024 * 1024), _wal_warn_bytes() // (1024 * 1024),
        ))
    wait_ms = database.get("wait_ms")
    if wait_ms is not None and wait_ms >= DB_WAIT_WARN_MS:
        status = _worse(status, "warn")
        alerts.append("应用库响应 %.0f ms，高于 %d ms" % (wait_ms, DB_WAIT_WARN_MS))
    latest = backups.get("latest")
    if latest is not None:
        if not latest.get("manifest_ok"):
            status = _worse(status, "warn")
            alerts.append("最近备份清单无法解析，建议重新执行备份校验")
        age = latest.get("age_seconds") or 0
        if age >= backups.get("warn_age_seconds") or 0:
            status = _worse(status, "warn")
            alerts.append("最近备份已 %.1f 小时前（阈值 %d 小时）" % (
                age / 3600, (backups.get("warn_age_seconds") or 0) // 3600,
            ))

    db_detail = "%s 可读，%d 张表" % (database["engine"], database["table_count"])
    if database.get("size_bytes") is not None:
        db_detail += "，库 %.1f MB" % (database["size_bytes"] / (1024 * 1024))
    if wal_bytes:
        db_detail += "，WAL %.1f MB" % (wal_bytes / (1024 * 1024))
    detail = "%s；题库 %s（%s 题）；%s" % (
        db_detail,
        bank.get("version") or ("可读" if bank["readable"] else "不可用"),
        bank.get("questions") if bank.get("questions") is not None else "?",
        backups.get("detail") or "无备份记录",
    )
    return _result(
        status, detail, alerts,
        engine=database["engine"],
        database=database,
        bank=bank,
        backups=backups,
    )


# ---------------------------------------------------------------------------
# plaza：撤回传播抽样 + 反馈积压（只读查询，不改广场业务逻辑）
# ---------------------------------------------------------------------------

def _feedback_backlog(con, tables: dict, now: datetime) -> dict:
    output = {"issues": 0, "reports": 0, "appeals": 0, "total_pending": 0,
              "oldest_pending_seconds": None}
    mapping = (
        ("issues", "plaza_issues"),
        ("reports", "plaza_reports"),
        ("appeals", "plaza_appeals"),
    )
    for key, table in mapping:
        if table not in tables:
            continue
        try:
            row = con.execute(
                'SELECT COUNT(*) AS total, MIN(created_at) AS oldest FROM "%s" WHERE status=?' % table,
                ("pending",),
            ).fetchone()
        except Exception:  # noqa: BLE001 - 单表异常只跳过该计数
            continue
        data = _row_dict(row) if row is not None else {}
        count = int(data.get("total") or 0)
        output[key] = count
        output["total_pending"] += count
        age = _age_seconds(data.get("oldest"), now)
        if age is not None:
            output["oldest_pending_seconds"] = age if output["oldest_pending_seconds"] is None \
                else max(output["oldest_pending_seconds"], age)
    return output


def _withdrawn_propagation(con, now: datetime) -> dict:
    """撤回传播抽样：被撤回的发布不得再出现在公开读查询里。

    公开可见口径与 ``app/plaza/public.py`` 一致：``plaza_submissions.status`` 属于
    PUBLIC_STATUSES 且 ``public_id`` 非空。这里只做只读抽样断言，不写任何表。
    """
    public_statuses = _public_statuses()
    result = {
        "public_statuses": list(public_statuses),
        "sample_size": 0,
        "checked": 0,
        "leaked": 0,
        "leaked_items": [],
        "orphan_active": 0,
        "orphan_active_items": [],
        "published_without_active": 0,
        "recent_withdrawn": 0,
    }
    rows = con.execute(
        "SELECT r.id AS release_id, r.submission_id, r.public_id AS release_public_id, r.withdrawn_at, "
        "s.status AS submission_status, s.public_id AS submission_public_id "
        "FROM plaza_releases r LEFT JOIN plaza_submissions s ON s.id=r.submission_id "
        "WHERE r.status='withdrawn' ORDER BY r.id DESC LIMIT ?",
        (SAMPLE_LIMIT,),
    ).fetchall()
    samples = [_row_dict(row) for row in rows]
    result["sample_size"] = len(samples)
    since = (now - timedelta(days=_ai_window_days())).isoformat()
    result["recent_withdrawn"] = sum(
        1 for row in samples if str(row.get("withdrawn_at") or "") >= since
    )

    release_public_ids = [
        str(row.get("release_public_id") or "") for row in samples
        if str(row.get("release_public_id") or "")
    ]
    if release_public_ids:
        # 只有「没有任何 active 发布指针」的同 public_id 才算撤回传播泄露：
        # 稿件被撤回后按同一 public_id 重新签发新版（或 restore）时，旧撤回记录
        # 仍留在 plaza_releases 历史里，但当前公开可见是合法的新发布，不能误报。
        visible = con.execute(
            "SELECT s.public_id, s.status FROM plaza_submissions s "
            "WHERE s.public_id IN (%s) AND s.status IN (%s) "
            "AND NOT EXISTS (SELECT 1 FROM plaza_releases a "
            "WHERE a.public_id=s.public_id AND a.status='active')" % (
                _placeholders(release_public_ids), _placeholders(public_statuses),
            ),
            (*release_public_ids, *public_statuses),
        ).fetchall()
        leaked = [_row_dict(row) for row in visible]
        result["leaked"] = len(leaked)
        result["leaked_items"] = [
            {"public_id": str(row.get("public_id") or ""), "status": str(row.get("status") or "")}
            for row in leaked[:20]
        ]

    active_rows = con.execute(
        "SELECT r.id AS release_id, r.submission_id, r.public_id AS release_public_id, "
        "s.status AS submission_status "
        "FROM plaza_releases r LEFT JOIN plaza_submissions s ON s.id=r.submission_id "
        "WHERE r.status='active' ORDER BY r.id DESC LIMIT ?",
        (SAMPLE_LIMIT,),
    ).fetchall()
    orphan = []
    for row in active_rows:
        data = _row_dict(row)
        if str(data.get("submission_status") or "") not in public_statuses:
            orphan.append({
                "submission_id": data.get("submission_id"),
                "public_id": str(data.get("release_public_id") or ""),
                "submission_status": str(data.get("submission_status") or ""),
            })
    result["orphan_active"] = len(orphan)
    result["orphan_active_items"] = orphan[:20]

    result["checked"] = len(samples)
    try:
        row = con.execute(
            "SELECT COUNT(*) FROM plaza_submissions s WHERE s.status='published' "
            "AND NOT EXISTS (SELECT 1 FROM plaza_releases r "
            "WHERE r.submission_id=s.id AND r.status='active')",
        ).fetchone()
        result["published_without_active"] = int(row[0] or 0) if row else 0
    except Exception:  # noqa: BLE001 - 一致性统计失败不影响抽样断言
        result["published_without_active"] = 0
    return result


def _check_plaza(ctx: dict) -> dict:
    con, tables, now = ctx["con"], ctx["tables"], ctx["now"]
    required = ("plaza_submissions", "plaza_releases")
    if any(table not in tables for table in required):
        return _result(
            "ok",
            "广场表尚未初始化（暂无投稿与发布）",
            initialized=False,
            feedback={"issues": 0, "reports": 0, "appeals": 0, "total_pending": 0,
                      "oldest_pending_seconds": None},
            releases={"active": 0, "withdrawn": 0},
            withdrawn={"sample_size": 0, "checked": 0, "leaked": 0, "leaked_items": [],
                       "orphan_active": 0, "orphan_active_items": [],
                       "published_without_active": 0, "recent_withdrawn": 0},
        )

    feedback = _feedback_backlog(con, tables, now)
    withdrawn = _withdrawn_propagation(con, now)
    releases = {
        "active": int(con.execute(
            "SELECT COUNT(*) FROM plaza_releases WHERE status='active'").fetchone()[0] or 0),
        "withdrawn": int(con.execute(
            "SELECT COUNT(*) FROM plaza_releases WHERE status='withdrawn'").fetchone()[0] or 0),
        "withdrawn_recent": withdrawn["recent_withdrawn"],
    }

    alerts: list[str] = []
    status = "ok"
    if withdrawn["leaked"]:
        status = _worse(status, "error")
        alerts.append("撤回传播异常：%d 条已撤回发布仍出现在公开读查询中" % withdrawn["leaked"])
    if withdrawn["orphan_active"]:
        status = _worse(status, "error")
        alerts.append("发布指针异常：%d 个活跃发布对应的投稿不在公开状态" % withdrawn["orphan_active"])
    if withdrawn["published_without_active"]:
        status = _worse(status, "warn")
        alerts.append("存在 %d 条已发布但没有活跃发布指针的投稿（公开读不可见）"
                      % withdrawn["published_without_active"])
    if feedback["total_pending"] >= _feedback_warn_pending():
        status = _worse(status, "warn")
        alerts.append("未处理反馈积压 %d 条（阈值 %d）" % (
            feedback["total_pending"], _feedback_warn_pending(),
        ))
    oldest = feedback.get("oldest_pending_seconds")
    if oldest is not None and oldest >= FEEDBACK_WARN_AGE_SECONDS:
        status = _worse(status, "warn")
        alerts.append("最老未处理反馈已积压 %.1f 小时" % (oldest / 3600))

    detail = "发布 %d（撤回 %d）；撤回抽样 %d 条、异常 %d；未处理反馈 %d 条" % (
        releases["active"], releases["withdrawn"], withdrawn["checked"],
        withdrawn["leaked"] + withdrawn["orphan_active"], feedback["total_pending"],
    )
    return _result(
        status, detail, alerts,
        initialized=True,
        feedback=feedback,
        releases=releases,
        withdrawn=withdrawn,
    )


_CHECK_FUNCS: dict[str, Callable[[dict], dict]] = {
    "tasks": _check_tasks,
    "ai": _check_ai,
    "media": _check_media,
    "data": _check_data,
    "plaza": _check_plaza,
}


# ---------------------------------------------------------------------------
# 采集 / 缓存 / 接口
# ---------------------------------------------------------------------------

def _collect_fresh(build_id: str = "", version: str = "") -> dict:
    started = time.perf_counter()
    now = _utcnow()
    # 每个检查独立连接与异常边界：单组失败只降级该组（PG 事务 aborted 也不外溢）。
    checks = {name: _run_check(name, now) for name in CHECK_ORDER}

    ordered_checks = {name: checks[name] for name in CHECK_ORDER}
    overall = "ok"
    for check in ordered_checks.values():
        overall = _worse(overall, str(check.get("status") or "ok"))
    statuses = {name: str(check.get("status") or "ok") for name, check in ordered_checks.items()}
    degraded = [name for name, status in statuses.items() if status != "ok"]
    return {
        "ok": overall != "error",
        "status": overall,
        "generated_at": now.isoformat(),
        "duration_ms": round((time.perf_counter() - started) * 1000, 3),
        "build": {
            "build_id": build_id,
            "version": version,
            "bank_version": db.bank_version_hint() or None,
        },
        "window_days": _ai_window_days(),
        "thresholds": {
            "queue_warn_seconds": QUEUE_WARN_SECONDS,
            "queue_error_seconds": QUEUE_ERROR_SECONDS,
            "failure_warn_rate": FAILURE_WARN_RATE,
            "job_timeout_seconds": _job_timeout_seconds(),
            "lease_seconds": _lease_seconds(),
            "ocr_p95_warn_ms": OCR_P95_WARN_MS,
            "feedback_warn_pending": _feedback_warn_pending(),
            "feedback_warn_age_seconds": FEEDBACK_WARN_AGE_SECONDS,
            "backup_warn_age_seconds": _backup_warn_seconds(),
            "wal_warn_bytes": _wal_warn_bytes(),
            "db_wait_warn_ms": DB_WAIT_WARN_MS,
        },
        "degraded": degraded,
        "checks": ordered_checks,
        "cache": {"ttl_seconds": _cache_ttl_seconds(), "cached": False, "age_seconds": 0.0},
    }


def collect_monitor(*, build_id: str = "", version: str = "", force: bool = False) -> dict:
    """采集运维监控数据（带进程内短缓存）；``force=True`` 绕过读缓存。"""
    ttl = _cache_ttl_seconds()
    if not force and ttl > 0:
        cached = _cache_lookup(ttl)
        if cached is not None:
            return cached
    with _cache_lock:
        if not force and ttl > 0:
            cached = _cache_lookup(ttl)
            if cached is not None:
                return cached
        payload = _collect_fresh(build_id=build_id, version=version)
        _cache_state.update({
            "key": _cache_key(),
            "at": time.monotonic(),
            "payload": copy.deepcopy(payload),
        })
        return payload


def _probe_token_ok(request: Request) -> bool:
    """部署探针口令（``STUDY_HEALTH_TOKEN`` + ``X-Health-Token``）只读访问监控。

    复用 ``app.main`` 的常量时间比较，避免两处实现漂移；循环导入在请求期才发生。
    """
    try:
        from .main import _health_token_ok

        return bool(_health_token_ok(request))
    except Exception:  # noqa: BLE001 - 探针口令只是增强入口，失败按未授权处理
        return False


def monitor_access(request: Request, user=Depends(auth.optional_user)):
    """监控看板访问：管理员会话，或部署探针口令（只读）。"""
    if _probe_token_ok(request):
        return {"role": "probe", "username": "health-probe", "via": "health_token"}
    if not user:
        raise HTTPException(401, "请先登录")
    if user.get("role") != "admin":
        raise HTTPException(403, "需要管理员权限")
    return {**user, "via": "session"}


@router.get("/admin/monitor")
def admin_monitor(
    request: Request,
    refresh: bool = Query(False, description="绕过进程内短缓存重新采集"),
    user=Depends(monitor_access),
):
    """运维监控看板：任务 / AI / 渲染 / 数据 / 广场五组只读检查（管理员或部署探针口令）。"""
    from .main import BUILD_ID, VERSION

    return collect_monitor(build_id=BUILD_ID, version=VERSION, force=bool(refresh))


__all__ = [
    "router",
    "collect_monitor",
    "monitor_access",
    "reset_cache",
    "CHECK_ORDER",
    "CHECK_LABELS",
    "ACTIVE_JOB_STATUSES",
    "TERMINAL_JOB_STATUSES",
    "MEDIA_TASKS",
    "OCR_TASKS",
]
