"""
Tests for pipeline.earnings_alert.py and pipeline.sector_mechanisms.py.

Coverage:
  - pipeline.sector_mechanisms: all 8 portfolio stocks are configured, all required
    sections present, every entry follows the [WHAT]->[WHO]->[HOW] pattern
  - pipeline.earnings_alert.render: produces concrete, mechanism-rich text for both
    T-2 and T-0 modes; never returns abstract labels
  - pipeline.earnings_alert.format: numeric and null formatting
  - pipeline.earnings_alert.alert_key: dedup keys stable
  - pipeline.earnings_alert.find_relevant_events: classifies T-2 vs T-0 correctly
  - dry-run safety: send_telegram never opens a network socket in dry-run
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

import pytest

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

import pipeline.sector_mechanisms as sm  # noqa: E402
import pipeline.earnings_alert as ea  # noqa: E402
from pipeline.earnings_alert import EarningsEvent  # noqa: E402


IST = ea.IST


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def sample_event() -> EarningsEvent:
    return EarningsEvent(
        ticker="ITC",
        company_name="ITC Limited",
        report_date=datetime(2026, 1, 20, 0, 0, tzinfo=IST),
        session="post-market",
        quarter_label="Q3 FY26",
        consensus_eps=7.40,
        consensus_revenue_cr=None,
        last_quarter_eps=7.12,
        last_quarter_revenue_cr=None,
        last_8q_eps=[7.12, 6.85, 7.40, 6.20, 5.95, 7.10, 6.80, 6.55],
    )


@pytest.fixture
def all_tickers() -> list[str]:
    return sm.list_configured_tickers()


# ===========================================================================
# 1. pipeline.sector_mechanisms — coverage
# ===========================================================================

REQUIRED_SECTIONS = [
    "sector",
    "primary_drivers",
    "watch_items",
    "results_day_history",
    "management_bellwethers",
]


class TestSectorMechanismsCoverage:
    def test_all_eight_portfolio_stocks_configured(self, all_tickers):
        """All 8 stocks from PORTFOLIO_EXPOSURE must have mechanism notes."""
        expected = {
            "ITC", "RELIANCE", "JIOFIN", "BANKBARODA",
            "NTPCGREEN", "KNRCON", "IRCON", "BALRAMCHIN",
        }
        assert set(all_tickers) == expected, (
            f"missing/extra tickers in mechanisms: "
            f"missing={expected - set(all_tickers)}, "
            f"extra={set(all_tickers) - expected}"
        )

    @pytest.mark.parametrize("ticker", [
        "ITC", "RELIANCE", "JIOFIN", "BANKBARODA",
        "NTPCGREEN", "KNRCON", "IRCON", "BALRAMCHIN",
    ])
    def test_each_entry_has_all_required_sections(self, ticker):
        m = sm.get_mechanism(ticker)
        assert m is not None, f"no mechanism for {ticker}"
        for section in REQUIRED_SECTIONS:
            assert section in m, (
                f"{ticker} missing required section '{section}'"
            )
            assert m[section], f"{ticker}.{section} is empty"

    @pytest.mark.parametrize("ticker", [
        "ITC", "RELIANCE", "JIOFIN", "BANKBARODA",
        "NTPCGREEN", "KNRCON", "IRCON", "BALRAMCHIN",
    ])
    def test_each_entry_has_enough_drivers(self, ticker):
        """At least 3 primary drivers per stock — too few = too thin."""
        m = sm.get_mechanism(ticker)
        assert len(m["primary_drivers"]) >= 3, (
            f"{ticker} has only {len(m['primary_drivers'])} primary_drivers"
        )

    @pytest.mark.parametrize("ticker", [
        "ITC", "RELIANCE", "JIOFIN", "BANKBARODA",
        "NTPCGREEN", "KNRCON", "IRCON", "BALRAMCHIN",
    ])
    def test_each_entry_has_enough_watch_items(self, ticker):
        m = sm.get_mechanism(ticker)
        assert len(m["watch_items"]) >= 3, (
            f"{ticker} has only {len(m['watch_items'])} watch_items"
        )

    @pytest.mark.parametrize("ticker", [
        "ITC", "RELIANCE", "JIOFIN", "BANKBARODA",
        "NTPCGREEN", "KNRCON", "IRCON", "BALRAMCHIN",
    ])
    def test_each_entry_has_bellwether_phrases(self, ticker):
        m = sm.get_mechanism(ticker)
        assert len(m["management_bellwethers"]) >= 2, (
            f"{ticker} has only {len(m['management_bellwethers'])} "
            f"bellwether phrases"
        )


# ===========================================================================
# 2. pipeline.sector_mechanisms — quality (concrete, not abstract)
# ===========================================================================

# Words that signal concrete mechanism (verbs + financial nouns).
# Same approach as TestExplanationQuality in test_sentiment.py.
_MECHANISM_WORDS = [
    # financial nouns
    "margin", "growth", "price", "demand", "supply", "cost", "capex",
    "revenue", "earnings", "order book", "loan", "subsidy", "tariff",
    "volume", "capacity", "yield", "spread", "credit", "asset",
    "provision", "tax", "realisation", "ebit", "nim", "gnpa", "casa",
    "subscribers", "arpu", "grm", "refining", "ethanol", "sugar",
    "monsoon", "crushing", "frp", "rbi", "msp",
    # financial metrics + concepts
    "subscriber", "subscribers", "working capital", "debt", "sale",
    "capex", "inflow", "outflow", "order", "execution", "book",
    "profit", "loss", "aum", "deposit", "borrower", "lender",
    "rate", "rates", "policy", "guidance", "outlook", "beat", "miss",
    "bid", "tender", "award", "contract", "pipeline",
    # verbs / mechanism actions
    "executed", "delivered", "commissioned", "added", "hiked", "raised",
    "passed", "flow", "compress", "expand", "increase", "decrease",
    "absorb", "recover", "miss", "beat", "disclosed", "awarded",
    "approved", "announced", "sold", "bought", "paid", "earned",
    "spent", "collected", "received", "repaid", "lent", "borrowed",
    "signed", "won", "lost", "grew", "fell", "rose", "dropped",
    "repriced", "re-priced", "maturing", "matured", "locked", "released",
    "moving", "moving up", "moving down",
]


class TestMechanismQuality:
    """Every mechanism entry must be a concrete cause-and-effect story,
    not an abstract label."""

    @pytest.mark.parametrize("ticker", [
        "ITC", "RELIANCE", "JIOFIN", "BANKBARODA",
        "NTPCGREEN", "KNRCON", "IRCON", "BALRAMCHIN",
    ])
    def test_no_just_direction_label_in_drivers(self, ticker):
        """Reject entries that are just a label like 'negative: rate hike'."""
        m = sm.get_mechanism(ticker)
        bad_pattern = re.compile(
            r"^\s*(bullish|bearish|positive|negative|neutral)\s*:",
            re.IGNORECASE,
        )
        for i, d in enumerate(m["primary_drivers"]):
            assert not bad_pattern.match(d), (
                f"{ticker} primary_drivers[{i}] is a bare label: {d!r}"
            )

    @pytest.mark.parametrize("ticker", [
        "ITC", "RELIANCE", "JIOFIN", "BANKBARODA",
        "NTPCGREEN", "KNRCON", "IRCON", "BALRAMCHIN",
    ])
    def test_no_just_direction_label_in_watch(self, ticker):
        m = sm.get_mechanism(ticker)
        bad_pattern = re.compile(
            r"^\s*(bullish|bearish|positive|negative|neutral)\s*:",
            re.IGNORECASE,
        )
        for i, w in enumerate(m["watch_items"]):
            assert not bad_pattern.match(w), (
                f"{ticker} watch_items[{i}] is a bare label: {w!r}"
            )

    @pytest.mark.parametrize("ticker", [
        "ITC", "RELIANCE", "JIOFIN", "BANKBARODA",
        "NTPCGREEN", "KNRCON", "IRCON", "BALRAMCHIN",
    ])
    def test_drivers_have_mechanism_words(self, ticker):
        """Each driver should contain at least one mechanism word —
        proves it's not a vague label."""
        m = sm.get_mechanism(ticker)
        for i, d in enumerate(m["primary_drivers"]):
            d_lower = d.lower()
            assert any(w in d_lower for w in _MECHANISM_WORDS), (
                f"{ticker} primary_drivers[{i}] has no mechanism word: "
                f"{d!r}"
            )

    @pytest.mark.parametrize("ticker", [
        "ITC", "RELIANCE", "JIOFIN", "BANKBARODA",
        "NTPCGREEN", "KNRCON", "IRCON", "BALRAMCHIN",
    ])
    def test_watch_items_have_mechanism_words(self, ticker):
        m = sm.get_mechanism(ticker)
        for i, w in enumerate(m["watch_items"]):
            w_lower = w.lower()
            assert any(word in w_lower for word in _MECHANISM_WORDS), (
                f"{ticker} watch_items[{i}] has no mechanism word: {w!r}"
            )

    @pytest.mark.parametrize("ticker", [
        "ITC", "RELIANCE", "JIOFIN", "BANKBARODA",
        "NTPCGREEN", "KNRCON", "IRCON", "BALRAMCHIN",
    ])
    def test_drivers_are_minimum_length(self, ticker):
        """A real explanation is > 60 chars. Anything shorter is a label."""
        m = sm.get_mechanism(ticker)
        for i, d in enumerate(m["primary_drivers"]):
            assert len(d) >= 60, (
                f"{ticker} primary_drivers[{i}] too short ({len(d)} chars): "
                f"{d!r}"
            )

    @pytest.mark.parametrize("ticker", [
        "ITC", "RELIANCE", "JIOFIN", "BANKBARODA",
        "NTPCGREEN", "KNRCON", "IRCON", "BALRAMCHIN",
    ])
    def test_results_day_history_is_specific(self, ticker):
        """History should mention % moves (a number) — proves it's not vague."""
        m = sm.get_mechanism(ticker)
        hist = m["results_day_history"]
        # must contain at least one number (like "3-5%" or "2.1%")
        assert re.search(r"\d", hist), (
            f"{ticker} results_day_history has no number: {hist!r}"
        )

    @pytest.mark.parametrize("ticker", [
        "ITC", "RELIANCE", "JIOFIN", "BANKBARODA",
        "NTPCGREEN", "KNRCON", "IRCON", "BALRAMCHIN",
    ])
    def test_bellwether_phrases_use_quotation_marks(self, ticker):
        """Bellwethers should be in quotes — they're literal phrases."""
        m = sm.get_mechanism(ticker)
        for i, b in enumerate(m["management_bellwethers"]):
            assert ("'" in b) or ('"' in b) or ("’" in b) or ("‘" in b), (
                f"{ticker} bellwether[{i}] not in quotes: {b!r}"
            )


# ===========================================================================
# 3. pipeline.earnings_alert.render — formatting helpers
# ===========================================================================

class TestFormattingHelpers:
    def test_fmt_eps_with_value(self):
        assert ea._fmt_eps(7.40) == "₹7.40"
        assert ea._fmt_eps(0.0) == "₹0.00"

    def test_fmt_eps_with_none(self):
        assert ea._fmt_eps(None) == "N/A"

    def test_fmt_revenue_under_1000(self):
        assert ea._fmt_revenue(500) == "₹500 cr"

    def test_fmt_revenue_over_1000(self):
        out = ea._fmt_revenue(2000)
        assert "lakh cr" in out
        assert "2" in out

    def test_fmt_revenue_none(self):
        assert ea._fmt_revenue(None) == "N/A"

    def test_escape_md_escapes_special_chars(self):
        escaped = ea._escape_md("ITC Ltd. (post-market) — earnings *watch*")
        # Telegram MarkdownV2 special chars must be escaped
        assert "\\." in escaped
        assert "\\(" in escaped
        assert "\\)" in escaped


# ===========================================================================
# 4. pipeline.earnings_alert.render_alert — both modes
# ===========================================================================

class TestRenderAlert:
    def test_T2_alert_contains_consensus(self, sample_event):
        text = ea.render_alert(sample_event, mode="T-2")
        assert "₹7.40" in text   # consensus_eps
        assert "ITC Limited" in text
        assert "Q3 FY26" in text

    def test_T2_alert_contains_heads_up_marker(self, sample_event):
        text = ea.render_alert(sample_event, mode="T-2")
        assert "2 DAYS TO RESULTS" in text

    def test_T0_alert_contains_today_marker(self, sample_event):
        text = ea.render_alert(sample_event, mode="T-0")
        assert "RESULTS DAY" in text
        assert "TODAY" in text

    def test_T0_alert_includes_watch_items(self, sample_event):
        text = ea.render_alert(sample_event, mode="T-0")
        assert "con-call" in text.lower() or "concall" in text.lower()

    def test_T0_alert_includes_bellwethers(self, sample_event):
        text = ea.render_alert(sample_event, mode="T-0")
        # bellwethers are wrapped in quotes, so quotes should appear
        assert "'" in text or '"' in text

    def test_T2_alert_includes_results_day_history(self, sample_event):
        text = ea.render_alert(sample_event, mode="T-2")
        # history contains numbers (% moves)
        assert re.search(r"\d", text)

    def test_alert_handles_missing_consensus(self):
        ev = EarningsEvent(
            ticker="ITC",
            company_name="ITC Limited",
            report_date=datetime(2026, 1, 20, tzinfo=IST),
            session="post-market",
            quarter_label="Q3 FY26",
        )
        text = ea.render_alert(ev, mode="T-0")
        assert "N/A" in text  # consensus missing -> rendered as N/A
        assert "ITC Limited" in text

    def test_alert_without_mechanism_still_renders(self):
        """Stock not in mechanism dict must still produce a usable alert."""
        ev = EarningsEvent(
            ticker="UNKNOWNSTOCK",
            company_name="Unknown Stock",
            report_date=datetime(2026, 1, 20, tzinfo=IST),
            session="post-market",
            quarter_label="Q3 FY26",
        )
        text = ea.render_alert(ev, mode="T-0")
        assert "Unknown Stock" in text
        assert "Q3 FY26" in text

    def test_alert_includes_sector_label(self, sample_event):
        text = ea.render_alert(sample_event, mode="T-2")
        assert "FMCG" in text or "Tobacco" in text


# ===========================================================================
# 5. alert_key — dedup stability
# ===========================================================================

class TestAlertKey:
    def test_key_format(self):
        d = datetime(2026, 1, 20, tzinfo=IST)
        assert ea.alert_key("ITC", d, "T-2") == "ITC|2026-01-20|T-2"
        assert ea.alert_key("ITC", d, "T-0") == "ITC|2026-01-20|T-0"

    def test_same_key_for_same_event(self):
        d = datetime(2026, 1, 20, 14, 30, tzinfo=IST)
        assert ea.alert_key("ITC", d, "T-2") == ea.alert_key(
            "ITC", d.replace(hour=9), "T-2"
        )


# ===========================================================================
# 6. find_relevant_events — T-2 vs T-0 classification
# ===========================================================================

class TestFindRelevantEvents:
    def test_classifies_T2_correctly(self, monkeypatch):
        """An event 2 days from 'today' should be tagged T-2."""
        today = datetime(2026, 1, 18, tzinfo=IST)
        ev_date = datetime(2026, 1, 20, tzinfo=IST)

        def fake_fetch(tickers, start, end):
            return [EarningsEvent(
                ticker="ITC", company_name="ITC Limited",
                report_date=ev_date, session="post-market",
                quarter_label="Q3 FY26",
            )]

        monkeypatch.setattr(ea, "fetch_nse_results", fake_fetch)
        pairs = ea.find_relevant_events(["ITC"], today)
        assert len(pairs) == 1
        mode, ev = pairs[0]
        assert mode == "T-2"
        assert ev.ticker == "ITC"

    def test_classifies_T0_correctly(self, monkeypatch):
        today = datetime(2026, 1, 20, tzinfo=IST)

        def fake_fetch(tickers, start, end):
            return [EarningsEvent(
                ticker="RELIANCE", company_name="Reliance Industries",
                report_date=today, session="post-market",
                quarter_label="Q3 FY26",
            )]

        monkeypatch.setattr(ea, "fetch_nse_results", fake_fetch)
        pairs = ea.find_relevant_events(["RELIANCE"], today)
        assert len(pairs) == 1
        mode, ev = pairs[0]
        assert mode == "T-0"

    def test_skips_T1_and_T_plus_1(self, monkeypatch):
        """T-1 (too late for heads-up) and T+1 (post-results) are skipped."""
        today = datetime(2026, 1, 20, tzinfo=IST)

        def fake_fetch(tickers, start, end):
            return [
                EarningsEvent(ticker="ITC", company_name="ITC",
                              report_date=today - timedelta(days=1),
                              session="post-market",
                              quarter_label="Q3 FY26"),
                EarningsEvent(ticker="RELIANCE", company_name="Reliance",
                              report_date=today + timedelta(days=1),
                              session="post-market",
                              quarter_label="Q3 FY26"),
            ]

        monkeypatch.setattr(ea, "fetch_nse_results", fake_fetch)
        pairs = ea.find_relevant_events(["ITC", "RELIANCE"], today)
        assert pairs == []


# ===========================================================================
# 7. run_once — dedup + enrichment wiring
# =========================================================================--

class TestRunOnce:
    def test_dedup_skips_already_seen(self, monkeypatch, tmp_path):
        """If alert_key is already in seen map, run_once should skip it."""
        # Point SEEN_FILE at a tmp file so we don't pollute real data
        seen_file = tmp_path / "seen.json"
        seen_file.write_text(json.dumps({
            "ITC|2026-01-20|T-2": "2026-01-18",
        }))
        monkeypatch.setattr(ea, "SEEN_FILE", seen_file)

        # find_relevant_events returns one candidate
        ev = EarningsEvent(
            ticker="ITC", company_name="ITC Limited",
            report_date=datetime(2026, 1, 20, tzinfo=IST),
            session="post-market", quarter_label="Q3 FY26",
        )
        monkeypatch.setattr(ea, "find_relevant_events",
                            lambda tickers, today: [("T-2", ev)])
        # enrich should not even be called (because we skip)
        monkeypatch.setattr(ea, "enrich_event",
                            lambda e: pytest.fail("should not enrich"))
        # send_telegram should not be called either
        monkeypatch.setattr(ea, "send_telegram",
                            lambda t: pytest.fail("should not send"))

        result = ea.run_once(today=datetime(2026, 1, 18, tzinfo=IST))
        assert result["skipped"] == 1
        assert result["sent"] == 0

    def test_force_send_bypasses_dedup(self, monkeypatch, tmp_path):
        seen_file = tmp_path / "seen.json"
        seen_file.write_text(json.dumps({
            "ITC|2026-01-20|T-2": "2026-01-18",
        }))
        monkeypatch.setattr(ea, "SEEN_FILE", seen_file)

        ev = EarningsEvent(
            ticker="ITC", company_name="ITC Limited",
            report_date=datetime(2026, 1, 20, tzinfo=IST),
            session="post-market", quarter_label="Q3 FY26",
        )
        monkeypatch.setattr(ea, "find_relevant_events",
                            lambda tickers, today: [("T-2", ev)])
        monkeypatch.setattr(ea, "enrich_event", lambda e: e)
        monkeypatch.setattr(ea, "send_telegram",
                            lambda t: {"sent": True, "mode": "dry_run",
                                       "chars": len(t)})

        result = ea.run_once(
            today=datetime(2026, 1, 18, tzinfo=IST), force_send=True,
        )
        assert result["sent"] == 1
        assert result["skipped"] == 0


# ===========================================================================
# 8. send_telegram — dry-run safety
# ===========================================================================

class TestSendTelegramDryRun:
    def test_dry_run_does_not_open_network(self, monkeypatch):
        """In dry-run mode, send_telegram must not call urlopen."""
        monkeypatch.setenv("EARNINGS_ALERT_DRY_RUN", "1")
        assert ea.is_dry_run() is True

        def fail_urlopen(*args, **kwargs):
            raise AssertionError(
                "urlopen should not be called in dry-run mode"
            )

        with mock.patch.object(
            ea.urllib.request, "urlopen", side_effect=fail_urlopen
        ):
            result = ea.send_telegram("test message")
        assert result["sent"] is False
        assert result["mode"] == "dry_run"

    def test_missing_credentials_returns_no_creds(self, monkeypatch):
        """Without Telegram creds + dry-run off, returns no_credentials."""
        monkeypatch.setenv("EARNINGS_ALERT_DRY_RUN", "0")
        monkeypatch.delenv("NEWS_TELEGRAM_BOT_TOKEN", raising=False)
        monkeypatch.delenv("NEWS_TELEGRAM_CHAT_ID", raising=False)
        monkeypatch.delenv("EARNINGS_TELEGRAM_BOT_TOKEN", raising=False)
        monkeypatch.delenv("EARNINGS_TELEGRAM_CHAT_ID", raising=False)

        def fail_urlopen(*args, **kwargs):
            raise AssertionError("urlopen should not be called")

        with mock.patch.object(
            ea.urllib.request, "urlopen", side_effect=fail_urlopen
        ):
            result = ea.send_telegram("test message")
        assert result["sent"] is False
        assert result["mode"] == "no_credentials"


# ===========================================================================
# 10. fetch_nse_results — parse, match, dedup (with mocked HTTP)
# ===========================================================================

class TestFetchNseParseRow:
    """Unit tests for the announcement-to-EarningsEvent mapping."""

    def _names(self):
        # Mirror what fetch_nse_results builds from PORTFOLIO_EXPOSURE:
        # includes the official name + all aliases + the ticker itself.
        return {
            "ITC": ["itc limited", "itc ltd", "india tobacco", "i.t.c.", "itc"],
            "RELIANCE": ["reliance industries", "ril", "reliance jio",
                         "reliance retail", "jio platforms", "ambani",
                         "mukesh ambani", "reliance"],
        }

    def _parse(self, row, tickers):
        name_by = {t: names for t, names in self._names().items()
                   if t in tickers}
        return ea._parse_nse_row(row, name_by, tickers)

    def test_matches_exact_symbol(self):
        row = {
            "symbol": "ITC", "comp": "ITC Limited",
            "desc": "Financial Result Updates",
            "attchmntText": "ITC Limited has submitted financial results...",
            "an_dt": "20-May-2025 17:30:00",
        }
        ev = self._parse(row, ["ITC"])
        assert ev is not None
        assert ev.ticker == "ITC"
        assert ev.quarter_label == "Q4 FY25"
        assert ev.session == "post-market"

    def test_rejects_non_results_announcement(self):
        row = {
            "symbol": "ITC", "comp": "ITC Limited",
            "desc": "Loss of Share Certificates",
            "attchmntText": "ITC Limited has reported loss of share certificates...",
            "an_dt": "20-May-2025",
        }
        assert self._parse(row, ["ITC"]) is None

    def test_accepts_outcome_of_board_meeting_with_results_text(self):
        row = {
            "symbol": "ITC", "comp": "ITC Limited",
            "desc": "Outcome of Board Meeting",
            "attchmntText": "ITC Limited has submitted financial results for Q4 FY25",
            "an_dt": "20-May-2025 18:00:00",
        }
        ev = self._parse(row, ["ITC"])
        assert ev is not None
        assert ev.ticker == "ITC"

    def test_rejects_outcome_of_board_meeting_without_results_text(self):
        """A board meeting about something other than results shouldn't match."""
        row = {
            "symbol": "ITC", "comp": "ITC Limited",
            "desc": "Outcome of Board Meeting",
            "attchmntText": "The Board approved the appointment of a new director.",
            "an_dt": "20-May-2025",
        }
        assert self._parse(row, ["ITC"]) is None

    def test_does_not_match_reliance_home_finance_as_reliance(self):
        """Substring matching must NOT confuse Reliance Industries with
        Reliance Home Finance / Reliance Infrastructure / etc."""
        row = {
            "symbol": "RELIANCEHOME", "comp": "Reliance Home Finance Limited",
            "desc": "Financial Result Updates",
            "attchmntText": (
                "Reliance Home Finance Limited has submitted to the "
                "Exchange, the financial results for the period ended "
                "March 31, 2025."
            ),
            "an_dt": "22-May-2025",
        }
        # Only "RELIANCE" (the symbol) is in our ticker list, but the
        # row's symbol is "RELIANCEHOME" so it should not match.
        ev = self._parse(row, ["RELIANCE"])
        assert ev is None

    def test_matches_reliance_industries_correctly(self):
        row = {
            "symbol": "RELIANCE", "comp": "Reliance Industries Limited",
            "desc": "Financial Result Updates",
            "attchmntText": (
                "Reliance Industries Limited has submitted to the Exchange, "
                "the financial results for the period ended September 30, 2024."
            ),
            "an_dt": "14-Oct-2024 19:02:31",
        }
        ev = self._parse(row, ["RELIANCE"])
        assert ev is not None
        assert ev.ticker == "RELIANCE"
        assert ev.quarter_label == "Q2 FY25"

    def test_quarter_label_for_october(self):
        """Q2 FY is Jul-Sep, reported Oct-Dec. Indian FY year = year it ENDS."""
        row = {
            "symbol": "ITC", "comp": "ITC Limited",
            "desc": "Financial Result Updates",
            "attchmntText": "ITC Limited has submitted financial results...",
            "an_dt": "14-Oct-2024 17:00:00",
        }
        ev = self._parse(row, ["ITC"])
        assert ev.quarter_label == "Q2 FY25"

    def test_quarter_label_for_january(self):
        """Q3 FY is Oct-Dec, reported Jan-Mar."""
        row = {
            "symbol": "ITC", "comp": "ITC Limited",
            "desc": "Financial Result Updates",
            "attchmntText": "ITC Limited has submitted financial results...",
            "an_dt": "20-Jan-2026 17:00:00",
        }
        ev = self._parse(row, ["ITC"])
        assert ev.quarter_label == "Q3 FY26"

    def test_quarter_label_for_may(self):
        """Q4 FY is Jan-Mar, reported Apr-Jun."""
        row = {
            "symbol": "ITC", "comp": "ITC Limited",
            "desc": "Financial Result Updates",
            "attchmntText": "ITC Limited has submitted financial results...",
            "an_dt": "22-May-2025 17:00:00",
        }
        ev = self._parse(row, ["ITC"])
        assert ev.quarter_label == "Q4 FY25"


class TestFetchNseDedup:
    """NSE often publishes the same result twice (once as a summary, once
    as the PDF). The fetcher must dedup by (ticker, date)."""

    def test_dedupes_same_ticker_same_date(self, monkeypatch):
        rows = [
            {"symbol": "ITC", "comp": "ITC Limited",
             "desc": "Financial Result Updates",
             "attchmntText": "ITC Limited submitted financial results...",
             "an_dt": "22-May-2025 17:30:00"},
            {"symbol": "ITC", "comp": "ITC Limited",
             "desc": "Financial Result Updates",
             "attchmntText": "ITC Limited submitted financial results...",
             "an_dt": "22-May-2025 19:00:00"},
        ]

        def fake_http(url, params=None):
            return json.dumps(rows)

        monkeypatch.setattr(ea, "_http_get_with_cookies", fake_http)

        events = ea.fetch_nse_results(
            ["ITC"],
            datetime(2025, 5, 22, tzinfo=IST),
            datetime(2025, 5, 22, tzinfo=IST),
        )
        assert len(events) == 1
        assert events[0].ticker == "ITC"

    def test_keeps_different_dates_for_same_ticker(self, monkeypatch):
        """RELIANCE legitimately files standalone + consolidated on
        different dates — keep both."""
        rows = [
            {"symbol": "RELIANCE", "comp": "Reliance Industries Limited",
             "desc": "Financial Result Updates",
             "attchmntText": "Reliance Industries Limited submitted financial results...",
             "an_dt": "14-Oct-2024 17:00:00"},
            {"symbol": "RELIANCE", "comp": "Reliance Industries Limited",
             "desc": "Financial Result Updates",
             "attchmntText": "Reliance Industries Limited submitted consolidated financial results...",
             "an_dt": "17-Oct-2024 17:00:00"},
        ]

        def fake_http(url, params=None):
            return json.dumps(rows)

        monkeypatch.setattr(ea, "_http_get_with_cookies", fake_http)

        events = ea.fetch_nse_results(
            ["RELIANCE"],
            datetime(2024, 10, 14, tzinfo=IST),
            datetime(2024, 10, 17, tzinfo=IST),
        )
        assert len(events) == 2


class TestFetchNseEmptyAndErrors:
    """Empty responses and network errors must degrade gracefully."""

    def test_empty_array_returns_empty_list(self, monkeypatch):
        monkeypatch.setattr(ea, "_http_get_with_cookies",
                            lambda url, params=None: "[]")
        monkeypatch.setattr(ea, "fetch_nse_via_playwright", lambda *a: [])
        events = ea.fetch_nse_results(
            ["ITC"],
            datetime(2025, 5, 22, tzinfo=IST),
            datetime(2025, 5, 22, tzinfo=IST),
        )
        assert events == []

    def test_http_error_returns_empty_list(self, monkeypatch):
        def boom(url, params=None):
            raise ea.urllib.error.HTTPError(
                url, 403, "Forbidden", {}, None,
            )
        monkeypatch.setattr(ea, "_http_get_with_cookies", boom)
        monkeypatch.setattr(ea, "fetch_nse_via_playwright", lambda *a: [])
        events = ea.fetch_nse_results(
            ["ITC"],
            datetime(2025, 5, 22, tzinfo=IST),
            datetime(2025, 5, 22, tzinfo=IST),
        )
        assert events == []

    def test_falls_back_to_playwright_when_urllib_returns_empty(self, monkeypatch):
        """If urllib gets blocked and returns [], we should try Playwright."""
        monkeypatch.setattr(ea, "_http_get_with_cookies",
                            lambda url, params=None: "[]")
        captured = {}
        def fake_pw(from_date, to_date):
            captured["called"] = True
            return [{"symbol": "ITC", "comp": "ITC Limited",
                     "desc": "Financial Result Updates",
                     "attchmntText": "ITC Limited submitted financial results...",
                     "an_dt": "22-May-2025 17:30:00"}]
        monkeypatch.setattr(ea, "fetch_nse_via_playwright", fake_pw)

        events = ea.fetch_nse_results(
            ["ITC"],
            datetime(2025, 5, 22, tzinfo=IST),
            datetime(2025, 5, 22, tzinfo=IST),
        )
        assert captured.get("called") is True
        assert len(events) == 1


class TestPlaywrightAvailable:
    """Detect whether Playwright is installed (for graceful degradation)."""

    def test_returns_bool(self):
        result = ea.playwright_available()
        assert isinstance(result, bool)

    def test_returns_true_when_installed(self):
        # playwright IS installed in this venv (we proved it above)
        assert ea.playwright_available() is True


# ===========================================================================
# 11. Live integration test (skipped by default; run with --run-live)
# ===========================================================================

@pytest.mark.skip(reason="live NSE fetch; run manually with --runlive")
class TestLiveNseFetch:
    def test_live_q2_fy25_results(self):
        """Verify the real NSE pipeline finds ITC/RELIANCE in Oct 2024."""
        events = ea.fetch_nse_results(
            ["ITC", "RELIANCE", "JIOFIN"],
            datetime(2024, 10, 10, tzinfo=IST),
            datetime(2024, 10, 25, tzinfo=IST),
        )
        tickers_found = {e.ticker for e in events}
        assert "RELIANCE" in tickers_found
        assert "JIOFIN" in tickers_found


# ===========================================================================
# 11. CLI smoke tests
# ===========================================================================

class TestCLI:
    def test_test_render_for_known_ticker(self, capsys, sample_event):
        """--test-render ITC should print both T-2 and T-0 alert bodies."""
        import subprocess
        proc = subprocess.run(
            [sys.executable, "-m", "pipeline.earnings_alert", "--test-render", "ITC"],
            capture_output=True, text=True, cwd=PROJECT,
        )
        assert proc.returncode == 0
        assert "2 DAYS TO RESULTS" in proc.stdout
        assert "RESULTS DAY" in proc.stdout
        assert "ITC Limited" in proc.stdout

    def test_test_render_unknown_ticker_exits_nonzero(self):
        import subprocess
        proc = subprocess.run(
            [sys.executable, "-m", "pipeline.earnings_alert", "--test-render", "NOPE"],
            capture_output=True, text=True, cwd=PROJECT,
        )
        assert proc.returncode != 0