"""
pipeline.cohort_charts
----------------------
Render per-cohort CAGR comparison charts.

For each cohort (large_cap, midcap, smallcap), this module produces a PNG
chart that shows the cohort's total-return trajectory vs its benchmark
index over the same window.

Two chart types per cohort:
  1. Total return indexed to 100 at the cohort's first buy date
     (so you can see the cumulative outperformance/underperformance)
  2. Rolling 30-day CAGR (to spot periods of strong/weak performance)

PNG files are saved to data/charts/ and served by the webapp at /charts/.

Re-rendering: the function `render_cohort_charts()` returns the file
paths; it's called from the webapp /cagr route handler. Charts are
regenerated on every page load (cheap, ~100ms total).
"""
from __future__ import annotations

import io
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

import matplotlib
matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import pandas as pd

from pipeline.cohorts import (
    COHORTS, compute_cohorts, BENCHMARK_LABELS,
)
from pipeline.index_data import (
    close_on, get_index_history, available_indices,
)
from pipeline.ledger import build_ledger, LedgerEntry
from pipeline.logging_setup import get_logger

log = get_logger("cohort_charts")

from pipeline.runtime_paths import data_root

PROJECT = Path(__file__).resolve().parents[1]
CHARTS_DIR = data_root() / "charts"
CHARTS_DIR.mkdir(parents=True, exist_ok=True)

# Cache TTL: how long to reuse a previously generated chart (seconds).
# Charts are regenerated only if older than this or the cohort
# definitions change. This avoids re-fetching yfinance data on every
# webapp page load (~20 ticker fetches per cohort).
CHART_CACHE_TTL_SEC = 15 * 60  # 15 minutes


def _chart_age_seconds(path: Path) -> float:
    """Return the age of a chart file in seconds, or +inf if it
    doesn't exist."""
    import time as _time
    try:
        mtime = path.stat().st_mtime
        return _time.time() - mtime
    except FileNotFoundError:
        return float("inf")


# ---------------------------------------------------------------------------
# Per-ticker close price fetcher (uses yfinance with caching)
# ---------------------------------------------------------------------------

_PRICE_CACHE: dict[str, pd.Series] = {}


def _get_ticker_close(ticker: str, start: date, end: date) -> Optional[pd.Series]:
    """Return the close-price Series for a NSE ticker, indexed by date.

    Cached for the duration of one render_cohort_charts() call.
    Returns None on failure.
    """
    if ticker in _PRICE_CACHE:
        return _PRICE_CACHE[ticker]
    _PRICE_CACHE[ticker] = None
    try:
        import yfinance as yf
        # Buffer 5 days on each side to handle trading-day alignment
        start_str = (start - timedelta(days=5)).isoformat()
        end_str = (end + timedelta(days=5)).isoformat()
        t = yf.Ticker(f"{ticker}.NS")
        h = t.history(start=start_str, end=end_str, auto_adjust=True)
        if h is None or h.empty:
            return None
        s = h["Close"].copy()
        s.index = s.index.date  # convert DatetimeIndex to date
        s.name = ticker
        _PRICE_CACHE[ticker] = s
        return s
    except Exception as e:
        log.warning("could not fetch price for %s: %s", ticker, e)
        return None


# ---------------------------------------------------------------------------
# Build a daily portfolio value series
# ---------------------------------------------------------------------------

def _build_portfolio_value_series(
    entries: list[LedgerEntry],
    today: date,
    anchor: date,
) -> Optional[pd.Series]:
    """Return a Series {date: portfolio value}, normalized so the anchor
    date = 100.

    Algorithm (per trading day):
      portfolio_value(d) = sum over closed lots sold by d of (sell_value)
                          + sum over open lots held on d of (qty * close(d))
                          + sum over closed lots still held on d of (qty * avg_cost)

    The third term (closed lots still held, valued at cost) lets the chart
    show how the closed-lot cohort's "money was tied up" between buy and
    sell dates. The first term (closed lots already sold) contributes their
    realized sell value on the sell date and stays flat thereafter.
    The second term (open lots) uses the actual close price on day d for
    each stock — this is what makes the portfolio line track the market
    day-by-day.
    """
    if not entries:
        return None

    # Fetch all unique tickers' close prices once (cached)
    unique_tickers = {e.ticker for e in entries}
    ticker_closes: dict[str, pd.Series] = {}
    for t in unique_tickers:
        s = _get_ticker_close(t, anchor, today)
        if s is not None:
            ticker_closes[t] = s

    # Build a sorted set of trading days in the union of all close-price
    # Series, intersected with [anchor, today]. Use as our X-axis.
    # Only include days from anchor onwards.
    all_trading_days: set[date] = set()
    for s in ticker_closes.values():
        all_trading_days.update(d for d in s.index if d >= anchor)
    # Also include buy_date and sell_date as anchor points (only if >= anchor)
    for e in entries:
        if e.buy_date and anchor <= e.buy_date <= today:
            all_trading_days.add(e.buy_date)
        if e.sell_date and anchor <= e.sell_date <= today:
            all_trading_days.add(e.sell_date)
    all_trading_days.add(today)
    all_trading_days.add(anchor)

    if not all_trading_days:
        return None
    sorted_days = sorted(all_trading_days)

    # For each day, compute the portfolio value.
    # portfolio_value(d) = sum of:
    #   - For lots still held on d: qty * close(d)  (mark to market)
    #   - For lots already sold (d >= sell_date): sell_value (realized, frozen)
    # This way a sold lot's value "stays" in the portfolio as realized P&L
    # instead of dropping to 0.
    values = []
    cumulative_invested_at_day: list[float] = []
    for d in sorted_days:
        v = 0.0
        invested_today = 0.0
        for e in entries:
            if e.buy_date is not None and d >= e.buy_date:
                invested_today += e.buy_value
            if e.sell_date is not None and d >= e.sell_date:
                # Sold — contribute the sell value on the sell date and beyond
                v += e.sell_value
            elif e.buy_date is not None and d >= e.buy_date:
                # Still held on day d (open or not yet sold)
                if e.ticker in ticker_closes:
                    closes = ticker_closes[e.ticker]
                    # Find the close on or before d
                    avail = [dd for dd in closes.index if dd <= d]
                    if avail:
                        v += e.qty * float(closes.loc[max(avail)])
                    else:
                        v += e.buy_value  # fallback to cost basis
                else:
                    # No price data: use cost basis
                    v += e.buy_value
        values.append(v)
        cumulative_invested_at_day.append(invested_today)

    s = pd.Series(values, index=pd.DatetimeIndex([pd.Timestamp(d) for d in sorted_days]),
                  name="portfolio")
    s = s.sort_index()
    invested = pd.Series(cumulative_invested_at_day,
                         index=pd.DatetimeIndex([pd.Timestamp(d) for d in sorted_days]),
                         name="invested")
    invested = invested.sort_index()

    if s.empty or s.iloc[0] is None or s.iloc[0] <= 0:
        return None
    # TWR-style total return: portfolio_value(d) / cumulative_invested(d)
    # This way the indexed line shows "₹100 of invested capital is now
    # worth X". Same denominator everywhere (cumulative invested) so
    # adding new positions doesn't reset the baseline.
    # Guard: if cumulative_invested is 0 for the first day (no lots
    # bought yet), use the portfolio value itself as the baseline.
    ratio = s / invested.replace(0, pd.NA)
    # Backfill: on the very first day the ratio is NaN (invested was 0).
    # Use 1.0 (= 100%) as the start.
    ratio = ratio.ffill().bfill()
    # Multiply by 100 so the chart shows "indexed to 100"
    s_indexed = ratio * 100.0
    return s_indexed


# ---------------------------------------------------------------------------
# Build a benchmark value series
# ---------------------------------------------------------------------------

def _build_benchmark_series(
    benchmark: str, anchor: date, today: date,
) -> Optional[pd.Series]:
    """Return a Series {date: index level} for the given benchmark,
    normalized to 100 at the anchor date."""
    hist = get_index_history(benchmark)
    if not hist:
        return None

    # Convert to Series indexed by date
    s = pd.Series(
        {d: close for d, close in hist},
        name=benchmark,
    )
    s.index = pd.to_datetime(s.index)
    s = s.sort_index()

    # Clip to [anchor, today]
    s = s.loc[pd.Timestamp(anchor):pd.Timestamp(today)]
    if s.empty:
        return None
    # Normalize to 100 at anchor (use the first value)
    if s.iloc[0] != 0:
        s = s / s.iloc[0] * 100.0
    return s


# ---------------------------------------------------------------------------
# Render one chart per cohort
# ---------------------------------------------------------------------------

def render_cohort_chart(
    cohort_name: str,
    current_ltp_fn=None,
) -> Optional[Path]:
    """Render a single cohort-vs-benchmark chart. Returns the PNG path
    or None if there's not enough data.

    The chart is anchored at the cohort's own first buy date (after
    filtering), so the line is never at 0 for long periods. This means
    the chart's CAGR is computed over the same window as the table's
    "weighted years" metric (which is also the cohort's holding period).
    """
    cfg = COHORTS.get(cohort_name)
    if not cfg:
        return None

    today = date.today()

    # Get the cohort entries
    from pipeline.cohorts import _is_longterm, _holding_days
    ledger = build_ledger()
    entries = [e for e in ledger if cfg["filter"](e)]
    if cfg.get("longterm_only"):
        entries = [e for e in entries if _is_longterm(e, today)]
    cohort_min_days = cfg.get("min_holding_days", 0)
    if cohort_min_days > 0:
        entries = [e for e in entries if _holding_days(e, today) >= cohort_min_days]
    if not entries:
        return None

    # Anchor: the cohort's own first buy date (after filtering).
    # This is the natural start of the cohort's story.
    cohort_anchor = min((e.buy_date for e in entries if e.buy_date), default=today)
    if cohort_anchor > today:
        return None
    anchor = cohort_anchor

    # Build the series
    portfolio = _build_portfolio_value_series(entries, today, anchor)
    if portfolio is None or portfolio.empty:
        return None
    benchmark = _build_benchmark_series(cfg["benchmark"], anchor, today)
    if benchmark is None or benchmark.empty:
        # Don't render if we don't have benchmark data
        return None

    # Plot
    fig, ax = plt.subplots(figsize=(11, 5.5))
    ax.plot(portfolio.index, portfolio.values,
            label=f"{cfg['label']} (portfolio)",
            color="#1f77b4", linewidth=2.0)
    bench_label = BENCHMARK_LABELS.get(cfg["benchmark"], cfg["benchmark"])
    ax.plot(benchmark.index, benchmark.values,
            label=bench_label,
            color="#ff7f0e", linewidth=2.0, linestyle="--")

    # Compute the final CAGR over the chart window
    years = (today - anchor).days / 365.25
    final_portfolio = portfolio.iloc[-1] / 100.0
    final_bench = benchmark.iloc[-1] / 100.0
    portfolio_cagr = ((final_portfolio) ** (1.0 / years) - 1.0) * 100 if years > 0 else 0
    bench_cagr = ((final_bench) ** (1.0 / years) - 1.0) * 100 if years > 0 else 0
    alpha = portfolio_cagr - bench_cagr

    ax.axhline(100, color="gray", linewidth=0.5, linestyle=":")

    ax.set_title(
        f"{cfg['label']} vs {bench_label}\n"
        f"Anchor: {anchor.isoformat()}  ({years:.2f} years)  "
        f"Portfolio CAGR: {portfolio_cagr:+.2f}%  |  "
        f"Benchmark CAGR: {bench_cagr:+.2f}%  |  "
        f"Alpha: {alpha:+.2f}%",
        fontsize=12, weight="bold",
    )
    ax.set_xlabel("Date")
    ax.set_ylabel("Indexed value (100 = anchor date)")
    ax.legend(loc="upper left", framealpha=0.9)
    ax.grid(True, alpha=0.3)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %Y"))
    ax.xaxis.set_major_locator(mdates.AutoDateLocator())
    fig.autofmt_xdate()

    # Annotate: write a small text box with the final values
    final_text = (
        f"Final portfolio: ₹{final_portfolio * 100:.0f} (indexed)\n"
        f"Final benchmark: ₹{final_bench * 100:.0f} (indexed)\n"
        f"Cumulative alpha: {alpha * years:+.1f} pp"
    )
    ax.text(0.99, 0.02, final_text, transform=ax.transAxes,
            fontsize=8, ha="right", va="bottom",
            family="monospace",
            bbox=dict(boxstyle="round,pad=0.4",
                      facecolor="#f5f5f5", edgecolor="#cccccc", linewidth=0.6))

    fig.tight_layout()
    out = CHARTS_DIR / f"cohort_{cohort_name}_{today.isoformat()}.png"
    fig.savefig(out, dpi=120, bbox_inches="tight")
    plt.close(fig)
    return out


# ---------------------------------------------------------------------------
# Combined portfolio vs Nifty 50 (DCA counterfactual)
# ---------------------------------------------------------------------------

def _build_combined_portfolio_series(
    today: date,
    current_ltp_fn=None,
    include_etfs: bool = False,
) -> Optional[pd.Series]:
    """Build a daily total portfolio value series across ALL equity
    holdings (and optionally ETFs). Same algorithm as
    _build_portfolio_value_series but applied to the whole ledger.

    Returns a Series normalized to 100 at the anchor.
    """
    ledger = build_ledger()
    if not include_etfs:
        ledger = [e for e in ledger if not e.is_etf]
    # Combined view: keep EVERY lot (no LT filter, no min-holding filter).
    # The user wants the full picture: "my entire portfolio".
    if not ledger:
        return None
    anchor = min((e.buy_date for e in ledger if e.buy_date), default=today)
    if anchor > today:
        return None
    return _build_portfolio_value_series(ledger, today, anchor)


def _build_nifty50_dca_series(
    cashflows: list[tuple[date, float]],
    today: date,
    ledger: list = None,
    current_ltp_fn=None,
) -> Optional[pd.Series]:
    """Build a 'Nifty 50 DCA' series: take the user's buy/sell cash flows
    and assume they were invested in Nifty 50 at the close on the cash
    flow date. For open positions, the terminal value is the Nifty 50
    equivalent of those positions (units of Nifty 50 bought at the
    user's buy dates, valued at today's Nifty 50 close).

    Algorithm:
      Treat the DCA position as a number of Nifty 50 "units" held.
      A buy of amount C on day d (Nifty 50 close = P_d) adds C/P_d
      units. A sell of amount C on day d removes C/P_d units. The
      value of the position on any later day d' is:
        units_held(d') * Nifty50_close(d')

      For open positions, the "Nifty 50 equivalent" is computed by
      going through the user's open lots and adding the units that
      each lot's buy_value would have bought in Nifty 50. This is
      done by appending a synthetic buy at today's Nifty 50 close
      for the same buy_value as the open lot's cost basis, then
      the Nifty 50 series correctly values these units at today's
      Nifty 50 close.

    The Nifty 50 DCA line is smooth (no spikes) and apples-to-apples
    with the portfolio TWR: same cash flows, same anchor, just
    invested in Nifty 50 instead of the user's chosen stocks.
    """
    nifty_hist = get_index_history("nifty50")
    if not nifty_hist:
        return None
    nifty = pd.Series({d: close for d, close in nifty_hist}, name="nifty50")
    nifty.index = pd.to_datetime(nifty.index)
    nifty = nifty.sort_index()

    if not cashflows:
        return None
    cashflows = list(cashflows)  # we'll add to it
    cashflows.sort(key=lambda x: x[0])

    # If we have the ledger, add a synthetic "buy" for the open
    # positions at today's Nifty 50 close, so the terminal value of
    # the DCA position equals the Nifty 50 equivalent of the open
    # positions. This way the chart compares apples-to-apples.
    if ledger and current_ltp_fn:
        from pipeline.index_data import close_on
        nifty_today = close_on("nifty50", today)
        if nifty_today and nifty_today > 0:
            for e in ledger:
                if e.is_open and e.buy_date:
                    # Add a "buy" at today's close for the same amount
                    # as the open lot's cost basis. This makes the DCA
                    # position grow to Nifty 50 equivalent of the open
                    # positions at today.
                    cashflows.append((today, -e.buy_value))

    cashflows.sort(key=lambda x: x[0])

    d0 = cashflows[0][0]

    def _nifty_close_on(d: date) -> Optional[float]:
        """Find Nifty 50 close on or before date d."""
        avail = [dd for dd in nifty.index if dd.date() <= d]
        if not avail:
            return None
        return float(nifty.loc[max(avail)])

    nifty_d0 = _nifty_close_on(d0)
    if nifty_d0 is None or nifty_d0 <= 0:
        return None

    # Build a unit-count series that jumps on each cash flow date.
    sorted_cf = sorted(cashflows, key=lambda x: x[0])
    unit_count_by_date: dict[date, float] = {}
    cumulative_units = 0.0
    for cd, cc in sorted_cf:
        nifty_at_event = _nifty_close_on(cd)
        if nifty_at_event is None or nifty_at_event <= 0:
            continue
        unit_delta = -cc / nifty_at_event
        cumulative_units += unit_delta
        unit_count_by_date[cd] = cumulative_units

    if not unit_count_by_date:
        return None

    # Build the daily value series: (cumulative units) * nifty_close[d]
    all_days = set(unit_count_by_date.keys())
    all_days.add(today)
    all_days.update(nifty.index.date)
    sorted_days = sorted(d for d in all_days if d0 <= d <= today)

    values = []
    for d in sorted_days:
        nifty_today_d = _nifty_close_on(d)
        if nifty_today_d is None:
            continue
        avail = [dd for dd in unit_count_by_date if dd <= d]
        if not avail:
            continue
        units = unit_count_by_date[max(avail)]
        values.append((d, units * nifty_today_d))

    if not values:
        return None
    s = pd.Series([v for _, v in values],
                  index=pd.DatetimeIndex([pd.Timestamp(d) for d, _ in values]),
                  name="nifty50_dca")
    if s.iloc[0] != 0:
        s = s / s.iloc[0] * 100.0
    return s


def render_combined_chart(
    current_ltp_fn=None,
    include_etfs: bool = False,
    force: bool = False,
) -> Optional[Path]:
    """Render the COMBINED-portfolio-vs-Nifty-50 chart. Includes all
    equity holdings (and optionally ETFs). Nifty 50 is shown as a
    lump-sum counterfactual: "if I'd invested my total cost basis in
    Nifty 50 on the first buy date, what would it be worth today?"

    Caches by date: if today's chart already exists and is <15min old,
    returns the existing path. force=True bypasses the cache.
    """
    from pipeline.ledger import build_ledger

    today = date.today()
    chart_path = CHARTS_DIR / f"combined_vs_nifty50_{today.isoformat()}.png"

    if not force and chart_path.exists() and _chart_age_seconds(chart_path) < CHART_CACHE_TTL_SEC:
        return chart_path

    # Get the full ledger
    ledger = build_ledger()
    if not include_etfs:
        ledger = [e for e in ledger if not e.is_etf]
    if not ledger:
        return None

    # Build the portfolio total-return series
    portfolio = _build_combined_portfolio_series(today, current_ltp_fn, include_etfs)
    if portfolio is None or portfolio.empty:
        return None

    # The Nifty 50 lump-sum counterfactual: take the user's TOTAL cost
    # basis as a single buy on the first buy date, then track it
    # through Nifty 50's daily closes. This is the simplest and
    # cleanest "what if I'd just bought Nifty 50" comparison.
    total_invested = sum(e.buy_value for e in ledger)
    first_buy = min((e.buy_date for e in ledger if e.buy_date), default=today)
    if first_buy > today:
        return None

    # Nifty 50 series from first_buy to today, in rupees (not indexed)
    nifty = _build_benchmark_series("nifty50", first_buy, today)
    if nifty is None or nifty.empty:
        return None
    # Scale to "if I'd invested total_invested on first_buy"
    # nifty at first_buy: nifty.iloc[0] = 100 (normalized)
    # So the lump-sum value at day d = total_invested * nifty.iloc[d_idx] / 100
    nifty_lumpsum = nifty / 100.0 * total_invested
    # Now normalize to 100 at the anchor
    nifty_lumpsum = nifty_lumpsum / nifty_lumpsum.iloc[0] * 100.0

    # Plot
    fig, ax = plt.subplots(figsize=(12, 6))
    ax.plot(portfolio.index, portfolio.values,
            label="My combined equity portfolio (TWR)",
            color="#1f77b4", linewidth=2.2)
    nifty_label = f"Nifty 50 (lump-sum: ₹{total_invested:,.0f} on {first_buy.isoformat()})"
    ax.plot(nifty_lumpsum.index, nifty_lumpsum.values,
            label=nifty_label,
            color="#ff7f0e", linewidth=2.0, linestyle="--")
    ax.axhline(100, color="gray", linewidth=0.5, linestyle=":")

    # Compute CAGRs from the chart data
    anchor = portfolio.index[0].date()
    years = (today - anchor).days / 365.25
    final_p = portfolio.iloc[-1] / 100.0
    final_b = nifty_lumpsum.iloc[-1] / 100.0
    p_cagr = ((final_p) ** (1.0 / years) - 1.0) * 100 if years > 0 else 0
    b_cagr = ((final_b) ** (1.0 / years) - 1.0) * 100 if years > 0 else 0
    alpha = p_cagr - b_cagr

    etfs_note = " (ETFs included)" if include_etfs else " (ETFs excluded)"
    ax.set_title(
        f"Combined Equity Portfolio{etfs_note} vs Nifty 50 (Lump-Sum Counterfactual)\n"
        f"Anchor: {anchor.isoformat()}  ({years:.2f} years)  "
        f"Portfolio CAGR: {p_cagr:+.2f}%  |  "
        f"Nifty 50 CAGR: {b_cagr:+.2f}%  |  "
        f"Alpha: {alpha:+.2f}%",
        fontsize=12, weight="bold",
    )
    ax.set_xlabel("Date")
    ax.set_ylabel("Indexed value (100 = anchor date)")
    ax.legend(loc="upper left", framealpha=0.9)
    ax.grid(True, alpha=0.3)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %Y"))
    ax.xaxis.set_major_locator(mdates.AutoDateLocator())
    fig.autofmt_xdate()

    # Annotation box
    total_realized = sum(e.sell_value for e in ledger if e.sell_date)
    open_value = sum(
        e.qty * (current_ltp_fn(e.ticker) if current_ltp_fn and current_ltp_fn(e.ticker) else e.buy_price)
        for e in ledger if e.is_open
    )
    counterfactual_value = total_invested * (final_b / 100.0)
    diff = (total_realized + open_value) - counterfactual_value
    final_text = (
        f"Window: {years:.2f} years  ({anchor.isoformat()} \u2192 {today.isoformat()})\n"
        f"Final portfolio (indexed):  \u20b9{final_p * 100:.0f}\n"
        f"Final Nifty 50 (indexed):   \u20b9{final_b * 100:.0f}\n"
        f"Counterfactual: invest \u20b9{total_invested:,.0f} on {first_buy.isoformat()}\n"
        f"  \u2192 would be worth \u20b9{counterfactual_value:,.0f} today\n"
        f"  Your actual value: \u20b9{total_realized + open_value:,.0f}\n"
        f"  Difference: \u20b9{diff:+,.0f}  ({'beat' if diff >= 0 else 'lost to'} index by \u20b9{abs(diff):,.0f})\n"
        f"Cumulative alpha: {alpha * years:+.1f} pp"
    )
    ax.text(0.99, 0.02, final_text, transform=ax.transAxes,
            fontsize=8, ha="right", va="bottom",
            family="monospace",
            bbox=dict(boxstyle="round,pad=0.4",
                      facecolor="#f5f5f5", edgecolor="#cccccc", linewidth=0.6))

    fig.tight_layout()
    fig.savefig(chart_path, dpi=120, bbox_inches="tight")
    plt.close(fig)
    return chart_path


def _cohort_chart_path(cohort_name: str) -> Path:
    """Return today's chart path for a cohort (not necessarily existing)."""
    return CHARTS_DIR / f"cohort_{cohort_name}_{date.today().isoformat()}.png"


def render_all_cohort_charts(current_ltp_fn=None, force: bool = False) -> dict[str, Optional[Path]]:
    """Render all three cohort charts. Returns {cohort_name: png_path}.

    Caches by date: if today's chart already exists and is less than
    CHART_CACHE_TTL_SEC old, returns the existing path without
    re-rendering. Pass force=True to bypass the cache.
    """
    out: dict[str, Optional[Path]] = {}

    # First pass: check the cache, populate out with existing files
    cache_hit = True
    for cohort_name in COHORTS:
        p = _cohort_chart_path(cohort_name)
        if force or _chart_age_seconds(p) >= CHART_CACHE_TTL_SEC:
            cache_hit = False
            break
        if p.exists():
            out[cohort_name] = p
        else:
            cache_hit = False
            break

    if cache_hit and len(out) == len(COHORTS):
        log.info("cohort charts: cache hit (all %d charts < %ds old)", len(COHORTS), CHART_CACHE_TTL_SEC)
        return out

    # Cache miss: clear the price cache and regenerate all
    _PRICE_CACHE.clear()
    for cohort_name in COHORTS:
        try:
            out[cohort_name] = render_cohort_chart(cohort_name, current_ltp_fn)
        except Exception as e:
            log.exception("failed to render %s chart: %s", cohort_name, e)
            out[cohort_name] = None

    # Clean up old chart files (keep only today's)
    for old in CHARTS_DIR.glob("cohort_*_*.png"):
        try:
            if not old.name.endswith(f"_{date.today().isoformat()}.png"):
                old.unlink()
        except Exception:
            pass
    return out


# ---------------------------------------------------------------------------
# CLI for testing
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(PROJECT))
    paths = render_all_cohort_charts()
    for k, v in paths.items():
        print(f"{k}: {v}")
