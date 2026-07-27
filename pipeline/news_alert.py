"""
news_alert.py
-------------
Daily global news digest, sent to Telegram at 8:55 AM IST.

Categories (each maps to a risk type):
  📊  market_risk      — stock market moves, volatility, bubbles
  🏦  interest_rate    — central bank actions, yield curve, QT/QE
  💸  purchasing_power — inflation, CPI, currency debasement
  💱  exchange_rate    — dollar index, USD/INR, FX intervention
  ⚔️  geopolitical     — war, political, sanctions, treaties
  🏢  business_risk    — earnings misses, lawsuits, product recalls
  📑  financial_risk    — leverage, debt, ratings, going concern
  💥  default_risk      — bankruptcies, missed payments, sovereign default
  💧  liquidity_risk    — trading halts, money-market freezes, bank runs
  👔  management_risk   — CEO/CFO exits, fraud, governance
  🦠  pandemic          — global health crises
  🌍  economic          — general macro / economic news (catch-all)
  🌋  natural_disaster  — earthquakes, floods, cyclones (GDACS only)

Categories are checked in priority order; the FIRST match wins, so an
article matching multiple categories appears only once. This avoids
duplicate headlines for the same story.

Sources (all free, no API key needed):
  RSS:
    - BBC News (world, business)
    - Al Jazeera
    - Reuters / AP   (DNS-blocked in some regions; gracefully skipped)
    - NPR, Guardian, FT, CNBC, Bloomberg Markets, MarketWatch
    - Federal Reserve press releases
    - RBI press releases
  JSON APIs:
    - GDACS (Global Disaster Alert and Coordination System)
    - USGS Earthquakes (GeoJSON, M5.5+ in past 24h)

Delivery:
  Telegram Bot API: POST https://api.telegram.org/bot<TOKEN>/sendMessage
  Falls back to "dry-run" mode (log the message) when token is missing.

Schedule:
  Daily at 8:55 AM IST (3:25 AM UTC). Disable with NEWS_DISABLED=1.

Storage:
  data/news_alert_log.json  — last 30 runs
  data/news_alert_seen.json — set of article URLs sent in the last 7 days
                                (prevents duplicates if a feed updates slowly)

Environment variables (.env):
  NEWS_TELEGRAM_BOT_TOKEN   Bot token from @BotFather
  NEWS_TELEGRAM_CHAT_ID     Your personal chat_id
  NEWS_DRY_RUN              "1" = log only, "0" = actually send
  NEWS_DISABLED             "1" = skip the scheduler entirely
  NEWS_QUIET_HOURS          "22-6" = don't send between 10 PM and 6 AM
                            (mainly for ad-hoc runs, the 8:55 AM schedule
                            is unaffected)
  NEWS_PORTFOLIO_ONLY       "0" = send the full general digest (legacy
                                  behavior). Default "1" (recommended):
                                  filter to articles that score >= 4
                                  against the user's 11 holdings via
                                  portfolio_impact._find_affected_tickers.
                                  Drops FIFA-finals-style stories that
                                  don't impact any holding.

CLI:
  python3 news_alert.py               # one-shot run
  python3 news_alert.py --dry-run     # log message instead of sending
  python3 news_alert.py --force       # send even if no important news
  python3 news_alert.py --start-scheduler   # run forever as foreground daemon
"""
# TABLE OF CONTENTS (read this first)
#
# This file has 9 major sections (1180 lines total):
#
# 1. Configuration ----------
# 2. Article model ----------
# 3. Fetch + filter + categorise ----------
# 4. Render Telegram message ----------
# 5. Telegram send ----------
# 6. Main run ----------
# 7. Scheduler ----------
# 8. Scheduler missed-window logic (pure) ----------
# 9. CLI ----------

from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.parse
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import requests

try:
    from dotenv import load_dotenv
    _ENV_FILE = Path(__file__).resolve().parent.parent / ".env"
    if _ENV_FILE.exists():
        load_dotenv(_ENV_FILE, override=False)
except ImportError:
    pass

from .logging_setup import get_logger

log = get_logger("news_alert")

from pipeline.runtime_paths import data_root

PROJECT = Path(__file__).resolve().parent.parent
DATA_DIR = data_root()

LOG_FILE = DATA_DIR / "alerts" / "news" / "log.json"
SEEN_FILE = DATA_DIR / "alerts" / "news" / "seen.json"

IST = timezone(timedelta(hours=5, minutes=30))
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 PortfolioTracker/1.0"


# ---------- Configuration ----------

# RSS feed sources grouped by category. Each entry: (name, url, category, importance_keywords)
# importance_keywords is a list of substrings; an article is included in the
# category only if its title or description contains at least one of them
# (case-insensitive). Empty list = include all articles from this feed
# (useful for general-purpose news where we want breadth, not precision).
NEWS_FEEDS: list[dict] = [
    # General-purpose feeds (categorized by content matching below)
    {"name": "BBC World",   "url": "https://feeds.bbci.co.uk/news/world/rss.xml",   "category": None},
    {"name": "BBC Business", "url": "https://feeds.bbci.co.uk/news/business/rss.xml", "category": "economic"},
    {"name": "Al Jazeera",  "url": "https://www.aljazeera.com/xml/rss/all.xml",       "category": None},
    {"name": "NPR World",   "url": "https://feeds.npr.org/1004/rss.xml",              "category": None},
    {"name": "NPR Business", "url": "https://feeds.npr.org/1006/rss.xml",             "category": "economic"},
    {"name": "Guardian World", "url": "https://www.theguardian.com/world/rss",         "category": None},
    {"name": "Guardian Business", "url": "https://www.theguardian.com/business/rss",   "category": "economic"},
    {"name": "CNBC Top",    "url": "https://www.cnbc.com/id/100003114/device/rss/rss.html", "category": "economic"},
    {"name": "Bloomberg Markets", "url": "https://feeds.bloomberg.com/markets/news.rss", "category": "economic"},
    {"name": "MarketWatch Top", "url": "https://feeds.marketwatch.com/marketwatch/topstories/", "category": "economic"},
    {"name": "Investing.com", "url": "https://www.investing.com/rss/news.rss",        "category": "economic"},
    {"name": "FT World",    "url": "https://www.ft.com/world?format=rss",             "category": None},
    {"name": "FT Companies", "url": "https://www.ft.com/companies?format=rss",         "category": "economic"},

    # Central banks — always relevant for interest rate risk
    {"name": "Fed Press",   "url": "https://www.federalreserve.gov/feeds/press_all.xml", "category": "interest_rate"},
    {"name": "RBI Press",   "url": "https://www.rbi.org.in/scripts/BS_PressReleaseRSS.aspx", "category": "interest_rate"},

    # WHO outbreak news
    {"name": "WHO Outbreaks", "url": "https://www.who.int/feeds/entity/csr/disease-outbreak-news/rss.xml", "category": "pandemic"},

    # ----------------------------------------------------------------
    # Indian-context feeds (monsoon, local politics, festivals, RBI,
    # government policy, commodities, supply chain).
    # These are pinned to "economic" since their ticker-specific
    # direction depends on context (e.g. "monsoon" is bullish for
    # ITC rural demand but bearish for BALRAMCHIN if it disrupts
    # cane harvest). The portfolio_impact scanner applies per-ticker
    # sentiment for the actual direction.
    # ----------------------------------------------------------------
    {"name": "PIB India",          "url": "https://pib.gov.in/RssMain.aspx?ModId=6&Lang=1&Regid=3", "category": "economic"},
    {"name": "MoneyControl Economy","url": "https://www.moneycontrol.com/rss/economy.xml", "category": "economic"},
    {"name": "MoneyControl Markets","url": "https://www.moneycontrol.com/rss/marketreports.xml", "category": "economic"},
    {"name": "Business Standard Economy", "url": "https://www.business-standard.com/rss/economy-105.rss", "category": "economic"},
    {"name": "Business Standard Finance", "url": "https://www.business-standard.com/rss/finance-103.rss", "category": "economic"},
    {"name": "LiveMint Politics",  "url": "https://www.livemint.com/rss/politics", "category": "geopolitical"},
    {"name": "LiveMint Companies", "url": "https://www.livemint.com/rss/companies", "category": "economic"},
    {"name": "Hindu BusinessLine", "url": "https://www.thehindubusinessline.com/?service=rss", "category": "economic"},
    {"name": "NDTV Top Stories",   "url": "https://feeds.feedburner.com/NDTV-LatestNews", "category": "geopolitical"},
    {"name": "Times of India India","url": "https://timesofindia.indiatimes.com/rssfeeds/-2128936835.cms", "category": "geopolitical"},
    {"name": "Indian Express",     "url": "https://indianexpress.com/section/india/feed/", "category": "geopolitical"},
    {"name": "The Hindu National", "url": "https://www.thehindu.com/news/national/feeder/default.rss", "category": "geopolitical"},
]

# Category keywords for matching articles from general-purpose feeds.
# Categories are listed in priority order — the FIRST matching category
# wins, and overlapping articles are NOT duplicated across categories.
#
# Design (risk taxonomy: systematic + unsystematic):
#   systematic risks (market-wide)
#     - market_risk        broad market moves, volatility, bubbles
#     - interest_rate      central bank actions, yield curve
#     - purchasing_power   inflation, CPI, currency debasement
#     - exchange_rate      dollar index, rupee, forex
#     - geopolitical       war, political (already covered above)
#   unsystematic risks (company-specific)
#     - business_risk      earnings, lawsuits, product issues
#     - financial_risk     debt, leverage, ratings
#     - default_risk       defaults, bankruptcies, missed payments
#     - liquidity_risk     trading halts, money market freezes
#     - management_risk    CEO/CFO exits, fraud, governance
#   other categories (covered separately)
#     - pandemic           global health crises
#     - natural_disaster   earthquakes, floods (GDACS handles this)
CATEGORY_KEYWORDS: dict[str, list[str]] = [
    # ---- Pandemic first (high specificity) ----
    ("pandemic", [
        "pandemic", "epidemic", "outbreak", "who declares",
        "world health organization",
        " virus", " viral", " infection", "infected",
        "vaccine", "vaccination",
        "covid", "monkeypox", "mpox", "ebola", "nipah",
        "h5n1", "bird flu", "avian flu", "swine flu",
        "cholera", "plague", "marburg", "lassa",
        "quarantine", "containment", "public health emergency",
        "global health emergency",
    ]),

    # ---- Interest rate risk ----
    ("interest_rate", [
        # Central bank names (various word boundaries)
        "federal reserve", "the fed", "fed reserve", "fomc",
        " fed ", "fed.", "fed,", "fed:", "fed cuts", "fed hikes",
        "fed raises", "fed lowers", "fed keeps", "fed holds",
        "rbi ", "rbi.", "rbi,", "reserve bank of india",
        "ecb ", "ecb.", "ecb,", "european central bank",
        "bank of england", "boe ", "boe.", "boe,",
        "bank of japan", "boj ", "boj.", "boj,",
        "central bank", "central banks",
        # Rate decisions
        "interest rate", "interest rates",
        "rate cut", "rate cuts", "rate hike", "rate hikes",
        "rates cut", "rates hike", "rates raised", "rates lowered",
        "rate decision", "repo rate", "reverse repo",
        "policy rate", "key rate",
        # Officials
        "powell", "lagarde", "bailey", "ueda", "das ", " subbarao",
        # Monetary policy terms
        "monetary policy", "monetary tightening",
        "rate-setting", "rate setting",
        "quantitative easing", "quantitative tightening",
        "balance sheet", "tapering",
        # Yield curve
        "yield curve", "10-year yield", "bond yield", "bond yields",
        "treasury yield", "treasury yields",
    ]),

    # ---- Exchange rate / FX risk ----
    ("exchange_rate", [
        # Currency names + moves
        "exchange rate", "exchange rates", "forex", "fx market",
        "currency war", "currency wars", "currency crisis",
        "currency devaluation", "currency depreciation",
        " rupee ", "rupee falls", "rupee rises", "rupee slides",
        "rupee hits", "rupee weakens", "rupee strengthens",
        " dollar index", "dxy ", "dxy.", "dxy,",
        "weakens vs", "strengthens vs", "falls vs dollar",
        "rises vs dollar", "hits record low", "hits record high",
        "debasement", "dollarisation", "dollarization",
        "currency intervention", "forex reserves",
        "foreign exchange reserves",
    ]),

    # ---- Purchasing power / inflation risk ----
    ("purchasing_power", [
        "inflation", "deflation", "disinflation", "stagflation",
        " cpi ", "cpi.", "cpi,", "cpi:", "cpi data", "cpi report",
        "consumer price index",
        " ppi ", "ppi.", "ppi,", "producer price index",
        "wholesale price", "wpi ",
        "core inflation", "headline inflation", "cpi inflation",
        "real wages", "cost of living", "living costs",
        "purchasing power", "price pressures", "price pressures",
        "hyperinflation",
    ]),

    # ---- Market risk (broad market moves, volatility, bubbles) ----
    ("market_risk", [
        # Indices and broad market terms
        "stock market", "stock markets",
        "sensex", "nifty", "nifty50", "nifty 50",
        "s&p 500", "s&p500", "dow jones", "dow jones industrial",
        "nasdaq",
        "ftse 100", "ftse100", "dax ", "cac 40", "hang seng",
        "nikkei 225", "nikkei", "kospi",
        "shanghai composite", "shanghai", "bse ", "nse ",
        # Volatility
        "vix", "volatility index", "market volatility",
        "fear index", "fear gauge",
        # Crashes / corrections
        "market crash", "stock market crash", "circuit breaker",
        "trading halt", "trading halts",
        "market correction", "sell-off", "selloff",
        "bear market", "bull market",
        "free fall", "free-fall", "plunge", "tumble",
        "rout", "bloodbath",
        # Bubbles
        "bubble", "asset bubble", "stock bubble", "housing bubble",
        "bubble burst", "dot-com", "irrational exuberance",
        # Sector rotations
        "risk-on", "risk-off", "flight to safety",
        "safe haven", "safe-haven", "haven flows",
        "risk aversion", "risk appetite",
    ]),

    # ---- Financial risk (leverage, debt, balance sheet) ----
    # Comes before default_risk because credit-rating news is more often
    # a "financial_risk" signal than an imminent default.
    ("financial_risk", [
        "leverage", "leveraged", "deleveraging",
        "high debt", "debt burden", "debt load",
        "balance sheet", "liabilities", "off-balance-sheet",
        "credit rating", "credit ratings", "rating agency",
        "moody", " s&p ", " fitch ", "rating outlook",
        "downgrade watch", "negative outlook",
        "going concern", "going-concern",
        "covenant breach", "covenant violation",
        " debt-to-equity", "debt to equity",
        "interest coverage", "debt service",
        "credit event", "credit downgrade",
    ]),

    # ---- Default risk ----
    ("default_risk", [
        "default", "defaulted", "defaulting",
        "sovereign default", "debt default",
        "missed payment", "missed coupon", "missed interest",
        "rating downgrade", "downgrade to junk", "junk status",
        "chapter 11", "chapter 15", "chapter 7",
        "bankruptcy", "bankruptcy filing", "files for bankruptcy",
        "insolvent", "insolvency", "winding up",
        " fdic ", " fdic.", "fdic,",
        "resolution corp", "bad bank",
    ]),

    # ---- Liquidity risk ----
    ("liquidity_risk", [
        "liquidity crisis", "liquidity crunch",
        "liquidity squeeze", "liquidity stress",
        "cash crunch", "cash squeeze",
        "money market freeze", "money market fund",
        "franklin templeton",  # the famous 2020 India MF freeze
        "withdrawal freeze", "redemption freeze",
        "redemption pressure", "investor redemptions",
        "bank run", "bank runs", "depositor panic",
        "deposit flight",
    ]),

    # ---- Business risk (company-specific operations) ----
    ("business_risk", [
        # Earnings
        "earnings miss", "earnings beat", "earnings warning",
        "profit warning", "revenue miss", "revenue beat",
        "guidance cut", "cuts guidance", "lowers guidance",
        "downgrades outlook", "outlook cut",
        # Operations / product
        "product recall", "recall ", "recall.",
        "lawsuit", "class action", " sues ", " sues.",
        "verdict", "settlement",
        "antitrust", "monopoly", "price-fixing",
        "regulatory action", "regulator ", "sec charges",
        "ftc ", "fda rejection", "fda approval",
        "data breach", "cyberattack", "ransomware",
        # Sector-specific
        "plant shutdown", "factory fire", "mine collapse",
        "production halt", "supply disruption",
        "chip shortage", "semiconductor shortage",
    ]),

    # ---- Management risk ----
    ("management_risk", [
        # CEO/CFO exits
        "ceo resigns", "ceo quits", "ceo steps down",
        "cfo resigns", "cfo quits", "cfo steps down",
        "chairman resigns", "chairman quits",
        "md resigns", "managing director resigns",
        "coo resigns", "cto resigns", "cfo departure",
        "founder exits", "founder quits", "founder leaves",
        # Fraud / governance
        "fraud", " accounting fraud", "accounting scandal",
        "insider trading", "market manipulation",
        "ponzi", " ponzi ", " ponzi.", " ponzi,",
        "whistleblower", "whistle-blower",
        "related-party transaction", "related party transaction",
        "governance", "corporate governance",
        "boardroom", "board of directors",
        "activist investor", "proxy fight", "hostile takeover",
        # Investigations / regulatory action against companies
        " sebi ", "sebi.", "sebi,",
        " insider trading probe",
        "fraud charges", "indicted",
        " cfo arrested", "ceo arrested",
        "executive arrested", "executive charged",
    ]),

    # ---- Geopolitical / political (last so it doesn't steal war/political news) ----
    ("geopolitical", [
        "war ", "wars", "warfare", "invasion", "invades", "invaded",
        "military strike", "airstrike", "air strike", "missile",
        "ceasefire", "cease-fire", "truce",
        "troops", "soldier", "frontline", "front line",
        " israel", " gaza", "hamas", "hezbollah",
        "russia-ukraine", "ukraine war", "russia ukraine",
        "north korea", "taiwan strait", "south china sea",
        "conflict", "armed conflict", "hostilities",
        "nato ", "nuclear threat", "weapon",
        "geopolitical", "geopolitics",
        # Elections / political change
        "election", "elected", "referendum",
        "prime minister", "chancellor", "cabinet ",
        "coup", "regime change", "revolution",
        "impeach", "impeachment", "president resigns", "pm resigns",
        "brexit", "treaty", "peace accord", "summit",
        "un security council", "security council",
        "diplomatic", "bilateral", "foreign minister",
        "nato summit", "g7 summit", "g20 summit", "brics summit",
    ]),

    # ---- General economic news (catch-all for econ/macro news that
    # doesn't fit a specific risk bucket above) ----
    ("economic", [
        "gdp", "gross domestic product",
        "unemployment", "jobs report", "nonfarm payroll",
        "trade deficit", "trade surplus", "trade balance",
        "current account",
        "imf ", "world bank",
        "growth forecast", "recession warning",
        "fiscal deficit", "budget deficit", "sovereign debt",
        "tariff", "trade war", "sanctions", "embargo",
        "supply chain", "global trade",
    ]),
]

CATEGORY_KEYWORDS = dict(CATEGORY_KEYWORDS)

# Article-age cap: drop items older than this (in hours) so the digest
# stays focused on what happened in the last day
MAX_AGE_HOURS = 36

# Max articles per category in the final digest (keeps Telegram message
# short — Telegram's limit is 4096 chars per message)
MAX_PER_CATEGORY = 4


# ---------- Article model ----------

@dataclass
class Article:
    """One news item, possibly categorised."""
    title: str
    url: str
    source: str
    published: Optional[datetime]
    description: str = ""
    category: Optional[str] = None
    importance: int = 1   # 1=normal, 2=high (headline / wire-service)


def _normalize_text(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())


def _parse_pubdate(s: str) -> Optional[datetime]:
    """Parse RSS pubDate with several common formats. Returns naive UTC."""
    if not s:
        return None
    s = s.strip()
    # RFC 822 (most common in RSS): "Sat, 27 Jun 2026 08:25:43 GMT"
    for fmt in (
        "%a, %d %b %Y %H:%M:%S %Z",
        "%a, %d %b %Y %H:%M:%S %z",
        "%a, %d %b %Y %H:%M:%S GMT",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%d %H:%M:%S",
    ):
        try:
            dt = datetime.strptime(s, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc).replace(tzinfo=None)
        except ValueError:
            continue
    return None


def _parse_rss(xml_text: str, source_name: str) -> list[Article]:
    """
    Parse RSS / Atom / RDF feed into Article objects. Stdlib only,
    no feedparser dependency.

    Handles:
      - RSS 2.0   (<rss><channel><item>)
      - Atom 1.0  (<feed><entry>)         (BBC, FT use this sometimes)
      - RDF       (<rdf:RDF><item>)
    """
    articles: list[Article] = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        log.warning("RSS parse failed for %s: %s", source_name, e)
        return articles

    # RSS 2.0
    for item in root.findall(".//item"):
        title = _normalize_text(item.findtext("title", ""))
        link = _normalize_text(item.findtext("link", ""))
        desc = _normalize_text(item.findtext("description", ""))
        pub = item.findtext("pubDate") or item.findtext("dc:date", namespaces={"dc": "http://purl.org/dc/elements/1.1/"})
        if not link and item.find("guid") is not None:
            link = _normalize_text(item.findtext("guid", ""))
        if not title:
            continue
        articles.append(Article(
            title=title, url=link, source=source_name,
            published=_parse_pubdate(pub or ""), description=desc,
        ))

    # Atom 1.0
    ns = {"atom": "http://www.w3.org/2005/Atom"}
    for entry in root.findall(".//atom:entry", ns):
        title = _normalize_text(entry.findtext("atom:title", "", ns))
        link_el = entry.find("atom:link", ns)
        link = _normalize_text(link_el.get("href", "") if link_el is not None else "")
        desc = _normalize_text(entry.findtext("atom:summary", "", ns)) or _normalize_text(entry.findtext("atom:content", "", ns))
        pub = _normalize_text(entry.findtext("atom:published", "", ns)) or _normalize_text(entry.findtext("atom:updated", "", ns))
        if not title:
            continue
        articles.append(Article(
            title=title, url=link, source=source_name,
            published=_parse_pubdate(pub), description=desc,
        ))

    return articles


def _classify(article: Article) -> Optional[str]:
    """
    Decide which of the 7 categories this article belongs to. Returns None
    if the article doesn't match any of your alert types (so we can skip it).

    For feeds that already have a category assigned in NEWS_FEEDS, we honour
    that (BBC Business -> "economic", Fed Press -> "interest_rate", etc.).
    """
    # If the feed pinned a category, use it directly
    pinned = next(
        (f["category"] for f in NEWS_FEEDS if f["name"] == article.source),
        None,
    )
    if pinned:
        return pinned

    # Otherwise match title + description against keyword lists. We try
    # each category in priority order (most-specific first). An article
    # that matches multiple categories is assigned to the FIRST match —
    # this prevents the same article from appearing under multiple
    # risk categories in the digest.
    text = (article.title + " " + article.description).lower()
    # Priority mirrors the order of CATEGORY_KEYWORDS list, but we make
    # it explicit here for clarity. Pandemic and unsystematic risks
    # (business/management) are most specific, so they win over broad
    # categories like market_risk or geopolitical.
    priority = [
        "pandemic",
        "interest_rate",
        "exchange_rate",
        "purchasing_power",
        "market_risk",
        "financial_risk",
        "default_risk",
        "liquidity_risk",
        "business_risk",
        "management_risk",
        "geopolitical",
        "economic",
    ]
    for cat in priority:
        keywords = CATEGORY_KEYWORDS.get(cat, [])
        if any(kw in text for kw in keywords):
            return cat
    return None


def _gdacs_to_articles() -> list[Article]:
    """
    Fetch last 36 hours of GDACS events (earthquakes, floods, cyclones, etc.)
    and convert to Article objects, one per event with category="disasters".
    """
    since = (datetime.now(timezone.utc) - timedelta(hours=MAX_AGE_HOURS))
    url = "https://www.gdacs.org/gdacsapi/api/events/geteventlist/SEARCH"
    try:
        r = requests.get(url, params={"from": since.strftime("%Y-%m-%d")},
                         timeout=20, headers={"User-Agent": USER_AGENT})
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        log.warning("GDACS fetch failed: %s", e)
        return []

    out: list[Article] = []
    type_emoji = {"EQ": "🌋", "FL": "🌊", "TC": "🌀", "WF": "🔥", "VO": "🌋", "DR": "🏜️"}
    type_name = {"EQ": "Earthquake", "FL": "Flood", "TC": "Cyclone",
                 "WF": "Wildfire", "VO": "Volcano", "DR": "Drought"}
    for f in data.get("features", []):
        p = f.get("properties", {})
        event_type = p.get("eventtype", "")
        # Skip events older than MAX_AGE_HOURS
        from_str = p.get("fromdate", "")
        try:
            from_dt = datetime.fromisoformat(from_str.replace("Z", ""))
            if from_dt.replace(tzinfo=timezone.utc) < since:
                continue
        except (ValueError, AttributeError):
            pass

        # Skip Green (low) and only show Orange/Red
        if p.get("alertlevel") not in ("Orange", "Red"):
            continue

        emoji = type_emoji.get(event_type, "⚠️")
        name = type_name.get(event_type, event_type)
        country = p.get("country", "")
        mag = p.get("magnitude")
        depth = p.get("depth")
        desc_parts = [f"{emoji} *{name} in {country}*" if country else f"{emoji} *{name}*"]
        if mag:
            desc_parts.append(f"M{mag:.1f}")
        if depth:
            desc_parts.append(f"depth {depth} km")
        desc_parts.append(f"alert level {p.get('alertlevel', '?')}")
        title = f"{emoji} {name}: {p.get('name', country or event_type)} ({country})"

        # GDACS event detail URL
        lat = f.get("geometry", {}).get("coordinates", [None, None])[1] or 0
        lon = f.get("geometry", {}).get("coordinates", [None, None])[0] or 0
        event_id = p.get("eventid", "")
        gdacs_url = f"https://www.gdacs.org/report.aspx?eventid={event_id}&eventtype={event_type}"

        out.append(Article(
            title=title, url=gdacs_url, source="GDACS",
            published=from_dt.replace(tzinfo=None) if from_str else None,
            description=" · ".join(desc_parts),
            category="natural_disaster", importance=2,
        ))
    return out


# ---------- Fetch + filter + categorise ----------

def _load_seen() -> dict[str, str]:
    """Load {url: first_seen_iso} dict; auto-expire entries older than 7 days."""
    if not SEEN_FILE.exists():
        return {}
    try:
        data = json.loads(SEEN_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return {}
    cutoff = (datetime.now() - timedelta(days=7)).isoformat()
    return {u: ts for u, ts in data.items() if ts > cutoff}


def _save_seen(seen: dict[str, str]) -> None:
    SEEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = SEEN_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(seen, indent=2))
    tmp.replace(SEEN_FILE)


def fetch_articles(timeout: int = 20) -> list[Article]:
    """
    Fetch all configured RSS feeds + GDACS events. Returns a list of
    Article objects (raw, not yet categorised or filtered).
    """
    all_articles: list[Article] = []

    # RSS feeds — fetch in parallel for speed
    import concurrent.futures
    def _fetch_one(feed: dict) -> list[Article]:
        try:
            r = requests.get(feed["url"], timeout=timeout, headers={"User-Agent": USER_AGENT})
            r.raise_for_status()
            return _parse_rss(r.text, feed["name"])
        except Exception as e:
            log.warning("feed %s failed: %s", feed["name"], e)
            return []

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        for results in pool.map(_fetch_one, NEWS_FEEDS):
            all_articles.extend(results)

    # GDACS (disasters)
    all_articles.extend(_gdacs_to_articles())

    log.info("fetched %d raw articles from %d feeds + GDACS",
             len(all_articles), len(NEWS_FEEDS))
    return all_articles


def _filter_fresh(articles: list[Article]) -> list[Article]:
    """Drop articles older than MAX_AGE_HOURS or with no parseable date."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=MAX_AGE_HOURS)
    cutoff_naive = cutoff.replace(tzinfo=None)
    out = []
    for a in articles:
        if a.published is None:
            # No date: keep (some feeds omit it for evergreen content),
            # but only if it doesn't have an "old" feel
            out.append(a)
            continue
        if a.published >= cutoff_naive:
            out.append(a)
    return out


# ---------- Portfolio-impact filter ----------
#
# User feedback (2026-07-27): "those news are unrealted to my holdings most
# of the times ... only send me the article links when there is the real
# impact on my holdings due to that news, once an article get published -
# find the keywords in the article first with my holdings names, then if
# any get matched then only send those articles via telegram message.
# What does FIFA finals have impact on my holdings?"
#
# Implementation: the news_alert general digest is filtered through
# portfolio_impact._find_affected_tickers. Articles that score < 4
# against the user's 11 holdings are dropped before they reach
# _categorise_and_dedup. The result is a Telegram message that ONLY
# contains articles matching one of the user's held tickers / sectors /
# themes.
#
# Opt-out: set NEWS_PORTFOLIO_ONLY=0 (default 1) to revert to the
# pre-filter general-digest behavior.
#
# Edge case: if the user has zero holdings (truth file says qty=0 for
# everything) the filter would drop ALL news, which would silently
# break the digest. We never want that — if no portfolio is configured,
# fall back to general news so the user can still see the alert and
# notice their config is wrong.

NEWS_PORTFOLIO_ONLY_DEFAULT = True


def _portfolio_only_enabled() -> bool:
    """Return True if the news digest should be filtered to portfolio-impact only."""
    raw = os.environ.get("NEWS_PORTFOLIO_ONLY", "").strip().lower()
    if raw in ("0", "false", "no", "off"):
        return False
    if raw in ("1", "true", "yes", "on"):
        return True
    return NEWS_PORTFOLIO_ONLY_DEFAULT


def _filter_for_portfolio(articles):
    """
    Drop articles that don't affect any held ticker. Returns
    (filtered_articles, dropped_count).

    Uses portfolio_impact._find_affected_tickers to score each article;
    keeps only those with a ticker scoring >= 4.
    If portfolio_impact can't be imported (e.g. running this file in
    isolation), we leave the articles untouched and return (input, 0).
    """
    if not _portfolio_only_enabled():
        return list(articles), 0
    try:
        from .portfolio_impact import (
            _find_affected_tickers, PORTFOLIO_EXPOSURE,
        )
    except ImportError:
        return list(articles), 0
    if not PORTFOLIO_EXPOSURE:
        return list(articles), 0

    kept = []
    dropped = 0
    for a in articles:
        scores = _find_affected_tickers(a.title, a.description)
        if any(s >= 4 for s in scores.values()):
            kept.append(a)
        else:
            dropped += 1
    log.info(
        "portfolio filter: kept %d / %d articles (%d dropped, no impact on holdings)",
        len(kept), len(articles), dropped,
    )
    return kept, dropped


def _categorise_and_dedup(
    articles: list[Article],
    seen: dict[str, str],
) -> dict[str, list[Article]]:
    """
    Classify each article into one of the 7 categories, deduplicate against
    the "seen" cache (we've already alerted the user about this URL), and
    return a {category: [articles]} dict.
    """
    buckets: dict[str, list[Article]] = {c: [] for c in CATEGORY_KEYWORDS}
    seen_added: list[str] = []

    for a in articles:
        cat = _classify(a)
        if not cat:
            continue  # doesn't match any alert type
        a.category = cat
        # Dedup: skip if we've sent this URL in the past 7 days
        if a.url and a.url in seen:
            continue
        # For feeds with very generic categories, only keep articles
        # whose title is "important" (high word count or specific keywords)
        # — this is implicit in CATEGORY_KEYWORDS matching above
        buckets[cat].append(a)
        if a.url:
            seen.append(a.url) if False else seen_added.append(a.url)

    # Update seen cache
    now_iso = datetime.now().isoformat()
    for url in seen_added:
        seen[url] = now_iso

    # Sort each category by importance desc, then date desc
    def _sort_key(a: Article):
        return (-a.importance, -(a.published.timestamp() if a.published else 0))
    for cat in buckets:
        buckets[cat].sort(key=_sort_key)
        buckets[cat] = buckets[cat][:MAX_PER_CATEGORY]

    return buckets


# ---------- Render Telegram message ----------

# Display metadata for each category. Order here = display order in the
# digest. Risk-bucket categories come first (systematic then
# unsystematic), then contextual categories (pandemic, geopolitical).
CATEGORY_DISPLAY = {
    # ---- Systematic risks ----
    "market_risk":      ("📊",  "Market Risk (index moves, volatility, bubbles)"),
    "interest_rate":    ("🏦",  "Interest Rate Risk (Fed/RBI/ECB, yield curve)"),
    "purchasing_power": ("💸",  "Purchasing Power Risk (inflation, CPI, debasement)"),
    "exchange_rate":    ("💱",  "Exchange Rate Risk (USD/INR, dollar index, FX)"),
    "geopolitical":     ("⚔️ ",  "Geopolitical / Political / War"),
    # ---- Unsystematic risks ----
    "business_risk":     ("🏢",  "Business Risk (earnings, lawsuits, product issues)"),
    "financial_risk":    ("📑",  "Financial Risk (leverage, debt, ratings)"),
    "default_risk":      ("💥",  "Default Risk (bankruptcy, missed payments, sovereign default)"),
    "liquidity_risk":    ("💧",  "Liquidity Risk (trading halts, money-market freezes, bank runs)"),
    "management_risk":   ("👔",  "Management Risk (CEO/CFO exits, fraud, governance)"),
    # ---- Other ----
    "pandemic":          ("🦠",  "Pandemic / Health Crises"),
    "economic":          ("🌍",  "Other Macro / Economic News"),
    "natural_disaster":  ("🌋",  "Natural Disasters (M5.5+ quakes, floods, cyclones)"),
}


def _escape_md(text: str) -> str:
    """
    Escape Telegram MarkdownV1 special chars that aren't part of an
    intentional formatting element.

    Telegram's Markdown parser is strict: an unmatched '_' or '*' anywhere
    in the message kills the whole send. We only use Markdown for:
      - bold (*text*)
      - italic (_text_)
      - inline links [text](url)
    Anything else (like a stray underscore in a stock name or footer) must
    be backslash-escaped.
    """
    if not text:
        return ""
    # First protect intentional formatting by replacing with sentinels
    placeholders = {}
    counter = [0]
    def stash(m):
        key = f"\x00PH{counter[0]}\x00"
        counter[0] += 1
        placeholders[key] = m.group(0)
        return key
    # Stash [text](url) links
    text = re.sub(r"\[[^\]]+\]\([^)]+\)", stash, text)
    # Stash *bold*
    text = re.sub(r"\*[^*\n]+\*", stash, text)
    # Now escape remaining special chars
    text = re.sub(r"([_*`\[])", r"\\\1", text)
    # Restore the stashed formatting
    for key, original in placeholders.items():
        text = text.replace(key, original)
    return text


def _truncate(s: str, n: int) -> str:
    if len(s) <= n:
        return s
    return s[: n - 1].rstrip() + "…"


def render_telegram(
    buckets: dict[str, list[Article]],
    *,
    date_str: str,
    force: bool = False,
) -> Optional[str]:
    """
    Build the final Telegram message. Returns None if there's nothing
    worth sending (no articles in any category AND force=False).

    Telegram has a 4096-char limit per message; we cap at 3800 to leave
    headroom. If the message would be longer, we trim per-category items.
    """
    total = sum(len(v) for v in buckets.values())
    if total == 0 and not force:
        return None

    lines: list[str] = []
    lines.append(f"📰 *Global News Digest* — {date_str}")
    lines.append(f"_Sources: {len(NEWS_FEEDS)} feeds + GDACS_")
    lines.append("")

    if total == 0:
        lines.append("No significant alerts in the last 36 hours.")
        lines.append("")
        lines.append("— sent by Portfolio Tracker")
        return "\n".join(lines)

    # Show categories in priority order
    for cat, (emoji, label) in CATEGORY_DISPLAY.items():
        items = buckets.get(cat, [])
        if not items:
            continue
        lines.append(f"{emoji} *{label}*")
        for a in items:
            title = _truncate(a.title, 160)
            url = a.url if a.url else ""
            md_title = _escape_md(title)
            if url:
                lines.append(f"  • [{md_title}]({url})")
            else:
                lines.append(f"  • {md_title}")
            # Optional one-liner description (skip for natural disasters — already compact)
            if a.description and cat != "natural_disaster":
                desc = _normalize_text(re.sub(r"<[^>]+>", "", a.description))
                desc = _truncate(desc, 180)
                if desc and not desc.lower().startswith(title.lower()[:30]):
                    lines.append(f"    _{_escape_md(desc)}_")
        lines.append("")

    lines.append("— sent by Portfolio Tracker • news_alert.py")

    msg = "\n".join(lines)
    if len(msg) > 3800:
        # Hard cap: trim the largest category until under limit
        # Find the biggest category
        biggest = max(((c, len(buckets[c])) for c in buckets if buckets[c]),
                     key=lambda x: x[1], default=(None, 0))
        if biggest[0]:
            buckets[biggest[0]] = buckets[biggest[0]][:-1]
            return render_telegram(buckets, date_str=date_str, force=force)
        # If still over: just truncate the message
        msg = msg[:3797] + "…"
    return msg


# ---------- Telegram send ----------

def _env(name: str, default: str | None = None) -> str | None:
    val = os.environ.get(name)
    return val if val else default


def is_dry_run() -> bool:
    if os.environ.get("NEWS_DRY_RUN", "").lower() in ("1", "true", "yes"):
        return True
    if not _env("NEWS_TELEGRAM_BOT_TOKEN") or not _env("NEWS_TELEGRAM_CHAT_ID"):
        return True
    return False


def _in_quiet_hours() -> bool:
    """Skip sending during user-defined quiet hours (only for ad-hoc runs,
    the 8:55 AM scheduler is unaffected). Format: 'HH-HH' (24h)."""
    qh = _env("NEWS_QUIET_HOURS")
    if not qh:
        return False
    m = re.match(r"^(\d{1,2})-(\d{1,2})$", qh.strip())
    if not m:
        return False
    start_h, end_h = int(m.group(1)), int(m.group(2))
    now_h = datetime.now(IST).hour
    if start_h <= end_h:
        return start_h <= now_h < end_h
    # Wraps midnight (e.g. 22-6)
    return now_h >= start_h or now_h < end_h


def send_telegram(text: str) -> dict:
    """
    Send a message via Telegram Bot API.

    We deliberately use plain text (no parse_mode) because:
      1. Telegram's Markdown parser is strict — any unbalanced `_` or `*` char
         in a 3500-char RSS-derived message causes the WHOLE send to fail
      2. The URLs in our links already render as clickable links in
         Telegram even without [text](url) markdown
      3. Plain text is more reliable for system-generated messages

    Returns {"sent": bool, "mode": "telegram"|"dry_run", "chat_id": str,
             "message_length": int, "error": str (if any)}
    """
    bot_token = _env("NEWS_TELEGRAM_BOT_TOKEN")
    chat_id = _env("NEWS_TELEGRAM_CHAT_ID")
    dry = is_dry_run() or _in_quiet_hours()

    if dry:
        reason = ("dry-run (NEWS_DRY_RUN=1 or missing creds)"
                  if is_dry_run() else "quiet hours")
        log.info("[%s] would send Telegram message (%d chars) to chat=%s",
                 reason, len(text), chat_id)
        # Log a snippet
        for ln in text.splitlines()[:30]:
            log.info("  %s", ln)
        return {"sent": False, "mode": "dry_run",
                "reason": reason, "chat_id": chat_id,
                "message_length": len(text)}

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    try:
        # No parse_mode — Telegram will auto-detect links and make them
        # clickable. This avoids the "Can't parse entities" error when
        # our generated message has stray markdown chars.
        r = requests.post(url, json={
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": True,
        }, timeout=30)
        r.raise_for_status()
        data = r.json()
        if not data.get("ok"):
            return {"sent": False, "mode": "telegram", "error": str(data),
                    "chat_id": chat_id, "message_length": len(text)}
        log.info("telegram message sent (%d chars) to chat=%s",
                 len(text), chat_id)
        return {"sent": True, "mode": "telegram", "chat_id": chat_id,
                "message_length": len(text)}
    except Exception as e:
        log.error("telegram send failed: %s", e)
        return {"sent": False, "mode": "telegram", "error": str(e),
                "chat_id": chat_id, "message_length": len(text)}


# ---------- Main run ----------

def _append_log(entry: dict) -> None:
    log_list: list[dict] = []
    if LOG_FILE.exists():
        try:
            log_list = json.loads(LOG_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            log_list = []
    log_list.append(entry)
    log_list = log_list[-30:]
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = LOG_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(log_list, indent=2, default=str))
    tmp.replace(LOG_FILE)


def run_once(force_send: bool = False) -> dict:
    """
    One-shot: fetch news, categorise, build Telegram message, send.

    Returns a status dict for logging / API responses.
    """
    ran_at = datetime.now(IST).isoformat(timespec="seconds")
    errors: list[str] = []

    try:
        all_articles = fetch_articles()
    except Exception as e:
        log.exception("news fetch failed")
        errors.append(f"fetch failed: {e}")
        all_articles = []

    fresh = _filter_fresh(all_articles)
    # Filter to portfolio-impact only (see _filter_for_portfolio docstring)
    fresh, _dropped = _filter_for_portfolio(fresh)
    seen = _load_seen()
    buckets = _categorise_and_dedup(fresh, seen)
    _save_seen(seen)

    total_articles = sum(len(v) for v in buckets.values())
    date_str = datetime.now(IST).strftime("%d %b %Y")
    message = render_telegram(buckets, date_str=date_str, force=force_send)

    if message is None:
        log.info("no significant news today (%d fresh articles, %d dropped by portfolio filter)",
                 len(fresh), _dropped)
        result = {
            "ran_at": ran_at, "fetch_ok": True,
            "articles_total": len(all_articles),
            "articles_fresh": len(fresh),
            "articles_kept": total_articles,
            "articles_dropped_by_portfolio_filter": _dropped,
            "categories": {c: len(buckets[c]) for c in buckets},
            "telegram": {"sent": False, "reason": "no significant news"},
            "errors": errors,
        }
        _append_log(result)
        return result

    send_result = send_telegram(message)
    result = {
        "ran_at": ran_at, "fetch_ok": True,
        "articles_total": len(all_articles),
        "articles_fresh": len(fresh),
        "articles_kept": total_articles,
        "articles_dropped_by_portfolio_filter": _dropped,
        "categories": {c: len(buckets[c]) for c in buckets},
        "message_length": len(message),
        "telegram": send_result,
        "errors": errors,
    }
    _append_log(result)
    log.info("news alert: %d articles, telegram sent=%s",
             total_articles, send_result.get("sent"))
    return result


# ---------- Scheduler ----------

_scheduler_started = False
_scheduler_lock = threading.Lock()


def _next_run_ist(hour: int = 8, minute: int = 55) -> datetime:
    """Next 8:55 AM IST (= 03:25 UTC)."""
    now_ist = datetime.now(IST)
    target = now_ist.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now_ist:
        target = target + timedelta(days=1)
    return target.astimezone(timezone.utc).replace(tzinfo=None)


# ---------- Scheduler missed-window logic (pure) ----------

def _should_skip_missed_run(target_utc_naive: datetime,
                            now_utc_naive: datetime,
                            grace_secs: int = 5 * 60) -> tuple[bool, float]:
    """
    Decide whether to skip a scheduled run because the wake-up
    happened past the target by more than ``grace_secs``.

    Returns (skip, missed_by_secs).
    """
    missed_by = (now_utc_naive - target_utc_naive).total_seconds()
    return (missed_by > grace_secs), missed_by


def _scheduler_loop(stop_event: threading.Event) -> None:
    """Background thread: run once a day at 8:55 AM IST.

    Missed-window guard: if the system was asleep (or otherwise
    suspended) past the target time by more than MAX_MISSED_SECS,
    skip the run and wait for tomorrow's window. Without this guard a
    single overnight sleep would re-send the digest at the moment of
    wake-up, doubling the daily message.
    """
    MAX_MISSED_SECS = 5 * 60  # 5 minutes
    while not stop_event.is_set():
        # Compute next run as UTC-naive for easy arithmetic with
        # datetime.now(timezone.utc).replace(tzinfo=None).
        next_run_utc_naive = _next_run_ist()
        now_utc_naive = datetime.now(timezone.utc).replace(tzinfo=None)
        wait_secs = (next_run_utc_naive - now_utc_naive).total_seconds()

        # Convert to IST for the log line (the previous version logged
        # a UTC-naive timestamp labelled "IST", which was misleading).
        next_run_ist = next_run_utc_naive.replace(
            tzinfo=timezone.utc
        ).astimezone(IST)
        log.info(
            "news_alert scheduler: next run at %s IST (in %.0fs)",
            next_run_ist.strftime("%Y-%m-%d %H:%M:%S"),
            wait_secs,
        )

        while wait_secs > 0 and not stop_event.is_set():
            chunk = min(60, wait_secs)
            stop_event.wait(chunk)
            wait_secs -= chunk
        if stop_event.is_set():
            break

        # Re-check wall-clock against the target. threading.Event.wait()
        # uses time.monotonic() under the hood, which pauses during
        # system sleep on macOS — so if the Mac slept through the
        # 8:55 AM window, this thread will resume only when the Mac
        # wakes up and `wait_secs` will already be very negative.
        # Without the guard below we'd fire a late digest the moment
        # the lid opens, doubling the daily send.
        now_utc_naive = datetime.now(timezone.utc).replace(tzinfo=None)
        skip, missed_by = _should_skip_missed_run(
            next_run_utc_naive, now_utc_naive, MAX_MISSED_SECS
        )
        if skip:
            log.warning(
                "news_alert scheduler: missed target by %.0fs (>%ds) \u2014 "
                "skipping today's run to avoid duplicate send. "
                "Next attempt at tomorrow's 8:55 AM IST.",
                missed_by, MAX_MISSED_SECS,
            )
            # Sleep until tomorrow's window so the outer loop doesn't
            # tight-spin computing negative wait_secs forever.
            tomorrow_target = (
                next_run_utc_naive + timedelta(days=1)
            )
            while not stop_event.is_set():
                now_utc_naive = datetime.now(
                    timezone.utc
                ).replace(tzinfo=None)
                remaining = (tomorrow_target - now_utc_naive).total_seconds()
                if remaining <= 0:
                    break
                stop_event.wait(min(60, remaining))
            continue

        try:
            run_once()
        except Exception as e:
            log.exception("scheduled news run failed: %s", e)


def start_daily_scheduler() -> threading.Event:
    """Start the daily scheduler (daemon thread, runs at 8:55 AM IST)."""
    global _scheduler_started
    with _scheduler_lock:
        if _scheduler_started:
            return threading.Event()
        _scheduler_started = True
        stop_event = threading.Event()
        t = threading.Thread(target=_scheduler_loop, args=(stop_event,),
                             daemon=True, name="news-alert")
        t.start()
        log.info("news_alert scheduler started")
        return stop_event


# ---------- CLI ----------

def _cli():
    import argparse
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--dry-run", action="store_true",
                   help="Log the Telegram message instead of sending it.")
    p.add_argument("--force", action="store_true",
                   help="Send the message even if no significant news.")
    p.add_argument("--start-scheduler", action="store_true",
                   help="Run the scheduler in the foreground (Ctrl-C to stop).")
    p.add_argument("--render-only", action="store_true",
                   help="Just print the message that would be sent (no fetch).")
    args = p.parse_args()

    if args.dry_run:
        os.environ["NEWS_DRY_RUN"] = "1"

    if args.start_scheduler:
        stop = start_daily_scheduler()
        try:
            while not stop.wait(60):
                pass
        except KeyboardInterrupt:
            stop.set()
        return

    if args.render_only:
        # Build a fake message from sample buckets (for debugging the template)
        sample_buckets = {c: [] for c in CATEGORY_DISPLAY}
        sample_buckets["interest_rate"].append(Article(
            title="(sample) Fed cuts rates by 25 bps",
            url="https://example.com/fed", source="sample",
            published=datetime.now(), description="Sample description.",
            category="interest_rate", importance=2,
        ))
        print(render_telegram(sample_buckets, date_str="27 Jun 2026"))
        return

    result = run_once(force_send=args.force)
    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    _cli()