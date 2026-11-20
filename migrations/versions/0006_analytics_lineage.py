"""Bind analytics publications to both the active import and mart generation."""

from alembic import op

revision = "0006_analytics_lineage"
down_revision = "0005_category_activity"
branch_labels = None
depends_on = None

MART_VIEWS = (
    ("dataset_overview", "_dataset_overview"),
    ("order_activity", "_order_activity"),
    ("order_summary", "_order_summary"),
    ("category_activity", "_category_activity"),
    ("department_reorders", "_department_reorders"),
    ("product_popularity", "_product_popularity"),
    ("cart_position_stats", "_cart_position_stats"),
    ("customer_features", "_customer_features"),
)
ANALYSIS_VIEWS = (
    ("customer_segments", "_customer_segments"),
    ("cluster_metrics", "_cluster_metrics"),
    ("cluster_profiles", "_cluster_profiles"),
    ("hypothesis_results", "_hypothesis_results"),
    ("hypothesis_categories", "_hypothesis_categories"),
    ("hypothesis_pairs", "_hypothesis_pairs"),
)


def _replace_public_views() -> None:
    for view, table in MART_VIEWS:
        op.execute(
            f"CREATE OR REPLACE VIEW mart.{view} AS "
            f"SELECT d.* FROM mart.{table} d "
            "JOIN ops.analytics_state s ON s.singleton AND s.marts_run_id = d.run_id "
            "JOIN ops.analytics_runs mr ON mr.analytics_run_id = d.run_id "
            "AND mr.run_type = 'marts' AND mr.status = 'succeeded' "
            "JOIN ops.active_dataset a ON a.singleton AND a.run_id = mr.ingestion_run_id "
            "AND a.dataset_fingerprint = mr.dataset_fingerprint "
            "WHERE s.active_ingestion_run_id = a.run_id "
            "AND s.dataset_fingerprint = a.dataset_fingerprint "
            "AND d.dataset_fingerprint = a.dataset_fingerprint"
        )
    for view, table in ANALYSIS_VIEWS:
        op.execute(
            f"CREATE OR REPLACE VIEW mart.{view} AS "
            f"SELECT d.* FROM mart.{table} d "
            "JOIN ops.analytics_state s ON s.singleton AND s.analysis_run_id = d.run_id "
            "JOIN ops.analytics_runs ar ON ar.analytics_run_id = d.run_id "
            "AND ar.run_type = 'analysis' AND ar.status = 'succeeded' "
            "AND ar.marts_run_id = s.marts_run_id "
            "JOIN ops.active_dataset a ON a.singleton AND a.run_id = ar.ingestion_run_id "
            "AND a.dataset_fingerprint = ar.dataset_fingerprint "
            "WHERE s.active_ingestion_run_id = a.run_id "
            "AND s.dataset_fingerprint = a.dataset_fingerprint "
            "AND d.dataset_fingerprint = a.dataset_fingerprint"
        )


def upgrade() -> None:
    op.execute("ALTER TABLE ops.analytics_state ADD COLUMN active_ingestion_run_id UUID")
    op.execute("ALTER TABLE ops.analytics_runs ADD COLUMN marts_run_id UUID")
    op.execute(
        "UPDATE ops.analytics_state s SET active_ingestion_run_id = a.run_id "
        "FROM ops.active_dataset a WHERE s.singleton AND a.singleton"
    )
    op.execute(
        "UPDATE ops.analytics_runs SET marts_run_id = (results->>'marts_run_id')::UUID "
        "WHERE run_type = 'analysis' AND results ? 'marts_run_id'"
    )
    # Previous analysis intervals used a fixed customer subsample and must be recomputed.
    op.execute("UPDATE ops.analytics_state SET analysis_run_id = NULL WHERE singleton")
    op.execute("ALTER TABLE mart._hypothesis_categories ALTER COLUMN ci_lower DROP NOT NULL")
    op.execute("ALTER TABLE mart._hypothesis_categories ALTER COLUMN ci_upper DROP NOT NULL")
    op.execute("ALTER TABLE mart._hypothesis_pairs ALTER COLUMN risk_difference DROP NOT NULL")
    op.execute("ALTER TABLE mart._hypothesis_pairs ALTER COLUMN p_value DROP NOT NULL")
    op.execute("ALTER TABLE mart._hypothesis_pairs ALTER COLUMN p_adjusted DROP NOT NULL")

    _replace_public_views()
    op.execute(
        """
        CREATE VIEW ops.analytics_readiness AS
        SELECT a.run_id AS active_ingestion_run_id,
               a.dataset_fingerprint,
               s.marts_run_id,
               s.analysis_run_id,
               ready.marts_ready,
               ready.analysis_ready,
               (ready.marts_ready AND ready.analysis_ready) AS dashboard_ready
        FROM ops.active_dataset a
        LEFT JOIN ops.analytics_state s ON s.singleton
        LEFT JOIN ops.analytics_runs mr ON mr.analytics_run_id = s.marts_run_id
        LEFT JOIN ops.analytics_runs ar ON ar.analytics_run_id = s.analysis_run_id
        CROSS JOIN LATERAL (
            SELECT coalesce(
                s.active_ingestion_run_id = a.run_id
                AND s.dataset_fingerprint = a.dataset_fingerprint
                AND mr.run_type = 'marts'
                AND mr.status = 'succeeded'
                AND mr.ingestion_run_id = a.run_id
                AND mr.dataset_fingerprint = a.dataset_fingerprint,
                FALSE
            ) AS marts_ready,
            coalesce(
                s.active_ingestion_run_id = a.run_id
                AND s.dataset_fingerprint = a.dataset_fingerprint
                AND ar.run_type = 'analysis'
                AND ar.status = 'succeeded'
                AND ar.ingestion_run_id = a.run_id
                AND ar.dataset_fingerprint = a.dataset_fingerprint
                AND ar.marts_run_id = s.marts_run_id
                AND mr.run_type = 'marts'
                AND mr.status = 'succeeded'
                AND mr.ingestion_run_id = a.run_id
                AND mr.dataset_fingerprint = a.dataset_fingerprint,
                FALSE
            ) AS analysis_ready
        ) ready
        """
    )


def downgrade() -> None:
    op.execute("DROP VIEW ops.analytics_readiness")
    for view, table in MART_VIEWS:
        op.execute(
            f"CREATE OR REPLACE VIEW mart.{view} AS "
            f"SELECT d.* FROM mart.{table} d "
            "JOIN ops.analytics_state s ON s.singleton AND s.marts_run_id = d.run_id "
            "JOIN ops.active_dataset a ON a.singleton "
            "AND a.dataset_fingerprint = d.dataset_fingerprint "
            "WHERE s.dataset_fingerprint = a.dataset_fingerprint"
        )
    for view, table in ANALYSIS_VIEWS:
        op.execute(
            f"CREATE OR REPLACE VIEW mart.{view} AS "
            f"SELECT d.* FROM mart.{table} d "
            "JOIN ops.analytics_state s ON s.singleton AND s.analysis_run_id = d.run_id "
            "JOIN ops.active_dataset a ON a.singleton "
            "AND a.dataset_fingerprint = d.dataset_fingerprint "
            "WHERE s.dataset_fingerprint = a.dataset_fingerprint"
        )
    op.execute("ALTER TABLE ops.analytics_runs DROP COLUMN marts_run_id")
    op.execute("ALTER TABLE ops.analytics_state DROP COLUMN active_ingestion_run_id")
