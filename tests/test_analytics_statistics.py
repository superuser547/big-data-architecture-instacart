from __future__ import annotations

import numpy as np
from scipy.sparse import csr_matrix

from instacart.analytics.hypotheses import (
    _bootstrap_contingency,
    _bootstrap_pair_differences,
    _bootstrap_weights,
)
from instacart.analytics.statistics import (
    chi_square_summary,
    holm_adjust,
    log10_p_value,
    mcnemar_from_counts,
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


def test_cramers_v_finite_sample_bias_correction() -> None:
    weak = chi_square_summary([[255, 245], [245, 255]])
    assert weak["cramers_v_uncorrected"] > 0
    assert weak["cramers_v"] == 0
    strong = chi_square_summary([[90, 10], [10, 90]])
    assert 0 < strong["cramers_v"] < strong["cramers_v_uncorrected"]
    assert "Bergsma" in strong["cramers_v_correction"]


def test_customer_bootstrap_uses_full_population_and_is_reproducible() -> None:
    customer_counts = csr_matrix(
        np.asarray([[4, 1, 2, 3], [1, 4, 4, 1], [3, 2, 1, 4], [2, 3, 3, 2]])
    )
    first = _bootstrap_contingency(customer_counts, 2, 2, replicates=40, random_seed=2026)
    second = _bootstrap_contingency(customer_counts, 2, 2, replicates=40, random_seed=2026)
    assert first["sampled_customers"] == 4
    assert first["replicates"] == 40
    assert first["sampling_unit"] == "all customers; resampled with replacement"
    assert first["cramers_v_ci"] == second["cramers_v_ci"]
    assert first["category_rate_ci"] == second["category_rate_ci"]
    assert len(list(_bootstrap_weights(4, 3, 91))) == 3


def test_bootstrap_empty_population_has_no_invented_effect() -> None:
    result = _bootstrap_contingency(csr_matrix((0, 4)), 2, 2, replicates=20, random_seed=2026)
    assert result["sampled_customers"] == 0
    assert result["replicates"] == 0
    assert result["cramers_v_ci"] is None


def test_paired_bootstrap_handles_empty_and_bounded_risk_differences() -> None:
    empty = _bootstrap_pair_differences(np.zeros((3, 13), dtype=np.int64), 20, 2026)
    assert set(empty.values()) == {None}

    customers = np.zeros((2, 13), dtype=np.int64)
    customers[0, [0, 1, 4, 5, 7, 8, 9, 10]] = [1, 1, 1, 1, 1, 1, 1, 1]
    customers[1, [0, 3, 5, 6, 9, 10, 11, 12]] = [1, 1, -1, -1, 1, -1, 1, -1]
    intervals = _bootstrap_pair_differences(customers, 40, 2026)
    for interval in intervals.values():
        assert interval is not None
        assert -1 <= interval[0] <= interval[1] <= 1


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


def test_mcnemar_degenerate_and_empty_pairs() -> None:
    empty = mcnemar_summary([], [])
    assert empty["observations"] == 0
    assert empty["statistic"] is None
    assert empty["p_value"] is None
    assert empty["risk_difference"] is None

    identical = mcnemar_summary([True, False, True], [True, False, True])
    assert identical["discordant_first_only"] == 0
    assert identical["discordant_second_only"] == 0
    assert identical["statistic"] == 0
    assert identical["p_value"] == 1
    assert identical["risk_difference"] == 0
    assert mcnemar_from_counts(0, 0) == (0, 1)

    np.testing.assert_raises(ValueError, mcnemar_summary, [0, 1], [0])
    np.testing.assert_raises(ValueError, mcnemar_summary, [0, 2], [0, 1])
    np.testing.assert_raises(ValueError, mcnemar_from_counts, -1, 0)
