#!/usr/bin/env python3
"""
Interactive HTML chart comparing your portfolio (equity + MF + SGB) against
8 world indices.

Your portfolio line is rebased to 100 at the start of the requested period,
so you can see at a glance whether you beat the indices.

Run:
    python3 indices_html.py --period 6mo
    python3 indices_html.py --period 1y --include mf sgb
"""
# TABLE OF CONTENTS (read this first)
#
# This file has 3 major sections (227 lines total):
#
# 1. Portfolio value history (rebuilt from snapshots) ----------
# 2. Plot ----------
# 3. Main ----------

from __future__ import annotations

import argparse
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go

from .angel_client import fetch_holdings
from .history_db import HistoryDB
from .indices_chart import (
    INDICES,
    fetch_indices,
    normalize_to_base100,
)
from .logging_setup import get_logger
from .mf_sgb import fetch_mf_rows, fetch_sgb_rows, load_mfs, load_sgbs
from .parallel import fetch_all

log = get_logger("portfolio_html")

from pipeline.runtime_paths import data_root

PROJECT = Path(__file__).resolve().parent.parent
CHARTS_DIR = data_root() / "charts"
CHARTS_DIR.mkdir(exist_ok=True)


def cleanup_old_charts(period: str) -> None:
    """Remove every HTML for the given period so only the latest is kept."""
    for old in CHARTS_DIR.glob(f"portfolio_compare_{period}.*"):
        try:
            old.unlink()
        except OSError:
            pass


# ---------- Portfolio value history (rebuilt from snapshots) ----------
def portfolio_value_series(
    days: int,
    include: tuple[str, ...] = ("equity", "mf", "sgb"),
) -> pd.Series | None:
    """
    Build a per-day time series of total portfolio value, in ₹.

    Strategy: pull all ``portfolio_snapshot`` rows from SQLite for the
    requested window, then for any missing dates we keep the last known
    value flat-forward (portfolio value changes only on trading days).

    Returns ``None`` if no snapshots are available — caller should handle
    that by hiding the portfolio trace.
    """
    db = HistoryDB()
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    rows: list[dict] = []
    for kind in include:
        rows.extend(db.portfolio_history(kind=kind, days=days))
    if not rows:
        return None
    df = pd.DataFrame(rows)
    if df.empty or "kind" not in df.columns:
        return None
    # Sum equity + mf + sgb per date
    pivot = df.pivot_table(
        index="date", columns="kind", values="value", aggfunc="sum"
    ).sort_index()
    # Ensure all requested kinds are present as columns
    for k in include:
        if k not in pivot.columns:
            pivot[k] = 0.0
    pivot = pivot[list(include)].ffill().fillna(0)
    total = pivot.sum(axis=1)
    total.index = pd.to_datetime(total.index)
    return total[total.index >= cutoff]


# ---------- Plot ----------
PALETTE = [
    "#1f77b4", "#ff7f0e", "#2ca02c", "#d62728",
    "#9467bd", "#8c564b", "#e377c2", "#7f7f7f",
    "#bcbd22", "#17becf",
]


def render_html(
    norm: pd.DataFrame,
    period: str,
    portfolio: pd.Series | None,
    include: tuple[str, ...],
) -> Path:
    fig = go.Figure()

    for i, col in enumerate(norm.columns):
        s = norm[col].dropna()
        if s.empty:
            continue
        fig.add_trace(go.Scatter(
            x=s.index, y=s.values,
            mode="lines",
            name=col,
            line=dict(color=PALETTE[i % len(PALETTE)], width=2),
            hovertemplate=f"<b>{col}</b><br>%{{x|%Y-%m-%d}}<br>"
                          f"Index: %{{y:.2f}}<extra></extra>",
        ))

    # Portfolio overlay (rebased to 100 at the chart's first day)
    if portfolio is not None and len(portfolio) >= 2:
        # Re-base to match the indices' first day
        first_day = norm.dropna(how="all").index.min()
        port_aligned = portfolio[portfolio.index >= first_day]
        if len(port_aligned) >= 1:
            base = float(port_aligned.iloc[0])
            if base > 0:
                port_rebased = port_aligned / base * 100.0
                # Portfolio gets a thicker black line so it stands out
                fig.add_trace(go.Scatter(
                    x=port_rebased.index, y=port_rebased.values,
                    mode="lines",
                    name=f"My Portfolio ({', '.join(include)})",
                    line=dict(color="#000000", width=3.5),
                    hovertemplate="<b>My Portfolio</b><br>"
                                  "%{x|%Y-%m-%d}<br>"
                                  "Index: %{y:.2f}<extra></extra>",
                ))

    fig.add_hline(y=100, line_dash="dash", line_color="grey", opacity=0.6)

    # Title shows what the user picked
    title_text = (
        f"My Portfolio vs World Indices  —  base 100 = "
        f"{norm.dropna(how='all').index[0].date()}"
    )
    if portfolio is None:
        title_text += "<br><sup>Run equity_compare.py to record snapshots — "
        title_text += "portfolio line will appear once you have history.</sup>"

    fig.update_layout(
        title=dict(text=title_text, x=0.5, xanchor="center"),
        xaxis_title="Date",
        yaxis_title="Indexed value (base 100)",
        hovermode="x unified",
        template="plotly_white",
        legend=dict(orientation="h", yanchor="bottom", y=1.02,
                    xanchor="left", x=0),
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

    out = CHARTS_DIR / f"portfolio_compare_{period}.html"
    fig.write_html(out, include_plotlyjs="cdn")
    return out


# ---------- Main ----------
def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--period", default="6mo",
                   choices=["1mo", "3mo", "6mo", "1y", "2y", "5y", "max"])
    p.add_argument(
        "--include", nargs="+",
        choices=["equity", "mf", "sgb"],
        default=["equity", "mf", "sgb"],
        help="Which asset classes to include in the portfolio overlay.",
    )
    p.add_argument(
        "--no-portfolio",
        action="store_true",
        help="Skip the portfolio overlay; render indices only.",
    )
    args = p.parse_args()
    include = tuple(args.include)

    # Pull indices (cached) and portfolio history in parallel.
    def _fetch_idx():
        return fetch_indices(args.period)

    def _fetch_port():
        if args.no_portfolio:
            return None
        # days must be at least as long as the longest period we support
        days_map = {"1mo": 30, "3mo": 90, "6mo": 180, "1y": 365,
                    "2y": 730, "5y": 1825, "max": 365 * 10}
        days = days_map[args.period]
        return portfolio_value_series(days=days, include=include)

    log.info("loading indices + portfolio history (parallel)")
    results = fetch_all({"idx": _fetch_idx, "port": _fetch_port})
    raw = results["idx"]
    portfolio = results["port"]

    if raw.empty:
        log.error("no index data; aborting")
        return

    norm = normalize_to_base100(raw)
    cleanup_old_charts(args.period)
    out = render_html(norm, args.period, portfolio, include)
    if portfolio is not None and len(portfolio) >= 2:
        log.info("portfolio overlay: %d daily snapshots", len(portfolio))
    log.info("HTML chart saved: %s", out)
    print(f"Open with:  open {out}")


if __name__ == "__main__":
    main()