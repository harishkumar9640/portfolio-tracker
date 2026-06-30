"""
fair_value.search
------------------
Stock autocomplete + name/ISIN resolution for the web dashboard.

Data source: NSE publishes the full equity list as a CSV at
    https://archives.nseindia.com/content/equities/EQUITY_L.csv
We cache it at ``data/nse_equity_list.csv`` (TTL 1 day).

The endpoint exposes:
  - search_schemes(query, limit)  -> list of {"symbol", "name", "isin"}
  - resolve_ticker(user_input)     -> (canonical_symbol, full_name)

Matching strategy (relevance-ranked, lower score = better):
  0: exact symbol match
  1: exact name match (after lowercasing and stripping Ltd/Industries)
  2: symbol prefix match
  3: name prefix match
  4: whole-word match in name (token boundary)
  5: normalised name substring
  6: raw lowercase substring
  99: ISIN exact match (short-circuits everything)

Ties are broken alphabetically by symbol.

Used by:
  - GET /api/fairvalue/search?q=REL&limit=10
  - POST /api/fairvalue/lookup {"ticker": "Reliance Industries"}
"""
from __future__ import annotations

import csv
import threading
import time
from pathlib import Path
from typing import Optional

import requests

from ..logging_setup import get_logger

log = get_logger("fair_value.search")

PROJECT = Path(__file__).resolve().parent.parent.parent
CACHE_FILE = PROJECT / "data" / "data/cache/nse_equity_list.csv"
CACHE_TTL_SECONDS = 24 * 3600
NSE_URL = "https://archives.nseindia.com/content/equities/EQUITY_L.csv"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36"
    ),
}

_lock = threading.Lock()
_index: list[dict] = []
_index_loaded_at: float = 0.0
_cached_path: str = ""
_cached_mtime: float = 0.0


def _download_nse_list() -> str:
    """Fetch the NSE equity list and cache it as CSV. Returns the text."""
    log.info("downloading NSE equity list from %s", NSE_URL)
    r = requests.get(NSE_URL, headers=HEADERS, timeout=30)
    r.raise_for_status()
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    CACHE_FILE.write_text(r.text, encoding="utf-8")
    log.info("cached NSE equity list: %d bytes", len(r.text))
    return r.text


def _parse_csv(text: str):
    """Yield {symbol, name, isin, _sym, _name} dicts from the NSE CSV."""
    reader = csv.DictReader(text.splitlines())
    # NSE's CSV has a leading space in every column except SYMBOL
    # (" NAME OF COMPANY", " ISIN NUMBER"). Map stripped->original so
    # we can look up by clean key.
    field_map = {k.strip(): k for k in (reader.fieldnames or [])}
    sym_key  = field_map.get("SYMBOL",          "SYMBOL")
    name_key = field_map.get("NAME OF COMPANY", "NAME OF COMPANY")
    isin_key = field_map.get("ISIN NUMBER",     "ISIN NUMBER")
    for row in reader:
        sym  = (row.get(sym_key)  or "").strip().upper()
        name = (row.get(name_key) or "").strip()
        isin = (row.get(isin_key) or "").strip().upper()
        if not sym:
            continue
        yield {
            "symbol": sym,
            "name": name,
            "isin": isin,
            "_sym": sym.lower(),
            "_name": name.lower(),
        }


def _load_index(force: bool = False) -> list[dict]:
    """Return the in-memory list of {symbol, name, isin} entries.

    Invalidation: the cache is reused only if the underlying file path
    and mtime match what we loaded previously. This lets tests swap the
    cache file without polluting other consumers.
    """
    global _index, _index_loaded_at, _cached_path, _cached_mtime
    with _lock:
        if (not force
                and _index
                and (time.time() - _index_loaded_at) < CACHE_TTL_SECONDS
                and _cached_path == str(CACHE_FILE)
                and CACHE_FILE.exists()
                and _cached_mtime == CACHE_FILE.stat().st_mtime):
            return _index

        rows: list[dict] = []
        if not force and CACHE_FILE.exists():
            age = time.time() - CACHE_FILE.stat().st_mtime
            if age < CACHE_TTL_SECONDS:
                rows = list(_parse_csv(CACHE_FILE.read_text(encoding="utf-8")))
                log.debug("loaded %d NSE entries from cache (age %ds)",
                          len(rows), int(age))
        if not rows:
            try:
                csv_text = _download_nse_list()
                rows = list(_parse_csv(csv_text))
            except Exception as e:
                log.warning("NSE equity list download failed: %s", e)
                if CACHE_FILE.exists():
                    log.warning("using stale cache as fallback")
                    rows = list(_parse_csv(CACHE_FILE.read_text(encoding="utf-8")))
                else:
                    return []

        _index = rows
        _index_loaded_at = time.time()
        _cached_path = str(CACHE_FILE)
        _cached_mtime = CACHE_FILE.stat().st_mtime if CACHE_FILE.exists() else 0.0
        return _index


def _normalise(s: str) -> str:
    """Strip common suffixes so 'Reliance Industries' matches 'RELIANCE'.

    Removes (case-insensitive, with or without a leading space or
    trailing punctuation):
      - limited, ltd, ltd.
      - industries, industry
      - india, indian
      - and, &
    Then collapses whitespace and strips.
    """
    s = s.lower().strip()
    import re as _re
    # Strip each suffix once, anchored at the end. e.g.
    #   "Reliance Industries Ltd." -> "Reliance Industries"
    #   "Reliance Industries Ltd"  -> "Reliance Industries"
    for suffix in ("limited", "ltd.", "ltd", "industries", "industry",
                   "india", "indian"):
        s = _re.sub(r"(?:\s+|^)" + suffix + r"\.*$", "", s)
    # Strip "& " and "and " connectors
    s = _re.sub(r"\s+(?:&|and)\s+", " ", s)
    # Strip trailing punctuation/whitespace, collapse internal whitespace
    return " ".join(s.split())


def _score(e: dict, q_lower: str, q_norm: str) -> tuple:
    """Lower is better. Returns (primary, lcp_or_0, name_len).

    primary:   coarse relevance bucket (see below).
    lcp_or_0:  length of the longest common prefix between q_lower and
               the entry name. Used to break ties so "Reliance Industries"
               prefers RELIANCE over RCOM.
    name_len:  for the rare case where primary + lcp tie, prefer shorter
               (more specific) names. We sort on negative so shorter wins.
    """
    name = e["_name"]
    sym = e["_sym"]
    if sym == q_lower:
        return (0, 0, len(name))
    if name == q_lower or (q_norm and name == q_norm):
        return (1, len(name), len(name))
    if sym.startswith(q_lower):
        return (2, len(q_lower), len(name))
    if name.startswith(q_lower):
        return (3, len(q_lower), len(name))
    if q_norm and name.startswith(q_norm):
        return (4, len(q_norm), len(name))
    # Compute longest common prefix with the original (non-normalised)
    # query — captures how much of the user-typed name matched the entry.
    lcp = 0
    for a, b in zip(name, q_lower):
        if a == b:
            lcp += 1
        else:
            break
    if q_norm and any(w == q_norm for w in name.split()):
        return (5, len(name.split()[0]) if name.split() else 0, len(name))
    if q_norm and q_norm in name:
        return (6, len(q_norm), len(name))
    if q_lower in name:
        return (7, lcp, len(name))
    return (100, 0, 0)


def search_schemes(query: str, limit: int = 10) -> list[dict]:
    """
    Search the NSE equity list.

    Args:
        query: ticker prefix, name substring, or ISIN.
        limit: maximum number of results.

    Returns:
        List of {symbol, name, isin}, sorted by relevance (best first).
        Ties broken alphabetically by symbol.
    """
    q = (query or "").strip()
    if not q:
        idx = _load_index()
        # Alphabetical by symbol so the dropdown is stable.
        sorted_idx = sorted(idx, key=lambda e: e["symbol"])
        return [
            {"symbol": e["symbol"], "name": e["name"], "isin": e["isin"]}
            for e in sorted_idx[:limit]
        ]

    idx = _load_index()
    q_lower = q.lower()
    q_upper = q.upper()
    q_norm = _normalise(q)

    # ISIN exact match short-circuits
    for e in idx:
        if e["isin"] == q_upper:
            return [{"symbol": e["symbol"],
                     "name": e["name"],
                     "isin": e["isin"]}]

    scored: list[tuple[int, str, dict]] = []
    for e in idx:
        s = _score(e, q_lower, q_norm)
        if s[0] < 100:
            scored.append((s, e["symbol"], e))

    scored.sort(key=lambda t: t[0])  # (primary, prefix_len, -name_len)
    return [
        {"symbol": e["symbol"], "name": e["name"], "isin": e["isin"]}
        for _, _, e in scored[:limit]
    ]


def resolve_ticker(user_input: str) -> tuple[str, str]:
    """
    Resolve arbitrary user input (ticker, name, ISIN) to a canonical
    NSE ticker symbol + full company name.

    Returns (symbol, name). If nothing matches, returns the upper-cased
    input as-is so the caller can pass it to screener.in directly.
    """
    q = (user_input or "").strip()
    if not q:
        return ("", "")
    results = search_schemes(q, limit=1)
    if results:
        r = results[0]
        return (r["symbol"], r["name"])
    return (q.upper(), q)


# ---------- CLI for ad-hoc testing ----------
if __name__ == "__main__":
    import sys
    q = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else ""
    for r in search_schemes(q, limit=15):
        print(f"{r['symbol']:<14} {r['isin']:<14} {r['name']}")
