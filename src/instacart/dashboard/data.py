"""Read-only PostgreSQL access for the Streamlit dashboard."""

from __future__ import annotations

import math
import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from functools import wraps
from typing import Any

import psycopg
import streamlit as st
from psycopg.rows import dict_row


@dataclass(frozen=True, slots=True)
class Readiness:
    dashboard_ready: bool
    marts_ready: bool
    analysis_ready: bool
    dataset_fingerprint: str | None = None
    ingestion_run_id: str | None = None
    marts_run_id: str | None = None
    analysis_run_id: str | None = None

    @property
    def cache_key(self) -> tuple[str | None, str | None, str | None, str | None]:
        return (
            self.dataset_fingerprint,
            self.ingestion_run_id,
            self.marts_run_id,
            self.analysis_run_id,
        )


class PublicationChanged(RuntimeError):
    """Raised when a page's expected publication is no longer active."""


def _positive_setting(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError as exc:
        raise ValueError(f"{name} must be a positive integer") from exc
    if value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


@contextmanager
def readonly_connection(
    database_url: str, *, optimize_overview: bool = False
) -> Iterator[psycopg.Connection[Any]]:
    """Open a bounded repeatable-read, read-only transaction."""
    connect_timeout = _positive_setting("DASHBOARD_CONNECT_TIMEOUT_SECONDS", 5)
    statement_timeout = _positive_setting("DASHBOARD_STATEMENT_TIMEOUT_MS", 30_000)
    # Avoid repeated lineage probes; overview aggregation also avoids sorting all facts.
    options = f"-c statement_timeout={statement_timeout} -c enable_nestloop=off"
    if optimize_overview:
        options += " -c enable_sort=off"
    with psycopg.connect(
        database_url,
        row_factory=dict_row,
        connect_timeout=connect_timeout,
        options=options,
    ) as connection:
        connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        yield connection


def _readiness_from_row(row: dict[str, Any] | None) -> Readiness:
    if row is None:
        return Readiness(False, False, False)
    return Readiness(
        dashboard_ready=bool(row["dashboard_ready"]),
        marts_ready=bool(row["marts_ready"]),
        analysis_ready=bool(row["analysis_ready"]),
        dataset_fingerprint=row["dataset_fingerprint"],
        ingestion_run_id=str(row["active_ingestion_run_id"])
        if row["active_ingestion_run_id"]
        else None,
        marts_run_id=str(row["marts_run_id"]) if row["marts_run_id"] else None,
        analysis_run_id=str(row["analysis_run_id"]) if row["analysis_run_id"] else None,
    )


def _assert_publication(
    connection: psycopg.Connection[Any], expected: tuple[str, str, str, str]
) -> None:
    row = connection.execute(
        "SELECT dashboard_ready, marts_ready, analysis_ready, dataset_fingerprint, "
        "active_ingestion_run_id, marts_run_id, analysis_run_id "
        "FROM ops.analytics_readiness"
    ).fetchone()
    actual = _readiness_from_row(row)
    if not actual.dashboard_ready or actual.cache_key != expected:
        raise PublicationChanged(
            "Публикация аналитики изменилась во время чтения. Обновите страницу."
        )


def _assert_current_publication(database_url: str, expected: tuple[str, str, str, str]) -> None:
    with readonly_connection(database_url) as connection:
        _assert_publication(connection, expected)


def assert_publication_current(database_url: str, expected: Readiness) -> None:
    """Confirm that a fully rendered page still matches its initial publication."""
    if not expected.dashboard_ready:
        raise PublicationChanged("Актуальная публикация отсутствует. Обновите страницу.")
    _assert_current_publication(database_url, expected.cache_key)


def _validate_cached_publication(function):
    """Validate lineage on cache hits, misses, and after the cached read returns."""

    @wraps(function)
    def checked(
        database_url: str,
        dataset_fingerprint: str,
        ingestion_run_id: str,
        marts_run_id: str,
        analysis_run_id: str,
        *args: Any,
        **kwargs: Any,
    ):
        expected = _lineage_params(
            dataset_fingerprint, ingestion_run_id, marts_run_id, analysis_run_id
        )
        _assert_current_publication(database_url, expected)
        result = function(
            database_url,
            dataset_fingerprint,
            ingestion_run_id,
            marts_run_id,
            analysis_run_id,
            *args,
            **kwargs,
        )
        _assert_current_publication(database_url, expected)
        return result

    checked.clear = function.clear
    return checked


def get_readiness(database_url: str) -> Readiness:
    """Read the lightweight publication status without caching it."""
    with readonly_connection(database_url) as connection:
        row = connection.execute(
            "SELECT dashboard_ready, marts_ready, analysis_ready, dataset_fingerprint, "
            "active_ingestion_run_id, marts_run_id, analysis_run_id "
            "FROM ops.analytics_readiness"
        ).fetchone()
    return _readiness_from_row(row)


def _lineage_params(
    dataset_fingerprint: str,
    ingestion_run_id: str,
    marts_run_id: str,
    analysis_run_id: str,
) -> tuple[str, str, str, str]:
    return dataset_fingerprint, ingestion_run_id, marts_run_id, analysis_run_id


@_validate_cached_publication
@st.cache_data(ttl=45, max_entries=24, show_spinner=False)
def load_overview(
    database_url: str,
    dataset_fingerprint: str,
    ingestion_run_id: str,
    marts_run_id: str,
    analysis_run_id: str,
    eval_sets: tuple[str, ...],
    order_dows: tuple[int, ...],
    hours: tuple[int, ...],
    basket_min: int,
    basket_max: int,
) -> dict[str, Any]:
    """Load aggregate overview data with all-order and known-basket scopes separated."""
    lineage = _lineage_params(dataset_fingerprint, ingestion_run_id, marts_run_id, analysis_run_id)
    with readonly_connection(database_url, optimize_overview=True) as connection:
        _assert_publication(connection, lineage)
        overview = connection.execute(
            "SELECT * FROM mart.dataset_overview WHERE run_id = %s AND dataset_fingerprint = %s",
            (lineage[2], lineage[0]),
        ).fetchone()
        activity = connection.execute(
            "SELECT order_dow, order_hour_of_day, sum(order_count)::BIGINT AS order_count "
            "FROM mart.order_activity WHERE run_id = %s AND dataset_fingerprint = %s "
            "AND eval_set = ANY(%s) AND order_dow = ANY(%s) AND order_hour_of_day = ANY(%s) "
            "GROUP BY order_dow, order_hour_of_day ORDER BY order_dow, order_hour_of_day",
            (lineage[2], lineage[0], list(eval_sets), list(order_dows), list(hours)),
        ).fetchall()
        aggregates = connection.execute(
            "WITH grouped AS MATERIALIZED (SELECT "
            "GROUPING(basket_from) = 0 AS is_basket_group, "
            "GROUPING(interval_days) = 0 AS is_interval_group, basket_from, interval_days, "
            "count(*)::BIGINT AS order_count, "
            "coalesce(sum(basket_size), 0)::BIGINT AS item_count, "
            "coalesce(sum(repeat_count), 0)::BIGINT AS repeated_item_count, "
            "avg(basket_size)::DOUBLE PRECISION AS avg_basket_size "
            "FROM (SELECT basket_size, repeat_count, "
            "floor((basket_size - 1) / 5.0)::INTEGER * 5 + 1 AS basket_from, "
            "floor(days_since_prior_order)::INTEGER AS interval_days "
            "FROM mart.order_summary WHERE run_id = %s AND dataset_fingerprint = %s "
            "AND eval_set = ANY(%s) AND order_dow = ANY(%s) "
            "AND order_hour_of_day = ANY(%s) AND basket_size BETWEEN %s AND %s) filtered "
            "GROUP BY GROUPING SETS ((), (basket_from), (interval_days))), "
            "summary AS (SELECT jsonb_build_object("
            "'known_order_count', order_count, "
            "'item_count', item_count, 'repeated_item_count', repeated_item_count, "
            "'avg_basket_size', avg_basket_size) AS value FROM grouped "
            "WHERE NOT is_basket_group AND NOT is_interval_group), "
            "basket_counts AS (SELECT coalesce(jsonb_agg(jsonb_build_object("
            "'basket_from', basket_from, 'order_count', order_count) ORDER BY basket_from), "
            "'[]'::jsonb) AS value FROM grouped WHERE is_basket_group), "
            "intervals AS (SELECT coalesce(jsonb_agg(jsonb_build_object("
            "'interval_days', interval_days, 'order_count', order_count) ORDER BY interval_days), "
            "'[]'::jsonb) AS value FROM grouped WHERE is_interval_group "
            "AND interval_days IS NOT NULL) "
            "SELECT summary.value AS known_summary, basket_counts.value AS basket_counts, "
            "intervals.value AS intervals FROM summary CROSS JOIN basket_counts "
            "CROSS JOIN intervals",
            (
                lineage[2],
                lineage[0],
                list(eval_sets),
                list(order_dows),
                list(hours),
                basket_min,
                basket_max,
            ),
        ).fetchone()
        known_summary = aggregates["known_summary"]
        basket_counts = aggregates["basket_counts"]
        intervals = aggregates["intervals"]
    return {
        "overview": overview,
        "activity": activity,
        "known_summary": known_summary,
        "basket_counts": basket_counts,
        "intervals": intervals,
    }


@_validate_cached_publication
@st.cache_data(ttl=45, max_entries=24, show_spinner=False)
def load_category_data(
    database_url: str,
    dataset_fingerprint: str,
    ingestion_run_id: str,
    marts_run_id: str,
    analysis_run_id: str,
    eval_sets: tuple[str, ...],
    department_ids: tuple[int, ...],
    aisle_ids: tuple[int, ...],
    category_level: str,
    order_dows: tuple[int, ...],
    hours: tuple[int, ...],
    basket_min: int,
    basket_max: int,
    top_n: int,
    search_term: str = "",
    result_limit: int = 500,
) -> dict[str, Any]:
    """Load category popularity, products, and time/basket-aware category demand."""
    lineage = _lineage_params(dataset_fingerprint, ingestion_run_id, marts_run_id, analysis_run_id)
    if category_level not in {"department", "aisle"}:
        raise ValueError("category_level must be department or aisle")
    if result_limit <= 0:
        raise ValueError("result_limit must be a positive integer")
    with readonly_connection(database_url) as connection:
        _assert_publication(connection, lineage)
        options = connection.execute(
            "SELECT group_type, group_id, group_name, department_id, department_name "
            "FROM mart.department_reorders WHERE run_id = %s AND dataset_fingerprint = %s "
            "AND eval_set = 'prior' ORDER BY group_type, group_name",
            (lineage[2], lineage[0]),
        ).fetchall()
        popularity = connection.execute(
            "SELECT group_type, group_id, group_name, department_id, department_name, "
            "sum(item_count)::BIGINT AS item_count, "
            "sum(reorder_count)::BIGINT AS reorder_count, "
            "sum(order_count)::BIGINT AS order_count "
            "FROM mart.department_reorders "
            "WHERE run_id = %s AND dataset_fingerprint = %s AND eval_set = ANY(%s) "
            "AND (cardinality(%s::SMALLINT[]) = 0 OR department_id = ANY(%s)) "
            "AND (cardinality(%s::INTEGER[]) = 0 OR "
            "(group_type = 'aisle' AND group_id = ANY(%s))) "
            "GROUP BY group_type, group_id, group_name, department_id, department_name "
            "ORDER BY item_count DESC, group_name",
            (
                lineage[2],
                lineage[0],
                list(eval_sets),
                list(department_ids),
                list(department_ids),
                list(aisle_ids),
                list(aisle_ids),
            ),
        ).fetchall()
        product_query = (
            "SELECT product_id, product_name, aisle_id, aisle_name, department_id, "
            "department_name, sum(item_count)::BIGINT AS item_count, "
            "sum(reorder_count)::BIGINT AS reorder_count, "
            "sum(reorder_count)::DOUBLE PRECISION / NULLIF(sum(item_count), 0) AS reorder_rate, "
            "sum(order_count)::BIGINT AS order_count "
            "FROM mart.product_popularity WHERE run_id = %s AND dataset_fingerprint = %s "
            "AND eval_set = ANY(%s) AND product_name ILIKE %s "
            "AND (cardinality(%s::SMALLINT[]) = 0 OR department_id = ANY(%s)) "
            "AND (cardinality(%s::INTEGER[]) = 0 OR aisle_id = ANY(%s)) "
            "GROUP BY product_id, product_name, aisle_id, aisle_name, department_id, "
            "department_name ORDER BY item_count DESC, product_name LIMIT %s"
        )
        product_scope = (
            lineage[2],
            lineage[0],
            list(eval_sets),
        )
        product_filters = (
            list(department_ids),
            list(department_ids),
            list(aisle_ids),
            list(aisle_ids),
        )
        top_products = connection.execute(
            product_query,
            (*product_scope, "%%", *product_filters, int(top_n)),
        ).fetchall()
        products = connection.execute(
            product_query,
            (*product_scope, f"%{search_term.strip()}%", *product_filters, int(result_limit)),
        ).fetchall()
        if aisle_ids:
            department_rollups: dict[int, dict[str, Any]] = {}
            for row in popularity:
                department_id = int(row["department_id"])
                rollup = department_rollups.setdefault(
                    department_id,
                    {
                        "group_type": "department",
                        "group_id": department_id,
                        "group_name": row["department_name"],
                        "department_id": department_id,
                        "department_name": row["department_name"],
                        "item_count": 0,
                        "reorder_count": 0,
                        "order_count": None,
                    },
                )
                rollup["item_count"] += int(row["item_count"])
                rollup["reorder_count"] += int(row["reorder_count"])
            popularity.extend(department_rollups.values())
        rollup_department = bool(aisle_ids) and category_level == "department"
        activity_level = "aisle" if rollup_department else category_level
        activity_category_ids = list(aisle_ids) if aisle_ids else []
        category_activity = connection.execute(
            "SELECT CASE WHEN %s::BOOLEAN THEN department_id ELSE category_id END AS category_id, "
            "CASE WHEN %s::BOOLEAN THEN department_name ELSE category_name END AS category_name, "
            "sum(reorder_count)::BIGINT AS reorder_count, "
            "sum(item_count)::BIGINT AS item_count "
            "FROM mart.category_activity WHERE run_id = %s AND dataset_fingerprint = %s "
            "AND category_level = %s AND eval_set = ANY(%s) "
            "AND order_dow = ANY(%s) AND order_hour_of_day = ANY(%s) "
            "AND basket_size BETWEEN %s AND %s "
            "AND (cardinality(%s::SMALLINT[]) = 0 OR department_id = ANY(%s)) "
            "AND (cardinality(%s::INTEGER[]) = 0 OR category_id = ANY(%s)) "
            "GROUP BY 1, 2 ORDER BY item_count DESC, category_name",
            (
                rollup_department,
                rollup_department,
                lineage[2],
                lineage[0],
                activity_level,
                list(eval_sets),
                list(order_dows),
                list(hours),
                basket_min,
                basket_max,
                list(department_ids),
                list(department_ids),
                activity_category_ids,
                activity_category_ids,
            ),
        ).fetchall()
    return {
        "options": options,
        "popularity": popularity,
        "top_products": top_products,
        "products": products,
        "activity": category_activity,
    }


@_validate_cached_publication
@st.cache_data(ttl=45, max_entries=24, show_spinner=False)
def load_hypotheses(
    database_url: str,
    dataset_fingerprint: str,
    ingestion_run_id: str,
    marts_run_id: str,
    analysis_run_id: str,
    category_level: str,
    eval_sets: tuple[str, ...],
    cart_positions: tuple[int, ...],
    selected_category_id: int | None,
) -> dict[str, Any]:
    """Read published H1–H3 results and their descriptive category slices."""
    lineage = _lineage_params(dataset_fingerprint, ingestion_run_id, marts_run_id, analysis_run_id)
    if category_level not in {"department", "aisle"}:
        raise ValueError("category_level must be department or aisle")
    with readonly_connection(database_url) as connection:
        _assert_publication(connection, lineage)
        results = connection.execute(
            "SELECT hypothesis_id, h0, h1, method, observation_count, statistic, p_value, "
            "effect_name, effect_value, ci_lower, ci_upper, interpretation, metrics "
            "FROM mart.hypothesis_results WHERE run_id = %s AND dataset_fingerprint = %s "
            "ORDER BY hypothesis_id",
            (lineage[3], lineage[0]),
        ).fetchall()
        category_results = connection.execute(
            "SELECT hypothesis_id, category_level, category_id, category_name, item_count, "
            "reorder_count, reorder_rate, ci_lower, ci_upper, eval_set, cart_position "
            "FROM mart.hypothesis_categories WHERE run_id = %s AND dataset_fingerprint = %s "
            "AND ((hypothesis_id = %s AND category_level = %s AND eval_set = 'prior+train' "
            "AND cart_position = 0) OR (hypothesis_id = 'H3_category' "
            "AND category_level = %s AND eval_set = ANY(%s) "
            "AND cart_position = ANY(%s))) "
            "AND (hypothesis_id <> 'H3_category' OR %s::INTEGER IS NULL OR category_id = %s) "
            "ORDER BY hypothesis_id, category_name, eval_set, cart_position",
            (
                lineage[3],
                lineage[0],
                "H2_aisle" if category_level == "aisle" else "H2_department",
                category_level,
                category_level,
                list(eval_sets),
                list(cart_positions),
                selected_category_id,
                selected_category_id,
            ),
        ).fetchall()
        pairs = connection.execute(
            "SELECT comparison, first_position, second_position, department_matched, "
            "order_count, discordant_first_only, discordant_second_only, risk_difference, "
            "ci_lower, ci_upper, p_value, p_adjusted FROM mart.hypothesis_pairs "
            "WHERE run_id = %s AND dataset_fingerprint = %s "
            "ORDER BY department_matched, first_position, second_position",
            (lineage[3], lineage[0]),
        ).fetchall()
        position_stats = connection.execute(
            "SELECT cart_position, sum(item_count)::BIGINT AS item_count, "
            "sum(reorder_count)::BIGINT AS reorder_count "
            "FROM mart.cart_position_stats WHERE run_id = %s AND dataset_fingerprint = %s "
            "AND eval_set = ANY(%s) AND category_level = '' "
            "GROUP BY cart_position ORDER BY cart_position",
            (lineage[2], lineage[0], list(eval_sets)),
        ).fetchall()
    return {
        "results": results,
        "categories": category_results,
        "pairs": pairs,
        "position_stats": position_stats,
    }


@_validate_cached_publication
@st.cache_data(ttl=45, max_entries=24, show_spinner=False)
def load_h1_slice(
    database_url: str,
    dataset_fingerprint: str,
    ingestion_run_id: str,
    marts_run_id: str,
    analysis_run_id: str,
    eval_sets: tuple[str, ...],
    order_dows: tuple[int, ...],
    hours: tuple[int, ...],
) -> list[dict[str, Any]]:
    """Load a descriptive time slice; no stored statistical test is recomputed."""
    lineage = _lineage_params(dataset_fingerprint, ingestion_run_id, marts_run_id, analysis_run_id)
    with readonly_connection(database_url) as connection:
        _assert_publication(connection, lineage)
        return connection.execute(
            "SELECT eval_set, order_dow, order_hour_of_day, "
            "sum(order_count)::BIGINT AS order_count "
            "FROM mart.order_activity WHERE run_id = %s AND dataset_fingerprint = %s "
            "AND eval_set = ANY(%s) AND order_dow = ANY(%s) AND order_hour_of_day = ANY(%s) "
            "GROUP BY eval_set, order_dow, order_hour_of_day "
            "ORDER BY eval_set, order_dow, order_hour_of_day",
            (lineage[2], lineage[0], list(eval_sets), list(order_dows), list(hours)),
        ).fetchall()


@_validate_cached_publication
@st.cache_data(ttl=45, max_entries=24, show_spinner=False)
def load_segments(
    database_url: str,
    dataset_fingerprint: str,
    ingestion_run_id: str,
    marts_run_id: str,
    analysis_run_id: str,
    sample_limit: int = 12_000,
) -> dict[str, Any]:
    """Read full population summaries and a deterministic, cluster-balanced PCA sample."""
    lineage = _lineage_params(dataset_fingerprint, ingestion_run_id, marts_run_id, analysis_run_id)
    if sample_limit <= 0:
        raise ValueError("sample_limit must be a positive integer")
    with readonly_connection(database_url) as connection:
        _assert_publication(connection, lineage)
        profiles = connection.execute(
            "SELECT cluster_id, customer_count, feature_means, transformed_center "
            "FROM mart.cluster_profiles WHERE run_id = %s AND dataset_fingerprint = %s "
            "ORDER BY cluster_id",
            (lineage[3], lineage[0]),
        ).fetchall()
        metrics = connection.execute(
            "SELECT k, inertia, silhouette, silhouette_sample_size, cluster_sizes, selected "
            "FROM mart.cluster_metrics WHERE run_id = %s AND dataset_fingerprint = %s "
            "ORDER BY k",
            (lineage[3], lineage[0]),
        ).fetchall()
        analysis = connection.execute(
            "SELECT results->'clustering'->>'selection_basis' AS selection_basis, "
            "results->'clustering'->'transform'->'pca_explained_variance_ratio' "
            "AS pca_explained_variance_ratio "
            "FROM ops.analytics_runs WHERE analytics_run_id = %s "
            "AND dataset_fingerprint = %s AND ingestion_run_id = %s AND marts_run_id = %s "
            "AND run_type = 'analysis' AND status = 'succeeded'",
            (lineage[3], lineage[0], lineage[1], lineage[2]),
        ).fetchone()
        population_size = sum(int(row["customer_count"]) for row in profiles)
        cluster_count = len(profiles)
        if sample_limit < cluster_count:
            raise ValueError("sample_limit cannot be smaller than the number of segments")
        representatives = connection.execute(
            "SELECT DISTINCT ON (cluster_id) user_id, cluster_id, pca_1, pca_2 "
            "FROM mart.customer_segments WHERE run_id = %s AND dataset_fingerprint = %s "
            "ORDER BY cluster_id, user_id",
            (lineage[3], lineage[0]),
        ).fetchall()
        if {int(row["cluster_id"]) for row in representatives} != {
            int(row["cluster_id"]) for row in profiles
        }:
            raise PublicationChanged("В публикации отсутствуют точки одного из сегментов PCA.")
        sample_slots = sample_limit - len(representatives)
        points = list(representatives)
        if sample_slots:
            sample_stride = max(1, math.ceil(population_size / sample_slots))
            points.extend(
                connection.execute(
                    "SELECT user_id, cluster_id, pca_1, pca_2 "
                    "FROM mart.customer_segments WHERE run_id = %s "
                    "AND dataset_fingerprint = %s AND mod(user_id, %s) = 0 "
                    "AND NOT (user_id = ANY(%s)) "
                    "ORDER BY hashint4(user_id), user_id LIMIT %s",
                    (
                        lineage[3],
                        lineage[0],
                        sample_stride,
                        [int(row["user_id"]) for row in representatives],
                        sample_slots,
                    ),
                ).fetchall()
            )
    selected = next((row["k"] for row in metrics if row["selected"]), None)
    return {
        "profiles": profiles,
        "metrics": metrics,
        "points": points,
        "population_size": population_size,
        "selected_k": selected,
        "selection_basis": analysis["selection_basis"] if analysis else None,
        "pca_explained_variance_ratio": analysis["pca_explained_variance_ratio"]
        if analysis
        else None,
        "displayed_sample_size": len(points),
        "sample_limit": sample_limit,
    }
