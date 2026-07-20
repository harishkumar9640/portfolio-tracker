"""
pipeline.cohorts
----------------
Cohort-based CAGR comparison for the user's equity positions.

Cohorts (tier-based, excluding ETFs):
  - "large_cap_equity"  : large-cap equity (Nifty50 universe) vs Nifty50
  - "midcap_equity"      : mid-cap equity vs Nifty Midcap 150
                          (fallback to Nifty50 if data missing)
  - "smallcap_equity"    : small-cap equity vs Nifty Smallcap 250
                          (fallback to Nifty50 if data missing)
  - "etf"                : all ETFs (excluded from comparison per user request)

Each cohort includes BOTH currently-held positions AND realized P&L
from positions sold in the past (per user request: "I want you to
include old holdings as well which I sold in past for this comparision").

For each cohort we report:
  - Total cost basis (open + closed)
  - Total current value (open at LTP + closed at sell price)
  - Realized P&L (sum of P&L from closed positions)
  - Total return: (current - cost) / cost
  - CAGR (simple, weighted by invested)
  - Benchmark CAGR (same window)
  - Alpha vs benchmark

If the cohort-specific index data is not available, the benchmark falls
back to Nifty50 with a clear "fallback" flag in the output.

Long-term filter
----------------
Pass `longterm_only=True` to only count lots held for > 1 year (per the
Indian Income Tax Act: equity held > 12 months = long-term capital
gains). A closed lot is long-term if (sell_date - buy_date).days > 365.
An open lot is long-term if (today - buy_date).days > 365.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import date, datetime
from pathlib import Path
from typing import Optional

from pipeline.ledger import (
    LedgerEntry, build_ledger, ledger_summary, _is_etf,
)
from pipeline.marketcap import classify, get_market_cap_cr
from pipeline.index_data import close_on, is_available, available_indices
from pipeline.cagr import cagr as cagr_fn

PROJECT = Path(__file__).resolve().parents[1]


# Long-term threshold: Indian Income Tax Act treats equity held > 12 months
# as long-term capital gains. Using 365 days as a simple proxy.
LONGTERM_DAYS = 365

# Pretty benchmark display names
BENCHMARK_LABELS = {
    "nifty50": "Nifty 50",
    "nifty_midcap_150": "Nifty Midcap 150",
    "nifty_smallcap_250": "Nifty Smallcap 250",
}


# Cohort definitions: name -> dict of filter predicates
def _in_cohort_large_cap(e: LedgerEntry) -> bool:
    """Large-cap equity (Nifty50 universe), current + sold."""
    return e.tier == "large" and not e.is_etf


def _in_cohort_midcap(e: LedgerEntry) -> bool:
    """Mid-cap equity, current + sold."""
    return e.tier == "mid" and not e.is_etf


def _in_cohort_smallcap(e: LedgerEntry) -> bool:
    """Small-cap equity, current + sold."""
    return e.tier == "small" and not e.is_etf


COHORTS = {
    "large_cap_equity": {
        "label": "Large-cap Equity (Nifty50 universe)",
        "benchmark": "nifty50",
        "filter": _in_cohort_large_cap,
        "longterm_only": True,   # user requested LT-only for large-cap
        "min_holding_days": 30,  # skip day-trades that produce meaningless CAGRs
    },
    "midcap_equity": {
        "label": "Mid-cap Equity",
        "benchmark": "nifty_midcap_150",
        "filter": _in_cohort_midcap,
        "longterm_only": False,  # user said "from past to present" for mid/small
        "min_holding_days": 30,  # skip day-trades that produce meaningless CAGRs
    },
    "smallcap_equity": {
        "label": "Small-cap Equity",
        "benchmark": "nifty_smallcap_250",
        "filter": _in_cohort_smallcap,
        "longterm_only": False,  # user said "from past to present" for mid/small
        "min_holding_days": 30,  # skip day-trades that produce meaningless CAGRs
    },
}


@dataclass
class CohortResult:
    name: str
    label: str
    benchmark: str
    benchmark_available: bool
    benchmark_using_fallback: bool
    fallback_to: str
    # Aggregate metrics
    n_lots: int
    n_open: int
    n_closed: int
    n_tickers: int
    tickers: list[str]
    # Money flows
    total_invested: float       # sum of buy_value for both open and closed
    open_value: float           # current LTP × qty for open positions
    realized_pnl: float         # sum of P&L from closed positions
    total_current: float        # open_value + sum of sell_value for closed
    total_pnl: float            # total_current - total_invested
    total_return_pct: float     # total_pnl / total_invested * 100
    # Time metrics
    earliest_buy: Optional[str]
    latest_buy: Optional[str]
    weighted_years: float
    cagr_pct: Optional[float]
    nifty_cagr_pct: Optional[float]
    alpha_pct: Optional[float]
    # Per-ticker breakdown
    tickers_detail: list[dict]


def _is_longterm(e: LedgerEntry, today: date) -> bool:
    """A lot is longterm if it's been held for more than 365 days.
    For open positions, use (today - buy_date).days.
    For closed positions, use (sell_date - buy_date).days."""
    end = e.sell_date if e.sell_date is not None else today
    return (end - e.buy_date).days > LONGTERM_DAYS


def _holding_days(e: LedgerEntry, today: date) -> int:
    """How many days was this lot held? Uses sell_date if closed, else today."""
    end = e.sell_date if e.sell_date is not None else today
    return (end - e.buy_date).days


# Minimum holding period (days) to consider a lot's CAGR reliable.
# Day-trades and sub-30-day speculative positions produce absurdly large
# annualised rates from tiny price moves, which swamp weighted averages.
# Same threshold the /cagr page uses for open positions.
MIN_DAYS_FOR_CAGR = 30


def _build_ltp_fn() -> callable:
    """Build a callable(ticker) -> ltp that fetches LTPs from yfinance
    for the user's currently-held equity positions only.

    Returns None on any failure (the caller should fall back to buy_price).
    Caches the result for the lifetime of the CLI invocation.
    """
    cache: dict[str, Optional[float]] = {}
    def ltp_fn(ticker: str) -> Optional[float]:
        if ticker in cache:
            return cache[ticker]
        cache[ticker] = None
        try:
            from pipeline.portfolio_truth import load_truth
            truth = load_truth()
            open_tickers = list(truth.get("equity", {}).keys())
            if ticker.upper() not in [t.upper() for t in open_tickers]:
                return None
            import yfinance as yf
            t = yf.Ticker(f"{ticker}.NS")
            h = t.history(period="5d")
            if len(h) > 0:
                cache[ticker] = float(h["Close"].iloc[-1])
        except Exception:
            pass
        return cache[ticker]
    return ltp_fn


def _safe_cagr(stock: float, ref: float, years: float) -> Optional[float]:
    if years is None or years <= 0 or ref is None or ref <= 0 or stock is None or stock <= 0:
        return None
    return cagr_fn(stock, ref, years)


def _resolve_benchmark(name: str) -> tuple[str, bool, bool]:
    """Given a desired benchmark, return (actual_benchmark, available, used_fallback)."""
    if is_available(name):
        return name, True, False
    # Fallback: Nifty50
    if name != "nifty50" and is_available("nifty50"):
        return "nifty50", True, True
    return name, False, False


def _lot_pnl_for_cohort(entry: LedgerEntry, asof: date, current_ltp_fn) -> tuple[float, float, float]:
    """For one ledger entry, return (buy_value, current_value_or_sell_value, pnl).

    current_ltp_fn(ticker) -> Optional[float] is the live LTP fetcher.
    Open positions contribute (qty × ltp). Closed positions contribute
    their sell_value. P&L is current_value - buy_value.
    """
    if entry.is_open:
        ltp = current_ltp_fn(entry.ticker) or entry.buy_price
        cur = entry.qty * ltp
    else:
        cur = entry.sell_value
    return (entry.buy_value, cur, cur - entry.buy_value)


def compute_cohorts(current_ltp_fn=None, longterm_only: bool = False,
                    start_date: Optional[date] = None,
                    min_holding_days: int = 0) -> dict:
    """Compute all cohort results. Returns a dict with cohort details + index availability.

    Args:
        current_ltp_fn: callable(ticker) -> Optional[float] for live LTP.
            Defaults to using avg_price as a fallback (so the function
            still works without the broker).
        longterm_only: if True, only count lots held > 1 year (> 365 days).
            Per Indian Income Tax Act, equity held > 12 months is LT.
        start_date: anchor date for the benchmark CAGR. If None, uses
            the cohort's own earliest buy date. If a date, all three
            cohorts use the same anchor (so CAGRs are directly
            comparable across tiers). Recommended: your very first
            stock purchase date — 2023-12-20 for this portfolio.
        min_holding_days: skip any lot held for fewer than this many days.
            Day-trades (< 30 days) have annualised CAGRs that are
            mathematically correct but practically meaningless.
            Default 0 (include everything; set 30 or 90 to filter noise).
    """
    if current_ltp_fn is None:
        def current_ltp_fn(t):
            return None  # falls back to avg_price in _lot_pnl_for_cohort

    ledger = build_ledger()
    today = date.today()

    # Default start_date = your first ever stock purchase, so all three
    # cohorts are measured against their indices over the same window
    # ("alpha since inception"). Pass start_date=... to override.
    if start_date is None and ledger:
        first_buys = [e.buy_date for e in ledger if e.buy_date]
        if first_buys:
            start_date = min(first_buys)

    if longterm_only:
        # Filter ledger to only longterm lots
        ledger = [e for e in ledger if _is_longterm(e, today)]
    if min_holding_days > 0:
        ledger = [e for e in ledger if _holding_days(e, today) >= min_holding_days]
    out: dict = {
        "asof": today.isoformat(),
        "longterm_only": longterm_only,
        "longterm_days_threshold": LONGTERM_DAYS,
        "min_holding_days": min_holding_days,
        "start_date": start_date.isoformat() if start_date else None,
        "indices_available": available_indices(),
    }

    for cohort_name, cfg in COHORTS.items():
        pred = cfg["filter"]
        entries = [e for e in ledger if pred(e)]
        # Per-cohort longterm filter (large-cap only, per user request)
        if cfg.get("longterm_only"):
            entries = [e for e in entries if _is_longterm(e, today)]
        # Per-cohort min-holding-days filter (skip day-trades by default)
        cohort_min_days = cfg.get("min_holding_days", 0)
        if cohort_min_days > 0:
            entries = [e for e in entries if _holding_days(e, today) >= cohort_min_days]
        # Build ticker-level aggregation
        per_ticker: dict[str, dict] = {}
        for e in entries:
            tk = e.ticker
            if tk not in per_ticker:
                per_ticker[tk] = {
                    "ticker": tk,
                    "tier": e.tier,
                    "is_etf": e.is_etf,
                    "open_qty": 0,
                    "open_cost": 0.0,        # qty × buy_price for open positions
                    "open_value": 0.0,        # qty × ltp for open positions
                    "closed_qty": 0,
                    "closed_buy_value": 0.0,
                    "closed_sell_value": 0.0,
                    "closed_pnl": 0.0,
                    "earliest_buy": None,
                    "latest_buy": None,
                }
            t = per_ticker[tk]
            if e.is_open:
                t["open_qty"] += e.qty
                ltp = current_ltp_fn(tk) or e.buy_price
                t["open_value"] += e.qty * ltp
                t["open_cost"] += e.buy_value
                t["earliest_buy"] = e.buy_date.isoformat() if (
                    t["earliest_buy"] is None or e.buy_date.isoformat() < t["earliest_buy"]
                ) else t["earliest_buy"]
                t["latest_buy"] = e.buy_date.isoformat() if (
                    t["latest_buy"] is None or e.buy_date.isoformat() > t["latest_buy"]
                ) else t["latest_buy"]
            else:
                t["closed_qty"] += e.qty
                t["closed_buy_value"] += e.buy_value
                t["closed_sell_value"] += e.sell_value
                t["closed_pnl"] += e.pnl
                # Track buy dates for closed lots too
                if t["earliest_buy"] is None or e.buy_date.isoformat() < t["earliest_buy"]:
                    t["earliest_buy"] = e.buy_date.isoformat()
                if t["latest_buy"] is None or e.buy_date.isoformat() > t["latest_buy"]:
                    t["latest_buy"] = e.buy_date.isoformat()

        # Cohort aggregates
        # total_invested = total cost basis (buy prices for both open and closed)
        # total_current = open at LTP + closed at sell price
        # total_pnl = total_current - total_invested
        total_invested = sum(t["open_cost"] + t["closed_buy_value"] for t in per_ticker.values())
        open_value = sum(t["open_value"] for t in per_ticker.values())
        realized_pnl = sum(t["closed_pnl"] for t in per_ticker.values())
        total_current = open_value + sum(t["closed_sell_value"] for t in per_ticker.values())
        total_pnl = total_current - total_invested
        total_return_pct = (total_pnl / total_invested * 100) if total_invested else 0.0

        # Time metrics: weighted by invested (open_value + closed_buy)
        # Years = today - buy_date for each lot
        weighted_years_sum = 0.0
        weighted_years_weight = 0.0
        earliest_buy = None
        latest_buy = None
        n_open = 0
        n_closed = 0
        for e in entries:
            years = (today - e.buy_date).days / 365.25
            weight = e.buy_value
            weighted_years_sum += years * weight
            weighted_years_weight += weight
            if earliest_buy is None or e.buy_date < earliest_buy:
                earliest_buy = e.buy_date
            if latest_buy is None or e.buy_date > latest_buy:
                latest_buy = e.buy_date
            if e.is_open:
                n_open += 1
            else:
                n_closed += 1
        weighted_years = (weighted_years_sum / weighted_years_weight) if weighted_years_weight else 0.0

        # CAGR
        cagr = _safe_cagr(1.0 + total_return_pct / 100.0, 1.0, weighted_years) if weighted_years else None

        # Benchmark. The benchmark's start date is the *external* start_date
        # if provided (so all cohorts share a common anchor — your very first
        # purchase, e.g. 2023-12-20), otherwise the cohort's own earliest_buy.
        # The portfolio's own CAGR is always measured over weighted_years
        # (time since the cohort's own first buy) — the benchmark CAGR is
        # rescaled to that same window so the alpha is apples-to-apples.
        bench_name, bench_avail, used_fallback = _resolve_benchmark(cfg["benchmark"])
        bench_anchor = start_date if start_date else earliest_buy
        bench_then = None
        bench_cagr = None
        bench_alpha = None
        if bench_avail and bench_anchor:
            bench_then = close_on(bench_name, bench_anchor)
            if bench_then:
                # Benchmark CAGR over the SAME window as the portfolio,
                # so the alpha is apples-to-apples regardless of anchor.
                bench_cagr = cagr_fn(
                    close_on(bench_name, today) or bench_then,
                    bench_then,
                    weighted_years,
                )
        if cagr is not None and bench_cagr is not None:
            bench_alpha = cagr - bench_cagr

        # Per-ticker detail
        tickers_detail = []
        for tk, t in sorted(per_ticker.items()):
            # invested = cost basis (open at buy_price + closed at buy_price)
            # current  = market value (open at LTP + closed at sell_price)
            inv = t["open_cost"] + t["closed_buy_value"]
            cur = t["open_value"] + t["closed_sell_value"]
            ret_pct = ((cur - inv) / inv * 100) if inv else 0.0
            # Per-ticker CAGR — mark as unreliable for very short holding
            # periods (CAGR of a sub-30-day position is mathematically
            # correct but practically meaningless).
            t_years = 0.0
            if t["earliest_buy"]:
                t_years = (today - date.fromisoformat(t["earliest_buy"])).days / 365.25
            t_cagr = _safe_cagr(1.0 + ret_pct / 100.0, 1.0, t_years) if t_years > 0 else None
            unreliable = (t_years * 365.25) < MIN_DAYS_FOR_CAGR
            tickers_detail.append({
                "ticker": tk,
                "tier": t["tier"],
                "open_qty": t["open_qty"],
                "open_value": round(t["open_value"], 2),
                "closed_qty": t["closed_qty"],
                "closed_buy_value": round(t["closed_buy_value"], 2),
                "closed_sell_value": round(t["closed_sell_value"], 2),
                "closed_pnl": round(t["closed_pnl"], 2),
                "earliest_buy": t["earliest_buy"],
                "latest_buy": t["latest_buy"],
                "holding_days": int(t_years * 365.25),
                "cagr_pct": round(t_cagr, 2) if t_cagr is not None else None,
                "cagr_unreliable": unreliable,
            })

        out[cohort_name] = CohortResult(
            name=cohort_name,
            label=cfg["label"],
            benchmark=cfg["benchmark"],
            benchmark_available=bench_avail,
            benchmark_using_fallback=used_fallback,
            fallback_to=bench_name if not used_fallback else "nifty50",
            n_lots=len(entries),
            n_open=n_open,
            n_closed=n_closed,
            n_tickers=len(per_ticker),
            tickers=sorted(per_ticker.keys()),
            total_invested=round(total_invested, 2),
            open_value=round(open_value, 2),
            realized_pnl=round(realized_pnl, 2),
            total_current=round(total_current, 2),
            total_pnl=round(total_pnl, 2),
            total_return_pct=round(total_return_pct, 2),
            earliest_buy=earliest_buy.isoformat() if earliest_buy else None,
            latest_buy=latest_buy.isoformat() if latest_buy else None,
            weighted_years=round(weighted_years, 3),
            cagr_pct=round(cagr, 2) if cagr is not None else None,
            nifty_cagr_pct=round(bench_cagr, 2) if bench_cagr is not None else None,
            alpha_pct=round(bench_alpha, 2) if bench_alpha is not None else None,
            tickers_detail=tickers_detail,
        ).__dict__ if False else None
        # Build manually so we control the output dict
        out[cohort_name] = {
            "name": cohort_name,
            "label": cfg["label"],
            "benchmark": cfg["benchmark"],
            "benchmark_available": bench_avail,
            "benchmark_using_fallback": used_fallback,
            "fallback_to": bench_name,
            "n_lots": len(entries),
            "n_open": n_open,
            "n_closed": n_closed,
            "n_tickers": len(per_ticker),
            "tickers": sorted(per_ticker.keys()),
            "total_invested": round(total_invested, 2),
            "open_value": round(open_value, 2),
            "realized_pnl": round(realized_pnl, 2),
            "total_current": round(total_current, 2),
            "total_pnl": round(total_pnl, 2),
            "total_return_pct": round(total_return_pct, 2),
            "earliest_buy": earliest_buy.isoformat() if earliest_buy else None,
            "latest_buy": latest_buy.isoformat() if latest_buy else None,
            "weighted_years": round(weighted_years, 3),
            "cagr_pct": round(cagr, 2) if cagr is not None else None,
            "nifty_cagr_pct": round(bench_cagr, 2) if bench_cagr is not None else None,
            "alpha_pct": round(bench_alpha, 2) if bench_alpha is not None else None,
            "tickers_detail": tickers_detail,
        }

    return out


# CLI
if __name__ == "__main__":
    import json
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--json", action="store_true")
    p.add_argument("--longterm-only", action="store_true",
                   help="Only include lots held > 1 year (Indian IT Act LT threshold)")
    p.add_argument("--no-ltp", action="store_true",
                   help="Skip yfinance LTP fetch for open positions (faster, open P&L = 0)")
    p.add_argument("--start-date", type=str, default=None,
                   help="Anchor date YYYY-MM-DD for benchmark CAGR (e.g. your first buy date). "
                        "If set, all cohorts use the same anchor so CAGRs are directly comparable.")
    p.add_argument("--min-holding-days", type=int, default=0,
                   help="Skip lots held for fewer than N days (filter out day-trades). "
                        "Recommended: 30. 0 = include everything.")
    args = p.parse_args()

    sd = None
    if args.start_date:
        try:
            from datetime import date as _d
            sd = _d.fromisoformat(args.start_date)
        except ValueError:
            print(f"BAD: --start-date must be YYYY-MM-DD, got {args.start_date!r}", file=sys.stderr)
            sys.exit(1)

    ltp_fn = None
    if not args.no_ltp:
        ltp_fn = _build_ltp_fn()
    result = compute_cohorts(current_ltp_fn=ltp_fn, longterm_only=args.longterm_only,
                            start_date=sd, min_holding_days=args.min_holding_days)
    if args.json:
        print(json.dumps(result, indent=2, default=str))
    else:
        print(f"\nAs of: {result['asof']}")
        if result.get("longterm_only"):
            print(f"Filter: LONGTERM ONLY (held > {result['longterm_days_threshold']} days)")
        if result.get("min_holding_days", 0) > 0:
            print(f"Filter: min holding {result['min_holding_days']} days (day-trades excluded)")
        if result.get("start_date"):
            print(f"Benchmark anchor: {result['start_date']} (your first buy — CAGRs are directly comparable across tiers)")
        else:
            print("Benchmark anchor: cohort's own earliest buy (each cohort measured over its own window)")
        print()
        for cname, cdata in result.items():
            if cname in ("asof", "indices_available", "longterm_only",
                         "longterm_days_threshold", "start_date"):
                continue
            if not isinstance(cdata, dict):
                continue
            print(f"\n=== {cdata['label']} ===")
            bench_disp = BENCHMARK_LABELS.get(cdata['benchmark'], cdata['benchmark'])
            bench_actual = BENCHMARK_LABELS.get(cdata['fallback_to'], cdata['fallback_to'])
            fb_note = f" [fallback: {bench_actual}]" if cdata['benchmark_using_fallback'] else ""
            print(f"  Benchmark: {bench_disp}{fb_note}")
            print(f"  Tickers: {cdata['n_tickers']}  ({', '.join(cdata['tickers'])})")
            print(f"  Lots: {cdata['n_lots']} ({cdata['n_open']} open, {cdata['n_closed']} closed)")
            print(f"  Total invested:   \u20b9{cdata['total_invested']:>12,.0f}")
            print(f"  Open value:       \u20b9{cdata['open_value']:>12,.0f}")
            print(f"  Realized P&L:     \u20b9{cdata['realized_pnl']:>+12,.0f}")
            print(f"  Total current:    \u20b9{cdata['total_current']:>12,.0f}")
            print(f"  Total P&L:        \u20b9{cdata['total_pnl']:>+12,.0f}  ({cdata['total_return_pct']:+.2f}%)")
            print(f"  Weighted years:   {cdata['weighted_years']:.2f}")
            print(f"  CAGR:             {cdata['cagr_pct'] or 0:+.2f}%  vs {bench_disp}: {cdata['nifty_cagr_pct'] or 0:+.2f}%  alpha: {cdata['alpha_pct'] or 0:+.2f}%")
            if cdata.get("tickers_detail"):
                print(f"  Per-ticker detail:")
                from datetime import date as _date
                _today = _date.today()
                for td in cdata["tickers_detail"]:
                    days_ago = "n/a"
                    if td.get("earliest_buy"):
                        d = _date.fromisoformat(td["earliest_buy"])
                        days_ago = f"{(_today - d).days}d"
                    if td.get("cagr_unreliable"):
                        cagr_str = "n/a (<30d)"
                    elif td['cagr_pct'] is not None:
                        cagr_str = f"{td['cagr_pct']:+.2f}%"
                    else:
                        cagr_str = "n/a"
                    print(f"    {td['ticker']:<14} CAGR {cagr_str}  open={td['open_qty']}  closed={td['closed_qty']}  since {td['earliest_buy']} ({days_ago})")
