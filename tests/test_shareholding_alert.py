"""
Tests for shareholding_alert.py — quarterly shareholding-pattern
change detector + email notifier.

Covers:
  - _parse_pct: handles "16.76%", "16.76 %", "0.0", "—"
  - _parse_shareholding_table: extracts 12-quarter table from real HTML
  - fetch_one: returns TickerShareholding for valid ticker; None for invalid
  - diff_snapshots: detects significant changes (>0.5%); ignores small ones
  - diff_snapshots: handles Promoter Pledged / Locked sub-rows
  - diff_snapshots: returns empty list when snapshots identical
  - render_email: subject line for "no changes" and "N changed"
  - render_email: HTML body includes ticker + category names
  - render_email: text body has per-stock change lines
  - run_once: first run never alerts (only persists snapshot)
  - run_once: second run with no changes returns sent=False
  - run_once: detects change and emails
  - run_once: handles fetch failure gracefully
  - _next_run_ist: math (today 16:35 if before, else tomorrow)
  - Alert log persistence
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

import shareholding_alert as sha  # noqa: E402
from shareholding_alert import (  # noqa: E402
    QuarterSnapshot,
    TickerShareholding,
    ShpChange,
    CATEGORIES,
    SIGNIFICANCE_PCT,
    IST,
    LOG_FILE,
    PREV_FILE,
    _parse_pct,
    _parse_shareholding_table,
    fetch_one,
    fetch_all,
    diff_snapshots,
    render_email,
    run_once,
    _next_run_ist,
    _load_prev,
    _save_curr,
)


# ---------- Fixtures ----------

SAMPLE_HTML = """
<html>
<body>
<table>
  <tr><th>Summary</th><th>Mar 2026</th><th>Dec 2025</th><th>Sep 2025</th><th>Jun 2025</th></tr>
  <tr><td>Promoter</td><td>50.00%</td><td>50.10%</td><td>50.20%</td><td>50.30%</td></tr>
  <tr><td>Pledged</td><td>0.0%</td><td>0.0%</td><td>0.0%</td><td>0.0%</td></tr>
  <tr><td>Locked</td><td>0.0%</td><td>0.0%</td><td>0.0%</td><td>0.0%</td></tr>
  <tr><td>FII</td><td>20.00%</td><td>20.50%</td><td>21.00%</td><td>21.50%</td></tr>
  <tr><td>DII</td><td>20.00%</td><td>20.00%</td><td>19.50%</td><td>19.00%</td></tr>
  <tr><td>Mutual Funds</td><td>10.00%</td><td>10.00%</td><td>10.00%</td><td>9.80%</td></tr>
  <tr><td>Banks</td><td>1.00%</td><td>1.00%</td><td>1.00%</td><td>1.00%</td></tr>
  <tr><td>Insurance</td><td>8.00%</td><td>8.00%</td><td>8.00%</td><td>8.00%</td></tr>
  <tr><td>Public</td><td>10.00%</td><td>9.50%</td><td>9.50%</td><td>9.50%</td></tr>
  <tr><td>Others</td><td>0.00%</td><td>0.00%</td><td>0.00%</td><td>0.00%</td></tr>
</table>
</body>
</html>
"""


def _make_quarter(q: str, **kwargs) -> QuarterSnapshot:
    """Helper to create a QuarterSnapshot with sane defaults."""
    defaults = dict(
        quarter=q, promoter=0.0, promoter_pledged=0.0, promoter_locked=0.0,
        fii=0.0, dii=0.0, mutual_funds=0.0, banks=0.0, insurance=0.0,
        public=0.0, others=0.0,
    )
    defaults.update(kwargs)
    return QuarterSnapshot(**defaults)


def _make_ticker(tkr: str = "ITC", quarters: list = None) -> TickerShareholding:
    return TickerShareholding(
        ticker=tkr, name=tkr, url="https://example.com",
        fetched_at=datetime.now(IST).isoformat(),
        quarters=quarters or [],
    )


@pytest.fixture
def tmp_data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(sha, "PREV_FILE", tmp_path / "shareholding_prev.json")
    monkeypatch.setattr(sha, "LOG_FILE", tmp_path / "shareholding_alert_log.json")
    return tmp_path


@pytest.fixture
def clean_smtp_env(monkeypatch):
    """Strip SMTP env vars so send_email runs in dry-run mode."""
    for k in ("MF_ALERT_SMTP_HOST", "MF_ALERT_SMTP_USER", "MF_ALERT_SMTP_PASS",
              "MF_ALERT_SMTP_PORT", "MF_ALERT_TO", "MF_ALERT_FROM",
              "MF_ALERT_DRY_RUN"):
        monkeypatch.delenv(k, raising=False)
        monkeypatch.setenv(k, "")


# ---------- _parse_pct ----------

class TestParsePct:
    def test_simple_pct(self):
        assert _parse_pct("16.76%") == 16.76

    def test_pct_with_space(self):
        assert _parse_pct("16.76 %") == 16.76

    def test_no_pct_symbol(self):
        assert _parse_pct("16.76") == 16.76

    def test_zero(self):
        assert _parse_pct("0.0") == 0.0

    def test_dash_returns_zero(self):
        assert _parse_pct("—") == 0.0

    def test_empty_string_returns_zero(self):
        assert _parse_pct("") == 0.0

    def test_with_thousands_comma(self):
        assert _parse_pct("1,234.56") == 1234.56


# ---------- _parse_shareholding_table ----------

class TestParseShareholdingTable:
    def test_parses_sample_html(self):
        quarters = _parse_shareholding_table(SAMPLE_HTML)
        assert len(quarters) == 4
        # First quarter is Mar 2026 (most recent)
        assert quarters[0].quarter == "Mar 2026"
        assert quarters[0].promoter == 50.00
        assert quarters[0].fii == 20.00
        assert quarters[0].dii == 20.00
        assert quarters[0].mutual_funds == 10.00

    def test_sub_rows_mapped_to_promoter(self):
        quarters = _parse_shareholding_table(SAMPLE_HTML)
        # "Pledged" and "Locked" sub-rows map to promoter_pledged/locked
        assert quarters[0].promoter_pledged == 0.0
        assert quarters[0].promoter_locked == 0.0

    def test_returns_empty_for_no_table(self):
        html = "<html><body><p>No tables here</p></body></html>"
        assert _parse_shareholding_table(html) == []

    def test_returns_empty_for_table_without_quarter_headers(self):
        html = """
        <html><body><table>
          <tr><th>Summary</th><th>Col1</th></tr>
          <tr><td>Promoter</td><td>50%</td></tr>
        </table></body></html>
        """
        assert _parse_shareholding_table(html) == []

    def test_returns_empty_for_table_without_promoter(self):
        html = """
        <html><body><table>
          <tr><th>Summary</th><th>Mar 2026</th></tr>
          <tr><td>Other</td><td>1%</td></tr>
        </table></body></html>
        """
        assert _parse_shareholding_table(html) == []


# ---------- fetch_one ----------

class TestFetchOne:
    def test_invalid_ticker_returns_none(self):
        # Network might fail; if it succeeds, we expect a TickerShareholding
        # if it fails, we get None — both are acceptable.
        result = fetch_one("ZZZNOTREAL")
        assert result is None

    def test_placeholder_id_skipped(self):
        """Tickers with placeholder IDs (>= threshold) are skipped."""
        import mf_holdings
        # Save original
        orig = mf_holdings.TICKER_MAP.copy()
        try:
            mf_holdings.TICKER_MAP["TEST_FAKE"] = {
                "name": "Fake", "id": 99_999_999,
                "url_slug": "fake",
            }
            # Should return None without network call
            with patch.object(sha, "requests") as mock_req:
                result = fetch_one("TEST_FAKE")
                assert result is None
                mock_req.get.assert_not_called()
        finally:
            mf_holdings.TICKER_MAP = orig


# ---------- diff_snapshots ----------

class TestDiffSnapshots:
    def test_identical_snapshots_yield_no_changes(self):
        ts = _make_ticker("ITC", quarters=[
            _make_quarter("Mar 2026", fii=20.0),
            _make_quarter("Dec 2025", fii=20.0),
        ])
        prev = {"ITC": {"quarters": [q.to_dict() for q in ts.quarters]}}
        assert diff_snapshots(prev, {"ITC": ts}) == []

    def test_detects_significant_fii_change(self):
        ts = _make_ticker("ITC", quarters=[_make_quarter("Mar 2026", fii=20.0)])
        prev = {"ITC": {"quarters": [_make_quarter("Mar 2026", fii=18.0).to_dict()]}}
        changes = diff_snapshots(prev, {"ITC": ts})
        fii_changes = [c for c in changes if c.category == "FII"]
        assert len(fii_changes) == 1
        assert fii_changes[0].delta == 2.0

    def test_ignores_small_change_below_threshold(self):
        ts = _make_ticker("ITC", quarters=[_make_quarter("Mar 2026", fii=20.0)])
        prev = {"ITC": {"quarters": [_make_quarter("Mar 2026", fii=19.7).to_dict()]}}
        # 0.3% < 0.5% threshold for FII
        changes = diff_snapshots(prev, {"ITC": ts})
        assert not any(c.category == "FII" for c in changes)

    def test_first_run_returns_no_changes(self):
        """First-ever run: prev is empty, we don't alert (would be too noisy)."""
        ts = _make_ticker("ITC", quarters=[_make_quarter("Mar 2026", fii=20.0)])
        # Empty prev
        changes = diff_snapshots({}, {"ITC": ts})
        assert changes == []

    def test_promoter_change_uses_lower_threshold(self):
        """Promoter has 0.1% threshold (more sensitive)."""
        ts = _make_ticker("ITC", quarters=[_make_quarter("Mar 2026", promoter=50.3)])
        prev = {"ITC": {"quarters": [_make_quarter("Mar 2026", promoter=50.0).to_dict()]}}
        changes = diff_snapshots(prev, {"ITC": ts})
        # 0.3% > 0.1% threshold for Promoter
        promoter_changes = [c for c in changes if c.category == "Promoter"]
        assert len(promoter_changes) == 1

    def test_promoter_below_threshold_ignored(self):
        ts = _make_ticker("ITC", quarters=[_make_quarter("Mar 2026", promoter=50.05)])
        prev = {"ITC": {"quarters": [_make_quarter("Mar 2026", promoter=50.0).to_dict()]}}
        # 0.05% < 0.1% threshold
        changes = diff_snapshots(prev, {"ITC": ts})
        assert not any(c.category == "Promoter" for c in changes)

    def test_multiple_categories_change(self):
        """Several categories change at once, all should be reported."""
        ts = _make_ticker("ITC", quarters=[_make_quarter(
            "Mar 2026", promoter=50.0, fii=20.0, mutual_funds=10.0,
            public=10.0,
        )])
        prev = {"ITC": {"quarters": [_make_quarter(
            "Mar 2026", promoter=50.0, fii=22.0, mutual_funds=10.0, public=8.0,
        ).to_dict()]}}
        changes = diff_snapshots(prev, {"ITC": ts})
        categories = {c.category for c in changes}
        assert "FII" in categories  # -2% (significant)
        assert "Public" in categories  # +2% (significant)
        assert "Mutual Funds" not in categories  # 0% (no change)


# ---------- render_email ----------

class TestRenderEmail:
    def test_no_changes_subject(self):
        ts = _make_ticker("ITC", quarters=[_make_quarter("Mar 2026", fii=20.0)])
        subject, plain, html = render_email([], {"ITC": ts})
        assert "No changes" in subject
        assert "ITC" in plain  # per-stock snapshot table

    def test_changes_subject_lists_tickers(self):
        ts = _make_ticker("ITC", quarters=[_make_quarter("Mar 2026", fii=22.0)])
        changes = [ShpChange(
            ticker="ITC", name="ITC", category="FII",
            old_quarter="Mar 2026", new_quarter="Mar 2026",
            old_value=20.0, new_value=22.0, delta=2.0,
        )]
        subject, plain, html = render_email(changes, {"ITC": ts})
        assert "ITC" in subject
        assert "ITC" in html
        assert "FII" in html

    def test_subject_truncates_at_five_tickers(self):
        changes = []
        ts_map = {}
        for i in range(8):
            tkr = f"T{i}"
            ts_map[tkr] = _make_ticker(tkr, quarters=[_make_quarter("Mar 2026", fii=22.0)])
            changes.append(ShpChange(
                ticker=tkr, name=tkr, category="FII",
                old_quarter="Mar 2026", new_quarter="Mar 2026",
                old_value=20.0, new_value=22.0, delta=2.0,
            ))
        subject, _, _ = render_email(changes, ts_map)
        assert "8 stocks" in subject
        assert "(+3 more)" in subject

    def test_returns_three_strings(self):
        ts = _make_ticker("ITC", quarters=[_make_quarter("Mar 2026")])
        s, p, h = render_email([], {"ITC": ts})
        assert isinstance(s, str) and isinstance(p, str) and isinstance(h, str)
        assert "<html" in h.lower()

    def test_plain_text_includes_arrow_notation(self):
        ts = _make_ticker("ITC", quarters=[_make_quarter("Mar 2026", fii=22.0)])
        changes = [ShpChange(
            ticker="ITC", name="ITC", category="FII",
            old_quarter="Mar 2026", new_quarter="Mar 2026",
            old_value=20.0, new_value=22.0, delta=2.0,
        )]
        _, plain, _ = render_email(changes, {"ITC": ts})
        # Should show ▲ for increase
        assert "▲" in plain
        assert "FII" in plain
        assert "+22.00" in plain
        assert "+2.00" in plain


# ---------- _next_run_ist ----------

class TestNextRunIst:
    def test_returns_today_if_before_1635(self, monkeypatch):
        class FakeDT:
            @classmethod
            def now(cls, tz=None):
                if tz is IST:
                    return datetime(2026, 6, 28, 9, 0, tzinfo=IST)
                return datetime(2026, 6, 28, 3, 30, tzinfo=timezone.utc)
        monkeypatch.setattr(sha, "datetime", FakeDT)
        nxt = _next_run_ist()
        # 16:35 IST = 11:05 UTC
        assert nxt.hour == 11 and nxt.minute == 5
        assert nxt.day == 28

    def test_returns_tomorrow_if_after_1635(self, monkeypatch):
        class FakeDT:
            @classmethod
            def now(cls, tz=None):
                if tz is IST:
                    return datetime(2026, 6, 28, 20, 0, tzinfo=IST)
                return datetime(2026, 6, 28, 14, 30, tzinfo=timezone.utc)
        monkeypatch.setattr(sha, "datetime", FakeDT)
        nxt = _next_run_ist()
        assert nxt.day == 29


# ---------- run_once ----------

class TestRunOnce:
    def test_first_run_persists_no_alert(self, tmp_data_dir, clean_smtp_env):
        """First-ever run: persist snapshot but don't alert (too noisy)."""
        snap = {"ITC": _make_ticker("ITC", quarters=[
            _make_quarter("Mar 2026", fii=20.0),
        ])}
        with patch.object(sha, "fetch_all", return_value=snap):
            result = run_once()
        assert result["fetch_ok"] is True
        assert result["stocks_with_changes"] == 0  # first run = no alert
        # But snapshot IS persisted
        assert sha.PREV_FILE.exists()

    def test_second_run_no_changes_returns_no_alert(self, tmp_data_dir, clean_smtp_env):
        ts = _make_ticker("ITC", quarters=[_make_quarter("Mar 2026", fii=20.0)])
        with patch.object(sha, "fetch_all", return_value={"ITC": ts}):
            run_once()  # baseline
            result = run_once()  # same snapshot
        assert result["stocks_with_changes"] == 0
        assert "no changes" in result["email"]["reason"]

    def test_run_detects_changes_between_runs(self, tmp_data_dir, clean_smtp_env):
        ts1 = _make_ticker("ITC", quarters=[_make_quarter("Mar 2026", fii=20.0)])
        ts2 = _make_ticker("ITC", quarters=[_make_quarter("Mar 2026", fii=22.0)])
        with patch.object(sha, "fetch_all", return_value={"ITC": ts1}):
            run_once()  # baseline
        with patch.object(sha, "fetch_all", return_value={"ITC": ts2}):
            result = run_once()  # 2% increase in FII
        assert result["stocks_with_changes"] == 1
        assert "ITC" in result["tickers_changed"]
        assert result["email"]["mode"] == "dry_run"

    def test_run_force_email_sends_when_no_changes(self, tmp_data_dir, clean_smtp_env):
        ts = _make_ticker("ITC", quarters=[_make_quarter("Mar 2026", fii=20.0)])
        with patch.object(sha, "fetch_all", return_value={"ITC": ts}):
            run_once()  # baseline
            result = run_once(force_email=True)  # force
        assert result["stocks_with_changes"] == 0
        # force_email=True means even dry-run mode is exercised
        assert result["email"]["mode"] == "dry_run"

    def test_run_handles_fetch_failure(self, tmp_data_dir, clean_smtp_env):
        with patch.object(sha, "fetch_all", side_effect=RuntimeError("net down")):
            result = run_once()
        assert result["fetch_ok"] is False
        assert result["stocks_with_changes"] == 0
        assert result["errors"]

    def test_alert_log_persists_across_runs(self, tmp_data_dir, clean_smtp_env):
        ts = _make_ticker("ITC", quarters=[_make_quarter("Mar 2026", fii=20.0)])
        with patch.object(sha, "fetch_all", return_value={"ITC": ts}):
            for _ in range(3):
                run_once()
        assert sha.LOG_FILE.exists()
        log = json.loads(sha.LOG_FILE.read_text())
        assert len(log) == 3


# ---------- Integration ----------

@pytest.mark.integration
@pytest.mark.xfail(reason="Trendlyne rate-limits automated fetches; "
                          "may return HTTP 405 under heavy load",
                   strict=False)
class TestIntegration:
    """Real-network tests against Trendlyne (xfail when rate-limited)."""

    def test_fetch_real_itc_shareholding(self):
        """Fetch ITC and verify the table parses with sensible values."""
        ts = fetch_one("ITC")
        assert ts is not None
        assert ts.ticker == "ITC"
        assert len(ts.quarters) >= 4  # at least 4 quarters
        # Latest quarter should be recent (Mar/Jun/Sep/Dec of recent year)
        q = ts.quarters[0]
        assert re.match(r"(Mar|Jun|Sep|Dec) \d{4}", q.quarter)
        # ITC has 0% promoter (public company)
        assert q.promoter == 0.0
        # FII typically 30-45% for ITC
        assert 25 < q.fii < 50

    def test_fetch_real_reliance_shareholding(self):
        """RELIANCE has 50% promoter (Ambani family)."""
        ts = fetch_one("RELIANCE")
        assert ts is not None
        q = ts.quarters[0]
        assert 45 < q.promoter < 55  # ~50%
        assert q.fii > 0
        assert q.mutual_funds > 0

    def test_real_run_once_first_run_persists(self, tmp_data_dir, clean_smtp_env):
        """Full integration: fetch all 8 tickers, persist snapshot."""
        result = run_once()
        assert result["fetch_ok"] is True
        assert result["stocks_with_changes"] == 0  # first run
        # Snapshot persisted
        assert sha.PREV_FILE.exists()
        prev = _load_prev()
        assert len(prev) >= 6  # at least 6 of 8 tickers parsed OK