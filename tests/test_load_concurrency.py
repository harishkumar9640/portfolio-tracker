"""Load and concurrency tests.

We don't run a full load test (would need locust/wrk), but we verify
that our concurrency primitives don't introduce deadlocks, races, or
memory leaks under modest pipeline.parallel load.
"""
from __future__ import annotations

import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))


def _has_psutil() -> bool:
    try:
        import psutil  # noqa: F401
        return True
    except ImportError:
        return False


# ---------- map_parallel / fetch_all ----------

class TestParallelCorrectness:
    """The pipeline.parallel helpers must preserve input order and not drop results."""

    def test_map_parallel_preserves_order(self):
        from pipeline.parallel import map_parallel

        def fn(x):
            time.sleep((10 - x) * 0.001)
            return x * 2

        result = map_parallel(fn, list(range(10)), workers=4)
        assert result == [x * 2 for x in range(10)]

    def test_map_parallel_workers_exceed_items(self):
        from pipeline.parallel import map_parallel
        result = map_parallel(lambda x: x + 1, [1, 2, 3], workers=100)
        assert result == [2, 3, 4]

    def test_fetch_all_runs_in_parallel(self):
        from pipeline.parallel import fetch_all
        DELAY = 0.05
        def slow():
            time.sleep(DELAY)
            return 1
        t0 = time.time()
        out = fetch_all({"a": slow, "b": slow, "c": slow, "d": slow})
        elapsed = time.time() - t0
        assert elapsed < 3 * DELAY, \
            f"fetch_all took {elapsed:.3f}s (serial would be {4*DELAY}s)"
        assert out == {"a": 1, "b": 1, "c": 1, "d": 1}


class TestParallelThreadSafety:
    """The HistoryDB module must be safe under concurrent reads/writes."""

    def test_concurrent_writes_dont_lose_rows(self, tmp_path):
        from pipeline.history_db import HistoryDB
        db = HistoryDB(tmp_path / "load_test.db")
        errors: list[Exception] = []

        def writer(i: int):
            try:
                for j in range(10):
                    db.record_sgb_prices([(
                        f"IN_ISIN_{i:03d}", f"2026-06-{j+1:02d}",
                        100.0 + i + j, "test",
                    )])
            except Exception as e:
                errors.append(e)

        with ThreadPoolExecutor(max_workers=20) as pool:
            futures = [pool.submit(writer, i) for i in range(100)]
            for f in futures:
                f.result()
        assert not errors, f"writers failed: {errors[:3]}"

        with db._tx() as c:
            count = c.execute("SELECT COUNT(*) AS n FROM sgb_price").fetchone()["n"]
        assert count == 1000, f"expected 1000 rows, got {count}"

    def test_concurrent_reads_and_writes(self, tmp_path):
        from pipeline.history_db import HistoryDB
        db = HistoryDB(tmp_path / "rw_test.db")
        db.record_sgb_prices([("IN000", "2026-06-25", 100.0, "test")])

        stop = threading.Event()
        errors: list[Exception] = []

        def reader():
            try:
                while not stop.is_set():
                    db.sgb_history("IN000")
                    db.portfolio_history("total")
            except Exception as e:
                errors.append(e)

        def writer():
            try:
                i = 0
                while not stop.is_set():
                    db.record_sgb_prices([(
                        f"IN_{i:04d}", "2026-06-25", 100.0, "test",
                    )])
                    i += 1
            except Exception as e:
                errors.append(e)

        threads = [
            threading.Thread(target=reader, daemon=True),
            threading.Thread(target=reader, daemon=True),
            threading.Thread(target=writer, daemon=True),
        ]
        for t in threads:
            t.start()
        time.sleep(0.5)
        stop.set()
        for t in threads:
            t.join(timeout=5)

        assert not errors, f"concurrent ops failed: {errors[:3]}"

    def test_lock_serialises_writes(self, tmp_path):
        from pipeline.history_db import HistoryDB
        db = HistoryDB(tmp_path / "lock_test.db")
        db.record_sgb_prices([("IN_A", "2026-06-25", 100.0, "v1")])
        db.record_sgb_prices([("IN_A", "2026-06-25", 200.0, "v2")])
        rows = db.sgb_history("IN_A")
        assert len(rows) == 1
        assert rows[0]["price"] == 200.0


# ---------- Web server concurrency ----------

class TestWebServerConcurrency:
    """The FastAPI app should handle a small burst of concurrent requests."""

    def _build_client(self):
        import tempfile
        csv = "SYMBOL,NAME OF COMPANY\nRELIANCE,Reliance Industries Limited\n"
        f = tempfile.NamedTemporaryFile(
            mode="w", suffix=".csv", delete=False, encoding="utf-8"
        )
        f.write(csv)
        f.close()
        import pipeline.fair_value.search as s
        s.CACHE_FILE = Path(f.name)
        s._index = []
        s._index_loaded_at = 0.0
        import webapp.data as wd
        import webapp.server as ws
        _stub = lambda force=False: {
            "asof": "2026-06-25", "indices": [],
            "equity": {"row": None, "holdings": [], "value": 0, "prev_value": 0},
            "mf": {"count": 0, "value": 0, "prev_value": 0, "pct": 0},
            "sgb": {"count": 0, "value": 0, "prev_value": 0, "pct": 0, "rows": []},
            "total": {"value": 0, "prev_value": 0, "pct": 0},
            "best_index": None, "worst_index": None,
        }
        wd.get_portfolio_snapshot = _stub
        ws.get_portfolio_snapshot = _stub
        wd.get_fairvalue_snapshot = lambda force=False: {"asof": "2026-06-25", "rows": []}
        ws.get_fairvalue_snapshot = wd.get_fairvalue_snapshot
        from fastapi.testclient import TestClient
        client = TestClient(ws.app)
        return client, Path(f.name)

    def test_concurrent_health_requests_succeed(self):
        client, csv_path = self._build_client()
        try:
            results: list[int] = []
            errors: list[Exception] = []

            def hit():
                try:
                    r = client.get("/api/health")
                    results.append(r.status_code)
                except Exception as e:
                    errors.append(e)

            threads = [threading.Thread(target=hit) for _ in range(10)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=10)

            assert not errors
            assert all(s == 200 for s in results)
        finally:
            csv_path.unlink(missing_ok=True)


# ---------- Smoke ----------

class TestWorkloadSmoke:
    def test_repeated_map_parallel_does_not_crash(self):
        from pipeline.parallel import map_parallel
        for _ in range(50):
            map_parallel(lambda x: x * 2, list(range(100)), workers=4)

    @pytest.mark.skipif(not _has_psutil(), reason="psutil not installed")
    def test_repeated_map_parallel_memory_bounded(self):
        import os
        import psutil
        from pipeline.parallel import map_parallel
        process = psutil.Process(os.getpid())
        baseline = process.memory_info().rss
        for _ in range(50):
            map_parallel(lambda x: x * 2, list(range(100)), workers=4)
        growth = process.memory_info().rss - baseline
        assert growth < 50_000_000, \
            f"Memory grew by {growth/1e6:.1f}MB after 50 iterations"
