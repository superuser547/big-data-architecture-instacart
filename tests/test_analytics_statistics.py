from __future__ import annotations

import numpy as np

from instacart.analytics.statistics import (
    chi_square_summary,
    holm_adjust,
    log10_p_value,
    mcnemar_summary,
    wilson_interval,
)


def test_wilson_interval_and_empty_sample() -> None:
    interval = wilson_interval(50, 100)
    assert interval is not None
    assert interval[0] < 0.5 < interval[1]
    assert wilson_interval(0, 0) is None
    assert wilson_interval(0, 10)[0] == 0


def test_chi_square_effect_and_degenerate_contingency_tables() -> None:
    result = chi_square_summary([[10, 20], [20, 10]])
    assert result["observations"] == 60
    assert result["statistic"] > 0
    assert 0 < result["p_value"] < 1
    assert result["cramers_v"] > 0
    assert np.asarray(result["observed"]).shape == (2, 2)
    assert chi_square_summary([[0, 0], [0, 0]])["statistic"] is None
    assert chi_square_summary([[2, 0], [3, 0]])["p_value"] is None
    assert chi_square_summary([[0, 3], [0, 2]])["expected"] == [[0.0, 3.0], [0.0, 2.0]]


def test_mcnemar_and_holm_adjustment() -> None:
    result = mcnemar_summary([1, 1, 0, 0], [0, 1, 1, 0])
    assert result["observations"] == 4
    assert result["discordant_first_only"] == 1
    assert result["discordant_second_only"] == 1
    assert result["risk_difference"] == 0
    assert holm_adjust([0.01, 0.04, 0.03]) == [0.03, 0.06, 0.06]
    assert holm_adjust([]) == []
    assert log10_p_value(2_000, 2, 0.0) < -400
    assert log10_p_value(0, 2, 1.0) == 0
