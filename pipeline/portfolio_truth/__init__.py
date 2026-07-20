"""
pipeline.portfolio_truth
-------------------------
Single source of truth for ALL portfolio positions (equity, mutual funds, SGBs).

This is the **only file that the AI / pipeline should read first** to know what
the user holds. Every other module (portfolio monitor, fair-value tracker,
scheduler) derives from this.

File: data/portfolio_truth.json
Updated by: pipeline/portfolio_truth/update.py (manual CLI)
Auto-refreshed: 11:00 PM IST daily via scheduler.py (when broker + MF feeds work)

Schema (v1):

  {
    "schema_version": 1,
    "asof": "<ISO timestamp of last update>",
    "source": "broker" | "user-edited" | "mfs.json" | "sgbs.json" | "merged",
    "equity": {
      "<TICKER>": {
        "ticker": "RELIANCE",
        "qty": 60,
        "avg_price": 1250.52,
        "exchange": "NSE",
        "source": "angel"  # or "user"
      },
      ...
    },
    "mutual_funds": {
      "<scheme_name>": {
        "scheme_name": "HDFC Mid Cap Fund - Growth Option - Direct Plan",
        "units": 500.58,
        "source": "mfs.json"
      },
      ...
    },
    "sgbs": {
      "<ISIN>": {
        "isin": "IN0020230184",
        "units": 1,
        "invested_per_g": 7920,
        "buy_date": "2023-02-15",
        "source": "sgbs.json"
      },
      ...
    },
    "watchlist": [
      # Tickers we want to track via fair-value pipeline but don't currently hold.
      # Derived from my_tickers.txt minus the equity tickers above.
      "TCS", "INFY", "HDFCBANK", "ICICIBANK"
    ]
  }

Design rules:
  - This file is the single source of truth. NEVER hardcode holdings anywhere else.
  - On every project start, run `python -m pipeline.portfolio_truth status` to verify
    the file is current.
  - The updater reconciles against live broker / config files, NOT against the truth
    file itself. This way, the truth file mirrors the "ground truth" (broker, JSON
    config), and the AI can detect drift.
  - All quantities are in absolute units (shares for equity, units for MFs, grams for SGBs).
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

log = logging.getLogger("portfolio_truth")

PROJECT = Path(__file__).resolve().parents[2]
DATA_DIR = PROJECT / "data"
TRUTH_FILE = DATA_DIR / "portfolio_truth.json"
TRUTH_BACKUP_DIR = DATA_DIR / "portfolio_truth_backups"
TRUTH_BACKUP_DIR.mkdir(parents=True, exist_ok=True)

MFS_FILE = PROJECT / "mfs.json"
SGBS_FILE = PROJECT / "sgbs.json"
WATCHLIST_FILE = PROJECT / "my_tickers.txt"

SCHEMA_VERSION = 1


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------- Read / write the truth file ----------

def load_truth() -> dict:
    """Read the truth file. Returns a fresh default dict if file doesn't exist.

    Reads go through the shared webapp LRU cache (`webapp.cache.cached_json`)
    so that 10 calls per page request don't translate to 10 disk reads. The
    cache is mtime-based, so the file is re-read on disk only when something
    actually changes (manual edit, scheduler auto-update, etc.).

    Note: the cache is process-local. If you run multiple uvicorn workers,
    each worker has its own copy — that's fine, the file is small.
    """
    # Lazy import to avoid a circular dep (webapp.cache imports logging
    # but pipeline/portfolio_truth is loaded before webapp at app start).
    try:
        from webapp.cache import cached_json
        cached = cached_json(TRUTH_FILE)
        if cached is not None:
            return cached
    except ImportError:
        # webapp.cache not on path (e.g. running pipeline directly without
        # the webapp server). Fall through to direct read.
        pass
    if not TRUTH_FILE.exists():
        log.warning("truth file missing at %s; returning empty default", TRUTH_FILE)
        return {
            "schema_version": SCHEMA_VERSION,
            "asof": None,
            "source": "default",
            "equity": {},
            "mutual_funds": {},
            "sgbs": {},
            "watchlist": [],
        }
    try:
        return json.loads(TRUTH_FILE.read_text())
    except (json.JSONDecodeError, OSError) as e:
        log.error("could not parse truth file: %s", e)
        raise


def save_truth(truth: dict, *, source: str = "user-edited",
               keep_backup: bool = True) -> None:
    """Save the truth file. Optionally backup the previous version first."""
    truth["schema_version"] = SCHEMA_VERSION
    truth["asof"] = _now_iso()
    truth["source"] = source
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if keep_backup and TRUTH_FILE.exists():
        backup = TRUTH_BACKUP_DIR / f"portfolio_truth_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        try:
            backup.write_text(TRUTH_FILE.read_text())
        except OSError as e:
            log.warning("could not write backup: %s", e)
    TRUTH_FILE.write_text(json.dumps(truth, indent=2, default=str))
    # Invalidate the cache so the next read in this process sees the new
    # mtime/content. (The mtime check would catch this anyway, but
    # invalidating avoids one wasted stat() call.)
    try:
        from webapp.cache import invalidate_json
        invalidate_json(TRUTH_FILE)
    except ImportError:
        pass
    log.info("truth saved to %s (source=%s, %d equity, %d MF, %d SGB)",
             TRUTH_FILE, source,
             len(truth.get("equity", {})),
             len(truth.get("mutual_funds", {})),
             len(truth.get("sgbs", {})))


# ---------- Builders from each data source ----------

def _from_broker() -> dict:
    """Pull equity positions from Angel One SmartAPI."""
    try:
        from pipeline.angel_client import fetch_holdings
        holdings = fetch_holdings()
    except Exception as e:
        log.error("broker fetch failed: %s", e)
        return {}
    out = {}
    for h in holdings:
        if h.quantity <= 0:
            continue
        tk = h.symbol.replace("-EQ", "").upper()
        out[tk] = {
            "ticker": tk,
            "qty": int(h.quantity),
            "avg_price": float(h.avg_price),
            "exchange": h.exchange or "NSE",
            "source": "angel",
        }
    return out


def _from_mfs_json() -> dict:
    """Pull MF positions from mfs.json (the existing config file)."""
    if not MFS_FILE.exists():
        return {}
    try:
        raw = json.loads(MFS_FILE.read_text())
    except (json.JSONDecodeError, OSError) as e:
        log.error("could not read mfs.json: %s", e)
        return {}
    out = {}
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name", "").strip()
        units = entry.get("units")
        if not name or not units:
            continue
        out[name] = {
            "scheme_name": name,
            "units": float(units),
            "source": "mfs.json",
        }
    return out


def _from_sgbs_json() -> dict:
    """Pull SGB positions from sgbs.json (the existing config file)."""
    if not SGBS_FILE.exists():
        return {}
    try:
        raw = json.loads(SGBS_FILE.read_text())
    except (json.JSONDecodeError, OSError) as e:
        log.error("could not read sgbs.json: %s", e)
        return {}
    out = {}
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        isin = entry.get("isin", "").strip()
        units = entry.get("units")
        if not isin or not units:
            continue
        out[isin] = {
            "isin": isin,
            "units": float(units),
            "invested_per_g": entry.get("invested_per_g"),
            "buy_date": entry.get("buy_date"),
            "source": "sgbs.json",
        }
    return out


def _from_watchlist() -> list[str]:
    """Pull the watchlist from my_tickers.txt."""
    if not WATCHLIST_FILE.exists():
        return []
    out = []
    for line in WATCHLIST_FILE.read_text().splitlines():
        s = line.strip()
        if s and not s.startswith("#"):
            out.append(s.upper())
    # de-dupe, preserve order
    seen = set()
    deduped = []
    for t in out:
        if t not in seen:
            seen.add(t)
            deduped.append(t)
    return deduped


# ---------- Reconciliation ----------

def fetch_live_state() -> dict:
    """Pull live state from ALL sources. Used by update + diff."""
    return {
        "equity": _from_broker(),
        "mutual_funds": _from_mfs_json(),
        "sgbs": _from_sgbs_json(),
        "watchlist": _from_watchlist(),
        "fetched_at": _now_iso(),
    }


def _norm_equity(d: dict) -> dict:
    """Normalize a single equity entry for comparison."""
    return {
        "ticker": d.get("ticker", "").upper(),
        "qty": int(d.get("qty", 0)),
        "avg_price": round(float(d.get("avg_price", 0)), 2),
    }


def _norm_mf(d: dict) -> dict:
    return {
        "scheme_name": d.get("scheme_name", "").strip(),
        "units": round(float(d.get("units", 0)), 4),
    }


def _norm_sgb(d: dict) -> dict:
    return {
        "isin": d.get("isin", "").strip(),
        "units": round(float(d.get("units", 0)), 4),
    }


def diff_states(current: dict, live: dict) -> dict:
    """
    Compare two truth-file-shaped dicts. Returns a structured diff.
    Empty lists/empty dicts mean "no change".
    """
    cur_eq = current.get("equity", {})
    liv_eq = live.get("equity", {})
    cur_mf = current.get("mutual_funds", {})
    liv_mf = live.get("mutual_funds", {})
    cur_sgb = current.get("sgbs", {})
    liv_sgb = live.get("sgbs", {})
    cur_wl = set(current.get("watchlist", []))
    liv_wl = set(live.get("watchlist", []))

    equity_changes = {
        "added": [t for t in liv_eq if t not in cur_eq],
        "removed": [t for t in cur_eq if t not in liv_eq],
        "qty_changed": sorted(
            t for t in (set(liv_eq) & set(cur_eq))
            if liv_eq[t]["qty"] != cur_eq[t]["qty"]
        ),
        "avg_changed": sorted(
            t for t in (set(liv_eq) & set(cur_eq))
            if round(liv_eq[t]["avg_price"], 2) != round(cur_eq[t]["avg_price"], 2)
        ),
    }
    mf_changes = {
        "added": [n for n in liv_mf if n not in cur_mf],
        "removed": [n for n in cur_mf if n not in liv_mf],
        "units_changed": sorted(
            n for n in (set(liv_mf) & set(cur_mf))
            if round(liv_mf[n]["units"], 4) != round(cur_mf[n]["units"], 4)
        ),
    }
    sgb_changes = {
        "added": [i for i in liv_sgb if i not in cur_sgb],
        "removed": [i for i in cur_sgb if i not in liv_sgb],
        "units_changed": sorted(
            i for i in (set(liv_sgb) & set(cur_sgb))
            if round(liv_sgb[i]["units"], 4) != round(cur_sgb[i]["units"], 4)
        ),
    }
    watchlist_changes = {
        "added": sorted(liv_wl - cur_wl),
        "removed": sorted(cur_wl - liv_wl),
    }

    def _is_clean(d: dict) -> bool:
        return not any(d.values())

    return {
        "asof_current": current.get("asof"),
        "asof_live": live.get("fetched_at"),
        "equity": equity_changes,
        "mutual_funds": mf_changes,
        "sgbs": sgb_changes,
        "watchlist": watchlist_changes,
        "is_clean": (
            _is_clean(equity_changes)
            and _is_clean(mf_changes)
            and _is_clean(sgb_changes)
            and _is_clean(watchlist_changes)
        ),
    }


def merge_from_live(live: dict, existing: Optional[dict] = None) -> dict:
    """
    Build a new truth dict by merging live state with existing truth.
    Existing truth's avg_price / invested_per_g / buy_date are preserved
    if the position is still held (broker doesn't give us cost basis reliably
    across sessions; we trust the file for that).
    """
    if existing is None:
        existing = load_truth()
    out = {
        "schema_version": SCHEMA_VERSION,
        "asof": _now_iso(),
        "source": "merged",
        "equity": {},
        "mutual_funds": {},
        "sgbs": {},
        "watchlist": list(live.get("watchlist", [])),
    }
    # Equity: preserve avg_price from existing
    for tk, live_pos in live.get("equity", {}).items():
        merged = dict(live_pos)
        if tk in existing.get("equity", {}):
            old = existing["equity"][tk]
            if old.get("avg_price", 0) > 0:
                merged["avg_price"] = old["avg_price"]
        out["equity"][tk] = merged
    # MF: preserve from existing
    for name, live_mf in live.get("mutual_funds", {}).items():
        out["mutual_funds"][name] = dict(live_mf)
    # SGB: preserve buy_date / invested_per_g
    for isin, live_sgb in live.get("sgbs", {}).items():
        merged = dict(live_sgb)
        if isin in existing.get("sgbs", {}):
            old = existing["sgbs"][isin]
            for k in ("invested_per_g", "buy_date"):
                if old.get(k) is not None:
                    merged[k] = old[k]
        out["sgbs"][isin] = merged
    return out


ETF_SUFFIXES = ("ETF", "BEES")


def _is_etf(ticker: str) -> bool:
    """True if the ticker is an ETF/BeES (excluded from MF-holdings and
    shareholding-pattern lookups — these instruments don't have meaningful
    MF-holding or shareholding-pattern data on Trendlyne)."""
    t = ticker.upper()
    return t.endswith(ETF_SUFFIXES) or "-ETF" in t


def equity_tickers(*, include_etfs: bool = True) -> list[str]:
    """Return the list of equity tickers currently in the truth file.

    The truth file is the registry of "what to track". Every section of the
    Portfolio tab that is per-equity-ticker (Equity Holdings, MF Holdings
    Trend, Shareholding Pattern) should derive its universe from this
    function rather than hardcoding tickers.

    Args:
        include_etfs: if False, drop ETF/BeES tickers (GOLDBEES, METALIETF,
            NEXT50IETF, ...). MF Holdings Trend uses include_etfs=False
            because Trendlyne has no MF-holdings data for ETFs. Shareholding
            Pattern uses include_etfs=True because the user wants to see all
            11 positions there.

    Returns:
        Sorted list of ticker symbols (e.g. ["BALRAMCHIN", "ITC", ...]).
        Stable order: alphabetical, so diffs and snapshots are reproducible.
    """
    truth = load_truth()
    eq = truth.get("equity", {}) or {}
    tickers = list(eq.keys())
    if not include_etfs:
        tickers = [t for t in tickers if not _is_etf(t)]
    return sorted(t.upper() for t in tickers)


def truth_mtime() -> float:
    """Last-modified time of the truth file (epoch seconds).

    Used by webapp/data.py to invalidate caches whenever the truth file
    changes (manual edit, scheduler auto-update, broker sync, etc.).
    Returns 0.0 if the file does not exist.
    """
    if not TRUTH_FILE.exists():
        return 0.0
    try:
        return TRUTH_FILE.stat().st_mtime
    except OSError:
        return 0.0


# ---------- Convenience: derive a quick status report ----------

def status() -> dict:
    """Return a summary view of the current truth file + drift check."""
    current = load_truth()
    live = fetch_live_state()
    diff = diff_states(current, live)
    return {
        "truth_file": str(TRUTH_FILE),
        "truth_asof": current.get("asof"),
        "truth_source": current.get("source"),
        "n_equity": len(current.get("equity", {})),
        "n_mf": len(current.get("mutual_funds", {})),
        "n_sgb": len(current.get("sgbs", {})),
        "n_watchlist": len(current.get("watchlist", [])),
        "diff": diff,
    }


def print_status() -> None:
    import json as _json
    s = status()
    print(f"Truth file: {s['truth_file']}")
    print(f"  asof: {s['truth_asof']}  source: {s['truth_source']}")
    print(f"  equity: {s['n_equity']}  MF: {s['n_mf']}  SGB: {s['n_sgb']}  watchlist: {s['n_watchlist']}")
    print()
    d = s["diff"]
    if d["is_clean"]:
        print("✓ No drift. Truth file matches live state.")
        return
    print("✗ DRIFT detected:")
    eq = d["equity"]
    if any(eq.values()):
        print(f"  EQUITY:  added={eq['added']} removed={eq['removed']} "
              f"qty_changed={eq['qty_changed']} avg_changed={eq['avg_changed']}")
    mf = d["mutual_funds"]
    if any(mf.values()):
        print(f"  MF:      added={mf['added']} removed={mf['removed']} units_changed={mf['units_changed']}")
    sg = d["sgbs"]
    if any(sg.values()):
        print(f"  SGB:     added={sg['added']} removed={sg['removed']} units_changed={sg['units_changed']}")
    wl = d["watchlist"]
    if any(wl.values()):
        print(f"  WATCH:   added={wl['added']} removed={wl['removed']}")


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "status":
        print_status()
    elif len(sys.argv) > 1 and sys.argv[1] == "path":
        print(TRUTH_FILE)
    elif len(sys.argv) > 1 and sys.argv[1] == "diff":
        print(json.dumps(status(), indent=2, default=str))
    else:
        print(f"Usage: python -m pipeline.portfolio_truth [status|path|diff]")
