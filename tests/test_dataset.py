from __future__ import annotations

import zipfile
from pathlib import Path

import pytest
from conftest import HEADERS

from instacart.dataset import DatasetError, open_dataset


def test_directory_and_zip_share_fingerprint(dataset_dir: Path, tmp_path: Path) -> None:
    archive = tmp_path / "source.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as output:
        for name in HEADERS:
            output.write(dataset_dir / name, arcname=f"instacart/{name}")
    with open_dataset(dataset_dir) as directory, open_dataset(archive) as zipped:
        assert directory.fingerprint == zipped.fingerprint
        assert directory.file_sha256 == zipped.file_sha256
        assert zipped.source == "zip"


def test_directory_requires_all_files(dataset_dir: Path) -> None:
    (dataset_dir / "aisles.csv").unlink()
    with pytest.raises(DatasetError, match="aisles.csv"):
        with open_dataset(dataset_dir):
            pass


def test_header_contract_is_exact(dataset_dir: Path) -> None:
    path = dataset_dir / "orders.csv"
    path.write_text("order_id,user_id,wrong\n", encoding="utf-8")
    with pytest.raises(DatasetError, match="Неверный заголовок"):
        with open_dataset(dataset_dir):
            pass


def test_zip_crc_is_checked(dataset_dir: Path, tmp_path: Path) -> None:
    archive = tmp_path / "source.zip"
    with zipfile.ZipFile(archive, "w") as output:
        for name in HEADERS:
            output.write(dataset_dir / name, arcname=name)
    damaged = bytearray(archive.read_bytes())
    damaged[-30] ^= 0xFF
    archive.write_bytes(damaged)
    with pytest.raises(DatasetError):
        with open_dataset(archive):
            pass
