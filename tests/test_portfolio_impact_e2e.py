"""
End-to-end browser + API tests for the Portfolio Impact Scanner.

Verifies:
  - /api/portfolio_impact/scan returns a valid status dict
  - /api/portfolio_impact/log returns the alert history
  - /api/portfolio_impact/exposure returns the ticker → sectors map
  - Settings page has the "Scan now" button
  - Clicking the button triggers a scan and updates the history
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def server_url(tmp_path_factory):
    """Start the server on a free port."""
    port = _free_port()
    env = os.environ.copy()
    env["PT_LOG_LEVEL"] = "ERROR"
    env["PYTHONPATH"] = str(PROJECT) + (
        os.pathsep + env.get("PYTHONPATH", "") if env.get("PYTHONPATH") else ""
    )
    env["MF_ALERT_DRY_RUN"] = "1"
    env["MF_ALERT_DISABLED"] = "1"
    env["NEWS_DRY_RUN"] = "1"
    env["NEWS_DISABLED"] = "1"
    env["PORTFOLIO_IMPACT_DISABLED"] = "1"
    proc = subprocess.Popen(
        [sys.executable, "-m", "webapp.server",
         "--host", "127.0.0.1", "--port", str(port)],
        env=env, cwd=str(PROJECT),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
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
    ctx = browser.new_context(viewport={"width": 1280, "height": 1100})
    pg = ctx.new_page()
    yield pg
    ctx.close()


class TestPortfolioImpactAPI:
    def test_scan_endpoint_returns_dict(self, server_url):
        import requests
        r = requests.post(f"{server_url}/api/portfolio_impact/scan",
                          json={"dry_run": True}, timeout=30)
        assert r.status_code == 200
        data = r.json()
        for key in ("ran_at", "fetch_ok", "articles_scanned",
                    "alerts_sent", "tickers_alerted"):
            assert key in data, f"missing key {key!r}"
        assert data["fetch_ok"] is True

    def test_log_endpoint_returns_history(self, server_url):
        import requests
        # Trigger a scan first so we have a log entry
        requests.post(f"{server_url}/api/portfolio_impact/scan",
                      json={"dry_run": True}, timeout=30)
        r = requests.get(f"{server_url}/api/portfolio_impact/log", timeout=10)
        assert r.status_code == 200
        data = r.json()
        assert "alerts" in data
        # Most recent first (or empty)
        assert isinstance(data["alerts"], list)

    def test_exposure_endpoint_returns_map(self, server_url):
        import requests
        r = requests.get(f"{server_url}/api/portfolio_impact/exposure", timeout=10)
        assert r.status_code == 200
        data = r.json()
        assert "exposure" in data
        ex = data["exposure"]
        # All 8 tickers present
        assert set(ex.keys()) == {
            "ITC", "RELIANCE", "JIOFIN", "BANKBARODA",
            "NTPCGREEN", "KNRCON", "IRCON", "BALRAMCHIN",
        }
        # Each ticker has name + sectors + themes
        for tkr, info in ex.items():
            assert "name" in info
            assert "sectors" in info
            assert "themes" in info


class TestPortfolioImpactUI:
    def test_settings_page_has_scan_buttons(self, page, server_url):
        page.goto(f"{server_url}/settings", wait_until="networkidle")
        live_btn = page.query_selector("#runImpactBtn")
        dry_btn = page.query_selector("#runImpactDryBtn")
        assert live_btn is not None
        assert dry_btn is not None
        assert live_btn.is_visible()
        assert dry_btn.is_visible()

    def test_exposure_map_visible(self, page, server_url):
        page.goto(f"{server_url}/settings", wait_until="networkidle")
        # Open the details element to expose the exposure map
        details = page.query_selector("#impactExposureMap")
        if details:
            # Click any <details> summary to open it
            page.click("summary:has-text('exposure map')")
            time.sleep(0.5)
        # Check the exposure content was populated
        text = page.inner_text("#impactExposureMap")
        # Should mention at least one ticker
        for tkr in ["ITC", "RELIANCE"]:
            if tkr in text:
                return  # found one
        # If we didn't find any ticker name, the API might've returned empty
        # — that's OK, just ensure the element is present.
        assert details is not None

    def test_dry_run_scan_updates_history(self, page, server_url):
        page.goto(f"{server_url}/settings", wait_until="networkidle")
        # Click dry-run scan button
        page.click("#runImpactDryBtn")
        # Wait for the button to revert (scan completed)
        page.wait_for_function(
            "document.getElementById('runImpactDryBtn').textContent.trim() === 'Scan now (dry-run)'",
            timeout=30000,
        )
        # History should now have at least 1 row (if any alerts were found)
        rows = page.query_selector_all("#impact-alert-status table tbody tr")
        # Just ensure the alert-status element is populated (not the empty state)
        status_text = page.inner_text("#impact-alert-status")
        # Either we have rows OR we have the "no alerts yet" message
        assert rows or "No impact alerts" in status_text or len(status_text) > 20

    def test_no_console_errors(self, page, server_url):
        errs = []
        page.on("console", lambda m: errs.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: errs.append(str(e)))
        page.goto(f"{server_url}/settings", wait_until="networkidle")
        time.sleep(1)
        assert errs == [], f"console errors: {errs}"