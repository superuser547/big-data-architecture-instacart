"""Overview page with full-order and known-composition scopes."""

from __future__ import annotations

import plotly.graph_objects as go
import streamlit as st

from instacart.dashboard.charts import PALETTE, style_figure
from instacart.dashboard.components import (
    activity_filters,
    empty_state,
    eval_set_filter,
    metric_card,
    page_title,
)
from instacart.dashboard.data import Readiness, load_overview
from instacart.dashboard.metrics import format_number, lineage_arguments


def render(readiness: Readiness, database_url: str) -> None:
    page_title(
        "Обзор данных",
        "Активность заказов и состав известных корзин. Код дня недели сохраняется как в источнике.",
    )
    eval_sets = eval_set_filter(key="overview_eval_sets")
    weekdays, hours, basket_range = activity_filters("overview")
    fingerprint, ingestion_id, marts_id, analysis_id = lineage_arguments(readiness)
    data = load_overview(
        database_url,
        fingerprint,
        ingestion_id,
        marts_id,
        analysis_id,
        eval_sets,
        weekdays,
        hours,
        *basket_range,
    )
    overview = data["overview"]
    if overview is None:
        empty_state("В опубликованной витрине нет строки mart.dataset_overview.")
        return

    st.subheader("Полный активный набор")
    metrics = st.columns(6)
    cards = [
        ("Покупатели", overview["user_count"], "Покупатели активного импорта."),
        ("Заказы", overview["order_count"], "Все заказы, включая test и заказы без состава."),
        (
            "Товарные позиции",
            overview["known_item_count"],
            "Позиции только в заказах prior/train с известным составом.",
        ),
        ("Товары", overview["product_count"], "Число товаров в справочнике."),
        (
            "Средняя известная корзина",
            overview["avg_basket_size"],
            "Среднее по заказам prior/train с известным составом, товаров на заказ.",
        ),
        (
            "Повторные покупки",
            overview["repeat_rate"],
            "Доля повторных товарных позиций среди всех известных позиций.",
        ),
    ]
    for column, (label, value, help_text) in zip(metrics, cards, strict=True):
        with column:
            metric_card(label, value, help_text, percent=label == "Повторные покупки")
    st.caption(
        f"Известный состав: {format_number(overview['known_order_count'])} заказов и "
        f"{format_number(overview['known_item_count'])} позиций. "
        f"Заказы test ({format_number(overview['test_order_count'])}) входят "
        "в общее число заказов, "
        "но состав их корзин не опубликован."
    )

    eval_counts = {
        "Prior": overview["prior_order_count"],
        "Train": overview["train_order_count"],
        "Test": overview["test_order_count"],
    }
    eval_figure = go.Figure(
        go.Bar(
            x=list(eval_counts),
            y=list(eval_counts.values()),
            marker_color=[PALETTE["navy"], PALETTE["blue"], PALETTE["gold"]],
            hovertemplate="%{x}<br>Заказов: %{y:,}<extra></extra>",
        )
    )
    st.plotly_chart(
        style_figure(
            eval_figure,
            title="Состав заказов по выборке",
            x_title="Выборка",
            y_title="Все заказы",
            height=320,
        ),
        use_container_width=True,
    )

    st.subheader("Активность по времени")
    activity = data["activity"]
    if not activity:
        empty_state("Нет заказов для выбранных выборок и кодов времени.")
    else:
        matrix = [[0] * 24 for _ in range(7)]
        weekday_counts = [0] * 7
        hour_counts = [0] * 24
        for row in activity:
            dow, hour, count = row["order_dow"], row["order_hour_of_day"], row["order_count"]
            matrix[dow][hour] = count
            weekday_counts[dow] += count
            hour_counts[hour] += count
        left, right = st.columns(2)
        with left:
            heatmap = go.Figure(
                go.Heatmap(
                    z=matrix,
                    x=[f"{hour:02d}:00" for hour in range(24)],
                    y=[f"Код {dow}" for dow in range(7)],
                    colorscale=[[0, "#E8EEF4"], [0.45, PALETTE["blue"]], [1, PALETTE["navy"]]],
                    colorbar={"title": "Заказы"},
                    hovertemplate="%{y}, %{x}<br>Заказов: %{z:,}<extra></extra>",
                )
            )
            st.plotly_chart(
                style_figure(
                    heatmap, title="Заказы: код дня × час", x_title="Час", y_title="Код order_dow"
                ),
                use_container_width=True,
            )
        with right:
            day_tab, hour_tab = st.tabs(["По коду дня", "По часу"])
            with day_tab:
                day_chart = go.Figure(
                    go.Bar(
                        x=[f"Код {dow}" for dow in range(7)],
                        y=weekday_counts,
                        marker_color=PALETTE["teal"],
                        hovertemplate="%{x}<br>Заказов: %{y:,}<extra></extra>",
                    )
                )
                st.plotly_chart(
                    style_figure(
                        day_chart,
                        title="Распределение заказов по кодам дней",
                        x_title="Код order_dow",
                        y_title="Заказы",
                    ),
                    use_container_width=True,
                )
            with hour_tab:
                hour_chart = go.Figure(
                    go.Bar(
                        x=[f"{hour:02d}:00" for hour in range(24)],
                        y=hour_counts,
                        marker_color=PALETTE["gold"],
                        hovertemplate="%{x}<br>Заказов: %{y:,}<extra></extra>",
                    )
                )
                st.plotly_chart(
                    style_figure(
                        hour_chart,
                        title="Распределение заказов по часам",
                        x_title="Час",
                        y_title="Заказы",
                    ),
                    use_container_width=True,
                )

    st.subheader("Заказы с известным составом")
    known = data["known_summary"]
    if not known or known["known_order_count"] == 0:
        empty_state("Для выбранных фильтров нет заказов с известной корзиной (prior/train).")
        return
    cols = st.columns(4)
    with cols[0]:
        metric_card(
            "Известные заказы", known["known_order_count"], "Заказы prior/train после фильтров."
        )
    with cols[1]:
        metric_card(
            "Покупатели в срезе",
            known["buyer_count"],
            "Уникальные покупатели внутри выбранного среза.",
        )
    with cols[2]:
        metric_card("Позиции", known["item_count"], "Позиции известных корзин выбранного среза.")
    with cols[3]:
        metric_card(
            "Повторные позиции",
            (known["repeated_item_count"] / known["item_count"]) if known["item_count"] else None,
            "Доля повторных позиций по суммарному числителю и знаменателю выбранного среза.",
            percent=True,
        )

    left, right = st.columns(2)
    with left:
        rows = data["basket_counts"]
        basket_fig = go.Figure(
            go.Bar(
                x=[f"{row['basket_from']}–{row['basket_from'] + 4}" for row in rows],
                y=[row["order_count"] for row in rows],
                marker_color=PALETTE["blue"],
                hovertemplate="Корзина %{x} товаров<br>Заказов: %{y:,}<extra></extra>",
            )
        )
        st.plotly_chart(
            style_figure(
                basket_fig,
                title="Размер известной корзины",
                x_title="Товаров в корзине",
                y_title="Заказы",
            ),
            use_container_width=True,
        )
    with right:
        rows = data["intervals"]
        interval_fig = go.Figure(
            go.Bar(
                x=[row["interval_days"] for row in rows],
                y=[row["order_count"] for row in rows],
                marker_color=PALETTE["purple"],
                hovertemplate="Интервал: %{x} дней<br>Заказов: %{y:,}<extra></extra>",
            )
        )
        st.plotly_chart(
            style_figure(
                interval_fig,
                title="Интервалы между покупками",
                x_title="Дней с прошлого заказа",
                y_title="Заказы",
            ),
            use_container_width=True,
        )
    st.caption(
        "Часовая и дневная активность учитывает все заказы выбранных выборок. "
        "Размер корзины, интервалы и показатели состава рассчитываются только "
        "по известным корзинам; "
        "фильтр размера корзины действует на эту часть страницы."
    )
