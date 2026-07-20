"""
pipeline.marketcap
-----------------
Stock market cap lookup + classification (large / mid / small) per
the NSE's standard tiering (rebalanced semi-annually).

Tier thresholds (NSE definitions, last updated 2025):
  - Large cap : market cap > ₹70,000 Cr
  - Mid cap   : ₹20,000 Cr - ₹70,000 Cr
  - Small cap : < ₹20,000 Cr

Source: yfinance `.info['marketCap']` (updated daily). Cached to
`data/cache/marketcap.json` for 24 h to avoid re-fetching on every call.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

PROJECT = Path(__file__).resolve().parents[1]
CACHE_FILE = PROJECT / "data" / "cache" / "marketcap.json"
CACHE_TTL = timedelta(hours=24)

LARGE_CAP_THRESHOLD_CR = 70_000  # in Cr
MID_CAP_THRESHOLD_CR = 20_000


def _load_cache() -> dict:
    if not CACHE_FILE.exists():
        return {}
    try:
        with CACHE_FILE.open() as fh:
            data = json.load(fh)
    except Exception:
        return {}
    return data


def _save_cache(data: dict) -> None:
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with CACHE_FILE.open("w") as fh:
        json.dump(data, fh, indent=2)


def _is_fresh(entry: dict) -> bool:
    """True if the cache entry was fetched within the TTL window."""
    try:
        ts = datetime.fromisoformat(entry.get("fetched_at", ""))
        return datetime.now() - ts < CACHE_TTL
    except Exception:
        return False


def get_market_cap_cr(ticker: str) -> Optional[float]:
    """Return the current market cap of the given NSE ticker, in ₹ Cr.

    Returns None if the lookup fails. Uses yfinance, with file cache.
    """
    cache = _load_cache()
    key = ticker.upper()
    if key in cache and _is_fresh(cache[key]):
        return cache[key].get("market_cap_cr")

    # Fetch from yfinance
    try:
        import yfinance as yf
        info = yf.Ticker(f"{ticker}.NS").info
        mc_inr = info.get("marketCap")
        if not mc_inr or mc_inr <= 0:
            return None
        mc_cr = mc_inr / 1e7  # INR to Cr (1e7 = 1 Cr)
    except Exception:
        return None

    # Update cache
    cache[key] = {
        "market_cap_cr": mc_cr,
        "fetched_at": datetime.now().isoformat(),
        "ticker": ticker,
    }
    _save_cache(cache)
    return mc_cr


def classify(ticker: str) -> str:
    """Return 'large', 'mid', 'small', or 'unknown' for the given ticker."""
    mc = get_market_cap_cr(ticker)
    if mc is None or mc <= 0:
        return "unknown"
    if mc > LARGE_CAP_THRESHOLD_CR:
        return "large"
    if mc > MID_CAP_THRESHOLD_CR:
        return "mid"
    return "small"


def classify_many(tickers: list[str]) -> dict[str, str]:
    """Bulk classify. Returns {ticker: 'large'/'mid'/'small'/'unknown'}."""
    out: dict[str, str] = {}
    for tk in tickers:
        out[tk.upper()] = classify(tk)
    return out
