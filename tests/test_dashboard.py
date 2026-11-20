from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import plotly.graph_objects as go
import pytest

from instacart.dashboard import data
from instacart.dashboard.data import Readiness
from instacart.dashboard.metrics import (
    format_p_value,
    format_percent,
    format_percentage_points,
    interval_text,
    lineage_arguments,
    percentage_point_interval,
    ratio,
    resolve_parent_departments,
)


class _Result:
    def __init__(
        self, *, one: dict[str, Any] | None = None, many: list[dict[str, Any]] | None = None
    ):
        self.one = one
        self.many = many or []

    def fetchone(self) -> dict[str, Any] | None:
        return self.one

    def fetchall(self) -> list[dict[str, Any]]:
        return self.many


class _Connection:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...] | None]] = []
        self.readiness_row: dict[str, Any] | None = {
            "dashboard_ready": True,
            "marts_ready": True,
            "analysis_ready": True,
            "dataset_fingerprint": "sha256:abc",
            "active_ingestion_run_id": "ingestion-1",
            "marts_run_id": "marts-1",
            "analysis_run_id": "analysis-1",
        }

    def execute(self, sql: str, params: tuple[Any, ...] | None = None) -> _Result:
        self.calls.append((sql, params))
        if "FROM ops.analytics_readiness" in sql:
            return _Result(one=self.readiness_row)
        if "mart.dataset_overview" in sql:
            return _Result(one={"order_count": 2})
        if "mart.order_activity" in sql:
            return _Result(many=[{"order_dow": 0, "order_hour_of_day": 9, "order_count": 2}])
        if "'known_order_count'" in sql:
            return _Result(
                one={
                    "known_summary": {
                        "known_order_count": 1,
                        "buyer_count": 1,
                        "item_count": 2,
                        "repeated_item_count": 1,
                        "avg_basket_size": 2.0,
                    },
                    "basket_counts": [{"basket_from": 1, "order_count": 1}],
                    "intervals": [{"interval_days": 7, "order_count": 1}],
                }
            )
        return _Result(many=[])


def _fixture_readiness(**overrides: Any) -> Readiness:
    values = {
        "dashboard_ready": True,
        "marts_ready": True,
        "analysis_ready": True,
        "dataset_fingerprint": "sha256:abc",
        "ingestion_run_id": "ingestion-1",
        "marts_run_id": "marts-1",
        "analysis_run_id": "analysis-1",
    }
    values.update(overrides)
    return Readiness(**values)


def test_readonly_connection_starts_a_repeatable_read_transaction(monkeypatch) -> None:
    connection = _Connection()
    connect_args = {}

    class ConnectionContext:
        def __enter__(self):
            return connection

        def __exit__(self, *_args):
            return False

    def connect(*_args, **kwargs):
        connect_args.update(kwargs)
        return ConnectionContext()

    monkeypatch.setenv("DASHBOARD_CONNECT_TIMEOUT_SECONDS", "7")
    monkeypatch.setenv("DASHBOARD_STATEMENT_TIMEOUT_MS", "12000")
    monkeypatch.setattr(data.psycopg, "connect", connect)
    with data.readonly_connection("postgresql://example") as returned:
        assert returned is connection
    assert connection.calls[0][0] == "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"
    assert connect_args["connect_timeout"] == 7
    assert connect_args["options"] == "-c statement_timeout=12000 -c enable_nestloop=off"


def test_readiness_without_active_import_is_not_ready(monkeypatch) -> None:
    connection = _Connection()
    connection.readiness_row = None
    monkeypatch.setattr(data, "readonly_connection", lambda _url: _readiness_context(connection))

    assert data.get_readiness("postgresql://example") == Readiness(False, False, False)


@contextmanager
def _readiness_context(connection: _Connection) -> Iterator[_Connection]:
    yield connection


def test_readiness_contains_the_complete_cache_lineage(monkeypatch) -> None:
    connection = _Connection()

    def execute(sql: str, params: tuple[Any, ...] | None = None) -> _Result:
        connection.calls.append((sql, params))
        return _Result(
            one={
                "dashboard_ready": True,
                "marts_ready": True,
                "analysis_ready": True,
                "dataset_fingerprint": "sha256:abc",
                "active_ingestion_run_id": "ingestion-1",
                "marts_run_id": "marts-1",
                "analysis_run_id": "analysis-1",
            }
        )

    connection.execute = execute  # type: ignore[method-assign]
    monkeypatch.setattr(data, "readonly_connection", lambda _url: _readiness_context(connection))
    result = data.get_readiness("postgresql://example")

    assert result.dashboard_ready
    assert result.cache_key == ("sha256:abc", "ingestion-1", "marts-1", "analysis-1")
    assert (
        len(
            {
                result.cache_key,
                _fixture_readiness(analysis_run_id="analysis-2").cache_key,
                _fixture_readiness(marts_run_id="marts-2").cache_key,
                _fixture_readiness(ingestion_run_id="ingestion-2").cache_key,
                _fixture_readiness(dataset_fingerprint="sha256:def").cache_key,
            }
        )
        == 5
    )


def test_overview_uses_parameterized_filters_and_full_lineage_cache_key(monkeypatch) -> None:
    connection = _Connection()
    contexts: list[_Connection] = []
    optimized_contexts: list[bool] = []

    @contextmanager
    def readonly(_url: str, *, optimize_overview: bool = False) -> Iterator[_Connection]:
        optimized_contexts.append(optimize_overview)
        contexts.append(connection)
        yield connection

    monkeypatch.setattr(data, "readonly_connection", readonly)
    data.load_overview.clear()
    args = (
        "postgresql://example",
        "sha256:abc",
        "ingestion-1",
        "marts-1",
        "analysis-1",
        ("prior", "train"),
        (0, 2),
        (9, 18),
        5,
        40,
    )
    first = data.load_overview(*args)
    data.load_overview(*args)
    connection.readiness_row = {**connection.readiness_row, "analysis_run_id": "analysis-2"}
    data.load_overview(*args[:4], "analysis-2", *args[5:])

    assert first["known_summary"]["known_order_count"] == 1
    assert len(contexts) == 8
    assert optimized_contexts.count(True) == 2
    activity_sql, activity_params = next(
        call for call in connection.calls if "mart.order_activity" in call[0]
    )
    assert "eval_set = ANY(%s)" in activity_sql
    assert activity_params == ("marts-1", "sha256:abc", ["prior", "train"], [0, 2], [9, 18])
    assert "prior" not in activity_sql
    assert "WHERE run_id = %s AND dataset_fingerprint = %s" in activity_sql
    aggregate_sql = next(call[0] for call in connection.calls if "GROUPING SETS" in call[0])
    assert aggregate_sql.count("FROM mart.order_summary") == 1


def test_category_filters_are_sent_as_query_parameters(monkeypatch) -> None:
    connection = _Connection()

    @contextmanager
    def readonly(_url: str, **_kwargs: Any) -> Iterator[_Connection]:
        yield connection

    monkeypatch.setattr(data, "readonly_connection", readonly)
    data.load_category_data.clear()
    data.load_category_data(
        "postgresql://example",
        "sha256:abc",
        "ingestion-1",
        "marts-1",
        "analysis-1",
        ("prior", "train"),
        (4,),
        (12,),
        "aisle",
        (0, 1),
        (10, 11),
        5,
        40,
        25,
        "apples",
        500,
    )

    product_sql, product_params = next(
        call for call in connection.calls if "mart.product_popularity" in call[0]
    )
    assert "department_id = ANY(%s)" in product_sql
    assert "aisle_id = ANY(%s)" in product_sql
    assert product_params[-1] == 25
    search_sql, search_params = next(
        call
        for call in connection.calls
        if "product_name ILIKE" in call[0] and call[1][3] == "%apples%"
    )
    assert search_sql == product_sql
    assert search_params[3] == "%apples%"
    assert search_params[-1] == 500
    popularity_sql = next(
        call[0]
        for call in connection.calls
        if "FROM mart.department_reorders" in call[0] and "sum(item_count)" in call[0]
    )
    assert "group_type = 'aisle' AND group_id = ANY(%s)" in popularity_sql
    category_sql, category_params = next(
        call for call in connection.calls if "mart.category_activity" in call[0]
    )
    assert "basket_size BETWEEN %s AND %s" in category_sql
    assert "category_id = ANY(%s)" in category_sql
    assert category_params[2:10] == (
        "marts-1",
        "sha256:abc",
        "aisle",
        ["prior", "train"],
        [0, 1],
        [10, 11],
        5,
        40,
    )
    assert category_params[-2:] == ([12], [12])


def test_descriptive_cart_position_stats_use_marts_lineage_and_sum_all_basket_sizes(
    monkeypatch,
) -> None:
    connection = _Connection()

    @contextmanager
    def readonly(_url: str) -> Iterator[_Connection]:
        yield connection

    monkeypatch.setattr(data, "readonly_connection", readonly)
    data.load_hypotheses.clear()
    data.load_hypotheses(
        "postgresql://example",
        "sha256:abc",
        "ingestion-1",
        "marts-1",
        "analysis-1",
        "department",
        ("prior", "train"),
        (1, 10, 20),
        None,
    )

    sql, params = next(call for call in connection.calls if "mart.cart_position_stats" in call[0])
    assert "category_level = ''" in sql
    assert "basket_size = 0" not in sql
    assert params == ("marts-1", "sha256:abc", ["prior", "train"])


def test_segment_sample_is_deterministic_and_adds_missing_clusters(monkeypatch) -> None:
    connection = _Connection()

    def execute(sql: str, params: tuple[Any, ...] | None = None) -> _Result:
        connection.calls.append((sql, params))
        if "FROM ops.analytics_readiness" in sql:
            return _Result(one=connection.readiness_row)
        if "mart.cluster_profiles" in sql:
            return _Result(
                many=[
                    {"cluster_id": 0, "customer_count": 6},
                    {"cluster_id": 1, "customer_count": 4},
                ]
            )
        if "mart.cluster_metrics" in sql:
            return _Result(many=[])
        if "ops.analytics_runs" in sql:
            return _Result(one={"selection_basis": "sample", "pca_explained_variance_ratio": []})
        if "DISTINCT ON (cluster_id)" in sql:
            return _Result(
                many=[
                    {"user_id": 1, "cluster_id": 0, "pca_1": 0.2, "pca_2": 0.3},
                    {"user_id": 2, "cluster_id": 1, "pca_1": 0.8, "pca_2": 0.9},
                ]
            )
        if "mod(user_id, %s) = 0" in sql:
            return _Result(
                many=[
                    {"user_id": 4, "cluster_id": 0, "pca_1": 0.4, "pca_2": 0.5},
                    {"user_id": 6, "cluster_id": 1, "pca_1": 0.6, "pca_2": 0.7},
                    {"user_id": 8, "cluster_id": 0, "pca_1": 0.7, "pca_2": 0.8},
                ]
            )
        return _Result(many=[])

    connection.execute = execute  # type: ignore[method-assign]

    @contextmanager
    def readonly(_url: str) -> Iterator[_Connection]:
        yield connection

    monkeypatch.setattr(data, "readonly_connection", readonly)
    data.load_segments.clear()
    result = data.load_segments(
        "postgresql://example",
        "sha256:abc",
        "ingestion-1",
        "marts-1",
        "analysis-1",
        sample_limit=5,
    )

    assert result["population_size"] == 10
    assert result["displayed_sample_size"] == 5
    assert result["displayed_sample_size"] <= result["sample_limit"]
    assert {row["cluster_id"] for row in result["points"]} == {0, 1}
    sample_sql, sample_params = next(call for call in connection.calls if "mod(user_id" in call[0])
    assert "run_id = %s AND dataset_fingerprint = %s" in sample_sql
    assert "ORDER BY hashint4(user_id), user_id LIMIT %s" in sample_sql
    assert sample_params == ("analysis-1", "sha256:abc", 4, [1, 2], 3)


def test_display_formatting_handles_missing_values_and_small_probabilities() -> None:
    assert format_percent(0.25) == "25,0%"
    assert format_percent(None) == "нет данных"
    assert format_percentage_points(0.02) == "+2,0 п.п."
    assert format_p_value(0) == "< 1e-300 (ниже точности float)"
    assert format_p_value(0, -325.4) == "< 1e-300 (log₁₀ p = -325,4)"
    assert format_p_value(None) == "не оценено"
    assert interval_text(None, 0.2) == "не оценён"
    assert percentage_point_interval(-0.1, 0.2) == "[-10,0 п.п.; +20,0 п.п.]"
    assert ratio(3, 0) is None


def test_lineage_arguments_rejects_partial_or_stale_publication() -> None:
    assert lineage_arguments(_fixture_readiness()) == (
        "sha256:abc",
        "ingestion-1",
        "marts-1",
        "analysis-1",
    )
    try:
        lineage_arguments(_fixture_readiness(analysis_run_id=None))
    except ValueError as exc:
        assert "цепочка" in str(exc)
    else:
        raise AssertionError("partial lineage must not be used for a cached read")


def test_cached_result_is_rejected_when_publication_changes(monkeypatch) -> None:
    connection = _Connection()

    @contextmanager
    def readonly(_url: str, **_kwargs: Any) -> Iterator[_Connection]:
        yield connection

    monkeypatch.setattr(data, "readonly_connection", readonly)
    data.load_overview.clear()
    args = (
        "postgresql://example",
        "sha256:abc",
        "ingestion-1",
        "marts-1",
        "analysis-1",
        ("prior",),
        (0,),
        (9,),
        1,
        40,
    )
    data.load_overview(*args)
    connection.readiness_row = {**connection.readiness_row, "analysis_run_id": "analysis-2"}

    with pytest.raises(data.PublicationChanged):
        data.load_overview(*args)


def test_displayed_confidence_limits_keep_actual_bounds() -> None:
    from instacart.dashboard.pages.hypotheses import _add_interval_lines

    figure = go.Figure()
    rows = [
        {
            "name": "estimate outside interval",
            "estimate": 0.5,
            "lower": 0.2,
            "upper": 0.4,
        },
        {"name": "no interval", "estimate": 0.6, "lower": None, "upper": None},
    ]
    missing = _add_interval_lines(
        figure,
        rows,
        category_key="name",
        estimate_key="estimate",
        lower_key="lower",
        upper_key="upper",
        scale=100,
    )

    assert missing == 1
    assert len(figure.data) == 2
    assert list(figure.data[0].x) == [20.0, 40.0, None]
    assert list(figure.data[1].x) == [50.0, 60.0]


def test_displayed_confidence_limits_without_intervals_show_markers_only() -> None:
    from instacart.dashboard.pages.hypotheses import _add_interval_lines

    figure = go.Figure()
    missing = _add_interval_lines(
        figure,
        [{"name": "estimate", "estimate": 0.3, "lower": None, "upper": None}],
        category_key="name",
        estimate_key="estimate",
        lower_key="lower",
        upper_key="upper",
        scale=100,
    )

    assert missing == 1
    assert len(figure.data) == 1
    assert list(figure.data[0].x) == [30.0]


def test_aisle_filter_narrows_department_measures_to_parent_departments() -> None:
    mapping = {30: 1, 31: 2}
    assert resolve_parent_departments((1, 2), (30,), mapping) == (1,)
    assert resolve_parent_departments((), (31,), mapping) == (2,)
    assert resolve_parent_departments((1, 2), (), mapping) == (1, 2)
