"""Transactional, streaming PostgreSQL ingestion."""

from __future__ import annotations

import csv
import logging
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from instacart.config import Settings
from instacart.dataset import Dataset, open_dataset
from instacart.db import connect
from instacart.profiling import profile_dataset
from instacart.reports import write_json
from instacart.validation import validate_database

logger = logging.getLogger(__name__)
ADVISORY_LOCK = (1_140_201, 16_202_611)


def _record_discovery_failure(settings: Settings, report_path: Path, error: BaseException) -> None:
    run_id = uuid.uuid4()
    message = str(error)[:10_000]
    try:
        write_json(
            report_path,
            {
                "run_id": str(run_id),
                "status": "failed",
                "stage": "dataset_discovery",
                "error": message,
            },
        )
    except OSError:
        logger.exception("Не удалось записать отчёт об ошибке источника: %s", report_path)
    try:
        with connect(settings) as connection:
            connection.execute(
                "INSERT INTO ops.ingestion_runs "
                "(run_id, dataset_fingerprint, status, finished_at, "
                "validation_results, error_message) "
                "VALUES (%s, NULL, 'failed', clock_timestamp(), %s, %s)",
                (run_id, Jsonb({"stage": "dataset_discovery"}), message),
            )
    except Exception:
        logger.exception("Не удалось сохранить ошибку обнаружения датасета в ops.ingestion_runs")


@contextmanager
def _open_for_ingestion(
    dataset_path: Path, settings: Settings, report_path: Path
) -> Iterator[Dataset]:
    opened = False
    try:
        with open_dataset(dataset_path) as dataset:
            opened = True
            yield dataset
    except (Exception, KeyboardInterrupt) as exc:
        if not opened:
            _record_discovery_failure(settings, report_path, exc)
        raise


def _copy_csv_file(
    connection: psycopg.Connection,
    table: str,
    columns: str,
    path: Path,
) -> None:
    sql = f"COPY {table} ({columns}) FROM STDIN WITH (FORMAT CSV, HEADER TRUE, NULL '')"
    with path.open("rb") as source, connection.cursor().copy(sql) as copy:
        first = True
        while chunk := source.read(1024 * 1024):
            if first:
                chunk = chunk.removeprefix(b"\xef\xbb\xbf")
                first = False
            copy.write(chunk)


def _copy_departments(connection: psycopg.Connection, path: Path) -> None:
    with path.open("r", encoding="utf-8-sig", newline="") as source:
        rows = csv.reader(source)
        next(rows)
        with connection.cursor().copy(
            "COPY core.departments (department_id, department_name) FROM STDIN"
        ) as copy:
            for row in rows:
                copy.write_row((int(row[0]), row[1]))


def _read_aisle_departments(path: Path) -> dict[int, int]:
    mapping: dict[int, int] = {}
    with path.open("r", encoding="utf-8-sig", newline="") as source:
        rows = csv.reader(source)
        next(rows)
        for row in rows:
            aisle_id, department_id = int(row[2]), int(row[3])
            previous = mapping.setdefault(aisle_id, department_id)
            if previous != department_id:
                raise ValueError(f"aisle_id={aisle_id} связан с несколькими department_id")
    return mapping


def _copy_aisles(
    connection: psycopg.Connection, path: Path, department_by_aisle: dict[int, int]
) -> None:
    seen: set[int] = set()
    with path.open("r", encoding="utf-8-sig", newline="") as source:
        rows = csv.reader(source)
        next(rows)
        with connection.cursor().copy(
            "COPY core.aisles (aisle_id, aisle_name, department_id) FROM STDIN"
        ) as copy:
            for row in rows:
                aisle_id = int(row[0])
                department_id = department_by_aisle.get(aisle_id)
                if department_id is None:
                    raise ValueError(
                        f"Для aisle_id={aisle_id} не найден department_id в products.csv"
                    )
                seen.add(aisle_id)
                copy.write_row((aisle_id, row[1], department_id))
    missing = sorted(set(department_by_aisle) - seen)
    if missing:
        raise ValueError(f"products.csv ссылается на неизвестные aisle_id: {missing[:10]}")


def _copy_products(connection: psycopg.Connection, path: Path) -> None:
    with path.open("r", encoding="utf-8-sig", newline="") as source:
        rows = csv.reader(source)
        next(rows)
        with connection.cursor().copy(
            "COPY core.products (product_id, product_name, aisle_id) FROM STDIN"
        ) as copy:
            for row in rows:
                copy.write_row((int(row[0]), row[1], int(row[2])))


def _stage_and_normalize(connection: psycopg.Connection, dataset: Dataset) -> None:
    connection.execute(
        "CREATE TEMP TABLE stage_orders ("
        "order_id INTEGER NOT NULL, user_id INTEGER NOT NULL, eval_set TEXT NOT NULL, "
        "order_number SMALLINT NOT NULL, order_dow SMALLINT NOT NULL, "
        "order_hour_of_day SMALLINT NOT NULL, days_since_prior_order REAL) ON COMMIT DROP"
    )
    aisle_departments = _read_aisle_departments(dataset.files["products.csv"])
    _copy_departments(connection, dataset.files["departments.csv"])
    _copy_aisles(connection, dataset.files["aisles.csv"], aisle_departments)
    _copy_products(connection, dataset.files["products.csv"])
    _copy_csv_file(
        connection,
        "stage_orders",
        "order_id, user_id, eval_set, order_number, order_dow, order_hour_of_day, "
        "days_since_prior_order",
        dataset.files["orders.csv"],
    )
    connection.execute("INSERT INTO core.users (user_id) SELECT DISTINCT user_id FROM stage_orders")
    connection.execute(
        "INSERT INTO core.orders (order_id, user_id, eval_set, order_number, order_dow, "
        "order_hour_of_day, days_since_prior_order) "
        "SELECT order_id, user_id, eval_set, order_number, order_dow, order_hour_of_day, "
        "days_since_prior_order FROM stage_orders"
    )
    _drop_order_item_indexes(connection)
    for filename in ("order_products__prior.csv", "order_products__train.csv"):
        _copy_csv_file(
            connection,
            "core.order_items",
            "order_id, product_id, add_to_cart_order, reordered",
            dataset.files[filename],
        )
    _rebuild_order_item_indexes(connection)


def _drop_order_item_indexes(connection: psycopg.Connection) -> None:
    """Load the largest fact table as a heap, then build its indexes in bulk."""
    for name in (
        "pk_order_items",
        "uq_order_items_cart_position",
        "order_items_order_id_fkey",
        "order_items_product_id_fkey",
        "fk_order_items_order",
        "fk_order_items_product",
    ):
        connection.execute(f"ALTER TABLE core.order_items DROP CONSTRAINT IF EXISTS {name}")
    connection.execute("DROP INDEX IF EXISTS core.idx_order_items_product_id")


def _rebuild_order_item_indexes(connection: psycopg.Connection) -> None:
    connection.execute(
        "ALTER TABLE core.order_items ADD CONSTRAINT pk_order_items "
        "PRIMARY KEY (order_id, product_id)"
    )
    connection.execute(
        "ALTER TABLE core.order_items ADD CONSTRAINT uq_order_items_cart_position "
        "UNIQUE (order_id, add_to_cart_order)"
    )
    connection.execute(
        "ALTER TABLE core.order_items ADD CONSTRAINT fk_order_items_order "
        "FOREIGN KEY (order_id) REFERENCES core.orders(order_id) NOT VALID"
    )
    connection.execute(
        "ALTER TABLE core.order_items ADD CONSTRAINT fk_order_items_product "
        "FOREIGN KEY (product_id) REFERENCES core.products(product_id) NOT VALID"
    )
    connection.execute("ALTER TABLE core.order_items VALIDATE CONSTRAINT fk_order_items_order")
    connection.execute("ALTER TABLE core.order_items VALIDATE CONSTRAINT fk_order_items_product")
    connection.execute("CREATE INDEX idx_order_items_product_id ON core.order_items (product_id)")


def _register_run(connection: psycopg.Connection, run_id: uuid.UUID, fingerprint: str) -> None:
    connection.execute(
        "INSERT INTO ops.ingestion_runs (run_id, dataset_fingerprint, status) "
        "VALUES (%s, %s, 'running')",
        (run_id, fingerprint),
    )


def _set_status(
    connection: psycopg.Connection,
    run_id: uuid.UUID,
    status: str,
    row_counts: dict[str, Any],
    validation_results: dict[str, Any],
    error: str | None = None,
) -> None:
    connection.execute(
        "UPDATE ops.ingestion_runs SET status = %s, finished_at = clock_timestamp(), "
        "row_counts = %s, validation_results = %s, error_message = %s WHERE run_id = %s",
        (status, Jsonb(row_counts), Jsonb(validation_results), error, run_id),
    )


def _set_active_dataset(
    connection: psycopg.Connection, run_id: uuid.UUID, fingerprint: str
) -> None:
    connection.execute(
        "INSERT INTO ops.active_dataset (singleton, dataset_fingerprint, run_id) "
        "VALUES (TRUE, %s, %s) ON CONFLICT (singleton) DO UPDATE SET "
        "dataset_fingerprint = EXCLUDED.dataset_fingerprint, run_id = EXCLUDED.run_id, "
        "activated_at = clock_timestamp()",
        (fingerprint, run_id),
    )


def ingest(
    dataset_path: Path,
    settings: Settings,
    force: bool = False,
    report_path: Path | None = None,
) -> dict[str, Any]:
    """Import one complete dataset atomically; a failed rebuild preserves old core data."""
    report_path = report_path or settings.profile_report
    started = time.monotonic()
    with _open_for_ingestion(dataset_path, settings, report_path) as dataset:
        lock_connection = connect(settings)
        lock_connection.autocommit = True
        run_id = uuid.uuid4()
        report: dict[str, Any] = {
            "run_id": str(run_id),
            "dataset_fingerprint": dataset.fingerprint,
            "source": dataset.source,
            "file_sha256": dataset.file_sha256,
        }
        succeeded = False
        try:
            lock_connection.execute("SELECT pg_advisory_lock(%s, %s)", ADVISORY_LOCK)
            lock_connection.execute(
                "UPDATE ops.ingestion_runs SET status = 'failed', finished_at = clock_timestamp(), "
                "error_message = 'Предыдущий процесс завершился до фиксации результата' "
                "WHERE status = 'running'"
            )
            active = lock_connection.execute(
                "SELECT a.run_id, a.dataset_fingerprint, r.row_counts, r.validation_results "
                "FROM ops.active_dataset a JOIN ops.ingestion_runs r ON r.run_id = a.run_id "
                "WHERE a.singleton = TRUE AND r.status = 'succeeded'"
            ).fetchone()
            _register_run(lock_connection, run_id, dataset.fingerprint)
            logger.info("Проверка исходных CSV: %s", dataset.fingerprint)
            profile = profile_dataset(dataset, settings)
            report["profile"] = profile
            if not profile["invariants"]["passed"]:
                failed = [
                    item["name"] for item in profile["invariants"]["checks"] if not item["passed"]
                ]
                raise ValueError(f"Нарушены контракты исходных данных: {', '.join(failed)}")

            if active and active[1] == dataset.fingerprint and not force:
                row_counts = active[2]
                old_validation = active[3]
                skipped = {
                    "skipped": True,
                    "reason": "этот fingerprint уже активен в core",
                    "active_run_id": str(active[0]),
                    "invariants": profile["invariants"],
                    "reference_counts": profile["reference_counts"],
                }
                _set_status(lock_connection, run_id, "succeeded", row_counts, skipped)
                report.update(
                    {
                        "status": "succeeded",
                        "idempotent_skip": True,
                        "previous_validation": old_validation,
                        "database_validation": skipped,
                    }
                )
                succeeded = True
                logger.info("Fingerprint уже загружен; повторная запись пропущена")
            else:
                logger.info(
                    "Потоковая загрузка в PostgreSQL начата%s", " (пересборка)" if force else ""
                )
                load_started = time.monotonic()
                with connect(settings) as connection:
                    with connection.transaction():
                        connection.execute(
                            "TRUNCATE core.order_items, core.orders, core.users, core.products, "
                            "core.aisles, core.departments"
                        )
                        loading_started = time.monotonic()
                        _stage_and_normalize(connection, dataset)
                        loading_seconds = time.monotonic() - loading_started
                        row_counts = {
                            "users": int(
                                connection.execute("SELECT count(*) FROM core.users").fetchone()[0]
                            ),
                            "departments": int(
                                connection.execute(
                                    "SELECT count(*) FROM core.departments"
                                ).fetchone()[0]
                            ),
                            "aisles": int(
                                connection.execute("SELECT count(*) FROM core.aisles").fetchone()[0]
                            ),
                            "products": int(
                                connection.execute("SELECT count(*) FROM core.products").fetchone()[
                                    0
                                ]
                            ),
                            "orders": int(
                                connection.execute("SELECT count(*) FROM core.orders").fetchone()[0]
                            ),
                            "order_items": int(
                                connection.execute(
                                    "SELECT count(*) FROM core.order_items"
                                ).fetchone()[0]
                            ),
                        }
                        validation_started = time.monotonic()
                        database_report = validate_database(connection, profile["derived_counts"])
                        database_report["validation_seconds"] = round(
                            time.monotonic() - validation_started, 3
                        )
                        if not database_report["passed"]:
                            failed = [
                                item["name"]
                                for item in database_report["checks"]
                                if not item["passed"]
                            ]
                            raise ValueError(
                                f"Проверки PostgreSQL не пройдены: {', '.join(failed)}"
                            )
                        for table in (
                            "users",
                            "departments",
                            "aisles",
                            "products",
                            "orders",
                            "order_items",
                        ):
                            connection.execute(f"ANALYZE core.{table}")
                        database_report["loading_seconds"] = round(loading_seconds, 3)
                        database_report["analyze_seconds"] = round(
                            time.monotonic()
                            - validation_started
                            - database_report["validation_seconds"],
                            3,
                        )
                        database_report["duration_seconds"] = round(
                            time.monotonic() - load_started, 3
                        )
                        _set_status(
                            connection,
                            run_id,
                            "succeeded",
                            row_counts,
                            database_report,
                        )
                        _set_active_dataset(connection, run_id, dataset.fingerprint)
                report.update(
                    {
                        "status": "succeeded",
                        "idempotent_skip": False,
                        "row_counts": row_counts,
                        "database_validation": database_report,
                    }
                )
                succeeded = True
                logger.info(
                    "Загрузка завершена за %.1f с",
                    report["database_validation"]["duration_seconds"],
                )
            report["duration_seconds"] = round(time.monotonic() - started, 3)
            write_json(report_path, report)
            logger.info("JSON-отчёт сохранён: %s", report_path)
            return report
        except (Exception, KeyboardInterrupt) as exc:
            if not succeeded:
                try:
                    _set_status(
                        lock_connection,
                        run_id,
                        "failed",
                        report.get("row_counts", {}),
                        report.get("database_validation", report.get("profile", {})),
                        str(exc)[:10_000],
                    )
                except Exception:
                    logger.exception("Не удалось сохранить статус ошибки загрузки")
            report.update(
                {
                    "status": "failed",
                    "error": str(exc),
                    "duration_seconds": round(time.monotonic() - started, 3),
                }
            )
            try:
                write_json(report_path, report)
            except OSError:
                logger.exception("Не удалось записать отчёт об ошибке: %s", report_path)
            raise
        finally:
            lock_connection.close()
