"""P2「SymPy 常驻子进程池 + 数学文本标准化」离线测试。

不联网；子进程池用真实 SymPy（案例控制在 2 秒内）与可编程 fake worker 两条路径覆盖：
* 真实 worker：连续 3 次比对复用同一个子进程 PID；
* fake worker：超时回收、崩溃自愈、请求数上限回收、并发不串包。
"""
import textwrap
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.ai import sympy_pool, verify


# ---------------------------------------------------------------------------
# fake worker：实现 sympy_worker.py 的 --serve 行协议
# ---------------------------------------------------------------------------

_FAKE_WORKER = textwrap.dedent(
    """
    import json
    import os
    import sys
    import time

    def main():
        print(json.dumps({"ready": True}), flush=True)
        pid = os.getpid()
        for raw in sys.stdin:
            raw = raw.strip()
            if not raw:
                continue
            try:
                request = json.loads(raw)
            except ValueError:
                request = {}
            if not isinstance(request, dict):
                request = {}
            request_id = request.get("id")
            key_a = str(request.get("a"))
            key_b = str(request.get("b"))
            if key_a == "SLOW":
                time.sleep(30)
            if key_a == "CRASH":
                os._exit(9)
            result = {"status": "equal" if key_a == key_b else "different", "pid": pid}
            if request_id is not None:
                result["id"] = request_id
            print(json.dumps(result), flush=True)

    main()
    """
)

_FAKE_ONESHOT_WORKER = textwrap.dedent(
    """
    import json
    import sys

    payload = json.load(sys.stdin)
    print(json.dumps({"status": "equal" if payload.get("a") == payload.get("b") else "different"}))
    """
)


@pytest.fixture
def fake_worker(tmp_path):
    path = tmp_path / "fake_sympy_worker.py"
    path.write_text(_FAKE_WORKER, encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def _cleanup_pools():
    """每个用例后回收池句柄，避免常驻子进程跨用例泄漏/污染。"""
    yield
    try:
        verify._reset_sympy_pool()
    finally:
        sympy_pool.shutdown_pool()


# ---------------------------------------------------------------------------
# 1. 常驻池生命周期
# ---------------------------------------------------------------------------

def test_pool_reuses_same_worker_for_three_requests(fake_worker):
    pool = sympy_pool.SympyPool(worker_path=fake_worker, max_workers=1)
    pids = []
    for _ in range(3):
        verdict, reason = pool.compare("x", "x", 2000)
        assert (verdict, reason) == (True, "sympy")
        pids.append(pool.stats()["worker_pids"])
    stats = pool.stats()
    assert stats["created_workers"] == 1
    assert stats["requests"] == 3
    assert pids[0] == pids[1] == pids[2] and len(pids[0]) == 1
    pool.close()


def test_pool_timeout_recycles_worker_and_next_request_recovers(fake_worker):
    pool = sympy_pool.SympyPool(worker_path=fake_worker, max_workers=1)
    verdict, reason = pool.compare("SLOW", "x", 200)
    assert verdict is None and reason == "timeout"
    assert pool.stats()["timeouts"] == 1
    # 超时进程已被 kill；下一次请求必须自动重建并正常返回。
    verdict, reason = pool.compare("新鲜请求", "新鲜请求", 2000)
    assert (verdict, reason) == (True, "sympy")
    stats = pool.stats()
    assert stats["created_workers"] == 2
    assert stats["live_workers"] == 1
    pool.close()


def test_pool_recycles_after_max_requests(fake_worker):
    pool = sympy_pool.SympyPool(worker_path=fake_worker, max_workers=1, max_requests=2)
    for _ in range(3):
        assert pool.compare("x", "x", 2000) == (True, "sympy")
    stats = pool.stats()
    assert stats["requests"] == 3
    assert stats["created_workers"] == 2
    assert stats["retired_after_requests"] == 1
    pool.close()


def test_pool_restarts_after_worker_crash(fake_worker):
    pool = sympy_pool.SympyPool(worker_path=fake_worker, max_workers=1)
    verdict, reason = pool.compare("CRASH", "x", 2000)
    # 请求本身弄崩子进程：重试一次仍崩，返回引擎错误而不是把坏结果当结论。
    assert verdict is None and reason == "engine_error"
    assert pool.stats()["crashes"] == 2
    verdict, reason = pool.compare("y", "y", 2000)
    assert (verdict, reason) == (True, "sympy")
    assert pool.stats()["created_workers"] == 3
    pool.close()


def test_pool_concurrent_requests_do_not_cross_packets(fake_worker):
    pool = sympy_pool.SympyPool(worker_path=fake_worker, max_workers=2)

    def _call(index):
        if index % 2 == 0:
            return index, pool.compare(f"value-{index}", f"value-{index}", 5000)
        return index, pool.compare(f"value-{index}", f"other-{index}", 5000)

    with ThreadPoolExecutor(max_workers=8) as executor:
        outcomes = list(executor.map(_call, range(24)))

    assert len(outcomes) == 24
    for index, (verdict, reason) in outcomes:
        assert reason == "sympy"
        assert verdict is (index % 2 == 0)
    stats = pool.stats()
    assert stats["requests"] == 24
    assert stats["crashes"] == 0 and stats["protocol_errors"] == 0
    assert stats["created_workers"] <= 2
    pool.close()


def test_budget_rejection_does_not_start_pool(monkeypatch):
    monkeypatch.setattr(
        verify, "_get_sympy_pool",
        lambda: (_ for _ in ()).throw(AssertionError("pool 不应被启动")),
    )
    # 预算拒绝发生在池之前：绝不允许为一个被拒的表达式启动子进程。
    for bomb in ("9**9**9", "10**100000", "factorial(100000)"):
        result = verify.compare_answers(
            {"kind": "expression", "value": bomb}, {"kind": "expression", "value": "1"},
        )
        assert result["comparable"] is False
        assert result["agreed"] is None


def test_verify_falls_back_to_oneshot_when_pool_unavailable(tmp_path, monkeypatch):
    one_shot = tmp_path / "fake_oneshot_worker.py"
    one_shot.write_text(_FAKE_ONESHOT_WORKER, encoding="utf-8")
    monkeypatch.setattr(verify, "_SYMPY_WORKER", one_shot)
    monkeypatch.setattr(verify, "_get_sympy_pool", lambda: None)
    verdict, reason = verify._sympy_equivalent_detailed("x", "x")
    assert (verdict, reason) == (True, "sympy")


def test_verify_prefers_pool_compare(monkeypatch):
    calls = []

    class _FakePool:
        def compare(self, srepr_a, srepr_b, timeout_ms):
            calls.append((srepr_a, srepr_b, timeout_ms))
            return True, "sympy"

    monkeypatch.setattr(verify, "_get_sympy_pool", lambda: _FakePool())
    result = verify.compare_answers(
        {"kind": "expression", "value": "x + 1"}, {"kind": "expression", "value": "1 + x"},
    )
    assert result["agreed"] is True and result["method"] == "sympy"
    assert calls and "Symbol('x')" in calls[0][0] and calls[0][2] > 0


def test_real_pool_reuses_worker_across_three_compare_answers():
    """真实 SymPy：连续 3 次比对必须复用同一个常驻子进程（<2s）。"""
    verify._reset_sympy_pool()
    cases = [
        ("(x+1)^2", "x^2 + 2x + 1"),
        ("1/2", "0.5"),
        ("\\frac{\\sqrt{2}}{2}", "sqrt(2)/2"),
    ]
    started = time.perf_counter()
    pids = []
    results = []
    for left, right in cases:
        results.append(verify.compare_answers(
            {"kind": "expression", "value": left}, {"kind": "expression", "value": right},
        ))
        pids.append(verify._get_sympy_pool().stats()["worker_pids"])
    elapsed = time.perf_counter() - started
    assert all(result["agreed"] for result in results)
    assert pids[0] == pids[1] == pids[2] and len(pids[0]) == 1
    stats = verify._get_sympy_pool().stats()
    assert stats["created_workers"] == 1
    assert stats["requests"] == 3
    assert stats["crashes"] == 0 and stats["timeouts"] == 0
    assert elapsed < 2.0, f"常驻池未消除冷启动：3 次比对耗时 {elapsed:.2f}s"


def test_pool_env_disable_returns_none(monkeypatch):
    monkeypatch.setenv("STUDY_SYMPY_POOL", "0")
    verify._reset_sympy_pool()
    assert verify._get_sympy_pool() is None


# ---------------------------------------------------------------------------
# 2. 数学文本标准化：嵌套花括号分式
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("value,expected", [
    ("\\frac{\\sqrt{2}}{2}", "((sqrt(2))/(2))"),
    ("\\frac{a}{b+c}", "((a)/(b+c))"),
    ("\\frac{\\frac{1}{2}}{3}", "((((1)/(2)))/(3))"),
    ("\\sqrt{\\frac{2}{3}}", "sqrt(((2)/(3)))"),
    ("\\dfrac{1}{\\sqrt{3}}", "((1)/(sqrt(3)))"),
])
def test_normalize_math_text_handles_nested_braces(value, expected):
    assert verify._normalize_math_text(value) == expected


@pytest.mark.parametrize("left,right", [
    ("\\frac{\\sqrt{2}}{2}", "sqrt(2)/2"),
    ("\\frac{a}{b+c}", "a/(b+c)"),
    ("\\frac{\\frac{1}{2}}{3}", "1/6"),
    ("\\sqrt{\\frac{2}{3}}", "sqrt(2/3)"),
])
def test_compare_answers_nested_fraction_equivalence(left, right):
    result = verify.compare_answers(
        {"kind": "expression", "value": left}, {"kind": "expression", "value": right},
    )
    assert result["comparable"] is True
    assert result["agreed"] is True
    assert result["method"] == "sympy"


def test_legacy_simple_fraction_and_sqrt_still_work():
    assert verify._normalize_math_text("\\frac{1}{2}") == "((1)/(2))"
    assert verify._normalize_math_text("√2") == "sqrt(2)"
    assert verify.compare_answers(
        {"kind": "expression", "value": "\\frac{1}{2}"},
        {"kind": "expression", "value": "0.5"},
    )["agreed"] is True


# ---------------------------------------------------------------------------
# 3. 理化单位扩充（μ 前缀 / 能量功率压强 / eV / 温度 / 角度）
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("left,right", [
    ("2 μm", "2e-6 m"),
    ("5 μm", "5e-6 m"),
    ("5 μA", "5e-6 A"),
    ("2 μF", "2e-6 F"),
    ("3 μC", "3e-6 C"),
    ("3 kJ", "3000 J"),
    ("2 kW", "2000 W"),
    ("4 kPa", "4000 Pa"),
    ("2 MPa", "2e6 Pa"),
    ("1 eV", "1.602176634e-19 J"),
    ("25°C", "298.15 K"),
    ("25°C", "25 ℃"),
    ("90°", "1.5707963268 rad"),
    ("50%", "0.5"),
    ("1 km", "1000 m"),
    ("9.8 m/s^2", "9.8 N/kg"),
])
def test_units_convert_to_comparable_form(left, right):
    result = verify.compare_answers(
        {"kind": "number", "value": left}, {"kind": "number", "value": right},
    )
    assert result["comparable"] is True, (left, right, result.get("method"))
    assert result["agreed"] is True, (left, right, result.get("method"))
    assert result["method"] == "numeric_rel_1e-6"


def test_units_keep_dimension_semantics():
    mismatch = verify.compare_answers(
        {"kind": "number", "value": "2 m"}, {"kind": "number", "value": "2 s"},
    )
    assert mismatch["comparable"] is False and mismatch["reason"] == "unit_mismatch"
    # 摄氏度不能被当成无量纲数直接和 25 比较。
    missing = verify.compare_answers(
        {"kind": "number", "value": "25°C"}, {"kind": "number", "value": "25"},
    )
    assert missing["comparable"] is False and missing["reason"] == "unit_missing"
    # 仿射单位换算后温度不同必须判为不一致。
    different = verify.compare_answers(
        {"kind": "number", "value": "25°C"}, {"kind": "number", "value": "25 K"},
    )
    assert different["agreed"] is False
    # 未知单位仍然明确不可比，不能被静默剥离。
    unknown = verify.compare_answers(
        {"kind": "number", "value": "5 元"}, {"kind": "number", "value": "5 元"},
    )
    assert unknown["comparable"] is False and unknown["reason"] == "unit_unknown"
