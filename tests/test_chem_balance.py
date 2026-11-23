"""化学方程式配平（app/chem_balance.py，方案 §6.2）验收。

覆盖：常见方程式、嵌套括号、最小整数系数、无解、非法字符/带系数输入、超长、
未登录 401，以及配平字符串的展示口径。
"""
import pytest
from fastapi.testclient import TestClient

from app.chem_balance import balance_equation, parse_formula
from app.main import app


def test_parse_formula_supports_nested_parentheses():
    assert dict(parse_formula("C3H8")) == {"C": 3, "H": 8}
    assert dict(parse_formula("Ca(OH)2")) == {"Ca": 1, "O": 2, "H": 2}
    assert dict(parse_formula("(NH4)2SO4")) == {"N": 2, "H": 8, "S": 1, "O": 4}


@pytest.mark.parametrize("equation, expected", [
    ("H2 + O2 -> H2O", [2, 1, 2]),
    ("C3H8 + O2 -> CO2 + H2O", [1, 5, 3, 4]),
    ("Fe + O2 -> Fe2O3", [4, 3, 2]),
    ("Ca(OH)2 + H3PO4 -> Ca3(PO4)2 + H2O", [3, 2, 1, 6]),
    ("NH4NO3 -> N2O + H2O", [1, 1, 2]),
    ("H2 + O2 = H2O", [2, 1, 2]),
])
def test_balances_common_equations_with_minimal_integers(equation, expected):
    result = balance_equation(equation)
    assert result["ok"] is True
    assert result["coefficients"] == expected
    assert result["reason"] == ""


def test_balanced_text_omits_coefficient_one():
    result = balance_equation("Fe + O2 -> Fe2O3")
    assert result["balanced"] == "4Fe + 3O2 -> 2Fe2O3"


def test_unsolvable_equation_returns_ok_false():
    result = balance_equation("H2 -> O2")
    assert result["ok"] is False
    assert result["coefficients"] == []
    assert "无法找到" in result["reason"]


@pytest.mark.parametrize("equation", [
    "2H2 + O2 -> H2O",
    "H2 + O2",
    "H2 + -> H2O",
    "H2 + O2 -> H2O +",
    "H2 + O2 -> H2O$",
    "H2 + O2 -> H2O·",
])
def test_invalid_inputs_return_reason(equation):
    with pytest.raises(ValueError):
        balance_equation(equation)


def test_api_requires_login_and_returns_422_for_bad_equation(client, monkeypatch):
    anonymous = TestClient(app)
    response = anonymous.post("/api/chem/balance", json={"equation": "H2 + O2 -> H2O"})
    assert response.status_code == 401

    response = client.post("/api/chem/balance", json={"equation": "H2 + O2 -> H2O"})
    assert response.status_code == 200, response.text
    assert response.json()["coefficients"] == [2, 1, 2]

    response = client.post("/api/chem/balance", json={"equation": "2H2 + O2 -> H2O"})
    assert response.status_code == 422
    assert "系数" in response.json()["detail"]

    response = client.post("/api/chem/balance", json={"equation": "H" * 201})
    assert response.status_code == 422
