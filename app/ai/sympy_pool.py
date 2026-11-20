"""常驻 SymPy 子进程池：把「每次比对冷启 Python + import sympy」变成复用进程。

背景
----
`verify._sympy_equivalent_detailed` 只在主进程里做受限解析（`evaluate=False`），
真正的 `simplify(a - b)` 必须放进可强杀的子进程。旧实现每次比对都
`subprocess.run([python, sympy_worker.py])`，Windows 上冷启动 + import sympy
约 300–600ms，是交叉核验的主要延迟来源。

本模块维护 1–2 个常驻子进程：

* 懒启动：第一次比对时才 import sympy 并握手，进程启动失败就返回 None，
  调用方回退到一次性子进程，功能不降级；
* 请求级硬超时：每个请求单独计时，超时立即 kill 并回收（SymPy 卡死不能污染池）；
* 回收策略：子进程处理满 N 次请求（默认 200）主动重建，避免 SymPy 缓存无限增长；
  崩溃/协议错乱/超时一律销毁，下次请求自动重建；
* 并发安全：池内句柄由 Condition 分配，每个句柄同一时刻只服务一个线程；
  请求在子进程内串行执行，因此不会出现响应串包；
* atexit 关闭；`close()` 可重复调用。

协议见 `sympy_worker.py`（`--serve`：先输出 {"ready": true}，之后一行请求一行响应，
响应回显请求的 `id`，池子用 id 校验防止错位）。
"""
from __future__ import annotations

import atexit
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time

DEFAULT_MAX_WORKERS = 1
DEFAULT_MAX_REQUESTS = 200
# import sympy 通常 0.3–0.6s；慢机器/杀软扫描时可能更久，留出充足握手预算。
DEFAULT_SPAWN_TIMEOUT = 20.0
DEFAULT_CALL_TIMEOUT = 1200.0  # ms，仅作为未显式传入 timeout_ms 时的兜底

_WORKER_PATH = Path(__file__).with_name("sympy_worker.py")


def _env_int(name: str, default: int, low: int, high: int) -> int:
    try:
        value = int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default
    return max(low, min(high, value))


def _creation_flags() -> int:
    if os.name == "nt":
        return getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return 0


class SympyWorkerUnavailable(RuntimeError):
    """子进程无法启动/握手失败；调用方应回退到一次性实现。"""


class _PersistentWorker:
    """一个常驻 SymPy 子进程句柄。非线程安全：同一时刻只由一个调用方持有。"""

    def __init__(self, worker_path: Path, *, spawn_timeout: float = DEFAULT_SPAWN_TIMEOUT):
        self.worker_path = Path(worker_path)
        self.spawn_timeout = spawn_timeout
        self.proc: subprocess.Popen | None = None
        self.pid: int | None = None
        self.requests = 0
        self._responses: queue.Queue = queue.Queue()
        self._reader: threading.Thread | None = None
        self._spawn()

    # -- lifecycle ---------------------------------------------------------
    def _spawn(self) -> None:
        created = _creation_flags()
        try:
            proc = subprocess.Popen(
                [sys.executable, str(self.worker_path), "--serve"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                bufsize=1,
                creationflags=created,
            )
        except (OSError, ValueError) as exc:
            raise SympyWorkerUnavailable(f"spawn_failed:{exc}") from exc
        self.proc = proc
        self.pid = proc.pid
        self.requests = 0
        responses: queue.Queue = queue.Queue()
        self._responses = responses

        def _reader(pipe=proc.stdout, sink=responses) -> None:
            try:
                for line in pipe:
                    sink.put(line)
            except (OSError, ValueError):
                pass
            finally:
                sink.put(None)  # EOF 哨兵：子进程退出/被 kill

        reader = threading.Thread(
            target=_reader, daemon=True, name=f"sympy-worker-{proc.pid}",
        )
        reader.start()
        self._reader = reader
        # 就绪握手把「import sympy」与「请求超时」区分开：import 耗时不算进单次请求预算。
        line = self._read_line(self.spawn_timeout)
        if line is None:
            self._kill()
            raise SympyWorkerUnavailable("handshake_timeout")
        try:
            info = json.loads(line)
        except (ValueError, TypeError):
            info = None
        if not (isinstance(info, dict) and info.get("ready") is True):
            self._kill()
            raise SympyWorkerUnavailable("handshake_failed")

    def _read_line(self, timeout: float):
        try:
            line = self._responses.get(timeout=max(0.0, timeout))
        except queue.Empty:
            return None
        if line is None:
            return None
        return line.strip() or None

    @property
    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def _close_pipes(self) -> None:
        proc = self.proc
        if proc is None:
            return
        for stream in (proc.stdin, proc.stdout):
            try:
                if stream is not None:
                    stream.close()
            except (OSError, ValueError):
                pass

    def _kill(self) -> None:
        proc = self.proc
        self.proc = None
        if proc is None:
            return
        try:
            if proc.poll() is None:
                proc.kill()
                try:
                    proc.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    pass
        except (OSError, ValueError):
            pass
        self._close_pipes()

    def close(self) -> None:
        """主动回收：先温和关闭 stdin，再按需强杀，避免留下孤儿进程。"""
        proc = self.proc
        if proc is None:
            return
        try:
            if proc.stdin is not None and proc.poll() is None:
                proc.stdin.close()
                try:
                    proc.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    pass
        except (OSError, ValueError):
            pass
        self._kill()

    # -- protocol ----------------------------------------------------------
    def request(self, payload: dict, timeout_s: float, request_id: str):
        """发送一个请求；返回 (result_dict, failure_reason)。

        failure_reason 为 None 表示拿到了合法响应；否则为 timeout/crash/protocol。
        超时/崩溃/协议错乱后本句柄已被销毁，不能再复用。
        """
        proc = self.proc
        if proc is None or not self.alive:
            self._kill()
            return None, "crash"
        line = json.dumps({**payload, "id": request_id}, ensure_ascii=False)
        try:
            assert proc.stdin is not None
            proc.stdin.write(line + "\n")
            proc.stdin.flush()
        except (BrokenPipeError, OSError, ValueError):
            self._kill()
            return None, "crash"
        deadline = time.monotonic() + max(0.001, timeout_s)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._kill()
                return None, "timeout"
            try:
                raw = self._responses.get(timeout=remaining)
            except queue.Empty:
                self._kill()
                return None, "timeout"
            if raw is None:
                # EOF：子进程已退出（crash/被杀），句柄不可再用。
                self._kill()
                return None, "crash"
            raw = raw.strip()
            if not raw:
                continue
            try:
                result = json.loads(raw)
            except (ValueError, TypeError):
                self._kill()
                return None, "protocol"
            if not isinstance(result, dict) or result.get("id") != request_id:
                # 响应错位说明协议已脏，必须换新进程，绝不能把别人的结果当自己的。
                self._kill()
                return None, "protocol"
            self.requests += 1
            return result, None


class SympyPool:
    """1–2 个常驻 SymPy 子进程的懒启动池（线程安全）。"""

    def __init__(
        self,
        *,
        max_workers: int | None = None,
        max_requests: int | None = None,
        worker_path: Path | str | None = None,
        spawn_timeout: float = DEFAULT_SPAWN_TIMEOUT,
    ):
        if max_workers is None:
            max_workers = _env_int("STUDY_SYMPY_POOL_SIZE", DEFAULT_MAX_WORKERS, 1, 2)
        if max_requests is None:
            max_requests = _env_int(
                "STUDY_SYMPY_POOL_MAX_REQUESTS", DEFAULT_MAX_REQUESTS, 1, 100_000,
            )
        self.max_workers = max(1, min(2, int(max_workers)))
        self.max_requests = max(1, int(max_requests))
        self.worker_path = Path(worker_path) if worker_path is not None else _WORKER_PATH
        self.spawn_timeout = float(spawn_timeout)
        self._cond = threading.Condition()
        self._idle: list[_PersistentWorker] = []
        self._busy: set[_PersistentWorker] = set()
        self._live = 0
        self._closed = False
        self._stats = {
            "created_workers": 0,
            "recycled_workers": 0,
            "retired_after_requests": 0,
            "spawn_failures": 0,
            "requests": 0,
            "timeouts": 0,
            "crashes": 0,
            "protocol_errors": 0,
        }

    # -- account -----------------------------------------------------------
    def _forget(self, worker: _PersistentWorker) -> None:
        """销毁一个句柄并更新计数（调用方需持有 self._cond）。"""
        self._busy.discard(worker)
        if worker in self._idle:
            self._idle.remove(worker)
        if self._live > 0:
            self._live -= 1
        worker.close()

    def _checkout(self, timeout_s: float):
        """取一个可用句柄；拿不到返回 None（调用方回退一次性实现）。"""
        with self._cond:
            deadline = time.monotonic() + timeout_s
            while True:
                if self._closed:
                    return None
                while self._idle:
                    worker = self._idle.pop()
                    if worker.alive and worker.requests < self.max_requests:
                        self._busy.add(worker)
                        return worker
                    self._forget(worker)
                    self._stats["recycled_workers"] += 1
                if self._live < self.max_workers:
                    try:
                        worker = _PersistentWorker(
                            self.worker_path, spawn_timeout=self.spawn_timeout,
                        )
                    except SympyWorkerUnavailable:
                        self._stats["spawn_failures"] += 1
                        return None
                    self._live += 1
                    self._busy.add(worker)
                    self._stats["created_workers"] += 1
                    return worker
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._cond.wait(timeout=remaining)

    def _checkin(self, worker: _PersistentWorker, *, healthy: bool) -> None:
        with self._cond:
            self._busy.discard(worker)
            if (
                healthy
                and not self._closed
                and worker.alive
                and worker.requests < self.max_requests
            ):
                self._idle.append(worker)
                self._cond.notify()
                return
            if healthy:
                self._stats["retired_after_requests"] += 1
            self._forget(worker)
            self._cond.notify()

    # -- public ------------------------------------------------------------
    def compare(self, srepr_a: str, srepr_b: str, timeout_ms: int | float):
        """在常驻子进程里比较两个 srepr；返回 (verdict, reason) 或 None。

        返回 None 仅表示「池子本次无法提供服务」（启动失败/拿不到句柄），
        调用方应回退到一次性子进程；超时/崩溃/协议错误则返回
        (None, "timeout"|"engine_error" 等) 与旧实现口径一致，不再重复重计算。
        """
        if not isinstance(srepr_a, str) or not isinstance(srepr_b, str):
            return None, "engine_error"
        request_timeout = max(0.05, float(timeout_ms or DEFAULT_CALL_TIMEOUT) / 1000.0)
        # 排队等句柄的预算与计算预算分开：等待不计入请求级硬超时。
        checkout_timeout = max(5.0, request_timeout * 3)
        payload = {"a": srepr_a, "b": srepr_b}
        last_failure = "engine_error"
        for attempt in range(2):
            worker = self._checkout(checkout_timeout)
            if worker is None:
                return None
            request_id = f"{os.getpid()}-{id(worker)}-{attempt}-{time.monotonic_ns()}"
            result, failure = worker.request(payload, request_timeout, request_id)
            if failure is None:
                with self._cond:
                    self._stats["requests"] += 1
                self._checkin(worker, healthy=True)
                return self._map_result(result)
            if failure == "timeout":
                with self._cond:
                    self._stats["timeouts"] += 1
                self._checkin(worker, healthy=False)
                return None, "timeout"
            # 崩溃/协议错乱：换一个新进程重试一次（瞬时故障可自愈）。
            with self._cond:
                if failure == "protocol":
                    self._stats["protocol_errors"] += 1
                else:
                    self._stats["crashes"] += 1
            self._checkin(worker, healthy=False)
            last_failure = "engine_error"
            if attempt == 1:
                break
        return None, last_failure

    @staticmethod
    def _map_result(result: dict):
        status = result.get("status")
        if status == "equal":
            return True, "sympy"
        if status == "different":
            return False, "sympy"
        if status == "unverified":
            return None, str(result.get("reason") or "unverified")
        return None, str(result.get("reason") or "simplify_failed")

    def stats(self) -> dict:
        with self._cond:
            data = dict(self._stats)
            data.update({
                "max_workers": self.max_workers,
                "max_requests": self.max_requests,
                "live_workers": self._live,
                "idle_workers": len(self._idle),
                "busy_workers": len(self._busy),
                "worker_pids": sorted(
                    worker.pid for worker in (self._idle + list(self._busy))
                    if worker.pid is not None
                ),
                "closed": self._closed,
            })
            return data

    def close(self) -> None:
        with self._cond:
            if self._closed:
                return
            self._closed = True
            idle = list(self._idle)
            busy = list(self._busy)
            self._idle.clear()
            self._busy.clear()
            self._live = 0
            for worker in idle + busy:
                worker.close()
            self._cond.notify_all()


# ---------------------------------------------------------------------------
# 进程级单例
# ---------------------------------------------------------------------------

_pool: SympyPool | None = None
_pool_lock = threading.Lock()


def get_pool() -> SympyPool:
    """进程级共享池（懒创建；真正子进程要到第一次请求才启动）。"""
    global _pool
    if _pool is not None:
        return _pool
    with _pool_lock:
        if _pool is None:
            _pool = SympyPool()
            atexit.register(shutdown_pool)
    return _pool


def shutdown_pool() -> None:
    """关闭并丢弃单例（可重复调用；atexit 与测试共用）。"""
    global _pool
    with _pool_lock:
        pool = _pool
        _pool = None
    if pool is not None:
        try:
            pool.close()
        except Exception:  # noqa: BLE001 - 退出路径绝不抛异常
            pass
