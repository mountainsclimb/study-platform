"""专注计时（番茄钟）：顶栏胶囊的会话记录与统计。

契约（对齐 `INTEGRATION-CONTRACT.md`）：

* 前端计时器只负责「按绝对时间戳推算剩余」，服务端不跑任何定时任务；
* 一条会话由前端在结束/停止时提交一次，`client_id` 作为幂等键，重复提交（
  `pagehide` 兜底、网络重试）不会重复计数；
* 所有表带 `user_id`，跨账号一律 404；
* 统计只累计真实会话（`focus` 模式的已完成/已停止），没有记录就返回 0，
  绝不按计划时长估算——「不伪造学习数据」是本项目的一贯口径；
* 日期分桶用前端提交的 `local_date`：用户在中国时区的 23:50 计入当天，而不是
  UTC 次日的 07:50。服务端只校验它与 UTC 日期的偏差不超过 1 天。

计时状态机（开始/暂停/恢复/结束）vendored 自 shigure（MIT），见
`web/vendor/shigure/README.md`。
"""
from datetime import date, datetime, timedelta, timezone
import re

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from . import db
from .auth import current_user

router = APIRouter(prefix="/api/focus", tags=["Focus timer"])

MODES = ("focus", "short_break", "long_break")
STATUSES = ("completed", "stopped", "abandoned")
MAX_PLANNED_MINUTES = 180
# 前端可能在到点后补一次「结束」请求；允许 90 秒的收尾误差，但拒绝明显造假。
MAX_ELAPSED_SLACK_SECONDS = 90
MAX_TASK_CHARS = 80
MAX_PAGE_SIZE = 100
DEFAULT_PAGE_SIZE = 20
MAX_SUMMARY_DAYS = 365
_CLIENT_ID = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
_LOCAL_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS focus_sessions (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    client_id TEXT NOT NULL,
    mode TEXT NOT NULL DEFAULT 'focus',
    planned_minutes INTEGER NOT NULL,
    elapsed_seconds INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'completed',
    task_label TEXT NOT NULL DEFAULT '',
    local_date TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS focus_sessions_client ON focus_sessions(user_id, client_id);
CREATE INDEX IF NOT EXISTS focus_sessions_user_date ON focus_sessions(user_id, local_date);
"""


def init_focus() -> None:
    """幂等建表：由 `app.main.lifespan` 在启动时调用。"""
    with db.connection() as con:
        con.executescript(_SCHEMA)


# ---------------------------------------------------------------------------
# 校验与转换
# ---------------------------------------------------------------------------


class SessionIn(BaseModel):
    client_id: str = Field(min_length=8, max_length=64)
    mode: str = Field(default="focus", max_length=16)
    planned_minutes: int = Field(ge=1, le=MAX_PLANNED_MINUTES)
    elapsed_seconds: int = Field(default=0, ge=0)
    status: str = Field(default="completed", max_length=16)
    task_label: str = Field(default="", max_length=MAX_TASK_CHARS)
    local_date: str = Field(min_length=10, max_length=10)
    started_ms: int = Field(ge=0)
    ended_ms: int | None = Field(default=None, ge=0)


def _utc_date() -> date:
    return datetime.now(timezone.utc).date()


def _parse_local_date(value: str) -> date:
    if not _LOCAL_DATE.match(value):
        raise HTTPException(422, "local_date 需要 YYYY-MM-DD 格式")
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        raise HTTPException(422, "local_date 不是合法日期") from None
    # 中国时区最多比 UTC 早 1 天（凌晨 0–8 点），不接受明显错误的日期。
    delta = (parsed - _utc_date()).days
    if delta < -1 or delta > 1:
        raise HTTPException(422, "local_date 与服务器时间相差过大，请检查设备时间")
    return parsed


def _ms_to_iso(value: int, field: str) -> str:
    if value > int(datetime.now(timezone.utc).timestamp() * 1000) + 86400000:
        raise HTTPException(422, f"{field} 不能晚于当前时间一天以上")
    try:
        return datetime.fromtimestamp(value / 1000, tz=timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        raise HTTPException(422, f"{field} 不是合法时间戳") from None


def _validate(payload: SessionIn) -> dict:
    if not _CLIENT_ID.match(payload.client_id):
        raise HTTPException(422, "client_id 只能包含字母、数字、下划线和连字符")
    if payload.mode not in MODES:
        raise HTTPException(422, "mode 只能是 focus / short_break / long_break")
    if payload.status not in STATUSES:
        raise HTTPException(422, "status 只能是 completed / stopped / abandoned")
    limit = payload.planned_minutes * 60 + MAX_ELAPSED_SLACK_SECONDS
    if payload.elapsed_seconds > limit:
        raise HTTPException(422, "elapsed_seconds 超过了计划时长允许的误差范围")
    # 日期分桶以用户本地日期为准，但只接受与 UTC 日期相差 ≤1 天，防止伪造历史。
    local_day = _parse_local_date(payload.local_date)
    started = _ms_to_iso(payload.started_ms, "started_ms")
    ended = _ms_to_iso(payload.ended_ms, "ended_ms") if payload.ended_ms else None
    if ended and payload.ended_ms < payload.started_ms:
        raise HTTPException(422, "ended_ms 不能早于 started_ms")
    return {
        "client_id": payload.client_id,
        "mode": payload.mode,
        "planned_minutes": payload.planned_minutes,
        "elapsed_seconds": payload.elapsed_seconds,
        "status": payload.status,
        "task_label": payload.task_label.strip()[:MAX_TASK_CHARS],
        "local_date": local_day.isoformat(),
        "started_at": started,
        "ended_at": ended,
    }


def _public(row) -> dict:
    return {
        "id": row["id"],
        "client_id": row["client_id"],
        "mode": row["mode"],
        "planned_minutes": row["planned_minutes"],
        "elapsed_seconds": row["elapsed_seconds"],
        "status": row["status"],
        "task_label": row["task_label"],
        "local_date": row["local_date"],
        "started_at": row["started_at"],
        "ended_at": row["ended_at"],
        "created_at": row["created_at"],
    }


_COLUMNS = (
    "id, user_id, client_id, mode, planned_minutes, elapsed_seconds, status, "
    "task_label, local_date, started_at, ended_at, created_at"
)


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


@router.post("/sessions")
def create_session(payload: SessionIn, user=Depends(current_user)):
    """记录一次专注会话；同一 `client_id` 重复提交只保留第一条（幂等）。"""
    data = _validate(payload)
    now = db.utcnow()
    with db.connection() as con:
        con.execute(
            "INSERT INTO focus_sessions "
            "(user_id, client_id, mode, planned_minutes, elapsed_seconds, status, task_label, "
            " local_date, started_at, ended_at, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(user_id, client_id) DO NOTHING",
            (
                user["id"], data["client_id"], data["mode"], data["planned_minutes"],
                data["elapsed_seconds"], data["status"], data["task_label"],
                data["local_date"], data["started_at"], data["ended_at"], now, now,
            ),
        )
        row = con.execute(
            f"SELECT {_COLUMNS} FROM focus_sessions WHERE user_id=? AND client_id=?",
            (user["id"], data["client_id"]),
        ).fetchone()
    return {"item": _public(row), "created": row["created_at"] == now}


@router.get("/sessions")
def list_sessions(
    days: int = Query(7, ge=1, le=MAX_SUMMARY_DAYS),
    page: int = Query(1, ge=1),
    page_size: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    user=Depends(current_user),
):
    since = (_utc_date() - timedelta(days=days)).isoformat()
    with db.connection() as con:
        total = con.execute(
            "SELECT COUNT(*) FROM focus_sessions WHERE user_id=? AND local_date>=?",
            (user["id"], since),
        ).fetchone()[0]
        rows = con.execute(
            f"SELECT {_COLUMNS} FROM focus_sessions WHERE user_id=? AND local_date>=? "
            "ORDER BY started_at DESC, id DESC LIMIT ? OFFSET ?",
            (user["id"], since, page_size, (page - 1) * page_size),
        ).fetchall()
    return {
        "items": [_public(row) for row in rows],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@router.get("/summary")
def summary(
    days: int = Query(7, ge=1, le=MAX_SUMMARY_DAYS),
    today: str | None = Query(None, min_length=10, max_length=10),
    user=Depends(current_user),
):
    """专注统计：今日/近 N 天/连续天数。无记录时全部为 0，不做估算。"""
    anchor = _parse_local_date(today) if today else _utc_date()
    window_start = anchor - timedelta(days=days - 1)
    with db.connection() as con:
        rows = con.execute(
            "SELECT local_date, mode, elapsed_seconds, status, started_at "
            "FROM focus_sessions WHERE user_id=? AND local_date>=? ORDER BY local_date",
            (user["id"], window_start.isoformat()),
        ).fetchall()
        totals = con.execute(
            "SELECT COUNT(*), COALESCE(SUM(elapsed_seconds),0) FROM focus_sessions "
            "WHERE user_id=? AND mode='focus' AND status!='abandoned'",
            (user["id"],),
        ).fetchone()
        last = con.execute(
            f"SELECT {_COLUMNS} FROM focus_sessions WHERE user_id=? "
            "ORDER BY started_at DESC, id DESC LIMIT 1",
            (user["id"],),
        ).fetchone()

    by_day: dict[str, dict] = {}
    for offset in range(days):
        key = (window_start + timedelta(days=offset)).isoformat()
        by_day[key] = {"date": key, "minutes": 0, "sessions": 0}
    for row in rows:
        if row["mode"] != "focus" or row["status"] == "abandoned":
            continue
        bucket = by_day.get(row["local_date"])
        if bucket is None:
            continue
        bucket["minutes"] += max(0, int(row["elapsed_seconds"] or 0)) / 60.0
        bucket["sessions"] += 1
    for bucket in by_day.values():
        bucket["minutes"] = int(round(bucket["minutes"]))

    today_key = anchor.isoformat()
    today_minutes = by_day.get(today_key, {}).get("minutes", 0)
    week_start = (anchor - timedelta(days=6)).isoformat()
    week_minutes = sum(
        item["minutes"] for key, item in by_day.items() if key >= week_start
    )
    streak_days = 0
    cursor_day = anchor if today_minutes > 0 or by_day.get(today_key, {}).get("sessions", 0) > 0 else anchor - timedelta(days=1)
    while True:
        item = by_day.get(cursor_day.isoformat())
        # 打卡按「当天有一次真实专注会话」计，而不是按取整后的分钟数（40 秒的会话也算今天来过）。
        if not item or item["sessions"] <= 0:
            break
        streak_days += 1
        cursor_day -= timedelta(days=1)
    return {
        "today": today_key,
        "days": days,
        "today_minutes": today_minutes,
        "week_minutes": int(week_minutes),
        "total_minutes": int(round((totals[1] or 0) / 60.0)),
        "total_sessions": int(totals[0] or 0),
        "streak_days": streak_days,
        "streak_includes_today": by_day.get(today_key, {}).get("sessions", 0) > 0,
        "by_day": [by_day[key] for key in sorted(by_day)],
        "last_session": _public(last) if last else None,
    }


@router.delete("/sessions/{session_id}")
def delete_session(session_id: int, user=Depends(current_user)):
    with db.connection() as con:
        row = con.execute(
            "SELECT id FROM focus_sessions WHERE id=? AND user_id=?",
            (session_id, user["id"]),
        ).fetchone()
        if not row:
            raise HTTPException(404, "会话不存在")
        con.execute(
            "DELETE FROM focus_sessions WHERE id=? AND user_id=?",
            (session_id, user["id"]),
        )
    return {"deleted": True, "id": session_id}
