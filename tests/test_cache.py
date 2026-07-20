"""Tests for the webapp LRU cache."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from webapp.cache import LruJsonCache, cached_json, invalidate_json, clear_json_cache


# ---------- LruJsonCache basic behavior ----------

class TestLruJsonCacheBasic:
    def test_first_read_populates_cache(self, tmp_path):
        cache = LruJsonCache()
        f = tmp_path / "data.json"
        f.write_text(json.dumps({"a": 1, "b": [2, 3]}))
        result = cache.get(f)
        assert result == {"a": 1, "b": [2, 3]}
        assert len(cache) == 1

    def test_second_read_uses_cache(self, tmp_path):
        cache = LruJsonCache()
        f = tmp_path / "data.json"
        f.write_text(json.dumps({"a": 1}))
        cache.get(f)  # populate
        # If we read again with the file deleted, we should still get
        # the cached value (proving the file wasn't re-read from disk).
        f.unlink()
        result = cache.get(f)
        assert result == {"a": 1}

    def test_mtime_change_invalidates_cache(self, tmp_path):
        cache = LruJsonCache()
        f = tmp_path / "data.json"
        f.write_text(json.dumps({"a": 1}))
        assert cache.get(f) == {"a": 1}
        # Sleep so the mtime definitely changes (filesystem mtime
        # resolution can be 1s on some systems).
        time.sleep(1.1)
        f.write_text(json.dumps({"a": 2}))
        result = cache.get(f)
        assert result == {"a": 2}, "cache should have been invalidated by mtime change"

    def test_missing_file_returns_default(self, tmp_path):
        cache = LruJsonCache()
        f = tmp_path / "missing.json"
        result = cache.get(f, default={"empty": True})
        assert result == {"empty": True}
        assert len(cache) == 0

    def test_invalid_json_returns_default(self, tmp_path):
        cache = LruJsonCache()
        f = tmp_path / "broken.json"
        f.write_text("{not valid json")
        result = cache.get(f, default={"empty": True})
        assert result == {"empty": True}
        assert len(cache) == 0

    def test_invalidate_drops_entry(self, tmp_path):
        cache = LruJsonCache()
        f = tmp_path / "data.json"
        f.write_text(json.dumps({"a": 1}))
        cache.get(f)
        assert len(cache) == 1
        cache.invalidate(f)
        assert len(cache) == 0
        # Next read repopulates from disk
        f.write_text(json.dumps({"a": 2}))
        # mtime hasn't changed yet (just invalidate), so we need a touch
        time.sleep(1.1)
        f.write_text(json.dumps({"a": 2}))
        result = cache.get(f)
        assert result == {"a": 2}

    def test_clear_drops_everything(self, tmp_path):
        cache = LruJsonCache()
        for i in range(5):
            f = tmp_path / f"f{i}.json"
            f.write_text(json.dumps({"i": i}))
            cache.get(f)
        assert len(cache) == 5
        cache.clear()
        assert len(cache) == 0


# ---------- LRU eviction policy ----------

class TestLruJsonCacheEviction:
    def test_max_entries_enforced(self, tmp_path):
        cache = LruJsonCache(max_entries=3)
        files = []
        for i in range(5):
            f = tmp_path / f"f{i}.json"
            f.write_text(json.dumps({"i": i}))
            cache.get(f)
            files.append(f)
        # Only the last 3 should be in the cache (LRU)
        assert len(cache) == 3
        # Oldest (f0, f1) should be evicted; newest (f2, f3, f4) retained
        for f in files[:2]:
            assert str(f) not in cache._data
        for f in files[2:]:
            assert str(f) in cache._data

    def test_lru_order_updated_on_access(self, tmp_path):
        cache = LruJsonCache(max_entries=2)
        f0 = tmp_path / "f0.json"
        f1 = tmp_path / "f1.json"
        f0.write_text(json.dumps({"i": 0}))
        f1.write_text(json.dumps({"i": 1}))
        cache.get(f0)
        cache.get(f1)
        # Re-access f0 — it should now be the "newest", and f1 evicted
        # on next insert.
        cache.get(f0)
        f2 = tmp_path / "f2.json"
        f2.write_text(json.dumps({"i": 2}))
        cache.get(f2)
        # f0 retained, f1 evicted
        assert str(f0) in cache._data
        assert str(f2) in cache._data
        assert str(f1) not in cache._data


# ---------- Thread-safety ----------

class TestLruJsonCacheThreadSafe:
    def test_concurrent_reads(self, tmp_path):
        """Multiple threads reading the same file should not corrupt state."""
        import threading
        cache = LruJsonCache()
        f = tmp_path / "data.json"
        f.write_text(json.dumps({"a": 1}))
        errors = []
        def reader():
            try:
                for _ in range(100):
                    cache.get(f)
            except Exception as e:
                errors.append(e)
        threads = [threading.Thread(target=reader) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors, f"thread errors: {errors}"
        assert len(cache) == 1


# ---------- Module-level singleton ----------

class TestModuleLevelCache:
    def test_cached_json_returns_value(self, tmp_path, monkeypatch):
        # Use a temp directory and monkey-patch the module path
        clear_json_cache()
        f = tmp_path / "data.json"
        f.write_text(json.dumps({"a": 1}))
        assert cached_json(f) == {"a": 1}

    def test_cached_json_uses_mtime(self, tmp_path):
        clear_json_cache()
        f = tmp_path / "data.json"
        f.write_text(json.dumps({"a": 1}))
        assert cached_json(f) == {"a": 1}
        time.sleep(1.1)
        f.write_text(json.dumps({"a": 2}))
        assert cached_json(f) == {"a": 2}

    def test_invalidate_json_clears_entry(self, tmp_path):
        clear_json_cache()
        f = tmp_path / "data.json"
        f.write_text(json.dumps({"a": 1}))
        assert cached_json(f) == {"a": 1}
        invalidate_json(f)
        # File deleted; subsequent read returns default
        f.unlink()
        assert cached_json(f, default="missing") == "missing"

    def test_clear_json_cache(self, tmp_path):
        clear_json_cache()
        for i in range(3):
            f = tmp_path / f"f{i}.json"
            f.write_text(json.dumps({"i": i}))
            cached_json(f)
        # Singleton has 3 entries now
        from webapp.cache import _json_cache
        assert len(_json_cache) == 3
        clear_json_cache()
        assert len(_json_cache) == 0


# ---------- Integration with pipeline/portfolio_truth ----------

class TestLoadTruthCaching:
    """Verify that pipeline/portfolio_truth.load_truth() uses the cache
    so the file isn't re-read from disk on every call."""

    def test_load_truth_caches_within_same_process(self, tmp_path, monkeypatch):
        from pipeline import portfolio_truth
        # Point the truth file at a temp path
        monkeypatch.setattr(portfolio_truth, "TRUTH_FILE", tmp_path / "truth.json")
        (tmp_path / "truth.json").write_text(json.dumps({
            "schema_version": 1, "asof": "2026-07-03", "source": "test",
            "equity": {"RELIANCE": {"ticker": "RELIANCE", "qty": 1, "avg_price": 100.0}},
            "mutual_funds": {}, "sgbs": {}, "watchlist": [],
        }))
        # Reset the singleton cache (in case a previous test populated it)
        from webapp.cache import clear_json_cache
        clear_json_cache()
        # First call: file is read
        t1 = portfolio_truth.load_truth()
        assert t1["equity"]["RELIANCE"]["qty"] == 1
        # Delete the file; the second call should still return cached data
        (tmp_path / "truth.json").unlink()
        t2 = portfolio_truth.load_truth()
        assert t2["equity"]["RELIANCE"]["qty"] == 1
