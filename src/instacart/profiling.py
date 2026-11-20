"""Out-of-core source profiling and invariant validation with DuckDB."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import duckdb

from instacart.config import Settings
from instacart.dataset import Dataset

SOURCE_TYPES: dict[str, dict[str, str]] = {
    "orders.csv": {
        "order_id": "BIGINT",
        "user_id": "BIGINT",
        "eval_set": "VARCHAR",
        "order_number": "INTEGER",
        "order_dow": "INTEGER",
        "order_hour_of_day": "INTEGER",
        "days_since_prior_order": "DOUBLE",
    },
    "products.csv": {
        "product_id": "INTEGER",
        "product_name": "VARCHAR",
        "aisle_id": "INTEGER",
        "department_id": "INTEGER",
    },
    "aisles.csv": {"aisle_id": "INTEGER", "aisle": "VARCHAR"},
    "departments.csv": {"department_id": "INTEGER", "department": "VARCHAR"},
    "order_products__prior.csv": {
        "order_id": "BIGINT",
        "product_id": "INTEGER",
        "add_to_cart_order": "INTEGER",
        "reordered": "INTEGER",
    },
    "order_products__train.csv": {
        "order_id": "BIGINT",
        "product_id": "INTEGER",
        "add_to_cart_order": "INTEGER",
        "reordered": "INTEGER",
    },
}

REFERENCE_COUNTS = {
    "users": 206_209,
    "departments": 21,
    "aisles": 134,
    "products": 49_688,
    "orders": 3_421_083,
    "order_items": 33_819_106,
    "prior_orders": 3_214_874,
    "prior_items": 32_434_489,
    "train_orders": 131_209,
    "train_items": 1_384_617,
    "test_orders": 75_000,
    "null_days_since_prior_order": 206_209,
}


def _literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _read_csv_sql(path: Path, columns: dict[str, str]) -> str:
    column_map = ", ".join(f"'{name}': '{kind}'" for name, kind in columns.items())
    return (
        f"read_csv({_literal(str(path))}, header=true, columns={{{column_map}}}, "
        "nullstr='', strict_mode=true, ignore_errors=false, sample_size=-1)"
    )


def _query_scalar(connection: duckdb.DuckDBPyConnection, sql: str) -> int:
    return int(connection.execute(sql).fetchone()[0])


def profile_dataset(dataset: Dataset, settings: Settings) -> dict[str, Any]:
    """Validate source contracts and return a JSON-serializable audit report."""
    started = time.monotonic()
    settings.duckdb_temp_directory.mkdir(parents=True, exist_ok=True)
    connection = duckdb.connect(database=":memory:")
    try:
        connection.execute(f"SET memory_limit={_literal(settings.duckdb_memory_limit)}")
        connection.execute(f"SET threads={int(settings.duckdb_threads)}")
        connection.execute(
            f"SET temp_directory={_literal(str(settings.duckdb_temp_directory.resolve()))}"
        )
        for filename, columns in SOURCE_TYPES.items():
            table = "src_" + filename.removesuffix(".csv").replace("__", "_")
            connection.execute(
                f"CREATE TABLE {table} AS SELECT * FROM "
                f"{_read_csv_sql(dataset.files[filename], columns)}"
            )

        table_names = {
            "orders.csv": "src_orders",
            "products.csv": "src_products",
            "aisles.csv": "src_aisles",
            "departments.csv": "src_departments",
            "order_products__prior.csv": "src_order_products_prior",
            "order_products__train.csv": "src_order_products_train",
        }
        tables: dict[str, dict[str, Any]] = {}
        for filename, table in table_names.items():
            columns = SOURCE_TYPES[filename]
            count = _query_scalar(connection, f"SELECT count(*) FROM {table}")
            nulls = {
                column: _query_scalar(
                    connection, f"SELECT count(*) FROM {table} WHERE {column} IS NULL"
                )
                for column in columns
            }
            types = {row[0]: row[1] for row in connection.execute(f"DESCRIBE {table}").fetchall()}
            tables[filename] = {"rows": count, "nulls": nulls, "types": types}

        checks: list[dict[str, Any]] = []

        def check(name: str, invalid_count: int, detail: str) -> None:
            checks.append(
                {
                    "name": name,
                    "passed": invalid_count == 0,
                    "violations": invalid_count,
                    "detail": detail,
                }
            )

        # Key uniqueness and mandatory source fields.
        for table, key in (
            ("src_orders", "order_id"),
            ("src_products", "product_id"),
            ("src_aisles", "aisle_id"),
            ("src_departments", "department_id"),
        ):
            check(
                f"{table}.key_not_null",
                _query_scalar(connection, f"SELECT count(*) FROM {table} WHERE {key} IS NULL"),
                key,
            )
            check(
                f"{table}.key_unique",
                _query_scalar(
                    connection,
                    f"SELECT count(*) FROM (SELECT {key} FROM {table} "
                    f"GROUP BY {key} HAVING count(*) > 1) d",
                ),
                key,
            )
        check(
            "orders.user_order_unique",
            _query_scalar(
                connection,
                "SELECT count(*) FROM (SELECT user_id, order_number FROM src_orders "
                "GROUP BY user_id, order_number HAVING count(*) > 1) d",
            ),
            "UNIQUE (user_id, order_number)",
        )

        for filename, table in (
            ("order_products__prior.csv", "src_order_products_prior"),
            ("order_products__train.csv", "src_order_products_train"),
        ):
            check(
                f"{filename}.keys_not_null",
                _query_scalar(
                    connection,
                    f"SELECT count(*) FROM {table} WHERE order_id IS NULL OR product_id IS NULL "
                    "OR add_to_cart_order IS NULL OR reordered IS NULL",
                ),
                "all four fields are required",
            )
            for label, expression in (
                ("order_product_unique", "order_id, product_id"),
                ("cart_position_unique", "order_id, add_to_cart_order"),
            ):
                check(
                    f"{filename}.{label}",
                    _query_scalar(
                        connection,
                        f"SELECT count(*) FROM (SELECT {expression} FROM {table} "
                        f"GROUP BY {expression} HAVING count(*) > 1) d",
                    ),
                    expression,
                )

        checks_sql = {
            "departments.id_positive": (
                "SELECT count(*) FROM src_departments WHERE department_id <= 0"
            ),
            "departments.name_present": (
                "SELECT count(*) FROM src_departments "
                "WHERE department IS NULL OR trim(department) = ''"
            ),
            "aisles.id_positive": "SELECT count(*) FROM src_aisles WHERE aisle_id <= 0",
            "aisles.name_present": (
                "SELECT count(*) FROM src_aisles WHERE aisle IS NULL OR trim(aisle) = ''"
            ),
            "products.fields_present": (
                "SELECT count(*) FROM src_products WHERE product_name IS NULL "
                "OR trim(product_name) = '' OR aisle_id IS NULL OR department_id IS NULL"
            ),
            "products.ids_positive": (
                "SELECT count(*) FROM src_products "
                "WHERE product_id <= 0 OR aisle_id <= 0 OR department_id <= 0"
            ),
            "products.aisle_exists": (
                "SELECT count(*) FROM src_products p LEFT JOIN src_aisles a USING (aisle_id) "
                "WHERE a.aisle_id IS NULL"
            ),
            "products.department_exists": (
                "SELECT count(*) FROM src_products p "
                "LEFT JOIN src_departments d USING (department_id) WHERE d.department_id IS NULL"
            ),
            "orders.fields_present": (
                "SELECT count(*) FROM src_orders WHERE order_id IS NULL OR user_id IS NULL "
                "OR eval_set IS NULL OR order_number IS NULL OR order_dow IS NULL "
                "OR order_hour_of_day IS NULL"
            ),
            "orders.eval_set_allowed": (
                "SELECT count(*) FROM src_orders WHERE eval_set NOT IN ('prior', 'train', 'test')"
            ),
            "orders.values_in_range": (
                "SELECT count(*) FROM src_orders WHERE order_id <= 0 OR user_id <= 0 "
                "OR order_number < 1 OR order_number > 32767 "
                "OR order_dow NOT BETWEEN 0 AND 6 "
                "OR order_hour_of_day NOT BETWEEN 0 AND 23 "
                "OR days_since_prior_order < 0 OR days_since_prior_order > 30"
            ),
            "orders.first_order_null_days": (
                "SELECT count(*) FROM src_orders "
                "WHERE (order_number = 1 AND days_since_prior_order IS NOT NULL) "
                "OR (order_number > 1 AND days_since_prior_order IS NULL)"
            ),
            "orders.number_sequence": (
                "SELECT count(*) FROM (SELECT user_id FROM src_orders GROUP BY user_id "
                "HAVING min(order_number) <> 1 OR max(order_number) <> count(*)) d"
            ),
        }
        for filename, table in (
            ("order_products__prior.csv", "src_order_products_prior"),
            ("order_products__train.csv", "src_order_products_train"),
        ):
            checks_sql[f"{filename}.values_in_range"] = (
                f"SELECT count(*) FROM {table} WHERE order_id <= 0 OR product_id <= 0 "
                "OR add_to_cart_order < 1 OR add_to_cart_order > 32767 OR reordered NOT IN (0, 1)"
            )
            checks_sql[f"{filename}.order_exists"] = (
                f"SELECT count(*) FROM {table} i LEFT JOIN src_orders o USING (order_id) "
                "WHERE o.order_id IS NULL"
            )
            checks_sql[f"{filename}.eval_set_matches"] = (
                f"SELECT count(*) FROM {table} i JOIN src_orders o USING (order_id) "
                f"WHERE o.eval_set <> '{'prior' if 'prior' in filename else 'train'}'"
            )
            checks_sql[f"{filename}.product_exists"] = (
                f"SELECT count(*) FROM {table} i LEFT JOIN src_products p USING (product_id) "
                "WHERE p.product_id IS NULL"
            )
            checks_sql[f"{filename}.cart_sequence"] = (
                f"SELECT count(*) FROM (SELECT order_id FROM {table} GROUP BY order_id "
                "HAVING min(add_to_cart_order) <> 1 OR max(add_to_cart_order) <> count(*)) d"
            )
        checks_sql["products.aisle_department_consistent"] = (
            "SELECT count(*) FROM (SELECT aisle_id FROM src_products GROUP BY aisle_id "
            "HAVING count(DISTINCT department_id) <> 1) d"
        )
        checks_sql["aisles.have_product_mapping"] = (
            "SELECT count(*) FROM src_aisles a LEFT JOIN "
            "(SELECT DISTINCT aisle_id FROM src_products) p USING (aisle_id) "
            "WHERE p.aisle_id IS NULL"
        )
        for name, sql in checks_sql.items():
            check(name, _query_scalar(connection, sql), "zero invalid rows/groups expected")

        total_items = (
            tables["order_products__prior.csv"]["rows"]
            + tables["order_products__train.csv"]["rows"]
        )
        order_sets = connection.execute(
            "SELECT eval_set, count(*) FROM src_orders GROUP BY eval_set ORDER BY eval_set"
        ).fetchall()
        counts = {
            "users": _query_scalar(connection, "SELECT count(DISTINCT user_id) FROM src_orders"),
            "departments": tables["departments.csv"]["rows"],
            "aisles": tables["aisles.csv"]["rows"],
            "products": tables["products.csv"]["rows"],
            "orders": tables["orders.csv"]["rows"],
            "null_days_since_prior_order": tables["orders.csv"]["nulls"]["days_since_prior_order"],
            "order_items": total_items,
            "prior_orders": dict(order_sets).get("prior", 0),
            "prior_items": tables["order_products__prior.csv"]["rows"],
            "train_orders": dict(order_sets).get("train", 0),
            "train_items": tables["order_products__train.csv"]["rows"],
            "test_orders": dict(order_sets).get("test", 0),
        }
        full_reference_match = counts == REFERENCE_COUNTS
        return {
            "dataset_fingerprint": dataset.fingerprint,
            "source": dataset.source,
            "file_sha256": dataset.file_sha256,
            "tables": tables,
            "derived_counts": counts,
            "invariants": {"passed": all(item["passed"] for item in checks), "checks": checks},
            "reference_counts": {
                "applicable": full_reference_match,
                "expected": REFERENCE_COUNTS,
                "observed": counts,
                "matched": full_reference_match,
            },
            "duration_seconds": round(time.monotonic() - started, 3),
        }
    finally:
        connection.close()
