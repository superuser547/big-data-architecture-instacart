"""Three reproducible association studies over PostgreSQL aggregates."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any

import numpy as np
import psycopg
from psycopg.types.json import Jsonb

from instacart.analytics.statistics import (
    chi_square_summary,
    holm_adjust,
    log10_p_value,
    percentile_interval,
    wilson_interval,
)

ALPHA = 0.05


def _bootstrap_weights(customer_count: int, replicates: int, random_seed: int) -> list[np.ndarray]:
    rng = np.random.default_rng(random_seed)
    return [
        np.bincount(rng.integers(0, customer_count, size=customer_count), minlength=customer_count)
        for _ in range(replicates)
    ]


def _cramers_v(table: np.ndarray) -> float | None:
    return chi_square_summary(table)["cramers_v"]


def _bootstrap_contingency(
    customer_matrix: np.ndarray,
    row_count: int,
    column_count: int,
    *,
    replicates: int,
    random_seed: int,
) -> dict[str, Any]:
    if customer_matrix.shape[0] == 0:
        return {"sampled_customers": 0, "replicates": 0, "cramers_v_ci": None}
    weights = _bootstrap_weights(customer_matrix.shape[0], replicates, random_seed)
    effects: list[float] = []
    significant = 0
    category_rates = [[] for _ in range(row_count)] if column_count == 2 else None
    for sample_weights in weights:
        table = (sample_weights @ customer_matrix).reshape(row_count, column_count)
        summary = chi_square_summary(table)
        if summary["cramers_v"] is not None:
            effects.append(summary["cramers_v"])
            significant += int(summary["p_value"] is not None and summary["p_value"] < ALPHA)
        if category_rates is not None:
            totals = table.sum(axis=1)
            for row_index, total in enumerate(totals):
                if total:
                    category_rates[row_index].append(table[row_index, 1] / total)
    result = {
        "sampled_customers": int(customer_matrix.shape[0]),
        "replicates": replicates,
        "cramers_v_ci": percentile_interval(effects),
        "significant_replicate_share": significant / len(effects) if effects else None,
        "sampling_unit": "customer",
        "random_seed": random_seed,
    }
    if category_rates is not None:
        result["category_rate_ci"] = [percentile_interval(values) for values in category_rates]
    return result


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


def _create_customer_sample(
    connection: psycopg.Connection,
    random_seed: int,
    sample_size: int,
) -> int:
    connection.execute(
        "CREATE TEMP TABLE tmp_analytics_users ON COMMIT DROP AS "
        "SELECT user_id FROM core.users "
        "ORDER BY hashtextextended(user_id::TEXT, %s) LIMIT %s",
        (random_seed, sample_size),
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
    bootstrap_customer_sample: int = 10_000,
) -> dict[str, Any]:
    """Calculate full-data tests and customer-cluster bootstrap stability checks."""
    for table in ("_hypothesis_results", "_hypothesis_categories", "_hypothesis_pairs"):
        connection.execute(f"DELETE FROM mart.{table}")
    sample_count = _create_customer_sample(connection, random_seed, bootstrap_customer_sample)
    h1 = _h1(connection, run_id, fingerprint, sample_count, bootstrap_replicates, random_seed)
    h2 = _h2(connection, run_id, fingerprint, sample_count, bootstrap_replicates, random_seed)
    h3 = _h3(
        connection,
        run_id,
        fingerprint,
        sample_count,
        bootstrap_replicates,
        random_seed,
    )
    return {
        "alpha": ALPHA,
        "customer_bootstrap": {
            "random_seed": random_seed,
            "replicates": bootstrap_replicates,
            "sampled_customers": sample_count,
            "customer_sample_method": "lowest hashtextextended(user_id, random_seed)",
        },
        "H1": h1,
        "H2": h2,
        "H3": h3,
    }


def _h1(
    connection: psycopg.Connection,
    run_id: uuid.UUID,
    fingerprint: str,
    sample_count: int,
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

    customer_ids = [
        int(row[0])
        for row in connection.execute("SELECT user_id FROM tmp_analytics_users ORDER BY user_id")
    ]
    position = {user_id: index for index, user_id in enumerate(customer_ids)}
    per_customer = np.zeros((sample_count, 7 * 24), dtype=np.int64)
    if customer_ids:
        for user_id, dow, hour, count in connection.execute(
            "SELECT s.user_id, o.order_dow, o.order_hour_of_day, count(*) "
            "FROM tmp_analytics_users s JOIN core.orders o USING (user_id) "
            "GROUP BY s.user_id, o.order_dow, o.order_hour_of_day"
        ).fetchall():
            per_customer[position[int(user_id)], int(dow) * 24 + int(hour)] = int(count)
    bootstrap = _bootstrap_contingency(
        per_customer,
        7,
        24,
        replicates=replicates,
        random_seed=random_seed + 1,
    )
    metrics = {
        **summary,
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
        method="Критерий Пирсона χ² независимости на таблице 7×24; bootstrap по покупателям.",
        observations=summary["observations"],
        statistic=summary["statistic"],
        p_value=summary["p_value"],
        effect_name="Cramér’s V",
        effect_value=summary["cramers_v"],
        ci=bootstrap["cramers_v_ci"],
        metrics=metrics,
        interpretation=_interpret(summary["p_value"], "Cramér’s V", summary["cramers_v"]),
    )
    return metrics


def _category_customer_matrix(
    connection: psycopg.Connection,
    level: str,
    category_ids: Sequence[int],
    sample_count: int,
) -> np.ndarray:
    index = {category_id: position for position, category_id in enumerate(category_ids)}
    result = np.zeros((sample_count, len(category_ids) * 2), dtype=np.int64)
    users = [
        int(row[0])
        for row in connection.execute("SELECT user_id FROM tmp_analytics_users ORDER BY user_id")
    ]
    user_index = {user_id: position for position, user_id in enumerate(users)}
    if level == "department":
        category_sql = "d.department_id"
    else:
        category_sql = "a.aisle_id"
    rows = connection.execute(
        f"""
        SELECT s.user_id, {category_sql} AS category_id,
               count(*)::BIGINT AS item_count,
               sum(i.reordered::INTEGER)::BIGINT AS repeat_count
        FROM tmp_analytics_users s
        JOIN core.orders o ON o.user_id = s.user_id AND o.eval_set IN ('prior', 'train')
        JOIN core.order_items i USING (order_id)
        JOIN core.products p USING (product_id)
        JOIN core.aisles a USING (aisle_id)
        JOIN core.departments d USING (department_id)
        GROUP BY s.user_id, {category_sql}
        """
    ).fetchall()
    for user_id, category_id, item_count, repeat_count in rows:
        u = user_index[int(user_id)]
        c = index[int(category_id)]
        result[u, c * 2] = int(item_count - repeat_count)
        result[u, c * 2 + 1] = int(repeat_count)
    return result


def _h2(
    connection: psycopg.Connection,
    run_id: uuid.UUID,
    fingerprint: str,
    sample_count: int,
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
        interval = wilson_interval(int(repeat_count), int(item_count))
        entry = {
            "category_id": int(group_id),
            "category_name": group_name,
            "department_id": int(department_id),
            "department_name": department_name,
            "item_count": int(item_count),
            "reorder_count": int(repeat_count),
            "reorder_rate": float(repeat_count / item_count) if item_count else 0.0,
            "confidence_interval": interval,
            "confidence_interval_method": (
                "Wilson score; replaced by customer bootstrap when estimable"
            ),
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
                interval[0] if interval else 0.0,
                interval[1] if interval else 0.0,
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
        user_matrix = _category_customer_matrix(connection, level, category_ids, sample_count)
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
            if interval is None:
                interval = category["confidence_interval"]
            category["confidence_interval"] = interval
            connection.execute(
                "UPDATE mart._hypothesis_categories SET ci_lower = %s, ci_upper = %s, metrics = %s "
                "WHERE run_id = %s AND hypothesis_id = %s AND category_level = %s "
                "AND category_id = %s AND eval_set = 'prior+train'",
                (
                    interval[0],
                    interval[1],
                    Jsonb(
                        {
                            "interval_method": (
                                "customer bootstrap percentile"
                                if index < len(category_rate_intervals)
                                and category_rate_intervals[index]
                                else "Wilson score fallback"
                            ),
                            "bootstrap_customers": sample_count,
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
                f"Критерий Пирсона χ² для {level} × reordered/not reordered; "
                "bootstrap по покупателям."
            ),
            observations=test["observations"],
            statistic=test["statistic"],
            p_value=test["p_value"],
            effect_name="Cramér’s V",
            effect_value=test["cramers_v"],
            ci=bootstrap["cramers_v_ci"],
            metrics=metrics,
            interpretation=_interpret(test["p_value"], "Cramér’s V", test["cramers_v"]),
        )
    p_values = [
        test_summaries[level]["p_value"] if test_summaries[level]["p_value"] is not None else 1.0
        for level in ("aisle", "department")
    ]
    adjusted = holm_adjust(p_values)
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


def _mcnemar_from_counts(first_only: int, second_only: int) -> tuple[float, float]:
    discordant = first_only + second_only
    statistic = max(abs(first_only - second_only) - 1, 0) ** 2 / discordant if discordant else 0.0
    from scipy.stats import chi2

    return float(statistic), float(chi2.sf(statistic, 1)) if discordant else 1.0


def _sample_customer_pair_rows(
    connection: psycopg.Connection,
) -> list[tuple[int, ...]]:
    return connection.execute(
        """
        SELECT s.user_id,
               count(h.order_id)::BIGINT,
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
        GROUP BY s.user_id ORDER BY s.user_id
        """
    ).fetchall()


def _bootstrap_pair_differences(
    rows: Sequence[Sequence[int]], replicates: int, random_seed: int
) -> dict[str, tuple[float, float] | None]:
    if not rows:
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
    data = np.asarray(rows, dtype=np.int64)
    pairs = (
        ("1_vs_10", 1, 2),
        ("1_vs_20", 1, 3),
        ("10_vs_20", 1, 4),
        ("1_vs_10_same_department", 5, 6),
        ("1_vs_20_same_department", 7, 8),
        ("10_vs_20_same_department", 9, 10),
    )
    result: dict[str, tuple[float, float] | None] = {}
    weights = _bootstrap_weights(data.shape[0], replicates, random_seed)
    for name, denominator_column, numerator_column in pairs:
        values = []
        for sample_weights in weights:
            denominator = int(sample_weights @ data[:, denominator_column])
            numerator = int(sample_weights @ data[:, numerator_column])
            if denominator:
                values.append(numerator / denominator)
        result[name] = percentile_interval(values)
    return result


def _h3(
    connection: psycopg.Connection,
    run_id: uuid.UUID,
    fingerprint: str,
    sample_count: int,
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

    samples = _sample_customer_pair_rows(connection) if sample_count else []
    intervals = _bootstrap_pair_differences(samples, replicates, random_seed + 4)
    counts_by_pair: dict[str, dict[str, Any]] = {}
    comparisons = ((1, 10), (1, 20), (10, 20))
    raw_ps: list[float] = []
    matched_ps: list[float] = []
    for first, second in comparisons:
        counts = _pair_counts(connection, first, second)
        name = f"{first}_vs_{second}"
        stat, p_value = _mcnemar_from_counts(
            counts["discordant_first_only"], counts["discordant_second_only"]
        )
        matched_stat, matched_p = _mcnemar_from_counts(
            counts["same_department_first_only"], counts["same_department_second_only"]
        )
        raw_ps.append(p_value)
        matched_ps.append(matched_p)
        counts_by_pair[name] = {
            **counts,
            "statistic": stat,
            "p_value": p_value,
            "log10_p_value": log10_p_value(stat, 1, p_value),
            "risk_difference": counts["difference_sum"] / counts["orders"]
            if counts["orders"]
            else 0.0,
            "same_department_statistic": matched_stat,
            "same_department_p_value": matched_p,
            "same_department_log10_p_value": log10_p_value(matched_stat, 1, matched_p),
            "same_department_risk_difference": (
                counts["same_department_difference_sum"] / counts["same_department_orders"]
                if counts["same_department_orders"]
                else 0.0
            ),
            "bootstrap_ci": intervals[name],
            "same_department_bootstrap_ci": intervals[f"{name}_same_department"],
        }
    adjusted = holm_adjust(raw_ps)
    adjusted_matched = holm_adjust(matched_ps)
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

    rate_range = max((value for value in rates.values() if value is not None), default=0.0) - min(
        (value for value in rates.values() if value is not None), default=0.0
    )
    # Bootstrap the overall range using per-customer event totals from the same sampled users.
    customer_position_counts = connection.execute(
        "SELECT s.user_id, count(h.order_id)::BIGINT, coalesce(sum(h.y1), 0)::BIGINT, "
        "coalesce(sum(h.y10), 0)::BIGINT, coalesce(sum(h.y20), 0)::BIGINT "
        "FROM tmp_analytics_users s LEFT JOIN tmp_h3_orders h USING (user_id) "
        "GROUP BY s.user_id ORDER BY s.user_id"
    ).fetchall()
    range_ci = None
    if customer_position_counts:
        values = np.asarray(customer_position_counts, dtype=np.int64)[:, 1:]
        weights = _bootstrap_weights(values.shape[0], replicates, random_seed + 5)
        ranges = []
        for sample_weights in weights:
            totals = sample_weights @ values
            if totals[0]:
                position_rates = totals[1:] / totals[0]
                ranges.append(float(position_rates.max() - position_rates.min()))
        range_ci = percentile_interval(ranges)

    result_metrics = {
        "orders_with_basket_size_at_least_20": orders,
        "position_reorder_rates": rates,
        "position_rate_range": rate_range,
        "cochran_q": q_statistic,
        "cochran_q_p_value": q_p_value,
        "cochran_q_log10_p_value": q_log10_p_value,
        "pairwise_mcnemar": counts_by_pair,
        "holm_adjusted_p_values": {
            key: value for key, value in zip(counts_by_pair, adjusted, strict=True)
        },
        "holm_adjusted_same_department_p_values": {
            key: value for key, value in zip(counts_by_pair, adjusted_matched, strict=True)
        },
        "bootstrap": {
            "sampling_unit": "customer",
            "sampled_customers": sample_count,
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
        "SELECT eval_set, cart_position, category_level, category_id, category_name, "
        "sum(item_count)::BIGINT, sum(reorder_count)::BIGINT "
        "FROM mart.cart_position_stats "
        "WHERE cart_position IN (1, 10, 20) AND category_level IN ('department', 'aisle') "
        "GROUP BY eval_set, cart_position, category_level, category_id, category_name "
        "ORDER BY category_level, category_id, eval_set, cart_position"
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
                Jsonb({"interval_method": "Wilson score"}),
            ),
        )
    return result_metrics
