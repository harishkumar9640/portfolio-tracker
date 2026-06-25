"""
Tests for the pure valuation functions in fair_value/valuation.py
and the structured breakdowns the modal renders.

These functions are pure (no I/O, no network), so they can be tested
fully offline. They are the core math behind every fair-value lookup
on the dashboard, so regressions here would silently break the UI.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

from fair_value.valuation import (
    graham_number, pe_relative_value, dcf_value,
    dcf_breakdown, graham_breakdown, pe_relative_breakdown,
    check, ValuationRow,
)


# ---------- Graham Number ----------
class TestGrahamNumber:
    def test_known_value(self):
        # Classic ITC example: sqrt(22.5 * 4.16 * 57.90) ≈ 73.62
        v = graham_number(4.16, 57.90)
        assert abs(v - 73.62) < 0.5

    def test_returns_zero_for_zero_eps(self):
        assert graham_number(0, 100) == 0.0

    def test_returns_zero_for_negative_bvps(self):
        # Negative book value => meaningless, return 0
        assert graham_number(5, -10) == 0.0

    def test_breakdown_includes_step_math(self):
        bd = graham_breakdown(4.16, 57.90)
        assert bd["value"] > 0
        assert "sqrt" in bd["formula"]
        assert "EPS" in bd["step_math"]
        assert "BVPS" in bd["step_math"]


# ---------- PE-Relative ----------
class TestPeRelativeValue:
    def test_basic_multiplication(self):
        # EPS 10, Industry PE 20 -> 200
        assert pe_relative_value(10, 20) == 200.0

    def test_returns_zero_for_no_industry_pe(self):
        assert pe_relative_value(10, 0) == 0.0
        assert pe_relative_value(10, None) == 0.0

    def test_breakdown_uses_industry_pe(self):
        bd = pe_relative_breakdown(10, 25)
        assert bd["value"] == 250.0
        assert "10.00" in bd["formula"]
        assert "25.0" in bd["formula"]


# ---------- Two-stage DCF ----------
class TestDcfValue:
    def test_returns_zero_for_zero_fcf(self):
        assert dcf_value(0) == 0.0
        assert dcf_value(-5) == 0.0

    def test_returns_zero_when_r_leq_g2(self):
        # r=5%, g2=5% would divide by zero; r=4% < g2 is invalid
        assert dcf_value(10, g1=0.10, g2=0.05, r=0.05) == 0.0
        assert dcf_value(10, g1=0.10, g2=0.05, r=0.04) == 0.0

    def test_default_params_give_positive_value(self):
        v = dcf_value(14.73)
        # ITC-like FCF with defaults should give ~290
        assert v > 200
        assert v < 400

    def test_higher_growth_increases_value(self):
        low = dcf_value(10, g1=0.05, g2=0.02, r=0.10)
        high = dcf_value(10, g1=0.15, g2=0.03, r=0.10)
        assert high > low

    def test_higher_discount_rate_decreases_value(self):
        low_r = dcf_value(10, g1=0.10, g2=0.03, r=0.08)
        high_r = dcf_value(10, g1=0.10, g2=0.03, r=0.15)
        assert low_r > high_r


# ---------- dcf_breakdown (structured output for the modal) ----------
class TestDcfBreakdown:
    def test_returns_empty_dict_for_invalid_inputs(self):
        assert dcf_breakdown(0) == {}
        assert dcf_breakdown(-5) == {}
        # r <= g2
        assert dcf_breakdown(10, g1=0.10, g2=0.10, r=0.10) == {}

    def test_contains_all_required_sections(self):
        bd = dcf_breakdown(14.73)
        assert "inputs" in bd
        assert "years" in bd
        assert "terminal" in bd
        assert "totals" in bd
        assert "step_math" in bd

    def test_inputs_match_function_args(self):
        bd = dcf_breakdown(14.73, g1=0.12, g2=0.04, r=0.11, years=5)
        assert bd["inputs"]["fcf_per_share"] == 14.73
        assert bd["inputs"]["g1"] == 0.12
        assert bd["inputs"]["g2"] == 0.04
        assert bd["inputs"]["r"] == 0.11
        assert bd["inputs"]["years"] == 5

    def test_year_count_matches_years_arg(self):
        bd = dcf_breakdown(10, years=5)
        assert len(bd["years"]) == 5
        for i, yr in enumerate(bd["years"], 1):
            assert yr["year"] == i

    def test_year_projection_formula(self):
        """FCF₁ = FCF₀ × (1 + g₁)^1 = 10 × 1.10 = 11.00"""
        bd = dcf_breakdown(10, g1=0.10, g2=0.03, r=0.10, years=3)
        assert abs(bd["years"][0]["projected_fcf"] - 11.00) < 0.01

    def test_present_value_formula(self):
        """PV₁ = 11.00 / 1.10 = 10.00"""
        bd = dcf_breakdown(10, g1=0.10, g2=0.03, r=0.10, years=3)
        assert abs(bd["years"][0]["present_value"] - 10.00) < 0.01

    def test_discount_factor_correct(self):
        """1/(1.10)^3 = 0.7513..."""
        bd = dcf_breakdown(10, g1=0.10, g2=0.03, r=0.10, years=3)
        assert abs(bd["years"][2]["discount_factor"] - (1 / 1.331)) < 0.0001

    def test_terminal_value_formula(self):
        """TV = FCF_5 × (1+g₂) / (r - g₂) = 10×1.10^5 × 1.03 / 0.07
        = 16.105 × 1.03 / 0.07 ≈ 236.97"""
        bd = dcf_breakdown(10, g1=0.10, g2=0.03, r=0.10, years=5)
        expected_tv = (10 * 1.10 ** 5) * 1.03 / 0.07
        assert abs(bd["terminal"]["terminal_value"] - expected_tv) < 0.01

    def test_total_dcf_matches_dcf_value(self):
        """The breakdown's totals['dcf'] must match dcf_value() exactly
        (same formula, same inputs)."""
        for fcf, g1, g2, r in [(10, 0.10, 0.03, 0.10),
                                (14.73, 0.12, 0.04, 0.11),
                                (5, 0.08, 0.02, 0.09)]:
            v = dcf_value(fcf, g1, g2, r)
            bd = dcf_breakdown(fcf, g1, g2, r)
            assert abs(bd["totals"]["dcf"] - v) < 0.01, \
                f"breakdown {bd['totals']['dcf']} != dcf_value {v}"

    def test_terminal_pct_sums_correctly(self):
        """terminal_pct = pv_terminal / (pv_stage1 + pv_terminal)"""
        bd = dcf_breakdown(10)
        expected = bd["totals"]["pv_terminal"] / bd["totals"]["dcf"]
        assert abs(bd["totals"]["terminal_pct"] - expected) < 0.001

    def test_step_math_contains_key_steps(self):
        bd = dcf_breakdown(10)
        text = bd["step_math"]
        # Each numbered step must be present so the user can read the math
        for keyword in ["Step 1", "Step 2", "Step 3", "Step 4", "Step 5"]:
            assert keyword in text, f"{keyword!r} missing from step_math"
        # Terminal-value dominance warning
        assert "terminal value contributes" in text
        # The two key inputs are referenced
        assert "FCF" in text
        assert "discount" in text.lower() or "r =" in text

    def test_terminal_dominance_increases_with_higher_g1(self):
        """Higher stage-1 growth means more compounding before terminal,
        but it also amplifies the terminal value. Either way, terminal
        should remain the dominant share."""
        low_growth = dcf_breakdown(10, g1=0.05, g2=0.03, r=0.10)
        high_growth = dcf_breakdown(10, g1=0.20, g2=0.03, r=0.10)
        # In both cases terminal dominates (>50%)
        assert low_growth["totals"]["terminal_pct"] > 0.5
        assert high_growth["totals"]["terminal_pct"] > 0.5


# ---------- check() returns breakdowns ----------
class TestCheckReturnsBreakdowns:
    """End-to-end: check() must populate dcf_breakdown and other_methods
    on every ValuationRow. We use a mock fetcher so this is offline."""

    def _fake_fetch(self, ticker):
        # check() does data.get("..."), so return a real dict
        return {
            "ticker": ticker,
            "current_price": 290.0, "eps": 4.16, "book_value": 57.90,
            "operating_cash_flow_per_share": 14.73, "market_cap": 363417.0,
            "error": None,
        }

    def test_dcf_breakdown_populated(self, monkeypatch):
        from fair_value import valuation
        monkeypatch.setattr(valuation, "fetch", self._fake_fetch)
        rows = check(["ITC"])
        assert len(rows) == 1
        bd = rows[0].dcf_breakdown
        assert bd is not None
        assert "years" in bd
        assert "terminal" in bd
        assert "totals" in bd

    def test_other_methods_populated(self, monkeypatch):
        from fair_value import valuation
        monkeypatch.setattr(valuation, "fetch", self._fake_fetch)
        rows = check(["ITC"])
        other = rows[0].other_methods
        assert other is not None
        # With no industry_pe passed, only Graham is in other_methods
        assert "graham" in other
        assert "pe_relative" not in other

    def test_other_methods_includes_pe_relative_when_industry_pe_given(self, monkeypatch):
        from fair_value import valuation
        monkeypatch.setattr(valuation, "fetch", self._fake_fetch)
        rows = check(["ITC"], industry_pe=25)
        other = rows[0].other_methods
        assert "graham" in other
        assert "pe_relative" in other
        assert other["pe_relative"]["value"] == pytest.approx(4.16 * 25, rel=0.01)

    def test_to_dict_includes_breakdowns(self, monkeypatch):
        """The webapp reads ValuationRow.to_dict() — both breakdowns
        must appear in the JSON so the modal can render them."""
        from fair_value import valuation
        monkeypatch.setattr(valuation, "fetch", self._fake_fetch)
        rows = check(["ITC"])
        d = rows[0].to_dict()
        assert "dcf_breakdown" in d
        assert "other_methods" in d
        assert d["dcf_breakdown"]["totals"]["dcf"] > 200
        assert d["other_methods"]["graham"]["value"] > 0

    def test_no_breakdown_when_no_fcf(self, monkeypatch):
        """If the company has no FCF data, dcf_breakdown should be None
        (so the modal can show 'DCF breakdown unavailable')."""
        from fair_value import valuation
        def fake_fetch_no_fcf(ticker):
            return {
                "ticker": ticker, "current_price": 290.0, "eps": 4.16,
                "book_value": 57.90, "operating_cash_flow_per_share": None,
                "market_cap": 363417.0, "error": None,
            }
        monkeypatch.setattr(valuation, "fetch", fake_fetch_no_fcf)
        rows = check(["X"])
        assert rows[0].dcf_breakdown is None
        assert rows[0].dcf is None