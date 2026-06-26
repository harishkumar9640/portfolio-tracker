"""
End-to-end browser tests for the MF Holdings Trend section on
/portfolio. Verifies:
  - Section is visible on the portfolio page
  - All 8 tickers are listed
  - Glossary toggle works (collapsed by default, expands on click)
  - Clicking a row expands the detail panel showing top buyer / seller
  - Detail row has the right ticker name and share counts
  - Clicking again collapses the panel
  - Keyboard (Enter) also toggles the row
  - Section is responsive (table-wrap present)
  - API endpoint /api/mf_holdings returns 8 rows sorted by |net_change|
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))


# ---------- Server lifecycle ----------

def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def server_url():
    """Start uvicorn on a free port for this test module."""
    port = _free_port()
    env = os.environ.copy()
    env["PT_LOG_LEVEL"] = "ERROR"
    env["PYTHONPATH"] = str(PROJECT) + (
        os.pathsep + env.get("PYTHONPATH", "") if env.get("PYTHONPATH") else ""
    )
    proc = subprocess.Popen(
        [sys.executable, "-m", "webapp.server", "--host", "127.0.0.1", "--port", str(port)],
        env=env,
        cwd=str(PROJECT),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    base = f"http://127.0.0.1:{port}"
    for _ in range(50):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                break
        except OSError:
            time.sleep(0.2)
    else:
        proc.kill()
        raise RuntimeError(f"server didn't start on port {port}")
    yield base
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


@pytest.fixture(scope="module")
def browser():
    """One Chromium browser shared across all tests in this module."""
    pytest.importorskip("playwright.sync_api")
    from playwright.sync_api import sync_playwright
    try:
        with sync_playwright() as p:
            b = p.chromium.launch(headless=True)
            yield b
            b.close()
    except Exception as e:
        pytest.skip(f"Chromium not available: {e}")


@pytest.fixture
def page(browser, server_url):
    """A fresh page+context per test."""
    context = browser.new_context(viewport={"width": 1280, "height": 900})
    pg = context.new_page()
    yield pg
    context.close()


EXPECTED_TICKERS = [
    "RELIANCE", "BANKBARODA", "JIOFIN", "ITC",
    "NTPCGREEN", "KNRCON", "BALRAMCHIN", "IRCON",
]


# ---------- Section is rendered ----------

class TestMFHoldingsSection:
    def test_section_visible(self, page, server_url):
        page.goto(f"{server_url}/portfolio", wait_until="networkidle")
        section = page.query_selector("#mf-holdings-section")
        assert section is not None, "MF holdings section not found in DOM"
        assert section.is_visible(), "MF holdings section is not visible"

    def test_section_heading_text(self, page, server_url):
        page.goto(f"{server_url}/portfolio", wait_until="networkidle")
        h2 = page.query_selector("#mf-holdings-section h2")
        assert h2 is not None
        assert "Mutual Fund Holdings Trend" in h2.inner_text()

    def test_all_eight_tickers_present(self, page, server_url):
        page.goto(f"{server_url}/portfolio", wait_until="networkidle")
        rows = page.query_selector_all("#mf-holdings-section .mf-row")
        tickers = [r.get_attribute("data-ticker") for r in rows]
        assert sorted(tickers) == sorted(EXPECTED_TICKERS), (
            f"missing tickers: {set(EXPECTED_TICKERS) - set(tickers)}; "
            f"extra: {set(tickers) - set(EXPECTED_TICKERS)}"
        )

    def test_first_row_is_biggest_mover(self, page, server_url):
        """Largest |net_change| should appear first (RELIANCE ≈ 50.8M)."""
        page.goto(f"{server_url}/portfolio", wait_until="networkidle")
        rows = page.query_selector_all("#mf-holdings-section .mf-row")
        first_ticker = rows[0].get_attribute("data-ticker")
        assert first_ticker == "RELIANCE", f"expected RELIANCE first, got {first_ticker}"

    def test_net_change_displayed_correctly(self, page, server_url):
        """The first row should show +50,783,807 (RELIANCE's net change)."""
        page.goto(f"{server_url}/portfolio", wait_until="networkidle")
        rows = page.query_selector_all("#mf-holdings-section .mf-row")
        net_el = rows[0].query_selector(".mf-net")
        assert net_el is not None
        text = net_el.inner_text().replace(",", "").strip()
        # The cell shows "+50,783,807" → stripped to "+50783807"
        assert "+50783807" in text or text == "+50783807"


# ---------- Glossary ----------

class TestMFGlossary:
    def test_glossary_present_and_collapsed(self, page, server_url):
        page.goto(f"{server_url}/portfolio", wait_until="networkidle")
        glossary = page.query_selector(".mf-glossary")
        assert glossary is not None
        # <details> without `open` is collapsed by default
        assert glossary.get_attribute("open") is None

    def test_glossary_expands_on_click(self, page, server_url):
        page.goto(f"{server_url}/portfolio", wait_until="networkidle")
        page.click(".mf-glossary summary")
        page.wait_for_selector(".mf-glossary[open]", timeout=2000)
        text = page.inner_text(".mf-glossary-list")
        for term in ("AUM", "ETF", "FII", "NAV", "MF"):
            assert term in text, f"glossary missing {term!r}"


# ---------- Row click expand/collapse ----------

class TestMFHoldingsExpand:
    def test_click_row_expands_detail(self, page, server_url):
        page.goto(f"{server_url}/portfolio", wait_until="networkidle")
        row = page.query_selector('#mf-holdings-section .mf-row[data-ticker="ITC"]')
        detail = page.query_selector("#mf-detail-ITC")
        assert detail is not None
        # Initially hidden
        assert detail.get_attribute("hidden") is not None
        row.click()
        page.wait_for_selector("#mf-detail-ITC:not([hidden])", timeout=2000)
        assert row.get_attribute("aria-expanded") == "true"

    def test_detail_shows_buyer_and_seller(self, page, server_url):
        page.goto(f"{server_url}/portfolio", wait_until="networkidle")
        page.click('#mf-holdings-section .mf-row[data-ticker="ITC"]')
        page.wait_for_selector("#mf-detail-ITC:not([hidden])", timeout=2000)
        buyer = page.inner_text("#mf-detail-ITC .mf-detail-buyer .mf-detail-name")
        seller = page.inner_text("#mf-detail-ITC .mf-detail-seller .mf-detail-name")
        assert "Parag Parikh" in buyer, f"unexpected buyer: {buyer!r}"
        assert "Kotak Arbitrage" in seller, f"unexpected seller: {seller!r}"

    def test_click_again_collapses(self, page, server_url):
        page.goto(f"{server_url}/portfolio", wait_until="networkidle")
        row = page.query_selector('#mf-holdings-section .mf-row[data-ticker="ITC"]')
        row.click()
        page.wait_for_selector("#mf-detail-ITC:not([hidden])", timeout=2000)
        row.click()
        # The [hidden] element is by definition not visible, so we wait
        # for the attribute to be present (state="attached" is the default
        # for wait_for_selector if we use a function-style locator).
        page.wait_for_function(
            "document.getElementById('mf-detail-ITC').hasAttribute('hidden')",
            timeout=2000,
        )
        assert row.get_attribute("aria-expanded") == "false"

    def test_keyboard_enter_toggles(self, page, server_url):
        page.goto(f"{server_url}/portfolio", wait_until="networkidle")
        row = page.query_selector('#mf-holdings-section .mf-row[data-ticker="RELIANCE"]')
        row.focus()
        page.keyboard.press("Enter")
        page.wait_for_selector("#mf-detail-RELIANCE:not([hidden])", timeout=2000)
        assert row.get_attribute("aria-expanded") == "true"

    def test_detail_has_trendlyne_link(self, page, server_url):
        page.goto(f"{server_url}/portfolio", wait_until="networkidle")
        page.click('#mf-holdings-section .mf-row[data-ticker="RELIANCE"]')
        page.wait_for_selector("#mf-detail-RELIANCE:not([hidden])", timeout=2000)
        link = page.query_selector("#mf-detail-RELIANCE .mf-detail-link a")
        assert link is not None
        href = link.get_attribute("href")
        assert "trendlyne.com" in href
        assert "RELIANCE" in href


# ---------- Responsive ----------

class TestMFHoldingsResponsive:
    def test_table_has_scroll_wrapper(self, page, server_url):
        page.goto(f"{server_url}/portfolio", wait_until="networkidle")
        wrap = page.query_selector("#mf-holdings-section .table-wrap")
        assert wrap is not None
        assert wrap.query_selector("table") is not None

    def test_mobile_width_hides_expand_hint(self, page, server_url):
        page.set_viewport_size({"width": 375, "height": 800})
        page.goto(f"{server_url}/portfolio", wait_until="networkidle")
        hint = page.query_selector(".mf-expand-hint")
        if hint:
            display = hint.evaluate("el => getComputedStyle(el).display")
            assert display == "none", f"hint still visible: display={display}"


# ---------- API ----------

class TestMFHoldingsAPI:
    def test_api_endpoint_returns_8_rows(self, server_url):
        import requests
        r = requests.get(f"{server_url}/api/mf_holdings", timeout=10)
        assert r.status_code == 200
        data = r.json()
        assert data["row_count"] == 8
        tickers = {row["ticker"] for row in data["rows"]}
        assert tickers == set(EXPECTED_TICKERS)

    def test_api_row_shape(self, server_url):
        import requests
        r = requests.get(f"{server_url}/api/mf_holdings", timeout=10)
        row = r.json()["rows"][0]
        for key in ("ticker", "name", "total_mfs_holding", "mfs_bought",
                    "mfs_sold", "net_change_shares", "net_change_label",
                    "top_buyer", "top_seller", "url", "fetched_at"):
            assert key in row, f"missing key {key!r} in API row"

    def test_api_sorted_by_abs_net_change(self, server_url):
        import requests
        r = requests.get(f"{server_url}/api/mf_holdings", timeout=10)
        rows = r.json()["rows"]
        assert rows[0]["ticker"] == "RELIANCE"
        prev = float("inf")
        for row in rows:
            nc = abs(row["net_change_shares"] or 0)
            assert nc <= prev, f"rows not sorted by |net_change|: {prev} -> {nc}"
            prev = nc