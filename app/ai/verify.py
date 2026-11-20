"""Cross-Verification 2.0, programmatic/SymPy answer comparison, 6 explanation modes & rubric grading."""
import asyncio
import ast
from functools import lru_cache
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import threading

from fastapi import HTTPException
import yaml

from .. import content_refs, db, storage
from .adapters import _strip_json_fences
from .providers import _json, _owned

MAX_FILE = content_refs.MAX_IMAGE_BYTES
PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"


@lru_cache(maxsize=4)
def load_explain_modes() -> dict:
    data = yaml.safe_load((PROMPTS_DIR / "explain_modes.yaml").read_text(encoding="utf-8"))
    return data


def _file_path(name):
    return storage.local_path("ai-files", name)


# 图片读取与块级图片采集统一走 app.content_refs（与离线 eval 共用同一实现）。
_image_part = content_refs.image_part


def _bank_evidence(user_id, data):
    from .. import practice
    question = practice.load_question(data.qid)
    return content_refs.collect_bank_evidence(question)


# ---------------------------------------------------------------------------
# Programmatic Answer Comparator (Choice / Numeric with units / Budgeted SymPy)
#
# 审计口径（知序整体优化方案 P0）：
#   * JSON 数值零是有效答案，不能当作缺失；
#   * 数值必须核对完整输入、量纲、单位换算与允许误差，同形异量纲明确判为不可比；
#   * 符号表达式只在受限语法、长度与运算预算内比较，超限或无法核验一律降为待核验；
#   * 每条结论保留可复核轨迹（原卷参考、程序对照、模型 A/B 输出）。
# ---------------------------------------------------------------------------

def _verify_budget() -> dict:
    """Allowlists and budget are configurable so tests can pin each limit."""

    def _int_env(name, default):
        try:
            return max(1, int(os.environ.get(name, default)))
        except (TypeError, ValueError):
            return default

    extra_funcs = {
        name.strip()
        for name in os.environ.get("STUDY_VERIFY_ALLOWED_FUNCS", "").split(",")
        if name.strip()
    }
    return {
        "max_chars": _int_env("STUDY_VERIFY_MAX_CHARS", 240),
        "max_ops": _int_env("STUDY_VERIFY_MAX_OPS", 240),
        "timeout_ms": _int_env("STUDY_VERIFY_TIMEOUT_MS", 1200),
        # 解析前的 DoS 预算：幂指数、幂塔深度、阶乘参数与估算位宽都必须受限，
        # 否则 9**9**9 / 10**100000 / 100000! 会在 SymPy 解析或求值阶段直接拖死进程。
        "max_exponent": _int_env("STUDY_VERIFY_MAX_EXPONENT", 1000),
        "max_power_depth": _int_env("STUDY_VERIFY_MAX_POWER_DEPTH", 2),
        "max_factorial": _int_env("STUDY_VERIFY_MAX_FACTORIAL", 200),
        "max_bits": _int_env("STUDY_VERIFY_MAX_BITS", 200_000),
        "extra_funcs": extra_funcs,
    }


_ALLOWED_FUNCS = {
    "sqrt", "sin", "cos", "tan", "cot", "sec", "csc", "asin", "acos", "atan",
    "sinh", "cosh", "tanh", "log", "ln", "exp", "Abs", "abs", "factorial",
    "floor", "ceiling", "Max", "Min", "sign",
}
_ALLOWED_SYMBOLS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ") | {
    "alpha", "beta", "gamma", "delta", "epsilon", "zeta", "eta", "theta",
    "iota", "kappa", "lambda_", "mu", "nu", "xi", "rho", "sigma", "tau",
    "upsilon", "phi", "chi", "psi", "omega",
    "pi", "E", "e", "i", "oo", "zoo", "nan",
}
_ALLOWED_CONSTANTS = {"pi": "pi", "E": "E", "e": "E", "i": "I", "oo": "oo"}
_REJECTED_CHARS = re.compile(r"[;=\[\]{}'\"`@#$%&|<>!~?]")
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z_0-9]*")
_SAFE_CHARS = re.compile(r"^[0-9A-Za-z_+\-*/^().,\s]*$")


class _ExpressionRejected(ValueError):
    """Raised when an expression falls outside the approved grammar or budget."""


def _expression_functions(extra: set[str]) -> dict:
    import sympy as sp

    funcs = {
        "sqrt": sp.sqrt, "sin": sp.sin, "cos": sp.cos, "tan": sp.tan,
        "cot": lambda x: 1 / sp.tan(x), "sec": lambda x: 1 / sp.cos(x),
        "csc": lambda x: 1 / sp.sin(x), "asin": sp.asin, "acos": sp.acos,
        "atan": sp.atan, "sinh": sp.sinh, "cosh": sp.cosh, "tanh": sp.tanh,
        "log": sp.log, "ln": sp.log, "exp": sp.exp, "Abs": sp.Abs, "abs": sp.Abs,
        "factorial": sp.factorial, "floor": sp.floor, "ceiling": sp.ceiling,
        "Max": sp.Max, "Min": sp.Min, "sign": sp.sign,
    }
    for name in extra:
        funcs[name] = getattr(sp, name, None) or funcs.get(name)
    return {name: fn for name, fn in funcs.items() if fn is not None}


_SUPERSCRIPT = {
    "⁰": "0", "¹": "1", "²": "2", "³": "3", "⁴": "4", "⁵": "5",
    "⁶": "6", "⁷": "7", "⁸": "8", "⁹": "9", "⁻": "-", "⁺": "+",
}


def _balanced_brace(text: str, start: int):
    """读取 `text[start] == '{'` 起始的平衡花括号组，返回 (内容, 右括号后下标)。

    失败（未闭合/起点不是左花括号）返回 (None, start)。嵌套花括号按深度配对，
    因此 `\\frac{\\sqrt{2}}{2}` 这类分子/分母本身带花括号的写法也能被正确切分。
    """
    if start >= len(text) or text[start] != "{":
        return None, start
    depth = 0
    for index in range(start, len(text)):
        char = text[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start + 1:index], index + 1
    return None, start


_FRAC_COMMAND = re.compile(r"\\(?:dfrac|tfrac|frac)\s*\{")
_SQRT_COMMAND = re.compile(r"(?:\\sqrt|√)\s*\{")


def _rewrite_braced_fracs(text: str):
    """把 `\\frac{A}{B}`（含嵌套花括号）改写为 `((A)/(B))`。返回 (新串, 是否改写)。"""
    out: list[str] = []
    index = 0
    changed = False
    while index < len(text):
        match = _FRAC_COMMAND.match(text, index)
        if not match:
            out.append(text[index])
            index += 1
            continue
        numerator, after_num = _balanced_brace(text, match.end() - 1)
        cursor = after_num
        while cursor < len(text) and text[cursor].isspace():
            cursor += 1
        denominator, after_den = _balanced_brace(text, cursor) if cursor < len(text) else (None, cursor)
        if numerator is None or denominator is None:
            out.append(text[index])
            index += 1
            continue
        out.append(f"(({numerator})/({denominator}))")
        index = after_den
        changed = True
    return "".join(out), changed


def _rewrite_braced_sqrts(text: str):
    """把 `\\sqrt{A}` / `√{A}`（含嵌套花括号）改写为 `sqrt(A)`。"""
    out: list[str] = []
    index = 0
    changed = False
    while index < len(text):
        match = _SQRT_COMMAND.match(text, index)
        if not match:
            out.append(text[index])
            index += 1
            continue
        inner, after = _balanced_brace(text, match.end() - 1)
        if inner is None:
            out.append(text[index])
            index += 1
            continue
        out.append(f"sqrt({inner})")
        index = after
        changed = True
    return "".join(out), changed


def _rewrite_nested_latex(text: str, rounds: int = 24) -> str:
    """由内向外反复改写嵌套分式/根式，直到不再变化（防御病态深嵌套）。"""
    for _ in range(rounds):
        text, frac_changed = _rewrite_braced_fracs(text)
        text, sqrt_changed = _rewrite_braced_sqrts(text)
        if not (frac_changed or sqrt_changed):
            return text
    return text


def _normalize_math_text(value: str) -> str:
    s = str(value or "").strip()
    s = s.strip("$").strip()
    s = "".join(_SUPERSCRIPT.get(ch, ch) for ch in s)
    s = (
        s.replace("×", "*").replace("·", "*").replace("⋅", "*").replace("∙", "*")
        .replace("÷", "/").replace("−", "-").replace("–", "-").replace("—", "-")
        .replace("π", "pi").replace("≤", "<=").replace("≥", ">=")
    )
    # 分式/根式先做平衡花括号级改写：\frac{\sqrt{2}}{2}、\frac{a}{b+c}、
    # \sqrt{\frac{2}{3}} 这类嵌套结构不能再依赖 `[^{}]+` 的扁平正则。
    s = _rewrite_nested_latex(s)
    # 根号：√{x} / √(x+1) / √2 -> sqrt(...)
    s = re.sub(r"√\s*\{([^{}]+)\}", r"sqrt(\1)", s)
    s = re.sub(r"√\s*\(([^()]+)\)", r"sqrt(\1)", s)
    s = re.sub(r"√\s*([0-9A-Za-z]+)", r"sqrt(\1)", s)
    s = s.replace("√", "sqrt")
    s = s.replace("\\cdot", "*").replace("\\times", "*").replace("\\div", "/")
    s = s.replace("\\pi", "pi").replace("\\left", "").replace("\\right", "")
    s = re.sub(r"\\(?:displaystyle|limits|,|;|:|!|\s+)", "", s)
    # 下标在表达式里作为符号名的一部分：v_{1} -> v1
    s = re.sub(r"_\{([^{}]*)\}", r"\1", s)
    s = re.sub(r"_([0-9]+)", r"\1", s)
    # 幂次：^ -> **（避免破坏已是 ** 的写法）
    s = re.sub(r"(?<!\*)\^(?!\*)", "**", s)
    return s.strip()


def _validate_expression(clean: str, budget: dict) -> None:
    if not clean:
        raise _ExpressionRejected("empty")
    if len(clean) > budget["max_chars"]:
        raise _ExpressionRejected("too_long")
    if _REJECTED_CHARS.search(clean) or not _SAFE_CHARS.match(clean.replace("**", "*")):
        raise _ExpressionRejected("unsupported_syntax")
    allowed_funcs = _ALLOWED_FUNCS | budget["extra_funcs"]
    for name in _IDENTIFIER.findall(clean):
        if name in allowed_funcs or name in _ALLOWED_SYMBOLS:
            continue
        raise _ExpressionRejected(f"unapproved_symbol:{name}")


def _thread_call(fn, timeout_ms: int):
    """Run `fn` with a wall-clock budget. Returns (status, value) with status ok/timeout/error.

    Kept for lightweight, non-SymPy callers/tests：Python 线程超时后无法真正杀死计算，
    因此 SymPy 的 simplify 比对必须走下面的可强杀子进程，不能依赖线程超时。
    """
    result: dict = {}

    def _target():
        try:
            result["value"] = fn()
        except BaseException as exc:  # noqa: BLE001 - reported as an unverified verdict
            result["error"] = exc

    worker = threading.Thread(target=_target, daemon=True)
    worker.start()
    worker.join(timeout_ms / 1000.0)
    if worker.is_alive():
        return "timeout", None
    if "error" in result:
        return "error", result["error"]
    return "ok", result.get("value")


# --- Parse-time DoS guards ----------------------------------------------------
#
# `parse_expr(..., evaluate=False)` 只构造语法树，但语法树本身也可能是个炸弹：
# 9**9**9、10**100000、factorial(100000) 一旦进入 SymPy 求值就会吃光内存/CPU。
# 所以在交给 SymPy 之前先做纯 AST/字符串级预算检查，超限直接判为「不可比」。
# ---------------------------------------------------------------------------

def _const_number(node):
    """Best-effort bounded evaluation of a literal-only AST subtree (None if unknown)."""

    if isinstance(node, ast.Constant):
        value = node.value
        if isinstance(value, bool) or not isinstance(value, int):
            return None
        if abs(value) > 10**12:
            return None
        return value
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        inner = _const_number(node.operand)
        if inner is None:
            return None
        return -inner if isinstance(node.op, ast.USub) else inner
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Sub, ast.Mult, ast.Pow)):
        left, right = _const_number(node.left), _const_number(node.right)
        if left is None or right is None:
            return None
        try:
            if isinstance(node.op, ast.Add):
                value = left + right
            elif isinstance(node.op, ast.Sub):
                value = left - right
            elif isinstance(node.op, ast.Mult):
                value = left * right
            else:
                if right < 0 or right > 64 or abs(left) > 10**6:
                    return None
                value = left**right
        except (ArithmeticError, ValueError):
            return None
        return value if abs(value) <= 10**12 else None
    return None


def _pow_tower_depth(node) -> int:
    if not (isinstance(node, ast.BinOp) and isinstance(node.op, ast.Pow)):
        return 0
    return 1 + max(_pow_tower_depth(node.left), _pow_tower_depth(node.right))


_SATURATED = 10**18


def _numeric_bound(node):
    """估算字面量表达式数量级的浮点上界（饱和到 1e18），用于解析前的幂/阶乘预算。

    与 `_const_number` 不同，它不要求结果可精确表示：`2**(2**1000)` 这种
    「内层指数本身已经巨大」的幂塔必须在这里被判出来，否则 parse_expr 仍会
    真的去算这个天文数字。
    """
    if isinstance(node, ast.Constant):
        value = node.value
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        return min(abs(float(value)), float(_SATURATED))
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        return _numeric_bound(node.operand)
    if isinstance(node, ast.BinOp):
        left, right = _numeric_bound(node.left), _numeric_bound(node.right)
        if isinstance(node.op, (ast.Add, ast.Sub)):
            values = [value for value in (left, right) if value is not None]
            return min(max(values) + 1.0, float(_SATURATED)) if values else None
        if left is None or right is None:
            return None
        if isinstance(node.op, ast.Mult):
            return min(left * right, float(_SATURATED))
        if isinstance(node.op, ast.Pow):
            if left <= 1.0:
                return 1.0
            if right > 64.0:
                return float(_SATURATED)
            try:
                return min(left ** right, float(_SATURATED))
            except (OverflowError, ValueError):
                return float(_SATURATED)
        return None
    return None


def _log2_bound(node):
    """估算 |表达式| 的 log2 上界；符号表达式返回 None（无法造成数值爆炸）。"""
    constant = _const_number(node)
    if constant is not None:
        return 0.0 if constant == 0 else math.log2(abs(constant))
    numeric = _numeric_bound(node)
    if numeric is not None:
        return 0.0 if numeric == 0 else math.log2(numeric)
    if isinstance(node, ast.UnaryOp):
        return _log2_bound(node.operand)
    if isinstance(node, ast.BinOp):
        left, right = _log2_bound(node.left), _log2_bound(node.right)
        if isinstance(node.op, (ast.Add, ast.Sub)):
            known = [value for value in (left, right) if value is not None]
            return (max(known) + 1.0) if known else None
        if left is None or right is None:
            return None
        if isinstance(node.op, ast.Mult):
            return left + right
        if isinstance(node.op, ast.Pow):
            exponent = _numeric_bound(node.right)
            if exponent is None:
                return None
            return left * exponent if left > 0 else None
    return None


def _estimate_power_bits(base, exponent) -> int | None:
    if exponent == 0:
        return 1
    if abs(base) <= 1:
        return 1
    try:
        return int(abs(exponent) * math.log2(abs(base))) + 1
    except (ValueError, OverflowError):
        return None


def _check_power_node(node, budget):
    if _pow_tower_depth(node) > budget["max_power_depth"]:
        raise _ExpressionRejected("power_tower_too_deep")
    exponent = _numeric_bound(node.right)
    if exponent is not None and exponent > budget["max_exponent"]:
        raise _ExpressionRejected("exponent_too_large")
    base_log2 = _log2_bound(node.left)
    if exponent is not None and base_log2 is not None:
        if exponent * max(base_log2, 0.0) > budget["max_bits"]:
            raise _ExpressionRejected("power_too_large")


def _ast_precheck(tree, budget):
    nodes = 0
    stack = [tree.body]
    while stack:
        node = stack.pop()
        if isinstance(node, ast.BinOp):
            nodes += 1
            if isinstance(node.op, ast.Pow):
                _check_power_node(node, budget)
            stack.append(node.left)
            stack.append(node.right)
        elif isinstance(node, ast.UnaryOp):
            nodes += 1
            stack.append(node.operand)
        elif isinstance(node, ast.Call):
            nodes += 1
            name = node.func.id if isinstance(node.func, ast.Name) else ""
            if name in ("factorial", "fact"):
                # factorial 在 parse_expr 阶段就会真实求值，参数必须是有界字面量；
                # 否则 factorial(factorial(9)) 会先算出 362880!，直接绕过所有预算。
                argument = _const_number(node.args[0]) if node.args else None
                if argument is None:
                    raise _ExpressionRejected("factorial_argument_unbounded")
                if argument < 0 or argument > budget["max_factorial"]:
                    raise _ExpressionRejected("factorial_too_large")
            stack.extend(node.args)
        elif isinstance(node, (ast.Name, ast.Constant)):
            continue
        elif isinstance(node, ast.Load):
            continue
        else:
            # 比较、条件表达式、属性访问、lambda 等都不在受限语法内。
            raise _ExpressionRejected("unsupported_syntax")
    if nodes > budget["max_ops"]:
        raise _ExpressionRejected("too_complex")


def _string_precheck(clean, budget):
    """Fallback for implicit multiplication (`2x`) where the text is not valid Python syntax."""

    if clean.count("**") > budget["max_power_depth"] and re.search(
        r"\d\s*\*\*\s*\d\s*\*\*\s*\d", clean
    ):
        raise _ExpressionRejected("power_tower_too_deep")
    for match in re.finditer(r"(?<![\w.])(\d+(?:\.\d+)?)\s*\*\*\s*([+-]?\d+)", clean):
        exponent = int(match.group(2))
        if abs(exponent) > budget["max_exponent"]:
            raise _ExpressionRejected("exponent_too_large")
        bits = _estimate_power_bits(float(match.group(1)), exponent)
        if bits is not None and bits > budget["max_bits"]:
            raise _ExpressionRejected("power_too_large")
    for match in re.finditer(r"factorial\s*\(\s*([+-]?\d+)", clean):
        argument = int(match.group(1))
        if argument < 0 or argument > budget["max_factorial"]:
            raise _ExpressionRejected("factorial_too_large")
    if len(re.findall(r"\*\*|[+\-*/^]", clean)) > budget["max_ops"]:
        raise _ExpressionRejected("too_complex")


def _precheck_expression(clean: str, budget: dict) -> None:
    try:
        tree = ast.parse(clean, mode="eval")
    except (SyntaxError, ValueError, MemoryError, RecursionError):
        _string_precheck(clean, budget)
        return
    _ast_precheck(tree, budget)


_SYMPY_WORKER = Path(__file__).with_name("sympy_worker.py")

# 常驻池句柄缓存在模块级：首个 SymPy 比对才真正启动子进程（懒启动）。
_SYMPY_POOL = None
_SYMPY_POOL_DISABLED = False
_SYMPY_POOL_LOCK = threading.Lock()


def _get_sympy_pool():
    """返回进程级常驻池；池不可用（导入/构造失败/显式禁用）时返回 None 让它回退一次性实现。"""
    global _SYMPY_POOL, _SYMPY_POOL_DISABLED
    if str(os.environ.get("STUDY_SYMPY_POOL", "")).strip().lower() in {
        "0", "false", "off", "no", "disabled",
    }:
        return None
    if _SYMPY_POOL is not None:
        return _SYMPY_POOL
    if _SYMPY_POOL_DISABLED:
        return None
    with _SYMPY_POOL_LOCK:
        if _SYMPY_POOL is None and not _SYMPY_POOL_DISABLED:
            try:
                from .sympy_pool import get_pool

                _SYMPY_POOL = get_pool()
            except Exception:  # noqa: BLE001 - 池不可用必须静默回退，不能影响核验
                _SYMPY_POOL_DISABLED = True
                _SYMPY_POOL = None
    return _SYMPY_POOL


def _reset_sympy_pool() -> None:
    """关闭并清空模块级池句柄（测试与热更新用；生产无需调用）。"""
    global _SYMPY_POOL, _SYMPY_POOL_DISABLED
    with _SYMPY_POOL_LOCK:
        pool = _SYMPY_POOL
        _SYMPY_POOL = None
        _SYMPY_POOL_DISABLED = False
    if pool is not None:
        try:
            pool.close()
        except Exception:  # noqa: BLE001 - 清理路径绝不抛异常
            pass


def _sympy_compare_with_pool(srepr_a: str, srepr_b: str, timeout_ms: int):
    """优先走常驻池；池不可用/拿不到句柄时回退一次性子进程（行为完全不变）。"""
    pool = _get_sympy_pool()
    if pool is not None:
        try:
            result = pool.compare(srepr_a, srepr_b, timeout_ms)
        except Exception:  # noqa: BLE001 - 池故障一律降级到一次性实现
            result = None
        if result is not None:
            return result
    return _sympy_compare_in_subprocess(srepr_a, srepr_b, timeout_ms)


def _sympy_compare_in_subprocess(srepr_a: str, srepr_b: str, timeout_ms: int):
    """在可强杀的子进程里做 simplify 比对，硬超时即 kill；返回 (verdict, reason)。"""
    payload = json.dumps({"a": srepr_a, "b": srepr_b})
    timeout = max(0.05, timeout_ms / 1000.0)
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    try:
        completed = subprocess.run(
            [sys.executable, str(_SYMPY_WORKER)],
            input=payload, capture_output=True, text=True, timeout=timeout,
            creationflags=creationflags, check=False,
        )
    except subprocess.TimeoutExpired:
        return None, "timeout"
    except (OSError, ValueError):
        return None, "engine_unavailable"
    if completed.returncode != 0:
        return None, "engine_error"
    lines = [line for line in (completed.stdout or "").splitlines() if line.strip()]
    if not lines:
        return None, "engine_error"
    try:
        result = json.loads(lines[-1])
    except ValueError:
        return None, "engine_error"
    status = result.get("status") if isinstance(result, dict) else None
    if status == "equal":
        return True, "sympy"
    if status == "different":
        return False, "sympy"
    if status == "unverified":
        return None, "unverified"
    return None, str((result or {}).get("reason") or "simplify_failed")


def _parse_expression(value: str, budget: dict):
    import sympy as sp
    from sympy.parsing.sympy_parser import (
        implicit_multiplication_application,
        parse_expr,
        standard_transformations,
    )

    clean = _normalize_math_text(value)
    _validate_expression(clean, budget)
    _precheck_expression(clean, budget)
    functions = _expression_functions(budget["extra_funcs"])
    # The parser transformations emit names such as Integer/Float/Rational; expose only those
    # constructors plus the approved functions and constants.
    global_dict = {
        name: getattr(sp, name)
        for name in ("Symbol", "Integer", "Float", "Rational", "Mul", "Add", "Pow")
    }
    global_dict.update(functions)
    for name, symbol in _ALLOWED_CONSTANTS.items():
        global_dict[name] = getattr(sp, symbol)
    transformations = standard_transformations + (implicit_multiplication_application,)
    # evaluate=False：先只建语法树，绝不在主进程/事件循环里求值。
    expr = parse_expr(
        clean, global_dict=global_dict, local_dict={},
        transformations=transformations, evaluate=False,
    )
    if sp.count_ops(expr) > budget["max_ops"]:
        raise _ExpressionRejected("too_complex")
    return sp, expr


def _sympy_equivalent_detailed(a: str, b: str) -> tuple[bool | None, str]:
    """Return (verdict, reason). `None` means "not programmatically verified"（待核验）。"""
    budget = _verify_budget()
    try:
        sp, ea = _parse_expression(a, budget)
        _, eb = _parse_expression(b, budget)
    except _ExpressionRejected as exc:
        return None, f"rejected:{exc}"
    except (SyntaxError, TypeError, ValueError, AttributeError, RecursionError):
        return None, "unparsed"
    try:
        srepr_a, srepr_b = sp.srepr(ea), sp.srepr(eb)
    except (TypeError, ValueError, AttributeError):
        return None, "unparsed"
    # simplify/求值一并放进子进程：主进程只负责受限解析，永远不会被 simplify 卡死。
    # 常驻池复用已 import sympy 的子进程（消除每轮 300–600ms 冷启动），池不可用时
    # 自动回退到一次性子进程；解析前预算与 evaluate=False 预检都保持不变。
    return _sympy_compare_with_pool(srepr_a, srepr_b, budget["timeout_ms"])


def _sympy_equivalent(a: str, b: str) -> bool | None:
    verdict, _ = _sympy_equivalent_detailed(a, b)
    return verdict


# --- Numeric values and units -------------------------------------------------

_NUMBER_TOKEN = re.compile(
    r"^\s*([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:\s*/\s*\d+(?:\.\d*)?)?(?:\s*[eE]\s*[+-]?\d+)?)"
)
_UNKNOWN_UNIT = "__unknown_unit__"
_UNIT_ALIASES = {
    "米": "m", "厘米": "cm", "毫米": "mm", "分米": "dm", "千米": "km", "公里": "km",
    "微米": "um", "纳米": "nm",
    "秒": "s", "毫秒": "ms", "分钟": "min", "分": "min", "小时": "h", "时": "h", "天": "d",
    "克": "g", "千克": "kg", "公斤": "kg", "毫克": "mg", "吨": "t",
    "安": "A", "安培": "A", "毫安": "mA", "微安": "uA",
    "开": "K", "开尔文": "K", "摩尔": "mol", "坎": "cd",
    "牛": "N", "牛顿": "N", "焦": "J", "焦耳": "J", "瓦": "W", "瓦特": "W",
    "千焦": "kJ", "千焦耳": "kJ", "千瓦": "kW", "千瓦特": "kW",
    "帕": "Pa", "帕斯卡": "Pa", "千帕": "kPa", "兆帕": "MPa", "赫兹": "Hz", "赫": "Hz",
    "伏": "V", "伏特": "V", "微伏": "uV", "库": "C", "库仑": "C", "微库": "uC",
    "欧姆": "Ω", "欧": "Ω",
    "法": "F", "法拉": "F", "微法": "uF", "韦伯": "Wb", "特斯拉": "T", "亨": "H", "亨利": "H",
    "升": "L", "毫升": "mL",
    "℃": "°C", "摄氏度": "°C", "电子伏": "eV", "电子伏特": "eV",
    "弧度": "rad",
}
_DIMENSIONLESS = (0, 0, 0, 0, 0, 0, 0)
# (长度, 质量, 时间, 电流, 温度, 物质的量, 发光强度) + SI 换算系数
_UNIT_BASE: dict[str, tuple[tuple[int, ...], float]] = {
    "m": ((1, 0, 0, 0, 0, 0, 0), 1.0),
    "km": ((1, 0, 0, 0, 0, 0, 0), 1000.0),
    "dm": ((1, 0, 0, 0, 0, 0, 0), 0.1),
    "cm": ((1, 0, 0, 0, 0, 0, 0), 0.01),
    "mm": ((1, 0, 0, 0, 0, 0, 0), 0.001),
    "um": ((1, 0, 0, 0, 0, 0, 0), 1e-6),
    "nm": ((1, 0, 0, 0, 0, 0, 0), 1e-9),
    "kg": ((0, 1, 0, 0, 0, 0, 0), 1.0),
    "g": ((0, 1, 0, 0, 0, 0, 0), 0.001),
    "mg": ((0, 1, 0, 0, 0, 0, 0), 1e-6),
    "t": ((0, 1, 0, 0, 0, 0, 0), 1000.0),
    "s": ((0, 0, 1, 0, 0, 0, 0), 1.0),
    "ms": ((0, 0, 1, 0, 0, 0, 0), 0.001),
    "us": ((0, 0, 1, 0, 0, 0, 0), 1e-6),
    "min": ((0, 0, 1, 0, 0, 0, 0), 60.0),
    "h": ((0, 0, 1, 0, 0, 0, 0), 3600.0),
    "d": ((0, 0, 1, 0, 0, 0, 0), 86400.0),
    "A": ((0, 0, 0, 1, 0, 0, 0), 1.0),
    "mA": ((0, 0, 0, 1, 0, 0, 0), 0.001),
    "uA": ((0, 0, 0, 1, 0, 0, 0), 1e-6),
    "K": ((0, 0, 0, 0, 1, 0, 0), 1.0),
    # 摄氏度是仿射单位：base = number * 1 + 273.15（见 _UNIT_OFFSETS）。
    "°C": ((0, 0, 0, 0, 1, 0, 0), 1.0),
    "mol": ((0, 0, 0, 0, 0, 1, 0), 1.0),
    "cd": ((0, 0, 0, 0, 0, 0, 1), 1.0),
    "N": ((1, 1, -2, 0, 0, 0, 0), 1.0),
    "J": ((2, 1, -2, 0, 0, 0, 0), 1.0),
    "kJ": ((2, 1, -2, 0, 0, 0, 0), 1000.0),
    # 1 eV = 1.602176634e-19 J（2019 SI 定义值）
    "eV": ((2, 1, -2, 0, 0, 0, 0), 1.602176634e-19),
    "W": ((2, 1, -3, 0, 0, 0, 0), 1.0),
    "kW": ((2, 1, -3, 0, 0, 0, 0), 1000.0),
    "Pa": ((-1, 1, -2, 0, 0, 0, 0), 1.0),
    "kPa": ((-1, 1, -2, 0, 0, 0, 0), 1000.0),
    "MPa": ((-1, 1, -2, 0, 0, 0, 0), 1e6),
    "Hz": ((0, 0, -1, 0, 0, 0, 0), 1.0),
    "V": ((2, 1, -3, -1, 0, 0, 0), 1.0),
    "uV": ((2, 1, -3, -1, 0, 0, 0), 1e-6),
    "C": ((0, 0, 1, 1, 0, 0, 0), 1.0),
    "uC": ((0, 0, 1, 1, 0, 0, 0), 1e-6),
    "Ω": ((2, 1, -3, -2, 0, 0, 0), 1.0),
    "F": ((-2, -1, 4, 2, 0, 0, 0), 1.0),
    "uF": ((-2, -1, 4, 2, 0, 0, 0), 1e-6),
    "Wb": ((2, 1, -2, -1, 0, 0, 0), 1.0),
    "T": ((0, 1, -2, -1, 0, 0, 0), 1.0),
    "H": ((2, 1, -2, -2, 0, 0, 0), 1.0),
    "L": ((3, 0, 0, 0, 0, 0, 0), 0.001),
    "mL": ((3, 0, 0, 0, 0, 0, 0), 1e-6),
    # 角度是无量纲量：rad 基准，° 为 π/180 rad。
    "rad": (_DIMENSIONLESS, 1.0),
    "°": (_DIMENSIONLESS, math.pi / 180.0),
}
# 仿射单位：base = number * factor + offset（仅对单一单位答案生效）。
_UNIT_OFFSETS = {"°C": 273.15}


def _parse_unit(unit: str):
    """Parse a unit expression into (dimension vector, factor, offset).

    `offset` 用于摄氏度这类仿射单位；普通单位偏移为 0。None 表示未知单位。
    """
    text = str(unit or "").strip()
    if not text:
        return None
    text = "".join(_SUPERSCRIPT.get(ch, ch) for ch in text)
    # 微前缀统一成 ASCII u：μm/µm -> um、μA -> uA、μF -> uF、μC -> uC。
    text = text.replace("µ", "μ").replace("μ", "u")
    text = text.replace("·", "*").replace("⋅", "*").replace("×", "*").replace("／", "/")
    text = text.replace("**", "^")
    # 中文单位别名（可能紧跟数字，如 "3米"）
    for alias in sorted(_UNIT_ALIASES, key=len, reverse=True):
        text = text.replace(alias, _UNIT_ALIASES[alias])
    text = re.sub(r"\s+", "", text)
    if not text:
        return None
    if text == "%":
        return _DIMENSIONLESS, 0.01, 0.0
    if text in _UNIT_OFFSETS:
        vector, base_factor = _UNIT_BASE[text]
        return vector, base_factor, _UNIT_OFFSETS[text]
    dimension = [0] * 7
    factor = 1.0
    parts = re.split(r"([*/])", text)
    sign = 1
    for part in parts:
        if part in ("*", "/"):
            sign = 1 if part == "*" else -1
            continue
        if not part:
            continue
        match = re.fullmatch(r"([A-Za-zΩ°]+)\^?\(?([+-]?\d+)\)?", part) or re.fullmatch(r"([A-Za-zΩ°]+)", part)
        if not match:
            return None
        name, power = match.group(1), int(match.group(2)) if match.lastindex == 2 else 1
        base = _UNIT_BASE.get(name)
        if base is None:
            return None
        vector, base_factor = base
        exponent = sign * power
        for index, value in enumerate(vector):
            dimension[index] += value * exponent
        try:
            factor *= base_factor ** exponent
        except (OverflowError, ZeroDivisionError):
            return None
    return tuple(dimension), factor, 0.0


def _parse_quantity(value: str):
    """Split a numeric answer into (number, unit) without discarding a JSON zero."""
    text = str(value if value is not None else "").strip()
    if not text:
        return None
    # 先处理上标科学计数法（1.5×10³ -> 1.5e3），避免把 ×103 误读成 ×10³。
    text = re.sub(
        r"[×·*]\s*10\s*([⁰¹²³⁴⁵⁶⁷⁸⁹⁻⁺]+)",
        lambda match: "e" + "".join(_SUPERSCRIPT.get(ch, ch) for ch in match.group(1)),
        text,
    )
    text = "".join(_SUPERSCRIPT.get(ch, ch) for ch in text)
    text = text.replace("−", "-").replace("–", "-").replace("—", "-")
    # 科学计数法：1.5×10^3 / 1.5*10^-3 -> 1.5e3 / 1.5e-3
    text = re.sub(
        r"\s*[×·*]\s*10\s*\^\s*\(?\s*([+-]?\d+)\s*\)?", r"e\1", text,
    )
    match = _NUMBER_TOKEN.match(text)
    if not match:
        return None
    token = re.sub(r"\s+", "", match.group(1))
    remainder = text[match.end():].strip().lstrip("　 ").strip()
    try:
        if "/" in token:
            numerator, denominator = token.split("/", 1)
            number = float(numerator) / float(denominator)
        else:
            number = float(token)
    except (ValueError, ZeroDivisionError):
        return None
    unit = None
    if remainder:
        unit = _parse_unit(remainder)
        if unit is None:
            # A unit-shaped suffix we cannot convert must never be silently dropped.
            return number, (_UNKNOWN_UNIT, remainder)
    return number, unit


def _is_unknown_unit(unit) -> bool:
    return isinstance(unit, tuple) and bool(unit) and unit[0] == _UNKNOWN_UNIT


def _parse_number(val: str) -> float | None:
    parsed = _parse_quantity(val)
    if parsed is None:
        return None
    number, unit = parsed
    if _is_unknown_unit(unit):
        return None
    if unit is not None and (unit[0] != _DIMENSIONLESS or unit[2]):
        return None
    return number


def _relative_equal(a: float, b: float, tolerance: float = 1e-6) -> bool:
    """非零值用纯相对误差，零值用绝对误差。

    旧实现用 `max(1.0, ...)` 做分母，导致 1.8e-10 与 5e-10 这类微小物理/化学常数
    被静默判为一致；这里必须让非零量级真正参与相对误差。
    """
    if a == b:
        return True
    if a == 0 or b == 0:
        return abs(a - b) <= tolerance
    return abs(a - b) / max(abs(a), abs(b)) <= tolerance


def _compare_numeric(x: str, y: str) -> tuple[bool | None, str]:
    """Return (verdict, reason); `None` means the pair is not comparable（待核验）。"""
    left = _parse_quantity(x)
    right = _parse_quantity(y)
    if left is None or right is None:
        return None, "unparsed_number"
    number_a, unit_a = left
    number_b, unit_b = right
    if _is_unknown_unit(unit_a) or _is_unknown_unit(unit_b):
        return None, "unit_unknown"
    if unit_a is None and unit_b is None:
        return _relative_equal(number_a, number_b), "numeric_rel_1e-6"
    if unit_a is None or unit_b is None:
        dim, factor, offset = unit_a or unit_b
        if dim != _DIMENSIONLESS or offset:
            return None, "unit_missing"
        scaled = number_a * (factor if unit_a else 1.0)
        other = number_b * (factor if unit_b else 1.0)
        return _relative_equal(scaled, other), "numeric_rel_1e-6"
    dim_a, factor_a, offset_a = unit_a
    dim_b, factor_b, offset_b = unit_b
    if dim_a != dim_b:
        return None, "unit_mismatch"
    # 仿射单位（°C）先换算到基准单位再比较：25 °C == 298.15 K。
    return _relative_equal(
        number_a * factor_a + offset_a, number_b * factor_b + offset_b,
    ), "numeric_rel_1e-6"


def _normalize_choice(val: str) -> str:
    letters = sorted(set(re.findall(r"[A-H]", str(val or "").upper())))
    return "".join(letters)


def _answer_value(value) -> str:
    """Keep 0 and 0.0 as real answers; only None/empty count as missing."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _unpack_answer(value) -> tuple[str, str]:
    if isinstance(value, dict):
        kind = str(value.get("kind") or "text").strip().lower()
        return kind, _answer_value(value.get("value"))
    return "text", _answer_value(value)


_SYMBOLIC_LETTERS = re.compile(r"^[A-Za-z][0-9]?$|^[A-Za-z]{2}$")


def _classify_kind(kind_a: str, kind_b: str, val_a: str, val_b: str) -> str:
    """Decide how to compare a pair. Only explicit choice/choice pairs use set semantics.

    旧实现在 `[A-H]{1,6}` 上优先匹配，导致代数答案 `"a"`、`"b"`、`"AB"` 被当成
    选择题字母集合（`"AB"` 与 `"BA"` 会被判成同一个选项集合）。现在只有双方
    都声明 kind=choice 时才按选项集合比较；纯字母答案按表达式比较。
    """
    ka = str(kind_a or "").strip().lower()
    kb = str(kind_b or "").strip().lower()
    if ka == "choice" and kb == "choice":
        return "choice"
    if ka == kb and ka in ("number", "expression"):
        return ka
    if "number" in (ka, kb) and "choice" not in (ka, kb):
        return "number"
    if "expression" in (ka, kb):
        return "expression"
    # 双方都没声明 kind 的纯字母答案（a / b / AB / x1）按代数表达式处理。
    if _SYMBOLIC_LETTERS.match(str(val_a or "").strip()) and _SYMBOLIC_LETTERS.match(str(val_b or "").strip()):
        return "expression"
    # Text answers that both look numeric are compared as quantities, not as prose.
    if _parse_quantity(val_a) is not None and _parse_quantity(val_b) is not None:
        return "number"
    return "text"


_VERIFIED_METHODS = frozenset({"sympy", "numeric_rel_1e-6", "choice_set"})


def _pair_equal(kind: str, x: str, y: str) -> tuple[bool | None, str]:
    if kind == "choice":
        nx, ny = _normalize_choice(x), _normalize_choice(y)
        return (nx == ny and bool(nx)), "choice_set"
    if kind == "number":
        verdict, reason = _compare_numeric(x, y)
        if verdict is not None:
            return verdict, reason
        # 量纲不足或单位不同不能靠表达式解析绕过，必须保持不可比（待核验）。
        if reason != "unparsed_number":
            return None, reason
        expr_verdict, expr_reason = _sympy_equivalent_detailed(x, y)
        if expr_verdict is not None:
            return expr_verdict, expr_reason
        if _literal_equal(x, y):
            return True, "literal_equal_unverified"
        return None, expr_reason
    if kind == "expression":
        verdict, reason = _sympy_equivalent_detailed(x, y)
        if verdict is not None:
            return verdict, reason
        if _literal_equal(x, y):
            return True, "literal_equal_unverified"
        return None, reason
    return None, "text_needs_judge"


def _literal_equal(x: str, y: str) -> bool:
    def _norm(value: str) -> str:
        return re.sub(r"\s+", "", _normalize_math_text(value)).replace("**", "^")

    nx, ny = _norm(x), _norm(y)
    return bool(nx) and nx == ny


def compare_answers(ans_a: dict | str | None, ans_b: dict | str | None, reference: str | None = None) -> dict:
    """Programmatic comparison with an auditable trace.

    `comparable=False` 表示当前证据不足以程序化判定，必须交由独立裁判或保持待核验，
    不能把无法核验的表达式当成「一致」。
    """
    kind_a, val_a = _unpack_answer(ans_a)
    kind_b, val_b = _unpack_answer(ans_b)
    trace = {
        "input_a": {"kind": kind_a, "value": val_a},
        "input_b": {"kind": kind_b, "value": val_b},
        "reference": _answer_value(reference),
        "budget": {key: value for key, value in _verify_budget().items() if key != "extra_funcs"},
    }
    if not val_a or not val_b:
        return {
            "comparable": False, "agreed": None, "method": "missing",
            "matches_reference": None, "reason": "missing", "trace": trace,
        }

    kind = _classify_kind(kind_a, kind_b, val_a, val_b)
    agreed, method = _pair_equal(kind, val_a, val_b)
    trace.update({"kind": kind, "method": method})
    if agreed is None:
        return {
            "comparable": False, "agreed": None, "method": method,
            "matches_reference": None, "reason": method, "trace": trace,
        }

    matches_ref = None
    reference_verified = None
    ref_clean = _answer_value(reference)
    if ref_clean and _usable_reference(ref_clean) and agreed:
        ref_eq, ref_method = _pair_equal(kind, val_a, ref_clean)
        trace["reference_method"] = ref_method
        matches_ref = ref_eq
        reference_verified = ref_method in _VERIFIED_METHODS

    verified = method in _VERIFIED_METHODS
    result = {
        "comparable": True,
        "agreed": bool(agreed),
        "method": method,
        "value_a": val_a,
        "value_b": val_b,
        "matches_reference": matches_ref,
        "reference_verified": reference_verified,
        "verified": verified,
        "trace": trace,
    }
    if method == "literal_equal_unverified":
        # 字面一致只是猜测等价，不能当程序化核验结果用：不得给绿标/社区共享资格。
        result["warning"] = "literal_equal_unverified"
        result["notice"] = "两侧答案仅字面完全一致，未通过表达式或数值程序化核验，只能作为待确认参考。"
    return result


def _try_parse_structured_solution(text: str) -> dict | None:
    try:
        obj = json.loads(_strip_json_fences(text))
    except Exception:
        return None
    if not isinstance(obj, dict):
        return None
    if "final_answer" in obj and isinstance(obj.get("steps"), list):
        return obj
    return None


def _render_structured_to_markdown(sol: dict) -> str:
    parts = []
    fa = sol.get("final_answer") or {}
    val = fa.get("value") if isinstance(fa, dict) else fa
    unit = fa.get("unit") if isinstance(fa, dict) else None
    if val:
        parts.append(f"**结论**：{val}" + (f" {unit}" if unit else ""))
    if sol.get("method"):
        parts.append(f"**题型与通法**：{sol['method']}")
    if sol.get("knowledge_points"):
        parts.append("**涉及知识点**：" + "、".join(str(k) for k in sol["knowledge_points"]))
    steps = sol.get("steps") or []
    if steps:
        parts.append("\n### 解题步骤")
        for idx, st in enumerate(steps, 1):
            no = st.get("no", idx)
            title = st.get("title") or f"第 {no} 步"
            sp = " `得分点`" if st.get("score_point") else ""
            parts.append(f"**{no}. {title}**{sp}\n\n{st.get('content', '')}")
    if sol.get("assumptions"):
        parts.append("\n**前提与隐含条件**：\n" + "\n".join(f"- {a}" for a in sol["assumptions"]))
    if sol.get("uncertain"):
        parts.append("\n**信息存疑 / 需核对**：\n" + "\n".join(f"- {u}" for u in sol["uncertain"]))
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Subjective Rubric Grading (strictly preserving existing invariants)
# ---------------------------------------------------------------------------

def _parse_grade(text, rubric=None):
    clean = _strip_json_fences(text)
    try:
        grade = json.loads(clean)
    except (ValueError, TypeError):
        raise ValueError("评分模型没有返回可校验的 JSON，结果保持待确认") from None
    if not isinstance(grade, dict) or type(grade.get("is_correct")) is not bool:
        raise ValueError("评分缺少明确的是非判定，结果保持待确认")
    score, maximum, steps = grade.get("score"), grade.get("max_score"), grade.get("steps")
    if (type(score) not in (int, float) or type(maximum) not in (int, float) or maximum != 100
            or not 0 <= score <= 100 or not isinstance(steps, list) or not steps or len(steps) > 30):
        raise ValueError("评分量表格式不完整，结果保持待确认")
    earned_total = possible_total = 0
    for step in steps:
        if not isinstance(step, dict) or not isinstance(step.get("label"), str):
            raise ValueError("评分步骤不完整，结果保持待确认")
        earned, possible = step.get("earned"), step.get("possible")
        if (type(earned) not in (int, float) or type(possible) not in (int, float)
                or not 0 <= earned <= possible <= 100):
            raise ValueError("评分步骤分值无效，结果保持待确认")
        earned_total += earned
        possible_total += possible
    if abs(earned_total - score) > 0.01 or abs(possible_total - 100) > 0.01:
        raise ValueError("评分步骤与总分不一致，结果保持待确认")
    if grade["is_correct"] != (score == 100):
        raise ValueError("评分结论和得分矛盾，结果保持待确认")
    if rubric is not None:
        expected = {c["label"]: c["possible"] for c in rubric["criteria"]}
        actual = {s["label"]: s["possible"] for s in steps}
        if len(actual) != len(steps) or expected != actual:
            raise ValueError("模型没有按共同评分条目给分，结果保持待确认")
    return grade


def _parse_rubric(text):
    try:
        value = json.loads(_strip_json_fences(text))
        criteria = value["criteria"]
        if value.get("max_score") != 100 or not isinstance(criteria, list) or not 1 <= len(criteria) <= 12:
            raise ValueError
        labels = set()
        total = 0
        for criterion in criteria:
            label, possible = criterion["label"], criterion["possible"]
            if (not isinstance(label, str) or not label.strip() or label in labels or len(label) > 150
                    or type(possible) not in (int, float) or not 0 < possible <= 100
                    or not isinstance(criterion.get("expected"), str) or not criterion["expected"].strip()):
                raise ValueError
            labels.add(label)
            total += possible
        if abs(total - 100) > 0.01:
            raise ValueError
    except (ValueError, KeyError, TypeError):
        raise ValueError("未能生成可比较的共同评分标准，保持待确认") from None
    return value


def _usable_reference(text):
    return bool(re.search(r"[\u4e00-\u9fffA-Za-z0-9]", str(text)))


async def _grade_job(user_id, data, provider, verifier, *, job_id=None):
    from . import _bank_evidence as bank_evidence, _call_model as call_model
    from .. import practice

    evidence, images = bank_evidence(user_id, data)
    attempt = practice.latest_attempt(user_id, data.qid)
    if not attempt or attempt["id"] != data.attempt_id:
        raise ValueError("已提交新作答，本次旧作答评分失效")
    if attempt.get("grade_source") == "reference" and attempt.get("result") in ("correct", "incorrect"):
        raise ValueError("客观题原卷判题结果不能被 AI 评分覆盖")
    if not _usable_reference(evidence["原卷参考答案"]):
        report = {"content": "原卷缺少可用参考答案，无法可靠核验，已保持待确认。", "verification": {"status": "partial", "verdict": "pending"}}
        practice.set_ai_grade(user_id, data.attempt_id, "pending", report)
        return report

    rubric_call = await call_model(
        provider, data.model,
        "只根据题目和原卷参考答案，制定本题可核验的共同评分标准。不要猜测缺失题图或未给出的题目条件。"
        "该量表是AI建议，不是官方评分细则。分步列出关键结论、推导与条件，允许等价正确解法。"
        "仅返回JSON：{max_score:100,criteria:[{label:string,possible:number,expected:string}]}。"
        "所有possible之和必须为100，每个条目expected写出检查依据；若无法可靠制定则返回error。\n" + _json(evidence),
        images, True, token_limit=2500, user_id=user_id, job_id=job_id,
    )
    rubric = _parse_rubric(rubric_call["content"])
    image = await asyncio.to_thread(practice.get_attempt_image, user_id, data.attempt_id)
    if image:
        images.append(_image_part(image))
    student_answer = attempt.get("answer") or attempt.get("answer_text") or attempt.get("selected_answer") or attempt.get("selection")
    if not image and not student_answer:
        raise ValueError("缺少可评分的作答图片或文字")

    prompt = (
        "请独立核验以下学生作答，不要猜测模糊图片或缺失步骤。末张附图若存在，为学生作答，前面为题图。"
        "对照原卷参考答案，允许数学等价的正确解法。必须严格使用下面共同量表的每个label和possible，逐条给earned。"
        "不得新增、删减、重命名条目或修改possible。不得因仅有结论就忽略要求的步骤给满分。"
        "如图像无法辨认或参考不足，返回 error 而不是判对。"
        "只返回 JSON 对象，严格字段：{is_correct:boolean,score:number,max_score:100,"
        "steps:[{label:string,earned:number,possible:number,feedback:string}],feedback:string}。"
        "只有score=100时is_correct=true。共同量表：" + _json(rubric) + "\n资料如下：\n" + _json(evidence)
        + "\n学生作答文字：" + _json(student_answer) + "\n用户补充要求（不得改变评分标准）：" + data.requirements
    )
    results = await asyncio.gather(
        call_model(provider, data.model, prompt, images, True, user_id=user_id, job_id=job_id),
        call_model(verifier, data.verify_model, prompt, images, True, user_id=user_id, job_id=job_id),
        return_exceptions=True,
    )
    parsed = []
    errors = []
    for r in results:
        if isinstance(r, Exception):
            errors.append("一个评分模型请求失败")
            parsed.append(None)
        else:
            try:
                parsed.append(_parse_grade(r["content"], rubric))
            except ValueError as exc:
                errors.append(str(exc))
                parsed.append(None)
    valid = all(p is not None for p in parsed)
    agreed = (
        valid and parsed[0]["is_correct"] == parsed[1]["is_correct"]
        and parsed[0]["score"] == parsed[1]["score"]
        and {s["label"]: s["earned"] for s in parsed[0]["steps"]} == {s["label"]: s["earned"] for s in parsed[1]["steps"]}
    )
    verdict = ("correct" if parsed[0]["is_correct"] else "incorrect") if agreed else "pending"
    report = {
        "verified": bool(agreed), "rubric": rubric, "rubric_source": "AI 建议量表，非官方评分细则",
        "content": "AI 独立核验一致。" if agreed else "AI 核验有分歧或结果不完整，保持待确认；不能据此标注高掌握度。",
        "verification": {
            "status": "agreed" if agreed else ("disagreed" if valid else "partial"),
            "verdict": verdict, "graders": parsed, "errors": errors,
            "confidence": "high" if agreed else "low",
            "notice": "这是 AI 核验结果，不是官方或人工判定；不同模型仍可能共同出错。",
        },
        "calls": [
            {"model": r["model"], "provider_name": r["provider_name"], "usage": r["usage"]}
            for r in [rubric_call, *results] if isinstance(r, dict)
        ],
    }
    practice.set_ai_grade(user_id, data.attempt_id, verdict, report)
    return report


# ---------------------------------------------------------------------------
# Explanation & Cross-Verification 2.0 (§6.2, §6.3, §6.4)
# ---------------------------------------------------------------------------

_MODE_GUARDS = {
    # 讲解模式与交叉核验结果解耦：即使用户同时开启交叉验证，正文也必须遵守模式约束。
    "hints": (
        "\n【模式硬约束】启发引导模式严禁给出最终答案、选项字母、最终数值或可直接照抄的完整推导；"
        "只能给分层提示，让学生自己算出结论。独立核验的结论仅用于后台核对，不得写进本讲解正文。"
    ),
    "standard_steps": (
        "\n【模式硬约束】必须给出「采分点与分值建议」表（明确注明为 AI 参考、非官方评分细则），"
        "并逐步骤标注【得分点】；不得用通用解题摘要代替采分点表。"
    ),
    "variants": (
        "\n【模式硬约束】必须输出 2 道同知识点变式题（题干完整），答案与解析折叠在 <details> 中；"
        "不得把本题独立核验结论当成变式题正文。"
    ),
    "pattern_teaching": (
        "\n【模式硬约束】必须按「题型识别特征 → 通用解法框架 → 本题示范 → 常见变形」组织，"
        "不得只贴一份通用解题结论。"
    ),
}


def _mode_prompt(mode: str, evidence: dict, requirements: str) -> str:
    spec = load_explain_modes()["modes"]
    mode_info = spec.get(mode) or spec["detailed"]
    return (
        mode_info["prompt"]
        + _MODE_GUARDS.get(mode, "")
        + "\n数学公式请使用 $...$（行内）或 $$...$$（独立）。若信息缺失先列出缺失项，不编造题目或图形。"
        + "原卷答案仅作参考，可指出疑点但不能改写来源。\n题目证据：\n"
        + _json(evidence)
        + ("\n用户希望的讲解方式（仅影响风格）：" + requirements if requirements else "")
    )


def _structured_solve_prompt(evidence: dict, requirements: str) -> str:
    return (
        "请独立求解本题，不要受原卷答案暗示影响，先自己完整推导。"
        "严格返回 JSON 对象，包含字段：\n"
        '{"final_answer":{"kind":"choice|number|expression|text","value":"最终答案","unit":null},'
        '"steps":[{"no":1,"title":"步骤标题","content":"详细推导(公式用$...$)","score_point":true}],'
        '"knowledge_points":["知识点1"],"method":"题型与通法","assumptions":[],"uncertain":[]}\n'
        "题目证据（不含原卷答案，请独立推理）：\n"
        + _json({k: v for k, v in evidence.items() if k not in ("原卷参考答案", "原卷解析")})
        + ("\n用户要求：" + requirements if requirements else "")
    )


# 裁判 JSON 契约（线上讲解与离线评测共用）：任何关键分歧必须 agreed=false，禁止多数投票。
JUDGE_PROMPT_CONTRACT = (
    "你是独立裁判。请逐步骤对比两份独立题解 A 和 B 的最终答案、约束条件和关键推导是否一致。"
    "严格返回 JSON 对象："
    '{"agreed":boolean,"diverge_at_step":number|null,"which_is_right":"A|B|neither|unsure",'
    '"reason":"简明指出分歧所在步骤与依据","confidence":"high|medium|low","summary":"总结","differences":["分歧点"]}。'
    "有任何关键分歧必须 agreed=false。不要用多数投票强行统一。\n"
)


def build_judge_prompt(evidence: dict, solution_a: str, solution_b: str) -> str:
    """拼接裁判提示词；`_explain_job` 与离线 `eval.run_evaluation` 复用同一契约。"""
    return JUDGE_PROMPT_CONTRACT + _json({
        "evidence": evidence,
        "solution_A": solution_a,
        "solution_B": solution_b,
    })


def _upload_images(row) -> list[dict]:
    files = [row, *json.loads(row.get("extra_files_json") or "[]")]
    return [
        _image_part(storage.read_file("ai-files", item["storage_name"]))
        for item in files if item.get("media_type", "").startswith("image/")
    ]


def _load_upload_evidence(user_id: int, upload_id: int):
    row = _owned("ai_uploads", upload_id, user_id)
    confirmed = json.loads(row["confirmed_json"]) if row.get("confirmed_json") else None
    if confirmed:
        evidence = {
            "用户确认题目": confirmed.get("stem") or row["text_content"],
            "选项": confirmed.get("options") or [],
            "学科": confirmed.get("subject") or "",
            "题型": confirmed.get("type") or "",
            "补充条件": confirmed.get("conditions") or [],
        }
    else:
        evidence = {
            "上传题目": row["text_content"],
            "图片说明": "题目在附图中，请先核对识别；模糊或缺少条件时要求补充。",
        }
    return evidence, _upload_images(row)


def _save_explanation(
    user_id: int, qid: str | None, upload_id: int | None, mode: str,
    result: dict, *, share_eligible: bool = False, job_id: int | None = None,
):
    from . import PROMPT_VERSION
    try:
        with db.connection() as con:
            content_json = _json({
                "content": result.get("content", ""),
                "mode_cards": result.get("mode_cards") or [],
                "knowledge_points": result.get("knowledge_points") or [],
                "method": result.get("method") or "",
            })
            models_json = _json(result.get("calls") or [])
            verification_json = _json(result.get("verification") or {})
            # 任务重试的幂等写入：同一 job_id 只保留一份最终讲解产物。
            if job_id:
                existing = con.execute(
                    "SELECT id FROM ai_explanations WHERE job_id=?", (job_id,)
                ).fetchone()
                if existing:
                    con.execute(
                        "UPDATE ai_explanations SET qid=?,upload_id=?,mode=?,models_json=?,content_json=?,"
                        "verification_json=?,prompt_version=?,share_eligible=? WHERE id=? AND user_id=?",
                        (
                            qid, upload_id, mode, models_json, content_json, verification_json,
                            PROMPT_VERSION, int(share_eligible), existing["id"], user_id,
                        ),
                    )
                    return existing["id"]
            return con.execute(
                "INSERT INTO ai_explanations(qid,upload_id,user_id,mode,models_json,content_json,"
                "verification_json,prompt_version,created_at,share_eligible,job_id) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    qid, upload_id, user_id, mode, models_json, content_json, verification_json,
                    PROMPT_VERSION, db.utcnow(), int(share_eligible), job_id,
                ),
            ).lastrowid
    except Exception:
        return None


async def _explain_job(user_id, data, provider, verifier, *, judge=None, judge_model=None, on_delta=None, on_thinking=None, job_id=None):
    from . import _bank_evidence as bank_evidence, _call_model as call_model

    if data.qid:
        evidence, images = bank_evidence(user_id, data)
    else:
        evidence, images = await asyncio.to_thread(_load_upload_evidence, user_id, data.upload_id)

    reference_answer = evidence.get("原卷参考答案") or ""
    modes_spec = load_explain_modes()["modes"]

    # Determine requested mode(s)
    requested_modes = [m for m in (getattr(data, "modes", None) or []) if m in modes_spec]
    primary_mode = getattr(data, "explain_mode", None)
    if not requested_modes:
        if primary_mode and primary_mode in modes_spec:
            requested_modes = [primary_mode]
        elif data.task == "question-analysis":
            requested_modes = ["pattern_teaching"]
        else:
            requested_modes = ["detailed"]

    suffix = {"user_id": user_id, "job_id": job_id}

    def _call_summary(call):
        return {"model": call["model"], "provider_name": call["provider_name"], "usage": call["usage"]}

    def _unique_calls(items):
        """调用清单只保留参与模型去重后的展示项；逐次 token 账本在 ai_usage 表。"""
        seen = set()
        result = []
        for call in items:
            summary = _call_summary(call)
            key = (summary["model"], summary["provider_name"])
            if key in seen:
                continue
            seen.add(key)
            result.append(summary)
        return result

    async def _generate_mode_card(mode):
        call = await call_model(
            provider, data.model, _mode_prompt(mode, evidence, data.requirements), images,
            **suffix,
        )
        return {
            "mode": mode, "name": modes_spec[mode]["name"], "content": call["content"],
        }, call

    async def _reference_check(answer):
        """用同一答案自身 + 原卷答案，得到最终结论与原卷的对照结果（不额外调用模型）。

        注意：`compare_answers(answer, answer, reference)` 的 `agreed` 恒为 True，
        真正判断「与原卷是否冲突」的字段是 `matches_reference`。
        """
        if not answer or not (reference_answer and _usable_reference(reference_answer)):
            return None
        try:
            return await asyncio.to_thread(compare_answers, answer, answer, reference_answer)
        except Exception:
            return None

    # Cross-Verification 2.0 pipeline when verifier is provided
    if verifier:
        structured_verify = bool(getattr(data, "structured_verify", False))
        first_mode = requested_modes[0]
        # 独立求解只用于交叉核验/裁判/程序比对：结构化解题结果绝不能冒充讲解正文。
        solve_prompt = (
            _structured_solve_prompt(evidence, data.requirements)
            if structured_verify
            else _mode_prompt(first_mode, evidence, data.requirements)
        )
        # 结构化独立求解要求严格 JSON（json_output=True），避免模型输出包裹文字导致
        # 解析失败后退化为昂贵且不稳定的裁判调用；未开启时该调用本身就是讲解正文。
        raw_calls = await asyncio.gather(
            call_model(
                provider, data.model, solve_prompt, images,
                json_output=structured_verify, **suffix,
            ),
            call_model(
                verifier, data.verify_model, solve_prompt, images,
                json_output=structured_verify, **suffix,
            ),
            return_exceptions=True,
        )
        if all(isinstance(c, Exception) for c in raw_calls):
            raise ValueError("解题模型和核验模型均未成功，请检查连接与额度")

        labelled = [
            ("A" if index == 0 else "B", call)
            for index, call in enumerate(raw_calls) if isinstance(call, dict)
        ]
        solve_calls = [call for _label, call in labelled]
        sol_a = _try_parse_structured_solution(labelled[0][1]["content"]) if len(labelled) >= 1 else None
        sol_b = _try_parse_structured_solution(labelled[1][1]["content"]) if len(labelled) >= 2 else None

        independent_results = []
        for label, call in labelled:
            parsed_sol = sol_a if label == "A" else sol_b
            independent_results.append({
                "label": label,
                "model": call["model"],
                "provider_name": call["provider_name"],
                "content": _render_structured_to_markdown(parsed_sol) if parsed_sol else call["content"],
                "structured": parsed_sol,
                "source": "structured_solve" if structured_verify else "mode_solve",
            })

        verification = {
            "status": "partial",
            "confidence": "low",
            "independent_results": independent_results,
            "programmatic": None,
            "judge": None,
            "peer_review": None,
            "reference_conflict": False,
            "notice": "已分别独立解题；这是 AI 结果，不代表官方答案。",
        }

        if len(labelled) == 2:
            # Step 2: Zero-cost programmatic check when structured answers exist
            prog = None
            if sol_a and sol_b:
                # SymPy simplify 在可强杀子进程里执行，这里再放进线程池，绝不占用事件循环。
                prog = await asyncio.to_thread(
                    compare_answers, sol_a.get("final_answer"), sol_b.get("final_answer"), reference_answer,
                )
                verification["programmatic"] = prog

            if prog and prog.get("comparable") and prog.get("agreed"):
                if prog.get("warning"):
                    # 仅字面一致（literal_equal_unverified）：不得给绿标，也不得获得社区共享资格。
                    verification["status"] = "agreed"
                    verification["confidence"] = "low"
                    verification["warning"] = prog["warning"]
                    verification["notice"] = "🟡 双模型结论仅字面一致，未通过程序化核验，置信度已降级为待确认。"
                elif prog["matches_reference"] is True and prog.get("verified"):
                    verification["status"] = "agreed"
                    verification["confidence"] = "high"
                    verification["notice"] = "🟢 双模型独立求解一致，且与原卷参考答案一致（AI 结果，不代表官方答案）。"
                elif prog["matches_reference"] is False:
                    verification["status"] = "agreed"
                    verification["confidence"] = "medium"
                    verification["reference_conflict"] = True
                    verification["notice"] = f"🟡 双模型独立结论一致（{prog['value_a']}），但与原卷答案（{reference_answer}）不同：原卷存疑，两说并列。"
                else:
                    verification["status"] = "agreed"
                    verification["confidence"] = "medium"
                    verification["notice"] = "🟡 双模型独立求解一致（无原卷对照；AI 结果，不代表官方答案）。"
            else:
                # Step 3: Independent Judge — prefer Model C, fallback to Verifier Model B, NEVER Primary Model A!
                j_provider = judge or verifier
                j_model = judge_model or data.verify_model
                judge_prompt = build_judge_prompt(
                    evidence, labelled[0][1]["content"], labelled[1][1]["content"],
                )
                try:
                    # 裁判必须同时拿到题目附图（与解题模型同一批 evidence images）。
                    j_call = await call_model(
                        j_provider, j_model, judge_prompt, images, True,
                        token_limit=2000, **suffix,
                    )
                    parsed = json.loads(_strip_json_fences(j_call["content"]))
                    if not isinstance(parsed, dict) or type(parsed.get("agreed")) is not bool:
                        raise ValueError("模型比较格式不完整")
                    summary = parsed.get("summary") or parsed.get("reason") or ""
                    if not isinstance(summary, str):
                        raise ValueError("模型比较格式不完整")
                    parsed.setdefault("summary", summary)
                    parsed.setdefault("differences", [])
                    verification["judge"] = {
                        "model": j_call["model"],
                        "provider_name": j_call["provider_name"],
                        **parsed,
                    }
                    verification["comparison"] = parsed
                    solve_calls.append(j_call)

                    if parsed["agreed"]:
                        # 裁判判「一致」也要与程序化比对 / 原卷答案核对，冲突必须降级标注。
                        conflict = None
                        conflict_cmp = None
                        if prog is not None and prog.get("comparable"):
                            if prog.get("agreed") is False:
                                conflict = "programmatic_disagreed"
                            elif prog.get("matches_reference") is False:
                                conflict = "reference_conflict"
                        else:
                            conflict_cmp = await _reference_check(sol_a.get("final_answer") if sol_a else None)
                            if conflict_cmp is not None:
                                verification["reference_check"] = conflict_cmp
                                # _reference_check 用 (answer, answer, reference) 自比：agreed 恒为 True，
                                # 与原卷是否冲突只看 matches_reference，绝不能判断 agreed is False（死代码）。
                                if (
                                    conflict_cmp.get("comparable")
                                    and conflict_cmp.get("matches_reference") is False
                                ):
                                    conflict = "reference_conflict"
                        if conflict:
                            verification["status"] = "agreed"
                            verification["confidence"] = "low"
                            verification["reference_conflict"] = conflict == "reference_conflict"
                            verification["warning"] = conflict
                            verification["notice"] = (
                                "🟡 裁判判定双解一致，但程序化比对或原卷答案存在冲突，已降级为待人工确认；"
                                "不能据此标注高掌握度或社区共享。"
                            )
                        else:
                            verification["status"] = "agreed"
                            high = bool(
                                prog and prog.get("comparable") and prog.get("verified")
                                and prog.get("matches_reference") is True
                                and not prog.get("warning")
                            )
                            verification["confidence"] = "high" if high else "medium"
                            verification["notice"] = "🟡 裁判判定双解一致（裁判也是 AI，不代表官方答案）。"
                    else:
                        verification["status"] = "disagreed"
                        verification["confidence"] = "low"
                        # Step 4: Optional 1-round peer review in accurate mode
                        if getattr(data, "peer_review", False):
                            # 结构化模式下双解本身就是严格 JSON；互审也必须只返回同样结构，
                            # 否则修正后的答案无法重新解析、无法做复判。
                            revision_json_contract = (
                                "\n只返回严格 JSON 对象，字段与独立求解完全一致："
                                '{"final_answer":{"kind":"choice|number|expression|text",'
                                '"value":"最终答案","unit":null},'
                                '"steps":[{"no":1,"title":"步骤标题","content":"详细推导(公式用$...$)",'
                                '"score_point":true}],"knowledge_points":["知识点1"],'
                                '"method":"题型与通法","assumptions":[],"uncertain":[]}。'
                                "不要输出 JSON 之外的任何文字。"
                                if structured_verify else ""
                            )
                            pr_a = (
                                "另一位独立解题者给出了不同解答，请审视对方步骤与你的步骤，决定维持原解或修正错误（最多一轮，不要盲从）：\n"
                                f"你的解答：\n{labelled[0][1]['content']}\n\n对方解答：\n{labelled[1][1]['content']}\n"
                                f"裁判指出的分歧：{parsed.get('reason', '')}"
                                + revision_json_contract
                            )
                            pr_b = (
                                "另一位独立解题者给出了不同解答，请审视对方步骤与你的步骤，决定维持原解或修正错误（最多一轮，不要盲从）：\n"
                                f"你的解答：\n{labelled[1][1]['content']}\n\n对方解答：\n{labelled[0][1]['content']}\n"
                                f"裁判指出的分歧：{parsed.get('reason', '')}"
                                + revision_json_contract
                            )
                            rev_calls = await asyncio.gather(
                                call_model(
                                    provider, data.model, pr_a, images,
                                    json_output=structured_verify, token_limit=2500, **suffix,
                                ),
                                call_model(
                                    verifier, data.verify_model, pr_b, images,
                                    json_output=structured_verify, token_limit=2500, **suffix,
                                ),
                                return_exceptions=True,
                            )
                            revisions = [
                                {"label": "A" if i == 0 else "B", "model": r["model"], "content": r["content"]}
                                for i, r in enumerate(rev_calls) if isinstance(r, dict)
                            ]
                            for r in rev_calls:
                                if isinstance(r, dict):
                                    solve_calls.append(r)
                            verification["peer_review"] = {"round": 1, "revisions": revisions}
                            verification["notice"] = "已完成 1 轮互审，请对照两份解答与裁判意见判断（AI 结果，不代表官方答案）。"
                            # 互审若触发修正，必须对修正后的答案重新做一次程序化比对并更新结论。
                            revised = {}
                            for label, revision_call in (("A", rev_calls[0]), ("B", rev_calls[1])):
                                if isinstance(revision_call, dict):
                                    revised_sol = _try_parse_structured_solution(revision_call["content"])
                                    if revised_sol:
                                        revised[label] = revised_sol
                            if "A" in revised and "B" in revised:
                                try:
                                    recheck = await asyncio.to_thread(
                                        compare_answers,
                                        revised["A"].get("final_answer"),
                                        revised["B"].get("final_answer"),
                                        reference_answer,
                                    )
                                except Exception:
                                    recheck = None
                                verification["peer_review"]["recheck"] = recheck
                                if recheck and recheck.get("comparable"):
                                    verification["programmatic"] = recheck
                                    if recheck.get("agreed") and recheck.get("matches_reference") is True:
                                        verification["status"] = "agreed"
                                        verification["confidence"] = "medium"
                                        verification["notice"] = (
                                            "🟡 互审修正后双解结论一致且与原卷答案一致，已更新核验结果"
                                            "（AI 结果，不代表官方答案）。"
                                        )
                                    elif recheck.get("agreed"):
                                        verification["status"] = "agreed"
                                        verification["confidence"] = "low"
                                        verification["reference_conflict"] = recheck.get("matches_reference") is False
                                        verification["notice"] = "🟡 互审修正后双解结论一致，但与原卷答案不一致或无法对照，保持待确认。"
                                    else:
                                        verification["status"] = "disagreed"
                                        verification["confidence"] = "low"
                                        verification["notice"] = "互审修正后两份答案仍不一致，保持分歧。"
                except (ValueError, HTTPException, TypeError, json.JSONDecodeError):
                    verification["status"] = "partial"
                    verification["confidence"] = "low"
                    verification["notice"] = "两份题解已保存，自动比较失败，不能宣称交叉核验通过。"

        # 讲解正文：始终按 requested_modes 各自用模式提示词生成，结构化解题结果只做核验证据。
        mode_cards = []
        body_calls = []
        if structured_verify:
            for mode in requested_modes:
                card, body_call = await _generate_mode_card(mode)
                mode_cards.append(card)
                body_calls.append(body_call)
        else:
            if labelled and labelled[0][0] == "A":
                # 未开启结构化独立求解时，A 的调用本身就是第一个模式的讲解正文，直接复用。
                mode_cards.append({
                    "mode": first_mode,
                    "name": modes_spec[first_mode]["name"],
                    "content": labelled[0][1]["content"],
                })
            else:
                card, body_call = await _generate_mode_card(first_mode)
                mode_cards.append(card)
                body_calls.append(body_call)
            for extra_mode in requested_modes[1:]:
                card, body_call = await _generate_mode_card(extra_mode)
                mode_cards.append(card)
                body_calls.append(body_call)

        primary_content = mode_cards[0]["content"]
        combined = primary_content if len(mode_cards) == 1 else "\n\n---\n\n".join(
            f"# {c['name']}\n\n{c['content']}" for c in mode_cards
        )
        kps = (sol_a or {}).get("knowledge_points") or []
        method = (sol_a or {}).get("method") or ""
        programmatic = verification.get("programmatic") or {}
        # 跨中转站同底座模型：允许运行，但降级为待确认且不参与社区共享（决策 3）。
        verify_model = getattr(data, "verify_model", None)
        if verifier is not None and verify_model:
            try:
                from . import providers as _providers
                identity = _providers.identity_risk(provider, data.model, verifier, verify_model)
            except Exception:  # noqa: BLE001 - 身份判断失败不能影响讲解
                identity = None
            if identity:
                verification["identity_risk"] = identity
                if identity.get("cross_relay_same_model"):
                    if verification.get("confidence") == "high":
                        verification["confidence"] = "medium"
                    verification["warning"] = "cross_relay_same_model"
                    note = verification.get("notice") or ""
                    verification["notice"] = (
                        note + " ⚠️ 两个连接实际调用同一底座模型（可能只是不同中转站），"
                        "已降级为待确认，不计入高可信交叉核验。"
                    ).strip()
        result = {
            "content": combined,
            "mode": first_mode,
            "mode_cards": mode_cards,
            "knowledge_points": kps,
            "method": method,
            "verification": verification,
            "calls": _unique_calls([*solve_calls, *body_calls]),
        }
        notices = [call.get("notice") for call in [*solve_calls, *body_calls] if call.get("notice")]
        if notices:
            result["notices"] = notices
        # Only a single bank-only explanation with a verified reference match can be published.
        # Free-text instructions, literal-only matches and additional mode cards stay private.
        share_eligible = bool(
            data.qid and not data.upload_id and not data.attempt_id
            and not data.requirements.strip() and len(requested_modes) == 1
            and verification["status"] == "agreed"
            and programmatic.get("matches_reference") is True
            and programmatic.get("verified") is True
            and not programmatic.get("warning")
            and not verification.get("reference_conflict")
            and not (verification.get("identity_risk") or {}).get("cross_relay_same_model")
        )
        exp_id = _save_explanation(
            user_id, data.qid, data.upload_id, first_mode, result,
            share_eligible=share_eligible, job_id=job_id,
        )
        if exp_id:
            result["explanation_id"] = exp_id
        return result

    # Single-model mode (fast mode) — supports streaming delta & multi-mode cards!
    mode_cards = []
    calls = []
    for idx, mode in enumerate(requested_modes):
        prompt = _mode_prompt(mode, evidence, data.requirements)
        call_kwargs = {"user_id": user_id, "job_id": job_id}
        if on_delta is not None and len(requested_modes) == 1:
            call_kwargs["on_delta"] = on_delta
        if on_thinking is not None:
            call_kwargs["on_thinking"] = on_thinking
        call = await call_model(provider, data.model, prompt, images, **call_kwargs)
        calls.append(call)
        mode_cards.append({
            "mode": mode,
            "name": modes_spec[mode]["name"],
            "content": call["content"],
        })

    combined = mode_cards[0]["content"] if len(mode_cards) == 1 else "\n\n---\n\n".join(
        f"# {c['name']}\n\n{c['content']}" for c in mode_cards
    )
    result = {
        "content": combined,
        "mode": requested_modes[0],
        "mode_cards": mode_cards,
        "verification": {"status": "not_requested", "confidence": None, "notice": "快速模式（单模型），未进行交叉核验；AI 结果不代表官方答案。"},
        "calls": [{"model": c["model"], "provider_name": c["provider_name"], "usage": c["usage"]} for c in calls],
    }
    notices = [call.get("notice") for call in calls if call.get("notice")]
    if notices:
        result["notices"] = notices
    exp_id = _save_explanation(user_id, data.qid, data.upload_id, requested_modes[0], result, job_id=job_id)
    if exp_id:
        result["explanation_id"] = exp_id
    return result
