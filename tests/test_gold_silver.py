"""Tests for the Gold:Silver ratio snapshot (webapp.data)."""
from __future__ import annotations

from unittest.mock import patch, MagicMock

import pytest


@pytest.fixture(autouse=True)
def _reset_cache(monkeypatch):
    """Reset the in-process cache between tests."""
    import webapp.data as wd
    wd._gold_silver_cache = {"asof": None, "data": None, "ts": 0.0}
    wd._gold_silver_in_progress = False
    wd._gold_silver_in_progress_ts = 0.0
    wd._gsilver_fetcher = None  # force re-import in each test (so patch takes effect)


# ---------- Pure-logic tests (no network) ----------

def test_gsilver_signal_high_means_buy_silver():
    from webapp.data import _gsilver_signal
    label, color, cls = _gsilver_signal(95.0)
    assert "BUY SILVER" in label
    assert "high" in label.lower()
    assert color == "#d62728"


def test_gsilver_signal_low_means_take_profit():
    from webapp.data import _gsilver_signal
    label, color, cls = _gsilver_signal(55.0)
    assert "TAKE PROFIT" in label
    assert "low" in label.lower()
    assert color == "#ff7f0e"


def test_gsilver_signal_neutral_zone():
    from webapp.data import _gsilver_signal
    label, color, cls = _gsilver_signal(75.0)
    assert "HOLD" in label
    assert color == "#2ca02c"


def test_gsilver_signal_threshold_boundaries():
    """Boundaries: ratio=90 should still be BUY; ratio=60 should still be TAKE PROFIT."""
    from webapp.data import _gsilver_signal
    # 90.0 is exactly the HIGH threshold; our check is `>=` so it triggers
    label_at_high, _, _ = _gsilver_signal(90.0)
    assert "BUY SILVER" in label_at_high
    label_just_above_high, _, _ = _gsilver_signal(89.9)
    assert "HOLD" in label_just_above_high
    # 60.0 is exactly the LOW threshold; our check is `<=` so it triggers
    label_at_low, _, _ = _gsilver_signal(60.0)
    assert "TAKE PROFIT" in label_at_low
    label_just_above_low, _, _ = _gsilver_signal(60.1)
    assert "HOLD" in label_just_above_low


def test_historical_context_brackets():
    from webapp.data import _historical_context
    very_high = _historical_context(110)
    assert "very high" in very_high.lower()
    # 85 is in the 70-89 bracket (neutral-to-cautious), not the high bracket
    neutral_high = _historical_context(85)
    assert "mean" in neutral_high.lower() or "neutral" in neutral_high.lower()
    # 92 is in the high bracket (>= 90)
    high = _historical_context(92)
    assert ("cheap relative to gold" in high.lower()
            or "rotate" in high.lower())
    low = _historical_context(65)   # 65 is in 60-69 low bracket
    assert "rich" in low.lower()
    very_low = _historical_context(45)  # 45 is in very-low (<60) bracket
    assert "very low" in very_low.lower()


def test_empty_snapshot_shape():
    """The empty snapshot must have the same keys the template relies on,
    so the template can render even when yfinance is down."""
    from webapp.data import _empty_gsilver_snapshot
    s = _empty_gsilver_snapshot()
    for key in ("ratio", "gold_usd_oz", "silver_usd_oz",
                "signal", "signal_label", "signal_color", "signal_class",
                "historical", "asof", "asof_human", "source"):
        assert key in s, f"missing key: {key}"
    assert s["ratio"] is None
    assert s["source"] == "unavailable"


# ---------- Tests with mocked yfinance (no network) ----------

def _mock_yfinance(gold_price: float, silver_price: float):
    """Build a mock for pipeline.portfolio_monitor.concentration_check
    that returns a fake ratio."""
    def fake_get_gold_silver_ratio():
        if not gold_price or not silver_price:
            return None
        return {
            "gold_usd_oz": gold_price,
            "silver_usd_oz": silver_price,
            "ratio": round(gold_price / silver_price, 2),
            "asof": "2026-07-19T10:00:00+05:30",
        }
    return fake_get_gold_silver_ratio


def test_snapshot_calls_yfinance_and_shapes_result():
    from webapp.data import get_gold_silver_ratio_snapshot
    fake = _mock_yfinance(2400, 27)
    with patch("webapp.data._gsilver_fetcher", fake, create=True):
        s = get_gold_silver_ratio_snapshot(force=True)
    assert s["ratio"] == round(2400 / 27, 2)  # ~88.89
    assert s["gold_usd_oz"] == 2400
    assert s["silver_usd_oz"] == 27
    assert s["source"] == "yfinance"
    # 88.89 should be in the "HOLD" zone (< 90)
    assert "HOLD" in s["signal"]


def test_snapshot_buy_silver_signal():
    from webapp.data import get_gold_silver_ratio_snapshot
    # Gold 3000 / Silver 30 = 100 (very high -> BUY SILVER)
    fake = _mock_yfinance(3000, 30)
    with patch("webapp.data._gsilver_fetcher", fake, create=True):
        s = get_gold_silver_ratio_snapshot(force=True)
    assert s["ratio"] == 100.0
    assert "BUY SILVER" in s["signal"]
    assert s["signal_color"] == "#d62728"


def test_snapshot_handles_yfinance_failure():
    from webapp.data import get_gold_silver_ratio_snapshot
    def boom():
        raise RuntimeError("network down")
    with patch("webapp.data._gsilver_fetcher", boom, create=True):
        s = get_gold_silver_ratio_snapshot(force=True)
    # Falls back to empty snapshot
    assert s["ratio"] is None
    assert s["source"] == "unavailable"


def test_snapshot_caches_results():
    """A second call within the TTL should not re-fetch."""
    from webapp.data import get_gold_silver_ratio_snapshot
    call_count = [0]
    def fake():
        call_count[0] += 1
        return _mock_yfinance(2400, 27)()
    with patch("webapp.data._gsilver_fetcher", fake, create=True):
        s1 = get_gold_silver_ratio_snapshot(force=True)
        s2 = get_gold_silver_ratio_snapshot(force=False)  # cache hit
        assert call_count[0] == 1
        assert s1["ratio"] == s2["ratio"]


def test_snapshot_force_bypasses_cache():
    from webapp.data import get_gold_silver_ratio_snapshot
    call_count = [0]
    def fake():
        call_count[0] += 1
        return _mock_yfinance(2400, 27)()
    with patch("webapp.data._gsilver_fetcher", fake, create=True):
        get_gold_silver_ratio_snapshot(force=True)
        get_gold_silver_ratio_snapshot(force=True)  # explicit force
        get_gold_silver_ratio_snapshot(force=True)
    assert call_count[0] == 3


def test_snapshot_returns_none_ratio_gracefully():
    from webapp.data import get_gold_silver_ratio_snapshot
    def fake_none():
        return None
    with patch("webapp.data._gsilver_fetcher", fake_none, create=True):
        s = get_gold_silver_ratio_snapshot(force=True)
    assert s["ratio"] is None
    assert s["source"] == "unavailable"
    # The signal label is graceful
    assert "unavailable" in s["signal_label"].lower()


def test_snapshot_asof_human_present():
    from webapp.data import get_gold_silver_ratio_snapshot
    fake = _mock_yfinance(2400, 27)
    with patch("webapp.data._gsilver_fetcher", fake, create=True):
        s = get_gold_silver_ratio_snapshot(force=True)
    assert "asof" in s
    assert "asof_human" in s
    assert s["asof"]  # non-empty
