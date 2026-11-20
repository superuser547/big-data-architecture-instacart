from __future__ import annotations

import json
import os
from pathlib import Path

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from conftest import create_dataset

import instacart.ingestion as ingestion
from instacart.config import Settings


@pytest.mark.integration
def test_migration_import_idempotency_and_transactional_rollback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dsn = os.getenv("INTEGRATION_DATABASE_URL")
    if not dsn:
        pytest.skip("INTEGRATION_DATABASE_URL не задан")

    monkeypatch.setenv("DATABASE_URL", dsn)
    config = Config("alembic.ini")
    command.upgrade(config, "head")
    with psycopg.connect(dsn) as connection:
        connection.execute(
            "TRUNCATE core.order_items, core.orders, core.users, core.products, core.aisles, "
            "core.departments"
        )
        connection.execute("DELETE FROM ops.ingestion_runs")

    first_dataset = create_dataset(tmp_path / "first")
    alternate_dataset = create_dataset(tmp_path / "alternate", product_name="Peas, frozen")
    settings = Settings(
        dataset_path=first_dataset,
        database_url=dsn,
        duckdb_memory_limit="256MB",
        duckdb_threads=1,
        duckdb_temp_directory=tmp_path / "duckdb-tmp",
        profile_report=tmp_path / "ingestion.json",
        validation_report=tmp_path / "validation.json",
    )

    broken_source = tmp_path / "missing-source"
    broken_source.mkdir()
    with pytest.raises(ValueError, match="Не найдены обязательные CSV"):
        ingestion.ingest(broken_source, settings, report_path=tmp_path / "discovery-error.json")
    with psycopg.connect(dsn) as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM ops.ingestion_runs "
                "WHERE status = 'failed' AND dataset_fingerprint IS NULL"
            ).fetchone()[0]
            == 1
        )
    discovery_report = json.loads((tmp_path / "discovery-error.json").read_text(encoding="utf-8"))
    assert discovery_report["stage"] == "dataset_discovery"

    first = ingestion.ingest(first_dataset, settings, report_path=tmp_path / "first.json")
    assert first["database_validation"]["passed"] is True
    assert first["row_counts"] == {
        "users": 2,
        "departments": 2,
        "aisles": 2,
        "products": 2,
        "orders": 5,
        "order_items": 5,
    }
    first_json = json.loads((tmp_path / "first.json").read_text(encoding="utf-8"))
    assert first_json["status"] == "succeeded"
    assert first_json["database_validation"]["counts"]["test_items"] == 0
    repeated = ingestion.ingest(first_dataset, settings, report_path=tmp_path / "repeat.json")
    assert repeated["idempotent_skip"] is True
    with psycopg.connect(dsn) as connection:
        assert connection.execute("SELECT count(*) FROM core.order_items").fetchone()[0] == 5

    original_stage = ingestion._stage_and_normalize

    def fail_after_loading(connection: psycopg.Connection, dataset) -> None:
        original_stage(connection, dataset)
        raise RuntimeError("injected rebuild failure")

    monkeypatch.setattr(ingestion, "_stage_and_normalize", fail_after_loading)
    with pytest.raises(RuntimeError, match="injected rebuild failure"):
        ingestion.ingest(
            alternate_dataset,
            settings,
            force=True,
            report_path=tmp_path / "failed.json",
        )
    with psycopg.connect(dsn) as connection:
        assert connection.execute("SELECT count(*) FROM core.order_items").fetchone()[0] == 5
        assert (
            connection.execute(
                "SELECT product_name FROM core.products WHERE product_id = 10"
            ).fetchone()[0]
            == "Frozen peas"
        )
        assert (
            connection.execute(
                "SELECT count(*) FROM ops.ingestion_runs WHERE status = 'failed' "
                "AND error_message LIKE '%injected rebuild failure%'"
            ).fetchone()[0]
            == 1
        )

    monkeypatch.setattr(ingestion, "_stage_and_normalize", original_stage)
    rebuilt = ingestion.ingest(
        alternate_dataset,
        settings,
        force=True,
        report_path=tmp_path / "rebuilt.json",
    )
    assert rebuilt["idempotent_skip"] is False
    with psycopg.connect(dsn) as connection:
        assert (
            connection.execute(
                "SELECT product_name FROM core.products WHERE product_id = 10"
            ).fetchone()[0]
            == "Peas, frozen"
        )
        assert connection.execute("SELECT count(*) FROM core.order_items").fetchone()[0] == 5
