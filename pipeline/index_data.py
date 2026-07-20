"""
pipeline.index_data
-------------------
Multi-index historical data loader.

Supports:
- Nifty50 (`^NSEI` column in `data/cache/indices_cache.csv`)
- Nifty Midcap 150 (new — manual CSV import)
- Nifty Smallcap 250 (new — manual CSV import)

CSV format expected for Midcap150/Smallcap250 (one column per date row):
    Date,Open,High,Low,Close,...
    2024-01-01,12345.6,12400.0,12300.0,12380.5,...

Or a simpler two-column format:
    Date,Close
    2024-01-01,12380.5
    ...

The file should be placed at:
    data/cache/indices/nifty_midcap_150.csv
    data/cache/indices/nifty_smallcap_250.csv

If the file is missing, the loader returns an empty history. The CAGR
module then falls back to Nifty50 (with a warning in the output).
"""
from __future__ import annotations

import csv
from datetime import date, datetime
from pathlib import Path
from typing import Optional

PROJECT = Path(__file__).resolve().parents[1]
INDICES_DIR = PROJECT / "data" / "cache" / "indices"
NIFTY50_CSV = PROJECT / "data" / "cache" / "indices_cache.csv"


def _parse_date(s: str) -> Optional[date]:
    s = (s or "").strip()
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _parse_float(v: str) -> Optional[float]:
    try:
        return float((v or "").replace(",", "").strip())
    except (ValueError, TypeError):
        return None


def _read_two_col_csv(path: Path) -> list[tuple[date, float]]:
    """Read a {Date, Close} style CSV. Returns sorted [(date, close), ...]."""
    if not path.exists():
        return []
    out: list[tuple[date, float]] = []
    try:
        with path.open() as fh:
            reader = csv.DictReader(fh)
            for row in reader:
                # Accept common header names
                d = _parse_date(row.get("Date") or row.get("date") or "")
                # Try Close first, then Adj Close, then last numeric column
                c = (
                    _parse_float(row.get("Close") or "")
                    or _parse_float(row.get("Adj Close") or "")
                    or _parse_float(row.get("close") or "")
                )
                if d is not None and c is not None and c > 0:
                    out.append((d, c))
    except Exception:
        return []
    out.sort(key=lambda x: x[0])
    return out


def _read_nifty50_csv() -> list[tuple[date, float]]:
    """Read Nifty50 from the existing indices_cache.csv (^NSEI column)."""
    if not NIFTY50_CSV.exists():
        return []
    out: list[tuple[date, float]] = []
    try:
        with NIFTY50_CSV.open() as fh:
            reader = csv.DictReader(fh)
            for row in reader:
                d = _parse_date(row.get("Date") or "")
                c = _parse_float(row.get("^NSEI") or "")
                if d is not None and c is not None and c > 0:
                    out.append((d, c))
    except Exception:
        return []
    out.sort(key=lambda x: x[0])
    return out


# --- Module-level cached loads (lazy) ---
_CACHE: dict[str, list[tuple[date, float]]] = {}


def get_index_history(name: str) -> list[tuple[date, float]]:
    """Return the sorted [(date, close), ...] history for the named index.

    Supported names: 'nifty50', 'nifty_midcap_150', 'nifty_smallcap_250'.

    Returns an empty list if the data file is missing.
    """
    if name in _CACHE:
        return _CACHE[name]
    if name == "nifty50":
        out = _read_nifty50_csv()
    elif name == "nifty_midcap_150":
        out = _read_two_col_csv(INDICES_DIR / "nifty_midcap_150.csv")
    elif name == "nifty_smallcap_250":
        out = _read_two_col_csv(INDICES_DIR / "nifty_smallcap_250.csv")
    else:
        out = []
    _CACHE[name] = out
    return out


def close_on(name: str, d: date) -> Optional[float]:
    """Return the index close on date d (or nearest prior trading day)."""
    hist = get_index_history(name)
    if not hist:
        return None
    # Binary search for the latest date <= d
    lo, hi = 0, len(hist) - 1
    ans: Optional[float] = None
    while lo <= hi:
        mid = (lo + hi) // 2
        if hist[mid][0] <= d:
            ans = hist[mid][1]
            lo = mid + 1
        else:
            hi = mid - 1
    return ans


def is_available(name: str) -> bool:
    """True if the index has at least some historical data loaded."""
    return bool(get_index_history(name))


def available_indices() -> dict[str, dict]:
    """Return metadata about all indices: {name: {available, points, first_date, last_date, file}}."""
    out: dict[str, dict] = {}
    for name, path in [
        ("nifty50", NIFTY50_CSV),
        ("nifty_midcap_150", INDICES_DIR / "nifty_midcap_150.csv"),
        ("nifty_smallcap_250", INDICES_DIR / "nifty_smallcap_250.csv"),
    ]:
        hist = get_index_history(name)
        out[name] = {
            "available": bool(hist),
            "points": len(hist),
            "first_date": hist[0][0].isoformat() if hist else None,
            "last_date": hist[-1][0].isoformat() if hist else None,
            "file": str(path.relative_to(PROJECT)) if path.exists() else None,
        }
    return out
