"""Environment-backed application configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True, slots=True)
class Settings:
    dataset_path: Path
    database_url: str
    duckdb_memory_limit: str
    duckdb_threads: int
    duckdb_temp_directory: Path
    profile_report: Path
    validation_report: Path
    analytics_report: Path = Path("reports/analytics.json")
    analytics_markdown_report: Path = Path("reports/analytics.md")

    @classmethod
    def from_env(cls) -> Settings:
        load_dotenv()
        return cls(
            dataset_path=Path(os.getenv("DATASET_PATH", "data/raw")),
            database_url=os.getenv(
                "DATABASE_URL",
                "postgresql://instacart:instacart_local@localhost:5432/instacart",
            ),
            duckdb_memory_limit=os.getenv("DUCKDB_MEMORY_LIMIT", "1GB"),
            duckdb_threads=max(1, int(os.getenv("DUCKDB_THREADS", "2"))),
            duckdb_temp_directory=Path(os.getenv("DUCKDB_TEMP_DIRECTORY", "data/tmp/duckdb")),
            profile_report=Path(os.getenv("PROFILE_REPORT", "reports/profile.json")),
            validation_report=Path(os.getenv("VALIDATION_REPORT", "reports/validation.json")),
            analytics_report=Path(os.getenv("ANALYTICS_REPORT", "reports/analytics.json")),
            analytics_markdown_report=Path(
                os.getenv("ANALYTICS_MARKDOWN_REPORT", "reports/analytics.md")
            ),
        )
