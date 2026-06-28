"""
sentiment.py
-----------
Bullish / Bearish / Neutral sentiment analysis for the Portfolio Impact
Scanner. Pure-Python, deterministic, no LLM.

Strategy
========
Each (ticker, risk_keyword) pair has a known sentiment direction. The
classifier:

  1. Detects risk-keyword signals in the article text + description
  2. Looks up each signal in a per-ticker polarity table:
       "+"  = bullish for that ticker
       "-"  = bearish for that ticker
       "."  = neutral / mixed (signal present but no clear direction)
  3. Aggregates the polarities:
       net_score = bullish_count - bearish_count
       direction = "bullish" if net >= 2
                 = "bearish" if net <= -2
                 = "neutral"  otherwise
  4. Picks the top signal as the "key reason" for the analysis

This is a hand-built rules engine, not an LLM — it's explainable,
deterministic, and works offline. We accept that it won't catch
every nuance; the goal is to give the user a reasonable first-pass
view that they can sanity-check against the headline.

We bias toward caution: a single "slightly bearish" signal + one
"neutral" signal => NEUTRAL, not bearish. The user is in charge of
decisions, not us.
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from typing import Optional

# Per-(ticker, risk_keyword) polarity table. When the article text
# contains one of these keywords, it carries that polarity for the
# ticker. Polarity values:
#   "+"  = bullish for this ticker
#   "-"  = bearish for this ticker
#   "."  = neutral / context-dependent (flagged but doesn't count
#          toward net direction)
#
# This table is intentionally conservative. If a signal isn't here
# for a particular ticker, the classifier returns "neutral" for that
# signal.
#
# Format: TICKER -> { keyword_substring: polarity, ... }
POLARITY = {
    "ITC": {
        # Rate decisions: FMCG demand is rate-sensitive
        "rate cut": "+", "rate cuts": "+", "interest rate cut": "+",
        "lower rates": "+", "rate reduction": "+",
        "rate hike": "-", "rate hikes": "-", "interest rate hike": "-",
        "higher rates": "-", "tightening": "-",
        "hikes rates": "-", "cuts rates": "+",
        # Tobacco / sin tax
        "excise duty": "-", "sin tax": "-", "tobacco tax": "-",
        "cigarette ban": "-", "anti-tobacco": "-",
        "tax cut": "+", "tax cuts": "+", "tax reduction": "+",
        "tax holiday": "+",
        # Inflation
        "inflation": "-", "inflation surge": "-", "cpi": "-",
        "deflation": "+",
        # FMCG demand
        "rural demand": "+", "rural consumption": "+",
        "fmcg demand": "+", "fmcg growth": "+",
        "fmcg slowdown": "-", "weak demand": "-",
        "monsoon": "+", "good monsoon": "+", "normal monsoon": "+",
        "poor monsoon": "-", "deficient monsoon": "-",
        # Currency
        "rupee depreciation": "-", "rupee weakness": "-",
        "rupee strengthens": "+", "rupee appreciation": "+",
        # Markets
        "market crash": "-", "market sell-off": "-", "bear market": "-",
        "market rally": "+", "bull market": "+",
    },
    "RELIANCE": {
        # Oil prices: bigger beneficiary
        "crude oil price": "+", "crude oil prices": "+",
        "oil price": "+", "oil prices": "+",
        "brent crude": "+", "wti crude": "+", "opec cut": "+",
        "opec production cut": "+", "opec": "+",
        "oil glut": "-", "oil oversupply": "-",
        "opec increase": "-", "opec production increase": "-",
        # Refining margins (GRM)
        "singapore complex": "+", "GRM": "+", "refining margin": "+",
        "crack spread": "+",
        # Telecom (Jio)
        "tariff hike": "+", "telecom tariff": "+", "tariff increase": "+",
        "ARPU": "+", "jio arpu": "+",
        "tariff cut": "-", "tariff war": "-", "price war": "-",
        "spectrum auction": "+",  # capex spend but long-term ARPU
        # Retail
        "retail growth": "+", "consumer spending": "+",
        "retail slowdown": "-", "weak consumer": "-",
        # Rates
        "rate cut": "+", "rate cuts": "+",
        "rate hike": "-", "rate hikes": "-",
        # Markets
        "market crash": "-", "market sell-off": "-",
        "market rally": "+",
        # Currency
        "rupee depreciation": "-", "rupee weakness": "-",
        "rupee strengthens": "+",
    },
    "JIOFIN": {
        # NBFC: rate cuts are GOOD (lower cost of funds), rate hikes BAD
        "rate cut": "+", "rate cuts": "+", "lower rates": "+",
        "rate reduction": "+", "repo rate cut": "+", "repo cut": "+",
        "rate hike": "-", "rate hikes": "-", "tightening": "-",
        "repo rate hike": "-", "repo hike": "-",
        "hikes rates": "-", "cuts rates": "+",
        "interest rate hike": "-", "interest rate cut": "+",
        # Credit growth
        "loan growth": "+", "credit growth": "+", "disbursement": "+",
        "lending growth": "+", "AUM growth": "+",
        "loan default": "-", "NPA": "-", "asset quality": "-",
        "credit slowdown": "-", "loan slowdown": "-",
        # RBI regulation
        "rbi norm": ".", "rbi rule": ".",
        "rbi restriction": "-", "rbi ban": "-", "rbi penalty": "-",
        # Markets
        "market crash": "-", "market sell-off": "-",
        "market rally": "+", "bull market": "+",
        # Currency / inflation
        "rupee depreciation": "-",
        "inflation surge": "-",
    },
    "BANKBARODA": {
        # PSU bank: rate hikes GOOD for NIM (with a lag), rate cuts BAD
        "rate hike": "+", "rate hikes": "+", "rate increase": "+",
        "tightening": "+", "hawkish": "+",
        "rate cut": "-", "rate cuts": "-", "rate reduction": "-",
        "easing": "-", "dovish": "-", "lower rates": "-",
        "repo rate hike": "-", "repo rate cut": "+",
        "hikes rate": "+", "cuts rate": "-",
        "raises rate": "+", "raises rates": "+", "lowered rate": "-",
        "hiked rate": "+", "hiked rates": "+",
        "raised rate": "+", "raised rates": "+",
        # The single word "rate" is too noisy (matches everything),
        # so we don't include it. Instead, we include the common
        # natural-language variants like "rate hike", "rate cut",
        # "interest rate", "repo rate".
        "interest rate hike": "+", "interest rate cut": "-",
        "interest rate": ".",  # direction depends on context
        "hikes rates": "+", "cuts rates": "-",
        "policy rate": ".",  # neutral on its own
        "rate decision": ".",  # neutral on its own
        # Credit / asset quality
        "credit growth": "+", "loan growth": "+", "deposit growth": "+",
        "NPA decline": "+", "asset quality improvement": "+",
        "NPA rise": "-", "asset quality": "-", "NPA": "-",
        "credit slowdown": "-",
        # PSU-specific
        "recapitalisation": "+", "capital infusion": "+",
        "psu disinvestment": "-", "privatisation": "-",
        "psu bank merger": ".",  # neutral — depends on role
        # FII flows
        "FII inflow": "+", "FII buying": "+", "FII stake": "+",
        "FII outflow": "-", "FII selling": "-",
        # Markets
        "market crash": "-", "market sell-off": "-",
        "market rally": "+", "bull market": "+",
        # Currency
        "rupee depreciation": "-", "rupee weakness": "-",
    },
    "NTPCGREEN": {
        # Renewable energy: rate cuts GOOD (lower cost of capital),
        # rate hikes BAD
        "rate cut": "+", "rate cuts": "+", "lower rates": "+",
        "rate hike": "-", "rate hikes": "-", "tightening": "-",
        "hikes rate": "-", "cuts rate": "+",
        "hikes rates": "-", "cuts rates": "+",
        "interest rate hike": "-", "interest rate cut": "+",
        # Solar/renewable policy
        "solar policy": "+", "solar scheme": "+", "solar subsidy": "+",
        "PM Surya Ghar": "+", "PM-KUSUM": "+",
        "module price decline": "+", "module price drop": "+",
        "module prices drop": "+", "module prices fall": "+",
        "module price falls": "+", "solar tariff hike": "+",
        "solar tariff": ".",  # depends: low = competitive, high = revenue
        "RPO": "+", "renewable purchase obligation": "+",
        "green hydrogen": "+", "national hydrogen mission": "+",
        # Fossil fuel competition
        "coal price": "-", "thermal power": "-", "coal phase-down": "+",
        # Oil/gas prices (substitutes for renewables when high)
        "oil price": "+", "crude oil price": "+",
        # Currency
        "rupee depreciation": "-",  # imported modules get costlier
        # Carbon
        "carbon tax": "+", "carbon credit": "+",
        "climate policy": "+", "net zero": "+",
        # Markets
        "market crash": "-", "market sell-off": "-",
        "market rally": "+", "green energy boom": "+",
    },
    "KNRCON": {
        # Infra/road: rate cuts GOOD, rate hikes BAD (capex cost)
        "rate cut": "+", "rate cuts": "+", "lower rates": "+",
        "rate hike": "-", "rate hikes": "-", "tightening": "-",
        "hikes rate": "+", "cuts rate": "-",
        "hikes rates": "-", "cuts rates": "+",
        "interest rate hike": "-", "interest rate cut": "+",
        # Government capex — use specific phrasings, not just "government"
        # (otherwise "Government announces election" would trigger this)
        "nhai award": "+", "nhai tender": "+",
        "highway project award": "+", "highway tender": "+",
        "highway capex": "+", "road project award": "+",
        "capex push": "+",
        "infra capex": "+", "infrastructure capex": "+",
        "gati shakti": "+",
        "budget cut": "-", "capex cut": "-", "highway project cut": "-",
        "capex reduced": "-", "capex decline": "-", "capex slashed": "-",
        # Steel/cement (input costs)
        "steel price": "-", "steel price rise": "-", "cement price": "-",
        "steel price fall": "+", "cement price fall": "+",
        # Markets
        "market crash": "-", "market sell-off": "-",
        "market rally": "+", "bull market": "+",
    },
    "IRCON": {
        # Railway: capex is the main driver
        "railway capex": "+", "rail capex": "+",
        "railway order": "+", "railway tender": "+",
        "railway board": ".",  # neutral
        "railway project": "+", "vande bharat": "+", "bullet train": "+",
        "high-speed rail": "+", "dedicated freight corridor": "+",
        "dfc": "+", "metro project": "+", "rail electrification": "+",
        "railway budget": "+", "railway ministry": "+",
        "railway minister": ".",
        # Rate cuts help (cheaper borrowing)
        "rate cut": "+", "rate cuts": "+", "lower rates": "+",
        "rate hike": "-", "rate hikes": "-", "tightening": "-",
        "hikes rates": "-", "cuts rates": "+",
        "interest rate hike": "-", "interest rate cut": "+",
        # Steel prices (input cost)
        "steel price": "-", "steel price rise": "-",
        "steel price fall": "+",
        # Budget
        "union budget": ".",  # depends on allocation
        "budget cut": "-", "capex cut": "-",
        "capex push": "+",
        # Markets
        "market crash": "-", "market sell-off": "-",
        "market rally": "+",
    },
    "BALRAMCHIN": {
        # Sugar/ethanol
        "sugar export": "+", "sugar quota": "+", "sugar subsidy": "+",
        "sugar price rise": "+", "sugar prices rise": "+",
        "sugar price fall": "-", "sugar prices fall": "-",
        "sugar glut": "-", "sugar oversupply": "-",
        "ethanol blending": "+", "ethanol policy": "+", "EBP": "+",
        "ethanol procurement": "+",
        "ethanol price rise": "+",
        # Cane
        "cane arrears": "-", "cane price rise": "+", "cane shortage": "-",
        "FRP hike": "+", "MSP cane": "+",
        "fair and remunerative price": "+",
        # Monsoon (sugarcane is rain-fed)
        "good monsoon": "+", "normal monsoon": "+",
        "poor monsoon": "-", "deficient monsoon": "-",
        "drought": "-",
        # Currency (sugar is partly exported)
        "rupee depreciation": "+",  # exports become more valuable
        "rupee strengthens": "-",  # exports less competitive
        # Rate cuts boost consumption
        "rate cut": "+", "rate cuts": "+", "lower rates": "+",
        "rate hike": "-", "rate hikes": "-",
        "hikes rates": "-", "cuts rates": "+",
        "interest rate hike": "-", "interest rate cut": "+",
        # Markets
        "market crash": "-", "market sell-off": "-",
        "market rally": "+",
    },
}


# Confidence threshold: net score must be >= this to call a direction
# (anything weaker = neutral). Default 1 (a single strong signal is
# enough to call direction).
DIRECTION_THRESHOLD = 1


@dataclass
class SentimentResult:
    direction: str          # "bullish" | "bearish" | "neutral"
    confidence: str         # "low" | "medium" | "high"
    net_score: int          # bullish_signals - bearish_signals
    bullish_signals: list[str] = None
    bearish_signals: list[str] = None
    key_reason: str = ""    # one-line summary

    def to_dict(self) -> dict:
        return {
            "direction": self.direction,
            "confidence": self.confidence,
            "net_score": self.net_score,
            "bullish_signals": self.bullish_signals or [],
            "bearish_signals": self.bearish_signals or [],
            "key_reason": self.key_reason,
        }


def _extract_signal_phrases(text: str) -> list[str]:
    """
    Extract candidate signal phrases from the text. We split into
    n-grams (1-3 words) and lowercase for matching.

    We keep all tokens (no length filter) because bigrams like "rate cut"
    need the short word "rate" to be present in the bag of tokens.
    """
    text = text.lower()
    # Tokenize: keep letters, digits, spaces
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    tokens = text.split()
    # Filter out very short noise (single chars) but keep "rate", "fed", etc.
    tokens = [t for t in tokens if len(t) >= 3]
    phrases = set()
    # Unigrams
    for t in tokens:
        phrases.add(t)
    # Bigrams and trigrams
    for n in (2, 3):
        for i in range(len(tokens) - n + 1):
            phrase = " ".join(tokens[i:i + n])
            phrases.add(phrase)
    return list(phrases)


def analyze_sentiment_for_ticker(
    title: str,
    description: str,
    ticker: str,
) -> SentimentResult:
    """
    Classify the article's sentiment for one ticker.

    Returns SentimentResult with direction, confidence, and the list of
    bullish/bearish signals that drove the classification.
    """
    table = POLARITY.get(ticker, {})
    if not table:
        return SentimentResult(
            direction="neutral", confidence="low", net_score=0,
            key_reason="no sentiment data for this ticker",
        )

    # We match signals in TWO ways, ordered by precision:
    #
    # 1. ADJACENT PHRASE MATCH: signal words appear in the text in the
    #    same order (allowing up to 2 words between them). This is the
    #    most reliable — "highway capex" matches "highway capex cut" but
    #    NOT "NHAI capex reduced for highway projects" (where highway
    #    and capex aren't adjacent).
    #
    # 2. BAG-OF-WORDS MATCH: all signal words appear anywhere in the
    #    text (fallback for paraphrases like "the rate was hiked" which
    #    doesn't have "rate hike" as adjacent words but should still
    #    match the "rate hike" signal).
    text_lower = (title + " " + description).lower()
    text_clean = re.sub(r"[^a-z0-9\s]", " ", text_lower)
    text_clean = re.sub(r"\s+", " ", text_clean).strip()
    text_words_list = text_clean.split()
    text_words = set(text_words_list)

    def _adjacent_match(signal: str) -> bool:
        sig_words = [w for w in re.findall(r"[a-z]+", signal) if w]
        if not sig_words or not text_words_list:
            return False
        if len(sig_words) == 1:
            if len(sig_words[0]) < 4:
                return False
            return sig_words[0] in text_words
        # For multi-word signals: find each word in the text (in order)
        # with up to 3 words of slack between them
        positions = []
        last_pos = -1
        n = len(text_words_list)
        for sw in sig_words:
            found = False
            # Start search after last_pos; clamp to >= 0 to avoid
            # Python's negative-index wrap-around behaviour
            for i in range(max(last_pos + 1, 0), n):
                if text_words_list[i] == sw:
                    positions.append(i)
                    last_pos = i
                    found = True
                    break
            if not found:
                return False
        # Require all words to be within 3 positions of each other
        for i in range(1, len(positions)):
            if positions[i] - positions[i-1] > 3:
                return False
        return True

    def _bag_match(signal: str) -> bool:
        sig_words = [w for w in re.findall(r"[a-z]+", signal) if w]
        if not sig_words:
            return False
        if len(sig_words) == 1 and len(sig_words[0]) < 4:
            return False
        return all(w in text_words for w in sig_words)

    bullish: list[str] = []
    bearish: list[str] = []

    for signal, polarity in table.items():
        # Prefer the more-precise adjacent match
        if _adjacent_match(signal) or _bag_match(signal):
            if polarity == "+":
                bullish.append(signal)
            elif polarity == "-":
                bearish.append(signal)
            # "." signals are noted but ignored for direction

    # Deduplicate (in case "rate hike" and "rate hikes" both match)
    bullish = sorted(set(bullish))
    bearish = sorted(set(bearish))

    net = len(bullish) - len(bearish)

    if net >= DIRECTION_THRESHOLD:
        direction = "bullish"
    elif net <= -DIRECTION_THRESHOLD:
        direction = "bearish"
    else:
        direction = "neutral"

    # Confidence based on |net| and total signal count
    total = len(bullish) + len(bearish)
    if abs(net) >= 4 and total >= 4:
        confidence = "high"
    elif abs(net) >= 2 and total >= 2:
        confidence = "medium"
    else:
        confidence = "low"

    # Key reason: top signal + a one-line interpretation
    key_reason = ""
    if direction == "bullish" and bullish:
        key_reason = f"positive: {', '.join(bullish[:3])}"
    elif direction == "bearish" and bearish:
        key_reason = f"negative: {', '.join(bearish[:3])}"
    elif bullish and bearish:
        key_reason = f"mixed: +{','.join(bullish[:2])} / -{','.join(bearish[:2])}"
    else:
        key_reason = "no clear directional signal"

    return SentimentResult(
        direction=direction,
        confidence=confidence,
        net_score=net,
        bullish_signals=bullish,
        bearish_signals=bearish,
        key_reason=key_reason,
    )


# ---------- Display helpers ----------

DIRECTION_EMOJI = {
    "bullish": "🟢",
    "bearish": "🔴",
    "neutral": "⚪",
}

DIRECTION_LABEL = {
    "bullish": "BULLISH",
    "bearish": "BEARISH",
    "neutral": "NEUTRAL",
}


def format_sentiment_line(sent: SentimentResult) -> str:
    """Build the one-line sentiment display for the alert message."""
    emoji = DIRECTION_EMOJI.get(sent.direction, "⚪")
    label = DIRECTION_LABEL.get(sent.direction, "NEUTRAL")
    conf = sent.confidence.upper()
    return f"{emoji} *{label}* ({conf} confidence, score {sent.net_score:+d})"


def format_analysis_block(sent: SentimentResult, ticker: str) -> str:
    """Build the multi-line analysis block shown in the alert message."""
    lines = []
    lines.append(format_sentiment_line(sent))
    lines.append(f"   Analysis: {sent.key_reason}")
    return "\n".join(lines)


# ============================================================
# Indian-context themes (monsoon, local politics, festivals,
# regulatory). Run BEFORE the per-ticker classifier so we can
# surface context like "monsoon onset impacts FMCG & cement"
# in the alert message.
# ============================================================

# A signal is a keyword/phrase that maps to one of these themes.
# Each theme has a list of tickers it impacts and the direction
# (mostly neutral on theme alone; the per-ticker table handles
# ticker-specific direction).
INDIAN_THEMES: dict[str, dict] = {
    "monsoon": {
        "name": "Monsoon / Rainfall",
        "description": "India's southwest monsoon (Jun-Sep) drives rural "
                       "demand, kharif sowing, and reservoir levels. "
                       "Deficient monsoons hurt FMCG, cement, and two-wheelers; "
                       "above-normal monsoons boost them.",
        "signals": [
            "monsoon", "rainfall", "deficient monsoon", "below normal monsoon",
            "above normal monsoon", "excess rainfall", "drought",
            "kharif sowing", "kharif season", "reservoir level",
            "imd", "met department", "weather department",
            "monsoon forecast", "monsoon onset", "monsoon withdrawal",
        ],
        "affected_tickers": ["ITC", "BALRAMCHIN", "KNRCON"],
        "context": "Monsoon onset/progress impacts rural demand, agri output, "
                   "and construction activity",
    },
    "local_politics": {
        "name": "State / Local Politics",
        "description": "State elections, coalition changes, and state-level "
                       "policy affect mining, alcohol/tobacco taxation, "
                       "real estate approvals, and PSU contracts.",
        "signals": [
            "state election", "state government", "coalition government",
            "state budget", "state cabinet", "chief minister",
            "assembly election", "lok sabha election", "general election",
            "by-election", "byelection", "by poll", "bypoll",
            "political instability", "no-confidence motion", "trust vote",
            "rajya sabha", "lok sabha", "bill passed", "ordinance",
            "president rule", "governor's rule",
            "delhi", "mumbai", "karnataka", "tamil nadu", "west bengal",
            "uttar pradesh", "maharashtra", "gujarat", "rajasthan",
            "andhra pradesh", "telangana", "kerala", "punjab",
        ],
        "affected_tickers": ["BANKBARODA", "KNRCON", "IRCON", "ITC"],
        "context": "State-level political changes can affect PSUs, "
                   "infrastructure, and FMCG (state taxes)",
    },
    "festival_wedding": {
        "name": "Festivals & Wedding Season",
        "description": "Diwali, Dhanteras, Akshaya Tritiya, and the "
                       "Oct-Jan wedding season drive gold, jewellery, "
                       "consumer durables, and discretionary spending.",
        "signals": [
            "diwali", "dhanteras", "akshaya tritiya", "karva chauth",
            "navratri", "dussehra", "holi", "ganesh chaturthi",
            "wedding season", "marriage season", "festive season",
            "festive demand", "festive sales", "festive buying",
            "wedding demand", "wedding jewellery",
        ],
        "affected_tickers": ["ITC"],
        "context": "Festive/wedding seasons boost consumer spending "
                   "(FMCG, gold, durables) and rural income",
    },
    "rbi_policy": {
        "name": "RBI / SEBI / Government Policy",
        "description": "RBI rate decisions, SEBI rules, FII/DII flows, "
                       "and GST council decisions move all stocks.",
        "signals": [
            "rbi policy", "rbi governor", "rbi rate", "repo rate",
            "reverse repo", "msf rate", "crr", "slr", "monetary policy committee",
            "fpi flows", "fii flows", "dii flows",
            "sebi", "sebi board", "sebi chairman", "sebi circular",
            "gst council", "gst rate", "gst hike", "gst cut",
            "budget 2024", "budget 2025", "budget 2026", "union budget",
            "finance bill", "finance minister", "nirmala sitharaman",
            "policy stance", "monetary policy", "laf", "msf",
        ],
        "affected_tickers": ["BANKBARODA", "JIOFIN", "RELIANCE", "ITC"],
        "context": "RBI policy + FII flows + GST = broad market impact",
    },
    "commodity_prices": {
        "name": "Commodity Prices",
        "description": "Brent crude, gold, silver, copper, steel, cement, "
                       "and coal prices directly impact input costs or "
                       "realisations of specific stocks.",
        "signals": [
            "crude oil", "brent", "wti", "opec",
            "gold price", "silver price", "bullion",
            "copper price", "aluminium price", "zinc price",
            "steel price", "iron ore", "coking coal", "thermal coal",
            "cement price", " clinker",
            "natural gas", "lng", "lng price", "gas price",
            "palm oil", "soybean oil", "edible oil",
            "sugar price", "wheat price", "rice price", "wheat", "rice",
        ],
        "affected_tickers": ["RELIANCE", "BALRAMCHIN", "KNRCON"],
        "context": "Commodity prices feed into input costs or top-line revenue",
    },
    "global_supply_chain": {
        "name": "Global Supply Chain / Trade",
        "description": "Container rates, China slowdown, semiconductor "
                       "shortages, and global trade tensions affect "
                       "export-oriented sectors.",
        "signals": [
            "container rate", "shipping rate", "freight rate", "container shortage",
            "china slowdown", "china demand", "china export",
            "semiconductor", "chip shortage", "chip supply",
            "taiwan", "south korea export", "global trade",
            "trade war", "tariff hike", "tariff cut", "import duty",
            "export ban", "export duty", "anti-dumping",
            "red sea", "suez canal", "shipping disruption",
        ],
        "affected_tickers": ["RELIANCE", "KNRCON", "ITC", "JIOFIN",
                              "BANKBARODA", "NTPCGREEN", "IRCON", "BALRAMCHIN"],
        "context": "Supply chain shocks hit input costs and export demand",
    },
    "gdp_iip_print": {
        "name": "GDP / IIP / Macro Data Print",
        "description": "India's quarterly GDP, monthly IIP (Index of "
                       "Industrial Production), and PMI prints drive "
                       "broad-market sentiment and stock-specific "
                       "expectations on volume growth.",
        "signals": [
            "gdp print", "gdp growth", "gdp data", "gdp number", "gdp number",
            "gdp estimate", "iip print", "iip data", "iip growth",
            "industrial production", "manufacturing pmi",
            "services pmi", "composite pmi", "pmi print", "pmi data",
            "core sector", "core industries", "infrastructure output",
            "factory output", "manufacturing growth",
            "gdp forecast", "growth estimate", "growth forecast",
            "rbi growth", "imf growth", "world bank growth",
        ],
        "affected_tickers": ["ITC", "RELIANCE", "JIOFIN", "BANKBARODA",
                              "NTPCGREEN", "KNRCON", "IRCON", "BALRAMCHIN"],
        "context": "Strong GDP/IIP prints lift broad-market multiples; "
                   "weak prints compress them",
    },
    "fii_dii_flows": {
        "name": "FII / DII Flows",
        "description": "Foreign Institutional Investor (FII) and "
                       "Domestic Institutional Investor (DII) flows are "
                       "the dominant short-term driver of Indian market "
                       "direction. FII selling pressure (e.g. on global "
                       "risk-off) is the most common cause of sharp "
                       "Nifty/BankNifty sell-offs.",
        "signals": [
            "fii selling", "fii outflow", "fii outflows", "fii pullout",
            "fii buying", "fii inflow", "fii inflows", "fii flow", "fii flows",
            "fii data", "fii activity",
            "dii buying", "dii inflow", "dii inflows",
            "dii selling", "dii outflow", "dii data", "dii activity",
            "foreign investor", "foreign portfolio investor", "fpi data",
            "fpi flow", "fpi flows", "fpi selling", "fpi outflow",
            "fpi buying", "fpi inflow",
            "block deal", "bulk deal", "bulk deal data",
            "institutional activity", "institutional flows",
        ],
        "affected_tickers": ["BANKBARODA", "JIOFIN", "RELIANCE", "ITC",
                              "NTPCGREEN", "KNRCON", "IRCON", "BALRAMCHIN"],
        "context": "FII selling = broad market headwind (all stocks); "
                   "DII buying often offsets FII selling for large-caps",
    },
    "auto_sales": {
        "name": "Auto Sales / 2W Numbers",
        "description": "Monthly auto sales numbers (passenger vehicles, "
                       "two-wheelers, tractors) are a key indicator of "
                       "rural + urban demand. Strong numbers signal "
                       "expansion; weak numbers compress valuations.",
        "signals": [
            "auto sales", "car sales", "vehicle sales", "passenger vehicle",
            "two-wheeler", "two wheeler", "2w sales", "2-wheeler",
            "motorcycle sales", "scooter sales", "tractor sales",
            "commercial vehicle", "cv sales", "truck sales", "m&hcv",
            "maruti suzuki", "maruti sales", "tata motors", "mahindra",
            "bajaj auto", "hero motocorp", "tvs motor", "eicher motors",
            "monthly auto", "auto numbers", "auto dispatches",
            "two-wheeler sales", "tractor volumes",
        ],
        "affected_tickers": ["KNRCON", "BANKBARODA", "JIOFIN"],
        "context": "Auto sales indicate rural + urban demand health; "
                   "they affect NBFC (auto loans), banks (auto loan "
                   "portfolios), and infra (road demand)",
    },
    "gst_council": {
        "name": "GST Council Decisions",
        "description": "GST rate changes (hikes, cuts, rate rationalisation) "
                       "directly impact FMCG, cement, auto, and consumer "
                       "sectors. State-level GST collections also indicate "
                       "economic activity.",
        "signals": [
            "gst council", "gst meeting", "gst rate", "gst hike", "gst cut",
            "gst reduction", "gst increase", "gst slab", "gst rate change",
            "gst compensation", "gst compensation cess",
            "gst collection", "gst revenue", "gst collections",
            "gst evasion", "gst notice", "gst anti-profiteering",
            "compensation cess", "gstr", "gstr-1", "gstr-3b",
            "tax rate change", "indirect tax", "tax council",
        ],
        "affected_tickers": ["ITC", "RELIANCE", "KNRCON", "BALRAMCHIN"],
        "context": "GST hikes compress margins in affected sectors; "
                   "GST cuts boost volumes. Council meetings drive "
                   "short-term volatility.",
    },
}


# ============================================================
# Time-horizon impact map: (theme, ticker) -> per-horizon effect
# ============================================================
# Each entry is:
#   short:  3-6 months effect on the stock (revenue, demand, costs)
#   mid:    6-12 months effect (capex, contracts, market share)
#   long:   1-3 years effect (policy, secular trends, demographics)
# Plus a 'direction' and 'confidence' for the per-ticker view.
#
# Direction values: "+" (bullish), "-" (bearish), "." (neutral / context-dependent)
#
# This is intentionally compact: a few lines per (theme, ticker)
# that capture the dominant transmission mechanism. The user can
# drill into the per-ticker analysis block for details.
INDIAN_THEME_IMPACT: dict[tuple[str, str], dict] = {
    # ---------- Monsoon ----------
    ("monsoon", "ITC"): {
        "short": ("+", "More rain means farmers earn more, so they buy more "
                       "cigarettes, snacks, and soaps from ITC. Direct sales "
                       "boost in 3-6 months."),
        "mid":   ("+", "Two straight years of good monsoons mean villages have "
                       "more disposable income, so ITC's rural distribution "
                       "(which is 60% of sales) keeps growing for 1-2 years."),
        "long":  (".", "Climate change is making monsoons less reliable; "
                       "this is a long-term risk to ITC's rural base, but "
                       "their FMCG diversification (oats, dairy, hygiene) "
                       "partially insulates them."),
    },
    ("monsoon", "BALRAMCHIN"): {
        "short": ("+", "More rain means sugarcane farmers plant more cane, so "
                       "Balrampur Chini gets MORE raw material to crush. Sugar "
                       "output rises in the next crushing season (Nov-Mar)."),
        "mid":   (".", "Sugar prices are set by global markets and government "
                       "export quotas — good rain helps volumes, but doesn't "
                       "control realisations. Ethanol diversification provides "
                       "a margin buffer."),
        "long":  (".", "Climate volatility could make cane yields more "
                       "variable over decades, but ethanol (from sugarcane) "
                       "is becoming a structural growth driver as India "
                       "pushes E20 blending."),
    },
    ("monsoon", "KNRCON"): {
        "short": ("+", "Good monsoon means the government spends more on rural "
                       "roads (PMGSY scheme), and water-logged rural areas "
                       "need new roads. KNR's order book fills up within "
                       "1-2 quarters."),
        "mid":   (".", "Actual project awards depend on NHAI and state "
                       "budgets, not directly on rain. Monsoon gives a tailwind, "
                       "but the timing of tender awards is a separate cycle."),
        "long":  (".", "Climate change may shift priorities toward flood-"
                       "control and irrigation roads. But India's rural road "
                       "TAM (target 1 lakh+ km) is so large that KNR will "
                       "have work for 15+ years regardless."),
    },
    ("monsoon", "NTPCGREEN"): {
        "short": ("-", "Weak monsoon means less hydro-power generation, and "
                       "rain doesn't help solar (which needs clear skies). "
                       "Short-term: lower capacity utilisation for hydro "
                       "assets."),
        "mid":   (".", "Hydro reservoirs recover over 6-12 months anyway. "
                       "Solar capacity additions are weather-independent — "
                       "they just need capex. Monsoon is a temporary drag, "
                       "not a structural one."),
        "long":  (".", "Climate change is a net long-term tailwind for "
                       "renewables — more extreme weather drives demand for "
                       "clean energy. NTPC Green's solar + wind + hydro "
                       "portfolio is structurally well-positioned for the "
                       "energy transition."),
    },
    ("monsoon", "RELIANCE"): {
        "short": ("-", "Strong monsoon reduces crude oil imports slightly "
                       "(less farm machinery fuel, fewer power cuts), so "
                       "Reliance's refining margins get a tiny drag."),
        "mid":   (".", "Refining margins are dominated by Singapore complex "
                       "dynamics and product cracks (petrol, diesel, jet fuel) "
                       "— domestic monsoon is too small a factor to matter "
                       "in the medium term."),
        "long":  (".", "India's structural energy demand growth is the real "
                       "Reliance story. Monsoon cycles wash through over a "
                       "quarter; they don't move the long-term value."),
    },
    ("monsoon", "BANKBARODA"): {
        "short": ("+", "Good monsoon means farmers take crop loans and "
                       "tractor loans — Bank of Baroda's agri-loan book "
                       "grows within 1 quarter. Higher rural income also "
                       "means more savings deposits."),
        "mid":   ("+", "Sustained rural credit demand for 2-3 years + lower "
                       "NPA cycle in agri-loans = better book quality and "
                       "stable NIM. BoB is a major rural lender."),
        "long":  (".", "Rural banking is a structural play for PSU banks — "
                       "but monsoon volatility is a recurring but manageable "
                       "risk. Long-term: depends on digital banking "
                       "penetration, not weather."),
    },
    ("monsoon", "JIOFIN"): {
        "short": ("+", "Good monsoon lifts small-ticket lending demand: "
                       "tractor loans, two-wheeler finance, personal loans "
                       "in rural areas. Jio Financial's NBFC sees volume "
                       "growth in 1-2 quarters."),
        "mid":   (".", "NBFC AUM growth is more driven by the interest-rate "
                       "cycle and competition from banks. Monsoon is a "
                       "seasonal boost, not a structural driver."),
        "long":  (".", "Jio's distribution scale (450M+ telecom users) is "
                       "the long-term moat for cross-selling loans. Monsoon "
                       "is a small variable in a much bigger equation."),
    },
    ("monsoon", "IRCON"): {
        "short": (".", "Railway construction is mostly all-weather. Monsoon "
                       "doesn't directly affect IRCON's project execution."),
        "mid":   (".", "Floods and landslides in hilly states can delay some "
                       "projects by 1-2 months. Mid-term: minor impact on "
                       "delivery timelines, not on order book size."),
        "long":  (".", "Climate resilience (flood-proofing) is becoming a "
                       "structural component of railway capex. Minor effect."),
    },
    # ---------- Local politics ----------
    ("local_politics", "BANKBARODA"): {
        "short": (".", "State elections cause short-term sentiment swings "
                       "in PSU bank stocks, but they don't change BoB's "
                       "underlying earnings. Watch the headline, not the "
                       "thesis."),
        "mid":   (".", "State budgets allocate MSME credit and agri-loan "
                       "subsidies, which can affect BoB's regional loan book. "
                       "Long-term impact depends on whether new CMs cut or "
                       "increase spending."),
        "long":  (".", "PSU bank consolidation, privatisation, and capital "
                       "infusion are CENTRAL-government decisions, not state. "
                       "State politics is noise over 3-5 year horizon."),
    },
    ("local_politics", "KNRCON"): {
        "short": ("+", "When a new CM is pro-infrastructure, NHAI awards and "
                       "state highway projects get fast-tracked. KNR's order "
                       "book fills up in 1-2 quarters."),
        "mid":   ("+", "A pro-infrastructure coalition government that stays "
                       "in power for 3-5 years provides order-book visibility. "
                       "State projects are awarded faster, faster payments."),
        "long":  (".", "Long-term infra spending depends on central + state "
                       "finances, not political colour. KNR benefits under "
                       "most regimes; political swings matter less than "
                       "fiscal deficits."),
    },
    ("local_politics", "IRCON"): {
        "short": ("+", "A new state CM prioritising rail/metro capex can "
                       "lead to fast-track state-funded projects. IRCON "
                       "wins more contracts within 1-2 quarters."),
        "mid":   ("+", "Central railway capex is largely insulated from state "
                       "politics — the railway budget is decided in Delhi. "
                       "But state-funded projects get priority based on CMs."),
        "long":  (".", "IRCON's order book is dominated by central rail "
                       "ministry projects, not state. Long-term trajectory "
                       "follows central capex, not state-level politics."),
    },
    ("local_politics", "ITC"): {
        "short": ("-", "State governments hike VAT on cigarettes to raise "
                       "revenue — this directly squeezes ITC's margins in "
                       "those states. State tax hikes are a 3-6 month "
                       "drag on per-stick realisations."),
        "mid":   ("-", "Persistent state tax hikes compound. ITC has limited "
                       "ability to pass these on to consumers because "
                       "cigarettes are a sin-good with political baggage."),
        "long":  (".", "ITC's diversification into FMCG non-tobacco (oats, "
                       "dairy, hygiene) reduces long-run state-tax exposure, "
                       "but cigarettes are 50%+ of profits and "
                       "structurally challenged."),
    },
    ("local_politics", "JIOFIN"): {
        "short": (".", "State-level consumer-protection rules can affect "
                       "Jio's digital lending collection practices. Mostly "
                       "compliance cost — no major P&L impact."),
        "mid":   (".", "Mid-term: digital lending regulations vary by state; "
                       "compliance overhead can compress margins by 0.5-1%. "
                       "But growth dominates over compliance cost."),
        "long":  (".", "Long-term: Jio's lending moat is built on its 450M+ "
                       "telecom subscribers and data, not on state-level "
                       "regulation. State politics is a tactical variable."),
    },
    ("local_politics", "RELIANCE"): {
        "short": (".", "State-level fuel pricing variations marginally affect "
                       "petrol pump margins. Telecom licensing is central, "
                       "not state. Most Reliance segments are insulated."),
        "mid":   (".", "Mid-term: state retail policy (Sunday closures, shop "
                       "timing, labour laws) affects Reliance Retail's "
                       "store revenues across states, but net impact is "
                       "small relative to the national roll-out."),
        "long":  (".", "Reliance's long-term value is dominated by O2C "
                       "margins, Jio ARPU, and retail scale. State politics "
                       "is a tertiary factor for 80% of the business."),
    },
    ("local_politics", "NTPCGREEN"): {
        "short": (".", "State land acquisition delays can push back solar "
                       "project commissioning by 1-2 quarters. But "
                       "delays are project-specific, not company-wide."),
        "mid":   (".", "Mid-term: state renewable-energy policy (subsidies, "
                       "land banks) shapes where NTPC Green builds next. "
                       "States with strong RE policy attract more capex."),
        "long":  ("+", "Long-term: India's energy transition is a central "
                       "policy priority. NTPC Green is the public-sector "
                       "vehicle for state-level renewable build-out — "
                       "structural tailwind regardless of state politics."),
    },
    ("local_politics", "BALRAMCHIN"): {
        "short": ("+", "State governments set the SAP (State Advised Price) "
                       "for sugarcane, which is a major input cost. A higher "
                       "SAP means more cost for sugar mills. A lower SAP "
                       "boosts margins. (Note: SAPs usually only move up.)"),
        "mid":   (".", "Mid-term: state-level ethanol procurement policy and "
                       "sugar export quotas (decided by central + state) "
                       "affect realisations. This is a tactical variable, "
                       "not a structural one."),
        "long":  (".", "Long-term: the sugar industry is centrally regulated "
                       "(CACP, FRP). State-level decisions are tactical, "
                       "not strategic. Ethanol blending is the long-term "
                       "structural shift."),
    },
    # ---------- Festival / Wedding ----------
    ("festival_wedding", "ITC"): {
        "short": ("+", "Diwali, Dhanteras, Akshaya Tritiya = peak season for "
                       "cigarettes, snacks, chocolates, and personal-care. "
                       "Sales volume jumps 15-30% in the festive quarter."),
        "mid":   (".", "Mid-term: rural income (post-monsoon) is the bigger "
                       "driver of FMCG demand. Festivals are a tailwind, "
                       "not a structural shift."),
        "long":  (".", "Wedding season (Oct-Jan) is a perennial demand driver. "
                       "Long-term: ITC's mix shift toward non-tobacco FMCG "
                       "is the structural story, not festival intensity."),
    },
    ("festival_wedding", "RELIANCE"): {
        "short": ("+", "Festive season boosts Reliance Retail (apparel, "
                       "electronics, jewellery) and Jio recharge plans during "
                       "Dhanteras / Diwali. Same-store sales spike 20-40%."),
        "mid":   (".", "Mid-term: festive boost is seasonal; structural retail "
                       "growth depends on store count expansion and consumer "
                       "credit availability."),
        "long":  (".", "Long-term: Reliance Retail and Jio have structural "
                       "growth independent of any single festive cycle. "
                       "TAM is large and growing."),
    },
    ("festival_wedding", "BANKBARODA"): {
        "short": ("+", "Festive / wedding season drives personal loans, "
                       "auto loans, gold loans, and credit card spend. "
                       "BoB's retail loan book grows within 1 quarter."),
        "mid":   (".", "Mid-term: loan growth is structurally supported by "
                       "rising consumer credit penetration. Festive spikes "
                       "are tactical, not structural."),
        "long":  (".", "Long-term: bank valuation is driven by NIM, asset "
                       "quality, and digital adoption — not festive demand. "
                       "PSU banks are structural beneficiaries of credit "
                       "deepening."),
    },
    ("festival_wedding", "KNRCON"): {
        "short": (".", "Wedding season boosts demand for finished roads and "
                       "real-estate connectivity. But this is too indirect "
                       "to move KNR's order book in 1-2 quarters."),
        "mid":   (".", "Mid-term: KNR's order book is driven by government "
                       "capex cycles (NHAI, state PWDs), not festive demand. "
                       "No material transmission mechanism."),
        "long":  (".", "Long-term: KNR's order book is policy-driven; "
                       "festive demand is a tactical factor, not a "
                       "structural one."),
    },
    ("festival_wedding", "BALRAMCHIN"): {
        "short": ("+", "Indian weddings use lots of sugar (sweets, desserts, "
                       "chai). Festive season drives 10-15% volume bump for "
                       "branded sugar and value-added products."),
        "mid":   (".", "Mid-term: sugar demand is seasonal but predictable. "
                       "No structural change from festive intensity."),
        "long":  (".", "Long-term: structural shift is toward ethanol and "
                       "industrial sugar use. Festive demand is a stable "
                       "tactical factor, not a structural one."),
    },
    ("festival_wedding", "JIOFIN"): {
        "short": ("+", "Wedding / festive season drives consumer credit demand: "
                       "personal loans, two-wheeler finance, gold loans. "
                       "Jio Financial sees 15-20% spike in disbursements."),
        "mid":   (".", "Mid-term: AUM growth is more sensitive to rate cycle "
                       "and competition than to festive demand."),
        "long":  (".", "Long-term: Jio's distribution scale (450M+ users) is "
                       "the moat. Festive spikes are tactical, not structural."),
    },
    ("festival_wedding", "NTPCGREEN"): {
        "short": (".", "Festive demand doesn't directly affect power "
                       "generation. Power demand follows industrial + "
                       "agricultural cycles, not consumer festive cycles."),
        "mid":   (".", "Mid-term: same — power consumption is structural, "
                       "not festive. No transmission mechanism between "
                       "Diwali and NTPC Green's plant output."),
        "long":  (".", "Long-term: power generation is regulated and "
                       "demand-driven; festive cycles don't move it."),
    },
    # ---------- RBI / SEBI / Government Policy ----------
    ("rbi_policy", "BANKBARODA"): {
        "short": ("+", "When RBI raises rates, banks earn MORE on their loans "
                       "(home loans, car loans) but pay slightly more on "
                       "deposits. Net Interest Margin (NIM) widens within "
                       "1-2 quarters. Bank of Baroda benefits directly."),
        "mid":   ("+", "Sustained tight policy (high rates) boosts treasury "
                       "income and credit growth — but if rates stay high too "
                       "long, loan demand slows. Mid-term: depends on whether "
                       "RBI pivots to cuts."),
        "long":  (".", "Long-term: BoB's valuation is driven by structural "
                       "growth (deposits, credit penetration, digital banking) "
                       "and asset quality, not the rate cycle."),
    },
    ("rbi_policy", "JIOFIN"): {
        "short": ("+", "NBFCs like Jio Financial borrow short and lend long. "
                       "When RBI CUTS rates, their cost of funds drops, but "
                       "their loan book re-prices slowly — net interest margin "
                       "expands. When RBI HIKES, the opposite: NIM compresses."),
        "mid":   ("+", "An easing cycle (rate cuts over 12-18 months) "
                       "supports AUM growth and disbursement volumes. A "
                       "tightening cycle slows growth but improves asset quality "
                       "(fewer defaults in a slowdown)."),
        "long":  (".", "Long-term: Jio's competitive moat is built on its "
                       "telecom distribution scale and digital lending "
                       "penetration, not policy rates. Rates are tactical, "
                       "moat is structural."),
    },
    ("rbi_policy", "RELIANCE"): {
        "short": (".", "Reliance is diversified across refining, telecom, and "
                       "retail — each segment has a different sensitivity. "
                       "Rate hikes slow Jio capex returns, but boost deposit "
                       "franchise growth. Net effect is mixed."),
        "mid":   (".", "Mid-term: an easing cycle boosts consumer credit "
                       "demand (JioMart, Reliance Retail credit) and "
                       "encourages telecom capex absorption. A tightening "
                       "cycle pressures both. But O2C (refining + petrochem) "
                       "is mostly insulated."),
        "long":  (".", "Reliance's long-term value is driven by refining "
                       "margins, Jio ARPU growth, and retail scale. Policy "
                       "rates are a tertiary factor over 3-5 year horizon."),
    },
    ("rbi_policy", "ITC"): {
        "short": (".", "Rate cuts help FMCG demand (cheaper consumer credit) "
                       "but rate hikes hurt it. Net effect is mild because "
                       "cigarettes and daily essentials are not credit-driven "
                       "purchases."),
        "mid":   (".", "Mid-term: rural FMCG volumes are more sensitive to "
                       "monsoon and inflation than to repo rate. Rate cycle "
                       "matters less than the underlying income story."),
        "long":  (".", "Long-term: India's consumption growth (rural income, "
                       "FMCG penetration) is the bigger story. Rate cycles "
                       "wash through over 2-3 years."),
    },
    ("rbi_policy", "NTPCGREEN"): {
        "short": ("+", "Rate cuts lower the cost of capital for renewable "
                       "projects. Project IRRs improve, so new projects are "
                       "sanctioned faster. Direct benefit within 1 quarter."),
        "mid":   ("+", "Mid-term: a rate-cut cycle (12-18 months) drives "
                       "renewable capex as developers can finance at lower "
                       "cost. NTPC Green wins more project awards."),
        "long":  (".", "Long-term: structural energy transition is the bigger "
                       "tailwind. Rate cycles are tactical; transition is "
                       "20-year structural."),
    },
    ("rbi_policy", "KNRCON"): {
        "short": ("+", "Rate cuts lower working-capital borrowing cost for "
                       "EPC contractors. Mid-sized infra firms benefit most. "
                       "Loan EMIs come down; project cash flows improve."),
        "mid":   ("+", "Mid-term: rate-cut cycle unlocks new infra project "
                       "tenders (NHAI, state PWDs) and improves HAM bid "
                       "economics. KNR wins more projects at better margins."),
        "long":  (".", "Long-term: India's infra build-out (PM Gati Shakti) "
                       "is a 10-15 year structural story. Rate cycle is a "
                       "secondary factor."),
    },
    ("rbi_policy", "IRCON"): {
        "short": (".", "PSU railway funding is mostly sovereign-backed; "
                       "rate cycle has minor impact on project economics for "
                       "IRCON. The company doesn't borrow much at market rates."),
        "mid":   (".", "Mid-term: state PSU bond issuance is affected by "
                       "rate cycle (mild impact on borrowing cost), but "
                       "railway capex is policy-driven regardless of rates."),
        "long":  (".", "Long-term: railway capex is a sovereign decision "
                       "(railway budget voted in Parliament). Rate cycle is "
                       "noise over 3-5 year horizon."),
    },
    ("rbi_policy", "BALRAMCHIN"): {
        "short": (".", "Sugar mills have seasonal working-capital loans. "
                       "Rate cuts slightly ease these, but the impact is "
                       "minor relative to sugar prices and export quota."),
        "mid":   (".", "Mid-term: sugar industry is heavily regulated; margins "
                       "are determined by FRP (Fair and Remunerative Price) "
                       "and export quota, not by RBI rates."),
        "long":  (".", "Long-term: structural shift to ethanol and energy "
                       "applications is the long-term value driver. Rate "
                       "cycle is irrelevant to that story."),
    },
    # ---------- Commodity Prices ----------
    ("commodity_prices", "RELIANCE"): {
        "short": ("+", "When Brent crude price goes up, Reliance's refining "
                       "margins (called GRM) expand because they process crude "
                       "at low cost and sell refined products (petrol, diesel) "
                       "at higher prices. Direct P&L benefit within a quarter."),
        "mid":   (".", "Mid-term: very high crude prices eventually hurt "
                       "marketing margins (downstream fuel demand falls) "
                       "and squeeze petrochem realisations. The O2C segment "
                       "is a balancing act over 1-2 years."),
        "long":  (".", "Long-term: India's energy transition (renewables, "
                       "green hydrogen, EVs) gradually reduces crude oil's "
                       "structural importance. Reliance's long-term value is "
                       "in Jio + retail, not refining."),
    },
    ("commodity_prices", "BALRAMCHIN"): {
        "short": ("+", "When global sugar prices go up, Balrampur Chini "
                       "earns more per kg sold. Ethanol price hike also "
                       "boosts per-litre margin. Direct revenue boost within "
                       "1-2 quarters."),
        "mid":   (".", "Sugar is a cyclical commodity. Mid-term prices depend "
                       "on global supply-demand balance and Indian export "
                       "quota policy. Ethanol diversification provides some "
                       "margin stability."),
        "long":  (".", "Long-term: ethanol blending policy (E20 by 2025) and "
                       "energy-transition demand for ethanol is a structural "
                       "tailwind. Sugar is cyclical; ethanol is the growth "
                       "story."),
    },
    ("commodity_prices", "KNRCON"): {
        "short": ("-", "When steel, cement, or bitumen prices go up, KNR's "
                       "project margins shrink because they have to buy these "
                       "inputs at higher prices. They can't immediately pass "
                       "the cost to NHAI/state clients."),
        "mid":   (".", "Mid-term: input costs settle; competitive bidding "
                       "adjusts. Net effect depends on order mix (HAM projects "
                       "have better inflation-indexed pass-through than pure "
                       "EPC)."),
        "long":  (".", "Long-term: input cost volatility is the new normal. "
                       "Players with scale + backward integration (their own "
                       "steel, cement) have an edge. KNR is mid-scale."),
    },
    ("commodity_prices", "ITC"): {
        "short": ("-", "Cigarette paper, packaging materials, and crude (for "
                       "ITC's hotel business) are minor cost inputs. Commodity "
                       "moves are small but directional. Margin compression "
                       "in 1-2 quarters."),
        "mid":   (".", "Mid-term: FMCG input costs are managed through long-"
                       "term contracts. Commodity price spikes are smoothed "
                       "out over 6-12 months. Net impact is small."),
        "long":  (".", "Long-term: ITC's cost structure is dominated by taxes "
                       "(excise, GST) and labor, not commodities. Pricing power "
                       "and regulatory environment matter more."),
    },
    ("commodity_prices", "IRCON"): {
        "short": ("-", "Steel is a key input for railway tracks, bridges, "
                       "and stations. When steel prices rise, IRCON's project "
                       "margins compress. Limited ability to pass through to "
                       "Indian Railways (the buyer)."),
        "mid":   (".", "Mid-term: input cost volatility is partially absorbed "
                       "by contract escalation clauses (if any). IRCON's "
                       "margins are also helped by lower competitive intensity "
                       "in railway projects (few PSU competitors)."),
        "long":  (".", "Long-term: railway capex is policy-driven; input cost "
                       "volatility is a recurring but manageable risk. India "
                       "needs 4-5% GDP growth to support railway infra capex."),
    },
    ("commodity_prices", "JIOFIN"): {
        "short": (".", "Jio Financial is an NBFC; commodity prices don't "
                       "directly affect their lending margins."),
        "mid":   (".", "Mid-term: same — no direct transmission mechanism. "
                       "NBFC margins are driven by rate spread, credit "
                       "quality, and operational efficiency."),
        "long":  (".", "Long-term: Jio's moat is its distribution scale. "
                       "Commodity prices are noise over 3-5 year horizon."),
    },
    ("commodity_prices", "NTPCGREEN"): {
        "short": ("-", "Solar module prices going UP hurts project economics "
                       "(higher capex per MW). Steel and aluminium price rises "
                       "also hurt. Net: input cost pressure on capex."),
        "mid":   (".", "Mid-term: technology improvements continue to drive "
                       "solar module prices DOWN over 3-5 years. Steel and "
                       "aluminium prices fluctuate. Net: balance is roughly "
                       "neutral over 1-2 years."),
        "long":  ("+", "Long-term: continued module price decline is a "
                       "structural tailwind for renewable energy economics. "
                       "Module costs have fallen 90%+ in 10 years. NTPC Green "
                       "benefits from this long-term trend."),
    },
    # ---------- Global Supply Chain ----------
    ("global_supply_chain", "RELIANCE"): {
        "short": (".", "Reliance is largely self-sufficient for refining, "
                       "petrochem, and retail. O2C imports some catalysts — "
                       "small impact. Jio's 5G equipment is imported but "
                       "spreads over many quarters."),
        "mid":   (".", "Mid-term: Jio's 5G equipment rollout is exposed to "
                       "semiconductor supply chain. Delays can push back "
                       "capex absorption and ARPU growth timing."),
        "long":  (".", "Long-term: India's PLI (Production-Linked Incentive) "
                       "scheme is shifting supply chains domestic. Reliance's "
                       "supply chain exposure reduces over time."),
    },
    ("global_supply_chain", "KNRCON"): {
        "short": ("-", "Container rate / shipping cost rise increases the "
                       "cost of imported road-building equipment (specialised "
                       "machinery, asphalt plants). Project margins shrink."),
        "mid":   (".", "Mid-term: pass-through to NHAI in HAM/EPC contracts "
                       "is delayed by 1-2 quarters. Margins can compress in "
                       "the interim. Bid economics on new tenders adjust."),
        "long":  (".", "Long-term: India's domestic construction equipment "
                       "manufacturing is scaling (under Make in India). "
                       "KNR's import dependence reduces over 5-10 years."),
    },
    ("global_supply_chain", "ITC"): {
        "short": (".", "ITC sources most packaging domestically. Paper and "
                       "pulp imports are a small share of cost. Supply chain "
                       "shocks are smoothed by long-term contracts."),
        "mid":   (".", "Mid-term: same — minimal direct exposure. Most inputs "
                       "are domestic; cost impact is small."),
        "long":  (".", "Long-term: domestic packaging industry is well-"
                       "developed; import dependence is small and decreasing."),
    },
    ("global_supply_chain", "JIOFIN"): {
        "short": (".", "Jio's tech stack is mostly in-house (Reliance Jio "
                       "Platforms); no significant supply chain exposure. "
                       "NBFC is a financial services business."),
        "mid":   (".", "Mid-term: same — minimal direct exposure. NBFC is a "
                       "financial services business, not industrial."),
        "long":  (".", "Long-term: digital lending is a software business; "
                       "physical supply chain is irrelevant to the thesis."),
    },
    ("global_supply_chain", "BANKBARODA"): {
        "short": (".", "Banks are not directly exposed to global supply "
                       "chains. No direct impact."),
        "mid":   (".", "Mid-term: trade finance volumes (LCs, guarantees) "
                       "are affected by global supply chain disruptions. "
                       "But the impact is small relative to retail loan "
                       "growth."),
        "long":  (".", "Long-term: bank growth is driven by domestic credit "
                       "demand. Supply chain is not a factor for retail/PSU "
                       "banking."),
    },
    ("global_supply_chain", "NTPCGREEN"): {
        "short": ("-", "Solar modules and equipment imports are exposed to "
                       "shipping delays. Project commissioning can slip by "
                       "1-2 quarters if modules don't arrive on time."),
        "mid":   (".", "Mid-term: PLI scheme is building domestic solar "
                       "manufacturing capacity. Import dependence is reducing. "
                       "Module import share is falling year-over-year."),
        "long":  ("+", "Long-term: domestic solar module manufacturing (under "
                       "PLI) makes NTPC Green structurally less exposed to "
                       "global supply chain shocks. Strong structural shift."),
    },
    ("global_supply_chain", "IRCON"): {
        "short": ("-", "Railway equipment imports (signalling systems, "
                       "rolling stock components) face shipping cost rises. "
                       "Some critical parts have few domestic alternatives."),
        "mid":   (".", "Mid-term: 'Make in India' for railways is increasing; "
                       "import dependence is reducing. BEML, BEML Ltd, and "
                       "other PSU vendors are scaling up."),
        "long":  (".", "Long-term: railway sector is heavily domestic. Supply "
                       "chain impact is tactical, not structural. Railways are "
                       "a 10+ year capex story."),
    },
    ("global_supply_chain", "BALRAMCHIN"): {
        "short": (".", "Sugar is a domestic commodity. Sugar mills use "
                       "domestic machinery and labour. No significant global "
                       "supply chain exposure."),
        "mid":   (".", "Mid-term: same — sugar production is local, no "
                       "shipping cost impact. Ethanol and power are also "
                       "domestic."),
        "long":  (".", "Long-term: sugar sector is largely self-sufficient. "
                       "Supply chain is not a factor."),
    },

    # ---------- GDP / IIP / Macro Data ----------
    ("gdp_iip_print", "ITC"): {
        "short": ("+", "When India's GDP grows fast or IIP (factory output) "
                       "rises, consumers feel richer, so they buy more FMCG "
                       "goods — cigarettes, soaps, snacks. Sales pick up "
                       "in 1-2 quarters."),
        "mid":   (".", "Mid-term: structural growth is multi-year; a single "
                       "data print is tactical, not a trend. Watch 2-3 prints "
                       "in a row to confirm direction."),
        "long":  (".", "Long-term: ITC is a structural play on India's "
                       "consumption story. The GDP cycle is one factor among "
                       "many (rural income, demographics, regulations)."),
    },
    ("gdp_iip_print", "RELIANCE"): {
        "short": ("+", "Strong GDP/IIP prints lift refining margins (more "
                       "fuel demand), Jio ARPU (corporate customers upgrade), "
                       "and retail same-store sales (footfall rises)."),
        "mid":   (".", "Mid-term: structural GDP growth supports the entire "
                       "Reliance portfolio (consumer + industrial). "
                       "Single prints are tactical; trend matters more."),
        "long":  (".", "Long-term: India's structural GDP growth is a tailwind "
                       "for Reliance's diversified businesses. 6-7% GDP "
                       "compounded for 10 years = enormous demand."),
    },
    ("gdp_iip_print", "JIOFIN"): {
        "short": ("+", "Strong GDP/IIP prints lift credit demand — "
                       "people take more personal loans, business loans, "
                       "and auto loans. Jio Financial's AUM grows in 1-2 "
                       "quarters."),
        "mid":   (".", "Mid-term: NBFC AUM grows with the credit cycle. "
                       "GDP is a leading indicator (3-6 months ahead of "
                       "credit growth)."),
        "long":  (".", "Long-term: financial deepening (more Indians using "
                       "formal credit) is a 10-20 year structural play. "
                       "GDP growth supports this trend."),
    },
    ("gdp_iip_print", "BANKBARODA"): {
        "short": ("+", "Strong GDP/IIP prints drive credit growth (loans "
                       "for factories, businesses, retail) and lower NPA "
                       "provisioning (borrowers pay back on time when "
                       "economy is strong). Bank earnings improve."),
        "mid":   (".", "Mid-term: GDP is a long-term tailwind for banking, "
                       "but a single print is tactical. Watch the trend "
                       "over 2-3 quarters."),
        "long":  (".", "Long-term: PSU banks are structural beneficiaries of "
                       "India's GDP growth — credit penetration is at ~60% "
                       "of GDP (vs 100%+ in China), so multi-decade tailwind."),
    },
    ("gdp_iip_print", "NTPCGREEN"): {
        "short": ("+", "Strong IIP (industrial production) and manufacturing "
                       "PMI lift power demand from factories. Renewable "
                       "capex absorption improves as state discoms pay up."),
        "mid":   (".", "Mid-term: GDP growth drives electricity demand growth. "
                       "Long-term capex visibility for renewable projects."),
        "long":  (".", "Long-term: India needs 6-7% GDP growth to support "
                       "energy transition capex (solar, wind, hydro, storage). "
                       "Strong structural tailwind."),
    },
    ("gdp_iip_print", "KNRCON"): {
        "short": ("+", "Strong infra capex (correlated with GDP) drives NHAI "
                       "and state project awards. KNR wins more contracts in "
                       "1-2 quarters."),
        "mid":   (".", "Mid-term: KNR's order book visibility is tied to "
                       "multi-year capex cycles (5-year plans), not single "
                       "GDP prints. The order book grows in step with GDP."),
        "long":  (".", "Long-term: India's infra build-out (PM Gati Shakti, "
                       "Bharatmala) is a 10-15 year structural story aligned "
                       "with GDP growth. Rs 100 lakh crore+ opportunity."),
    },
    ("gdp_iip_print", "IRCON"): {
        "short": ("+", "Strong IIP / capex prints drive railway and metro "
                       "project awards. Order book fills up in 1-2 quarters."),
        "mid":   (".", "Mid-term: order book growth correlates with multi-year "
                       "capex plans (5-year railway capex cycle). GDP "
                       "growth supports but doesn't determine it."),
        "long":  (".", "Long-term: railway infrastructure is a sovereign "
                       "decision tied to GDP and industrial output growth. "
                       "Strong structural tailwind."),
    },
    ("gdp_iip_print", "BALRAMCHIN"): {
        "short": (".", "Sugar demand is rural-income-driven; GDP effect is "
                       "indirect via rural wages and consumption. Single "
                       "GDP prints don't move sugar prices."),
        "mid":   (".", "Mid-term: same — no direct GDP linkage. Sugar "
                       "prices are set by global markets and Indian export "
                       "quota policy."),
        "long":  (".", "Long-term: rural GDP growth is a tailwind for sugar "
                       "demand (more sweet consumption) and ethanol "
                       "diversification (more fuel demand)."),
    },

    # ---------- FII / DII Flows ----------
    ("fii_dii_flows", "BANKBARODA"): {
        "short": ("+", "When foreign investors (FIIs) buy Indian PSU bank "
                       "stocks (often because they're cheap vs global peers), "
                       "the price lifts. DII buying is steady. Net FII outflow "
                       "is a headwind but DII often absorbs."),
        "mid":   (".", "Mid-term: PSU bank valuations track fundamentals more "
                       "than FII flows. Over 1-2 years, ROA, NIM, and asset "
                       "quality matter more than who is buying."),
        "long":  (".", "Long-term: structural domestic flows (DII SIPs of "
                       "Rs 20,000+ crore/month) outweigh FII cyclicality. "
                       "India's GDP growth + financial deepening is the story."),
    },
    ("fii_dii_flows", "JIOFIN"): {
        "short": ("+", "When FIIs sell Indian mid-cap NBFCs like Jio "
                       "Financial, the stock price drops. When they buy, "
                       "it rises. NBFC valuations are very flow-sensitive."),
        "mid":   (".", "Mid-term: AUM growth (lending) is the structural "
                       "driver. FII flows move the price but don't change "
                       "the underlying business fundamentals."),
        "long":  (".", "Long-term: financial deepening in India is a "
                       "domestic story (450M+ Jio users). FII flows are "
                       "noise over 3-5 year horizon — DII SIPs are the "
                       "structural buyer."),
    },
    ("fii_dii_flows", "RELIANCE"): {
        "short": (".", "Reliance is a mega-cap with deep FII + DII "
                       "ownership. FII flows move the price (intraday) but "
                       "don't change the underlying refining / Jio / retail "
                       "fundamentals. Net effect is mostly noise."),
        "mid":   (".", "Mid-term: structural growth (O2C margins, Jio ARPU, "
                       "retail) is the bigger story. FII flows are tactical."),
        "long":  (".", "Long-term: Reliance's scale and diversification "
                       "insulate it from flow-driven volatility. Even if FIIs "
                       "sell, DIIs and the Ambani family hold the float."),
    },
    ("fii_dii_flows", "ITC"): {
        "short": (".", "ITC is a defensive FMCG stock. FII flows move the "
                       "price, but ITC's earnings are stable regardless — "
                       "FMCG demand is inelastic to flow cycles."),
        "mid":   (".", "Mid-term: FMCG defensiveness is the main draw. "
                       "When markets are volatile, money rotates into "
                       "FMCG. FII flows are tactical, not directional."),
        "long":  (".", "Long-term: dividend + FMCG + hotels = stable cash "
                       "flows regardless of FII activity. ITC is a yield "
                       "stock, not a momentum stock."),
    },
    ("fii_dii_flows", "NTPCGREEN"): {
        "short": ("-", "Renewable energy is a thematic sector that attracts "
                       "FII flows. When FIIs sell on global risk-off, "
                       "renewables get hit harder than defensives."),
        "mid":   (".", "Mid-term: structural renewable capex is policy-"
                       "driven (PM Surya Ghar, PLI), not flow-driven. "
                       "Order book grows regardless of FII activity."),
        "long":  (".", "Long-term: energy transition is a 20-year structural "
                       "tailwind (India needs to add 50GW+ renewable capacity "
                       "by 2030). FII flows are noise over this horizon."),
    },
    ("fii_dii_flows", "KNRCON"): {
        "short": ("-", "Mid-cap infra is sensitive to FII selling. KNR has "
                       "less float than large-caps, so FII outflows hit the "
                       "stock disproportionately (price drops more %)."),
        "mid":   (".", "Mid-term: order book is structural (policy-driven, "
                       "multi-year capex plans). FII flows are tactical."),
        "long":  (".", "Long-term: India's infra TAM is structural (PM Gati "
                       "Shakti, Bharatmala). FII flows are noise over 3-5 "
                       "year horizon."),
    },
    ("fii_dii_flows", "IRCON"): {
        "short": ("-", "PSU is mid-cap sensitive to FII flows. PSU stocks "
                       "often under-perform when FIIs are heavy sellers "
                       "(FIIs prefer private large-caps). DII holding is "
                       "steady but limited."),
        "mid":   (".", "Mid-term: railway capex is structural. FII flows are "
                       "tactical and don't change the order book."),
        "long":  (".", "Long-term: PSU railway story is policy-driven. "
                       "Government holding + DII support insulate from "
                       "FII volatility. 10-year horizon is structural."),
    },
    ("fii_dii_flows", "BALRAMCHIN"): {
        "short": ("-", "Sugar is mid-cap and FII-sensitive. FII outflows hit "
                       "the stock price. Mid-caps have less float, so price "
                       "drops more in % terms."),
        "mid":   (".", "Mid-term: sugar sector is cyclical. FII flows are "
                       "tactical. Real driver is global sugar prices + Indian "
                       "export policy."),
        "long":  (".", "Long-term: ethanol + structural sugar demand is the "
                       "structural story. India's ethanol blending program "
                       "(E20) is policy-driven regardless of FII flows."),
    },

    # ---------- Auto Sales / 2W Numbers ----------
    ("auto_sales", "KNRCON"): {
        "short": ("+", "When auto sales go up, it means new roads are being "
                       "driven on. Strong auto numbers signal that India's "
                       "infrastructure build-out is paying off. KNR's order "
                       "book fills up in 1-2 quarters."),
        "mid":   (".", "Mid-term: KNR's order book is driven by government "
                       "capex (NHAI, state PWDs), not directly by auto "
                       "demand. Auto sales are a leading indicator of road "
                       "usage, not construction activity."),
        "long":  (".", "Long-term: road infrastructure is multi-decade. "
                       "India's auto TAM (passenger + 2W) is 30M+ units "
                       "annually. Correlation with KNR is tactical."),
    },
    ("auto_sales", "BANKBARODA"): {
        "short": ("+", "Strong auto sales drive auto-loan growth. When people "
                       "buy cars and 2-wheelers, they take loans from banks. "
                       "BoB's retail loan book grows in 1-2 quarters."),
        "mid":   (".", "Mid-term: loan book is structural. Auto-loan growth "
                       "is one component — housing, personal, business loans "
                       "are the bigger pieces."),
        "long":  (".", "Long-term: vehicle financing is a structural PSU "
                       "bank product. India's auto-loan penetration is at "
                       "~15% of new car sales; multi-decade growth potential."),
    },
    ("auto_sales", "JIOFIN"): {
        "short": ("+", "Strong 2W / auto sales drive Jio's two-wheeler "
                       "finance and personal loan products. Jio Financial "
                       "sees 15-20% spike in disbursements in 1 quarter."),
        "mid":   (".", "Mid-term: AUM growth is the structural driver. "
                       "Auto loans are one of several product lines (also "
                       "personal, business, merchant loans)."),
        "long":  (".", "Long-term: Jio's distribution scale (450M+ users) is "
                       "the moat. Auto finance is one product line; long-term "
                       "value comes from cross-selling across the Jio "
                       "ecosystem."),
    },

    # ---------- GST Council ----------
    ("gst_council", "ITC"): {
        "short": ("+", "If GST is CUT on cigarettes (rare, politically "
                       "sensitive), volumes would rise. If GST is HIKED, "
                       "margins compress. Either way, the effect is "
                       "immediate in 1 quarter."),
        "mid":   (".", "Mid-term: GST rates are stable. Rate changes are "
                       "rare and politically sensitive (tobacco is a "
                       "controversial sin-good). Status quo is the base case."),
        "long":  (".", "Long-term: GST council is dominated by compensation "
                       "and rate-rationalisation debates, not cigarette-"
                       "specific rate changes. Cigarette tax is more "
                       "driven by central excise than GST."),
    },
    ("gst_council", "RELIANCE"): {
        "short": (".", "GST is mostly passed through to consumers. When GST "
                       "is cut, demand goes up slightly. When GST is hiked, "
                       "demand falls slightly. Direct margin impact on "
                       "Reliance is minimal."),
        "mid":   (".", "Mid-term: GST rate rationalisation (e.g. removing "
                       "the 12% slab and moving everything to 18%) would be "
                       "net positive — simpler compliance, slightly higher "
                       "consumption. But this is years away."),
        "long":  (".", "Long-term: GST is a one-time tax reform. Once set, "
                       "rates don't change often. Structural growth (India's "
                       "consumption story) is the bigger Reliance driver."),
    },
    ("gst_council", "KNRCON"): {
        "short": (".", "GST on construction services is mostly passed through "
                       "to clients. Rate changes are rare and have minor "
                       "impact on KNR's revenue."),
        "mid":   (".", "Mid-term: GST Input Tax Credit (ITC) disputes affect "
                       "working capital. Refunds get stuck with the government, "
                       "increasing working-capital needs. This is a real cost "
                       "for mid-sized infra firms."),
        "long":  (".", "Long-term: GST regime is stable. Infra capex is the "
                       "structural driver. GST is a one-time reform."),
    },
    ("gst_council", "BALRAMCHIN"): {
        "short": (".", "Sugar is largely GST-EXEMPT (5% or nil). Rate changes "
                       "on sugar don't affect Balrampur's business much."),
        "mid":   (".", "Mid-term: ethanol GST (18%) vs sugar (5% or nil) is "
                       "a structural advantage for ethanol. The tax arbitrage "
                       "encourages mills to make more ethanol. This is a "
                       "subtle but real tailwind for Balrampur."),
        "long":  (".", "Long-term: GST regime is stable. The structural shift "
                       "from sugar to ethanol is the bigger story; GST "
                       "arbitrage just accelerates it."),
    },
}


# Map time horizons to short labels
HORIZON_LABELS = {
    "short": "Short term (3-6 mo)",
    "mid":   "Mid term (6-12 mo)",
    "long":  "Long term (1-3 yr)",
}
HORIZON_EMOJI = {
    "+": "🟢",  # bullish
    "-": "🔴",  # bearish
    ".": "⚪",  # neutral / context-dependent
}


def get_theme_impact(theme: str, ticker: str) -> dict | None:
    """Look up the time-horizon impact for (theme, ticker)."""
    return INDIAN_THEME_IMPACT.get((theme, ticker))


def format_theme_impact_block(
    theme: str, ticker: str, article_sentiment: str | None = None
) -> str:
    """
    Build the per-(theme, ticker) time-horizon impact block.
    Renders as: Short term / Mid term / Long term with direction
    emoji and one-line explanation.
    """
    impact = get_theme_impact(theme, ticker)
    if not impact:
        return ""
    lines = []
    for horizon in ("short", "mid", "long"):
        direction, explanation = impact[horizon]
        emoji = HORIZON_EMOJI[direction]
        label = HORIZON_LABELS[horizon]
        # Use a stable direction label
        dir_label = {"+": "positive", "-": "negative", ".": "neutral"}[direction]
        lines.append(f"    {emoji} *{label}*: {dir_label} — {explanation}")
    return "\n".join(lines)


@dataclass
class IndianThemeHit:
    """One Indian-theme match for the article."""
    theme: str           # key into INDIAN_THEMES
    theme_name: str      # human label
    signals_matched: list[str]
    affected_tickers: list[str]


def detect_indian_themes(title: str, description: str) -> list[IndianThemeHit]:
    """
    Detect Indian-context themes in the article (monsoon, local
    politics, festivals, RBI policy, commodity prices, supply chain).
    Returns a list of matched themes with the tickers they affect.
    """
    text_lower = (title + " " + description).lower()
    text_words_list = re.sub(r"\s+", " ", re.sub(r"[^a-z0-9\s]", " ", text_lower)).split()
    text_words = set(text_words_list)

    def _adjacent_match(signal: str) -> bool:
        sig_words = [w for w in re.findall(r"[a-z]+", signal) if w]
        if not sig_words or not text_words_list:
            return False
        if len(sig_words) == 1:
            if len(sig_words[0]) < 4:
                return False
            return sig_words[0] in text_words
        positions = []
        last_pos = -1
        n = len(text_words_list)
        for sw in sig_words:
            found = False
            for i in range(max(last_pos + 1, 0), n):
                if text_words_list[i] == sw:
                    positions.append(i)
                    last_pos = i
                    found = True
                    break
            if not found:
                return False
        # Multi-word signals need adjacency (<= 3 words apart)
        for i in range(1, len(positions)):
            if positions[i] - positions[i-1] > 3:
                return False
        return True

    hits: list[IndianThemeHit] = []
    for theme_key, theme in INDIAN_THEMES.items():
        matched_signals = [
            s for s in theme["signals"] if _adjacent_match(s)
        ]
        if matched_signals:
            hits.append(IndianThemeHit(
                theme=theme_key,
                theme_name=theme["name"],
                signals_matched=matched_signals,
                affected_tickers=list(theme.get("affected_tickers", [])),
            ))
    return hits


def format_indian_theme_block(
    hits: list[IndianThemeHit],
    article_title: str = "",
    article_desc: str = "",
) -> str:
    """
    Build the "Indian context" block shown in the alert message.
    Returns an empty string if no themes were detected.

    For each theme, shows:
      - the theme name and which tickers it affects
      - per-ticker time-horizon impact (Short / Mid / Long)
        with direction (positive/negative/neutral) and a one-line
        explanation of WHY the effect happens over that horizon
    """
    if not hits:
        return ""
    lines = ["🇮🇳 *Indian context:*"]
    for h in hits:
        tickers_str = ", ".join(f"*{t}*" for t in h.affected_tickers) or "all"
        sigs = ", ".join(f"`{s}`" for s in h.signals_matched[:3])
        lines.append(f"  • {h.theme_name} (matched: {sigs})")
        lines.append(f"    Affects: {tickers_str}")
        # For each affected ticker, show the time-horizon impact
        for tkr in h.affected_tickers:
            impact_block = format_theme_impact_block(
                h.theme, tkr,
            )
            if impact_block:
                lines.append(f"    📊 *{tkr}* over time:")
                lines.append(impact_block)
    return "\n".join(lines)