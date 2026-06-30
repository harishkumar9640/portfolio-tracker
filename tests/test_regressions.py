"""
Regression tests for bugs we've fixed in the past.

Each test name references the commit/PR that introduced the fix so a
future regression can be traced back to the original bug report.

If you fix a bug, ADD a regression test here. If a regression test
fails, it means somebody re-introduced a previously-fixed bug.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))


# ---------- Equity / portfolio regressions ----------
class TestEquityPrevFix:
    """Regression: pipeline.equity_compare.build_snapshot() used to skip
    computing equity_prev, so the Total Portfolio % under-weighted
    equity when MF/SGB data was present.
    """

    def test_build_snapshot_populates_equity_prev(self):
        """build_snapshot() must always include equity.prev_value so the
        Total Portfolio day-% correctly weights equity.
        """
        from pipeline.equity_compare import build_snapshot
        # Pass a synthetic mock to avoid broker/NSE network calls.
        mock_holdings = [type("H", (), {
            "symbol": "X", "quantity": 10,
            "avg_price": 100.0, "ltp": 110.0, "prev_close": 105.0,
            "current_value": 1100.0, "pnl": 100.0, "pnl_pct": 10.0,
        })()]
        snap = build_snapshot(_mock={
            "indices": None,        # empty
            "holdings": mock_holdings,
            "mf_assets": [],
            "sgb_assets": [],
        })
        assert "equity" in snap
        assert "prev_value" in snap["equity"]
        # Equity prev_value = sum(qty * prev_close) for each holding
        assert snap["equity"]["prev_value"] == pytest.approx(10 * 105.0)
        # Total prev_value should include equity's contribution
        assert snap["total"]["prev_value"] >= snap["equity"]["prev_value"]


# ---------- Fairvalue / SGB regressions ----------
class TestSGBPriceFix:
    """Regression: SGB prices were sourced from mintbyte (Motilal Oswal
    broker quote) instead of NSE, causing 2-3% portfolio value drift.
    """

    def test_nse_used_before_mintbyte(self):
        """Verify that when both NSE and mintbyte return data, NSE wins."""
        import pipeline.mf_sgb
        from unittest.mock import patch

        # Block ALL upstream network calls (NSE, mintbyte, IBJA) so the
        # test runs offline. The NSE mock returns a populated universe;
        # mintbyte and IBJA return empty so the NSE path is the only one
        # with data.
        nse_universe = [{
            "symbol": "SGBFEB32IV", "ltP": "14950.00",
            "prevClose": "15129.77", "maturityDate": "B32IV",
        }]
        isin_map = {"IN0020230184": "SGBFEB32IV"}
        fake_mintbyte = {
            "IN0020230184": {"price": 15302.74, "date": "2026-06-22"},
        }
        with patch.object(pipeline.mf_sgb, "_fetch_nse_sgb_universe",
                          return_value=nse_universe), \
             patch.object(pipeline.mf_sgb, "_build_isin_to_nse_symbol_map",
                          return_value=isin_map), \
             patch.object(pipeline.mf_sgb, "fetch_mintbyte_with_history",
                          return_value=fake_mintbyte), \
             patch.object(pipeline.mf_sgb, "_fetch_ibja_gold_price",
                          return_value=(None, None)):
            rows = pipeline.mf_sgb.fetch_sgb_rows([{
                "isin": "IN0020230184", "units": 1, "name": "SGB IV",
            }])
        assert len(rows) == 1
        # The value must reflect the NSE price (14950), not mintbyte
        assert rows[0].value == pytest.approx(14950.0, rel=0.01)
        assert "NSE" in rows[0].extra["source"]


class TestISINMappingFix:
    """Regression: NSE CSV has leading-space column names (' ISIN NUMBER').
    Initial parser looked up 'ISIN NUMBER' exactly and got nothing."""

    def test_csv_with_leading_spaces_parses(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        import pipeline.fair_value.search as s
        csv_text = (
            "SYMBOL,NAME OF COMPANY, SERIES, DATE OF LISTING, "
            "PAID UP VALUE, MARKET LOT, ISIN NUMBER, FACE VALUE\n"
            "RELIANCE,Reliance Industries Limited,EQ,29-NOV-1995,"
            "10,1,INE002A01018,10\n"
        )
        p = tmp_path / "nse.csv"
        p.write_text(csv_text, encoding="utf-8")
        # Mock the network so force=True uses our fixture, not the live
        # NSE CSV (which has 2372 rows).
        mock_resp = type("R", (), {
            "text": csv_text,
            "raise_for_status": lambda self: None,
        })()
        monkeypatch.setattr(s.requests, "get", lambda *a, **kw: mock_resp)
        monkeypatch.setattr(s, "CACHE_FILE", p)
        s._index = []
        s._index_loaded_at = 0
        s._cached_path = ""
        s._cached_mtime = 0

        idx = s._load_index(force=True)
        assert len(idx) == 1
        assert idx[0]["isin"] == "INE002A01018", \
            "ISIN must be read despite leading space in CSV header"


class TestScoringFix:
    """Regression: 'Reliance Industries Ltd' used to return RCOM
    (alphabetically first 'reliance' substring match) instead of
    RELIANCE (the user's actual intent)."""

    def test_reliance_industries_prefers_reliance_over_rcom(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        import pipeline.fair_value.search as s
        csv_text = (
            "SYMBOL,NAME OF COMPANY, SERIES, DATE OF LISTING, "
            "PAID UP VALUE, MARKET LOT, ISIN NUMBER, FACE VALUE\n"
            "RELIANCE,Reliance Industries Limited,EQ,29-NOV-1995,10,1,INE002A01018,10\n"
            "RCOM,Reliance Communications Limited,EQ,06-NOV-2006,5,1,INE330H01018,5\n"
        )
        p = tmp_path / "nse.csv"
        p.write_text(csv_text, encoding="utf-8")
        monkeypatch.setattr(s, "CACHE_FILE", p)
        s._index = []
        s._index_loaded_at = 0
        s._cached_path = ""
        s._cached_mtime = 0

        results = s.search_schemes("Reliance Industries Ltd", limit=5)
        assert results, "search must return at least one result"
        assert results[0]["symbol"] == "RELIANCE", (
            f"RELIANCE must rank first; got {results[0]['symbol']}"
        )


class TestTokenCacheFix:
    """Regression: Angel One token cache must exist and accept a fresh
    session on first login."""

    def test_angel_session_cache_load_save(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        import pipeline.angel_client
        # Point the cache at a temp file
        cache = tmp_path / "angel_session.json"
        monkeypatch.setattr(pipeline.angel_client, "ANGEL_SESSION_FILE", cache)
        # Save a session
        pipeline.angel_client._save_session({
            "jwtToken": "x", "refreshToken": "y", "feedToken": "",
            "client_code": "F00000", "logged_in_at": 9999999999,
        })
        # Reload module so the module-level constant is the temp path
        # (monkeypatch already did this in-place)
        loaded = pipeline.angel_client._load_session()
        assert loaded is not None
        assert loaded["jwtToken"] == "x"
        # Atomic write — the .tmp file should not linger
        leftovers = list(tmp_path.glob("*.tmp"))
        assert not leftovers


# ---------- Logging regressions ----------
class TestLoggingSetup:
    """Regression: get_logger used to print a configuration line on
    every import. We expect config to be deferred until first use."""

    def test_get_logger_lazy_configures(self):
        from pipeline.logging_setup import get_logger, _configured
        # Reset the configured flag so we can test the lazy behaviour
        import pipeline.logging_setup
        original = pipeline.logging_setup._configured
        pipeline.logging_setup._configured = False
        try:
            log = get_logger("regression-test")
            # After calling get_logger, _configured should be True
            assert pipeline.logging_setup._configured is True
        finally:
            pipeline.logging_setup._configured = original


# ---------- Concurrent fetch regressions ----------
class TestParallelSafety:
    """Regression: map_parallel used to leak exceptions from worker
    threads and crash the calling code."""

    def test_parallel_isolates_per_item_failures(self):
        from pipeline.parallel import map_parallel

        def fail_on_three(x: int) -> int:
            if x == 3:
                raise ValueError("bad input")
            return x * 10

        out = map_parallel(fail_on_three, [1, 2, 3, 4, 5])
        # Failures don't kill the batch; they map to None
        assert out == [10, 20, None, 40, 50]

    def test_parallel_empty_input(self):
        from pipeline.parallel import map_parallel
        assert map_parallel(lambda x: x, []) == []


# ---------- Snapshot regressions ----------
class TestSnapshotMathFix:
    """Regression: portfolio_total % used to drop MF/SGB contribution
    when equity_prev was 0 (e.g. no broker session)."""

    def test_total_includes_mf_when_equity_zero(self):
        """The total snapshot must sum equity + mf + sgb even when
        equity_value is 0 (so the chart can still show MF day change).
        """
        from pipeline.equity_compare import build_snapshot
        # Mock MF/SGB with positive values and zero equity.
        mf = [type("M", (), {
            "name": "Fund", "units": 100, "value": 1000.0,
            "prev_value": 950.0, "pct": 5.26, "extra": {},
        })()]
        sgb = [type("S", (), {
            "name": "SGB", "units": 1, "value": 15000.0,
            "prev_value": 15500.0, "pct": -3.23, "extra": {},
        })()]
        snap = build_snapshot(_mock={
            "indices": None,
            "holdings": [],          # zero equity
            "mf_assets": mf,
            "sgb_assets": sgb,
        })
        # Total must include MF + SGB even with no equity.
        assert snap["total"]["value"] == pytest.approx(1000 + 15000)
        assert snap["total"]["value"] >= snap["mf"]["value"]
        assert snap["total"]["value"] >= snap["sgb"]["value"]


# ---------- Import regressions ----------
class TestImportSurface:
    """Regression: import names that downstream code relies on."""

    def test_top_level_fairvalue_cli(self):
        # The old `python3 fairvalue.py` CLI was removed in the
        # restructure; the fair-value checker now lives in
        # pipeline.fair_value and is invoked via the web UI.
        pytest.skip("fairvalue.py CLI was removed in restructure")

    def test_webapp_server_help(self):
        import subprocess
        result = subprocess.run(
            [sys.executable, "-m", "webapp.server", "--help"],
            capture_output=True, text=True, timeout=15,
        )
        assert result.returncode == 0, result.stderr
        assert "--port" in result.stdout

    def test_all_modules_importable(self):
        # Every module we ship must import without error
        for module in (
            "pipeline.logging_setup", "pipeline.history_db", "pipeline.parallel", "pipeline.fair_value",
            "pipeline.fair_value.fetcher", "pipeline.fair_value.valuation",
            "pipeline.fair_value.search", "pipeline.angel_client", "pipeline.mf_sgb",
            "pipeline.indices_chart", "pipeline.equity_compare",
            "webapp", "webapp.data",
            "webapp.server",
        ):
            importlib.import_module(module)
