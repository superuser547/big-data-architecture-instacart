"""Add a covering index for interactive overview filters."""

from alembic import op

revision = "0007_dashboard_index"
down_revision = "0006_analytics_lineage"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_mart_order_summary_dashboard_filter "
            "ON mart._order_summary "
            "(run_id, eval_set, order_dow, order_hour_of_day, basket_size) "
            "INCLUDE (dataset_fingerprint, days_since_prior_order, repeat_count)"
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("DROP INDEX CONCURRENTLY idx_mart_order_summary_dashboard_filter")
