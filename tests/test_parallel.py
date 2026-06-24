"""
Tests for the parallel helpers.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

from parallel import map_parallel, fetch_all  # noqa: E402


def _slow_double(x: int) -> int:
    time.sleep(0.05)
    return x * 2


def _fail_on_three(x: int) -> int:
    if x == 3:
        raise ValueError("bad input")
    return x * 10


class TestMapParallel:
    def test_empty(self):
        assert map_parallel(_slow_double, []) == []

    def test_preserves_order(self):
        out = map_parallel(_slow_double, [3, 1, 2])
        assert out == [6, 2, 4]

    def test_parallel_speedup(self):
        # 8 items × 50ms each serially = 400ms; with workers=8 ≈ ~60ms.
        t0 = time.time()
        map_parallel(_slow_double, list(range(8)), workers=8, desc="t")
        elapsed = time.time() - t0
        # Allow plenty of slack; we just want serial-like run to be detectably slower.
        assert elapsed < 0.30

    def test_per_item_failures_dont_kill_batch(self):
        out = map_parallel(_fail_on_three, [1, 2, 3, 4, 5])
        # 1->10, 2->20, 3->None, 4->40, 5->50
        assert out == [10, 20, None, 40, 50]

    def test_on_error_fallback(self):
        def fallback(item, exc):
            return f"fb-{item}"
        out = map_parallel(_fail_on_three, [1, 3, 5], on_error=fallback)
        assert out == [10, "fb-3", 50]

    def test_workers_capped_to_n(self):
        # 2 items, workers=8 -> only 2 are actually spawned (we trust the executor)
        out = map_parallel(_slow_double, [1, 2], workers=8)
        assert out == [2, 4]


class TestFetchAll:
    def test_all_run_in_parallel(self):
        def f1(): time.sleep(0.1); return "a"
        def f2(): time.sleep(0.1); return "b"
        def f3(): time.sleep(0.1); return "c"
        t0 = time.time()
        out = fetch_all({"x": f1, "y": f2, "z": f3})
        # Should take ~0.1s not ~0.3s
        assert time.time() - t0 < 0.25
        assert out == {"x": "a", "y": "b", "z": "c"}

    def test_failure_returns_none(self):
        def ok(): return 1
        def bad(): raise RuntimeError("nope")
        out = fetch_all({"a": ok, "b": bad})
        assert out["a"] == 1
        assert out["b"] is None