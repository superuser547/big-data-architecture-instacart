"""Category and product demand views."""

from __future__ import annotations

import plotly.graph_objects as go
import streamlit as st

from instacart.dashboard.charts import PALETTE, style_figure
from instacart.dashboard.components import empty_state, eval_set_filter, page_title
from instacart.dashboard.data import Readiness, load_category_data
from instacart.dashboard.metrics import format_number, format_percent, lineage_arguments, ratio


def _name_options(rows: list[dict], kind: str) -> dict[int, str]:
    return {
        int(row["group_id"]): str(row["group_name"]) for row in rows if row["group_type"] == kind
    }


def _category_filters(data: dict) -> tuple[tuple[int, ...], tuple[int, ...]]:
    department_names = _name_options(data["options"], "department")
    aisle_rows = [row for row in data["options"] if row["group_type"] == "aisle"]
    left, right = st.columns(2)
    with left:
        selected_departments = st.multiselect(
            "Подразделения",
            options=list(department_names),
            format_func=lambda value: department_names[value],
            key="category_departments",
        )
    selected_dept_ids = tuple(int(value) for value in selected_departments)
    allowed_aisles = [
        row
        for row in aisle_rows
        if not selected_dept_ids or int(row["department_id"]) in selected_dept_ids
    ]
    aisle_names = {int(row["group_id"]): str(row["group_name"]) for row in allowed_aisles}
    st.session_state["category_aisles"] = [
        value for value in st.session_state.get("category_aisles", []) if int(value) in aisle_names
    ]
    with right:
        selected_aisles = st.multiselect(
            "Товарные группы",
            options=list(aisle_names),
            format_func=lambda value: aisle_names[value],
            key="category_aisles",
            help="Список групп сужается выбранными подразделениями.",
        )
    selected_aisle_ids = tuple(int(value) for value in selected_aisles)
    return selected_dept_ids, selected_aisle_ids


def _popularity_chart(rows: list[dict], level: str, title: str) -> None:
    selected = sorted(
        (row for row in rows if row["group_type"] == level),
        key=lambda row: row["item_count"],
        reverse=True,
    )
    if not selected:
        empty_state("Нет строк для выбранных фильтров и уровня категории.")
        return
    chart_rows = selected[:25]
    names = [row["group_name"] for row in chart_rows][::-1]
    item_counts = [int(row["item_count"]) for row in chart_rows][::-1]
    rates = [ratio(row["reorder_count"], row["item_count"]) for row in chart_rows][::-1]
    figure = go.Figure()
    figure.add_bar(
        y=names,
        x=item_counts,
        orientation="h",
        name="Товарные позиции",
        marker_color=PALETTE["blue"],
        customdata=[[format_percent(rate)] for rate in rates],
        hovertemplate="%{y}<br>Позиции: %{x:,}<br>Повторные: %{customdata[0]}<extra></extra>",
    )
    figure.update_layout(yaxis={"categoryorder": "array", "categoryarray": names})
    st.plotly_chart(
        style_figure(
            figure,
            title=title,
            x_title="Позиции",
            y_title="",
            height=min(640, max(390, len(names) * 24)),
        ),
        use_container_width=True,
    )
    table_rows = [
        {
            "Категория": row["group_name"],
            "Позиции": row["item_count"],
            "Повторные позиции": row["reorder_count"],
            "Доля повторов": ratio(row["reorder_count"], row["item_count"]),
            "Заказы": row["order_count"],
        }
        for row in selected
    ]
    st.dataframe(
        table_rows,
        hide_index=True,
        column_config={"Доля повторов": st.column_config.NumberColumn(format="percent")},
        use_container_width=True,
    )


def _top_products_chart(rows: list[dict]) -> None:
    if not rows:
        empty_state("Нет товаров для построения Top-N по выбранным выборкам и категориям.")
        return
    visible = list(reversed(rows))
    figure = go.Figure(
        go.Bar(
            x=[int(row["item_count"]) for row in visible],
            y=[row["product_name"] for row in visible],
            orientation="h",
            marker_color=PALETTE["blue"],
            customdata=[
                [format_percent(ratio(row["reorder_count"], row["item_count"]))] for row in visible
            ],
            hovertemplate=(
                "%{y}<br>Товарные позиции: %{x:,}<br>Доля повторов: %{customdata[0]}<extra></extra>"
            ),
        )
    )
    st.plotly_chart(
        style_figure(
            figure,
            title="Top-N товаров по товарным позициям",
            x_title="Товарные позиции",
            y_title="",
            height=min(640, max(380, len(visible) * 26)),
        ),
        use_container_width=True,
    )


def render(readiness: Readiness, database_url: str) -> None:
    page_title(
        "Категории и товары",
        "Популярность и повторные позиции по опубликованным витринам категорий и товаров.",
    )
    left, right = st.columns([2, 1])
    with left:
        eval_sets = eval_set_filter(key="category_eval_sets")
    with right:
        category_level = st.selectbox(
            "Уровень детализации спроса",
            options=["department", "aisle"],
            format_func=lambda value: (
                "Подразделения" if value == "department" else "Товарные группы"
            ),
            key="category_level",
        )
    st.caption(
        "Выборки, подразделения и товарные группы применяются к популярности, товарам и "
        "структуре спроса. Время и размер корзины применяются только к структуре спроса. "
        "Top-N меняет только график популярных товаров; поиск ограничивает только таблицу."
    )
    time_cols = st.columns([1.3, 2, 2, 1])
    with time_cols[0]:
        weekdays = st.multiselect(
            "Коды order_dow",
            options=list(range(7)),
            default=list(range(7)),
            format_func=lambda code: f"Код {code}",
            key="category_dow",
            help="Числовые коды источника; это не названия календарных дней.",
        )
    with time_cols[1]:
        hours = st.multiselect(
            "Час заказа",
            options=list(range(24)),
            default=list(range(24)),
            format_func=lambda hour: f"{hour:02d}:00",
            key="category_hours",
        )
    with time_cols[2]:
        basket = st.slider(
            "Размер корзины, товаров",
            min_value=1,
            max_value=200,
            value=(1, 200),
            key="category_basket",
        )
    with time_cols[3]:
        top_n = st.select_slider("Top товаров", options=[10, 20, 50, 100], value=20)
    product_search = st.text_input(
        "Поиск товара по названию",
        key="category_product_search",
        help="Ищет по всей выборке товаров с применёнными выборками и фильтрами категорий.",
    )

    fingerprint, ingestion_id, marts_id, analysis_id = lineage_arguments(readiness)
    base_args = (
        database_url,
        fingerprint,
        ingestion_id,
        marts_id,
        analysis_id,
        eval_sets,
    )
    # The first cached read supplies the compact dimension options used by the filters.
    dimension_data = load_category_data(
        *base_args,
        (),
        (),
        category_level,
        tuple(weekdays),
        tuple(hours),
        int(basket[0]),
        int(basket[1]),
        int(top_n),
        product_search,
    )
    department_ids, aisle_ids = _category_filters(dimension_data)
    if department_ids or aisle_ids:
        data = load_category_data(
            *base_args,
            department_ids,
            aisle_ids,
            category_level,
            tuple(weekdays),
            tuple(hours),
            int(basket[0]),
            int(basket[1]),
            int(top_n),
            product_search,
        )
    else:
        data = dimension_data

    st.subheader("Популярность категорий")
    tab_dept, tab_aisle = st.tabs(["Подразделения", "Товарные группы"])
    with tab_dept:
        _popularity_chart(
            data["popularity"], "department", "Топ подразделений по товарным позициям"
        )
    with tab_aisle:
        _popularity_chart(data["popularity"], "aisle", "Топ товарных групп по товарным позициям")
    st.caption(
        "Доля повторов рассчитана как сумма повторных позиций / сумма всех позиций. "
        "Число покупателей не агрегируется между группами. При фильтре товарных групп "
        "объёмы подразделений пересчитываются по выбранным группам; число заказов для такого "
        "итога не показывается, поскольку заказы могут пересекаться."
    )

    st.subheader("Структура спроса и выбранная категория")
    rows = data["activity"]
    if not rows:
        empty_state("Нет товарных позиций для выбранных категории, времени и размера корзины.")
    else:
        total_items = sum(int(row["item_count"]) for row in rows)
        visible_rows = rows[:20]
        figure = go.Figure(
            go.Bar(
                y=[row["category_name"] for row in visible_rows][::-1],
                x=[row["item_count"] for row in visible_rows][::-1],
                orientation="h",
                marker_color=PALETTE["teal"],
                customdata=[
                    [format_percent(ratio(row["item_count"], total_items))] for row in visible_rows
                ][::-1],
                hovertemplate=(
                    "%{y}<br>Позиции: %{x:,}<br>Доля среза: %{customdata[0]}<extra></extra>"
                ),
            )
        )
        st.plotly_chart(
            style_figure(
                figure,
                title=(
                    f"Структура спроса: {category_level} · топ-20"
                    if len(rows) > 20
                    else f"Структура спроса: {category_level}"
                ),
                x_title="Позиции",
                y_title="Категория",
                height=min(640, max(390, len(visible_rows) * 24)),
            ),
            use_container_width=True,
        )
        st.dataframe(
            [
                {
                    "Категория": row["category_name"],
                    "Позиции": row["item_count"],
                    "Повторные позиции": row["reorder_count"],
                    "Доля повторов": ratio(row["reorder_count"], row["item_count"]),
                    "Доля спроса": ratio(row["item_count"], total_items),
                }
                for row in rows
            ],
            hide_index=True,
            column_config={
                "Доля повторов": st.column_config.NumberColumn(format="percent"),
                "Доля спроса": st.column_config.NumberColumn(format="percent"),
            },
            use_container_width=True,
        )
        category_names = {int(row["category_id"]): str(row["category_name"]) for row in rows}
        selected_id = st.selectbox(
            "Детализация категории",
            options=list(category_names),
            format_func=lambda value: category_names[value],
            key="category_detail",
        )
        selected = next(row for row in rows if int(row["category_id"]) == int(selected_id))
        cols = st.columns(4)
        with cols[0]:
            st.metric("Позиции", format_number(selected["item_count"]))
        with cols[1]:
            st.metric("Повторные позиции", format_number(selected["reorder_count"]))
        with cols[2]:
            st.metric(
                "Доля повторов",
                format_percent(ratio(selected["reorder_count"], selected["item_count"])),
            )
        with cols[3]:
            st.metric(
                "Доля спроса в срезе", format_percent(ratio(selected["item_count"], total_items))
            )
    st.caption(
        "Категорийный срез использует mart.category_activity: eval_set, код дня, час и размер "
        "известной корзины применены совместно. Количество покупателей здесь не суммируется."
    )

    st.subheader("Популярные товары")
    _top_products_chart(data["top_products"])
    products = data["products"]
    if not products:
        empty_state("Нет совпадений в поиске или товары недоступны для выбранных выборок.")
    else:
        product_table = [
            {
                "Товар": row["product_name"],
                "Позиции": row["item_count"],
                "Повторные позиции": row["reorder_count"],
                "Доля повторов": row["reorder_rate"],
                "Заказы": row["order_count"],
                "Товарная группа": row["aisle_name"],
                "Подразделение": row["department_name"],
            }
            for row in products
        ]
        st.dataframe(
            product_table,
            hide_index=True,
            column_config={"Доля повторов": st.column_config.NumberColumn(format="percent")},
            use_container_width=True,
        )
    st.caption(
        "Поиск выполняется по всей подходящей выборке, затем выводятся первые 500 совпадений "
        "по количеству позиций. График Top-N использует отдельный рейтинг."
    )
