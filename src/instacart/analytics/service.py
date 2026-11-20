"""Transactional orchestration for marts and statistical analyses."""

from __future__ import annotations

import time
import uuid
from datetime import UTC, datetime
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from instacart.analytics.clustering import cluster_customers
from instacart.analytics.hypotheses import run_hypotheses
from instacart.analytics.marts import build_marts as _build_marts
from instacart.config import Settings
from instacart.db import connect
from instacart.ingestion import ADVISORY_LOCK


class AnalyticsDataUnavailable(RuntimeError):
    """Raised when the requested operation has no compatible source snapshot."""


def _active_dataset(connection: psycopg.Connection) -> tuple[str, uuid.UUID] | None:
    row = connection.execute(
        "SELECT a.dataset_fingerprint, a.run_id FROM ops.active_dataset a "
        "JOIN ops.ingestion_runs r ON r.run_id = a.run_id "
        "WHERE a.singleton AND r.status = 'succeeded'"
    ).fetchone()
    return (str(row[0]), row[1]) if row else None


def _start_run(
    connection: psycopg.Connection,
    run_id: uuid.UUID,
    fingerprint: str,
    ingestion_run_id: uuid.UUID,
    run_type: str,
    parameters: dict[str, Any],
    marts_run_id: uuid.UUID | None = None,
) -> None:
    connection.execute(
        "INSERT INTO ops.analytics_runs "
        "(analytics_run_id, dataset_fingerprint, ingestion_run_id, marts_run_id, run_type, "
        "status, parameters) VALUES (%s, %s, %s, %s, %s, 'running', %s)",
        (run_id, fingerprint, ingestion_run_id, marts_run_id, run_type, Jsonb(parameters)),
    )


def _fail_abandoned_runs(connection: psycopg.Connection) -> None:
    """Close analytics runs left in progress by a process that exited unexpectedly."""
    connection.execute(
        "UPDATE ops.analytics_runs SET status = 'failed', finished_at = clock_timestamp(), "
        "error_message = 'Предыдущий аналитический процесс завершился без фиксации результата' "
        "WHERE status = 'running'"
    )


def _mark_failed(
    lock_connection: psycopg.Connection,
    run_id: uuid.UUID,
    error: BaseException,
    elapsed: float,
) -> None:
    lock_connection.execute(
        "UPDATE ops.analytics_runs SET status = 'failed', finished_at = clock_timestamp(), "
        "results = %s, error_message = %s WHERE analytics_run_id = %s AND status = 'running'",
        (
            Jsonb({"duration_seconds": round(elapsed, 3)}),
            str(error)[:10_000],
            run_id,
        ),
    )


def _fresh_marts(
    connection: psycopg.Connection, fingerprint: str, ingestion_run_id: uuid.UUID
) -> uuid.UUID | None:
    row = connection.execute(
        "SELECT s.marts_run_id FROM ops.analytics_state s "
        "JOIN ops.analytics_runs r ON r.analytics_run_id = s.marts_run_id "
        "WHERE s.singleton AND s.dataset_fingerprint = %s "
        "AND s.active_ingestion_run_id = %s AND r.status = 'succeeded' "
        "AND r.run_type = 'marts' AND r.dataset_fingerprint = %s "
        "AND r.ingestion_run_id = %s",
        (fingerprint, ingestion_run_id, fingerprint, ingestion_run_id),
    ).fetchone()
    return row[0] if row else None


def build_marts(settings: Settings) -> dict[str, Any]:
    """Atomically build the current SQL marts and publish their freshness pointer."""
    started = time.monotonic()
    run_id = uuid.uuid4()
    lock_connection = connect(settings)
    lock_connection.autocommit = True
    lock_acquired = False
    try:
        lock_connection.execute("SELECT pg_advisory_lock(%s, %s)", ADVISORY_LOCK)
        lock_acquired = True
        _fail_abandoned_runs(lock_connection)
        active = _active_dataset(lock_connection)
        if active is None:
            raise AnalyticsDataUnavailable(
                "Нет успешного активного импорта в core. Сначала выполните make ingest."
            )
        fingerprint, ingestion_run_id = active
        if not lock_connection.execute("SELECT EXISTS (SELECT 1 FROM core.orders)").fetchone()[0]:
            raise AnalyticsDataUnavailable(
                "Активный импорт не содержит заказов; аналитические витрины не построены."
            )
        parameters = {"sql_aggregation": "PostgreSQL", "version": 1}
        _start_run(lock_connection, run_id, fingerprint, ingestion_run_id, "marts", parameters)
        try:
            with connect(settings) as connection, connection.transaction():
                counts = _build_marts(connection, run_id, fingerprint, ingestion_run_id)
                checks = _validate_marts(connection, run_id, fingerprint, ingestion_run_id, counts)
                failed = [check["name"] for check in checks if not check["passed"]]
                if failed:
                    raise RuntimeError(f"Проверки витрин не пройдены: {', '.join(failed)}")
                for table in (
                    "_dataset_overview",
                    "_order_activity",
                    "_order_summary",
                    "_category_activity",
                    "_department_reorders",
                    "_product_popularity",
                    "_cart_position_stats",
                    "_customer_features",
                ):
                    connection.execute(f"ANALYZE mart.{table}")
                mart_size_bytes = int(
                    connection.execute(
                        "SELECT coalesce(sum(pg_total_relation_size(c.oid)), 0) "
                        "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                        "WHERE n.nspname = 'mart' AND c.relkind IN ('r', 'm')"
                    ).fetchone()[0]
                )
                results = {
                    "analytics_run_id": str(run_id),
                    "dataset_fingerprint": fingerprint,
                    "ingestion_run_id": str(ingestion_run_id),
                    "row_counts": counts,
                    "mart_size_bytes": mart_size_bytes,
                    "checks": checks,
                    "duration_seconds": round(time.monotonic() - started, 3),
                }
                connection.execute(
                    "UPDATE ops.analytics_runs SET status = 'succeeded', "
                    "finished_at = clock_timestamp(), results = %s, checks = %s "
                    "WHERE analytics_run_id = %s",
                    (Jsonb(results), Jsonb(checks), run_id),
                )
            return results
        except (Exception, KeyboardInterrupt) as exc:
            _mark_failed(lock_connection, run_id, exc, time.monotonic() - started)
            raise
    finally:
        if lock_acquired:
            lock_connection.execute("SELECT pg_advisory_unlock(%s, %s)", ADVISORY_LOCK)
        lock_connection.close()


def _validate_marts(
    connection: psycopg.Connection,
    run_id: uuid.UUID,
    fingerprint: str,
    ingestion_run_id: uuid.UUID,
    counts: dict[str, int],
) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []

    def add(name: str, passed: bool, detail: str) -> None:
        checks.append({"name": name, "passed": bool(passed), "detail": detail})

    overview = connection.execute(
        "SELECT known_order_count, known_item_count, repeat_numerator "
        "FROM mart._dataset_overview WHERE run_id = %s AND dataset_fingerprint = %s",
        (run_id, fingerprint),
    ).fetchone()
    summary = connection.execute(
        "SELECT count(*), coalesce(sum(basket_size), 0), coalesce(sum(repeat_count), 0) "
        "FROM mart._order_summary WHERE run_id = %s AND dataset_fingerprint = %s",
        (run_id, fingerprint),
    ).fetchone()
    add(
        "order_summary_matches_overview",
        overview is not None
        and tuple(int(value or 0) for value in overview)
        == tuple(int(value or 0) for value in summary),
        "заказы с известным составом и суммы товарных позиций совпадают с overview",
    )
    categorized = connection.execute(
        "SELECT coalesce(sum(item_count), 0), coalesce(sum(reorder_count), 0) "
        "FROM mart._department_reorders WHERE run_id = %s AND group_type = 'department' "
        "AND eval_set IN ('prior', 'train')",
        (run_id,),
    ).fetchone()
    add(
        "category_totals_match_overview",
        overview is not None
        and int(categorized[0]) == int(overview[1])
        and int(categorized[1]) == int(overview[2]),
        "суммы позиций и повторных покупок по подразделениям равны общей сумме",
    )
    cross_filter_totals = connection.execute(
        "SELECT coalesce(sum(item_count), 0), coalesce(sum(reorder_count), 0) "
        "FROM mart._category_activity WHERE run_id = %s AND category_level = 'department' "
        "AND eval_set IN ('prior', 'train')",
        (run_id,),
    ).fetchone()
    add(
        "category_activity_totals_match_overview",
        overview is not None
        and int(cross_filter_totals[0]) == int(overview[1])
        and int(cross_filter_totals[1]) == int(overview[2]),
        "кросс-фильтруемая витрина категорий совпадает с количеством известных позиций",
    )
    test_items = connection.execute(
        "SELECT count(*) FROM mart._order_summary WHERE run_id = %s AND eval_set = 'test'",
        (run_id,),
    ).fetchone()[0]
    add(
        "test_orders_excluded_from_known_baskets",
        test_items == 0,
        "test отсутствует в витрине состава корзин",
    )
    add(
        "active_fingerprint",
        connection.execute(
            "SELECT EXISTS (SELECT 1 FROM ops.active_dataset a "
            "JOIN ops.analytics_state s ON s.singleton "
            "WHERE a.singleton AND a.dataset_fingerprint = %s AND a.run_id = %s "
            "AND s.dataset_fingerprint = a.dataset_fingerprint "
            "AND s.active_ingestion_run_id = a.run_id AND s.marts_run_id = %s)",
            (fingerprint, ingestion_run_id, run_id),
        ).fetchone()[0],
        "витрины построены для активного fingerprint",
    )
    add(
        "nonempty_order_activity",
        counts["order_activity"] > 0,
        "активность включает заказы всех eval_set",
    )
    add(
        "customer_features_present",
        counts["customer_features"] > 0,
        "сформированы признаки покупателей с prior",
    )
    return checks


def analyze(
    settings: Settings,
    *,
    random_seed: int = 2026,
    bootstrap_replicates: int = 200,
    silhouette_sample_size: int = 5_000,
) -> dict[str, Any]:
    """Run hypotheses and K-Means on a fresh marts snapshot, then publish atomically."""
    started = time.monotonic()
    run_id = uuid.uuid4()
    lock_connection = connect(settings)
    lock_connection.autocommit = True
    parameters = {
        "random_seed": random_seed,
        "bootstrap_replicates": bootstrap_replicates,
        "silhouette_sample_size": silhouette_sample_size,
        "alpha": 0.05,
        "cluster_k_range": [2, 8],
        "kmeans_n_init": 10,
    }
    lock_acquired = False
    try:
        lock_connection.execute("SELECT pg_advisory_lock(%s, %s)", ADVISORY_LOCK)
        lock_acquired = True
        _fail_abandoned_runs(lock_connection)
        active = _active_dataset(lock_connection)
        if active is None:
            raise AnalyticsDataUnavailable(
                "Нет успешного активного импорта в core. Сначала выполните make ingest."
            )
        fingerprint, ingestion_run_id = active
        marts_run_id = _fresh_marts(lock_connection, fingerprint, ingestion_run_id)
        if marts_run_id is None:
            raise AnalyticsDataUnavailable(
                "Для активного набора нет актуальных витрин. Сначала выполните make marts."
            )
        _start_run(
            lock_connection,
            run_id,
            fingerprint,
            ingestion_run_id,
            "analysis",
            parameters,
            marts_run_id=marts_run_id,
        )
        marts_results = lock_connection.execute(
            "SELECT results FROM ops.analytics_runs WHERE analytics_run_id = %s",
            (marts_run_id,),
        ).fetchone()[0]
        try:
            with connect(settings) as connection, connection.transaction():
                current_marts_run_id = _fresh_marts(connection, fingerprint, ingestion_run_id)
                if current_marts_run_id is None:
                    raise AnalyticsDataUnavailable(
                        "Витрины устарели до начала анализа; выполните make marts."
                    )
                for table in (
                    "_customer_segments",
                    "_cluster_metrics",
                    "_cluster_profiles",
                    "_hypothesis_results",
                    "_hypothesis_categories",
                    "_hypothesis_pairs",
                ):
                    connection.execute(f"DELETE FROM mart.{table}")
                cluster_started = time.monotonic()
                cluster_results = cluster_customers(
                    connection,
                    run_id,
                    fingerprint,
                    random_seed=random_seed,
                    silhouette_sample_size=silhouette_sample_size,
                )
                cluster_duration = round(time.monotonic() - cluster_started, 3)
                hypotheses_started = time.monotonic()
                hypothesis_results = run_hypotheses(
                    connection,
                    run_id,
                    fingerprint,
                    random_seed=random_seed,
                    bootstrap_replicates=bootstrap_replicates,
                )
                hypotheses_duration = round(time.monotonic() - hypotheses_started, 3)
                published = connection.execute(
                    "UPDATE ops.analytics_state SET analysis_run_id = %s, "
                    "updated_at = clock_timestamp() "
                    "WHERE singleton AND dataset_fingerprint = %s AND marts_run_id = %s "
                    "AND active_ingestion_run_id = %s",
                    (run_id, fingerprint, current_marts_run_id, ingestion_run_id),
                )
                if published.rowcount != 1:
                    raise AnalyticsDataUnavailable(
                        "Активный импорт или поколение витрин изменились во время анализа."
                    )
                result = {
                    "analytics_run_id": str(run_id),
                    "dataset_fingerprint": fingerprint,
                    "ingestion_run_id": str(ingestion_run_id),
                    "marts_run_id": str(current_marts_run_id),
                    "parameters": parameters,
                    "row_counts": {
                        **marts_results.get("row_counts", {}),
                        "customer_segments": cluster_results["customer_count"],
                        "cluster_metrics": len(cluster_results["metrics"]),
                        "cluster_profiles": len(cluster_results["profiles"]),
                        "hypothesis_results": int(
                            connection.execute(
                                "SELECT count(*) FROM mart._hypothesis_results WHERE run_id = %s",
                                (run_id,),
                            ).fetchone()[0]
                        ),
                        "hypothesis_categories": int(
                            connection.execute(
                                "SELECT count(*) FROM mart._hypothesis_categories "
                                "WHERE run_id = %s",
                                (run_id,),
                            ).fetchone()[0]
                        ),
                        "hypothesis_pairs": int(
                            connection.execute(
                                "SELECT count(*) FROM mart._hypothesis_pairs WHERE run_id = %s",
                                (run_id,),
                            ).fetchone()[0]
                        ),
                    },
                    "mart_size_bytes": int(
                        connection.execute(
                            "SELECT coalesce(sum(pg_total_relation_size(c.oid)), 0) "
                            "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                            "WHERE n.nspname = 'mart' AND c.relkind IN ('r', 'm')"
                        ).fetchone()[0]
                    ),
                    "marts_duration_seconds": marts_results.get("duration_seconds"),
                    "clustering": cluster_results,
                    "hypotheses": hypothesis_results,
                    "stage_durations_seconds": {
                        "clustering": cluster_duration,
                        "hypotheses": hypotheses_duration,
                    },
                    "duration_seconds": round(time.monotonic() - started, 3),
                }
                connection.execute(
                    "UPDATE ops.analytics_runs SET status = 'succeeded', "
                    "finished_at = clock_timestamp(), results = %s, checks = %s "
                    "WHERE analytics_run_id = %s",
                    (
                        Jsonb(result),
                        Jsonb([{"name": "published_atomically", "passed": True}]),
                        run_id,
                    ),
                )
                for table in (
                    "_customer_segments",
                    "_cluster_metrics",
                    "_cluster_profiles",
                    "_hypothesis_results",
                    "_hypothesis_categories",
                    "_hypothesis_pairs",
                ):
                    connection.execute(f"ANALYZE mart.{table}")
            return result
        except (Exception, KeyboardInterrupt) as exc:
            _mark_failed(lock_connection, run_id, exc, time.monotonic() - started)
            raise
    finally:
        if lock_acquired:
            lock_connection.execute("SELECT pg_advisory_unlock(%s, %s)", ADVISORY_LOCK)
        lock_connection.close()


def validate_analytics(settings: Settings) -> dict[str, Any]:
    """Check that all published results agree with the active source snapshot."""
    started = time.monotonic()
    with connect(settings) as connection:
        connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        active = _active_dataset(connection)
        state = connection.execute(
            "SELECT dataset_fingerprint, active_ingestion_run_id, marts_run_id, analysis_run_id "
            "FROM ops.analytics_state WHERE singleton"
        ).fetchone()
        checks: list[dict[str, Any]] = []

        def add(name: str, passed: bool, detail: str) -> None:
            checks.append({"name": name, "passed": bool(passed), "detail": detail})

        add(
            "active_dataset_exists",
            active is not None,
            "ops.active_dataset указывает на успешный импорт",
        )
        if active is None:
            return {
                "status": "stale_or_missing",
                "passed": False,
                "stale": True,
                "dashboard_ready": False,
                "checks": checks,
                "duration_seconds": round(time.monotonic() - started, 3),
            }
        fingerprint, ingestion_run_id = active
        add(
            "analytics_state_exists",
            state is not None,
            "состояние публикации аналитики зарегистрировано",
        )
        if state is None:
            return {
                "status": "stale_or_missing",
                "passed": False,
                "stale": True,
                "dashboard_ready": False,
                "active_dataset_fingerprint": fingerprint,
                "ingestion_run_id": str(ingestion_run_id),
                "checks": checks,
                "duration_seconds": round(time.monotonic() - started, 3),
            }
        state_fingerprint, state_ingestion_run_id, marts_run_id, analysis_run_id = state
        state_import_current = state_ingestion_run_id == ingestion_run_id
        add(
            "dataset_fingerprint_current",
            state_fingerprint == fingerprint,
            f"active={fingerprint}; analytics={state_fingerprint}",
        )
        add(
            "ingestion_run_id_current",
            state_import_current,
            f"active={ingestion_run_id}; analytics={state_ingestion_run_id}",
        )
        run_rows = connection.execute(
            "SELECT analytics_run_id, run_type, status, dataset_fingerprint, ingestion_run_id, "
            "marts_run_id, finished_at, results "
            "FROM ops.analytics_runs WHERE analytics_run_id = ANY(%s)",
            ([marts_run_id, analysis_run_id] if analysis_run_id else [marts_run_id],),
        ).fetchall()
        run_by_id = {row[0]: row for row in run_rows}
        marts_run = run_by_id.get(marts_run_id)
        analysis_run = run_by_id.get(analysis_run_id) if analysis_run_id else None
        add(
            "marts_run_succeeded",
            bool(
                marts_run
                and marts_run[1] == "marts"
                and marts_run[2] == "succeeded"
                and marts_run[3] == fingerprint
                and marts_run[4] == ingestion_run_id
                and state_import_current
            ),
            "последний запуск SQL-витрин успешен и связан с активным набором",
        )
        add(
            "analysis_run_succeeded",
            bool(
                analysis_run
                and analysis_run[1] == "analysis"
                and analysis_run[2] == "succeeded"
                and analysis_run[3] == fingerprint
                and analysis_run[4] == ingestion_run_id
                and analysis_run[5] == marts_run_id
                and state_import_current
            ),
            "проверки гипотез и кластеризация опубликованы для активного набора",
        )
        marts_ready = bool(
            marts_run
            and state_fingerprint == fingerprint
            and marts_run[1] == "marts"
            and marts_run[2] == "succeeded"
            and marts_run[3] == fingerprint
            and marts_run[4] == ingestion_run_id
            and state_import_current
        )
        dashboard_ready = bool(
            marts_ready
            and analysis_run
            and analysis_run[1] == "analysis"
            and analysis_run[2] == "succeeded"
            and analysis_run[3] == fingerprint
            and analysis_run[4] == ingestion_run_id
            and analysis_run[5] == marts_run_id
        )
        readiness_row = connection.execute(
            "SELECT marts_ready, analysis_ready, dashboard_ready "
            "FROM ops.analytics_readiness WHERE active_ingestion_run_id = %s",
            (ingestion_run_id,),
        ).fetchone()
        add(
            "dashboard_readiness_view_matches",
            bool(readiness_row)
            and tuple(readiness_row) == (marts_ready, dashboard_ready, dashboard_ready),
            "ops.analytics_readiness связывает активный импорт, витрины и анализ",
        )

        mart_counts = {
            "dataset_overview": int(
                connection.execute("SELECT count(*) FROM mart.dataset_overview").fetchone()[0]
            ),
            "order_activity": int(
                connection.execute("SELECT count(*) FROM mart.order_activity").fetchone()[0]
            ),
            "order_summary": int(
                connection.execute("SELECT count(*) FROM mart.order_summary").fetchone()[0]
            ),
            "category_activity": int(
                connection.execute("SELECT count(*) FROM mart.category_activity").fetchone()[0]
            ),
            "department_reorders": int(
                connection.execute("SELECT count(*) FROM mart.department_reorders").fetchone()[0]
            ),
            "product_popularity": int(
                connection.execute("SELECT count(*) FROM mart.product_popularity").fetchone()[0]
            ),
            "cart_position_stats": int(
                connection.execute("SELECT count(*) FROM mart.cart_position_stats").fetchone()[0]
            ),
            "customer_features": int(
                connection.execute("SELECT count(*) FROM mart.customer_features").fetchone()[0]
            ),
            "customer_segments": int(
                connection.execute("SELECT count(*) FROM mart.customer_segments").fetchone()[0]
            ),
            "cluster_metrics": int(
                connection.execute("SELECT count(*) FROM mart.cluster_metrics").fetchone()[0]
            ),
            "cluster_profiles": int(
                connection.execute("SELECT count(*) FROM mart.cluster_profiles").fetchone()[0]
            ),
            "hypothesis_results": int(
                connection.execute("SELECT count(*) FROM mart.hypothesis_results").fetchone()[0]
            ),
            "hypothesis_categories": int(
                connection.execute("SELECT count(*) FROM mart.hypothesis_categories").fetchone()[0]
            ),
            "hypothesis_pairs": int(
                connection.execute("SELECT count(*) FROM mart.hypothesis_pairs").fetchone()[0]
            ),
        }
        checks.extend(
            _validate_marts(
                connection,
                marts_run_id,
                fingerprint,
                ingestion_run_id,
                {
                    "order_activity": mart_counts["order_activity"],
                    "customer_features": mart_counts["customer_features"],
                },
            )
        )
        add(
            "segment_count_matches_feature_count",
            mart_counts["customer_segments"] == mart_counts["customer_features"]
            and mart_counts["customer_segments"] > 0,
            "каждому покупателю с признаками присвоен кластер и PCA-координаты",
        )
        add(
            "expected_hypothesis_results_present",
            mart_counts["hypothesis_results"] >= 4
            and mart_counts["hypothesis_pairs"] == 6
            and mart_counts["cluster_metrics"] > 0
            and mart_counts["cluster_profiles"] > 0,
            "есть H1, H2 по уровням departments/aisles, H3 и парные сравнения",
        )
        size = int(
            connection.execute(
                "SELECT coalesce(sum(pg_total_relation_size(c.oid)), 0) FROM pg_class c "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = 'mart' AND c.relkind IN ('r', 'm')"
            ).fetchone()[0]
        )
        passed = all(item["passed"] for item in checks)
        return {
            "status": "succeeded" if passed else "stale_or_invalid",
            "passed": passed,
            "stale": not dashboard_ready,
            "dashboard_ready": dashboard_ready,
            "active_dataset_fingerprint": fingerprint,
            "ingestion_run_id": str(ingestion_run_id),
            "marts_run_id": str(marts_run_id),
            "analysis_run_id": str(analysis_run_id) if analysis_run_id else None,
            "marts_finished_at": marts_run[6].isoformat() if marts_run and marts_run[6] else None,
            "analysis_finished_at": analysis_run[6].isoformat()
            if analysis_run and analysis_run[6]
            else None,
            "row_counts": mart_counts,
            "mart_size_bytes": size,
            "checks": checks,
            "duration_seconds": round(time.monotonic() - started, 3),
        }


def report_metadata(result: dict[str, Any]) -> dict[str, Any]:
    """Attach stable UTC generation timestamps to a report payload."""
    return {"generated_at": datetime.now(UTC).isoformat(), **result}
