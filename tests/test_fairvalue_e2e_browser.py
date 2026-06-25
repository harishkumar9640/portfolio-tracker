"""
End-to-end browser tests for the fair-value page.

These tests run a real headless browser against the live webapp
server, simulating the exact user interaction:
  1. Load /fairvalue
  2. Type a ticker
  3. Click "Calculate fair value"
  4. Verify the modal appears with all valuation data

Why this file exists:
  - Offline tests don't catch JavaScript syntax errors at runtime.
  - The ?? operator (ES2020) silently broke parsing in older browsers
    and headless test runners — clicks did nothing.
  - The market_cap field was dropped by the JSON serializer — the
    modal showed "—" instead of "₹3,63,417.00 Cr".
  - These bugs are invisible to unit tests; only a real browser catches them.

Requirements:
  pip install playwright pytest
  python -m playwright install chromium
"""
from __future__ import annotations

import os
import re as _re
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
    """A fresh page+context per test. Returns (page, errors) where
    errors.console_errors and errors.page_errors are lists populated
    by listeners attached at fixture-setup time."""
    context = browser.new_context(viewport={"width": 1280, "height": 900})
    pg = context.new_page()
    errs = SimpleNamespace(console_errors=[], page_errors=[])

    pg.on("console", lambda msg: errs.console_errors.append(msg.text)
        if msg.type == "error" else None)
    pg.on("pageerror", lambda err: errs.page_errors.append(str(err)))

    yield pg, errs
    context.close()


# ---------- User-journey tests ----------

class TestFairValueModalUX:
    """The complete user journey: type, click, see the dialog with results."""

    def test_page_loads_without_console_errors(self, page, server_url):
        pg, errs = page
        pg.goto(f"{server_url}/fairvalue", wait_until="networkidle")
        assert pg.title() == "Fair Value · Portfolio Tracker"
        assert errs.console_errors == [], \
            f"console errors on page load: {errs.console_errors}"
        assert errs.page_errors == [], \
            f"page errors on page load: {errs.page_errors}"

    def test_input_and_button_are_visible_and_enabled(self, page, server_url):
        pg, _ = page
        pg.goto(f"{server_url}/fairvalue", wait_until="networkidle")
        assert pg.locator("#lookupInput").is_visible()
        assert pg.locator("#lookupInput").is_enabled()
        assert pg.locator("#lookupSubmit").is_visible()
        assert pg.locator("#lookupSubmit").is_enabled()

    def test_clicking_calculate_opens_modal(self, page, server_url):
        pg, _ = page
        pg.goto(f"{server_url}/fairvalue", wait_until="networkidle")
        assert not pg.locator("#resultModal").is_visible(), \
            "modal must be hidden on page load"

        pg.fill("#lookupInput", "ITC")
        pg.click("#lookupSubmit")

        pg.wait_for_selector("#resultModal:not([hidden])", timeout=10000)
        assert pg.locator("#resultModal").is_visible()
        assert pg.locator(".modal-dialog").is_visible()

    def test_modal_shows_valuation_numbers(self, page, server_url):
        pg, _ = page
        pg.goto(f"{server_url}/fairvalue", wait_until="networkidle")
        pg.fill("#lookupInput", "ITC")
        pg.click("#lookupSubmit")
        pg.wait_for_selector("#resultModal:not([hidden])", timeout=10000)
        # Wait for the actual valuation data (not the loading state)
        pg.wait_for_selector(".modal-dialog:has-text('TWO-STAGE DCF')", timeout=10000)

        text = pg.locator(".modal-dialog").inner_text()
        assert "ITC" in text
        assert "TWO-STAGE DCF" in text.upper(), f"DCF missing from modal:\n{text}"
        assert pg.locator(".modal-close").is_visible()

    def test_three_tabs_present(self, page, server_url):
        """The modal now uses tabs: Summary, Calculation, Other methods.
        All three must be present so the user can find the math."""
        pg, _ = page
        pg.goto(f"{server_url}/fairvalue", wait_until="networkidle")
        pg.fill("#lookupInput", "ITC")
        pg.click("#lookupSubmit")
        pg.wait_for_selector("#resultModal:not([hidden])", timeout=10000)
        pg.wait_for_selector(".modal-dialog:has-text('TWO-STAGE DCF')", timeout=10000)

        tabs = pg.locator(".lookup-tab")
        assert tabs.count() == 3, f"expected 3 tabs, got {tabs.count()}"
        tab_labels = [tabs.nth(i).inner_text() for i in range(3)]
        assert "Summary" in tab_labels
        assert "Calculation" in tab_labels
        assert "Other methods" in tab_labels

    def test_calculation_tab_shows_full_math(self, page, server_url):
        """Regression: previously the math was hidden inside a collapsed
        <details> element. Users couldn't see the calculation on click.
        Now it lives behind a tab that's a single click away."""
        pg, _ = page
        pg.goto(f"{server_url}/fairvalue", wait_until="networkidle")
        pg.fill("#lookupInput", "ITC")
        pg.click("#lookupSubmit")
        pg.wait_for_selector("#resultModal:not([hidden])", timeout=10000)
        pg.wait_for_selector(".modal-dialog:has-text('TWO-STAGE DCF')", timeout=10000)
        # Click the Calculation tab
        pg.click('.lookup-tab[data-tab="calc"]')
        pg.wait_for_timeout(500)
        # The math panel must now be visible
        panel = pg.locator('.lookup-tab-panel[data-panel="calc"]')
        assert panel.is_visible(), "Calculation panel should be visible after clicking the tab"
        text = panel.inner_text()
        assert "VARIABLES USED" in text.upper(), "Variables table missing"
        assert "STEP-BY-STEP" in text.upper(), "Step-by-step math missing"
        assert "YEAR-BY-YEAR" in text.upper(), "Year-by-year table missing"
        assert "TERMINAL VALUE" in text.upper(), "Terminal value section missing"
        assert "ABBREVIATIONS" in text.upper(), "Abbreviations glossary missing"
        assert "REALITY CHECK" in text.upper(), "Reality check missing"

    def test_market_cap_is_not_a_dash(self, page, server_url):
        """Regression: market_cap was being dropped by the JSON
        serializer, so the modal showed '—' instead of the value."""
        pg, _ = page
        pg.goto(f"{server_url}/fairvalue", wait_until="networkidle")
        pg.fill("#lookupInput", "ITC")
        pg.click("#lookupSubmit")
        pg.wait_for_selector("#resultModal:not([hidden])", timeout=10000)
        pg.wait_for_selector(".modal-dialog:has-text('GRAHAM')", timeout=10000)

        text = pg.locator(".modal-dialog").inner_text()
        # Mkt Cap should show a numeric value in crores, not "—"
        m = _re.search(r"Mkt Cap\s*\n?\s*₹([\d,]+\.\d+)\s*Cr", text)
        assert m, \
            f"Mkt Cap should show a numeric value in crores. Modal text:\n{text}"
        cap_val = float(m.group(1).replace(",", ""))
        assert cap_val > 1000, \
            f"Mkt Cap looks too low ({cap_val}); field is probably being dropped"

    def test_close_button_dismisses_modal(self, page, server_url):
        pg, _ = page
        pg.goto(f"{server_url}/fairvalue", wait_until="networkidle")
        pg.fill("#lookupInput", "ITC")
        pg.click("#lookupSubmit")
        pg.wait_for_selector("#resultModal:not([hidden])", timeout=10000)

        pg.click(".modal-close")
        # Wait until the hidden attribute is back
        for _ in range(20):
            if pg.locator("#resultModal").get_attribute("hidden") is not None:
                break
            time.sleep(0.1)
        assert pg.locator("#resultModal").get_attribute("hidden") is not None

    def test_other_methods_tab_shows_graham(self, page, server_url):
        """Other methods tab should show Graham Number (hidden by default
        in Summary, available on this tab)."""
        pg, _ = page
        pg.goto(f"{server_url}/fairvalue", wait_until="networkidle")
        pg.fill("#lookupInput", "ITC")
        pg.click("#lookupSubmit")
        pg.wait_for_selector("#resultModal:not([hidden])", timeout=10000)
        pg.wait_for_selector(".modal-dialog:has-text('TWO-STAGE DCF')", timeout=10000)
        pg.click('.lookup-tab[data-tab="other"]')
        pg.wait_for_timeout(500)
        panel = pg.locator('.lookup-tab-panel[data-panel="other"]')
        assert panel.is_visible()
        text = panel.inner_text()
        # Graham is in the other methods section
        assert "Graham" in text, f"Graham missing from Other methods tab:\n{text}"

    def test_escape_key_dismisses_modal(self, page, server_url):
        pg, _ = page
        pg.goto(f"{server_url}/fairvalue", wait_until="networkidle")
        pg.fill("#lookupInput", "ITC")
        pg.click("#lookupSubmit")
        pg.wait_for_selector("#resultModal:not([hidden])", timeout=10000)

        pg.keyboard.press("Escape")
        for _ in range(20):
            if pg.locator("#resultModal").get_attribute("hidden") is not None:
                break
            time.sleep(0.1)
        assert pg.locator("#resultModal").get_attribute("hidden") is not None


# ---------- JS syntax safety (catches the ?? bug) ----------

class TestJSSyntaxSafety:
    """Defence in depth: don't reintroduce ES2020-only syntax."""

    def test_no_nullish_coalescing_in_fairvalue_js(self):
        """The original bug used `??` which breaks Safari < 13.1."""
        js = (PROJECT / "webapp" / "static" / "js" / "fairvalue.js").read_text()
        bad = _re.findall(r"(?<!\?)\?\?(?!\?)", js)
        assert not bad, \
            f"fairvalue.js uses nullish coalescing (??) which breaks older browsers: {bad[:5]}"

    def test_no_nullish_coalescing_in_app_js(self):
        js = (PROJECT / "webapp" / "static" / "js" / "app.js").read_text()
        bad = _re.findall(r"(?<!\?)\?\?(?!\?)", js)
        assert not bad, \
            f"app.js uses nullish coalescing (??): {bad[:5]}"

    def test_js_parses_with_node(self):
        """Run node --check on every JS file to catch any syntax error."""
        from shutil import which
        node = which("node")
        if not node:
            pytest.skip("node not installed; skipping JS syntax check")
        for js_file in (PROJECT / "webapp" / "static" / "js").glob("*.js"):
            result = subprocess.run(
                [node, "--check", str(js_file)],
                capture_output=True, text=True, timeout=10,
            )
            assert result.returncode == 0, \
                f"{js_file.name} has a syntax error:\n{result.stderr}"
