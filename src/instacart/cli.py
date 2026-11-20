"""Command-line entry point."""

from __future__ import annotations

import argparse
import logging
import sys
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from instacart.analytics.service import (
    AnalyticsDataUnavailable,
    analyze,
    build_marts,
    validate_analytics,
)
from instacart.config import Settings
from instacart.dataset import open_dataset
from instacart.db import connect
from instacart.ingestion import ingest
from instacart.profiling import profile_dataset
from instacart.reports import write_json, write_markdown
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
    marts = commands.add_parser("marts", help="обновить SQL-аналитические витрины")
    marts.add_argument("--json-report", type=Path, default=settings.analytics_report)
    marts.add_argument("--markdown-report", type=Path, default=settings.analytics_markdown_report)
    analysis = commands.add_parser(
        "analyze", help="проверить гипотезы и сегментировать покупателей"
    )
    analysis.add_argument("--json-report", type=Path, default=settings.analytics_report)
    analysis.add_argument(
        "--markdown-report", type=Path, default=settings.analytics_markdown_report
    )
    analysis.add_argument("--random-seed", type=int, default=2026)
    analysis.add_argument("--bootstrap-replicates", type=int, default=200)
    analysis.add_argument("--silhouette-sample-size", type=int, default=5_000)
    analytics_validation = commands.add_parser(
        "validate-analytics", help="проверить корректность и актуальность аналитики"
    )
    analytics_validation.add_argument(
        "--report", type=Path, default=Path("reports/analytics-validation.json")
    )
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
        if args.command in ("marts", "analyze"):
            command_started = time.monotonic()
            try:
                if args.command == "marts":
                    result = build_marts(settings)
                else:
                    result = analyze(
                        settings,
                        random_seed=args.random_seed,
                        bootstrap_replicates=args.bootstrap_replicates,
                        silhouette_sample_size=args.silhouette_sample_size,
                    )
                payload = {"status": "succeeded", **result}
                payload["generated_at"] = datetime.now(UTC).isoformat()
                write_json(args.json_report, payload)
                write_markdown(args.markdown_report, payload)
                logging.getLogger(__name__).info(
                    "Отчёт аналитики: %s; Markdown: %s; длительность %.1f с",
                    args.json_report,
                    args.markdown_report,
                    time.monotonic() - command_started,
                )
                return 0
            except AnalyticsDataUnavailable as exc:
                payload = {
                    "status": "stale_or_missing",
                    "error": str(exc),
                    "duration_seconds": round(time.monotonic() - command_started, 3),
                    "generated_at": datetime.now(UTC).isoformat(),
                }
                write_json(args.json_report, payload)
                write_markdown(args.markdown_report, payload)
                logging.getLogger(__name__).error("%s", exc)
                return 2
            except Exception as exc:
                payload = {
                    "status": "failed",
                    "error": str(exc),
                    "duration_seconds": round(time.monotonic() - command_started, 3),
                    "generated_at": datetime.now(UTC).isoformat(),
                }
                write_json(args.json_report, payload)
                write_markdown(args.markdown_report, payload)
                logging.getLogger(__name__).exception("Аналитическая команда завершилась ошибкой")
                return 1
        if args.command == "validate-analytics":
            report = validate_analytics(settings)
            report["validated_at"] = datetime.now(UTC).isoformat()
            write_json(args.report, report)
            logging.getLogger(__name__).info(
                "Проверка аналитики: %s; витрин: %s; размер: %s байт",
                report["status"],
                report.get("row_counts", {}),
                report.get("mart_size_bytes", 0),
            )
            return 0 if report["passed"] else 1
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
