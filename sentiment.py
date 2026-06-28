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