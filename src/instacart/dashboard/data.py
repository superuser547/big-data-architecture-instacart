"""Read-only PostgreSQL access for the Streamlit dashboard."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
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


@contextmanager
def readonly_connection(database_url: str) -> Iterator[psycopg.Connection[Any]]:
    """Open a repeatable-read, read-only transaction for a consistent page load."""
    with psycopg.connect(database_url, row_factory=dict_row) as connection:
        connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        yield connection


def get_readiness(database_url: str) -> Readiness:
    """Read the lightweight publication status without caching it."""
    with readonly_connection(database_url) as connection:
        row = connection.execute(
            "SELECT dashboard_ready, marts_ready, analysis_ready, dataset_fingerprint, "
            "active_ingestion_run_id, marts_run_id, analysis_run_id "
            "FROM ops.analytics_readiness"
        ).fetchone()
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


def _lineage_params(
    dataset_fingerprint: str,
    ingestion_run_id: str,
    marts_run_id: str,
    analysis_run_id: str,
) -> tuple[str, str, str, str]:
    return dataset_fingerprint, ingestion_run_id, marts_run_id, analysis_run_id


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
    with readonly_connection(database_url) as connection:
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
        known_filter = (
            lineage[2],
            lineage[0],
            list(eval_sets),
            list(order_dows),
            list(hours),
            basket_min,
            basket_max,
        )
        known_summary = connection.execute(
            "SELECT count(*)::BIGINT AS known_order_count, "
            "count(DISTINCT user_id)::BIGINT AS buyer_count, "
            "coalesce(sum(basket_size), 0)::BIGINT AS item_count, "
            "coalesce(sum(repeat_count), 0)::BIGINT AS repeated_item_count, "
            "avg(basket_size)::DOUBLE PRECISION AS avg_basket_size "
            "FROM mart.order_summary WHERE run_id = %s AND dataset_fingerprint = %s "
            "AND eval_set = ANY(%s) AND order_dow = ANY(%s) "
            "AND order_hour_of_day = ANY(%s) AND basket_size BETWEEN %s AND %s",
            known_filter,
        ).fetchone()
        basket_counts = connection.execute(
            "SELECT (floor((basket_size - 1) / 5.0)::INTEGER * 5 + 1) AS basket_from, "
            "count(*)::BIGINT AS order_count FROM mart.order_summary "
            "WHERE run_id = %s AND dataset_fingerprint = %s AND eval_set = ANY(%s) "
            "AND order_dow = ANY(%s) AND order_hour_of_day = ANY(%s) "
            "AND basket_size BETWEEN %s AND %s GROUP BY basket_from ORDER BY basket_from",
            known_filter,
        ).fetchall()
        intervals = connection.execute(
            "SELECT floor(days_since_prior_order)::INTEGER AS interval_days, "
            "count(*)::BIGINT AS order_count FROM mart.order_summary "
            "WHERE run_id = %s AND dataset_fingerprint = %s AND eval_set = ANY(%s) "
            "AND order_dow = ANY(%s) AND order_hour_of_day = ANY(%s) "
            "AND basket_size BETWEEN %s AND %s AND days_since_prior_order IS NOT NULL "
            "GROUP BY interval_days ORDER BY interval_days",
            known_filter,
        ).fetchall()
    return {
        "overview": overview,
        "activity": activity,
        "known_summary": known_summary,
        "basket_counts": basket_counts,
        "intervals": intervals,
    }


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
) -> dict[str, Any]:
    """Load category popularity, products, and time/basket-aware category demand."""
    lineage = _lineage_params(dataset_fingerprint, ingestion_run_id, marts_run_id, analysis_run_id)
    if category_level not in {"department", "aisle"}:
        raise ValueError("category_level must be department or aisle")
    with readonly_connection(database_url) as connection:
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
            "AND (group_type = 'department' OR cardinality(%s::INTEGER[]) = 0 "
            "OR group_id = ANY(%s)) "
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
        products = connection.execute(
            "SELECT product_id, product_name, aisle_id, aisle_name, department_id, "
            "department_name, sum(item_count)::BIGINT AS item_count, "
            "sum(reorder_count)::BIGINT AS reorder_count, "
            "sum(order_count)::BIGINT AS order_count "
            "FROM mart.product_popularity WHERE run_id = %s AND dataset_fingerprint = %s "
            "AND eval_set = ANY(%s) "
            "AND (cardinality(%s::SMALLINT[]) = 0 OR department_id = ANY(%s)) "
            "AND (cardinality(%s::INTEGER[]) = 0 OR aisle_id = ANY(%s)) "
            "GROUP BY product_id, product_name, aisle_id, aisle_name, department_id, "
            "department_name ORDER BY item_count DESC, product_name LIMIT %s",
            (
                lineage[2],
                lineage[0],
                list(eval_sets),
                list(department_ids),
                list(department_ids),
                list(aisle_ids),
                list(aisle_ids),
                top_n,
            ),
        ).fetchall()
        category_activity = connection.execute(
            "SELECT category_id, category_name, sum(item_count)::BIGINT AS item_count, "
            "sum(reorder_count)::BIGINT AS reorder_count "
            "FROM mart.category_activity WHERE run_id = %s AND dataset_fingerprint = %s "
            "AND category_level = %s AND eval_set = ANY(%s) "
            "AND order_dow = ANY(%s) AND order_hour_of_day = ANY(%s) "
            "AND basket_size BETWEEN %s AND %s "
            "AND (cardinality(%s::SMALLINT[]) = 0 OR department_id = ANY(%s)) "
            "AND (category_level <> 'aisle' OR cardinality(%s::INTEGER[]) = 0 "
            "OR category_id = ANY(%s)) "
            "GROUP BY category_id, category_name ORDER BY item_count DESC, category_name",
            (
                lineage[2],
                lineage[0],
                category_level,
                list(eval_sets),
                list(order_dows),
                list(hours),
                basket_min,
                basket_max,
                list(department_ids),
                list(department_ids),
                list(aisle_ids),
                list(aisle_ids),
            ),
        ).fetchall()
    return {
        "options": options,
        "popularity": popularity,
        "products": products,
        "activity": category_activity,
    }


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
    return {"results": results, "categories": category_results, "pairs": pairs}


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
        return connection.execute(
            "SELECT eval_set, order_dow, order_hour_of_day, "
            "sum(order_count)::BIGINT AS order_count "
            "FROM mart.order_activity WHERE run_id = %s AND dataset_fingerprint = %s "
            "AND eval_set = ANY(%s) AND order_dow = ANY(%s) AND order_hour_of_day = ANY(%s) "
            "GROUP BY eval_set, order_dow, order_hour_of_day "
            "ORDER BY eval_set, order_dow, order_hour_of_day",
            (lineage[2], lineage[0], list(eval_sets), list(order_dows), list(hours)),
        ).fetchall()


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
    with readonly_connection(database_url) as connection:
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
        points = connection.execute(
            "WITH cluster_count AS (SELECT count(DISTINCT cluster_id) AS count "
            "FROM mart.customer_segments WHERE run_id = %s AND dataset_fingerprint = %s), "
            "ranked AS (SELECT cluster_id, pca_1, pca_2, user_id, "
            "row_number() OVER (PARTITION BY cluster_id ORDER BY "
            "hashtextextended(user_id::TEXT, 2026), user_id) AS sample_rank, "
            "greatest(1, %s / cluster_count.count) AS per_cluster "
            "FROM mart.customer_segments CROSS JOIN cluster_count "
            "WHERE run_id = %s AND dataset_fingerprint = %s) "
            "SELECT cluster_id, pca_1, pca_2 FROM ranked "
            "WHERE sample_rank <= per_cluster ORDER BY cluster_id, sample_rank",
            (lineage[3], lineage[0], sample_limit, lineage[3], lineage[0]),
        ).fetchall()
    selected = next((row["k"] for row in metrics if row["selected"]), None)
    return {
        "profiles": profiles,
        "metrics": metrics,
        "points": points,
        "population_size": sum(int(row["customer_count"]) for row in profiles),
        "selected_k": selected,
        "selection_basis": analysis["selection_basis"] if analysis else None,
        "pca_explained_variance_ratio": analysis["pca_explained_variance_ratio"]
        if analysis
        else None,
        "displayed_sample_size": len(points),
        "sample_limit": sample_limit,
    }
