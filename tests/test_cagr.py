"""Tests for pipeline.cagr (equity CAGR vs Nifty50, per-lot with per-lot prices)."""
from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path
from unittest.mock import patch

import pytest

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

from pipeline import cagr as cagr_mod
from pipeline.cagr import (
    MIN_DAYS_HELD,
    Lot,
    cagr as cagr_fn,
    compute_equity_cagr,
    nifty_close_on,
    reconstruct_lots,
    get_equity_cagr,
)


# ---------- Pure math ----------

class TestCagrMath:
    def test_double_in_two_years(self):
        r = cagr_fn(200, 100, 2.0)
        assert r is not None
        assert abs(r - 41.4213) < 0.01

    def test_double_in_one_year(self):
        r = cagr_fn(200, 100, 1.0)
        assert r is not None
        assert abs(r - 100.0) < 1e-6

    def test_50pct_in_one_year(self):
        assert abs(cagr_fn(150, 100, 1.0) - 50.0) < 1e-6

    def test_loss(self):
        r = cagr_fn(50, 100, 2.0)
        assert r is not None
        assert abs(r - (-29.289)) < 0.01

    def test_zero_years_returns_none(self):
        assert cagr_fn(200, 100, 0) is None
        assert cagr_fn(200, 100, -1) is None

    def test_zero_ref_returns_none(self):
        assert cagr_fn(200, 0, 1) is None

    def test_zero_stock_returns_none(self):
        assert cagr_fn(0, 100, 1) is None


# ---------- Nifty lookup ----------

class TestNiftyCloseOn:
    def test_returns_value_for_known_date(self):
        v = nifty_close_on(date(2026, 1, 15))
        assert v is not None
        assert 25000 < v < 26000

    def test_returns_prior_trading_day_for_weekend(self):
        v_sat = nifty_close_on(date(2026, 7, 4))
        v_fri = nifty_close_on(date(2026, 7, 3))
        assert v_sat == v_fri
        assert v_sat is not None


# ---------- reconstruct_lots ----------

class TestReconstructLots:
    """Test per-lot reconstruction with the v8 algorithm signature:
    reconstruct_lots(ticker, snapshots, breakup_snapshots, all_sells, all_buys,
                     current_qty, current_avg)
    """

    def test_no_xlsx_data_uses_manual(self):
        lots = reconstruct_lots("UNOMINDA", [], [], [], [],
                                current_qty=36, current_avg=1096.7)
        assert len(lots) == 1
        assert lots[0].buy_date == date(2026, 7, 1)
        assert lots[0].source == "manual"

    def test_first_seen_lot_price_uses_next_snapshot(self):
        # GOLDBEES-like: 500 @ 60.76 first, 200 @ 60.21 second (after sells).
        # The first_seen lot's true price = 60.21 (the snapshot where it's
        # the only remaining lot, with no new lots added yet).
        snaps = [
            (date(2025, 3, 31), {"X": {"qty": 500, "avg_price": 60.76}}),
            (date(2026, 3, 31), {"X": {"qty": 200, "avg_price": 60.21}}),
            (date(2026, 7, 4), {"X": {"qty": 300, "avg_price": 81.42}}),
        ]
        # 300 sold in FY 2025-26 at avg buy 61.07
        sells = [
            {"ticker": "X", "qty": 250, "buy_date": date(2024, 5, 28),
             "sell_date": date(2025, 6, 27), "buy_price": 61.29},
            {"ticker": "X", "qty": 50, "buy_date": date(2024, 7, 23),
             "sell_date": date(2025, 9, 10), "buy_price": 60.20},
        ]
        lots = reconstruct_lots("X", snaps, [], sells, [],
                                current_qty=300, current_avg=81.42)
        # 2 lots expected: first_seen (200 @ ~60.21) and qty_breakup (100 @ ~123.84)
        assert len(lots) == 2
        first_seen = [l for l in lots if l.source == "first_seen"][0]
        assert first_seen.qty == 200
        # Price should be 60.21 (from the snapshot where it's the only lot)
        assert abs(first_seen.buy_price - 60.21) < 0.05
        # The qty_breakup lot (100 shares) should be backed out
        second = [l for l in lots if l.source == "qty_breakup"][0]
        assert second.qty == 100
        # (300 × 81.42 - 200 × 60.21) / 100 = (24426 - 12042) / 100 = 123.84
        assert abs(second.buy_price - 123.84) < 0.5

    def test_qty_breakup_lot_price_backout(self):
        # Simpler: 2 snapshots, position grew 100 -> 200, no sells.
        snaps = [
            (date(2024, 3, 31), {"X": {"qty": 100, "avg_price": 100.0}}),
            (date(2025, 3, 31), {"X": {"qty": 200, "avg_price": 150.0}}),
        ]
        lots = reconstruct_lots("X", snaps, [], [], [],
                                current_qty=200, current_avg=150.0)
        # 1 first_seen lot + 1 qty_breakup lot
        assert len(lots) == 2
        first = [l for l in lots if l.source == "first_seen"][0]
        assert first.qty == 100
        # first_seen: no sells happened, so the first_seen lot's price
        # = the next snapshot's avg (where the position equals lot.qty)
        # = 100.0 (since position never went to 100 again after the first
        # snap). Actually 100 is the first_snap avg, so first_seen uses it.
        assert first.buy_price == 100.0

    def test_fifo_reduces_qty(self):
        # 100 shares bought, 50 sold. Lot should have 50 remaining.
        snaps = [(date(2024, 3, 31), {"X": {"qty": 100, "avg_price": 100.0}})]
        sells = [
            {"ticker": "X", "qty": 50, "buy_date": date(2024, 1, 1),
             "sell_date": date(2024, 6, 1), "buy_price": 100.0},
        ]
        lots = reconstruct_lots("X", snaps, [], sells, [],
                                current_qty=50, current_avg=100.0)
        assert len(lots) == 1
        assert lots[0].qty == 50


# ---------- compute_equity_cagr (full pipeline) ----------

class TestComputeEquityCagr:
    """Test the full computation with mocked truth + LTPs."""

    TRUTH = {
        "equity": {
            "STABLE":  {"ticker": "STABLE",  "qty": 100, "avg_price": 100.0},
            "DOUBLED": {"ticker": "DOUBLED", "qty":  50, "avg_price": 200.0},
            "HALVED":  {"ticker": "HALVED",  "qty": 200, "avg_price":  50.0},
            "NEW":     {"ticker": "NEW",     "qty":  10, "avg_price": 500.0},
        }
    }

    LTPS = {
        "STABLE":  100.0,
        "DOUBLED": 400.0,
        "HALVED":   25.0,
        "NEW":     510.0,
    }

    def _setup_snapshots(self):
        return [
            (date(2024, 3, 31), {
                "STABLE":  {"qty": 100, "avg_price": 100.0},
                "DOUBLED": {"qty":  50, "avg_price": 200.0},
                "HALVED":  {"qty": 200, "avg_price":  50.0},
            }),
            (date(2025, 3, 31), {
                "STABLE":  {"qty": 100, "avg_price": 100.0},
                "DOUBLED": {"qty":  50, "avg_price": 200.0},
                "HALVED":  {"qty": 200, "avg_price":  50.0},
                "NEW":     {"qty":  10, "avg_price": 500.0},
            }),
        ]

    def _patches(self):
        return [
            patch.object(cagr_mod, "load_truth", return_value=self.TRUTH),
            patch.object(cagr_mod, "_get_live_snapshot",
                         return_value=(self.LTPS, "mock")),
            patch.object(cagr_mod, "_xlsx_snapshots",
                         return_value=self._setup_snapshots()),
            patch.object(cagr_mod, "_xlsx_breakup_snapshots",
                         return_value=self._setup_snapshots()),
            patch.object(cagr_mod, "_all_xlsx_sells", return_value=[]),
            patch.object(cagr_mod, "_all_xlsx_buys", return_value=[]),
        ]

    def test_per_lot_rows_present(self):
        ps = self._patches()
        with ps[0], ps[1], ps[2], ps[3], ps[4], ps[5]:
            rep = compute_equity_cagr()
        # 4 tickers, 4 lots (each has 1 lot — no sells, no new buys within snapshots)
        assert len(rep["lots"]) == 4

    def test_per_ticker_summaries_present(self):
        ps = self._patches()
        with ps[0], ps[1], ps[2], ps[3], ps[4], ps[5]:
            rep = compute_equity_cagr()
        s = {t["ticker"]: t for t in rep["ticker_summaries"]}
        assert set(s.keys()) == {"STABLE", "DOUBLED", "HALVED", "NEW"}

    def test_aggregate_total_invested(self):
        ps = self._patches()
        with ps[0], ps[1], ps[2], ps[3], ps[4], ps[5]:
            rep = compute_equity_cagr()
        a = rep["aggregate"]
        # 100*100 + 50*200 + 200*50 + 10*500 = 10000+10000+10000+5000 = 35000
        assert a["total_invested"] == 35000.0


# ---------- Cache ----------

class TestCache:
    def test_cache_round_trip(self, tmp_path, monkeypatch):
        cache_file = tmp_path / "cagr_cache.json"
        monkeypatch.setattr(cagr_mod, "CACHE_FILE", cache_file)
        cagr_mod._write_cache("k1", {"x": 1, "y": [1, 2]})
        got = cagr_mod._read_cache("k1")
        assert got == {"x": 1, "y": [1, 2]}
        assert cagr_mod._read_cache("missing") is None

    def test_cache_bounds_to_20_entries(self, tmp_path, monkeypatch):
        cache_file = tmp_path / "cagr_cache.json"
        monkeypatch.setattr(cagr_mod, "CACHE_FILE", cache_file)
        for i in range(25):
            cagr_mod._write_cache(f"k{i}", {"i": i})
        with cache_file.open() as fh:
            d = json.load(fh)
        assert len(d) == 20
        assert "k0" not in d
        assert "k24" in d
