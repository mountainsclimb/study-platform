"""化学方程式配平（sympy，零新依赖）：`POST /api/chem/balance`。

边界（方案 §6.2 的配套能力）：

* 只接受元素符号、括号与数字下标（如 ``C3H8``、``Ca(OH)2``），不接受
  ``·``、电荷、状态符号或带系数的输入；
* 用 sympy 求元素守恒线性方程组的零空间，再取全正的最小整数解；
* 无解、系数过大或格式非法时返回中文原因（格式非法 422，无解返回 ``ok:false``）；
* 无状态、不落库、不是学习附件类型。
"""
from __future__ import annotations

from collections import Counter
from functools import reduce
import math
import re

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from .auth import current_user

__all__ = ["router", "balance_equation", "parse_formula", "MAX_EQUATION_CHARS"]

router = APIRouter(prefix="/api/chem", tags=["Chem balance"])

MAX_EQUATION_CHARS = 200
MAX_TERM_CHARS = 80
MAX_TERMS = 20
MAX_ELEMENTS = 40
MAX_COEFFICIENT = 1000

_ELEMENT_RE = re.compile(r"[A-Z][a-z]?")
#: 真实元素符号（118 种）；只做成员校验，不引入周期表依赖。
ELEMENTS = frozenset({
    "H", "He", "Li", "Be", "B", "C", "N", "O", "F", "Ne", "Na", "Mg", "Al",
    "Si", "P", "S", "Cl", "Ar", "K", "Ca", "Sc", "Ti", "V", "Cr", "Mn", "Fe",
    "Co", "Ni", "Cu", "Zn", "Ga", "Ge", "As", "Se", "Br", "Kr", "Rb", "Sr",
    "Y", "Zr", "Nb", "Mo", "Tc", "Ru", "Rh", "Pd", "Ag", "Cd", "In", "Sn",
    "Sb", "Te", "I", "Xe", "Cs", "Ba", "La", "Ce", "Pr", "Nd", "Pm", "Sm",
    "Eu", "Gd", "Tb", "Dy", "Ho", "Er", "Tm", "Yb", "Lu", "Hf", "Ta", "W",
    "Re", "Os", "Ir", "Pt", "Au", "Hg", "Tl", "Pb", "Bi", "Po", "At", "Rn",
    "Fr", "Ra", "Ac", "Th", "Pa", "U", "Np", "Pu", "Am", "Cm", "Bk", "Cf",
    "Es", "Fm", "Md", "No", "Lr", "Rf", "Db", "Sg", "Bh", "Hs", "Mt", "Ds",
    "Rg", "Cn", "Nh", "Fl", "Mc", "Lv", "Ts", "Og",
})
_ARROW_RE = re.compile(r"<=>|<->|⇌|⇋|↔|->|→|⟶|=")
_REVERSIBLE_ARROWS = ("<=>", "<->", "⇌", "⇋", "↔")
_TERM_ALLOWED_RE = re.compile(r"^(?:[A-Z][a-z]?\d*|\(|\)\d*)+$")


class BalanceInput(BaseModel):
    equation: str = Field(min_length=3, max_length=MAX_EQUATION_CHARS)


def parse_formula(term: str) -> Counter:
    """解析一个化学式（支持嵌套括号），返回 ``{元素: 原子数}``。"""
    value = str(term or "").strip()
    if not value:
        raise ValueError("化学式不能为空")
    if len(value) > MAX_TERM_CHARS:
        raise ValueError(f"单个化学式不能超过 {MAX_TERM_CHARS} 个字符")
    if value[0].isdigit():
        raise ValueError("方程式不要带配平系数，请只输入化学式")
    if not _TERM_ALLOWED_RE.fullmatch(value):
        raise ValueError(f"无法识别的化学式：{value}（只接受元素符号、括号与数字下标）")

    stack: list[Counter] = [Counter()]
    index = 0
    while index < len(value):
        char = value[index]
        if char == "(":
            stack.append(Counter())
            index += 1
            continue
        if char == ")":
            if len(stack) == 1:
                raise ValueError(f"括号不匹配：{value}")
            index += 1
            start = index
            while index < len(value) and value[index].isdigit():
                index += 1
            multiplier = int(value[start:index] or "1")
            if multiplier < 1 or multiplier > 100:
                raise ValueError(f"括号后的数字下标无效：{value}")
            group = stack.pop()
            if not group:
                raise ValueError(f"括号内的化学式为空：{value}")
            for element, count in group.items():
                stack[-1][element] += count * multiplier
            continue
        match = _ELEMENT_RE.match(value, index)
        if match is None:
            raise ValueError(f"无法识别的化学式：{value}")
        element = match.group(0)
        if element not in ELEMENTS:
            raise ValueError(f"未知元素符号：{element}")
        index = match.end()
        start = index
        while index < len(value) and value[index].isdigit():
            index += 1
        count = int(value[start:index] or "1")
        if count < 1 or count > 100:
            raise ValueError(f"元素下标无效：{value}")
        stack[-1][element] += count

    if len(stack) != 1:
        raise ValueError(f"括号不匹配：{value}")
    counts = stack[0]
    if not counts:
        raise ValueError("化学式为空")
    if len(counts) > MAX_ELEMENTS:
        raise ValueError(f"化学式元素过多（最多 {MAX_ELEMENTS} 种）")
    return counts


def _split_side(part: str, label: str) -> list[str]:
    terms = [item.strip() for item in str(part or "").split("+")]
    if not terms or any(not item for item in terms):
        raise ValueError(f"{label}有空的化学式")
    if len(terms) > MAX_TERMS:
        raise ValueError(f"单侧化学式最多 {MAX_TERMS} 项")
    return terms


def _positive_solution(matrix) -> list[int] | None:
    """求元素守恒方程组的全正有理解，并约分为整数系数。

    使用线性规划 ``min sum(x_i)``、``x_i >= 1``、``A x = 0``：只要存在全正解，
    缩放到每个分量 ≥1 后一定可行，不依赖零空间基的组合搜索；随后按分母公倍数
    与最大公约数化为一组整数系数（同一配平反应的不同整数倍只保留最简比）。
    """
    from sympy import Eq, Ge, Rational, ilcm, symbols
    from sympy.solvers.simplex import lpmin

    column_count = matrix.cols
    if column_count <= 0:
        return None
    variables = symbols(f"x0:{column_count}", real=True)
    constraints = [Ge(variable, 1) for variable in variables]
    for row in range(matrix.rows):
        constraints.append(Eq(
            sum(matrix[row, column] * variables[column] for column in range(column_count)),
            0,
        ))
    try:
        _value, solution = lpmin(sum(variables), constraints)
    except Exception:  # 无可行解 / 线性规划异常都按无解处理
        return None
    values = [Rational(solution[variable]) for variable in variables]
    if not values or not all(value > 0 for value in values):
        return None
    denominator_lcm = ilcm(*[value.q for value in values])
    integers = [int(value * denominator_lcm) for value in values]
    common = reduce(math.gcd, integers)
    if common > 1:
        integers = [value // common for value in integers]
    if all(value > 0 for value in integers) and max(integers) <= MAX_COEFFICIENT:
        return integers
    return None


def _formula_with_coefficient(coefficient: int, formula: str) -> str:
    return (f"{coefficient}" if coefficient != 1 else "") + formula


def balance_equation(equation: str) -> dict:
    """配平方程式；格式错误抛 ``ValueError``，无解返回 ``ok:false``。"""
    value = str(equation or "").strip()
    if not value:
        raise ValueError("请输入化学方程式")
    if len(value) > MAX_EQUATION_CHARS:
        raise ValueError(f"方程式不能超过 {MAX_EQUATION_CHARS} 个字符")
    from sympy import Matrix

    parts = _ARROW_RE.split(value)
    if len(parts) != 2:
        raise ValueError("方程式需要使用一个箭头（->、→、⇌ 或 =）分隔反应物与生成物")
    reversible = any(arrow in value for arrow in _REVERSIBLE_ARROWS)
    left_terms = _split_side(parts[0], "反应物")
    right_terms = _split_side(parts[1], "生成物")
    if len(left_terms) + len(right_terms) > MAX_TERMS:
        raise ValueError(f"方程式最多 {MAX_TERMS} 项")

    formulas = [*left_terms, *right_terms]
    counts = [parse_formula(formula) for formula in formulas]
    elements = sorted({element for item in counts for element in item})
    if not elements:
        raise ValueError("方程式没有可识别的元素")
    if len(elements) > MAX_ELEMENTS:
        raise ValueError(f"方程式元素种类过多（最多 {MAX_ELEMENTS} 种）")
    signs = [1] * len(left_terms) + [-1] * len(right_terms)
    matrix = Matrix([
        [counts[index].get(element, 0) * signs[index] for index in range(len(formulas))]
        for element in elements
    ])
    solution = _positive_solution(matrix)
    if solution is None:
        return {
            "ok": False,
            "coefficients": [],
            "balanced": "",
            "arrow": "⇌" if reversible else "->",
            "reversible": reversible,
            "reason": "无法找到正整数配平系数（请检查元素是否守恒，或方程式是否可配平）",
        }
    left = " + ".join(
        _formula_with_coefficient(coefficient, formula)
        for coefficient, formula in zip(solution[:len(left_terms)], left_terms)
    )
    right = " + ".join(
        _formula_with_coefficient(coefficient, formula)
        for coefficient, formula in zip(solution[len(left_terms):], right_terms)
    )
    arrow = "⇌" if reversible else "->"
    return {
        "ok": True,
        "coefficients": solution,
        "balanced": f"{left} {arrow} {right}",
        "arrow": arrow,
        "reversible": reversible,
        "reason": "",
    }


@router.post("/balance")
def balance(payload: BalanceInput, user=Depends(current_user)):
    """配平化学方程式（需登录；无状态、不落库）。"""
    try:
        return balance_equation(payload.equation)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
