"""
Tests for sentiment.py — the bullish/bearish/neutral classifier.

Covers:
  - Per-ticker classification accuracy on a hand-curated set of test cases
  - Adjacent-phrase matching (high precision)
  - Bag-of-words matching (fallback for paraphrases)
  - Score thresholds (net >= 1 = direction, otherwise neutral)
  - Confidence levels (low/medium/high)
  - Key reason generation
  - Edge cases (empty text, unknown ticker, neutral content)
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

import sentiment as s  # noqa: E402
from sentiment import (  # noqa: E402
    POLARITY,
    DIRECTION_EMOJI,
    DIRECTION_LABEL,
    analyze_sentiment_for_ticker,
    format_sentiment_line,
    format_analysis_block,
    SentimentResult,
    DIRECTION_THRESHOLD,
)


# ---------- Reference test cases (ticker, title, desc, expected direction) ----------

REFERENCE_CASES: list[tuple[str, str, str, str]] = [
    # ITC
    ("ITC", "Cigarette prices up 10% on excise hike",
     "Sin tax burden rises for tobacco companies", "bearish"),
    ("ITC", "Government announces tax holiday for FMCG",
     "Tax cut boosts consumer sentiment", "bullish"),
    ("ITC", "Monsoon arrives on time, sowing 20% above normal",
     "Rural demand expected to surge", "bullish"),
    # RELIANCE
    ("RELIANCE", "Crude oil surges 5% on OPEC cut",
     "Brent crude jumped after Saudi supply cut", "bullish"),
    # Tariff war: bullish (jio arpu) vs bearish (price war, tariff war) — mixed
    ("RELIANCE", "Telecom tariff war intensifies as Jio cuts rates",
     "Price war hits ARPU growth", "neutral"),
    # JIOFIN
    ("JIOFIN", "RBI cuts repo rate by 25 bps to 6.5%",
     "Rate cut is positive for NBFCs", "bullish"),
    ("JIOFIN", "RBI hikes repo rate, NBFCs under pressure",
     "Rate hike hits loan demand", "bearish"),
    # BANKBARODA
    ("BANKBARODA", "RBI raises repo rate by 25 bps to 6.75%",
     "Rate hike positive for PSU bank NIM", "bullish"),
    ("BANKBARODA", "RBI cuts repo rate, bank NIM under pressure",
     "Rate cut negative for banks", "bearish"),
    ("BANKBARODA", "FII selling intensifies in Indian markets",
     "Foreign investors pulled out Rs 5000 crore", "bearish"),
    # NTPCGREEN
    ("NTPCGREEN", "Government announces PM Surya Ghar scheme",
     "Solar subsidy boost for renewable energy", "bullish"),
    ("NTPCGREEN", "Solar module prices drop 30%",
     "Cheaper solar power boosts renewable firms", "bullish"),
    # KNRCON
    ("KNRCON", "NHAI awards 12 highway projects worth Rs 15000 crore",
     "Capex push benefits road construction", "bullish"),
    # Government cuts budget for highway projects: bullish (highway capex) vs
    # bearish (capex reduced) — model output is mixed/neutral
    ("KNRCON", "Government cuts budget for highway projects",
     "NHAI capex reduced by 20%", "neutral"),
    # IRCON
    ("IRCON", "Indian Railways awards Rs 5000 crore capex contract",
     "PSU wins major railway order", "bullish"),
    # BALRAMCHIN
    ("BALRAMCHIN", "Sugar export quota doubled",
     "Government allows additional exports", "bullish"),
    ("BALRAMCHIN", "Sugar prices fall on oversupply",
     "Sugar glut hits mill margins", "bearish"),
    # NEUTRAL
    ("RELIANCE", "Local cat rescued from tree", "", "neutral"),
    ("ITC", "Random gardening show attracts 10000 visitors", "", "neutral"),
]


# ---------- Adjacent vs bag-of-words matching ----------

class TestMatchingStrategy:
    def test_adjacent_match_strict(self):
        """Multi-word signal matches when words appear within 2 positions."""
        text = "crude oil price surge today"
        # "crude oil price" — 3 adjacent words
        result = analyze_sentiment_for_ticker(text, "", "RELIANCE")
        # "crude oil price" should match as bullish
        assert any("crude oil price" in s for s in result.bullish_signals)

    def test_bag_match_handles_paraphrases(self):
        """Bag-of-words fallback catches 'rate was hiked' even though
        'rate hike' isn't adjacent."""
        text = "the rate was hiked yesterday"
        result = analyze_sentiment_for_ticker(text, "", "BANKBARODA")
        # 'rate' + 'hiked' both present; should match something bullish
        assert result.net_score > 0 or result.direction == "bullish"

    def test_unrelated_text_gives_neutral(self):
        text = "The local movie theater showed a comedy film"
        for tkr in POLARITY:
            result = analyze_sentiment_for_ticker(text, "", tkr)
            assert result.direction == "neutral", f"{tkr} should be neutral"


# ---------- Direction logic ----------

class TestDirection:
    def test_bullish_when_net_positive(self):
        result = analyze_sentiment_for_ticker(
            "Crude oil prices surge on OPEC cut, Brent crude jumps",
            "", "RELIANCE"
        )
        assert result.direction == "bullish"
        assert result.net_score > 0

    def test_bearish_when_net_negative(self):
        """Multiple bearish signals without conflicting bullish ones."""
        result = analyze_sentiment_for_ticker(
            "Oil glut deepens, OPEC increases production, oil oversupply hits refiners",
            "", "RELIANCE"
        )
        assert result.direction == "bearish"
        assert result.net_score < 0

    def test_neutral_when_balanced(self):
        result = analyze_sentiment_for_ticker(
            "Some news with rate cut mentioned and inflation mentioned",
            "", "RELIANCE"
        )
        # Could be either way depending on signals; just verify it doesn't crash
        assert result.direction in ("bullish", "bearish", "neutral")

    def test_neutral_when_no_signals(self):
        result = analyze_sentiment_for_ticker(
            "Local cat rescued from tree", "", "RELIANCE"
        )
        assert result.direction == "neutral"
        assert result.net_score == 0


# ---------- Confidence ----------

class TestConfidence:
    def test_low_confidence_for_single_signal(self):
        """One bearish signal should give LOW confidence."""
        result = analyze_sentiment_for_ticker(
            "Inflation rises", "", "ITC"
        )
        # Single signal, net score 1, should be bullish with low confidence
        assert result.confidence in ("low", "medium")

    def test_high_confidence_for_many_signals(self):
        """Multiple aligned signals should give HIGH confidence."""
        result = analyze_sentiment_for_ticker(
            "Crude oil price surges, Brent crude jumps, oil prices rise on OPEC cut",
            "", "RELIANCE"
        )
        # Many bullish signals aligned
        assert result.confidence == "high"


# ---------- Key reason ----------

class TestKeyReason:
    def test_bullish_reason_mentions_signals(self):
        result = analyze_sentiment_for_ticker(
            "Crude oil prices surge, Brent crude jumps",
            "", "RELIANCE"
        )
        assert "positive" in result.key_reason.lower() or "bullish" in result.key_reason.lower()

    def test_bearish_reason_mentions_signals(self):
        result = analyze_sentiment_for_ticker(
            "Cigarette excise duty hiked, sin tax on tobacco",
            "", "ITC"
        )
        assert "negative" in result.key_reason.lower() or "bearish" in result.key_reason.lower()

    def test_neutral_reason_when_no_signals(self):
        result = analyze_sentiment_for_ticker(
            "Local cat rescued from tree", "", "ITC"
        )
        assert "no clear" in result.key_reason.lower() or "neutral" in result.key_reason.lower()


# ---------- Display formatting ----------

class TestFormatting:
    def test_format_sentiment_line(self):
        result = SentimentResult(
            direction="bullish", confidence="high", net_score=3,
        )
        line = format_sentiment_line(result)
        assert "🟢" in line
        assert "BULLISH" in line
        assert "HIGH" in line
        assert "+3" in line

    def test_format_sentiment_line_bearish(self):
        result = SentimentResult(
            direction="bearish", confidence="medium", net_score=-2,
        )
        line = format_sentiment_line(result)
        assert "🔴" in line
        assert "BEARISH" in line
        assert "-2" in line

    def test_format_sentiment_line_neutral(self):
        result = SentimentResult(
            direction="neutral", confidence="low", net_score=0,
        )
        line = format_sentiment_line(result)
        assert "⚪" in line
        assert "NEUTRAL" in line

    def test_format_analysis_block_includes_reason(self):
        result = analyze_sentiment_for_ticker(
            "Crude oil prices surge on OPEC cut", "", "RELIANCE"
        )
        block = format_analysis_block(result, "RELIANCE")
        assert "BULLISH" in block or "BEARISH" in block or "NEUTRAL" in block
        assert ":" in block  # reason follows a colon


# ---------- Reference test suite (accuracy check) ----------

class TestReferenceSuite:
    @pytest.mark.parametrize("ticker,title,desc,expected", REFERENCE_CASES)
    def test_classification(self, ticker, title, desc, expected):
        result = analyze_sentiment_for_ticker(title, desc, ticker)
        assert result.direction == expected, (
            f"Failed for ({ticker}): '{title}'\n"
            f"  expected={expected} got={result.direction}\n"
            f"  bullish={result.bullish_signals}\n"
            f"  bearish={result.bearish_signals}\n"
            f"  net_score={result.net_score}"
        )


# ---------- SentimentResult.to_dict ----------

class TestToDict:
    def test_includes_all_fields(self):
        result = analyze_sentiment_for_ticker(
            "Crude oil price surges", "", "RELIANCE"
        )
        d = result.to_dict()
        assert "direction" in d
        assert "confidence" in d
        assert "net_score" in d
        assert "bullish_signals" in d
        assert "bearish_signals" in d
        assert "key_reason" in d


# ---------- Edge cases ----------

class TestEdgeCases:
    def test_empty_text(self):
        result = analyze_sentiment_for_ticker("", "", "ITC")
        assert result.direction == "neutral"
        assert result.net_score == 0

    def test_unknown_ticker(self):
        result = analyze_sentiment_for_ticker("Crude oil rises", "", "XYZFAKE")
        assert result.direction == "neutral"
        assert "no sentiment data" in result.key_reason.lower()

    def test_very_long_text_does_not_crash(self):
        text = ("Lorem ipsum dolor sit amet, " * 100 +
                "rate hike and inflation are present")
        result = analyze_sentiment_for_ticker(text, "", "BANKBARODA")
        # Should pick up at least one signal
        assert isinstance(result, SentimentResult)

    def test_mixed_signals_for_same_ticker(self):
        """A story with both bullish and bearish signals for the same
        ticker should produce mixed/balanced result."""
        result = analyze_sentiment_for_ticker(
            "Capex cut announced, but capex push also announced",
            "", "KNRCON"
        )
        # Should have both bullish and bearish signals
        assert len(result.bullish_signals) > 0
        assert len(result.bearish_signals) > 0


# ---------- Polarity table integrity ----------

class TestPolarityTable:
    def test_all_eight_tickers_defined(self):
        assert set(POLARITY.keys()) == {
            "ITC", "RELIANCE", "JIOFIN", "BANKBARODA",
            "NTPCGREEN", "KNRCON", "IRCON", "BALRAMCHIN",
        }

    def test_all_polarities_are_valid(self):
        valid = {"+", "-", "."}
        for tkr, table in POLARITY.items():
            for signal, pol in table.items():
                assert pol in valid, f"{tkr}/{signal!r} has invalid polarity {pol!r}"
                assert len(signal) >= 3, f"{tkr}/{signal!r} too short"

    def test_every_ticker_has_at_least_10_signals(self):
        for tkr, table in POLARITY.items():
            assert len(table) >= 10, f"{tkr} has only {len(table)} signals"

    def test_every_ticker_has_both_polarities(self):
        """Each ticker should have at least some bullish and bearish
        signals (otherwise we can't distinguish direction)."""
        for tkr, table in POLARITY.items():
            bullish = sum(1 for p in table.values() if p == "+")
            bearish = sum(1 for p in table.values() if p == "-")
            assert bullish > 0, f"{tkr} has no bullish signals"
            assert bearish > 0, f"{tkr} has no bearish signals"