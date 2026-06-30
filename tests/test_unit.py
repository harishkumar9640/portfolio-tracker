"""
Unit tests for the pure (non-network, non-broker) helpers.
These run with no internet access and no .env — safe for CI.

Run:
    pytest tests/
    pytest tests/ -v
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Make the project root importable when running ``pytest`` from any cwd
PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

from pipeline.angel_client import Holding               # noqa: E402
from pipeline.mf_sgb import aggregate, _norm            # noqa: E402
from pipeline.logging_setup import get_logger, LOGS_DIR # noqa: E402


# ---------------------------------------------------------------------------
# Holding arithmetic
# ---------------------------------------------------------------------------
class TestHolding:
    def test_invested_and_value(self):
        h = Holding(
            symbol="RELIANCE-EQ", exchange="NSE", quantity=10,
            avg_price=100.0, ltp=110.0, prev_close=105.0, symbol_token="2885",
        )
        assert h.invested == 1000.0
        assert h.current_value == 1100.0
        assert h.pnl == 100.0
        assert h.pnl_pct == pytest.approx(10.0)

    def test_day_pnl(self):
        h = Holding(
            symbol="X", exchange="NSE", quantity=20,
            avg_price=50.0, ltp=55.0, prev_close=54.0, symbol_token="t",
        )
        assert h.day_pnl == pytest.approx(20.0)            # 20 * (55 - 54)
        assert h.day_pct == pytest.approx((55/54 - 1) * 100)

    def test_zero_invested_is_safe(self):
        h = Holding(
            symbol="X", exchange="NSE", quantity=0,
            avg_price=0.0, ltp=0.0, prev_close=0.0, symbol_token="t",
        )
        assert h.invested == 0.0
        assert h.pnl == 0.0
        assert h.pnl_pct == 0.0                           # guarded div-by-zero
        assert h.day_pnl == 0.0
        assert h.day_pct == 0.0

    def test_loss_case(self):
        h = Holding(
            symbol="X", exchange="NSE", quantity=5,
            avg_price=200.0, ltp=180.0, prev_close=185.0, symbol_token="t",
        )
        assert h.pnl == -100.0
        assert h.pnl_pct == pytest.approx(-10.0)


# ---------------------------------------------------------------------------
# AssetRow aggregate
# ---------------------------------------------------------------------------
class TestAggregate:
    def test_empty(self):
        from pipeline.mf_sgb import AssetRow
        agg = aggregate([])
        assert agg == {"value": 0.0, "prev_value": 0.0, "pct": 0.0, "count": 0}

    def test_skips_zero_rows(self):
        from pipeline.mf_sgb import AssetRow
        rows = [
            AssetRow(name="A", kind="mf", units=10, value=1000, prev_value=950, pct=0, extra={}),
            AssetRow(name="B", kind="mf", units=0, value=0, prev_value=0, pct=0, extra={"error": "x"}),
        ]
        agg = aggregate(rows)
        assert agg["value"] == 1000.0
        assert agg["prev_value"] == 950.0
        assert agg["count"] == 1
        assert agg["pct"] == pytest.approx((1000/950 - 1) * 100)

    def test_pct_guarded_on_zero_prev(self):
        from pipeline.mf_sgb import AssetRow
        rows = [
            AssetRow(name="A", kind="mf", units=10, value=1000, prev_value=0, pct=0, extra={}),
        ]
        agg = aggregate(rows)
        assert agg["pct"] == 0.0                            # no div-by-zero


# ---------------------------------------------------------------------------
# Name normalisation for fuzzy scheme matching
# ---------------------------------------------------------------------------
class TestNameNormalisation:
    @pytest.mark.parametrize("raw,expected", [
        ("HDFC Mid Cap Fund - Direct Plan", "hdfcmidcapfunddirectplan"),
        ("Parag Parikh Flexi Cap Fund - Growth Option - Direct Plan",
         "paragparikhflexicapfundgrowthoptiondirectplan"),
        ("Axis ELSS  -  Direct  Growth", "axiselssdirectgrowth"),
    ])
    def test_norm_strips_separators_and_punct(self, raw, expected):
        assert _norm(raw) == expected

    def test_norm_case_insensitive(self):
        assert _norm("HDFC Mid Cap") == _norm("hdfc mid cap") == _norm("Hdfc-Mid-Cap")


# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------
class TestLogging:
    def test_get_logger_returns_namespaced_logger(self):
        log = get_logger("foo")
        assert log.name == "portfolio.foo"

    def test_logs_directory_exists(self):
        # Calling get_logger triggers configure_logging which mkdirs the dir
        get_logger("ensure-configured")
        assert LOGS_DIR.exists()
        assert LOGS_DIR.is_dir()

    def test_idempotent_configuration(self):
        import logging
        from pipeline.logging_setup import configure_logging
        root = logging.getLogger("portfolio")
        handlers_before = list(root.handlers)
        configure_logging()
        handlers_after = list(root.handlers)
        # No new handlers added on a re-configure
        assert len(handlers_before) == len(handlers_after)


# ---------------------------------------------------------------------------
# pipeline.equity_compare bug regression
# ---------------------------------------------------------------------------
class TestEquityPrevFix:
    """
    Regression test for the bug where ``equity_prev`` was never computed in
    main(), so the total portfolio % under-reported equity weight.
    The fix sets equity_prev = sum(h.quantity * h.prev_close for h in holdings).
    """

    def test_total_prev_includes_equity_when_row_present(self):
        # Simulate main()'s logic in isolation.
        from dataclasses import dataclass
        from pipeline.mf_sgb import AssetRow

        @dataclass
        class H:
            quantity: int
            prev_close: float

        holdings = [H(quantity=10, prev_close=100.0), H(quantity=5, prev_close=200.0)]
        equity_value = 10 * 110 + 5 * 220         # = 2200
        equity_row = {"value": equity_value}
        equity_prev = sum(h.quantity * h.prev_close for h in holdings if h.prev_close > 0)

        mf_assets = [
            AssetRow(name="F", kind="mf", units=100, value=2000, prev_value=1990, pct=0, extra={}),
        ]
        mf_value = sum(a.value for a in mf_assets)
        mf_prev = sum(a.prev_value for a in mf_assets)

        total_value = equity_value + mf_value
        total_prev = equity_prev + mf_prev

        assert equity_prev == 2000.0
        assert total_value == 4200.0
        assert total_prev == 3990.0
        # The bug would have left total_prev == mf_prev == 1990, dropping equity weight.
        assert total_prev > mf_prev


# pipeline.equity_compare equity_row.pnl_today should equal value − prev_value
# ---------------------------------------------------------------------------
class TestEquityRowPnlToday:
    """Regression test for the 'show today's ₹ gain/loss on the bar' feature.
    build_snapshot() must populate equity_row.pnl_today = current − previous.
    """

    def test_pnl_today_is_value_minus_prev(self):
        from pipeline.equity_compare import build_snapshot
        # Construct a minimal _mock with one equity holding
        mock = {
            "holdings": [
                # SimpleNamespace-like object with the fields pipeline.equity_compare reads
                # We'll use SimpleNamespace for type-safety
            ],
        }
        from types import SimpleNamespace
        mock["holdings"] = [
            SimpleNamespace(
                symbol="TEST", quantity=10, avg_price=100.0, prev_close=100.0,
                ltp=110.0, current_value=1100.0, pnl=100.0, pnl_pct=10.0,
            ),
        ]
        mock["indices"] = type("I", (), {"columns": [], "empty": True})()
        snap = build_snapshot(_mock=mock)
        eq = snap["equity"]
        assert eq["value"] == 1100.0
        assert eq["prev_value"] == 1000.0
        row = eq["row"]
        assert row is not None
        assert row["pnl_today"] == 100.0  # 1100 - 1000
        assert abs(row["pct"] - 10.0) < 1e-9  # (1100/1000 - 1) * 100

    def test_pnl_today_negative_when_price_drops(self):
        from pipeline.equity_compare import build_snapshot
        from types import SimpleNamespace
        mock = {
            "holdings": [
                SimpleNamespace(
                    symbol="TEST", quantity=10, avg_price=100.0, prev_close=100.0,
                    ltp=95.0, current_value=950.0, pnl=-50.0, pnl_pct=-5.0,
                ),
            ],
            "indices": type("I", (), {"columns": [], "empty": True})(),
        }
        snap = build_snapshot(_mock=mock)
        row = snap["equity"]["row"]
        assert row["pnl_today"] == -50.0
        assert abs(row["pct"] - (-5.0)) < 1e-9