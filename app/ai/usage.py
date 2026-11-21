"""AI 任务用量账本（``ai_job_usage``）与按任务类型的配额 / 全站并发。

与既有 ``ai_usage`` 的分工（不重复、不覆盖）：

* ``ai_usage``（``providers.record_usage``）是「逐次模型调用」的 token 账本，调用点与既有
  设置页用量表沿用该表，本模块不改它们；
* ``ai_job_usage``（本模块）是「每个任务一行、终态写一次」的可追踪账本：模型、状态、耗时、
  重试次数、token 汇总、费用估算（可选价格表）与入参分类。两张表通过 ``job_id`` 关联。

设计约束：

* 只记录任务类型、模型、token / 耗时与**入参分类**（text/image/pdf/audio/none），不记录题干、
  资料正文、文件名等用户内容，也不记录可反查私密内容的标识；``meta_json`` 只放分类与计数。
* 任务成功 / 失败 / 取消 / 中断都会入账（可观测、可对账）；但每日配额只统计「未失败」的任务，
  失败与取消不消耗配额。
* 配额与并发按任务类型（``kind`` = ``ai_jobs.task``）设置：先查管理员在 ``ai_quotas`` 的覆盖值，
  再查环境变量 ``STUDY_AI_QUOTA_<KIND>`` / ``STUDY_AI_CONCURRENCY_<KIND>``，最后用保守默认值；
  完全未配置的任务类型不阻断，只在日志里记一条警告。
* 全站并发计数以数据库为准（SQLite / PostgreSQL 同一套 SQL，跨进程一致），并用进程内活跃任务
  注册表在数据库计数异常时兜底；超限统一返回 429 与可读提示。
* 账本写入失败只记日志，绝不影响任务状态机、SSE 或产品结果。
"""
from datetime import datetime, timedelta, timezone
import json
import logging
import math
import os
import re

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from .. import db
from ..auth import current_user, require_admin

router = APIRouter(prefix="/api", tags=["AI usage & quotas"])
log = logging.getLogger(__name__)

USAGE_TABLE = "ai_job_usage"
QUOTA_TABLE = "ai_quotas"

TERMINAL_STATUSES = ("completed", "failed", "interrupted", "cancelled")
# 占用「全站并发」的未失败状态；awaiting_outline 是等待人工确认提纲的在途任务，
# 执行进程已退出、不再占用算力，不能计入并发（否则暂停的课程会永久占坑导致后续 429）。
# 它仍占用「每人每日配额」的在途保护见 daily_used 的独立口径。
CONSUMING_ACTIVE_STATES = ("queued", "running", "cancel_requested")
NON_CONSUMING_STATES = ("failed", "cancelled", "interrupted")
INPUT_KINDS = ("text", "image", "pdf", "audio", "none")

DEFAULT_USAGE_RETENTION_DAYS = 180
MAX_QUERY_ROWS = 10000
ADMIN_PAGE_SIZE = 20
ME_RECENT_LIMIT = 10

QUOTA_ENV_PREFIX = "STUDY_AI_QUOTA_"
CONCURRENCY_ENV_PREFIX = "STUDY_AI_CONCURRENCY_"
RETENTION_ENV = "STUDY_AI_USAGE_RETENTION_DAYS"

# 默认每人每日配额（UTC 自然日，失败/取消不计数）：保守起步，管理员可在管理台按任务类型覆盖。
DEFAULT_DAILY_QUOTAS = {
    "explain": 60,
    "grade": 30,
    "question-analysis": 30,
    "notes": 20,
    "recognize": 30,
    "tagging": 10,
    "ocr": 20,
    "course": 10,
    "course-narration": 10,
    "course-video": 5,
    "paper-import": 20,
    "paper-enrich": 20,
    "paper-compose": 20,
    "paper-revision": 20,
    "paper-annotate": 20,
    "assistant-turn": 100,
    "insight-report": 10,
}
# 默认全站同时占用的任务数上限（按任务类型）：与全局并发 STUDY_AI_GLOBAL_CONCURRENCY=8 配合使用。
DEFAULT_CONCURRENCY = {
    "explain": 6,
    "grade": 4,
    "question-analysis": 4,
    "notes": 3,
    "recognize": 3,
    "tagging": 2,
    "ocr": 3,
    "course": 2,
    "course-narration": 2,
    "course-video": 1,
    "paper-import": 2,
    "paper-enrich": 2,
    "paper-compose": 2,
    "paper-revision": 2,
    "paper-annotate": 2,
    "assistant-turn": 6,
    "insight-report": 2,
}
# 错误提示用的中文任务名；扩展任务优先取 register_task_handler 的 label。
DEFAULT_LABELS = {
    "explain": "AI 讲解",
    "grade": "主观评分",
    "question-analysis": "题目分析",
    "notes": "知识笔记",
    "recognize": "拍题识别",
    "tagging": "批量打标",
    "ocr": "扫描识别",
    "course": "AI 课堂",
    "course-narration": "课堂旁白",
    "course-video": "课堂视频",
    "paper-import": "试卷识别",
    "paper-enrich": "答案补全",
    "paper-compose": "AI 组卷",
    "paper-revision": "错题巩固卷",
    "paper-annotate": "试卷切题",
    "assistant-turn": "AI 助手",
    "insight-report": "学情报告",
}

# 进程内活跃任务注册表（job_id → kind）：仅作为数据库计数异常时的并发兜底，正常路径以数据库为准。
_active_jobs: dict[int, str] = {}
_ready_key = None


# ---------------------------------------------------------------------------
# 建表 / 初始化
# ---------------------------------------------------------------------------

def _ensure_tables():
    """幂等建表并补齐 ``ai_jobs`` 的新列（老库不重建表）。"""
    with db.connection() as con:
        db.write_lock(con, "ai-usage-schema")
        con.executescript("""
            CREATE TABLE IF NOT EXISTS ai_job_usage(
                id INTEGER PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                job_id INTEGER NOT NULL,
                kind TEXT NOT NULL,
                provider_id INTEGER,
                model TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL,
                duration_ms INTEGER NOT NULL DEFAULT 0,
                retries INTEGER NOT NULL DEFAULT 0,
                tokens_in INTEGER NOT NULL DEFAULT 0,
                tokens_out INTEGER NOT NULL DEFAULT 0,
                tokens_estimate INTEGER NOT NULL DEFAULT 0,
                cost_micro INTEGER,
                created_at TEXT NOT NULL,
                meta_json TEXT NOT NULL DEFAULT '{}');
            CREATE UNIQUE INDEX IF NOT EXISTS idx_ai_job_usage_job ON ai_job_usage(job_id);
            CREATE INDEX IF NOT EXISTS idx_ai_job_usage_user_date ON ai_job_usage(user_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_ai_job_usage_kind_date ON ai_job_usage(kind, created_at);
            CREATE TABLE IF NOT EXISTS ai_quotas(
                kind TEXT PRIMARY KEY,
                daily_limit INTEGER,
                concurrency_limit INTEGER,
                updated_by INTEGER,
                updated_at TEXT NOT NULL);
        """)
        # 任务耗时与重试次数：老库用 ensure_column 幂等补齐，不重建 ai_jobs。
        db.ensure_column(con, "ai_jobs", "started_at", "TEXT")
        db.ensure_column(con, "ai_jobs", "retry_count", "INTEGER NOT NULL DEFAULT 0")


def init_usage():
    """应用启动时调用：建表、补列并按保留策略清理过期账本。"""
    global _ready_key
    try:
        _ensure_tables()
        _ready_key = (str(db.DATA_DIR), db.postgres_enabled())
        removed = purge_old_usage()
        if removed:
            log.info("AI 用量账本已按保留策略清理 %s 行", removed)
    except Exception:  # 账本不可用不能阻断应用启动
        log.warning("初始化 AI 用量账本失败（任务状态机不受影响）", exc_info=True)


def _ensure_ready():
    """懒初始化：直接调用本模块函数（而非走 lifespan）时也能保证表存在。"""
    key = (str(db.DATA_DIR), db.postgres_enabled())
    if _ready_key != key:
        _ensure_tables()
        globals()["_ready_key"] = key


def reset_runtime_state() -> None:
    """清空进程内活跃任务注册表（测试隔离用，不涉及数据库）。"""
    _active_jobs.clear()


# ---------------------------------------------------------------------------
# 任务类型与配额配置
# ---------------------------------------------------------------------------

def known_kinds() -> list[str]:
    """全部已知任务类型（内置 + 整合模块注册 + 默认配额表），用于配额管理页。"""
    from .jobs import EXTRA_TASKS, STANDARD_TASKS

    return sorted(set(DEFAULT_DAILY_QUOTAS) | set(STANDARD_TASKS) | set(EXTRA_TASKS))


def kind_label(kind: str) -> str:
    from .jobs import EXTRA_TASKS

    name = str(kind or "")
    extra = EXTRA_TASKS.get(name) or {}
    return DEFAULT_LABELS.get(name) or str(extra.get("label") or name or "AI")


def _env_suffix(kind: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", str(kind or "")).strip("_").upper()


def _env_limit(prefix: str, kind: str) -> int | None:
    """环境变量覆盖：未设置返回 None；``0`` 表示不限制；非法值告警后忽略。"""
    raw = os.environ.get(prefix + _env_suffix(kind))
    if raw is None or not str(raw).strip():
        return None
    try:
        return max(0, int(str(raw).strip()))
    except (TypeError, ValueError):
        log.warning("环境变量 %s%s=%r 不是整数，已忽略", prefix, _env_suffix(kind), raw)
        return None


def _quota_overrides(con=None) -> dict[str, dict]:
    """读取管理员在 ``ai_quotas`` 中的覆盖值；表不存在/数据库异常时视为无覆盖。

    传入 ``con`` 时复用调用方事务（避免在写事务里再开连接）；否则自建只读连接。
    """
    try:
        if con is None:
            _ensure_ready()
            with db.connection() as own:
                rows = own.execute(
                    "SELECT kind, daily_limit, concurrency_limit, updated_by, updated_at FROM ai_quotas"
                ).fetchall()
        else:
            rows = con.execute(
                "SELECT kind, daily_limit, concurrency_limit, updated_by, updated_at FROM ai_quotas"
            ).fetchall()
    except Exception:
        return {}
    return {str(row["kind"]): dict(row) for row in rows}


def effective_daily_quota(kind: str, con=None) -> tuple[int | None, str]:
    """返回 (每人每日配额, 来源)；``None`` 表示未配置（不阻断并记警告）。

    来源优先级：管理员设置（db） → 环境变量（env） → 内置默认（default） → 未配置（unconfigured）。
    """
    overrides = _quota_overrides(con).get(str(kind)) or {}
    value = overrides.get("daily_limit")
    if value is not None:
        return max(0, int(value)), "db"
    env_value = _env_limit(QUOTA_ENV_PREFIX, kind)
    if env_value is not None:
        return env_value, "env"
    if str(kind) in DEFAULT_DAILY_QUOTAS:
        return DEFAULT_DAILY_QUOTAS[str(kind)], "default"
    return None, "unconfigured"


def effective_concurrency(kind: str, con=None) -> tuple[int | None, str]:
    """返回 (全站并发上限, 来源)；``None`` 表示未配置（不阻断并记警告）。"""
    overrides = _quota_overrides(con).get(str(kind)) or {}
    value = overrides.get("concurrency_limit")
    if value is not None:
        return max(0, int(value)), "db"
    env_value = _env_limit(CONCURRENCY_ENV_PREFIX, kind)
    if env_value is not None:
        return env_value, "env"
    if str(kind) in DEFAULT_CONCURRENCY:
        return DEFAULT_CONCURRENCY[str(kind)], "default"
    return None, "unconfigured"


def _warn_unconfigured(kind: str, what: str) -> None:
    log.warning("AI 任务类型 %r 未配置%s，按不限制处理（可用 STUDY_AI_* 环境变量或管理台配置）", kind, what)


# ---------------------------------------------------------------------------
# 计数：每日配额 / 全站并发
# ---------------------------------------------------------------------------

def _utc_day_start(now: datetime | None = None) -> str:
    moment = now or datetime.now(timezone.utc)
    start = datetime(moment.year, moment.month, moment.day, tzinfo=timezone.utc)
    return start.isoformat()


def _count_consumed(con, *, user_id: int | None, kind: str | None = None,
                    since: str | None = None) -> int:
    """统计「消耗配额」的任务数：未失败/取消/中断，可按用户与任务类型过滤。"""
    where = ["status NOT IN (%s)" % ",".join("?" for _ in NON_CONSUMING_STATES)]
    params: list = list(NON_CONSUMING_STATES)
    if user_id is not None:
        where.append("user_id=?")
        params.append(int(user_id))
    if kind is not None:
        where.append("task=?")
        params.append(str(kind))
    if since is not None:
        where.append("created_at>=?")
        params.append(since)
    return int(con.execute(
        "SELECT COUNT(*) FROM ai_jobs WHERE " + " AND ".join(where), tuple(params)
    ).fetchone()[0])


def daily_used(con, user_id: int, kind: str) -> int:
    """本人今日（UTC 自然日）该任务类型已消耗的配额数。"""
    return _count_consumed(con, user_id=int(user_id), kind=kind, since=_utc_day_start())


def _count_active_jobs(con, kind: str) -> int:
    placeholders = ",".join("?" for _ in CONSUMING_ACTIVE_STATES)
    return int(con.execute(
        f"SELECT COUNT(*) FROM ai_jobs WHERE task=? AND status IN ({placeholders})",
        (str(kind), *CONSUMING_ACTIVE_STATES),
    ).fetchone()[0])


def _memory_active(kind: str) -> int:
    return sum(1 for value in _active_jobs.values() if value == str(kind))


def current_concurrency(kind: str, con=None) -> int:
    """该任务类型当前占用的全站并发数。

    数据库计数是跨进程（PostgreSQL 多 Worker）的权威来源；数据库异常时退化为进程内活跃任务
    注册表，保证单进程模式仍有限流能力。
    """
    try:
        if con is not None:
            return _count_active_jobs(con, kind)
        with db.connection() as own:
            return _count_active_jobs(own, kind)
    except Exception:
        return _memory_active(kind)


def note_job_active(job_id: int, kind: str) -> None:
    """登记一个在途任务（进程内兜底计数用；重复登记幂等）。"""
    try:
        _active_jobs[int(job_id)] = str(kind)
    except (TypeError, ValueError):
        return


def _release_active(job_id: int) -> None:
    try:
        _active_jobs.pop(int(job_id), None)
    except (TypeError, ValueError):
        return


def _quota_error(kind: str, message: str) -> HTTPException:
    return HTTPException(429, {"code": "ai_quota_exceeded", "kind": str(kind), "message": message})


def check_new_job(con, *, user_id: int, kind: str) -> None:
    """创建任务前的准入校验：先查每人每日配额，再查全站按类型并发。

    失败直接抛 429（带 ``code/kind/message``），调用方在同一个事务里、INSERT 之前调用，
    因此被拒绝的请求不会落库、也不消耗配额。
    """
    kind = str(kind)
    label = kind_label(kind)
    daily_limit, _daily_source = effective_daily_quota(kind, con)
    if daily_limit is None:
        _warn_unconfigured(kind, "每日配额")
    elif daily_limit > 0:
        used = daily_used(con, int(user_id), kind)
        if used + 1 > daily_limit:
            raise _quota_error(
                kind,
                f"{label}任务今日配额已用完（{used}/{daily_limit}，按 UTC 自然日统计，失败任务不计数），"
                f"请稍后再试或联系管理员调整配额",
            )
    concurrency_limit, _source = effective_concurrency(kind, con)
    if concurrency_limit is None:
        _warn_unconfigured(kind, "全站并发上限")
    elif concurrency_limit > 0:
        in_use = current_concurrency(kind, con)
        if in_use >= concurrency_limit:
            raise _quota_error(
                kind,
                f"{label}任务并发已达上限（全站最多 {concurrency_limit} 个同时进行），请稍后重试",
            )


def check_resume(con, *, kind: str, job_id: int) -> None:
    """续跑 / 重新执行前的并发校验：该任务当前不占并发，只检查同类型其他在途任务。"""
    kind = str(kind)
    concurrency_limit, _source = effective_concurrency(kind, con)
    if concurrency_limit is None or concurrency_limit <= 0:
        return
    in_use = current_concurrency(kind, con)
    if in_use >= concurrency_limit:
        raise _quota_error(
            kind,
            f"{kind_label(kind)}任务并发已达上限（全站最多 {concurrency_limit} 个同时进行），请稍后重试",
        )


# ---------------------------------------------------------------------------
# 用量入账
# ---------------------------------------------------------------------------

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


def _duration_ms(job_row) -> int:
    """任务耗时：优先 started_at → finished_at；未开始执行的任务从创建时间起算。"""
    started = _parse_iso(job_row["started_at"]) or _parse_iso(job_row["created_at"])
    finished = _parse_iso(job_row["finished_at"]) or _parse_iso(job_row["updated_at"])
    if not started or not finished:
        return 0
    return max(0, int((finished - started).total_seconds() * 1000))


def _classify_input(con, task: str, request: dict) -> str:
    """只按分类记录实际入参类型：text / image / pdf / audio / none（不读、不存内容）。"""
    task = str(task or "")
    upload_id = request.get("upload_id")
    if upload_id:
        try:
            row = con.execute("SELECT media_type FROM ai_uploads WHERE id=?", (int(upload_id),)).fetchone()
        except Exception:
            row = None
        media = str(row["media_type"] if row else "").lower()
        if media.startswith("image/"):
            return "image"
        if media.startswith("audio/"):
            return "audio"
        if "pdf" in media:
            return "pdf"
        if media:
            return "text"
    doc_ids = request.get("document_ids") or ([request["document_id"]] if request.get("document_id") else [])
    if doc_ids:
        try:
            row = con.execute("SELECT storage_name FROM ai_documents WHERE id=?", (int(doc_ids[0]),)).fetchone()
        except Exception:
            row = None
        storage = str(row["storage_name"] if row else "").lower()
        return "pdf" if storage.endswith(".pdf") else "text"
    if task == "ocr":
        return "pdf"
    if task in ("paper-import", "paper-annotate"):
        return "image"
    if request.get("qid"):
        return "text"
    if task in (
        "explain", "grade", "question-analysis", "notes", "course", "course-narration",
        "course-video", "paper-enrich", "paper-compose", "paper-revision", "assistant-turn",
        "insight-report",
    ):
        return "text"
    return "none"


def _result_text(result: dict) -> str:
    for key in ("content", "markdown", "summary", "message", "outline"):
        value = result.get(key)
        if isinstance(value, str) and value.strip():
            return value
    reply = result.get("reply")
    if isinstance(reply, dict) and isinstance(reply.get("text"), str):
        return reply["text"]
    return ""


def _model_calls(con, job_id: int) -> tuple[int, int, int]:
    """从逐次调用账本 ``ai_usage`` 汇总该任务的 token（兼容老账本缺失）。"""
    try:
        row = con.execute(
            "SELECT COUNT(*), COALESCE(SUM(input_tokens),0), COALESCE(SUM(output_tokens),0) "
            "FROM ai_usage WHERE job_id=?",
            (int(job_id),),
        ).fetchone()
    except Exception:
        return 0, 0, 0
    if not row:
        return 0, 0, 0
    return int(row[0] or 0), int(row[1] or 0), int(row[2] or 0)


def estimate_cost_micro(con, *, user_id: int, provider_id, model: str,
                        tokens_in: int, tokens_out: int) -> int | None:
    """按「模型能力与单价」估算费用（微元，1e-6 元）。

    单价口径与既有设置页一致：元 / 百万 tokens。因此
    ``cost_micro = tokens_in × input_price + tokens_out × output_price``（tokens 数 × 每百万单价
    恰好等于微元）。任一单价缺失时返回 ``None``，绝不按 0 元编造费用。
    """
    try:
        if provider_id is not None:
            row = con.execute(
                "SELECT input_price, output_price FROM ai_models "
                "WHERE user_id=? AND provider_id=? AND model_id=? ORDER BY id LIMIT 1",
                (int(user_id), int(provider_id), str(model)),
            ).fetchone()
        else:
            row = con.execute(
                "SELECT input_price, output_price FROM ai_models "
                "WHERE user_id=? AND model_id=? ORDER BY id LIMIT 1",
                (int(user_id), str(model)),
            ).fetchone()
    except Exception:
        return None
    if not row:
        return None
    try:
        input_price, output_price = row["input_price"], row["output_price"]
    except (KeyError, IndexError, TypeError):
        return None
    if input_price is None or output_price is None:
        return None
    try:
        value = int(tokens_in) * float(input_price) + int(tokens_out) * float(output_price)
    except (TypeError, ValueError):
        return None
    return int(round(value))


def _meta_for(con, job_row, request: dict, result: dict, input_kind: str) -> dict:
    """只放分类、计数与版本信息：不放题干/资料内容，也不放可反查私密内容的标识。"""
    models = [{"provider_id": request.get("provider_id"), "model": request.get("model")}]
    if request.get("cross_verify") and request.get("verify_model"):
        models.append({"provider_id": request.get("verify_provider_id"), "model": request.get("verify_model")})
    if request.get("judge_model"):
        models.append({"provider_id": request.get("judge_provider_id"), "model": request.get("judge_model")})
    document_ids = request.get("document_ids") or (
        [request["document_id"]] if request.get("document_id") else []
    )
    meta = {
        "input_kind": input_kind,
        "models": models,
        "cross_verify": bool(request.get("cross_verify")),
        "structured_verify": bool(request.get("structured_verify")),
        "peer_review": bool(request.get("peer_review")),
        "source_mode": request.get("source_mode") or None,
        "explain_mode": request.get("explain_mode") or None,
        "modes_count": len(request.get("modes") or []),
        "document_count": len(document_ids or []),
        "requirements_present": bool(request.get("requirements")),
        "retries": int(job_row["retry_count"] or 0),
        "prompt_version": result.get("prompt_version") or None,
    }
    return {key: value for key, value in meta.items() if value not in (None, [], {}, False)}


def _build_usage(con, job_row) -> dict | None:
    """把一条终态 ``ai_jobs`` 行投影成 ``ai_job_usage`` 行；终态以外返回 None。"""
    status = str(job_row["status"] or "")
    if status not in TERMINAL_STATUSES:
        return None
    try:
        request = json.loads(job_row["request_json"] or "{}")
    except (TypeError, ValueError):
        request = {}
    if not isinstance(request, dict):
        request = {}
    try:
        result = json.loads(job_row["result_json"] or "{}")
    except (TypeError, ValueError):
        result = {}
    if not isinstance(result, dict):
        result = {}
    kind = str(job_row["task"] or request.get("task") or "")
    job_id = int(job_row["id"])
    user_id = int(job_row["user_id"])
    provider_id = request.get("provider_id")
    model = str(request.get("model") or "")
    call_count, tokens_in, tokens_out = _model_calls(con, job_id)
    tokens_estimate = 0
    if call_count == 0 and status == "completed":
        # 部分本地模型不回传 usage：只用「长度」做粗估，并显式标记为估算值。
        text = _result_text(result)
        if text:
            tokens_in = max(1, len(job_row["request_json"] or "") // 4)
            tokens_out = max(1, len(text) // 2)
            tokens_estimate = 1
    cost_micro = None
    if call_count or tokens_estimate:
        cost_micro = estimate_cost_micro(
            con, user_id=user_id, provider_id=provider_id, model=model,
            tokens_in=tokens_in, tokens_out=tokens_out,
        )
    input_kind = _classify_input(con, kind, request)
    return {
        "user_id": user_id,
        "job_id": job_id,
        "kind": kind,
        "provider_id": provider_id,
        "model": model,
        "status": status,
        "duration_ms": _duration_ms(job_row),
        "retries": int(job_row["retry_count"] or 0),
        "tokens_in": int(tokens_in),
        "tokens_out": int(tokens_out),
        "tokens_estimate": tokens_estimate,
        "cost_micro": cost_micro,
        "input_kind": input_kind,
        "meta": _meta_for(con, job_row, request, result, input_kind),
    }


def record_job_usage(job_id: int) -> bool:
    """任务终态入账（幂等：``job_id`` 唯一；重复调用只更新同一行）。

    返回 True 表示写入了账本；任务未到终态、任务不存在或账本异常时返回 False。
    任何异常都只记日志，不影响任务状态机。
    """
    try:
        _ensure_ready()
        created = False
        with db.connection() as con:
            job_row = con.execute("SELECT * FROM ai_jobs WHERE id=?", (int(job_id),)).fetchone()
            if job_row is None:
                return False
            payload = _build_usage(con, job_row)
            if payload is None:
                return False
            meta_json = json.dumps(payload.pop("meta"), ensure_ascii=False)
            now = db.utcnow()
            db.write_lock(con, "ai-usage-write")
            existing = con.execute(
                "SELECT id FROM ai_job_usage WHERE job_id=?", (int(job_id),)
            ).fetchone()
            if existing:
                con.execute(
                    "UPDATE ai_job_usage SET kind=?, provider_id=?, model=?, status=?, duration_ms=?, "
                    "retries=?, tokens_in=?, tokens_out=?, tokens_estimate=?, cost_micro=?, "
                    "meta_json=? WHERE job_id=?",
                    (
                        payload["kind"], payload["provider_id"], payload["model"], payload["status"],
                        payload["duration_ms"], payload["retries"], payload["tokens_in"],
                        payload["tokens_out"], payload["tokens_estimate"], payload["cost_micro"],
                        meta_json, payload["job_id"],
                    ),
                )
            else:
                con.execute(
                    "INSERT INTO ai_job_usage(user_id,job_id,kind,provider_id,model,status,duration_ms,"
                    "retries,tokens_in,tokens_out,tokens_estimate,cost_micro,meta_json,created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        payload["user_id"], payload["job_id"], payload["kind"], payload["provider_id"],
                        payload["model"], payload["status"], payload["duration_ms"], payload["retries"],
                        payload["tokens_in"], payload["tokens_out"], payload["tokens_estimate"],
                        payload["cost_micro"], meta_json, now,
                    ),
                )
                created = True
        if created:
            # 进程内活跃注册表只在「首次终态入账」时释放；幂等更新不重复扣减。
            _release_active(int(job_id))
        return True
    except Exception:
        log.warning("AI 任务 %s 用量入账失败（不影响任务状态）", job_id, exc_info=True)
        return False


def clear_job_usage(job_id: int) -> None:
    """续跑/重新执行前清掉旧终态账本：下一次终态写入时重建唯一行。"""
    try:
        _ensure_ready()
        with db.connection() as con:
            db.write_lock(con, "ai-usage-write")
            con.execute("DELETE FROM ai_job_usage WHERE job_id=?", (int(job_id),))
        _release_active(int(job_id))
    except Exception:
        log.warning("清理 AI 任务 %s 的旧用量账本失败（不影响任务）", job_id, exc_info=True)


def purge_old_usage(retention_days: int | None = None) -> int:
    """按保留策略删除过期账本行，返回删除行数（幂等；保留当前在途任务不涉及）。

    保留天数优先级：参数 → ``STUDY_AI_USAGE_RETENTION_DAYS`` → 默认 180 天；
    0/负数表示永久保留（不清理）。
    """
    if retention_days is None:
        raw = os.environ.get(RETENTION_ENV)
        if raw is None or not str(raw).strip():
            retention_days = DEFAULT_USAGE_RETENTION_DAYS
        else:
            try:
                retention_days = int(str(raw).strip())
            except (TypeError, ValueError):
                log.warning("环境变量 %s=%r 不是整数，使用默认值 %s", RETENTION_ENV, raw,
                            DEFAULT_USAGE_RETENTION_DAYS)
                retention_days = DEFAULT_USAGE_RETENTION_DAYS
    retention_days = int(retention_days)
    if retention_days <= 0:
        return 0
    cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat()
    try:
        _ensure_ready()
        with db.connection() as con:
            db.write_lock(con, "ai-usage-write")
            return int(con.execute("DELETE FROM ai_job_usage WHERE created_at < ?", (cutoff,)).rowcount)
    except Exception:
        log.warning("清理过期 AI 用量账本失败", exc_info=True)
        return 0


# ---------------------------------------------------------------------------
# 汇总与查询
# ---------------------------------------------------------------------------

def _percentile(values: list[int], fraction: float) -> int | None:
    if not values:
        return None
    ordered = sorted(int(value) for value in values)
    position = (len(ordered) - 1) * float(fraction)
    low = int(math.floor(position))
    high = int(math.ceil(position))
    if low == high:
        return ordered[low]
    return int(round(ordered[low] + (ordered[high] - ordered[low]) * (position - low)))


def _meta_of(row) -> dict:
    try:
        meta = json.loads(row["meta_json"] or "{}")
    except (TypeError, ValueError):
        return {}
    return meta if isinstance(meta, dict) else {}


def _public_usage(row) -> dict:
    meta = _meta_of(row)
    return {
        "job_id": int(row["job_id"]),
        "user_id": int(row["user_id"]),
        "kind": str(row["kind"] or ""),
        "status": str(row["status"] or ""),
        "provider_id": row["provider_id"],
        "model": str(row["model"] or ""),
        "duration_ms": int(row["duration_ms"] or 0),
        "retries": int(row["retries"] or 0),
        "tokens_in": int(row["tokens_in"] or 0),
        "tokens_out": int(row["tokens_out"] or 0),
        "tokens_estimate": bool(int(row["tokens_estimate"] or 0)),
        "cost_micro": None if row["cost_micro"] is None else int(row["cost_micro"]),
        "input_kind": str(meta.get("input_kind") or "none"),
        "prompt_version": meta.get("prompt_version"),
        "created_at": str(row["created_at"] or ""),
    }


def _aggregate(rows: list[dict]) -> dict:
    """按一组用量行聚合：总量、失败率、P50/P95 耗时、token 与费用。"""
    jobs = len(rows)
    by_status = {status: 0 for status in TERMINAL_STATUSES}
    durations: list[int] = []
    tokens_in = tokens_out = retries = 0
    cost_total = 0
    cost_covered = cost_unpriced = 0
    for row in rows:
        status = str(row.get("status") or "")
        if status in by_status:
            by_status[status] += 1
        duration = int(row.get("duration_ms") or 0)
        if duration:
            durations.append(duration)
        tokens_in += int(row.get("tokens_in") or 0)
        tokens_out += int(row.get("tokens_out") or 0)
        retries += int(row.get("retries") or 0)
        cost = row.get("cost_micro")
        if cost is None:
            cost_unpriced += 1
        else:
            cost_total += int(cost)
            cost_covered += 1
    failed = by_status["failed"] + by_status["interrupted"]
    return {
        "jobs": jobs,
        "succeeded": by_status["completed"],
        "failed": failed,
        "cancelled": by_status["cancelled"],
        "failure_rate": round(failed / jobs, 4) if jobs else 0.0,
        "duration_p50_ms": _percentile(durations, 0.5),
        "duration_p95_ms": _percentile(durations, 0.95),
        "duration_avg_ms": int(sum(durations) / len(durations)) if durations else None,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "retries": retries,
        "cost_micro": cost_total if cost_covered else None,
        "cost_covered_jobs": cost_covered,
        "cost_unpriced_jobs": cost_unpriced,
    }


def _group_aggregate(rows: list[dict], key_fn, label_key: str) -> list[dict]:
    """按任务类型（``kind``）或模型（``provider_id`` + ``model``）分组聚合。"""
    groups: dict = {}
    for row in rows:
        groups.setdefault(key_fn(row), []).append(row)
    items = []
    for key, group in groups.items():
        summary = _aggregate(group)
        if label_key == "kind":
            summary["kind"] = str(key)
        else:
            summary["provider_id"] = key[0]
            summary["model"] = str(key[1])
        items.append(summary)
    items.sort(key=lambda item: (-item["jobs"], str(item.get("kind") or item.get("model") or "")))
    return items


def _fetch_usage(con, *, user_id: int | None, kind: str | None, since: str) -> tuple[list[dict], bool]:
    where = ["created_at>=?"]
    params: list = [since]
    if user_id is not None:
        where.append("user_id=?")
        params.append(int(user_id))
    if kind:
        where.append("kind=?")
        params.append(str(kind))
    rows = con.execute(
        "SELECT * FROM ai_job_usage WHERE " + " AND ".join(where) + " ORDER BY id DESC LIMIT ?",
        (*params, MAX_QUERY_ROWS + 1),
    ).fetchall()
    truncated = len(rows) > MAX_QUERY_ROWS
    return [dict(row) for row in rows[:MAX_QUERY_ROWS]], truncated


def _window_days(days: int, *, maximum: int = 365) -> int:
    return max(1, min(int(days or 1), maximum))


def _period_start(days: int) -> str:
    start = datetime.now(timezone.utc) - timedelta(days=days)
    return start.isoformat()


def _today_bounds() -> tuple[str, str]:
    now = datetime.now(timezone.utc)
    start = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
    return start.isoformat(), (start + timedelta(days=1)).isoformat()


def _quota_entry(con, kind: str, *, user_id: int | None) -> dict:
    daily_limit, daily_source = effective_daily_quota(kind, con)
    concurrency_limit, concurrency_source = effective_concurrency(kind, con)
    if user_id is None:
        consumed = _count_consumed(con, user_id=None, kind=kind, since=_utc_day_start())
    else:
        consumed = daily_used(con, int(user_id), kind)
    running = _count_active_jobs(con, kind)
    remaining = None
    if daily_limit is not None and daily_limit > 0:
        remaining = max(0, daily_limit - consumed)
    return {
        "kind": kind,
        "label": kind_label(kind),
        "daily_limit": daily_limit,
        "daily_source": daily_source,
        "concurrency_limit": concurrency_limit,
        "concurrency_source": concurrency_source,
        "used_today": consumed,
        "running": running,
        "remaining_today": remaining,
        "limit_reached": bool(daily_limit and daily_limit > 0 and consumed >= daily_limit),
    }


def _quota_entries(con, *, user_id: int | None, kinds: list[str] | None = None) -> list[dict]:
    return [_quota_entry(con, kind, user_id=user_id) for kind in (kinds or known_kinds())]


def _retention_days() -> int:
    raw = os.environ.get(RETENTION_ENV)
    if raw is None or not str(raw).strip():
        return DEFAULT_USAGE_RETENTION_DAYS
    try:
        return max(0, int(str(raw).strip()))
    except (TypeError, ValueError):
        return DEFAULT_USAGE_RETENTION_DAYS


# ---------------------------------------------------------------------------
# 查询接口：本人用量 / 管理台用量 / 配额
# ---------------------------------------------------------------------------

@router.get("/ai/usage/me")
def my_usage(days: int = Query(14, ge=1, le=365), user=Depends(current_user)):
    """本人 AI 用量：按天 / 按任务类型汇总 + 今日配额 + 最近任务。

    只统计本人账本，不返回任何全站数据；普通账号看不到他人用量。
    """
    _ensure_ready()
    window = _window_days(days)
    since = _period_start(window)
    today_start, _tomorrow_start = _today_bounds()
    with db.connection() as con:
        rows, truncated = _fetch_usage(con, user_id=int(user["id"]), kind=None, since=since)
        today_rows = [row for row in rows if str(row["created_at"] or "") >= today_start]
        quota = _quota_entries(con, user_id=int(user["id"]))
        recent = rows[:ME_RECENT_LIMIT]
    today = _aggregate(today_rows)
    today["date"] = today_start[:10]
    by_day_groups: dict[str, list[dict]] = {}
    for row in rows:
        day = str(row["created_at"] or "")[:10]
        by_day_groups.setdefault(day, []).append(row)
    by_day = []
    for day in sorted(by_day_groups, reverse=True):
        summary = _aggregate(by_day_groups[day])
        summary["day"] = day
        by_day.append(summary)
    return {
        "days": window,
        "generated_at": db.utcnow(),
        "today": today,
        "by_day": by_day,
        "by_kind": _group_aggregate(rows, lambda row: str(row["kind"] or ""), "kind"),
        "quota": {
            "date": today_start[:10],
            "timezone": "UTC",
            "kinds": quota,
            "notice": "每日配额按 UTC 自然日统计，失败/取消的任务不消耗配额；只记录任务类型、模型、"
                      "token 与耗时，不记录题目和资料内容。",
        },
        "recent": [_public_usage(row) for row in recent],
        "truncated": truncated,
        "notice": "费用为估算：按模型设置里的单价（元/百万 tokens）计算，只展示、不扣减。",
    }


@router.get("/admin/ai/usage")
def admin_usage(
    days: int = Query(30, ge=1, le=365),
    kind: str | None = Query(None, max_length=40),
    user_id: int | None = Query(None, ge=1),
    page: int = Query(1, ge=1, le=1000),
    user=Depends(require_admin),
):
    """全站 AI 用量汇总（管理员）：总量、失败率、P50/P95 耗时、token/费用与按类型/模型分布。"""
    _ensure_ready()
    window = _window_days(days)
    since = _period_start(window)
    with db.connection() as con:
        rows, truncated = _fetch_usage(con, user_id=user_id, kind=kind, since=since)
    totals = _aggregate(rows)
    offset = (page - 1) * ADMIN_PAGE_SIZE
    page_rows = rows[offset:offset + ADMIN_PAGE_SIZE]
    by_day_groups: dict[str, list[dict]] = {}
    for row in rows:
        by_day_groups.setdefault(str(row["created_at"] or "")[:10], []).append(row)
    by_day = []
    for day in sorted(by_day_groups, reverse=True):
        summary = _aggregate(by_day_groups[day])
        summary["day"] = day
        by_day.append(summary)
    return {
        "days": window,
        "generated_at": db.utcnow(),
        "filters": {"kind": kind, "user_id": user_id, "page": page, "page_size": ADMIN_PAGE_SIZE},
        "totals": totals,
        "by_kind": _group_aggregate(rows, lambda row: str(row["kind"] or ""), "kind"),
        "by_model": _group_aggregate(
            rows,
            lambda row: (row.get("provider_id"), str(row.get("model") or "")),
            "model",
        ),
        "by_day": by_day,
        "page": {
            "page": page,
            "page_size": ADMIN_PAGE_SIZE,
            "total": len(rows),
            "has_more": offset + ADMIN_PAGE_SIZE < len(rows),
            "items": [_public_usage(row) for row in page_rows],
        },
        "truncated": truncated or len(rows) >= MAX_QUERY_ROWS,
        "page_size_limit": MAX_QUERY_ROWS,
        "notice": "费用为按模型单价估算的微元累计；未配置单价的模型单独计数，不按 0 元计入。",
    }


@router.get("/admin/ai/quotas")
def admin_quotas(user=Depends(require_admin)):
    """配额管理视图：每个任务类型的有效配额、来源、今日全站消耗与当前并发。"""
    _ensure_ready()
    with db.connection() as con:
        overrides = _quota_overrides(con)
        entries = _quota_entries(con, user_id=None)
    defaults = {
        "daily": dict(DEFAULT_DAILY_QUOTAS),
        "concurrency": dict(DEFAULT_CONCURRENCY),
        "retention_days": _retention_days(),
        "daily_env_prefix": QUOTA_ENV_PREFIX,
        "concurrency_env_prefix": CONCURRENCY_ENV_PREFIX,
    }
    return {
        "generated_at": db.utcnow(),
        "date": _utc_day_start()[:10],
        "timezone": "UTC",
        "defaults": defaults,
        "overrides": [
            {
                "kind": str(entry["kind"]),
                "daily_limit": entry.get("daily_limit"),
                "concurrency_limit": entry.get("concurrency_limit"),
                "updated_by": entry.get("updated_by"),
                "updated_at": entry.get("updated_at"),
            }
            for entry in overrides.values()
        ],
        "items": entries,
        "notice": "每日配额按 UTC 自然日统计，失败/取消的任务不消耗配额；"
                  "daily_limit / concurrency_limit 为 0 表示不限制。",
    }


class QuotaUpdate(BaseModel):
    kind: str = Field(min_length=1, max_length=40)
    daily_limit: int | None = Field(default=None, ge=0, le=100000)
    concurrency_limit: int | None = Field(default=None, ge=0, le=1000)
    reset: bool = False


@router.post("/admin/ai/quotas")
def update_admin_quota(data: QuotaUpdate, user=Depends(require_admin)):
    """调整某任务类型的每人每日配额 / 全站并发；``reset=true`` 恢复内置默认。"""
    _ensure_ready()
    if data.kind not in known_kinds():
        raise HTTPException(422, {"code": "unknown_task_kind", "message": f"未知任务类型：{data.kind}"})
    if data.reset:
        with db.connection() as con:
            db.write_lock(con, "ai-quota-write")
            con.execute("DELETE FROM ai_quotas WHERE kind=?", (data.kind,))
    else:
        if data.daily_limit is None and data.concurrency_limit is None:
            raise HTTPException(422, "请至少提供 daily_limit 或 concurrency_limit，或使用 reset 恢复默认")
        existing = _quota_overrides().get(data.kind) or {}
        daily_limit = data.daily_limit if data.daily_limit is not None else existing.get("daily_limit")
        concurrency_limit = (
            data.concurrency_limit if data.concurrency_limit is not None
            else existing.get("concurrency_limit")
        )
        with db.connection() as con:
            db.write_lock(con, "ai-quota-write")
            con.execute(
                "INSERT INTO ai_quotas(kind,daily_limit,concurrency_limit,updated_by,updated_at) "
                "VALUES (?,?,?,?,?) ON CONFLICT(kind) DO UPDATE SET "
                "daily_limit=excluded.daily_limit, concurrency_limit=excluded.concurrency_limit, "
                "updated_by=excluded.updated_by, updated_at=excluded.updated_at",
                (data.kind, daily_limit, concurrency_limit, int(user["id"]), db.utcnow()),
            )
    with db.connection() as con:
        entry = _quota_entry(con, data.kind, user_id=None)
    return {
        "kind": data.kind,
        "reset": bool(data.reset),
        "daily_limit": entry["daily_limit"],
        "daily_source": entry["daily_source"],
        "concurrency_limit": entry["concurrency_limit"],
        "concurrency_source": entry["concurrency_source"],
        "item": entry,
    }


__all__ = [
    "router", "init_usage", "record_job_usage", "clear_job_usage", "purge_old_usage",
    "check_new_job", "check_resume", "current_concurrency", "daily_used",
    "effective_daily_quota", "effective_concurrency", "known_kinds", "kind_label",
    "estimate_cost_micro", "reset_runtime_state", "note_job_active",
    "TERMINAL_STATUSES", "INPUT_KINDS", "DEFAULT_DAILY_QUOTAS", "DEFAULT_CONCURRENCY",
]
