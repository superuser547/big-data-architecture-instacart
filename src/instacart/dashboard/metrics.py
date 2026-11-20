"""Formatting helpers for reported measures and published statistical results."""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any


def format_number(value: Any, digits: int = 0) -> str:
    if value is None:
        return "нет данных"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "нет данных"
    if not math.isfinite(number):
        return "нет данных"
    return f"{number:,.{digits}f}".replace(",", "\u202f").replace(".", ",")


def format_percent(value: Any, digits: int = 1) -> str:
    if value is None:
        return "нет данных"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "нет данных"
    if not math.isfinite(number):
        return "нет данных"
    return f"{number * 100:.{digits}f}%".replace(".", ",")


def format_percentage_points(value: Any, digits: int = 1) -> str:
    if value is None:
        return "нет данных"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "нет данных"
    if not math.isfinite(number):
        return "нет данных"
    sign = "+" if number > 0 else ""
    return f"{sign}{format_number(number * 100, digits)} п.п."


def format_p_value(value: Any, log10_value: Any = None) -> str:
    if value is None:
        return "не оценено"
    try:
        p_value = float(value)
    except (TypeError, ValueError):
        return "не оценено"
    if not math.isfinite(p_value) or not 0 <= p_value <= 1:
        return "не оценено"
    if p_value == 0:
        if log10_value is not None:
            try:
                log_value = float(log10_value)
            except (TypeError, ValueError):
                log_value = math.nan
            if math.isfinite(log_value):
                return f"< 1e-300 (log₁₀ p = {format_number(log_value, 1)})"
        return "< 1e-300 (ниже точности float)"
    if p_value < 0.001:
        return f"{p_value:.2e}".replace(".", ",")
    return format_number(p_value, 3)


def interval_text(lower: Any, upper: Any, *, percent: bool = False) -> str:
    if lower is None or upper is None:
        return "не оценён"
    formatter = format_percent if percent else lambda value: format_number(value, 3)
    return f"[{formatter(lower)}; {formatter(upper)}]"


def percentage_point_interval(lower: Any, upper: Any) -> str:
    if lower is None or upper is None:
        return "не оценён"
    return f"[{format_percentage_points(lower)}; {format_percentage_points(upper)}]"


def ratio(numerator: int | float, denominator: int | float) -> float | None:
    if denominator <= 0:
        return None
    return numerator / denominator


def resolve_parent_departments(
    department_ids: tuple[int, ...],
    aisle_ids: tuple[int, ...],
    aisle_to_department: Mapping[int, int],
) -> tuple[int, ...]:
    """Apply aisle selections to department-level measures through their parent department."""
    departments = set(department_ids)
    if not aisle_ids:
        return tuple(sorted(departments))
    parents = {
        aisle_to_department[aisle_id] for aisle_id in aisle_ids if aisle_id in aisle_to_department
    }
    if departments:
        parents &= departments
    return tuple(sorted(parents))


def lineage_arguments(readiness: Any) -> tuple[str, str, str, str]:
    values = (
        readiness.dataset_fingerprint,
        readiness.ingestion_run_id,
        readiness.marts_run_id,
        readiness.analysis_run_id,
    )
    if any(value is None for value in values):
        raise ValueError("Актуальная цепочка аналитических запусков отсутствует")
    return values  # type: ignore[return-value]


def json_log_p(metrics: Mapping[str, Any] | None, key: str = "log10_p_value") -> float | None:
    if not metrics:
        return None
    value = metrics.get(key)
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None
