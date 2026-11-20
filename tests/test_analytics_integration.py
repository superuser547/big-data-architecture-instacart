from __future__ import annotations

import os
import uuid
from pathlib import Path

import psycopg
import pytest
from alembic import command
from alembic.config import Config

import instacart.analytics.service as analytics_service
from instacart.analytics.service import analyze, build_marts, validate_analytics
from instacart.config import Settings


def _seed_core(dsn: str) -> str:
    fingerprint = "analytics-integration-v1"
    with psycopg.connect(dsn) as connection:
        connection.execute(
            "TRUNCATE core.order_items, core.orders, core.users, core.products, "
            "core.aisles, core.departments"
        )
        connection.execute("DELETE FROM ops.analytics_state")
        connection.execute("DELETE FROM ops.active_dataset")
        connection.execute("DELETE FROM ops.analytics_runs")
        connection.execute("DELETE FROM ops.ingestion_runs")
        connection.execute("INSERT INTO core.departments VALUES (1, 'Frozen'), (2, 'Produce')")
        connection.execute(
            "INSERT INTO core.aisles VALUES "
            "(1, 'Aisle 1', 1), (2, 'Aisle 2', 1), (3, 'Aisle 3', 2), (4, 'Aisle 4', 2)"
        )
        connection.execute(
            "INSERT INTO core.products (product_id, product_name, aisle_id) "
            "SELECT product_id, 'Product ' || product_id, ((product_id - 1) / 10)::SMALLINT + 1 "
            "FROM generate_series(1, 40) AS product_id"
        )
        connection.execute("INSERT INTO core.users SELECT generate_series(1, 12)")

        order_rows = []
        item_rows = []
        next_order_id = 1000
        for user_id in range(1, 13):
            prior_sizes = (20 + user_id % 4, 20 + (user_id * 3) % 5)
            definitions = (
                [
                    ("prior", 1, prior_sizes[0], None),
                    ("train", 2, 5 + user_id % 3, 1.0),
                    ("test", 3, 0, 2.0),
                ]
                if user_id == 12
                else [
                    ("prior", 1, prior_sizes[0], None),
                    ("prior", 2, prior_sizes[1], float(user_id % 30)),
                    ("train", 3, 5 + user_id % 3, 1.0),
                ]
            )
            for order_number, (eval_set, _, basket_size, days) in enumerate(definitions, start=1):
                order_id = next_order_id
                next_order_id += 1
                order_rows.append(
                    (
                        order_id,
                        user_id,
                        eval_set,
                        order_number,
                        (user_id + order_number) % 7,
                        (user_id * 2 + order_number) % 24,
                        days,
                    )
                )
                for position in range(1, basket_size + 1):
                    product_id = ((position * 3 + user_id * 5 + order_number - 1) % 40) + 1
                    reordered = (position + user_id + order_number) % 3 == 0
                    item_rows.append((order_id, product_id, position, reordered))
        connection.cursor().executemany(
            "INSERT INTO core.orders VALUES (%s, %s, %s, %s, %s, %s, %s)", order_rows
        )
        connection.cursor().executemany(
            "INSERT INTO core.order_items VALUES (%s, %s, %s, %s)", item_rows
        )
        run_id = uuid.uuid4()
        connection.execute(
            "INSERT INTO ops.ingestion_runs "
            "(run_id, dataset_fingerprint, status, finished_at) "
            "VALUES (%s, %s, 'succeeded', clock_timestamp())",
            (run_id, fingerprint),
        )
        connection.execute(
            "INSERT INTO ops.active_dataset (singleton, dataset_fingerprint, run_id) "
            "VALUES (TRUE, %s, %s)",
            (fingerprint, run_id),
        )
    return fingerprint


@pytest.mark.integration
def test_marts_analysis_idempotency_rollback_and_staleness(monkeypatch: pytest.MonkeyPatch) -> None:
    dsn = os.getenv("INTEGRATION_DATABASE_URL")
    if not dsn:
        pytest.skip("INTEGRATION_DATABASE_URL не задан")
    monkeypatch.setenv("DATABASE_URL", dsn)
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", dsn.replace("%", "%%"))
    command.upgrade(config, "head")
    fingerprint = _seed_core(dsn)
    settings = Settings(
        dataset_path=Path("/tmp/no-source-needed-for-analytics"),
        database_url=dsn,
        duckdb_memory_limit="256MB",
        duckdb_threads=1,
        duckdb_temp_directory=Path("/tmp/analytics-test"),
        profile_report=Path("/tmp/profile.json"),
        validation_report=Path("/tmp/validation.json"),
    )

    marts = build_marts(settings)
    assert marts["row_counts"]["order_summary"] == 35
    original_build_marts = analytics_service._build_marts

    def fail_marts(*args, **kwargs):
        raise RuntimeError("injected marts failure")

    monkeypatch.setattr(analytics_service, "_build_marts", fail_marts)
    with pytest.raises(RuntimeError, match="injected marts failure"):
        build_marts(settings)
    monkeypatch.setattr(analytics_service, "_build_marts", original_build_marts)
    with psycopg.connect(dsn) as connection:
        assert connection.execute("SELECT count(*) FROM mart.dataset_overview").fetchone()[0] == 1
        assert (
            connection.execute(
                "SELECT status FROM ops.analytics_runs WHERE run_type = 'marts' "
                "ORDER BY started_at DESC LIMIT 1"
            ).fetchone()[0]
            == "failed"
        )
    with psycopg.connect(dsn) as connection:
        abandoned_id = uuid.uuid4()
        active_ingestion_id = connection.execute(
            "SELECT run_id FROM ops.active_dataset WHERE singleton"
        ).fetchone()[0]
        connection.execute(
            "INSERT INTO ops.analytics_runs "
            "(analytics_run_id, dataset_fingerprint, ingestion_run_id, run_type, status) "
            "VALUES (%s, %s, %s, 'analysis', 'running')",
            (abandoned_id, fingerprint, active_ingestion_id),
        )
    repeated_marts = build_marts(settings)
    assert repeated_marts["row_counts"] == marts["row_counts"]
    with psycopg.connect(dsn) as connection:
        assert (
            connection.execute(
                "SELECT status FROM ops.analytics_runs WHERE analytics_run_id = %s",
                (abandoned_id,),
            ).fetchone()[0]
            == "failed"
        )
        assert connection.execute("SELECT count(*) FROM mart.dataset_overview").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM mart.customer_features").fetchone()[0] == 12
        overview = connection.execute(
            "SELECT test_order_count, known_item_count FROM mart.dataset_overview"
        ).fetchone()
        assert overview is not None
        assert overview[0] == 1
        assert (
            connection.execute(
                "SELECT count(*) FROM mart.order_summary WHERE eval_set = 'test'"
            ).fetchone()[0]
            == 0
        )
        assert (
            connection.execute(
                "SELECT count(*) FROM mart.order_activity WHERE eval_set = 'test'"
            ).fetchone()[0]
            == 1
        )
        assert (
            connection.execute(
                "SELECT count(*) FROM mart.customer_features WHERE avg_days_between_orders IS NULL"
            ).fetchone()[0]
            == 1
        )
        assert (
            connection.execute(
                "SELECT sum(item_count) FROM mart.department_reorders "
                "WHERE group_type = 'department'"
            ).fetchone()[0]
            == overview[1]
        )
        category_activity = connection.execute(
            "SELECT sum(item_count), sum(reorder_count), count(*) "
            "FROM mart.category_activity WHERE category_level = 'department'"
        ).fetchone()
        assert category_activity[0] == overview[1]
        assert (
            category_activity[1]
            == connection.execute("SELECT repeat_numerator FROM mart.dataset_overview").fetchone()[
                0
            ]
        )
        assert category_activity[2] > 0

    first = analyze(
        settings,
        random_seed=2026,
        bootstrap_replicates=10,
        bootstrap_customer_sample=12,
        silhouette_sample_size=12,
    )
    assert first["clustering"]["customer_count"] == 12
    assert 2 <= first["clustering"]["selected_k"] <= 8
    assert first["hypotheses"]["H1"]["observations"] == 36
    assert first["hypotheses"]["H3"]["orders_with_basket_size_at_least_20"] == 23
    with psycopg.connect(dsn) as connection:
        assert connection.execute("SELECT count(*) FROM mart.customer_segments").fetchone()[0] == 12
        assert connection.execute("SELECT count(*) FROM mart.hypothesis_pairs").fetchone()[0] == 6
        first_segments = connection.execute(
            "SELECT user_id, cluster_id, pca_1, pca_2 FROM mart.customer_segments ORDER BY user_id"
        ).fetchall()

    old_result = first["analytics_run_id"]
    original_cluster = analytics_service.cluster_customers

    def fail_clustering(*args, **kwargs):
        raise RuntimeError("injected analytics failure")

    monkeypatch.setattr(analytics_service, "cluster_customers", fail_clustering)
    with pytest.raises(RuntimeError, match="injected analytics failure"):
        analyze(
            settings,
            random_seed=2026,
            bootstrap_replicates=10,
            bootstrap_customer_sample=12,
            silhouette_sample_size=12,
        )
    monkeypatch.setattr(analytics_service, "cluster_customers", original_cluster)
    with psycopg.connect(dsn) as connection:
        assert (
            connection.execute(
                "SELECT analysis_run_id::TEXT FROM ops.analytics_state WHERE singleton"
            ).fetchone()[0]
            == old_result
        )
        assert connection.execute("SELECT count(*) FROM mart.customer_segments").fetchone()[0] == 12

    second = analyze(
        settings,
        random_seed=2026,
        bootstrap_replicates=10,
        bootstrap_customer_sample=12,
        silhouette_sample_size=12,
    )
    with psycopg.connect(dsn) as connection:
        segments = connection.execute(
            "SELECT user_id, cluster_id, pca_1, pca_2 FROM mart.customer_segments ORDER BY user_id"
        ).fetchall()
        assert len(segments) == 12
        assert connection.execute("SELECT count(*) FROM mart.cluster_metrics").fetchone()[0] == 7
        assert segments == first_segments
    assert first["clustering"]["selected_k"] == second["clustering"]["selected_k"]

    validation = validate_analytics(settings)
    assert validation["passed"] is True
    assert validation["active_dataset_fingerprint"] == fingerprint

    with psycopg.connect(dsn) as connection:
        next_run = uuid.uuid4()
        stale_fingerprint = "analytics-integration-v2"
        connection.execute(
            "INSERT INTO ops.ingestion_runs "
            "(run_id, dataset_fingerprint, status, finished_at) "
            "VALUES (%s, %s, 'succeeded', clock_timestamp())",
            (next_run, stale_fingerprint),
        )
        connection.execute(
            "UPDATE ops.active_dataset SET dataset_fingerprint = %s, run_id = %s WHERE singleton",
            (stale_fingerprint, next_run),
        )
        assert connection.execute("SELECT count(*) FROM mart.dataset_overview").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM mart.hypothesis_results").fetchone()[0] == 0
    stale_validation = validate_analytics(settings)
    assert stale_validation["passed"] is False
    assert stale_validation["stale"] is True
