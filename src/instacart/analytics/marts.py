"""SQL-first analytical marts; Python only sees customer-level aggregates."""

from __future__ import annotations

import uuid

import psycopg


def _count(connection: psycopg.Connection, relation: str) -> int:
    return int(connection.execute(f"SELECT count(*) FROM {relation}").fetchone()[0])


def build_marts(
    connection: psycopg.Connection,
    run_id: uuid.UUID,
    fingerprint: str,
) -> dict[str, int]:
    """Rebuild all SQL aggregates in the caller's transaction."""
    previous = connection.execute(
        "SELECT dataset_fingerprint FROM ops.analytics_state WHERE singleton"
    ).fetchone()
    changed_dataset = previous is not None and previous[0] != fingerprint
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
        connection.execute(f"DELETE FROM mart.{table}")
    if changed_dataset:
        for table in (
            "_customer_segments",
            "_cluster_metrics",
            "_cluster_profiles",
            "_hypothesis_results",
            "_hypothesis_categories",
            "_hypothesis_pairs",
        ):
            connection.execute(f"DELETE FROM mart.{table}")

    connection.execute(
        """
        CREATE TEMP TABLE tmp_order_metrics ON COMMIT DROP AS
        SELECT o.order_id, o.user_id, o.eval_set, o.order_number, o.order_dow,
               o.order_hour_of_day, o.days_since_prior_order,
               count(*)::BIGINT AS basket_size,
               sum(i.reordered::INTEGER)::BIGINT AS repeat_count,
               count(DISTINCT a.aisle_id)::BIGINT AS aisle_count,
               count(DISTINCT d.department_id)::BIGINT AS department_count
        FROM core.orders o
        JOIN core.order_items i USING (order_id)
        JOIN core.products p USING (product_id)
        JOIN core.aisles a USING (aisle_id)
        JOIN core.departments d USING (department_id)
        WHERE o.eval_set IN ('prior', 'train')
        GROUP BY o.order_id, o.user_id, o.eval_set, o.order_number, o.order_dow,
                 o.order_hour_of_day, o.days_since_prior_order
        """
    )
    connection.execute("CREATE UNIQUE INDEX ON tmp_order_metrics (order_id)")
    connection.execute("ANALYZE tmp_order_metrics")
    connection.execute(
        """
        INSERT INTO mart._order_summary
            (run_id, dataset_fingerprint, order_id, user_id, eval_set, order_number,
             order_dow, order_hour_of_day, days_since_prior_order, basket_size, repeat_count,
             aisle_count, department_count)
        SELECT %s, %s, order_id, user_id, eval_set, order_number, order_dow,
               order_hour_of_day, days_since_prior_order, basket_size, repeat_count,
               aisle_count, department_count
        FROM tmp_order_metrics
        """,
        (run_id, fingerprint),
    )
    connection.execute(
        """
        INSERT INTO mart._order_activity
            (run_id, dataset_fingerprint, eval_set, order_dow, order_hour_of_day, order_count)
        SELECT %s, %s, eval_set, order_dow, order_hour_of_day, count(*)
        FROM core.orders
        GROUP BY eval_set, order_dow, order_hour_of_day
        """,
        (run_id, fingerprint),
    )
    connection.execute(
        """
        INSERT INTO mart._dataset_overview
            (run_id, dataset_fingerprint, user_count, department_count, aisle_count,
             product_count, order_count, prior_order_count, train_order_count, test_order_count,
             known_order_count, known_item_count, repeated_item_count, avg_basket_size,
             repeat_rate, repeat_numerator, repeat_denominator)
        WITH order_counts AS (
            SELECT count(*) FILTER (WHERE eval_set = 'prior') AS prior_count,
                   count(*) FILTER (WHERE eval_set = 'train') AS train_count,
                   count(*) FILTER (WHERE eval_set = 'test') AS test_count,
                   count(*) AS total_count
            FROM core.orders
        ), item_metrics AS (
            SELECT count(*)::BIGINT AS item_count,
                   coalesce(sum(repeat_count), 0)::BIGINT AS repeated_count,
                   count(*)::BIGINT AS known_orders,
                   sum(basket_size)::BIGINT AS known_items
            FROM tmp_order_metrics
        )
        SELECT %s, %s,
               (SELECT count(*) FROM core.users),
               (SELECT count(*) FROM core.departments),
               (SELECT count(*) FROM core.aisles),
               (SELECT count(*) FROM core.products),
               o.total_count, o.prior_count, o.train_count, o.test_count,
               m.known_orders, m.known_items, m.repeated_count,
               m.known_items::DOUBLE PRECISION / NULLIF(m.known_orders, 0),
               m.repeated_count::DOUBLE PRECISION / NULLIF(m.known_items, 0),
               m.repeated_count, m.known_items
        FROM order_counts o CROSS JOIN item_metrics m
        """,
        (run_id, fingerprint),
    )

    # One grouping-sets pass over order_items feeds category, product and cart-position marts.
    connection.execute(
        """
        CREATE TEMP TABLE tmp_item_rollups ON COMMIT DROP AS
        SELECT o.eval_set,
               GROUPING(d.department_id) = 0 AS has_department,
               GROUPING(a.aisle_id) = 0 AS has_aisle,
               GROUPING(p.product_id) = 0 AS has_product,
               GROUPING(i.add_to_cart_order) = 0 AS has_position,
               GROUPING(om.basket_size) = 0 AS has_basket_size,
               d.department_id, d.department_name, a.aisle_id, a.aisle_name,
               p.product_id, p.product_name, i.add_to_cart_order,
               om.basket_size,
               count(*)::BIGINT AS item_count,
               sum(i.reordered::INTEGER)::BIGINT AS reorder_count,
               count(DISTINCT o.order_id)::BIGINT AS order_count,
               count(DISTINCT o.user_id)::BIGINT AS customer_count
        FROM core.order_items i
        JOIN core.orders o USING (order_id)
        JOIN tmp_order_metrics om USING (order_id)
        JOIN core.products p USING (product_id)
        JOIN core.aisles a USING (aisle_id)
        JOIN core.departments d USING (department_id)
        GROUP BY GROUPING SETS (
            (o.eval_set, d.department_id, d.department_name),
            (o.eval_set, d.department_id, d.department_name, a.aisle_id, a.aisle_name),
            (o.eval_set, d.department_id, d.department_name, a.aisle_id, a.aisle_name,
             p.product_id, p.product_name),
            (o.eval_set, i.add_to_cart_order, om.basket_size),
            (o.eval_set, i.add_to_cart_order, d.department_id, d.department_name),
            (o.eval_set, i.add_to_cart_order, a.aisle_id, a.aisle_name)
        )
        """
    )
    connection.execute("ANALYZE tmp_item_rollups")
    # This sparse aggregate supports combined dashboard filters without copying line items.
    connection.execute(
        """
        CREATE TEMP TABLE tmp_category_activity ON COMMIT DROP AS
        SELECT o.eval_set, o.order_dow, o.order_hour_of_day, om.basket_size,
               GROUPING(a.aisle_id) = 1 AS is_department,
               d.department_id, d.department_name, a.aisle_id, a.aisle_name,
               count(*)::BIGINT AS item_count,
               sum(i.reordered::INTEGER)::BIGINT AS reorder_count
        FROM core.order_items i
        JOIN core.orders o USING (order_id)
        JOIN tmp_order_metrics om USING (order_id)
        JOIN core.products p USING (product_id)
        JOIN core.aisles a USING (aisle_id)
        JOIN core.departments d USING (department_id)
        GROUP BY GROUPING SETS (
            (o.eval_set, o.order_dow, o.order_hour_of_day, om.basket_size,
             d.department_id, d.department_name),
            (o.eval_set, o.order_dow, o.order_hour_of_day, om.basket_size,
             d.department_id, d.department_name, a.aisle_id, a.aisle_name)
        )
        """
    )
    connection.execute(
        """
        INSERT INTO mart._category_activity
            (run_id, dataset_fingerprint, eval_set, order_dow, order_hour_of_day,
             basket_size, category_level, category_id, category_name,
             department_id, department_name, item_count, reorder_count)
        SELECT %s, %s, eval_set, order_dow, order_hour_of_day, basket_size,
               CASE WHEN is_department THEN 'department' ELSE 'aisle' END,
               CASE WHEN is_department THEN department_id ELSE aisle_id END,
               CASE WHEN is_department THEN department_name ELSE aisle_name END,
               department_id, department_name, item_count, reorder_count
        FROM tmp_category_activity
        """,
        (run_id, fingerprint),
    )
    connection.execute(
        """
        INSERT INTO mart._department_reorders
            (run_id, dataset_fingerprint, eval_set, group_type, group_id, group_name,
             department_id, department_name, item_count, reorder_count, order_count, customer_count)
        SELECT %s, %s, eval_set,
               CASE WHEN has_aisle THEN 'aisle' ELSE 'department' END,
               CASE WHEN has_aisle THEN aisle_id ELSE department_id END,
               CASE WHEN has_aisle THEN aisle_name ELSE department_name END,
               department_id, department_name, item_count, reorder_count, order_count,
               customer_count
        FROM tmp_item_rollups
        WHERE NOT has_product AND NOT has_position AND (has_department OR has_aisle)
        """,
        (run_id, fingerprint),
    )
    connection.execute(
        """
        INSERT INTO mart._product_popularity
            (run_id, dataset_fingerprint, eval_set, product_id, product_name,
             aisle_id, aisle_name, department_id, department_name,
             item_count, reorder_count, order_count, customer_count)
        SELECT %s, %s, r.eval_set, r.product_id, r.product_name,
               r.aisle_id, a.aisle_name, r.department_id, r.department_name,
               r.item_count, r.reorder_count, r.order_count, r.customer_count
        FROM tmp_item_rollups r
        JOIN core.products p USING (product_id)
        JOIN core.aisles a ON a.aisle_id = r.aisle_id
        WHERE r.has_product
        """,
        (run_id, fingerprint),
    )
    connection.execute(
        """
        INSERT INTO mart._cart_position_stats
            (run_id, dataset_fingerprint, eval_set, cart_position, basket_size,
             category_level, category_id, category_name, item_count, reorder_count, order_count)
        SELECT %s, %s, eval_set, add_to_cart_order::SMALLINT,
               CASE WHEN has_basket_size THEN basket_size::SMALLINT ELSE 0 END,
               CASE WHEN has_position AND has_basket_size THEN ''
                    WHEN has_department AND NOT has_aisle THEN 'department'
                    ELSE 'aisle' END,
               CASE WHEN has_position AND has_basket_size THEN 0
                    WHEN has_department AND NOT has_aisle THEN department_id ELSE aisle_id END,
               CASE WHEN has_position AND has_basket_size THEN ''
                    WHEN has_department AND NOT has_aisle THEN department_name ELSE aisle_name END,
               item_count, reorder_count, order_count
        FROM tmp_item_rollups
        WHERE has_position AND NOT has_product
          AND ((has_basket_size AND NOT has_department AND NOT has_aisle)
               OR (has_department AND NOT has_aisle AND NOT has_basket_size)
               OR (has_aisle AND NOT has_basket_size))
        """,
        (run_id, fingerprint),
    )
    connection.execute(
        """
        INSERT INTO mart._customer_features
            (run_id, dataset_fingerprint, user_id, prior_order_count, avg_basket_size,
             reorder_rate, avg_days_between_orders, unique_product_count,
             unique_aisle_count, unique_department_count)
        WITH order_features AS (
            SELECT user_id, count(*)::INTEGER AS prior_order_count,
                   avg(days_since_prior_order)::DOUBLE PRECISION AS avg_days_between_orders
            FROM core.orders
            WHERE eval_set = 'prior'
            GROUP BY user_id
        ), item_features AS (
            SELECT o.user_id, count(*)::BIGINT AS item_count,
                   sum(i.reordered::INTEGER)::BIGINT AS repeat_count,
                   count(DISTINCT i.product_id)::INTEGER AS unique_product_count,
                   count(DISTINCT p.aisle_id)::INTEGER AS unique_aisle_count,
                   count(DISTINCT a.department_id)::INTEGER AS unique_department_count
            FROM core.orders o
            JOIN core.order_items i USING (order_id)
            JOIN core.products p USING (product_id)
            JOIN core.aisles a USING (aisle_id)
            WHERE o.eval_set = 'prior'
            GROUP BY o.user_id
        )
        SELECT %s, %s, o.user_id, o.prior_order_count,
               coalesce(i.item_count::DOUBLE PRECISION / NULLIF(o.prior_order_count, 0), 0),
               i.repeat_count::DOUBLE PRECISION / NULLIF(i.item_count, 0),
               o.avg_days_between_orders,
               coalesce(i.unique_product_count, 0), coalesce(i.unique_aisle_count, 0),
               coalesce(i.unique_department_count, 0)
        FROM order_features o
        LEFT JOIN item_features i USING (user_id)
        """,
        (run_id, fingerprint),
    )

    counts = {
        "dataset_overview": _count(connection, "mart._dataset_overview"),
        "order_activity": _count(connection, "mart._order_activity"),
        "order_summary": _count(connection, "mart._order_summary"),
        "category_activity": _count(connection, "mart._category_activity"),
        "department_reorders": _count(connection, "mart._department_reorders"),
        "product_popularity": _count(connection, "mart._product_popularity"),
        "cart_position_stats": _count(connection, "mart._cart_position_stats"),
        "customer_features": _count(connection, "mart._customer_features"),
    }
    connection.execute(
        "INSERT INTO ops.analytics_state (singleton, dataset_fingerprint, marts_run_id, "
        "analysis_run_id) VALUES (TRUE, %s, %s, NULL) ON CONFLICT (singleton) DO UPDATE SET "
        "dataset_fingerprint = EXCLUDED.dataset_fingerprint, marts_run_id = EXCLUDED.marts_run_id, "
        "analysis_run_id = CASE WHEN ops.analytics_state.dataset_fingerprint "
        "= EXCLUDED.dataset_fingerprint "
        "THEN ops.analytics_state.analysis_run_id ELSE NULL END, updated_at = clock_timestamp()",
        (fingerprint, run_id),
    )
    return counts
