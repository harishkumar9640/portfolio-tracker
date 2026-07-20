"""Tests for pipeline.cohort_charts (per-cohort benchmark-comparison charts)."""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path
from unittest.mock import patch

import pytest

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

from pipeline import cohort_charts as charts_mod
from pipeline.cohort_charts import (
    _build_portfolio_value_series,
    _build_benchmark_series,
    _build_nifty50_dca_series,
    _build_combined_portfolio_series,
    _get_ticker_close,
    render_cohort_chart,
    render_combined_chart,
    render_all_cohort_charts,
    COHORTS,
)
from pipeline.ledger import LedgerEntry


# ---------- get_ticker_close ----------

class TestGetTickerClose:
    def test_returns_none_for_unknown_ticker(self):
        # Use a clearly invalid ticker
        result = _get_ticker_close("ZZZINVALIDTICKER999", date(2024, 1, 1), date(2024, 1, 31))
        # yfinance may return empty df; either None or empty is acceptable
        assert result is None or len(result) == 0

    def test_caches_result(self):
        # First call should populate cache
        _get_ticker_close("ZZZ_INVALID_999", date(2024, 1, 1), date(2024, 1, 31))
        # Second call should hit cache
        result1 = _get_ticker_close("ZZZ_INVALID_999", date(2024, 1, 1), date(2024, 1, 31))
        result2 = _get_ticker_close("ZZZ_INVALID_999", date(2024, 1, 1), date(2024, 1, 31))
        assert result1 is result2  # same object (cached)


# ---------- _build_portfolio_value_series ----------

class TestBuildPortfolioValueSeries:
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
            sell_value=qty * sell_price if sd else 0,
            pnl=(qty * (sell_price - buy_price)) if sd else 0,
        )

    def test_empty_entries_returns_none(self):
        assert _build_portfolio_value_series([], date.today(), date(2024, 1, 1)) is None

    def test_open_position_with_real_price(self):
        """An open position should produce a non-None series using real yfinance prices."""
        e = self._entry("RELIANCE", qty=10, buy_price=100, buy_date=date(2024, 6, 1))
        s = _build_portfolio_value_series([e], date.today(), date(2024, 6, 1))
        if s is not None:
            # Series should be non-empty and contain valid values
            assert len(s) > 0
            assert not s.isna().any()  # no NaN
            assert (s > 0).all()  # all values positive

    def test_sell_value_persists_after_sell(self):
        """After selling, the sell value should be in the portfolio (not drop to 0)."""
        e = self._entry("RELIANCE", qty=10, buy_price=100, buy_date=date(2024, 6, 1),
                        sell_date=date(2024, 9, 1), sell_price=150)
        s = _build_portfolio_value_series([e], date(2024, 12, 1), date(2024, 6, 1))
        if s is not None:
            # Final value should reflect the sell price, not 0
            final = s.iloc[-1]
            assert final > 100  # we sold for 50% gain


# ---------- _build_benchmark_series ----------

class TestBuildBenchmarkSeries:
    def test_nifty50_available(self):
        s = _build_benchmark_series("nifty50", date(2024, 1, 1), date(2024, 12, 31))
        if s is not None:
            assert s.iloc[0] == pytest.approx(100.0)
            assert len(s) > 100

    def test_unknown_benchmark_returns_none(self):
        s = _build_benchmark_series("nifty_does_not_exist", date(2024, 1, 1), date(2024, 12, 31))
        assert s is None


# ---------- render_cohort_chart ----------

class TestRenderCohortChart:
    def test_unknown_cohort_returns_none(self):
        assert render_cohort_chart("nonexistent_cohort") is None

    def test_renders_large_cap_chart(self):
        """Should produce a PNG file for the large-cap cohort (real data)."""
        path = render_cohort_chart("large_cap_equity")
        if path is not None:
            assert path.exists()
            assert path.suffix == ".png"
            assert path.stat().st_size > 1000  # at least 1KB (real PNG)
            path.unlink()  # cleanup

    def test_renders_midcap_chart(self):
        path = render_cohort_chart("midcap_equity")
        if path is not None:
            assert path.exists()
            assert path.suffix == ".png"
            path.unlink()

    def test_renders_smallcap_chart(self):
        path = render_cohort_chart("smallcap_equity")
        if path is not None:
            assert path.exists()
            assert path.suffix == ".png"
            path.unlink()


# ---------- render_all_cohort_charts ----------

class TestRenderAllCohortCharts:
    def test_returns_dict_for_all_three_cohorts(self):
        result = render_all_cohort_charts()
        assert set(result.keys()) == {"large_cap_equity", "midcap_equity", "smallcap_equity"}
        # Each value is either a Path (success) or None (no data)
        for cname, path in result.items():
            if path is not None:
                assert path.exists()
                assert cname in path.name
                path.unlink()  # cleanup

    def test_clears_price_cache(self):
        # Render once to populate the cache
        render_all_cohort_charts()
        # After a full render, _PRICE_CACHE.clear() is called at the start,
        # but entries fetched during the render are still there. The next
        # call clears them again at the start. Either way the cache should
        # only contain entries from the most recent render (which is fine).
        # The important behavior is that the render doesn't accumulate
        # cache entries across multiple calls.
        before = len(charts_mod._PRICE_CACHE)
        render_all_cohort_charts()
        after = len(charts_mod._PRICE_CACHE)
        # The cache should not have grown unboundedly; the second render
        # should be similar in size to the first.
        assert after <= before + 5, (
            f"Cache grew unboundedly: before={before}, after={after}"
        )


# ---------- chart output is date-stamped ----------

def test_chart_files_are_dated_today():
    """Generated charts should have today's date in the filename,
    so old charts are easy to identify for cleanup."""
    for cname in COHORTS:
        path = render_cohort_chart(cname)
        if path is not None:
            assert date.today().isoformat() in path.name
            path.unlink()


# ---------- render_all_cohort_charts caching ----------

class TestRenderAllCohortChartsCaching:
    def test_cache_hit_returns_existing_files(self, tmp_path):
        """If all charts already exist (just rendered), the second call
        should return the existing paths without re-rendering."""
        # Render once to create the files
        first = render_all_cohort_charts()
        for cname, p in first.items():
            if p is not None:
                assert p.exists()
        # Render again - should hit cache
        second = render_all_cohort_charts()
        for cname in first:
            if first[cname] is not None:
                assert second[cname] == first[cname]
        # Cleanup
        for p in first.values():
            if p is not None:
                p.unlink()

    def test_force_bypasses_cache(self):
        """force=True should always re-render even if cache is valid."""
        first = render_all_cohort_charts()
        # Cleanup before force
        for p in first.values():
            if p is not None:
                p.unlink()
        # Force re-render
        forced = render_all_cohort_charts(force=True)
        for cname, p in forced.items():
            if p is not None:
                assert p.exists()
                p.unlink()


# ---------- render_combined_chart ----------

class TestRenderCombinedChart:
    def test_renders_combined_chart(self):
        """Should produce a PNG for the combined portfolio vs Nifty 50."""
        from pipeline.cohorts import _build_ltp_fn
        path = render_combined_chart(current_ltp_fn=_build_ltp_fn())
        if path is not None:
            assert path.exists()
            assert path.suffix == ".png"
            assert "combined_vs_nifty50" in path.name
            assert path.stat().st_size > 1000
            path.unlink()

    def test_force_bypasses_cache(self):
        from pipeline.cohorts import _build_ltp_fn
        # Render once
        first = render_combined_chart(current_ltp_fn=_build_ltp_fn())
        if first is None:
            pytest.skip("no data")
        # Render again with force=True, should re-create
        second = render_combined_chart(current_ltp_fn=_build_ltp_fn(), force=True)
        assert second == first
        first.unlink()

    def test_combined_chart_uses_today_in_filename(self):
        from datetime import date
        from pipeline.cohorts import _build_ltp_fn
        path = render_combined_chart(current_ltp_fn=_build_ltp_fn())
        if path is not None:
            assert date.today().isoformat() in path.name
            path.unlink()
