from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

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

    def execute(self, sql: str, params: tuple[Any, ...] | None = None) -> _Result:
        self.calls.append((sql, params))
        if "mart.dataset_overview" in sql:
            return _Result(one={"order_count": 2})
        if "mart.order_activity" in sql:
            return _Result(many=[{"order_dow": 0, "order_hour_of_day": 9, "order_count": 2}])
        if "count(*)::BIGINT AS known_order_count" in sql:
            return _Result(
                one={
                    "known_order_count": 1,
                    "buyer_count": 1,
                    "item_count": 2,
                    "repeated_item_count": 1,
                    "avg_basket_size": 2.0,
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

    class ConnectionContext:
        def __enter__(self):
            return connection

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(data.psycopg, "connect", lambda *_args, **_kwargs: ConnectionContext())
    with data.readonly_connection("postgresql://example") as returned:
        assert returned is connection
    assert connection.calls[0][0] == "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"


def test_readiness_without_active_import_is_not_ready(monkeypatch) -> None:
    connection = _Connection()
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

    @contextmanager
    def readonly(_url: str) -> Iterator[_Connection]:
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
    data.load_overview(*args[:4], "analysis-2", *args[5:])

    assert first["known_summary"]["known_order_count"] == 1
    assert len(contexts) == 2
    activity_sql, activity_params = next(
        call for call in connection.calls if "mart.order_activity" in call[0]
    )
    assert "eval_set = ANY(%s)" in activity_sql
    assert activity_params == ("marts-1", "sha256:abc", ["prior", "train"], [0, 2], [9, 18])
    assert "prior" not in activity_sql
    assert "WHERE run_id = %s AND dataset_fingerprint = %s" in activity_sql


def test_category_filters_are_sent_as_query_parameters(monkeypatch) -> None:
    connection = _Connection()

    @contextmanager
    def readonly(_url: str) -> Iterator[_Connection]:
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
    )

    product_sql, product_params = next(
        call for call in connection.calls if "mart.product_popularity" in call[0]
    )
    assert "department_id = ANY(%s)" in product_sql
    assert "aisle_id = ANY(%s)" in product_sql
    assert product_params[-1] == 25
    category_sql, category_params = next(
        call for call in connection.calls if "mart.category_activity" in call[0]
    )
    assert "basket_size BETWEEN %s AND %s" in category_sql
    assert "category_id = ANY(%s)" in category_sql
    assert category_params[2:8] == ("aisle", ["prior", "train"], [0, 1], [10, 11], 5, 40)
    assert category_params[-2:] == ([12], [12])


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


def test_aisle_filter_narrows_department_measures_to_parent_departments() -> None:
    mapping = {30: 1, 31: 2}
    assert resolve_parent_departments((1, 2), (30,), mapping) == (1,)
    assert resolve_parent_departments((), (31,), mapping) == (2,)
    assert resolve_parent_departments((1, 2), (), mapping) == (1, 2)
