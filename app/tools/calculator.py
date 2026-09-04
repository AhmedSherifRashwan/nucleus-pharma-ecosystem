"""Deterministic arithmetic (PRD s19).

Material arithmetic - mass balance, concentrations, yields, shelf-life
extrapolation - must not be done by a language model. This evaluates a restricted
expression grammar with no names, calls or attribute access, so there is no
code-execution surface.
"""

from __future__ import annotations

import ast
import math
import operator
from typing import Any

_BIN_OPS: dict[type[ast.operator], Any] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.Pow: operator.pow,
    ast.Mod: operator.mod,
    ast.FloorDiv: operator.floordiv,
}
_UNARY_OPS: dict[type[ast.unaryop], Any] = {ast.UAdd: operator.pos, ast.USub: operator.neg}
_FUNCTIONS: dict[str, Any] = {
    "abs": abs,
    "round": round,
    "min": min,
    "max": max,
    "sum": sum,
    "sqrt": math.sqrt,
    "log": math.log,
    "log10": math.log10,
    "exp": math.exp,
}
_CONSTANTS: dict[str, float] = {"pi": math.pi, "e": math.e}

MAX_EXPRESSION_LENGTH = 500
MAX_POWER = 1000


class CalculatorError(ValueError):
    pass


def _eval(node: ast.AST) -> Any:
    if isinstance(node, ast.Expression):
        return _eval(node.body)
    if isinstance(node, ast.Constant):
        if isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
            return node.value
        raise CalculatorError(f"unsupported constant: {node.value!r}")
    if isinstance(node, ast.BinOp):
        op = _BIN_OPS.get(type(node.op))
        if op is None:
            raise CalculatorError(f"unsupported operator: {type(node.op).__name__}")
        left, right = _eval(node.left), _eval(node.right)
        if isinstance(node.op, ast.Pow) and abs(right) > MAX_POWER:
            raise CalculatorError("exponent too large")
        return op(left, right)
    if isinstance(node, ast.UnaryOp):
        op = _UNARY_OPS.get(type(node.op))
        if op is None:
            raise CalculatorError(f"unsupported unary operator: {type(node.op).__name__}")
        return op(_eval(node.operand))
    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name) or node.func.id not in _FUNCTIONS:
            raise CalculatorError("only whitelisted functions may be called")
        if node.keywords:
            raise CalculatorError("keyword arguments are not supported")
        return _FUNCTIONS[node.func.id](*[_eval(a) for a in node.args])
    if isinstance(node, ast.Name):
        if node.id in _CONSTANTS:
            return _CONSTANTS[node.id]
        raise CalculatorError(f"unknown name: {node.id}")
    if isinstance(node, (ast.List, ast.Tuple)):
        return [_eval(e) for e in node.elts]
    raise CalculatorError(f"unsupported expression element: {type(node).__name__}")


def evaluate(expression: str) -> float:
    if not expression or not expression.strip():
        raise CalculatorError("empty expression")
    if len(expression) > MAX_EXPRESSION_LENGTH:
        raise CalculatorError("expression too long")
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as exc:
        raise CalculatorError(f"invalid expression: {exc.msg}") from exc
    result = _eval(tree)
    if isinstance(result, bool) or not isinstance(result, (int, float)):
        raise CalculatorError("expression did not evaluate to a number")
    if math.isnan(result) or math.isinf(result):
        raise CalculatorError("expression produced a non-finite result")
    return float(result)
