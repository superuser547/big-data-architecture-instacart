"""Three reproducible association studies over PostgreSQL aggregates."""

from __future__ import annotations

import uuid
from collections.abc import Iterator, Sequence
from typing import Any

import numpy as np
import psycopg
from psycopg.types.json import Jsonb
from scipy.sparse import csr_matrix

from instacart.analytics.statistics import (
    chi_square_summary,
    holm_adjust,
    log10_p_value,
    mcnemar_from_counts,
    percentile_interval,
    wilson_interval,
)

ALPHA = 0.05


def _bootstrap_weights(
    customer_count: int, replicates: int, random_seed: int
) -> Iterator[np.ndarray]:
    """Yield one full-population customer bootstrap weight vector at a time."""
    if customer_count <= 0:
        return
    rng = np.random.default_rng(random_seed)
    for _ in range(replicates):
        yield np.bincount(
            rng.integers(0, customer_count, size=customer_count), minlength=customer_count
        )


def _cramers_v(table: np.ndarray) -> float | None:
    return chi_square_summary(table)["cramers_v"]


def _bootstrap_contingency(
    customer_matrix: csr_matrix,
    row_count: int,
    column_count: int,
    *,
    replicates: int,
    random_seed: int,
) -> dict[str, Any]:
    if customer_matrix.shape[0] == 0:
        return {"sampled_customers": 0, "replicates": 0, "cramers_v_ci": None}
    effects: list[float] = []
    category_rates = [[] for _ in range(row_count)] if column_count == 2 else None
    for sample_weights in _bootstrap_weights(customer_matrix.shape[0], replicates, random_seed):
        weighted_cells = np.asarray(customer_matrix.T @ sample_weights).reshape(-1)
        table = weighted_cells.reshape(row_count, column_count)
        summary = chi_square_summary(table)
        if summary["cramers_v"] is not None:
            effects.append(summary["cramers_v"])
        if category_rates is not None:
            totals = table.sum(axis=1)
            for row_index, total in enumerate(totals):
                if total:
                    category_rates[row_index].append(table[row_index, 1] / total)
    result = {
        "sampled_customers": int(customer_matrix.shape[0]),
        "replicates": replicates,
        "cramers_v_ci": percentile_interval(effects),
        "sampling_unit": "all customers; resampled with replacement",
        "random_seed": random_seed,
    }
    if category_rates is not None:
        result["category_rate_ci"] = [percentile_interval(values) for values in category_rates]
    return result


def _customer_csr_from_query(
    connection: psycopg.Connection,
    query: str,
    parameters: tuple[Any, ...],
    customer_count: int,
    column_count: int,
    transform_batch,
) -> csr_matrix:
    """Stream sorted customer-cell aggregates into CSR without retaining SQL rows."""
    indptr = np.zeros(customer_count + 1, dtype=np.int64)
    data_chunks: list[np.ndarray] = []
    index_chunks: list[np.ndarray] = []
    nonzero_count = 0
    current_row: int | None = None
    with connection.cursor(name=f"analytics_bootstrap_{uuid.uuid4().hex}") as cursor:
        cursor.execute(query, parameters)
        while batch := cursor.fetchmany(50_000):
            rows, columns, values = transform_batch(np.asarray(batch, dtype=np.int64))
            nonzero = values != 0
            rows = rows[nonzero]
            columns = columns[nonzero]
            values = values[nonzero]
            if not len(rows):
                continue
            data_chunks.append(values)
            index_chunks.append(columns)
            starts = np.r_[0, np.flatnonzero(rows[1:] != rows[:-1]) + 1]
            ends = np.r_[starts[1:], len(rows)]
            for start, end in zip(starts, ends, strict=True):
                row_index = int(rows[start])
                if row_index < 0 or row_index >= customer_count:
                    raise ValueError("customer index is outside the bootstrap population")
                if current_row is None:
                    indptr[: row_index + 1] = 0
                elif row_index != current_row:
                    if row_index < current_row:
                        raise ValueError("bootstrap query must be ordered by customer and cell")
                    indptr[current_row + 1 : row_index + 1] = nonzero_count
                if row_index != current_row:
                    indptr[row_index] = nonzero_count
                    current_row = row_index
                nonzero_count += int(end - start)
    if current_row is not None:
        indptr[current_row + 1 :] = nonzero_count
    data = np.concatenate(data_chunks) if data_chunks else np.empty(0, dtype=np.int64)
    indices = np.concatenate(index_chunks) if index_chunks else np.empty(0, dtype=np.int32)
    return csr_matrix((data, indices, indptr), shape=(customer_count, column_count), copy=False)


def _put_result(
    connection: psycopg.Connection,
    run_id: uuid.UUID,
    fingerprint: str,
    *,
    hypothesis_id: str,
    h0: str,
    h1: str,
    method: str,
    observations: int,
    statistic: float | None,
    p_value: float | None,
    effect_name: str | None,
    effect_value: float | None,
    ci: tuple[float, float] | None,
    metrics: dict[str, Any],
    interpretation: str,
) -> None:
    connection.execute(
        "INSERT INTO mart._hypothesis_results "
        "(run_id, dataset_fingerprint, hypothesis_id, h0, h1, method, observation_count, "
        "statistic, p_value, effect_name, effect_value, ci_lower, ci_upper, interpretation, "
        "metrics) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (
            run_id,
            fingerprint,
            hypothesis_id,
            h0,
            h1,
            method,
            observations,
            statistic,
            p_value,
            effect_name,
            effect_value,
            ci[0] if ci else None,
            ci[1] if ci else None,
            interpretation,
            Jsonb(metrics),
        ),
    )


def _interpret(p_value: float | None, effect_name: str, effect_value: float | None) -> str:
    if p_value is None:
        return "Результат не оценивается: после исключения пустых групп недостаточно категорий."
    decision = (
        "Есть статистические свидетельства различий"
        if p_value < ALPHA
        else "Статистических свидетельств различий при α = 0,05 недостаточно"
    )
    effect = f" {effect_name} = {effect_value:.4f}." if effect_value is not None else ""
    formatted_p = "< 1e-300 (ниже точности float)" if p_value == 0 else f"= {p_value:.4g}"
    return (
        f"{decision}; p {formatted_p}.{effect} Связь не является доказательством причинности, "
        "а статистическая значимость сама по себе не означает сильный эффект."
    )


def _create_customer_population(connection: psycopg.Connection) -> int:
    connection.execute(
        "CREATE TEMP TABLE tmp_analytics_users ON COMMIT DROP AS "
        "SELECT user_id, (row_number() OVER (ORDER BY user_id) - 1)::INTEGER AS customer_index "
        "FROM core.users"
    )
    connection.execute("CREATE UNIQUE INDEX ON tmp_analytics_users (user_id)")
    connection.execute("ANALYZE tmp_analytics_users")
    return int(connection.execute("SELECT count(*) FROM tmp_analytics_users").fetchone()[0])


def run_hypotheses(
    connection: psycopg.Connection,
    run_id: uuid.UUID,
    fingerprint: str,
    *,
    random_seed: int = 2026,
    bootstrap_replicates: int = 200,
) -> dict[str, Any]:
    """Calculate full-data tests and all-customer cluster-bootstrap intervals."""
    if bootstrap_replicates < 1:
        raise ValueError("bootstrap_replicates must be positive")
    for table in ("_hypothesis_results", "_hypothesis_categories", "_hypothesis_pairs"):
        connection.execute(f"DELETE FROM mart.{table}")
    customer_count = _create_customer_population(connection)
    h1 = _h1(connection, run_id, fingerprint, customer_count, bootstrap_replicates, random_seed)
    h2 = _h2(connection, run_id, fingerprint, customer_count, bootstrap_replicates, random_seed)
    h3 = _h3(
        connection,
        run_id,
        fingerprint,
        customer_count,
        bootstrap_replicates,
        random_seed,
    )
    return {
        "alpha": ALPHA,
        "customer_bootstrap": {
            "random_seed": random_seed,
            "replicates": bootstrap_replicates,
            "sampled_customers": customer_count,
            "population": "all core.users",
            "method": "resample customers with replacement; one replicate streamed at a time",
        },
        "H1": h1,
        "H2": h2,
        "H3": h3,
    }


def _h1(
    connection: psycopg.Connection,
    run_id: uuid.UUID,
    fingerprint: str,
    customer_count: int,
    replicates: int,
    random_seed: int,
) -> dict[str, Any]:
    counts = np.zeros((7, 24), dtype=np.int64)
    for dow, hour, count in connection.execute(
        "SELECT order_dow, order_hour_of_day, count(*) FROM core.orders "
        "GROUP BY order_dow, order_hour_of_day"
    ).fetchall():
        counts[int(dow), int(hour)] = int(count)
    summary = chi_square_summary(counts)

    per_customer = _customer_csr_from_query(
        connection,
        "SELECT s.customer_index, o.order_dow, o.order_hour_of_day, count(*)::BIGINT "
        "FROM tmp_analytics_users s JOIN core.orders o USING (user_id) "
        "GROUP BY s.customer_index, o.order_dow, o.order_hour_of_day "
        "ORDER BY s.customer_index, o.order_dow, o.order_hour_of_day",
        (),
        customer_count,
        7 * 24,
        lambda batch: (
            batch[:, 0].astype(np.int32),
            (batch[:, 1] * 24 + batch[:, 2]).astype(np.int32),
            batch[:, 3],
        ),
    )
    bootstrap = _bootstrap_contingency(
        per_customer,
        7,
        24,
        replicates=replicates,
        random_seed=random_seed + 1,
    )
    metrics = {
        **summary,
        "p_value_method": "Pearson chi-square on all orders; assumes independent orders",
        "effect_estimator": "Bergsma bias-corrected Cramér's V",
        "day_labels": [f"order_dow={day}" for day in range(7)],
        "hour_labels": list(range(24)),
        "bootstrap": bootstrap,
    }
    _put_result(
        connection,
        run_id,
        fingerprint,
        hypothesis_id="H1",
        h0="Распределение заказов по часам одинаково для всех значений order_dow.",
        h1="Распределение заказов по часам зависит от order_dow.",
        method=(
            "Критерий Пирсона χ² на всех заказах; эффект — V с конечновыборочной поправкой, "
            "интервал — кластерный bootstrap по всем покупателям."
        ),
        observations=summary["observations"],
        statistic=summary["statistic"],
        p_value=summary["p_value"],
        effect_name="Bias-corrected Cramér’s V",
        effect_value=summary["cramers_v"],
        ci=bootstrap["cramers_v_ci"],
        metrics=metrics,
        interpretation=_interpret(
            summary["p_value"], "bias-corrected Cramér’s V", summary["cramers_v"]
        ),
    )
    return metrics


def _category_customer_matrix(
    connection: psycopg.Connection,
    level: str,
    category_ids: Sequence[int],
    customer_count: int,
) -> csr_matrix:
    if level == "department":
        category_sql = "d.department_id"
    else:
        category_sql = "a.aisle_id"
    categories = np.asarray(category_ids, dtype=np.int64)

    def expand(batch: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        customer_indices = np.repeat(batch[:, 0].astype(np.int32), 2)
        category_indices = np.searchsorted(categories, batch[:, 1]).astype(np.int32)
        columns = np.column_stack((category_indices * 2, category_indices * 2 + 1)).reshape(-1)
        values = np.column_stack((batch[:, 2] - batch[:, 3], batch[:, 3])).reshape(-1)
        return customer_indices, columns, values

    return _customer_csr_from_query(
        connection,
        f"""
        SELECT s.customer_index, {category_sql} AS category_id,
               count(*)::BIGINT AS item_count,
               sum(i.reordered::INTEGER)::BIGINT AS repeat_count
        FROM tmp_analytics_users s
        JOIN core.orders o ON o.user_id = s.user_id AND o.eval_set IN ('prior', 'train')
        JOIN core.order_items i USING (order_id)
        JOIN core.products p USING (product_id)
        JOIN core.aisles a USING (aisle_id)
        JOIN core.departments d USING (department_id)
        GROUP BY s.customer_index, {category_sql}
        ORDER BY s.customer_index, category_id
        """,
        (),
        customer_count,
        len(category_ids) * 2,
        expand,
    )


def _h2(
    connection: psycopg.Connection,
    run_id: uuid.UUID,
    fingerprint: str,
    customer_count: int,
    replicates: int,
    random_seed: int,
) -> dict[str, Any]:
    rows = connection.execute(
        "SELECT group_type, group_id, group_name, department_id, department_name, "
        "sum(item_count)::BIGINT, sum(reorder_count)::BIGINT "
        "FROM mart.department_reorders WHERE eval_set IN ('prior', 'train') "
        "GROUP BY group_type, group_id, group_name, department_id, department_name "
        "ORDER BY group_type, group_id"
    ).fetchall()
    category_data: dict[str, list[dict[str, Any]]] = {"department": [], "aisle": []}
    for (
        group_type,
        group_id,
        group_name,
        department_id,
        department_name,
        item_count,
        repeat_count,
    ) in rows:
        entry = {
            "category_id": int(group_id),
            "category_name": group_name,
            "department_id": int(department_id),
            "department_name": department_name,
            "item_count": int(item_count),
            "reorder_count": int(repeat_count),
            "reorder_rate": float(repeat_count / item_count) if item_count else 0.0,
            "confidence_interval": None,
            "confidence_interval_method": ("all-customer cluster bootstrap percentile"),
        }
        category_data[group_type].append(entry)
        connection.execute(
            "INSERT INTO mart._hypothesis_categories "
            "(run_id, dataset_fingerprint, hypothesis_id, category_level, category_id, "
            "category_name, eval_set, item_count, reorder_count, reorder_rate, ci_lower, ci_upper) "
            "VALUES (%s, %s, %s, %s, %s, %s, 'prior+train', %s, %s, %s, %s, %s)",
            (
                run_id,
                fingerprint,
                "H2_department" if group_type == "department" else "H2_aisle",
                group_type,
                int(group_id),
                group_name,
                int(item_count),
                int(repeat_count),
                float(repeat_count / item_count) if item_count else 0.0,
                None,
                None,
            ),
        )

    bootstrap_details: dict[str, Any] = {}
    test_summaries: dict[str, dict[str, Any]] = {}
    for level in ("department", "aisle"):
        categories = category_data[level]
        category_ids = [item["category_id"] for item in categories]
        table = np.asarray(
            [
                [item["item_count"] - item["reorder_count"], item["reorder_count"]]
                for item in categories
            ],
            dtype=np.int64,
        ).reshape((-1, 2))
        test = chi_square_summary(table)
        user_matrix = _category_customer_matrix(connection, level, category_ids, customer_count)
        bootstrap = _bootstrap_contingency(
            user_matrix,
            len(category_ids),
            2,
            replicates=replicates,
            random_seed=random_seed + (2 if level == "department" else 3),
        )
        category_rate_intervals = bootstrap.get("category_rate_ci", [])
        for index, category in enumerate(categories):
            interval = (
                category_rate_intervals[index] if index < len(category_rate_intervals) else None
            )
            category["confidence_interval"] = interval
            connection.execute(
                "UPDATE mart._hypothesis_categories SET ci_lower = %s, ci_upper = %s, metrics = %s "
                "WHERE run_id = %s AND hypothesis_id = %s AND category_level = %s "
                "AND category_id = %s AND eval_set = 'prior+train'",
                (
                    interval[0] if interval else None,
                    interval[1] if interval else None,
                    Jsonb(
                        {
                            "interval_method": "all-customer cluster bootstrap percentile"
                            if interval
                            else "not estimable",
                            "bootstrap_customers": customer_count,
                            "bootstrap_replicates": replicates,
                        }
                    ),
                    run_id,
                    "H2_aisle" if level == "aisle" else "H2_department",
                    level,
                    category["category_id"],
                ),
            )
        bootstrap_details[level] = bootstrap
        test_summaries[level] = test
        hypothesis_id = "H2_department" if level == "department" else "H2_aisle"
        metrics = {
            **test,
            "p_value_method": (
                "Pearson chi-square on all prior/train positions; assumes independent positions"
            ),
            "effect_estimator": "Bergsma bias-corrected Cramér's V",
            "category_level": level,
            "categories": categories,
            "bootstrap": bootstrap,
            "eval_sets": ["prior", "train"],
        }
        _put_result(
            connection,
            run_id,
            fingerprint,
            hypothesis_id=hypothesis_id,
            h0=f"Доля повторных покупок одинакова для всех {level}.",
            h1=f"Доля повторных покупок различается между {level}.",
            method=(
                f"Критерий Пирсона χ² на всех prior/train позициях по {level}; "
                "эффект — V с конечновыборочной поправкой; CI — bootstrap по покупателям."
            ),
            observations=test["observations"],
            statistic=test["statistic"],
            p_value=test["p_value"],
            effect_name="Bias-corrected Cramér’s V",
            effect_value=test["cramers_v"],
            ci=bootstrap["cramers_v_ci"],
            metrics=metrics,
            interpretation=_interpret(
                test["p_value"], "bias-corrected Cramér’s V", test["cramers_v"]
            ),
        )
    p_values = [test_summaries[level]["p_value"] for level in ("aisle", "department")]
    adjusted = _holm_adjust_optional(p_values)
    for level, adjusted_p in zip(("aisle", "department"), adjusted, strict=True):
        connection.execute(
            "UPDATE mart._hypothesis_results SET metrics = metrics || %s "
            "WHERE run_id = %s AND hypothesis_id = %s",
            (
                Jsonb({"holm_adjusted_p_value_across_category_levels": adjusted_p}),
                run_id,
                "H2_aisle" if level == "aisle" else "H2_department",
            ),
        )
    return {
        "primary_test": "aisle",
        "tests": {"aisle": test_summaries["aisle"], "department": test_summaries["department"]},
        "bootstrap": bootstrap_details,
        "category_count": {level: len(category_data[level]) for level in ("aisle", "department")},
        "holm_adjusted_p_values": {"aisle": adjusted[0], "department": adjusted[1]},
        "eval_sets": ["prior", "train"],
    }


def _build_h3_orders(connection: psycopg.Connection) -> None:
    connection.execute(
        """
        CREATE TEMP TABLE tmp_h3_orders ON COMMIT DROP AS
        SELECT om.user_id, om.order_id,
               max(i.reordered::INTEGER) FILTER (WHERE i.add_to_cart_order = 1)::SMALLINT AS y1,
               max(i.reordered::INTEGER) FILTER (WHERE i.add_to_cart_order = 10)::SMALLINT AS y10,
               max(i.reordered::INTEGER) FILTER (WHERE i.add_to_cart_order = 20)::SMALLINT AS y20,
               max(d.department_id) FILTER (WHERE i.add_to_cart_order = 1)::SMALLINT AS dept1,
               max(d.department_id) FILTER (WHERE i.add_to_cart_order = 10)::SMALLINT AS dept10,
               max(d.department_id) FILTER (WHERE i.add_to_cart_order = 20)::SMALLINT AS dept20
        FROM mart.order_summary om
        JOIN core.order_items i USING (order_id)
        JOIN core.products p USING (product_id)
        JOIN core.aisles a USING (aisle_id)
        JOIN core.departments d USING (department_id)
        WHERE om.basket_size >= 20 AND i.add_to_cart_order IN (1, 10, 20)
        GROUP BY om.user_id, om.order_id
        HAVING count(*) = 3
        """
    )
    connection.execute("CREATE UNIQUE INDEX ON tmp_h3_orders (order_id)")
    connection.execute("CREATE INDEX ON tmp_h3_orders (user_id)")
    connection.execute("ANALYZE tmp_h3_orders")


def _pair_counts(connection: psycopg.Connection, first: int, second: int) -> dict[str, int]:
    y1, y2 = f"y{first}", f"y{second}"
    matched = f"dept{first} = dept{second}"
    row = connection.execute(
        f"""
        SELECT count(*)::BIGINT,
               count(*) FILTER (WHERE {y1} = 1 AND {y2} = 0)::BIGINT,
               count(*) FILTER (WHERE {y1} = 0 AND {y2} = 1)::BIGINT,
               sum({y1} - {y2})::BIGINT,
               count(*) FILTER (WHERE {matched})::BIGINT,
               count(*) FILTER (WHERE {matched} AND {y1} = 1 AND {y2} = 0)::BIGINT,
               count(*) FILTER (WHERE {matched} AND {y1} = 0 AND {y2} = 1)::BIGINT,
               sum({y1} - {y2}) FILTER (WHERE {matched})::BIGINT
        FROM tmp_h3_orders
        """
    ).fetchone()
    keys = (
        "orders",
        "discordant_first_only",
        "discordant_second_only",
        "difference_sum",
        "same_department_orders",
        "same_department_first_only",
        "same_department_second_only",
        "same_department_difference_sum",
    )
    return {key: int(value or 0) for key, value in zip(keys, row, strict=True)}


def _customer_pair_counts(connection: psycopg.Connection, customer_count: int) -> np.ndarray:
    """Stream H3 per-customer counts into a compact fixed-width numeric array."""
    values = np.zeros((customer_count, 13), dtype=np.int64)
    with connection.cursor(name=f"analytics_h3_customers_{uuid.uuid4().hex}") as cursor:
        cursor.execute(
            """
        SELECT s.customer_index,
               count(h.order_id)::BIGINT,
               coalesce(sum(h.y1), 0)::BIGINT,
               coalesce(sum(h.y10), 0)::BIGINT,
               coalesce(sum(h.y20), 0)::BIGINT,
               coalesce(sum(h.y1 - h.y10), 0)::BIGINT,
               coalesce(sum(h.y1 - h.y20), 0)::BIGINT,
               coalesce(sum(h.y10 - h.y20), 0)::BIGINT,
               count(*) FILTER (WHERE h.dept1 = h.dept10)::BIGINT,
               coalesce(sum(h.y1 - h.y10) FILTER (WHERE h.dept1 = h.dept10), 0)::BIGINT,
               count(*) FILTER (WHERE h.dept1 = h.dept20)::BIGINT,
               coalesce(sum(h.y1 - h.y20) FILTER (WHERE h.dept1 = h.dept20), 0)::BIGINT,
               count(*) FILTER (WHERE h.dept10 = h.dept20)::BIGINT,
               coalesce(sum(h.y10 - h.y20) FILTER (WHERE h.dept10 = h.dept20), 0)::BIGINT
        FROM tmp_analytics_users s LEFT JOIN tmp_h3_orders h USING (user_id)
        GROUP BY s.customer_index ORDER BY s.customer_index
        """
        )
        offset = 0
        while batch := cursor.fetchmany(50_000):
            matrix = np.asarray(batch, dtype=np.int64)
            if matrix.shape[1] != values.shape[1] + 1:
                raise ValueError("unexpected H3 customer aggregate shape")
            indices = matrix[:, 0]
            if np.any(indices < offset) or np.any(indices >= customer_count):
                raise ValueError("H3 customer aggregates are not ordered or in range")
            values[indices] = matrix[:, 1:]
            offset = int(indices[-1]) + 1
        if offset != customer_count:
            raise ValueError("H3 customer aggregates do not cover the full population")
    return values


def _bootstrap_pair_differences(
    data: np.ndarray, replicates: int, random_seed: int
) -> dict[str, tuple[float, float] | None]:
    if data.shape[0] == 0:
        return {
            name: None
            for name in (
                "1_vs_10",
                "1_vs_20",
                "10_vs_20",
                "1_vs_10_same_department",
                "1_vs_20_same_department",
                "10_vs_20_same_department",
            )
        }
    pairs = (
        ("1_vs_10", 0, 4),
        ("1_vs_20", 0, 5),
        ("10_vs_20", 0, 6),
        ("1_vs_10_same_department", 7, 8),
        ("1_vs_20_same_department", 9, 10),
        ("10_vs_20_same_department", 11, 12),
    )
    result: dict[str, tuple[float, float] | None] = {}
    samples = {name: [] for name, _denominator, _numerator in pairs}
    for sample_weights in _bootstrap_weights(data.shape[0], replicates, random_seed):
        totals = sample_weights @ data
        for name, denominator_column, numerator_column in pairs:
            denominator = int(totals[denominator_column])
            if denominator:
                samples[name].append(float(totals[numerator_column] / denominator))
    for name, _denominator, _numerator in pairs:
        result[name] = percentile_interval(samples[name])
    return result


def _holm_adjust_optional(p_values: Sequence[float | None]) -> list[float | None]:
    valid_positions = [index for index, value in enumerate(p_values) if value is not None]
    adjusted: list[float | None] = [None] * len(p_values)
    if valid_positions:
        values = holm_adjust([float(p_values[index]) for index in valid_positions])
        for index, value in zip(valid_positions, values, strict=True):
            adjusted[index] = value
    return adjusted


def _h3(
    connection: psycopg.Connection,
    run_id: uuid.UUID,
    fingerprint: str,
    customer_count: int,
    replicates: int,
    random_seed: int,
) -> dict[str, Any]:
    _build_h3_orders(connection)
    omnibus = connection.execute(
        "SELECT count(*)::BIGINT, sum(y1 + y10 + y20)::BIGINT, "
        "sum((y1 + y10 + y20)::BIGINT * (y1 + y10 + y20)::BIGINT)::BIGINT, "
        "sum(y1)::BIGINT, sum(y10)::BIGINT, sum(y20)::BIGINT FROM tmp_h3_orders"
    ).fetchone()
    orders, total_success, sum_squares, r1, r10, r20 = (int(value or 0) for value in omnibus)
    numerator = 2 * (3 * (r1 * r1 + r10 * r10 + r20 * r20) - (r1 + r10 + r20) ** 2)
    denominator = 3 * total_success - sum_squares
    q_statistic = float(numerator / denominator) if denominator > 0 else None
    from scipy.stats import chi2

    q_p_value = float(chi2.sf(q_statistic, 2)) if q_statistic is not None else None
    q_log10_p_value = (
        log10_p_value(q_statistic, 2, q_p_value)
        if q_statistic is not None and q_p_value is not None
        else None
    )
    rates = {
        "1": r1 / orders if orders else None,
        "10": r10 / orders if orders else None,
        "20": r20 / orders if orders else None,
    }

    customer_counts = _customer_pair_counts(connection, customer_count)
    intervals = _bootstrap_pair_differences(customer_counts, replicates, random_seed + 4)
    counts_by_pair: dict[str, dict[str, Any]] = {}
    comparisons = ((1, 10), (1, 20), (10, 20))
    raw_ps: list[float | None] = []
    matched_ps: list[float | None] = []
    for first, second in comparisons:
        counts = _pair_counts(connection, first, second)
        name = f"{first}_vs_{second}"
        stat, p_value = (
            mcnemar_from_counts(counts["discordant_first_only"], counts["discordant_second_only"])
            if counts["orders"]
            else (None, None)
        )
        matched_stat, matched_p = (
            mcnemar_from_counts(
                counts["same_department_first_only"],
                counts["same_department_second_only"],
            )
            if counts["same_department_orders"]
            else (None, None)
        )
        raw_ps.append(p_value)
        matched_ps.append(matched_p)
        counts_by_pair[name] = {
            **counts,
            "statistic": stat,
            "p_value": p_value,
            "log10_p_value": log10_p_value(stat, 1, p_value)
            if stat is not None and p_value is not None
            else None,
            "risk_difference": counts["difference_sum"] / counts["orders"]
            if counts["orders"]
            else None,
            "same_department_statistic": matched_stat,
            "same_department_p_value": matched_p,
            "same_department_log10_p_value": log10_p_value(matched_stat, 1, matched_p)
            if matched_stat is not None and matched_p is not None
            else None,
            "same_department_risk_difference": (
                counts["same_department_difference_sum"] / counts["same_department_orders"]
                if counts["same_department_orders"]
                else None
            ),
            "bootstrap_ci": intervals[name],
            "same_department_bootstrap_ci": intervals[f"{name}_same_department"],
        }
    adjusted = _holm_adjust_optional(raw_ps)
    adjusted_matched = _holm_adjust_optional(matched_ps)
    for index, (first, second) in enumerate(comparisons):
        name = f"{first}_vs_{second}"
        item = counts_by_pair[name]
        item["p_adjusted"] = adjusted[index]
        item["same_department_p_adjusted"] = adjusted_matched[index]
        for department_matched, _stat, p_value, p_adjusted, difference, interval, n, b, c in (
            (
                False,
                item["statistic"],
                item["p_value"],
                adjusted[index],
                item["risk_difference"],
                item["bootstrap_ci"],
                item["orders"],
                item["discordant_first_only"],
                item["discordant_second_only"],
            ),
            (
                True,
                item["same_department_statistic"],
                item["same_department_p_value"],
                adjusted_matched[index],
                item["same_department_risk_difference"],
                item["same_department_bootstrap_ci"],
                item["same_department_orders"],
                item["same_department_first_only"],
                item["same_department_second_only"],
            ),
        ):
            bounds = interval or (None, None)
            connection.execute(
                "INSERT INTO mart._hypothesis_pairs "
                "(run_id, dataset_fingerprint, comparison, first_position, second_position, "
                "department_matched, order_count, discordant_first_only, discordant_second_only, "
                "risk_difference, ci_lower, ci_upper, p_value, p_adjusted, "
                "bootstrap_ci_lower, bootstrap_ci_upper) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    run_id,
                    fingerprint,
                    name,
                    first,
                    second,
                    department_matched,
                    n,
                    b,
                    c,
                    difference,
                    bounds[0],
                    bounds[1],
                    p_value,
                    p_adjusted,
                    bounds[0],
                    bounds[1],
                ),
            )

    nonempty_rates = [value for value in rates.values() if value is not None]
    rate_range = max(nonempty_rates) - min(nonempty_rates) if nonempty_rates else None
    range_ci = None
    if customer_count:
        ranges = []
        position_values = customer_counts[:, :4]
        for sample_weights in _bootstrap_weights(customer_count, replicates, random_seed + 5):
            totals = sample_weights @ position_values
            if totals[0]:
                position_rates = totals[1:4] / totals[0]
                ranges.append(float(position_rates.max() - position_rates.min()))
        range_ci = percentile_interval(ranges)

    result_metrics = {
        "orders_with_basket_size_at_least_20": orders,
        "position_reorder_rates": rates,
        "position_rate_range": rate_range,
        "cochran_q": q_statistic,
        "cochran_q_p_value": q_p_value,
        "cochran_q_log10_p_value": q_log10_p_value,
        "omnibus_p_value_method": (
            "Cochran Q on eligible orders; assumes independent orders within customers too"
        ),
        "pairwise_p_value_method": (
            "continuity-corrected McNemar; assumes independent pairs within customers too"
        ),
        "pairwise_mcnemar": counts_by_pair,
        "holm_adjusted_p_values": {
            key: value for key, value in zip(counts_by_pair, adjusted, strict=True)
        },
        "holm_adjusted_same_department_p_values": {
            key: value for key, value in zip(counts_by_pair, adjusted_matched, strict=True)
        },
        "bootstrap": {
            "sampling_unit": "customer",
            "sampled_customers": customer_count,
            "replicates": replicates,
            "random_seed": random_seed + 4,
            "position_rate_range_ci": range_ci,
        },
        "basket_size_condition": "same prior/train orders with at least 20 known items",
        "category_sensitivity": (
            "pair comparisons restricted to the same department at both positions"
        ),
    }
    _put_result(
        connection,
        run_id,
        fingerprint,
        hypothesis_id="H3",
        h0="В долях повторных покупок для позиций 1, 10 и 20 в одной корзине нет различий.",
        h1="Хотя бы для одной пары позиций 1, 10 и 20 доли повторных покупок различаются.",
        method="Критерий Кокрана Q для трёх связанных бинарных измерений; парные тесты Мак-Немара "
        "с поправкой Holm; bootstrap по покупателям.",
        observations=orders,
        statistic=q_statistic,
        p_value=q_p_value,
        effect_name="range of position reorder rates",
        effect_value=rate_range,
        ci=range_ci,
        metrics=result_metrics,
        interpretation=_interpret(q_p_value, "размах долей", rate_range),
    )

    for row in connection.execute(
        "SELECT om.eval_set, i.add_to_cart_order, cat.category_level, cat.category_id, "
        "cat.category_name, count(*)::BIGINT, sum(i.reordered::INTEGER)::BIGINT "
        "FROM tmp_h3_orders h JOIN mart.order_summary om USING (order_id) "
        "JOIN core.order_items i USING (order_id) "
        "JOIN core.products p USING (product_id) "
        "JOIN core.aisles a USING (aisle_id) "
        "JOIN core.departments d USING (department_id) "
        "CROSS JOIN LATERAL (VALUES "
        "('department'::TEXT, d.department_id::INTEGER, d.department_name), "
        "('aisle'::TEXT, a.aisle_id::INTEGER, a.aisle_name)"
        ") AS cat(category_level, category_id, category_name) "
        "WHERE i.add_to_cart_order IN (1, 10, 20) "
        "GROUP BY om.eval_set, i.add_to_cart_order, cat.category_level, cat.category_id, "
        "cat.category_name ORDER BY cat.category_level, cat.category_id, om.eval_set, "
        "i.add_to_cart_order"
    ).fetchall():
        eval_set, position, level, category_id, category_name, item_count, repeat_count = row
        interval = wilson_interval(int(repeat_count), int(item_count))
        connection.execute(
            "INSERT INTO mart._hypothesis_categories "
            "(run_id, dataset_fingerprint, hypothesis_id, category_level, category_id, "
            "category_name, eval_set, cart_position, item_count, reorder_count, reorder_rate, "
            "ci_lower, ci_upper, metrics) VALUES "
            "(%s, %s, 'H3_category', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                run_id,
                fingerprint,
                level,
                int(category_id),
                category_name,
                eval_set,
                int(position),
                int(item_count),
                int(repeat_count),
                float(repeat_count / item_count) if item_count else 0.0,
                interval[0] if interval else 0.0,
                interval[1] if interval else 0.0,
                Jsonb(
                    {
                        "interval_method": (
                            "descriptive Wilson score; no customer-cluster adjustment"
                        ),
                        "category_scope": "same prior/train orders with basket_size >= 20",
                    }
                ),
            ),
        )
    return result_metrics
