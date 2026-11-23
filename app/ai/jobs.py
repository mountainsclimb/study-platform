"""AI Job queue, SSE streaming hub, cancellation, checkpoint resumption & persisted explanations."""
import asyncio
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
import json
import logging
import os
import time
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, field_validator

from .. import db
from ..auth import current_user, require_admin
from . import usage as ai_usage
from .notes_agent import TEMPLATES, _notes_job
from .providers import _identity, _json, _owned, _selected_provider
from .upload_pipeline import _recognize_job, _tagging_job
from .verify import _explain_job, _grade_job, load_explain_modes

router = APIRouter(prefix="/api", tags=["AI jobs & streaming"])
log = logging.getLogger(__name__)

# 兼容旧引用：实际每人配额由 STUDY_AI_MAX_ACTIVE_PER_USER 在运行时读取（见 max_active_per_user）。
MAX_ACTIVE = 2
DEFAULT_MAX_ACTIVE_PER_USER = 2
DEFAULT_GLOBAL_CONCURRENCY = 8
DAILY_JOB_LIMIT = 100
QUEUE_NAME = "zhixu:ai"
# 任务租约：执行进程定期写 heartbeat_at，超过租约仍无心跳视为 Worker 已宕机。
JOB_LEASE_SECONDS = int(os.environ.get("AI_JOB_LEASE_SECONDS", "90") or 90)
JOB_HEARTBEAT_SECONDS = int(os.environ.get("AI_JOB_HEARTBEAT_SECONDS", "10") or 10)
# 真跨进程流式：Redis Pub/Sub 频道 `zhixu:job:{job_id}`，Worker 每产生一个增量就发布一次。
JOB_CHANNEL_PREFIX = "zhixu:job:"
STREAM_KEEPALIVE_SECONDS = 15.0
STREAM_POLL_SECONDS = 0.75
STREAM_PUBSUB_POLL_SECONDS = 1.0
STREAM_CONTROL_EVENTS = frozenset({"snapshot", "stage", "done"})
REDIS_PUBLISH_TIMEOUT_SECONDS = 1.0
REDIS_SOCKET_TIMEOUT_SECONDS = 3.0
REDIS_PUBLISH_RETRY_SECONDS = 5.0

# ---------------------------------------------------------------------------
# 任务类型注册表：核心任务（讲解/评分/笔记/OCR/识别/打标）在下方固定分支；
# 后续整合的功能模块（AI 课堂、试卷工坊、AI 助手、学情报告……）通过
# ``register_task_handler()`` 注册自己的 JobInput 准入校验与执行器，
# 不必改动本文件的派发主流程。校验失败抛 HTTPException，错误会直接回给用户。
# ---------------------------------------------------------------------------
STANDARD_TASKS = frozenset({"explain", "grade", "question-analysis", "notes", "recognize", "tagging", "ocr"})
EXTRA_TASKS: dict[str, dict] = {}


def register_task_handler(name: str, handler, validate=None, *, label: str = "") -> None:
    """注册一个扩展任务类型。

    - ``handler(job_id, user_id, data, provider, verifier)``：async 执行器，返回结果 dict；
      可通过 ``publish_event`` 推送阶段与增量，通过 ``is_job_cancelled`` 响应取消。
    - ``validate(data, user)``：同步准入校验，负责所有权与参数检查，失败抛 HTTPException。
    重复注册同一任务名会覆盖旧处理器（便于测试替换），但不会被普通业务代码调用多次。
    """
    if not name or name in STANDARD_TASKS:
        raise ValueError("扩展任务名不能为空且不能与内置任务重名：%r" % (name,))
    if not callable(handler):
        raise ValueError("任务执行器必须可调用")
    EXTRA_TASKS[name] = {"handler": handler, "validate": validate, "label": label or name}


def extra_task(name: str) -> dict | None:
    return EXTRA_TASKS.get(name)


_tasks: set[asyncio.Task] = set()
_running_by_id: dict[int, asyncio.Task] = {}
_global_sem: asyncio.Semaphore | None = None
_global_sem_limit = 0
_dispatch_token: ContextVar[str | None] = ContextVar("dispatch_token", default=None)
TERMINAL_STATES = ("completed", "failed", "interrupted", "cancelled", "awaiting_outline")

# In-memory streaming state per active job
_stream_buffers: dict[int, list[str]] = {}
_stream_queues: dict[int, list[asyncio.Queue]] = {}

# Redis Pub/Sub 发布端：同步客户端复用 + 失败熔断（发布失败只告警，不影响任务）。
_sync_redis_client = None
_sync_redis_disabled_until = 0.0
_redis_warned: set[str] = set()
# arq 连接池模块级复用（入队与 recover_queued_jobs 轮询共用，不再每次新建）。
_arq_pool = None
_arq_pool_loop = None
_arq_pool_lock = None
_arq_pool_lock_loop = None


class JobUnavailableError(RuntimeError):
    """任务执行器或依赖暂不可用：错误信息可直接展示给用户。"""


def _env_positive_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        return max(1, int(str(raw).strip()))
    except (TypeError, ValueError):
        log.warning("环境变量 %s=%r 不是正整数，使用默认值 %s", name, raw, default)
        return default


def max_active_per_user() -> int:
    """每人同时可占用的任务数（含排队中、运行中与取消请求中的任务）。"""
    return _env_positive_int("STUDY_AI_MAX_ACTIVE_PER_USER", DEFAULT_MAX_ACTIVE_PER_USER)


def global_concurrency() -> int:
    """全站同时运行任务上限，同时是 _global_sem 的信号量容量。"""
    return _env_positive_int("STUDY_AI_GLOBAL_CONCURRENCY", DEFAULT_GLOBAL_CONCURRENCY)


def _global_semaphore() -> asyncio.Semaphore:
    global _global_sem, _global_sem_limit
    limit = global_concurrency()
    if _global_sem is None or _global_sem_limit != limit:
        _global_sem = asyncio.Semaphore(limit)
        _global_sem_limit = limit
    return _global_sem


def _warn_redis_once(key: str, message: str, *, exc_info: bool = False) -> None:
    if key in _redis_warned:
        return
    _redis_warned.add(key)
    log.warning(message, exc_info=exc_info)


def _job_channel(job_id: int) -> str:
    return f"{JOB_CHANNEL_PREFIX}{job_id}"


def _load_aioredis():
    """按需导入 redis.asyncio；缺包时返回 None，调用方回退数据库轮询。"""
    try:
        import redis.asyncio as aioredis
    except Exception:
        _warn_redis_once("aioredis", "redis.asyncio 不可用（缺少 redis 依赖），任务流 SSE 回退为 0.75s 数据库轮询")
        return None
    return aioredis


def _sync_redis():
    global _sync_redis_client
    if _sync_redis_client is None:
        import redis
        _sync_redis_client = redis.Redis.from_url(
            redis_url(),
            socket_connect_timeout=REDIS_PUBLISH_TIMEOUT_SECONDS,
            socket_timeout=REDIS_PUBLISH_TIMEOUT_SECONDS,
        )
    return _sync_redis_client


def close_publish_client() -> None:
    """关闭发布用同步 Redis 客户端（进程退出/测试收尾可选调用）。"""
    global _sync_redis_client
    client, _sync_redis_client = _sync_redis_client, None
    if client is not None:
        try:
            client.close()
        except Exception:
            pass


def _publish_to_redis(job_id: int, payload: dict) -> None:
    """把事件发布到 Pub/Sub；失败只告警并熔断 5s，绝不向调用方抛错。"""
    global _sync_redis_disabled_until
    if not redis_url():
        return
    now = time.monotonic()
    if _sync_redis_disabled_until > now:
        return
    try:
        client = _sync_redis()
        client.publish(_job_channel(job_id), json.dumps(payload, ensure_ascii=False))
    except Exception:
        _sync_redis_disabled_until = time.monotonic() + REDIS_PUBLISH_RETRY_SECONDS
        close_publish_client()
        _warn_redis_once(
            "publish",
            "Redis Pub/Sub 发布失败，任务流增量暂时只写本进程队列（稍后自动重试）",
            exc_info=True,
        )


def _queue_message(job_id: int, message: dict) -> None:
    queues = _stream_queues.get(job_id)
    if not queues:
        return
    control = message["event"] in STREAM_CONTROL_EVENTS
    for q in list(queues):
        try:
            q.put_nowait(message)
            continue
        except asyncio.QueueFull:
            pass
        if not control:
            continue
        # 控制事件（snapshot/stage/done）永不因队列满而丢失：丢弃最旧的一条增量腾位置。
        try:
            q.get_nowait()
        except asyncio.QueueEmpty:
            continue
        try:
            q.put_nowait(message)
        except asyncio.QueueFull:
            continue


def publish_event(job_id: int, event: str, data: dict) -> None:
    """发布一条任务流事件（`snapshot|stage|delta|thinking|done`）。

    - 始终写入本进程 SSE 队列（同进程 Worker 的增量）；
    - 配置 REDIS_URL 时同时发布到 `zhixu:job:{job_id}`，供跨进程 SSE 订阅；
    - Redis 发布失败只告警，不影响任务执行。
    """
    message = {"event": event, "data": data}
    if event == "delta" and data.get("text"):
        _stream_buffers.setdefault(job_id, []).append(data["text"])
    _queue_message(job_id, message)
    if redis_url():
        _publish_to_redis(job_id, {"event": event, "data": data, "ts": time.time()})


def _emit_stream(job_id: int, event_type: str, data: dict):
    """兼容旧调用点：统一走 publish_event（本地队列 + Redis Pub/Sub 双写）。"""
    publish_event(job_id, event_type, data)


def _flush_partial(job_id: int):
    buf = _stream_buffers.get(job_id)
    if not buf:
        return
    partial = "".join(buf)
    try:
        with db.connection() as con:
            where, params = _current_run()
            con.execute(
                "UPDATE ai_jobs SET partial_content=?, updated_at=? WHERE id=?" + where,
                (partial, db.utcnow(), job_id, *params),
            )
    except Exception:
        pass


def is_job_cancelled(job_id: int) -> bool:
    with db.connection() as con:
        row = con.execute("SELECT cancelled, status, dispatch_token FROM ai_jobs WHERE id=?", (job_id,)).fetchone()
    token = _dispatch_token.get()
    return row is None or bool(row["cancelled"]) or row["status"] == "cancelled" or (
        token is not None and token != row["dispatch_token"]
    )


def _current_run():
    token = _dispatch_token.get()
    return (" AND dispatch_token=? AND cancelled=0", (token,)) if token is not None else ("", ())


def _job_set_checkpoint(job_id: int, checkpoint: dict):
    with db.connection() as con:
        where, params = _current_run()
        con.execute(
            "UPDATE ai_jobs SET checkpoint_json=?, updated_at=? WHERE id=?" + where,
            (_json(checkpoint), db.utcnow(), job_id, *params),
        )


def _job_get_checkpoint(job_id: int) -> dict | None:
    with db.connection() as con:
        row = con.execute("SELECT checkpoint_json FROM ai_jobs WHERE id=?", (job_id,)).fetchone()
    if not row or not row["checkpoint_json"]:
        return None
    try:
        return json.loads(row["checkpoint_json"])
    except Exception:
        return None


def redis_url() -> str:
    return os.environ.get("REDIS_URL", "").strip()


def _lease_cutoff(lease_seconds=None) -> str:
    seconds = JOB_LEASE_SECONDS if lease_seconds is None else max(1, int(lease_seconds))
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()


def touch_job_heartbeat(job_id: int, token: str | None = None) -> bool:
    """续写任务租约心跳；token 已被新派发替换时不再写入，避免旧执行进程「复活」。"""
    where, params = (" AND dispatch_token=? AND cancelled=0", (token,)) if token else ("", ())
    with db.connection() as con:
        changed = con.execute(
            "UPDATE ai_jobs SET heartbeat_at=? WHERE id=?" + where,
            (db.utcnow(), job_id, *params),
        )
    return bool(changed.rowcount)


async def _heartbeat_loop(job_id: int, token: str):
    interval = max(2, JOB_HEARTBEAT_SECONDS)
    while True:
        await asyncio.sleep(interval)
        try:
            touch_job_heartbeat(job_id, token)
        except Exception:
            log.warning("Could not refresh AI job %s lease", job_id, exc_info=True)


def reap_stale_jobs(*, lease_seconds=None, redis_mode=None) -> dict:
    """回收心跳超期的任务，避免账号被失联 Worker 的旧任务占满 MAX_ACTIVE。

    - running / cancel_requested 且 heartbeat 超期（或从未写过心跳）→ interrupted，可续跑；
    - 本地单进程模式没有存活 Worker 来消费 queued 任务 → interrupted；
    - Redis 模式保留已提交派发（dispatch_token 非空）的 queued 任务，交给
      recover_queued_jobs 重新入队，不再因为「跳过带 token 的任务」而永久锁死。
    """
    cutoff = _lease_cutoff(lease_seconds)
    now = db.utcnow()
    redis_mode = bool(redis_url()) if redis_mode is None else bool(redis_mode)
    message = "执行进程心跳超时，任务已中断，可点击续跑或重新发起"
    stale_ids: list[int] = []
    queued_ids: list[int] = []
    with db.connection() as con:
        db.write_lock(con, "job-reap")
        stale_ids = [
            row[0] for row in con.execute(
                "SELECT id FROM ai_jobs WHERE status IN ('running','cancel_requested') "
                "AND (heartbeat_at IS NULL OR heartbeat_at < ?)",
                (cutoff,),
            ).fetchall()
        ]
        running = con.execute(
            "UPDATE ai_jobs SET status='interrupted',stage='interrupted',error=?,updated_at=?,finished_at=? "
            "WHERE status IN ('running','cancel_requested') AND (heartbeat_at IS NULL OR heartbeat_at < ?)",
            (message, now, now, cutoff),
        ).rowcount
        queued = 0
        if redis_mode:
            # 从未写入 dispatch_token 的排队任务不会有 Worker 认领，只能标记中断。
            queued_ids = [
                row[0] for row in con.execute(
                    "SELECT id FROM ai_jobs WHERE status='queued' AND dispatch_token IS NULL"
                ).fetchall()
            ]
            queued = con.execute(
                "UPDATE ai_jobs SET status='interrupted',stage='interrupted',error=?,updated_at=?,finished_at=? "
                "WHERE status='queued' AND dispatch_token IS NULL",
                (message, now, now),
            ).rowcount
        else:
            queued_ids = [
                row[0] for row in con.execute("SELECT id FROM ai_jobs WHERE status='queued'").fetchall()
            ]
            queued = con.execute(
                "UPDATE ai_jobs SET status='interrupted',stage='interrupted',error=?,updated_at=?,finished_at=? "
                "WHERE status='queued'",
                (message, now, now),
            ).rowcount
    # 回收的中断任务同样入账（可观测），失败/中断不消耗配额。
    for job_id in (*stale_ids, *queued_ids):
        ai_usage.record_job_usage(job_id)
    return {"running": running, "queued": queued}


def redis_settings():
    from arq.connections import RedisSettings
    settings = RedisSettings.from_dsn(redis_url())
    settings.conn_timeout = 3
    settings.conn_retries = 0
    return settings


async def _get_arq_pool():
    """复用模块级 arq 连接池：入队与 recover_queued_jobs 轮询不再每次新建/关闭连接池。"""
    global _arq_pool, _arq_pool_loop, _arq_pool_lock, _arq_pool_lock_loop
    from arq import create_pool
    loop = asyncio.get_running_loop()
    if _arq_pool_lock is None or _arq_pool_lock_loop is not loop:
        _arq_pool_lock = asyncio.Lock()
        _arq_pool_lock_loop = loop
    async with _arq_pool_lock:
        if _arq_pool is not None and _arq_pool_loop is not loop:
            # 事件循环已切换（多轮 asyncio.run / 重启）：旧池绑在旧循环上，直接丢弃重建。
            _arq_pool = None
        if _arq_pool is None:
            _arq_pool = await create_pool(redis_settings())
            _arq_pool_loop = loop
        return _arq_pool


async def close_arq_pool() -> None:
    """关闭模块级 arq 连接池（应用关停/测试收尾可选调用）。"""
    global _arq_pool, _arq_pool_loop
    pool, _arq_pool, _arq_pool_loop = _arq_pool, None, None
    if pool is not None:
        try:
            await pool.aclose()
        except Exception:
            log.warning("关闭 arq 连接池失败", exc_info=True)


async def close_redis_connections() -> None:
    """关闭本模块持有的 Redis 资源（发布客户端 + arq 连接池）。"""
    close_publish_client()
    await close_arq_pool()


async def _enqueue_jobs(jobs):
    pool = await _get_arq_pool()
    try:
        for job_id, token in jobs:
            await asyncio.wait_for(
                pool.enqueue_job(
                    "run_ai_job_task", job_id, token, _job_id=f"zhixu:{token}", _queue_name=QUEUE_NAME,
                ),
                timeout=5,
            )
    except Exception:
        # 连接池可能已失效：丢弃缓存，下次入队重建；异常交由调用方决定降级行为。
        await close_arq_pool()
        raise


async def recover_queued_jobs():
    """Replay committed dispatches if an API process exits before enqueueing."""
    while True:
        try:
            reap_stale_jobs()
            with db.connection() as con:
                queued = con.execute(
                    "SELECT id, dispatch_token FROM ai_jobs WHERE status='queued' AND dispatch_token IS NOT NULL"
                ).fetchall()
            if queued:
                await _enqueue_jobs((row[0], row[1]) for row in queued)
        except Exception:
            log.warning("AI queue recovery failed. Redis dispatch will retry.", exc_info=True)
        await asyncio.sleep(5)


async def _dispatch(job_id, token, runner):
    if not redis_url():
        _spawn_job_task(job_id, token, runner)
        return
    try:
        await _enqueue_jobs([(job_id, token)])
    except Exception:
        log.warning("Could not enqueue AI job %s", job_id, exc_info=True)
        with db.connection() as con:
            changed = con.execute(
                "UPDATE ai_jobs SET status='failed',stage='dispatch',error=?,updated_at=? "
                "WHERE id=? AND dispatch_token=? AND status='queued'",
                ("任务队列暂不可用，请在任务列表重试", db.utcnow(), job_id, token),
            ).rowcount
        if changed:
            ai_usage.record_job_usage(job_id)
            raise HTTPException(503, "任务队列暂不可用，请在任务列表重试") from None


async def execute_dispatched_job(job_id, token, runner, *, retry=False, monitor=False):
    with db.connection() as con:
        if retry:
            # A worker died after claiming the job. Resuming explicitly avoids silently duplicating paid work.
            con.execute(
                "UPDATE ai_jobs SET status='interrupted',stage='interrupted',error=?,updated_at=? "
                "WHERE id=? AND dispatch_token=? AND status='running'",
                ("执行进程中断，请从检查点续跑", db.utcnow(), job_id, token),
            )
        changed = con.execute(
            "UPDATE ai_jobs SET status='running',stage='preparing',heartbeat_at=?,started_at=COALESCE(started_at,?),"
            "updated_at=? WHERE id=? AND dispatch_token=? AND cancelled=0 AND status='queued'",
            (db.utcnow(), db.utcnow(), db.utcnow(), job_id, token),
        ).rowcount
        if not changed:
            return
        row = con.execute("SELECT user_id, request_json FROM ai_jobs WHERE id=?", (job_id,)).fetchone()
    data = JobInput.model_validate_json(row["request_json"])
    context = _dispatch_token.set(token)
    watcher = None
    heartbeat = asyncio.create_task(_heartbeat_loop(job_id, token))
    try:
        if monitor:
            running = asyncio.current_task()

            async def watch_cancel():
                while True:
                    await asyncio.sleep(1)
                    try:
                        cancelled = is_job_cancelled(job_id)
                    except Exception:
                        log.warning("Could not check AI job %s cancellation", job_id, exc_info=True)
                        continue
                    if cancelled:
                        running.cancel()
                        return

            watcher = asyncio.create_task(watch_cancel())
        await runner(job_id, row["user_id"], data)
    except asyncio.CancelledError:
        if not is_job_cancelled(job_id):
            raise
    finally:
        heartbeat.cancel()
        await asyncio.gather(heartbeat, return_exceptions=True)
        if watcher is not None:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)
        _dispatch_token.reset(context)


def _job_public(row):
    data = dict(row)
    data.pop("dispatch_token", None)
    data["request"] = json.loads(data.pop("request_json"))
    raw_result = data.pop("result_json")
    data["result"] = json.loads(raw_result) if raw_result else None
    raw_cp = data.pop("checkpoint_json", None)
    data["has_checkpoint"] = bool(raw_cp)
    # 需处理：失败/中断但仍有检查点，用户可续跑或重新发起。
    data["needs_attention"] = data["status"] in ("failed", "interrupted") and data["has_checkpoint"]
    return data


def _job_update(job_id, status, stage, result=None, error=None):
    where, params = _current_run()
    now = db.utcnow()
    with db.connection() as con:
        changed = con.execute(
            "UPDATE ai_jobs SET status=?,stage=?,result_json=COALESCE(?,result_json),error=?,updated_at=?,"
            "started_at=COALESCE(started_at,?),"
            "finished_at=CASE WHEN ? IN ('completed','failed','interrupted','cancelled') THEN ? ELSE finished_at END "
            "WHERE id=?" + where,
            (
                status, stage, _json(result) if result is not None else None, error, now,
                now if status == "running" else None,
                status, now, job_id, *params,
            ),
        )
    if changed.rowcount:
        _emit_stream(job_id, "stage", {"status": status, "stage": stage, "error": error})
        if status in ai_usage.TERMINAL_STATUSES:
            # 终态统一入账（幂等；失败只记日志，不影响任务状态机与 SSE）。
            ai_usage.record_job_usage(job_id)


def update_job(job_id: int, status: str, stage: str = "") -> None:
    """公共包装：更新任务状态/阶段并推送 `stage` 事件（供 notes_agent 的 OCR 执行器等复用）。

    终态（completed/failed/interrupted/cancelled）会同时写入 finished_at；stage 为空时回退用 status。
    """
    _job_update(job_id, status, stage or status)


def _finalize_cancel(job_id, message="用户已取消任务"):
    """把取消请求落定到「已取消」，并记录结束时间。"""
    now = db.utcnow()
    with db.connection() as con:
        changed = con.execute(
            "UPDATE ai_jobs SET status='cancelled',stage='cancelled',cancelled=1,error=?,updated_at=?,finished_at=? "
            "WHERE id=? AND status IN ('queued','running','cancel_requested')",
            (message, now, now, job_id),
        ).rowcount
    if changed:
        _emit_stream(job_id, "stage", {"status": "cancelled", "stage": "cancelled", "error": message})
        ai_usage.record_job_usage(job_id)
    return bool(changed)


class JobInput(BaseModel):
    # 任务名形态 + 已知任务校验：内置任务与扩展注册表在解析阶段就拦住未知任务，
    # 避免任意字符串进入任务表；扩展任务在 init_<module>() 注册后自动有效。
    task: str = Field(pattern="^[a-z][a-z0-9-]{1,39}$")

    @field_validator("task")
    @classmethod
    def _known_task(cls, value: str) -> str:
        if value not in STANDARD_TASKS and value not in EXTRA_TASKS:
            raise ValueError("未知任务类型")
        return value

    qid: str | None = Field(default=None, max_length=300)
    attempt_id: int | None = None
    document_id: int | None = None
    document_ids: list[int] | None = None
    # PDF OCR 异步任务：页码范围（page_to 为空时由执行器按总页数/单批上限截断）。
    page_from: int = Field(default=1, ge=1)
    page_to: int | None = Field(default=None, ge=1)
    upload_id: int | None = None
    provider_id: int
    model: str = Field(min_length=1, max_length=160)
    verify_provider_id: int | None = None
    verify_model: str | None = Field(default=None, max_length=160)
    judge_provider_id: int | None = None
    judge_model: str | None = Field(default=None, max_length=160)
    cross_verify: bool = False
    structured_verify: bool = False
    peer_review: bool = False
    explain_mode: str | None = None
    modes: list[str] | None = None
    template: str = "checklist"
    prompt_config: dict | None = None
    source_mode: str = Field(default="user_docs", pattern="^(user_docs|official_plus_docs|wrongbook)$")
    kp_ids: list[int] | None = None
    subject: str | None = Field(default=None, max_length=40)
    confirm_outline: bool = False
    map_extract: bool = False
    tagging_limit: int = Field(default=50, ge=1, le=1000)
    requirements: str = Field(default="", max_length=4000)
    # 扩展任务参数：由注册的任务校验器解释（例如 AI 课堂的 scene 类型、试卷工坊的操作目标）。
    # 只允许 JSON 对象并限制体积，避免把大文件或二进制塞进任务请求。
    params: dict | None = None


class OutlineConfirmInput(BaseModel):
    outline: str = Field(min_length=1, max_length=30000)
    sections: list[str] = Field(default_factory=list, max_length=20)


@router.get("/ai/explain-modes")
def get_explain_modes(user=Depends(current_user)):
    spec = load_explain_modes()
    return {
        "version": spec["version"],
        "items": [{"id": k, "name": v["name"], "short": v["short"]} for k, v in spec["modes"].items()],
    }


@router.get("/explanations")
def list_explanations(
    qid: str | None = None,
    upload_id: int | None = None,
    limit: int = Query(20, ge=1, le=50),
    user=Depends(current_user),
):
    if bool(qid) == bool(upload_id):
        raise HTTPException(422, "请选择题库题目或上传题目")
    community_enabled = _community_enabled()
    community_rows = []
    with db.connection() as con:
        if qid:
            rows = con.execute(
                "SELECT * FROM ai_explanations WHERE user_id=? AND qid=? ORDER BY id DESC LIMIT ?",
                (user["id"], qid, limit),
            ).fetchall()
            if community_enabled:
                community_rows = con.execute(
                    "SELECT * FROM ai_explanations WHERE qid=? AND user_id<>? AND upload_id IS NULL "
                    "AND share_eligible=1 AND shared_at IS NOT NULL "
                    "AND EXISTS (SELECT 1 FROM attempts WHERE user_id=? AND qid=?) "
                    "ORDER BY CASE WHEN featured_at IS NULL THEN 1 ELSE 0 END, id DESC LIMIT ?",
                    (qid, user["id"], user["id"], qid, limit),
                ).fetchall()
        else:
            rows = con.execute(
                "SELECT * FROM ai_explanations WHERE user_id=? AND upload_id=? ORDER BY id DESC LIMIT ?",
                (user["id"], upload_id, limit),
            ).fetchall()
    return {
        "items": [_explanation_public(r, sharing_enabled=community_enabled) for r in rows],
        "community_items": [_explanation_public(r, community=True) for r in community_rows],
        "community_enabled": community_enabled,
    }


def _community_enabled() -> bool:
    return os.environ.get("COMMUNITY_EXPLANATIONS_ENABLED", "").lower() in {"1", "true", "yes"}


def _explanation_public(row, *, community=False, sharing_enabled=False):
    content = json.loads(row["content_json"])
    models = json.loads(row["models_json"])
    if community:
        models = [{"model": call["model"]} for call in models if call.get("model")]
        verification = {
            "status": "agreed", "confidence": "high",
            "notice": "双模型独立求解一致，且与原卷参考答案一致。该讲解由 AI 生成。",
        }
    else:
        verification = json.loads(row["verification_json"]) if row["verification_json"] else None
    return {
        "id": row["id"], "qid": row["qid"], "upload_id": row["upload_id"],
        "mode": row["mode"], "models": models,
        "content": content.get("content", ""),
        "mode_cards": content.get("mode_cards") or [],
        "knowledge_points": content.get("knowledge_points") or [],
        "method": content.get("method") or "",
        "verification": verification,
        "prompt_version": row["prompt_version"], "created_at": row["created_at"],
        "can_share": bool(sharing_enabled and row["share_eligible"]),
        "shared": bool((sharing_enabled or community) and row["shared_at"]),
        "featured": bool((sharing_enabled or community) and row["featured_at"]),
    }


class ExplanationShareInput(BaseModel):
    shared: bool


@router.put("/explanations/{exp_id}/share")
def share_explanation(exp_id: int, data: ExplanationShareInput, user=Depends(current_user)):
    if not _community_enabled():
        raise HTTPException(404, "社区讲解未启用")
    with db.connection() as con:
        if data.shared:
            row = con.execute(
                "UPDATE ai_explanations SET shared_at=COALESCE(shared_at, ?) WHERE id=? AND user_id=? "
                "AND qid IS NOT NULL AND upload_id IS NULL AND share_eligible=1 RETURNING shared_at, featured_at",
                (db.utcnow(), exp_id, user["id"]),
            ).fetchone()
            if row is None and con.execute(
                "SELECT 1 FROM ai_explanations WHERE id=? AND user_id=?", (exp_id, user["id"]),
            ).fetchone():
                raise HTTPException(422, "只能共享无自定义要求且通过原卷答案交叉核验的题库讲解")
        else:
            row = con.execute(
                "UPDATE ai_explanations SET shared_at=NULL, featured_at=NULL WHERE id=? AND user_id=? "
                "RETURNING shared_at, featured_at",
                (exp_id, user["id"]),
            ).fetchone()
    if row is None:
        raise HTTPException(404, "未找到该讲解记录")
    return {"id": exp_id, "shared": bool(row["shared_at"]), "featured": bool(row["featured_at"])}


class ExplanationFeatureInput(BaseModel):
    featured: bool


@router.get("/admin/community-explanations")
def list_community_explanations(
    limit: int = Query(20, ge=1, le=50),
    offset: int = Query(0, ge=0),
    user=Depends(require_admin),
):
    if not _community_enabled():
        return {"enabled": False, "items": [], "has_more": False}
    with db.connection() as con:
        rows = con.execute(
            "SELECT * FROM ai_explanations WHERE qid IS NOT NULL AND upload_id IS NULL "
            "AND share_eligible=1 AND shared_at IS NOT NULL "
            "ORDER BY CASE WHEN featured_at IS NULL THEN 0 ELSE 1 END, id DESC LIMIT ? OFFSET ?",
            (limit + 1, offset),
        ).fetchall()
    return {
        "enabled": True, "items": [_explanation_public(r, community=True) for r in rows[:limit]],
        "has_more": len(rows) > limit,
    }


@router.put("/admin/community-explanations/{exp_id}/feature")
def feature_explanation(exp_id: int, data: ExplanationFeatureInput, user=Depends(require_admin)):
    if not _community_enabled():
        raise HTTPException(404, "社区讲解未启用")
    with db.connection() as con:
        row = con.execute(
            "UPDATE ai_explanations SET featured_at=? WHERE id=? AND qid IS NOT NULL AND upload_id IS NULL "
            "AND share_eligible=1 AND shared_at IS NOT NULL RETURNING featured_at",
            (db.utcnow() if data.featured else None, exp_id),
        ).fetchone()
    if row is None:
        raise HTTPException(404, "未找到已共享的题库讲解")
    return {"id": exp_id, "featured": bool(row["featured_at"])}


@router.delete("/explanations/{exp_id}")
def delete_explanation(exp_id: int, user=Depends(current_user)):
    with db.connection() as con:
        cur = con.execute("DELETE FROM ai_explanations WHERE id=? AND user_id=?", (exp_id, user["id"]))
        if cur.rowcount == 0:
            raise HTTPException(404, "未找到该讲解记录")
    return {"deleted": True}


async def submit_job(user: dict, data: JobInput) -> dict:
    """创建并派发一个 AI 任务（HTTP 路由与后台入口共用同一套准入校验）。

    校验与旧的 ``POST /api/ai/jobs`` 行为完全一致：模型/连接归属、任务参数、
    每人活跃数与 24h 配额、全站并发上限、主观题去重、裁判独立性等。
    """
    from . import _run_job as run_job
    from .. import practice

    if int(data.provider_id or 0) == 0 and data.task != "course-narration":
        raise HTTPException(422, "本地语音连接仅用于课堂旁白任务")
    provider = _selected_provider(
        user["id"], data.provider_id, data.model,
        allow_local=data.task == "course-narration",
    )
    if data.task == "tagging" and user["role"] != "admin":
        raise HTTPException(403, "批量打标仅限管理员操作")
    if data.task == "grade" and not data.cross_verify:
        raise HTTPException(422, "主观题评分必须开启独立交叉核验")
    if data.cross_verify:
        if not data.verify_provider_id or not data.verify_model:
            raise HTTPException(422, "请选择独立的核验连接和模型")
        verifier = _selected_provider(user["id"], data.verify_provider_id, data.verify_model)
        if _identity(provider, data.model) == _identity(verifier, data.verify_model):
            raise HTTPException(422, "相同连接和模型重复调用不构成交叉核验，请选择不同模型")
    if data.judge_provider_id and data.judge_model:
        judge = _selected_provider(user["id"], data.judge_provider_id, data.judge_model)
        if _identity(judge, data.judge_model) == _identity(provider, data.model):
            raise HTTPException(422, "主模型不能担任同一任务的裁判，请选择独立的裁判模型")

    extra = EXTRA_TASKS.get(data.task)
    if extra is None and data.task not in STANDARD_TASKS:
        raise HTTPException(422, "未知任务类型")
    if data.params is not None:
        if len(_json(data.params).encode("utf-8")) > 16384:
            raise HTTPException(422, "任务参数过大")
    if extra is not None:
        # 扩展模块自行校验所有权与参数；校验器抛出的 HTTPException 原样返回。
        validator = extra.get("validate")
        if validator is not None:
            validator(data, user)
    elif data.task == "notes":
        doc_ids = list(dict.fromkeys([d for d in (data.document_ids or []) if d]))
        if data.document_id and data.document_id not in doc_ids:
            doc_ids.insert(0, data.document_id)
        if data.source_mode == "user_docs":
            if not doc_ids:
                raise HTTPException(422, "请先上传并选择资料")
            for did in doc_ids:
                doc = _owned("ai_documents", did, user["id"])
                if doc["status"] != "ready":
                    raise HTTPException(422, "资料没有可用文本，请先进行 OCR")
        if data.template not in {t["id"] for t in TEMPLATES}:
            raise HTTPException(422, "未知笔记模板")
    elif data.task == "tagging":
        if not data.subject:
            raise HTTPException(422, "请选择要批量打标的学科")
    elif data.task == "recognize":
        if not data.upload_id:
            raise HTTPException(422, "请先上传题目图片")
        _owned("ai_uploads", data.upload_id, user["id"])
    elif data.task == "ocr":
        if not data.document_id:
            raise HTTPException(422, "请选择要识别的扫描 PDF 资料")
        doc = _owned("ai_documents", data.document_id, user["id"])
        if not str(doc["storage_name"] or "").lower().endswith(".pdf"):
            raise HTTPException(422, "仅扫描 PDF 支持逐页 OCR")
        total = int(doc["pages"] or 0)
        if data.page_from > total:
            raise HTTPException(422, "OCR 起始页超出资料页数")
        if data.page_to is not None and data.page_to < data.page_from:
            raise HTTPException(422, "OCR 页码范围不正确，结束页不能早于起始页")
    elif data.qid:
        practice.ensure_unlocked(user["id"], data.qid)
        practice.load_question(data.qid)
        if data.task == "grade":
            latest = practice.latest_attempt(user["id"], data.qid)
            if not latest or latest["id"] != data.attempt_id:
                raise HTTPException(422, "只能核验本人当前最新作答，请重新打开题目")
            if latest.get("grade_source") == "reference" and latest.get("result") in ("correct", "incorrect"):
                raise HTTPException(422, "该作答已按原卷答案完成客观判题；AI 可讲解，但不能改写原卷判题结果")
    elif data.upload_id and data.task in ("explain", "question-analysis"):
        _owned("ai_uploads", data.upload_id, user["id"])
    else:
        raise HTTPException(422, "请选择题目、上传题目或资料")

    token = uuid4().hex
    per_user = max_active_per_user()
    global_limit = global_concurrency()
    active_states = "('queued','running','cancel_requested')"
    with db.connection() as con:
        db.write_lock(con, "job-admission")
        # 取消请求中的任务仍占用配额：Worker 落定前不能无限再提交。
        active = con.execute(
            f"SELECT COUNT(*) FROM ai_jobs WHERE user_id=? AND status IN {active_states}",
            (user["id"],),
        ).fetchone()[0]
        global_active = con.execute(
            f"SELECT COUNT(*) FROM ai_jobs WHERE status IN {active_states}"
        ).fetchone()[0]
        today = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        recent = con.execute("SELECT COUNT(*) FROM ai_jobs WHERE user_id=? AND created_at>?", (user["id"], today)).fetchone()[0]
        if active >= per_user or global_active >= global_limit or recent >= DAILY_JOB_LIMIT:
            raise HTTPException(
                429,
                f"每人最多同时运行 {per_user} 个任务、24 小时内 {DAILY_JOB_LIMIT} 个任务，请稍后重试",
            )
        # 按任务类型的每人每日配额与全站并发：都在 INSERT 之前校验，被拒绝的请求不落库、不消耗配额。
        ai_usage.check_new_job(con, user_id=user["id"], kind=data.task)
        if data.task == "grade":
            running = con.execute(
                "SELECT request_json FROM ai_jobs WHERE user_id=? AND task='grade' AND status IN ('queued','running','cancel_requested')",
                (user["id"],),
            ).fetchall()
            if any(json.loads(r[0]).get("attempt_id") == data.attempt_id for r in running):
                raise HTTPException(409, "这次作答已有评分任务进行中，请等待该任务完成")
        now = db.utcnow()
        job_id = con.execute(
            "INSERT INTO ai_jobs(user_id,task,status,stage,request_json,partial_content,cancelled,created_at,updated_at,dispatch_token) "
            "VALUES (?,?,?,?,?,'',0,?,?,?)",
            (user["id"], data.task, "queued", "queued", _json(data.model_dump()), now, now, token),
        ).lastrowid

    ai_usage.note_job_active(job_id, data.task)
    await _dispatch(job_id, token, run_job)
    return _job_public(_owned("ai_jobs", job_id, user["id"]))


@router.post("/ai/jobs")
async def create_job(data: JobInput, user=Depends(current_user)):
    return await submit_job(user, data)


def _spawn_job_task(job_id: int, token: str, runner):
    task = asyncio.create_task(execute_dispatched_job(job_id, token, runner))
    _tasks.add(task)
    _running_by_id[job_id] = task

    def _cleanup(t):
        _tasks.discard(t)
        if _running_by_id.get(job_id) is t:
            _running_by_id.pop(job_id, None)

    task.add_done_callback(_cleanup)


@router.get("/ai/jobs")
def jobs(user=Depends(current_user)):
    with db.connection() as con:
        rows = con.execute("SELECT * FROM ai_jobs WHERE user_id=? ORDER BY id DESC LIMIT 50", (user["id"],)).fetchall()
    return {"items": [_job_public(r) for r in rows]}


@router.get("/ai/jobs/{job_id}")
def job_detail(job_id: int, user=Depends(current_user)):
    return _job_public(_owned("ai_jobs", job_id, user["id"]))


@router.post("/ai/jobs/{job_id}/cancel")
async def cancel_job(job_id: int, user=Depends(current_user)):
    row = _owned("ai_jobs", job_id, user["id"])
    if row["status"] in ("completed", "failed", "interrupted", "cancelled"):
        return _job_public(row)
    now = db.utcnow()
    with db.connection() as con:
        db.write_lock(con, "job-cancel")
        current = con.execute("SELECT status FROM ai_jobs WHERE id=?", (job_id,)).fetchone()
        status = current["status"] if current else row["status"]
        if status == "queued":
            # 尚未开始执行：直接落定为已取消。
            con.execute(
                "UPDATE ai_jobs SET status='cancelled',stage='cancelled',cancelled=1,error=?,updated_at=?,"
                "cancel_requested_at=COALESCE(cancel_requested_at,?),finished_at=? WHERE id=? AND status='queued'",
                ("用户已取消任务", now, now, now, job_id),
            )
        elif status in ("running", "cancel_requested"):
            # 已开始执行：先进入「取消请求」，等待执行进程确认后落定为已取消。
            con.execute(
                "UPDATE ai_jobs SET status='cancel_requested',stage='cancel_requested',cancelled=1,error=?,"
                "updated_at=?,cancel_requested_at=COALESCE(cancel_requested_at,?) "
                "WHERE id=? AND status IN ('running','cancel_requested')",
                ("已收到取消请求，正在停止任务", now, now, job_id),
            )
        elif status == "awaiting_outline":
            # 等待人工确认提纲：执行进程已退出，没有正在运行的协程可取消，直接落定。
            # 否则任务会永久停在 awaiting_outline，既无法取消也无法释放该用户的任务槽。
            con.execute(
                "UPDATE ai_jobs SET status='cancelled',stage='cancelled',cancelled=1,error=?,updated_at=?,"
                "cancel_requested_at=COALESCE(cancel_requested_at,?),finished_at=? "
                "WHERE id=? AND status='awaiting_outline'",
                ("用户已取消等待确认的提纲任务", now, now, now, job_id),
            )
    t = _running_by_id.get(job_id)
    if t and not t.done():
        t.cancel()
        # 本进程内执行的任务会立即落定；跨进程 Worker 由心跳轮询确认，保持「取消中」。
        if isinstance(t, asyncio.Future):
            try:
                await asyncio.wait({t}, timeout=2.0)
            except asyncio.CancelledError:
                pass
    elif redis_url():
        # 分布式模式：当前 Web 进程没有任务句柄，不能直接宣布取消完成；
        # 置为 cancel_requested 交给 Worker 轮询落定，失联 Worker 的租约由 reap_stale_jobs 回收。
        pass
    else:
        _finalize_cancel(job_id)
    latest = _owned("ai_jobs", job_id, user["id"])
    if latest["status"] == "cancelled":
        # 落定为已取消：同步写用量账本（幂等；非终态时该方法直接返回）。
        ai_usage.record_job_usage(job_id)
        _emit_stream(job_id, "done", {"status": "cancelled"})
    else:
        _emit_stream(job_id, "stage", {"status": "cancel_requested", "stage": "cancel_requested"})
    return _job_public(latest)


@router.post("/ai/jobs/{job_id}/confirm-outline")
async def confirm_job_outline(job_id: int, body: OutlineConfirmInput, user=Depends(current_user)):
    """Human-in-the-loop: confirm/edit outline and resume notes generation."""
    from . import _run_job as run_job
    row = _owned("ai_jobs", job_id, user["id"])
    if row["status"] != "awaiting_outline":
        raise HTTPException(409, "当前任务不在等待确认提纲状态")
    cp = _job_get_checkpoint(job_id) or {}
    cp["confirmed_outline"] = body.outline.strip()
    sections = [s.strip() for s in body.sections if s.strip()]
    if not sections:
        # Auto-extract ## headings as parallel sections
        sections = [line.lstrip("#").strip() for line in body.outline.splitlines() if line.strip().startswith("## ")]
    cp["confirmed_sections"] = sections[:12]
    data = JobInput(**json.loads(row["request_json"]))
    data.confirm_outline = False
    token = _requeue_job(row, data, checkpoint=cp)
    await _dispatch(job_id, token, run_job)
    return _job_public(_owned("ai_jobs", job_id, user["id"]))


@router.post("/ai/jobs/{job_id}/resume")
async def resume_job(job_id: int, user=Depends(current_user)):
    """Resume an interrupted or failed checkpointed job from its last completed step."""
    from . import _run_job as run_job
    row = _owned("ai_jobs", job_id, user["id"])
    if row["status"] not in ("interrupted", "failed", "cancelled"):
        raise HTTPException(409, "仅支持续跑已中断、失败或取消的任务")
    data = JobInput(**json.loads(row["request_json"]))
    token = _requeue_job(row, data)
    await _dispatch(job_id, token, run_job)
    return _job_public(_owned("ai_jobs", job_id, user["id"]))


def _requeue_job(row, data, *, checkpoint=None):
    token = uuid4().hex
    with db.connection() as con:
        db.write_lock(con, "job-admission")
        per_user = max_active_per_user()
        global_limit = global_concurrency()
        active = con.execute(
            "SELECT COUNT(*) FROM ai_jobs WHERE user_id=? AND status IN ('queued','running','cancel_requested')",
            (row["user_id"],),
        ).fetchone()[0]
        global_active = con.execute(
            "SELECT COUNT(*) FROM ai_jobs WHERE status IN ('queued','running','cancel_requested')"
        ).fetchone()[0]
        if active >= per_user or global_active >= global_limit:
            raise HTTPException(429, "同时运行的任务已达上限，请稍后重试")
        # 续跑不新增任务行（不重复消耗每日配额），但仍受该任务类型的全站并发上限约束。
        ai_usage.check_resume(con, kind=row["task"], job_id=row["id"])
        changed = con.execute(
            "UPDATE ai_jobs SET status='queued',stage='resuming',cancelled=0,error=NULL,"
            "request_json=?,checkpoint_json=COALESCE(?,checkpoint_json),partial_content='',"
            "retry_count=COALESCE(retry_count,0)+1,"
            "updated_at=?,dispatch_token=? WHERE id=? AND status=? AND updated_at=?",
            (_json(data.model_dump()), _json(checkpoint) if checkpoint is not None else None,
             db.utcnow(), token, row["id"], row["status"], row["updated_at"]),
        ).rowcount
        if not changed:
            raise HTTPException(409, "任务状态已变化，请刷新后重试")
    # 旧终态账本先清掉：下一次终态写入时重建唯一行，并把重试次数记到新账本里。
    ai_usage.clear_job_usage(row["id"])
    ai_usage.note_job_active(row["id"], row["task"])
    return token


class _RedisStreamUnavailable(RuntimeError):
    """Redis Pub/Sub 不可用：SSE 回退到 0.75s 数据库轮询实现。"""


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {_json(data)}\n\n"


def _parse_publish_payload(raw) -> dict | None:
    """把 Pub/Sub 消息体解析成 {"event","data","ts"}；脏数据只丢弃不抛错。"""
    if isinstance(raw, (bytes, bytearray)):
        try:
            raw = bytes(raw).decode("utf-8", "replace")
        except Exception:
            return None
    if not isinstance(raw, str):
        return None
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        log.warning("忽略无法解析的 AI 任务流消息")
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("event"), str):
        return None
    if not isinstance(payload.get("data"), dict):
        return None
    return payload


def _delta_text_of(messages) -> str:
    return "".join(
        item["data"].get("text") or "" for item in messages if item.get("event") == "delta"
    )


def _covered_prefix_len(partial: str, messages) -> int:
    """已落库 partial_content 覆盖的缓冲增量前缀长度，避免重连补齐后重复追加。"""
    text = _delta_text_of(messages)
    if not text or not partial:
        return 0
    for size in range(min(len(text), len(partial)), 0, -1):
        if partial.endswith(text[:size]):
            return size
    return 0


async def _drain_pubsub(pubsub) -> list[dict]:
    """订阅建立后先取走已缓冲的消息，与落库补齐全量去重。"""
    messages = []
    while True:
        try:
            raw = await asyncio.wait_for(
                pubsub.get_message(ignore_subscribe_messages=True, timeout=0), timeout=0.05
            )
        except Exception:
            break
        if not raw:
            break
        payload = _parse_publish_payload(raw.get("data"))
        if payload is not None:
            messages.append(payload)
    return messages


async def _close_redis_stream(pubsub, client, channel) -> None:
    if pubsub is not None:
        try:
            await pubsub.unsubscribe(channel)
        except Exception:
            pass
        try:
            await pubsub.aclose()
        except Exception:
            pass
    if client is not None:
        try:
            await client.aclose()
        except Exception:
            pass


async def _redis_stream_frames(job_id: int, user: dict, aioredis):
    """订阅 `zhixu:job:{job_id}` 并产出 SSE 帧（真跨进程增量流式）。

    连接/订阅失败抛 `_RedisStreamUnavailable`，由调用方回退数据库轮询；
    取消/断线时在 finally 中退订并关闭连接，不再持有订阅。
    """
    channel = _job_channel(job_id)
    client = None
    pubsub = None
    try:
        client = aioredis.Redis.from_url(
            redis_url(),
            decode_responses=True,
            socket_connect_timeout=REDIS_SOCKET_TIMEOUT_SECONDS,
            socket_timeout=REDIS_SOCKET_TIMEOUT_SECONDS,
        )
        pubsub = client.pubsub()
        await pubsub.subscribe(channel)
    except Exception as exc:
        await _close_redis_stream(pubsub, client, channel)
        _warn_redis_once(
            "stream-subscribe", f"AI 任务流 {job_id} 无法订阅 Redis Pub/Sub，回退数据库轮询：{exc}"
        )
        raise _RedisStreamUnavailable(str(exc)) from None
    try:
        # 订阅建立后先发最新 snapshot，并把已落库 partial_content 作为一次 replace 补齐。
        latest = _owned("ai_jobs", job_id, user["id"])
        yield _sse("snapshot", _job_public(latest))
        if latest["status"] in TERMINAL_STATES:
            yield _sse("done", {"status": latest["status"]})
            return
        partial = latest.get("partial_content") or ""
        buffered = await _drain_pubsub(pubsub)
        covered = 0
        if partial:
            yield _sse("delta", {"text": partial, "replace": True})
            covered = _covered_prefix_len(partial, buffered)
        for payload in buffered:
            if payload["event"] == "delta":
                continue
            yield _sse(payload["event"], payload["data"])
            if payload["event"] == "done":
                return
        rest = _delta_text_of(buffered)[covered:]
        if rest:
            yield _sse("delta", {"text": rest})
        keepalive_at = time.monotonic() + STREAM_KEEPALIVE_SECONDS
        while True:
            message = None
            try:
                message = await asyncio.wait_for(
                    pubsub.get_message(ignore_subscribe_messages=True, timeout=STREAM_PUBSUB_POLL_SECONDS),
                    timeout=STREAM_PUBSUB_POLL_SECONDS + 1.0,
                )
            except asyncio.TimeoutError:
                message = None
            except Exception:
                log.warning(
                    "AI 任务流 %s 的 Redis 订阅中断，客户端可重连（DB 已保留 partial_content 兜底）",
                    job_id, exc_info=True,
                )
                return
            if message:
                payload = _parse_publish_payload(message.get("data"))
                if payload is not None:
                    yield _sse(payload["event"], payload["data"])
                    if payload["event"] == "done":
                        return
                    keepalive_at = time.monotonic() + STREAM_KEEPALIVE_SECONDS
                continue
            # get_message 无消息时返回 None：给事件循环一次让出机会，避免空转。
            await asyncio.sleep(min(0.05, max(STREAM_PUBSUB_POLL_SECONDS, 0.001)))
            if time.monotonic() < keepalive_at:
                continue
            keepalive_at = time.monotonic() + STREAM_KEEPALIVE_SECONDS
            try:
                current = _job_public(_owned("ai_jobs", job_id, user["id"]))
            except Exception:
                current = None
            if current is not None and current["status"] in TERMINAL_STATES:
                yield _sse("snapshot", current)
                yield _sse("done", {"status": current["status"]})
                return
            yield ": keepalive\n\n"
    finally:
        await _close_redis_stream(pubsub, client, channel)


@router.get("/ai/jobs/{job_id}/stream")
async def stream_job(job_id: int, user=Depends(current_user)):
    """SSE endpoint streaming stage transitions and live model text deltas.

    - 未配置 REDIS_URL：同进程 Worker 的内存队列事件；
    - 配置 REDIS_URL：订阅 `zhixu:job:{job_id}` Pub/Sub 增量（真跨进程流式），
      订阅失败或缺包时自动回退 0.75s 数据库轮询。
    """
    initial = _owned("ai_jobs", job_id, user["id"])

    async def event_gen():
        # Send current snapshot immediately
        snap = _job_public(initial)
        yield _sse("snapshot", snap)
        if snap["status"] in TERMINAL_STATES:
            yield _sse("done", {"status": snap["status"]})
            return

        # Catch up any buffered text already emitted
        existing_buf = "".join(_stream_buffers.get(job_id, [])) or snap.get("partial_content") or ""
        if existing_buf:
            yield _sse("delta", {"text": existing_buf, "replace": True})

        if redis_url():
            aioredis = _load_aioredis()
            if aioredis is not None:
                try:
                    async for frame in _redis_stream_frames(job_id, user, aioredis):
                        yield frame
                    return
                except _RedisStreamUnavailable:
                    pass
                except Exception:
                    log.warning(
                        "AI 任务流 %s 的 Pub/Sub 订阅异常结束，客户端可重连", job_id, exc_info=True
                    )
                    return

            # Redis 不可用：回退旧的 0.75s 数据库轮询实现（增量以 replace 全量下发）。
            latest = _owned("ai_jobs", job_id, user["id"])
            last_update = latest["updated_at"]
            last_partial = latest["partial_content"] or ""
            yield _sse("snapshot", _job_public(latest))
            if latest["status"] in TERMINAL_STATES:
                yield _sse("done", {"status": latest["status"]})
                return
            if last_partial and last_partial != existing_buf:
                yield _sse("delta", {"text": last_partial, "replace": True})
            keepalive_at = time.monotonic() + STREAM_KEEPALIVE_SECONDS
            while True:
                await asyncio.sleep(STREAM_POLL_SECONDS)
                latest = _owned("ai_jobs", job_id, user["id"])
                if latest["updated_at"] != last_update:
                    last_update = latest["updated_at"]
                    if latest["partial_content"] != last_partial:
                        last_partial = latest["partial_content"] or ""
                        yield _sse("delta", {"text": last_partial, "replace": True})
                    yield _sse("snapshot", _job_public(latest))
                    if latest["status"] in TERMINAL_STATES:
                        yield _sse("done", {"status": latest["status"]})
                        return
                    keepalive_at = time.monotonic() + STREAM_KEEPALIVE_SECONDS
                elif time.monotonic() >= keepalive_at:
                    yield ": keepalive\n\n"
                    keepalive_at = time.monotonic() + STREAM_KEEPALIVE_SECONDS

        q: asyncio.Queue = asyncio.Queue(maxsize=256)
        _stream_queues.setdefault(job_id, []).append(q)
        try:
            while True:
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=STREAM_KEEPALIVE_SECONDS)
                    yield _sse(msg["event"], msg["data"])
                    if msg["event"] == "done":
                        break
                except asyncio.TimeoutError:
                    # Heartbeat & check DB status in case task finished
                    latest = _owned("ai_jobs", job_id, user["id"])
                    if latest["status"] in TERMINAL_STATES:
                        yield _sse("snapshot", _job_public(latest))
                        yield _sse("done", {"status": latest["status"]})
                        break
                    yield ": keepalive\n\n"
        finally:
            lst = _stream_queues.get(job_id)
            if lst:
                if q in lst:
                    lst.remove(q)
                if not lst:
                    # 消费者全部断开：删除空列表，避免 _stream_queues 内存泄漏。
                    _stream_queues.pop(job_id, None)

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _input_reference(data: "JobInput", prompt_version: str) -> dict:
    """记录本次任务实际使用的输入引用，供任务结果和费用对账复核。"""
    reference = {
        "task": data.task, "model": data.model,
        "verify_model": data.verify_model if data.cross_verify else None,
        "judge_model": data.judge_model if (data.judge_provider_id and data.judge_model) else None,
        "qid": data.qid, "attempt_id": data.attempt_id, "upload_id": data.upload_id,
        "document_ids": data.document_ids or ([data.document_id] if data.document_id else []),
        "prompt_version": prompt_version,
        "explain_mode": data.explain_mode, "modes": data.modes or [],
    }
    if data.qid:
        try:
            from .. import practice
            question = practice.load_question(data.qid)
            reference.update({
                "bank_version": question.get("bank_version"),
                "content_revision": question.get("content_revision"),
                "answer_revision": question.get("answer_revision"),
                "practice_status": question.get("practice_status"),
                "grading_status": question.get("grading_status"),
            })
        except Exception:
            reference["question_context"] = "unavailable"
    return reference


async def _run_job(job_id, user_id, data: JobInput):
    from . import PROMPT_VERSION
    _stream_buffers[job_id] = []
    flush_counter = 0
    last_flush = 0.0

    thinking_chars = 0

    def on_thinking(text: str):
        """推理模型（DeepSeek-R1/QwQ 等）的思考增量：单独事件，不混入正文。"""
        nonlocal thinking_chars
        if not text:
            return
        thinking_chars += len(text)
        _emit_stream(job_id, "thinking", {"text": text, "chars": thinking_chars})

    def on_delta(text: str):
        nonlocal flush_counter, last_flush
        _emit_stream(job_id, "delta", {"text": text})
        flush_counter += 1
        now = time.monotonic()
        if (redis_url() and now - last_flush >= 0.75) or (not redis_url() and flush_counter % 20 == 0):
            _flush_partial(job_id)
            last_flush = now

    try:
        async with _global_semaphore():
            _job_update(job_id, "running", "preparing")
            provider = _selected_provider(
                user_id, data.provider_id, data.model,
                allow_local=data.task == "course-narration",
            )
            verifier = _selected_provider(user_id, data.verify_provider_id, data.verify_model) if data.cross_verify else None
            judge = None
            judge_model = None
            if data.judge_provider_id and data.judge_model:
                judge = _selected_provider(user_id, data.judge_provider_id, data.judge_model)
                judge_model = data.judge_model
            elif data.cross_verify:
                # Look up user's saved default judge model if configured
                with db.connection() as con:
                    td = con.execute("SELECT judge_provider_id, judge_model FROM ai_task_defaults WHERE user_id=? AND task='judge'", (user_id,)).fetchone()
                if td and td["judge_provider_id"] and td["judge_model"]:
                    try:
                        cand = _selected_provider(user_id, td["judge_provider_id"], td["judge_model"])
                        if _identity(cand, td["judge_model"]) != _identity(provider, data.model):
                            judge, judge_model = cand, td["judge_model"]
                    except HTTPException:
                        pass
            # 服务端硬性规则：主模型不能担任同一任务的裁判；历史配置若相同则退回独立核验模型。
            if judge is not None and judge_model and _identity(judge, judge_model) == _identity(provider, data.model):
                judge, judge_model = None, None

            if data.task == "grade":
                result = await _grade_job(user_id, data, provider, verifier, job_id=job_id)
            elif data.task == "notes":
                result = await _notes_job(job_id, user_id, data, provider, verifier, on_delta=on_delta)
                if isinstance(result, dict) and result.get("__paused__"):
                    _emit_stream(job_id, "done", {"status": "awaiting_outline"})
                    return
            elif data.task == "ocr":
                # PDF 逐页 OCR 异步执行器由 notes_agent 提供（分页识别/落库/断点续跑/SSE 进度）。
                try:
                    from .notes_agent import _ocr_job
                except ImportError as exc:  # 执行器尚未在 notes_agent 中实现
                    raise JobUnavailableError("OCR 任务执行器尚未就绪") from exc
                result = await _ocr_job(job_id, user_id, data, provider)
            elif data.task == "recognize":
                result = await _recognize_job(user_id, data, provider, verifier, job_id=job_id)
            elif data.task == "tagging":
                result = await _tagging_job(job_id, user_id, data, provider)
            elif data.task in EXTRA_TASKS:
                # 整合模块注册的扩展任务（AI 课堂、试卷工坊、AI 助手、学情报告等）。
                result = await EXTRA_TASKS[data.task]["handler"](
                    job_id, user_id, data, provider, verifier,
                )
                # 与 notes 的人工确认语义一致：执行器已把状态写成 awaiting_outline，
                # 这里直接结束本轮，等待 POST /api/ai/jobs/{id}/confirm-outline 续跑。
                if isinstance(result, dict) and result.get("__paused__"):
                    _emit_stream(job_id, "done", {"status": "awaiting_outline"})
                    return
            else:
                result = await _explain_job(
                    user_id, data, provider, verifier,
                    judge=judge, judge_model=judge_model, on_delta=on_delta,
                    on_thinking=on_thinking, job_id=job_id,
                )

            _flush_partial(job_id)
            result["prompt_version"] = PROMPT_VERSION
            result["models"] = (
                [{"provider_id": provider["id"], "model": data.model}]
                + ([{"provider_id": verifier["id"], "model": data.verify_model}] if verifier else [])
                + ([{"provider_id": judge["id"], "model": judge_model}] if judge else [])
            )
            result["input_ref"] = _input_reference(data, PROMPT_VERSION)
            if is_job_cancelled(job_id):
                # 取消请求已到达：产物若已按 job_id 幂等提交则保留，任务状态落定为已取消。
                _finalize_cancel(job_id)
                _emit_stream(job_id, "done", {"status": "cancelled"})
                return
            _job_update(job_id, "completed", "completed", result=result)
            _emit_stream(job_id, "done", {"status": "completed"})
    except asyncio.CancelledError:
        _flush_partial(job_id)
        cancelled = is_job_cancelled(job_id)
        if cancelled:
            _finalize_cancel(job_id)
        else:
            _job_update(job_id, "interrupted", "interrupted", error="服务停止，任务已中断，请续跑或重新发起")
        _emit_stream(job_id, "done", {"status": "cancelled" if cancelled else "interrupted"})
        raise
    except Exception as exc:
        _flush_partial(job_id)
        if data.task == "grade" and data.attempt_id:
            try:
                from .. import practice
                latest = practice.latest_attempt(user_id, data.qid)
                if latest and latest["id"] == data.attempt_id and latest["result"] == "pending":
                    practice.set_ai_grade(user_id, data.attempt_id, "pending", {"error": "AI 评分未完整完成，保持待确认"})
            except Exception:
                pass
        message = (
            exc.detail if isinstance(exc, HTTPException)
            else str(exc) if isinstance(exc, (ValueError, JobUnavailableError))
            else "任务处理失败，请检查文件与模型配置后重试"
        )
        _job_update(job_id, "failed", "failed", error=message[:500])
        _emit_stream(job_id, "done", {"status": "failed", "error": message[:500]})
    finally:
        _stream_buffers.pop(job_id, None)
