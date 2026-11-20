"""Add versioned analytics outputs without constraining core table rebuilds."""

from alembic import op

revision = "0004_analytics_layer"
down_revision = "0003_active_dataset"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE ops.analytics_runs (
            analytics_run_id UUID PRIMARY KEY,
            dataset_fingerprint VARCHAR(71) NOT NULL,
            ingestion_run_id UUID NOT NULL,
            run_type TEXT NOT NULL CHECK (run_type IN ('marts', 'analysis')),
            status TEXT NOT NULL CHECK (status IN ('running', 'succeeded', 'failed')),
            started_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            finished_at TIMESTAMPTZ,
            parameters JSONB NOT NULL DEFAULT '{}'::jsonb,
            results JSONB NOT NULL DEFAULT '{}'::jsonb,
            checks JSONB NOT NULL DEFAULT '[]'::jsonb,
            error_message TEXT,
            CHECK ((status = 'running' AND finished_at IS NULL)
                OR (status <> 'running' AND finished_at IS NOT NULL))
        )
        """
    )
    op.execute(
        "CREATE INDEX idx_analytics_runs_dataset ON ops.analytics_runs "
        "(dataset_fingerprint, run_type, status, finished_at DESC)"
    )
    op.execute(
        """
        CREATE TABLE ops.analytics_state (
            singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
            dataset_fingerprint VARCHAR(71) NOT NULL,
            marts_run_id UUID NOT NULL,
            analysis_run_id UUID,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
        )
        """
    )

    op.execute(
        """
        CREATE TABLE mart._dataset_overview (
            run_id UUID NOT NULL,
            dataset_fingerprint VARCHAR(71) NOT NULL,
            user_count BIGINT NOT NULL,
            department_count BIGINT NOT NULL,
            aisle_count BIGINT NOT NULL,
            product_count BIGINT NOT NULL,
            order_count BIGINT NOT NULL,
            prior_order_count BIGINT NOT NULL,
            train_order_count BIGINT NOT NULL,
            test_order_count BIGINT NOT NULL,
            known_order_count BIGINT NOT NULL,
            known_item_count BIGINT NOT NULL,
            repeated_item_count BIGINT NOT NULL,
            avg_basket_size DOUBLE PRECISION,
            repeat_rate DOUBLE PRECISION,
            repeat_numerator BIGINT NOT NULL,
            repeat_denominator BIGINT NOT NULL,
            built_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            PRIMARY KEY (run_id)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE mart._order_activity (
            run_id UUID NOT NULL,
            dataset_fingerprint VARCHAR(71) NOT NULL,
            eval_set VARCHAR(8) NOT NULL,
            order_dow SMALLINT NOT NULL,
            order_hour_of_day SMALLINT NOT NULL,
            order_count BIGINT NOT NULL,
            PRIMARY KEY (run_id, eval_set, order_dow, order_hour_of_day)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE mart._order_summary (
            run_id UUID NOT NULL,
            dataset_fingerprint VARCHAR(71) NOT NULL,
            order_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            eval_set VARCHAR(8) NOT NULL,
            order_number SMALLINT NOT NULL,
            order_dow SMALLINT NOT NULL,
            order_hour_of_day SMALLINT NOT NULL,
            days_since_prior_order REAL,
            basket_size BIGINT NOT NULL,
            repeat_count BIGINT NOT NULL,
            aisle_count BIGINT NOT NULL,
            department_count BIGINT NOT NULL,
            PRIMARY KEY (run_id, order_id)
        )
        """
    )
    op.execute(
        "CREATE INDEX idx_mart_order_summary_filter ON mart._order_summary "
        "(run_id, eval_set, order_dow, basket_size)"
    )
    op.execute(
        "CREATE INDEX idx_mart_order_summary_user ON mart._order_summary "
        "(run_id, user_id, order_id)"
    )

    op.execute(
        """
        CREATE TABLE mart._department_reorders (
            run_id UUID NOT NULL,
            dataset_fingerprint VARCHAR(71) NOT NULL,
            eval_set VARCHAR(8) NOT NULL,
            group_type TEXT NOT NULL CHECK (group_type IN ('department', 'aisle')),
            group_id INTEGER NOT NULL,
            group_name TEXT NOT NULL,
            department_id SMALLINT NOT NULL,
            department_name TEXT NOT NULL,
            item_count BIGINT NOT NULL,
            reorder_count BIGINT NOT NULL,
            order_count BIGINT NOT NULL,
            customer_count BIGINT NOT NULL,
            PRIMARY KEY (run_id, eval_set, group_type, group_id)
        )
        """
    )
    op.execute(
        "CREATE INDEX idx_mart_department_reorders_filter ON mart._department_reorders "
        "(run_id, eval_set, department_id, group_type)"
    )
    op.execute(
        """
        CREATE TABLE mart._product_popularity (
            run_id UUID NOT NULL,
            dataset_fingerprint VARCHAR(71) NOT NULL,
            eval_set VARCHAR(8) NOT NULL,
            product_id INTEGER NOT NULL,
            product_name TEXT NOT NULL,
            aisle_id SMALLINT NOT NULL,
            aisle_name TEXT NOT NULL,
            department_id SMALLINT NOT NULL,
            department_name TEXT NOT NULL,
            item_count BIGINT NOT NULL,
            reorder_count BIGINT NOT NULL,
            order_count BIGINT NOT NULL,
            customer_count BIGINT NOT NULL,
            PRIMARY KEY (run_id, eval_set, product_id)
        )
        """
    )
    op.execute(
        "CREATE INDEX idx_mart_product_popularity_filter ON mart._product_popularity "
        "(run_id, eval_set, department_id, aisle_id, item_count DESC)"
    )
    op.execute(
        """
        CREATE TABLE mart._cart_position_stats (
            run_id UUID NOT NULL,
            dataset_fingerprint VARCHAR(71) NOT NULL,
            eval_set VARCHAR(8) NOT NULL,
            cart_position SMALLINT NOT NULL,
            basket_size SMALLINT NOT NULL DEFAULT 0,
            category_level TEXT NOT NULL DEFAULT '',
            category_id INTEGER NOT NULL DEFAULT 0,
            category_name TEXT NOT NULL DEFAULT '',
            item_count BIGINT NOT NULL,
            reorder_count BIGINT NOT NULL,
            order_count BIGINT NOT NULL,
            PRIMARY KEY (run_id, eval_set, cart_position, basket_size, category_level, category_id)
        )
        """
    )
    op.execute(
        "CREATE INDEX idx_mart_cart_position_filter ON mart._cart_position_stats "
        "(run_id, eval_set, basket_size, category_level, category_id, cart_position)"
    )

    op.execute(
        """
        CREATE TABLE mart._customer_features (
            run_id UUID NOT NULL,
            dataset_fingerprint VARCHAR(71) NOT NULL,
            user_id INTEGER NOT NULL,
            prior_order_count INTEGER NOT NULL,
            avg_basket_size DOUBLE PRECISION,
            reorder_rate DOUBLE PRECISION,
            avg_days_between_orders DOUBLE PRECISION,
            unique_product_count INTEGER NOT NULL,
            unique_aisle_count INTEGER NOT NULL,
            unique_department_count INTEGER NOT NULL,
            PRIMARY KEY (run_id, user_id)
        )
        """
    )
    op.execute(
        "CREATE INDEX idx_mart_customer_features_user ON mart._customer_features (run_id, user_id)"
    )
    op.execute(
        """
        CREATE TABLE mart._customer_segments (
            run_id UUID NOT NULL,
            dataset_fingerprint VARCHAR(71) NOT NULL,
            user_id INTEGER NOT NULL,
            cluster_id SMALLINT NOT NULL,
            pca_1 DOUBLE PRECISION NOT NULL,
            pca_2 DOUBLE PRECISION NOT NULL,
            PRIMARY KEY (run_id, user_id)
        )
        """
    )
    op.execute(
        "CREATE INDEX idx_mart_customer_segments_cluster ON mart._customer_segments "
        "(run_id, cluster_id)"
    )
    op.execute(
        """
        CREATE TABLE mart._cluster_metrics (
            run_id UUID NOT NULL,
            dataset_fingerprint VARCHAR(71) NOT NULL,
            k SMALLINT NOT NULL,
            inertia DOUBLE PRECISION NOT NULL,
            silhouette DOUBLE PRECISION NOT NULL,
            silhouette_sample_size INTEGER NOT NULL,
            cluster_sizes JSONB NOT NULL,
            selected BOOLEAN NOT NULL DEFAULT FALSE,
            PRIMARY KEY (run_id, k)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE mart._cluster_profiles (
            run_id UUID NOT NULL,
            dataset_fingerprint VARCHAR(71) NOT NULL,
            cluster_id SMALLINT NOT NULL,
            customer_count BIGINT NOT NULL,
            feature_means JSONB NOT NULL,
            transformed_center JSONB NOT NULL,
            PRIMARY KEY (run_id, cluster_id)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE mart._hypothesis_results (
            run_id UUID NOT NULL,
            dataset_fingerprint VARCHAR(71) NOT NULL,
            hypothesis_id TEXT NOT NULL,
            h0 TEXT NOT NULL,
            h1 TEXT NOT NULL,
            method TEXT NOT NULL,
            observation_count BIGINT NOT NULL,
            statistic DOUBLE PRECISION,
            p_value DOUBLE PRECISION,
            effect_name TEXT,
            effect_value DOUBLE PRECISION,
            ci_lower DOUBLE PRECISION,
            ci_upper DOUBLE PRECISION,
            interpretation TEXT NOT NULL,
            metrics JSONB NOT NULL,
            PRIMARY KEY (run_id, hypothesis_id)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE mart._hypothesis_categories (
            run_id UUID NOT NULL,
            dataset_fingerprint VARCHAR(71) NOT NULL,
            hypothesis_id TEXT NOT NULL,
            category_level TEXT NOT NULL,
            category_id INTEGER NOT NULL,
            category_name TEXT NOT NULL,
            eval_set TEXT NOT NULL DEFAULT 'all',
            cart_position SMALLINT NOT NULL DEFAULT 0,
            item_count BIGINT NOT NULL,
            reorder_count BIGINT NOT NULL,
            reorder_rate DOUBLE PRECISION NOT NULL,
            ci_lower DOUBLE PRECISION NOT NULL,
            ci_upper DOUBLE PRECISION NOT NULL,
            metrics JSONB NOT NULL DEFAULT '{}'::jsonb,
            PRIMARY KEY (
                run_id, hypothesis_id, category_level, category_id, eval_set, cart_position
            )
        )
        """
    )
    op.execute(
        "CREATE INDEX idx_mart_hypothesis_categories_filter ON mart._hypothesis_categories "
        "(run_id, hypothesis_id, category_level, eval_set, cart_position)"
    )
    op.execute(
        """
        CREATE TABLE mart._hypothesis_pairs (
            run_id UUID NOT NULL,
            dataset_fingerprint VARCHAR(71) NOT NULL,
            comparison TEXT NOT NULL,
            first_position SMALLINT NOT NULL,
            second_position SMALLINT NOT NULL,
            department_matched BOOLEAN NOT NULL,
            order_count BIGINT NOT NULL,
            discordant_first_only BIGINT NOT NULL,
            discordant_second_only BIGINT NOT NULL,
            risk_difference DOUBLE PRECISION NOT NULL,
            ci_lower DOUBLE PRECISION,
            ci_upper DOUBLE PRECISION,
            p_value DOUBLE PRECISION NOT NULL,
            p_adjusted DOUBLE PRECISION NOT NULL,
            bootstrap_ci_lower DOUBLE PRECISION,
            bootstrap_ci_upper DOUBLE PRECISION,
            PRIMARY KEY (run_id, comparison, department_matched)
        )
        """
    )

    # Public views expose only the snapshot that matches the active core import.
    for view, table in (
        ("dataset_overview", "_dataset_overview"),
        ("order_activity", "_order_activity"),
        ("order_summary", "_order_summary"),
        ("department_reorders", "_department_reorders"),
        ("product_popularity", "_product_popularity"),
        ("cart_position_stats", "_cart_position_stats"),
    ):
        op.execute(
            f"CREATE VIEW mart.{view} AS SELECT d.* FROM mart.{table} d "
            "JOIN ops.analytics_state s ON s.singleton AND s.marts_run_id = d.run_id "
            "JOIN ops.active_dataset a ON a.singleton "
            "AND a.dataset_fingerprint = d.dataset_fingerprint "
            "WHERE s.dataset_fingerprint = a.dataset_fingerprint"
        )
    op.execute(
        "CREATE VIEW mart.customer_features AS "
        "SELECT d.* FROM mart._customer_features d "
        "JOIN ops.analytics_state s ON s.singleton AND s.marts_run_id = d.run_id "
        "JOIN ops.active_dataset a ON a.singleton "
        "AND a.dataset_fingerprint = d.dataset_fingerprint "
        "WHERE s.dataset_fingerprint = a.dataset_fingerprint"
    )
    for view, table in (
        ("customer_segments", "_customer_segments"),
        ("cluster_metrics", "_cluster_metrics"),
        ("cluster_profiles", "_cluster_profiles"),
        ("hypothesis_results", "_hypothesis_results"),
        ("hypothesis_categories", "_hypothesis_categories"),
        ("hypothesis_pairs", "_hypothesis_pairs"),
    ):
        op.execute(
            f"CREATE VIEW mart.{view} AS SELECT d.* FROM mart.{table} d "
            "JOIN ops.analytics_state s ON s.singleton AND s.analysis_run_id = d.run_id "
            "JOIN ops.active_dataset a ON a.singleton "
            "AND a.dataset_fingerprint = d.dataset_fingerprint "
            "WHERE s.dataset_fingerprint = a.dataset_fingerprint"
        )


def downgrade() -> None:
    for view in (
        "dataset_overview",
        "order_activity",
        "order_summary",
        "department_reorders",
        "product_popularity",
        "cart_position_stats",
        "customer_features",
        "customer_segments",
        "cluster_metrics",
        "cluster_profiles",
        "hypothesis_results",
        "hypothesis_categories",
        "hypothesis_pairs",
    ):
        op.execute(f"DROP VIEW mart.{view}")
    for table in (
        "_hypothesis_pairs",
        "_hypothesis_categories",
        "_hypothesis_results",
        "_cluster_profiles",
        "_cluster_metrics",
        "_customer_segments",
        "_customer_features",
        "_cart_position_stats",
        "_product_popularity",
        "_department_reorders",
        "_order_summary",
        "_order_activity",
        "_dataset_overview",
    ):
        op.execute(f"DROP TABLE mart.{table}")
    op.execute("DROP TABLE ops.analytics_state")
    op.execute("DROP TABLE ops.analytics_runs")
