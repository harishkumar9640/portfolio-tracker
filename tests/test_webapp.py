"""
Tests for the FastAPI web dashboard.

We use FastAPI's TestClient (which doesn't need a real HTTP server).
All network calls are mocked so the tests run offline.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))


# ---------- Fixtures ----------
@pytest.fixture
def client():
    """A TestClient for the FastAPI app. Mocked data layer underneath."""
    # Force the data cache to start empty so each test gets fresh data.
    import webapp.data as wd
    wd._portfolio_cache = {"asof": None, "data": None, "ts": 0.0}
    wd._fairvalue_cache = {"asof": None, "data": None, "ts": 0.0}
    # Build a fake snapshot dict
    fake_portfolio = {
        "asof": "2026-06-25",
        "indices": [
            {"name": "Nifty 50 (IN)", "pct": 0.50},
            {"name": "S&P 500 (US)",  "pct": -1.44},
            {"name": "Hang Seng (HK)", "pct": 0.04},
        ],
        "equity": {
            "row": {"name": "My Equity", "pct": -0.26, "value": 495969.0,
                    "prev_value": 497262.0, "pnl_today": -1293.0},
            "holdings": [
                {"symbol": "RELIANCE-EQ", "quantity": 60, "avg_price": 1250.52,
                 "ltp": 1304.40, "current_value": 78264.0, "pnl": 3232.8,
                 "pnl_pct": 4.31},
            ],
            "value": 495969.0, "prev_value": 497262.0,
        },
        "mf": {"count": 5, "value": 350273.0, "prev_value": 353201.0, "pct": -0.83},
        "sgb": {
            "count": 2, "value": 30313.0, "prev_value": 30710.0, "pct": -1.29,
            "rows": [
                {"name": "SGB 2022-23 IV", "units": 1, "price_per_g": 14950,
                 "value": 14950, "pct": -1.19, "source": "NSE (SGBFEB32IV)"},
            ],
        },
        "total": {"value": 876555.0, "prev_value": 881173.0, "pct": -0.52},
        "best_index": {"name": "Nifty 50 (IN)", "pct": 0.50},
        "worst_index": {"name": "S&P 500 (US)", "pct": -1.44},
    }
    fake_fairvalue = {
        "asof": "2026-06-25",
        "rows": [
            {"ticker": "RELIANCE", "price": 1327.0, "eps": 14.26,
             "book_value": 668.0, "fcf_per_share": 141.97,
             "graham": 462.96, "graham_margin_pct": -65.13,
             "dcf": 2798.82, "dcf_margin_pct": 110.91},
            {"ticker": "TCS", "price": 2199.0, "eps": 31.13,
             "book_value": 296.0, "fcf_per_share": 144.0,
             "graham": 455.33, "graham_margin_pct": -79.29,
             "dcf": 2838.89, "dcf_margin_pct": 29.10},
        ],
    }
    # webapp.server does `from webapp.data import get_*` so it holds its
    # own module-level references. Patch both the source (webapp.data)
    # and the consumer (webapp.server) so the real network functions
    # are never called during tests.
    import webapp.server as ws

    wd.get_portfolio_snapshot = lambda force=False: fake_portfolio
    ws.get_portfolio_snapshot = wd.get_portfolio_snapshot

    wd.get_fairvalue_snapshot = lambda force=False: fake_fairvalue
    ws.get_fairvalue_snapshot = wd.get_fairvalue_snapshot

    def fake_health():
        return {
            "now": "2026-06-25T11:00:00",
            "last_portfolio_run": {"ran_at": "2026-06-25T10:00:00",
                                   "status": "ok", "note": "asof=2026-06-25"},
            "last_fairvalue_run": {},
            "snapshots_in_db": 5,
            "sgb_price_rows": 100,
        }
    wd.get_health = fake_health
    ws.get_health = fake_health

    wd.get_holdings_summary = lambda: {
        "mfs": [{"name": "HDFC Mid Cap Fund", "units": 500.58}],
        "sgbs": [{"isin": "IN0020230184", "name": "SGB 2022-23 IV", "units": 1}],
        "tickers": ["RELIANCE", "TCS", "INFY"],
    }
    ws.get_holdings_summary = wd.get_holdings_summary
    ws.start_background_refresh = lambda kind="all": None

    from fastapi.testclient import TestClient
    from webapp.server import app
    return TestClient(app)


# ---------- Page routes ----------
class TestRoutes:
    def test_root_redirects(self, client):
        r = client.get("/", follow_redirects=False)
        assert r.status_code == 307
        assert r.headers["location"] == "/portfolio"

    def test_portfolio_page(self, client):
        r = client.get("/portfolio")
        assert r.status_code == 200
        assert "Today's Portfolio" in r.text
        assert "Total Portfolio" in r.text
        assert "Nifty 50 (IN)" in r.text
        assert "S&amp;P 500 (US)" in r.text or "S&P 500 (US)" in r.text

    def test_page_loading_stopwatch_present(self, client):
        """Every page must include the page-loading stopwatch element
        so users see feedback during slow server responses."""
        for path in ["/portfolio", "/fairvalue", "/history", "/settings"]:
            r = client.get(path)
            assert r.status_code == 200
            assert "page-loading" in r.text, \
                f"{path} missing #page-loading stopwatch"
            assert "pageLoadingClock" in r.text, \
                f"{path} missing the stopwatch clock element"
            assert "page-loading-spinner" in r.text, \
                f"{path} missing the spinner element"
            assert "Fetching today's data" in r.text, \
                f"{path} missing the loading hint text"

    def test_equity_bar_shows_pnl_today(self, client):
        """The My Equity bar must show today's gain/loss in ₹ as well
        as the percentage change, so the user can see the actual money
        moved today without doing the math. The ₹ value is rendered
        *inside* the bar fill itself (white text on the bar colour).
        """
        r = client.get("/portfolio")
        assert r.status_code == 200
        # The in-bar label element must be present.
        assert "bar-fill-label" in r.text, \
            "bar-fill-label element should be present inside the bar"
        # The formatted ₹ value must be in the bar fill label.
        # (Template uses no parentheses around the number for in-bar
        # rendering — it's styled as part of the bar's visual identity.)
        assert "-1,293" in r.text, \
            "the bar should contain the formatted ₹ P&L"

    def test_api_portfolio_returns_pnl_today(self, client):
        """/api/portfolio JSON should include pnl_today for the equity row."""
        r = client.get("/api/portfolio")
        assert r.status_code == 200
        data = r.json()
        assert "equity" in data
        row = data["equity"].get("row") or {}
        assert "pnl_today" in row, "equity.row should include pnl_today"
        assert isinstance(row["pnl_today"], (int, float))
        # Sanity: pnl_today must be derivable from value − prev_value
        assert abs(row["pnl_today"] - (row["value"] - row["prev_value"])) < 0.01

    def test_fairvalue_page(self, client):
        r = client.get("/fairvalue")
        assert r.status_code == 200
        assert "Fair Value" in r.text
        assert "RELIANCE" in r.text
        assert "Graham" in r.text

    def test_history_page(self, client):
        r = client.get("/history")
        assert r.status_code == 200
        assert "Portfolio History" in r.text

    def test_settings_page(self, client):
        r = client.get("/settings")
        assert r.status_code == 200
        assert "Settings" in r.text
        assert "HDFC Mid Cap Fund" in r.text
        assert "IN0020230184" in r.text

    def test_static_css_served(self, client):
        r = client.get("/static/css/app.css")
        assert r.status_code == 200
        # Should be substantial CSS
        assert len(r.text) > 5000
        # Should contain the responsive grid breakpoints
        assert "@media" in r.text
        assert "grid-template-columns" in r.text

    def test_static_js_served(self, client):
        r = client.get("/static/js/app.js")
        assert r.status_code == 200
        assert "navToggle" in r.text


# ---------- API endpoints ----------
class TestAPI:
    def test_api_health(self, client):
        r = client.get("/api/health")
        assert r.status_code == 200
        data = r.json()
        assert "now" in data
        assert "last_portfolio_run" in data
        assert "snapshots_in_db" in data

    def test_api_portfolio(self, client):
        r = client.get("/api/portfolio")
        assert r.status_code == 200
        data = r.json()
        assert data["asof"] == "2026-06-25"
        assert "indices" in data
        assert "equity" in data
        assert "total" in data
        assert isinstance(data["total"]["value"], (int, float))

    def test_api_fairvalue(self, client):
        r = client.get("/api/fairvalue")
        assert r.status_code == 200
        data = r.json()
        assert "rows" in data
        assert len(data["rows"]) == 2
        assert data["rows"][0]["ticker"] == "RELIANCE"

    def test_api_refresh_get(self, client):
        r = client.get("/api/refresh")
        assert r.status_code == 202
        assert r.json()["status"] == "queued"

    def test_api_refresh_post(self, client):
        r = client.post("/api/refresh?kind=portfolio")
        assert r.status_code == 202
        assert "portfolio" in r.json()["kinds"]

    def test_api_refresh_kind_fairvalue(self, client):
        r = client.post("/api/refresh?kind=fairvalue")
        assert r.status_code == 202
        assert r.json()["kinds"] == ["fairvalue"]

    def test_flows_page_renders(self, client):
        r = client.get("/flows")
        assert r.status_code == 200
        assert "FII" in r.text
        assert "DII" in r.text
        # nav link should be present and active
        assert 'href="/flows"' in r.text
        assert "is-active" in r.text

    def test_api_flows_returns_valid_json(self, client):
        r = client.get("/api/flows")
        assert r.status_code == 200
        data = r.json()
        assert "today_fii" in data
        assert "today_dii" in data
        assert "chart" in data
        assert "portfolio_deals" in data
        assert "recent_deals" in data
        assert "asof" in data

    def test_flows_nav_link_present_on_all_pages(self, client):
        """Every page should show the 'Flows' link in the nav."""
        for path in ["/portfolio", "/fairvalue", "/flows"]:
            r = client.get(path)
            assert 'href="/flows"' in r.text, (
                f"missing Flows nav link on {path}"
            )


# ---------- Responsive design verification ----------
class TestResponsiveOutput:
    """Verify the rendered HTML contains the hooks for responsive layout."""

    def test_viewport_meta_tag(self, client):
        r = client.get("/portfolio")
        assert 'name="viewport"' in r.text
        assert "width=device-width" in r.text

    def test_skip_link_present(self, client):
        r = client.get("/portfolio")
        assert 'class="skip-link"' in r.text
        assert 'href="#main"' in r.text

    def test_hamburger_toggle_in_html(self, client):
        r = client.get("/portfolio")
        assert 'id="navToggle"' in r.text
        assert 'aria-controls="primaryNav"' in r.text
        assert 'aria-expanded="false"' in r.text

    def test_theme_color_meta(self, client):
        r = client.get("/portfolio")
        assert 'name="theme-color"' in r.text

    def test_lang_attribute(self, client):
        r = client.get("/portfolio")
        assert '<html lang="en">' in r.text

    def test_table_wrapper_for_horizontal_scroll(self, client):
        r = client.get("/portfolio")
        assert 'class="table-wrap"' in r.text


# ---------- Visual smoke test via curl + grep ----------
class TestResponsiveCSS:
    """Verify the CSS file has all the right responsive hooks."""

    @pytest.fixture
    def css(self):
        return (PROJECT / "webapp" / "static" / "css" / "app.css").read_text()

    def test_has_dark_mode_media_query(self, css):
        assert "prefers-color-scheme: dark" in css

    def test_has_mobile_breakpoint(self, css):
        assert "max-width: 720px" in css

    def test_has_tablet_breakpoint(self, css):
        assert "min-width: 600px" in css
        assert "min-width: 720px" in css

    def test_has_desktop_breakpoint(self, css):
        assert "min-width: 1100px" in css

    def test_uses_grid_and_flex(self, css):
        assert "display: grid" in css
        assert "display: flex" in css

    def test_uses_css_variables(self, css):
        assert ":root {" in css
        assert "--c-bg:" in css
        assert "--s-4:" in css

    def test_has_accessible_focus_state(self, css):
        assert ".skip-link:focus" in css

    def test_uses_aria_friendly_responsive_nav(self, css):
        # nav-toggle shows on mobile only
        assert ".nav-toggle" in css
        # The .primary-nav position changes at the mobile breakpoint
        assert ".primary-nav" in css
        # CSS uses ARIA selectors for the hamburger open/close animation
        assert '[aria-expanded="true"]' in css
        assert ".is-open" in css