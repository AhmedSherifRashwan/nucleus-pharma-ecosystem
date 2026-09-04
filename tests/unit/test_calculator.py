"""The calculator computes; it must not execute (PRD s19)."""

from __future__ import annotations

import pytest

from app.tools.calculator import CalculatorError, evaluate


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        ("2 + 2", 4.0),
        ("250 / 800 * 100", 31.25),
        ("round(100 / 3, 2)", 33.33),
        ("sqrt(16)", 4.0),
        ("2 ** 10", 1024.0),
        ("sum([45.0, 25.0, 15.0, 10.0, 4.5, 0.5])", 100.0),  # a mass balance
    ],
)
def test_arithmetic(expression, expected):
    assert evaluate(expression) == pytest.approx(expected)


@pytest.mark.parametrize(
    "expression",
    [
        "__import__('os').system('ls')",
        "open('/etc/passwd').read()",
        "().__class__.__bases__",
        "eval('1+1')",
        "globals()",
        "lambda: 1",
        "x + 1",
        "print(1)",
    ],
)
def test_code_execution_is_blocked(expression):
    with pytest.raises(CalculatorError):
        evaluate(expression)


def test_resource_exhaustion_is_bounded():
    with pytest.raises(CalculatorError, match="exponent too large"):
        evaluate("2 ** 999999")
    with pytest.raises(CalculatorError, match="too long"):
        evaluate("1+" * 400 + "1")


def test_non_finite_and_empty_are_refused():
    with pytest.raises(CalculatorError):
        evaluate("1e308 * 10")
    with pytest.raises(CalculatorError):
        evaluate("")


def test_division_by_zero_propagates():
    with pytest.raises(ZeroDivisionError):
        evaluate("1 / 0")
