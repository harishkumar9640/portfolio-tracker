"""
pipeline.xirr
-------------
XIRR (Extended Internal Rate of Return) calculation for the portfolio.

XIRR is the discount rate that makes the NPV of all cash flows equal
zero, with dates in actual calendar days (not 30/360 like IRR). It's
the right metric for portfolios where you invest at irregular
intervals (e.g. SIPs, multiple buy events) because each cash flow is
discounted by its actual time from the first cash flow.

Definition
----------
For cash flows C_1, C_2, ..., C_n at dates d_1, d_2, ..., d_n:
    NPV(r) = sum_i  C_i / (1 + r)^((d_i - d_0) / 365.25) = 0
where d_0 is the earliest cash-flow date. The XIRR is the r that
satisfies this equation.

We use 365.25 days/year (the standard for XIRR per Excel / OpenOffice
specifications).

Cash flow construction
-----------------------
For the user's portfolio:
  * Outflows (negative): every buy event in the Delivery P&L xlsx
    (qty * buy_price) at buy_date
  * Inflows  (positive): every sell event
    (qty * sell_price) at sell_date
  * Terminal value (positive): sum of (qty * ltp) for open positions
    at today's date

API
---
  from pipeline.xirr import compute_xirr
  result = compute_xirr(current_ltp_fn=lambda t: ltps.get(t))
  # result is a dict with rate, annualized, money-weighted return
  # plus cash flow details for transparency.

Performance vs CAGR
-------------------
- CAGR is a point-in-time measurement. It assumes the same total
  return over the entire window and ignores the timing of cash
  flows.
- XIRR (money-weighted) accounts for *when* you put money in. If
  you invested ₹10L in March 2024 and it doubled, but you also
  invested ₹90L in May 2026 and it dropped 5%, your time-weighted
  CAGR might be negative but your XIRR is positive (you made money
  on the big March bet, lost a bit on the recent one). XIRR is the
  number professional investors quote.

Newton-Raphson solver
---------------------
We use Newton-Raphson (max 100 iterations) to find the root of
NPV(r) = 0, with bracket-fallback to bisection if Newton diverges.
The derivative d(NPV)/dr is also analytic so Newton converges in
~5-10 iterations for any reasonable portfolio.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Callable, Optional

from pipeline.ledger import build_ledger, LedgerEntry
from pipeline.logging_setup import get_logger

log = get_logger("xirr")

PROJECT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# Solver
# ---------------------------------------------------------------------------

def _npv(rate: float, cashflows: list[tuple[date, float]],
         d0: date) -> float:
    """Net present value of the cashflows at the given annual rate."""
    if rate <= -1.0:
        return float("inf")
    npv = 0.0
    for d, c in cashflows:
        t = (d - d0).days / 365.25
        npv += c / ((1.0 + rate) ** t)
    return npv


def _npv_deriv(rate: float, cashflows: list[tuple[date, float]],
               d0: date) -> float:
    """Derivative of NPV w.r.t. rate (analytic)."""
    if rate <= -1.0:
        return float("inf")
    d = 0.0
    for dt, c in cashflows:
        t = (dt - d0).days / 365.25
        if t == 0:
            continue
        d += -t * c / ((1.0 + rate) ** (t + 1.0))
    return d


def _xirr_newton(cashflows: list[tuple[date, float]],
                d0: date,
                guess: float = 0.1,
                tol: float = 1e-7,
                max_iter: int = 100) -> Optional[float]:
    """Newton-Raphson solver for XIRR. Returns rate or None on failure."""
    if not cashflows:
        return None
    rate = guess
    for i in range(max_iter):
        try:
            f = _npv(rate, cashflows, d0)
            fp = _npv_deriv(rate, cashflows, d0)
        except (OverflowError, ZeroDivisionError):
            return None
        if abs(fp) < 1e-12:
            # Derivative too small; fall back to bisection
            return _xirr_bisect(cashflows, d0, tol=tol)
        delta = f / fp
        new_rate = rate - delta
        # Keep the rate in a sensible range; Newton's method can
        # wander to negative territory or to very large values.
        if new_rate <= -0.99:
            new_rate = -0.99
        if abs(new_rate - rate) < tol:
            return new_rate
        rate = new_rate
    # Didn't converge in max_iter iterations
    return None


def _xirr_bisect(cashflows: list[tuple[date, float]],
                d0: date,
                lo: float = -0.999,
                hi: float = 10.0,
                tol: float = 1e-7,
                max_iter: int = 200) -> Optional[float]:
    """Bisection fallback when Newton's method fails or oscillates."""
    try:
        f_lo = _npv(lo, cashflows, d0)
        f_hi = _npv(hi, cashflows, d0)
    except (OverflowError, ZeroDivisionError):
        return None
    if math.isnan(f_lo) or math.isnan(f_hi) or math.isinf(f_lo) or math.isinf(f_hi):
        return None
    # If there's no sign change, the root isn't bracketed
    if f_lo * f_hi > 0:
        return None
    for _ in range(max_iter):
        mid = (lo + hi) / 2.0
        try:
            f_mid = _npv(mid, cashflows, d0)
        except (OverflowError, ZeroDivisionError):
            return None
        if abs(f_mid) < tol or (hi - lo) < tol:
            return mid
        if f_lo * f_mid < 0:
            hi = mid
        else:
            lo = mid
            f_lo = f_mid
    return (lo + hi) / 2.0


def xirr(cashflows: list[tuple[date, float]],
         guess: float = 0.1) -> Optional[float]:
    """Compute the XIRR for a list of (date, cashflow) tuples.

    Cash flows are interpreted as: positive = money in, negative = money out.
    The XIRR is the annual rate that makes NPV=0.
    """
    if not cashflows:
        return None
    # Filter out any None dates (defensive)
    cashflows = [(d, c) for d, c in cashflows if d is not None]
    if not cashflows:
        return None
    cashflows.sort(key=lambda x: x[0])
    d0 = cashflows[0][0]

    # Try Newton first
    rate = _xirr_newton(cashflows, d0, guess=guess)
    if rate is None:
        # Fall back to bisection with a wider bracket
        rate = _xirr_bisect(cashflows, d0)
    return rate


# ---------------------------------------------------------------------------
# Build cash flows from the user's ledger
# ---------------------------------------------------------------------------

def _build_cashflows(entries: list[LedgerEntry],
                    today: date,
                    current_ltp_fn: Optional[Callable[[str], Optional[float]]] = None
                    ) -> tuple[list[tuple[date, float]], dict]:
    """Build the XIRR cash flow list from ledger entries.

    Returns (cashflows, stats) where stats is a dict with totals
    for the report (invested, realized, open_value, etc.).
    """
    flows: list[tuple[date, float]] = []
    total_invested = 0.0  # sum of all buy values
    total_realized = 0.0  # sum of sell_value for closed lots
    open_value = 0.0      # sum of qty * ltp for open positions

    for e in entries:
        if e.buy_date is None:
            continue
        # Outflow: invest e.buy_value on e.buy_date
        flows.append((e.buy_date, -e.buy_value))
        total_invested += e.buy_value
        if e.sell_date is not None:
            # Inflow: receive e.sell_value on e.sell_date
            flows.append((e.sell_date, +e.sell_value))
            total_realized += e.sell_value
        else:
            # Open position: add terminal value at today
            ltp = None
            if current_ltp_fn is not None:
                try:
                    ltp = current_ltp_fn(e.ticker)
                except Exception:
                    ltp = None
            if ltp is None or ltp <= 0:
                ltp = e.buy_price  # fallback
            val = e.qty * ltp
            open_value += val

    # Terminal inflow: open positions sold at today's LTP
    if open_value > 0:
        flows.append((today, +open_value))

    stats = {
        "total_invested": total_invested,
        "total_realized": total_realized,
        "open_value": open_value,
        "n_lots": len(entries),
        "n_open_lots": sum(1 for e in entries if e.is_open),
        "n_closed_lots": sum(1 for e in entries if e.is_closed),
    }
    return flows, stats


def compute_xirr(current_ltp_fn: Optional[Callable[[str], Optional[float]]] = None,
                 include_etfs: bool = False) -> dict:
    """Compute XIRR for the user's full equity portfolio.

    Args:
        current_ltp_fn: callable(ticker) -> Optional[float] for live LTP.
            Defaults to None (uses buy_price as fallback for open lots).
        include_etfs: if False (default), ETFs are excluded from the
            XIRR calculation (they're not equity holdings per the
            user's request). If True, ETFs are included.

    Returns:
        dict with keys:
            - xirr_pct: XIRR as a percentage (e.g. 12.34 means 12.34%)
            - xirr: XIRR as a decimal (e.g. 0.1234)
            - total_invested: total cost basis
            - total_realized: sum of sell_value for closed lots
            - open_value: current market value of open positions
            - total_return_pct: (realized + open_value - invested) / invested * 100
            - earliest_date: first cashflow date
            - latest_date: last cashflow date
            - n_lots, n_open_lots, n_closed_lots
            - cashflows: list of (date, amount) for transparency
            - converged: True/False (False if solver didn't converge)
    """
    today = date.today()
    ledger = build_ledger()
    if not include_etfs:
        ledger = [e for e in ledger if not e.is_etf]

    flows, stats = _build_cashflows(ledger, today, current_ltp_fn)

    if not flows:
        return {
            "xirr_pct": None,
            "xirr": None,
            "converged": False,
            "error": "no cashflows to compute",
            **stats,
        }

    rate = xirr(flows)
    converged = rate is not None

    if rate is None:
        # No convergence. Fall back to a "simple" total return CAGR
        # so the caller has *something* to display.
        if stats["total_invested"] > 0 and flows:
            d0 = flows[0][0]
            years = (today - d0).days / 365.25
            total_final = stats["total_realized"] + stats["open_value"]
            total_return = total_final / stats["total_invested"]
            rate = (total_return ** (1.0 / years) - 1.0) if years > 0 else 0.0
        else:
            rate = 0.0

    total_return_pct = (
        (stats["total_realized"] + stats["open_value"]) / stats["total_invested"] - 1.0
    ) * 100 if stats["total_invested"] > 0 else 0.0

    return {
        "xirr_pct": round(rate * 100, 2),
        "xirr": round(rate, 6),
        "converged": converged,
        "total_invested": round(stats["total_invested"], 2),
        "total_realized": round(stats["total_realized"], 2),
        "open_value": round(stats["open_value"], 2),
        "total_return_pct": round(total_return_pct, 2),
        "earliest_date": flows[0][0].isoformat() if flows else None,
        "latest_date": flows[-1][0].isoformat() if flows else None,
        "n_lots": stats["n_lots"],
        "n_open_lots": stats["n_open_lots"],
        "n_closed_lots": stats["n_closed_lots"],
        "cashflows": [(d.isoformat(), round(c, 2)) for d, c in flows],
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse
    import json
    import sys

    sys.path.insert(0, str(PROJECT))
    from pipeline.cohorts import _build_ltp_fn

    p = argparse.ArgumentParser()
    p.add_argument("--json", action="store_true", help="Output JSON")
    p.add_argument("--include-etfs", action="store_true", help="Include ETFs")
    p.add_argument("--no-ltp", action="store_true", help="Don't fetch LTPs (faster)")
    args = p.parse_args()

    ltp_fn = None if args.no_ltp else _build_ltp_fn()
    result = compute_xirr(current_ltp_fn=ltp_fn, include_etfs=args.include_etfs)

    if args.json:
        print(json.dumps(result, indent=2, default=str))
    else:
        print(f"\nXIRR for full equity portfolio (ETFs {'included' if args.include_etfs else 'excluded'})\n")
        if result["converged"]:
            print(f"  XIRR:              {result['xirr_pct']:+.2f}%")
        else:
            print(f"  XIRR:              did not converge (using simple CAGR fallback)")
            print(f"  Simple total CAGR:  {result['xirr_pct']:+.2f}%")
        print(f"  Total invested:     ₹{result['total_invested']:>12,.0f}")
        print(f"  Realized (closed):  ₹{result['total_realized']:>12,.0f}")
        print(f"  Open value:         ₹{result['open_value']:>12,.0f}")
        print(f"  Total current:      ₹{result['total_realized'] + result['open_value']:>12,.0f}")
        print(f"  Total P&L:          ₹{(result['total_realized'] + result['open_value']) - result['total_invested']:>+12,.0f}  ({result['total_return_pct']:+.2f}%)")
        print(f"  Lots: {result['n_lots']} ({result['n_open_lots']} open, {result['n_closed_lots']} closed)")
        print(f"  Window: {result['earliest_date']} → {result['latest_date']}")
