"""Atomic JSON report output."""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Iterable
from pathlib import Path
from typing import Any


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _table(headers: Iterable[str], rows: Iterable[Iterable[Any]]) -> str:
    header = list(headers)
    body = [list(row) for row in rows]
    lines = ["| " + " | ".join(header) + " |", "| " + " | ".join("---" for _ in header) + " |"]
    lines.extend("| " + " | ".join(str(value) for value in row) + " |" for row in body)
    return "\n".join(lines)


def _p_value(value: Any) -> str:
    if value is None:
        return "не оценивается"
    if value == 0:
        return "<1e-300"
    return f"{float(value):.4g}"


def analytics_markdown(payload: dict[str, Any]) -> str:
    """Render a compact, shareable Russian report from persisted result payloads."""
    lines = [
        "# Аналитический отчёт Instacart",
        "",
        f"- Статус: `{payload.get('status', 'unknown')}`",
        f"- Сформирован: {payload.get('generated_at', 'не указано')}",
        f"- Fingerprint набора: `{payload.get('dataset_fingerprint', 'не указан')}`",
        f"- Длительность: {payload.get('duration_seconds', 'не указана')} с",
    ]
    row_counts = payload.get("row_counts")
    if row_counts:
        lines.extend(
            ["", "## Строки витрин", "", _table(("Витрина", "Строки"), row_counts.items())]
        )

    hypotheses = payload.get("hypotheses")
    if hypotheses:
        lines.extend(["", "## Проверка гипотез", ""])
        h1 = hypotheses.get("H1", {})
        if h1:
            lines.extend(
                [
                    "### H1. День недели и час заказа",
                    "",
                    _table(
                        ("Заказы", "χ²", "p-value", "Cramér’s V", "95% CI V"),
                        [
                            (
                                h1.get("observations"),
                                h1.get("statistic"),
                                _p_value(h1.get("p_value")),
                                h1.get("cramers_v"),
                                h1.get("bootstrap", {}).get("cramers_v_ci"),
                            )
                        ],
                    ),
                    "",
                    h1.get("interpretation", ""),
                ]
            )
        h2 = hypotheses.get("H2", {})
        if h2:
            lines.extend(
                [
                    "",
                    "### H2. Категория и повторная покупка",
                    "",
                    _table(
                        ("Уровень", "Позиции", "χ²", "p-value", "Cramér’s V", "Bootstrap 95% CI"),
                        [
                            (
                                level,
                                values.get("observations"),
                                values.get("statistic"),
                                _p_value(values.get("p_value")),
                                values.get("cramers_v"),
                                h2.get("bootstrap", {}).get(level, {}).get("cramers_v_ci"),
                            )
                            for level, values in h2.get("tests", {}).items()
                        ],
                    ),
                    "",
                    "Детальные доли и интервалы по departments и aisles доступны "
                    "в PostgreSQL и JSON.",
                ]
            )
        h3 = hypotheses.get("H3", {})
        if h3:
            pair_rows = []
            for comparison, values in h3.get("pairwise_mcnemar", {}).items():
                pair_rows.append(
                    (
                        comparison,
                        values.get("orders"),
                        values.get("risk_difference"),
                        values.get("bootstrap_ci"),
                        _p_value(values.get("p_value")),
                        _p_value(values.get("p_adjusted")),
                        values.get("same_department_risk_difference"),
                        _p_value(values.get("same_department_p_adjusted")),
                    )
                )
            lines.extend(
                [
                    "",
                    "### H3. Позиция в корзине",
                    "",
                    f"Заказы размером ≥20: {h3.get('orders_with_basket_size_at_least_20')}; "
                    f"Cochran Q = {h3.get('cochran_q')}; "
                    f"p = {_p_value(h3.get('cochran_q_p_value'))}; "
                    f"размах долей = {h3.get('position_rate_range')}; "
                    f"bootstrap 95% CI = {h3.get('bootstrap', {}).get('position_rate_range_ci')}.",
                    "",
                    _table(
                        (
                            "Пара",
                            "Заказы",
                            "Δ долей (п.п.)",
                            "Bootstrap 95% CI",
                            "p McNemar",
                            "p Holm",
                            "Δ при одинаковом department",
                            "p Holm, одинаковый department",
                        ),
                        pair_rows,
                    ),
                ]
            )

    clustering = payload.get("clustering")
    if clustering:
        lines.extend(
            [
                "",
                "## Сегментация покупателей",
                "",
                f"Покупатели: {clustering.get('customer_count')}; выбрано k = "
                f"{clustering.get('selected_k')} ({clustering.get('selection_basis')}).",
                "",
                _table(
                    ("k", "Inertia", "Silhouette", "Размеры кластеров", "Выбрано"),
                    [
                        (
                            item.get("k"),
                            item.get("inertia"),
                            item.get("silhouette"),
                            item.get("cluster_sizes"),
                            item.get("selected", False),
                        )
                        for item in clustering.get("metrics", [])
                    ],
                ),
                "",
                _table(
                    ("Кластер", "Покупатели", "Средние признаки"),
                    [
                        (
                            profile.get("cluster_id"),
                            profile.get("customer_count"),
                            profile.get("feature_means"),
                        )
                        for profile in clustering.get("profiles", [])
                    ],
                ),
            ]
        )
    checks = payload.get("checks")
    if checks:
        lines.extend(
            [
                "",
                "## Проверки",
                "",
                _table(
                    ("Проверка", "Пройдена", "Детали"),
                    ((item.get("name"), item.get("passed"), item.get("detail")) for item in checks),
                ),
            ]
        )
    if payload.get("error"):
        lines.extend(["", "## Ошибка", "", str(payload["error"])])
    lines.append("")
    return "\n".join(lines)


def write_markdown(path: Path, payload: dict[str, Any]) -> None:
    """Atomically write an analytics Markdown report."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(analytics_markdown(payload))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
