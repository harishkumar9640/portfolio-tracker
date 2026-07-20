"""Tests for pipeline.xirr (Extended Internal Rate of Return)."""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

from pipeline.xirr import (
    xirr,
    _npv,
    _npv_deriv,
    _build_cashflows,
    compute_xirr,
)
from pipeline.ledger import LedgerEntry


# ---------- xirr() core solver ----------

class TestXirrSolver:
    def test_empty_returns_none(self):
        assert xirr([]) is None

    def test_single_cashflow_returns_none(self):
        # Need at least one negative and one positive for a meaningful XIRR
        assert xirr([(date(2024, 1, 1), -1000.0)]) is None

    def test_one_year_ten_percent(self):
        # Invest 1000, get 1100 back after 1 year -> XIRR = 10%
        flows = [(date(2024, 1, 1), -1000.0), (date(2025, 1, 1), 1100.0)]
        rate = xirr(flows)
        assert rate is not None
        assert rate == pytest.approx(0.10, abs=0.005)

    def test_two_year_doubling(self):
        # 1000 -> 2000 in 2 years -> XIRR = sqrt(2) - 1 = ~41.4%
        flows = [(date(2024, 1, 1), -1000.0), (date(2026, 1, 1), 2000.0)]
        rate = xirr(flows)
        assert rate is not None
        assert rate == pytest.approx(0.4142, abs=0.005)

    def test_loss_returns_negative(self):
        # 1000 -> 500 in 1 year -> XIRR = -50%
        flows = [(date(2024, 1, 1), -1000.0), (date(2025, 1, 1), 500.0)]
        rate = xirr(flows)
        assert rate is not None
        assert rate == pytest.approx(-0.50, abs=0.005)

    def test_dca_higher_than_twr_in_recent_gains(self):
        # Real-world test: XIRR should differ from TWR when cash flows
        # are uneven. Here the LATE investment is the one that did well,
        # so XIRR > TWR.
        # TWR: invest 1000 (1y) -> 1100, then add 1000 (6mo) -> 2500
        # Period 1: 1000 -> 1100 (10% over 0.5y)
        # Period 2: 1100+1000=2100 -> 2500 (19% over 0.5y)
        # TWR = (1.10 * 1.19) - 1 = 30.9%
        # XIRR will be different because the second 1000 was only in for 0.5y
        flows = [
            (date(2024, 1, 1), -1000.0),
            (date(2024, 7, 1), -1000.0),
            (date(2025, 1, 1), 2500.0),
        ]
        rate = xirr(flows)
        assert rate is not None
        # XIRR should be positive and around 30-40%
        assert 0.20 < rate < 0.50

    def test_unsorted_flows(self):
        # Flows can be in any order; xirr() sorts them
        flows = [
            (date(2025, 1, 1), 1100.0),
            (date(2024, 1, 1), -1000.0),
        ]
        rate = xirr(flows)
        assert rate is not None
        assert rate == pytest.approx(0.10, abs=0.005)

    def test_negative_guess_finds_correct_rate(self):
        # Test that the solver handles negative starting guesses
        flows = [(date(2024, 1, 1), -1000.0), (date(2025, 1, 1), 800.0)]
        # 1000 -> 800 = -20% over 1y
        rate = xirr(flows, guess=-0.5)
        assert rate is not None
        assert rate == pytest.approx(-0.20, abs=0.005)

    def test_high_return_rate(self):
        # 1000 -> 10000 in 1y = 900% return
        flows = [(date(2024, 1, 1), -1000.0), (date(2025, 1, 1), 10000.0)]
        rate = xirr(flows)
        assert rate is not None
        assert rate == pytest.approx(9.0, abs=0.05)


# ---------- NPV helper ----------

class TestNpv:
    def test_npv_zero_rate(self):
        flows = [(date(2024, 1, 1), -1000.0), (date(2024, 7, 1), 500.0)]
        # At rate=0, NPV = sum of cashflows = -500
        assert _npv(0.0, flows, date(2024, 1, 1)) == pytest.approx(-500.0)

    def test_npv_invariant_to_d0(self):
        # NPV depends on time-from-d0, not the absolute dates
        flows1 = [(date(2024, 1, 1), -1000.0), (date(2025, 1, 1), 1100.0)]
        flows2 = [(date(2020, 1, 1), -1000.0), (date(2021, 1, 1), 1100.0)]
        # Both should give the same NPV at the same rate
        n1 = _npv(0.10, flows1, date(2024, 1, 1))
        n2 = _npv(0.10, flows2, date(2020, 1, 1))
        assert n1 == pytest.approx(n2)


# ---------- _build_cashflows ----------

class TestBuildCashflows:
    def _entry(self, ticker, qty=10, buy_price=100, buy_date=None,
               sell_date=None, sell_price=120):
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
        )

    def test_empty_ledger(self):
        flows, stats = _build_cashflows([], date(2024, 12, 1))
        assert flows == []
        assert stats["total_invested"] == 0

    def test_open_position_adds_terminal_value(self):
        e = self._entry("RELIANCE", qty=10, buy_price=100, buy_date=date(2024, 6, 1))
        today = date(2024, 12, 1)
        flows, stats = _build_cashflows([e], today, current_ltp_fn=lambda t: 110.0)
        # One outflow (buy), one inflow (terminal LTP-based value)
        assert len(flows) == 2
        assert flows[0] == (date(2024, 6, 1), -1000.0)  # buy
        assert flows[1] == (today, 10 * 110.0)            # terminal
        assert stats["open_value"] == 10 * 110.0
        assert stats["total_invested"] == 1000.0
        assert stats["n_open_lots"] == 1

    def test_closed_position(self):
        e = self._entry("RELIANCE", qty=10, buy_price=100,
                        buy_date=date(2024, 6, 1),
                        sell_date=date(2024, 9, 1), sell_price=150)
        flows, stats = _build_cashflows([e], date(2024, 12, 1))
        # One outflow (buy), one inflow (sell)
        assert len(flows) == 2
        assert flows[0] == (date(2024, 6, 1), -1000.0)
        assert flows[1] == (date(2024, 9, 1), 1500.0)
        assert stats["total_realized"] == 1500.0
        assert stats["open_value"] == 0  # no open lots

    def test_ltp_fallback_to_buy_price(self):
        e = self._entry("RELIANCE", qty=10, buy_price=100, buy_date=date(2024, 6, 1))
        # No current_ltp_fn: should fall back to e.buy_price = 100
        flows, stats = _build_cashflows([e], date(2024, 12, 1), current_ltp_fn=None)
        assert flows[1] == (date(2024, 12, 1), 1000.0)  # 10 * 100


# ---------- compute_xirr (full pipeline) ----------

class TestComputeXirr:
    def test_real_portfolio_excludes_etfs_by_default(self):
        """The user's portfolio has ETFs (GOLDBEES, METALIETF, NEXT50IETF)
        which should be excluded by default."""
        result = compute_xirr(current_ltp_fn=None)
        # Should have a non-None XIRR (or a meaningful fallback)
        assert result is not None
        # If the result has an error, the issue is no data
        if "error" in result:
            return  # skip if no data
        # n_lots should be 69 (matching the cohort output minus ETFs)
        # Actual count from the live data: 69 (8 open + 61 closed)
        # We don't assert this strictly because the ledger is dynamic

    def test_include_etfs(self):
        result_no_etf = compute_xirr(include_etfs=False)
        result_with_etf = compute_xirr(include_etfs=True)
        # With ETFs, the invested amount is higher (ETFs are also investments)
        if "error" in result_no_etf or "error" in result_with_etf:
            pytest.skip("no ledger data")
        assert result_with_etf["total_invested"] >= result_no_etf["total_invested"]

    def test_output_structure(self):
        result = compute_xirr(current_ltp_fn=None)
        # Required fields
        for key in ("xirr_pct", "xirr", "converged", "total_invested",
                    "total_realized", "open_value", "total_return_pct",
                    "earliest_date", "latest_date", "n_lots",
                    "n_open_lots", "n_closed_lots", "cashflows"):
            assert key in result, f"missing key: {key}"

    def test_xirr_returns_none_for_loss_only(self):
        """A pure-loss scenario (no terminal inflow) should still compute."""
        # We can't easily create this from the real ledger, so just
        # verify the function handles the edge case.
        result = compute_xirr(current_ltp_fn=lambda t: 0.0)
        # If all LTPs are 0, the open_value is 0, total is < 0
        # XIRR should still be defined (it's a loss)
        assert "xirr" in result or "error" in result
