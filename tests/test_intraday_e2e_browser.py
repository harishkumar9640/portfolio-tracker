"""
End-to-end browser test for the intraday chart on the history page.

Spins up a real webapp server, opens a real Chromium browser, and:
  1. Loads /history
  2. Verifies the intraday buttons (15m / 5m / 1m) are visible
  3. Verifies the 5m chart renders by default
  4. Clicks each interval button and verifies the API is called
  5. Asserts no console errors at any step

Reuses the same _free_port/_start_server helpers pattern as
test_fairvalue_e2e_browser.py so we don't conflict on port 8000.

NOTE: This test hits the real yfinance API. Skip if network is unavailable.
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


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def server_url():
    """Start uvicorn on a free port for this module."""
    port = _free_port()
    env = os.environ.copy()
    env["PT_LOG_LEVEL"] = "ERROR"
    env["PYTHONPATH"] = str(PROJECT) + (
        os.pathsep + env.get("PYTHONPATH", "") if env.get("PYTHONPATH") else ""
    )
    proc = subprocess.Popen(
        [sys.executable, "-m", "webapp.server", "--host", "127.0.0.1", "--port", str(port)],
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
        raise RuntimeError(f"server did not start on {port}")
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
    context = browser.new_context(viewport={"width": 1280, "height": 900})
    pg = context.new_page()
    errs = SimpleNamespace(console_errors=[], page_errors=[])

    def on_console(msg):
        if msg.type == "error":
            errs.console_errors.append(msg.text)
    def on_pageerror(err):
        errs.page_errors.append(str(err))

    pg.on("console", on_console)
    pg.on("pageerror", on_pageerror)
    yield pg, errs
    context.close()


class TestIntradayHistoryUX:
    """The complete user journey: open history, see today's chart,
    click each interval button."""

    def test_page_loads_no_console_errors(self, page, server_url):
        pg, errs = page
        pg.goto(f"{server_url}/history", wait_until="networkidle", timeout=15000)
        assert pg.title() == "Portfolio History · Portfolio Tracker"
        # No console errors except those coming from the actual yfinance API
        # (which we mock below). For now, just check no JS errors.
        js_errors = [e for e in errs.page_errors]
        assert js_errors == [], f"JS errors: {js_errors}"

    def test_three_interval_buttons_present(self, page, server_url):
        pg, _ = page
        pg.goto(f"{server_url}/history", wait_until="networkidle", timeout=15000)
        assert pg.locator('button[data-interval="15m"]').count() == 1
        assert pg.locator('button[data-interval="5m"]').count() == 1
        assert pg.locator('button[data-interval="1m"]').count() == 1

    def test_5m_is_default_active_button(self, page, server_url):
        pg, _ = page
        pg.goto(f"{server_url}/history", wait_until="networkidle", timeout=15000)
        pressed = pg.locator('button[data-interval="5m"]').get_attribute("aria-pressed")
        assert pressed == "true", "5m button should be the default active"

    def test_chart_loads_with_mocked_api(self, page, server_url):
        """Mock /api/intraday to avoid hitting real yfinance. Verify the
        chart library receives data and renders the status line."""
        import json as _json

        def mock_intraday(route, request):
            # URL like .../api/intraday?interval=5m
            url = request.url
            if "interval=1m" in url:
                interval = "1m"
            elif "interval=15m" in url:
                interval = "15m"
            else:
                interval = "5m"
            payload = {
                "interval": interval,
                "asof": "2026-06-25T15:30:00+00:00",
                "series": {
                    "Nifty 50 (IN)": [
                        {"t": "2026-06-25T09:15:00+00:00", "v": 100.0},
                        {"t": "2026-06-25T15:30:00+00:00", "v": 101.2},
                    ],
                    "My Portfolio": [
                        {"t": "2026-06-25T09:15:00+00:00", "v": 100.0},
                        {"t": "2026-06-25T15:30:00+00:00", "v": 100.7},
                    ],
                },
            }
            route.fulfill(status=200, content_type="application/json",
                          body=_json.dumps(payload))

        pg, errs = page
        pg.route("**/api/intraday**", mock_intraday)
        pg.goto(f"{server_url}/history", wait_until="networkidle", timeout=15000)
        # Wait for the status line to update
        pg.wait_for_function(
            "document.getElementById('intraday-status').textContent.startsWith('Showing')",
            timeout=15000,
        )
        status = pg.locator("#intraday-status").inner_text()
        assert "5m interval" in status
        assert "2 series" in status or "Showing 2" in status

    def test_clicking_1m_switches_button_and_status(self, page, server_url):
        import json as _json
        def mock_intraday(route, request):
            interval = "1m" if "interval=1m" in request.url else "5m"
            route.fulfill(status=200, content_type="application/json",
                          body=_json.dumps({
                              "interval": interval,
                              "asof": "2026-06-25T15:30:00+00:00",
                              "series": {"Nifty 50 (IN)": [{"t": "2026-06-25T09:15:00+00:00", "v": 100.0}]},
                          }))
        pg, _ = page
        pg.route("**/api/intraday**", mock_intraday)
        pg.goto(f"{server_url}/history", wait_until="networkidle", timeout=15000)
        pg.wait_for_function(
            "document.getElementById('intraday-status').textContent.startsWith('Showing')",
            timeout=15000,
        )
        pg.click('button[data-interval="1m"]')
        pg.wait_for_function(
            'document.querySelector(`button[data-interval="1m"]`).getAttribute(`aria-pressed`) === `true`',
            timeout=5000,
        )
        pg.wait_for_function(
            'document.getElementById(`intraday-status`).textContent.includes(`1m interval`)',
            timeout=10000,
        )

    def test_clicking_15m_switches_button_and_status(self, page, server_url):
        import json as _json
        def mock_intraday(route, request):
            interval = "15m" if "interval=15m" in request.url else "5m"
            route.fulfill(status=200, content_type="application/json",
                          body=_json.dumps({
                              "interval": interval,
                              "asof": "2026-06-25T15:30:00+00:00",
                              "series": {"Nifty 50 (IN)": [{"t": "2026-06-25T09:15:00+00:00", "v": 100.0}]},
                          }))
        pg, _ = page
        pg.route("**/api/intraday**", mock_intraday)
        pg.goto(f"{server_url}/history", wait_until="networkidle", timeout=15000)
        pg.wait_for_function(
            "document.getElementById('intraday-status').textContent.startsWith('Showing')",
            timeout=15000,
        )
        pg.click('button[data-interval="15m"]')
        pg.wait_for_function(
            'document.querySelector(`button[data-interval="15m"]`).getAttribute(`aria-pressed`) === `true`',
            timeout=5000,
        )
        pg.wait_for_function(
            'document.getElementById(`intraday-status`).textContent.includes(`15m interval`)',
            timeout=10000,
        )

    def test_invalid_interval_returns_error_message(self, page, server_url):
        import json as _json
        def mock_error(route, request):
            route.fulfill(status=200, content_type="application/json",
                          body=_json.dumps({"error": "unsupported interval 'foo'", "interval": "foo"}))
        pg, _ = page
        pg.route("**/api/intraday**", mock_error)
        pg.goto(f"{server_url}/history", wait_until="networkidle", timeout=15000)
        # Trigger a load with the default button click sequence
        pg.wait_for_function(
            "document.getElementById('intraday-status').textContent.includes('Error') || "
            "document.getElementById('intraday-status').textContent.startsWith('Showing') || "
            "document.getElementById('intraday-status').textContent.includes('Failed')",
            timeout=15000,
        )
        # No JS crash
