"""
portfolio_monitor.holdings
-------------------------
Single source of truth for the live equity portfolio.

Tries (in order):
  1. Angel One SmartAPI (broker) — freshest, real-time LTP
  2. yfinance fallback (when broker call fails or returns empty)
  3. .env.example-tagged static list — last resort

Used by all three monitor scripts.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Optional

from pipeline.logging_setup import get_logger

log = get_logger("portfolio_monitor")

PROJECT = Path(__file__).resolve().parents[2]

# Single source of truth for current holdings. The truth file is the
# canonical reference; the static list below is the last-known fallback
# for when the broker AND the truth file are both unavailable.
from pipeline.portfolio_truth import load_truth

# Static fallback — what to use if both broker and yfinance fail.
# Prices here are last known; they're "stale" labels, not real data.
# (Keep in sync with my_tickers.txt; broker is the source of truth at runtime.)
STATIC_HOLDINGS: list[dict] = [
    {"symbol": "UNOMINDA-EQ",  "ticker": "UNOMINDA",  "qty":  36, "avg": 1096.70},
    {"symbol": "NEXT50IETF-EQ", "ticker": "NEXT50IETF", "qty":  36, "avg":   77.21},
    {"symbol": "BALRAMCHIN-EQ", "ticker": "BALRAMCHIN", "qty":  33, "avg":  532.27},
    {"symbol": "NTPCGREEN-EQ",  "ticker": "NTPCGREEN",  "qty": 700, "avg":   92.79},
    {"symbol": "ITC-EQ",        "ticker": "ITC",        "qty": 300, "avg":  331.93},
    {"symbol": "METALIETF-EQ",  "ticker": "METALIETF",  "qty":1500, "avg":    8.40},
    {"symbol": "RELIANCE-EQ",   "ticker": "RELIANCE",   "qty":  60, "avg": 1250.52},
    {"symbol": "BANKBARODA-EQ", "ticker": "BANKBARODA", "qty": 181, "avg":  238.71},
    {"symbol": "KNRCON-EQ",     "ticker": "KNRCON",     "qty": 200, "avg":  150.67},
    {"symbol": "JIOFIN-EQ",     "ticker": "JIOFIN",     "qty": 300, "avg":  256.24},
    {"symbol": "GOLDBEES-EQ",   "ticker": "GOLDBEES",   "qty": 300, "avg":   81.42},
]

# Sector / theme classification (for sector-mix reporting)
SECTOR_MAP: dict[str, str] = {
    "UNOMINDA":   "Auto Components",
    "NEXT50IETF": "Index ETF",
    "BALRAMCHIN": "Specialty Chemicals",
    "NTPCGREEN":  "Renewable Power",
    "ITC":        "FMCG",
    "METALIETF":  "Metals ETF",
    "RELIANCE":   "Energy Conglomerate",
    "BANKBARODA": "PSU Bank",
    "KNRCON":     "Infra Construction",
    "JIOFIN":     "NBFC / Fintech",
    "GOLDBEES":   "Gold ETF",
    "IRCON":      "Infra Construction",  # legacy
    "PIDILITIND": "Specialty Chemicals", # alternative add (we ended up on UNOMINDA)
}

# Index tier — for large-cap concentration reporting
LARGE_CAP = {"RELIANCE", "ITC", "HDFCBANK", "ICICIBANK", "SBIN",
            "BHARTIARTL", "INFY", "TCS", "LT", "HINDUNILVR", "HCLTECH",
            "AXISBANK", "BAJFINANCE", "MARUTI", "SUNPHARMA", "NTPC",
            "POWERGRID", "MANDM", "TITAN", "ULTRACEMCO", "ASIANPAINT"}


@dataclass
class Position:
    ticker: str
    qty: int
    avg: float
    ltp: float
    sector: str
    source: str  # "broker" | "yfinance" | "static"
    asof: str    # ISO timestamp

    @property
    def cur_value(self) -> float:
        return self.qty * self.ltp

    @property
    def invested(self) -> float:
        return self.qty * self.avg

    @property
    def pnl(self) -> float:
        return self.cur_value - self.invested

    @property
    def pnl_pct(self) -> float:
        return (self.ltp / self.avg - 1.0) * 100.0 if self.avg else 0.0

    @property
    def weight(self) -> float:
        return 0.0  # filled in by snapshot()

    def to_dict(self) -> dict:
        d = asdict(self)
        d.update({
            "cur_value": round(self.cur_value, 2),
            "invested": round(self.invested, 2),
            "pnl": round(self.pnl, 2),
            "pnl_pct": round(self.pnl_pct, 2),
        })
        return d


def _from_broker() -> Optional[list[Position]]:
    """Fetch from Angel One SmartAPI. Returns None on failure."""
    try:
        from pipeline.angel_client import fetch_holdings as _fetch
        hs = _fetch()
        if not hs:
            return None
        now = datetime.now().isoformat(timespec="seconds")
        out: list[Position] = []
        for h in hs:
            # Skip zero-qty (closed positions)
            if h.quantity <= 0:
                continue
            tk = h.symbol.replace("-EQ", "").upper()
            out.append(Position(
                ticker=tk,
                qty=h.quantity,
                avg=h.avg_price,
                ltp=h.ltp,
                sector=SECTOR_MAP.get(tk, "Unclassified"),
                source="broker",
                asof=now,
            ))
        return out
    except Exception as e:
        log.warning("broker fetch failed: %s", e)
        return None


def _from_yfinance(tickers: list[str]) -> dict[str, float]:
    """Get LTP from yfinance for tickers not returned by broker."""
    import yfinance as yf
    import time
    out: dict[str, float] = {}
    for t in tickers:
        try:
            sym = f"{t}.NS"
            info = yf.Ticker(sym).info or {}
            time.sleep(0.2)
            p = info.get("currentPrice") or info.get("regularMarketPrice") or 0
            if p > 0:
                out[t] = float(p)
        except Exception as e:
            log.warning("yfinance fetch failed for %s: %s", t, e)
    return out


def _from_static(ltp_overrides: dict[str, float] | None = None) -> list[Position]:
    """Use the truth file as the source of truth; fall back to STATIC_HOLDINGS
    if the truth file is unavailable or has no equity section."""
    now = datetime.now().isoformat(timespec="seconds")
    rows: list[dict] = []
    source_label = "static"
    try:
        truth = load_truth()
        equity = truth.get("equity", {})
        if equity:
            rows = [
                {
                    "ticker": pos["ticker"],
                    "qty": pos["qty"],
                    "avg": pos["avg_price"],
                    "src": "truth",
                }
                for pos in equity.values()
                if pos.get("qty", 0) > 0
            ]
            source_label = "truth"
    except Exception as e:
        log.warning("could not read truth file for static fallback: %s", e)
    if not rows:
        rows = [{"ticker": h["ticker"], "qty": h["qty"], "avg": h["avg"], "src": "static"}
                for h in STATIC_HOLDINGS]
    out: list[Position] = []
    for h in rows:
        tk = h["ticker"]
        ltp = (ltp_overrides or {}).get(tk, h["avg"])  # default to avg if no LTP
        out.append(Position(
            ticker=tk,
            qty=h["qty"],
            avg=h["avg"],
            ltp=ltp,
            sector=SECTOR_MAP.get(tk, "Unclassified"),
            source=h["src"] if ltp_overrides else source_label,
            asof=now,
        ))
    return out


def get_positions(force_yfinance: bool = False) -> tuple[list[Position], str]:
    """
    Returns (positions, source) where source is one of:
      "broker"     — fresh data from Angel One
      "yfinance"   — broker failed, fetched via yfinance
      "mixed"      — broker for some, yfinance for others
      "static"     — both failed; static list with avg as ltp (stale)
    """
    if not force_yfinance:
        broker = _from_broker()
        if broker:
            return broker, "broker"
        log.warning("broker returned no positions; falling back to yfinance")

    # Fallback path: yfinance
    tickers = [h["ticker"] for h in STATIC_HOLDINGS]
    ltps = _from_yfinance(tickers)
    if ltps:
        positions = _from_static(ltp_overrides=ltps)
        # Mark source per-position
        for p in positions:
            if p.ticker in ltps:
                p.source = "yfinance"
        return positions, "yfinance" if not force_yfinance else "yfinance"

    log.error("all data sources failed; returning static list with stale LTPs")
    return _from_static(), "static"


def get_snapshot(force_yfinance: bool = False) -> dict:
    """Full snapshot with totals, weights, and source metadata."""
    positions, source = get_positions(force_yfinance=force_yfinance)
    total_cv = sum(p.cur_value for p in positions) or 1.0
    snap = {
        "asof": datetime.now().isoformat(timespec="seconds"),
        "source": source,
        "total_value": round(total_cv, 2),
        "total_pnl": round(sum(p.pnl for p in positions), 2),
        "positions": [],
    }
    for p in positions:
        d = p.to_dict()
        d["weight"] = round(p.cur_value / total_cv * 100, 2)
        snap["positions"].append(d)
    # Sort by value desc
    snap["positions"].sort(key=lambda x: -x["cur_value"])
    return snap


def get_concentration() -> dict:
    """Top-1, top-2, top-3 weight as % of total. Used by all three scripts."""
    positions, source = get_positions()
    total_cv = sum(p.cur_value for p in positions) or 1.0
    weights = sorted(
        [(p.ticker, p.cur_value / total_cv * 100) for p in positions],
        key=lambda x: -x[1],
    )
    return {
        "asof": datetime.now().isoformat(timespec="seconds"),
        "source": source,
        "total_value": round(total_cv, 2),
        "weights": weights,  # [(ticker, weight%), ...] sorted desc
        "top1_pct": weights[0][1] if len(weights) > 0 else 0,
        "top2_pct": sum(w for _, w in weights[:2]),
        "top3_pct": sum(w for _, w in weights[:3]),
    }


def get_sector_mix() -> dict:
    """Sector exposure as % of total. Used by rebalance_diagnostic."""
    positions, source = get_positions()
    total_cv = sum(p.cur_value for p in positions) or 1.0
    by_sector: dict[str, float] = {}
    for p in positions:
        by_sector[p.sector] = by_sector.get(p.sector, 0.0) + p.cur_value
    return {
        "asof": datetime.now().isoformat(timespec="seconds"),
        "source": source,
        "total_value": round(total_cv, 2),
        "by_sector": {k: round(v / total_cv * 100, 2) for k, v in
                       sorted(by_sector.items(), key=lambda x: -x[1])},
    }


def get_large_cap_share() -> float:
    """% of equity in Nifty 50 names. Used by rebalance_diagnostic."""
    positions, _ = get_positions()
    total_cv = sum(p.cur_value for p in positions) or 1.0
    in_large = sum(p.cur_value for p in positions if p.ticker in LARGE_CAP)
    return round(in_large / total_cv * 100, 2)


def write_snapshot_json(out_path: Path | None = None) -> Path:
    """Write the full snapshot to data/portfolio_snapshot.json (for caching)."""
    snap = get_snapshot()
    out_path = out_path or (PROJECT / "data" / "portfolio_snapshot.json")
    out_path.write_text(json.dumps(snap, indent=2, default=str))
    log.info("snapshot written to %s (source=%s)", out_path, snap["source"])
    return out_path


if __name__ == "__main__":
    # Quick CLI for testing
    import sys
    force = "--yfinance" in sys.argv
    snap = get_snapshot(force_yfinance=force)
    print(f"Source: {snap['source']}")
    print(f"Total value: ₹{snap['total_value']:,.0f}")
    print(f"Total P&L:   ₹{snap['total_pnl']:,.0f}")
    print()
    print(f"{'TICKER':<12} {'QTY':>5} {'AVG':>9} {'LTP':>9} {'VALUE':>11} {'PnL%':>7} {'WEIGHT':>7}  SECTOR")
    for p in snap['positions']:
        print(f"{p['ticker']:<12} {p['qty']:>5} {p['avg']:>9.2f} {p['ltp']:>9.2f} "
              f"{p['cur_value']:>11,.0f} {p['pnl_pct']:>6.1f}% {p['weight']:>6.1f}%  {p['sector']}")
