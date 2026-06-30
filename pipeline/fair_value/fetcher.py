"""
fair_value.fetcher
------------------
Scrape screener.in for fundamental data on Indian stocks.

Differences vs. the original fairvalue_check.py:
  - Live in the portfolio-tracker package; importable from tests.
  - Use the project's logging_setup so cache misses / failures show up
    in logs/YYYY-MM-DD/app.log.
  - Return a richer dict (incl. market_cap, ROE, ROCE when parseable)
    so the web UI can render more than just Graham / DCF.
"""
from __future__ import annotations

import hashlib
import os
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import requests
from bs4 import BeautifulSoup

from ..logging_setup import get_logger

log = get_logger("fair_value")

PROJECT = Path(__file__).resolve().parent.parent.parent
CACHE_DIR = PROJECT / ".cache" / "screener"
CACHE_EXPIRE_HOURS = 1

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36"
    ),
}


def _ensure_cache_dir() -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)


def _get_cache_path(url: str) -> Path:
    url_hash = hashlib.md5(url.encode("utf-8")).hexdigest()
    return CACHE_DIR / f"{url_hash}.html"


def _is_cache_valid(cache_path: Path) -> bool:
    if not cache_path.exists():
        return False
    file_time = datetime.fromtimestamp(cache_path.stat().st_mtime)
    return datetime.now() - file_time < timedelta(hours=CACHE_EXPIRE_HOURS)


def _get_cached_page(url: str) -> str:
    """Return page HTML, hitting the cache when fresh."""
    _ensure_cache_dir()
    cache_path = _get_cache_path(url)

    if _is_cache_valid(cache_path):
        log.debug("cache hit %s", url)
        return cache_path.read_text(encoding="utf-8")

    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        html = resp.text
        cache_path.write_text(html, encoding="utf-8")
        return html
    except requests.RequestException as e:
        # If we have any cached version (even stale), prefer it to a hard fail.
        if cache_path.exists():
            log.warning("using stale cache for %s: %s", url, e)
            return cache_path.read_text(encoding="utf-8")
        raise


def _clean_number(text: str) -> Optional[float]:
    """Strip currency/commas/percent and return a float."""
    if not text:
        return None
    cleaned = re.sub(r"[₹Rs,%]", "", text).replace(",", "").strip()
    try:
        return float(cleaned)
    except ValueError:
        return None


def _extract_value_after_label(strings: list[str], label: str) -> Optional[float]:
    """Given a flat list of strings, find `label` and return the next number."""
    try:
        idx = strings.index(label)
    except ValueError:
        return None
    i = idx + 1
    # Skip currency symbols
    while i < len(strings) and strings[i] in ("₹", "Rs"):
        i += 1
    if i >= len(strings):
        return None
    return _clean_number(strings[i])


def fetch(ticker: str) -> dict:
    """
    Fetch fundamental data for a ticker from screener.in.

    Returns a dict with at minimum:
        ticker, current_price, eps, book_value, market_cap,
        operating_cash_flow_per_share, source_url, fetched_at

    Returns a dict with only `ticker` + `error` on failure.
    """
    ticker = ticker.upper().strip()
    url = f"https://www.screener.in/company/{ticker}/consolidated/"

    out: dict = {
        "ticker": ticker,
        "source_url": url,
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
    }

    try:
        html = _get_cached_page(url)
    except Exception as e:
        log.warning("fetch failed for %s: %s", ticker, e)
        out["error"] = str(e)
        return out

    soup = BeautifulSoup(html, "html.parser")

    # Top ratios block
    top_ratios = soup.find(id="top-ratios")
    if top_ratios:
        strings = list(top_ratios.stripped_strings)
        out["current_price"] = _extract_value_after_label(strings, "Current Price")
        out["market_cap"] = _extract_value_after_label(strings, "Market Cap")

    # EPS (latest year)
    eps_label = soup.find(string=lambda t: t and "EPS in Rs" in t)
    if eps_label:
        parent_td = eps_label.parent
        if parent_td:
            siblings = parent_td.find_next_siblings("td")
            if siblings:
                out["eps"] = _clean_number(siblings[0].get_text(strip=True))

    # Book Value (latest year)
    bv_label = soup.find(string=lambda t: t and "Book Value" in t)
    if bv_label:
        parent = bv_label.parent
        if parent:
            for sib in parent.next_siblings:
                if getattr(sib, "name", None):
                    text = sib.get_text(strip=True)
                    if text:
                        out["book_value"] = _clean_number(text)
                        break

    # Operating cash flow per share
    market_cap = out.get("market_cap")
    price = out.get("current_price")
    if market_cap and price and market_cap > 0 and price > 0:
        cf_section = soup.find(id="cash-flow")
        if cf_section:
            table = cf_section.find("table", class_="data-table")
            if table:
                tbody = table.find("tbody")
                if tbody:
                    for row in tbody.find_all("tr"):
                        first_td = row.find("td")
                        if not first_td:
                            continue
                        label_text = first_td.get_text(strip=True)
                        if "Free Cash Flow" in label_text or "Cash from Operating Activity" in label_text:
                            tds = row.find_all("td")
                            if len(tds) >= 2:
                                last_td = tds[-1]
                                value_cr = _clean_number(last_td.get_text(strip=True))
                                if value_cr is not None:
                                    # FCF per share = (FCF_cr * price) / market_cap_cr
                                    out["operating_cash_flow_per_share"] = (
                                        value_cr * price / market_cap
                                    )
                                    break

    return out