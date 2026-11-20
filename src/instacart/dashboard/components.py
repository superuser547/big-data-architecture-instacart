"""Reusable Streamlit components and filter controls."""

from __future__ import annotations

from typing import Any

import streamlit as st

from instacart.dashboard.metrics import format_number, format_percent

EVAL_SET_LABELS = {"prior": "Prior", "train": "Train", "test": "Test"}
FEATURE_LABELS = {
    "prior_order_count": ("Заказы prior", "заказов"),
    "avg_basket_size": ("Средний размер корзины", "товаров/заказ"),
    "reorder_rate": ("Доля повторных позиций", "%"),
    "avg_days_between_orders": ("Интервал между заказами", "дней"),
    "unique_product_count": ("Уникальные товары", "товаров"),
    "unique_aisle_count": ("Разнообразие товарных групп", "групп"),
    "unique_department_count": ("Разнообразие подразделений", "подразделений"),
}


def page_title(title: str, description: str) -> None:
    st.title(title)
    st.caption(description)


def metric_card(label: str, value: Any, help_text: str, *, percent: bool = False) -> None:
    rendered = (
        format_percent(value)
        if percent
        else format_number(value, 1 if isinstance(value, float) else 0)
    )
    st.metric(label, rendered, help=help_text)


def eval_set_filter(
    *, key: str, default: tuple[str, ...] = ("prior", "train", "test")
) -> tuple[str, ...]:
    labels = st.multiselect(
        "Выборки заказов",
        options=list(EVAL_SET_LABELS),
        default=list(default),
        format_func=lambda value: EVAL_SET_LABELS[value],
        key=key,
        help="Выборки исходного набора: prior, train и test.",
    )
    return tuple(labels)


def activity_filters(prefix: str) -> tuple[tuple[int, ...], tuple[int, ...], tuple[int, int]]:
    left, middle, right = st.columns([1.3, 2, 2])
    with left:
        weekdays = st.multiselect(
            "Коды order_dow",
            options=list(range(7)),
            default=list(range(7)),
            format_func=lambda code: f"Код {code}",
            key=f"{prefix}_dow",
            help=(
                "Показываются исходные числовые коды 0–6. Названия календарных дней "
                "не подтверждены."
            ),
        )
    with middle:
        hours = st.multiselect(
            "Час заказа",
            options=list(range(24)),
            default=list(range(24)),
            format_func=lambda hour: f"{hour:02d}:00",
            key=f"{prefix}_hours",
        )
    with right:
        basket = st.slider(
            "Размер известной корзины, товаров",
            min_value=1,
            max_value=200,
            value=(1, 200),
            key=f"{prefix}_basket",
            help="Применяется к графикам на уровне заказов/позиций с известным составом.",
        )
    return tuple(weekdays), tuple(hours), (int(basket[0]), int(basket[1]))


def empty_state(message: str) -> None:
    st.info(message)


def render_result_lineage(readiness: Any) -> None:
    st.caption(
        "Опубликованный срез: "
        f"fingerprint `{readiness.dataset_fingerprint}` · "
        f"import `{readiness.ingestion_run_id}` · "
        f"витрины `{readiness.marts_run_id}` · "
        f"анализ `{readiness.analysis_run_id}`"
    )
