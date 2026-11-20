"""Dataset discovery, contract checks, content hashes, and source-independent IDs."""

from __future__ import annotations

import csv
import hashlib
import json
import shutil
import tempfile
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

EXPECTED_HEADERS: dict[str, tuple[str, ...]] = {
    "orders.csv": (
        "order_id",
        "user_id",
        "eval_set",
        "order_number",
        "order_dow",
        "order_hour_of_day",
        "days_since_prior_order",
    ),
    "products.csv": ("product_id", "product_name", "aisle_id", "department_id"),
    "aisles.csv": ("aisle_id", "aisle"),
    "departments.csv": ("department_id", "department"),
    "order_products__prior.csv": (
        "order_id",
        "product_id",
        "add_to_cart_order",
        "reordered",
    ),
    "order_products__train.csv": (
        "order_id",
        "product_id",
        "add_to_cart_order",
        "reordered",
    ),
}


class DatasetError(ValueError):
    """Raised when the source data does not match the supported contract."""


@dataclass(frozen=True, slots=True)
class Dataset:
    root: Path
    files: dict[str, Path]
    file_sha256: dict[str, str]
    fingerprint: str
    source: str


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _find_files(root: Path) -> dict[str, Path]:
    found: dict[str, list[Path]] = {name: [] for name in EXPECTED_HEADERS}
    for path in root.rglob("*.csv"):
        if path.name in found:
            found[path.name].append(path)
    duplicates = [name for name, matches in found.items() if len(matches) > 1]
    if duplicates:
        raise DatasetError(f"Несколько файлов с одинаковым именем: {', '.join(sorted(duplicates))}")
    missing = [name for name, matches in found.items() if not matches]
    if missing:
        raise DatasetError(f"Не найдены обязательные CSV: {', '.join(missing)}")
    return {name: matches[0] for name, matches in found.items()}


def _check_headers(files: dict[str, Path]) -> None:
    for name, expected in EXPECTED_HEADERS.items():
        try:
            with files[name].open("r", encoding="utf-8-sig", newline="") as stream:
                actual = tuple(next(csv.reader(stream)))
        except (OSError, UnicodeError, StopIteration, csv.Error) as exc:
            raise DatasetError(f"Не удалось прочитать заголовок {name}: {exc}") from exc
        if actual != expected:
            raise DatasetError(f"Неверный заголовок {name}: ожидался {expected}, получен {actual}")


def _build(root: Path, source: str) -> Dataset:
    files = _find_files(root)
    _check_headers(files)
    file_hashes = {name: _sha256(files[name]) for name in EXPECTED_HEADERS}
    manifest = json.dumps(file_hashes, sort_keys=True, separators=(",", ":")).encode()
    fingerprint = "sha256:" + hashlib.sha256(manifest).hexdigest()
    return Dataset(
        root=root, files=files, file_sha256=file_hashes, fingerprint=fingerprint, source=source
    )


@contextmanager
def open_dataset(path: Path) -> Iterator[Dataset]:
    """Open a directory or ZIP; ZIP payloads are extracted to a private temporary directory."""
    path = path.expanduser().resolve()
    if path.is_dir():
        yield _build(path, "directory")
        return
    if not path.is_file():
        raise DatasetError(f"Путь к датасету не существует: {path}")
    if not zipfile.is_zipfile(path):
        raise DatasetError(f"Файл не является корректным ZIP-архивом: {path}")

    with zipfile.ZipFile(path) as archive:
        corrupt = archive.testzip()
        if corrupt is not None:
            raise DatasetError(f"Ошибка CRC в ZIP-архиве, файл: {corrupt}")
        members: dict[str, zipfile.ZipInfo] = {}
        for info in archive.infolist():
            if Path(info.filename).name in EXPECTED_HEADERS and not info.is_dir():
                name = Path(info.filename).name
                if name in members:
                    raise DatasetError(f"В ZIP несколько файлов {name}")
                members[name] = info
        missing = [name for name in EXPECTED_HEADERS if name not in members]
        if missing:
            raise DatasetError(f"В ZIP не найдены обязательные CSV: {', '.join(missing)}")

        with tempfile.TemporaryDirectory(prefix="instacart-dataset-") as temporary:
            root = Path(temporary)
            for name, info in members.items():
                destination = root / name
                with archive.open(info) as source, destination.open("wb") as target:
                    shutil.copyfileobj(source, target, length=1024 * 1024)
            yield _build(root, "zip")
