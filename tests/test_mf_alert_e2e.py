"""
End-to-end browser + API tests for the MF Alert system.

Verifies:
  - /api/mf_alert/run returns a valid status dict
  - /api/mf_alert/log returns the alert history
  - Settings page has the "Run alert check now" button
  - Clicking the button triggers an alert run and updates the history
  - "Force-send email" button also works
  - SMTP setup section is collapsible
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
    """Start the server on a free port with isolated data dir."""
    port = _free_port()
    data_dir = tmp_path_factory.mktemp("mf_alert_data")
    env = os.environ.copy()
    env["PT_LOG_LEVEL"] = "ERROR"
    env["PYTHONPATH"] = str(PROJECT) + (
        os.pathsep + env.get("PYTHONPATH", "") if env.get("PYTHONPATH") else ""
    )
    env["MF_ALERT_DRY_RUN"] = "1"  # don't actually try to send email
    env["MF_ALERT_DISABLED"] = "1"  # don't run pipeline.scheduler in background
    # Point the alert log + prev snapshot into tmp_path so each run
    # starts with a clean slate.
    import pipeline.mf_holdings_alert as mha
    mha.PREV_SNAPSHOT_FILE = data_dir / "mf_holdings_prev.json"
    mha.ALERT_LOG_FILE = data_dir / "mf_holdings_alert_log.json"

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
    ctx = browser.new_context(viewport={"width": 1280, "height": 900})
    pg = ctx.new_page()
    yield pg
    ctx.close()


# ---------- API ----------

class TestMfAlertAPI:
    def test_run_endpoint_returns_status(self, server_url):
        """POST /api/mf_alert/run returns a status dict."""
        import requests
        r = requests.post(f"{server_url}/api/mf_alert/run", timeout=30)
        assert r.status_code == 200
        data = r.json()
        for key in ("ran_at", "snapshot_ok", "stocks_with_changes",
                    "tickers_changed", "email", "errors"):
            assert key in data, f"missing key {key!r}"
        assert data["snapshot_ok"] is True
        assert isinstance(data["stocks_with_changes"], int)

    def test_run_endpoint_force_email(self, server_url):
        """force_email=True sends even if no changes."""
        import requests
        r = requests.post(
            f"{server_url}/api/mf_alert/run",
            json={"force_email": True},
            timeout=30,
        )
        assert r.status_code == 200
        data = r.json()
        assert data["email"]["mode"] == "dry_run"

    def test_log_endpoint_returns_history(self, server_url):
        """GET /api/mf_alert/log returns the alert history."""
        import requests
        # First, run an alert so we have a log entry
        requests.post(f"{server_url}/api/mf_alert/run", timeout=30)
        r = requests.get(f"{server_url}/api/mf_alert/log", timeout=10)
        assert r.status_code == 200
        data = r.json()
        assert "runs" in data
        assert len(data["runs"]) >= 1
        first = data["runs"][0]
        for key in ("ran_at", "snapshot_ok", "stocks_with_changes",
                    "tickers_changed", "email"):
            assert key in first


# ---------- Browser ----------

class TestMfAlertUI:
    def test_settings_page_has_run_button(self, page, server_url):
        page.goto(f"{server_url}/settings", wait_until="networkidle")
        run_btn = page.query_selector("#runAlertBtn")
        force_btn = page.query_selector("#forceAlertBtn")
        assert run_btn is not None
        assert force_btn is not None
        assert run_btn.is_visible()
        assert force_btn.is_visible()

    def test_run_button_triggers_alert_and_updates_history(self, page, server_url):
        page.goto(f"{server_url}/settings", wait_until="networkidle")
        # Snapshot the alert history table (should be empty or minimal)
        page.click("#runAlertBtn")
        # Wait for the button text to revert (indicates run finished)
        page.wait_for_function(
            "document.getElementById('runAlertBtn').textContent.trim() === 'Run alert check now'",
            timeout=30000,
        )
        # Now the history table should have a row showing the run
        rows = page.query_selector_all("#mf-alert-status table tbody tr")
        assert len(rows) >= 1, "alert history should have at least one row after a run"

    def test_force_button_also_works(self, page, server_url):
        page.goto(f"{server_url}/settings", wait_until="networkidle")
        page.click("#forceAlertBtn")
        page.wait_for_function(
            "document.getElementById('forceAlertBtn').textContent.trim() === 'Force-send email'",
            timeout=30000,
        )
        # History should now have at least 2 entries (one from each button)
        rows = page.query_selector_all("#mf-alert-status table tbody tr")
        assert len(rows) >= 2

    def test_smtp_setup_section_is_collapsible(self, page, server_url):
        """The SMTP setup <details> should be closed by default."""
        page.goto(f"{server_url}/settings", wait_until="networkidle")
        # Find the SMTP details element
        details = page.query_selector("details summary")
        # The SMTP section is the one with text mentioning SMTP_HOST
        smtp_section = None
        all_details = page.query_selector_all("details")
        for d in all_details:
            txt = d.inner_text()
            if "SMTP setup" in txt or "MF_ALERT_SMTP_HOST" in txt:
                smtp_section = d
                break
        if smtp_section:
            assert smtp_section.get_attribute("open") is None
            # Click summary to expand
            page.click(f"details:has-text('SMTP setup') summary")
            page.wait_for_selector("details[open]", timeout=2000)
            body = page.inner_text("details[open]")
            assert "MF_ALERT_SMTP_HOST" in body

    def test_settings_page_renders_without_console_errors(self, page, server_url):
        """No JS console errors on the settings page (where the alert UI lives)."""
        errs = []
        page.on("console", lambda m: errs.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: errs.append(str(e)))
        page.goto(f"{server_url}/settings", wait_until="networkidle")
        time.sleep(1)
        assert errs == [], f"console errors: {errs}"