"""Versioned deterministic report calculations using decimal arithmetic."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any


class ReportCalculationError(ValueError):
    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


def _decimal(value: Any) -> Decimal:
    if isinstance(value, bool):
        raise ReportCalculationError("布尔值不能作为公式输入", code="REPORT_FORMULA_INPUT_INVALID")
    if isinstance(value, (int, float, str)):
        try:
            return Decimal(str(value))
        except InvalidOperation as exc:
            raise ReportCalculationError(
                "公式输入必须是十进制数值", code="REPORT_FORMULA_INPUT_INVALID"
            ) from exc
    if isinstance(value, list):
        return sum((_decimal(item) for item in value), Decimal(0))
    if isinstance(value, dict):
        return sum((_decimal(item) for item in value.values()), Decimal(0))
    raise ReportCalculationError(
        "公式输入必须为数值、数值数组或数值对象", code="REPORT_FORMULA_INPUT_INVALID"
    )


def execute_registered_formula(
    formula: dict[str, Any],
    *,
    inputs: dict[str, Any],
    parameters: dict[str, Any] | None = None,
) -> Decimal:
    input_ids = list(formula.get("input_field_ids") or [])
    if set(inputs) != set(input_ids):
        raise ReportCalculationError("公式输入字段不匹配", code="REPORT_FORMULA_INPUT_MISMATCH")
    parameter_ids = list(formula.get("parameter_ids") or [])
    supplied_parameters = parameters or {}
    if set(supplied_parameters) != set(parameter_ids):
        raise ReportCalculationError("公式参数不匹配", code="REPORT_FORMULA_PARAMETER_MISMATCH")

    collection_value_key = formula.get("collection_value_key")
    values: list[Decimal] = []
    for field_id in input_ids:
        value = inputs[field_id]
        if collection_value_key and isinstance(value, list):
            try:
                values.append(
                    sum(
                        (_decimal(item[collection_value_key]) for item in value),
                        Decimal(0),
                    )
                )
            except (KeyError, TypeError) as exc:
                raise ReportCalculationError(
                    "集合公式输入缺少注册的数值字段",
                    code="REPORT_FORMULA_INPUT_INVALID",
                ) from exc
        else:
            values.append(_decimal(value))
    operator = formula.get("operator")
    if operator == "SUM":
        result = sum(values, Decimal(0))
    elif operator in {"DIVIDE", "SCALED_DIVIDE", "PERCENT"}:
        if len(values) != 2 or values[1] == 0:
            raise ReportCalculationError(
                "除法公式要求两个输入且分母不能为零", code="REPORT_FORMULA_DIVISION_INVALID"
            )
        result = values[0] / values[1]
        if operator == "PERCENT":
            result *= Decimal(100)
        elif operator == "SCALED_DIVIDE":
            result *= _decimal(formula.get("scale", 1))
    elif operator == "WEIGHTED_SUM":
        if len(parameter_ids) != len(input_ids):
            raise ReportCalculationError(
                "加权求和公式参数定义不完整", code="REPORT_FORMULA_DEFINITION_INVALID"
            )
        result = sum(
            (
                value * _decimal(supplied_parameters[parameter_id])
                for value, parameter_id in zip(values, parameter_ids, strict=True)
            ),
            Decimal(0),
        )
    else:
        raise ReportCalculationError("公式算子未注册", code="REPORT_FORMULA_OPERATOR_UNSUPPORTED")
    return result.quantize(Decimal("0.000001"))
