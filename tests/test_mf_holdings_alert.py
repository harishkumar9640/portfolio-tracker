"""
Tests for mf_holdings_alert.py

Covers:
  - diff_snapshots: detects field changes for mfs_bought, mfs_sold,
    net_change_shares, total_mfs_holding, asof, top_buyer.name,
    top_buyer.shares, top_seller.name, top_seller.shares
  - diff_snapshots: returns empty list when snapshots are identical
  - diff_snapshots: handles tickers that appear / disappear between
    snapshots
  - render_email: subject line for "no changes" and "N changed"
  - render_email: HTML body contains ticker + field names
  - is_dry_run: returns True when SMTP creds are missing
  - send_email: in dry-run mode, returns sent=False without raising
  - run_once: persists the current snapshot for next-day diff
  - run_once: returns "no changes" status when diff is empty
  - run_once: returns "stocks_with_changes > 0" when diff is non-empty
  - run_once: handles snapshot fetch failure gracefully
  - Alert log persistence (last 30 runs)
  - _next_run_ist: returns today 16:30 IST if before, else tomorrow
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

import mf_holdings_alert as mha  # noqa: E402
from mf_holdings_alert import (  # noqa: E402
    MfHoldingChange,
    diff_snapshots,
    render_email,
    is_dry_run,
    send_email,
    run_once,
    IST,
    PREV_SNAPSHOT_FILE,
    ALERT_LOG_FILE,
    _next_run_ist,
    _load_prev_snapshot,
    _save_curr_snapshot,
)


# ---------- Fixtures ----------

def _snap(tkr, *, mfs_bought=10, mfs_sold=5, net=1000,
          total_holding=100, asof="May 2026",
          top_buyer=("Buyer A", 500, 0.05),
          top_seller=("Seller X", 200, 0.02)):
    """Build a synthetic snapshot dict for one ticker."""
    return {
        "ticker": tkr,
        "name": tkr,
        "mfs_bought": mfs_bought,
        "mfs_sold": mfs_sold,
        "net_change_shares": net,
        "total_mfs_holding": total_holding,
        "asof": asof,
        "top_buyer": {"name": top_buyer[0], "shares": top_buyer[1],
                      "pct_of_company": top_buyer[2]},
        "top_seller": {"name": top_seller[0], "shares": top_seller[1],
                       "pct_of_company": top_seller[2]},
    }


@pytest.fixture
def tmp_data_dir(tmp_path, monkeypatch):
    """Point PREV_SNAPSHOT_FILE and ALERT_LOG_FILE at a tmp dir."""
    monkeypatch.setattr(mha, "PREV_SNAPSHOT_FILE", tmp_path / "mf_holdings_prev.json")
    monkeypatch.setattr(mha, "ALERT_LOG_FILE", tmp_path / "mf_holdings_alert_log.json")
    return tmp_path


@pytest.fixture
def clean_env(monkeypatch):
    """Strip MF_ALERT_* env vars so is_dry_run() returns True."""
    for k in ("MF_ALERT_DRY_RUN", "MF_ALERT_SMTP_HOST", "MF_ALERT_SMTP_USER",
              "MF_ALERT_SMTP_PASS", "MF_ALERT_SMTP_PORT", "MF_ALERT_TO",
              "MF_ALERT_FROM"):
        monkeypatch.delenv(k, raising=False)


# ---------- diff_snapshots ----------

class TestDiffSnapshots:
    def test_identical_snapshots_yield_no_changes(self):
        s = {"ITC": _snap("ITC")}
        assert diff_snapshots(s, s) == []

    def test_mfs_bought_change_detected(self):
        prev = {"ITC": _snap("ITC", mfs_bought=10)}
        curr = {"ITC": _snap("ITC", mfs_bought=12)}
        changes = diff_snapshots(prev, curr)
        assert len(changes) == 1
        assert changes[0].field == "mfs_bought"
        assert changes[0].old == 10 and changes[0].new == 12
        assert changes[0].delta == 2

    def test_multiple_field_changes_one_ticker(self):
        prev = {"ITC": _snap("ITC", mfs_bought=10, net=1000, total_holding=100)}
        curr = {"ITC": _snap("ITC", mfs_bought=15, net=2000, total_holding=110)}
        changes = diff_snapshots(prev, curr)
        fields = {c.field for c in changes}
        assert "mfs_bought" in fields
        assert "net_change_shares" in fields
        assert "total_mfs_holding" in fields

    def test_top_buyer_name_change_detected(self):
        prev = {"ITC": _snap("ITC", top_buyer=("Old Buyer", 100, 0.01))}
        curr = {"ITC": _snap("ITC", top_buyer=("New Buyer", 200, 0.02))}
        changes = diff_snapshots(prev, curr)
        fields = {c.field for c in changes}
        assert "top_buyer.name" in fields
        assert "top_buyer.shares" in fields
        # Both fields changed
        assert len([c for c in changes if c.field.startswith("top_buyer.")]) == 2

    def test_top_seller_name_change_detected(self):
        prev = {"ITC": _snap("ITC", top_seller=("Old Seller", 100, 0.01))}
        curr = {"ITC": _snap("ITC", top_seller=("New Seller", 200, 0.02))}
        changes = diff_snapshots(prev, curr)
        assert any(c.field == "top_seller.name" for c in changes)
        assert any(c.field == "top_seller.shares" for c in changes)

    def test_new_ticker_in_curr(self):
        """When a ticker appears for the first time, every watched field
        looks 'changed' (None -> value)."""
        prev = {"ITC": _snap("ITC")}
        curr = {"ITC": _snap("ITC"), "RELIANCE": _snap("RELIANCE")}
        changes = diff_snapshots(prev, curr)
        rel_changes = [c for c in changes if c.ticker == "RELIANCE"]
        # Should see mfs_bought, mfs_sold, net_change_shares,
        # total_mfs_holding, asof, top_buyer.name, top_buyer.shares,
        # top_seller.name, top_seller.shares
        assert len(rel_changes) >= 8

    def test_dropped_ticker_in_curr(self):
        """When a ticker disappears, fields look changed (value -> None)."""
        prev = {"ITC": _snap("ITC"), "RELIANCE": _snap("RELIANCE")}
        curr = {"ITC": _snap("ITC")}
        changes = diff_snapshots(prev, curr)
        rel_changes = [c for c in changes if c.ticker == "RELIANCE"]
        assert any(c.field == "mfs_bought" for c in rel_changes)
        assert any(c.new is None for c in rel_changes)

    def test_asof_change_detected(self):
        """A new month (asof change) should be flagged so the user
        knows it's a fresh monthly report."""
        prev = {"ITC": _snap("ITC", asof="Apr 2026")}
        curr = {"ITC": _snap("ITC", asof="May 2026")}
        changes = diff_snapshots(prev, curr)
        assert any(c.field == "asof" for c in changes)


# ---------- render_email ----------

class TestRenderEmail:
    def test_no_changes_subject(self):
        curr = {"ITC": _snap("ITC")}
        subject, plain, html = render_email([], curr, prev_snapshot=curr)
        assert "No changes" in subject
        assert "ITC" in plain  # per-stock snapshot table

    def test_changes_subject_lists_tickers(self):
        prev = {"ITC": _snap("ITC", mfs_bought=10)}
        curr = {"ITC": _snap("ITC", mfs_bought=15),
                "RELIANCE": _snap("RELIANCE")}
        changes = diff_snapshots(prev, curr)
        subject, plain, html = render_email(changes, curr, prev_snapshot=prev)
        assert "ITC" in subject
        assert "ITC" in html
        assert "RELIANCE" in html

    def test_html_includes_top_buyer_seller(self):
        prev = {"ITC": _snap("ITC", top_buyer=("Old", 100, 0.01))}
        curr = {"ITC": _snap("ITC", top_buyer=("New", 200, 0.02))}
        changes = diff_snapshots(prev, curr)
        _, _, html = render_email(changes, curr, prev_snapshot=prev)
        assert "top_buyer.name" in html
        assert "New" in html

    def test_subject_truncates_at_five_tickers(self):
        """If 8 tickers changed, subject shows first 5 + '(+3 more)'."""
        prev = {f"T{i}": _snap(f"T{i}", mfs_bought=10) for i in range(8)}
        curr = {f"T{i}": _snap(f"T{i}", mfs_bought=20) for i in range(8)}
        changes = diff_snapshots(prev, curr)
        subject, _, _ = render_email(changes, curr, prev_snapshot=prev)
        assert "8 stocks changed" in subject
        assert "(+3 more)" in subject

    def test_returns_three_strings(self):
        s, p, h = render_email([], {}, prev_snapshot={})
        assert isinstance(s, str) and isinstance(p, str) and isinstance(h, str)
        assert "<html" in h.lower()  # HTML body


# ---------- is_dry_run / send_email ----------

class TestDryRun:
    def test_dry_run_when_no_smtp_creds(self, clean_env):
        assert is_dry_run() is True

    def test_dry_run_when_explicit_flag(self, monkeypatch, clean_env):
        monkeypatch.setenv("MF_ALERT_DRY_RUN", "1")
        assert is_dry_run() is True

    def test_dry_run_when_host_only(self, monkeypatch, clean_env):
        monkeypatch.setenv("MF_ALERT_SMTP_HOST", "smtp.gmail.com")
        # Still missing USER/PASS/TO -> dry-run
        assert is_dry_run() is True

    def test_not_dry_run_when_creds_present(self, monkeypatch, clean_env):
        for k, v in {
            "MF_ALERT_SMTP_HOST": "smtp.gmail.com",
            "MF_ALERT_SMTP_USER": "alice@example.com",
            "MF_ALERT_SMTP_PASS": "secret",
            "MF_ALERT_TO": "alice@example.com",
        }.items():
            monkeypatch.setenv(k, v)
        assert is_dry_run() is False

    def test_send_email_dry_run_returns_sent_false(self, clean_env):
        result = send_email("Test subject", "Plain text", "<html>html</html>")
        assert result["sent"] is False
        assert result["mode"] == "dry_run"
        assert result["subject"] == "Test subject"


# ---------- run_once ----------

class TestRunOnce:
    def test_run_once_dry_run_no_changes(self, tmp_data_dir, clean_env):
        """First-ever run: no previous snapshot -> diff has all-new tickers,
        but we still run; the result should include 'stocks_with_changes'."""
        # Patch mf_holdings.get_mf_holdings to return a stable snapshot
        snap = {"ITC": _snap("ITC")}
        with patch.object(mha.mf_holdings, "get_mf_holdings", return_value=snap):
            result = mha.run_once()
        assert result["snapshot_ok"] is True
        # First run: previous is empty -> all fields look new -> 8 changes
        assert result["stocks_with_changes"] >= 1
        assert result["email"]["sent"] is False  # dry-run mode
        assert result["email"]["mode"] == "dry_run"

    def test_run_once_persists_snapshot_for_next_time(self, tmp_data_dir, clean_env):
        snap = {"ITC": _snap("ITC")}
        with patch.object(mha.mf_holdings, "get_mf_holdings", return_value=snap):
            mha.run_once()
        prev = _load_prev_snapshot()
        assert "ITC" in prev

    def test_run_once_no_changes_second_run(self, tmp_data_dir, clean_env):
        """If the second run sees the same snapshot, no email is sent."""
        snap = {"ITC": _snap("ITC")}
        with patch.object(mha.mf_holdings, "get_mf_holdings", return_value=snap):
            mha.run_once()  # first run -> persists
            result = mha.run_once()  # second run -> no changes
        assert result["stocks_with_changes"] == 0
        assert "no changes" in result["email"]["reason"]

    def test_run_once_detects_changes_between_runs(self, tmp_data_dir, clean_env):
        """If the snapshot changes between runs, we get changes + dry-run email."""
        with patch.object(mha.mf_holdings, "get_mf_holdings",
                          return_value={"ITC": _snap("ITC", mfs_bought=10)}):
            mha.run_once()  # baseline
        with patch.object(mha.mf_holdings, "get_mf_holdings",
                          return_value={"ITC": _snap("ITC", mfs_bought=20)}):
            result = mha.run_once()  # changed
        assert result["stocks_with_changes"] == 1
        assert "ITC" in result["tickers_changed"]
        assert result["email"]["sent"] is False  # dry-run mode
        assert result["email"]["mode"] == "dry_run"
        assert "ITC" in result["email"]["subject"]

    def test_run_once_force_email_sends_no_changes(self, tmp_data_dir, clean_env):
        """force_email=True sends even when nothing changed."""
        snap = {"ITC": _snap("ITC")}
        with patch.object(mha.mf_holdings, "get_mf_holdings", return_value=snap):
            mha.run_once()  # baseline
            result = mha.run_once(force_email=True)  # force
        assert result["stocks_with_changes"] == 0
        assert result["email"]["mode"] == "dry_run"  # still dry-run because no creds
        # But the subject should be the "no changes" one
        assert "No changes" in result["email"]["subject"]

    def test_run_once_handles_snapshot_failure(self, tmp_data_dir, clean_env):
        """If Trendlyne fetch fails, run_once returns snapshot_ok=False
        but doesn't raise."""
        def boom():
            raise RuntimeError("network down")
        with patch.object(mha.mf_holdings, "get_mf_holdings", side_effect=boom):
            result = mha.run_once()
        assert result["snapshot_ok"] is False
        assert result["stocks_with_changes"] == 0
        assert result["errors"]

    def test_alert_log_persists_across_runs(self, tmp_data_dir, clean_env):
        """Each run appends an entry; only last 30 are kept."""
        snap = {"ITC": _snap("ITC")}
        with patch.object(mha.mf_holdings, "get_mf_holdings", return_value=snap):
            for _ in range(3):
                mha.run_once()
        # Use the module symbol (which monkeypatch updated)
        assert mha.ALERT_LOG_FILE.exists()
        log = json.loads(mha.ALERT_LOG_FILE.read_text())
        assert len(log) == 3


# ---------- _next_run_ist ----------

class TestNextRunIst:
    def test_returns_today_if_before_1630(self, monkeypatch):
        # Pretend it's 2026-06-26 09:00 IST
        monkeypatch.setattr(
            "mf_holdings_alert.datetime",
            _FakeDatetime(fixed_ist=datetime(2026, 6, 26, 9, 0, tzinfo=IST)),
        )
        nxt = _next_run_ist()
        # Should be 2026-06-26 16:30 IST = 2026-06-26 11:00 UTC (naive)
        assert nxt.hour == 11  # 16:30 IST = 11:00 UTC
        assert nxt.day == 26

    def test_returns_tomorrow_if_after_1630(self, monkeypatch):
        monkeypatch.setattr(
            "mf_holdings_alert.datetime",
            _FakeDatetime(fixed_ist=datetime(2026, 6, 26, 20, 0, tzinfo=IST)),
        )
        nxt = _next_run_ist()
        assert nxt.day == 27  # tomorrow


class _FakeDatetime:
    """Minimal datetime mock that always returns the fixed IST time."""
    def __init__(self, fixed_ist):
        self._fixed_ist = fixed_ist

    def now(self, tz=None):
        if tz is None:
            # Return naive UTC equivalent
            return self._fixed_ist.astimezone(timezone.utc).replace(tzinfo=None)
        return self._fixed_ist

    def __call__(self, *args, **kwargs):
        return datetime(*args, **kwargs)


# ---------- Real network: dry-run with real fetch ----------

@pytest.mark.integration
class TestIntegration:
    """These hit the real Trendlyne. Skip if no network or cache is fresh."""

    def test_real_first_run_detects_changes(self, tmp_data_dir, clean_env):
        # Wipe any persisted prev snapshot
        if mha.PREV_SNAPSHOT_FILE.exists():
            mha.PREV_SNAPSHOT_FILE.unlink()
        result = mha.run_once()
        assert result["snapshot_ok"] is True
        # First-ever run sees an empty previous; at least 8 stocks show as 'changed'
        assert result["stocks_with_changes"] >= 8

    def test_real_second_run_no_changes(self, tmp_data_dir, clean_env):
        mha.run_once()  # persist
        result = mha.run_once()
        assert result["stocks_with_changes"] == 0


# ---------- Edge cases ----------

class TestEdgeCases:
    def test_diff_handles_missing_top_buyer_gracefully(self):
        """A snapshot might lack top_buyer if the regex didn't match.
        We shouldn't crash."""
        prev = {"ITC": {"ticker": "ITC", "name": "ITC", "mfs_bought": 10}}
        curr = {"ITC": {"ticker": "ITC", "name": "ITC", "mfs_bought": 15}}
        changes = diff_snapshots(prev, curr)
        assert len(changes) == 1  # only mfs_bought

    def test_send_email_constructs_valid_message(self, clean_env):
        """Even in dry-run, the EmailMessage is built (we just don't send)."""
        # We can verify render_email produces valid output, send_email
        # accepts it without raising, and the returned dict has the
        # expected keys.
        s, p, h = render_email([], {"ITC": _snap("ITC")}, prev_snapshot={})
        result = send_email(s, p, h)
        assert "sent" in result
        assert "subject" in result
        assert result["subject"] == s

    def test_run_once_with_empty_curr_dict(self, tmp_data_dir, clean_env):
        """If the snapshot fetch returns empty (all tickers failed),
        run_once should still return without raising."""
        with patch.object(mha.mf_holdings, "get_mf_holdings", return_value={}):
            result = mha.run_once()
        assert result["snapshot_ok"] is True
        assert result["stocks_with_changes"] == 0