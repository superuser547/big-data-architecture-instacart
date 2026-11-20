"""Post-import PostgreSQL integrity checks."""

from __future__ import annotations

from typing import Any

import psycopg


def _scalar(connection: psycopg.Connection, query: str, params: tuple = ()) -> int:
    return int(connection.execute(query, params).fetchone()[0])


def validate_database(
    connection: psycopg.Connection, expected_counts: dict[str, int] | None = None
) -> dict[str, Any]:
    items_by_eval_set = dict(
        connection.execute(
            "SELECT o.eval_set, count(*) FROM core.order_items i "
            "JOIN core.orders o USING (order_id) GROUP BY o.eval_set"
        ).fetchall()
    )
    counts = {
        "users": _scalar(connection, "SELECT count(*) FROM core.users"),
        "departments": _scalar(connection, "SELECT count(*) FROM core.departments"),
        "aisles": _scalar(connection, "SELECT count(*) FROM core.aisles"),
        "products": _scalar(connection, "SELECT count(*) FROM core.products"),
        "orders": _scalar(connection, "SELECT count(*) FROM core.orders"),
        "order_items": _scalar(connection, "SELECT count(*) FROM core.order_items"),
        "prior_orders": _scalar(
            connection, "SELECT count(*) FROM core.orders WHERE eval_set = 'prior'"
        ),
        "train_orders": _scalar(
            connection, "SELECT count(*) FROM core.orders WHERE eval_set = 'train'"
        ),
        "test_orders": _scalar(
            connection, "SELECT count(*) FROM core.orders WHERE eval_set = 'test'"
        ),
        "prior_items": int(items_by_eval_set.get("prior", 0)),
        "train_items": int(items_by_eval_set.get("train", 0)),
        "test_items": int(items_by_eval_set.get("test", 0)),
        "null_days_since_prior_order": _scalar(
            connection, "SELECT count(*) FROM core.orders WHERE days_since_prior_order IS NULL"
        ),
    }
    checks: list[dict[str, Any]] = []

    def check(name: str, violations: int, detail: str) -> None:
        checks.append(
            {"name": name, "passed": violations == 0, "violations": violations, "detail": detail}
        )

    check(
        "users_from_orders",
        _scalar(
            connection,
            "SELECT abs((SELECT count(*) FROM core.users) - "
            "(SELECT count(DISTINCT user_id) FROM core.orders))",
        ),
        "users equal distinct order.user_id values",
    )
    check(
        "order_item_eval_set",
        counts["test_items"],
        "test orders have no known item rows",
    )
    check(
        "order_items_order_fk",
        _scalar(
            connection,
            "SELECT count(*) FROM pg_constraint WHERE conrelid = 'core.order_items'::regclass "
            "AND conname = 'fk_order_items_order' AND NOT convalidated",
        ),
        "the order foreign key is validated by PostgreSQL",
    )
    check(
        "order_items_product_fk",
        _scalar(
            connection,
            "SELECT count(*) FROM pg_constraint WHERE conrelid = 'core.order_items'::regclass "
            "AND conname = 'fk_order_items_product' AND NOT convalidated",
        ),
        "the product foreign key is validated by PostgreSQL",
    )
    check(
        "order_number_sequence",
        _scalar(
            connection,
            "SELECT count(*) FROM (SELECT user_id FROM core.orders GROUP BY user_id "
            "HAVING min(order_number) <> 1 OR max(order_number) <> count(*)) q",
        ),
        "each user's order numbers start at 1 and have no gaps",
    )
    check(
        "first_order_days_null",
        _scalar(
            connection,
            "SELECT count(*) FROM core.orders WHERE "
            "(order_number = 1 AND days_since_prior_order IS NOT NULL) OR "
            "(order_number > 1 AND days_since_prior_order IS NULL)",
        ),
        "only first orders have NULL days_since_prior_order",
    )
    check(
        "cart_position_sequence",
        _scalar(
            connection,
            "SELECT count(*) FROM (SELECT order_id FROM core.order_items GROUP BY order_id "
            "HAVING min(add_to_cart_order) <> 1 OR max(add_to_cart_order) <> count(*)) q",
        ),
        "cart positions start at 1 and are contiguous",
    )
    check(
        "aisle_department_consistency",
        _scalar(
            connection,
            "SELECT count(*) FROM (SELECT a.aisle_id FROM core.aisles a "
            "LEFT JOIN core.products p USING (aisle_id) "
            "GROUP BY a.aisle_id, a.department_id HAVING count(p.product_id) = 0 "
            "OR a.department_id IS NULL) q",
        ),
        "every aisle is assigned to a department and contains mapped products",
    )
    if expected_counts is not None:
        for key in ("users", "departments", "aisles", "products", "orders", "order_items"):
            check(
                f"source_count.{key}",
                abs(counts[key] - int(expected_counts[key])),
                f"database={counts[key]}, source={expected_counts[key]}",
            )
        for key in (
            "prior_orders",
            "train_orders",
            "test_orders",
            "prior_items",
            "train_items",
            "null_days_since_prior_order",
        ):
            check(
                f"source_count.{key}",
                abs(counts[key] - int(expected_counts[key])),
                f"database={counts[key]}, source={expected_counts[key]}",
            )
    size_bytes = _scalar(
        connection,
        "SELECT coalesce(sum(pg_total_relation_size(c.oid)), 0) FROM pg_class c "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = 'core' AND c.relkind IN ('r', 'm')",
    )
    return {
        "passed": all(item["passed"] for item in checks),
        "counts": counts,
        "checks": checks,
        "core_size_bytes": size_bytes,
    }
