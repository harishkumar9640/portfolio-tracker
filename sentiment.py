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
        "affected_tickers": ["RELIANCE", "KNRCON"],
        "context": "Supply chain shocks hit input costs and export demand",
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
        "short": ("+", "Above-normal rainfall lifts rural FMCG demand "
                       "in the festive and sowing season (3-6 months)."),
        "mid":   ("+", "Sustained rural income supports volume growth and "
                       "distribution expansion into tier-3/4 markets."),
        "long":  (".", "Climate change makes monsoons more volatile — long-run "
                       "risk to agricultural input chains and rural demand."),
    },
    ("monsoon", "BALRAMCHIN"): {
        "short": ("+", "Good monsoon + timely sowing expands cane area; "
                       "sugar output rises in the next crushing season."),
        "mid":   (".", "Sugar realisation is also driven by global prices and "
                       "export policy — monsoon alone doesn't determine "
                       "mid-term margins."),
        "long":  (".", "Climate volatility increases sugar-cane yield risk over "
                       "the long term; ethanol diversification provides a "
                       "structural buffer."),
    },
    ("monsoon", "KNRCON"): {
        "short": ("+", "Good monsoon boosts rural road construction demand "
                       "(PMGSY, rural connectivity projects)."),
        "mid":   (".", "Project award timing depends on NHAI / state budgets — "
                       "monsoon improves demand but not order book pace."),
        "long":  (".", "Climate change may shift infrastructure priorities "
                       "(flood-control, irrigation) but road-construction TAM "
                       "remains large."),
    },
    # ---------- Local politics ----------
    ("local_politics", "BANKBARODA"): {
        "short": (".", "State elections rarely move PSU bank valuations "
                       "directly; brief sentiment impact on stock price."),
        "mid":   (".", "Post-election state budgets can shift MSME credit demand; "
                       "long-term PSU privatisation (if any) is the bigger "
                       "structural variable."),
        "long":  (".", "PSU bank consolidation / privatisation is a multi-year "
                       "policy question, not driven by single state elections."),
    },
    ("local_politics", "KNRCON"): {
        "short": ("+", "State-level capex push (new CM's infra agenda) can "
                       "lead to fast-track NHAI / state highway project awards."),
        "mid":   ("+", "Coalition governments that prioritise infra tend to "
                       "maintain capex momentum over a 2-3 year horizon."),
        "long":  (".", "Long-term infra spending depends on central + state "
                       "finances, not political colour. Bullish under most "
                       "regimes, neutral under severe fiscal stress."),
    },
    ("local_politics", "IRCON"): {
        "short": ("+", "New state CMs typically signal continued railway + infra "
                       "capex; existing PSU contracts get prioritised."),
        "mid":   ("+", "Central railway capex is largely insulated from state "
                       "politics; state projects tied to political stability."),
        "long":  (".", "IRCON's order book is policy-driven (railways, DFCC, "
                       "high-speed rail); long-term trajectory is a function of "
                       "central capex, not state."),
    },
    ("local_politics", "ITC"): {
        "short": ("-", "State VAT / excise hikes on tobacco / cigarettes "
                       "directly compress ITC margins in those states."),
        "mid":   ("-", "Persistent state-level tax increases compound; "
                       "pricing power is limited due to regulatory environment."),
        "long":  (".", "Diversification into FMCG non-tobacco reduces long-run "
                       "state-tax exposure, but cigarette business is "
                       "structurally challenged."),
    },
    # ---------- Festival / Wedding ----------
    ("festival_wedding", "ITC"): {
        "short": ("+", "Festive season (Diwali, Akshaya Tritiya) is the "
                       "biggest consumption quarter for cigarettes, snacks, "
                       "and discretionary FMCG."),
        "mid":   (".", "Mid-term depends on rural income (post-monsoon) — "
                       "festivals alone don't move structural demand."),
        "long":  (".", "Wedding season is a perennial demand driver; structural "
                       "shift is in the product mix (FMCG vs tobacco) rather than "
                       "festive intensity."),
    },
    # ---------- RBI / SEBI / Government Policy ----------
    ("rbi_policy", "BANKBARODA"): {
        "short": ("+", "Rate hikes widen NIM within 1-2 quarters; rate cuts "
                       "compress NIM almost immediately."),
        "mid":   ("+", "Sustained tight policy boosts treasury income and credit "
                       "growth; loose policy compresses NIM but boosts loan "
                       "volumes."),
        "long":  (".", "Long-term valuation depends on structural growth, "
                       "digital adoption, and credit-cost ratios — not policy "
                       "rate alone."),
    },
    ("rbi_policy", "JIOFIN"): {
        "short": ("+", "Rate cuts lower cost of funds; rate hikes compress "
                       "margins. NBFC P&L is highly rate-sensitive."),
        "mid":   ("+", "Easing cycle supports AUM growth and disbursement "
                       "volumes; tightening cycle slows growth but improves "
                       "asset quality."),
        "long":  (".", "Long-term moat depends on Jio's distribution scale, "
                       "digital lending penetration — not policy rates."),
    },
    ("rbi_policy", "RELIANCE"): {
        "short": (".", "Reliance is diversified; rate impact comes through "
                       "Jio (telecom capex) and retail (consumer credit demand)."),
        "mid":   (".", "Easing cycle boosts consumer credit demand for retail; "
                       "tightening cycle pressures telecom capex returns."),
        "long":  (".", "Reliance's long-term value is driven by refining margins, "
                       "Jio ARPU, and retail — policy rates are a tertiary factor."),
    },
    ("rbi_policy", "ITC"): {
        "short": (".", "Rate cuts support FMCG demand via cheaper consumer "
                       "credit; rate hikes compress it. Net effect is mild."),
        "mid":   (".", "Mid-term: rural FMCG volumes are more sensitive to "
                       "monsoon and inflation than to repo rate."),
        "long":  (".", "Long-term: macro trends (rural income, FMCG penetration) "
                       "matter more than any single rate cycle."),
    },
    # ---------- Commodity Prices ----------
    ("commodity_prices", "RELIANCE"): {
        "short": ("+", "Brent crude price increases boost GRM (refining margin) "
                       "within a quarter; oil & gas E&P also benefits."),
        "mid":   (".", "Mid-term: high crude eventually hurts marketing margins "
                       "(downstream); offset by O2C segment."),
        "long":  (".", "Long-term: energy transition reduces crude's structural "
                       "importance; renewables + green hydrogen are the long "
                       "play."),
    },
    ("commodity_prices", "BALRAMCHIN"): {
        "short": ("+", "Sugar price rise directly improves realisations; "
                       "ethanol price hike boosts margin per litre."),
        "mid":   (".", "Sugar prices are cyclical; mid-term depends on global "
                       "supply-demand balance and Indian export quota policy."),
        "long":  (".", "Long-term: ethanol blending policy and energy-transition "
                       "demand for ethanol provides structural support."),
    },
    ("commodity_prices", "KNRCON"): {
        "short": ("-", "Steel / cement / bitumen price rise directly compresses "
                       "project margins; pass-through to clients is delayed."),
        "mid":   (".", "Mid-term: input costs settle; competitive bidding "
                       "adjusts; net effect depends on order-mix and contract "
                       "structure (HAM vs EPC)."),
        "long":  (".", "Long-term: input cost volatility is the new normal; "
                       "players with scale + backward integration (steel, "
                       "cement) have an edge."),
    },
    # ---------- Global Supply Chain ----------
    ("global_supply_chain", "RELIANCE"): {
        "short": (".", "Reliance is largely self-sufficient; supply chain shocks "
                       "have minor impact (O2C imports some catalysts)."),
        "mid":   (".", "Mid-term: Jio's 5G equipment is imported; semiconductor "
                       "shortages can delay rollout and capex absorption."),
        "long":  (".", "Long-term: shift to domestic supply chain (PLI scheme) "
                       "reduces Reliance's exposure over time."),
    },
    ("global_supply_chain", "KNRCON"): {
        "short": ("-", "Container rate / shipping cost rise increases imported "
                       "equipment costs (specialised road-building machinery)."),
        "mid":   (".", "Mid-term: pass-through to NHAI in HAM/EPC contracts is "
                       "delayed; margins can compress in the interim."),
        "long":  (".", "Long-term: as domestic construction equipment "
                       "manufacturing scales, import dependence reduces."),
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