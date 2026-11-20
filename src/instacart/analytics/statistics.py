"""Statistical summaries used by the reproducible hypothesis analyses."""

from __future__ import annotations

from collections.abc import Sequence
from math import lgamma, log, log10
from typing import Any

import numpy as np
from scipy.stats import chi2, chi2_contingency


def wilson_interval(
    successes: int, observations: int, confidence: float = 0.95
) -> tuple[float, float] | None:
    """Return a Wilson score interval for one binomial proportion."""
    if observations <= 0:
        return None
    if successes < 0 or successes > observations:
        raise ValueError("successes must be between zero and observations")
    z = 1.959963984540054 if confidence == 0.95 else _normal_quantile(confidence)
    p = successes / observations
    denominator = 1 + z * z / observations
    center = (p + z * z / (2 * observations)) / denominator
    margin = z * np.sqrt(p * (1 - p) / observations + z * z / (4 * observations**2)) / denominator
    return float(max(0, center - margin)), float(min(1, center + margin))


def _normal_quantile(confidence: float) -> float:
    from scipy.stats import norm

    if not 0 < confidence < 1:
        raise ValueError("confidence must be between zero and one")
    return float(norm.ppf(0.5 + confidence / 2))


def chi_square_summary(table: Sequence[Sequence[int]] | np.ndarray) -> dict[str, Any]:
    """Pearson chi-square and Cramér's V, retaining empty margins for plots."""
    observed = np.asarray(table, dtype=np.int64)
    if observed.ndim != 2 or np.any(observed < 0):
        raise ValueError("contingency table must be a nonnegative 2D array")
    row_totals = observed.sum(axis=1)
    col_totals = observed.sum(axis=0)
    active_rows = row_totals > 0
    active_cols = col_totals > 0
    expected = np.zeros(observed.shape, dtype=np.float64)
    residuals = np.zeros(observed.shape, dtype=np.float64)
    total = int(observed.sum())
    if total:
        expected = np.outer(row_totals, col_totals) / total
        residuals = np.divide(
            observed - expected,
            np.sqrt(expected),
            out=np.zeros(observed.shape, dtype=np.float64),
            where=expected > 0,
        )
    normalized = np.divide(
        observed,
        row_totals[:, None],
        out=np.zeros(observed.shape, dtype=np.float64),
        where=row_totals[:, None] != 0,
    )
    result: dict[str, Any] = {
        "observations": total,
        "statistic": None,
        "p_value": None,
        "log10_p_value": None,
        "degrees_of_freedom": 0,
        "cramers_v": None,
        "cramers_v_uncorrected": None,
        "cramers_v_correction": "Bergsma finite-sample bias correction",
        "observed": observed.tolist(),
        "expected": expected.tolist(),
        "pearson_residuals": residuals.tolist(),
        "row_normalized": normalized.tolist(),
    }
    if total == 0 or active_rows.sum() < 2 or active_cols.sum() < 2:
        return result

    reduced = observed[np.ix_(active_rows, active_cols)]
    statistic, p_value, degrees, expected_reduced = chi2_contingency(reduced, correction=False)
    expected[np.ix_(active_rows, active_cols)] = expected_reduced
    residuals[np.ix_(active_rows, active_cols)] = (reduced - expected_reduced) / np.sqrt(
        expected_reduced
    )
    dimensions = min(int(active_rows.sum()) - 1, int(active_cols.sum()) - 1)
    raw_effect = float(np.sqrt(statistic / (total * dimensions))) if dimensions > 0 else None
    corrected_effect = None
    if dimensions > 0 and total > 1:
        rows = int(active_rows.sum())
        columns = int(active_cols.sum())
        phi_squared = statistic / total
        corrected_phi_squared = max(
            0.0,
            phi_squared - ((rows - 1) * (columns - 1)) / (total - 1),
        )
        corrected_rows = rows - ((rows - 1) ** 2) / (total - 1)
        corrected_columns = columns - ((columns - 1) ** 2) / (total - 1)
        corrected_dimensions = min(corrected_rows - 1, corrected_columns - 1)
        if corrected_dimensions > 0:
            corrected_effect = float(np.sqrt(corrected_phi_squared / corrected_dimensions))
    result.update(
        {
            "statistic": float(statistic),
            "p_value": float(p_value),
            "log10_p_value": log10_p_value(float(statistic), int(degrees), float(p_value)),
            "degrees_of_freedom": int(degrees),
            "cramers_v": corrected_effect,
            "cramers_v_uncorrected": raw_effect,
            "expected": expected.tolist(),
            "pearson_residuals": residuals.tolist(),
        }
    )
    return result


def log10_p_value(statistic: float, degrees_of_freedom: int, p_value: float) -> float | None:
    """Stable log10 tail probability when a floating-point p-value underflows."""
    if degrees_of_freedom <= 0 or statistic < 0:
        return None
    if p_value > 0:
        return float(log10(p_value))
    shape = degrees_of_freedom / 2
    value = statistic / 2
    if value <= 0:
        return 0.0
    # The zero-underflow region is far enough into the tail for this expansion.
    series = 1.0
    term = 1.0
    for index in range(1, 40):
        term *= (shape - index) / value
        if abs(term) < 1e-16:
            break
        series += term
    if series <= 0:
        return None
    log_probability = (shape - 1) * log(value) - value - lgamma(shape) + log(series)
    return float(log_probability / log(10))


def mcnemar_from_counts(first_only: int, second_only: int) -> tuple[float, float]:
    """McNemar continuity-corrected statistic and p-value from discordant counts."""
    if first_only < 0 or second_only < 0:
        raise ValueError("discordant counts must be nonnegative")
    discordant = first_only + second_only
    statistic = max(abs(first_only - second_only) - 1, 0) ** 2 / discordant if discordant else 0.0
    p_value = float(chi2.sf(statistic, 1)) if discordant else 1.0
    return float(statistic), p_value


def mcnemar_summary(first: Sequence[int], second: Sequence[int]) -> dict[str, Any]:
    """McNemar test with continuity correction for paired binary observations."""
    first_values = np.asarray(first)
    second_values = np.asarray(second)
    if first_values.shape != second_values.shape:
        raise ValueError("paired observations must have equal shapes")
    if first_values.ndim != 1:
        raise ValueError("paired observations must be one-dimensional")
    if np.any((first_values != 0) & (first_values != 1)) or np.any(
        (second_values != 0) & (second_values != 1)
    ):
        raise ValueError("paired outcomes must be binary")
    first_values = first_values.astype(np.int8, copy=False)
    second_values = second_values.astype(np.int8, copy=False)
    discordant_first = int(np.sum((first_values == 1) & (second_values == 0)))
    discordant_second = int(np.sum((first_values == 0) & (second_values == 1)))
    statistic, p_value = mcnemar_from_counts(discordant_first, discordant_second)
    difference = float(np.mean(first_values - second_values)) if first_values.size else None
    return {
        "observations": int(first_values.size),
        "discordant_first_only": discordant_first,
        "discordant_second_only": discordant_second,
        "statistic": statistic if first_values.size else None,
        "p_value": p_value if first_values.size else None,
        "risk_difference": difference,
    }


def holm_adjust(p_values: Sequence[float]) -> list[float]:
    """Holm step-down adjusted p-values in their original order."""
    values = np.asarray(p_values, dtype=np.float64)
    if np.any((values < 0) | (values > 1)):
        raise ValueError("p-values must be between zero and one")
    count = values.size
    if not count:
        return []
    order = np.argsort(values, kind="stable")
    adjusted_sorted = np.maximum.accumulate((count - np.arange(count)) * values[order])
    adjusted = np.empty(count, dtype=np.float64)
    adjusted[order] = np.minimum(adjusted_sorted, 1.0)
    return adjusted.tolist()


def percentile_interval(
    samples: Sequence[float], confidence: float = 0.95
) -> tuple[float, float] | None:
    """Percentile interval for a nonempty bootstrap sample."""
    values = np.asarray(samples, dtype=np.float64)
    values = values[np.isfinite(values)]
    if values.size == 0:
        return None
    tail = (1 - confidence) / 2
    lower, upper = np.quantile(values, [tail, 1 - tail])
    return float(lower), float(upper)
