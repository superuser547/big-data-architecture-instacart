"""Add a sparse cross-filter aggregate for category, time, basket size and eval set."""

from alembic import op

revision = "0005_category_activity"
down_revision = "0004_analytics_layer"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE mart._category_activity (
            run_id UUID NOT NULL,
            dataset_fingerprint VARCHAR(71) NOT NULL,
            eval_set VARCHAR(8) NOT NULL,
            order_dow SMALLINT NOT NULL,
            order_hour_of_day SMALLINT NOT NULL,
            basket_size SMALLINT NOT NULL,
            category_level TEXT NOT NULL CHECK (category_level IN ('department', 'aisle')),
            category_id INTEGER NOT NULL,
            category_name TEXT NOT NULL,
            department_id SMALLINT NOT NULL,
            department_name TEXT NOT NULL,
            item_count BIGINT NOT NULL,
            reorder_count BIGINT NOT NULL,
            PRIMARY KEY (
                run_id, eval_set, order_dow, order_hour_of_day,
                basket_size, category_level, category_id
            )
        )
        """
    )
    op.execute(
        "CREATE INDEX idx_mart_category_activity_filter ON mart._category_activity "
        "(run_id, category_level, category_id, eval_set, order_dow, basket_size)"
    )
    op.execute(
        "CREATE VIEW mart.category_activity AS "
        "SELECT d.* FROM mart._category_activity d "
        "JOIN ops.analytics_state s ON s.singleton AND s.marts_run_id = d.run_id "
        "JOIN ops.active_dataset a ON a.singleton "
        "AND a.dataset_fingerprint = d.dataset_fingerprint "
        "WHERE s.dataset_fingerprint = a.dataset_fingerprint"
    )


def downgrade() -> None:
    op.execute("DROP VIEW mart.category_activity")
    op.execute("DROP TABLE mart._category_activity")
