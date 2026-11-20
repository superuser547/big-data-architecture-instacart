from __future__ import annotations

import pytest

from instacart.analytics.clustering import (
    _candidate_k_values,
    _silhouette_is_estimable,
)


def test_candidate_clusters_respect_population_and_distinct_features() -> None:
    assert list(_candidate_k_values(12, 12)) == list(range(2, 9))
    assert list(_candidate_k_values(4, 2)) == [2]
    assert list(_candidate_k_values(5, 4)) == [2, 3, 4]


@pytest.mark.parametrize(
    ("customer_count", "distinct_count"),
    [(0, 0), (1, 1), (2, 2), (10, 1)],
)
def test_candidate_clusters_reject_unestimable_populations(
    customer_count: int, distinct_count: int
) -> None:
    with pytest.raises(ValueError):
        _candidate_k_values(customer_count, distinct_count)


def test_silhouette_requires_two_labels_and_fewer_labels_than_rows() -> None:
    assert _silhouette_is_estimable(2, 3)
    assert not _silhouette_is_estimable(1, 3)
    assert not _silhouette_is_estimable(3, 3)
