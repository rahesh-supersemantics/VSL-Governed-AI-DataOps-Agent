"""Plotly figures built from view-model data. They return ``None`` for empty data so the UI can show an empty state."""

from __future__ import annotations

import plotly.graph_objects as go

SEVERITY_COLOURS = {"critical": "#C94A4A", "high": "#D9763A", "medium": "#D49B32", "low": "#3977D5"}
NEUTRAL = "#5B6B85"
_FONT = {"family": "Segoe UI, system-ui, sans-serif", "color": "#1B2A3D", "size": 13}


def _style(fig: go.Figure) -> go.Figure:
    """Shared look: white plot on the light workspace, light gridlines, no legend chrome. Data labels stay on the bars so
    values never depend on colour or hover."""
    fig.update_layout(paper_bgcolor="#FFFFFF", plot_bgcolor="#FFFFFF", font=_FONT, title_font={"size": 15})
    fig.update_xaxes(gridcolor="#E3E8F0", linecolor="#C9D3E1")
    fig.update_yaxes(gridcolor="#E3E8F0", linecolor="#C9D3E1")
    return fig


def _bar(pairs: list[tuple[str, int]], title: str, colours: dict[str, str] | None = None, horizontal: bool = False) -> go.Figure | None:
    if not pairs:
        return None
    labels, values = [p[0] for p in pairs], [p[1] for p in pairs]
    marker = {"color": [colours.get(k, NEUTRAL) for k in labels]} if colours else {}
    fig = go.Figure(go.Bar(x=values if horizontal else labels, y=labels if horizontal else values,
                           orientation="h" if horizontal else "v", marker=marker, text=values, textposition="auto"))
    fig.update_layout(title=title, margin={"l": 10, "r": 10, "t": 40, "b": 10}, height=320, showlegend=False)
    if horizontal:
        fig.update_yaxes(autorange="reversed")
    return _style(fig)


def severity_chart(pairs: list[tuple[str, int]]) -> go.Figure | None:
    return _bar(pairs, "Incidents by severity", SEVERITY_COLOURS)


def workflow_chart(pairs: list[tuple[str, int]]) -> go.Figure | None:
    return _bar(pairs, "Incidents by workflow status", horizontal=True)


def timeline_chart(pairs: list[tuple[str, int]]) -> go.Figure | None:
    if not pairs:
        return None
    fig = go.Figure(go.Bar(x=[p[0] for p in pairs], y=[p[1] for p in pairs], marker={"color": NEUTRAL}))
    fig.update_layout(title="Incidents created per day (loaded incidents only)", margin={"l": 10, "r": 10, "t": 40, "b": 10},
                      height=320, xaxis_title="Day", yaxis_title="Incidents")
    return _style(fig)
