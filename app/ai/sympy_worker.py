"""Standalone SymPy comparison worker.

`verify._sympy_equivalent_detailed` 只负责受限解析并生成 srepr；真正的
`simplify(a - b)` 在这个独立子进程里执行，父进程用 hard timeout 强杀，避免
`9**9**9` 一类表达式拖死 FastAPI 的事件循环。

协议（一行一个 JSON）：
  * 一次性模式（默认）：stdin 读取整个 JSON {"a": srepr, "b": srepr}；stdout 输出一行：
      {"status": "equal"|"different"|"unverified"|"error", "reason": "..."}
  * 常驻模式（`--serve`）：import sympy 一次后进入 stdin/stdout 行循环，便于
    `sympy_pool` 复用进程、消除每次 300–600ms 的冷启动开销。就绪后先输出一行
    {"ready": true}；之后每条请求 {"a": srepr, "b": srepr, "id": 任意值}，
    输出一行 {"id": <回显>, "status": ..., "reason": ...}。坏行/坏请求返回 error
    后继续循环，不会被单条请求带崩；stdin 关闭时正常退出。

本文件刻意不 import app 内的任何模块：子进程只需要 SymPy，启动更快、也不受
应用初始化副作用影响。
"""
import json
import sys


def _compare(payload: dict) -> dict:
    import sympy as sp

    try:
        expr_a = sp.sympify(payload["a"])
        expr_b = sp.sympify(payload["b"])
    except Exception:  # noqa: BLE001 - 输入来自父进程的受限解析，异常一律视为解析失败
        return {"status": "error", "reason": "unparsed"}
    try:
        diff = sp.simplify(expr_a - expr_b)
    except Exception:  # noqa: BLE001 - 简化失败不等于一致，保持待核验
        return {"status": "error", "reason": "simplify_failed"}
    if diff == 0:
        return {"status": "equal"}
    if getattr(diff, "is_number", False):
        try:
            diff_value = complex(diff.evalf())
        except (TypeError, ValueError, OverflowError):
            return {"status": "unverified", "reason": "evalf_failed"}
        try:
            value_a = complex(expr_a.evalf())
            value_b = complex(expr_b.evalf())
            scale = max(abs(value_a), abs(value_b))
        except (TypeError, ValueError, OverflowError):
            # 符号式做差得到常数（如 (x+1)-(x+2)=-1）：只比较差值本身（绝对误差）。
            scale = 0.0
        # 非零值用纯相对误差，零值用绝对误差（与 verify._relative_equal 口径一致）。
        if scale > 0:
            ok = abs(diff_value) / scale <= 1e-6
        else:
            ok = abs(diff_value) <= 1e-6
        return {"status": "equal" if ok else "different"}
    return {"status": "different"}


def _handle_payload(payload) -> dict:
    """校验并比较一个请求对象；永不抛出（返回 error 状态）。"""
    if not isinstance(payload, dict) or not isinstance(payload.get("a"), str) \
            or not isinstance(payload.get("b"), str):
        return {"status": "error", "reason": "bad_request"}
    try:
        return _compare(payload)
    except MemoryError:
        return {"status": "error", "reason": "out_of_memory"}
    except Exception:  # noqa: BLE001 - 子进程绝不能让父进程拿到半截输出
        return {"status": "error", "reason": "worker_error"}


def _run_once() -> int:
    try:
        payload = json.load(sys.stdin)
    except (ValueError, OSError):
        payload = None
    print(json.dumps(_handle_payload(payload)))
    sys.stdout.flush()
    return 0


def _serve() -> int:
    """常驻模式：import sympy 一次，然后按行处理请求。"""
    import sympy  # noqa: F401 - 预热一次；后续 sympify/simplify 直接复用
    import time

    # 就绪握手：父进程据此区分「import sympy 中」和「请求超时」。
    print(json.dumps({"ready": True}), flush=True)
    started = time.monotonic()
    for raw in sys.stdin:
        line = raw.strip()
        if not line:
            continue
        req_id = None
        parsed = None
        try:
            parsed = json.loads(line)
        except (ValueError, TypeError):
            parsed = None
        if isinstance(parsed, dict):
            req_id = parsed.get("id")
        result = _handle_payload(parsed)
        if req_id is not None:
            result = {**result, "id": req_id}
        # 单条响应绝不能混入日志/半截输出：只写一行并立即 flush。
        print(json.dumps(result), flush=True)
        if time.monotonic() - started > 6 * 3600:
            # 极端长命进程主动退出，让池子在下次请求时重建（防御未知内存泄漏）。
            return 0
    return 0


def main() -> int:
    if "--serve" in sys.argv[1:]:
        return _serve()
    return _run_once()


if __name__ == "__main__":
    raise SystemExit(main())
