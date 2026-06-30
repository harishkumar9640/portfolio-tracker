"""
mf_sgb.py
---------
Mutual fund and Sovereign Gold Bond integration.

Reads:
    mfs.json     - list of mutual fund holdings (name + units)
    sgbs.json    - list of SGB holdings (isin + units)

Outputs:
    A unified list of "asset rows" ready for the chart:
        [{"name": ..., "kind": "equity"|"mf"|"sgb", "value": ..., "prev_value": ...,
          "pct": ..., "units": ..., "extra": {...}}, ...]

Sources:
    MFs  -> https://api.mfapi.in/mf/<code>/latest   (free, no auth)
    SGBs -> https://api.mfapi.in/mf  (master scheme list, used for SGB NAV too)
            Note: mfapi.in is for mutual funds. For SGB prices we use the
            issuer isin + NSE public quote endpoint (best-effort).
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import pandas as pd
import requests

from .logging_setup import get_logger
from .parallel import map_parallel

log = get_logger("mf_sgb")

PROJECT = Path(__file__).resolve().parent.parent
MF_FILE = PROJECT / "mfs.json"
SGB_FILE = PROJECT / "sgbs.json"
SGB_PRICE_CACHE_LEGACY = PROJECT / "data" / "sgb_price_history.json"   # JSON, migrated

from .history_db import HistoryDB


# ---------- Data classes ----------
@dataclass
class AssetRow:
    name: str
    kind: str             # "mf" or "sgb"
    units: float
    value: float          # current value (₹)
    prev_value: float     # previous-day value (₹)
    pct: float            # day-change %
    extra: dict           # nav / price / etc. for display


# ---------- IO ----------
def load_mfs() -> list[dict]:
    if not MF_FILE.exists():
        return []
    return json.loads(MF_FILE.read_text())


def load_sgbs() -> list[dict]:
    if not SGB_FILE.exists():
        return []
    return json.loads(SGB_FILE.read_text())


# ---------- MF scheme code resolution ----------
_MASTER_CACHE: list[dict] | None = None


def _master_list() -> list[dict]:
    """mfapi.in master list of all ~12k schemes. Cached in-process."""
    global _MASTER_CACHE
    if _MASTER_CACHE is not None:
        return _MASTER_CACHE
    cache_file = PROJECT / "data" / "data/cache/mf_master_cache.json"
    if cache_file.exists():
        _MASTER_CACHE = json.loads(cache_file.read_text())
        if _MASTER_CACHE:
            return _MASTER_CACHE
    print("[mf] fetching master scheme list from mfapi.in (one-time, ~2 MB)…")
    r = requests.get("https://api.mfapi.in/mf", timeout=30)
    r.raise_for_status()
    _MASTER_CACHE = r.json()
    cache_file.parent.mkdir(exist_ok=True)
    cache_file.write_text(json.dumps(_MASTER_CACHE))
    log.info("master list: %d schemes", len(_MASTER_CACHE))
    return _MASTER_CACHE


def _norm(s: str) -> str:
    """Normalize a scheme name for fuzzy matching."""
    s = s.lower()
    s = re.sub(r"\s*-\s*", "-", s)
    s = re.sub(r"[^a-z0-9]+", "", s)
    return s


def resolve_scheme_code(scheme_name: str) -> tuple[int | None, str | None]:
    """
    Find the best match for `scheme_name` in the mfapi.in master list.
    Returns (scheme_code, full_name) or (None, None) if no match.
    """
    master = _master_list()
    needle = _norm(scheme_name)
    if not needle:
        return None, None

    # Strategy 1: exact normalized match
    for entry in master:
        if _norm(entry.get("schemeName", "")) == needle:
            return int(entry["schemeCode"]), entry["schemeName"]

    # Strategy 2: contains match — pick the longest scheme name that is a substring
    best = None
    best_len = 0
    for entry in master:
        n = _norm(entry.get("schemeName", ""))
        if needle in n or n in needle:
            if len(n) > best_len:
                best_len = len(n)
                best = entry
    if best:
        return int(best["schemeCode"]), best["schemeName"]

    return None, None


# ---------- NAV fetch ----------
def _fetch_mf_history(code: int) -> list[dict]:
    """Fetch latest NAV history. mfapi.in returns ~last 30 prints.
    Retries up to 3 times with exponential backoff to handle timeouts."""
    import time as _time
    last_err: Exception | None = None
    for attempt in range(3):
        try:
            r = requests.get(f"https://api.mfapi.in/mf/{code}", timeout=15)
            r.raise_for_status()
            return r.json().get("data", [])
        except Exception as e:
            last_err = e
            _time.sleep(1.0 * (attempt + 1))
    raise RuntimeError(f"mfapi.in failed for {code} after 3 attempts: {last_err}")


def fetch_mf_rows(mfs: list[dict]) -> list[AssetRow]:
    """
    Fetch MF NAVs in parallel and compute day-change % directly from mfapi.in.

    No assumptions about which day it is — just use whatever mfapi.in returns
    as the latest NAV. Day-change = (latest NAV) vs (previous NAV in history).
    If mfapi.in is stale, the chart will reflect that automatically.
    """
    # Pre-resolve scheme codes serially (master list is in-process cached),
    # then fetch NAVs in parallel.
    resolved: list[tuple[int | None, str, dict]] = []
    for entry in mfs:
        name = entry.get("name", "").strip()
        units = float(entry.get("units", 0))
        if not name or units <= 0:
            continue
        code, full_name = resolve_scheme_code(name)
        if code is None:
            log.info("not matched: %s", name)
        resolved.append((code, full_name or name, entry))

    def _one(pair: tuple[int | None, str, dict]) -> AssetRow | None:
        code, full_name, entry = pair
        if code is None:
            return None
        units = float(entry.get("units", 0))
        try:
            hist = _fetch_mf_history(code)
        except Exception as e:
            log.warning("NAV fetch failed for %s: %s", full_name, e)
            return None
        if len(hist) < 1:
            return AssetRow(name=full_name, kind="mf", units=units,
                            value=0.0, prev_value=0.0, pct=0.0,
                            extra={"error": "no NAV data"})
        latest = hist[0]
        prev = hist[1] if len(hist) >= 2 else latest
        nav = float(latest.get("nav", 0))
        prev_nav = float(prev.get("nav", 0))
        value = units * nav
        prev_value = units * prev_nav
        pct = ((nav / prev_nav) - 1.0) * 100.0 if prev_nav else 0.0
        log.info("%-50s units=%10.2f  NAV=%8.4f (%s)  value=₹%s  day=%+.2f%%",
                 full_name[:50], units, nav, latest.get('date', ''),
                 f"{value:,.2f}", pct)
        return AssetRow(
            name=full_name, kind="mf", units=units,
            value=value, prev_value=prev_value, pct=pct,
            extra={"nav": nav, "nav_date": latest.get("date", "")},
        )

    results = map_parallel(_one, resolved, desc="MF NAVs")

    # Stitch results back into the original mfs order, adding "not matched"
    # placeholders for schemes we couldn't resolve.
    rows: list[AssetRow] = []
    seen: set[int] = set()
    for entry in mfs:
        name = entry.get("name", "").strip()
        units = float(entry.get("units", 0))
        if not name or units <= 0:
            continue
        code, full_name = resolve_scheme_code(name)   # cached, free
        if code is None:
            rows.append(AssetRow(name=name, kind="mf", units=units,
                                 value=0.0, prev_value=0.0, pct=0.0,
                                 extra={"error": "scheme not found"}))
            continue
        if code in seen:
            continue
        # find the matching result
        for (rc, _rn, _), asset in zip(resolved, results):
            if rc == code:
                seen.add(code)
                if asset is not None:
                    rows.append(asset)
                break
    return rows


# ---------- SGB price fetch ----------
def _fetch_mintbyte_sgb_prices() -> dict[str, dict] | None:
    """
    Scrape SGB market LTP from mintbyte.com (sourced from Motilal Oswal moAPI).
    Returns dict keyed by ISIN: {isin: {"price": float, "date": "YYYY-MM-DD"}}.
    No auth required. Updated daily.

    HTML structure: for each SGB row, the ISIN appears, followed by three
    ₹-prefixed numbers in order: issue price, market LTP, current gold spot.
    A date string (YYYY-MM-DD) appears near each ISIN.
    """
    try:
        r = requests.get(
            "https://mintbyte.com/sgb/premium/",
            headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                              "AppleWebKit/537.36 (KHTML, like Gecko) "
                              "Chrome/120.0.0.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            },
            timeout=20,
        )
        r.raise_for_status()
        html = r.text

        # Find each ISIN, take a 3000-char window, extract date + 3 prices
        results: dict[str, dict] = {}
        seen_isins: set[str] = set()
        # Use [0-9]+ (not \d{8}) — this Python build's regex has a quirk
        # where high {N} repetitions on \d/[0-9] fail to match.
        for m in re.finditer(r'(IN002[0-9]+)', html):
            isin = m.group(1)
            if isin in seen_isins:
                continue
            seen_isins.add(isin)
            window = html[m.start():m.start() + 3000]
            prices = re.findall(r'₹\s*([0-9,]+(?:\.[0-9]+)?)', window)
            dates = re.findall(r'([0-9]{4}-[0-9]{2}-[0-9]{2})', window)
            if len(prices) >= 2 and dates:
                # Position 1 is market LTP (after issue price at position 0)
                results[isin] = {
                    "price": float(prices[1].replace(",", "")),
                    "date": dates[0],
                }
        return results if results else None
    except Exception as e:
        print(f"[mintbyte] fetch failed: {e}")
        return None


# ---------- SGB history helpers (DB-backed, lazily cached) ----------
_sgb_history_cache: dict | None = None


def _load_sgb_history() -> dict:
    """Return the SGB history as ``{isin: {date: price}}`` from the SQLite DB.
    Lazily cached in-process; clears on process restart."""
    global _sgb_history_cache
    if _sgb_history_cache is not None:
        return _sgb_history_cache
    db = HistoryDB()
    out: dict[str, dict[str, float]] = {}
    with db._tx() as c:
        for row in c.execute("SELECT isin, date, price FROM sgb_price").fetchall():
            out.setdefault(row["isin"], {})[row["date"]] = float(row["price"])
    _sgb_history_cache = out
    return out


def _save_sgb_history(history: dict) -> None:
    """Persist ``{isin: {date: price}}`` into the SQLite DB.
    Cheap because INSERT OR REPLACE skips duplicates."""
    items: list[tuple[str, str, float, str]] = []
    for isin, dates in history.items():
        for d, p in dates.items():
            items.append((isin, d, float(p), "legacy-shim"))
    if items:
        HistoryDB().record_sgb_prices(items)
        # Refresh the in-memory cache so subsequent reads see the new rows.
        global _sgb_history_cache
        _sgb_history_cache = None


def fetch_mintbyte_with_history() -> dict[str, dict] | None:
    """
    Fetch today's mintbyte prices and update the history (SQLite DB).
    Returns the *latest* snapshot for each ISIN (same shape as _fetch_mintbyte_sgb_prices).
    """
    today_data = _fetch_mintbyte_sgb_prices()
    if not today_data:
        return None
    from datetime import datetime as _dt
    today_iso = _dt.now().strftime("%Y-%m-%d")
    db = HistoryDB()
    items: list[tuple[str, str, float, str]] = []
    for isin, info in today_data.items():
        items.append((isin, info["date"], float(info["price"]), "mintbyte"))
        if info["date"] != today_iso:
            # Also store under today_iso so the most-recent fetch has a
            # stable key aligned with the run date.
            items.append((isin, today_iso, float(info["price"]), "mintbyte-today"))
    db.record_sgb_prices(items)
    # Bust the in-memory shim cache.
    global _sgb_history_cache
    _sgb_history_cache = None
    return today_data


def get_sgb_prev_price(isin: str) -> tuple[float | None, str | None]:
    """
    Get the previous-day SGB price for a given ISIN from the local cache.
    Returns (prev_price, prev_date) or (None, None).
    """
    history = _load_sgb_history()
    isin_hist = history.get(isin.upper(), {})
    if not isin_hist:
        return None, None
    # Sort by date desc; skip today's entry
    today = pd.Timestamp.today().strftime("%Y-%m-%d")
    dated = [(d, p) for d, p in isin_hist.items() if d < today]
    if not dated:
        return None, None
    dated.sort(reverse=True)
    return dated[0][1], dated[0][0]


def _fetch_ibja_gold_price(date_str: str | None = None, occurrence: int = 0) -> tuple[float | None, str | None]:
    """
    Scrape IBJA (India Bullion and Jewellers Association) daily gold reference
    price for 999 purity. RBI uses IBJA as the official benchmark for SGB
    valuation. Returns (price_per_gram, date_str) or (None, None).

    The ibjarates.com homepage has AM and PM tables. Each table has ~4 days
    of history, most recent first. The AM and PM rows for the same date are
    usually identical or very close, so we deduplicate by date.

    occurrence:
      0 -> most recent (default)
      1 -> second most recent (for "yesterday")

    HTML pattern (one row per date in AM/PM table):
        <strong>DD/MM/YYYY</strong></td>
        ...
        <td data-label="Gold 999">RATE</td>
    """
    try:
        r = requests.get(
            "https://ibjarates.com/",
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=15,
        )
        r.raise_for_status()
        rows = re.findall(
            r'<strong>(\d{2}/\d{2}/\d{4})</strong>.*?Gold 999\">(\d+)</td>',
            r.text,
            re.DOTALL,
        )

        if not rows:
            return None, None

        # Deduplicate: AM and PM rows for the same date are identical or
        # near-identical — keep one per date.
        seen_dates: set[str] = set()
        unique: list[tuple[str, str]] = []
        for d, rate in rows:
            if d in seen_dates:
                continue
            seen_dates.add(d)
            unique.append((d, rate))

        if date_str:
            for d, rate in unique:
                if d == date_str:
                    return float(rate) / 10.0, d
            return None, None

        if occurrence >= len(unique):
            return None, None

        d, rate = unique[occurrence]
        return float(rate) / 10.0, d
    except Exception as e:
        print(f"[ibja] fetch failed: {e}")
        return None, None


# Cache the ISIN <-> NSE symbol mapping so we don't re-scrape mintbyte on every run.
_ISIN_TO_NSE_SYMBOL: dict[str, str] = {}
_NSE_SGB_DATA: list[dict] = []     # last successful NSE payload
_NSE_SGB_FETCHED_AT: float = 0.0   # monotonic timestamp of last fetch


def _fetch_nse_sgb_universe() -> list[dict] | None:
    """
    Fetch the full NSE SGB universe (all ~45 instruments) in one call.
    Endpoint: https://www.nseindia.com/api/sovereign-gold-bonds

    Returns a list of dicts with keys like:
        symbol      e.g. "SGBFEB32IV"
        ltP         last traded price (₹/g)
        prevClose   previous trading day's close
        chn, per    absolute & % change
        issue_price
        maturityDate

    Returns None on any failure; the caller should fall back to mintbyte.

    Note: NSE requires a session cookie from the SGB landing page. Without it
    the API returns a tiny 13-byte empty response.
    """
    global _NSE_SGB_DATA, _NSE_SGB_FETCHED_AT
    # Cache the result for 5 minutes per process — the API rarely changes
    # within a single Python run.
    if _NSE_SGB_DATA and (time.monotonic() - _NSE_SGB_FETCHED_AT) < 300:
        return _NSE_SGB_DATA
    try:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://www.nseindia.com/market-data/sovereign-gold-bond",
        }
        s = requests.Session()
        # Establish cookies via the landing page — NSE blocks API calls
        # without the right cookies set.
        s.get(
            "https://www.nseindia.com/market-data/sovereign-gold-bond",
            headers=headers,
            timeout=15,
        )
        r = s.get(
            "https://www.nseindia.com/api/sovereign-gold-bonds",
            headers=headers,
            timeout=15,
        )
        if r.status_code != 200 or len(r.text) < 1000:
            log.warning("NSE SGB API returned %s (%d bytes)",
                        r.status_code, len(r.text))
            return None
        data = r.json()
        if not isinstance(data, dict) or "data" not in data:
            return None
        _NSE_SGB_DATA = data["data"]
        _NSE_SGB_FETCHED_AT = time.monotonic()
        log.info("NSE: fetched %d SGB instruments", len(_NSE_SGB_DATA))
        return _NSE_SGB_DATA
    except Exception as e:
        log.warning("NSE SGB fetch failed: %s", e)
        return None


def _build_isin_to_nse_symbol_map() -> dict[str, str]:
    """
    Build {isin: nse_symbol} mapping from mintbyte.com's HTML.

    mintbyte's table lists every SGB with its ISIN and the NSE trading
    symbol (e.g. IN0020230184 -> SGBFEB32IV). We scrape this once and
    cache it in-process; missing entries fall back to a fuzzy match.
    """
    global _ISIN_TO_NSE_SYMBOL
    if _ISIN_TO_NSE_SYMBOL:
        return _ISIN_TO_NSE_SYMBOL
    mapping: dict[str, str] = {}
    try:
        r = requests.get(
            "https://mintbyte.com/sgb/premium/",
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            },
            timeout=20,
        )
        r.raise_for_status()
        # In mintbyte's HTML the columns appear in this order:
        #   ISIN, "SGB <series>", issue_price, interest_rate, maturity_date,
        #   days_left, LTP, LTP_date, IBJA_999_gold, arrow, change_pct,
        #   "From <date>", <NSE_symbol>
        # i.e. the NSE symbol appears AFTER (and to the right of) the ISIN
        # in the rendered page, but in the raw HTML the ISIN appears AFTER
        # its symbol because the table cells are emitted right-to-left in
        # some layouts. To be safe we look in both directions.
        for m in re.finditer(r'(IN002[0-9]+)', r.text):
            isin = m.group(1)
            # Look backward up to 1500 chars for the most recent SGB symbol
            before = r.text[max(0, m.start() - 1500):m.start()]
            before_syms = re.findall(r'(SGB[A-Z]+\d+[IVX]+)', before)
            if before_syms:
                mapping[isin] = before_syms[-1]
                continue
            # Fallback: look forward up to 2500 chars
            after = r.text[m.start():m.start() + 2500]
            after_syms = re.findall(r'(SGB[A-Z]+\d+[IVX]+)', after)
            if after_syms:
                mapping[isin] = after_syms[0]
        log.info("ISIN→NSE symbol map: %d entries from mintbyte", len(mapping))
    except Exception as e:
        log.warning("mintbyte symbol-map fetch failed: %s", e)
    _ISIN_TO_NSE_SYMBOL = mapping
    return mapping


def _fetch_nse_quote(isin: str) -> dict | None:
    """
    Fetch live SGB price from NSE's sovereign-gold-bonds API.

    Returns ``{"lastPrice": float, "previousClose": float, "symbol": str}``
    or None on failure. This is the price your demat account values your
    SGBs at on the Angel One app.
    """
    universe = _fetch_nse_sgb_universe()
    if not universe:
        return None
    sym_map = _build_isin_to_nse_symbol_map()
    target_symbol = sym_map.get(isin.upper())
    if not target_symbol:
        return None
    for d in universe:
        if d.get("symbol", "").upper() == target_symbol.upper():
            try:
                return {
                    "lastPrice": float(d["ltP"]),
                    "previousClose": float(d["prevClose"]),
                    "symbol": d["symbol"],
                }
            except (KeyError, ValueError):
                return None
    return None


def fetch_sgb_rows(sgbs: list[dict], asof: pd.Timestamp | None = None) -> list[AssetRow]:
    """
    Fetch SGB prices. Order of preference (most accurate first):
      1. NSE wholesale debt market (the price Angel One shows in your demat)
      2. mintbyte (Motilal Oswal moAPI) — broker-quoted indicative price;
         fallback when NSE is down or the ISIN is not on NSE's traded list
      3. Manual price in sgbs.json (user-provided, e.g. from sgbanalyzer.com)
      4. IBJA gold-spot proxy (RBI benchmark) — last resort
    Returns list of AssetRows including prev_value for XIRR.

    `asof` is accepted for backward compatibility but currently unused
    (IBJA's "today" is its own most recent date).
    """
    rows: list[AssetRow] = []

    # Fetch IBJA today (most recent) and prev (second most recent)
    ibja_today = _fetch_ibja_gold_price(occurrence=0)
    ibja_prev = _fetch_ibja_gold_price(occurrence=1)

    # Fetch mintbyte SGB prices (Motilal Oswal moAPI proxy) and update history
    mintbyte_data = fetch_mintbyte_with_history()
    if mintbyte_data:
        sample = list(mintbyte_data.items())[:3]
        sample_str = ", ".join(f"{i[0]}={i[1]['price']}" for i in sample)
        log.info("mintbyte: %d SGBs fetched  (e.g. %s)", len(mintbyte_data), sample_str)
    else:
        log.warning("mintbyte: fetch failed")

    if ibja_today[0]:
        log.info("IBJA 999 gold today:  ₹%s/g  (%s)", f"{ibja_today[0]:,.2f}", ibja_today[1])
    if ibja_prev[0]:
        log.info("IBJA 999 gold prev:   ₹%s/g  (%s)", f"{ibja_prev[0]:,.2f}", ibja_prev[1])

    for entry in sgbs:
        isin = entry.get("isin", "").strip().upper()
        units = float(entry.get("units", 0))
        name = entry.get("name", isin)
        manual_price = entry.get("manual_price_per_g")
        if not isin or units <= 0:
            continue

        last = prev = None
        source = ""

        # 1) Try NSE public quote (best when working)
        try:
            quote = _fetch_nse_quote(isin)
            if quote and quote.get("lastPrice") and quote.get("previousClose"):
                last = float(quote["lastPrice"])
                prev = float(quote["previousClose"])
                source = f"NSE ({quote.get('symbol', '')})"
                # Persist to the SQLite history so future runs have an
                # authoritative NSE-anchored prev-day price. This also
                # lets the HTML chart overlay use real NSE-traded values
                # instead of mintbyte's broker-quoted indicative price.
                try:
                    today_iso = pd.Timestamp.today().strftime("%Y-%m-%d")
                    HistoryDB().record_sgb_prices([(
                        isin, today_iso, last, f"NSE/{quote.get('symbol','')}"
                    )])
                    global _sgb_history_cache
                    _sgb_history_cache = None
                except Exception as persist_err:
                    log.debug("NSE price persist failed: %s", persist_err)
        except Exception:
            pass

        # 2) Mintbyte (Motilal Oswal moAPI) — auto-updated daily, no auth
        if last is None and mintbyte_data and isin in mintbyte_data:
            last = float(mintbyte_data[isin]["price"])
            # Use cached previous-day price from history
            prev_cached, prev_date = get_sgb_prev_price(isin)
            if prev_cached is not None:
                prev = prev_cached
                source = f"mintbyte (prev cached {prev_date})"
            else:
                # First run — no history yet; use today's price as prev (no day-change)
                prev = last
                source = "mintbyte (today only — no prev cached yet)"

        # 3) Manual price in sgbs.json (user-provided, e.g. from sgbanalyzer.com)
        if last is None and manual_price is not None:
            last = float(manual_price)
            manual_prev = entry.get("manual_prev_price_per_g")
            if manual_prev is not None:
                prev = float(manual_prev)
                source = "manual (both prices)"
            else:
                prev = float(manual_price)
                source = "manual (current only — no day-change)"

        # 4) IBJA gold-spot proxy (RBI benchmark) — last resort
        if last is None and ibja_today[0]:
            last = ibja_today[0]
            prev = ibja_prev[0] if ibja_prev[0] else ibja_today[0]
            source = "IBJA (RBI benchmark proxy)"

        if last is None:
            log.warning("no price for %s (%s) — add manual_price_per_g to sgbs.json", isin, name)
            rows.append(AssetRow(
                name=name, kind="sgb", units=units,
                value=0.0, prev_value=0.0, pct=0.0,
                extra={"error": "no price available", "isin": isin},
            ))
            continue

        value = units * last
        prev_value = units * prev
        pct = ((last / prev) - 1.0) * 100.0 if prev else 0.0
        rows.append(AssetRow(
            name=name, kind="sgb", units=units,
            value=value, prev_value=prev_value, pct=pct,
            extra={"price_per_g": last, "isin": isin, "source": source,
                   "buy_date": entry.get("buy_date", "")},
        ))
        log.info("%-50s %sg  ₹%s/g  value=₹%s  day=%+.2f%%  (%s)",
                 name[:50], f"{units:g}",
                 f"{last:,.0f}", f"{value:,.0f}", pct, source)
    return rows


# ---------- Aggregates ----------
def aggregate(rows: Iterable[AssetRow]) -> dict:
    rows = list(rows)
    value = sum(r.value for r in rows if r.value > 0)
    prev_value = sum(r.prev_value for r in rows if r.prev_value > 0)
    pct = ((value / prev_value) - 1.0) * 100.0 if prev_value > 0 else 0.0
    return {
        "value": value,
        "prev_value": prev_value,
        "pct": pct,
        "count": len([r for r in rows if r.value > 0]),
    }



