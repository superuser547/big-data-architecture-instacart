from __future__ import annotations

import csv
from pathlib import Path

import pytest

HEADERS = {
    "orders.csv": [
        "order_id",
        "user_id",
        "eval_set",
        "order_number",
        "order_dow",
        "order_hour_of_day",
        "days_since_prior_order",
    ],
    "products.csv": ["product_id", "product_name", "aisle_id", "department_id"],
    "aisles.csv": ["aisle_id", "aisle"],
    "departments.csv": ["department_id", "department"],
    "order_products__prior.csv": [
        "order_id",
        "product_id",
        "add_to_cart_order",
        "reordered",
    ],
    "order_products__train.csv": [
        "order_id",
        "product_id",
        "add_to_cart_order",
        "reordered",
    ],
}

ROWS = {
    "orders.csv": [
        [100, 1, "prior", 1, 2, 10, ""],
        [101, 1, "prior", 2, 3, 11, 7],
        [102, 2, "train", 1, 4, 15, ""],
        [103, 2, "test", 2, 1, 8, 5],
        [104, 2, "train", 3, 2, 9, 4],
    ],
    "products.csv": [[10, "Frozen peas", 1, 1], [20, "Spinach", 2, 2]],
    "aisles.csv": [[1, "frozen"], [2, "fresh vegetables"]],
    "departments.csv": [[1, "frozen"], [2, "produce"]],
    "order_products__prior.csv": [[100, 10, 1, 0], [101, 20, 1, 1]],
    "order_products__train.csv": [[102, 10, 1, 0], [104, 10, 1, 1], [104, 20, 2, 0]],
}


def create_dataset(root: Path, product_name: str = "Frozen peas") -> Path:
    root.mkdir(parents=True, exist_ok=True)
    for name, headers in HEADERS.items():
        rows = [row[:] for row in ROWS[name]]
        if name == "products.csv":
            rows[0][1] = product_name
        with (root / name).open("w", encoding="utf-8", newline="") as output:
            writer = csv.writer(output, lineterminator="\n")
            writer.writerow(headers)
            writer.writerows(rows)
    return root


@pytest.fixture
def dataset_dir(tmp_path: Path) -> Path:
    return create_dataset(tmp_path / "dataset")
