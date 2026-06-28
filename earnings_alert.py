"""
Earnings calendar alerts for the 8 portfolio stocks.

Sends two alerts per results day per stock:
  1. T-2 days, 8:55 AM IST — "heads-up" alert with consensus, mechanism
     notes, historical pattern.
  2. T-0 (results day), 8:55 AM IST — same-day reminder with focus on
     what to watch in the con-call.

Data sources (scraper-only, no Angel One):
  - NSE corporate announcements API for the calendar
  - Moneycontrol /financials/ pages for consensus estimates
  - Screener.in /consolidated/ pages for last 8 quarters actuals

Output:
  - Telegram message in the same format as news_alert.py
  - Dry-run by default (set EARNINGS_ALERT_DRY_RUN=0 to actually send)

Usage:
    python earnings_alert.py --run-once          # run check + send if any
    python earnings_alert.py --dry-run           # print, don't send
    python earnings_alert.py --start-scheduler   # daemon mode (8:55 AM IST)
    python earnings_alert.py --test-render ITC   # render alert for ITC for testing
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

import urllib.error
import urllib.request
from http.cookiejar import CookieJar

import sector_mechanisms as sm

# ---------------------------------------------------------------------------
# Constants & logging
# ---------------------------------------------------------------------------

IST = ZoneInfo("Asia/Kolkata")

PROJECT_ROOT = Path(__file__).resolve().parent
LOG_FILE = PROJECT_ROOT / "earnings_alert.log"
SEEN_FILE = PROJECT_ROOT / "earnings_alert_seen.json"   # dedup: alert_key -> date
LOG_FILE_HISTORY = PROJECT_ROOT / "earnings_alert_log.json"  # historical sends

# ---------- HTTP ----------

HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36"
    ),
    "Accept": "text/html,application/json",
    "Accept-Language": "en-IN,en;q=0.9",
}
HTTP_TIMEOUT = 20

# ---------- Logging ----------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("earnings_alert")


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class EarningsEvent:
    """A scheduled earnings (results) announcement for one portfolio stock."""
    ticker: str
    company_name: str
    report_date: datetime          # date of the announcement (IST)
    session: str                   # "pre-market" / "post-market" / "unknown"
    quarter_label: str             # e.g. "Q3 FY26"
    consensus_eps: Optional[float] = None
    consensus_revenue_cr: Optional[float] = None
    last_quarter_eps: Optional[float] = None
    last_quarter_revenue_cr: Optional[float] = None
    last_8q_eps: list[float] = field(default_factory=list)
    source_url: str = ""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _env(name: str, default: Optional[str] = None) -> Optional[str]:
    return os.environ.get(name, default)


def is_dry_run() -> bool:
    """Dry-run unless explicitly disabled."""
    flag = _env("EARNINGS_ALERT_DRY_RUN", "1")
    return flag not in ("0", "false", "False")


# ---------------------------------------------------------------------------
# Cookie-jar aware HTTP helpers (urllib's default urlopen doesn't share
# cookies between calls — NSE requires this).
# ---------------------------------------------------------------------------

_NSE_COOKIE_JAR: Optional[CookieJar] = None
_NSE_OPENER: Optional[urllib.request.OpenerDirector] = None


def _get_nse_opener() -> urllib.request.OpenerDirector:
    """Return a urllib opener with a shared cookie jar (so NSE cookies
    persist across requests)."""
    global _NSE_COOKIE_JAR, _NSE_OPENER
    if _NSE_OPENER is None:
        _NSE_COOKIE_JAR = CookieJar()
        _NSE_OPENER = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(_NSE_COOKIE_JAR),
        )
    return _NSE_OPENER


def _http_get(url: str, params: Optional[dict] = None) -> str:
    """Plain HTTP GET with our standard headers."""
    if params:
        from urllib.parse import urlencode
        url = f"{url}?{urlencode(params)}"
    req = urllib.request.Request(url, headers=HTTP_HEADERS)
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _http_get_with_cookies(url: str, params: Optional[dict] = None) -> str:
    """HTTP GET using the shared NSE cookie jar."""
    if params:
        from urllib.parse import urlencode
        url = f"{url}?{urlencode(params)}"
    req = urllib.request.Request(url, headers=HTTP_HEADERS)
    opener = _get_nse_opener()
    with opener.open(req, timeout=HTTP_TIMEOUT) as resp:
        return resp.read().decode("utf-8", errors="replace")


# NSE blocks API calls unless the client has session cookies (set by
# visiting the homepage first). We prime those cookies once per process.
_NSE_SESSION_PRIMED = False


def _prime_nse_session() -> None:
    """Visit NSE homepage once so we get cookies the API requires."""
    global _NSE_SESSION_PRIMED
    if _NSE_SESSION_PRIMED:
        return
    try:
        opener = _get_nse_opener()
        req = urllib.request.Request(
            "https://www.nseindia.com/", headers=HTTP_HEADERS,
        )
        with opener.open(req, timeout=HTTP_TIMEOUT) as resp:
            resp.read()  # discard body; we just want the cookie jar populated
        _NSE_SESSION_PRIMED = True
        log.debug("NSE session primed (%d cookies)",
                  len(_NSE_COOKIE_JAR) if _NSE_COOKIE_JAR else 0)
    except Exception as e:
        log.debug("NSE session prime failed (continuing): %s", e)


# ---------------------------------------------------------------------------
# Playwright (lazy-loaded, only used when NSE returns nothing via urllib)
# ---------------------------------------------------------------------------

_PLAYWRIGHT_AVAILABLE: Optional[bool] = None


def playwright_available() -> bool:
    """Check if Playwright is importable (cheap probe, cached)."""
    global _PLAYWRIGHT_AVAILABLE
    if _PLAYWRIGHT_AVAILABLE is None:
        try:
            import playwright  # noqa: F401
            _PLAYWRIGHT_AVAILABLE = True
        except ImportError:
            _PLAYWRIGHT_AVAILABLE = False
    return _PLAYWRIGHT_AVAILABLE


def fetch_nse_via_playwright(
    from_date: datetime, to_date: datetime,
) -> list[dict]:
    """Fetch NSE corporate announcements using a headless Chromium browser.

    NSE's API rejects plain urllib requests (403/[]/Access Denied) because
    it relies on browser-session cookies that the homepage sets. We use
    Playwright to load the homepage once, then call the API from the
    same browser context — this gets us real JSON.

    Returns raw list of announcement dicts (empty list on failure).
    """
    if not playwright_available():
        log.debug("playwright not installed; skipping browser fetch")
        return []

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return []

    rows: list[dict] = []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                ],
            )
            context = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0 Safari/537.36"
                ),
                viewport={"width": 1920, "height": 1080},
                locale="en-IN",
                timezone_id="Asia/Kolkata",
            )
            page = context.new_page()
            page.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', "
                "{get: () => undefined});"
            )

            try:
                page.goto(
                    "https://www.nseindia.com/",
                    wait_until="load", timeout=30000,
                )
            except Exception as e:
                log.debug("NSE homepage load via Playwright failed: %s", e)

            time.sleep(2)

            resp = page.request.get(
                "https://www.nseindia.com/api/corporate-announcements",
                params={
                    "index": "equities",
                    "from_date": from_date.strftime("%d-%m-%Y"),
                    "to_date": to_date.strftime("%d-%m-%Y"),
                },
                headers={
                    "Accept": "application/json",
                    "Referer": (
                        "https://www.nseindia.com/companies-listing/"
                        "corporate-announcements"
                    ),
                },
            )
            if resp.status == 200:
                body = resp.text()
                if body and body != "[]":
                    rows = json.loads(body)
                    log.info(
                        "Playwright NSE fetch: %d announcements",
                        len(rows),
                    )
            browser.close()
    except Exception as e:
        log.exception("Playwright fetch failed")
        return []

    return rows


def _quarter_label_for_date(d: datetime) -> str:
    """Indian-fiscal-quarter label for a calendar date.
    Indian FY: Apr-Mar. FY year = the year it ENDS in.
      Q1 = Apr-Jun (reported Jul-Sep)
      Q2 = Jul-Sep (reported Oct-Dec)
      Q3 = Oct-Dec (reported Jan-Mar)
      Q4 = Jan-Mar (reported Apr-Jun)
    """
    m, y = d.month, d.year
    if m in (4, 5, 6):
        return f"Q4 FY{y - 2000}"     # FY ending in current calendar year
    if m in (7, 8, 9):
        return f"Q1 FY{y + 1 - 2000}"
    if m in (10, 11, 12):
        return f"Q2 FY{y + 1 - 2000}"
    # Jan, Feb, Mar
    return f"Q3 FY{y - 2000}"


def _load_seen() -> dict[str, str]:
    """Load the dedup map: alert_key -> iso date last sent."""
    if not SEEN_FILE.exists():
        return {}
    try:
        with SEEN_FILE.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        log.exception("could not parse %s, starting empty", SEEN_FILE)
        return {}


def _save_seen(seen: dict[str, str]) -> None:
    """Persist dedup map."""
    try:
        with SEEN_FILE.open("w", encoding="utf-8") as f:
            json.dump(seen, f, indent=2, sort_keys=True)
    except Exception:
        log.exception("could not write %s", SEEN_FILE)


def _append_history(entry: dict) -> None:
    """Append an entry to the alert-history log (one JSON object per line)."""
    try:
        with LOG_FILE_HISTORY.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        log.exception("could not append to %s", LOG_FILE_HISTORY)


# ---------------------------------------------------------------------------
# NSE corporate announcements
# ---------------------------------------------------------------------------

# NSE publishes results dates under:
#   https://www.nseindia.com/companies-listing/corporate-announcements
# But the actual API is:
#   https://www.nseindia.com/api/corporates-announcements?index=equities
#   &from_date=DD-MM-YYYY&to_date=DD-MM-YYYY
# For a "financial results" filter:
#   https://www.nseindia.com/api/corporates-financial-results?index=equities
#   &from_date=DD-MM-YYYY&to_date=DD-MM-YYYY&symbol=SYMBOL

# We will pull a date window around "today" (T-3 .. T+1) and filter for
# the 8 tickers + the "Financial Results" announcement type.

NSE_RESULTS_URL = (
    "https://www.nseindia.com/api/corporates-financial-results"
)
# NSE's general corporate-announcements endpoint is the one that
# actually returns data with browser-cookie sessions. We filter this
# for "financial results" type announcements.
NSE_ANNOUNCEMENTS_URL = (
    "https://www.nseindia.com/api/corporate-announcements"
)


# Keywords in `desc` (description) field that mark an announcement
# as a quarterly-results announcement. We match any of these (case
# insensitive). These are NSE's actual desc values from observation:
#   "Financial Result Updates"   (the common case)
#   "Financial Results" / "Audited Financial Results" / "Unaudited ..."
#   "Limited Review Report"
#   "Board Meeting" only when attch contains "result"
_RESULTS_DESC_KEYWORDS = (
    "financial result",
    "audited financial",
    "unaudited financial",
    "limited review",
)


def _is_results_announcement(desc: str, attch: str) -> bool:
    """True iff this announcement is about quarterly/annual results."""
    desc_l = (desc or "").lower()
    attch_l = (attch or "").lower()
    if any(kw in desc_l for kw in _RESULTS_DESC_KEYWORDS):
        return True
    # Board meeting alone (no "result" in desc) is a con-call notice,
    # NOT a results filing. We only treat it as results if the
    # attachment text explicitly mentions "result".
    if "board meeting" in desc_l and "result" in attch_l:
        return True
    return False


def _sibling_names(
    tickers: list[str], name_by_ticker: dict[str, list[str]],
) -> dict[str, set[str]]:
    """For each ticker, the set of tokens used by ANY sibling's
    aliases. Used to compute disambiguating needles."""
    out: dict[str, set[str]] = {}
    for tkr in tickers:
        sibling_tokens: set[str] = set()
        for other_tkr in tickers:
            if other_tkr == tkr:
                continue
            for needle in name_by_ticker.get(other_tkr, []):
                sibling_tokens.update(needle.lower().split())
        out[tkr] = sibling_tokens
    return out


def _is_disambiguating_needle(
    needle: str, tkr: str, siblings: dict[str, set[str]],
) -> bool:
    """A needle is disambiguating if it has at least one token that
    does NOT appear in any sibling's name AND it's not just the bare
    ticker symbol (too ambiguous on its own)."""
    if not needle or len(needle) < 5:
        return False
    needle_tokens = set(needle.lower().split())
    if not needle_tokens:
        return False
    # Bare single-word needle (length <= 8) is never disambiguating.
    # "reliance" (8) matches Reliance Home Finance, "industries"
    # (10) appears in many company names. Real disambiguation comes
    # from multi-word phrases like "reliance industries" or "jio
    # platforms", or from distinctive single words like "ambani".
    if len(needle_tokens) == 1:
        only = next(iter(needle_tokens))
        if len(only) <= 8:
            return False
    # Must have at least one token unique to this ticker
    unique = needle_tokens - siblings.get(tkr, set())
    return len(unique) > 0


def _parse_nse_row(row: dict, name_by_ticker: dict[str, list[str]],
                    tickers: list[str]) -> Optional[EarningsEvent]:
    """Map one NSE announcement row to an EarningsEvent (or None)."""
    sym = (row.get("symbol") or "").upper().strip()
    comp = (row.get("comp") or "").lower()
    desc = (row.get("desc") or "").lower()
    attch = (row.get("attchmntText") or "").lower()
    date_iso = row.get("an_dt") or row.get("date") or row.get("recDate") or ""

    if not sym and not comp:
        return None

    matched_ticker: Optional[str] = None
    for tkr in tickers:
        if sym == tkr:
            # Exact symbol match — no ambiguity
            matched_ticker = tkr
            break

    if not matched_ticker:
        # Fall back to alias match. To avoid matching "Reliance
        # Industries" against "Reliance Home Finance" / "Reliance
        # Communications" / "Reliance Infrastructure" / etc., we
        # build "disambiguating tokens" from each alias and require
        # at least ONE token that doesn't appear in any OTHER
        # portfolio company's name/aliases. The bare ticker
        # ("reliance", "itc") is too ambiguous and is excluded.
        siblings = _sibling_names(tickers, name_by_ticker)
        for tkr in tickers:
            for needle in name_by_ticker.get(tkr, []):
                if not _is_disambiguating_needle(needle, tkr, siblings):
                    continue
                if needle in comp or needle in attch:
                    matched_ticker = tkr
                    break
            if matched_ticker:
                break

    if not matched_ticker:
        return None

    # Filter to financial-results type only.
    if not _is_results_announcement(desc, attch):
        return None

    # Parse date. NSE format: "20-Jan-2026 15:30:00"
    try:
        report_dt = datetime.strptime(date_iso.split()[0], "%d-%b-%Y").replace(
            tzinfo=IST,
        )
    except (ValueError, IndexError, AttributeError):
        log.debug("could not parse date %r for %s", date_iso, sym)
        return None

    session = "post-market"
    if "pre-market" in attch or "morning session" in attch:
        session = "pre-market"

    return EarningsEvent(
        ticker=matched_ticker,
        company_name=row.get("comp") or matched_ticker,
        report_date=report_dt,
        session=session,
        quarter_label=_quarter_label_for_date(report_dt),
        source_url=(
            "https://www.nseindia.com/companies-listing/"
            "corporate-announcements"
        ),
    )


def fetch_nse_results(
    tickers: list[str],
    from_date: datetime,
    to_date: datetime,
) -> list[EarningsEvent]:
    """Fetch financial-results announcements from NSE for the given window.

    Strategy:
      1. Try urllib first (fast, no browser startup). If NSE returns
         real data, use it.
      2. If urllib returns [] or fails, fall back to Playwright
         (slower, but NSE cooperates with browser-cookie sessions).

    Returns a list of EarningsEvent objects. Best-effort; returns empty
    list on any failure (logged) so the alert pipeline never crashes.
    """
    # Build alias map (ticker -> multiple search strings)
    from portfolio_impact import PORTFOLIO_EXPOSURE  # type: ignore

    name_by_ticker: dict[str, list[str]] = {}
    for tkr, info in PORTFOLIO_EXPOSURE.items():
        names = {info["name"].lower()}
        names.update(a.lower() for a in info["aliases"])
        names.add(tkr.lower())
        name_by_ticker[tkr.upper()] = list(names)

    params = {
        "index": "equities",
        "from_date": from_date.strftime("%d-%m-%Y"),
        "to_date": to_date.strftime("%d-%m-%Y"),
    }

    rows: list[dict] = []

    # --- 1. urllib attempt (fast path, with shared cookie jar) ---
    _prime_nse_session()
    try:
        body = _http_get_with_cookies(
            NSE_ANNOUNCEMENTS_URL, params=params,
        )
        if body and body != "[]":
            parsed = json.loads(body)
            if isinstance(parsed, list) and len(parsed) > 0:
                rows = parsed
                log.info("urllib NSE fetch: %d announcements", len(rows))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
        log.warning("urllib NSE fetch failed: %s", e)
    except Exception as e:
        log.exception("unexpected urllib NSE error")

    # --- 2. Playwright fallback ---
    if not rows:
        rows = fetch_nse_via_playwright(from_date, to_date)

    if not rows:
        log.warning(
            "NSE returned no announcements for window %s..%s",
            params["from_date"], params["to_date"],
        )
        return []

    events: list[EarningsEvent] = []
    for row in rows:
        ev = _parse_nse_row(row, name_by_ticker, tickers)
        if ev is not None:
            events.append(ev)

    # Dedup by (ticker, date) — NSE often publishes the same result
    # twice (once as "Financial Result Updates" with the summary text,
    # once as the actual results PDF). Keep the first occurrence.
    seen_keys: set[tuple[str, datetime]] = set()
    deduped: list[EarningsEvent] = []
    for ev in events:
        key = (ev.ticker, ev.report_date.date())
        if key in seen_keys:
            continue
        seen_keys.add(key)
        deduped.append(ev)

    log.info("NSE results: %d events in window %s..%s",
             len(deduped), params["from_date"], params["to_date"])
    return deduped


# ---------------------------------------------------------------------------
# Moneycontrol consensus + screener.in historicals
# ---------------------------------------------------------------------------

# Moneycontrol "financials" page for a stock:
#   https://www.moneycontrol.com/financials/<slug>/consolidated-profit-loss/
# Concensus estimates (analyst expectations):
#   https://www.moneycontrol.com/stocks/company-info/stockrecommendations/
#   <slug>/<company-id>
#
# Screener.in for last-8-quarters actuals:
#   https://www.screener.in/company/<slug>/consolidated/
#
# These scrapers are best-effort: if HTML changes, we degrade gracefully
# (None for the missing field, alert still renders).


_MC_SLUGS: dict[str, str] = {
    "ITC": "itc",
    "RELIANCE": "relianceindustries",
    "JIOFIN": "jiofinancialservices",
    "BANKBARODA": "bankofbaroda",
    "NTPCGREEN": "ntpcgreenenergy",
    "KNRCON": "knrconstructions",
    "IRCON": "irconinternational",
    "BALRAMCHIN": "balrampurchinimills",
}

_SCREENER_SLUGS: dict[str, str] = {
    "ITC": "ITC",
    "RELIANCE": "RELIANCE",
    "JIOFIN": "JIOFIN",
    "BANKBARODA": "BANKBARODA",
    "NTPCGREEN": "NTPCGREEN",
    "KNRCON": "KNRCON",
    "IRCON": "IRCON",
    "BALRAMCHIN": "BALRAMCHIN",
}


def fetch_consensus(ticker: str) -> dict:
    """Best-effort fetch of consensus EPS + revenue from Moneycontrol.

    Returns dict with optional keys:
        consensus_eps: float
        consensus_revenue_cr: float
        last_quarter_eps: float
        last_quarter_revenue_cr: float
        source: str  (URL we scraped)
    All fields may be None on failure.
    """
    slug = _MC_SLUGS.get(ticker)
    if not slug:
        return {}

    # Moneycontrol consensus URL — analyst recommendations page often
    # has a small table with mean / median EPS estimate.
    url = (
        f"https://www.moneycontrol.com/stocks/company-info/"
        f"stockrecommendations/{slug}/1"
    )
    try:
        html = _http_get(url)
    except Exception as e:
        log.debug("Moneycontrol fetch failed for %s: %s", ticker, e)
        return {"source": url}

    out: dict = {"source": url}

    # Consensus EPS: look for "Mean" / "Median" rows near EPS / "Earnings"
    # Moneycontrol's analyst-recos page typically shows a table with
    # period / mean EPS / median EPS. We look for the first numeric EPS.
    eps_matches = re.findall(
        r"(?:Mean|Median|Average)[^<]*?EPS[^<]*?([\d]+\.\d+)",
        html, flags=re.IGNORECASE,
    )
    if eps_matches:
        try:
            out["consensus_eps"] = float(eps_matches[0])
        except ValueError:
            pass

    # Revenue is rarely on this page — fall back to screener.in below.
    return out


def fetch_historical_actuals(ticker: str) -> list[float]:
    """Best-effort fetch of last 8 quarters EPS from screener.in."""
    slug = _SCREENER_SLUGS.get(ticker)
    if not slug:
        return []
    url = f"https://www.screener.in/company/{slug}/consolidated/"

    try:
        html = _http_get(url)
    except Exception as e:
        log.debug("screener fetch failed for %s: %s", ticker, e)
        return []

    # Screener.in renders quarters as table headers in a "quarters" table.
    # The structure (heuristic): <th>Q1 FY26</th><td>EPS</td>... repeated.
    # We grab all EPS cells that look like "X.XX" in the first numeric row.
    eps_pattern = re.compile(r"<td[^>]*>\s*([\d]+\.\d+)\s*</td>")
    matches = eps_pattern.findall(html)
    # Take first 8 numeric cells — best-effort; will likely include
    # revenue too on some layouts, but EPS is the first row in screener.
    # We use only cells that are "reasonable" EPS values (0.01 to 1000).
    plausible = []
    for m in matches:
        try:
            v = float(m)
        except ValueError:
            continue
        if 0.01 <= v <= 1000:
            plausible.append(v)
    return plausible[:8]


# ---------------------------------------------------------------------------
# Alert rendering
# ---------------------------------------------------------------------------

INDIAN_NUMBER_FORMAT_PREFIX = "₹"


def _fmt_eps(v: Optional[float]) -> str:
    if v is None:
        return "N/A"
    return f"₹{v:.2f}"


def _fmt_revenue(v: Optional[float]) -> str:
    if v is None:
        return "N/A"
    if v >= 1000:
        return f"₹{v / 1000:.2f} lakh cr"
    return f"₹{v:,.0f} cr"


def _escape_md(text: str) -> str:
    """Escape characters that break Telegram MarkdownV2."""
    return re.sub(r"([_*\[\]()~`>#+\-=|{}.!\\])", r"\\\1", text)


def render_alert(
    event: EarningsEvent,
    mode: str,                            # "T-2" or "T-0"
    mechanism: dict | None = None,
    seen_count: int = 0,
) -> str:
    """Render a single earnings alert in Telegram-friendly MarkdownV2.

    mode='T-2' is the heads-up alert.
    mode='T-0' is the same-day reminder.
    """
    m = mechanism or sm.get_mechanism(event.ticker) or {}
    lines: list[str] = []

    heading = (
        "📅 *2 DAYS TO RESULTS*" if mode == "T-2"
        else "🚨 *RESULTS DAY — TODAY*"
    )

    date_str = event.report_date.strftime("%a %d %b %Y")
    lines.append(heading)
    lines.append("")
    lines.append(f"*{event.company_name}* ({event.ticker})")
    lines.append(f"📅 {date_str} • {event.session}")
    lines.append(f"📊 {event.quarter_label}")

    # Consensus
    lines.append("")
    lines.append("🎯 *Consensus estimates*")
    lines.append(
        f"   • EPS: {_fmt_eps(event.consensus_eps)}"
    )
    lines.append(
        f"   • Revenue: {_fmt_revenue(event.consensus_revenue_cr)}"
    )
    if event.last_quarter_eps is not None:
        lines.append(
            f"   • Last quarter EPS: {_fmt_eps(event.last_quarter_eps)}"
        )

    # Historical pattern
    if event.last_8q_eps:
        recent = event.last_8q_eps[:4]
        lines.append("")
        lines.append("📈 *Recent quarterly EPS:*")
        lines.append(
            "   " + "  •  ".join(f"₹{v:.2f}" for v in recent)
        )

    lines.append("")
    if m.get("results_day_history"):
        lines.append(f"📊 *Pattern on results day:*")
        lines.append(f"   {m['results_day_history']}")
        lines.append("")

    # Mechanism — primary drivers (1-2 only, in T-0 we focus on watch items)
    if m.get("primary_drivers"):
        lines.append("🔍 *What actually moves this stock on results day:*")
        for d in m["primary_drivers"][:3 if mode == "T-2" else 2]:
            lines.append(f"   • {d}")
        lines.append("")

    # T-0: emphasise watch-items (con-call focus)
    if mode == "T-0" and m.get("watch_items"):
        lines.append("👂 *Listen for these in the con-call:*")
        for w in m["watch_items"][:4]:
            lines.append(f"   • {w}")
        lines.append("")
    elif mode == "T-2" and m.get("watch_items"):
        # T-2: just tease the first watch item so the user knows what to listen for
        first = m["watch_items"][0]
        lines.append(f"👂 *Top thing to listen for tomorrow:*")
        lines.append(f"   • {first}")
        lines.append("")

    # Bellwether phrases
    if m.get("management_bellwethers") and mode == "T-0":
        lines.append("🗣️ *Management phrases to listen for:*")
        for b in m["management_bellwethers"][:3]:
            lines.append(f"   • {b}")
        lines.append("")

    # Footer
    lines.append(f"🇮🇳 _{m.get('sector', 'Portfolio stock')}_")
    lines.append(f"\\#{event.ticker} #Earnings")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Telegram sending
# ---------------------------------------------------------------------------

def send_telegram(text: str) -> dict:
    """Send to Telegram. Mirrors news_alert.send_telegram."""
    if is_dry_run():
        log.info("[DRY-RUN] would send %d chars to Telegram:\n%s",
                 len(text), text)
        return {"sent": False, "mode": "dry_run", "chars": len(text)}

    bot_token = _env("NEWS_TEGRAM_BOT_TOKEN") or _env("EARNINGS_TELEGRAM_BOT_TOKEN")
    chat_id = _env("NEWS_TELEGRAM_CHAT_ID") or _env("EARNINGS_TELEGRAM_CHAT_ID")

    if not bot_token or not chat_id:
        log.warning(
            "no Telegram credentials set "
            "(NEWS_TELEGRAM_BOT_TOKEN / NEWS_TEGRAM_CHAT_ID) — skipping"
        )
        return {"sent": False, "mode": "no_credentials", "chars": len(text)}

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = json.dumps({
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "MarkdownV2",
        "disable_web_page_preview": True,
    }).encode("utf-8")

    req = urllib.request.Request(
        url, data=payload, headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            body = resp.read().decode("utf-8")
            log.info("Telegram OK: %d chars", len(text))
            return {"sent": True, "mode": "telegram", "chars": len(text),
                    "response": body[:200]}
    except Exception as e:
        log.exception("Telegram send failed")
        return {"sent": False, "mode": "error", "error": str(e),
                "chars": len(text)}


# ---------------------------------------------------------------------------
# Core pipeline: find events → render → send
# ---------------------------------------------------------------------------

def find_relevant_events(
    tickers: list[str], today: datetime
) -> list[tuple[str, EarningsEvent]]:
    """Find T-2 and T-0 events for the given tickers.

    Window: T-3 .. T+1 — covers the case where we missed yesterday
    (still want T-2 alert) and gives us a 1-day buffer on either side.
    """
    window_start = today - timedelta(days=3)
    window_end = today + timedelta(days=2)

    raw = fetch_nse_results(tickers, window_start, window_end)

    out: list[tuple[str, EarningsEvent]] = []
    for ev in raw:
        delta = (ev.report_date.date() - today.date()).days
        if delta == 2:
            out.append(("T-2", ev))
        elif delta == 0:
            out.append(("T-0", ev))
        # We deliberately skip T-1 (too late for heads-up), T-3 (too early),
        # and T+1 / T+2 (post-results).
    return out


def enrich_event(event: EarningsEvent) -> EarningsEvent:
    """Attach consensus + historicals to an event."""
    consensus = fetch_consensus(event.ticker)
    if "consensus_eps" in consensus:
        event.consensus_eps = consensus["consensus_eps"]
    event.source_url = consensus.get("source", event.source_url)

    hist = fetch_historical_actuals(event.ticker)
    if hist:
        event.last_8q_eps = hist
        # The most recent actual = the previous quarter's EPS
        event.last_quarter_eps = hist[0]
    return event


def alert_key(ticker: str, report_date: datetime, mode: str) -> str:
    return f"{ticker}|{report_date.strftime('%Y-%m-%d')}|{mode}"


def run_once(today: Optional[datetime] = None, force_send: bool = False) -> dict:
    """One-shot: find events, render, send (deduped)."""
    today = today or datetime.now(IST)
    tickers = sm.list_configured_tickers()

    raw_pairs = find_relevant_events(tickers, today)
    log.info("found %d candidate alerts for window around %s",
             len(raw_pairs), today.strftime("%Y-%m-%d"))

    seen = _load_seen()
    sent_count = 0
    skipped_count = 0
    errors: list[str] = []

    for mode, ev in raw_pairs:
        key = alert_key(ev.ticker, ev.report_date, mode)
        if not force_send and key in seen:
            skipped_count += 1
            continue

        try:
            ev = enrich_event(ev)
        except Exception as e:
            log.exception("enrich failed for %s", ev.ticker)
            errors.append(f"enrich {ev.ticker}: {e}")
            continue

        mechanism = sm.get_mechanism(ev.ticker)
        text = render_alert(ev, mode=mode, mechanism=mechanism)

        result = send_telegram(text)
        seen[key] = today.strftime("%Y-%m-%d")
        sent_count += 1

        _append_history({
            "ts": today.isoformat(timespec="seconds"),
            "ticker": ev.ticker,
            "mode": mode,
            "report_date": ev.report_date.strftime("%Y-%m-%d"),
            "quarter": ev.quarter_label,
            "sent": result.get("sent", False),
            "mode_out": result.get("mode"),
            "chars": result.get("chars", 0),
        })

    _save_seen(seen)
    return {
        "ran_at": today.isoformat(timespec="seconds"),
        "candidates": len(raw_pairs),
        "sent": sent_count,
        "skipped": skipped_count,
        "errors": errors,
    }


# ---------------------------------------------------------------------------
# Daemon scheduler (8:55 AM IST daily — same as mf_holdings_alert.py)
# ---------------------------------------------------------------------------

_scheduler_started = False
_scheduler_lock = threading.Lock()


def _next_run_ist(hour: int = 8, minute: int = 55) -> datetime:
    now = datetime.now(IST)
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return target


def _scheduler_loop(stop_event: threading.Event) -> None:
    while not stop_event.is_set():
        target = _next_run_ist()
        wait_s = (target - datetime.now(IST)).total_seconds()
        log.info("earnings_alert scheduler: next run at %s IST (in %.0fs)",
                 target.strftime("%H:%M"), wait_s)
        if stop_event.wait(timeout=wait_s):
            return
        try:
            run_once()
        except Exception as e:
            log.exception("scheduled run failed: %s", e)


def start_daily_scheduler() -> threading.Event:
    """Start the background daemon. Returns stop_event (call .set() to stop)."""
    global _scheduler_started
    with _scheduler_lock:
        if _scheduler_started:
            log.info("earnings_alert scheduler already running")
            return threading.Event()
        _scheduler_started = True
        stop_event = threading.Event()
        t = threading.Thread(
            target=_scheduler_loop, args=(stop_event,),
            name="earnings_alert_scheduler", daemon=True,
        )
        t.start()
        log.info("earnings_alert scheduler started")
        return stop_event


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    p = argparse.ArgumentParser(description="Earnings calendar alerts")
    p.add_argument("--run-once", action="store_true",
                   help="Run one check and exit")
    p.add_argument("--dry-run", action="store_true",
                   help="Force dry-run mode for this invocation")
    p.add_argument("--start-scheduler", action="store_true",
                   help="Start background daemon (runs forever)")
    p.add_argument("--test-render", metavar="TICKER",
                   help="Render a sample alert for the given ticker")
    p.add_argument("--force", action="store_true",
                   help="Re-send even if already in seen map")
    args = p.parse_args()

    if args.dry_run:
        os.environ["EARNINGS_ALERT_DRY_RUN"] = "1"

    if args.test_render:
        m = sm.get_mechanism(args.test_render.upper())
        if not m:
            print(f"no mechanism configured for {args.test_render}")
            sys.exit(1)
        # Build a synthetic event for rendering
        ev = EarningsEvent(
            ticker=args.test_render.upper(),
            company_name=m["name"],
            report_date=datetime.now(IST) + timedelta(days=2),
            session="post-market",
            quarter_label="Q3 FY26",
            consensus_eps=7.40,
            consensus_revenue_cr=None,
            last_quarter_eps=7.12,
            last_quarter_revenue_cr=None,
            last_8q_eps=[7.12, 6.85, 7.40, 6.20, 5.95, 7.10, 6.80, 6.55],
        )
        print(render_alert(ev, mode="T-2", mechanism=m))
        print("\n----- T-0 variant -----\n")
        print(render_alert(ev, mode="T-0", mechanism=m))
        return

    if args.start_scheduler:
        stop = start_daily_scheduler()
        try:
            while not stop.is_set():
                time.sleep(1)
        except KeyboardInterrupt:
            log.info("scheduler interrupted")
            stop.set()
        return

    # default: --run-once
    result = run_once(force_send=args.force)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()