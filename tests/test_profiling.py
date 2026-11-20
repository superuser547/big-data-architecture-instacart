from __future__ import annotations

from pathlib import Path

import pytest
from conftest import create_dataset

from instacart.config import Settings
from instacart.dataset import open_dataset
from instacart.profiling import profile_dataset


def settings(tmp_path: Path) -> Settings:
    return Settings(
        dataset_path=tmp_path,
        database_url="postgresql://unused",
        duckdb_memory_limit="256MB",
        duckdb_threads=1,
        duckdb_temp_directory=tmp_path / "duckdb-tmp",
        profile_report=tmp_path / "profile.json",
        validation_report=tmp_path / "validation.json",
    )


def test_profile_reports_counts_checks_and_reference_controls(tmp_path: Path) -> None:
    path = create_dataset(tmp_path / "source")
    with open_dataset(path) as dataset:
        report = profile_dataset(dataset, settings(tmp_path))
    assert report["invariants"]["passed"] is True
    assert report["derived_counts"]["users"] == 2
    assert report["derived_counts"]["order_items"] == 5
    assert report["reference_counts"]["applicable"] is False
    assert report["tables"]["orders.csv"]["nulls"]["days_since_prior_order"] == 2


def test_profile_detects_eval_set_mismatch(tmp_path: Path) -> None:
    path = create_dataset(tmp_path / "source")
    item_file = path / "order_products__prior.csv"
    item_file.write_text(
        item_file.read_text(encoding="utf-8").replace("100,10,1,0", "102,10,1,0"),
        encoding="utf-8",
    )
    with open_dataset(path) as dataset:
        report = profile_dataset(dataset, settings(tmp_path))
    failed = {item["name"] for item in report["invariants"]["checks"] if not item["passed"]}
    assert "order_products__prior.csv.eval_set_matches" in failed


@pytest.mark.parametrize(
    ("filename", "old", "new", "expected_check"),
    [
        ("orders.csv", "104,2,train,3,2,9,4", "100,2,train,3,2,9,4", "src_orders.key_unique"),
        ("orders.csv", "102,2,train,1,4,15,", "102,2,train,1,9,15,", "orders.values_in_range"),
        (
            "order_products__train.csv",
            "104,10,1,1",
            "104,999,1,1",
            "order_products__train.csv.product_exists",
        ),
    ],
)
def test_profile_reports_key_range_and_reference_violations(
    tmp_path: Path, filename: str, old: str, new: str, expected_check: str
) -> None:
    path = create_dataset(tmp_path / "source")
    file_path = path / filename
    file_path.write_text(file_path.read_text(encoding="utf-8").replace(old, new), encoding="utf-8")
    with open_dataset(path) as dataset:
        report = profile_dataset(dataset, settings(tmp_path))
    failed = {item["name"] for item in report["invariants"]["checks"] if not item["passed"]}
    assert expected_check in failed
