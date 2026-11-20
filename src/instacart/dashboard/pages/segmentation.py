"""Published K-Means customer segmentation results."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import plotly.graph_objects as go
import streamlit as st

from instacart.dashboard.charts import COLORWAY, style_figure
from instacart.dashboard.components import FEATURE_LABELS, empty_state, page_title
from instacart.dashboard.data import Readiness, load_segments
from instacart.dashboard.metrics import format_number, format_percent, lineage_arguments


def _means(profile: Mapping[str, Any]) -> Mapping[str, float | None]:
    feature_means = profile.get("feature_means") or {}
    return feature_means.get("mean", {})


def _display_feature_value(feature: str, value: float | None) -> float | None:
    if value is None:
        return None
    return value * 100 if feature == "reorder_rate" else value


def _selection_rationale(value: str | None) -> str:
    translations = {
        "maximum sampled silhouette with every cluster >= 0.5% of customers or 20": (
            "Максимальный silhouette среди решений, где размер каждого сегмента "
            "не меньше max(20; 0,5% покупателей)."
        ),
        "maximum sampled silhouette; no candidate met the minimum cluster size": (
            "Минимальный размер сегмента не достигнут ни одним кандидатом; "
            "выбран максимум silhouette среди допустимых решений."
        ),
    }
    if value is None:
        return "Основание выбора не сохранено в журнале запуска."
    return translations.get(value, value)


def _profile_interpretation(profiles: list[dict[str, Any]]) -> str:
    if len(profiles) < 2:
        return "Нужно не менее двух сегментов, чтобы сравнить профили."
    findings: list[tuple[float, str]] = []
    for feature, (label, unit) in FEATURE_LABELS.items():
        values = [(int(row["cluster_id"]), _means(row).get(feature)) for row in profiles]
        values = [(cluster, float(value)) for cluster, value in values if value is not None]
        if len(values) < 2:
            continue
        low_cluster, low = min(values, key=lambda item: item[1])
        high_cluster, high = max(values, key=lambda item: item[1])
        span = high - low
        relative_span = span / max(abs(high), abs(low), 1e-9)
        if span > 0:
            findings.append(
                (
                    relative_span,
                    f"{label}: максимум у кластера {high_cluster} "
                    f"({format_number(_display_feature_value(feature, high), 2)} {unit}), "
                    f"минимум у кластера {low_cluster} "
                    f"({format_number(_display_feature_value(feature, low), 2)} {unit})",
                )
            )
    if not findings:
        return "Опубликованные средние профили сегментов не различаются по отображаемым признакам."
    strongest = sorted(findings, reverse=True)[:3]
    return "Наиболее заметные различия средних: " + "; ".join(text for _, text in strongest) + "."


def render(readiness: Readiness, database_url: str) -> None:
    page_title(
        "Сегментация покупателей",
        "Опубликованные результаты K-Means и PCA для признаков покупателей по prior-заказам.",
    )
    fingerprint, ingestion_id, marts_id, analysis_id = lineage_arguments(readiness)
    data = load_segments(
        database_url,
        fingerprint,
        ingestion_id,
        marts_id,
        analysis_id,
    )
    profiles = data["profiles"]
    if not profiles:
        empty_state("В текущей публикации нет профилей кластеров.")
        return

    selected_k = data["selected_k"]
    cluster_count = data["population_size"]
    cols = st.columns(4)
    with cols[0]:
        st.metric("Покупатели", format_number(cluster_count))
    with cols[1]:
        st.metric("Выбранное число кластеров", format_number(selected_k))
    with cols[2]:
        st.metric("Сегментов", format_number(len(profiles)))
    with cols[3]:
        st.metric("Точек PCA", format_number(data["displayed_sample_size"]))
    st.caption(
        "k выбрано по максимуму silhouette среди допустимых решений с ограничением на минимальный "
        "размер сегмента. Основание публикации: "
        f"{_selection_rationale(data['selection_basis'])}"
    )

    for start in range(0, len(profiles), 4):
        batch = profiles[start : start + 4]
        size_columns = st.columns(len(batch))
        for column, profile in zip(size_columns, batch, strict=True):
            cluster_id = int(profile["cluster_id"])
            share = profile["customer_count"] / cluster_count if cluster_count else None
            with column:
                st.metric(
                    f"Кластер {cluster_id} · {format_percent(share)}",
                    format_number(profile["customer_count"]),
                )

    st.subheader("Профили сегментов")
    st.caption(
        "Средние показаны в исходных единицах признаков. Профили и доли рассчитаны "
        "по полной совокупности."
    )
    feature_cols = st.columns(2)
    for index, (feature, (label, unit)) in enumerate(FEATURE_LABELS.items()):
        rows = [
            (
                int(row["cluster_id"]),
                _display_feature_value(feature, _means(row).get(feature)),
            )
            for row in profiles
        ]
        rows = [(cluster, value) for cluster, value in rows if value is not None]
        figure = go.Figure(
            go.Bar(
                x=[f"Кластер {cluster}" for cluster, _ in rows],
                y=[value for _, value in rows],
                marker_color=[COLORWAY[cluster % len(COLORWAY)] for cluster, _ in rows],
                hovertemplate=f"%{{x}}<br>{label}: %{{y:.2f}} {unit}<extra></extra>",
            )
        )
        with feature_cols[index % 2]:
            st.plotly_chart(
                style_figure(figure, title=label, x_title="", y_title=unit, height=300),
                use_container_width=True,
            )
    st.info(_profile_interpretation(profiles))

    st.subheader("PCA покупателей")
    if not data["points"]:
        empty_state("Не удалось получить детерминированную выборку точек PCA.")
    else:
        pca_figure = go.Figure()
        for profile in profiles:
            cluster_id = int(profile["cluster_id"])
            points = [point for point in data["points"] if int(point["cluster_id"]) == cluster_id]
            if not points:
                continue
            pca_figure.add_trace(
                go.Scattergl(
                    x=[point["pca_1"] for point in points],
                    y=[point["pca_2"] for point in points],
                    mode="markers",
                    name=f"Кластер {cluster_id}",
                    marker={
                        "size": 5,
                        "opacity": 0.62,
                        "color": COLORWAY[cluster_id % len(COLORWAY)],
                    },
                    hovertemplate=(
                        f"Кластер {cluster_id}<br>PC1: %{{x:.3f}}<br>PC2: %{{y:.3f}}<extra></extra>"
                    ),
                )
            )
        st.plotly_chart(
            style_figure(
                pca_figure,
                title="Проекция признаков покупателей на первые две компоненты",
                x_title="PC1",
                y_title="PC2",
                height=540,
            ),
            use_container_width=True,
        )
        variance = data["pca_explained_variance_ratio"]
        variance_note = ""
        if variance:
            variance_note = (
                f" Доли объяснённой дисперсии: PC1 {format_percent(variance[0])}, "
                f"PC2 {format_percent(variance[1])}."
            )
        st.caption(
            f"Показана детерминированная выборка из {format_number(data['displayed_sample_size'])} "
            f"из {format_number(cluster_count)} покупателей; представлены все сегменты."
            f"{variance_note}"
        )

    st.subheader("Метрики исследованных k")
    metrics = data["metrics"]
    if not metrics:
        empty_state("Нет опубликованных метрик для исследованных значений k.")
        return
    left, right = st.columns(2)
    with left:
        silhouette = go.Figure(
            go.Scatter(
                x=[row["k"] for row in metrics],
                y=[row["silhouette"] for row in metrics],
                mode="lines+markers",
                marker={
                    "size": 9,
                    "color": ["#D96C5F" if row["selected"] else "#6096BA" for row in metrics],
                },
                customdata=[
                    ["Выбрано" if row["selected"] else "Кандидат", row["silhouette_sample_size"]]
                    for row in metrics
                ],
                hovertemplate=(
                    "k=%{x}<br>Silhouette: %{y:.4f}<br>%{customdata[0]}"
                    "<br>Выборка: %{customdata[1]:,}<extra></extra>"
                ),
            )
        )
        st.plotly_chart(
            style_figure(
                silhouette,
                title="Silhouette по исследованным k",
                x_title="Число кластеров k",
                y_title="Silhouette",
            ),
            use_container_width=True,
        )
    with right:
        inertia = go.Figure(
            go.Scatter(
                x=[row["k"] for row in metrics],
                y=[row["inertia"] for row in metrics],
                mode="lines+markers",
                marker={
                    "size": 9,
                    "color": ["#D96C5F" if row["selected"] else "#6096BA" for row in metrics],
                },
                customdata=[["Выбрано" if row["selected"] else "Кандидат"] for row in metrics],
                hovertemplate="k=%{x}<br>Inertia: %{y:,.2f}<br>%{customdata[0]}<extra></extra>",
            )
        )
        st.plotly_chart(
            style_figure(
                inertia,
                title="Inertia по исследованным k",
                x_title="Число кластеров k",
                y_title="Сумма квадратов расстояний",
            ),
            use_container_width=True,
        )

    table_rows = []
    for profile in profiles:
        means = _means(profile)
        row: dict[str, Any] = {
            "Сегмент": int(profile["cluster_id"]),
            "Покупатели": int(profile["customer_count"]),
            "Доля покупателей": (
                profile["customer_count"] / cluster_count if cluster_count else None
            ),
        }
        for feature, (label, unit) in FEATURE_LABELS.items():
            row[f"{label}, {unit}"] = _display_feature_value(feature, means.get(feature))
        table_rows.append(row)
    st.dataframe(
        table_rows,
        hide_index=True,
        column_config={"Доля покупателей": st.column_config.NumberColumn(format="percent")},
        use_container_width=True,
    )
