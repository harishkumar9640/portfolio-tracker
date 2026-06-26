"""
webapp.data
-----------
Background data layer for the web dashboard.

Every endpoint reads from this module rather than calling broker /
network code directly. This:
  - keeps request handlers fast (no network calls in the request path)
  - lets us cache results in-memory between requests
  - makes it trivial to wire a "Refresh" button that invalidates the cache
    and re-runs the heavy fetches in a background thread.

Data sources:
  - history_db.HistoryDB  — persistent snapshots, SGB prices, run log
  - portfolio_html.*      — today's portfolio snapshot (regenerated on demand)
  - fair_value.*          — fair-value checker
"""
from __future__ import annotations

import json
import threading
from datetime import date, datetime
from pathlib import Path
from typing import Optional

from history_db import HistoryDB
from logging_setup import get_logger

log = get_logger("webapp")

PROJECT = Path(__file__).resolve().parent.parent

# ----- module-level caches (populated lazily; TTL in seconds) -----
_portfolio_cache: dict = {"asof": None, "data": None, "ts": 0.0}
_fairvalue_cache: dict = {"asof": None, "data": None, "ts": 0.0}
_mf_holdings_cache: dict = {"asof": None, "data": None, "ts": 0.0}
# Portfolio snapshots take ~5s to build (Angel One + mfapi + NSE).
# Cache them aggressively so most requests are instant. The user can
# still click "Refresh" to force a rebuild.
_CACHE_TTL = 300  # 5 minutes


# ---------- Today's portfolio snapshot ----------
def _build_portfolio_snapshot() -> dict:
    """Build a JSON-serialisable snapshot of today's portfolio."""
    from equity_compare import build_snapshot
    from fair_value import valuation as _fv  # noqa: F401  (touch to warm up)

    snap = build_snapshot()

    # The webapp wants pre-rounded floats for templates; build_snapshot
    # already rounds but we apply one more pass for consistency.
    def r(v, n=2):
        return round(float(v), n) if isinstance(v, (int, float)) else v

    snap["equity"]["value"]     = r(snap["equity"]["value"])
    snap["equity"]["prev_value"]= r(snap["equity"]["prev_value"])
    snap["mf"]["value"]         = r(snap["mf"]["value"])
    snap["mf"]["prev_value"]    = r(snap["mf"]["prev_value"])
    snap["mf"]["pct"]           = r(snap["mf"]["pct"])
    snap["sgb"]["value"]        = r(snap["sgb"]["value"])
    snap["sgb"]["prev_value"]   = r(snap["sgb"]["prev_value"])
    snap["sgb"]["pct"]          = r(snap["sgb"]["pct"])
    snap["total"]["value"]      = r(snap["total"]["value"])
    snap["total"]["prev_value"] = r(snap["total"]["prev_value"])
    snap["total"]["pct"]        = r(snap["total"]["pct"])
    if snap.get("equity", {}).get("row"):
        snap["equity"]["row"]["pct"]   = r(snap["equity"]["row"]["pct"])
        snap["equity"]["row"]["value"] = r(snap["equity"]["row"]["value"])
    for row in snap.get("sgb", {}).get("rows", []):
        row["price_per_g"] = r(row.get("price_per_g", 0), 0)
        row["value"]       = r(row.get("value", 0))
        row["pct"]         = r(row.get("pct", 0))
    return snap


def get_portfolio_snapshot(force: bool = False) -> dict:
    """Return today's portfolio snapshot, cached for _CACHE_TTL seconds."""
    import time
    now = time.time()
    if not force and _portfolio_cache["data"] is not None:
        if (now - _portfolio_cache["ts"]) < _CACHE_TTL:
            return _portfolio_cache["data"]
    data = _build_portfolio_snapshot()
    _portfolio_cache["data"] = data
    _portfolio_cache["ts"] = now
    _portfolio_cache["asof"] = data.get("asof")
    return data


# ---------- Fair-value snapshot ----------
def _build_fairvalue_snapshot(tickers: Optional[list[str]] = None) -> dict:
    """Run the fair-value checker against the given tickers."""
    from fair_value import check
    if tickers is None:
        from fair_value.valuation import load_tickers
        tickers = load_tickers()
    rows = check(tickers)
    # Augment with "margin of safety" for the UI. Always set the keys
    # (to None when not computable) so Jinja templates can use
    # ``is not none`` rather than ``is defined``.
    out = []
    for r in rows:
        d = r.to_dict()
        d["graham_margin_pct"] = (
            round((r.graham - r.price) / r.price * 100, 2)
            if (r.price and r.graham) else None
        )
        d["dcf_margin_pct"] = (
            round((r.dcf - r.price) / r.price * 100, 2)
            if (r.price and r.dcf) else None
        )
        out.append(d)
    return {"asof": date.today().isoformat(), "rows": out}


def get_fairvalue_snapshot(force: bool = False) -> dict:
    import time
    now = time.time()
    if not force and _fairvalue_cache["data"] is not None:
        if (now - _fairvalue_cache["ts"]) < _CACHE_TTL:
            return _fairvalue_cache["data"]
    data = _build_fairvalue_snapshot()
    _fairvalue_cache["data"] = data
    _fairvalue_cache["ts"] = now
    return data


# ---------- MF holdings snapshot ----------
def _build_mf_holdings_snapshot() -> dict:
    """
    Fetch the latest monthly MF holdings data for the user's 8
    equity tickers from Trendlyne. Returns the shape consumed by
    /api/mf_holdings and the portfolio page.

    Falls back to cache on network failure (Trendlyne can be flaky).
    """
    try:
        from mf_holdings import get_mf_holdings_summary
        rows = get_mf_holdings_summary()
    except Exception as e:
        log.warning("MF holdings snapshot failed (using cache): %s", e)
        # Try cache directly
        from mf_holdings import _load_cache
        rows = []
        cache = _load_cache()
        for tkr, d in cache.items():
            nc = d.get("net_change_shares")
            sign = "+" if nc and nc >= 0 else ""
            rows.append({
                "ticker": tkr,
                "name": d.get("name", tkr),
                "asof": d.get("asof"),
                "total_mfs_holding": d.get("total_mfs_holding"),
                "mfs_bought": d.get("mfs_bought"),
                "mfs_sold": d.get("mfs_sold"),
                "net_change_shares": nc,
                "net_change_label": (
                    d.get("net_change_label")
                    or (f"{sign}{nc:,}" if nc is not None else "\u2014")
                ),
                "total_shares_held": d.get("total_shares_held"),
                "top_buyer": d.get("top_buyer"),
                "top_seller": d.get("top_seller"),
                "top_buyers": d.get("top_buyers", []),
                "top_sellers": d.get("top_sellers", []),
                "url": d.get("url"),
                "fetched_at": d.get("fetched_at"),
            })
        rows.sort(key=lambda r: (
            1 if r.get("net_change_shares") is None else 0,
            -abs(r.get("net_change_shares") or 0),
        ))
    return {
        "asof": date.today().isoformat(),
        "row_count": len(rows),
        "rows": rows,
    }


def get_mf_holdings_snapshot(force: bool = False) -> dict:
    import time
    now = time.time()
    if not force and _mf_holdings_cache["data"] is not None:
        if (now - _mf_holdings_cache["ts"]) < _CACHE_TTL:
            return _mf_holdings_cache["data"]
    data = _build_mf_holdings_snapshot()
    _mf_holdings_cache["data"] = data
    _mf_holdings_cache["ts"] = now
    return data


# ---------- Health / last-run info ----------
def get_health() -> dict:
    db = HistoryDB()
    last_run = db.last_run("equity_compare.py") or {}
    last_run_fv = db.last_run("fairvalue.py") or {}
    last_run_mf = db.last_run("mf_holdings.py") or {}
    snap_count = len(db.portfolio_history("total"))
    sgb_count = len(db.sgb_history("IN0020230184"))  # just a sample
    return {
        "now": datetime.now().isoformat(timespec="seconds"),
        "last_portfolio_run": last_run,
        "last_fairvalue_run": last_run_fv,
        "last_mf_holdings_run": last_run_mf,
        "snapshots_in_db": snap_count,
        "sgb_price_rows": sgb_count,
    }


# ---------- Background refresh ----------
def start_background_refresh(kind: str = "portfolio") -> None:
    """Force a fresh snapshot build in a worker thread."""
    def _worker():
        try:
            if kind == "portfolio":
                get_portfolio_snapshot(force=True)
            elif kind == "fairvalue":
                get_fairvalue_snapshot(force=True)
            elif kind == "mf_holdings":
                get_mf_holdings_snapshot(force=True)
        except Exception as e:
            log.error("background refresh failed: %s", e)

    t = threading.Thread(target=_worker, daemon=True, name=f"refresh-{kind}")
    t.start()


# ---------- Holdings JSON for the settings page ----------
def get_holdings_summary() -> dict:
    """Read mfs.json / sgbs.json / my_tickers.txt for the settings page."""
    from mf_sgb import load_mfs, load_sgbs
    from fair_value.valuation import load_tickers
    return {
        "mfs": load_mfs(),
        "sgbs": load_sgbs(),
        "tickers": load_tickers(),
    }