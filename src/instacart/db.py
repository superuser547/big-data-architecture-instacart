"""PostgreSQL connection helpers."""

from __future__ import annotations

import psycopg

from instacart.config import Settings


def connect(settings: Settings) -> psycopg.Connection:
    return psycopg.connect(settings.database_url)
