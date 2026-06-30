"""
Tests for the SGB price-fetching pipeline.
We mock all network calls so tests run offline.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

import pipeline.mf_sgb  # noqa: E402
from pipeline.history_db import HistoryDB  # noqa: E402


@pytest.fixture
def tmp_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Fresh SQLite DB for each test."""
    monkeypatch.setattr("pipeline.mf_sgb.HistoryDB", lambda *a, **kw: HistoryDB(tmp_path / "test.db"))


# Mock data representing NSE's /api/sovereign-gold-bonds response.
NSE_UNIVERSE = [
    {
        "symbol": "SGBFEB32IV",
        "open": "14950", "high": "15050", "low": "14900",
        "issue_price": "6213",
        "ltP": "14950",
        "chn": "-179.77",
        "per": "-1.19",
        "prevClose": "15129.77",
        "maturityDate": "B32IV",
    },
    {
        "symbol": "SGBJUN31I",
        "open": "14700", "high": "14750", "low": "14680",
        "issue_price": "5926",
        "ltP": "14690",
        "chn": "-157.91",
        "per": "-1.06",
        "prevClose": "14847.91",
        "maturityDate": "JUN31I",
    },
]


# Mintbyte HTML containing the ISIN -> symbol mappings.
# In the real page the NSE symbol appears IMMEDIATELY BEFORE the ISIN
# cell in the row text (the table is laid out right-to-left). The
# backward-search in _build_isin_to_nse_symbol_map relies on that.
MINTBYTE_HTML = """
<table>
  <tr>
    <td>SGBFEB32IV</td>
    <td>IN0020230184</td>
    <td>SGB IV</td>
    <td>₹6,263</td>
    <td>2.50%</td>
    <td>28 Feb 2032</td>
    <td>₹15,302.74</td>
  </tr>
  <tr>
    <td>SGBJUN31I</td>
    <td>IN0020230069</td>
    <td>SGB I</td>
    <td>₹5,926</td>
    <td>2.50%</td>
    <td>28 Jun 2031</td>
    <td>₹15,010.11</td>
  </tr>
</table>
"""


class TestBuildIsinToNseSymbolMap:
    def setup_method(self):
        # Clear the module-level cache
        pipeline.mf_sgb._ISIN_TO_NSE_SYMBOL = {}

    def test_extracts_both_isins(self, monkeypatch):
        # Mock mintbyte HTML
        mock_resp = MagicMock()
        mock_resp.text = MINTBYTE_HTML
        mock_resp.raise_for_status = MagicMock()

        monkeypatch.setattr(pipeline.mf_sgb.requests, "get", lambda *a, **kw: mock_resp)
        result = pipeline.mf_sgb._build_isin_to_nse_symbol_map()
        assert result["IN0020230184"] == "SGBFEB32IV"
        assert result["IN0020230069"] == "SGBJUN31I"
        assert len(result) == 2

    def test_handles_fetch_failure(self, monkeypatch):
        def boom(*a, **kw):
            raise pipeline.mf_sgb.requests.RequestException("timeout")
        monkeypatch.setattr(pipeline.mf_sgb.requests, "get", boom)
        result = pipeline.mf_sgb._build_isin_to_nse_symbol_map()
        assert result == {}

    def test_is_cached_after_first_call(self, monkeypatch):
        mock_resp = MagicMock()
        mock_resp.text = MINTBYTE_HTML
        mock_resp.raise_for_status = MagicMock()
        call_count = {"n": 0}

        def counting_get(*a, **kw):
            call_count["n"] += 1
            return mock_resp

        monkeypatch.setattr(pipeline.mf_sgb.requests, "get", counting_get)
        pipeline.mf_sgb._build_isin_to_nse_symbol_map()
        pipeline.mf_sgb._build_isin_to_nse_symbol_map()
        assert call_count["n"] == 1, "second call should hit the in-memory cache"


class TestFetchNseQuote:
    def setup_method(self):
        pipeline.mf_sgb._ISIN_TO_NSE_SYMBOL = {}
        pipeline.mf_sgb._NSE_SGB_DATA = []
        pipeline.mf_sgb._NSE_SGB_FETCHED_AT = 0

    def test_returns_price_for_known_isin(self, monkeypatch):
        monkeypatch.setattr(pipeline.mf_sgb, "_fetch_nse_sgb_universe", lambda: NSE_UNIVERSE)
        monkeypatch.setattr(pipeline.mf_sgb, "_build_isin_to_nse_symbol_map",
                            lambda: {"IN0020230184": "SGBFEB32IV"})
        result = pipeline.mf_sgb._fetch_nse_quote("IN0020230184")
        assert result is not None
        assert result["lastPrice"] == 14950.0
        assert result["previousClose"] == 15129.77
        assert result["symbol"] == "SGBFEB32IV"

    def test_returns_none_for_unknown_isin(self, monkeypatch):
        monkeypatch.setattr(pipeline.mf_sgb, "_fetch_nse_sgb_universe", lambda: NSE_UNIVERSE)
        monkeypatch.setattr(pipeline.mf_sgb, "_build_isin_to_nse_symbol_map", lambda: {})
        result = pipeline.mf_sgb._fetch_nse_quote("IN0000000000")
        assert result is None

    def test_returns_none_when_nse_unavailable(self, monkeypatch):
        monkeypatch.setattr(pipeline.mf_sgb, "_fetch_nse_sgb_universe", lambda: None)
        result = pipeline.mf_sgb._fetch_nse_quote("IN0020230184")
        assert result is None

    def test_returns_none_when_symbol_not_in_universe(self, monkeypatch):
        monkeypatch.setattr(pipeline.mf_sgb, "_fetch_nse_sgb_universe", lambda: NSE_UNIVERSE)
        monkeypatch.setattr(pipeline.mf_sgb, "_build_isin_to_nse_symbol_map",
                            lambda: {"IN0020230184": "SGBXX99ZZ"})
        result = pipeline.mf_sgb._fetch_nse_quote("IN0020230184")
        assert result is None

    def test_universe_is_cached_for_5_minutes(self, monkeypatch):
        # First call fetches; second call within 5min returns cached.
        # We don't actually call NSE — just inspect the cache logic.
        pipeline.mf_sgb._NSE_SGB_DATA = NSE_UNIVERSE
        pipeline.mf_sgb._NSE_SGB_FETCHED_AT = pipeline.mf_sgb.time.monotonic()
        result = pipeline.mf_sgb._fetch_nse_sgb_universe()
        assert result == NSE_UNIVERSE


class TestSgbPricePriority:
    """Integration test: fetch_sgb_rows uses NSE > mintbyte > manual > IBJA."""

    def setup_method(self):
        pipeline.mf_sgb._ISIN_TO_NSE_SYMBOL = {}
        pipeline.mf_sgb._NSE_SGB_DATA = []
        pipeline.mf_sgb._NSE_SGB_FETCHED_AT = 0

    def test_nse_takes_priority_over_mintbyte(
        self, tmp_db, monkeypatch,
    ):
        # Both NSE and mintbyte return data; NSE should win.
        monkeypatch.setattr(pipeline.mf_sgb, "_fetch_nse_sgb_universe", lambda: NSE_UNIVERSE)
        monkeypatch.setattr(pipeline.mf_sgb, "_build_isin_to_nse_symbol_map",
                            lambda: {"IN0020230184": "SGBFEB32IV"})
        # mintbyte would return ₹15,302 — but should be ignored because NSE wins.
        monkeypatch.setattr(pipeline.mf_sgb, "fetch_mintbyte_with_history", lambda: {
            "IN0020230184": {"price": 15302.74, "date": "2026-06-22"},
        })
        monkeypatch.setattr(pipeline.mf_sgb, "_fetch_ibja_gold_price",
                            lambda **kw: (14478.80, "23/06/2026"))
        monkeypatch.setattr(pipeline.mf_sgb, "fetch_mf_rows", lambda mfs: [])

        rows = pipeline.mf_sgb.fetch_sgb_rows([
            {"isin": "IN0020230184", "units": 1, "name": "SGB 2022-23 IV"},
        ])
        assert len(rows) == 1
        assert rows[0].value == 14950.0        # NSE price, not mintbyte's 15302
        assert "NSE" in rows[0].extra["source"]

    def test_falls_back_to_mintbyte_when_nse_missing(
        self, tmp_db, monkeypatch,
    ):
        # NSE returns no universe → fall back to mintbyte.
        monkeypatch.setattr(pipeline.mf_sgb, "_fetch_nse_sgb_universe", lambda: None)
        monkeypatch.setattr(pipeline.mf_sgb, "_build_isin_to_nse_symbol_map", lambda: {})
        monkeypatch.setattr(pipeline.mf_sgb, "fetch_mintbyte_with_history", lambda: {
            "IN0020230184": {"price": 15302.74, "date": "2026-06-22"},
        })
        monkeypatch.setattr(pipeline.mf_sgb, "_fetch_ibja_gold_price",
                            lambda **kw: (14478.80, "23/06/2026"))

        rows = pipeline.mf_sgb.fetch_sgb_rows([
            {"isin": "IN0020230184", "units": 1, "name": "SGB IV"},
        ])
        assert len(rows) == 1
        assert rows[0].value == 15302.74
        assert "mintbyte" in rows[0].extra["source"]

    def test_nse_price_persisted_to_history(
        self, tmp_db, monkeypatch,
    ):
        # Verify that a successful NSE fetch writes to the DB so future
        # runs have an authoritative prev-day anchor.
        monkeypatch.setattr(pipeline.mf_sgb, "_fetch_nse_sgb_universe", lambda: NSE_UNIVERSE)
        monkeypatch.setattr(pipeline.mf_sgb, "_build_isin_to_nse_symbol_map",
                            lambda: {"IN0020230184": "SGBFEB32IV"})
        monkeypatch.setattr(pipeline.mf_sgb, "fetch_mintbyte_with_history", lambda: {})
        monkeypatch.setattr(pipeline.mf_sgb, "_fetch_ibja_gold_price",
                            lambda **kw: (None, None))

        from datetime import date
        before = pipeline.mf_sgb.HistoryDB().sgb_history("IN0020230184")
        pipeline.mf_sgb.fetch_sgb_rows([
            {"isin": "IN0020230184", "units": 1, "name": "SGB IV"},
        ])
        after = pipeline.mf_sgb.HistoryDB().sgb_history("IN0020230184")
        assert len(after) == len(before) + 1
        # The new row should have today's date and the NSE price.
        today = date.today().isoformat()
        new_rows = [r for r in after if r["date"] == today]
        assert new_rows, f"no row for {today}"
        assert new_rows[0]["price"] == 14950.0
        assert new_rows[0]["source"].startswith("NSE/")