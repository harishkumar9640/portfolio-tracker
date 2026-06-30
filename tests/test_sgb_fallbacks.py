"""
Tests for pipeline.mf_sgb.fetch_sgb_rows covering all 4 price-source fallbacks:

  1. NSE (preferred) — matches what Angel One shows in your demat
  2. Mintbyte (Motilal Oswal) — fallback when NSE doesn't list the bond
  3. Manual price in sgbs.json — user-provided fallback
  4. IBJA gold-spot proxy — last-resort fallback when nothing else works

Each test mocks out the upstream dependencies so we exercise the
decision tree without hitting the network.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

import pipeline.mf_sgb  # noqa: E402


def _make_holding(isin: str, **overrides) -> dict:
    """Build a minimal sgbs.json entry, overriding defaults."""
    base = {
        "isin": isin, "units": 1, "name": f"SGB {isin}",
        "buy_date": "2023-01-01", "invested_per_g": 5000,
    }
    base.update(overrides)
    return base


@pytest.fixture(autouse=True)
def reset_module_state():
    """Reset pipeline.mf_sgb's module-level cache before every test."""
    pipeline.mf_sgb._ISIN_TO_NSE_SYMBOL = {}
    pipeline.mf_sgb._NSE_SGB_DATA = []
    pipeline.mf_sgb._NSE_SGB_FETCHED_AT = 0.0
    pipeline.mf_sgb._sgb_history_cache = None

@pytest.fixture(autouse=True)
def no_network_calls(monkeypatch):
    """Block ALL upstream network calls so every test runs offline.

    Tests that need to inject specific behaviour (a non-empty NSE
    universe, a specific mintbyte response, etc.) should re-patch
    inside the test body; this autouse fixture only ensures the
    *default* behaviour is no-op.
    """
    # Reset module-level cache between tests
    pipeline.mf_sgb._ISIN_TO_NSE_SYMBOL = {}
    pipeline.mf_sgb._NSE_SGB_DATA = []
    pipeline.mf_sgb._NSE_SGB_FETCHED_AT = 0.0
    pipeline.mf_sgb._sgb_history_cache = None

    # Patch upstream fetchers to return empty / None by default
    monkeypatch.setattr(pipeline.mf_sgb, "_fetch_nse_sgb_universe", lambda: [])
    monkeypatch.setattr(pipeline.mf_sgb, "_build_isin_to_nse_symbol_map", lambda: {})
    monkeypatch.setattr(pipeline.mf_sgb, "fetch_mintbyte_with_history", lambda: {})
    monkeypatch.setattr(pipeline.mf_sgb, "_fetch_ibja_gold_price",
                        lambda **kw: (None, None))
    yield


# ---------- Path 1: NSE (preferred) ----------

class TestNSEPath:
    def test_nse_used_when_available(self):
        """When NSE returns data for an ISIN, that price wins."""
        nse_universe = [{
            "symbol": "SGBFEB32IV", "ltP": "14950.00",
            "prevClose": "15129.77", "maturityDate": "B32IV",
        }]
        isin_map = {"IN0020230184": "SGBFEB32IV"}
        # Re-patch to provide non-empty NSE data and ISIN map.
        with patch.object(pipeline.mf_sgb, "_fetch_nse_sgb_universe",
                          return_value=nse_universe), \
             patch.object(pipeline.mf_sgb, "_build_isin_to_nse_symbol_map",
                          return_value=isin_map), \
             patch.object(pipeline.mf_sgb, "fetch_mintbyte_with_history",
                          return_value={"IN0020230184":
                                       {"price": 99999.99, "date": "2026-06-22"}}):
            rows = pipeline.mf_sgb.fetch_sgb_rows(
                [_make_holding("IN0020230184")],
                asof=None,
            )
        assert rows[0].value == 14950.0
        assert "NSE" in rows[0].extra["source"]

    def test_nse_price_persisted_to_history(self):
        """Successful NSE lookups write to pipeline.history_db for next time."""
        nse_universe = [{
            "symbol": "SGBFEB32IV", "ltP": "14950.00",
            "prevClose": "15129.77", "maturityDate": "B32IV",
        }]
        isin_map = {"IN0020230184": "SGBFEB32IV"}
        # Use a real tmp DB so we exercise the real persist path.
        tmp_db_path = Path("/tmp/test_sgb_history_regression.db")
        if tmp_db_path.exists():
            tmp_db_path.unlink()
        from pipeline.history_db import HistoryDB
        from unittest.mock import patch as mp
        with patch.object(pipeline.mf_sgb, "_fetch_nse_sgb_universe",
                          return_value=nse_universe), \
             patch.object(pipeline.mf_sgb, "_build_isin_to_nse_symbol_map",
                          return_value=isin_map), \
             mp.object(pipeline.mf_sgb, "HistoryDB",
                       lambda *a, **kw: HistoryDB(tmp_db_path)):
            pipeline.mf_sgb.fetch_sgb_rows([_make_holding("IN0020230184")])
            db = HistoryDB(tmp_db_path)
            rows = db.sgb_history("IN0020230184")
            assert len(rows) == 1
            assert rows[0]["price"] == 14950.0
        if tmp_db_path.exists():
            tmp_db_path.unlink()


# ---------- Path 2: Mintbyte fallback ----------

class TestMintbyteFallback:
    def test_mintbyte_used_when_nse_unavailable(self):
        """If NSE doesn't have the ISIN, mintbyte price is used."""
        pipeline.mf_sgb._NSE_SGB_DATA = []   # empty NSE universe
        pipeline.mf_sgb._NSE_SGB_FETCHED_AT = float("inf")
        pipeline.mf_sgb._ISIN_TO_NSE_SYMBOL = {}   # no mapping

        with patch.object(pipeline.mf_sgb, "fetch_mintbyte_with_history",
                          return_value={"IN0020230184":
                                       {"price": 15302.74, "date": "2026-06-22"}}), \
             patch.object(pipeline.mf_sgb, "get_sgb_prev_price",
                          return_value=(15400.00, "2026-06-22")):
            rows = pipeline.mf_sgb.fetch_sgb_rows([_make_holding("IN0020230184")])
        assert rows[0].value == 15302.74
        assert "mintbyte" in rows[0].extra["source"]

    def test_mintbyte_with_no_history_uses_today_as_prev(self):
        """First-time mintbyte fetch: no prev cached, so pct = 0."""
        pipeline.mf_sgb._NSE_SGB_DATA = []
        pipeline.mf_sgb._NSE_SGB_FETCHED_AT = float("inf")
        pipeline.mf_sgb._ISIN_TO_NSE_SYMBOL = {}

        with patch.object(pipeline.mf_sgb, "fetch_mintbyte_with_history",
                          return_value={"IN0020230184":
                                       {"price": 15302.74, "date": "2026-06-22"}}), \
             patch.object(pipeline.mf_sgb, "get_sgb_prev_price",
                          return_value=(None, None)):
            rows = pipeline.mf_sgb.fetch_sgb_rows([_make_holding("IN0020230184")])
        assert rows[0].pct == 0.0
        assert "today only" in rows[0].extra["source"]


# ---------- Path 3: Manual price ----------

class TestManualPriceFallback:
    def test_manual_used_when_nse_and_mintbyte_unavailable(self):
        """If both NSE and mintbyte fail, manual_price_per_g is used."""
        pipeline.mf_sgb._NSE_SGB_DATA = []
        pipeline.mf_sgb._NSE_SGB_FETCHED_AT = float("inf")
        pipeline.mf_sgb._ISIN_TO_NSE_SYMBOL = {}

        with patch.object(pipeline.mf_sgb, "fetch_mintbyte_with_history",
                          return_value={}):
            rows = pipeline.mf_sgb.fetch_sgb_rows([_make_holding(
                "IN0020999999", manual_price_per_g=14800.0,
                manual_prev_price_per_g=15000.0,
            )])
        assert rows[0].value == 14800.0
        assert "manual" in rows[0].extra["source"]

    def test_manual_current_only_yields_zero_pct(self):
        """manual_price_per_g without manual_prev_price_per_g -> pct=0."""
        pipeline.mf_sgb._NSE_SGB_DATA = []
        pipeline.mf_sgb._NSE_SGB_FETCHED_AT = float("inf")
        pipeline.mf_sgb._ISIN_TO_NSE_SYMBOL = {}

        with patch.object(pipeline.mf_sgb, "fetch_mintbyte_with_history",
                          return_value={}):
            rows = pipeline.mf_sgb.fetch_sgb_rows([_make_holding(
                "IN0020999999", manual_price_per_g=14800.0,
            )])
        assert rows[0].pct == 0.0
        assert "current only" in rows[0].extra["source"]


# ---------- Path 4: IBJA gold-spot proxy ----------

class TestIBJAFallback:
    def test_ibja_used_when_all_else_fails(self):
        """If NSE, mintbyte, and manual all fail, IBJA gold price is used."""
        pipeline.mf_sgb._NSE_SGB_DATA = []
        pipeline.mf_sgb._NSE_SGB_FETCHED_AT = float("inf")
        pipeline.mf_sgb._ISIN_TO_NSE_SYMBOL = {}

        with patch.object(pipeline.mf_sgb, "fetch_mintbyte_with_history",
                          return_value={}), \
             patch.object(pipeline.mf_sgb, "_fetch_ibja_gold_price",
                          side_effect=[(14478.80, "23/06/2026"),
                                       (14666.40, "22/06/2026")]):
            rows = pipeline.mf_sgb.fetch_sgb_rows([_make_holding("IN0020999999")])
        assert rows[0].value == 14478.80
        assert "IBJA" in rows[0].extra["source"]

    def test_ibja_only_today_no_prev(self):
        """IBJA returns only today's price (no prev) -> pct=0."""
        pipeline.mf_sgb._NSE_SGB_DATA = []
        pipeline.mf_sgb._NSE_SGB_FETCHED_AT = float("inf")
        pipeline.mf_sgb._ISIN_TO_NSE_SYMBOL = {}

        with patch.object(pipeline.mf_sgb, "fetch_mintbyte_with_history",
                          return_value={}), \
             patch.object(pipeline.mf_sgb, "_fetch_ibja_gold_price",
                          side_effect=[(14478.80, "23/06/2026"),
                                       (None, None)]):
            rows = pipeline.mf_sgb.fetch_sgb_rows([_make_holding("IN0020999999")])
        assert rows[0].value == 14478.80
        assert rows[0].pct == 0.0


# ---------- Edge case: no price available anywhere ----------

class TestNoPriceAnywhere:
    def test_warns_and_returns_empty_value_when_no_source(self):
        """All four sources fail -> warning log, value=0, no exception."""
        pipeline.mf_sgb._NSE_SGB_DATA = []
        pipeline.mf_sgb._NSE_SGB_FETCHED_AT = float("inf")
        pipeline.mf_sgb._ISIN_TO_NSE_SYMBOL = {}

        with patch.object(pipeline.mf_sgb, "fetch_mintbyte_with_history",
                          return_value={}), \
             patch.object(pipeline.mf_sgb, "_fetch_ibja_gold_price",
                          return_value=(None, None)):
            rows = pipeline.mf_sgb.fetch_sgb_rows([_make_holding("IN0020999999")])
        assert rows[0].value == 0.0
        assert rows[0].pct == 0.0
        assert "error" in rows[0].extra


# ---------- Multiple holdings ----------

class TestMultipleHoldings:
    def test_handles_mixed_sources_in_one_call(self):
        """Two SGBs: one via NSE, one via IBJA fallback."""
        nse_universe = [{
            "symbol": "SGBFEB32IV", "ltP": "14950.00",
            "prevClose": "15129.77", "maturityDate": "B32IV",
        }]
        isin_map = {"IN0020230184": "SGBFEB32IV"}

        with patch.object(pipeline.mf_sgb, "_fetch_nse_sgb_universe",
                          return_value=nse_universe), \
             patch.object(pipeline.mf_sgb, "_build_isin_to_nse_symbol_map",
                          return_value=isin_map), \
             patch.object(pipeline.mf_sgb, "fetch_mintbyte_with_history",
                          return_value={"IN0020999999":
                                       {"price": 14000.0, "date": "2026-06-22"}}), \
             patch.object(pipeline.mf_sgb, "get_sgb_prev_price",
                          return_value=(14100.0, "2026-06-22")):
            rows = pipeline.mf_sgb.fetch_sgb_rows([
                _make_holding("IN0020230184"),     # NSE wins
                _make_holding("IN0020999999"),     # mintbyte wins
            ])
        assert rows[0].value == 14950.0   # NSE
        assert rows[1].value == 14000.0   # mintbyte

    def test_skips_entries_with_zero_units(self):
        """Holdings with units <= 0 are skipped (no spurious rows)."""
        pipeline.mf_sgb._NSE_SGB_DATA = []
        pipeline.mf_sgb._NSE_SGB_FETCHED_AT = float("inf")
        pipeline.mf_sgb._ISIN_TO_NSE_SYMBOL = {}
        with patch.object(pipeline.mf_sgb, "fetch_mintbyte_with_history", return_value={}):
            rows = pipeline.mf_sgb.fetch_sgb_rows([
                _make_holding("IN0020230184", units=0),
                _make_holding("IN0020999999", units=-1),
            ])
        assert rows == []
