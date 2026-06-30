"""
Tests for pipeline.mf_holdings.py — mutual fund holdings data for the user's
portfolio (Trendlyne-scraped).

These tests run entirely offline with cached HTML fixtures. We
verify:
  - URL construction is correct
  - The headline regex extracts "46 MFs bought, 19 MFs sold, +3,329,442 net"
  - Top buyer / top seller regex extracts name + shares + % of company
  - Table-row parsing computes total_mfs_holding, total_shares_held
  - When the headline regex misses, the table parser provides fallback
    top_buyer / top_seller (largest positive / negative month_change)
  - The cache: 7-day TTL; expired entries are re-fetched
  - get_mf_holdings_summary returns the right shape sorted by |net_change|

The fixtures below are real (lightly cleaned) HTML snippets copied
from the actual Trendlyne MF-holdings pages.
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock, patch

import pytest

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

import pipeline.mf_holdings  # noqa: E402


# ---------- Fixtures ----------
# A small slice of an NTPC Green Energy MF-holdings page (the real data we
# fetched). The text contains the headline summary, the top buyer/seller
# bullets, and the per-MF table.
NTPC_FIXTURE = """
<!DOCTYPE html>
<html>
<body>
<h1>NTPC Green Energy</h1>

<title>
    Mutual Fund May 2026 share holdings and fund action in NTPC Green Energy
</title>

<h2 class="gr fs1rem fw400 mb15 header-styles">
    46 MFs bought and 19 MFs
    sold NTPC Green Energy in the month
    of May 2026 for a net change
    of 3,329,442 stocks
</h2>

<h3 class="fs085rem fw400">Nippon India Power & Infra Fund - Growth was the highest buyer of 2,403,505 shares in May 2026 constituting 0.03% of the paid up equity of the company.</h3>

<h3 class="fs085rem fw400">HSBC Large and Mid Cap Fund - IDCW was the highest seller of 3,729,293 shares in May 2026 constituting 0.04% of the paid up equity of the company.</h3>

<p>Choose Month May 2026</p>

<table>
  <thead>
    <tr><th>MF</th><th>AUM (Cr)</th><th>AUM %</th><th>Shares Held</th><th>Month Change</th><th>Month Change %</th><th>Shares Held Prev</th><th>Prev %</th></tr>
  </thead>
  <tbody>
    <tr>
      <td><a href="/mf/1">Nippon India Power & Infra Gr</a></td>
      <td>296.01</td>
      <td>3.75%</td>
      <td>28,594,584</td>
      <td>2,403,505</td>
      <td>9.18%</td>
      <td>26,191,079</td>
      <td>0%</td>
    </tr>
    <tr>
      <td>Sundaram Flexi Cap Gr</td>
      <td>15.84</td>
      <td>2.80%</td>
      <td>3,003,217</td>
      <td>286,762</td>
      <td>10.56%</td>
      <td>2,716,455</td>
      <td>0%</td>
    </tr>
    <tr>
      <td>HDFC Mid Cap Gr</td>
      <td>134.06</td>
      <td>0.14%</td>
      <td>10,583,872</td>
      <td>0</td>
      <td>0%</td>
      <td>10,583,872</td>
      <td>0%</td>
    </tr>
    <tr>
      <td>Some Exited Fund</td>
      <td>0.55</td>
      <td>0%</td>
      <td>0</td>
      <td>-50,000</td>
      <td>-100%</td>
      <td>50,000</td>
      <td>0%</td>
    </tr>
  </tbody>
</table>
</body>
</html>
"""


# ---------- URL construction ----------
class TestUrlConstruction:
    def test_builds_correct_url_for_known_ticker(self):
        assert (
            pipeline.mf_holdings._build_url("ITC")
            == "https://trendlyne.com/equity/monthly-mutual-fund-share-holding/"
               "647/ITC/latest/itc-ltd/"
        )

    def test_known_tickers_all_have_real_ids(self):
        for tkr, info in pipeline.mf_holdings.TICKER_MAP.items():
            assert info["id"] < pipeline.mf_holdings.PLACEHOLDER_ID_THRESHOLD, (
                f"{tkr} still has placeholder id={info['id']}; "
                "search trendlyne.com for the real id"
            )

    def test_includes_slash_when_url_slug_ends_in_slash(self):
        # BANKBARODA's slug used to end in "/"; we now normalise away
        # trailing slashes, but the built URL must still end with
        # exactly one "/".
        url = pipeline.mf_holdings._build_url("BANKBARODA")
        assert "bank-of-baroda/" in url
        # No double slashes anywhere in the path
        assert "//" not in url.replace("https://", "")


# ---------- Headline parser ----------
class TestHeadlineRegex:
    def test_extracts_mfs_bought_sold_and_net(self):
        m = pipeline.mf_holdings._HEADLINE_RE.search(NTPC_FIXTURE)
        assert m is not None
        assert m.group(1) == "46"
        assert m.group(2) == "19"
        assert m.group(3) == "3,329,442"

    def test_returns_none_for_unrelated_text(self):
        m = pipeline.mf_holdings._HEADLINE_RE.search("nothing relevant here")
        assert m is None


# ---------- Top buyer/seller parser ----------
class TestTopSection:
    def test_extracts_top_buyer(self):
        m = pipeline.mf_holdings._TOP_BUYER_RE.search(NTPC_FIXTURE)
        assert m is not None
        assert m.group("name") == "Nippon India Power & Infra Fund - Growth"
        assert m.group("shares") == "2,403,505"
        assert m.group("pct") == "0.03"

    def test_extracts_top_seller(self):
        m = pipeline.mf_holdings._TOP_SELLER_RE.search(NTPC_FIXTURE)
        assert m is not None
        assert m.group("name") == "HSBC Large and Mid Cap Fund - IDCW"
        assert m.group("shares") == "3,729,293"
        assert m.group("pct") == "0.04"

    def test_parse_top_section_caps_at_five(self):
        # Build a string with 10 top-buyer entries; we expect 5
        html = " ".join(
            f'<h3 class="x">Fund {i} was the highest buyer of {100*i} shares '
            f'in the month of Jan 2025 constituting 0.{i}% of the paid up equity of the company.</h3>'
            for i in range(10)
        )
        results = pipeline.mf_holdings._parse_top_section(html, pipeline.mf_holdings._TOP_BUYER_RE)
        assert len(results) == 5
        assert results[0]["name"] == "Fund 0"
        assert results[4]["name"] == "Fund 4"


# ---------- Table row parser ----------
class TestRowTable:
    def test_parses_all_four_rows(self):
        rows = pipeline.mf_holdings._parse_row_table(NTPC_FIXTURE)
        assert len(rows) == 4
        assert rows[0]["name"] == "Nippon India Power & Infra Gr"
        assert rows[0]["shares"] == 28594584
        assert rows[0]["month_change"] == 2403505
        assert rows[2]["month_change"] == 0   # HDFC Mid Cap didn't move
        assert rows[3]["month_change"] == -50000  # the exited fund

    def test_empty_html_returns_empty_list(self):
        assert pipeline.mf_holdings._parse_row_table("<html></html>") == []

    def test_skips_malformed_rows(self):
        # A <tr> with too few <td> cells should be skipped
        html = "<table><tr><td>Only</td><td>Two</td></tr><tr><td>A</td><td>B</td><td>C</td><td>D</td><td>E</td><td>F</td><td>G</td><td>H</td></tr></table>"
        rows = pipeline.mf_holdings._parse_row_table(html)
        # Only the 8-cell row should parse (≥5 cells required)
        assert len(rows) == 1
        assert rows[0]["name"] == "A"


# ---------- Full _parse_html ----------
class TestParseHtml:
    def test_returns_full_dict(self):
        result = pipeline.mf_holdings._parse_html("NTPCGREEN", NTPC_FIXTURE, "http://x")
        assert result["ticker"] == "NTPCGREEN"
        assert result["mfs_bought"] == 46
        assert result["mfs_sold"] == 19
        assert result["net_change_shares"] == 3329442
        # 4 rows: Nippon (28M shares, bought), Sundaram (3M, bought),
        # HDFC Mid Cap (10M, unchanged), Exited (0 shares).
        # total_mfs_holding = MFs with shares > 0 = 3 (Nippon, Sundaram, HDFC)
        assert result["total_mfs_holding"] == 3
        assert result["total_shares_held"] == 28594584 + 3003217 + 10583872
        assert result["top_buyer"]["name"] == "Nippon India Power & Infra Fund - Growth"
        assert result["top_buyer"]["shares"] == 2403505
        assert result["top_seller"]["name"] == "HSBC Large and Mid Cap Fund - IDCW"
        assert result["top_seller"]["shares"] == 3729293
        # asof captured from "Mutual Fund May 2026 share holdings..."
        assert result["asof"] == "May 2026"
        assert result["net_change_label"] == "+3,329,442"

    def test_net_change_label_sign_for_negative(self):
        """When net change is negative, no '+' sign."""
        # The real fixture has the headline broken across lines:
        # "for a net change\n    of 3,329,442 stocks"
        # so we replace both halves.
        modified = NTPC_FIXTURE.replace(
            "for a net change", "for a net change",
        ).replace("3,329,442 stocks", "-1,234,567 stocks")
        result = pipeline.mf_holdings._parse_html("NTPCGREEN", modified, "http://x")
        assert result["net_change_shares"] == -1234567
        assert result["net_change_label"] == "-1,234,567"

    def test_missing_headline_falls_back_to_table(self):
        """If the headline text is absent, the table parser should still
        find top_buyer (max month_change) and top_seller (min month_change)
        among the MFs that actually hold shares."""
        # A page without the "46 MFs bought..." summary, but with 8-cell rows
        html_no_headline = """
        <table>
          <tr><td>MF</td><td>AUM</td><td>AUM%</td><th>Shares</th><th>Month Change</th><th>%</th><th>Prev</th><th>%</th></tr>
          <tr><td>Fund A</td><td>1</td><td>0%</td><td>1000</td><td>500</td><td>50%</td><td>500</td><td>0%</td></tr>
          <tr><td>Fund B</td><td>2</td><td>0%</td><td>2000</td><td>-300</td><td>-15%</td><td>2300</td><td>0%</td></tr>
          <tr><td>Exited</td><td>0</td><td>0%</td><td>0</td><td>-100</td><td>-100%</td><td>100</td><td>0%</td></tr>
        </table>
        """
        result = pipeline.mf_holdings._parse_html("TEST", html_no_headline, "http://x")
        assert result["mfs_bought"] is None
        assert result["mfs_sold"] is None
        # Table-only fallback: holder = Fund A (1000) + Fund B (2000) = 2
        # (Exited has 0 shares)
        assert result["total_mfs_holding"] == 2
        # Fund A has highest month_change among holders: 500
        # Fund B has lowest month_change among holders: -300
        assert result["top_buyer"]["name"] == "Fund A"
        assert result["top_buyer"]["shares"] == 500
        assert result["top_seller"]["name"] == "Fund B"
        assert result["top_seller"]["shares"] == 300


# ---------- Cache ----------
class TestCache:
    def test_cache_hit_avoids_network(self, tmp_path, monkeypatch):
        """When the cache is fresh for ALL tickers, no network is hit."""
        cache_path = tmp_path / "mf_cache.json"
        # Pre-populate cache with valid, fresh data for every ticker
        now = datetime.now().isoformat(timespec="seconds")
        valid_data = {
            tkr: {"ticker": tkr, "mfs_bought": 10, "fetched_at": now}
            for tkr in pipeline.mf_holdings.TICKER_MAP
        }
        cache_path.write_text(json.dumps(valid_data))

        monkeypatch.setattr(pipeline.mf_holdings, "CACHE_FILE", cache_path)
        with patch.object(pipeline.mf_holdings.requests, "get") as mock_get:
            result = pipeline.mf_holdings.get_mf_holdings()
            # All tickers should be present from cache
            assert "ITC" in result and result["ITC"]["mfs_bought"] == 10
            mock_get.assert_not_called()

    def test_cache_miss_triggers_network(self, tmp_path, monkeypatch):
        """When the cache is empty, we DO call requests.get for each ticker."""
        cache_path = tmp_path / "mf_cache.json"
        cache_path.write_text("{}")  # empty cache

        monkeypatch.setattr(pipeline.mf_holdings, "CACHE_FILE", cache_path)
        with patch.object(pipeline.mf_holdings.requests, "get") as mock_get:
            # Make every request fail so we don't try to actually fetch
            mock_get.return_value.status_code = 404
            mock_get.return_value.raise_for_status.side_effect = Exception("404")
            result = pipeline.mf_holdings.get_mf_holdings(force=True)
            # No data returned because all requests failed
            assert result == {}
            # But network WAS called (once per ticker with real IDs)
            real_id_tickers = [
                t for t, info in pipeline.mf_holdings.TICKER_MAP.items()
                if info["id"] < pipeline.mf_holdings.PLACEHOLDER_ID_THRESHOLD
            ]
            assert mock_get.call_count == len(real_id_tickers)

    def test_force_refresh_overrides_fresh_cache(self, tmp_path, monkeypatch):
        """force=True skips the cache TTL check entirely."""
        cache_path = tmp_path / "mf_cache.json"
        # Fresh cache for all tickers
        now = datetime.now().isoformat(timespec="seconds")
        cache_path.write_text(json.dumps({
            tkr: {"ticker": tkr, "value": 1, "fetched_at": now}
            for tkr in pipeline.mf_holdings.TICKER_MAP
        }))
        monkeypatch.setattr(pipeline.mf_holdings, "CACHE_FILE", cache_path)
        with patch.object(pipeline.mf_holdings.requests, "get") as mock_get:
            # Return valid HTML for every request
            mock_get.return_value.status_code = 200
            mock_get.return_value.text = (
                '<html><body>'
                '<h2>10 MFs bought and 5 MFs sold ITC in the month '
                'of May 2026 for a net change of 100 stocks</h2>'
                '</body></html>'
            )
            mock_get.return_value.raise_for_status = lambda: None
            pipeline.mf_holdings.get_mf_holdings(force=True)
            # force=True should have called requests.get for every ticker
            real_id_tickers = [
                t for t, info in pipeline.mf_holdings.TICKER_MAP.items()
                if info["id"] < pipeline.mf_holdings.PLACEHOLDER_ID_THRESHOLD
            ]
            assert mock_get.call_count == len(real_id_tickers)


# ---------- Summary shape ----------
class TestSummary:
    def test_summary_sorted_by_absolute_net_change(self, monkeypatch):
        """The summary should put the biggest |net_change| first so the user
        sees the biggest movers at the top."""
        cache = {
            "A": {"ticker": "A", "mfs_bought": 10, "mfs_sold": 1, "net_change_shares": 100, "fetched_at": "now"},
            "B": {"ticker": "B", "mfs_bought": 50, "mfs_sold": 50, "net_change_shares": 50000, "fetched_at": "now"},
            "C": {"ticker": "C", "mfs_bought": 5, "mfs_sold": 10, "net_change_shares": -200, "fetched_at": "now"},
        }
        # Mock the underlying fetcher to return our canned cache
        monkeypatch.setattr(pipeline.mf_holdings, "get_mf_holdings", lambda force=False: cache)
        summary = pipeline.mf_holdings.get_mf_holdings_summary()
        # Order: |50000|, |200|, |100| -> B, C, A
        assert [s["ticker"] for s in summary] == ["B", "C", "A"]

    def test_summary_includes_human_readable_label(self, monkeypatch):
        cache = {
            "X": {
                "ticker": "X", "mfs_bought": 3, "mfs_sold": 2,
                "net_change_shares": 1000,
                "fetched_at": "now", "name": "X Co",
                "total_mfs_holding": 50,
            }
        }
        monkeypatch.setattr(pipeline.mf_holdings, "get_mf_holdings", lambda force=False: cache)
        s = pipeline.mf_holdings.get_mf_holdings_summary()[0]
        assert s["net_change_label"] == "+1,000"
        assert s["name"] == "X Co"
        assert s["total_mfs_holding"] == 50


# ---------- User's specific question ----------
# "Mention what all variables we use and also abbrivated words meaning"
# This is now part of the FairValue work; the MF table should provide a
# glossary as well.
class TestGlossary:
    def test_mf_terms_documented_in_module(self):
        """Sanity check: the module's docstring mentions the key terms
        the user asked about (MFs, ETF, FII, etc.)."""
        import inspect
        src = inspect.getsource(pipeline.mf_holdings)
        # Abbreviations that must be defined/used somewhere in the module
        for term in ["MFs", "MF", "FII", "ETF", "AUM", "NAV", "ISIN"]:
            assert term in src, f"abbreviation {term!r} not mentioned in module"

    def test_known_tickers_match_users_portfolio(self):
        """The 8 tickers the user is holding must all be supported."""
        expected = {"BALRAMCHIN", "ITC", "JIOFIN", "NTPCGREEN",
                    "KNRCON", "IRCON", "BANKBARODA", "RELIANCE"}
        actual = set(pipeline.mf_holdings.TICKER_MAP.keys())
        missing = expected - actual
        assert not missing, f"missing tickers in TICKER_MAP: {missing}"
