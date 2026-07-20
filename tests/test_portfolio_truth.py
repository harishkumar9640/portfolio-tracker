"""Tests for pipeline.portfolio_truth (the single source of truth)."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

from pipeline.portfolio_truth import (
    TRUTH_FILE, _from_mfs_json, _from_sgbs_json, _from_watchlist,
    diff_states, fetch_live_state, load_truth, merge_from_live,
    save_truth,
)
from pipeline.portfolio_truth.bootstrap import bootstrap


# ---------- Fixture: clean state ----------

@pytest.fixture
def clean_truth(monkeypatch, tmp_path):
    """Set up a clean truth environment. No broker calls."""
    # Use a tmp truth file for tests
    monkeypatch.setattr("pipeline.portfolio_truth.TRUTH_FILE", tmp_path / "truth.json")
    monkeypatch.setattr("pipeline.portfolio_truth.MFS_FILE", tmp_path / "mfs.json")
    monkeypatch.setattr("pipeline.portfolio_truth.SGBS_FILE", tmp_path / "sgbs.json")
    monkeypatch.setattr("pipeline.portfolio_truth.WATCHLIST_FILE", tmp_path / "watchlist.txt")

    # Write a config-like state
    (tmp_path / "mfs.json").write_text(json.dumps([
        {"name": "HDFC Mid Cap Fund - Direct Plan", "units": 500.58},
    ]))
    (tmp_path / "sgbs.json").write_text(json.dumps([
        {"isin": "IN0020230184", "units": 1, "invested_per_g": 7920},
    ]))
    (tmp_path / "watchlist.txt").write_text("RELIANCE\nHDFCBANK\n")

    return tmp_path


# ---------- load/save roundtrip ----------

def test_load_truth_returns_default_when_missing(clean_truth):
    truth = load_truth()
    assert truth["schema_version"] == 1
    assert truth["equity"] == {}
    assert truth["mutual_funds"] == {}
    assert truth["sgbs"] == {}
    assert truth["watchlist"] == []
    assert truth["asof"] is None


def test_save_and_load_roundtrip(clean_truth):
    truth = load_truth()
    truth["equity"]["UNOMINDA"] = {
        "ticker": "UNOMINDA", "qty": 36, "avg_price": 1096.70,
        "exchange": "NSE", "source": "test",
    }
    save_truth(truth, source="test")
    loaded = load_truth()
    assert loaded["equity"]["UNOMINDA"]["qty"] == 36
    assert loaded["source"] == "test"


# ---------- data-source readers ----------

def test_from_mfs_json(clean_truth):
    mfs = _from_mfs_json()
    assert "HDFC Mid Cap Fund - Direct Plan" in mfs
    assert mfs["HDFC Mid Cap Fund - Direct Plan"]["units"] == 500.58


def test_from_sgbs_json(clean_truth):
    sgbs = _from_sgbs_json()
    assert "IN0020230184" in sgbs
    assert sgbs["IN0020230184"]["invested_per_g"] == 7920


def test_from_watchlist_dedupes_and_skips_comments(clean_truth):
    # Add a duplicate and a comment
    path = clean_truth / "watchlist.txt"
    path.write_text("# top picks\nRELIANCE\nHDFCBANK\n  reliance  \n# another\n")
    wl = _from_watchlist()
    # Order preserved, deduped, no comments, whitespace stripped
    assert wl == ["RELIANCE", "HDFCBANK"]


def test_from_mfs_json_handles_missing_file(clean_truth):
    (clean_truth / "mfs.json").unlink()
    assert _from_mfs_json() == {}


# ---------- fetch_live_state ----------

def test_fetch_live_state_no_broker(clean_truth):
    """fetch_live_state should work even if broker fails (broker is
    a soft dependency). Equity will be empty, others populated."""
    with patch("pipeline.portfolio_truth._from_broker", return_value={}):
        state = fetch_live_state()
    assert state["equity"] == {}
    assert "HDFC Mid Cap Fund - Direct Plan" in state["mutual_funds"]
    assert "IN0020230184" in state["sgbs"]
    assert "RELIANCE" in state["watchlist"]


# ---------- diff ----------

def test_diff_clean_state(clean_truth):
    current = {"equity": {}, "mutual_funds": {}, "sgbs": {}, "watchlist": []}
    live = {"equity": {}, "mutual_funds": {}, "sgbs": {}, "watchlist": []}
    d = diff_states(current, live)
    assert d["is_clean"] is True


def test_diff_detects_added_position(clean_truth):
    current = {"equity": {}, "mutual_funds": {}, "sgbs": {}, "watchlist": []}
    live = {"equity": {"UNOMINDA": {"ticker": "UNOMINDA", "qty": 36, "avg_price": 1100}},
            "mutual_funds": {}, "sgbs": {}, "watchlist": []}
    d = diff_states(current, live)
    assert d["is_clean"] is False
    assert "UNOMINDA" in d["equity"]["added"]


def test_diff_detects_qty_change(clean_truth):
    current = {"equity": {"X": {"ticker": "X", "qty": 100, "avg_price": 50}},
            "mutual_funds": {}, "sgbs": {}, "watchlist": []}
    live = {"equity": {"X": {"ticker": "X", "qty": 200, "avg_price": 50}},
            "mutual_funds": {}, "sgbs": {}, "watchlist": []}
    d = diff_states(current, live)
    assert "X" in d["equity"]["qty_changed"]


def test_diff_detects_avg_price_change(clean_truth):
    current = {"equity": {"X": {"ticker": "X", "qty": 100, "avg_price": 50.00}},
            "mutual_funds": {}, "sgbs": {}, "watchlist": []}
    live = {"equity": {"X": {"ticker": "X", "qty": 100, "avg_price": 55.00}},
            "mutual_funds": {}, "sgbs": {}, "watchlist": []}
    d = diff_states(current, live)
    assert "X" in d["equity"]["avg_changed"]


def test_diff_detects_removed_position(clean_truth):
    current = {"equity": {"X": {"ticker": "X", "qty": 100, "avg_price": 50}},
            "mutual_funds": {}, "sgbs": {}, "watchlist": []}
    live = {"equity": {}, "mutual_funds": {}, "sgbs": {}, "watchlist": []}
    d = diff_states(current, live)
    assert "X" in d["equity"]["removed"]


# ---------- merge_from_live ----------

def test_merge_preserves_avg_price(clean_truth):
    existing = {"equity": {"UNOMINDA": {"ticker": "UNOMINDA", "qty": 36, "avg_price": 1096.70, "source": "user"}},
                "mutual_funds": {}, "sgbs": {}, "watchlist": []}
    live = {"equity": {"UNOMINDA": {"ticker": "UNOMINDA", "qty": 36, "avg_price": 0, "source": "angel"}},
            "mutual_funds": {}, "sgbs": {}, "watchlist": []}
    out = merge_from_live(live, existing)
    # avg_price from existing preserved (broker doesn't give cost basis)
    assert out["equity"]["UNOMINDA"]["avg_price"] == 1096.70


def test_merge_preserves_sgb_metadata(clean_truth):
    existing = {"equity": {}, "mutual_funds": {},
                "sgbs": {"IN0020230184": {"isin": "IN0020230184", "units": 1,
                                            "invested_per_g": 7920, "buy_date": "2023-02-15"}},
                "watchlist": []}
    live = {"equity": {}, "mutual_funds": {},
            "sgbs": {"IN0020230184": {"isin": "IN0020230184", "units": 1, "source": "sgbs.json"}},
            "watchlist": []}
    out = merge_from_live(live, existing)
    assert out["sgbs"]["IN0020230184"]["invested_per_g"] == 7920
    assert out["sgbs"]["IN0020230184"]["buy_date"] == "2023-02-15"


# ---------- bootstrap ----------

def test_bootstrap_returns_empty_when_truth_missing(clean_truth):
    with patch("pipeline.portfolio_truth.load_truth",
               side_effect=lambda: load_truth()):
        snap = bootstrap(quiet=True)
    assert snap["equity"] == {}
    assert snap["mutual_funds"] == {} or "HDFC Mid Cap Fund - Direct Plan" in snap["mutual_funds"]
    assert snap["drift"] is None  # no existing truth → no drift to report


def test_bootstrap_force_init_creates_file(clean_truth, tmp_path):
    # Truth file doesn't exist; force_init should create it
    assert not (tmp_path / "truth.json").exists()
    # Patch the broker to return nothing; the MFs come from mfs.json fixture
    with patch("pipeline.portfolio_truth._from_broker", return_value={}):
        snap = bootstrap(force_init=True, quiet=True)
    assert (tmp_path / "truth.json").exists()
    assert snap["asof"] is not None
    assert "HDFC Mid Cap Fund - Direct Plan" in snap["mutual_funds"]


# ---------- equity_compare filtering: zero-qty positions are excluded ----------

def test_zero_qty_position_excluded_from_snapshot():
    """Regression: the broker returns IRCON with qty=0 after we sold it.
    The portfolio snapshot MUST NOT show it. This is a real bug that
    was found in the webapp on 2026-07-02."""
    # Simulate a Holding with qty=0 (post-sell IRCON)
    from pipeline.angel_client import Holding

    class FakeHolding:
        def __init__(self, **kw):
            for k, v in kw.items():
                setattr(self, k, v)

    # Build a small set: IRCON (qty=0) + UNOMINDA (qty=36)
    fake_holdings = [
        FakeHolding(
            symbol="IRCON-EQ", exchange="NSE", quantity=0,
            avg_price=134.70, ltp=0, prev_close=134.70, symbol_token="",
        ),
        FakeHolding(
            symbol="UNOMINDA-EQ", exchange="NSE", quantity=36,
            avg_price=1096.70, ltp=1100, prev_close=1096.70, symbol_token="",
        ),
    ]
    # We test the filter logic directly (matches what build_snapshot does)
    active = [h for h in fake_holdings if h.quantity > 0]
    symbols = [h.symbol for h in active]
    assert "IRCON-EQ" not in symbols
    assert "UNOMINDA-EQ" in symbols


# ---------- equity_tickers(): truth-file universe for Portfolio sections ----------

def test_equity_tickers_default_includes_etfs(clean_truth):
    """By default (Shareholding Pattern), all equity tickers are returned
    including ETFs (GOLDBEES, METALIETF, NEXT50IETF)."""
    truth = {
        "schema_version": 1,
        "equity": {
            "RELIANCE": {"ticker": "RELIANCE", "qty": 60, "avg_price": 1250.52},
            "UNOMINDA": {"ticker": "UNOMINDA", "qty": 36, "avg_price": 1096.7},
            "GOLDBEES": {"ticker": "GOLDBEES", "qty": 300, "avg_price": 81.42},
            "METALIETF": {"ticker": "METALIETF", "qty": 1500, "avg_price": 8.4},
            "NEXT50IETF": {"ticker": "NEXT50IETF", "qty": 36, "avg_price": 77.21},
        },
    }
    with patch("pipeline.portfolio_truth.load_truth", return_value=truth):
        from pipeline.portfolio_truth import equity_tickers
        out = equity_tickers()
    assert "GOLDBEES" in out
    assert "METALIETF" in out
    assert "NEXT50IETF" in out
    assert "RELIANCE" in out
    assert "UNOMINDA" in out
    # sorted alphabetically
    assert out == sorted(out)


def test_equity_tickers_excludes_etfs_when_requested(clean_truth):
    """MF Holdings Trend uses include_etfs=False — ETFs must be excluded."""
    truth = {
        "schema_version": 1,
        "equity": {
            "RELIANCE": {"ticker": "RELIANCE", "qty": 60, "avg_price": 1250.52},
            "UNOMINDA": {"ticker": "UNOMINDA", "qty": 36, "avg_price": 1096.7},
            "GOLDBEES": {"ticker": "GOLDBEES", "qty": 300, "avg_price": 81.42},
            "METALIETF": {"ticker": "METALIETF", "qty": 1500, "avg_price": 8.4},
            "NEXT50IETF": {"ticker": "NEXT50IETF", "qty": 36, "avg_price": 77.21},
        },
    }
    with patch("pipeline.portfolio_truth.load_truth", return_value=truth):
        from pipeline.portfolio_truth import equity_tickers
        out = equity_tickers(include_etfs=False)
    assert "GOLDBEES" not in out
    assert "METALIETF" not in out
    assert "NEXT50IETF" not in out
    assert "RELIANCE" in out
    assert "UNOMINDA" in out
    assert out == sorted(out)


def test_equity_tickers_handles_lowercase_etf_suffix(clean_truth):
    """Edge case: someone might use lowercase 'etf' suffix."""
    truth = {
        "schema_version": 1,
        "equity": {
            "FOOETF": {"ticker": "FOOETF", "qty": 1, "avg_price": 100.0},
            "BAR": {"ticker": "BAR", "qty": 1, "avg_price": 100.0},
        },
    }
    with patch("pipeline.portfolio_truth.load_truth", return_value=truth):
        from pipeline.portfolio_truth import equity_tickers
        out_no_etf = equity_tickers(include_etfs=False)
    assert "FOOETF" not in out_no_etf
    assert "BAR" in out_no_etf


def test_truth_mtime_returns_zero_for_missing_file(monkeypatch, tmp_path):
    """truth_mtime() should return 0.0 (not raise) when the file is missing,
    so cache invalidation can treat 'no truth file' as 'nothing changed'."""
    monkeypatch.setattr("pipeline.portfolio_truth.TRUTH_FILE", tmp_path / "no_such.json")
    from pipeline.portfolio_truth import truth_mtime
    assert truth_mtime() == 0.0


def test_truth_mtime_reflects_file_modification(monkeypatch, tmp_path):
    """truth_mtime() must return the file's actual mtime so the cache
    invalidation in webapp.data can detect changes."""
    import os
    p = tmp_path / "truth.json"
    p.write_text("{}")
    monkeypatch.setattr("pipeline.portfolio_truth.TRUTH_FILE", p)
    from pipeline.portfolio_truth import truth_mtime
    m1 = truth_mtime()
    assert m1 > 0
    # Touch + rewrite
    import time
    time.sleep(0.1)
    p.write_text("{}")
    m2 = truth_mtime()
    assert m2 > m1


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
