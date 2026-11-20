"""Optional arq (Redis) distributed worker adapter for multi-process production deployments.

Start worker:
  arq deploy.worker.WorkerSettings
"""
import json
import os

try:  # 本地未安装 arq 时仍允许导入模块（Web 进程和单元测试不需要队列实现）
    from arq import cron
except ImportError:  # pragma: no cover - 仅缺少可选依赖时发生
    cron = None

from app import ai, db, practice
from app.ai.jobs import QUEUE_NAME, execute_dispatched_job, redis_settings, redis_url


async def run_ai_job_task(ctx, job_id: int, token: str):
    await execute_dispatched_job(
        job_id, token, ai._run_job, retry=ctx["job_try"] > 1, monitor=True,
    )


async def write_worker_heartbeat(ctx):
    """就绪检查读取的心跳：证明有可用的 Worker 在消费任务队列。"""
    with db.connection() as con:
        con.execute(
            "INSERT INTO service_heartbeat(name,beat_at,payload_json) VALUES ('worker',?,?) "
            "ON CONFLICT(name) DO UPDATE SET beat_at=excluded.beat_at, payload_json=excluded.payload_json",
            (
                db.utcnow(),
                json.dumps({
                    "queue": QUEUE_NAME,
                    "concurrency": os.environ.get("STUDY_WORKER_CONCURRENCY", "8"),
                }, ensure_ascii=False),
            ),
        )


async def startup(ctx):
    if not redis_url():
        raise RuntimeError("REDIS_URL is required for the distributed worker")
    db.init_db()
    practice.init_practice()
    ai.init_ai()
    await write_worker_heartbeat(ctx)


async def shutdown(ctx):
    await ai.shutdown_ai()


class WorkerSettings:
    functions = [run_ai_job_task]
    on_startup = startup
    on_shutdown = shutdown
    max_jobs = int(os.environ.get("STUDY_WORKER_CONCURRENCY", "8"))
    job_timeout = int(os.environ.get("STUDY_WORKER_JOB_TIMEOUT", "1800"))
    queue_name = QUEUE_NAME
    redis_settings = redis_settings() if redis_url() else None
    # 每 30 秒续写心跳；就绪检查超过 90 秒无心跳会标记 Worker 异常。
    cron_jobs = [cron(write_worker_heartbeat, second={0, 30})] if cron else []

