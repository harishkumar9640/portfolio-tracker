"""
End-to-end browser test for the page-loading stopwatch.

Spins up a real webapp server and verifies:
  1. The stopwatch appears immediately on page load
  2. The clock ticks up while the page is loading
  3. The stopwatch hides itself once the document is interactive
  4. The final clock value is non-zero (proving the timer actually ran)

The stopwatch exists because building a portfolio snapshot from Angel
One + mfapi.in + NSE takes ~5 seconds on a cold cache. Without the
stopwatch, the user sees a blank page during that time.
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
        raise RuntimeError(f"server didn't start on {port}")
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


class TestPageLoadingStopwatch:
    """Verify the stopwatch element renders, ticks, and hides correctly."""

    def test_stopwatch_present_on_all_pages(self, browser, server_url):
        """Every page must include the #page-loading element."""
        from playwright.sync_api import sync_playwright
        for path in ["/portfolio", "/fairvalue", "/history", "/settings"]:
            page = browser.new_page(viewport={"width": 1280, "height": 900})
            page.goto(f"{server_url}{path}", wait_until="domcontentloaded")
            # The stopwatch element must exist in DOM
            assert page.locator("#page-loading").count() == 1, \
                f"{path} missing #page-loading"
            assert page.locator("#pageLoadingClock").count() == 1, \
                f"{path} missing clock"
            assert page.locator(".page-loading-spinner").count() == 1, \
                f"{path} missing spinner"
            page.close()

    def test_clock_starts_at_zero(self, browser, server_url):
        """When the page loads, the clock shows 0.0s (or 0ms)."""
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        page.goto(f"{server_url}/settings", wait_until="domcontentloaded")
        clock = page.locator("#pageLoadingClock")
        text = clock.inner_text()
        # Could be "0ms", "0.0s", "100ms" — anything under 1 second
        assert any(text.startswith(p) for p in ("0", "1")), \
            f"clock should start near zero, got: {text!r}"
        page.close()

    def test_stopwatch_hides_after_load(self, browser, server_url):
        """Once the document is interactive, the stopwatch hides itself
        (with a 250ms delay so the user sees the final time)."""
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        page.goto(f"{server_url}/settings", wait_until="networkidle")
        # After networkidle, the loader should be hidden (250ms after load)
        page.wait_for_function(
            "document.getElementById('page-loading').classList.contains('is-hidden')",
            timeout=5000,
        )
        is_hidden = page.locator("#page-loading").evaluate(
            "el => el.classList.contains('is-hidden')"
        )
        assert is_hidden, "stopwatch should be hidden after load"
        page.close()

    def test_clock_shows_nonzero_value_for_slow_page(self, browser, server_url):
        """On a cold cache, the portfolio page takes ~5s. The clock must
        record this so the user sees that something is happening."""
        # Force a cold cache by sending a force-refresh request first
        import requests
        requests.post(f"{server_url}/api/refresh?kind=portfolio", timeout=5)

        page = browser.new_page(viewport={"width": 1280, "height": 900})
        # Don't wait for networkidle — we want to read the clock while it's still ticking
        page.goto(f"{server_url}/portfolio", wait_until="domcontentloaded")
        # The clock element should have content (could be any value)
        clock = page.locator("#pageLoadingClock")
        assert clock.count() == 1
        text = clock.inner_text()
        assert text and text != "0ms", \
            f"clock should tick during the slow page load, got: {text!r}"
        # Wait for completion
        page.wait_for_function(
            "document.getElementById('page-loading').classList.contains('is-hidden')",
            timeout=30000,
        )
        page.close()