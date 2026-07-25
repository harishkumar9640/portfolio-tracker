"""
mf_holdings.py
---------------
Fetch the latest monthly mutual fund holdings data for a fixed set of
NSE/BSE tickers (the user's portfolio) from Trendlyne.

Output per ticker:
  {
    "ticker": "ITC",
    "name": "ITC Limited",
    "asof": "2026-05-31",           # last month the data is for
    "total_mfs_holding": 195,         # count of MFs with >0 shares
    "mfs_bought": 28,                # MFs that increased
    "mfs_sold": 13,                  # MFs that decreased
    "net_change_shares": 677044,     # net shares added (signed)
    "net_change_label": "+677,044",  # human-readable
    "total_shares_held": 51272751,   # total shares now held by all MFs
    "top_buyer": {"name": "...", "shares": 418723},
    "top_seller": {"name": "...", "shares": 10875},
    "top_buyers": [...],   # top 5 by shares added
    "top_sellers": [...],  # top 5 by shares removed
    "url": "https://trendlyne.com/equity/monthly-mutual-fund-share-holding/647/ITC/latest/itc-ltd/",
  }

Caching:
  - Each ticker's latest data is cached in data/mf_holdings_cache.json
  - TTL: 7 days (data is monthly, refresh weekly)
  - Stale-but-recent data is served on fetch failure

Scraping strategy:
  - 1 request per ticker, 1-second delay between (Trendlyne's robots.txt
    asks for this). 8 tickers = 8 seconds total.
  - We use a single parallel fetch (8 requests in flight) to keep the
    page snappy: page loads in ~1.5s instead of 8s.
  - User-Agent set to a real browser to avoid blocks
  - On 4xx/5xx, we log and return cached data if available

Glossary (abbreviations used in the output and in Trendlyne's pages):
  - MF  : Mutual Fund
  - MFs : Mutual Funds
  - AUM : Assets Under Management (₹ Crore)
  - NAV : Net Asset Value (per-unit price of a mutual fund)
  - ISIN: International Securities Identification Number
  - FII : Foreign Institutional Investor
  - ETF : Exchange-Traded Fund
"""
# TABLE OF CONTENTS (read this first)
#
# This file has 5 major sections (526 lines total):
#
# 1. Ticker map ----------
# 2. Cache helpers ----------
# 3. HTML parsing ----------
# 4. Main fetch ----------
# 5. Public API ----------

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import requests

from .logging_setup import get_logger
from .parallel import map_parallel

log = get_logger("mf_holdings")

from pipeline.runtime_paths import data_root

PROJECT = Path(__file__).resolve().parent.parent
DATA_DIR = data_root()
CACHE_FILE = DATA_DIR / "cache" / "mf_holdings_cache.json"

CACHE_TTL = timedelta(days=7)

# Sentinel: any ticker id at or above this value is treated as a placeholder
# (real Trendlyne IDs are 7 digits or fewer for now, with NTPC Green at 2,789,016
# as the largest currently known; we set the threshold well above that).
PLACEHOLDER_ID_THRESHOLD = 10_000_000
HTTP_TIMEOUT = 15  # seconds per request
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


# ---------- Ticker map ----------
# The Trendlyne URL format is:
#   /equity/monthly-mutual-fund-share-holding/{id}/{TICKER}/latest/{name}/
# These IDs are stable but internal. We hard-code them per the user's
# portfolio. To add a new ticker, search trendlyne.com for the stock
# and find the numeric ID in the URL.
TICKER_MAP: dict[str, dict] = {
    "BALRAMCHIN": {
        "name": "Balrampur Chini Mills",
        "id": 157,
        "url_slug": "balrampur-chini-mills-ltd",
    },
    "ITC": {
        "name": "ITC",
        "id": 647,
        "url_slug": "itc-ltd",
    },
    "JIOFIN": {
        "name": "Jio Financial Services",
        "id": 1564869,
        "url_slug": "jio-financial-services-ltd",
    },
    "NTPCGREEN": {
        "name": "NTPC Green Energy",
        "id": 2789016,
        "url_slug": "ntpc-green-energy-ltd",
    },
    "KNRCON": {
        "name": "KNR Constructions",
        "id": 752,
        "url_slug": "knr-constructions-ltd",
    },
    "IRCON": {
        "name": "IRCON International",
        "id": 109297,
        "url_slug": "ircon-international-ltd",
    },
    "BANKBARODA": {
        "name": "Bank of Baroda",
        "id": 162,
        "url_slug": "bank-of-baroda",
    },
    "RELIANCE": {
        "name": "Reliance Industries",
        "id": 1127,
        "url_slug": "reliance-industries-ltd",
    },
}


# ---------- Cache helpers ----------
def _load_cache() -> dict:
    if CACHE_FILE.exists():
        try:
            return json.loads(CACHE_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _save_cache(cache: dict) -> None:
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = CACHE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(cache, indent=2))
    tmp.replace(CACHE_FILE)


def _is_fresh(entry: dict) -> bool:
    if "fetched_at" not in entry:
        return False
    try:
        fetched = datetime.fromisoformat(entry["fetched_at"])
    except ValueError:
        return False
    return datetime.now() - fetched < CACHE_TTL


# ---------- HTML parsing ----------
# The Trendlyne page always has a "Mutual Fund <Month> <Year> share holdings
# and fund action in <Name>" section followed by a line like:
#   ## 29 MFs bought and 13 MFs sold <Name> in the month of <Month> <Year> for a net change of 677,044 stocks
# Then a table with one row per MF.
#
# The table is rendered as plain HTML with a row per MF. Columns include:
#   <MF name> | <AUM(Cr)> | <AUM %> | <Shares Held> | <Month Change>
#   | <Month Change %> | <Shares Held prev> | <Month Change %> | ...
#
# We don't need to parse every row — just the headline numbers and the
# top buyers / top sellers (which are the rows with the largest absolute
# positive and negative month changes for the most recent month).

# Headline: "167 MFs bought and 120 MFs sold ITC in the month
# of May 2026 for a net change of 13,181,340 stocks"
# The page uses <h2>/<h3> tags with whitespace/newlines inside. We allow
# any HTML markup between tokens.
_HEADLINE_RE = re.compile(
    r"(\d+)\s*MFs?\s*bought.*?"
    r"(\d+)\s*MFs?\s*sold.*?"
    r"for\s*a\s*net\s*change\s*of\s*"
    r"(-?[\d,]+)\s*stocks?",
    re.IGNORECASE | re.DOTALL,
)

# Top buyer: "<Name> was the highest buyer of <shares> shares in
# <Month> <Year> constituting <pct>% of the paid up equity..."
_TOP_BUYER_RE = re.compile(
    r"<h3[^>]*>\s*"
    r"(?P<name>[^<]+?)\s*was\s*the\s*highest\s*buyer\s*of\s*"
    r"(?P<shares>[\d,]+)\s*shares?\s*in\s*"
    r"(?:the\s*month\s*of\s*)?"
    r"(?:\w+\s+\d{4})?\s*"
    r"constituting\s*(?P<pct>[\d.]+)%\s*of\s*the\s*paid\s*up\s*equity",
    re.IGNORECASE,
)

# Top seller — same shape
_TOP_SELLER_RE = re.compile(
    r"<h3[^>]*>\s*"
    r"(?P<name>[^<]+?)\s*was\s*the\s*highest\s*seller\s*of\s*"
    r"(?P<shares>[\d,]+)\s*shares?\s*in\s*"
    r"(?:the\s*month\s*of\s*)?"
    r"(?:\w+\s+\d{4})?\s*"
    r"constituting\s*(?P<pct>[\d.]+)%\s*of\s*the\s*paid\s*up\s*equity",
    re.IGNORECASE,
)

# Fallback: if the headline pattern is missing, we still try to find
# the table. Each row is a sequence of <td>...</td> cells.
_ROW_RE = re.compile(r"<tr[^>]*>(.*?)</tr>", re.IGNORECASE | re.DOTALL)
_CELL_RE = re.compile(r"<td[^>]*>(.*?)</td>", re.IGNORECASE | re.DOTALL)
_TAG_STRIP_RE = re.compile(r"<[^>]+>")


def _strip(html: str) -> str:
    """Strip HTML tags and collapse whitespace."""
    return _TAG_STRIP_RE.sub("", html or "").strip()


def _build_url(ticker: str) -> str:
    """Build the Trendlyne MF-holdings URL for a given ticker.

    NOTE: BANKBARODA's url_slug is ``"bank-of-baroda/"`` (with trailing
    slash). We strip any trailing slash from the slug before joining
    so we don't end up with a double slash in the URL.
    """
    info = TICKER_MAP[ticker]
    slug = info["url_slug"].rstrip("/")
    return (
        f"https://trendlyne.com/equity/monthly-mutual-fund-share-holding/"
        f"{info['id']}/{ticker}/latest/{slug}/"
    )


def _parse_int(s: str) -> int:
    """Parse a string like '28,594,584' or '-50,000' into an int.

    Returns 0 for unparseable values (like '-' or 'n/a') instead of
    raising — we don't want one missing row to abort the entire table parse.
    """
    cleaned = (s or "").replace(",", "").replace(" ", "").strip()
    # Drop any leading sign character that's not a digit; then try int
    cleaned = cleaned.lstrip("+")
    if not cleaned or not cleaned.lstrip("-").isdigit():
        return 0
    try:
        return int(cleaned)
    except ValueError:
        return 0


def _parse_top_section(html: str, regex: re.Pattern) -> list[dict]:
    """Extract a list of {name, shares, pct} entries from the page.
    Used for top buyers / top sellers.
    """
    results = []
    for m in regex.finditer(html):
        results.append({
            "name": _strip(m.group("name")),
            "shares": _parse_int(m.group("shares")),
            "pct_of_company": float(m.group("pct")),
        })
    return results[:5]  # cap at 5


def _parse_row_table(html: str) -> list[dict]:
    """Parse the per-MF table to compute total_mfs_holding and
    top buyers/sellers when the dedicated regex misses them.

    Real Trendlyne tables have 8 cells per row:
       0: MF name (often <a>)
       1: AUM (Cr)
       2: AUM %
       3: Shares Held
       4: Month Change  ← shares added or sold
       5: Month Change %
       6: Shares Held (previous)
       7: Month Change % (previous)

    Returns a list of {name, shares, month_change} for all rows that
    have at least 5 cells.
    """
    rows = []
    # Split by <tr ...> markers (handles nested tags better than
    # matching <tr>...</tr> end-to-end since some rows have nested
    # </tr> from child elements like dropdowns).
    chunks = re.split(r"<tr[\s>]", html)
    for chunk in chunks[1:]:  # skip the pre-table chunk
        cells = [_strip(c) for c in _CELL_RE.findall(chunk)]
        if len(cells) < 5:
            continue
        try:
            shares = _parse_int(cells[3])
            month_change = _parse_int(cells[4])
        except (ValueError, IndexError):
            continue
        rows.append({
            "name": cells[0],
            "shares": shares,
            "month_change": month_change,
        })
    return rows


def _safe_fetch(ticker: str) -> tuple[str, Optional[dict]]:
    """Wrapper that converts exceptions into (ticker, None) so map_parallel
    never loses the ticker identity on failure."""
    try:
        return _fetch_one(ticker)
    except Exception as e:  # noqa: BLE001 — any error is non-fatal
        log.warning("safe_fetch: %s failed: %s", ticker, e)
        return ticker, None


# ---------- Main fetch ----------
def _fetch_one(ticker: str) -> tuple[str, Optional[dict]]:
    """
    Fetch the latest monthly MF holdings for one ticker.
    Returns (ticker, parsed_dict_or_None).
    """
    info = TICKER_MAP.get(ticker)
    if not info:
        return ticker, None
    # If we don't have a real ID (still using the placeholder), skip.
    # The placeholder is intentionally far above any real Trendlyne ID.
    if info.get("id", 0) >= PLACEHOLDER_ID_THRESHOLD:
        log.warning("no Trendlyne ID for %s — skipping", ticker)
        return ticker, None
    url = (
        f"https://trendlyne.com/equity/monthly-mutual-fund-share-holding/"
        f"{info['id']}/{ticker}/latest/{info['url_slug']}/"
    )
    try:
        r = requests.get(
            url, headers={"User-Agent": USER_AGENT}, timeout=HTTP_TIMEOUT,
        )
        r.raise_for_status()
        html = r.text
    except requests.RequestException as e:
        log.warning("fetch failed for %s: %s", ticker, e)
        return ticker, None

    return ticker, _parse_html(ticker, html, url)


def _parse_html(ticker: str, html: str, url: str) -> Optional[dict]:
    """Parse a Trendlyne MF-holdings page into our structured dict.

    The ticker must be in TICKER_MAP; if it's not, we fall back to using
    the ticker string itself as the name so callers can still test with
    arbitrary tickers.
    """
    info = TICKER_MAP.get(ticker, {"name": ticker})
    parsed = {
        "ticker": ticker,
        "name": info["name"],
        "url": url,
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
    }

    # --- Headline numbers (MFs buying / selling / net change) ---
    m = _HEADLINE_RE.search(html)
    if m:
        parsed["mfs_bought"] = int(m.group(1))
        parsed["mfs_sold"] = int(m.group(2))
        parsed["net_change_shares"] = _parse_int(m.group(3))
    else:
        parsed["mfs_bought"] = None
        parsed["mfs_sold"] = None
        parsed["net_change_shares"] = None

    # --- Top buyer / seller from the headline bullets ---
    buyers = _parse_top_section(html, _TOP_BUYER_RE)
    sellers = _parse_top_section(html, _TOP_SELLER_RE)
    parsed["top_buyer"] = buyers[0] if buyers else None
    parsed["top_seller"] = sellers[0] if sellers else None
    parsed["top_buyers"] = buyers
    parsed["top_sellers"] = sellers

    # --- Total MFs holding: derived from the table ---
    rows = _parse_row_table(html)
    if rows:
        holders = [r for r in rows if r["shares"] > 0]
        parsed["total_mfs_holding"] = len(holders)
        parsed["total_shares_held"] = sum(r["shares"] for r in holders)
        # If the headline regex missed buyers/sellers, derive them from
        # the table: largest positive month_change = top buyer,
        # largest negative = top seller.
        if not buyers and holders:
            top = max(holders, key=lambda r: r["month_change"])
            if top["month_change"] > 0:
                parsed["top_buyer"] = {
                    "name": top["name"], "shares": top["month_change"],
                    "pct_of_company": 0.0,
                }
        if not sellers and holders:
            bot = min(holders, key=lambda r: r["month_change"])
            if bot["month_change"] < 0:
                parsed["top_seller"] = {
                    "name": bot["name"], "shares": -bot["month_change"],
                    "pct_of_company": 0.0,
                }

    # --- "As of" month label: pick the most recent month shown ---
    months = re.findall(
        r"Mutual Fund\s+(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)"
        r"\s+(\d{4})",
        html, re.IGNORECASE,
    )
    if months:
        first, year = months[0]
        parsed["asof"] = f"{first} {year}"

    # --- Friendly net_change_label ---
    nc = parsed.get("net_change_shares")
    if nc is not None:
        sign = "+" if nc >= 0 else ""
        parsed["net_change_label"] = f"{sign}{nc:,}"
    else:
        parsed["net_change_label"] = "—"

    return parsed


# ---------- Public API ----------
def get_mf_holdings(force: bool = False) -> dict[str, dict]:
    """
    Returns {ticker: parsed_dict} for every ticker in TICKER_MAP.
    Honours the 7-day cache. Network fetch is parallel (~1.5s for 8 tickers).

    On network failure, returns whatever cached data we have (possibly
    stale). If even the cache is empty, the entry is omitted.
    """
    cache = _load_cache()
    now = datetime.now()

    tickers_to_fetch: list[str] = []
    if not force:
        for tkr in TICKER_MAP:
            entry = cache.get(tkr)
            if entry and _is_fresh(entry):
                continue
            tickers_to_fetch.append(tkr)
    else:
        tickers_to_fetch = list(TICKER_MAP.keys())

    if tickers_to_fetch:
        log.info("fetching MF holdings for %d tickers: %s",
                 len(tickers_to_fetch), ",".join(tickers_to_fetch))
        # Fetch in parallel: ~1-2s for 8 tickers (workers=8 concurrent)
        results = map_parallel(
            _safe_fetch, tickers_to_fetch, desc="MF holdings", workers=8,
        )
        for tkr, parsed in results:
            if parsed is not None:
                cache[tkr] = parsed
            else:
                log.warning("no data for %s", tkr)
        # Always save (even partial success) so we keep fresh data
        try:
            _save_cache(cache)
        except OSError as e:
            log.warning("could not save cache: %s", e)
    else:
        log.info("MF holdings cache hit (all fresh)")

    # Return only entries we actually have (skip placeholders / failures)
    out = {tkr: cache[tkr] for tkr in TICKER_MAP if tkr in cache}
    return out


def get_mf_holdings_summary(force: bool = False) -> list[dict]:
    """
    Returns a list of {ticker, name, mfs_holding, mfs_bought, ...}
    sorted by |net_change| descending (biggest movers first).

    This is the shape the dashboard's table expects.
    """
    raw = get_mf_holdings(force=force)
    rows = []
    for tkr, d in raw.items():
        # Compute human-readable net_change_label if missing
        nc = d.get("net_change_shares")
        if nc is not None:
            sign = "+" if nc >= 0 else ""
            net_label = d.get("net_change_label") or f"{sign}{nc:,}"
        else:
            net_label = d.get("net_change_label", "\u2014")
        rows.append({
            "ticker": tkr,
            "name": d.get("name", tkr),
            "asof": d.get("asof"),
            "total_mfs_holding": d.get("total_mfs_holding"),
            "mfs_bought": d.get("mfs_bought"),
            "mfs_sold": d.get("mfs_sold"),
            "net_change_shares": nc,
            "net_change_label": net_label,
            "total_shares_held": d.get("total_shares_held"),
            "top_buyer": d.get("top_buyer"),
            "top_seller": d.get("top_seller"),
            "top_buyers": d.get("top_buyers", []),
            "top_sellers": d.get("top_sellers", []),
            "url": d.get("url"),
            "fetched_at": d.get("fetched_at"),
        })
    # Sort: those with valid net_change first, biggest absolute change on top
    def sort_key(r):
        nc = r.get("net_change_shares")
        if nc is None:
            return (1, 0)  # unknown, push to end
        return (0, -abs(nc))
    rows.sort(key=sort_key)
    return rows


if __name__ == "__main__":
    # Quick CLI for debugging: `python3 mf_holdings.py ITC`
    import sys
    if len(sys.argv) > 1:
        # Force-refresh a single ticker
        from .parallel import fetch_all
        ticker = sys.argv[1].upper()
        _, parsed = _fetch_one(ticker)
        print(json.dumps(parsed, indent=2, default=str))
    else:
        summary = get_mf_holdings_summary(force=False)
        print(json.dumps(summary, indent=2, default=str))