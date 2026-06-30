#!/usr/bin/env python3
"""
Step 1 of the portfolio tracker:
Fetch the previous trading day's close for Nifty 50, S&P 500, NASDAQ, DAX
and produce a single-day %-change comparison chart.

Run:
    python3 indices_chart.py            # previous trading day
"""
# TABLE OF CONTENTS (read this first)
#
# This file has 5 major sections (327 lines total):
#
# 1. Config ----------
# 2. Fetch ----------
# 3. Normalize ----------
# 4. Chart ----------
# 5. Main ----------

from __future__ import annotations

import argparse
import json
import time
from datetime import datetime
from pathlib import Path

import matplotlib
matplotlib.use("Agg")  # default: headless. Switched to macosx if --show.
import matplotlib.pyplot as plt
import pandas as pd
import yfinance as yf

from .logging_setup import get_logger
from .parallel import map_parallel

log = get_logger("indices")

# ---------- Config ----------
PROJECT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT / "data"
CHARTS_DIR = PROJECT / "data" / "charts"
DATA_DIR.mkdir(exist_ok=True)
CHARTS_DIR.mkdir(exist_ok=True)

# Map: display_name -> yfinance ticker
# Top 3 world indices + Nifty 50 for the Indian reference baseline.
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

CACHE_FILE = PROJECT / "data/cache/indices_cache.csv"


# ---------- Fetch ----------
def _fetch_nse_nifty() -> float | None:
    """
    Fetch today's Nifty 50 close from NSE's public API.
    NSE is more accurate than Yahoo for the official EOD print.
    Returns None on failure.
    """
    try:
        import requests
        headers = {
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json",
            "Accept-Language": "en-US,en;q=0.9",
        }
        r = requests.get(
            "https://www.nseindia.com/api/chart-databyindex?index=NIFTY%2050&indices=true",
            headers=headers, timeout=10,
        )
        r.raise_for_status()
        # The first series point is the latest, the last is the oldest
        data = r.json()
        if isinstance(data, dict) and "grapthData" in data:
            pts = data["grapthData"]
            if pts:
                # Last entry is the most recent
                return float(pts[-1][1])
    except Exception:
        pass
    return None


def fetch_indices(period: str) -> pd.DataFrame:
    """
    Returns a DataFrame indexed by date with one column per index (close price).
    Uses a local cache so subsequent runs are fast and Yahoo rate limits are avoided.
    """
    tickers = list(INDICES.values())

    def _download_one(t: str) -> pd.Series:
        """yfinance helper that returns a single ticker's close series, with retries."""
        last_err: Exception | None = None
        for attempt in range(3):
            try:
                df = yf.download(
                    t,
                    period="max",
                    interval="1d",
                    auto_adjust=True,
                    progress=False,
                    threads=False,
                )
                if not df.empty and "Close" in df.columns:
                    return df["Close"]
            except Exception as e:  # network blips, "database is locked", etc.
                last_err = e
                time.sleep(2 * (attempt + 1))
        raise RuntimeError(f"yfinance failed for {t}: {last_err}")

    if CACHE_FILE.exists():
        cached = pd.read_csv(CACHE_FILE, index_col=0)
        cached.index = pd.to_datetime(cached.index, errors="coerce")
        cached = cached[cached.index.notna()]
    else:
        cached = pd.DataFrame()

    # Decide what to fetch.
    # If the cache already has *today's* row for every ticker, use it.
    # Otherwise, refetch. (We can't rely on a simple "is the cache < 1 day old"
    # check because some indices (e.g. NSE) get patched in-place, leaving the
    # others stale.)
    today = pd.Timestamp.today().normalize()
    needs_fetch = cached.empty
    if not needs_fetch:
        # Check whether every ticker has a row for today's date
        for t in tickers:
            if t not in cached.columns or today not in cached.index:
                needs_fetch = True
                break
            # If today's row exists but the value is NaN, also refetch
            if pd.isna(cached.loc[today, t]):
                needs_fetch = True
                break

    if needs_fetch:
        # Always pull "max" so the cache grows over time; we slice to `period` at the end.
        log.info("fetching %d tickers from Yahoo Finance...", len(tickers))
        # Parallelise per-ticker downloads. Yahoo's rate-limit window is
        # generous enough for ~8 concurrent connections on a typical network.
        def _safe_download(t: str) -> pd.Series | None:
            try:
                s = _download_one(t)
                log.info("  ok  %s  (%d days)", t, len(s))
                return s
            except Exception as e:
                log.error("  FAIL %s: %s", t, e)
                return None
        series_list = map_parallel(
            _safe_download, tickers,
            desc="yahoo tickers",
        )
        series_map: dict[str, pd.Series] = {
            t: s for t, s in zip(tickers, series_list) if s is not None
        }
        if not series_map:
            raise RuntimeError("yfinance returned no data — check your network or ticker symbols")
        closes = pd.concat(series_map, axis=1, sort=True)
        closes.index = pd.to_datetime(closes.index)
        # If pd.concat produced a 2-level column index (ticker, 'Close'), flatten it.
        if isinstance(closes.columns, pd.MultiIndex):
            closes.columns = closes.columns.get_level_values(0)
        # Merge with cache (cache wins where newer)
        if not cached.empty:
            cached.index = pd.to_datetime(cached.index)
            closes = closes.combine_first(cached)
        closes = closes.sort_index()

        # Patch Nifty 50 with NSE's official EOD close (more accurate than Yahoo).
        nse_nifty = _fetch_nse_nifty()
        if nse_nifty is not None and "^NSEI" in closes.columns:
            today = pd.Timestamp.today().normalize()
            # If today's row already exists, update; else create it
            if today in closes.index:
                closes.loc[today, "^NSEI"] = nse_nifty
            else:
                # Only add if the most recent row is *today* (otherwise Yahoo's
                # most recent row is the actual latest trading day, and we don't
                # want to overwrite it).
                last_idx = closes.index.max()
                if last_idx.normalize() == today - pd.tseries.offsets.BDay(1):
                    # Yesterday's row exists but today's hasn't printed yet —
                    # don't fabricate a row.
                    pass
                else:
                    closes.loc[today, "^NSEI"] = nse_nifty
            log.info("  nse_nifty override: %s", nse_nifty)

        closes.to_csv(CACHE_FILE)
        log.info("  cached %d days  (%s -> %s)",
                 closes.shape[0], closes.index.min().date(), closes.index.max().date())
        cached = closes
    else:
        log.info("cache hit: %d days  (%s -> %s)",
                 len(cached), cached.index.min().date(), cached.index.max().date())

    # Slice to the requested period.
    period_days = {
        "1mo": 30, "3mo": 90, "6mo": 180, "1y": 365, "2y": 730, "5y": 1825, "max": None,
    }.get(period)
    if period_days is not None:
        cutoff = cached.index.max() - pd.Timedelta(days=period_days)
        cached = cached[cached.index >= cutoff]

    # Rename columns from tickers to display names
    inv = {v: k for k, v in INDICES.items()}
    cached = cached.rename(columns=inv)
    return cached


# ---------- Normalize ----------
def normalize_to_base100(df: pd.DataFrame) -> pd.DataFrame:
    """Set first valid row to 100, scale every other row by the same factor."""
    return df.div(df.iloc[0]).mul(100)


# ---------- Chart ----------
def cleanup_old_charts(period: str) -> None:
    """Remove every PNG/HTML for the given period so only the latest is kept."""
    for ext in ("png", "html"):
        for old in CHARTS_DIR.glob(f"indices_{period}.*"):
            try:
                old.unlink()
            except OSError:
                pass


def fetch_previous_day_changes() -> tuple[pd.DataFrame, pd.Timestamp]:
    """
    Returns (df, asof_date) where:
      df has columns: index_name, prev_close, last_close, pct_change
      asof_date is the most recent trading day for which we have data
                 (i.e. the "previous trading day" relative to today, or
                  today if the market has just closed and a print is available).
    """
    raw = fetch_indices("5d")  # tiny window; just need last 2 prints
    # Use the last *two* valid rows per index
    rows = []
    asof: pd.Timestamp | None = None
    for col in raw.columns:
        s = raw[col].dropna()
        if len(s) < 1:
            continue
        last = s.iloc[-1]
        prev = s.iloc[-2] if len(s) >= 2 else last
        pct = (last / prev - 1.0) * 100.0 if prev else 0.0
        rows.append({
            "index_name": col,
            "prev_close": float(prev),
            "last_close": float(last),
            "pct_change": float(pct),
        })
        d = s.index[-1]
        if asof is None or d > asof:
            asof = d
    df = pd.DataFrame(rows).sort_values("pct_change", ascending=False).reset_index(drop=True)
    return df, asof


def draw_chart(pct_df: pd.DataFrame, asof: pd.Timestamp, period: str) -> tuple["matplotlib.figure.Figure", Path]:
    fig, ax = plt.subplots(figsize=(11, 6))
    colors = ["#2ca02c" if v >= 0 else "#d62728" for v in pct_df["pct_change"]]
    bars = ax.barh(pct_df["index_name"], pct_df["pct_change"], color=colors, edgecolor="black", linewidth=0.6)
    ax.axvline(0, color="black", linewidth=0.8)

    # Annotate each bar with its % value
    for bar, v in zip(bars, pct_df["pct_change"]):
        x = bar.get_width()
        offset = 0.05 if v >= 0 else -0.05
        ha = "left" if v >= 0 else "right"
        ax.text(x + offset, bar.get_y() + bar.get_height() / 2,
                f"{v:+.2f}%", va="center", ha=ha, fontsize=10, weight="bold")

    ax.set_title(f"Index % Change  —  {asof.strftime('%Y-%m-%d')}", fontsize=14, weight="bold")
    ax.set_xlabel("Percent change")
    ax.set_ylabel("")
    ax.grid(True, axis="x", alpha=0.3)
    # Add a little padding on the x axis so the labels fit
    xmin, xmax = ax.get_xlim()
    pad = max(0.3, (xmax - xmin) * 0.15)
    ax.set_xlim(xmin - pad, xmax + pad)
    fig.tight_layout()

    out = CHARTS_DIR / f"indices_{period}.png"
    fig.savefig(out, dpi=130)
    return fig, out


def print_summary(pct_df: pd.DataFrame, asof: pd.Timestamp) -> None:
    print(f"\nIndex % change on {asof.strftime('%Y-%m-%d')}:")
    print("-" * 50)
    for _, r in pct_df.iterrows():
        print(f"  {r['index_name']:<22} {r['pct_change']:+7.2f}%   "
              f"({r['prev_close']:,.2f} -> {r['last_close']:,.2f})")
    print(f"\nBase date (previous trading day): "
          f"{(asof - pd.tseries.offsets.BDay(1)).strftime('%Y-%m-%d')}")


# ---------- Main ----------
def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--show",
        action="store_true",
        help="Open the chart in an interactive matplotlib window (in addition to saving PNG).",
    )
    args = parser.parse_args()

    if args.show:
        try:
            matplotlib.use("macosx", force=True)
            import matplotlib.pyplot as _plt  # noqa: F401
        except Exception as e:
            log.warning("could not enable macosx backend (%s); falling back to inline.", e)

    period = "prev_day"
    pct_df, asof = fetch_previous_day_changes()
    cleanup_old_charts(period)
    fig, out = draw_chart(pct_df, asof, period)
    print_summary(pct_df, asof)
    print(f"\nChart saved: {out}")

    if args.show:
        plt.show()


if __name__ == "__main__":
    main()
