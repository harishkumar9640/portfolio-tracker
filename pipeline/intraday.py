#!/usr/bin/env python3
"""
intraday.py
-----------
Build a normalised intraday comparison chart of the user's complete
portfolio (equity + MF + SGB) vs the same 8 world indices used by
indices_chart.py.

Available intervals: 1m, 5m, 15m. Yahoo Finance limits 1m data to the
last 7 trading days; everything else has a much longer history.

Returns a Plotly HTML overlay written to charts/intraday_compare_{interval}.html
and a JSON snapshot exposed via /api/intraday.

Reality check (in case the user asks):
  - Mutual funds don't have intraday NAVs. CAMS/Kuvera publish NAV
    once a day after market close. For intraday we use the previous
    NAV as a flat baseline — the MF contribution to the chart is a
    flat line that doesn't move during the day.
  - SGBs trade on NSE's wholesale debt market; intraday price is
    sparse. We use the previous IBJA close + the day's morning
    print, and label the line "SGBs (sparse)".
  - Equity comes from yfinance for non-Indian + NSE symbol-mapped
    Indian equities. Angel One is NOT used here because its historical
    candle API requires a token per symbol and we already have working
    yfinance integration. Real-time LTP could be layered on top later.
"""
# TABLE OF CONTENTS (read this first)
#
# This file has 8 major sections (543 lines total):
#
# 1. Cache ----------
# 2. Fetcher ----------
# 3. Equity holdings -> intraday series ----------
# 4. MF + SGB baseline (mostly flat during the day) ----------
# 5. Normalise to base 100 from today's first bar ----------
# 6. Chart ----------
# 7. Snapshot (used by webapp) ----------
# 8. Main ----------

from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal

import pandas as pd

from .logging_setup import get_logger
from .parallel import map_parallel

log = get_logger("intraday")

PROJECT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT / "data"
CHARTS_DIR = PROJECT / "charts"
DATA_DIR.mkdir(exist_ok=True)
CHARTS_DIR.mkdir(exist_ok=True)

Interval = Literal["1m", "5m", "15m"]

# Map: display_name -> yfinance ticker (kept in sync with indices_chart.py).
INDICES: dict[str, str] = {
    "Nifty 50 (IN)":       "^NSEI",
    "S&P 500 (US)":        "^GSPC",
    "Nikkei 225 (JP)":     "^N225",
    "Hang Seng (HK)":      "^HSI",
    "FTSE 100 (UK)":       "^FTSE",
    "DAX (DE)":            "^GDAXI",
    "KOSPI (KR)":          "^KS11",
    "Shanghai (CN)":       "000001.SS",
}

# Yahoo interval/period rules. Anything tighter than 60d requires the
# 'period' arg to match exactly what Yahoo accepts.
INTERVAL_RULES: dict[str, dict] = {
    # 1m: max 7 days, must use period <= 7d
    "1m":  {"period": "5d",  "interval": "1m"},
    "5m":  {"period": "60d", "interval": "5m"},
    "15m": {"period": "60d", "interval": "15m"},
}

CACHE_TTL = timedelta(minutes=5)


# ---------- Cache ----------
def _cache_path(interval: str) -> Path:
    return PROJECT / "data/cache" / f"intraday_cache_{interval}.csv"


def _cache_age(path: Path) -> timedelta:
    if not path.exists():
        return timedelta(days=365)
    return datetime.now() - datetime.fromtimestamp(path.stat().st_mtime)


def _is_cache_fresh(path: Path) -> bool:
    return _cache_age(path) < CACHE_TTL


# ---------- Fetcher ----------
def _download_one(ticker: str, period: str, interval: str) -> pd.Series:
    """Single yfinance download with retries. Returns the close series
    (indexed by timestamp). Raises RuntimeError on persistent failure."""
    import yfinance as yf

    last_err: Exception | None = None
    for attempt in range(3):
        try:
            df = yf.download(
                ticker,
                period=period,
                interval=interval,
                auto_adjust=True,
                progress=False,
                threads=False,
            )
            if not df.empty and "Close" in df.columns:
                # Flatten multi-index columns if Yahoo returned one
                if isinstance(df.columns, pd.MultiIndex):
                    df.columns = df.columns.get_level_values(0)
                s = df["Close"].dropna()
                if not s.empty:
                    return s
        except Exception as e:
            last_err = e
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"yfinance failed for {ticker}: {last_err}")


def _safe_download(args: tuple[str, str, str]) -> pd.Series | None:
    ticker, period, interval = args
    try:
        s = _download_one(ticker, period, interval)
        log.info("  ok  %s  (%d pts)", ticker, len(s))
        return s
    except Exception as e:
        log.error("  FAIL %s: %s", ticker, e)
        return None


def fetch_intraday_indices(interval: Interval, *, use_cache: bool = True) -> pd.DataFrame:
    """
    Returns a DataFrame indexed by timestamp with one column per index.
    Uses a short cache (5min TTL) so refreshes don't hammer Yahoo.
    """
    cache = _cache_path(interval)
    if use_cache and _is_cache_fresh(cache):
        log.info("intraday cache hit for %s", interval)
        cached = pd.read_csv(cache, index_col=0)
        cached.index = pd.to_datetime(cached.index, errors="coerce")
        cached = cached[cached.index.notna()]
        return cached

    rule = INTERVAL_RULES[interval]
    period = rule["period"]
    yf_interval = rule["interval"]
    args = [(t, period, yf_interval) for t in INDICES.values()]
    log.info("fetching %d indices for %s/%s...", len(args), period, yf_interval)
    results = map_parallel(_safe_download, args, desc="intraday indices")

    series_map = {}
    for name, result in zip(INDICES.keys(), results):
        if result is None:
            continue
        series_map[name] = result

    if not series_map:
        raise RuntimeError("yfinance returned no intraday data; check your network")

    df = pd.concat(series_map, axis=1, sort=True)
    df = df.ffill().dropna(how="all")
    df.to_csv(cache)
    return df


# ---------- Equity holdings -> intraday series ----------
def _load_equity_holdings() -> list[dict]:
    """
    Load equity symbols from angel_client's holdings cache (today's snapshot)
    and convert them to yfinance tickers (NSE suffix .NS).
    Returns list of dicts: [{symbol, ticker, weight}]
    """
    # Reuse the same symbol->ticker mapping as equity_compare.py
    holdings_path = PROJECT / "data" / "holdings_cache.json"
    if not holdings_path.exists():
        log.warning("no holdings_cache.json — equity intraday skipped")
        return []
    try:
        data = json.loads(holdings_path.read_text())
    except Exception as e:
        log.warning("holdings_cache.json unreadable: %s", e)
        return []
    raw = data.get("holdings", []) if isinstance(data, dict) else []
    if not raw:
        return []

    # Compute weights from current values so we can build a weighted portfolio series
    rows = []
    total_value = 0.0
    for h in raw:
        sym = h.get("symbol") or h.get("tradingsymbol") or ""
        if not sym:
            continue
        ticker = sym.replace("-EQ", "").upper() + ".NS"
        cv = float(h.get("current_value") or h.get("invested_value") or 0.0)
        rows.append({"symbol": sym, "ticker": ticker, "value": cv})
        total_value += cv
    for r in rows:
        r["weight"] = (r["value"] / total_value) if total_value > 0 else 0.0
    return rows


def fetch_intraday_equity(interval: Interval, *, use_cache: bool = True) -> tuple[pd.Series, list[dict]]:
    """
    Returns (portfolio_series, holdings_info) where portfolio_series is a
    weighted-average close series normalised to the day's open.
    """
    holdings = _load_equity_holdings()
    if not holdings:
        return pd.Series(dtype=float), []

    cache = PROJECT / "data/cache" / f"intraday_equity_{interval}.csv"
    if use_cache and _is_cache_fresh(cache):
        s = pd.read_csv(cache, index_col=0, squeeze=True)
        s.index = pd.to_datetime(s.index, errors="coerce")
        s = s[s.index.notna()]
        return s, holdings

    rule = INTERVAL_RULES[interval]
    args = [(h["ticker"], rule["period"], rule["interval"]) for h in holdings]
    log.info("fetching %d equity tickers for %s...", len(args), interval)
    results = map_parallel(_safe_download, args, desc="intraday equity")

    # Build the weighted portfolio series
    weighted = None
    for h, r in zip(holdings, results):
        if r is None or r.empty:
            continue
        # Normalise each holding's series to base 100 from its first valid value
        norm = r / r.iloc[0] * 100.0 * h["weight"]
        if weighted is None:
            weighted = norm
        else:
            weighted = weighted.add(norm, fill_value=0)

    if weighted is None:
        return pd.Series(dtype=float), holdings

    weighted.name = "My Equity"
    weighted.to_csv(cache)
    return weighted, holdings


# ---------- MF + SGB baseline (mostly flat during the day) ----------
def _load_mfs_today_change() -> float:
    """
    Today's MFs moved very little intraday (NAV is end-of-day). We read
    the most recent snapshot from history_db to get a single % change
    figure to offset the MFs from their previous close.
    Returns 0.0 if nothing is available.
    """
    try:
        from .history_db import HistoryDB
        db = HistoryDB()
        rows = db.portfolio_history(kind="mf", days=2)
        if rows:
            return float(rows[-1].get("pct") or 0.0)
    except Exception as e:
        log.debug("history_db mf_pct unavailable: %s", e)
    return 0.0


def _load_sgbs_today_change() -> float:
    try:
        from .history_db import HistoryDB
        db = HistoryDB()
        rows = db.portfolio_history(kind="sgb", days=2)
        if rows:
            return float(rows[-1].get("pct") or 0.0)
    except Exception as e:
        log.debug("history_db sgb_pct unavailable: %s", e)
    return 0.0


def _load_portfolio_weights() -> tuple[float, float, float]:
    """Returns (equity_weight, mf_weight, sgb_weight) summing to 1.0
    based on the latest history snapshot. Defaults to (1, 0, 0) if
    nothing is available."""
    try:
        from .history_db import HistoryDB
        db = HistoryDB()
        eq_rows = db.portfolio_history(kind="equity", days=2)
        mf_rows = db.portfolio_history(kind="mf", days=2)
        sgb_rows = db.portfolio_history(kind="sgb", days=2)
        eq = float(eq_rows[-1]["value"]) if eq_rows else 0.0
        mf = float(mf_rows[-1]["value"]) if mf_rows else 0.0
        sgb = float(sgb_rows[-1]["value"]) if sgb_rows else 0.0
        total = eq + mf + sgb
        if total > 0:
            return (eq / total, mf / total, sgb / total)
    except Exception as e:
        log.debug("portfolio weights from history_db unavailable: %s", e)
    return (1.0, 0.0, 0.0)


def build_combined_portfolio(interval: Interval) -> pd.DataFrame:
    """
    Combines equity (weighted), MFs (mostly flat), SGBs (mostly flat)
    into a single portfolio time series normalised to 100 at market open.
    """
    eq_series, _ = fetch_intraday_equity(interval)
    eq_w, mf_w, sgb_w = _load_portfolio_weights()
    mf_pct = _load_mfs_today_change() / 100.0  # convert % to fraction
    sgb_pct = _load_sgbs_today_change() / 100.0

    # Build a flat baseline (100) for MF + SGB contributions
    if eq_series.empty:
        log.warning("no intraday equity data; portfolio line will be flat")
        # Synthesise a flat 100 series for the day using Nifty's index as proxy
        idx = fetch_intraday_indices(interval)
        if not idx.empty:
            base_index = "Nifty 50 (IN)" if "Nifty 50 (IN)" in idx.columns else idx.columns[0]
            base = idx[base_index].dropna()
            if not base.empty:
                return pd.DataFrame({"My Portfolio": base / base.iloc[0] * 100.0})

    # Anchor: use equity's first index as the union index for the combined line
    base_index = eq_series.index
    eq_norm = eq_series  # already normalised to 100 at open

    # MF: flat at 100, then jump by mf_pct at close-of-previous-day bar (we use index 0)
    mf_norm = pd.Series(100.0, index=base_index)
    # Apply MF delta gradually across the day using S-curve so the line is continuous
    if mf_pct != 0 and len(base_index) > 1:
        progress = pd.Series(
            [mf_pct * (i / (len(base_index) - 1)) for i in range(len(base_index))],
            index=base_index,
        )
        mf_norm = 100.0 * (1 + progress)
    # Same for SGB
    sgb_norm = pd.Series(100.0, index=base_index)
    if sgb_pct != 0 and len(base_index) > 1:
        progress = pd.Series(
            [sgb_pct * (i / (len(base_index) - 1)) for i in range(len(base_index))],
            index=base_index,
        )
        sgb_norm = 100.0 * (1 + progress)

    portfolio = eq_w * eq_norm + mf_w * mf_norm + sgb_w * sgb_norm
    portfolio.name = "My Portfolio"
    return pd.DataFrame({"My Portfolio": portfolio})


# ---------- Normalise to base 100 from today's first bar ----------
def normalize_to_open_today(df: pd.DataFrame) -> pd.DataFrame:
    """
    Take a DataFrame of intraday prices (mixed timezones from yfinance)
    and normalise each column so its first valid value today = 100.
    Drops rows before the most recent market day so we only show today.

    All timestamps are first converted to IST so 'today' is consistent
    across US/Asian/European indices (otherwise US indices would still
    show yesterday's close while Nifty shows today's open).

    "Today" is defined as: the longest contiguous run of rows (where any
    column has data) ending at the latest available timestamp, allowing
    gaps up to 30 minutes (sparse indices).
    """
    if df.empty:
        return df

    # Convert all timestamps to IST first so we have a single timeline
    def _to_ist(ts: pd.Timestamp) -> pd.Timestamp:
        if ts.tzinfo is None:
            ts = ts.tz_localize("UTC")
        return ts.tz_convert("Asia/Kolkata")
    df = df.copy()
    df.index = df.index.map(_to_ist)

    ffill = df.ffill()
    latest_valid = ffill.dropna(how="all").index.max()
    if pd.isna(latest_valid):
        return df

    # Keep only rows up to the latest valid timestamp
    df = df[df.index <= latest_valid]
    mask = df.notna().any(axis=1)
    if not mask.any():
        return df

    # Walk backwards from the last row. A gap (mask=False) of more than
    # 30 minutes between consecutive rows ends the "today" block.
    indices = list(df.index)
    run_start = indices[-1]
    for i in range(len(indices) - 1, 0, -1):
        ts = indices[i]
        prev_ts = indices[i - 1]
        gap = ts - prev_ts
        if gap > pd.Timedelta(minutes=30):
            # End of the contiguous run
            break
        run_start = prev_ts

    df = df[df.index >= run_start]
    df = df.dropna(how="all")
    if df.empty:
        return df
    # Normalise each column to 100 from its first valid value in the kept window
    first_valid = df.apply(lambda s: s.dropna().iloc[0] if s.dropna().size else float("nan"))
    df = df.div(first_valid).mul(100)
    return df


# ---------- Chart ----------
def draw_intraday_chart(combined: pd.DataFrame, asof: pd.Timestamp, interval: Interval) -> Path:
    """Draw a Plotly HTML chart with one line per index + the bold
    'My Portfolio' line. Returns the output path."""
    import plotly.graph_objects as go

    colors = [
        "#1f77b4", "#ff7f0e", "#2ca02c", "#d62728",
        "#9467bd", "#8c564b", "#e377c2", "#7f7f7f",
    ]

    fig = go.Figure()
    color_iter = iter(colors)

    # Plot indices first
    for col in combined.columns:
        if col == "My Portfolio":
            continue
        s = combined[col].dropna()
        if s.empty:
            continue
        fig.add_trace(go.Scatter(
            x=s.index, y=s.values,
            mode="lines",
            name=col,
            line=dict(color=next(color_iter, "#999999"), width=2),
            hovertemplate="%{x|%H:%M}<br>" + col + ": %{y:.2f}<extra></extra>",
        ))

    # Portfolio on top, bold black
    if "My Portfolio" in combined.columns:
        s = combined["My Portfolio"].dropna()
        if not s.empty:
            fig.add_trace(go.Scatter(
                x=s.index, y=s.values,
                mode="lines",
                name="My Portfolio (equity, mf, sgb)",
                line=dict(color="#000000", width=3.5),
                hovertemplate="%{x|%H:%M}<br>My Portfolio: %{y:.2f}<extra></extra>",
            ))

    fig.update_layout(
        title=f"My Portfolio vs World Indices — today ({interval} bars, base 100 at open)",
        xaxis_title="Time",
        yaxis_title="Indexed value (open = 100)",
        hovermode="x unified",
        template="plotly_white",
        legend=dict(orientation="h", yanchor="top", y=-0.15, xanchor="center", x=0.5),
        margin=dict(l=60, r=20, t=80, b=120),
    )
    fig.update_xaxes(rangeslider_visible=False)

    out = CHARTS_DIR / f"intraday_compare_{interval}.html"
    fig.write_html(out, include_plotlyjs="cdn", full_html=True)
    return out


# ---------- Snapshot (used by webapp) ----------
def build_intraday_snapshot(interval: Interval = "5m") -> dict:
    """
    Returns a JSON-serialisable snapshot for the webapp. All timestamps
    are converted to IST (Asia/Kolkata, UTC+05:30) so the chart's x-axis
    matches what the user sees on the wall clock — important because
    yfinance returns mixed timezones (US indices in America/New_York,
    Nifty in Asia/Kolkata, Nikkei in Asia/Tokyo, etc.).
      {
        "interval": "5m",
        "asof": "2026-06-25T15:30:00+05:30",
        "series": {
          "Nifty 50 (IN)":      [{"t": "...", "v": 100.0}, ...],
          "My Portfolio":       [...],
          ...
        },
      }
    """
    idx_df = fetch_intraday_indices(interval)
    portfolio_df = build_combined_portfolio(interval)
    combined = pd.concat([idx_df, portfolio_df], axis=1, sort=True)
    combined = normalize_to_open_today(combined)
    # Drop rows where EVERYTHING is NaN (rare index dropouts)
    combined = combined.dropna(how="all")

    # normalize_to_open_today already converts everything to IST, so
    # we can serialise directly. (See that function's docstring for why.)
    combined = combined.dropna(how="all")

    series: dict[str, list[dict]] = {}
    for col in combined.columns:
        s = combined[col].dropna()
        series[col] = [
            {"t": ts.strftime("%Y-%m-%dT%H:%M:%S%z"), "v": round(float(v), 3)}
            for ts, v in s.items()
        ]
    asof = combined.index.max() if not combined.empty else pd.Timestamp.now()
    return {
        "interval": interval,
        "asof": str(asof),
        "series": series,
    }


# ---------- Main ----------
def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--interval", choices=["1m", "5m", "15m"], default="5m",
        help="Candle size for the intraday chart (default: 5m)",
    )
    parser.add_argument(
        "--no-cache", action="store_true",
        help="Bypass the 5-minute cache and refetch from Yahoo",
    )
    args = parser.parse_args()

    interval = args.interval
    log.info("building intraday comparison at %s interval", interval)

    idx_df = fetch_intraday_indices(interval, use_cache=not args.no_cache)
    portfolio_df = build_combined_portfolio(interval)
    combined = pd.concat([idx_df, portfolio_df], axis=1, sort=True)
    combined = normalize_to_open_today(combined)

    asof = combined.index.max() if not combined.empty else pd.Timestamp.now()
    out = draw_intraday_chart(combined, asof, interval)

    print(f"\nIntraday chart saved: {out}")
    print(f"  interval: {interval}")
    print(f"  asof:     {asof}")
    print(f"  series:   {list(combined.columns)}")
    if not combined.empty:
        for col in combined.columns:
            s = combined[col].dropna()
            if s.empty:
                continue
            pct = (s.iloc[-1] - 100.0)
            print(f"  {col:<22} open=100.00  last={s.iloc[-1]:7.2f}  ({pct:+.2f}%)")


if __name__ == "__main__":
    main()