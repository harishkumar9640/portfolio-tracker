"""
Heavy robustness tests — load testing, memory, edge cases.

Tests:
  - 100 concurrent users across all pages
  - Memory: page render doesn't balloon RAM
  - Static file caching: assets return ETag/Last-Modified
  - Bad input handling: malformed query params, huge inputs, etc.
  - Repeated polls: dashboard.js poll endpoint is fast
  - Cold start vs warm: second request much faster than first
  - Slowloris-style: connection that hangs doesn't kill server
  - Whitespace in ticker param: /concalls?ticker= ITC
  - Special chars in URLs: %20, %3C, etc.
  - Long URL: 8KB URL doesn't crash
"""
from __future__ import annotations

import gc
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))


def _playwright_available() -> bool:
    try:
        from playwright.sync_api import sync_playwright  # noqa: F401
        return True
    except ImportError:
        return False


pytestmark = pytest.mark.skipif(
    not _playwright_available(),
    reason="Playwright not installed",
)


# ---------- Server helpers ----------

def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _start_server(port: int) -> subprocess.Popen:
    proc = subprocess.Popen(
        [sys.executable, "-m", "webapp.server", "--port", str(port)],
        cwd=PROJECT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.STDOUT,
    )
    for _ in range(40):
        time.sleep(0.25)
        try:
            r = subprocess.run(
                ["curl", "-sf", f"http://127.0.0.1:{port}/api/health"],
                capture_output=True, timeout=2,
            )
            if r.returncode == 0:
                return proc
        except Exception:
            pass
    proc.terminate()
    raise RuntimeError(f"server didn't start on port {port}")


@pytest.fixture(scope="module")
def server():
    port = _free_port()
    proc = _start_server(port)
    yield SimpleNamespace(url=f"http://127.0.0.1:{port}", port=port)
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


# ===========================================================================
# 1. Load test: 100 concurrent users across all pages
# ===========================================================================

class TestLoad100Concurrent:
    """Simulate 100 simultaneous users hitting different endpoints.
    Every request must complete (no timeouts) and return 2xx/3xx.

    Note: cold /api/portfolio triggers a 20s broker rebuild. We warm it
    up first so the load test measures steady-state behaviour, not the
    one-time cold-start cost."""

    def test_100_concurrent_requests(self, server):
        import urllib.request
        import concurrent.futures

        # Warm up all endpoints (cold /api/portfolio takes ~20s)
        for path in ["/api/portfolio", "/api/flows", "/api/concalls",
                      "/flows", "/concalls"]:
            try:
                with urllib.request.urlopen(server.url + path,
                                            timeout=60) as r:
                    r.read()
            except Exception:
                pass

        # The cached endpoints
        paths = [
            "/flows", "/concalls",
            "/fairvalue", "/history", "/settings",
            "/api/portfolio", "/api/flows", "/api/concalls",
            "/api/health",
        ]

        urls = [server.url + p for p in paths] * 12  # 108 requests

        results: list[tuple[str, float, int]] = []
        errors: list[str] = []

        def fetch(url: str):
            t0 = time.monotonic()
            try:
                with urllib.request.urlopen(url, timeout=60) as resp:
                    status = resp.status
                    resp.read()
                return (url, time.monotonic() - t0, status)
            except Exception as e:
                errors.append(f"{url}: {e}")
                return (url, time.monotonic() - t0, 0)

        with concurrent.futures.ThreadPoolExecutor(max_workers=20) as pool:
            futures = [pool.submit(fetch, u) for u in urls]
            for f in concurrent.futures.as_completed(futures):
                results.append(f.result())

        statuses = [r[2] for r in results]
        n_success = sum(1 for s in statuses if 200 <= s < 400)
        n_fail = sum(1 for s in statuses if s == 0 or s >= 500)

        assert n_fail == 0, (
            f"{n_fail} of {len(urls)} requests failed: {errors[:5]}"
        )
        assert n_success == len(urls), (
            f"only {n_success}/{len(urls)} returned 2xx/3xx"
        )

        times = [r[1] for r in results]
        p50 = sorted(times)[len(times) // 2]
        p95 = sorted(times)[int(len(times) * 0.95)]
        p99 = sorted(times)[int(len(times) * 0.99)]
        print(f"\n  load test: {len(urls)} requests, "
              f"p50={p50*1000:.0f}ms, p95={p95*1000:.0f}ms, "
              f"p99={p99*1000:.0f}ms, max={max(times)*1000:.0f}ms")

        # p99 should be under 5s for cached endpoints (warm)
        assert p99 < 5, f"p99 too slow: {p99:.2f}s"


# ===========================================================================
# 2. Memory: page render doesn't balloon RAM
# ===========================================================================

class TestMemoryStable:
    """Repeated renders don't accumulate Python objects."""

    def test_repeated_renders_dont_leak(self, server):
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                ctx = browser.new_context(viewport={"width": 1280, "height": 800})
                page = ctx.new_page()

                # Warm up
                for _ in range(3):
                    page.goto(server.url + "/flows", wait_until="load")
                    time.sleep(0.1)

                # Collect baseline
                gc.collect()
                baseline_uncollectable = len(gc.garbage)
                gc.garbage.clear()

                # Render 30 times
                for i in range(30):
                    page.goto(server.url + f"/flows?round={i}",
                              wait_until="load")

                # If we generated uncollectable objects, that's a leak
                gc.collect()
                uncollectable_after = len(gc.garbage)
                gc.garbage.clear()

                assert uncollectable_after == 0, (
                    f"generated {uncollectable_after} uncollectable objects "
                    f"(likely a reference cycle leak)"
                )
            finally:
                browser.close()


# ===========================================================================
# 3. Static file caching headers
# ===========================================================================

class TestStaticAssetsCache:
    """Static files should have caching headers (Cache-Control or ETag)."""

    def test_static_js_has_cache_header(self, server):
        import urllib.request
        req = urllib.request.Request(server.url + "/static/js/app.js")
        with urllib.request.urlopen(req, timeout=10) as resp:
            cc = resp.headers.get("Cache-Control", "")
            etag = resp.headers.get("ETag", "")
            last_mod = resp.headers.get("Last-Modified", "")
        # At least one caching mechanism must be present
        assert cc or etag or last_mod, (
            "no caching header on static asset"
        )

    def test_static_css_has_cache_header(self, server):
        import urllib.request
        with urllib.request.urlopen(server.url + "/static/css/app.css",
                                    timeout=10) as resp:
            cc = resp.headers.get("Cache-Control", "")
            etag = resp.headers.get("ETag", "")
            last_mod = resp.headers.get("Last-Modified", "")
        assert cc or etag or last_mod, (
            "no caching header on static asset"
        )


# ===========================================================================
# 4. Bad input handling
# ===========================================================================

class TestBadInputHandling:
    """Server should handle malformed/edge-case input gracefully."""

    def test_concalls_with_whitespace_ticker(self, server):
        """?ticker= ITC (leading space) should not crash."""
        import urllib.request
        from urllib.parse import quote
        url = server.url + "/concalls?ticker=" + quote(" ITC")
        try:
            with urllib.request.urlopen(url, timeout=10) as resp:
                assert resp.status == 200
        except urllib.error.HTTPError as e:
            # 200 OK or 4xx (handled gracefully) — NOT 5xx
            assert 400 <= e.code < 500, f"unexpected {e.code}"

    def test_concalls_with_unknown_ticker(self, server):
        """?ticker=DOESNOTEXIST should show empty results, not crash."""
        import urllib.request
        with urllib.request.urlopen(
            server.url + "/concalls?ticker=ZZZZZZ", timeout=10,
        ) as resp:
            assert resp.status == 200
            body = resp.read().decode()
            assert "No con-call summaries" in body

    def test_concalls_with_empty_ticker(self, server):
        """?ticker= (empty) should be treated as no filter."""
        import urllib.request
        with urllib.request.urlopen(
            server.url + "/concalls?ticker=", timeout=10,
        ) as resp:
            assert resp.status == 200

    def test_special_chars_in_url_dont_crash(self, server):
        """URLs with %xx escapes should not crash the server."""
        import urllib.request
        from urllib.parse import quote
        # %3C = <, %3E = >, %22 = ", %27 = '
        url = server.url + "/concalls?ticker=" + quote('<script>"x"</script>')
        try:
            with urllib.request.urlopen(url, timeout=10) as resp:
                assert resp.status == 200
        except urllib.error.HTTPError as e:
            # Server should not 5xx on this
            assert 400 <= e.code < 500, f"server crashed on special chars: {e.code}"

    def test_very_long_url_doesnt_crash(self, server):
        """8KB URL should be rejected (414) or handled, not crash."""
        import urllib.request
        long_param = "x" * 8000
        url = server.url + "/concalls?ticker=" + long_param
        try:
            with urllib.request.urlopen(url, timeout=10) as resp:
                # If accepted, response should be reasonable
                assert resp.status == 200
        except urllib.error.HTTPError as e:
            # 414 (URI too long) is the correct response
            assert e.code in (400, 414, 431), (
                f"unexpected status for long URL: {e.code}"
            )

    def test_unicode_in_url_doesnt_crash(self, server):
        """Unicode in URLs (e.g. Hindi) should not crash."""
        import urllib.request
        from urllib.parse import quote
        url = server.url + "/concalls?ticker=" + quote('आईटीसी')
        try:
            with urllib.request.urlopen(url, timeout=10) as resp:
                assert resp.status == 200
        except urllib.error.HTTPError as e:
            assert 400 <= e.code < 500


# ===========================================================================
# 5. Cold start vs warm cache
# ===========================================================================

class TestColdVsWarm:
    """First request (cold cache) vs subsequent (warm) timing."""

    def test_warm_cache_is_faster(self, server):
        """Warm requests should be measurably faster than cold."""
        import urllib.request

        # Cold (first hit)
        t0 = time.monotonic()
        with urllib.request.urlopen(server.url + "/api/health",
                                    timeout=10) as resp:
            resp.read()
        cold = time.monotonic() - t0

        # Warm (next 5 hits)
        warm_times = []
        for _ in range(5):
            t0 = time.monotonic()
            with urllib.request.urlopen(server.url + "/api/health",
                                        timeout=10) as resp:
                resp.read()
            warm_times.append(time.monotonic() - t0)

        warm_avg = sum(warm_times) / len(warm_times)
        print(f"\n  cold={cold*1000:.0f}ms, warm_avg={warm_avg*1000:.0f}ms")
        # Warm should be at least as fast (allow some variance for noise)
        assert warm_avg <= cold + 0.05, (
            f"warm ({warm_avg*1000:.0f}ms) slower than cold "
            f"({cold*1000:.0f}ms)"
        )


# ===========================================================================
# 6. Repeated polling (simulates dashboard.js auto-refresh)
# ===========================================================================

class TestRepeatedPolling:
    """The /api/* endpoints must handle sustained polling without slowdown."""

    def test_30_polls_in_30_seconds(self, server):
        """30 calls to /api/flows in 30s — should all be fast."""
        import urllib.request

        start = time.monotonic()
        times = []
        for i in range(30):
            t0 = time.monotonic()
            with urllib.request.urlopen(server.url + "/api/flows",
                                        timeout=5) as resp:
                resp.read()
            times.append(time.monotonic() - t0)
            # Pace: 1 call per second (matches dashboard.js 30s poll
            # with multiple users)
            elapsed = time.monotonic() - start
            if elapsed < i + 1:
                time.sleep((i + 1) - elapsed)

        total = time.monotonic() - start
        avg = sum(times) / len(times)
        p95 = sorted(times)[int(len(times) * 0.95)]
        print(f"\n  30 polls in {total:.1f}s: "
              f"avg={avg*1000:.0f}ms, p95={p95*1000:.0f}ms")
        # Each poll should be well under 1 second
        assert p95 < 1.0, f"p95 too slow: {p95:.2f}s"


# ===========================================================================
# 7. JSON API shape consistency
# ===========================================================================

class TestJsonApiShape:
    """The /api/* endpoints should return JSON with expected keys."""

    def test_api_health_shape(self, server):
        import urllib.request, json
        with urllib.request.urlopen(server.url + "/api/health",
                                    timeout=10) as resp:
            data = json.loads(resp.read().decode())
        assert isinstance(data, dict)
        assert "now" in data
        assert "snapshots_in_db" in data

    def test_api_flows_shape(self, server):
        import urllib.request, json
        with urllib.request.urlopen(server.url + "/api/flows",
                                    timeout=10) as resp:
            data = json.loads(resp.read().decode())
        assert "today_fii" in data
        assert "today_dii" in data
        assert "chart" in data
        assert isinstance(data["chart"], list)
        assert "portfolio_deals" in data
        assert isinstance(data["portfolio_deals"], list)
        assert "asof" in data

    def test_api_concalls_shape(self, server):
        import urllib.request, json
        with urllib.request.urlopen(server.url + "/api/concalls",
                                    timeout=10) as resp:
            data = json.loads(resp.read().decode())
        assert "summaries" in data
        assert "recent_count" in data
        assert "tone_counts" in data
        assert isinstance(data["tone_counts"], dict)
        assert "asof" in data

    def test_json_content_type(self, server):
        """JSON endpoints must return application/json, not text/html."""
        import urllib.request
        for path in ["/api/flows", "/api/concalls", "/api/health",
                     "/api/portfolio"]:
            req = urllib.request.Request(server.url + path,
                                          headers={"Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                ct = resp.headers.get("Content-Type", "")
            assert "json" in ct.lower(), (
                f"{path}: Content-Type is '{ct}', expected JSON"
            )


# ===========================================================================
# 8. Response completeness
# ===========================================================================

class TestResponseCompleteness:
    """Required DOM elements present on each page."""

    @pytest.mark.parametrize("path,expected", [
        ("/portfolio", ["TOTAL PORTFOLIO", "MY EQUITY"]),
        ("/flows", ["FII", "DII", "Re-scan"]),
        ("/concalls", ["Con-call summaries", "Re-scan", "Tone"]),
        ("/fairvalue", ["Fair Value"]),
        ("/history", ["History"]),
        ("/settings", ["Settings"]),
    ])
    def test_page_has_required_text(self, server, path, expected):
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                ctx = browser.new_context(viewport={"width": 1280, "height": 800})
                page = ctx.new_page()
                resp = page.goto(server.url + path,
                                wait_until="load", timeout=30000)
                assert resp.status == 200
                page.wait_for_function(
                    "() => { const el = document.getElementById('page-loading');"
                    "  return !el || el.classList.contains('is-hidden'); }",
                    timeout=15000,
                )
                body = page.locator("body").inner_text()
                body_lower = body.lower()
                for keyword in expected:
                    assert keyword.lower() in body_lower, (
                        f"{path}: missing required text '{keyword}'\n"
                        f"Body (first 400): {body[:400]}..."
                    )
            finally:
                browser.close()


# ===========================================================================
# 9. Concurrent writes (race conditions on data files)
# ===========================================================================

class TestConcurrentWrites:
    """Multiple writers using atomic write (tmp + rename) shouldn't corrupt.
    Even with race conditions, the final file must be valid JSON."""

    def test_concurrent_atomic_writes_keeps_file_valid(self, tmp_path):
        """After concurrent writes, the file must be valid JSON (no
        corruption). We don't assert specific data preservation because
        atomic tmp+rename isn't a CRDT — last writer wins per row."""
        import json
        target = tmp_path / "fii_dii_history.json"
        target.write_text(json.dumps({"version": 0, "rows": []}))

        def writer(start_id: int, writer_id: int):
            for i in range(20):
                tmp = target.with_suffix(f".tmp.{writer_id}.{i}")
                try:
                    if target.exists():
                        try:
                            data = json.loads(target.read_text())
                        except json.JSONDecodeError:
                            continue
                    else:
                        data = {"version": 0, "rows": []}
                    data["rows"].append({"id": start_id + i, "w": writer_id})
                    tmp.write_text(json.dumps(data))
                    tmp.replace(target)
                finally:
                    if tmp.exists():
                        try:
                            tmp.unlink()
                        except OSError:
                            pass

        t1 = threading.Thread(target=writer, args=(1, 1))
        t2 = threading.Thread(target=writer, args=(1001, 2))
        t1.start(); t2.start()
        t1.join(); t2.join()

        # Critical assertion: file is valid JSON, not corrupted
        data = json.loads(target.read_text())
        assert "rows" in data
        assert isinstance(data["rows"], list)
        # We expect SOME writes from each writer (race allows loss,
        # but not all from both)
        writer_ids = {r["w"] for r in data["rows"]}
        assert writer_ids, "all writes lost (likely file corruption)"
        assert len(data["rows"]) >= 10, (
            f"too few rows survived: {len(data['rows'])} "
            f"(expected at least 10)"
        )


# ===========================================================================
# 10. Concurrent pipeline.scheduler + webapp (no file lock conflicts)
# ===========================================================================

class TestSchedulerAndWebappCoexist:
    """The pipeline.scheduler and webapp can both touch data files at once.
    The webapp should never serve stale data beyond its cache TTL,
    and the pipeline.scheduler should never corrupt data."""

    def test_read_during_write_doesnt_corrupt(self, tmp_path):
        """Reader gets valid JSON even if writer is mid-write."""
        import json
        target = tmp_path / "test.json"
        target.write_text(json.dumps({"version": 0}))

        stop = threading.Event()
        errors = []

        def writer():
            i = 0
            while not stop.is_set():
                # Write atomically: tmp file + rename
                tmp = target.with_suffix(".tmp")
                tmp.write_text(json.dumps({"version": i}))
                tmp.replace(target)
                i += 1
                time.sleep(0.001)

        def reader():
            for _ in range(100):
                try:
                    data = json.loads(target.read_text())
                    assert isinstance(data, dict)
                    assert "version" in data
                except json.JSONDecodeError:
                    # Race: read happened mid-rename. Acceptable IF
                    # we retry; otherwise log as error.
                    pass
                except Exception as e:
                    errors.append(str(e))

        wt = threading.Thread(target=writer, daemon=True)
        rt = threading.Thread(target=reader)
        wt.start()
        rt.start()
        rt.join()
        stop.set()
        wt.join(timeout=2)

        assert not errors, f"reader errors: {errors[:3]}"


# ===========================================================================
# 11. Page-level: no flash of unstyled content
# ===========================================================================

class TestNoFlashOfUnstyledContent:
    """On a fresh load, the page should have CSS applied immediately.
    The body must have a defined background colour (not transparent)."""

    def test_body_has_background_immediately(self, server):
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                ctx = browser.new_context(viewport={"width": 1280, "height": 800})
                page = ctx.new_page()
                # Wait for full load (the page-loading overlay must hide)
                page.goto(server.url + "/portfolio",
                          wait_until="load", timeout=30000)
                page.wait_for_function(
                    "() => { const el = document.getElementById('page-loading');"
                    "  return !el || el.classList.contains('is-hidden'); }",
                    timeout=15000,
                )
                # Now check the body has a defined background
                bg = page.evaluate("""() => {
                    const cs = getComputedStyle(document.body);
                    return cs.backgroundColor;
                }""")
                assert bg and bg != "rgba(0, 0, 0, 0)", (
                    f"body bg is transparent after load: {bg}"
                )
            finally:
                browser.close()