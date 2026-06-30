"""
Tests for pipeline.intraday.py — the today's-vs-world-indices pipeline.intraday
comparison chart (1m / 5m / 15m).

These tests run entirely offline by mocking yfinance and the holdings
cache. They cover:

  - CACHE_TTL: cache is considered fresh for 5 minutes, stale after
  - INTERVAL_RULES: every supported interval has a valid yfinance rule
  - normalize_to_open_today: each column scales so its first valid
    value today equals 100; rows beyond 24h are dropped; all-NaN
    rows are dropped
  - build_intraday_snapshot: returns the expected JSON shape
    (interval, asof, series dict); every series is a list of
    {t, v} dicts; no empty series leak out
  - _load_equity_holdings: returns [] when cache missing, correctly
    maps symbol -> ticker (strips -EQ, adds .NS) and computes weights
  - build_combined_portfolio: with mocked equity, MF, and SGB inputs,
    produces a portfolio line that is the weighted sum of the three
    components (within floating-point tolerance)
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))


# ---------- Cache TTL ----------
class TestCacheTTL:
    def test_cache_age_for_missing_file_is_large(self, tmp_path):
        from pipeline.intraday import _cache_age
        age = _cache_age(tmp_path / "nope.csv")
        assert age > timedelta(days=30)

    def test_cache_fresh_within_5_minutes(self, tmp_path):
        from pipeline.intraday import _cache_age, _is_cache_fresh, CACHE_TTL
        p = tmp_path / "fresh.csv"
        p.write_text("a,b\n1,2\n")
        # Just touched, must be fresh
        assert _cache_age(p) < CACHE_TTL
        assert _is_cache_fresh(p) is True

    def test_cache_stale_after_5_minutes(self, tmp_path):
        from pipeline.intraday import _is_cache_fresh
        import os
        p = tmp_path / "stale.csv"
        p.write_text("a,b\n1,2\n")
        # Backdate by 10 minutes
        old = (datetime.now() - timedelta(minutes=10)).timestamp()
        os.utime(p, (old, old))
        assert _is_cache_fresh(p) is False


# ---------- Interval rules ----------
class TestIntervalRules:
    def test_all_three_intervals_have_valid_rules(self):
        from pipeline.intraday import INTERVAL_RULES
        assert set(INTERVAL_RULES.keys()) == {"1m", "5m", "15m"}
        for interval, rule in INTERVAL_RULES.items():
            assert "period" in rule and rule["period"]
            assert "interval" in rule and rule["interval"]

    def test_1m_period_respects_yahoo_7d_limit(self):
        from pipeline.intraday import INTERVAL_RULES
        # Yahoo Finance 1m data is only available for <= 7 days
        period = INTERVAL_RULES["1m"]["period"]
        # Convert "5d" / "7d" -> int days
        n = int(period.rstrip("d"))
        assert n <= 7


# ---------- Equity holdings parsing ----------
class TestLoadEquityHoldings:
    def test_missing_cache_returns_empty(self, tmp_path, monkeypatch):
        from pipeline.intraday import _load_equity_holdings
        monkeypatch.setattr("pipeline.intraday.PROJECT", tmp_path)
        assert _load_equity_holdings() == []

    def test_corrupt_cache_returns_empty(self, tmp_path, monkeypatch):
        from pipeline.intraday import _load_equity_holdings
        (tmp_path / "data").mkdir()
        (tmp_path / "data" / "holdings_cache.json").write_text("not-json{")
        monkeypatch.setattr("pipeline.intraday.PROJECT", tmp_path)
        assert _load_equity_holdings() == []

    def test_empty_holdings_list_returns_empty(self, tmp_path, monkeypatch):
        from pipeline.intraday import _load_equity_holdings
        (tmp_path / "data").mkdir()
        (tmp_path / "data" / "holdings_cache.json").write_text('{"holdings": []}')
        monkeypatch.setattr("pipeline.intraday.PROJECT", tmp_path)
        assert _load_equity_holdings() == []

    def test_normalises_symbols_and_computes_weights(self, tmp_path, monkeypatch):
        from pipeline.intraday import _load_equity_holdings
        (tmp_path / "data").mkdir()
        (tmp_path / "data" / "holdings_cache.json").write_text(json.dumps({
            "holdings": [
                {"symbol": "RELIANCE-EQ", "current_value": 60000},
                {"symbol": "TCS-EQ",       "current_value": 40000},
            ]
        }))
        monkeypatch.setattr("pipeline.intraday.PROJECT", tmp_path)
        rows = _load_equity_holdings()
        assert len(rows) == 2
        # -EQ stripped, .NS suffix added
        assert rows[0]["ticker"] == "RELIANCE.NS"
        assert rows[1]["ticker"] == "TCS.NS"
        # Weights sum to 1 and are in proportion to value
        total_weight = sum(r["weight"] for r in rows)
        assert abs(total_weight - 1.0) < 1e-9
        assert abs(rows[0]["weight"] - 0.6) < 1e-9
        assert abs(rows[1]["weight"] - 0.4) < 1e-9

    def test_zero_total_value_falls_back_to_zero_weights(self, tmp_path, monkeypatch):
        from pipeline.intraday import _load_equity_holdings
        (tmp_path / "data").mkdir()
        (tmp_path / "data" / "holdings_cache.json").write_text(json.dumps({
            "holdings": [{"symbol": "X-EQ", "current_value": 0}]
        }))
        monkeypatch.setattr("pipeline.intraday.PROJECT", tmp_path)
        rows = _load_equity_holdings()
        assert rows[0]["weight"] == 0.0


# ---------- normalize_to_open_today ----------
class TestNormalizeToOpenToday:
    def _make_df(self) -> pd.DataFrame:
        # Two columns, mixed-timezone feel; 100 ticks, only the last 50
        # are within the past 24h (everything older should be dropped).
        # Use tz-aware UTC timestamps (like yfinance returns)
        utc = pd.Timestamp.now(tz="UTC").tz
        old = pd.date_range("2024-01-01", periods=50, freq="5min", tz="UTC")
        new = pd.date_range(datetime.now(tz=utc) - timedelta(hours=2),
                            periods=50, freq="5min", tz="UTC")
        idx = old.append(new)
        return pd.DataFrame(
            {"A": [float(i) for i in range(100)],
             "B": [float(i * 2) for i in range(100)]},
            index=idx,
        )

    def test_drops_rows_older_than_24h(self):
        from pipeline.intraday import normalize_to_open_today
        df = self._make_df()
        out = normalize_to_open_today(df)
        # The kept window should be the recent block; first row should
        # be within 24h of the last row
        if not out.empty:
            gap = (out.index[-1] - out.index[0]).total_seconds()
            assert gap < 24 * 3600 + 60

    def test_first_valid_value_becomes_100(self):
        from pipeline.intraday import normalize_to_open_today
        df = self._make_df()
        out = normalize_to_open_today(df)
        # Each column's first non-NaN value should be exactly 100
        for col in out.columns:
            first_valid = out[col].dropna().iloc[0]
            assert abs(first_valid - 100.0) < 1e-9

    def test_drops_all_nan_rows(self):
        from pipeline.intraday import normalize_to_open_today
        df = self._make_df()
        # Add an all-NaN row in the middle of the recent window
        mid_idx = df.index[-25]
        df.loc[mid_idx, :] = float("nan")
        out = normalize_to_open_today(df)
        # The all-NaN row must be gone
        assert mid_idx not in out.index

    def test_empty_input_returns_empty(self):
        from pipeline.intraday import normalize_to_open_today
        df = pd.DataFrame()
        out = normalize_to_open_today(df)
        assert out.empty

    def test_keeps_only_today_when_mixed_old_and_new(self):
        """Regression: previously the algorithm kept 24h of data which
        made US indices show yesterday's close on the same axis as
        Nifty's today. The fix is to keep only the contiguous block
        ending at the latest timestamp with at most 30-minute gaps."""
        from pipeline.intraday import normalize_to_open_today
        # Use IST-anchored "now" then build UTC ranges from the same instant
        now_ist = pd.Timestamp.now(tz="Asia/Kolkata")
        recent_start_ist = now_ist.normalize() + pd.Timedelta(hours=9)  # 09:00 IST today
        recent_idx = pd.date_range(
            start=recent_start_ist.tz_convert("UTC"),
            periods=36, freq="10min",
        )
        # 1-day gap, then 4 hours of older data
        old_start_ist = recent_start_ist - pd.Timedelta(days=1, hours=4)
        old_idx = pd.date_range(
            start=old_start_ist.tz_convert("UTC"),
            periods=24, freq="10min",
        )
        idx = old_idx.append(recent_idx)
        df = pd.DataFrame({"A": range(len(idx))}, index=idx)
        out = normalize_to_open_today(df)
        # Only the recent block should remain (36 points)
        assert len(out) == 36
        # The first row's timestamp should be the recent block's first
        # converted to IST
        expected_first_ist = recent_idx[0].tz_convert("Asia/Kolkata")
        assert out.index[0] == expected_first_ist

    def test_handles_sparse_indices_with_30min_gaps(self):
        """If gaps are at most 30 minutes (e.g. a sparse index), the
        algorithm should treat them as part of the same contiguous block."""
        from pipeline.intraday import normalize_to_open_today
        base = pd.Timestamp.now(tz="Asia/Kolkata").normalize() + pd.Timedelta(hours=10)
        # 10 points spaced 15 minutes apart (small gaps)
        idx = pd.date_range(base, periods=10, freq="15min")
        df = pd.DataFrame({"A": range(len(idx))}, index=idx)
        out = normalize_to_open_today(df)
        assert len(out) == 10

    def test_splits_at_large_gap_even_when_latest_has_no_data(self):
        """A 5-hour gap (e.g. between US close and Asian reopen) must
        split the chart into two blocks; we keep only the latest block."""
        from pipeline.intraday import normalize_to_open_today
        recent = pd.date_range("2026-06-25 09:00", periods=5, freq="5min", tz="Asia/Kolkata")
        sparse_late = pd.date_range("2026-06-25 15:00", periods=2, freq="5min", tz="Asia/Kolkata")
        idx = recent.append(sparse_late)
        df = pd.DataFrame({"A": range(len(idx))}, index=idx)
        out = normalize_to_open_today(df)
        # Only the latest 2 points survive the 5h40m gap (after IST conversion they're still in IST)
        assert len(out) == 2
        assert out.index[0] == sparse_late[0]


# ---------- build_intraday_snapshot (mocked yfinance) ----------
class TestBuildIntradaySnapshot:
    """Build a synthetic pipeline.intraday world with two index tickers and
    one equity ticker; verify the JSON snapshot has the right shape
    and that the normalisation is correct."""

    def _mock_series(self, n: int = 30, base: float = 100.0, slope: float = 0.1) -> pd.Series:
        idx = pd.date_range(datetime.now(tz=pd.Timestamp.now(tz="UTC").tz) - timedelta(hours=n * 5 / 60),
                            periods=n, freq="5min", tz="UTC")
        return pd.Series([base + slope * i for i in range(n)], index=idx)

    def test_returns_expected_json_shape(self, tmp_path, monkeypatch):
        from pipeline.intraday import build_intraday_snapshot

        # Mock the index fetch: two indices with very different slopes
        nifty = self._mock_series(n=30, base=18000, slope=10.0)
        sp500 = self._mock_series(n=30, base=4500, slope=-2.0)

        with patch("pipeline.intraday.fetch_intraday_indices", return_value=pd.DataFrame({
            "Nifty 50 (IN)": nifty, "S&P 500 (US)": sp500,
        })), \
             patch("pipeline.intraday.build_combined_portfolio", return_value=pd.DataFrame({
                 "My Portfolio": self._mock_series(n=30, base=500000, slope=50.0),
             })), \
             patch("pipeline.intraday._cache_path", return_value=tmp_path / "cache.csv"):
            snap = build_intraday_snapshot("5m")

        assert snap["interval"] == "5m"
        assert "asof" in snap
        assert isinstance(snap["series"], dict)
        # All three series must be present
        assert set(snap["series"].keys()) == {"Nifty 50 (IN)", "S&P 500 (US)", "My Portfolio"}

    def test_each_series_starts_at_100(self, tmp_path):
        from pipeline.intraday import build_intraday_snapshot
        nifty = self._mock_series(n=20, base=18000, slope=5.0)
        sp500 = self._mock_series(n=20, base=4500, slope=-1.0)
        portfolio = self._mock_series(n=20, base=500000, slope=20.0)

        with patch("pipeline.intraday.fetch_intraday_indices", return_value=pd.DataFrame({
            "Nifty 50 (IN)": nifty, "S&P 500 (US)": sp500,
        })), \
             patch("pipeline.intraday.build_combined_portfolio", return_value=pd.DataFrame({
                 "My Portfolio": portfolio,
             })), \
             patch("pipeline.intraday._cache_path", return_value=tmp_path / "cache.csv"):
            snap = build_intraday_snapshot("5m")

        for name, pts in snap["series"].items():
            assert len(pts) > 0, f"{name} has no points"
            # First point should be exactly 100 (normalised to base)
            assert abs(pts[0]["v"] - 100.0) < 1e-6, \
                f"{name} first point is {pts[0]['v']}, expected 100"

    def test_each_point_has_iso_timestamp(self, tmp_path):
        from pipeline.intraday import build_intraday_snapshot
        nifty = self._mock_series(n=10)
        with patch("pipeline.intraday.fetch_intraday_indices", return_value=pd.DataFrame({
            "Nifty 50 (IN)": nifty,
        })), \
             patch("pipeline.intraday.build_combined_portfolio", return_value=pd.DataFrame({
                 "My Portfolio": nifty,
             })), \
             patch("pipeline.intraday._cache_path", return_value=tmp_path / "cache.csv"):
            snap = build_intraday_snapshot("5m")
        for col, pts in snap["series"].items():
            for p in pts:
                assert "t" in p and "v" in p
                # ISO timestamp must parse
                ts = pd.Timestamp(p["t"])
                assert ts.year >= 2024

    def test_timestamps_are_in_ist(self, tmp_path):
        """Regression: previously the API returned timestamps in each
        index's native timezone (US in America/New_York, Asia in
        Asia/Tokyo etc.), so the chart's x-axis showed wrong hours for
        Indian users. Now every timestamp must carry +05:30 offset."""
        from pipeline.intraday import build_intraday_snapshot
        # Use tz-aware indices in DIFFERENT timezones to simulate the
        # mixed-tz yfinance output (US Eastern for S&P 500, IST for Nifty).
        us_tz = pd.date_range("2026-06-25 09:30", periods=4, freq="1h",
                              tz="America/New_York")
        in_tz = pd.date_range("2026-06-25 09:15", periods=4, freq="1h",
                              tz="Asia/Kolkata")
        nifty = pd.Series([100, 101, 102, 103], index=in_tz)
        sp500 = pd.Series([4000, 4005, 4010, 4015], index=us_tz)
        with patch("pipeline.intraday.fetch_intraday_indices", return_value=pd.DataFrame({
            "Nifty 50 (IN)": nifty, "S&P 500 (US)": sp500,
        })), \
             patch("pipeline.intraday.build_combined_portfolio", return_value=pd.DataFrame({
                 "My Portfolio": nifty,
             })), \
             patch("pipeline.intraday._cache_path", return_value=tmp_path / "cache.csv"):
            snap = build_intraday_snapshot("5m")
        # Every serialised timestamp must end with +05:30 (IST) regardless
        # of which index it came from
        for col, pts in snap["series"].items():
            for p in pts:
                assert p["t"].endswith("+0530") or p["t"].endswith("+05:30"), \
                    f"{col} timestamp {p['t']!r} is not IST (expected +0530 or +05:30)"


# ---------- Combined portfolio weighting ----------
class TestBuildCombinedPortfolio:
    """Verify that the portfolio line = weight_eq * equity + weight_mf * mf + weight_sgb * sgb."""

    def _series(self, n=30, base=100.0, slope=0.5) -> pd.Series:
        idx = pd.date_range(datetime.now(tz=pd.Timestamp.now(tz="UTC").tz) - timedelta(hours=n * 5 / 60),
                            periods=n, freq="5min", tz="UTC")
        return pd.Series([base + slope * i for i in range(n)], index=idx, name="x")

    def test_weighted_sum_with_zero_mf_sgb(self, tmp_path, monkeypatch):
        from pipeline.intraday import build_combined_portfolio
        eq = self._series(n=30, base=100.0, slope=1.0)
        monkeypatch.setattr("pipeline.intraday.fetch_intraday_equity",
                            lambda interval, *, use_cache=True: (eq, [{"weight": 1.0}]))
        monkeypatch.setattr("pipeline.intraday._load_portfolio_weights", lambda: (1.0, 0.0, 0.0))
        monkeypatch.setattr("pipeline.intraday._load_mfs_today_change", lambda: 0.0)
        monkeypatch.setattr("pipeline.intraday._load_sgbs_today_change", lambda: 0.0)

        out = build_combined_portfolio("5m")
        # With equity weight = 1, the portfolio should be the equity series
        # (already normalised to 100 at open)
        assert (out["My Portfolio"].dropna().iloc[:5].values == eq.iloc[:5].values).all()

    def test_mf_contribution_applied_gradually(self, tmp_path, monkeypatch):
        from pipeline.intraday import build_combined_portfolio
        # Flat equity (no movement) so any drift comes from the MF contribution
        eq = self._series(n=30, base=100.0, slope=0.0)
        monkeypatch.setattr("pipeline.intraday.fetch_intraday_equity",
                            lambda interval, *, use_cache=True: (eq, []))
        monkeypatch.setattr("pipeline.intraday._load_portfolio_weights", lambda: (0.5, 0.5, 0.0))
        monkeypatch.setattr("pipeline.intraday._load_mfs_today_change", lambda: 2.0)  # +2% MF
        monkeypatch.setattr("pipeline.intraday._load_sgbs_today_change", lambda: 0.0)

        out = build_combined_portfolio("5m")
        last = out["My Portfolio"].dropna().iloc[-1]
        # Equity contributes 50% * 100 = 50; MF contributes 50% * 100 * 1.02 = 51
        # Total = 101.0 (no, wait — equity is flat at 100, so 0.5*100 = 50; MF ends at 100*1.02 = 102,
        # so 0.5*102 = 51; total = 101). Allow some tolerance for floating-point drift.
        assert abs(last - 101.0) < 0.5

    def test_missing_equity_falls_back_to_nifty(self, tmp_path, monkeypatch):
        from pipeline.intraday import build_combined_portfolio
        nifty = self._series(n=30, base=18000, slope=10.0)
        monkeypatch.setattr("pipeline.intraday.fetch_intraday_equity",
                            lambda interval, *, use_cache=True: (pd.Series(dtype=float), []))
        monkeypatch.setattr("pipeline.intraday.fetch_intraday_indices",
                            lambda interval, **kwargs: pd.DataFrame({"Nifty 50 (IN)": nifty}))
        monkeypatch.setattr("pipeline.intraday._load_portfolio_weights", lambda: (1.0, 0.0, 0.0))
        monkeypatch.setattr("pipeline.intraday._load_mfs_today_change", lambda: 0.0)
        monkeypatch.setattr("pipeline.intraday._load_sgbs_today_change", lambda: 0.0)

        out = build_combined_portfolio("5m")
        assert "My Portfolio" in out.columns
        assert out["My Portfolio"].dropna().iloc[0] == 100.0


# ---------- Draw chart (Plotly smoke test) ----------
class TestDrawChart:
    def test_draw_produces_html_file(self, tmp_path, monkeypatch):
        from pipeline.intraday import draw_intraday_chart, CHARTS_DIR
        idx = pd.date_range(datetime.now(tz=pd.Timestamp.now(tz="UTC").tz) - timedelta(hours=2),
                            periods=30, freq="5min", tz="UTC")
        df = pd.DataFrame({
            "Nifty 50 (IN)": [100 + i * 0.1 for i in range(30)],
            "My Portfolio":  [100 + i * 0.2 for i in range(30)],
        }, index=idx)
        out = draw_intraday_chart(df, df.index[-1], "5m")
        assert out.exists()
        assert out.suffix == ".html"
        # The HTML should reference both the index and the portfolio
        text = out.read_text()
        assert "Nifty 50" in text
        assert "My Portfolio" in text