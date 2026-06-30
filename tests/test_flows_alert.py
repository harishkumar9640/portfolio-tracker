"""
Tests for pipeline.flows_alert.py.

Coverage:
  - FII/DII threshold logic (alert if |net| > ₹5,000 cr)
  - Bulk/block deal CSV parsing
  - Portfolio filter (only stocks we hold)
  - Deal-alert rendering (BUY vs SELL side)
  - Dedup (fii_dii_key, deal_key)
  - History archival (FII/DII overwrite semantics)
  - Dry-run safety
  - CLI smoke tests
"""
from __future__ import annotations

import csv
import io
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

import pytest

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

import pipeline.flows_alert as fa  # noqa: E402
from pipeline.flows_alert import (  # noqa: E402
    FiiDiiRow, DealRow, _fii_dii_key, _deal_key,
    render_fii_dii_alert, render_deal_alert, _parse_deals_csv,
    filter_for_portfolio, FII_DII_LARGE_FLOW_CR,
)


IST = fa.IST


# ===========================================================================
# 1. Threshold logic — FII/DII alerts only fire above ₹5,000 cr
# ===========================================================================

class TestFiiDiiThreshold:
    def _rows(self, fii_net: float, dii_net: float) -> list[FiiDiiRow]:
        return [
            FiiDiiRow(category="FII/FPI", date="25-Jun-2026",
                      buy_value_cr=20000, sell_value_cr=20000 - fii_net,
                      net_value_cr=fii_net),
            FiiDiiRow(category="DII", date="25-Jun-2026",
                      buy_value_cr=20000, sell_value_cr=20000 - dii_net,
                      net_value_cr=dii_net),
        ]

    def test_no_alert_when_both_below_threshold(self):
        # Both well below ₹5,000 cr threshold
        rows = self._rows(fii_net=383, dii_net=200)
        text = render_fii_dii_alert(rows)
        assert text is None

    def test_alert_at_exactly_threshold(self):
        """At exactly the threshold, we DO alert (>= semantics)."""
        rows = self._rows(fii_net=FII_DII_LARGE_FLOW_CR, dii_net=0)
        text = render_fii_dii_alert(rows)
        assert text is not None
        assert "FII/FPI" in text

    def test_alert_when_fii_above(self):
        rows = self._rows(fii_net=-10000, dii_net=0)
        text = render_fii_dii_alert(rows)
        assert text is not None
        assert "FII/FPI" in text
        assert "SOLD" in text
        assert "10,000" in text

    def test_alert_when_dii_above(self):
        rows = self._rows(fii_net=0, dii_net=7000)
        text = render_fii_dii_alert(rows)
        assert text is not None
        assert "DII" in text
        assert "BOUGHT" in text

    def test_alert_when_both_above(self):
        rows = self._rows(fii_net=-8000, dii_net=9000)
        text = render_fii_dii_alert(rows)
        assert text is not None
        assert "FII" in text
        assert "DII" in text

    def test_alert_uses_emoji_for_direction(self):
        """FII selling = red, FII buying = green; same for DII."""
        rows_sell = self._rows(fii_net=-6000, dii_net=0)
        text_sell = render_fii_dii_alert(rows_sell)
        assert "🔴" in text_sell

        rows_buy = self._rows(fii_net=0, dii_net=6000)
        text_buy = render_fii_dii_alert(rows_buy)
        assert "🟢" in text_buy

    def test_alert_includes_portfolio_specific_impact(self):
        """The 'What this means' section should name specific tickers."""
        rows = self._rows(fii_net=-8000, dii_net=9000)
        text = render_fii_dii_alert(rows)
        # Large-cap headwind when FII selling
        assert "RELIANCE" in text or "ITC" in text
        # Mid/small-cap tailwind when DII buying
        assert "KNRCON" in text or "IRCON" in text


# ===========================================================================
# 2. CSV parsing — bulk & block deals
# ===========================================================================

class TestDealsCsvParsing:
    def test_parses_bulk_csv(self):
        body = (
            "Date,Symbol,Security Name,Client Name,Buy/Sell,"
            "Quantity Traded,Trade Price / Wght. Avg. Price,Remarks\n"
            "25-JUN-2026,ITC,ITC Limited,SBI MUTUAL FUND,BUY,"
            "2500000,425.50,-\n"
        )
        rows = _parse_deals_csv(body, "bulk")
        assert len(rows) == 1
        r = rows[0]
        assert r.deal_type == "bulk"
        assert r.symbol == "ITC"
        assert r.client_name == "SBI MUTUAL FUND"
        assert r.side == "BUY"
        assert r.quantity == 2500000
        assert r.price == 425.50
        assert r.remarks == "-"

    def test_parses_block_csv(self):
        # Block deal CSV has fewer columns (no Remarks)
        body = (
            "Date,Symbol,Security Name,Client Name,Buy/Sell,"
            "Quantity Traded,Trade Price / Wght. Avg. Price\n"
            "25-JUN-2026,RELIANCE,Reliance Industries Limited,"
            "NORTHERN TRUST,SELL,500000,2890.00\n"
        )
        rows = _parse_deals_csv(body, "block")
        assert len(rows) == 1
        assert rows[0].deal_type == "block"
        assert rows[0].symbol == "RELIANCE"

    def test_handles_large_unquoted_numbers(self):
        """NSE never uses commas in numeric fields; test large numbers."""
        body = (
            "Date,Symbol,Security Name,Client Name,Buy/Sell,"
            "Quantity Traded,Trade Price / Wght. Avg. Price,Remarks\n"
            "25-JUN-2026,ITC,ITC Limited,FII A,BUY,"
            "12500000,425.50,-\n"
        )
        rows = _parse_deals_csv(body, "bulk")
        assert len(rows) == 1
        assert rows[0].quantity == 12500000
        assert rows[0].price == 425.50

    def test_skips_malformed_rows(self):
        body = (
            "Date,Symbol,Security Name,Client Name,Buy/Sell,"
            "Quantity Traded,Trade Price / Wght. Avg. Price,Remarks\n"
            "25-JUN-2026,ITC,ITC Limited,FII,BUY,notanumber,425,-\n"
            "25-JUN-2026,RELIANCE,RIL,FII,SELL,1000,2890,-\n"
        )
        rows = _parse_deals_csv(body, "bulk")
        # The malformed row is skipped; only the valid one remains
        assert len(rows) == 1
        assert rows[0].symbol == "RELIANCE"


# ===========================================================================
# 3. Portfolio filter
# ===========================================================================

class TestFilterForPortfolio:
    def test_keeps_portfolio_stocks(self):
        deals = [
            DealRow(deal_type="bulk", date="25-Jun-2026", symbol="ITC",
                    security_name="ITC Limited", client_name="X",
                    side="BUY", quantity=1000, price=425),
            DealRow(deal_type="bulk", date="25-Jun-2026", symbol="RELIANCE",
                    security_name="Reliance Industries", client_name="Y",
                    side="SELL", quantity=500, price=2890),
        ]
        result = filter_for_portfolio(deals)
        assert len(result) == 2
        symbols = {d.symbol for d in result}
        assert symbols == {"ITC", "RELIANCE"}

    def test_filters_non_portfolio_stocks(self):
        deals = [
            DealRow(deal_type="bulk", date="25-Jun-2026", symbol="ITC",
                    security_name="ITC Limited", client_name="X",
                    side="BUY", quantity=1000, price=425),
            DealRow(deal_type="bulk", date="25-Jun-2026", symbol="WIPRO",
                    security_name="Wipro Limited", client_name="Y",
                    side="BUY", quantity=1000, price=400),
            DealRow(deal_type="bulk", date="25-Jun-2026", symbol="TATAMOTORS",
                    security_name="Tata Motors", client_name="Z",
                    side="SELL", quantity=2000, price=900),
        ]
        result = filter_for_portfolio(deals)
        assert len(result) == 1
        assert result[0].symbol == "ITC"

    def test_empty_deals_returns_empty(self):
        assert filter_for_portfolio([]) == []


# ===========================================================================
# 4. Deal-alert rendering
# ===========================================================================

class TestDealAlertRendering:
    def test_buy_deal_uses_green_emoji(self):
        deal = DealRow(deal_type="bulk", date="25-Jun-2026", symbol="ITC",
                       security_name="ITC Limited", client_name="SBI MF",
                       side="BUY", quantity=2500000, price=425.50)
        text = render_deal_alert(deal)
        assert "🟢" in text
        assert "BUY" not in text.upper().split("VALUE")[0] or "bought" in text.lower()

    def test_sell_deal_uses_red_emoji(self):
        deal = DealRow(deal_type="block", date="25-Jun-2026", symbol="RELIANCE",
                       security_name="Reliance Industries", client_name="NT",
                       side="SELL", quantity=500000, price=2890.00)
        text = render_deal_alert(deal)
        assert "🔴" in text
        assert "sold" in text.lower()

    def test_deal_includes_value_in_crores(self):
        deal = DealRow(deal_type="bulk", date="25-Jun-2026", symbol="ITC",
                       security_name="ITC Limited", client_name="X",
                       side="BUY", quantity=2_500_000, price=425.50)
        text = render_deal_alert(deal)
        # 2.5M * 425.50 = 1,063,750,000 = 106.375 cr
        assert "106.38" in text or "106.4" in text

    def test_deal_includes_hashtags(self):
        deal = DealRow(deal_type="bulk", date="25-Jun-2026", symbol="ITC",
                       security_name="ITC Limited", client_name="X",
                       side="BUY", quantity=1000, price=425)
        text = render_deal_alert(deal)
        assert "#ITC" in text
        assert "#BULKDeal" in text

    def test_block_deal_uses_block_hashtag(self):
        deal = DealRow(deal_type="block", date="25-Jun-2026", symbol="ITC",
                       security_name="ITC Limited", client_name="X",
                       side="SELL", quantity=1000, price=425)
        text = render_deal_alert(deal)
        assert "#BLOCKDeal" in text


# ===========================================================================
# 5. Dedup keys
# ===========================================================================

class TestDedupKeys:
    def test_fii_dii_key_uses_date(self):
        assert _fii_dii_key("25-Jun-2026") == "fii_dii|25-Jun-2026"

    def test_deal_key_is_specific(self):
        deal1 = DealRow(deal_type="bulk", date="25-Jun-2026", symbol="ITC",
                        security_name="X", client_name="A",
                        side="BUY", quantity=100, price=425)
        deal2 = DealRow(deal_type="bulk", date="25-Jun-2026", symbol="ITC",
                        security_name="X", client_name="B",
                        side="BUY", quantity=100, price=425)
        assert _deal_key(deal1) != _deal_key(deal2)

    def test_deal_key_distinguishes_buy_sell(self):
        deal_buy = DealRow(deal_type="bulk", date="25-Jun-2026", symbol="ITC",
                           security_name="X", client_name="A",
                           side="BUY", quantity=100, price=425)
        deal_sell = DealRow(deal_type="bulk", date="25-Jun-2026", symbol="ITC",
                            security_name="X", client_name="A",
                            side="SELL", quantity=100, price=425)
        assert _deal_key(deal_buy) != _deal_key(deal_sell)


# ===========================================================================
# 6. History archival — overwrite semantics for FII/DII
# ===========================================================================

class TestFiiDiiArchival:
    def test_new_entry_appended(self, monkeypatch, tmp_path):
        hist_file = tmp_path / "fii_dii_history.json"
        monkeypatch.setattr(fa, "HISTORY_FILE", hist_file)

        rows = [FiiDiiRow(category="FII/FPI", date="25-Jun-2026",
                          buy_value_cr=20000, sell_value_cr=19000,
                          net_value_cr=1000)]
        fa.archive_fii_dii(rows)
        assert json.loads(hist_file.read_text()) == [{
            "category": "FII/FPI", "date": "25-Jun-2026",
            "buy_value_cr": 20000, "sell_value_cr": 19000,
            "net_value_cr": 1000,
        }]

    def test_same_day_overwrites(self, monkeypatch, tmp_path):
        hist_file = tmp_path / "fii_dii_history.json"
        monkeypatch.setattr(fa, "HISTORY_FILE", hist_file)

        # Provisional number
        fa.archive_fii_dii([FiiDiiRow(
            category="FII/FPI", date="25-Jun-2026",
            buy_value_cr=15000, sell_value_cr=12000, net_value_cr=3000,
        )])
        # Final number (same date)
        fa.archive_fii_dii([FiiDiiRow(
            category="FII/FPI", date="25-Jun-2026",
            buy_value_cr=18000, sell_value_cr=12000, net_value_cr=6000,
        )])
        history = json.loads(hist_file.read_text())
        assert len(history) == 1
        assert history[0]["net_value_cr"] == 6000
        assert history[0]["buy_value_cr"] == 18000

    def test_different_dates_both_kept(self, monkeypatch, tmp_path):
        hist_file = tmp_path / "fii_dii_history.json"
        monkeypatch.setattr(fa, "HISTORY_FILE", hist_file)

        fa.archive_fii_dii([FiiDiiRow(
            category="FII/FPI", date="24-Jun-2026",
            buy_value_cr=10000, sell_value_cr=8000, net_value_cr=2000,
        )])
        fa.archive_fii_dii([FiiDiiRow(
            category="FII/FPI", date="25-Jun-2026",
            buy_value_cr=12000, sell_value_cr=9000, net_value_cr=3000,
        )])
        history = json.loads(hist_file.read_text())
        assert len(history) == 2


class TestDealsArchival:
    def test_new_deal_appended(self, monkeypatch, tmp_path):
        deals_file = tmp_path / "deals.json"
        monkeypatch.setattr(fa, "DEALS_HISTORY_FILE", deals_file)

        deals = [DealRow(deal_type="bulk", date="25-Jun-2026", symbol="ITC",
                         security_name="ITC Limited", client_name="SBI",
                         side="BUY", quantity=1000, price=425)]
        fa.archive_deals(deals)
        data = json.loads(deals_file.read_text())
        assert len(data) == 1
        assert data[0]["symbol"] == "ITC"

    def test_duplicate_deal_not_duplicated(self, monkeypatch, tmp_path):
        deals_file = tmp_path / "deals.json"
        monkeypatch.setattr(fa, "DEALS_HISTORY_FILE", deals_file)

        deals = [DealRow(deal_type="bulk", date="25-Jun-2026", symbol="ITC",
                         security_name="ITC Limited", client_name="SBI",
                         side="BUY", quantity=1000, price=425)]
        fa.archive_deals(deals)
        fa.archive_deals(deals)   # duplicate
        data = json.loads(deals_file.read_text())
        assert len(data) == 1


# ===========================================================================
# 7. send_telegram — dry-run safety
# ===========================================================================

class TestSendTelegramDryRun:
    def test_dry_run_does_not_open_network(self, monkeypatch):
        monkeypatch.setenv("FLOWS_ALERT_DRY_RUN", "1")
        assert fa.is_dry_run() is True

        def fail(*args, **kwargs):
            raise AssertionError("urlopen should not be called")

        with mock.patch.object(fa.urllib.request, "urlopen",
                               side_effect=fail):
            result = fa.send_telegram("test")
        assert result["sent"] is False
        assert result["mode"] == "dry_run"

    def test_missing_credentials_returns_no_creds(self, monkeypatch):
        monkeypatch.setenv("FLOWS_ALERT_DRY_RUN", "0")
        monkeypatch.delenv("NEWS_TELEGRAM_BOT_TOKEN", raising=False)
        monkeypatch.delenv("NEWS_TELEGRAM_CHAT_ID", raising=False)
        monkeypatch.delenv("FLOWS_TELEGRAM_BOT_TOKEN", raising=False)
        monkeypatch.delenv("FLOWS_TELEGRAM_CHAT_ID", raising=False)

        def fail(*args, **kwargs):
            raise AssertionError("urlopen should not be called")

        with mock.patch.object(fa.urllib.request, "urlopen",
                               side_effect=fail):
            result = fa.send_telegram("test")
        assert result["sent"] is False
        assert result["mode"] == "no_credentials"


# ===========================================================================
# 8. run_once — wiring (mocked HTTP)
# ===========================================================================

class TestRunOnceWiring:
    def test_runs_all_three_pipelines(self, monkeypatch, tmp_path):
        # Redirect all file outputs to tmp_path
        monkeypatch.setattr(fa, "HISTORY_FILE",
                            tmp_path / "fii_dii_history.json")
        monkeypatch.setattr(fa, "DEALS_HISTORY_FILE",
                            tmp_path / "deals_history.json")
        monkeypatch.setattr(fa, "SEEN_FILE", tmp_path / "seen.json")
        monkeypatch.setattr(fa, "LOG_FILE_HISTORY",
                            tmp_path / "log.json")

        # Mock FII/DII fetch — small numbers, no alert
        monkeypatch.setattr(fa, "fetch_fii_dii", lambda: [
            FiiDiiRow(category="FII/FPI", date="25-Jun-2026",
                      buy_value_cr=10000, sell_value_cr=9000,
                      net_value_cr=1000),
            FiiDiiRow(category="DII", date="25-Jun-2026",
                      buy_value_cr=8000, sell_value_cr=9000,
                      net_value_cr=-1000),
        ])
        # Mock bulk — empty
        monkeypatch.setattr(fa, "fetch_bulk_deals", lambda: [])
        monkeypatch.setattr(fa, "fetch_block_deals", lambda: [])

        result = fa.run_once(today=datetime(2026, 6, 25, tzinfo=IST))
        assert result["fii_dii_rows"] == 2
        assert result["bulk_deals"] == 0
        assert result["block_deals"] == 0
        assert result["sent"] == 0     # below threshold

    def test_sends_deal_alert_for_portfolio_stock(self, monkeypatch, tmp_path):
        monkeypatch.setattr(fa, "HISTORY_FILE",
                            tmp_path / "fii_dii.json")
        monkeypatch.setattr(fa, "DEALS_HISTORY_FILE",
                            tmp_path / "deals.json")
        monkeypatch.setattr(fa, "SEEN_FILE", tmp_path / "seen.json")
        monkeypatch.setattr(fa, "LOG_FILE_HISTORY", tmp_path / "log.json")

        monkeypatch.setattr(fa, "fetch_fii_dii", lambda: [])

        bulk = [DealRow(deal_type="bulk", date="25-Jun-2026", symbol="ITC",
                        security_name="ITC Limited", client_name="SBI",
                        side="BUY", quantity=2_500_000, price=425.50)]
        monkeypatch.setattr(fa, "fetch_bulk_deals", lambda: bulk)
        monkeypatch.setattr(fa, "fetch_block_deals", lambda: [])

        sent_text = []
        monkeypatch.setattr(fa, "send_telegram",
                            lambda t: sent_text.append(t) or {
                                "sent": True, "mode": "dry_run",
                                "chars": len(t),
                            })

        result = fa.run_once(today=datetime(2026, 6, 25, tzinfo=IST))
        assert result["bulk_deals"] == 1
        assert result["sent"] == 1
        assert "ITC" in sent_text[0]

    def test_dedup_skips_already_seen(self, monkeypatch, tmp_path):
        seen_file = tmp_path / "seen.json"
        today_str = "2026-06-25"
        seen_file.write_text(json.dumps({
            "fii_dii|25-Jun-2026": today_str,
        }))
        monkeypatch.setattr(fa, "SEEN_FILE", seen_file)
        monkeypatch.setattr(fa, "HISTORY_FILE", tmp_path / "fii_dii.json")
        monkeypatch.setattr(fa, "DEALS_HISTORY_FILE", tmp_path / "deals.json")
        monkeypatch.setattr(fa, "LOG_FILE_HISTORY", tmp_path / "log.json")

        # Big flow but already alerted
        monkeypatch.setattr(fa, "fetch_fii_dii", lambda: [
            FiiDiiRow(category="FII/FPI", date="25-Jun-2026",
                      buy_value_cr=10000, sell_value_cr=20000,
                      net_value_cr=-10000),
            FiiDiiRow(category="DII", date="25-Jun-2026",
                      buy_value_cr=20000, sell_value_cr=10000,
                      net_value_cr=10000),
        ])
        monkeypatch.setattr(fa, "fetch_bulk_deals", lambda: [])
        monkeypatch.setattr(fa, "fetch_block_deals", lambda: [])

        result = fa.run_once(today=datetime(2026, 6, 25, tzinfo=IST))
        assert result["sent"] == 0
        assert result["skipped"] == 1


# ===========================================================================
# 9. CLI smoke tests
# ===========================================================================

class TestCLI:
    def test_test_render_prints_all_three_alerts(self, capsys):
        import subprocess
        proc = subprocess.run(
            [sys.executable, "-m", "pipeline.flows_alert", "--test-render"],
            capture_output=True, text=True, cwd=PROJECT,
        )
        assert proc.returncode == 0
        assert "FII / DII activity" in proc.stdout
        assert "BULK DEAL" in proc.stdout
        assert "BLOCK DEAL" in proc.stdout

    def test_run_once_with_dry_run(self):
        """--run-once --dry-run should run and exit cleanly."""
        import subprocess
        proc = subprocess.run(
            [sys.executable, "-m", "pipeline.flows_alert", "--run-once", "--dry-run"],
            capture_output=True, text=True, cwd=PROJECT, timeout=30,
        )
        assert proc.returncode == 0, (
            f"exit={proc.returncode}, stderr={proc.stderr}"
        )
        # stdout mixes INFO logs with a multi-line JSON dict. Find the
        # JSON block by collecting lines between '{' (alone) and '}'.
        lines = proc.stdout.splitlines()
        json_lines = []
        in_json = False
        for line in lines:
            stripped = line.strip()
            if stripped == "{":
                in_json = True
                json_lines.append(line)
            elif stripped == "}":
                json_lines.append(line)
                break
            elif in_json:
                json_lines.append(line)
        assert json_lines, (
            f"no JSON block in stdout:\n{proc.stdout}"
        )
        data = json.loads("\n".join(json_lines))
        assert "fii_dii_rows" in data
        assert "bulk_deals" in data
        assert "block_deals" in data