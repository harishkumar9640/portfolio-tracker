"""
portfolio_impact.py
------------------
Cross-references news from news_alert.py against your 8 holdings and
alerts you on Telegram when a story is likely to move your portfolio.

This is the "be my portfolio manager" piece — for each news article, we
score how strongly it affects each of your 8 tickers based on:

  1. Direct hit      — the article mentions the company name or ticker
  2. Sector hit      — the article mentions the company's industry
  3. Theme hit       — the article matches known risk drivers for that
                       company (e.g. RELIANCE → oil prices, JIOFIN →
                       interest rates, NTPCGREEN → solar policy)
  4. Risk category   — the article matches a generic market risk that
                       affects all stocks (rate hike, market crash)

Scoring:
  direct_hit  + 5 points  (article names the company)
  sector_hit  + 3 points  (article names the sector)
  theme_hit   + 2 points  (article names a risk driver)
  risk_hit    + 1 point   (matches a generic risk category)
  total >= 4  →  alert the user

Delivery:
  Same Telegram channel as the daily news digest (NEWS_TELEGRAM_*).
  Runs every 30 minutes during market hours (9 AM - 4 PM IST), plus
  once at 8:55 AM IST for the daily digest.
"""
# TABLE OF CONTENTS (read this first)
#
# This file has 8 major sections (687 lines total):
#
# 1. Portfolio exposure map ----------
# 2. Risk-category-based impact ----------
# 3. Telegram alert rendering ----------
# 4. Persistence ----------
# 5. Telegram send (reuse from news_alert) ----------
# 6. Main run ----------
# 7. Scheduler ----------
# 8. CLI ----------

from __future__ import annotations

import json
import os
import re
import sys
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

try:
    from dotenv import load_dotenv
    _ENV_FILE = Path(__file__).resolve().parent.parent / ".env"
    if _ENV_FILE.exists():
        load_dotenv(_ENV_FILE, override=False)
except ImportError:
    pass

from .logging_setup import get_logger

log = get_logger("portfolio_impact")

from pipeline.runtime_paths import data_root

PROJECT = Path(__file__).resolve().parent.parent
DATA_DIR = data_root()

IMPACT_LOG_FILE = DATA_DIR / "alerts" / "portfolio_impact" / "log.json"
SEEN_IMPACT_FILE = DATA_DIR / "alerts" / "portfolio_impact" / "seen.json"

IST = timezone(timedelta(hours=5, minutes=30))
USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
              "AppleWebKit/537.36 PortfolioTracker/1.0")


# ---------- Portfolio exposure map ----------
# Hard-coded — the most reliable way to map tickers → sectors & risk
# drivers without depending on a flaky API. Updated whenever your
# portfolio changes.
# Each ticker gets:
#   name:       company display name
#   aliases:    extra name variants an article might use
#   sectors:    industry keywords (lower-case substring match)
#   themes:     specific risk drivers relevant to this company
PORTFOLIO_EXPOSURE: dict[str, dict] = {
    "ITC": {
        "name": "ITC Limited",
        "aliases": ["itc ltd", "itc limited", "i.t.c.", "india tobacco company"],
        "sectors": ["fmcg", "tobacco", "cigarette", "cigarettes", "hotel",
                    "hospitality", "ciggy", "fmcg sector"],
        "themes": ["sin tax", "tobacco tax", "excise duty", "tax on cigarettes",
                   "cigarette ban", "anti-tobacco", "fmcg demand", "rural demand",
                   "monsoon", "fmcg margins", "state taxes"],
    },
    "RELIANCE": {
        "name": "Reliance Industries",
        "aliases": ["reliance industries", "reliance jio", "reliance retail",
                    "jio platforms", "ambani", "mukesh ambani",
                    "reliance industries limited"],
        "sectors": ["oil refining", "oil refinery", "petrochemical", "petrochem",
                    "refining", "telecom", "wireless", "5g",
                    "oil & gas", "ongc", "energy sector"],
        "themes": ["crude oil price", "oil price", "opec cut", "opec",
                   "brent crude", "wti crude", "singapore complex", "GRM",
                   "gas pricing", "kg-d6", "jio arpu", "tariff hike",
                   "telecom tariff", "spectrum auction", "retail expansion",
                   "fdi in retail"],
    },
    "JIOFIN": {
        "name": "Jio Financial Services",
        "aliases": ["jio financial", "jiofinancial", "jio lending", "jio fin"],
        "sectors": ["nbfc", "non-banking", "lending", "consumer finance",
                    "financial services", "fintech", "loan"],
        "themes": ["loan growth", "nbfc crisis", "nbfc stress", "nbfc regulation",
                   "rbi policy", "consumer credit", "personal loan",
                   "digital lending", "lending app", "loan default",
                   "asset quality", "npa", "rbi rate", "repo rate",
                   "reverse repo", "monetary policy"],
    },
    "BANKBARODA": {
        "name": "Bank of Baroda",
        "aliases": ["bank of baroda", "baroda bank"],
        "sectors": ["psu bank", "public sector bank", "psb", "banking",
                    "public bank", "indian bank", "bank stock", "bank nifty"],
        "themes": ["psu bank merger", "psb merger", "psu disinvestment",
                   "rbi rate", "repo rate", "reverse repo", "npa cycle",
                   "asset quality", "msci inclusion", "fii flows",
                   "deposit growth", "credit growth", "cd ratio",
                   "psu bank privatization", "rbi policy", "rate hike",
                   "rate cut", "monetary policy"],
    },
    "NTPCGREEN": {
        "name": "NTPC Green Energy",
        "aliases": ["ntpc green", "ntpc renewable", "ngel", "ntpc green energy"],
        "sectors": ["renewable energy", "solar", "green energy", "wind energy",
                    "clean energy", "power sector", "renewable",
                    "renewables", "solar panel", "solar power", "renewable sector"],
        "themes": ["solar policy", "pm-surya ghar", "pm kusum", "solar tender",
                   "solar tariff", "module price", "aldb prices",
                   "renewable purchase obligation", "rpo", "esco",
                   "green hydrogen", "national green hydrogen mission",
                   "coal phase-down", "thermal power"],
    },
    "KNRCON": {
        "name": "KNR Constructions",
        "aliases": ["knr constructions", "knr const", "knr constructions limited"],
        "sectors": ["construction", "infrastructure", "highway", "roads",
                    "highways", "road construction", "infra sector", "epc"],
        "themes": ["nhai award", "nhai tender", "highway project",
                   "road project", "irb invitait", "ham project", "hybrid annuity",
                   "capex", "government capex", "infra spending", "gati shakti",
                   "pmgsy", "rural road", "irb", "irb invitat",
                   "asset divestment", "asset sale", "toll collection"],
    },
    "BALRAMCHIN": {
        "name": "Balrampur Chini Mills",
        "aliases": ["balrampur", "balrampur chini", "bcml", "balrampur chini mills"],
        "sectors": ["sugar", "sugar mill", "sugar sector", "ethanol",
                    "distillery", "agro", "sugarcane"],
        "themes": ["sugar export", "sugar quota", "msp sugarcane",
                   "fair and remunerative price", "frp", "sugar production",
                   "ethanol blending", "ebp", "ethanol procurement",
                   "sugar prices", "sugar inventory", "cane arrears",
                   "monsoon", "sugarcane acreage", "sugar mills",
                   "sugarcane harvest", "cane crushing", "sugar crushing",
                   "heavy rainfall", "rainfall"],
    },
    "UNOMINDA": {
        "name": "Uno Minda",
        "aliases": ["uno minda", "minda industries", "minda corp",
                    "uno minda limited", "spark minds"],
        "sectors": ["auto components", "auto ancillary", "auto electronics",
                    "ev components", "wiring harness", "automotive components",
                    "auto parts", "switchgear", "battery components"],
        "themes": ["maruti sales", "tata motors ev", "two-wheeler demand",
                   "automotive electronics", "ev demand", "ev penetration",
                   "auto component export", "auto demand", "auto sales",
                   "passenger vehicle", "two wheeler", "auto sector"],
    },
    "GOLDBEES": {
        "name": "Gold ETF (GOLDBEES)",
        "aliases": ["goldbees", "gold etf", "nippon gold"],
        "sectors": ["gold", "gold price", "precious metals", "bullion"],
        "themes": ["gold rate", "mcx gold", "spot gold", "gold futures",
                   "rupee gold", "gold import", "gold demand",
                   "safe haven"],
    },
    "METALIETF": {
        "name": "Metals ETF (METALIETF)",
        "aliases": ["metal etf", "metalbees", "nippon metal"],
        "sectors": ["metals", "metal index", "base metals", "copper",
                    "zinc", "aluminum", "lead", "nickel"],
        "themes": ["lme", "london metal exchange", "metal prices",
                   "copper price", "aluminum price", "zinc price",
                   "steel price", "iron ore"],
    },
    "NEXT50IETF": {
        "name": "Nifty Next 50 ETF (NEXT50IETF)",
        "aliases": ["next 50 etf", "next50 etf", "next50"],
        "sectors": ["nifty next 50", "next50 index", "nifty next"],
        "themes": ["nifty next 50 rebalance", "next50 reweight",
                   "nifty next 50 inclusion", "next50 exclusion"],
    },
}

# Reverse-lookup: any keyword (lower-case) → list of affected tickers
_KEYWORD_TO_TICKERS: dict[str, list[str]] = {}
for tkr, info in PORTFOLIO_EXPOSURE.items():
    keys = set()
    keys.add(tkr.lower())
    for alias in info["aliases"]:
        keys.add(alias.lower())
    for sec in info["sectors"]:
        keys.add(sec.lower())
    for th in info["themes"]:
        keys.add(th.lower())
    for k in keys:
        _KEYWORD_TO_TICKERS.setdefault(k, []).append(tkr)


def _keyword_regex(keyword: str) -> re.Pattern:
    """
    Compile a whole-word/whole-phrase matcher for a keyword.

    Using plain substring matching (the old approach) causes false
    positives like the ticker alias "bob" (Bank of Baroda) matching
    inside "Landbobank" (an unrelated Danish bank), or "rail" matching
    inside "retail" or "trailer". \\b word boundaries fix this: a match
    only counts if the keyword appears as a standalone word/phrase, not
    as a substring of some other word.

    Keywords in the source data sometimes carry deliberate leading/
    trailing spaces or punctuation (e.g. "rbi ", " fed ", "boe,") which
    were originally there to fake word-boundary behaviour under plain
    substring matching. We strip that decoration and rely on \\b instead.
    """
    cleaned = keyword.strip(" .,:")
    if not cleaned:
        cleaned = keyword.strip()
    escaped = re.escape(cleaned)
    return re.compile(rf"\b{escaped}\b", re.IGNORECASE)


_ALIAS_REGEX_CACHE: dict[str, re.Pattern] = {}


def _cached_regex(keyword: str) -> re.Pattern:
    pat = _ALIAS_REGEX_CACHE.get(keyword)
    if pat is None:
        pat = _keyword_regex(keyword)
        _ALIAS_REGEX_CACHE[keyword] = pat
    return pat


def _find_affected_tickers(title: str, description: str) -> dict[str, int]:
    """
    Return {ticker: score} for tickers affected by this article.

    Score is the sum of points from matches at different specificity levels:
      ticker/alias hit  → +5
      sector hit         → +3
      theme hit          → +2

    All matching uses whole-word/whole-phrase regex (see _keyword_regex)
    so that short aliases like "bob" or sector words like "rail" can't
    match as substrings inside unrelated words (e.g. "Landbobank",
    "retail", "trailer").

    Bare ticker (e.g. "RELIANCE") is matched CASE-SENSITIVELY so
    "self-reliance" / "energy reliance" / "strategic reliance" don't
    fire (those are English idioms, lowercase). Real news uses
    "Reliance" or "RELIANCE" (uppercase, often in titles).
    Aliases are matched case-insensitively because they're phrases
    like "Reliance Industries" that may be lowercased in body text.
    """
    text = f"{title} {description}"
    hits: dict[str, set[str]] = {}  # ticker → set of matched keys

    # Check direct ticker/alias hits first (most specific).
    for tkr, info in PORTFOLIO_EXPOSURE.items():
        # Bare ticker, case-sensitive (prevents "self-reliance" idiom)
        if len(tkr.strip()) >= 3:
            pat = re.compile(rf"\b{re.escape(tkr.strip())}\b")
            if pat.search(text):
                hits.setdefault(tkr, set()).add(f"direct:{tkr.lower()}")
        # Aliases, case-insensitive (need >= 4 chars to avoid the
        # "ril"/"bob"/"knr" substring-matches-inside-other-words class)
        for alias in info["aliases"]:
            if alias and len(alias.strip()) >= 4 and _cached_regex(alias).search(text):
                hits.setdefault(tkr, set()).add(f"direct:{alias.lower()}")

    # Sector hits
    for tkr, info in PORTFOLIO_EXPOSURE.items():
        for sec in info["sectors"]:
            if _cached_regex(sec).search(text):
                hits.setdefault(tkr, set()).add(f"sector:{sec.lower()}")

    # Theme hits
    for tkr, info in PORTFOLIO_EXPOSURE.items():
        for th in info["themes"]:
            if _cached_regex(th).search(text):
                hits.setdefault(tkr, set()).add(f"theme:{th.lower()}")

    # Convert sets → scores
    score_per_tier = {"direct:": 5, "sector:": 3, "theme:": 2}
    scores: dict[str, int] = {}
    for tkr, matched in hits.items():
        score = 0
        for m in matched:
            for prefix, pts in score_per_tier.items():
                if m.startswith(prefix):
                    score += pts
                    break
        scores[tkr] = score
    return scores


# ---------- Risk-category-based impact ----------
# NOTE: this module used to also fire a "market-wide" alert for every
# holding whenever an article matched a broad risk category (rate
# hikes, inflation, market crash, FX) even with ZERO direct/sector/
# theme keyword hits. Per user requirement, alerts must only be sent
# when the article actually names a held company, its sector, or one
# of its specific risk-driver themes — a generic macro category match
# alone is no longer sufficient. That fallback path has been removed;
# generic macro news is still covered by the separate news_alert.py
# daily digest, just not re-sent here as a portfolio-impact alert.


def _score_article_for_portfolio(
    article,  # news_alert.Article
) -> tuple[list[tuple[str, int, str]], bool]:
    """
    Score an article against the user's portfolio.

    Returns:
      (impacts, is_generic_only)
        impacts: [(ticker, score, reason), ...] for tickers affected.
        is_generic_only: always False now (kept for return-shape
                         compatibility with callers) — every alert
                         returned here comes from a real direct/
                         sector/theme keyword match, matched with
                         whole-word boundaries so short aliases like
                         "bob" can't match inside unrelated words like
                         "Landbobank".

    Score is the sum of points from matches at different specificity levels:
      ticker/alias hit  → +5
      sector hit         → +3
      theme hit          → +2
    An article must reach score >= 4 for a ticker to be included, i.e.
    at least one direct hit, or a sector + theme combination.
    """
    scores = _find_affected_tickers(article.title, article.description)
    impacts: list[tuple[str, int, str]] = []
    text = f"{article.title} {article.description}"
    for tkr, score in scores.items():
        if score >= 4:
            reasons = []
            for kw, affected_list in _KEYWORD_TO_TICKERS.items():
                if tkr in affected_list and _cached_regex(kw).search(text):
                    if len(reasons) < 3:
                        reasons.append(kw)
            reason = ", ".join(reasons[:3]) if reasons else "matches portfolio"
            impacts.append((tkr, score, reason))

    return impacts, False


# ---------- Telegram alert rendering ----------

CATEGORY_EMOJI = {
    "war":       ("⚔️ ", "War"),
    "fed_rates": ("🏦", "Interest Rate"),
    "interest_rate": ("🏦", "Interest Rate"),
    "pandemic":  ("🦠", "Pandemic"),
    "crisis":    ("📉", "Crisis"),
    "default_risk": ("💥", "Default"),
    "liquidity_risk": ("💧", "Liquidity"),
    "financial_risk": ("📑", "Financial"),
    "business_risk": ("🏢", "Business"),
    "management_risk": ("👔", "Management"),
    "geopolitical": ("⚔️ ", "Geopolitical"),
    "economic":  ("🌍", "Economic"),
    "market_risk": ("📊", "Market"),
    "purchasing_power": ("💸", "Inflation"),
    "exchange_rate": ("💱", "FX"),
    "disasters": ("🌋", "Disaster"),
    "natural_disaster": ("🌋", "Disaster"),
}


def _render_impact_alert(
    article,
    impacts: list[tuple[str, int, str]],
) -> str:
    """Build the Telegram message for a portfolio-impact alert.

    Each affected ticker gets its own sentiment analysis (bullish /
    bearish / neutral) with confidence and a one-line key reason.
    """
    from .sentiment import (
        analyze_sentiment_for_ticker, format_analysis_block,
    )
    cat = article.category or "economic"
    emoji, cat_label = CATEGORY_EMOJI.get(cat, ("📰", cat))
    title = article.title[:160]
    lines = []
    lines.append(f"🚨 *Portfolio Impact Alert* — {emoji} {cat_label} risk")
    lines.append(f"_Source: {article.source}_")
    lines.append("")
    lines.append(f"📰 {title}")
    if article.url:
        lines.append(f"   {article.url}")
    if article.description:
        desc = re.sub(r"<[^>]+>", "", article.description)
        desc = re.sub(r"\s+", " ", desc).strip()[:200]
        if desc:
            lines.append(f"   _{desc}_")
    lines.append("")
    lines.append("🎯 *Why this matters to your portfolio:*")
    # Sort by score desc, dedup
    seen_tkrs = set()
    sorted_impacts = sorted(impacts, key=lambda x: -x[1])
    for tkr, score, reason in sorted_impacts:
        if tkr in seen_tkrs:
            continue
        seen_tkrs.add(tkr)
        info = PORTFOLIO_EXPOSURE.get(tkr, {})
        name = info.get("name", tkr)
        # Sentiment analysis (per-ticker)
        sent = analyze_sentiment_for_ticker(article.title, article.description, tkr)
        emoji_s = {"bullish": "🟢", "bearish": "🔴", "neutral": "⚪"}.get(sent.direction, "⚪")
        lines.append(f"  • *{tkr}* ({name})")
        lines.append(f"    Match strength: {score} pts")
        lines.append(f"    Reason: {reason}")
        lines.append(f"    {emoji_s} *{sent.direction.upper()}* "
                     f"({sent.confidence} confidence, score {sent.net_score:+d})")
        lines.append(f"    Analysis: {sent.key_reason}")
    lines.append("")
    # Indian-context themes (monsoon, local politics, festivals, etc.)
    from .sentiment import detect_indian_themes, format_indian_theme_block
    theme_hits = detect_indian_themes(article.title, article.description)
    if theme_hits:
        lines.append(format_indian_theme_block(
            theme_hits,
            article_title=article.title,
            article_desc=article.description,
        ))
        lines.append("")
    lines.append("— Portfolio Tracker • portfolio_impact.py")
    return "\n".join(lines)


def _render_generic_alert(article, tickers: list[str]) -> str:
    """
    Build a single Telegram message for a generic market-wide risk
    (e.g. "Fed raises rates"). Lists all tickers affected instead of
    sending N separate alerts. Each ticker gets its own sentiment
    analysis so the user can see which holdings are most at risk.
    """
    from .sentiment import (
        analyze_sentiment_for_ticker, format_analysis_block,
    )
    cat = article.category or "economic"
    emoji, cat_label = CATEGORY_EMOJI.get(cat, ("📰", cat))
    title = article.title[:160]
    lines = []
    lines.append(f"🌐 *Market-wide Alert* — {emoji} {cat_label} risk")
    lines.append(f"_Source: {article.source}_")
    lines.append("")
    lines.append(f"📰 {title}")
    if article.url:
        lines.append(f"   {article.url}")
    if article.description:
        desc = re.sub(r"<[^>]+>", "", article.description)
        desc = re.sub(r"\s+", " ", desc).strip()[:200]
        if desc:
            lines.append(f"   _{desc}_")
    lines.append("")
    lines.append(f"📊 *Affects all {len(tickers)} of your holdings:*")
    # Per-ticker sentiment (compact one-liner per ticker)
    for tkr in tickers:
        sent = analyze_sentiment_for_ticker(article.title, article.description, tkr)
        emoji_s = {"bullish": "🟢", "bearish": "🔴", "neutral": "⚪"}.get(sent.direction, "⚪")
        lines.append(f"  • *{tkr}* {emoji_s} {sent.direction.upper()}"
                     f" ({sent.confidence}, score {sent.net_score:+d})"
                     f" — {sent.key_reason}")
    lines.append("")
    # Indian-context themes (always shown for generic alerts)
    from .sentiment import detect_indian_themes, format_indian_theme_block
    theme_hits = detect_indian_themes(article.title, article.description)
    if theme_hits:
        lines.append(format_indian_theme_block(
            theme_hits,
            article_title=article.title,
            article_desc=article.description,
        ))
        lines.append("")
    cat_pretty = cat.replace("_", " ")
    lines.append(f"_This is a market-wide {cat_pretty} move; review your"
                 " sector exposure for second-order effects._")
    lines.append("")
    lines.append("— Portfolio Tracker • portfolio_impact.py")
    return "\n".join(lines)


# ---------- Persistence ----------

def _load_impact_seen() -> dict[str, str]:
    """URL → first_seen_iso. Auto-expire after 7 days."""
    if not SEEN_IMPACT_FILE.exists():
        return {}
    try:
        data = json.loads(SEEN_IMPACT_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return {}
    cutoff = (datetime.now() - timedelta(days=7)).isoformat()
    return {u: ts for u, ts in data.items() if ts > cutoff}


def _save_impact_seen(seen: dict[str, str]) -> None:
    SEEN_IMPACT_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = SEEN_IMPACT_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(seen, indent=2))
    tmp.replace(SEEN_IMPACT_FILE)


def _append_log(entry: dict) -> None:
    log_list: list[dict] = []
    if IMPACT_LOG_FILE.exists():
        try:
            log_list = json.loads(IMPACT_LOG_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            log_list = []
    log_list.append(entry)
    log_list = log_list[-50:]
    IMPACT_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = IMPACT_LOG_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(log_list, indent=2, default=str))
    tmp.replace(IMPACT_LOG_FILE)


# ---------- Telegram send (reuse from news_alert) ----------

def send_telegram(text: str) -> dict:
    """Send via Telegram Bot API. Reuses credentials from news_alert."""
    from .news_alert import _env, is_dry_run as nd_dry_run, send_telegram as na_send
    bot_token = _env("NEWS_TELEGRAM_BOT_TOKEN")
    chat_id = _env("NEWS_TELEGRAM_CHAT_ID")
    dry = nd_dry_run() or not (bot_token and chat_id)
    if dry:
        log.info("[dry-run] would send impact alert (%d chars) to chat=%s",
                 len(text), chat_id)
        for ln in text.splitlines()[:25]:
            log.info("  %s", ln)
        return {"sent": False, "mode": "dry_run", "chat_id": chat_id,
                "message_length": len(text)}
    return na_send(text)


# ---------- Main run ----------

def scan_once(send: bool = True, min_score: int = 4) -> dict:
    """
    Fetch news articles, score each against the portfolio, and send
    Telegram alerts for any article that affects a held stock with
    score >= min_score.

    Returns a status dict.
    """
    from .news_alert import (
        fetch_articles, _filter_fresh, _load_seen, _save_seen, IST,
    )
    ran_at = datetime.now(IST).isoformat(timespec="seconds")
    alerts_sent = 0
    tickers_alerted: set[str] = set()
    errors: list[str] = []

    try:
        all_articles = fetch_articles()
    except Exception as e:
        log.exception("fetch failed")
        return {"ran_at": ran_at, "fetch_ok": False,
                "alerts_sent": 0, "errors": [f"fetch failed: {e}"]}

    fresh = _filter_fresh(all_articles)
    seen = _load_impact_seen()
    seen_added = []

    for article in fresh:
        if not article.url or article.url in seen:
            continue
        impacts, is_generic_only = _score_article_for_portfolio(article)
        if not impacts:
            continue
        # If this is a generic-only alert (e.g. "rates up, affects everyone"),
        # we batch it into a single consolidated message per article
        # so we don't spam 8 separate alerts for one news event.
        if is_generic_only:
            tickers_in_msg = [t for t, _, _ in impacts]
            msg = _render_generic_alert(article, tickers_in_msg)
        else:
            msg = _render_impact_alert(article, impacts)
        if send:
            result = send_telegram(msg)
            if result.get("sent"):
                alerts_sent += 1
                tickers_alerted.update(t for t, _, _ in impacts)
                seen_added.append(article.url)
                log.info("ALERT: %s affects %s%s",
                         article.title[:60],
                         ", ".join(sorted({t for t, _, _ in impacts})),
                         " [generic]" if is_generic_only else "")
            else:
                log.warning("telegram send failed for %s: %s",
                            article.url, result.get("error"))
        # Always log to file even if not sent (so we have a record)
        # Compute sentiment + Indian themes for each affected ticker for the log
        from .sentiment import analyze_sentiment_for_ticker, detect_indian_themes
        sentiment_log = {}
        for t, _, _ in impacts:
            s = analyze_sentiment_for_ticker(
                article.title, article.description, t
            )
            sentiment_log[t] = s.to_dict()
        theme_hits_log = detect_indian_themes(article.title, article.description)
        themes_log = [
            {
                "theme": th.theme,
                "name": th.theme_name,
                "signals": th.signals_matched,
                "affected_tickers": th.affected_tickers,
            }
            for th in theme_hits_log
        ]
        _append_log({
            "ran_at": ran_at,
            "url": article.url,
            "title": article.title,
            "category": article.category,
            "impacts": [{"ticker": t, "score": s, "reason": r}
                        for t, s, r in impacts],
            "sentiment": sentiment_log,
            "indian_themes": themes_log,
            "is_generic_only": is_generic_only,
            "telegram_sent": send,
        })

    # Update seen cache
    now_iso = datetime.now().isoformat()
    for url in seen_added:
        seen[url] = now_iso
    _save_impact_seen(seen)

    result = {
        "ran_at": ran_at, "fetch_ok": True,
        "articles_scanned": len(fresh),
        "alerts_sent": alerts_sent,
        "tickers_alerted": sorted(tickers_alerted),
        "errors": errors,
    }
    log.info("impact scan: %d alerts sent for %d tickers",
             alerts_sent, len(tickers_alerted))
    return result


# ---------- Scheduler ----------

_scheduler_started = False
_scheduler_lock = threading.Lock()


def _next_scan_time(minutes_interval: int = 30,
                    only_market_hours: bool = True) -> datetime:
    """
    Next scan time. If only_market_hours, returns the next time during
    Indian market hours (9:00 - 15:30 IST, Mon-Fri).
    """
    now_ist = datetime.now(IST)
    if only_market_hours and now_ist.weekday() < 5:
        market_open = now_ist.replace(hour=9, minute=0, second=0, microsecond=0)
        market_close = now_ist.replace(hour=15, minute=30, second=0, microsecond=0)
        if market_open <= now_ist <= market_close:
            # Mid-market — schedule next slot
            next_slot = now_ist + timedelta(minutes=minutes_interval)
            # If next slot is past market close, jump to tomorrow's open
            if next_slot > market_close:
                next_slot = market_close
        elif now_ist < market_open:
            next_slot = market_open
        else:
            # Past close — jump to tomorrow's open
            tomorrow = (now_ist + timedelta(days=1)).replace(
                hour=9, minute=0, second=0, microsecond=0)
            next_slot = tomorrow
    else:
        next_slot = now_ist + timedelta(minutes=minutes_interval)
    return next_slot.astimezone(timezone.utc).replace(tzinfo=None)


def _scan_loop(stop_event: threading.Event) -> None:
    """Background thread that scans for portfolio-impact news every
    minutes_interval during market hours."""
    while not stop_event.is_set():
        next_run = _next_scan_time()
        now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
        wait_secs = (next_run - now_utc).total_seconds()
        log.info("portfolio_impact scan: next run at %s IST (in %.0fs)",
                 next_run.strftime("%Y-%m-%d %H:%M:%S"), wait_secs)
        while wait_secs > 0 and not stop_event.is_set():
            chunk = min(60, wait_secs)
            stop_event.wait(chunk)
            wait_secs -= chunk
        if stop_event.is_set():
            break
        try:
            scan_once()
        except Exception as e:
            log.exception("scheduled impact scan failed: %s", e)


def start_daily_scheduler(interval_minutes: int = 30) -> threading.Event:
    """Start the background scanner."""
    global _scheduler_started
    with _scheduler_lock:
        if _scheduler_started:
            log.info("portfolio_impact scheduler already running")
            return threading.Event()
        _scheduler_started = True
        stop_event = threading.Event()
        t = threading.Thread(target=_scan_loop, args=(stop_event,),
                             daemon=True, name="portfolio-impact")
        t.start()
        log.info("portfolio_impact scheduler started (every %d min)",
                 interval_minutes)
        return stop_event


# ---------- CLI ----------

def _cli():
    import argparse
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--dry-run", action="store_true",
                   help="Print alerts instead of sending.")
    p.add_argument("--start-scheduler", action="store_true",
                   help="Run scheduler in foreground (Ctrl-C to stop).")
    p.add_argument("--interval", type=int, default=30,
                   help="Scan interval in minutes (default 30).")
    args = p.parse_args()
    if args.dry_run:
        os.environ["NEWS_DRY_RUN"] = "1"
    if args.start_scheduler:
        stop = start_daily_scheduler(args.interval)
        try:
            while not stop.wait(60):
                pass
        except KeyboardInterrupt:
            stop.set()
        return
    result = scan_once(send=not args.dry_run)
    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    _cli()