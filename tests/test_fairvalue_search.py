"""
Tests for the fair-value lookup and stock autocomplete.

We mock the network so tests run offline. The NSE equity-list parser
is exercised against a fixture CSV that mimics NSE's leading-space
column-name quirk.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))


# Fixture CSV with the same leading-space quirk as NSE's real file.
NSE_CSV = (
    "SYMBOL,NAME OF COMPANY, SERIES, DATE OF LISTING, PAID UP VALUE, MARKET LOT, ISIN NUMBER, FACE VALUE\n"
    "RELIANCE,Reliance Industries Limited,EQ,29-NOV-1995,10,1,INE002A01018,10\n"
    "TCS,Tata Consultancy Services Limited,EQ,25-AUG-2004,1,1,INE467B01029,1\n"
    "INFY,Infosys Limited,EQ,08-FEB-1999,5,1,INE009A01021,5\n"
    "RCOM,Reliance Communications Limited,EQ,06-NOV-2006,5,1,INE330H01018,5\n"
    "RELIABLE,Reliable Data Services Limited,EQ,12-OCT-2022,10,1,INE234F01028,10\n"
    "WIPRO,Wipro Limited,EQ,23-OCT-2000,2,1,INE075A01022,2\n"
    "20MICRONS,20 Microns Limited,EQ,06-OCT-2008,5,1,INE144J01027,5\n"
)


@pytest.fixture
def tmp_nse_csv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Write a fixture CSV and re-point fair_value.search at it."""
    csv_path = tmp_path / "nse_equity_list.csv"
    csv_path.write_text(NSE_CSV, encoding="utf-8")

    import fair_value.search as search_mod
    # Override the cache path and reset module-level state
    monkeypatch.setattr(search_mod, "CACHE_FILE", csv_path)
    search_mod._index = []
    search_mod._index_loaded_at = 0.0
    return search_mod


# ---------- NSE CSV parser ----------
class TestParseCsv:
    def test_strips_leading_space_from_columns(self, tmp_nse_csv):
        """The NSE CSV has ' ISIN NUMBER' not 'ISIN NUMBER'."""
        rows = list(tmp_nse_csv._parse_csv(NSE_CSV))
        assert len(rows) == 7
        for r in rows:
            assert r["isin"], f"empty ISIN for {r['symbol']}"
        assert rows[0]["symbol"] == "RELIANCE"
        assert rows[0]["isin"] == "INE002A01018"
        assert rows[0]["_sym"] == "reliance"
        assert rows[0]["_name"] == "reliance industries limited"

    def test_skips_rows_with_no_symbol(self, tmp_nse_csv):
        bad = "SYMBOL,NAME OF COMPANY\n,foo\nRELIANCE,Reliance Industries Limited\n"
        rows = list(tmp_nse_csv._parse_csv(bad))
        assert len(rows) == 1
        assert rows[0]["symbol"] == "RELIANCE"


# ---------- search_schemes ----------
class TestSearchSchemes:
    def test_empty_query_returns_alphabetical(self, tmp_nse_csv):
        results = tmp_nse_csv.search_schemes("", limit=5)
        # First entry is 20MICRONS alphabetically
        assert results[0]["symbol"] == "20MICRONS"
        assert len(results) == 5

    def test_exact_symbol_match(self, tmp_nse_csv):
        results = tmp_nse_csv.search_schemes("RELIANCE", limit=5)
        assert results[0]["symbol"] == "RELIANCE"
        assert results[0]["name"] == "Reliance Industries Limited"

    def test_case_insensitive_symbol_match(self, tmp_nse_csv):
        results = tmp_nse_csv.search_schemes("tcs", limit=5)
        assert results[0]["symbol"] == "TCS"

    def test_symbol_prefix(self, tmp_nse_csv):
        results = tmp_nse_csv.search_schemes("REL", limit=10)
        symbols = [r["symbol"] for r in results]
        # Symbol-prefix matches come first
        assert "RELIANCE" in symbols
        assert "RELIABLE" in symbols
        # Best (lowest-score) match must be a symbol-prefix entry
        assert results[0]["symbol"].startswith("REL"), \
            f"expected symbol-prefix first, got {results[0]['symbol']}"
        # Symbol-prefix matches must rank ahead of name-substring matches.
        prefix_syms = [r["symbol"] for r in results
                       if r["symbol"].startswith("REL")]
        assert prefix_syms[0] in ("RELIANCE", "RELIABLE")
        # RCOM (no symbol prefix on "REL") may still appear at lower
        # priority via name-substring matching, but never above RELIANCE.
        if "RCOM" in symbols:
            assert symbols.index("RCOM") > symbols.index("RELIANCE")

    def test_name_substring(self, tmp_nse_csv):
        results = tmp_nse_csv.search_schemes("Reliance Industries", limit=10)
        symbols = [r["symbol"] for r in results]
        assert "RELIANCE" in symbols
        # RELIANCE should rank above RCOM (both contain "reliance industries"
        # but only RELIANCE's name == "Reliance Industries Limited")
        assert results[0]["symbol"] == "RELIANCE"

    def test_normalised_name_strips_ltd(self, tmp_nse_csv):
        # "reliance industries ltd" should still find RELIANCE
        results = tmp_nse_csv.search_schemes("Reliance Industries Ltd", limit=5)
        assert results[0]["symbol"] == "RELIANCE"

    def test_isin_exact_match(self, tmp_nse_csv):
        results = tmp_nse_csv.search_schemes("INE009A01021", limit=5)
        assert len(results) == 1
        assert results[0]["symbol"] == "INFY"
        assert results[0]["isin"] == "INE009A01021"

    def test_isin_lowercase_works(self, tmp_nse_csv):
        results = tmp_nse_csv.search_schemes("ine009a01021", limit=5)
        assert results[0]["symbol"] == "INFY"

    def test_limit_respected(self, tmp_nse_csv):
        results = tmp_nse_csv.search_schemes("reliance", limit=2)
        assert len(results) == 2

    def test_relevance_ordering_prefers_exact(self, tmp_nse_csv):
        # "tcs" exact match should come before any name-substring match
        results = tmp_nse_csv.search_schemes("tcs", limit=5)
        assert results[0]["symbol"] == "TCS"

    def test_no_match_returns_empty(self, tmp_nse_csv):
        results = tmp_nse_csv.search_schemes("xyzzy_no_such_company", limit=5)
        assert results == []


# ---------- resolve_ticker ----------
class TestResolveTicker:
    def test_resolves_company_name(self, tmp_nse_csv):
        sym, name = tmp_nse_csv.resolve_ticker("Infosys Limited")
        assert sym == "INFY"
        assert "Infosys" in name

    def test_resolves_short_name(self, tmp_nse_csv):
        sym, _ = tmp_nse_csv.resolve_ticker("Reliance Industries")
        assert sym == "RELIANCE"

    def test_resolves_isin(self, tmp_nse_csv):
        sym, name = tmp_nse_csv.resolve_ticker("INE009A01021")
        assert sym == "INFY"
        assert name == "Infosys Limited"

    def test_resolves_ticker_passthrough(self, tmp_nse_csv):
        sym, name = tmp_nse_csv.resolve_ticker("TCS")
        assert sym == "TCS"
        assert "Tata Consultancy" in name

    def test_falls_back_to_uppercased_input(self, tmp_nse_csv):
        # Unknown input → pass through upper-cased (screener.in will handle)
        sym, name = tmp_nse_csv.resolve_ticker("nonsense-input")
        assert sym == "NONSENSE-INPUT"
        assert name == "nonsense-input"


# ---------- _normalise ----------
class TestNormalise:
    @pytest.mark.parametrize("raw,expected", [
        ("Reliance Industries",       "reliance"),
        ("Reliance Industries Ltd",   "reliance"),
        ("Reliance Industries Ltd.",  "reliance"),
        ("Infosys Limited",           "infosys"),
        ("WIPRO AND CO",              "wipro co"),
        ("  spaces   ",               "spaces"),
    ])
    def test_strips_common_suffixes(self, raw, expected):
        from fair_value.search import _normalise
        assert _normalise(raw) == expected


# ---------- _score ----------
class TestScore:
    """Lower score = better. Score is a tuple (primary, lcp, name_len).

    primary: coarse relevance bucket
    lcp:     longest common prefix with the original (un-normalised) query
    name_len: length of the entry name (tie-breaker; shorter = better)
    """

    def setup_method(self):
        from fair_value.search import _score
        self.score = _score
        self.reliance = {
            "symbol": "RELIANCE", "_sym": "reliance",
            "name": "Reliance Industries Limited", "_name": "reliance industries limited",
            "isin": "INE002A01018",
        }
        self.rcom = {
            "symbol": "RCOM", "_sym": "rcom",
            "name": "Reliance Communications Limited", "_name": "reliance communications limited",
            "isin": "INE330H01018",
        }

    def test_exact_symbol_is_best(self):
        # Primary 0 (best bucket)
        assert self.score(self.reliance, "reliance", "reliance")[0] == 0

    def test_symbol_prefix_beats_name_match(self):
        # "rel" prefix on RELIANCE symbol: primary 2 (symbol prefix)
        assert self.score(self.reliance, "rel", "rel")[0] == 2

    def test_word_boundary_beats_substring(self):
        # "Reliance Industries" matches RELIANCE best (longest LCP).
        # Both have primary=3 (name starts with q_lower) because the
        # user query starts with "Reliance Industries".
        s_rel = self.score(self.reliance, "reliance industries", "reliance")
        s_rcom = self.score(self.rcom,      "reliance industries", "reliance")
        # RELIANCE wins by LCP: "reliance industries " is longer than
        # "reliance communications " before they diverge at position 9.
        assert s_rel < s_rcom, f"RELIANCE {s_rel} should beat RCOM {s_rcom}"
        # Specifically, RELIANCE's LCP includes "reliance industries" (19 chars)
        assert s_rel[1] >= 19

    def test_shorter_name_wins_on_full_tie(self):
        # Same primary + LCP -> shorter name wins (specificity).
        # Note: _score expects q_lower (lowercased) and q_norm (normalised).
        from fair_value.search import _score
        a = {"symbol": "AAA", "_sym": "aaa",
             "name": "Reliance Industries", "_name": "reliance industries"}
        b = {"symbol": "ZZZ", "_sym": "zzz",
             "name": "Reliance Industries Ltd Extra", "_name": "reliance industries ltd extra"}
        # Exact-name match on the smaller one (Reliance Industries)
        sa = _score(a, "reliance industries", "reliance industries")
        # Substring match on the larger one
        sb = _score(b, "reliance industries", "reliance industries")
        # sa must be strictly better (smaller tuple)
        assert sa < sb, f"shorter-name score {sa} should beat longer-name {sb}"
        # And name_len should differ
        assert sa[2] < sb[2]


# ---------- Network behaviour ----------
class TestNetwork:
    def test_uses_stale_cache_on_download_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ):
        """If the CSV download fails but the cached file is present (even
        stale), we should still serve from cache."""
        import fair_value.search as search_mod
        # Write a stale cache with valid content
        cache = tmp_path / "stale.csv"
        cache.write_text(NSE_CSV, encoding="utf-8")
        # Make it look old
        import os, time
        old_time = time.time() - (search_mod.CACHE_TTL_SECONDS + 3600)
        os.utime(cache, (old_time, old_time))

        monkeypatch.setattr(search_mod, "CACHE_FILE", cache)
        search_mod._index = []
        search_mod._index_loaded_at = 0.0

        # Mock requests.get to fail
        def boom(*a, **kw):
            raise search_mod.requests.RequestException("network down")
        monkeypatch.setattr(search_mod.requests, "get", boom)

        idx = search_mod._load_index(force=False)
        assert len(idx) == 7  # served from stale cache
        assert idx[0]["symbol"] == "RELIANCE"

    def test_downloads_when_no_cache(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ):
        import fair_value.search as search_mod
        cache = tmp_path / "fresh.csv"  # doesn't exist yet
        monkeypatch.setattr(search_mod, "CACHE_FILE", cache)
        search_mod._index = []
        search_mod._index_loaded_at = 0.0

        # Mock requests.get to return our fixture CSV
        mock_resp = MagicMock()
        mock_resp.text = NSE_CSV
        mock_resp.raise_for_status = MagicMock()
        monkeypatch.setattr(search_mod.requests, "get", lambda *a, **kw: mock_resp)

        idx = search_mod._load_index(force=False)
        assert len(idx) == 7
        assert cache.exists()


# ---------- HTTP endpoint via FastAPI TestClient ----------
class TestSearchEndpoint:
    @pytest.fixture
    def client(self):
        """TestClient with a stubbed NSE CSV."""
        import tempfile
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".csv", delete=False, encoding="utf-8"
        ) as f:
            f.write(NSE_CSV)
            csv_path = Path(f.name)
        try:
            import fair_value.search as search_mod
            search_mod.CACHE_FILE = csv_path
            search_mod._index = []
            search_mod._index_loaded_at = 0.0

            # Mock the screener.in fetcher. Both module-level references
            # must be patched: valuation imports ``fetch`` from
            # fair_value.fetcher at module load time.
            import fair_value.fetcher
            import fair_value.valuation as fv_val
            fake_data = {
                "RELIANCE": {"ticker": "RELIANCE", "current_price": 1327.0,
                             "eps": 14.26, "book_value": 668.0,
                             "market_cap": 1700000.0,
                             "operating_cash_flow_per_share": 141.97,
                             "source_url": "", "fetched_at": ""},
                "TCS":      {"ticker": "TCS", "current_price": 2199.0,
                             "eps": 31.13, "book_value": 296.0,
                             "market_cap": 800000.0,
                             "operating_cash_flow_per_share": 144.0,
                             "source_url": "", "fetched_at": ""},
                "INFY":     {"ticker": "INFY", "current_price": 1054.0,
                             "eps": 32.0, "book_value": 220.0,
                             "market_cap": 440000.0,
                             "operating_cash_flow_per_share": 70.0,
                             "source_url": "", "fetched_at": ""},
            }
            fake_fetch = lambda t: fake_data.get(
                t, {"ticker": t, "error": "not in mock"}
            )
            fair_value.fetcher.fetch = fake_fetch
            fv_val.fetch = fake_fetch

            from fastapi.testclient import TestClient
            from webapp.server import app
            yield TestClient(app)
        finally:
            csv_path.unlink(missing_ok=True)

    def test_search_endpoint_returns_results(self, client):
        r = client.get("/api/fairvalue/search?q=REL&limit=10")
        assert r.status_code == 200
        data = r.json()
        assert "query" in data
        assert "results" in data
        symbols = [r["symbol"] for r in data["results"]]
        assert "RELIANCE" in symbols

    def test_search_endpoint_handles_isin(self, client):
        r = client.get("/api/fairvalue/search?q=INE009A01021")
        assert r.status_code == 200
        data = r.json()
        assert data["results"][0]["symbol"] == "INFY"

    def test_search_endpoint_empty_query(self, client):
        r = client.get("/api/fairvalue/search?q=")
        assert r.status_code == 200
        data = r.json()
        # First result alphabetically (lowest symbol)
        assert data["results"][0]["symbol"] == "20MICRONS"

    def test_lookup_endpoint_with_ticker(self, client):
        r = client.post("/api/fairvalue/lookup",
                        json={"ticker": "RELIANCE", "industry_pe": 25})
        assert r.status_code == 200
        data = r.json()
        assert data["resolved_ticker"] == "RELIANCE"
        assert data["price"] == 1327.0
        assert data["graham"] == pytest.approx(462.96, rel=0.01)
        assert data["pe_relative"] == pytest.approx(14.26 * 25, rel=0.01)
        assert "graham_margin_pct" in data
        assert "dcf_margin_pct" in data
        assert data["queried_as"] == "RELIANCE"
        assert data["params"]["industry_pe"] == 25

    def test_lookup_endpoint_with_company_name(self, client):
        r = client.post("/api/fairvalue/lookup",
                        json={"ticker": "Infosys Limited"})
        assert r.status_code == 200
        data = r.json()
        assert data["resolved_ticker"] == "INFY"
        assert data["queried_as"] == "Infosys Limited"

    def test_lookup_endpoint_with_isin(self, client):
        r = client.post("/api/fairvalue/lookup",
                        json={"ticker": "INE002A01018"})
        assert r.status_code == 200
        data = r.json()
        assert data["resolved_ticker"] == "RELIANCE"

    def test_lookup_endpoint_with_unknown_returns_error(self, client):
        r = client.post("/api/fairvalue/lookup",
                        json={"ticker": "XYZNOSUCHTICKER"})
        assert r.status_code == 200  # we return 200 + error JSON
        data = r.json()
        assert "error" in data or data.get("queried_as") is None
        # Falls through to screener.in which we haven't mocked for this ticker

    def test_lookup_endpoint_missing_ticker(self, client):
        r = client.post("/api/fairvalue/lookup", json={"ticker": ""})
        assert r.status_code == 200
        assert r.json()["error"] == "ticker is required"

    def test_lookup_endpoint_custom_dcf_params(self, client):
        r = client.post("/api/fairvalue/lookup", json={
            "ticker": "TCS",
            "dcf_g1": 0.15, "dcf_g2": 0.04, "dcf_r": 0.12,
        })
        assert r.status_code == 200
        data = r.json()
        assert data["params"]["dcf_g1"] == 0.15
        assert data["params"]["dcf_g2"] == 0.04
        assert data["params"]["dcf_r"] == 0.12
        assert data["dcf"] > 0


# ---------- UI smoke tests ----------
class TestFairvaluePageMarkup:
    """Verify the template renders the search box + result panel."""

    @pytest.fixture
    def client(self):
        import tempfile
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".csv", delete=False, encoding="utf-8"
        ) as f:
            f.write(NSE_CSV)
            csv_path = Path(f.name)
        try:
            import fair_value.search as search_mod
            search_mod.CACHE_FILE = csv_path
            search_mod._index = []
            search_mod._index_loaded_at = 0.0

            from fastapi.testclient import TestClient
            from webapp.server import app
            yield TestClient(app)
        finally:
            csv_path.unlink(missing_ok=True)

    def test_fairvalue_page_has_search_input(self, client):
        r = client.get("/fairvalue")
        assert r.status_code == 200
        assert 'id="lookupInput"' in r.text
        assert 'id="lookupSuggestions"' in r.text
        assert 'id="lookupResult"' in r.text
        assert 'id="lookupClear"' in r.text

    def test_fairvalue_page_loads_fairvalue_js(self, client):
        r = client.get("/fairvalue")
        assert "/static/js/fairvalue.js" in r.text

    def test_fairvalue_page_has_advanced_params(self, client):
        r = client.get("/fairvalue")
        assert 'id="paramIndustryPe"' in r.text
        assert 'id="paramDcfG1"' in r.text
        assert 'id="paramDcfG2"' in r.text
        assert 'id="paramDcfR"' in r.text

    def test_other_pages_dont_load_fairvalue_js(self, client):
        """fairvalue.js should only load on the fairvalue page."""
        for path in ("/portfolio", "/history", "/settings"):
            r = client.get(path)
            assert "/static/js/fairvalue.js" not in r.text