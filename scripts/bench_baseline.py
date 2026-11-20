"""跨进程性能基线：固定题库 + 并发浏览会话 + 活跃任务，记录关键接口与 SSE 刷新间隔。

用法（先启动一个用于压测的实例，不要对正式库压测）：
  python scripts/bench_baseline.py \
      --base-url http://127.0.0.1:8765 \
      --username bench-admin --password bench-password-123 \
      --sessions 20 --rounds 3 --out eval/baseline-20261120.json

可选项：
  --qid <qid>         指定题目详情/提交使用的题号；缺省时从只读题库自动取一道可作答题
  --answer <选项>     作答提交使用的答案；缺省时自动读取原卷答案（仅用于压测账号）
  --allow-submit      允许调用 POST /api/attempts（会写入压测账号的作答记录）
  --job-id <id>       轮询一个进行中的任务，记录 SSE 增量刷新间隔
  --sse-seconds 20    SSE 采样时长

输出 JSON 包含：每类请求的 count / 错误率 / mean / p50 / p95 / max，
SSE 事件间隔的 p50/p95/最大间隔，以及（配置 DATABASE_URL 时）PostgreSQL 连接数峰值。
"""
import argparse
import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import statistics
import sys
import time

import httpx

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _percentile(values: list[float], ratio: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * ratio))))
    return round(ordered[index], 2)


def _summarize(samples: list[dict]) -> dict:
    ok = [item for item in samples if item["ok"]]
    latencies = [item["ms"] for item in ok]
    return {
        "count": len(samples),
        "errors": len(samples) - len(ok),
        "error_rate": round(100.0 * (len(samples) - len(ok)) / len(samples), 2) if samples else None,
        "mean_ms": round(statistics.fmean(latencies), 2) if latencies else None,
        "p50_ms": _percentile(latencies, 0.50),
        "p95_ms": _percentile(latencies, 0.95),
        "max_ms": round(max(latencies), 2) if latencies else None,
    }


async def _timed(client: httpx.AsyncClient, method: str, path: str, **kwargs) -> dict:
    started = time.perf_counter()
    try:
        response = await client.request(method, path, **kwargs)
        return {
            "ms": (time.perf_counter() - started) * 1000,
            "status": response.status_code,
            "ok": response.status_code < 500,
        }
    except httpx.HTTPError:
        return {"ms": (time.perf_counter() - started) * 1000, "status": None, "ok": False}


def _local_question() -> tuple[str | None, str | None]:
    """从只读题库取一道可作答题，供详情/提交压测。仅在项目目录内可用。"""
    try:
        sys.path.insert(0, str(PROJECT_ROOT))
        from app import practice

        with practice._bank() as con:
            ids = [row[0] for row in con.execute(
                "SELECT qid FROM questions WHERE objective=1 ORDER BY year DESC LIMIT 200"
            )]
        for qid in ids:
            question = practice.load_question(qid)
            if question["practice_allowed"] and question["has_answer"] and question["type"] == "single_choice":
                return qid, question["answer_text"]
    except Exception:
        return None, None
    return None, None


async def _sample_postgres_connections(stop: asyncio.Event, samples: list[int]):
    dsn = os.environ.get("DATABASE_URL", "").strip()
    if not dsn:
        return
    try:
        import psycopg
    except ImportError:
        return

    def _read() -> int | None:
        try:
            with psycopg.connect(dsn, connect_timeout=3) as con:
                row = con.execute(
                    "SELECT COUNT(*) FROM pg_stat_activity WHERE datname=current_database()"
                ).fetchone()
                return int(row[0]) if row else None
        except Exception:
            return None

    while not stop.is_set():
        value = await asyncio.to_thread(_read)
        if value is not None:
            samples.append(value)
        try:
            await asyncio.wait_for(stop.wait(), timeout=0.5)
        except asyncio.TimeoutError:
            continue


async def _sample_sse(client: httpx.AsyncClient, job_id: int, seconds: float) -> dict:
    intervals: list[float] = []
    events: list[str] = []
    last_at = time.perf_counter()
    try:
        async with client.stream("GET", f"/api/ai/jobs/{job_id}/stream", timeout=seconds + 10) as response:
            if response.status_code >= 400:
                return {"enabled": False, "error": f"SSE 返回 {response.status_code}"}
            deadline = time.perf_counter() + seconds
            async for line in response.aiter_lines():
                if time.perf_counter() >= deadline:
                    break
                if line.startswith("event:"):
                    event = line.split(":", 1)[1].strip()
                    now = time.perf_counter()
                    if events:
                        intervals.append((now - last_at) * 1000)
                    last_at = now
                    events.append(event)
    except (httpx.HTTPError, asyncio.TimeoutError):
        return {"enabled": True, "events": len(events), "interval_ms": _summarize_intervals(intervals)}
    return {
        "enabled": True,
        "events": len(events),
        "event_types": sorted(set(events)),
        "interval_ms": _summarize_intervals(intervals),
    }


def _summarize_intervals(intervals: list[float]) -> dict:
    return {
        "count": len(intervals),
        "p50_ms": _percentile(intervals, 0.50),
        "p95_ms": _percentile(intervals, 0.95),
        "max_ms": round(max(intervals), 2) if intervals else None,
    }


KINDS = ("catalog", "search", "detail", "submit", "ready")


async def _session(client: httpx.AsyncClient, qid: str | None, answer: str | None,
                   allow_submit: bool, rounds: int, samples: dict[str, list[dict]]):
    for _ in range(rounds):
        samples["catalog"].append(await _timed(client, "GET", "/api/catalog"))
        samples["search"].append(await _timed(client, "GET", "/api/questions?page=1&page_size=12"))
        samples["ready"].append(await _timed(client, "GET", "/api/health/ready"))
        if qid:
            samples["detail"].append(await _timed(client, "GET", f"/api/questions/{qid}"))
            if allow_submit and answer:
                samples["submit"].append(await _timed(
                    client, "POST", "/api/attempts",
                    data={"qid": qid, "answer": json.dumps(list(answer))},
                ))


async def main_async(args) -> dict:
    headers = {"X-Requested-With": "XMLHttpRequest"}
    async with httpx.AsyncClient(base_url=args.base_url.rstrip("/"), timeout=30, headers=headers) as client:
        if args.register:
            await client.post("/api/auth/register", json={
                "username": args.username, "password": args.password,
            })
        login = await client.post("/api/auth/login", json={
            "username": args.username, "password": args.password,
        })
        if login.status_code != 200:
            raise SystemExit(f"登录失败：{login.status_code} {login.text[:200]}")
        me = (await client.get("/api/auth/me")).json()
        ready = (await client.get("/api/health/ready")).json()

        qid, answer = args.qid, args.answer
        if not args.qid and not args.answer:
            auto_qid, auto_answer = _local_question()
            qid = qid or auto_qid
            answer = answer or auto_answer

        samples: dict[str, list[dict]] = {kind: [] for kind in KINDS}
        pg_samples: list[int] = []
        stop = asyncio.Event()
        sampler = asyncio.create_task(_sample_postgres_connections(stop, pg_samples))

        started = time.perf_counter()
        await asyncio.gather(*(
            _session(client, qid, answer, args.allow_submit, args.rounds, samples)
            for _ in range(args.sessions)
        ))
        duration = time.perf_counter() - started
        stop.set()
        await sampler

        sse = {"enabled": False}
        if args.job_id:
            sse = await _sample_sse(client, args.job_id, args.sse_seconds)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "base_url": args.base_url,
        "database": "postgresql" if os.environ.get("DATABASE_URL") else "sqlite",
        "bank_version": (ready.get("checks") or {}).get("bank", {}).get("bank_version"),
        "readiness": ready.get("status"),
        "concurrency": {
            "sessions": args.sessions,
            "rounds": args.rounds,
            "active_jobs": args.active_jobs,
            "duration_seconds": round(duration, 2),
        },
        "http": {kind: _summarize(items) for kind, items in samples.items() if items},
        "sse": sse,
        "postgres_connections": {
            "configured": bool(os.environ.get("DATABASE_URL")),
            "samples": len(pg_samples),
            "peak": max(pg_samples) if pg_samples else None,
        },
        "notice": "压测只用于容量基线；提交接口需显式 --allow-submit，避免污染正式账号学习记录。",
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description="知序 · 跨进程性能基线脚本")
    parser.add_argument("--base-url", default="http://127.0.0.1:8765")
    parser.add_argument("--username", required=True)
    parser.add_argument("--password", required=True)
    parser.add_argument("--register", action="store_true", help="账号不存在时先注册（仅本地/测试实例）")
    parser.add_argument("--sessions", type=int, default=20)
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--active-jobs", type=int, default=8, help="同时在跑的 AI 任务数（仅记录口径）")
    parser.add_argument("--qid")
    parser.add_argument("--answer")
    parser.add_argument("--allow-submit", action="store_true")
    parser.add_argument("--job-id", type=int)
    parser.add_argument("--sse-seconds", type=float, default=20)
    parser.add_argument("--out", type=Path, default=PROJECT_ROOT / "eval" / "baseline.json")
    args = parser.parse_args(argv)
    report = asyncio.run(main_async(args))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
