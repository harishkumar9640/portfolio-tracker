"""
Tests for pipeline.portfolio_impact.py — the portfolio-impact scanner that
cross-references news against the user's 8 holdings and sends Telegram
alerts when a story affects one of them.

Covers:
  - _find_affected_tickers: direct hits, sector hits, theme hits
  - _score_article_for_portfolio: returns is_generic_only correctly
  - _render_impact_alert: includes ticker names, scores, reasons
  - _render_generic_alert: lists all tickers when market-wide risk
  - Scan dedup: same URL is not alerted twice
  - Score threshold: low-score articles are not alerted
  - PORTFOLIO_EXPOSURE: all 8 tickers are present
  - _KEYWORD_TO_TICKERS reverse-lookup works
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

import pipeline.portfolio_impact as pi  # noqa: E402
from pipeline.portfolio_impact import (  # noqa: E402
    PORTFOLIO_EXPOSURE,
    _KEYWORD_TO_TICKERS,
    IST,
    IMPACT_LOG_FILE,
    SEEN_IMPACT_FILE,
    _find_affected_tickers,
    _score_article_for_portfolio,
    _render_impact_alert,
    _render_generic_alert,
    scan_once,
    _load_impact_seen,
    _save_impact_seen,
)


# ---------- Test article factory ----------

@dataclass
class FakeArticle:
    title: str = ""
    url: str = "https://example.com/test"
    source: str = "Test"
    published: object = None
    description: str = ""
    category: str = "economic"
    importance: int = 1


def make_article(title, description="", category="economic", url="https://test/x"):
    return FakeArticle(
        title=title, description=description, category=category, url=url,
    )


# ---------- Fixtures ----------

@pytest.fixture
def tmp_data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(pi, "IMPACT_LOG_FILE", tmp_path / "portfolio_impact_log.json")
    monkeypatch.setattr(pi, "SEEN_IMPACT_FILE", tmp_path / "portfolio_impact_seen.json")
    return tmp_path


@pytest.fixture
def clean_smtp_env(monkeypatch):
    for k in ("MF_ALERT_SMTP_HOST", "MF_ALERT_SMTP_USER", "MF_ALERT_SMTP_PASS",
              "MF_ALERT_SMTP_PORT", "MF_ALERT_TO", "MF_ALERT_FROM",
              "MF_ALERT_DRY_RUN", "NEWS_TELEGRAM_BOT_TOKEN",
              "NEWS_TELEGRAM_CHAT_ID", "NEWS_DRY_RUN", "NEWS_DISABLED"):
        monkeypatch.delenv(k, raising=False)
        monkeypatch.setenv(k, "")


# ---------- Portfolio exposure map ----------

class TestPortfolioExposure:
    def test_all_eight_tickers_present(self):
        assert set(PORTFOLIO_EXPOSURE.keys()) == {
            "ITC", "RELIANCE", "JIOFIN", "BANKBARODA",
            "NTPCGREEN", "KNRCON", "IRCON", "BALRAMCHIN",
        }

    def test_every_ticker_has_required_fields(self):
        for tkr, info in PORTFOLIO_EXPOSURE.items():
            assert "name" in info, f"{tkr} missing 'name'"
            assert "aliases" in info, f"{tkr} missing 'aliases'"
            assert "sectors" in info, f"{tkr} missing 'sectors'"
            assert "themes" in info, f"{tkr} missing 'themes'"
            assert isinstance(info["aliases"], list)
            assert isinstance(info["sectors"], list)
            assert isinstance(info["themes"], list)
            assert len(info["sectors"]) > 0, f"{tkr} has no sectors"
            assert len(info["themes"]) > 0, f"{tkr} has no themes"

    def test_keyword_reverse_lookup_built(self):
        # Every ticker should appear as a key
        for tkr in PORTFOLIO_EXPOSURE:
            assert tkr.lower() in _KEYWORD_TO_TICKERS


# ---------- _find_affected_tickers ----------

class TestFindAffectedTickers:
    def test_direct_hit_on_ticker(self):
        scores = _find_affected_tickers(
            "RELIANCE announces record profit",
            "The conglomerate posted strong numbers",
        )
        assert "RELIANCE" in scores
        assert scores["RELIANCE"] >= 5

    def test_direct_hit_on_alias(self):
        scores = _find_affected_tickers(
            "RIL to invest in retail expansion",
            "The Ambani company plans aggressive growth",
        )
        assert "RELIANCE" in scores  # RIL is an alias

    def test_sector_hit(self):
        scores = _find_affected_tickers(
            "Sugar mills face cane arrears",
            "Ethanol blending policy in focus",
        )
        assert "BALRAMCHIN" in scores
        assert scores["BALRAMCHIN"] >= 3  # sector match only

    def test_theme_hit(self):
        scores = _find_affected_tickers(
            "NHAI awards 12 highway projects",
            "Construction companies win major road contracts",
        )
        # KNRCON has both highway and road sector themes
        assert "KNRCON" in scores
        assert scores["KNRCON"] >= 2

    def test_no_match_returns_empty(self):
        scores = _find_affected_tickers(
            "Local cat rescued from tree",
            "Adorable feline saved by firefighters",
        )
        assert scores == {}

    def test_case_insensitive(self):
        scores = _find_affected_tickers(
            "ITC TO RAISE CIGARETTE PRICES",
            "TOBACCO MAJOR ANNOUNCES HIKE",
        )
        assert "ITC" in scores

    def test_short_alias_does_not_match_inside_unrelated_word(self):
        """
        Regression test for a real false positive: the BANKBARODA alias
        "bob" matched as a substring inside "Landbobank" (an unrelated
        Danish bank buying back its own shares), producing a bogus
        direct-hit alert. Whole-word matching must reject this.
        """
        scores = _find_affected_tickers(
            "Ringkj\u00f8bing Landbobank buys back shares worth DKK 21.3m",
            "",
        )
        assert "BANKBARODA" not in scores

    def test_sector_word_does_not_match_inside_unrelated_word(self):
        """
        "rail" (IRCON sector keyword) must not match inside "retail" or
        "trailer", and must not fire just because an unrelated flood
        story happens to mention train services being suspended without
        naming IRCON, Indian Railways, or a railway capex/project theme.
        """
        scores = _find_affected_tickers(
            "Retailer opens new trailer park showroom",
            "Big discounts on retail goods this festive season",
        )
        assert "IRCON" not in scores

    def test_generic_bank_word_alone_does_not_trigger_alert_threshold(self):
        """
        An unrelated foreign-bank story that only contains the generic
        word "bank" should not reach the alert threshold for BANKBARODA
        just from one sector-keyword hit (score 3 < 4).
        """
        a_scores = _find_affected_tickers(
            "Ringkj\u00f8bing Landbobank buys back shares worth DKK 21.3m",
            "A small Danish bank announced a buyback programme",
        )
        # "bank" alone is not one of BANKBARODA's sector keywords
        # ("psu bank", "public sector bank", etc. require more context),
        # so no match should occur at all for this unrelated story.
        assert a_scores.get("BANKBARODA", 0) < 4


# ---------- _score_article_for_portfolio ----------

class TestScoreArticle:
    def test_specific_hit_is_not_generic(self):
        a = make_article(
            "RELIANCE: Crude oil prices surge on OPEC cut",
            "Brent crude jumped sharply",
            category="market_risk",
        )
        impacts, is_generic = _score_article_for_portfolio(a)
        assert not is_generic
        assert any(t == "RELIANCE" for t, _, _ in impacts)

    def test_market_wide_risk_without_specific_hit_is_not_alerted(self):
        """
        Generic macro news with no direct/sector/theme keyword match must
        NOT trigger a portfolio-impact alert, even if it matches a broad
        risk category. The old behaviour synthesized an "affects all 8
        holdings" alert purely from the category, which is exactly the
        noise the user asked to eliminate: alerts must be tied to a real
        mention of a holding or its sector in the article.
        """
        a = make_article(
            "S&P 500 plunges 3% on inflation fears",
            "Broad market sell-off continues",
            category="market_risk",
        )
        impacts, is_generic = _score_article_for_portfolio(a)
        assert impacts == []
        assert is_generic is False

    def test_no_match_returns_empty(self):
        a = make_article(
            "Local gardening show attracts 10000 visitors",
            "Petunias steal the spotlight",
            category="economic",
        )
        impacts, is_generic = _score_article_for_portfolio(a)
        assert impacts == []

    def test_ircon_railway_capex(self):
        a = make_article(
            "IRCON wins Rs 2500 crore railway capex contract",
            "PSU railway order from Indian Railways",
            category="economic",
        )
        impacts, is_generic = _score_article_for_portfolio(a)
        assert not is_generic
        tickers = [t for t, _, _ in impacts]
        assert "IRCON" in tickers
        # IRCON should have high score due to multiple keyword matches
        ircon_score = next(s for t, s, _ in impacts if t == "IRCON")
        assert ircon_score >= 5

    def test_ntpcgreen_solar_policy(self):
        a = make_article(
            "PM Surya Ghar solar scheme gets 5 lakh applications",
            "Renewable energy policy boosts module demand",
            category="economic",
        )
        impacts, is_generic = _score_article_for_portfolio(a)
        assert not is_generic
        assert any(t == "NTPCGREEN" for t, _, _ in impacts)

    def test_itc_tobacco_tax(self):
        a = make_article(
            "Government hikes excise duty on cigarettes",
            "Sin tax to impact tobacco companies",
            category="economic",
        )
        impacts, is_generic = _score_article_for_portfolio(a)
        assert not is_generic
        assert any(t == "ITC" for t, _, _ in impacts)

    def test_jiofin_nbfc_regulation(self):
        a = make_article(
            "RBI tightens NBF lending norms",
            "Asset quality concerns for consumer finance firms",
            category="interest_rate",
        )
        impacts, is_generic = _score_article_for_portfolio(a)
        assert not is_generic
        assert any(t == "JIOFIN" for t, _, _ in impacts)


# ---------- _render_impact_alert ----------

class TestRenderImpactAlert:
    def test_includes_ticker_name_and_score(self):
        a = make_article(
            "RELIANCE: Crude oil surge hits refiners",
            "Brent crude jumped sharply",
            category="market_risk",
        )
        impacts, _ = _score_article_for_portfolio(a)
        msg = _render_impact_alert(a, impacts)
        assert "RELIANCE" in msg
        assert "Reliance Industries" in msg
        assert "pts" in msg
        assert "Why this matters" in msg

    def test_includes_url(self):
        a = make_article(
            "RELIANCE: Crude oil surge",
            "Brent crude jumped",
            category="market_risk",
            url="https://example.com/reliance-oil-news",
        )
        impacts, _ = _score_article_for_portfolio(a)
        msg = _render_impact_alert(a, impacts)
        assert "https://example.com/reliance-oil-news" in msg

    def test_specific_alert_does_not_list_all_tickers(self):
        """A specific-hit alert should only mention affected tickers."""
        a = make_article(
            "IRCON wins railway capex contract",
            "PSU railway order",
            category="economic",
        )
        impacts, _ = _score_article_for_portfolio(a)
        msg = _render_impact_alert(a, impacts)
        # IRCON must be present
        assert "IRCON" in msg
        # But other unrelated tickers should NOT be in this message
        assert "BALRAMCHIN" not in msg
        assert "Sugar" not in msg


# ---------- _render_generic_alert ----------

class TestRenderGenericAlert:
    def test_lists_all_tickers(self):
        a = make_article(
            "S&P 500 plunges 3%",
            "Broad market sell-off",
            category="market_risk",
        )
        msg = _render_generic_alert(a, list(PORTFOLIO_EXPOSURE.keys()))
        for tkr in PORTFOLIO_EXPOSURE:
            assert tkr in msg

    def test_includes_risk_category(self):
        a = make_article(
            "Fed raises interest rates",
            "Monetary tightening continues",
            category="interest_rate",
        )
        msg = _render_generic_alert(a, list(PORTFOLIO_EXPOSURE.keys()))
        assert "interest rate" in msg.lower()
        assert "Market-wide Alert" in msg


# ---------- Scan dedup ----------

class TestScanDedup:
    def test_seen_cache_round_trip(self, tmp_data_dir):
        _save_impact_seen({"https://a": "2026-06-27T08:00:00"})
        loaded = _load_impact_seen()
        assert "https://a" in loaded

    def test_seen_cache_expires_old(self, tmp_data_dir):
        eight_days_ago = (datetime.now() - timedelta(days=8)).isoformat()
        _save_impact_seen({
            "https://old": eight_days_ago,
            "https://recent": datetime.now().isoformat(),
        })
        loaded = _load_impact_seen()
        assert "https://old" not in loaded
        assert "https://recent" in loaded

    def test_already_seen_url_not_re_alerted(self, tmp_data_dir, clean_smtp_env):
        """Same article URL should not trigger duplicate alerts."""
        # Pre-populate seen cache with one URL
        _save_impact_seen({"https://seen": "2026-06-26T08:00:00"})
        # Build a mock news feed with one article whose URL is "seen"
        article = make_article(
            "RELIANCE: Big news",
            url="https://seen",
            category="economic",
        )
        # Patch fetch_articles in the pipeline.news_alert module (where it's defined)
        # and the helper functions inside scan_once's namespace.
        import pipeline.news_alert as na
        with patch.object(na, "fetch_articles", return_value=[article]), \
             patch.object(na, "_filter_fresh", return_value=[article]), \
             patch.object(pi, "send_telegram") as mock_send:
            result = scan_once(send=True, min_score=4)
        # No alerts sent for already-seen URL
        assert mock_send.call_count == 0
        assert result["alerts_sent"] == 0


# ---------- Integration ----------

@pytest.mark.integration
@pytest.mark.xfail(reason="Trendlyne rate limits RSS fetches; "
                          "may intermittently fail",
                   strict=False)
class TestIntegration:
    def test_real_scan_returns_dict(self, tmp_data_dir, clean_smtp_env):
        """Run a real scan against live RSS feeds."""
        # Clear seen cache so all articles are fresh
        _save_impact_seen({})
        result = scan_once(send=False)
        assert result["fetch_ok"] is True
        assert "articles_scanned" in result
        assert "alerts_sent" in result
        assert "tickers_alerted" in result

    def test_alerts_target_real_holdings(self, tmp_data_dir, clean_smtp_env):
        """If we get alerts, at least one should be for our 8 tickers."""
        _save_impact_seen({})
        result = scan_once(send=False)
        for tkr in result["tickers_alerted"]:
            assert tkr in PORTFOLIO_EXPOSURE