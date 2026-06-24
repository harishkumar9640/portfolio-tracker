#!/usr/bin/env python3
"""
Same data as indices_chart.py, but rendered as an interactive HTML chart
(hover values, toggle lines on/off, zoom, range selector).

Run:
    python3 indices_html.py --period 6mo
"""
from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import yfinance as yf

from indices_chart import INDICES, fetch_indices, normalize_to_base100

PROJECT = Path(__file__).resolve().parent
CHARTS_DIR = PROJECT / "charts"
CHARTS_DIR.mkdir(exist_ok=True)


def cleanup_old_charts(period: str) -> None:
    """Remove every PNG/HTML for the given period so only the latest is kept."""
    for ext in ("png", "html"):
        for old in CHARTS_DIR.glob(f"indices_{period}.*"):
            try:
                old.unlink()
            except OSError:
                pass


def render_html(norm: pd.DataFrame, period: str) -> Path:
    fig = go.Figure()
    palette = ["#1f77b4", "#ff7f0e", "#2ca02c", "#d62728",
               "#9467bd", "#8c564b", "#e377c2", "#7f7f7f"]

    for i, col in enumerate(norm.columns):
        s = norm[col].dropna()
        if s.empty:
            continue
        fig.add_trace(go.Scatter(
            x=s.index, y=s.values,
            mode="lines",
            name=col,
            line=dict(color=palette[i % len(palette)], width=2),
            hovertemplate=f"<b>{col}</b><br>%{{x|%Y-%m-%d}}<br>Index: %{{y:.2f}}<extra></extra>",
        ))

    fig.add_hline(y=100, line_dash="dash", line_color="grey", opacity=0.6)

    fig.update_layout(
        title=dict(
            text=f"World Indices Comparison  —  base 100 = {norm.index[0].date()}",
            x=0.5, xanchor="center",
        ),
        xaxis_title="Date",
        yaxis_title="Indexed value (base 100)",
        hovermode="x unified",
        template="plotly_white",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0),
        height=620,
    )
    fig.update_xaxes(
        rangeslider=dict(visible=True, thickness=0.05),
        rangeselector=dict(
            buttons=[
                dict(count=1, label="1M", step="month", stepmode="backward"),
                dict(count=3, label="3M", step="month", stepmode="backward"),
                dict(count=6, label="6M", step="month", stepmode="backward"),
                dict(count=1, label="1Y", step="year", stepmode="backward"),
                dict(count=2, label="2Y", step="year", stepmode="backward"),
                dict(step="all", label="All"),
            ]
        ),
    )

    out = CHARTS_DIR / f"indices_{period}.html"
    fig.write_html(out, include_plotlyjs="cdn")  # small file; loads plotly from CDN
    return out


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--period", default="6mo",
                   choices=["1mo", "3mo", "6mo", "1y", "2y", "5y", "max"])
    args = p.parse_args()

    df = fetch_indices(args.period)
    norm = normalize_to_base100(df)
    cleanup_old_charts(args.period)
    out = render_html(norm, args.period)
    print(f"HTML chart saved: {out}")
    print(f"Open with:  open {out}")


if __name__ == "__main__":
    main()
