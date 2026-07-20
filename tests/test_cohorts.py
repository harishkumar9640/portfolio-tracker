"""Tests for pipeline.cohorts (cohort-based CAGR comparison)."""
from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path
from unittest.mock import patch

import pytest

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

from pipeline import cohorts as cohorts_mod
from pipeline.cohorts import (
    COHORTS,
    _in_cohort_large_cap,
    _in_cohort_midcap,
    _in_cohort_smallcap,
    _is_longterm,
    _resolve_benchmark,
    compute_cohorts,
)
from pipeline.ledger import LedgerEntry, _is_etf
from pipeline.index_data import available_indices


# ---------- Filter predicates ----------

class TestCohortFilters:
    def _entry(self, ticker="X", tier="large", is_etf=False, buy_date=None,
               sell_date=None):
        return LedgerEntry(
            ticker=ticker,
            buy_date=buy_date or date(2024, 6, 1),
            sell_date=sell_date,
            qty=10,
            buy_price=100.0,
            buy_value=1000.0,
            tier=tier,
            is_etf=is_etf,
        )

    def test_large_cap_includes_all_large_tier(self):
        assert _in_cohort_large_cap(self._entry("RELIANCE", "large")) is True
        assert _in_cohort_large_cap(self._entry("HDFCBANK", "large")) is True

    def test_large_cap_excludes_mid_small_etf(self):
        assert _in_cohort_large_cap(self._entry("UNOMINDA", "mid")) is False
        assert _in_cohort_large_cap(self._entry("KNRCON", "small")) is False
        assert _in_cohort_large_cap(self._entry("GOLDBEES", "large", is_etf=True)) is False

    def test_midcap_only_mid(self):
        assert _in_cohort_midcap(self._entry("UNOMINDA", "mid")) is True
        assert _in_cohort_midcap(self._entry("RELIANCE", "large")) is False
        assert _in_cohort_midcap(self._entry("KNRCON", "small")) is False
        assert _in_cohort_midcap(self._entry("GOLDBEES", "mid", is_etf=True)) is False

    def test_smallcap_only_small(self):
        assert _in_cohort_smallcap(self._entry("KNRCON", "small")) is True
        assert _in_cohort_smallcap(self._entry("RELIANCE", "large")) is False
        assert _in_cohort_smallcap(self._entry("UNOMINDA", "mid")) is False
        assert _in_cohort_smallcap(self._entry("GOLDBEES", "small", is_etf=True)) is False

    def test_is_longterm(self):
        today = date(2026, 7, 7)
        # Bought 2 years ago, still open -> LT
        e = self._entry("X", "large", buy_date=date(2024, 1, 1))
        assert _is_longterm(e, today) is True
        # Bought 6 months ago, still open -> ST
        e = self._entry("X", "large", buy_date=date(2026, 1, 1))
        assert _is_longterm(e, today) is False
        # Bought 2 years ago, sold after 1.5 years -> LT
        e = self._entry("X", "large", buy_date=date(2024, 1, 1), sell_date=date(2025, 7, 1))
        assert _is_longterm(e, today) is True
        # Bought and sold within 6 months -> ST
        e = self._entry("X", "large", buy_date=date(2026, 1, 1), sell_date=date(2026, 5, 1))
        assert _is_longterm(e, today) is False

    def test_cohort_definitions(self):
        # large_cap_equity has longterm_only=True per user request
        assert COHORTS["large_cap_equity"]["longterm_only"] is True
        assert COHORTS["midcap_equity"]["longterm_only"] is False
        assert COHORTS["smallcap_equity"]["longterm_only"] is False
        # Benchmark mapping
        assert COHORTS["large_cap_equity"]["benchmark"] == "nifty50"
        assert COHORTS["midcap_equity"]["benchmark"] == "nifty_midcap_150"
        assert COHORTS["smallcap_equity"]["benchmark"] == "nifty_smallcap_250"


# ---------- Benchmark resolution ----------

class TestResolveBenchmark:
    def test_native_benchmark_when_available(self):
        # Pretend both indices have data
        with patch.object(cohorts_mod, "is_available", return_value=True):
            name, avail, used_fb = _resolve_benchmark("nifty_midcap_150")
            assert name == "nifty_midcap_150"
            assert avail is True
            assert used_fb is False

    def test_fallback_to_nifty50(self):
        # Midcap not available, Nifty50 is
        def fake_avail(name):
            return name == "nifty50"
        with patch.object(cohorts_mod, "is_available", side_effect=fake_avail):
            name, avail, used_fb = _resolve_benchmark("nifty_midcap_150")
            assert name == "nifty50"
            assert avail is True
            assert used_fb is True

    def test_no_benchmark_available(self):
        with patch.object(cohorts_mod, "is_available", return_value=False):
            name, avail, used_fb = _resolve_benchmark("nifty_midcap_150")
            assert name == "nifty_midcap_150"
            assert avail is False


# ---------- compute_cohorts (full pipeline) ----------

class TestComputeCohorts:
    """Test the full cohort computation with mocked ledger + LTPs."""

    def _make_entry(self, ticker, tier, qty=10, buy_price=100.0,
                    buy_date=None, sell_date=None, sell_price=120.0,
                    is_etf=False):
        """Build a LedgerEntry for testing."""
        bd = buy_date or date(2024, 6, 1)
        sd = sell_date
        return LedgerEntry(
            ticker=ticker,
            buy_date=bd,
            sell_date=sd,
            qty=qty,
            buy_price=buy_price,
            sell_price=sell_price,
            buy_value=qty * buy_price,
            sell_value=qty * sell_price if sd else 0.0,
            pnl=(qty * (sell_price - buy_price)) if sd else 0.0,
            tier=tier,
            is_etf=is_etf,
        )

    def test_empty_ledger(self):
        with patch.object(cohorts_mod, "build_ledger", return_value=[]):
            result = compute_cohorts(current_ltp_fn=lambda t: 100.0)
        for cname in ["large_cap_equity", "midcap_equity", "smallcap_equity"]:
            c = result[cname]
            assert c["n_lots"] == 0
            assert c["total_invested"] == 0
            assert c["cagr_pct"] is None

    def test_large_cap_aggregate_excludes_short_term_by_default(self):
        # The large_cap_equity cohort has longterm_only=True, so a stock
        # bought < 1y ago (sell_date None) should be excluded.
        today = date.today()
        # 2 long-term large-cap positions (bought > 1y ago)
        # 1 short-term large-cap position (bought < 1y ago, should be excluded)
        entries = [
            self._make_entry("RELIANCE", "large", qty=10, buy_price=100,
                             buy_date=date(today.year - 2, 1, 1)),  # LT (open > 1y)
            self._make_entry("HDFCBANK", "large", qty=5, buy_price=200,
                             buy_date=date(today.year - 3, 1, 1),
                             sell_date=date(today.year - 1, 1, 1),
                             sell_price=180),  # LT (held 2 years, sold)
            self._make_entry("BANKBARODA", "large", qty=10, buy_price=200,
                             buy_date=date(today.year, 1, 15)),  # ST - should be excluded
            self._make_entry("GOLDBEES", "large", qty=100, buy_price=80,
                             buy_date=date(today.year - 2, 1, 1),
                             is_etf=True),  # ETF - should be excluded
        ]
        ltp_fn = lambda t: {"RELIANCE": 110.0, "HDFCBANK": 180.0,
                            "BANKBARODA": 220.0, "GOLDBEES": 115.0}.get(t)
        with patch.object(cohorts_mod, "build_ledger", return_value=entries), \
             patch.object(cohorts_mod, "_resolve_benchmark",
                          return_value=("nifty50", True, False)), \
             patch.object(cohorts_mod, "close_on",
                          side_effect=lambda n, d: 20000.0):
            result = compute_cohorts(current_ltp_fn=ltp_fn)
        c = result["large_cap_equity"]
        # BANKBARODA (ST) and GOLDBEES (ETF) should be excluded
        assert "RELIANCE" in c["tickers"]
        assert "HDFCBANK" in c["tickers"]
        assert "BANKBARODA" not in c["tickers"]
        assert "GOLDBEES" not in c["tickers"]
        assert c["n_tickers"] == 2

    def test_midcap_only_contains_mid_tier(self):
        entries = [
            self._make_entry("RELIANCE", "large", qty=10, buy_price=100),
            self._make_entry("UNOMINDA", "mid", qty=8, buy_price=500),
            self._make_entry("KNRCON", "small", qty=20, buy_price=100),
        ]
        with patch.object(cohorts_mod, "build_ledger", return_value=entries), \
             patch.object(cohorts_mod, "_resolve_benchmark",
                          return_value=("nifty50", True, False)):
            result = compute_cohorts(current_ltp_fn=lambda t: 100.0)
        c = result["midcap_equity"]
        assert c["tickers"] == ["UNOMINDA"]
        assert c["n_tickers"] == 1

    def test_smallcap_aggregate_includes_all_history(self):
        # Small-cap cohort has longterm_only=False, so short-term lots are included
        today = date.today()
        entries = [
            self._make_entry("KNRCON", "small", qty=20, buy_price=100,
                             buy_date=date(today.year - 2, 1, 1)),  # LT
            self._make_entry("ASTRAMICRO", "small", qty=10, buy_price=200,
                             buy_date=date(today.year, 1, 15)),  # ST - included!
        ]
        with patch.object(cohorts_mod, "build_ledger", return_value=entries), \
             patch.object(cohorts_mod, "_resolve_benchmark",
                          return_value=("nifty50", True, False)), \
             patch.object(cohorts_mod, "close_on",
                          side_effect=lambda n, d: 10000.0):
            result = compute_cohorts(current_ltp_fn=lambda t: 100.0)
        c = result["smallcap_equity"]
        assert "KNRCON" in c["tickers"]
        assert "ASTRAMICRO" in c["tickers"]
        assert c["n_tickers"] == 2

    def test_alpha_calculated_correctly(self):
        # 1 lot bought 2024-01-01, current LTP=110 (10% return),
        # benchmark: 100 -> 105 over same window (5% return).
        # Years from 2024-01-01 to today (~2.5y).
        # CAGR portfolio = (1.1)^(1/2.5) - 1 = 3.87%
        # CAGR benchmark = (1.05)^(1/2.5) - 1 = 1.96%
        # Alpha ≈ +1.91%
        entries = [
            self._make_entry("X", "small", qty=10, buy_price=100,
                             buy_date=date(2024, 1, 1)),
        ]
        with patch.object(cohorts_mod, "build_ledger", return_value=entries), \
             patch.object(cohorts_mod, "_resolve_benchmark",
                          return_value=("nifty50", True, False)), \
             patch.object(cohorts_mod, "close_on",
                          side_effect=lambda n, d: 100.0 if d < date(2026, 1, 1) else 105.0):
            result = compute_cohorts(current_ltp_fn=lambda t: 110.0)
        c = result["smallcap_equity"]
        assert c["cagr_pct"] is not None
        assert c["nifty_cagr_pct"] is not None
        assert c["alpha_pct"] is not None
        # Range check
        assert 3.5 < c["cagr_pct"] < 4.5
        assert 1.5 < c["nifty_cagr_pct"] < 2.5
        assert 1.5 < c["alpha_pct"] < 2.5

    def test_indices_available_in_output(self):
        result = compute_cohorts(current_ltp_fn=lambda t: 100.0)
        assert "indices_available" in result
        assert "nifty50" in result["indices_available"]
        assert "nifty_midcap_150" in result["indices_available"]
        assert "nifty_smallcap_250" in result["indices_available"]


# ---------- Integration with build_ledger ----------

class TestBuildLedgerIntegration:
    def test_build_ledger_returns_entries(self):
        """The real build_ledger (no xlsx mocking) should return at least
        a few entries from the user's actual xlsx files."""
        from pipeline.ledger import build_ledger
        ledger = build_ledger()
        # The user has at least 8 open equity positions
        assert len(ledger) > 0
        # And many closed positions
        n_closed = sum(1 for e in ledger if e.is_closed)
        n_open = sum(1 for e in ledger if e.is_open)
        assert n_open > 0
        assert n_closed > 0
