"""Shared Plotly styling and chart helpers."""

from __future__ import annotations

from typing import Any

import plotly.graph_objects as go

PALETTE = {
    "navy": "#274C77",
    "blue": "#6096BA",
    "teal": "#3A7D78",
    "gold": "#E0A458",
    "coral": "#D96C5F",
    "purple": "#8075A8",
    "slate": "#8291A5",
    "light": "#DDE5ED",
    "green": "#6E9B72",
}
COLORWAY = [
    PALETTE["navy"],
    PALETTE["teal"],
    PALETTE["gold"],
    PALETTE["coral"],
    PALETTE["purple"],
    PALETTE["blue"],
    PALETTE["slate"],
    PALETTE["green"],
]


def style_figure(
    figure: go.Figure,
    *,
    title: str | None = None,
    x_title: str | None = None,
    y_title: str | None = None,
    height: int = 390,
) -> go.Figure:
    figure.update_layout(
        title={"text": title, "x": 0.02, "xanchor": "left"} if title else None,
        template="plotly_white",
        colorway=COLORWAY,
        font={"family": "Arial, sans-serif", "size": 13, "color": "#263442"},
        margin={"l": 16, "r": 16, "t": 56 if title else 28, "b": 20},
        height=height,
        legend={"orientation": "h", "y": 1.08, "x": 1, "xanchor": "right"},
        hoverlabel={"bgcolor": "white", "font_size": 13},
    )
    if x_title is not None:
        figure.update_xaxes(title_text=x_title, automargin=True)
    if y_title is not None:
        figure.update_yaxes(title_text=y_title, automargin=True)
    return figure


def metric_figures(points: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert dictionary rows to plain records for Plotly and Streamlit."""
    return [dict(point) for point in points]
