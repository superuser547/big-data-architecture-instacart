"""Command-line entry point."""

from __future__ import annotations

import argparse
import logging
import sys
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from instacart.config import Settings
from instacart.dataset import open_dataset
from instacart.db import connect
from instacart.ingestion import ingest
from instacart.profiling import profile_dataset
from instacart.reports import write_json
from instacart.validation import validate_database


def _parser(settings: Settings) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="instacart", description="Загрузка и проверка Instacart Analytics"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("profile", "ingest"):
        command = commands.add_parser(name, help=f"{name}: работа с исходным датасетом")
        command.add_argument("--dataset", type=Path, default=settings.dataset_path)
        command.add_argument("--report", type=Path, default=settings.profile_report)
        if name == "ingest":
            command.add_argument(
                "--force", action="store_true", help="пересобрать core в транзакции"
            )
    validate = commands.add_parser("validate", help="проверить загруженные таблицы PostgreSQL")
    validate.add_argument("--report", type=Path, default=settings.validation_report)
    return parser


def _configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def main(argv: Sequence[str] | None = None) -> int:
    _configure_logging()
    settings = Settings.from_env()
    args = _parser(settings).parse_args(argv)
    try:
        if args.command == "profile":
            started = time.monotonic()
            try:
                with open_dataset(args.dataset) as dataset:
                    report = profile_dataset(dataset, settings)
            except Exception as exc:
                write_json(args.report, {"status": "failed", "error": str(exc)})
                raise
            report["status"] = "succeeded" if report["invariants"]["passed"] else "failed"
            report["duration_seconds"] = round(time.monotonic() - started, 3)
            write_json(args.report, report)
            logging.getLogger(__name__).info("Отчёт профилирования: %s", args.report)
            return 0 if report["invariants"]["passed"] else 1
        if args.command == "ingest":
            report = ingest(args.dataset, settings, force=args.force, report_path=args.report)
            logging.getLogger(__name__).info(
                "Запуск %s завершён; отчёт: %s", report["run_id"], args.report
            )
            return 0
        with connect(settings) as connection:
            report = validate_database(connection)
        report["validated_at"] = datetime.now(UTC).isoformat()
        report["status"] = "succeeded" if report["passed"] else "failed"
        write_json(args.report, report)
        logging.getLogger(__name__).info("Отчёт проверки: %s", args.report)
        return 0 if report["passed"] else 1
    except Exception as exc:
        logging.getLogger(__name__).exception(
            "Команда %s завершилась ошибкой: %s", args.command, exc
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
