"""Published H1–H3 inference and clearly separated descriptive slices."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import plotly.graph_objects as go
import streamlit as st

from instacart.dashboard.charts import PALETTE, style_figure
from instacart.dashboard.components import empty_state, page_title
from instacart.dashboard.data import Readiness, load_h1_slice, load_hypotheses
from instacart.dashboard.metrics import (
    format_number,
    format_p_value,
    format_percent,
    format_percentage_points,
    interval_text,
    json_log_p,
    lineage_arguments,
    percentage_point_interval,
)


def _result_by_id(rows: list[dict[str, Any]], identifier: str) -> dict[str, Any] | None:
    return next((row for row in rows if row["hypothesis_id"] == identifier), None)


def _show_inference(result: dict[str, Any] | None) -> None:
    if result is None:
        empty_state("Для этой гипотезы нет опубликованного результата.")
        return
    metrics = result["metrics"] or {}
    log_key = "cochran_q_log10_p_value" if result["hypothesis_id"] == "H3" else "log10_p_value"
    log_p = json_log_p(metrics, log_key)
    st.markdown(f"**H₀:** {result['h0']}")
    st.markdown(f"**H₁:** {result['h1']}")
    st.caption(f"Метод: {result['method']}")
    cols = st.columns(5)
    with cols[0]:
        st.metric("Наблюдения", format_number(result["observation_count"]))
    with cols[1]:
        st.metric("Статистика теста", format_number(result["statistic"], 3))
    with cols[2]:
        st.metric(
            "p-value",
            format_p_value(result["p_value"], log_p),
        )
    with cols[3]:
        st.metric(
            result["effect_name"] or "Размер эффекта",
            format_percentage_points(result["effect_value"])
            if result["hypothesis_id"] == "H3"
            else format_number(result["effect_value"], 3),
        )
    with cols[4]:
        st.metric(
            "95% ДИ эффекта",
            percentage_point_interval(result["ci_lower"], result["ci_upper"])
            if result["hypothesis_id"] == "H3"
            else interval_text(result["ci_lower"], result["ci_upper"]),
        )
    st.info(result["interpretation"])


def _heatmap(
    values: list[list[float]], title: str, *, percent: bool = False, residual: bool = False
) -> None:
    colorscale = (
        "RdBu" if residual else [[0, "#EDF2F7"], [0.45, PALETTE["blue"]], [1, PALETTE["navy"]]]
    )
    midpoint = 0 if residual else None
    hover = (
        "%{y}, %{x}<br>Остаток Пирсона: %{z:.2f}<extra></extra>"
        if residual
        else (
            "%{y}, %{x}<br>Доля: %{z:.1%}<extra></extra>"
            if percent
            else "%{y}, %{x}<br>Заказов: %{z:,}<extra></extra>"
        )
    )
    figure = go.Figure(
        go.Heatmap(
            z=values,
            x=[f"{hour:02d}:00" for hour in range(24)],
            y=[f"Код {day}" for day in range(7)],
            colorscale=colorscale,
            zmid=midpoint,
            colorbar={"title": "Остаток" if residual else ("Доля" if percent else "Заказы")},
            hovertemplate=hover,
        )
    )
    st.plotly_chart(
        style_figure(figure, title=title, x_title="Час", y_title="Код order_dow"),
        use_container_width=True,
    )


def _h1_page(result: dict[str, Any] | None, readiness: Readiness, database_url: str) -> None:
    st.subheader("H1 · Код дня недели и час заказа")
    _show_inference(result)
    if result is None:
        return
    metrics = result["metrics"] or {}
    if not metrics.get("observed"):
        empty_state("В опубликованном результате отсутствует матрица наблюдаемых частот.")
        return
    left, right = st.columns(2)
    with left:
        _heatmap(metrics["observed"], "Наблюдаемые частоты · весь набор")
    with right:
        _heatmap(metrics["row_normalized"], "Нормированная доля внутри кода дня", percent=True)
    _heatmap(metrics["pearson_residuals"], "Остатки Пирсона · весь набор", residual=True)

    st.markdown("**Описательный интерактивный срез**")
    st.caption(
        "Фильтры меняют только частоты ниже. χ², p-value, эффект и интервал выше "
        "рассчитаны на полном наборе."
    )
    filter_cols = st.columns(3)
    with filter_cols[0]:
        eval_sets = st.multiselect(
            "Выборки",
            ["prior", "train", "test"],
            default=["prior", "train", "test"],
            format_func=lambda value: {"prior": "Prior", "train": "Train", "test": "Test"}[value],
            key="h1_slice_eval",
        )
    with filter_cols[1]:
        weekdays = st.multiselect(
            "Коды order_dow",
            list(range(7)),
            default=list(range(7)),
            format_func=lambda code: f"Код {code}",
            key="h1_slice_dow",
        )
    with filter_cols[2]:
        hours = st.multiselect(
            "Час заказа",
            list(range(24)),
            default=list(range(24)),
            format_func=lambda hour: f"{hour:02d}:00",
            key="h1_slice_hour",
        )
    fingerprint, ingestion_id, marts_id, analysis_id = lineage_arguments(readiness)
    rows = load_h1_slice(
        database_url,
        fingerprint,
        ingestion_id,
        marts_id,
        analysis_id,
        tuple(eval_sets),
        tuple(weekdays),
        tuple(hours),
    )
    if not rows:
        empty_state("Для выбранных фильтров заказов нет.")
    else:
        matrix = [[0] * 24 for _ in range(7)]
        for row in rows:
            matrix[row["order_dow"]][row["order_hour_of_day"]] += row["order_count"]
        _heatmap(matrix, "Интерактивная частота заказов · описательная выборка")


def _h2_page(results: list[dict[str, Any]], categories: list[dict[str, Any]], level: str) -> None:
    identifier = "H2_aisle" if level == "aisle" else "H2_department"
    result = _result_by_id(results, identifier)
    st.subheader("H2 · Категория и повторная покупка")
    _show_inference(result)
    if result is None:
        return
    metrics = result["metrics"] or {}
    holm = metrics.get("holm_adjusted_p_value_across_category_levels")
    st.caption(
        f"Поправка Holm между двумя уровнями категорий: {format_p_value(holm)}. "
        "Проверка и bootstrap-интервалы рассчитаны для полного набора позиций prior/train."
    )
    entries = [
        row
        for row in categories
        if row["eval_set"] == "prior+train" and row["category_id"] is not None
    ]
    if not entries:
        empty_state("Нет опубликованных категорийных долей для H2.")
        return
    visible = sorted(entries, key=lambda row: row["item_count"], reverse=True)[:20]
    visible = list(reversed(visible))
    x = [row["reorder_rate"] * 100 for row in visible]
    lower = [
        max(0.0, (row["reorder_rate"] - row["ci_lower"]) * 100)
        if row["ci_lower"] is not None
        else 0
        for row in visible
    ]
    upper = [
        max(0.0, (row["ci_upper"] - row["reorder_rate"]) * 100)
        if row["ci_upper"] is not None
        else 0
        for row in visible
    ]
    figure = go.Figure(
        go.Scatter(
            x=x,
            y=[row["category_name"] for row in visible],
            mode="markers",
            marker={"size": 9, "color": PALETTE["teal"]},
            error_x={
                "type": "data",
                "symmetric": False,
                "array": upper,
                "arrayminus": lower,
                "color": PALETTE["navy"],
            },
            customdata=[[row["item_count"], row["reorder_count"]] for row in visible],
            hovertemplate=(
                "%{y}<br>Доля повторов: %{x:.2f}%<br>Позиции: %{customdata[0]:,}"
                "<br>Повторы: %{customdata[1]:,}<extra></extra>"
            ),
        )
    )
    st.plotly_chart(
        style_figure(
            figure,
            title="Доля повторов и 95% bootstrap ДИ · категории с наибольшим объёмом",
            x_title="Повторные позиции, %",
            y_title="",
            height=min(640, max(420, len(visible) * 24)),
        ),
        use_container_width=True,
    )
    table = [
        {
            "Категория": row["category_name"],
            "Позиции": row["item_count"],
            "Повторные позиции": row["reorder_count"],
            "Доля повторов": format_percent(row["reorder_rate"]),
            "95% ДИ": interval_text(row["ci_lower"], row["ci_upper"], percent=True),
        }
        for row in entries
    ]
    st.dataframe(table, hide_index=True, use_container_width=True)
    st.caption(
        "Интервалы рассчитаны кластерным bootstrap по покупателям. Уникальные "
        "покупатели между категориями не суммируются."
    )


def _h3_page(
    results: list[dict[str, Any]],
    categories: list[dict[str, Any]],
    pairs: list[dict[str, Any]],
    category_level: str,
    eval_sets: tuple[str, ...],
    cart_positions: tuple[int, ...],
    selected_category_id: int | None,
) -> None:
    result = _result_by_id(results, "H3")
    st.subheader("H3 · Позиция товара в корзине")
    _show_inference(result)
    if result is None:
        return
    metrics = result["metrics"] or {}
    rates: Mapping[str, Any] = metrics.get("position_reorder_rates", {})
    rate_rows = [
        {"Позиция": position, "Доля повторов": rates.get(str(position))}
        for position in (1, 10, 20)
        if rates.get(str(position)) is not None
    ]
    if rate_rows:
        rate_fig = go.Figure(
            go.Bar(
                x=[f"Позиция {row['Позиция']}" for row in rate_rows],
                y=[row["Доля повторов"] * 100 for row in rate_rows],
                marker_color=PALETTE["blue"],
                hovertemplate="%{x}<br>Повторные покупки: %{y:.2f}%<extra></extra>",
            )
        )
        st.plotly_chart(
            style_figure(
                rate_fig,
                title="Доля повторных покупок по позиции · корзина от 20 товаров",
                x_title="Позиция товара",
                y_title="Повторные позиции, %",
            ),
            use_container_width=True,
        )

    st.markdown("**Парные различия на сопоставимых корзинах**")
    matched = st.toggle(
        "Обе позиции относятся к одному подразделению",
        value=False,
        key="h3_same_department",
        help=(
            "Ограничивает пары корзинами, в которых сравниваемые позиции относятся "
            "к одному department."
        ),
    )
    pair_rows = [row for row in pairs if bool(row["department_matched"]) == matched]
    if pair_rows:
        differences = [
            row["risk_difference"] * 100 if row["risk_difference"] is not None else None
            for row in pair_rows
        ]
        lower = [
            max(0, (row["risk_difference"] - row["ci_lower"]) * 100)
            if row["risk_difference"] is not None and row["ci_lower"] is not None
            else 0
            for row in pair_rows
        ]
        upper = [
            max(0, (row["ci_upper"] - row["risk_difference"]) * 100)
            if row["risk_difference"] is not None and row["ci_upper"] is not None
            else 0
            for row in pair_rows
        ]
        figure = go.Figure(
            go.Scatter(
                x=differences,
                y=[row["comparison"].replace("_vs_", " − ") for row in pair_rows],
                mode="markers",
                marker={"size": 10, "color": PALETTE["coral"] if matched else PALETTE["navy"]},
                error_x={"type": "data", "symmetric": False, "array": upper, "arrayminus": lower},
                customdata=[[row["order_count"]] for row in pair_rows],
                hovertemplate=(
                    "%{y}<br>Разность долей: %{x:.2f} п.п.<br>"
                    "Корзины: %{customdata[0]:,}<extra></extra>"
                ),
            )
        )
        figure.add_vline(x=0, line_dash="dash", line_color=PALETTE["slate"])
        st.plotly_chart(
            style_figure(
                figure,
                title="Парные разности долей повторов и 95% bootstrap ДИ",
                x_title="Разность, п.п. (первая позиция − вторая)",
                y_title="Сравнение",
                height=330,
            ),
            use_container_width=True,
        )
        table = [
            {
                "Сравнение": row["comparison"].replace("_vs_", " − "),
                "Корзины": row["order_count"],
                "Первая позиция повторена": row["discordant_first_only"],
                "Вторая позиция повторена": row["discordant_second_only"],
                "Разность долей": format_percentage_points(row["risk_difference"]),
                "95% bootstrap ДИ": percentage_point_interval(row["ci_lower"], row["ci_upper"]),
                "p-value": format_p_value(row["p_value"]),
                "p-value Holm": format_p_value(row["p_adjusted"]),
            }
            for row in pair_rows
        ]
        st.dataframe(table, hide_index=True, use_container_width=True)

    st.markdown("**Описательная детализация по категориям**")
    st.caption(
        "Фильтры влияют только на таблицу ниже. Основной тест H3 и парные p-value "
        "выше остаются опубликованными оценками полного набора."
    )
    rows = [
        row
        for row in categories
        if row["hypothesis_id"] == "H3_category"
        and row["category_level"] == category_level
        and row["eval_set"] in eval_sets
        and row["cart_position"] in cart_positions
        and (selected_category_id is None or row["category_id"] == selected_category_id)
    ]
    if not rows:
        empty_state("Нет категорийных наблюдений для выбранных фильтров.")
        return
    rows.sort(key=lambda row: (row["category_name"], row["eval_set"], row["cart_position"]))
    category_volume: dict[int, int] = {}
    for row in rows:
        category_id = int(row["category_id"])
        category_volume[category_id] = category_volume.get(category_id, 0) + int(row["item_count"])
    chart_category_ids = {
        category_id
        for category_id, _volume in sorted(
            category_volume.items(), key=lambda item: item[1], reverse=True
        )[:20]
    }
    chart_rows = [row for row in rows if int(row["category_id"]) in chart_category_ids]
    category_figure = go.Figure()
    for eval_set in eval_sets:
        subset = [row for row in chart_rows if row["eval_set"] == eval_set]
        if not subset:
            continue
        category_figure.add_trace(
            go.Bar(
                name=eval_set.capitalize(),
                x=[f"{row['category_name']} · {row['cart_position']}" for row in subset],
                y=[row["reorder_rate"] * 100 for row in subset],
                customdata=[
                    [
                        row["item_count"],
                        format_percent(row["ci_lower"]),
                        format_percent(row["ci_upper"]),
                    ]
                    for row in subset
                ],
                hovertemplate=(
                    "%{x}<br>Доля: %{y:.2f}%<br>Позиции: %{customdata[0]:,}"
                    "<br>95% Wilson ДИ: %{customdata[1]}–%{customdata[2]}<extra></extra>"
                ),
            )
        )
    if len(chart_category_ids) < len(category_volume):
        st.caption(
            "График ограничен 20 категориями с наибольшим объёмом; таблица ниже "
            "содержит все категории выбранного среза."
        )
    category_figure.update_layout(barmode="group")
    category_figure.update_xaxes(tickangle=-40)
    st.plotly_chart(
        style_figure(
            category_figure,
            title="Доля повторов по выбранным категориям и позициям",
            x_title="Категория · позиция",
            y_title="Повторные позиции, %",
            height=430,
        ),
        use_container_width=True,
    )
    st.dataframe(
        [
            {
                "Категория": row["category_name"],
                "Выборка": row["eval_set"],
                "Позиция": row["cart_position"],
                "Наблюдений": row["item_count"],
                "Доля повторов": format_percent(row["reorder_rate"]),
                "95% ДИ Wilson": interval_text(row["ci_lower"], row["ci_upper"], percent=True),
            }
            for row in rows
        ],
        hide_index=True,
        use_container_width=True,
    )
    st.caption(
        "Wilson интервалы категорийной детализации описательные и не корректируют "
        "повторные наблюдения покупателей."
    )


def render(readiness: Readiness, database_url: str) -> None:
    page_title(
        "Проверка гипотез",
        "H1–H3 показывают исследования полного набора; интерактивные срезы "
        "отделены от статистических результатов.",
    )
    fingerprint, ingestion_id, marts_id, analysis_id = lineage_arguments(readiness)
    level = st.radio(
        "Категориальный уровень H2 и H3",
        options=["department", "aisle"],
        format_func=lambda value: "Подразделения" if value == "department" else "Товарные группы",
        horizontal=True,
        key="hypothesis_level",
    )
    base_data = load_hypotheses(
        database_url,
        fingerprint,
        ingestion_id,
        marts_id,
        analysis_id,
        level,
        ("prior", "train"),
        (1, 10, 20),
        None,
    )
    h2_options = [row for row in base_data["categories"] if row["eval_set"] == "prior+train"]
    category_labels = {int(row["category_id"]): str(row["category_name"]) for row in h2_options}
    filters = st.columns([2, 1, 1, 2])
    with filters[0]:
        selected_category = st.selectbox(
            "Категорийная детализация",
            options=[None, *category_labels],
            format_func=lambda value: "Все категории" if value is None else category_labels[value],
            key="hypothesis_category",
        )
    with filters[1]:
        eval_sets = st.multiselect(
            "Выборки H3",
            options=["prior", "train"],
            default=["prior", "train"],
            key="hypothesis_eval_sets",
        )
    with filters[2]:
        positions = st.multiselect(
            "Позиции",
            options=[1, 10, 20],
            default=[1, 10, 20],
            key="hypothesis_positions",
        )
    with filters[3]:
        st.caption(
            "Фильтры управляют только категорийной детализацией H3. Опубликованные "
            "тесты и интервалы пересчёту не подвергаются."
        )
    if (
        selected_category is not None
        or tuple(eval_sets) != ("prior", "train")
        or tuple(positions) != (1, 10, 20)
    ):
        data = load_hypotheses(
            database_url,
            fingerprint,
            ingestion_id,
            marts_id,
            analysis_id,
            level,
            tuple(eval_sets),
            tuple(positions),
            selected_category,
        )
    else:
        data = base_data

    results = data["results"]
    tabs = st.tabs(["H1 · Время заказа", "H2 · Категория и повторы", "H3 · Позиция в корзине"])
    with tabs[0]:
        _h1_page(_result_by_id(results, "H1"), readiness, database_url)
    with tabs[1]:
        _h2_page(results, data["categories"], level)
    with tabs[2]:
        _h3_page(
            results,
            data["categories"],
            data["pairs"],
            level,
            tuple(eval_sets),
            tuple(positions),
            int(selected_category) if selected_category is not None else None,
        )
